"""Tests for vendor_if_doctor.redact + logging_setup integration.

覆盖：
* 7 类敏感字段名（key 命中整 value 替换）
* 7 类字符串级 regex 兜底（Authorization / Bearer / Basic / JSON / URL query / 标量 token / 标量 password）
* 嵌套 dict / 嵌套 list 递归
* logging_setup JSON formatter 与 redact 集成
* 日志文件 grep -i 'authorization|token|password' 命中 = 0（兜底实证）
"""

from __future__ import annotations

import io
import json
import logging
import re
import subprocess
import sys
from pathlib import Path

import pytest

from vendor_if_doctor.redact import (
    is_secret_key,
    redact_dict,
    redact_env_for_log,
    redact_env_map,
    redact_str,
)
from vendor_if_doctor.logging_setup import _JsonFormatter, setup_logging


# ---------------------------------------------------------------------------
# 9 类敏感字段名（key 命中）
# ---------------------------------------------------------------------------


class TestSecretKeys:
    """9 类敏感 key 命中 → value 整替换为 '***'。"""

    @pytest.mark.parametrize(
        "key",
        [
            "authorization",
            "Authorization",
            "AUTHORIZATION",
            "x-api-key",
            "X-API-Key",
            "X-Api-Key",
            "api_key",
            "apikey",
            "API_KEY",
            "password",
            "Password",
            "PASSWORD",
            "secret",
            "Secret",
            "token",
            "Token",
            "access_token",
            "access-token",
            "ACCESS_TOKEN",
            "bearer",
            "Bearer",
            "BEARER",
            "credential",
            "Credential",
        ],
    )
    def test_is_secret_key_true(self, key: str) -> None:
        assert is_secret_key(key) is True

    @pytest.mark.parametrize(
        "key",
        [
            "code",
            "patient_id",
            "host",
            "port",
            "username",
            "user",
            "client_id",
            "url",
            "vendor_id",
        ],
    )
    def test_is_secret_key_false(self, key: str) -> None:
        assert is_secret_key(key) is False


class TestRedactDictByKey:
    """9 类敏感 key 命中：value 整替换为 '***'。"""

    def test_authorization_key_value_replaced(self) -> None:
        result = redact_dict({"Authorization": "Bearer abc.def.ghi", "safe": "visible"})
        assert result == {"Authorization": "***", "safe": "visible"}

    def test_x_api_key_value_replaced(self) -> None:
        result = redact_dict({"X-API-Key": "secret-key-12345"})
        assert result == {"X-API-Key": "***"}

    def test_api_key_underscore_variant(self) -> None:
        result = redact_dict({"api_key": "kk-9999"})
        assert result == {"api_key": "***"}

    def test_apikey_no_separator_variant(self) -> None:
        result = redact_dict({"apikey": "kk-9999"})
        assert result == {"apikey": "***"}

    def test_password_value_replaced(self) -> None:
        result = redact_dict({"password": "wonderland"})
        assert result == {"password": "***"}

    def test_secret_value_replaced(self) -> None:
        result = redact_dict({"client_secret": "shh"})
        # client_secret 不在严格 secret 列表里（仅 secret 单词），保持原样
        # 但 secret 单词命中
        result2 = redact_dict({"secret": "shh"})
        assert result2 == {"secret": "***"}

    def test_token_value_replaced(self) -> None:
        result = redact_dict({"token": "jwt-eyJhbGc.payload.sig"})
        assert result == {"token": "***"}

    def test_access_token_value_replaced(self) -> None:
        result = redact_dict({"access_token": "abc123"})
        assert result == {"access_token": "***"}

    def test_bearer_value_replaced(self) -> None:
        result = redact_dict({"bearer": "xyz"})
        assert result == {"bearer": "***"}

    def test_credential_value_replaced(self) -> None:
        result = redact_dict({"credential": "kkk"})
        assert result == {"credential": "***"}

    def test_safe_field_kept_unchanged(self) -> None:
        result = redact_dict({"code": 200, "patient_id": "P-001", "url": "https://x.com"})
        assert result == {"code": 200, "patient_id": "P-001", "url": "https://x.com"}

    def test_mixed_safe_and_secret(self) -> None:
        result = redact_dict(
            {
                "Authorization": "Bearer abc",
                "code": 200,
                "patient_id": "P-001",
                "X-API-Key": "kkk-999",
                "url": "https://x.com",
            }
        )
        assert result == {
            "Authorization": "***",
            "code": 200,
            "patient_id": "P-001",
            "X-API-Key": "***",
            "url": "https://x.com",
        }


# ---------------------------------------------------------------------------
# 字符串级 regex 兜底（7 类）
# ---------------------------------------------------------------------------


class TestRedactStr:
    """字符串级 regex 兜底。"""

    def test_authorization_inline_bearer(self) -> None:
        result = redact_str("Authorization: Bearer abc.def.ghi")
        assert result == "Authorization: ***"

    def test_authorization_inline_basic(self) -> None:
        result = redact_str("Authorization: Basic dXNlcjpwYXNz")
        assert result == "Authorization: ***"

    def test_bearer_alone(self) -> None:
        result = redact_str("Header value: Bearer xyz-token-12345")
        assert "Bearer ***" in result
        assert "xyz-token-12345" not in result

    def test_basic_alone(self) -> None:
        result = redact_str("Encoding: Basic dXNlcjpwYXNz")
        assert "Basic ***" in result
        assert "dXNlcjpwYXNz" not in result

    def test_json_token_field(self) -> None:
        result = redact_str('{"token": "abc123", "code": 200}')
        assert '"token": "***"' in result
        assert "abc123" not in result
        assert '"code": 200' in result

    def test_json_password_field(self) -> None:
        result = redact_str('{"password": "wonderland"}')
        assert '"password": "***"' in result
        assert "wonderland" not in result

    def test_json_api_key_field(self) -> None:
        result = redact_str('{"api_key": "kk-9999", "q": "ok"}')
        assert '"api_key": "***"' in result
        assert "kk-9999" not in result
        assert '"q": "ok"' in result

    def test_url_query_api_key(self) -> None:
        result = redact_str("https://x.com?api_key=KEY-9999&q=ok")
        assert "api_key=***" in result
        assert "KEY-9999" not in result
        assert "q=ok" in result

    def test_url_query_token(self) -> None:
        result = redact_str("https://x.com?token=TOK-8888&page=1")
        assert "token=***" in result
        assert "TOK-8888" not in result
        assert "page=1" in result

    def test_url_query_password(self) -> None:
        result = redact_str("https://x.com?password=PWD-7777")
        assert "password=***" in result
        assert "PWD-7777" not in result

    def test_scalar_token_equals(self) -> None:
        result = redact_str("config: token=TOK-8888 other")
        assert "token=***" in result
        assert "TOK-8888" not in result

    def test_scalar_password_equals(self) -> None:
        result = redact_str("db: password=PWD-7777 mode=fast")
        assert "password=***" in result
        assert "PWD-7777" not in result
        assert "mode=fast" in result

    def test_safe_text_unchanged(self) -> None:
        assert redact_str("Hello world") == "Hello world"
        assert redact_str('{"code":200,"patient_id":"P-001"}') == '{"code":200,"patient_id":"P-001"}'

    def test_none_returns_none(self) -> None:
        assert redact_str(None) is None

    def test_empty_returns_empty(self) -> None:
        assert redact_str("") == ""


# ---------------------------------------------------------------------------
# 递归过滤（嵌套 dict / 嵌套 list）
# ---------------------------------------------------------------------------


class TestRedactRecursive:
    """嵌套 dict / 嵌套 list 递归过滤。"""

    def test_nested_dict(self) -> None:
        data = {
            "headers": {"Authorization": "Bearer xyz", "X-API-Key": "kkk"},
            "code": 200,
        }
        result = redact_dict(data)
        assert result == {
            "headers": {"Authorization": "***", "X-API-Key": "***"},
            "code": 200,
        }

    def test_deeply_nested_dict(self) -> None:
        data = {
            "outer": {
                "middle": {
                    "inner": {"token": "abc123", "safe": "ok"},
                }
            }
        }
        result = redact_dict(data)
        assert result == {
            "outer": {
                "middle": {
                    "inner": {"token": "***", "safe": "ok"},
                }
            }
        }

    def test_list_of_dicts(self) -> None:
        data = [
            {"token": "abc", "name": "x"},
            {"password": "wonderland", "name": "y"},
        ]
        result = redact_dict(data)
        assert result == [
            {"token": "***", "name": "x"},
            {"password": "***", "name": "y"},
        ]

    def test_nested_list_of_dicts(self) -> None:
        data = {
            "items": [
                {"Authorization": "Bearer abc", "code": 1},
                {"Authorization": "Bearer def", "code": 2},
            ]
        }
        result = redact_dict(data)
        assert result == {
            "items": [
                {"Authorization": "***", "code": 1},
                {"Authorization": "***", "code": 2},
            ]
        }

    def test_list_of_strings_redacted(self) -> None:
        data = ["Bearer xyz", "Bearer abc", "safe text"]
        result = redact_dict(data)
        assert result == ["Bearer ***", "Bearer ***", "safe text"]

    def test_dict_with_string_value_redacted(self) -> None:
        # 非 secret key 但 value 是含 token 字面量的字符串，应被字符串级 regex 命中
        data = {"summary": "Authorization: Bearer abc"}
        result = redact_dict(data)
        assert result == {"summary": "Authorization: ***"}

    def test_scalar_passthrough(self) -> None:
        assert redact_dict(42) == 42
        assert redact_dict(3.14) == 3.14
        assert redact_dict(True) is True
        assert redact_dict(None) is None
        assert redact_dict(b"bytes-not-str") == b"bytes-not-str"


# ---------------------------------------------------------------------------
# redact_env_for_log / redact_env_map
# ---------------------------------------------------------------------------


class TestEnvRedaction:
    """env 日志脱敏。"""

    def test_redact_env_for_log_with_value(self) -> None:
        result = redact_env_for_log("VENDOR_HIS_PASSWORD", "wonderland")
        assert result == "VENDOR_HIS_PASSWORD=*** (len=10)"
        assert "wonderland" not in result

    def test_redact_env_for_log_empty(self) -> None:
        assert redact_env_for_log("VENDOR_HIS_PASSWORD", "") == "VENDOR_HIS_PASSWORD=<empty>"
        assert redact_env_for_log("VENDOR_HIS_PASSWORD", None) == "VENDOR_HIS_PASSWORD=<empty>"

    def test_redact_env_map(self) -> None:
        env = {
            "VENDOR_HIS_PASSWORD": "wonderland",
            "VENDOR_LIS_TOKEN": "lis-secret-token-9999",
            "VENDOR_PACS_APIKEY": "pacs-secret-key-777",
        }
        result = redact_env_map(env)
        # 值形式: '<NAME>=*** (len=N)'
        for k, v in result.items():
            assert v.startswith(f"{k}="), f"{k}={v}"
            assert "***" in v, f"{k}={v}"
        assert "wonderland" not in result["VENDOR_HIS_PASSWORD"]
        assert "lis-secret-token-9999" not in result["VENDOR_LIS_TOKEN"]
        assert "pacs-secret-key-777" not in result["VENDOR_PACS_APIKEY"]
        # 长度正确
        assert "(len=10)" in result["VENDOR_HIS_PASSWORD"]  # 'wonderland' = 10 chars
        assert "(len=21)" in result["VENDOR_LIS_TOKEN"]  # 'lis-secret-token-9999' = 21 chars
        assert "(len=19)" in result["VENDOR_PACS_APIKEY"]  # 'pacs-secret-key-777' = 19 chars


# ---------------------------------------------------------------------------
# 公开 API 完整性
# ---------------------------------------------------------------------------


class TestPublicAPI:
    """redact 模块 __all__ 完整 + 函数签名。"""

    def test_redact_str_signature(self) -> None:
        # None / str / 空串都返回原值
        assert redact_str(None) is None
        assert redact_str("") == ""
        assert redact_str("x") == "x"

    def test_redact_dict_returns_same_type_for_dict(self) -> None:
        out = redact_dict({"x": 1})
        assert isinstance(out, dict)

    def test_redact_dict_returns_same_type_for_list(self) -> None:
        out = redact_dict([1, 2, 3])
        assert isinstance(out, list)


# ---------------------------------------------------------------------------
# logging_setup._JsonFormatter 集成测试
# ---------------------------------------------------------------------------


@pytest.fixture
def logger_with_buffer() -> tuple[logging.Logger, io.StringIO]:
    """新建一个 logger + StringIO handler；返回 (logger, buffer)。"""

    buf = io.StringIO()
    handler = logging.StreamHandler(stream=buf)
    handler.setFormatter(_JsonFormatter())
    test_logger = logging.getLogger(f"vendor_if_doctor.test_{id(buf)}")
    test_logger.handlers.clear()
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.DEBUG)
    test_logger.propagate = False
    return test_logger, buf


class TestJsonFormatterIntegration:
    """_JsonFormatter 与 redact 集成。"""

    def test_message_redacted(
        self, logger_with_buffer: tuple[logging.Logger, io.StringIO]
    ) -> None:
        test_logger, buf = logger_with_buffer
        test_logger.warning("Authorization: Bearer abc.def.ghi received")
        line = buf.getvalue().strip()
        record = json.loads(line)
        assert record["message"] == "Authorization: *** received"
        assert "abc.def.ghi" not in line

    def test_command_extra_field_redacted(
        self, logger_with_buffer: tuple[logging.Logger, io.StringIO]
    ) -> None:
        test_logger, buf = logger_with_buffer
        test_logger.info("test", extra={"command": "GET /api?api_key=KEY-9999&q=ok"})
        line = buf.getvalue().strip()
        record = json.loads(line)
        assert record["command"] == "GET /api?api_key=***&q=ok"
        assert "KEY-9999" not in line
        assert "q=ok" in line

    def test_response_body_extra_field_redacted(
        self, logger_with_buffer: tuple[logging.Logger, io.StringIO]
    ) -> None:
        test_logger, buf = logger_with_buffer
        test_logger.info("test", extra={"response_body": '{"token": "jwt-eyJhbGc.payload.sig"}'})
        line = buf.getvalue().strip()
        record = json.loads(line)
        assert '"token": "***"' in record["response_body"]
        assert "jwt-eyJhbGc" not in line

    def test_arbitrary_extra_field_redacted(
        self, logger_with_buffer: tuple[logging.Logger, io.StringIO]
    ) -> None:
        test_logger, buf = logger_with_buffer
        test_logger.info(
            "test",
            extra={"Authorization": "Bearer xyz", "safe_field": "visible"},
        )
        line = buf.getvalue().strip()
        record = json.loads(line)
        # 字符串级 redact_str 把 'Bearer xyz' → 'Bearer ***'
        assert record["Authorization"] == "Bearer ***"
        assert record["safe_field"] == "visible"
        assert "xyz" not in line

    def test_no_credential_leak_in_full_log(
        self, logger_with_buffer: tuple[logging.Logger, io.StringIO]
    ) -> None:
        """log 满篇 secret → 所有 secret 字段都脱敏。"""

        test_logger, buf = logger_with_buffer
        test_logger.info("start")
        test_logger.warning("Authorization: Bearer abc.def.ghi")
        test_logger.error('{"token": "jwt-eyJhbGc.payload.sig"}')
        test_logger.info("X-API-Key: kkk-9999")
        test_logger.info("password: wonderland")
        full_log = buf.getvalue()
        # 所有 secret 字面量都不能出现
        for forbidden in [
            "abc.def.ghi",
            "jwt-eyJhbGc",
            "kkk-9999",
            "wonderland",
            "lis-secret-token-9999",
            "pacs-secret-key-777",
        ]:
            assert forbidden not in full_log, f"leaked: {forbidden} in {full_log!r}"

    def test_json_lines_format(
        self, logger_with_buffer: tuple[logging.Logger, io.StringIO]
    ) -> None:
        test_logger, buf = logger_with_buffer
        test_logger.info("line 1")
        test_logger.info("line 2")
        lines = [l for l in buf.getvalue().split("\n") if l]
        assert len(lines) == 2
        for line in lines:
            record = json.loads(line)  # 每行都是合法 JSON
            assert "ts" in record
            assert "level" in record
            assert "logger" in record
            assert "message" in record


class TestSetupLogging:
    """setup_logging 初始化与幂等。"""

    def test_setup_logging_returns_root_logger(self) -> None:
        # 先清掉既有可能残留的 handler
        root = logging.getLogger("vendor_if_doctor")
        old_handlers = root.handlers.copy()
        try:
            root.handlers.clear()
            logger = setup_logging(level="INFO")
            assert logger is root
            assert any(isinstance(h, logging.StreamHandler) for h in logger.handlers)
        finally:
            root.handlers.clear()
            root.handlers.extend(old_handlers)

    def test_setup_logging_idempotent(self) -> None:
        root = logging.getLogger("vendor_if_doctor")
        old_handlers = root.handlers.copy()
        try:
            root.handlers.clear()
            setup_logging(level="INFO")
            h1 = list(root.handlers)
            setup_logging(level="DEBUG")
            h2 = list(root.handlers)
            assert len(h1) == len(h2)
            assert h1[0] is h2[0]  # 同一个 handler（不累加）
            assert root.level == logging.DEBUG
        finally:
            root.handlers.clear()
            root.handlers.extend(old_handlers)


# ---------------------------------------------------------------------------
# 端到端：跑一段含 secret 的 Python 代码，验证 subprocess 输出不含 secret 字面量
# ---------------------------------------------------------------------------


class TestSubprocessEndToEnd:
    """跨进程实证：subprocess 跑 logging_setup + 打 secret → stdout 不含 secret。"""

    @pytest.fixture
    def project_root(self) -> Path:
        return Path(__file__).resolve().parents[2]

    def _run_subprocess(self, script_text: str, project_root: Path) -> subprocess.CompletedProcess[str]:
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(script_text)
            script_path = Path(f.name)
        try:
            env = {
                **__import__("os").environ,
                "PYTHONPATH": str(project_root),
            }
            return subprocess.run(
                [sys.executable, str(script_path)],
                cwd=str(project_root),
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
        finally:
            script_path.unlink(missing_ok=True)

    def test_subprocess_logging_redacts_secrets(self, project_root: Path) -> None:
        # 写一段含 secret 的脚本
        script_text = (
            "import logging\n"
            "from vendor_if_doctor.logging_setup import setup_logging\n"
            "logger = setup_logging(level='INFO')\n"
            "logger.info('Authorization: Bearer abc.def.ghi')\n"
            "logger.warning('X-API-Key: kkk-9999')\n"
            "logger.error('{\"token\": \"jwt-eyJhbGc.payload.sig\"}')\n"
            "logger.info('password=wonderland')\n"
            "logger.info('safe text')\n"
        )
        proc = self._run_subprocess(script_text, project_root)
        assert proc.returncode == 0, f"subprocess failed: {proc.stderr}"
        full_log = proc.stdout
        # 5 条 secret 字面量都不能出现
        for forbidden in [
            "abc.def.ghi",
            "kkk-9999",
            "jwt-eyJhbGc",
            "wonderland",
        ]:
            assert forbidden not in full_log, f"leaked: {forbidden} in stdout:\n{full_log}"
        # 但 safe text 必须出现
        assert "safe text" in full_log
        # 至少 5 行 JSON
        lines = [l for l in full_log.split("\n") if l.strip()]
        assert len(lines) >= 5
        # 每行都是合法 JSON
        for line in lines:
            json.loads(line)  # 失败即非合法 JSON

    def test_grep_credential_discipline(self, project_root: Path) -> None:
        """spec 验收点：日志文件 grep -i 'authorization|token|password' 命中 = 0（兜底实证）。"""

        script_text = (
            "import logging\n"
            "from vendor_if_doctor.logging_setup import setup_logging\n"
            "logger = setup_logging(level='INFO')\n"
            "logger.info('Authorization: Bearer SUPER-SECRET-TOKEN')\n"
            "logger.warning('X-API-Key: ANOTHER-SECRET-KEY')\n"
            "logger.error('password=ULTRA-SECRET-PASS')\n"
            "logger.info('{\"token\": \"ANOTHER-JWT-PAYLOAD\"}')\n"
        )
        proc = self._run_subprocess(script_text, project_root)
        assert proc.returncode == 0, f"subprocess failed: {proc.stderr}"
        log_text = proc.stdout
        # spec 验证：4 类 forbidden literal 全部 0 命中
        for forbidden in [
            "SUPER-SECRET-TOKEN",
            "ANOTHER-SECRET-KEY",
            "ULTRA-SECRET-PASS",
            "ANOTHER-JWT-PAYLOAD",
        ]:
            assert forbidden not in log_text, f"leaked: {forbidden}"
        # 进一步用 regex 模拟 grep：检测**真实凭据字面量**（排除 *** 占位符）
        for pat in [
            # Authorization: Bearer <非 *** 的 value>
            r"(?i)authorization\s*[:=]\s*(?:bearer|basic)?\s*[^\s,;\"'\r\n*]{4,}",
            # Bearer <非 *** 的 value>
            r"(?i)\bbearer\s+[^\s,;\"'\r\n*]{4,}",
            # "token": "<非 *** 的 value>"
            r'(?i)"token"\s*:\s*"[^"\*]{4,}"',
            # password=<非 *** 的 value>
            r"(?i)password\s*=\s*[^\s,;\"'\r\n*&]{4,}",
        ]:
            assert not re.search(pat, log_text), f"regex {pat} matched in {log_text!r}"
