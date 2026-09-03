"""app.refs.refs_as_image_inputs 的 time_anchor 接线（WS9）。

覆盖：project_id 存在时改走 portrait_lookup_for_episode 并把 time_anchor
原样传下去；不传 time_anchor（默认 None）时行为与改动前一致（回归防线）；
project_id 为空时仍走 bible 内置 ref_image_path，不受这次改动影响。
"""
from __future__ import annotations

from app.refs import refs_as_image_inputs
from app.schemas import Bible


def _bible(*, ref_image_path: str | None = None) -> Bible:
    return Bible.model_validate({
        "characters": [{
            "name": "里奥", "role": "主角", "appearance_canonical": "十六岁少年",
            "ref_image_path": ref_image_path,
        }],
        "scenes": [], "world": {"era": "", "genre": "", "visual_style_canonical": "国风"},
    })


def test_time_anchor_is_forwarded_to_portrait_lookup(monkeypatch, tmp_path):
    captured: dict = {}
    image_path = tmp_path / "anchor.jpg"
    image_path.write_bytes(b"x")

    def fake_lookup(project_id, name, episode_no, *, visual_entity_id=None, time_anchor=None, conn=None):
        captured["project_id"] = project_id
        captured["name"] = name
        captured["episode_no"] = episode_no
        captured["time_anchor"] = time_anchor
        return {"image_path": str(image_path), "appearance": None, "portrait_id": "anchor1", "look_mismatch": None}

    monkeypatch.setattr("app.portraits.portrait_lookup.portrait_lookup_for_episode", fake_lookup)
    out = refs_as_image_inputs(
        _bible(), ["里奥"], 1, project_id="p1", episode_no=3, time_anchor="age:35",
    )
    assert captured == {"project_id": "p1", "name": "里奥", "episode_no": 3, "time_anchor": "age:35"}
    assert len(out) == 1
    assert out[0][1] == "reference_image"


def test_without_time_anchor_still_calls_lookup_with_none(monkeypatch, tmp_path):
    captured: dict = {}
    image_path = tmp_path / "seg.jpg"
    image_path.write_bytes(b"x")

    def fake_lookup(project_id, name, episode_no, *, visual_entity_id=None, time_anchor=None, conn=None):
        captured["time_anchor"] = time_anchor
        return {"image_path": str(image_path), "appearance": None, "portrait_id": "seg1", "look_mismatch": None}

    monkeypatch.setattr("app.portraits.portrait_lookup.portrait_lookup_for_episode", fake_lookup)
    refs_as_image_inputs(_bible(), ["里奥"], 1, project_id="p1", episode_no=3)
    assert captured["time_anchor"] is None


def test_without_project_id_falls_back_to_bible_ref_image_path(tmp_path):
    image_path = tmp_path / "bible.jpg"
    image_path.write_bytes(b"x")
    out = refs_as_image_inputs(_bible(ref_image_path=str(image_path)), ["里奥"], 1)
    assert len(out) == 1
    assert out[0][1] == "reference_image"
