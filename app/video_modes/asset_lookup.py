"""人物/场景参考图库资产查找：路径拼装与库内既有素材读取。"""
from __future__ import annotations

import json
import re

from pathlib import Path
from typing import Any

from app import config, hiagent
from app.db import new_id
from app.schemas import Bible

from .mode_selection import ReferenceImageAsset



def _safe_ref_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "ref"


def reference_image_path(project_id: str, episode_no: int, shot_no: int, ref_type: str, index: int) -> Path:
    d = config.PROJECTS_DIR / project_id / "episodes" / str(episode_no) / "shots" / str(shot_no) / "references"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{index:02d}_{_safe_ref_name(ref_type)}.jpg"


def _asset_from_path(*, path: str, ref_type: str, source: str, shot_id: str | None = None,
                     episode_id: str | None = None, scene_id: str | None = None,
                     related_character_ids: list[str] | None = None,
                     quality_score: float | None = None, qa: dict[str, Any] | None = None,
                     entity_type: str | None = None, entity_name: str | None = None,
                     library_revision_id: str | None = None, library_view_id: str | None = None,
                     view_role: str | None = None, purposes: list[str] | None = None,
                     required: bool = False, slot_key: str | None = None,
                     costume_mode: str | None = None,
                     resources_order: int | None = None) -> ReferenceImageAsset:
    return ReferenceImageAsset(
        id=new_id("ref"),
        url=hiagent.data_url_from_file(path),
        path=path,
        type=ref_type,
        source=source,
        shotId=shot_id,
        episodeId=episode_id,
        sceneId=scene_id,
        relatedCharacterIds=related_character_ids or [],
        qualityScore=quality_score,
        qa=qa,
        entity_type=entity_type,
        entity_name=entity_name,
        library_revision_id=library_revision_id,
        library_view_id=library_view_id,
        view_role=view_role,
        purposes=list(purposes or []),
        required=required,
        slot_key=slot_key,
        costume_mode=costume_mode,
        resources_order=resources_order,
    )


def _shot_time_anchor(shot: Any) -> str | None:
    """本镜时间线锚点键（WS11）：与 app.validators.resource_forecast.
    character_time_anchor_advisories 同一判据（age/year 取最具体的一条），
    返回可直接喂给 portrait_lookup_for_episode 的 anchor_key；shot 未带
    storyboard_pack_segment 或没有可查询锚点（era/relative）时返回 None，
    不兜底猜测。"""
    from app.validators.resource_forecast import _best_time_anchor

    segment = getattr(shot, "storyboard_pack_segment", None) or {}
    anchor = _best_time_anchor(segment.get("timeline_anchors") or [])
    return anchor["anchor_key"] if anchor else None


def _character_view_asset(
    preferred: dict[str, Any], name: str, purposes: list[str], costume_mode: str | None,
) -> ReferenceImageAsset | None:
    """优先 front_full 的人物库视角 -> ``ReferenceImageAsset``；图片文件缺失时
    返回 None（沿用原 ``character_reference_assets`` 该分支的逻辑，抽成独立
    函数只是为了不把新增的 ``costume_mode`` 接线逻辑焊进调用方那个已顶格撞在
    ``app/FILE_CONVENTIONS.toml`` 单函数基线的大函数里）。"""
    path = preferred.get("image_path")
    if not path or not Path(path).exists():
        return None
    qa = None
    if preferred.get("qa_json"):
        try:
            qa = json.loads(preferred["qa_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            qa = None
    score = None
    if isinstance(qa, dict) and qa.get("overall") is not None:
        try:
            score = float(qa["overall"])
        except (TypeError, ValueError):
            score = None
    try:
        return _asset_from_path(
            path=path, ref_type="character", source="asset_library",
            related_character_ids=[name], quality_score=score,
            qa=qa or {"status": "unverified", "overall": None, "issues": ["人物库图缺少 QA"]},
            entity_type="character", entity_name=name,
            library_revision_id=preferred.get("portrait_id"),
            library_view_id=preferred.get("id"),
            view_role=preferred.get("view_role"),
            purposes=purposes, costume_mode=costume_mode,
        )
    except OSError:
        return None


def _character_fallback_asset(
    path: str, name: str, purposes: list[str], costume_mode: str | None,
) -> ReferenceImageAsset | None:
    """无多视角库图时的单图回退 -> ``ReferenceImageAsset``；同 ``_character_view_asset``
    的拆分理由。"""
    try:
        return _asset_from_path(
            path=path, ref_type="character", source="asset_library",
            related_character_ids=[name], quality_score=None,
            qa={"status": "unverified", "overall": None, "issues": ["旧单图无分项 QA"]},
            entity_type="character", entity_name=name, view_role="front_full",
            purposes=purposes, costume_mode=costume_mode,
        )
    except OSError:
        return None


def character_reference_assets(bible: Bible, character_names: list[str], *, limit: int,
                               project_id: str | None = None,
                               episode_no: int | None = None,
                               shot: Any | None = None) -> list[ReferenceImageAsset]:
    """人物库图作为 keyframe_seed + qa_anchor；默认不直接 video_input。

    ``costume_mode``（见 app.portraits.neutral_identity）原样从命中的
    ``character_portraits`` 行带到 ``ReferenceImageAsset``，供
    ``seedance_reference_notes`` 切换参考图用途文案；未接这条线会让中性定妆照
    在真实生成请求里形同虚设（只读复核 2026-09-30 实测复现）。
    """
    # 函数内 import app.multiview（新增 portrait_row_for_episode，与同语句里其它
    # 名字保持同一处理方式）：模块级 import 会在本文件里冻结一份引用副本，撞上
    # tests/test_video_modes_monkeypatch_guard.py 记录的拆包陷阱——
    # monkeypatch.setattr(app.multiview, name, stub) 这类既有测试打桩会静默
    # 打不中（本文件的局部名字仍解析到旧引用）；函数内 import 保证每次调用都
    # 从 app.multiview 重新取值，不是为了避免循环依赖。
    from app.multiview import (
        PURPOSE_KEYFRAME_SEED, PURPOSE_QA_ANCHOR, portrait_row_for_episode, portrait_views_for_episode,
        character_multiview_enabled,
    )
    assets: list[ReferenceImageAsset] = []
    by_name, time_anchor = {c.name: c for c in bible.characters}, _shot_time_anchor(shot)
    purposes = [PURPOSE_KEYFRAME_SEED, PURPOSE_QA_ANCHOR]
    for name in character_names:
        if len(assets) >= limit:
            break
        c = by_name.get(name)
        views = []
        if c is not None and project_id is not None and character_multiview_enabled():
            views = portrait_views_for_episode(project_id, name, episode_no, ready_only=True)
        if views:
            # 优先 front_full，其次任意 ready 视角
            preferred = next((v for v in views if v.get("view_role") == "front_full"), views[0])
            row = portrait_row_for_episode(project_id, name, episode_no)
            costume_mode = row["costume_mode"] if row and "costume_mode" in row.keys() else None
            asset = _character_view_asset(preferred, name, purposes, costume_mode)
            if asset is not None:
                assets.append(asset)
            continue
        # 回退单图
        path, costume_mode = None, None
        if c is not None and project_id is not None:
            from app.portraits.portrait_lookup import portrait_lookup_for_episode
            lookup = portrait_lookup_for_episode(project_id, name, episode_no, time_anchor=time_anchor)
            path, costume_mode = lookup["image_path"], lookup.get("costume_mode")
        if not path:
            path = getattr(c, "ref_image_path", None) if c else None
        if not path or not Path(path).exists():
            continue
        asset = _character_fallback_asset(path, name, purposes, costume_mode)
        if asset is not None:
            assets.append(asset)
    return assets


def scene_reference_assets(bible: Bible, scene_name: str, *, project_id: str | None = None,
                           episode_no: int | None = None) -> list[ReferenceImageAsset]:
    """该镜场景的场景库图 →[ReferenceImageAsset]（环境真值锚点；默认 keyframe_seed+qa_anchor）。"""
    from app.multiview import (
        PURPOSE_KEYFRAME_SEED, PURPOSE_QA_ANCHOR, scene_views_for_episode, scene_multiview_enabled,
    )
    if not scene_name:
        return []
    if project_id and scene_multiview_enabled():
        views = scene_views_for_episode(project_id, scene_name, episode_no, ready_only=True)
        if views:
            preferred = next((v for v in views if v.get("view_role") == "establishing"), views[0])
            path = preferred.get("image_path")
            qa = None
            if preferred.get("qa_json"):
                try:
                    qa = json.loads(preferred["qa_json"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    qa = None
            score = float(qa["overall"]) if isinstance(qa, dict) and qa.get("overall") is not None else None
            if path and Path(path).exists():
                try:
                    return [_asset_from_path(
                        path=path, ref_type="scene", source="asset_library",
                        quality_score=score, qa=qa or {"status": "unverified", "overall": None},
                        entity_type="scene", entity_name=scene_name,
                        library_revision_id=preferred.get("scene_reference_id"),
                        library_view_id=preferred.get("id"),
                        view_role=preferred.get("view_role"),
                        purposes=[PURPOSE_KEYFRAME_SEED, PURPOSE_QA_ANCHOR],
                    )]
                except OSError:
                    pass
    from app.scenes import scene_ref_for_episode, scene_ref_qa_for_episode
    path = scene_ref_for_episode(project_id, scene_name, episode_no) if project_id else None
    # 有项目上下文时，scene_ref_for_episode 已执行新版整包硬门禁；不得再回退到
    # bible_json 中可能仍指向历史硬失败图的兼容缓存。
    if not path and not project_id:
        by_name = {s.name: s for s in (getattr(bible, "scenes", None) or [])}
        sc = by_name.get(scene_name)
        path = getattr(sc, "ref_image_path", None) if sc else None
    if not path or not Path(path).exists():
        return []
    qa = scene_ref_qa_for_episode(project_id, scene_name, episode_no) if project_id else None
    score = float(qa.get("overall")) if isinstance(qa, dict) and qa.get("overall") is not None else None
    try:
        return [_asset_from_path(
            path=path, ref_type="scene", source="asset_library",
            quality_score=score, qa=qa or {"status": "unverified", "overall": None},
            entity_type="scene", entity_name=scene_name, view_role="establishing",
            purposes=[PURPOSE_KEYFRAME_SEED, PURPOSE_QA_ANCHOR],
        )]
    except OSError:
        return []
