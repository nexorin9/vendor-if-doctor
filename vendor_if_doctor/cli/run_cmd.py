"""run 子命令 — 加载 spec → 跑 :func:`test_connection` → 渲染 handover.md +
tickets.jsonl + 写 SQLite。

凭据纪律
--------

* ``--validate-credentials`` 强制读 env；缺凭据时抛 :class:`SpecValidationError`
  并打印字段名（不打印值）；
* Evidence 在收集阶段已被 ``_coarse_redact`` 处理；handover.md / tickets.jsonl
  / 日志再过 :func:`vendor_if_doctor.cli.config.redact_str` 兜底。
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import typer

from vendor_if_doctor.cli.config import redact_env_for_log, redact_str
from vendor_if_doctor.connection.async_runner import run_all_async
from vendor_if_doctor.connection.evidence import Evidence
from vendor_if_doctor.connection.runner import test_connection
from vendor_if_doctor.renderers.handover_md import write_handover
from vendor_if_doctor.renderers.sqlite_history import write_history
from vendor_if_doctor.renderers.ticket_jsonl import write_tickets
from vendor_if_doctor.spec.errors import SpecValidationError
from vendor_if_doctor.spec.loader import load_vendor_spec

logger = logging.getLogger(__name__)


def _filter_by_vendor(evidence_list: List[Evidence], vendor: str) -> List[Evidence]:
    """按 vendor 过滤 evidence 列表；``all`` 不过滤。"""

    if vendor == "all" or not vendor:
        return evidence_list
    out: List[Evidence] = []
    for ev in evidence_list:
        if ev.vendor_id == vendor:
            out.append(ev)
    if not out:
        logger.warning("vendor=%s 在 spec 中无匹配 interface", vendor)
    return out


def _today_str() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


def _resolve_handover_dir(base: Optional[Path]) -> Path:
    env = os.environ.get("VENDOR_DOCTOR_HANDOVER_DIR")
    if base is not None:
        target = base
    elif env:
        target = Path(env) / _today_str()
    else:
        target = Path.cwd() / "out" / _today_str()
    target.mkdir(parents=True, exist_ok=True)
    return target


def _resolve_tickets_dir(base: Optional[Path]) -> Path:
    env = os.environ.get("VENDOR_DOCTOR_TICKETS_DIR")
    if base is not None:
        target = base
    elif env:
        target = Path(env) / _today_str()
    else:
        target = Path.cwd() / "out" / _today_str()
    target.mkdir(parents=True, exist_ok=True)
    return target


def _resolve_sqlite_path(base: Optional[Path]) -> Path:
    env = os.environ.get("VENDOR_DOCTOR_DB")
    if base is not None:
        target = base
    elif env:
        target = Path(env)
    else:
        target = Path.cwd() / "vendor_if_doctor.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def run_cmd(
    vendor: str = typer.Option(
        "all",
        "--vendor",
        "-v",
        help='vendor_id（his / lis / pacs / emr / ...）；"all" = 全部',
    ),
    spec: Path = typer.Option(
        "examples/vendor_spec.example.yaml",
        "--spec",
        "-s",
        help="vendor_spec.yaml 路径（支持目录 / glob）",
    ),
    handover_dir: Optional[Path] = typer.Option(
        None,
        "--handover-dir",
        help="handover.md 输出目录（默认 out/<date>/，可被 $VENDOR_DOCTOR_HANDOVER_DIR 覆盖）",
    ),
    tickets_dir: Optional[Path] = typer.Option(
        None,
        "--tickets-dir",
        help="tickets.jsonl 输出目录",
    ),
    sqlite: Optional[Path] = typer.Option(
        None,
        "--sqlite",
        help="巡检历史 SQLite 路径（默认 ./vendor_if_doctor.db）",
    ),
    validate_credentials: bool = typer.Option(
        False,
        "--validate-credentials",
        help="启动期读取所有凭据 env；缺一就抛错（默认不强制）",
    ),
    no_sqlite: bool = typer.Option(
        False,
        "--no-sqlite",
        help="跳过 SQLite 写入（仅留 handover.md + tickets.jsonl）",
    ),
    async_run: bool = typer.Option(
        False,
        "--async",
        help="多 vendor 并发跑（asyncio.gather + semaphore），wall-time ≈ max 而非 sum",
    ),
    concurrency: int = typer.Option(
        10,
        "--concurrency",
        "-c",
        help="--async 模式下的并发上限（asyncio.Semaphore），默认 10",
    ),
) -> None:
    """跑一次厂商接口连通巡检。"""

    # 1) 加载 spec（启动期校验，不发起请求）
    try:
        vendor_spec = load_vendor_spec(
            spec,
            env=dict(os.environ),
            validate_credentials=validate_credentials,
        )
    except SpecValidationError as exc:
        typer.echo(
            f"[SpecValidationError] {len(exc.issues)} issue(s) in {exc.source}:",
            err=True,
        )
        for issue in exc.issues:
            typer.echo(
                f"  - field={issue.field} rule={issue.rule} hint={issue.hint}",
                err=True,
            )
        sys.exit(2)

    n_interfaces = len(vendor_spec.interfaces)
    logger.info(
        "loaded spec %s with %d interface(s); vendor=%s async=%s concurrency=%s",
        spec,
        n_interfaces,
        vendor,
        async_run,
        concurrency,
    )

    # 2) 跑连接测试（可能发起真实请求）
    try:
        if async_run:
            evidence_list = run_all_async(
                [vendor_spec],
                max_concurrency=concurrency,
                env=dict(os.environ),
            )
        else:
            evidence_list = test_connection(vendor_spec, env=dict(os.environ))
    except Exception as exc:
        logger.error("test_connection 异常退出: %s", type(exc).__name__)
        typer.echo(f"test_connection 失败: {redact_str(str(exc))[:200]}", err=True)
        sys.exit(3)

    evidence_list = _filter_by_vendor(evidence_list, vendor)
    n_ev = len(evidence_list)
    n_pass = sum(1 for ev in evidence_list if ev.status == "pass")
    n_fail = sum(1 for ev in evidence_list if ev.status == "fail")
    n_skip = sum(1 for ev in evidence_list if ev.status == "skip")
    n_deg = sum(1 for ev in evidence_list if ev.status == "degraded")

    # 3) 渲染 handover.md + tickets.jsonl
    h_dir = _resolve_handover_dir(handover_dir)
    t_dir = _resolve_tickets_dir(tickets_dir)
    # renderers 自己会再 append _today()，所以这里只覆盖 env 的「基础目录」部分
    # 若 h_dir/t_dir 是默认形式（out/<date>），还原为 out/
    h_base = h_dir.parent if h_dir.name == _today_str() else h_dir
    t_base = t_dir.parent if t_dir.name == _today_str() else t_dir
    h_base.mkdir(parents=True, exist_ok=True)
    t_base.mkdir(parents=True, exist_ok=True)
    os.environ["VENDOR_DOCTOR_HANDOVER_DIR"] = str(h_base)
    os.environ["VENDOR_DOCTOR_TICKETS_DIR"] = str(t_base)
    handover_path = write_handover(evidence_list)
    tickets_path = write_tickets(evidence_list)
    logger.info("wrote handover to %s", handover_path)
    logger.info("wrote tickets to %s", tickets_path)

    # 4) 写 SQLite（除非 --no-sqlite）
    run_ids: List[str] = []
    if not no_sqlite:
        db_path = _resolve_sqlite_path(sqlite)
        run_ids = write_history(
            evidence_list,
            meta={"vendor_filter": vendor, "spec_path": str(spec)},
            path=db_path,
        )
        logger.info("wrote %d run(s) to sqlite %s", len(run_ids), db_path)

    # 5) 输出汇总（人类可读，stdout）
    typer.echo(f"vendor-if-doctor run finished")
    typer.echo(f"  spec:           {spec}")
    typer.echo(f"  vendor filter:  {vendor}")
    typer.echo(f"  mode:           {'async (concurrency=' + str(concurrency) + ')' if async_run else 'sync'}")
    typer.echo(f"  interfaces:     {n_interfaces}")
    typer.echo(f"  evidence:       {n_ev} (pass={n_pass} fail={n_fail} degraded={n_deg} skip={n_skip})")
    typer.echo(f"  handover:       {handover_path}")
    typer.echo(f"  tickets:        {tickets_path}")
    if not no_sqlite:
        typer.echo(f"  sqlite runs:    {len(run_ids)}")

    # 凭据是否已 export（脱敏打印）
    used_envs: list[str] = []
    for itf in vendor_spec.interfaces:
        for env_name in itf.secret_envs:
            used_envs.append(env_name)
    for env_name in sorted(set(used_envs)):
        typer.echo(f"  credential:    {redact_env_for_log(env_name, os.environ.get(env_name))}")

    if n_fail > 0:
        sys.exit(1)


__all__ = ["run_cmd"]