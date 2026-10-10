"""vendor-if-doctor 协议适配器集合。

当前落地
--------

* :class:`HTTPConnection` — HTTP/HTTPS 协议（task 3 最小骨架 → task 6 完整 5 类 auth +
  list/sample + required_fields 断言）；
* :class:`HL7v2Connection` — HL7v2 + MLLP over TCP（task 7）；
* :class:`WebServiceConnection` — WebService/SOAP 占位（task 8）；
* :class:`SQLConnection` — SQL 探活占位（task 8）。

凭据纪律（适用全部适配器）
------------------------

* 凭据相关字段一律从环境变量读取（``secret_env`` / ``username_env`` / ``cert_env`` 等）；
* :class:`BaseConnection` 子类的 :attr:`Evidence.response_body` 在写入前过
  :func:`vendor_if_doctor.redact.redact_dict` 兜底（task 18 完整实现）。
"""

from __future__ import annotations

from vendor_if_doctor.adapters.hl7v2 import HL7v2Connection
from vendor_if_doctor.adapters.http import HTTPConnection
from vendor_if_doctor.adapters.sql import SQLConnection
from vendor_if_doctor.adapters.webservice import WebServiceConnection

__all__ = ["HTTPConnection", "HL7v2Connection", "SQLConnection", "WebServiceConnection"]
