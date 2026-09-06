"""对话类调用未送达/未处理时的退避重放（2026-09-06 第 11 轮第 23 集：身份调查工具对话 read 超时 300s，整台失败）。

``model_gateway.chat`` 里已经有一条带退避的重放（8 次、封顶 120s），但不是所有文本调用都从它进——
``hiagent.chat_with_tools``（身份调查的工具对话）直接打供应商，超时/流中断/网关信封一次就死。
本模块把同一条退避表包成一个可复用的包装：只重放**没有作者答案可保留**的失败——
* ``provider_answer_undelivered``：首字前超时（0 字）、流在 [DONE] 前被掐；
* ``replay_safe_stream_interruption``：文本对话的流中断 / 送达状态未知的网络错误；
* ``provider_envelope_unprocessed``：限流 / 网关超时的结构化错误信封（网关自己说没处理）。
已送达但不可用的答案（业务校验失败、结构不合法）照常一次失败，不重摇。退避睡在供应商槽位之外：
调用方把 ``run_with_provider_call_slot`` 放在 ``operation`` 里面。
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from app import config, hiagent
from app.db import log_provider_call
from app.harness.model_gateway_moderation import provider_envelope_unprocessed, replay_safe_stream_interruption

T = TypeVar("T")


def replayable(exc: BaseException) -> bool:
    return (
        hiagent.provider_answer_undelivered(exc)
        or replay_safe_stream_interruption(exc)
        or provider_envelope_unprocessed(exc)
    )


async def replay_undelivered(operation: Callable[[], Awaitable[T]], *, call_meta: dict | None = None) -> T:
    """按网关同一条退避表重放 ``operation``；不可重放的失败原样抛出，预算用尽抛最后一次的异常。"""
    max_retries = config.TEXT_PROVIDER_MAX_RETRIES
    for failure_no in range(max_retries + 1):
        try:
            return await operation()
        except hiagent.ProviderError as exc:
            if failure_no >= max_retries or not replayable(exc):
                raise
            delay = min(config.TEXT_PROVIDER_RETRY_BASE_DELAY * (2 ** failure_no), config.TEXT_PROVIDER_RETRY_MAX_DELAY)
            log_provider_call(
                "chat_tools_replay", str((call_meta or {}).get("model") or ""), "RETRY", None, 0,
                meta={**(call_meta or {}), "retry_no": failure_no + 1, "max_retries": max_retries, "delay_s": delay,
                      "failure_kind": getattr(exc, "failure_kind", ""), "error": str(exc)[:200]},
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable undelivered replay state")
