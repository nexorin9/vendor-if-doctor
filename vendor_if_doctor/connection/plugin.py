"""插件加载器 — 启动期把 :data:`VENDOR_DOCTOR_PLUGINS` 列出的模块逐个 ``import``，
让插件模块在自己模块体里调用 :func:`register_protocol` 注册到 factory。

设计动机
--------

本项目内置 4 个协议（HTTP / HL7v2 / WebService / SQL）。但医院信息科现场常会临时
接入只走特殊通道的厂商（例如老 HIS 走 MLLPv3、DICOM 转发走 TCP 自定义协议、
医保前置机走 WebSocket）。为避免每来一家厂商就改主项目，task 33 提供一个**纯加载器**
插件机制：

* **不**预设插件注册入口（没有 setuptools entry_points、importlib.metadata 扫描）；
  避免在测试环境或离线环境下扫不到 vendor 包路径导致悄悄失败。
* 用户加载标准 **只一个**：``VENDOR_DOCTOR_PLUGINS`` 环境变量，逗号分隔 Python 模块
  dotted name。CLI 启动期、``build_connection`` 调用前、``run`` 命令加载 spec 前
  都会触发一次。
* 插件模块自行决定注册哪些 protocol（也负责把自己注册到
  :mod:`vendor_if_doctor.spec.json_schema` 的协议列表，否则 :func:`load_vendor_spec`
  校验 YAML 时会因为 ``protocol`` 不在枚举里失败）。

公开 API
--------

* :func:`load_plugins` — 启动期入口；读 env，逐个 import；
* :func:`is_registered_protocol` — 查询某个 protocol 名是否已被某插件注册（用于
  JSON Schema 校验放行）；
* :func:`list_registered_protocols` — 列出全部已注册的插件协议；
* :func:`register_protocol_via_plugin` — 插件注册入口：调用 factory 的
  :func:`register_protocol` **并且** 把协议名追加到 JSON Schema 的允许列表里；
* :func:`reset_plugins` — 清空插件注册（**仅供测试**；不可在生产代码中调用）。

凭据纪律
--------

* 插件模块**不**读取任何环境变量值，只跑 import；插件注册协议所需的凭据，仍由协议
  适配器在 step 执行时按既有约定从 env 读。
* 插件机制**不**绕过 redact：插件写出的 evidence / 走得通的 handover.md / tickets.jsonl
  仍走 :mod:`vendor_if_doctor.redact` 兜底脱敏。

测试覆盖
--------

* :mod:`tests.unit.test_plugin` — env 触发加载、协议路由、清理、占位不阻塞、
  凭据纪律、JSON Schema 同步扩展。
"""
from __future__ import annotations

import importlib
import logging
import os
from threading import Lock
from typing import Iterable, List, Optional, Sequence

from vendor_if_doctor.connection.factory import (
    register_protocol as _factory_register_protocol,
    unregister_protocol as _factory_unregister_protocol,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 全局状态（线程安全；只在 CLI 启动期 / 测试夹具里被改写）
# ---------------------------------------------------------------------------

_LOCK = Lock()
_LOADED_MODULES: List[str] = []
_REGISTERED_PLUGIN_PROTOCOLS: List[str] = []
"""已通过插件机制注册到 factory 的 protocol 列表（小写归一化后）。

用于 :func:`is_registered_protocol` / :func:`list_registered_protocols`，也供
:mod:`vendor_if_doctor.spec.json_schema` 在生成 schema 时把插件协议纳入 ``protocol`` 枚举。
"""


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------


def register_protocol_via_plugin(protocol: str, conn_cls) -> None:
    """插件注册入口 — 注册到 factory 并把协议名暴露给 JSON Schema 校验。

    与 :func:`vendor_if_doctor.connection.factory.register_protocol` 的差异在于：
    **同时** 把 ``protocol`` 名追加到 :mod:`vendor_if_doctor.spec.json_schema`
    的 ``_PROTOCOLS`` 列表 + :class:`vendor_if_doctor.spec.schema.Protocol` 枚举，
    使后续 ``load_vendor_spec`` 启动期校验（JSON Schema + pydantic 两层）能放过插件协议。

    Parameters
    ----------
    protocol : str
        协议名（不区分大小写）；空字符串或非字符串抛 ``ValueError``。
    conn_cls : type
        :class:`BaseConnection` 子类；非子类抛 ``TypeError``（由 factory 转发）。
    """

    # 1) 走 factory 注册（含 BaseConnection 子类校验、空白 trim、归一化）
    _factory_register_protocol(protocol, conn_cls)
    key = (protocol or "").strip().lower()
    if not key:
        raise ValueError("protocol name 不能为空")
    # 2) 同步到 JSON Schema 协议列表 + pydantic Protocol 枚举
    with _LOCK:
        if key not in _REGISTERED_PLUGIN_PROTOCOLS:
            _REGISTERED_PLUGIN_PROTOCOLS.append(key)
            _maybe_extend_json_schema([key])
            _maybe_extend_protocol_enum([key])


def list_registered_protocols() -> List[str]:
    """返回已被插件注册的协议名列表（按注册顺序，已去重）。"""

    with _LOCK:
        return list(_REGISTERED_PLUGIN_PROTOCOLS)


def is_registered_protocol(name: str) -> bool:
    """查询 ``name`` 是否已被插件注册（大小写不敏感）。

    用于 :mod:`vendor_if_doctor.spec.json_schema` 在生成 schema 时决定是否纳入
    ``protocol`` 枚举；也供 :func:`load_vendor_spec` 的二次校验使用。
    """

    if not name:
        return False
    key = name.strip().lower()
    with _LOCK:
        return key in _REGISTERED_PLUGIN_PROTOCOLS


def list_loaded_modules() -> List[str]:
    """返回已被加载的插件模块 dotted name 列表（按加载顺序，已去重）。"""

    with _LOCK:
        return list(_LOADED_MODULES)


def load_plugins(env: Optional[dict] = None, *, skip_if_loaded: bool = True) -> List[str]:
    """启动期插件加载入口。

    读取环境变量 ``VENDOR_DOCTOR_PLUGINS``（逗号分隔 Python dotted name 列表），
    逐个 ``importlib.import_module``；模块体里的 ``register_protocol_via_plugin``
    调用会同步生效到 factory + JSON Schema。

    Parameters
    ----------
    env : dict, optional
        测试可显式传入环境变量字典；默认读 :data:`os.environ`。
    skip_if_loaded : bool
        已加载过的模块名是否跳过（避免重复注册造成覆盖）。默认 True；
        测试场景设为 False 可强制重新 import。

    Returns
    -------
    list[str]
        实际成功 import 的模块 dotted name 列表（按入参顺序，已去重）。

    Notes
    -----
    * 单个模块 import 失败时，**不**静默继续；抛 :class:`ImportError` 让上层决定
      是降级（CI）还是终止（生产）。
    * 不读取任何环境变量值；只读模块名。
    """

    env_map = env if env is not None else dict(os.environ)
    plugins_csv = env_map.get("VENDOR_DOCTOR_PLUGINS", "") or ""
    modules: List[str] = []
    for raw in (s.strip() for s in plugins_csv.split(",")):
        if not raw:
            continue
        modules.append(raw)

    loaded: List[str] = []
    for name in modules:
        with _LOCK:
            if skip_if_loaded and name in _LOADED_MODULES:
                # 已加载过：仍然计入返回值（幂等语义）
                loaded.append(name)
                continue
        importlib.import_module(name)
        with _LOCK:
            if name not in _LOADED_MODULES:
                _LOADED_MODULES.append(name)
            loaded.append(name)
        logger.info("vendor-if-doctor plugin loaded: %s", name)
    return loaded


def reset_plugins() -> None:
    """清空插件全局状态（**仅供测试**）。

    * 把 factory._REGISTRY 里已注册的插件协议 ``unregister`` 掉；
    * 清空 :data:`_REGISTERED_PLUGIN_PROTOCOLS`；
    * 把 JSON Schema 的协议列表裁回内置 4 个；
    * 把 pydantic Protocol 枚举裁回内置 4 个；
    * 清空 :data:`_LOADED_MODULES`。
    """

    from vendor_if_doctor.spec.json_schema import _set_extra_protocols
    from vendor_if_doctor.spec.schema import _set_extra_protocol_enum

    with _LOCK:
        for proto in list(_REGISTERED_PLUGIN_PROTOCOLS):
            _factory_unregister_protocol(proto)
        _REGISTERED_PLUGIN_PROTOCOLS.clear()
        _set_extra_protocols([])
        _set_extra_protocol_enum([])
        _LOADED_MODULES.clear()


# ---------------------------------------------------------------------------
# JSON Schema 协议列表同步
# ---------------------------------------------------------------------------


def _maybe_extend_json_schema(extra: Sequence[str]) -> None:
    """把 ``extra`` 里的协议追加到 JSON Schema 的 ``_PROTOCOLS`` 列表。

    内部 helper；不直接对外暴露。
    """

    from vendor_if_doctor.spec.json_schema import _add_extra_protocols

    _add_extra_protocols(list(extra))


def _maybe_extend_protocol_enum(extra: Sequence[str]) -> None:
    """把 ``extra`` 里的协议追加到 pydantic 的 :class:`Protocol` 枚举。

    内部 helper；不直接对外暴露。
    """

    from vendor_if_doctor.spec.schema import _add_extra_protocol_enum_members

    _add_extra_protocol_enum_members(list(extra))


__all__ = [
    "is_registered_protocol",
    "list_loaded_modules",
    "list_registered_protocols",
    "load_plugins",
    "register_protocol_via_plugin",
    "reset_plugins",
]