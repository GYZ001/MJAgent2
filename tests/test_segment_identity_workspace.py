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
