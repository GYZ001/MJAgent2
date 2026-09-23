"""``app.harness.model_gateway_tools.chat_with_tools``：身份调查工具对话迁入网关
后的选路/限流/槽位/重放脚手架，以及刻意不做的跨供应商换路（会静默丢工具定义）。

真实供应商请求点仍是 hiagent 模块自身的 ``chat_with_tools``——本文件全部通过
monkeypatch 替换它，不触碰真实网络；``run_with_provider_call_slot`` 同样替换成
直通桩，避免依赖 settings.text_generation_concurrency 的具体取值。

``tests/conftest.py`` 的 ``_reset_capability_runtime`` 是全仓 autouse 夹具，
无条件把 ``identity_investigation._chat_with_tools`` 替换成一个空壳桩（详见该
函数 docstring）——测本文件想验证的「身份调查真的委托给网关」这件事，必须先
在测试模块加载时（早于任何夹具运行）把真实实现的引用另存一份，再在需要跑真实
实现的用例里用它把 autouse 桩临时叠盖回去；两次 setattr 用的是同一个
``monkeypatch`` 实例（同一测试内所有夹具共享），测试结束会按后进先出正确复原。
"""
from __future__ import annotations

import pytest

from app import config, hiagent
from app.harness import model_gateway_tools as mgt
from app.harness import undelivered_replay as ur
from app.harness.text_provider_scope import stage_text_provider
from app.portraits import identity_investigation as ii
from tests.patch_targets import patch_models_registry_everywhere

# 采集期（早于任何 autouse 夹具）保存真实实现，供下面两个用例把 conftest 的
# 空壳桩临时叠盖回真实实现。
_REAL_CHAT_WITH_TOOLS = ii._chat_with_tools


async def _passthrough_slot(fn):
    return await fn()


def _messages() -> list[dict]:
    return [{"role": "user", "content": "请核实这句引文"}]


async def test_chat_with_tools_threads_meta_provider_and_slot(monkeypatch) -> None:
    """选路/trace 元数据/并发槽位/kwargs 透传一次性核对：显式 provider 生效、
    call_meta 在调用方字段之外叠加网关的 trace 前缀、请求经过并发槽位。"""
    captured: dict[str, object] = {}

    async def fake_chat_with_tools(messages, tools, *, tool_choice, temperature, max_tokens, call_meta, on_token):
        captured.update(
            messages=messages, tools=tools, tool_choice=tool_choice, temperature=temperature,
            max_tokens=max_tokens, call_meta=call_meta, on_token=on_token,
        )
        return hiagent.AssistantTurn(content="ok")

    monkeypatch.setattr(hiagent, "chat_with_tools", fake_chat_with_tools)
    slot_calls = 0

    async def counting_slot(fn):
        nonlocal slot_calls
        slot_calls += 1
        return await fn()

    monkeypatch.setattr("app.generation_concurrency.run_with_provider_call_slot", counting_slot)

    messages, tools = _messages(), [{"type": "function", "function": {"name": "search_source"}}]
    turn = await mgt.chat_with_tools(
        messages, tools, temperature=0.1,
        call_meta={"stage_key": "screenplay_character_discovery"}, provider="prov-x",
    )

    assert turn.content == "ok"
    assert slot_calls == 1
    assert captured["messages"] is messages and captured["tools"] is tools
    assert captured["tool_choice"] == "auto" and captured["max_tokens"] == 65535
    assert captured["temperature"] == 0.1
    call_meta = captured["call_meta"]
    assert call_meta["stage_key"] == "screenplay_character_discovery"
    assert call_meta["gateway"] == "execution_harness"


async def test_provider_selection_defaults_to_stage_scope_but_explicit_wins(monkeypatch) -> None:
    """未显式传 provider 时回落 current_stage_text_provider()（与 model_gateway.chat
    同一条默认规则）；显式传入时优先，不被 stage 覆盖抢先。"""
    seen: list[str | None] = []

    def fake_rate_limit_candidate(provider):
        seen.append(provider)
        return None

    monkeypatch.setattr(mgt, "rate_limit_candidate", fake_rate_limit_candidate)
    monkeypatch.setattr(hiagent, "chat_with_tools", _fake_ok_turn)
    monkeypatch.setattr("app.generation_concurrency.run_with_provider_call_slot", _passthrough_slot)

    with stage_text_provider("stage-override"):
        await mgt.chat_with_tools(_messages(), [])
        await mgt.chat_with_tools(_messages(), [], provider="explicit-provider")

    assert seen == ["stage-override", "explicit-provider"]


async def _fake_ok_turn(messages, tools, **kwargs):
    return hiagent.AssistantTurn(content="ok")


async def test_undelivered_failure_is_replayed_with_existing_backoff(monkeypatch) -> None:
    """未送达（read 超时 0 字）按 app.harness.undelivered_replay 的既有退避表重放
    ——不是重新实现的第二份退避循环，是同一条表：直接断言它被真正驱动。"""
    monkeypatch.setattr(config, "TEXT_PROVIDER_MAX_RETRIES", 3)
    monkeypatch.setattr(config, "TEXT_PROVIDER_RETRY_BASE_DELAY", 30.0)
    monkeypatch.setattr(config, "TEXT_PROVIDER_RETRY_MAX_DELAY", 120.0)
    monkeypatch.setattr(ur, "log_provider_call", lambda *a, **kw: None)
    slept: list[float] = []

    async def record_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(ur.asyncio, "sleep", record_sleep)
    monkeypatch.setattr("app.generation_concurrency.run_with_provider_call_slot", _passthrough_slot)

    attempts = 0

    async def fake_chat_with_tools(messages, tools, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise hiagent.ProviderError(
                "调用read阶段超时（300010ms）", retryable=True, failure_kind="request_outcome_unknown",
                delivery_state="unknown", requires_explicit_retry=True, received_chars=0,
            )
        return hiagent.AssistantTurn(content="ok-after-retry")

    monkeypatch.setattr(hiagent, "chat_with_tools", fake_chat_with_tools)

    turn = await mgt.chat_with_tools(_messages(), [], provider="p")

    assert turn.content == "ok-after-retry"
    assert attempts == 2
    assert slept == [30.0]


async def test_non_replayable_failure_is_not_routed_to_cross_provider_failover(monkeypatch) -> None:
    """fail closed：限流失败不落在「未送达」三个判据里（有响应体、非流中断），
    只失败一次，绝不触发跨供应商换路——换路会悄悄换成不带工具定义的纯文本请求，
    静默丢弃工具调用能力。"""
    async def forbidden_failover(*_a, **_kw):
        raise AssertionError("chat_with_tools 不应触发跨供应商换路")

    # app.models_registry 是真实拆包（13 个先例之一），裸 monkeypatch.setattr(routing, ...)
    # 只打中 routing 自己这份绑定，守卫要求一律走 patch_models_registry_everywhere。
    patch_models_registry_everywhere(monkeypatch, "call_with_failover", forbidden_failover)
    monkeypatch.setattr("app.generation_concurrency.run_with_provider_call_slot", _passthrough_slot)

    attempts = 0

    async def fake_chat_with_tools(messages, tools, **kwargs):
        nonlocal attempts
        attempts += 1
        raise hiagent.ProviderError(
            "限流", retryable=True, failure_kind="rate_limited",
            delivery_state="responded", received_chars=500,
        )

    monkeypatch.setattr(hiagent, "chat_with_tools", fake_chat_with_tools)

    with pytest.raises(hiagent.ProviderError, match="限流"):
        await mgt.chat_with_tools(_messages(), [], provider="p")
    assert attempts == 1


async def test_identity_investigation_delegates_to_gateway_not_hiagent_directly(monkeypatch) -> None:
    """反向断言：把网关入口换成假实现后，hiagent 的 chat_with_tools 必须一次都不
    会被调用——证明身份调查现在只有一条经网关的路径，没有另一条绕过网关的直连。"""
    monkeypatch.setattr(ii, "_chat_with_tools", _REAL_CHAT_WITH_TOOLS)  # 叠盖 conftest 的空壳桩
    hiagent_calls = 0

    async def fail_if_called_directly(*_a, **_kw):
        nonlocal hiagent_calls
        hiagent_calls += 1
        raise AssertionError("不应绕过网关直接调用 hiagent 的 chat_with_tools")

    monkeypatch.setattr(hiagent, "chat_with_tools", fail_if_called_directly)

    gateway_calls = 0

    async def fake_gateway_chat_with_tools(messages, tools, **kwargs):
        nonlocal gateway_calls
        gateway_calls += 1
        return hiagent.AssistantTurn(content="via-gateway")

    monkeypatch.setattr(ii.model_gateway_tools, "chat_with_tools", fake_gateway_chat_with_tools)

    turn = await ii._chat_with_tools(
        _messages(), [], temperature=0.1, call_meta={"stage_key": "screenplay_character_discovery"},
    )

    assert turn.content == "via-gateway"
    assert gateway_calls == 1
    assert hiagent_calls == 0


async def test_identity_investigation_chat_with_tools_ignores_stage_scope_override(monkeypatch) -> None:
    """身份调查显式传 provider=hiagent.active_provider("text")：即使当前处于
    stage_text_provider(...) 覆盖范围内（映射台配了专属文本模型），工具调查仍必须
    用全局默认文本 provider——历史行为不变，供应商适配层的 chat_with_tools 本身
    也没有 provider 覆盖入口，不因为改走网关就悄悄开始跟随映射台的分环节模型。"""
    monkeypatch.setattr(ii, "_chat_with_tools", _REAL_CHAT_WITH_TOOLS)  # 叠盖 conftest 的空壳桩
    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "global-default-provider")
    monkeypatch.setattr(hiagent, "chat_with_tools", _fake_ok_turn)
    monkeypatch.setattr("app.generation_concurrency.run_with_provider_call_slot", _passthrough_slot)

    seen: list[str | None] = []

    def fake_rate_limit_candidate(provider):
        seen.append(provider)
        return None

    monkeypatch.setattr(mgt, "rate_limit_candidate", fake_rate_limit_candidate)

    with stage_text_provider("script-override-provider"):
        turn = await ii._chat_with_tools(_messages(), [], temperature=0.1, call_meta={})

    assert turn.content == "ok"
    assert seen == ["global-default-provider"]
