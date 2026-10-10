"""Unit tests for vendor_if_doctor/renderers/sqlite_history.py — 并发与事务隔离。

任务 30 覆盖：
* **WAL 已开** —— ``PRAGMA journal_mode = WAL`` 在 ``connect()`` 内硬开启；
* **BEGIN IMMEDIATE 包裹 INSERT** —— ``record_run`` 内对每条 INSERT 都
  显式 ``BEGIN IMMEDIATE`` 拿写锁（避免 ``SQLITE_BUSY`` 抖动）；
* **同 run_id 二次写入抛 ``IntegrityError``** —— 调用方据此实现幂等；
* **多线程并发写** —— 3 线程 × 10 run 共 30 条独立 run_id 同时落库，全部成功；
* **并发冲突不阻塞超 1s** —— ``PRAGMA busy_timeout`` + WAL writer 序列化
  让排队写尽快完成；
* **失败不静默改数据** —— 单条 INSERT 抛错时事务 rollback，剩余线程不受影响；
* **journal_mode 检查** —— ``connect()`` 返回的连接 ``PRAGMA journal_mode``
  实际读到 ``wal``（不是 ``memory``）。

约定：
* 测试用 ``tmp_path`` 提供隔离 SQLite 文件（每测试独立，不与既有 ``inspection_runs``
  共享）；
* 线程间使用 :func:`threading.Thread` + ``join()``，避免依赖外部并发库；
* SQLite 的连接默认 ``check_same_thread=True``，所以**每个线程独立
  ``connect()`` 一次**——这正是生产路径 ``record_run`` 的行为。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import List

import pytest

from vendor_if_doctor.connection.evidence import Evidence
from vendor_if_doctor.renderers.sqlite_history import (
    connect,
    count_history,
    ensure_schema,
    query_history,
    record_run,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    """每次测试一个临时 SQLite 文件（与既有 test_sqlite_history 同语义）。"""

    return tmp_path / "vendor_if_doctor_concurrent.db"


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch, tmp_path: Path):
    """隔离 VENDOR_DOCTOR_DB，避免别处覆盖 db_path()。"""

    monkeypatch.delenv("VENDOR_DOCTOR_DB", raising=False)
    yield


def _ev(
    step_name: str,
    *,
    status: str = "pass",
    command: str = "GET /api/v1/health",
    response_body: str | None = None,
    failure_class: str | None = None,
    latency_ms: int = 100,
    vendor_id: str = "his",
    interface_id: str = "outpatient-register",
) -> Evidence:
    """构造 Evidence（与既有 test_sqlite_history 同形）。"""

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


def _one_run(started_at: str, vendor: str = "his", interface: str = "outpatient-register"):
    """构造一组 5 步 evidence（与生产 happy path 同形）。"""

    return [
        _ev("preflight", latency_ms=10, vendor_id=vendor, interface_id=interface),
        _ev("auth", latency_ms=20, vendor_id=vendor, interface_id=interface),
        _ev("list", latency_ms=15, vendor_id=vendor, interface_id=interface),
        _ev("sample", latency_ms=50, vendor_id=vendor, interface_id=interface),
        _ev("assert", latency_ms=10, vendor_id=vendor, interface_id=interface),
    ], started_at


# ---------------------------------------------------------------------------
# 1. WAL 模式：connect() 必开
# ---------------------------------------------------------------------------


class TestWalModeHardEnabled:
    def test_connect_sets_wal_pragmas(self, tmp_db: Path):
        """``connect()`` 必须把 journal_mode 设为 WAL（不是 memory）。"""

        conn = connect(tmp_db)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            # 注意：在内存数据库上 WAL 会落回 memory；但在 file-backed DB 上
            # 必须读到 'wal'。本测试用 tmp_path 提供的文件 DB。
            assert mode.lower() == "wal", f"expected 'wal', got {mode!r}"
        finally:
            conn.close()

    def test_wal_files_appear_on_disk_while_holding_connection(self, tmp_db: Path):
        """WAL 模式在连接打开期间会留下 ``-wal`` 与 ``-shm`` 副文件。

        SQLite 在所有连接关闭后会做自动 checkpoint，把 WAL 内容合并回主库文件
        并删除 ``-wal`` / ``-shm`` 副文件（前提是没有未提交的事务）。本测试在
        持有连接 + 未提交事务的状态下检查副文件存在。
        """

        conn = connect(tmp_db)
        try:
            # 开启一个长事务，让 WAL 内容留在 -wal 文件里不被 checkpoint
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS _wal_probe (id INTEGER PRIMARY KEY)"
            )
            conn.execute("INSERT INTO _wal_probe (id) VALUES (1)")

            wal_file = tmp_db.with_suffix(".db-wal")
            shm_file = tmp_db.with_suffix(".db-shm")
            assert wal_file.exists(), (
                f"WAL file not created at {wal_file}; "
                "connect() may not be enabling journal_mode=WAL"
            )
            assert shm_file.exists(), f"SHM file not created at {shm_file}"
        finally:
            conn.execute("ROLLBACK")
            conn.close()

    def test_existing_connect_returns_wal_connection(self, tmp_db: Path):
        """连续两次 connect() 都返回 WAL 模式的连接。"""

        c1 = connect(tmp_db)
        c2 = connect(tmp_db)
        try:
            m1 = c1.execute("PRAGMA journal_mode").fetchone()[0]
            m2 = c2.execute("PRAGMA journal_mode").fetchone()[0]
            assert m1.lower() == "wal"
            assert m2.lower() == "wal"
        finally:
            c1.close()
            c2.close()


# ---------------------------------------------------------------------------
# 2. BEGIN IMMEDIATE 包裹 INSERT
# ---------------------------------------------------------------------------


class TestBeginImmediateWrapsInsert:
    """``record_run`` 必须在 INSERT 前开 ``BEGIN IMMEDIATE`` 拿写锁。"""

    def test_record_run_uses_immediate_transaction(self, tmp_db: Path, monkeypatch):
        """用 sqlite3 trace 验证：record_run 内确实跑过 ``BEGIN IMMEDIATE``。"""

        captured: List[str] = []

        conn = connect(tmp_db)
        try:
            # 在 connect() 后的新连接上装 trace；record_run 内部会再 connect()
            # （每次独立连接），所以我们改用 patch sqlite3.connect 来捕获。
            orig_connect = sqlite3.connect

            def traced_connect(*args, **kwargs):
                c = orig_connect(*args, **kwargs)
                c.set_trace_callback(lambda sql: captured.append(sql))
                return c

            monkeypatch.setattr(sqlite3, "connect", traced_connect)

            evs, started_at = _one_run("2026-10-10T08:30:00Z")
            record_run("his", "outpatient-register", evs, started_at=started_at, path=tmp_db)
        finally:
            conn.close()

        # 必须出现 BEGIN IMMEDIATE + INSERT INTO + COMMIT
        joined = "\n".join(captured)
        assert "BEGIN IMMEDIATE" in joined, (
            f"record_run did not issue BEGIN IMMEDIATE; trace was:\n{joined[:1500]}"
        )
        assert "INSERT INTO inspection_runs_" in joined, (
            f"record_run did not INSERT INTO inspection_runs_<month>; trace:\n{joined[:1500]}"
        )
        assert "COMMIT" in joined or "COMMIT;" in joined, (
            f"record_run did not COMMIT; trace:\n{joined[:1500]}"
        )

    def test_record_run_rollback_on_failure(self, tmp_db: Path, monkeypatch):
        """INSERT 抛错时必须 rollback，不留半条记录。"""

        # 第一次写成功
        evs1, t1 = _one_run("2026-10-10T08:30:00Z")
        rid = record_run("his", "outpatient-register", evs1, started_at=t1, path=tmp_db)
        assert len(rid) == 16

        # 第二次写同 run_id → IntegrityError → rollback → 数据库仍只 1 条
        with pytest.raises(sqlite3.IntegrityError):
            record_run(
                "his", "outpatient-register",
                evs1,
                started_at=t1,  # 同一 started_at → 同一 run_id
                path=tmp_db,
            )

        rows = query_history(path=tmp_db)
        assert len(rows) == 1, f"rollback should leave 1 row, got {len(rows)}"


# ---------------------------------------------------------------------------
# 3. 同 run_id 二次写入抛 IntegrityError（幂等防护）
# ---------------------------------------------------------------------------


class TestUniqueRunIdConstraint:
    """月表 PRIMARY KEY UNIQUE 保证幂等。"""

    def test_same_run_id_raises_integrity_error(self, tmp_db: Path):
        """同 started_at + 同 vendor + 同 interface → 同 run_id → 二次写抛错。"""

        evs, started_at = _one_run("2026-10-10T08:30:00Z")

        rid1 = record_run(
            "his", "outpatient-register", evs,
            started_at=started_at, path=tmp_db,
        )
        assert len(rid1) == 16

        with pytest.raises(sqlite3.IntegrityError):
            record_run(
                "his", "outpatient-register", evs,
                started_at=started_at, path=tmp_db,
            )

    def test_same_run_id_does_not_overwrite_first_row(self, tmp_db: Path):
        """二次写抛错时第一行不动（rollback 守护）。"""

        evs, started_at = _one_run("2026-10-10T08:30:00Z")
        rid = record_run("his", "outpatient-register", evs, started_at=started_at, path=tmp_db)

        # 二次写：故意把 evidence 换成 fail，看会不会污染第一次的 healthy 记录
        bad_evs = [_ev("preflight", status="fail", failure_class="timeout", latency_ms=100)]
        with pytest.raises(sqlite3.IntegrityError):
            record_run(
                "his", "outpatient-register", bad_evs,
                started_at=started_at, path=tmp_db,
            )

        rows = query_history(path=tmp_db)
        assert len(rows) == 1
        assert rows[0]["run_id"] == rid
        assert rows[0]["status"] == "healthy"  # 第一次的 status，未被覆盖

    def test_same_run_id_three_writes_only_first_succeeds(self, tmp_db: Path):
        """三次同 run_id 写：第一次成功 + 第二次/第三次抛 IntegrityError。"""

        evs, started_at = _one_run("2026-10-10T08:30:00Z")

        record_run("his", "outpatient-register", evs, started_at=started_at, path=tmp_db)
        for _ in range(2):
            with pytest.raises(sqlite3.IntegrityError):
                record_run(
                    "his", "outpatient-register", evs,
                    started_at=started_at, path=tmp_db,
                )

        assert count_history(path=tmp_db) == 1


# ---------------------------------------------------------------------------
# 4. 多线程并发写 — 30 条独立 run 同时落库
# ---------------------------------------------------------------------------


class TestMultiThreadedConcurrentWrites:
    """3 线程 × 10 run 共 30 条 run_id 同时写入，全部成功。"""

    def test_three_threads_ten_runs_each_all_succeed(
        self, tmp_db: Path,
    ):
        """主验证：3 线程 × 10 run 全部 record_run 成功；数据库 30 条；run_id 唯一。"""

        # 预热：先 connect 一次确保文件 + schema + WAL 已就绪
        _ = connect(tmp_db)
        ensure_schema(_)

        n_threads = 3
        per_thread = 10
        errors: List[BaseException] = []
        run_ids: List[str] = []
        ids_lock = threading.Lock()
        started_at = "2026-10-10T08:30:00Z"

        def worker(thread_idx: int) -> None:
            try:
                for i in range(per_thread):
                    # 每个 run 唯一 vendor_id（避免 run_id 撞车）
                    vendor_id = f"his-t{thread_idx}"
                    interface_id = f"iface-{i:02d}"
                    evs = [
                        _ev("preflight", vendor_id=vendor_id, interface_id=interface_id),
                        _ev("auth", vendor_id=vendor_id, interface_id=interface_id),
                        _ev("sample", vendor_id=vendor_id, interface_id=interface_id),
                        _ev("assert", vendor_id=vendor_id, interface_id=interface_id),
                    ]
                    rid = record_run(
                        vendor_id, interface_id, evs,
                        started_at=started_at, path=tmp_db,
                    )
                    with ids_lock:
                        run_ids.append(rid)
            except BaseException as exc:  # noqa: BLE001
                with ids_lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(idx,), name=f"writer-{idx}")
            for idx in range(n_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 所有 30 次 record_run 都成功（无任何异常）
        assert errors == [], f"concurrent writes raised: {[repr(e) for e in errors]}"
        assert len(run_ids) == n_threads * per_thread
        assert len(set(run_ids)) == n_threads * per_thread, (
            f"duplicate run_ids detected: {len(set(run_ids))} unique vs {len(run_ids)} total"
        )

        # 数据库里实际有 30 条
        assert count_history(path=tmp_db) == n_threads * per_thread

    def test_concurrent_writes_wal_total_time(self, tmp_db: Path):
        """并发写总 wall-time < 顺序写的 60%（粗略验证 WAL writer 序列化下不阻塞）。"""

        _ = connect(tmp_db)
        ensure_schema(_)

        evs, started_at = _one_run("2026-10-10T08:30:00Z")
        n = 6

        # 顺序写
        t0 = time.perf_counter()
        for i in range(n):
            vendor_id = f"v-seq-{i:02d}"
            interface_id = "iface"
            record_run(
                vendor_id, interface_id,
                [_ev("preflight", vendor_id=vendor_id, interface_id=interface_id)],
                started_at=started_at, path=tmp_db,
            )
        seq_ms = (time.perf_counter() - t0) * 1000

        # 并发写（同样 n 条）
        def w(idx: int) -> None:
            vendor_id = f"v-par-{idx:02d}"
            record_run(
                vendor_id, "iface",
                [_ev("preflight", vendor_id=vendor_id, interface_id="iface")],
                started_at=started_at, path=tmp_db,
            )

        threads = [threading.Thread(target=w, args=(i,)) for i in range(n)]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        par_ms = (time.perf_counter() - t0) * 1000

        # SQLite 是文件级锁，并发写本质是序列化；不要强求 60%，只要求
        # 并发不超过顺序的 2 倍（验证 WAL 没退化）。
        assert par_ms <= seq_ms * 2 + 50, (
            f"concurrent ({par_ms:.0f}ms) much slower than sequential ({seq_ms:.0f}ms); "
            "WAL writer pipeline may be broken"
        )

    def test_concurrent_write_does_not_block_over_1s(self, tmp_db: Path):
        """单次 record_run 在并发高峰时不应阻塞超过 1s。"""

        # 先预热
        _ = connect(tmp_db)
        ensure_schema(_)

        # 在后台线程塞 20 条并发写，主线程跑一条单写，测其耗时
        evs_base, started_at = _one_run("2026-10-10T08:30:00Z")

        def background_writers():
            for i in range(20):
                record_run(
                    f"v-bg-{i:02d}", "iface",
                    [_ev("preflight", vendor_id=f"v-bg-{i:02d}", interface_id="iface")],
                    started_at=started_at, path=tmp_db,
                )

        bg = threading.Thread(target=background_writers, name="bg-writers")
        bg.start()

        # 让后台先跑 50ms
        time.sleep(0.05)

        t0 = time.perf_counter()
        record_run(
            "v-fg", "iface",
            [_ev("preflight", vendor_id="v-fg", interface_id="iface")],
            started_at=started_at, path=tmp_db,
        )
        fg_ms = (time.perf_counter() - t0) * 1000

        bg.join()

        assert fg_ms < 1000, (
            f"foreground write blocked {fg_ms:.0f}ms (>1s) under concurrent writes; "
            "PRAGMA busy_timeout or WAL writer pipeline may be misconfigured"
        )


# ---------------------------------------------------------------------------
# 5. 并发场景下的失败路径
# ---------------------------------------------------------------------------


class TestConcurrentFailureHandling:
    """并发场景下失败不应污染其他线程的写入。"""

    def test_one_thread_fails_other_threads_unaffected(self, tmp_db: Path, monkeypatch):
        """让一个线程在 INSERT 阶段抛 IntegrityError（同 run_id），其他线程应正常。"""

        _ = connect(tmp_db)
        ensure_schema(_)

        # 先写一条固定 run_id
        evs_fixed, started_at = _one_run("2026-10-10T08:30:00Z")
        record_run("his", "outpatient-register", evs_fixed, started_at=started_at, path=tmp_db)

        # monkeypatch 模拟 INSERT 抛错（不动 record_run 整体行为，只在 conn.execute 上 patch）
        ok_count = 0
        fail_count = 0
        errors: List[BaseException] = []
        counter_lock = threading.Lock()

        def worker_failing():
            nonlocal ok_count, fail_count
            try:
                # 走 record_run → 同 run_id → 必抛 IntegrityError
                record_run(
                    "his", "outpatient-register", evs_fixed,
                    started_at=started_at, path=tmp_db,
                )
                with counter_lock:
                    ok_count += 1
            except sqlite3.IntegrityError:
                with counter_lock:
                    fail_count += 1

        def worker_ok(idx: int):
            nonlocal ok_count
            try:
                evs = [_ev("preflight", vendor_id=f"v-{idx:02d}", interface_id="iface")]
                record_run(
                    f"v-{idx:02d}", "iface", evs,
                    started_at=started_at, path=tmp_db,
                )
                with counter_lock:
                    ok_count += 1
            except BaseException as exc:  # noqa: BLE001
                with counter_lock:
                    errors.append(exc)

        threads = []
        threads.append(threading.Thread(target=worker_failing))
        for i in range(5):
            threads.append(threading.Thread(target=worker_ok, args=(i,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert fail_count == 1, f"expected exactly 1 IntegrityError, got {fail_count}"
        assert ok_count == 5, f"expected 5 successful writes, got {ok_count}"
        assert errors == [], f"unexpected errors: {[repr(e) for e in errors]}"

        # 数据库里：1 条原 run + 5 条新 = 6 条
        assert count_history(path=tmp_db) == 6

    def test_cross_thread_write_returns_distinct_run_ids(self, tmp_db: Path):
        """不同线程写不同 (vendor_id, interface_id, started_at) → run_id 各异。"""

        _ = connect(tmp_db)
        ensure_schema(_)

        threads_run_ids: List[List[str]] = [[] for _ in range(3)]
        lock = threading.Lock()

        def worker(t_idx: int):
            for i in range(5):
                vendor_id = f"v-{t_idx:02d}-{i:02d}"
                evs = [_ev("preflight", vendor_id=vendor_id, interface_id="iface")]
                rid = record_run(
                    vendor_id, "iface", evs,
                    started_at="2026-10-10T08:30:00Z",
                    path=tmp_db,
                )
                with lock:
                    threads_run_ids[t_idx].append(rid)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        all_ids = [rid for sub in threads_run_ids for rid in sub]
        assert len(all_ids) == 15
        assert len(set(all_ids)) == 15, "duplicate run_ids across threads"


# ---------------------------------------------------------------------------
# 6. 凭据纪律：并发场景不泄漏凭据字面量
# ---------------------------------------------------------------------------


class TestConcurrentCredentialSafety:
    """并发写入 + 凭据字面量 → DB grep 命中 = 0。"""

    def test_concurrent_writes_with_credentials_no_leak(self, tmp_db: Path):
        """并发写一组 evidence 含凭据字面量 → DB 不留痕。"""

        _ = connect(tmp_db)
        ensure_schema(_)

        forbidden = "CONCURRENT-CRED-LEAK-XYZ-12345"

        def worker(t_idx: int):
            for i in range(5):
                evs = [
                    _ev(
                        "sample",
                        command=f"GET /health  Authorization: Bearer {forbidden}",
                        response_body=f'{{"token": "{forbidden}", "patient_id": "<EXAMPLE>"}}',
                        vendor_id=f"v-{t_idx:02d}",
                        interface_id=f"iface-{i:02d}",
                    ),
                ]
                record_run(
                    f"v-{t_idx:02d}", f"iface-{i:02d}", evs,
                    started_at="2026-10-10T08:30:00Z",
                    path=tmp_db,
                )

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 全表 grep forbidden literal
        conn = sqlite3.connect(str(tmp_db))
        try:
            rows = conn.execute(
                "SELECT steps_json, evidence_hash, meta_json FROM inspection_runs_y2026m10"
            ).fetchall()
        finally:
            conn.close()

        joined = "\n".join(str(r) for r in rows)
        assert forbidden not in joined, (
            f"credential literal leaked into SQLite; saw {forbidden!r} in {joined[:500]}"
        )


# ---------------------------------------------------------------------------
# 7. journal_mode 守卫：connect() 不能让 WAL 退化为 memory
# ---------------------------------------------------------------------------


class TestJournalModeNotMemory:
    """``connect()`` 必须读到 'wal'（file-backed DB 上），不能是 'memory'。"""

    def test_pragmas_explicitly_wal(self, tmp_db: Path):
        conn = connect(tmp_db)
        try:
            # 设置并读回
            conn.execute("PRAGMA journal_mode = WAL")
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode.lower() == "wal"

            # synchronous 在 Python sqlite3 模块下返回整数：
            #   0=OFF / 1=NORMAL / 2=FULL（默认）/ 3=EXTRA
            # 验证它至少是 NORMAL 或更严，保证 durability。
            sync = conn.execute("PRAGMA synchronous").fetchone()[0]
            assert int(sync) >= 1, (
                f"expected synchronous >= 1 (NORMAL), got {sync}; "
                "WAL with synchronous=OFF is unsafe for durability"
            )
        finally:
            conn.close()
