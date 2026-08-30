"""Candidate image generation and QA review for the legacy per-shot
reference-asset build (see ``reference_generate_legacy.py``'s module
docstring for the full phase map): generating every missing candidate
concurrently (``_generate_candidates``); flagging slots whose candidates all
failed (``_flag_empty_candidate_slots``); and reviewing every unreviewed
candidate (``_review_candidates`` -- VLM review is disabled, so this is a
technical-readability gate that scores the rest at 1.0; see its body for the
inline note). Moved verbatim out of the pre-split single function -- only
the wrapping into named phase functions, and reading/writing through
``state`` instead of bare locals, is new.
"""
from __future__ import annotations

import asyncio

from pathlib import Path
from typing import Any

from app import hiagent

from .keyframe_contract import _shot_for_keyframe_beat, is_narrative_keyframe_slot
from .mode_selection import (
    KEYFRAME_PROMPT_CONTRACT_VERSION,
    ReferenceImageAsset,
    _MULTI_KEYFRAME_INVARIANCE_NOTE,
    _screenplay_call_kwargs,
    reference_gen_retries,
)
from .reference_generate import _generate_reference_keep_best
from .reference_generate_legacy_state import _ReferenceBuildState


async def _run_candidate(
    state: _ReferenceBuildState,
    slot_key: str,
    ref_type: str,
    override: str | None,
    candidate_no: int,
    generation_index: int,
) -> tuple[str, str, int, ReferenceImageAsset | None, list[ReferenceImageAsset], list[dict[str, Any]]]:
    beat_shot = _shot_for_keyframe_beat(state.shot, state.beat_by_slot.get(slot_key))
    invariance_note = _MULTI_KEYFRAME_INVARIANCE_NOTE if len(state.temporal_beats) > 1 else None
    extra_instruction = " ".join(
        part for part in (state.seed_order_note, invariance_note) if part
    ) or None
    asset, discarded, rej = await _generate_reference_keep_best(
        project_id=state.project_id,
        episode_no=state.episode_no,
        shot=beat_shot,
        bible=state.bible,
        ref_type=ref_type,
        index=generation_index,
        content_override=override,
        retries=reference_gen_retries(),
        seed_inputs=state.seeds_for(ref_type),
        extra_instruction=extra_instruction if ref_type == "plot_key_frame" else None,
        skip_inline_qa=True,
        **_screenplay_call_kwargs(state.screenplay),
    )
    return slot_key, ref_type, candidate_no, asset, discarded, rej


async def _generate_candidates(state: _ReferenceBuildState) -> None:
    """Generate every still-missing candidate concurrently, checkpointing as each lands."""
    if not state.specs:
        return
    generation_tasks = _build_generation_tasks(state)
    for completed in asyncio.as_completed(generation_tasks):
        slot_key, ref_type, candidate_no, asset, discarded, rej = await completed
        _apply_one_generation_result(state, slot_key, candidate_no, asset, discarded, rej)
        attempted = len(state.candidate_pool.get(slot_key, [])) + len(state.candidate_audit_records.get(slot_key, {}))
        status = "qa_pending" if attempted >= state.candidate_targets.get(slot_key, 1) else "generating_candidates"
        state.checkpoint_candidates(slot_key, status)
        state.publish_progress()


def _build_generation_tasks(state: _ReferenceBuildState) -> list[Any]:
    """Schedule one generation task per still-missing candidate slot."""
    generation_tasks = []
    for slot_key, ref_type, override, ordinal in state.specs:
        existing_nos = {no for no, _asset in state.candidate_pool.get(slot_key, [])}
        # 5 是候选数上限；不同 slot/candidate 的产物索引永不冲突。
        for candidate_no in range(1, state.candidate_targets.get(slot_key, 1) + 1):
            if candidate_no in existing_nos:
                continue
            generation_index = ordinal * 5 + candidate_no
            generation_tasks.append(asyncio.create_task(_run_candidate(
                state, slot_key, ref_type, override, candidate_no, generation_index,
            )))
    return generation_tasks


def _apply_one_generation_result(
    state: _ReferenceBuildState,
    slot_key: str,
    candidate_no: int,
    asset: ReferenceImageAsset | None,
    discarded: list[ReferenceImageAsset],
    rej: list[dict[str, Any]],
) -> None:
    """Record one generation result: discard stale assets, then register or log the failure."""
    from app.multiview import PURPOSE_QA_ANCHOR

    if state.rejection_details is not None:
        state.rejection_details.extend(rej)
    # skip_inline_qa 正常不会返回 discarded；防御性清理也不把它们放进画廊。
    for stale in discarded:
        stale.selectedForSeedance = False
        stale.deleted = True
        if stale.path:
            try:
                Path(stale.path).unlink(missing_ok=True)
            except OSError:
                pass
    if asset is None:
        state.candidate_audit_records.setdefault(slot_key, {})[candidate_no] = {
            "candidate_no": candidate_no,
            "id": None,
            "status": "generation_failed",
            "qa": None,
            "quality_score": None,
        }
        return
    asset.slot_key = slot_key
    asset.candidate_no = candidate_no
    asset.required = is_narrative_keyframe_slot(slot_key) or asset.type == "plot_key_frame"
    asset.entity_type = "shot"
    # winner 未决出前只是 QA staging，绝不能进入视频参考图。
    asset.purposes = [PURPOSE_QA_ANCHOR]
    asset.selectedForSeedance = False
    asset.rejectReason = None
    asset.qa = {"status": "qa_pending", "overall": None, "issues": []}
    asset.qualityScore = None
    asset.dependency_manifest = state.manifest
    asset.prompt_contract_version = KEYFRAME_PROMPT_CONTRACT_VERSION
    asset.keyframe_contract_fingerprint = state.current_keyframe_fingerprint
    state.apply_keyframe_beat(asset, slot_key)
    state.candidate_pool.setdefault(slot_key, []).append((candidate_no, asset))
    state.candidate_pool[slot_key].sort(key=lambda pair: pair[0])
    state.candidate_statuses[(slot_key, candidate_no)] = "qa_pending"
    state.candidate_audit_records.get(slot_key, {}).pop(candidate_no, None)


def _flag_empty_candidate_slots(state: _ReferenceBuildState) -> None:
    """Flag slots whose candidates all failed generation, dropping them from this round."""
    state.active_candidate_slots = set(state.candidate_pool) | state.selection_ready_slots
    empty_slots = [slot for slot in state.active_candidate_slots if not state.candidate_pool.get(slot)]
    if empty_slots:
        for slot_key in empty_slots:
            state.checkpoint_candidates(slot_key, "technical_failed")
        state.existing_meta["keyframe_generation_retry_exhausted"] = True
        state.existing_meta["keyframe_generation_warnings"] = [
            f"{slot_key}: 候选全部生成失败，改用已有锨点或纯文本"
            for slot_key in empty_slots
        ]
        state.active_candidate_slots.difference_update(empty_slots)
        state.publish_progress()


async def _review_candidate(
    slot_key: str,
    ref_type: str,
    candidate_no: int,
    asset: ReferenceImageAsset,
    payload: str,
) -> tuple[str, int, ReferenceImageAsset, dict[str, Any]]:
    # VLM 关键帧/参考图质检已下线：技术产物（文件已成功编码为 payload）
    # 存在即视为可用，不再调用模型评审。
    del payload
    qa = {"status": "scored", "overall": 1.0, "issues": []}
    return slot_key, candidate_no, asset, qa


async def _review_candidates(state: _ReferenceBuildState) -> None:
    """Review every unreviewed candidate (a technical-readability gate; see below)."""
    qa_tasks: list[Any] = []
    _review_active_slots(state, qa_tasks)
    _flag_unreadable_review_slots(state)
    await _apply_candidate_review_results(state, qa_tasks)


def _review_active_slots(state: _ReferenceBuildState, qa_tasks: list[Any]) -> None:
    """Queue a review task for every candidate not already reviewed, dropping unreadable ones."""
    for slot_key in sorted(state.active_candidate_slots):
        if slot_key in state.selection_ready_slots:
            continue
        state.candidate_pool[slot_key] = _collect_slot_review_pairs(state, slot_key, qa_tasks)


def _collect_slot_review_pairs(
    state: _ReferenceBuildState, slot_key: str, qa_tasks: list[Any],
) -> list[tuple[int, ReferenceImageAsset]]:
    """Classify one slot's candidates into already-reviewed/queued-for-review/unreadable."""
    ref_type = state.candidate_ref_types[slot_key]
    valid_pairs: list[tuple[int, ReferenceImageAsset]] = []
    for candidate_no, asset in state.candidate_pool.get(slot_key, []):
        saved_status = state.candidate_statuses.get((slot_key, candidate_no), "qa_pending")
        qa_snapshot = asset.qa if isinstance(asset.qa, dict) else {}
        already_reviewed = (
            (saved_status == "scored" and qa_snapshot.get("overall") is not None)
            or (saved_status == "unverified" and qa_snapshot.get("status") == "unverified")
        )
        if already_reviewed:
            valid_pairs.append((candidate_no, asset))
            continue
        if not asset.path or not Path(asset.path).is_file():
            state.candidate_audit_records[slot_key][candidate_no] = {
                "candidate_no": candidate_no,
                "id": asset.id,
                "status": "technical_failed",
                "qa": {"status": "unverified", "overall": None, "issues": ["关键帧文件缺失"]},
                "quality_score": None,
            }
            state.candidate_cleanup_pool.setdefault(slot_key, []).append((candidate_no, asset))
            continue
        try:
            payload = hiagent.encode_image_file(asset.path)
        except OSError:
            state.candidate_audit_records[slot_key][candidate_no] = {
                "candidate_no": candidate_no,
                "id": asset.id,
                "status": "technical_failed",
                "qa": {"status": "unverified", "overall": None, "issues": ["关键帧无法读取"]},
                "quality_score": None,
            }
            state.candidate_cleanup_pool.setdefault(slot_key, []).append((candidate_no, asset))
            continue
        valid_pairs.append((candidate_no, asset))
        qa_tasks.append(asyncio.create_task(_review_candidate(
            slot_key, ref_type, candidate_no, asset, payload,
        )))
    return valid_pairs


def _flag_unreadable_review_slots(state: _ReferenceBuildState) -> None:
    """Delete every candidate in a slot where nothing survived to review."""
    unreadable_slots = [
        slot for slot in state.active_candidate_slots
        if slot not in state.selection_ready_slots and not state.candidate_pool.get(slot)
    ]
    if not unreadable_slots:
        return
    for slot_key in unreadable_slots:
        for _candidate_no, asset in state.candidate_cleanup_pool.get(slot_key, []):
            if asset.path:
                try:
                    Path(asset.path).unlink(missing_ok=True)
                except OSError:
                    pass
        state.checkpoint_candidates(slot_key, "technical_failed")
    state.existing_meta["keyframe_file_retry_exhausted"] = True
    state.existing_meta["keyframe_file_warnings"] = [
        f"{slot_key}: 候选均不可读，改用已有锨点或纯文本"
        for slot_key in unreadable_slots
    ]
    state.active_candidate_slots.difference_update(unreadable_slots)
    state.publish_progress()


async def _apply_candidate_review_results(state: _ReferenceBuildState, qa_tasks: list[Any]) -> None:
    """Apply each review result to its candidate and checkpoint the slot."""
    from app.multiview import PURPOSE_QA_ANCHOR

    for completed in asyncio.as_completed(qa_tasks):
        slot_key, candidate_no, asset, qa = await completed
        asset.qa = dict(qa or {})
        state.apply_keyframe_beat(asset, slot_key)
        if asset.qa.get("status") == "unverified" or asset.qa.get("overall") is None:
            asset.qualityScore = None
            asset.rejectReason = "qa_unverified_score_only"
            state.candidate_statuses[(slot_key, candidate_no)] = "unverified"
        else:
            try:
                overall = float(asset.qa.get("overall"))
            except (TypeError, ValueError):
                overall = 0.0
            asset.qualityScore = overall
            asset.qa.setdefault("absolute_quality", overall)
            asset.rejectReason = None
            state.candidate_statuses[(slot_key, candidate_no)] = "scored"
        asset.selectedForSeedance = False
        asset.purposes = [PURPOSE_QA_ANCHOR]
        state.checkpoint_candidates(slot_key, "qa_pending")
        state.publish_progress()
