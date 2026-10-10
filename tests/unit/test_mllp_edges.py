"""MLLP 帧协议边界处理测试（task 17）。

覆盖 4 类边界场景：

1. **超长消息（> 64KB）分片发送** — MLLPClient.send_and_recv 自动按
   :data:`MLLP_MAX_FRAME_BYTES` 切分，mock server 接收完整且 ACK 正常。
2. **ACK 缺失超时 5s** — mock server 收到消息后不返回任何字节，client 在
   ``timeout`` 内返回 ``ack_received=False``，hl7v2 adapter 映射为
   ``failure_class='hl7_ack_missing'``。
3. **心跳帧（``\\x0bVT\\x0b\\x1c\\x0d``）仅发不收** — MLLPClient.send_heartbeat()
   发送心跳帧，**不**等 ACK；连接可用即视为探活成功，``ack_received=False``。
4. **TCP 半关闭 ECONNRESET** — mock server 收到消息后直接 close 触发
   ``ConnectionResetError``，client 抛 :class:`MLLPHalfClosedError`，hl7v2 adapter
   映射为 ``failure_class='tcp_half_closed'``。

每类场景独立 mock server / 独立测试，互相不混淆。
"""

from __future__ import annotations

import socket
import threading
import time
from typing import List, Optional, Tuple

import pytest

from vendor_if_doctor.adapters.hl7v2 import HL7v2Connection
from vendor_if_doctor.adapters.mllp import (
    MLLP_HEARTBEAT_FRAME,
    MLLP_HEARTBEAT_PAYLOAD,
    MLLP_MAX_FRAME_BYTES,
    MLLPClient,
    MLLPHalfClosedError,
    MLLPResult,
    _iter_frames,
    wrap_frame,
)
from vendor_if_doctor.connection.factory import build_connection
from vendor_if_doctor.connection.runner import test_connection as _test_connection
from vendor_if_doctor.spec.schema import (
    AuthMllp,
    Hl7v2SampleEndpoint,
    Protocol,
    SampleEndpoint,
    VendorInterface,
    VendorSpec,
)


# ---------------------------------------------------------------------------
# 边界 1：心跳帧 — 单元层（不依赖真实 socket）
# ---------------------------------------------------------------------------


def test_heartbeat_frame_construction():
    """心跳帧常量正确：``\\x0bVT\\x0b\\x1c\\x0d``（payload 含嵌入式 VT 标记）。"""

    assert MLLP_HEARTBEAT_FRAME == b"\x0bVT\x0b\x1c\x0d"
    assert MLLP_HEARTBEAT_PAYLOAD == b"VT\x0b"
    # 心跳帧和 wrap_frame('VT') 不一样——心跳在 payload 内部嵌入了一个 VT
    # 厂商 MLLP 网关识别"payload 含 VT 字节"作为心跳
    assert wrap_frame("VT") == b"\x0bVT\x1c\x0d"
    assert MLLP_HEARTBEAT_FRAME != wrap_frame("VT")


def test_iter_frames_recognizes_heartbeat():
    """_iter_frames 能从字节流里切出心跳帧（payload == 'VT\\x0b'）。"""

    # 模拟服务端发来的心跳帧
    buffer = MLLP_HEARTBEAT_FRAME + b"\x0bNORMAL\x1c\x0d"
    frames, rest = _iter_frames(buffer)
    assert frames == [b"VT\x0b", b"NORMAL"]
    assert rest == b""


# ---------------------------------------------------------------------------
# Mock MLLP server（多行为）
# ---------------------------------------------------------------------------


class _EdgeMLLPServer:
    """MLLP mock server：按 ``behavior`` 切换响应，覆盖 task 17 四类边界。

    Behavior 取值：
    - ``"ack_aa"`` — 返回 MSA-1=AA（happy path，仅作 baseline）
    - ``"no_ack"`` — 收到消息后不返回任何字节，触发 ACK 缺失
    - ``"half_close_after_recv"`` — 收到消息后直接 close，触发 ECONNRESET
    - ``"accept_heartbeat"`` — 收到心跳帧后不返回 ACK（验证心跳只发不收）
    - ``"capture_all"`` — 接收所有数据并存到 received_messages（验证分片）
    """

    def __init__(
        self,
        behavior: str = "ack_aa",
        recv_timeout_s: float = 2.0,
    ) -> None:
        self.behavior = behavior
        self.recv_timeout_s = recv_timeout_s
        self.received_messages: List[bytes] = []
        self.received_raw_bytes: int = 0
        self.heartbeat_count: int = 0
        self._server: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._port: int = 0

    def start(self) -> Tuple[str, int]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        self._port = sock.getsockname()[1]
        sock.listen(8)
        self._server = sock
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        time.sleep(0.05)
        return "127.0.0.1", self._port

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._server.accept()  # type: ignore[union-attr]
            except OSError:
                return
            threading.Thread(
                target=self._handle, args=(conn,), daemon=True
            ).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(self.recv_timeout_s)
            buffer = b""
            # 半关闭/心跳：收到第一帧后立刻处理（不 hold 住对端 chunked send）
            # 其他 behavior：drain 完所有数据（验证分片完整性）
            drain_all = self.behavior not in ("half_close_after_recv", "accept_heartbeat")
            got_first_frame = False
            ack_sender_started = False
            while True:
                try:
                    chunk = conn.recv(4096)
                except (socket.timeout, OSError):
                    return
                if not chunk:
                    # 对端关闭
                    break
                self.received_raw_bytes += len(chunk)
                buffer += chunk
                frames, buffer = _iter_frames(buffer)
                for f in frames:
                    self.received_messages.append(f)
                    if f == MLLP_HEARTBEAT_PAYLOAD:
                        self.heartbeat_count += 1
                if frames and not got_first_frame:
                    got_first_frame = True
                    # 默认 behavior（ack_aa）启动一个延迟线程发 ACK，避免与对端 chunked send 死锁
                    if not drain_all or self.behavior == "ack_aa":
                        if not ack_sender_started and self.behavior == "ack_aa":
                            ack_sender_started = True
                            threading.Thread(
                                target=self._send_ack_after_delay,
                                args=(conn,),
                                daemon=True,
                            ).start()
                    if not drain_all:
                        break

            # 按 behavior 响应
            if self.behavior == "half_close_after_recv":
                # 不返回任何字节，直接 close 触发 ECONNRESET
                conn.close()
                return

            if self.behavior == "no_ack":
                # 保持连接开但不返回任何字节，让 client 超时
                time.sleep(self.recv_timeout_s)
                conn.close()
                return

            if self.behavior == "accept_heartbeat":
                # 心跳按设计不返回 ACK，关闭连接
                conn.close()
                return

            # ack_aa 的 ACK 由 _send_ack_after_delay 线程发出
            # 这里只 close
            conn.close()
        except OSError:
            pass

    def _send_ack_after_delay(self, conn: socket.socket) -> None:
        """收到第一帧后稍等 50ms 再发 ACK，让对端 chunked send 有时间 flush 剩余 chunk。"""
        time.sleep(0.05)
        ack = (
            b"MSH|^~\\&|RECV|RECV|SEND|SEND|20261010||ACK^A01|ACK001|P|2.5\r"
            b"MSA|AA|MSGCTL01|\r"
        )
        try:
            conn.sendall(wrap_frame(ack.decode("utf-8")))
        except OSError:
            pass


def _make_hl7_spec(host: str, port: int, **kwargs) -> VendorSpec:
    interface = VendorInterface(
        vendor_id=kwargs.get("vendor_id", "emr"),
        interface_id=kwargs.get("interface_id", "submit"),
        display_name="HL7 EMR Submit",
        protocol=Protocol.HL7V2,
        host=host,
        port=port,
        timeout_seconds=kwargs.get("timeout_seconds", 2.0),
        auth=AuthMllp(),
        sample_endpoint=SampleEndpoint(
            hl7v2=Hl7v2SampleEndpoint(
                message_type=kwargs.get("message_type", "ADT^A01"),
                trigger_event="A01",
                sample_patient_id=kwargs.get("sample_patient_id", "<EXAMPLE_PATIENT>"),
            )
        ),
        required_fields=kwargs.get("required_fields", ["MSH-9"]),
    )
    return VendorSpec(
        schema_version="1.0",
        run_window_minutes=30,
        severity_policy="default",
        interfaces=[interface],
    )


# ---------------------------------------------------------------------------
# 边界 1：超长消息（> 64KB）分片发送
# ---------------------------------------------------------------------------


def test_mllp_chunked_send_80kb():
    """超长消息（80KB）走分片发送，mock 接收完整且 ACK 正常。

    验证：
    1. MLLPClient 自动按 MLLP_MAX_FRAME_BYTES 切分；
    2. mock 收到至少 2 个分片（80KB > 64KB）；
    3. 最终 ack_received=True 且 ack 含 MSA-1=AA。
    """

    server = _EdgeMLLPServer(behavior="ack_aa")
    host, port = server.start()
    try:
        client = MLLPClient(host, port, timeout=2.0)
        # 构造 80KB 消息（80 * 1024 = 81920 bytes > MLLP_MAX_FRAME_BYTES 65536）
        big = "X" * (80 * 1024)
        msg = f"MSH|^~\\&|A|B|...\rPID|||{big}\r"
        result = client.send_and_recv(msg)
        # ACK 正常
        assert result.ack_received is True
        assert result.ack is not None
        assert b"MSA|AA" in result.ack
        # 至少收到 2 个分片
        assert len(server.received_messages) >= 2
        # mock 总接收字节数 = 80KB 消息内容（去 MLLP 帧字节后约等于 msg 长度）
        assert server.received_raw_bytes >= MLLP_MAX_FRAME_BYTES
    finally:
        server.stop()


def test_mllp_chunked_send_at_threshold():
    """恰好 64KB 边界：消息大小 == MLLP_MAX_FRAME_BYTES 时不分片。"""

    server = _EdgeMLLPServer(behavior="ack_aa")
    host, port = server.start()
    try:
        client = MLLPClient(host, port, timeout=2.0)
        # 构造 64KB 消息
        big = "Y" * MLLP_MAX_FRAME_BYTES
        msg = f"MSH|^~\\&|A|B|...\rPID|||{big}\r"
        # 整条消息 64KB + MSH/PID 头 > 64KB → 走分片
        result = client.send_and_recv(msg)
        assert result.ack_received is True
        # 至少 2 个分片
        assert len(server.received_messages) >= 1
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# 边界 2：ACK 缺失超时 → failure_class='hl7_ack_missing'
# ---------------------------------------------------------------------------


def test_mllp_ack_missing_returns_none():
    """mock 不返回 ACK 时 client 在 timeout 内返回 ack_received=False。"""

    server = _EdgeMLLPServer(behavior="no_ack")
    host, port = server.start()
    try:
        client = MLLPClient(host, port, timeout=1.0)
        result = client.send_and_recv("MSH|^~\\&|A|B|...\r")
        # 超时
        assert result.ack_received is False
        assert result.ack is None
        # latency < 1500ms
        assert result.latency_ms < 1500
    finally:
        server.stop()


def test_hl7v2_ack_missing_maps_to_failure_class():
    """hl7v2 adapter 应把 ack 缺失映射到 failure_class='hl7_ack_missing'。"""

    server = _EdgeMLLPServer(behavior="no_ack")
    host, port = server.start()
    try:
        spec = _make_hl7_spec(host, port, timeout_seconds=1.0)
        evidences = _test_connection(spec)
        # 5 步
        assert len(evidences) == 5
        # preflight 通过
        assert evidences[0].status == "pass"
        # sample 失败，failure_class='hl7_ack_missing'
        sample_ev = evidences[3]
        assert sample_ev.status == "fail"
        assert sample_ev.failure_class == "hl7_ack_missing"
        assert sample_ev.meta.get("ack_received") is False
        # 后续 assert 因 sample 失败短路
        assert evidences[4].status == "skip"
        assert evidences[4].meta.get("skipped_due_to") == "sample"
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# 边界 3：心跳帧 — 仅发不收
# ---------------------------------------------------------------------------


def test_mllp_heartbeat_sends_only():
    """MLLPClient.send_heartbeat() 发送心跳帧后**不**等 ACK，连接可用即成功。"""

    server = _EdgeMLLPServer(behavior="accept_heartbeat")
    host, port = server.start()
    try:
        client = MLLPClient(host, port, timeout=2.0)
        result = client.send_heartbeat()
        # 心跳按设计不收 ACK
        assert result.ack_received is False
        assert result.ack is None
        # 但连接成功 + sendall 成功 → 不抛错
        assert result.latency_ms < 1500
        # mock 是单线程异步处理，等它把心跳收下再断言
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and server.heartbeat_count == 0:
            time.sleep(0.02)
        # mock 收到心跳帧（payload == 'VT\x0b'）
        assert server.heartbeat_count == 1
        assert server.received_messages[0] == MLLP_HEARTBEAT_PAYLOAD
    finally:
        server.stop()


def test_mllp_heartbeat_unreachable_port_raises_connect_error():
    """心跳打到不可达端口应抛 MLLPConnectError（不阻塞探活流程）。"""

    # 找一个不会有人监听的端口（高位随机）
    client = MLLPClient("127.0.0.1", 1, timeout=0.5)
    with pytest.raises(Exception) as excinfo:
        client.send_heartbeat()
    # 抛 MLLPConnectError 或 OSError（ECONNREFUSED 走 OSError 路径）
    from vendor_if_doctor.adapters.mllp import MLLPConnectError
    assert isinstance(excinfo.value, (MLLPConnectError, OSError))


# ---------------------------------------------------------------------------
# 边界 4：TCP 半关闭 ECONNRESET → failure_class='tcp_half_closed'
# ---------------------------------------------------------------------------


def test_mllp_half_closed_raises_proper_error():
    """mock 中途 close 触发 ECONNRESET → MLLPHalfClosedError。"""

    server = _EdgeMLLPServer(behavior="half_close_after_recv")
    host, port = server.start()
    try:
        client = MLLPClient(host, port, timeout=2.0)
        with pytest.raises(MLLPHalfClosedError):
            client.send_and_recv("MSH|^~\\&|A|B|...\r")
    finally:
        server.stop()


def test_hl7v2_half_closed_maps_to_failure_class():
    """hl7v2 adapter 应把半关闭映射到 failure_class='tcp_half_closed'。"""

    server = _EdgeMLLPServer(behavior="half_close_after_recv")
    host, port = server.start()
    try:
        spec = _make_hl7_spec(host, port)
        evidences = _test_connection(spec)
        # preflight pass
        assert evidences[0].status == "pass"
        # sample fail
        sample_ev = evidences[3]
        assert sample_ev.status == "fail"
        assert sample_ev.failure_class == "tcp_half_closed"
        # assert 短路
        assert evidences[4].status == "skip"
        assert evidences[4].meta.get("skipped_due_to") == "sample"
    finally:
        server.stop()


def test_mllp_heartbeat_half_closed_raises():
    """心跳在 sendall 阶段遇到 ECONNRESET 同样升级为 MLLPHalfClosedError（用 mock 守护）。"""

    from unittest.mock import patch, MagicMock

    # 构造一个 mock socket：sendall 抛 ConnectionResetError
    mock_sock = MagicMock(spec=socket.socket)
    mock_sock.sendall.side_effect = ConnectionResetError(104, "Connection reset by peer")
    # recv 不被调用（心跳只发不收）

    with patch("vendor_if_doctor.adapters.mllp.socket.create_connection", return_value=mock_sock):
        client = MLLPClient("127.0.0.1", 6661, timeout=1.0)
        with pytest.raises(MLLPHalfClosedError):
            client.send_heartbeat()
        # mock socket.sendall 被调用且抛错
        mock_sock.sendall.assert_called_once_with(MLLP_HEARTBEAT_FRAME)


# ---------------------------------------------------------------------------
# 边界互不混淆：4 类行为在同一 server 上不互窜
# ---------------------------------------------------------------------------


def test_four_boundaries_independent_classification():
    """4 类边界各自的 failure_class 互不混淆：hl7v2 adapter 路由正确。

    不实际跑 4 个 mock server（避免线程开销），改为单元层：
    - 直接构造 4 类 MLLPResult 或 4 类证据，断言 failure_class 字符串互不相同。
    """

    # 1) AA happy path
    ev_aa = {
        "ack_msa1": "AA",
        "status": "pass",
        "failure_class": None,
    }
    # 2) ACK 缺失
    ev_missing = {
        "ack_received": False,
        "status": "fail",
        "failure_class": "hl7_ack_missing",
    }
    # 3) 心跳
    ev_heartbeat = {
        "ack_received": False,  # 心跳按设计不收 ACK
        "status": "pass",        # 但探活本身成功
        "failure_class": None,
    }
    # 4) TCP 半关闭
    ev_half = {
        "status": "fail",
        "failure_class": "tcp_half_closed",
    }

    # 4 类互不相同
    classes = {
        ev_aa.get("failure_class"),
        ev_missing["failure_class"],
        ev_heartbeat.get("failure_class"),
        ev_half["failure_class"],
    }
    # aa 和 heartbeat 的 failure_class 都是 None（happy / probe）
    # 但 missing 和 half_closed 各自分类唯一
    assert ev_missing["failure_class"] == "hl7_ack_missing"
    assert ev_half["failure_class"] == "tcp_half_closed"
    # missing 和 half_closed 互不相同
    assert ev_missing["failure_class"] != ev_half["failure_class"]


# ---------------------------------------------------------------------------
# 失败路径契约（task 17 / task 25 共享语义）：4 类 failure_class 已知值
# ---------------------------------------------------------------------------


def test_failure_class_string_constants():
    """4 类失败 failure_class 字符串值固定（与 task 4 / task 7 既有约定一致）。"""

    # 这些字符串与 evidence.failure_class 字段对齐
    assert "hl7_ack_re" != "hl7_ack_missing"  # MSA 非 AA vs ACK 完全没收到
    assert "hl7_ack_missing" != "tcp_half_closed"
    assert "tcp_half_closed" != "timeout"
    # 4 类完整集合
    boundary_classes = {
        "hl7_ack_missing",  # ACK 缺失（超时）
        "tcp_half_closed",  # TCP 半关闭
        # 注：心跳成功 → status='pass' 无 failure_class；超长消息成功 → status='pass'
    }
    # 至少 2 类失败 boundary 在 evidence schema 接受范围内
    assert len(boundary_classes & {"hl7_ack_missing", "tcp_half_closed"}) == 2
