from __future__ import annotations

import json
import threading

import pytest

from app import db
from app.domain.storyboard_ops import _board_from_shot_rows, _finalize_storyboard_evidence
from app.evidence import repository as evidence_repository
from app.harness.types import EvidenceArtifact
from app.production.revision import ensure_production_revision
from app.repair_router import route_issues
from app.schemas import Dialogue, EpisodeScreenplay, Shot, Storyboard, StoryboardOutline
from app.storyboard_supervisor import (
    STORYBOARD_REPAIR_PLANNER_VERSION,
    SupervisorCheckpoint,
    _apply_repair,
    _begin_repair_activation,
    _commit_repair_candidate,
    _deterministic_dialogue_framing_candidate,
    _merge_repair_candidate,
    _migrate_checkpoint,
    _repair_feedback_for_shot,
    _recover_truncated_outline_from_approved_artifact,
    _repair_candidate_made_progress,
    _storyboard_generation_is_complete,
    _storyboard_hash,
    _validated_candidate_projection,
    _withdraw_legacy_failed_publication,
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
    plan = route_issues(["第 2 镜首帧画面含超纲细节词：衣角，请删除"])
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
        "shots[9](shot_no=10) 起连续 3 个镜头景别相同",
        "第 13 镜首帧画面含超纲细节词：衣角、指节",
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


def test_candidate_without_any_resolved_target_is_not_progress() -> None:
    issue = "shot_no=11 的对白同时包含剧情道具操作，shot_size 不得为特写"

    assert _repair_candidate_made_progress(
        mode="replace",
        candidate_passed=False,
        before_messages=[issue],
        after_messages=[issue],
    ) is False


@pytest.mark.parametrize(
    "action, initial_size, expected_size",
    [
        ("少年翻开手中名册，同时宣布下一位测试者的名字。", "特写", "近景"),
        ("少年说完后转身穿过人群，走向广场出口。", "近景", "中景"),
    ],
)
def test_deterministic_action_dialogue_framing_candidate(
    action: str, initial_size: str, expected_size: str,
) -> None:
    shot = _shot(2, action=action)
    shot.shot_size = initial_size
    shot.dialogues = [Dialogue(speaker="少年", line="下一位。", emotion="平静")]

    candidate = _deterministic_dialogue_framing_candidate(shot)

    assert candidate is not None
    assert candidate.shot_size == expected_size
    assert "dialogue_action_staging" in candidate.risk_tags
    assert shot.shot_size == initial_size


def test_repair_plan_uses_deterministic_dialogue_candidate_without_provider(repair_db) -> None:
    conn, _screenplay = repair_db
    shot = _shot(2, action="少年翻开手中名册，同时宣布下一位测试者的名字。")
    shot.shot_size = "特写"
    shot.dialogues = [Dialogue(speaker="少年", line="下一位。", emotion="平静")]
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
    plan = route_issues([issue], validated_prefix_end=3)
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
