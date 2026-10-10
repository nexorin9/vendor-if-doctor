"""severity — 失败告警分级（critical / warning / info）。

设计要点
--------

* **三档分级**：critical（需立即处理） / warning（继续观察） / info（已知占位）
* **映射规则**：

  ============  ============  ===========================================
  failure_class severity       工单草稿 priority
  ============  ============  ===========================================
  auth_failed     critical     high（凭据问题立即升级）
  schema_mismatch critical     high（接口字段不匹配需厂商核对）
  hl7_ack_re      critical     high（HL7 MSA-1 拒绝立即升级）
  timeout         critical     high（连通性问题需排查网络/防火墙）
  tcp_unreachable critical     high（TCP 不可达需立即排查）
  tcp_half_closed critical     high（半关闭需联系厂商）
  http_status     critical     high（HTTP 4xx/5xx 非鉴权失败）
  payload_empty   warning      low（响应空体继续观察）
  degraded_slow   warning      low（响应慢但可用继续观察）
  encoding_error  warning      low（编码错误继续观察）
  hl7_ack_missing warning      low（ACK 缺失，可能为偶发）
  not_implemented info         low（占位 / 待接入）
  ============  ============  ===========================================

* **可扩展**：failure_class 字符串不在表中时默认 warning（保守兜底，避免 silent
  漏报 critical）；调用方可显式提供 ``_warn_unknown`` 开关以控制是否日志警告。
* **优先级映射**：

  - critical → ``"high"``（值班员立即处理，可能涉及停诊/对账/质控）
  - warning → ``"low"``（继续观察，下一次巡检或人工复检时处理）
  - info    → ``"low"``（无需处理，仅在工单系统留痕）

* **凭据纪律**：本模块**不**读取 Evidence.body / response_body / meta 中的凭据字段，
  仅消费 ``evidence.failure_class`` 字符串值；调用方可放心用于自动告警通道。

调用方约定
----------

* :func:`severity_of(failure_class)` → :class:`Severity` 枚举值；
* :func:`priority_of(failure_class)` → ``"high"`` / ``"low"`` 字符串（工单 priority 字段）；
* :func:`severity_to_priority(severity)` → 与 ``priority_of`` 等价，便于模板分支判断；
* :func:`all_severities()` → 已知 severity 列表（健康检查 / 文档用）；
* :func:`is_critical(failure_class)` / :func:`is_warning(failure_class)` → 布尔便捷接口。

示例：

>>> severity_of("auth_failed").value
'critical'
>>> priority_of("payload_empty")
'low'
>>> is_critical("timeout")
True
>>> severity_of("totally_new_class").value
'warning'  # 未知 failure_class 默认 warning（保守）
"""

from __future__ import annotations

from enum import Enum
from typing import Optional


class Severity(str, Enum):
    """失败告警分级枚举（与 Phase 1 工单草稿 priority 字段对齐）。"""

    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


# ---------------------------------------------------------------------------
# 失败语义 → severity 映射表（单一真源）
# ---------------------------------------------------------------------------

_FAILURE_TO_SEVERITY: dict[str, Severity] = {
    # ---- critical：立即升级 ----
    "auth_failed": Severity.CRITICAL,
    "schema_mismatch": Severity.CRITICAL,
    "hl7_ack_re": Severity.CRITICAL,
    "timeout": Severity.CRITICAL,
    "tcp_unreachable": Severity.CRITICAL,
    "tcp_half_closed": Severity.CRITICAL,
    "http_status": Severity.CRITICAL,
    # ---- warning：继续观察 ----
    "payload_empty": Severity.WARNING,
    "degraded_slow": Severity.WARNING,
    "encoding_error": Severity.WARNING,
    "hl7_ack_missing": Severity.WARNING,
    # ---- info：已知占位 ----
    "not_implemented": Severity.INFO,
}


_SEVERITY_TO_PRIORITY: dict[Severity, str] = {
    Severity.CRITICAL: "high",
    Severity.WARNING: "low",
    Severity.INFO: "low",
}


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------


def severity_of(failure_class: Optional[str]) -> Severity:
    """根据 ``failure_class`` 返回 :class:`Severity` 枚举值。

    未知 / 空字符串 / ``None`` → 默认 :data:`Severity.WARNING`（保守兜底，避免
    silent 漏报 critical；同时也不把 unknown 推到 critical，避免误升级）。
    """

    if not failure_class:
        return Severity.WARNING
    return _FAILURE_TO_SEVERITY.get(failure_class, Severity.WARNING)


def priority_of(failure_class: Optional[str]) -> str:
    """根据 ``failure_class`` 返回工单草稿 ``priority`` 字段字符串。

    - critical → ``"high"``
    - warning / info / unknown → ``"low"``
    """

    return _SEVERITY_TO_PRIORITY[severity_of(failure_class)]


def severity_to_priority(severity: Severity) -> str:
    """显式 :class:`Severity` → priority 字符串映射（便于模板分支直接判断）。"""

    return _SEVERITY_TO_PRIORITY[severity]


def is_critical(failure_class: Optional[str]) -> bool:
    """判断 ``failure_class`` 是否映射到 critical 级别。"""

    return severity_of(failure_class) == Severity.CRITICAL


def is_warning(failure_class: Optional[str]) -> bool:
    """判断 ``failure_class`` 是否映射到 warning 级别。"""

    return severity_of(failure_class) == Severity.WARNING


def all_severities() -> list[str]:
    """返回全部已知 severity 字符串列表（按字母升序）。"""

    return sorted(s.value for s in Severity)


def all_severity_mappings() -> dict[str, str]:
    """返回 ``failure_class → severity`` 完整映射字典（测试 / 文档用）。"""

    return {fc: sev.value for fc, sev in _FAILURE_TO_SEVERITY.items()}


__all__ = [
    "Severity",
    "all_severities",
    "all_severity_mappings",
    "is_critical",
    "is_warning",
    "priority_of",
    "severity_of",
    "severity_to_priority",
]
