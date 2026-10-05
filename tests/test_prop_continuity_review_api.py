"""app.domain.storyboard_ops.prop_continuity_review_api -- 2026-10-05 快照化
修正第二轮：GET 预览把结果存成快照并返回 ``snapshot_id``；POST /rewrite
必填 ``snapshot_id`` + ``confirmed_segment_nos``，不再重新调用复核模型（见
``prop_continuity_review`` 模块 docstring「为什么 POST 不再重新调用复核
模型」）。``confirmed_segment_nos`` 必须是快照里「有已核验违规」段号集合的
子集，否则 409；快照不存在/不属于该集 404；快照过期 409；``kinds`` 非法
取值走标准请求校验拒绝（422）。真实数据库 + model_gateway.chat_structured
打桩，不打真实供应商往返；直接调用路由函数（不经 TestClient），与仓库内
同类「薄 API 装配层」测试同一先例。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.db import now
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


def _preview(episode_id: str) -> dict:
    """走 GET 路由产出一份真实快照，供 POST 测试复用——不手搭快照结构，
    与生产调用方走同一条路径。"""
    return asyncio.run(api.prop_continuity_review(episode_id))


def test_get_preview_returns_snapshot_id_and_expiry(fixture, monkeypatch):
    conn, episode, _payload = fixture
    monkeypatch.setattr(model_gateway, "chat_structured", _only_segment_two_flagged_chat())
    result = _preview(episode["id"])
    assert result["snapshot_id"]
    assert result["snapshot_expires_at"] > now()
    assert [s["segment_no"] for s in result["segments"]] == [1, 2]


def test_rewrite_rejects_segment_not_in_snapshot_flagged_set(fixture, monkeypatch):
    """调用方确认的段号（第 1 段，调用方以为有问题）与快照里真正待重写
    的段号（只有第 2 段）不是子集关系——拒绝，不静默按调用方的理解执行。"""
    conn, episode, _payload = fixture
    monkeypatch.setattr(model_gateway, "chat_structured", _only_segment_two_flagged_chat())
    preview = _preview(episode["id"])
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(api.prop_continuity_rewrite(
            episode["id"],
            api.PropContinuityRewriteBody(snapshot_id=preview["snapshot_id"], confirmed_segment_nos=[1]),
        ))
    assert exc_info.value.status_code == 409
    assert "2" in exc_info.value.detail


def test_rewrite_accepts_empty_confirmed_subset_as_a_no_op(fixture, monkeypatch):
    """空集合是任何集合的子集——合法输入，等于「这次什么都不重写」，不是错误。"""
    conn, episode, _payload = fixture
    monkeypatch.setattr(model_gateway, "chat_structured", _only_segment_two_flagged_chat())
    preview = _preview(episode["id"])
    result = asyncio.run(api.prop_continuity_rewrite(
        episode["id"], api.PropContinuityRewriteBody(snapshot_id=preview["snapshot_id"], confirmed_segment_nos=[]),
    ))
    assert all(not s["rewritten"] for s in result["segments"])


def test_rewrite_missing_snapshot_id_is_rejected_by_request_validation():
    """``snapshot_id`` 没有默认值——漏传直接 422，不会被当成「用服务端此刻
    状态」的隐式含义。"""
    with pytest.raises(ValidationError):
        api.PropContinuityRewriteBody(confirmed_segment_nos=[2])


def test_rewrite_unknown_snapshot_id_returns_404(fixture):
    conn, episode, _payload = fixture
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(api.prop_continuity_rewrite(
            episode["id"], api.PropContinuityRewriteBody(snapshot_id="pcrsnap_不存在", confirmed_segment_nos=[]),
        ))
    assert exc_info.value.status_code == 404


def test_rewrite_expired_snapshot_returns_409(fixture, monkeypatch):
    conn, episode, _payload = fixture
    monkeypatch.setattr(model_gateway, "chat_structured", _only_segment_two_flagged_chat())
    preview = _preview(episode["id"])
    # 直接把这条快照的 expires_at 改到过去，模拟超过有效期——不等真实 24 小时。
    conn.execute(
        "UPDATE prop_continuity_review_snapshots SET expires_at=? WHERE id=?",
        (now() - 1, preview["snapshot_id"]),
    )
    conn.commit()
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(api.prop_continuity_rewrite(
            episode["id"],
            api.PropContinuityRewriteBody(snapshot_id=preview["snapshot_id"], confirmed_segment_nos=[2]),
        ))
    assert exc_info.value.status_code == 409
    assert "重新" in exc_info.value.detail


def test_rewrite_kinds_with_unknown_value_is_rejected_by_request_validation():
    """``kinds`` 取值必须是 11 类判据之一，否则在请求体解析阶段就被拒绝
    （FastAPI 把 pydantic 校验错误转成 422），不走到业务逻辑。"""
    with pytest.raises(ValidationError, match="不支持的违规类别"):
        api.PropContinuityRewriteBody(snapshot_id="x", confirmed_segment_nos=[2], kinds=["not_a_real_kind"])


def test_rewrite_does_not_call_review_model_again(fixture, monkeypatch):
    """POST 不再重新调用复核函数——这是本次修正的核心：复核模型有随机性，
    重新复核一遍可能算出与快照不同的「待重写集合」，409 子集校验会因此在
    真实使用中经常失败（详见模块 docstring）。"""
    conn, episode, _payload = fixture
    monkeypatch.setattr(model_gateway, "chat_structured", _only_segment_two_flagged_chat())
    preview = _preview(episode["id"])

    async def must_not_be_called(*args, **kwargs):
        raise AssertionError("POST /rewrite 不应该重新调用整集复核")

    monkeypatch.setattr(api, "review_existing_episode_segments", must_not_be_called)
    result = asyncio.run(api.prop_continuity_rewrite(
        episode["id"], api.PropContinuityRewriteBody(snapshot_id=preview["snapshot_id"], confirmed_segment_nos=[]),
    ))
    assert result["segments"]


def test_rewrite_skips_segment_whose_prompt_text_changed_after_preview(fixture, monkeypatch):
    """预览之后、确认之前，正文被别的操作改过——必须跳过并给出可见原因，
    不能拿快照里过期的违规清单去瞎改当前正文。"""
    conn, episode, _payload = fixture
    monkeypatch.setattr(model_gateway, "chat_structured", _only_segment_two_flagged_chat())
    preview = _preview(episode["id"])
    stored = json.loads(conn.execute("SELECT shot_contract_json FROM shots WHERE id='s2'").fetchone()["shot_contract_json"])
    stored["storyboard_pack_segment"]["prompt_text"] = "镜头1：完全不同的新正文。镜头2：她转身离开。"
    conn.execute("UPDATE shots SET shot_contract_json=? WHERE id='s2'", (json.dumps(stored),))
    conn.commit()

    async def must_not_be_called(*args, **kwargs):
        raise AssertionError("正文已漂移的段不应该触发任何模型调用")

    monkeypatch.setattr(model_gateway, "chat_structured", must_not_be_called)
    result = asyncio.run(api.prop_continuity_rewrite(
        episode["id"], api.PropContinuityRewriteBody(snapshot_id=preview["snapshot_id"], confirmed_segment_nos=[2]),
    ))
    segment2 = result["segments"][1]
    assert segment2["rewritten"] is False
    assert segment2["skip_reason"] == "分镜在预览后已变化，请重新预览"


def test_rewrite_proceeds_when_confirmed_segment_nos_is_a_subset(fixture, monkeypatch):
    conn, episode, _payload = fixture
    monkeypatch.setattr(model_gateway, "chat_structured", _only_segment_two_flagged_chat())
    preview = _preview(episode["id"])

    async def chat(messages, **kwargs):
        request = json.loads(messages[1]["content"])
        if "rules" in request:  # 局部修改后的复核调用：确认新正文不再违规
            return kwargs["model_type"](violations=[])
        # 最小替换提案调用：只针对那句矛盾的原文给出一条替换。
        return kwargs["model_type"](replacements=[{"quote": "墙根插座上插着白色插头", "replacement": "插座两孔空着，插头已被拔下放在地上"}])

    monkeypatch.setattr(model_gateway, "chat_structured", chat)
    result = asyncio.run(api.prop_continuity_rewrite(
        episode["id"], api.PropContinuityRewriteBody(snapshot_id=preview["snapshot_id"], confirmed_segment_nos=[2]),
    ))
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

    async def preview_chat(messages, **kwargs):
        request = json.loads(messages[1]["content"])
        if request["segment_no"] == 1:
            return kwargs["model_type"](violations=[])
        return kwargs["model_type"](violations=[
            {"kind": "prop_state_regression", "shot_label": "镜头1", "prop_name": "插座与插头",
             "quote": "墙根插座上插着白色插头", "previous_quote": "插座两孔空着", "fix": "改回已拔下"},
            {"kind": "action_density", "shot_label": "镜头1", "quote": "墙根插座上插着白色插头", "fix": "拆成两镜"},
        ])

    monkeypatch.setattr(model_gateway, "chat_structured", preview_chat)
    preview = _preview(episode["id"])

    async def chat(messages, **kwargs):
        request = json.loads(messages[1]["content"])
        if "rules" in request:
            return kwargs["model_type"](violations=[])
        return kwargs["model_type"](replacements=[{"quote": "墙根插座上插着白色插头", "replacement": "插座两孔空着"}])

    monkeypatch.setattr(model_gateway, "chat_structured", chat)
    body = api.PropContinuityRewriteBody(snapshot_id=preview["snapshot_id"], confirmed_segment_nos=[2], kinds=["prop_state_regression"])
    result = asyncio.run(api.prop_continuity_rewrite(episode["id"], body))
    segment2 = result["segments"][1]
    assert segment2["rewritten"] is True
    manual_kinds = [v["kind"] for v in segment2["needs_manual_revision_violations"]]
    assert manual_kinds == ["action_density"], "结构类违规必须原样留痕，不随局部修改被清空"
