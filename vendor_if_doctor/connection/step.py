"""Step 协议 — 单步巡检动作的抽象协议。

* :class:`Step` — 步骤协议（含 name / run / expect）；
* :class:`StepResult` — 单步执行结果（success / evidence / error）；
* :data:`STEP_NAMES` — 默认 5 步流水线（preflight / auth / list / sample / assert）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Protocol, runtime_checkable

from vendor_if_doctor.connection.evidence import Evidence

STEP_NAMES = ("preflight", "auth", "list", "sample", "assert")
"""默认 5 步流水线名称。"""


@dataclass
class StepResult:
    """单步执行结果。"""

    success: bool
    evidence: Evidence
    error: Optional[BaseException] = None
    """原始异常（仅 debug 用；序列化前会被脱敏）。"""


@runtime_checkable
class Step(Protocol):
    """单步巡检动作协议。

    调用方实现 ``run(ctx) -> StepResult``；可选 ``expect(evidence) -> StepResult``
    在 run 之后做语义断言（区别于跑动作本身）。
    """

    name: str

    def run(self) -> StepResult:  # pragma: no cover - Protocol
        ...

    def expect(self, evidence: Evidence) -> StepResult:  # pragma: no cover - Protocol
        ...


# 简单函数式步骤工厂 ----------------------------------------------------------


def make_step(
    name: str,
    run_fn: Callable[[], StepResult],
    expect_fn: Optional[Callable[[Evidence], StepResult]] = None,
) -> Step:
    """函数式 :class:`Step` 工厂。

    用于 BaseConnection 子类内部构造流水线步骤。
    """

    class _FnStep:
        def __init__(self) -> None:
            self.name = name

        def run(self) -> StepResult:
            return run_fn()

        def expect(self, evidence: Evidence) -> StepResult:
            if expect_fn is None:
                return StepResult(success=evidence.status == "pass", evidence=evidence)
            return expect_fn(evidence)

    return _FnStep()


__all__ = ["STEP_NAMES", "Step", "StepResult", "make_step"]