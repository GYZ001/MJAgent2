import json
import sqlite3

from app.scenes import _merge_generated_scene_refs, _restore_approved_scene_bible
from app.schemas import Scene


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT)")
    conn.execute(
        "CREATE TABLE artifacts(type TEXT, scope_type TEXT, scope_id TEXT, "
        "status TEXT, version INTEGER, content_json TEXT)"
    )
    return conn


def test_approved_scene_bible_restores_only_missing_scenes() -> None:
    conn = _conn()
    current = {
        "characters": [],
        "world": {"visual_style_canonical": "国漫"},
        "scenes": [{"name": "广场", "scene_canonical": "人工修改后的广场"}],
    }
    approved = {
        "scenes": [
            {"name": "广场", "scene_canonical": "旧广场"},
            {"name": "后山", "scene_canonical": "批准的后山"},
        ]
    }
    conn.execute("INSERT INTO projects VALUES('p', ?)", (json.dumps(current, ensure_ascii=False),))
    conn.execute(
        "INSERT INTO artifacts VALUES('scene_bible', 'project', 'p', 'approved', 1, ?)",
        (json.dumps(approved, ensure_ascii=False),),
    )

    assert _restore_approved_scene_bible(conn, "p", current) is True
    restored = json.loads(conn.execute("SELECT bible_json FROM projects WHERE id='p'").fetchone()[0])

    assert [item["name"] for item in restored["scenes"]] == ["广场", "后山"]
    assert restored["scenes"][0]["scene_canonical"] == "人工修改后的广场"


def test_scene_path_merge_preserves_concurrent_character_update() -> None:
    conn = _conn()
    latest = {
        "characters": [{"name": "丙老", "ref_image_path": "portrait.jpg"}],
        "scenes": [{"name": "后山", "scene_canonical": "后山", "ref_image_path": None}],
    }
    conn.execute("INSERT INTO projects VALUES('p', ?)", (json.dumps(latest, ensure_ascii=False),))
    generated = Scene(name="后山", scene_canonical="后山", ref_image_path="scene.jpg")

    _merge_generated_scene_refs(conn, "p", [generated])
    merged = json.loads(conn.execute("SELECT bible_json FROM projects WHERE id='p'").fetchone()[0])

    assert merged["scenes"][0]["ref_image_path"] == "scene.jpg"
    assert merged["characters"] == latest["characters"]
