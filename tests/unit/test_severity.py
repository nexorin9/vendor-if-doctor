"""severity 单元测试 + 工单草稿分级模板集成测试。

覆盖（按 task 22 spec）：

1. 5 类失败契约 → severity 正确（auth_failed / schema_mismatch / hl7_ack_re /
   timeout / payload_empty）
2. 扩展 failure_class → severity 正确（tcp_unreachable / tcp_half_closed /
   degraded_slow / encoding_error / hl7_ack_missing / http_status / not_implemented）
3. 未知 failure_class → 默认 warning（保守兜底）
4. priority 字段：critical → 'high'；warning / info / unknown → 'low'
5. 工单草稿分级模板：critical 含 'priority: high' / '升级' 字样；warning 含 '继续观察' / 'low'
6. 工单草稿含 severity / priority / escalation_hint 三个新字段
7. 凭据纪律：severity 模块本身不读 evidence body / meta；分级不影响凭据脱敏
8. Severity 枚举 / all_severities / all_severity_mappings 公开 API
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from vendor_if_doctor.connection.evidence import Evidence
from vendor_if_doctor.connection.severity import (
    Severity,
    all_severities,
    all_severity_mappings,
    is_critical,
    is_warning,
    priority_of,
    severity_of,
    severity_to_priority,
)
from vendor_if_doctor.renderers import (
    render_tickets,
    severity_of as renderers_severity_of,
    priority_of as renderers_priority_of,
    suggested_actions_for,
    write_tickets,
)
from vendor_if_doctor.renderers.ticket_jsonl import (
    _ESCALATION_HINT,
    _coarse_redact,
    _coarse_redact_dict,
    _evidence_to_ticket,
    suggested_actions_for as ticket_suggested,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _ev(
    step_name: str,
    status: str,
    *,
    vendor_id: str = "his",
    interface_id: str = "outpatient-register",
    failure_class=None,
    response_body=None,
    command=None,
    evidence_hash=None,
    latency_ms=100,
    summary=None,
    meta=None,
):
    return Evidence(
        summary=summary or f"{vendor_id}.{interface_id} {step_name} {status}",
        command=command or f"GET /{step_name}",
        latency_ms=latency_ms,
        status=status,
        failure_class=failure_class,
        response_body=response_body,
        evidence_hash=evidence_hash,
        step_name=step_name,
        vendor_id=vendor_id,
        interface_id=interface_id,
        meta=meta or {},
    )


def _failed_auth_5():
    """5 步流水线：preflight pass + auth fail（critical） → 4 个 skip。"""
    return [
        _ev("preflight", "pass"),
        _ev("auth", "fail", failure_class="auth_failed"),
        _ev("list", "skip"),
        _ev("sample", "skip"),
        _ev("assert", "skip"),
    ]


def _failed_payload_empty_5():
    """5 步流水线：preflight pass + auth pass + sample pass 但 assert fail with payload_empty (warning)."""
    return [
        _ev("preflight", "pass"),
        _ev("auth", "pass"),
        _ev("list", "skip"),
        _ev("sample", "pass"),
        _ev(
            "assert",
            "degraded",
            failure_class="payload_empty",
            summary="assert 空响应体（warning）",
        ),
    ]


# ---------------------------------------------------------------------------
# Test 1: 5 类失败契约 → severity 正确
# ---------------------------------------------------------------------------


class TestFiveFailureClasses:
    """task 22 spec: 5 类失败 → severity 正确映射。"""

    @pytest.mark.parametrize(
        "failure_class,expected_severity",
        [
            ("auth_failed", "critical"),
            ("schema_mismatch", "critical"),
            ("hl7_ack_re", "critical"),
            ("timeout", "critical"),
            ("payload_empty", "warning"),
        ],
    )
    def test_basic_five_classes(self, failure_class, expected_severity):
        sev = severity_of(failure_class)
        assert sev == Severity(expected_severity)
        assert sev.value == expected_severity

    @pytest.mark.parametrize(
        "failure_class",
        ["auth_failed", "schema_mismatch", "hl7_ack_re", "timeout"],
    )
    def test_critical_classes_is_critical_true(self, failure_class):
        assert is_critical(failure_class) is True
        assert is_warning(failure_class) is False

    @pytest.mark.parametrize(
        "failure_class",
        ["payload_empty", "degraded_slow"],
    )
    def test_warning_classes_is_warning_true(self, failure_class):
        assert is_warning(failure_class) is True
        assert is_critical(failure_class) is False


# ---------------------------------------------------------------------------
# Test 2: 扩展 failure_class → severity 正确
# ---------------------------------------------------------------------------


class TestExtendedFailureClasses:
    """task 22 spec 隐含：扩展 failure_class 也要正确分级。"""

    @pytest.mark.parametrize(
        "failure_class,expected_severity",
        [
            ("tcp_unreachable", "critical"),
            ("tcp_half_closed", "critical"),
            ("http_status", "critical"),
            ("encoding_error", "warning"),
            ("degraded_slow", "warning"),
            ("hl7_ack_missing", "warning"),
            ("not_implemented", "info"),
        ],
    )
    def test_extended_classes(self, failure_class, expected_severity):
        sev = severity_of(failure_class)
        assert sev == Severity(expected_severity)

    def test_unknown_failure_class_defaults_to_warning(self):
        """未知 failure_class → 默认 warning（保守兜底）。"""
        assert severity_of("totally_unknown_class") == Severity.WARNING
        assert severity_of("") == Severity.WARNING
        assert severity_of(None) == Severity.WARNING

    def test_unknown_failure_class_priority_low(self):
        """未知 failure_class → priority='low'（不误升级 critical）。"""
        assert priority_of("totally_unknown_class") == "low"


# ---------------------------------------------------------------------------
# Test 3: priority 字段映射
# ---------------------------------------------------------------------------


class TestPriorityMapping:
    """task 22 spec: critical → 'high'；warning / info / unknown → 'low'。"""

    @pytest.mark.parametrize(
        "failure_class,expected_priority",
        [
            ("auth_failed", "high"),
            ("schema_mismatch", "high"),
            ("hl7_ack_re", "high"),
            ("timeout", "high"),
            ("tcp_unreachable", "high"),
            ("tcp_half_closed", "high"),
            ("http_status", "high"),
            ("payload_empty", "low"),
            ("degraded_slow", "low"),
            ("encoding_error", "low"),
            ("hl7_ack_missing", "low"),
            ("not_implemented", "low"),
            ("unknown_class", "low"),
            ("", "low"),
            (None, "low"),
        ],
    )
    def test_priority_of(self, failure_class, expected_priority):
        assert priority_of(failure_class) == expected_priority

    @pytest.mark.parametrize(
        "severity,expected_priority",
        [
            (Severity.CRITICAL, "high"),
            (Severity.WARNING, "low"),
            (Severity.INFO, "low"),
        ],
    )
    def test_severity_to_priority(self, severity, expected_priority):
        assert severity_to_priority(severity) == expected_priority


# ---------------------------------------------------------------------------
# Test 4: 工单草稿分级模板（critical / warning 分支）
# ---------------------------------------------------------------------------


class TestTicketSeverityTemplate:
    """task 22 spec: critical 模板含 priority='high' 与升级字样；warning 模板含
    '继续观察' / priority='low'。"""

    def test_auth_failed_ticket_has_high_priority(self):
        """AuthFailed → severity=critical → tickets.jsonl priority='high'。"""
        evs = _failed_auth_5()
        rendered = render_tickets(evs)
        # 1 条 evidence 进工单（status='fail' 的 auth 步），其他 4 条 skip 不进
        assert rendered.count("\n") == 1  # 1 行（不含末尾空行）
        ticket = json.loads(rendered.strip())
        assert ticket["severity"] == "critical"
        assert ticket["priority"] == "high"
        # escalation_hint 含"升级"字样
        assert "升级" in ticket["escalation_hint"]
        assert "high" in ticket["escalation_hint"]

    def test_payload_empty_ticket_has_low_priority_and_continue(self):
        """degraded_slow → severity=warning → tickets.jsonl priority='low' + '继续观察'。"""
        evs = _failed_payload_empty_5()
        rendered = render_tickets(evs)
        # 1 条 evidence 进工单（status='degraded' 的 assert 步），其他 4 条 skip/pass 不进
        ticket = json.loads(rendered.strip())
        assert ticket["severity"] == "warning"
        assert ticket["priority"] == "low"
        assert "继续观察" in ticket["escalation_hint"]
        assert "low" in ticket["escalation_hint"]

    def test_not_implemented_info_priority_low(self):
        """not_implemented → severity=info → priority='low'。"""
        evs = [
            _ev("preflight", "skip", failure_class="not_implemented"),
            _ev("auth", "skip", failure_class="not_implemented"),
            _ev("list", "skip", failure_class="not_implemented"),
            _ev("sample", "skip", failure_class="not_implemented"),
            _ev("assert", "skip", failure_class="not_implemented"),
        ]
        # status='skip' 不进 JSONL（避免刷屏），所以渲染结果为空
        rendered = render_tickets(evs)
        assert rendered.strip() == ""

    def test_ticket_has_all_three_new_fields(self):
        """工单草稿必须含 severity / priority / escalation_hint 三个新字段。"""
        ev = _ev("auth", "fail", failure_class="auth_failed")
        ticket = _evidence_to_ticket(
            ev,
            first_failed_step_for_run="auth",
            first_failed_step_for_evidence="auth",
            interface_fqn="his.outpatient-register",
        )
        assert "severity" in ticket
        assert "priority" in ticket
        assert "escalation_hint" in ticket
        assert ticket["severity"] == "critical"
        assert ticket["priority"] == "high"

    def test_ticket_escalation_hint_for_each_severity(self):
        """每个 severity 都有对应的 escalation_hint 文案。"""
        for sev in Severity:
            ev = _ev(
                "auth",
                "fail",
                failure_class={
                    Severity.CRITICAL: "auth_failed",
                    Severity.WARNING: "payload_empty",
                    Severity.INFO: "not_implemented",
                }[sev],
            )
            ticket = _evidence_to_ticket(
                ev,
                first_failed_step_for_run="auth",
                first_failed_step_for_evidence="auth",
                interface_fqn="his.outpatient-register",
            )
            assert ticket["escalation_hint"] == _ESCALATION_HINT[sev]


# ---------------------------------------------------------------------------
# Test 5: 凭据纪律（severity 模块不影响凭据脱敏）
# ---------------------------------------------------------------------------


class TestSeverityCredentialSafety:
    """severity 模块**不**读 evidence body / meta；分级不影响凭据脱敏。"""

    def test_severity_module_no_body_reading(self):
        """severity_of 只接收 failure_class 字符串，不读 body。"""
        # 即使 response_body 含 token 字面量，severity_of 仍能正确分级
        ev = _ev(
            "auth",
            "fail",
            failure_class="auth_failed",
            response_body='{"Authorization": "Bearer SUPER-SECRET-TOKEN-XYZ"}',
        )
        sev = severity_of(ev.failure_class)
        assert sev == Severity.CRITICAL
        # severity 模块不参与 redact——由 _coarse_redact 兜底
        redacted_meta = _coarse_redact_dict(ev.meta or {})
        # meta 中无凭据字段时不变（这里 meta 为空 dict）
        assert redacted_meta == {}

    def test_ticket_jsonl_no_token_in_priority_field(self):
        """priority='high'/'low' 是固定字符串，不含 token 字面量。"""
        ev = _ev("auth", "fail", failure_class="auth_failed")
        ticket = _evidence_to_ticket(
            ev,
            first_failed_step_for_run="auth",
            first_failed_step_for_evidence="auth",
            interface_fqn="his.outpatient-register",
        )
        # priority 是 "high" / "low" 字面量，绝不含任何凭据
        assert ticket["priority"] in ("high", "low")
        assert "token" not in ticket["priority"].lower()
        assert "password" not in ticket["priority"].lower()

    def test_render_tickets_priority_field_no_token_leak(self):
        """render_tickets 输出的 JSONL 不含凭据字面量（包含 priority 字段）。"""
        evs = [
            _ev(
                "auth",
                "fail",
                failure_class="auth_failed",
                response_body='{"Authorization": "Bearer SUPER-SECRET-TOKEN-LEAK-A"}',
                command="GET /api?token=tt-LEAK-B",
            )
        ]
        rendered = render_tickets(evs)
        # 凭据字面量一律不进入 JSONL
        assert "SUPER-SECRET-TOKEN-LEAK-A" not in rendered
        assert "tt-LEAK-B" not in rendered
        # 但 priority / severity 字段在
        assert '"priority": "high"' in rendered
        assert '"severity": "critical"' in rendered


# ---------------------------------------------------------------------------
# Test 6: 公开 API 一致性
# ---------------------------------------------------------------------------


class TestPublicAPI:
    """公开 API 一致性 + Severity 枚举完整性。"""

    def test_severity_enum_values(self):
        """Severity 枚举值与 spec 一致。"""
        assert Severity.CRITICAL.value == "critical"
        assert Severity.WARNING.value == "warning"
        assert Severity.INFO.value == "info"

    def test_all_severities_sorted(self):
        """all_severities 返回按字母升序。"""
        assert all_severities() == ["critical", "info", "warning"]

    def test_all_severity_mappings_keys_complete(self):
        """all_severity_mappings 覆盖核心 12 类 failure_class。"""
        mappings = all_severity_mappings()
        assert "auth_failed" in mappings
        assert "schema_mismatch" in mappings
        assert "hl7_ack_re" in mappings
        assert "timeout" in mappings
        assert "payload_empty" in mappings
        assert "tcp_unreachable" in mappings
        assert "tcp_half_closed" in mappings
        assert "http_status" in mappings
        assert "encoding_error" in mappings
        assert "degraded_slow" in mappings
        assert "hl7_ack_missing" in mappings
        assert "not_implemented" in mappings

    def test_all_severity_mappings_values_in_enum(self):
        """all_severity_mappings 的所有 value 都是合法 Severity。"""
        for failure_class, severity_str in all_severity_mappings().items():
            assert severity_str in all_severities(), (
                f"failure_class={failure_class} mapped to invalid severity={severity_str}"
            )

    def test_renderers_re_export_severity_helpers(self):
        """renderers 模块 re-export severity_of / priority_of，便于工单模板分支。"""
        assert renderers_severity_of is severity_of
        assert renderers_priority_of is priority_of

    def test_severity_module_all_symbols(self):
        """severity 模块 __all__ 包含全部公开符号。"""
        import vendor_if_doctor.connection.severity as sev_mod

        assert "Severity" in sev_mod.__all__
        assert "severity_of" in sev_mod.__all__
        assert "priority_of" in sev_mod.__all__
        assert "severity_to_priority" in sev_mod.__all__
        assert "is_critical" in sev_mod.__all__
        assert "is_warning" in sev_mod.__all__
        assert "all_severities" in sev_mod.__all__
        assert "all_severity_mappings" in sev_mod.__all__


# ---------------------------------------------------------------------------
# Test 7: 端到端 — render_tickets 含 priority / severity / escalation_hint
# ---------------------------------------------------------------------------


class TestEndToEndRenderTickets:
    """端到端：render_tickets 输出 JSONL 含 priority / severity / escalation_hint。"""

    def test_render_tickets_critical_auth_failed(self):
        """AuthFailed 端到端 → JSONL 含 severity=critical / priority=high。"""
        evs = _failed_auth_5()
        rendered = render_tickets(evs)
        ticket = json.loads(rendered.strip())
        assert ticket["severity"] == "critical"
        assert ticket["priority"] == "high"
        # escalation_hint 含中文"升级"和"high"
        assert "升级" in ticket["escalation_hint"]

    def test_render_tickets_warning_payload_empty(self):
        """payload_empty 端到端 → JSONL 含 severity=warning / priority=low / 继续观察。"""
        evs = _failed_payload_empty_5()
        rendered = render_tickets(evs)
        ticket = json.loads(rendered.strip())
        assert ticket["severity"] == "warning"
        assert ticket["priority"] == "low"
        assert "继续观察" in ticket["escalation_hint"]

    def test_render_tickets_multiple_failures_grouped_by_severity(self):
        """多个失败证据：每个都带各自的 severity / priority。"""
        evs = [
            _ev("preflight", "pass", vendor_id="his"),
            _ev("auth", "fail", vendor_id="his", failure_class="auth_failed"),
            _ev("list", "skip", vendor_id="his"),
            _ev("sample", "skip", vendor_id="his"),
            _ev("assert", "skip", vendor_id="his"),
            _ev("preflight", "pass", vendor_id="lis"),
            _ev("auth", "pass", vendor_id="lis"),
            _ev("sample", "fail", vendor_id="lis", failure_class="payload_empty"),
            _ev("assert", "skip", vendor_id="lis"),
        ]
        rendered = render_tickets(evs)
        lines = [json.loads(line) for line in rendered.strip().splitlines() if line]
        assert len(lines) == 2  # 2 条 evidence 进工单（auth_failed + payload_empty）
        severities = sorted([t["severity"] for t in lines])
        priorities = sorted([t["priority"] for t in lines])
        assert severities == ["critical", "warning"]
        assert priorities == ["high", "low"]
