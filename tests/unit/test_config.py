"""task 21 · cli/config.py 单元测试。

覆盖：
* **三种来源优先级**：env > config.yaml > 默认；
* **四类 alias**：VENDOR_<NAME>_TOKEN / _AUTH_TOKEN / _PASSWORD / _APIKEY / _USERNAME
  + ALLOWED_ALIASES 映射；
* **来源日志**：每条字段打印 ``Using source: env VENDOR_X`` /
  ``Using source: config.yaml vendor.x`` / ``Using source: default``；
* **重复凭据**：env 同时设置规范名 + alias 时优先 alias 并 warning；
* **凭据纪律**：日志/产物不含 token 字面量；
* **Config dataclass**：source_of() / sources dict 正确。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Mapping

import pytest

from vendor_if_doctor.cli.config import (
    ALLOWED_ALIASES,
    Config,
    DEFAULT_CONFIG_PATH,
    DEFAULT_LOG_LEVEL,
    detect_duplicate_credentials,
    load_config,
    load_yaml,
    resolve_alias,
)


# ---------------------------------------------------------------------------
# autouse fixture：测试隔离
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_vendor_if_doctor_logger_propagation():
    """让 ``vendor_if_doctor`` 包 logger 在本测试期间 ``propagate=True``，
    以便 :func:`caplog` 能捕获到 ``vendor_if_doctor.cli.config`` 的 warning。

    其他测试（如 ``test_redact``）调用 :func:`setup_logging` 会把
    ``vendor_if_doctor`` logger 的 ``propagate`` 设为 ``False``，导致 caplog
    后续抓不到本 logger 的子 logger 日志。本 fixture 在每个 test 入口处
    重置 propagate=True；测试结束自动还原（pytest fixture 销毁）。
    """

    pkg_logger = logging.getLogger("vendor_if_doctor")
    saved = pkg_logger.propagate
    pkg_logger.propagate = True
    try:
        yield
    finally:
        pkg_logger.propagate = saved


# ---------------------------------------------------------------------------
# 通用 fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def empty_env() -> Dict[str, str]:
    """不污染 os.environ 的空 env。"""

    return {}


@pytest.fixture
def sample_yaml(tmp_path: Path) -> Path:
    """写一份临时 config.yaml。"""

    cfg = tmp_path / "vendor_if_doctor.yaml"
    cfg.write_text(
        """\
vendor:
  doctor:
    handover_dir: ./reports/handover
    tickets_dir: ./reports/tickets
    sqlite_path: ./reports/history.db
    log_level: WARNING
    validate_credentials: true
  his:
    host: yaml.his.local
    token: yaml-his-token
  lis:
    host: yaml.lis.local
  pacs:
    host: yaml.pacs.local
    apikey: yaml-pacs-apikey
  mi:
    host: yaml.mi.local
    username: yaml-mi-user
    password: yaml-mi-password
""",
        encoding="utf-8",
    )
    return cfg


@pytest.fixture
def config_with_sample_yaml(sample_yaml: Path) -> Config:
    """用临时 YAML 加载 Config；env 为空。"""

    return load_config(sample_yaml, env={})


# ---------------------------------------------------------------------------
# 1. Config dataclass 基本结构
# ---------------------------------------------------------------------------


class TestConfigDataclass:
    def test_config_dataclass_has_required_fields(self, sample_yaml: Path, config_with_sample_yaml: Config):
        cfg = config_with_sample_yaml
        assert isinstance(cfg, Config)
        assert cfg.config_path == sample_yaml
        assert cfg.handover_dir == Path("./reports/handover")
        assert cfg.tickets_dir == Path("./reports/tickets")
        assert cfg.sqlite_path == Path("./reports/history.db")
        assert cfg.log_level == "WARNING"
        assert cfg.validate_credentials is True

    def test_config_vendor_hosts_from_yaml(self, config_with_sample_yaml: Config):
        cfg = config_with_sample_yaml
        assert cfg.vendor_hosts["his"] == "yaml.his.local"
        assert cfg.vendor_hosts["lis"] == "yaml.lis.local"
        assert cfg.vendor_hosts["pacs"] == "yaml.pacs.local"
        assert cfg.vendor_hosts["mi"] == "yaml.mi.local"

    def test_config_vendor_tokens_from_yaml(self, config_with_sample_yaml: Config):
        cfg = config_with_sample_yaml
        assert cfg.vendor_tokens["his"] == "yaml-his-token"
        assert cfg.vendor_tokens["pacs"] == "yaml-pacs-apikey"
        assert cfg.vendor_tokens["mi"] == "yaml-mi-password"

    def test_config_source_of_returns_dict_value(self, config_with_sample_yaml: Config):
        cfg = config_with_sample_yaml
        assert cfg.source_of("handover_dir") == "config.yaml vendor.doctor.handover_dir"
        assert cfg.source_of("host:his") == "config.yaml vendor.his.host"
        assert cfg.source_of("token:his") == "config.yaml vendor.his.token"

    def test_config_source_of_default_for_missing_key(self, config_with_sample_yaml: Config):
        cfg = config_with_sample_yaml
        assert cfg.source_of("nonexistent_key") == "default"


# ---------------------------------------------------------------------------
# 2. 默认值（无 YAML 无 env）
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_no_yaml_no_env_returns_defaults(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        cfg = load_config(tmp_path / "missing.yaml", env={})
        assert cfg.handover_dir == Path("out")
        assert cfg.tickets_dir == Path("out")
        assert cfg.sqlite_path == Path("vendor_if_doctor.db")
        assert cfg.log_level == DEFAULT_LOG_LEVEL
        assert cfg.validate_credentials is False
        assert cfg.vendor_hosts == {}
        assert cfg.vendor_tokens == {}
        # 来源都是 default
        assert cfg.source_of("handover_dir") == "default"
        assert cfg.source_of("tickets_dir") == "default"
        assert cfg.source_of("sqlite_path") == "default"
        assert cfg.source_of("log_level") == "default"
        assert cfg.source_of("validate_credentials") == "default"

    def test_no_yaml_default_path_resolution(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        # 不传 path → 走默认路径（cwd / vendor_if_doctor.yaml），不存在 → 返回空
        cfg = load_config(env={})
        assert cfg.config_path is None  # 文件不存在 → None

    def test_env_var_overrides_default_for_missing_yaml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        cfg = load_config(
            tmp_path / "missing.yaml",
            env={
                "VENDOR_DOCTOR_HANDOVER_DIR": "/tmp/test-handover",
                "VENDOR_DOCTOR_LOG_LEVEL": "DEBUG",
            },
        )
        assert cfg.handover_dir == Path("/tmp/test-handover")
        assert cfg.log_level == "DEBUG"
        assert cfg.source_of("handover_dir") == "env VENDOR_DOCTOR_HANDOVER_DIR"
        assert cfg.source_of("log_level") == "env VENDOR_DOCTOR_LOG_LEVEL"


# ---------------------------------------------------------------------------
# 3. env 覆盖 yaml（优先级 #1）
# ---------------------------------------------------------------------------


class TestEnvOverridesYaml:
    def test_env_handover_dir_overrides_yaml(
        self, sample_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.chdir(tmp_path)
        cfg = load_config(
            sample_yaml,
            env={
                "VENDOR_DOCTOR_HANDOVER_DIR": "/env/override/handover",
            },
        )
        assert cfg.handover_dir == Path("/env/override/handover")
        assert cfg.source_of("handover_dir") == "env VENDOR_DOCTOR_HANDOVER_DIR"

    def test_env_vendor_host_overrides_yaml_host(
        self, sample_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.chdir(tmp_path)
        cfg = load_config(
            sample_yaml,
            env={
                "VENDOR_HIS_HOST": "env.his.local",
                "VENDOR_LIS_HOST": "env.lis.local",
            },
        )
        assert cfg.vendor_hosts["his"] == "env.his.local"
        assert cfg.vendor_hosts["lis"] == "env.lis.local"
        # pacs / mi 不在 env 中 → 仍走 yaml
        assert cfg.vendor_hosts["pacs"] == "yaml.pacs.local"
        assert cfg.vendor_hosts["mi"] == "yaml.mi.local"
        assert cfg.source_of("host:his") == "env VENDOR_HIS_HOST"
        assert cfg.source_of("host:pacs") == "config.yaml vendor.pacs.host"

    def test_env_vendor_token_overrides_yaml_token(
        self, sample_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.chdir(tmp_path)
        cfg = load_config(
            sample_yaml,
            env={
                "VENDOR_HIS_TOKEN": "env-his-token-9999",
            },
        )
        assert cfg.vendor_tokens["his"] == "env-his-token-9999"
        assert cfg.source_of("token:his") == "env VENDOR_HIS_TOKEN"

    def test_env_sqlite_path_overrides_yaml(
        self, sample_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.chdir(tmp_path)
        cfg = load_config(
            sample_yaml,
            env={
                "VENDOR_DOCTOR_DB": "/var/lib/vendor_if_doctor.db",
            },
        )
        assert cfg.sqlite_path == Path("/var/lib/vendor_if_doctor.db")
        assert cfg.source_of("sqlite_path") == "env VENDOR_DOCTOR_DB"

    def test_env_validate_credentials_true_overrides_yaml_true(
        self, sample_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.chdir(tmp_path)
        cfg = load_config(
            sample_yaml,
            env={
                "VENDOR_DOCTOR_VALIDATE_CREDENTIALS": "true",
            },
        )
        assert cfg.validate_credentials is True
        assert "env VENDOR_DOCTOR_VALIDATE_CREDENTIALS" in cfg.source_of("validate_credentials")

    def test_env_validate_credentials_various_truthy_values(
        self, sample_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.chdir(tmp_path)
        for truthy in ["1", "true", "yes", "on", "TRUE", "Yes", "On"]:
            cfg = load_config(
                sample_yaml,
                env={"VENDOR_DOCTOR_VALIDATE_CREDENTIALS": truthy},
            )
            assert cfg.validate_credentials is True, f"truthy={truthy!r} should be True"

    def test_env_validate_credentials_falsy_falls_back_to_yaml(
        self, sample_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.chdir(tmp_path)
        # 空字符串视为未设置
        cfg = load_config(
            sample_yaml,
            env={"VENDOR_DOCTOR_VALIDATE_CREDENTIALS": ""},
        )
        # yaml 是 true → 走 yaml
        assert cfg.validate_credentials is True
        assert cfg.source_of("validate_credentials") == "config.yaml vendor.doctor.validate_credentials"


# ---------------------------------------------------------------------------
# 4. yaml 覆盖默认（优先级 #2）
# ---------------------------------------------------------------------------


class TestYamlOverridesDefault:
    def test_yaml_handover_overrides_default(
        self, sample_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.chdir(tmp_path)
        cfg = load_config(sample_yaml, env={})
        # yaml = ./reports/handover, default = ./out
        assert cfg.handover_dir == Path("./reports/handover")
        assert cfg.source_of("handover_dir") == "config.yaml vendor.doctor.handover_dir"

    def test_yaml_log_level_overrides_default(
        self, sample_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.chdir(tmp_path)
        cfg = load_config(sample_yaml, env={})
        assert cfg.log_level == "WARNING"
        assert cfg.source_of("log_level") == "config.yaml vendor.doctor.log_level"


# ---------------------------------------------------------------------------
# 5. Alias 解析（4 类）
# ---------------------------------------------------------------------------


class TestAliasResolution:
    def test_alias_fallback_when_canonical_unset(
        self, sample_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """VENDOR_HIS_TOKEN 未设置，VENDOR_HIS_AUTH_TOKEN 已设置 → 落回 alias。"""

        monkeypatch.chdir(tmp_path)
        cfg = load_config(
            sample_yaml,
            env={"VENDOR_HIS_AUTH_TOKEN": "alias-his-token-7777"},
        )
        assert cfg.vendor_tokens["his"] == "alias-his-token-7777"
        assert cfg.source_of("token:his") == "env VENDOR_HIS_AUTH_TOKEN"

    def test_alias_for_password(
        self, sample_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """VENDOR_MI_PASSWORD 未设置，VENDOR_MI_AUTH_TOKEN 已设置 → 落回 alias。"""

        monkeypatch.chdir(tmp_path)
        cfg = load_config(
            sample_yaml,
            env={"VENDOR_MI_AUTH_TOKEN": "alias-mi-token-8888"},
        )
        assert cfg.vendor_tokens["mi"] == "alias-mi-token-8888"
        assert cfg.source_of("token:mi") == "env VENDOR_MI_AUTH_TOKEN"

    def test_alias_for_apikey(
        self, sample_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """VENDOR_PACS_APIKEY 未设置，VENDOR_PACS_AUTH_TOKEN 已设置 → 落回 alias。"""

        monkeypatch.chdir(tmp_path)
        cfg = load_config(
            sample_yaml,
            env={"VENDOR_PACS_AUTH_TOKEN": "alias-pacs-token-6666"},
        )
        assert cfg.vendor_tokens["pacs"] == "alias-pacs-token-6666"
        assert cfg.source_of("token:pacs") == "env VENDOR_PACS_AUTH_TOKEN"

    def test_alias_for_username(
        self, sample_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """VENDOR_MI_USERNAME 未设置，VENDOR_MI_AUTH_USER 已设置 → 落回 alias。"""

        monkeypatch.chdir(tmp_path)
        cfg = load_config(
            sample_yaml,
            env={"VENDOR_MI_AUTH_USER": "alias-mi-user-5555"},
        )
        # username 在 vendor_tokens 里以 alias 命中填入
        assert cfg.vendor_tokens["mi"] == "alias-mi-user-5555"
        assert cfg.source_of("token:mi") == "env VENDOR_MI_AUTH_USER"

    def test_no_alias_no_yaml_token_returns_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """env 与 yaml 都没设置 token → vendor_tokens 不含该 vendor。"""

        monkeypatch.chdir(tmp_path)
        cfg = load_config(env={"VENDOR_HIS_HOST": "h.local"}, env_path_strict=False) if False else load_config(
            tmp_path / "missing.yaml", env={"VENDOR_HIS_HOST": "h.local"}
        )
        assert "his" not in cfg.vendor_tokens
        assert "his" in cfg.vendor_hosts  # host 仍设了


# ---------------------------------------------------------------------------
# 6. 重复凭据：env 同时设规范名 + alias，alias 优先 + warning
# ---------------------------------------------------------------------------


class TestDuplicateCredentials:
    def test_duplicate_token_and_auth_token_warning(
        self, sample_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        monkeypatch.chdir(tmp_path)
        with caplog.at_level(logging.WARNING, logger="vendor_if_doctor.cli.config"):
            cfg = load_config(
                sample_yaml,
                env={
                    "VENDOR_HIS_TOKEN": "canonical-token-aaa",
                    "VENDOR_HIS_AUTH_TOKEN": "alias-token-bbb",
                },
            )
        # alias 优先
        assert cfg.vendor_tokens["his"] == "alias-token-bbb"
        assert cfg.source_of("token:his") == "env VENDOR_HIS_AUTH_TOKEN"
        # warning 出现
        warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("重复凭据" in msg and "his" in msg for msg in warnings), warnings

    def test_duplicate_password_and_auth_token_warning(
        self, sample_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        monkeypatch.chdir(tmp_path)
        with caplog.at_level(logging.WARNING, logger="vendor_if_doctor.cli.config"):
            cfg = load_config(
                sample_yaml,
                env={
                    "VENDOR_MI_PASSWORD": "canonical-pass-ccc",
                    "VENDOR_MI_AUTH_TOKEN": "alias-token-ddd",
                },
            )
        # alias 优先（更具体名字）
        assert cfg.vendor_tokens["mi"] == "alias-token-ddd"
        warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("重复凭据" in msg and "mi" in msg for msg in warnings), warnings

    def test_no_warning_when_only_canonical_set(
        self, sample_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        monkeypatch.chdir(tmp_path)
        with caplog.at_level(logging.WARNING, logger="vendor_if_doctor.cli.config"):
            cfg = load_config(
                sample_yaml,
                env={"VENDOR_HIS_TOKEN": "canonical-only-eee"},
            )
        assert cfg.vendor_tokens["his"] == "canonical-only-eee"
        warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert not any("重复凭据" in msg for msg in warnings), warnings

    def test_no_warning_when_only_alias_set(
        self, sample_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        monkeypatch.chdir(tmp_path)
        with caplog.at_level(logging.WARNING, logger="vendor_if_doctor.cli.config"):
            cfg = load_config(
                sample_yaml,
                env={"VENDOR_HIS_AUTH_TOKEN": "alias-only-fff"},
            )
        assert cfg.vendor_tokens["his"] == "alias-only-fff"
        warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        # alias 单独设置不算重复
        assert not any("重复凭据" in msg for msg in warnings), warnings


# ---------------------------------------------------------------------------
# 7. 来源日志
# ---------------------------------------------------------------------------


class TestSourceLogging:
    def test_logging_includes_using_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        """yaml 只覆盖部分字段，余下走 default，验证三种来源日志都出现。"""

        monkeypatch.chdir(tmp_path)
        yaml_path = tmp_path / "partial.yaml"
        yaml_path.write_text(
            "vendor:\n  doctor:\n    handover_dir: ./reports/handover\n",
            encoding="utf-8",
        )
        with caplog.at_level(logging.INFO, logger="vendor_if_doctor.cli.config"):
            cfg = load_config(
                yaml_path,
                env={"VENDOR_HIS_HOST": "env.his.local"},
            )
        messages = [r.message for r in caplog.records]
        assert any("Using source: config.yaml" in msg for msg in messages)
        assert any("env VENDOR_HIS_HOST" in msg for msg in messages)
        assert any("config.yaml vendor.doctor.handover_dir" in msg for msg in messages)
        # tickets_dir / sqlite_path / log_level / validate_credentials 都走 default
        assert any("default for tickets_dir" in msg for msg in messages)
        assert any("default for sqlite_path" in msg for msg in messages)
        assert any("default for log_level" in msg for msg in messages)

    def test_logging_does_not_leak_credentials(
        self, sample_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        """日志中不应出现 token 字面量。"""

        monkeypatch.chdir(tmp_path)
        with caplog.at_level(logging.INFO, logger="vendor_if_doctor.cli.config"):
            cfg = load_config(
                sample_yaml,
                env={
                    "VENDOR_HIS_TOKEN": "SUPER-SECRET-TOKEN-XYZ-LEAK-CHECK",
                    "VENDOR_HIS_AUTH_TOKEN": "ANOTHER-SECRET-AUTH-TOKEN-LEAK-CHECK",
                },
            )
        # 拼接所有消息
        all_msg = "\n".join(r.message for r in caplog.records)
        # token 字面量不应进入日志
        assert "SUPER-SECRET-TOKEN-XYZ-LEAK-CHECK" not in all_msg
        assert "ANOTHER-SECRET-AUTH-TOKEN-LEAK-CHECK" not in all_msg


# ---------------------------------------------------------------------------
# 8. ALLOWED_ALIASES 数据完整性
# ---------------------------------------------------------------------------


class TestAllowedAliasesData:
    def test_allowed_aliases_contains_his_token_alias(self):
        assert "VENDOR_HIS_TOKEN" in ALLOWED_ALIASES
        assert "VENDOR_HIS_AUTH_TOKEN" in ALLOWED_ALIASES["VENDOR_HIS_TOKEN"]

    def test_allowed_aliases_contains_his_password_alias(self):
        assert "VENDOR_HIS_PASSWORD" in ALLOWED_ALIASES
        assert "VENDOR_HIS_AUTH_TOKEN" in ALLOWED_ALIASES["VENDOR_HIS_PASSWORD"]

    def test_allowed_aliases_contains_lis_token_alias(self):
        assert "VENDOR_LIS_TOKEN" in ALLOWED_ALIASES
        assert "VENDOR_LIS_AUTH_TOKEN" in ALLOWED_ALIASES["VENDOR_LIS_TOKEN"]

    def test_allowed_aliases_contains_pacs_apikey_alias(self):
        assert "VENDOR_PACS_APIKEY" in ALLOWED_ALIASES
        assert "VENDOR_PACS_AUTH_TOKEN" in ALLOWED_ALIASES["VENDOR_PACS_APIKEY"]

    def test_allowed_aliases_contains_mi_aliases(self):
        assert "VENDOR_MI_PASSWORD" in ALLOWED_ALIASES
        assert "VENDOR_MI_AUTH_TOKEN" in ALLOWED_ALIASES["VENDOR_MI_PASSWORD"]
        assert "VENDOR_MI_USERNAME" in ALLOWED_ALIASES
        assert "VENDOR_MI_AUTH_USER" in ALLOWED_ALIASES["VENDOR_MI_USERNAME"]


# ---------------------------------------------------------------------------
# 9. resolve_alias（向后兼容）
# ---------------------------------------------------------------------------


class TestResolveAliasLegacy:
    def test_resolve_alias_direct_hit(self):
        env = {"VENDOR_HIS_TOKEN": "direct-value"}
        assert resolve_alias("VENDOR_HIS_TOKEN", env) == "direct-value"

    def test_resolve_alias_alias_hit(self):
        env = {"VENDOR_HIS_AUTH_TOKEN": "alias-value"}
        assert resolve_alias("VENDOR_HIS_TOKEN", env) == "alias-value"

    def test_resolve_alias_no_hit(self):
        assert resolve_alias("VENDOR_UNKNOWN", {}) is None

    def test_resolve_alias_direct_and_alias_prefers_alias(self):
        """当直接命中且 alias 也命中时，alias 优先（旧 API 行为）。"""

        env = {"VENDOR_HIS_TOKEN": "direct", "VENDOR_HIS_AUTH_TOKEN": "alias"}
        # 直接命中 → 但 alias 也命中 → alias 优先
        assert resolve_alias("VENDOR_HIS_TOKEN", env) == "alias"

    def test_resolve_alias_alias_only_when_direct_empty(self):
        env = {"VENDOR_HIS_AUTH_TOKEN": "alias-only", "VENDOR_HIS_TOKEN": ""}
        # 直接命中但为空字符串 → alias 落回
        assert resolve_alias("VENDOR_HIS_TOKEN", env) == "alias-only"


# ---------------------------------------------------------------------------
# 10. detect_duplicate_credentials
# ---------------------------------------------------------------------------


class TestDetectDuplicate:
    def test_detect_duplicate_with_token_and_auth(self):
        env = {
            "VENDOR_HIS_TOKEN": "x",
            "VENDOR_HIS_AUTH_TOKEN": "y",
        }
        assert "his" in detect_duplicate_credentials(env)

    def test_detect_duplicate_with_password_and_auth(self):
        env = {
            "VENDOR_MI_PASSWORD": "x",
            "VENDOR_MI_AUTH_TOKEN": "y",
        }
        assert "mi" in detect_duplicate_credentials(env)

    def test_detect_no_duplicate_only_canonical(self):
        env = {"VENDOR_HIS_TOKEN": "x"}
        assert "his" not in detect_duplicate_credentials(env)

    def test_detect_no_duplicate_only_alias(self):
        env = {"VENDOR_HIS_AUTH_TOKEN": "x"}
        assert "his" not in detect_duplicate_credentials(env)

    def test_detect_empty_env(self):
        assert detect_duplicate_credentials({}) == []

    def test_detect_does_not_misclassify_unrelated_env_vars(self):
        env = {"PATH": "/usr/bin", "HOME": "/root", "VENDOR_HIS_TOKEN": "x"}
        assert detect_duplicate_credentials(env) == []


# ---------------------------------------------------------------------------
# 11. load_yaml 简化接口
# ---------------------------------------------------------------------------


class TestLoadYaml:
    def test_load_yaml_returns_dict(self, sample_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        data = load_yaml(sample_yaml, env={})
        assert isinstance(data, dict)
        assert "vendor" in data
        assert data["vendor"]["doctor"]["handover_dir"] == "./reports/handover"

    def test_load_yaml_missing_returns_empty_dict(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        data = load_yaml(tmp_path / "missing.yaml", env={})
        assert data == {}

    def test_load_yaml_invalid_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        bad = tmp_path / "bad.yaml"
        bad.write_text("[unclosed", encoding="utf-8")
        with pytest.raises(RuntimeError):
            load_yaml(bad, env={})

    def test_load_yaml_root_not_mapping_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        bad = tmp_path / "list.yaml"
        bad.write_text("- item1\n- item2\n", encoding="utf-8")
        with pytest.raises(RuntimeError):
            load_yaml(bad, env={})


# ---------------------------------------------------------------------------
# 12. _config_path 路径解析（不污染 os.environ）
# ---------------------------------------------------------------------------


class TestConfigPathResolution:
    def test_config_path_explicit_arg_wins(
        self, sample_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.chdir(tmp_path)
        # 入参 path 优先
        cfg = load_config(sample_yaml, env={"VENDOR_DOCTOR_CONFIG": str(tmp_path / "other.yaml")})
        assert cfg.config_path == sample_yaml

    def test_config_path_env_var_used_when_no_arg(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.chdir(tmp_path)
        # 在 tmp_path 写一个 yaml
        explicit_yaml = tmp_path / "explicit.yaml"
        explicit_yaml.write_text("vendor: {}\n", encoding="utf-8")
        cfg = load_config(
            None,
            env={"VENDOR_DOCTOR_CONFIG": str(explicit_yaml)},
        )
        assert cfg.config_path == explicit_yaml

    def test_config_path_does_not_mutate_os_environ(
        self, sample_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """load_config 不会污染调用方的 os.environ。"""

        monkeypatch.chdir(tmp_path)
        before = dict(os.environ)
        load_config(sample_yaml, env={"VENDOR_HIS_TOKEN": "x"})
        after = dict(os.environ)
        assert "VENDOR_HIS_TOKEN" not in after
        # os.environ 应等于 before（确保不写入）
        for k in before:
            assert after.get(k) == before[k], f"os.environ changed: {k}"


# ---------------------------------------------------------------------------
# 13. 实际任务验收（与 task 21 description 完全对齐）
# ---------------------------------------------------------------------------


class TestTask21Acceptance:
    """对齐 task 21 description 的「手动验证」两条："""

    def test_manual_verify_env_overrides_yaml_with_source_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        """env VENDOR_HIS_HOST=test.com + config.yaml vendor.his.host=other.com →
        load_config 返回 test.com，日志 'Using source: env VENDOR_HIS_HOST'。"""

        monkeypatch.chdir(tmp_path)
        yaml_path = tmp_path / "c.yaml"
        yaml_path.write_text(
            "vendor:\n  his:\n    host: other.com\n",
            encoding="utf-8",
        )
        with caplog.at_level(logging.INFO, logger="vendor_if_doctor.cli.config"):
            cfg = load_config(
                yaml_path,
                env={"VENDOR_HIS_HOST": "test.com"},
            )
        assert cfg.vendor_hosts["his"] == "test.com"
        log_msgs = [r.message for r in caplog.records]
        assert any("env VENDOR_HIS_HOST" in msg for msg in log_msgs), log_msgs
        assert cfg.source_of("host:his") == "env VENDOR_HIS_HOST"

    def test_manual_verify_duplicate_token_warning_alias_priority(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        """env VENDOR_HIS_TOKEN 与 VENDOR_HIS_AUTH_TOKEN 都设置时优先 AUTH_TOKEN
        并 warning 提示重复。"""

        monkeypatch.chdir(tmp_path)
        with caplog.at_level(logging.WARNING, logger="vendor_if_doctor.cli.config"):
            cfg = load_config(
                env={
                    "VENDOR_HIS_TOKEN": "canon-value",
                    "VENDOR_HIS_AUTH_TOKEN": "alias-value",
                },
            )
        # AUTH_TOKEN（alias）赢
        assert cfg.vendor_tokens["his"] == "alias-value"
        # warning
        warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("重复凭据" in w for w in warnings), warnings
        assert any("AUTH_TOKEN" in w or "VENDOR_HIS_AUTH_TOKEN" in w for w in warnings)
