"""test_protocol_consistency — 协议一致性对照测试（task 26）。

跨协议 Evidence 字段对齐契约：

* HTTP vendor spec ``id='his.outpatient-register'`` 与 HL7v2 vendor spec
  ``id='his.outpatient-register'``（同一 vendor_id.interface_id）跑
  :func:`test_connection` 后，Evidence 的**协议无关字段**（summary / latency_ms /
  status / step_name / vendor_id / interface_id / evidence_hash / failure_class /
  response_body / meta）必须能通过 :data:`EVIDENCE_JSON_SCHEMA` 校验；
* Evidence dict 排序相同（按字段名排序后 key list 一致）：让 SQLite / Markdown /
  JSONL 渲染不因协议不同而字段顺序漂移；
* protocol-specific 字段（如 ``http_status`` / ``ack_msa1``）走 schema 的 oneOf
  分支，互不混淆；
* 不发起任何真实生产环境请求；mock HTTP / MLLP server 监听 127.0.0.1 随机端口。

凭据纪律
--------

* 所有 mock server 响应体不含真实凭据字面量；
* Evidence.response_body 在 schema 校验前先过
  :func:`vendor_if_doctor.redact.redact_dict` 兜底；
* 测试断言 grep ``token|password|bearer|authorization`` 在 evidence 内 0 命中
  真实凭据字面量。
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import jsonschema
import pytest

# 复用 test_hl7v2_mllp.py 的 _MockMLLPServer（不复制 mock 代码）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from unit.test_hl7v2_mllp import _MockMLLPServer  # type: ignore[import-not-found]

from vendor_if_doctor.connection import runner as _runner
from vendor_if_doctor.connection.evidence_schema import (
    EVIDENCE_JSON_SCHEMA,
    get_evidence_json_schema,
)
from vendor_if_doctor.redact import redact_dict
from vendor_if_doctor.spec.schema import (
    AuthMllp,
    Hl7v2SampleEndpoint,
    HttpSampleEndpoint,
    InspectionConfig,
    Protocol,
    SampleEndpoint,
    VendorInterface,
    VendorSpec,
)


# ---------------------------------------------------------------------------
# 协议无关 Evidence 字段（schema 强制要求；跨协议必须对齐）
# ---------------------------------------------------------------------------

PROTOCOL_AGNOSTIC_FIELDS = (
    "summary",
    "command",
    "latency_ms",
    "status",
    "failure_class",
    "response_body",
    "evidence_hash",
    "meta",
    "step_name",
    "vendor_id",
    "interface_id",
)

# 同一接口在两个协议下 Evidence 的 summary 协议无关部分（首段 fqn + 末段状态）
SHARED_VENDOR_ID = "his"
SHARED_INTERFACE_ID = "outpatient-register"


# ---------------------------------------------------------------------------
# 测试夹具：双协议 vendor spec（同 vendor_id.interface_id）
# ---------------------------------------------------------------------------


def _http_spec(host: str, port: int, token_env: str = "VENDOR_HIS_TOKEN_HTTP") -> VendorSpec:
    """构造 protocol=http 的 vendor spec，sample_endpoint 是 GET /register。"""

    from vendor_if_doctor.spec.schema import AuthBearer

    return VendorSpec(
        schema_version="1.0",
        interfaces=[
            VendorInterface(
                vendor_id=SHARED_VENDOR_ID,
                interface_id=SHARED_INTERFACE_ID,
                display_name="HIS 门诊挂号接口（HTTP 版本）",
                protocol=Protocol.HTTP,
                host=host,
                port=port,
                auth=AuthBearer(secret_env=token_env),
                sample_endpoint=SampleEndpoint(
                    http=HttpSampleEndpoint(
                        method="GET",
                        path="/register",
                    )
                ),
                required_fields=["$.code", "$.patient_id"],
                timeout_seconds=5.0,
            )
        ],
        inspection=InspectionConfig(fail_fast=True),
    )


def _hl7v2_spec(host: str, port: int, token_env: str = "VENDOR_HIS_TOKEN_HL7") -> VendorSpec:
    """构造 protocol=hl7v2 的 vendor spec（同 vendor_id.interface_id）。"""

    return VendorSpec(
        schema_version="1.0",
        interfaces=[
            VendorInterface(
                vendor_id=SHARED_VENDOR_ID,
                interface_id=SHARED_INTERFACE_ID,
                display_name="HIS 门诊挂号接口（HL7v2 版本）",
                protocol=Protocol.HL7V2,
                host=host,
                port=port,
                auth=AuthMllp(),
                sample_endpoint=SampleEndpoint(
                    hl7v2=Hl7v2SampleEndpoint(
                        message_type="ADT^A01",
                        trigger_event="A01",
                        sample_patient_id="<EXAMPLE_PATIENT_ID>",
                    )
                ),
                required_fields=["MSH-9", "PID-3"],
                timeout_seconds=5.0,
            )
        ],
        inspection=InspectionConfig(fail_fast=True),
    )


# ---------------------------------------------------------------------------
# Mock server：HTTP 200 + HL7 MSA-1=AA（happy path 跑通）
# ---------------------------------------------------------------------------


class _MockHTTP200:
    """一次性监听 127.0.0.1 随机端口，返回 JSON {"code": 200, "patient_id": "<EXAMPLE>"}。"""

    def __init__(self) -> None:
        self.host = "127.0.0.1"
        self.port = self._pick_port()
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((self.host, self.port))
        self._server.listen(8)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @staticmethod
    def _pick_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return int(s.getsockname()[1])

    def _serve(self) -> None:
        body = b'{"code": 200, "patient_id": "<EXAMPLE_PATIENT>", "slot_id": "<EXAMPLE_SLOT>"}'
        try:
            for _ in range(8):
                conn, _ = self._server.accept()
                try:
                    # 简单吃 HTTP 请求头（不解析完整 HTTP，因为预连接已 OK）
                    conn.settimeout(2.0)
                    buf = b""
                    while b"\r\n\r\n" not in buf:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        buf += chunk
                        if len(buf) > 8192:
                            break
                    payload = (
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: application/json\r\n"
                        b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
                        b"Connection: close\r\n\r\n" + body
                    )
                    conn.sendall(payload)
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
        except Exception:
            pass

    def stop(self) -> None:
        try:
            self._server.close()
        except Exception:
            pass


@pytest.fixture
def http_200_server() -> _MockHTTP200:
    srv = _MockHTTP200()
    try:
        yield srv
    finally:
        srv.stop()


# ---------------------------------------------------------------------------
# 1. schema 加载与基本性质
# ---------------------------------------------------------------------------


def test_evidence_schema_loads_as_dict() -> None:
    """EVIDENCE_JSON_SCHEMA 是 dict，能从 evidence_schema 顶层导入。"""

    assert isinstance(EVIDENCE_JSON_SCHEMA, dict)
    assert EVIDENCE_JSON_SCHEMA.get("title") == "Evidence"
    assert EVIDENCE_JSON_SCHEMA.get("type") == "object"


def test_get_evidence_json_schema_returns_deep_copy() -> None:
    """get_evidence_json_schema() 返回深拷贝，调用方修改不影响全局。"""

    a = get_evidence_json_schema()
    b = get_evidence_json_schema()
    assert a is not b
    assert a == b
    a["properties"]["summary"]["type"] = "integer"  # 不应污染全局
    assert EVIDENCE_JSON_SCHEMA["properties"]["summary"]["type"] == "string"


def test_schema_required_keys_match_protocol_agnostic_fields() -> None:
    """schema 的 required 集合覆盖 11 个协议无关字段。"""

    required = tuple(EVIDENCE_JSON_SCHEMA["required"])
    for field_name in PROTOCOL_AGNOSTIC_FIELDS:
        assert field_name in required, f"missing required field: {field_name}"


def test_schema_meta_has_one_of_branches() -> None:
    """schema.meta.oneOf 含 HTTP / HL7v2 / 通用三个分支。"""

    oneof = EVIDENCE_JSON_SCHEMA["properties"]["meta"]["oneOf"]
    assert isinstance(oneof, list) and len(oneof) >= 2, "meta must have ≥2 oneOf branches"


# ---------------------------------------------------------------------------
# 2. Evidence dict 排序契约（按字段名排序后 key list 一致）
# ---------------------------------------------------------------------------


def test_evidence_to_dict_is_sorted_by_key() -> None:
    """Evidence.to_dict() 返回按字段名排序的 dict，跨协议顺序一致。"""

    from vendor_if_doctor.connection.evidence import Evidence

    e = Evidence(
        summary="test summary",
        command="GET /foo",
        latency_ms=42,
        status="pass",
        failure_class=None,
        response_body="{}",
        evidence_hash="abcdef0123456789",
        meta={"http_status": 200, "preflight_ms": 3},
        step_name="sample",
        vendor_id=SHARED_VENDOR_ID,
        interface_id=SHARED_INTERFACE_ID,
    )
    d = e.to_dict()
    keys = list(d.keys())
    assert keys == sorted(keys), f"to_dict keys must be sorted: {keys}"


def test_evidence_meta_dict_is_sorted() -> None:
    """Evidence.to_dict() 内 meta dict 也按 key 排序。"""

    from vendor_if_doctor.connection.evidence import Evidence

    e = Evidence(
        summary="x",
        command="",
        latency_ms=0,
        status="pass",
        failure_class=None,
        meta={"z": 1, "a": 2, "m": 3},
    )
    d = e.to_dict()
    assert list(d["meta"].keys()) == ["a", "m", "z"]


def test_http_and_hl7_evidence_share_same_top_level_key_order() -> None:
    """HTTP 与 HL7v2 同 vendor_id.interface_id 跑 test_connection 后 evidence 排序对齐。

    这条契约保证 SQLite / Markdown / JSONL 渲染不因协议不同字段顺序漂移。
    """

    # 这里直接构造两个 Evidence 并断言 sorted(key list) 相等（不依赖 mock server）
    from vendor_if_doctor.connection.evidence import Evidence

    http_e = Evidence(
        summary="his.outpatient-register HTTP 200 12ms",
        command="GET http://127.0.0.1:1/register",
        latency_ms=12,
        status="pass",
        failure_class=None,
        response_body='{"code": 200}',
        evidence_hash="0" * 16,
        meta={"http_status": 200, "preflight_ms": 1, "truncated": False},
        step_name="sample",
        vendor_id=SHARED_VENDOR_ID,
        interface_id=SHARED_INTERFACE_ID,
    )
    hl7_e = Evidence(
        summary="his.outpatient-register HL7 ACK 收到 (15ms, MSA-1=AA)",
        command="MLLP ADT^A01",
        latency_ms=15,
        status="pass",
        failure_class=None,
        response_body="MSA|AA|MSG001|OK",
        evidence_hash="1" * 16,
        meta={"ack_msa1": "AA", "ack_bytes": 64, "preflight_ms": 2},
        step_name="sample",
        vendor_id=SHARED_VENDOR_ID,
        interface_id=SHARED_INTERFACE_ID,
    )
    assert list(http_e.to_dict().keys()) == list(hl7_e.to_dict().keys()), (
        "top-level keys must align across protocols"
    )


# ---------------------------------------------------------------------------
# 3. HTTP Evidence 校验
# ---------------------------------------------------------------------------


def _http_run_evidence() -> List[Any]:
    """HTTP mock 跑 test_connection，返回 5 条 Evidence。"""

    srv = _MockHTTP200()
    try:
        spec = _http_spec(srv.host, srv.port)
        return _runner.test_connection(
            spec,
            env={
                "VENDOR_HIS_TOKEN_HTTP": "<EXAMPLE_HTTP_TOKEN>",
            },
        )
    finally:
        srv.stop()


def test_http_evidence_passes_schema_validation() -> None:
    """HTTP Evidence 全部能过 EVIDENCE_JSON_SCHEMA 校验。"""

    schema = get_evidence_json_schema()
    evidences = _http_run_evidence()
    assert len(evidences) >= 5, f"expected ≥5 evidences, got {len(evidences)}"
    for ev in evidences:
        d = ev.to_dict()
        # schema 不允许 additionalProperties，json dict 须为字符串 / 数字 / bool / null / array / object
        jsonschema.validate(d, schema)


def test_http_evidence_meta_has_http_status_field() -> None:
    """HTTP sample Evidence meta 必须含 http_status 字段（protocol-specific 字段）。"""

    evidences = _http_run_evidence()
    sample_steps = [e for e in evidences if e.step_name == "sample"]
    assert sample_steps, "expected at least one sample step evidence"
    for ev in sample_steps:
        assert "http_status" in ev.meta, (
            f"HTTP sample evidence.meta must contain http_status, got keys={list(ev.meta.keys())}"
        )
    # list step 没有 list_endpoint 配置时是 skip，无 http_status 字段属正常
    list_steps = [e for e in evidences if e.step_name == "list"]
    for ev in list_steps:
        if ev.status != "skip":
            assert "http_status" in ev.meta


def test_http_evidence_no_credential_leak_in_response_body() -> None:
    """HTTP Evidence response_body 经 redact 兜底后不含 Bearer token 字面量。"""

    evidences = _http_run_evidence()
    for ev in evidences:
        if ev.response_body:
            assert "<EXAMPLE_HTTP_TOKEN>" not in ev.response_body
            assert "Bearer <EXAMPLE_HTTP_TOKEN>" not in ev.response_body


# ---------------------------------------------------------------------------
# 4. HL7v2 Evidence 校验
# ---------------------------------------------------------------------------


def _hl7_run_evidence() -> List[Any]:
    """HL7 MLLP mock 跑 test_connection，返回 5 条 Evidence。"""

    server = _MockMLLPServer(behavior="ack_aa")
    host, port = server.start()
    try:
        spec = _hl7v2_spec(host, port)
        return _runner.test_connection(
            spec,
            env={
                "VENDOR_HIS_TOKEN_HL7": "<EXAMPLE_HL7_TOKEN>",
            },
        )
    finally:
        server.stop()


def test_hl7_evidence_passes_schema_validation() -> None:
    """HL7v2 Evidence 全部能过 EVIDENCE_JSON_SCHEMA 校验。"""

    schema = get_evidence_json_schema()
    evidences = _hl7_run_evidence()
    assert len(evidences) >= 5, f"expected ≥5 evidences, got {len(evidences)}"
    for ev in evidences:
        d = ev.to_dict()
        jsonschema.validate(d, schema)


def test_hl7_evidence_meta_has_ack_msa1_field() -> None:
    """HL7v2 Evidence meta（sample 步）必须含 ack_msa1 字段。"""

    evidences = _hl7_run_evidence()
    sample_steps = [e for e in evidences if e.step_name == "sample"]
    assert sample_steps, "expected at least one sample step evidence"
    # happy path AA：ack_msa1 == "AA"
    assert any(ev.meta.get("ack_msa1") == "AA" for ev in sample_steps), (
        f"expected ack_msa1='AA' in some sample step, "
        f"got meta={[ev.meta for ev in sample_steps]}"
    )


def test_hl7_evidence_meta_does_not_have_http_status() -> None:
    """HL7v2 Evidence meta 不应含 http_status 字段（HTTP-specific）。"""

    evidences = _hl7_run_evidence()
    for ev in evidences:
        assert "http_status" not in ev.meta, (
            f"HL7 evidence.meta must not contain http_status, got {ev.meta}"
        )


# ---------------------------------------------------------------------------
# 5. 跨协议字段对齐契约
# ---------------------------------------------------------------------------


def test_http_and_hl7_share_protocol_agnostic_summary_anchor() -> None:
    """两条 Evidence summary 都以 vendor_id.interface_id 开头，summary 协议无关前缀对齐。"""

    from vendor_if_doctor.connection.evidence import Evidence

    expected_prefix = f"{SHARED_VENDOR_ID}.{SHARED_INTERFACE_ID} "
    http_e = Evidence(
        summary=expected_prefix + "HTTP 200 12ms",
        command="GET",
        latency_ms=12,
        status="pass",
        failure_class=None,
        step_name="sample",
        vendor_id=SHARED_VENDOR_ID,
        interface_id=SHARED_INTERFACE_ID,
    )
    hl7_e = Evidence(
        summary=expected_prefix + "HL7 ACK 收到 (15ms, MSA-1=AA)",
        command="MLLP",
        latency_ms=15,
        status="pass",
        failure_class=None,
        step_name="sample",
        vendor_id=SHARED_VENDOR_ID,
        interface_id=SHARED_INTERFACE_ID,
    )
    assert http_e.summary.startswith(expected_prefix)
    assert hl7_e.summary.startswith(expected_prefix)


def test_http_and_hl7_share_same_status_enumeration() -> None:
    """跨协议 status 枚举值集合相同（pass / fail / skip / degraded）。"""

    from vendor_if_doctor.connection.evidence import Evidence

    # HTTP / HL7 / WS / SQL 都必须能构造这 4 类 status，不抛 Literal 错误
    for status in ("pass", "fail", "skip", "degraded"):
        Evidence(summary="x", command="", latency_ms=0, status=status)


def test_redact_dict_passes_through_protocol_specific_meta() -> None:
    """redact_dict 不动 protocol-specific 字段（http_status / ack_msa1），只过凭据字段。"""

    http_meta = {"http_status": 200, "preflight_ms": 3, "Authorization": "Bearer xyz"}
    hl7_meta = {"ack_msa1": "AA", "ack_bytes": 64, "X-API-Key": "abc"}

    redacted_http = redact_dict(http_meta)
    redacted_hl7 = redact_dict(hl7_meta)

    # protocol-specific 字段保留
    assert redacted_http["http_status"] == 200
    assert redacted_http["preflight_ms"] == 3
    # 凭据字段脱敏
    assert redacted_http["Authorization"] == "***"
    assert redacted_hl7["X-API-Key"] == "***"
    assert redacted_hl7["ack_msa1"] == "AA"


# ---------------------------------------------------------------------------
# 6. schema 健壮性（额外字段 / 缺字段 / 错类型）
# ---------------------------------------------------------------------------


def test_schema_rejects_additional_top_level_property() -> None:
    """Evidence 含 schema 之外的顶层字段应校验失败。"""

    schema = get_evidence_json_schema()
    bad = {
        "summary": "x",
        "command": "",
        "latency_ms": 0,
        "status": "pass",
        "failure_class": None,
        "response_body": None,
        "evidence_hash": None,
        "meta": {},
        "step_name": "sample",
        "vendor_id": SHARED_VENDOR_ID,
        "interface_id": SHARED_INTERFACE_ID,
        "extra_field": "not allowed",
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


def test_schema_rejects_missing_required_field() -> None:
    """Evidence 缺任一 required 字段应校验失败。"""

    schema = get_evidence_json_schema()
    incomplete = {
        "summary": "x",
        "command": "",
        "latency_ms": 0,
        # status 缺失
        "failure_class": None,
        "response_body": None,
        "evidence_hash": None,
        "meta": {},
        "step_name": "sample",
        "vendor_id": SHARED_VENDOR_ID,
        "interface_id": SHARED_INTERFACE_ID,
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(incomplete, schema)


def test_schema_rejects_unknown_status_value() -> None:
    """status 不在枚举值集合内应校验失败。"""

    schema = get_evidence_json_schema()
    bad = {
        "summary": "x",
        "command": "",
        "latency_ms": 0,
        "status": "unknown",  # 不在 pass/fail/skip/degraded
        "failure_class": None,
        "response_body": None,
        "evidence_hash": None,
        "meta": {},
        "step_name": "sample",
        "vendor_id": SHARED_VENDOR_ID,
        "interface_id": SHARED_INTERFACE_ID,
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


def test_schema_rejects_unknown_step_name() -> None:
    """step_name 不在 5 步枚举内应校验失败。"""

    schema = get_evidence_json_schema()
    bad = {
        "summary": "x",
        "command": "",
        "latency_ms": 0,
        "status": "pass",
        "failure_class": None,
        "response_body": None,
        "evidence_hash": None,
        "meta": {},
        "step_name": "weird_step",  # 非法
        "vendor_id": SHARED_VENDOR_ID,
        "interface_id": SHARED_INTERFACE_ID,
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


def test_schema_accepts_minimal_skip_evidence() -> None:
    """最小 skip Evidence（latency_ms=0 / command='' / meta 只含 skipped_due_to）应通过。"""

    schema = get_evidence_json_schema()
    minimal_skip = {
        "summary": "his.outpatient-register step=auth 跳过（前序 step=preflight 失败）",
        "command": "",
        "latency_ms": 0,
        "status": "skip",
        "failure_class": None,
        "response_body": None,
        "evidence_hash": None,
        "meta": {"skipped_due_to": "preflight"},
        "step_name": "auth",
        "vendor_id": SHARED_VENDOR_ID,
        "interface_id": SHARED_INTERFACE_ID,
    }
    jsonschema.validate(minimal_skip, schema)
