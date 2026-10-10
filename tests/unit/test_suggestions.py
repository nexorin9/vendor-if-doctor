"""suggestions 模块单元测试（task 31 失败建议动作模板库）。

覆盖范围：

* 全部 13 类 failure_class 各自能渲染 1–3 条具体命令；
* ``auth_failed`` 按 auth.type 分支（basic/bearer/apikey/mtls/mllp/none）；
* 占位符替换（必填 + 可选 + 自定义）；
* 凭据纪律：模板渲染结果不含真实 token / issuer 字面量；
* unknown / None failure_class 返回空列表；
* 公开 API（list_supported_failure_classes / placeholder_keys /
  auth_types_for_auth_failed）完整性。
"""
from __future__ import annotations

from typing import List

import pytest

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


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def full_context() -> dict:
    """完整 context（含必填 + 可选 + 自定义）。"""

    return {
        "host": "his-endpoint.example.com",
        "port": 80,
        "protocol": "http",
        "auth_type": "bearer",
        "auth_env": "VENDOR_HIS_TOKEN",
        "auth_env_alt": "VENDOR_HIS_AUTH_TOKEN",
        "timeout_seconds": 5,
        "cert_path": "/etc/ssl/his-cert.pem",
        "key_path": "/etc/ssl/his-key.pem",
        "ca_path": "/etc/ssl/his-ca.pem",
        "start_time": "2026-10-10T08:30:00",
        "end_time": "2026-10-10T08:30:30",
        "wsdl_url": "http://his-endpoint.example.com/soap?wsdl",
        "evidence_hash": "abc12345",
    }


# ---------------------------------------------------------------------------
# 公开 API 完整性
# ---------------------------------------------------------------------------


class TestPublicApi:
    def test_list_supported_failure_classes(self):
        classes = list_supported_failure_classes()
        assert isinstance(classes, list)
        # 12+ 类核心 failure_class 全覆盖（task 9 旧版仅 11 类；task 31 新增 ambient_runner_error）
        assert "auth_failed" in classes
        assert "http_status" in classes
        assert "timeout" in classes
        assert "schema_mismatch" in classes
        assert "payload_empty" in classes
        assert "hl7_ack_re" in classes
        # 扩展 failure_class
        assert "tcp_unreachable" in classes
        assert "tcp_half_closed" in classes
        assert "degraded_slow" in classes
        assert "encoding_error" in classes
        assert "hl7_ack_missing" in classes
        assert "not_implemented" in classes
        assert "ambient_runner_error" in classes
        # 排序
        assert classes == sorted(classes)

    def test_placeholder_keys(self):
        keys = placeholder_keys()
        assert isinstance(keys, frozenset)
        # 必填 10 个 + 可选 8 个
        assert len(keys) >= 18
        # 必填 4 类（task 9 旧版）+ 新增 6 类（task 31 host/port/protocol/auth_type/auth_env/timeout_seconds）
        for k in (
            "vendor_id", "vendor_token", "interface_id", "fqn",
            "host", "port", "protocol", "auth_type", "auth_env", "timeout_seconds",
        ):
            assert k in PLACEHOLDER_KEYS_REQUIRED
        # 可选 8 类
        for k in (
            "start_time", "end_time", "cert_path", "key_path",
            "ca_path", "auth_env_alt", "wsdl_url", "evidence_hash",
        ):
            assert k in PLACEHOLDER_KEYS_OPTIONAL

    def test_auth_types_for_auth_failed(self):
        types_ = auth_types_for_auth_failed()
        # 6 类鉴权（task 6 接入 5 类 + mllp 兜底）
        for t in ("basic", "bearer", "apikey", "mtls", "mllp", "none"):
            assert t in types_
        assert types_ == sorted(types_)


# ---------------------------------------------------------------------------
# failure_class 全覆盖渲染
# ---------------------------------------------------------------------------


class TestAllFailureClassesRender:
    """每个 failure_class 都能渲染出 1-3 条具体命令（不出现破损占位符）。"""

    @pytest.mark.parametrize("failure_class", list_supported_failure_classes())
    def test_renders_at_least_one_action(self, failure_class):
        actions = suggest_actions(
            failure_class, "his", "outpatient-register",
            host="his.example.com", port=80, protocol="http",
            auth_type="bearer", auth_env="VENDOR_HIS_TOKEN",
        )
        assert 1 <= len(actions) <= 3, (
            f"{failure_class} should render 1-3 actions, got {len(actions)}"
        )
        joined = "\n".join(actions)
        # 破损占位符不能出现（除已知 evidence_hash 渲染异常分支）
        for k in ("{host}", "{port}", "{vendor_id}", "{vendor_token}",
                  "{interface_id}", "{fqn}", "{auth_env}", "{auth_type}",
                  "{protocol}", "{timeout_seconds}"):
            assert k not in joined, (
                f"{failure_class} left placeholder {k} in: {joined!r}"
            )

    @pytest.mark.parametrize("failure_class", list_supported_failure_classes())
    def test_actions_are_non_empty_strings(self, failure_class):
        actions = suggest_actions(
            failure_class, "lis", "report-push",
            host="lis.example.com", port=8443, protocol="http",
            auth_type="bearer", auth_env="VENDOR_LIS_TOKEN",
        )
        for a in actions:
            assert isinstance(a, str)
            assert a.strip()  # not empty / whitespace-only


# ---------------------------------------------------------------------------
# auth_failed 按 auth.type 分支
# ---------------------------------------------------------------------------


class TestAuthFailedByAuthType:
    def test_basic_auth_specific(self):
        actions = suggest_actions(
            "auth_failed", "his", "outpatient-register",
            auth_type="basic", host="his.example.com", port=80,
            auth_env="VENDOR_HIS_PASSWORD",
        )
        joined = " ".join(actions)
        # basic 模板含 VENDOR_{vendor_token}_USERNAME 与 base64
        assert "VENDOR_HIS_USERNAME" in joined
        assert "base64" in joined.lower()

    def test_bearer_auth_specific(self):
        actions = suggest_actions(
            "auth_failed", "lis", "report-push",
            auth_type="bearer", host="lis.example.com", port=8443,
            auth_env="VENDOR_LIS_TOKEN",
        )
        joined = " ".join(actions)
        # bearer 模板含 VENDOR_{vendor_token}_TOKEN + jwt.io 提示
        assert "VENDOR_LIS_TOKEN" in joined
        assert "jwt.io" in joined.lower() or "JWT" in joined

    def test_apikey_auth_specific(self):
        actions = suggest_actions(
            "auth_failed", "pacs", "study-booking",
            auth_type="apikey", host="pacs.example.com", port=443,
            auth_env="VENDOR_PACS_APIKEY",
        )
        joined = " ".join(actions)
        # apikey 模板含 VENDOR_{vendor_token}_APIKEY + X-API-Key 注入提示
        assert "VENDOR_PACS_APIKEY" in joined
        assert "X-API-Key" in joined

    def test_mtls_auth_specific(self):
        actions = suggest_actions(
            "auth_failed", "his-bj", "outpatient-register",
            auth_type="mtls", host="his-bj.example.com", port=443,
            auth_env="VENDOR_HIS_BJ_CERT",
            cert_path="/etc/ssl/his-cert.pem",
            key_path="/etc/ssl/his-key.pem",
        )
        joined = " ".join(actions)
        # mtls 模板含 openssl verify + CERT/KEY env 名
        assert "openssl" in joined.lower()
        assert "VENDOR_HIS_BJ_CERT" in joined

    def test_mllp_auth_specific(self):
        actions = suggest_actions(
            "auth_failed", "emr", "emr-submit",
            auth_type="mllp", host="emr.example.com", port=6661,
            auth_env="MLLP", protocol="hl7v2",
        )
        joined = " ".join(actions)
        # mllp 模板含 IP 白名单 + telnet
        assert "telnet" in joined.lower() or "IP" in joined

    def test_none_auth_specific(self):
        actions = suggest_actions(
            "auth_failed", "regional", "read-only",
            auth_type="none", host="regional.example.com", port=1433,
            auth_env="", protocol="sql",
        )
        joined = " ".join(actions)
        # none 模板含 IP 白名单 + telnet
        assert "telnet" in joined.lower() or "IP" in joined

    def test_auth_failed_without_auth_type_falls_back_to_bearer(self):
        actions = suggest_actions(
            "auth_failed", "his", "outpatient-register",
            host="his.example.com", port=80,
        )
        joined = " ".join(actions)
        # 未传 auth_type → fallback 到 bearer 模板（因为模板库默认兜底）
        # bearer 模板首条是 VENDOR_{vendor_token}_TOKEN，因此应含 VENDOR_HIS_TOKEN
        assert "VENDOR_HIS_TOKEN" in joined


# ---------------------------------------------------------------------------
# 占位符替换
# ---------------------------------------------------------------------------


class TestPlaceholderSubstitution:
    def test_vendor_id_lowercase_preserved(self):
        # tcp_unreachable 模板含 grep -i {vendor_id}
        actions = suggest_actions(
            "tcp_unreachable", "his-bj", "outpatient-register",
            host="his-bj.example.com", port=443,
        )
        joined = " ".join(actions)
        assert "his-bj" in joined  # vendor_id 保留原大小写
        assert "{vendor_id}" not in joined

    def test_vendor_token_uppercased(self):
        actions = suggest_actions(
            "auth_failed", auth_type="bearer", vendor_id="his-bj", interface_id="x",
        )
        joined = " ".join(actions)
        # vendor_token = "HIS_BJ"
        assert "HIS_BJ" in joined
        assert "{vendor_token}" not in joined

    def test_fqn_is_valid_placeholder_in_set(self):
        # 验证 fqn 已在 placeholder_keys() 里暴露（便于文档自检与 lint）；
        # 实际模板暂未使用 fqn，但接口契约上保持可替换
        assert "fqn" in PLACEHOLDER_KEYS_REQUIRED
        # _build_default_context 构造时拼 fqn
        # 走 timeout 模板（无 fqn）确认函数仍可用
        actions = suggest_actions("timeout", "his", "outpatient-register", host="x.com", port=80)
        assert actions

    def test_host_port_replaced_when_provided(self):
        actions = suggest_actions(
            "timeout", "his", "x", host="his-real.com", port=8080,
        )
        joined = " ".join(actions)
        assert "his-real.com" in joined
        assert "8080" in joined
        assert "{host}" not in joined
        assert "{port}" not in joined

    def test_host_port_default_unknown_when_not_provided(self):
        actions = suggest_actions("timeout", "his", "x")
        joined = " ".join(actions)
        # 没传 host/port → 落 UNKNOWN 占位符（避免拼出破损 shell 命令）
        assert "UNKNOWN" in joined
        assert "{host}" not in joined
        assert "{port}" not in joined

    def test_optional_placeholders_default_na_in_mtls(self):
        # mtls auth_failed 模板用 {cert_path} / {ca_path}
        actions = suggest_actions(
            "auth_failed", "his-bj", "outpatient-register",
            auth_type="mtls", host="his-bj.example.com", port=443,
            auth_env="VENDOR_HIS_BJ_CERT",
        )
        joined = " ".join(actions)
        # 缺 cert_path / ca_path 时落 N/A；不出现破损占位符
        assert "{cert_path}" not in joined
        assert "{ca_path}" not in joined

    def test_optional_placeholders_replaced_when_provided(self, full_context):
        # mtls auth_failed 模板用 {cert_path} / {ca_path}；提供完整 context 应替换
        # full_context 含 auth_type=bearer，但 mtls auth_failed 模板需要 auth_type=mtls
        # 显式覆盖一次以避免冲突
        ctx = dict(full_context)
        ctx["auth_type"] = "mtls"
        actions = suggest_actions(
            "auth_failed", "his-bj", "outpatient-register", **ctx,
        )
        joined = " ".join(actions)
        assert "/etc/ssl/his-cert.pem" in joined
        assert "/etc/ssl/his-ca.pem" in joined
        assert "{cert_path}" not in joined
        assert "{ca_path}" not in joined

    def test_start_time_end_time_replaced_when_provided(self, full_context):
        # tcp_half_closed 模板含 start_time / end_time
        actions = suggest_actions(
            "tcp_half_closed", "his", "x", **full_context,
        )
        joined = " ".join(actions)
        assert "2026-10-10T08:30:00" in joined
        assert "2026-10-10T08:30:30" in joined
        assert "{start_time}" not in joined
        assert "{end_time}" not in joined

    def test_extra_context_keys_dont_break_rendering(self):
        """caller 传未在 PLACEHOLDER_KEYS 里声明的 key 时不应抛错（兼容 layer）。"""

        # 用 hl7_ack_re 模板（不含这些 key），传任意 extras 验证不抛错
        actions = suggest_actions(
            "hl7_ack_re", "emr", "emr-submit",
            host="emr.example.com", port=6661, protocol="hl7v2",
            extra_random_key="abc12345",  # 未声明占位符
        )
        assert actions  # 不应抛错


# ---------------------------------------------------------------------------
# 失败兜底
# ---------------------------------------------------------------------------


class TestFailureFallbacks:
    def test_none_failure_class_returns_empty(self):
        assert suggest_actions(None, "his", "x") == []

    def test_empty_string_failure_class_returns_empty(self):
        assert suggest_actions("", "his", "x") == []

    def test_unknown_failure_class_returns_empty(self):
        assert suggest_actions("totally_new_class", "his", "x") == []
        assert suggest_actions("foo_bar_baz", "lis", "y") == []

    def test_legacy_compat_alias(self):
        """suggested_actions_for 旧接口（task 9）仍可用，返回非空列表。"""

        actions = suggested_actions_for("auth_failed", "his", "outpatient-register")
        assert 1 <= len(actions) <= 3
        actions_hl7 = suggested_actions_for("hl7_ack_re", "his", "x")
        assert any("hl7apy" in a.lower() for a in actions_hl7)
        # 旧接口的 placeholder_substitution 测试（test_renderers.py 既有）
        actions_t = suggested_actions_for("timeout", "his-bj", "outpatient-register")
        joined = " ".join(actions_t)
        assert "{host}" not in joined
        assert "{port}" not in joined


# ---------------------------------------------------------------------------
# 凭据纪律
# ---------------------------------------------------------------------------


# 13 类 forbidden literal（与 task 13/29 凭据守门一致）
FORBIDDEN_LITERALS = (
    "wonderland",
    "TOK-8888",
    "KEY-9999",
    "SUPER-SECRET-TOKEN",
    "ANOTHER-JWT-PAYLOAD",
    "lis-secret-token-9999",
    "pacs-secret-key-7777",
    "demo-his-token-9999",
    "demo-lis-token-8888",
    "demo-pacs-token-7777",
    "VENDOR_HIS_ACTUAL_SECRET",
    "secretpassword123",
    "federated-jwt-eyJhbGc",
)


class TestCredentialSafety:
    @pytest.mark.parametrize("failure_class", list_supported_failure_classes())
    def test_no_forbidden_literal_in_rendered_actions(self, failure_class):
        """所有 failure_class 渲染结果不含 forbidden literal 凭据。"""

        actions = suggest_actions(
            failure_class, "his", "outpatient-register",
            host="his.example.com", port=80, protocol="http",
            auth_type="bearer", auth_env="VENDOR_HIS_TOKEN",
        )
        joined = "\n".join(actions)
        for literal in FORBIDDEN_LITERALS:
            assert literal not in joined, (
                f"{failure_class} leaked forbidden literal {literal!r}: {joined!r}"
            )

    def test_credential_env_var_template_only_references_names(self):
        """模板仅引用 env 变量名，模板字面量不携带真实凭据字面量。"""

        actions = suggest_actions(
            "auth_failed", "his", "outpatient-register",
            auth_type="bearer", host="his.example.com", port=80,
        )
        joined = " ".join(actions)
        # 模板里出现的是 VENDOR_HIS_TOKEN 这种变量名
        assert "VENDOR_HIS_TOKEN" in joined
        # 不应出现真实密码字面量
        assert "Bearer mysecrettoken" not in joined
        assert "Bearer actual-password-here" not in joined

    def test_apikey_uses_placeholder_not_real_value(self):
        actions = suggest_actions(
            "auth_failed", "pacs", "x",
            auth_type="apikey", host="pacs.example.com", port=443,
            auth_env="VENDOR_PACS_APIKEY",
        )
        joined = " ".join(actions)
        # apikey 模板里写 'X-API-Key: ***' / 'api_key=***'，不含真实 key
        assert "***" in joined


# ---------------------------------------------------------------------------
# 端到端：与 ticket_jsonl 渲染器集成
# ---------------------------------------------------------------------------


class TestEndToEndWithRenderer:
    """suggest_actions 与现有 ticket_jsonl 渲染器一起使用，suggested_actions 字段非空。"""

    def test_ticket_suggested_actions_non_empty_for_fail_evidence(self):
        from vendor_if_doctor.renderers.ticket_jsonl import (
            render_tickets, suggested_actions_for,
        )
        from vendor_if_doctor.connection.evidence import Evidence

        ev = Evidence(
            summary="his.outpatient-register sample 失败 (50ms): 401",
            command="GET /api/v1/register/slot",
            latency_ms=50,
            status="fail",
            failure_class="auth_failed",
            response_body="Unauthorized",
            evidence_hash="abc12345",
            meta={"http_status": 401},
            step_name="sample",
            vendor_id="his",
            interface_id="outpatient-register",
        )
        text = render_tickets([ev])
        # JSONL 至少 1 行
        assert text.strip()
        # suggested_actions 字段非空（每条 ticket 含至少 1 条命令）
        import json as _json
        lines = [ln for ln in text.splitlines() if ln.strip()]
        assert len(lines) == 1
        ticket = _json.loads(lines[0])
        assert "suggested_actions" in ticket
        assert 1 <= len(ticket["suggested_actions"]) <= 3
        # 至少 1 条提到 VENDOR_HIS（与凭据守门一致）
        joined = " ".join(ticket["suggested_actions"])
        assert "VENDOR_HIS" in joined
        # 不含 forbidden literal
        for lit in FORBIDDEN_LITERALS:
            assert lit not in joined

    def test_ticket_suggested_actions_for_unknown_failure_class(self):
        """未知 failure_class → 工单草稿里 suggested_actions 为空列表（保守兜底）。"""

        from vendor_if_doctor.renderers.ticket_jsonl import render_tickets
        from vendor_if_doctor.connection.evidence import Evidence

        ev = Evidence(
            summary="his.x 失败 (50ms): some unknown",
            command="GET /x",
            latency_ms=50,
            status="fail",
            failure_class="totally_unknown_class",
            response_body="",
            evidence_hash="abc",
            meta={},
            step_name="sample",
            vendor_id="his",
            interface_id="x",
        )
        text = render_tickets([ev])
        import json as _json
        lines = [ln for ln in text.splitlines() if ln.strip()]
        assert len(lines) == 1
        ticket = _json.loads(lines[0])
        assert ticket["suggested_actions"] == []