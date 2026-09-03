"""WS11：``app.video_modes.asset_lookup.character_reference_assets`` 按镜时间线
锚点选定妆照——按集选版的第二处调用点，此前仍走旧 ``portrait_for_episode``
（不感知锚点），未接上 WS4（``portrait_lookup_for_episode``）/WS9（时间线锚点）
已经给 ``app.refs.refs_as_image_inputs`` 接好的能力。

覆盖：
1. ``_shot_time_anchor``：无 shot/无 storyboard_pack_segment/只有不可查询锚点
   （era）时返回 None，不兜底猜测；age 锚点优先于 year（与
   ``app.validators.resource_forecast._best_time_anchor`` 同一判据）；返回值
   是可直接喂给 ``portrait_lookup_for_episode`` 的 ``anchor_key`` 形状
   （"age:8"），不是原文展示文本。
2. ``character_reference_assets`` 的单图回退分支改走
   ``portrait_lookup_for_episode``，把派生出的 time_anchor 原样传下去；不传
   ``shot``（默认 None）时行为与改动前一致（回归防线）；不传 project_id 时
   仍走 bible 内置 ref_image_path，不受这次改动影响。
"""
from __future__ import annotations

from app import db
from app.schemas import Bible, Shot
from app.video_modes.asset_lookup import _shot_time_anchor, character_reference_assets


def _isolated_db(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "asset-lookup-time-anchor.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()


def _bible(*, ref_image_path: str | None = None) -> Bible:
    return Bible.model_validate({
        "characters": [{
            "name": "里奥", "role": "主角", "appearance_canonical": "十六岁少年",
            "ref_image_path": ref_image_path,
        }],
        "scenes": [], "world": {"era": "", "genre": "", "visual_style_canonical": "国风"},
    })


def _shot_with_anchors(anchors: list[dict]) -> Shot:
    return Shot(
        shot_no=1, duration_s=15, shot_size="", camera_move="", action_desc="占位",
        storyboard_pack_segment={"timeline_anchors": anchors},
    )


def test_shot_time_anchor_none_without_shot():
    assert _shot_time_anchor(None) is None


def test_shot_time_anchor_none_without_storyboard_pack_segment():
    shot = Shot(shot_no=1, duration_s=15, shot_size="", camera_move="", action_desc="占位")
    assert _shot_time_anchor(shot) is None


def test_shot_time_anchor_none_when_only_era_anchor():
    shot = _shot_with_anchors([{"kind": "era", "value": "东汉末年"}])
    assert _shot_time_anchor(shot) is None


def test_shot_time_anchor_prefers_age_over_year():
    shot = _shot_with_anchors([
        {"kind": "year", "value": "2004年", "anchor_key": "year:2004"},
        {"kind": "age", "value": "八岁", "anchor_key": "age:8"},
    ])
    assert _shot_time_anchor(shot) == "age:8"


def test_character_reference_assets_forwards_time_anchor_to_portrait_lookup(monkeypatch, tmp_path):
    _isolated_db(tmp_path, monkeypatch)
    captured: dict = {}
    image_path = tmp_path / "anchor.jpg"
    image_path.write_bytes(b"x")

    def fake_lookup(project_id, name, episode_no, *, visual_entity_id=None, time_anchor=None, conn=None):
        captured["time_anchor"] = time_anchor
        return {"image_path": str(image_path), "appearance": None, "portrait_id": "anchor1", "look_mismatch": None}

    monkeypatch.setattr("app.portraits.portrait_lookup.portrait_lookup_for_episode", fake_lookup)
    shot = _shot_with_anchors([{"kind": "age", "value": "八岁", "anchor_key": "age:8"}])
    assets = character_reference_assets(
        _bible(), ["里奥"], limit=1, project_id="p1", episode_no=3, shot=shot,
    )
    assert captured["time_anchor"] == "age:8"
    assert len(assets) == 1
    assert assets[0].path == str(image_path)


def test_character_reference_assets_without_shot_calls_lookup_with_none(monkeypatch, tmp_path):
    """未传 shot（默认 None）时行为与改动前一致——time_anchor 传 None，
    portrait_lookup_for_episode 内部回退按集分段判据，回归防线。"""
    _isolated_db(tmp_path, monkeypatch)
    captured: dict = {}
    image_path = tmp_path / "seg.jpg"
    image_path.write_bytes(b"x")

    def fake_lookup(project_id, name, episode_no, *, visual_entity_id=None, time_anchor=None, conn=None):
        captured["time_anchor"] = time_anchor
        return {"image_path": str(image_path), "appearance": None, "portrait_id": "seg1", "look_mismatch": None}

    monkeypatch.setattr("app.portraits.portrait_lookup.portrait_lookup_for_episode", fake_lookup)
    assets = character_reference_assets(_bible(), ["里奥"], limit=1, project_id="p1", episode_no=3)
    assert captured["time_anchor"] is None
    assert len(assets) == 1


def test_character_reference_assets_without_project_id_falls_back_to_bible_ref_image_path(tmp_path):
    image_path = tmp_path / "bible.jpg"
    image_path.write_bytes(b"x")
    assets = character_reference_assets(_bible(ref_image_path=str(image_path)), ["里奥"], limit=1)
    assert len(assets) == 1
    assert assets[0].path == str(image_path)
