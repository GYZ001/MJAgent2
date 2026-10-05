"""app.domain.storyboard_ops.prop_continuity_review_api -- POST /rewrite 要求
显式确认段号（2026-10-04 复核修正第 7 条）：调用方必须回传当前复核结果里的
待重写段号，与服务端重新算出的结果不一致就拒绝，防止盲发/过期预览触发
批量重写。真实数据库 + model_gateway.chat_structured 打桩，不打真实供应商
往返；直接调用路由函数（不经 TestClient），与仓库内同类「薄 API 装配层」
测试同一先例。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException

from app.domain.storyboard_ops import prop_continuity_review_api as api
from app.harness import model_gateway
from app.production.storyboard_pack import _AiStoryboardSegmentDraft
from tests.test_prop_continuity_review import fixture  # noqa: F401 -- 复用同一套夹具，不重开第二份


def test_rewrite_rejects_mismatched_confirmed_segment_nos(fixture, monkeypatch):
    """调用方确认的段号（空列表，以为没有要重写的段）与服务端此刻算出的
    待重写段号（第 2 段）不一致——拒绝，不静默按调用方的理解执行。"""
    conn, episode, _payload = fixture

    async def chat(messages, **kwargs):
        request = json.loads(messages[1]["content"])
        if request["segment_no"] == 1:
            return kwargs["model_type"](violations=[])
        return kwargs["model_type"](violations=[{
            "kind": "prop_state_regression", "shot_label": "镜头1", "prop_name": "插座与插头",
            "quote": "墙根插座上插着白色插头", "previous_quote": "插座两孔空着", "fix": "改回已拔下",
        }])

    monkeypatch.setattr(model_gateway, "chat_structured", chat)
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(api.prop_continuity_rewrite(episode["id"], api.PropContinuityRewriteBody(confirmed_segment_nos=[])))
    assert exc_info.value.status_code == 409
    assert "2" in exc_info.value.detail


def test_rewrite_proceeds_when_confirmed_segment_nos_match(fixture, monkeypatch):
    conn, episode, _payload = fixture
    #: 第 2 段会被复核两次——GET 式预览（必须报违规，才有东西可确认）与重写
    #: 保存后的复核（必须报干净，证明重写真的解决了问题）；用调用次数区分
    #: 这两次，不是只区分段号。
    segment_2_review_calls = {"n": 0}

    async def chat(messages, **kwargs):
        request = json.loads(messages[1]["content"])
        if "task" not in request:  # 复核调用（初次预览或重写后复核）
            if request["segment_no"] == 1:
                return kwargs["model_type"](violations=[])
            segment_2_review_calls["n"] += 1
            if segment_2_review_calls["n"] == 1:
                return kwargs["model_type"](violations=[{
                    "kind": "prop_state_regression", "shot_label": "镜头1", "prop_name": "插座与插头",
                    "quote": "墙根插座上插着白色插头", "previous_quote": "插座两孔空着", "fix": "改回已拔下",
                }])
            return kwargs["model_type"](violations=[])
        candidate = dict(
            segment_no=2, synopsis="测试段", source_segment_indexes=[1], beat_ids=["B1"],
            beats=[{"beat_id": "B1", "summary": "测试", "segment_indexes": [1]}],
            shot_count=2, duration_s=15, target_model="seedance_2", degraded_capabilities=[],
            prompt_text="镜头1：墙根插座上插着插头已拔下、插座两孔空着。镜头2：她转身离开。", dialogue=[],
            resources={"characters": [], "scenes": [], "props": []},
            continuity_memo={"time_of_day": "白天", "props": []},
            shot_action_beats=[{"shot_no": n, "key_actions": ["动作"]} for n in (1, 2)],
        )
        return _AiStoryboardSegmentDraft.model_validate(candidate)

    monkeypatch.setattr(model_gateway, "chat_structured", chat)
    result = asyncio.run(api.prop_continuity_rewrite(episode["id"], api.PropContinuityRewriteBody(confirmed_segment_nos=[2])))
    assert result["segments"][1]["rewritten"] is True
    assert result["segments"][1]["error"] is None
