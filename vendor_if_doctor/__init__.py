"""vendor-if-doctor — 医院厂商接口连通巡检库。

信息科 / SRE 早 8:30 跑一条命令，5 分钟拿到昨日新接入或变更的厂商接口（HIS / LIS /
PACS / EMR / 医保 / SPD / 区域平台）的健康清单（healthy / degraded / failed）+ 工单
草稿 JSONL + SQLite 巡检历史，值班员人审后粘入工单系统。

设计要点
--------

* **多步骤结构化连接测试**：preflight → auth → list → sample → assert 5 步流水线，
  失败短路并暴露首个失败的 step 名。
* **5 类失败分类**：HTTPStatus / AuthFailed / Timeout / SchemaMismatch / PayloadEmpty
  + HL7AckRE / TcpHalfClosed / EncodingError / NotImplemented（占位协议）。
* **凭据仅从环境变量读**：vendor_spec.yaml 不允许内嵌明文；redact 工具保证
  Authorization / token / password 永远不进 SQLite / Markdown / JSONL / 日志。
* **幂等与并发**：run_id 唯一，同窗口二次写抛 IntegrityError；asyncio.gather 并发
  跑多个 vendor。
* **SQLite 月表分区 + WAL + BEGIN IMMEDIATE**：方便历史查询与并发写。
* **决策支持**：失败告警分级 + 工单草稿（人审后粘入） + 失败建议动作模板 + 趋势 /
  周报（HTML，离线可看）。
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
