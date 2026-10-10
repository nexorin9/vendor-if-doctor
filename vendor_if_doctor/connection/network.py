"""network — TCP 探活 + 网络层工具。

* :func:`preflight(host, port, timeout) -> None` — TCP 探活（成功抛 :class:`PreflightOK`，
  失败抛 :class:`PreflightError`）。

约定
----

* 不引入额外依赖；只走 stdlib ``socket``；
* ``timeout`` 默认 1.0s（满足 Phase 3 验收 ``latency_ms < 1500`` 上限）；
* **不**解析协议层（HTTP/HL7）；只验证 host:port 可达。
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from typing import Optional


@dataclass
class PreflightOK:
    """TCP 探活成功信号。"""

    host: str
    port: int
    latency_ms: int

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"preflight OK {self.host}:{self.port} ({self.latency_ms}ms)"


@dataclass
class PreflightError(Exception):
    """TCP 探活失败信号（异常 + 元数据）。"""

    host: str
    port: int
    latency_ms: int
    reason: str

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"preflight FAIL {self.host}:{self.port} ({self.latency_ms}ms): {self.reason}"


def _elapsed_ms(start: float, end: float) -> int:
    return max(0, int((end - start) * 1000))


def preflight(
    host: str,
    port: int,
    timeout: float = 1.0,
    *,
    _socket_factory=None,
) -> PreflightOK:
    """TCP 探活。

    Parameters
    ----------
    host : str
        主机名 / IP（不做 DNS 解析失败细化，捕获所有 :class:`OSError`）；
    port : int
        TCP 端口；
    timeout : float
        超时秒数，默认 1.0（满足 ``latency_ms < 1500``）；
    _socket_factory : optional
        测试用 socket 工厂（用于注入不可达 mock）。

    Raises
    ------
    PreflightError
        探活失败（超时 / 拒绝 / DNS 失败等），合并主行
    """

    import time

    factory = _socket_factory or socket.create_connection
    start = time.monotonic()
    try:
        sock = factory((host, port), timeout=timeout)
    except (socket.timeout, OSError) as exc:
        end = time.monotonic()
        raise PreflightError(
            host=host,
            port=port,
            latency_ms=_elapsed_ms(start, end),
            reason=f"{type(exc).__name__}: {exc}",
        ) from exc
    try:
        sock.close()
    except OSError:  # pragma: no cover - 关闭失败不影响结果
        pass
    end = time.monotonic()
    return PreflightOK(host=host, port=port, latency_ms=_elapsed_ms(start, end))


def safe_host_port(host: str, port: int, env: Optional[dict] = None) -> tuple[str, int]:
    """从环境变量展开 ``${ENV_VAR}`` 占位符。

    vendor_spec 允许 host 字段写 ``${VENDOR_HIS_HOST}``；启动期校验时调用此函数。
    """
    import os

    env = env if env is not None else os.environ
    if isinstance(host, str) and host.startswith("${") and host.endswith("}"):
        env_name = host[2:-1]
        host = env.get(env_name, host)
    return host, port


__all__ = ["PreflightError", "PreflightOK", "preflight", "safe_host_port"]