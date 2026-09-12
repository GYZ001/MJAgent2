"""timeout / rate_limited / server_error 跨模型换路（EP-05 第四阶段）。

``app.harness.model_gateway.chat()`` 现有同候选退避循环即将放弃（不可重放/
不可重试/重试预算耗尽）之前，改接
``app.harness.model_gateway_failover.technical_failure_failover`` 对这三类
结构化失败发起一次跨模型换路，与第三阶段已经接好的 content_rejected 换路
（``content_rejection_failover``）是两个独立调用点。

打桩方式与 ``test_model_gateway_moderation_fallback.py`` 一致：直接
``monkeypatch.setattr(model_gateway.hiagent, "chat", fake_chat)``。

三类失败的构造直接用 ``hiagent.ProviderError`` 的真实字段组合（贴近
``hiagent._classify_http_error``/``_transport_provider_error`` 实际产出的
形状），不是随手糊的 duck-type 假异常——``retryable``/``delivery_state``/
``raw`` 三者的组合决定了 ``model_gateway.chat`` 自己的
``replayable`` 判定是否为 False（同候选不可重放，立即放弃转入跨模型换路），
所以这里选用的组合都经过验证会在第一次尝试就触发放弃分支，测试不需要打桩
``asyncio.sleep`` 或压低 ``TEXT_PROVIDER_MAX_RETRIES``。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import hiagent
from app.db import get_conn, set_setting
from app.harness import model_gateway
from app.models_registry import bindings, store


def _timeout_error(msg: str = "网关超时，未确认结果是否送达") -> hiagent.ProviderError:
    """贴近 ``hiagent._transport_provider_error`` 的 request_outcome_unknown 形状：
    ``delivery_state="responded"``（不是 "unknown"）让 ``replay_safe_stream_
    interruption`` 判 False，配合非 JSON ``raw`` 让 ``provider_envelope_
    unprocessed`` 也判 False——同候选不可重放，立即放弃转入跨模型换路。"""
    return hiagent.ProviderError(
        msg, retryable=True, failure_kind="request_outcome_unknown",
        delivery_state="responded", timeout_phase="read", raw="上游超时",
    )


def _rate_limited_error(msg: str = "网关限流（HTTP 429）") -> hiagent.ProviderError:
    """贴近 ``hiagent._classify_http_error(429, body)`` 的真实产出形状。"""
    return hiagent.ProviderError(
        msg, retryable=True, failure_kind="rate_limited",
        delivery_state="responded", raw="限流了，请稍后再试",
    )


def _server_error(msg: str = "网关/上游故障（HTTP 503）") -> hiagent.ProviderError:
    """贴近 ``hiagent._classify_http_error(503, body)`` 的真实产出形状。"""
    return hiagent.ProviderError(
        msg, retryable=True, failure_kind="upstream_unavailable",
        delivery_state="responded", raw="上游服务暂时不可用",
    )


def _seed_two_candidate_chain() -> None:
    set_setting("custom_models", json.dumps([
        {"id": "model_primary", "provider": "custom:model_primary", "model": "text-primary",
         "kinds": ["text"], "builtin": False, "protocol": "openai",
         "base_url": "https://primary.example.test/v1"},
        {"id": "model_fb", "provider": "custom:model_fb", "model": "text-fb",
         "kinds": ["text"], "builtin": False, "protocol": "openai",
         "base_url": "https://fb.example.test/v1"},
    ], ensure_ascii=False))
    store.put_credential("model_primary", base_url="https://primary.example.test/v1", api_key="sk-p", rotated_by="t")
    store.put_credential("model_fb", base_url="https://fb.example.test/v1", api_key="sk-fb", rotated_by="t")
    bindings.upsert_binding(purpose="text:default", model_id="model_primary", priority=0)
    bindings.upsert_binding(purpose="text:default", model_id="model_fb", priority=1)


def _seed_single_binding_no_fallback() -> None:
    """只有 priority=0，没有 priority>=1——本轮的安全边界场景：不许有多余供应商
    调用，也不许改变最终抛出的异常。"""
    set_setting("custom_models", json.dumps([
        {"id": "model_primary", "provider": "custom:model_primary", "model": "text-primary",
         "kinds": ["text"], "builtin": False, "protocol": "openai",
         "base_url": "https://primary.example.test/v1"},
    ], ensure_ascii=False))
    store.put_credential("model_primary", base_url="https://primary.example.test/v1", api_key="sk-p", rotated_by="t")
    bindings.upsert_binding(purpose="text:default", model_id="model_primary", priority=0)


def _reroute_audit_rows() -> list:
    return get_conn().execute(
        "SELECT * FROM operation_audit WHERE event='models_registry.route_failover' ORDER BY ts"
    ).fetchall()


@pytest.mark.parametrize("make_error,expected_category", [
    (_timeout_error, "timeout"),
    (_rate_limited_error, "rate_limited"),
    (_server_error, "server_error"),
])
def test_technical_failure_with_binding_switches_and_succeeds(monkeypatch, make_error, expected_category) -> None:
    _seed_two_candidate_chain()
    calls: list[tuple[list[dict[str, str]], dict]] = []

    async def fake_chat(messages, **kwargs):
        calls.append((messages, kwargs))
        if len(calls) == 1:
            raise make_error()
        return "换路后正常产出"

    monkeypatch.setattr(model_gateway.hiagent, "chat", fake_chat)

    result = asyncio.run(model_gateway.chat([{"role": "user", "content": "写一段场景"}]))

    assert result == "换路后正常产出"
    assert len(calls) == 2  # 主用一次 + 换路一次，不做同候选重试（预算相加不相乘）
    fallback_kwargs = calls[1][1]
    assert fallback_kwargs["provider"] == "custom:model_fb"
    assert fallback_kwargs["model"] == "text-fb"
    assert fallback_kwargs["call_meta"]["technical_failover"] is True

    audit_rows = _reroute_audit_rows()
    assert len(audit_rows) == 1
    assert audit_rows[0]["target"] == "model_primary"
    assert audit_rows[0]["error_code"] == expected_category


@pytest.mark.parametrize("make_error", [_timeout_error, _rate_limited_error, _server_error])
def test_technical_failure_without_fallback_raises_original_error_unchanged(monkeypatch, make_error) -> None:
    """没有 priority>=1 候选：不得产生第二次真实供应商调用，且抛出的必须是这次
    真实失败的原始异常，不能被换路链路耗尽自己的 LookupError 覆盖——这是本轮
    "未配置 fallback 绑定的环境行为零影响"的直接验收用例。"""
    _seed_single_binding_no_fallback()
    calls: list[list[dict[str, str]]] = []
    err = make_error()

    async def fake_chat(messages, **_kwargs):
        calls.append(messages)
        raise err

    monkeypatch.setattr(model_gateway.hiagent, "chat", fake_chat)

    with pytest.raises(hiagent.ProviderError) as excinfo:
        asyncio.run(model_gateway.chat([{"role": "user", "content": "写一段场景"}]))

    assert excinfo.value is err  # 原样抛出同一个异常对象，不是换路链路自己新造的错误
    assert len(calls) == 1  # 唯一一次真实供应商调用，没有多余请求


def test_content_rejected_failure_is_not_double_processed_by_technical_failover(monkeypatch) -> None:
    """content_rejected 已经由 content_rejection_failover 单独处理；技术失败换路
    必须在分类阶段就跳过它，不能把同一条候选链再消耗一遍（两次落两条审计）。"""
    _seed_two_candidate_chain()
    calls: list[str] = []

    async def fake_chat(_messages, **kwargs):
        calls.append(kwargs.get("provider") or "primary")
        raise hiagent.ProviderError(
            "供应商内容审核已明确拒绝本次请求",
            raw="拒绝", failure=hiagent.ProviderFailure.model_rejection(),
        )

    monkeypatch.setattr(model_gateway.hiagent, "chat", fake_chat)

    with pytest.raises(hiagent.ProviderError, match="供应商内容审核已明确拒绝本次请求"):
        asyncio.run(model_gateway.chat([{"role": "user", "content": "写一段追杀情节"}]))

    # 主用 1 次 + content_rejection_failover 内部对唯一候选 model_fb 的 1 次
    # 尝试，恰好 2 次：若 technical_failure_failover 没有正确跳过 content_
    # rejected（bug 场景），它会用自己独立的 exclude 集合（只排除 primary，
    # 不知道 model_fb 已经在 content_rejection_failover 里试过）对 model_fb
    # 再发起一次真实调用，calls 会变成 3——这个断言足以抓住那类回归。
    assert len(calls) == 2
    # 两条审计都应该是 content_rejection_failover 自己产生的 content_rejected
    # 记录（循环外手写的"主用失败" attempt_no=0 + call_with_failover 内部对
    # model_fb 自身失败的记录），不应该出现第三条 technical_failover 分类的行。
    audit_rows = _reroute_audit_rows()
    assert len(audit_rows) == 2
    assert {row["error_code"] for row in audit_rows} == {"content_rejected"}


def test_unclassifiable_failure_does_not_attempt_reroute(monkeypatch) -> None:
    """没有可辨认的结构化失败字段形状（``routing.classify_exception`` 返回
    ``None``）：不发起任何换路查询、不落审计，原样抛出。"""
    _seed_two_candidate_chain()
    calls: list[str] = []
    err = hiagent.ProviderError(
        "无法分类的失败", failure=hiagent.ProviderFailure.technical("", retryable=False),
    )

    async def fake_chat(_messages, **_kwargs):
        calls.append("x")
        raise err

    monkeypatch.setattr(model_gateway.hiagent, "chat", fake_chat)

    with pytest.raises(hiagent.ProviderError) as excinfo:
        asyncio.run(model_gateway.chat([{"role": "user", "content": "x"}]))

    assert excinfo.value is err
    assert len(calls) == 1
    assert _reroute_audit_rows() == []


def test_technical_failure_chain_of_three_tries_each_candidate_once() -> None:
    """三条候选链：预算是"每个候选各一次"相加，不是同候选重试次数相乘候选数——
    直接调用 call_with_failover 验证（model_gateway.chat 端到端已由上面的用例
    覆盖），这里补一个三候选场景证明换路不会在某个候选上原地重试。"""
    from app.models_registry import routing

    set_setting("custom_models", json.dumps([
        {"id": f"model_{tag}", "provider": f"custom:model_{tag}", "model": f"text-{tag}",
         "kinds": ["text"], "builtin": False, "protocol": "openai",
         "base_url": f"https://{tag}.example.test/v1"}
        for tag in ("a", "b", "c")
    ], ensure_ascii=False))
    for tag in ("a", "b", "c"):
        bindings.upsert_binding(purpose="text:default", model_id=f"model_{tag}", priority={"a": 0, "b": 1, "c": 2}[tag])

    calls: list[str] = []

    async def fn(candidate: routing.ResolvedModel) -> str:
        calls.append(candidate.model_id)
        if candidate.model_id == "model_c":
            return "done"
        raise hiagent.ProviderError(
            "svc down", retryable=True, failure_kind="upstream_unavailable",
            delivery_state="responded", raw="不是 JSON 信封",
        )

    result = asyncio.run(routing.call_with_failover("text:default", fn, request_id="req-budget"))

    assert result == "done"
    assert calls == ["model_a", "model_b", "model_c"]  # 各恰好一次，非指数放大
    assert len(_reroute_audit_rows()) == 2
