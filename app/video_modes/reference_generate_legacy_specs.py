"""Per-slot candidate-plan resolution for the legacy per-shot reference-asset
build (see ``reference_generate_legacy.py``'s module docstring for the full
phase map). ``specs`` is the set of logical slots that still need a fresh
prompt/generation this round; a slot resolved from a resumable checkpoint or
already awaiting winner selection never reaches ``specs``.

Split into planning which slots exist (``_plan_reference_slots``) and
resolving each one (``_build_slot_candidates``, which calls
``_build_one_slot_candidates`` once per slot). The per-slot body's four
``continue`` statements become ``return`` now that it is its own function
call inside the ``for`` loop in ``_build_slot_candidates`` -- each one still
only skips the rest of *that slot's* resolution and lets the loop proceed to
the next slot, exactly as before. The nested ``for raw_record in
prior_records or []`` loop is not extracted, so its own ``continue``
statements are unchanged. Moved verbatim out of the pre-split single
function otherwise.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .asset_lookup import _asset_from_path
from .keyframe_contract import is_narrative_keyframe_slot
from .mode_selection import (
    KEYFRAME_PROMPT_CONTRACT_VERSION,
    REFERENCE_IMAGE_TYPES,
    keyframe_candidate_count,
    supporting_keyframe_candidate_count,
)
from .reference_generate import _SLOT_ROLE_CYCLE
from .reference_generate_legacy_state import _ReferenceBuildState


def _plan_reference_slots(state: _ReferenceBuildState) -> tuple[dict[str, Any], list[tuple[str, str, str | None, int]]]:
    """Determine which logical slots are resumable, and which are newly planned.

    Returns ``(resumable_slots, planned_slots)`` for ``_build_slot_candidates``.
    """
    type_cycle = [t for t in state.plan.types if t in REFERENCE_IMAGE_TYPES and t not in {"previous_shot_frame"}] or ["plot_key_frame"]
    model_specs = [p for p in (state.plan.prompts or []) if p.get("prompt")]

    resumable_slots = {
        k: v for k, v in state.slot_state.items()
        if isinstance(v, dict)
        and v.get("status") in {"passed", "unverified", "scored_warning"}
        and v.get("path")
        and (not is_narrative_keyframe_slot(k) or v.get("type") == "plot_key_frame")
        and v.get("prompt_contract_version") == KEYFRAME_PROMPT_CONTRACT_VERSION
        and v.get("keyframe_contract_fingerprint") == state.current_keyframe_fingerprint
    }
    planned_slots: list[tuple[str, str, str | None, int]] = []
    if state.decision.defaulted:
        planned_slots = [
            (
                str(beat["slot_key"]),
                "plot_key_frame",
                str(beat["prompt_intent"]),
                index,
            )
            for index, beat in enumerate(state.temporal_beats)
        ]
    else:
        custom_keyframe_count = 0
        for i in range(state.generated_needed):
            role = _SLOT_ROLE_CYCLE[i % len(_SLOT_ROLE_CYCLE)]
            proposed_type = (
                (model_specs[i].get("type") if i < len(model_specs) else None)
                or type_cycle[i % len(type_cycle)]
            )
            if proposed_type == "plot_key_frame":
                if custom_keyframe_count >= int(state.keyframe_plan["count"]):
                    continue
                slot_key = (
                    "narrative_keyframe"
                    if custom_keyframe_count == 0
                    else f"narrative_keyframe_{custom_keyframe_count:02d}"
                )
                custom_keyframe_count += 1
            else:
                slot_key = role[0] if i < len(_SLOT_ROLE_CYCLE) else f"extra_{i}"
            brief = model_specs[i].get("prompt") if i < len(model_specs) else None
            planned_slots.append((slot_key, proposed_type, brief, i))
    return resumable_slots, planned_slots


def _build_slot_candidates(
    state: _ReferenceBuildState,
    resumable_slots: dict[str, Any],
    planned_slots: list[tuple[str, str, str | None, int]],
) -> None:
    """Resolve every planned slot's candidate plan, in slot order."""
    for slot_key, proposed_type, planned_brief, i in planned_slots:
        _build_one_slot_candidates(state, resumable_slots, slot_key, proposed_type, planned_brief, i)


def _build_one_slot_candidates(
    state: _ReferenceBuildState,
    resumable_slots: dict[str, Any],
    slot_key: str,
    proposed_type: str,
    planned_brief: str | None,
    i: int,
) -> None:
    """Resolve one slot: resume a passed checkpoint, await its winner, or plan a spec."""
    # 叙事关键帧是必需几何合同槽；旧/custom mode plan 不得把它降成 character/scene。
    ref_type = "plot_key_frame" if is_narrative_keyframe_slot(slot_key) else proposed_type
    prior = state.slot_state.get(slot_key) if isinstance(state.slot_state.get(slot_key), dict) else {}
    state.candidate_ref_types[slot_key] = ref_type
    if _resume_slot_from_checkpoint(state, resumable_slots, slot_key, ref_type):
        return

    prior_contract_ok = (
        prior.get("type") == ref_type
        and prior.get("prompt_contract_version") == KEYFRAME_PROMPT_CONTRACT_VERSION
        and prior.get("keyframe_contract_fingerprint") == state.current_keyframe_fingerprint
    )
    target, prior_records = _determine_slot_candidate_target(prior, prior_contract_ok, slot_key)
    state.candidate_targets[slot_key] = target
    state.candidate_pool.setdefault(slot_key, [])
    state.candidate_audit_records.setdefault(slot_key, {})
    _rehydrate_slot_candidate_pool(state, slot_key, ref_type, prior_records, target)

    _finalize_slot_spec(state, slot_key, ref_type, proposed_type, planned_brief, prior, prior_contract_ok, i)


def _resume_slot_from_checkpoint(
    state: _ReferenceBuildState,
    resumable_slots: dict[str, Any],
    slot_key: str,
    ref_type: str,
) -> bool:
    """Resume a previously-passed candidate straight into ``selected``.

    Returns ``True`` when the slot is fully resolved this way (the caller
    must stop processing this slot).
    """
    from app.multiview import PURPOSE_QA_ANCHOR, PURPOSE_VIDEO_INPUT

    if slot_key not in resumable_slots:
        return False
    prev = resumable_slots[slot_key]
    path = prev.get("path")
    if not path or not Path(path).is_file():
        return False
    asset = _asset_from_path(
        path=path,
        ref_type=prev.get("type") or ref_type,
        source="seedream_generated",
        quality_score=float(prev.get("quality_score") or 0.0) if prev.get("quality_score") is not None else None,
        qa=prev.get("qa") or {"overall": None, "status": "unverified", "resumed": True},
        purposes=[PURPOSE_VIDEO_INPUT, PURPOSE_QA_ANCHOR],
        required=True,
        slot_key=slot_key,
        entity_type="shot",
    )
    asset.dependency_manifest = state.manifest
    asset.prompt_contract_version = KEYFRAME_PROMPT_CONTRACT_VERSION
    asset.keyframe_contract_fingerprint = state.current_keyframe_fingerprint
    asset.candidate_no = int(prev.get("winner_candidate_no") or 1)
    state.apply_keyframe_beat(asset, slot_key)
    if prev.get("status") == "unverified":
        asset.rejectReason = "qa_unverified_score_only"
    elif prev.get("status") == "scored_warning":
        asset.rejectReason = "quality_below_threshold_score_only"
    state.selected.append(asset)
    return True


def _determine_slot_candidate_target(
    prior: dict[str, Any],
    prior_contract_ok: bool,
    slot_key: str,
) -> tuple[int, list[Any] | None]:
    """Determine this slot's candidate-count target and its prior candidate records."""
    from app.multiview import NARRATIVE_KEYFRAME_SLOT

    if slot_key == NARRATIVE_KEYFRAME_SLOT:
        target = keyframe_candidate_count()
    elif is_narrative_keyframe_slot(slot_key):
        target = supporting_keyframe_candidate_count()
    else:
        target = 1
    prior_records = prior.get("candidates") if prior_contract_ok else None
    # 兼容旧的单图 qa_pending 恢复点：已付费的图不在升级中重生。
    legacy_single_pending = bool(
        prior_contract_ok
        and prior.get("status") == "qa_pending"
        and not isinstance(prior_records, list)
        and prior.get("path")
    )
    if legacy_single_pending:
        target = 1
        prior_records = [{
            "candidate_no": 1,
            "id": prior.get("id"),
            "path": prior.get("path"),
            "status": "qa_pending",
            "qa": prior.get("qa"),
            "quality_score": prior.get("quality_score"),
        }]
    elif prior_contract_ok and prior.get("candidate_target") is not None:
        try:
            target = max(1, min(int(prior["candidate_target"]), 5))
        except (TypeError, ValueError):
            pass
    return target, prior_records


def _rehydrate_slot_candidate_pool(
    state: _ReferenceBuildState,
    slot_key: str,
    ref_type: str,
    prior_records: list[Any] | None,
    target: int,
) -> None:
    """Rehydrate every still-valid prior candidate record into the working pool."""
    seen_candidate_nos: set[int] = set()
    for raw_record in prior_records or []:
        if not isinstance(raw_record, dict):
            continue
        try:
            candidate_no = int(raw_record.get("candidate_no"))
        except (TypeError, ValueError):
            continue
        if candidate_no < 1 or candidate_no > target or candidate_no in seen_candidate_nos:
            continue
        seen_candidate_nos.add(candidate_no)
        asset = state.rehydrate_candidate(slot_key, ref_type, candidate_no, raw_record)
        if asset is not None:
            state.candidate_pool[slot_key].append((candidate_no, asset))
            state.candidate_statuses[(slot_key, candidate_no)] = str(raw_record.get("status") or "qa_pending")
        elif raw_record.get("status") in {
            "discarded_deleted", "discarded_pending_cleanup", "cleanup_pending", "generation_failed",
        }:
            recovered_status = str(raw_record.get("status"))
            if recovered_status in {"discarded_pending_cleanup", "cleanup_pending"}:
                # checkpoint 之后、最终状态落库之前已删除：按已清理恢复。
                recovered_status = "discarded_deleted"
            state.candidate_audit_records[slot_key][candidate_no] = {
                "candidate_no": candidate_no,
                "id": raw_record.get("id"),
                "status": recovered_status,
                "qa": raw_record.get("qa"),
                "quality_score": raw_record.get("quality_score"),
            }
    state.candidate_pool[slot_key].sort(key=lambda pair: pair[0])


def _finalize_slot_spec(
    state: _ReferenceBuildState,
    slot_key: str,
    ref_type: str,
    proposed_type: str,
    planned_brief: str | None,
    prior: dict[str, Any],
    prior_contract_ok: bool,
    i: int,
) -> None:
    """Decide whether this slot awaits winner selection, reuses a prompt, or needs a fresh one."""
    target = state.candidate_targets[slot_key]
    winner_no = int(prior.get("winner_candidate_no") or 0)
    if prior_contract_ok and prior.get("status") == "selection_pending_cleanup" and any(
        no == winner_no for no, _asset in state.candidate_pool[slot_key]
    ):
        state.selection_ready_slots.add(slot_key)
        return
    if len(state.candidate_pool[slot_key]) >= target:
        return
    prior_prompt = str((prior or {}).get("prompt") or "").strip()
    if (
        prior_contract_ok
        and (
            prior_prompt
            or prior.get("prompt_source") == "deterministic_template"
            or bool(state.candidate_pool[slot_key])
        )
    ):
        state.specs.append((slot_key, ref_type, prior_prompt or None, i))
        state.checkpointed_prompt_slots.add(slot_key)
        return
    # 旧计划若把必需关键帧声明成 character/scene，其 portrait/environment 正文也必须作废，
    # 不能只把 type 标签改成 plot_key_frame 后继续稀释硬合同。
    brief = planned_brief
    if is_narrative_keyframe_slot(slot_key) and proposed_type != ref_type:
        brief = None
    # 失效 passed/错类型 slot 不得在 **old 合并时残留旧 path/qa/score。
    if not state.candidate_pool[slot_key]:
        state.slot_state.pop(slot_key, None)
    state.specs.append((slot_key, ref_type, brief, i))
