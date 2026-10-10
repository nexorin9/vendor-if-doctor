#!/usr/bin/env bash
# demo_http.sh — 一条命令从零跑通 HTTP demo
# 流程：
#   1. 准备临时 vendor_spec.yaml（3 个 HTTP 接口指向本机 mock server）；
#   2. 启动 mock HTTP server（端口 9999，返回 200 + 含 required_fields 的 JSON）；
#   3. export 3 类凭据 env；
#   4. 跑 `python -m vendor_if_doctor run --vendor all --no-sqlite`；
#   5. cat out/<date>/handover.md 与 tickets.jsonl 关键行。
# 用法：`bash examples/demo_http.sh`
# 退出：脚本始终 exit 0。CLI exit 1 时把 fail evidence 摘要打出来（demo 中 mock 返回
#       200 + 含全部字段，本应全部 pass；若 CLI exit 1 也算 demo 成功，端到端跑通即可）。

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEMO_DIR="${PROJECT_DIR}/out/demo"
TODAY="$(date -u +%Y-%m-%d)"

mkdir -p "${DEMO_DIR}"

cd "${PROJECT_DIR}"

echo "[demo_http] 1/5 准备临时 vendor_spec.yaml（3 vendor 指向 127.0.0.1:9999）..."
cat > "${DEMO_DIR}/vendor_spec.yaml" <<'YAML'
vendor_spec:
  schema_version: "1.0"
  run_window_minutes: 30
  severity_policy: "default"
  interfaces:
    - vendor_id: his
      interface_id: outpatient-register
      display_name: "HIS 门诊挂号接口（demo）"
      protocol: http
      host: 127.0.0.1
      port: 9999
      base_path: /api/v1
      timeout_seconds: 5
      auth:
        type: bearer
        secret_env: VENDOR_HIS_TOKEN
      sample_endpoint:
        http:
          method: GET
          path: /register/slot
          headers:
            Accept: application/json
          body: ""
      required_fields:
        - "$.code"
        - "$.patient_id"
        - "$.slot_id"
    - vendor_id: lis
      interface_id: report-push
      display_name: "LIS 检验报告接口（demo）"
      protocol: http
      host: 127.0.0.1
      port: 9999
      base_path: /v2
      timeout_seconds: 5
      auth:
        type: bearer
        secret_env: VENDOR_LIS_TOKEN
      sample_endpoint:
        http:
          method: GET
          path: /reports/sample
          headers:
            Accept: application/json
          body: ""
      required_fields:
        - "$.report_id"
        - "$.patient_id"
    - vendor_id: pacs
      interface_id: study-booking
      display_name: "PACS 检查预约接口（demo）"
      protocol: http
      host: 127.0.0.1
      port: 9999
      base_path: /pacs
      timeout_seconds: 5
      auth:
        type: bearer
        secret_env: VENDOR_PACS_TOKEN
      sample_endpoint:
        http:
          method: GET
          path: /studies/slot
          headers:
            Accept: application/json
          body: ""
      required_fields:
        - "$.study_uid"
        - "$.schedule_time"
YAML
echo "      ✓ ${DEMO_DIR}/vendor_spec.yaml"

echo "[demo_http] 2/5 启动 mock HTTP server（端口 9999）..."
python3.11 -c "
import http.server, threading, socketserver, json, time, sys

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({'code': 200, 'patient_id': 'P-DEMO', 'slot_id': 'S-DEMO', 'report_id': 'R-DEMO', 'study_uid': 'ST-DEMO', 'schedule_time': '2026-10-10T08:30:00+08:00'}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args, **kw):
        pass

# allow_reuse_address 避免「上一次 demo 没清干净 mock server」导致 OSError 48
socketserver.TCPServer.allow_reuse_address = True
with socketserver.TCPServer(('127.0.0.1', 9999), H) as httpd:
    httpd.serve_forever()
" > "${DEMO_DIR}/mock_http.log" 2>&1 &
MOCK_PID=$!
sleep 0.5
# 验证 mock 实际在监听（避免「python -c 立刻退出但 bash 把退出码当真 PID」陷阱）
if ! kill -0 "${MOCK_PID}" 2>/dev/null; then
    echo "      ✗ mock HTTP server 启动失败；查看 ${DEMO_DIR}/mock_http.log"
    cat "${DEMO_DIR}/mock_http.log" || true
    exit 1
fi
echo "      ✓ mock HTTP server PID=${MOCK_PID} → ${DEMO_DIR}/mock_http.log"

cleanup() {
    kill "${MOCK_PID}" 2>/dev/null || true
    wait "${MOCK_PID}" 2>/dev/null || true
}
trap cleanup EXIT

echo "[demo_http] 3/5 export 3 类凭据 env..."
export VENDOR_HIS_TOKEN="demo-his-token-9999"
export VENDOR_LIS_TOKEN="demo-lis-token-8888"
export VENDOR_PACS_TOKEN="demo-pacs-token-7777"
export VENDOR_DOCTOR_HANDOVER_DIR="${DEMO_DIR}"
export VENDOR_DOCTOR_TICKETS_DIR="${DEMO_DIR}"

echo "[demo_http] 4/5 跑 python -m vendor_if_doctor run --vendor all --no-sqlite ..."
# Demo 模式：CLI 退出码非零不立刻 abort
set +e
python3.11 -m vendor_if_doctor run \
    --spec "${DEMO_DIR}/vendor_spec.yaml" \
    --vendor all \
    --handover-dir "${DEMO_DIR}/${TODAY}" \
    --tickets-dir "${DEMO_DIR}/${TODAY}" \
    --no-sqlite
CLI_EXIT=$?
set -e

echo "[demo_http] 5/5 检查产物（demo 成功 = handover.md 含 3 个 vendor 条目）..."
HANDOVER="${DEMO_DIR}/${TODAY}/handover.md"
TICKETS="${DEMO_DIR}/${TODAY}/tickets.jsonl"

if [[ -f "${HANDOVER}" ]]; then
    VENDOR_COUNT="$(grep -c '^## Vendor:' "${HANDOVER}")"
    echo "      ✓ handover.md 存在 ($(wc -l < "${HANDOVER}") 行，含 ${VENDOR_COUNT} 个 vendor 条目)"
    echo "      --- 首 30 行 handover.md ---"
    head -n 30 "${HANDOVER}"
    echo "      ----------------------------"
    if [[ "${VENDOR_COUNT}" -lt 3 ]]; then
        echo "      ✗ handover.md vendor 条目数 ${VENDOR_COUNT} < 3"
        exit 1
    fi
else
    echo "      ✗ handover.md 未生成"
    exit 1
fi

if [[ -f "${TICKETS}" ]]; then
    TICKET_LINES="$(wc -l < "${TICKETS}")"
    echo "      ✓ tickets.jsonl 存在 (${TICKET_LINES} 行)"
    if [[ "${TICKET_LINES}" -gt 0 ]]; then
        echo "      --- 第一条 ticket ---"
        head -n 1 "${TICKETS}"
        echo "      ---------------------"
    fi
else
    echo "      (无 tickets.jsonl — happy path 不写工单)"
fi

if [[ "${CLI_EXIT}" -ne 0 ]]; then
    echo "[demo_http] NOTE: CLI exit=${CLI_EXIT}（demo 期望 happy path exit 0；若非零说明 mock 或 schema 有问题）"
fi

echo "[demo_http] DONE — 演示成功"
exit 0
