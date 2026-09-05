"""WS1b：文本模型审核拒答换路（docs/failure_triage_and_self_heal_plan_2026-09-05.md）。

``app.harness.model_gateway.chat()`` 收到结构化 MODEL_REJECTION
（``app.hiagent.ProviderFailure.model_rejection()``，对外码 LLM-REJECTED）时，
若 settings.text_moderation_fallback_route（"provider:model"）非空，换路重试
一次；system 提示前必须挂固定框架语；仍拒答或未配置都必须原样抛出第一次的
错误，不得被换路请求自己的报错覆盖（CLAUDE.md「界面承诺必须与实际行为一
致」——调用方看到的应当是"这次请求为什么失败"，不是"换路目的地本身出了什么
问题"）。

打桩方式与 ``tests/test_text_provider_retry.py`` 一致：直接
``monkeypatch.setattr(model_gateway.hiagent, "chat", fake_chat)``——
``model_gateway.hiagent`` 与 ``model_gateway_moderation.hiagent`` 是同一个
``app.hiagent`` 模块对象（两边都写 ``from app import hiagent`` 后按属性调用
``hiagent.chat(...)``，不是 ``from app.hiagent import chat`` 提前绑定值），
打一次桩两条调用路径都生效。
"""
from __future__ import annotations

import asyncio

import pytest

from app import hiagent
from app.harness import model_gateway, model_gateway_moderation


def _model_rejection_error(message: str) -> hiagent.ProviderError:
    return hiagent.ProviderError(
        message, raw=message, failure=hiagent.ProviderFailure.model_rejection(),
    )


def test_model_rejection_without_fallback_route_raises_original_error(monkeypatch) -> None:
    """未配置换路目的地：不得猜一个供应商，原样抛出，只发生一次真实请求。"""
    monkeypatch.setattr(model_gateway_moderation, "get_setting", lambda _key: "")
    calls: list[list[dict[str, str]]] = []

    async def fake_chat(messages, **_kwargs):
        calls.append(messages)
        raise _model_rejection_error("供应商内容审核已明确拒绝本次请求")

    monkeypatch.setattr(model_gateway.hiagent, "chat", fake_chat)

    with pytest.raises(hiagent.ProviderError, match="供应商内容审核已明确拒绝本次请求"):
        asyncio.run(model_gateway.chat([{"role": "user", "content": "写一段追杀情节"}]))

    assert len(calls) == 1


def test_model_rejection_with_fallback_route_second_call_succeeds(monkeypatch) -> None:
    """配置了换路目的地：第一次结构化拒答后换路重试一次并成功——system 提示
    前挂固定框架语，provider/model 按配置整体切换（配套传递，不拆开）。"""
    monkeypatch.setattr(
        model_gateway_moderation, "get_setting",
        lambda _key: "custom-provider:custom-model",
    )
    calls: list[tuple[list[dict[str, str]], dict]] = []

    async def fake_chat(messages, **kwargs):
        calls.append((messages, kwargs))
        if len(calls) == 1:
            raise _model_rejection_error("供应商内容审核已明确拒绝本次请求")
        return "换路后正常产出"

    monkeypatch.setattr(model_gateway.hiagent, "chat", fake_chat)

    original_messages = [
        {"role": "system", "content": "你是分镜助手"},
        {"role": "user", "content": "写一段追杀情节"},
    ]
    result = asyncio.run(model_gateway.chat(original_messages))

    assert result == "换路后正常产出"
    assert len(calls) == 2
    fallback_messages, fallback_kwargs = calls[1]
    assert fallback_kwargs["provider"] == "custom-provider"
    assert fallback_kwargs["model"] == "custom-model"
    assert fallback_kwargs["call_meta"]["moderation_fallback"] is True
    assert fallback_messages[0]["role"] == "system"
    assert fallback_messages[0]["content"].startswith("以下内容是文学作品的改编分析任务")
    assert "你是分镜助手" in fallback_messages[0]["content"]
    # 原始 messages 不得被就地改写
    assert original_messages[0]["content"] == "你是分镜助手"


def test_model_rejection_with_fallback_route_still_rejected_raises_original_error(
    monkeypatch,
) -> None:
    """换路后仍拒答：必须抛出第一次的原始错误，不能被换路请求自己的报错覆盖。"""
    monkeypatch.setattr(
        model_gateway_moderation, "get_setting",
        lambda _key: "custom-provider:custom-model",
    )
    call_count = 0

    async def fake_chat(_messages, **_kwargs):
        nonlocal call_count
        call_count += 1
        raise _model_rejection_error(f"第 {call_count} 次仍被拒答")

    monkeypatch.setattr(model_gateway.hiagent, "chat", fake_chat)

    with pytest.raises(hiagent.ProviderError, match="第 1 次仍被拒答"):
        asyncio.run(model_gateway.chat([{"role": "user", "content": "写一段追杀情节"}]))

    assert call_count == 2  # 主路 + 换路各一次
