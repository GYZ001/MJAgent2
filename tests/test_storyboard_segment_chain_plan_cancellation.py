"""独立审查发现 2 回归（2026-10-01，换场并行链）：
``app.production.storyboard_segment_chain_plan._run_chains_parallel`` 一条链
失败时必须取消仍在进行中的其它链，不能留下后台孤儿模型调用——这是
``storyboard_segment_chains``/``storyboard_segment_chain_plan`` 多链并行调度
机制的一部分，见那两个模块的模块 docstring。

审查实测复现的真实行为：裸 ``asyncio.gather``（``return_exceptions=False``）
在第一个协程抛出异常时会立刻把异常传给调用方，但不会取消其余仍在进行中的
协程——那些协程会在后台继续跑完一次真实模型调用，结果无人读取被静默丢弃，
白白耗费本机有限的 CPU/网络资源，调用方捕获异常后立刻重试还会与孤儿调用
撞上同一集的重复真实模型调用。修法是改用 ``asyncio.wait(...,
return_when=asyncio.FIRST_EXCEPTION)``，失败时主动 ``cancel()`` 并等待其余
任务真正收尾，再重新抛出原始异常（不包装成 ``ExceptionGroup``，否则上层按
原异常类型 except 的分支会被悄悄破坏）。
"""
from __future__ import annotations

import asyncio

import pytest

import app.production.storyboard_segment_chain_plan as chain_plan_module


@pytest.mark.asyncio
async def test_run_chains_parallel_cancels_pending_chain_on_failure(monkeypatch):
    """链 A 立即失败、链 B 仍在 ``await asyncio.sleep`` 中：修法后 B 必须在 A
    失败后立刻被取消，不会跑到 ``ran_to_completion``；向上传播的仍是原始
    异常类型（不是 ``ExceptionGroup``），上层按 ``RuntimeError`` 的 except
    分支继续可用。"""
    started: list[str] = []
    ran_to_completion: list[str] = []

    async def fake_gated(ctx, chain_plans, semaphore):
        async with semaphore:
            started.append(chain_plans)
            if chain_plans == "A":
                raise RuntimeError("boom-A")
            await asyncio.sleep(0.3)
            ran_to_completion.append(chain_plans)
            return "state-B"

    monkeypatch.setattr(chain_plan_module, "_run_one_chain_gated", fake_gated)

    with pytest.raises(RuntimeError, match="boom-A"):
        await chain_plan_module._run_chains_parallel(None, ["A", "B"])

    await asyncio.sleep(0.5)  # 给被取消的 B 留出真正收尾（CancelledError 传播完）的时间
    assert started == ["A", "B"], "Semaphore(2) 下两条链应该都已经开始，不是 A 挡住了 B"
    assert ran_to_completion == [], "链 A 失败后链 B 必须被取消，不能在后台静默跑完一次真实调用"


@pytest.mark.asyncio
async def test_run_chains_parallel_merges_in_chain_order_when_all_succeed():
    """全部链成功时按链的原始顺序合并——不是取消路径，验证正常路径没有被
    重写坏：返回的 ``merged``/``outcomes`` 与裸 ``asyncio.gather`` 时行为一致。"""

    class _FakeState:
        def __init__(self, segment_no: int) -> None:
            self.by_segment_no = {segment_no: f"draft-{segment_no}"}
            self.review_outcomes = [{"segment_no": segment_no}]

    async def fake_gated(ctx, chain_plans, semaphore):
        async with semaphore:
            await asyncio.sleep(0.01 if chain_plans[0] == 1 else 0)
            return _FakeState(chain_plans[0])

    import app.production.storyboard_segment_chain_plan as m
    original = m._run_one_chain_gated
    m._run_one_chain_gated = fake_gated
    try:
        merged, outcomes = await chain_plan_module._run_chains_parallel(None, [[1], [2]])
    finally:
        m._run_one_chain_gated = original

    assert merged == {1: "draft-1", 2: "draft-2"}
    assert outcomes == [{"segment_no": 1}, {"segment_no": 2}]
