"""WebServiceConnection — WebService/SOAP 协议占位（task 8）。

Phase 4 task 8 实现
------------------

本轮**仅提供占位框架**：不真正解析 WSDL、不发起 SOAP 请求、不解析 SOAP 响应。
:class:`WebServiceConnection` 继承 :class:`BaseConnection`，5 步流水线全部返回
``status='skip'`` + ``failure_class='not_implemented'`` 的 evidence，
**不**误判为 healthy，**不**触发 short-circuit（与其他 vendor 同跑可见）。

后续 Phase（如有需要）可在此基础上扩展：

* WSDL 解析 + port binding；
* SOAP XML 消息体模板（按 operation 名称生成）；
* SOAP 响应 Fault 解析 → 失败分类；
* WS-Security username / X.509 令牌；
* MTOM / SwA 附件。

凭据纪律
--------

* 凭据仅从 env 读（``secret_env`` / ``username_env`` 等），**不**在 evidence.response_body
  写入任何 token / Authorization 字面量；
* auth 步骤在凭据缺失时输出 ``failure_class='auth_failed'`` evidence + short-circuit=True
  （与其他协议保持一致——凭据缺失是真实业务失败，不应被静默吞掉）。
"""

from __future__ import annotations

import time
from typing import List, Optional

from vendor_if_doctor.adapters.auth import AuthHeaderError, inject_auth
from vendor_if_doctor.connection.base import BaseConnection
from vendor_if_doctor.connection.evidence import Evidence
from vendor_if_doctor.connection.network import (
    PreflightError,
    preflight,
    safe_host_port,
)
from vendor_if_doctor.connection.step import Step, StepResult, make_step
from vendor_if_doctor.spec.schema import VendorInterface


# ---------------------------------------------------------------------------
# Evidence 工具
# ---------------------------------------------------------------------------


def _pass_evidence(
    interface: VendorInterface,
    step_name: str,
    summary: str,
    *,
    command: str = "",
    latency_ms: int = 0,
    meta: Optional[dict] = None,
) -> Evidence:
    return Evidence(
        summary=summary,
        command=command,
        latency_ms=latency_ms,
        status="pass",
        failure_class=None,
        step_name=step_name,
        vendor_id=interface.vendor_id,
        interface_id=interface.interface_id,
        meta=meta or {},
    )


def _skip_evidence(
    interface: VendorInterface,
    step_name: str,
    summary: str,
    *,
    failure_class: Optional[str] = "not_implemented",
    meta: Optional[dict] = None,
) -> Evidence:
    return Evidence(
        summary=summary,
        command="",
        latency_ms=0,
        status="skip",
        failure_class=failure_class,
        step_name=step_name,
        vendor_id=interface.vendor_id,
        interface_id=interface.interface_id,
        meta=meta or {},
    )


def _fail_evidence(
    interface: VendorInterface,
    step_name: str,
    summary: str,
    *,
    command: str = "",
    latency_ms: int = 0,
    failure_class: str = "auth_failed",
    meta: Optional[dict] = None,
) -> Evidence:
    return Evidence(
        summary=summary,
        command=command,
        latency_ms=latency_ms,
        status="fail",
        failure_class=failure_class,
        step_name=step_name,
        vendor_id=interface.vendor_id,
        interface_id=interface.interface_id,
        meta=meta or {},
    )


# ---------------------------------------------------------------------------
# 5 步实现（占位）
# ---------------------------------------------------------------------------


def _preflight_step(conn: "WebServiceConnection") -> StepResult:
    """preflight：复用 TCP 探活（SOAP 通常跑在 HTTP 上，端口可达即可粗粒度验证）。

    注意：WebService 协议在本轮**未**实现完整 SOAP 客户端。preflight 仅做 TCP 探活
    以验证主机可达；不发起 SOAP 请求。
    """

    interface = conn.spec
    host, port = safe_host_port(interface.host, interface.port, env=conn.env)
    start = time.monotonic()
    try:
        pf = preflight(host, port, timeout=min(1.0, interface.timeout_seconds))
        latency_ms = pf.latency_ms
        evidence = _pass_evidence(
            interface,
            "preflight",
            f"{interface.fqn} preflight OK {host}:{port} ({latency_ms}ms)",
            command=f"tcp {host}:{port}",
            latency_ms=latency_ms,
            meta={"host": host, "port": port, "preflight_ms": latency_ms},
        )
        conn._state["host"] = host
        conn._state["port"] = port
        conn._state["preflight_ms"] = latency_ms
        return StepResult(success=True, evidence=evidence)
    except PreflightError as exc:
        latency_ms = exc.latency_ms
        reason_lower = (exc.reason or "").lower()
        fc = "timeout" if ("timeout" in reason_lower or "timed out" in reason_lower) else "tcp_unreachable"
        evidence = _fail_evidence(
            interface,
            "preflight",
            f"{interface.fqn} preflight 失败 ({latency_ms}ms): {exc.reason}",
            command=f"tcp {host}:{port}",
            latency_ms=latency_ms,
            failure_class=fc,
            meta={"host": host, "port": port, "preflight_ms": latency_ms, "reason": exc.reason},
        )
        return StepResult(success=False, evidence=evidence, error=exc)


def _auth_step(conn: "WebServiceConnection") -> StepResult:
    """auth：仅校验凭据是否能在 env 中解析。WebService 常见 WS-Security username/password。

    * 凭据缺失 → failure_class='auth_failed' + short-circuit（凭据缺失是真实失败）；
    * 凭据就绪 → skip 状态（继续后续占位步骤）。
    """

    interface = conn.spec
    auth = interface.auth
    auth_type = auth.type
    start = time.monotonic()

    if auth_type in ("none", "mllp"):
        latency_ms = max(0, int((time.monotonic() - start) * 1000))
        return StepResult(
            success=True,
            evidence=_skip_evidence(
                interface, "auth",
                f"{interface.fqn} auth={auth_type} WebService 占位不实际注入凭据",
                failure_class="not_implemented",
                meta={"auth_type": auth_type, "reason": "webservice adapter is placeholder"},
            ),
        )

    # basic / bearer / apikey / mtls：仅校验 env 变量存在性，不发起请求
    try:
        headers = inject_auth({}, auth, conn.env)
    except AuthHeaderError as exc:
        latency_ms = max(0, int((time.monotonic() - start) * 1000))
        return StepResult(
            success=False,
            evidence=_fail_evidence(
                interface, "auth",
                f"{interface.fqn} auth.type={auth_type} 凭据缺失: {exc}",
                latency_ms=latency_ms,
                failure_class="auth_failed",
                meta={"auth_type": auth_type},
            ),
        )
    latency_ms = max(0, int((time.monotonic() - start) * 1000))
    return StepResult(
        success=True,
        evidence=_skip_evidence(
            interface, "auth",
            f"{interface.fqn} auth={auth_type} 凭据解析成功（占位不发起 SOAP 请求）",
            failure_class="not_implemented",
            meta={"auth_type": auth_type, "reason": "webservice adapter is placeholder"},
        ),
    )


def _list_step(conn: "WebServiceConnection") -> StepResult:
    """list：WebService 通常无标准 list 端点；返回 skip evidence。"""

    interface = conn.spec
    return StepResult(
        success=True,
        evidence=_skip_evidence(
            interface, "list",
            f"{interface.fqn} WebService 占位无 list 端点语义",
            failure_class="not_implemented",
            meta={"reason": "webservice placeholder; no list semantics"},
        ),
    )


def _sample_step(conn: "WebServiceConnection") -> StepResult:
    """sample：占位——不发起 SOAP 请求；返回 skip evidence。"""

    interface = conn.spec
    return StepResult(
        success=True,
        evidence=_skip_evidence(
            interface, "sample",
            f"{interface.fqn} WebService SOAP 客户端本轮未实现（task 8 占位）",
            failure_class="not_implemented",
            meta={
                "protocol": (interface.protocol.value if hasattr(interface.protocol, "value") else interface.protocol),
                "reason": "webservice adapter is placeholder; extend in future phase",
                "wsdl_url": f"http://{interface.host}:{interface.port}{interface.base_path}?wsdl",
            },
        ),
    )


def _assert_step(conn: "WebServiceConnection") -> StepResult:
    """assert：占位——返回 skip evidence，不校验 SOAP 响应。"""

    interface = conn.spec
    return StepResult(
        success=True,
        evidence=_skip_evidence(
            interface, "assert",
            f"{interface.fqn} WebService SOAP 响应断言本轮未实现",
            failure_class="not_implemented",
            meta={"reason": "webservice placeholder; no response assertion"},
        ),
    )


# ---------------------------------------------------------------------------
# WebServiceConnection 类
# ---------------------------------------------------------------------------


class WebServiceConnection(BaseConnection[None]):
    """WebService/SOAP 协议占位（task 8）。

    5 步流水线（preflight/auth/list/sample/assert）全部以占位形式返回
    ``status='skip'`` evidence；auth 步骤在凭据缺失时输出 ``status='fail'``
    ``failure_class='auth_failed'``（短路由其余 step，但跨接口不干扰）。

    :attr:`_client` 类型为 ``None``——本轮不构造任何 SOAP 客户端（zeep / pysimplesoap
    或 httpx + 手工 SOAP envelope）；占位时 ``_get_client`` 直接返回 ``None``。
    """

    def _get_client(self) -> None:
        return None

    def close(self) -> None:
        self._client = None
        self._state = {}

    def steps(self) -> List[Step]:
        return [
            make_step("preflight", lambda: _preflight_step(self)),
            make_step("auth", lambda: _auth_step(self)),
            make_step("list", lambda: _list_step(self)),
            make_step("sample", lambda: _sample_step(self)),
            make_step("assert", lambda: _assert_step(self)),
        ]


__all__ = ["WebServiceConnection"]


# --- 启动期注册到 factory ---
def _register_self() -> None:  # pragma: no cover - simple wiring
    from vendor_if_doctor.connection.factory import register_protocol

    register_protocol("webservice", WebServiceConnection)


_register_self()