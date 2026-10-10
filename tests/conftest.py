"""conftest — vendor-if-doctor 测试共享 fixtures。

任务 12 接入：
- ``mock_http_server``（基于 :class:`pytest_httpserver.HTTPServer`）— 5 类失败
  行为预设（200/401/403/500/timeout/空响应/缺字段）；
- ``sample_vendor_spec`` — 单条 HIS 接口（Bearer + required_fields）样例；
- ``tmp_sqlite_db`` — 临时 SQLite 路径（每个测试隔离）；
- ``env_with_credentials`` — env 注入 4 类真实凭据；
- ``example_vendor_spec_path`` — ``examples/vendor_spec.example.yaml`` 绝对路径。

约定：
- 所有 fixture 不发起任何真实生产环境请求；mock server 监听 127.0.0.1 随机端口；
- 临时目录由 pytest ``tmp_path`` 自动清理；
- 凭据字面量仅在测试 fixture 内出现，**绝不**写入证据或落盘产物。
"""

from __future__ import annotations

import os
import socket
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping

import pytest
from pytest_httpserver import HTTPServer

from vendor_if_doctor.renderers.sqlite_history import connect, ensure_schema


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------


PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = PROJECT_ROOT / "examples"
FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures" / "valid_specs"
INVALID_FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures" / "vendor_specs"


@pytest.fixture(scope="session")
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture(scope="session")
def example_vendor_spec_path() -> Path:
    return EXAMPLES_DIR / "vendor_spec.example.yaml"


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """valid_specs 目录路径。"""

    return FIXTURES_DIR


@pytest.fixture(scope="session")
def invalid_specs_dir() -> Path:
    """invalid vendor_specs 目录路径（用于负例测试）。"""

    return INVALID_FIXTURES_DIR


# ---------------------------------------------------------------------------
# mock HTTP server（基于 pytest-httpserver，task 12 核心 fixture）
# ---------------------------------------------------------------------------


def _pick_free_port() -> int:
    """找一个当前空闲的本地端口（用于 mock server）。"""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def mock_http_server() -> Iterator[HTTPServer]:
    """启动一个 pytest_httpserver 实例（监听 127.0.0.1 随机端口）。

    使用方式：
        server = mock_http_server
        server.expect_request("/api/health").respond_with_json({...})
        # base_url: f"http://{server.host}:{server.port}"
    """

    server = HTTPServer(host="127.0.0.1", port=_pick_free_port())
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def mock_http_base_url(mock_http_server: HTTPServer) -> str:
    """mock HTTP server 的 base URL（http://127.0.0.1:<port>）。"""

    return f"http://{mock_http_server.host}:{mock_http_server.port}"


# ---------------------------------------------------------------------------
# 5 类失败行为预设的 mock server（contract 测试用）
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_http_401() -> Iterator[HTTPServer]:
    """mock 401 Unauthorized — auth_failed 契约。"""

    server = HTTPServer(host="127.0.0.1", port=_pick_free_port())
    server.start()
    server.expect_request("/").respond_with_data("Unauthorized", status=401)
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def mock_http_500() -> Iterator[HTTPServer]:
    """mock 500 Internal Server Error — http_status 契约。"""

    server = HTTPServer(host="127.0.0.1", port=_pick_free_port())
    server.start()
    server.expect_request("/").respond_with_data("Internal Server Error", status=500)
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def mock_http_empty() -> Iterator[HTTPServer]:
    """mock 200 + 空响应体 — payload_empty 契约。"""

    server = HTTPServer(host="127.0.0.1", port=_pick_free_port())
    server.start()
    server.expect_request("/").respond_with_data("", status=200)
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def mock_http_missing_required() -> Iterator[HTTPServer]:
    """mock 200 + 缺 required_fields — schema_mismatch 契约。

    响应含 ``code`` 但缺 ``patient_id`` 与 ``slot_id``。
    """

    server = HTTPServer(host="127.0.0.1", port=_pick_free_port())
    server.start()
    payload = {"code": 200, "msg": "no patient_id / slot_id in this response"}
    server.expect_request("/").respond_with_json(payload)
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def mock_http_happy() -> Iterator[HTTPServer]:
    """mock 200 + 含全部 required_fields — happy path 契约。"""

    server = HTTPServer(host="127.0.0.1", port=_pick_free_port())
    server.start()
    payload = {
        "code": 200,
        "patient_id": "<EXAMPLE_PATIENT>",
        "slot_id": "<EXAMPLE_SLOT>",
        "report_id": "<EXAMPLE_REPORT>",
    }
    server.expect_request("/").respond_with_json(payload)
    try:
        yield server
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# 临时 SQLite 数据库
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_sqlite_db(tmp_path: Path) -> Iterator[Path]:
    """临时 SQLite 路径（自动创建父目录；测试结束自动清理）。"""

    db_path = tmp_path / "vendor_if_doctor_test.db"
    conn = connect(db_path)
    ensure_schema(conn)
    conn.close()
    yield db_path
    if db_path.exists():
        db_path.unlink()


# ---------------------------------------------------------------------------
# 凭据 env fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def env_with_credentials(monkeypatch: pytest.MonkeyPatch) -> Mapping[str, str]:
    """注入 4 类凭据 env；返回 dict 便于断言。

    真实凭据字面量仅在测试作用域内出现，**绝不**落盘或写入 Evidence。
    """

    creds = {
        "VENDOR_HIS_USERNAME": "alice",
        "VENDOR_HIS_PASSWORD": "wonderland",
        "VENDOR_LIS_TOKEN": "lis-secret-token-9999",
        "VENDOR_PACS_APIKEY": "pacs-secret-key-777",
        "VENDOR_EMR_MLLP_HOST": "127.0.0.1",
        "VENDOR_HIS_HOST": "127.0.0.1",
        "VENDOR_LIS_HOST": "127.0.0.1",
        "VENDOR_PACS_HOST": "127.0.0.1",
        "VENDOR_MI_HOST": "127.0.0.1",
        "VENDOR_REGIONAL_HOST": "127.0.0.1",
    }
    for k, v in creds.items():
        monkeypatch.setenv(k, v)
    return creds


@pytest.fixture
def env_clean(monkeypatch: pytest.MonkeyPatch) -> Mapping[str, str]:
    """清空所有 VENDOR_* env 变量，便于测试缺凭据场景。"""

    for k in list(os.environ.keys()):
        if k.startswith("VENDOR_"):
            monkeypatch.delenv(k, raising=False)
    return {}


# ---------------------------------------------------------------------------
# vendor_spec fixture（用 inline YAML 而非文件）
# ---------------------------------------------------------------------------


@pytest.fixture
def inline_his_bearer_spec(mock_http_base_url: str, env_with_credentials: Mapping[str, str]):
    """构造一份 inline VendorSpec：HIS Bearer 接口指向 mock_http_base_url。

    返回 ``(VendorSpec, vendor_id, interface_id)``。
    """

    from vendor_if_doctor.spec.schema import (
        AuthBearer,
        HttpSampleEndpoint,
        VendorInterface,
        VendorSpec,
        InspectionConfig,
    )

    from urllib.parse import urlparse

    parsed = urlparse(mock_http_base_url)
    interface = VendorInterface(
        vendor_id="his",
        interface_id="outpatient-register",
        display_name="HIS 门诊挂号接口 (test)",
        protocol="http",
        host=parsed.hostname or "127.0.0.1",
        port=parsed.port or 80,
        base_path="",
        timeout_seconds=5,
        auth=AuthBearer(secret_env="VENDOR_LIS_TOKEN"),
        sample_endpoint=HttpSampleEndpoint(
            method="GET",
            path="/",
            headers={"Accept": "application/json"},
            body="",
        ),
        required_fields=("$.code", "$.patient_id", "$.slot_id"),
    )
    spec = VendorSpec(
        schema_version="1.0",
        run_window_minutes=30,
        severity_policy="default",
        interfaces=[interface],
        inspection=InspectionConfig(),
    )
    return spec, "his", "outpatient-register"


# ---------------------------------------------------------------------------
# 工具：MHLP mock server fixture（用于 HL7v2 测试）
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_mllp_server() -> Iterator[tuple[str, int]]:
    """启动一个本地 MLLP server（asyncio），监听 127.0.0.1 随机端口。

    返回 ``(host, port)``。

    说明：本 fixture 是一个占位 — task 12 不要求 HL7v2 demo mock server 真实启动
    （test_hl7v2_mllp.py 已覆盖 23 个测试场景），这里仅暴露一个轻量端口供 contract
    测试用。
    """

    port = _pick_free_port()
    yield ("127.0.0.1", port)


# ---------------------------------------------------------------------------
# 工具：MHLP mock server fixture（用于 HL7v2 测试）
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_mllp_server() -> Iterator[tuple[str, int]]:
    """启动一个本地 MLLP server（asyncio），监听 127.0.0.1 随机端口。

    返回 ``(host, port)``。

    说明：本 fixture 是一个占位 — task 12 不要求 HL7v2 demo mock server 真实启动
    （test_hl7v2_mllp.py 已覆盖 23 个测试场景），这里仅暴露一个轻量端口供 contract
    测试用。
    """

    port = _pick_free_port()
    yield ("127.0.0.1", port)


# ---------------------------------------------------------------------------
# e2e mock server fixtures（task 32 端到端契约测试）
# ---------------------------------------------------------------------------


from tests.fixtures.mock_servers.e2e_server import (  # noqa: E402
    e2e_http_401_server,
    e2e_http_missing_required_server,
    e2e_mllp_msa_ae_server,
)


# ---------------------------------------------------------------------------
# 工具：截断 + 跳过历史的辅助函数
# ---------------------------------------------------------------------------


@contextmanager
def cwd_in(directory: Path):
    """临时切换工作目录到指定目录（用于 CLI 测试，避免污染 cwd）。"""

    import os

    prev = Path.cwd()
    os.chdir(directory)
    try:
        yield
    finally:
        os.chdir(prev)


@pytest.fixture
def cwd_tmp(tmp_path: Path) -> Iterator[Path]:
    """把 cwd 切到 tmp_path（CLI / renderers 测试用）。"""

    with cwd_in(tmp_path):
        yield tmp_path