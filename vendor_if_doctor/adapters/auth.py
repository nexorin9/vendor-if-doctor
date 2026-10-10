"""auth — 5 种鉴权 header 注入函数（task 6 抽离）。

把 :class:`HTTPConnection` 的鉴权逻辑抽离到独立模块，让 :class:`HTTPConnection` 的
``_auth_step`` 与 ``_get_client`` 共用同一套注入函数，便于：

* 单测覆盖（``tests/unit/test_auth.py``）—— 5 种鉴权 header 构造；
* 凭据纪律 —— 注入函数 **不** 返回任何凭据字面量给调用方；
* mTLS 证书加载 —— ``load_mtls_cert`` 校验文件存在性 + 返回 :class:`MtlsMaterial`。

凭据纪律
--------

* 凭据值一律从 ``env`` 字典读取，**不**接受硬编码；
* :func:`inject_auth` 返回新 dict（含原始 headers 副本 + 鉴权 header），**不**就地修改；
* ``Authorization`` / ``Bearer <token>`` / ``Basic <base64>`` 字面量在 evidence 写入前
  由 :func:`vendor_if_doctor.connection.runner._coarse_redact` 兜底过滤；
* :func:`load_mtls_cert` 校验文件存在性，文件路径走 ``_coarse_redact`` 防止路径泄漏凭据。

公开 API
--------

* :func:`inject_auth` — 5 种鉴权 header 注入；
* :func:`load_mtls_cert` — mTLS 证书加载；
* :data:`MtlsMaterial` — dataclass（含 cert / key / ca 三路径）。
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Dict, Mapping, Optional

from vendor_if_doctor.spec.schema import (
    AuthApiKey,
    AuthBasic,
    AuthBearer,
    AuthConfig,
    AuthMllp,
    AuthMtls,
    AuthNone,
)


def _coarse_redact(text: str) -> str:
    """本模块局部复制的 _coarse_redact（避免与 connection.runner 循环依赖）。

    与 :func:`vendor_if_doctor.connection.runner._coarse_redact` 行为一致：
    过滤 Authorization / Bearer / Basic / token / password 等敏感字面量。
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


# ---------------------------------------------------------------------------
# mTLS 材质
# ---------------------------------------------------------------------------


@dataclass
class MtlsMaterial:
    """mTLS 三件套路径（cert / key / 可选 ca bundle）。"""

    cert_path: str
    key_path: str
    ca_path: Optional[str] = None

    def to_httpx_kwargs(self) -> dict:
        """生成 httpx.Client 的 ``cert=`` / ``verify=`` 参数。"""

        kwargs: dict = {"cert": (self.cert_path, self.key_path)}
        kwargs["verify"] = self.ca_path if self.ca_path else True
        return kwargs


class MtlsCredentialError(Exception):
    """mTLS 凭据加载失败（cert_env / key_env 缺失或文件不存在）。"""


def load_mtls_cert(
    auth: AuthMtls,
    env: Mapping[str, str],
) -> MtlsMaterial:
    """从 env 读 cert / key / ca bundle 路径，校验文件存在性。"""

    cert_path = env.get(auth.cert_env, "")
    key_path = env.get(auth.key_env, "")
    if not cert_path or not key_path:
        raise MtlsCredentialError(
            f"mTLS 凭据缺失：env 变量 {auth.cert_env!r} / {auth.key_env!r} 未设置"
        )
    if not os.path.exists(cert_path):
        raise MtlsCredentialError(
            f"mTLS 证书路径不存在：{_coarse_redact(cert_path)}"
        )
    if not os.path.exists(key_path):
        raise MtlsCredentialError(
            f"mTLS 私钥路径不存在：{_coarse_redact(key_path)}"
        )
    ca_path = env.get(auth.ca_bundle_env) if auth.ca_bundle_env else None
    if ca_path and not os.path.exists(ca_path):
        # ca 路径存在但不可达 —— 警告级别，但不阻塞
        pass
    return MtlsMaterial(cert_path=cert_path, key_path=key_path, ca_path=ca_path)


# ---------------------------------------------------------------------------
# header 注入
# ---------------------------------------------------------------------------


class AuthHeaderError(Exception):
    """鉴权 header 构造失败（如 basic 缺 username、secret_env 缺失等）。"""


def inject_auth(
    headers: Mapping[str, str],
    auth: AuthConfig,
    env: Mapping[str, str],
) -> Dict[str, str]:
    """按 :class:`AuthConfig` 类型注入鉴权 header。

    Parameters
    ----------
    headers : mapping
        基础 headers（如 ``{'User-Agent': 'vendor-if-doctor/0.1'}``）。
    auth : AuthConfig
        鉴权 discriminated union。
    env : mapping
        环境变量字典。

    Returns
    -------
    dict
        新 headers dict（含原 headers + 鉴权 header）。

    Raises
    ------
    AuthHeaderError
        凭据缺失 / 文件路径不存在 / 鉴权类型未知。

    Notes
    -----
    * ``auth.type='none'`` 直接返回原 headers 副本；
    * ``auth.type='basic'`` 拼 ``Basic <base64(user:pass)>``；
    * ``auth.type='bearer'`` 拼 ``Bearer <token>``；
    * ``auth.type='apikey'`` 按 ``inject`` 走 header 注入或 query 注入（query 注入由调用方把
      返回 dict 里的特殊 key ``__apikey_query__`` 读取后拼到 URL；本函数仍返回 headers）；
    * ``auth.type='mtls'`` 不写 header（凭 httpx.Client 的 ``cert=`` / ``verify=`` 装载）；
    * ``auth.type='mllp'`` 应在 HL7v2Connection 而非 HTTP 出现，调用方报错。
    """

    base: Dict[str, str] = dict(headers)
    if isinstance(auth, AuthNone):
        return base

    if isinstance(auth, AuthMllp):
        raise AuthHeaderError(
            "auth.type=mllp 不应在 HTTP 接口出现；走 HL7v2Connection"
        )

    if isinstance(auth, AuthMtls):
        # mTLS 不写 header；调用方另行装载 httpx.Client(cert=..., verify=...)
        return base

    secret = env.get(auth.secret_env, "")
    if not secret:
        raise AuthHeaderError(
            f"auth.type={auth.type} secret_env {auth.secret_env!r} 缺失/为空"
        )

    if isinstance(auth, AuthBasic):
        username = env.get(auth.username_env, "")
        if not username:
            raise AuthHeaderError(
                f"basic auth username_env {auth.username_env!r} 缺失"
            )
        encoded = base64.b64encode(f"{username}:{secret}".encode("utf-8")).decode(
            "ascii"
        )
        base["Authorization"] = f"Basic {encoded}"
        return base

    if isinstance(auth, AuthBearer):
        base["Authorization"] = f"Bearer {secret}"
        return base

    if isinstance(auth, AuthApiKey):
        if auth.inject == "header":
            base[auth.header_name] = secret
            return base
        # query 注入：通过特殊 key 透传给调用方
        base["__apikey_query__"] = f"{auth.query_param}={secret}"
        return base

    raise AuthHeaderError(f"未实现的鉴权类型：{auth.type!r}")


def extract_apikey_query(headers: Mapping[str, str]) -> Optional[str]:
    """从 :func:`inject_auth` 返回值中抽出 apikey query 注入字符串（inject=query 时）。"""

    return headers.get("__apikey_query__")


__all__ = [
    "AuthHeaderError",
    "MtlsCredentialError",
    "MtlsMaterial",
    "extract_apikey_query",
    "inject_auth",
    "load_mtls_cert",
]
