"""道具参考图接入参考图池（P2 视频侧）。

覆盖三层：
1. ``app.video_modes.prop_references``——分镜段 resources.props 接上
   ``app.props`` 的 ready 图（否则不带 ready_image_path），manifest 展开成
   与人物/场景同形状的锚点。
2. ``app.video_modes.reference_assemble.select_library_references``——人物 >
   场景 > 道具的选取顺序，超出 ``max_images`` 时道具最先被舍弃。
3. 打包/说明文案/``@道具名`` 替换（``app.video_modes.seedance_pack``/
   ``seedance_reference_notes``）与 ``reference_gallery_matches_library_
   policy`` 放行 prop 类型。

``app.props`` 已在并行开发中落地（``app/props/store.py::prop_reference_for_
episode``），但本文件仍然只通过 monkeypatch
``app.video_modes.prop_references._prop_reference_lookup`` 打桩，不直接依赖
真实表/文件系统状态，符合派单「不依赖 WS-P1 是否已完成」的要求。
"""
from __future__ import annotations

from app import db, video_modes
from app.multiview import _storyboard_pack_asset_dependencies, library_anchor_assets_from_manifest
from app.schemas import Bible, World
from app.video_modes.mode_selection import ReferenceImageAsset
from app.video_modes.prop_references import prop_library_anchors, resolve_segment_prop_manifest_entries
from app.video_modes.reference_assemble import select_library_references
from app.video_modes.reference_prompt import reference_gallery_matches_library_policy
from app.video_modes.seedance_reference_notes import build_seedance_reference_prompt_notes
from tests.conftest import patch_video_modes_everywhere

import app.video_modes.prop_references as prop_references


def _bible() -> Bible:
    return Bible(characters=[], world=World(visual_style_canonical="水墨"))


# ---------------------------------------------------------------------------
# resolve_segment_prop_manifest_entries / prop_library_anchors
# ---------------------------------------------------------------------------

def test_resolve_segment_prop_manifest_entries_ready_with_existing_file(monkeypatch, tmp_path) -> None:
    image = tmp_path / "cat_bag.jpg"
    image.write_bytes(b"jpeg")
    monkeypatch.setattr(
        prop_references, "_prop_reference_lookup",
        lambda conn, project_id, name, episode_no: {"status": "ready", "image_path": str(image)},
    )
    out = resolve_segment_prop_manifest_entries(
        [{"label": "旧猫包", "description": "破猫包"}], conn=object(), project_id="proj-1", episode_no=3,
    )
    assert out == [{"label": "旧猫包", "description": "破猫包", "ready": True, "image_path": str(image)}]


def test_resolve_segment_prop_manifest_entries_not_ready_when_no_row(monkeypatch) -> None:
    monkeypatch.setattr(prop_references, "_prop_reference_lookup", lambda *a, **k: None)
    out = resolve_segment_prop_manifest_entries(
        [{"label": "旧猫包", "description": "破猫包"}], conn=object(), project_id="proj-1", episode_no=3,
    )
    assert out == [{"label": "旧猫包", "description": "破猫包", "ready": False, "image_path": ""}]


def test_resolve_segment_prop_manifest_entries_not_ready_when_file_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        prop_references, "_prop_reference_lookup",
        lambda conn, project_id, name, episode_no: {"status": "ready", "image_path": "/nonexistent.jpg"},
    )
    out = resolve_segment_prop_manifest_entries(
        [{"label": "旧猫包"}], conn=object(), project_id="proj-1", episode_no=3,
    )
    assert out[0]["ready"] is False


def test_prop_library_anchors_only_ready_entries_with_real_file(tmp_path) -> None:
    image = tmp_path / "cat_bag.jpg"
    image.write_bytes(b"jpeg")
    manifest_props = [
        {"label": "旧猫包", "ready": True, "image_path": str(image)},
        {"label": "没图的道具", "ready": False, "image_path": ""},
    ]
    anchors = prop_library_anchors(manifest_props)
    assert len(anchors) == 1
    assert anchors[0]["entity_type"] == "prop"
    assert anchors[0]["entity_name"] == "旧猫包"
    assert anchors[0]["type"] == "prop"
    assert anchors[0]["source"] == "asset_library"


# ---------------------------------------------------------------------------
# 全链路：_storyboard_pack_asset_dependencies -> manifest["props"] ->
# library_anchor_assets_from_manifest
# ---------------------------------------------------------------------------

def test_storyboard_pack_asset_dependencies_props_flow_into_library_anchors(monkeypatch, tmp_path) -> None:
    conn = db.get_conn()
    image = tmp_path / "cat_bag.jpg"
    image.write_bytes(b"jpeg")
    monkeypatch.setattr(
        prop_references, "_prop_reference_lookup",
        lambda c, project_id, name, episode_no: (
            {"status": "ready", "image_path": str(image)} if name == "旧猫包" else None
        ),
    )
    segment = {"resources": {"characters": [], "scenes": [], "props": [
        {"label": "旧猫包", "description": "破猫包"},
        {"label": "没图的道具", "description": "x"},
    ]}}
    manifest = _storyboard_pack_asset_dependencies(
        project_id="proj-1", episode_no=3, shot_id="shot-1", segment=segment,
        conn=conn, bible=_bible(),
    )
    props = manifest["props"]
    assert {p["label"] for p in props} == {"旧猫包", "没图的道具"}
    ready_prop = next(p for p in props if p["label"] == "旧猫包")
    assert ready_prop["ready"] is True and ready_prop["image_path"] == str(image)

    anchors = library_anchor_assets_from_manifest(manifest)
    prop_anchors = [a for a in anchors if a.get("entity_type") == "prop"]
    assert len(prop_anchors) == 1
    assert prop_anchors[0]["entity_name"] == "旧猫包"


# ---------------------------------------------------------------------------
# select_library_references：人物 > 场景 > 道具，超出上限先舍道具
# ---------------------------------------------------------------------------

def _asset(entity_type: str, name: str, **kwargs) -> ReferenceImageAsset:
    return ReferenceImageAsset(
        id=f"{entity_type}-{name}", url="", type=entity_type, source="asset_library",
        path=f"/tmp/{entity_type}-{name}.jpg", entity_type=entity_type, entity_name=name,
        relatedCharacterIds=[name], **kwargs,
    )


def test_select_library_references_orders_character_scene_then_prop() -> None:
    assets = [
        _asset("prop", "旧猫包"),
        _asset("scene", "山顶"),
        _asset("character", "少年"),
    ]
    selected = select_library_references(assets, ["少年"], max_images=9)
    kinds = [a.entity_type for a in selected]
    assert kinds == ["character", "scene", "prop"]


def test_select_library_references_drops_props_first_when_over_cap() -> None:
    assets = [
        _asset("character", "少年"),
        _asset("scene", "山顶"),
        _asset("prop", "旧猫包"),
    ]
    # 上限只够人物+场景两张：道具应该被完全挤掉，不是随机哪个被挤掉。
    selected = select_library_references(assets, ["少年"], max_images=2)
    kinds = {a.entity_type for a in selected}
    assert kinds == {"character", "scene"}
    assert len(selected) == 2


def test_select_library_references_props_fill_remaining_budget_dedup_by_name() -> None:
    assets = [
        _asset("character", "少年"),
        _asset("prop", "旧猫包"),
        _asset("prop", "旧猫包"),  # 同名重复候选应去重
        _asset("prop", "折扇"),
    ]
    selected = select_library_references(assets, ["少年"], max_images=3)
    prop_names = {a.entity_name for a in selected if a.entity_type == "prop"}
    assert prop_names == {"旧猫包", "折扇"}


# ---------------------------------------------------------------------------
# 打包说明文案 + @道具名 替换 + 库策略放行 prop
# ---------------------------------------------------------------------------

def test_pack_reference_images_for_seedance_includes_prop_and_purpose_text(monkeypatch) -> None:
    patch_video_modes_everywhere(monkeypatch, "max_reference_images", lambda: 9)
    patch_video_modes_everywhere(monkeypatch, "max_character_reference_images", lambda: 2)
    refs = [
        {
            "id": "character-a", "url": "data:image/jpeg;base64,x", "type": "character",
            "source": "asset_library", "selectedForSeedance": True, "entity_name": "少年",
            "relatedCharacterIds": ["少年"],
        },
        {
            "id": "prop-catbag", "url": "data:image/jpeg;base64,y", "type": "prop",
            "source": "asset_library", "selectedForSeedance": True, "entity_name": "旧猫包",
            "relatedCharacterIds": ["旧猫包"],
        },
    ]
    packed = video_modes.pack_reference_images_for_seedance(refs, max_images=9)
    assert {ref["id"] for ref in packed} == {"character-a", "prop-catbag"}

    prompt = build_seedance_reference_prompt_notes(
        "少年抱着 @旧猫包 走进院子。", packed, duration_s=5,
    )
    assert "道具旧猫包参考，只用来锁定外观与材质" in prompt
    prop_index = next(i for i, ref in enumerate(packed, 1) if ref["id"] == "prop-catbag")
    assert f"@图片{prop_index}" in prompt
    assert "@旧猫包" not in prompt.split("参考图说明：")[0]


def test_reference_gallery_matches_library_policy_allows_prop_type() -> None:
    meta = {
        "reference_input_policy_version": video_modes.REFERENCE_INPUT_POLICY_VERSION,
        "reference_images": [{
            "path": __file__,  # 任意真实存在的文件路径，满足"可读"判据
            "type": "prop", "entity_type": "prop", "source": "asset_library",
            "selectedForSeedance": True,
        }],
    }
    assert reference_gallery_matches_library_policy(meta) is True
