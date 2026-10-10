"""failure_server — 失败 mock server 实现（task 25）。

task 25 契约测试的核心是"10 类失败行为 × 2 协议"的端到端 mock。本模块把
常见的失败模式抽到统一切换器里：

* HTTP：基于 :class:`pytest_httpserver.HTTPServer`，用 ``mode`` 字符串切换
  ``200 / 401 / 403 / 500 / missing_required / empty / slow``；
* HL7v2：基于 asyncio.start_server 的轻量 MLLP server，``mode`` 字符串切换
  ``MSA-1=AA | MSA-1=AE | MSA-1=AR | no-ack | slow | missing-PID3``。

凭据与脱敏纪律
--------------

* mock_server 返回的响应体**不**含真实凭据字面量（必要时仅含 ``<EXAMPLE>``
  占位符）；即便在 ``Authorization: Bearer`` 头部出现也是测试 fixture 注入的
  字面量，不进入 evidence / SQLite / Markdown / JSONL。
* 慢响应 / 无 ACK / 半关闭均通过真实 TCP 流控实现，不依赖修改测试受控代码；
  因此离线运行 pytest 完全闭环，不访问真实厂商接口。
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator, Optional, Tuple

import pytest
from pytest_httpserver import HTTPServer


# ---------------------------------------------------------------------------
# 常量与枚举
# ---------------------------------------------------------------------------

HTTP_SLOW_DELAY_S: float = 5.5
"""HTTP 慢响应延迟秒数（> 5s 阈值触发 ``degraded_slow``）。"""

MLLP_SLOW_DELAY_S: float = 5.5
"""MLLP 慢响应延迟秒数。"""

MLLP_DEFAULT_PORT_PLACEHOLDER: int = 0
"""start_mllp_server 默认 ``port=0``（随机空闲端口）。"""


class FailureScenarioHTTP(str, Enum):
    """HTTP mock 切换模式。"""

    HTTP_401 = "http_401"
    HTTP_403 = "http_403"
    HTTP_500 = "http_500"
    HTTP_HAPPY = "http_happy"
    HTTP_MISSING_REQUIRED = "http_missing_required"
    HTTP_EMPTY = "http_empty"
    HTTP_SLOW = "http_slow"


class FailureScenarioHL7(str, Enum):
    """MLLP mock 切换模式。"""

    MLLP_MSA_AA = "mlpp_msa_aa"
    MLLP_MSA_AE = "mlpp_msa_ae"
    MLLP_MSA_AR = "mlpp_msa_ar"
    MLLP_NO_ACK = "mlpp_no_ack"
    MLLP_SLOW = "mlpp_slow"
    MLLP_MISSING_PID3 = "mlpp_missing_pid3"


def free_port() -> int:
    """选一个当前空闲的本地端口并立即释放。

    用于构造"端口先占、后释放"的 unreachable-port 模拟。
    """

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def make_unreachable_port() -> int:
    """返回一个当前不可达端口（先占后释放）。

    比固定端口号更稳：避免测试在共享机器上撞端口冲突。
    """

    return free_port()


# ---------------------------------------------------------------------------
# HTTP mock server
# ---------------------------------------------------------------------------


def _make_httpserver(host: str, port: int) -> HTTPServer:
    server = HTTPServer(host=host, port=port)
    server.start()
    return server


def _respond_with_status(server: HTTPServer, status: int, body: str) -> None:
    server.expect_request("/").respond_with_data(body, status=status)


def _respond_with_json(server: HTTPServer, payload: dict) -> None:
    server.expect_request("/").respond_with_json(payload)


def _respond_with_delay(server: HTTPServer, delay_s: float, status: int, body: str) -> None:
    """挂一个会先睡 ``delay_s`` 再响应的 handler。"""

    def _handler(request):
        import time

        time.sleep(delay_s)
        from werkzeug import Response  # type: ignore

        return Response(body, status=status, mimetype="application/json")

    server.expect_request("/").respond_with_handler(_handler)


def make_http_server(
    mode: str | FailureScenarioHTTP = FailureScenarioHTTP.HTTP_500,
    *,
    port: int = 0,
    host: str = "127.0.0.1",
    slow_delay_s: float = HTTP_SLOW_DELAY_S,
) -> HTTPServer:
    """构造一个按 ``mode`` 切换行为的 HTTP mock server。

    Parameters
    ----------
    mode : str or FailureScenarioHTTP
        行为模式。取值见 :class:`FailureScenarioHTTP`。
    port : int
        默认 0（系统选空闲端口）；测试可显式指定。
    slow_delay_s : float
        ``HTTP_SLOW`` 模式延迟秒数。
    """

    if isinstance(mode, FailureScenarioHTTP):
        mode = mode.value

    server = _make_httpserver(host, port)
    try:
        if mode == FailureScenarioHTTP.HTTP_401.value:
            _respond_with_status(server, 401, "Unauthorized (test mock)")
        elif mode == FailureScenarioHTTP.HTTP_403.value:
            _respond_with_status(server, 403, "Forbidden (test mock)")
        elif mode == FailureScenarioHTTP.HTTP_500.value:
            _respond_with_status(server, 500, "Internal Server Error (test mock)")
        elif mode == FailureScenarioHTTP.HTTP_HAPPY.value:
            _respond_with_json(
                server,
                {
                    "code": 200,
                    "patient_id": "<EXAMPLE_PATIENT>",
                    "slot_id": "<EXAMPLE_SLOT>",
                    "report_id": "<EXAMPLE_REPORT>",
                },
            )
        elif mode == FailureScenarioHTTP.HTTP_MISSING_REQUIRED.value:
            _respond_with_json(
                server,
                {
                    "code": 200,
                    "msg": "mock returns partial fields (test mock)",
                    # missing patient_id and slot_id intentionally
                },
            )
        elif mode == FailureScenarioHTTP.HTTP_EMPTY.value:
            _respond_with_status(server, 200, "")
        elif mode == FailureScenarioHTTP.HTTP_SLOW.value:
            _respond_with_delay(
                server, slow_delay_s, 200,
                '{"code": 200, "patient_id": "<EXAMPLE_PATIENT>", '
                '"slot_id": "<EXAMPLE_SLOT>"}',
            )
        else:
            raise ValueError(f"未知 HTTP mock mode: {mode!r}")
    except Exception:
        # 配置失败时确保 server 被关闭，避免 fixture 泄漏
        try:
            server.stop()
        except Exception:
            pass
        raise
    return server


# ---------------------------------------------------------------------------
# HL7v2 MLLP mock server
# ---------------------------------------------------------------------------


_MLLP_START = b"\x0b"
_MLLP_END = b"\x1c"
_MLLP_CR = b"\x0d"


def _wrap_mllp(payload: bytes) -> bytes:
    return _MLLP_START + payload + _MLLP_END + _MLLP_CR


def _build_msa(msa1: str, ack_text_extra: str = "") -> bytes:
    """构造 MSA 段并打包成 MLLP 帧。

    简化版：``MSH|\\<encoding\\>\\^~\\\\&|MOCK|HIS|VENDOR|HIS|<ts>||ACK|...``
    后跟 ``MSA|<msa1>|...``。我们这里只关心 MSA-1（contract 校验），
    所以用最小可解析的 ACK 文本。
    """

    msa = f"MSA|{msa1}|test|MOCK-ACK\r".encode("utf-8")
    return _wrap_mllp(msa)


def _build_ack_with_pid3(msa1: str = "AA", include_pid: bool = True) -> bytes:
    """构造带 ``MSA|<msa1>`` + 可选 ``PID`` 段的 ACK。

    HL7 segment 分隔符用 ``\\r``；hl7apy 解析必需最小结构：MSH + MSA（assert
    步骤只看 MSA-1，PID-3 是 required_fields 任选验证位）。
    """

    msh = (
        "MSH|^~\\&|MOCK|HIS|VENDOR|HIS|20261010093000||ACK^A01|"
        "MOCK-ACK-12345|P|2.5\r"
    )
    msa = f"MSA|{msa1}|SUCCESS\r"
    pid = (
        "PID|1||<EXAMPLE_PATIENT_ID>||DOE^JOHN||19800101|M\r"
        if include_pid
        else ""
    )
    return _wrap_mllp((msh + msa + pid).encode("utf-8"))


@dataclass
class _MLLPServerHandle:
    """异步 MLLP server 控制句柄。"""

    host: str
    port: int
    server: asyncio.base_events.Server = field(repr=False)
    received: list = field(default_factory=list)
    request_count: int = 0


async def _mlpp_handler(  # noqa: D401 - kept for completeness, not used in production
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    mode: str,
    handle: _MLLPServerHandle,
    delay_s: float,
) -> None:
    """异步 handler（参考实现，未在线程模式使用）。

    当前生产路径采用 ``_start_mllp_blocking`` 内置的同步 socket.server，
    以避免 event-loop 与 sync MLLPClient 互相阻塞。下面保留 asyncio 版本
    以便未来把 MLLPClient 异步化时直接复用。
    """

    try:
        buf = bytearray()
        end_with_cr_idx = -1
        attempts = 0
        while end_with_cr_idx < 0 and attempts < 64:
            attempts += 1
            chunk = await reader.read(8192)
            if not chunk:
                return
            buf.extend(chunk)
            for i in range(len(buf) - 1):
                if buf[i] == 0x1C and buf[i + 1] == 0x0D:
                    end_with_cr_idx = i + 1
                    break
            if len(buf) > 4 * 1024 * 1024:
                return

        if end_with_cr_idx < 0:
            return

        start_idx = -1
        for i in range(end_with_cr_idx + 1):
            if buf[i] == 0x0B:
                start_idx = i
                break
        if start_idx < 0:
            return
        frame = bytes(buf[start_idx:end_with_cr_idx + 1])
        handle.received.append(frame)
        handle.request_count += 1

        if mode == FailureScenarioHL7.MLLP_NO_ACK.value:
            return
        if mode == FailureScenarioHL7.MLLP_SLOW.value:
            await asyncio.sleep(delay_s)
            writer.write(_build_msa("AA"))
        elif mode == FailureScenarioHL7.MLLP_MSA_AE.value:
            writer.write(_build_msa("AE"))
        elif mode == FailureScenarioHL7.MLLP_MSA_AR.value:
            writer.write(_build_msa("AR"))
        elif mode == FailureScenarioHL7.MLLP_MSA_AA.value:
            writer.write(_build_ack_with_pid3("AA", include_pid=True))
        elif mode == FailureScenarioHL7.MLLP_MISSING_PID3.value:
            writer.write(_build_ack_with_pid3("AA", include_pid=False))
        else:
            writer.write(_build_ack_with_pid3("AA", include_pid=True))
        await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
        return
    except Exception:
        return
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _start_mllp_under_loop(
    mode: str,
    port: int,
    delay_s: float,
) -> _MLLPServerHandle:
    """该 async 实现仅保留为参考；正式启动改走同步 socket.server 模式。"""

    handle = _MLLPServerHandle(host="127.0.0.1", port=0, server=None)

    async def _on_connect(r, w):
        await _mlpp_handler(r, w, mode, handle, delay_s)

    server = await asyncio.start_server(_on_connect, host="127.0.0.1", port=port)
    handle.server = server
    handle.host, handle.port = "127.0.0.1", server.sockets[0].getsockname()[1]
    return handle


def _start_mllp_blocking(
    mode: str,
    port: int,
    delay_s: float,
) -> _MLLPServerHandle:
    """用同步 socket server 在后台线程启动 MLLP mock（与 MLLPClient 兼容）。

    实现要点：
    * socketserver.TCPServer 用 ``daemon_threads=True`` 防僵尸；
    * 单条连接处理（BaseRequestHandler）同步读帧、构造响应、写回；
    * 后台线程启动 serve_forever，调用方通过 ``stop_mllp_server`` shutdown。
    """

    import socketserver
    import threading

    handle = _MLLPServerHandle(host="127.0.0.1", port=0, server=None)

    class _Handler(socketserver.BaseRequestHandler):
        def handle(self):  # noqa: D401 - sync handler
            sock = self.request
            try:
                sock.settimeout(20.0)
                buf = bytearray()
                end_with_cr_idx = -1
                attempts = 0
                while end_with_cr_idx < 0 and attempts < 64:
                    attempts += 1
                    chunk = sock.recv(8192)
                    if not chunk:
                        return
                    buf.extend(chunk)
                    for i in range(len(buf) - 1):
                        if buf[i] == 0x1C and buf[i + 1] == 0x0D:
                            end_with_cr_idx = i + 1
                            break
                    if len(buf) > 4 * 1024 * 1024:
                        return

                if end_with_cr_idx < 0:
                    return

                start_idx = -1
                for i in range(end_with_cr_idx + 1):
                    if buf[i] == 0x0B:
                        start_idx = i
                        break
                if start_idx < 0:
                    return
                frame = bytes(buf[start_idx:end_with_cr_idx + 1])
                handle.received.append(frame)
                handle.request_count += 1

                if mode == FailureScenarioHL7.MLLP_NO_ACK.value:
                    return  # 客户端 timeout
                elif mode == FailureScenarioHL7.MLLP_SLOW.value:
                    import time

                    time.sleep(delay_s)
                    sock.sendall(_build_msa("AA"))
                elif mode == FailureScenarioHL7.MLLP_MSA_AE.value:
                    sock.sendall(_build_msa("AE"))
                elif mode == FailureScenarioHL7.MLLP_MSA_AR.value:
                    sock.sendall(_build_msa("AR"))
                elif mode == FailureScenarioHL7.MLLP_MSA_AA.value:
                    sock.sendall(_build_ack_with_pid3("AA", include_pid=True))
                elif mode == FailureScenarioHL7.MLLP_MISSING_PID3.value:
                    sock.sendall(_build_ack_with_pid3("AA", include_pid=False))
                else:
                    sock.sendall(_build_ack_with_pid3("AA", include_pid=True))
            except (ConnectionResetError, BrokenPipeError, OSError):
                return
            finally:
                try:
                    sock.close()
                except Exception:
                    pass

    class _ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        daemon_threads = True
        allow_reuse_address = True

    server = _ThreadedTCPServer(("127.0.0.1", port), _Handler)
    handle.server = server
    handle.host, handle.port = "127.0.0.1", server.server_address[1]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    handle.__dict__["_thread"] = thread
    return handle


def start_mllp_server(
    mode: str | FailureScenarioHL7 = FailureScenarioHL7.MLLP_MSA_AA,
    *,
    port: int = MLLP_DEFAULT_PORT_PLACEHOLDER,
    slow_delay_s: float = MLLP_SLOW_DELAY_S,
) -> Tuple[str, int, "_MLLPServerHandle"]:
    """启动一个 MLLP mock server（同步入口，返回 ``(host, port, handle)``）。

    实现采用 :class:`socketserver.ThreadingMixIn` + :class:`socketserver.TCPServer`
    的后台线程方式，**不**使用 asyncio——后者无法与同步阻塞式
    :class:`vendor_if_doctor.adapters.mllp.MLLPClient` 共存（MLLPClient 在
    等待 ACK 时会阻塞主 event loop）。
    """

    if isinstance(mode, FailureScenarioHL7):
        mode = mode.value

    handle = _start_mllp_blocking(mode, port, slow_delay_s)
    return handle.host, handle.port, handle


def stop_mllp_server(handle) -> None:
    """关闭 :func:`start_mllp_server` 返回的 server（关闭 socket 后台线程）。"""

    if handle is None:
        return
    srv = getattr(handle, "server", None)
    if srv is not None:
        try:
            srv.shutdown()
        except Exception:
            pass
        try:
            srv.server_close()
        except Exception:
            pass
    thread = handle.__dict__.pop("_thread", None)
    if thread is not None:
        try:
            thread.join(timeout=2.0)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# pytest fixture 一键注入（可选）
# ---------------------------------------------------------------------------


# 注意：正式测试用 inline 调用 start_mllp_server + stop_mllp_server，避免
# pytest-asyncio event_loop 复用副作用。这里仅提供"基于 pytest_httpserver"的
# 即用 fixture，不引入新依赖。

@pytest.fixture
def mock_http_401_extended() -> Iterator[HTTPServer]:
    server = make_http_server(FailureScenarioHTTP.HTTP_401)
    try:
        yield server
    finally:
        try:
            server.stop()
        except Exception:
            pass


@pytest.fixture
def mock_http_403_extended() -> Iterator[HTTPServer]:
    server = make_http_server(FailureScenarioHTTP.HTTP_403)
    try:
        yield server
    finally:
        try:
            server.stop()
        except Exception:
            pass


@pytest.fixture
def mock_http_500_extended() -> Iterator[HTTPServer]:
    server = make_http_server(FailureScenarioHTTP.HTTP_500)
    try:
        yield server
    finally:
        try:
            server.stop()
        except Exception:
            pass


@pytest.fixture
def mock_http_slow_extended() -> Iterator[HTTPServer]:
    server = make_http_server(FailureScenarioHTTP.HTTP_SLOW)
    try:
        yield server
    finally:
        try:
            server.stop()
        except Exception:
            pass


__all__ = [
    "HTTP_SLOW_DELAY_S",
    "MLLP_SLOW_DELAY_S",
    "MLLP_DEFAULT_PORT_PLACEHOLDER",
    "FailureScenarioHTTP",
    "FailureScenarioHL7",
    "free_port",
    "make_http_server",
    "make_unreachable_port",
    "start_mllp_server",
    "stop_mllp_server",
    "mock_http_401_extended",
    "mock_http_403_extended",
    "mock_http_500_extended",
    "mock_http_slow_extended",
]
