"""道具外观/参考图接入分镜阶段二素材清单（P2：道具形态漂移修复的消费侧）。

背景：用户投诉相邻两段视频里道具形态漂移（猫包一会儿网状一会儿透明）——根因
与人物换装同源（真实 EP1 回归，见 storyboard_pack.py 590 行附近注释）：
asset_manifest 只给道具写 label/description 两个文字字段，模型每段各自现编
外观，没有任何跨段锚点可沿用。WS-P1 并行建世界书物件库（app/schemas/world.py
新增 ``Prop``、新包 ``app.props`` 提供 ``prop_reference_for_episode``），本模块
是消费侧——与 storyboard_pack.py 里 ``_character_canonical_appearance``/
``_scene_canonical_description`` 同一模式，只是道具在 asset_manifest 里没有
预先绑定的 revision id（没有素材库表事先建卡），appearance 靠逐字匹配
``Bible.props[].name``/``aliases``，参考图靠按 label 现查
``prop_reference_for_episode``。

``app.props`` 是并行开发中的另一路产物，落地前后本模块不应有可见差异：查不到
时按"没有标准外观/没有参考图"处理（与角色/场景查不到 portrait_id/
scene_reference_id 时的既有回退语义一致），不阻断分镜生成——道具参考图是
增量能力，不是分镜生成的前置门禁。测试通过 monkeypatch 本模块的
``_prop_reference_lookup`` 验证装配逻辑，不依赖 ``app.props`` 是否已存在。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

_NO_CANONICAL_PROP_APPEARANCE_NOTE = (
    "素材库没有为这个道具建立标准外观定妆照：由你在本集第一次出现这个道具时"
    "自行确定其可视特征（材质、颜色、形状、磨损细节等可视信息），并在本集所有"
    "涉及这个道具的段落里原样沿用同一套自定特征，不得每段重新编写。"
)


def _prop_reference_lookup(conn, project_id: str, name: str, episode_no: int) -> Any:
    """惰性接 WS-P1 的 ``app.props.prop_reference_for_episode``；该模块尚未落地
    （并行开发中）时返回 None，语义等同"查不到"——道具参考图缺失不阻断分镜
    生成。测试直接 monkeypatch 本函数验证装配逻辑，不依赖 app.props 是否存在。
    """
    try:
        from app.props import prop_reference_for_episode
    except ImportError:
        return None
    return prop_reference_for_episode(conn, project_id, name, episode_no)


def _bible_prop_appearance_index(bible: Any) -> dict[str, str]:
    """世界书道具库 name/aliases -> appearance_canonical 的逐字索引。

    ``bible.props`` 字段本身也在并行开发中（Bible schema 尚未落地前用
    ``getattr`` 兜底为空列表），查不到时上层按未命中处理，不是本函数的职责。
    """
    index: dict[str, str] = {}
    for prop in (getattr(bible, "props", None) or []):
        appearance = str(getattr(prop, "appearance_canonical", "") or "").strip()
        if not appearance:
            continue
        name = str(getattr(prop, "name", "") or "").strip()
        if name:
            index[name] = appearance
        for alias in (getattr(prop, "aliases", None) or []):
            alias_text = str(alias or "").strip()
            if alias_text:
                index[alias_text] = appearance
    return index


def enrich_prop_manifest_entries(
    conn, manifest: dict[str, Any], *, bible: Any = None,
    project_id: str | None = None, episode_no: int | None = None,
) -> None:
    """原地给 ``manifest["props"]`` 每项补 ``appearance``（世界书标准外观逐字
    命中，未命中写占位说明——同 ``_NO_CANONICAL_APPEARANCE_NOTE`` 对角色的做法）
    与 ``ref_image_path``（``app.props`` 有 ready 图时才带，供
    ``app.video_modes.prop_references`` 装配参考图池；查不到/未 ready 时不带
    这个键，下游据此判定"这个道具没有可用参考图"，不是留空当成有图）。

    ``project_id``/``episode_no`` 允许为空只是兼容既有单测（历史调用点只测
    外观文字匹配，不关心参考图）；生产唯一调用点
    ``generate_storyboard_pack`` 始终显式传两者。
    """
    appearance_index = _bible_prop_appearance_index(bible)
    for prop in manifest.get("props") or []:
        label = str(prop.get("label") or "").strip()
        prop["appearance"] = appearance_index.get(label) or _NO_CANONICAL_PROP_APPEARANCE_NOTE
        if not (label and project_id and episode_no is not None):
            continue
        row = _prop_reference_lookup(conn, project_id, label, episode_no)
        if not row or str(row["status"] or "") != "ready":
            continue
        image_path = str(row["image_path"] or "").strip()
        if image_path and Path(image_path).is_file():
            prop["ref_image_path"] = image_path
