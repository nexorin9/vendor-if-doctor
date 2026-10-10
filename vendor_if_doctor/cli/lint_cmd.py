"""lint 子命令 — 校验 vendor_spec.yaml 并输出人类可读报告。

行为约定
--------

* 输入支持单文件 / 目录 / glob（复用 :func:`vendor_if_doctor.spec.loader._expand_paths`）。
* 复用 :func:`vendor_if_doctor.spec.loader.load_vendor_spec` 的完整校验链
  （JSON Schema + 二次校验 + pydantic），不重新发明规则。
* 输出格式::

      path/to/spec.yaml:LINE: [error] field: rule (hint)

  便于编辑器 / IDE 跳转到具体位置。
* 不修改任何文件，只读校验；exit code 0 = 全部 OK，1 = 有错误。
* **不发起任何网络请求**；仅做启动期校验，与 ``run`` 子命令的启动期段保持一致。
* 凭据纪律：错误消息只暴露**字段名**，永不含 token / Authorization 字面值。
"""

from __future__ import annotations

import glob as _glob
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Mapping, Optional, Sequence

import typer
import yaml

from vendor_if_doctor.spec.errors import SpecValidationError
from vendor_if_doctor.spec.loader import _parse_one

logger = logging.getLogger(__name__)


# --- 路径解析 + 行号定位 -------------------------------------------------------


# 把 SpecIssue.field 解析成可走的路径列表。
# SpecIssue.field 的格式例子（来自 spec/validate.py 的二次校验）：
#   - "vendor_spec.interfaces[0] (his.outpatient-register).auth.secret_env"
#   - "interfaces[0] (lis.report-push).required_fields"
#   - "<root>"
# 来自 JSON Schema 校验的 SpecIssue.field 例子：
#   - "vendor_spec/interfaces/0/required_fields/0"  （用 "/" 作分隔）
#   - "<root>"
_PATH_TOKEN_RE = re.compile(r"([A-Za-z_][\w-]*)|\[(\d+)\]")
_PARENS_RE = re.compile(r"\s*\([^)]*\)")


def _parse_field_to_path(field: str) -> List[object]:
    """把 ``SpecIssue.field`` 字符串解析为可走 YAML 节点树的路径列表。

    返回值样例：
    * ``"vendor_spec.interfaces[0] (his.outpatient-register).auth.secret_env"``
      → ``['vendor_spec', 'interfaces', 0, 'auth', 'secret_env']``
    * ``"vendor_spec/interfaces/0/required_fields/0"``（JSON Schema 用 / 分隔）
      → ``['vendor_spec', 'interfaces', 0, 'required_fields', 0]``
    * ``"<root>"`` 或无法解析 → ``[]``
    """

    if not field or field.startswith("<"):
        return []
    # JSON Schema 走 / 分隔
    if "/" in field:
        parts: List[object] = []
        for tok in field.split("/"):
            if not tok:
                continue
            if tok.isdigit():
                parts.append(int(tok))
            else:
                parts.append(tok)
        return parts
    # 二次校验走 . 分隔，但有 "(vendor_id.interface_id)" 这种括号段，先剥掉
    cleaned = _PARENS_RE.sub("", field)
    parts = []
    for m in _PATH_TOKEN_RE.finditer(cleaned):
        if m.group(1) is not None:
            parts.append(m.group(1))
        elif m.group(2) is not None:
            parts.append(int(m.group(2)))
    return parts


class _LineFinder:
    """在已用 :func:`yaml.compose` 解析的节点树上按路径找行号。

    返回**1-indexed** 行号（与编辑器 / IDE 一致）。
    """

    def __init__(self, root_node) -> None:
        self.root = root_node

    def find_line(self, path: Sequence[object]) -> Optional[int]:
        if not path:
            return None
        # 优先定位到「key 的行」；只有路径走完才退回到 value
        key_line = self._find_key_line(self.root, list(path))
        if key_line is not None:
            return key_line + 1
        value_line = self._find_value_line(self.root, list(path))
        if value_line is None:
            return None
        return value_line + 1

    def _find_key_line(self, node, path: List[object]) -> Optional[int]:
        """沿路径找「key 节点」的行号。"""

        if not path:
            return None
        head = path[0]
        rest = path[1:]
        if isinstance(node, yaml.MappingNode):
            for k, v in node.value:
                if isinstance(k, yaml.ScalarNode) and k.value == str(head):
                    if rest:
                        return self._find_key_line(v, rest)
                    # 路径结束：返回 key 节点的行号
                    return k.start_mark.line
            return None
        if isinstance(node, yaml.SequenceNode):
            try:
                idx = int(head)
            except (TypeError, ValueError):
                return None
            if 0 <= idx < len(node.value):
                return self._find_key_line(node.value[idx], rest)
            return None
        return None

    def _find_value_line(self, node, path: List[object]) -> Optional[int]:
        """沿路径走完整个路径并返回最后一个 value 节点的行号（兜底）。"""

        try:
            node = self._walk(node, list(path))
        except Exception:
            return None
        if node is None:
            return None
        return getattr(getattr(node, "start_mark", None), "line", None)

    def _walk(self, node, path: List[object]):
        if not path:
            return node
        head = path[0]
        rest = path[1:]
        if isinstance(node, yaml.MappingNode):
            for k, v in node.value:
                if isinstance(k, yaml.ScalarNode) and k.value == str(head):
                    return self._walk(v, rest)
            return None
        if isinstance(node, yaml.SequenceNode):
            try:
                idx = int(head)
            except (TypeError, ValueError):
                return None
            if 0 <= idx < len(node.value):
                return self._walk(node.value[idx], rest)
            return None
        return None


def _compose_root(path: Path):
    """读取 YAML 文本并用 :func:`yaml.compose` 解析（保留行号）。"""

    text = path.read_text(encoding="utf-8")
    return yaml.compose(text)


# --- 单文件 lint 报告 ---------------------------------------------------------


@dataclass
class LintIssue:
    """lint 单条问题记录。"""

    field: str
    rule: str
    hint: str
    line: Optional[int]


@dataclass
class LintReport:
    """lint 单文件报告。"""

    path: Path
    issues: List[LintIssue]
    parse_error: Optional[str] = None  # YAML 解析失败时的错误消息

    @property
    def ok(self) -> bool:
        return not self.issues and self.parse_error is None

    @property
    def n_errors(self) -> int:
        return len(self.issues)


def _lint_one(path: Path) -> LintReport:
    """对单个 vendor_spec 文件做完整 lint 校验。"""

    # 1) YAML 解析（保留行号用于定位）
    try:
        root_node = _compose_root(path)
    except yaml.YAMLError as e:
        msg = e.splitlines()[0][:200] if hasattr(e, "splitlines") else str(e)[:200]
        return LintReport(path=path, issues=[], parse_error=msg)
    except OSError as e:
        return LintReport(path=path, issues=[], parse_error=f"file read error: {e}")

    if root_node is None:
        return LintReport(path=path, issues=[], parse_error="empty document")

    # 2) 跑 raw 解析（覆盖 root mapping 检查 + JSON Schema + 二次校验）
    issues: List[SpecIssue] = []
    raw: Optional[Mapping] = None
    try:
        raw = _parse_one(path)
    except SpecValidationError as exc:
        issues = list(exc.issues)

    # 3) 再走一次 validate_raw 拿 JSON Schema 错误（pydantic 错误通常在 _parse_one 已捕获）
    if raw is not None:
        try:
            from vendor_if_doctor.spec.validate import validate_raw

            validate_raw(raw, source=str(path))
        except SpecValidationError as exc:
            seen = {(i.field, i.rule) for i in issues}
            for i in exc.issues:
                if (i.field, i.rule) not in seen:
                    issues.append(i)
                    seen.add((i.field, i.rule))

    # 4) 走 pydantic 二次校验（捕获 _parse_one 漏掉的模型级错误）
    if raw is not None:
        try:
            from vendor_if_doctor.spec.schema import VendorSpec

            VendorSpec.model_validate(raw.get("vendor_spec", raw))
        except Exception as exc:  # pydantic.ValidationError 等
            from vendor_if_doctor.spec.errors import SpecIssue

            attr_issues = getattr(exc, "errors", None)
            if callable(attr_issues):
                seen = {(i.field, i.rule) for i in issues}
                for err in attr_issues():
                    field = ".".join(str(p) for p in err.get("loc", ())) or "<root>"
                    rule = err.get("type", "unknown")
                    hint = (err.get("msg", "") or "")[:200]
                    sig = (field, rule)
                    if sig not in seen:
                        issues.append(SpecIssue(field=field, rule=rule, hint=hint))
                        seen.add(sig)

    # 5) 定位行号
    finder = _LineFinder(root_node)
    lint_issues: List[LintIssue] = []
    for i in issues:
        path_tokens = _parse_field_to_path(i.field)
        line = finder.find_line(path_tokens)
        lint_issues.append(
            LintIssue(
                field=i.field,
                rule=i.rule,
                hint=i.hint,
                line=line,
            )
        )

    return LintReport(path=path, issues=lint_issues, parse_error=None)


# --- 报告渲染 ------------------------------------------------------------------


def _format_issue(report: LintReport, issue: LintIssue) -> str:
    """渲染单条 lint 问题为 ``path:line: [error] field: rule (hint)`` 形式。"""

    line_part = f":{issue.line}" if issue.line is not None else ":?"
    field = issue.field or "<root>"
    rule = issue.rule or "unknown"
    hint = issue.hint or ""
    if hint:
        return f"{report.path}{line_part}: [error] {field}: {rule} ({hint})"
    return f"{report.path}{line_part}: [error] {field}: {rule}"


def _format_report(report: LintReport) -> str:
    """渲染单文件 lint 报告。"""

    if report.parse_error:
        return f"{report.path}:1: [error] <yaml>: parse_error ({report.parse_error})"
    if report.ok:
        return f"OK — {report.path} 校验通过（启动期 fail-fast 已放过，未发起任何网络请求）"
    lines: List[str] = []
    for it in report.issues:
        lines.append(_format_issue(report, it))
    lines.append(f"{report.path}: summary: {report.n_errors} error(s)")
    return "\n".join(lines)


# --- 子命令 --------------------------------------------------------------------


def lint_cmd(
    path: Path = typer.Argument(
        ...,
        help="vendor_spec.yaml 路径（支持单文件 / 目录 / glob，如 'examples/*.yaml'）",
        exists=False,
    ),
    recursive: bool = typer.Option(
        False,
        "--recursive",
        "-r",
        help="目录递归扫描 .yaml/.yml/.json（默认只扫顶层）",
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="成功路径只输出汇总；错误路径仍逐条打印",
    ),
) -> None:
    """校验 vendor_spec.yaml，输出人类可读报告；非零退出码当有错误。"""

    paths = _resolve_lint_paths(path, recursive=recursive)
    if not paths:
        typer.echo(f"lint: no yaml/json files matched under {path}", err=True)
        sys.exit(2)

    reports: List[LintReport] = [_lint_one(p) for p in paths]
    n_errors = sum(r.n_errors for r in reports)
    n_parse_errors = sum(1 for r in reports if r.parse_error)

    # 输出
    for r in reports:
        text = _format_report(r)
        if r.ok:
            if not quiet:
                typer.echo(text)
        else:
            typer.echo(text)

    n_ok = sum(1 for r in reports if r.ok)
    summary = (
        f"lint: scanned {len(reports)} file(s); "
        f"ok={n_ok} errors={n_errors} parse_errors={n_parse_errors}"
    )
    typer.echo(summary)

    if n_errors > 0 or n_parse_errors > 0:
        sys.exit(1)


def _resolve_lint_paths(path: Path, *, recursive: bool) -> List[Path]:
    """把 lint 入参解析为具体文件列表。"""

    s = str(path)
    # glob 通配
    if any(ch in s for ch in "*?["):
        return sorted({Path(p) for p in _glob.glob(s)})
    if path.is_dir():
        files: List[Path] = []
        patterns = ("*.yaml", "*.yml", "*.json")
        for ext in patterns:
            files.extend(path.glob(ext))
            if recursive:
                files.extend(path.rglob(ext))
        return sorted(set(files))
    return [path]


__all__ = ["lint_cmd", "LintIssue", "LintReport"]
