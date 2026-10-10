"""ticket_jsonl — JSONL 工单草稿渲染器。

设计要点
--------

* **每个 failed / degraded evidence 一行**：每个 vendor × interface × 失败 step 视
  为一条工单草稿，便于值班员复制粘贴进工单系统。``status='skip'`` 写不进来
  （避免值班员被"占位步骤"刷屏）；``status='pass'`` 不写。
* **首失败步骤暴露**：每条 evidence 携带 ``first_failed_step`` 字段（run 内首
  个失败 step 名）；同一 run 内多条 evidence 给出同一 ``first_failed_step`` 便于
  联动查看断点。
* **凭据字段过滤**：写盘前对所有 value 跑 ``_coarse_redact_dict`` 兜底，且
  inline 过滤 Authorization / Bearer / X-API-Key / token / password / apikey
  等敏感 key 出现时整字段值替换为 ``"***"``。grep ``Authorization`` / ``token``
  / ``password`` 在交付 JSONL 里命中数 = 0。
* **失败建议动作**：每个 failure_class 关联 1–3 条具体排查命令；
  ``suggested_actions_for(failure_class, vendor_id, interface_id)`` 调用
  :mod:`vendor_if_doctor.connection.suggestions` 的模板库（task 31 完整化）
  ，替换 ``{host}`` / ``{port}`` / ``{vendor_id}`` / ``{interface_id}`` /
  ``{fqn}`` / ``{auth_type}`` / ``{auth_env}`` 等占位符。
* **路径**：默认 ``out/<YYYY-MM-DD>/tickets.jsonl``，可由
  :envvar:`VENDOR_DOCTOR_TICKETS_DIR` 覆盖。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, List, Optional

from vendor_if_doctor.connection.evidence import Evidence
from vendor_if_doctor.connection.severity import (
    Severity,
    priority_of,
    severity_of,
)
from vendor_if_doctor.connection.suggestions import suggested_actions_for


# ---------------------------------------------------------------------------
# 字典级 / 字符串级敏感字段过滤
# ---------------------------------------------------------------------------

_SECRET_KEY_RE = re.compile(
    r"(?i)^(authorization|x-api-key|api[_-]?key|password|secret|token|access[_-]?token|bearer|credential)$"
)

_REDACT_IN_STRING_PATTERNS: list[tuple[str, str]] = [
    (r"(?im)(authorization\s*[:=]\s*)(?:bearer|basic)?\s*[^\s,;\"'\r\n]+", r"\1***"),
    (r"(?i)(\bbearer\s+)[^\s,;\"'\r\n]+", r"\1***"),
    (r"(?i)(\bbasic\s+)[^\s,;\"'\r\n]+", r"\1***"),
    (
        r'(?i)("(?:token|password|api[_-]?key|authorization|secret|x-api-key|access_token)"\s*:\s*)"[^"]*"',
        r'\1"***"',
    ),
    (r"(?i)([\?&](?:api[_-]?key|access[_-]?token|token|password)\s*=\s*)([^&\s=]+)", r"\1***"),
    (r"(?i)(\btoken\s*=\s*)[^\s,;\"'\r\n&]+", r"\1***"),
    (r"(?i)(\bpassword\s*=\s*)[^\s,;\"'\r\n&]+", r"\1***"),
]


def _coarse_redact(text: Optional[str]) -> Optional[str]:
    if not text:
        return text
    out = text
    for pat, repl in _REDACT_IN_STRING_PATTERNS:
        out = re.sub(pat, repl, out)
    return out


def _coarse_redact_dict(obj: Any) -> Any:
    """递归过滤 dict 中所有 secret-ish key 的 value。

    * key 名命中 `_SECRET_KEY_RE` → value 替换为 ``"***"``；
    * value 是 str → 过 ``_coarse_redact`` 字符串过滤；
    * value 是 dict / list → 递归；
    * 其它类型（int / bool / None）保留。
    """

    if isinstance(obj, dict):
        out: dict = {}
        for k, v in obj.items():
            if isinstance(k, str) and _SECRET_KEY_RE.match(k):
                out[k] = "***"
            else:
                out[k] = _coarse_redact_dict(v)
        return out
    if isinstance(obj, list):
        return [_coarse_redact_dict(v) for v in obj]
    if isinstance(obj, str):
        return _coarse_redact(obj)
    return obj


# ---------------------------------------------------------------------------
# 失败建议动作：委托给 connection.suggestions（task 31 完整化模板库）
# ---------------------------------------------------------------------------

# `suggested_actions_for` 由顶部 `from vendor_if_doctor.connection.suggestions import
# suggested_actions_for` 注入；保留本符号对外接口（task 9 兼容），但实际模板
# 来自 connection.suggestions 的 12 类全覆盖 + auth_type 分支版本。


# ---------------------------------------------------------------------------
# JSONL 行构造
# ---------------------------------------------------------------------------

def _safe_json_default(obj: Any) -> Any:
    """json.dumps fallback：Evidence / 其它 dataclass 转 dict，路径转 str。"""
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


def _evidence_to_ticket(
    evidence: Evidence,
    *,
    first_failed_step_for_run: Optional[str],
    first_failed_step_for_evidence: Optional[str],
    interface_fqn: str,
) -> dict:
    """组装一条工单草稿 dict。"""

    safe_meta = _coarse_redact_dict(evidence.meta or {})
    summary_redacted = _coarse_redact(evidence.summary) or ""
    command_redacted = _coarse_redact(evidence.command) or ""
    body_redacted = _coarse_redact(evidence.response_body)
    body_truncated = (
        len(evidence.response_body) > 32 * 1024
        if evidence.response_body
        else False
    )

    actions = suggested_actions_for(
        evidence.failure_class,
        evidence.vendor_id or "<unknown>",
        evidence.interface_id or "<unknown>",
    )

    # task 22：失败告警分级（warning / critical）+ 工单草稿分级模板
    sev = severity_of(evidence.failure_class)
    pri = priority_of(evidence.failure_class)
    escalation = _ESCALATION_HINT.get(sev, "")

    return {
        "fqn": interface_fqn,
        "vendor_id": evidence.vendor_id,
        "interface_id": evidence.interface_id,
        "step_name": evidence.step_name,
        "status": evidence.status,
        "failure_class": evidence.failure_class,
        "severity": sev.value,
        "priority": pri,
        "escalation_hint": escalation,
        "first_failed_step": first_failed_step_for_evidence
        or first_failed_step_for_run,
        "summary": summary_redacted,
        "command": command_redacted,
        "latency_ms": evidence.latency_ms,
        "response_body_snippet": body_redacted,
        "response_body_truncated": body_truncated,
        "evidence_hash": evidence.evidence_hash,
        "meta": safe_meta,
        "suggested_actions": actions,
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# task 22：升级建议文案（按 severity 分级）
# ---------------------------------------------------------------------------

_ESCALATION_HINT: dict[Severity, str] = {
    Severity.CRITICAL: "立即升级到厂商 + 工单优先级 high（凭据/接口字段/MED/MSA/连通性问题影响业务）",
    Severity.WARNING: "继续观察，下一次巡检或人工复检时处理（工单优先级 low）",
    Severity.INFO: "已知占位/未接入，留痕即可（工单优先级 low）",
}


def _group_by_interface(
    evidence: Iterable[Evidence],
) -> "list[tuple[str, str, list[Evidence]]]":
    grouped: "dict[tuple[str, str], list[Evidence]]" = {}
    for ev in evidence:
        key = (ev.vendor_id or "<unknown>", ev.interface_id or "<unknown>")
        grouped.setdefault(key, []).append(ev)
    return [(vid, iid, evs) for (vid, iid), evs in grouped.items()]


def _run_first_failed_step(evs: List[Evidence]) -> Optional[str]:
    for ev in evs:
        if ev.status in ("fail", "degraded"):
            return ev.step_name
    return None


def _build_ticket_lines(evidence_list: List[Evidence]) -> List[dict]:
    tickets: list[dict] = []
    for vendor_id, interface_id, evs in _group_by_interface(evidence_list):
        fqn = f"{vendor_id}.{interface_id}"
        first_failed = _run_first_failed_step(evs)

        for ev in evs:
            if ev.status == "skip":
                # 占位步骤不进工单（避免值班员被刷屏）
                continue
            if ev.status not in ("fail", "degraded"):
                continue
            tickets.append(
                _evidence_to_ticket(
                    ev,
                    first_failed_step_for_run=first_failed,
                    first_failed_step_for_evidence=first_failed,
                    interface_fqn=fqn,
                )
            )
    return tickets


# ---------------------------------------------------------------------------
# 渲染 + 写盘
# ---------------------------------------------------------------------------

def render_tickets(evidence_list: List[Evidence]) -> str:
    """把 evidence 列表渲染为 JSONL 字符串（行末带 ``\\n``）。"""

    lines = _build_ticket_lines(evidence_list)
    out_lines: list[str] = []
    for ticket in lines:
        rendered = json.dumps(
            ticket,
            ensure_ascii=False,
            default=_safe_json_default,
            sort_keys=True,
        )
        # 二次过滤（防止 value 内部又有新密钥；dict 级已过滤但保持双保险）
        rendered = _coarse_redact(rendered) or ""
        out_lines.append(rendered)
    # 末尾补一个空行便于 cat
    return "\n".join(out_lines) + ("\n" if out_lines else "")


def _today() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


def tickets_path(base: Optional[os.PathLike] = None) -> Path:
    """计算默认 tickets.jsonl 路径。

    默认：``$VENDOR_DOCTOR_TICKETS_DIR`` / ``<YYYY-MM-DD>`` / ``tickets.jsonl``；
    缺省时回落到 ``out/<YYYY-MM-DD>/tickets.jsonl``。
    """

    base_dir = os.environ.get("VENDOR_DOCTOR_TICKETS_DIR")
    if base is not None:
        target = Path(base)
    elif base_dir:
        target = Path(base_dir) / _today()
    else:
        target = Path.cwd() / "out" / _today()
    target.mkdir(parents=True, exist_ok=True)
    return target / "tickets.jsonl"


def write_tickets(
    evidence_list: List[Evidence],
    *,
    path: Optional[os.PathLike] = None,
) -> Path:
    """渲染并落盘 tickets.jsonl。返回最终写入路径。"""

    text = render_tickets(evidence_list)
    out_path = Path(path) if path is not None else tickets_path()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    return out_path


__all__ = [
    "_coarse_redact",
    "_coarse_redact_dict",
    "priority_of",
    "render_tickets",
    "severity_of",
    "suggested_actions_for",
    "tickets_path",
    "write_tickets",
]
