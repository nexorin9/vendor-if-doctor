"""weekly_html — HTML 周报渲染器。

设计要点
--------

* **输入**：巡检历史 SQLite（``inspection_runs`` + 月表
  ``inspection_runs_y<YYYY>m<MM>``）。仅统计「最近 7 天」内的 run；
  早于 7 天的 run 不参与聚合，但仍落库可查。
* **三栏**：

  * 连通率（``system-rate``）—— 按业务系统（``vendor_id`` 前缀如 ``his`` /
    ``lis`` / ``pacs``）聚合，给出 ``healthy / degraded / failed / skipped``
    计数与百分比，按连通率从低到高排序（红灯优先）。
  * 趋势 ASCII 火花图（``trend-spark``）—— 每系统一行 7 列 Unicode
    sparkline（``▁▂▃▄▅▆▇█``）；展示近 7 天 healthy 比例变化。
  * 异常清单（``anomaly-list``）—— 仅列 ``failed`` / ``degraded`` 的 run，
    含 ``vendor_id`` / ``interface_id`` / ``first_failed_step`` /
    ``failure_class`` / ``severity`` / ``evidence_hash`` 前 8 位。

* **离线可看**——HTML 内嵌极简 CSS（无 ``<script>`` / 无 ``<link>`` / 无
  ``<style @import>`` / 无外部图片）。``grep '<script\\|<link rel'`` 在产物里
  命中数 = 0。
* **凭据纪律** —— 渲染前所有 evidence 字段再过一道字符串级
  ``_coarse_redact`` 兜底；HTML 里 grep ``Authorization`` / ``Bearer`` /
  ``token`` / ``password`` 命中数 = 0（凭据值不会进 HTML 周报）。
* **路径**：默认 ``$VENDOR_DOCTOR_WEEKLY_DIR`` 或
  ``./out/weekly/weekly.html``；可由 :envvar:`VENDOR_DOCTOR_WEEKLY_DIR`
  覆盖。

不在范围内
----------

* 不画 3D / WebGL / 动画效果，只画静态 HTML；
* 不连任何远端字体 / CDN / Google Analytics；
* 不直接发送邮件 / 推送到 IM，由信息科自行打包发送。
"""

from __future__ import annotations

import html
import os
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# 凭据字段字符串级过滤（与 handover_md / ticket_jsonl / sqlite_history 语义一致）
# ---------------------------------------------------------------------------

_REDACT_PATTERNS: list[tuple[str, str]] = [
    (r"(?im)(authorization\s*[:=]\s*)(?:bearer|basic)?\s*[^\s,;\"'\r\n]+", r"\1***"),
    (r"(?i)(\bbearer\s+)[^\s,;\"'\r\n]+", r"\1***"),
    (r"(?i)(\bbasic\s+)[^\s,;\"'\r\n]+", r"\1***"),
    (
        r'(?i)("(?:token|password|api[_-]?key|authorization|secret|x-api-key|access_token)"\s*:\s*)"[^"]*"',
        r'\1"***"',
    ),
    (r"(?i)([\?&](?:api[_-]?key|access[_-]?token|token|password)\s*=\s*)([^&\s=]+)", r"\1***"),
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
# 数据聚合
# ---------------------------------------------------------------------------

_SYSTEM_KEYS = ("his", "lis", "pacs", "emr", "medical-insurance", "regional", "spd", "rtms")
"""常见的业务系统（vendor_id 前缀），未知 vendor 归入 ``other``。"""

_SPARK_BARS = "▁▂▃▄▅▆▇█"


def _system_key(vendor_id: str) -> str:
    """``vendor_id`` → 业务系统分类。

    ``his`` / ``lis-foo-bar`` → ``his``；``medical-insurance`` →
    ``medical-insurance``；其它 → ``other``。前缀匹配比完全匹配更宽容，
    便于信息科写 ``his_outpatient`` / ``lis-blah`` 这类命名。
    """

    if not vendor_id:
        return "other"
    v = vendor_id.strip().lower()
    for sys_key in _SYSTEM_KEYS:
        if v == sys_key or v.startswith(sys_key + "_") or v.startswith(sys_key + "-"):
            return sys_key
    return "other"


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _parse_iso(s: str) -> datetime:
    """ISO8601 → ``datetime``；兼容 ``...Z`` 结尾与 ``±HH:MM`` 偏移。"""

    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


# ---------------------------------------------------------------------------
# 数据结构：周报聚合产物
# ---------------------------------------------------------------------------


@dataclass
class SystemAgg:
    """某个业务系统在一周内的连通率聚合。"""

    system: str
    total: int = 0
    healthy: int = 0
    degraded: int = 0
    failed: int = 0
    skipped: int = 0
    trend: List[float] = field(default_factory=list)  # 7 个 0..1 连通率
    anomalies: List[dict] = field(default_factory=list)

    @property
    def connectivity(self) -> float:
        if self.total == 0:
            return 1.0
        # 仅 healthy / degraded 计「连通」；failed + skipped 不算
        connected = self.healthy + self.degraded
        return connected / self.total

    @property
    def trend_spark(self) -> str:
        if not self.trend:
            return "—"
        out = []
        for r in self.trend:
            idx = max(0, min(len(_SPARK_BARS) - 1, int(round(r * (len(_SPARK_BARS) - 1)))))
            out.append(_SPARK_BARS[idx])
        return "".join(out)


@dataclass
class WeeklyReport:
    """周报聚合结果（render_weekly_html 接受此对象或 raw rows）。"""

    systems: List[SystemAgg] = field(default_factory=list)
    total_runs: int = 0
    week_start: str = ""
    week_end: str = ""
    generated_at: str = ""
    window_days: int = 7


# ---------------------------------------------------------------------------
# DB 读取
# ---------------------------------------------------------------------------

_RUN_PROJECTION = (
    "run_id", "vendor_id", "interface_id",
    "started_at", "finished_at",
    "status", "evidence_hash", "steps_json",
)


def _existing_storage_tables(conn: sqlite3.Connection) -> List[str]:
    """与 sqlite_history 保持一致：含旧表 + 全部月表。"""

    cur = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND (name='inspection_runs' "
        "OR name LIKE 'inspection_runs_y%') "
        "ORDER BY name"
    )
    return [row[0] for row in cur.fetchall()]


def _week_window(now: datetime, window_days: int = 7) -> Tuple[datetime, datetime]:
    """计算窗口起点与终点（含当天）。

    起点 = now - (window_days - 1) 天；终点 = now（结束于当天 23:59:59）。
    """

    start = (now - timedelta(days=window_days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end = now
    return start, end


def fetch_recent_runs(
    *,
    window_days: int = 7,
    path: Optional[os.PathLike] = None,
    conn: Optional[sqlite3.Connection] = None,
    now: Optional[datetime] = None,
) -> List[dict]:
    """读最近 ``window_days`` 天的 inspection_runs 行（跨月 UNION ALL）。

    返回 list of dict，字段：``run_id`` / ``vendor_id`` / ``interface_id`` /
    ``started_at`` / ``finished_at`` / ``status`` / ``evidence_hash`` /
    ``steps_json``。``steps_json`` 是字符串（按调用方需求解析）；为空时不参与
    ``first_failed_step`` 计算。
    """

    if window_days < 1:
        raise ValueError("window_days must be >= 1")

    now = now or _now_utc()
    start, end = _week_window(now, window_days)
    start_iso = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso = end.strftime("%Y-%m-%dT%H:%M:%SZ")

    close_conn = False
    if conn is None:
        from vendor_if_doctor.renderers.sqlite_history import connect
        conn = connect(path)
        close_conn = True

    try:
        tables = _existing_storage_tables(conn)
        if not tables:
            return []

        cols = (
            "run_id, vendor_id, interface_id, "
            "started_at, finished_at, status, evidence_hash, steps_json"
        )
        parts = [
            f"SELECT {cols} FROM {tbl} "
            f"WHERE started_at >= ? AND started_at <= ?"
            for tbl in tables
        ]
        sql = (
            " UNION ALL ".join(parts)
            + " ORDER BY started_at DESC, run_id DESC"
        )
        params: list = []
        for _ in tables:
            params.extend([start_iso, end_iso])

        cur = conn.execute(sql, params)
        rows = cur.fetchall()
        return [dict(zip(_RUN_PROJECTION, r)) for r in rows]
    finally:
        if close_conn:
            conn.close()


def _first_failed_step_from_steps_json(steps_json: Optional[str]) -> Optional[str]:
    """从 ``steps_json`` 反序列化首个 ``status in (fail, degraded)`` step 名。"""

    if not steps_json:
        return None
    try:
        import json

        steps = json.loads(steps_json)
    except Exception:
        return None
    if not isinstance(steps, list):
        return None
    for st in steps:
        if not isinstance(st, dict):
            continue
        status = st.get("status")
        if status in ("fail", "degraded"):
            return st.get("step_name")
    return None


def _first_failure_class_from_steps_json(steps_json: Optional[str]) -> Optional[str]:
    if not steps_json:
        return None
    try:
        import json

        steps = json.loads(steps_json)
    except Exception:
        return None
    if not isinstance(steps, list):
        return None
    for st in steps:
        if not isinstance(st, dict):
            continue
        status = st.get("status")
        if status in ("fail", "degraded"):
            return st.get("failure_class")
    return None


def _severity_of_failure_class(failure_class: Optional[str]) -> Optional[str]:
    """映射 failure_class → severity（与 connection.severity 兼容）。

    critical: auth_failed / schema_mismatch / hl7_ack_re / timeout /
               tcp_unreachable / tcp_half_closed / http_status
    warning:  payload_empty / degraded_slow / encoding_error / hl7_ack_missing
    info:     not_implemented / None / 未知
    """

    if not failure_class:
        return "info"
    critical = {
        "auth_failed", "schema_mismatch", "hl7_ack_re", "timeout",
        "tcp_unreachable", "tcp_half_closed", "http_status",
    }
    warning = {"payload_empty", "degraded_slow", "encoding_error", "hl7_ack_missing"}
    if failure_class in critical:
        return "critical"
    if failure_class in warning:
        return "warning"
    if failure_class == "not_implemented":
        return "info"
    return "info"


def aggregate_recent_runs(
    runs: Sequence[dict],
    *,
    window_days: int = 7,
    now: Optional[datetime] = None,
) -> WeeklyReport:
    """聚合 ``fetch_recent_runs`` 结果 → WeeklyReport。

    * 按 ``vendor_id`` 前缀归入业务系统（``_system_key``）；
    * 每系统产出 trend 列表（按日期升序，每天取连通率 0..1）；
    * anomalies 仅保留 ``status in (failed, degraded)`` 的 run；
    * 已用 ``_coarse_redact`` 对 ``evidence_hash`` 等敏感字符串字段兜底。
    """

    now = now or _now_utc()
    start, end = _week_window(now, window_days)

    # 各系统的每天健康计数（init 7 天 0..1）
    sys_per_day_healthy: Dict[str, Dict[str, list[int, int]]] = defaultdict(
        lambda: defaultdict(lambda: [0, 0])  # [healthy_count, total_count]
    )
    sys_totals: Dict[str, SystemAgg] = {}

    for row in runs:
        vendor_id = row.get("vendor_id") or ""
        sys_key = _system_key(vendor_id)
        if sys_key not in sys_totals:
            sys_totals[sys_key] = SystemAgg(system=sys_key)
        agg = sys_totals[sys_key]

        status = (row.get("status") or "skipped").lower()
        agg.total += 1
        if status == "healthy":
            agg.healthy += 1
        elif status == "degraded":
            agg.degraded += 1
        elif status == "failed":
            agg.failed += 1
        else:
            agg.skipped += 1

        # 计算 trend（按自然日聚合连通率）
        started_at = row.get("started_at") or ""
        if started_at:
            try:
                dt = _parse_iso(started_at)
                day = dt.strftime("%Y-%m-%d")
            except Exception:
                day = "_unknown"
        else:
            day = "_unknown"
        bucket = sys_per_day_healthy[sys_key][day]
        bucket[1] += 1
        if status in ("healthy", "degraded"):
            bucket[0] += 1

        # 异常清单
        if status in ("failed", "degraded"):
            steps_json = row.get("steps_json")
            first_step = _first_failed_step_from_steps_json(steps_json)
            first_fc = _first_failure_class_from_steps_json(steps_json)
            sev = _severity_of_failure_class(first_fc)
            ev_hash = _coarse_redact(row.get("evidence_hash")) or ""
            ev_hash_short = ev_hash[:8] if ev_hash else "—"
            anomaly = {
                "vendor_id": vendor_id,
                "interface_id": row.get("interface_id") or "",
                "first_failed_step": first_step or "—",
                "failure_class": first_fc or "—",
                "severity": sev or "info",
                "evidence_hash_short": ev_hash_short,
                "evidence_hash_full": ev_hash,
                "started_at": started_at,
                "status": status,
            }
            agg.anomalies.append(anomaly)

    # 构造 trend 列表（按日期升序，缺失天补 0/0）
    for sys_key, agg in sys_totals.items():
        per_day = sys_per_day_healthy.get(sys_key, {})
        trend: List[float] = []
        cur = start
        while cur <= end:
            day = cur.strftime("%Y-%m-%d")
            cell = per_day.get(day)
            if cell is None:
                trend.append(1.0)  # 没 run 视为连通（不计入分母）
            else:
                h, t = cell[0], cell[1]
                trend.append(h / t if t > 0 else 1.0)
            cur = cur + timedelta(days=1)
        agg.trend = trend

    # 按连通率升序排（红灯优先）
    systems = sorted(
        sys_totals.values(),
        key=lambda a: (a.connectivity, -a.failed, a.system),
    )

    return WeeklyReport(
        systems=systems,
        total_runs=sum(a.total for a in sys_totals.values()),
        week_start=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        week_end=end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        generated_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        window_days=window_days,
    )


# ---------------------------------------------------------------------------
# 模板渲染
# ---------------------------------------------------------------------------

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def _load_template():
    from jinja2 import Environment, FileSystemLoader, StrictUndefined

    # 关闭 jinja2 自动 escape — 我们通过 _escape() 手动 escape + redact，
    # 避免双重 escape（jinja2 autoescape 会把已 escape 的 &lt; 再变 &amp;lt;）。
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
        trim_blocks=False,
        lstrip_blocks=False,
    )
    return env.get_template("weekly.html.j2")


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _escape(s: Optional[str]) -> str:
    if s is None:
        return ""
    return html.escape(_coarse_redact(s) or "", quote=True)


def render_weekly_html(
    runs: Optional[Sequence[dict]] = None,
    *,
    report: Optional[WeeklyReport] = None,
    window_days: int = 7,
    path: Optional[os.PathLike] = None,
    generated_at: Optional[str] = None,
) -> str:
    """渲染 HTML 周报。

    两种调用方式二选一：

    * ``runs`` — 已从 DB 读出的 raw rows（``fetch_recent_runs`` 返回值）；
      与 ``path`` 配套使用，``path`` 可设 None 让函数自建连接。
    * ``report`` — 已聚合的 :class:`WeeklyReport`（便于测试 / 多周对比）。

    注意：HTML 报告**不写盘**，调用方负责落盘（CLI 在 task 35 接入）。
    """

    if report is None:
        if runs is None:
            runs = fetch_recent_runs(window_days=window_days, path=path)
        report = aggregate_recent_runs(runs, window_days=window_days)
        if generated_at is not None:
            report.generated_at = generated_at

    template = _load_template()

    # 把 SystemAgg 转成视图层需要的 dict，再喂给模板
    systems_view: List[dict] = []
    for agg in report.systems:
        systems_view.append(
            {
                "system": agg.system,
                "total": agg.total,
                "healthy": agg.healthy,
                "degraded": agg.degraded,
                "failed": agg.failed,
                "skipped": agg.skipped,
                "connectivity_pct": _pct(agg.connectivity),
                "trend_spark": agg.trend_spark,
                "anomalies": [
                    {
                        "vendor_id": _escape(a["vendor_id"]),
                        "interface_id": _escape(a["interface_id"]),
                        "first_failed_step": _escape(a["first_failed_step"]),
                        "failure_class": _escape(a["failure_class"]),
                        "severity": _escape(a["severity"]),
                        "evidence_hash_short": _escape(a["evidence_hash_short"]),
                        "started_at": _escape(a["started_at"]),
                        "status": _escape(a["status"]),
                    }
                    for a in agg.anomalies
                ],
            }
        )

    rendered = template.render(
        generated_at=_escape(report.generated_at),
        week_start=_escape(report.week_start[:10]),
        week_end=_escape(report.week_end[:10]),
        window_days=report.window_days,
        total_runs=report.total_runs,
        systems=systems_view,
    )
    # 二次脱敏防御
    return _coarse_redact(rendered) or ""


# ---------------------------------------------------------------------------
# 路径 + 落盘
# ---------------------------------------------------------------------------


def _today() -> str:
    return _now_utc().strftime("%Y%m%d")


def weekly_html_path(base: Optional[os.PathLike] = None) -> Path:
    """计算默认 weekly.html 写入路径。

    优先级：``base`` 入参 → ``$VENDOR_DOCTOR_WEEKLY_DIR`` →
    ``./out/weekly/<YYYYMMDD>/weekly.html``。

    ``base`` 如果以 ``.html`` 结尾则视为完整文件路径（含文件名）；
    否则视为目录，会再拼上 ``<YYYYMMDD>/weekly.html``。
    """

    env = os.environ.get("VENDOR_DOCTOR_WEEKLY_DIR")

    if base is not None:
        p = Path(base)
        # 完整文件路径（含文件名）
        if p.suffix == ".html":
            target = p
        else:
            target = p / _today() / "weekly.html"
    elif env:
        p = Path(env)
        if p.suffix == ".html":
            target = p
        else:
            target = p / _today() / "weekly.html"
    else:
        target = Path.cwd() / "out" / "weekly" / _today() / "weekly.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def write_weekly_html(
    runs: Optional[Sequence[dict]] = None,
    *,
    report: Optional[WeeklyReport] = None,
    window_days: int = 7,
    db_path: Optional[os.PathLike] = None,
    path: Optional[os.PathLike] = None,
    generated_at: Optional[str] = None,
) -> Path:
    """渲染并落盘。``db_path`` 仅在 ``runs is None`` 时使用（与 ``path``
    入参共存时：``path`` 决定 HTML 落盘路径，``db_path`` 决定 SQLite 来源）。
    """

    if report is None and runs is None:
        html_text = render_weekly_html(
            None,
            window_days=window_days,
            path=db_path,
            generated_at=generated_at,
        )
    else:
        html_text = render_weekly_html(
            runs,
            report=report,
            window_days=window_days,
            path=db_path,
            generated_at=generated_at,
        )

    out_path = Path(path) if path is not None else weekly_html_path()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_text, encoding="utf-8")
    return out_path


__all__ = [
    "WeeklyReport",
    "SystemAgg",
    "aggregate_recent_runs",
    "fetch_recent_runs",
    "render_weekly_html",
    "weekly_html_path",
    "write_weekly_html",
    "_coarse_redact",
    "_system_key",
]
