"""vendor-if-doctor 规格 pydantic 模型。

* :class:`VendorSpec` — 顶层模型（包含多个 :class:`VendorInterface`）。
* :class:`VendorInterface` — 单条接口规格。
* :class:`AuthConfig` — 鉴权 discriminated union（按 ``type`` 字段路由）。
* :class:`HttpSampleEndpoint` / :class:`Hl7v2SampleEndpoint` — 协议 sample 端点。

约定：
* 凭据相关字段名（``secret_env`` / ``username_env`` / ``password_env`` 等）只允许写
  **环境变量名**，绝对不允许内嵌明文。如果在加载后发现 ``secret_env`` 缺失，立即抛
  :class:`SpecValidationError` 并终止加载，**不**发起任何请求。
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Protocol(str, Enum):
    """支持的协议枚举。

    内置 4 条（HTTP / HL7v2 / WebService / SQL）；插件机制（task 33）允许在启动期
    通过 :func:`vendor_if_doctor.connection.plugin.register_protocol_via_plugin`
    追加新协议名（记录在 :data:`_PLUGIN_PROTOCOLS`）。
    """

    HTTP = "http"
    HL7V2 = "hl7v2"
    WEBSERVICE = "webservice"
    SQL = "sql"


_PLUGIN_PROTOCOLS: List[str] = []


def is_plugin_protocol(name: str) -> bool:
    """查询 ``name`` 是否已被插件注册（不区分大小写）。"""

    if not name:
        return False
    key = name.strip().lower()
    return key in _PLUGIN_PROTOCOLS


def _add_extra_protocol_enum_members(extra: List[str]) -> None:
    """把插件协议名记录在 :class:`Protocol` 的"插件协议"列表里。

    由 :mod:`vendor_if_doctor.connection.plugin` 在插件模块 import 时调用。
    Python 原生 Enum 不支持动态添加成员，因此 :class:`VendorInterface` 的
    ``protocol`` 字段使用 :func:`_validate_protocol_value` 自定义 validator，
    接受内置枚举值 + 已注册的插件协议字符串。
    """

    for name in extra:
        key = (name or "").strip().lower()
        if not key:
            continue
        if key in {p.value for p in Protocol}:
            continue
        if key in _PLUGIN_PROTOCOLS:
            continue
        _PLUGIN_PROTOCOLS.append(key)


def _set_extra_protocol_enum(extra: List[str]) -> None:
    """把插件协议列表裁到 ``extra``（**仅供测试重置用**）。
    """

    global _PLUGIN_PROTOCOLS
    cleaned: List[str] = []
    for name in extra:
        key = (name or "").strip().lower()
        if key and key not in {p.value for p in Protocol} and key not in cleaned:
            cleaned.append(key)
    _PLUGIN_PROTOCOLS = cleaned


def _validate_protocol_value(value):
    """校验 ``protocol`` 字段值 — 内置枚举成员 + 已注册的插件协议字符串均合法。

    返回归一化（小写）后的字符串值。供 :class:`VendorInterface` 的
    ``field_validator("protocol")`` 调用；非字符串/未知值抛 ``ValueError``。
    """

    if isinstance(value, Protocol):
        return value.value
    if not isinstance(value, str):
        raise ValueError(f"protocol 必须是字符串或 Protocol 枚举，得到 {type(value).__name__}")
    key = value.strip().lower()
    if not key:
        raise ValueError("protocol 不能为空字符串")
    if key in {p.value for p in Protocol}:
        return key
    if key in _PLUGIN_PROTOCOLS:
        return key
    raise ValueError(
        f"protocol={key!r} 不在支持的协议列表中 "
        f"(内置: {[p.value for p in Protocol]}; "
        f"已注册插件: {_PLUGIN_PROTOCOLS or '无'})"
    )


# --- 鉴权 ---------------------------------------------------------------------


class _AuthBase(BaseModel):
    """鉴权配置基类。"""

    model_config = ConfigDict(extra="forbid", frozen=False)


class AuthNone(_AuthBase):
    type: Literal["none"] = "none"


class AuthBasic(_AuthBase):
    type: Literal["basic"] = "basic"
    username_env: str = Field(..., description="basic auth 用户名环境变量名")
    secret_env: str = Field(..., description="basic auth 密码环境变量名")
    realm: Optional[str] = Field(None, description="basic auth realm（可选）")


class AuthBearer(_AuthBase):
    type: Literal["bearer"] = "bearer"
    secret_env: str = Field(..., description="bearer token 环境变量名")


class AuthApiKey(_AuthBase):
    type: Literal["apikey"] = "apikey"
    secret_env: str = Field(..., description="API key 环境变量名")
    inject: Literal["header", "query"] = Field("header", description="注入位置")
    header_name: str = Field("X-API-Key", description="注入 header 名（inject=header）")
    query_param: str = Field("api_key", description="注入 query 参数名（inject=query）")


class AuthMtls(_AuthBase):
    type: Literal["mtls"] = "mtls"
    cert_env: str = Field(..., description="mTLS 客户端证书路径环境变量名")
    key_env: str = Field(..., description="mTLS 客户端私钥路径环境变量名")
    ca_bundle_env: Optional[str] = Field(None, description="CA bundle 路径环境变量名")


class AuthMllp(_AuthBase):
    """MLLP over TCP 通常无独立凭据，仅靠 MLLP 通道 IP 白名单；保留扩展位。"""

    type: Literal["mllp"] = "mllp"


AuthConfig = Annotated[
    Union[AuthNone, AuthBasic, AuthBearer, AuthApiKey, AuthMtls, AuthMllp],
    Field(discriminator="type"),
]


# --- sample endpoint ----------------------------------------------------------


class HttpListEndpoint(BaseModel):
    """HTTP 协议下的 list 端点描述（task 6 接入；用于 vendor 资源列表的健康探活）。"""

    model_config = ConfigDict(extra="forbid")

    method: str = Field("GET", description="HTTP method")
    path: str = Field(..., description="相对 base_path 的路径")
    headers: Dict[str, str] = Field(default_factory=dict, description="附加 header")
    page_param: Optional[str] = Field(
        None, description="分页 page 参数名（如 'page' / 'pageNo'）；为 None 时不分页"
    )
    page_size_param: Optional[str] = Field(
        None, description="分页 page_size 参数名；为 None 时不带 size"
    )
    page_size: Optional[int] = Field(
        None, ge=1, le=1000, description="每页条数（page_size_param 设置时生效）"
    )
    expected_count_min: Optional[int] = Field(
        None, ge=0, description="期望最小条数（None = 不校验）"
    )

    @field_validator("method")
    @classmethod
    def _upper(cls, v: str) -> str:
        v = (v or "").strip().upper()
        if v not in {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}:
            raise ValueError(f"不支持的 HTTP method：{v!r}")
        return v

    @field_validator("path")
    @classmethod
    def _path_nonempty(cls, v: str) -> str:
        if not v or not v.startswith("/"):
            raise ValueError("path 必须以 '/' 开头")
        return v


class HttpSampleEndpoint(BaseModel):
    """HTTP 协议下的 sample 端点描述。"""

    model_config = ConfigDict(extra="forbid")

    method: str = Field("GET", description="HTTP method")
    path: str = Field(..., description="相对 base_path 的路径，可含 query string")
    headers: Dict[str, str] = Field(default_factory=dict, description="附加 header")
    body: str = Field("", description="可选请求体（POST/PUT 时使用）")
    list_endpoint: Optional[HttpListEndpoint] = Field(
        None, description="可选的 list 端点（task 6 接入，用于资源列表探活）"
    )

    @field_validator("method")
    @classmethod
    def _upper(cls, v: str) -> str:
        v = (v or "").strip().upper()
        if v not in {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}:
            raise ValueError(f"不支持的 HTTP method：{v!r}")
        return v

    @field_validator("path")
    @classmethod
    def _path_nonempty(cls, v: str) -> str:
        if not v or not v.startswith("/"):
            raise ValueError("path 必须以 '/' 开头")
        return v


class Hl7v2SampleEndpoint(BaseModel):
    """HL7v2 协议下的 sample 端点描述（消息模板选择）。"""

    model_config = ConfigDict(extra="forbid")

    message_type: str = Field("ADT^A01", description="HL7 消息类型 ADT^A01 / ORM^O01 / ORU^R01 / SIU^S12")
    trigger_event: str = Field("A01", description="trigger event，如 A01 / O01 / R01 / S12")
    sample_patient_id: str = Field("<EXAMPLE_PATIENT_ID>", description="样例患者 ID（脱敏占位）")


# --- vendor interface ---------------------------------------------------------


class SampleEndpoint(BaseModel):
    """sample 端点描述（同时容纳 HTTP 与 HL7v2 两种形态）。

    YAML 中按 protocol 选择其中一个子字段填写：

    ```yaml
    sample_endpoint:
      http:
        method: GET
        path: /register/slot
    # 或
    sample_endpoint:
      hl7v2:
        message_type: ADT^A01
    ```
    """

    model_config = ConfigDict(extra="forbid")

    http: Optional[HttpSampleEndpoint] = Field(None, description="HTTP 协议下的 sample 端点")
    hl7v2: Optional[Hl7v2SampleEndpoint] = Field(None, description="HL7v2 协议下的 sample 端点")

    @model_validator(mode="after")
    def _exactly_one(self) -> "SampleEndpoint":
        if (self.http is None) == (self.hl7v2 is None):
            raise ValueError("sample_endpoint 必须且只能填写 http / hl7v2 其一")
        return self


class VendorInterface(BaseModel):
    """单条 vendor 接口规格。"""

    model_config = ConfigDict(extra="forbid")

    vendor_id: str = Field(..., min_length=1, description="厂商/业务系统 ID，如 his/lis/pacs")
    interface_id: str = Field(..., min_length=1, description="接口 ID，与 vendor_id 组成 FQN")
    display_name: str = Field(..., min_length=1, description="中文显示名")
    protocol: str = Field(..., description="协议名（内置枚举或插件注册协议字符串）")
    host: str = Field(..., min_length=1, description="vendor 主机（IP / 域名 / env 名）")
    port: int = Field(..., ge=1, le=65535, description="vendor 端口")
    base_path: str = Field("/", description="HTTP base path")
    timeout_seconds: float = Field(5.0, gt=0, le=120, description="单步超时")
    auth: AuthConfig = Field(..., description="鉴权配置（discriminated union）")
    sample_endpoint: SampleEndpoint = Field(..., description="sample 端点描述（HTTP / HL7v2 二选一）")
    required_fields: List[str] = Field(
        default_factory=list,
        description="必填字段路径列表（HTTP: JSONPath；HL7: 段路径如 MSH-9）",
    )

    @field_validator("protocol", mode="before")
    @classmethod
    def _coerce_protocol(cls, v):
        """接受内置枚举值或已注册的插件协议字符串，统一归一化为小写字符串。"""

        return _validate_protocol_value(v)

    @model_validator(mode="after")
    def _validate_protocol_endpoint(self) -> "VendorInterface":
        ep = self.sample_endpoint
        proto = self.protocol
        if proto == Protocol.HTTP.value and ep.http is None:
            raise ValueError("protocol=http 时 sample_endpoint.http 必须存在")
        if proto == Protocol.HL7V2.value and ep.hl7v2 is None:
            raise ValueError("protocol=hl7v2 时 sample_endpoint.hl7v2 必须存在")
        # 内置 WEBSERVICE/SQL 严格；插件协议（plugin）放过此项校验（由插件自行负责）
        if proto in (Protocol.WEBSERVICE.value, Protocol.SQL.value):
            if ep.http is None and ep.hl7v2 is None:
                raise ValueError(
                    f"protocol={proto} 必须填写 sample_endpoint.http 或 hl7v2 之一"
                )
        return self

    @property
    def active_endpoint(self) -> Union[HttpSampleEndpoint, Hl7v2SampleEndpoint]:
        """按 protocol 返回当前激活的 sample endpoint 子对象。"""

        if self.sample_endpoint.http is not None:
            return self.sample_endpoint.http
        if self.sample_endpoint.hl7v2 is not None:
            return self.sample_endpoint.hl7v2
        raise ValueError("sample_endpoint 既无 http 也无 hl7v2，不应发生（构造时已校验）")

    @property
    def fqn(self) -> str:
        """接口 FQN：``vendor_id.interface_id``，全库唯一。"""

        return f"{self.vendor_id}.{self.interface_id}"

    @property
    def secret_envs(self) -> List[str]:
        """返回该接口涉及的凭据环境变量名列表（按鉴权类型聚合）。"""

        envs: List[str] = []
        if isinstance(self.auth, (AuthBasic, AuthBearer, AuthApiKey)) and self.auth.secret_env:
            envs.append(self.auth.secret_env)
        if isinstance(self.auth, AuthBasic) and self.auth.username_env:
            envs.append(self.auth.username_env)
        if isinstance(self.auth, AuthMtls):
            envs.extend([self.auth.cert_env, self.auth.key_env])
            if self.auth.ca_bundle_env:
                envs.append(self.auth.ca_bundle_env)
        return envs

    def to_safe_dict(self) -> Dict[str, Any]:
        """返回脱敏的 dict（凭据相关字段保留为 ``<env:VENDOR_...>`` 形式）。"""

        data = self.model_dump()
        if "auth" in data:
            data["auth"] = dict(data["auth"])
        return data


# --- top-level spec -----------------------------------------------------------


class InspectionConfig(BaseModel):
    """巡检编排选项。"""

    model_config = ConfigDict(extra="forbid")

    fail_fast: bool = Field(False, description="首个失败是否短路（默认跑完所有 vendor）")
    handover_dir: str = Field("./out/${DATE}", description="handover.md 输出目录")
    tickets_dir: str = Field("./out/${DATE}", description="tickets.jsonl 输出目录")
    sqlite_db: str = Field("./out/vendor_if_doctor.db", description="巡检历史 SQLite 路径")
    redact_credentials: bool = Field(True, description="凭据脱敏开关")


class VendorSpec(BaseModel):
    """vendor_if_doctor 顶层规格。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field("1.0", description="spec schema 版本")
    run_window_minutes: int = Field(30, ge=1, le=1440, description="单次 run 的时间窗")
    severity_policy: str = Field("default", description="失败告警分级策略")
    interfaces: List[VendorInterface] = Field(..., min_length=1)
    inspection: InspectionConfig = Field(default_factory=InspectionConfig)

    @model_validator(mode="after")
    def _unique_fqns(self) -> "VendorSpec":
        seen: Dict[str, str] = {}
        dups: List[str] = []
        for itf in self.interfaces:
            fqn = itf.fqn
            if fqn in seen:
                dups.append(fqn)
            else:
                seen[fqn] = itf.host
        if dups:
            raise ValueError(f"interfaces FQN 不唯一：{', '.join(sorted(set(dups)))}")
        return self

    def find(self, vendor_id: str, interface_id: str) -> Optional[VendorInterface]:
        for itf in self.interfaces:
            if itf.vendor_id == vendor_id and itf.interface_id == interface_id:
                return itf
        return None

    def iter_vendors(self):
        return iter(self.interfaces)


__all__ = [
    "AuthApiKey",
    "AuthBasic",
    "AuthBearer",
    "AuthConfig",
    "AuthMllp",
    "AuthMtls",
    "AuthNone",
    "Hl7v2SampleEndpoint",
    "HttpListEndpoint",
    "HttpSampleEndpoint",
    "InspectionConfig",
    "Protocol",
    "SampleEndpoint",
    "VendorInterface",
    "VendorSpec",
]
