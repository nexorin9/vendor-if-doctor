"""renderers.weekly_html 单元测试。

覆盖：
  * fetch_recent_runs：跨月 UNION ALL + 7 天窗口 + 早于窗口的 run 被排除
  * aggregate_recent_runs：业务系统分类 + 三/四态计数 + 异常清单 +
    severity 映射 + 趋势 spark 生成
  * render_weekly_html：HTML 含 .system-rate / .trend-spark / .anomaly-list
    三块；无 <script> / <link rel>；ASCII 火花在 tbody；evidence_hash
    只显示前 8 位；凭据字段被 redact
  * write_weekly_html：默认路径 + 自定义路径 + 文件落盘
  * 7 天窗口边界 — 正好 7 天前的 run 算入，超出 7 天的不算
  * _system_key：his / lis / lis-foo / medical-insurance / other
  * 凭据纪律：Authorization / Bearer / token 不进入 HTML 输出
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

import pytest

from vendor_if_doctor.renderers import (
    SystemAgg,
    WeeklyReport,
    aggregate_recent_runs,
    fetch_recent_runs,
    render_weekly_html,
    weekly_html_path,
    write_weekly_html,
)
from vendor_if_doctor.renderers.weekly_html import _system_key, _coarse_redact


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _row(
    *,
    run_id: str = "abcd1234ef567890",
    vendor_id: str = "his",
    interface_id: str = "outpatient-register",
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
    status: str = "failed",
    evidence_hash: Optional[str] = "abc123def4567890",
    steps_json: Optional[str] = None,
) -> dict:
    if steps_json is None and status in ("failed", "degraded"):
        steps_json = json.dumps(
            [
                {"step_name": "preflight", "status": "pass", "failure_class": None},
                {
                    "step_name": "auth",
                    "status": "fail",
                    "failure_class": "auth_failed",
                },
                {"step_name": "list", "status": "skip", "failure_class": None},
                {"step_name": "sample", "status": "skip", "failure_class": None},
                {"step_name": "assert", "status": "skip", "failure_class": None},
            ],
            ensure_ascii=False,
        )
    return {
        "run_id": run_id,
        "vendor_id": vendor_id,
        "interface_id": interface_id,
        "started_at": started_at or "2026-10-08T03:00:00Z",
        "finished_at": finished_at or "2026-10-08T03:00:01Z",
        "status": status,
        "evidence_hash": evidence_hash,
        "steps_json": steps_json,
    }


@pytest.fixture
def now():
    return datetime(2026, 10, 10, 4, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def runs(now):
    """5 个 vendor + 跨 7 天窗口，覆盖三/四态。"""

    # 业务系统：his(2 healthy + 1 failed), lis(1 degraded), pacs(1 healthy),
    # medical-insurance(1 failed), regional(1 skipped)
    rows = [
        # his 系统
        _row(
            run_id="his001", vendor_id="his", interface_id="outpatient-register",
            started_at="2026-10-09T01:00:00Z", status="healthy",
            evidence_hash="0011223344556677",
            steps_json=json.dumps(
                [{"step_name": "preflight", "status": "pass", "failure_class": None}] * 5
            ),
        ),
        _row(
            run_id="his002", vendor_id="his", interface_id="outpatient-register",
            started_at="2026-10-10T01:00:00Z", status="healthy",
            evidence_hash="1122334455667788",
            steps_json=json.dumps(
                [{"step_name": "preflight", "status": "pass", "failure_class": None}] * 5
            ),
        ),
        _row(
            run_id="his003", vendor_id="his", interface_id="outpatient-register",
            started_at="2026-10-10T02:30:00Z", status="failed",
            evidence_hash="2233445566778899",
        ),
        # lis 系统
        _row(
            run_id="lis001", vendor_id="lis", interface_id="result-pull",
            started_at="2026-10-09T03:00:00Z", status="degraded",
            evidence_hash="3344556677889900",
            steps_json=json.dumps([
                {"step_name": "preflight", "status": "pass", "failure_class": None},
                {"step_name": "auth", "status": "pass", "failure_class": None},
                {"step_name": "list", "status": "pass", "failure_class": None},
                {"step_name": "sample", "status": "degraded", "failure_class": "degraded_slow"},
                {"step_name": "assert", "status": "degraded", "failure_class": "schema_mismatch"},
            ], ensure_ascii=False),
        ),
        # pacs 系统
        _row(
            run_id="pacs001", vendor_id="pacs", interface_id="study-pull",
            started_at="2026-10-08T05:00:00Z", status="healthy",
            evidence_hash="4455667788990011",
            steps_json=json.dumps(
                [{"step_name": "preflight", "status": "pass", "failure_class": None}] * 5
            ),
        ),
        # 医保系统
        _row(
            run_id="mi001", vendor_id="medical-insurance", interface_id="claim-submit",
            started_at="2026-10-10T03:00:00Z", status="failed",
            evidence_hash="5566778899001122",
        ),
        # 区域平台
        _row(
            run_id="reg001", vendor_id="regional-readonly", interface_id="profile",
            started_at="2026-10-09T07:00:00Z", status="skipped",
            evidence_hash="6677889900112233",
            steps_json=json.dumps(
                [{"step_name": "preflight", "status": "skip", "failure_class": "not_implemented"}] * 5
            ),
        ),
    ]
    return rows


# ---------------------------------------------------------------------------
# _system_key 分类
# ---------------------------------------------------------------------------


class TestSystemKey:
    def test_his(self):
        assert _system_key("his") == "his"

    def test_his_with_suffix(self):
        # his_outpatient / lis-foo 都归到他自己的 prefix
        assert _system_key("his_outpatient") == "his"
        assert _system_key("lis-foo-bar") == "lis"
        assert _system_key("pacs_ct") == "pacs"

    def test_medical_insurance_full_name(self):
        # medical-insurance 是完整前缀
        assert _system_key("medical-insurance") == "medical-insurance"
        assert _system_key("medical-insurance-extra") == "medical-insurance"

    def test_unknown(self):
        assert _system_key("custom-vendor") == "other"
        assert _system_key("") == "other"
        assert _system_key(None) == "other"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _coarse_redact 字符串级过滤
# ---------------------------------------------------------------------------


class TestCoarseRedact:
    def test_authorization_header(self):
        out = _coarse_redact("Authorization: Bearer SUPER-SECRET-TOKEN-XYZ")
        assert "SUPER-SECRET-TOKEN-XYZ" not in out
        assert "***" in out

    def test_token_value(self):
        out = _coarse_redact('{"token": "abc123"}')
        assert "abc123" not in out
        assert "***" in out

    def test_passthrough(self):
        # 安全文本不被过滤
        assert _coarse_redact("hello world") == "hello world"
        assert _coarse_redact('{"code": 200}') == '{"code": 200}'


# ---------------------------------------------------------------------------
# fetch_recent_runs（直接构造 SQLite 数据，不依赖 on-disk 测试 fixture）
# ---------------------------------------------------------------------------


def _make_db(db_path: Path, rows: List[dict], now: datetime) -> None:
    """构造 SQLite 含 monthly partition 表 + 1 row 写老 inspection_runs。"""

    conn = sqlite3.connect(str(db_path))
    try:
        # Legacy inspection_runs
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS inspection_runs (
                run_id        TEXT PRIMARY KEY,
                vendor_id     TEXT NOT NULL,
                interface_id  TEXT NOT NULL,
                started_at    TEXT NOT NULL,
                finished_at   TEXT NOT NULL,
                status        TEXT NOT NULL,
                steps_json    TEXT,
                evidence_hash TEXT,
                meta_json     TEXT
            );
            """
        )
        # 2026-10 月表
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS inspection_runs_y2026m10 (
                run_id        TEXT PRIMARY KEY,
                vendor_id     TEXT NOT NULL,
                interface_id  TEXT NOT NULL,
                started_at    TEXT NOT NULL,
                finished_at   TEXT NOT NULL,
                status        TEXT NOT NULL,
                steps_json    TEXT,
                evidence_hash TEXT,
                meta_json     TEXT
            );
            """
        )
        for row in rows:
            target_table = (
                "inspection_runs_y2026m10"
                if row["started_at"].startswith("2026-10")
                else "inspection_runs"
            )
            conn.execute(
                f"INSERT INTO {target_table} ("
                " run_id, vendor_id, interface_id, started_at, finished_at, "
                " status, steps_json, evidence_hash, meta_json"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["run_id"],
                    row["vendor_id"],
                    row["interface_id"],
                    row["started_at"],
                    row["finished_at"],
                    row["status"],
                    row["steps_json"],
                    row["evidence_hash"],
                    None,
                ),
            )
        conn.commit()
    finally:
        conn.close()


class TestFetchRecentRuns:
    def test_window_filters_old_runs(self, tmp_path):
        db = tmp_path / "inspect.db"
        now = datetime(2026, 10, 10, 4, 0, 0, tzinfo=timezone.utc)
        rows = [
            _row(
                run_id="in-window", vendor_id="his",
                started_at="2026-10-08T00:00:00Z", status="healthy",
            ),
            _row(
                run_id="out-of-window", vendor_id="lis",
                started_at="2026-10-01T00:00:00Z", status="healthy",
            ),
            _row(
                run_id="today-edge", vendor_id="pacs",
                started_at="2026-10-10T03:59:59Z", status="failed",
            ),
        ]
        _make_db(db, rows, now)
        result = fetch_recent_runs(window_days=7, path=db, now=now)
        ids = {r["run_id"] for r in result}
        # 7 天窗口：起点 2026-10-04 00:00:00Z；in-window (10-08) 含；out-of-window (10-01) 不含
        assert "in-window" in ids
        assert "out-of-window" not in ids
        assert "today-edge" in ids

    def test_window_days_validation(self, tmp_path):
        db = tmp_path / "inspect.db"
        with pytest.raises(ValueError):
            fetch_recent_runs(window_days=0, path=db)


# ---------------------------------------------------------------------------
# aggregate_recent_runs
# ---------------------------------------------------------------------------


class TestAggregateRecentRuns:
    def test_total_counts(self, runs, now):
        rep = aggregate_recent_runs(runs, now=now, window_days=7)
        # 7 行 runs
        assert rep.total_runs == 7
        # 5 个系统：his, lis, pacs, medical-insurance, regional
        sys_names = {s.system for s in rep.systems}
        assert sys_names == {"his", "lis", "pacs", "medical-insurance", "regional"}

    def test_his_breakdown(self, runs, now):
        rep = aggregate_recent_runs(runs, now=now, window_days=7)
        his = next(s for s in rep.systems if s.system == "his")
        assert his.total == 3
        assert his.healthy == 2
        assert his.failed == 1
        # 连通率 = (2 healthy + 0 degraded) / 3 = 0.6667
        assert abs(his.connectivity - 2 / 3) < 0.01

    def test_lis_degraded(self, runs, now):
        rep = aggregate_recent_runs(runs, now=now, window_days=7)
        lis = next(s for s in rep.systems if s.system == "lis")
        assert lis.degraded == 1
        assert lis.failed == 0

    def test_pacs_healthy_only(self, runs, now):
        rep = aggregate_recent_runs(runs, now=now, window_days=7)
        pacs = next(s for s in rep.systems if s.system == "pacs")
        assert pacs.healthy == 1
        assert pacs.failed == 0
        assert pacs.connectivity == 1.0

    def test_regional_skipped(self, runs, now):
        rep = aggregate_recent_runs(runs, now=now, window_days=7)
        regional = next(s for s in rep.systems if s.system == "regional")
        assert regional.skipped == 1
        assert regional.failed == 0

    def test_anomaly_first_failed_step_extracted(self, runs, now):
        rep = aggregate_recent_runs(runs, now=now, window_days=7)
        his = next(s for s in rep.systems if s.system == "his")
        # 1 个 failed run: his.outpatient-register
        assert len(his.anomalies) == 1
        anom = his.anomalies[0]
        # auth step 是首个 fail
        assert anom["first_failed_step"] == "auth"
        assert anom["failure_class"] == "auth_failed"
        assert anom["severity"] == "critical"

    def test_anomaly_degraded_warning(self, runs, now):
        rep = aggregate_recent_runs(runs, now=now, window_days=7)
        lis = next(s for s in rep.systems if s.system == "lis")
        assert len(lis.anomalies) == 1
        anom = lis.anomalies[0]
        # degraded_slow → warning
        assert anom["severity"] == "warning"

    def test_anomaly_hash_truncated_to_8_chars(self, runs, now):
        rep = aggregate_recent_runs(runs, now=now, window_days=7)
        his = next(s for s in rep.systems if s.system == "his")
        anom = his.anomalies[0]
        assert len(anom["evidence_hash_short"]) == 8
        assert anom["evidence_hash_short"] == "22334455"

    def test_trend_length_equals_window_days(self, runs, now):
        rep = aggregate_recent_runs(runs, now=now, window_days=7)
        for sys in rep.systems:
            assert len(sys.trend) == 7
            # 趋势里每项 0..1
            for v in sys.trend:
                assert 0.0 <= v <= 1.0

    def test_trend_spark_unicode(self, runs, now):
        rep = aggregate_recent_runs(runs, now=now, window_days=7)
        for sys in rep.systems:
            # 7 个 unicode sparkline 字符或 1 个 —
            spark = sys.trend_spark
            assert len(spark) == 7 or spark == "—"

    def test_anomaly_skipped_excluded(self, runs, now):
        # 验证 skipped run 不进入 anomaly list
        rep = aggregate_recent_runs(runs, now=now, window_days=7)
        regional = next(s for s in rep.systems if s.system == "regional")
        assert regional.anomalies == []

    def test_systems_sorted_by_connectivity_asc_then_failed(self, runs, now):
        rep = aggregate_recent_runs(runs, now=now, window_days=7)
        # 排序：连通率 asc → failed desc → system name
        rates = [s.connectivity for s in rep.systems]
        assert rates == sorted(rates)

    def test_week_window_dates(self, runs, now):
        rep = aggregate_recent_runs(runs, now=now, window_days=7)
        # 起点 = 2026-10-04T00:00:00Z；now = 2026-10-10T04:00:00Z
        assert rep.week_start.startswith("2026-10-04")
        assert rep.week_end.startswith("2026-10-10")


# ---------------------------------------------------------------------------
# render_weekly_html — HTML 产物结构
# ---------------------------------------------------------------------------


class TestRenderWeeklyHtml:
    def test_three_required_blocks_present(self, runs, now):
        html_text = render_weekly_html(
            runs, window_days=7, generated_at="2026-10-10T04:00:00Z"
        )
        assert 'class="system-rate"' in html_text
        assert 'class="trend-spark"' in html_text
        assert 'class="anomaly-list"' in html_text

    def test_no_external_resources(self, runs, now):
        html_text = render_weekly_html(
            runs, window_days=7, generated_at="2026-10-10T04:00:00Z"
        )
        # 没有 <script / 没有 <link rel=...>
        assert "<script" not in html_text.lower()
        assert "<link rel" not in html_text.lower()
        assert "<style @import" not in html_text.lower()
        assert "<img" not in html_text.lower()
        assert "@import" not in html_text.lower()

    def test_utf8_charset(self, runs, now):
        html_text = render_weekly_html(
            runs, window_days=7, generated_at="2026-10-10T04:00:00Z"
        )
        assert 'charset="utf-8"' in html_text.lower()

    def test_doctype_html5(self, runs, now):
        html_text = render_weekly_html(
            runs, window_days=7, generated_at="2026-10-10T04:00:00Z"
        )
        assert html_text.lstrip().startswith("<!DOCTYPE html>")

    def test_anomaly_evidence_hash_truncated_to_8(self, runs, now):
        html_text = render_weekly_html(
            runs, window_days=7, generated_at="2026-10-10T04:00:00Z"
        )
        # his001 evidence_hash 2233445566778899 → 截到 22334455
        assert "22334455" in html_text
        # 完整 16 位 hash 不出现
        assert "2233445566778899" not in html_text

    def test_no_anomaly_message_when_no_failures(self, now):
        rows = [
            _row(
                run_id="only-healthy", vendor_id="his",
                started_at="2026-10-10T01:00:00Z", status="healthy",
                steps_json=json.dumps(
                    [{"step_name": "preflight", "status": "pass", "failure_class": None}] * 5
                ),
            ),
        ]
        html_text = render_weekly_html(
            rows, window_days=7, generated_at="2026-10-10T04:00:00Z"
        )
        assert "本周无异常 run" in html_text

    def test_credential_redaction_in_output(self, now):
        # 把 token 字面量注入到 evidence_hash 字段
        rows = [
            _row(
                run_id="leakcheck", vendor_id="his",
                started_at="2026-10-10T01:00:00Z", status="failed",
                evidence_hash="Bearer SECRET-LEAK-CHECK-XYZ",
            ),
        ]
        html_text = render_weekly_html(
            rows, window_days=7, generated_at="2026-10-10T04:00:00Z"
        )
        # 完整 token 不出现
        assert "SECRET-LEAK-CHECK-XYZ" not in html_text
        # 仅截到 8 位前 "Bearer " 头被 redact
        # 验证 "Bearer SECRET" 这种串不再出现
        assert "Bearer SECRET" not in html_text

    def test_html_escaping_vendor_id(self, now):
        # 验证 vendor_id / interface_id 里 HTML 注入字符被 escape
        # 失败行会进 anomaly-list，interface_id 才会被渲染
        rows = [
            _row(
                run_id="xss1",
                vendor_id="<script>alert(1)</script>",
                interface_id="<img src=x onerror=alert(2)>",
                started_at="2026-10-10T01:00:00Z", status="failed",
            ),
        ]
        html_text = render_weekly_html(
            rows, window_days=7, generated_at="2026-10-10T04:00:00Z"
        )
        # 关键：原始 <script> / <img 标签不再出现（作为可执行 HTML）
        assert "<script>" not in html_text
        assert "<img " not in html_text
        # 关键：原始 'attr=val' 配对（与左右尖括号配套）不出现
        assert "<img src=x" not in html_text
        # html.escape 后变成 &lt;script&gt; / &lt;img
        assert "&lt;script&gt;" in html_text
        assert "&lt;img" in html_text

    def test_anomaly_severity_class(self, runs, now):
        html_text = render_weekly_html(
            runs, window_days=7, generated_at="2026-10-10T04:00:00Z"
        )
        # auth_failed → critical; degraded_slow → warning
        assert 'class="severity-critical"' in html_text
        assert 'class="severity-warning"' in html_text

    def test_trend_spark_in_tbody(self, runs, now):
        html_text = render_weekly_html(
            runs, window_days=7, generated_at="2026-10-10T04:00:00Z"
        )
        # 7 个 Unicode sparkline 字符在 trend-spark table 里出现
        # 这里至少找到一个 block（单个系统）
        m = re.search(r'class="trend-spark">([▁▂▃▄▅▆▇█]+)</span>', html_text)
        assert m is not None
        assert len(m.group(1)) == 7


# ---------------------------------------------------------------------------
# write_weekly_html
# ---------------------------------------------------------------------------


class TestWriteWeeklyHtml:
    def test_default_path(self, runs, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("VENDOR_DOCTOR_WEEKLY_DIR", raising=False)
        html_path = write_weekly_html(
            runs,
            window_days=7,
            generated_at="2026-10-10T04:00:00Z",
        )
        assert html_path.exists()
        assert html_path.name == "weekly.html"

    def test_custom_path(self, runs, tmp_path):
        target = tmp_path / "myreport.html"
        html_path = write_weekly_html(
            runs,
            window_days=7,
            path=target,
            generated_at="2026-10-10T04:00:00Z",
        )
        assert html_path == target
        assert target.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")

    def test_env_var_override(self, runs, monkeypatch, tmp_path):
        target_dir = tmp_path / "weekly_env"
        monkeypatch.setenv("VENDOR_DOCTOR_WEEKLY_DIR", str(target_dir))
        html_path = write_weekly_html(
            runs, window_days=7, generated_at="2026-10-10T04:00:00Z"
        )
        # parent 是 VENDOR_DOCTOR_WEEKLY_DIR/<today>/
        assert target_dir in html_path.parents
        assert html_path.exists()


class TestWeeklyHtmlPath:
    def test_default(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("VENDOR_DOCTOR_WEEKLY_DIR", raising=False)
        p = weekly_html_path()
        assert p.parent.exists()
        assert p.name == "weekly.html"

    def test_base_override(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("VENDOR_DOCTOR_WEEKLY_DIR", raising=False)
        target = tmp_path / "out" / "weekly_custom" / "weekly.html"
        p = weekly_html_path(target)
        assert p == target


# ---------------------------------------------------------------------------
# 端到端：读 SQLite → 聚合 → 渲染 → 落盘
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_db_to_html(self, tmp_path):
        db = tmp_path / "e2e.db"
        now = datetime(2026, 10, 10, 4, 0, 0, tzinfo=timezone.utc)
        rows = [
            _row(
                run_id="e2e1", vendor_id="his", interface_id="outpatient-register",
                started_at="2026-10-10T01:00:00Z", status="failed",
                evidence_hash="e2e0000000000010",
            ),
            _row(
                run_id="e2e2", vendor_id="lis", interface_id="result-pull",
                started_at="2026-10-09T02:00:00Z", status="healthy",
                evidence_hash="e2e0000000000020",
                steps_json=json.dumps(
                    [{"step_name": "preflight", "status": "pass", "failure_class": None}] * 5
                ),
            ),
        ]
        _make_db(db, rows, now)

        # 1. 从 DB 读
        fetched = fetch_recent_runs(window_days=7, path=db, now=now)
        assert len(fetched) == 2

        # 2. 聚合
        rep = aggregate_recent_runs(fetched, window_days=7, now=now)
        assert rep.total_runs == 2

        # 3. 渲染
        html_text = render_weekly_html(
            fetched, window_days=7, generated_at="2026-10-10T04:00:00Z"
        )
        assert 'class="system-rate"' in html_text
        assert 'class="trend-spark"' in html_text
        assert 'class="anomaly-list"' in html_text
        assert "e2e0000" in html_text  # evidence hash 前 8 位
        assert "his" in html_text
        assert "lis" in html_text

    def test_empty_db_returns_empty_html(self, tmp_path):
        db = tmp_path / "empty.db"
        now = datetime(2026, 10, 10, 4, 0, 0, tzinfo=timezone.utc)
        # 表是空的，但 schema 已建
        conn = sqlite3.connect(str(db))
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS inspection_runs (
                run_id TEXT PRIMARY KEY, vendor_id TEXT, interface_id TEXT,
                started_at TEXT, finished_at TEXT, status TEXT,
                steps_json TEXT, evidence_hash TEXT, meta_json TEXT
            );
            """
        )
        conn.commit()
        conn.close()

        fetched = fetch_recent_runs(window_days=7, path=db, now=now)
        assert fetched == []
        rep = aggregate_recent_runs(fetched, now=now, window_days=7)
        html_text = render_weekly_html(
            None, report=rep, window_days=7,
            generated_at="2026-10-10T04:00:00Z",
        )
        assert 'class="system-rate"' in html_text
        # 0 个 system 时表格是空的，但仍渲染成功
        assert 'class="trend-spark"' in html_text


# ---------------------------------------------------------------------------
# WeeklyReport / SystemAgg 数据类
# ---------------------------------------------------------------------------


class TestDataClasses:
    def test_system_agg_connectivity_zero_total(self):
        agg = SystemAgg(system="his")
        assert agg.connectivity == 1.0  # 0/0 不报错

    def test_system_agg_trend_spark(self):
        agg = SystemAgg(system="his")
        agg.trend = [0.0, 0.25, 0.5, 0.75, 1.0, 1.0, 0.5]
        spark = agg.trend_spark
        assert len(spark) == 7
        # 第一项 0% → '▁'，最后 50% → '▄'
        assert spark[0] == "▁"

    def test_weekly_report_defaults(self):
        rep = WeeklyReport()
        assert rep.systems == []
        assert rep.total_runs == 0
        assert rep.window_days == 7
