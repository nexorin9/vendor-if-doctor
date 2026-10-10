"""Unit tests for SQLite monthly partitioning (task 19).

覆盖：
  * partition_table_name / _partition_suffix / _partition_ddl 命名与表名正确性
  * ensure_partition 幂等建表 + 索引（idx_run_id_<suffix> / idx_vendor_id_<suffix> / idx_started_at_<suffix>）
  * record_run 按 started_at 月份路由到 inspection_runs_y<YYYY>m<MM>
  * 跨月 UNION ALL：3 个不同月份（2026-10 / 2026-11 / 2026-12）各写 1 条 → 3 张分表各 1 行 → 总数 3
  * query_history 按 started_at DESC 排序 + LIMIT / OFFSET 分页
  * count_history 跨月总数
  * fetch_run / fetch_runs_by_vendor 跨月 UNION ALL 查询
  * .schema inspection_runs_y2026m10 输出含 idx_run_id / idx_vendor_id / idx_started_at 子串
  * 索引存在性校验：sqlite_master 列出三个索引名
  * 凭据纪律：月表内步骤 JSON 与 evidence_hash 同样不泄漏 token
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from vendor_if_doctor.connection.evidence import Evidence
from vendor_if_doctor.renderers.sqlite_history import (
    _existing_storage_tables,
    _partition_ddl,
    _partition_suffix,
    connect,
    count_history,
    db_path,
    ensure_partition,
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
    return tmp_path / "vendor_if_doctor.db"


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch, tmp_path: Path):
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
# partition_table_name / _partition_suffix
# ---------------------------------------------------------------------------


class TestPartitionNaming:
    def test_partition_suffix_format(self):
        assert _partition_suffix("2026-10-10T08:30:00Z") == "y2026m10"
        assert _partition_suffix("2026-11-01T00:00:00Z") == "y2026m11"
        assert _partition_suffix("2026-12-31T23:59:59Z") == "y2026m12"
        assert _partition_suffix("2027-01-01T00:00:00Z") == "y2027m01"

    def test_partition_table_name_format(self):
        assert (
            partition_table_name("2026-10-10T08:30:00Z") == "inspection_runs_y2026m10"
        )
        assert (
            partition_table_name("2026-11-15T12:00:00Z") == "inspection_runs_y2026m11"
        )

    def test_partition_suffix_handles_timezone_offsets(self):
        # 含 +HH:MM 偏移
        assert _partition_suffix("2026-10-10T08:30:00+08:00") == "y2026m10"
        assert _partition_suffix("2026-10-10T00:30:00+00:00") == "y2026m10"


# ---------------------------------------------------------------------------
# ensure_partition / _partition_ddl
# ---------------------------------------------------------------------------


class TestEnsurePartition:
    def test_ddl_contains_required_columns(self):
        ddl = _partition_ddl("y2026m10")
        # 必含字段
        for col in (
            "run_id", "vendor_id", "interface_id",
            "started_at", "finished_at", "status",
            "steps_json", "evidence_hash", "meta_json",
        ):
            assert col in ddl, f"DDL missing column: {col}"

    def test_ddl_creates_three_indices(self):
        ddl = _partition_ddl("y2026m10")
        assert "CREATE INDEX IF NOT EXISTS idx_run_id_y2026m10" in ddl
        assert "CREATE INDEX IF NOT EXISTS idx_vendor_id_y2026m10" in ddl
        assert "CREATE INDEX IF NOT EXISTS idx_started_at_y2026m10" in ddl

    def test_ddl_indices_reference_substrings_for_schema_check(self):
        """任务验证点：``.schema <table>`` 含 idx_run_id / idx_vendor_id / idx_started_at 子串。"""

        ddl = _partition_ddl("y2026m10")
        # 索引名带 _y2026m10 后缀，但保留 idx_run_id / idx_vendor_id 子串
        assert "idx_run_id_y2026m10" in ddl
        assert "idx_vendor_id_y2026m10" in ddl
        assert "idx_started_at_y2026m10" in ddl

    def test_ensure_partition_creates_table(self, tmp_db):
        conn = connect(tmp_db)
        try:
            table = ensure_partition(conn, "2026-10-10T08:30:00Z")
            assert table == "inspection_runs_y2026m10"
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            )
            assert cur.fetchone() is not None
        finally:
            conn.close()

    def test_ensure_partition_idempotent(self, tmp_db):
        conn = connect(tmp_db)
        try:
            ensure_partition(conn, "2026-10-10T08:30:00Z")
            ensure_partition(conn, "2026-10-10T08:30:00Z")
            cur = conn.execute(
                "SELECT count(*) FROM sqlite_master "
                "WHERE type='table' AND name='inspection_runs_y2026m10'"
            )
            assert cur.fetchone()[0] == 1
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# record_run → 月表路由
# ---------------------------------------------------------------------------


class TestRecordRunPartition:
    def test_record_run_writes_to_correct_monthly_table(self, tmp_db):
        evs = [_ev("preflight"), _ev("sample")]
        rid = record_run(
            "his",
            "outpatient-register",
            evs,
            started_at="2026-10-10T08:30:00Z",
            path=tmp_db,
        )
        conn = sqlite3.connect(str(tmp_db))
        try:
            # 数据确实在 y2026m10
            cur = conn.execute(
                "SELECT count(*) FROM inspection_runs_y2026m10 WHERE run_id = ?",
                (rid,),
            )
            assert cur.fetchone()[0] == 1
            # 旧 inspection_runs 不应有数据（task 19 后不再写入旧表）
            cur = conn.execute(
                "SELECT count(*) FROM inspection_runs WHERE run_id = ?",
                (rid,),
            )
            assert cur.fetchone()[0] == 0
        finally:
            conn.close()

    def test_record_run_creates_three_indices_per_partition(self, tmp_db):
        record_run(
            "his",
            "outpatient-register",
            [_ev("preflight")],
            started_at="2026-10-10T08:30:00Z",
            path=tmp_db,
        )
        conn = sqlite3.connect(str(tmp_db))
        try:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='inspection_runs_y2026m10'"
            )
            names = {row[0] for row in cur.fetchall()}
            assert "idx_run_id_y2026m10" in names
            assert "idx_vendor_id_y2026m10" in names
            assert "idx_started_at_y2026m10" in names
        finally:
            conn.close()

    def test_schema_dump_contains_index_substrings(self, tmp_db):
        """任务验证点：``.schema inspection_runs_y2026m10`` 含 idx_run_id / idx_vendor_id。"""

        record_run(
            "his",
            "outpatient-register",
            [_ev("preflight")],
            started_at="2026-10-10T08:30:00Z",
            path=tmp_db,
        )
        conn = sqlite3.connect(str(tmp_db))
        try:
            # 用 sqlite_master 模拟 .schema <table> 的输出
            cur = conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE (type='table' OR type='index') "
                "AND (tbl_name='inspection_runs_y2026m10' OR name='inspection_runs_y2026m10')"
            )
            dump = "\n".join(row[0] or "" for row in cur.fetchall())
            assert "idx_run_id" in dump
            assert "idx_vendor_id" in dump
            assert "idx_started_at" in dump
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 跨月 UNION ALL
# ---------------------------------------------------------------------------


class TestCrossMonthUnion:
    def test_three_months_three_partitions_each_one_row(self, tmp_db):
        """任务验证点：连续 3 次 run 跨 2026-10/11/12 月 → 3 张分表各 1 行 → UNION ALL 后总数 3。"""

        evs = [_ev("preflight"), _ev("auth"), _ev("sample"), _ev("assert")]
        for month in ("10", "11", "12"):
            record_run(
                "his",
                "outpatient-register",
                evs,
                started_at=f"2026-{month}-15T08:30:00Z",
                path=tmp_db,
            )

        conn = sqlite3.connect(str(tmp_db))
        try:
            for month in ("10", "11", "12"):
                table = f"inspection_runs_y2026m{month}"
                cur = conn.execute(f"SELECT count(*) FROM {table}")
                assert cur.fetchone()[0] == 1, f"{table} 应有 1 行"
        finally:
            conn.close()

        # query_history 跨月 UNION ALL 总数 = 3
        rows = query_history(path=tmp_db, limit=100)
        assert len(rows) == 3

    def test_query_history_orders_by_started_at_desc(self, tmp_db):
        """任务验证点：query_history(limit=10) 返回最新 10 条按 started_at DESC。"""

        evs = [_ev("preflight"), _ev("sample")]
        # 倒序写入，但 DESC 排序应让 12 月排第一
        for month in ("10", "11", "12"):
            record_run(
                "lis",
                "report-push",
                evs,
                started_at=f"2026-{month}-15T08:30:00Z",
                path=tmp_db,
            )

        rows = query_history(path=tmp_db, limit=10)
        assert len(rows) == 3
        # started_at DESC: 12 月 → 11 月 → 10 月
        started_ats = [r["started_at"] for r in rows]
        assert started_ats == sorted(started_ats, reverse=True)
        assert started_ats[0].startswith("2026-12-")

    def test_query_history_with_vendor_filter(self, tmp_db):
        evs = [_ev("preflight"), _ev("sample")]
        record_run(
            "his",
            "outpatient-register",
            evs,
            started_at="2026-10-10T08:30:00Z",
            path=tmp_db,
        )
        record_run(
            "lis",
            "report-push",
            evs,
            started_at="2026-11-10T08:30:00Z",
            path=tmp_db,
        )
        record_run(
            "his",
            "outpatient-register",
            evs,
            started_at="2026-12-10T08:30:00Z",
            path=tmp_db,
        )

        # vendor_id='his' 应跨 10/12 月过滤到 2 条
        rows = query_history(vendor_id="his", path=tmp_db, limit=10)
        assert len(rows) == 2
        assert all(r["vendor_id"] == "his" for r in rows)

    def test_count_history_across_months(self, tmp_db):
        evs = [_ev("preflight"), _ev("sample")]
        for month in ("10", "11", "12"):
            record_run(
                "his",
                "outpatient-register",
                evs,
                started_at=f"2026-{month}-15T08:30:00Z",
                path=tmp_db,
            )
        assert count_history(path=tmp_db) == 3
        assert count_history(vendor_id="his", path=tmp_db) == 3
        assert count_history(vendor_id="lis", path=tmp_db) == 0


# ---------------------------------------------------------------------------
# 分页 (limit / offset)
# ---------------------------------------------------------------------------


class TestPagination:
    def test_pagination_limit_offset(self, tmp_db):
        """跨月数据用 limit / offset 分页取回正确切片。"""

        evs = [_ev("preflight"), _ev("sample")]
        # 写入 10 条跨 3 个月（10 月 4 条、11 月 3 条、12 月 3 条）
        for month, day in [
            ("10", 1), ("10", 2), ("10", 3), ("10", 4),
            ("11", 1), ("11", 2), ("11", 3),
            ("12", 1), ("12", 2), ("12", 3),
        ]:
            record_run(
                "his",
                "outpatient-register",
                evs,
                started_at=f"2026-{month}-{day:02d}T08:30:00Z",
                path=tmp_db,
            )

        total = count_history(path=tmp_db)
        assert total == 10

        # 第一页：最新 5 条（DESC 排序）
        page1 = query_history(path=tmp_db, limit=5, offset=0)
        assert len(page1) == 5
        started_p1 = [r["started_at"] for r in page1]
        assert started_p1 == sorted(started_p1, reverse=True)

        # 第二页：剩余 5 条
        page2 = query_history(path=tmp_db, limit=5, offset=5)
        assert len(page2) == 5
        started_p2 = [r["started_at"] for r in page2]
        assert started_p2 == sorted(started_p2, reverse=True)

        # 第一页和第二页无重叠
        ids_p1 = {r["run_id"] for r in page1}
        ids_p2 = {r["run_id"] for r in page2}
        assert ids_p1.isdisjoint(ids_p2)

        # 合并 = 全部
        all_ids = ids_p1 | ids_p2
        assert len(all_ids) == 10

    def test_pagination_offset_beyond_total(self, tmp_db):
        evs = [_ev("preflight"), _ev("sample")]
        for month in ("10", "11"):
            record_run(
                "his",
                "outpatient-register",
                evs,
                started_at=f"2026-{month}-15T08:30:00Z",
                path=tmp_db,
            )
        rows = query_history(path=tmp_db, limit=10, offset=100)
        assert rows == []

    def test_negative_limit_raises(self, tmp_db):
        with pytest.raises(ValueError):
            query_history(path=tmp_db, limit=-1)

    def test_negative_offset_raises(self, tmp_db):
        with pytest.raises(ValueError):
            query_history(path=tmp_db, offset=-1)


# ---------------------------------------------------------------------------
# fetch_run / fetch_runs_by_vendor 跨月查询
# ---------------------------------------------------------------------------


class TestFetchAcrossPartitions:
    def test_fetch_run_searches_all_partitions(self, tmp_db):
        evs = [_ev("preflight"), _ev("sample"), _ev("assert")]
        rid_oct = record_run(
            "his",
            "outpatient-register",
            evs,
            started_at="2026-10-10T08:30:00Z",
            path=tmp_db,
        )
        rid_nov = record_run(
            "his",
            "outpatient-register",
            evs,
            started_at="2026-11-10T08:30:00Z",
            path=tmp_db,
        )
        rid_dec = record_run(
            "lis",
            "report-push",
            evs,
            started_at="2026-12-10T08:30:00Z",
            path=tmp_db,
        )

        for rid in (rid_oct, rid_nov, rid_dec):
            rec = fetch_run(rid, path=tmp_db)
            assert rec is not None
            assert rec["run_id"] == rid

    def test_fetch_run_returns_none_for_missing(self, tmp_db):
        record_run(
            "his",
            "outpatient-register",
            [_ev("preflight")],
            started_at="2026-10-10T08:30:00Z",
            path=tmp_db,
        )
        assert fetch_run("does-not-exist", path=tmp_db) is None

    def test_fetch_runs_by_vendor_across_months(self, tmp_db):
        evs = [_ev("preflight"), _ev("sample")]
        for month in ("10", "11", "12"):
            record_run(
                "his",
                "outpatient-register",
                evs,
                started_at=f"2026-{month}-15T08:30:00Z",
                path=tmp_db,
            )
        # lis 跨一个月
        record_run(
            "lis",
            "report-push",
            evs,
            started_at="2026-10-20T08:30:00Z",
            path=tmp_db,
        )

        his_rows = fetch_runs_by_vendor("his", limit=10, path=tmp_db)
        assert len(his_rows) == 3
        assert {r["vendor_id"] for r in his_rows} == {"his"}
        # DESC 排序
        assert [r["started_at"] for r in his_rows] == sorted(
            [r["started_at"] for r in his_rows], reverse=True
        )


# ---------------------------------------------------------------------------
# _existing_storage_tables 探测
# ---------------------------------------------------------------------------


class TestExistingStorageTables:
    def test_lists_legacy_and_monthly_tables(self, tmp_db):
        record_run(
            "his",
            "outpatient-register",
            [_ev("preflight")],
            started_at="2026-10-10T08:30:00Z",
            path=tmp_db,
        )
        record_run(
            "his",
            "outpatient-register",
            [_ev("preflight")],
            started_at="2026-11-10T08:30:00Z",
            path=tmp_db,
        )
        conn = connect(tmp_db)
        try:
            tables = _existing_storage_tables(conn)
        finally:
            conn.close()
        assert "inspection_runs" in tables
        assert "inspection_runs_y2026m10" in tables
        assert "inspection_runs_y2026m11" in tables
        assert "inspection_runs_y2026m12" not in tables

    def test_empty_database_lists_legacy_only(self, tmp_db):
        conn = connect(tmp_db)
        try:
            tables = _existing_storage_tables(conn)
        finally:
            conn.close()
        # 旧 inspection_runs 一定在（DDL 保证）；月表未创建
        assert "inspection_runs" in tables
        assert all(t.startswith("inspection_runs_y") is False or t == "inspection_runs" for t in tables)


# ---------------------------------------------------------------------------
# 凭据纪律：月表内 JSON 同样脱敏
# ---------------------------------------------------------------------------


class TestPartitionCredentialSafety:
    def test_steps_json_in_monthly_table_no_token(self, tmp_db):
        evs = [
            _ev(
                "sample",
                command="GET /api  Authorization: Bearer SUPER-SECRET-TOKEN-XYZ",
            ),
            _ev("assert"),
        ]
        record_run(
            "his",
            "outpatient-register",
            evs,
            started_at="2026-10-10T08:30:00Z",
            path=tmp_db,
        )
        conn = sqlite3.connect(str(tmp_db))
        try:
            cur = conn.execute(
                "SELECT steps_json FROM inspection_runs_y2026m10"
            )
            (raw,) = cur.fetchone()
            assert "SUPER-SECRET-TOKEN-XYZ" not in raw
            assert "***" in raw  # 已 redact
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# write_history 跨月写入
# ---------------------------------------------------------------------------


class TestWriteHistoryAcrossMonths:
    def test_write_history_uses_default_started_at(self, tmp_db):
        """write_history 默认 started_at = 调用时刻 → 全部 run 落同一当前月表。"""

        evs = [
            _ev("preflight", vendor_id="his", interface_id="a"),
            _ev("sample", vendor_id="his", interface_id="a"),
            _ev("preflight", vendor_id="lis", interface_id="b"),
            _ev("sample", vendor_id="lis", interface_id="b"),
        ]
        run_ids = write_history(evs, path=tmp_db)
        assert len(run_ids) == 2

        # 默认 started_at 走 _now_utc_iso()，两个 run 落到同一当前月表
        conn = sqlite3.connect(str(tmp_db))
        try:
            cur = conn.execute(
                "SELECT count(*) FROM sqlite_master "
                "WHERE type='table' AND name LIKE 'inspection_runs_y%'"
            )
            assert cur.fetchone()[0] >= 1
        finally:
            conn.close()

        # query_history 仍能读到全部 2 条
        rows = query_history(path=tmp_db, limit=10)
        assert len(rows) == 2
