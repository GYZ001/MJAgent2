"""视频换路前的供应商终态失败确认。L3（同 app.video_providers）。

CLAUDE.md 记录的既定纪律：文本调用不计费，视频计费；客户端超时不等于供应商
侧失败——那边可能正在真实出片，自动换路重发就是重复计费（EP-05 §6 第 3 条
硬约束）。本模块是 ``app.models_registry.routing.call_with_failover`` 的
``confirm_terminal_failure`` 回调的真实实现：按 task_id 向供应商适配器发起一次
真实轮询，只有轮询本身成功返回、且状态明确是 ``"failed"`` 才认定终态失败、
允许换路；轮询本身失败/超时，或状态是 running/succeeded，一律返回 False——
fail-closed，不确定就不允许再花一笔钱。

本阶段只提供这个可复用的确认原语与它的端到端测试（见
``tests/test_model_routing_failover.py`` 的视频费用纪律用例：构造"客户端超时
但供应商任务仍在跑"的场景，断言没有产生第二次供应商调用）。接入
``app/media_exec/run_job.py`` 真实的多供应商换路重发（创建新任务、记新的
budget claim、切换 job 归属 provider）是一项更大的改造——那条路径本身已经有
一套独立的、经过实战检验的防重复计费机制（``provider_create_state``/
``ProviderCreateUnresolved``，见 ``app/media_exec/run_job.py``），本阶段不改动
它，留给专门的一轮，见交付报告"未完成项"。
"""
from __future__ import annotations

from typing import Any


async def confirm_video_terminal_failure(
    provider: str, task_id: str, *, call_meta: dict[str, Any] | None = None,
) -> bool:
    """真实轮询一次供应商任务状态，只有明确 ``"failed"`` 才返回 True。"""
    from app import video_providers

    if not str(task_id or "").strip():
        return False
    adapter = video_providers.resolve(provider)
    try:
        result = await adapter.poll_video_task(task_id, call_meta=call_meta)
    except Exception:  # noqa: BLE001 确认本身失败：fail-closed，不允许换路
        return False
    return bool(result) and result.get("status") == "failed"
