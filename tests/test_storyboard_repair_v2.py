from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from app import db
from app.domain.storyboard_ops import (
    _board_from_shot_rows,
    _ensure_current_storyboard_shot_artifacts,
    _finalize_storyboard_evidence,
    _insert_storyboard_shot,
    _new_storyboard_recorder,
    _storyboard_shot_evidence_requires_rebind,
)
from app.evidence import repository as evidence_repository
from app.harness.types import EvidenceArtifact
from app.orchestration.engine import WorkflowRecorder
from app.production.revision import ensure_production_revision
from app.repair_router import route_issues
from app.schemas import (
    AudioTimelineItem,
    Bible,
    Character,
    Dialogue,
    EpisodeScreenplay,
    Shot,
    Storyboard,
    StoryboardOutline,
    StoryboardOutlineShot,
    StoryboardContextRequirement,
    StoryboardSceneContext,
    StoryboardScenePack,
    World,
)
from app.source_excerpt import align_source_excerpt
from app.storyboard_control import request_control
from app.storyboard_supervisor import (
    STORYBOARD_REPAIR_PLANNER_VERSION,
    SupervisorCheckpoint,
    _annotate_blind_review_repair,
    _apply_storyboard_planning_target,
    _apply_repair,
    _begin_repair_activation,
    _commit_repair_candidate,
    _deterministic_ambient_audio_cast_candidate,
    _deterministic_dialogue_framing_candidate,
    _merge_repair_candidate,
    _migrate_checkpoint,
    _open_shot_gap,
    _repair_is_pending,
    _repair_feedback_for_shot,
    _recover_outline_from_current_artifact,
    _recover_truncated_outline_from_approved_artifact,
    _repair_candidate_made_progress,
    _retarget_spine_repair_brief,
    _retarget_spine_repair_shot,
    _shot_checkpoint_payload,
    _storyboard_bible_snapshot,
    _storyboard_generation_is_complete,
    _storyboard_hash,
    _validated_candidate_projection,
    _withdraw_legacy_failed_publication,
    prepare_published_storyboard_repair,
    run_storyboard_supervisor,
    save_checkpoint,
)


def test_blind_review_annotation_preserves_pending_repair_lifecycle() -> None:
    checkpoint = SupervisorCheckpoint(
        episode_id="e1",
        phase="REPAIRING",
        last_repair={"status": "candidate_pending", "window_start": 69},
    )

    _annotate_blind_review_repair(checkpoint, ["review evidence missing"])

    assert _repair_is_pending(checkpoint)
    assert checkpoint.last_repair == {
        "status": "candidate_pending",
        "window_start": 69,
        "review_status": "blind_review_repair_planned",
        "blind_review_errors": ["review evidence missing"],
    }


def test_blind_review_annotation_keeps_paused_state_non_pending() -> None:
    checkpoint = SupervisorCheckpoint(
        episode_id="e1",
        phase="WAITING_HUMAN",
        last_repair={"status": "paused"},
    )

    _annotate_blind_review_repair(checkpoint, ["review failed"])

    assert not _repair_is_pending(checkpoint)
    assert checkpoint.last_repair["status"] == "paused"
    assert checkpoint.last_repair["review_status"] == "blind_review_failed_paused"


def test_cancelled_run_cannot_overwrite_storyboard_checkpoint(repair_db) -> None:
    conn, _screenplay = repair_db
    recorder = WorkflowRecorder.create(
        workflow_type="storyboard",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="checkpoint-owner",
    )
    recorder.start()
    conn.execute(
        "UPDATE episodes SET active_storyboard_run_id=? WHERE id='e1'",
        (recorder.run_id,),
    )
    conn.commit()
    checkpoint = SupervisorCheckpoint(
        episode_id="e1",
        phase="VALIDATING_EPISODE",
        validated_prefix_end=1,
        next_shot_no=2,
        expected_total=1,
    )
    current_id = save_checkpoint(
        checkpoint,
        run_id=recorder.run_id,
    )
    recorder.cancel("superseded")
    checkpoint.phase = "WAITING_HUMAN"
    checkpoint.outcome = "STALE_RESULT"

    returned_id = save_checkpoint(
        checkpoint,
        run_id=recorder.run_id,
    )

    assert returned_id == current_id
    rows = conn.execute(
        """SELECT content_json FROM artifacts
             WHERE type='storyboard_supervisor_checkpoint'
               AND scope_id='e1'"""
    ).fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["content_json"])["phase"] == (
        "VALIDATING_EPISODE"
    )


def test_stale_run_cannot_write_after_model_await(repair_db, monkeypatch) -> None:
    from app.stages import StoryboardShotDraft
    import app.storyboard_supervisor as supervisor_module

    conn, _screenplay = repair_db
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=number,
                scene_setting="日，广场",
                beat=f"少年完成第{number}步动作",
            )
            for number in range(1, 5)
        ],
    )
    conn.execute(
        "UPDATE episodes SET storyboard_outline_json=? WHERE id='e1'",
        (outline.model_dump_json(),),
    )
    stale = WorkflowRecorder.create(
        workflow_type="storyboard",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="stale-owner",
    )
    stale.start()
    current = WorkflowRecorder.create(
        workflow_type="storyboard",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="current-owner",
    )
    current.start()
    conn.execute(
        "UPDATE episodes SET status='scripting',active_storyboard_run_id=? WHERE id='e1'",
        (stale.run_id,),
    )
    conn.commit()
    save_checkpoint(
        SupervisorCheckpoint(
            episode_id="e1",
            phase="GENERATING_SHOTS",
            validated_prefix_end=3,
            next_shot_no=4,
            expected_total=4,
            input_versions={"screenplay_artifact_id": "sp1"},
        ),
        run_id=stale.run_id,
    )

    async def lose_ownership_during_generation(*_args, **_kwargs):
        conn.execute(
            """UPDATE episodes
                  SET status='scripting',script_error='current owner is running',
                      active_storyboard_run_id=?
                WHERE id='e1'""",
            (current.run_id,),
        )
        conn.commit()
        return StoryboardShotDraft(
            episode_no=1,
            shot=_shot(4).model_copy(update={"is_final": True}),
            is_final=True,
        )

    monkeypatch.setattr(
        supervisor_module,
        "generate_storyboard_next_shot",
        lose_ownership_during_generation,
    )

    asyncio.run(run_storyboard_supervisor(
        "e1",
        resume=True,
        run_id=stale.run_id,
        preflight_done=True,
    ))

    episode = conn.execute(
        "SELECT status,script_error,active_storyboard_run_id FROM episodes WHERE id='e1'"
    ).fetchone()
    assert episode["active_storyboard_run_id"] == current.run_id
    assert episode["status"] == "scripting"
    assert episode["script_error"] == "current owner is running"
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id='e1'"
    ).fetchone()["c"] == 3


@pytest.mark.parametrize("narrative_authority", [False, True])
def test_storyboard_planning_target_preserves_published_narrative_duration(
    repair_db,
    narrative_authority: bool,
) -> None:
    conn, _screenplay_value = repair_db
    episode_data = dict(
        conn.execute("SELECT * FROM episodes WHERE id='e1'").fetchone()
    )

    _apply_storyboard_planning_target(
        conn,
        "e1",
        episode_data,
        60,
        narrative_authority=narrative_authority,
    )

    assert episode_data["target_duration_s"] == 60
    persisted = conn.execute(
        "SELECT target_duration_s FROM episodes WHERE id='e1'"
    ).fetchone()["target_duration_s"]
    assert persisted == (15 if narrative_authority else 60)


@pytest.fixture()
def repair_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "storyboard-repair-v2.db")
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_local", threading.local())
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id,name,status,created_at) VALUES('p1','P','planned',1)"
    )
    conn.execute(
        """INSERT INTO chapters(project_id,idx,title,content,char_count,cleaned_lines)
           VALUES('p1',1,'第一章',?,100,0)""",
        ("少年走到石碑前，抬头查看上面显现的清晰结果。" * 6,),
    )
    screenplay = EpisodeScreenplay(
        id="script-1",
        episode_no=1,
        title="E",
        full_script_text="日，广场。少年走到石碑前，抬头查看结果。",
    )
    conn.execute(
        """INSERT INTO episodes(
               id,project_id,episode_no,title,source_chapters,target_duration_s,
               screenplay_json,screenplay_status,screenplay_artifact_id,status,created_at
           ) VALUES('e1','p1',1,'E','[1]',15,?,'ready','sp1','scripted',1)""",
        (screenplay.model_dump_json(),),
    )
    for number in range(1, 4):
        shot = _shot(number)
        conn.execute(
            """INSERT INTO shots(
                   id,episode_id,script_id,shot_no,duration_s,shot_size,camera_move,
                   scene_setting,characters,action_desc,first_frame_desc,last_frame_desc,
                   source_excerpt,narration,dialogues,transition,continuity_from_prev,
                   shot_contract_json,continuity_mode,observed_state_out
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"s{number}", "e1", "script-1", number, shot.duration_s,
                shot.shot_size, shot.camera_move, shot.scene_setting,
                json.dumps(shot.characters, ensure_ascii=False), shot.action_desc,
                shot.first_frame_desc, shot.last_frame_desc, shot.source_excerpt,
                shot.narration, "[]", shot.transition, 0,
                json.dumps(shot.model_dump(mode="json"), ensure_ascii=False),
                shot.continuity_mode, shot.observed_state_out,
            ),
        )
    conn.commit()
    yield conn, screenplay
    conn.close()


def test_storyboard_bible_snapshot_uses_checkpoint_artifact(monkeypatch) -> None:
    current = Bible(
        characters=[],
        world=World(visual_style_canonical="current-style"),
    )
    frozen = Bible(
        characters=[],
        world=World(visual_style_canonical="frozen-style"),
    )
    cp = SupervisorCheckpoint(
        episode_id="e1",
        input_versions={"bible_artifact_id": "bible-frozen"},
    )
    monkeypatch.setattr(
        evidence_repository,
        "get_artifact",
        lambda _artifact_id: {
            "type": "character_bible",
            "scope_type": "project",
            "scope_id": "p1",
            "content": frozen.model_dump(mode="json"),
        },
    )

    snapshot = _storyboard_bible_snapshot(
        {"id": "p1", "bible_json": current.model_dump_json()},
        cp,
    )

    assert snapshot.world.visual_style_canonical == "frozen-style"


def test_storyboard_recorder_fingerprint_tracks_bible_artifact(
    repair_db,
    monkeypatch,
) -> None:
    conn, _screenplay = repair_db
    captured: list[str] = []

    def fake_create(_cls, **kwargs):
        captured.append(kwargs["input_fingerprint"])
        return SimpleNamespace(run_id=f"run-{len(captured)}")

    monkeypatch.setattr(
        WorkflowRecorder,
        "create",
        classmethod(fake_create),
    )
    conn.execute(
        "UPDATE projects SET bible_artifact_id='bible-v1' WHERE id='p1'"
    )
    conn.commit()
    _new_storyboard_recorder("e1")
    conn.execute(
        "UPDATE projects SET bible_artifact_id='bible-v2' WHERE id='p1'"
    )
    conn.commit()
    _new_storyboard_recorder("e1")

    assert len(captured) == 2
    assert captured[0] != captured[1]


def test_resumed_storyboard_recorder_keeps_checkpoint_bible_fingerprint(
    repair_db,
    monkeypatch,
) -> None:
    conn, _screenplay = repair_db
    save_checkpoint(SupervisorCheckpoint(
        episode_id="e1",
        input_versions={
            "screenplay_artifact_id": "sp1",
            "bible_artifact_id": "bible-frozen",
        },
    ))
    captured: list[str] = []

    def fake_create(_cls, **kwargs):
        captured.append(kwargs["input_fingerprint"])
        return SimpleNamespace(run_id=f"run-{len(captured)}")

    monkeypatch.setattr(
        WorkflowRecorder,
        "create",
        classmethod(fake_create),
    )
    for current_artifact in ("bible-current-v2", "bible-current-v3"):
        conn.execute(
            "UPDATE projects SET bible_artifact_id=? WHERE id='p1'",
            (current_artifact,),
        )
        conn.commit()
        _new_storyboard_recorder("e1", resume=True)

    assert captured[0] == captured[1]


def _shot(number: int, *, action: str | None = None) -> Shot:
    return Shot(
        shot_no=number,
        duration_s=5,
        shot_size="中景",
        camera_move="固定",
        scene_setting="日，广场",
        characters=["少年"],
        action_desc=action or f"少年在石碑前完成第{number}步动作。",
        first_frame_desc="少年站在石碑前，保持稳定姿态。",
        last_frame_desc="同一机位，少年停在石碑旁查看结果。",
        source_excerpt="少年走到石碑前，抬头查看上面显现的清晰结果。",
        prompt_contract_version="renderability_v1",
        is_final=number == 3,
    )


def _current_board(conn) -> Storyboard:
    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id='e1' ORDER BY shot_no"
    ).fetchall()
    return _board_from_shot_rows(rows, 1)


def test_supervisor_generates_and_commits_scene_packs(
    repair_db,
    monkeypatch,
) -> None:
    conn, _screenplay = repair_db
    request_control("e1", "clear")
    conn.execute("DELETE FROM shots WHERE episode_id='e1'")
    bible = Bible(
        characters=[Character(
            name="少年",
            role="主角",
            appearance_canonical="十六岁黑发少年，蓝色长衫，身形清瘦，目光坚定",
        )],
        world=World(
            era="古代",
            genre="剧情",
            visual_style_canonical="国风动画电影感，稳定统一光影",
        ),
    )
    conn.execute(
        "UPDATE projects SET bible_json=? WHERE id='p1'",
        (bible.model_dump_json(),),
    )
    conn.commit()
    contexts = [
        StoryboardSceneContext(
            scene_id="SC001",
            scene_no=1,
            scene_name="广场",
            scene_time="日",
            entry_state="少年站在广场入口观察石碑",
            exit_state="少年走到石碑前确认结果",
            spatial_axis="入口、少年和石碑保持同一横向轴线",
            context_requirements=[
                StoryboardContextRequirement(
                    requirement_id="CTX-SC001-01",
                    description="建立广场入口、少年与石碑的位置关系",
                    required_before_shot_no=2,
                )
            ],
        )
    ]
    focuses = [
        ("context", "全景", "固定", ["CTX-SC001-01"]),
        ("action", "中景", "跟随", []),
        ("emotion", "近景", "固定", []),
    ]
    outcomes = [
        "观众明确广场入口、少年和石碑的空间关系",
        "少年从入口走到石碑前并完成主要动作",
        "少年看清结果后由期待转为警觉和克制",
    ]
    outline = StoryboardOutline(
        episode_no=1,
        scene_contexts=contexts,
        shots=[
            StoryboardOutlineShot(
                shot_no=index,
                shot_id=f"SH{index:04d}",
                scene_id="SC001",
                scene_time="日",
                scene_name="广场",
                beat=f"少年完成第{index}个独立剧情任务",
                purpose=f"第{index}镜承担独立剧情与导演作用",
                context_requirement_ids=context_ids,
                resulting_change=outcomes[index - 1],
                readability_focus=focus,
                camera_size=size,
                camera_angle="平视侧面角度",
                camera_movement=move,
                camera_motivation="让本镜的空间、动作或情绪清楚可读",
                state_in="少年位于上一个动作结束位置",
                primary_action=f"少年完成第{index}步动作",
                state_out=f"少年完成第{index}步动作后停下",
                continuity_mode="same_scene_cut",
                duration_s=5,
                characters_visible=["少年"],
            )
            for index, (focus, size, move, context_ids) in enumerate(
                focuses,
                start=1,
            )
        ],
    )
    pack = StoryboardScenePack(
        episode_no=1,
        scene_id="SC001",
        shots=[
            _shot(index).model_copy(update={
                "shot_id": f"SH{index:04d}",
                "scene_id": "SC001",
                "scene_name": "广场",
                "scene_time": "日",
                "shot_size": size,
                "camera_angle": "平视侧面角度",
                "camera_move": move,
                "camera_motivation": "让本镜的空间、动作或情绪清楚可读",
                "purpose": f"第{index}镜承担独立剧情与导演作用",
                "context_requirement_ids": context_ids,
                "resulting_change": outcomes[index - 1],
                "readability_focus": focus,
                "prompt_contract_version": "director_scene_pack_v1",
                "is_final": index == 3,
            })
            for index, (focus, size, move, context_ids) in enumerate(
                focuses,
                start=1,
            )
        ],
    )

    async def fake_outline(*_args, **_kwargs):
        return outline

    calls: list[str] = []

    async def fake_scene_pack(*_args, **_kwargs):
        calls.append(_args[-1].scene_id)
        return pack

    def passed_evaluation(_ep, board, *_args, **_kwargs):
        return SimpleNamespace(
            passed=True,
            errors=[],
            warnings=[],
            issues=[],
            board=board,
        )

    import app.domain.storyboard_ops as storyboard_ops
    import app.domain.video_ops as video_ops
    import app.storyboard_supervisor as supervisor_module

    monkeypatch.setattr(supervisor_module, "generate_storyboard_outline", fake_outline)
    monkeypatch.setattr(supervisor_module, "generate_storyboard_scene_pack", fake_scene_pack)
    monkeypatch.setattr(video_ops, "evaluate_storyboard_for_confirmation", passed_evaluation)
    monkeypatch.setattr(
        storyboard_ops,
        "_finalize_storyboard_evidence",
        lambda *_args, **_kwargs: "art-final",
    )

    checkpoint = asyncio.run(run_storyboard_supervisor(
        "e1",
        resume=False,
        preflight_done=True,
    ))

    assert checkpoint.phase == "SUCCEEDED"
    assert calls == ["SC001"]
    rows = conn.execute(
        "SELECT shot_no,shot_size,camera_move,shot_contract_json "
        "FROM shots WHERE episode_id='e1' ORDER BY shot_no"
    ).fetchall()
    assert [row["shot_no"] for row in rows] == [1, 2, 3]
    assert rows[1]["shot_size"] == "中景"
    assert rows[1]["camera_move"] == "跟随"
    contract = json.loads(rows[1]["shot_contract_json"])
    assert contract["camera_angle"] == "平视侧面角度"
    assert contract["purpose"].startswith("第2镜")


def test_scene_pack_failure_preserves_successful_later_batch(
    repair_db,
    monkeypatch,
) -> None:
    from app.stages import StageError, StoryboardShotDraft

    conn, _screenplay = repair_db
    request_control("e1", "clear")
    conn.execute("DELETE FROM shots WHERE episode_id='e1'")
    bible = Bible(
        characters=[Character(
            name="少年",
            role="主角",
            appearance_canonical="十六岁黑发少年，蓝色长衫，身形清瘦，目光坚定",
        )],
        world=World(visual_style_canonical="国风动画电影感"),
    )
    conn.execute(
        "UPDATE projects SET bible_json=? WHERE id='p1'",
        (bible.model_dump_json(),),
    )
    contexts = [
        StoryboardSceneContext(
            scene_id=f"SC00{number}",
            scene_no=number,
            scene_name="广场",
            scene_time="日",
            entry_state=f"第{number}场开始",
            exit_state=f"第{number}场结束",
        )
        for number in range(1, 4)
    ]
    outline = StoryboardOutline(
        episode_no=1,
        scene_contexts=contexts,
        shots=[
            StoryboardOutlineShot(
                shot_no=number,
                shot_id=f"SH{number:04d}",
                scene_id=f"SC00{number}",
                scene_time="日",
                scene_name="广场",
                beat=f"少年完成第{number}个任务",
                purpose=f"第{number}镜承担独立剧情作用",
                resulting_change=f"第{number}镜完成后状态改变",
                readability_focus="action",
                camera_size="中景",
                camera_angle="平视侧面角度",
                camera_movement="跟随",
                camera_motivation="完整展示动作路径与结果",
                state_in=f"第{number}镜开始状态",
                primary_action=f"少年完成第{number}步动作",
                state_out=f"第{number}镜结束状态",
                continuity_mode="same_scene_cut",
                duration_s=5,
                characters_visible=["少年"],
            )
            for number in range(1, 4)
        ],
    )

    def packed_shot(number: int) -> StoryboardScenePack:
        return StoryboardScenePack(
            episode_no=1,
            scene_id=f"SC00{number}",
            shots=[_shot(number).model_copy(update={
                "shot_id": f"SH{number:04d}",
                "scene_id": f"SC00{number}",
                "scene_name": "广场",
                "scene_time": "日",
                "shot_size": "中景",
                "camera_move": "跟随",
                "purpose": f"第{number}镜承担独立剧情作用",
                "resulting_change": f"第{number}镜完成后状态改变",
                "readability_focus": "action",
                "camera_angle": "平视侧面角度",
                "camera_motivation": "完整展示动作路径与结果",
                "is_final": number == 3,
            })],
        )

    async def fake_outline(*_args, **_kwargs):
        return outline

    pack_calls: list[str] = []
    persisted_before_pack: dict[str, int] = {}

    async def fake_scene_pack(*_args, **_kwargs):
        context = _args[-1]
        pack_calls.append(context.scene_id)
        persisted_before_pack[context.scene_id] = int(conn.execute(
            "SELECT COUNT(*) AS c FROM shots WHERE episode_id='e1'"
        ).fetchone()["c"])
        if context.scene_id == "SC002":
            raise StageError("场景分镜", ["模拟第二场批量格式失败"])
        return packed_shot(context.scene_no)

    shot_calls: list[int] = []

    async def fake_next_shot(*_args, **kwargs):
        shot_no = len(kwargs["completed_shots"]) + 1
        shot_calls.append(shot_no)
        return StoryboardShotDraft(
            episode_no=1,
            shot=packed_shot(shot_no).shots[0],
            is_final=False,
        )

    def passed_evaluation(_ep, board, *_args, **_kwargs):
        return SimpleNamespace(
            passed=True,
            errors=[],
            warnings=[],
            issues=[],
            board=board,
        )

    import app.domain.storyboard_ops as storyboard_ops
    import app.domain.video_ops as video_ops
    import app.storyboard_supervisor as supervisor_module
    import app.validators as storyboard_validators

    monkeypatch.setattr(supervisor_module, "generate_storyboard_outline", fake_outline)
    monkeypatch.setattr(supervisor_module, "generate_storyboard_scene_pack", fake_scene_pack)
    monkeypatch.setattr(supervisor_module, "generate_storyboard_next_shot", fake_next_shot)
    monkeypatch.setattr(video_ops, "evaluate_storyboard_for_confirmation", passed_evaluation)
    monkeypatch.setattr(
        storyboard_validators,
        "validate_storyboard_direction_contract",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        storyboard_ops,
        "_finalize_storyboard_evidence",
        lambda *_args, **_kwargs: "art-final",
    )

    checkpoint = asyncio.run(run_storyboard_supervisor(
        "e1",
        resume=False,
        preflight_done=True,
    ))

    assert checkpoint.phase == "SUCCEEDED"
    assert pack_calls == ["SC001", "SC002", "SC003"]
    # 场景包按连续前沿逐批提交：第二场开始前，第一场已经可被前端读取。
    assert persisted_before_pack == {"SC001": 0, "SC002": 1, "SC003": 2}
    assert shot_calls == [2]
    assert [
        row["shot_no"]
        for row in conn.execute(
            "SELECT shot_no FROM shots WHERE episode_id='e1' ORDER BY shot_no"
        )
    ] == [1, 2, 3]


def test_resume_atomically_cleans_legacy_ghost_contract_before_gate(
    repair_db, monkeypatch
) -> None:
    """“继续修复”必须先清理旧合同，再跑整集门禁，不应再调用模型重写。"""
    conn, _screenplay = repair_db
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
    conn.execute(
        "UPDATE projects SET bible_json=? WHERE id='p1'",
        (bible.model_dump_json(),),
    )
    row = conn.execute("SELECT shot_contract_json FROM shots WHERE id='s1'").fetchone()
    contract = json.loads(row["shot_contract_json"] or "{}")
    contract.update({
        "characters_visible": ["韩枫", "少年"],
        "audio_cast": ["韩枫"],
        "audio_timeline": [{
            "start_s": 0.5,
            "end_s": 2.5,
            "type": "spoken_dialogue",
            "speaker_id": "韩枫",
            "text": "立刻停下。",
            "lip_sync": True,
            "emotion": "平静",
            "voice_canonical": "",
        }],
        "reference_roles": ["character_identity:韩枫"],
    })
    conn.execute(
        "UPDATE shots SET shot_contract_json=? WHERE id='s1'",
        (json.dumps(contract, ensure_ascii=False),),
    )
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=number,
                scene_setting="日，广场",
                beat=f"少年完成第{number}步动作",
                state_in="少年站在石碑前",
                primary_action=f"少年完成第{number}步动作",
                state_out="少年停在石碑旁",
                continuity_mode="same_scene_cut",
                duration_s=5,
                characters_visible=["少年"],
            )
            for number in range(1, 4)
        ],
    )
    conn.execute(
        "UPDATE episodes SET storyboard_outline_json=? WHERE id='e1'",
        (outline.model_dump_json(),),
    )
    conn.commit()
    # 全量套件中其他控制测试也使用 e1；清理进程内 pause/handoff，
    # 保证本测试只验证恢复路径。
    request_control("e1", "clear")
    save_checkpoint(SupervisorCheckpoint(
        episode_id="e1",
        phase="WAITING_HUMAN",
        outcome="WAITING_RETRY_GATE_REPAIR_EXHAUSTED",
        validated_prefix_end=3,
        next_shot_no=4,
        expected_total=3,
        input_versions={"screenplay_artifact_id": "sp1"},
        planner_version=STORYBOARD_REPAIR_PLANNER_VERSION,
        last_repair={
            "status": "best_effort_preserved",
            "issue_messages": ["Prompt 编译失败：not a functional extra: 韩枫"],
        },
    ))

    async def _unexpected_generation(*_args, **_kwargs):
        raise AssertionError("确定性角色合同修复通过后不应再调用镜头生成模型")

    def _passed_evaluation(_ep, board, *_args, **_kwargs):
        return SimpleNamespace(
            passed=True,
            errors=[],
            warnings=[],
            issues=[],
            board=board,
        )

    import app.domain.storyboard_ops as storyboard_ops
    import app.domain.video_ops as video_ops
    import app.storyboard_supervisor as supervisor_module

    monkeypatch.setattr(supervisor_module, "generate_storyboard_next_shot", _unexpected_generation)
    monkeypatch.setattr(video_ops, "evaluate_storyboard_for_confirmation", _passed_evaluation)
    monkeypatch.setattr(storyboard_ops, "_finalize_storyboard_evidence", lambda *_args: "art-final")

    checkpoint = asyncio.run(run_storyboard_supervisor(
        "e1",
        resume=True,
        preflight_done=True,
        new_activation=True,
    ))

    assert checkpoint.phase == "SUCCEEDED"
    repaired = conn.execute("SELECT * FROM shots WHERE id='s1'").fetchone()
    repaired_contract = json.loads(repaired["shot_contract_json"])
    assert repaired_contract["characters_visible"] == ["少年"]
    assert repaired_contract["audio_cast"] == []
    assert repaired_contract["audio_timeline"] == []
    assert repaired_contract["reference_roles"] == []


def test_resume_discards_pending_candidate_when_current_gate_already_passes(
    repair_db, monkeypatch,
) -> None:
    conn, _screenplay = repair_db
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=number,
                scene_setting="日，广场",
                beat=f"少年完成第{number}步动作",
            )
            for number in range(1, 4)
        ],
    )
    conn.execute(
        "UPDATE episodes SET storyboard_outline_json=? WHERE id='e1'",
        (outline.model_dump_json(),),
    )
    conn.commit()
    request_control("e1", "clear")
    current = _current_board(conn)
    save_checkpoint(SupervisorCheckpoint(
        episode_id="e1",
        phase="PAUSED_EXTERNAL",
        outcome="PAUSED_BY_USER",
        validated_prefix_end=3,
        next_shot_no=2,
        expected_total=3,
        input_versions={"screenplay_artifact_id": "sp1"},
        planner_version=STORYBOARD_REPAIR_PLANNER_VERSION,
        last_repair={
            "status": "candidate_pending",
            "mode": "insert",
            "window_start": 2,
            "window_end": 2,
            "base_hash": _storyboard_hash(current),
            "fingerprint": "obsolete-policy-gate",
            "candidate_outline": outline.model_dump(mode="json"),
            "issue_messages": ["旧门禁问题"],
        },
        repair_candidate_shots=[_shot(2, action="不应继续生成的过期候选").model_dump(mode="json")],
    ))

    async def _unexpected_generation(*_args, **_kwargs):
        raise AssertionError("当前正式分镜已通过新门禁，不应恢复过期候选")

    def _passed_evaluation(_ep, board, *_args, **_kwargs):
        return SimpleNamespace(
            passed=True,
            errors=[],
            warnings=[],
            issues=[],
            board=board,
        )

    import app.domain.storyboard_ops as storyboard_ops
    import app.domain.video_ops as video_ops
    import app.storyboard_supervisor as supervisor_module

    monkeypatch.setattr(
        supervisor_module,
        "generate_storyboard_next_shot",
        _unexpected_generation,
    )
    monkeypatch.setattr(
        video_ops,
        "evaluate_storyboard_for_confirmation",
        _passed_evaluation,
    )
    monkeypatch.setattr(
        storyboard_ops,
        "_finalize_storyboard_evidence",
        lambda *_args: "art-final",
    )

    checkpoint = asyncio.run(run_storyboard_supervisor(
        "e1",
        resume=True,
        preflight_done=True,
        new_activation=False,
    ))

    assert checkpoint.phase == "SUCCEEDED"
    assert checkpoint.repair_candidate_shots == []
    assert checkpoint.last_repair["status"] == "obsolete_current_gate_passed"


def test_legacy_runaway_counter_becomes_audit_not_new_activation_budget() -> None:
    checkpoint = SupervisorCheckpoint.model_validate({
        "episode_id": "e1",
        "phase": "WAITING_HUMAN",
        "repair_epoch": 5558,
        "issue_fingerprint_counts": {"old": 5558},
        "issue_strategy_history": {"old": ["repair_current:old-attempt"]},
        "outcome": "PAUSED_REPAIR_SAFETY_LIMIT",
    })

    migrated = _migrate_checkpoint(checkpoint)

    assert migrated.planner_version == STORYBOARD_REPAIR_PLANNER_VERSION
    assert migrated.repair_epoch == 5558
    assert migrated.activation_attempt_count == 0
    assert migrated.issue_fingerprint_counts == {}
    assert migrated.issue_strategy_history == {}
    assert migrated.legacy_repair_audit["repair_epoch"] == 5558
    assert migrated.legacy_repair_audit["issue_strategy_history"] == {
        "old": ["repair_current:old-attempt"],
    }
    assert migrated.outcome == "WAITING_RETRY_LEGACY_MIGRATED"


def test_new_activation_resets_all_activation_local_repair_budgets() -> None:
    checkpoint = SupervisorCheckpoint(
        episode_id="e1",
        planner_version=STORYBOARD_REPAIR_PLANNER_VERSION,
        activation_no=2,
        activation_attempt_count=6,
        repair_epoch=11,
        issue_fingerprint_counts={"same-issue": 4},
        issue_strategy_history={
            "same-issue": [
                "repair_current:a1",
                "repair_current:a2",
                "repair_window:a3",
                "repair_window:a4",
            ],
        },
        last_repair={"status": "paused"},
        repair_candidate_shots=[_shot(2).model_dump(mode="json")],
        outcome="REPAIR_FAILED_STRATEGIES_EXHAUSTED",
    )

    activated = _begin_repair_activation(checkpoint)

    assert activated.activation_no == 3
    assert activated.activation_attempt_count == 0
    assert activated.repair_epoch == 11
    assert activated.issue_fingerprint_counts == {}
    assert activated.issue_strategy_history == {}
    archived = activated.legacy_repair_audit["completed_activation_histories"][-1]
    assert archived["activation_no"] == 2
    assert archived["attempt_count"] == 6
    assert archived["issue_strategy_history"]["same-issue"][-1] == "repair_window:a4"
    assert activated.repair_candidate_shots == []
    assert activated.last_repair["status"] == "superseded_by_new_activation"
    assert activated.outcome is None


def test_published_gate_repair_starts_in_isolated_candidate_window(
    repair_db,
) -> None:
    conn, _screenplay = repair_db
    before = _storyboard_hash(_current_board(conn))
    save_checkpoint(SupervisorCheckpoint(
        episode_id="e1",
        phase="SUCCEEDED",
        outcome="SUCCEEDED_READY_FOR_CONFIRM",
        planner_version=STORYBOARD_REPAIR_PLANNER_VERSION,
        validated_prefix_end=3,
        next_shot_no=4,
        expected_total=3,
        input_versions={"screenplay_artifact_id": "sp1"},
    ))

    checkpoint = prepare_published_storyboard_repair(
        "e1",
        ["Prompt 编译失败：shot_no=2 的画面合同与声音合同不一致"],
    )

    assert _repair_is_pending(checkpoint)
    assert checkpoint.activation_no == 1
    assert checkpoint.last_repair["mode"] == "replace"
    assert checkpoint.last_repair["window_start"] == 2
    assert checkpoint.last_repair["window_end"] == 2
    assert checkpoint.repair_candidate_shots == []
    assert _storyboard_hash(_current_board(conn)) == before


def test_new_activation_withdraws_legacy_failed_gate_publication(repair_db) -> None:
    conn, _screenplay = repair_db
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="storyboard",
        scope_type="episode",
        scope_id="e1",
        status="approved",
        trust_level="T2",
        content=_current_board(conn).model_dump(mode="json"),
    ))
    revision = ensure_production_revision(
        episode_id="e1",
        kind="storyboard",
        input_fingerprint="legacy-failed-gate",
        resume=False,
    )
    conn.execute(
        "UPDATE production_revisions SET status='published',working_artifact_id=?,"
        "published_artifact_id=? WHERE id=?",
        (artifact["id"], artifact["id"], revision.id),
    )
    conn.execute(
        """UPDATE episodes SET storyboard_artifact_id=?,working_storyboard_artifact_id=?,
                  published_storyboard_artifact_id=?,storyboard_completion_certificate_id='bad-cert',
                  storyboard_production_revision_id=? WHERE id='e1'""",
        (artifact["id"], artifact["id"], artifact["id"], revision.id),
    )
    conn.commit()
    episode = conn.execute("SELECT * FROM episodes WHERE id='e1'").fetchone()
    checkpoint = SupervisorCheckpoint(
        episode_id="e1",
        phase="SUCCEEDED",
        outcome="SUCCEEDED_GATE_RETRY_EXHAUSTED_FALLBACK",
    )

    assert _withdraw_legacy_failed_publication(conn, "e1", episode, checkpoint) is True

    current = conn.execute(
        """SELECT storyboard_artifact_id,working_storyboard_artifact_id,
                  published_storyboard_artifact_id,storyboard_completion_certificate_id,
                  storyboard_production_revision_id,storyboard_warning FROM episodes WHERE id='e1'""",
    ).fetchone()
    assert all(current[key] is None for key in (
        "storyboard_artifact_id",
        "working_storyboard_artifact_id",
        "published_storyboard_artifact_id",
        "storyboard_completion_certificate_id",
        "storyboard_production_revision_id",
    ))
    assert "误发布" in current["storyboard_warning"]
    assert conn.execute(
        "SELECT status FROM artifacts WHERE id=?", (artifact["id"],),
    ).fetchone()["status"] == "stale"
    assert conn.execute(
        "SELECT status,published_artifact_id FROM production_revisions WHERE id=?",
        (revision.id,),
    ).fetchone()["status"] == "superseded"
    assert conn.execute("SELECT COUNT(*) AS c FROM shots WHERE episode_id='e1'").fetchone()["c"] == 3


def test_repair_plan_does_not_delete_or_mutate_official_shots(repair_db) -> None:
    conn, _screenplay = repair_db
    before = [tuple(row) for row in conn.execute(
        "SELECT id,shot_no,action_desc FROM shots ORDER BY shot_no"
    ).fetchall()]
    plan = route_issues(["[FRAME_STATE_INVALID] 第 2 镜首帧与 planned_state_in 不一致"])
    checkpoint = SupervisorCheckpoint(
        episode_id="e1",
        planner_version=STORYBOARD_REPAIR_PLANNER_VERSION,
        validated_prefix_end=3,
    )

    planned = _apply_repair(
        checkpoint, plan, conn, "e1", list(_current_board(conn).shots), None,
    )

    after = [tuple(row) for row in conn.execute(
        "SELECT id,shot_no,action_desc FROM shots ORDER BY shot_no"
    ).fetchall()]
    assert after == before
    assert planned.activation_attempt_count == 1
    assert planned.last_repair["status"] == "candidate_pending"
    assert planned.last_repair["semantic_attempt_id"].startswith("sbatt_")
    assert planned.repair_candidate_shots == []


def test_repair_plan_reuses_pinned_screenplay_authority(
    repair_db,
    monkeypatch,
) -> None:
    conn, screenplay = repair_db
    plan = route_issues([
        "[FRAME_STATE_INVALID] 第 2 镜首帧与 planned_state_in 不一致"
    ])
    monkeypatch.setattr(
        "app.production.screenplay_authority.resolve_downstream_screenplay",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("修复阶段不应重新解析已锁定的剧本权威链")
        ),
    )

    planned = _apply_repair(
        SupervisorCheckpoint(
            episode_id="e1",
            planner_version=STORYBOARD_REPAIR_PLANNER_VERSION,
            validated_prefix_end=3,
        ),
        plan,
        conn,
        "e1",
        list(_current_board(conn).shots),
        None,
        repair_screenplay=screenplay,
        narrative_repair_active=False,
    )

    assert planned.last_repair["status"] == "candidate_pending"


def test_cognitive_bridge_is_isolated_until_candidate_commit(
    repair_db, monkeypatch,
) -> None:
    conn, screenplay = repair_db
    official_outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=number,
                scene_setting="日，广场",
                beat=f"节拍{number}",
            )
            for number in range(1, 4)
        ],
    )
    official_json = official_outline.model_dump_json()
    conn.execute(
        "UPDATE episodes SET storyboard_outline_json=? WHERE id='e1'",
        (official_json,),
    )
    conn.commit()
    plan = route_issues(
        ["shot_no=2 的观众理解跳跃，需局部补足证据交付"],
        validated_prefix_end=3,
        semantic_diagnosis={
            "scope": "current_shot",
            "selected_strategy": "repair_current",
            "reason": "仅修复当前镜的证据交付",
            "semantic_gap": "一个观众先验未获得必要证据",
            "assimilation_task_ids": ["AT-1"],
            "affected_relation_ids": ["XI-1"],
            "candidate_assessments": [{
                "strategy": "repair_current",
                "passes_deletion_test": False,
                "passes_marginal_gain_test": True,
                "expected_narrative_gain": 0.7,
            }],
            "selection_reason": "局部贡献足以弥补缺口",
        },
    )

    checkpoint = _apply_repair(
        SupervisorCheckpoint(
            episode_id="e1",
            planner_version=STORYBOARD_REPAIR_PLANNER_VERSION,
            validated_prefix_end=3,
        ),
        plan,
        conn,
        "e1",
        list(_current_board(conn).shots),
        official_outline,
    )

    # Planning is candidate-only: neither the DB projection nor the caller's
    # official outline object may observe the bridge before full-gate commit.
    assert conn.execute(
        "SELECT storyboard_outline_json FROM episodes WHERE id='e1'"
    ).fetchone()[0] == official_json
    assert official_outline.cognitive_bridge_plans == []
    candidate_outline = StoryboardOutline.model_validate(
        checkpoint.last_repair["candidate_outline"]
    )
    assert [item.assimilation_task_ids for item in candidate_outline.cognitive_bridge_plans] == [
        ["AT-1"]
    ]
    assert candidate_outline.cognitive_bridge_plans[0].deletion_test_result == {
        "applicable": False,
        "passed": True,
        "reason": "selected operation does not delete an outline node",
    }

    current = _current_board(conn)
    replacement = _shot(2, action="少年指向石碑结果，让证据在画面中完整停留。")
    checkpoint.repair_candidate_shots = [replacement.model_dump(mode="json")]
    candidate_board = _merge_repair_candidate(current, checkpoint)
    monkeypatch.setattr(
        "app.worker.clear_shot_artifacts", lambda *_args, **_kwargs: None,
    )

    _commit_repair_candidate(
        conn,
        checkpoint,
        episode_id="e1",
        screenplay=screenplay,
        current_board=current,
        candidate_board=candidate_board,
        expected_screenplay_artifact_id="sp1",
        run_id=None,
    )

    committed_outline = StoryboardOutline.model_validate_json(
        conn.execute(
            "SELECT storyboard_outline_json FROM episodes WHERE id='e1'"
        ).fetchone()[0]
    )
    assert [item.assimilation_task_ids for item in committed_outline.cognitive_bridge_plans] == [
        ["AT-1"]
    ]


def test_spine_missing_insert_targets_earliest_declared_beat(repair_db) -> None:
    conn, _screenplay = repair_db
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=number,
                scene_setting="日，广场",
                beat=f"少年完成第{number}步动作",
                spine_beat_ids=[f"S{number:02d}"],
                primary_action=f"少年完成第{number}步动作",
                state_in="少年站在石碑前",
                state_out="少年完成动作",
                characters_visible=["少年"],
            )
            for number in range(1, 4)
        ],
    )
    for number in range(1, 4):
        shot = _shot(number)
        shot.spine_beat_ids = [f"S{number:02d}"]
        conn.execute(
            "UPDATE shots SET shot_contract_json=? WHERE id=?",
            (shot.model_dump_json(), f"s{number}"),
        )
    conn.commit()
    plan = route_issues(
        ["主线节拍主体已入画但未完成对应动作/对白交付：S01/少年:在屋顶点燃红色信号"],
        validated_prefix_end=3,
        next_shot_no=4,
        semantic_diagnosis={
            "scope": "structure",
            "selected_strategy": "insert_shot",
            "reason": "该测试验证已判定需要插镜后的最早节拍定位",
        },
    )

    planned = _apply_repair(
        SupervisorCheckpoint(
            episode_id="e1",
            planner_version=STORYBOARD_REPAIR_PLANNER_VERSION,
            validated_prefix_end=3,
        ),
        plan,
        conn,
        "e1",
        list(_current_board(conn).shots),
        outline,
    )

    assert planned.last_repair["mode"] == "insert"
    assert planned.last_repair["window_start"] == 2
    assert planned.last_repair["window_end"] == 2
    candidate_outline = StoryboardOutline.model_validate(
        planned.last_repair["candidate_outline"]
    )
    assert len(candidate_outline.shots) == 4
    assert candidate_outline.shots[1].spine_beat_ids == ["S01"]
    assert candidate_outline.shots[1].primary_action == "在屋顶点燃红色信号"
    assert conn.execute(
        "SELECT storyboard_outline_json FROM episodes WHERE id='e1'"
    ).fetchone()[0] is None


def test_spine_repair_uses_bound_shot_when_another_issue_has_earlier_frontier(
    repair_db,
) -> None:
    conn, _screenplay = repair_db
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=number,
                scene_setting="日，广场",
                beat=f"少年完成第{number}步动作",
                spine_beat_ids=[f"S{number:02d}"],
                primary_action=f"少年完成第{number}步动作",
                state_in="少年站在石碑前",
                state_out="少年完成动作",
                characters_visible=["少年"],
            )
            for number in range(1, 4)
        ],
    )
    for number in range(1, 4):
        shot = _shot(number)
        shot.spine_beat_ids = [f"S{number:02d}"]
        conn.execute(
            "UPDATE shots SET shot_contract_json=? WHERE id=?",
            (shot.model_dump_json(), f"s{number}"),
        )
    conn.commit()
    plan = route_issues([
        "第 1 镜缺少必填字段：scene_name",
        "主线节拍主体已入画但未完成对应动作/对白交付：S03/少年:举起信物",
    ], validated_prefix_end=3, semantic_diagnosis={
        "scope": "structure",
        "selected_strategy": "insert_shot",
        "reason": "该测试验证插镜目标绑定不被其他更早问题覆盖",
    })

    planned = _apply_repair(
        SupervisorCheckpoint(
            episode_id="e1",
            planner_version=STORYBOARD_REPAIR_PLANNER_VERSION,
            validated_prefix_end=3,
        ),
        plan,
        conn,
        "e1",
        list(_current_board(conn).shots),
        outline,
    )

    assert planned.last_repair["mode"] == "insert"
    assert planned.last_repair["window_start"] == 4
    candidate_outline = StoryboardOutline.model_validate(
        planned.last_repair["candidate_outline"]
    )
    assert candidate_outline.shots[3].spine_beat_ids == ["S03"]
    assert candidate_outline.shots[3].primary_action == "举起信物"


def test_spine_repair_preserves_spoken_delivery_contract() -> None:
    brief = StoryboardOutlineShot(
        shot_no=6,
        scene_setting="黄昏，青石空地",
        beat="路人丙说明门规",
        spine_beat_ids=["S02"],
        key_line_ids=["KL05", "KL06"],
        information_ids=["I02"],
        new_information_ids=["I02"],
        primary_action="路人丙说明门规",
        characters_visible=["孟浩", "路人丙"],
        audio_cast=[],
    )

    _retarget_spine_repair_brief(
        brief,
        ("S02", "路人丙", "交代杂役规则并发放凝气卷"),
    )

    assert brief.spine_beat_ids == ["S02"]
    assert brief.key_line_ids == ["KL05", "KL06"]
    assert brief.information_ids == ["I02"]
    assert brief.new_information_ids == ["I02"]
    assert brief.characters_visible == ["孟浩", "路人丙"]
    assert brief.audio_cast == ["路人丙"]


def test_spine_repair_does_not_infer_delivery_channel_from_words() -> None:
    from app.validators import _spine_delivery_clauses

    visible, spoken, receptive = _spine_delivery_clauses(
        "在宝阁内被珠光宝气震撼，喃喃自语"
    )
    assert visible == ["在宝阁内被珠光宝气震撼，喃喃自语"]
    assert spoken == []
    assert receptive == []

    brief = StoryboardOutlineShot(
        shot_no=2,
        scene_setting="日，外宗宝阁内",
        beat="孟浩浏览宝物",
        spine_beat_ids=["S01"],
        primary_action="孟浩浏览宝物",
        characters_visible=["孟浩"],
        audio_cast=[],
    )
    _retarget_spine_repair_brief(
        brief,
        ("S01", "孟浩", "在宝阁内被珠光宝气震撼，喃喃自语"),
    )

    assert brief.audio_cast == []


def test_generated_spine_repair_reapplies_authorized_contract() -> None:
    brief = StoryboardOutlineShot(
        shot_no=9,
        scene_setting="晚，外宗住所内",
        beat="孟浩研究铜镜",
        spine_beat_ids=["S05"],
        key_line_ids=["KL06"],
        information_ids=["I05"],
        new_information_ids=["I05"],
        primary_action="孟浩研究铜镜",
        characters_visible=["孟浩"],
        audio_cast=["孟浩"],
    )
    shot = _shot(9, action="孟浩放下端详许久的铜镜后闭目打坐")
    shot.characters_visible = ["孟浩"]
    shot.audio_cast = []
    shot.key_line_ids = []
    shot.information_ids = []
    shot.new_information_ids = []

    _retarget_spine_repair_shot(
        shot,
        brief,
        (
            "S05",
            "孟浩",
            "在外宗住所研究铜镜至深夜无果，喃喃身份转变",
        ),
    )

    assert shot.primary_action == (
        "在外宗住所研究铜镜至深夜无果，喃喃身份转变"
    )
    assert shot.spine_beat_ids == ["S05"]
    assert shot.key_line_ids == ["KL06"]
    assert shot.information_ids == ["I05"]
    assert shot.new_information_ids == ["I05"]
    assert shot.audio_cast == ["孟浩"]


def test_validated_candidate_commits_atomically_and_preserves_shot_identity(
    repair_db, monkeypatch,
) -> None:
    conn, screenplay = repair_db
    current = _current_board(conn)
    replacement = _shot(2, action="少年从中景切换为近景，只抬头查看石碑结果。")
    checkpoint = SupervisorCheckpoint(
        episode_id="e1",
        planner_version=STORYBOARD_REPAIR_PLANNER_VERSION,
        last_repair={
            "status": "candidate_generating",
            "mode": "replace",
            "window_start": 2,
            "window_end": 2,
            "base_hash": _storyboard_hash(current),
            "semantic_attempt_id": "sbatt_test",
            "fingerprint": "fp",
            "issue_messages": ["第 2 镜需修复"],
        },
        repair_candidate_shots=[replacement.model_dump(mode="json")],
    )
    candidate = _merge_repair_candidate(current, checkpoint)
    monkeypatch.setattr(
        "app.worker.clear_shot_artifacts", lambda *_args, **_kwargs: None,
    )

    _commit_repair_candidate(
        conn,
        checkpoint,
        episode_id="e1",
        screenplay=screenplay,
        current_board=current,
        candidate_board=candidate,
        expected_screenplay_artifact_id="sp1",
        run_id=None,
    )

    row = conn.execute(
        "SELECT id,action_desc FROM shots WHERE episode_id='e1' AND shot_no=2"
    ).fetchone()
    assert row["id"] == "s2"
    assert "近景" in row["action_desc"]
    assert conn.execute(
        "SELECT working_storyboard_artifact_id FROM episodes WHERE id='e1'"
    ).fetchone()[0]


def test_repair_candidate_preserves_official_source_evidence(
    repair_db, monkeypatch,
) -> None:
    conn, screenplay = repair_db
    stitched_excerpt = "少年走到石碑前……上面显现的清晰结果。"
    conn.execute(
        "UPDATE shots SET source_excerpt=? WHERE id='s2'",
        (stitched_excerpt,),
    )
    conn.commit()
    current = _current_board(conn)
    replacement = _shot(2, action="只修复画面，不应改写原文证据。")
    replacement.source_excerpt = "模型拼接出的非连续原文片段。"
    checkpoint = SupervisorCheckpoint(
        episode_id="e1",
        planner_version=STORYBOARD_REPAIR_PLANNER_VERSION,
        last_repair={
            "status": "candidate_generating",
            "mode": "replace",
            "window_start": 2,
            "window_end": 2,
            "base_hash": _storyboard_hash(current),
            "semantic_attempt_id": "sbatt_source_evidence",
            "fingerprint": "fp-source-evidence",
            "issue_messages": ["第 2 镜画面需修复"],
        },
        repair_candidate_shots=[replacement.model_dump(mode="json")],
    )
    candidate = _merge_repair_candidate(current, checkpoint)
    monkeypatch.setattr(
        "app.worker.clear_shot_artifacts", lambda *_args, **_kwargs: None,
    )

    _commit_repair_candidate(
        conn,
        checkpoint,
        episode_id="e1",
        screenplay=screenplay,
        current_board=current,
        candidate_board=candidate,
        expected_screenplay_artifact_id="sp1",
        run_id=None,
    )

    source = conn.execute(
        "SELECT content FROM chapters WHERE project_id='p1' AND idx=1"
    ).fetchone()["content"]
    aligned = align_source_excerpt(stitched_excerpt, source)
    assert aligned is not None
    row = conn.execute(
        "SELECT source_excerpt FROM shots WHERE id='s2'"
    ).fetchone()
    binding = conn.execute(
        "SELECT shot_id FROM storyboard_source_bindings WHERE shot_id='s2'"
    ).fetchone()
    assert candidate.shots[1].source_excerpt == aligned.excerpt
    assert row["source_excerpt"] == aligned.excerpt
    assert binding["shot_id"] == "s2"


def test_repair_candidate_uses_authorized_candidate_when_official_evidence_is_invalid(
    repair_db, monkeypatch,
) -> None:
    conn, screenplay = repair_db
    conn.execute(
        "UPDATE shots SET source_excerpt='拼接后无法证明的旧证据' WHERE id='s2'"
    )
    conn.commit()
    current = _current_board(conn)
    replacement = _shot(2, action="修复画面并提供可证明的连续原文。")
    replacement.source_excerpt = "候选片段同样无法证明"
    authorized_dialogue = _shot(2).source_excerpt
    replacement.dialogues = [
        Dialogue(speaker="少年", line=authorized_dialogue, emotion="平静"),
    ]
    checkpoint = SupervisorCheckpoint(
        episode_id="e1",
        planner_version=STORYBOARD_REPAIR_PLANNER_VERSION,
        last_repair={
            "status": "candidate_generating",
            "mode": "replace",
            "window_start": 2,
            "window_end": 2,
            "base_hash": _storyboard_hash(current),
            "semantic_attempt_id": "sbatt_candidate_evidence",
            "fingerprint": "fp-candidate-evidence",
            "issue_messages": ["第 2 镜画面需修复"],
        },
        repair_candidate_shots=[replacement.model_dump(mode="json")],
    )
    candidate = _merge_repair_candidate(current, checkpoint)
    monkeypatch.setattr(
        "app.worker.clear_shot_artifacts", lambda *_args, **_kwargs: None,
    )

    _commit_repair_candidate(
        conn,
        checkpoint,
        episode_id="e1",
        screenplay=screenplay,
        current_board=current,
        candidate_board=candidate,
        expected_screenplay_artifact_id="sp1",
        run_id=None,
    )

    row = conn.execute(
        "SELECT source_excerpt FROM shots WHERE id='s2'"
    ).fetchone()
    binding = conn.execute(
        "SELECT shot_id FROM storyboard_source_bindings WHERE shot_id='s2'"
    ).fetchone()
    assert row["source_excerpt"] == authorized_dialogue
    assert candidate.shots[1].source_excerpt == authorized_dialogue
    assert binding["shot_id"] == "s2"


def test_commit_persists_normalized_candidate_not_raw_model_payload(
    repair_db, monkeypatch,
) -> None:
    conn, screenplay = repair_db
    current = _current_board(conn)
    raw = _shot(2, action="模型原始候选")
    checkpoint = SupervisorCheckpoint(
        episode_id="e1",
        last_repair={
            "status": "candidate_generating",
            "mode": "replace",
            "window_start": 2,
            "window_end": 2,
            "base_hash": _storyboard_hash(current),
        },
        repair_candidate_shots=[raw.model_dump(mode="json")],
    )
    validated = _merge_repair_candidate(current, checkpoint)
    validated.shots[1].shot_size = "全景"
    monkeypatch.setattr(
        "app.worker.clear_shot_artifacts", lambda *_args, **_kwargs: None,
    )

    _commit_repair_candidate(
        conn,
        checkpoint,
        episode_id="e1",
        screenplay=screenplay,
        current_board=current,
        candidate_board=validated,
        expected_screenplay_artifact_id="sp1",
        run_id=None,
    )

    assert conn.execute("SELECT shot_size FROM shots WHERE id='s2'").fetchone()[0] == "全景"


def test_candidate_cas_conflict_never_overwrites_official_projection(
    repair_db, monkeypatch,
) -> None:
    conn, screenplay = repair_db
    current = _current_board(conn)
    replacement = _shot(2, action="不应落库的候选")
    checkpoint = SupervisorCheckpoint(
        episode_id="e1",
        planner_version=STORYBOARD_REPAIR_PLANNER_VERSION,
        last_repair={
            "status": "candidate_generating",
            "mode": "replace",
            "window_start": 2,
            "window_end": 2,
            "base_hash": "stale-hash",
        },
        repair_candidate_shots=[replacement.model_dump(mode="json")],
    )
    monkeypatch.setattr(
        "app.worker.clear_shot_artifacts", lambda *_args, **_kwargs: None,
    )

    with pytest.raises(RuntimeError, match="CAS conflict"):
        _commit_repair_candidate(
            conn,
            checkpoint,
            episode_id="e1",
            screenplay=screenplay,
            current_board=current,
            candidate_board=_merge_repair_candidate(current, checkpoint),
            expected_screenplay_artifact_id="sp1",
            run_id=None,
        )

    assert conn.execute(
        "SELECT action_desc FROM shots WHERE id='s2'"
    ).fetchone()[0] == _shot(2).action_desc


def test_candidate_merge_never_mutates_cas_baseline(repair_db) -> None:
    conn, _screenplay = repair_db
    current = _current_board(conn)
    baseline_hash = _storyboard_hash(current)
    inserted = _shot(2, action="插入候选")
    checkpoint = SupervisorCheckpoint(
        episode_id="e1",
        last_repair={"mode": "insert", "window_start": 2},
        repair_candidate_shots=[inserted.model_dump(mode="json")],
    )

    merged = _merge_repair_candidate(current, checkpoint)

    assert len(merged.shots) == len(current.shots) + 1
    assert _storyboard_hash(current) == baseline_hash


def test_candidate_checkpoint_preserves_evidence_artifact_id(repair_db) -> None:
    conn, _screenplay = repair_db
    current = _current_board(conn)
    inserted = _shot(2, action="插入候选")
    object.__setattr__(inserted, "evidence_artifact_id", "art-inserted")
    checkpoint = SupervisorCheckpoint(
        episode_id="e1",
        last_repair={"mode": "insert", "window_start": 2},
        repair_candidate_shots=[_shot_checkpoint_payload(inserted)],
    )

    merged = _merge_repair_candidate(current, checkpoint)

    assert getattr(merged.shots[1], "evidence_artifact_id", None) == "art-inserted"


def test_publish_rebinds_inserted_and_shifted_shots_to_current_evidence(
    repair_db,
) -> None:
    conn, screenplay = repair_db
    old_shot = _shot(2)
    old_artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="storyboard_shot",
        scope_type="storyboard_checkpoint",
        scope_id="e1:2",
        status="approved",
        trust_level="T2",
        content=old_shot.model_dump(mode="json"),
    ))
    conn.execute(
        "UPDATE shots SET storyboard_artifact_id=? WHERE id='s2'",
        (old_artifact["id"],),
    )
    conn.commit()

    _open_shot_gap(conn, "e1", 2)
    inserted = _shot(2, action="新增主线镜头")
    inserted.is_final = False
    _insert_storyboard_shot(conn, "e1", screenplay, inserted, "sp1")
    conn.commit()
    board = _current_board(conn)

    assert _storyboard_shot_evidence_requires_rebind(conn, "e1", board) is True
    rows = _ensure_current_storyboard_shot_artifacts(conn, "e1", board)

    assert len(rows) == 4
    assert _storyboard_shot_evidence_requires_rebind(conn, "e1", board) is False
    for row, shot in zip(rows, board.shots):
        artifact = evidence_repository.get_artifact(row["storyboard_artifact_id"])
        assert artifact is not None
        assert artifact["scope_id"] == f"e1:{shot.shot_no}"
        assert artifact["status"] == "approved"
        assert artifact["content_hash"] == evidence_repository.content_hash(
            shot.model_dump(mode="json")
        )
    historical = evidence_repository.get_artifact(old_artifact["id"])
    assert historical is not None
    assert historical["content"]["shot_no"] == 2
    assert historical["status"] == "superseded"


def test_generated_shot_cannot_claim_human_duration_review(repair_db) -> None:
    conn, screenplay = repair_db
    generated = _shot(4, action="模型新增镜头")
    generated.duration_s = 10
    generated.risk_tags = ["duration_human_reviewed"]

    _insert_storyboard_shot(conn, "e1", screenplay, generated, "sp1")
    row = conn.execute(
        "SELECT shot_contract_json FROM shots WHERE episode_id='e1' AND shot_no=4"
    ).fetchone()

    assert "duration_human_reviewed" not in json.loads(
        row["shot_contract_json"]
    )["risk_tags"]


def test_validated_projection_does_not_leak_unrelated_derived_fields(repair_db) -> None:
    conn, _screenplay = repair_db
    current = _current_board(conn)
    replacement = _shot(2, action="已校验候选")
    checkpoint = SupervisorCheckpoint(
        episode_id="e1",
        last_repair={"mode": "replace", "window_start": 2, "window_end": 2},
        repair_candidate_shots=[replacement.model_dump(mode="json")],
    )
    evaluated = _merge_repair_candidate(current, checkpoint)
    evaluated.shots[0].camera_angle = "俯视"
    evaluated.shots[1].shot_size = "全景"

    projected = _validated_candidate_projection(current, evaluated, checkpoint)

    assert projected.shots[0].camera_angle == current.shots[0].camera_angle
    assert projected.shots[1].shot_size == "全景"


def test_repair_feedback_is_localized_to_target_shot() -> None:
    messages = [
        "shots[9](shot_no=10).camera_move=「环绕」不在合法枚举中",
        "[FRAME_STATE_INVALID] 第 13 镜首帧与 planned_state_in 不一致",
    ]

    assert _repair_feedback_for_shot(messages, 10) == [messages[0]]
    assert _repair_feedback_for_shot(messages, 13) == [messages[1]]


def test_candidate_can_commit_one_of_two_independent_gate_repairs() -> None:
    shot_11 = "shot_no=11 的对白同时包含剧情道具操作，shot_size 不得为特写"
    shot_13 = "shot_no=13 的对白同时包含走位/离场等大形体动作，不能用单人大近景替代"

    assert _repair_candidate_made_progress(
        mode="replace",
        candidate_passed=False,
        before_messages=[shot_11, shot_13],
        after_messages=[shot_13],
    ) is True


def test_candidate_partial_progress_rejects_new_hard_gate_regression() -> None:
    shot_11 = "shot_no=11 的对白同时包含剧情道具操作，shot_size 不得为特写"
    shot_13 = "shot_no=13 的对白同时包含走位/离场等大形体动作，不能用单人大近景替代"

    assert _repair_candidate_made_progress(
        mode="replace",
        candidate_passed=False,
        before_messages=[shot_11, shot_13],
        after_messages=[shot_13, "shot_no=7 缺少必填字段：source_excerpt"],
    ) is False


def test_candidate_rejects_new_gate_outside_repair_window() -> None:
    assert _repair_candidate_made_progress(
        mode="replace",
        candidate_passed=False,
        before_messages=["shot_no=8 缺少有效口播"],
        after_messages=["镜头 10 声音身份未绑定"],
        window_start=8,
        window_end=8,
    ) is False


def test_candidate_without_any_resolved_target_is_not_progress() -> None:
    issue = "shot_no=11 的对白同时包含剧情道具操作，shot_size 不得为特写"

    assert _repair_candidate_made_progress(
        mode="replace",
        candidate_passed=False,
        before_messages=[issue],
        after_messages=[issue],
    ) is False


def test_candidate_can_resolve_one_target_from_combined_spine_issue() -> None:
    conflict = "shot_no=16 dialogues 与 audio_timeline 的口播内容分叉"
    warning = "[FRAME_STATE_INVALID] shots[1](shot_no=2).last_frame_desc 与 planned_state_out 不一致"
    combined_spine = (
        "主线节拍主体已入画但未完成对应动作/对白交付："
        "S01/李富贵:一路哭闹抱怨；S02/路人丙:交代杂役规则并发放凝气卷"
    )
    remaining_spine = (
        "主线节拍主体已入画但未完成对应动作/对白交付："
        "S02/路人丙:交代杂役规则并发放凝气卷"
    )

    assert _repair_candidate_made_progress(
        mode="insert",
        candidate_passed=False,
        before_messages=[conflict, combined_spine, warning],
        after_messages=[conflict, remaining_spine, warning],
    ) is True


@pytest.mark.parametrize(
    "action, initial_size, expected_size, staging_tag",
    [
        (
            "少年翻开手中名册，同时宣布下一位测试者的名字。",
            "特写",
            "近景",
            "dialogue_action_prop_staging",
        ),
        (
            "少年说完后转身穿过人群，走向广场出口。",
            "近景",
            "中景",
            "dialogue_action_spatial_staging",
        ),
    ],
)
def test_deterministic_action_dialogue_framing_candidate(
    action: str,
    initial_size: str,
    expected_size: str,
    staging_tag: str,
) -> None:
    shot = _shot(2, action=action)
    shot.shot_size = initial_size
    shot.dialogues = [Dialogue(speaker="少年", line="下一位。", emotion="平静")]
    shot.risk_tags = [staging_tag]

    candidate = _deterministic_dialogue_framing_candidate(shot)

    assert candidate is not None
    assert candidate.shot_size == expected_size
    assert "dialogue_action_staging" in candidate.risk_tags
    assert shot.shot_size == initial_size






def test_deterministic_ambient_audio_cast_candidate_removes_identity_claim() -> None:
    shot = _shot(2, action="门外传来一阵敲击声。")
    shot.audio_cast = ["未绑定的拟音标签"]
    shot.audio_timeline = [AudioTimelineItem(
        start_s=0.5,
        end_s=2.0,
        type="ambient_sound",
        speaker_id=None,
        text="咚咚",
        lip_sync=False,
    )]

    candidate = _deterministic_ambient_audio_cast_candidate(shot)

    assert candidate is not None
    assert candidate.audio_cast == []
    assert candidate.audio_timeline == shot.audio_timeline
    assert shot.audio_cast == ["未绑定的拟音标签"]




def test_repair_plan_uses_deterministic_dialogue_candidate_without_provider(repair_db) -> None:
    conn, _screenplay = repair_db
    shot = _shot(2, action="少年翻开手中名册，同时宣布下一位测试者的名字。")
    shot.shot_size = "特写"
    shot.dialogues = [Dialogue(speaker="少年", line="下一位。", emotion="平静")]
    shot.risk_tags = ["dialogue_action_prop_staging"]
    conn.execute(
        "UPDATE shots SET shot_size=?,action_desc=?,dialogues=?,shot_contract_json=? WHERE id='s2'",
        (
            shot.shot_size,
            shot.action_desc,
            json.dumps([dialogue.model_dump() for dialogue in shot.dialogues], ensure_ascii=False),
            json.dumps(shot.model_dump(mode="json"), ensure_ascii=False),
        ),
    )
    conn.commit()
    issue = (
        "shot_no=2 的对白同时包含剧情道具操作，shot_size 不得为特写；"
        "请至少使用近景并完整保留双手、道具和接触关系"
    )
    plan = route_issues(
        [issue],
        validated_prefix_end=3,
        semantic_diagnosis={
            "scope": "current_shot",
            "selected_strategy": "repair_current",
            "reason": "景别与双手道具构图可在当前镜确定性修正",
        },
    )
    checkpoint = SupervisorCheckpoint(
        episode_id="e1",
        planner_version=STORYBOARD_REPAIR_PLANNER_VERSION,
        validated_prefix_end=3,
    )

    planned = _apply_repair(
        checkpoint, plan, conn, "e1", list(_current_board(conn).shots), None,
    )

    assert planned.last_repair["status"] == "candidate_generating"
    assert planned.last_repair["deterministic_repair"] == "dialogue_action_framing"
    assert len(planned.repair_candidate_shots) == 1
    assert planned.repair_candidate_shots[0]["shot_size"] == "近景"




def test_early_final_marker_cannot_finish_before_persisted_plan() -> None:
    shots = [_shot(1), _shot(2), _shot(3)]

    assert shots[-1].is_final is True
    assert _storyboard_generation_is_complete(shots, planned_total=6, max_shots=20) is False
    assert _storyboard_generation_is_complete(shots, planned_total=3, max_shots=20) is True


def test_finalize_rejects_short_storyboard_without_publishing_certificate(repair_db) -> None:
    conn, _screenplay = repair_db
    conn.execute(
        "UPDATE episodes SET storyboard_outline_json=? WHERE id='e1'",
        (json.dumps({"shots": [{}, {}, {}, {}]}, ensure_ascii=False),),
    )
    conn.commit()

    with pytest.raises(RuntimeError, match="已完成 3/4 镜"):
        _finalize_storyboard_evidence("e1", _current_board(conn))

    episode = conn.execute(
        "SELECT storyboard_artifact_id,storyboard_completion_certificate_id "
        "FROM episodes WHERE id='e1'",
    ).fetchone()
    assert episode["storyboard_artifact_id"] is None
    assert episode["storyboard_completion_certificate_id"] is None
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM completion_certificates WHERE kind='storyboard' AND scope_id='e1'",
    ).fetchone()["c"] == 0


def test_finalize_never_invents_missing_final_marker(repair_db) -> None:
    conn, _screenplay = repair_db
    board = _current_board(conn)
    board.shots[-1].is_final = False

    with pytest.raises(RuntimeError, match="最终镜未标记收束"):
        _finalize_storyboard_evidence("e1", board)

    assert board.shots[-1].is_final is False
    assert conn.execute(
        "SELECT storyboard_artifact_id FROM episodes WHERE id='e1'",
    ).fetchone()[0] is None


@pytest.mark.parametrize(
    ("hard_gate_passed", "runtime_blocking", "expected_recovery"),
    [(0, 1, False), (1, 0, True)],
)
def test_outline_recovery_requires_nonblocking_evaluation(
    repair_db,
    hard_gate_passed: int,
    runtime_blocking: int,
    expected_recovery: bool,
) -> None:
    conn, _screenplay = repair_db
    outline = StoryboardOutline(
        episode_no=1,
        shots=[StoryboardOutlineShot(
            shot_no=1,
            shot_id="SH-RECOVER",
            beat="少年走到石碑前查看结果",
        )],
    )
    conn.execute(
        """INSERT INTO artifacts(
               id,type,scope_type,scope_id,version,status,trust_level,content_json,
               content_hash,parent_artifact_ids_json,created_at
           ) VALUES('outline-recovery','storyboard_outline','episode','e1',1,
                    'candidate','T1',?,'outline-recovery-hash','[]',10)""",
        (outline.model_dump_json(),),
    )
    conn.execute(
        """INSERT INTO evaluations(
               id,artifact_id,evaluator_type,evaluator_name,evaluator_version,
               status,hard_gate_passed,evaluation_role,runtime_blocking,
               issues_json,created_at
           ) VALUES('eval-outline-recovery','outline-recovery','deterministic',
                    'storyboard_outline_validator','3.0.0',?,?,?,?,'[]',11)""",
        (
            "warning" if hard_gate_passed else "failed",
            hard_gate_passed,
            "score_only" if hard_gate_passed else None,
            runtime_blocking,
        ),
    )
    conn.commit()
    episode = conn.execute("SELECT * FROM episodes WHERE id='e1'").fetchone()
    checkpoint = SupervisorCheckpoint(episode_id="e1")

    recovered = _recover_outline_from_current_artifact(
        conn,
        episode,
        checkpoint,
    )

    assert (recovered is not None) is expected_recovery
    if expected_recovery:
        assert checkpoint.outline_artifact_id == "outline-recovery"
    else:
        assert checkpoint.outline_artifact_id is None


def test_incomplete_success_recovers_only_current_screenplay_approved_outline(
    repair_db,
) -> None:
    conn, _screenplay = repair_db
    # Reuse valid schema-bearing outlines from the fixture's shot data.
    current_outline = {
        "episode_no": 1,
        "shots": [
            {"shot_no": n, "scene_setting": "日，广场", "beat": f"节拍{n}"}
            for n in range(1, 3)
        ],
    }
    approved_outline = {
        "episode_no": 1,
        "shots": [
            {"shot_no": n, "scene_setting": "日，广场", "beat": f"节拍{n}"}
            for n in range(1, 5)
        ],
    }
    conn.execute(
        """INSERT INTO artifacts(
               id,type,scope_type,scope_id,version,status,trust_level,content_json,
               content_hash,parent_artifact_ids_json,created_at
           ) VALUES('outline-current','storyboard_outline','episode','e1',1,'approved','T2',
                    ?,'hash-outline',?,2)""",
        (json.dumps(approved_outline, ensure_ascii=False), json.dumps(["sp1"])),
    )
    conn.execute(
        """UPDATE episodes SET storyboard_outline_json=?,storyboard_artifact_id='bad-board'
           WHERE id='e1'""",
        (json.dumps(current_outline, ensure_ascii=False),),
    )
    conn.commit()
    ep = conn.execute("SELECT * FROM episodes WHERE id='e1'").fetchone()
    checkpoint = SupervisorCheckpoint(
        episode_id="e1",
        phase="SUCCEEDED",
        outcome="SUCCEEDED_READY_FOR_CONFIRM",
        expected_total=6,
    )

    recovered = _recover_truncated_outline_from_approved_artifact(
        conn,
        ep,
        checkpoint,
        StoryboardOutline.model_validate(current_outline),
    )

    assert recovered is not None and len(recovered.shots) == 4
    assert checkpoint.expected_total == 4
    assert checkpoint.phase == "VALIDATING_OUTLINE"


def test_full_gate_finalize_publishes_with_completion_certificate(repair_db) -> None:
    conn, _screenplay = repair_db
    board = _current_board(conn)

    artifact_id = _finalize_storyboard_evidence("e1", board)

    episode = conn.execute(
        """SELECT storyboard_artifact_id,published_storyboard_artifact_id,
                  working_storyboard_artifact_id,storyboard_completion_certificate_id
           FROM episodes WHERE id='e1'"""
    ).fetchone()
    assert episode["storyboard_artifact_id"] == artifact_id
    assert episode["published_storyboard_artifact_id"] == artifact_id
    assert episode["working_storyboard_artifact_id"] == artifact_id
    assert episode["storyboard_completion_certificate_id"]
    certificate = conn.execute(
        "SELECT consumed_at FROM completion_certificates WHERE id=?",
        (episode["storyboard_completion_certificate_id"],),
    ).fetchone()
    assert certificate is not None and certificate["consumed_at"] is not None
