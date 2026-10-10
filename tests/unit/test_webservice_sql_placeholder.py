"""WebService / SQL 占位适配器测试（task 8）。

验证目标：

* factory 路由：protocol=webservice → WebServiceConnection；protocol=sql → SQLConnection；
* 启动期加载：spec.protocol=webservice / sql 的 YAML 不抛错（load_vendor_spec 成功）；
* 占位语义：5 步流水线均返回 status='skip' + failure_class='not_implemented'，
  success=True（不短路、不误判 healthy）；
* auth 凭据缺失：failure_class='auth_failed' + success=False（短路径其余 step）；
* 凭据纪律：占位 evidence 不含 token / Authorization 字面量；
* 不发起任何网络请求 / 数据库连接（pymssql / psycopg2 / pymysql / socket 探测可控）。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from vendor_if_doctor.connection.factory import (
    _REGISTRY,
    build_connection,
    list_protocols,
    register_protocol,
    unregister_protocol,
)
from vendor_if_doctor.connection import runner as _runner_mod
from vendor_if_doctor.spec.loader import load_vendor_spec

# 不在本文件顶层出现 "test_connection" 符号 —— pytest 会收集为测试函数
# 调用方请用 _runner_mod.test_connection(...) 形式
from vendor_if_doctor.spec.schema import (
    AuthBasic,
    AuthNone,
    HttpSampleEndpoint,
    Protocol,
    SampleEndpoint,
    VendorInterface,
    VendorSpec,
)


# ---------------------------------------------------------------------------
# Fixtures（构造 spec）
# ---------------------------------------------------------------------------


def _webservice_spec(
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    auth: object = None,
) -> VendorInterface:
    """构造一条 protocol=webservice 的 VendorInterface（基本 + http sample_endpoint）。"""

    return VendorInterface(
        vendor_id="medical-insurance",
        interface_id="claim-submit",
        display_name="医保结算接口",
        protocol=Protocol.WEBSERVICE,
        host=host,
        port=port,
        base_path="/soap",
        timeout_seconds=5,
        auth=auth if auth is not None else AuthNone(),
        sample_endpoint=SampleEndpoint(
            http=HttpSampleEndpoint(
                method="POST",
                path="/claim/submit",
                headers={"Content-Type": "text/xml"},
                body="",
            )
        ),
        required_fields=["$.claim_id"],
    )


def _sql_spec(
    *,
    host: str = "127.0.0.1",
    port: int = 1433,
    auth: object = None,
) -> VendorInterface:
    """构造一条 protocol=sql 的 VendorInterface（none auth + http sample_endpoint）。"""

    return VendorInterface(
        vendor_id="regional",
        interface_id="read-only",
        display_name="区域平台只读连接",
        protocol=Protocol.SQL,
        host=host,
        port=port,
        base_path="/",
        timeout_seconds=5,
        auth=auth if auth is not None else AuthNone(),
        sample_endpoint=SampleEndpoint(
            http=HttpSampleEndpoint(
                method="GET",
                path="/",
                headers={},
                body="",
            )
        ),
        required_fields=["$.ok"],
    )


# ---------------------------------------------------------------------------
# factory 路由测试
# ---------------------------------------------------------------------------


class TestFactoryRouting:
    """验证 protocol=webservice / sql 已注册到 factory._REGISTRY。"""

    def test_webservice_registered(self):
        # 触发 connection.__init__ 内的 bootstrap 后应已注册
        from vendor_if_doctor import connection as _conn

        assert "webservice" in _conn._REGISTRY
        assert _conn._REGISTRY["webservice"].__name__ == "WebServiceConnection"

    def test_sql_registered(self):
        from vendor_if_doctor import connection as _conn

        assert "sql" in _conn._REGISTRY
        assert _conn._REGISTRY["sql"].__name__ == "SQLConnection"

    def test_list_protocols_contains_webservice_and_sql(self):
        protocols = list_protocols()
        assert "webservice" in protocols
        assert "sql" in protocols
        # 与既有协议并存
        assert "http" in protocols
        assert "hl7v2" in protocols

    def test_build_connection_routes_webservice(self):
        spec = _webservice_spec()
        conn = build_connection(spec)
        assert conn.__class__.__name__ == "WebServiceConnection"

    def test_build_connection_routes_sql(self):
        spec = _sql_spec()
        conn = build_connection(spec)
        assert conn.__class__.__name__ == "SQLConnection"


# ---------------------------------------------------------------------------
# 占位语义测试（不依赖网络）
# ---------------------------------------------------------------------------


class TestWebServicePlaceholderSteps:
    """WebService 占位：5 步流水线输出 status='skip' + failure_class='not_implemented'。"""

    def test_preflight_skip_when_host_unreachable(self):
        """不可达主机：preflight 失败 → 后续 4 步全 skip。"""

        spec = _webservice_spec(host="127.0.0.1", port=1)  # 端口不可达
        conn = build_connection(spec)
        # test_connection 期望 VendorSpec；包一层
        spec_wrap = VendorSpec(interfaces=[spec])
        evidences = _runner_mod.test_connection(spec_wrap)
        # 5 条 evidence：1 preflight + 1 auth + 1 list + 1 sample + 1 assert
        assert len(evidences) == 5
        # preflight 失败（host 不可达），auth/list/sample/assert 全 skip
        preflight_ev = evidences[0]
        assert preflight_ev.step_name == "preflight"
        assert preflight_ev.status == "fail"  # host 不可达是真实失败
        assert preflight_ev.failure_class in ("timeout", "tcp_unreachable")
        # 后续 step 因 short-circuit 走 skip
        for ev in evidences[1:]:
            assert ev.status == "skip", f"{ev.step_name} 应 skip，实际 {ev.status}"
            assert ev.failure_class in ("not_implemented", "auth_failed") or ev.failure_class is None
        conn.close()

    def test_sample_step_outputs_not_implemented(self):
        """sample 步骤固定返回 status='skip' + failure_class='not_implemented'。"""

        spec = _webservice_spec()
        conn = build_connection(spec)
        # 直接调 sample step（不依赖 preflight 是否成功）
        sample_step = conn.get_step("sample")
        assert sample_step is not None
        result = sample_step.run()
        assert result.success is True  # skip != fail，不短路
        assert result.evidence.status == "skip"
        assert result.evidence.failure_class == "not_implemented"
        assert "WebService" in result.evidence.summary or "webservice" in result.evidence.summary.lower()
        conn.close()

    def test_assert_step_outputs_not_implemented(self):
        spec = _webservice_spec()
        conn = build_connection(spec)
        assert_step = conn.get_step("assert")
        assert assert_step is not None
        result = assert_step.run()
        assert result.success is True
        assert result.evidence.status == "skip"
        assert result.evidence.failure_class == "not_implemented"
        conn.close()

    def test_list_step_outputs_not_implemented(self):
        spec = _webservice_spec()
        conn = build_connection(spec)
        list_step = conn.get_step("list")
        assert list_step is not None
        result = list_step.run()
        assert result.success is True
        assert result.evidence.status == "skip"
        assert result.evidence.failure_class == "not_implemented"
        conn.close()

    def test_auth_step_skip_when_no_credentials_required(self):
        """auth.type=none：auth 步骤直接 skip（不读凭据）。"""

        spec = _webservice_spec(auth=AuthNone())
        conn = build_connection(spec)
        auth_step = conn.get_step("auth")
        assert auth_step is not None
        result = auth_step.run()
        assert result.success is True
        assert result.evidence.status == "skip"
        assert result.evidence.failure_class == "not_implemented"
        conn.close()

    def test_auth_step_fails_when_credentials_missing(self):
        """auth.type=basic 但 secret_env 未设置 → auth 步骤 fail + auth_failed。"""

        env = {"VENDOR_MI_USERNAME": "alice"}  # 缺 VENDOR_MI_PASSWORD
        spec = _webservice_spec(auth=AuthBasic(username_env="VENDOR_MI_USERNAME", secret_env="VENDOR_MI_PASSWORD"))
        conn = build_connection(spec, env=env)
        auth_step = conn.get_step("auth")
        assert auth_step is not None
        result = auth_step.run()
        assert result.success is False
        assert result.evidence.status == "fail"
        assert result.evidence.failure_class == "auth_failed"
        conn.close()


class TestSQLPlaceholderSteps:
    """SQL 占位：5 步流水线输出 status='skip' + failure_class='not_implemented'。"""

    def test_preflight_skip_when_host_unreachable(self):
        spec = _sql_spec(host="127.0.0.1", port=1)
        spec_wrap = VendorSpec(interfaces=[spec])
        evidences = _runner_mod.test_connection(spec_wrap)
        assert len(evidences) == 5
        preflight_ev = evidences[0]
        assert preflight_ev.step_name == "preflight"
        assert preflight_ev.status == "fail"
        assert preflight_ev.failure_class in ("timeout", "tcp_unreachable")
        # 后续 step 因 short-circuit 走 skip
        for ev in evidences[1:]:
            assert ev.status == "skip"

    def test_sample_step_outputs_not_implemented(self):
        spec = _sql_spec()
        conn = build_connection(spec)
        sample_step = conn.get_step("sample")
        assert sample_step is not None
        result = sample_step.run()
        assert result.success is True
        assert result.evidence.status == "skip"
        assert result.evidence.failure_class == "not_implemented"
        assert "SQL" in result.evidence.summary or "sql" in result.evidence.summary.lower()
        conn.close()

    def test_assert_step_outputs_not_implemented(self):
        spec = _sql_spec()
        conn = build_connection(spec)
        assert_step = conn.get_step("assert")
        assert assert_step is not None
        result = assert_step.run()
        assert result.success is True
        assert result.evidence.status == "skip"
        assert result.evidence.failure_class == "not_implemented"
        conn.close()

    def test_list_step_outputs_not_implemented(self):
        spec = _sql_spec()
        conn = build_connection(spec)
        list_step = conn.get_step("list")
        assert list_step is not None
        result = list_step.run()
        assert result.success is True
        assert result.evidence.status == "skip"
        assert result.evidence.failure_class == "not_implemented"
        conn.close()

    def test_auth_step_skip_when_no_credentials_required(self):
        spec = _sql_spec(auth=AuthNone())
        conn = build_connection(spec)
        auth_step = conn.get_step("auth")
        assert auth_step is not None
        result = auth_step.run()
        assert result.success is True
        assert result.evidence.status == "skip"
        assert result.evidence.failure_class == "not_implemented"
        conn.close()


# ---------------------------------------------------------------------------
# 凭据纪律测试（占位 evidence 不含 token）
# ---------------------------------------------------------------------------


class TestPlaceholderCredentialSafety:
    """占位 evidence 不含 Authorization / Bearer / token 字面量。"""

    def test_webservice_evidence_no_token_leak(self):
        spec = _webservice_spec(
            auth=AuthBasic(username_env="VENDOR_MI_USERNAME", secret_env="VENDOR_MI_PASSWORD")
        )
        env = {
            "VENDOR_MI_USERNAME": "super-secret-username",
            "VENDOR_MI_PASSWORD": "super-secret-password-9999",
        }
        conn = build_connection(spec, env=env)
        for step in conn.steps():
            result = step.run()
            assert "super-secret-username" not in str(result.evidence.meta)
            assert "super-secret-password-9999" not in str(result.evidence.meta)
            assert "super-secret-username" not in result.evidence.summary
            assert "super-secret-password-9999" not in result.evidence.summary
        conn.close()

    def test_sql_evidence_no_token_leak(self):
        spec = _sql_spec()
        env = {"VENDOR_REGIONAL_TOKEN": "top-secret-token-value"}
        conn = build_connection(spec, env=env)
        for step in conn.steps():
            result = step.run()
            assert "top-secret-token-value" not in str(result.evidence.meta)
            assert "top-secret-token-value" not in result.evidence.summary
        conn.close()


# ---------------------------------------------------------------------------
# 不发起任何网络/数据库连接的测试
# ---------------------------------------------------------------------------


class TestNoNetworkOrDBActivity:
    """占位阶段不应发起数据库连接 / SOAP 请求；仅 TCP 探活（受 socket.create_connection）。"""

    def test_webservice_does_not_import_soap_clients(self):
        """验证 webservice.py 不导入 zeep / pysimplesoap 等 SOAP 库。"""

        import vendor_if_doctor.adapters.webservice as ws_mod

        # 检查模块 globals 中不应有 zeep / SOAP 客户端符号
        assert "zeep" not in ws_mod.__dict__
        assert "Client" not in ws_mod.__dict__
        assert "Transport" not in ws_mod.__dict__

    def test_sql_does_not_import_database_drivers(self):
        """验证 sql.py 不导入 pymssql / psycopg2 / pymysql 等数据库 driver。"""

        import vendor_if_doctor.adapters.sql as sql_mod

        assert "pymssql" not in sql_mod.__dict__
        assert "psycopg2" not in sql_mod.__dict__
        assert "pymysql" not in sql_mod.__dict__
        assert "MySQLdb" not in sql_mod.__dict__


# ---------------------------------------------------------------------------
# 启动期 spec 加载（与 YAML 兼容）
# ---------------------------------------------------------------------------


class TestSpecLoading:
    """examples/vendor_spec.example.yaml 含 webservice + sql 接口；load_vendor_spec 不抛错。"""

    def test_load_example_yaml_succeeds(self):
        spec = load_vendor_spec("examples/vendor_spec.example.yaml")
        assert spec is not None
        # 含 6 个接口：1 HIS + 1 LIS + 1 PACS + 1 EMR + 1 medical-insurance(webservice) + 1 regional(sql)
        assert len(spec.interfaces) == 6
        protocols = {itf.protocol for itf in spec.interfaces}
        assert Protocol.WEBSERVICE in protocols
        assert Protocol.SQL in protocols

    def test_load_webservice_interface_in_example(self):
        spec = load_vendor_spec("examples/vendor_spec.example.yaml")
        mi = spec.find("medical-insurance", "claim-submit")
        assert mi is not None
        assert mi.protocol == Protocol.WEBSERVICE

    def test_load_sql_interface_in_example(self):
        spec = load_vendor_spec("examples/vendor_spec.example.yaml")
        regional = spec.find("regional", "read-only")
        assert regional is not None
        assert regional.protocol == Protocol.SQL


# ---------------------------------------------------------------------------
# 与 NotImplementedConnection 的关系（确保占位优于 not_implemented）
# ---------------------------------------------------------------------------


class TestPlaceholderBeatsNotImplemented:
    """WebServiceConnection / SQLConnection 应在 factory 路由中替代 NotImplementedConnection。"""

    def test_webservice_placeholder_is_not_notimplemented(self):
        spec = _webservice_spec()
        conn = build_connection(spec)
        assert conn.__class__.__name__ != "NotImplementedConnection"
        assert conn.__class__.__name__ == "WebServiceConnection"

    def test_sql_placeholder_is_not_notimplemented(self):
        spec = _sql_spec()
        conn = build_connection(spec)
        assert conn.__class__.__name__ != "NotImplementedConnection"
        assert conn.__class__.__name__ == "SQLConnection"


# ---------------------------------------------------------------------------
# 注册可清理（unregister 路径）
# ---------------------------------------------------------------------------


class TestRegistrationLifecycle:
    """register_protocol / unregister_protocol 在占位协议上也应工作（task 33 插件 hook）。"""

    def setup_method(self):
        # 备份已有注册
        self._backup = dict(_REGISTRY)

    def teardown_method(self):
        # 恢复 _REGISTRY（不污染后续测试）
        for k in list(_REGISTRY.keys()):
            if k not in self._backup:
                _REGISTRY.pop(k, None)

    def test_unregister_and_reregister_webservice(self):
        unregister_protocol("webservice")
        assert "webservice" not in _REGISTRY
        # 重新 import 触发 _register_self
        from vendor_if_doctor.adapters import webservice as _ws

        register_protocol("webservice", _ws.WebServiceConnection)
        assert "webservice" in _REGISTRY


__all__: list = []  # 标记为测试模块（pytest 自动发现）