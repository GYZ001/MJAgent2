"""app.domain.storyboard_ops.prop_continuity_review_api -- POST /rewrite 要求
确认段号子集（2026-10-05 复核修正第二轮）：调用方必须回传当前复核结果里
希望重写的段号，必须是服务端重新算出的待重写段号集合的**子集**（不再要求
完全相等——用户现在可以只重写一部分段），不是子集就拒绝，防止盲发/过期
预览触发超出调用方认知的重写；``kinds`` 非法取值走标准请求校验拒绝（422）。
真实数据库 + model_gateway.chat_structured 打桩，不打真实供应商往返；直接
调用路由函数（不经 TestClient），与仓库内同类「薄 API 装配层」测试同一先例。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.domain.storyboard_ops import prop_continuity_review_api as api
from app.harness import model_gateway
from tests.test_prop_continuity_review import fixture  # noqa: F401 -- 复用同一套夹具，不重开第二份


def _only_segment_two_flagged_chat():
    async def chat(messages, **kwargs):
        request = json.loads(messages[1]["content"])
        if request["segment_no"] == 1:
            return kwargs["model_type"](violations=[])
        return kwargs["model_type"](violations=[{
            "kind": "prop_state_regression", "shot_label": "镜头1", "prop_name": "插座与插头",
            "quote": "墙根插座上插着白色插头", "previous_quote": "插座两孔空着", "fix": "改回已拔下",
        }])

    return chat


def test_rewrite_rejects_segment_not_in_server_flagged_set(fixture, monkeypatch):
    """调用方确认的段号（第 1 段，调用方以为有问题）与服务端此刻算出的待重写
    段号（只有第 2 段）不是子集关系——拒绝，不静默按调用方的理解执行。"""
    conn, episode, _payload = fixture
    monkeypatch.setattr(model_gateway, "chat_structured", _only_segment_two_flagged_chat())
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(api.prop_continuity_rewrite(episode["id"], api.PropContinuityRewriteBody(confirmed_segment_nos=[1])))
    assert exc_info.value.status_code == 409
    assert "2" in exc_info.value.detail


def test_rewrite_accepts_empty_confirmed_subset_as_a_no_op(fixture, monkeypatch):
    """空集合是任何集合的子集——合法输入，等于「这次什么都不重写」，不是错误。"""
    conn, episode, _payload = fixture
    monkeypatch.setattr(model_gateway, "chat_structured", _only_segment_two_flagged_chat())
    result = asyncio.run(api.prop_continuity_rewrite(episode["id"], api.PropContinuityRewriteBody(confirmed_segment_nos=[])))
    assert all(not s["rewritten"] for s in result["segments"])


def test_rewrite_kinds_with_unknown_value_is_rejected_by_request_validation():
    """``kinds`` 取值必须是 11 类判据之一，否则在请求体解析阶段就被拒绝
    （FastAPI 把 pydantic 校验错误转成 422），不走到业务逻辑。"""
    with pytest.raises(ValidationError, match="不支持的违规类别"):
        api.PropContinuityRewriteBody(confirmed_segment_nos=[2], kinds=["not_a_real_kind"])


def test_rewrite_proceeds_when_confirmed_segment_nos_is_a_subset(fixture, monkeypatch):
    conn, episode, _payload = fixture
    #: 第 2 段会被复核两次——GET 式预览（必须报违规，才有东西可确认）与局部
    #: 修改保存后的复核（必须报干净，证明替换真的解决了问题）；用调用次数区分
    #: 这两次，不是只区分段号。
    segment_2_review_calls = {"n": 0}

    async def chat(messages, **kwargs):
        request = json.loads(messages[1]["content"])
        if "rules" in request:  # 复核调用（初次预览或局部修改后复核）
            if request["segment_no"] == 1:
                return kwargs["model_type"](violations=[])
            segment_2_review_calls["n"] += 1
            if segment_2_review_calls["n"] == 1:
                return kwargs["model_type"](violations=[{
                    "kind": "prop_state_regression", "shot_label": "镜头1", "prop_name": "插座与插头",
                    "quote": "墙根插座上插着白色插头", "previous_quote": "插座两孔空着", "fix": "改回已拔下",
                }])
            return kwargs["model_type"](violations=[])
        # 最小替换提案调用：只针对那句矛盾的原文给出一条替换。
        return kwargs["model_type"](replacements=[{"quote": "墙根插座上插着白色插头", "replacement": "插座两孔空着，插头已被拔下放在地上"}])

    monkeypatch.setattr(model_gateway, "chat_structured", chat)
    result = asyncio.run(api.prop_continuity_rewrite(episode["id"], api.PropContinuityRewriteBody(confirmed_segment_nos=[2])))
    assert result["segments"][1]["rewritten"] is True
    assert result["segments"][1]["error"] is None
    assert result["segments"][1]["remaining_violations"] == [], "局部替换已验证不再违规，remaining_violations 应为空"
    stored = json.loads(conn.execute("SELECT shot_contract_json FROM shots WHERE id='s2'").fetchone()["shot_contract_json"])
    assert "插座两孔空着" in stored["storyboard_pack_segment"]["prompt_text"]


def test_rewrite_kinds_filter_only_touches_confirmed_category(fixture, monkeypatch):
    """GET 预览里第 2 段同时带 prop_state_regression（文本可局部改）与
    action_density（结构类，需人工）两条——只确认重写、只传
    kinds={"prop_state_regression"} 时，action_density 必须原样留在
    ``needs_manual_revision_violations`` 里，不被触碰。"""
    conn, episode, _payload = fixture

    async def chat(messages, **kwargs):
        request = json.loads(messages[1]["content"])
        if "rules" in request:
            if request["segment_no"] == 1:
                return kwargs["model_type"](violations=[])
            return kwargs["model_type"](violations=[
                {"kind": "prop_state_regression", "shot_label": "镜头1", "prop_name": "插座与插头",
                 "quote": "墙根插座上插着白色插头", "previous_quote": "插座两孔空着", "fix": "改回已拔下"},
                {"kind": "action_density", "shot_label": "镜头1", "quote": "墙根插座上插着白色插头", "fix": "拆成两镜"},
            ])
        return kwargs["model_type"](replacements=[{"quote": "墙根插座上插着白色插头", "replacement": "插座两孔空着"}])

    monkeypatch.setattr(model_gateway, "chat_structured", chat)
    body = api.PropContinuityRewriteBody(confirmed_segment_nos=[2], kinds=["prop_state_regression"])
    result = asyncio.run(api.prop_continuity_rewrite(episode["id"], body))
    segment2 = result["segments"][1]
    assert segment2["rewritten"] is True
    manual_kinds = [v["kind"] for v in segment2["needs_manual_revision_violations"]]
    assert manual_kinds == ["action_density"], "结构类违规必须原样留痕，不随局部修改被清空"
