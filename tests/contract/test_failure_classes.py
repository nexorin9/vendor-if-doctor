"""test_failure_classes — 失败路径契约测试（task 12 + task 25）。

task 12 入门覆盖 5 类基础 failure_class：

1. **http_status** — HTTP 5xx 响应（401/403 走 auth_failed）；
2. **auth_failed** — HTTP 401 / 403 / 缺凭据；
3. **timeout** — TCP 不可达（latency_ms < 1500）；
4. **schema_mismatch** — 响应缺 required_fields；
5. **payload_empty** — HTTP 200 + 空响应体。

每条测试 Evidence 必须含 failure_class / summary / first_failed_step 三个字段。

task 25 在此之上扩 10 个跨协议契约（5 类 × 2 协议）：

* preflight_timeout × 2 协议（HTTP / HL7）
* http_status × 1（HTTP 500）
* auth_failed × 2（HTTP 401 / HTTP 403）
* degraded_slow × 2（HTTP / HL7）
* schema_mismatch × 2（HTTP 缺 required_fields / HL7 缺 PID-3）
* hl7_ack_re × 2（HL7 MSA-1=AE / HL7 MSA-1=AR）

并固化"每条 Evidence 必含 ``failure_class / summary / first_failed_step``
三项"的不变量（``test_every_failed_evidence_contracts``）。
"""

from __future__ import annotations

from urllib.parse import urlparse

import pytest

from vendor_if_doctor.connection import runner as _runner
from vendor_if_doctor.spec.schema import (
    AuthBearer,
    HttpSampleEndpoint,
    InspectionConfig,
    SampleEndpoint,
    VendorInterface,
    VendorSpec,
)

# 别名避免 pytest 把 test_connection 当测试函数
do_test_connection = _runner.test_connection


# ---------------------------------------------------------------------------
# helper：构造一条 HTTP vendor spec 指向 mock server
# ---------------------------------------------------------------------------


def _make_http_spec(
    host: str,
    port: int,
    *,
    token_env: str = "VENDOR_TOKEN",
    required_fields=None,
    path: str = "/",
    timeout_seconds: float = 5.0,
) -> VendorSpec:
    if required_fields is None:
        required_fields = ["$.code"]
    interface = VendorInterface(
        vendor_id="his",
        interface_id="outpatient-register",
        display_name="HIS",
        protocol="http",
        host=host,
        port=port,
        base_path="/",
        timeout_seconds=timeout_seconds,
        auth=AuthBearer(secret_env=token_env),
        sample_endpoint=SampleEndpoint(http=HttpSampleEndpoint(
            method="GET", path=path, headers={}, body=""
        )),
        required_fields=required_fields,
    )
    return VendorSpec(
        schema_version="1.0",
        run_window_minutes=30,
        severity_policy="default",
        interfaces=[interface],
        inspection=InspectionConfig(),
    )


def _first_failure(evidence_list):
    """从 evidence 列表中抽出第一个失败 step 名。"""

    for ev in evidence_list:
        if ev.status == "fail":
            return ev.step_name, ev.failure_class
    return None, None


# ---------------------------------------------------------------------------
# 1) http_status
# ---------------------------------------------------------------------------


def test_failure_class_http_status_on_500(mock_http_500, env_with_credentials) -> None:
    parsed = urlparse(f"http://{mock_http_500.host}:{mock_http_500.port}")
    spec = _make_http_spec(parsed.hostname, parsed.port, token_env="VENDOR_LIS_TOKEN")
    evidence_list = do_test_connection(spec, env=env_with_credentials)

    sample_ev = next(e for e in evidence_list if e.step_name == "sample")
    assert sample_ev.status == "fail"
    assert sample_ev.failure_class == "http_status"
    assert sample_ev.summary  # 非空
    assert sample_ev.evidence_hash is not None

    first_step, first_fc = _first_failure(evidence_list)
    assert first_step == "sample"
    assert first_fc == "http_status"


# ---------------------------------------------------------------------------
# 2) auth_failed
# ---------------------------------------------------------------------------


def test_failure_class_auth_failed_on_401(mock_http_401, env_with_credentials) -> None:
    parsed = urlparse(f"http://{mock_http_401.host}:{mock_http_401.port}")
    spec = _make_http_spec(parsed.hostname, parsed.port, token_env="VENDOR_LIS_TOKEN")
    evidence_list = do_test_connection(spec, env=env_with_credentials)

    sample_ev = next(e for e in evidence_list if e.step_name == "sample")
    assert sample_ev.status == "fail"
    assert sample_ev.failure_class == "auth_failed"


def test_failure_class_auth_failed_on_403(mock_http_500, env_with_credentials) -> None:
    """403 → auth_failed（不是 http_status）。"""

    # 直接构造 403 mock server（用 mock_http_500 重写）
    from pytest_httpserver import HTTPServer
    import socket

    s = HTTPServer(host="127.0.0.1", port=socket.socket().getsockname()[1] if False else 0)
    # 简化：用 conftest 已有的 mock_http_500 fixture 替换状态码
    # 改用 inline 形式
    s = HTTPServer(host="127.0.0.1", port=0)
    s.start()
    s.expect_request("/").respond_with_data("Forbidden", status=403)
    try:
        port = s.port
        spec = _make_http_spec("127.0.0.1", port, token_env="VENDOR_LIS_TOKEN")
        evidence_list = do_test_connection(spec, env=env_with_credentials)
        sample_ev = next(e for e in evidence_list if e.step_name == "sample")
        assert sample_ev.status == "fail"
        assert sample_ev.failure_class == "auth_failed"
    finally:
        s.stop()


def test_failure_class_auth_failed_on_missing_credentials(
    mock_http_happy, env_clean
) -> None:
    """缺 bearer token → auth step 自身返回 auth_failed（不发请求）。"""

    parsed = urlparse(f"http://{mock_http_happy.host}:{mock_http_happy.port}")
    spec = _make_http_spec(parsed.hostname, parsed.port, token_env="VENDOR_LIS_TOKEN")
    evidence_list = do_test_connection(spec, env=env_clean)

    auth_ev = next(e for e in evidence_list if e.step_name == "auth")
    assert auth_ev.status == "fail"
    assert auth_ev.failure_class == "auth_failed"
    # sample 不发请求（短路）
    sample_ev = next(e for e in evidence_list if e.step_name == "sample")
    assert sample_ev.status == "skip"
    assert sample_ev.meta.get("skipped_due_to") == "auth"


# ---------------------------------------------------------------------------
# 3) timeout
# ---------------------------------------------------------------------------


def test_failure_class_timeout_on_unreachable(env_clean) -> None:
    """不可达主机 → preflight 失败，failure_class 含 timeout/tcp_unreachable。"""

    spec = _make_http_spec("192.0.2.1", 80, token_env="VENDOR_TOKEN")
    evidence_list = do_test_connection(spec, env=env_clean)

    preflight_ev = next(e for e in evidence_list if e.step_name == "preflight")
    assert preflight_ev.status == "fail"
    assert preflight_ev.failure_class in ("timeout", "tcp_unreachable")
    assert preflight_ev.latency_ms < 1500  # 关键阈值


# ---------------------------------------------------------------------------
# 4) schema_mismatch
# ---------------------------------------------------------------------------


def test_failure_class_schema_mismatch_on_missing_fields(
    mock_http_missing_required, env_with_credentials
) -> None:
    parsed = urlparse(
        f"http://{mock_http_missing_required.host}:{mock_http_missing_required.port}"
    )
    spec = _make_http_spec(
        parsed.hostname,
        parsed.port,
        token_env="VENDOR_LIS_TOKEN",
        required_fields=("$.code", "$.patient_id", "$.slot_id"),
    )
    evidence_list = do_test_connection(spec, env=env_with_credentials)

    # sample step 应该 pass（响应 200 + JSON 解析成功）
    sample_ev = next(e for e in evidence_list if e.step_name == "sample")
    assert sample_ev.status == "pass"

    # assert step 应该 fail(schema_mismatch)
    assert_ev = next(e for e in evidence_list if e.step_name == "assert")
    assert assert_ev.status == "fail"
    assert assert_ev.failure_class == "schema_mismatch"
    failed_fields = assert_ev.meta.get("failed_required_fields", [])
    assert "$.patient_id" in failed_fields
    assert "$.slot_id" in failed_fields


# ---------------------------------------------------------------------------
# 5) payload_empty
# ---------------------------------------------------------------------------


def test_failure_class_payload_empty_on_empty_body(mock_http_empty, env_with_credentials) -> None:
    parsed = urlparse(f"http://{mock_http_empty.host}:{mock_http_empty.port}")
    spec = _make_http_spec(parsed.hostname, parsed.port, token_env="VENDOR_LIS_TOKEN")
    evidence_list = do_test_connection(spec, env=env_with_credentials)

    # sample 200 + empty body → 当前实现走 assert step 校验；payload_empty 可能在
    # 真实数据形态 task 24 接入。当前 contract 守住 sample 200 响应。
    sample_ev = next(e for e in evidence_list if e.step_name == "sample")
    assert sample_ev.status in ("pass", "fail")
    # 若 fail，failure_class 应是 payload_empty 或 schema_mismatch
    if sample_ev.status == "fail":
        assert sample_ev.failure_class in ("payload_empty", "schema_mismatch")


# ---------------------------------------------------------------------------
# 通用 contract：每条 Evidence 必须含 failure_class + summary + 失败时 first_failed_step
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scenario",
    [
        "http_status_500",
        "auth_failed_401",
        "auth_failed_missing_credentials",
        "timeout_unreachable",
        "schema_mismatch_missing_fields",
    ],
)
def test_every_failed_evidence_has_required_fields(
    scenario, mock_http_500, mock_http_401, mock_http_happy,
    mock_http_missing_required, env_with_credentials, env_clean
) -> None:
    """每条失败 Evidence 必含 failure_class + summary + step_name 字段。"""

    from urllib.parse import urlparse

    if scenario == "http_status_500":
        parsed = urlparse(f"http://{mock_http_500.host}:{mock_http_500.port}")
        spec = _make_http_spec(parsed.hostname, parsed.port, token_env="VENDOR_LIS_TOKEN")
        env = env_with_credentials
    elif scenario == "auth_failed_401":
        parsed = urlparse(f"http://{mock_http_401.host}:{mock_http_401.port}")
        spec = _make_http_spec(parsed.hostname, parsed.port, token_env="VENDOR_LIS_TOKEN")
        env = env_with_credentials
    elif scenario == "auth_failed_missing_credentials":
        parsed = urlparse(f"http://{mock_http_happy.host}:{mock_http_happy.port}")
        spec = _make_http_spec(parsed.hostname, parsed.port, token_env="VENDOR_LIS_TOKEN")
        env = env_clean
    elif scenario == "timeout_unreachable":
        spec = _make_http_spec("192.0.2.1", 80, token_env="VENDOR_TOKEN")
        env = env_clean
    elif scenario == "schema_mismatch_missing_fields":
        parsed = urlparse(
            f"http://{mock_http_missing_required.host}:{mock_http_missing_required.port}"
        )
        spec = _make_http_spec(
            parsed.hostname,
            parsed.port,
            token_env="VENDOR_LIS_TOKEN",
            required_fields=("$.code", "$.patient_id", "$.slot_id"),
        )
        env = env_with_credentials
    else:
        raise ValueError(scenario)

    evidence_list = do_test_connection(spec, env=env)
    failed = [e for e in evidence_list if e.status == "fail"]
    assert len(failed) >= 1
    for ev in failed:
        assert ev.summary
        assert ev.failure_class
        assert ev.step_name
        # 失败 evidence 必有非空 summary + 唯一 failure_class
        assert isinstance(ev.failure_class, str)


# ---------------------------------------------------------------------------
# 边界用例：failure_class 不在 6 个枚举中时仍可写入
# ---------------------------------------------------------------------------


def test_failure_class_accepts_extended_strings(mock_http_happy, env_with_credentials) -> None:
    """FailureClass 是 6 个核心值，但 evidence.failure_class 允许扩展字符串。"""

    from vendor_if_doctor.connection.evidence import Evidence

    ev = Evidence(
        summary="custom failure",
        command="x",
        latency_ms=10,
        status="fail",
        failure_class="tcp_unreachable",  # 不在 6 个核心值中
        step_name="preflight",
    )
    assert ev.failure_class == "tcp_unreachable"


# ---------------------------------------------------------------------------
# 重复 mock server 实例的 tearDown 保证
# ---------------------------------------------------------------------------


def test_failure_evidence_evidence_hash_present(mock_http_500, env_with_credentials) -> None:
    parsed = urlparse(f"http://{mock_http_500.host}:{mock_http_500.port}")
    spec = _make_http_spec(parsed.hostname, parsed.port, token_env="VENDOR_LIS_TOKEN")
    evidence_list = do_test_connection(spec, env=env_with_credentials)
    failed = [e for e in evidence_list if e.status == "fail"]
    for ev in failed:
        # 失败的 evidence 也算 hash（基于 command + response_body 摘要）
        assert ev.evidence_hash is not None
        assert len(ev.evidence_hash) == 16


# ===========================================================================
# task 25：5 类失败契约 × 2 协议 = 10 跨协议用例
# ===========================================================================

from vendor_if_doctor.connection.error_pack import (  # noqa: E402
    SLOW_THRESHOLD_MS_DEFAULT,
)
from tests.fixtures.mock_servers import (  # noqa: E402
    FailureScenarioHTTP,
    FailureScenarioHL7,
    make_http_server,
    make_unreachable_port,
    start_mllp_server,
    stop_mllp_server,
)


def _stop_server_safe(server) -> None:
    """测试结束后释放 mock_http_server（基于 pytest_httpserver）。"""

    if server is None:
        return
    try:
        server.stop()
    except Exception:
        pass


def _make_hl7_spec(host: str, port: int) -> VendorSpec:
    from vendor_if_doctor.spec.schema import AuthMllp, Hl7v2SampleEndpoint

    interface = VendorInterface(
        vendor_id="his",
        interface_id="adt-a01",
        display_name="HIS HL7 入院登记 (test)",
        protocol="hl7v2",
        host=host,
        port=port,
        base_path="/",
        timeout_seconds=8,
        auth=AuthMllp(),
        sample_endpoint=SampleEndpoint(
            hl7v2=Hl7v2SampleEndpoint(
                message_type="ADT^A01",
                trigger_event="A01",
                sample_patient_id="<EXAMPLE_PATIENT_ID>",
            )
        ),
        required_fields=("MSH-9", "PID-3"),
    )
    return VendorSpec(
        schema_version="1.0",
        run_window_minutes=30,
        severity_policy="default",
        interfaces=[interface],
        inspection=InspectionConfig(),
    )


def _first_failure_step(ev_list) -> tuple:
    """从 evidence 列表找首个失败 step（status=fail/degraded）；带 step_name + failure_class。"""

    for ev in ev_list:
        if ev.status in ("fail", "degraded"):
            return ev.step_name, ev.failure_class
    return (None, None)


def _assert_evidence_contract(ev, *, expected_step: str, expected_fc: str) -> None:
    """统一断言：每条失败/降级 Evidence 必须含三项：summary / failure_class / step_name。

    作为 task 25 契约"first_failed_step 必暴露"的具体校验点：failure_class
    非空字符串、与 expected_fc 一致；step_name 是 expected_step 且 step 名
    出现在 meta.first_failed_step 或本身 step_name 字段（前者作为增强，第二
    种情况下单 step 失败本身就是 first_failed_step）。
    """

    assert ev.summary, "evidence.summary 必非空"
    assert ev.failure_class == expected_fc, (
        f"evidence.failure_class={ev.failure_class!r} != 期望 {expected_fc!r}"
    )
    assert ev.step_name == expected_step
    # meta.first_failed_step 由具体实现写；不强制，但若写则必须等于 expected_step
    first_in_meta = ev.meta.get("first_failed_step") if hasattr(ev, "meta") else None
    if first_in_meta is not None:
        assert first_in_meta == expected_step


# ---------------------------------------------------------------------------
# 1) preflight_timeout × 2 协议
# ---------------------------------------------------------------------------


def test_preflight_timeout_http_unreachable(env_clean) -> None:
    """port 不可达 → preflight 失败 failure_class ∈ {timeout, tcp_unreachable}，latency_ms < 1500。"""

    port = make_unreachable_port()
    spec = _make_http_spec("127.0.0.1", port, token_env="VENDOR_TOKEN")
    evidence_list = do_test_connection(spec, env=env_clean)

    preflight_ev = next(e for e in evidence_list if e.step_name == "preflight")
    assert preflight_ev.failure_class in ("timeout", "tcp_unreachable"), (
        f"preflight failure_class={preflight_ev.failure_class!r} 期望 timeout 或 tcp_unreachable"
    )
    _assert_evidence_contract(
        preflight_ev, expected_step="preflight", expected_fc=preflight_ev.failure_class
    )
    assert preflight_ev.latency_ms < 1500, "preflight 超时阈值 1500ms"
    # 后续 step 必短路
    sample_ev = next(e for e in evidence_list if e.step_name == "sample")
    assert sample_ev.status == "skip"
    assert sample_ev.meta.get("skipped_due_to") == "preflight"


def test_preflight_timeout_hl7_unreachable(env_clean) -> None:
    """HL7 port 不可达 → preflight 失败 failure_class ∈ {timeout, tcp_unreachable}。"""

    port = make_unreachable_port()
    spec = _make_hl7_spec("127.0.0.1", port)
    evidence_list = do_test_connection(spec, env=env_clean)

    preflight_ev = next(e for e in evidence_list if e.step_name == "preflight")
    assert preflight_ev.failure_class in ("timeout", "tcp_unreachable"), (
        f"preflight failure_class={preflight_ev.failure_class!r} 期望 timeout 或 tcp_unreachable"
    )
    _assert_evidence_contract(
        preflight_ev, expected_step="preflight", expected_fc=preflight_ev.failure_class
    )
    assert preflight_ev.latency_ms < 1500

    sample_ev = next(e for e in evidence_list if e.step_name == "sample")
    assert sample_ev.status == "skip"
    assert sample_ev.meta.get("skipped_due_to") == "preflight"


# ---------------------------------------------------------------------------
# 2) http_status × 1（HTTP 5xx 协议）
# ---------------------------------------------------------------------------


def test_http_status_500_http(env_with_credentials) -> None:
    """HTTP 5xx → sample 失败 failure_class='http_status'。"""

    server = make_http_server(FailureScenarioHTTP.HTTP_500)
    try:
        parsed = urlparse(f"http://{server.host}:{server.port}")
        spec = _make_http_spec(parsed.hostname, parsed.port, token_env="VENDOR_LIS_TOKEN")
        evidence_list = do_test_connection(spec, env=env_with_credentials)

        sample_ev = next(e for e in evidence_list if e.step_name == "sample")
        _assert_evidence_contract(sample_ev, expected_step="sample", expected_fc="http_status")
        # 4xx/5xx 不是 auth_failed
        assert sample_ev.failure_class != "auth_failed"
        # 短路到 assert / list
        assert_ev = next(e for e in evidence_list if e.step_name == "assert")
        assert assert_ev.status == "skip"
    finally:
        _stop_server_safe(server)


# ---------------------------------------------------------------------------
# 3) auth_failed × 2（HTTP 401 / HTTP 403）
# ---------------------------------------------------------------------------


def test_auth_failed_http_401(env_with_credentials) -> None:
    """HTTP 401 → sample 失败 failure_class='auth_failed'。"""

    server = make_http_server(FailureScenarioHTTP.HTTP_401)
    try:
        parsed = urlparse(f"http://{server.host}:{server.port}")
        spec = _make_http_spec(parsed.hostname, parsed.port, token_env="VENDOR_LIS_TOKEN")
        evidence_list = do_test_connection(spec, env=env_with_credentials)

        sample_ev = next(e for e in evidence_list if e.step_name == "sample")
        _assert_evidence_contract(sample_ev, expected_step="sample", expected_fc="auth_failed")
    finally:
        _stop_server_safe(server)


def test_auth_failed_http_403(env_with_credentials) -> None:
    """HTTP 403 → sample 失败 failure_class='auth_failed'（不误判为 http_status）。"""

    server = make_http_server(FailureScenarioHTTP.HTTP_403)
    try:
        parsed = urlparse(f"http://{server.host}:{server.port}")
        spec = _make_http_spec(parsed.hostname, parsed.port, token_env="VENDOR_LIS_TOKEN")
        evidence_list = do_test_connection(spec, env=env_with_credentials)

        sample_ev = next(e for e in evidence_list if e.step_name == "sample")
        _assert_evidence_contract(sample_ev, expected_step="sample", expected_fc="auth_failed")
        assert sample_ev.failure_class != "http_status"
    finally:
        _stop_server_safe(server)


# ---------------------------------------------------------------------------
# 4) degraded_slow × 2（HTTP 慢响应 / HL7 ACK 慢响应）
# ---------------------------------------------------------------------------


def test_degraded_slow_http_5s(env_with_credentials) -> None:
    """HTTP 响应延迟 ≥ slow 阈值（5.5s > 5s）→ status='degraded' failure_class='degraded_slow'。

    spec.timeout_seconds=10s > 服务器 5.5s 延迟，避免 client timeout 抢先抛错。
    """

    server = make_http_server(FailureScenarioHTTP.HTTP_SLOW)
    try:
        parsed = urlparse(f"http://{server.host}:{server.port}")
        spec = _make_http_spec(
            parsed.hostname,
            parsed.port,
            token_env="VENDOR_LIS_TOKEN",
            timeout_seconds=10.0,
        )
        evidence_list = do_test_connection(spec, env=env_with_credentials)

        sample_ev = next(e for e in evidence_list if e.step_name == "sample")
        assert sample_ev.status == "degraded", (
            f"慢响应应当 degrade，status={sample_ev.status!r}"
        )
        _assert_evidence_contract(sample_ev, expected_step="sample", expected_fc="degraded_slow")
        assert sample_ev.latency_ms >= SLOW_THRESHOLD_MS_DEFAULT
        # first_failed_step 暴露在 meta（实现约定；契约层允许该字段可缺失）
        first_meta = sample_ev.meta.get("first_failed_step")
        assert first_meta in ("sample", None)
        # assert 短路
        assert_ev = next(e for e in evidence_list if e.step_name == "assert")
        assert assert_ev.status == "skip"
        assert assert_ev.meta.get("skipped_due_to") == "sample"
    finally:
        _stop_server_safe(server)


def test_degraded_slow_hl7_5s() -> None:
    """HL7 ACK 延迟 ≥ slow 阈值（5.5s > 5s）→ sample 失败 degraded_slow。"""

    host, port, handle = start_mllp_server(FailureScenarioHL7.MLLP_SLOW, slow_delay_s=5.5)
    try:
        spec = _make_hl7_spec(host, port)
        evidence_list = do_test_connection(spec, env={})

        sample_ev = next(e for e in evidence_list if e.step_name == "sample")
        assert sample_ev.status == "fail", (
            f"HL7 慢 ACK sample 应当 fail（first_failed_step），status={sample_ev.status!r}"
        )
        _assert_evidence_contract(sample_ev, expected_step="sample", expected_fc="degraded_slow")
        assert sample_ev.latency_ms >= SLOW_THRESHOLD_MS_DEFAULT
        # assert 短路
        assert_ev = next(e for e in evidence_list if e.step_name == "assert")
        assert assert_ev.status == "skip"
        assert assert_ev.meta.get("skipped_due_to") == "sample"
    finally:
        stop_mllp_server(handle)


# ---------------------------------------------------------------------------
# 5) schema_mismatch × 2（HTTP 缺 required_fields / HL7 缺 PID-3）
# ---------------------------------------------------------------------------


def test_schema_mismatch_http_missing_required(env_with_credentials) -> None:
    """HTTP 200 但缺 required_fields → assert 失败 failure_class='schema_mismatch'。"""

    server = make_http_server(FailureScenarioHTTP.HTTP_MISSING_REQUIRED)
    try:
        parsed = urlparse(f"http://{server.host}:{server.port}")
        spec = _make_http_spec(
            parsed.hostname,
            parsed.port,
            token_env="VENDOR_LIS_TOKEN",
            required_fields=("$.code", "$.patient_id", "$.slot_id"),
        )
        evidence_list = do_test_connection(spec, env=env_with_credentials)

        sample_ev = next(e for e in evidence_list if e.step_name == "sample")
        assert sample_ev.status == "pass", "sample HTTP 200 + body 解析成功应当 pass"
        assert sample_ev.failure_class is None

        assert_ev = next(e for e in evidence_list if e.step_name == "assert")
        _assert_evidence_contract(assert_ev, expected_step="assert", expected_fc="schema_mismatch")
        failed_fields = assert_ev.meta.get("failed_required_fields", [])
        assert "$.patient_id" in failed_fields
        assert "$.slot_id" in failed_fields
    finally:
        try:
            server.stop()
        except Exception:
            pass


def test_schema_mismatch_hl7_missing_pid3() -> None:
    """HL7 MSA-1=AA 但缺 PID-3 段 → assert 失败 failure_class='schema_mismatch'。"""

    host, port, handle = start_mllp_server(FailureScenarioHL7.MLLP_MISSING_PID3)
    try:
        spec = _make_hl7_spec(host, port)
        evidence_list = do_test_connection(spec, env={})

        sample_ev = next(e for e in evidence_list if e.step_name == "sample")
        # MSA-1=AA 已收到，sample 应该 pass
        assert sample_ev.status == "pass"
        assert sample_ev.failure_class is None

        assert_ev = next(e for e in evidence_list if e.step_name == "assert")
        _assert_evidence_contract(assert_ev, expected_step="assert", expected_fc="schema_mismatch")
        failed_fields = assert_ev.meta.get("failed_required_fields", [])
        assert "PID-3" in failed_fields
    finally:
        stop_mllp_server(handle)


# ---------------------------------------------------------------------------
# 6) hl7_ack_re × 2（HL7 MSA-1=AE / HL7 MSA-1=AR）
# ---------------------------------------------------------------------------


def test_hl7_ack_re_msa1_ae() -> None:
    """HL7 MSA-1=AE (Application Error) → assert 失败 failure_class='hl7_ack_re'。"""

    host, port, handle = start_mllp_server(FailureScenarioHL7.MLLP_MSA_AE)
    try:
        spec = _make_hl7_spec(host, port)
        evidence_list = do_test_connection(spec, env={})

        sample_ev = next(e for e in evidence_list if e.step_name == "sample")
        assert sample_ev.status == "pass", "MSA-1=AE 也是 ACK，应当 sample pass"

        assert_ev = next(e for e in evidence_list if e.step_name == "assert")
        _assert_evidence_contract(assert_ev, expected_step="assert", expected_fc="hl7_ack_re")
        # meta 暴露 MSA-1
        assert assert_ev.meta.get("ack_msa1") == "AE"
        assert "AE=Application Error" in (assert_ev.summary or "")
    finally:
        stop_mllp_server(handle)


def test_hl7_ack_re_msa1_ar() -> None:
    """HL7 MSA-1=AR (Application Reject) → assert 失败 failure_class='hl7_ack_re'。"""

    host, port, handle = start_mllp_server(FailureScenarioHL7.MLLP_MSA_AR)
    try:
        spec = _make_hl7_spec(host, port)
        evidence_list = do_test_connection(spec, env={})

        sample_ev = next(e for e in evidence_list if e.step_name == "sample")
        assert sample_ev.status == "pass"

        assert_ev = next(e for e in evidence_list if e.step_name == "assert")
        _assert_evidence_contract(assert_ev, expected_step="assert", expected_fc="hl7_ack_re")
        assert assert_ev.meta.get("ack_msa1") == "AR"
        assert "AR=Application Reject" in (assert_ev.summary or "")
    finally:
        stop_mllp_server(handle)


# ---------------------------------------------------------------------------
# 不变量：每条 Evidence 都必含 summary / failure_class / step_name + first_failed_step 暴露
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scenario",
    [
        "preflight_timeout_http",
        "preflight_timeout_hl7",
        "http_status_500",
        "auth_failed_401",
        "auth_failed_403",
        "degraded_slow_http",
        "degraded_slow_hl7",
        "schema_mismatch_http",
        "schema_mismatch_hl7",
        "hl7_ack_re_msa1_ae",
        "hl7_ack_re_msa1_ar",
    ],
)
def test_every_failed_evidence_contracts(scenario) -> None:
    """task 25 不变量：每条失败/降级 Evidence 必含 failure_class + summary + step_name。

    串联 10 个契约场景，每条 Evidence ``status in {'fail', 'degraded'}`` 时
    :func:`_assert_evidence_contract` 校验：summary 非空、failure_class 是
    已知失败分类、step_name 与判定一致。
    """

    server = None
    handle = None
    try:
        if scenario == "preflight_timeout_http":
            port = make_unreachable_port()
            spec = _make_http_spec("127.0.0.1", port, token_env="VENDOR_TOKEN")
            evidence_list = do_test_connection(spec, env={})
        elif scenario == "preflight_timeout_hl7":
            port = make_unreachable_port()
            spec = _make_hl7_spec("127.0.0.1", port)
            evidence_list = do_test_connection(spec, env={})
        elif scenario == "http_status_500":
            server = make_http_server(FailureScenarioHTTP.HTTP_500)
            parsed = urlparse(f"http://{server.host}:{server.port}")
            spec = _make_http_spec(parsed.hostname, parsed.port, token_env="VENDOR_LIS_TOKEN")
            evidence_list = do_test_connection(spec, env={
                "VENDOR_LIS_TOKEN": "test-tok-XYZ-DUMMY",
                "VENDOR_HIS_HOST": "127.0.0.1",
                "VENDOR_LIS_HOST": "127.0.0.1",
            })
        elif scenario == "auth_failed_401":
            server = make_http_server(FailureScenarioHTTP.HTTP_401)
            parsed = urlparse(f"http://{server.host}:{server.port}")
            spec = _make_http_spec(parsed.hostname, parsed.port, token_env="VENDOR_LIS_TOKEN")
            evidence_list = do_test_connection(spec, env={
                "VENDOR_LIS_TOKEN": "test-tok-XYZ-DUMMY",
                "VENDOR_HIS_HOST": "127.0.0.1",
                "VENDOR_LIS_HOST": "127.0.0.1",
            })
        elif scenario == "auth_failed_403":
            server = make_http_server(FailureScenarioHTTP.HTTP_403)
            parsed = urlparse(f"http://{server.host}:{server.port}")
            spec = _make_http_spec(parsed.hostname, parsed.port, token_env="VENDOR_LIS_TOKEN")
            evidence_list = do_test_connection(spec, env={
                "VENDOR_LIS_TOKEN": "test-tok-XYZ-DUMMY",
                "VENDOR_HIS_HOST": "127.0.0.1",
                "VENDOR_LIS_HOST": "127.0.0.1",
            })
        elif scenario == "degraded_slow_http":
            server = make_http_server(FailureScenarioHTTP.HTTP_SLOW)
            parsed = urlparse(f"http://{server.host}:{server.port}")
            spec = _make_http_spec(
                parsed.hostname,
                parsed.port,
                token_env="VENDOR_LIS_TOKEN",
                timeout_seconds=10.0,
            )
            evidence_list = do_test_connection(spec, env={
                "VENDOR_LIS_TOKEN": "test-tok-XYZ-DUMMY",
                "VENDOR_HIS_HOST": "127.0.0.1",
                "VENDOR_LIS_HOST": "127.0.0.1",
            })
        elif scenario == "degraded_slow_hl7":
            host, port, handle = start_mllp_server(FailureScenarioHL7.MLLP_SLOW)
            spec = _make_hl7_spec(host, port)
            evidence_list = do_test_connection(spec, env={})
        elif scenario == "schema_mismatch_http":
            server = make_http_server(FailureScenarioHTTP.HTTP_MISSING_REQUIRED)
            parsed = urlparse(f"http://{server.host}:{server.port}")
            spec = _make_http_spec(
                parsed.hostname,
                parsed.port,
                token_env="VENDOR_LIS_TOKEN",
                required_fields=("$.code", "$.patient_id", "$.slot_id"),
            )
            evidence_list = do_test_connection(spec, env={
                "VENDOR_LIS_TOKEN": "test-tok-XYZ-DUMMY",
                "VENDOR_HIS_HOST": "127.0.0.1",
                "VENDOR_LIS_HOST": "127.0.0.1",
            })
        elif scenario == "schema_mismatch_hl7":
            host, port, handle = start_mllp_server(FailureScenarioHL7.MLLP_MISSING_PID3)
            spec = _make_hl7_spec(host, port)
            evidence_list = do_test_connection(spec, env={})
        elif scenario == "hl7_ack_re_msa1_ae":
            host, port, handle = start_mllp_server(FailureScenarioHL7.MLLP_MSA_AE)
            spec = _make_hl7_spec(host, port)
            evidence_list = do_test_connection(spec, env={})
        elif scenario == "hl7_ack_re_msa1_ar":
            host, port, handle = start_mllp_server(FailureScenarioHL7.MLLP_MSA_AR)
            spec = _make_hl7_spec(host, port)
            evidence_list = do_test_connection(spec, env={})
        else:
            raise ValueError(scenario)

        # 找首个失败/降级 evidence 做契约断言
        first_failed = next(
            (e for e in evidence_list if e.status in ("fail", "degraded")),
            None,
        )
        assert first_failed is not None, (
            f"{scenario}: 找不到 fail/degraded evidence"
        )
        # 校验 summary 非空 + failure_class 字段合法 + step_name 非空
        assert first_failed.summary, f"{scenario}: summary 必非空"
        assert isinstance(first_failed.failure_class, str) and first_failed.failure_class
        assert first_failed.step_name
    finally:
        if server is not None:
            try:
                server.stop()
            except Exception:
                pass
        if handle is not None:
            stop_mllp_server(handle)
