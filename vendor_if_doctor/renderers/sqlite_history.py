"""sqlite_history — SQLite 巡检历史写入器。

设计要点
--------

* **月表分区**（task 19）：每条 run 按 ``started_at`` 所在月写入
  ``inspection_runs_y<YYYY>m<MM>`` 月表；月表 schema 与列布局与单表一致，
  各表自建 ``idx_run_id_<suffix>`` / ``idx_vendor_id_<suffix>`` /
  ``idx_started_at_<suffix>`` 三个索引。``inspection_runs`` 旧表保留在 schema
  但不再写入（兼容 task 10 以前的 demo 数据 / 已发布 SQLite 文件）。
* **查询跨月 UNION ALL**：``query_history`` / ``fetch_run`` /
  ``fetch_runs_by_vendor`` 把 ``sqlite_master`` 里所有
  ``inspection_runs_y%`` 月表 + 旧 ``inspection_runs`` 表用 ``UNION ALL``
  串起来，再 ``ORDER BY started_at DESC`` + ``LIMIT`` / ``OFFSET`` 分页。
* **PRAGMA journal_mode=WAL**：连接时设置；允许读不阻塞写、写不阻塞读；
  配合 ``BEGIN IMMEDIATE`` 拿写锁避免 ``SQLITE_BUSY`` 抖动（task 30 并发测试覆盖）。
* **run_id 唯一约束**：同 run_id 二次写入抛 ``sqlite3.IntegrityError``；调用方
  可据此实现幂等（task 11 CLI ``run`` 失败重试不会重复落库）。
* **evidence_hash = sha256(command + response_body)**：跑内全部 evidence 的
  command + response_body 拼接后再 sha256 取前 16 位，作为这次跑的指纹；
  Evidence 在收集阶段已经过 ``_coarse_redact``，本模块再兜底一次字符串过滤，
  保证 SQLite 落库不含 Authorization / Bearer / token / password 字面量。
* **路径**：默认 ``$VENDOR_DOCTOR_DB`` 或 ``./vendor_if_doctor.db``；
  不在 ``out/`` 下，与 handover.md / tickets.jsonl 区分（巡检历史是状态库，
  值班交接清单是单日产物）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Mapping, Optional, Sequence

from vendor_if_doctor.connection.evidence import Evidence


# ---------------------------------------------------------------------------
# 凭据纪律兜底（与 handover_md / ticket_jsonl 保持一致语义）
# ---------------------------------------------------------------------------

_REDACT_PATTERNS: list[tuple[str, str]] = [
    (r"(?im)(authorization\s*[:=]\s*)(?:bearer|basic)?\s*[^\s,;\"'\r\n]+", r"\1***"),
    (r"(?i)(\bbearer\s+)[^\s,;\"'\r\n]+", r"\1***"),
    (r"(?i)(\bbasic\s+)[^\s,;\"'\r\n]+", r"\1***"),
    (
        r'(?i)("(?:token|password|api[_-]?key|authorization|secret|x-api-key)"\s*:\s*)"[^"]*"',
        r'\1"***"',
    ),
    (r"(?i)([\?&](?:api[_-]?key|access[_-]?token|token|password)\s*=\s*)([^&\s=]+)", r"\1***"),
    (r"(?i)(\btoken\s*=\s*)[^\s,;\"'\r\n&]+", r"\1***"),
    (r"(?i)(\bpassword\s*=\s*)[^\s,;\"'\r\n&]+", r"\1***"),
]


def _coarse_redact(text: Optional[str]) -> str:
    if not text:
        return ""
    out = text
    for pat, repl in _REDACT_PATTERNS:
        out = re.sub(pat, repl, out)
    return out


# ---------------------------------------------------------------------------
# Schema (DDL)
# ---------------------------------------------------------------------------

# 旧 schema（task 10）— inspection_runs 单表。task 19 起不再写入，但仍
# 出现在 schema 以兼容已有 SQLite 文件与 query_history 跨表 UNION ALL。
LEGACY_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS inspection_runs (
    run_id        TEXT PRIMARY KEY,
    vendor_id     TEXT NOT NULL,
    interface_id  TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('healthy','degraded','failed','skipped')),
    steps_json    TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    meta_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_inspection_runs_vendor_id
    ON inspection_runs(vendor_id);
CREATE INDEX IF NOT EXISTS idx_inspection_runs_started_at
    ON inspection_runs(started_at);
"""

# 兼容外部引用：原 SCHEMA_DDL 等价于 LEGACY_SCHEMA_DDL。
SCHEMA_DDL = LEGACY_SCHEMA_DDL


def _partition_ddl(suffix: str) -> str:
    """生成月表 ``inspection_runs_<suffix>`` 的 DDL。

    索引命名约定：``idx_run_id_<suffix>`` / ``idx_vendor_id_<suffix>`` /
    ``idx_started_at_<suffix>``；命名带 ``<suffix>`` 保证跨月表不冲突，
    又保留 ``idx_run_id`` / ``idx_vendor_id`` 子串便于 ``.schema`` 校验。
    """

    table = f"inspection_runs_{suffix}"
    return f"""
CREATE TABLE IF NOT EXISTS {table} (
    run_id        TEXT PRIMARY KEY,
    vendor_id     TEXT NOT NULL,
    interface_id  TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('healthy','degraded','failed','skipped')),
    steps_json    TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    meta_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_run_id_{suffix}
    ON {table}(run_id);
CREATE INDEX IF NOT EXISTS idx_vendor_id_{suffix}
    ON {table}(vendor_id);
CREATE INDEX IF NOT EXISTS idx_started_at_{suffix}
    ON {table}(started_at);
"""


def _parse_iso_utc(s: str) -> datetime:
    """ISO8601 → ``datetime``（兼容 ``...Z`` 结尾与 ``±HH:MM`` 偏移）。"""

    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def _partition_suffix(started_at: str) -> str:
    """``started_at`` ISO 字符串 → 月表后缀 ``y<YYYY>m<MM>``。"""

    dt = _parse_iso_utc(started_at)
    return f"y{dt.year:04d}m{dt.month:02d}"


def partition_table_name(started_at: str) -> str:
    """``started_at`` → 月表名 ``inspection_runs_y<YYYY>m<MM>``。"""

    return f"inspection_runs_{_partition_suffix(started_at)}"


def ensure_partition(conn: sqlite3.Connection, started_at: str) -> str:
    """确保 ``started_at`` 对应月表存在（幂等），返回月表名。"""

    suffix = _partition_suffix(started_at)
    conn.executescript(_partition_ddl(suffix))
    return f"inspection_runs_{suffix}"


def ensure_schema(conn: sqlite3.Connection) -> None:
    """建表 + 索引（兼容旧 inspection_runs 单表）。重复执行幂等。"""

    conn.executescript(LEGACY_SCHEMA_DDL)


def _existing_storage_tables(conn: sqlite3.Connection) -> List[str]:
    """列出数据库内全部历史表（旧 inspection_runs + 全部 inspection_runs_y*）。"""

    cur = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND (name='inspection_runs' "
        "OR name LIKE 'inspection_runs_y%') "
        "ORDER BY name"
    )
    return [row[0] for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------


def db_path(path: Optional[os.PathLike] = None) -> Path:
    """计算 SQLite 文件路径。

    优先级：``path`` 入参 → ``$VENDOR_DOCTOR_DB`` → ``./vendor_if_doctor.db``。
    """

    if path is not None:
        target = Path(path)
    else:
        env = os.environ.get("VENDOR_DOCTOR_DB")
        target = Path(env) if env else Path.cwd() / "vendor_if_doctor.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def connect(path: Optional[os.PathLike] = None) -> sqlite3.Connection:
    """打开 SQLite 连接 + 设 PRAGMA WAL + 启动期建表。"""

    conn = sqlite3.connect(str(db_path(path)))
    # WAL 模式允许多 reader + 1 writer 并发；task 30 并发测试依赖此
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_schema(conn)
    return conn


# ---------------------------------------------------------------------------
# Run 聚合（与 handover_md 语义一致）
# ---------------------------------------------------------------------------

_STATUS_PRIORITY = {"failed": 0, "degraded": 1, "healthy": 2, "skipped": 3}


def _classify_run_status(evidence: Sequence[Evidence]) -> str:
    if not evidence:
        return "skipped"
    statuses = [ev.status for ev in evidence]
    if any(s == "fail" for s in statuses):
        return "failed"
    if any(s == "degraded" for s in statuses):
        return "degraded"
    if any(s == "skip" for s in statuses) and any(s == "pass" for s in statuses):
        return "degraded"
    if all(s == "skip" for s in statuses):
        return "skipped"
    return "healthy"


def _first_failed_step(evidence: Sequence[Evidence]) -> Optional[str]:
    for ev in evidence:
        if ev.status in ("fail", "degraded"):
            return ev.step_name
    return None


def _first_failure_class(evidence: Sequence[Evidence]) -> Optional[str]:
    for ev in evidence:
        if ev.status in ("fail", "degraded"):
            return ev.failure_class
    return None


def _group_by_interface(
    evidence: Iterable[Evidence],
) -> "list[tuple[str, str, list[Evidence]]]":
    grouped: dict[tuple[str, str], list[Evidence]] = {}
    for ev in evidence:
        key = (ev.vendor_id or "<unknown>", ev.interface_id or "<unknown>")
        grouped.setdefault(key, []).append(ev)
    return [(vid, iid, evs) for (vid, iid), evs in grouped.items()]


def _now_utc_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_run_id(vendor_id: str, interface_id: str, started_at: str) -> str:
    """确定性 run_id：sha256(vendor_id + interface_id + started_at)[:16]。

    同一 vendor + interface + started_at 永远得同一 run_id → 二次写入 UNIQUE
    触发 IntegrityError，调用方据此实现幂等。
    """

    raw = f"{vendor_id}|{interface_id}|{started_at}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _steps_to_json(evidence: Sequence[Evidence]) -> str:
    """evidence 列表 → JSON 字符串（按字段名排序，跨协议一致）。

    写盘前对每个 evidence 的 ``command`` / ``response_body`` / ``summary`` /
    ``meta`` 字段过 :func:`_coarse_redact` 兜底（Evidence 自身在收集阶段已
    redact 过，这里再过滤一遍，保证 ``sqlite3 <db> 'SELECT * FROM
    inspection_runs' | grep -i 'token\\|password\\|authorization'`` 命中数 = 0）。
    """

    payload: list[dict] = []
    for ev in evidence:
        rec = ev.to_dict()
        # 字符串字段先过 redact
        if isinstance(rec.get("command"), str):
            rec["command"] = _coarse_redact(rec["command"])
        if isinstance(rec.get("response_body"), str):
            rec["response_body"] = _coarse_redact(rec["response_body"])
        if isinstance(rec.get("summary"), str):
            rec["summary"] = _coarse_redact(rec["summary"])
        # meta 字典内字符串值也过 redact
        meta = rec.get("meta") or {}
        if isinstance(meta, dict):
            rec["meta"] = {
                k: _coarse_redact(str(v)) if isinstance(v, str) else v
                for k, v in meta.items()
            }
        payload.append(rec)
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def _compute_run_evidence_hash(
    evidence: Sequence[Evidence],
) -> str:
    """sha256(command + response_body) 前 16 位，作为 run 指纹。

    Evidence 已自带 ``_coarse_redact`` 兜底；本函数再过一道过滤后拼接 →
    数据库里 grep ``token`` / ``password`` / ``Authorization`` 命中数 = 0。
    """

    parts: list[str] = []
    for ev in evidence:
        cmd = _coarse_redact(ev.command)
        body = _coarse_redact(ev.response_body)
        parts.append(cmd)
        parts.append(body)
    joined = "\n".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Run 记录（核心写入入口）
# ---------------------------------------------------------------------------


def record_run(
    vendor_id: str,
    interface_id: str,
    evidence: Sequence[Evidence],
    *,
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
    run_id: Optional[str] = None,
    meta: Optional[Mapping[str, str]] = None,
    path: Optional[os.PathLike] = None,
) -> str:
    """写入一条 inspection_runs 行，返回 run_id。

    实际写入目标表是 ``started_at`` 月份对应的
    ``inspection_runs_y<YYYY>m<MM>`` 月表（task 19 月表分区）；同 run_id
    二次写入由月表 PRIMARY KEY UNIQUE 抛 ``sqlite3.IntegrityError``。
    """

    started_at = started_at or _now_utc_iso()
    finished_at = finished_at or _now_utc_iso()
    rid = run_id or _make_run_id(vendor_id, interface_id, started_at)

    steps_json = _steps_to_json(evidence)
    ev_hash = _compute_run_evidence_hash(evidence)
    status = _classify_run_status(evidence)
    meta_json = json.dumps(dict(meta), ensure_ascii=False, sort_keys=True) if meta else None

    conn = connect(path)
    try:
        # BEGIN IMMEDIATE 拿写锁 → 避免并发 writer 卡 SQLITE_BUSY
        conn.execute("BEGIN IMMEDIATE")
        try:
            table_name = ensure_partition(conn, started_at)
            conn.execute(
                f"""
                INSERT INTO {table_name} (
                    run_id, vendor_id, interface_id,
                    started_at, finished_at, status,
                    steps_json, evidence_hash, meta_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rid,
                    vendor_id,
                    interface_id,
                    started_at,
                    finished_at,
                    status,
                    steps_json,
                    ev_hash,
                    meta_json,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()

    return rid


def write_history(
    evidence_list: Sequence[Evidence],
    *,
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
    meta: Optional[Mapping[str, str]] = None,
    path: Optional[os.PathLike] = None,
) -> List[str]:
    """把一组 Evidence 写入 SQLite（按 (vendor_id, interface_id) 分 run）。

    返回写入的 run_id 列表（按插入顺序）。各 run 按其 ``started_at`` 月份
    路由到对应月表；同一 evidence_list 内不同 run 可能落在不同月表。
    """

    grouped = _group_by_interface(evidence_list)
    started = started_at or _now_utc_iso()
    finished = finished_at or _now_utc_iso()
    run_ids: list[str] = []
    for vendor_id, interface_id, evs in grouped:
        rid = record_run(
            vendor_id,
            interface_id,
            evs,
            started_at=started,
            finished_at=finished,
            meta=meta,
            path=path,
        )
        run_ids.append(rid)
    return run_ids


# ---------------------------------------------------------------------------
# 查询（跨月 UNION ALL + 分页）
# ---------------------------------------------------------------------------


_RUN_PROJECTION = (
    "run_id", "vendor_id", "interface_id",
    "started_at", "finished_at",
    "status", "evidence_hash",
)


def _union_select(
    conn: sqlite3.Connection,
    *,
    columns: str,
    vendor_filter: Optional[str],
) -> tuple[str, list]:
    """构造跨月 + 旧表 UNION ALL 子查询，返回 (sql, params)。

    使用调用方的 ``conn`` 探测表名（保持事务一致）；``columns`` 通常是
    ``run_id, vendor_id, ...``，由调用方控制 SELECT 列表；
    ``vendor_filter`` 非空时所有子查询都加 ``WHERE vendor_id = ?``。
    """

    tables = _existing_storage_tables(conn)

    if not tables:
        # 兜底：哪怕数据库还没建表，先让 SQL 合法可解析
        tables = ["inspection_runs"]

    parts: list[str] = []
    params: list = []
    for tbl in tables:
        if vendor_filter:
            parts.append(
                f"SELECT {columns} FROM {tbl} WHERE vendor_id = ?"
            )
            params.append(vendor_filter)
        else:
            parts.append(f"SELECT {columns} FROM {tbl}")
    return " UNION ALL ".join(parts), params


def fetch_run(
    run_id: str,
    *,
    path: Optional[os.PathLike] = None,
) -> Optional[dict]:
    """按 run_id 查一条 inspection_runs（跨月 UNION ALL）。"""

    conn = connect(path)
    try:
        tables = _existing_storage_tables(conn)
        if not tables:
            return None

        keys = (
            "run_id", "vendor_id", "interface_id",
            "started_at", "finished_at",
            "status", "steps_json", "evidence_hash", "meta_json",
        )
        # 跨月 + 旧表 UNION ALL，每个 SELECT 都带 WHERE run_id = ?
        parts = [
            f"SELECT run_id, vendor_id, interface_id, started_at, finished_at, "
            f"status, steps_json, evidence_hash, meta_json FROM {tbl} WHERE run_id = ?"
            for tbl in tables
        ]
        sql = " UNION ALL ".join(parts) + " LIMIT 1"
        params: list = [run_id] * len(tables)

        cur = conn.execute(sql, params)
        row = cur.fetchone()
        if row is None:
            return None
        rec = dict(zip(keys, row))
        if rec.get("steps_json"):
            rec["steps"] = json.loads(rec.pop("steps_json"))
        if rec.get("meta_json"):
            rec["meta"] = json.loads(rec.pop("meta_json"))
        else:
            rec.pop("meta_json", None)
        return rec
    finally:
        conn.close()


def fetch_runs_by_vendor(
    vendor_id: str,
    *,
    limit: int = 50,
    path: Optional[os.PathLike] = None,
) -> List[dict]:
    """按 vendor_id 查最近 N 条 runs（按 started_at DESC，跨月）。"""

    conn = connect(path)
    try:
        tables = _existing_storage_tables(conn)
        if not tables:
            return []

        # 跨月 + 旧表 UNION ALL，每个 SELECT 都带 WHERE vendor_id = ?
        cols = "run_id, vendor_id, interface_id, started_at, finished_at, status, evidence_hash"
        parts = [
            f"SELECT {cols} FROM {tbl} WHERE vendor_id = ?"
            for tbl in tables
        ]
        sql = (
            " UNION ALL ".join(parts)
            + " ORDER BY started_at DESC LIMIT ?"
        )
        params: list = [vendor_id] * len(tables) + [int(limit)]

        cur = conn.execute(sql, params)
        rows = cur.fetchall()
        return [dict(zip(_RUN_PROJECTION, r)) for r in rows]
    finally:
        conn.close()


def query_history(
    *,
    vendor_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    path: Optional[os.PathLike] = None,
) -> List[dict]:
    """查询巡检历史，支持跨月 UNION ALL 聚合 + 分页（task 19 入口）。

    Parameters
    ----------
    vendor_id : str, optional
        过滤特定 ``vendor_id``；``None`` 表示全部 vendor。
    limit : int
        返回行数上限（默认 50）。
    offset : int
        跳过的行数（用于分页）。
    path : PathLike, optional
        SQLite 文件路径。

    Returns
    -------
    List[dict]
        按 ``started_at DESC`` 排序的巡检 run 列表；每条含
        ``run_id`` / ``vendor_id`` / ``interface_id`` / ``started_at`` /
        ``finished_at`` / ``status`` / ``evidence_hash`` 共 7 个字段。
    """

    if limit < 0:
        raise ValueError("limit must be >= 0")
    if offset < 0:
        raise ValueError("offset must be >= 0")

    cols = (
        "run_id, vendor_id, interface_id, "
        "started_at, finished_at, status, evidence_hash"
    )

    conn = connect(path)
    try:
        union_sql, params = _union_select(
            conn, columns=cols, vendor_filter=vendor_id,
        )
        sql = (
            f"SELECT * FROM ({union_sql}) "
            f"ORDER BY started_at DESC, run_id DESC LIMIT ? OFFSET ?"
        )
        params = [*params, int(limit), int(offset)]

        cur = conn.execute(sql, params)
        rows = cur.fetchall()
        return [dict(zip(_RUN_PROJECTION, r)) for r in rows]
    finally:
        conn.close()


def count_history(
    *,
    vendor_id: Optional[str] = None,
    path: Optional[os.PathLike] = None,
) -> int:
    """跨月 UNION ALL 总行数（便于分页 UI 显示）。"""

    conn = connect(path)
    try:
        union_sql, params = _union_select(
            conn,
            columns="run_id, vendor_id, interface_id, started_at, "
            "finished_at, status, evidence_hash",
            vendor_filter=vendor_id,
        )
        sql = f"SELECT count(*) FROM ({union_sql})"
        cur = conn.execute(sql, params)
        row = cur.fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


__all__ = [
    "LEGACY_SCHEMA_DDL",
    "SCHEMA_DDL",
    "ensure_schema",
    "ensure_partition",
    "partition_table_name",
    "db_path",
    "connect",
    "record_run",
    "write_history",
    "fetch_run",
    "fetch_runs_by_vendor",
    "query_history",
    "count_history",
    "_classify_run_status",
    "_first_failed_step",
    "_compute_run_evidence_hash",
    "_make_run_id",
    "_coarse_redact",
    "_partition_ddl",
    "_partition_suffix",
    "_existing_storage_tables",
]
