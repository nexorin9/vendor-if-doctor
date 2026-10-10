"""async_runner 单元测试 — task 20 验收。

测试目标
--------

1. :func:`run_async_pool` 基本契约：
   - 空 specs 返回 ``[]``；
   - 单 spec 跑通返回 List[List[Evidence]] 长度 = interface 数；
   - max_concurrency < 1 抛 ValueError；
2. :func:`run_all_async` sync 包装：可直接在 sync 测试中调用；
3. **并发墙钟验证**：5 vendor 单跑总时长 ≈ 5 × 1.0s（preflight timeout），
   并发跑总时长 ≈ 1.0–2.0s（max 而非 sum）；
4. **Semaphore 限流验证**：max_concurrency=2 时跑 4 个 vendor，wall-time
   ≈ ceil(4/2) × 单 vendor 时长；
5. 凭据纪律：async 路径不引入额外凭据泄漏窗口（evidence.response_body
   仍不含 token 字面量）。

实现说明
--------

* 用 ``tmp_path`` 写临时 spec 文件（loader 需要 PathLike）；
* monkeypatch ``connection.network.preflight`` + ``adapters.http.preflight``
  替换成 ``time.sleep(0.20)``，制造 5 vendor × 200ms ≈ 1.0s 的顺序
  baseline，便于观察并发收益；
* 单 spec 端口写 127.0.0.1:1 让 preflight 失败（不依赖 mock server）。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from vendor_if_doctor.connection.async_runner import (
    run_all_async,
    run_async,
    run_async_pool,
)
from vendor_if_doctor.connection.evidence import Evidence
from vendor_if_doctor.spec.loader import load_vendor_spec


def _write_spec_yaml(tmp_path: Path, name: str, vendor_id: str, interface_id: str = "ping") -> Path:
    yaml_text = f"""
vendor_spec:
  schema_version: "1.0"
  run_window_minutes: 30
  severity_policy: "default"
  interfaces:
    - vendor_id: {vendor_id}
      interface_id: {interface_id}
      display_name: 巡检测试接口
      protocol: http
      host: 127.0.0.1
      port: 1
      base_path: /
      timeout_seconds: 5.0
      auth:
        type: bearer
        secret_env: VENDOR_{vendor_id.upper()}_TOKEN
      sample_endpoint:
        http:
          method: GET
          path: /health
      required_fields:
        - "$.code"
        - "$.patient_id"
  inspection:
    fail_fast: false
    handover_dir: "./out/${{DATE}}"
    tickets_dir: "./out/${{DATE}}"
    sqlite_db: "./vendor_if_doctor.db"
    redact_credentials: true
"""
    p = tmp_path / name
    p.write_text(yaml_text, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 基础契约
# ---------------------------------------------------------------------------


def test_run_all_async_empty_specs_returns_empty():
    """空 specs 返回 []，不抛错。"""
    out = run_all_async([])
    assert out == []


def test_run_all_async_basic_returns_evidence_per_interface(tmp_path):
    """run_all_async 跑通 1 spec，返回 evidence 数 = interface 数 × 5。"""

    spec_path = _write_spec_yaml(tmp_path, "spec0.yaml", "test0")
    env = {"VENDOR_TEST0_TOKEN": "test0-secret-token-XXXXX"}
    spec = load_vendor_spec(spec_path, env=env)

    evidence = run_all_async([spec], env=env)
    # 1 interface × 5 steps = 5 evidence（preflight fail + 4 skipped）
    assert len(evidence) == 5
    assert all(isinstance(e, Evidence) for e in evidence)
    # preflight 必失败（端口 1 不可达）
    assert any(e.step_name == "preflight" for e in evidence)


def test_test_all_async_max_concurrency_validation(tmp_path):
    """max_concurrency < 1 抛 ValueError。"""

    async def _go():
        await run_async_pool([], max_concurrency=0)

    with pytest.raises(ValueError, match="max_concurrency must be >= 1"):
        asyncio.run(_go())


# ---------------------------------------------------------------------------
# 并发性能（wall-time 验证）
# ---------------------------------------------------------------------------


def test_run_all_async_concurrent_wall_time(tmp_path, monkeypatch):
    """5 vendor 并发跑 vs 顺序跑 —— wall-time ≈ max 而非 sum。

    实现：monkeypatch connection.network.preflight 让它 sleep 200ms，
    让单 interface 时长 ≈ 200ms+。5 vendor 顺序跑 ≈ 5×200=1000ms；
    并发跑 max=10 时 ≈ 200–400ms（allow overhead）。
    """

    import vendor_if_doctor.connection.network as network_mod
    import vendor_if_doctor.adapters.http as http_mod

    real_preflight = network_mod.preflight
    sleep_seconds = 0.20

    def slow_preflight(host, port, timeout=1.0):
        time.sleep(sleep_seconds)
        return real_preflight(host, port, timeout=timeout)

    monkeypatch.setattr(network_mod, "preflight", slow_preflight)
    monkeypatch.setattr(http_mod, "preflight", slow_preflight)

    # 构造 5 vendor specs（端口 127.0.0.1:1 让 preflight 失败）
    specs = []
    env = {}
    for i in range(5):
        path = _write_spec_yaml(tmp_path, f"spec{i}.yaml", f"bench{i}")
        env[f"VENDOR_BENCH{i}_TOKEN"] = f"bench{i}-token-XXXXX"
        specs.append(load_vendor_spec(path, env=env))

    # 1) 顺序跑 baseline（max_concurrency=1）
    t0 = time.monotonic()
    seq_evidence = run_all_async(specs, max_concurrency=1, env=env)
    seq_ms = int((time.monotonic() - t0) * 1000)

    # 2) 并发跑 max_concurrency=10
    t0 = time.monotonic()
    par_evidence = run_all_async(specs, max_concurrency=10, env=env)
    par_ms = int((time.monotonic() - t0) * 1000)

    # baseline：5 × 200ms = 1000ms（CI 机器慢也至少 700ms）
    assert seq_ms >= 5 * sleep_seconds * 1000 * 0.6, (
        f"顺序跑太短：seq_ms={seq_ms} < 期望 ~1000ms"
    )

    # 并发：1 × 200ms = 200ms（CI 上限到 600ms 都算并发收益）
    assert par_ms < seq_ms * 0.6, (
        f"并发没提速：par_ms={par_ms} seq_ms={seq_ms}（期望 par<seq*0.6）"
    )

    # evidence 数一致（5 vendor × 5 step = 25）
    assert len(seq_evidence) == len(par_evidence) == 25
    # 每条 evidence 必含 step_name + vendor_id
    for ev in par_evidence:
        assert ev.step_name
        assert ev.vendor_id
        assert ev.interface_id


def test_run_all_async_semaphore_throttles_concurrency(tmp_path, monkeypatch):
    """Semaphore 限流验证：max_concurrency=2 + 4 vendor → wall-time ≈ 2× 单 vendor 时长。

    单 vendor 时长 ≈ 200ms（slow preflight）。4 vendor × 200ms / 2 ≈ 400ms。
    """

    import vendor_if_doctor.connection.network as network_mod
    import vendor_if_doctor.adapters.http as http_mod

    real_preflight = network_mod.preflight
    sleep_seconds = 0.20

    def slow_preflight(host, port, timeout=1.0):
        time.sleep(sleep_seconds)
        return real_preflight(host, port, timeout=timeout)

    monkeypatch.setattr(network_mod, "preflight", slow_preflight)
    monkeypatch.setattr(http_mod, "preflight", slow_preflight)

    specs = []
    env = {}
    for i in range(4):
        path = _write_spec_yaml(tmp_path, f"throttle{i}.yaml", f"throttle{i}")
        env[f"VENDOR_THROTTLE{i}_TOKEN"] = f"throttle{i}-token-XXXXX"
        specs.append(load_vendor_spec(path, env=env))

    t0 = time.monotonic()
    evidence = run_all_async(specs, max_concurrency=2, env=env)
    wall_ms = int((time.monotonic() - t0) * 1000)

    # 4 vendor × 200ms / 2 concurrent = 400ms（allow overhead 200–700ms）
    assert wall_ms >= 2 * sleep_seconds * 1000 * 0.5, (
        f"限流过宽：wall_ms={wall_ms} < 期望 ~400ms"
    )
    # 也不应跑出并发全开的水平（< 200ms）
    assert wall_ms > 1 * sleep_seconds * 1000 * 0.6, (
        f"限流未生效：wall_ms={wall_ms} 已与 max=10 并发相当"
    )

    assert len(evidence) == 4 * 5


# ---------------------------------------------------------------------------
# 凭据纪律
# ---------------------------------------------------------------------------


def test_run_all_async_no_credential_leak_in_evidence(tmp_path):
    """async 路径 evidence 不引入额外凭据泄漏窗口。"""

    spec_path = _write_spec_yaml(tmp_path, "leak.yaml", "leak0")
    secret = "SUPER-SECRET-TOKEN-LEAK-CHECK-XYZ"
    env = {"VENDOR_LEAK0_TOKEN": secret}
    spec = load_vendor_spec(spec_path, env=env)

    evidence = run_all_async([spec], env=env)

    # evidence.summary / command / response_body 不含 token 字面量
    for ev in evidence:
        assert secret not in ev.summary
        assert secret not in ev.command
        # response_body 可能是 None（mock host 不可达）
        if ev.response_body:
            assert secret not in ev.response_body


def test_run_async_single_spec_returns_flat_evidence(tmp_path):
    """run_async(spec) 等价于 test_all_async([spec]) flat 拼接。"""

    spec_path = _write_spec_yaml(tmp_path, "flat.yaml", "flat0")
    env = {"VENDOR_FLAT0_TOKEN": "flat0-token-XXXXX"}
    spec = load_vendor_spec(spec_path, env=env)

    # 同步包装
    evidence_sync = run_all_async([spec], env=env)
    # async 入口
    async def _gather():
        return await run_async(spec, env=env)

    evidence_async = asyncio.run(_gather())

    assert len(evidence_sync) == len(evidence_async) == 5
    # 同 (vendor, interface, step_name) 必同 latency_ms 量级
    for ev_s, ev_a in zip(evidence_sync, evidence_async):
        assert ev_s.step_name == ev_a.step_name
        assert ev_s.vendor_id == ev_a.vendor_id
        assert ev_s.interface_id == ev_a.interface_id


def test_run_all_async_per_vendor_call_failure_returns_fail_evidence(tmp_path, monkeypatch):
    """单 interface 抛异常时，run_all_async 包装为单条 fail evidence，不污染其它。"""

    # monkeypatch async_runner 引用的 test_connection 抛异常
    import vendor_if_doctor.connection.async_runner as ar_mod

    real_test_connection = ar_mod.test_connection

    def boom(spec, env=None):
        # 仅对 boom vendor 抛错
        if spec.interfaces[0].vendor_id == "boom":
            raise RuntimeError("simulated failure")
        return real_test_connection(spec, env=env)

    monkeypatch.setattr(ar_mod, "test_connection", boom)

    spec_path_ok = _write_spec_yaml(tmp_path, "ok.yaml", "ok")
    spec_path_boom = _write_spec_yaml(tmp_path, "boom.yaml", "boom")
    env = {"VENDOR_OK_TOKEN": "ok-token-X", "VENDOR_BOOM_TOKEN": "boom-token-X"}
    spec_ok = load_vendor_spec(spec_path_ok, env=env)
    spec_boom = load_vendor_spec(spec_path_boom, env=env)

    nested = asyncio.run(
        run_async_pool([spec_ok, spec_boom], max_concurrency=2, env=env)
    )
    # 2 interface 各自一条 list
    assert len(nested) == 2
    # ok vendor 5 evidence（preflight fail + 4 skip）
    assert len(nested[0]) == 5
    assert all(e.vendor_id == "ok" for e in nested[0])
    # boom vendor 1 条 fail evidence（包装异常）
    assert len(nested[1]) == 1
    assert nested[1][0].vendor_id == "boom"
    assert nested[1][0].status == "fail"
    assert nested[1][0].failure_class == "ambient_runner_error"
    assert "RuntimeError" in nested[1][0].summary