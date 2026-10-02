"""片段修订的真实数据库、HTTP、队列边界与独立连接回归。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import sqlite3
from threading import Barrier

from fastapi.testclient import TestClient
import pytest

from app import db
from app.auth.sessions import create_session
from app.domain.storyboard_ops import identity_workspace as workspace
from app.main import app
from app.media_exec.fences import ReviewDependencyFence
from app.media_exec.identity_fence import assert_identity_revision
from app.production.storyboard_identity_contract import identity_contract_fingerprint, stamp_identity_contract
from app.production.storyboard_identity_regenerate import regenerate_identity_candidate
from app.production.storyboard_pack import _AiStoryboardSegmentDraft
from app.production.storyboard_speech_render import render_segment_speech
from app.schemas import Bible
from app.video_modes.prop_references import resolve_segment_prop_manifest_entries
from tests.conftest import SessionTestClient


@pytest.fixture
def fixture(tmp_path):
    conn = db.get_conn()
    payload = {"prep_pack_version":"2.0.0", "asset_manifest":{"characters":[{"identity_id":"bible:孟浩","display_name":"孟浩","segment_indexes":[1]}],"scenes":[],"functional_extras":[]}}
    conn.execute("INSERT INTO projects(id,name,bible_json,created_at) VALUES('p','本地回归','{}',?)", (db.now(),))
    conn.execute("INSERT INTO chapters(project_id,idx,title,content) VALUES('p',1,'第五章',?)", ('孟浩（OS）：我一定会回来。\n\n山风吹过。',))
    conn.execute("INSERT INTO episodes(id,project_id,episode_no,title,source_chapters,status,screenplay_json,created_at) VALUES('ep','p',5,'第五集','[1]','confirmed',?,?)", (json.dumps(payload),db.now()))
    segment = dict(segment_no=1,synopsis="孟浩自述",source_segment_indexes=[1],beat_ids=["B1"],
                   beats=[{"beat_id":"B1","summary":"孟浩自述","segment_indexes":[1]}],shot_count=2,duration_s=15,
                   target_model="seedance_2",degraded_capabilities=[],prompt_text="镜头1：远处山风。{{speech:U01}} 镜头2：山路空寂。",
                   dialogue=[dict(utterance_id="U01",speaker_identity_id="bible:孟浩",line="我一定会回来。",source_segment_index=1,delivery="offscreen_voice",delivery_kind="inner_monologue")],
                   resources={"characters":[dict(identity_id="bible:孟浩",display_name="孟浩",subject_kind="character",visibility="voice_only")],"scenes":[],"props":[]})
    render_segment_speech(segment,dialect="seedance")
    stamp_identity_contract(segment)
    paths = []
    for i in (1,2):
        current = deepcopy(segment)
        current["segment_no"] = i
        path = tmp_path / f"original-{i}.mp4"
        path.write_bytes(b"original-video-must-survive")
        paths.append(path)
        conn.execute("INSERT INTO shots(id,episode_id,shot_no,duration_s,shot_size,camera_move,scene_setting,action_desc,narration,characters,dialogues,source_excerpt,shot_contract_json,adopted_version_id) VALUES(?, 'ep',?,15,'','','','','','[]','[]',?,?,?)", (f"s{i}",i,'孟浩（OS）：我一定会回来。',json.dumps({"storyboard_pack_segment":current}),f"v{i}"))
        conn.execute("INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,video_path,created_at) VALUES(?,?,1,?,?,'succeeded',?,?)", (f"v{i}",f"s{i}",current["prompt_text"],f"old-{i}",str(path),db.now()))
    conn.commit()
    return conn, segment, payload, paths


def candidate_of(segment):
    candidate = deepcopy(segment)
    candidate["resources"]["characters"][0]["visibility"] = "visible"
    candidate["speech_template"] = "镜头1：@孟浩 望向山路。{{speech:U01}} 镜头2：山路空寂。"
    return candidate


def read_independent(query, args=()):
    with sqlite3.connect(db.DB_PATH) as other:
        other.row_factory = sqlite3.Row
        return [dict(row) for row in other.execute(query,args)]


def test_preview_is_read_only_and_apply_retains_history_and_sibling(fixture):
    conn, segment, _, paths = fixture
    before = read_independent("SELECT * FROM shots")
    candidate = candidate_of(segment)
    preview = workspace.prepare_identity_candidate(conn,shot_id="s1",candidate=candidate)
    assert read_independent("SELECT * FROM shots") == before
    assert "内心独白（孟浩）" in preview["prompt_text"]
    result = workspace.save_identity_candidate(conn,shot_id="s1",baseline=identity_contract_fingerprint(segment),candidate=candidate)
    after = read_independent("SELECT * FROM shots")
    assert after[1] == before[1]
    assert after[0]["adopted_version_id"] is None
    assert json.loads(after[0]["characters"]) == ["孟浩"]
    assert result["history_versions_preserved"] == 1
    versions = read_independent("SELECT id,status,video_path FROM shot_versions ORDER BY id")
    assert [(v["id"],v["status"]) for v in versions] == [("v1","stale"),("v2","succeeded")]
    assert all(path.read_bytes() == b"original-video-must-survive" for path in paths)
    assert read_independent("SELECT status FROM artifacts WHERE id=?",(result["artifact_id"],))[0]["status"] == "approved"


def test_apply_adopts_candidate_resources_props_scenes_not_pinned_to_original(fixture):
    """真实回归 ep_a3c61162b4ce EP1 段28：「修订本段」重写出 8 件道具（含「水泡坏的
    行李箱」），apply 返回 200 后合同 resources.props 仍是旧的 3 件——根因是
    ``prepare_identity_candidate`` 曾把 resources.scenes/props 强制钉回 original，
    丢弃候选里分镜模型重写的内容；continuity_memo/degraded_capabilities 同一根因。
    候选刻意不触碰 characters/dialogue，排除人物可见性变化带来的定妆照告警噪音。"""
    conn, segment, _, _ = fixture
    candidate = deepcopy(segment)
    candidate["resources"]["props"] = [
        {"label": "水泡坏的行李箱", "description": "香槟金铝框行李箱，边角泡水发黑"},
        {"label": "小木星星", "description": "沿用既有道具"},
    ]
    candidate["resources"]["scenes"] = [{"scene_id": "scene:出租屋", "scene_reference_id": None, "description": "分镜模型新增的场景"}]
    candidate["continuity_memo"] = {"time_of_day": "夜晚"}
    candidate["degraded_capabilities"] = ["[STORYBOARD_PROP_APPEARANCE_LOCK_STALE_ADAPTATION][未拦截] 测试占位告警"]

    prepared = workspace.prepare_identity_candidate(conn, shot_id="s1", candidate=candidate)
    assert prepared["resources"]["props"] == candidate["resources"]["props"]
    assert prepared["resources"]["scenes"] == candidate["resources"]["scenes"]
    assert prepared["continuity_memo"] == {"time_of_day": "夜晚"}
    assert prepared["degraded_capabilities"] == candidate["degraded_capabilities"]

    workspace.save_identity_candidate(conn, shot_id="s1", baseline=identity_contract_fingerprint(segment), candidate=candidate)
    stored = json.loads(read_independent("SELECT shot_contract_json FROM shots WHERE id='s1'")[0]["shot_contract_json"])["storyboard_pack_segment"]
    assert stored["resources"]["props"] == candidate["resources"]["props"]
    assert stored["continuity_memo"] == {"time_of_day": "夜晚"}

    manifest_entries = resolve_segment_prop_manifest_entries(stored["resources"]["props"], conn=conn, project_id="p", episode_no=5)
    assert [entry["label"] for entry in manifest_entries] == ["水泡坏的行李箱", "小木星星"]


def test_failed_artifact_write_rolls_back_everything(fixture, monkeypatch):
    conn, segment, _, _ = fixture
    before = read_independent("SELECT * FROM shots")
    def fail(*args,**kwargs):
        raise RuntimeError("模拟证据存储失败")
    monkeypatch.setattr(workspace.evidence_repository,"create_and_commit_artifact_in_transaction",fail)
    with pytest.raises(RuntimeError,match="模拟"):
        workspace.save_identity_candidate(conn,shot_id="s1",baseline=identity_contract_fingerprint(segment),candidate=candidate_of(segment))
    assert not conn.in_transaction
    assert read_independent("SELECT * FROM shots") == before
    assert read_independent("SELECT status FROM shot_versions WHERE id='v1'")[0]["status"] == "succeeded"


@pytest.mark.parametrize("attempt",range(10))
def test_two_real_connections_cannot_overwrite_same_revision(fixture,attempt):
    _, segment, _, _ = fixture
    barrier = Barrier(2)
    def save():
        conn = sqlite3.connect(db.DB_PATH,timeout=20)
        conn.row_factory = sqlite3.Row
        barrier.wait()
        try:
            workspace.save_identity_candidate(conn,shot_id="s1",baseline=identity_contract_fingerprint(segment),candidate=candidate_of(segment))
            return "saved"
        except ValueError as exc:
            assert "片段已更新" in str(exc)
            return "stale"
        finally:
            conn.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: save(),range(2))) == ["saved","stale"]
    assert len(read_independent("SELECT id FROM artifacts WHERE type='storyboard_shot'")) == 1


@pytest.mark.parametrize("active_kind",["slot","job"])
def test_active_video_work_prevents_apply(fixture,active_kind):
    conn,segment,_,_ = fixture
    if active_kind == "slot":
        conn.execute("UPDATE shot_versions SET video_slot_active=1 WHERE id='v1'")
    else:
        conn.execute("INSERT INTO jobs(id,kind,shot_id,status,created_at,updated_at) VALUES('j','video','s1','queued',?,?)",(db.now(),db.now()))
    conn.commit()
    with pytest.raises(ValueError,match="仍有视频任务"):
        workspace.save_identity_candidate(conn,shot_id="s1",baseline=identity_contract_fingerprint(segment),candidate=candidate_of(segment))


def test_paid_boundary_rechecks_identity_but_allows_existing_task_poll(fixture):
    conn,segment,_,_ = fixture
    meta = {"segment_identity_fingerprint":identity_contract_fingerprint(segment)}
    assert_identity_revision(conn,shot_id="s1",meta=meta,write_point="provider_submit")
    workspace.save_identity_candidate(conn,shot_id="s1",baseline=meta["segment_identity_fingerprint"],candidate=candidate_of(segment))
    for point in ("worker_start","provider_input_adoption","provider_submit"):
        with pytest.raises(ReviewDependencyFence,match="STORYBOARD_IDENTITY_STALE"):
            assert_identity_revision(conn,shot_id="s1",meta=meta,write_point=point)
    assert_identity_revision(conn,shot_id="s1",meta=meta,write_point="provider_poll")


def test_apply_persists_narrator_voice_character_missing_on_legacy_row(fixture):
    """真实回归（proj_ca86b15ab7d7 EP1 段 9）：旧行落库时没有 narrator_voice_character
    字段（``StoryboardPackSegment`` 曾经没有这个字段，见 app.production.storyboard_pack）。
    身份工作台 preview（``prepare_identity_candidate``）现查项目设置把字段补上了，
    但此前 ``save_identity_candidate`` 的「未变化」判据（``identity_contract_
    fingerprint``）不读这个字段，两次指纹算出来相同，被误判 unchanged 直接
    rollback——存量缺字段的行因此永远无法通过身份工作台这个入口修复。指纹现在
    把它计入比较：同一份合同首次原样重提必须落盘补齐字段；随后再原样重提一次
    （baseline 换成刚落盘的新指纹）才是真正的「没有变化」。"""
    conn, segment, _, _ = fixture
    # 先在旁白音色尚未设置时跑一次 preview，把 required_dialogue（按原文重新计算）、
    # speech_dialect（按 target_model 归一）先稳定下来——这两项与 narrator_voice_
    # character 无关但同样会被 prepare_identity_candidate 每次重算，不先固定它们，
    # 后面的比较会把它们的正常重算也算成「变化」，混淆了这次单独要验证的字段。
    stable = workspace.prepare_identity_candidate(conn, shot_id="s1", candidate=deepcopy(segment))
    legacy = dict(stable)
    del legacy["narrator_voice_character"]  # 模拟修复前落库的旧行：字段整个不存在
    conn.execute(
        "UPDATE shots SET shot_contract_json=? WHERE id='s1'",
        (json.dumps({"storyboard_pack_segment": legacy}),),
    )
    conn.execute("UPDATE projects SET narrator_voice_character='温念' WHERE id='p'")
    conn.commit()

    baseline = identity_contract_fingerprint(legacy)
    preview = workspace.prepare_identity_candidate(conn, shot_id="s1", candidate=deepcopy(legacy))
    assert preview["narrator_voice_character"] == "温念"
    # preview 的其余字段与 legacy 逐一相同——这次「变化」只应该来自 narrator_voice_
    # character，不是 required_dialogue/speech_dialect 等其它字段的正常重算波动。
    for key in ("resources", "dialogue", "required_dialogue", "prompt_text", "speech_dialect", "identity_contract_version"):
        assert preview[key] == legacy[key], f"字段 {key} 不应在这次无编辑的重提中变化"

    result = workspace.save_identity_candidate(conn, shot_id="s1", baseline=baseline, candidate=deepcopy(legacy))
    assert result.get("unchanged") is not True, "旧行缺字段时，补齐后的候选必须判定为有变化并落盘，不能被 unchanged 挡住"
    stored = json.loads(
        conn.execute("SELECT shot_contract_json FROM shots WHERE id='s1'").fetchone()["shot_contract_json"]
    )["storyboard_pack_segment"]
    assert stored["narrator_voice_character"] == "温念"

    # 原样再提交一次（baseline/candidate 都是刚落盘、已经带字段的合同）：这次必须
    # 判定未变化，不产生冗余修订、不多占一个 shot_versions 版本号。
    baseline2 = identity_contract_fingerprint(stored)
    result2 = workspace.save_identity_candidate(conn, shot_id="s1", baseline=baseline2, candidate=deepcopy(stored))
    assert result2 == {"unchanged": True}


def test_http_preview_errors_and_observations_are_scoped(fixture):
    _,segment,_,_ = fixture
    client = SessionTestClient(TestClient(app))
    review = client.get("/api/shots/s1/identity-review")
    assert review.status_code == 200, review.text
    assert "video_path" not in review.json()["versions"][0]
    candidate = candidate_of(segment)
    candidate["dialogue"][0]["delivery_kind"] = "broken"
    response = client.post("/api/shots/s1/identity-review/preview",json={"baseline":review.json()["baseline"],"candidate":candidate})
    assert response.status_code == 409, response.text
    assert client.post("/api/shots/s1/identity-review/observation",json={"version_id":"v2","notes":"越界"}).status_code == 422
    response = client.post("/api/shots/s1/identity-review/observation",json={"version_id":"v1","notes":"孟浩闭口，声音来自内心独白"})
    assert response.status_code == 200, response.text
    assert read_independent("SELECT content_json FROM artifacts WHERE id=?",(response.json()["artifact_id"],))


def test_identity_routes_enforce_owner_access(fixture):
    conn,_,_,_ = fixture
    conn.execute("INSERT INTO users(id,username,display_name,auth_provider,status,created_at) VALUES('other','other','other','local','active',?)",(db.now(),))
    conn.commit()
    client = TestClient(app)
    headers = {"X-Manju-Session":create_session("other")}
    assert client.get("/api/shots/s1/identity-review",headers=headers).status_code == 404
    for action in ("regenerate","preview","apply","observation"):
        assert client.post(f"/api/shots/s1/identity-review/{action}",headers=headers,json={"baseline":"x","candidate":{}}).status_code == 404


def test_regenerate_calls_model_only_for_selected_segment(fixture,monkeypatch):
    conn,segment,payload,_ = fixture
    from app.production import storyboard_pack
    requests = []
    async def chat(messages,**kwargs):
        request = json.loads(messages[1]["content"])
        requests.append(request)
        candidate = candidate_of(segment)
        candidate["prompt_text"] = candidate.pop("speech_template")
        candidate["continuity_memo"] = {"time_of_day":"白天"}
        candidate["shot_action_beats"] = [{"shot_no":n,"key_actions":["说话"]} for n in range(1,int(candidate.get("shot_count") or 1)+1)]  # 动作密度软检查要求逐镜申报
        draft = _AiStoryboardSegmentDraft.model_validate(candidate)
        assert kwargs["validate"](draft) == []
        return draft
    monkeypatch.setattr(storyboard_pack.model_gateway,"chat_structured",chat)
    before = read_independent("SELECT * FROM shots")
    episode = dict(conn.execute("SELECT * FROM episodes WHERE id='ep'").fetchone())
    result = asyncio.run(regenerate_identity_candidate(conn,episode=episode,shot_id="s1",payload=payload,bible=Bible.model_validate({"world":{"visual_style_canonical":"测试画风"},"characters":[],"scenes":[]})))
    assert len(requests) == 1 and requests[0]["segment_no"] == 1
    assert "内心独白（孟浩）" in result["prompt_text"]
    assert read_independent("SELECT * FROM shots") == before


@pytest.fixture
def narrative_authority_fixture(tmp_path):
    """叙事权威集：episode.screenplay_json 不带 prep_pack_version 标记。

    ``is_prep_pack_payload``（app/production/screenplay_authority.py）与
    ``load_identity_workspace`` 共用同一个判据——payload 里有没有
    ``prep_pack_version`` 键；没有这个键就是仍在跑 narrative_plan 权威链的
    集（合同 major 3-5，见 ``screenplay_contract_requires_narrative``），
    与 prep_pack（major>=6）互斥。用这个最小信号构造夹具，不重建完整
    narrative_plan schema，因为要锁住的正是 identity_workspace 这一道门。
    """
    conn = db.get_conn()
    payload = {"episode_no": 5, "narrative_plan": {"marker": "legacy-authority"}}
    conn.execute("INSERT INTO projects(id,name,bible_json,created_at) VALUES('p','本地回归','{}',?)", (db.now(),))
    conn.execute("INSERT INTO chapters(project_id,idx,title,content) VALUES('p',1,'第五章',?)", ('孟浩（OS）：我一定会回来。\n\n山风吹过。',))
    conn.execute("INSERT INTO episodes(id,project_id,episode_no,title,source_chapters,status,screenplay_json,created_at) VALUES('ep','p',5,'第五集','[1]','confirmed',?,?)", (json.dumps(payload),db.now()))
    segment = dict(segment_no=1,synopsis="孟浩自述",source_segment_indexes=[1],beat_ids=["B1"],
                   beats=[{"beat_id":"B1","summary":"孟浩自述","segment_indexes":[1]}],shot_count=1,duration_s=15,
                   target_model="seedance_2",degraded_capabilities=[],prompt_text="镜头1：远处山风。{{speech:U01}}",
                   dialogue=[dict(utterance_id="U01",speaker_identity_id="bible:孟浩",line="我一定会回来。",source_segment_index=1,delivery="offscreen_voice",delivery_kind="inner_monologue")],
                   resources={"characters":[dict(identity_id="bible:孟浩",display_name="孟浩",subject_kind="character",visibility="voice_only")],"scenes":[],"props":[]})
    render_segment_speech(segment,dialect="seedance")
    stamp_identity_contract(segment)
    conn.execute("INSERT INTO shots(id,episode_id,shot_no,duration_s,shot_size,camera_move,scene_setting,action_desc,narration,characters,dialogues,source_excerpt,shot_contract_json,adopted_version_id) VALUES('s1', 'ep',1,15,'','','','','','[]','[]',?,?,NULL)", ('孟浩（OS）：我一定会回来。',json.dumps({"storyboard_pack_segment":segment})))
    conn.commit()
    return conn, segment


def test_load_identity_workspace_rejects_narrative_authority_episode(narrative_authority_fixture):
    """叙事权威集不得进片段身份复核工作区——直接读取就要拒绝。"""
    conn, _segment = narrative_authority_fixture
    with pytest.raises(ValueError, match="叙事权威分镜请走原有受控修订流程"):
        workspace.load_identity_workspace(conn, "s1")


def test_apply_rejects_narrative_authority_episode_same_as_preview(narrative_authority_fixture):
    """apply（save_identity_candidate）与 preview 共用同一道 load_identity_workspace 拦截。

    两条独立调用点都要挡住：``_assert_idle_current`` 与
    ``prepare_identity_candidate`` 各自都会先调用 ``load_identity_workspace``，
    不存在只有只读路径校验、写入路径绕过的缺口。
    """
    conn, segment = narrative_authority_fixture
    candidate = candidate_of(segment)
    baseline = identity_contract_fingerprint(segment)
    before = read_independent("SELECT * FROM shots")
    with pytest.raises(ValueError, match="叙事权威分镜请走原有受控修订流程"):
        workspace.prepare_identity_candidate(conn, shot_id="s1", candidate=candidate)
    with pytest.raises(ValueError, match="叙事权威分镜请走原有受控修订流程"):
        workspace.save_identity_candidate(conn, shot_id="s1", baseline=baseline, candidate=candidate)
    assert read_independent("SELECT * FROM shots") == before


def test_http_identity_review_routes_reject_narrative_authority_episode(narrative_authority_fixture):
    """四条路由（GET 复核 + POST regenerate/preview/apply）全部要 409，不止预览。"""
    _conn, segment = narrative_authority_fixture
    client = SessionTestClient(TestClient(app))
    get_response = client.get("/api/shots/s1/identity-review")
    assert get_response.status_code == 409, get_response.text
    assert "叙事权威分镜请走原有受控修订流程" in get_response.text
    candidate = candidate_of(segment)
    for action in ("regenerate", "preview", "apply"):
        response = client.post(
            f"/api/shots/s1/identity-review/{action}",
            json={"baseline": "any", "candidate": candidate},
        )
        assert response.status_code == 409, (action, response.text)
        assert "叙事权威分镜请走原有受控修订流程" in response.text, (action, response.text)


def test_regenerate_http_uses_project_stage_provider(fixture,monkeypatch):
    conn,_,_,_ = fixture
    from app.domain.storyboard_ops import identity_review as routes
    from app.harness.text_provider_scope import current_stage_text_provider
    conn.execute("UPDATE projects SET bible_json='', board_text_provider='chosen' WHERE id='p'")
    conn.commit()
    selected = []
    monkeypatch.setattr(routes,"resolve_stage_text_provider",lambda provider: selected.append(provider) or "chosen")
    async def candidate(*args,**kwargs):
        assert current_stage_text_provider() == "chosen"
        return {"checked":True}
    monkeypatch.setattr(routes,"regenerate_identity_candidate",candidate)
    client = SessionTestClient(TestClient(app))
    baseline = client.get("/api/shots/s1/identity-review").json()["baseline"]
    result = client.post("/api/shots/s1/identity-review/regenerate",json={"baseline":baseline})
    assert result.status_code == 200, result.text
    assert result.json()["candidate"]["checked"]
    assert selected == ["chosen"]
