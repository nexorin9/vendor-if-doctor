"""vendor-if-doctor CLI 模块。

对外导出 :data:`app`（typer 应用对象），由 ``python -m vendor_if_doctor`` 或
``vendor-if-doctor`` console script 入口。

子命令：

* :func:`init_cmd.init_cmd` ``init`` — 在当前目录（或 ``--output``）生成
  ``vendor_spec.yaml`` 模板；
* :func:`run_cmd.run_cmd` ``run`` — 加载 spec → 跑 :func:`test_connection` →
  渲染 ``handover.md`` + ``tickets.jsonl`` + 写 SQLite；
* :func:`history_cmd.history_cmd` ``history`` — 查 SQLite 巡检历史（按 vendor /
  全量）；:func:`history_cmd.history_show` ``history-show`` — 按 run_id 查一条。

凭据纪律
--------

* CLI 启动时强制走 :func:`logging_setup.setup_logging`，所有日志记录先过
  :func:`vendor_if_doctor.cli.config.redact_str` 兜底（永不打 Authorization /
  Bearer / token / password 字面量）；
* ``run`` 子命令在打印任何错误信息时只显示字段名（spec 字段），不显示值。
"""

from __future__ import annotations

from vendor_if_doctor.cli.main import app, main

__all__ = ["app", "main"]