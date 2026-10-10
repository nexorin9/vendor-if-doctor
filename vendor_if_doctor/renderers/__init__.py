"""vendor-if-doctor 渲染器（renderers）模块。

把 :class:`~vendor_if_doctor.connection.evidence.Evidence` 列表落成值班员可读产物：

* :func:`render_handover` — Markdown 值班交接清单（按 vendor 分组 + healthy /
  degraded / failed 三态 + 首次失败步骤提示 + 证据行 hash 前 8 位）。写文件路径
  由 :envvar:`VENDOR_DOCTOR_HANDOVER_DIR` 控制，默认 ``out/<date>/handover.md``。
* :func:`render_tickets` — JSONL 工单草稿（每个 failed / degraded step 一行；
  含 ``first_failed_step`` / ``suggested_actions`` / ``evidence_hash``），值班员人审
  后粘入工单系统，**不**直连工单 API。写文件路径由 :envvar:`VENDOR_DOCTOR_TICKETS_DIR`
  控制，默认 ``out/<date>/tickets.jsonl``。

凭据纪律（最关键）
-------------------

* 渲染前 Evidence 已自带 ``_coarse_redact`` 安全网，本模块再走一道字典级
  :func:`vendor_if_doctor.redact.redact_dict` 兜底（task 18 才正式实现）；当前
  优先依赖 Evidence 自身的 redact 与本模块的 inline 过滤。
* Authorization / Bearer / X-API-Key / token / password / api_key 等字段名
  匹配一律替换为 ``***``；apikey query 注入的 URL 参数 ``api_key=...``
  单独处理。
* 全部产物落盘前会在内存再过一遍字符串过滤；grep ``token`` / ``password``
  / ``Authorization`` 在交付物里命中数 = 0。

未实现协议
----------

``status='skip'`` ``failure_class='not_implemented'`` 的 evidence **不**进入工单
草稿（避免污染值班员工单系统），但在 handover.md 里仍以``### Status: skipped``
小节呈现，便于看到"还没接的"清单。
"""
from __future__ import annotations

from vendor_if_doctor.renderers.handover_md import (
    render_handover,
    write_handover,
    handover_path,
)
from vendor_if_doctor.renderers.ticket_jsonl import (
    priority_of,
    render_tickets,
    severity_of,
    suggested_actions_for,
    tickets_path,
    write_tickets,
)
from vendor_if_doctor.connection.suggestions import (
    PLACEHOLDER_KEYS as _PLACEHOLDER_KEYS,
    PLACEHOLDER_KEYS_OPTIONAL as _PLACEHOLDER_KEYS_OPTIONAL,
    PLACEHOLDER_KEYS_REQUIRED as _PLACEHOLDER_KEYS_REQUIRED,
    auth_types_for_auth_failed as _auth_types_for_auth_failed,
    list_supported_failure_classes as _list_supported_failure_classes,
    placeholder_keys as _placeholder_keys,
    suggest_actions as suggest_actions,
)
from vendor_if_doctor.renderers.sqlite_history import (
    connect,
    count_history,
    db_path,
    ensure_partition,
    ensure_schema,
    fetch_run,
    fetch_runs_by_vendor,
    partition_table_name,
    query_history,
    record_run,
    write_history,
)
from vendor_if_doctor.renderers.weekly_html import (
    SystemAgg,
    WeeklyReport,
    aggregate_recent_runs,
    fetch_recent_runs,
    render_weekly_html,
    weekly_html_path,
    write_weekly_html,
)

__all__ = [
    "PLACEHOLDER_KEYS",
    "PLACEHOLDER_KEYS_OPTIONAL",
    "PLACEHOLDER_KEYS_REQUIRED",
    "SystemAgg",
    "WeeklyReport",
    "aggregate_recent_runs",
    "auth_types_for_auth_failed",
    "connect",
    "count_history",
    "db_path",
    "ensure_partition",
    "ensure_schema",
    "fetch_recent_runs",
    "fetch_run",
    "fetch_runs_by_vendor",
    "handover_path",
    "list_supported_failure_classes",
    "partition_table_name",
    "placeholder_keys",
    "priority_of",
    "query_history",
    "record_run",
    "render_handover",
    "render_tickets",
    "render_weekly_html",
    "severity_of",
    "suggest_actions",
    "suggested_actions_for",
    "tickets_path",
    "weekly_html_path",
    "write_history",
    "write_handover",
    "write_tickets",
    "write_weekly_html",
]
