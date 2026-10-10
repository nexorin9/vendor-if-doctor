"""factory — 由 :class:`VendorInterface` 构造 :class:`BaseConnection` 子类。

Phase 4 task 5 实现
-------------------

* :func:`build_connection` — 按 ``spec.protocol`` 路由到对应 ``BaseConnection`` 子类；
  HTTP 路由到 :class:`vendor_if_doctor.adapters.http.HTTPConnection`；
  其他协议暂时落到 :class:`NotImplementedConnection`，输出 ``status='skip'`` +
  ``failure_class='not_implemented'`` 的单条 evidence，**不**误判为 healthy。
* :func:`register_protocol` — 给后续 task 7 / 8 / 33（插件扩展）留的 hook；
* :func:`list_protocols` — 调试用，列出已注册协议。

凭据纪律
--------

* 构造过程中**不**读取环境变量；凭据解析交给具体协议子类的 step 执行。
* factory 不发起任何网络请求。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Type

from vendor_if_doctor.connection.base import BaseConnection
from vendor_if_doctor.connection.evidence import Evidence
from vendor_if_doctor.connection.step import Step, StepResult, make_step
from vendor_if_doctor.spec.schema import VendorInterface


# 已注册的 protocol → BaseConnection 子类映射表
_REGISTRY: Dict[str, Type[BaseConnection]] = {}


def register_protocol(protocol: str, conn_cls: Type[BaseConnection]) -> None:
    """把 ``protocol`` 名字（不区分大小写）映射到 ``BaseConnection`` 子类。

    后续 task 33（插件）会通过环境变量 ``VENDOR_DOCTOR_PLUGINS`` 触发自动注册。
    已注册的同名 protocol 会被覆盖（插件应当谨慎）。
    """

    key = (protocol or "").strip().lower()
    if not key:
        raise ValueError("protocol name 不能为空")
    if not isinstance(conn_cls, type) or not issubclass(conn_cls, BaseConnection):
        raise TypeError(f"{conn_cls!r} 必须是 BaseConnection 子类")
    _REGISTRY[key] = conn_cls


def unregister_protocol(protocol: str) -> None:
    """移除已注册的 protocol（测试与插件清理用）。"""

    key = (protocol or "").strip().lower()
    _REGISTRY.pop(key, None)


def list_protocols() -> List[str]:
    """返回已注册的全部 protocol 名字（按字典序）。"""

    return sorted(_REGISTRY.keys())


class NotImplementedConnection(BaseConnection):
    """协议尚未实现的占位 :class:`BaseConnection`。

    单步骤流水线（preflight）输出 ``status='skip'`` +
    ``failure_class='not_implemented'`` 的 evidence，**不**误判 healthy，
    同时也**不**触发 short-circuit（避免与真实失败混淆）。
    """

    def __init__(self, spec: VendorInterface, env: Optional[dict] = None) -> None:
        super().__init__(spec, env)

    def _get_client(self):  # pragma: no cover - 永不调用
        return None

    def steps(self) -> List[Step]:
        def _skip_step() -> StepResult:
            proto_str = (
                self.spec.protocol.value
                if hasattr(self.spec.protocol, "value")
                else str(self.spec.protocol)
            )
            ev = Evidence(
                summary=(
                    f"{self.spec.fqn} protocol={proto_str} "
                    f"尚未在本轮实现（task 7/8/33 接入）"
                ),
                command="",
                latency_ms=0,
                status="skip",
                failure_class="not_implemented",
                step_name="preflight",
                vendor_id=self.spec.vendor_id,
                interface_id=self.spec.interface_id,
                meta={"protocol": proto_str},
            )
            # 故意 success=True 让 short-circuit 不触发（skip ≠ fail）
            return StepResult(success=True, evidence=ev)

        return [make_step("preflight", _skip_step)]


def build_connection(
    spec: VendorInterface,
    *,
    env: Optional[dict] = None,
) -> BaseConnection:
    """按 :attr:`VendorInterface.protocol` 路由构造 :class:`BaseConnection`。

    Parameters
    ----------
    spec : VendorInterface
        单条 vendor 接口规格；
    env : dict, optional
        环境变量字典（默认读 ``os.environ``）。

    Returns
    -------
    BaseConnection
        已注册协议 → 对应子类实例；未注册协议 → :class:`NotImplementedConnection` 占位。
    """

    proto_value = spec.protocol.value if hasattr(spec.protocol, "value") else str(spec.protocol)
    key = (proto_value or "").strip().lower()
    cls = _REGISTRY.get(key)
    if cls is None:
        return NotImplementedConnection(spec, env=env)
    return cls(spec, env=env)


__all__ = [
    "NotImplementedConnection",
    "build_connection",
    "list_protocols",
    "register_protocol",
    "unregister_protocol",
]
