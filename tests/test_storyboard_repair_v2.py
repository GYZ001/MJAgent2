from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from app import db
from app.domain.storyboard_ops import (
    _board_from_shot_rows,
    _finalize_storyboard_evidence,
    _insert_storyboard_shot,
    _new_storyboard_recorder,
)
from app.orchestration.engine import WorkflowRecorder
from app.repair_router import route_issues
from app.schemas import (
    AudioTimelineItem,
    Dialogue,
    EpisodeScreenplay,
    Shot,
    Storyboard,
    StoryboardOutline,
    StoryboardOutlineShot,
)
from app.storyboard_supervisor import (
    STORYBOARD_REPAIR_PLANNER_VERSION,
    SupervisorCheckpoint,
    _apply_repair,
    _begin_repair_activation,
    _deterministic_ambient_audio_cast_candidate,
    _deterministic_dialogue_framing_candidate,
    _migrate_checkpoint,
    _repair_is_pending,
    _recover_outline_from_current_artifact,
    _retarget_spine_repair_brief,
    _storyboard_hash,
    prepare_published_storyboard_repair,
    save_checkpoint,
)


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
    recorder.cancel("superseded", conn=None)
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


def test_repair_missing_generated_bindings_is_idempotent_for_76_to_82(
    repair_db,
) -> None:
    conn, screenplay = repair_db
    conn.execute("DELETE FROM shots WHERE episode_id='e1'")
    for shot_no in range(76, 83):
        shot = _shot(shot_no)
        shot.is_final = shot_no == 82
        _insert_storyboard_shot(conn, "e1", screenplay, shot, "sp1")
    conn.commit()

    from app.storyboard_workspace import repair_generated_source_bindings

    first = repair_generated_source_bindings("e1")
    second = repair_generated_source_bindings("e1")

    assert first == {
        "bound": 7,
        "realigned": 0,
        "unresolved_shot_nos": [],
    }
    assert second == {
        "bound": 0,
        "realigned": 0,
        "unresolved_shot_nos": [],
    }
    assert conn.execute(
        "SELECT COUNT(*) FROM storyboard_source_bindings"
    ).fetchone()[0] == 7


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


def test_published_multi_shot_gate_repair_covers_all_explicit_targets(
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
        [
            "第 1 镜的可见身份不属于本镜叙事任务",
            "第 3 镜的可见身份不属于本镜叙事任务",
        ],
    )

    assert _repair_is_pending(checkpoint)
    assert checkpoint.last_repair["mode"] == "replace"
    assert checkpoint.last_repair["window_start"] == 1
    assert checkpoint.last_repair["window_end"] == 3
    assert checkpoint.repair_candidate_shots == []
    assert _storyboard_hash(_current_board(conn)) == before


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
