"""error_pack — 失败语义分类（failure_class）。

本模块把各类异常 / 错误响应映射到统一的 ``failure_class`` 字符串，供
:class:`vendor_if_doctor.connection.evidence.Evidence` 写入。 6 个核心
``failure_class``：

* ``"http_status"`` — HTTP 4xx/5xx（非 401/403），请求结构到达对端但语义拒绝。
* ``"auth_failed"`` — HTTP 401 / 403，鉴权被拒。
* ``"timeout"`` — TCP / HTTP / MLLP 超时或连接被拒绝。
* ``"schema_mismatch"`` — JSON 解析失败 / required_fields 缺失 / 段路径错误。
* ``"payload_empty"`` — 响应体为空（HTTP 空 body / HL7 空 ACK）。
* ``"hl7_ack_re"`` — HL7 MSA-1 ≠ AA（Application Error / Application Reject）。
* ``"degraded_slow"`` — 响应成功收到但耗时 ≥ SLOW_THRESHOLD_MS（task 25 慢响应契约）；
  ``status='degraded'`` 而非 ``fail``，允许值班员在工单草稿 priority=low。

失败扩展分类（task 6/task 17/task 24 落盘）
* ``"tcp_unreachable"`` — TCP 不可达（connect refused）；
* ``"tcp_half_closed"`` — MLLP ACK 路径 ECONNRESET；
* ``"encoding_error"`` — UTF-8 / GBK / latin-1 全部解码失败；
* ``"hl7_ack_missing"`` — ACK 缺失（timeout 5s 返回 None）；
* ``"not_implemented"`` — 协议未实现占位（task 8 webservice/sql 接入）。

调用方约定
----------

* ``classify_exception(exc, response=None)`` 返回 ``str`` 类型的 failure_class；
* 分类**只看异常类型 + response.status_code**，**不**写凭据；response_text 仅在需要
  时走 :func:`vendor_if_doctor.redact.redact_dict`，本模块默认不读 body；
* HTTP 状态码在 (401, 403) 优先判 ``auth_failed``，其它 4xx/5xx 判 ``http_status``，
  2xx/3xx 调用方应在上层已判 pass，本模块只处理失败路径。
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any, Optional

from vendor_if_doctor.connection.network import PreflightError


class FailureClass(str, Enum):
    """失败语义分类枚举（与 Phase 1 的 Evidence.failure_class 字段对齐）。"""

    HTTP_STATUS = "http_status"
    AUTH_FAILED = "auth_failed"
    TIMEOUT = "timeout"
    SCHEMA_MISMATCH = "schema_mismatch"
    PAYLOAD_EMPTY = "payload_empty"
    HL7_ACK_RE = "hl7_ack_re"
    DEGRADED_SLOW = "degraded_slow"


# task 25 契约：响应总耗时（含 preflight / sample / recv）≥ 5 秒判 ``degraded_slow``。
# 该阈值与 MLLP_ACK_TIMEOUT_S 同档；保守暴露为 env 覆盖项，便于值班员在网络
# 抖动时段临时调高（用 env ``VENDOR_DOCTOR_SLOW_THRESHOLD_MS``），避免
# 一过性慢响应被错误升级到 critical。Evidence.status 同时为 ``"degraded"``，
# runner 走与 fail 同档的短路分支（first_failed_step 暴露该 step）。
SLOW_THRESHOLD_MS_DEFAULT = 5000
"""``degraded_slow`` 默认阈值（毫秒）。"""


def slow_threshold_ms(env: Optional[dict] = None) -> int:
    """返回当前生效的 slow 阈值（毫秒）。

    优先级：``env['VENDOR_DOCTOR_SLOW_THRESHOLD_MS']`` > 默认 5000。
    非法值（非整数 / 负数）落到默认。
    """

    val = None
    if env:
        val = env.get("VENDOR_DOCTOR_SLOW_THRESHOLD_MS")
    if val is None:
        try:
            import os

            val = os.environ.get("VENDOR_DOCTOR_SLOW_THRESHOLD_MS")
        except Exception:  # pragma: no cover - 防御
            val = None
    if val is None or val == "":
        return SLOW_THRESHOLD_MS_DEFAULT
    try:
        n = int(str(val).strip())
    except Exception:
        return SLOW_THRESHOLD_MS_DEFAULT
    if n < 0:
        return SLOW_THRESHOLD_MS_DEFAULT
    return n


def is_degraded_slow(latency_ms: int, env: Optional[dict] = None) -> bool:
    """判断 ``latency_ms`` 是否达到 slow 阈值（task 25 契约入口）。"""

    if latency_ms is None:
        return False
    return int(latency_ms) >= slow_threshold_ms(env)


# 便于按字符串值快速查找（如 ``FailureClass("auth_failed")`` 等价于 ``FailureClass.AUTH_FAILED``）。
_FAILURE_BY_VALUE = {fc.value: fc for fc in FailureClass}


def _safe_status_code(response: Any) -> Optional[int]:
    """从 httpx.Response / 任意对象中抽出 ``status_code``，避免抛错。"""

    if response is None:
        return None
    code = getattr(response, "status_code", None)
    if isinstance(code, int):
        return code
    return None


def classify_exception(
    exc: BaseException,
    response: Any = None,
    *,
    response_text: Optional[str] = None,
) -> str:
    """把异常 + 可选 response 映射到 :data:`FailureClass` 字符串值。

    Parameters
    ----------
    exc : BaseException
        实际抛出的异常。允许任何 ``BaseException`` 子类；非预期类型判到
        ``http_status``（保守默认，调用方可在 adapter 层改写）。
    response : optional
        可选 httpx.Response / 自定义 response 对象。本模块只读取
        ``response.status_code``；**不**读 body。
    response_text : optional
        保留字段；某些 caller 在 response_text 为空时希望映射到 ``payload_empty``。
        当前实现未消费，留作 Phase 4 中后段扩展。

    Returns
    -------
    str
        ``FailureClass.value`` 的字符串值（与 ``Evidence.failure_class`` 字段一致）。

    Notes
    -----
    调用方责任：抛到本函数的异常应已经被 :func:`vendor_if_doctor.redact.redact_dict`
    过滤（不存凭据字面量）。
    """

    # 1) 超时类（含连接超时 / PreflightError / socket.timeout）
    if isinstance(exc, PreflightError):
        # PreflightError.reason 形如 ``"socket.timeout: timed out"`` / ``"ConnectionRefusedError: [Errno 111]"``
        reason = (exc.reason or "").lower()
        if "timeout" in reason or "timed out" in reason:
            return FailureClass.TIMEOUT.value
        # PreflightError 通常都算 timeout 类（task 6 之后可能细分 tcp_unreachable）
        return FailureClass.TIMEOUT.value
    if isinstance(exc, (TimeoutError,)) or _exc_name_prefix(exc, "Timeout"):
        return FailureClass.TIMEOUT.value

    # 2) httpx.HTTPStatusError：401/403 优先判 auth_failed；其余 4xx/5xx 判 http_status
    if _is_httpx_http_status_error(exc):
        code = _safe_status_code(response) or _safe_status_code(getattr(exc, "response", None))
        if code in (401, 403):
            return FailureClass.AUTH_FAILED.value
        # 200/2xx 一般不应走到这里；保守归 http_status
        return FailureClass.HTTP_STATUS.value

    # 3) JSON 解析错误：schema_mismatch
    if isinstance(exc, json.JSONDecodeError):
        return FailureClass.SCHEMA_MISMATCH.value

    # 3.5) 编码错误：UnicodeDecodeError / LookupError（task 24 真实数据形态）
    if isinstance(exc, UnicodeDecodeError) or _exc_name_prefix(exc, "Decode"):
        return "encoding_error"

    # 4) 显式 schema 错误（如未填 required_fields 时 adapter 抛 ValueError）
    if isinstance(exc, ValueError):
        msg = (str(exc) or "").lower()
        if "required_fields" in msg or "jsonpath" in msg or "missing" in msg:
            return FailureClass.SCHEMA_MISMATCH.value
        if "empty" in msg:
            return FailureClass.PAYLOAD_EMPTY.value

    # 5) httpx.ConnectError / socket.timeout / OSError：统一 timeout
    if isinstance(exc, OSError) or _exc_name_prefix(exc, "Connect"):
        return FailureClass.TIMEOUT.value

    # 6) 默认：HTTP 状态级失败
    return FailureClass.HTTP_STATUS.value


def is_failure_class(value: str) -> bool:
    """判断一个字符串是否是 :class:`FailureClass` 已知值。"""

    return value in _FAILURE_BY_VALUE


def all_failure_classes() -> list[str]:
    """返回全部已知 failure_class 字符串列表（供工单草稿 severity 映射用）。"""

    return [fc.value for fc in FailureClass]


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _exc_name_prefix(exc: BaseException, prefix: str) -> bool:
    """跨库检测：异常类型名是否以 ``prefix`` 开头（不依赖 ``isinstance``）。"""

    cls_name = type(exc).__name__
    return cls_name.startswith(prefix)


def _is_httpx_http_status_error(exc: BaseException) -> bool:
    """检测 httpx.HTTPStatusError（包括第三方 mock 出来的同名异常）。"""

    cls_name = type(exc).__name__
    if cls_name == "HTTPStatusError":
        return True
    # 兼容 from httpx import HTTPStatusError
    try:
        import httpx  # type: ignore

        return isinstance(exc, httpx.HTTPStatusError)
    except Exception:  # pragma: no cover - 兜底
        return False


__all__ = [
    "FailureClass",
    "SLOW_THRESHOLD_MS_DEFAULT",
    "all_failure_classes",
    "classify_exception",
    "is_degraded_slow",
    "is_failure_class",
    "slow_threshold_ms",
]
