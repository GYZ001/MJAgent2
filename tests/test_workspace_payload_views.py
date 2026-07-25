from __future__ import annotations

import json
import sqlite3

from app import db
from app.domain import common, projects, storyboard_ops
from app.media_pipeline.status import episode_pipeline_statuses


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
    assert all(not v["image_inputs"]["reference_images"] for v in wall["shots"][0]["versions"])
    assert not any("SELECT * FROM shot_versions" in sql for sql in statements)
    assert not any("json_extract" in sql.lower() for sql in statements)


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
