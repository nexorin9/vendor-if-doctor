"""HL7v2 消息模板库（task 16）。

按 HL7v2 消息类型 / trigger event 预生成巡检用的样例消息，4 类：

* **ADT^A01**（Admit / Discharge / Transfer）— :func:`build_adt_a01`
* **ORM^O01**（Order Message）— :func:`build_orm_o01`
* **ORU^R01**（Observation Result）— :func:`build_oru_r01`
* **SIU^S12**（Scheduling Information）— :func:`build_siu_s12`

设计要点
========

* **字符串拼接而非 :class:`hl7apy.Message`**：hl7apy v2.5/v2.7 的 ``Message`` 构造器仅支持
  ``parse_message`` 入口，且 ``validation_level=2`` 严格校验对 ``ORU^R01`` 的 ``OBR/OBX``
  嵌套结构不友好（OBR 必须包在 PATIENT_OBSERVATION → ORDER_OBSERVATION 群组中，访问
  ``msg.obr.obr_4`` 会抛 :class:`ChildNotFound`）。直接 ER7 字符串拼接最稳定。
* **占位符与脱敏纪律**：所有患者 ID / 医生 ID / 就诊号 / 订单号等敏感字段统一用
  ``<EXAMPLE_*>`` 占位（**不**写真实编号）；信息科巡检目的而非生产演练。
* **可解析性**：4 类消息经 :func:`hl7apy.parser.parse_message` ``validation_level=2``
  解析时，至少 ``MSH-9`` / ``MSH-10`` 字段能被识别（ADT^A01 已验证可解析整段）。
* **段分隔符**：HL7v2 用 ``\\r``（CR）作为段分隔，消息末尾保留一个 ``\\r``。

字段路径（与 :class:`vendor_if_doctor.spec.schema.Hl7v2SampleEndpoint` 的
``required_fields`` 路径一致）：

* ``MSH-9`` — 消息类型 / trigger event（如 ``ADT^A01``）
* ``MSH-10`` — Message Control ID
* ``PID-3`` — Patient ID
* ``PV1-7`` — Attending Doctor（ADT^A01）
* ``ORC-2`` — Placer Order Number（ORM^O01）
* ``OBR-2`` — Placer Order Number（ORM^O01 / ORU^R01）
* ``OBR-4`` — Universal Service Identifier（ORM^O01 / ORU^R01）
* ``OBX-3`` — Observation Identifier（ORU^R01）
* ``OBX-5`` — Observation Value（ORU^R01）
* ``SCH-1`` — Placer Appointment ID（SIU^S12）
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import List, Optional

from vendor_if_doctor.spec.schema import Hl7v2SampleEndpoint, VendorInterface


# HL7v2 默认编码字符：字段分隔符 |、组件分隔符 ^、子组件分隔符 &、重复分隔符 ~、转义符 \\
_DEFAULT_ENCODING_CHARS = "^~\\&"

# HL7v2 默认版本
_DEFAULT_VERSION = "2.5"


# ---------------------------------------------------------------------------
# 数据类（typed parameters）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ADTPatient:
    """ADT^A01（Admit / Discharge / Transfer）样例患者参数。"""

    patient_id: str = "<EXAMPLE_PATIENT_ID>"
    patient_name: str = "<EXAMPLE^Patient>"
    visit_number: str = "<EXAMPLE_VISIT_NUMBER>"
    attending_doctor_id: str = "<EXAMPLE_DOCTOR_ID"
    attending_doctor_name: str = "Vendor^Doctor"
    clinic: str = "<EXAMPLE_CLINIC>"


@dataclass(frozen=True)
class ORMOrder:
    """ORM^O01（Order Message）样例医嘱参数。"""

    patient_id: str = "<EXAMPLE_PATIENT_ID>"
    placer_order_number: str = "<EXAMPLE_ORDER_NUMBER>"
    universal_service_id: str = "<EXAMPLE_SERVICE_CODE^CBC^L"
    order_effective_time: str = ""  # 空 → 自动填当前 YYYYMMDDHHMMSS
    ordering_provider_id: str = "<EXAMPLE_DOCTOR_ID"


@dataclass(frozen=True)
class ORUResult:
    """ORU^R01（Observation Result）样例检验结果参数。"""

    patient_id: str = "<EXAMPLE_PATIENT_ID>"
    placer_order_number: str = "<EXAMPLE_ORDER_NUMBER>"
    filler_order_number: str = "<EXAMPLE_ACC_NUMBER>"
    universal_service_id: str = "<EXAMPLE_SERVICE_CODE^CBC^L"
    observation_identifier: str = "<EXAMPLE_OBS_CODE^Glucose^L"
    observation_value: str = "5.6"
    observation_units: str = "mmol/L"
    observation_result_status: str = "F"  # Final
    observation_time: str = ""  # 空 → 自动填


@dataclass(frozen=True)
class SIUSchedule:
    """SIU^S12（Scheduling Information）样例排班参数。"""

    placer_appointment_id: str = "<EXAMPLE_APPOINTMENT_ID>"
    appointment_duration: str = "30"  # 单位 MIN
    appointment_duration_units: str = "MIN"
    appointment_location: str = "<EXAMPLE_CLINIC>"
    appointment_resource: str = "<EXAMPLE_RESOURCE>"


# ---------------------------------------------------------------------------
# 字段消毒（防止 HL7 字段分隔符 / 控制字符污染消息结构）
# ---------------------------------------------------------------------------


# 字段分隔符 ``|`` 与段分隔符 ``\r`` ``\n`` 必须过滤（否则攻击者可伪造段结构），
# 但 **不** 过滤 ``^`` ``&`` ``~`` ``\\`` 这些**组件 / 子组件 / 重复 / 转义**
# 分隔符 — 它们是 HL7 字段内合法结构（如 ``OBR-4 = LAB001^CBC^L`` 的 component 1/2/3），
# 过滤会破坏结构化值。
_FIELD_SANITIZE_PATTERN = re.compile(r"[|\r\n]")


def _sanitize_field(value: str, max_len: int = 200) -> str:
    """去除字段分隔符 / 段分隔符并截断；保留组件 / 子组件 / 重复分隔符。"""

    if value is None:
        return ""
    sanitized = _FIELD_SANITIZE_PATTERN.sub("_", str(value))
    if len(sanitized) > max_len:
        sanitized = sanitized[:max_len]
    return sanitized


def _now_timestamp() -> str:
    """生成 HL7 标准时间戳 YYYYMMDDHHMMSS。"""

    return time.strftime("%Y%m%d%H%M%S", time.localtime())


def _date_timestamp() -> str:
    """生成 HL7 标准日期 YYYYMMDD。"""

    return time.strftime("%Y%m%d", time.localtime())


# ---------------------------------------------------------------------------
# MSH 段构造（公用）
# ---------------------------------------------------------------------------


def _build_msh_segment(
    interface: VendorInterface,
    message_type: str,
    trigger_event: str,
) -> str:
    """构造 MSH 段（Message Header，12 个字段）。

    MSH-9 = <message_type>^<trigger_event>（如 ADT^A01）
    MSH-10 = Message Control ID（<vendor_id>.<interface_id> 限长 40 字符）
    MSH-12 = HL7 version（默认 2.5）
    """

    timestamp = _now_timestamp()
    host_safe = _sanitize_field(interface.host or "REMOTE", max_len=40)
    msg_control_id = _sanitize_field(f"VID-{interface.fqn}", max_len=40)
    return (
        f"MSH|{_DEFAULT_ENCODING_CHARS}"
        f"|VENDOR-IF-DOCTOR|vendor-if-doctor"
        f"|{host_safe}|{host_safe}"
        f"|{timestamp}"
        f"||{message_type}^{trigger_event}"
        f"|{msg_control_id}"
        f"|P|{_DEFAULT_VERSION}"
    )


# ---------------------------------------------------------------------------
# 4 类消息模板
# ---------------------------------------------------------------------------


def build_adt_a01(
    interface: VendorInterface,
    sample: Hl7v2SampleEndpoint,
    patient: Optional[ADTPatient] = None,
) -> str:
    """构造 ADT^A01 消息（Admit / Discharge / Transfer 入院登记）。

    段：MSH / EVN / PID / PV1（4 段）。
    关键字段：MSH-9=ADT^A01 / PID-3=patient.patient_id / PV1-7=patient.attending_doctor_id。

    :param interface: vendor 接口规格（用于 MSH 段 sender / receiver）
    :param sample: HL7v2 sample endpoint（提供 patient_id 默认值）
    :param patient: 患者参数（None 时使用 sample 提供的默认值 + EXAMPLE_* 占位）
    :return: ER7 字符串（以 ``\\r`` 结尾）
    """

    if patient is None:
        patient = ADTPatient(patient_id=sample.sample_patient_id or "<EXAMPLE_PATIENT_ID>")

    pid3 = _sanitize_field(patient.patient_id, max_len=40)
    patient_name = _sanitize_field(patient.patient_name, max_len=48)
    doctor_id = _sanitize_field(patient.attending_doctor_id, max_len=40)
    doctor_name = _sanitize_field(patient.attending_doctor_name, max_len=48)
    clinic = _sanitize_field(patient.clinic, max_len=20)
    visit = _sanitize_field(patient.visit_number, max_len=40)

    timestamp = _now_timestamp()

    msh = _build_msh_segment(interface, "ADT", "A01")
    evn = f"EVN|A01|{timestamp}"
    pid = f"PID|||{pid3}||{patient_name}|||{visit}"
    pv1 = f"PV1|I|{clinic}|||||{doctor_id}^{doctor_name}"
    return "\r".join([msh, evn, pid, pv1]) + "\r"


def build_orm_o01(
    interface: VendorInterface,
    sample: Hl7v2SampleEndpoint,
    order: Optional[ORMOrder] = None,
) -> str:
    """构造 ORM^O01 消息（Order Message，下单类）。

    段：MSH / PID / ORC / OBR（4 段）。
    关键字段：MSH-9=ORM^O01 / PID-3=order.patient_id / ORC-2=order.placer_order_number /
    OBR-2=order.placer_order_number / OBR-4=order.universal_service_id。

    :param interface: vendor 接口规格
    :param sample: HL7v2 sample endpoint
    :param order: 医嘱参数（None 时使用占位 + sample 提供 patient_id）
    :return: ER7 字符串（以 ``\\r`` 结尾）
    """

    if order is None:
        order = ORMOrder(patient_id=sample.sample_patient_id or "<EXAMPLE_PATIENT_ID>")

    pid3 = _sanitize_field(order.patient_id, max_len=40)
    placer = _sanitize_field(order.placer_order_number, max_len=40)
    service_id = _sanitize_field(order.universal_service_id, max_len=60)
    effective = order.order_effective_time or _date_timestamp()
    effective = _sanitize_field(effective, max_len=14)
    provider = _sanitize_field(order.ordering_provider_id, max_len=40)

    msh = _build_msh_segment(interface, "ORM", "O01")
    pid = f"PID|||{pid3}"
    # ORC-1 = Order Control（NW = New Order）
    orc = f"ORC|NW|{placer}|||{provider}^Vendor^Doctor"
    # OBR-2 = Placer Order Number，OBR-4 = Universal Service ID，OBR-7 = Observation Date/Time
    obr = f"OBR|1|{placer}||{service_id}|||{effective}"
    return "\r".join([msh, pid, orc, obr]) + "\r"


def build_oru_r01(
    interface: VendorInterface,
    sample: Hl7v2SampleEndpoint,
    result: Optional[ORUResult] = None,
) -> str:
    """构造 ORU^R01 消息（Observation Result，观察结果回报）。

    段：MSH / PID / OBR / OBX（4 段）。
    关键字段：MSH-9=ORU^R01 / PID-3=result.patient_id / OBR-2=result.placer_order_number /
    OBR-4=result.universal_service_id / OBX-3=result.observation_identifier /
    OBX-5=result.observation_value。

    :param interface: vendor 接口规格
    :param sample: HL7v2 sample endpoint
    :param result: 检验结果参数（None 时使用占位 + sample 提供 patient_id）
    :return: ER7 字符串（以 ``\\r`` 结尾）
    """

    if result is None:
        result = ORUResult(patient_id=sample.sample_patient_id or "<EXAMPLE_PATIENT_ID>")

    pid3 = _sanitize_field(result.patient_id, max_len=40)
    placer = _sanitize_field(result.placer_order_number, max_len=40)
    filler = _sanitize_field(result.filler_order_number, max_len=40)
    service_id = _sanitize_field(result.universal_service_id, max_len=60)
    obs_id = _sanitize_field(result.observation_identifier, max_len=60)
    obs_value = _sanitize_field(result.observation_value, max_len=20)
    obs_units = _sanitize_field(result.observation_units, max_len=20)
    obs_status = _sanitize_field(result.observation_result_status, max_len=1) or "F"
    obs_time = result.observation_time or _date_timestamp()
    obs_time = _sanitize_field(obs_time, max_len=14)

    msh = _build_msh_segment(interface, "ORU", "R01")
    pid = f"PID|||{pid3}"
    # OBR-2 = Placer Order Number，OBR-3 = Filler Order Number，OBR-4 = Universal Service ID
    obr = f"OBR|1|{placer}|{filler}||{service_id}||||||{obs_time}"
    # OBX-2 = Value Type（NM = Numeric），OBX-3 = Observation Identifier，OBX-5 = Observation Value
    obx = f"OBX|1|NM|{obs_id}||{obs_value}|{obs_units}|||||{obs_status}|||{obs_time}"
    return "\r".join([msh, pid, obr, obx]) + "\r"


def build_siu_s12(
    interface: VendorInterface,
    sample: Hl7v2SampleEndpoint,
    schedule: Optional[SIUSchedule] = None,
) -> str:
    """构造 SIU^S12 消息（Scheduling Information，新增排班通知）。

    段：MSH / SCH（2 段；最小可用形态）。
    关键字段：MSH-9=SIU^S12 / SCH-1=schedule.placer_appointment_id /
    SCH-7=schedule.appointment_duration^schedule.appointment_duration_units /
    SCH-8=schedule.appointment_location / SCH-9=schedule.appointment_resource。

    :param interface: vendor 接口规格
    :param sample: HL7v2 sample endpoint
    :param schedule: 排班参数（None 时使用占位）
    :return: ER7 字符串（以 ``\\r`` 结尾）
    """

    if schedule is None:
        schedule = SIUSchedule()

    appointment_id = _sanitize_field(schedule.placer_appointment_id, max_len=20)
    duration = _sanitize_field(schedule.appointment_duration, max_len=10)
    duration_units = _sanitize_field(schedule.appointment_duration_units, max_len=10)
    location = _sanitize_field(schedule.appointment_location, max_len=20)
    resource = _sanitize_field(schedule.appointment_resource, max_len=20)

    msh = _build_msh_segment(interface, "SIU", "S12")
    # SCH-1 = Placer Appointment ID，SCH-7 = Appointment Duration^Duration Units，
    # SCH-8 = Appointment Location，SCH-9 = Appointment Resource
    sch = f"SCH|1|{appointment_id}|||||{duration}^{duration_units}|||{location}|{resource}"
    return "\r".join([msh, sch]) + "\r"


# ---------------------------------------------------------------------------
# 统一入口（按 message_type 路由到对应 builder）
# ---------------------------------------------------------------------------


# 已知 message_type → builder 的映射表
_MESSAGE_TYPE_BUILDERS = {
    "ADT": build_adt_a01,
    "ORM": build_orm_o01,
    "ORU": build_oru_r01,
    "SIU": build_siu_s12,
}


def build_message_by_type(
    interface: VendorInterface,
    sample: Hl7v2SampleEndpoint,
) -> str:
    """按 :attr:`Hl7v2SampleEndpoint.message_type` 选择对应模板。

    支持 ``ADT^A01`` / ``ORM^O01`` / ``ORU^R01`` / ``SIU^S12``（前缀匹配）四种；
    未知类型回退到 ADT^A01 模板（保持 task 7 既有兼容行为）。
    """

    msg_type = (sample.message_type or "ADT^A01").upper()
    prefix = msg_type.split("^", 1)[0]
    builder = _MESSAGE_TYPE_BUILDERS.get(prefix, build_adt_a01)
    return builder(interface, sample)


def list_supported_message_types() -> List[str]:
    """返回支持的 message_type 列表。"""

    return ["ADT^A01", "ORM^O01", "ORU^R01", "SIU^S12"]


__all__ = [
    "ADTPatient",
    "ORMOrder",
    "ORUResult",
    "SIUSchedule",
    "build_adt_a01",
    "build_message_by_type",
    "build_orm_o01",
    "build_oru_r01",
    "build_siu_s12",
    "list_supported_message_types",
]
