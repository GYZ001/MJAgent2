"""app.domain.storyboard_ops.prop_continuity_review -- 存量分镜按现行复核
规则批量核查（P0 第 4/5 项，2026-10-04，用户反馈《顾念长安》EP1 第 1→2 段
插座/插头驱动）。真实数据库 + model_gateway.chat_structured 打桩，不打真实
供应商往返。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import db
from app.domain.storyboard_ops import prop_continuity_review as batch
from app.harness import model_gateway
from app.production.storyboard_identity_contract import stamp_identity_contract
from app.production.storyboard_pack import _AiStoryboardSegmentDraft
from app.production.storyboard_prose_review import ProseViolation


def _segment(segment_no: int, prompt_text: str, *, props=(), continuity_props=()) -> dict:
    segment = dict(
        segment_no=segment_no, synopsis="测试段", source_segment_indexes=[1], beat_ids=["B1"],
        beats=[{"beat_id": "B1", "summary": "测试", "segment_indexes": [1]}],
        shot_count=2, duration_s=15, target_model="seedance_2", degraded_capabilities=[],
        prompt_text=prompt_text, dialogue=[],
        resources={"characters": [], "scenes": [], "props": list(props)},
        continuity_memo={"time_of_day": "白天", "props": list(continuity_props)},
    )
    stamp_identity_contract(segment)
    return segment


@pytest.fixture
def fixture():
    conn = db.get_conn()
    payload = {"prep_pack_version": "2.0.0", "asset_manifest": {"characters": [], "scenes": [], "functional_extras": []}}
    conn.execute("INSERT INTO projects(id,name,bible_json,created_at) VALUES('p','道具续接回归','',?)", (db.now(),))
    conn.execute("INSERT INTO chapters(project_id,idx,title,content) VALUES('p',1,'第一章',?)", ("温念拔下插头。墙根插座上插着白色插头。",))
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,title,source_chapters,status,screenplay_json,created_at) VALUES('ep','p',1,'第一集','[1]','confirmed',?,?)",
        (json.dumps(payload), db.now()),
    )
    seg1 = _segment(1, "镜头1：她伸手拔下插头。镜头2：插头落在地上。", continuity_props=[{"name": "插座与插头", "location": "插座两孔空着", "state": "已拔下"}])
    seg2 = _segment(2, "镜头1：墙根插座上插着白色插头。镜头2：她转身离开。")
    for i, segment in ((1, seg1), (2, seg2)):
        conn.execute(
            "INSERT INTO shots(id,episode_id,shot_no,duration_s,shot_size,camera_move,scene_setting,action_desc,narration,characters,dialogues,source_excerpt,shot_contract_json,adopted_version_id) "
            "VALUES(?, 'ep',?,15,'','','','','','[]','[]','',?,?)",
            (f"s{i}", i, json.dumps({"storyboard_pack_segment": segment}), f"v{i}"),
        )
        conn.execute(
            "INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,video_path,created_at) VALUES(?,?,1,?,?,'succeeded','',?)",
            (f"v{i}", f"s{i}", segment["prompt_text"], f"old-{i}", db.now()),
        )
    conn.commit()
    episode = dict(conn.execute("SELECT * FROM episodes WHERE id='ep'").fetchone())
    return conn, episode, payload


def _bible():
    from app.schemas import Bible

    return Bible.model_validate({"characters": [], "world": {"era": "", "genre": "", "visual_style_canonical": "测试画风"}})


# ---------------------------------------------------------------------------
# review_existing_episode_segments：只读复核
# ---------------------------------------------------------------------------

def test_review_flags_prop_state_regression_on_segment_two(fixture, monkeypatch):
    conn, episode, _payload = fixture

    async def chat(messages, **kwargs):
        request = json.loads(messages[1]["content"])
        if request["segment_no"] == 1:
            return kwargs["model_type"](violations=[])
        return kwargs["model_type"](violations=[ProseViolation(
            kind="prop_state_regression", shot_label="镜头1", prop_name="插座与插头",
            quote="墙根插座上插着白色插头", previous_quote="插座两孔空着", fix="改回插头已拔下、插座两孔空着",
        )])

    monkeypatch.setattr(model_gateway, "chat_structured", chat)
    outcomes = asyncio.run(batch.review_existing_episode_segments(conn, episode=episode, bible=_bible()))
    assert [o.segment_no for o in outcomes] == [1, 2]
    assert outcomes[0].violations == []
    assert len(outcomes[1].violations) == 1
    assert outcomes[1].violations[0].kind == "prop_state_regression"
    assert outcomes[1].rewritten is False and outcomes[1].artifact_id is None


def test_review_does_not_call_model_gateway_in_a_way_that_writes_to_db(fixture, monkeypatch):
    """纯复核不应该触碰 shots/shot_versions——哪怕复核发现了违规。"""
    conn, episode, _payload = fixture

    def before():
        return [dict(r) for r in conn.execute("SELECT * FROM shots ORDER BY shot_no")]

    async def chat(messages, **kwargs):
        return kwargs["model_type"](violations=[ProseViolation(kind="negated_action", quote="x", fix="x")])

    monkeypatch.setattr(model_gateway, "chat_structured", chat)
    snapshot = before()
    asyncio.run(batch.review_existing_episode_segments(conn, episode=episode, bible=_bible()))
    assert before() == snapshot


def test_review_rejects_legacy_episode_without_storyboard_pack_segment(fixture):
    conn, episode, _payload = fixture
    conn.execute("UPDATE shots SET shot_contract_json='{}' WHERE id='s2'")
    conn.commit()
    with pytest.raises(ValueError, match="旧式分镜"):
        asyncio.run(batch.review_existing_episode_segments(conn, episode=episode, bible=_bible()))


# ---------------------------------------------------------------------------
# rewrite_flagged_segments：复核后按「修订本段」语义重写并保存
# ---------------------------------------------------------------------------

def test_rewrite_skips_segments_without_violations(fixture, monkeypatch):
    conn, episode, payload = fixture
    outcomes = [batch.SegmentReviewOutcome(segment_no=1, shot_id="s1", violations=[]), batch.SegmentReviewOutcome(segment_no=2, shot_id="s2", violations=[])]

    async def must_not_be_called(*args, **kwargs):
        raise AssertionError("没有违规的段不应该触发任何模型调用")

    monkeypatch.setattr(model_gateway, "chat_structured", must_not_be_called)
    result = asyncio.run(batch.rewrite_flagged_segments(conn, episode=episode, payload=payload, bible=_bible(), outcomes=outcomes))
    assert all(not o.rewritten and o.artifact_id is None and o.error is None for o in result)


def test_rewrite_saves_fixed_segment_and_keeps_old_video_until_success(fixture, monkeypatch):
    conn, episode, payload = fixture
    violation = ProseViolation(
        kind="prop_state_regression", shot_label="镜头1", prop_name="插座与插头",
        quote="墙根插座上插着白色插头", previous_quote="插座两孔空着", fix="改回插头已拔下、插座两孔空着",
    )
    outcomes = [batch.SegmentReviewOutcome(segment_no=2, shot_id="s2", violations=[violation])]

    async def chat(messages, **kwargs):
        request = json.loads(messages[1]["content"])
        if "task" not in request:  # 重写后的复核调用：确认新正文不再违规
            return kwargs["model_type"](violations=[])
        candidate = _segment(2, "镜头1：墙根插座上插着插头已拔下、插座两孔空着。镜头2：她转身离开。")
        candidate["shot_action_beats"] = [{"shot_no": n, "key_actions": ["动作"]} for n in (1, 2)]
        draft = _AiStoryboardSegmentDraft.model_validate(candidate)
        assert "改回插头已拔下" in json.dumps(request["rules"], ensure_ascii=False)
        assert kwargs["validate"](draft) == []
        return draft

    monkeypatch.setattr(model_gateway, "chat_structured", chat)
    before_v2 = dict(conn.execute("SELECT * FROM shot_versions WHERE id='v2'").fetchone())
    result = asyncio.run(batch.rewrite_flagged_segments(conn, episode=episode, payload=payload, bible=_bible(), outcomes=outcomes))
    assert result[0].rewritten is True and result[0].artifact_id and result[0].error is None
    assert result[0].remaining_violations == [], "重写已验证不再违规，remaining_violations 应为空"
    after_v2 = dict(conn.execute("SELECT * FROM shot_versions WHERE id='v2'").fetchone())
    assert after_v2["status"] == "succeeded", "旧视频继续采用，直到新版本生成成功——不因本次修订被撤销"
    assert after_v2["video_path"] == before_v2["video_path"] and after_v2["prompt_text"] == before_v2["prompt_text"]
    stored = json.loads(conn.execute("SELECT shot_contract_json FROM shots WHERE id='s2'").fetchone()["shot_contract_json"])
    assert "插头已拔下" in stored["storyboard_pack_segment"]["prompt_text"]


def test_rewrite_records_remaining_violation_when_rewrite_does_not_fix_it(fixture, monkeypatch):
    """重写保存成功（``rewritten=True``）不等于问题真的解决了——复核仍核验
    通过的违规必须写进 ``remaining_violations``，不能只看 rewritten 就当没事。"""
    conn, episode, payload = fixture
    violation = ProseViolation(
        kind="prop_state_regression", shot_label="镜头1", prop_name="插座与插头",
        quote="墙根插座上插着白色插头", previous_quote="插座两孔空着", fix="改回插头已拔下、插座两孔空着",
    )
    outcomes = [batch.SegmentReviewOutcome(segment_no=2, shot_id="s2", violations=[violation])]

    async def chat(messages, **kwargs):
        request = json.loads(messages[1]["content"])
        if "task" not in request:  # 重写后的复核调用：模型发现问题依然存在
            return kwargs["model_type"](violations=[ProseViolation(
                kind="prop_state_regression", shot_label="镜头1", prop_name="插座与插头",
                quote="墙根插座上插着白色插头", previous_quote="已拔下", fix="改回插头已拔下、插座两孔空着",
            )])
        candidate = _segment(2, "镜头1：墙根插座上插着白色插头，光线昏暗。镜头2：她转身离开。")  # 没有真的改
        candidate["shot_action_beats"] = [{"shot_no": n, "key_actions": ["动作"]} for n in (1, 2)]
        return _AiStoryboardSegmentDraft.model_validate(candidate)

    monkeypatch.setattr(model_gateway, "chat_structured", chat)
    result = asyncio.run(batch.rewrite_flagged_segments(conn, episode=episode, payload=payload, bible=_bible(), outcomes=outcomes))
    assert result[0].rewritten is True and result[0].error is None
    assert len(result[0].remaining_violations) == 1
    assert result[0].remaining_violations[0].kind == "prop_state_regression"


def test_rewrite_records_error_on_one_segment_and_still_processes_the_next(fixture, monkeypatch):
    """第 2 段的模型调用失败不能让第 3 段连试都不被试——批量重写的单段失败
    隔离，必须用真的有「其余段落」的场景验证，不能只放一个段落就断言异常
    往外抛（那只证明了会中断，不是不中断）。"""
    conn, episode, payload = fixture
    seg3 = _segment(3, "镜头1：她走出房间，光线明亮。镜头2：远处传来脚步声。")
    seg3["shot_action_beats"] = [{"shot_no": n, "key_actions": ["动作"]} for n in (1, 2)]
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s,shot_size,camera_move,scene_setting,action_desc,narration,characters,dialogues,source_excerpt,shot_contract_json,adopted_version_id) "
        "VALUES('s3','ep',3,15,'','','','','','[]','[]','',?,'v3')",
        (json.dumps({"storyboard_pack_segment": seg3}),),
    )
    conn.execute(
        "INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,video_path,created_at) VALUES('v3','s3',1,?,'old-3','succeeded','',?)",
        (seg3["prompt_text"], db.now()),
    )
    conn.commit()
    violation2 = ProseViolation(kind="prop_state_regression", prop_name="插座与插头", quote="墙根插座上插着白色插头", previous_quote="插座两孔空着", fix="x")
    violation3 = ProseViolation(kind="negated_action", quote="她没有回头", fix="改成正面描述")
    outcomes = [
        batch.SegmentReviewOutcome(segment_no=2, shot_id="s2", violations=[violation2]),
        batch.SegmentReviewOutcome(segment_no=3, shot_id="s3", violations=[violation3]),
    ]
    attempted_segment_nos = []

    async def chat(messages, **kwargs):
        request = json.loads(messages[1]["content"])
        attempted_segment_nos.append(request["segment_no"])
        if request["segment_no"] == 2:
            raise RuntimeError("模拟供应商 500")
        if "task" not in request:  # 第 3 段重写后的复核调用：确认没有残留违规
            return kwargs["model_type"](violations=[])
        candidate = _segment(3, "镜头1：她走出房间，光线明亮，正面朝向镜头。镜头2：远处传来脚步声。")
        candidate["shot_action_beats"] = [{"shot_no": n, "key_actions": ["动作"]} for n in (1, 2)]
        return _AiStoryboardSegmentDraft.model_validate(candidate)

    monkeypatch.setattr(model_gateway, "chat_structured", chat)
    result = asyncio.run(batch.rewrite_flagged_segments(conn, episode=episode, payload=payload, bible=_bible(), outcomes=outcomes))
    assert attempted_segment_nos[0] == 2, "第 2 段必须真的被尝试过，不是被跳过"
    assert 3 in attempted_segment_nos, "第 2 段失败后，第 3 段仍然要被尝试——不中断其余段落"
    assert result[0].error is not None and "模拟" in result[0].error and result[0].rewritten is False
    assert result[1].rewritten is True and result[1].error is None and result[1].remaining_violations == []
