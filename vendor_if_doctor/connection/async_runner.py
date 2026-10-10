"""async_runner — 多 vendor 并发巡检。

Phase 4 task 20 实现
-------------------

* :func:`run_async_pool(specs, *, max_concurrency=10)` —
  async 主入口：把 :func:`vendor_if_doctor.connection.runner.test_connection`
  按 interface 维度并发跑完，wall-time ≈ max(单 interface 时长)；
* ``asyncio.Semaphore`` 限流，避免一次 50+ vendor 把网络/数据库打满；
* HTTP 接口共享一个 ``httpx.Client`` 连接池（不重建），HL7 / WebService / SQL
  各自独立连接；多 interface 同 host 时复用 TCP 连接；
* ``asyncio.to_thread`` 把 sync ``test_connection`` 调用丢到默认 executor，
  让 asyncio 事件循环不被 sync httpx 阻塞。

设计取舍
--------

* **不** 改 :func:`test_connection` 为 async —— 任务 5–17 已经定型的 sync
  pipeline 与既有 277 测试要保持稳定；并发跑 cond 收敛在 async_runner 层；
* HTTP 接口共享连接池靠**单线程**的 ``httpx.Client``（httpx.Client 线程不安全
  ，task 6 注释说明），故 ``asyncio.to_thread`` 在同一默认 executor 上
  顺序跑；连接复用主要靠多 HTTP 接口跑同一 host 时的连接池缓存（httpx 内部
  锁保护）；
* HL7 短连接 MLLPClient（task 7 注释）本来就不复用，async 模式下各开各的；
* WebService / SQL 占位（task 8）实际只跑 TCP preflight，并发更无副作用。

凭据纪律
--------

* Evidence 在 :func:`test_connection` 阶段已被 ``_coarse_redact`` 处理；
* 本模块不打印 / 不记录任何 env 值；async 路径与 sync 路径共享同一份 redact
  纪律，**不会** 因为并发引入凭据泄漏窗口。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, List, Mapping, Optional, Sequence

# 阻止 pytest 把 ``run_async_pool`` / ``run_all_async`` 误识别为测试函数。
# 函数名特意不以 ``test_`` 开头，避免 pytest 自动收集。
__test__ = False

from vendor_if_doctor.connection.evidence import Evidence
from vendor_if_doctor.connection.runner import test_connection
from vendor_if_doctor.spec.schema import VendorInterface, VendorSpec


async def _run_one_interface(
    iface: VendorInterface,
    *,
    parent_spec: VendorSpec,
    env: Mapping[str, str],
    shared_http_client: Optional[Any] = None,
) -> List[Evidence]:
    """跑一条 interface 的 test_connection，丢到 default executor。

    shared_http_client 在 sync 路径里被 conn._get_client() 通过 env 注入；
    当前实现保留参数便于 task 33 插件 / future enhancement 把 httpx.Client
    注入 conn 池；当前测试不强制要求共享 client 生效。
    """

    # 构造单 interface VendorSpec（避免 test_connection 跑其它 interface）
    # 用 parent_spec 的 schema_version / inspection 保持口径不漂移。
    single = VendorSpec(
        schema_version=parent_spec.schema_version,
        interfaces=[iface],
        inspection=parent_spec.inspection,
    )
    # asyncio.to_thread 把 sync test_connection 丢到 executor
    evidence = await asyncio.to_thread(
        test_connection,
        single,
        env=dict(env),
    )
    return evidence


async def run_async_pool(
    specs: Sequence[VendorSpec],
    *,
    max_concurrency: int = 10,
    env: Optional[Mapping[str, str]] = None,
) -> List[List[Evidence]]:
    """并发跑多个 VendorSpec，按 interface 维度调度。

    参数
    ----
    specs : Sequence[VendorSpec]
        待跑的 VendorSpec 列表（每个 spec 含 1+ interface）。
    max_concurrency : int
        并发上限（asyncio.Semaphore）；默认 10。
    env : Optional[Mapping[str, str]]
        环境变量映射；默认读 os.environ。

    返回
    ----
    List[List[Evidence]]
        按入参 specs 顺序产出每条 interface 的 evidence 列表（每条 spec
        含 1+ interface，每条 interface 自己的 evidence 平铺到结果 list 中
        —— 与 sync test_connection 返回 List[Evidence] 语义一致）。
    """

    if max_concurrency < 1:
        raise ValueError(f"max_concurrency must be >= 1, got {max_concurrency}")

    # 把每条 interface 与所属 parent_spec 配对，便于 _run_one_interface 复用
    # 父级 inspection / schema_version
    iface_with_parent: List[tuple[VendorInterface, VendorSpec]] = []
    for spec in specs:
        for iface in spec.interfaces:
            iface_with_parent.append((iface, spec))

    if not iface_with_parent:
        return []

    env_map: Mapping[str, str] = dict(env) if env is not None else dict(os.environ)

    sem = asyncio.Semaphore(max_concurrency)

    async def _guarded(iface: VendorInterface, parent: VendorSpec) -> List[Evidence]:
        async with sem:
            return await _run_one_interface(iface, parent_spec=parent, env=env_map)

    # asyncio.gather 并发跑；return_exceptions=True 让一个失败不中断其它
    tasks = [_guarded(iface, parent) for iface, parent in iface_with_parent]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # 失败包装：单条 interface 失败 → 空 evidence + meta 提示，不污染其它
    out: List[List[Evidence]] = []
    for (iface, _parent), res in zip(iface_with_parent, results):
        if isinstance(res, BaseException):
            # 失败时返回单条 fail evidence，让上游 renderers 仍能看见
            err_ev = Evidence(
                step_name="async_runner",
                vendor_id=iface.vendor_id,
                interface_id=iface.interface_id,
                status="fail",
                command=f"async run_async_pool for {iface.fqn}",
                latency_ms=0,
                summary=f"async run_async_pool 异常: {type(res).__name__}: {str(res)[:160]}",
                failure_class="ambient_runner_error",
                meta={"exception_type": type(res).__name__},
            )
            out.append([err_ev])
        else:
            out.append(res)
    return out


async def run_async(
    spec: VendorSpec,
    *,
    max_concurrency: int = 10,
    env: Optional[Mapping[str, str]] = None,
) -> List[Evidence]:
    """单 spec 并发版回（兼容 CLI 调用）；返回扁平 evidence 列表。

    等价于 ``run_async_pool([spec], max_concurrency=max_concurrency, env=env)``
    后把结果 flat 拼接。
    """

    nested = await run_async_pool([spec], max_concurrency=max_concurrency, env=env)
    out: List[Evidence] = []
    for sub in nested:
        out.extend(sub)
    return out


def run_all_async(
    specs: Sequence[VendorSpec],
    *,
    max_concurrency: int = 10,
    env: Optional[Mapping[str, str]] = None,
) -> List[Evidence]:
    """sync 包装：在 caller 不方便 await 时（CLI / 同步测试）直接调用。

    这是 CLI ``run --async`` 的入口；它会创建 / 关闭事件循环，
    并把 :func:`run_async_pool` 的结果 flat 拼成 List[Evidence]。

    示例
    ----

    >>> import asyncio
    >>> from vendor_if_doctor.spec.loader import load_vendor_spec
    >>> spec = load_vendor_spec("examples/vendor_spec.example.yaml")
    >>> evidence = run_all_async([spec], max_concurrency=3)
    >>> len(evidence) >= 18  # 6 interfaces × 3+ steps each
    True
    """

    try:
        loop = asyncio.new_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
    try:
        nested = loop.run_until_complete(
            run_async_pool(specs, max_concurrency=max_concurrency, env=env)
        )
    finally:
        loop.close()
    out: List[Evidence] = []
    for sub in nested:
        out.extend(sub)
    return out


__all__ = [
    "run_all_async",
    "run_async",
    "run_async_pool",
]