"""HL7v2 消息模板库测试（task 16）。"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

# Allow running as a single file from project root
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from hl7apy.parser import parse_message

from vendor_if_doctor.adapters.hl7_templates import (
    ADTPatient,
    ORMOrder,
    ORUResult,
    SIUSchedule,
    _FIELD_SANITIZE_PATTERN,
    _sanitize_field,
    build_adt_a01,
    build_message_by_type,
    build_orm_o01,
    build_oru_r01,
    build_siu_s12,
    list_supported_message_types,
)
from vendor_if_doctor.spec.schema import (
    AuthMllp,
    Hl7v2SampleEndpoint,
    Protocol,
    SampleEndpoint,
    VendorInterface,
)


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------


def _make_interface() -> VendorInterface:
    """构造一条最简 HL7v2 VendorInterface（host=127.0.0.1，无真实网络）。"""

    sample = Hl7v2SampleEndpoint(
        message_type="ADT^A01",
        trigger_event="A01",
        sample_patient_id="<EXAMPLE_PATIENT_ID>",
    )
    return VendorInterface(
        vendor_id="his",
        interface_id="outpatient-register",
        display_name="his outpatient-register",
        protocol=Protocol.HL7V2,
        host="127.0.0.1",
        port=12575,
        base_path="/",
        timeout_seconds=5.0,
        auth=AuthMllp(),
        sample_endpoint=SampleEndpoint(hl7v2=sample),
        required_fields=["MSH-9", "PID-3"],
    )


# ---------------------------------------------------------------------------
# _sanitize_field 工具
# ---------------------------------------------------------------------------


def test_sanitize_field_replaces_field_and_segment_separators():
    """字段分隔符 | 与段分隔符 \\r \\n 替换为下划线；组件分隔符 ^ 保留。"""

    # 字段分隔符 | 替换为 _
    assert _sanitize_field("a|b") == "a_b"
    assert _sanitize_field("a||b") == "a__b"
    # 段分隔符 \r \n 替换为 _
    assert _sanitize_field("a\rb") == "a_b"
    assert _sanitize_field("a\nb") == "a_b"
    # 组件分隔符 ^ 保留（HL7 字段内合法结构，如 LAB001^CBC^L）
    assert _sanitize_field("a^b") == "a^b"
    assert _sanitize_field("LAB001^CBC^L") == "LAB001^CBC^L"
    # 子组件分隔符 & 与重复分隔符 ~ 保留
    assert _sanitize_field("a&b") == "a&b"
    assert _sanitize_field("a~b") == "a~b"


def test_sanitize_field_truncates_to_max_len():
    """超过 max_len 时截断。"""

    long = "x" * 100
    assert len(_sanitize_field(long, max_len=10)) == 10


def test_sanitize_field_handles_none_and_empty():
    """None 和空字符串处理。"""

    assert _sanitize_field(None) == ""
    assert _sanitize_field("") == ""


def test_sanitize_field_preserves_safe_chars():
    """安全字符（字母数字 / 横线 / 点）保持不变。"""

    assert _sanitize_field("patient-123.abc") == "patient-123.abc"


# ---------------------------------------------------------------------------
# ADT^A01
# ---------------------------------------------------------------------------


def test_build_adt_a01_segments():
    """ADT^A01 段结构：MSH / EVN / PID / PV1（4 段，末尾 \r）。"""

    interface = _make_interface()
    sample = interface.sample_endpoint.hl7v2
    msg = build_adt_a01(interface, sample)

    assert msg.endswith("\r")
    segs = [s for s in msg.rstrip("\r").split("\r") if s]
    assert len(segs) == 4
    assert segs[0].startswith("MSH|")
    assert segs[1].startswith("EVN|")
    assert segs[2].startswith("PID|")
    assert segs[3].startswith("PV1|")


def test_build_adt_a01_msh9_and_pid3():
    """ADT^A01 关键字段：MSH-9=ADT^A01 + PID-3=patient_id。"""

    interface = _make_interface()
    sample = interface.sample_endpoint.hl7v2
    msg = build_adt_a01(interface, sample)

    parsed = parse_message(msg, validation_level=2)
    assert parsed.msh.msh_9.to_er7() == "ADT^A01"
    assert parsed.pid.pid_3.to_er7() == "<EXAMPLE_PATIENT_ID>"


def test_build_adt_a01_custom_patient():
    """ADT^A01 自定义患者参数覆盖默认值。"""

    interface = _make_interface()
    sample = interface.sample_endpoint.hl7v2
    patient = ADTPatient(
        patient_id="P12345",
        patient_name="DOE^JOHN",
        attending_doctor_id="DR001",
        attending_doctor_name="SMITH^JANE",
        clinic="CARD-101",
        visit_number="V9876",
    )
    msg = build_adt_a01(interface, sample, patient=patient)

    parsed = parse_message(msg, validation_level=2)
    assert parsed.pid.pid_3.to_er7() == "P12345"
    # PV1-7 是 attending doctor（dr_id^name）
    assert parsed.pv1.pv1_7.to_er7() == "DR001^SMITH^JANE"


def test_build_adt_a01_msh10_msg_control_id():
    """ADT^A01 MSH-10 = Message Control ID 含 FQN 前缀。"""

    interface = _make_interface()
    sample = interface.sample_endpoint.hl7v2
    msg = build_adt_a01(interface, sample)

    parsed = parse_message(msg, validation_level=2)
    # MSH-10 是 message control id（VID-his.outpatient-register）
    assert "VID-his" in parsed.msh.msh_10.to_er7()
    assert "outpatient-register" in parsed.msh.msh_10.to_er7()


def test_build_adt_a01_no_credential_leak():
    """ADT^A01 不含任何凭据字面量（HL7 无凭据，仍验证无明文）。"""

    interface = _make_interface()
    sample = interface.sample_endpoint.hl7v2
    msg = build_adt_a01(interface, sample)

    lower = msg.lower()
    for forbidden in ["password", "token", "authorization", "bearer", "api_key"]:
        assert forbidden not in lower, f"凭据字段 {forbidden} 泄漏到 ADT^A01"


# ---------------------------------------------------------------------------
# ORM^O01
# ---------------------------------------------------------------------------


def test_build_orm_o01_segments():
    """ORM^O01 段结构：MSH / PID / ORC / OBR（4 段）。"""

    interface = _make_interface()
    sample = Hl7v2SampleEndpoint(message_type="ORM^O01", trigger_event="O01")
    interface_sample = SampleEndpoint(hl7v2=sample)
    interface = VendorInterface.model_construct(  # type: ignore[call-arg]
        **{
            **interface.model_dump(),
            "sample_endpoint": interface_sample,
        }
    )
    msg = build_orm_o01(interface, sample)

    assert msg.endswith("\r")
    segs = [s for s in msg.rstrip("\r").split("\r") if s]
    assert len(segs) == 4
    assert segs[0].startswith("MSH|")
    assert segs[1].startswith("PID|")
    assert segs[2].startswith("ORC|")
    assert segs[3].startswith("OBR|")


def test_build_orm_o01_message_type_in_msh9():
    """ORM^O01 MSH-9 = ORM^O01。"""

    interface = _make_interface()
    sample = Hl7v2SampleEndpoint(message_type="ORM^O01", trigger_event="O01")
    interface_sample = SampleEndpoint(hl7v2=sample)
    interface = VendorInterface.model_construct(  # type: ignore[call-arg]
        **{
            **interface.model_dump(),
            "sample_endpoint": interface_sample,
        }
    )
    msg = build_orm_o01(interface, sample)

    # 第 9 字段在 MSH 段形如 "MSH|enc|...|20261010||ORM^O01|VID-his|..."
    msh_line = msg.split("\r", 1)[0]
    fields = msh_line.split("|")
    # MSH-9 是第 9 个字段（索引 8，0 是 MSH 段名）
    assert fields[8] == "ORM^O01"


def test_build_orm_o01_orc2_obr2_obr4_fields():
    """ORM^O01 关键字段：ORC-2 / OBR-2 / OBR-4 = placer_order_number + service_id。"""

    interface = _make_interface()
    sample = Hl7v2SampleEndpoint(message_type="ORM^O01", trigger_event="O01")
    interface_sample = SampleEndpoint(hl7v2=sample)
    interface = VendorInterface.model_construct(  # type: ignore[call-arg]
        **{
            **interface.model_dump(),
            "sample_endpoint": interface_sample,
        }
    )
    order = ORMOrder(
        patient_id="P99887",
        placer_order_number="ORD-12345",
        universal_service_id="LAB001^CBC^L",
        order_effective_time="20261015",
        ordering_provider_id="DR999",
    )
    msg = build_orm_o01(interface, sample, order=order)

    # ORC 行包含 ORD-12345（ORC-2）
    assert "ORD-12345" in msg
    # OBR-4 = service id
    assert "LAB001^CBC^L" in msg
    # PID-3
    assert "P99887" in msg
    # 字段分隔符没被污染
    assert "ORD-12345||LAB001" in msg


def test_build_orm_o01_default_patient_uses_sample():
    """ORM^O01 未提供 order 时，patient_id 默认走 sample.sample_patient_id。"""

    interface = _make_interface()
    sample = Hl7v2SampleEndpoint(
        message_type="ORM^O01",
        trigger_event="O01",
        sample_patient_id="<SAMPLE_DEFAULT_PATIENT>",
    )
    interface_sample = SampleEndpoint(hl7v2=sample)
    interface = VendorInterface.model_construct(  # type: ignore[call-arg]
        **{
            **interface.model_dump(),
            "sample_endpoint": interface_sample,
        }
    )
    msg = build_orm_o01(interface, sample)

    assert "<SAMPLE_DEFAULT_PATIENT>" in msg


# ---------------------------------------------------------------------------
# ORU^R01
# ---------------------------------------------------------------------------


def test_build_oru_r01_segments():
    """ORU^R01 段结构：MSH / PID / OBR / OBX（4 段）。"""

    interface = _make_interface()
    sample = Hl7v2SampleEndpoint(message_type="ORU^R01", trigger_event="R01")
    interface_sample = SampleEndpoint(hl7v2=sample)
    interface = VendorInterface.model_construct(  # type: ignore[call-arg]
        **{
            **interface.model_dump(),
            "sample_endpoint": interface_sample,
        }
    )
    msg = build_oru_r01(interface, sample)

    assert msg.endswith("\r")
    segs = [s for s in msg.rstrip("\r").split("\r") if s]
    assert len(segs) == 4
    assert segs[0].startswith("MSH|")
    assert segs[1].startswith("PID|")
    assert segs[2].startswith("OBR|")
    assert segs[3].startswith("OBX|")


def test_build_oru_r01_message_type_in_msh():
    """ORU^R01 MSH-9 = ORU^R01。"""

    interface = _make_interface()
    sample = Hl7v2SampleEndpoint(message_type="ORU^R01", trigger_event="R01")
    interface_sample = SampleEndpoint(hl7v2=sample)
    interface = VendorInterface.model_construct(  # type: ignore[call-arg]
        **{
            **interface.model_dump(),
            "sample_endpoint": interface_sample,
        }
    )
    msg = build_oru_r01(interface, sample)

    msh_line = msg.split("\r", 1)[0]
    fields = msh_line.split("|")
    assert fields[8] == "ORU^R01"


def test_build_oru_r01_obr4_obx3_obx5_fields():
    """ORU^R01 关键字段：OBR-4 service_id + OBX-3 obs_id + OBX-5 obs_value。"""

    interface = _make_interface()
    sample = Hl7v2SampleEndpoint(message_type="ORU^R01", trigger_event="R01")
    interface_sample = SampleEndpoint(hl7v2=sample)
    interface = VendorInterface.model_construct(  # type: ignore[call-arg]
        **{
            **interface.model_dump(),
            "sample_endpoint": interface_sample,
        }
    )
    result = ORUResult(
        patient_id="P55555",
        placer_order_number="ORD-98765",
        filler_order_number="ACC-11111",
        universal_service_id="LAB001^CBC^L",
        observation_identifier="GLU^Glucose^L",
        observation_value="6.7",
        observation_units="mmol/L",
        observation_result_status="F",
        observation_time="20261020",
    )
    msg = build_oru_r01(interface, sample, result=result)

    # OBR-4 = service id
    assert "LAB001^CBC^L" in msg
    # OBX-3 = observation identifier
    assert "GLU^Glucose^L" in msg
    # OBX-5 = observation value
    assert "6.7" in msg
    # OBX-6 = units
    assert "mmol/L" in msg
    # PID-3
    assert "P55555" in msg
    # OBR-2 = placer order number
    assert "ORD-98765" in msg
    # OBR-3 = filler order number
    assert "ACC-11111" in msg
    # OBX-11 = result status (F = Final)
    assert "OBX|1|NM|GLU" in msg
    assert "F|||" in msg


def test_build_oru_r01_obx_value_type_numeric():
    """ORU^R01 OBX-2 value type = NM (Numeric)。"""

    interface = _make_interface()
    sample = Hl7v2SampleEndpoint(message_type="ORU^R01", trigger_event="R01")
    interface_sample = SampleEndpoint(hl7v2=sample)
    interface = VendorInterface.model_construct(  # type: ignore[call-arg]
        **{
            **interface.model_dump(),
            "sample_endpoint": interface_sample,
        }
    )
    msg = build_oru_r01(interface, sample)

    obx_line = msg.split("OBX|")[1].split("\r", 1)[0]
    obx_fields = obx_line.split("|")
    # OBX-2 = value type
    assert obx_fields[1] == "NM"


# ---------------------------------------------------------------------------
# SIU^S12
# ---------------------------------------------------------------------------


def test_build_siu_s12_segments():
    """SIU^S12 段结构：MSH / SCH（2 段，最小可用形态）。"""

    interface = _make_interface()
    sample = Hl7v2SampleEndpoint(message_type="SIU^S12", trigger_event="S12")
    interface_sample = SampleEndpoint(hl7v2=sample)
    interface = VendorInterface.model_construct(  # type: ignore[call-arg]
        **{
            **interface.model_dump(),
            "sample_endpoint": interface_sample,
        }
    )
    msg = build_siu_s12(interface, sample)

    assert msg.endswith("\r")
    segs = [s for s in msg.rstrip("\r").split("\r") if s]
    assert len(segs) == 2
    assert segs[0].startswith("MSH|")
    assert segs[1].startswith("SCH|")


def test_build_siu_s12_message_type_in_msh():
    """SIU^S12 MSH-9 = SIU^S12。"""

    interface = _make_interface()
    sample = Hl7v2SampleEndpoint(message_type="SIU^S12", trigger_event="S12")
    interface_sample = SampleEndpoint(hl7v2=sample)
    interface = VendorInterface.model_construct(  # type: ignore[call-arg]
        **{
            **interface.model_dump(),
            "sample_endpoint": interface_sample,
        }
    )
    msg = build_siu_s12(interface, sample)

    msh_line = msg.split("\r", 1)[0]
    fields = msh_line.split("|")
    assert fields[8] == "SIU^S12"


def test_build_siu_s12_sch_fields():
    """SIU^S12 关键字段：SCH-1 appointment_id + SCH-7 duration + SCH-8 location。"""

    interface = _make_interface()
    sample = Hl7v2SampleEndpoint(message_type="SIU^S12", trigger_event="S12")
    interface_sample = SampleEndpoint(hl7v2=sample)
    interface = VendorInterface.model_construct(  # type: ignore[call-arg]
        **{
            **interface.model_dump(),
            "sample_endpoint": interface_sample,
        }
    )
    schedule = SIUSchedule(
        placer_appointment_id="APPT-789",
        appointment_duration="45",
        appointment_duration_units="MIN",
        appointment_location="CLINIC-CARD",
        appointment_resource="DR-SMITH",
    )
    msg = build_siu_s12(interface, sample, schedule=schedule)

    # SCH-1
    assert "SCH|1|APPT-789" in msg
    # SCH-7 = duration^units
    assert "45^MIN" in msg
    # SCH-8 = location
    assert "CLINIC-CARD" in msg
    # SCH-9 = resource
    assert "DR-SMITH" in msg


# ---------------------------------------------------------------------------
# build_message_by_type 路由
# ---------------------------------------------------------------------------


def test_build_message_by_type_adt():
    """build_message_by_type 路由 ADT^A01 到 build_adt_a01。"""

    interface = _make_interface()
    sample = Hl7v2SampleEndpoint(message_type="ADT^A01", trigger_event="A01")
    interface_sample = SampleEndpoint(hl7v2=sample)
    interface = VendorInterface.model_construct(  # type: ignore[call-arg]
        **{
            **interface.model_dump(),
            "sample_endpoint": interface_sample,
        }
    )
    msg = build_message_by_type(interface, sample)

    assert "EVN|" in msg
    assert "PV1|" in msg
    assert "ADT^A01" in msg


def test_build_message_by_type_orm():
    """build_message_by_type 路由 ORM^O01 到 build_orm_o01。"""

    interface = _make_interface()
    sample = Hl7v2SampleEndpoint(message_type="ORM^O01", trigger_event="O01")
    interface_sample = SampleEndpoint(hl7v2=sample)
    interface = VendorInterface.model_construct(  # type: ignore[call-arg]
        **{
            **interface.model_dump(),
            "sample_endpoint": interface_sample,
        }
    )
    msg = build_message_by_type(interface, sample)

    assert "ORC|" in msg
    assert "OBR|" in msg
    assert "EVN|" not in msg  # ADT 才有的 EVN


def test_build_message_by_type_oru():
    """build_message_by_type 路由 ORU^R01 到 build_oru_r01。"""

    interface = _make_interface()
    sample = Hl7v2SampleEndpoint(message_type="ORU^R01", trigger_event="R01")
    interface_sample = SampleEndpoint(hl7v2=sample)
    interface = VendorInterface.model_construct(  # type: ignore[call-arg]
        **{
            **interface.model_dump(),
            "sample_endpoint": interface_sample,
        }
    )
    msg = build_message_by_type(interface, sample)

    assert "OBX|" in msg
    assert "ORC|" not in msg  # ORM 才有的 ORC


def test_build_message_by_type_siu():
    """build_message_by_type 路由 SIU^S12 到 build_siu_s12。"""

    interface = _make_interface()
    sample = Hl7v2SampleEndpoint(message_type="SIU^S12", trigger_event="S12")
    interface_sample = SampleEndpoint(hl7v2=sample)
    interface = VendorInterface.model_construct(  # type: ignore[call-arg]
        **{
            **interface.model_dump(),
            "sample_endpoint": interface_sample,
        }
    )
    msg = build_message_by_type(interface, sample)

    assert "SCH|" in msg
    assert "EVN|" not in msg
    assert "OBX|" not in msg


def test_build_message_by_type_unknown_falls_back_to_adt():
    """未知 message_type 回退到 ADT^A01（保持 task 7 既有行为）。"""

    interface = _make_interface()
    sample = Hl7v2SampleEndpoint(message_type="UNKNOWN^X99", trigger_event="X99")
    interface_sample = SampleEndpoint(hl7v2=sample)
    interface = VendorInterface.model_construct(  # type: ignore[call-arg]
        **{
            **interface.model_dump(),
            "sample_endpoint": interface_sample,
        }
    )
    msg = build_message_by_type(interface, sample)

    # 走 ADT 模板：4 段
    segs = [s for s in msg.rstrip("\r").split("\r") if s]
    assert len(segs) == 4
    assert segs[1].startswith("EVN|")  # EVN 是 ADT 特征段


def test_list_supported_message_types():
    """list_supported_message_types 返回 4 类。"""

    supported = list_supported_message_types()
    assert supported == ["ADT^A01", "ORM^O01", "ORU^R01", "SIU^S12"]


# ---------------------------------------------------------------------------
# 公共属性：MSH 段 / 编码字符 / 段分隔符
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message_type,trigger_event,expected_segments",
    [
        ("ADT^A01", "A01", ["MSH", "EVN", "PID", "PV1"]),
        ("ORM^O01", "O01", ["MSH", "PID", "ORC", "OBR"]),
        ("ORU^R01", "R01", ["MSH", "PID", "OBR", "OBX"]),
        ("SIU^S12", "S12", ["MSH", "SCH"]),
    ],
)
def test_all_templates_segment_order(message_type, trigger_event, expected_segments):
    """4 类消息段顺序正确。"""

    interface = _make_interface()
    sample = Hl7v2SampleEndpoint(message_type=message_type, trigger_event=trigger_event)
    interface_sample = SampleEndpoint(hl7v2=sample)
    interface = VendorInterface.model_construct(  # type: ignore[call-arg]
        **{
            **interface.model_dump(),
            "sample_endpoint": interface_sample,
        }
    )
    msg = build_message_by_type(interface, sample)

    segs = [s.split("|", 1)[0] for s in msg.rstrip("\r").split("\r") if s]
    assert segs == expected_segments


@pytest.mark.parametrize(
    "message_type,trigger_event",
    [
        ("ADT^A01", "A01"),
        ("ORM^O01", "O01"),
        ("ORU^R01", "R01"),
        ("SIU^S12", "S12"),
    ],
)
def test_all_templates_msh_encoding_chars(message_type, trigger_event):
    """4 类消息 MSH 段第 2 字段 = 编码字符 ^~\\&。"""

    interface = _make_interface()
    sample = Hl7v2SampleEndpoint(message_type=message_type, trigger_event=trigger_event)
    interface_sample = SampleEndpoint(hl7v2=sample)
    interface = VendorInterface.model_construct(  # type: ignore[call-arg]
        **{
            **interface.model_dump(),
            "sample_endpoint": interface_sample,
        }
    )
    msg = build_message_by_type(interface, sample)

    msh_line = msg.split("\r", 1)[0]
    fields = msh_line.split("|")
    # MSH-2 = 编码字符
    assert fields[1] == "^~\\&"


@pytest.mark.parametrize(
    "message_type,trigger_event",
    [
        ("ADT^A01", "A01"),
        ("ORM^O01", "O01"),
        ("ORU^R01", "R01"),
        ("SIU^S12", "S12"),
    ],
)
def test_all_templates_end_with_cr(message_type, trigger_event):
    """4 类消息都以 \\r 结尾（HL7 标准）。"""

    interface = _make_interface()
    sample = Hl7v2SampleEndpoint(message_type=message_type, trigger_event=trigger_event)
    interface_sample = SampleEndpoint(hl7v2=sample)
    interface = VendorInterface.model_construct(  # type: ignore[call-arg]
        **{
            **interface.model_dump(),
            "sample_endpoint": interface_sample,
        }
    )
    msg = build_message_by_type(interface, sample)

    assert msg.endswith("\r")
    # 段间用 \r 分隔，不是 \n
    assert "\n" not in msg


@pytest.mark.parametrize(
    "message_type,trigger_event",
    [
        ("ADT^A01", "A01"),
        ("ORM^O01", "O01"),
        ("ORU^R01", "R01"),
        ("SIU^S12", "S12"),
    ],
)
def test_all_templates_no_credential_leak(message_type, trigger_event):
    """4 类消息都不含任何凭据字面量。"""

    interface = _make_interface()
    sample = Hl7v2SampleEndpoint(message_type=message_type, trigger_event=trigger_event)
    interface_sample = SampleEndpoint(hl7v2=sample)
    interface = VendorInterface.model_construct(  # type: ignore[call-arg]
        **{
            **interface.model_dump(),
            "sample_endpoint": interface_sample,
        }
    )
    msg = build_message_by_type(interface, sample)

    lower = msg.lower()
    for forbidden in ["password", "bearer ", "authorization:", "api_key="]:
        assert forbidden not in lower, f"凭据字段 {forbidden!r} 泄漏到 {message_type} 模板"


# ---------------------------------------------------------------------------
# 字段分隔符不会被污染
# ---------------------------------------------------------------------------


def test_field_sanitization_prevents_segment_injection():
    """字段值含 | 字符时，sanitize 把 | 替换为 _，避免段结构污染。"""

    interface = _make_interface()
    sample = interface.sample_endpoint.hl7v2
    patient = ADTPatient(patient_id="P123|FAKE_SEG|MSH")
    msg = build_adt_a01(interface, sample, patient=patient)

    # | 全部替换为 _
    assert "P123_FAKE_SEG_MSH" in msg
    # 不能伪造出额外的 MSH 段（仍然只有 1 段 MSH）
    segments = [s for s in msg.rstrip("\r").split("\r") if s]
    msh_segments = [s for s in segments if s.startswith("MSH")]
    assert len(msh_segments) == 1


def test_field_sanitization_preserves_component_separators():
    """组件分隔符 ^ 保留（HL7 字段内合法结构）。"""

    interface = _make_interface()
    sample = interface.sample_endpoint.hl7v2
    patient = ADTPatient(
        patient_id="P123",
        attending_doctor_id="DR001",
        attending_doctor_name="SMITH^JANE^DR",  # 多组件的医生姓名
    )
    msg = build_adt_a01(interface, sample, patient=patient)

    # PV1-7 保留 ^ 分隔的多组件
    assert "DR001^SMITH^JANE^DR" in msg


def test_field_sanitization_strips_control_chars():
    """字段值含 \\r \\n \\t 时，sanitize 全部替换为 _。"""

    interface = _make_interface()
    sample = interface.sample_endpoint.hl7v2
    patient = ADTPatient(patient_id="P123\rMSH|FAKE")
    msg = build_adt_a01(interface, sample, patient=patient)

    # \r 被替换成 _，不应有第二段 MSH
    segments = [s for s in msg.rstrip("\r").split("\r") if s]
    msh_segments = [s for s in segments if s.startswith("MSH")]
    assert len(msh_segments) == 1


# ---------------------------------------------------------------------------
# 与 hl7v2.py 集成
# ---------------------------------------------------------------------------


def test_hl7v2_uses_templates_for_adt():
    """hl7v2._build_message 在 message_type=ADT^A01 时调用 build_adt_a01。"""

    from vendor_if_doctor.adapters.hl7v2 import _build_message

    interface = _make_interface()
    sample = interface.sample_endpoint.hl7v2
    msg = _build_message(interface, sample)

    segs = [s for s in msg.rstrip("\r").split("\r") if s]
    assert len(segs) == 4
    assert segs[1].startswith("EVN|")  # ADT 特征段


def test_hl7v2_uses_templates_for_orm():
    """hl7v2._build_message 在 message_type=ORM^O01 时调用 build_orm_o01。"""

    from vendor_if_doctor.adapters.hl7v2 import _build_message

    interface = _make_interface()
    sample = Hl7v2SampleEndpoint(message_type="ORM^O01", trigger_event="O01")
    interface_sample = SampleEndpoint(hl7v2=sample)
    interface = VendorInterface.model_construct(  # type: ignore[call-arg]
        **{
            **interface.model_dump(),
            "sample_endpoint": interface_sample,
        }
    )
    msg = _build_message(interface, sample)

    segs = [s for s in msg.rstrip("\r").split("\r") if s]
    assert len(segs) == 4
    assert segs[2].startswith("ORC|")  # ORM 特征段（ORC 在第 3 段）


def test_hl7v2_uses_templates_for_oru():
    """hl7v2._build_message 在 message_type=ORU^R01 时调用 build_oru_r01。"""

    from vendor_if_doctor.adapters.hl7v2 import _build_message

    interface = _make_interface()
    sample = Hl7v2SampleEndpoint(message_type="ORU^R01", trigger_event="R01")
    interface_sample = SampleEndpoint(hl7v2=sample)
    interface = VendorInterface.model_construct(  # type: ignore[call-arg]
        **{
            **interface.model_dump(),
            "sample_endpoint": interface_sample,
        }
    )
    msg = _build_message(interface, sample)

    segs = [s for s in msg.rstrip("\r").split("\r") if s]
    assert len(segs) == 4
    assert segs[3].startswith("OBX|")  # ORU 特征段（OBX 在第 4 段）


def test_hl7v2_uses_templates_for_siu():
    """hl7v2._build_message 在 message_type=SIU^S12 时调用 build_siu_s12。"""

    from vendor_if_doctor.adapters.hl7v2 import _build_message

    interface = _make_interface()
    sample = Hl7v2SampleEndpoint(message_type="SIU^S12", trigger_event="S12")
    interface_sample = SampleEndpoint(hl7v2=sample)
    interface = VendorInterface.model_construct(  # type: ignore[call-arg]
        **{
            **interface.model_dump(),
            "sample_endpoint": interface_sample,
        }
    )
    msg = _build_message(interface, sample)

    segs = [s for s in msg.rstrip("\r").split("\r") if s]
    assert len(segs) == 2
    assert segs[1].startswith("SCH|")  # SIU 特征段（SCH 在第 2 段）
