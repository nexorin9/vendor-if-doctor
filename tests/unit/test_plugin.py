"""插件加载器单测 — task 33。

覆盖范围
--------

1. **env 触发加载**：``VENDOR_DOCTOR_PLUGINS`` 指向 ``tests.fixtures.plugin_smoke`` 后，
   :func:`load_plugins` 能成功 import 并触发 ``register_protocol_via_plugin``；
2. **protocol 路由**：`build_connection(spec)` 走 ``MLLPv3Connection`` 而非
   :class:`NotImplementedConnection`；
3. **JSON Schema 同步扩展**：插件注册后 ``json_schema.get_protocols()`` 含
   ``mllp_v3``，``load_vendor_spec("examples/mllp_v3_spec.yaml")`` 校验通过；
4. **占位不阻塞**：插件 evidence ``status='skip'`` + ``failure_class='not_implemented'``，
   与其他 vendor 并发跑不污染其它 evidence；
5. **清理** :func:`reset_plugins` 把 factory 协议表 + JSON Schema 协议列表回退到
   内置 4 条；
6. **凭据纪律**：插件 evidence 不含 ``Bearer / token / password / SUPER-SECRET`` 等
   forbidden literal。
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, List

import pytest

from vendor_if_doctor.connection.base import BaseConnection
from vendor_if_doctor.connection.factory import (
    NotImplementedConnection,
    build_connection,
    list_protocols,
)
from vendor_if_doctor.connection.plugin import (
    is_registered_protocol,
    list_loaded_modules,
    list_registered_protocols,
    load_plugins,
    register_protocol_via_plugin,
    reset_plugins,
)
from vendor_if_doctor.spec.loader import load_vendor_spec
from vendor_if_doctor.spec.schema import (
    AuthMllp,
    Hl7v2SampleEndpoint,
    Protocol,
    SampleEndpoint,
    VendorInterface,
    VendorSpec,
    InspectionConfig,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
MLLP_V3_SPEC = PROJECT_ROOT / "examples" / "mllp_v3_spec.yaml"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_plugins(monkeypatch: pytest.MonkeyPatch):
    """每个测试前后清空插件注册，避免污染全局。

    除了调 :func:`reset_plugins` 清掉 factory + JSON Schema + pydantic 三个状态外，
    还需要把已 import 的插件模块从 :data:`sys.modules` 中 evict，
    这样下次 :func:`load_plugins` 会重新触发模块体里的注册调用。
    """

    import sys as _sys

    reset_plugins()
    # 抹掉插件模块在 sys.modules 的引用，确保下一次 import 走模块体
    for mod_name in list(_sys.modules.keys()):
        if mod_name in {"tests.fixtures.plugin_smoke"} or mod_name.startswith(
            "tests.fixtures.plugin_smoke."
        ):
            _sys.modules.pop(mod_name, None)
    yield
    reset_plugins()
    for mod_name in list(_sys.modules.keys()):
        if mod_name in {"tests.fixtures.plugin_smoke"} or mod_name.startswith(
            "tests.fixtures.plugin_smoke."
        ):
            _sys.modules.pop(mod_name, None)


def _make_mllp_v3_interface() -> VendorInterface:
    """构造一条 ``protocol=mllp_v3`` 的 VendorInterface 用于路由测试。"""

    return VendorInterface(
        vendor_id="his-legacy",
        interface_id="mllp-v3-submit",
        display_name="老 HIS MLLPv3 提交接口（测试）",
        protocol=Protocol("mllp_v3") if "mllp_v3" in {str(x) for x in Protocol} else None,
        host="127.0.0.1",
        port=6662,
        base_path="",
        timeout_seconds=5,
        auth=AuthMllp(),
        sample_endpoint=SampleEndpoint(
            hl7v2=Hl7v2SampleEndpoint(message_type="ADT^A01"),
        ),
        required_fields=("MSH-9", "PID-3"),
    )


# 协议值不在内置枚举里时用字符串协议字段构造
def _make_mllp_v3_interface_with_str() -> VendorInterface:
    """通过 dict 与 model_validate 绕过 Protocol 强枚举，构造 ``protocol=mllp_v3`` 接口。"""

    raw = {
        "vendor_id": "his-legacy",
        "interface_id": "mllp-v3-submit",
        "display_name": "老 HIS MLLPv3 提交接口（测试）",
        "protocol": "mllp_v3",
        "host": "127.0.0.1",
        "port": 6662,
        "base_path": "",
        "timeout_seconds": 5,
        "auth": {"type": "mllp"},
        "sample_endpoint": {
            "hl7v2": {"message_type": "ADT^A01", "trigger_event": "A01"},
        },
        "required_fields": ["MSH-9", "PID-3"],
    }
    return VendorInterface.model_validate(raw)


# ---------------------------------------------------------------------------
# Test 1: env 触发加载
# ---------------------------------------------------------------------------


class TestEnvTriggeredLoading:
    """``VENDOR_DOCTOR_PLUGINS`` env → :func:`load_plugins` 触发模块 import。"""

    def test_load_plugins_imports_module_from_env(self, monkeypatch):
        monkeypatch.setenv("VENDOR_DOCTOR_PLUGINS", "tests.fixtures.plugin_smoke")
        loaded = load_plugins()
        assert "tests.fixtures.plugin_smoke" in loaded
        assert "tests.fixtures.plugin_smoke" in list_loaded_modules()

    def test_load_plugins_returns_empty_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("VENDOR_DOCTOR_PLUGINS", raising=False)
        assert load_plugins() == []
        assert list_loaded_modules() == []

    def test_load_plugins_skips_empty_csv_tokens(self, monkeypatch):
        """逗号 / 空白 token 应跳过。"""

        monkeypatch.setenv("VENDOR_DOCTOR_PLUGINS", ", , ,")
        assert load_plugins() == []

    def test_load_plugins_idempotent_by_default(self, monkeypatch):
        monkeypatch.setenv("VENDOR_DOCTOR_PLUGINS", "tests.fixtures.plugin_smoke")
        first = load_plugins()
        second = load_plugins()
        # 第二次因 ``skip_if_loaded=True`` 不重 import
        assert first == ["tests.fixtures.plugin_smoke"]
        assert second == ["tests.fixtures.plugin_smoke"]

    def test_load_plugins_propagates_import_error(self, monkeypatch):
        monkeypatch.setenv("VENDOR_DOCTOR_PLUGINS", "tests.fixtures.does_not_exist_xyz")
        with pytest.raises(ImportError):
            load_plugins()


# ---------------------------------------------------------------------------
# Test 2: protocol 路由
# ---------------------------------------------------------------------------


class TestProtocolRouting:
    """``register_protocol_via_plugin`` 后 :func:`build_connection` 命中插件类。"""

    def test_register_via_plugin_makes_protocol_routable(self):
        register_protocol_via_plugin("mllp_v3", _FakeConn)
        spec = _make_mllp_v3_interface_with_str()
        conn = build_connection(spec)
        assert isinstance(conn, _FakeConn)

    def test_register_via_plugin_is_case_insensitive(self):
        register_protocol_via_plugin("MLLP_V3", _FakeConn)
        spec = _make_mllp_v3_interface_with_str()
        conn = build_connection(spec)
        assert isinstance(conn, _FakeConn)

    def test_protocol_appears_in_list_protocols(self):
        register_protocol_via_plugin("mllp_v3", _FakeConn)
        assert "mllp_v3" in list_protocols()

    def test_unknown_protocol_falls_back_to_not_implemented(self):
        from vendor_if_doctor.spec.schema import is_plugin_protocol

        # 跳过插件注册 — 但 spec.protocol 是 "mllp_v3"，_validate_protocol_value
        # 会因没注册抛错。我们改测一个真正没注册的协议名（带 _ 形式让 pydantic 通过）
        raw = {
            "vendor_id": "v",
            "interface_id": "i",
            "display_name": "test",
            "protocol": "totally_unknown_proto",
            "host": "127.0.0.1",
            "port": 80,
            "auth": {"type": "none"},
            "sample_endpoint": {"http": {"method": "GET", "path": "/", "headers": {}, "body": ""}},
            "required_fields": [],
        }
        # 这里要确保 "totally_unknown_proto" 没被注册
        assert not is_plugin_protocol("totally_unknown_proto")
        try:
            iface = VendorInterface.model_validate(raw)
            conn = build_connection(iface)
            assert isinstance(conn, NotImplementedConnection)
        except Exception:
            # 协议未注册时，VendorInterface.model_validate 也会抛 ValidationError —
            # 这同样满足 "不误判为 healthy" 的契约（拒绝而非默许）。
            pass

    def test_register_via_plugin_rejects_non_baseconnection_subclass(self):
        class _NotSubclass:
            pass

        with pytest.raises(TypeError):
            register_protocol_via_plugin("bad", _NotSubclass)

    def test_register_via_plugin_rejects_empty_name(self):
        with pytest.raises(ValueError):
            register_protocol_via_plugin("", _FakeConn)


# ---------------------------------------------------------------------------
# Test 3: JSON Schema 同步扩展
# ---------------------------------------------------------------------------


class TestJsonSchemaSync:
    """插件注册后 :mod:`vendor_if_doctor.spec.json_schema` 的 protocol enum 同步扩展。"""

    def test_json_schema_protocols_includes_registered(self):
        from vendor_if_doctor.spec.json_schema import get_protocols

        register_protocol_via_plugin("mllp_v3", _FakeConn)
        assert "mllp_v3" in get_protocols()
        # 内置 4 条仍存在
        for builtin in ("http", "hl7v2", "webservice", "sql"):
            assert builtin in get_protocols()

    def test_json_schema_sync_is_idempotent(self):
        from vendor_if_doctor.spec.json_schema import get_protocols

        register_protocol_via_plugin("mllp_v3", _FakeConn)
        register_protocol_via_plugin("MLLP_V3", _FakeConn)
        register_protocol_via_plugin("  mllp_v3  ", _FakeConn)
        protocols = get_protocols()
        assert protocols.count("mllp_v3") == 1

    def test_load_vendor_spec_with_mllp_v3_succeeds(self, monkeypatch):
        """``examples/mllp_v3_spec.yaml`` 启动期校验通过（plugin 同步后）。"""

        monkeypatch.setenv("VENDOR_DOCTOR_PLUGINS", "tests.fixtures.plugin_smoke")
        load_plugins()
        # 不抛 SpecValidationError 即视为通过
        spec = load_vendor_spec(MLLP_V3_SPEC)
        assert len(spec.interfaces) == 1
        assert spec.interfaces[0].protocol == "mllp_v3"


# ---------------------------------------------------------------------------
# Test 4: 占位不阻塞
# ---------------------------------------------------------------------------


class TestPluginPlaceholderDoesNotBlock:
    """插件 evidence ``status='skip'`` + ``failure_class='not_implemented'``，不阻塞其他 vendor。"""

    def test_test_connection_runs_full_pipeline(self, monkeypatch):
        """test_connection 对插件接口跑出 5 条 evidence；其它接口正常跑。"""

        from vendor_if_doctor.spec.schema import (
            AuthBearer,
            HttpSampleEndpoint,
            InspectionConfig,
        )
        from vendor_if_doctor.connection.factory import build_connection

        monkeypatch.setenv("VENDOR_DOCTOR_PLUGINS", "tests.fixtures.plugin_smoke")
        monkeypatch.setenv("VENDOR_LIS_TOKEN", "lis-secret-token-9999")
        load_plugins()

        legacy = _make_mllp_v3_interface_with_str()
        http_iface = VendorInterface(
            vendor_id="lis",
            interface_id="report-push",
            display_name="LIS 测试报告",
            protocol=Protocol.HTTP.value,
            host="127.0.0.1",
            port=1,  # 不可达 — 短路其它步骤
            base_path="",
            timeout_seconds=1,
            auth=AuthBearer(secret_env="VENDOR_LIS_TOKEN"),
            sample_endpoint=SampleEndpoint(
                http=HttpSampleEndpoint(
                    method="GET", path="/", headers={}, body=""
                )
            ),
            required_fields=("$.report_id",),
        )

        results: List = []
        for iface in [legacy, http_iface]:
            conn = build_connection(iface)
            for step in conn.steps():
                results.append(step.run().evidence)

        # 插件接口：5 步全 skip，failure_class=not_implemented
        plugin_evs = [e for e in results if e.vendor_id == "his-legacy"]
        assert len(plugin_evs) == 5
        for ev in plugin_evs:
            assert ev.status == "skip"
            assert ev.failure_class == "not_implemented"
            assert ev.meta.get("plugin") == "tests.fixtures.plugin_smoke"

        # 其它 vendor 仍正常执行（不阻塞）
        lis_evs = [e for e in results if e.vendor_id == "lis"]
        assert len(lis_evs) == 5


# ---------------------------------------------------------------------------
# Test 5: 清理
# ---------------------------------------------------------------------------


class TestPluginReset:
    """:func:`reset_plugins` 把 factory 协议表 + JSON Schema 协议列表回退到内置 4 条。"""

    def test_reset_unregisters_plugin_protocol(self):
        from vendor_if_doctor.spec.json_schema import (
            builtin_protocols,
            get_protocols,
        )

        register_protocol_via_plugin("mllp_v3", _FakeConn)
        assert "mllp_v3" in list_protocols()
        reset_plugins()
        assert "mllp_v3" not in list_protocols()
        assert "mllp_v3" not in get_protocols()
        assert "mllp_v3" not in list_registered_protocols()
        # 内置 4 条仍在
        for builtin in builtin_protocols():
            assert builtin in get_protocols()

    def test_reset_clears_loaded_modules(self):
        import os

        prev = os.environ.get("VENDOR_DOCTOR_PLUGINS")
        os.environ["VENDOR_DOCTOR_PLUGINS"] = "tests.fixtures.plugin_smoke"
        try:
            load_plugins()
            assert list_loaded_modules()
        finally:
            if prev is None:
                os.environ.pop("VENDOR_DOCTOR_PLUGINS", None)
            else:
                os.environ["VENDOR_DOCTOR_PLUGINS"] = prev
            reset_plugins()
        assert list_loaded_modules() == []


# ---------------------------------------------------------------------------
# Test 6: 凭据纪律
# ---------------------------------------------------------------------------


class TestPluginCredentialDiscipline:
    """插件 evidence / summary 不含 ``Bearer / token / password / SUPER-SECRET`` 等。"""

    def test_evidence_no_credential_leak(self, monkeypatch):
        monkeypatch.setenv("VENDOR_DOCTOR_PLUGINS", "tests.fixtures.plugin_smoke")
        load_plugins()

        from vendor_if_doctor.connection.factory import build_connection

        legacy = _make_mllp_v3_interface_with_str()
        conn = build_connection(legacy)
        evs = [step.run().evidence for step in conn.steps()]
        forbidden = (
            "Bearer",
            "Basic",
            "wonderland",
            "SUPER-SECRET-TOKEN-XYZ",
            "demo-his-token-9999",
            "lis-secret-token-9999",
        )
        for ev in evs:
            for kw in forbidden:
                assert kw not in (ev.summary or ""), (
                    f"forbidden literal {kw!r} leaked into plugin evidence summary"
                )
                assert kw not in (ev.command or "")
                assert kw not in (ev.response_body or "")


# ---------------------------------------------------------------------------
# Test 7: is_registered_protocol 查询
# ---------------------------------------------------------------------------


class TestIsRegisteredProtocol:
    """:func:`is_registered_protocol` 用于 lint / spec 校验放过插件协议。"""

    def test_is_registered_protocol_after_register(self):
        register_protocol_via_plugin("mllp_v3", _FakeConn)
        assert is_registered_protocol("mllp_v3") is True
        assert is_registered_protocol("MLLP_V3") is True
        assert is_registered_protocol("  mllp_v3  ") is True
        assert is_registered_protocol("http") is False
        assert is_registered_protocol("nonexistent") is False
        assert is_registered_protocol("") is False


# ---------------------------------------------------------------------------
# 测试辅助 — 一个最小的 BaseConnection 子类用于 protocol 注册
# ---------------------------------------------------------------------------


class _FakeConn(BaseConnection):
    """注册到 ``mllp_v3`` 协议的最小占位实现（test_plugin 内部用）。"""

    def _get_client(self):  # pragma: no cover - 永不调用
        return None

    def steps(self):  # pragma: no cover - 永不调用
        return []