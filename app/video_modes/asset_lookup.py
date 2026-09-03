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
                     required: bool = False, slot_key: str | None = None) -> ReferenceImageAsset:
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


def character_reference_assets(bible: Bible, character_names: list[str], *, limit: int,
                               project_id: str | None = None,
                               episode_no: int | None = None,
                               shot: Any | None = None) -> list[ReferenceImageAsset]:
    """人物库图作为 keyframe_seed + qa_anchor；默认不直接 video_input。"""
    from app.multiview import (
        PURPOSE_KEYFRAME_SEED, PURPOSE_QA_ANCHOR, portrait_views_for_episode,
        character_multiview_enabled,
    )
    assets: list[ReferenceImageAsset] = []
    by_name, time_anchor = {c.name: c for c in bible.characters}, _shot_time_anchor(shot)
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
            path = preferred.get("image_path")
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
            if not path or not Path(path).exists():
                continue
            try:
                assets.append(_asset_from_path(
                    path=path,
                    ref_type="character",
                    source="asset_library",
                    related_character_ids=[name],
                    quality_score=score,
                    qa=qa or {"status": "unverified", "overall": None, "issues": ["人物库图缺少 QA"]},
                    entity_type="character",
                    entity_name=name,
                    library_revision_id=preferred.get("portrait_id"),
                    library_view_id=preferred.get("id"),
                    view_role=preferred.get("view_role"),
                    purposes=[PURPOSE_KEYFRAME_SEED, PURPOSE_QA_ANCHOR],
                ))
            except OSError:
                continue
            continue
        # 回退单图
        path = None
        if c is not None and project_id is not None:
            from app.portraits.portrait_lookup import portrait_lookup_for_episode
            path = portrait_lookup_for_episode(project_id, name, episode_no, time_anchor=time_anchor)["image_path"]
        if not path:
            path = getattr(c, "ref_image_path", None) if c else None
        if not path or not Path(path).exists():
            continue
        try:
            assets.append(_asset_from_path(
                path=path,
                ref_type="character",
                source="asset_library",
                related_character_ids=[name],
                quality_score=None,
                qa={"status": "unverified", "overall": None, "issues": ["旧单图无分项 QA"]},
                entity_type="character",
                entity_name=name,
                view_role="front_full",
                purposes=[PURPOSE_KEYFRAME_SEED, PURPOSE_QA_ANCHOR],
            ))
        except OSError:
            continue
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
