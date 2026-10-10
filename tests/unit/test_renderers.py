"""renderers 单元测试：handover_md + ticket_jsonl.

覆盖：
  * 三/四态分类（healthy / degraded / failed / skipped）
  * 首次失败步骤 + failure_class 提取
  * JSONL 每行含 first_failed_step / suggested_actions
  * 凭据字段（Authorization / Bearer / X-API-Key / token / password / api_key
    / query 参数）一律 '***'，不进入交付物
  * suggested_actions_for 5+ 类 placeholder 替换
  * 文件落盘（write_handover / write_tickets）
  * skip 状态不进 JSONL（避免刷屏）
  * 空 evidence_list 不崩
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from vendor_if_doctor.connection.evidence import Evidence
from vendor_if_doctor.renderers import (
    handover_path,
    render_handover,
    render_tickets,
    suggested_actions_for,
    tickets_path,
    write_handover,
    write_tickets,
)
from vendor_if_doctor.renderers.handover_md import (
    _build_runs,
    _classify_run_status,
    _coarse_redact,
    _first_failed_step,
    _group_by_interface,
)
from vendor_if_doctor.renderers.ticket_jsonl import (
    _coarse_redact_dict,
    _run_first_failed_step,
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


def _healthy_5():
    """All five steps pass (including a list-sample step when list_endpoint 不配置时 skip;为完整 happy path 用 5 pass)."""
    return [
        _ev("preflight", "pass"),
        _ev("auth", "pass"),
        _ev("list", "pass"),
        _ev("sample", "pass", evidence_hash="abc123def4567890"),
        _ev("assert", "pass"),
    ]


def _failed_5():
    return [
        _ev("preflight", "pass"),
        _ev("auth", "fail", failure_class="auth_failed", summary="auth cred missing"),
        _ev("list", "skip"),
        _ev("sample", "skip"),
        _ev("assert", "skip"),
    ]


def _skipped_protocol():
    return [
        _ev("preflight", "skip"),
    ]


def _degraded_payload():
    return [
        _ev("preflight", "pass"),
        _ev("auth", "pass"),
        _ev("list", "pass"),
        _ev("sample", "pass", response_body="", failure_class="payload_empty"),
        _ev("assert", "degraded", failure_class="schema_mismatch"),
    ]


# ---------------------------------------------------------------------------
# classify + group
# ---------------------------------------------------------------------------


class TestClassifyRunStatus:
    def test_healthy_when_all_pass(self):
        assert _classify_run_status(_healthy_5()) == "healthy"

    def test_failed_when_any_fail(self):
        assert _classify_run_status(_failed_5()) == "failed"

    def test_skipped_when_all_skip(self):
        assert _classify_run_status(_skipped_protocol()) == "skipped"

    def test_degraded_when_degraded_present(self):
        assert _classify_run_status(_degraded_payload()) == "degraded"

    def test_empty_evidence_skipped(self):
        assert _classify_run_status([]) == "skipped"


class TestFirstFailedStep:
    def test_first_failed_step(self):
        assert _first_failed_step(_failed_5()) == "auth"

    def test_no_failed_returns_none(self):
        assert _first_failed_step(_healthy_5()) is None

    def test_first_failed_honors_order(self):
        # preflight failed should be returned over later steps
        evs = [_ev("preflight", "fail"), _ev("sample", "fail")]
        assert _first_failed_step(evs) == "preflight"


class TestGroupByInterface:
    def test_groups(self):
        a = _ev("preflight", "pass", vendor_id="a", interface_id="i1")
        b1 = _ev("preflight", "pass", vendor_id="b", interface_id="i1")
        b2 = _ev("auth", "pass", vendor_id="b", interface_id="i1")
        grouped = _group_by_interface([a, b1, b2])
        assert (("a", "i1"),) in grouped.keys() or ("a", "i1") in grouped
        assert len(grouped[("b", "i1")]) == 2
        assert len(grouped[("a", "i1")]) == 1


class TestBuildRuns:
    def test_runs_have_required_fields(self):
        runs = _build_runs(_failed_5())
        assert len(runs) == 1
        run = runs[0]
        assert run["vendor_id"] == "his"
        assert run["status"] == "failed"
        assert run["first_failed_step"] == "auth"
        assert run["failure_class"] == "auth_failed"
        assert run["evidence"]
        assert run["suggested_actions"]

    def test_runs_sorted_failed_first(self):
        from dataclasses import replace
        ev1 = [replace(e, vendor_id="z_healthy") for e in _healthy_5()]
        ev2 = [replace(e, vendor_id="a_failed") for e in _failed_5()]
        runs = _build_runs(ev1 + ev2)
        assert runs[0]["vendor_id"] == "a_failed"  # failed first
        assert runs[0]["status"] == "failed"
        assert runs[1]["vendor_id"] == "z_healthy"
        assert runs[1]["status"] == "healthy"


# ---------------------------------------------------------------------------
# markdown rendering
# ---------------------------------------------------------------------------


class TestRenderHandover:
    def test_basic_sections_present(self):
        md = render_handover(_failed_5())
        assert "## Vendor: his" in md
        assert "### Status: failed" in md
        assert "首次失败步骤：`auth`" in md

    def test_renders_healthy_run(self):
        md = render_handover(_healthy_5())
        assert "## Vendor: his" in md
        assert "### Status: healthy" in md

    def test_empty_evidence(self):
        md = render_handover([])
        assert "本次覆盖 vendor 条数：**0**" in md

    def test_multiple_vendors_in_one_md(self):
        ev1 = [Evidence(**{**e.__dict__, "vendor_id": "his"}) for e in _failed_5()]
        ev2 = [Evidence(**{**e.__dict__, "vendor_id": "lis"}) for e in _healthy_5()]
        md = render_handover(ev1 + ev2)
        assert "## Vendor: his" in md
        assert "## Vendor: lis" in md

    def test_no_raw_token_in_md(self):
        leak_ev = _ev(
            "sample",
            "fail",
            failure_class="auth_failed",
            response_body='{"token":"supersecret123","Authorization":"Bearer xyz"}',
            command="GET https://his.example.com?api_key=kk-999&token=tt-999",
        )
        md = render_handover([leak_ev])
        assert "supersecret123" not in md
        assert "Bearer xyz" not in md
        assert "kk-999" not in md
        assert "tt-999" not in md

    def test_evidence_hash_visible(self):
        evs = _healthy_5()
        evs[3].evidence_hash = "deadbeef12345678"
        md = render_handover(evs)
        assert "`deadbeef12345678`" in md


# ---------------------------------------------------------------------------
# jsonl rendering
# ---------------------------------------------------------------------------


class TestRenderTickets:
    def test_failed_lines_have_required_keys(self):
        text = render_tickets(_failed_5())
        lines = [l for l in text.splitlines() if l.strip()]
        assert lines, "must produce at least one line"
        for line in lines:
            obj = json.loads(line)
            assert "first_failed_step" in obj
            assert "suggested_actions" in obj

    def test_healthy_lines_excluded(self):
        text = render_tickets(_healthy_5())
        lines = [l for l in text.splitlines() if l.strip()]
        assert lines == [], "healthy evidence must not generate tickets"

    def test_skipped_excluded(self):
        text = render_tickets(_skipped_protocol())
        assert text.strip() == ""

    def test_first_failed_step_value(self):
        text = render_tickets(_failed_5())
        first_obj = json.loads(text.splitlines()[0])
        assert first_obj["first_failed_step"] == "auth"
        assert first_obj["failure_class"] == "auth_failed"

    def test_suggested_actions_placeholder_substitution(self):
        text = render_tickets(_failed_5())
        obj = json.loads(text.splitlines()[0])
        actions = obj["suggested_actions"]
        assert actions, "must include suggested_actions for auth_failed"
        joined = "\n".join(actions)
        # 占位符必须被替换
        assert "{vendor_id}" not in joined
        assert "{vendor_token}" not in joined
        # 业务信息必须保留
        assert "VENDOR_HIS_" in joined or "his" in joined

    def test_no_raw_token_in_jsonl(self):
        leak_ev = _ev(
            "sample",
            "fail",
            failure_class="auth_failed",
            response_body='{"token":"s3cretvalue"}',
            command="GET /api?token=rawvalue",
        )
        text = render_tickets([leak_ev])
        assert "s3cretvalue" not in text
        assert "rawvalue" not in text
        assert "***" in text  # replacement marker present

    def test_no_full_bearer_in_jsonl(self):
        leak_ev = _ev(
            "auth",
            "fail",
            failure_class="auth_failed",
            response_body='{"Authorization":"Bearer abc.def.ghi"}',
        )
        text = render_tickets([leak_ev])
        assert "abc.def.ghi" not in text
        assert "Bearer abc.def.ghi" not in text

    def test_each_line_is_valid_json(self):
        evs = [_failed_5(), _healthy_5(), _skipped_protocol()]
        all_evs = [e for sub in evs for e in sub]
        text = render_tickets(all_evs)
        for line in text.splitlines():
            if not line.strip():
                continue
            json.loads(line)  # must parse


# ---------------------------------------------------------------------------
# redact helpers
# ---------------------------------------------------------------------------


class TestRedactHelpers:
    def test_dict_redact_secret_keys(self):
        d = {
            "Authorization": "Bearer xyz",
            "X-API-Key": "abc",
            "password": "p",
            "token": "t",
            "api_key": "k",
            "safe": "visible",
        }
        out = _coarse_redact_dict(d)
        assert out["Authorization"] == "***"
        assert out["X-API-Key"] == "***"
        assert out["password"] == "***"
        assert out["token"] == "***"
        assert out["api_key"] == "***"
        assert out["safe"] == "visible"

    def test_dict_recursion(self):
        d = {"a": {"b": {"Authorization": "Bearer t"}}, "c": [{"token": "foo"}]}
        out = _coarse_redact_dict(d)
        assert out["a"]["b"]["Authorization"] == "***"
        assert out["c"][0]["token"] == "***"

    def test_str_redact_bearer(self):
        assert _coarse_redact("Bearer abc.def") == "Bearer ***"

    def test_str_redact_json_field(self):
        s = '{"Authorization": "Bearer xyz", "safe": "ok"}'
        out = _coarse_redact(s) or ""
        assert "Bearer xyz" not in out
        assert "ok" in out

    def test_str_redact_url_query(self):
        s = "/api?api_key=kk-777&token=tt-888&q=ok"
        out = _coarse_redact(s) or ""
        assert "kk-777" not in out
        assert "tt-888" not in out
        assert "q=ok" in out

    def test_none_in_none_out(self):
        assert _coarse_redact(None) is None
        assert _coarse_redact("") == ""


# ---------------------------------------------------------------------------
# suggested_actions_for
# ---------------------------------------------------------------------------


class TestSuggestedActions:
    def test_auth_failed_returns_actions(self):
        actions = suggested_actions_for("auth_failed", "his", "outpatient-register")
        assert len(actions) >= 1
        joined = " ".join(actions)
        # 模板使用 `VENDOR_{vendor_token}_*` 通配形式（vendor_token=HIS），覆盖
        # auth_failed 类别的常见 env 变量 (VENDOR_HIS_TOKEN / VENDOR_HIS_AUTH_TOKEN / 任意)
        assert "VENDOR_HIS" in joined

    def test_hl7_ack_re_returns_actions(self):
        actions = suggested_actions_for("hl7_ack_re", "his", "emr-submit")
        joined = " ".join(actions).lower()
        assert "hl7apy" in joined or "mlp" in joined

    def test_unknown_failure_returns_empty(self):
        assert suggested_actions_for(None, "his", "x") == []
        assert suggested_actions_for("not_a_class", "his", "x") == []

    def test_placeholder_substitution(self):
        actions = suggested_actions_for("timeout", "his-bj", "outpatient-register")
        joined = " ".join(actions)
        # 内部 host 占位符替换
        assert "{host}" not in joined
        assert "{port}" not in joined


# ---------------------------------------------------------------------------
# path helpers + write
# ---------------------------------------------------------------------------


class TestPathsAndWrites:
    def test_default_paths_use_out_dir(self, tmp_path, monkeypatch):
        monkeypatch.delenv("VENDOR_DOCTOR_HANDOVER_DIR", raising=False)
        monkeypatch.delenv("VENDOR_DOCTOR_TICKETS_DIR", raising=False)
        monkeypatch.chdir(tmp_path)
        h = handover_path()
        t = tickets_path()
        assert h.name == "handover.md"
        assert t.name == "tickets.jsonl"
        assert "out" in str(h.parent) or "/" in str(h.parent)

    def test_env_paths_respected(self, tmp_path, monkeypatch):
        h_dir = tmp_path / "handover"
        t_dir = tmp_path / "tickets"
        h_dir.mkdir()
        t_dir.mkdir()
        monkeypatch.setenv("VENDOR_DOCTOR_HANDOVER_DIR", str(h_dir))
        monkeypatch.setenv("VENDOR_DOCTOR_TICKETS_DIR", str(t_dir))
        h = handover_path()
        t = tickets_path()
        assert h.parent.parent == h_dir
        assert t.parent.parent == t_dir

    def test_write_handover_creates_file(self, tmp_path):
        out = tmp_path / "h.md"
        write_handover(_failed_5(), path=out)
        assert out.exists()
        text = out.read_text(encoding="utf-8")
        assert "## Vendor: his" in text

    def test_write_tickets_creates_file(self, tmp_path):
        out = tmp_path / "t.jsonl"
        write_tickets(_failed_5(), path=out)
        assert out.exists()
        for line in out.read_text(encoding="utf-8").splitlines():
            if line.strip():
                json.loads(line)
