"""HTTP 适配器完整测试（task 6）：5 种 auth + list 步骤 + 401/403 → auth_failed。"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, List, Optional, Tuple

import pytest

from vendor_if_doctor.connection.factory import build_connection
from vendor_if_doctor.connection.runner import test_connection as _test_connection
from vendor_if_doctor.connection.runner import test_one as _test_one
from vendor_if_doctor.spec.schema import (
    AuthApiKey,
    AuthBasic,
    AuthBearer,
    AuthMllp,
    AuthMtls,
    AuthNone,
    HttpListEndpoint,
    HttpSampleEndpoint,
    Protocol,
    SampleEndpoint,
    VendorInterface,
    VendorSpec,
)


# ---------------------------------------------------------------------------
# Mock HTTP server
# ---------------------------------------------------------------------------


class _MockHandler(BaseHTTPRequestHandler):
    """按 ``request_state``（module-level）控制响应：status / body / required_headers。"""

    # 避免 stderr 输出压满 pytest
    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass

    def do_GET(self) -> None:  # noqa: N802
        # 记录请求
        request_state["last_path"] = self.path
        request_state["last_headers"] = {k: v for k, v in self.headers.items()}

        status = request_state.get("status", 200)
        body = request_state.get("body", '{"code":200,"patient_id":"<EXAMPLE>","slot_id":"S1"}')

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))


request_state: Dict[str, object] = {}


@pytest.fixture
def mock_server() -> str:
    """启动一个 mock HTTP server；返回 host:port。"""

    server = HTTPServer(("127.0.0.1", 0), _MockHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.05)

    # 重置 state
    request_state.clear()
    request_state["status"] = 200
    request_state["body"] = '{"code":200,"patient_id":"<EXAMPLE>","slot_id":"S1"}'

    yield f"127.0.0.1:{port}"

    server.shutdown()
    server.server_close()


def _build_http_interface(
    host: str,
    port: int,
    auth,
    *,
    path: str = "/health",
    required_fields: Optional[List[str]] = None,
    list_endpoint: Optional[HttpListEndpoint] = None,
    body: str = "",
    headers: Optional[Dict[str, str]] = None,
) -> VendorInterface:
    return VendorInterface(
        vendor_id="mock",
        interface_id="health",
        display_name="Mock Health",
        protocol=Protocol.HTTP,
        host=host,
        port=port,
        base_path="/",
        timeout_seconds=5.0,
        auth=auth,
        sample_endpoint=SampleEndpoint(
            http=HttpSampleEndpoint(
                method="GET",
                path=path,
                headers=headers or {},
                body=body,
                list_endpoint=list_endpoint,
            )
        ),
        required_fields=required_fields or [],
    )


# ---------------------------------------------------------------------------
# 5 种 auth 类型 happy path
# ---------------------------------------------------------------------------


def test_auth_none_passes(mock_server: str) -> None:
    host, port = mock_server.rsplit(":", 1)
    ifc = _build_http_interface(host, int(port), AuthNone())
    spec = VendorSpec(schema_version="1.0", interfaces=[ifc])
    evidences = _test_connection(spec)
    assert evidences[-1].status == "pass"


def test_auth_basic_passes(mock_server: str) -> None:
    host, port = mock_server.rsplit(":", 1)
    ifc = _build_http_interface(
        host, int(port),
        AuthBasic(username_env="VENDOR_USER", secret_env="VENDOR_PASS"),
    )
    spec = VendorSpec(schema_version="1.0", interfaces=[ifc])
    import os
    env = os.environ.copy()
    env["VENDOR_USER"] = "alice"
    env["VENDOR_PASS"] = "wonderland"
    evidences = _test_connection(spec, env=env)
    # sample step pass
    sample_ev = next(e for e in evidences if e.step_name == "sample")
    assert sample_ev.status == "pass"
    # basic auth 注入验证：Authorization header 应为 "Basic <base64(alice:wonderland)>"
    import base64
    expected = "Basic " + base64.b64encode(b"alice:wonderland").decode("ascii")
    headers_lower = {k.lower(): v for k, v in request_state["last_headers"].items()}
    assert headers_lower["authorization"] == expected


def test_auth_bearer_passes(mock_server: str) -> None:
    host, port = mock_server.rsplit(":", 1)
    ifc = _build_http_interface(
        host, int(port),
        AuthBearer(secret_env="VENDOR_BEARER"),
    )
    spec = VendorSpec(schema_version="1.0", interfaces=[ifc])
    import os
    env = os.environ.copy()
    env["VENDOR_BEARER"] = "token-xyz"
    evidences = _test_connection(spec, env=env)
    sample_ev = next(e for e in evidences if e.step_name == "sample")
    assert sample_ev.status == "pass"
    headers_lower = {k.lower(): v for k, v in request_state["last_headers"].items()}
    assert headers_lower["authorization"] == "Bearer token-xyz"


def test_auth_apikey_header_passes(mock_server: str) -> None:
    host, port = mock_server.rsplit(":", 1)
    ifc = _build_http_interface(
        host, int(port),
        AuthApiKey(
            secret_env="VENDOR_APIKEY",
            inject="header",
            header_name="X-API-Key",
            query_param="api_key",
        ),
    )
    spec = VendorSpec(schema_version="1.0", interfaces=[ifc])
    import os
    env = os.environ.copy()
    env["VENDOR_APIKEY"] = "key-123"
    evidences = _test_connection(spec, env=env)
    sample_ev = next(e for e in evidences if e.step_name == "sample")
    assert sample_ev.status == "pass"
    # BaseHTTPRequestHandler 规范化 header name 为 title-case（X-Api-Key）
    headers_lower = {k.lower(): v for k, v in request_state["last_headers"].items()}
    assert headers_lower["x-api-key"] == "key-123"


def test_auth_apikey_query_passes(mock_server: str) -> None:
    host, port = mock_server.rsplit(":", 1)
    ifc = _build_http_interface(
        host, int(port),
        AuthApiKey(
            secret_env="VENDOR_APIKEY",
            inject="query",
            header_name="X-API-Key",
            query_param="api_key",
        ),
    )
    spec = VendorSpec(schema_version="1.0", interfaces=[ifc])
    import os
    env = os.environ.copy()
    env["VENDOR_APIKEY"] = "key-456"
    evidences = _test_connection(spec, env=env)
    sample_ev = next(e for e in evidences if e.step_name == "sample")
    assert sample_ev.status == "pass"
    # query 注入验证：URL 应含 api_key=key-456（mock 只记最后一个请求路径，
    # 即 sample 步骤的 path；list 步骤没配置所以未触发）
    assert "api_key=key-456" in request_state["last_path"]


def test_auth_mtls_passes(mock_server: str, tmp_path) -> None:
    """mTLS 在 mock server 下不真正验证证书，但应能完成 preflight+auth。"""
    host, port = mock_server.rsplit(":", 1)
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("dummy")
    key.write_text("dummy")
    ifc = _build_http_interface(
        host, int(port),
        AuthMtls(cert_env="VENDOR_CERT", key_env="VENDOR_KEY"),
    )
    spec = VendorSpec(schema_version="1.0", interfaces=[ifc])
    import os
    env = os.environ.copy()
    env["VENDOR_CERT"] = str(cert)
    env["VENDOR_KEY"] = str(key)
    evidences = _test_connection(spec, env=env)
    auth_ev = next(e for e in evidences if e.step_name == "auth")
    assert auth_ev.status == "pass"
    assert auth_ev.meta.get("auth_type") == "mtls"


def test_auth_mtls_missing_cert_env_fails(mock_server: str) -> None:
    host, port = mock_server.rsplit(":", 1)
    ifc = _build_http_interface(
        host, int(port),
        AuthMtls(cert_env="VENDOR_CERT_MISSING", key_env="VENDOR_KEY_MISSING"),
    )
    spec = VendorSpec(schema_version="1.0", interfaces=[ifc])
    evidences = _test_connection(spec)
    auth_ev = next(e for e in evidences if e.step_name == "auth")
    assert auth_ev.status == "fail"
    assert auth_ev.failure_class == "auth_failed"


def test_auth_mllp_on_http_interface_fails(mock_server: str) -> None:
    """HTTP 协议用 MLLP 鉴权应失败（auth_failed）。"""
    host, port = mock_server.rsplit(":", 1)
    ifc = _build_http_interface(host, int(port), AuthMllp())
    spec = VendorSpec(schema_version="1.0", interfaces=[ifc])
    evidences = _test_connection(spec)
    auth_ev = next(e for e in evidences if e.step_name == "auth")
    assert auth_ev.status == "fail"
    assert auth_ev.failure_class == "auth_failed"


def test_auth_bearer_missing_token_fails(mock_server: str) -> None:
    """bearer 凭据缺失 → auth_failed（不发起任何请求）。"""
    host, port = mock_server.rsplit(":", 1)
    ifc = _build_http_interface(
        host, int(port),
        AuthBearer(secret_env="VENDOR_BEARER_NOT_SET"),
    )
    spec = VendorSpec(schema_version="1.0", interfaces=[ifc])
    evidences = _test_connection(spec, env={})
    auth_ev = next(e for e in evidences if e.step_name == "auth")
    assert auth_ev.status == "fail"
    assert auth_ev.failure_class == "auth_failed"
    # 不应到达 sample 步骤
    sample_evs = [e for e in evidences if e.step_name == "sample"]
    for ev in sample_evs:
        assert ev.status == "skip"


# ---------------------------------------------------------------------------
# 401/403 → auth_failed 映射
# ---------------------------------------------------------------------------


def test_sample_step_401_maps_to_auth_failed(mock_server: str) -> None:
    """mock server 返回 401 → sample step failure_class='auth_failed'（task 6 核心）。"""
    host, port = mock_server.rsplit(":", 1)
    request_state["status"] = 401
    request_state["body"] = '{"error":"unauthorized"}'
    ifc = _build_http_interface(host, int(port), AuthNone())
    spec = VendorSpec(schema_version="1.0", interfaces=[ifc])
    evidences = _test_connection(spec)
    sample_ev = next(e for e in evidences if e.step_name == "sample")
    assert sample_ev.status == "fail"
    assert sample_ev.failure_class == "auth_failed"
    # assert 步骤被短路
    assert_ev = next(e for e in evidences if e.step_name == "assert")
    assert assert_ev.status == "skip"
    assert assert_ev.meta.get("skipped_due_to") == "sample"


def test_sample_step_403_maps_to_auth_failed(mock_server: str) -> None:
    host, port = mock_server.rsplit(":", 1)
    request_state["status"] = 403
    request_state["body"] = '{"error":"forbidden"}'
    ifc = _build_http_interface(host, int(port), AuthNone())
    spec = VendorSpec(schema_version="1.0", interfaces=[ifc])
    evidences = _test_connection(spec)
    sample_ev = next(e for e in evidences if e.step_name == "sample")
    assert sample_ev.status == "fail"
    assert sample_ev.failure_class == "auth_failed"


def test_sample_step_500_maps_to_http_status(mock_server: str) -> None:
    """5xx 不应误判为 auth_failed。"""
    host, port = mock_server.rsplit(":", 1)
    request_state["status"] = 500
    request_state["body"] = '{"error":"server error"}'
    ifc = _build_http_interface(host, int(port), AuthNone())
    spec = VendorSpec(schema_version="1.0", interfaces=[ifc])
    evidences = _test_connection(spec)
    sample_ev = next(e for e in evidences if e.step_name == "sample")
    assert sample_ev.status == "fail"
    assert sample_ev.failure_class == "http_status"


# ---------------------------------------------------------------------------
# required_fields 断言
# ---------------------------------------------------------------------------


def test_assert_step_missing_required_field_fails(mock_server: str) -> None:
    """response_body 缺 required_fields → schema_mismatch。"""
    host, port = mock_server.rsplit(":", 1)
    request_state["status"] = 200
    request_state["body"] = '{"code":200}'  # 缺 patient_id / slot_id
    ifc = _build_http_interface(
        host, int(port),
        AuthNone(),
        required_fields=["$.code", "$.patient_id", "$.slot_id"],
    )
    spec = VendorSpec(schema_version="1.0", interfaces=[ifc])
    evidences = _test_connection(spec)
    assert_ev = next(e for e in evidences if e.step_name == "assert")
    assert assert_ev.status == "fail"
    assert assert_ev.failure_class == "schema_mismatch"


def test_assert_step_all_required_fields_present_passes(mock_server: str) -> None:
    host, port = mock_server.rsplit(":", 1)
    ifc = _build_http_interface(
        host, int(port),
        AuthNone(),
        required_fields=["$.code", "$.patient_id"],
    )
    spec = VendorSpec(schema_version="1.0", interfaces=[ifc])
    evidences = _test_connection(spec)
    assert_ev = next(e for e in evidences if e.step_name == "assert")
    assert assert_ev.status == "pass"


# ---------------------------------------------------------------------------
# list 步骤（task 6 接入）
# ---------------------------------------------------------------------------


def test_list_step_with_endpoint_runs(mock_server: str) -> None:
    """配置 list_endpoint 时 list 步骤跑通（status=pass）。"""
    host, port = mock_server.rsplit(":", 1)
    request_state["status"] = 200
    request_state["body"] = '{"data":[{"id":1},{"id":2},{"id":3}]}'
    list_ep = HttpListEndpoint(
        method="GET", path="/items", page_param="page", page_size_param="size",
        page_size=10, expected_count_min=1,
    )
    ifc = _build_http_interface(
        host, int(port), AuthNone(),
        path="/health",
        list_endpoint=list_ep,
    )
    spec = VendorSpec(schema_version="1.0", interfaces=[ifc])
    evidences = _test_connection(spec)
    list_ev = next(e for e in evidences if e.step_name == "list")
    assert list_ev.status == "pass"
    assert list_ev.meta.get("http_status") == 200
    assert list_ev.meta.get("actual_count") == 3
    # list 步骤的 URL 通过 evidence.command 暴露（含分页参数）
    assert "page=1" in list_ev.command
    assert "size=10" in list_ev.command


def test_list_step_skipped_when_not_configured(mock_server: str) -> None:
    """未配置 list_endpoint 时 list 步骤输出 status=skip（不阻塞）。"""
    host, port = mock_server.rsplit(":", 1)
    ifc = _build_http_interface(host, int(port), AuthNone())
    spec = VendorSpec(schema_version="1.0", interfaces=[ifc])
    evidences = _test_connection(spec)
    list_ev = next(e for e in evidences if e.step_name == "list")
    assert list_ev.status == "skip"
    assert list_ev.meta.get("reason") == "no list_endpoint configured"


def test_list_step_above_expected_count_min_fails(mock_server: str) -> None:
    """list 步骤 expected_count_min 不满足 → schema_mismatch。"""
    host, port = mock_server.rsplit(":", 1)
    request_state["status"] = 200
    request_state["body"] = '{"data":[]}'  # 0 条
    list_ep = HttpListEndpoint(method="GET", path="/items", expected_count_min=5)
    ifc = _build_http_interface(
        host, int(port), AuthNone(),
        list_endpoint=list_ep,
    )
    spec = VendorSpec(schema_version="1.0", interfaces=[ifc])
    evidences = _test_connection(spec)
    list_ev = next(e for e in evidences if e.step_name == "list")
    assert list_ev.status == "fail"
    assert list_ev.failure_class == "schema_mismatch"
    assert list_ev.meta.get("expected_count_min") == 5
    assert list_ev.meta.get("actual_count") == 0


# ---------------------------------------------------------------------------
# 凭据纪律
# ---------------------------------------------------------------------------


def test_credentials_not_in_evidence_response_body(mock_server: str) -> None:
    """evidence.response_body 不含 token 字面值（_coarse_redact 兜底）。"""
    host, port = mock_server.rsplit(":", 1)
    request_state["status"] = 200
    request_state["body"] = '{"Authorization":"Bearer secret-leaked","code":200}'
    ifc = _build_http_interface(
        host, int(port),
        AuthBearer(secret_env="VENDOR_TOK"),
    )
    spec = VendorSpec(schema_version="1.0", interfaces=[ifc])
    import os
    env = os.environ.copy()
    env["VENDOR_TOK"] = "secret-leaked"
    evidences = _test_connection(spec, env=env)
    sample_ev = next(e for e in evidences if e.step_name == "sample")
    assert sample_ev.response_body is not None
    assert "secret-leaked" not in sample_ev.response_body


# ---------------------------------------------------------------------------
# shared httpx.Client 复用（task 6 验收）
# ---------------------------------------------------------------------------


def test_shared_httpx_client_reused_across_calls() -> None:
    """两次跑同一连接应复用同一 client（连接池复用）。"""
    from vendor_if_doctor.adapters.http import HTTPConnection

    ifc = _build_http_interface("127.0.0.1", 1, AuthNone())  # 任意 host
    conn = HTTPConnection(ifc, env={})
    try:
        client1 = conn.client
        client2 = conn.client
        assert client1 is client2  # 懒构造 + 复用
    finally:
        conn.close()
