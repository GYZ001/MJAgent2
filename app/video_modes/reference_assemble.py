"""参考资产一致性校验与基于图库素材的整体装配、对外统一入口 build_reference_assets。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from app.schemas import Bible, EpisodeScreenplay, Shot

from .asset_lookup import _asset_from_path, character_reference_assets, scene_reference_assets
from .mode_selection import (
    REFERENCE_INPUT_POLICY_VERSION,
    ReferenceImageAsset,
    ShotVideoModeDecision,
    _dedupe_str,
    max_reference_images,
)
from .seedance_pack import _dedupe_assets



async def _enforce_reference_consistency(*, selected: list[ReferenceImageAsset], shot: Shot, bible: Bible,
                                         project_id: str, episode_no: int,
                                         rejection_details: list[dict[str, Any]] | None = None,
                                         rejected_out: list[ReferenceImageAsset] | None = None,
                                         screenplay: EpisodeScreenplay | None = None,
                                         ) -> list[ReferenceImageAsset]:
    """VLM 参考图一致性质检已下线：不再跨候选比对锚点、不再触发漂移重生。

    技术产物存在即可用——已生成的候选原样放行，去留交给人工在成片里判断。
    保留全部形参与返回类型不变，使调用方无需改动；``rejection_details``/
    ``rejected_out`` 不再被写入（没有一致性检查就没有可报告的一致性拒绝理由）。
    """
    del project_id, episode_no, rejection_details, rejected_out, shot, bible, screenplay
    return selected


async def _build_library_reference_assets(
    *,
    conn: Any,
    project_id: str,
    episode_no: int,
    episode_id: str,
    shot_id: str,
    shot: Shot,
    bible: Bible,
    on_progress: Callable[
        [list[ReferenceImageAsset], list[ReferenceImageAsset]], None
    ] | None = None,
    existing_meta: dict[str, Any] | None = None,
    screenplay: EpisodeScreenplay | None = None,
) -> list[ReferenceImageAsset]:
    """Resolve existing character/scene-library images without generating media."""
    from app.continuity import effective_characters_visible
    from app.multiview import (
        PURPOSE_QA_ANCHOR,
        PURPOSE_VIDEO_INPUT,
        assert_manifest_allows_production,
        library_anchor_assets_from_manifest,
        resolve_shot_asset_dependencies,
    )

    meta = existing_meta if existing_meta is not None else {}
    scene_name = str(getattr(shot, "scene_name", "") or "").strip()
    visible_names = effective_characters_visible(shot)
    bible_names = {character.name for character in bible.characters}
    if screenplay is not None and screenplay.narrative_plan is not None:
        from app.identity_contracts import narrative_identity_resolver

        resolver = narrative_identity_resolver(bible, screenplay)
        identity_names = list(dict.fromkeys(
            identity.asset_name
            for identity in (
                resolver.resolve(name, usage="visual") for name in visible_names
            )
            if identity.allows_asset
        ))
    else:
        identity_names = [name for name in visible_names if name in bible_names]

    manifest = resolve_shot_asset_dependencies(
        project_id=project_id,
        episode_no=episode_no,
        shot_id=shot_id,
        shot=shot,
        scene_name=scene_name or None,
        conn=conn,
        bible=bible,
        screenplay=screenplay,
    )
    warnings = assert_manifest_allows_production(manifest)
    if warnings:
        meta["asset_manifest_gate_retry_exhausted"] = True
        meta["asset_manifest_warnings"] = list(warnings)

    assets: list[ReferenceImageAsset] = []
    for anchor in library_anchor_assets_from_manifest(manifest):
        entity_type = str(anchor.get("entity_type") or anchor.get("type") or "")
        if entity_type not in {"character", "scene"}:
            continue
        path = str(anchor.get("image_path") or "").strip()
        if not path or not Path(path).is_file():
            continue
        try:
            assets.append(_asset_from_path(
                path=path,
                ref_type=entity_type,
                source="asset_library",
                related_character_ids=(
                    [str(anchor.get("entity_name"))]
                    if entity_type == "character" and anchor.get("entity_name")
                    else None
                ),
                qa={"status": "library", "overall": None, "issues": []},
                entity_type=entity_type,
                entity_name=anchor.get("entity_name"),
                library_revision_id=anchor.get("library_revision_id"),
                library_view_id=anchor.get("library_view_id"),
                view_role=anchor.get("view_role"),
                purposes=[PURPOSE_QA_ANCHOR],
            ))
        except OSError:
            continue

    if not any(asset.entity_type == "character" for asset in assets):
        assets.extend(character_reference_assets(
            bible,
            identity_names,
            limit=max(1, len(identity_names)),
            project_id=project_id,
            episode_no=episode_no, shot=shot,
        ))
    if not any(asset.entity_type == "scene" for asset in assets):
        assets.extend(scene_reference_assets(
            bible,
            scene_name,
            project_id=project_id,
            episode_no=episode_no,
        ))
    assets = [
        asset for asset in _dedupe_assets(assets)
        if (asset.entity_type or asset.type) in {"character", "scene"}
        and asset.source == "asset_library"
    ]

    role_priority = {
        "front_full": 0,
        "three_quarter": 1,
        "profile": 2,
        "side_full": 2,
        "action_zone": 0,
        "establishing": 1,
        "reverse_angle": 2,
    }

    def _rank(asset: ReferenceImageAsset) -> tuple[int, int, str]:
        kind = asset.entity_type or asset.type
        kind_rank = 0 if kind == "character" else 1
        return (
            kind_rank,
            role_priority.get(str(asset.view_role or ""), 9),
            asset.path or asset.id,
        )

    selected: list[ReferenceImageAsset] = []
    selected_names: set[str] = set()
    for asset in sorted(assets, key=_rank):
        if len(selected) >= max_reference_images():
            break
        if (asset.entity_type or asset.type) != "character":
            continue
        name = str(asset.entity_name or "").strip()
        if identity_names and name not in identity_names:
            continue
        key = name or "|".join(asset.relatedCharacterIds)
        if key in selected_names:
            continue
        selected_names.add(key)
        selected.append(asset)
    scene_asset = next(
        (
            asset for asset in sorted(assets, key=_rank)
            if (asset.entity_type or asset.type) == "scene"
        ),
        None,
    )
    if scene_asset is not None and len(selected) < max_reference_images():
        selected.append(scene_asset)

    selected_ids = {id(asset) for asset in selected}
    for asset in assets:
        asset.shotId = shot_id
        asset.episodeId = episode_id
        asset.required = id(asset) in selected_ids
        asset.selectedForSeedance = id(asset) in selected_ids
        asset.purposes = _dedupe_str([
            *(asset.purposes or []),
            PURPOSE_QA_ANCHOR,
            *([PURPOSE_VIDEO_INPUT] if id(asset) in selected_ids else []),
        ])

    meta.update({
        "reference_input_policy_version": REFERENCE_INPUT_POLICY_VERSION,
        "reference_manifest": manifest,
        "reference_manifest_frozen": True,
        "reference_slots": {},
        "keyframe_sequence": {"beats": [], "beat_count": 0},
        "narrative_keyframe_missing": False,
    })
    for key in (
        "keyframe_fallback_mode",
        "keyframe_structural_fallback_slots",
        "keyframe_contract_fingerprint",
    ):
        meta.pop(key, None)
    if on_progress is not None:
        on_progress(list(assets), [])
    return assets if selected else []


async def build_reference_assets(*, conn: Any, project_id: str, episode_no: int, episode_id: str,
                                 shot_id: str, shot: Shot, bible: Bible,
                                 decision: ShotVideoModeDecision, prev_shot: Any | None = None,
                                 rejection_details: list[dict[str, Any]] | None = None,
                                 rejected_out: list[ReferenceImageAsset] | None = None,
                                 on_progress: Callable[
                                     [list[ReferenceImageAsset], list[ReferenceImageAsset]], None
                                 ] | None = None,
                                 allow_missing_continuity_tail: bool = False,
                                 job_id: str | None = None,
                                 existing_meta: dict[str, Any] | None = None,
                                 screenplay: EpisodeScreenplay | None = None) -> list[ReferenceImageAsset]:
    return await _build_library_reference_assets(
        conn=conn,
        project_id=project_id,
        episode_no=episode_no,
        episode_id=episode_id,
        shot_id=shot_id,
        shot=shot,
        bible=bible,
        on_progress=on_progress,
        existing_meta=existing_meta,
        screenplay=screenplay,
    )
