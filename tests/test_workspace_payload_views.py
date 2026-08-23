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


def test_review_detail_projects_mode_specific_input_media(monkeypatch) -> None:
    conn = _conn()
    _seed_episode(conn)
    conn.execute(
        """INSERT INTO shots(
               id,episode_id,shot_no,duration_s,shot_size,camera_move,
               scene_setting,characters,action_desc,narration,dialogues,
               transition,continuity_from_prev
           ) VALUES('upstream','e1',2,5,'medium','static','room','[]',
                    'action','','[]','cut',0)"""
    )
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,
               video_path,cost_cny,latency_s,created_at
           ) VALUES('upstream-v','upstream',1,'prompt','upstream-idem',
                    'succeeded','/owned/upstream.mp4',1,1,1)"""
    )
    conn.execute(
        "UPDATE shot_versions SET image_inputs=? WHERE id='v1'",
        (json.dumps({
            "mode": "FIRST_LAST_FRAME_MODE",
            "first_frame_path": "/owned/first.jpg",
            "last_frame_path": "/owned/last.jpg",
            "boundary_pair_qa": {
                "first_frame_source": "PREVIOUS_STATIC_TAIL",
                "last_frame_source": "STATIC_BOUNDARY_ASSET",
            },
            "upstream_adopted_video_revision": "upstream-v",
            "video_input_url": "https://provider.example/signed.mp4",
        }),),
    )
    conn.commit()
    _patch_storyboard_db(monkeypatch, conn)
    monkeypatch.setattr(
        storyboard_ops,
        "_media_url",
        lambda path: f"/media/{str(path).rsplit('/', 1)[-1]}" if path else None,
    )

    review = storyboard_ops.shot_review_detail("s1")
    inputs = next(
        version["image_inputs"]
        for version in review["versions"]
        if version["id"] == "v1"
    )

    assert inputs["first_frame_image_url"] == "/media/first.jpg"
    assert inputs["first_frame_source"] == "PREVIOUS_STATIC_TAIL"
    assert inputs["last_frame_image_url"] == "/media/last.jpg"
    assert inputs["last_frame_source"] == "STATIC_BOUNDARY_ASSET"
    assert inputs["video_input_url"] == "/media/upstream.mp4"
    assert inputs["video_input_source_revision_id"] == "upstream-v"


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


def _seed_picker_project(conn: sqlite3.Connection, count: int = 40) -> None:
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES('p1','demo','created',1)"
    )
    for idx in range(1, count + 1):
        conn.execute(
            """INSERT INTO episodes(
                   id, project_id, episode_no, title, source_chapters,
                   screenplay_status, status, created_at
               ) VALUES(?,?,?,?,?,'pending','planned',1)""",
            (f"e{idx}", "p1", idx, f"episode {idx}", json.dumps([idx])),
        )
    conn.commit()


def test_picker_without_limit_keeps_returning_every_episode(monkeypatch) -> None:
    """旧契约：不带 episode_limit 时仍是整份分集，窗口字段不出现。"""
    conn = _conn()
    _seed_picker_project(conn)
    monkeypatch.setattr(common, "get_conn", lambda: conn)
    monkeypatch.setattr(projects, "get_conn", lambda: conn)

    result = projects.project_detail("p1", view="picker")

    assert len(result["episodes"]) == 40
    assert "episode_total" not in result


def test_picker_window_centers_on_cursor_and_reports_neighbors(monkeypatch) -> None:
    """窗口模式只回一段，但总数、序号与上/下一集必须仍然可用。

    回归 2026-08-21：1616 集项目里整份分集未压缩 250KB，中文标题 gzip 压不动，
    每次切集都重拉一遍，移动端明显卡顿。
    """
    conn = _conn()
    _seed_picker_project(conn)
    monkeypatch.setattr(common, "get_conn", lambda: conn)
    monkeypatch.setattr(projects, "get_conn", lambda: conn)

    result = projects.project_detail(
        "p1", view="picker", episode_limit=9, episode_cursor="e20"
    )

    numbers = [episode["episode_no"] for episode in result["episodes"]]
    assert len(numbers) == 9
    assert 20 in numbers, "光标分集必须落在窗口内，否则前端解析不出当前集"
    assert numbers == sorted(numbers)
    assert result["episode_total"] == 40
    assert result["episode_index"] == 19
    assert result["episode_current"]["id"] == "e20"
    assert result["episode_prev"]["episode_no"] == 19
    assert result["episode_next"]["episode_no"] == 21
    # 切换器用不到自动改动流水，不应随每次切集回传
    assert "bible_auto_changes_json" not in result


def test_picker_window_search_runs_on_the_server(monkeypatch) -> None:
    conn = _conn()
    _seed_picker_project(conn)
    monkeypatch.setattr(common, "get_conn", lambda: conn)
    monkeypatch.setattr(projects, "get_conn", lambda: conn)

    result = projects.project_detail(
        "p1", view="picker", episode_limit=60, episode_query="episode 3"
    )

    numbers = {episode["episode_no"] for episode in result["episodes"]}
    assert numbers == {3, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39}
    assert result["episode_match_total"] == 11
    assert result["episode_total"] == 40


def test_picker_window_keeps_cursor_even_when_filtered_out(monkeypatch) -> None:
    """搜索命中里没有当前集时也要带上它，否则切换器标题会退化成「第 N 集」。"""
    conn = _conn()
    _seed_picker_project(conn)
    monkeypatch.setattr(common, "get_conn", lambda: conn)
    monkeypatch.setattr(projects, "get_conn", lambda: conn)

    result = projects.project_detail(
        "p1", view="picker", episode_limit=60,
        episode_query="episode 7", episode_cursor="e20",
    )

    ids = [episode["id"] for episode in result["episodes"]]
    assert "e20" in ids
    assert result["episode_current"]["id"] == "e20"


def test_picker_window_limits_the_sql_itself(monkeypatch) -> None:
    """窗口必须由 SQL 完成——否则仍然把整份分集读进内存，等于没优化。"""
    conn = _conn()
    _seed_picker_project(conn)
    monkeypatch.setattr(common, "get_conn", lambda: conn)
    monkeypatch.setattr(projects, "get_conn", lambda: conn)
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    projects.project_detail("p1", view="picker", episode_limit=9, episode_cursor="e20")

    windowed = [sql for sql in statements if "LIMIT" in sql and "OFFSET" in sql]
    assert windowed, "没有生成带 LIMIT/OFFSET 的取窗语句"
    assert not [
        sql for sql in statements
        if "FROM episodes" in sql and "ORDER BY episode_no" in sql and "LIMIT" not in sql
    ], "仍然存在一条无上限的整表分集查询"


def _seed_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    workflow_type: str,
    scope_type: str,
    scope_id: str,
    status: str,
    started_at: float,
    finished_at: float | None = None,
) -> None:
    conn.execute(
        """INSERT INTO workflow_runs(
               id, workflow_type, scope_type, scope_id, status,
               input_fingerprint, started_at, finished_at, updated_at
           ) VALUES(?,?,?,?,?,'fp',?,?,?)""",
        (run_id, workflow_type, scope_type, scope_id, status,
         started_at, finished_at, finished_at or started_at),
    )
    conn.commit()


def test_project_task_timings_come_from_server_runs(monkeypatch) -> None:
    """任务计时必须由服务端 run 提供起止时间。

    前端曾把起点存在 localStorage：任务运行中刷新页面会让起点永久搁浅，下一个
    任务复用旧起点后显示出「已等待 1244 分」这类虚高时长。
    """
    conn = _conn()
    _seed_episode(conn)
    monkeypatch.setattr(common, "get_conn", lambda: conn)
    monkeypatch.setattr(projects, "get_conn", lambda: conn)

    _seed_run(conn, run_id="r_bible", workflow_type="character_bible",
              scope_type="project", scope_id="p1", status="SUCCEEDED",
              started_at=100.0, finished_at=160.0)
    _seed_run(conn, run_id="r_refs", workflow_type="character_references",
              scope_type="project", scope_id="p1", status="RUNNING",
              started_at=200.0)

    timings = projects.project_detail("p1", view="bible")["task_timings"]
    assert timings["bible"] == {"started_at": 100.0, "finished_at": 160.0}
    # 活跃 run 不给结束时间，否则计时会提前停住。
    assert timings["refs"] == {"started_at": 200.0, "finished_at": None}
    # 从未跑过的任务不编造时间。
    assert timings["scene_refs"] == {"started_at": None, "finished_at": None}


def test_project_task_timings_pick_the_latest_attempt(monkeypatch) -> None:
    """挂起态（PAUSED_EXTERNAL）也算活跃，不能让它盖过更近的一次尝试。"""
    conn = _conn()
    _seed_episode(conn)
    monkeypatch.setattr(common, "get_conn", lambda: conn)
    monkeypatch.setattr(projects, "get_conn", lambda: conn)

    _seed_run(conn, run_id="r_old", workflow_type="scene_references",
              scope_type="project", scope_id="p1", status="PAUSED_EXTERNAL",
              started_at=100.0)
    _seed_run(conn, run_id="r_new", workflow_type="scene_references",
              scope_type="project", scope_id="p1", status="SUCCEEDED",
              started_at=500.0, finished_at=560.0)

    timings = projects.project_detail("p1", view="scenes")["task_timings"]
    assert timings["scene_refs"] == {"started_at": 500.0, "finished_at": 560.0}


def test_storyboard_batch_timing_aggregates_active_runs(monkeypatch) -> None:
    """批量分镜没有父 run，只能按活跃子 run 的最早起点聚合。"""
    conn = _conn()
    _seed_episode(conn)
    monkeypatch.setattr(common, "get_conn", lambda: conn)
    monkeypatch.setattr(projects, "get_conn", lambda: conn)

    _seed_run(conn, run_id="r_sb_done", workflow_type="storyboard",
              scope_type="episode", scope_id="e1", status="SUCCEEDED",
              started_at=100.0, finished_at=150.0)
    assert projects.project_detail("p1", view="episodes")["task_timings"][
        "storyboard_batch"
    ] == {"started_at": None, "finished_at": None}

    _seed_run(conn, run_id="r_sb_live", workflow_type="storyboard",
              scope_type="episode", scope_id="e1", status="RUNNING",
              started_at=400.0)
    assert projects.project_detail("p1", view="episodes")["task_timings"][
        "storyboard_batch"
    ] == {"started_at": 400.0, "finished_at": None}


def test_storyboard_shot_timings_sum_all_retry_iterations(monkeypatch) -> None:
    """逐镜耗时累计全部重试迭代，仍在跑的那轮回报起点供前端实时叠加。"""
    conn = _conn()
    _seed_episode(conn)
    _patch_storyboard_db(monkeypatch, conn)

    _seed_run(conn, run_id="r_sb", workflow_type="storyboard",
              scope_type="episode", scope_id="e1", status="RUNNING",
              started_at=1_000.0)
    rows = [
        # 镜 1：四轮重试全部结束，累计 518702ms。
        ("sr1", "storyboard_shot_1.iteration", 1_000.0, 1_067.2, 67_243),
        ("sr2", "storyboard_shot_1.iteration", 1_070.0, 1_233.8, 163_830),
        ("sr3", "storyboard_shot_1.iteration", 1_240.0, 1_434.5, 194_568),
        ("sr4", "storyboard_shot_1.iteration", 1_440.0, 1_533.0, 93_061),
        # 镜 2：一轮已结束，第二轮仍在跑（finished_at 为空，latency_ms 尚未回填）。
        ("sr5", "storyboard_shot_2.iteration", 1_540.0, 1_600.0, 60_000),
        ("sr6", "storyboard_shot_2.iteration", 1_610.0, None, 0),
    ]
    conn.executemany(
        """INSERT INTO step_runs(id, run_id, step_key, iteration_no, status,
                                 started_at, finished_at, latency_ms)
           VALUES(?,'r_sb',?,1,'SUCCEEDED',?,?,?)""",
        rows,
    )
    conn.commit()

    timings = storyboard_ops.episode_detail("e1", view="board")["shot_timings"]
    assert timings["1"] == {
        "elapsed_ms": 518_702, "running_since": None, "iterations": 4,
    }
    # 在跑的那轮不计入 elapsed_ms，只交出起点。
    assert timings["2"] == {
        "elapsed_ms": 60_000, "running_since": 1_610.0, "iterations": 2,
    }


def test_storyboard_shot_timings_are_scoped_to_the_latest_run(monkeypatch) -> None:
    """重新生成后只算最近一次 run，不把上一次的耗时累加进来。"""
    conn = _conn()
    _seed_episode(conn)
    _patch_storyboard_db(monkeypatch, conn)

    _seed_run(conn, run_id="r_old", workflow_type="storyboard",
              scope_type="episode", scope_id="e1", status="CANCELLED",
              started_at=100.0, finished_at=200.0)
    _seed_run(conn, run_id="r_new", workflow_type="storyboard",
              scope_type="episode", scope_id="e1", status="RUNNING",
              started_at=900.0)
    conn.executemany(
        """INSERT INTO step_runs(id, run_id, step_key, iteration_no, status,
                                 started_at, finished_at, latency_ms)
           VALUES(?,?,'storyboard_shot_1.iteration',1,'SUCCEEDED',?,?,?)""",
        [
            ("old1", "r_old", 100.0, 190.0, 90_000),
            ("new1", "r_new", 900.0, 930.0, 30_000),
        ],
    )
    conn.commit()

    timings = storyboard_ops.episode_detail("e1", view="board")["shot_timings"]
    assert timings["1"]["elapsed_ms"] == 30_000, "不应把上一次 run 的耗时算进来"
    assert timings["1"]["iterations"] == 1


def test_batch_timings_prefer_the_task_level_start_over_the_latest_run(monkeypatch) -> None:
    """定妆照/场景图续跑会新建 run，起点必须取批次列而非最近一次 run。

    与剧本台同一类缺陷：只看最近一次 run，跑了很久的批次在续跑后会显示成几分钟。
    """
    conn = _conn()
    _seed_episode(conn)
    monkeypatch.setattr(common, "get_conn", lambda: conn)
    monkeypatch.setattr(projects, "get_conn", lambda: conn)

    batch_started = 1_000.0        # 批次真实起点
    resumed_run_started = 3_400.0  # 续跑后新建的 run
    _seed_run(conn, run_id="r_refs", workflow_type="character_references",
              scope_type="project", scope_id="p1", status="RUNNING",
              started_at=resumed_run_started)
    _seed_run(conn, run_id="r_scene", workflow_type="scene_references",
              scope_type="project", scope_id="p1", status="RUNNING",
              started_at=resumed_run_started)
    conn.execute(
        "UPDATE projects SET refs_batch_started_at=?, scene_refs_batch_started_at=? "
        "WHERE id='p1'",
        (batch_started, batch_started),
    )
    conn.commit()

    timings = projects.project_detail("p1", view="bible")["task_timings"]
    assert timings["refs"]["started_at"] == batch_started
    assert timings["refs"]["started_at"] != resumed_run_started

    scenes = projects.project_detail("p1", view="scenes")["task_timings"]
    assert scenes["scene_refs"]["started_at"] == batch_started


def test_batch_timings_fall_back_to_run_when_batch_column_is_empty(monkeypatch) -> None:
    """批次列为空（字段启用前跑过的旧批次）时回退到 run，不能整个计时消失。"""
    conn = _conn()
    _seed_episode(conn)
    monkeypatch.setattr(common, "get_conn", lambda: conn)
    monkeypatch.setattr(projects, "get_conn", lambda: conn)

    _seed_run(conn, run_id="r_refs", workflow_type="character_references",
              scope_type="project", scope_id="p1", status="SUCCEEDED",
              started_at=500.0, finished_at=560.0)
    conn.commit()

    timings = projects.project_detail("p1", view="bible")["task_timings"]
    assert timings["refs"] == {"started_at": 500.0, "finished_at": 560.0}
