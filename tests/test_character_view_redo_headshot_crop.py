"""app.multiview.regenerate_character_view 的 face_closeup 分支（单视角重做，
2026-10-02）：改走 app.portraits.headshot_crop 纯像素裁切，不再调用生图模型；
缺 front_full 时给出清楚的中文报错，不静默兜底成"不带种子图硬生成"。
"""
from __future__ import annotations

import threading

import pytest

from app import config, db, hiagent, multiview


@pytest.fixture
def real_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "redo-headshot-crop.db")
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_local", threading.local())
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    db.init_db()
    yield db.get_conn(), tmp_path
    db.get_conn().close()


def _seed_portrait_with_front(conn, tmp_path, *, project_id: str, portrait_id: str) -> str:
    front_path = tmp_path / "front.jpg"
    front_path.write_bytes(b"front-bytes")
    conn.execute(
        "INSERT INTO projects(id, name, status, bible_json, bible_version, created_at) "
        "VALUES(?,?,?,?,1,1)", (project_id, "P", "bible_ready", "{}"),
    )
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, "
        "appearance, image_path, created_at) VALUES(?,?,?,1,NULL,?,?,1)",
        (portrait_id, project_id, "温念", "原有外观锚点", str(front_path)),
    )
    conn.execute(
        "INSERT INTO character_portrait_views(id,portrait_id,view_role,framing,image_path,"
        "prompt,qa_json,status,selected,created_at) VALUES(?,?,'front_full','full_body',?,"
        "'seed prompt',NULL,'ready',1,1)",
        (f"{portrait_id}_front", portrait_id, str(front_path)),
    )
    conn.commit()
    return str(front_path)


async def test_regenerate_face_closeup_crops_instead_of_calling_generate_image(
    real_db, monkeypatch,
) -> None:
    conn, tmp_path = real_db
    front_path = _seed_portrait_with_front(conn, tmp_path, project_id="proj1", portrait_id="portrait1")

    async def forbidden_generate_image(*_args, **_kwargs):
        raise AssertionError("face_closeup 重做不得调用生图模型")

    async def fake_crop(source_path, *, dest_path, call_meta):
        from pathlib import Path
        assert source_path == front_path
        assert call_meta["character_name"] == "温念"
        Path(dest_path).write_bytes(b"cropped-bytes")
        return {
            "provenance_preserved": True, "head_box": [0.3, 0.1, 0.7, 0.4],
            "clothing_top_y": 0.42, "crop_box_px": [0, 0, 100, 120],
            "output_size": [768, 921], "source_path": source_path,
        }

    monkeypatch.setattr(multiview, "_generate_image", forbidden_generate_image)
    monkeypatch.setattr(multiview, "crop_headshot_from_portrait", fake_crop)

    result = await multiview.regenerate_character_view(
        project_id="proj1", portrait_id="portrait1", view_role="face_closeup",
    )

    assert result["status"] == "ready"
    row = conn.execute(
        "SELECT image_path, status, qa_json FROM character_portrait_views "
        "WHERE portrait_id='portrait1' AND view_role='face_closeup'",
    ).fetchone()
    assert row["status"] == "ready"
    import json
    qa = json.loads(row["qa_json"])
    assert qa["provenance_preserved"] is True
    from pathlib import Path
    assert Path(row["image_path"]).read_bytes() == b"cropped-bytes"


async def test_regenerate_face_closeup_without_front_full_raises_clear_error(
    real_db, monkeypatch,
) -> None:
    conn, tmp_path = real_db
    conn.execute(
        "INSERT INTO projects(id, name, status, bible_json, bible_version, created_at) "
        "VALUES('proj2','P','bible_ready','{}',1,1)",
    )
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, "
        "appearance, image_path, created_at) VALUES('portrait2','proj2','温念',1,NULL,'外观锚点',NULL,1)",
    )
    conn.commit()

    async def forbidden_generate_image(*_args, **_kwargs):
        raise AssertionError("不应该走到生图模型调用")

    async def forbidden_crop(*_args, **_kwargs):
        raise AssertionError("没有 front_full 时不应该尝试裁切")

    monkeypatch.setattr(multiview, "_generate_image", forbidden_generate_image)
    monkeypatch.setattr(multiview, "crop_headshot_from_portrait", forbidden_crop)

    with pytest.raises(hiagent.ProviderError, match="全身定妆照"):
        await multiview.regenerate_character_view(
            project_id="proj2", portrait_id="portrait2", view_role="face_closeup",
        )
