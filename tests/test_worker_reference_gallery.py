import json
import sqlite3

from app import compiler, db, worker
from app.schemas import Bible, Character, World


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for stmt in db.MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    return conn


def _seed_project(conn: sqlite3.Connection) -> None:
    bible = Bible(
        characters=[Character(name="A", role="lead", appearance_canonical="black hair")],
        world=World(visual_style_canonical="anime drama style"),
    )
    conn.execute(
        "INSERT INTO projects(id, name, status, bible_json, created_at) VALUES(?,?,?,?,?)",
        ("p1", "P", "created", bible.model_dump_json(), 1.0),
    )
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, status, created_at) VALUES(?,?,?,?,?)",
        ("e1", "p1", 1, "confirmed", 1.0),
    )
    conn.execute(
        """INSERT INTO shots(
               id, episode_id, shot_no, duration_s, shot_size, camera_move, scene_setting,
               characters, action_desc, source_excerpt, narration, dialogues, transition,
               continuity_from_prev, first_frame_desc, last_frame_desc, scene_status
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "s1", "e1", 1, 10, "中景", "固定", "室内",
            json.dumps(["A"]), "A坐在桌前说话。", "A坐在桌前说话。",
            None, json.dumps([]), "硬切", 0, "A坐下。", "A抬头。", "approved",
        ),
    )
    conn.commit()


def test_edited_reference_gallery_changes_enqueue_idempotency(monkeypatch) -> None:
    conn = _conn()
    _seed_project(conn)
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(compiler, "compile_prompt", lambda *a, **k: "PROMPT --dur 10")

    first = worker.enqueue_shot("s1")
    assert first["reused"] is False

    refs = [
        {
            "id": "r_keep",
            "url": "data:image/jpeg;base64,keep",
            "type": "plot_key_frame",
            "source": "seedream_generated",
            "selectedForSeedance": True,
        },
        {
            "id": "r_gone",
            "url": "data:image/jpeg;base64,gone",
            "type": "plot_key_frame",
            "source": "seedream_generated",
            "selectedForSeedance": False,
            "deleted": True,
        },
    ]
    meta = {"mode": "REFERENCE_IMAGE_MODE", "reference_images": refs}
    conn.execute(
        "UPDATE shot_versions SET status='succeeded', image_inputs=? WHERE id=?",
        (json.dumps(meta, ensure_ascii=False), first["version_id"]),
    )
    conn.execute("UPDATE shots SET adopted_version_id=? WHERE id='s1'", (first["version_id"],))
    conn.commit()

    unchanged = worker.enqueue_shot("s1")
    assert unchanged == {"reused": True, "version_id": first["version_id"]}

    refs[1]["selectedForSeedance"] = True
    refs[1]["deleted"] = False
    edited_meta = {
        "mode": "REFERENCE_IMAGE_MODE",
        "reference_images": refs,
        "reference_gallery_revision": 123.0,
        "reference_gallery_edited": True,
    }
    conn.execute(
        "UPDATE shot_versions SET image_inputs=? WHERE id=?",
        (json.dumps(edited_meta, ensure_ascii=False), first["version_id"]),
    )
    conn.commit()

    changed = worker.enqueue_shot("s1")
    assert changed["reused"] is False
    assert changed["version_id"] != first["version_id"]

    new_meta = json.loads(conn.execute(
        "SELECT image_inputs FROM shot_versions WHERE id=?", (changed["version_id"],)
    ).fetchone()["image_inputs"])
    assert new_meta["reference_gallery_source_version_id"] == first["version_id"]
    assert [r["id"] for r in new_meta["reference_images"]] == ["r_keep", "r_gone"]
