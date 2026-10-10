"""HL7v2Connection — HL7v2 + MLLP over TCP 协议适配器（task 7）。

实现要点
--------

* :class:`HL7v2Connection` 继承 :class:`BaseConnection[MLLPClient]`；
* :meth:`steps` 返回 ``[preflight, auth, list, sample, assert]``：
  - **preflight** — TCP 探活 ``host:port``（复用 :func:`vendor_if_doctor.connection.network.preflight`）；
  - **auth** — MLLP 无独立凭据（仅 IP 白名单 / TCP 通道），返回 ``status='skip'`` evidence；
  - **list** — HL7 无 list 端点语义，返回 ``status='skip'`` evidence；
  - **sample** — 用 hl7apy 生成 HL7v2 消息（默认 ADT^A01），通过
    :class:`vendor_if_doctor.adapters.mllp.MLLPClient.send_and_recv` 发送并接收 ACK；
  - **assert** — 用 :func:`assert_hl7_msa1` 解析 ACK MSA-1 段（AA=pass / AE/AR=fail）。

* :meth:`_get_client` 懒构造 :class:`MLLPClient` 实例（不复用 socket 连接池 — MLLP 短连接）；

凭据纪律
--------

* HL7 MLLP 通常无独立凭据（依赖网络层 IP 白名单 + TCP 通道），所以本适配器**不**读取
  任何环境变量作为凭据；
* ACK 文本走 :func:`vendor_if_doctor.connection.runner._coarse_redact` 兜底过滤；
* evidence.evidence_hash = sha256(command + response_body) 前 16 位，**不**含 token 字面量。
"""

from __future__ import annotations

import re
import time
from typing import List, Optional

from hl7apy.parser import parse_message

from vendor_if_doctor.adapters.hl7_templates import (
    build_adt_a01,
    build_message_by_type,
)
from vendor_if_doctor.adapters.mllp import (
    MLLPClient,
    MLLPConnectError,
    MLLPError,
    MLLPFrameError,
    MLLPHalfClosedError,
)
from vendor_if_doctor.connection.base import BaseConnection
from vendor_if_doctor.connection.error_pack import (
    classify_exception,
    is_degraded_slow,
)
from vendor_if_doctor.connection.evidence import Evidence
from vendor_if_doctor.connection.matchers import assert_hl7_msa1
from vendor_if_doctor.connection.network import (
    PreflightError,
    preflight,
    safe_host_port,
)
from vendor_if_doctor.connection.runner import _coarse_redact, _evidence_hash
from vendor_if_doctor.connection.step import Step, StepResult, make_step
from vendor_if_doctor.spec.schema import (
    Hl7v2SampleEndpoint,
    VendorInterface,
)


# ---------------------------------------------------------------------------
# HL7 消息构造（task 16 接入模板库，task 7 的 _build_adt_a01 委托给 :mod:`hl7_templates`）
# ---------------------------------------------------------------------------


# 兼容 task 7 旧引用（tests/unit/test_hl7v2_mllp.py 仍导入此符号）
def _build_adt_a01(
    interface: VendorInterface,
    sample: Hl7v2SampleEndpoint,
) -> str:
    """委托给 :func:`vendor_if_doctor.adapters.hl7_templates.build_adt_a01`（task 16）。"""

    return build_adt_a01(interface, sample)


def _build_message(
    interface: VendorInterface,
    sample: Hl7v2SampleEndpoint,
) -> str:
    """按 :attr:`Hl7v2SampleEndpoint.message_type` 选择对应模板（task 16 4 类均支持）。"""

    return build_message_by_type(interface, sample)


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
    failure_class: str = "hl7_ack_re",
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


def _preflight_step(conn: "HL7v2Connection") -> StepResult:
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


def _auth_step(conn: "HL7v2Connection") -> StepResult:
    """MLLP 无独立凭据；返回 skip evidence（不影响后续步骤）。"""

    interface = conn.spec
    return StepResult(
        success=True,
        evidence=_skip_evidence(
            interface, "auth",
            f"{interface.fqn} MLLP 无独立凭据（依赖网络层 IP 白名单）",
            meta={"reason": "mllp has no per-vendor credential"},
        ),
    )


def _list_step(conn: "HL7v2Connection") -> StepResult:
    """HL7 无 list 端点语义；返回 skip evidence。"""

    interface = conn.spec
    return StepResult(
        success=True,
        evidence=_skip_evidence(
            interface, "list",
            f"{interface.fqn} HL7v2 无 list 端点语义",
            meta={"reason": "hl7 has no list endpoint"},
        ),
    )


def _sample_step(conn: "HL7v2Connection") -> StepResult:
    interface = conn.spec
    sample_ep = interface.sample_endpoint.hl7v2
    if sample_ep is None:
        return StepResult(
            success=False,
            evidence=_fail_evidence(
                interface, "sample",
                f"{interface.fqn} sample_endpoint.hl7v2 为空",
                failure_class="schema_mismatch",
                meta={"protocol": (interface.protocol.value if hasattr(interface.protocol, "value") else interface.protocol)},
            ),
        )

    host = conn._state.get("host") or safe_host_port(interface.host, interface.port, env=conn.env)[0]
    port = conn._state.get("port") or interface.port
    preflight_ms = int(conn._state.get("preflight_ms") or 0)

    # 1) 用 hl7apy 生成 HL7 消息（ADT^A01）
    try:
        hl7_msg = _build_message(interface, sample_ep)
    except Exception as exc:
        end = time.monotonic()
        latency_ms = preflight_ms + max(0, int((end - time.monotonic()) * 1000))
        return StepResult(
            success=False,
            evidence=_fail_evidence(
                interface, "sample",
                f"{interface.fqn} HL7 消息构造失败: {type(exc).__name__}: {exc}",
                command="hl7apy build",
                latency_ms=latency_ms,
                failure_class="schema_mismatch",
                meta={"exception": type(exc).__name__},
            ),
            error=exc,
        )

    command = f"MLLP {host}:{port} send ADT^A01"
    client = conn.client  # MLLPClient（懒构造）
    try:
        result = client.send_and_recv(hl7_msg)
    except MLLPHalfClosedError as exc:
        end = time.monotonic()
        latency_ms = preflight_ms + max(0, int((end - time.monotonic()) * 1000))
        return StepResult(
            success=False,
            evidence=_fail_evidence(
                interface, "sample",
                f"{interface.fqn} MLLP 半关闭 ({latency_ms}ms): {exc}",
                command=command,
                latency_ms=latency_ms,
                failure_class="tcp_half_closed",
                meta={"exception": "MLLPHalfClosedError"},
            ),
            error=exc,
        )
    except MLLPFrameError as exc:
        return StepResult(
            success=False,
            evidence=_fail_evidence(
                interface, "sample",
                f"{interface.fqn} MLLP 帧异常: {exc}",
                command=command,
                latency_ms=preflight_ms,
                failure_class="schema_mismatch",
                meta={"exception": "MLLPFrameError"},
            ),
            error=exc,
        )
    except MLLPConnectError as exc:
        latency_ms = preflight_ms
        return StepResult(
            success=False,
            evidence=_fail_evidence(
                interface, "sample",
                f"{interface.fqn} MLLP 连接失败: {exc}",
                command=command,
                latency_ms=latency_ms,
                failure_class="timeout",
                meta={"exception": "MLLPConnectError"},
            ),
            error=exc,
        )
    except MLLPError as exc:
        return StepResult(
            success=False,
            evidence=_fail_evidence(
                interface, "sample",
                f"{interface.fqn} MLLP 错误: {exc}",
                command=command,
                latency_ms=preflight_ms,
                failure_class=classify_exception(exc),
                meta={"exception": type(exc).__name__},
            ),
            error=exc,
        )
    except Exception as exc:
        return StepResult(
            success=False,
            evidence=_fail_evidence(
                interface, "sample",
                f"{interface.fqn} MLLP 异常: {type(exc).__name__}: {exc}",
                command=command,
                latency_ms=preflight_ms,
                failure_class=classify_exception(exc),
                meta={"exception": type(exc).__name__},
            ),
            error=exc,
        )

    latency_ms = preflight_ms + result.latency_ms

    # task 25 失败路径契约：响应总耗时 ≥ slow 阈值（默认 5s）→ ``degraded_slow``
    # HL7 与 HTTP 不同：ACK 收到后还要给后续 assert 步骤解析，所以此处返回
    # ``status='degraded'`` evidence，并在 ``meta['first_failed_step']='sample'`` 暴露
    # 短路源（runner 看到 status='degraded' 会触发 first_failed 短路，assert 步骤
    # 走 skip）。
    if result.ack_received and is_degraded_slow(latency_ms, env=conn.env):
        return StepResult(
            success=False,
            evidence=_fail_evidence(
                interface, "sample",
                f"{interface.fqn} HL7 ACK 收到但 {latency_ms}ms ≥ slow 阈值（降级）",
                command=command,
                latency_ms=latency_ms,
                failure_class="degraded_slow",
                meta={
                    "ack_received": True,
                    "ack_msa1": "",
                    "latency_ms": latency_ms,
                    "first_failed_step": "sample",
                },
            ),
        )

    # ACK 超时：failure_class = 'hl7_ack_missing'（不在 task 4 6 类枚举，但 Evidence.failure_class 是自由字符串）
    if not result.ack_received:
        evidence = _fail_evidence(
            interface, "sample",
            f"{interface.fqn} HL7 ACK 缺失（{latency_ms}ms 超时）",
            command=command,
            latency_ms=latency_ms,
            failure_class="hl7_ack_missing",
            meta={"ack_received": False, "timeout_s": client.timeout},
        )
        return StepResult(success=False, evidence=evidence)

    # 成功收到 ACK：写入 state 给 assert 步骤
    ack_text_raw = result.ack.decode("utf-8", errors="replace")
    ack_text = _coarse_redact(ack_text_raw)[: 32 * 1024]
    ack_msa1 = _extract_msa1(ack_text_raw) or ""
    evidence_hash = _evidence_hash(command, ack_text)
    conn._state["ack_text"] = ack_text
    conn._state["ack_msa1"] = ack_msa1
    conn._state["evidence_hash"] = evidence_hash

    return StepResult(
        success=True,
        evidence=_pass_evidence(
            interface, "sample",
            f"{interface.fqn} HL7 ACK 收到 ({latency_ms}ms, MSA-1={ack_msa1})",
            command=command,
            latency_ms=latency_ms,
            meta={
                "ack_msa1": ack_msa1,
                "ack_bytes": len(result.ack or b""),
                "evidence_hash": evidence_hash,
            },
        ),
    )


def _extract_msa1(ack_text: str) -> Optional[str]:
    """从 ACK 文本抽出 MSA-1 字段（不依赖 hl7apy，避免 hl7apy 解析严格模式抛错）。"""

    if not ack_text:
        return None
    # HL7 段分隔 \r 或 \r\n
    segments = re.split(r"\r\n|\r|\n", ack_text)
    for seg in segments:
        if seg.startswith("MSA"):
            parts = seg.split("|")
            if len(parts) >= 2 and parts[1]:
                return parts[1].strip()
    return None


def _assert_step(conn: "HL7v2Connection") -> StepResult:
    interface = conn.spec
    ack_text = conn._state.get("ack_text")
    ack_msa1 = conn._state.get("ack_msa1") or ""
    evidence_hash = conn._state.get("evidence_hash", "")

    if not ack_text:
        return StepResult(
            success=False,
            evidence=_fail_evidence(
                interface, "assert",
                f"{interface.fqn} assert 失败：sample 未产出 ACK 文本",
                failure_class="hl7_ack_missing",
                meta={"reason": "no ack_text from sample"},
            ),
        )

    match = assert_hl7_msa1(ack_text)
    if not match.ok:
        return StepResult(
            success=False,
            evidence=_fail_evidence(
                interface, "assert",
                f"{interface.fqn} HL7 MSA-1={ack_msa1} != 'AA' ({match.reason})",
                failure_class="hl7_ack_re",
                meta={
                    "ack_msa1": ack_msa1,
                    "evidence_hash": evidence_hash,
                },
            ),
        )

    # 可选：尝试解析 required_fields（如 MSH-9 / PID-3）
    missing = _check_required_hl7_fields(interface, ack_text)
    if missing:
        return StepResult(
            success=False,
            evidence=_fail_evidence(
                interface, "assert",
                f"{interface.fqn} HL7 required_fields 缺失: {'; '.join(missing)[:200]}",
                failure_class="schema_mismatch",
                meta={
                    "ack_msa1": ack_msa1,
                    "failed_required_fields": missing,
                    "evidence_hash": evidence_hash,
                },
            ),
        )

    return StepResult(
        success=True,
        evidence=_pass_evidence(
            interface, "assert",
            f"{interface.fqn} HL7 MSA-1=AA 通过（{len(interface.required_fields)} required_fields）",
            meta={
                "ack_msa1": ack_msa1,
                "checked_required_fields": list(interface.required_fields),
                "evidence_hash": evidence_hash,
            },
        ),
    )


def _check_required_hl7_fields(interface: VendorInterface, ack_text: str) -> List[str]:
    """检查 HL7 required_fields 在 ACK 中是否齐备（缺失返回字段名列表）。

    HL7 段路径：``MSH-9`` / ``PID-3`` / ``PV1-7``；第一个字符是段名，``-N`` 是字段索引。
    """

    missing: List[str] = []
    if not interface.required_fields:
        return missing
    try:
        parsed = parse_message(ack_text, validation_level=2)
    except Exception:
        # 解析失败：不阻断 msa1=AA 的成功证据
        return missing
    for path in interface.required_fields:
        seg = path.split("-")[0]
        try:
            seg_obj = getattr(parsed, seg.lower())
            idx = int(path.split("-")[1])
            comp = getattr(seg_obj, f"{seg.lower()}_{idx}")
            value = comp.to_er7() if comp.value else None
        except Exception:
            value = None
        if value is None or value == "":
            missing.append(path)
    return missing


# ---------------------------------------------------------------------------
# HL7v2Connection 类
# ---------------------------------------------------------------------------


class HL7v2Connection(BaseConnection[MLLPClient]):
    """HL7v2 + MLLP over TCP 协议适配器（task 7 落地）。"""

    def _get_client(self) -> MLLPClient:
        host = self._state.get("host") or self.spec.host
        port = self._state.get("port") or self.spec.port
        return MLLPClient(host, port, timeout=self.spec.timeout_seconds)

    def close(self) -> None:
        # MLLPClient 是短连接单 socket，无需关闭
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


__all__ = ["HL7v2Connection"]


# --- 启动期注册到 factory（避免 factory 在 import 阶段 import adapters.hl7v2 形成循环依赖）---
def _register_self() -> None:  # pragma: no cover - simple wiring
    from vendor_if_doctor.connection.factory import register_protocol

    register_protocol("hl7v2", HL7v2Connection)


_register_self()
