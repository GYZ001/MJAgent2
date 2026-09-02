"""映射台预检的图片费用预估（app/domain/screenplay_ops/cast_impact_estimate.py）：
已知会出图的部分（已登记角色/场景本集原文命中但缺参考图）必须算准，本集真正
新增的角色/场景数量在生成前结构上无法确知，必须如实标为 None，不得编造精确数字
（CLAUDE.md「不得兜底填充」）。背景与判据见该模块的模块 docstring；今晚已经因为
「无人物谱时按 20 角色报 12 元」的假报价吃过亏（e531f37），本文件专门盯这类回归。
"""
from __future__ import annotations

import json

import pytest

from app import db
from app.domain.screenplay_ops.cast_impact_estimate import (
    _prep_pack_known_pending_images,
    _prep_pack_pending_characters,
    _prep_pack_pending_scenes,
)
from app.domain.screenplay_ops.status_snapshot import _screenplay_cast_impact
from app.multiview import CHARACTER_REQUIRED_VIEWS, SCENE_REQUIRED_VIEWS
from app.schemas import Bible


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "cast-impact.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES('p1','demo','created',?)",
        (db.now(),),
    )
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, title, screenplay_status, status, created_at) "
        "VALUES('e1','p1',3,'第三集','pending','planned',?)",
        (db.now(),),
    )
    conn.commit()
    yield conn


def _bible(characters=None, scenes=None) -> Bible:
    return Bible.model_validate({
        "characters": characters or [],
        "scenes": scenes or [],
        "world": {"era": "", "genre": "", "visual_style_canonical": "写实摄影风"},
    })


def _character(name: str, aliases: list[str] | None = None) -> dict:
    return {
        "name": name,
        "role": "重要配角",
        "appearance_canonical": "年轻男子，黑发",
        "aliases": [
            {
                "text": a, "name_kind": "referential",
                "evidence_chapter_index": 1, "evidence_quote": a,
            }
            for a in (aliases or [])
        ],
    }


def _scene(name: str, aliases: list[str] | None = None) -> dict:
    return {"name": name, "scene_canonical": "宗门广场，白天", "aliases": aliases or []}


def _insert_portrait(conn, name: str, *, ep_start: int = 1, ep_end=None):
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (f"cp-{name}", "p1", name, ep_start, ep_end, db.now()),
    )
    conn.commit()


def _insert_scene_ref(conn, name: str, *, ep_start: int = 1, ep_end=None):
    conn.execute(
        "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (f"sr-{name}", "p1", name, ep_start, ep_end, db.now()),
    )
    conn.commit()


# ---- ① 已建卡缺图的角色/场景必须被算进已知部分 ----

def test_pending_character_missing_portrait_is_counted(_db):
    conn = _db
    bible = _bible(characters=[_character("李富贵", aliases=["小胖子"])])
    source_text = "小胖子跑进院子，喊了一声师兄。"
    pending = _prep_pack_pending_characters(
        conn, project_id="p1", episode_no=3, source_text=source_text, bible=bible,
    )
    assert pending == ["李富贵"]


def test_pending_scene_missing_reference_is_counted(_db):
    conn = _db
    bible = _bible(scenes=[_scene("宗门广场", aliases=["聚义广场"])])
    source_text = "众人聚在聚义广场前，等待宗主训话。"
    pending = _prep_pack_pending_scenes(
        conn, project_id="p1", episode_no=3, source_text=source_text, bible=bible,
    )
    assert pending == ["宗门广场"]


def test_known_pending_images_computes_credible_known_count(_db):
    conn = _db
    bible = _bible(
        characters=[_character("李富贵", aliases=["小胖子"])],
        scenes=[_scene("宗门广场")],
    )
    source_text = "小胖子站在宗门广场上，四处张望。"
    result = _prep_pack_known_pending_images(
        conn, project_id="p1", episode_no=3, source_text=source_text, bible=bible,
    )
    expected_images = len(CHARACTER_REQUIRED_VIEWS) + len(SCENE_REQUIRED_VIEWS)
    assert result["known_pending_characters"] == ["李富贵"]
    assert result["known_pending_scenes"] == ["宗门广场"]
    assert result["known_image_count"] == expected_images
    assert result["deferred"] is False


# ---- 已有参考图的角色/场景必须走复用，不重复出图 ----

def test_character_with_existing_portrait_is_not_double_charged(_db):
    conn = _db
    _insert_portrait(conn, "李富贵", ep_start=1, ep_end=None)
    bible = _bible(characters=[_character("李富贵", aliases=["小胖子"])])
    source_text = "小胖子今天没有说话。"
    pending = _prep_pack_pending_characters(
        conn, project_id="p1", episode_no=3, source_text=source_text, bible=bible,
    )
    assert pending == []


def test_scene_with_existing_reference_is_not_double_charged(_db):
    conn = _db
    _insert_scene_ref(conn, "宗门广场", ep_start=1, ep_end=None)
    bible = _bible(scenes=[_scene("宗门广场")])
    source_text = "众人再次聚在宗门广场。"
    pending = _prep_pack_pending_scenes(
        conn, project_id="p1", episode_no=3, source_text=source_text, bible=bible,
    )
    assert pending == []


# ---- 未在本集原文出现的角色/场景不计入（即便缺图） ----

def test_character_not_mentioned_this_episode_is_not_counted(_db):
    conn = _db
    bible = _bible(characters=[_character("王大锤")])
    source_text = "今天风和日丽，什么都没发生。"
    pending = _prep_pack_pending_characters(
        conn, project_id="p1", episode_no=3, source_text=source_text, bible=bible,
    )
    assert pending == []


# ---- ② 新发现部分必须如实标为不可预知，不得编造精确数字 ----

def test_new_character_beyond_bible_keeps_estimate_honestly_unknown(_db):
    conn = _db
    # 人物谱为空：本集原文提到的"陈锋"是人物谱之外的角色，生成前无法确知是否
    # 会被判定为新角色——estimated_* 必须保持 None，不能假装能预测。
    bible = _bible()
    source_text = "陈锋推门而入，环顾四周。"
    result = _prep_pack_known_pending_images(
        conn, project_id="p1", episode_no=3, source_text=source_text, bible=bible,
    )
    assert result["estimated_images"] is None
    assert "无法确知" in result["note"]


def test_screenplay_cast_impact_never_fakes_a_precise_total(_db):
    conn = _db
    conn.execute(
        "UPDATE projects SET bible_json=? WHERE id='p1'",
        (json.dumps({
            "characters": [], "scenes": [],
            "world": {"era": "", "genre": "", "visual_style_canonical": "写实摄影风"},
        }),),
    )
    conn.commit()
    ep = dict(conn.execute("SELECT * FROM episodes WHERE id='e1'").fetchone())
    impact = _screenplay_cast_impact(conn, ep, "全新的一集，出现了从未记录过的角色。")
    stage = impact["portrait_asset_stage"]
    assert stage["estimated_images"] is None


# ---- ④ 零出图的情形必须报 0，不是报一个默认值 ----

def test_zero_pending_assets_report_zero_not_a_default(_db):
    conn = _db
    bible = _bible()  # 空人物谱、空场景库
    result = _prep_pack_known_pending_images(
        conn, project_id="p1", episode_no=3, source_text="什么都没有发生。", bible=bible,
    )
    assert result["known_pending_characters"] == []
    assert result["known_pending_scenes"] == []
    assert result["known_image_count"] == 0


def test_zero_pending_when_everything_already_has_reference_images(_db):
    conn = _db
    _insert_portrait(conn, "李富贵")
    _insert_scene_ref(conn, "宗门广场")
    bible = _bible(
        characters=[_character("李富贵")],
        scenes=[_scene("宗门广场")],
    )
    source_text = "李富贵站在宗门广场上。"
    result = _prep_pack_known_pending_images(
        conn, project_id="p1", episode_no=3, source_text=source_text, bible=bible,
    )
    assert result["known_image_count"] == 0
