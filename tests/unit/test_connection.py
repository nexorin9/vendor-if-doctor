"""test_connection — connection 子模块单元测试（task 12）。

覆盖：
- BaseConnection 抽象性（不可直接实例化）；
- Evidence 序列化 + is_failure；
- network.preflight TCP 探活（成功 + 失败 + latency 阈值）；
- step.STEP_NAMES 完整性；
- factory.build_connection 路由 + register_protocol 生命周期；
- error_pack FailureClass 枚举完整性；
- matchers resolve_jsonpath + assert_*。
"""

from __future__ import annotations

import pytest

from vendor_if_doctor.connection import (
    BaseConnection,
    Evidence,
    FailureClass,
    PreflightError,
    PreflightOK,
    STEP_NAMES,
    build_connection,
    classify_exception,
    list_protocols,
    preflight,
    register_protocol,
    unregister_protocol,
)
from vendor_if_doctor.connection.step import Step, StepResult, make_step
from vendor_if_doctor.spec.schema import (
    AuthNone,
    HttpSampleEndpoint,
    SampleEndpoint,
    VendorInterface,
)


# ---------------------------------------------------------------------------
# BaseConnection 抽象性
# ---------------------------------------------------------------------------


def test_base_connection_cannot_be_instantiated() -> None:
    """抽象基类不能直接实例化。"""

    spec = VendorInterface(
        vendor_id="x",
        interface_id="y",
        display_name="x",
        protocol="http",
        host="127.0.0.1",
        port=80,
        base_path="/",
        timeout_seconds=5,
        auth=AuthNone(),
        sample_endpoint=SampleEndpoint(http=HttpSampleEndpoint(
            method="GET", path="/", headers={}, body=""
        )),
        required_fields=[],
    )
    with pytest.raises(TypeError):
        BaseConnection(spec)  # type: ignore[abstract]


def test_step_names_includes_full_pipeline() -> None:
    """STEP_NAMES 至少含 preflight/auth/list/sample/assert 五项。"""

    assert "preflight" in STEP_NAMES
    assert "auth" in STEP_NAMES
    assert "list" in STEP_NAMES
    assert "sample" in STEP_NAMES
    assert "assert" in STEP_NAMES


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


def test_evidence_is_failure() -> None:
    ev_pass = Evidence(summary="ok", command="GET /", latency_ms=10, status="pass")
    ev_fail = Evidence(summary="bad", command="GET /", latency_ms=10, status="fail")
    ev_skip = Evidence(summary="skipped", command="GET /", latency_ms=0, status="skip")
    ev_deg = Evidence(summary="slow", command="GET /", latency_ms=5000, status="degraded")
    assert not ev_pass.is_failure
    assert ev_fail.is_failure
    assert not ev_skip.is_failure
    assert ev_deg.is_failure


def test_evidence_to_dict_contains_all_fields() -> None:
    ev = Evidence(summary="ok", command="GET /", latency_ms=10, status="pass")
    d = ev.to_dict()
    # 所有字段必须出现
    for key in ("summary", "command", "latency_ms", "status", "failure_class",
                "response_body", "evidence_hash", "meta", "step_name",
                "vendor_id", "interface_id"):
        assert key in d
    assert d["status"] == "pass"


def test_evidence_default_meta_empty_dict() -> None:
    ev = Evidence(summary="x", command="x", latency_ms=0, status="pass")
    assert ev.meta == {}


# ---------------------------------------------------------------------------
# Step / StepResult / make_step
# ---------------------------------------------------------------------------


def test_make_step_returns_step_protocol_object() -> None:
    def run() -> StepResult:
        return StepResult(success=True, evidence=Evidence(
            summary="ok", command="x", latency_ms=0, status="pass"
        ))

    s = make_step(name="custom", run_fn=run)
    assert isinstance(s, Step)
    assert s.name == "custom"
    result = s.run()
    assert isinstance(result, StepResult)
    assert result.success is True


def test_make_step_failure_result() -> None:
    def run() -> StepResult:
        return StepResult(success=False, evidence=Evidence(
            summary="bad", command="x", latency_ms=0, status="fail"
        ))

    s = make_step(name="x", run_fn=run)
    result = s.run()
    assert result.success is False


# ---------------------------------------------------------------------------
# network.preflight
# ---------------------------------------------------------------------------


def test_preflight_returns_ok_on_local_server() -> None:
    """本地 127.0.0.1 任意空闲端口 → preflight OK。"""

    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.listen(1)
        try:
            result = preflight("127.0.0.1", port, timeout=1.0)
            assert isinstance(result, PreflightOK)
            assert result.host == "127.0.0.1"
            assert result.port == port
            assert result.latency_ms < 1500
        finally:
            srv.close()


def test_preflight_raises_error_on_unreachable_port() -> None:
    """192.0.2.1（RFC 5737 TEST-NET-1）不可达 → preflight 抛 PreflightError。"""

    with pytest.raises(PreflightError) as exc_info:
        preflight("192.0.2.1", 80, timeout=0.5)
    assert exc_info.value.host == "192.0.2.1"
    assert exc_info.value.latency_ms < 1500


# ---------------------------------------------------------------------------
# classify_exception
# ---------------------------------------------------------------------------


def test_failure_class_enum_has_six_values() -> None:
    assert FailureClass.HTTP_STATUS.value == "http_status"
    assert FailureClass.AUTH_FAILED.value == "auth_failed"
    assert FailureClass.TIMEOUT.value == "timeout"
    assert FailureClass.SCHEMA_MISMATCH.value == "schema_mismatch"
    assert FailureClass.PAYLOAD_EMPTY.value == "payload_empty"
    assert FailureClass.HL7_ACK_RE.value == "hl7_ack_re"


def test_classify_exception_returns_string() -> None:
    fc = classify_exception(ValueError("test"))
    assert isinstance(fc, str)
    assert fc in {f.value for f in FailureClass}


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------


def test_list_protocols_contains_known() -> None:
    protocols = list_protocols()
    assert "http" in protocols
    assert "hl7v2" in protocols


def test_register_and_unregister_protocol() -> None:
    class FakeConn(BaseConnection):
        def _get_client(self):
            return None

        def steps(self):
            return []

    register_protocol("fake-test", FakeConn)
    try:
        assert "fake-test" in list_protocols()
    finally:
        unregister_protocol("fake-test")
    assert "fake-test" not in list_protocols()


# ---------------------------------------------------------------------------
# matchers / JSONPath
# ---------------------------------------------------------------------------


def test_resolve_jsonpath_simple_field() -> None:
    from vendor_if_doctor.connection.matchers import resolve_jsonpath

    doc = {"code": 200, "patient_id": "P-001"}
    found, value = resolve_jsonpath(doc, "$.code")
    assert found is True
    assert value == 200


def test_resolve_jsonpath_array_index() -> None:
    from vendor_if_doctor.connection.matchers import resolve_jsonpath

    doc = {"data": {"items": [{"id": "a1"}, {"id": "a2"}]}}
    found, value = resolve_jsonpath(doc, "$.data.items[1].id")
    assert found is True
    assert value == "a2"


def test_resolve_jsonpath_recursive_descent() -> None:
    from vendor_if_doctor.connection.matchers import resolve_jsonpath

    doc = {"a": {"id": 1}, "b": {"id": 2}}
    found, value = resolve_jsonpath(doc, "$..id")
    assert found is True
    # 递归下降找到第一个匹配的 id
    assert value in (1, 2)


def test_resolve_jsonpath_missing_field() -> None:
    from vendor_if_doctor.connection.matchers import resolve_jsonpath

    doc = {"code": 200}
    found, value = resolve_jsonpath(doc, "$.missing")
    assert found is False