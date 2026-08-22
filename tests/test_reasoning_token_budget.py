"""思考型模型的 completion 预算回归。

生产根因：各业务阶段算出来的 ``max_tokens`` 一律是「答案要多大」，但供应商把
reasoning token 计入**同一份** completion 预算。实测场次语义审查阶段 reasoning
占 completion 的中位数 97%，于是一个规模正确的答案预算仍会以
``finish_reason=length`` 截断，并以不可重试的 ProviderError 杀死整集。

这些用例覆盖的是**这一类**问题，而不是某一个阶段的常量：
凡是经过 ``text_request_token_limits`` 的调用都必须带上思考预留，
并且顶到模型上限仍被截断时只做一次有界抬升。
"""
from __future__ import annotations

import pytest

from app import config, hiagent


_LIMITS = {
    "context_window_tokens": 131072,
    "max_output_tokens": 32768,
    "token_limits_source": "test",
}


def _patch_model(monkeypatch, *, max_output: int = 32768) -> None:
    monkeypatch.setattr(hiagent, "active_provider", lambda _kind: "hiagent")
    monkeypatch.setattr(
        hiagent, "active_model", lambda *_args, **_kwargs: "thinking-model"
    )
    monkeypatch.setattr(
        hiagent,
        "active_model_token_limits",
        lambda *_args, **_kwargs: {**_LIMITS, "max_output_tokens": max_output},
    )
    monkeypatch.setattr(hiagent, "get_setting", lambda _key: "")


# 实测分位（2767 次成功调用的 usage.completion_tokens_details.reasoning_tokens）：
# p90=4436 / p95=6849 / p99=10859 / max=21959。
OBSERVED_REASONING_P99 = 10859


@pytest.mark.parametrize("answer_budget", [2048, 4096, 6144, 8192, 16384])
def test_answer_budget_always_reserves_room_for_reasoning(
    monkeypatch, answer_budget: int
) -> None:
    """任何阶段的答案预算都必须留出实测 p99 的思考开销。"""
    _patch_model(monkeypatch)
    _provider, _model, effective = hiagent.text_request_token_limits(
        requested_max_tokens=answer_budget,
    )
    assert effective > answer_budget
    assert effective - answer_budget >= OBSERVED_REASONING_P99


def test_reasoning_reserve_never_exceeds_model_output_limit(monkeypatch) -> None:
    """预留只抬高上限，绝不越过模型自身能输出的上限。"""
    _patch_model(monkeypatch, max_output=4096)
    _provider, _model, effective = hiagent.text_request_token_limits(
        requested_max_tokens=2048,
    )
    assert effective == 4096

    _patch_model(monkeypatch, max_output=32768)
    _provider, _model, effective = hiagent.text_request_token_limits(
        requested_max_tokens=64000,
    )
    assert effective == 32768


def test_reasoning_reserve_is_operator_tunable(monkeypatch) -> None:
    _patch_model(monkeypatch)
    monkeypatch.setattr(
        hiagent,
        "get_setting",
        lambda key: "0" if key == "text_reasoning_token_reserve" else "",
    )
    _provider, _model, effective = hiagent.text_request_token_limits(
        requested_max_tokens=2048,
    )
    assert effective == 2048


def test_default_reserve_covers_the_observed_reasoning_distribution() -> None:
    assert config.TEXT_REASONING_TOKEN_RESERVE >= OBSERVED_REASONING_P99


def _install_fake_provider(monkeypatch, responses: list[dict]):
    """让 hiagent.chat 打到一个可控的假供应商，并记录每次 payload。"""
    payloads: list[dict] = []
    monkeypatch.setattr(
        hiagent,
        "_model_connection",
        lambda *_args, **_kwargs: ("https://example.invalid", {}),
    )
    monkeypatch.setattr(
        hiagent, "_cached_successful_provider_response", lambda *_a, **_k: None
    )
    monkeypatch.setattr(hiagent, "_require_cached_replay_or_raise", lambda *_a, **_k: None)

    async def fake_request(_client, _url, payload, **_kwargs):
        payloads.append(payload)
        return responses[min(len(payloads) - 1, len(responses) - 1)]

    monkeypatch.setattr(hiagent, "_plain_chat_request", fake_request)
    return payloads


def _truncated_response() -> dict:
    return {
        "choices": [
            {
                "finish_reason": "length",
                "message": {"content": "", "reasoning": "想了很久"},
            }
        ],
        "usage": {
            "completion_tokens": 2049,
            "completion_tokens_details": {"reasoning_tokens": 2048},
        },
    }


def _ok_response() -> dict:
    return {
        "choices": [
            {"finish_reason": "stop", "message": {"content": '{"ok":true}'}}
        ],
        "usage": {"completion_tokens": 120},
    }


@pytest.mark.asyncio
async def test_truncation_escalates_once_to_the_model_output_limit(
    monkeypatch,
) -> None:
    """截断没有交付任何答案，允许把同一确定性请求抬到模型上限再发一次。"""
    _patch_model(monkeypatch)
    payloads = _install_fake_provider(
        monkeypatch, [_truncated_response(), _ok_response()]
    )

    result = await hiagent.chat(
        [{"role": "user", "content": "写一段"}],
        max_tokens=2048,
        call_meta={"operation_id": "op-truncation-escalation"},
    )

    assert result == '{"ok":true}'
    assert len(payloads) == 2
    # 第一次带思考预留，第二次直接顶到模型输出上限。
    assert payloads[0]["max_tokens"] == 2048 + config.TEXT_REASONING_TOKEN_RESERVE
    assert payloads[1]["max_tokens"] == 32768


@pytest.mark.asyncio
async def test_truncation_at_the_output_limit_still_fails(monkeypatch) -> None:
    """顶到模型上限仍截断时不再无限抬升，照常按供应商失败上报。"""
    _patch_model(monkeypatch)
    payloads = _install_fake_provider(
        monkeypatch, [_truncated_response(), _truncated_response()]
    )

    with pytest.raises(hiagent.ProviderError) as excinfo:
        await hiagent.chat(
            [{"role": "user", "content": "写一段"}],
            max_tokens=2048,
            call_meta={"operation_id": "op-truncation-exhausted"},
        )

    assert (
        excinfo.value.failure_kind
        == hiagent.ProviderFailureKind.OUTPUT_TRUNCATED.value
    )
    assert len(payloads) == 2
