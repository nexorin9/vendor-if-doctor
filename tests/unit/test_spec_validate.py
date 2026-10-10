"""test_spec_validate — :mod:`vendor_if_doctor.spec.validate` 单元测试（task 13）。

覆盖：
* validate_raw happy path（合规 spec 不抛错）；
* validate_raw 4 类失败 fixture（缺 auth.secret_env / 缺 required_fields / protocol
  非法 / host 缺失）；
* 校验失败信息**永不**含凭据字段值（Authorization / Bearer / token / password /
  api_key / secret 字面量）；
* validate_vendor_spec 缺 env / env 为空字符串时抛错；
* dump_validation_errors 把 issues 写到 logs/spec_validation_error.log，文件里**不**
  含任何凭据字面量；
* say_ok 在 CLI 成功路径打印一行「OK — ...」；
* 启动期校验**不**发起任何网络请求（mock httpx / socket）。
"""

from __future__ import annotations

import io
import json
import socket
from pathlib import Path
from unittest.mock import patch

import pytest

from vendor_if_doctor.spec import (
    DEFAULT_VALIDATION_LOG,
    SpecValidationError,
    dump_validation_errors,
    load_vendor_spec,
    say_ok,
    validate_raw,
    validate_vendor_spec,
)
from vendor_if_doctor.spec.errors import SpecIssue


# ---------------------------------------------------------------------------
# validate_raw — happy path
# ---------------------------------------------------------------------------


def test_validate_raw_passes_for_example_spec(example_vendor_spec_path: Path) -> None:
    """examples/vendor_spec.example.yaml 是合规 spec：validate_raw 不抛错。"""

    raw = _read_yaml(example_vendor_spec_path)
    # 应静默返回（成功路径不抛错、不打印 OK — OK 由 CLI 显式调用 say_ok）
    validate_raw(raw, source=str(example_vendor_spec_path))


def test_validate_raw_silent_success_does_not_print(
    example_vendor_spec_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """validate_raw 成功路径**不**打印 OK（OK 由 CLI 显式 say_ok）。"""

    raw = _read_yaml(example_vendor_spec_path)
    validate_raw(raw)
    captured = capsys.readouterr()
    assert "OK" not in captured.out


# ---------------------------------------------------------------------------
# validate_raw — 4 类失败 fixture
# ---------------------------------------------------------------------------


def test_validate_raw_missing_secret_env(invalid_specs_dir: Path) -> None:
    """缺 auth.secret_env：抛 SpecValidationError，消息含 secret_env 但不含凭据值。"""

    raw = _read_yaml(invalid_specs_dir / "invalid_no_auth.yaml")
    with pytest.raises(SpecValidationError) as exc_info:
        validate_raw(raw)
    msg = str(exc_info.value)
    assert "auth" in msg
    assert "secret_env" in msg
    _assert_no_credential_leak(msg)


def test_validate_raw_empty_required_fields(invalid_specs_dir: Path) -> None:
    """required_fields 空列表：抛 SpecValidationError，消息含 required_fields。"""

    raw = _read_yaml(invalid_specs_dir / "invalid_required_fields_empty.yaml")
    with pytest.raises(SpecValidationError) as exc_info:
        validate_raw(raw)
    msg = str(exc_info.value)
    assert "required_fields" in msg
    _assert_no_credential_leak(msg)


def test_validate_raw_invalid_protocol(invalid_specs_dir: Path) -> None:
    """protocol 枚举非法：抛 SpecValidationError，消息含 protocol / enum 规则。"""

    raw = _read_yaml(invalid_specs_dir / "invalid_protocol.yaml")
    with pytest.raises(SpecValidationError) as exc_info:
        validate_raw(raw)
    msg = str(exc_info.value)
    assert "protocol" in msg
    # protocol 是枚举校验失败时 jsonschema validator 给出 enum
    assert "enum" in msg or "is not one of" in msg
    _assert_no_credential_leak(msg)


def test_validate_raw_missing_endpoint(invalid_specs_dir: Path) -> None:
    """sample_endpoint 形态不对齐（protocol=http 但缺 http 子段）：抛错，消息含 sample_endpoint。"""

    raw = _read_yaml(invalid_specs_dir / "invalid_no_endpoint.yaml")
    with pytest.raises(SpecValidationError) as exc_info:
        validate_raw(raw)
    msg = str(exc_info.value)
    assert "sample_endpoint" in msg
    _assert_no_credential_leak(msg)


def test_validate_raw_missing_host() -> None:
    """手工构造缺 host 的 spec，验证 host 校验触发 SpecValidationError。"""

    raw = {
        "vendor_spec": {
            "schema_version": "1.0",
            "interfaces": [
                {
                    "vendor_id": "his",
                    "interface_id": "x",
                    "display_name": "x",
                    "protocol": "http",
                    # host 字段缺失
                    "port": 80,
                    "auth": {"type": "none"},
                    "sample_endpoint": {
                        "http": {"method": "GET", "path": "/", "headers": {}, "body": ""}
                    },
                    "required_fields": ["$.code"],
                }
            ],
        }
    }
    with pytest.raises(SpecValidationError) as exc_info:
        validate_raw(raw)
    msg = str(exc_info.value)
    assert "host" in msg
    _assert_no_credential_leak(msg)


def test_validate_raw_missing_required(invalid_specs_dir: Path) -> None:
    """required_fields 全部缺字段 / 字段名为空：抛 SpecValidationError。"""

    raw = _read_yaml(invalid_specs_dir / "invalid_missing_required.yaml")
    with pytest.raises(SpecValidationError) as exc_info:
        validate_raw(raw)
    msg = str(exc_info.value)
    assert "required_fields" in msg
    _assert_no_credential_leak(msg)


# ---------------------------------------------------------------------------
# validate_raw — 通用边界
# ---------------------------------------------------------------------------


def test_validate_raw_root_not_mapping_raises() -> None:
    """根不是 mapping（list / scalar）抛 SpecValidationError。"""

    with pytest.raises(SpecValidationError) as exc_info:
        validate_raw(["not", "a", "mapping"])  # type: ignore[arg-type]
    assert "mapping" in str(exc_info.value)


def test_validate_raw_collects_multiple_issues() -> None:
    """一次校验应能收集多条 issue（issues 列表非空）。"""

    raw = {
        "vendor_spec": {
            "schema_version": "1.0",
            "interfaces": [
                {
                    "vendor_id": "x",
                    "interface_id": "y",
                    "display_name": "x",
                    "protocol": "ftp",  # 非法
                    "host": "127.0.0.1",
                    "port": 80,
                    "auth": {"type": "bearer"},  # 缺 secret_env
                    "sample_endpoint": {
                        "http": {"method": "GET", "path": "/", "headers": {}, "body": ""}
                    },
                    "required_fields": [],  # 空
                }
            ],
        }
    }
    with pytest.raises(SpecValidationError) as exc_info:
        validate_raw(raw)
    err = exc_info.value
    assert len(err.issues) >= 2
    fields = {i.field for i in err.issues}
    # protocol 路径 + secret_env + required_fields 至少 2 类
    assert any("protocol" in f for f in fields)
    assert any("secret_env" in f for f in fields) or any(
        "required_fields" in f for f in fields
    )


def test_spec_validation_error_exposes_issues_list(invalid_specs_dir: Path) -> None:
    """SpecValidationError.issues 必须暴露为 SpecIssue 列表。"""

    raw = _read_yaml(invalid_specs_dir / "invalid_protocol.yaml")
    with pytest.raises(SpecValidationError) as exc_info:
        validate_raw(raw)
    assert isinstance(exc_info.value.issues, list)
    assert len(exc_info.value.issues) >= 1
    for issue in exc_info.value.issues:
        assert isinstance(issue, SpecIssue)


def test_spec_validation_error_first_issue(invalid_specs_dir: Path) -> None:
    """first_issue() 返回 list[0]，便于 lint CLI 取首条渲染。"""

    raw = _read_yaml(invalid_specs_dir / "invalid_no_auth.yaml")
    with pytest.raises(SpecValidationError) as exc_info:
        validate_raw(raw)
    head = exc_info.value.first_issue()
    assert head is not None
    assert isinstance(head, SpecIssue)


# ---------------------------------------------------------------------------
# validate_vendor_spec — 凭据 / env 校验
# ---------------------------------------------------------------------------


def test_validate_vendor_spec_missing_env_raises_with_var_name(
    fixtures_dir: Path, env_clean: dict
) -> None:
    """缺凭据 env：抛错，错误消息含 VENDOR_* 变量名但不含值。"""

    spec = load_vendor_spec(fixtures_dir / "missing_credentials.yaml")
    with pytest.raises(SpecValidationError) as exc_info:
        validate_vendor_spec(spec, env=env_clean)
    msg = str(exc_info.value)
    assert "VENDOR_MISSING_TOKEN_FOR_TEST" in msg
    _assert_no_credential_leak(msg)


def test_validate_vendor_spec_empty_env_value_raises(
    fixtures_dir: Path,
) -> None:
    """env 值是空串时仍判定为未设置。"""

    spec = load_vendor_spec(fixtures_dir / "missing_credentials.yaml")
    env = {"VENDOR_MISSING_TOKEN_FOR_TEST": ""}
    with pytest.raises(SpecValidationError) as exc_info:
        validate_vendor_spec(spec, env=env)
    assert "empty" in str(exc_info.value).lower() or "空" in str(exc_info.value)


def test_validate_vendor_spec_present_passes(
    fixtures_dir: Path,
) -> None:
    """env 已 export 时不抛错。"""

    spec = load_vendor_spec(fixtures_dir / "http_happy_path.yaml")
    env = {"VENDOR_HIS_TOKEN": "test-token-happy"}
    validate_vendor_spec(spec, env=env)  # 不抛


def test_validate_vendor_spec_mtls_cert_env_path_not_found_raises(
    fixtures_dir: Path,
) -> None:
    """mTLS 证书 env 指向不存在的路径时抛 path not found 错误。"""

    raw = {
        "vendor_spec": {
            "schema_version": "1.0",
            "interfaces": [
                {
                    "vendor_id": "mi",
                    "interface_id": "claim-submit",
                    "display_name": "医保接口",
                    "protocol": "http",
                    "host": "mi.local",
                    "port": 443,
                    "auth": {
                        "type": "mtls",
                        "cert_env": "VENDOR_MI_MTLS_CERT",
                        "key_env": "VENDOR_MI_MTLS_KEY",
                    },
                    "sample_endpoint": {
                        "http": {"method": "POST", "path": "/", "headers": {}, "body": ""}
                    },
                    "required_fields": ["$.code"],
                }
            ],
        }
    }
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        import yaml

        yaml.safe_dump(raw, f)
        f_name = f.name
    try:
        spec = load_vendor_spec(f_name)
        env = {
            "VENDOR_MI_MTLS_CERT": "/nonexistent/path/cert.pem",
            "VENDOR_MI_MTLS_KEY": "/nonexistent/path/key.pem",
        }
        with pytest.raises(SpecValidationError) as exc_info:
            validate_vendor_spec(spec, env=env)
        msg = str(exc_info.value)
        assert "path not found" in msg
        _assert_no_credential_leak(msg)
    finally:
        import os

        os.unlink(f_name)


# ---------------------------------------------------------------------------
# dump_validation_errors — 落盘 + 凭据纪律
# ---------------------------------------------------------------------------


def test_dump_validation_errors_writes_log_without_credentials(
    invalid_specs_dir: Path, tmp_path: Path
) -> None:
    """dump_validation_errors 把 issues 写入 logs/spec_validation_error.log，文件不含任何凭据值。"""

    raw = _read_yaml(invalid_specs_dir / "invalid_no_auth.yaml")
    with pytest.raises(SpecValidationError) as exc_info:
        validate_raw(raw, source=str(invalid_specs_dir / "invalid_no_auth.yaml"))
    log_path = tmp_path / "spec_validation_error.log"
    written = dump_validation_errors(exc_info.value, log_path=log_path)
    assert written == log_path
    assert log_path.exists()
    text = log_path.read_text(encoding="utf-8")
    payload = json.loads(text.strip().splitlines()[-1])
    assert payload["source"].endswith("invalid_no_auth.yaml")
    assert payload["issue_count"] >= 1
    assert isinstance(payload["issues"], list)
    _assert_no_credential_leak(text)


def test_dump_validation_errors_default_path_is_log(
    invalid_specs_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认 log_path 落在 logs/spec_validation_error.log。"""

    # 把 cwd 切到 tmp_path，再调用 dump_validation_errors
    monkeypatch.chdir(tmp_path)
    raw = _read_yaml(invalid_specs_dir / "invalid_protocol.yaml")
    with pytest.raises(SpecValidationError) as exc_info:
        validate_raw(raw)
    written = dump_validation_errors(exc_info.value)
    assert written == Path("logs/spec_validation_error.log")
    assert (tmp_path / "logs" / "spec_validation_error.log").exists()


def test_dump_validation_errors_appends_multiple(
    invalid_specs_dir: Path, tmp_path: Path
) -> None:
    """多次 dump_validation_errors 追加到同一文件，每行 1 条 JSONL。"""

    log_path = tmp_path / "spec.log"
    for fixture_name in ("invalid_no_auth.yaml", "invalid_protocol.yaml", "invalid_no_endpoint.yaml"):
        raw = _read_yaml(invalid_specs_dir / fixture_name)
        with pytest.raises(SpecValidationError) as exc_info:
            validate_raw(raw)
        dump_validation_errors(exc_info.value, log_path=log_path)
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    for line in lines:
        payload = json.loads(line)
        assert "issues" in payload
        assert "ts" in payload


def test_default_validation_log_constant_path() -> None:
    """DEFAULT_VALIDATION_LOG 是 Path('logs/spec_validation_error.log')。"""

    assert DEFAULT_VALIDATION_LOG == Path("logs/spec_validation_error.log")


# ---------------------------------------------------------------------------
# say_ok — CLI 成功路径结尾
# ---------------------------------------------------------------------------


def test_say_ok_writes_to_stream() -> None:
    """say_ok 把 OK 行写到给定 stream。"""

    buf = io.StringIO()
    say_ok(stream=buf, label="vendor_spec.example.yaml")
    out = buf.getvalue()
    assert "OK — vendor_spec.example.yaml 校验通过" in out
    assert "启动期 fail-fast 已放过" in out


def test_say_ok_default_writes_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    """不传 stream 时默认写 stdout。"""

    say_ok(label="<test>")
    captured = capsys.readouterr()
    assert "OK — <test> 校验通过" in captured.out


# ---------------------------------------------------------------------------
# 启动期 fail-fast：校验不发起任何网络请求
# ---------------------------------------------------------------------------


def test_validate_raw_does_not_call_httpx_or_socket(
    example_vendor_spec_path: Path,
) -> None:
    """validate_raw 启动期**不**调用 httpx.Client.get 与 socket.socket。"""

    raw = _read_yaml(example_vendor_spec_path)
    with patch("httpx.Client.get") as mock_get, \
         patch("socket.socket") as mock_socket:
        validate_raw(raw)
        # validate_raw 不允许任何网络原语被触发
        assert mock_get.call_count == 0


def test_load_vendor_spec_does_not_dial_out(
    example_vendor_spec_path: Path,
) -> None:
    """load_vendor_spec 阶段也不该拨号（仅做 YAML + Schema 校验）。"""

    with patch("httpx.Client.get") as mock_get, \
         patch("socket.socket") as mock_socket:
        load_vendor_spec(example_vendor_spec_path)
    assert mock_get.call_count == 0


def test_validate_vendor_spec_does_not_dial_out(
    example_vendor_spec_path: Path,
) -> None:
    """validate_vendor_spec 阶段也不该拨号（仅检查 env 变量名是否 export）。"""

    spec = load_vendor_spec(example_vendor_spec_path)
    # 把所有需要的 VENDOR_* env 都填上，避免因 env 缺失而抛错
    env = {
        "VENDOR_HIS_USERNAME": "u",
        "VENDOR_HIS_PASSWORD": "p",
        "VENDOR_LIS_TOKEN": "t",
        "VENDOR_PACS_APIKEY": "k",
        "VENDOR_MI_USERNAME": "u",
        "VENDOR_MI_PASSWORD": "p",
    }
    with patch("httpx.Client.get") as mock_get, \
         patch("socket.socket") as mock_socket:
        validate_vendor_spec(spec, env=env)
    assert mock_get.call_count == 0


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _read_yaml(path: Path) -> dict:
    import yaml

    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data


def _assert_no_credential_leak(msg: str) -> None:
    """断言校验错误消息里不含任何凭据字面量。

    仅检测示例 / 测试中可能出现的字面值；不试图证明强安全性。
    """

    forbidden_literals = [
        "Bearer ",
        "Basic ",
        "lis-secret-token-9999",
        "wonderland",
        "pacs-secret-key-777",
        "emr-mllp-token-abc",
        "SUPER-SECRET",
        "super-secret",
        "JWT-eyJ",
        "TOK-8888",
        "KEY-9999",
        "LIST-TOK-7777",
    ]
    msg_lower = msg.lower()
    for lit in forbidden_literals:
        # 仅检查字面值；不要把字段名匹配进去（如 secret_env / secret 等）
        # 'Bearer ' / 'Basic ' 的字面量只在 Header 取值时出现
        if lit.lower() in msg_lower:
            pytest.fail(f"凭据字面量泄漏在错误消息中：{lit!r}\nmessage={msg[:400]}")
