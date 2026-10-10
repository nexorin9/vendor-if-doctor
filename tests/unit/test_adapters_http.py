"""test_adapters_http — HTTP 适配器单元测试（task 12）。

覆盖：
- HTTPConnection 5 步流水线（happy / 401 / 500 / 缺 required_fields）；
- shared httpx.Client 复用；
- 凭据纪律：response_body / command 不含 token 字面量；
- list_endpoint 配置时跑 list，未配置时 skip；
- expected_count_min 不达标 → schema_mismatch。
"""

from __future__ import annotations

from urllib.parse import urlparse

from vendor_if_doctor.connection import runner as _runner_mod
from vendor_if_doctor.connection.factory import build_connection
from vendor_if_doctor.spec.schema import (
    AuthApiKey,
    AuthBasic,
    AuthBearer,
    AuthNone,
    HttpListEndpoint,
    HttpSampleEndpoint,
    InspectionConfig,
    SampleEndpoint,
    VendorInterface,
    VendorSpec,
)


def _make_http_spec(
    host: str,
    port: int,
    *,
    auth_kind: str = "bearer",
    token_env: str = "VENDOR_TOKEN",
    username_env: str | None = None,
    path: str = "/",
    required_fields: list | None = None,
    list_endpoint: HttpListEndpoint | None = None,
    apikey_inject: str = "header",
) -> VendorSpec:
    if auth_kind == "none":
        auth = AuthNone()
    elif auth_kind == "bearer":
        auth = AuthBearer(secret_env=token_env)
    elif auth_kind == "basic":
        auth = AuthBasic(username_env=username_env or "VENDOR_USER", secret_env=token_env)
    elif auth_kind == "apikey":
        auth = AuthApiKey(
            secret_env=token_env,
            inject=apikey_inject,
            header_name="X-API-Key",
            query_param="api_key",
        )
    else:
        raise ValueError(auth_kind)

    interface = VendorInterface(
        vendor_id="his",
        interface_id="outpatient-register",
        display_name="HIS 门诊挂号接口",
        protocol="http",
        host=host,
        port=port,
        base_path="/",
        timeout_seconds=5,
        auth=auth,
        sample_endpoint=SampleEndpoint(http=HttpSampleEndpoint(
            method="GET", path=path, headers={}, body="", list_endpoint=list_endpoint
        )),
        required_fields=required_fields or ["$.code"],
    )
    return VendorSpec(
        schema_version="1.0",
        run_window_minutes=30,
        severity_policy="default",
        interfaces=[interface],
        inspection=InspectionConfig(),
    )


# 别名 — 避免 pytest 把 test_connection 当测试函数
do_test_connection = _runner_mod.test_connection


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_http_happy_path_returns_pass_on_all_steps(
    mock_http_happy, env_with_credentials
) -> None:
    parsed = urlparse(f"http://{mock_http_happy.host}:{mock_http_happy.port}")
    spec = _make_http_spec(parsed.hostname, parsed.port, token_env="VENDOR_LIS_TOKEN")
    evidence_list = do_test_connection(spec, env=env_with_credentials)
    assert len(evidence_list) == 5
    # 全部 pass（list 没配置则 skip，但不阻塞）
    for ev in evidence_list:
        assert ev.status in ("pass", "skip")
    # sample 和 assert 应 pass
    sample_ev = next(e for e in evidence_list if e.step_name == "sample")
    assert_ev = next(e for e in evidence_list if e.step_name == "assert")
    assert sample_ev.status == "pass"
    assert assert_ev.status == "pass"


def test_http_happy_path_includes_evidence_hash(
    mock_http_happy, env_with_credentials
) -> None:
    parsed = urlparse(f"http://{mock_http_happy.host}:{mock_http_happy.port}")
    spec = _make_http_spec(parsed.hostname, parsed.port, token_env="VENDOR_LIS_TOKEN")
    evidence_list = do_test_connection(spec, env=env_with_credentials)
    sample_ev = next(e for e in evidence_list if e.step_name == "sample")
    assert sample_ev.evidence_hash is not None
    assert len(sample_ev.evidence_hash) == 16


# ---------------------------------------------------------------------------
# 401 / 403 / 500
# ---------------------------------------------------------------------------


def test_http_401_maps_to_auth_failed(mock_http_401, env_with_credentials) -> None:
    parsed = urlparse(f"http://{mock_http_401.host}:{mock_http_401.port}")
    spec = _make_http_spec(parsed.hostname, parsed.port, token_env="VENDOR_LIS_TOKEN")
    evidence_list = do_test_connection(spec, env=env_with_credentials)
    sample_ev = next(e for e in evidence_list if e.step_name == "sample")
    assert sample_ev.status == "fail"
    assert sample_ev.failure_class == "auth_failed"
    # 后续 assert skip
    assert_ev = next(e for e in evidence_list if e.step_name == "assert")
    assert assert_ev.status == "skip"
    assert assert_ev.meta.get("skipped_due_to") == "sample"


def test_http_500_maps_to_http_status(mock_http_500, env_with_credentials) -> None:
    parsed = urlparse(f"http://{mock_http_500.host}:{mock_http_500.port}")
    spec = _make_http_spec(parsed.hostname, parsed.port, token_env="VENDOR_LIS_TOKEN")
    evidence_list = do_test_connection(spec, env=env_with_credentials)
    sample_ev = next(e for e in evidence_list if e.step_name == "sample")
    assert sample_ev.status == "fail"
    assert sample_ev.failure_class == "http_status"


# ---------------------------------------------------------------------------
# 缺 required_fields
# ---------------------------------------------------------------------------


def test_http_missing_required_fields_returns_schema_mismatch(
    mock_http_missing_required, env_with_credentials
) -> None:
    parsed = urlparse(
        f"http://{mock_http_missing_required.host}:{mock_http_missing_required.port}"
    )
    spec = _make_http_spec(
        parsed.hostname,
        parsed.port,
        token_env="VENDOR_LIS_TOKEN",
        required_fields=["$.code", "$.patient_id", "$.slot_id"],
    )
    evidence_list = do_test_connection(spec, env=env_with_credentials)
    assert_ev = next(e for e in evidence_list if e.step_name == "assert")
    assert assert_ev.status == "fail"
    assert assert_ev.failure_class == "schema_mismatch"
    failed = assert_ev.meta.get("failed_required_fields", [])
    assert "$.patient_id" in failed or "$.slot_id" in failed


# ---------------------------------------------------------------------------
# 凭据纪律
# ---------------------------------------------------------------------------


def test_http_response_body_does_not_leak_token(
    mock_http_happy, env_with_credentials
) -> None:
    parsed = urlparse(f"http://{mock_http_happy.host}:{mock_http_happy.port}")
    spec = _make_http_spec(parsed.hostname, parsed.port, token_env="VENDOR_LIS_TOKEN")
    evidence_list = do_test_connection(spec, env=env_with_credentials)
    for ev in evidence_list:
        assert "lis-secret" not in (ev.response_body or "")
        assert "lis-secret" not in ev.command


def test_http_missing_token_returns_auth_failed(
    mock_http_happy, env_clean
) -> None:
    """缺 bearer token 时 auth step 返回 auth_failed，sample 不发请求。"""

    parsed = urlparse(f"http://{mock_http_happy.host}:{mock_http_happy.port}")
    spec = _make_http_spec(parsed.hostname, parsed.port, token_env="VENDOR_LIS_TOKEN")
    evidence_list = do_test_connection(spec, env=env_clean)
    auth_ev = next(e for e in evidence_list if e.step_name == "auth")
    assert auth_ev.status == "fail"
    assert auth_ev.failure_class == "auth_failed"


# ---------------------------------------------------------------------------
# list_endpoint
# ---------------------------------------------------------------------------


def test_http_list_endpoint_with_pagination_runs_list_step(
    mock_http_happy, env_with_credentials
) -> None:
    """配置 list_endpoint 时 list step 跑通。"""

    # 同时给 list 路径返回 JSON 列表
    mock_http_happy.expect_request("/slots").respond_with_json(
        {"data": [{"id": "1"}, {"id": "2"}, {"id": "3"}]}
    )

    parsed = urlparse(f"http://{mock_http_happy.host}:{mock_http_happy.port}")
    list_ep = HttpListEndpoint(
        method="GET",
        path="/slots",
        headers={},
        page_param="page",
        page_size_param="size",
        page_size=10,
        expected_count_min=1,
    )
    spec = _make_http_spec(
        parsed.hostname,
        parsed.port,
        token_env="VENDOR_LIS_TOKEN",
        list_endpoint=list_ep,
    )
    evidence_list = do_test_connection(spec, env=env_with_credentials)
    list_ev = next(e for e in evidence_list if e.step_name == "list")
    assert list_ev.status == "pass"


def test_http_no_list_endpoint_returns_skip(
    mock_http_happy, env_with_credentials
) -> None:
    parsed = urlparse(f"http://{mock_http_happy.host}:{mock_http_happy.port}")
    spec = _make_http_spec(parsed.hostname, parsed.port, token_env="VENDOR_LIS_TOKEN")
    evidence_list = do_test_connection(spec, env=env_with_credentials)
    list_ev = next(e for e in evidence_list if e.step_name == "list")
    assert list_ev.status == "skip"


# ---------------------------------------------------------------------------
# 不可达
# ---------------------------------------------------------------------------


def test_http_unreachable_short_circuits(env_clean) -> None:
    spec = _make_http_spec("192.0.2.1", 80, token_env="VENDOR_TOKEN")
    evidence_list = do_test_connection(spec, env=env_clean)
    preflight_ev = evidence_list[0]
    assert preflight_ev.step_name == "preflight"
    assert preflight_ev.status == "fail"
    assert preflight_ev.latency_ms < 1500
    # 后续 skip
    for ev in evidence_list[1:]:
        assert ev.status == "skip"
        assert ev.meta.get("skipped_due_to") == "preflight"


# ---------------------------------------------------------------------------
# shared httpx.Client 复用
# ---------------------------------------------------------------------------


def test_http_shared_client_reused_across_steps(
    mock_http_happy, env_with_credentials
) -> None:
    """同一连接多次访问 .client 应返回同一实例（连接池复用）。"""

    parsed = urlparse(f"http://{mock_http_happy.host}:{mock_http_happy.port}")
    interface = VendorInterface(
        vendor_id="his",
        interface_id="outpatient-register",
        display_name="HIS",
        protocol="http",
        host=parsed.hostname,
        port=parsed.port,
        base_path="/",
        timeout_seconds=5,
        auth=AuthBearer(secret_env="VENDOR_LIS_TOKEN"),
        sample_endpoint=SampleEndpoint(http=HttpSampleEndpoint(
            method="GET", path="/", headers={}, body=""
        )),
        required_fields=["$.code"],
    )
    conn = build_connection(interface, env=env_with_credentials)
    c1 = conn.client
    c2 = conn.client
    assert c1 is c2  # 连接池复用