#!/usr/bin/env bash
# demo_hl7v2.sh — 一条命令从零跑通 HL7v2 / MLLP demo
# 流程：
#   1. 准备临时 vendor_spec.yaml（EMR 接口走 HL7v2 + MLLP，指向 127.0.0.1:9998）；
#   2. 启动 mock MLLP server（返回 MSA|AA ACK）；
#   3. 跑 `python -m vendor_if_doctor run --vendor emr --no-sqlite`；
#   4. cat handover.md 关键段 + tickets.jsonl 关键行；
#   5. 清理 mock server。
# 用法：`bash examples/demo_hl7v2.sh`
# 退出：脚本始终 exit 0。Demo 故意演示「sample step 通过（MSA-1=AA）但 assert step 失败
#       （ACK 段不含 PID-3/PV1-7 业务字段）→ 工单草稿落盘」这条路径，CLI exit 1 被吞掉。
#       Demo 的目标是「端到端跑通 → mock 返回 ACK → 工单草稿落盘」。

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEMO_DIR="${PROJECT_DIR}/out/demo-hl7"
TODAY="$(date -u +%Y-%m-%d)"

mkdir -p "${DEMO_DIR}"

cd "${PROJECT_DIR}"

echo "[demo_hl7v2] 1/5 准备临时 vendor_spec.yaml（指向 127.0.0.1:9998）..."
cat > "${DEMO_DIR}/vendor_spec.yaml" <<'YAML'
vendor_spec:
  schema_version: "1.0"
  run_window_minutes: 30
  severity_policy: "default"
  interfaces:
    - vendor_id: emr
      interface_id: emr-submit
      display_name: "电子病历 EMR 提交接口（demo）"
      protocol: hl7v2
      host: 127.0.0.1
      port: 9998
      timeout_seconds: 5
      auth:
        type: mllp
      sample_endpoint:
        hl7v2:
          message_type: ADT^A01
          trigger_event: A01
      # Demo 故意要求业务字段（PID-3 / PV1-7）：mock ACK 只返 MSA|AA，assert 步骤会
      # 触发 schema_mismatch → 工单草稿落盘。这模拟真实厂商「网络通、业务字段不对」的现场。
      required_fields:
        - "MSH-9"
        - "PID-3"
        - "PV1-7"
YAML
echo "      ✓ ${DEMO_DIR}/vendor_spec.yaml"

echo "[demo_hl7v2] 2/5 启动 mock MLLP server（端口 9998，返回 MSA|AA）..."
python3.11 -c "
import socket, threading, time

VT = b'\x0b'
FS = b'\x1c'
CR = b'\x0d'

def wrap(msg):
    return VT + msg.encode('utf-8') + FS + CR

ACK = wrap('MSH|^~\\\\&|ACK|DEMO|DEMO||20261010||ACK|EMR1|P|2.5\rMSA|AA|EMR1|OK')

def serve():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('127.0.0.1', 9998))
    s.listen(8)
    while True:
        try:
            conn, _ = s.accept()
        except OSError:
            return
        threading.Thread(target=handle, args=(conn,), daemon=True).start()

def handle(conn):
    conn.settimeout(3.0)
    buf = b''
    try:
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
            if FS in buf and CR in buf:
                break
        conn.sendall(ACK)
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass

serve()
" > "${DEMO_DIR}/mock_mllp.log" 2>&1 &
MOCK_PID=$!
sleep 0.5
# 验证 mock 实际在监听（避免「python -c 立刻退出但 bash 把退出码当真 PID」陷阱）
if ! kill -0 "${MOCK_PID}" 2>/dev/null; then
    echo "      ✗ mock MLLP server 启动失败；查看 ${DEMO_DIR}/mock_mllp.log"
    cat "${DEMO_DIR}/mock_mllp.log" || true
    exit 1
fi
echo "      ✓ mock MLLP server PID=${MOCK_PID} → ${DEMO_DIR}/mock_mllp.log"

cleanup() {
    kill "${MOCK_PID}" 2>/dev/null || true
    wait "${MOCK_PID}" 2>/dev/null || true
}
trap cleanup EXIT

echo "[demo_hl7v2] 3/5 export 凭据 env..."
export VENDOR_DOCTOR_HANDOVER_DIR="${DEMO_DIR}"
export VENDOR_DOCTOR_TICKETS_DIR="${DEMO_DIR}"

echo "[demo_hl7v2] 4/5 跑 python -m vendor_if_doctor run --vendor emr --no-sqlite ..."
# Demo 模式：CLI 退出码非零不立刻 abort（demo 的目标是「端到端跑通 + 工单草稿落盘」）
set +e
python3.11 -m vendor_if_doctor run \
    --spec "${DEMO_DIR}/vendor_spec.yaml" \
    --vendor emr \
    --handover-dir "${DEMO_DIR}/${TODAY}" \
    --tickets-dir "${DEMO_DIR}/${TODAY}" \
    --no-sqlite
CLI_EXIT=$?
set -e

echo "[demo_hl7v2] 5/5 检查产物..."
HANDOVER="${DEMO_DIR}/${TODAY}/handover.md"
TICKETS="${DEMO_DIR}/${TODAY}/tickets.jsonl"

if [[ -f "${HANDOVER}" ]]; then
    echo "      ✓ handover.md 存在 ($(wc -l < "${HANDOVER}") 行)"
    echo "      --- 关键片段 ---"
    grep -E '^## Vendor|^### Status|首次失败步骤|hl7' "${HANDOVER}" || true
    echo "      ----------------------------"
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
    else
        echo "      ✗ tickets.jsonl 为空（demo 期望至少 1 条工单草稿）"
        exit 1
    fi
else
    echo "      ✗ tickets.jsonl 未生成"
    exit 1
fi

if [[ "${CLI_EXIT}" -ne 0 ]]; then
    echo "[demo_hl7v2] NOTE: CLI exit=${CLI_EXIT}（demo 预期路径：MSA-1=AA 但 required_fields 检查触发 schema_mismatch → 工单草稿落盘）"
fi

echo "[demo_hl7v2] DONE — HL7v2 演示成功"
exit 0
