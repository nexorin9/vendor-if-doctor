"""vendor-if-doctor 启动期校验。

行为约定：
* 调用 :func:`validate_vendor_spec` 后，**不**发起任何网络请求。
* 任何字段缺失（``auth.secret_env``、``required_fields`` 缺失元素、``host`` 缺失）
  立即抛 :class:`SpecValidationError` 并指出字段名 + 规则 + 修复建议。
* 校验失败信息**永不含**凭据字段值（``Authorization`` / ``Bearer xxx`` / token
  / password）—只暴露字段名与规则。

辅助能力（task 13）：
* :func:`dump_validation_errors` — 把失败原因写到 ``logs/spec_validation_error.log``，
  写入内容只含字段名 / 规则 / 修复建议，**绝不**含任何凭据字面量，便于巡检 / 飞检留痕；
* :func:`make_ok_messenger` — 返回一个回调，调用后打印「OK — vendor_spec 校验通过」，
  供 CLI ``lint`` / ``init`` 子命令在成功路径结尾使用。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from vendor_if_doctor.spec.errors import SpecIssue, SpecValidationError
from vendor_if_doctor.spec.json_schema import SPEC_JSON_SCHEMA
from vendor_if_doctor.spec.schema import (
    AuthApiKey,
    AuthBasic,
    AuthBearer,
    AuthMtls,
    VendorInterface,
    VendorSpec,
)

logger = logging.getLogger(__name__)


# --- 凭据 / 必填字段二次校验 --------------------------------------------------


def _check_secret_env_required(raw: Mapping[str, Any]) -> List[SpecIssue]:
    """检查所有需要 ``secret_env`` 的鉴权类型是否填写了非空环境变量名。

    注意：这里只校验**环境变量名**是否填写，**不**读取环境变量，也不解析模板，
    留到执行器阶段再读取实际凭据。
    """

    issues: List[SpecIssue] = []
    interfaces = raw.get("vendor_spec", {}).get("interfaces", [])
    for idx, itf in enumerate(interfaces):
        auth = itf.get("auth") or {}
        atype = auth.get("type")
        prefix = f"vendor_spec.interfaces[{idx}] ({itf.get('vendor_id')}.{itf.get('interface_id')}).auth"
        if atype in {"basic", "bearer", "apikey"}:
            if not auth.get("secret_env"):
                issues.append(
                    SpecIssue(
                        field=f"{prefix}.secret_env",
                        rule="must be set for auth.type=" + str(atype),
                        hint="请在 vendor_spec.yaml 写 VENDOR_<NAME>_TOKEN / PASSWORD / API_KEY 的环境变量名",
                    )
                )
        elif atype == "mtls":
            for f in ("cert_env", "key_env"):
                if not auth.get(f):
                    issues.append(
                        SpecIssue(
                            field=f"{prefix}.{f}",
                            rule="must be set for auth.type=mtls",
                            hint="请在 vendor_spec.yaml 写 VENDOR_<NAME>_MTLS_CERT / _KEY 的环境变量名",
                        )
                    )
    return issues


def _check_required_fields_present(raw: Mapping[str, Any]) -> List[SpecIssue]:
    """``required_fields`` 非空检查：HTTP 至少 1 个 JSONPath；HL7v2 至少 1 个段路径。"""

    issues: List[SpecIssue] = []
    interfaces = raw.get("vendor_spec", {}).get("interfaces", [])
    for idx, itf in enumerate(interfaces):
        prefix = f"vendor_spec.interfaces[{idx}] ({itf.get('vendor_id')}.{itf.get('interface_id')})"
        req = itf.get("required_fields")
        if req is None:
            continue
        if not isinstance(req, list) or not req:
            issues.append(
                SpecIssue(
                    field=f"{prefix}.required_fields",
                    rule="must be a non-empty list",
                    hint="至少声明 1 个 JSONPath 或 HL7 段路径（如 $.code / MSH-9）",
                )
            )
            continue
        for k, item in enumerate(req):
            if not isinstance(item, str) or not item.strip():
                issues.append(
                    SpecIssue(
                        field=f"{prefix}.required_fields[{k}]",
                        rule="must be non-empty string",
                        hint="HTTP 写 JSONPath（如 $.patient_id），HL7 写段路径（如 PID-3）",
                    )
                )
    return issues


def _check_protocol_endpoint_alignment(raw: Mapping[str, Any]) -> List[SpecIssue]:
    """``protocol`` 与 ``sample_endpoint`` 形态对齐检查。"""

    issues: List[SpecIssue] = []
    interfaces = raw.get("vendor_spec", {}).get("interfaces", [])
    for idx, itf in enumerate(interfaces):
        protocol = itf.get("protocol")
        ep = itf.get("sample_endpoint") or {}
        prefix = f"vendor_spec.interfaces[{idx}] ({itf.get('vendor_id')}.{itf.get('interface_id')})"
        if protocol == "http" and "http" not in ep:
            issues.append(
                SpecIssue(
                    field=f"{prefix}.sample_endpoint.http",
                    rule="required when protocol=http",
                    hint="sample_endpoint.http.{method,path} 必须存在",
                )
            )
        elif protocol == "hl7v2" and "hl7v2" not in ep:
            issues.append(
                SpecIssue(
                    field=f"{prefix}.sample_endpoint.hl7v2",
                    rule="required when protocol=hl7v2",
                    hint="sample_endpoint.hl7v2.message_type 必须存在",
                )
            )
    return issues


# --- 顶层校验入口 -------------------------------------------------------------


def validate_raw(raw: Mapping[str, Any], *, source: str | None = None) -> None:
    """对原始 dict 做 JSON Schema 校验 + 凭据 / 必填字段二次校验。

    失败抛 :class:`SpecValidationError`；成功则静默返回（不打印 OK，避免污染日志）。
    """

    if not isinstance(raw, Mapping):
        raise SpecValidationError(
            [SpecIssue(field="<root>", rule="must be a mapping", hint="vendor_spec 文件根必须是 YAML mapping")],
            source=source,
        )

    # 1) JSON Schema 校验（Draft 2020-12）
    schema_issues: List[SpecIssue] = []
    try:
        validator = Draft202012Validator(SPEC_JSON_SCHEMA)
        for err in sorted(validator.iter_errors(dict(raw)), key=lambda e: list(e.absolute_path)):
            path = "/".join(str(p) for p in err.absolute_path) or "<root>"
            schema_issues.append(
                SpecIssue(
                    field=path,
                    rule=err.validator,
                    hint=err.message[:200],
                )
            )
    except Exception as e:  # pragma: no cover - schema 自身错误
        raise SpecValidationError(
            [SpecIssue(field="<schema>", rule="internal_error", hint=str(e)[:200])],
            source=source,
        ) from e

    issues: List[SpecIssue] = list(schema_issues)
    issues.extend(_check_secret_env_required(raw))
    issues.extend(_check_required_fields_present(raw))
    issues.extend(_check_protocol_endpoint_alignment(raw))

    if issues:
        raise SpecValidationError(issues, source=source)


def validate_vendor_spec(
    spec: VendorSpec,
    *,
    env: Optional[Mapping[str, str]] = None,
    source: str | None = None,
) -> VendorSpec:
    """对已构建好的 :class:`VendorSpec` 做凭据可用性校验。

    * 默认 ``env`` 为 :data:`os.environ`；可注入测试替身。
    * 校验失败抛 :class:`SpecValidationError`。
    """

    env_map = dict(env) if env is not None else dict(os.environ)
    issues: List[SpecIssue] = []

    for idx, itf in enumerate(spec.interfaces):
        prefix = f"interfaces[{idx}] ({itf.fqn}).auth"
        auth = itf.auth
        if isinstance(auth, (AuthBasic, AuthBearer, AuthApiKey)):
            env_name = auth.secret_env
            if env_name not in env_map:
                issues.append(
                    SpecIssue(
                        field=f"{prefix}.secret_env ({env_name})",
                        rule="environment variable not set",
                        hint=f"export {env_name}=*** 后重试（凭据永不入库）",
                    )
                )
            elif not env_map[env_name]:
                issues.append(
                    SpecIssue(
                        field=f"{prefix}.secret_env ({env_name})",
                        rule="environment variable is empty",
                        hint=f"export {env_name}=<non-empty> 后重试",
                    )
                )
        if isinstance(auth, AuthBasic):
            if auth.username_env not in env_map or not env_map[auth.username_env]:
                issues.append(
                    SpecIssue(
                        field=f"{prefix}.username_env ({auth.username_env})",
                        rule="environment variable not set or empty",
                        hint=f"export {auth.username_env}=*** 后重试",
                    )
                )
        if isinstance(auth, AuthMtls):
            for fld, name in (("cert_env", auth.cert_env), ("key_env", auth.key_env)):
                val = env_map.get(name)
                if not val:
                    issues.append(
                        SpecIssue(
                            field=f"{prefix}.{fld} ({name})",
                            rule="environment variable not set",
                            hint=f"export {name}=<abs path> 后重试",
                        )
                    )
                elif not Path(val).exists():
                    issues.append(
                        SpecIssue(
                            field=f"{prefix}.{fld} ({name})",
                            rule="path not found",
                            hint=f"{val} 不存在；检查证书路径或重启终端",
                        )
                    )

    if issues:
        raise SpecValidationError(issues, source=source)
    return spec


# --- 失败产物落盘 + OK 回调 ---------------------------------------------------


_DEFAULT_VALIDATION_LOG = Path("logs") / "spec_validation_error.log"


def _serialize_issue(issue: SpecIssue) -> Dict[str, str]:
    """把 :class:`SpecIssue` 序列化为安全 dict（字段名 / 规则 / 修复建议）。"""

    return {
        "field": str(issue.field),
        "rule": str(issue.rule),
        "hint": str(issue.hint),
    }


def dump_validation_errors(
    exc: SpecValidationError,
    *,
    log_path: Optional[Path] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> Path:
    """把 :class:`SpecValidationError` 的 issues 写到 ``logs/spec_validation_error.log``。

    写入内容**只含**字段名 / 规则 / 修复建议。绝不含任何凭据字面量（错误源里出现过的
    token / Authorization / Bearer 字面值不会进 log，因为 :class:`SpecIssue` 不携带值）。
    返回实际落盘的日志路径。
    """

    target = Path(log_path) if log_path else _DEFAULT_VALIDATION_LOG
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "ts": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": exc.source or "<unknown>",
        "issue_count": len(exc.issues),
        "issues": [_serialize_issue(i) for i in exc.issues],
    }
    if extra:
        for k, v in extra.items():
            if k in ("ts", "source", "issue_count", "issues"):
                continue
            payload[str(k)] = v

    line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    with target.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    return target


def say_ok(
    *,
    stream=None,
    label: str = "vendor_spec",
) -> None:
    """在 CLI 成功路径结尾打印一行「OK — <label> 校验通过」。

    默认写 stdout；可通过 ``stream`` 注入测试替身。
    """

    out = stream if stream is not None else sys.stdout
    out.write(f"OK — {label} 校验通过（启动期 fail-fast 已放过，未发起任何网络请求）\n")
    try:
        out.flush()
    except Exception:  # pragma: no cover - 容错
        pass


__all__ = [
    "DEFAULT_VALIDATION_LOG",
    "dump_validation_errors",
    "say_ok",
    "validate_raw",
    "validate_vendor_spec",
]

# 兼容 ``log_path`` 默认值读取
DEFAULT_VALIDATION_LOG = _DEFAULT_VALIDATION_LOG
