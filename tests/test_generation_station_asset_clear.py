import json
import sqlite3

from app import artifacts, db


def _database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for statement in db.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,status,created_at) "
        "VALUES('e','p',1,'done',0)"
    )
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s,adopted_version_id) "
        "VALUES('s','e',1,5,'v1')"
    )
    conn.commit()
    return conn


def test_video_only_clear_preserves_reference_gallery(tmp_path, monkeypatch) -> None:
    conn = _database()
    root = tmp_path / "projects"
    shot_dir = root / "p" / "episodes" / "1" / "shots" / "1"
    refs = shot_dir / "references"
    refs.mkdir(parents=True)
    ref_path = refs / "keyframe.png"
    ref_path.write_bytes(b"ref")
    videos = []
    for no in (1, 2):
        path = shot_dir / f"v{no}.mp4"
        path.write_bytes(b"video")
        videos.append(path)
        conn.execute(
            """INSERT INTO shot_versions(
                   id,shot_id,version_no,prompt_text,idem_key,status,video_path,image_inputs,created_at
               ) VALUES(?,?,?,?,?,'succeeded',?,?,?)""",
            (
                f"v{no}", "s", no, "prompt", f"key-{no}", str(path),
                json.dumps({"reference_images": [{"id": "ref", "path": str(ref_path)}]}), no,
            ),
        )
    conn.execute(
        "INSERT INTO jobs(id,kind,shot_id,version_id,episode_id,project_id,status,created_at,updated_at) "
        "VALUES('j','video','s','v2','e','p','succeeded',0,0)"
    )
    conn.commit()
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts.config, "PROJECTS_DIR", root)

    result = artifacts.clear_shot_video_assets("s")

    assert result["videos"] == 2
    assert ref_path.exists()
    assert not any(path.exists() for path in videos)
    rows = conn.execute(
        "SELECT status,video_path,image_inputs FROM shot_versions WHERE shot_id='s'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "references_ready"
    assert rows[0]["video_path"] is None
    assert json.loads(rows[0]["image_inputs"])["reference_images"][0]["id"] == "ref"
    assert conn.execute("SELECT adopted_version_id FROM shots WHERE id='s'").fetchone()[0] is None


def test_reference_only_clear_preserves_existing_video(tmp_path, monkeypatch) -> None:
    conn = _database()
    root = tmp_path / "projects"
    shot_dir = root / "p" / "episodes" / "1" / "shots" / "1"
    refs = shot_dir / "references"
    refs.mkdir(parents=True)
    ref_path = refs / "keyframe.png"
    ref_path.write_bytes(b"ref")
    scene_path = shot_dir / "scene.png"
    scene_path.write_bytes(b"scene")
    video_path = shot_dir / "v1.mp4"
    video_path.write_bytes(b"video")
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,video_path,image_inputs,created_at
           ) VALUES('v1','s',1,'prompt','key','succeeded',?,?,0)""",
        (str(video_path), json.dumps({"reference_images": [{"id": "ref", "path": str(ref_path)}]})),
    )
    conn.execute(
        """INSERT INTO shot_scenes(id,shot_id,version_no,kind,prompt_text,image_path,status,created_at)
           VALUES('scene','s',1,'head','prompt',?,'succeeded',0)""",
        (str(scene_path),),
    )
    conn.commit()
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts.config, "PROJECTS_DIR", root)

    result = artifacts.clear_shot_reference_assets("s")

    assert result["videos_preserved"] is True
    assert video_path.exists()
    assert not ref_path.exists()
    assert not scene_path.exists()
    row = conn.execute(
        "SELECT status,video_path,image_inputs FROM shot_versions WHERE id='v1'"
    ).fetchone()
    assert row["status"] == "succeeded"
    assert row["video_path"] == str(video_path)
    assert "reference_images" not in json.loads(row["image_inputs"])


def test_resource_clear_removes_video_images_and_reference_indexes(tmp_path, monkeypatch) -> None:
    conn = _database()
    root = tmp_path / "projects"
    shot_dir = root / "p" / "episodes" / "1" / "shots" / "1"
    refs = shot_dir / "references"
    refs.mkdir(parents=True)
    ref_path = refs / "keyframe.png"
    scene_path = shot_dir / "scene.png"
    boundary_path = shot_dir / "boundaries" / "first.jpg"
    boundary_path.parent.mkdir(parents=True)
    video_path = shot_dir / "v1.mp4"
    ref_path.write_bytes(b"ref")
    scene_path.write_bytes(b"scene")
    boundary_path.write_bytes(b"boundary")
    video_path.write_bytes(b"video")
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,video_path,image_inputs,created_at
           ) VALUES('v1','s',1,'prompt','key','succeeded',?,?,0)""",
        (str(video_path), json.dumps({"reference_images": [{"id": "ref", "path": str(ref_path)}]})),
    )
    conn.execute(
        """INSERT INTO shot_scenes(id,shot_id,version_no,kind,prompt_text,image_path,status,created_at)
           VALUES('scene','s',1,'head','prompt',?,'succeeded',0)""",
        (str(scene_path),),
    )
    conn.execute(
        """INSERT INTO reference_sets(
               id,shot_id,source_version_id,fingerprint,created_at,updated_at
           ) VALUES('set','s','v1','fp',0,0)"""
    )
    conn.execute(
        """INSERT INTO reference_assets(
               id,reference_set_id,asset_type,path,created_at
           ) VALUES('asset','set','plot_key_frame',?,0)""",
        (str(ref_path),),
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,created_at,updated_at
           ) VALUES('j','video','s','v1','e','p','succeeded',0,0)"""
    )
    conn.execute(
        """INSERT INTO video_boundary_assets(
               id,episode_video_plan_id,shot_plan_id,shot_id,role,source,path,
               qa_status,qa_json,fingerprint,created_at
           ) VALUES(
               'boundary','historical-plan','historical-shot-plan','s',
               'first_frame','STATIC_BOUNDARY_ASSET',?,'passed','{}','fp',0
           )""",
        (str(boundary_path),),
    )
    conn.commit()
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts.config, "PROJECTS_DIR", root)

    result = artifacts.clear_shot_artifacts("s")

    assert result["videos"] == 1
    assert result["references"] == 1
    assert not video_path.exists()
    assert not ref_path.exists()
    assert not scene_path.exists()
    assert not boundary_path.exists()
    for table in (
        "shot_versions", "shot_scenes", "reference_sets", "reference_assets",
        "video_boundary_assets", "jobs",
    ):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    shot = conn.execute(
        "SELECT adopted_version_id,approved_scene_id,mode_plan FROM shots WHERE id='s'"
    ).fetchone()
    assert tuple(shot) == (None, None, None)


def test_episode_resource_clear_supersedes_active_video_plan(
    tmp_path,
    monkeypatch,
) -> None:
    conn = _database()
    conn.execute(
        """INSERT INTO provider_video_capability_snapshots(
               id,provider,model,capabilities_json,probe_time,probe_result,
               technical_success,created_at
           ) VALUES('cap','provider','model','{}',0,'succeeded',1,0)"""
    )
    conn.execute(
        """INSERT INTO episode_video_generation_plans(
               id,episode_id,plan_revision,source_storyboard_revision_id,
               capability_snapshot_id,status,created_at
           ) VALUES('plan','e',1,'storyboard','cap','valid',0)"""
    )
    conn.execute(
        """INSERT INTO shot_video_generation_plans(
               id,episode_video_plan_id,shot_id,shot_no,planned_mode,
               capability_snapshot_id,status,created_at,updated_at
           ) VALUES(
               'shot-plan','plan','s',1,'FIRST_LAST_FRAME_MODE',
               'cap','planned',0,0
           )"""
    )
    conn.execute(
        "UPDATE shots SET mode_plan=? WHERE id='s'",
        (json.dumps({"mode": "FIRST_LAST_FRAME_MODE"}),),
    )
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,video_path,created_at
           ) VALUES('version-audit','s',1,'prompt','idem','failed',NULL,0)"""
    )
    conn.execute(
        """INSERT INTO video_generation_attempts(
               id,shot_plan_id,version_id,attempt_no,planned_mode,actual_mode,
               status,created_at,updated_at
           ) VALUES(
               'attempt-audit','shot-plan','version-audit',1,
               'FIRST_LAST_FRAME_MODE','FIRST_LAST_FRAME_MODE','failed',0,0
           )"""
    )
    conn.execute(
        """INSERT INTO video_mode_qa_results(
               id,shot_plan_id,version_id,planned_mode,actual_mode,
               technical_success,result_json,created_at
           ) VALUES(
               'qa-audit','shot-plan','version-audit',
               'FIRST_LAST_FRAME_MODE','FIRST_LAST_FRAME_MODE',0,'{}',0
           )"""
    )
    conn.commit()
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(
        artifacts.config,
        "PROJECTS_DIR",
        tmp_path / "projects",
    )

    artifacts.clear_episode_artifacts("e")

    assert conn.execute(
        "SELECT status FROM episode_video_generation_plans WHERE id='plan'"
    ).fetchone()[0] == "superseded"
    assert conn.execute(
        "SELECT status FROM shot_video_generation_plans WHERE id='shot-plan'"
    ).fetchone()[0] == "stale"
    assert conn.execute(
        "SELECT mode_plan FROM shots WHERE id='s'"
    ).fetchone()[0] is None
    assert conn.execute(
        "SELECT status FROM shot_versions WHERE id='version-audit'"
    ).fetchone()[0] == "cleared"
    from app.api import _public_shot_versions

    assert _public_shot_versions(conn, "s", include_inputs=True) == []
    assert conn.execute(
        "SELECT COUNT(*) FROM video_generation_attempts WHERE id='attempt-audit'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM video_mode_qa_results WHERE id='qa-audit'"
    ).fetchone()[0] == 1
