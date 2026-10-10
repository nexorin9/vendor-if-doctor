"""vendor-if-doctor 规格校验错误类型。

启动期（加载 vendor_spec.yaml / JSON / 目录时）发现的任何问题都必须包装为
:class:`SpecValidationError`，并写明「字段名 + 规则名」，**不**进入
``test_connection``，**不**发起任何网络请求。

凭据值（Authorization header / token / password）**永远不**进入错误消息；
错误消息只暴露字段名 + 违反规则 + 修复建议。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence


@dataclass
class SpecIssue:
    """单条校验失败信息。"""

    field: str
    rule: str
    hint: str

    def __str__(self) -> str:  # pragma: no cover - 简单字符串
        return f"{self.field}: {self.rule}（{self.hint}）"


class SpecValidationError(ValueError):
    """启动期 vendor spec 校验失败。

    行为约定：
    * 异常消息只暴露字段名 / 规则 / 修复建议，**不含任何凭据值**。
    * :attr:`issues` 列表给出每一条具体问题，方便 ``lint`` 子命令逐条渲染。
    """

    def __init__(self, issues: Sequence[SpecIssue] | SpecIssue, *, source: str | None = None):
        if isinstance(issues, SpecIssue):
            issues = [issues]
        self.issues: List[SpecIssue] = list(issues)
        self.source = source
        head = f"vendor_spec 校验失败（{source}）" if source else "vendor_spec 校验失败"
        details = "; ".join(str(i) for i in self.issues)
        super().__init__(f"{head}：{details}" if details else head)

    def first_issue(self) -> SpecIssue | None:
        return self.issues[0] if self.issues else None


__all__ = ["SpecIssue", "SpecValidationError"]
