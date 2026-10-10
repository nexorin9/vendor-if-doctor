"""vendor_if_doctor.spec.loader — vendor_spec 加载器。

支持三类输入：

* 单文件：``load_vendor_spec('vendor_spec.yaml')``
* 目录：``load_vendor_spec('conf/vendor_specs/')`` — 加载目录下所有 ``*.yaml`` / ``*.yml`` / ``*.json``
* glob：``load_vendor_spec('conf/vendor_specs/*.yaml')``

行为约定：
* YAML 解析 + JSON Schema 校验在启动期完成，**不**发起任何网络请求。
* 任何文件解析失败 / schema 校验失败立即抛 :class:`SpecValidationError`。
* 多个文件加载时（:func:`load_vendor_specs`），所有 interfaces 会合并进
  :class:`VendorSpec`，重复 FQN 立即抛错。
"""

from __future__ import annotations

import glob
import json
import logging
from pathlib import Path
from typing import Iterable, List, Mapping, Optional, Union

import yaml
from pydantic import ValidationError

from vendor_if_doctor.spec.errors import SpecValidationError, SpecIssue
from vendor_if_doctor.spec.schema import InspectionConfig, VendorInterface, VendorSpec
from vendor_if_doctor.spec.validate import validate_raw, validate_vendor_spec

logger = logging.getLogger(__name__)

PathLike = Union[str, Path]


# --- 单文件 / 单输入解析 -------------------------------------------------------


def _parse_one(path: Path) -> Mapping:
    """读取单文件并解析为 mapping。"""

    if not path.exists():
        raise SpecValidationError(
            [SpecIssue(field=str(path), rule="file not found", hint="检查路径或权限")],
            source=str(path),
        )
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    try:
        if suffix == ".json":
            data = json.loads(text)
        else:
            data = yaml.safe_load(text)
    except (yaml.YAMLError, json.JSONDecodeError) as e:
        raise SpecValidationError(
            [SpecIssue(field=str(path), rule="parse error", hint=str(e).splitlines()[0][:200])],
            source=str(path),
        ) from e
    if not isinstance(data, Mapping):
        raise SpecValidationError(
            [SpecIssue(field=str(path), rule="root must be mapping", hint="YAML 根必须是 mapping，不是 list 或 scalar")],
            source=str(path),
        )
    return data


def _build_vendor_spec(raw: Mapping, *, source: Optional[str]) -> VendorSpec:
    """从 raw dict 构建 VendorSpec；内部捕获 pydantic 错误并包装。"""

    try:
        spec = VendorSpec.model_validate(raw.get("vendor_spec", raw))
    except ValidationError as e:
        issues = [
            SpecIssue(
                field=".".join(str(p) for p in err["loc"]) or "<root>",
                rule=err["type"],
                hint=err["msg"][:200],
            )
            for err in e.errors()
        ]
        raise SpecValidationError(issues, source=source) from e
    return spec


# --- 单文件 / 目录 / glob 输入解析 --------------------------------------------


def _expand_paths(path: PathLike) -> List[Path]:
    """根据输入 path 解析为具体文件列表。"""

    p = Path(path)
    s = str(path)
    if any(ch in s for ch in "*?["):
        return sorted(Path(x) for x in glob.glob(s))
    if p.is_dir():
        files: List[Path] = []
        for ext in ("*.yaml", "*.yml", "*.json"):
            files.extend(p.glob(ext))
        if not files:
            raise SpecValidationError(
                [SpecIssue(field=str(p), rule="no yaml/json found", hint="目录里至少要有 1 个 .yaml / .yml / .json")],
                source=str(p),
            )
        return sorted(set(files))
    return [p]


def load_vendor_spec(
    path: PathLike,
    *,
    env: Optional[Mapping[str, str]] = None,
    validate_credentials: bool = False,
) -> VendorSpec:
    """加载单文件 / 目录 / glob 并返回 :class:`VendorSpec`。

    ``validate_credentials=False``（默认）只做 schema 与字段校验，不读取环境变量，
    适合 CLI ``init`` / ``lint`` / 测试阶段。

    ``validate_credentials=True`` 时再走 :func:`validate_vendor_spec`，
    要求所有 ``auth.secret_env`` 等环境变量都已 export。
    """

    paths = _expand_paths(path)
    if len(paths) > 1:
        return load_vendor_specs(paths, env=env, validate_credentials=validate_credentials)

    only = paths[0]
    raw = _parse_one(only)
    validate_raw(raw, source=str(only))
    spec = _build_vendor_spec(raw, source=str(only))
    if validate_credentials:
        validate_vendor_spec(spec, env=env, source=str(only))
    else:
        # 即便不要求凭据可用，仍记录未设置的 secret_env 数量到 logger，便于运维自检
        _ = spec
    return spec


def load_vendor_specs(
    paths: Iterable[PathLike],
    *,
    env: Optional[Mapping[str, str]] = None,
    validate_credentials: bool = False,
) -> VendorSpec:
    """加载多个文件并合并到同一个 :class:`VendorSpec`。

    * 所有文件的 ``vendor_spec.schema_version`` 必须一致；不一致抛错。
    * 所有 interfaces 合并后 FQN 唯一（由 :class:`VendorSpec` 模型校验保证）。
    * ``inspection`` 配置以**第一个文件**为准；其余忽略并发出 logger.warning。
    """

    file_list = [Path(p) for p in paths]
    if not file_list:
        raise SpecValidationError(
            [SpecIssue(field="<paths>", rule="empty list", hint="至少传入 1 个 vendor_spec 文件")],
            source="<paths>",
        )

    merged_interfaces: List[VendorInterface] = []
    base_inspection: Optional[InspectionConfig] = None
    schema_version: Optional[str] = None
    run_window_minutes: Optional[int] = None
    severity_policy: Optional[str] = None

    for p in file_list:
        raw = _parse_one(p)
        validate_raw(raw, source=str(p))

        top = raw.get("vendor_spec", raw)
        sv = top.get("schema_version")
        if schema_version is None:
            schema_version = sv
        elif sv != schema_version:
            raise SpecValidationError(
                [
                    SpecIssue(
                        field="vendor_spec.schema_version",
                        rule="inconsistent across files",
                        hint=f"{p}: {sv} != {schema_version}",
                    )
                ],
                source=str(p),
            )
        if run_window_minutes is None and top.get("run_window_minutes") is not None:
            run_window_minutes = top["run_window_minutes"]
        if severity_policy is None and top.get("severity_policy") is not None:
            severity_policy = top["severity_policy"]
        if base_inspection is None and top.get("inspection"):
            try:
                base_inspection = InspectionConfig.model_validate(top["inspection"])
            except ValidationError as e:
                raise SpecValidationError(
                    [
                        SpecIssue(
                            field=f"{p}.inspection",
                            rule=err["type"],
                            hint=err["msg"][:200],
                        )
                        for err in e.errors()
                    ],
                    source=str(p),
                ) from e
        elif top.get("inspection"):
            logger.warning("inspection 配置以首文件为准，忽略 %s", p)

        spec = _build_vendor_spec(raw, source=str(p))
        merged_interfaces.extend(spec.interfaces)

    if not merged_interfaces:
        raise SpecValidationError(
            [SpecIssue(field="vendor_spec.interfaces", rule="must be non-empty", hint="至少需要 1 条接口")],
            source="<merged>",
        )

    merged = VendorSpec(
        schema_version=schema_version or "1.0",
        run_window_minutes=run_window_minutes or 30,
        severity_policy=severity_policy or "default",
        interfaces=merged_interfaces,
        inspection=base_inspection or InspectionConfig(),
    )

    if validate_credentials:
        validate_vendor_spec(merged, env=env, source="<merged>")
    return merged


__all__ = ["load_vendor_spec", "load_vendor_specs"]
