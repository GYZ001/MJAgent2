"""人物谱/场景库「完全手动新增/替换」（app/domain/bible_ops/manual_character.py、
manual_scene.py、manual_upload.py）。

覆盖任务要求的六件事：
① 手动新增命中已有角色时不新建、报出归属者；
② 歧义时 fail closed；
③ 描述长度越界时报出差多少字；
④ 替换后旧图进负数归档槽位、可回滚；
⑤ 原子性——落盘失败时旧图完好；
⑥ 上传非图片/超大文件被拒。
"""
from __future__ import annotations

import asyncio
import io
import json

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

from app import db
from app.domain.bible_ops import manual_character, manual_scene, manual_upload
from app.domain.bible_ops.manual_character import add_manual_character, replace_character_portrait_image
from app.domain.bible_ops.manual_scene import add_manual_scene, replace_scene_image, rollback_manual_scene_image
from app.domain.bible_ops.portrait_candidates import rollback_portrait_candidate

_JPEG = b"\xff\xd8\xff\xe0" + b"0" * 200
_JPEG2 = b"\xff\xd8\xff\xe1" + b"1" * 200  # 内容不同的第二张合法 JPEG，便于区分「新图/旧图」
_APPEARANCE = "高大英武剑眉星目" * 3  # 24 字，落在 20~80 字区间
_SCENE_CANONICAL = "清晨薄雾竹林小院" * 4  # 32 字，落在 30~80 字区间
_STYLE = "国风水墨写意插画风格，色彩淡雅意境悠远"


def _upload(raw: bytes, filename: str = "test.jpg") -> UploadFile:
    return UploadFile(io.BytesIO(raw), filename=filename)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "manual_assets.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    return db.get_conn()


def _seed_project(conn, project_id: str, bible: dict) -> None:
    conn.execute(
        "INSERT INTO projects(id, name, bible_json, bible_version, created_at) VALUES(?,?,?,?,?)",
        (project_id, "测试项目", json.dumps(bible, ensure_ascii=False), 1, db.now()),
    )
    conn.commit()


def _bible_row(conn, project_id: str) -> dict:
    row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    return json.loads(row["bible_json"])


# ---------------------------------------------------------------------------
# ① 手动新增命中已有角色：不新建，报出归属者
# ---------------------------------------------------------------------------

def test_add_manual_character_hits_existing_owner_reports_owner_no_new_card(conn) -> None:
    _seed_project(conn, "p1", {
        "characters": [{
            "name": "李富贵", "role": "配角", "appearance_canonical": _APPEARANCE,
            "aliases": [{
                "text": "小胖子", "name_kind": "honorific",
                "evidence_chapter_index": 1, "evidence_quote": "众人都唤他小胖子",
            }],
        }],
        "world": {"era": "现代", "genre": "都市", "visual_style_canonical": _STYLE},
        "scenes": [],
    })

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(add_manual_character(
            "p1", name="小胖子", appearance_canonical=_APPEARANCE,
            period_costume_canonical="常服", image=_upload(_JPEG),
        ))

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "CHARACTER_ALREADY_EXISTS"
    assert exc_info.value.detail["owner"] == "李富贵"
    assert len(_bible_row(conn, "p1")["characters"]) == 1  # 没有建出第二张卡


# ---------------------------------------------------------------------------
# ② 命中多个角色：fail closed
# ---------------------------------------------------------------------------

def test_add_manual_character_ambiguous_label_fails_closed(conn) -> None:
    alias = {
        "text": "大汉", "name_kind": "referential", "is_exclusive": False,
        "evidence_chapter_index": 1, "evidence_quote": "那大汉",
    }
    _seed_project(conn, "p1", {
        "characters": [
            {"name": "曹阳", "role": "配角", "appearance_canonical": _APPEARANCE, "aliases": [alias]},
            {"name": "虎爷", "role": "配角", "appearance_canonical": _APPEARANCE, "aliases": [alias]},
        ],
        "world": {"era": "现代", "genre": "都市", "visual_style_canonical": _STYLE},
        "scenes": [],
    })

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(add_manual_character(
            "p1", name="大汉", appearance_canonical=_APPEARANCE,
            period_costume_canonical="常服", image=_upload(_JPEG),
        ))

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "CHARACTER_NAME_AMBIGUOUS"
    assert sorted(exc_info.value.detail["owners"]) == ["曹阳", "虎爷"]
    assert len(_bible_row(conn, "p1")["characters"]) == 2  # 没有猜一个，也没有建第三张卡


# ---------------------------------------------------------------------------
# ③ 描述长度越界：精确报出差多少字
# ---------------------------------------------------------------------------

def test_add_manual_character_appearance_too_short_reports_exact_gap(conn) -> None:
    _seed_project(conn, "p1", {
        "characters": [], "scenes": [],
        "world": {"era": "现代", "genre": "都市", "visual_style_canonical": _STYLE},
    })

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(add_manual_character(
            "p1", name="新角色", appearance_canonical="矮个子",  # 3 字，远低于 20 字下限
            period_costume_canonical="常服", image=_upload(_JPEG),
        ))

    assert exc_info.value.status_code == 422
    assert "还需 17 字" in exc_info.value.detail  # 20 - 3 = 17
    assert len(_bible_row(conn, "p1")["characters"]) == 0


def test_add_manual_scene_canonical_too_long_reports_exact_excess(conn) -> None:
    _seed_project(conn, "p1", {
        "characters": [], "scenes": [],
        "world": {"era": "现代", "genre": "都市", "visual_style_canonical": _STYLE},
    })
    too_long = "场" * 81  # 超出 80 字上限 1 字

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(add_manual_scene(
            "p1", name="新场景", scene_canonical=too_long, image=_upload(_JPEG),
        ))

    assert exc_info.value.status_code == 422
    assert "超出上限 1 字" in exc_info.value.detail
    assert len(_bible_row(conn, "p1")["scenes"]) == 0


# ---------------------------------------------------------------------------
# ④ 替换后旧图进负数归档槽位，且可回滚（角色 + 场景两侧）
# ---------------------------------------------------------------------------

def test_replace_character_portrait_archives_old_and_rollback_restores_it(conn, tmp_path) -> None:
    _seed_project(conn, "p1", {
        "characters": [], "scenes": [],
        "world": {"era": "现代", "genre": "都市", "visual_style_canonical": _STYLE},
    })
    added = asyncio.run(add_manual_character(
        "p1", name="沈知微", appearance_canonical=_APPEARANCE,
        period_costume_canonical="常服", image=_upload(_JPEG),
    ))
    old_portrait_id = added["portrait_id"]
    old_row = conn.execute(
        "SELECT * FROM character_portraits WHERE id=?", (old_portrait_id,),
    ).fetchone()
    old_image_path = old_row["image_path"]
    assert old_row["ep_start"] == 1 and old_row["ep_end"] is None

    replaced = asyncio.run(replace_character_portrait_image(
        "p1", "沈知微", image=_upload(_JPEG2),
    ))
    new_portrait_id = replaced["portrait_id"]
    assert new_portrait_id != old_portrait_id
    assert replaced["style_warning"]
    assert replaced["downstream_notice"]

    archived = conn.execute("SELECT * FROM character_portraits WHERE id=?", (old_portrait_id,)).fetchone()
    assert archived["ep_start"] < 0  # 旧图压入负数历史槽位
    assert archived["ep_end"] == 0
    assert archived["image_path"] == old_image_path  # 旧图文件路径未被覆盖

    current = conn.execute(
        "SELECT * FROM character_portraits WHERE project_id='p1' AND character_name='沈知微' "
        "AND ep_end IS NULL ORDER BY ep_start DESC LIMIT 1",
    ).fetchone()
    assert current["id"] == new_portrait_id
    assert current["ep_start"] == 1

    bible = _bible_row(conn, "p1")
    character = next(c for c in bible["characters"] if c["name"] == "沈知微")
    assert character["ref_image_path"] == current["image_path"]

    # 既有 rollback 端点原样可用，不是另开的第二套。
    rolled_back = asyncio.run(rollback_portrait_candidate("p1", "沈知微", new_portrait_id, {}))
    assert rolled_back["rolled_back"] is True
    restored = conn.execute(
        "SELECT * FROM character_portraits WHERE project_id='p1' AND character_name='沈知微' "
        "AND ep_end IS NULL ORDER BY ep_start DESC LIMIT 1",
    ).fetchone()
    assert restored["id"] == old_portrait_id
    assert restored["image_path"] == old_image_path


def test_replace_scene_image_archives_old_and_manual_rollback_restores_it(conn) -> None:
    _seed_project(conn, "p1", {
        "characters": [], "scenes": [],
        "world": {"era": "现代", "genre": "都市", "visual_style_canonical": _STYLE},
    })
    added = asyncio.run(add_manual_scene(
        "p1", name="后山竹林", scene_canonical=_SCENE_CANONICAL, image=_upload(_JPEG),
    ))
    old_id = added["scene_reference_id"]
    old_image_path = conn.execute(
        "SELECT image_path FROM scene_references WHERE id=?", (old_id,),
    ).fetchone()["image_path"]

    replaced = asyncio.run(replace_scene_image("p1", "后山竹林", image=_upload(_JPEG2)))
    new_id_ = replaced["scene_reference_id"]
    assert new_id_ != old_id
    assert replaced["style_warning"]
    assert replaced["downstream_notice"]
    # rollback_url 必须指向被归档的旧版本，不是刚提升的新版本——否则前端拿它
    # 调用 manual-rollback 会把"当前"当成"要恢复的历史版本"传进去。
    assert replaced["previous_scene_reference_id"] == old_id
    assert replaced["rollback_url"].endswith(f"/refs/{old_id}/manual-rollback")

    archived = conn.execute("SELECT * FROM scene_references WHERE id=?", (old_id,)).fetchone()
    assert archived["ep_start"] < 0
    assert archived["ep_end"] == 0
    assert archived["image_path"] == old_image_path

    rolled_back = asyncio.run(rollback_manual_scene_image("p1", "后山竹林", old_id, {}))
    assert rolled_back["rolled_back"] is True
    current = conn.execute(
        "SELECT * FROM scene_references WHERE project_id='p1' AND scene_name='后山竹林' "
        "AND ep_end IS NULL ORDER BY ep_start DESC LIMIT 1",
    ).fetchone()
    assert current["image_path"] == old_image_path
    assert current["id"] != old_id  # 回滚产生新行，旧行继续留在归档槽位——回滚本身也可再被回滚
    assert current["ep_start"] == 1


# ---------------------------------------------------------------------------
# ⑤ 原子性：落盘/登记失败时旧图完好
# ---------------------------------------------------------------------------

def test_replace_character_portrait_keeps_old_image_when_write_fails(conn, monkeypatch) -> None:
    _seed_project(conn, "p1", {
        "characters": [], "scenes": [],
        "world": {"era": "现代", "genre": "都市", "visual_style_canonical": _STYLE},
    })
    added = asyncio.run(add_manual_character(
        "p1", name="沈知微", appearance_canonical=_APPEARANCE,
        period_costume_canonical="常服", image=_upload(_JPEG),
    ))
    old_portrait_id = added["portrait_id"]
    old_image_path = conn.execute(
        "SELECT image_path FROM character_portraits WHERE id=?", (old_portrait_id,),
    ).fetchone()["image_path"]
    old_bytes = open(old_image_path, "rb").read()

    def _boom(*args, **kwargs):
        raise OSError("模拟磁盘写入失败")

    monkeypatch.setattr(manual_character, "atomic_write_bytes", _boom)

    with pytest.raises(OSError):
        asyncio.run(replace_character_portrait_image("p1", "沈知微", image=_upload(_JPEG2)))

    # 旧图行原封不动：仍是当前版本，仍是 ep_start=1，没有半途产生的新行。
    rows = conn.execute(
        "SELECT * FROM character_portraits WHERE project_id='p1' AND character_name='沈知微'",
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == old_portrait_id
    assert rows[0]["ep_start"] == 1 and rows[0]["ep_end"] is None
    assert open(old_image_path, "rb").read() == old_bytes  # 旧图文件内容未被触碰

    bible = _bible_row(conn, "p1")
    character = next(c for c in bible["characters"] if c["name"] == "沈知微")
    assert character["ref_image_path"] == old_image_path


def test_add_manual_scene_does_not_create_half_written_scene_when_write_fails(conn, monkeypatch) -> None:
    _seed_project(conn, "p1", {
        "characters": [], "scenes": [],
        "world": {"era": "现代", "genre": "都市", "visual_style_canonical": _STYLE},
    })

    def _boom(*args, **kwargs):
        raise OSError("模拟磁盘写入失败")

    monkeypatch.setattr(manual_scene, "atomic_write_bytes", _boom)

    with pytest.raises(OSError):
        asyncio.run(add_manual_scene(
            "p1", name="后山竹林", scene_canonical=_SCENE_CANONICAL, image=_upload(_JPEG),
        ))

    assert _bible_row(conn, "p1")["scenes"] == []  # 写盘失败，场景没有半成品残留
    assert conn.execute(
        "SELECT COUNT(*) c FROM scene_references WHERE project_id='p1'",
    ).fetchone()["c"] == 0


# ---------------------------------------------------------------------------
# ⑥ 上传校验：非图片 / 超大文件被拒
# ---------------------------------------------------------------------------

def test_add_manual_character_rejects_non_image_upload(conn) -> None:
    _seed_project(conn, "p1", {
        "characters": [], "scenes": [],
        "world": {"era": "现代", "genre": "都市", "visual_style_canonical": _STYLE},
    })

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(add_manual_character(
            "p1", name="新角色", appearance_canonical=_APPEARANCE,
            period_costume_canonical="常服",
            image=_upload(b"this is not an image, just plain text bytes"),
        ))

    assert exc_info.value.status_code == 415
    assert len(_bible_row(conn, "p1")["characters"]) == 0


def test_add_manual_character_rejects_oversized_upload(conn, monkeypatch) -> None:
    monkeypatch.setattr(manual_upload, "MAX_MANUAL_IMAGE_BYTES", 10)  # 收紧阈值，测试无需真造 10MB
    _seed_project(conn, "p1", {
        "characters": [], "scenes": [],
        "world": {"era": "现代", "genre": "都市", "visual_style_canonical": _STYLE},
    })

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(add_manual_character(
            "p1", name="新角色", appearance_canonical=_APPEARANCE,
            period_costume_canonical="常服", image=_upload(_JPEG),  # 204 字节 > 10 字节上限
        ))

    assert exc_info.value.status_code == 413
    assert len(_bible_row(conn, "p1")["characters"]) == 0


def test_add_manual_character_rejects_empty_upload(conn) -> None:
    _seed_project(conn, "p1", {
        "characters": [], "scenes": [],
        "world": {"era": "现代", "genre": "都市", "visual_style_canonical": _STYLE},
    })

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(add_manual_character(
            "p1", name="新角色", appearance_canonical=_APPEARANCE,
            period_costume_canonical="常服", image=_upload(b""),
        ))

    assert exc_info.value.status_code == 422
