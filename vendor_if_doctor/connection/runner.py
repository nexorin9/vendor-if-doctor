"""runner — 巡检执行入口。

Phase 4 task 5 实现
------------------

* :func:`test_one(spec)` — 兼容旧入口（task 3 单步 HTTP happy path，**不**变）；
* :func:`test_connection_steps(spec, steps, *, env)` — 多步调度器：按 ``steps`` 顺序
  执行各 step；首个失败 step 之前产生的 evidence + 该失败 evidence 一并返回；
  同一接口里失败 step 之后的 step 输出 ``status='skip'`` evidence；继续处理
  spec 中其他 interface；
* :func:`test_connection(spec, *, env)` — 默认走 5 步流程（preflight / auth / list /
  sample / assert）。

短路策略
--------

* **每条 interface 内部**：首个失败 step 之后的 step 输出 ``status='skip'`` evidence
  并附 ``meta={'skipped_due_to': <failed_step_name>}``，便于人工一眼看清断点位置。
* **跨 interface**：除非 spec 显式 ``fail_fast=True``（task 11 才完整暴露，现阶段
  保留扩展位），否则跑完所有 interface。
* **未实现协议**：vendor 的 ``failure_class='not_implemented'`` 不触发 short-circuit
  （``success=True``），既告知值班员"还得接入"又不阻塞其他 vendor。

凭据纪律
--------

* 所有 evidence.response_body 在写入前先过 :func:`_coarse_redact` 兜底；
* evidence_hash = sha256(command + response_body) 前 16 位；
* ``authorization`` / token / password 字段一律不进 SQLite / Markdown / JSONL。
"""

from __future__ import annotations

import hashlib
import os
from typing import List, Optional

# 阻止 pytest 把 ``test_one`` / ``test_connection`` / ``test_connection_steps``
# 误识别为测试函数而要求 ``spec`` fixture —— 它们是公开 API。
__test__ = False

from vendor_if_doctor.connection.error_pack import classify_exception
from vendor_if_doctor.connection.evidence import Evidence
from vendor_if_doctor.connection.factory import build_connection
from vendor_if_doctor.connection.network import preflight, safe_host_port
from vendor_if_doctor.connection.step import STEP_NAMES
from vendor_if_doctor.spec.schema import (
    HttpSampleEndpoint,
    Protocol,
    VendorInterface,
    VendorSpec,
)


# ---------------------------------------------------------------------------
# 内部工具：redact（task 18 才完整实现；这里先做兜底）
# ---------------------------------------------------------------------------


def _coarse_redact(text: str) -> str:
    """兜底 redact：移除常见 Authorization / Bearer / token 字面量。

    不替代 :func:`vendor_if_doctor.redact.redact_dict`，只用于 evidence 拼装前
    安全网，避免凭据泄漏到 SQLite / Markdown / JSONL。
    """

    if not text:
        return text
    import re

    patterns = [
        (r"(?im)^(\s*(?:authorization|x-api-key|api[_-]?key|password)\s*[:=]\s*).+$", r"\1***"),
        (r"(?i)(bearer\s+)[^\s,;\"'\r\n]+", r"\1***"),
        (r"(?i)(basic\s+)[^\s,;\"'\r\n]+", r"\1***"),
        (r'(?i)("(?:token|password|api[_-]?key|authorization|secret)"\s*:\s*)"[^"]*"', r'\1"***"'),
        (r"(?i)(\btoken\s*=\s*)[^\s,;\"'\r\n]+", r"\1***"),
        (r"(?i)(\bpassword\s*=\s*)[^\s,;\"'\r\n]+", r"\1***"),
    ]
    out = text
    for pat, repl in patterns:
        out = re.sub(pat, repl, out)
    return out


def _evidence_hash(command: str, response_body: str) -> str:
    h = hashlib.sha256()
    h.update(command.encode("utf-8"))
    h.update(b"\x00")
    h.update(response_body.encode("utf-8", errors="replace"))
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# test_one — 单步 HTTP happy path（task 3 兼容入口）
# ---------------------------------------------------------------------------


def _resolve_credentials(spec: VendorInterface, env: dict) -> dict:
    """从 env 解出鉴权 header（5 种鉴权通用）。task 5 仅用做 ``test_one``。"""

    headers: dict = {}
    auth = spec.auth
    auth_type = auth.type
    if auth_type == "none":
        return headers
    if auth_type == "basic":
        import base64

        username = env.get(auth.username_env, "")
        password = env.get(auth.secret_env, "")
        creds = f"{username}:{password}".encode("utf-8")
        token = base64.b64encode(creds).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
        return headers
    if auth_type == "bearer":
        token = env.get(auth.secret_env, "")
        headers["Authorization"] = f"Bearer {token}"
        return headers
    if auth_type == "apikey":
        token = env.get(auth.secret_env, "")
        if auth.inject == "header":
            headers[auth.header_name] = token
        else:
            import warnings

            warnings.warn(
                f"apikey inject=query 在 test_one 最小 HTTP 未实现；忽略",
                stacklevel=2,
            )
        return headers
    if auth_type == "mtls":
        import warnings

        warnings.warn(
            f"mtls 在 test_one 最小 HTTP 未实现；交由 task 6 接入",
            stacklevel=2,
        )
        return headers
    if auth_type == "mllp":
        return headers
    return headers


def _http_get_sample(
    spec: VendorInterface,
    env: dict,
    *,
    http_client=None,
) -> Evidence:
    """最小 HTTP GET sample_endpoint 单步（兼容 ``test_one``）。"""

    import time

    if spec.sample_endpoint.http is None:
        return Evidence(
            summary=f"{spec.fqn} protocol={spec.protocol} 非 HTTP，跳过 sample",
            command="",
            latency_ms=0,
            status="skip",
            failure_class=None,
            step_name="sample",
            vendor_id=spec.vendor_id,
            interface_id=spec.interface_id,
        )

    http_ep: HttpSampleEndpoint = spec.sample_endpoint.http
    host, port = safe_host_port(spec.host, spec.port, env=env)
    headers = _resolve_credentials(spec, env)
    headers.update(http_ep.headers or {})

    start_pre = time.monotonic()
    try:
        pf = preflight(host, port, timeout=min(1.0, spec.timeout_seconds))
        preflight_ms = pf.latency_ms
    except Exception as exc:
        end_pre = time.monotonic()
        latency_ms = max(0, int((end_pre - start_pre) * 1000))
        return Evidence(
            summary=f"{spec.fqn} preflight 失败 ({latency_ms}ms): {exc.reason}",
            command=f"tcp {host}:{port}",
            latency_ms=latency_ms,
            status="fail",
            failure_class="timeout" if "timeout" in exc.reason.lower() else "tcp_unreachable",
            step_name="preflight",
            vendor_id=spec.vendor_id,
            interface_id=spec.interface_id,
            meta={"reason": _coarse_redact(exc.reason)},
        )

    url = f"http://{host}:{port}{spec.base_path.rstrip('/')}{http_ep.path}"
    command = f"GET {url}"
    start_req = time.monotonic()
    close_client = False
    if http_client is None:
        import httpx

        http_client = httpx.Client(timeout=spec.timeout_seconds)
        close_client = True
    try:
        resp = http_client.request(
            http_ep.method,
            url,
            headers=headers,
            content=http_ep.body or None,
        )
        end_req = time.monotonic()
        latency_ms = preflight_ms + max(0, int((end_req - start_req) * 1000))
        body = resp.text[: 32 * 1024] if resp.text else ""
        body_redacted = _coarse_redact(body)
        http_status = resp.status_code
        ok = 200 <= http_status < 400
        return Evidence(
            summary=(
                f"{spec.fqn} HTTP {http_status} {latency_ms}ms"
                if ok
                else f"{spec.fqn} HTTP {http_status} {latency_ms}ms (failed)"
            ),
            command=command,
            latency_ms=latency_ms,
            status="pass" if ok else "fail",
            failure_class=None if ok else "http_status",
            response_body=body_redacted,
            evidence_hash=_evidence_hash(command, body_redacted),
            meta={"http_status": http_status, "preflight_ms": preflight_ms},
            step_name="sample",
            vendor_id=spec.vendor_id,
            interface_id=spec.interface_id,
        )
    except Exception as exc:
        end_req = time.monotonic()
        latency_ms = preflight_ms + max(0, int((end_req - start_req) * 1000))
        return Evidence(
            summary=f"{spec.fqn} GET 失败 ({latency_ms}ms): {type(exc).__name__}: {exc}",
            command=command,
            latency_ms=latency_ms,
            status="fail",
            failure_class="timeout" if "timeout" in str(exc).lower() else "http_error",
            step_name="sample",
            vendor_id=spec.vendor_id,
            interface_id=spec.interface_id,
            meta={"exception": type(exc).__name__},
        )
    finally:
        if close_client:
            try:
                http_client.close()
            except Exception:  # pragma: no cover
                pass


def test_one(spec: VendorSpec, *, env: Optional[dict] = None) -> Evidence:
    """单步 HTTP happy path 入口（task 3 兼容）。

    spec.interfaces 取第一条 HTTP 接口；如无 HTTP 接口返回 ``status='skip'`` evidence。
    """

    env = env if env is not None else dict(os.environ)
    for itf in spec.interfaces:
        if itf.protocol == Protocol.HTTP.value:
            return _http_get_sample(itf, env)
    return Evidence(
        summary="spec.interfaces 中无 HTTP 接口",
        command="",
        latency_ms=0,
        status="skip",
        failure_class=None,
        step_name="sample",
    )


# ---------------------------------------------------------------------------
# test_connection_steps — 多步调度器（task 5 核心入口）
# ---------------------------------------------------------------------------


def _emit_skip_evidence(
    interface: VendorInterface,
    step_name: str,
    failed_step: str,
    latency_ms: int = 0,
) -> Evidence:
    """短路后续 step 时输出的 ``status='skip'`` evidence。

    ``meta['skipped_due_to']`` 暴露首个失败的 step 名字，handover 渲染与值班员一眼
    就能找到断点位置。
    """

    return Evidence(
        summary=(
            f"{interface.fqn} step={step_name} 跳过（前序 step={failed_step} 失败）"
        ),
        command="",
        latency_ms=latency_ms,
        status="skip",
        failure_class=None,
        step_name=step_name,
        vendor_id=interface.vendor_id,
        interface_id=interface.interface_id,
        meta={"skipped_due_to": failed_step},
    )


def test_connection_steps(
    spec: VendorSpec,
    steps: Optional[List[str]] = None,
    *,
    env: Optional[dict] = None,
) -> List[Evidence]:
    """多步调度器：按 ``steps`` 顺序跑 :attr:`VendorSpec.interfaces` 每条接口。

    Parameters
    ----------
    spec : VendorSpec
        顶层 vendor 规格（含多条 :class:`VendorInterface`）。
    steps : list of str, optional
        要跑的 step 名字列表（默认全部 ``STEP_NAMES``，即
        ``('preflight','auth','list','sample','assert')``）。
    env : dict, optional
        环境变量字典。

    Returns
    -------
    list of Evidence
        所有接口、所有 step 的 evidence 列表。**包含失败后跳过的 step**
        （``status='skip'`` + ``meta['skipped_due_to']``），便于 handover 渲染。

    Notes
    -----
    * 失败 step 在 evidence ``status='fail'``；同一接口后续 step 输出 ``status='skip'``；
      不触发 ``fail_fast`` 短路跨接口（默认跑完所有 interface）。
    * 未实现协议（``failure_class='not_implemented'``）的 evidence 标为 ``success=True``，
      不会触发本接口的短路，便于值班员看到全部 vendor 的状态而非一个未实现的 vendor
      卡住后续接口。
    """

    requested = list(steps) if steps is not None else list(STEP_NAMES)
    env = env if env is not None else dict(os.environ)

    accumulated: List[Evidence] = []

    for interface in spec.interfaces:
        conn = build_connection(interface, env=env)
        try:
            first_failed: Optional[str] = None
            # 把流水线裁剪到只跑 caller 指定的 step（保持顺序）
            pipeline = conn.steps()
            for name in requested:
                step_obj = next((s for s in pipeline if s.name == name), None)
                if step_obj is None:
                    # 当前协议不暴露这个 step（占位 protocol 仅有 preflight），跳过
                    continue

                if first_failed is not None:
                    accumulated.append(
                        _emit_skip_evidence(interface, name, first_failed)
                    )
                    continue

                try:
                    result = step_obj.run()
                except Exception as exc:
                    failure_class = classify_exception(
                        exc,
                        getattr(exc, "response", None),
                    )
                    evidence = Evidence(
                        summary=(
                            f"{interface.fqn} step={name} raised "
                            f"{type(exc).__name__}: {_coarse_redact(str(exc))[:120]}"
                        ),
                        command="",
                        latency_ms=0,
                        status="fail",
                        failure_class=failure_class,
                        step_name=name,
                        vendor_id=interface.vendor_id,
                        interface_id=interface.interface_id,
                        meta={
                            "exception": type(exc).__name__,
                            "exception_message": _coarse_redact(str(exc))[:200],
                        },
                    )
                    result.evidence = evidence  # type: ignore[attr-defined]
                    result.success = False

                # 同步失败标记
                if result.evidence.status in ("fail", "degraded") or not result.success:
                    first_failed = name
                accumulated.append(result.evidence)
        finally:
            conn.close()

    return accumulated


def test_connection(
    spec: VendorSpec,
    *,
    env: Optional[dict] = None,
) -> List[Evidence]:
    """默认走 5 步流程（``test_connection_steps`` 默认 ``steps=STEP_NAMES``）。"""

    return test_connection_steps(spec, list(STEP_NAMES), env=env)


__all__ = ["STEP_NAMES", "test_connection", "test_connection_steps", "test_one"]
