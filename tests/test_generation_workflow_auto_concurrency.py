"""剧本/分镜工作流并发 0/空=自动（安全阀），只受机器水位闸约束（2026-09-05 用户拍板）。"""
from __future__ import annotations


import pytest

from app import generation_concurrency as gc
from app.observability import machine_watermark


def test_blank_or_zero_means_safety_ceiling_and_explicit_values_are_kept(monkeypatch) -> None:
    values = {"text_generation_workflow_concurrency": ""}
    monkeypatch.setattr(gc, "get_setting", lambda key: values.get(key))
    assert gc._configured_limit("storyboard") == gc.MAX_TEXT_GENERATION_CONCURRENCY
    values["text_generation_workflow_concurrency"] = "0"
    assert gc._configured_limit("screenplay") == gc.MAX_TEXT_GENERATION_CONCURRENCY
    values["text_generation_workflow_concurrency"] = "5"
    assert gc._configured_limit("storyboard") == 5
    values["text_generation_workflow_concurrency"] = "999"
    assert gc._configured_limit("storyboard") == gc.MAX_TEXT_GENERATION_CONCURRENCY
    # 不再回落到供应商请求并发那个键
    values["text_generation_workflow_concurrency"] = ""
    values["text_generation_concurrency"] = "3"
    assert gc._configured_limit("storyboard") == gc.MAX_TEXT_GENERATION_CONCURRENCY


@pytest.mark.asyncio
async def test_workflow_slot_waits_for_the_machine_watermark(monkeypatch) -> None:
    monkeypatch.setattr(gc, "get_setting", lambda key: "")
    state = {"reason": "CPU 负载/核 1.2 ≥ 0.8", "polls": 0}

    def reason():
        state["polls"] += 1
        if state["polls"] >= 3:
            state["reason"] = None
        return state["reason"]

    monkeypatch.setattr(machine_watermark, "throttle_reason", reason)

    async def fast_wait(poll_s: float = 2.0) -> None:  # 同 wait_until_admitted，只是轮询间隔缩到 10ms
        import asyncio
        while machine_watermark.throttle_reason() is not None:
            await asyncio.sleep(0.01)

    monkeypatch.setattr(machine_watermark, "wait_until_admitted", fast_wait)

    async def operation() -> str:
        return "ran"

    assert await gc.run_with_generation_slot("storyboard", operation) == "ran"
    assert state["polls"] >= 3  # 前两次超标都等了，回落后才起工作流
