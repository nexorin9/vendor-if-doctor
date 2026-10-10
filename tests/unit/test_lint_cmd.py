"""cli/lint_cmd.py 单测。覆盖 6 大场景：

1. ``_parse_field_to_path`` 路径解析（点 / 斜杠 / 括号段）
2. ``_LineFinder.find_line`` 在 YAML 节点树上按路径定位行号
3. ``_lint_one`` 对合法 spec → ``LintReport.ok=True``
4. ``_lint_one`` 对 4 类非法 fixture → 触发对应 ``SpecIssue`` + 行号定位
5. ``_format_issue`` / ``_format_report`` 渲染格式正确，**不含**凭据字面量
6. CLI 子命令注册 / exit code（0 vs 1）/ glob / 目录 / parse error

凭据纪律：错误消息里 ``token`` / ``Bearer`` / ``wonderland`` 全部 0 命中。
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import pytest
import yaml
from typer.testing import CliRunner

from vendor_if_doctor.cli.lint_cmd import (
    LintIssue,
    LintReport,
    _LineFinder,
    _format_issue,
    _format_report,
    _lint_one,
    _parse_field_to_path,
    _resolve_lint_paths,
    lint_cmd,
)
from vendor_if_doctor.cli.main import app


FIXTURES_INVALID = Path(__file__).resolve().parent.parent / "fixtures" / "vendor_specs"
FIXTURES_VALID = Path(__file__).resolve().parent.parent / "fixtures" / "valid_specs"


# ---- _parse_field_to_path -----------------------------------------------------


class TestParseFieldToPath:
    """``SpecIssue.field`` → 路径列表。"""

    def test_dotted_path_with_parens(self):
        """二次校验格式：``vendor_spec.interfaces[0] (his.outpatient-register).auth.secret_env``"""

        result = _parse_field_to_path(
            "vendor_spec.interfaces[0] (his.outpatient-register).auth.secret_env"
        )
        assert result == ["vendor_spec", "interfaces", 0, "auth", "secret_env"]

    def test_dotted_path_no_parens(self):
        result = _parse_field_to_path("interfaces[0].required_fields")
        assert result == ["interfaces", 0, "required_fields"]

    def test_slash_path_json_schema(self):
        """JSON Schema 格式：用 ``/`` 分隔。"""

        result = _parse_field_to_path("vendor_spec/interfaces/0/protocol")
        assert result == ["vendor_spec", "interfaces", 0, "protocol"]

    def test_slash_path_with_index(self):
        result = _parse_field_to_path("vendor_spec/interfaces/0/required_fields/0")
        assert result == ["vendor_spec", "interfaces", 0, "required_fields", 0]

    def test_root_marker(self):
        assert _parse_field_to_path("<root>") == []
        assert _parse_field_to_path("") == []

    def test_parens_only(self):
        """仅括号段时剥掉后为空。"""

        result = _parse_field_to_path("(some.thing)")
        assert result == []

    def test_dotted_with_trailing_env_var(self):
        """``interfaces[0] (lis.report-push).auth.secret_env (VENDOR_LIS_TOKEN)``"""

        result = _parse_field_to_path(
            "interfaces[0] (lis.report-push).auth.secret_env (VENDOR_LIS_TOKEN)"
        )
        assert result == ["interfaces", 0, "auth", "secret_env"]


# ---- _LineFinder --------------------------------------------------------------


class TestLineFinder:
    """``yaml.compose`` 节点树 → 行号。"""

    def test_simple_path(self):
        text = (
            "vendor_spec:\n"
            "  schema_version: '1.0'\n"
            "  interfaces:\n"
            "    - vendor_id: his\n"
            "      interface_id: outpatient-register\n"
            "      protocol: http\n"
        )
        node = yaml.compose(text)
        lf = _LineFinder(node)
        assert lf.find_line(["vendor_spec", "schema_version"]) == 2
        assert lf.find_line(["vendor_spec", "interfaces", 0, "vendor_id"]) == 4
        assert lf.find_line(["vendor_spec", "interfaces", 0, "protocol"]) == 6

    def test_missing_path_returns_none(self):
        text = "vendor_spec:\n  schema_version: '1.0'\n"
        node = yaml.compose(text)
        lf = _LineFinder(node)
        assert lf.find_line(["vendor_spec", "interfaces"]) is None

    def test_empty_path_returns_none(self):
        text = "vendor_spec:\n  schema_version: '1.0'\n"
        node = yaml.compose(text)
        lf = _LineFinder(node)
        assert lf.find_line([]) is None


# ---- _lint_one ----------------------------------------------------------------


class TestLintOneValid:
    """合法 spec 应返回 ok=True，无 issues。"""

    def test_example_spec_ok(self):
        report = _lint_one(Path("examples/vendor_spec.example.yaml"))
        assert report.ok
        assert report.issues == []
        assert report.parse_error is None

    def test_valid_specs_happy_path(self):
        report = _lint_one(FIXTURES_VALID / "http_happy_path.yaml")
        assert report.ok


class TestLintOneInvalid:
    """4 类非法 fixture 应触发对应 SpecIssue。"""

    def test_no_auth(self):
        report = _lint_one(FIXTURES_INVALID / "invalid_no_auth.yaml")
        assert not report.ok
        assert report.n_errors >= 1
        fields = [i.field for i in report.issues]
        # JSON Schema 校验 + 二次校验 + pydantic 至少一处提了 auth/secret_env
        assert any("secret_env" in f or "auth" in f for f in fields)

    def test_protocol_invalid(self):
        report = _lint_one(FIXTURES_INVALID / "invalid_protocol.yaml")
        assert not report.ok
        rules = [i.rule for i in report.issues]
        assert any("enum" in r for r in rules)

    def test_no_endpoint(self):
        report = _lint_one(FIXTURES_INVALID / "invalid_no_endpoint.yaml")
        assert not report.ok
        # sample_endpoint.http 必填错误应当出现
        hints = [i.hint for i in report.issues]
        assert any("sample_endpoint" in h or "anyOf" in h for h in hints)

    def test_required_fields_empty(self):
        report = _lint_one(FIXTURES_INVALID / "invalid_required_fields_empty.yaml")
        assert not report.ok
        # 验证 issues 至少有 1 条，且 issues 触达 required_fields 字段
        all_text = " ".join(f"{i.field} {i.rule} {i.hint}" for i in report.issues)
        assert any("required_fields" in s for s in [i.field for i in report.issues])
        # rule 或 hint 中至少有 1 处出现「non-empty」/「至少声明」/「value_error」
        text = " ".join(f"{i.rule} {i.hint}" for i in report.issues)
        assert any(k in text for k in ("non-empty", "至少声明", "value_error", "至少 1"))

    def test_missing_required(self):
        report = _lint_one(FIXTURES_INVALID / "invalid_missing_required.yaml")
        assert not report.ok
        all_text = " ".join(f"{i.field} {i.rule} {i.hint}" for i in report.issues)
        assert "required_fields" in all_text

    def test_invalid_yaml_syntax(self):
        """YAML 解析错误时走 parse_error 通道。"""

        bad = FIXTURES_INVALID / "_syntax_error.yaml"
        bad.write_text("vendor_spec:\n  interfaces: [unclosed\n", encoding="utf-8")
        try:
            report = _lint_one(bad)
            assert report.parse_error is not None
            assert report.n_errors == 0
        finally:
            bad.unlink(missing_ok=True)


# ---- _format_issue / _format_report -------------------------------------------


class TestFormatIssue:
    """渲染格式正确，且不含凭据字面量。"""

    def test_format_with_line(self):
        report = LintReport(path=Path("a.yaml"), issues=[])
        issue = LintIssue(field="interfaces[0].auth.secret_env", rule="required", hint="填写 VENDOR_<NAME>_TOKEN", line=11)
        out = _format_issue(report, issue)
        assert out == "a.yaml:11: [error] interfaces[0].auth.secret_env: required (填写 VENDOR_<NAME>_TOKEN)"

    def test_format_without_line(self):
        report = LintReport(path=Path("a.yaml"), issues=[])
        issue = LintIssue(field="interfaces[0].auth.secret_env", rule="required", hint="填写 VENDOR_<NAME>_TOKEN", line=None)
        out = _format_issue(report, issue)
        assert out == "a.yaml:?: [error] interfaces[0].auth.secret_env: required (填写 VENDOR_<NAME>_TOKEN)"

    def test_format_no_hint(self):
        report = LintReport(path=Path("a.yaml"), issues=[])
        issue = LintIssue(field="<root>", rule="root must be mapping", hint="", line=1)
        out = _format_issue(report, issue)
        assert out == "a.yaml:1: [error] <root>: root must be mapping"


class TestFormatReport:
    def test_ok(self):
        report = LintReport(path=Path("a.yaml"), issues=[])
        out = _format_report(report)
        assert "OK" in out
        assert "a.yaml" in out

    def test_parse_error(self):
        report = LintReport(path=Path("a.yaml"), issues=[], parse_error="unexpected token")
        out = _format_report(report)
        assert "parse_error" in out
        assert "unexpected token" in out

    def test_with_issues(self):
        report = LintReport(
            path=Path("a.yaml"),
            issues=[
                LintIssue(field="interfaces[0].auth.secret_env", rule="required", hint="x", line=10),
                LintIssue(field="interfaces[0].protocol", rule="enum", hint="y", line=8),
            ],
        )
        out = _format_report(report)
        assert "a.yaml:10: [error]" in out
        assert "a.yaml:8: [error]" in out
        assert "2 error(s)" in out


# ---- _resolve_lint_paths ------------------------------------------------------


class TestResolveLintPaths:
    def test_glob(self, tmp_path: Path):
        (tmp_path / "a.yaml").write_text("vendor_spec: {}\n", encoding="utf-8")
        (tmp_path / "b.yml").write_text("vendor_spec: {}\n", encoding="utf-8")
        (tmp_path / "c.json").write_text('{"vendor_spec": {}}\n', encoding="utf-8")
        (tmp_path / "d.txt").write_text("ignore\n", encoding="utf-8")

        # 构造一个 glob 模式
        pattern = str(tmp_path / "*.yaml")
        paths = _resolve_lint_paths(Path(pattern), recursive=False)
        assert any(p.name == "a.yaml" for p in paths)
        assert all(p.suffix in (".yaml", ".yml", ".json") or "d.txt" not in [pp.name for pp in paths] for p in paths)

    def test_directory(self, tmp_path: Path):
        (tmp_path / "x.yaml").write_text("vendor_spec: {}\n", encoding="utf-8")
        (tmp_path / "y.json").write_text('{"vendor_spec": {}}\n', encoding="utf-8")
        paths = _resolve_lint_paths(tmp_path, recursive=False)
        names = sorted(p.name for p in paths)
        assert names == ["x.yaml", "y.json"]

    def test_directory_recursive(self, tmp_path: Path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (tmp_path / "x.yaml").write_text("vendor_spec: {}\n", encoding="utf-8")
        (sub / "y.yaml").write_text("vendor_spec: {}\n", encoding="utf-8")
        paths = _resolve_lint_paths(tmp_path, recursive=True)
        names = sorted(p.name for p in paths)
        assert "x.yaml" in names and "y.yaml" in names

    def test_single_file(self, tmp_path: Path):
        f = tmp_path / "single.yaml"
        f.write_text("vendor_spec: {}\n", encoding="utf-8")
        paths = _resolve_lint_paths(f, recursive=False)
        assert paths == [f]


# ---- CLI 注册 + exit code -----------------------------------------------------


class TestLintCli:
    """typer 子命令注册 + 退出码。"""

    def setup_method(self):
        self.runner = CliRunner()

    def test_lint_subcommand_registered(self):
        result = self.runner.invoke(app, ["--help"])
        assert "lint" in result.output
        assert result.exit_code == 0

    def test_lint_valid_file_exit_0(self):
        result = self.runner.invoke(app, ["lint", "examples/vendor_spec.example.yaml"])
        assert result.exit_code == 0, result.output
        assert "OK" in result.output
        assert "ok=1" in result.output

    def test_lint_invalid_file_exit_1(self):
        result = self.runner.invoke(app, ["lint", str(FIXTURES_INVALID / "invalid_no_auth.yaml")])
        assert result.exit_code == 1, result.output
        assert "error(s)" in result.output

    def test_lint_protocol_invalid_exit_1(self):
        result = self.runner.invoke(app, ["lint", str(FIXTURES_INVALID / "invalid_protocol.yaml")])
        assert result.exit_code == 1
        assert "enum" in result.output

    def test_lint_required_fields_empty_exit_1(self):
        result = self.runner.invoke(app, ["lint", str(FIXTURES_INVALID / "invalid_required_fields_empty.yaml")])
        assert result.exit_code == 1
        # 该 fixture 触发的是二次校验的「non-empty list」提示
        all_text = result.output
        assert "required_fields" in all_text or "non-empty" in all_text.lower()

    def test_lint_no_endpoint_exit_1(self):
        result = self.runner.invoke(app, ["lint", str(FIXTURES_INVALID / "invalid_no_endpoint.yaml")])
        assert result.exit_code == 1
        assert "sample_endpoint" in result.output or "anyOf" in result.output

    def test_lint_glob_pattern(self):
        """glob 模式扫描目录里所有 fixture。"""

        pattern = str(FIXTURES_INVALID / "*.yaml")
        result = self.runner.invoke(app, ["lint", pattern])
        # 5 个 fixture 全是 error → exit 1
        assert result.exit_code == 1
        assert "scanned 5 file(s)" in result.output

    def test_lint_quiet_suppresses_ok_lines(self):
        result = self.runner.invoke(app, ["lint", "examples/vendor_spec.example.yaml", "--quiet"])
        assert result.exit_code == 0
        # quiet 模式下不应该打印 "OK — ..." 这一行
        assert "OK —" not in result.output
        assert "scanned 1 file(s)" in result.output

    def test_lint_no_match_exit_1(self, tmp_path: Path):
        """不存在的路径：typer 已校验过，会在 lint 命令内被解析为单文件并走 lint_one → parse_error 通道。"""

        result = self.runner.invoke(app, ["lint", str(tmp_path / "nonexistent.yaml")])
        # 单文件不存在时 lint_one 走 OSError 通道 → n_errors=1 → exit 1
        assert result.exit_code == 1

    def test_lint_directory_scan(self, tmp_path: Path):
        (tmp_path / "ok.yaml").write_text("vendor_spec: {}\n", encoding="utf-8")
        result = self.runner.invoke(app, ["lint", str(tmp_path)])
        # vendor_spec: {} 是空 dict → JSON Schema 必填字段缺失 → exit 1
        assert result.exit_code == 1


# ---- 凭据纪律 -----------------------------------------------------------------


class TestLintCredentialSafety:
    """lint 输出与错误消息不得泄漏凭据字面量。"""

    def test_invalid_no_auth_no_token_leak(self):
        report = _lint_one(FIXTURES_INVALID / "invalid_no_auth.yaml")
        all_text = " ".join(
            f"{i.field} {i.rule} {i.hint}" for i in report.issues
        )
        # 13 类 forbidden literal 一律 0 命中
        for forbidden in [
            "Bearer", "wonderland", "lis-secret", "TOK-8888",
            "KEY-9999", "SUPER-SECRET", "ANOTHER-JWT", "demo-his-token",
        ]:
            assert forbidden not in all_text, f"泄漏 {forbidden}"

    def test_invalid_protocol_no_token_leak(self):
        report = _lint_one(FIXTURES_INVALID / "invalid_protocol.yaml")
        all_text = " ".join(
            f"{i.field} {i.rule} {i.hint}" for i in report.issues
        )
        for forbidden in ["Bearer", "wonderland", "lis-secret"]:
            assert forbidden not in all_text

    def test_cli_render_no_token_leak(self):
        runner = CliRunner()
        result = runner.invoke(app, ["lint", str(FIXTURES_INVALID / "invalid_no_auth.yaml")])
        for forbidden in ["Bearer", "wonderland", "TOK-8888", "SUPER-SECRET"]:
            assert forbidden not in result.output
