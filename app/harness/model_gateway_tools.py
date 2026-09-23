"""网关层的原生工具调用入口，让「有界只读工具循环」复用 ``chat()`` 的选路/限流/
槽位/重放脚手架，不必每个调用方各自手搓一份。``app.portraits.identity_investigation``
的身份调查 Phase A 是第一个迁移进来的调用方（2026-09 两轮热修补的正是这里缺的
网关能力，见该模块 docstring）。

复用清单——全部直接调用既有函数，不重造：

- 选路：与 ``model_gateway.chat`` 相同的默认规则——显式 ``provider`` 优先，否则
  读 ``current_stage_text_provider()``。``hiagent.chat_with_tools`` 本身没有
  ``provider`` 覆盖入口（只有 ``model``，且调用方从未用到），这里解析出的
  ``provider`` 只影响限流分桶与换路判据，不改变它内部的实际选路；调用方若要让
  工具调用换到 stage 覆盖之外的 provider，需要知悉这一底层限制。
- trace 元数据：与 ``chat()`` 相同的 gateway/run_id/step_run_id/trace_id 前缀。
- 限流作用域：直接复用 ``model_gateway_failover.rate_limit_candidate/_scope``。
- 并发槽位：直接复用 ``app.generation_concurrency.run_with_provider_call_slot``。
- 「未送达」退避重放：直接复用 ``app.harness.undelivered_replay.replay_undelivered``
  ——这就是该模块本来专为工具调用抽出的同一条退避表（8 次、封顶延迟），不再手搓
  第二份重试循环。
- provider_calls 记录：真实请求仍是 hiagent 模块的 ``chat_with_tools``，它内部按
  ``kind="chat_tools"`` 自动记录，把 call_meta 传够即可，不需要额外代码。

**跨供应商换路刻意不接入。** ``chat()`` 的 content_rejection_failover /
technical_failure_failover 两个换路 helper 都固定改发一次不带工具定义的纯文本
请求（换路候选也从不校验是否支持 tools）；即便换路"成功"，调用方期待的
``AssistantTurn.tool_calls`` 也无从产生，等于把"有工具调查"静默降级成"没有工具
的一次性问答"——这正是 CLAUDE.md 禁止的静默降级。因此这里 fail closed：未送达
失败仍按 ``replay_undelivered`` 的退避表原地重放；重放预算耗尽或遇到不可重放的
失败，原样向上抛出，交回调用方自行处理，不做跨供应商换路。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app import hiagent
from app.harness.model_gateway_failover import rate_limit_candidate, rate_limit_scope
from app.harness.text_provider_scope import current_stage_text_provider
from app.harness.undelivered_replay import replay_undelivered
from app.observability.tracing import current_trace


async def chat_with_tools(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    tool_choice: str = "auto",
    temperature: float = 0.7,
    max_tokens: int = 65535,
    call_meta: dict[str, Any] | None = None,
    on_token: Callable[[str, str], None] | None = None,
    provider: str | None = None,
) -> hiagent.AssistantTurn:
    """带原生工具调用的网关入口；内部实际请求仍是 hiagent 模块的 ``chat_with_tools``。

    行为与 ``model_gateway.chat`` 对齐：trace 元数据打进 ``call_meta``、按
    ``provider`` 分桶限流、经并发槽位、未送达失败按既有退避表重放。不做跨供应商
    换路，理由见模块 docstring。
    """
    if provider is None:
        provider = current_stage_text_provider()
    trace = current_trace()
    meta = {
        "gateway": "execution_harness",
        "run_id": trace.run_id,
        "step_run_id": trace.step_run_id,
        "trace_id": trace.trace_id,
        **(call_meta or {}),
    }
    rl_candidate = rate_limit_candidate(provider)

    async def _attempt() -> hiagent.AssistantTurn:
        # 延迟导入：与 model_gateway.chat/model_gateway_failover 的两个换路
        # helper 同一惯例（三处都是函数内 import 同一个名字），monkeypatch
        # 只需对准 app.generation_concurrency 这一份权威源即可覆盖所有网关
        # 调用点，不必逐个模块单独打桩。
        from app.generation_concurrency import run_with_provider_call_slot

        async with rate_limit_scope(rl_candidate, estimated_tokens=max_tokens):
            return await run_with_provider_call_slot(
                lambda: hiagent.chat_with_tools(
                    messages, tools, tool_choice=tool_choice, temperature=temperature,
                    max_tokens=max_tokens, call_meta=meta, on_token=on_token,
                )
            )

    return await replay_undelivered(_attempt, call_meta=meta)
