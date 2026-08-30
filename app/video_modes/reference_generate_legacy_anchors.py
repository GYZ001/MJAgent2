"""Reusable-anchor selection and keyframe-sequence planning for the legacy
per-shot reference-asset build (see ``reference_generate_legacy.py``'s
module docstring for the full phase map). Moved verbatim out of the
pre-split single function -- only the wrapping into named phase functions,
and reading/writing through ``state`` instead of bare locals, is new.
"""
from __future__ import annotations

import hashlib
import json

from typing import Any

from .keyframe_contract import narrative_keyframe_beats, timeline_keyframe_plan
from .mode_selection import (
    KEYFRAME_PROMPT_CONTRACT_VERSION,
    REFERENCE_IMAGE_MODE,
    ReferenceImageAsset,
    _dedupe_str,
    max_character_reference_images,
)
from .reference_generate_legacy_state import _ReferenceBuildState


def _anchor_rank(asset: ReferenceImageAsset) -> tuple[int, int, float, str]:
    role_priority = {
        "front_full": 0,
        "three_quarter": 1,
        "profile": 2,
        "side_full": 2,
        "action_zone": 0,
        "establishing": 1,
        "reverse_angle": 2,
    }
    kind = asset.entity_type or asset.type
    kind_rank = 0 if kind == "character" else (1 if kind == "scene" else 2)
    try:
        score = float(asset.qualityScore) if asset.qualityScore is not None else 0.0
    except (TypeError, ValueError):
        score = 0.0
    return (kind_rank, role_priority.get(str(asset.view_role or ""), 9), -score, asset.path or asset.id)


def _select_reference_anchors(state: _ReferenceBuildState) -> None:
    """Reserve continuity/keyframe slots and pick person/scene anchors up to budget."""
    from app.multiview import narrative_keyframe_required

    state.selected = list(state.forced)

    # Seedance 最终输入不再只有生成关键帧：人物定妆与场景定场各至少预留一席，
    # 其余容量也不会驱动多生成关键帧；时序关键帧由 1–2 帧策略独立控制。
    # 人物只取各身份的首选视角，避免多张定妆照诱发分身。
    # 即使上一镜尾帧尚未到达，action_continuation 也先预留该席，保证恢复前后时序计划稳定。
    state.continuity_slot_reserve = 1 if state.needs_tail else 0
    keyframe_slot_reserve = 1 if state.decision.mode == REFERENCE_IMAGE_MODE and narrative_keyframe_required() else 0
    anchor_budget = max(0, state.max_refs - state.continuity_slot_reserve - keyframe_slot_reserve)

    _select_character_and_scene_anchors(state, anchor_budget)
    _finalize_reference_anchors(state)


def _select_character_and_scene_anchors(state: _ReferenceBuildState, anchor_budget: int) -> None:
    """Fill ``state.video_anchor_assets`` with one anchor per visible identity, then a scene anchor."""
    seen_anchor_characters: set[str] = set()
    # The setting caps redundant views of one identity, not the number of
    # distinct named people. Every visible named identity gets one anchor when
    # capacity allows; otherwise later characters silently lose their outfit and
    # body-scale evidence at the paid video boundary.
    character_anchor_limit = max(
        max_character_reference_images(), len(state.identity_character_names),
    )
    for asset in sorted(state.evidence_assets, key=_anchor_rank):
        if len(state.video_anchor_assets) >= anchor_budget:
            break
        kind = asset.entity_type or asset.type
        if kind != "character" or len(seen_anchor_characters) >= character_anchor_limit:
            continue
        character_key = str(asset.entity_name or "") or "|".join(asset.relatedCharacterIds) or asset.path or asset.id
        if character_key in seen_anchor_characters:
            continue
        seen_anchor_characters.add(character_key)
        state.video_anchor_assets.append(asset)
    if len(state.video_anchor_assets) < anchor_budget:
        scene_anchor = next(
            (
                asset for asset in sorted(state.evidence_assets, key=_anchor_rank)
                if (asset.entity_type or asset.type) == "scene" and asset not in state.video_anchor_assets
            ),
            None,
        )
        if scene_anchor is not None:
            state.video_anchor_assets.append(scene_anchor)


def _finalize_reference_anchors(state: _ReferenceBuildState) -> None:
    """Mark the selected anchors as required video inputs and size the generated-slot budget."""
    from app.multiview import PURPOSE_QA_ANCHOR, PURPOSE_VIDEO_INPUT

    for asset in state.video_anchor_assets:
        asset.purposes = _dedupe_str([*(asset.purposes or []), PURPOSE_VIDEO_INPUT, PURPOSE_QA_ANCHOR])
        asset.required = True
        asset.selectedForSeedance = True
    state.selected.extend(state.video_anchor_assets)

    state.available_generated_slots = max(
        0,
        state.max_refs - state.continuity_slot_reserve - len(state.video_anchor_assets),
    )


def _plan_keyframe_sequence(state: _ReferenceBuildState) -> None:
    """Plan the temporal-beat count and fingerprint the keyframe sequence.

    Clears stale slot state when the sequence fingerprint (slot count,
    anchors or beat timing) has changed since the last attempt.
    """
    state.keyframe_plan = timeline_keyframe_plan(state.shot)
    if state.decision.mode != REFERENCE_IMAGE_MODE:
        state.generated_needed = 0
    elif state.decision.defaulted:
        state.generated_needed = min(int(state.keyframe_plan["count"]), state.available_generated_slots)
    else:
        state.generated_needed = min(state.want_gen, state.available_generated_slots)

    temporal_beat_count = state.generated_needed if state.decision.defaulted else (1 if state.generated_needed else 0)
    state.temporal_beats = narrative_keyframe_beats(state.shot, temporal_beat_count) if temporal_beat_count else []
    state.beat_by_slot = {str(beat["slot_key"]): beat for beat in state.temporal_beats}

    state.sequence_material = {
        "policy_version": KEYFRAME_PROMPT_CONTRACT_VERSION,
        "max_images": state.max_refs,
        "continuity_reserved": bool(state.needs_tail),
        "keyframe_plan": state.keyframe_plan,
        "anchor_keys": [
            {
                "entity_type": asset.entity_type or asset.type,
                "entity_name": asset.entity_name,
                "library_revision_id": asset.library_revision_id,
                "library_view_id": asset.library_view_id,
                "path": asset.path,
            }
            for asset in state.video_anchor_assets
        ],
        "beats": state.temporal_beats,
    }
    sequence_fingerprint = _fingerprint_sequence_material(state.sequence_material)
    prior_sequence = state.existing_meta.get("keyframe_sequence")
    prior_sequence_fingerprint = str(
        prior_sequence.get("fingerprint") if isinstance(prior_sequence, dict) else ""
    )
    if prior_sequence_fingerprint and prior_sequence_fingerprint != sequence_fingerprint:
        # 席位数/锚点/时间点改变时，旧 winner 不再是同一个冻结节拍。
        state.slot_state.clear()
        state.existing_meta["reference_slots"] = state.slot_state
    state.existing_meta["keyframe_sequence"] = {
        **state.sequence_material,
        "fingerprint": sequence_fingerprint,
        "beat_count": len(state.temporal_beats),
        "reserved_input_count": state.continuity_slot_reserve + len(state.video_anchor_assets),
    }


def _fingerprint_sequence_material(sequence_material: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(sequence_material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
