"""场景绑定当前生效行（问题一修复，真实 proj_ca86b15ab7d7 EP1 回归）。

从 app.production.storyboard_pack 拆出（该文件在 app/FILE_CONVENTIONS.toml 的
line_count 基线已到零余量 1934，不许再长；新逻辑一律放这里，不放回原文件）。
同属 app.production 前缀，L4，与调用方 storyboard_pack.py 同层。

根因：映射台把场景解析成 scene_reference_id 写进 asset_manifest 是一次性快照
（建包那一刻的「当前生效行」）。之后如果这个场景被整包重生（旧图挪进历史槽，
scene_references.ep_start 变成负数、ep_end 封顶；新图另起一行、ep_start=当前
集号、ep_end=NULL），分镜台如果直接读快照里冻结的旧 id，会把已经作废的历史行
的 scene_canonical 喂给模型、烧进 prompt_text——而生成阶段的参考图装配
（app.multiview._resolve_scene_entry）本就按场景名+集号重新查表，用的是新图。
文字描述旧场景、画面用新图，两者互相矛盾。

修法：在 canonical 锚点查询之前，把每条 manifest 场景的 scene_reference_id
按「此刻对这一集生效」重新解析一遍（复用 app.production.prep_pack 已有的
确定性 DB 绑定函数，其查询本就按 ep_start<=? AND (ep_end IS NULL OR
ep_end>=?) 过滤，历史槽（ep_start<0 且 ep_end 已封顶）结构上不可能命中）。
查不到就显式写 None，不回退旧快照值——旧值已经被证明不是当前生效行，兜底
填充只是把同一个错误换个理由继续发生（CLAUDE.md「不得兜底填充」）。
"""
from __future__ import annotations

from typing import Any

from app.production.prep_pack import (
    _prep_pack_resolve_scene_reference_with_alias,
    _resolve_scene_reference_id,
)
from app.production.storyboard_prop_assets import enrich_prop_manifest_entries
from app.scene_reverse import segment_views as reverse_segment_views
from app.schemas import Bible

_NO_CANONICAL_APPEARANCE_NOTE = (
    "素材库没有为这个角色建立标准外观定妆照（群演/一次性人物，没有定妆照）："
    "由你在本集第一次出现这个角色时自行确定其外观特征（年龄体型、发型头饰、"
    "服装颜色材质、随身物等可视信息），并在本集所有涉及这个角色的段落里原样"
    "沿用同一套自定特征，不得每段重新编写。"
)

_NO_CANONICAL_SCENE_NOTE = (
    "素材库没有为这个场景建立标准场景描述：由你在本集第一次出现这个场景时"
    "自行确定其可视特征（空间格局、主要陈设、光线氛围等），并在本集所有涉及"
    "这个场景的段落里原样沿用同一套自定特征，不得每段重新编写。"
)


def _character_canonical_appearance(
    conn, portrait_id: str | None, *, bible_appearance: str | None = None,
) -> str | None:
    """这个已解析 portrait_id 对应的世界书标准外观锚点串；查不到（含出图已
    解耦到后台、portrait_id 本就为空）时回退 ``bible_appearance``——世界书
    ``Character.appearance_canonical`` 本来就是外观权威，不是 character_
    portraits 行的附属产物。
    """
    if portrait_id:
        row = conn.execute(
            "SELECT appearance FROM character_portraits WHERE id=?", (portrait_id,),
        ).fetchone()
        if row is not None:
            appearance = str(row["appearance"] or "").strip()
            if appearance:
                return appearance
    return bible_appearance


def _scene_canonical_description(
    conn, scene_reference_id: str | None, *, bible_scene_canonical: str | None = None,
) -> str | None:
    """场景侧同构（见 ``_character_canonical_appearance``）：查不到时回退
    ``bible_scene_canonical``（世界书 ``Scene.scene_canonical``）。
    """
    if scene_reference_id:
        row = conn.execute(
            "SELECT scene_canonical FROM scene_references WHERE id=?", (scene_reference_id,),
        ).fetchone()
        if row is not None:
            canonical = str(row["scene_canonical"] or "").strip()
            if canonical:
                return canonical
    return bible_scene_canonical


def _rebind_current_scene_reference(
    conn, scene: dict[str, Any], *,
    bible: Bible | None, project_id: str | None, episode_no: int | None,
) -> None:
    """把这条 manifest 场景的 ``scene_reference_id`` 重新解析成当前生效行，
    替换掉映射包快照冻结的旧值（见模块 docstring）。没有 project_id/episode_no
    就没有可查询的范围，保持原值不动——这是「结构上没法查」，不是「查了没找
    到」，两者不能混为一谈。
    """
    if not project_id or episode_no is None:
        return
    display_name = str(scene.get("display_name") or "")
    if not display_name:
        return
    if bible is not None:
        scene_reference_id, _canonical_name = _prep_pack_resolve_scene_reference_with_alias(
            conn, project_id, episode_no, display_name, bible,
        )
    else:
        scene_reference_id = _resolve_scene_reference_id(conn, project_id, display_name, episode_no)
    scene["scene_reference_id"] = scene_reference_id


def _enrich_asset_manifest_canonical_visuals(
    conn, payload: dict[str, Any], *, bible: Bible | None = None, project_id: str | None = None,
) -> None:
    """原地把世界书标准外观/场景锚点补进 ``payload["asset_manifest"]``。

    在 ``_generate_beat_sheet``/``_generate_all_segment_prompts`` 之前调用一次。
    ``bible`` 非空时兜底取世界书原始锚点——出图已解耦到后台，卡在人物谱/场景库
    但还没出图的资产查不到 character_portraits/scene_references 行，不该被读成
    "没有任何外观信息"。``functional_extras``（群演）没有 portrait_id，天生没有
    标准外观，这里显式写一条说明而不是留空，避免被模型读成"无信息"而各段各编。
    """
    bible_appearance = {c.name: c.appearance_canonical for c in (bible.characters if bible else [])}
    bible_scenes = {s.name: s.scene_canonical for s in (bible.scenes if bible else [])}
    manifest = payload.get("asset_manifest") or {}
    for character in manifest.get("characters") or []:
        character["appearance"] = _character_canonical_appearance(
            conn, character.get("portrait_id"),
            bible_appearance=bible_appearance.get(str(character.get("display_name") or "")),
        ) or _NO_CANONICAL_APPEARANCE_NOTE
    for extra in manifest.get("functional_extras") or []:
        extra["appearance"] = _NO_CANONICAL_APPEARANCE_NOTE
    episode_no = payload.get("episode_no")
    for scene in manifest.get("scenes") or []:
        _rebind_current_scene_reference(conn, scene, bible=bible, project_id=project_id, episode_no=episode_no)
        scene["scene_canonical"] = _scene_canonical_description(
            conn, scene.get("scene_reference_id"),
            bible_scene_canonical=bible_scenes.get(str(scene.get("display_name") or "")),
        ) or _NO_CANONICAL_SCENE_NOTE
        reverse_segment_views.annotate_manifest_scene(conn, scene, project_id=project_id, episode_no=episode_no)
    enrich_prop_manifest_entries(conn, manifest, bible=bible, project_id=project_id, episode_no=episode_no)
