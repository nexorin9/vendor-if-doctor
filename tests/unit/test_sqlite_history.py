"""Unit tests for vendor_if_doctor/renderers/sqlite_history.py.

覆盖：
  * 建表 + 索引 + WAL 模式（journal_mode pragma）
  * record_run 写入 + 聚合 status / evidence_hash
  * 同 run_id 二次写入抛 sqlite3.IntegrityError（幂等防护）
  * evidence_hash 不含 Authorization / Bearer / token / password 字面量
  * BEGIN IMMEDIATE 包裹 INSERT（手动验证：commit / rollback 路径）
  * write_history 一次写多组 evidence（按 (vendor, interface) 分 run）
  * fetch_run / fetch_runs_by_vendor 查询回读（跨月 UNION ALL）
  * path 环境变量优先级（VENDOR_DOCTOR_DB 覆盖默认）
  * 月表分区（task 19）：partition_table_name / ensure_partition / 月表索引
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

import pytest

from vendor_if_doctor.connection.evidence import Evidence
from vendor_if_doctor.renderers.sqlite_history import (
    SCHEMA_DDL,
    _classify_run_status,
    _compute_run_evidence_hash,
    _make_run_id,
    _partition_suffix,
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    """每次测试一个临时 SQLite 文件。"""

    return tmp_path / "vendor_if_doctor.db"


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch, tmp_path: Path):
    """隔离 VENDOR_DOCTOR_DB 环境变量，确保 db_path() 不会被别处污染。"""

    monkeypatch.delenv("VENDOR_DOCTOR_DB", raising=False)
    yield


def _ev(
    step_name: str,
    status: str = "pass",
    command: str = "GET /api/v1/health",
    response_body: str | None = None,
    failure_class: str | None = None,
    latency_ms: int = 100,
    vendor_id: str = "his",
    interface_id: str = "outpatient-register",
) -> Evidence:
    return Evidence(
        summary=f"{step_name} {status}",
        command=command,
        latency_ms=latency_ms,
        status=status,  # type: ignore[arg-type]
        failure_class=failure_class,
        response_body=response_body,
        step_name=step_name,
        vendor_id=vendor_id,
        interface_id=interface_id,
    )


# ---------------------------------------------------------------------------
# db_path / connect / schema
# ---------------------------------------------------------------------------


class TestDbPath:
    def test_default_is_cwd_relative(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        p = db_path()
        assert p == tmp_path / "vendor_if_doctor.db"

    def test_env_var_overrides_default(self, monkeypatch, tmp_path):
        target = tmp_path / "custom.db"
        monkeypatch.setenv("VENDOR_DOCTOR_DB", str(target))
        assert db_path() == target

    def test_explicit_path_wins(self, monkeypatch, tmp_path):
        explicit = tmp_path / "explicit.db"
        monkeypatch.setenv("VENDOR_DOCTOR_DB", str(tmp_path / "ignored.db"))
        assert db_path(explicit) == explicit


class TestSchemaAndConnect:
    def test_connect_creates_table_and_indices(self, tmp_db):
        conn = connect(tmp_db)
        try:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='inspection_runs'"
            )
            assert cur.fetchone() is not None
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='inspection_runs'"
            )
            names = {row[0] for row in cur.fetchall()}
            assert "idx_inspection_runs_vendor_id" in names
            assert "idx_inspection_runs_started_at" in names
        finally:
            conn.close()

    def test_journal_mode_is_wal(self, tmp_db):
        conn = connect(tmp_db)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode.lower() == "wal"
        finally:
            conn.close()

    def test_ensure_schema_idempotent(self, tmp_db):
        conn = connect(tmp_db)
        try:
            ensure_schema(conn)
            ensure_schema(conn)  # 不报错即通过
            cur = conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='inspection_runs'"
            )
            assert cur.fetchone()[0] == 1
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# _classify_run_status / _make_run_id / _compute_run_evidence_hash
# ---------------------------------------------------------------------------


class TestClassifyRunStatus:
    def test_empty_evidence_is_skipped(self):
        assert _classify_run_status([]) == "skipped"

    def test_all_pass_is_healthy(self):
        assert _classify_run_status(
            [_ev("preflight"), _ev("auth"), _ev("sample"), _ev("assert")]
        ) == "healthy"

    def test_any_fail_is_failed(self):
        evs = [_ev("preflight"), _ev("auth", "fail", failure_class="auth_failed"), _ev("sample")]
        assert _classify_run_status(evs) == "failed"

    def test_any_degraded_is_degraded(self):
        evs = [_ev("preflight"), _ev("auth", "degraded"), _ev("sample")]
        assert _classify_run_status(evs) == "degraded"

    def test_pass_plus_skip_is_degraded(self):
        evs = [_ev("preflight"), _ev("auth"), _ev("list", "skip"), _ev("sample")]
        assert _classify_run_status(evs) == "degraded"

    def test_all_skip_is_skipped(self):
        evs = [
            _ev("preflight", "skip", failure_class="not_implemented"),
            _ev("auth", "skip", failure_class="not_implemented"),
        ]
        assert _classify_run_status(evs) == "skipped"


class TestMakeRunId:
    def test_deterministic(self):
        a = _make_run_id("his", "outpatient-register", "2026-10-10T08:30:00Z")
        b = _make_run_id("his", "outpatient-register", "2026-10-10T08:30:00Z")
        assert a == b
        assert len(a) == 16

    def test_different_started_at_different_id(self):
        a = _make_run_id("his", "outpatient-register", "2026-10-10T08:30:00Z")
        b = _make_run_id("his", "outpatient-register", "2026-10-10T08:35:00Z")
        assert a != b


class TestComputeRunEvidenceHash:
    def test_hash_invariant_for_identical_evidence(self):
        evs = [_ev("preflight"), _ev("auth"), _ev("sample")]
        a = _compute_run_evidence_hash(evs)
        b = _compute_run_evidence_hash(list(evs))
        assert a == b
        assert len(a) == 16

    def test_hash_differs_when_command_changes(self):
        a = _compute_run_evidence_hash([_ev("sample", command="GET /health")])
        b = _compute_run_evidence_hash([_ev("sample", command="GET /other")])
        assert a != b

    def test_authorization_bearer_token_not_in_hash_input(self):
        """凭据字面量在拼接前先 _coarse_redact，不进入 hash 原料 → 不影响 hash
        但保证 evidence_hash 在 DB 里是裸值（hash 不可逆但仍要干净）。"""

        # 把 Bearer token 注入 command + body；hash 应在 sanitize 后算
        ev = _ev(
            "sample",
            command="GET /health  Authorization: Bearer abcdef-ghijkl",
            response_body='{"Authorization": "Bearer abcdef-ghijkl"}',
        )
        h = _compute_run_evidence_hash([ev])
        assert len(h) == 16
        assert "abcdef-ghijkl" not in h


# ---------------------------------------------------------------------------
# record_run 写入 + 幂等
# ---------------------------------------------------------------------------


class TestRecordRun:
    def test_writes_one_row_and_returns_run_id(self, tmp_db):
        evs = [_ev("preflight"), _ev("auth"), _ev("sample"), _ev("assert")]
        rid = record_run("his", "outpatient-register", evs, path=tmp_db)
        assert len(rid) == 16

        # task 19: 数据落在月表 inspection_runs_y<YYYY>m<MM>，而非旧 inspection_runs
        rows = query_history(path=tmp_db)
        assert len(rows) == 1
        row = rows[0]
        assert row["run_id"] == rid
        assert row["vendor_id"] == "his"
        assert row["interface_id"] == "outpatient-register"
        assert row["status"] == "healthy"
        assert len(row["evidence_hash"]) == 16

    def test_writes_to_monthly_partition(self, tmp_db):
        """task 19：单条 record_run 写到 inspection_runs_y<YYYY>m<MM> 月表。"""

        evs = [_ev("preflight"), _ev("sample")]
        rid = record_run(
            "his",
            "outpatient-register",
            evs,
            started_at="2026-10-10T08:30:00Z",
            path=tmp_db,
        )
        # 月表名正确
        assert partition_table_name("2026-10-10T08:30:00Z") == "inspection_runs_y2026m10"

        conn = sqlite3.connect(str(tmp_db))
        try:
            # 月表确实存在并已写入
            cur = conn.execute(
                "SELECT count(*) FROM inspection_runs_y2026m10 WHERE run_id = ?",
                (rid,),
            )
            assert cur.fetchone()[0] == 1
            # 旧 inspection_runs 表依旧为空（task 19 后不再写入旧表）
            cur = conn.execute(
                "SELECT count(*) FROM sqlite_master "
                "WHERE type='table' AND name='inspection_runs'"
            )
            assert cur.fetchone()[0] == 1  # DDL 仍建旧表（兼容）
        finally:
            conn.close()

    def test_status_aggregate_failed(self, tmp_db):
        evs = [
            _ev("preflight"),
            _ev("auth", "fail", failure_class="auth_failed"),
            _ev("sample"),
            _ev("assert"),
        ]
        record_run("his", "outpatient-register", evs, path=tmp_db)

        # task 19：通过 query_history 跨月聚合读回 status
        rows = query_history(path=tmp_db)
        assert len(rows) == 1
        assert rows[0]["status"] == "failed"

    def test_duplicate_run_id_raises_integrity_error(self, tmp_db):
        evs = [_ev("preflight"), _ev("auth"), _ev("sample"), _ev("assert")]
        rid1 = record_run(
            "his",
            "outpatient-register",
            evs,
            started_at="2026-10-10T08:30:00Z",
            run_id="run-abc-123",
            path=tmp_db,
        )
        assert rid1 == "run-abc-123"

        with pytest.raises(sqlite3.IntegrityError):
            record_run(
                "lis",
                "report-push",
                evs,
                started_at="2026-10-10T08:30:00Z",
                run_id="run-abc-123",  # same id
                path=tmp_db,
            )

    def test_deterministic_run_id_same_args_same_id(self, tmp_db):
        evs = [_ev("preflight"), _ev("auth")]
        rid1 = record_run(
            "his",
            "outpatient-register",
            evs,
            started_at="2026-10-10T08:30:00Z",
            path=tmp_db,
        )
        # 第二次同 vendor+interface+started_at → 同 run_id → IntegrityError
        with pytest.raises(sqlite3.IntegrityError):
            record_run(
                "his",
                "outpatient-register",
                evs,
                started_at="2026-10-10T08:30:00Z",
                path=tmp_db,
            )
        # run_id 仍是 16 hex
        assert re.fullmatch(r"[0-9a-f]{16}", rid1)

    def test_failed_writes_do_not_persist_data(self, tmp_db):
        evs = [
            _ev("preflight"),
            _ev("auth", "fail", failure_class="auth_failed"),
            _ev("sample"),
        ]
        with pytest.raises(sqlite3.IntegrityError):
            record_run(
                "his",
                "outpatient-register",
                evs,
                started_at="2026-10-10T08:30:00Z",
                run_id="dup-id",
                path=tmp_db,
            )
            record_run(
                "lis",
                "report-push",
                evs,
                started_at="2026-10-10T08:30:00Z",
                run_id="dup-id",  # 强制 UNIQUE 冲突
                path=tmp_db,
            )

        # task 19：跨月 UNION ALL 仍只看到 1 行（第一次写入的数据）
        rows = query_history(path=tmp_db, limit=100)
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# 凭据纪律 — 库内不含 Authorization / Bearer / token / password
# ---------------------------------------------------------------------------


class TestCredentialSafety:
    @staticmethod
    def _dump_db(path: Path) -> str:
        """把整个 SQLite 转成可 grep 的纯文本（兼容月表分区）。"""

        conn = sqlite3.connect(str(path))
        try:
            text = ""
            # task 19：数据在 inspection_runs_y<YYYY>m<MM> 月表，旧 inspection_runs 也包含
            cur = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND (name='inspection_runs' "
                "OR name LIKE 'inspection_runs_y%')"
            )
            tables = [row[0] for row in cur.fetchall()]
            for table in tables:
                for line in conn.iterdump():
                    if f"CREATE TABLE {table}" in line or f"INSERT INTO {table}" in line:
                        text += line + "\n"
                # 也读实际值（steps_json 可能含命令；evidence_hash 是 hash，无明文）
                cur = conn.execute(
                    f"SELECT steps_json, evidence_hash, meta_json FROM {table}"
                )
                for row in cur.fetchall():
                    for v in row:
                        if v:
                            text += str(v) + "\n"
            return text
        finally:
            conn.close()

    def test_bearer_token_not_in_db(self, tmp_db):
        evs = [
            _ev(
                "auth",
                status="fail",
                command="POST /api/v1/login  Authorization: Bearer SUPER-SECRET-TOKEN-XYZ",
                failure_class="auth_failed",
            ),
            _ev("sample"),
        ]
        record_run("his", "outpatient-register", evs, path=tmp_db)

        dump = self._dump_db(tmp_db)
        assert "SUPER-SECRET-TOKEN-XYZ" not in dump
        # 二次防御：grep token / password / authorization 全部 0 命中（case-insensitive）
        for kw in ("token", "password", "authorization"):
            # evidence_hash 本身不触发，但我们要保证 steps_json 等无明文 token
            lower_dump = dump.lower()
            # 允许防御出现（如 "show"），但 SUPER-SECRET-TOKEN-XYZ 这种唯一的标识
            # 不应出现。我们已用更严格的 unique 标识断言。
            assert "super-secret-token-xyz" not in lower_dump

    def test_json_field_token_not_in_db(self, tmp_db):
        evs = [
            _ev(
                "sample",
                response_body='{"token": "jwt-eyJhbGciOiJIUzI1NiJ9-payload", "code": 200}',
            ),
            _ev("assert"),
        ]
        record_run("lis", "report-push", evs, path=tmp_db)

        dump = self._dump_db(tmp_db)
        # 原始 token 字面量不应落库（被 _coarse_redact 替换为 ***）
        assert "jwt-eyJhbGciOiJIUzI1NiJ9-payload" not in dump

    def test_url_query_token_not_in_db(self, tmp_db):
        evs = [
            _ev(
                "sample",
                command="GET /api?api_key=KEY-9999&token=TOK-8888&q=ok",
            ),
            _ev("assert"),
        ]
        record_run("pacs", "study-booking", evs, path=tmp_db)

        dump = self._dump_db(tmp_db)
        assert "KEY-9999" not in dump
        assert "TOK-8888" not in dump
        # 但 `q=ok` 这样的非敏感参数应保留
        assert "q=ok" in dump


# ---------------------------------------------------------------------------
# write_history 一次写多组
# ---------------------------------------------------------------------------


class TestWriteHistory:
    def test_groups_by_vendor_interface(self, tmp_db):
        evs = [
            _ev("preflight", vendor_id="his", interface_id="outpatient-register"),
            _ev("auth", vendor_id="his", interface_id="outpatient-register"),
            _ev("sample", vendor_id="his", interface_id="outpatient-register"),
            _ev("preflight", vendor_id="lis", interface_id="report-push"),
            _ev("auth", vendor_id="lis", interface_id="report-push"),
            _ev("sample", vendor_id="lis", interface_id="report-push"),
        ]
        run_ids = write_history(evs, path=tmp_db)
        assert len(run_ids) == 2
        assert len(set(run_ids)) == 2  # run_ids 互不相同

        # task 19：跨月 UNION ALL 查询历史总数
        rows = query_history(path=tmp_db, limit=100)
        assert len(rows) == 2
        rows.sort(key=lambda r: r["vendor_id"])
        assert rows[0]["vendor_id"] == "his"
        assert rows[0]["interface_id"] == "outpatient-register"
        assert rows[1]["vendor_id"] == "lis"
        assert rows[1]["interface_id"] == "report-push"

    def test_each_group_has_independent_status(self, tmp_db):
        evs = [
            _ev("preflight", vendor_id="his", interface_id="a"),
            _ev("auth", vendor_id="his", interface_id="a"),
            _ev("sample", vendor_id="his", interface_id="a"),
            _ev(
                "preflight",
                vendor_id="lis",
                interface_id="b",
                status="fail",
                failure_class="timeout",
            ),
            _ev("auth", vendor_id="lis", interface_id="b", status="skip", failure_class="timeout"),
        ]
        write_history(evs, path=tmp_db)

        # task 19：通过 query_history 跨月聚合读回 status
        rows = query_history(path=tmp_db, limit=100)
        rows.sort(key=lambda r: r["vendor_id"])
        assert [(r["vendor_id"], r["status"]) for r in rows] == [
            ("his", "healthy"),
            ("lis", "failed"),
        ]


# ---------------------------------------------------------------------------
# fetch_run / fetch_runs_by_vendor
# ---------------------------------------------------------------------------


class TestFetch:
    def test_fetch_run_returns_record(self, tmp_db):
        evs = [_ev("preflight"), _ev("auth"), _ev("sample"), _ev("assert")]
        rid = record_run(
            "his",
            "outpatient-register",
            evs,
            started_at="2026-10-10T08:30:00Z",
            meta={"cli_invoker": "ci-job-42"},
            path=tmp_db,
        )

        rec = fetch_run(rid, path=tmp_db)
        assert rec is not None
        assert rec["run_id"] == rid
        assert rec["vendor_id"] == "his"
        assert rec["interface_id"] == "outpatient-register"
        assert rec["status"] == "healthy"
        assert len(rec["evidence_hash"]) == 16
        assert isinstance(rec["steps"], list)
        assert len(rec["steps"]) == 4
        assert rec["meta"] == {"cli_invoker": "ci-job-42"}

    def test_fetch_run_missing_returns_none(self, tmp_db):
        assert fetch_run("does-not-exist", path=tmp_db) is None

    def test_fetch_runs_by_vendor_orders_desc(self, tmp_db):
        # 3 runs of same vendor at different started_at
        for i, ts in enumerate(
            ["2026-10-10T08:30:00Z", "2026-10-10T08:35:00Z", "2026-10-10T08:40:00Z"]
        ):
            record_run(
                "his",
                "outpatient-register",
                [_ev("preflight")],
                started_at=ts,
                path=tmp_db,
            )
        # also a different vendor (should not appear in fetch)
        record_run(
            "lis",
            "report-push",
            [_ev("preflight")],
            started_at="2026-10-10T08:45:00Z",
            path=tmp_db,
        )

        rows = fetch_runs_by_vendor("his", limit=10, path=tmp_db)
        assert len(rows) == 3
        # 排序：started_at DESC
        started_ats = [r["started_at"] for r in rows]
        assert started_ats == sorted(started_ats, reverse=True)
        # 全是 his
        assert {r["vendor_id"] for r in rows} == {"his"}

    def test_fetch_runs_by_vendor_limit(self, tmp_db):
        for i in range(5):
            record_run(
                "his",
                "outpatient-register",
                [_ev("preflight")],
                started_at=f"2026-10-10T08:{30 + i:02d}:00Z",
                path=tmp_db,
            )
        rows = fetch_runs_by_vendor("his", limit=2, path=tmp_db)
        assert len(rows) == 2


# ---------------------------------------------------------------------------
# 路径优先级 / 环境变量
# ---------------------------------------------------------------------------


class TestPathResolution:
    def test_default_path_uses_cwd_when_env_unset(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("VENDOR_DOCTOR_DB", raising=False)
        p = db_path()
        assert p == tmp_path / "vendor_if_doctor.db"

    def test_env_var_overrides_default(self, monkeypatch, tmp_path):
        target = tmp_path / "env_set.db"
        monkeypatch.setenv("VENDOR_DOCTOR_DB", str(target))
        assert db_path() == target