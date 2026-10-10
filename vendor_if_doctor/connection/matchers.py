"""matchers — 语义断言原语。

为避免引入 ``jsonpath_ng`` 等额外依赖，本模块实现一个**最小 JSONPath 子集**：

* ``$`` — 根节点；
* ``$.field`` / ``$.field.subfield`` — 嵌套 key 链；
* ``$.field[0]`` / ``$.field[0].sub`` — 数组下标 + 嵌套；
* 多个 ``.field`` / ``[N]`` 自由组合；
* ``$..field`` — 递归下降（任一深度命中即返回第一个找到的 value）。

不支持：

* 过滤表达式（``?(@.foo>0)``）；
* 通配符 ``*`` / 切片 ``[0:3]``；
* 函数调用 ``length()`` 等。

如未来需要这些特性，再独立加 ``jsonpath_ng`` 依赖；本模块不挡路。

四个公开断言函数（task 4）：

* :func:`assert_http_status_in` — 检查 ``Evidence.meta['http_status']`` 在期望集合内；
* :func:`assert_jsonpath_present` — JSONPath 命中（值存在 + 非 None）；
* :func:`assert_jsonpath_equals` — JSONPath 命中且值等于期望；
* :func:`assert_hl7_msa1` — HL7 ACK MSA-1 = AA（pass）/ ≠ AA（fail，映射 ``hl7_ack_re``）。

每个函数返回 :class:`MatcherResult`，调用方写入 ``Evidence``：

.. code-block:: python

    result = assert_jsonpath_present(body, "$.data.items[0].code")
    evidence.failure_class = None if result.ok else "schema_mismatch"
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Sequence, Tuple, Union


# ---------------------------------------------------------------------------
# MatcherResult
# ---------------------------------------------------------------------------


@dataclass
class MatcherResult:
    """单次断言结果。"""

    ok: bool
    reason: str = ""
    actual: Any = None

    def to_failure_class(self) -> Optional[str]:
        """返回对应的 :data:`FailureClass` 字符串值；通过时为 ``None``。"""

        if self.ok:
            return None
        # 断言失败默认 schema_mismatch；调用方可在 expect() 中改写
        return "schema_mismatch"


__all__ = [
    "MatcherResult",
    "assert_http_status_in",
    "assert_jsonpath_present",
    "assert_jsonpath_equals",
    "assert_hl7_msa1",
    "resolve_jsonpath",
]


# ---------------------------------------------------------------------------
# HTTP 状态码断言
# ---------------------------------------------------------------------------


def assert_http_status_in(
    evidence_status: Any,
    allowed: Sequence[int],
    *,
    observed: Optional[int] = None,
) -> MatcherResult:
    """断言 HTTP 状态码在 ``allowed`` 集合内。

    Parameters
    ----------
    evidence_status : Any
        调用方可传 :class:`Evidence` 或直接 ``int``，函数优先从
        ``evidence_status.meta['http_status']`` 抽出数值；当 ``observed`` 显式给出时
        以 ``observed`` 为准。
    allowed : Sequence[int]
        期望通过的 HTTP 状态码集合（如 ``(200, 201, 204)``）。
    observed : optional
        测试 / 显式场景下直接给定状态码；优先级高于从 evidence 抽取。

    Returns
    -------
    MatcherResult
        ``ok=True`` 时 reason 空字符串；失败时 ``actual`` 是观察到的状态码。
    """

    code: Optional[int] = observed
    if code is None:
        if hasattr(evidence_status, "meta"):
            try:
                code = evidence_status.meta.get("http_status")  # type: ignore[attr-defined]
            except Exception:
                code = None
        elif isinstance(evidence_status, int):
            code = evidence_status
    if code is None:
        return MatcherResult(ok=False, reason="no http_status in evidence", actual=None)
    if code in set(allowed):
        return MatcherResult(ok=True, actual=code)
    return MatcherResult(ok=False, reason=f"http_status={code} not in {sorted(allowed)}", actual=code)


# ---------------------------------------------------------------------------
# JSONPath 解析（最小子集）
# ---------------------------------------------------------------------------


_TOKEN_RE = re.compile(r"(\[\d+\])|\.\.")


def _tokenize(path: str) -> List[str]:
    """把 JSONPath 拆成 token 序列。

    支持：

    * ``.field`` → ``"field"``
    * ``[N]`` → ``"[N]"``（保留方括号）
    * ``$..field`` → ``".."``, ``"field"``
    """

    if not path:
        raise ValueError("jsonpath 不能为空")
    if path == "$":
        return ["$"]
    if not path.startswith("$"):
        raise ValueError(f"jsonpath 必须以 '$' 开头：{path!r}")
    rest = path[1:]
    tokens: List[str] = []
    i = 0
    while i < len(rest):
        c = rest[i]
        if c == ".":
            if i + 1 < len(rest) and rest[i + 1] == ".":
                tokens.append("..")
                i += 2
                continue
            # 单点：跳过分隔符
            i += 1
            continue
        if c == "[":
            # 数组下标，形如 [0]
            m = re.match(r"\[\d+\]", rest[i:])
            if not m:
                raise ValueError(f"非法数组下标：{rest[i: i + 8]!r}")
            tokens.append(m.group(0))
            i += len(m.group(0))
            continue
        # field 名称：累积直到遇到 . / [ 结束
        j = i
        while j < len(rest) and rest[j] not in (".", "["):
            j += 1
        if j == i:
            i += 1
            continue
        tokens.append(rest[i:j])
        i = j
    return ["$", *tokens]


def resolve_jsonpath(doc: Any, path: str) -> Tuple[bool, Any]:
    """按最小 JSONPath 解析 ``doc``；返回 ``(found, value)``。

    ``found=False`` 时 ``value`` 是 ``None``。
    """

    tokens = _tokenize(path)
    return _eval_tokens(doc, tokens)


def _eval_tokens(doc: Any, tokens: List[str]) -> Tuple[bool, Any]:
    """执行 token 序列。"""

    if not tokens or tokens[0] != "$":
        return False, None

    # 简化执行：从 index=1 开始迭代
    current: List[Tuple[Any, str]] = [(doc, "root")]
    idx = 1
    while idx < len(tokens):
        tok = tokens[idx]
        next_nodes: List[Tuple[Any, str]] = []
        if tok == "..":
            # 递归下降：下一 token 应为字段名
            if idx + 1 >= len(tokens):
                # 单独的 "$.." 视为非法
                return False, None
            field = tokens[idx + 1]
            idx += 2
            for node, _trail in current:
                _collect_field_recursive(node, field, next_nodes)
        elif tok.startswith("[") and tok.endswith("]"):
            idx_num = int(tok[1:-1])
            for node, _trail in current:
                if isinstance(node, list) and -len(node) <= idx_num < len(node):
                    next_nodes.append((node[idx_num], f"{_trail}{tok}"))
            idx += 1
        else:
            # 普通 field
            for node, trail in current:
                if isinstance(node, dict) and tok in node:
                    next_nodes.append((node[tok], f"{trail}.{tok}"))
            idx += 1
        if not next_nodes:
            return False, None
        current = next_nodes
    # 取第一个结果
    value, _ = current[0]
    return True, value


def _collect_field_recursive(
    node: Any,
    field: str,
    out: List[Tuple[Any, str]],
) -> None:
    """DFS 收集所有 ``node..field`` 命中（找到第一个非 None 值就停）。"""

    if isinstance(node, dict):
        if field in node:
            out.append((node[field], f"..{field}"))
            return
        for _k, v in node.items():
            _collect_field_recursive(v, field, out)
            if out:
                return
    elif isinstance(node, list):
        for item in node:
            _collect_field_recursive(item, field, out)
            if out:
                return


# ---------------------------------------------------------------------------
# JSONPath 断言
# ---------------------------------------------------------------------------


def _parse_body(body: Union[str, bytes, dict, list, None]) -> Tuple[Optional[Any], Optional[str]]:
    """把 body 解到 Python 对象；解析失败返回 ``(None, error_msg)``。"""

    if body is None:
        return None, "empty body"
    if isinstance(body, (dict, list)):
        return body, None
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8")
        except UnicodeDecodeError:
            # 走 GBK 兜底（task 24 真实数据形态已明确）
            try:
                body = body.decode("gbk")
            except Exception:
                return None, "decode error"
    if not isinstance(body, str):
        return None, "unsupported body type"
    s = body.strip()
    if not s:
        return None, "empty body"
    # 去除 UTF-8 BOM
    if s.startswith("﻿"):
        s = s[1:]
    try:
        return json.loads(s), None
    except json.JSONDecodeError as exc:
        return None, f"JSONDecodeError: {exc.msg}"


def assert_jsonpath_present(body: Union[str, bytes, dict, list, None], path: str) -> MatcherResult:
    """断言 JSONPath 命中（存在且非 None / 非空字符串）。

    task 24 真实数据形态：业务上必填字段值为 ``""`` 等同缺失；空串与 ``null`` 一并视为
    "不可用"，返回 ``ok=False`` + reason ``'<path> is empty string or null'``，调用方
    映射 ``schema_mismatch``。
    """

    parsed, err = _parse_body(body)
    if parsed is None:
        return MatcherResult(ok=False, reason=f"body parse failed: {err}")
    found, value = resolve_jsonpath(parsed, path)
    if not found or value is None:
        return MatcherResult(ok=False, reason=f"{path} not present in body", actual=value)
    # task 24：业务空串视为缺失
    if isinstance(value, str) and value.strip() == "":
        return MatcherResult(
            ok=False,
            reason=f"{path} is empty string",
            actual=value,
        )
    return MatcherResult(ok=True, actual=value)


def assert_jsonpath_equals(
    body: Union[str, bytes, dict, list, None],
    path: str,
    expected: Any,
) -> MatcherResult:
    """断言 JSONPath 命中且值等于 ``expected``。"""

    parsed, err = _parse_body(body)
    if parsed is None:
        return MatcherResult(ok=False, reason=f"body parse failed: {err}")
    found, value = resolve_jsonpath(parsed, path)
    if not found:
        return MatcherResult(ok=False, reason=f"{path} not present in body", actual=None)
    # 用 == 比较；类型不一致也判不相等
    if value == expected:
        return MatcherResult(ok=True, actual=value)
    return MatcherResult(
        ok=False,
        reason=f"{path} expected={expected!r} got={value!r}",
        actual=value,
    )


# ---------------------------------------------------------------------------
# HL7 MSA-1 断言
# ---------------------------------------------------------------------------


def assert_hl7_msa1(ack_message: str) -> MatcherResult:
    """断言 HL7 ACK 的 MSA-1 段 = AA。

    HL7 段以 ``\\r`` (CR) 分隔；segment 内 ``|`` 分隔 fields，第 1 个 field（索引 1）
    是 ``MSA-1``。本函数**只**读取 ACK 文本，**不**调用 hl7apy（保留 task 7 主路径
    入口；matchers 仅做最小解析）。

    Parameters
    ----------
    ack_message : str
        原始 ACK 文本（已 redact，不含凭据）。

    Returns
    -------
    MatcherResult
        ``ok=True`` 当 MSA-1 == 'AA'；否则 ``reason`` 含原始 MSA-1 值。
    """

    if not ack_message or not ack_message.strip():
        return MatcherResult(ok=False, reason="empty ACK", actual=None)
    # HL7 段分隔：\r\n 或 \r；hl7apy 通常产生 \r
    segments = re.split(r"\r\n|\r|\n", ack_message)
    msa_segment = None
    for seg in segments:
        if seg.startswith("MSA"):
            msa_segment = seg
            break
    if msa_segment is None:
        return MatcherResult(ok=False, reason="no MSA segment in ACK", actual=None)
    parts = msa_segment.split("|")
    if len(parts) < 2:
        return MatcherResult(ok=False, reason="MSA segment malformed", actual=msa_segment)
    msa1 = parts[1]
    if msa1 == "AA":
        return MatcherResult(ok=True, actual=msa1)
    return MatcherResult(
        ok=False,
        reason=f"MSA-1={msa1!r} != 'AA' (AE=Application Error / AR=Application Reject)",
        actual=msa1,
    )
