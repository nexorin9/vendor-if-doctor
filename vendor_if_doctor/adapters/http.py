"""HTTPConnection — HTTP/HTTPS 协议适配器（task 6 完整实现）。

Phase 4 task 6 实现
------------------

* :class:`HTTPConnection` 继承 :class:`BaseConnection[httpx.Client]`；
* :meth:`steps` 返回 ``[preflight, auth, list, sample, assert]`` 真实流水线：
  - **preflight** — TCP 探活 ``host:port``；不可达时 ``status='fail'``，
    ``failure_class='timeout'`` 或 ``'tcp_unreachable'``（按 reason 区分），``latency_ms < 1500``；
  - **auth** — 5 种鉴权（``none`` / ``basic`` / ``bearer`` / ``apikey`` / ``mtls``）
    走 :func:`vendor_if_doctor.adapters.auth.inject_auth`；mTLS 走
    :func:`vendor_if_doctor.adapters.auth.load_mtls_cert` 校验文件可达性；
  - **list** — 当 :attr:`HttpSampleEndpoint.list_endpoint` 配置时，GET list 端点；
    支持 ``page_param`` / ``page_size_param`` 分页；否则 ``status='skip'`` evidence；
  - **sample** — 复用 httpx.Client 共享连接池；按 status code 走 :func:`classify_exception`
    实现 ``401 / 403 → auth_failed`` 与 ``5xx → http_status``；响应体 32KB 截断；
  - **assert** — :func:`assert_http_status_in` (200/201/204) + 每个
    :attr:`required_fields` 的 :func:`assert_jsonpath_present`；
* :meth:`test_one_http` — 单步 happy path（task 3 兼容入口）；
* :meth:`_get_client` — mTLS 装载 ``cert=(cert,key)`` + ``verify=ca_bundle``；其他走普通
  共享 httpx.Client（连接池复用）。

凭据纪律
--------

* :func:`vendor_if_doctor.adapters.auth.inject_auth` 仅从 env 读 secret_env / username_env；
* ``_coarse_redact`` 兜底过滤 evidence 写入前的 Authorization / Bearer / token 字面量；
* ``auth_header`` 在 :attr:`BaseConnection._state` 仅作为请求构造引用，**不**写 evidence；
* preflight 阶段不读任何凭据；auth 阶段仅校验 env 变量名是否存在，不打印 env 变量值；
* HTTP 401/403 经 :func:`classify_exception` 映射到 ``failure_class='auth_failed'``。
"""

from __future__ import annotations

import os
import time
from typing import List, Optional

import httpx

from vendor_if_doctor.adapters.auth import (
    AuthHeaderError,
    MtlsCredentialError,
    extract_apikey_query,
    inject_auth,
    load_mtls_cert,
)
from vendor_if_doctor.adapters.response_decode import (
    MAX_RESPONSE_BYTES,
    BodyDecodeResult,
    decode_response_bytes,
    parse_response,
)
from vendor_if_doctor.connection.base import BaseConnection
from vendor_if_doctor.connection.error_pack import (
    classify_exception,
    is_degraded_slow,
)
from vendor_if_doctor.connection.evidence import Evidence
from vendor_if_doctor.connection.matchers import (
    assert_http_status_in,
    assert_jsonpath_present,
)
from vendor_if_doctor.connection.network import (
    PreflightError,
    preflight,
    safe_host_port,
)
from vendor_if_doctor.connection.runner import _coarse_redact, _evidence_hash
from vendor_if_doctor.connection.step import Step, StepResult, make_step
from vendor_if_doctor.spec.schema import (
    HttpListEndpoint,
    VendorInterface,
)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


_ALLOWED_HTTP_STATUS = (200, 201, 204)
"""默认 assert 步骤允许的 HTTP 状态码。"""


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
    meta: Optional[dict] = None,
) -> Evidence:
    return Evidence(
        summary=summary,
        command="",
        latency_ms=0,
        status="skip",
        failure_class=None,
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
    failure_class: str = "http_status",
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
# 5 步实现
# ---------------------------------------------------------------------------


def _preflight_step(conn: "HTTPConnection") -> StepResult:
    interface = conn.spec
    host, port = safe_host_port(interface.host, interface.port, env=conn.env)
    start = time.monotonic()
    try:
        pf = preflight(host, port, timeout=min(1.0, interface.timeout_seconds))
        latency_ms = pf.latency_ms
        end = time.monotonic()
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
        # timeout / unreachable 由 reason 关键词区分
        reason_lower = (exc.reason or "").lower()
        if "timeout" in reason_lower or "timed out" in reason_lower:
            fc = "timeout"
        else:
            fc = "tcp_unreachable"
        evidence = _fail_evidence(
            interface,
            "preflight",
            f"{interface.fqn} preflight 失败 ({latency_ms}ms): {_coarse_redact(exc.reason)}",
            command=f"tcp {host}:{port}",
            latency_ms=latency_ms,
            failure_class=fc,
            meta={
                "host": host,
                "port": port,
                "preflight_ms": latency_ms,
                "reason": _coarse_redact(exc.reason),
            },
        )
        return StepResult(success=False, evidence=evidence, error=exc)


def _auth_step(conn: "HTTPConnection") -> StepResult:
    """5 种鉴权 header 注入（task 6 抽离到 adapters/auth.py）。

    失败映射：
      * 凭据缺失 → ``failure_class='auth_failed'``；
      * mTLS 文件不存在 → ``failure_class='auth_failed'``；
      * auth.type=mllp（HTTP 不应出现）→ ``failure_class='auth_failed'``。
    """

    interface = conn.spec
    auth = interface.auth
    auth_type = auth.type
    start = time.monotonic()

    if auth_type == "mtls":
        try:
            mtls = load_mtls_cert(auth, conn.env)
        except MtlsCredentialError as exc:
            latency_ms = max(0, int((time.monotonic() - start) * 1000))
            return StepResult(
                success=False,
                evidence=_fail_evidence(
                    interface, "auth",
                    f"{interface.fqn} mTLS 失败: {exc}",
                    latency_ms=latency_ms,
                    failure_class="auth_failed",
                    meta={"auth_type": "mtls"},
                ),
            )
        conn._state["mtls_cert"] = mtls.cert_path
        conn._state["mtls_key"] = mtls.key_path
        conn._state["mtls_ca"] = mtls.ca_path
        latency_ms = max(0, int((time.monotonic() - start) * 1000))
        return StepResult(
            success=True,
            evidence=_pass_evidence(
                interface, "auth",
                f"{interface.fqn} auth=mtls 证书解析成功",
                latency_ms=latency_ms,
                meta={"auth_type": "mtls"},
            ),
        )

    # none / basic / bearer / apikey / mllp 均走 inject_auth
    try:
        headers = inject_auth({}, auth, conn.env)
    except AuthHeaderError as exc:
        latency_ms = max(0, int((time.monotonic() - start) * 1000))
        return StepResult(
            success=False,
            evidence=_fail_evidence(
                interface, "auth",
                f"{interface.fqn} auth.type={auth_type} 失败: {exc}",
                latency_ms=latency_ms,
                failure_class="auth_failed",
                meta={"auth_type": auth_type},
            ),
        )

    # 提取 apikey query 注入（如有）
    apikey_q = extract_apikey_query(headers)
    if apikey_q is not None:
        conn._state["apikey_query"] = apikey_q
        headers.pop("__apikey_query__", None)

    # 写 _state['auth_header'] 作为 sample 步骤构造请求用（不含 token 原值）
    if "Authorization" in headers:
        conn._state["auth_header"] = ("Authorization", headers["Authorization"])
    # apikey header 注入：找非标准 header 名（X-API-Key 等）
    standard_headers = {"Authorization", "User-Agent", "Accept", "Content-Type"}
    for k, v in headers.items():
        if k not in standard_headers:
            conn._state["auth_header"] = (k, v)
            break

    latency_ms = max(0, int((time.monotonic() - start) * 1000))
    if auth_type == "none":
        msg = f"{interface.fqn} auth=none 跳过"
    else:
        msg = f"{interface.fqn} auth={auth_type} 凭据已解析（不打印原值）"
    return StepResult(
        success=True,
        evidence=_pass_evidence(
            interface, "auth", msg,
            latency_ms=latency_ms,
            meta={"auth_type": auth_type},
        ),
    )


def _list_step(conn: "HTTPConnection") -> StepResult:
    """list 步骤（task 6 接入）：GET sample_endpoint.list_endpoint，支持分页参数。"""

    interface = conn.spec
    http_ep = interface.sample_endpoint.http
    if http_ep is None or http_ep.list_endpoint is None:
        return StepResult(
            success=True,  # skip != fail，不短路
            evidence=_skip_evidence(
                interface, "list",
                f"{interface.fqn} list 步骤未配置 list_endpoint，跳过",
                meta={"reason": "no list_endpoint configured"},
            ),
        )

    list_ep: HttpListEndpoint = http_ep.list_endpoint
    return _http_get(
        conn,
        list_ep.method,
        list_ep.path,
        extra_headers=list_ep.headers or {},
        list_ep=list_ep,
    )


def _sample_step(conn: "HTTPConnection") -> StepResult:
    """sample 步骤：GET sample_endpoint + required_fields JSONPath 断言（前置 assert 步骤）。"""

    interface = conn.spec
    http_ep = interface.sample_endpoint.http
    if http_ep is None:
        return StepResult(
            success=False,
            evidence=_fail_evidence(
                interface, "sample",
                f"{interface.fqn} sample_endpoint.http 为空，HTTP 接口缺失",
                failure_class="schema_mismatch",
                meta={"protocol": (interface.protocol.value if hasattr(interface.protocol, "value") else interface.protocol)},
            ),
        )
    return _http_get(
        conn,
        http_ep.method,
        http_ep.path,
        extra_headers=http_ep.headers or {},
        request_body=http_ep.body,
    )


def _http_get(
    conn: "HTTPConnection",
    method: str,
    path: str,
    *,
    extra_headers: dict,
    request_body: str = "",
    list_ep: Optional[HttpListEndpoint] = None,
) -> StepResult:
    """通用 HTTP GET 路径（sample 与 list 共用）。

    list_ep 非空时为 list 步骤；否则为 sample 步骤。
    失败映射走 :func:`classify_exception` 以保证 401/403 → auth_failed。
    """

    interface = conn.spec
    step_name = "list" if list_ep is not None else "sample"

    host = conn._state.get("host") or safe_host_port(interface.host, interface.port, env=conn.env)[0]
    port = conn._state.get("port") or interface.port
    base_path = (interface.base_path or "/").rstrip("/") or ""
    url_path = path

    # apikey query 注入（来自 auth 步骤）
    apikey_q = conn._state.get("apikey_query")
    if apikey_q:
        sep = "&" if "?" in url_path else "?"
        url_path = f"{url_path}{sep}{apikey_q}"

    # list 分页参数
    if list_ep is not None and list_ep.page_param:
        sep = "&" if "?" in url_path else "?"
        url_path = f"{url_path}{sep}{list_ep.page_param}=1"
    if list_ep is not None and list_ep.page_size_param and list_ep.page_size:
        sep = "&" if "?" in url_path else "?"
        url_path = f"{url_path}{sep}{list_ep.page_size_param}={list_ep.page_size}"

    url = f"http://{host}:{port}{base_path}{url_path}"
    command = f"{method} {url}"

    headers: dict = dict(extra_headers or {})
    auth_hdr = conn._state.get("auth_header")
    if auth_hdr is not None:
        name, value = auth_hdr  # type: ignore[misc]
        headers[name] = value

    preflight_ms = int(conn._state.get("preflight_ms") or 0)
    start_req = time.monotonic()
    try:
        client = conn.client  # shared httpx.Client
        resp = client.request(
            method,
            url,
            headers=headers,
            content=request_body or None,
        )
        end_req = time.monotonic()
        latency_ms = preflight_ms + max(0, int((end_req - start_req) * 1000))
        # task 24：用 resp.content (bytes) 走 UTF-8 → GBK → latin-1 兜底
        # 替代 resp.text 单编码解码；保留 httpx 内部 encoding hint 但不被其限制。
        decoded: BodyDecodeResult = parse_response(resp.content)
        body_truncated = decoded.text
        truncated = decoded.truncated
        body_redacted = _coarse_redact(body_truncated)
        evidence_hash = _evidence_hash(command, body_redacted)
        http_status = resp.status_code
        ok = 200 <= http_status < 400

        # 写共享 state，给 assert 步骤读
        conn._state["http_status"] = http_status
        conn._state["response_body"] = body_redacted
        conn._state["evidence_hash"] = evidence_hash

        if ok:
            # 同时尝试解析 JSON（assert 步骤需要 dict）
            parsed: Optional[object] = decoded.parsed
            parse_err: Optional[str] = decoded.parse_error
            duplicate_keys: List[str] = list(decoded.duplicate_keys or [])
            conn._state["parsed_body"] = parsed

            if step_name == "list":
                # list 步骤额外校验 expected_count_min；并把 actual_count 写入 meta
                count = _extract_count(parsed)
                if list_ep and list_ep.expected_count_min is not None:
                    if count is None or count < list_ep.expected_count_min:
                        return StepResult(
                            success=False,
                            evidence=_evidence_with_body(
                                interface, step_name,
                                f"{interface.fqn} list 条数 {count} < 期望 {list_ep.expected_count_min}",
                                command=command,
                                latency_ms=latency_ms,
                                http_status=http_status,
                                body=body_redacted,
                                evidence_hash=evidence_hash,
                                preflight_ms=preflight_ms,
                                truncated=truncated,
                                status="fail",
                                failure_class="schema_mismatch",
                                extra_meta={
                                    "expected_count_min": list_ep.expected_count_min,
                                    "actual_count": count,
                                    "encoding": decoded.encoding,
                                    "bom_stripped": decoded.bom_stripped,
                                    "raw_size": decoded.raw_size,
                                    "duplicate_keys": duplicate_keys,
                                    "parse_error": parse_err,
                                },
                            ),
                        )
                extra_meta_pass: Optional[dict] = (
                    {"actual_count": count} if count is not None else None
                )
                summary = f"{interface.fqn} list HTTP {http_status} {latency_ms}ms"
            else:
                summary = f"{interface.fqn} HTTP {http_status} {latency_ms}ms"
                extra_meta_pass = None
            # 真实数据形态：encoding / bom / dup_keys 写 meta（不会泄漏凭据）
            extra_meta_pass = dict(extra_meta_pass or {})
            extra_meta_pass.update(
                {
                    "encoding": decoded.encoding,
                    "bom_stripped": decoded.bom_stripped,
                    "raw_size": decoded.raw_size,
                    "duplicate_keys": duplicate_keys,
                    "parse_error": parse_err,
                }
            )

            # task 25 失败路径契约：响应耗时 ≥ slow 阈值（默认 5s）→ ``degraded_slow``
            # status='degraded'（非 fail，但 runner 同样触发 first_failed_step 短路）。
            # 仅当响应**成功**（2xx）时考虑 slow；4xx/5xx 走失败路径不重写。
            if is_degraded_slow(latency_ms, env=getattr(conn, "env", None)):
                slow_meta = dict(extra_meta_pass)
                slow_meta["first_failed_step"] = step_name
                return StepResult(
                    success=False,
                    evidence=_evidence_with_body(
                        interface, step_name,
                        f"{interface.fqn} {method} 响应 {latency_ms}ms（≥slow 阈值，降级）",
                        command=command,
                        latency_ms=latency_ms,
                        http_status=http_status,
                        body=body_redacted,
                        evidence_hash=evidence_hash,
                        preflight_ms=preflight_ms,
                        truncated=truncated,
                        status="degraded",
                        failure_class="degraded_slow",
                        extra_meta=slow_meta,
                    ),
                )

            return StepResult(
                success=True,
                evidence=_evidence_with_body(
                    interface, step_name, summary,
                    command=command,
                    latency_ms=latency_ms,
                    http_status=http_status,
                    body=body_redacted,
                    evidence_hash=evidence_hash,
                    preflight_ms=preflight_ms,
                    truncated=truncated,
                    extra_meta=extra_meta_pass,
                ),
            )
        else:
            # 非 2xx：构造 httpx.HTTPStatusError 替身以走 classify_exception（401/403 → auth_failed）
            import httpx as _httpx

            err = _httpx.HTTPStatusError(
                f"HTTP {http_status}", request=resp.request, response=resp
            )
            failure_class = classify_exception(err, resp)
            summary = f"{interface.fqn} HTTP {http_status} {latency_ms}ms (non-2xx)"
            return StepResult(
                success=False,
                evidence=_evidence_with_body(
                    interface, step_name, summary,
                    command=command,
                    latency_ms=latency_ms,
                    http_status=http_status,
                    body=body_redacted,
                    evidence_hash=evidence_hash,
                    preflight_ms=preflight_ms,
                    truncated=truncated,
                    status="fail",
                    failure_class=failure_class,
                    extra_meta={
                        "encoding": decoded.encoding,
                        "bom_stripped": decoded.bom_stripped,
                        "raw_size": decoded.raw_size,
                        "duplicate_keys": list(decoded.duplicate_keys or []),
                        "parse_error": decoded.parse_error,
                    },
                ),
                error=err,
            )
    except Exception as exc:
        end_req = time.monotonic()
        latency_ms = preflight_ms + max(0, int((end_req - start_req) * 1000))
        resp_obj = getattr(exc, "response", None)
        failure_class = classify_exception(exc, resp_obj)
        return StepResult(
            success=False,
            evidence=_fail_evidence(
                interface, step_name,
                f"{interface.fqn} {method} 异常 ({latency_ms}ms): "
                f"{type(exc).__name__}: {_coarse_redact(str(exc))[:120]}",
                command=command,
                latency_ms=latency_ms,
                failure_class=failure_class,
                meta={
                    "exception": type(exc).__name__,
                    "exception_message": _coarse_redact(str(exc))[:200],
                    "http_status": getattr(resp_obj, "status_code", None),
                },
            ),
            error=exc,
        )


def _extract_count(parsed: Optional[object]) -> Optional[int]:
    """从 list 步骤响应中抽出条数。

    支持的形态：
      * list 直接 → ``len(list)``；
      * dict 含 ``data`` / ``items`` / ``records`` / ``rows`` 数组 → 该数组长度；
      * dict 含 ``total`` / ``count`` / ``total_count`` 整数 → 直接用整数。
    """

    if parsed is None:
        return None
    if isinstance(parsed, list):
        return len(parsed)
    if isinstance(parsed, dict):
        for key in ("data", "items", "records", "rows", "list"):
            v = parsed.get(key)
            if isinstance(v, list):
                return len(v)
        for key in ("total", "count", "total_count"):
            v = parsed.get(key)
            if isinstance(v, int):
                return v
    return None


def _evidence_with_body(
    interface: VendorInterface,
    step_name: str,
    summary: str,
    *,
    command: str,
    latency_ms: int,
    http_status: int,
    body: str,
    evidence_hash: str,
    preflight_ms: int,
    truncated: bool,
    status: str = "pass",
    failure_class: Optional[str] = None,
    extra_meta: Optional[dict] = None,
) -> Evidence:
    """sample/list 步骤专用 evidence 构造器。"""

    meta = {
        "http_status": http_status,
        "preflight_ms": preflight_ms,
        "truncated": truncated,
        "evidence_hash": evidence_hash,
    }
    if extra_meta:
        meta.update(extra_meta)
    return Evidence(
        summary=summary,
        command=command,
        latency_ms=latency_ms,
        status=status,  # type: ignore[arg-type]
        failure_class=failure_class,
        response_body=body,
        evidence_hash=evidence_hash,
        step_name=step_name,
        vendor_id=interface.vendor_id,
        interface_id=interface.interface_id,
        meta=meta,
    )


def _assert_step(conn: "HTTPConnection") -> StepResult:
    interface = conn.spec
    http_status = conn._state.get("http_status")
    parsed_body = conn._state.get("parsed_body")
    evidence_hash = conn._state.get("evidence_hash", "")
    latency_ms = 0  # assert 自身快速，仅做语义校验

    if http_status is None:
        return StepResult(
            success=False,
            evidence=_fail_evidence(
                interface, "assert",
                f"{interface.fqn} assert 失败：sample 未产出 http_status",
                latency_ms=latency_ms,
                failure_class="schema_mismatch",
                meta={"reason": "no http_status from sample"},
            ),
        )

    # 1) HTTP 状态码断言
    status_match = assert_http_status_in(int(http_status), _ALLOWED_HTTP_STATUS)
    if not status_match.ok:
        return StepResult(
            success=False,
            evidence=_fail_evidence(
                interface, "assert",
                f"{interface.fqn} HTTP {http_status} 不在 {list(_ALLOWED_HTTP_STATUS)}: {status_match.reason}",
                latency_ms=latency_ms,
                failure_class="http_status",
                meta={
                    "http_status": int(http_status),
                    "allowed": list(_ALLOWED_HTTP_STATUS),
                    "evidence_hash": evidence_hash,
                },
            ),
        )

    # 2) required_fields JSONPath 断言
    failures: List[str] = []
    fields = list(interface.required_fields or [])
    for path in fields:
        present = assert_jsonpath_present(parsed_body, path)
        if not present.ok:
            failures.append(f"{path}: {present.reason}")

    if failures:
        return StepResult(
            success=False,
            evidence=_fail_evidence(
                interface, "assert",
                f"{interface.fqn} required_fields 缺失: {'; '.join(failures)[:200]}",
                latency_ms=latency_ms,
                failure_class="schema_mismatch",
                meta={
                    "http_status": int(http_status),
                    "failed_required_fields": fields,
                    "evidence_hash": evidence_hash,
                },
            ),
        )

    return StepResult(
        success=True,
        evidence=_pass_evidence(
            interface, "assert",
            f"{interface.fqn} 全部通过（HTTP {http_status}, "
            f"{len(fields)} required_fields）",
            latency_ms=latency_ms,
            meta={
                "http_status": int(http_status),
                "checked_required_fields": fields,
                "evidence_hash": evidence_hash,
            },
        ),
    )


# ---------------------------------------------------------------------------
# HTTPConnection 类
# ---------------------------------------------------------------------------


class HTTPConnection(BaseConnection[httpx.Client]):
    """HTTP/HTTPS 协议连接（task 6 完整 5 步 + 5 种 auth + list 分页）。"""

    def _get_client(self) -> httpx.Client:
        # mTLS 装载
        mtls_cert = self._state.get("mtls_cert")
        mtls_key = self._state.get("mtls_key")
        mtls_ca = self._state.get("mtls_ca")
        if mtls_cert and mtls_key:
            try:
                return httpx.Client(
                    timeout=self.spec.timeout_seconds,
                    follow_redirects=False,
                    cert=(mtls_cert, mtls_key),
                    verify=mtls_ca if mtls_ca else True,
                )
            except Exception:
                # 加载失败回退普通客户端
                pass
        return httpx.Client(
            timeout=self.spec.timeout_seconds,
            follow_redirects=False,
        )

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # pragma: no cover
                pass
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

    def test_one_http(self) -> Evidence:
        """最小 GET sample_endpoint；task 3 验收入口（在 task 5 中由 sample_step 复用）。"""

        return _sample_step(self).evidence


__all__ = ["HTTPConnection"]


# --- 启动期注册到 factory（避免 factory 在 import 阶段 import adapters.http 形成循环依赖） ---
# factory 已经在 import 阶段准备好 ``_REGISTRY``；此处 HTTPConnection 模块加载完成
# 时主动注册自身，保证 ``build_connection`` 能找到。
def _register_self() -> None:  # pragma: no cover - simple wiring
    from vendor_if_doctor.connection.factory import register_protocol

    if "http" not in register_protocol.__globals__.get("_REGISTRY", {}):
        register_protocol("http", HTTPConnection)


_register_self()
