"""规划窗口输出不合法时带证据有界重问（第 14 轮第 28 集：少一个引号整集 AI_PLAN_SCHEMA_INVALID）。"""
from __future__ import annotations

import asyncio

from app.video_plan import planner_window_call as pwc


def _run(responses: list[str], monkeypatch):
    calls: list[tuple[list[dict], dict]] = []

    async def fake_chat(messages, *, temperature, max_tokens, call_meta):
        calls.append((messages, call_meta))
        return responses[len(calls) - 1]

    monkeypatch.setattr(pwc.model_gateway, "chat", fake_chat)
    monkeypatch.setattr(pwc, "cached_window_is_valid", lambda text: text.startswith("{\"shots\""))
    out = asyncio.run(pwc.planner_window_response(
        [{"role": "user", "content": "规划"}], temperature=0.1, max_tokens=4096,
        call_meta={"stage": "episode_video_mode_plan", "operation_id": "op_video_plan_abc"},
    ))
    return out, calls


def test_invalid_then_valid_retries_once_with_evidence_and_new_operation_id(monkeypatch) -> None:
    bad = '{"shots":[{"shot_id":"shot_360a45648b8d,"relations":{}}]}'.replace('{"shots"', '{ "shots"')
    good = '{"shots":[{"shot_id":"shot_360a45648b8d"}]}'
    out, calls = _run([bad, good], monkeypatch)
    assert out == good and len(calls) == 2
    retry_messages, retry_meta = calls[1]
    assert retry_messages[-1]["role"] == "user" and "shot_360a45648b8d," in retry_messages[-1]["content"]
    assert retry_messages[-2] == {"role": "assistant", "content": bad}
    assert retry_meta["operation_id"] != "op_video_plan_abc" and retry_meta["planner_retry_no"] == 1


def test_still_invalid_after_budget_returns_last_response_for_caller_to_reject(monkeypatch) -> None:
    out, calls = _run(["x1", "x2", "x3", "x4"], monkeypatch)
    assert out == "x3" and len(calls) == pwc.PLANNER_WINDOW_ATTEMPTS


def test_valid_first_response_is_not_retried(monkeypatch) -> None:
    out, calls = _run(['{"shots":[]}'], monkeypatch)
    assert out == '{"shots":[]}' and len(calls) == 1 and calls[0][1]["operation_id"] == "op_video_plan_abc"
