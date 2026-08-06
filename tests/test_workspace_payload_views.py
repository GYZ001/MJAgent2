from __future__ import annotations

import json
import sqlite3

from app import db, storyboard_workspace
from app.domain import common, projects, storyboard_ops
from app.media_pipeline.status import episode_pipeline_statuses
from app.schemas import EpisodeScreenplay


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for statement in db.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    return conn


def _seed_episode(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES('p1','demo','created',1)"
    )
    conn.execute(
        """INSERT INTO episodes(
               id, project_id, episode_no, title, source_chapters,
               screenplay_status, status, created_at
           ) VALUES('e1','p1',1,'episode 1','[1]','ready','confirmed',1)"""
    )
    conn.execute(
        """INSERT INTO shots(
               id, episode_id, shot_no, duration_s, shot_size, camera_move,
               scene_setting, characters, action_desc, narration, dialogues,
               transition, continuity_from_prev
           ) VALUES('s1','e1',1,5,'medium','static','room','[]','action','','[]','cut',0)"""
    )
    small_inputs = json.dumps({
        "reference_images": [{"id": "ref-small", "path": "missing.jpg"}],
    })
    large_inputs = json.dumps({"embedded": "x" * 1_000_100})
    conn.executemany(
        """INSERT INTO shot_versions(
               id, shot_id, version_no, prompt_text, idem_key, status,
               video_path, qa_json, cost_cny, latency_s, image_inputs, created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            ("v1", "s1", 1, "prompt 1", "idem-1", "succeeded", "", None, 1, 2, small_inputs, 1),
            ("v2", "s1", 2, "prompt 2", "idem-2", "succeeded", "", None, 1, 2, large_inputs, 2),
        ],
    )
    conn.execute("UPDATE shots SET adopted_version_id='v1' WHERE id='s1'")
    conn.commit()


def _patch_storyboard_db(monkeypatch, conn: sqlite3.Connection) -> None:
    monkeypatch.setattr(common, "get_conn", lambda: conn)
    monkeypatch.setattr(storyboard_ops, "get_conn", lambda: conn)
    monkeypatch.setattr(storyboard_workspace, "get_conn", lambda: conn)
    monkeypatch.setattr(storyboard_ops, "get_setting", lambda _key: "100")
    monkeypatch.setattr(storyboard_ops.worker, "episode_cost", lambda _episode_id: 0.0)


def test_workspace_episode_views_do_not_expand_historical_inputs(monkeypatch) -> None:
    conn = _conn()
    _seed_episode(conn)
    _patch_storyboard_db(monkeypatch, conn)

    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    script = storyboard_ops.episode_detail("e1", "script")
    board = storyboard_ops.episode_detail("e1", "board")
    wall = storyboard_ops.episode_detail("e1", "wall")

    assert script["shot_count"] == 1
    assert script["shots"] == []
    assert board["shots"][0]["versions"] == []
    assert board["shots"][0]["version_count"] == 2
    assert len(wall["shots"][0]["versions"]) == 2
    assert wall["shots"][0]["video_status"] == "adopted"
    assert all(not v["image_inputs"]["reference_images"] for v in wall["shots"][0]["versions"])
    assert not any("SELECT * FROM shot_versions" in sql for sql in statements)
    assert not any("json_extract" in sql.lower() for sql in statements)


def test_narrative_summaries_and_shot_contract_are_scoped_to_script_and_board(
    monkeypatch,
) -> None:
    conn = _conn()
    _seed_episode(conn)
    screenplay = {
        "episode_no": 1,
        "narrative_plan": {
            "contract_version": "narrative-continuity.v1",
            "scope_id": "e1",
            "propositions": [
                {
                    "proposition_id": "PROP-1",
                    "canonical_statement": "The letter changes the hero's goal.",
                    "narrative_domain": "adapted_story",
                }
            ],
            "events": [{"event_id": "EVENT-1"}],
            "audience_priors": [
                {
                    "audience_prior_id": "PRIOR-1",
                    "audience_description": "First-time viewer",
                }
            ],
            "experience_intents": [
                {
                    "experience_intent_id": "INTENT-1",
                    "scope_id": "e1",
                    "director_objective": "Notice the letter before the reaction.",
                }
            ],
            "assimilation_tasks": [
                {
                    "assimilation_task_id": "TASK-1",
                    "experience_intent_id": "INTENT-1",
                    "audience_path_id": "PATH-1",
                    "target_delta_id": "DELTA-1",
                    "satisfaction_criteria": "Viewer connects the letter to the reaction.",
                }
            ],
        },
    }
    conn.execute(
        "UPDATE episodes SET screenplay_json=? WHERE id='e1'",
        (json.dumps(screenplay),),
    )
    shot_contract = {
        "shot_id": "SHOT-1",
        "scene_id": "SCENE-1",
        "event_ids": ["EVENT-1"],
        "primary_action_id": "ACTION-1",
        "supporting_action_ids": ["ACTION-2"],
        "shot_contribution": {
            "shot_contribution_id": "CONTRIB-1",
            "experience_intent_ids": ["INTENT-1"],
            "target_delta_ids": ["DELTA-1"],
            "assimilation_task_ids": ["TASK-1"],
            "evidence_ids": ["EVIDENCE-1"],
        },
        "audience_state_paths": [
            {
                "audience_prior_id": "PRIOR-1",
                "audience_state_in_id": "AUDIENCE-IN-1",
                "audience_state_out_target_id": "AUDIENCE-OUT-1",
            }
        ],
        "planned_state_in_fact_ids": ["FACT-1"],
        "planned_delta_add_fact_ids": ["FACT-2"],
        "planned_delta_remove_fact_ids": ["FACT-1"],
        "planned_state_out_fact_ids": ["FACT-2"],
        "completed_before_action_ids": ["ACTION-0"],
        "reserved_future_event_ids": ["EVENT-2"],
        "readability_window_ids": ["WINDOW-1"],
        "narrative_boundary_from_previous": {
            "boundary_id": "BOUNDARY-1",
            "previous_shot_id": "SHOT-0",
            "next_shot_id": "SHOT-1",
            "narrative_relation": "consequence",
            "forbidden_replay_action_ids": ["ACTION-0"],
            "cut_motivation": "Reveal the consequence, not the completed action.",
        },
    }
    conn.execute(
        "UPDATE shots SET shot_contract_json=? WHERE id='s1'",
        (json.dumps(shot_contract),),
    )
    report_rows = [
        (
            "review-1",
            1,
            "validated",
            {"decision": "pass", "low_percentile_result": {"score": 0.9}},
        ),
        (
            "review-2",
            2,
            "needs_revision",
            {
                "decision": "revise",
                "low_percentile_result": {"score": 0.35, "prior_id": "PRIOR-1"},
                "inference_variance": 0.42,
                "reason": "The causal link is not yet readable.",
                "evidence_gap_ids": ["PRIVATE-GAP-1"],
            },
        ),
        (
            "review-3",
            3,
            "stale",
            {"decision": "pass", "reason": "Historical result"},
        ),
    ]
    conn.executemany(
        """INSERT INTO artifacts(
               id, type, scope_type, scope_id, version, status, trust_level,
               content_json, content_hash, created_at
           ) VALUES(?, 'narrative_review_report', 'episode', 'e1', ?, ?, 'T2', ?, ?, ?)""",
        [
            (artifact_id, version, status, json.dumps(content), artifact_id, version)
            for artifact_id, version, status, content in report_rows
        ],
    )
    conn.commit()
    _patch_storyboard_db(monkeypatch, conn)

    script = storyboard_ops.episode_detail("e1", "script")
    board = storyboard_ops.episode_detail("e1", "board")
    wall = storyboard_ops.episode_detail("e1", "wall")

    expected_contract_summary = {
        "contract_version": "narrative-continuity.v1",
        "proposition_count": 1,
        "event_count": 1,
        "audience_prior_count": 1,
        "experience_intent_count": 1,
        "assimilation_task_count": 1,
    }
    expected_review_summary = {
        "artifact_id": "review-2",
        "version": 2,
        "status": "needs_revision",
        "decision": "revise",
        "low_percentile": {"score": 0.35, "prior_id": "PRIOR-1"},
        "inference_variance": 0.42,
        "reason": "The causal link is not yet readable.",
    }
    assert script["narrative_contract_summary"] == expected_contract_summary
    assert board["narrative_contract_summary"] == expected_contract_summary
    assert script["narrative_review_summary"] == expected_review_summary
    assert board["narrative_review_summary"] == expected_review_summary
    assert "evidence_gap_ids" not in board["narrative_review_summary"]
    assert wall["narrative_contract_summary"] is None
    assert wall["narrative_review_summary"] is None
    assert board["narrative_metrics"]["contract_present"] is True
    assert "audience_processing_debt" in board["narrative_metrics"]
    assert wall["narrative_metrics"] is None

    public_shot = board["shots"][0]
    for key, expected in shot_contract.items():
        if key in {"shot_contribution", "narrative_boundary_from_previous"}:
            continue
        assert public_shot[key] == expected
    for key, expected in shot_contract["shot_contribution"].items():
        assert public_shot["shot_contribution"][key] == expected
    for key, expected in shot_contract["narrative_boundary_from_previous"].items():
        assert public_shot["narrative_boundary_from_previous"][key] == expected


def test_review_detail_omits_oversized_legacy_inputs(monkeypatch) -> None:
    conn = _conn()
    _seed_episode(conn)
    _patch_storyboard_db(monkeypatch, conn)

    review = storyboard_ops.shot_review_detail("s1")
    versions = {version["id"]: version for version in review["versions"]}

    assert versions["v1"]["image_inputs"]["reference_images"][0]["id"] == "ref-small"
    assert versions["v1"]["image_inputs"]["omitted_for_size"] is False
    assert versions["v2"]["image_inputs"]["omitted_for_size"] is True
    assert versions["v2"]["image_inputs"]["reference_images"] == []
    assert review["video_status"] == "adopted"


def test_review_detail_reads_published_artifact_when_write_authority_is_stale(
    monkeypatch,
) -> None:
    conn = _conn()
    _seed_episode(conn)
    conn.execute(
        """UPDATE episodes
              SET screenplay_artifact_id='screenplay-published',
                  published_screenplay_artifact_id='screenplay-published'
            WHERE id='e1'"""
    )
    conn.commit()
    _patch_storyboard_db(monkeypatch, conn)
    monkeypatch.setattr(
        "app.production.screenplay_authority.resolve_downstream_screenplay",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("完成凭证 input_fingerprint 已变化")
        ),
    )
    monkeypatch.setattr(
        "app.production.screenplay_authority.episode_requires_immutable_screenplay_authority",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "app.production.patch.load_screenplay_from_artifact",
        lambda artifact_id: EpisodeScreenplay(
            episode_no=1,
            title=f"published:{artifact_id}",
            full_script_text="【场1】日 / 室内\n角色完成动作。",
        ),
    )

    review = storyboard_ops.shot_review_detail("s1")

    assert review["id"] == "s1"
    assert review["video_status"] == "adopted"


def test_pipeline_reference_query_is_scoped_to_episode() -> None:
    conn = _conn()
    _seed_episode(conn)
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    statuses, summary = episode_pipeline_statuses("e1", conn=conn)

    assert "s1" in statuses
    assert summary["shots_total"] == 1
    reference_queries = [sql for sql in statements if "FROM reference_sets" in sql]
    assert len(reference_queries) == 1
    assert "s.episode_id='e1'" in reference_queries[0]


def test_review_wall_video_statuses_are_exactly_five_and_adopted_wins() -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES('p1','demo','created',1)"
    )
    conn.execute(
        """INSERT INTO episodes(id, project_id, episode_no, status, created_at)
           VALUES('e1','p1',1,'generating',1)"""
    )
    conn.executemany(
        """INSERT INTO shots(id, episode_id, shot_no, duration_s, characters, dialogues)
           VALUES(?, 'e1', ?, 5, '[]', '[]')""",
        [
            ("s_adopted", 1),
            ("s_generating", 2),
            ("s_pending_adoption", 3),
            ("s_failed", 4),
            ("s_pending_generation", 5),
        ],
    )
    conn.executemany(
        """INSERT INTO shot_versions(
               id, shot_id, version_no, prompt_text, idem_key, status, video_path, created_at
           ) VALUES(?, ?, ?, 'prompt', ?, ?, ?, 1)""",
        [
            ("v_adopted", "s_adopted", 1, "idem-adopted", "succeeded", "/tmp/adopted.mp4"),
            ("v_adopted_retake", "s_adopted", 2, "idem-adopted-retake", "running", None),
            ("v_generating", "s_generating", 1, "idem-generating", "queued", None),
            ("v_candidate", "s_pending_adoption", 1, "idem-candidate", "succeeded", "/tmp/candidate.mp4"),
            ("v_failed", "s_failed", 1, "idem-failed", "failed", None),
        ],
    )
    conn.execute(
        "UPDATE shots SET adopted_version_id='v_adopted' WHERE id='s_adopted'"
    )
    conn.executemany(
        """INSERT INTO jobs(
               id, kind, shot_id, version_id, episode_id, project_id, status, created_at, updated_at
           ) VALUES(?, 'video', ?, ?, 'e1', 'p1', ?, 1, 1)""",
        [
            ("j_adopted_retake", "s_adopted", "v_adopted_retake", "running"),
            ("j_generating", "s_generating", "v_generating", "queued"),
        ],
    )
    conn.commit()

    statuses, summary = episode_pipeline_statuses("e1", conn=conn)

    assert statuses["s_adopted"]["video_status"] == "adopted"
    assert statuses["s_adopted"]["video_status_label"] == "已采纳"
    assert statuses["s_generating"]["video_status"] == "generating"
    assert statuses["s_pending_adoption"]["video_status"] == "pending_adoption"
    assert statuses["s_failed"]["video_status"] == "generation_failed"
    assert statuses["s_pending_generation"]["video_status"] == "pending_generation"
    assert summary["video_status_counts"] == {
        "pending_generation": 1,
        "generating": 1,
        "pending_adoption": 1,
        "adopted": 1,
        "generation_failed": 1,
    }


def test_review_wall_adoption_pointer_wins_even_when_media_health_is_bad() -> None:
    """补齐状态由采用决定；媒体健康问题不能把已采用镜头重新归为待生成。"""
    conn = _conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES('p1','demo','created',1)"
    )
    conn.execute(
        """INSERT INTO episodes(id, project_id, episode_no, status, created_at)
           VALUES('e1','p1',1,'confirmed',1)"""
    )
    conn.execute(
        """INSERT INTO shots(id, episode_id, shot_no, duration_s, characters, dialogues,
                             adopted_version_id)
           VALUES('s1','e1',1,5,'[]','[]','missing_version')"""
    )
    conn.commit()

    statuses, summary = episode_pipeline_statuses("e1", conn=conn)

    assert statuses["s1"]["video_status"] == "adopted"
    assert summary["video_status_counts"]["adopted"] == 1


def test_project_episode_view_is_server_paginated(monkeypatch) -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES('p1','demo','created',1)"
    )
    for idx in range(1, 41):
        conn.execute(
            "INSERT INTO chapters(project_id, idx, title, content) VALUES('p1',?,?,?)",
            (idx, f"chapter {idx}", f"content {idx}"),
        )
        conn.execute(
            """INSERT INTO episodes(
                   id, project_id, episode_no, title, source_chapters,
                   screenplay_status, status, created_at
               ) VALUES(?,?,?,?,?,'pending','planned',1)""",
            (f"e{idx}", "p1", idx, f"episode {idx}", json.dumps([idx])),
        )
    conn.commit()
    monkeypatch.setattr(common, "get_conn", lambda: conn)
    monkeypatch.setattr(projects, "get_conn", lambda: conn)
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    result = projects.project_detail("p1", view="episodes", page=2, page_size=15)

    assert [episode["episode_no"] for episode in result["episodes"]] == list(range(16, 31))
    assert result["episodes_total"] == 40
    assert result["episodes_page"] == 2
    assert result["episodes_page_count"] == 3
    assert result["episodes_query"] == ""
    assert result["episodes_status_filter"] == "all"
    assert len(result["chapters"]) == 15
    episode_queries = [sql for sql in statements if "FROM episodes" in sql and "ORDER BY episode_no" in sql]
    assert len(episode_queries) == 1
    assert "LIMIT 15 OFFSET 15" in episode_queries[0]
