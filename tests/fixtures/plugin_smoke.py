"""测试用插件 fixture — task 33 接入。

模块体里 :func:`register_protocol_via_plugin` 注册一个占位协议 ``mllp_v3``，
让 :func:`load_vendor_spec` 启动期校验放过该 ``protocol`` 值，
``build_connection`` 走 :class:`MLLPv3Connection` 占位实现。

用法
----

``VENDOR_DOCTOR_PLUGINS=tests.fixtures.plugin_smoke python -m vendor_if_doctor ...``

凭据纪律
--------

* 模块体**不**读取任何环境变量值；
* :class:`MLLPv3Connection` 的 steps 输出 ``failure_class='not_implemented'`` 的
  skip evidence，summary 不含任何凭据字面量；
* test_connection / build_connection 调用方拿到的 evidence 仍走
  :mod:`vendor_if_doctor.redact` 兜底脱敏。
"""

from __future__ import annotations

from typing import List, Optional

from vendor_if_doctor.connection.base import BaseConnection
from vendor_if_doctor.connection.evidence import Evidence
from vendor_if_doctor.connection.plugin import register_protocol_via_plugin
from vendor_if_doctor.connection.step import Step, StepResult, make_step
from vendor_if_doctor.spec.schema import VendorInterface


class MLLPv3Connection(BaseConnection):
    """占位 ``mllp_v3`` 协议 — 演示插件接入。

    5 步流水线（preflight / auth / list / sample / assert）每步都返回
    ``status='skip'`` + ``failure_class='not_implemented'`` 的 evidence，
    与 :class:`NotImplementedConnection` 等价但**走 plugin 路径**，方便在测试里
    区分"内置未实现"和"插件占位"。
    """

    def __init__(self, spec: VendorInterface, env: Optional[dict] = None) -> None:
        super().__init__(spec, env)

    def _get_client(self):  # pragma: no cover - 永不调用
        return None

    def steps(self) -> List[Step]:
        def _skip_step(step_name: str) -> StepResult:
            proto_str = (
                self.spec.protocol.value
                if hasattr(self.spec.protocol, "value")
                else str(self.spec.protocol)
            )
            ev = Evidence(
                summary=(
                    f"{self.spec.fqn} protocol={proto_str} "
                    "插件占位（mllp_v3）— Phase 5 之后扩展为真实 MLLP v3 帧"
                ),
                command="",
                latency_ms=0,
                status="skip",
                failure_class="not_implemented",
                step_name=step_name,
                vendor_id=self.spec.vendor_id,
                interface_id=self.spec.interface_id,
                meta={
                    "protocol": proto_str,
                    "plugin": "tests.fixtures.plugin_smoke",
                },
            )
            return StepResult(success=True, evidence=ev)

        return [
            make_step("preflight", lambda: _skip_step("preflight")),
            make_step("auth", lambda: _skip_step("auth")),
            make_step("list", lambda: _skip_step("list")),
            make_step("sample", lambda: _skip_step("sample")),
            make_step("assert", lambda: _skip_step("assert")),
        ]


# 模块体自注册 — import 时即生效。
register_protocol_via_plugin("mllp_v3", MLLPv3Connection)


__all__ = ["MLLPv3Connection"]