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
from app.multiview import _storyboard_pack_asset_dependencies, library_anchor_assets_from_manifest, ref_pack_priority
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
        lambda conn, project_id, name, episode_no: {
            "id": "prop_cat_bag_rev1", "status": "ready", "image_path": str(image),
        },
    )
    out = resolve_segment_prop_manifest_entries(
        [{"label": "旧猫包", "description": "破猫包"}], conn=object(), project_id="proj-1", episode_no=3,
    )
    assert out == [{
        "label": "旧猫包", "description": "破猫包", "ready": True, "image_path": str(image),
        "resources_order": 0, "prop_revision_id": "prop_cat_bag_rev1",
    }]


def test_resolve_segment_prop_manifest_entries_not_ready_when_no_row(monkeypatch) -> None:
    monkeypatch.setattr(prop_references, "_prop_reference_lookup", lambda *a, **k: None)
    out = resolve_segment_prop_manifest_entries(
        [{"label": "旧猫包", "description": "破猫包"}], conn=object(), project_id="proj-1", episode_no=3,
    )
    assert out == [{
        "label": "旧猫包", "description": "破猫包", "ready": False, "image_path": "",
        "resources_order": 0, "prop_revision_id": None,
    }]


def test_resolve_segment_prop_manifest_entries_stamps_order_by_input_position(monkeypatch) -> None:
    """2026-10-01（第 1 集第 19/20 段真实回归）：resources_order 必须原样反映
    ``resources.props`` 的声明顺序（模型给出的重要性排序），不是按 label 重排、
    不是查库结果决定——哪怕第一项查不到图，它的下标仍然是 0。"""
    monkeypatch.setattr(prop_references, "_prop_reference_lookup", lambda *a, **k: None)
    out = resolve_segment_prop_manifest_entries(
        [{"label": "行李箱"}, {"label": "热牛奶"}, {"label": "小木星星"}],
        conn=object(), project_id="proj-1", episode_no=3,
    )
    assert [entry["resources_order"] for entry in out] == [0, 1, 2]
    assert [entry["label"] for entry in out] == ["行李箱", "热牛奶", "小木星星"]


def test_resolve_segment_prop_manifest_entries_not_ready_when_file_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        prop_references, "_prop_reference_lookup",
        lambda conn, project_id, name, episode_no: {
            "id": "prop_cat_bag_rev1", "status": "ready", "image_path": "/nonexistent.jpg",
        },
    )
    out = resolve_segment_prop_manifest_entries(
        [{"label": "旧猫包"}], conn=object(), project_id="proj-1", episode_no=3,
    )
    assert out[0]["ready"] is False


def test_prop_library_anchors_only_ready_entries_with_real_file(tmp_path) -> None:
    image = tmp_path / "cat_bag.jpg"
    image.write_bytes(b"jpeg")
    manifest_props = [
        {"label": "旧猫包", "ready": True, "image_path": str(image), "resources_order": 2},
        {"label": "没图的道具", "ready": False, "image_path": ""},
    ]
    anchors = prop_library_anchors(manifest_props)
    assert len(anchors) == 1
    assert anchors[0]["entity_type"] == "prop"
    assert anchors[0]["entity_name"] == "旧猫包"
    assert anchors[0]["type"] == "prop"
    assert anchors[0]["source"] == "asset_library"
    assert anchors[0]["resources_order"] == 2


def test_prop_library_anchors_resources_order_defaults_to_none_for_legacy_entries(tmp_path) -> None:
    """旧数据没有 resources_order 字段时（没走过本次改动的 resolve_segment_prop_
    manifest_entries）不得兜底编一个序号，老老实实传 None——ref_pack_priority
    据此把它排在有序号的道具之后，见该函数测试。"""
    image = tmp_path / "cat_bag.jpg"
    image.write_bytes(b"jpeg")
    anchors = prop_library_anchors([{"label": "旧猫包", "ready": True, "image_path": str(image)}])
    assert anchors[0]["resources_order"] is None


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
            {"id": "prop_cat_bag_rev1", "status": "ready", "image_path": str(image)}
            if name == "旧猫包" else None
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


def test_select_library_references_keeps_every_named_scene_not_just_first() -> None:
    """多场景转场段（additional_scenes）与本段被点名的反打视角（entity_name
    带「·反打」后缀）互为不同名字，都应该进最终选取，不是只挑第一个场景。
    单场景（本用例的既有兄弟测试 test_..._orders_character_scene_then_prop）
    输出保持逐条不变，锁住改动前行为。"""
    assets = [
        _asset("character", "少年"),
        _asset("scene", "山顶", view_role="establishing"),
        _asset("scene", "山顶·反打", view_role="reverse_angle"),
        _asset("scene", "老宅", view_role="establishing"),
    ]
    selected = select_library_references(assets, ["少年"], max_images=9)
    scene_names = {a.entity_name for a in selected if a.entity_type == "scene"}
    assert scene_names == {"山顶", "山顶·反打", "老宅"}


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
# select_library_references：道具档内按 resources_order 排序（2026-10-01，第
# 1 集第 20 段真实回归：2 人物 + 场景 + 8 道具已超 9 张库参考上限，这道闸——
# 不是 app.multiview.ref_pack_priority——先行截断，此前道具间 tiebreak 是
# asset.path（文件路径，与 resources.props 声明顺序无关），哪件道具被截掉
# 与这件道具在本段的重要性无关）
# ---------------------------------------------------------------------------

def test_select_library_references_props_use_resources_order_not_path() -> None:
    """path 字典序与 resources_order 刻意反向："zzz" 的 path 比 "aaa" 字典序更
    大，若仍按 path 排会选 aaa；按 resources_order 排必须选 zzz（order=0）。"""
    assets = [
        _asset("character", "少年"),
        _asset("prop", "zzz", resources_order=0),
        _asset("prop", "aaa", resources_order=1),
    ]
    selected = select_library_references(assets, ["少年"], max_images=2)
    prop_names = {a.entity_name for a in selected if a.entity_type == "prop"}
    assert prop_names == {"zzz"}


def test_select_library_references_props_without_resources_order_keep_path_tiebreak() -> None:
    """都没有 resources_order（旧数据）时，组内排序必须与改动前逐字一致——仍按
    path 字典序，不受本次改动影响。"""
    assets = [
        _asset("character", "少年"),
        _asset("prop", "zzz"),
        _asset("prop", "aaa"),
    ]
    selected = select_library_references(assets, ["少年"], max_images=2)
    prop_names = {a.entity_name for a in selected if a.entity_type == "prop"}
    assert prop_names == {"aaa"}  # path 字典序 aaa < zzz，与改动前行为一致


def test_select_library_references_props_with_order_beat_legacy_ones_without() -> None:
    assets = [
        _asset("character", "少年"),
        _asset("prop", "legacy"),
        _asset("prop", "ordered", resources_order=5),
    ]
    selected = select_library_references(assets, ["少年"], max_images=2)
    prop_names = {a.entity_name for a in selected if a.entity_type == "prop"}
    assert prop_names == {"ordered"}


def test_select_library_references_full_chain_keeps_lowest_order_props_over_cap() -> None:
    """真实故障复现：第 20 段 2 人物 + 场景 + 8 道具已超 9 张库参考上限。保留的
    道具必须是 resources_order 最小的那 6 件（9 - 2 人物 - 1 场景 = 6 个名额），
    与 path 字典序无关——8 件道具的 path 按 entity_name 字典序与 resources_order
    刻意反向排列。"""
    assets = [
        _asset("character", "少年"),
        _asset("character", "阿姨"),
        _asset("scene", "老宅"),
    ] + [
        _asset("prop", f"道具{chr(ord('z') - i)}", resources_order=i)
        for i in range(8)
    ]
    selected = select_library_references(assets, ["少年", "阿姨"], max_images=9)
    prop_assets = [a for a in selected if a.entity_type == "prop"]
    assert len(prop_assets) == 6
    kept_orders = sorted(a.resources_order for a in prop_assets)
    assert kept_orders == list(range(6))


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
        "少年抱着 @旧猫包 走进院子。", packed, duration_s=5, aspect_ratio="9:16",
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


# ---------------------------------------------------------------------------
# ref_pack_priority：超限裁剪按 resources_order，不靠随机 id
# （2026-10-01，第 1 集第 19/20 段真实成片复查：8 件道具 + 人物/场景超过 9 张
# 参考图上限，行李箱因为随机 id 被舍弃，成片颜色与卡片不符）
# ---------------------------------------------------------------------------

def _prop_ref(ref_id: str) -> dict:
    return {"id": ref_id, "type": "prop", "source": "asset_library", "entity_name": ref_id}


def test_ref_pack_priority_orders_props_by_resources_order_not_id() -> None:
    """id 刻意反向排列（字典序 zzz < aaa 不成立，这里 "z..." 比 "a..." 大），
    证明排序结果跟的是 resources_order 不是 id 字典序。"""
    low_order_high_id = {**_prop_ref("zzz-id"), "resources_order": 0}
    high_order_low_id = {**_prop_ref("aaa-id"), "resources_order": 1}
    ordered = sorted([high_order_low_id, low_order_high_id], key=ref_pack_priority)
    assert [r["id"] for r in ordered] == ["zzz-id", "aaa-id"]


def test_ref_pack_priority_props_without_resources_order_sort_after_ones_with_it() -> None:
    legacy_no_order = _prop_ref("legacy")
    has_order = {**_prop_ref("new"), "resources_order": 5}
    ordered = sorted([legacy_no_order, has_order], key=ref_pack_priority)
    assert [r["id"] for r in ordered] == ["new", "legacy"]


def test_ref_pack_priority_legacy_props_keep_original_quality_id_tiebreak() -> None:
    """都没有 resources_order（旧数据）时，组内排序必须与改动前逐字一致——
    按 -quality 再按 id 排，不受本次改动影响。"""
    a = _prop_ref("b-id")
    b = _prop_ref("a-id")
    ordered = sorted([a, b], key=ref_pack_priority)
    assert [r["id"] for r in ordered] == ["a-id", "b-id"]  # 都 quality=0，退化到 id 字典序


def test_ref_pack_priority_props_still_sort_after_characters_and_scenes() -> None:
    """道具档排序改动不影响既有的「人物/场景先于道具」大分组。"""
    prop = {**_prop_ref("prop-1"), "resources_order": 0}
    character = {"id": "char-1", "type": "character", "source": "asset_library"}
    ordered = sorted([prop, character], key=ref_pack_priority)
    assert [r["id"] for r in ordered] == ["char-1", "prop-1"]


def test_pack_reference_images_for_seedance_keeps_lowest_resources_order_prop_when_over_cap(monkeypatch) -> None:
    """全链路真实故障复现：第 20 段 resources.props 列了多件道具，加上人物/场景
    超过 9 张参考图上限——改动前这里的取舍看随机 id，改动后必须稳定保留
    resources_order 最小（模型声明顺序最靠前）的那件，与 id 字典序无关。"""
    patch_video_modes_everywhere(monkeypatch, "max_reference_images", lambda: 9)
    patch_video_modes_everywhere(monkeypatch, "max_character_reference_images", lambda: 2)
    character_refs = [
        {
            "id": f"char-{i}", "url": "data:image/jpeg;base64,x", "type": "character",
            "source": "asset_library", "selectedForSeedance": True, "entity_name": f"角色{i}",
            "relatedCharacterIds": [f"角色{i}"],
        }
        for i in range(6)
    ]
    # 8 件道具，id 刻意与 resources_order 反向排列，只有 1 个名额留给道具
    # （6 人物 + 9 上限 = 3 个名额，但 required_identity_names 为空、
    # char_limit 取 max(max_character_reference_images()=2, 0)=2，实际占用
    # 由 pack_references_by_purpose 内部 character_limit 决定——这里只关心
    # 道具之间的相对取舍，不精算人物占用数）。
    prop_refs = [
        {
            "id": f"prop-{chr(ord('z') - i)}", "url": "data:image/jpeg;base64,y", "type": "prop",
            "source": "asset_library", "selectedForSeedance": True, "entity_name": f"道具{i}",
            "relatedCharacterIds": [f"道具{i}"], "resources_order": i,
        }
        for i in range(8)
    ]
    packed = video_modes.pack_reference_images_for_seedance(character_refs + prop_refs, max_images=9)
    packed_prop_ids = [ref["id"] for ref in packed if ref["type"] == "prop"]
    assert packed_prop_ids, "至少应该保留一件道具"
    # 保留的道具必须是 resources_order 最小的那些（即 prop-z, prop-y, ... 按
    # i 递增取前 N 个），不是 id 字典序最小/最大的那些。
    kept_orders = sorted(
        next(p["resources_order"] for p in prop_refs if p["id"] == pid) for pid in packed_prop_ids
    )
    assert kept_orders == list(range(len(packed_prop_ids)))
