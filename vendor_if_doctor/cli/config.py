"""CLI 配置加载 — 优先级合并 + 凭据 alias 兼容 + 来源日志。

设计要点
--------

* **配置层（task 21 完整化）**：

  * 入口 :func:`load_config` 把三类来源（env / config.yaml / 默认值）按
    **env > config.yaml > 默认** 合并到一个 :class:`Config` dataclass；
  * 输出路径类 (``handover_dir`` / ``tickets_dir`` / ``sqlite_path``) 走
    ``$VENDOR_DOCTOR_HANDOVER_DIR`` / ``$VENDOR_DOCTOR_TICKETS_DIR`` /
    ``$VENDOR_DOCTOR_DB`` 或 ``config.yaml`` 的 ``vendor.doctor.*`` 节；
  * 凭据类（``vendor.<name>.host`` / ``vendor.<name>.token`` 等）走
    ``$VENDOR_<NAME>_HOST`` / ``$VENDOR_<NAME>_TOKEN`` / ``$VENDOR_<NAME>_AUTH_TOKEN``
    等，环境变量优先于 YAML。

* **凭据 env alias 兼容**：规范名 ``VENDOR_<NAME>_TOKEN`` 的 alias 列表见
  :data:`ALLOWED_ALIASES`，例如 ``VENDOR_HIS_AUTH_TOKEN`` / ``VENDOR_HIS_PASSWORD``
  都识别为 ``VENDOR_HIS_TOKEN`` 的别名。

  * 当 env 同时设置 ``VENDOR_HIS_TOKEN`` 与 ``VENDOR_HIS_AUTH_TOKEN`` 时，**优先
    用 alias（更具体的名字）**并 :func:`logging.warning` 打印「重复凭据」提示；
  * 当 env 仅设置 alias（如 ``VENDOR_HIS_AUTH_TOKEN``）而规范名
    ``VENDOR_HIS_TOKEN`` 为空时，落回 alias 值（alias 解析）；
  * 都未设置 → 返回 ``None``（启动期不强制凭据；``--validate-credentials`` 才报错）。

* **来源日志（强制）**：每次合并后，对每个被赋值的字段打印一行
  ``Using source: env VENDOR_HIS_HOST`` / ``Using source: config.yaml
  vendor.his.host`` / ``Using source: default``，便于值班员排查"凭据从哪来"。
  日志经过 :func:`vendor_if_doctor.redact.redact_str` 兜底，绝不出 token 字面量。

* **redact 兼容 re-export**：本模块仍从 :mod:`vendor_if_doctor.redact`
  顶层模块统一导入（task 18 把 redact 工具下沉为单一真源；本模块保留
  ``redact_dict`` / ``redact_str`` / ``redact_env_for_log`` 的 re-export，
  不破坏既有 CLI 子命令的导入）。

不在本模块发起网络请求；仅做配置层加载。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import yaml

from vendor_if_doctor.redact import (
    redact_dict,
    redact_env_for_log,
    redact_str,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 配置路径与默认值
# ---------------------------------------------------------------------------


DEFAULT_CONFIG_PATH = "vendor_if_doctor.yaml"

# 输出路径 / 数据库的默认位置（与 run_cmd 默认值保持一致）。
_DEFAULT_HANDOVER_DIR = Path("out")
_DEFAULT_TICKETS_DIR = Path("out")
_DEFAULT_SQLITE_PATH = Path("vendor_if_doctor.db")
DEFAULT_LOG_LEVEL = "INFO"  # noqa: S105 — 默认值常量，不暴露凭据


# task 21 完整化的别名映射：规范名 → alias 列表（按顺序查找第一个非空）。
#
# 命名规则：
#   - 凭据类：VENDOR_<NAME>_<TYPE> 的 alias（如 _AUTH_TOKEN、_PASSWORD）；
#   - 同时设置规范名与 alias 时，alias（更具体）赢；
#   - 别名表用于「直接命中」（spec 写的名字）与「alias 命中」（env 设的名字）
#     两条路径。
#
# 字段类型约定：
#   - HOST / APIKEY: 走 vendor.<name>.host / VENDOR_<NAME>_HOST
#   - TOKEN: 走 vendor.<name>.token / VENDOR_<NAME>_TOKEN + AUTH_TOKEN alias
#   - PASSWORD: 走 vendor.<name>.password / VENDOR_<NAME>_PASSWORD + AUTH_TOKEN alias
#   - USERNAME: 走 vendor.<name>.username / VENDOR_<NAME>_USERNAME
ALLOWED_ALIASES: Dict[str, List[str]] = {
    # HIS — token / password 互通（部分厂商用 _AUTH_TOKEN 命名）
    "VENDOR_HIS_TOKEN": ["VENDOR_HIS_AUTH_TOKEN"],
    "VENDOR_HIS_PASSWORD": ["VENDOR_HIS_AUTH_TOKEN", "VENDOR_HIS_TOKEN"],
    # LIS — auth_type=bearer，token 也认 AUTH_TOKEN alias
    "VENDOR_LIS_TOKEN": ["VENDOR_LIS_AUTH_TOKEN"],
    # PACS — auth_type=apikey，apikey 也认 AUTH_TOKEN alias
    "VENDOR_PACS_APIKEY": ["VENDOR_PACS_AUTH_TOKEN"],
    # 医保（medical-insurance）— SOAP basic auth
    "VENDOR_MI_PASSWORD": ["VENDOR_MI_AUTH_TOKEN"],
    "VENDOR_MI_USERNAME": ["VENDOR_MI_AUTH_USER"],
    # EMR — MLLP 鉴权走 token 别名
    "VENDOR_EMR_TOKEN": ["VENDOR_EMR_AUTH_TOKEN"],
}


# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------


@dataclass
class Config:
    """CLI 配置聚合 dataclass — 暴露给 run / history / init 子命令的视图。

    字段来源（按优先级 env > config.yaml > 默认）：
      * ``config_path``:   入参 / ``$VENDOR_DOCTOR_CONFIG`` / 默认 ``./vendor_if_doctor.yaml``
      * ``handover_dir``:  ``$VENDOR_DOCTOR_HANDOVER_DIR`` / config / ``./out``
      * ``tickets_dir``:   ``$VENDOR_DOCTOR_TICKETS_DIR`` / config / ``./out``
      * ``sqlite_path``:   ``$VENDOR_DOCTOR_DB`` / config / ``./vendor_if_doctor.db``
      * ``log_level``:     ``$VENDOR_DOCTOR_LOG_LEVEL`` / config / ``INFO``
      * ``validate_credentials``: config / 默认 False
      * ``vendor_hosts``:  ``$VENDOR_<NAME>_HOST`` / config / 未设置 → 空
      * ``vendor_tokens``: ``$VENDOR_<NAME>_TOKEN`` (alias ``_AUTH_TOKEN``) / config / 未设置 → 空
    """

    config_path: Optional[Path]
    handover_dir: Path
    tickets_dir: Path
    sqlite_path: Path
    log_level: str
    validate_credentials: bool
    vendor_hosts: Dict[str, str] = field(default_factory=dict)
    vendor_tokens: Dict[str, Optional[str]] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)
    sources: Dict[str, str] = field(default_factory=dict)

    def source_of(self, key: str) -> str:
        """查询某个字段的来源（env / config.yaml / default）。"""

        return self.sources.get(key, "default")


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------


def _config_path(path: Optional[os.PathLike], env: Mapping[str, str]) -> Path:
    """解析配置文件路径，优先级：入参 > env > 默认。"""

    if path is not None:
        return Path(path)
    env_val = env.get("VENDOR_DOCTOR_CONFIG")
    if env_val:
        return Path(env_val)
    return Path.cwd() / DEFAULT_CONFIG_PATH


def _resolve_path_field(
    *,
    env_var: str,
    config_keys: List[str],
    default: Path,
    raw_config: Mapping[str, Any],
    env: Mapping[str, str],
) -> tuple[Path, str]:
    """按 env > config > default 解析一个路径字段。

    返回 ``(resolved_path, source_label)``；source_label 形如 ``env VENDOR_X`` /
    ``config.yaml vendor.doctor.x`` / ``default``，由 :func:`logger.info` 打印。

    支持点分路径（如 ``vendor.doctor.handover_dir``）— 按 ``.`` 切段在 YAML 嵌套
    mapping 中逐层下钻。
    """

    env_val = env.get(env_var)
    if env_val:
        return Path(env_val), f"env {env_var}"
    for key in config_keys:
        value = _resolve_dotted(raw_config, key)
        if isinstance(value, str) and value:
            return Path(value), f"config.yaml {key}"
    return default, "default"


def _resolve_str_field(
    *,
    env_var: str,
    config_keys: List[str],
    default: str,
    raw_config: Mapping[str, Any],
    env: Mapping[str, str],
) -> tuple[str, str]:
    """按 env > config > default 解析一个字符串字段。"""

    env_val = env.get(env_var)
    if env_val:
        return env_val, f"env {env_var}"
    for key in config_keys:
        value = _resolve_dotted(raw_config, key)
        if isinstance(value, str) and value:
            return value, f"config.yaml {key}"
    return default, "default"


def _resolve_bool_field(
    *,
    env_var: str,
    config_keys: List[str],
    default: bool,
    raw_config: Mapping[str, Any],
    env: Mapping[str, str],
) -> tuple[bool, str]:
    """按 env > config > default 解析一个布尔字段。env 真值解析遵循 1/true/yes/on。"""

    env_val = env.get(env_var)
    if env_val is not None and env_val != "":
        truthy = env_val.strip().lower() in {"1", "true", "yes", "on"}
        return truthy, f"env {env_var}={redact_str(env_val)}"
    for key in config_keys:
        value = _resolve_dotted(raw_config, key)
        if isinstance(value, bool):
            return value, f"config.yaml {key}"
    return default, "default"


def _resolve_dotted(obj: Mapping[str, Any], dotted_key: str) -> Any:
    """按 ``.`` 切段在嵌套 mapping 中逐层下钻，返回叶节点值；不存在 → ``None``。"""

    cur: Any = obj
    for segment in dotted_key.split("."):
        if isinstance(cur, Mapping) and segment in cur:
            cur = cur[segment]
        else:
            return None
    return cur


# ---------------------------------------------------------------------------
# vendor.* 段解析
# ---------------------------------------------------------------------------


def _parse_vendor_section(
    raw_config: Mapping[str, Any],
) -> tuple[Dict[str, str], Dict[str, Optional[str]]]:
    """从 ``config.yaml`` 的 ``vendor`` 节抽出 host / token。

    返回 ``({vendor_id: host}, {vendor_id: token_or_None})``；env 解析在
    :func:`_merge_env_credentials` 单独做（priority 在 env）。
    """

    hosts: Dict[str, str] = {}
    tokens: Dict[str, Optional[str]] = {}
    vendor_block = raw_config.get("vendor") or {}
    if not isinstance(vendor_block, Mapping):
        return hosts, tokens
    for vendor_id, fields in vendor_block.items():
        if not isinstance(fields, Mapping):
            continue
        host = fields.get("host")
        if isinstance(host, str) and host:
            hosts[vendor_id] = host
        token = fields.get("token") or fields.get("password") or fields.get("apikey")
        if isinstance(token, str) and token:
            tokens[vendor_id] = token
    return hosts, tokens


def _yaml_token_field(vendor_block: Mapping[str, Any], vendor_id: str) -> str:
    """从 ``raw_config['vendor'][vendor_id]`` 找回 token / password / apikey 哪个
    字段命中（用于 source 日志）。"""

    fields = vendor_block.get(vendor_id)
    if not isinstance(fields, Mapping):
        return "token"
    if "token" in fields:
        return "token"
    if "password" in fields:
        return "password"
    if "apikey" in fields:
        return "apikey"
    return "token"


def _merge_env_credentials(
    yaml_hosts: Mapping[str, str],
    yaml_tokens: Mapping[str, Optional[str]],
    raw_config: Mapping[str, Any],
    env: Mapping[str, str],
) -> tuple[Dict[str, str], Dict[str, Optional[str]], Dict[str, str]]:
    """把 env 中的 ``VENDOR_<NAME>_*`` 变量合并到 yaml 之上（env 优先）。

    返回 ``(hosts, tokens, sources)``；``sources`` 形如
    ``{"host:his": "env VENDOR_HIS_HOST", "token:his": "config.yaml vendor.his.token"}``。
    重复凭据（env 同时设置规范名 + alias）触发 warning + 优先用 alias（更具体）。
    """

    hosts: Dict[str, str] = dict(yaml_hosts)
    tokens: Dict[str, Optional[str]] = dict(yaml_tokens)
    sources: Dict[str, str] = {}

    # hosts: VENDOR_<NAME>_HOST 直接覆盖（无 alias）
    for env_name in sorted(env.keys()):
        if not env_name.startswith("VENDOR_") or not env_name.endswith("_HOST"):
            continue
        env_value = env[env_name]
        if not env_value:
            continue
        # VENDOR_HIS_HOST → his
        vendor_id = env_name[len("VENDOR_"):-len("_HOST")].lower()
        if not vendor_id:
            continue
        hosts[vendor_id] = env_value
        sources[f"host:{vendor_id}"] = f"env {env_name}"

    # tokens: VENDOR_<NAME>_TOKEN / _AUTH_TOKEN / _PASSWORD / _APIKEY / _USERNAME 五类
    vendor_block = raw_config.get("vendor") or {}
    # 收集所有 vendor_id：YAML 中存在 + env 中存在（env-only vendor 也应识别）
    all_vendor_ids: set[str] = set()
    all_vendor_ids.update(yaml_hosts.keys())
    all_vendor_ids.update(yaml_tokens.keys())
    # 后缀优先级（更具体的 alias 在前）
    cred_suffixes = (
        "_AUTH_TOKEN",
        "_AUTH_USER",
        "_AUTH_APK",
        "_TOKEN",
        "_PASSWORD",
        "_USERNAME",
        "_APIKEY",
    )
    for env_name in env:
        if not env_name.startswith("VENDOR_"):
            continue
        for suffix in cred_suffixes:
            if env_name.endswith(suffix) and len(env_name) > len(suffix) + len("VENDOR_"):
                name = env_name[len("VENDOR_"):-len(suffix)]
                if name:
                    all_vendor_ids.add(name.lower())
                break

    for vendor_id in sorted(all_vendor_ids):
        vendor_id_upper = vendor_id.upper()
        canonical_token = f"VENDOR_{vendor_id_upper}_TOKEN"
        canonical_password = f"VENDOR_{vendor_id_upper}_PASSWORD"
        canonical_username = f"VENDOR_{vendor_id_upper}_USERNAME"
        canonical_apikey = f"VENDOR_{vendor_id_upper}_APIKEY"
        # 该 vendor 的所有 alias 候选（按 ALLOWED_ALIASES 反向匹配）
        alias_candidates: List[str] = []
        for key, aliases in ALLOWED_ALIASES.items():
            if key.startswith(f"VENDOR_{vendor_id_upper}_"):
                alias_candidates.extend(aliases)

        # 1) 收集 env 中该 vendor 的所有非空凭据
        env_hits: Dict[str, str] = {}
        all_keys = {
            canonical_token,
            canonical_password,
            canonical_username,
            canonical_apikey,
            *alias_candidates,
        }
        for key in all_keys:
            val = env.get(key)
            if val:
                env_hits[key] = val

        chosen_key: Optional[str] = None
        chosen_value: Optional[str] = None
        if env_hits:
            # 优先级：alias（更具体） > 规范名（_TOKEN / _PASSWORD / _USERNAME / _APIKEY）
            # 1. alias 命中（按 alias_candidates 顺序）
            for k in alias_candidates:
                if k in env_hits:
                    chosen_key = k
                    break
            # 2. 规范名
            if chosen_key is None:
                for k in (
                    canonical_token,
                    canonical_password,
                    canonical_username,
                    canonical_apikey,
                ):
                    if k in env_hits:
                        chosen_key = k
                        break
            chosen_value = env_hits[chosen_key] if chosen_key else None

            # 重复凭据 warning：env_hits > 1 时报警（alias + 规范名同时设置）
            if len(env_hits) > 1:
                dup_keys = sorted(env_hits.keys())
                logger.warning(
                    "重复凭据：%s 同时设置了 %s；优先使用 alias %s（更具体），建议保留一种命名",
                    vendor_id,
                    " + ".join(dup_keys),
                    chosen_key,
                )

        # 优先级：env > yaml > unset
        if chosen_value is not None:
            tokens[vendor_id] = chosen_value
            sources[f"token:{vendor_id}"] = f"env {chosen_key}"
        elif vendor_id in yaml_tokens and yaml_tokens[vendor_id]:
            # 已在 _parse_vendor_section 决定 yaml 字段名
            yaml_field = _yaml_token_field(vendor_block, vendor_id)
            sources[f"token:{vendor_id}"] = f"config.yaml vendor.{vendor_id}.{yaml_field}"
        # else: 不设置（启动期不强制凭据）

    # 补全 yaml 中存在但 env 没设置的 vendor 的 source 标注
    for vendor_id, host_val in yaml_hosts.items():
        if f"host:{vendor_id}" not in sources:
            sources[f"host:{vendor_id}"] = f"config.yaml vendor.{vendor_id}.host"

    return hosts, tokens, sources


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------


def load_config(
    path: Optional[os.PathLike] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> Config:
    """按 env > config.yaml > 默认 合并配置。

    * 文件不存在时返回空 dict（CLI 允许"无配置"启动）；
    * 文件存在但解析失败抛 ``RuntimeError``；
    * env 参数为 ``None`` 时从 :data:`os.environ` 读；测试可注入 ``env=...``；
    * 返回 :class:`Config` dataclass 同时记录每个字段的来源，便于排查。

    启动期输出（INFO 级日志）：

      * ``Using source: config.yaml <path>`` — YAML 文件被加载；
      * ``Using source: env VENDOR_HIS_HOST`` — env 命中某字段；
      * ``Using source: config.yaml vendor.his.host`` — YAML 命中某字段；
      * ``Using source: default`` — 默认值兜底；
      * ``重复凭据：his 同时设置了 ...`` — env 同时设置规范名 + alias。
    """

    if env is None:
        env = os.environ

    cfg_path = _config_path(path, env)
    raw = _load_yaml(cfg_path, env)

    # 输出路径字段
    handover_dir, h_src = _resolve_path_field(
        env_var="VENDOR_DOCTOR_HANDOVER_DIR",
        config_keys=["vendor.doctor.handover_dir"],
        default=_DEFAULT_HANDOVER_DIR,
        raw_config=raw,
        env=env,
    )
    tickets_dir, t_src = _resolve_path_field(
        env_var="VENDOR_DOCTOR_TICKETS_DIR",
        config_keys=["vendor.doctor.tickets_dir"],
        default=_DEFAULT_TICKETS_DIR,
        raw_config=raw,
        env=env,
    )
    sqlite_path, db_src = _resolve_path_field(
        env_var="VENDOR_DOCTOR_DB",
        config_keys=["vendor.doctor.sqlite_path"],
        default=_DEFAULT_SQLITE_PATH,
        raw_config=raw,
        env=env,
    )
    log_level, ll_src = _resolve_str_field(
        env_var="VENDOR_DOCTOR_LOG_LEVEL",
        config_keys=["vendor.doctor.log_level"],
        default=DEFAULT_LOG_LEVEL,
        raw_config=raw,
        env=env,
    )
    validate_creds, vc_src = _resolve_bool_field(
        env_var="VENDOR_DOCTOR_VALIDATE_CREDENTIALS",
        config_keys=["vendor.doctor.validate_credentials"],
        default=False,
        raw_config=raw,
        env=env,
    )

    # vendor.* 段
    yaml_hosts, yaml_tokens = _parse_vendor_section(raw)
    hosts, tokens, vendor_sources = _merge_env_credentials(yaml_hosts, yaml_tokens, raw, env)

    sources: Dict[str, str] = {
        "handover_dir": h_src,
        "tickets_dir": t_src,
        "sqlite_path": db_src,
        "log_level": ll_src,
        "validate_credentials": vc_src,
        **vendor_sources,
    }

    cfg = Config(
        config_path=cfg_path if cfg_path.exists() else None,
        handover_dir=handover_dir,
        tickets_dir=tickets_dir,
        sqlite_path=sqlite_path,
        log_level=log_level,
        validate_credentials=validate_creds,
        vendor_hosts=hosts,
        vendor_tokens=tokens,
        raw=raw,
        sources=sources,
    )

    # 来源日志（INFO 级；后续 CLI 可 setup_logging() 控制输出）
    logger.info("Using source: config.yaml %s", cfg_path)
    for key in (
        "handover_dir",
        "tickets_dir",
        "sqlite_path",
        "log_level",
        "validate_credentials",
    ):
        logger.info("Using source: %s for %s", sources[key], key)
    for vendor_key in sorted(sources.keys()):
        if vendor_key.startswith("host:") or vendor_key.startswith("token:"):
            logger.info("Using source: %s for %s", sources[vendor_key], vendor_key)

    return cfg


def load_yaml(
    path: Optional[os.PathLike] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """仅加载 YAML 不做合并 — ``load_config`` 的简化版（保留旧 API 兼容）。"""

    if env is None:
        env = os.environ
    return _load_yaml(_config_path(path, env), env)


def _load_yaml(cfg_path: Path, env: Mapping[str, str]) -> Dict[str, Any]:
    """读取 YAML 文件；不存在 → 空 dict；解析失败 → 抛 :class:`RuntimeError`。"""

    if not cfg_path.exists():
        logger.info("无配置文件 %s，使用默认配置", cfg_path)
        return {}
    try:
        text = cfg_path.read_text(encoding="utf-8")
        data = yaml.safe_load(text) or {}
    except (yaml.YAMLError, OSError) as exc:
        raise RuntimeError(f"配置文件解析失败 {cfg_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"配置文件 {cfg_path} 根必须是 mapping（key: value）")
    # 来源日志（打印前先 redact）
    redacted = redact_dict(data)
    logger.info("Using source: config.yaml %s (loaded %d keys)", cfg_path, len(data))
    return data


def resolve_alias(env_name: str, env: Mapping[str, str]) -> Optional[str]:
    """查 :data:`ALLOWED_ALIASES` 别名映射，找到第一个非空值。

    * 直接命中 ``env_name`` → 返回该值；
    * 否则按 ``ALLOWED_ALIASES[env_name]`` 列表顺序查找；
    * 都未命中 → 返回 ``None``。

    本函数保持原有签名（旧测试兼容）；env 同时设置规范名 + alias 时，alias 优先。
    """

    if env_name in env and env[env_name]:
        # 直接命中：若同时存在 alias 则优先 alias（更具体）
        aliases = ALLOWED_ALIASES.get(env_name, [])
        for alias in aliases:
            if alias in env and env[alias]:
                logger.warning(
                    "Using alias: env %s (= %s 的 alias) 已设置；alias %s 优先使用",
                    env_name,
                    alias,
                    alias,
                )
                return env[alias]
        return env[env_name]
    for alias in ALLOWED_ALIASES.get(env_name, []):
        if alias in env and env[alias]:
            if alias != env_name:
                logger.warning(
                    "Using alias: env %s (= %s 的 alias) 已设置，作为 fallback 使用",
                    env_name,
                    alias,
                )
            return env[alias]
    return None


def detect_duplicate_credentials(env: Mapping[str, str]) -> List[str]:
    """检测 env 中同一 vendor 的规范名 + alias 同时设置的重复凭据。

    返回有重复的 vendor_id 列表（每个 vendor 一项）；供 CLI / 测试用。
    """

    out: List[str] = []
    for env_name in env:
        if not env_name.startswith("VENDOR_"):
            continue
        # 仅匹配形如 VENDOR_<NAME>_<TYPE> 结尾，TYPE ∈
        # {TOKEN, AUTH_TOKEN, PASSWORD, AUTH_USER, USERNAME, APIKEY}
        matched_suffix: Optional[str] = None
        for suffix in (
            "_AUTH_TOKEN",
            "_AUTH_USER",
            "_AUTH_APK",
            "_TOKEN",
            "_PASSWORD",
            "_USERNAME",
            "_APIKEY",
        ):
            if env_name.endswith(suffix) and len(env_name) > len(suffix):
                matched_suffix = suffix
                break
        if matched_suffix is None:
            continue
        name = env_name[len("VENDOR_"):-len(matched_suffix)]
        if not name:
            continue
        # alias 表命中
        aliases: List[str] = []
        aliases.extend(ALLOWED_ALIASES.get(f"VENDOR_{name}_TOKEN", []))
        aliases.extend(ALLOWED_ALIASES.get(f"VENDOR_{name}_PASSWORD", []))
        aliases.extend(ALLOWED_ALIASES.get(f"VENDOR_{name}_APIKEY", []))
        for alias in aliases:
            if alias in env and env[alias] and alias != env_name:
                vendor_id = name.lower()
                if vendor_id not in out:
                    out.append(vendor_id)
                break
    return out


__all__ = [
    "ALLOWED_ALIASES",
    "Config",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_LOG_LEVEL",
    "detect_duplicate_credentials",
    "load_config",
    "load_yaml",
    "redact_dict",
    "redact_env_for_log",
    "redact_str",
    "resolve_alias",
]
