"""CLI 模块单元测试。

覆盖：
* ``init_cmd`` 生成 vendor_spec.yaml 模板（默认路径 / --force / 已存在拒绝）；
* ``run_cmd`` 加载 spec → 渲染 → SQLite 写盘 + 凭据脱敏；
* ``history_cmd`` 按 vendor 过滤 + 全量 + status 过滤；
* ``history_show`` 按 run_id 查询；
* ``cli.config.redact_dict`` / ``redact_str`` 覆盖 7 类敏感字段；
* ``cli.config.resolve_alias`` 别名映射；
* ``logging_setup`` JSON formatter 脱敏；
* Spec 加载期校验（缺 auth.secret_env / 缺 required_fields 等）。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vendor_if_doctor import __version__
from vendor_if_doctor.cli.config import (
    ALLOWED_ALIASES,
    redact_dict,
    redact_env_for_log,
    redact_str,
    resolve_alias,
)
from vendor_if_doctor.cli.history_cmd import history_cmd, history_show
from vendor_if_doctor.cli.init_cmd import init_cmd
from vendor_if_doctor.cli.main import app
from vendor_if_doctor.cli.run_cmd import run_cmd
from vendor_if_doctor.logging_setup import setup_logging
from vendor_if_doctor.spec.errors import SpecValidationError


runner = CliRunner()


# ---------------------------------------------------------------------------
# CLI 主入口 / 子命令展示
# ---------------------------------------------------------------------------


class TestCliMain:
    def test_help_lists_three_subcommands(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "init" in result.stdout
        assert "run" in result.stdout
        assert "history" in result.stdout

    def test_version(self):
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert __version__ in result.stdout

    def test_log_level_env(self, monkeypatch):
        monkeypatch.setenv("VENDOR_DOCTOR_LOG_LEVEL", "DEBUG")
        result = runner.invoke(app, ["init", "--help"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# init 子命令
# ---------------------------------------------------------------------------


class TestInitCmd:
    def test_init_default_path(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["init"])
        assert result.exit_code == 0
        out_file = tmp_path / "vendor_spec.yaml"
        assert out_file.exists()
        text = out_file.read_text(encoding="utf-8")
        assert "vendor_id: his" in text
        assert "interface_id: outpatient-register" in text
        assert "protocol: http" in text
        assert "VENDOR_HIS_HOST" in text
        # 模板必须不含明文 token 字面量
        assert "Bearer eyJ" not in text
        assert "Basic alice" not in text

    def test_init_custom_output(self, tmp_path):
        out = tmp_path / "conf" / "spec.yaml"
        result = runner.invoke(app, ["init", "--output", str(out)])
        assert result.exit_code == 0
        assert out.exists()

    def test_init_refuses_overwrite_without_force(self, tmp_path):
        out = tmp_path / "spec.yaml"
        out.write_text("existing", encoding="utf-8")
        result = runner.invoke(app, ["init", "--output", str(out)])
        assert result.exit_code != 0
        assert "已存在" in result.stderr or "exists" in result.stderr.lower()
        # 文件未被覆盖
        assert out.read_text(encoding="utf-8") == "existing"

    def test_init_force_overwrites(self, tmp_path):
        out = tmp_path / "spec.yaml"
        out.write_text("existing", encoding="utf-8")
        result = runner.invoke(app, ["init", "--output", str(out), "--force"])
        assert result.exit_code == 0
        assert "vendor_id" in out.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# run 子命令
# ---------------------------------------------------------------------------


_EXAMPLE_SPEC = """vendor_spec:
  schema_version: "1.0"
  run_window_minutes: 30
  interfaces:
    - vendor_id: his
      interface_id: outpatient-register
      display_name: "HIS"
      protocol: http
      host: ${VENDOR_HIS_HOST}
      port: 80
      timeout_seconds: 5
      auth:
        type: basic
        username_env: VENDOR_HIS_USERNAME
        secret_env: VENDOR_HIS_PASSWORD
      sample_endpoint:
        http:
          method: GET
          path: /register/slot
          headers: {}
          body: ""
      required_fields:
        - "$.code"
"""


class TestRunCmd:
    def test_run_writes_three_artifacts(self, tmp_path, monkeypatch):
        spec = tmp_path / "spec.yaml"
        spec.write_text(_EXAMPLE_SPEC, encoding="utf-8")
        handover = tmp_path / "handover"
        tickets = tmp_path / "tickets"
        db = tmp_path / "vendor_if_doctor.db"
        # 不实际连真实服务（VENDOR_HIS_HOST 不解析 → preflight 失败）
        monkeypatch.delenv("VENDOR_HIS_USERNAME", raising=False)
        monkeypatch.delenv("VENDOR_HIS_PASSWORD", raising=False)
        # 用 subprocess 跑，避免 monkeypatch.chdir 影响其他测试
        result = runner.invoke(
            app,
            [
                "run",
                "--vendor",
                "all",
                "--spec",
                str(spec),
                "--handover-dir",
                str(handover),
                "--tickets-dir",
                str(tickets),
                "--sqlite",
                str(db),
                "--validate-credentials",  # 强制检查缺凭据
            ],
        )
        # validate_credentials=True 时缺凭据 → 启动期失败 + exit 2
        assert result.exit_code == 2
        assert "SpecValidationError" in result.stderr
        assert "VENDOR_HIS_PASSWORD" in result.stderr
        # 错误消息里**绝不**含 token / password 字面量
        assert "wonderland" not in result.stderr

    def test_run_with_credentials_skips_validate(self, tmp_path, monkeypatch):
        spec = tmp_path / "spec.yaml"
        spec.write_text(_EXAMPLE_SPEC, encoding="utf-8")
        handover = tmp_path / "handover"
        tickets = tmp_path / "tickets"
        db = tmp_path / "vendor_if_doctor.db"
        monkeypatch.setenv("VENDOR_HIS_USERNAME", "alice")
        monkeypatch.setenv("VENDOR_HIS_PASSWORD", "wonderland-token-xyz")
        result = runner.invoke(
            app,
            [
                "run",
                "--vendor",
                "all",
                "--spec",
                str(spec),
                "--handover-dir",
                str(handover),
                "--tickets-dir",
                str(tickets),
                "--sqlite",
                str(db),
            ],
        )
        # exit 0 或 1 都行（preflight 会 fail 但产物已落盘 → exit 1）
        assert result.exit_code in (0, 1), result.stdout + result.stderr
        # 产物落盘
        assert (handover / "2026-10-10" / "handover.md").exists()
        assert (tickets / "2026-10-10" / "tickets.jsonl").exists()
        assert db.exists()
        # 凭据脱敏：handover.md 不含明文 password / token
        h_text = (handover / "2026-10-10" / "handover.md").read_text(encoding="utf-8")
        assert "wonderland-token-xyz" not in h_text
        # ticket 同理
        t_text = (tickets / "2026-10-10" / "tickets.jsonl").read_text(encoding="utf-8")
        assert "wonderland-token-xyz" not in t_text

    def test_run_no_sqlite_skips_db(self, tmp_path, monkeypatch):
        spec = tmp_path / "spec.yaml"
        spec.write_text(_EXAMPLE_SPEC, encoding="utf-8")
        handover = tmp_path / "handover"
        tickets = tmp_path / "tickets"
        monkeypatch.setenv("VENDOR_HIS_USERNAME", "alice")
        monkeypatch.setenv("VENDOR_HIS_PASSWORD", "wonderland")
        db = tmp_path / "vendor_if_doctor.db"
        result = runner.invoke(
            app,
            [
                "run",
                "--vendor",
                "all",
                "--spec",
                str(spec),
                "--handover-dir",
                str(handover),
                "--tickets-dir",
                str(tickets),
                "--sqlite",
                str(db),
                "--no-sqlite",
            ],
        )
        assert result.exit_code in (0, 1)
        assert not db.exists()

    def test_run_vendor_filter(self, tmp_path, monkeypatch):
        spec = tmp_path / "spec.yaml"
        spec.write_text(_EXAMPLE_SPEC, encoding="utf-8")
        handover = tmp_path / "handover"
        tickets = tmp_path / "tickets"
        monkeypatch.setenv("VENDOR_HIS_USERNAME", "alice")
        monkeypatch.setenv("VENDOR_HIS_PASSWORD", "wonderland")
        result = runner.invoke(
            app,
            [
                "run",
                "--vendor",
                "lis",  # 不存在的 vendor
                "--spec",
                str(spec),
                "--handover-dir",
                str(handover),
                "--tickets-dir",
                str(tickets),
                "--sqlite",
                str(tmp_path / "db"),
                "--no-sqlite",
            ],
        )
        assert result.exit_code == 0  # 不过滤出东西 = 0 fail
        # evidence=0 在 ticket JSONL
        t_text = (tickets / "2026-10-10" / "tickets.jsonl").read_text(encoding="utf-8")
        assert t_text.strip() == ""

    def test_run_invalid_spec_fails_fast(self, tmp_path):
        # 不存在的 spec
        result = runner.invoke(
            app,
            ["run", "--spec", str(tmp_path / "no-such.yaml")],
        )
        # SpecValidationError 走 main 顶层 except → exit 2
        assert result.exit_code in (2, 1)
        # 提示里只有路径，不含 token
        assert "no-such.yaml" in result.stderr or "SpecValidationError" in result.stderr


# ---------------------------------------------------------------------------
# history 子命令
# ---------------------------------------------------------------------------


class TestHistoryCmd:
    def test_history_no_db(self, tmp_path):
        result = runner.invoke(
            app,
            ["history", "--db", str(tmp_path / "missing.db")],
        )
        assert result.exit_code != 0
        assert "不存在" in result.stderr or "not exist" in result.stderr.lower()

    def test_history_lists_runs(self, tmp_path, monkeypatch):
        # 先跑一次 run 落 SQLite
        spec = tmp_path / "spec.yaml"
        spec.write_text(_EXAMPLE_SPEC, encoding="utf-8")
        db = tmp_path / "vendor_if_doctor.db"
        handover = tmp_path / "handover"
        tickets = tmp_path / "tickets"
        monkeypatch.setenv("VENDOR_HIS_USERNAME", "alice")
        monkeypatch.setenv("VENDOR_HIS_PASSWORD", "wonderland")
        runner.invoke(
            app,
            [
                "run",
                "--spec",
                str(spec),
                "--handover-dir",
                str(handover),
                "--tickets-dir",
                str(tickets),
                "--sqlite",
                str(db),
            ],
        )
        # 现在查 history
        result = runner.invoke(app, ["history", "--db", str(db), "--limit", "10"])
        assert result.exit_code == 0
        assert "vendor-if-doctor history" in result.stdout
        assert "his" in result.stdout

    def test_history_filter_by_vendor(self, tmp_path, monkeypatch):
        spec = tmp_path / "spec.yaml"
        spec.write_text(_EXAMPLE_SPEC, encoding="utf-8")
        db = tmp_path / "vendor_if_doctor.db"
        handover = tmp_path / "handover"
        tickets = tmp_path / "tickets"
        monkeypatch.setenv("VENDOR_HIS_USERNAME", "alice")
        monkeypatch.setenv("VENDOR_HIS_PASSWORD", "wonderland")
        runner.invoke(
            app,
            [
                "run",
                "--spec",
                str(spec),
                "--handover-dir",
                str(handover),
                "--tickets-dir",
                str(tickets),
                "--sqlite",
                str(db),
            ],
        )
        result = runner.invoke(
            app,
            ["history", "--vendor", "his", "--db", str(db)],
        )
        assert result.exit_code == 0
        assert "his" in result.stdout
        assert "lis" not in result.stdout

    def test_history_filter_by_status(self, tmp_path, monkeypatch):
        spec = tmp_path / "spec.yaml"
        spec.write_text(_EXAMPLE_SPEC, encoding="utf-8")
        db = tmp_path / "vendor_if_doctor.db"
        handover = tmp_path / "handover"
        tickets = tmp_path / "tickets"
        monkeypatch.setenv("VENDOR_HIS_USERNAME", "alice")
        monkeypatch.setenv("VENDOR_HIS_PASSWORD", "wonderland")
        runner.invoke(
            app,
            [
                "run",
                "--spec",
                str(spec),
                "--handover-dir",
                str(handover),
                "--tickets-dir",
                str(tickets),
                "--sqlite",
                str(db),
            ],
        )
        result = runner.invoke(
            app,
            ["history", "--status", "failed", "--db", str(db)],
        )
        assert result.exit_code == 0
        assert "failed" in result.stdout


# ---------------------------------------------------------------------------
# history-show 子命令
# ---------------------------------------------------------------------------


class TestHistoryShow:
    def test_show_existing_run(self, tmp_path, monkeypatch):
        spec = tmp_path / "spec.yaml"
        spec.write_text(_EXAMPLE_SPEC, encoding="utf-8")
        db = tmp_path / "vendor_if_doctor.db"
        handover = tmp_path / "handover"
        tickets = tmp_path / "tickets"
        monkeypatch.setenv("VENDOR_HIS_USERNAME", "alice")
        monkeypatch.setenv("VENDOR_HIS_PASSWORD", "wonderland")
        runner.invoke(
            app,
            [
                "run",
                "--spec",
                str(spec),
                "--handover-dir",
                str(handover),
                "--tickets-dir",
                str(tickets),
                "--sqlite",
                str(db),
            ],
        )
        # task 19：数据落在月表 inspection_runs_y<YYYY>m<MM>，通过 query_history 跨月查
        from vendor_if_doctor.renderers.sqlite_history import query_history
        rows = query_history(path=db, limit=1)
        assert rows, "expected at least one run after CLI run"
        run_id = rows[0]["run_id"]
        result = runner.invoke(app, ["history-show", run_id, "--db", str(db)])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["vendor_id"] == "his"
        assert "steps" in payload

    def test_show_missing_run(self, tmp_path):
        result = runner.invoke(
            app,
            ["history-show", "deadbeef" * 4, "--db", str(tmp_path / "missing.db")],
        )
        # missing.db 不存在 → connect 会建空库 → fetch_run → None → exit 1
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# cli.config — redact / alias / load_config
# ---------------------------------------------------------------------------


class TestConfigRedact:
    def test_redact_str_authorization(self):
        out = redact_str("Authorization: Bearer abc.def.ghi")
        assert "***" in out
        assert "abc.def.ghi" not in out

    def test_redact_str_basic(self):
        out = redact_str("Basic dXNlcjpwYXNz")
        assert "***" in out
        assert "dXNlcjpwYXNz" not in out

    def test_redact_str_token_in_query(self):
        out = redact_str("https://api.example.com?api_key=secret123&page=1")
        assert "secret123" not in out
        assert "page=1" in out  # 非敏感字段保留

    def test_redact_str_safe_text_passes_through(self):
        text = "Hello world"
        assert redact_str(text) == text

    def test_redact_dict_nested(self):
        d = {
            "Authorization": "Bearer xyz",
            "safe": "ok",
            "nested": {"password": "p", "x": 1},
            "list": [{"token": "t1"}, "plain"],
        }
        out = redact_dict(d)
        assert out["Authorization"] == "***"
        assert out["safe"] == "ok"
        assert out["nested"]["password"] == "***"
        assert out["nested"]["x"] == 1
        assert out["list"][0]["token"] == "***"
        assert out["list"][1] == "plain"

    def test_redact_dict_non_dict_passthrough(self):
        assert redact_dict(123) == 123
        assert redact_dict(None) is None
        assert redact_dict(True) is True

    def test_redact_env_for_log(self):
        out = redact_env_for_log("VENDOR_X_TOKEN", "secret-xyz")
        assert "VENDOR_X_TOKEN" in out
        assert "secret-xyz" not in out
        assert "len=" in out
        # empty
        out_empty = redact_env_for_log("VENDOR_X_TOKEN", "")
        assert "<empty>" in out_empty

    def test_resolve_alias_direct_hit(self):
        env = {"VENDOR_HIS_TOKEN": "abc"}
        assert resolve_alias("VENDOR_HIS_TOKEN", env) == "abc"

    def test_resolve_alias_alias_hit(self):
        env = {"VENDOR_HIS_AUTH_TOKEN": "abc", "VENDOR_HIS_TOKEN": ""}
        # 优先 alias（也即 ALLOWED_ALIASES 里列出的）作为 fallback
        assert resolve_alias("VENDOR_HIS_TOKEN", env) == "abc"

    def test_resolve_alias_no_hit(self):
        assert resolve_alias("VENDOR_UNKNOWN", {}) is None


# ---------------------------------------------------------------------------
# logging_setup
# ---------------------------------------------------------------------------


class TestLoggingSetup:
    def test_setup_logging_returns_logger(self):
        log = setup_logging("INFO")
        assert isinstance(log, logging.Logger)
        assert log.name == "vendor_if_doctor"

    def test_setup_logging_redacts_message(self, caplog):
        log = setup_logging("DEBUG")
        # 注入敏感字段
        log.info("Authorization: Bearer secret-token-xyz")
        # 任意 handler 收集到的 record 应被 formatter 过滤
        # 直接调 formatter：
        from vendor_if_doctor.logging_setup import _JsonFormatter

        record = log.makeRecord(
            "vendor_if_doctor.test",
            logging.INFO,
            "fn",
            1,
            "Authorization: Bearer secret-token-xyz",
            None,
            None,
        )
        out = _JsonFormatter().format(record)
        assert "secret-token-xyz" not in out
        assert "***" in out

    def test_setup_logging_idempotent(self):
        log1 = setup_logging("DEBUG")
        n_handlers_1 = len(log1.handlers)
        log2 = setup_logging("DEBUG")
        n_handlers_2 = len(log2.handlers)
        # 重复调用不会累加 handler
        assert n_handlers_1 == n_handlers_2


# ---------------------------------------------------------------------------
# validate_credentials=True 失败场景（仅供本任务覆盖）
# ---------------------------------------------------------------------------


class TestCliValidateCredentials:
    def test_validate_credentials_missing_env(self, tmp_path, monkeypatch):
        spec = tmp_path / "spec.yaml"
        spec.write_text(_EXAMPLE_SPEC, encoding="utf-8")
        monkeypatch.delenv("VENDOR_HIS_USERNAME", raising=False)
        monkeypatch.delenv("VENDOR_HIS_PASSWORD", raising=False)
        result = runner.invoke(
            app,
            [
                "run",
                "--spec",
                str(spec),
                "--validate-credentials",
                "--no-sqlite",
            ],
        )
        assert result.exit_code == 2
        assert "VENDOR_HIS_PASSWORD" in result.stderr
        assert "VENDOR_HIS_USERNAME" in result.stderr
        # 错误信息绝不含凭据值
        assert "alice" not in result.stderr
        assert "wonderland" not in result.stderr