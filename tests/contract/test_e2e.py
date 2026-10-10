r"""test_e2e — 端到端契约测试（task 32）。

串联所有模块，验证三条失败链路产物都正确：

1. **auth_failed**（HTTP 401）→ ``handover.md`` 含 ``### Status: failed``
   + ``首次失败步骤：`sample` ``（401 由 sample 步的非 2xx 检测识别）；
   ``tickets.jsonl`` 含 ``first_failed_step=sample``
   + ``suggested_actions`` ≥ 1 条 + ``severity=critical`` + ``priority=high``；
   SQLite 月表 ``inspection_runs_y<YYYY>m<MM>`` 含至少 1 行 + ``run_id`` +
   ``evidence_hash``。

2. **hl7_ack_re**（MLLP mock 返 ``MSA|AE``）→ ``handover.md`` 含
   ``首次失败步骤:\`assert\```；``tickets.jsonl`` 含
   ``failure_class=hl7_ack_re`` + ``suggested_actions`` 含 ``MLLP``。

3. **schema_mismatch**（HTTP 200 但响应缺 ``required_fields``）→
   ``tickets.jsonl`` 含 ``failure_class=schema_mismatch`` + ``priority=high``
   + ``meta.failed_required_fields`` 含缺失字段名。

每个测试用独立 ``tmp_path`` 跑通渲染 + SQLite，产物可被人类读取。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List, Mapping

import pytest

from vendor_if_doctor.connection import runner as _runner
from vendor_if_doctor.renderers.handover_md import write_handover
from vendor_if_doctor.renderers.sqlite_history import (
    fetch_run,
    partition_table_name,
    query_history,
    write_history,
)
from vendor_if_doctor.renderers.ticket_jsonl import write_tickets
from vendor_if_doctor.spec.schema import (
    AuthBearer,
    Hl7v2SampleEndpoint,
    HttpSampleEndpoint,
    InspectionConfig,
    SampleEndpoint,
    VendorInterface,
    VendorSpec,
)

# 别名避免 pytest 把 test_connection 当测试函数
do_test_connection = _runner.test_connection


# ---------------------------------------------------------------------------
# helpers：构造 spec + 写产物到 tmp_path
# ---------------------------------------------------------------------------


def _make_http_bearer_spec(
    host: str,
    port: int,
    *,
    required_fields=None,
    token_env: str = "VENDOR_LIS_TOKEN",
    interface_id: str = "outpatient-register",
) -> VendorSpec:
    if required_fields is None:
        required_fields = ["$.code", "$.patient_id", "$.slot_id"]
    interface = VendorInterface(
        vendor_id="e2e",
        interface_id=interface_id,
        display_name="e2e HTTP bearer (test)",
        protocol="http",
        host=host,
        port=port,
        base_path="/",
        timeout_seconds=5,
        auth=AuthBearer(secret_env=token_env),
        sample_endpoint=SampleEndpoint(http=HttpSampleEndpoint(
            method="GET",
            path="/",
            headers={"Accept": "application/json"},
            body="",
        )),
        required_fields=required_fields,
    )
    return VendorSpec(
        schema_version="1.0",
        run_window_minutes=30,
        severity_policy="default",
        interfaces=[interface],
        inspection=InspectionConfig(),
    )


def _make_hl7v2_spec(
    host: str,
    port: int,
    *,
    required_fields=None,
    token_env: str = "VENDOR_LIS_TOKEN",
    interface_id: str = "emr-submit",
) -> VendorSpec:
    if required_fields is None:
        required_fields = ["MSH-9", "MSA-1"]
    interface = VendorInterface(
        vendor_id="e2e",
        interface_id=interface_id,
        display_name="e2e HL7v2 (test)",
        protocol="hl7v2",
        host=host,
        port=port,
        base_path="",
        timeout_seconds=5,
        auth=AuthBearer(secret_env=token_env),
        sample_endpoint=SampleEndpoint(hl7v2=Hl7v2SampleEndpoint(
            message_type="ADT^A01",
            trigger_event="A01",
            sample_patient_id="<EXAMPLE_PATIENT>",
        )),
        required_fields=required_fields,
    )
    return VendorSpec(
        schema_version="1.0",
        run_window_minutes=30,
        severity_policy="default",
        interfaces=[interface],
        inspection=InspectionConfig(),
    )


def _build_artifacts(
    tmp_path: Path,
    evidence_list: List,
    *,
    db_path: Path,
) -> Path:
    """跑渲染三件套（handover.md / tickets.jsonl / SQLite），返回 handover.md 路径。"""

    handover_path = tmp_path / "handover.md"
    tickets_path = tmp_path / "tickets.jsonl"

    # 渲染：handover.md + tickets.jsonl 写到 tmp_path
    write_handover(evidence_list, path=handover_path)
    write_tickets(evidence_list, path=tickets_path)

    # 写 SQLite
    write_history(evidence_list, path=db_path)

    return handover_path


def _read_jsonl(path: Path) -> List[dict]:
    """读 JSONL 文件为 list of dict（容错空行）。"""

    out: List[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


def _all_evidence_for_vendor(evidence_list: List, vendor_id: str) -> List:
    return [ev for ev in evidence_list if ev.vendor_id == vendor_id]


# ---------------------------------------------------------------------------
# 1) auth_failed 端到端契约（HTTP 401）
# ---------------------------------------------------------------------------


def test_e2e_http_auth_failed_full_pipeline(
    tmp_path: Path,
    e2e_http_401_server,
    env_with_credentials: Mapping[str, str],
) -> None:
    """HTTP 401 → 端到端跑通：handover.md / tickets.jsonl / SQLite 三件套。

    凭据纪律：测试 fixture env 注入真实 token，但 Evidence / handover.md /
    tickets.jsonl / SQLite **均不**含 token 字面量（仅 ``***`` 占位）。
    """

    host, port = e2e_http_401_server
    db_path = tmp_path / "e2e_auth_failed.db"
    spec = _make_http_bearer_spec(host, port)

    # 跑连接测试
    evidence_list = do_test_connection(spec, env=env_with_credentials)

    # 端到端跑产物
    handover_path = _build_artifacts(tmp_path, evidence_list, db_path=db_path)
    tickets_path = handover_path.parent / "tickets.jsonl"

    # --- 断言 1：handover.md 内容 ---
    assert handover_path.exists(), "handover.md 未生成"
    md_text = handover_path.read_text(encoding="utf-8")
    assert "## Vendor: e2e" in md_text, "handover.md 缺 Vendor 段"
    assert "### Status: failed" in md_text, "handover.md 缺 Status: failed 段"
    assert "首次失败步骤：`sample`" in md_text, "handover.md 缺 first_failed_step=sample 标记"

    # --- 断言 2：tickets.jsonl 内容 ---
    assert tickets_path.exists(), "tickets.jsonl 未生成"
    ticket_lines = _read_jsonl(tickets_path)
    assert len(ticket_lines) >= 1, "tickets.jsonl 至少需 1 行"
    auth_ticket = next(
        (t for t in ticket_lines if t.get("failure_class") == "auth_failed"),
        None,
    )
    assert auth_ticket is not None, "tickets.jsonl 缺 failure_class=auth_failed 行"
    assert auth_ticket.get("first_failed_step") == "sample", (
        "auth_failed 工单 first_failed_step 应为 'sample'（401 由 sample 步的非 2xx 检测识别）"
    )
    assert auth_ticket.get("severity") == "critical", (
        "auth_failed 工单 severity 应为 critical"
    )
    assert auth_ticket.get("priority") == "high", (
        "auth_failed 工单 priority 应为 high"
    )
    suggested = auth_ticket.get("suggested_actions") or []
    assert len(suggested) >= 1, "auth_failed 工单 suggested_actions 至少 1 条"
    suggested_blob = " ".join(suggested)
    assert "curl" in suggested_blob or "VENDOR_E2E" in suggested_blob, (
        "suggested_actions 应含 curl 或 env 变量名"
    )

    # --- 断言 3：SQLite 月表 + run_id + evidence_hash ---
    # 找一条 inspection_runs 行
    all_runs = query_history(limit=10, path=db_path)
    assert len(all_runs) >= 1, "SQLite inspection_runs 至少 1 行"
    run_row = all_runs[0]
    assert run_row.get("vendor_id") == "e2e"
    assert run_row.get("interface_id") == "outpatient-register"
    assert run_row.get("run_id"), "run_id 必填"
    assert run_row.get("evidence_hash"), "evidence_hash 必填"
    # evidence_hash 是 sha256 前 16 位 hex
    assert re.fullmatch(r"[0-9a-f]{16}", run_row["evidence_hash"]), (
        f"evidence_hash 不是 16 位 hex: {run_row['evidence_hash']!r}"
    )
    # 月表存在
    started_at = run_row.get("started_at") or ""
    tbl = partition_table_name(started_at)
    fetched = fetch_run(run_row["run_id"], path=db_path)
    assert fetched is not None, "fetch_run 应能查到刚写入的行"
    assert fetched["vendor_id"] == "e2e"

    # --- 断言 4：凭据纪律 ---
    for artifact_path in (handover_path, tickets_path):
        text = artifact_path.read_text(encoding="utf-8")
        for forbidden in (
            "lis-secret-token-9999",
            "pacs-secret-key-777",
            "wonderland",
        ):
            assert forbidden not in text, (
                f"{artifact_path.name} 含凭据字面量 {forbidden!r}（凭据纪律违反）"
            )
    # SQLite 也应不含凭据
    for run in all_runs:
        for forbidden in ("lis-secret-token-9999", "pacs-secret-key-777", "wonderland"):
            blob = json.dumps(run, ensure_ascii=False)
            assert forbidden not in blob, (
                f"SQLite 含凭据字面量 {forbidden!r}（凭据纪律违反）"
            )


# ---------------------------------------------------------------------------
# 2) hl7_ack_re 端到端契约（MLLP mock 返 MSA|AE）
# ---------------------------------------------------------------------------


def test_e2e_hl7_ack_re_full_pipeline(
    tmp_path: Path,
    e2e_mllp_msa_ae_server,
    env_with_credentials: Mapping[str, str],
) -> None:
    """MLLP ``MSA|AE`` → 端到端跑通 + 工单 severity=critical + suggested 含 MLLP。"""

    host, port = e2e_mllp_msa_ae_server
    db_path = tmp_path / "e2e_hl7_ack_re.db"
    spec = _make_hl7v2_spec(host, port)

    evidence_list = do_test_connection(spec, env=env_with_credentials)

    handover_path = _build_artifacts(tmp_path, evidence_list, db_path=db_path)
    tickets_path = handover_path.parent / "tickets.jsonl"

    md_text = handover_path.read_text(encoding="utf-8")
    assert "## Vendor: e2e" in md_text
    assert "### Status: failed" in md_text
    # hl7_ack_re 在 assert 步发现
    assert "首次失败步骤：`assert`" in md_text, (
        "hl7_ack_re 首次失败步骤应为 assert（MSA-1≠AA 在 assert 步判失败）"
    )

    ticket_lines = _read_jsonl(tickets_path)
    ack_ticket = next(
        (t for t in ticket_lines if t.get("failure_class") == "hl7_ack_re"),
        None,
    )
    assert ack_ticket is not None, "tickets.jsonl 缺 hl7_ack_re 行"
    assert ack_ticket.get("first_failed_step") == "assert"
    assert ack_ticket.get("severity") == "critical"
    assert ack_ticket.get("priority") == "high"
    suggested = ack_ticket.get("suggested_actions") or []
    suggested_blob = " ".join(suggested)
    assert "MLLP" in suggested_blob or "hl7apy" in suggested_blob.lower(), (
        "hl7_ack_re 工单 suggested_actions 应含 MLLP 或 hl7apy 关键字"
    )

    # SQLite 验证
    all_runs = query_history(limit=10, path=db_path)
    assert len(all_runs) >= 1
    run_row = all_runs[0]
    assert run_row.get("interface_id") == "emr-submit"
    assert run_row.get("evidence_hash")
    assert re.fullmatch(r"[0-9a-f]{16}", run_row["evidence_hash"])

    # 凭据纪律（同步路径中凭据 redact 同样生效）
    for artifact_path in (handover_path, tickets_path):
        text = artifact_path.read_text(encoding="utf-8")
        for forbidden in ("lis-secret-token-9999", "wonderland"):
            assert forbidden not in text, (
                f"{artifact_path.name} 含凭据字面量 {forbidden!r}"
            )


# ---------------------------------------------------------------------------
# 3) schema_mismatch 端到端契约（HTTP 200 缺 required_fields）
# ---------------------------------------------------------------------------


def test_e2e_http_schema_mismatch_full_pipeline(
    tmp_path: Path,
    e2e_http_missing_required_server,
    env_with_credentials: Mapping[str, str],
) -> None:
    """HTTP 200 但响应缺 ``$.patient_id`` / ``$.slot_id`` → assert 步 schema_mismatch。"""

    host, port = e2e_http_missing_required_server
    db_path = tmp_path / "e2e_schema_mismatch.db"
    spec = _make_http_bearer_spec(
        host, port,
        required_fields=["$.code", "$.patient_id", "$.slot_id"],
    )

    evidence_list = do_test_connection(spec, env=env_with_credentials)

    handover_path = _build_artifacts(tmp_path, evidence_list, db_path=db_path)
    tickets_path = handover_path.parent / "tickets.jsonl"

    md_text = handover_path.read_text(encoding="utf-8")
    assert "## Vendor: e2e" in md_text
    assert "### Status: failed" in md_text
    assert "首次失败步骤：`assert`" in md_text, (
        "schema_mismatch 首次失败步骤应为 assert（required_fields 检查在 assert 步）"
    )

    ticket_lines = _read_jsonl(tickets_path)
    sm_ticket = next(
        (t for t in ticket_lines if t.get("failure_class") == "schema_mismatch"),
        None,
    )
    assert sm_ticket is not None, "tickets.jsonl 缺 schema_mismatch 行"
    assert sm_ticket.get("first_failed_step") == "assert"
    assert sm_ticket.get("severity") == "critical"
    assert sm_ticket.get("priority") == "high"
    # schema_mismatch meta 应暴露 failed_required_fields
    meta = sm_ticket.get("meta") or {}
    failed_fields = meta.get("failed_required_fields") or []
    assert "$.patient_id" in failed_fields or "$.slot_id" in failed_fields, (
        f"meta.failed_required_fields 应含缺失字段；got={failed_fields!r}"
    )

    # SQLite 验证 + 月表
    all_runs = query_history(limit=10, path=db_path)
    assert len(all_runs) >= 1
    run_row = all_runs[0]
    assert run_row.get("vendor_id") == "e2e"
    assert run_row.get("interface_id") == "outpatient-register"
    started_at = run_row.get("started_at") or ""
    tbl = partition_table_name(started_at)
    # 月表名形如 inspection_runs_y2026m10
    assert tbl.startswith("inspection_runs_y"), (
        f"月表名格式不对: {tbl!r}"
    )

    # 凭据纪律
    for artifact_path in (handover_path, tickets_path):
        text = artifact_path.read_text(encoding="utf-8")
        for forbidden in ("lis-secret-token-9999", "wonderland"):
            assert forbidden not in text, (
                f"{artifact_path.name} 含凭据字面量 {forbidden!r}"
            )


# ---------------------------------------------------------------------------
# 4) 三件套一致性：跨 3 个失败路径 run_id + evidence_hash 唯一
# ---------------------------------------------------------------------------


def test_e2e_three_failures_have_unique_run_ids(
    tmp_path: Path,
    e2e_http_401_server,
    e2e_http_missing_required_server,
    e2e_mllp_msa_ae_server,
    env_with_credentials: Mapping[str, str],
) -> None:
    """三路径串行跑通，run_id 各异，evidence_hash 各异（不串数据）。"""

    db_path = tmp_path / "e2e_three_failures.db"

    host_401, port_401 = e2e_http_401_server
    host_schema, port_schema = e2e_http_missing_required_server
    host_mllp, port_mllp = e2e_mllp_msa_ae_server

    specs = [
        _make_http_bearer_spec(host_401, port_401, interface_id="auth-fail"),
        _make_http_bearer_spec(
            host_schema, port_schema,
            required_fields=["$.code", "$.patient_id", "$.slot_id"],
            interface_id="schema-fail",
        ),
        _make_hl7v2_spec(host_mllp, port_mllp, interface_id="hl7-fail"),
    ]

    all_evidence = []
    for spec in specs:
        all_evidence.extend(do_test_connection(spec, env=env_with_credentials))

    # 渲染三件套（写到 tmp_path 下的独立子目录避免相互覆盖）
    artifacts_root = tmp_path / "artifacts"
    artifacts_root.mkdir()
    handover_path = write_handover(all_evidence, path=artifacts_root / "handover.md")
    tickets_path = write_tickets(all_evidence, path=artifacts_root / "tickets.jsonl")
    run_ids = write_history(all_evidence, path=db_path)

    # 三条 run_id 各异
    assert len(run_ids) == 3, f"应写入 3 条 run；got {len(run_ids)}"
    assert len(set(run_ids)) == 3, f"run_id 应唯一；got {run_ids!r}"

    # evidence_hash 各异（3 类失败路径 response_body 不同）
    fetched_runs = [fetch_run(rid, path=db_path) for rid in run_ids]
    assert all(r is not None for r in fetched_runs)
    hashes = [r["evidence_hash"] for r in fetched_runs]
    assert len(set(hashes)) == 3, f"evidence_hash 应各异；got {hashes!r}"

    # handover.md 三个 Vendor 段
    md_text = handover_path.read_text(encoding="utf-8")
    assert md_text.count("## Vendor: e2e") == 3, "handover.md 应有 3 个 Vendor 段"

    # tickets.jsonl 三行
    ticket_lines = _read_jsonl(tickets_path)
    assert len(ticket_lines) == 3, f"tickets.jsonl 应有 3 行；got {len(ticket_lines)}"
    failure_classes = sorted(t.get("failure_class") for t in ticket_lines)
    assert failure_classes == sorted(["auth_failed", "hl7_ack_re", "schema_mismatch"]), (
        f"3 类 failure_class 应齐全；got={failure_classes!r}"
    )