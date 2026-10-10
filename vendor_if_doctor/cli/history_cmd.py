"""history 子命令 — 查 SQLite 巡检历史。

* ``--vendor`` 过滤；不传 = 全部；
* ``--limit`` / ``--status`` 控制返回范围；
* 输出人类可读 Markdown 表格（CI 也好 grep）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import typer

from vendor_if_doctor.renderers.sqlite_history import (
    db_path,
    fetch_run,
    fetch_runs_by_vendor,
    query_history,
)


def _format_table(rows: list[dict]) -> str:
    if not rows:
        return "(无记录)"
    headers = ("run_id", "vendor_id", "interface_id", "started_at", "finished_at", "status", "evidence_hash")
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        lines.append("| " + " | ".join(str(r.get(h, "")) for h in headers) + " |")
    return "\n".join(lines)


def history_cmd(
    vendor: Optional[str] = typer.Option(
        None,
        "--vendor",
        "-v",
        help="按 vendor_id 过滤；不传 = 全部",
    ),
    limit: int = typer.Option(
        30,
        "--limit",
        "-n",
        min=1,
        max=500,
        help="返回记录条数上限",
    ),
    status: Optional[str] = typer.Option(
        None,
        "--status",
        help="按 status 过滤（healthy/degraded/failed/skipped）",
    ),
    db: Optional[Path] = typer.Option(
        None,
        "--db",
        help="SQLite 路径（默认 ./vendor_if_doctor.db，可被 $VENDOR_DOCTOR_DB 覆盖）",
    ),
    show_steps: bool = typer.Option(
        False,
        "--show-steps",
        help="展开每条 run 的 steps_json（默认仅显示聚合字段）",
    ),
) -> None:
    """查 SQLite 巡检历史。"""

    target_db = db or Path(os.environ.get("VENDOR_DOCTOR_DB", str(db_path())))

    # task 19：跨月 UNION ALL 查询（不论 vendor 是否提供）
    if vendor:
        rows = fetch_runs_by_vendor(vendor, limit=limit, path=target_db)
    else:
        if not target_db.exists():
            typer.echo(f"SQLite {target_db} 不存在；先跑一次 run", err=True)
            raise typer.Exit(code=1)
        rows = query_history(limit=limit, path=target_db)

    if status:
        rows = [r for r in rows if r.get("status") == status]

    typer.echo(f"# vendor-if-doctor history — {len(rows)} run(s) (db={target_db})")
    typer.echo(_format_table(rows))

    if show_steps and rows:
        typer.echo("")
        typer.echo("## steps_json 详情")
        for r in rows[:5]:
            full = fetch_run(r["run_id"], path=target_db)
            if full:
                typer.echo(f"\n### {r['run_id']} ({r['vendor_id']}.{r['interface_id']})")
                steps = full.get("steps") or []
                for s in steps:
                    typer.echo(
                        f"- step={s.get('step_name')} status={s.get('status')} "
                        f"fc={s.get('failure_class')} latency={s.get('latency_ms')}ms "
                        f"hash={(s.get('evidence_hash') or '')[:8]}"
                    )
                meta = full.get("meta")
                if meta:
                    typer.echo(f"  meta: {meta}")


def history_show(
    run_id: str = typer.Argument(..., help="要查的 run_id（16 位 hex）"),
    db: Optional[Path] = typer.Option(
        None,
        "--db",
        help="SQLite 路径",
    ),
) -> None:
    """按 run_id 查一条 run 的完整 steps_json。"""

    target_db = db or Path(os.environ.get("VENDOR_DOCTOR_DB", str(db_path())))
    full = fetch_run(run_id, path=target_db)
    if not full:
        typer.echo(f"run_id={run_id} 不存在", err=True)
        raise typer.Exit(code=1)
    typer.echo(json.dumps(full, ensure_ascii=False, indent=2))


__all__ = ["history_cmd", "history_show"]