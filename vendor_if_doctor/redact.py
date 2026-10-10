"""凭据脱敏工具 — vendor-if-doctor 全模块共享。

设计要点
--------

* **7 类敏感字段名**（大小写不敏感）：``Authorization`` / ``X-API-Key`` /
  ``api_key`` / ``password`` / ``secret`` / ``token`` / ``access_token`` /
  ``bearer`` / ``credential``。
* **递归过滤**：dict 中所有 secret-ish key 的 value 替换为 ``"***"``；list 递归；
  嵌套 dict 递归；标量 str 走字符串级 regex 兜底。
* **字符串级 regex 兜底**：JSON field / URL query / 行内 ``Authorization: Bearer``
  等；与 renderers/_coarse_redact 风格一致（**单一真源**）。
* **不动其他模块**：本模块不读 env、不发起请求、不依赖其他 vendor_if_doctor
  子模块（避免循环依赖）；其他模块需要脱敏时 ``from vendor_if_doctor.redact
  import redact_dict``。

不在本模块发起网络请求；仅做脱敏。
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional


# ---------------------------------------------------------------------------
# 敏感字段名 / 模式（与 renderers / cli.config 一致）
# ---------------------------------------------------------------------------

# 9 类敏感 key 名（大小写不敏感）；key 命中时整 value 替换为 "***"
_SECRET_KEY_RE = re.compile(
    r"(?i)^(authorization|x-api-key|api[_-]?key|password|secret|token|access[_-]?token|bearer|credential)$"
)

# 字符串级兜底 pattern（覆盖 9 类常见凭据字面量形态）
_REDACT_STR_PATTERNS: list[tuple[str, str]] = [
    # (1) 行内 Authorization: Bearer xyz / Authorization: Basic xyz
    (
        r"(?im)(authorization\s*[:=]\s*)(?:bearer|basic)?\s*[^\s,;\"'\r\n]+",
        r"\1***",
    ),
    # (2) Bearer xyz（裸出现）
    (r"(?i)(\bbearer\s+)[^\s,;\"'\r\n]+", r"\1***"),
    # (3) Basic xyz（裸出现）
    (r"(?i)(\bbasic\s+)[^\s,;\"'\r\n]+", r"\1***"),
    # (4) JSON field: "token": "xyz" / "password": "xyz"
    (
        r'(?i)("(?:token|password|api[_-]?key|authorization|secret|x-api-key|access_token|credential)"\s*:\s*)"[^"]*"',
        r'\1"***"',
    ),
    # (5) URL query: ?api_key=xxx &token=yyy（注意不吞后接参数）
    (
        r"(?i)([\?&](?:api[_-]?key|access[_-]?token|token|password)\s*=\s*)([^&\s=]+)",
        r"\1***",
    ),
    # (6) 标量 token=xxx（无 & 边界）
    (r"(?i)(\btoken\s*=\s*)[^\s,;\"'\r\n&]+", r"\1***"),
    # (7) 标量 password=xxx
    (r"(?i)(\bpassword\s*=\s*)[^\s,;\"'\r\n&]+", r"\1***"),
    # (8) 行内 X-API-Key: xxx / x-api-key=xxx（无 Bearer/Basic 修饰的敏感 header）
    (
        r"(?i)(x-api-key\s*[:=]\s*)(?:bearer|basic)?\s*[^\s,;\"'\r\n]+",
        r"\1***",
    ),
    # (9) 行内 password: xxx / token: xxx（裸字段名 + 冒号 + 值，常见于日志/配置 dump）
    (
        r"(?i)((?:password|secret|access_token)\s*:\s*)[^\s,;\"'\r\n&]+",
        r"\1***",
    ),
]


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------


def redact_str(text: Optional[str]) -> Optional[str]:
    """字符串级敏感字段过滤。

    命中以上 7 类 regex 时，把字段值（Authorization/Bearer/Basic 后跟的 token；
    JSON 字段值；URL query 值；标量 token=/password= 后续值）替换为 ``***``。

    ``None`` / 空串直接返回原值（不抛错、不打日志）。
    """

    if not text:
        return text
    out = text
    for pat, repl in _REDACT_STR_PATTERNS:
        out = re.sub(pat, repl, out)
    return out


def redact_dict(obj: Any) -> Any:
    """递归过滤 dict / list 中的所有 secret-ish key。

    * dict 中 key 匹配 ``_SECRET_KEY_RE`` 的，value 整替换为 ``"***"``；
    * dict 中其他 key 的 value 递归；
    * list 中元素递归；
    * str 类型走 :func:`redact_str` 兜底；
    * 非 dict/list/str 标量（int / float / bool / None / bytes）原样返回。
    """

    if isinstance(obj, dict):
        out: dict = {}
        for k, v in obj.items():
            if isinstance(k, str) and _SECRET_KEY_RE.match(k):
                out[k] = "***"
            else:
                out[k] = redact_dict(v)
        return out
    if isinstance(obj, list):
        return [redact_dict(v) for v in obj]
    if isinstance(obj, str):
        return redact_str(obj)
    # tuple / set / bytes / 标量保持不变
    return obj


def redact_env_for_log(env_name: str, value: Optional[str]) -> str:
    """打印 env 时只显示名字 + 长度；值永远替换为 '***'。"""

    if value is None or value == "":
        return f"{env_name}=<empty>"
    return f"{env_name}=*** (len={len(value)})"


def is_secret_key(key: Any) -> bool:
    """判断 dict key 是否在敏感字段名集合内（供调用方做精细判断）。"""

    return isinstance(key, str) and bool(_SECRET_KEY_RE.match(key))


def redact_env_map(env: Mapping[str, Optional[str]]) -> dict[str, str]:
    """批量把 env dict 转为 ``name=*** (len=N)`` 形式。"""

    out: dict[str, str] = {}
    for k, v in env.items():
        out[k] = redact_env_for_log(k, v)
    return out


__all__ = [
    "is_secret_key",
    "redact_dict",
    "redact_env_for_log",
    "redact_env_map",
    "redact_str",
]
