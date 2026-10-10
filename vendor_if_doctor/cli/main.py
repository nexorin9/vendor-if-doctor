"""typer 应用入口 — ``python -m vendor_if_doctor`` 或 ``vendor-if-doctor`` console script。

子命令
------

* :func:`init_cmd` ``init`` — 生成 vendor_spec.yaml 模板；
* :func:`lint_cmd` ``lint`` — 校验 vendor_spec.yaml 并输出人类可读报告（CI 友好）；
* :func:`run_cmd` ``run`` — 跑一次巡检并落 handover.md + tickets.jsonl + SQLite；
* :func:`history_cmd` ``history`` — 查 SQLite 巡检历史。

启动期约定
----------

* :func:`main` 入口调 :func:`setup_logging` 一次；后续子命令的 logger 都基于
  ``vendor_if_doctor.*`` 命名空间；
* 任何子命令抛 :class:`SpecValidationError` 时，CLI 打印字段名（不打印值）
  并以非零退出码退出（CI 友好）。
"""

from __future__ import annotations

import logging
import sys

import typer

from vendor_if_doctor import __version__
from vendor_if_doctor.cli.history_cmd import history_cmd, history_show
from vendor_if_doctor.cli.init_cmd import init_cmd
from vendor_if_doctor.cli.lint_cmd import lint_cmd
from vendor_if_doctor.cli.run_cmd import run_cmd
from vendor_if_doctor.connection.plugin import load_plugins as _load_plugins
from vendor_if_doctor.logging_setup import setup_logging
from vendor_if_doctor.spec.errors import SpecValidationError

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="vendor-if-doctor",
    help="医院厂商接口连通巡检库（信息科值班员日跑一条命令）。",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"vendor-if-doctor {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="打印版本号并退出",
    ),
    log_level: str = typer.Option(
        "INFO",
        "--log-level",
        envvar="VENDOR_DOCTOR_LOG_LEVEL",
        help="日志级别（DEBUG/INFO/WARNING/ERROR）",
    ),
) -> None:
    """vendor-if-doctor 全局选项。"""
    setup_logging(log_level)
    # 启动期插件加载（task 33）：读 VENDOR_DOCTOR_PLUGINS env，逐个 import，
    # 让插件模块在模块体里完成 register_protocol_via_plugin 调用。
    try:
        loaded = _load_plugins()
        if loaded:
            logger.info("plugins loaded: %s", ",".join(loaded))
    except Exception as exc:  # pragma: no cover - import errors propagate
        logger.warning("plugin loading failed during CLI startup: %s", exc)


# 注册子命令
app.command("init", help="生成 vendor_spec.yaml 模板")(init_cmd)
app.command("lint", help="校验 vendor_spec.yaml 并输出人类可读报告")(lint_cmd)
app.command("run", help="跑一次厂商接口连通巡检")(run_cmd)
app.command("history", help="查 SQLite 巡检历史")(history_cmd)
app.command("history-show", help="按 run_id 查一条 run 的 steps_json")(history_show)


def main() -> None:
    """``python -m vendor_if_doctor`` 入口；捕获顶层异常保证非零退出码。"""

    setup_logging()
    try:
        app()
    except SpecValidationError as exc:
        typer.echo(
            f"[SpecValidationError] {len(exc.issues)} issue(s) in {exc.source or '<spec>'}:",
            err=True,
        )
        for issue in exc.issues:
            typer.echo(
                f"  - field={issue.field} rule={issue.rule} hint={issue.hint}",
                err=True,
            )
        sys.exit(2)
    except KeyboardInterrupt:  # pragma: no cover
        typer.echo("Interrupted by user", err=True)
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = ["app", "main"]