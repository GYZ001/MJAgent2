"""文本模型审核拒答换路（原 WS1b，EP-05 第三阶段收编进统一策略）。

``app.harness.model_gateway.chat()`` 收到结构化 MODEL_REJECTION
（``app.hiagent.ProviderFailure.model_rejection()``，对外码 LLM-REJECTED）时，
不再读固定的 ``settings.text_moderation_fallback_route``，改用
``app.models_registry.routing.call_with_failover`` 按 ``text:default``
优先级链换路；system 提示前仍然挂固定框架语（见
``app.harness.model_gateway_moderation.framed_moderation_messages``，这部分
行为未变）；链路耗尽/未配置任何绑定都必须原样抛出第一次的错误，不得被换路
请求自己的报错覆盖（CLAUDE.md「界面承诺必须与实际行为一致」）。

打桩方式与旧版一致：直接
``monkeypatch.setattr(model_gateway.hiagent, "chat", fake_chat)``——
``model_gateway.hiagent`` 与 ``model_gateway_failover.hiagent`` 是同一个
``app.hiagent`` 模块对象（两边都写 ``from app import hiagent`` 后按属性调用
``hiagent.chat(...)``），打一次桩两条调用路径都生效。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import hiagent
from app.db import get_conn, set_setting
from app.harness import model_gateway
from app.models_registry import bindings, store


def _model_rejection_error(message: str) -> hiagent.ProviderError:
    return hiagent.ProviderError(
        message, raw=message, failure=hiagent.ProviderFailure.model_rejection(),
    )


def _seed_fallback_model(model_id: str = "model_fb") -> None:
    """两条 text:default 绑定：priority=0 是"主用"（``provider=None`` 时
    ``hiagent.chat`` 内部默认路由与本测试的第一次调用命中同一条），priority=1
    才是真正的换路目的地——只挂一条会让"主用"与"换路目的地"变成同一个模型
    （EP-05 第三阶段联调时踩过：单绑定场景下 exclude 集合会把唯一候选也排除
    掉，链路耗尽，见交付报告"如何验的"）。"""
    set_setting("custom_models", json.dumps([
        {"id": "model_primary", "provider": "custom:model_primary", "model": "text-primary",
         "kinds": ["text"], "builtin": False, "protocol": "openai",
         "base_url": "https://primary.example.test/v1"},
        {"id": model_id, "provider": f"custom:{model_id}", "model": "text-fb",
         "kinds": ["text"], "builtin": False, "protocol": "openai",
         "base_url": "https://fb.example.test/v1"},
    ], ensure_ascii=False))
    store.put_credential("model_primary", base_url="https://primary.example.test/v1", api_key="sk-p", rotated_by="t")
    store.put_credential(model_id, base_url="https://fb.example.test/v1", api_key="sk-fb", rotated_by="t")
    bindings.upsert_binding(purpose="text:default", model_id="model_primary", priority=0)
    bindings.upsert_binding(purpose="text:default", model_id=model_id, priority=1)


def _reroute_audit_rows() -> list:
    return get_conn().execute(
        "SELECT * FROM operation_audit WHERE event='models_registry.route_failover' ORDER BY ts"
    ).fetchall()


def test_model_rejection_without_any_binding_raises_original_error(monkeypatch) -> None:
    """没有任何 text:default 绑定：不得猜一个供应商，原样抛出，只发生一次真实请求。"""
    calls: list[list[dict[str, str]]] = []

    async def fake_chat(messages, **_kwargs):
        calls.append(messages)
        raise _model_rejection_error("供应商内容审核已明确拒绝本次请求")

    monkeypatch.setattr(model_gateway.hiagent, "chat", fake_chat)

    with pytest.raises(hiagent.ProviderError, match="供应商内容审核已明确拒绝本次请求"):
        asyncio.run(model_gateway.chat([{"role": "user", "content": "写一段追杀情节"}]))

    assert len(calls) == 1
    assert _reroute_audit_rows() == []


def test_model_rejection_with_binding_second_call_succeeds(monkeypatch) -> None:
    """配置了 text:default 优先级链：第一次结构化拒答后按链路换路重试一次并
    成功——system 提示前挂固定框架语，provider/model 按候选整体切换（配套
    传递，不拆开），且落一条换路审计。"""
    _seed_fallback_model()
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
    assert fallback_kwargs["provider"] == "custom:model_fb"
    assert fallback_kwargs["model"] == "text-fb"
    assert fallback_kwargs["call_meta"]["moderation_fallback"] is True
    assert fallback_messages[0]["role"] == "system"
    assert fallback_messages[0]["content"].startswith("以下内容是文学作品的改编分析任务")
    assert "你是分镜助手" in fallback_messages[0]["content"]
    # 原始 messages 不得被就地改写
    assert original_messages[0]["content"] == "你是分镜助手"
    audit_rows = _reroute_audit_rows()
    assert len(audit_rows) == 1
    assert audit_rows[0]["error_code"] == "content_rejected"


def test_model_rejection_with_binding_still_rejected_raises_original_error(monkeypatch) -> None:
    """换路后仍拒答（链路耗尽）：必须抛出第一次的原始错误，不能被换路请求
    自己的报错覆盖。"""
    _seed_fallback_model()
    call_count = 0

    async def fake_chat(_messages, **_kwargs):
        nonlocal call_count
        call_count += 1
        raise _model_rejection_error(f"第 {call_count} 次仍被拒答")

    monkeypatch.setattr(model_gateway.hiagent, "chat", fake_chat)

    with pytest.raises(hiagent.ProviderError, match="第 1 次仍被拒答"):
        asyncio.run(model_gateway.chat([{"role": "user", "content": "写一段追杀情节"}]))

    assert call_count == 2  # 主路 + 换路各一次（链路只有一条候选，耗尽即止）
