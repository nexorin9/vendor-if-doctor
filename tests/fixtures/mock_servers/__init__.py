"""vendor-if-doctor 失败 mock server 工具集。

本包对 Phase 4 失败路径契约测试（task 25 + task 32）暴露 6 类 mock server 构造器，
覆盖 HTTP 与 HL7v2 两种协议下所有 failure_class：

* preflight_timeout  ×  2协议（HTTP 不可达端口 / HL7 不可达端口）
* http_status        ×  1协议（HTTP 5xx）
* auth_failed        ×  1协议（HTTP 401/403）
* degraded_slow      ×  2协议（HTTP 慢响应 / HL7 ACK 慢返回）
* schema_mismatch    ×  2协议（HTTP 缺 required_fields / HL7 缺 PID-3）
* hl7_ack_re         ×  2协议（MSA-1=AE / MSA-1=AR）

公共 API
--------

* :func:`make_http_server` — 构造 HTTP mock，``mode`` 字符串指定行为：
  ``"http_401" | "http_403" | "http_500" | "http_happy" | "http_missing_required" |
  "http_empty" | "http_slow"``
* :func:`start_mllp_server` — 启动一个 MLLP mock 服务（asyncio），返回
  ``(host, port)``；``mode`` 指定行为：
  ``"mlpp_msa_aa" | "mlpp_msa_ae" | "mlpp_msa_ar" | "mlpp_no_ack" | "mlpp_slow" |
  "mlpp_missing_pid3"``
* :func:`make_unreachable_port` — 选一个空闲端口后立即关闭，给预连接失败用。
* :mod:`tests.fixtures.mock_servers.e2e_server` — task 32 端到端契约测试
  三类失败路径 mock server（HTTP 401 / HTTP missing required / MLLP MSA|AE）。
"""

from __future__ import annotations

from tests.fixtures.mock_servers.e2e_server import (
    E2EScenario,
    e2e_http_401_server,
    e2e_http_missing_required_server,
    e2e_mllp_msa_ae_server,
    start_e2e_http_server,
    start_e2e_mllp_server,
    stop_e2e_http_server,
    stop_e2e_mllp_server,
)
from tests.fixtures.mock_servers.failure_server import (
    HTTP_SLOW_DELAY_S,
    MLLP_SLOW_DELAY_S,
    MLLP_DEFAULT_PORT_PLACEHOLDER,
    FailureScenarioHTTP,
    FailureScenarioHL7,
    free_port,
    make_http_server,
    make_unreachable_port,
    start_mllp_server,
    stop_mllp_server,
)

__all__ = [
    "E2EScenario",
    "HTTP_SLOW_DELAY_S",
    "MLLP_SLOW_DELAY_S",
    "MLLP_DEFAULT_PORT_PLACEHOLDER",
    "FailureScenarioHTTP",
    "FailureScenarioHL7",
    "e2e_http_401_server",
    "e2e_http_missing_required_server",
    "e2e_mllp_msa_ae_server",
    "free_port",
    "make_http_server",
    "make_unreachable_port",
    "start_e2e_http_server",
    "start_e2e_mllp_server",
    "start_mllp_server",
    "stop_e2e_http_server",
    "stop_e2e_mllp_server",
    "stop_mllp_server",
]