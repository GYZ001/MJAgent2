"""冻结参考图清单复用必须感知道具选取/排序与外观卡修订变化。

2026-10-01（《顾念长安》第 1 集第 22 段真实故障复核，B 库 ``ver_53281696f64f``
的冻结 ``image_inputs``）：``app.multiview.manifest_revisions_match`` 此前完全
不比较 ``manifest["props"]``，只比人物/场景的
``asset_revision_ids``/``view_fingerprints``/``asset_required``；77232c94 给道具
选取接上 ``resources_order`` 排序闸之后，这道函数依旧对"道具条目多了/少了
这个字段"视而不见——而 ``reference_gallery_matches_library_policy`` 只看常量
``REFERENCE_INPUT_POLICY_VERSION``（77232c94 没有 bump）。两道闸都放行的结果：
修复上线约 3 小时后生成的新版本，仍然原样复用修复前冻结的旧清单——
``app.media_exec.input_reference._prepare_reference_mode_inputs_impl`` 与
``app.media_exec.enqueue_prompt.resolve_reference_gallery`` 两个复用入口都是
受害者，行李箱参考图（``selectedForSeedance``）从冻结那一刻起就没再被选中。

本文件：
1. 用真实回归形状（修复前 ``manifest["props"]`` 没有 ``resources_order``/
   ``prop_revision_id`` 字段——不是值为 None，是键压根没写过）直接测
   ``manifest_revisions_match``，证明现在会判过期；外观卡单独换版本（图重新
   登记）也要判过期；两侧都是修复后形状则不该误判过期（负对照，否则每次
   重试都要重新装配，白白多花生成成本）。
2. 判过期之后触发重新装配（``select_library_references`` + ``ref_pack_
   priority``），证明行李箱真的能被选中——不是"判完过期就不管了"。
"""
from __future__ import annotations

from app.multiview import manifest_revisions_match, ref_pack_priority
from app.video_modes.mode_selection import ReferenceImageAsset
from app.video_modes.reference_assemble import select_library_references

# 第 1 集第 22 段真实回归的 7 件道具，顺序与 B 库冻结 manifest 一致。
_SHOT22_PROP_LABELS = [
    "小木星星", "浅灰色卫衣", "米白色针织开衫", "浅蓝色碎花长裙",
    "水泡坏的行李箱", "外套", "深灰色围巾",
]


def _real_shot22_props(*, with_resources_order: bool) -> list[dict]:
    """``with_resources_order=False`` 复刻修复前的冻结快照：没有
    ``resources_order``/``prop_revision_id`` 键。``True`` 复刻修复后重新计算
    出来的当前依赖：按声明顺序打上下标与外观卡行 id。"""
    out = []
    for index, label in enumerate(_SHOT22_PROP_LABELS):
        entry = {
            "label": label, "description": "", "ready": True,
            "image_path": f"/projects/proj/prop_refs/{label}.png",
        }
        if with_resources_order:
            entry["resources_order"] = index
            entry["prop_revision_id"] = f"prop_{index}_rev1"
        out.append(entry)
    return out


def _manifest(*, with_resources_order: bool) -> dict:
    return {
        "episode_no": 1, "shot_id": "shot_7f60ddc00d6e",
        "characters": [
            {"name": "温念", "look_revision_id": "rev-wn", "asset_required": True},
            {"name": "顾屿", "look_revision_id": "rev-gy", "asset_required": True},
        ],
        "scene": {"name": "顾屿家客房", "scene_revision_id": "rev-scene", "asset_required": True},
        "additional_scenes": [],
        "props": _real_shot22_props(with_resources_order=with_resources_order),
    }


def test_frozen_manifest_without_resources_order_is_stale_against_fixed_manifest() -> None:
    frozen = _manifest(with_resources_order=False)
    current = _manifest(with_resources_order=True)
    assert manifest_revisions_match(frozen, current) is False


def test_manifest_unchanged_on_both_sides_still_matches_no_spurious_reassembly() -> None:
    """负对照：两侧都是修复后的同一份 manifest，不该被判过期。"""
    frozen = _manifest(with_resources_order=True)
    current = _manifest(with_resources_order=True)
    assert manifest_revisions_match(frozen, current) is True


def test_prop_card_revision_change_alone_marks_manifest_stale() -> None:
    """只换了外观卡图（``prop_revision_id`` 变了，``resources_order`` 没变）
    也要判过期——道具外观卡重新登记后旧清单里的图已经不是当前外观。"""
    frozen = _manifest(with_resources_order=True)
    current = _manifest(with_resources_order=True)
    current["props"][4]["prop_revision_id"] = "prop_4_rev2"  # 行李箱外观卡重新登记
    assert manifest_revisions_match(frozen, current) is False


def _asset(entity_type: str, name: str, **kwargs) -> ReferenceImageAsset:
    return ReferenceImageAsset(
        id=f"{entity_type}-{name}", url="", type=entity_type, source="asset_library",
        path=f"/tmp/{entity_type}-{name}.jpg", entity_type=entity_type, entity_name=name,
        relatedCharacterIds=[name], **kwargs,
    )


def test_stale_manifest_reassembly_selects_the_previously_dropped_suitcase() -> None:
    """判过期只是第一步；重新装配必须真的按新 resources_order 选图。真实回归：
    9 张上限内，2 人物 + 1 场景 + 7 道具共 10 项必须舍 1 项；水泡坏的行李箱
    声明顺序第 5（resources_order=4），比声明顺序最后的深灰色围巾
    （resources_order=6）更靠前，理应是围巾被舍而不是行李箱。"""
    props = _real_shot22_props(with_resources_order=True)
    assets = [
        _asset("character", "温念"), _asset("character", "顾屿"),
        _asset("scene", "顾屿家客房"),
        *[
            _asset("prop", prop["label"], resources_order=prop["resources_order"])
            for prop in props
        ],
    ]
    selected = select_library_references(assets, ["温念", "顾屿"], max_images=9)
    selected_props = {a.entity_name for a in selected if a.entity_type == "prop"}
    assert len(selected) == 9
    assert "水泡坏的行李箱" in selected_props
    assert "深灰色围巾" not in selected_props

    # ref_pack_priority（装箱阶段第二道闸，供应商请求级别的超限裁剪）同样
    # 必须按 resources_order 保留行李箱、舍弃围巾，不是只在
    # select_library_references 这一道闸生效。
    ref_dicts = [
        {
            "id": f"prop-{prop['label']}", "type": "prop", "source": "asset_library",
            "resources_order": prop["resources_order"],
        }
        for prop in props
    ]
    kept = sorted(ref_dicts, key=ref_pack_priority)[:6]  # 9 - 2 人物 - 1 场景 = 6 个道具名额
    kept_ids = {ref["id"] for ref in kept}
    assert "prop-水泡坏的行李箱" in kept_ids
    assert "prop-深灰色围巾" not in kept_ids
