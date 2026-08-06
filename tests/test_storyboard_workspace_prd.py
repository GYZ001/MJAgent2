import asyncio
import json
import threading

import pytest
from fastapi import HTTPException

from app import api, db, narrative_review, task_registry
from app.capabilities.direct import enter_handler
from app.evidence import repository
from app.harness.types import EvidenceArtifact
from app.schemas import Bible, Character, EpisodeScreenplay, World
from app import storyboard_workspace as workspace
from app.orchestration import api as orchestration_api
from app.orchestration.engine import WorkflowRecorder
from app.orchestration.state_machine import transition_run
from app.storyboard_supervisor import (
    SupervisorCheckpoint,
    load_latest_checkpoint,
    save_checkpoint,
)
from app.domain.storyboard_ops import _recorded_storyboard_task


@pytest.fixture()
def storyboard_db(tmp_path, monkeypatch):
    database = tmp_path / "storyboard-workspace.db"
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_local", threading.local())
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        """INSERT INTO projects(id,name,bible_json,bible_status,plan_status,created_at)
           VALUES('p1','测试项目','', 'ready','ready',1)"""
    )
    screenplay = {"id": "script-1", "episode_no": 1, "title": "测试", "full_script_text": "测试正文"}
    conn.execute(
        """INSERT INTO episodes(
               id,project_id,episode_no,title,source_chapters,target_duration_s,
               screenplay_json,screenplay_status,screenplay_artifact_id,status,created_at
           ) VALUES('e1','p1',1,'第一集','[1]',10,?,'ready','screenplay-v1','scripted',1)""",
        (json.dumps(screenplay, ensure_ascii=False),),
    )
    source = "少年推开房门，看见桌上的信，神色骤然一沉。"
    conn.execute(
        "INSERT INTO chapters(project_id,idx,title,content) VALUES('p1',1,'第一章',?)",
        (source,),
    )
    artifact = repository.create_artifact(EvidenceArtifact(
        type="storyboard_shot",
        scope_type="storyboard_checkpoint",
        scope_id="e1:1",
        status="validated",
        trust_level="T2",
        content={"shot_no": 1, "action_desc": "少年推门查看信件"},
    ))
    contract = {
        "state_in": "少年站在门外", "primary_action": "少年推门拿起信件",
        "state_out": "少年看完信后神色一沉", "characters_visible": ["少年"],
        "audio_cast": [], "audio_timeline": [], "new_information_ids": [],
        "spoken_contract_status": "coherent", "is_final": True,
    }
    conn.execute(
        """INSERT INTO shots(
               id,episode_id,shot_no,duration_s,shot_size,camera_move,scene_setting,
               characters,action_desc,first_frame_desc,last_frame_desc,source_excerpt,
               narration,dialogues,transition,continuity_from_prev,shot_contract_json,
               storyboard_artifact_id
           ) VALUES('s1','e1',1,5,'中景','固定','白天，房间','["少年"]',?,?,?,?,
                    '', '[]','硬切',0,?,?)""",
        (
            "少年推开房门并拿起桌上的信件查看。",
            "少年站在紧闭的房门外。",
            "少年拿着信件神色骤然一沉。",
            source,
            json.dumps(contract, ensure_ascii=False),
            artifact["id"],
        ),
    )
    conn.execute("UPDATE episodes SET storyboard_artifact_id=? WHERE id='e1'", (artifact["id"],))
    conn.commit()
    yield conn
    conn.close()


def _seed_narrative_review_lineage(conn):
    shot_artifact_id = conn.execute(
        "SELECT storyboard_artifact_id FROM shots WHERE id='s1'"
    ).fetchone()[0]
    review_input = repository.create_artifact(EvidenceArtifact(
        type="storyboard_review_input",
        scope_type="episode",
        scope_id="e1",
        status="validated",
        trust_level="T2",
        content={"reviewed": "board"},
        parent_artifact_ids=[shot_artifact_id],
    ))
    observation = repository.create_artifact(EvidenceArtifact(
        type="blind_audience_observation",
        scope_type="episode",
        scope_id="e1",
        status="validated",
        trust_level="T2",
        content={"observation": "first impression"},
        parent_artifact_ids=[review_input["id"]],
    ))
    report = repository.create_artifact(EvidenceArtifact(
        type="narrative_review_report",
        scope_type="episode",
        scope_id="e1",
        status="validated",
        trust_level="T2",
        content={"decision": "pass"},
        parent_artifact_ids=[observation["id"]],
    ))
    future_consumer = repository.create_artifact(EvidenceArtifact(
        type="future_review_consumer",
        scope_type="episode",
        scope_id="e1",
        status="validated",
        trust_level="T2",
        content={"derived": True},
        parent_artifact_ids=[report["id"]],
    ))
    conn.execute(
        "UPDATE episodes SET narrative_status='ready',narrative_review_artifact_id=? "
        "WHERE id='e1'",
        (report["id"],),
    )
    conn.commit()
    return {
        "shot": shot_artifact_id,
        "review_input": review_input["id"],
        "observation": observation["id"],
        "report": report["id"],
        "future_consumer": future_consumer["id"],
    }


def test_narrative_review_input_parents_current_screenplay_and_every_shot_artifact(
    storyboard_db,
    monkeypatch,
):
    screenplay_artifact = repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id="e1",
        status="validated",
        trust_level="T2",
        content={"episode_no": 1},
    ))
    storyboard_db.execute(
        "UPDATE episodes SET screenplay_artifact_id=? WHERE id='e1'",
        (screenplay_artifact["id"],),
    )
    storyboard_db.commit()
    screenplay = EpisodeScreenplay.model_validate({
        "episode_no": 1,
        "narrative_plan": {
            "scope_id": "e1",
            "audience_priors": [{
                "audience_prior_id": "PRIOR-1",
                "audience_description": "A first-time viewer",
            }],
        },
    })
    rows = storyboard_db.execute(
        "SELECT * FROM shots WHERE episode_id='e1' ORDER BY shot_no"
    ).fetchall()
    board = api._board_from_shot_rows(rows, 1)
    api._ensure_current_storyboard_shot_artifacts(
        storyboard_db,
        "e1",
        board,
    )

    async def stop_after_review_input(**_kwargs):
        raise RuntimeError("review input captured")

    monkeypatch.setattr(
        narrative_review,
        "_resolve_review_screenplay_authority",
        lambda **_kwargs: (screenplay, screenplay_artifact["id"]),
    )
    monkeypatch.setattr(narrative_review, "_structured_call", stop_after_review_input)
    with pytest.raises(RuntimeError, match="review input captured"):
        asyncio.run(narrative_review.run_blind_audience_review(
            episode_id="e1",
            screenplay=screenplay,
            board=board,
            screenplay_artifact_id=screenplay_artifact["id"],
        ))

    review_input = storyboard_db.execute(
        """SELECT parent_artifact_ids_json FROM artifacts
           WHERE type='storyboard_review_input' AND scope_id='e1'
           ORDER BY version DESC LIMIT 1"""
    ).fetchone()
    shot_artifact_id = storyboard_db.execute(
        "SELECT storyboard_artifact_id FROM shots WHERE id='s1'"
    ).fetchone()[0]
    assert json.loads(review_input["parent_artifact_ids_json"]) == [
        screenplay_artifact["id"],
        shot_artifact_id,
    ]


def test_snapshot_version_is_monotonic_and_action_is_unique(storyboard_db):
    ep = api.episode_detail("e1", view="board")
    first = ep["storyboard_status"]
    assert first["recommended_action"] in {
        "confirm_storyboard", "resume_storyboard", "refresh_status",
    }
    assert isinstance(first["recommended_action"], str)
    assert first["hard_gate_issue_count"] == len(first["hard_gate_issues"])
    assert ep["shots"][0]["qa_warnings"]
    assert not ep["shots"][0].get("preflight_errors")

    storyboard_db.execute("UPDATE episodes SET status='scripting' WHERE id='e1'")
    storyboard_db.commit()
    second = api.episode_detail("e1", view="board")["storyboard_status"]
    assert second["snapshot_version"] > first["snapshot_version"]
    assert second["recommended_action"] == "view_progress"
    assert second["confirmable"] is False


def test_board_status_gate_receives_outline_hidden_from_public_response(
    storyboard_db,
    monkeypatch,
):
    outline_json = json.dumps({
        "episode_no": 1,
        "shots": [{"shot_no": 1}],
        "readability_windows": [{
            "readability_window_id": "RW-1",
            "shot_ids": ["SH001"],
        }],
    })
    storyboard_db.execute(
        "UPDATE episodes SET storyboard_outline_json=? WHERE id='e1'",
        (outline_json,),
    )
    storyboard_db.commit()
    captured: dict[str, object] = {}

    def current_gate(ep, board, _screenplay, _bible, **_kwargs):
        captured["outline_json"] = ep.get("storyboard_outline_json")
        return api.ConfirmationEvaluation(
            passed=True,
            errors=[],
            warnings=[],
            issues=[],
            board=board,
            compact_target=10,
            estimated_cost_cny=0,
        )

    monkeypatch.setattr(api, "evaluate_storyboard_for_confirmation", current_gate)

    detail = api.episode_detail("e1", view="board")

    assert captured["outline_json"] == outline_json
    assert "storyboard_outline_json" not in detail
    assert detail["storyboard_status"]["hard_gate_issues"] == []


def test_unlocatable_legacy_excerpt_is_not_a_user_facing_gate(storyboard_db):
    storyboard_db.execute(
        "UPDATE shots SET source_excerpt=? WHERE id='s1'",
        ("这是一段长度足够但并非授权章节逐字原文的历史证据",),
    )
    storyboard_db.commit()

    episode = api.episode_detail("e1", view="board")
    status = episode["storyboard_status"]

    assert status["state"] == "ready_to_confirm"
    assert status["recommended_action"] == "confirm_storyboard"
    assert not any("授权原文中定位" in issue for issue in status["hard_gate_issues"])
    assert not episode["shots"][0].get("preflight_errors")


def test_episode_detail_does_not_run_scene_projection_writes(storyboard_db, monkeypatch):
    def unexpected_write(*_args, **_kwargs):
        raise AssertionError("GET episode detail must not reconcile persistent scene fields")

    monkeypatch.setattr(api, "_reconcile_storyboard_scene_projection", unexpected_write)

    detail = api.episode_detail("e1", view="board")

    assert detail["id"] == "e1"


def test_real_structural_error_is_attached_to_the_problem_shot(storyboard_db):
    storyboard_db.execute("UPDATE shots SET source_excerpt='' WHERE id='s1'")
    storyboard_db.commit()

    episode = api.episode_detail("e1", view="board")
    status = episode["storyboard_status"]

    assert status["state"] == "failed"
    assert any("缺少必填字段" in issue for issue in status["hard_gate_issues"])
    assert any("缺少必填字段" in issue for issue in episode["shots"][0]["preflight_errors"])


def test_complete_board_uses_current_gate_instead_of_stale_paused_issue(
    storyboard_db, monkeypatch,
):
    stale_issue = "整集分镜遗漏本集剧本场景：旧场景名"

    def current_gate(_ep, board, _screenplay, _bible, **_kwargs):
        return api.ConfirmationEvaluation(
            passed=True,
            errors=[],
            warnings=[],
            issues=[],
            board=board,
            compact_target=10,
            estimated_cost_cny=0,
        )

    monkeypatch.setattr(api, "evaluate_storyboard_for_confirmation", current_gate)
    save_checkpoint(SupervisorCheckpoint(
        episode_id="e1",
        phase="WAITING_HUMAN",
        validated_prefix_end=1,
        next_shot_no=2,
        expected_total=1,
        last_repair={"issue_messages": [stale_issue]},
    ))
    storyboard_db.execute(
        "UPDATE episodes SET status='scripted',script_error=? WHERE id='e1'",
        (stale_issue,),
    )
    storyboard_db.commit()

    detail = api.episode_detail("e1", view="board")
    status = detail["storyboard_status"]

    assert status["state"] == "ready_to_confirm"
    assert status["hard_gate_issues"] == []
    assert detail["script_error"] is None


def test_legacy_confirmed_board_reopens_when_current_hard_gate_fails(
    storyboard_db, monkeypatch,
):
    issue = "主线节拍主体已入画但未完成对应动作/对白交付：S03/少年"

    def failed_gate(_ep, board, _screenplay, _bible, **_kwargs):
        return api.ConfirmationEvaluation(
            passed=False,
            errors=[issue],
            warnings=[],
            issues=[],
            board=board,
            compact_target=10,
            estimated_cost_cny=0,
        )

    monkeypatch.setattr(api, "evaluate_storyboard_for_confirmation", failed_gate)
    storyboard_db.execute("UPDATE episodes SET status='confirmed' WHERE id='e1'")
    storyboard_db.commit()

    status = api.episode_detail("e1", view="board")["storyboard_status"]

    assert status["state"] == "failed"
    assert status["confirmed"] is True
    assert status["hard_gates_passed"] is False
    assert status["hard_gate_issues"] == [issue]
    assert status["recommended_action"] == "resume_storyboard"
    assert status["confirmable"] is False


def test_status_distinguishes_visible_drafts_from_zero_safe_checkpoint(storyboard_db):
    save_checkpoint(SupervisorCheckpoint(
        episode_id="e1",
        phase="PAUSED_EXTERNAL",
        validated_prefix_end=0,
        next_shot_no=1,
        expected_total=5,
    ))
    storyboard_db.execute(
        "UPDATE episodes SET status='scripting',script_error='外部依赖暂不可用' WHERE id='e1'"
    )
    storyboard_db.commit()

    status = api.episode_detail("e1", view="board")["storyboard_status"]

    assert status["state"] == "paused"
    assert status["planned_shots"] == 5
    assert status["draft_shots"] == 1
    assert status["safe_checkpoint_shots"] == 0
    assert status["pending_revalidation_shots"] == 1
    assert status["resume_from_shot"] == 1
    assert "通过" not in status["headline"]


@pytest.mark.asyncio
async def test_final_tail_with_hard_gates_reopens_existing_repair_instead_of_appending(
    storyboard_db, monkeypatch,
):
    issue = "shot_no=1 的对白构图仍需修复"

    def failed_gate(_ep, board, _screenplay, _bible, **_kwargs):
        return api.ConfirmationEvaluation(
            passed=False,
            errors=[issue],
            warnings=[],
            issues=[],
            board=board,
            compact_target=10,
            estimated_cost_cny=0,
        )

    monkeypatch.setattr(api, "evaluate_storyboard_for_confirmation", failed_gate)
    save_checkpoint(SupervisorCheckpoint(
        episode_id="e1",
        phase="SUCCEEDED",
        validated_prefix_end=1,
        next_shot_no=2,
        expected_total=1,
        activation_no=1,
        activation_attempt_count=5,
        outcome="SUCCEEDED_GATE_RETRY_EXHAUSTED_FALLBACK",
        input_versions={"screenplay_artifact_id": "screenplay-v1"},
        last_repair={
            "status": "fallback_published",
            "reason": "repair_candidate_no_progress",
            "issue_messages": [issue],
        },
    ))

    detail = api.episode_detail("e1", view="board")
    status = detail["storyboard_status"]
    preview = api._storyboard_start_preflight_payload("e1")

    assert status["state"] == "failed"
    assert status["recommended_action"] == "resume_storyboard"
    assert status["resume_mode"] == "repair_existing"
    assert preview["resume_mode"] == "repair_existing"
    assert preview["can_start"] is True
    assert preview["kept_validated_shots"] == 1
    assert preview["remaining_shots"] == 0
    assert preview["current_gate_issues"] == [issue]
    assert "重新执行整集门禁" in preview["impact"]

    spawned: dict[str, object] = {}

    def fake_spawn(kind, key, coro, *, project_id=None):
        spawned.update(kind=kind, key=key, project_id=project_id)
        coro.close()
        return object()

    monkeypatch.setattr(task_registry, "spawn", fake_spawn)
    approved_preview = api.storyboard_start_preflight("e1", {})
    result = await api.resume_storyboard(
        "e1",
        {"preflight_token": approved_preview["preview_token"]},
    )

    assert result["action"] == "resume"
    assert result["next_shot_no"] == 2
    assert spawned == {"kind": "storyboard", "key": "e1", "project_id": "p1"}
    assert load_latest_checkpoint("e1").last_repair["status"] == "candidate_pending"
    assert storyboard_db.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id='e1'"
    ).fetchone()["c"] == 1


@pytest.mark.asyncio
async def test_confirmable_final_tail_still_rejects_blind_resume(storyboard_db, monkeypatch):
    def passed_gate(_ep, board, _screenplay, _bible, **_kwargs):
        return api.ConfirmationEvaluation(
            passed=True,
            errors=[],
            warnings=[],
            issues=[],
            board=board,
            compact_target=10,
            estimated_cost_cny=0,
        )

    monkeypatch.setattr(api, "evaluate_storyboard_for_confirmation", passed_gate)
    save_checkpoint(SupervisorCheckpoint(
        episode_id="e1",
        phase="SUCCEEDED",
        validated_prefix_end=1,
        next_shot_no=2,
        expected_total=1,
        outcome="SUCCEEDED_READY_FOR_CONFIRM",
        input_versions={"screenplay_artifact_id": "screenplay-v1"},
    ))

    preview = api._storyboard_start_preflight_payload("e1")
    assert preview["can_start"] is False
    assert "直接确认分镜" in preview["blocking_reason"]

    with pytest.raises(HTTPException, match="直接确认分镜") as caught:
        await api.resume_storyboard("e1")

    assert caught.value.status_code == 409


def _leave_stale_checkpoint_after_screenplay_republish(storyboard_db) -> None:
    storyboard_db.execute("DELETE FROM shots WHERE episode_id='e1'")
    storyboard_db.execute(
        """UPDATE episodes
           SET screenplay_artifact_id='screenplay-v2',
               storyboard_outline_json=NULL,
               storyboard_artifact_id=NULL,
               working_storyboard_artifact_id=NULL,
               published_storyboard_artifact_id=NULL,
               storyboard_production_revision_id=NULL,
               storyboard_completion_certificate_id=NULL,
               status='scripted',
               script_error='上游剧本已变更，自动完成授权失效，请重新授权后继续'
           WHERE id='e1'"""
    )
    storyboard_db.commit()
    save_checkpoint(SupervisorCheckpoint(
        episode_id="e1",
        phase="WAITING_AUTHORIZATION",
        validated_prefix_end=12,
        next_shot_no=13,
        expected_total=12,
        input_versions={"screenplay_artifact_id": "screenplay-v1"},
    ))


def test_republished_screenplay_does_not_resume_checkpoint_from_cleared_board(storyboard_db):
    _leave_stale_checkpoint_after_screenplay_republish(storyboard_db)

    detail = api.episode_detail("e1", view="board")
    preview = api.storyboard_start_preflight("e1", {})

    assert detail["supervisor"] is None
    assert detail["script_error"] is None
    assert detail["storyboard_status"]["state"] == "empty"
    assert detail["storyboard_status"]["recommended_action"] == "generate_storyboard"
    assert detail["storyboard_status"]["resume_from_shot"] == 1
    assert preview["action"] == "create"
    assert preview["checkpoint"] == {
        "available": False,
        "phase": None,
        "resume_from_shot": 1,
    }
    assert preview["kept_validated_shots"] == 0
    assert preview["planned_shots"] is None


@pytest.mark.asyncio
async def test_canonical_start_creates_for_republished_screenplay_with_only_stale_checkpoint(
    storyboard_db, monkeypatch,
):
    _leave_stale_checkpoint_after_screenplay_republish(storyboard_db)
    preview = api.storyboard_start_preflight("e1", {})

    def fake_spawn(_kind, _key, coro, *, project_id=None):
        coro.close()
        return object()

    monkeypatch.setattr(task_registry, "spawn", fake_spawn)
    result = await api.start_storyboard("e1", {
        "preflight_token": preview["preview_token"],
    })

    assert result["action"] == "create"
    episode = storyboard_db.execute(
        "SELECT status,script_error,active_storyboard_run_id FROM episodes WHERE id='e1'"
    ).fetchone()
    assert episode["status"] == "scripting"
    assert episode["script_error"] is None
    assert episode["active_storyboard_run_id"] == result["run_id"]


@pytest.mark.asyncio
async def test_direct_resume_rejects_stale_checkpoint_after_board_was_cleared(storyboard_db):
    _leave_stale_checkpoint_after_screenplay_republish(storyboard_db)

    with pytest.raises(HTTPException, match="重新生成分镜") as caught:
        await api.resume_storyboard("e1")

    assert caught.value.status_code == 409


def test_upstream_mismatch_still_requires_resume_when_current_board_exists(storyboard_db):
    storyboard_db.execute(
        "UPDATE episodes SET screenplay_artifact_id='screenplay-v2' WHERE id='e1'"
    )
    storyboard_db.commit()
    save_checkpoint(SupervisorCheckpoint(
        episode_id="e1",
        phase="WAITING_AUTHORIZATION",
        validated_prefix_end=1,
        next_shot_no=2,
        expected_total=2,
        input_versions={"screenplay_artifact_id": "screenplay-v1"},
    ))

    preview = api.storyboard_start_preflight("e1", {})

    assert preview["action"] == "resume"
    assert preview["checkpoint"]["available"] is True
    assert preview["checkpoint"]["resume_from_shot"] == 2


def _cancel_test_run(storyboard_db) -> WorkflowRecorder:
    recorder = WorkflowRecorder.create(
        workflow_type="storyboard",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="cancel-test",
    )
    recorder.start()
    save_checkpoint(SupervisorCheckpoint(
        episode_id="e1",
        phase="GENERATING_SHOTS",
        validated_prefix_end=1,
        next_shot_no=2,
        expected_total=5,
    ), run_id=recorder.run_id)
    storyboard_db.execute(
        "UPDATE episodes SET status='scripting',script_error=NULL,active_storyboard_run_id=? WHERE id='e1'",
        (recorder.run_id,),
    )
    storyboard_db.commit()
    return recorder


def test_recorded_storyboard_task_releases_terminal_write_pointer(storyboard_db, monkeypatch):
    recorder = WorkflowRecorder.create(
        workflow_type="storyboard", scope_type="episode", scope_id="e1",
        input_fingerprint="release-pointer-test",
    )
    storyboard_db.execute(
        "UPDATE episodes SET active_storyboard_run_id=?,status='scripted',script_error='暂停待处理' WHERE id='e1'",
        (recorder.run_id,),
    )
    storyboard_db.commit()

    async def completed_task(*args, **kwargs):
        return None

    monkeypatch.setattr("app.domain.storyboard_ops._storyboard_task", completed_task)
    asyncio.run(_recorded_storyboard_task("e1", recorder, resume=True))

    row = storyboard_db.execute(
        "SELECT active_storyboard_run_id FROM episodes WHERE id='e1'",
    ).fetchone()
    assert row["active_storyboard_run_id"] is None


@pytest.mark.asyncio
async def test_run_cancel_converges_storyboard_episode_and_checkpoint(storyboard_db):
    recorder = _cancel_test_run(storyboard_db)

    result = await orchestration_api.cancel_run(recorder.run_id)

    assert result["cancelled"] is True
    assert result["run"]["status"] == "CANCELLED"
    episode = storyboard_db.execute(
        "SELECT status,script_error,active_storyboard_run_id FROM episodes WHERE id='e1'"
    ).fetchone()
    assert episode["status"] == "script_failed"
    assert "第 2 镜继续" in episode["script_error"]
    assert episode["active_storyboard_run_id"] is None
    checkpoint = load_latest_checkpoint("e1")
    assert checkpoint is not None
    assert checkpoint.phase == "CANCELLED"
    assert checkpoint.outcome == "CANCELLED"


def test_board_read_repairs_legacy_cancelled_run_without_losing_shots(storyboard_db):
    recorder = _cancel_test_run(storyboard_db)
    recorder.cancel("模拟旧版本仅取消 Run")

    detail = api.episode_detail("e1", view="board")

    assert detail["status"] == "script_failed"
    assert detail["active_storyboard_run_id"] is None
    assert detail["storyboard_status"]["state"] == "failed"
    assert detail["storyboard_status"]["recommended_action"] == "resume_storyboard"
    assert len(detail["shots"]) == 1
    assert detail["supervisor"]["phase"] == "CANCELLED"


def test_active_storyboard_run_can_clear_its_repair_window_only(storyboard_db):
    from app.artifacts import clear_shot_artifacts

    recorder = WorkflowRecorder.create(
        workflow_type="storyboard", scope_type="episode", scope_id="e1",
        input_fingerprint="repair-window",
    )
    recorder.start()
    storyboard_db.execute(
        "UPDATE episodes SET status='scripting',active_storyboard_run_id=? WHERE id='e1'",
        (recorder.run_id,),
    )
    storyboard_db.commit()

    with pytest.raises(ValueError, match="仍在写入"):
        clear_shot_artifacts("s1", active_storyboard_run_id="run_not_current")
    storyboard_db.execute("UPDATE episodes SET script_error='修复计划已写入' WHERE id='e1'")
    assert storyboard_db.in_transaction is True
    result = clear_shot_artifacts("s1", active_storyboard_run_id=recorder.run_id)
    assert result["shot_id"] == "s1"
    assert storyboard_db.execute("SELECT id FROM shots WHERE id='s1'").fetchone() is not None


def test_supervisor_artifact_clear_can_join_one_atomic_repair_transaction(storyboard_db):
    from app.artifacts import clear_shot_artifacts

    recorder = WorkflowRecorder.create(
        workflow_type="storyboard", scope_type="episode", scope_id="e1",
        input_fingerprint="atomic-window",
    )
    recorder.start()
    storyboard_db.execute(
        "UPDATE episodes SET status='scripting',active_storyboard_run_id=? WHERE id='e1'",
        (recorder.run_id,),
    )
    storyboard_db.commit()

    clear_shot_artifacts(
        "s1", active_storyboard_run_id=recorder.run_id, commit=False
    )
    assert storyboard_db.in_transaction is True
    storyboard_db.execute("UPDATE shots SET shot_no=2 WHERE id='s1'")
    storyboard_db.rollback()
    assert storyboard_db.execute("SELECT shot_no FROM shots WHERE id='s1'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_resume_persists_active_storyboard_run_before_spawning(storyboard_db, monkeypatch):
    from app import task_registry

    storyboard_db.execute(
        "UPDATE episodes SET status='script_failed',script_error='可恢复' WHERE id='e1'"
    )
    storyboard_db.commit()
    spawned: dict[str, object] = {}

    def fake_spawn(kind, key, coro, *, project_id=None):
        spawned.update(kind=kind, key=key, project_id=project_id)
        coro.close()
        return object()

    monkeypatch.setattr(task_registry, "spawn", fake_spawn)
    result = await api.resume_storyboard("e1")

    episode = storyboard_db.execute(
        "SELECT status,active_storyboard_run_id FROM episodes WHERE id='e1'"
    ).fetchone()
    assert result["run_id"] == episode["active_storyboard_run_id"]
    assert episode["status"] == "scripting"
    assert spawned == {"kind": "storyboard", "key": "e1", "project_id": "p1"}


@pytest.mark.asyncio
async def test_resume_does_not_deduplicate_terminal_run_behind_scripting_projection(
    storyboard_db, monkeypatch,
):
    from app import task_registry

    terminal = WorkflowRecorder.create(
        workflow_type="storyboard",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="terminal-partial",
    )
    terminal.start()
    terminal.partial("activation budget yielded")
    storyboard_db.execute(
        "UPDATE episodes SET status='scripting',active_storyboard_run_id=? WHERE id='e1'",
        (terminal.run_id,),
    )
    storyboard_db.commit()
    spawned: dict[str, object] = {}

    def fake_spawn(kind, key, coro, *, project_id=None):
        spawned.update(kind=kind, key=key, project_id=project_id)
        coro.close()
        return object()

    monkeypatch.setattr(task_registry, "spawn", fake_spawn)
    monkeypatch.setattr(task_registry, "active", lambda _kind, _key: False)

    result = await api.resume_storyboard("e1")

    assert result.get("deduplicated") is not True
    assert result["run_id"] != terminal.run_id
    assert spawned == {"kind": "storyboard", "key": "e1", "project_id": "p1"}


@pytest.mark.asyncio
async def test_batch_storyboard_does_not_take_over_durable_active_run(
    storyboard_db, monkeypatch,
):
    from app import task_registry
    from app.capabilities import dispatch

    active = WorkflowRecorder.create(
        workflow_type="storyboard",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="other-instance-active",
    )
    active.start()
    storyboard_db.execute(
        "UPDATE episodes SET status='scripting',active_storyboard_run_id=? WHERE id='e1'",
        (active.run_id,),
    )
    storyboard_db.commit()
    monkeypatch.setattr(task_registry, "active", lambda *_args: False)

    async def bypass_capability_route(*_args, **_kwargs):
        return None

    monkeypatch.setattr(dispatch, "ui_route", bypass_capability_route)

    with pytest.raises(HTTPException) as rejected:
        await api.start_storyboard_all("p1")

    assert rejected.value.status_code == 409
    episode = storyboard_db.execute(
        "SELECT status,active_storyboard_run_id FROM episodes WHERE id='e1'",
    ).fetchone()
    assert dict(episode) == {
        "status": "scripting",
        "active_storyboard_run_id": active.run_id,
    }


@pytest.mark.asyncio
async def test_first_storyboard_spawn_failure_restores_episode_state(
    storyboard_db, monkeypatch,
):
    # “开始任务”只接受干净分镜；已有数据必须先明确清空，不能被 create 暗中覆盖。
    with enter_handler():
        await api.clear_storyboard("e1")
    storyboard_db.execute(
        "UPDATE episodes SET status='planned',script_error='启动前状态' WHERE id='e1'"
    )
    storyboard_db.commit()

    def fail_spawn(_kind, _key, coro, *, project_id=None):
        coro.close()
        raise RuntimeError("event loop unavailable")

    monkeypatch.setattr(task_registry, "spawn", fail_spawn)
    with enter_handler(), pytest.raises(HTTPException) as exc_info:
        await api.start_storyboard("e1")

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "STORYBOARD_START_SPAWN_FAILED"
    episode = storyboard_db.execute(
        "SELECT status,script_error,active_storyboard_run_id FROM episodes WHERE id='e1'"
    ).fetchone()
    assert dict(episode) == {
        "status": "planned",
        "script_error": "启动前状态",
        "active_storyboard_run_id": None,
    }
    latest = storyboard_db.execute(
        "SELECT status FROM workflow_runs WHERE workflow_type='storyboard' "
        "AND scope_id='e1' ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    assert latest["status"] == "CANCELLED"


def test_storyboard_recovery_resumes_service_restart_and_persists_pointer(
    storyboard_db, monkeypatch,
):
    parent = WorkflowRecorder.create(
        workflow_type="storyboard",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="restart",
    )
    parent.start()
    parent.pause_external("服务重启")
    storyboard_db.execute(
        "UPDATE episodes SET status='scripting',active_storyboard_run_id=? WHERE id='e1'",
        (parent.run_id,),
    )
    storyboard_db.commit()
    spawned: dict[str, object] = {}

    def fake_spawn(kind, key, coro, *, project_id=None):
        spawned.update(kind=kind, key=key, project_id=project_id)
        coro.close()
        return None

    monkeypatch.setattr(task_registry, "spawn", fake_spawn)

    assert api.recover_storyboard_tasks() == 1
    episode = storyboard_db.execute(
        "SELECT status,active_storyboard_run_id FROM episodes WHERE id='e1'"
    ).fetchone()
    assert episode["status"] == "scripting"
    assert episode["active_storyboard_run_id"] != parent.run_id
    child = storyboard_db.execute(
        "SELECT parent_run_id,trigger_type FROM workflow_runs WHERE id=?",
        (episode["active_storyboard_run_id"],),
    ).fetchone()
    assert dict(child) == {"parent_run_id": parent.run_id, "trigger_type": "resume"}
    assert spawned == {"kind": "storyboard", "key": "e1", "project_id": "p1"}


def test_storyboard_recovery_does_not_take_over_user_pause(
    storyboard_db, monkeypatch,
):
    paused = WorkflowRecorder.create(
        workflow_type="storyboard",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="user-pause",
    )
    paused.start()
    transition_run(paused.run_id, "RUNNING", "PAUSED_EXTERNAL", "user_pause")
    storyboard_db.execute(
        "UPDATE episodes SET status='scripting',active_storyboard_run_id=? WHERE id='e1'",
        (paused.run_id,),
    )
    storyboard_db.commit()
    monkeypatch.setattr(
        task_registry,
        "spawn",
        lambda *_args, **_kwargs: pytest.fail("用户暂停不应被启动恢复接管"),
    )

    assert api.recover_storyboard_tasks() == 0
    assert storyboard_db.execute(
        "SELECT active_storyboard_run_id FROM episodes WHERE id='e1'"
    ).fetchone()[0] == paused.run_id


@pytest.mark.asyncio
async def test_recorded_storyboard_shutdown_becomes_recoverable_pause(
    storyboard_db, monkeypatch,
):
    recorder = WorkflowRecorder.create(
        workflow_type="storyboard",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="shutdown",
    )
    storyboard_db.execute(
        "UPDATE episodes SET status='scripting',active_storyboard_run_id=? WHERE id='e1'",
        (recorder.run_id,),
    )
    storyboard_db.commit()

    async def interrupted(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr("app.domain.storyboard_ops._storyboard_task", interrupted)
    monkeypatch.setattr(task_registry, "shutdown_in_progress", lambda: True)

    with pytest.raises(asyncio.CancelledError):
        await _recorded_storyboard_task("e1", recorder, resume=True)

    run = repository.get_run(recorder.run_id)
    assert run["status"] == "PAUSED_EXTERNAL"
    assert run["failure_code"] == "SERVICE_RESTART"
    assert storyboard_db.execute(
        "SELECT active_storyboard_run_id FROM episodes WHERE id='e1'"
    ).fetchone()[0] is None


@pytest.mark.asyncio
async def test_batch_storyboard_reports_partial_start_failure(
    storyboard_db, monkeypatch,
):
    screenplay = storyboard_db.execute(
        "SELECT screenplay_json,screenplay_artifact_id FROM episodes WHERE id='e1'"
    ).fetchone()
    storyboard_db.execute(
        "UPDATE episodes SET status='planned',script_error=NULL,active_storyboard_run_id=NULL "
        "WHERE id='e1'"
    )
    storyboard_db.execute(
        """INSERT INTO episodes(
               id,project_id,episode_no,title,screenplay_json,screenplay_status,
               screenplay_artifact_id,status,created_at
           ) VALUES('e2','p1',2,'第二集',?,'ready',?,'planned',1)""",
        (screenplay["screenplay_json"], screenplay["screenplay_artifact_id"]),
    )
    storyboard_db.commit()

    def selective_spawn(_kind, key, coro, *, project_id=None):
        coro.close()
        if key == "e2":
            raise RuntimeError("queue unavailable")
        return None

    monkeypatch.setattr(task_registry, "spawn", selective_spawn)
    with enter_handler():
        result = await api.start_storyboard_all("p1")

    assert result["started"] == 1
    assert result["retryable_failures"] == 1
    assert result["failed_to_start"][0]["episode_id"] == "e2"
    rows = {
        row["id"]: dict(row)
        for row in storyboard_db.execute(
            "SELECT id,status,active_storyboard_run_id FROM episodes ORDER BY id"
        ).fetchall()
    }
    assert rows["e1"]["status"] == "scripting"
    assert rows["e1"]["active_storyboard_run_id"]
    assert rows["e2"] == {
        "id": "e2",
        "status": "planned",
        "active_storyboard_run_id": None,
    }


@pytest.mark.asyncio
async def test_canonical_start_auto_resumes_existing_work(storyboard_db, monkeypatch):
    from app import task_registry

    storyboard_db.execute(
        "UPDATE episodes SET status='script_failed',script_error='可恢复' WHERE id='e1'"
    )
    storyboard_db.commit()
    preview = api.storyboard_start_preflight("e1", {})
    assert preview["action"] == "resume"

    def fake_spawn(_kind, _key, coro, *, project_id=None):
        coro.close()
        return object()

    monkeypatch.setattr(task_registry, "spawn", fake_spawn)
    result = await api.start_storyboard("e1", {
        "preflight_token": preview["preview_token"],
    })

    assert result["action"] == "resume"
    assert storyboard_db.execute(
        "SELECT COUNT(*) AS c FROM completion_grants"
    ).fetchone()["c"] == 0


def test_start_preflight_expires_when_state_drifts(storyboard_db):
    preview = api.storyboard_start_preflight("e1", {"mode": "resume"})
    storyboard_db.execute("UPDATE episodes SET status='planned' WHERE id='e1'")
    storyboard_db.commit()
    with pytest.raises(HTTPException) as caught:
        workspace.require_preview(preview["preview_token"], "start:resume", "e1")
    assert caught.value.status_code == 409


def test_running_state_cannot_acquire_edit_lease(storyboard_db):
    recorder = WorkflowRecorder.create(
        workflow_type="storyboard",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="active-edit-guard",
    )
    recorder.start()
    storyboard_db.execute(
        "UPDATE episodes SET status='scripting',active_storyboard_run_id=? WHERE id='e1'",
        (recorder.run_id,),
    )
    storyboard_db.commit()
    with pytest.raises(HTTPException) as caught:
        workspace.create_edit_session("s1")
    assert caught.value.status_code == 409


def test_paused_scripting_state_can_acquire_edit_lease(storyboard_db):
    storyboard_db.execute(
        """UPDATE episodes
              SET status='scripting',active_storyboard_run_id=NULL,
                  script_error='用户已暂停分镜任务'
            WHERE id='e1'"""
    )
    storyboard_db.commit()

    session = workspace.create_edit_session("s1")

    assert session["edit_session_token"].startswith("sblease_")


def test_stale_edit_session_is_rejected_without_borrowing_new_version(storyboard_db):
    session = workspace.create_edit_session("s1")
    newer = repository.create_artifact(EvidenceArtifact(
        type="storyboard_shot", scope_type="storyboard_checkpoint", scope_id="e1:1",
        status="validated", trust_level="T2", content={"shot_no": 1, "action_desc": "新版本"},
    ))
    storyboard_db.execute("UPDATE shots SET storyboard_artifact_id=? WHERE id='s1'", (newer["id"],))
    storyboard_db.commit()
    with pytest.raises(HTTPException) as caught:
        workspace.require_edit_session(session["edit_session_token"], "s1")
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "STALE_EDIT_BASELINE"


def test_source_binding_only_accepts_authorized_contiguous_range(storyboard_db):
    source = workspace.chapter_sources("e1")[0]
    start = source["content"].index("桌上的信")
    excerpt, normalized = workspace.validate_source_binding("e1", {
        "chapter_id": source["id"], "source_version_hash": source["source_version_hash"],
        "start_offset": start, "end_offset": start + len("桌上的信"),
    })
    assert excerpt == "桌上的信"
    workspace.persist_source_binding("s1", normalized)
    assert workspace.verify_or_bind_existing_excerpt("e1", "s1", excerpt)["chapter_idx"] == 1

    with pytest.raises(HTTPException):
        workspace.validate_source_binding("e1", {
            "chapter_id": source["id"], "source_version_hash": "wrong",
            "start_offset": start, "end_offset": start + 2,
        })
    with pytest.raises(HTTPException):
        workspace.validate_source_binding("e1", {
            "chapter_id": 999, "source_version_hash": source["source_version_hash"],
            "start_offset": 0, "end_offset": 2,
        })

    storyboard_db.execute("UPDATE chapters SET content=content || '新版' WHERE id=?", (source["id"],))
    storyboard_db.commit()
    with pytest.raises(HTTPException) as drifted:
        workspace.verify_or_bind_existing_excerpt("e1", "s1", excerpt)
    assert drifted.value.status_code == 409


def test_generated_source_binding_repair_replaces_stitched_excerpt(storyboard_db):
    stitched = "少年推开房门，看见桌上的信……神色骤然一沉。"
    storyboard_db.execute(
        "UPDATE shots SET source_excerpt=? WHERE id='s1'",
        (stitched,),
    )
    storyboard_db.commit()

    result = workspace.repair_generated_source_bindings("e1")

    assert result == {"bound": 1, "realigned": 1, "unresolved_shot_nos": []}
    repaired = storyboard_db.execute(
        "SELECT source_excerpt FROM shots WHERE id='s1'",
    ).fetchone()["source_excerpt"]
    assert repaired == "少年推开房门，看见桌上的信"
    assert workspace.verify_or_bind_existing_excerpt("e1", "s1", repaired)["chapter_idx"] == 1


def test_generated_source_binding_repair_falls_back_to_story_event_span(
    storyboard_db,
):
    episode = storyboard_db.execute(
        "SELECT screenplay_json FROM episodes WHERE id='e1'",
    ).fetchone()
    screenplay = json.loads(episode["screenplay_json"])
    screenplay["events"] = [{
        "event_id": "E1",
        "source_span": "chapter1：少年推开房门，看见桌上的信，神色骤然一沉。",
        "source_fact": "少年发现信件",
        "state_in": "门外",
        "trigger": "推门",
        "visible_change": "看见信件",
        "state_out": "神色一沉",
    }]
    row = storyboard_db.execute(
        "SELECT shot_contract_json FROM shots WHERE id='s1'",
    ).fetchone()
    contract = json.loads(row["shot_contract_json"])
    contract["event_ids"] = ["E1"]
    storyboard_db.execute(
        "UPDATE episodes SET screenplay_json=? WHERE id='e1'",
        (json.dumps(screenplay, ensure_ascii=False),),
    )
    storyboard_db.execute(
        "UPDATE shots SET source_excerpt=?,shot_contract_json=? WHERE id='s1'",
        (
            "少年进入房间后发现了改变情绪的重要物品。",
            json.dumps(contract, ensure_ascii=False),
        ),
    )
    storyboard_db.commit()

    result = workspace.repair_generated_source_bindings("e1")

    assert result == {"bound": 1, "realigned": 1, "unresolved_shot_nos": []}
    repaired = storyboard_db.execute(
        "SELECT source_excerpt FROM shots WHERE id='s1'",
    ).fetchone()["source_excerpt"]
    assert repaired == "少年推开房门，看见桌上的信，神色骤然一沉。"
    assert workspace.verify_or_bind_existing_excerpt(
        "e1", "s1", repaired,
    )["chapter_idx"] == 1


def test_edit_impact_preview_is_noop_safe_and_exact(storyboard_db):
    session = workspace.create_edit_session("s1")
    no_op = api.preview_shot_edit_impact("s1", {
        "edit_session_token": session["edit_session_token"],
        "changes": {"duration_s": 5},
    })
    assert no_op["unchanged"] is True
    assert "preview_token" not in no_op

    changed = api.preview_shot_edit_impact("s1", {
        "edit_session_token": session["edit_session_token"],
        "changes": {"duration_s": 6},
    })
    assert changed["unchanged"] is False
    assert changed["changed_fields"] == ["duration_s"]
    assert changed["preview_token"].startswith("sbpv_")
    assert 1 in changed["revalidation_shots"]


def test_shot_edit_commits_deterministic_gate_without_treating_authorship_as_failure(storyboard_db):
    session = workspace.create_edit_session("s1")
    changes = {"camera_move": "缓慢推近"}
    preview = api.preview_shot_edit_impact("s1", {
        "edit_session_token": session["edit_session_token"],
        "changes": changes,
    })

    result = asyncio.run(api.edit_shot("s1", {
        **changes,
        "expected_version": session["baseline_artifact_id"],
        "edit_session_token": session["edit_session_token"],
        "preview_token": preview["preview_token"],
        "baseline_content_hash": session["baseline_content_hash"],
        "change_source": "test_edit",
    }))

    assert result["ok"] is True
    assert storyboard_db.execute("SELECT camera_move FROM shots WHERE id='s1'").fetchone()[0] == "缓慢推近"
    evaluations = repository.get_evaluations(result["artifact_id"])
    assert any(row["evaluator_name"] == "storyboard_editor" and not row["hard_gate_passed"] for row in evaluations)
    assert any(row["evaluator_name"] == "storyboard_shot_business_gate" and row["hard_gate_passed"] for row in evaluations)


def test_manual_shot_edit_rejects_review_pointer_without_published_authority(
    storyboard_db,
):
    lineage = _seed_narrative_review_lineage(storyboard_db)
    session = workspace.create_edit_session("s1")
    changes = {"camera_move": "缓慢推近"}
    before = dict(storyboard_db.execute("SELECT * FROM shots WHERE id='s1'").fetchone())
    with pytest.raises(HTTPException) as caught:
        api.preview_shot_edit_impact("s1", {
            "edit_session_token": session["edit_session_token"],
            "changes": changes,
        })

    episode = storyboard_db.execute(
        "SELECT narrative_status,narrative_review_artifact_id FROM episodes WHERE id='e1'"
    ).fetchone()
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "storyboard_screenplay_authority_invalid"
    assert dict(storyboard_db.execute("SELECT * FROM shots WHERE id='s1'").fetchone()) == before
    assert episode["narrative_status"] == "ready"
    assert episode["narrative_review_artifact_id"] == lineage["report"]
    assert {
        row["id"]: row["status"]
        for row in storyboard_db.execute(
            "SELECT id,status FROM artifacts WHERE id IN (?,?,?,?)",
            (
                lineage["review_input"],
                lineage["observation"],
                lineage["report"],
                lineage["future_consumer"],
            ),
        ).fetchall()
    } == {
        lineage["review_input"]: "validated",
        lineage["observation"]: "validated",
        lineage["report"]: "validated",
        lineage["future_consumer"]: "validated",
    }


def test_manual_duration_edit_records_reviewed_duration_contract(storyboard_db):
    session = workspace.create_edit_session("s1")
    changes = {"duration_s": 10}
    preview = api.preview_shot_edit_impact("s1", {
        "edit_session_token": session["edit_session_token"],
        "changes": changes,
    })

    asyncio.run(api.edit_shot("s1", {
        **changes,
        "expected_version": session["baseline_artifact_id"],
        "edit_session_token": session["edit_session_token"],
        "preview_token": preview["preview_token"],
        "baseline_content_hash": session["baseline_content_hash"],
        "change_source": "test_manual_duration",
    }))

    row = storyboard_db.execute(
        "SELECT duration_s,shot_contract_json FROM shots WHERE id='s1'"
    ).fetchone()
    contract = json.loads(row["shot_contract_json"])
    assert row["duration_s"] == 10
    assert "duration_human_reviewed" in contract["risk_tags"]


def test_manual_dialogue_edit_cannot_reintroduce_unresolved_descriptive_identity(storyboard_db):
    bible = Bible(
        characters=[Character(
            name="少年",
            role="主角",
            appearance_canonical="十六岁黑发少年，蓝色长衫，身形清瘦，目光坚定，衣着朴素整洁",
            personality="坚定",
        )],
        world=World(
            era="古代",
            genre="剧情",
            visual_style_canonical="国风动画电影感，光影稳定克制",
        ),
    )
    storyboard_db.execute(
        "UPDATE projects SET bible_json=? WHERE id='p1'", (bible.model_dump_json(),)
    )
    storyboard_db.execute(
        "UPDATE episodes SET storyboard_warning='上次确认的缺失台词警告' WHERE id='e1'"
    )
    storyboard_db.commit()
    session = workspace.create_edit_session("s1")
    changes = {
        "action_desc": "绿袍男子站在少年面前厉声警告，少年紧张地看向他。",
        "dialogues": [{
            "speaker": "绿袍男子",
            "line": "再说一句废话，直接割了你的舌头。",
            "emotion": "冷厉",
            "delivery": "spoken_dialogue",
        }],
    }
    preview = api.preview_shot_edit_impact("s1", {
        "edit_session_token": session["edit_session_token"],
        "changes": changes,
    })

    before = dict(storyboard_db.execute("SELECT * FROM shots WHERE id='s1'").fetchone())
    with pytest.raises(HTTPException) as caught:
        asyncio.run(api.edit_shot("s1", {
            **changes,
            "expected_version": session["baseline_artifact_id"],
            "edit_session_token": session["edit_session_token"],
            "preview_token": preview["preview_token"],
            "baseline_content_hash": session["baseline_content_hash"],
            "change_source": "regression_described_extra_dialogue",
        }))

    assert caught.value.status_code == 422
    assert caught.value.detail["code"] == "storyboard_character_identity_unresolved"
    row = dict(storyboard_db.execute("SELECT * FROM shots WHERE id='s1'").fetchone())
    assert row == before
    assert storyboard_db.execute(
        "SELECT storyboard_warning FROM episodes WHERE id='e1'"
    ).fetchone()[0] == "上次确认的缺失台词警告"


def test_free_text_source_edit_is_rejected(storyboard_db):
    session = workspace.create_edit_session("s1")
    with pytest.raises(HTTPException) as caught:
        api.preview_shot_edit_impact("s1", {
            "edit_session_token": session["edit_session_token"],
            "changes": {"source_excerpt": "我自己编一段原文"},
        })
    assert caught.value.status_code == 422


def test_structure_preview_guards_unique_and_final_shot(storyboard_db):
    with pytest.raises(HTTPException) as unique:
        api.preview_storyboard_structure("e1", {"operation": "delete", "shot_id": "s1"})
    assert "唯一镜头" in str(unique.value.detail)

    preview = api.preview_storyboard_structure("e1", {
        "operation": "duplicate_after", "shot_id": "s1", "target_index": 0,
    })
    assert preview["before_count"] == 1
    assert preview["after_count"] == 2
    assert preview["requires_reconfirm"] is True


def test_structure_commit_keeps_contiguous_numbers_and_one_final(storyboard_db):
    preview = api.preview_storyboard_structure("e1", {
        "operation": "duplicate_after", "shot_id": "s1", "target_index": 0,
    })
    result = api.apply_storyboard_structure("e1", {
        "preview_token": preview["preview_token"], "operation": "duplicate_after",
        "shot_id": "s1", "target_index": 0, "new_final_shot_id": None,
    })
    assert result["shot_count"] == 2
    rows = storyboard_db.execute(
        "SELECT shot_no,shot_contract_json FROM shots WHERE episode_id='e1' ORDER BY shot_no"
    ).fetchall()
    assert [row["shot_no"] for row in rows] == [1, 2]
    assert sum(bool(json.loads(row["shot_contract_json"] or "{}").get("is_final")) for row in rows) == 1
    episode = storyboard_db.execute(
        "SELECT status,storyboard_outline_json FROM episodes WHERE id='e1'"
    ).fetchone()
    assert episode["status"] == "scripted"
    assert len(json.loads(episode["storyboard_outline_json"])["shots"]) == 2
    status = api.episode_detail("e1", view="board")["storyboard_status"]
    assert status["planned_shots"] == 2
    assert status["produced_shots"] == 2
    assert status["editable"] is True


def test_structure_mutation_rejects_review_pointer_without_published_authority(storyboard_db):
    lineage = _seed_narrative_review_lineage(storyboard_db)
    with pytest.raises(HTTPException) as caught:
        api.preview_storyboard_structure("e1", {
            "operation": "duplicate_after", "shot_id": "s1", "target_index": 0,
        })

    episode = storyboard_db.execute(
        "SELECT narrative_status,narrative_review_artifact_id FROM episodes WHERE id='e1'"
    ).fetchone()
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "storyboard_screenplay_authority_invalid"
    assert episode["narrative_status"] == "ready"
    assert episode["narrative_review_artifact_id"] == lineage["report"]
    statuses = {
        row["id"]: row["status"]
        for row in storyboard_db.execute(
            "SELECT id,status FROM artifacts WHERE id IN (?,?,?,?)",
            (
                lineage["review_input"],
                lineage["observation"],
                lineage["report"],
                lineage["future_consumer"],
            ),
        ).fetchall()
    }
    assert set(statuses.values()) == {"validated"}
    assert storyboard_db.execute(
        "SELECT COUNT(*) FROM shots WHERE episode_id='e1'"
    ).fetchone()[0] == 1


def test_structure_move_delete_and_add_keep_atomic_plan(storyboard_db):
    duplicate = api.preview_storyboard_structure("e1", {
        "operation": "duplicate_after", "shot_id": "s1", "target_index": 0,
    })
    created = api.apply_storyboard_structure("e1", {
        "preview_token": duplicate["preview_token"], "operation": "duplicate_after",
        "shot_id": "s1", "target_index": 0, "new_final_shot_id": None,
    })["created_shot_id"]

    move = api.preview_storyboard_structure("e1", {
        "operation": "move", "shot_id": created, "target_index": 0,
    })
    api.apply_storyboard_structure("e1", {
        "preview_token": move["preview_token"], "operation": "move",
        "shot_id": created, "target_index": 0, "new_final_shot_id": None,
    })
    assert storyboard_db.execute(
        "SELECT id FROM shots WHERE episode_id='e1' ORDER BY shot_no LIMIT 1"
    ).fetchone()[0] == created

    delete = api.preview_storyboard_structure("e1", {
        "operation": "delete", "shot_id": created,
    })
    api.apply_storyboard_structure("e1", {
        "preview_token": delete["preview_token"], "operation": "delete",
        "shot_id": created, "target_index": delete["target_index"], "new_final_shot_id": None,
    })
    add = api.preview_storyboard_structure("e1", {
        "operation": "add_after", "shot_id": "s1", "target_index": 0,
    })
    api.apply_storyboard_structure("e1", {
        "preview_token": add["preview_token"], "operation": "add_after",
        "shot_id": "s1", "target_index": 0, "new_final_shot_id": None,
    })
    rows = storyboard_db.execute(
        "SELECT shot_no,shot_contract_json FROM shots WHERE episode_id='e1' ORDER BY shot_no"
    ).fetchall()
    assert [row["shot_no"] for row in rows] == [1, 2]
    assert sum(bool(json.loads(row["shot_contract_json"] or "{}").get("is_final")) for row in rows) == 1


def test_confirmation_preview_is_rejected_after_version_drift(storyboard_db):
    preview = workspace.create_preview("confirm", "e1", {"hard_gates": {"passed": True}})
    storyboard_db.execute("UPDATE shots SET duration_s=6 WHERE id='s1'")
    storyboard_db.commit()
    with pytest.raises(HTTPException) as caught:
        workspace.require_preview(preview["preview_token"], "confirm", "e1")
    assert caught.value.status_code == 409


def test_confirmation_preview_is_rejected_after_rate_drift(storyboard_db, monkeypatch):
    from app import config

    preview = workspace.create_preview("confirm", "e1", {"hard_gates": {"passed": True}})
    monkeypatch.setattr(config, "VIDEO_PRICE_PER_SECOND", config.VIDEO_PRICE_PER_SECOND + 0.1)
    with pytest.raises(HTTPException) as caught:
        workspace.require_preview(preview["preview_token"], "confirm", "e1")
    assert caught.value.status_code == 409


def test_confirmation_preview_is_rejected_after_source_version_drift(storyboard_db):
    preview = workspace.create_preview("confirm", "e1", {"hard_gates": {"passed": True}})
    storyboard_db.execute("UPDATE chapters SET content=content || '变更' WHERE project_id='p1' AND idx=1")
    storyboard_db.commit()
    with pytest.raises(HTTPException) as caught:
        workspace.require_preview(preview["preview_token"], "confirm", "e1")
    assert caught.value.status_code == 409


def test_emergency_readonly_flag_preserves_browsing_and_blocks_writes(storyboard_db):
    storyboard_db.execute(
        "UPDATE settings SET value='true' WHERE key='storyboard_workspace_safe_readonly'"
    )
    storyboard_db.commit()
    detail = api.episode_detail("e1", view="board")
    assert len(detail["shots"]) == 1
    assert detail["storyboard_status"]["state"] == "syncing"
    assert detail["storyboard_status"]["editable"] is False
    assert detail["storyboard_status"]["confirmable"] is False


def test_failed_draft_is_listed_and_published_version_unchanged(storyboard_db):
    draft = repository.create_artifact(EvidenceArtifact(
        type="storyboard_shot", scope_type="storyboard_checkpoint", scope_id="e1:1",
        status="needs_revision", trust_level="T1", content={"shot_no": 1, "duration_s": 9},
        parent_artifact_ids=[storyboard_db.execute("SELECT storyboard_artifact_id FROM shots WHERE id='s1'").fetchone()[0]],
    ))
    before = storyboard_db.execute("SELECT storyboard_artifact_id FROM shots WHERE id='s1'").fetchone()[0]
    items = api.list_shot_edit_drafts("s1")["items"]
    assert items[0]["id"] == draft["id"]
    assert items[0]["content"]["duration_s"] == 9
    assert storyboard_db.execute("SELECT storyboard_artifact_id FROM shots WHERE id='s1'").fetchone()[0] == before


def test_confirmation_preview_blocks_non_terminal_episode(storyboard_db):
    storyboard_db.execute("UPDATE episodes SET status='script_failed', script_error='尚有问题' WHERE id='e1'")
    storyboard_db.commit()
    with pytest.raises(HTTPException) as caught:
        api.create_storyboard_confirmation_preview("e1")
    assert caught.value.status_code == 409
    preview = caught.value.detail
    assert preview["hard_gates"]["passed"] is False
    assert any("尚未达到完整终态" in error for error in preview["hard_gates"]["errors"])
    assert "preview_token" not in preview


def test_confirmation_reports_hard_errors_and_score_only_warnings(storyboard_db):
    storyboard_db.execute(
        "UPDATE episodes SET status='script_failed', script_error='尚有问题' WHERE id='e1'"
    )
    storyboard_db.execute(
        "UPDATE shots SET duration_s=8, dialogues=? WHERE id='s1'",
        (json.dumps([{
            "speaker": "少年",
            "line": "我要把这封信从头到尾认真地念完后再做决定",
            "emotion": "凝重",
            "delivery": "spoken_dialogue",
        }], ensure_ascii=False),),
    )
    storyboard_db.commit()

    with pytest.raises(HTTPException) as caught:
        api.create_storyboard_confirmation_preview("e1")
    preview = caught.value.detail
    warnings = preview["warnings"]
    assert "存在超过 5 秒的镜头，已纳入 QA 评分报告" not in warnings
    hard_errors = preview["hard_gates"]["errors"]
    assert hard_errors
    assert any("尚未达到完整终态" in error for error in hard_errors)
    assert any("低于硬下限" in str(w) or "action_desc" in str(w) for w in warnings)


def test_confirmation_rebuilds_episode_artifact_after_manual_shot_artifact_change(storyboard_db):
    old_episode_artifact = storyboard_db.execute(
        "SELECT storyboard_artifact_id FROM episodes WHERE id='e1'"
    ).fetchone()[0]
    current_shot_artifact = storyboard_db.execute(
        "SELECT storyboard_artifact_id FROM shots WHERE id='s1'"
    ).fetchone()[0]
    preview = api.create_storyboard_confirmation_preview("e1")

    result = api.confirm_episode_core("e1", preview_token=preview["preview_token"])

    episode = storyboard_db.execute(
        "SELECT status,storyboard_artifact_id,published_storyboard_artifact_id "
        "FROM episodes WHERE id='e1'"
    ).fetchone()
    assert result["confirmed"] is True
    assert episode["status"] == "confirmed"
    assert episode["storyboard_artifact_id"] != old_episode_artifact
    assert episode["published_storyboard_artifact_id"] == episode["storyboard_artifact_id"]
    artifact = repository.get_artifact(episode["storyboard_artifact_id"])
    assert current_shot_artifact in artifact["parent_artifact_ids"]


def test_idempotent_confirmation_converges_terminal_runtime_state(storyboard_db):
    recorder = _cancel_test_run(storyboard_db)
    orphan = WorkflowRecorder.create(
        workflow_type="storyboard",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="orphan-paused-run",
    )
    orphan.start()
    orphan.pause_external("服务重启后遗留")
    storyboard_db.execute("UPDATE episodes SET status='confirmed' WHERE id='e1'")
    storyboard_db.commit()

    result = api.confirm_episode_core("e1")

    assert result["confirmed"] is True
    assert result["idempotent"] is True
    episode = storyboard_db.execute(
        "SELECT active_storyboard_run_id,script_error FROM episodes WHERE id='e1'"
    ).fetchone()
    assert episode["active_storyboard_run_id"] is None
    assert episode["script_error"] is None
    run_states = {
        row["id"]: row["status"]
        for row in storyboard_db.execute(
            "SELECT id,status FROM workflow_runs WHERE id IN (?,?)",
            (recorder.run_id, orphan.run_id),
        ).fetchall()
    }
    assert run_states == {recorder.run_id: "CANCELLED", orphan.run_id: "CANCELLED"}
    assert api._review_upstream_snapshot("e1")["eligible_for_production"] is True
    checkpoint = load_latest_checkpoint("e1")
    assert checkpoint.phase == "SUCCEEDED"
    assert checkpoint.outcome == "SUCCEEDED_READY_FOR_CONFIRM"


def test_preview_consume_is_atomic_under_concurrent_submit(storyboard_db):
    preview = workspace.create_preview("test_atomic", "e1", {"ok": True})
    barrier = threading.Barrier(2)
    results: list[str] = []

    def consume() -> None:
        barrier.wait()
        try:
            workspace.require_preview(
                preview["preview_token"],
                "test_atomic",
                "e1",
                consume=True,
            )
            results.append("accepted")
        except HTTPException as exc:
            results.append(f"rejected:{exc.status_code}")
        finally:
            thread_conn = db.get_conn()
            thread_conn.close()
            db._local.conn = None

    threads = [threading.Thread(target=consume) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == ["accepted", "rejected:409"]


def test_preview_fingerprint_covers_independent_shot_fields(storyboard_db):
    preview = workspace.create_preview("test_full_fingerprint", "e1", {"ok": True})
    storyboard_db.execute(
        "UPDATE shots SET camera_move='横摇',scene_time='黄昏' WHERE id='s1'"
    )
    storyboard_db.commit()

    with pytest.raises(HTTPException) as caught:
        workspace.require_preview(
            preview["preview_token"],
            "test_full_fingerprint",
            "e1",
        )

    assert caught.value.status_code == 409
    assert "基线已变化" in str(caught.value.detail)


def test_active_video_run_blocks_edit_and_structure(storyboard_db):
    recorder = WorkflowRecorder.create(
        workflow_type="episode_video_completion",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="active-video-write-fence",
    )
    recorder.start()
    storyboard_db.execute(
        "UPDATE episodes SET status='generating',active_video_run_id=? WHERE id='e1'",
        (recorder.run_id,),
    )
    storyboard_db.commit()

    with pytest.raises(HTTPException) as edit_error:
        workspace.create_edit_session("s1")
    with pytest.raises(HTTPException) as structure_error:
        api.preview_storyboard_structure(
            "e1",
            {"operation": "duplicate_after", "shot_id": "s1", "target_index": 0},
        )

    assert edit_error.value.status_code == 409
    assert structure_error.value.status_code == 409
    assert "视频任务" in str(edit_error.value.detail)
    recorder.cancel("test cleanup")


def test_shot_edit_rolls_back_projection_and_preview_when_evidence_fails(
    storyboard_db,
    monkeypatch,
):
    session = workspace.create_edit_session("s1")
    changes = {"camera_move": "缓慢推近"}
    preview = api.preview_shot_edit_impact(
        "s1",
        {
            "edit_session_token": session["edit_session_token"],
            "changes": changes,
        },
    )
    before = dict(storyboard_db.execute("SELECT * FROM shots WHERE id='s1'").fetchone())

    def fail_evidence(*_args, **_kwargs):
        raise RuntimeError("injected evidence failure")

    monkeypatch.setattr(
        repository,
        "create_and_commit_artifact_in_transaction",
        fail_evidence,
    )
    with pytest.raises(RuntimeError, match="injected evidence failure"):
        asyncio.run(api.edit_shot("s1", {
            **changes,
            "expected_version": session["baseline_artifact_id"],
            "edit_session_token": session["edit_session_token"],
            "preview_token": preview["preview_token"],
            "baseline_content_hash": session["baseline_content_hash"],
        }))

    assert dict(storyboard_db.execute("SELECT * FROM shots WHERE id='s1'").fetchone()) == before
    token = storyboard_db.execute(
        "SELECT consumed_at FROM storyboard_action_previews WHERE token=?",
        (preview["preview_token"],),
    ).fetchone()
    assert token["consumed_at"] is None
    assert storyboard_db.execute(
        "SELECT COUNT(*) FROM media_cleanup_outbox"
    ).fetchone()[0] == 0


def test_structure_rolls_back_rows_cleanup_and_preview_when_evidence_fails(
    storyboard_db,
    monkeypatch,
):
    preview = api.preview_storyboard_structure(
        "e1",
        {"operation": "duplicate_after", "shot_id": "s1", "target_index": 0},
    )
    before = [
        dict(row)
        for row in storyboard_db.execute(
            "SELECT * FROM shots WHERE episode_id='e1' ORDER BY shot_no"
        ).fetchall()
    ]

    def fail_rebind(*_args, **_kwargs):
        raise RuntimeError("injected structure evidence failure")

    monkeypatch.setattr(api, "_ensure_current_storyboard_shot_artifacts", fail_rebind)
    with pytest.raises(RuntimeError, match="injected structure evidence failure"):
        api.apply_storyboard_structure("e1", {
            "preview_token": preview["preview_token"],
            "operation": "duplicate_after",
            "shot_id": "s1",
            "target_index": 0,
            "new_final_shot_id": None,
        })

    after = [
        dict(row)
        for row in storyboard_db.execute(
            "SELECT * FROM shots WHERE episode_id='e1' ORDER BY shot_no"
        ).fetchall()
    ]
    assert after == before
    assert storyboard_db.execute(
        "SELECT COUNT(*) FROM media_cleanup_outbox"
    ).fetchone()[0] == 0
    assert storyboard_db.execute(
        "SELECT consumed_at FROM storyboard_action_previews WHERE token=?",
        (preview["preview_token"],),
    ).fetchone()["consumed_at"] is None


def test_gate_evaluator_exception_is_system_error_not_repair_action(
    storyboard_db,
    monkeypatch,
):
    def fail_gate(*_args, **_kwargs):
        raise RuntimeError("injected evaluator outage")

    monkeypatch.setattr(api, "evaluate_storyboard_for_confirmation", fail_gate)
    status = api.episode_detail("e1", view="board")["storyboard_status"]

    assert status["state"] == "syncing"
    assert status["recommended_action"] == "refresh_status"
    assert status["hard_gate_issues"] == []
    assert "injected evaluator outage" in status["system_error"]
    assert status["editable"] is False


def test_confirmation_gate_failure_does_not_mutate_projection_or_leave_owner(
    storyboard_db,
    monkeypatch,
):
    preview = api.create_storyboard_confirmation_preview("e1")
    before_episode = dict(
        storyboard_db.execute("SELECT * FROM episodes WHERE id='e1'").fetchone()
    )
    before_shot = dict(
        storyboard_db.execute("SELECT * FROM shots WHERE id='s1'").fetchone()
    )

    def fail_gate(*_args, **_kwargs):
        raise RuntimeError("injected submit gate failure")

    monkeypatch.setattr(api, "evaluate_storyboard_for_confirmation", fail_gate)
    with pytest.raises(RuntimeError, match="injected submit gate failure"):
        api.confirm_episode_core("e1", preview_token=preview["preview_token"])

    after_episode = dict(
        storyboard_db.execute("SELECT * FROM episodes WHERE id='e1'").fetchone()
    )
    after_shot = dict(
        storyboard_db.execute("SELECT * FROM shots WHERE id='s1'").fetchone()
    )
    assert after_shot == before_shot
    assert after_episode["status"] == before_episode["status"]
    assert after_episode["target_duration_s"] == before_episode["target_duration_s"]
    assert after_episode["storyboard_artifact_id"] == before_episode["storyboard_artifact_id"]
    assert after_episode["active_storyboard_run_id"] is None


def test_snapshot_versions_are_distinct_under_concurrent_state_changes(storyboard_db):
    baseline = workspace.monotonic_snapshot_version("e1", "snapshot-baseline")
    barrier = threading.Barrier(2)
    versions: list[int] = []

    def update_snapshot(fingerprint: str) -> None:
        barrier.wait()
        versions.append(
            workspace.monotonic_snapshot_version("e1", fingerprint)
        )
        thread_conn = db.get_conn()
        thread_conn.close()
        db._local.conn = None

    threads = [
        threading.Thread(target=update_snapshot, args=("snapshot-a",)),
        threading.Thread(target=update_snapshot, args=("snapshot-b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(versions) == [baseline + 1, baseline + 2]


def test_starting_owner_is_treated_as_live_and_not_replaced(
    storyboard_db,
    monkeypatch,
):
    owner = "starting:123:storyboard-test"
    storyboard_db.execute(
        "UPDATE episodes SET status='scripting',active_storyboard_run_id=? WHERE id='e1'",
        (owner,),
    )
    storyboard_db.commit()

    def unexpected_recorder(*_args, **_kwargs):
        raise AssertionError("live starting owner must not be replaced")

    monkeypatch.setattr(api, "_new_storyboard_recorder", unexpected_recorder)
    with enter_handler():
        result = asyncio.run(api.start_storyboard("e1"))

    assert result["deduplicated"] is True
    assert result["run_id"] == owner
    assert storyboard_db.execute(
        "SELECT active_storyboard_run_id FROM episodes WHERE id='e1'"
    ).fetchone()[0] == owner


def test_media_cleanup_outbox_keeps_files_until_database_commit(
    storyboard_db,
    tmp_path,
    monkeypatch,
):
    from app import artifacts

    projects_root = tmp_path / "projects"
    monkeypatch.setattr(artifacts.config, "PROJECTS_DIR", projects_root)
    reference_dir = projects_root / "p1" / "episodes" / "1" / "shots" / "1" / "references"
    reference_dir.mkdir(parents=True)
    reference_file = reference_dir / "reference.png"
    reference_file.write_bytes(b"reference")
    video_file = tmp_path / "shot.mp4"
    video_file.write_bytes(b"video")
    storyboard_db.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,video_path,created_at
           ) VALUES('v-cleanup','s1',1,'prompt','idem','done',?,1)""",
        (str(video_file),),
    )
    storyboard_db.commit()

    storyboard_db.execute("BEGIN IMMEDIATE")
    staged = artifacts.stage_shot_artifact_cleanup(storyboard_db, "s1")
    assert reference_file.exists()
    assert video_file.exists()
    assert storyboard_db.execute(
        "SELECT COUNT(*) FROM shot_versions WHERE shot_id='s1'"
    ).fetchone()[0] == 0
    storyboard_db.commit()

    assert artifacts.flush_media_cleanup_outbox(staged["outbox_id"]) is True
    assert not reference_file.exists()
    assert not video_file.exists()
    assert storyboard_db.execute(
        "SELECT status FROM media_cleanup_outbox WHERE id=?",
        (staged["outbox_id"],),
    ).fetchone()[0] == "completed"
