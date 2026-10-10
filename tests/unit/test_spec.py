"""test_spec — vendor_if_doctor.spec 单元测试（task 12）。

覆盖：
- load_vendor_spec 单文件 / 目录 / glob；
- SpecValidationError 触发条件（4 类 fixture）；
- validate_credentials=True 时缺 env 抛错；
- load_vendor_specs 多文件合并 + FQN 唯一约束。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from vendor_if_doctor.spec import (
    SpecValidationError,
    load_vendor_spec,
    load_vendor_specs,
    validate_vendor_spec,
)
from vendor_if_doctor.spec.errors import SpecIssue


# ---------------------------------------------------------------------------
# 单文件加载：合法 spec
# ---------------------------------------------------------------------------


def test_load_happy_path_http_spec(fixtures_dir: Path) -> None:
    spec = load_vendor_spec(fixtures_dir / "http_happy_path.yaml")
    assert len(spec.interfaces) == 1
    iface = spec.interfaces[0]
    assert iface.vendor_id == "his"
    assert iface.interface_id == "outpatient-register"
    assert iface.protocol == "http"
    assert iface.auth.type == "bearer"
    assert list(iface.required_fields) == ["$.code", "$.patient_id", "$.slot_id"]


def test_load_happy_path_hl7v2_spec(fixtures_dir: Path) -> None:
    spec = load_vendor_spec(fixtures_dir / "hl7v2_minimal.yaml")
    assert spec.interfaces[0].protocol == "hl7v2"
    assert spec.interfaces[0].auth.type == "mllp"
    assert "MSH-9" in list(spec.interfaces[0].required_fields)


def test_load_sql_placeholder_spec(fixtures_dir: Path) -> None:
    spec = load_vendor_spec(fixtures_dir / "sql_placeholder.yaml")
    assert spec.interfaces[0].protocol == "sql"
    assert spec.interfaces[0].auth.type == "none"


def test_load_webservice_placeholder_spec(fixtures_dir: Path) -> None:
    spec = load_vendor_spec(fixtures_dir / "webservice_placeholder.yaml")
    assert spec.interfaces[0].protocol == "webservice"
    assert spec.interfaces[0].auth.type == "basic"


# ---------------------------------------------------------------------------
# SpecValidationError 触发条件
# ---------------------------------------------------------------------------


def test_load_invalid_no_auth_raises_with_secret_env_reference(invalid_specs_dir: Path) -> None:
    with pytest.raises(SpecValidationError) as exc_info:
        load_vendor_spec(invalid_specs_dir / "invalid_no_auth.yaml")
    msg = str(exc_info.value)
    # 必须包含字段名提示（auth.secret_env），但绝不泄漏任何 token 字面量
    assert "auth" in msg or "secret_env" in msg
    assert "Bearer" not in msg
    assert "token" not in msg.lower() or "secret_env" in msg


def test_load_invalid_missing_required_raises(invalid_specs_dir: Path) -> None:
    with pytest.raises(SpecValidationError):
        load_vendor_spec(invalid_specs_dir / "invalid_missing_required.yaml")


def test_load_invalid_protocol_raises(invalid_specs_dir: Path) -> None:
    with pytest.raises(SpecValidationError) as exc_info:
        load_vendor_spec(invalid_specs_dir / "invalid_protocol.yaml")
    assert "protocol" in str(exc_info.value)


def test_load_invalid_no_endpoint_raises(invalid_specs_dir: Path) -> None:
    with pytest.raises(SpecValidationError) as exc_info:
        load_vendor_spec(invalid_specs_dir / "invalid_no_endpoint.yaml")
    assert "sample_endpoint" in str(exc_info.value)


def test_load_required_fields_empty_raises(invalid_specs_dir: Path) -> None:
    with pytest.raises(SpecValidationError):
        load_vendor_spec(invalid_specs_dir / "invalid_required_fields_empty.yaml")


def test_spec_validation_error_contains_issues() -> None:
    """SpecValidationError 必须有 issues 列表。"""

    # 构造一个故意无效的 spec：protocol 枚举外
    raw = {
        "vendor_spec": {
            "schema_version": "1.0",
            "interfaces": [
                {
                    "vendor_id": "x",
                    "interface_id": "y",
                    "display_name": "test",
                    "protocol": "invalid-proto",
                    "host": "127.0.0.1",
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
    # pydantic 应当直接抛 ValidationError，被 loader 包装为 SpecValidationError
    import yaml
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(raw, f)
        f_name = f.name
    try:
        with pytest.raises(SpecValidationError) as exc_info:
            load_vendor_spec(f_name)
        assert isinstance(exc_info.value.issues, list)
        assert len(exc_info.value.issues) >= 1
        assert isinstance(exc_info.value.issues[0], SpecIssue)
    finally:
        os.unlink(f_name)


# ---------------------------------------------------------------------------
# validate_credentials=True 行为
# ---------------------------------------------------------------------------


def test_validate_credentials_missing_raises_with_env_var_name(
    monkeypatch: pytest.MonkeyPatch, fixtures_dir: Path
) -> None:
    """缺凭据时抛错，错误消息含 env 变量名但不含值。"""

    for k in list(os.environ.keys()):
        if k.startswith("VENDOR_"):
            monkeypatch.delenv(k, raising=False)
    spec = load_vendor_spec(fixtures_dir / "missing_credentials.yaml")
    with pytest.raises(SpecValidationError) as exc_info:
        validate_vendor_spec(spec, env={})
    msg = str(exc_info.value)
    assert "VENDOR_MISSING_TOKEN_FOR_TEST" in msg
    # 不泄漏任何凭据字面量
    assert "Bearer" not in msg


def test_validate_credentials_present_passes(
    env_with_credentials: dict, fixtures_dir: Path
) -> None:
    """凭据 env 已 export 时不抛错。"""

    spec = load_vendor_spec(fixtures_dir / "http_happy_path.yaml")
    # 注入对应 token
    env_with_credentials["VENDOR_HIS_TOKEN"] = "test-token-happy-path"
    validate_vendor_spec(spec, env=env_with_credentials)


# ---------------------------------------------------------------------------
# load_vendor_specs 多文件 + 目录加载
# ---------------------------------------------------------------------------


def test_load_vendor_specs_valid_directory(fixtures_dir: Path) -> None:
    """valid_specs 目录模式加载全部 .yaml 文件（5 类）。"""

    spec = load_vendor_spec(fixtures_dir)
    # 5 个 valid fixture（http/hl7v2/sql/webservice/missing_credentials）
    assert len(spec.interfaces) == 5


def test_load_vendor_specs_glob(fixtures_dir: Path) -> None:
    """glob 模式加载匹配文件。"""

    import glob as _glob

    paths = sorted(_glob.glob(str(fixtures_dir / "*.yaml")))
    spec = load_vendor_specs(paths)
    assert len(spec.interfaces) == 5


def test_load_vendor_specs_empty_list_raises() -> None:
    with pytest.raises(SpecValidationError) as exc_info:
        load_vendor_specs([])
    assert "empty" in str(exc_info.value).lower() or "at least" in str(exc_info.value).lower()


def test_load_vendor_specs_duplicate_fqn_raises(tmp_path: Path) -> None:
    """两个 yaml 文件含相同 FQN 时抛错（pydantic ValidationError 或 SpecValidationError）。"""

    raw = """vendor_spec:
  schema_version: "1.0"
  interfaces:
    - vendor_id: dup
      interface_id: x
      display_name: "x"
      protocol: http
      host: 127.0.0.1
      port: 80
      auth: {type: bearer, secret_env: VENDOR_X}
      sample_endpoint:
        http: {method: GET, path: /x, headers: {}, body: ""}
      required_fields: ["$.code"]
"""
    p1 = tmp_path / "a.yaml"
    p2 = tmp_path / "b.yaml"
    p1.write_text(raw)
    p2.write_text(raw)
    # pydantic ValidationError 在 VendorSpec 合并阶段抛出；调用方应能捕获任一种异常
    with pytest.raises((SpecValidationError, Exception)) as exc_info:
        load_vendor_specs([p1, p2])
    # 错误消息应提及 FQN 不唯一
    msg = str(exc_info.value)
    assert "FQN" in msg or "unique" in msg.lower() or "不唯一" in msg


# ---------------------------------------------------------------------------
# examples/vendor_spec.example.yaml 加载（顶层端到端）
# ---------------------------------------------------------------------------


def test_load_example_spec_has_six_interfaces(example_vendor_spec_path: Path) -> None:
    spec = load_vendor_spec(example_vendor_spec_path)
    assert len(spec.interfaces) == 6
    fqns = sorted(i.fqn for i in spec.interfaces)
    assert "his.outpatient-register" in fqns
    assert "lis.report-push" in fqns
    assert "pacs.study-booking" in fqns
    assert "emr.emr-submit" in fqns
    assert "medical-insurance.claim-submit" in fqns
    assert "regional.read-only" in fqns