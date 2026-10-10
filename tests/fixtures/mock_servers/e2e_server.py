"""e2e_server — 端到端契约 mock server（task 32）。

把 task 32 的 3 类失败路径（auth_failed / hl7_ack_re / schema_mismatch）
抽到统一切换器里，``mode`` 字符串决定行为：

* ``http_401`` — mock HTTP 401 Unauthorized；
* ``http_missing_required`` — mock HTTP 200 但响应缺 required_fields；
* ``mllp_msa_ae`` — mock MLLP server 返回 ``MSA|AE`` ACK（HL7v2 失败）。

凭据纪律
--------

* mock server 返回的响应体**不**含真实凭据字面量；
* 所有 401 / 403 走 task 4 ``classify_exception`` → ``auth_failed`` 路由；
* schema_mismatch 走 ``required_fields`` 检查 → ``assert`` step failure。

与 ``failure_server.py`` 的关系
----------------

failure_server 走 ``pytest_httpserver`` + 异步 ``socket``；本模块
e2e_server 是 **同步纯 socket**，与 run_cmd 的同步路径兼容，且能用
``mode=...`` 字符串在同一函数里切换行为（避免 fixture 嵌套爆炸）。
"""

from __future__ import annotations

import enum
import socket
import socketserver
import threading
from dataclasses import dataclass, field
from typing import Iterator, Optional, Tuple

import pytest


# ---------------------------------------------------------------------------
# 常量与枚举
# ---------------------------------------------------------------------------


HTTP_SERVER_HOST: str = "127.0.0.1"
MLLP_SERVER_HOST: str = "127.0.0.1"

HTTP_DEFAULT_RESPONSE_401: str = "Unauthorized (e2e mock)"
HTTP_DEFAULT_RESPONSE_SCHEMA_MISSING: str = (
    '{"code": 200, "msg": "no required_fields in this response (e2e mock)"}'
)
MLLP_DEFAULT_MSA_ACK_AE: bytes = b"MSA|AE|test|MOCK-ACK-AE\r"


class E2EScenario(str, enum.Enum):
    """e2e mock 切换模式（task 32 三类失败路径）。"""

    HTTP_401 = "http_401"
    HTTP_MISSING_REQUIRED = "http_missing_required"
    MLLP_MSA_AE = "mllp_msa_ae"


def _free_port() -> int:
    """选一个当前空闲的本地端口并立即释放。"""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ---------------------------------------------------------------------------
# HTTP mock server（同步 BaseRequestHandler，socketserver.ThreadingMixIn）
# ---------------------------------------------------------------------------


@dataclass
class _E2EHttpHandle:
    host: str
    port: int
    server: socketserver.TCPServer = field(repr=False)
    request_count: int = 0


def _make_http_server(mode: str, port: int) -> _E2EHttpHandle:
    """用同步 socket server 在后台线程启动 HTTP mock。"""

    if mode == E2EScenario.HTTP_401.value:
        response_body = HTTP_DEFAULT_RESPONSE_401.encode("utf-8")
        status_line = b"HTTP/1.1 401 Unauthorized\r\n"
    elif mode == E2EScenario.HTTP_MISSING_REQUIRED.value:
        response_body = HTTP_DEFAULT_RESPONSE_SCHEMA_MISSING.encode("utf-8")
        status_line = b"HTTP/1.1 200 OK\r\n"
    else:
        raise ValueError(f"未知 HTTP e2e mock mode: {mode!r}")

    response = (
        status_line
        + b"Content-Type: application/json\r\n"
        + b"Content-Length: " + str(len(response_body)).encode() + b"\r\n"
        + b"Connection: close\r\n"
        + b"\r\n"
        + response_body
    )

    class _Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:  # noqa: D401 - sync handler
            sock = self.request
            try:
                sock.settimeout(10.0)
                # 读 HTTP request headers（直到 \r\n\r\n）
                buf = bytearray()
                while b"\r\n\r\n" not in buf:
                    chunk = sock.recv(4096)
                    if not chunk:
                        return
                    buf.extend(chunk)
                    if len(buf) > 64 * 1024:
                        return
                sock.sendall(response)
                handle.request_count += 1
            except (ConnectionResetError, BrokenPipeError, OSError):
                return
            finally:
                try:
                    sock.close()
                except Exception:
                    pass

    class _ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        daemon_threads = True
        allow_reuse_address = True

    server = _ThreadedTCPServer((HTTP_SERVER_HOST, port), _Handler)
    handle = _E2EHttpHandle(host=HTTP_SERVER_HOST, port=port, server=server)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    handle.__dict__["_thread"] = thread
    # 关键：server.server_address[1] 是 OS 实际分配的端口（port=0 时）
    actual_port = int(server.server_address[1])
    handle.port = actual_port
    return handle


def start_e2e_http_server(
    mode: str | E2EScenario = E2EScenario.HTTP_401,
    *,
    port: int = 0,
) -> Tuple[str, int, _E2EHttpHandle]:
    """启动 HTTP mock（同步入口），返回 ``(host, port, handle)``。"""

    if isinstance(mode, E2EScenario):
        mode = mode.value
    handle = _make_http_server(mode, port)
    return handle.host, handle.port, handle


def stop_e2e_http_server(handle) -> None:
    """关闭 :func:`start_e2e_http_server` 返回的 server。"""

    if handle is None:
        return
    srv = getattr(handle, "server", None)
    if srv is not None:
        try:
            srv.shutdown()
        except Exception:
            pass
        try:
            srv.server_close()
        except Exception:
            pass
    thread = handle.__dict__.pop("_thread", None)
    if thread is not None:
        try:
            thread.join(timeout=2.0)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# MLLP mock server（同步 socket，task 7 / task 17 风格）
# ---------------------------------------------------------------------------


_MLLP_START = b"\x0b"
_MLLP_END = b"\x1c"
_MLLP_CR = b"\x0d"


def _wrap_mllp(payload: bytes) -> bytes:
    return _MLLP_START + payload + _MLLP_END + _MLLP_CR


def _build_msa_ae_ack() -> bytes:
    """构造带 ``MSA|AE`` 的最小可解析 ACK（task 32 hl7_ack_re 契约用）。"""

    msa = b"MSA|AE|test|MOCK-ACK-AE\r"
    return _wrap_mllp(msa)


@dataclass
class _E2EMllpHandle:
    host: str
    port: int
    server: socketserver.TCPServer = field(repr=False)
    received: list = field(default_factory=list)
    request_count: int = 0


def _make_mllp_server(port: int) -> _E2EMllpHandle:
    """用同步 socket server 在后台线程启动 MLLP mock（固定返 ``MSA|AE``）。"""

    class _Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:  # noqa: D401 - sync handler
            sock = self.request
            try:
                sock.settimeout(15.0)
                buf = bytearray()
                end_idx = -1
                attempts = 0
                while end_idx < 0 and attempts < 64:
                    attempts += 1
                    chunk = sock.recv(8192)
                    if not chunk:
                        return
                    buf.extend(chunk)
                    for i in range(len(buf) - 1):
                        if buf[i] == 0x1C and buf[i + 1] == 0x0D:
                            end_idx = i + 1
                            break
                    if len(buf) > 4 * 1024 * 1024:
                        return

                if end_idx < 0:
                    return

                start_idx = -1
                for i in range(end_idx + 1):
                    if buf[i] == 0x0B:
                        start_idx = i
                        break
                if start_idx < 0:
                    return
                frame = bytes(buf[start_idx:end_idx + 1])
                handle.received.append(frame)
                handle.request_count += 1

                sock.sendall(_build_msa_ae_ack())
            except (ConnectionResetError, BrokenPipeError, OSError):
                return
            finally:
                try:
                    sock.close()
                except Exception:
                    pass

    class _ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        daemon_threads = True
        allow_reuse_address = True

    server = _ThreadedTCPServer((MLLP_SERVER_HOST, port), _Handler)
    handle = _E2EMllpHandle(host=MLLP_SERVER_HOST, port=port, server=server)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    handle.__dict__["_thread"] = thread
    # 关键：server.server_address[1] 是 OS 实际分配的端口（port=0 时）
    actual_port = int(server.server_address[1])
    handle.port = actual_port
    return handle


def start_e2e_mllp_server(
    *,
    port: int = 0,
) -> Tuple[str, int, _E2EMllpHandle]:
    """启动 MLLP mock（同步入口），返回 ``(host, port, handle)``。"""

    handle = _make_mllp_server(port)
    return handle.host, handle.port, handle


def stop_e2e_mllp_server(handle) -> None:
    """关闭 :func:`start_e2e_mllp_server` 返回的 server。"""

    if handle is None:
        return
    srv = getattr(handle, "server", None)
    if srv is not None:
        try:
            srv.shutdown()
        except Exception:
            pass
        try:
            srv.server_close()
        except Exception:
            pass
    thread = handle.__dict__.pop("_thread", None)
    if thread is not None:
        try:
            thread.join(timeout=2.0)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# pytest fixtures（一键注入，三类失败路径可选）
# ---------------------------------------------------------------------------


@pytest.fixture
def e2e_http_401_server() -> Iterator[Tuple[str, int]]:
    host, port, handle = start_e2e_http_server(E2EScenario.HTTP_401)
    try:
        yield host, port
    finally:
        stop_e2e_http_server(handle)


@pytest.fixture
def e2e_http_missing_required_server() -> Iterator[Tuple[str, int]]:
    host, port, handle = start_e2e_http_server(E2EScenario.HTTP_MISSING_REQUIRED)
    try:
        yield host, port
    finally:
        stop_e2e_http_server(handle)


@pytest.fixture
def e2e_mllp_msa_ae_server() -> Iterator[Tuple[str, int]]:
    host, port, handle = start_e2e_mllp_server()
    try:
        yield host, port
    finally:
        stop_e2e_mllp_server(handle)


__all__ = [
    "E2EScenario",
    "HTTP_SERVER_HOST",
    "MLLP_SERVER_HOST",
    "start_e2e_http_server",
    "stop_e2e_http_server",
    "start_e2e_mllp_server",
    "stop_e2e_mllp_server",
    "e2e_http_401_server",
    "e2e_http_missing_required_server",
    "e2e_mllp_msa_ae_server",
]