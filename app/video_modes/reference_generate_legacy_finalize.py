"""Cross-shot consistency enforcement and final gallery assembly for the
legacy per-shot reference-asset build (see ``reference_generate_legacy.py``'s
module docstring for the full phase map): enforcing relative consistency and
timeline-keyframe invariance across the selected ``video_input`` candidates,
falling back to a single decisive keyframe when invariance fails
(``_enforce_cross_shot_consistency``); then deduping, gating on the
required-keyframe presence, and assembling the final gallery returned to the
caller (``_assemble_final_gallery``). Moved verbatim out of the pre-split
single function -- only the wrapping into named phase functions, and
reading/writing through ``state`` instead of bare locals, is new.

``video_candidates`` is reassigned several times as it moves through
consistency/invariance/dedupe/finalize -- each reassignment stays local to
these two functions (nothing outside them reads it), so it is plain local
rebinding, not a ``state`` field.
"""
from __future__ import annotations

import hashlib
import json

from pathlib import Path

from .keyframe_contract import narrative_keyframe_beats
from .mode_selection import (
    KEYFRAME_PROMPT_CONTRACT_VERSION,
    KEYFRAME_STRUCTURAL_FALLBACK_MODE,
    ReferenceImageAsset,
)
from .reference_assemble import _enforce_reference_consistency, _enforce_timeline_keyframe_invariance
from .reference_generate_legacy_state import _ReferenceBuildState
from .seedance_pack import _dedupe_assets
from .continuity_tail import _finalize_reference_selection


async def _enforce_cross_shot_consistency(state: _ReferenceBuildState) -> list[ReferenceImageAsset]:
    """Enforce relative consistency and timeline-keyframe invariance on the winners.

    Falls back to a single decisive keyframe (dropping the second beat) when
    invariance cannot be proven, and re-fingerprints the sequence to match.
    Returns ``video_candidates`` for ``_assemble_final_gallery``.
    """
    from app.multiview import PURPOSE_VIDEO_INPUT

    # Phase 2：整组相对一致性检查（仅对 video_input 候选）
    if state.job_id:
        try:
            from app.media_pipeline import stages as media_stages
            from app.media_pipeline.stage_state import set_pipeline_stage
            set_pipeline_stage(state.job_id, media_stages.STAGE_REFERENCE_CONSISTENCY)
        except Exception:  # noqa: BLE001
            pass
    video_candidates = [a for a in state.selected if PURPOSE_VIDEO_INPUT in (a.purposes or []) or a.type == "previous_shot_frame"]
    video_candidates = await _enforce_reference_consistency(
        selected=video_candidates, shot=state.shot, bible=state.bible, project_id=state.project_id, episode_no=state.episode_no,
        rejection_details=state.rejection_details, rejected_out=state.rejected_out,
        screenplay=state.screenplay)
    video_candidates, invariant_dropped_slots = await _enforce_timeline_keyframe_invariance(
        selected=video_candidates,
        shot=state.shot,
        bible=state.bible,
        rejection_details=state.rejection_details,
        rejected_out=state.rejected_out,
        screenplay=state.screenplay,
    )
    if invariant_dropped_slots:
        # 两帧无法证明人物不变量时，回退为单一决定性关键帧，并同步冻结元数据；
        # 不能只在装箱时偷偷少传一张，否则恢复链路会把已删除的辅助帧当缺失重建。
        _fallback_to_single_keyframe(state, video_candidates, invariant_dropped_slots)
    return video_candidates


def _fallback_to_single_keyframe(
    state: _ReferenceBuildState,
    video_candidates: list[ReferenceImageAsset],
    invariant_dropped_slots: list[str],
) -> None:
    """Collapse the keyframe sequence to one beat after an invariance failure."""
    state.temporal_beats = narrative_keyframe_beats(state.shot, 1)
    state.beat_by_slot = {str(beat["slot_key"]): beat for beat in state.temporal_beats}
    master_beat = state.temporal_beats[0]
    for asset in video_candidates:
        if asset.slot_key == "narrative_keyframe":
            asset.keyframe_index = 1
            asset.keyframe_total = 1
            asset.keyframe_time_ratio = float(master_beat["time_ratio"])
            asset.keyframe_target_desc = str(master_beat["target_desc"])
            asset.qa = {**(asset.qa or {}), "keyframe_beat": dict(master_beat)}
    for dropped_slot in invariant_dropped_slots:
        raw_slot = state.slot_state.get(dropped_slot)
        if not isinstance(raw_slot, dict):
            continue
        records = []
        for raw_record in raw_slot.get("candidates") or []:
            if not isinstance(raw_record, dict):
                continue
            records.append({
                **raw_record,
                "status": "discarded_deleted",
                "path": None,
            })
        state.slot_state[dropped_slot] = {
            **raw_slot,
            "status": "excluded_cross_frame_identity_drift",
            "path": None,
            "candidates": records,
        }
    if isinstance(state.slot_state.get("narrative_keyframe"), dict):
        state.slot_state["narrative_keyframe"]["keyframe_beat"] = dict(master_beat)
    state.sequence_material["beats"] = state.temporal_beats
    state.sequence_material["keyframe_plan"] = {
        **state.keyframe_plan,
        "count": 1,
        "reason": "cross_frame_identity_invariance_fallback",
        "requested_count": state.keyframe_plan["count"],
    }
    sequence_fingerprint = hashlib.sha256(
        json.dumps(
            state.sequence_material, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    state.existing_meta["keyframe_sequence"] = {
        **state.sequence_material,
        "fingerprint": sequence_fingerprint,
        "beat_count": 1,
        "reserved_input_count": state.continuity_slot_reserve + len(state.video_anchor_assets),
    }
    state.existing_meta["reference_slots"] = state.slot_state


def _assemble_final_gallery(
    state: _ReferenceBuildState,
    video_candidates: list[ReferenceImageAsset],
) -> list[ReferenceImageAsset]:
    """Dedupe, gate on required-keyframe presence, and assemble the final gallery."""
    from app.multiview import PURPOSE_VIDEO_INPUT

    video_candidates = _dedupe_assets(video_candidates)
    video_candidates = _finalize_reference_selection(
        video_candidates, rejected_out=state.rejected_out, rejection_details=state.rejection_details)

    _gate_required_keyframe_presence(state, video_candidates)

    for asset in video_candidates:
        if PURPOSE_VIDEO_INPUT not in (asset.purposes or []):
            asset.purposes = list(asset.purposes or []) + [PURPOSE_VIDEO_INPUT]
        asset.selectedForSeedance = True
        asset.shotId = asset.shotId or state.shot_id
        asset.episodeId = asset.episodeId or state.episode_id

    return _build_final_gallery(state, video_candidates)


def _gate_required_keyframe_presence(
    state: _ReferenceBuildState,
    video_candidates: list[ReferenceImageAsset],
) -> None:
    """Gate on every required narrative-keyframe slot having a valid delivered candidate.

    QA 只评分：必需关键帧只做技术/结构门禁，VLM 低分或未评分不伪装成文件缺失。
    """
    from app.multiview import PURPOSE_VIDEO_INPUT, narrative_keyframe_required

    valid_keyframe_slots = {
        str(a.slot_key or "")
        for a in video_candidates
        if a.type == "plot_key_frame"
        and not a.deleted
        and a.selectedForSeedance
        and PURPOSE_VIDEO_INPUT in (a.purposes or [])
        and (
            bool(a.path and Path(a.path).is_file())
            or str(a.url or "").startswith("data:image")
        )
    }
    expected_keyframe_slots = {
        str(beat.get("slot_key") or "")
        for beat in state.temporal_beats
        if str(beat.get("slot_key") or "")
    }
    fallback_slots = {
        str(slot or "").strip()
        for slot in (state.existing_meta.get("keyframe_structural_fallback_slots") or [])
        if str(slot or "").strip()
    }
    structural_fallback = (
        state.existing_meta.get("keyframe_fallback_mode") == KEYFRAME_STRUCTURAL_FALLBACK_MODE
        and bool(fallback_slots)
        and fallback_slots.issubset(expected_keyframe_slots)
    )
    required_keyframe_slots = (
        expected_keyframe_slots - fallback_slots if structural_fallback else expected_keyframe_slots
    )
    has_keyframe = (
        required_keyframe_slots.issubset(valid_keyframe_slots)
        if expected_keyframe_slots
        else bool(valid_keyframe_slots)
    )
    if narrative_keyframe_required() and not has_keyframe:
        state.existing_meta["narrative_keyframe_missing"] = True
        state.existing_meta["reference_group_gate_passed"] = False
    else:
        state.existing_meta["narrative_keyframe_missing"] = False


def _build_final_gallery(
    state: _ReferenceBuildState,
    video_candidates: list[ReferenceImageAsset],
) -> list[ReferenceImageAsset]:
    """Merge evidence anchors into the gallery and persist the final build metadata."""
    from app.multiview import PURPOSE_VIDEO_INPUT

    # 合并证据锚点进画廊（不选中为 video_input，除非显式加入）
    gallery = _dedupe_assets(list(video_candidates) + list(state.evidence_assets))
    for asset in gallery:
        asset.shotId = asset.shotId or state.shot_id
        asset.episodeId = asset.episodeId or state.episode_id
        if PURPOSE_VIDEO_INPUT not in (asset.purposes or []) and asset.type != "previous_shot_frame":
            asset.selectedForSeedance = False
    if state.rejected_out is not None:
        for asset in state.rejected_out:
            asset.selectedForSeedance = False
            asset.shotId = asset.shotId or state.shot_id
            asset.episodeId = asset.episodeId or state.episode_id
    if state.existing_meta is not None:
        state.existing_meta["reference_slots"] = state.slot_state
        state.existing_meta["reference_manifest"] = state.manifest
        state.existing_meta["reference_manifest_frozen"] = True
        state.existing_meta["keyframe_prompt_contract_version"] = KEYFRAME_PROMPT_CONTRACT_VERSION
        state.existing_meta["keyframe_contract_fingerprint"] = state.current_keyframe_fingerprint
    state.publish_progress()
    return gallery
