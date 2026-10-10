"""response_decode — 真实数据形态解析（task 24）。

现实接口响应里会出现 6 类棘手形态：

1. **生僻字 / CJK 扩展 B** — 字段值含 ``U+20000-U+2FAFF`` 区段（如 ``李𠮷王``）。
   Python ``str`` 与 JSON 自然保留，但 shell 终端、CSV 导出、Markdown 渲染会丢字；
2. **GBK 编码** — 部分老 HIS/LIS 仅返回 GBK bytes，httpx 默认 ``utf-8`` 解码会抛
   ``UnicodeDecodeError``；
3. **超长字段 > 32KB** — 病历文书、检验报告常含整段长文；直接落库会爆 SQLite；
4. **必填字段空字符串** — ``"patient_id": ""`` 与缺失语义不同；
5. **必填字段 null** — JSON ``null`` 解析后是 ``None``；
6. **BOM + 重复 key** — 部分老接口响应以 ``\\xef\\xbb\\xbf`` 开头；同一 key 重复出现
   Python ``json.loads`` 默认保留**最后一个**值，但其他 vendor 把第一个当主索引。

本模块统一提供：

* :func:`decode_response_bytes` — bytes → str；UTF-8 → GBK → latin-1 兜底；
* :func:`strip_bom` — 剥离 UTF-8 / UTF-16 / UTF-32 BOM；
* :func:`parse_body_with_dup_keys` — JSON 解析 + 重复 key 检测；
* :func:`truncate_body` — 32KB 截断（与 Phase 1 closure_dimensions 对齐）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union


__all__ = [
    "BodyDecodeResult",
    "decode_response_bytes",
    "strip_bom",
    "parse_body_with_dup_keys",
    "truncate_body",
    "MAX_RESPONSE_BYTES",
]


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------


MAX_RESPONSE_BYTES = 32 * 1024
"""Evidence.response_body 截断上限（与 spec.md closure_dimensions 一致）。"""


# UTF-8/16/32 BOM 字节
_BOM_UTF8 = b"\xef\xbb\xbf"
_BOM_UTF16_LE = b"\xff\xfe"
_BOM_UTF16_BE = b"\xfe\xff"
_BOM_UTF32_LE = b"\xff\xfe\x00\x00"
_BOM_UTF32_BE = b"\x00\x00\xfe\xff"


@dataclass
class BodyDecodeResult:
    """响应体解码结果。

    Fields
    ------

    * ``text`` — 解码后的字符串（已剥离 BOM）；
    * ``encoding`` — 实际使用的编码（``utf-8`` / ``gbk`` / ``latin-1``）；
    * ``bom_stripped`` — 是否剥离过 BOM；
    * ``truncated`` — 是否被截断到 :data:`MAX_RESPONSE_BYTES`；
    * ``raw_size`` — 截断前的字节数（用于 Evidence.meta）；
    * ``parsed`` — JSON 解析结果（dict / list / 标量 / 解析失败为 ``None``）；
    * ``parse_error`` — JSON 解析失败的简短描述（成功时为 ``None``）；
    * ``duplicate_keys`` — 检测到的重复 key 列表（同一 key 出现多次记一次）。
    """

    text: str = ""
    encoding: str = "utf-8"
    bom_stripped: bool = False
    truncated: bool = False
    raw_size: int = 0
    parsed: Any = None
    parse_error: Optional[str] = None
    duplicate_keys: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 编码 / BOM / 截断
# ---------------------------------------------------------------------------


def strip_bom(data: bytes) -> Tuple[bytes, bool]:
    """剥离 UTF-8/16/32 BOM；返回 ``(data_without_bom, bom_stripped)``。"""

    for bom in (_BOM_UTF8, _BOM_UTF32_LE, _BOM_UTF32_BE, _BOM_UTF16_LE, _BOM_UTF16_BE):
        if data.startswith(bom):
            # UTF-8 BOM 长度 3；UTF-16/32 BOM 长度 4（含 \x00）
            return data[len(bom):], True
    return data, False


def _decode_bytes(data: bytes) -> Tuple[str, str]:
    """把 bytes 解到 str（utf-8 → gbk → latin-1 兜底）。

    返回 ``(text, encoding_name)``。``latin-1`` 永远成功（每个字节都合法），
    作为最后兜底，**仅**当 utf-8 / gbk 都失败时启用。
    """

    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("gbk"), "gbk"
    except (UnicodeDecodeError, LookupError):
        pass
    # latin-1 永远不会失败
    return data.decode("latin-1"), "latin-1"


def decode_response_bytes(data: Union[bytes, str, None]) -> BodyDecodeResult:
    """把 httpx ``resp.content`` / ``resp.text`` 解为标准形态。

    * bytes → utf-8 → gbk → latin-1 兜底；
    * 剥离 UTF-8 / UTF-16 / UTF-32 BOM；
    * 截断到 :data:`MAX_RESPONSE_BYTES`（按字符数截断，避免破坏多字节字符）；
    * 同时尝试 JSON 解析（``parse_body_with_dup_keys``），结果放 ``parsed`` /
      ``parse_error`` / ``duplicate_keys``。
    """

    result = BodyDecodeResult()
    if data is None:
        result.parse_error = "empty body"
        return result
    if isinstance(data, str):
        # 已经解码；按 bytes 走 utf-8 → gbk 一致性也补一次（兜底）
        raw_bytes = data.encode("utf-8", errors="replace")
    else:
        raw_bytes = data
    result.raw_size = len(raw_bytes)
    stripped, bom_stripped = strip_bom(raw_bytes)
    result.bom_stripped = bom_stripped
    text, encoding = _decode_bytes(stripped)
    result.text = text
    result.encoding = encoding
    if len(text) > MAX_RESPONSE_BYTES:
        result.text = text[:MAX_RESPONSE_BYTES]
        result.truncated = True
    # 顺手解析 JSON（含重复 key 检测）
    if result.text:
        parsed, err, dups = parse_body_with_dup_keys(result.text)
        result.parsed = parsed
        result.parse_error = err
        result.duplicate_keys = dups
    return result


def truncate_body(text: str, max_bytes: int = MAX_RESPONSE_BYTES) -> Tuple[str, bool]:
    """按字符数截断到 ``max_bytes``；返回 ``(truncated_text, was_truncated)``。"""

    if len(text) <= max_bytes:
        return text, False
    return text[:max_bytes], True


# ---------------------------------------------------------------------------
# JSON 解析 + 重复 key 检测
# ---------------------------------------------------------------------------


def _detect_duplicate_keys(
    obj_or_pairs: Union[Any, List[Tuple[str, Any]]],
    duplicates_out: List[str],
) -> Any:
    """``object_pairs_hook`` — 检测 dict 重复 key 并构造结果。"""

    if not isinstance(obj_or_pairs, list):
        return obj_or_pairs
    seen: Dict[str, Any] = {}
    duplicates: List[str] = []
    for k, v in obj_or_pairs:
        if k in seen:
            if k not in duplicates:
                duplicates.append(k)
            # Python json.loads 默认保留最后一个值；保留该语义
        seen[k] = v
    if duplicates:
        duplicates_out.extend(duplicates)
    return seen


def parse_body_with_dup_keys(text: str) -> Tuple[Any, Optional[str], List[str]]:
    """解析 JSON 并检测重复 key。

    返回 ``(parsed, error_msg_or_None, duplicate_keys_list)``。

    * ``parsed`` 解析失败时为 ``None``；
    * ``error_msg`` 成功时 ``None``；
    * 重复 key 列表按出现顺序去重。
    """

    s = text.strip()
    if not s:
        return None, "empty body", []
    duplicates: List[str] = []
    try:
        parsed = json.loads(s, object_pairs_hook=lambda pairs: _detect_duplicate_keys(pairs, duplicates))
    except json.JSONDecodeError as exc:
        return None, f"JSONDecodeError: {exc.msg}", []
    return parsed, None, duplicates


# ---------------------------------------------------------------------------
# 便捷：bytes / str → BodyDecodeResult（含 JSON 解析）
# ---------------------------------------------------------------------------


def parse_response(data: Union[bytes, str, None]) -> BodyDecodeResult:
    """一站式：解码 → 剥离 BOM → 截断 → 解析 JSON（含重复 key 检测）。

    返回的 :class:`BodyDecodeResult` 含 ``parsed`` / ``parse_error`` /
    ``duplicate_keys``；调用方按需写到 ``Evidence.meta``。
    """

    result = decode_response_bytes(data)
    if result.text and not result.parse_error:
        parsed, err, dups = parse_body_with_dup_keys(result.text)
        result.parsed = parsed
        result.parse_error = err
        result.duplicate_keys = dups
    return result