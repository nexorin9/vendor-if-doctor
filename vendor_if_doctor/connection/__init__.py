"""vendor-if-doctor 连接（connection）模块。

对外导出：

* :class:`BaseConnection` — 协议无关抽象基类，子类须实现 :meth:`_get_client` 与 :meth:`steps`；
* :class:`Step` / :class:`StepResult` — 步骤协议 + 单步执行结果；
* :class:`Evidence` — 巡检证据（含 ``status`` / ``failure_class`` / ``evidence_hash`` 等）；
* :func:`preflight` — TCP 探活；
* :func:`test_one` — 单步执行入口（Phase 3 中期扩展为 :func:`test_connection_steps`）；
* :func:`test_connection_steps` / :func:`test_connection` — 多步调度器（task 5）；
* :func:`build_connection` / :class:`NotImplementedConnection` /
  :func:`register_protocol` / :func:`list_protocols` — factory 路由（task 5）；
* :class:`FailureClass` / :func:`classify_exception` — 失败语义分类（task 4）；
* :func:`assert_http_status_in` / :func:`assert_jsonpath_present` /
  :func:`assert_jsonpath_equals` / :func:`assert_hl7_msa1` / :func:`resolve_jsonpath`
  — 语义断言（task 4）。

本模块 **不** 直接处理协议细节；协议相关代码放在 :mod:`vendor_if_doctor.adapters`。

凭据纪律：所有 Evidence 字段在收集过程中由 :mod:`vendor_if_doctor.redact` 兜底过滤，
**绝不**写入 ``Authorization`` / ``X-API-Key`` / token / password 等凭据字段值。
"""

from __future__ import annotations

import logging

from vendor_if_doctor.connection.base import BaseConnection
from vendor_if_doctor.connection.error_pack import (
    FailureClass,
    SLOW_THRESHOLD_MS_DEFAULT,
    all_failure_classes,
    classify_exception,
    is_degraded_slow,
    is_failure_class,
    slow_threshold_ms,
)
from vendor_if_doctor.connection.evidence import Evidence
from vendor_if_doctor.connection.evidence_schema import (
    EVIDENCE_JSON_SCHEMA,
    get_evidence_json_schema,
    load_packaged_evidence_schema_text,
    schema_as_json_string,
)
from vendor_if_doctor.connection.factory import (
    NotImplementedConnection,
    _REGISTRY,
    build_connection,
    list_protocols,
    register_protocol,
    unregister_protocol,
)
from vendor_if_doctor.connection.matchers import (
    MatcherResult,
    assert_hl7_msa1,
    assert_http_status_in,
    assert_jsonpath_equals,
    assert_jsonpath_present,
    resolve_jsonpath,
)
from vendor_if_doctor.connection.network import (
    PreflightError,
    PreflightOK,
    preflight,
)
from vendor_if_doctor.connection.runner import test_connection, test_connection_steps, test_one
from vendor_if_doctor.connection.severity import (
    Severity,
    all_severities,
    all_severity_mappings,
    is_critical,
    is_warning,
    priority_of,
    severity_of,
    severity_to_priority,
)
from vendor_if_doctor.connection.step import STEP_NAMES, Step, StepResult
from vendor_if_doctor.connection.suggestions import (
    PLACEHOLDER_KEYS,
    PLACEHOLDER_KEYS_OPTIONAL,
    PLACEHOLDER_KEYS_REQUIRED,
    auth_types_for_auth_failed,
    list_supported_failure_classes,
    placeholder_keys,
    suggest_actions,
    suggested_actions_for,
)

__all__ = [
    "BaseConnection",
    "Evidence",
    "FailureClass",
    "MatcherResult",
    "NotImplementedConnection",
    "PLACEHOLDER_KEYS",
    "PLACEHOLDER_KEYS_OPTIONAL",
    "PLACEHOLDER_KEYS_REQUIRED",
    "PreflightError",
    "PreflightOK",
    "Severity",
    "STEP_NAMES",
    "Step",
    "StepResult",
    "all_failure_classes",
    "all_severities",
    "all_severity_mappings",
    "assert_hl7_msa1",
    "assert_http_status_in",
    "assert_jsonpath_equals",
    "assert_jsonpath_present",
    "auth_types_for_auth_failed",
    "build_connection",
    "EVIDENCE_JSON_SCHEMA",
    "get_evidence_json_schema",
    "load_packaged_evidence_schema_text",
    "schema_as_json_string",
    "classify_exception",
    "is_critical",
    "is_degraded_slow",
    "is_failure_class",
    "is_warning",
    "list_protocols",
    "list_supported_failure_classes",
    "placeholder_keys",
    "preflight",
    "priority_of",
    "register_protocol",
    "resolve_jsonpath",
    "SLOW_THRESHOLD_MS_DEFAULT",
    "severity_of",
    "severity_to_priority",
    "slow_threshold_ms",
    "suggest_actions",
    "suggested_actions_for",
    "test_connection",
    "test_connection_steps",
    "test_one",
    "unregister_protocol",
]

# async_runner 是 task 20 新增模块；导入即触发协议注册检查
from vendor_if_doctor.connection.async_runner import (  # noqa: E402
    run_all_async,
    run_async,
    run_async_pool,
)

__all__ += [
    "run_all_async",
    "run_async",
    "run_async_pool",
]


# 插件加载器（task 33）— 暴露 ``load_plugins`` / ``register_protocol_via_plugin``
# 等公开 API；启动期调用 ``load_plugins()`` 把 ``VENDOR_DOCTOR_PLUGINS`` env 指向
# 的模块逐个 import，让它们在模块体里完成 ``register_protocol`` 调用。
from vendor_if_doctor.connection.plugin import (  # noqa: E402
    is_registered_protocol,
    list_loaded_modules,
    list_registered_protocols,
    load_plugins,
    register_protocol_via_plugin,
    reset_plugins,
)

__all__ += [
    "is_registered_protocol",
    "list_loaded_modules",
    "list_registered_protocols",
    "load_plugins",
    "register_protocol_via_plugin",
    "reset_plugins",
]


# --- 启动期协议注册 -----------------------------------------------------------
# 当 connection 包被首次导入时，强制加载已实现协议的子类模块，让它们把自己注册
# 到 factory._REGISTRY。这样从任何路径（factory / runner / test_connection）进来
# 都能直接拿到 HTTPConnection 等实例，避免循环依赖。
def _bootstrap_known_protocols() -> None:  # pragma: no cover - simple wiring
    if (
        "http" in _REGISTRY
        and "hl7v2" in _REGISTRY
        and "webservice" in _REGISTRY
        and "sql" in _REGISTRY
    ):
        return
    # 触发 adapters 子模块加载；模块尾部 _register_self() 会调用 register_protocol
    from vendor_if_doctor.adapters import http as _http_mod  # noqa: F401
    from vendor_if_doctor.adapters import hl7v2 as _hl7_mod  # noqa: F401
    from vendor_if_doctor.adapters import webservice as _ws_mod  # noqa: F401
    from vendor_if_doctor.adapters import sql as _sql_mod  # noqa: F401


_bootstrap_known_protocols()


# --- 启动期插件加载 -----------------------------------------------------------
# 启动期自动触发一次插件加载：读 ``VENDOR_DOCTOR_PLUGINS`` env 变量，逗号分隔模块
# 名列表，逐个 import。失败抛 ImportError 让上层决定如何处理。
_logger = logging.getLogger("vendor_if_doctor.connection")
try:
    load_plugins()
except Exception as _exc:  # pragma: no cover - import errors propagate
    _logger.warning("plugin loader failed during startup: %s", _exc)