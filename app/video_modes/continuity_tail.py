"""跨镜连续性尾帧装配与冗余惩罚、参考选择终裁。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.db import new_id
from app.schemas import Bible, EpisodeScreenplay, Shot

from .asset_lookup import _asset_from_path, reference_image_path
from .mode_selection import (
    ReferenceImageAsset,
    _MAX_REDUNDANCY_PENALTY,
    _reference_runtime_blocking,
    max_character_reference_images,
)
from .reference_assemble import _enforce_reference_consistency
from .reference_generate import previous_tail_reference_asset
from .seedance_pack import _dedupe_assets



async def assemble_continuity_tail(
    *, conn: Any, project_id: str, episode_no: int, episode_id: str, shot_id: str,
    shot: Shot, bible: Bible, meta: dict[str, Any], prev_shot: Any | None,
    rejection_details: list[dict[str, Any]] | None = None,
    rejected_out: list[ReferenceImageAsset] | None = None,
    screenplay: EpisodeScreenplay | None = None,
) -> list[ReferenceImageAsset]:
    """尾帧到达后：装配连续性参考并做最终组门禁；不重跑已通过静态槽位。"""
    from app.multiview import PURPOSE_VIDEO_INPUT, purpose_list

    refs = list(meta.get("reference_images") or [])
    selected: list[ReferenceImageAsset] = []
    for ref in refs:
        path = ref.get("path") or ref.get("image_path")
        url = str(ref.get("url") or "")
        if path and not Path(path).is_file():
            continue
        if not path and not url.startswith("data:image/"):
            continue
        if ref.get("type") == "previous_shot_frame":
            continue  # 用最新尾帧替换
        if path:
            asset = _asset_from_path(
                path=path,
                ref_type=ref.get("type") or "plot_key_frame",
                source=ref.get("source") or "pipeline",
                shot_id=ref.get("shotId") or shot_id,
                episode_id=ref.get("episodeId") or episode_id,
                quality_score=ref.get("qualityScore"),
                qa=ref.get("qa"),
                related_character_ids=list(ref.get("relatedCharacterIds") or []),
                entity_type=ref.get("entity_type"),
                entity_name=ref.get("entity_name"),
                library_revision_id=ref.get("library_revision_id"),
                library_view_id=ref.get("library_view_id"),
                view_role=ref.get("view_role"),
                purposes=purpose_list(ref),
                required=bool(ref.get("required")),
                slot_key=ref.get("slot_key"),
            )
        else:
            asset = ReferenceImageAsset(
                id=ref.get("id") or new_id("ref"),
                url=url,
                type=ref.get("type") or "plot_key_frame",
                source=ref.get("source") or "pipeline",
                shotId=ref.get("shotId") or shot_id,
                episodeId=ref.get("episodeId") or episode_id,
                relatedCharacterIds=list(ref.get("relatedCharacterIds") or []),
                qualityScore=ref.get("qualityScore"),
                qa=ref.get("qa"),
                entity_type=ref.get("entity_type"),
                entity_name=ref.get("entity_name"),
                library_revision_id=ref.get("library_revision_id"),
                library_view_id=ref.get("library_view_id"),
                view_role=ref.get("view_role"),
                purposes=purpose_list(ref),
                required=bool(ref.get("required")),
                slot_key=ref.get("slot_key"),
            )
        asset.id = ref.get("id") or asset.id
        asset.selectedForSeedance = bool(ref.get("selectedForSeedance"))
        asset.deleted = bool(ref.get("deleted"))
        asset.rejectReason = ref.get("rejectReason")
        asset.dependency_manifest = ref.get("dependency_manifest")
        asset.prompt_contract_version = ref.get("prompt_contract_version")
        asset.keyframe_contract_fingerprint = ref.get("keyframe_contract_fingerprint")
        asset.candidate_no = ref.get("candidate_no")
        asset.keyframe_index = ref.get("keyframe_index")
        asset.keyframe_total = ref.get("keyframe_total")
        asset.keyframe_time_ratio = ref.get("keyframe_time_ratio")
        asset.keyframe_target_desc = ref.get("keyframe_target_desc")

        # 静态参考图可能在等待上一镜尾帧期间被人工废弃，QA 淘汰图也会
        # 保留 video_input 用途供审计。两者都只能留在废弃画廊，不能参与
        # 连续性重装配，否则后续门禁可能把高分旧候选重新标成 selected。
        stale_video_candidate = (
            PURPOSE_VIDEO_INPUT in asset.purposes and not asset.selectedForSeedance
        )
        structural_reject = _reference_runtime_blocking(asset)
        if asset.deleted or structural_reject or stale_video_candidate:
            asset.selectedForSeedance = False
            if rejected_out is not None and asset not in rejected_out:
                rejected_out.append(asset)
            continue
        selected.append(asset)

    prev = prev_shot
    if prev is None and int(getattr(shot, "shot_no", 0) or 0) > 1:
        prev = conn.execute(
            "SELECT * FROM shots WHERE episode_id=? AND shot_no=?",
            (episode_id, int(shot.shot_no) - 1)).fetchone()
    if prev is not None:
        ref_dir = reference_image_path(project_id, episode_no, shot.shot_no, "previous_shot_frame", 0).parent
        tail = previous_tail_reference_asset(conn, prev, dest_dir=ref_dir)
        if tail:
            selected = [tail] + [a for a in selected if a.type != "previous_shot_frame"]

    selected = await _enforce_reference_consistency(
        selected=selected, shot=shot, bible=bible, project_id=project_id, episode_no=episode_no,
        rejection_details=rejection_details, rejected_out=rejected_out,
        screenplay=screenplay,
    )
    selected = _dedupe_assets(selected)
    selected = _finalize_reference_selection(
        selected, rejected_out=rejected_out, rejection_details=rejection_details)
    for asset in selected:
        asset.selectedForSeedance = True
        asset.shotId = shot_id
        asset.episodeId = episode_id
    return selected


def _apply_redundancy_penalties(assets: list[ReferenceImageAsset]) -> None:
    """对超额含人物图按类型优先级降低排序位置；不直接踢出，只影响装箱时的先后顺序。

    VLM 图片质检已下线：``qualityScore`` 不再是质量分（生成成功即恒为 1.0），这里
    把它复用成"冗余优先级"排序键——超出配额的图片分数被扣低，装箱阶段自然让位
    给配额内的图片，行为与原来"综合分里叠一层冗余惩罚"等价，只是不再有 QA 分量。
    """
    # 时序关键帧虽然含人，但它们是不同剧情时刻，不是多张重复定妆照；
    # 不得消耗 character 配额或因帧数增加而被逐张扣分。
    char_refs = [a for a in assets if a.type == "character"]
    if len(char_refs) <= 1:
        return

    def _rank_key(a: ReferenceImageAsset) -> tuple:
        # 与旧优先级对齐：尾帧 > 生成图 > 定妆照/其他
        if a.type == "previous_shot_frame":
            pri = 0
        elif a.source == "seedream_generated":
            pri = 1
        else:
            pri = 2
        return pri

    ranked = sorted(char_refs, key=_rank_key)
    distinct_identities = {
        str(asset.entity_name or "").strip()
        or next((str(name).strip() for name in asset.relatedCharacterIds if str(name).strip()), "")
        for asset in char_refs
    }
    distinct_identities.discard("")
    # One anchor per distinct named identity is required evidence, not
    # redundant imagery. Only extra views beyond that baseline are penalized.
    prefer = max(max_character_reference_images(), len(distinct_identities))
    for i, a in enumerate(ranked):
        if i < prefer:
            continue
        excess = i - prefer + 1
        penalty = min(_MAX_REDUNDANCY_PENALTY, 0.05 * excess)
        a.qualityScore = max(0.0, (a.qualityScore if a.qualityScore is not None else 1.0) - penalty)


def _finalize_reference_selection(
    assets: list[ReferenceImageAsset],
    *,
    rejected_out: list[ReferenceImageAsset] | None = None,
    rejection_details: list[dict[str, Any]] | None = None,
) -> list[ReferenceImageAsset]:
    """技术产物存在即可用：全部技术有效参考图保留，按冗余优先级排序装箱顺序。"""
    del rejected_out, rejection_details
    if not assets:
        return []
    _apply_redundancy_penalties(assets)
    for asset in assets:
        asset.selectedForSeedance = True
        asset.rejectReason = None
    return sorted(assets, key=lambda a: a.qualityScore or 0.0, reverse=True)
