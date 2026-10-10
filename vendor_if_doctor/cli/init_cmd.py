"""init 子命令 — 生成 vendor_spec.yaml 模板到当前目录或 ``--output`` 指定路径。

模板字段覆盖：4 类接口（HIS HTTP basic / LIS HTTP bearer / PACS HTTP apikey /
EMR HL7v2），全部以 ``${VENDOR_*_HOST}`` 形式引用环境变量，值班员复制后再按
信息科实际凭据替换。
"""

from __future__ import annotations

from pathlib import Path

import typer

_TEMPLATE = """# vendor-if-doctor 模板 — 由 `python -m vendor_if_doctor init` 生成
# 字段对齐：vendor_spec.schema.json（启动期校验），所有 *_HOST / *_TOKEN 必须从 env 读取
# 凭据纪律：vendor_spec.yaml 永不含明文 token / password；只允许写 "环境变量名"

vendor_spec:
  schema_version: "1.0"
  run_window_minutes: 30
  severity_policy: "default"

  interfaces:
    # 1) HIS 门诊挂号 — HTTP + Basic Auth
    - vendor_id: his
      interface_id: outpatient-register
      display_name: "HIS 门诊挂号接口"
      protocol: http
      host: ${VENDOR_HIS_HOST}
      port: 80
      base_path: /api/v1
      timeout_seconds: 5
      auth:
        type: basic
        username_env: VENDOR_HIS_USERNAME
        secret_env: VENDOR_HIS_PASSWORD
      sample_endpoint:
        http:
          method: GET
          path: /register/slot
          headers: { Accept: application/json }
          body: ""
      required_fields:
        - "$.code"
        - "$.patient_id"

    # 2) LIS 检验报告 — HTTP + Bearer Auth
    - vendor_id: lis
      interface_id: report-push
      display_name: "LIS 检验报告接口"
      protocol: http
      host: ${VENDOR_LIS_HOST}
      port: 8443
      base_path: /v2
      timeout_seconds: 5
      auth:
        type: bearer
        secret_env: VENDOR_LIS_TOKEN
      sample_endpoint:
        http:
          method: GET
          path: /reports/sample
          headers: { Accept: application/json }
          body: ""
      required_fields:
        - "$.report_id"

    # 3) PACS 检查预约 — HTTP + API Key header
    - vendor_id: pacs
      interface_id: study-booking
      display_name: "PACS 检查预约接口"
      protocol: http
      host: ${VENDOR_PACS_HOST}
      port: 443
      base_path: /pacs
      timeout_seconds: 8
      auth:
        type: apikey
        secret_env: VENDOR_PACS_APIKEY
        inject: header
        header_name: X-API-Key
      sample_endpoint:
        http:
          method: GET
          path: /studies/slot
          headers: { Accept: application/json }
          body: ""
      required_fields:
        - "$.study_uid"

    # 4) EMR 病历提交 — HL7v2 + MLLP
    - vendor_id: emr
      interface_id: emr-submit
      display_name: "EMR 提交接口（HL7v2/MLLP）"
      protocol: hl7v2
      host: ${VENDOR_EMR_MLLP_HOST}
      port: 6661
      timeout_seconds: 6
      auth:
        type: mllp
      sample_endpoint:
        hl7v2:
          message_type: ADT^A01
          trigger_event: A01
      required_fields:
        - "MSH-9"
        - "PID-3"

  inspection:
    fail_fast: false
    handover_dir: "./out/${DATE}"
    tickets_dir: "./out/${DATE}"
    sqlite_db: "./out/vendor_if_doctor.db"
    redact_credentials: true
"""


def init_cmd(
    output: Path = typer.Option(
        "vendor_spec.yaml",
        "--output",
        "-o",
        help="模板输出路径（默认当前目录 vendor_spec.yaml）",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="目标文件存在时仍覆盖",
    ),
) -> None:
    """生成 vendor_spec.yaml 模板。"""

    if output.exists() and not force:
        typer.echo(
            f"{output} 已存在；用 --force 覆盖或换个 --output 路径",
            err=True,
        )
        raise typer.Exit(code=1)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_TEMPLATE, encoding="utf-8")
    typer.echo(f"Wrote {output} ({len(_TEMPLATE)} chars)")


__all__ = ["init_cmd"]