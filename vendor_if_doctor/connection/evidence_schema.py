"""vendor-if-doctor Evidence 一致性 schema（task 26）。

对外导出：

* :data:`EVIDENCE_JSON_SCHEMA` — 内嵌 JSON Schema（Draft-07），跨协议 Evidence
  字段对齐契约的硬性约束；
* :func:`get_evidence_json_schema` — 返回深拷贝，避免调用方误改全局常量；
* :func:`load_packaged_evidence_schema_text` — 从 vendored 资源加载原始文本
  （备用入口，与 :mod:`vendor_if_doctor.spec.json_schema` 风格一致）。

schema 设计原则
---------------

* **protocol-agnostic 字段**（summary / command / latency_ms / status /
  failure_class / response_body / evidence_hash / meta / step_name / vendor_id /
  interface_id）所有协议共用，强制出现在 ``required`` 列表；
* **protocol-specific 字段**走 :attr:`meta.oneOf` 分支：HTTP / HL7v2 各自允许
  不同的 meta 字段（如 ``http_status`` vs ``ack_msa1``）；
* :attr:`Evidence.to_dict` 按字段名排序输出 dict，跨协议字段顺序对齐；
* 顶层 :attr:`additionalProperties=False`，禁止 Evidence 出现 schema 之外的
  顶层字段；
* Evidence 不应泄漏真实凭据字面量；本 schema **不**做凭据脱敏校验（那是
  :mod:`vendor_if_doctor.redact` 的职责），但 schema ``status`` / ``step_name``
  枚举约束保证类型安全。

不发起任何网络请求；schema 加载失败必须启动期立即抛 :class:`RuntimeError`。
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any, Dict


EVIDENCE_JSON_SCHEMA: Dict[str, Any] = {
    "$schema": "https://json-schema.org/draft-07/schema#",
    "$id": "https://vendor-if-doctor.local/connection/evidence_schema.json",
    "title": "Evidence",
    "description": (
        "巡检 Evidence 协议一致性 schema — protocol-agnostic 字段对所有协议统一；"
        "protocol-specific 字段走 oneOf 分支（HTTP / HL7v2）。"
    ),
    "type": "object",
    "required": [
        "summary",
        "command",
        "latency_ms",
        "status",
        "failure_class",
        "response_body",
        "evidence_hash",
        "meta",
        "step_name",
        "vendor_id",
        "interface_id",
    ],
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "command": {"type": "string"},
        "latency_ms": {"type": "integer", "minimum": 0},
        "status": {
            "type": "string",
            "enum": ["pass", "fail", "skip", "degraded"],
        },
        "failure_class": {
            "type": ["string", "null"],
        },
        "response_body": {
            "type": ["string", "null"],
        },
        "evidence_hash": {
            "type": ["string", "null"],
        },
        "step_name": {
            "type": "string",
            "enum": ["preflight", "auth", "list", "sample", "assert"],
        },
        "vendor_id": {"type": "string", "minLength": 1},
        "interface_id": {"type": "string", "minLength": 1},
        "meta": {
            "type": "object",
            "description": (
                "协议 / vendor 相关的额外字段。HTTP / HL7 各自有不同字段；"
                "跨协议时差异字段走 oneOf 分支。"
            ),
            "oneOf": [
                {
                    "$comment": "HTTP / WebService / SQL（任何含 http_status 的协议）",
                    "type": "object",
                    "required": ["http_status"],
                    "properties": {
                        "http_status": {
                            "type": "integer",
                            "minimum": 100,
                            "maximum": 599,
                        },
                        "preflight_ms": {"type": "integer", "minimum": 0},
                        "truncated": {"type": "boolean"},
                        "evidence_hash": {"type": "string"},
                        "first_failed_step": {"type": "string"},
                        "skipped_due_to": {"type": "string"},
                        "actual_count": {"type": "integer", "minimum": 0},
                        "encoding": {"type": "string"},
                        "bom_stripped": {"type": "boolean"},
                        "raw_size": {"type": "integer", "minimum": 0},
                        "duplicate_keys": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "parse_error": {"type": ["string", "null"]},
                        "reason": {"type": "string"},
                        "exception": {"type": "string"},
                        "allowed": {
                            "type": "array",
                            "items": {"type": "integer"},
                        },
                    },
                    "additionalProperties": True,
                },
                {
                    "$comment": "HL7v2 / MLLP（任何含 ack_msa1 的协议，不含 http_status）",
                    "type": "object",
                    "required": ["ack_msa1"],
                    "allOf": [{"not": {"required": ["http_status"]}}],
                    "properties": {
                        "ack_msa1": {"type": "string"},
                        "ack_received": {"type": "boolean"},
                        "ack_bytes": {"type": "integer", "minimum": 0},
                        "evidence_hash": {"type": "string"},
                        "first_failed_step": {"type": "string"},
                        "skipped_due_to": {"type": "string"},
                        "timeout_s": {"type": "number", "minimum": 0},
                        "host": {"type": "string"},
                        "port": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 65535,
                        },
                        "preflight_ms": {"type": "integer", "minimum": 0},
                        "exception": {"type": "string"},
                        "latency_ms": {"type": "integer", "minimum": 0},
                    },
                    "additionalProperties": True,
                },
                {
                    "$comment": "通用 meta：preflight 失败短路 / not_implemented / 占位 placeholder / auth step（不含 http_status / ack_msa1）",
                    "type": "object",
                    "allOf": [
                        {"not": {"required": ["http_status"]}},
                        {"not": {"required": ["ack_msa1"]}},
                    ],
                    "properties": {
                        "skipped_due_to": {"type": "string"},
                        "first_failed_step": {"type": "string"},
                        "reason": {"type": "string"},
                        "exception": {"type": "string"},
                        "host": {"type": "string"},
                        "port": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 65535,
                        },
                        "preflight_ms": {"type": "integer", "minimum": 0},
                        "auth_type": {"type": "string"},
                    },
                    "additionalProperties": True,
                },
            ],
        },
    },
}


def get_evidence_json_schema() -> Dict[str, Any]:
    """返回内嵌 JSON Schema 深拷贝，避免调用方误改全局常量。"""

    return json.loads(json.dumps(EVIDENCE_JSON_SCHEMA))


def schema_as_json_string() -> str:
    """返回 JSON Schema 的序列化字符串（用于落盘 / 调试）。"""

    return json.dumps(
        EVIDENCE_JSON_SCHEMA, ensure_ascii=False, indent=2, sort_keys=True
    )


def load_packaged_evidence_schema_text() -> str:
    """从 vendored 的 ``evidence_schema.json`` 资源加载原始 schema 文本。"""

    pkg = resources.files("vendor_if_doctor").joinpath(
        "connection", "evidence_schema.json"
    )
    if pkg.is_file():
        return pkg.read_text(encoding="utf-8")
    return schema_as_json_string()


__all__ = [
    "EVIDENCE_JSON_SCHEMA",
    "get_evidence_json_schema",
    "load_packaged_evidence_schema_text",
    "schema_as_json_string",
]
