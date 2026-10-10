"""MLLP — Minimal Lower Layer Protocol over TCP（task 7 基础层 + task 17 边界处理）。

MLLP 帧协议
----------

发送端包裹 HL7 消息：``\\x0b<message>\\x1c\\x0d``
  - ``\\x0b`` (VT, 0x0B) — 帧起始字节
  - ``\\x1c`` (FS, 0x1C) — 帧结束字节
  - ``\\x0d`` (CR, 0x0D) — 帧尾回车

接收端按相同规则剥壳。本模块**只**做帧封装/解封装 + TCP 收发，**不**做 HL7 段解析
（HL7 解析由 :mod:`vendor_if_doctor.adapters.hl7v2` 用 hl7apy 完成）。

设计要点
--------

* 单连接单 ACK 模式（HL7v2 巡检一次只发一条 ADT^A01 拿一条 ACK），不复用连接池；
* ACK 超时 5 秒（spec.timeout_seconds 默认 5.0；超过抛 :class:`HL7AckTimeout`）；
* 半关闭 ECONNRESET → :class:`MLLPHalfClosedError`（failure_class 走
  :func:`vendor_if_doctor.connection.error_pack.classify_exception` 归 ``tcp_half_closed``）；
* 超长消息 > 64KB 走分片发送（避免单帧 > 厂商 MLLP 缓冲区，本任务仅在 client 端做
  简单分片，不模拟接收端重组；典型厂商服务端支持 > 64KB 单帧）。
* 心跳帧 ``\\x0bVT\\x0b\\x1c\\x0d`` 仅发不收（task 17 新增）—— 厂商 MLLP 网关常驻场景
  用来探活但不必等 ACK；服务端忽略心跳的负载（payload = ``VT``）。

异常映射
--------

* :class:`MLLPTimeoutError` → 失败 ``failure_class='hl7_ack_missing'``（调用方在 hl7v2 adapter
  内部显式映射；本模块只抛异常）；
* :class:`MLLPHalfClosedError` → 失败 ``failure_class='tcp_half_closed'``；
* :class:`MLLPFrameError` → 失败 ``failure_class='schema_mismatch'``；
* :class:`MLLPConnectError` → 失败 ``failure_class='timeout'``。
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass
from typing import Optional

# MLLP 帧字节
MLLP_START = b"\x0b"  # VT
MLLP_END = b"\x1c"    # FS
MLLP_CR = b"\x0d"     # CR
"""MLLP 单帧完整序列：``start_byte + message + end_byte + cr``。"""

# 默认配置
MLLP_ACK_TIMEOUT_S = 5.0
"""ACK 接收超时（秒）。可在构造 :class:`MLLPClient` 时覆盖。"""

MLLP_MAX_FRAME_BYTES = 64 * 1024
"""单帧最大字节数（> 该值走分片发送）。"""

# 心跳帧（task 17 新增）
MLLP_HEARTBEAT_PAYLOAD = b"VT\x0b"
"""心跳帧负载：3 字节 ``VT\\x0b``（字母 VT 后接一个 VT 字节作为心跳标记）。

厂商 MLLP 网关识别"payload 内含 VT 字节"作为心跳；服务端可选择性忽略或返回业务无关 ACK。
按设计客户端**不**等 ACK。
"""

MLLP_HEARTBEAT_FRAME = MLLP_START + MLLP_HEARTBEAT_PAYLOAD + MLLP_END + MLLP_CR
"""完整心跳帧：``\\x0bVT\\x0b\\x1c\\x0d``（6 字节）。用于 MLLP 通道探活，**只发不收**。"""


# ---------------------------------------------------------------------------
# 异常类型
# ---------------------------------------------------------------------------


class MLLPError(Exception):
    """MLLP 客户端/服务端通用错误基类。"""


class MLLPConnectError(MLLPError):
    """TCP 连接失败（host/port 不可达 / ECONNREFUSED）。"""


class MLLPTimeoutError(MLLPError):
    """ACK 等待超时（HL7 ACK 缺失，failure_class='hl7_ack_missing' 派生）。"""


class MLLPHalfClosedError(MLLPError):
    """对端 TCP 半关闭 / ECONNRESET（failure_class='tcp_half_closed' 派生）。"""


class MLLPFrameError(MLLPError):
    """MLLP 帧格式异常（如缺 VT / FS / CR；frame 内容解析失败）。"""


# ---------------------------------------------------------------------------
# 帧封装
# ---------------------------------------------------------------------------


def wrap_frame(message: str | bytes) -> bytes:
    """把 HL7 消息包裹为 MLLP 帧（VT + msg + FS + CR）。"""

    if isinstance(message, str):
        message = message.encode("utf-8")
    return MLLP_START + message + MLLP_END + MLLP_CR


def _iter_frames(buffer: bytes) -> tuple[list[bytes], bytes]:
    """从 ``buffer`` 中切出一个或多个完整 MLLP 帧。

    返回 ``(frames, rest_buffer)``：已切出的帧列表与剩余不足一帧的字节。
    """

    frames: list[bytes] = []
    rest = buffer
    while True:
        start_idx = rest.find(MLLP_START)
        if start_idx < 0:
            # 没有 VT；保留 buffer 末尾字节（避免切到一半的 VT 丢失）
            return frames, rest[-1:] if rest else b""
        end_idx = rest.find(MLLP_END, start_idx + 1)
        if end_idx < 0:
            # 帧未完整：丢弃 VT 之前的字节，保留 VT 起的部分
            return frames, rest[start_idx:]
        # 期望 end_idx 后紧跟 CR
        cr_idx = end_idx + 1
        if cr_idx >= len(rest):
            return frames, rest[start_idx:]
        if rest[cr_idx : cr_idx + 1] != MLLP_CR:
            # 帧格式错误：FS 后没有 CR
            raise MLLPFrameError(
                f"MLLP 帧 FS 后未跟 CR：got={rest[end_idx:cr_idx + 1]!r}"
            )
        frames.append(rest[start_idx + 1 : end_idx])
        rest = rest[cr_idx + 1 :]
    return frames, rest  # pragma: no cover - unreachable


# ---------------------------------------------------------------------------
# MLLPClient
# ---------------------------------------------------------------------------


@dataclass
class MLLPResult:
    """MLLP send_and_recv 结果。"""

    ack: Optional[bytes]
    """ACK 帧字节（已剥壳）；超时时为 ``None``。"""
    latency_ms: int
    """send_and_recv 总耗时（含建立连接 + 发送 + 接收 ACK）。"""
    ack_received: bool
    """``True`` 时 :attr:`ack` 非空；``False`` 时是超时（failure_class='hl7_ack_missing'）。"""


class MLLPClient:
    """MLLP over TCP 客户端（单连接短生命周期）。

    Parameters
    ----------
    host : str
        MLLP 服务器地址；
    port : int
        MLLP 端口；
    timeout : float
        连接 / 收发超时秒数；
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout: float = MLLP_ACK_TIMEOUT_S,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = float(timeout)

    def send_and_recv(self, message: str | bytes) -> MLLPResult:
        """发送一条 HL7 消息，等待 ACK，返回 :class:`MLLPResult`。

        Raises
        ------
        MLLPConnectError
            TCP 连接失败（ECONNREFUSED / DNS / etc.）；
        MLLPHalfClosedError
            对端中途 ECONNRESET；
        MLLPFrameError
            帧格式异常（FS 后未跟 CR 等）。
        """

        if isinstance(message, str):
            message = message.encode("utf-8")

        start = time.monotonic()
        sock = socket.create_connection(
            (self.host, self.port), timeout=self.timeout
        )
        try:
            sock.settimeout(self.timeout)
            # 超长消息分片发送（每片都是完整帧；HL7 接收端按 MLLP 帧切分）
            payload = message
            if len(payload) > MLLP_MAX_FRAME_BYTES:
                # 分片：每片最大 MLLP_MAX_FRAME_BYTES
                chunks = [
                    payload[i : i + MLLP_MAX_FRAME_BYTES]
                    for i in range(0, len(payload), MLLP_MAX_FRAME_BYTES)
                ]
            else:
                chunks = [payload]

            for chunk in chunks:
                sock.sendall(wrap_frame(chunk))

            # 读取 ACK（按帧切分）
            buffer = b""
            deadline = start + self.timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    end = time.monotonic()
                    return MLLPResult(
                        ack=None,
                        latency_ms=max(0, int((end - start) * 1000)),
                        ack_received=False,
                    )
                sock.settimeout(remaining)
                try:
                    chunk_in = sock.recv(4096)
                except socket.timeout:
                    end = time.monotonic()
                    return MLLPResult(
                        ack=None,
                        latency_ms=max(0, int((end - start) * 1000)),
                        ack_received=False,
                    )
                if not chunk_in:
                    # 对端关闭
                    raise MLLPHalfClosedError(
                        f"MLLP 对端在 ACK 前关闭连接 {self.host}:{self.port}"
                    )
                buffer += chunk_in
                frames, buffer = _iter_frames(buffer)
                if frames:
                    end = time.monotonic()
                    return MLLPResult(
                        ack=frames[0],
                        latency_ms=max(0, int((end - start) * 1000)),
                        ack_received=True,
                    )
        except (ConnectionResetError, OSError) as exc:
            # ECONNRESET 等
            if isinstance(exc, ConnectionResetError):
                raise MLLPHalfClosedError(
                    f"MLLP ECONNRESET from {self.host}:{self.port}"
                ) from exc
            # OSError 兜底：socket.timeout 在上面已经处理
            raise MLLPConnectError(
                f"MLLP socket error {self.host}:{self.port}: {exc}"
            ) from exc
        finally:
            try:
                sock.close()
            except OSError:  # pragma: no cover
                pass

    def send_heartbeat(self) -> MLLPResult:
        """发送心跳帧 ``\\x0bVT\\x0b\\x1c\\x0d`` 并**只发不收**——连接可用即视为探活成功。

        心跳帧用于 MLLP 网关常驻探活：厂商服务端通常不返回 ACK（或返回业务无关 ACK），
        所以这里不等待 ACK；只要 TCP 连接建立 + 心跳帧成功 sendall 即视为成功。

        Returns
        -------
        MLLPResult
            :attr:`MLLPResult.ack_received` 始终为 ``False``（按设计）；:attr:`MLLPResult.latency_ms`
            只计连接建立 + sendall 耗时；:attr:`MLLPResult.ack` 始终为 ``None``。

        Raises
        ------
        MLLPConnectError
            TCP 连接失败（ECONNREFUSED / DNS / etc.）；
        MLLPHalfClosedError
            对端中途 ECONNRESET（sendall 阶段）；
        MLLPTimeoutError
            连接建立超时（socket.timeout）。
        """

        start = time.monotonic()
        sock = socket.create_connection(
            (self.host, self.port), timeout=self.timeout
        )
        try:
            sock.settimeout(self.timeout)
            try:
                sock.sendall(MLLP_HEARTBEAT_FRAME)
            except ConnectionResetError as exc:
                # 升级为 HalfClosed（心跳是 sendall 阶段，对端主动 RST）
                raise MLLPHalfClosedError(
                    f"MLLP heartbeat ECONNRESET from {self.host}:{self.port}"
                ) from exc
            except socket.timeout as exc:
                raise MLLPTimeoutError(
                    f"MLLP heartbeat timeout from {self.host}:{self.port}"
                ) from exc
            except OSError as exc:
                # 其他 socket 错误归为连接层错误
                raise MLLPConnectError(
                    f"MLLP heartbeat socket error {self.host}:{self.port}: {exc}"
                ) from exc
            end = time.monotonic()
            return MLLPResult(
                ack=None,
                latency_ms=max(0, int((end - start) * 1000)),
                ack_received=False,  # 心跳按设计不收 ACK
            )
        except (ConnectionResetError, OSError) as exc:
            # 兜底：与 send_and_recv 路径一致
            if isinstance(exc, ConnectionResetError):
                raise MLLPHalfClosedError(
                    f"MLLP heartbeat ECONNRESET from {self.host}:{self.port}"
                ) from exc
            raise MLLPConnectError(
                f"MLLP heartbeat socket error {self.host}:{self.port}: {exc}"
            ) from exc
        finally:
            try:
                sock.close()
            except OSError:  # pragma: no cover
                pass


__all__ = [
    "MLLP_ACK_TIMEOUT_S",
    "MLLP_HEARTBEAT_FRAME",
    "MLLP_HEARTBEAT_PAYLOAD",
    "MLLP_MAX_FRAME_BYTES",
    "MLLPClient",
    "MLLPConnectError",
    "MLLPError",
    "MLLPFrameError",
    "MLLPHalfClosedError",
    "MLLPResult",
    "MLLPTimeoutError",
    "wrap_frame",
]
