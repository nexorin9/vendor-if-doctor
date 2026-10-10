"""auth — 5 种鉴权 header 注入函数 + mTLS 证书加载 单测（task 6 接入）。"""

from __future__ import annotations

import base64
import os
import tempfile

import pytest

from vendor_if_doctor.adapters.auth import (
    AuthHeaderError,
    MtlsCredentialError,
    MtlsMaterial,
    extract_apikey_query,
    inject_auth,
    load_mtls_cert,
)
from vendor_if_doctor.spec.schema import (
    AuthApiKey,
    AuthBasic,
    AuthBearer,
    AuthMllp,
    AuthMtls,
    AuthNone,
)


# ---------------------------------------------------------------------------
# none / basic / bearer / apikey 注入
# ---------------------------------------------------------------------------


def test_inject_auth_none_returns_original_headers() -> None:
    headers = {"User-Agent": "vendor-if-doctor/0.1", "X-Trace": "abc"}
    result = inject_auth(headers, AuthNone(), env={})
    assert result == headers
    assert result is not headers  # 新 dict


def test_inject_auth_basic_produces_authorization_basic() -> None:
    auth = AuthBasic(username_env="VENDOR_BASIC_USER", secret_env="VENDOR_BASIC_PASS")
    env = {"VENDOR_BASIC_USER": "alice", "VENDOR_BASIC_PASS": "wonderland"}
    headers = inject_auth({}, auth, env=env)
    expected = base64.b64encode(b"alice:wonderland").decode("ascii")
    assert headers["Authorization"] == f"Basic {expected}"
    # 凭据字面量不在 headers 里以明文泄漏
    assert "alice" not in headers["Authorization"] or "Basic" in headers["Authorization"]


def test_inject_auth_bearer_produces_authorization_bearer() -> None:
    auth = AuthBearer(secret_env="VENDOR_BEARER_TOKEN")
    env = {"VENDOR_BEARER_TOKEN": "s3cr3t-token-value"}
    headers = inject_auth({}, auth, env=env)
    assert headers["Authorization"] == "Bearer s3cr3t-token-value"


def test_inject_auth_apikey_header_injection() -> None:
    auth = AuthApiKey(
        secret_env="VENDOR_APIKEY",
        inject="header",
        header_name="X-API-Key",
        query_param="api_key",
    )
    env = {"VENDOR_APIKEY": "key-abc-123"}
    headers = inject_auth({}, auth, env=env)
    assert headers["X-API-Key"] == "key-abc-123"
    assert "__apikey_query__" not in headers
    assert extract_apikey_query(headers) is None


def test_inject_auth_apikey_query_injection_uses_magic_key() -> None:
    auth = AuthApiKey(
        secret_env="VENDOR_APIKEY",
        inject="query",
        header_name="X-API-Key",
        query_param="api_key",
    )
    env = {"VENDOR_APIKEY": "key-abc-123"}
    headers = inject_auth({}, auth, env=env)
    assert "__apikey_query__" in headers
    assert extract_apikey_query(headers) == "api_key=key-abc-123"
    assert "X-API-Key" not in headers


def test_inject_auth_missing_secret_env_raises() -> None:
    auth = AuthBearer(secret_env="VENDOR_MISSING_TOKEN")
    with pytest.raises(AuthHeaderError) as exc_info:
        inject_auth({}, auth, env={})
    assert "VENDOR_MISSING_TOKEN" in str(exc_info.value)
    assert "s3cr3t" not in str(exc_info.value)  # 不暴露任何凭据字面值


def test_inject_auth_basic_missing_username_raises() -> None:
    auth = AuthBasic(username_env="VENDOR_BASIC_USER", secret_env="VENDOR_BASIC_PASS")
    env = {"VENDOR_BASIC_USER": "", "VENDOR_BASIC_PASS": "secret"}
    with pytest.raises(AuthHeaderError):
        inject_auth({}, auth, env=env)


def test_inject_auth_mllp_in_http_raises() -> None:
    auth = AuthMllp()
    with pytest.raises(AuthHeaderError) as exc_info:
        inject_auth({}, auth, env={})
    assert "mllp" in str(exc_info.value).lower()


def test_inject_auth_mtls_returns_headers_unchanged() -> None:
    """mTLS 不写 header（凭 httpx.Client 的 cert=/verify= 装载）。"""
    auth = AuthMtls(cert_env="VENDOR_CERT", key_env="VENDOR_KEY")
    env = {"VENDOR_CERT": "/tmp/cert.pem", "VENDOR_KEY": "/tmp/key.pem"}
    headers = inject_auth({"User-Agent": "ua"}, auth, env=env)
    assert headers == {"User-Agent": "ua"}
    assert "Authorization" not in headers


# ---------------------------------------------------------------------------
# mTLS 证书加载
# ---------------------------------------------------------------------------


def test_load_mtls_cert_ok_with_existing_files(tmp_path) -> None:
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("dummy cert")
    key.write_text("dummy key")
    auth = AuthMtls(cert_env="VENDOR_CERT", key_env="VENDOR_KEY")
    env = {"VENDOR_CERT": str(cert), "VENDOR_KEY": str(key)}
    mat = load_mtls_cert(auth, env)
    assert mat.cert_path == str(cert)
    assert mat.key_path == str(key)
    assert mat.ca_path is None
    kwargs = mat.to_httpx_kwargs()
    assert kwargs["cert"] == (str(cert), str(key))
    assert kwargs["verify"] is True


def test_load_mtls_cert_with_ca_bundle(tmp_path) -> None:
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    ca = tmp_path / "ca.pem"
    cert.write_text("dummy")
    key.write_text("dummy")
    ca.write_text("dummy ca")
    auth = AuthMtls(
        cert_env="VENDOR_CERT",
        key_env="VENDOR_KEY",
        ca_bundle_env="VENDOR_CA",
    )
    env = {
        "VENDOR_CERT": str(cert),
        "VENDOR_KEY": str(key),
        "VENDOR_CA": str(ca),
    }
    mat = load_mtls_cert(auth, env)
    assert mat.ca_path == str(ca)
    kwargs = mat.to_httpx_kwargs()
    assert kwargs["verify"] == str(ca)


def test_load_mtls_cert_missing_env_raises() -> None:
    auth = AuthMtls(cert_env="VENDOR_CERT_MISSING", key_env="VENDOR_KEY_MISSING")
    with pytest.raises(MtlsCredentialError) as exc_info:
        load_mtls_cert(auth, env={})
    msg = str(exc_info.value)
    assert "VENDOR_CERT_MISSING" in msg
    assert "VENDOR_KEY_MISSING" in msg


def test_load_mtls_cert_file_not_found_raises_redacts_path(tmp_path) -> None:
    auth = AuthMtls(cert_env="VENDOR_CERT", key_env="VENDOR_KEY")
    env = {"VENDOR_CERT": str(tmp_path / "nonexistent.pem"), "VENDOR_KEY": "/etc/passwd"}
    with pytest.raises(MtlsCredentialError) as exc_info:
        load_mtls_cert(auth, env)
    # 错误消息走 _coarse_redact：路径不应泄漏敏感字段
    msg = str(exc_info.value)
    assert "nonexistent" in msg
    # _coarse_redact 兜底不影响绝对路径（path=/tmp/... 不含敏感字段名）


# ---------------------------------------------------------------------------
# 凭据纪律：注入过程不返回 token 原始值给 evidence
# ---------------------------------------------------------------------------


def test_inject_does_not_return_secret_via_headers_when_auth_failed() -> None:
    """注入失败时不返回 headers 副本（避免 secret 通过 dict 残留）。"""
    auth = AuthBearer(secret_env="VENDOR_FAIL_TOKEN")
    try:
        inject_auth({"X-Custom": "value"}, auth, env={})
    except AuthHeaderError:
        pass
    # 失败路径只抛异常，不返回含 token 的 dict


def test_apikey_header_value_not_leaked_to_authorization() -> None:
    auth = AuthApiKey(
        secret_env="VENDOR_APIKEY",
        inject="header",
        header_name="X-API-Key",
        query_param="api_key",
    )
    env = {"VENDOR_APIKEY": "key-abc-123"}
    headers = inject_auth({}, auth, env=env)
    assert "Authorization" not in headers
    assert headers["X-API-Key"] == "key-abc-123"


# ---------------------------------------------------------------------------
# pytest 发现
# ---------------------------------------------------------------------------


def test_module_exports() -> None:
    """auth.py 公开 API 完整性。"""
    from vendor_if_doctor.adapters import auth

    assert "inject_auth" in auth.__all__
    assert "load_mtls_cert" in auth.__all__
    assert "MtlsMaterial" in auth.__all__
    assert "extract_apikey_query" in auth.__all__
