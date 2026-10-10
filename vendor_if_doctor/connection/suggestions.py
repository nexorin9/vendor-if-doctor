"""suggestions — 失败建议动作模板库（按 failure_class 给出 1-3 条具体排查命令）。

设计要点
--------

* **每个 failure_class 映射 1–3 条具体排查命令**：值班员拿到工单草稿
  ``tickets.jsonl`` 后能直接复制粘贴到工单系统 / 终端 / 厂商群。每条命令是
  可独立运行的 shell 命令或操作步骤，不是空泛的「请检查」之类的描述。
* **占位符替换**：模板里允许出现以下占位符；调用方传 ``context`` dict 后，
  函数自动替换。占位符分两类：

  - **接口身份占位符**（必填）：``{vendor_id}`` / ``{vendor_token}`` /
    ``{interface_id}`` / ``{fqn}`` / ``{host}`` / ``{port}`` /
    ``{protocol}`` / ``{auth_type}`` / ``{auth_env}`` / ``{timeout_seconds}``。
  - **可选占位符**（缺省时模板里被替换为「UNKNOWN」/「N/A」）：
    ``{start_time}`` / ``{end_time}`` / ``{cert_path}`` / ``{key_path}`` /
    ``{ca_path}`` / ``{auth_env_alt}`` / ``{wsdl_url}``。

  调用方也可以传额外字段（如 ``{evidence_hash}``），模板里出现未声明的
  占位符 → 原样保留该条（task 9 既有兼容行为），便于后续 Phase 增量扩展。

* **认证类型差异化**（task 31 新增）：``auth_failed`` 失败按 auth.type 走不同
  排查路径——basic 走 user/pass 校验 + base64 自检；bearer 走 token 过期 +
  JWT 解码；apikey 走 header vs query 注入位置；mtls 走证书文件存在性 +
  openssl 校验。``http_status`` 失败同样区分 401/403 vs 5xx（虽然 classify
  已经把 401/403 分离到 auth_failed，这里兜底 4xx/5xx 排查路径）。

* **凭据纪律**：模板字面量不写任何真实凭据；仅引用 ``{vendor_token}`` 这类
  环境变量名，让值班员照着 ``env | grep VENDOR_HIS`` 查值。``auth_failed``
  模板里出现 ``VENDOR_{vendor_token}_*`` 通配形式，避免泄漏具体 env 名
  （HIS / LIS / PACS 同形态时命名约定一致，env 名差异由值班员按现场补）。
* **空 failure_class 与未知 failure_class 一律返回空列表**：保守兜底，避免
  渲染出错误命令给值班员（task 9 既有语义）。模板缺失同样返空列表。

调用方约定
----------

* :func:`suggest_actions(failure_class, vendor_id, interface_id, **context)`
  返回 1–3 条具体命令字符串列表；
* :func:`list_supported_failure_classes()` 返回所有有模板的 failure_class
  字符串列表（便于 CLI `--list-suggestions` 兜底展示）；
* :func:`placeholder_keys()` 返回全部合法占位符 key 集合（便于文档自检）；
* :func:`suggested_actions_for(failure_class, vendor_id, interface_id)` 是
  :func:`suggest_actions` 的兼容别名（task 9 旧接口），仍可用但建议迁到
  新接口以享受占位符扩展。

示例
----

>>> actions = suggest_actions(
...     "auth_failed", vendor_id="his", interface_id="outpatient-register",
...     host="his-hospital.example.com", port=80, protocol="http",
...     auth_type="bearer", auth_env="VENDOR_HIS_TOKEN",
... )
>>> len(actions) >= 1
True
>>> "{vendor_id}" in " ".join(actions)
False
"""
from __future__ import annotations

from typing import Iterable, List, Mapping, Optional

# ---------------------------------------------------------------------------
# 占位符词表（单一真源）
# ---------------------------------------------------------------------------

# 接口身份必填占位符（调用方必须能解析；缺值时落 '<UNKNOWN>'）
PLACEHOLDER_KEYS_REQUIRED: frozenset[str] = frozenset(
    {
        "vendor_id",
        "vendor_token",
        "interface_id",
        "fqn",
        "host",
        "port",
        "protocol",
        "auth_type",
        "auth_env",
        "timeout_seconds",
    }
)

# 可选占位符（缺省时落 'N/A' / 'UNKNOWN'，避免渲染出破损的 shell 命令）
PLACEHOLDER_KEYS_OPTIONAL: frozenset[str] = frozenset(
    {
        "start_time",
        "end_time",
        "cert_path",
        "key_path",
        "ca_path",
        "auth_env_alt",
        "wsdl_url",
        "evidence_hash",
    }
)

PLACEHOLDER_KEYS: frozenset[str] = PLACEHOLDER_KEYS_REQUIRED | PLACEHOLDER_KEYS_OPTIONAL
"""全部合法占位符（必填 + 可选），便于文档自检与 lint 校验。"""


# ---------------------------------------------------------------------------
# 模板库（按 failure_class 排）
# ---------------------------------------------------------------------------

# 通用前缀说明：每条模板最多 3 条；每条末尾不带分号，复制即可贴终端。
# 涉及 vendor_id-derivative 占位符时用 snake-case（与 env var 命名一致）。

# failure_class = auth_failed（按 auth_type 分支）
_AUTH_FAILED_TEMPLATES_BY_AUTH_TYPE: Mapping[str, tuple[str, ...]] = {
    "basic": (
        # 1) 自检用户名/密码 env 是否就绪（basic auth 双字段）
        "确认 VENDOR_{vendor_token}_USERNAME 与 VENDOR_{vendor_token}_PASSWORD (或 SECRET) "
        "env 变量已 export 且未过期",
        # 2) 手工 base64 自检（让值班员直接看到 401 是不是 base64 编码错位）
        "printf '%s' \"$VENDOR_{vendor_token}_USERNAME:$VENDOR_{vendor_token}_PASSWORD\" | "
        "base64 | xargs -I{{}} curl -v -H 'Authorization: Basic {{}}' "
        "https://{host}:{port}/health",
        # 3) 核对厂商接口是否要求 realm
        "检查 vendor 文档：basic auth 是否需要 realm（若 spec 已声明 realm，看是否一致）",
    ),
    "bearer": (
        # 1) 凭据就绪 + token 有效期
        "确认 VENDOR_{vendor_token}_TOKEN (或 _AUTH_TOKEN) env 变量已 export 且未过期；"
        "若 JWT 用 https://jwt.io 解码看 exp 字段是否 < 当前时间",
        # 2) 手工 health 自检（避开重定向 / 路由）
        "curl -v -H 'Authorization: Bearer ***' https://{host}:{port}/health",
        # 3) 走厂商 OAuth 刷新路径
        "若 token 已过期，触发厂商 OAuth 刷新脚本（参考 vendor 文档的 token endpoint）",
    ),
    "apikey": (
        # 1) 凭据就绪
        "确认 VENDOR_{vendor_token}_APIKEY env 变量已 export 且未轮换",
        # 2) header vs query 注入自检
        "若 spec.auth.inject=header，curl -v -H 'X-API-Key: ***' https://{host}:{port}/health；"
        "若 spec.auth.inject=query，curl -v 'https://{host}:{port}/health?api_key=***'",
        # 3) header_name 命名约定
        "检查 spec.auth.header_name（默认 X-API-Key）与厂商文档约定一致",
    ),
    "mtls": (
        # 1) 证书文件存在性
        "确认 VENDOR_{vendor_token}_CERT 与 VENDOR_{vendor_token}_KEY env 指向路径存在且可读 "
        "（ls -l \"$VENDOR_{vendor_token}_CERT\" \"$VENDOR_{vendor_token}_KEY\"）",
        # 2) openssl 校验证书签名（用 cert_path 可选占位符兜底；缺值时显示 N/A）
        "openssl verify -CAfile \"$VENDOR_{vendor_token}_CA\" \"$VENDOR_{vendor_token}_CERT\" "
        "或 openssl x509 -in {cert_path} -noout -text | grep -A2 'Subject:'",
        # 3) 证书有效期（用 ca_path 可选占位符）
        "openssl x509 -in \"$VENDOR_{vendor_token}_CERT\" -noout -dates 看 notAfter 是否 < 当前时间；"
        "若证书目录是 {ca_path}，联系厂商替换过期 CA 链",
    ),
    "mllp": (
        # MLLP 通常无独立凭据；auth_failed 罕见，多半是通道被厂商防火墙拦
        "确认 MLLP 通道 IP 白名单已把院内出口 IP 加进去（联系厂商接口工程师）",
        "telnet {host} {port} 看 TCP 是否可达（MLLP 通道 IP 允许后才走 MLLP）",
        "若厂商 MLLP 网关在 VPN 后，核对 VPN 隧道是否正常（ip route get {host}）",
    ),
    "none": (
        # 无凭据情况下的 auth_failed 极少见；兜底建议（多半是厂商侧 IP 白名单）
        "确认院内出口 IP 已在厂商 IP 白名单（WebService / SQL / 默认无 auth 多见）",
        "telnet {host} {port} 看 TCP 是否可达",
        "核对 vendor spec.protocol 与厂商文档版本是否对得上",
    ),
}

# failure_class = http_status（5xx 与 4xx 走不同排查路径；4xx 非 401/403 兜底）
_TEMPLATE_HTTP_STATUS: tuple[str, ...] = (
    "查厂商接口状态码表（接口工程师 / 厂商群）：status={evidence_hash} 仅作 'ERROR' 占位",
    "curl -i https://{host}:{port}{protocol}-endpoint 看响应头与完整 body",
    "若连续 5xx > 5 次，触发 vendor 健康巡检告警（VENDOR_DOCTOR_SLOW_THRESHOLD_MS 调高）",
)

# failure_class = schema_mismatch（required_fields 缺失或字段名错位）
_TEMPLATE_SCHEMA_MISMATCH: tuple[str, ...] = (
    "对比 vendor 接口字段表 vs 当前 vendor_spec.yaml 的 required_fields",
    "用 jq '.code, .patient_id, .slot_id' 验证真实响应与 schema 哪个字段缺失/类型不匹配",
    "若厂商升级了字段，请求厂商提供新接口文档；同步更新 vendor_spec.required_fields",
)

# failure_class = payload_empty
_TEMPLATE_PAYLOAD_EMPTY: tuple[str, ...] = (
    "用 curl -v 直接打 vendor 接口看是否真的返回空 body（绕过巡检的 timeout）",
    "联系厂商问本期是否停用该接口或处于灰度切换期",
    "若偶发空体且 vendor 接口支持分页，把 list_endpoint.page_size 调小再观察",
)

# failure_class = hl7_ack_re（MSA-1=AE/AR）
_TEMPLATE_HL7_ACK_RE: tuple[str, ...] = (
    "用 hl7apy 校验发出的 ADT^A01 (或 ORM^O01/ORU^R01/SIU^S12) 段是否符合接口规范",
    "联系厂商确认 MLLP 通道 / 消息版本号（MSH-9 / MSH-12）是否对得上",
    "tcpdump -i any port {port} 抓包看 ACK 原文（AE=Application Error / AR=Application Reject）",
)

# failure_class = hl7_ack_missing（超时）
_TEMPLATE_HL7_ACK_MISSING: tuple[str, ...] = (
    "tcpdump -i any port {port} 抓包看 ACK 是否回到 client（避免偶发丢包被计时器吃掉）",
    "提高 spec.timeout_seconds 后再跑一次（默认 5s，建议先调到 8s 观察）",
    "检查服务端 keepalive 配置（长时间无数据可能触发厂商网关 idle 超时）",
)

# failure_class = timeout（含 httpx 与 MLLP 超时）
_TEMPLATE_TIMEOUT: tuple[str, ...] = (
    "telnet {host} {port}（确认 TCP 通道是否可达）",
    "mtr -r -c 30 {host} 看丢包率（连续 > 5% 即视为抖动）",
    "若 spec.timeout_seconds < 5，适当调大后再跑一次（默认 5s）",
)

# failure_class = tcp_unreachable（connect refused）
_TEMPLATE_TCP_UNREACHABLE: tuple[str, ...] = (
    "ip route get {host}（路由是否通；不通则核对 DNS / VPN）",
    "iptables -L -n -v | grep -i {vendor_id}（防火墙是否拦截出向连接）",
    "telnet {host} {port} 看是否立刻 ConnectionRefused（若是，对端服务可能挂了）",
)

# failure_class = tcp_half_closed（ECONNRESET / FIN_WAIT）
_TEMPLATE_TCP_HALF_CLOSED: tuple[str, ...] = (
    "ss -tn dport = :{port} | grep {vendor_id}（看长连接状态是否大量 FIN_WAIT）",
    "联系厂商接口工程师，要求检查其服务端的 keepalive 配置",
    "若短期内重启后即复现，怀疑厂商防火墙 / LB 在做连接回收；"
    "可参考最近一次巡检时间窗 start_time={start_time} 到 end_time={end_time} "
    "统计半关闭频次",
)

# failure_class = degraded_slow（响应时延 > 阈值）
_TEMPLATE_DEGRADED_SLOW: tuple[str, ...] = (
    "查 vendor 接口是否发布过维护公告（厂商群 / 公告板）",
    "用 curl -w 'time_total=%{time_total}\\n' 重复三次，看 P95 时延（> 阈值即认为 degraded）",
    "若短期内持续 degraded，临时把 VENDOR_DOCTOR_SLOW_THRESHOLD_MS 调高避免误升级 critical",
)

# failure_class = encoding_error
_TEMPLATE_ENCODING_ERROR: tuple[str, ...] = (
    "用 file -i <raw-response> 看真实编码（GBK / BIG5 / UTF-8）",
    "在 vendor_spec.yaml 把 sample_endpoint.headers.Accept-Charset 写明确 "
    "（Accept-Charset: utf-8, gbk）",
    "若厂商坚持 GBK，把 confirm_message 字段值用 iconv 转 UTF-8 后再校验",
)

# failure_class = not_implemented（占位 / 未接入）
_TEMPLATE_NOT_IMPLEMENTED: tuple[str, ...] = (
    "在 vendor_spec.protocol 里扩展为已实现的协议（http / hl7v2 / sql / webservice）",
    "或编写自定义 protocol plugin（参考 VENDOR_DOCTOR_PLUGINS 加载机制）",
    "在 task tracker / 接口接入计划里给该 vendor 排期，不阻塞 stall 计时",
)

# failure_class = ambient_runner_error（async runner 单 vendor 抛错包装，task 20）
_TEMPLATE_AMBIENT_RUNNER_ERROR: tuple[str, ...] = (
    "查看 progress.txt 末尾 stack trace，定位 runner 内部异常（多半是凭据 / schema 问题）",
    "用同 spec 跑单 sync path（python -m vendor_if_doctor run --vendor {vendor_id} --no-async）确认是否复现",
    "若是并发限流压死，把 spec.concurrency 调小到 1-2 再观察",
)


# 完整模板表（按 failure_class 字符串索引）
_TEMPLATES: Mapping[str, tuple[str, ...]] = {
    # 6 个核心 failure_class
    "http_status": _TEMPLATE_HTTP_STATUS,
    "auth_failed": _AUTH_FAILED_TEMPLATES_BY_AUTH_TYPE["bearer"],  # 默认兜底走 bearer
    "timeout": _TEMPLATE_TIMEOUT,
    "schema_mismatch": _TEMPLATE_SCHEMA_MISMATCH,
    "payload_empty": _TEMPLATE_PAYLOAD_EMPTY,
    "hl7_ack_re": _TEMPLATE_HL7_ACK_RE,
    # 扩展 failure_class（task 17/24/25 落地）
    "tcp_unreachable": _TEMPLATE_TCP_UNREACHABLE,
    "tcp_half_closed": _TEMPLATE_TCP_HALF_CLOSED,
    "degraded_slow": _TEMPLATE_DEGRADED_SLOW,
    "encoding_error": _TEMPLATE_ENCODING_ERROR,
    "hl7_ack_missing": _TEMPLATE_HL7_ACK_MISSING,
    "not_implemented": _TEMPLATE_NOT_IMPLEMENTED,
    "ambient_runner_error": _TEMPLATE_AMBIENT_RUNNER_ERROR,
}


# ---------------------------------------------------------------------------
# 内部辅助：拼 context + 渲染模板
# ---------------------------------------------------------------------------


def _build_default_context(
    vendor_id: str,
    interface_id: str,
    **overrides: object,
) -> dict[str, str]:
    """拼一份 context：必填占位符给 'UNKNOWN' 默认值，可选占位符给 'N/A'。"""

    vendor_token = vendor_id.upper().replace("-", "_")
    ctx: dict[str, str] = {
        # 必填占位符（task 9 既有 4 个）
        "vendor_id": vendor_id,
        "vendor_token": vendor_token,
        "interface_id": interface_id,
        "fqn": f"{vendor_id}.{interface_id}",
        # 新增必填占位符（task 31）— 缺值时落 UNKNOWN 便于渲染不破
        "host": str(overrides.pop("host", "UNKNOWN")),
        "port": str(overrides.pop("port", "UNKNOWN")),
        "protocol": str(overrides.pop("protocol", "UNKNOWN")),
        "auth_type": str(overrides.pop("auth_type", "none")),
        "auth_env": str(overrides.pop("auth_env", "UNKNOWN")),
        "timeout_seconds": str(overrides.pop("timeout_seconds", "5")),
    }
    for opt_key in PLACEHOLDER_KEYS_OPTIONAL:
        if opt_key in overrides:
            ctx[opt_key] = str(overrides[opt_key])
        else:
            ctx[opt_key] = "N/A"
    # 兼容 caller 传额外的未声明占位符（如 evidence_hash）；保留原 key
    for k, v in overrides.items():
        if k not in ctx:
            ctx[k] = str(v)
    return ctx


def _render_templates(
    templates: Iterable[str],
    context: Mapping[str, str],
) -> List[str]:
    """逐条渲染模板：占位符全替换；遇到 KeyError 原样保留该条。"""

    rendered: list[str] = []
    for tmpl in templates:
        try:
            rendered.append(tmpl.format(**context))
        except KeyError as exc:
            # 模板里出现未定义占位符 → 原样保留该条（task 9 既有兼容行为）
            # 仅当占位符不在 ctx 里时才走这条；否则应正常 format
            missing = exc.args[0] if exc.args else "?"
            rendered.append(tmpl.format_map({**context, missing: tmpl}))
    return rendered


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------


def suggest_actions(
    failure_class: Optional[str],
    vendor_id: str,
    interface_id: str,
    *,
    auth_type: Optional[str] = None,
    **context: object,
) -> List[str]:
    """根据 ``failure_class`` 返回 1–3 条具体排查命令。

    Parameters
    ----------
    failure_class : str or None
        任意 ``Evidence.failure_class`` 字符串；``None`` 或未知字符串 → 空列表。
    vendor_id, interface_id : str
        接口身份（必填）。
    auth_type : str, optional
        当 ``failure_class='auth_failed'`` 时按 auth.type 走不同分支
        （basic/bearer/apikey/mtls/mllp/none）；其它 failure_class 忽略。
    **context : object
        任意占位符值（如 ``host='his-endpoint.com'`` / ``port=80`` /
        ``auth_env='VENDOR_HIS_TOKEN'`` / ``cert_path=...`` /
        ``evidence_hash=...``）。未声明占位符走默认值（UNKNOWN / N/A）。

    Returns
    -------
    list[str]
        1–3 条具体 shell 命令字符串。空 failure_class / 未知 failure_class
        一律返回 ``[]``（保守兜底）。
    """

    if not failure_class:
        return []
    if failure_class == "auth_failed" and auth_type in _AUTH_FAILED_TEMPLATES_BY_AUTH_TYPE:
        templates = _AUTH_FAILED_TEMPLATES_BY_AUTH_TYPE[auth_type]
    else:
        templates = _TEMPLATES.get(failure_class)
    if not templates:
        return []

    ctx = _build_default_context(vendor_id, interface_id, **context)
    # auth_type 在 _build_default_context 已塞进 ctx；这里覆盖一次保证 caller 传入优先
    if auth_type is not None:
        ctx["auth_type"] = auth_type
    return _render_templates(templates, ctx)


def suggested_actions_for(
    failure_class: Optional[str],
    vendor_id: str,
    interface_id: str,
) -> List[str]:
    """task 9 兼容别名。

    仅替换 4 个必填占位符（vendor_id / vendor_token / interface_id / fqn）；
    调用方无须关心 host / port / auth_type 等扩展字段。task 31 之后建议迁到
    :func:`suggest_actions` 以享受完整占位符。
    """

    return suggest_actions(failure_class, vendor_id, interface_id)


def list_supported_failure_classes() -> list[str]:
    """返回所有有模板的 ``failure_class`` 字符串列表（按字母升序）。"""

    return sorted(_TEMPLATES.keys())


def placeholder_keys() -> frozenset[str]:
    """返回全部合法占位符 key 集合（必填 + 可选，便于文档自检）。"""

    return PLACEHOLDER_KEYS


def auth_types_for_auth_failed() -> list[str]:
    """返回 ``auth_failed`` 失败支持的 ``auth_type`` 分支列表（按字母升序）。"""

    return sorted(_AUTH_FAILED_TEMPLATES_BY_AUTH_TYPE.keys())


__all__ = [
    "PLACEHOLDER_KEYS",
    "PLACEHOLDER_KEYS_OPTIONAL",
    "PLACEHOLDER_KEYS_REQUIRED",
    "auth_types_for_auth_failed",
    "list_supported_failure_classes",
    "placeholder_keys",
    "suggest_actions",
    "suggested_actions_for",
]