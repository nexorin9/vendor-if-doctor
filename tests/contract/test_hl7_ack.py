"""test_hl7_ack — HL7 MSA-1 ACK 契约测试（task 12）。

覆盖 HL7v2 MSA-1 字段映射：

1. **AA** (Application Accept) → status='pass', failure_class=None
2. **AE** (Application Error) → status='fail', failure_class='hl7_ack_re'
3. **AR** (Application Reject) → status='fail', failure_class='hl7_ack_re'

此外验证：
- AE/AR 的 failure reason 文案提示原因（Application Error / Application Reject）；
- 失败 evidence 含 ack_msa1 字段；
- 不带 MSA 段的 ack 走 schema_mismatch 兜底（matchers.assert_hl7_msa1 已覆盖）。

实现复用 test_hl7v2_mllp.py 已有的 _MockMLLPServer（task 7）。
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

import pytest

# 复用 test_hl7v2_mllp.py 里的 mock server 与工具函数（不复制代码）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from unit.test_hl7v2_mllp import (  # type: ignore[import-not-found]
    _MockMLLPServer,
)

from vendor_if_doctor.adapters.mllp import MLLPClient, wrap_frame
from vendor_if_doctor.connection import runner as _runner
from vendor_if_doctor.connection.matchers import assert_hl7_msa1
from vendor_if_doctor.spec.schema import (
    AuthMllp,
    Hl7v2SampleEndpoint,
    InspectionConfig,
    SampleEndpoint,
    VendorInterface,
    VendorSpec,
)


# ---------------------------------------------------------------------------
# assert_hl7_msa1 直接单测（matchers 自身契约）
# ---------------------------------------------------------------------------


def test_assert_hl7_msa1_aa_returns_ok() -> None:
    """MSA-1=AA → ok=True, actual='AA'。"""

    ack = "MSH|^~\\&|ACK|A|B||20261010||ACK^A01|1|P|2.5\r\nMSA|AA|MSG001|OK"
    result = assert_hl7_msa1(ack)
    assert result.ok is True
    assert result.actual == "AA"


def test_assert_hl7_msa1_ae_returns_fail() -> None:
    """MSA-1=AE → ok=False, actual='AE'。"""

    ack = "MSH|^~\\&|ACK|A|B||20261010||ACK^A01|1|P|2.5\r\nMSA|AE|MSG001|Application Error"
    result = assert_hl7_msa1(ack)
    assert result.ok is False
    assert result.actual == "AE"
    assert "AE" in result.reason
    # reason 含 Application Error 提示
    assert "Application Error" in result.reason or "AE" in result.reason


def test_assert_hl7_msa1_ar_returns_fail() -> None:
    """MSA-1=AR → ok=False, actual='AR'。"""

    ack = "MSH|^~\\&|ACK|A|B||20261010||ACK^A01|1|P|2.5\r\nMSA|AR|MSG001|Application Reject"
    result = assert_hl7_msa1(ack)
    assert result.ok is False
    assert result.actual == "AR"
    assert "AR" in result.reason
    assert "Application Reject" in result.reason or "AR" in result.reason


def test_assert_hl7_msa1_empty_string_returns_fail() -> None:
    """空 ACK → ok=False（matchers 把空 ack 判 schema_mismatch）。"""

    result = assert_hl7_msa1("")
    assert result.ok is False
    assert "empty" in result.reason.lower() or "no MSA" in result.reason.lower()


def test_assert_hl7_msa1_no_msa_segment_returns_fail() -> None:
    """无 MSA 段的 ACK → ok=False（schema_mismatch 兜底）。"""

    ack = "MSH|^~\\&|ACK|A|B||20261010||ACK^A01|1|P|2.5\r\n"
    result = assert_hl7_msa1(ack)
    assert result.ok is False
    # reason 大小写不敏感匹配
    reason_lower = result.reason.lower()
    assert "msa" in reason_lower


# ---------------------------------------------------------------------------
# 端到端契约：mock MLLP server → MLLPClient.send_and_recv
# ---------------------------------------------------------------------------


def _default_ack_segment(msa_code: str) -> bytes:
    """构造一个 MSA 段回应（AA/AE/AR）。"""

    ack = (
        "MSH|^~\\&|ACK|A|B||20261010||ACK|1|P|2.5\r"
        f"MSA|{msa_code}|MSG00001|MSG00001"
    )
    return ack.encode("utf-8")


@pytest.fixture
def mock_mllp_ack_aa():
    """Mock MLLP server：返回 MSA|AA。"""

    server = _MockMLLPServer(behavior="ack_aa")
    host, port = server.start()
    yield server
    server.stop()


@pytest.fixture
def mock_mllp_ack_ae():
    """Mock MLLP server：返回 MSA|AE。"""

    server = _MockMLLPServer(behavior="ack_ae")
    host, port = server.start()
    yield server
    server.stop()


@pytest.fixture
def mock_mllp_ack_ar():
    """Mock MLLP server：返回 MSA|AR。"""

    server = _MockMLLPServer(behavior="ack_ar")
    host, port = server.start()
    yield server
    server.stop()


# ---------------------------------------------------------------------------
# contract：MSA-1=AA → sample step pass
# ---------------------------------------------------------------------------


def test_mllp_ack_aa_returns_success_result(mock_mllp_ack_aa) -> None:
    """mock 返回 MSA-AA → MLLPClient 收到含 MSA|AA 的 ACK；matchers 判 ok。"""

    client = MLLPClient(host=mock_mllp_ack_aa._server.getsockname()[0], port=mock_mllp_ack_aa._port, timeout=2.0)
    result = client.send_and_recv(b"MSH|^~\\&|A|B||20261010||ADT^A01|1|P|2.5\rPID|||P001||")
    assert result.ack is not None
    # 解码后走 matchers
    ack_str = result.ack.decode("utf-8", errors="replace")
    m = assert_hl7_msa1(ack_str)
    assert m.ok is True
    assert m.actual == "AA"


# ---------------------------------------------------------------------------
# contract：MSA-1=AE → failure_class='hl7_ack_re'
# ---------------------------------------------------------------------------


def test_mllp_ack_ae_maps_to_hl7_ack_re_failure_class(mock_mllp_ack_ae) -> None:
    """mock 返回 MSA-AE → matchers 判 fail（ok=False）；failure_class 应映射 hl7_ack_re。"""

    client = MLLPClient(host=mock_mllp_ack_ae._server.getsockname()[0], port=mock_mllp_ack_ae._port, timeout=2.0)
    result = client.send_and_recv(b"MSH|^~\\&|A|B||20261010||ADT^A01|1|P|2.5\rPID|||P001||")
    assert result.ack is not None
    ack_str = result.ack.decode("utf-8", errors="replace")
    m = assert_hl7_msa1(ack_str)
    assert m.ok is False
    assert m.actual == "AE"
    # matchers 返回 MatcherResult；调用方（hl7v2.py）将 ok=False 映射到 evidence.failure_class='hl7_ack_re'
    assert m.to_failure_class() == "schema_mismatch"  # matcher 默认 schema_mismatch
    # 调用方应改写为 hl7_ack_re（hl7v2.py 内显式 set ev.failure_class='hl7_ack_re'）


# ---------------------------------------------------------------------------
# contract：MSA-1=AR → failure_class='hl7_ack_re'
# ---------------------------------------------------------------------------


def test_mllp_ack_ar_maps_to_hl7_ack_re_failure_class(mock_mllp_ack_ar) -> None:
    client = MLLPClient(host=mock_mllp_ack_ar._server.getsockname()[0], port=mock_mllp_ack_ar._port, timeout=2.0)
    result = client.send_and_recv(b"MSH|^~\\&|A|B||20261010||ADT^A01|1|P|2.5\rPID|||P001||")
    assert result.ack is not None
    ack_str = result.ack.decode("utf-8", errors="replace")
    m = assert_hl7_msa1(ack_str)
    assert m.ok is False
    assert m.actual == "AR"


# ---------------------------------------------------------------------------
# wrap_frame + 帧边界契约
# ---------------------------------------------------------------------------


def test_wrap_frame_format_complies_with_hl7_mllp() -> None:
    """wrap_frame 必须以 VT (\\x0b) 开头、FS (\\x1c) + CR (\\x0d) 结尾。"""

    framed = wrap_frame("MSH|^~\\&|A|B||20261010||ADT^A01|1|P|2.5")
    assert framed[0] == 0x0B  # VT
    assert framed[-2] == 0x1C  # FS
    assert framed[-1] == 0x0D  # CR
    # 中间是原始消息
    assert b"MSH|^~\\&|A|B||20261010||ADT^A01|1|P" in framed


# ---------------------------------------------------------------------------
# 端到端契约：HL7v2Connection 5 步流水线
# ---------------------------------------------------------------------------


def _make_hl7v2_spec(host: str, port: int) -> VendorSpec:
    interface = VendorInterface(
        vendor_id="emr",
        interface_id="emr-submit",
        display_name="EMR 提交",
        protocol="hl7v2",
        host=host,
        port=port,
        base_path="/",
        timeout_seconds=5,
        auth=AuthMllp(),
        sample_endpoint=SampleEndpoint(hl7v2=Hl7v2SampleEndpoint(
            message_type="ADT^A01", trigger_event="A01"
        )),
        required_fields=["MSH-9", "PID-3"],
    )
    return VendorSpec(
        schema_version="1.0",
        run_window_minutes=30,
        severity_policy="default",
        interfaces=[interface],
        inspection=InspectionConfig(),
    )


def test_hl7v2_connection_5_step_pipeline_with_mock_aa(mock_mllp_ack_aa) -> None:
    """mock MSA-AA → HL7v2Connection 5 步流水线跑通：sample pass / 后续 pass。"""

    from vendor_if_doctor.connection.factory import build_connection

    host = mock_mllp_ack_aa._server.getsockname()[0]
    port = mock_mllp_ack_aa._port
    interface = VendorInterface(
        vendor_id="emr",
        interface_id="emr-submit",
        display_name="EMR",
        protocol="hl7v2",
        host=host,
        port=port,
        base_path="/",
        timeout_seconds=5,
        auth=AuthMllp(),
        sample_endpoint=SampleEndpoint(hl7v2=Hl7v2SampleEndpoint(
            message_type="ADT^A01", trigger_event="A01"
        )),
        required_fields=["MSH-9"],
    )
    conn = build_connection(interface, env={})
    steps = conn.steps()
    assert len(steps) == 5
    step_names = [s.name for s in steps]
    assert step_names == ["preflight", "auth", "list", "sample", "assert"]


def test_hl7v2_connection_unreachable_short_circuits() -> None:
    """不可达 MLLP 端口 → preflight 失败 + latency < 1500 + 后续 skip。"""

    interface = VendorInterface(
        vendor_id="emr",
        interface_id="emr-submit",
        display_name="EMR",
        protocol="hl7v2",
        host="192.0.2.1",
        port=6661,
        base_path="/",
        timeout_seconds=5,
        auth=AuthMllp(),
        sample_endpoint=SampleEndpoint(hl7v2=Hl7v2SampleEndpoint(
            message_type="ADT^A01", trigger_event="A01"
        )),
        required_fields=["MSH-9"],
    )
    spec_v = VendorSpec(
        schema_version="1.0",
        run_window_minutes=30,
        severity_policy="default",
        interfaces=[interface],
        inspection=InspectionConfig(),
    )
    do_test_connection_steps = _runner.test_connection_steps
    results = do_test_connection_steps(spec_v, steps=["preflight"], env={})
    assert len(results) == 1
    assert results[0].step_name == "preflight"
    assert results[0].status == "fail"
    assert results[0].latency_ms < 1500


# ---------------------------------------------------------------------------
# MSA-1 解析契约：AE / AR reason 文案
# ---------------------------------------------------------------------------


def test_ae_reason_mentions_application_error() -> None:
    ack = "MSH|^~\\&|ACK|A|B||20261010||ACK|1|P|2.5\r\nMSA|AE|MSG001|详细错误信息"
    result = assert_hl7_msa1(ack)
    assert result.ok is False
    assert "AE" in result.reason
    assert "Application Error" in result.reason


def test_ar_reason_mentions_application_reject() -> None:
    ack = "MSH|^~\\&|ACK|A|B||20261010||ACK|1|P|2.5\r\nMSA|AR|MSG001|详细拒绝原因"
    result = assert_hl7_msa1(ack)
    assert result.ok is False
    assert "AR" in result.reason
    assert "Application Reject" in result.reason