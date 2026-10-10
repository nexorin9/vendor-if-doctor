"""真实数据形态契约测试（task 24）。

6 类现实场景：
1. **生僻字 / CJK Extension B** — 字段值含 ``李𠮷王`` (U+20BB7)，保留 Unicode 不丢字；
2. **GBK 编码** — 老 HIS / LIS 经常返回 GBK 字节，httpx 自动 utf-8 解码会抛
   ``UnicodeDecodeError``，需走 GBK 兜底；
3. **超长字段 > 32KB** — 病历文书 / 检验报告泛指整段长文，需截断到 32KB；
4. **必填字段空字符串** — ``"patient_id": ""`` 与缺失语义不同（前者通过 JSONPath
   ``exists`` 但业务判定为空）；
5. **必填字段 null** — JSON ``null`` 解析后是 Python ``None``；
6. **BOM + 重复 key** — 部分老接口响应以 ``\\xef\\xbb\\xbf`` 开头；同一 key 重复
   出现 Python ``json.loads`` 默认保留最后一个值，但其他 vendor 把第一个当主索引。

每条测试都构造一个 ``pytest-httpserver`` mock 端点，按形态断言：

* ``Evidence.response_body`` 摘要保留可见字符（生僻字不丢）；
* ``Evidence.meta['encoding']`` / ``bom_stripped`` / ``duplicate_keys`` 正确写入；
* ``failure_class`` 分类正确（脏行映射 ``schema_mismatch``）；
* Evidence **不**包含原始请求体或响应体的字节流。

凭据纪律
--------

* 所有测试用 Bearer 凭据；token 字面量**仅在 fixture 内出现**，从不写入 Evidence；
* 测试结束断言 ``response_body`` 不含 token 字面量。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping

import pytest
from pytest_httpserver import HTTPServer

from vendor_if_doctor.adapters.response_decode import (
    MAX_RESPONSE_BYTES,
    decode_response_bytes,
    parse_body_with_dup_keys,
    parse_response,
    strip_bom,
    truncate_body,
)
from vendor_if_doctor.connection.factory import build_connection
from vendor_if_doctor.connection.runner import STEP_NAMES
from vendor_if_doctor.connection.runner import test_connection_steps as _test_connection_steps
from vendor_if_doctor.spec.schema import (
    AuthBearer,
    HttpSampleEndpoint,
    InspectionConfig,
    SampleEndpoint,
    VendorInterface,
    VendorSpec,
)


# ---------------------------------------------------------------------------
# 工具：构造 inline vendor_spec 指向 mock server
# ---------------------------------------------------------------------------


def _pick_free_port() -> int:
    import socket as _socket
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http_spec(url_base: str, required_fields) -> VendorInterface:
    from urllib.parse import urlparse

    parsed = urlparse(url_base)
    interface = VendorInterface(
        vendor_id="his",
        interface_id="real-data-shape",
        display_name="HIS 真实数据形态测试接口",
        protocol="http",
        host=parsed.hostname or "127.0.0.1",
        port=parsed.port or 80,
        base_path="",
        timeout_seconds=5,
        auth=AuthBearer(secret_env="VENDOR_REAL_DATA_TOKEN"),
        sample_endpoint=SampleEndpoint(
            http=HttpSampleEndpoint(
                method="GET",
                path="/",
                headers={"Accept": "application/json"},
                body="",
            ),
        ),
        required_fields=required_fields,
    )
    return interface


@pytest.fixture
def real_data_token(monkeypatch: pytest.MonkeyPatch) -> str:
    token = "real-data-token-XYZ-12345"
    monkeypatch.setenv("VENDOR_REAL_DATA_TOKEN", token)
    return token


@pytest.fixture
def mock_http_shape() -> HTTPServer:
    """空 mock server；每个测试自己注册端点响应。"""

    server = HTTPServer(host="127.0.0.1", port=_pick_free_port())
    server.start()
    try:
        yield server
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# 类 1：生僻字 / CJK Extension B
# ---------------------------------------------------------------------------


def test_cjk_extension_b_preserved(
    mock_http_shape: HTTPServer,
    real_data_token: str,
) -> None:
    """患者姓名含生僻字（如 ``李𠮷王``，U+20BB7）时 evidence 不丢字。"""

    base_url = f"http://{mock_http_shape.host}:{mock_http_shape.port}"
    payload_dict = {
        "code": 200,
        "patient_id": "<EXAMPLE_PATIENT>",
        "slot_id": "<EXAMPLE_SLOT>",
        # 姓名含 CJK Extension B（U+20000-U+2FAFF）
        "patient_name": "李𠮷王",
        "remark": "𠮷是𠮷王字，生僻字扩展B",
    }
    # 强制 ensure_ascii=False 保留生僻字原始字符（不被 \u 转义）
    raw = json.dumps(payload_dict, ensure_ascii=False).encode("utf-8")
    mock_http_shape.expect_request("/").respond_with_data(raw, content_type="application/json")
    interface = _http_spec(base_url, ("$.code", "$.patient_id", "$.slot_id", "$.patient_name"))
    conn = build_connection(interface, env={})
    try:
        spec = VendorSpec(schema_version='1.0', run_window_minutes=30, severity_policy='default', interfaces=[interface], inspection=InspectionConfig())
        evidence_list = _test_connection_steps(spec, list(STEP_NAMES))
    finally:
        conn.close()

    # 找到 sample 与 assert step
    sample = next(e for e in evidence_list if e.step_name == "sample")
    assert sample.status == "pass"
    # 关键：response_body 含生僻字，不被截断或丢字
    assert sample.response_body is not None
    assert "李𠮷王" in sample.response_body
    assert "𠮷" in sample.response_body  # CJK Extension B 字符保留
    # 元数据写明编码（utf-8；中文属于 UTF-8 标准区）
    assert sample.meta.get("encoding") == "utf-8"
    # 重复 key 列表为空
    assert sample.meta.get("duplicate_keys") == []
    # 不含凭据
    assert real_data_token not in (sample.response_body or "")
    # assert 步骤成功
    assert_step = next(e for e in evidence_list if e.step_name == "assert")
    assert assert_step.status == "pass"


# ---------------------------------------------------------------------------
# 类 2：GBK 编码响应
# ---------------------------------------------------------------------------


def test_gbk_encoded_response_decoded(
    mock_http_shape: HTTPServer,
    real_data_token: str,
) -> None:
    """老 HIS 返回 GBK 编码 bytes 时 evidence 正确解码，不抛 UnicodeDecodeError。"""

    base_url = f"http://{mock_http_shape.host}:{mock_http_shape.port}"
    # GBK 编码的中文 payload
    payload_dict = {
        "code": 200,
        "patient_id": "<EXAMPLE_PATIENT>",
        "slot_id": "<EXAMPLE_SLOT>",
        "patient_name": "李建国",  # 简体
    }
    payload_bytes = json.dumps(payload_dict, ensure_ascii=False).encode("gbk")
    mock_http_shape.expect_request("/").respond_with_data(payload_bytes, content_type="application/json")
    interface = _http_spec(base_url, ("$.code", "$.patient_id", "$.slot_id"))
    conn = build_connection(interface, env={})
    try:
        spec = VendorSpec(schema_version='1.0', run_window_minutes=30, severity_policy='default', interfaces=[interface], inspection=InspectionConfig())
        evidence_list = _test_connection_steps(spec, list(STEP_NAMES))
    finally:
        conn.close()

    sample = next(e for e in evidence_list if e.step_name == "sample")
    assert sample.status == "pass", f"sample should pass but got {sample.failure_class}"
    # GBK 解码
    assert sample.meta.get("encoding") == "gbk"
    # 中文保留
    assert sample.response_body is not None
    assert "李建国" in sample.response_body
    # parsed_body 仍能拿到 dict（assert 步骤能跑通）
    assert_step = next(e for e in evidence_list if e.step_name == "assert")
    assert assert_step.status == "pass"


# ---------------------------------------------------------------------------
# 类 3：超长字段 > 32KB
# ---------------------------------------------------------------------------


def test_oversized_field_truncated_to_32kb(
    mock_http_shape: HTTPServer,
    real_data_token: str,
) -> None:
    """病历长字段 > 32KB 时 evidence 截断到 32KB 且 ``meta.truncated=true``。"""

    base_url = f"http://{mock_http_shape.host}:{mock_http_shape.port}"
    # 长字段 64KB
    long_text = "病历文书段落。" * 7000  # ~70KB 字符
    payload_dict = {
        "code": 200,
        "patient_id": "<EXAMPLE_PATIENT>",
        "slot_id": "<EXAMPLE_SLOT>",
        "report_text": long_text,
    }
    payload_bytes = json.dumps(payload_dict, ensure_ascii=False).encode("utf-8")
    assert len(payload_bytes) > 32 * 1024, "fixture should exceed 32KB"
    mock_http_shape.expect_request("/").respond_with_data(payload_bytes, content_type="application/json")
    interface = _http_spec(base_url, ("$.code", "$.patient_id", "$.slot_id"))
    conn = build_connection(interface, env={})
    try:
        spec = VendorSpec(schema_version='1.0', run_window_minutes=30, severity_policy='default', interfaces=[interface], inspection=InspectionConfig())
        evidence_list = _test_connection_steps(spec, list(STEP_NAMES))
    finally:
        conn.close()

    sample = next(e for e in evidence_list if e.step_name == "sample")
    assert sample.status == "pass"
    # response_body 长度 ≤ 32KB
    assert sample.response_body is not None
    assert len(sample.response_body) <= MAX_RESPONSE_BYTES
    # meta.truncated = True
    assert sample.meta.get("truncated") is True
    # raw_size > 32KB
    assert sample.meta.get("raw_size", 0) > 32 * 1024
    # 不含凭据
    assert real_data_token not in (sample.response_body or "")


# ---------------------------------------------------------------------------
# 类 4：必填字段值为空字符串
# ---------------------------------------------------------------------------


def test_required_field_empty_string(
    mock_http_shape: HTTPServer,
    real_data_token: str,
) -> None:
    """必填字段 ``patient_id`` 为空字符串时 evidence.status=fail / fc=schema_mismatch。"""

    base_url = f"http://{mock_http_shape.host}:{mock_http_shape.port}"
    payload_dict = {
        "code": 200,
        "patient_id": "",  # 空字符串
        "slot_id": "<EXAMPLE_SLOT>",
    }
    mock_http_shape.expect_request("/").respond_with_json(payload_dict)
    interface = _http_spec(base_url, ("$.code", "$.patient_id", "$.slot_id"))
    conn = build_connection(interface, env={})
    try:
        spec = VendorSpec(schema_version='1.0', run_window_minutes=30, severity_policy='default', interfaces=[interface], inspection=InspectionConfig())
        evidence_list = _test_connection_steps(spec, list(STEP_NAMES))
    finally:
        conn.close()

    # sample pass（HTTP 200 + 解析成功）
    sample = next(e for e in evidence_list if e.step_name == "sample")
    assert sample.status == "pass"
    # assert step fail / fc=schema_mismatch
    assert_step = next(e for e in evidence_list if e.step_name == "assert")
    assert assert_step.status == "fail"
    assert assert_step.failure_class == "schema_mismatch"
    # failed_required_fields 暴露缺失字段
    assert "$.patient_id" in (assert_step.meta.get("failed_required_fields") or [])


# ---------------------------------------------------------------------------
# 类 5：必填字段值为 null
# ---------------------------------------------------------------------------


def test_required_field_null_value(
    mock_http_shape: HTTPServer,
    real_data_token: str,
) -> None:
    """必填字段 ``patient_id`` 为 ``null`` 时 evidence.status=fail / fc=schema_mismatch。"""

    base_url = f"http://{mock_http_shape.host}:{mock_http_shape.port}"
    payload_dict = {
        "code": 200,
        "patient_id": None,
        "slot_id": "<EXAMPLE_SLOT>",
    }
    payload_bytes = json.dumps(payload_dict).encode("utf-8")
    mock_http_shape.expect_request("/").respond_with_data(payload_bytes, content_type="application/json")
    interface = _http_spec(base_url, ("$.code", "$.patient_id", "$.slot_id"))
    conn = build_connection(interface, env={})
    try:
        spec = VendorSpec(schema_version='1.0', run_window_minutes=30, severity_policy='default', interfaces=[interface], inspection=InspectionConfig())
        evidence_list = _test_connection_steps(spec, list(STEP_NAMES))
    finally:
        conn.close()

    sample = next(e for e in evidence_list if e.step_name == "sample")
    assert sample.status == "pass"
    assert_step = next(e for e in evidence_list if e.step_name == "assert")
    assert assert_step.status == "fail"
    assert assert_step.failure_class == "schema_mismatch"


# ---------------------------------------------------------------------------
# 类 6：响应体含 BOM + 重复 key
# ---------------------------------------------------------------------------


def test_response_bom_stripped_and_dup_keys_detected(
    mock_http_shape: HTTPServer,
    real_data_token: str,
) -> None:
    """响应以 UTF-8 BOM 开头 + 重复 key 时 evidence 正确剥离 BOM + 标记重复 key。"""

    base_url = f"http://{mock_http_shape.host}:{mock_http_shape.port}"
    # 注意：JSON 规范不允许同一对象同一 key 重复，但部分老 HIS 会这样做。
    # 这里用 raw bytes 注入"BOM + 重复 key"形态（Python json 解析保留最后一个值）。
    raw = b'\xef\xbb\xbf{"code": 200, "patient_id": "<EXAMPLE_FIRST>", "patient_id": "<EXAMPLE_SECOND>", "slot_id": "<EXAMPLE_SLOT>"}'
    mock_http_shape.expect_request("/").respond_with_data(raw, content_type="application/json")
    interface = _http_spec(base_url, ("$.code", "$.patient_id", "$.slot_id"))
    conn = build_connection(interface, env={})
    try:
        spec = VendorSpec(schema_version='1.0', run_window_minutes=30, severity_policy='default', interfaces=[interface], inspection=InspectionConfig())
        evidence_list = _test_connection_steps(spec, list(STEP_NAMES))
    finally:
        conn.close()

    sample = next(e for e in evidence_list if e.step_name == "sample")
    # response_body 不应以 BOM 字节开头（已剥离）
    assert sample.response_body is not None
    assert not sample.response_body.startswith("﻿")  # BOM Unicode 字符
    # 元数据正确写入：标记了重复 key
    assert sample.meta.get("bom_stripped") is True
    assert "patient_id" in (sample.meta.get("duplicate_keys") or [])
    assert sample.meta.get("encoding") == "utf-8"
    # assert step 仍能跑通（patient_id 存在，Python json 默认保留最后一个值）
    assert_step = next(e for e in evidence_list if e.step_name == "assert")
    assert assert_step.status == "pass"


# ---------------------------------------------------------------------------
# 综合：所有 6 类形态同时存在
# ---------------------------------------------------------------------------


def test_combo_real_data_shapes(
    mock_http_shape: HTTPServer,
    real_data_token: str,
) -> None:
    """6 类形态综合：GBK + 中文字符 + BOM + 中等长度字段同时存在。

    注：生僻字（CJK Extension B）不在 GBK 字符集内，所以本组合测试用 GBK 编码
    时只放 GBK 范围内的中文（简体）。CJK Extension B 的保留验证在
    :func:`test_cjk_extension_b_preserved` 单独覆盖。
    """

    base_url = f"http://{mock_http_shape.host}:{mock_http_shape.port}"
    payload_dict = {
        "code": 200,
        "patient_id": "<EXAMPLE_PATIENT>",
        "slot_id": "<EXAMPLE_SLOT>",
        "patient_name": "李建国",  # GBK 范围内的中文
        "remark": "复合测试 " * 1000,  # 中等长度
    }
    # GBK 编码 + BOM 前缀
    payload_gbk = json.dumps(payload_dict, ensure_ascii=False).encode("gbk")
    raw_with_bom = b"\xef\xbb\xbf" + payload_gbk
    mock_http_shape.expect_request("/").respond_with_data(raw_with_bom, content_type="application/json")
    interface = _http_spec(base_url, ("$.code", "$.patient_id", "$.slot_id", "$.patient_name"))
    conn = build_connection(interface, env={})
    try:
        spec = VendorSpec(schema_version='1.0', run_window_minutes=30, severity_policy='default', interfaces=[interface], inspection=InspectionConfig())
        evidence_list = _test_connection_steps(spec, list(STEP_NAMES))
    finally:
        conn.close()

    sample = next(e for e in evidence_list if e.step_name == "sample")
    # 综合：sample 步仍能解析（GBK + BOM + 中文字符）
    assert sample.response_body is not None
    # BOM 剥离
    assert not sample.response_body.startswith("﻿")
    # GBK 解码
    assert sample.meta.get("encoding") == "gbk"
    # 中文保留
    assert "李建国" in sample.response_body
    # 不含凭据
    assert real_data_token not in sample.response_body
    # meta.bom_stripped
    assert sample.meta.get("bom_stripped") is True


# ---------------------------------------------------------------------------
# 工具函数单元测试（response_decode 模块直接覆盖）
# ---------------------------------------------------------------------------


def test_decode_response_bytes_strip_bom() -> None:
    raw = b'\xef\xbb\xbf{"hello": "world"}'
    result = decode_response_bytes(raw)
    assert result.bom_stripped is True
    assert result.text == '{"hello": "world"}'
    assert result.encoding == "utf-8"
    assert result.parsed == {"hello": "world"}
    assert result.duplicate_keys == []


def test_decode_response_bytes_gbk_fallback() -> None:
    raw = '{"你好": "世界"}'.encode("gbk")
    result = decode_response_bytes(raw)
    assert result.encoding == "gbk"
    assert "你好" in result.text
    assert result.parsed == {"你好": "世界"}


def test_decode_response_bytes_latin1_fallback() -> None:
    # bytes 含无效 UTF-8 / GBK 序列，触发 latin-1 兜底
    raw = b'\x80\x81\x82'
    text = decode_response_bytes(raw).text
    assert len(text) >= 1  # latin-1 永不失败


def test_strip_bom_utf8() -> None:
    data, stripped = strip_bom(b"\xef\xbb\xbfhello")
    assert stripped is True
    assert data == b"hello"


def test_strip_bom_no_bom() -> None:
    data, stripped = strip_bom(b"hello")
    assert stripped is False
    assert data == b"hello"


def test_parse_body_with_dup_keys() -> None:
    parsed, err, dups = parse_body_with_dup_keys('{"a": 1, "b": 2, "a": 3}')
    assert err is None
    assert parsed == {"a": 3, "b": 2}  # Python 默认保留最后一个
    assert "a" in dups


def test_parse_body_invalid_json() -> None:
    parsed, err, dups = parse_body_with_dup_keys("{not json")
    assert parsed is None
    assert err is not None
    assert "JSONDecodeError" in err


def test_truncate_body_under_limit() -> None:
    text, truncated = truncate_body("hello", max_bytes=100)
    assert text == "hello"
    assert truncated is False


def test_truncate_body_over_limit() -> None:
    long_text = "x" * 200
    text, truncated = truncate_body(long_text, max_bytes=100)
    assert len(text) == 100
    assert truncated is True


# ---------------------------------------------------------------------------
# 凭据纪律
# ---------------------------------------------------------------------------


def test_credentials_not_in_evidence_response_body(
    mock_http_shape: HTTPServer,
    real_data_token: str,
) -> None:
    """综合验证：6 类形态下 evidence.response_body 都不含 token 字面量。"""

    base_url = f"http://{mock_http_shape.host}:{mock_http_shape.port}"
    payload_dict = {
        "code": 200,
        "patient_id": "<EXAMPLE_PATIENT>",
        "slot_id": "<EXAMPLE_SLOT>",
        "patient_name": "李𠮷王",  # 生僻字
        "long_text": "测试 " * 3000,  # 中等长度
    }
    payload_bytes = json.dumps(payload_dict, ensure_ascii=False).encode("utf-8")
    mock_http_shape.expect_request("/").respond_with_data(payload_bytes, content_type="application/json")
    interface = _http_spec(base_url, ("$.code", "$.patient_id", "$.slot_id"))
    conn = build_connection(interface, env={})
    try:
        spec = VendorSpec(schema_version='1.0', run_window_minutes=30, severity_policy='default', interfaces=[interface], inspection=InspectionConfig())
        evidence_list = _test_connection_steps(spec, list(STEP_NAMES))
    finally:
        conn.close()

    for ev in evidence_list:
        assert real_data_token not in (ev.response_body or "")
        assert real_data_token not in (ev.summary or "")
        assert real_data_token not in (ev.command or "")


__all__ = [
    "test_cjk_extension_b_preserved",
    "test_gbk_encoded_response_decoded",
    "test_oversized_field_truncated_to_32kb",
    "test_required_field_empty_string",
    "test_required_field_null_value",
    "test_response_bom_stripped_and_dup_keys_detected",
    "test_combo_real_data_shapes",
    "test_decode_response_bytes_strip_bom",
    "test_decode_response_bytes_gbk_fallback",
    "test_decode_response_bytes_latin1_fallback",
    "test_strip_bom_utf8",
    "test_strip_bom_no_bom",
    "test_parse_body_with_dup_keys",
    "test_parse_body_invalid_json",
    "test_truncate_body_under_limit",
    "test_truncate_body_over_limit",
    "test_credentials_not_in_evidence_response_body",
]