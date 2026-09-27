"""反打视角装配端到端：分镜正文点名 → multiview 依赖解析 → 参考图锚点 → 提示词替换。

只有「正文点了名 @场景名·反打」且「该场景此刻有带通过证据、文件存在的反打图」
时才装反打图；存量无证据的反打图、没点名的段、开关关闭都保持改动前的单张
主视角。装不上的点名在提示词里降级成普通文字，不把无图 @ 发给供应商。
"""
from __future__ import annotations

import json

import pytest

from app import db
from app.multiview import _storyboard_pack_asset_dependencies, library_anchor_assets_from_manifest
from app.scene_reverse import evidence
from app.schemas import Bible, Scene, World
from app.video_modes.seedance_reference_notes import build_seedance_reference_prompt_notes

_PASSED = {"reverse_check": {"checked": True, "passed": True, "reason": "朝向相反", "error": None}}


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "scene-reverse-assembly.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()


def _seed(conn, tmp_path, *, reverse_qa) -> None:
    est = tmp_path / "est.png"
    est.write_bytes(b"est")
    rev = tmp_path / "rev.png"
    rev.write_bytes(b"rev")
    conn.execute(
        "INSERT INTO projects(id,name,bible_json,created_at) VALUES(?,?,?,?)",
        ("proj-1", "reverse fixture", "{}", db.now()),
    )
    conn.execute(
        "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end, image_path, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        ("scene_ref_a", "proj-1", "修表铺", 1, None, str(est), db.now()),
    )
    conn.execute(
        "INSERT INTO scene_reference_views(id, scene_reference_id, view_role, image_path, qa_json, status, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        ("view_rev", "scene_ref_a", "reverse_angle", str(rev),
         json.dumps(reverse_qa, ensure_ascii=False) if reverse_qa is not None else None, "ready", db.now()),
    )
    conn.commit()


def _manifest(conn, prompt_text: str):
    segment = {
        "segment_no": 1, "prompt_text": prompt_text,
        "resources": {"characters": [], "scenes": [{"scene_id": "scene:修表铺", "scene_reference_id": "scene_ref_a"}]},
    }
    bible = Bible(characters=[], world=World(visual_style_canonical="写实"),
                  scenes=[Scene(name="修表铺", scene_canonical="修表铺，傍晚")])
    return _storyboard_pack_asset_dependencies(
        project_id="proj-1", episode_no=1, shot_id="shot-x", segment=segment, conn=conn, bible=bible,
    )


def _scene_anchor_names(manifest) -> list[str]:
    return [a["entity_name"] for a in library_anchor_assets_from_manifest(manifest) if a["entity_type"] == "scene"]


def test_mentioned_and_verified_reverse_view_is_attached(tmp_path) -> None:
    conn = db.get_conn()
    _seed(conn, tmp_path, reverse_qa=_PASSED)
    manifest = _manifest(conn, "镜头1：推近 @修表铺 柜台。镜头2：固定 近景，背景是@修表铺·反打门口。")
    assert [v["view_role"] for v in manifest["scene"]["selected_views"]] == ["establishing", "reverse_angle"]
    assert _scene_anchor_names(manifest) == ["修表铺", "修表铺·反打"]


@pytest.mark.parametrize("reverse_qa", [None, {"reverse_check": {"checked": True, "passed": False}}])
def test_reverse_view_without_passing_evidence_is_never_attached(tmp_path, reverse_qa) -> None:
    conn = db.get_conn()
    _seed(conn, tmp_path, reverse_qa=reverse_qa)
    manifest = _manifest(conn, "镜头2：背景是@修表铺·反打门口。")
    assert [v["view_role"] for v in manifest["scene"]["selected_views"]] == ["establishing"]
    assert _scene_anchor_names(manifest) == ["修表铺"]


def test_unmentioned_segment_keeps_single_establishing_view(tmp_path) -> None:
    conn = db.get_conn()
    _seed(conn, tmp_path, reverse_qa=_PASSED)
    manifest = _manifest(conn, "镜头1：推近 修表铺柜台。")
    assert [v["view_role"] for v in manifest["scene"]["selected_views"]] == ["establishing"]


def test_switch_off_keeps_single_establishing_view(tmp_path, monkeypatch) -> None:
    conn = db.get_conn()
    _seed(conn, tmp_path, reverse_qa=_PASSED)
    monkeypatch.setattr(evidence, "get_setting", lambda key: "false")
    manifest = _manifest(conn, "镜头2：背景是@修表铺·反打门口。")
    assert [v["view_role"] for v in manifest["scene"]["selected_views"]] == ["establishing"]


def test_prompt_notes_replace_attached_mention_and_demote_unattached_one() -> None:
    refs = [
        {"type": "scene", "view_role": "establishing", "entity_name": "修表铺"},
        {"type": "scene", "view_role": "reverse_angle", "entity_name": "修表铺·反打"},
    ]
    text = "镜头2：背景是@修表铺·反打门口。镜头3：@后院·反打的水缸。"
    out = build_seedance_reference_prompt_notes(text, refs, aspect_ratio="9:16")
    body = out.split("\n")[0]
    assert "@修表铺·反打" not in body and "@图片2门口" in body
    assert "@后院·反打" not in out and "后院反打方向的水缸" in body


def test_prompt_without_any_reference_still_demotes_reverse_mentions() -> None:
    out = build_seedance_reference_prompt_notes("镜头2：背景是@修表铺·反打门口。", [], aspect_ratio="9:16")
    assert out == "镜头2：背景是修表铺反打方向门口。"


@pytest.mark.parametrize("reverse_qa,expected", [(_PASSED, True), (None, False)])
def test_storyboard_sees_reverse_availability_when_manifest_lacks_scene_reference_id(tmp_path, reverse_qa, expected) -> None:
    """生产映射包的 scene_reference_id 通常为空（场景图映射后才异步登记）；可用判定必须
    按场景名 + 集号解析，与装配期同一规则——否则 reverse_angle_available 恒为 false。"""
    from app.production.storyboard_pack import _enrich_asset_manifest_canonical_visuals

    conn = db.get_conn()
    _seed(conn, tmp_path, reverse_qa=reverse_qa)
    payload = {"episode_no": 1, "asset_manifest": {"scenes": [
        {"scene_id": "scene:修表铺", "display_name": "修表铺", "scene_reference_id": None},
    ]}}
    _enrich_asset_manifest_canonical_visuals(conn, payload, bible=None, project_id="proj-1")
    assert payload["asset_manifest"]["scenes"][0]["reverse_angle_available"] is expected


def test_manifest_scene_annotation_carries_reverse_view_description(tmp_path) -> None:
    """只给布尔标记时模型不知道反打画面有什么；可用时要把起草的另一侧描述一并给分镜模型。"""
    from app.scene_reverse.segment_views import annotate_manifest_scene

    conn = db.get_conn()
    qa = {**_PASSED, "draft": "门口朝里看：挂钟墙与玻璃柜台"}
    _seed(conn, tmp_path, reverse_qa=qa)
    scene = {"scene_id": "scene:修表铺", "display_name": "修表铺", "scene_reference_id": None}
    annotate_manifest_scene(conn, scene, project_id="proj-1", episode_no=1)
    assert scene["reverse_angle_available"] is True
    assert scene["reverse_angle_view"] == "门口朝里看：挂钟墙与玻璃柜台"
    missing = {"scene_id": "scene:后院", "display_name": "后院", "scene_reference_id": None}
    annotate_manifest_scene(conn, missing, project_id="proj-1", episode_no=1)
    assert missing["reverse_angle_available"] is False and "reverse_angle_view" not in missing
