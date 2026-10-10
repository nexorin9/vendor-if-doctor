"""HL7v2 + MLLP 适配器测试（task 7）。"""

from __future__ import annotations

import socket
import threading
import time
from typing import Optional, Tuple

import pytest

from vendor_if_doctor.adapters.hl7v2 import HL7v2Connection, _build_adt_a01
from vendor_if_doctor.adapters.mllp import (
    MLLPConnectError,
    MLLPFrameError,
    MLLPHalfClosedError,
    MLLP_MAX_FRAME_BYTES,
    MLLPClient,
    _iter_frames,
    wrap_frame,
)
from vendor_if_doctor.connection.factory import (
    build_connection,
    list_protocols,
    unregister_protocol,
)
from vendor_if_doctor.connection.runner import test_connection as _test_connection
from vendor_if_doctor.spec.schema import (
    AuthMllp,
    Hl7v2SampleEndpoint,
    SampleEndpoint,
    Protocol,
    VendorInterface,
    VendorSpec,
)


# ---------------------------------------------------------------------------
# MLLP 帧封装测试
# ---------------------------------------------------------------------------


def test_wrap_frame_basic():
    """基础 MLLP 帧封装：VT + msg + FS + CR。"""

    framed = wrap_frame("MSH|^~\\&|A|B|...")
    assert framed == b"\x0bMSH|^~\\&|A|B|...\x1c\x0d"


def test_iter_frames_single():
    """单帧切分。"""

    buffer = b"\x0bMSG1\x1c\x0d"
    frames, rest = _iter_frames(buffer)
    assert frames == [b"MSG1"]
    assert rest == b""


def test_iter_frames_multiple():
    """多帧连续切分。"""

    buffer = b"\x0bMSG1\x1c\x0d\x0bMSG2\x1c\x0d"
    frames, rest = _iter_frames(buffer)
    assert frames == [b"MSG1", b"MSG2"]
    assert rest == b""


def test_iter_frames_partial():
    """半截帧保留为 rest。"""

    buffer = b"\x0bMSG1\x1c\x0d\x0bPARTIAL"
    frames, rest = _iter_frames(buffer)
    assert frames == [b"MSG1"]
    assert rest == b"\x0bPARTIAL"


def test_iter_frames_no_vt():
    """无 VT 时保留末尾字节。"""

    buffer = b"junk\x0b"
    frames, rest = _iter_frames(buffer)
    assert frames == []
    assert rest == b"\x0b"


def test_iter_frames_invalid_fs_no_cr():
    """FS 后无 CR 抛 MLLPFrameError。"""

    buffer = b"\x0bMSG\x1cJUNK"
    with pytest.raises(MLLPFrameError):
        _iter_frames(buffer)


# ---------------------------------------------------------------------------
# Mock MLLP server
# ---------------------------------------------------------------------------


class _MockMLLPServer:
    """MLLP mock server：按 ``behavior`` 切换响应。"""

    def __init__(
        self,
        behavior: str = "ack_aa",
        ack_message: Optional[str] = None,
        half_close_after_recv: bool = False,
    ) -> None:
        self.behavior = behavior
        self.ack_message = ack_message
        self.half_close_after_recv = half_close_after_recv
        self.received_messages: list[bytes] = []
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
            conn.settimeout(2.0)
            buffer = b""
            # 读取一帧
            while True:
                try:
                    chunk = conn.recv(4096)
                except (socket.timeout, OSError):
                    return
                if not chunk:
                    return
                buffer += chunk
                frames, buffer = _iter_frames(buffer)
                if frames:
                    self.received_messages.append(frames[0])
                    break

            # 按 behavior 响应
            if self.half_close_after_recv:
                # 不返回 ACK，直接关闭
                conn.close()
                return

            if self.behavior == "ack_aa":
                ack = self.ack_message or _default_ack_aa()
            elif self.behavior == "ack_ae":
                ack = self.ack_message or _default_ack("AE", "Application Error")
            elif self.behavior == "ack_ar":
                ack = self.ack_message or _default_ack("AR", "Application Reject")
            elif self.behavior == "empty_ack":
                ack = b""
            elif self.behavior == "no_ack":
                # 不返回任何字节，等客户端超时
                time.sleep(10)
                conn.close()
                return
            elif self.behavior == "bad_frame":
                # 返回不合法 MLLP 帧
                conn.sendall(b"\x0bJUNK\x1cNO_CR")
                conn.close()
                return
            else:
                ack = self.ack_message or _default_ack_aa()

            conn.sendall(wrap_frame(ack))
            conn.close()
        except OSError:
            pass


def _default_ack_aa() -> bytes:
    return _default_ack("AA", "").encode("utf-8") if isinstance(_default_ack("AA", ""), str) else _default_ack("AA", "")


def _default_ack(msa1: str, err_text: str) -> bytes:
    text = f"MSH|^~\\&|RECEIVER|RECEIVER|SENDER|SENDER|20261010||ACK^A01|ACK001|P|2.5\rMSA|{msa1}|MSGCTL01|{err_text}\r"
    return text.encode("utf-8")


# ---------------------------------------------------------------------------
# MLLPClient 测试
# ---------------------------------------------------------------------------


def test_mllp_client_ack_aa():
    """happy path：mock 返回 AA，client 应拿到 ACK。"""

    server = _MockMLLPServer(behavior="ack_aa")
    host, port = server.start()
    try:
        client = MLLPClient(host, port, timeout=2.0)
        msg = "MSH|^~\\&|A|B|...\rPID|||X\r"
        result = client.send_and_recv(msg)
        assert result.ack_received is True
        assert result.ack is not None
        assert b"MSA|AA" in result.ack
    finally:
        server.stop()


def test_mllp_client_ack_timeout():
    """mock 不返回 ACK 时 client 应在 timeout 内返回 ack_received=False。"""

    server = _MockMLLPServer(behavior="no_ack")
    host, port = server.start()
    try:
        client = MLLPClient(host, port, timeout=1.0)
        result = client.send_and_recv("MSH|^~\\&|A|B|...\r")
        assert result.ack_received is False
        assert result.ack is None
        # 超时时间应 < 1500ms（满足 spec 要求）
        assert result.latency_ms < 1500
    finally:
        server.stop()


def test_mllp_client_half_closed():
    """mock 中途关闭连接应抛 MLLPHalfClosedError。"""

    server = _MockMLLPServer(half_close_after_recv=True)
    host, port = server.start()
    try:
        client = MLLPClient(host, port, timeout=2.0)
        with pytest.raises(MLLPHalfClosedError):
            client.send_and_recv("MSH|^~\\&|A|B|...\r")
    finally:
        server.stop()


def test_mllp_client_connect_error():
    """不可达端口应抛 MLLPConnectError（或 OSError 子类）。"""

    # 找一个不会有人监听的端口（高位随机）
    client = MLLPClient("127.0.0.1", 1, timeout=0.5)
    with pytest.raises((MLLPConnectError, OSError)):
        client.send_and_recv("MSH|^~\\&|A|B|...\r")


def test_mllp_client_large_message_chunked():
    """超长消息（> 64KB）走分片发送，mock 接收完整。"""

    server = _MockMLLPServer(behavior="ack_aa")
    host, port = server.start()
    try:
        client = MLLPClient(host, port, timeout=2.0)
        # 构造 80KB 消息（每个分片最大 64KB）
        big = "X" * (80 * 1024)
        msg = f"MSH|^~\\&|A|B|...\rPID|||{big}\r"
        result = client.send_and_recv(msg)
        assert result.ack_received is True
        # mock 收到至少两条分片（80KB > 64KB）
        received_total = sum(len(m) for m in server.received_messages)
        assert received_total >= MLLP_MAX_FRAME_BYTES
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# HL7v2Connection 测试
# ---------------------------------------------------------------------------


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
        required_fields=kwargs.get("required_fields", ["MSH-9", "PID-3"]),
    )
    return VendorSpec(
        schema_version="1.0",
        run_window_minutes=30,
        severity_policy="default",
        interfaces=[interface],
    )


def test_hl7v2_factory_routes_to_hl7v2_connection():
    """build_connection 应把 hl7v2 协议路由到 HL7v2Connection。"""

    spec = _make_hl7_spec("127.0.0.1", 6661)
    conn = build_connection(spec.interfaces[0])
    assert isinstance(conn, HL7v2Connection)
    assert "hl7v2" in list_protocols()


def test_hl7v2_steps_5_pipeline():
    """steps 应返回 5 步流水线。"""

    spec = _make_hl7_spec("127.0.0.1", 6661)
    conn = build_connection(spec.interfaces[0])
    step_names = [s.name for s in conn.steps()]
    assert step_names == ["preflight", "auth", "list", "sample", "assert"]


def test_hl7v2_ack_aa_pass():
    """mock 返回 MSA-1=AA 时整条流水线应 pass（ACK 自身不含 PID 段，所以只校验 MSH-9）。"""

    server = _MockMLLPServer(behavior="ack_aa")
    host, port = server.start()
    try:
        # ACK 不含 PID 段，required_fields 只用 MSH-9 验证 ACK 自身
        spec = _make_hl7_spec(host, port, required_fields=["MSH-9"])
        evidences = _test_connection(spec)
        # 5 步全过
        assert len(evidences) == 5
        # preflight pass
        assert evidences[0].status == "pass"
        # auth skip
        assert evidences[1].status == "skip"
        # list skip
        assert evidences[2].status == "skip"
        # sample pass
        assert evidences[3].status == "pass"
        # assert pass
        assert evidences[4].status == "pass"
        assert evidences[4].failure_class is None
        assert evidences[3].meta.get("ack_msa1") == "AA"
    finally:
        server.stop()


def test_hl7v2_ack_ae_hl7_ack_re():
    """mock 返回 MSA-1=AE 时 assert 应为 fail, failure_class='hl7_ack_re'。"""

    server = _MockMLLPServer(behavior="ack_ae")
    host, port = server.start()
    try:
        spec = _make_hl7_spec(host, port)
        evidences = _test_connection(spec)
        assert len(evidences) == 5
        # sample 通过（收到 ACK）
        assert evidences[3].status == "pass"
        assert evidences[3].meta.get("ack_msa1") == "AE"
        # assert 失败
        assert evidences[4].status == "fail"
        assert evidences[4].failure_class == "hl7_ack_re"
    finally:
        server.stop()


def test_hl7v2_ack_ar_hl7_ack_re():
    """mock 返回 MSA-1=AR 时同样映射到 hl7_ack_re。"""

    server = _MockMLLPServer(behavior="ack_ar")
    host, port = server.start()
    try:
        spec = _make_hl7_spec(host, port)
        evidences = _test_connection(spec)
        assert evidences[4].status == "fail"
        assert evidences[4].failure_class == "hl7_ack_re"
        assert evidences[4].meta.get("ack_msa1") == "AR"
    finally:
        server.stop()


def test_hl7v2_unreachable_preflight_timeout():
    """端口不可达时 preflight 应在 1500ms 内失败，failure_class='timeout'。"""

    spec = _make_hl7_spec("127.0.0.1", 1, timeout_seconds=2.0)
    evidences = _test_connection(spec)
    assert len(evidences) == 5
    # preflight 失败
    assert evidences[0].status == "fail"
    assert evidences[0].failure_class in ("timeout", "tcp_unreachable")
    assert evidences[0].latency_ms < 1500
    # 后续 step 短路为 skip
    for ev in evidences[1:]:
        assert ev.status == "skip"
        assert ev.meta.get("skipped_due_to") == "preflight"


def test_hl7v2_ack_missing_timeout():
    """mock 不返回 ACK 时 sample 应 hl7_ack_missing（latency < 1500ms 由 timeout 控制）。"""

    server = _MockMLLPServer(behavior="no_ack")
    host, port = server.start()
    try:
        spec = _make_hl7_spec(host, port, timeout_seconds=1.0)
        evidences = _test_connection(spec)
        assert len(evidences) == 5
        # preflight pass
        assert evidences[0].status == "pass"
        # sample fail, hl7_ack_missing
        assert evidences[3].status == "fail"
        assert evidences[3].failure_class == "hl7_ack_missing"
        assert evidences[3].meta.get("ack_received") is False
    finally:
        server.stop()


def test_hl7v2_half_closed_tcp():
    """mock 中途 ECONNRESET → sample fc='tcp_half_closed'。"""

    server = _MockMLLPServer(half_close_after_recv=True)
    host, port = server.start()
    try:
        spec = _make_hl7_spec(host, port)
        evidences = _test_connection(spec)
        assert evidences[3].status == "fail"
        assert evidences[3].failure_class == "tcp_half_closed"
    finally:
        server.stop()


def test_hl7v2_required_fields_check():
    """required_fields 缺失时 assert 应失败 fc='schema_mismatch'。"""

    # 构造一个 ACK MSA-1=AA 但缺 PID-3
    server = _MockMLLPServer(
        behavior="ack_aa",
        ack_message="MSH|^~\\&|RECV|RECV|SEND|SEND|20261010||ACK^A01|ACK001|P|2.5\rMSA|AA|MSGCTL\r",
    )
    host, port = server.start()
    try:
        spec = _make_hl7_spec(host, port, required_fields=["MSH-9", "PID-3"])
        evidences = _test_connection(spec)
        # sample pass
        assert evidences[3].status == "pass"
        # assert fail due to missing PID-3
        assert evidences[4].status == "fail"
        assert evidences[4].failure_class == "schema_mismatch"
        assert "PID-3" in evidences[4].meta.get("failed_required_fields", [])
    finally:
        server.stop()


def test_hl7v2_message_construction_uses_hl7apy():
    """构造的 HL7 消息能被 hl7apy parse_message 解析。"""

    from hl7apy.parser import parse_message

    interface = VendorInterface(
        vendor_id="emr",
        interface_id="submit",
        display_name="HL7 EMR Submit",
        protocol=Protocol.HL7V2,
        host="127.0.0.1",
        port=6661,
        timeout_seconds=2.0,
        auth=AuthMllp(),
        sample_endpoint=SampleEndpoint(
            hl7v2=Hl7v2SampleEndpoint(
                message_type="ADT^A01",
                trigger_event="A01",
                sample_patient_id="<EXAMPLE_PATIENT_ID>",
            )
        ),
        required_fields=[],
    )
    hl7 = _build_adt_a01(interface, interface.sample_endpoint.hl7v2)
    # hl7apy 解析验证
    parsed = parse_message(hl7)
    assert parsed.msh.msh_9.to_er7() == "ADT^A01"
    assert parsed.pid.pid_3.to_er7() == "<EXAMPLE_PATIENT_ID>"
    assert parsed.pv1.pv1_7.to_er7() == "<EXAMPLE_DOCTOR_ID^Vendor^Doctor"


def test_hl7v2_evidence_no_token_leak():
    """Evidence.response_body 不应包含任何 token 字面量（这里本来就没有 token，但验证。）。"""

    server = _MockMLLPServer(behavior="ack_aa")
    host, port = server.start()
    try:
        spec = _make_hl7_spec(host, port)
        evidences = _test_connection(spec)
        for ev in evidences:
            if ev.response_body:
                # 确认不含常见 token 字面量
                assert "Bearer " not in ev.response_body
                assert "Basic " not in ev.response_body
    finally:
        server.stop()


def test_hl7v2_skips_when_no_hl7v2_endpoint():
    """sample_endpoint.hl7v2 字段缺失时（防御性构造）sample step 仍能跑不崩。"""

    # 构造一个空 hl7v2 sample_endpoint（绕过 pydantic 验证，运行期防御）
    interface = VendorInterface(
        vendor_id="emr",
        interface_id="bad",
        display_name="Bad",
        protocol=Protocol.HL7V2,
        host="127.0.0.1",
        port=6661,
        timeout_seconds=1.0,
        auth=AuthMllp(),
        sample_endpoint=SampleEndpoint(hl7v2=Hl7v2SampleEndpoint()),
        required_fields=[],
    )
    # HL7v2Connection 应能构造（不崩溃）
    conn = HL7v2Connection(interface)
    # steps 列表应包含 5 步
    steps = conn.steps()
    assert len(steps) == 5
    # sample step 是函数式 step，直接调用不会立即崩（会进 sample_step 走超时路径）
    sample_step = conn.get_step("sample")
    assert sample_step is not None
    # 跑出 step（127.0.0.1:6661 不可达 → timeout），不应 crash
    try:
        result = sample_step.run()
        # 应得到 fail evidence（不论是 timeout 还是 schema_mismatch）
        assert result.evidence.status in ("fail", "skip")
        assert result.evidence.failure_class is not None
    except Exception:
        # 不应抛出异常；若有，跑出来由 pytest 报错
        pytest.fail("sample_step.run() 不应抛出未捕获异常")
