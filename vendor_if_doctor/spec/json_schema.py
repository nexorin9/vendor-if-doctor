"""vendor-if-doctor 规格的 JSON Schema（Draft 2020-12）。

提供 :data:`SPEC_JSON_SCHEMA` 内嵌常量与 :func:`get_json_schema` 加载函数，
供 :mod:`vendor_if_doctor.spec.validate` 在启动期做完整 schema 校验。

schema 校验失败时，错误消息只暴露字段名 / 规则 / 修复建议，**永远不**含
``Authorization`` / ``token`` / ``password`` 等凭据字段值。
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any, Dict, List

# 协议枚举（与 schema.Protocol 一致；保持手工同步）
_BUILTIN_PROTOCOLS = ["http", "hl7v2", "webservice", "sql"]
_EXTRA_PROTOCOLS: List[str] = []
_PROTOCOLS: List[str] = list(_BUILTIN_PROTOCOLS)


def _add_extra_protocols(extra: List[str]) -> None:
    """把插件注册的协议追加到 JSON Schema 的 ``protocol`` enum。

    由 :mod:`vendor_if_doctor.connection.plugin` 在插件模块 import 时调用。
    已存在的协议名跳过；其余按出现顺序追加。

    重要：``SPEC_JSON_SCHEMA`` 在模块导入时已经把 ``_PROTOCOLS`` 列表引用挂到
    ``"enum": _PROTOCOLS``，因此 ``_PROTOCOLS`` 必须**原列表就地 mutate**（.extend）
    而不是重新赋值；否则 :func:`jsonschema.validate` 看到的仍是旧列表。
    """

    new_extras: List[str] = []
    for name in extra:
        key = (name or "").strip().lower()
        if not key:
            continue
        if key in _BUILTIN_PROTOCOLS:
            continue
        if key in _EXTRA_PROTOCOLS or key in new_extras:
            continue
        new_extras.append(key)
    if not new_extras:
        return
    _EXTRA_PROTOCOLS.extend(new_extras)
    _PROTOCOLS.extend(new_extras)


def _set_extra_protocols(extra: List[str]) -> None:
    """把插件协议列表裁到 ``extra``（**仅供测试重置用**）。
    """

    cleaned: List[str] = []
    for name in extra:
        key = (name or "").strip().lower()
        if key and key not in _BUILTIN_PROTOCOLS and key not in cleaned:
            cleaned.append(key)
    # 找到要保留的（与裁后列表一致），清掉原列表其余项
    _PROTOCOLS.clear()
    _PROTOCOLS.extend(_BUILTIN_PROTOCOLS)
    _PROTOCOLS.extend(cleaned)
    _EXTRA_PROTOCOLS.clear()
    _EXTRA_PROTOCOLS.extend(cleaned)


def get_protocols() -> List[str]:
    """返回当前 JSON Schema 接受的全部 ``protocol`` 字符串（内置 + 插件）。"""

    return list(_PROTOCOLS)


def builtin_protocols() -> List[str]:
    """返回内置协议列表（4 条不变）。"""

    return list(_BUILTIN_PROTOCOLS)


# 鉴权 type 枚举
_AUTH_TYPES = ["none", "basic", "bearer", "apikey", "mtls", "mllp"]


SPEC_JSON_SCHEMA: Dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "vendor_spec",
    "type": "object",
    "required": ["vendor_spec"],
    "additionalProperties": False,
    "properties": {
        "vendor_spec": {
            "type": "object",
            "required": ["schema_version", "interfaces"],
            "additionalProperties": False,
            "properties": {
                "schema_version": {"type": "string", "minLength": 1},
                "run_window_minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
                "severity_policy": {"type": "string", "minLength": 1},
                "interfaces": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"$ref": "#/$defs/vendor_interface"},
                },
                "inspection": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "fail_fast": {"type": "boolean"},
                        "handover_dir": {"type": "string"},
                        "tickets_dir": {"type": "string"},
                        "sqlite_db": {"type": "string"},
                        "redact_credentials": {"type": "boolean"},
                    },
                },
            },
        }
    },
    "$defs": {
        "vendor_interface": {
            "type": "object",
            "required": [
                "vendor_id",
                "interface_id",
                "display_name",
                "protocol",
                "host",
                "port",
                "auth",
                "sample_endpoint",
            ],
            "additionalProperties": False,
            "properties": {
                "vendor_id": {"type": "string", "minLength": 1, "pattern": r"^[a-z0-9][a-z0-9_\-]*$"},
                "interface_id": {"type": "string", "minLength": 1, "pattern": r"^[a-z0-9][a-z0-9_\-]*$"},
                "display_name": {"type": "string", "minLength": 1},
                "protocol": {"type": "string", "enum": _PROTOCOLS},
                "host": {"type": "string", "minLength": 1},
                "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                "base_path": {"type": "string"},
                "timeout_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 120},
                "auth": {
                    "type": "object",
                    "required": ["type"],
                    "properties": {
                        "type": {"type": "string", "enum": _AUTH_TYPES},
                    },
                    "oneOf": [
                        {"properties": {"type": {"const": "none"}}, "additionalProperties": False},
                        {
                            "properties": {
                                "type": {"const": "basic"},
                                "username_env": {"type": "string", "minLength": 1},
                                "secret_env": {"type": "string", "minLength": 1},
                                "realm": {"type": "string"},
                            },
                            "required": ["username_env", "secret_env"],
                            "additionalProperties": False,
                        },
                        {
                            "properties": {
                                "type": {"const": "bearer"},
                                "secret_env": {"type": "string", "minLength": 1},
                            },
                            "required": ["secret_env"],
                            "additionalProperties": False,
                        },
                        {
                            "properties": {
                                "type": {"const": "apikey"},
                                "secret_env": {"type": "string", "minLength": 1},
                                "inject": {"type": "string", "enum": ["header", "query"]},
                                "header_name": {"type": "string"},
                                "query_param": {"type": "string"},
                            },
                            "required": ["secret_env"],
                            "additionalProperties": False,
                        },
                        {
                            "properties": {
                                "type": {"const": "mtls"},
                                "cert_env": {"type": "string", "minLength": 1},
                                "key_env": {"type": "string", "minLength": 1},
                                "ca_bundle_env": {"type": "string"},
                            },
                            "required": ["cert_env", "key_env"],
                            "additionalProperties": False,
                        },
                        {
                            "properties": {
                                "type": {"const": "mllp"},
                            },
                            "additionalProperties": False,
                        },
                    ],
                },
                "sample_endpoint": {
                    "type": "object",
                    "anyOf": [
                        {
                            "type": "object",
                            "required": ["http"],
                            "additionalProperties": False,
                            "properties": {
                                "http": {
                                    "type": "object",
                                    "required": ["method", "path"],
                                    "additionalProperties": False,
                                    "properties": {
                                        "method": {
                                            "type": "string",
                                            "enum": ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
                                        },
                                        "path": {"type": "string", "pattern": r"^/"},
                                        "headers": {
                                            "type": "object",
                                            "additionalProperties": {"type": "string"},
                                        },
                                        "body": {"type": "string"},
                                        "list_endpoint": {
                                            "type": "object",
                                            "additionalProperties": False,
                                            "properties": {
                                                "method": {
                                                    "type": "string",
                                                    "enum": ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
                                                },
                                                "path": {"type": "string", "pattern": r"^/"},
                                                "headers": {
                                                    "type": "object",
                                                    "additionalProperties": {"type": "string"},
                                                },
                                                "page_param": {"type": "string"},
                                                "page_size_param": {"type": "string"},
                                                "page_size": {"type": "integer", "minimum": 1, "maximum": 1000},
                                                "expected_count_min": {"type": "integer", "minimum": 0},
                                            },
                                            "required": ["method", "path"],
                                        },
                                    },
                                }
                            },
                        },
                        {
                            "type": "object",
                            "required": ["hl7v2"],
                            "additionalProperties": False,
                            "properties": {
                                "hl7v2": {
                                    "type": "object",
                                    "required": ["message_type"],
                                    "additionalProperties": False,
                                    "properties": {
                                        "message_type": {"type": "string"},
                                        "trigger_event": {"type": "string"},
                                        "sample_patient_id": {"type": "string"},
                                    },
                                }
                            },
                        },
                    ],
                },
                "required_fields": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                },
            },
        }
    },
}


def get_json_schema() -> Dict[str, Any]:
    """返回内嵌 JSON Schema 深拷贝，避免调用方误改全局常量。"""

    return json.loads(json.dumps(SPEC_JSON_SCHEMA))


def schema_as_json_string() -> str:
    """返回 JSON Schema 的序列化字符串（用于落盘 / 调试）。"""

    return json.dumps(SPEC_JSON_SCHEMA, ensure_ascii=False, indent=2, sort_keys=True)


def load_packaged_schema_text() -> str:
    """从 vendored 的 ``spec.json`` 资源加载原始 schema 文本（备用入口）。"""

    pkg = resources.files("vendor_if_doctor").joinpath("spec", "vendor_spec.schema.json")
    if pkg.is_file():
        return pkg.read_text(encoding="utf-8")
    # 资源不存在时回落到内嵌常量
    return schema_as_json_string()


__all__ = [
    "SPEC_JSON_SCHEMA",
    "get_json_schema",
    "load_packaged_schema_text",
    "schema_as_json_string",
]
