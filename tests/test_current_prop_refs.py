"""分镜台/映射台道具卡按道具名实时解析物件库定物图（2026-09-05）。"""

from __future__ import annotations

from app.domain.storyboard_ops import current_prop_refs as cpr


def test_attach_current_prop_references_resolves_by_label(monkeypatch):
    rows = {"葫芦": {"id": "prop_1", "image_path": "/x/prop_refs/葫芦.png"}, "纸条": {"id": "prop_2", "image_path": None}}
    calls: list[tuple] = []

    def fake_lookup(conn, project_id, name, episode_no):
        calls.append((project_id, name, episode_no))
        return rows.get(name)

    monkeypatch.setattr(cpr, "prop_reference_for_episode", fake_lookup)
    monkeypatch.setattr(cpr, "_media_url", lambda path: f"/media{path}" if path else None)
    monkeypatch.setattr(cpr, "get_conn", lambda: object())
    detail = {
        "project_id": "p1", "episode_no": 1,
        "prep_pack": {"asset_manifest": {"props": [{"label": "葫芦", "description": "d"}, {"label": "纸条"}]}},
        "shots": [{"storyboard_pack_segment": {"resources": {"props": [{"label": "葫芦"}, {"label": "藤条"}]}}}],
    }
    cpr.attach_current_prop_references(detail, None)
    manifest = detail["prep_pack"]["asset_manifest"]["props"]
    assert manifest[0]["current_prop_image_url"] == "/media/x/prop_refs/葫芦.png"
    assert manifest[0]["current_prop_reference_id"] == "prop_1"
    assert manifest[1]["current_prop_image_url"] is None and manifest[1]["current_prop_reference_id"] is None
    seg_props = detail["shots"][0]["storyboard_pack_segment"]["resources"]["props"]
    assert seg_props[0]["current_prop_image_url"] == "/media/x/prop_refs/葫芦.png"
    assert seg_props[1]["current_prop_image_url"] is None
    assert calls.count(("p1", "葫芦", 1)) == 1, "同名道具只解析一次"


def test_attach_current_prop_references_without_project_is_noop():
    detail = {"shots": [{"storyboard_pack_segment": {"resources": {"props": [{"label": "葫芦"}]}}}]}
    cpr.attach_current_prop_references(detail, None)
    assert "current_prop_image_url" not in detail["shots"][0]["storyboard_pack_segment"]["resources"]["props"][0]
