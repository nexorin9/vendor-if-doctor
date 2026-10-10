"""BaseConnection — 协议无关抽象基类。

子类须实现
----------

* :meth:`_get_client` — 返回共享客户端实例（HTTP: ``httpx.Client``；HL7v2: 自定义 MLLP 池）；
* :meth:`steps` — 返回 :class:`Step` 列表；默认 5 步流水线
  （preflight / auth / list / sample / assert）。

Phase 4 task 5 增强
-------------------

* :attr:`_state` — 跨步骤状态字典（``host`` / ``port`` / ``auth_header`` /
  ``parsed_body`` / ``http_status`` 等），各步骤透传 evidence 之间的中间结果；
* :meth:`get_step` — 按 name 在流水线中查找单个 :class:`Step`，便于调度器按
  ``steps`` 列表挑出要跑的下一步骤。
"""

from __future__ import annotations

import abc
from typing import Dict, Generic, List, Optional, TypeVar

from vendor_if_doctor.connection.step import Step
from vendor_if_doctor.spec.schema import VendorInterface

ClientT = TypeVar("ClientT")
"""客户端实例类型（HTTP = httpx.Client；HL7v2 = MLLPClient）。"""


class BaseConnection(abc.ABC, Generic[ClientT]):
    """协议无关连接抽象基类。

    Parameters
    ----------
    spec : VendorInterface
        单条 vendor 接口规格；
    env : dict, optional
        环境变量字典（默认读 ``os.environ``）；
    """

    def __init__(self, spec: VendorInterface, env: Optional[dict] = None) -> None:
        import os

        self.spec = spec
        self.env = env if env is not None else dict(os.environ)
        self._client: Optional[ClientT] = None
        # 跨步骤状态：每个 step 可写、可读。约定 key：
        #   host / port          — preflight 解析后的 (host, port)
        #   auth_header          — Authorization header（不含 token 原始值）
        #   auth_header_name     — apikey header 名
        #   apikey_query / apikey_value — query 注入参数
        #   last_response        — 最近一次 httpx.Response（sample 步骤写入）
        #   parsed_body          — 最近一次解析后的 JSON dict/list（assert 读取）
        #   http_status          — 最近一次 HTTP 状态码（assert 读取）
        self._state: Dict[str, object] = {}

    @abc.abstractmethod
    def _get_client(self) -> ClientT:
        """返回（共享）客户端实例。子类可在第一次调用时构造，后续复用。"""

    @property
    def client(self) -> ClientT:
        """共享客户端实例（懒构造，连接池复用）。"""

        if self._client is None:
            self._client = self._get_client()
        return self._client

    @abc.abstractmethod
    def steps(self) -> List[Step]:
        """返回该协议下要跑的 Step 列表。

        默认实现约定顺序：
        ``[preflight, auth, list, sample, assert]``
        """

    def get_step(self, name: str) -> Optional[Step]:
        """按名字在 :meth:`steps` 列表中查找单个 :class:`Step`，未找到返回 ``None``。"""

        for s in self.steps():
            if s.name == name:
                return s
        return None

    def close(self) -> None:
        """关闭客户端（默认空操作；HTTP / HL7v2 子类覆盖）。"""

        self._client = None
        self._state = {}


__all__ = ["BaseConnection"]
