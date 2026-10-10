"""handover_md — Markdown 值班交接清单渲染器。

设计要点
--------

* **三态 + 占位**：每个 vendor + interface 视为一条 run；其 aggregate status 来自
  evidence 列表：

  * ``healthy`` — 所有 step 都通过；
  * ``degraded`` — 有 step 通过但有 ``status='degraded'`` 或 ``failure_class``
    属 warning 级别（如 ``payload_empty`` / ``schema_mismatch``）；
  * ``failed`` — 有 step 出现 ``status='fail'``（任意 failure_class）；
  * ``skipped`` — 所有 step 都 ``status='skip'``（如 ``not_implemented``
    占位协议）。

* **首次失败步骤可见**：run 内首个 ``status='fail'`` 的 step 名出现在
  ``run.first_failed_step``，便于值班员一眼定位断点。
* **凭据过滤兜底**：渲染前所有 evidence 字段再过一道字符串级 regex 过滤；
  这一层是 evidence 已有 ``_coarse_redact`` 之外的二次防御，**不会**写入
  Authorization / Bearer / X-API-Key / token / password 等字段值。
* **环境变量控制产物路径**：默认 ``out/<YYYY-MM-DD>/handover.md``，可由
  :envvar:`VENDOR_DOCTOR_HANDOVER_DIR` 覆盖。
"""
from __future__ import annotations

import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Mapping, Optional

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from vendor_if_doctor.connection.evidence import Evidence


# ---------------------------------------------------------------------------
# 敏感字段字符串过滤（兜底）—— 与 runner._coarse_redact 保持语义一致
# ---------------------------------------------------------------------------

_REDACT_PATTERNS: list[tuple[str, str]] = [
    # 行内: `Authorization: Bearer xxx` / `Authorization: Basic xxx`
    (r"(?im)(authorization\s*[:=]\s*)(?:bearer|basic)?\s*[^\s,;\"'\r\n]+", r"\1***"),
    # `Bearer xyz` / `Basic xyz`
    (r"(?i)(\bbearer\s+)[^\s,;\"'\r\n]+", r"\1***"),
    (r"(?i)(\bbasic\s+)[^\s,;\"'\r\n]+", r"\1***"),
    # JSON: `"token": "abc"` / `"password": "abc"` 等
    (
        r'(?i)("(?:token|password|api[_-]?key|authorization|secret|x-api-key)"\s*:\s*)"[^"]*"',
        r'\1"***"',
    ),
    # URL query: `?api_key=xxx` / `&token=xxx` / `&password=xxx`
    (r"(?i)([\?&](?:api[_-]?key|access[_-]?token|token|password)\s*=\s*)([^&\s=]+)", r"\1***"),
    # 标量赋值: `token=xxx` `password=xxx` (limited by `&` to avoid吞掉后跟参数)
    (r"(?i)(\btoken\s*=\s*)[^\s,;\"'\r\n&]+", r"\1***"),
    (r"(?i)(\bpassword\s*=\s*)[^\s,;\"'\r\n&]+", r"\1***"),
]


def _coarse_redact(text: Optional[str]) -> Optional[str]:
    if not text:
        return text
    out = text
    for pat, repl in _REDACT_PATTERNS:
        out = re.sub(pat, repl, out)
    return out


# ---------------------------------------------------------------------------
# run 聚合（按 vendor + interface）
# ---------------------------------------------------------------------------

_STATUS_PRIORITY = {
    "failed": 0,
    "degraded": 1,
    "healthy": 2,
    "skipped": 3,
}


def _classify_run_status(evidence: List[Evidence]) -> str:
    if not evidence:
        return "skipped"

    statuses = [ev.status for ev in evidence]
    failure_classes = [ev.failure_class for ev in evidence if ev.failure_class]

    if any(s == "fail" for s in statuses):
        return "failed"
    if any(s == "degraded" for s in statuses):
        return "degraded"
    if any(s == "skip" for s in statuses) and any(s == "pass" for s in statuses):
        # 5 步里同时有 pass 和 skip：可能是真正部分跑通（如 list 步骤未配置）
        return "degraded"
    if all(s == "skip" for s in statuses):
        return "skipped"
    return "healthy"


def _first_failed_step(evidence: List[Evidence]) -> Optional[str]:
    for ev in evidence:
        if ev.status == "fail" or ev.status == "degraded":
            return ev.step_name
    return None


def _first_failure_class(evidence: List[Evidence]) -> Optional[str]:
    for ev in evidence:
        if ev.status == "fail" or ev.status == "degraded":
            return ev.failure_class
    return None


def _group_by_interface(
    evidence: Iterable[Evidence],
) -> "dict[tuple[str, str], list[Evidence]]":
    grouped: dict[tuple[str, str], list[Evidence]] = {}
    for ev in evidence:
        key = (ev.vendor_id or "<unknown>", ev.interface_id or "<unknown>")
        grouped.setdefault(key, []).append(ev)
    # 同一 interface 内 step 顺序保留插入顺序（runner 已按流水线顺序插入）
    return grouped


def _response_snippet(evidence: Evidence, limit: int = 240) -> str:
    body = evidence.response_body or ""
    body = _coarse_redact(body) or ""
    if len(body) > limit:
        return body[: limit - 1] + "…"
    return body


def _build_runs(evidence_list: List[Evidence]) -> list[dict]:
    grouped = _group_by_interface(evidence_list)

    # 懒导入避免循环：suggested_actions_for 在 ticket_jsonl 内
    from vendor_if_doctor.renderers.ticket_jsonl import suggested_actions_for

    runs: list[dict] = []
    for (vendor_id, interface_id), evs in grouped.items():
        fqn = (
            evs[0].meta.get("fqn")
            if evs and isinstance(evs[0].meta, dict)
            else f"{vendor_id}.{interface_id}"
        )
        if not fqn:
            fqn = f"{vendor_id}.{interface_id}"

        status = _classify_run_status(evs)
        first_failed = _first_failed_step(evs)
        first_fc = _first_failure_class(evs)
        total_latency = sum(max(0, ev.latency_ms) for ev in evs)

        # 呈现型 evidence（去除 raw 大对象，留摘要）
        view_evidence: list[dict] = []
        for ev in evs:
            view_evidence.append(
                {
                    "summary": _coarse_redact(ev.summary) or "",
                    "command": _coarse_redact(ev.command) or "",
                    "latency_ms": ev.latency_ms,
                    "status": ev.status,
                    "failure_class": ev.failure_class,
                    "evidence_hash": ev.evidence_hash,
                    "step_name": ev.step_name,
                    "response_body_snippet": _response_snippet(ev),
                    "meta": {
                        k: _coarse_redact(str(v)) if isinstance(v, str) else v
                        for k, v in (ev.meta or {}).items()
                    },
                }
            )

        actions: list[str] = []
        if first_fc:
            try:
                actions = suggested_actions_for(
                    first_fc, vendor_id, interface_id
                )
            except Exception:  # pragma: no cover - defensive
                actions = []

        runs.append(
            {
                "vendor_id": vendor_id,
                "interface_id": interface_id,
                "fqn": fqn,
                "status": status,
                "first_failed_step": first_failed,
                "failure_class": first_fc,
                "total_latency_ms": total_latency,
                "evidence": view_evidence,
                "suggested_actions": actions,
            }
        )

    # 排序：失败优先，其次同一 status 内按 vendor_id / interface_id 升序
    runs.sort(
        key=lambda r: (
            _STATUS_PRIORITY.get(r["status"], 99),
            r["vendor_id"],
            r["interface_id"],
        )
    )
    return runs


# ---------------------------------------------------------------------------
# 模板加载 / 渲染
# ---------------------------------------------------------------------------

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def _load_template():
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        undefined=StrictUndefined,
        autoescape=select_autoescape(enabled_extensions=("j2",), default=False),
        keep_trailing_newline=True,
        trim_blocks=False,
        lstrip_blocks=False,
    )
    return env.get_template("handover.md.j2")


def _counts(runs: list[dict]) -> dict[str, int]:
    c: Counter[str] = Counter()
    for r in runs:
        c[r["status"]] += 1
    return {
        "healthy_count": c.get("healthy", 0),
        "degraded_count": c.get("degraded", 0),
        "failed_count": c.get("failed", 0),
        "skipped_count": c.get("skipped", 0),
    }


def render_handover(
    evidence_list: List[Evidence],
    *,
    generated_at: Optional[str] = None,
    extra_meta: Optional[Mapping[str, str]] = None,
) -> str:
    """渲染 Markdown 值班交接清单。

    Parameters
    ----------
    evidence_list : list[Evidence]
        :func:`vendor_if_doctor.connection.runner.test_connection` 返回的全部
        evidence 列表；会按 ``(vendor_id, interface_id)`` 聚合为 run。
    generated_at : str, optional
        ISO8601 字符串，默认当前 UTC 时间。
    extra_meta : dict, optional
        预留字段（任务 11 CLI 注入：检测模式 / 配置名 / 触发人等），暂不展示。

    Returns
    -------
    str
        完整 Markdown 文本；调用方负责写盘。
    """

    if generated_at is None:
        generated_at = (
            datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        )

    runs = _build_runs(evidence_list)
    counts = _counts(runs)

    template = _load_template()
    rendered = template.render(
        generated_at=generated_at,
        total_runs=len(runs),
        runs=runs,
        **counts,
    )
    # 二次脱敏防御（防止模板里引入新敏感字段）
    return _coarse_redact(rendered) or ""


# ---------------------------------------------------------------------------
# 路径解析 + 写盘
# ---------------------------------------------------------------------------

def _today() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


def handover_path(base: Optional[os.PathLike] = None) -> Path:
    """计算默认 handover.md 写入路径。

    默认：``$VENDOR_DOCTOR_HANDOVER_DIR`` / ``<YYYY-MM-DD>`` / ``handover.md``；
    缺省时回落到运行目录的 ``out/<YYYY-MM-DD>/handover.md``。
    """

    base_dir = os.environ.get("VENDOR_DOCTOR_HANDOVER_DIR")
    if base is not None:
        target = Path(base)
    elif base_dir:
        target = Path(base_dir) / _today()
    else:
        target = Path.cwd() / "out" / _today()
    target.mkdir(parents=True, exist_ok=True)
    return target / "handover.md"


def write_handover(
    evidence_list: List[Evidence],
    *,
    path: Optional[os.PathLike] = None,
    generated_at: Optional[str] = None,
) -> Path:
    """渲染并落盘。返回最终写入路径。"""

    text = render_handover(
        evidence_list,
        generated_at=generated_at,
    )
    out_path = Path(path) if path is not None else handover_path()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    return out_path


__all__ = [
    "_coarse_redact",
    "_group_by_interface",
    "_classify_run_status",
    "_first_failed_step",
    "_build_runs",
    "render_handover",
    "write_handover",
    "handover_path",
]
