"""Evidence — 单步巡检证据。

设计要点
--------

* 5 步流水线（preflight / auth / list / sample / assert）每一步都会产生一条 Evidence；
* Evidence 含 ``status`` (``pass`` / ``fail`` / ``skip``) 与 ``failure_class``
  （失败语义分类：HTTPStatus / AuthFailed / Timeout / SchemaMismatch / PayloadEmpty /
  HL7AckRE / TcpHalfClosed / EncodingError / NotImplemented / Slow）；
* ``evidence_hash`` = sha256(command + response_body)，**绝不**含 Authorization /
  token / password 等凭据字段值；调用方须在算 hash 之前过
  :func:`vendor_if_doctor.redact.redact_dict`；
* Evidence **不**包含原始请求体或响应体的字节流，只保留 ``response_body`` 摘要
  （最多 32KB，见 task 24）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

EvidenceStatus = Literal["pass", "fail", "skip", "degraded"]
"""Evidence status：pass=完全通过；fail=失败；skip=跳过；degraded=部分通过（warning 级别）。"""


@dataclass
class Evidence:
    """单步巡检证据。

    字段
    ----

    * ``summary`` — 一句话可读结论（不含凭据）；
    * ``command`` — 实际发出的请求描述（不含 Authorization / token）；
    * ``latency_ms`` — 实际耗时（含超时上限）；
    * ``status`` — pass / fail / skip / degraded；
    * ``failure_class`` — 失败语义分类（仅 ``status='fail'/'degraded'`` 时非空）；
    * ``response_body`` — 响应摘要（最多 32KB，已 redact）；
    * ``evidence_hash`` — sha256(command + response_body) 前 16 位；
    * ``meta`` — 协议 / vendor 相关的额外字段（HTTP status / HL7 MSA-1 / etc.）。
    """

    summary: str
    command: str
    latency_ms: int
    status: EvidenceStatus
    failure_class: Optional[str] = None
    response_body: Optional[str] = None
    evidence_hash: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)
    step_name: str = "unknown"
    vendor_id: str = ""
    interface_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """dict 化（按字段名排序，跨协议一致性）。

        顶层字段与 ``meta`` 内部都按 key 排序输出——保证 SQLite / Markdown / JSONL
        渲染时同一接口跨协议（HTTP / HL7v2 / WebService / SQL）字段顺序一致，
        不因 Evidence 实例化时的字段顺序而漂移。
        """

        d = {
            "summary": self.summary,
            "command": self.command,
            "latency_ms": self.latency_ms,
            "status": self.status,
            "failure_class": self.failure_class,
            "response_body": self.response_body,
            "evidence_hash": self.evidence_hash,
            "meta": dict(sorted(self.meta.items())),
            "step_name": self.step_name,
            "vendor_id": self.vendor_id,
            "interface_id": self.interface_id,
        }
        return dict(sorted(d.items()))

    @property
    def is_failure(self) -> bool:
        return self.status in ("fail", "degraded")


__all__ = ["Evidence", "EvidenceStatus"]