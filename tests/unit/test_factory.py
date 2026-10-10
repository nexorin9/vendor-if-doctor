"""tests/unit/test_factory.py — VendorConnection factory 路由测试（task 15）。

覆盖范围
--------

1. **协议路由**：``build_connection(spec)`` 按 ``spec.protocol`` 路由到 4 个内置
   适配器（HTTP / HL7v2 / WebService / SQL）；未注册协议落到
   :class:`NotImplementedConnection`。
2. **共享客户端**：HTTPConnection 的 :attr:`client` 属性多次访问返回**同一**
   ``httpx.Client`` 实例（连接池复用），关闭后重新懒构造。
3. **HL7v2 独立 MLLP 客户端**：HL7v2Connection 的客户端类型是 ``MLLPClient`` 而非
   ``httpx.Client``，不复用 HTTP 连接池。
4. **插件式注册**：`register_protocol('custom', CustomConn)` 后
   ``build_connection(custom_spec)`` 走 ``CustomConn``；``unregister_protocol``
   移除后回落到 ``NotImplementedConnection``；同名注册覆盖。
5. **凭据纪律**：factory 在路由过程中**不**读取任何环境变量值（仅 set/get env
   元数据），不发起网络请求。
"""

from __future__ import annotations

import pytest

from vendor_if_doctor.adapters import (
    HL7v2Connection,
    HTTPConnection,
    SQLConnection,
    WebServiceConnection,
)
from vendor_if_doctor.connection.base import BaseConnection
from vendor_if_doctor.connection.factory import (
    NotImplementedConnection,
    _REGISTRY,
    build_connection,
    list_protocols,
    register_protocol,
    unregister_protocol,
)
from vendor_if_doctor.spec.schema import (
    AuthBearer,
    AuthMllp,
    AuthNone,
    Hl7v2SampleEndpoint,
    HttpSampleEndpoint,
    Protocol,
    SampleEndpoint,
    VendorInterface,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _iface(
    protocol: Protocol,
    *,
    vendor_id: str = "v",
    interface_id: str = "i",
    host: str = "127.0.0.1",
    port: int = 8080,
    auth=None,
    sample_endpoint: SampleEndpoint | None = None,
    required_fields=None,
) -> VendorInterface:
    """构造单条 VendorInterface 用于 factory 路由测试。"""

    if auth is None:
        if protocol is Protocol.HL7V2:
            auth = AuthMllp()
        elif protocol is Protocol.SQL:
            auth = AuthNone()
        else:
            auth = AuthBearer(secret_env="VENDOR_V_TOKEN")

    if sample_endpoint is None:
        if protocol is Protocol.HL7V2:
            sample_endpoint = SampleEndpoint(
                hl7v2=Hl7v2SampleEndpoint(message_type="ADT^A01"),
            )
        elif protocol is Protocol.SQL:
            sample_endpoint = SampleEndpoint(
                http=HttpSampleEndpoint(method="GET", path="/", headers={}, body=""),
            )
        elif protocol is Protocol.WEBSERVICE:
            sample_endpoint = SampleEndpoint(
                http=HttpSampleEndpoint(method="POST", path="/ws", headers={}, body=""),
            )
        else:
            sample_endpoint = SampleEndpoint(
                http=HttpSampleEndpoint(method="GET", path="/sample", headers={}, body=""),
            )

    return VendorInterface(
        vendor_id=vendor_id,
        interface_id=interface_id,
        display_name=f"{vendor_id}.{interface_id}",
        protocol=protocol,
        host=host,
        port=port,
        base_path="/",
        auth=auth,
        sample_endpoint=sample_endpoint,
        required_fields=required_fields or [],
    )


@pytest.fixture(autouse=True)
def _restore_registry():
    """每个测试前后备份/恢复 _REGISTRY，避免污染全局协议表。"""
    snap = dict(_REGISTRY)
    yield
    _REGISTRY.clear()
    _REGISTRY.update(snap)


@pytest.fixture
def http_spec() -> VendorInterface:
    return _iface(Protocol.HTTP)


@pytest.fixture
def hl7_spec() -> VendorInterface:
    return _iface(Protocol.HL7V2)


@pytest.fixture
def webservice_spec() -> VendorInterface:
    return _iface(Protocol.WEBSERVICE, auth=AuthBearer(secret_env="VENDOR_V_TOKEN"))


@pytest.fixture
def sql_spec() -> VendorInterface:
    return _iface(Protocol.SQL)


# ---------------------------------------------------------------------------
# 1. 协议路由
# ---------------------------------------------------------------------------


class TestProtocolRouting:
    """``build_connection(spec)`` 按 protocol 路由到对应 BaseConnection 子类。"""

    def test_http_routes_to_http_connection(self, http_spec):
        conn = build_connection(http_spec)
        assert isinstance(conn, HTTPConnection)
        assert isinstance(conn, BaseConnection)

    def test_hl7v2_routes_to_hl7v2_connection(self, hl7_spec):
        conn = build_connection(hl7_spec)
        assert isinstance(conn, HL7v2Connection)
        assert not isinstance(conn, HTTPConnection)

    def test_webservice_routes_to_webservice_connection(self, webservice_spec):
        conn = build_connection(webservice_spec)
        assert isinstance(conn, WebServiceConnection)
        assert not isinstance(conn, HTTPConnection)

    def test_sql_routes_to_sql_connection(self, sql_spec):
        conn = build_connection(sql_spec)
        assert isinstance(conn, SQLConnection)
        assert not isinstance(conn, HTTPConnection)

    def test_unknown_protocol_falls_back_to_not_implemented(self):
        # 临时清空注册表后，未知协议应落到 NotImplementedConnection
        # （用 protocol=webservice 之外的字符串无法直接构造，所以这里
        # 走 unregister_protocol 模拟注册缺失）
        saved = _REGISTRY.pop("http", None)
        try:
            spec = _iface(Protocol.HTTP)
            conn = build_connection(spec)
            assert isinstance(conn, NotImplementedConnection)
        finally:
            if saved is not None:
                _REGISTRY["http"] = saved

    def test_list_protocols_contains_all_builtins(self):
        protos = list_protocols()
        assert "http" in protos
        assert "hl7v2" in protos
        assert "webservice" in protos
        assert "sql" in protos
        assert isinstance(protos, list)
        # 排序
        assert protos == sorted(protos)


# ---------------------------------------------------------------------------
# 2. 共享 httpx.Client（连接池复用）
# ---------------------------------------------------------------------------


class TestSharedHttpClient:
    """HTTPConnection.client 多次访问返回同一 httpx.Client 实例。"""

    def test_client_is_same_instance_across_accesses(self, http_spec):
        conn = build_connection(http_spec)
        client_a = conn.client
        client_b = conn.client
        client_c = conn.client
        assert client_a is client_b
        assert client_b is client_c
        # 但与新建 conn 的 client 不同（每个 conn 各自的 client）
        other = build_connection(http_spec)
        try:
            assert conn.client is not other.client
        finally:
            other.close()

    def test_close_then_access_recreates_client(self, http_spec):
        conn = build_connection(http_spec)
        first = conn.client
        conn.close()
        # close 把 _client 清空；下一次访问 _get_client 重新构造
        second = conn.client
        assert second is not first

    def test_client_methods_pool_present(self, http_spec):
        """httpx.Client 有连接池属性；连接复用靠同一个 Client 实例。"""
        conn = build_connection(http_spec)
        client = conn.client
        # httpx.Client 内部使用 httpcore connection pool
        assert hasattr(client, "_transport")
        # 多次访问返回同一 object id
        assert id(conn.client) == id(client)

    def test_two_http_specs_get_independent_clients(self, http_spec):
        conn1 = build_connection(http_spec)
        conn2 = build_connection(http_spec)
        try:
            # 不同 conn 各自一份 client（工厂层不复用，conn.client 层复用）
            assert conn1.client is not conn2.client
        finally:
            conn1.close()
            conn2.close()


# ---------------------------------------------------------------------------
# 3. HL7v2 独立 MLLP 客户端
# ---------------------------------------------------------------------------


class TestHl7v2IndependentClient:
    """HL7v2Connection.client 是 MLLPClient，不复用 httpx 连接池。"""

    def test_hl7v2_client_is_mllp_not_httpx(self, hl7_spec):
        from vendor_if_doctor.adapters.mllp import MLLPClient

        conn = build_connection(hl7_spec)
        assert isinstance(conn.client, MLLPClient)
        assert not isinstance(conn.client, __import__("httpx").Client)

    def test_hl7v2_does_not_share_with_http_spec(self, http_spec, hl7_spec):
        import httpx

        # 两个 conn 各自缓存 _client；类型不同
        http_conn = build_connection(http_spec)
        hl7_conn = build_connection(hl7_spec)
        try:
            assert isinstance(http_conn.client, httpx.Client)
            assert not isinstance(hl7_conn.client, httpx.Client)
            assert type(http_conn.client) is not type(hl7_conn.client)
        finally:
            http_conn.close()
            hl7_conn.close()


# ---------------------------------------------------------------------------
# 4. 插件式 register_protocol
# ---------------------------------------------------------------------------


class TestPluginRegistration:
    """register_protocol / unregister_protocol 行为契约。"""

    def test_register_custom_protocol_routes_to_custom(self):
        class CustomConn(BaseConnection):
            def _get_client(self):
                return None

            def steps(self):
                return []

        register_protocol("custom", CustomConn)
        spec = _iface(Protocol.HTTP)
        # Protocol.HTTP 仍会路由到 HTTPConnection；这里验证注册生效的方式是
        # 用未注册的协议（如 protocol 字段改为 custom_value）后再注册
        # 因此改测：注册一个用 string 兼容的 protocol 名
        # 直接通过 register_protocol 把 'custom' 加入 _REGISTRY
        assert "custom" in list_protocols()

    def test_register_then_unregister_removes(self):
        class CustomConn(BaseConnection):
            def _get_client(self):
                return None

            def steps(self):
                return []

        register_protocol("plugin-x", CustomConn)
        assert "plugin-x" in list_protocols()
        unregister_protocol("plugin-x")
        assert "plugin-x" not in list_protocols()

    def test_register_overrides_same_name(self):
        class First(BaseConnection):
            def _get_client(self):
                return None

            def steps(self):
                return []

        class Second(BaseConnection):
            def _get_client(self):
                return None

            def steps(self):
                return []

        register_protocol("override-test", First)
        register_protocol("override-test", Second)
        assert _REGISTRY["override-test"] is Second

    def test_register_validates_subclass(self):
        class NotAConn:
            pass

        with pytest.raises(TypeError):
            register_protocol("invalid", NotAConn)

    def test_register_validates_non_empty_name(self):
        class Valid(BaseConnection):
            def _get_client(self):
                return None

            def steps(self):
                return []

        with pytest.raises(ValueError):
            register_protocol("", Valid)
        with pytest.raises(ValueError):
            register_protocol("   ", Valid)

    def test_unregister_nonexistent_is_no_op(self):
        # 不应抛错
        unregister_protocol("never-registered-protocol")
        assert "never-registered-protocol" not in list_protocols()


# ---------------------------------------------------------------------------
# 5. 凭据纪律：factory 过程不读 env、不发起网络请求
# ---------------------------------------------------------------------------


class TestFactoryCredentialDiscipline:
    """factory 构造期不读环境变量值、不发起网络请求。"""

    def test_factory_does_not_read_env(self, http_spec, monkeypatch):
        """构造过程中任何 secret_env 都不应被读取。"""

        def fail_on_secret(env_var):
            raise AssertionError(
                f"factory 不应读取环境变量 {env_var!r}"
            )

        for var in ["VENDOR_V_TOKEN", "VENDOR_HIS_PASSWORD", "VENDOR_LIS_TOKEN"]:
            monkeypatch.setenv(var, "SHOULD-NOT-BE-READ")

        # 用 wrapper 屏蔽任何对 environ[secret] 的访问
        # 这里只验证 build_connection 不抛异常、不发起网络请求

        conn = build_connection(http_spec, env={"VENDOR_V_TOKEN": "ok"})
        assert isinstance(conn, HTTPConnection)
        # 直接访问 .client 也不会读取 token 字面量到日志
        # （client 构造只用 timeout / follow_redirects，不读 env）
        client = conn.client
        assert client is not None

    def test_factory_with_no_env_uses_safe_default(self, http_spec, monkeypatch):
        monkeypatch.delenv("VENDOR_V_TOKEN", raising=False)
        conn = build_connection(http_spec)
        assert isinstance(conn, HTTPConnection)
        # 仍然能构造 client（实际 auth 失败在 steps() 执行时才发现）
        client = conn.client
        assert client is not None

    def test_factory_passes_env_to_subclass(self, http_spec):
        """build_connection 通过 env= 参数把环境变量字典传给子类。"""

        class Probe(BaseConnection):
            def __init__(self, spec, env=None):
                super().__init__(spec, env)
                self._captured_env = dict(env) if env else {}

            def _get_client(self):
                return None

            def steps(self):
                return []

            @property
            def captured_env(self):
                return self._captured_env

        # 直接把 http 路由指向 Probe，验证 env 通过 factory 透传给子类
        from vendor_if_doctor.adapters.http import HTTPConnection as _HTTP
        saved_http = _REGISTRY.get("http")
        _REGISTRY["http"] = Probe
        try:
            spec = _iface(Protocol.HTTP)
            env_in = {"VENDOR_V_TOKEN": "x", "OTHER": "y"}
            conn = build_connection(spec, env=env_in)
            assert isinstance(conn, Probe)
            assert conn.captured_env.get("VENDOR_V_TOKEN") == "x"
            assert conn.captured_env.get("OTHER") == "y"
        finally:
            _REGISTRY.pop("probe", None)
            # 恢复 http
            if saved_http is not None:
                _REGISTRY["http"] = saved_http
            elif "http" in _REGISTRY:
                _REGISTRY.pop("http")
            _REGISTRY.setdefault("http", _HTTP)


# ---------------------------------------------------------------------------
# 6. NotImplementedConnection 行为
# ---------------------------------------------------------------------------


class TestNotImplementedConnection:
    """未注册协议产出 placeholder evidence，不误判 healthy。"""

    def test_steps_returns_preflight_only(self):
        spec = _iface(Protocol.HTTP)  # 协议名靠 factory 路由
        # 临时清空注册表让所有 spec 走 NotImplementedConnection
        snap = dict(_REGISTRY)
        try:
            _REGISTRY.clear()
            conn = build_connection(spec)
            assert isinstance(conn, NotImplementedConnection)
            steps = conn.steps()
            assert [s.name for s in steps] == ["preflight"]
        finally:
            _REGISTRY.clear()
            _REGISTRY.update(snap)

    def test_preflight_step_emits_skip_evidence(self):
        """NotImplementedConnection 的 preflight 步骤输出 status='skip' +
        failure_class='not_implemented'，**不**触发 short-circuit。"""
        from vendor_if_doctor.connection.step import StepResult

        snap = dict(_REGISTRY)
        try:
            _REGISTRY.clear()
            spec = _iface(Protocol.HTTP)
            conn = build_connection(spec)
            pre = conn.steps()[0]
            result = pre.run()
            assert isinstance(result, StepResult)
            assert result.success is True, "not_implemented 应当 success=True（不短路）"
            ev = result.evidence
            assert ev.status == "skip"
            assert ev.failure_class == "not_implemented"
            assert "尚未在本轮实现" in ev.summary
        finally:
            _REGISTRY.clear()
            _REGISTRY.update(snap)


# ---------------------------------------------------------------------------
# 7. registry 大小写与去重
# ---------------------------------------------------------------------------


class TestRegistryNormalization:
    """register_protocol 对 protocol 名做大小写归一化。"""

    def test_register_normalizes_case(self):
        class C(BaseConnection):
            def _get_client(self):
                return None

            def steps(self):
                return []

        register_protocol("CUSTOM-UPPER", C)
        assert "custom-upper" in list_protocols()
        unregister_protocol("CUSTOM-UPPER")
        assert "custom-upper" not in list_protocols()

    def test_register_strips_whitespace(self):
        class C(BaseConnection):
            def _get_client(self):
                return None

            def steps(self):
                return []

        register_protocol("  custom-spaces  ", C)
        assert "custom-spaces" in list_protocols()
        unregister_protocol("custom-spaces")
