"""structured logging for vendor-if-doctor CLI。

* 输出 JSON Lines 到 stdout（便于 CI 抓取）；
* 兜底脱敏：每条 record 的 ``message`` / ``args`` / extras 走
  :func:`vendor_if_doctor.redact.redact_str`；
* ``$VENDOR_DOCTOR_LOG_LEVEL`` 控制级别（默认 ``INFO``）。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

from vendor_if_doctor.redact import redact_str


_LOG_RECORD_KEYS_TO_REDACT = (
    "message",
    "msg",
    "args",
    "summary",
    "command",
    "response_body",
)


class _JsonFormatter(logging.Formatter):
    """JSON Lines 格式化器；所有字符串字段先过 :func:`redact_str`。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_str(record.getMessage()) or "",
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        for key in _LOG_RECORD_KEYS_TO_REDACT:
            if key == "message":
                continue
            val = getattr(record, key, None)
            if val is None:
                continue
            if isinstance(val, str):
                payload[key] = redact_str(val) or ""
            else:
                payload[key] = val
        # 任何 extra 字段也走 redact
        for k, v in record.__dict__.items():
            if k.startswith("_") or k in payload or k in {
                "args", "asctime", "created", "exc_info", "exc_text", "filename",
                "funcName", "levelname", "levelno", "lineno", "module", "msecs",
                "message", "msg", "name", "pathname", "process", "processName",
                "relativeCreated", "stack_info", "thread", "threadName",
            }:
                continue
            payload[k] = redact_str(v) if isinstance(v, str) else v
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str | int | None = None) -> logging.Logger:
    """初始化 CLI 日志；返回根 logger。重复调用幂等。"""

    if level is None:
        level = os.environ.get("VENDOR_DOCTOR_LOG_LEVEL", "INFO")
    if isinstance(level, str):
        level = level.upper()
    root = logging.getLogger("vendor_if_doctor")
    # 已配置过则只更新 level
    if root.handlers:
        root.setLevel(level)
        return root
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False
    return root


__all__ = ["setup_logging"]