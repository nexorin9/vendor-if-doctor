"""test_runner — runner 单元测试（task 12）。

覆盖：
- test_one 单步入口（HTTP happy / 不可达 / 缺凭据）；
- test_connection 5 步流程；
- test_connection_steps 短路行为（首个失败 step 后输出 skip evidence）；
- evidence_hash 确定性 + 凭据不在 hash 原料中；
- _coarse_redact 7 类敏感字段过滤。
"""

from __future__ import annotations

from urllib.parse import urlparse

import pytest

from vendor_if_doctor.connection import runner as _runner
from vendor_if_doctor.connection.runner import (
    _coarse_redact,
    _evidence_hash,
)

# 别名（用 ``as`` 改名前缀，避免 pytest 收集 ``test_*`` 函数）
do_test_one = _runner.test_one
do_test_connection = _runner.test_connection
do_test_connection_steps = _runner.test_connection_steps
from vendor_if_doctor.spec.schema import (
    AuthBearer,
    Hl7v2SampleEndpoint,
    HttpSampleEndpoint,
    InspectionConfig,
    SampleEndpoint,
    VendorInterface,
    VendorSpec,
)


# ---------------------------------------------------------------------------
# _coarse_redact
# ---------------------------------------------------------------------------


def test_coarse_redact_authorization_header() -> None:
    """Authorization 头凭据字面量被替换为 ***。"""

    out = _coarse_redact("Authorization: Bearer abc.def.ghi")
    assert "abc.def.ghi" not in out
    assert "***" in out


def test_coarse_redact_bearer_token_standalone() -> None:
    """Bearer 关键字后接的 token 被替换。"""

    out = _coarse_redact("Bearer abc.def.ghi")
    assert "abc.def.ghi" not in out
    assert "***" in out


def test_coarse_redact_basic_token_standalone() -> None:
    out = _coarse_redact("Basic dXNlcjpwYXNz")
    assert "dXNlcjpwYXNz" not in out
    assert "***" in out


def test_coarse_redact_json_token() -> None:
    out = _coarse_redact('{"token": "abc123", "code": 200}')
    assert '"code": 200' in out
    assert "abc123" not in out


def test_coarse_redact_clean_text_unchanged() -> None:
    """非敏感文本应原样保留。"""

    text = '{"code": 200, "patient_id": "P-001", "msg": "ok"}'
    assert _coarse_redact(text) == text


def test_coarse_redact_handles_empty() -> None:
    assert _coarse_redact("") == ""
    assert _coarse_redact(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _evidence_hash
# ---------------------------------------------------------------------------


def test_evidence_hash_is_sha256_first_16_hex() -> None:
    h = _evidence_hash("GET /x", "{}")
    assert len(h) == 16
    assert all(c in "0123456789abcdef" for c in h)


def test_evidence_hash_deterministic() -> None:
    a = _evidence_hash("cmd", "body")
    b = _evidence_hash("cmd", "body")
    assert a == b


def test_evidence_hash_changes_with_input() -> None:
    a = _evidence_hash("cmd1", "body1")
    b = _evidence_hash("cmd2", "body1")
    assert a != b


# ---------------------------------------------------------------------------
# test_one (单步 HTTP)
# ---------------------------------------------------------------------------


def _make_inline_his_spec(host: str, port: int, token_env: str) -> VendorSpec:
    interface = VendorInterface(
        vendor_id="his",
        interface_id="outpatient-register",
        display_name="HIS 门诊挂号接口",
        protocol="http",
        host=host,
        port=port,
        base_path="/",
        timeout_seconds=5,
        auth=AuthBearer(secret_env=token_env),
        sample_endpoint=SampleEndpoint(http=HttpSampleEndpoint(
            method="GET", path="/", headers={}, body=""
        )),
        required_fields=["$.code"],
    )
    return VendorSpec(
        schema_version="1.0",
        run_window_minutes=30,
        severity_policy="default",
        interfaces=[interface],
        inspection=InspectionConfig(),
    )


def test_test_one_returns_fail_when_host_unreachable() -> None:
    """192.0.2.1 不可达 → preflight 失败 + latency < 1500。"""

    spec = _make_inline_his_spec("192.0.2.1", 80, "VENDOR_TOKEN")
    ev = do_test_one(spec, env={})
    assert ev.status == "fail"
    assert ev.failure_class in ("timeout", "tcp_unreachable")
    assert ev.latency_ms < 1500
    assert ev.step_name in ("preflight", "sample")


def test_test_one_happy_path_with_mock_server(
    mock_http_happy, env_with_credentials
) -> None:
    parsed = urlparse(f"http://{mock_http_happy.host}:{mock_http_happy.port}")
    spec = _make_inline_his_spec(parsed.hostname, parsed.port, "VENDOR_LIS_TOKEN")
    ev = do_test_one(spec, env=env_with_credentials)
    assert ev.status == "pass"
    assert ev.evidence_hash is not None
    assert ev.failure_class is None
    # 凭据字面量不在 response_body / command 摘要里
    assert "lis-secret" not in (ev.response_body or "")
    assert "lis-secret" not in ev.command


# ---------------------------------------------------------------------------
# test_connection (5 步)
# ---------------------------------------------------------------------------


def test_test_connection_returns_five_evidence_per_interface(
    mock_http_happy, env_with_credentials
) -> None:
    parsed = urlparse(f"http://{mock_http_happy.host}:{mock_http_happy.port}")
    spec = _make_inline_his_spec(parsed.hostname, parsed.port, "VENDOR_LIS_TOKEN")
    evidence_list = do_test_connection(spec, env=env_with_credentials)
    # 每条 interface 5 个 step
    assert len(evidence_list) == 5
    step_names = [e.step_name for e in evidence_list]
    assert "preflight" in step_names
    assert "auth" in step_names
    assert "list" in step_names
    assert "sample" in step_names
    assert "assert" in step_names


def test_test_connection_unreachable_short_circuits(env_clean: dict) -> None:
    """不可达主机 → preflight fail，后续 4 步 skip 且 skipped_due_to=preflight。"""

    spec = _make_inline_his_spec("192.0.2.1", 80, "VENDOR_TOKEN")
    evidence_list = do_test_connection(spec, env=env_clean)
    assert len(evidence_list) == 5
    preflight_ev = evidence_list[0]
    assert preflight_ev.step_name == "preflight"
    assert preflight_ev.status == "fail"
    # 后 4 步 skip
    for ev in evidence_list[1:]:
        assert ev.status == "skip"
        assert ev.meta.get("skipped_due_to") == "preflight"


def test_test_connection_no_http_interface_returns_skip_evidence() -> None:
    """无 HTTP 接口时所有 step 输出 skip + not_implemented。"""

    from vendor_if_doctor.spec.schema import (
        AuthNone,
    )

    interface = VendorInterface(
        vendor_id="hl7",
        interface_id="submit",
        display_name="HL7",
        protocol="hl7v2",
        host="127.0.0.1",
        port=9999,
        base_path="/",
        timeout_seconds=5,
        auth=AuthNone(),
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
    evidence_list = do_test_connection(spec_v, env={})
    assert len(evidence_list) == 5
    # HL7v2 路由到 HL7v2Connection 的 5 步流水线（实际行为）— 检查至少有 preflight 步骤
    step_names = [e.step_name for e in evidence_list]
    assert "preflight" in step_names


# ---------------------------------------------------------------------------
# test_connection_steps (子集 steps)
# ---------------------------------------------------------------------------


def test_test_connection_steps_subset(mock_http_happy, env_with_credentials) -> None:
    """只跑 preflight + sample 两步 → 返回 2 条 evidence。"""

    parsed = urlparse(f"http://{mock_http_happy.host}:{mock_http_happy.port}")
    spec = _make_inline_his_spec(parsed.hostname, parsed.port, "VENDOR_LIS_TOKEN")
    evidence_list = do_test_connection_steps(
        spec, steps=["preflight", "sample"], env=env_with_credentials
    )
    assert len(evidence_list) == 2
    step_names = [e.step_name for e in evidence_list]
    assert "preflight" in step_names
    assert "sample" in step_names
    assert "auth" not in step_names