"""片段身份复核面板『修订本段』的人工修订意见（2026-09-29）：

- ``storyboard_revision_notes.segment_rule_text``：空/纯空白不产出规则，非空转成
  一条完整正面陈述（CLAUDE.md「写完整的正面陈述，不写禁令」）。
- 端到端接线：``regenerate_identity_candidate`` 把 ``revision_notes`` 透传进
  ``_generate_all_segment_prompts``，只落在目标段自己的 ``task_payload["rules"]``
  里——同 ``tests/test_segment_identity_workspace.py::
  test_regenerate_calls_model_only_for_selected_segment`` 同一套夹具与断言手法
  （照抄已验证过的骨架，见该文件『子代理禁止跑全量』同一条纪律的姊妹条款
  「派单必带架构约束」）。
- HTTP 层：请求体新增的 ``revision_notes`` 字段真的被路由转发；超长时触发标准
  请求校验（422，全局 handler 已经是中文公开文案，见 app.main._on_request_
  validation）。
- 全集服装表/道具入场计划持久化：``assemble_adaptation_summary`` 新增
  ``wardrobe_plan_full``/``prop_entrances_full`` 两个 additive 字段（不覆盖既有
  的三态统计 key），``storyboard_identity_regenerate._existing_plan`` 从最近一条
  ``storyboard_pack_adaptation`` 产物里把它们找回。
- 2026-10-01（P0-F 补丁，协调方验收后追加）：``assemble_adaptation_summary`` 同一
  模式补 ``prop_appearance_locks_full``（P0-F 道具外观全集锁定当时漏了这一个），
  ``_existing_plan`` 从留档里恢复；留档是本次改动之前生成的老格式（没有这个 key）
  或根本没有留档时，``_restored_plan_items`` 报 ``prop_appearance_locks_stale=True``，
  ``regenerate_identity_candidate`` 据此给目标段 ``degraded_capabilities`` 追加一条
  可见信号，不静默当成"没有锁定"。
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json

from fastapi.testclient import TestClient
import pytest

from app import db
from app.evidence import repository as evidence_repository
from app.harness.types import EvidenceArtifact
from app.main import app
from app.production.storyboard_beat_causality import assemble_adaptation_summary
from app.production.storyboard_beat_sheet_schemas import (
    _AiBeat, _AiBeatSheetDraft, _AiPropAppearanceLock, _AiPropEntrance, _AiSegmentPlan, _AiWardrobeState,
)
from app.production.storyboard_identity_contract import stamp_identity_contract
from app.production.storyboard_identity_regenerate import (
    _STALE_PROP_LOCK_ADVISORY, _existing_plan, _restored_plan_items, regenerate_identity_candidate,
)
from app.production.storyboard_revision_notes import segment_rule_text
from app.production.storyboard_speech_render import render_segment_speech
from app.schemas import Bible
from app.source_excerpt import SourceSegment
from tests.conftest import SessionTestClient


# ---------------------------------------------------------------------------
# segment_rule_text：空/空白不产出规则，非空转成完整正面陈述
# ---------------------------------------------------------------------------

def test_empty_notes_produce_no_rule():
    assert segment_rule_text("") == []


def test_whitespace_only_notes_produce_no_rule():
    assert segment_rule_text("   \n\t  ") == []


def test_notes_become_one_positive_statement_rule():
    rules = segment_rule_text("  同一个杯子既在桌上又在她手里，去掉桌上那只  ")
    assert len(rules) == 1
    assert "本段修订意见" in rules[0]
    assert "逐条落实到本段画面与动作里" in rules[0]
    assert "与原文冲突时以原文为准" in rules[0]
    assert "degraded_capabilities" in rules[0]
    # 两端空白被裁掉，原始意见逐字保留在规则文本里。
    assert "同一个杯子既在桌上又在她手里，去掉桌上那只" in rules[0]
    assert not rules[0].endswith("  ")


# ---------------------------------------------------------------------------
# assemble_adaptation_summary：新增两个 additive 字段，不覆盖既有三态统计 key
# ---------------------------------------------------------------------------

def test_adaptation_summary_carries_full_wardrobe_and_prop_lists():
    beat_sheet = [_AiBeat(beat_id="B1", summary="咖啡馆见面", segment_indexes=[1])]
    segments_plan = [_AiSegmentPlan(segment_no=1, synopsis="x", source_segment_indexes=[1], beat_ids=["B1"])]
    wardrobe = _AiWardrobeState(identity_id="bible:c1", beat_id="B1", wardrobe="米白色针织开衫", change_reason="首次出场")
    prop = _AiPropEntrance(label="水浸行李箱", beat_id="B1", entrance_description="从屋内拖出到门口")
    draft = _AiBeatSheetDraft(beat_sheet=beat_sheet, segments=segments_plan, wardrobe_plan=[wardrobe], prop_entrances=[prop])
    payload = {"asset_manifest": {"characters": [{"identity_id": "bible:c1", "display_name": "温念"}]}}
    result = assemble_adaptation_summary(
        adaptation_mode="faithful", planned_segment_count=1, beat_draft=draft, dialogue_quotes=[],
        projected_segment_count=None, drop_review=None, wardrobe_recheck=None,
        segments=[SourceSegment(segment_id="s1", text="两人在咖啡馆见面。", start_offset=0, end_offset=9)], payload=payload,
    )
    assert result["wardrobe_plan_full"] == [wardrobe.model_dump(mode="json")]
    assert result["prop_entrances_full"] == [prop.model_dump(mode="json")]
    # 三态统计 key 名字被两个新 key 各自复用了前缀，必须确认没有互相覆盖。
    assert result["wardrobe_plan"] == {"status": "ok", "problem_count": 0}
    assert result["prop_entrances"] == {"status": "ok", "problem_count": 0}


def test_adaptation_summary_carries_full_prop_appearance_locks():
    """2026-10-01 P0-F 补丁：assemble_adaptation_summary 当时漏持久化
    prop_appearance_locks，用与 wardrobe_plan_full/prop_entrances_full 同一个
    additive 字段模式补上。"""
    beat_sheet = [_AiBeat(beat_id="B1", summary="咖啡馆见面", segment_indexes=[1])]
    segments_plan = [_AiSegmentPlan(segment_no=1, synopsis="x", source_segment_indexes=[1], beat_ids=["B1"])]
    lock = _AiPropAppearanceLock(label="水壶", appearance="墨绿色铁皮水壶，壶身有一道浅凹痕", beat_ids=["B1"])
    draft = _AiBeatSheetDraft(beat_sheet=beat_sheet, segments=segments_plan, prop_appearance_locks=[lock])
    payload = {"asset_manifest": {"characters": []}}
    result = assemble_adaptation_summary(
        adaptation_mode="faithful", planned_segment_count=1, beat_draft=draft, dialogue_quotes=[],
        projected_segment_count=None, drop_review=None, wardrobe_recheck=None,
        segments=[SourceSegment(segment_id="s1", text="两人在咖啡馆见面。", start_offset=0, end_offset=9)], payload=payload,
    )
    assert result["prop_appearance_locks_full"] == [lock.model_dump(mode="json")]


# ---------------------------------------------------------------------------
# _restored_plan_items：prop_appearance_locks_stale 可见信号
# ---------------------------------------------------------------------------

def test_restored_plan_items_stale_when_no_adaptation_artifact(fixture):
    conn, _segment, _payload = fixture
    restored = _restored_plan_items(conn, "ep")
    assert restored["prop_appearance_locks"] == []
    assert restored["prop_appearance_locks_stale"] is True


def test_restored_plan_items_stale_when_artifact_predates_the_field(fixture):
    """老格式留档（本次改动之前生成，有 wardrobe_plan_full/prop_entrances_full
    但没有 prop_appearance_locks_full）：不能把"key 不存在"误判成"模型提名了
    零条锁定"，必须报 stale。"""
    conn, _segment, _payload = fixture
    _write_adaptation_artifact(conn, "ep", {
        "adaptation_mode": "faithful", "dropped_source_spans": [],
        "wardrobe_plan_full": [], "prop_entrances_full": [],
    })
    restored = _restored_plan_items(conn, "ep")
    assert restored["prop_appearance_locks"] == []
    assert restored["prop_appearance_locks_stale"] is True


def test_restored_plan_items_not_stale_when_key_present_even_if_empty(fixture):
    """新格式留档即使模型这次确实一条锁定都没提名（key 存在、值是空列表），也不是
    stale——"没有锁定"是合法的三态之一，不该被误报。"""
    conn, _segment, _payload = fixture
    _write_adaptation_artifact(conn, "ep", {
        "adaptation_mode": "faithful", "dropped_source_spans": [],
        "prop_appearance_locks_full": [],
    })
    restored = _restored_plan_items(conn, "ep")
    assert restored["prop_appearance_locks"] == []
    assert restored["prop_appearance_locks_stale"] is False


# ---------------------------------------------------------------------------
# HTTP/regenerate 夹具：一集两段，均为合法 storyboard_pack_segment
# ---------------------------------------------------------------------------

@pytest.fixture
def fixture():
    conn = db.get_conn()
    payload = {
        "prep_pack_version": "2.0.0",
        "asset_manifest": {"characters": [{"identity_id": "bible:孟浩", "display_name": "孟浩", "segment_indexes": [1]}], "scenes": [], "functional_extras": []},
    }
    conn.execute("INSERT INTO projects(id,name,bible_json,created_at) VALUES('p','本地回归','',?)", (db.now(),))
    conn.execute("INSERT INTO chapters(project_id,idx,title,content) VALUES('p',1,'第五章',?)", ('孟浩（OS）：我一定会回来。\n\n山风吹过。',))
    conn.execute("INSERT INTO episodes(id,project_id,episode_no,title,source_chapters,status,screenplay_json,created_at) VALUES('ep','p',5,'第五集','[1]','confirmed',?,?)", (json.dumps(payload), db.now()))
    segment = dict(
        segment_no=1, synopsis="孟浩自述", source_segment_indexes=[1], beat_ids=["B1"],
        beats=[{"beat_id": "B1", "summary": "孟浩自述", "segment_indexes": [1]}], shot_count=2, duration_s=15,
        target_model="seedance_2", degraded_capabilities=[], prompt_text="镜头1：远处山风。{{speech:U01}} 镜头2：山路空寂。",
        dialogue=[dict(utterance_id="U01", speaker_identity_id="bible:孟浩", line="我一定会回来。", source_segment_index=1, delivery="offscreen_voice", delivery_kind="inner_monologue")],
        resources={"characters": [dict(identity_id="bible:孟浩", display_name="孟浩", subject_kind="character", visibility="voice_only")], "scenes": [], "props": []},
    )
    render_segment_speech(segment, dialect="seedance")
    stamp_identity_contract(segment)
    for i in (1, 2):
        current = deepcopy(segment)
        current["segment_no"] = i
        conn.execute(
            "INSERT INTO shots(id,episode_id,shot_no,duration_s,shot_size,camera_move,scene_setting,action_desc,narration,characters,dialogues,source_excerpt,shot_contract_json,adopted_version_id) "
            "VALUES(?, 'ep',?,15,'','','','','','[]','[]',?,?,NULL)",
            (f"s{i}", i, "孟浩（OS）：我一定会回来。", json.dumps({"storyboard_pack_segment": current})),
        )
    conn.commit()
    return conn, segment, payload


def _bible() -> Bible:
    return Bible.model_validate({"world": {"visual_style_canonical": "测试画风"}, "characters": [], "scenes": []})


def _fake_regenerated_candidate(segment: dict) -> dict:
    from app.production.storyboard_pack import _AiStoryboardSegmentDraft

    candidate = deepcopy(segment)
    candidate["resources"]["characters"][0]["visibility"] = "visible"
    candidate["prompt_text"] = candidate.pop("speech_template")
    candidate["continuity_memo"] = {"time_of_day": "白天"}
    # 动作密度软检查要求每一镜都申报关键动作（storyboard_action_density），夹具按 shot_count 逐镜补齐
    candidate["shot_action_beats"] = [
        {"shot_no": n, "key_actions": ["说话"]} for n in range(1, int(candidate.get("shot_count") or 1) + 1)
    ]
    return _AiStoryboardSegmentDraft.model_validate(candidate).model_dump(mode="json")


# ---------------------------------------------------------------------------
# 端到端接线：修订意见只落在目标段的 rules 里
# ---------------------------------------------------------------------------

def test_regenerate_injects_revision_notes_into_target_segment_only(fixture, monkeypatch):
    conn, segment, payload = fixture
    from app.production import storyboard_pack

    requests = []

    async def chat(messages, **kwargs):
        request = json.loads(messages[1]["content"])
        requests.append(request)
        draft = storyboard_pack._AiStoryboardSegmentDraft.model_validate(_fake_regenerated_candidate(segment))
        assert kwargs["validate"](draft) == []
        return draft

    monkeypatch.setattr(storyboard_pack.model_gateway, "chat_structured", chat)
    episode = dict(conn.execute("SELECT * FROM episodes WHERE id='ep'").fetchone())
    result = asyncio.run(regenerate_identity_candidate(
        conn, episode=episode, shot_id="s1", payload=payload, bible=_bible(),
        revision_notes="同一个杯子既在桌上又在她手里，去掉桌上那只",
    ))
    # 只有目标段真正发起了模型调用——另一段（s2）从未被发给模型，修订意见没有
    # 渠道能够波及它。
    assert len(requests) == 1 and requests[0]["segment_no"] == 1
    assert any("同一个杯子既在桌上又在她手里" in rule for rule in requests[0]["rules"])
    assert result["prompt_text"]


def test_regenerate_dialect_instructions_include_prop_visibility_rule(fixture, monkeypatch):
    """必查项（CLAUDE.md 派单，2026-10-01）：「修订本段」单段重生成与整集生成共用同一个
    ``storyboard_segment_chains._task_payload_dialect_instructions``——新增的道具可见性
    规则（``storyboard_prop_visibility``，无条件拼接、不依赖任何提名）必须在这条路径上
    同样出现，不能只在整集生成里生效，否则用户用这条入口修第 1 集第 35/28 段时拿不到
    同一套规则。"""
    conn, segment, payload = fixture
    from app.production import storyboard_pack
    from app.production.storyboard_prop_visibility import SEEDANCE_PROP_VISIBILITY_RULE

    requests = []

    async def chat(messages, **kwargs):
        request = json.loads(messages[1]["content"])
        requests.append(request)
        draft = storyboard_pack._AiStoryboardSegmentDraft.model_validate(_fake_regenerated_candidate(segment))
        assert kwargs["validate"](draft) == []
        return draft

    monkeypatch.setattr(storyboard_pack.model_gateway, "chat_structured", chat)
    episode = dict(conn.execute("SELECT * FROM episodes WHERE id='ep'").fetchone())
    asyncio.run(regenerate_identity_candidate(conn, episode=episode, shot_id="s1", payload=payload, bible=_bible()))
    assert SEEDANCE_PROP_VISIBILITY_RULE in requests[0]["dialect_instructions"]


def test_regenerate_injects_prop_appearance_lock_rule_for_target_segment(fixture, monkeypatch):
    """2026-10-01 P0-F 补丁（协调方验收后追加）：「修订本段」现在能从留档里恢复全集
    道具外观锁定，目标段（beat_ids=["B1"]）命中的锁定必须出现在 task_payload["rules"]
    里，与整集生成时的 ``storyboard_prop_appearance_lock.segment_rule_text`` 同一条
    正面陈述——这正是第 1 集第 5/8/16/17/18/22/28/32/33/34/35 段接下来要用的路径。"""
    conn, segment, payload = fixture
    from app.production import storyboard_pack

    _write_adaptation_artifact(conn, "ep", {
        "adaptation_mode": "faithful", "dropped_source_spans": [],
        "prop_appearance_locks_full": [
            {"label": "水壶", "appearance": "墨绿色铁皮水壶，壶身有一道浅凹痕", "beat_ids": ["B1"]},
        ],
    })
    requests = []

    async def chat(messages, **kwargs):
        request = json.loads(messages[1]["content"])
        requests.append(request)
        draft = storyboard_pack._AiStoryboardSegmentDraft.model_validate(_fake_regenerated_candidate(segment))
        assert kwargs["validate"](draft) == []
        return draft

    monkeypatch.setattr(storyboard_pack.model_gateway, "chat_structured", chat)
    episode = dict(conn.execute("SELECT * FROM episodes WHERE id='ep'").fetchone())
    asyncio.run(regenerate_identity_candidate(conn, episode=episode, shot_id="s1", payload=payload, bible=_bible()))
    assert any("水壶" in rule and "墨绿色铁皮水壶，壶身有一道浅凹痕" in rule for rule in requests[0]["rules"])


def test_regenerate_surfaces_stale_advisory_when_no_adaptation_artifact(fixture, monkeypatch):
    """没有任何整集生成留档（本集还没有/已失效）：「修订本段」必须让用户看见——
    目标段 degraded_capabilities 里要带上可见信号，不能默默当成"这件道具没有锁定"。"""
    conn, segment, payload = fixture
    from app.production import storyboard_pack

    async def chat(messages, **kwargs):
        draft = storyboard_pack._AiStoryboardSegmentDraft.model_validate(_fake_regenerated_candidate(segment))
        assert kwargs["validate"](draft) == []
        return draft

    monkeypatch.setattr(storyboard_pack.model_gateway, "chat_structured", chat)
    episode = dict(conn.execute("SELECT * FROM episodes WHERE id='ep'").fetchone())
    result = asyncio.run(regenerate_identity_candidate(conn, episode=episode, shot_id="s1", payload=payload, bible=_bible()))
    assert _STALE_PROP_LOCK_ADVISORY in result["degraded_capabilities"]


def test_regenerate_no_stale_advisory_when_locks_restored(fixture, monkeypatch):
    """新格式留档存在（哪怕这次模型确实一条锁定都没提名），不应该报 stale。"""
    conn, segment, payload = fixture
    from app.production import storyboard_pack

    _write_adaptation_artifact(conn, "ep", {
        "adaptation_mode": "faithful", "dropped_source_spans": [], "prop_appearance_locks_full": [],
    })

    async def chat(messages, **kwargs):
        draft = storyboard_pack._AiStoryboardSegmentDraft.model_validate(_fake_regenerated_candidate(segment))
        assert kwargs["validate"](draft) == []
        return draft

    monkeypatch.setattr(storyboard_pack.model_gateway, "chat_structured", chat)
    episode = dict(conn.execute("SELECT * FROM episodes WHERE id='ep'").fetchone())
    result = asyncio.run(regenerate_identity_candidate(conn, episode=episode, shot_id="s1", payload=payload, bible=_bible()))
    assert _STALE_PROP_LOCK_ADVISORY not in result["degraded_capabilities"]


def test_regenerate_with_blank_revision_notes_adds_no_rule(fixture, monkeypatch):
    conn, segment, payload = fixture
    from app.production import storyboard_pack

    requests = []

    async def chat(messages, **kwargs):
        request = json.loads(messages[1]["content"])
        requests.append(request)
        draft = storyboard_pack._AiStoryboardSegmentDraft.model_validate(_fake_regenerated_candidate(segment))
        assert kwargs["validate"](draft) == []
        return draft

    monkeypatch.setattr(storyboard_pack.model_gateway, "chat_structured", chat)
    episode = dict(conn.execute("SELECT * FROM episodes WHERE id='ep'").fetchone())
    asyncio.run(regenerate_identity_candidate(
        conn, episode=episode, shot_id="s1", payload=payload, bible=_bible(), revision_notes="   ",
    ))
    assert not any("本段修订意见" in rule for rule in requests[0]["rules"])


# ---------------------------------------------------------------------------
# HTTP 层：字段被路由转发；超长触发标准请求校验（中文公开文案）
# ---------------------------------------------------------------------------

def test_regenerate_http_forwards_revision_notes(fixture, monkeypatch):
    conn, _segment, _payload = fixture
    from app.domain.storyboard_ops import identity_review as routes

    received = {}

    async def fake_candidate(*args, **kwargs):
        received.update(kwargs)
        return {"checked": True}

    monkeypatch.setattr(routes, "regenerate_identity_candidate", fake_candidate)
    client = SessionTestClient(TestClient(app))
    baseline = client.get("/api/shots/s1/identity-review").json()["baseline"]
    response = client.post(
        "/api/shots/s1/identity-review/regenerate",
        json={"baseline": baseline, "revision_notes": "杯子重复了，去掉桌上那只"},
    )
    assert response.status_code == 200, response.text
    assert received["revision_notes"] == "杯子重复了，去掉桌上那只"


def test_regenerate_http_defaults_revision_notes_to_empty_string(fixture, monkeypatch):
    """不传这个字段是今天的默认行为，必须继续成立（老前端/老请求不受影响）。"""
    conn, _segment, _payload = fixture
    from app.domain.storyboard_ops import identity_review as routes

    received = {}

    async def fake_candidate(*args, **kwargs):
        received.update(kwargs)
        return {"checked": True}

    monkeypatch.setattr(routes, "regenerate_identity_candidate", fake_candidate)
    client = SessionTestClient(TestClient(app))
    baseline = client.get("/api/shots/s1/identity-review").json()["baseline"]
    response = client.post("/api/shots/s1/identity-review/regenerate", json={"baseline": baseline})
    assert response.status_code == 200, response.text
    assert received["revision_notes"] == ""


def test_regenerate_http_rejects_overlong_revision_notes(fixture):
    conn, _segment, _payload = fixture
    client = SessionTestClient(TestClient(app))
    baseline = client.get("/api/shots/s1/identity-review").json()["baseline"]
    response = client.post(
        "/api/shots/s1/identity-review/regenerate",
        json={"baseline": baseline, "revision_notes": "过长" * 501},
    )
    assert response.status_code == 422, response.text
    assert "请求参数不合法" in response.text


# ---------------------------------------------------------------------------
# 计划持久化：_existing_plan 从最近一条 storyboard_pack_adaptation 产物里
# 把全集服装表/道具入场计划找回
# ---------------------------------------------------------------------------

def _write_adaptation_artifact(conn, episode_id: str, content: dict, *, status: str = "validated") -> None:
    evidence_repository.create_artifact(
        EvidenceArtifact(type="storyboard_pack_adaptation", scope_type="episode", scope_id=episode_id, status=status, trust_level="T2", content=content),
        conn=conn, commit=True,
    )


def test_existing_plan_restores_wardrobe_and_prop_entrances(fixture):
    conn, segment, _payload = fixture
    _write_adaptation_artifact(conn, "ep", {
        "adaptation_mode": "faithful", "dropped_source_spans": [],
        "wardrobe_plan_full": [{"identity_id": "bible:孟浩", "beat_id": "B1", "wardrobe": "米白色针织开衫", "change_reason": "首次出场"}],
        "prop_entrances_full": [{"label": "水浸行李箱", "beat_id": "B1", "entrance_description": "从屋内拖出到门口"}],
    })
    draft = _existing_plan([segment, dict(segment, segment_no=2)], conn, "ep")
    assert [w.identity_id for w in draft.wardrobe_plan] == ["bible:孟浩"]
    assert draft.wardrobe_plan[0].wardrobe == "米白色针织开衫"
    assert [p.label for p in draft.prop_entrances] == ["水浸行李箱"]


def test_existing_plan_defaults_to_empty_without_adaptation_artifact(fixture):
    conn, segment, _payload = fixture
    draft = _existing_plan([segment], conn, "ep")
    assert draft.wardrobe_plan == []
    assert draft.prop_entrances == []


def test_existing_plan_ignores_non_validated_adaptation_artifact(fixture):
    """门禁按『最高 version 且 validated』判定当前留档——非 validated（例如
    candidate/stale）不能被当成当前计划，否则会把还没通过校验或已作废的一代
    悄悄带进单段重生成。"""
    conn, segment, _payload = fixture
    _write_adaptation_artifact(conn, "ep", {
        "adaptation_mode": "faithful", "dropped_source_spans": [],
        "wardrobe_plan_full": [{"identity_id": "bible:孟浩", "beat_id": "B1", "wardrobe": "米白色针织开衫", "change_reason": "首次出场"}],
    }, status="candidate")
    draft = _existing_plan([segment], conn, "ep")
    assert draft.wardrobe_plan == []
