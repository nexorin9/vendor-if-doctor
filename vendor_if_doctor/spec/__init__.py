"""vendor-if-doctor 规格（spec）模块。

对外导出：

* ``VendorSpec`` / ``VendorInterface`` — pydantic 强类型模型；
* ``load_vendor_spec`` — 从单文件 / 目录 / glob 加载 YAML，调用 ``validate_vendor_spec``；
* ``validate_vendor_spec`` — 启动期 JSON Schema 校验 + 凭据 / 必填字段二次校验；
* ``SPEC_JSON_SCHEMA`` — vendor_spec.schema.json 内嵌资源。

不在此模块发起任何网络请求；所有凭据 / 字段校验失败必须**启动期**立即抛
:class:`vendor_if_doctor.spec.errors.SpecValidationError`，**不**进入
``test_connection``。"""

from __future__ import annotations

from vendor_if_doctor.spec.errors import SpecValidationError
from vendor_if_doctor.spec.json_schema import SPEC_JSON_SCHEMA, get_json_schema
from vendor_if_doctor.spec.loader import load_vendor_spec, load_vendor_specs
from vendor_if_doctor.spec.schema import (
    AuthConfig,
    AuthNone,
    AuthBasic,
    AuthBearer,
    AuthApiKey,
    AuthMtls,
    AuthMllp,
    Hl7v2SampleEndpoint,
    HttpSampleEndpoint,
    InspectionConfig,
    Protocol,
    SampleEndpoint,
    VendorInterface,
    VendorSpec,
)
from vendor_if_doctor.spec.validate import (
    DEFAULT_VALIDATION_LOG,
    dump_validation_errors,
    say_ok,
    validate_raw,
    validate_vendor_spec,
)

__all__ = [
    "AuthApiKey",
    "AuthBasic",
    "AuthBearer",
    "AuthConfig",
    "AuthMllp",
    "AuthMtls",
    "AuthNone",
    "DEFAULT_VALIDATION_LOG",
    "Hl7v2SampleEndpoint",
    "HttpSampleEndpoint",
    "InspectionConfig",
    "Protocol",
    "SPEC_JSON_SCHEMA",
    "SampleEndpoint",
    "SpecValidationError",
    "VendorInterface",
    "VendorSpec",
    "dump_validation_errors",
    "get_json_schema",
    "load_vendor_spec",
    "load_vendor_specs",
    "say_ok",
    "validate_raw",
    "validate_vendor_spec",
]
