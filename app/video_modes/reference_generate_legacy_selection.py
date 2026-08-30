"""Per-slot winner selection and loser cleanup for the legacy per-shot
reference-asset build (see ``reference_generate_legacy.py``'s module
docstring for the full phase map).

VLM cross-slot consistency comparison, identity/geometry gating and runtime
hard-failure detection are all disabled (see the inline notes below, moved
verbatim from the pre-split source): a technical product that exists is
usable, and the remaining ``contract_blocked_by_slot``/``structural_
warnings`` machinery is always empty/false in practice but kept for
structural fidelity with the pre-split source rather than pruned here.

Split into the per-episode setup and per-slot loop (``_select_slot_winners``),
the per-slot orchestrator (``_select_one_slot_winner``), the (dead in
practice, kept for fidelity) contract-blocked-slot rejection path
(``_reject_slot_for_contract_block`` and its two helpers), winner selection
and status (``_choose_slot_winner`` / ``_finalize_slot_winner_status`` /
``_checkpoint_slot_winner``), and loser cleanup
(``_cleanup_slot_loser_candidates`` / ``_cleanup_slot_technical_failures`` /
``_persist_slot_winner``). The per-slot orchestrator's two top-level
``continue`` statements from the pre-split source become ``return`` now
that it is its own function call inside the loop -- each still only skips
the rest of *that slot's* winner selection and lets the loop proceed to the
next slot, exactly as before.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .keyframe_contract import is_narrative_keyframe_slot
from .mode_selection import (
    KEYFRAME_STRUCTURAL_FALLBACK_MODE,
    ReferenceImageAsset,
)
from .reference_generate_legacy_state import _ReferenceBuildState


def _select_slot_winners(state: _ReferenceBuildState) -> None:
    """Select (or resume) a winner for every active slot, then clean up its losers."""
    from .keyframe_contract import required_visual_anchor_names

    # VLM 跨槽一致性比对已下线（原用于在双关键帧之间比较身份/服装/体型/身高比例）：
    # 已知限制，双关键帧之间的一致性不再有自动化校验，需要人工在候选列表里复核。

    # VLM 关键帧身份/几何门禁与运行时硬失败检测已下线（原 keyframe_gate_passed/
    # keyframe_runtime_blocking_failures）：技术产物存在即可用，不再按身份契约
    # 剔除候选或删除候选文件。已知限制：多候选中撞脸/换装/几何错误的候选不再
    # 被自动过滤，需要人工在候选列表里复核后再采用。
    eligible_by_slot: dict[str, list[tuple[int, ReferenceImageAsset]]] = {
        slot_key: list(state.candidate_pool.get(slot_key, []))
        for slot_key in state.active_candidate_slots
    }
    contract_blocked_by_slot: dict[str, list[dict[str, Any]]] = {}
    state.existing_meta["reference_slots"] = state.slot_state

    all_cleanup_errors: list[str] = []
    required_identity_names = required_visual_anchor_names(state.manifest)
    anchored_identity_names = {
        str(asset.entity_name or "").strip()
        for asset in state.video_anchor_assets
        if (asset.entity_type or asset.type) == "character"
        and str(asset.entity_name or "").strip()
    }
    for slot_key in sorted(state.active_candidate_slots):
        _select_one_slot_winner(
            state, slot_key, eligible_by_slot, contract_blocked_by_slot,
            required_identity_names, anchored_identity_names, all_cleanup_errors,
        )

    if all_cleanup_errors:
        state.existing_meta["candidate_cleanup_warnings"] = all_cleanup_errors


def _select_one_slot_winner(
    state: _ReferenceBuildState,
    slot_key: str,
    eligible_by_slot: dict[str, list[tuple[int, ReferenceImageAsset]]],
    contract_blocked_by_slot: dict[str, list[dict[str, Any]]],
    required_identity_names: set[str],
    anchored_identity_names: set[str],
    all_cleanup_errors: list[str],
) -> None:
    """Select this slot's winner (or resume a frozen one) and clean up every loser."""
    all_pairs = state.candidate_pool.get(slot_key, [])
    pairs = eligible_by_slot.get(slot_key, all_pairs)
    if not all_pairs:
        state.checkpoint_candidates(slot_key, "technical_failed")
        return
    if not pairs and contract_blocked_by_slot.get(slot_key):
        _reject_slot_for_contract_block(
            state, slot_key, all_pairs, contract_blocked_by_slot,
            required_identity_names, anchored_identity_names, all_cleanup_errors,
        )
        return

    winner, winner_no = _choose_slot_winner(state, slot_key, pairs)
    winner_status = _finalize_slot_winner_status(state, slot_key, winner, winner_no)
    _checkpoint_slot_winner(state, slot_key, winner_no, winner, all_pairs)

    slot_cleanup_errors: list[str] = []
    final_records = _cleanup_slot_loser_candidates(
        state, slot_key, all_pairs, winner, slot_cleanup_errors, all_cleanup_errors,
    )
    _cleanup_slot_technical_failures(
        state, slot_key, final_records, winner, slot_cleanup_errors, all_cleanup_errors,
    )
    _persist_slot_winner(state, slot_key, winner, winner_no, winner_status, final_records, slot_cleanup_errors)


def _reject_slot_for_contract_block(
    state: _ReferenceBuildState,
    slot_key: str,
    all_pairs: list[tuple[int, ReferenceImageAsset]],
    contract_blocked_by_slot: dict[str, list[dict[str, Any]]],
    required_identity_names: set[str],
    anchored_identity_names: set[str],
    all_cleanup_errors: list[str],
) -> None:
    """Delete every candidate in a slot whose identity contract check failed."""
    from app.multiview import PURPOSE_VIDEO_INPUT

    final_records: list[dict[str, Any]] = []
    slot_cleanup_errors: list[str] = []
    for candidate_no, asset in all_pairs:
        asset.selectedForSeedance = False
        asset.deleted = True
        asset.rejectReason = "identity_contract_failed"
        asset.purposes = [
            purpose for purpose in (asset.purposes or [])
            if purpose != PURPOSE_VIDEO_INPUT
        ]
        delete_failed = False
        if asset.path:
            try:
                Path(asset.path).unlink(missing_ok=True)
            except OSError as exc:
                delete_failed = True
                message = f"{slot_key} candidate {candidate_no}: {exc}"
                slot_cleanup_errors.append(message)
                all_cleanup_errors.append(message)
        final_records.append(state.candidate_record(
            slot_key,
            candidate_no,
            asset,
            include_path=delete_failed,
            status="cleanup_pending" if delete_failed else "contract_rejected_deleted",
        ))
        if state.rejection_details is not None:
            state.rejection_details.append({
                "type": asset.type,
                "source": asset.source,
                "reason": "identity_contract_failed",
                "candidate_no": candidate_no,
                "identity_contract_passed": False,
            })
    _persist_contract_blocked_slot(
        state, slot_key, final_records, slot_cleanup_errors, contract_blocked_by_slot,
        required_identity_names, anchored_identity_names,
    )


def _persist_contract_blocked_slot(
    state: _ReferenceBuildState,
    slot_key: str,
    final_records: list[dict[str, Any]],
    slot_cleanup_errors: list[str],
    contract_blocked_by_slot: dict[str, list[dict[str, Any]]],
    required_identity_names: set[str],
    anchored_identity_names: set[str],
) -> None:
    """Persist the contract-gate-failed status, flagging structural fallback when eligible."""
    state.slot_state[slot_key] = {
        **(state.slot_state.get(slot_key) or {}),
        "status": (
            "contract_gate_cleanup_pending"
            if slot_cleanup_errors
            else "contract_gate_failed"
        ),
        "gate_retry_exhausted": True,
        "gate_warnings": contract_blocked_by_slot[slot_key],
        "candidate_target": state.candidate_targets.get(slot_key, len(final_records)),
        "candidate_count": len(final_records),
        "candidates": final_records,
        "winner_candidate_no": None,
        "path": None,
        "qa": None,
        "quality_score": None,
    }
    if required_identity_names.issubset(anchored_identity_names):
        fallback_slots = {
            str(item)
            for item in (
                state.existing_meta.get("keyframe_structural_fallback_slots") or []
            )
            if str(item)
        }
        fallback_slots.add(slot_key)
        state.existing_meta["keyframe_fallback_mode"] = (
            KEYFRAME_STRUCTURAL_FALLBACK_MODE
        )
        state.existing_meta["keyframe_structural_fallback_slots"] = sorted(
            fallback_slots
        )
    state.existing_meta["reference_slots"] = state.slot_state
    state.publish_progress()


def _numeric_qa_score(asset: ReferenceImageAsset) -> float | None:
    value = (asset.qa or {}).get("overall")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _choose_slot_winner(
    state: _ReferenceBuildState,
    slot_key: str,
    pairs: list[tuple[int, ReferenceImageAsset]],
) -> tuple[ReferenceImageAsset, int]:
    """Resume a frozen winner if one is pending cleanup, else pick the best-scored candidate."""
    prior = state.slot_state.get(slot_key) or {}
    frozen_winner_no = int(prior.get("winner_candidate_no") or 0)
    if slot_key in state.selection_ready_slots and any(no == frozen_winner_no for no, _asset in pairs):
        winner_no, winner = next(pair for pair in pairs if pair[0] == frozen_winner_no)
    else:
        # 有数字 QA 的候选永远优先于 unverified；同分/全未评分按 candidate_no 稳定取第一张。
        winner_no, winner = max(
            pairs,
            key=lambda pair: (
                _numeric_qa_score(pair[1]) is not None,
                _numeric_qa_score(pair[1]) or 0.0,
                -pair[0],
            ),
        )
    return winner, winner_no


def _finalize_slot_winner_status(
    state: _ReferenceBuildState,
    slot_key: str,
    winner: ReferenceImageAsset,
    winner_no: int,
) -> str:
    """Set the winner's video-input fields and determine its passed/blocked/unverified status."""
    from app.multiview import PURPOSE_QA_ANCHOR, PURPOSE_VIDEO_INPUT

    winner.candidate_no = winner_no
    winner.required = is_narrative_keyframe_slot(slot_key) or winner.type == "plot_key_frame"
    winner.purposes = [PURPOSE_VIDEO_INPUT, PURPOSE_QA_ANCHOR]
    winner.selectedForSeedance = True
    winner.deleted = False
    winner_status = "unverified"
    if _numeric_qa_score(winner) is None:
        winner.rejectReason = "qa_unverified_score_only"
    else:
        # VLM 身份/几何门禁与运行时硬失败检测已下线：技术产物存在即通过。
        passed = True
        structural_warnings: set[str] = set()
        if passed and not structural_warnings:
            winner_status = "passed"
            winner.rejectReason = None
        else:
            winner_status = "blocked"
            winner.selectedForSeedance = False
            winner.purposes = [
                purpose
                for purpose in winner.purposes
                if purpose != PURPOSE_VIDEO_INPUT
            ]
            winner.rejectReason = "runtime_contract_blocked"
            winner.qa = {
                **(winner.qa or {}),
                "gate_retry_exhausted": True,
            }
    if winner.selectedForSeedance and winner not in state.selected:
        state.selected.append(winner)
    return winner_status


def _checkpoint_slot_winner(
    state: _ReferenceBuildState,
    slot_key: str,
    winner_no: int,
    winner: ReferenceImageAsset,
    all_pairs: list[tuple[int, ReferenceImageAsset]],
) -> None:
    """Mark every candidate selected/discarded-pending-cleanup and persist the pre-delete gallery."""
    for candidate_no, _asset in all_pairs:
        state.candidate_statuses[(slot_key, candidate_no)] = (
            "selected_pending_cleanup" if candidate_no == winner_no else "discarded_pending_cleanup"
        )
    state.checkpoint_candidates(slot_key, "selection_pending_cleanup")
    state.slot_state[slot_key].update({
        "winner_candidate_no": winner_no,
        "path": winner.path,
        "qa": winner.qa,
        "quality_score": winner.qualityScore,
    })
    state.existing_meta["reference_slots"] = state.slot_state
    # 先持久化“画廊只有 winner”，再删文件；崩溃只会留下不可达的孤儿文件。
    state.publish_progress()


def _cleanup_slot_loser_candidates(
    state: _ReferenceBuildState,
    slot_key: str,
    all_pairs: list[tuple[int, ReferenceImageAsset]],
    winner: ReferenceImageAsset,
    slot_cleanup_errors: list[str],
    all_cleanup_errors: list[str],
) -> dict[int, dict[str, Any]]:
    """Delete every non-winner candidate's file and record its final status."""
    from app.multiview import PURPOSE_VIDEO_INPUT

    winner_resolved: Path | None = None
    if winner.path:
        try:
            winner_resolved = Path(winner.path).resolve(strict=False)
        except OSError:
            winner_resolved = Path(winner.path).absolute()
    final_records: dict[int, dict[str, Any]] = dict(state.candidate_audit_records.get(slot_key) or {})
    for candidate_no, asset in all_pairs:
        if asset is winner:
            final_records[candidate_no] = state.candidate_record(
                slot_key, candidate_no, asset, status="selected",
            )
            continue
        asset.selectedForSeedance = False
        asset.deleted = True
        asset.rejectReason = "best_of_three_not_selected"
        asset.purposes = [p for p in (asset.purposes or []) if p != PURPOSE_VIDEO_INPUT]
        delete_failed = False
        if asset.path:
            try:
                loser_resolved = Path(asset.path).resolve(strict=False)
            except OSError:
                loser_resolved = Path(asset.path).absolute()
            if winner_resolved is None or loser_resolved != winner_resolved:
                try:
                    Path(asset.path).unlink(missing_ok=True)
                except OSError as exc:
                    delete_failed = True
                    message = f"{slot_key} candidate {candidate_no}: {exc}"
                    slot_cleanup_errors.append(message)
                    all_cleanup_errors.append(message)
        final_records[candidate_no] = state.candidate_record(
            slot_key,
            candidate_no,
            asset,
            include_path=delete_failed,
            status="cleanup_pending" if delete_failed else "discarded_deleted",
        )
        if state.rejection_details is not None:
            state.rejection_details.append({
                "type": asset.type,
                "source": asset.source,
                "reason": "best_of_three_not_selected",
                "candidate_no": candidate_no,
                "quality_score": asset.qualityScore,
            })
    return final_records


def _cleanup_slot_technical_failures(
    state: _ReferenceBuildState,
    slot_key: str,
    final_records: dict[int, dict[str, Any]],
    winner: ReferenceImageAsset,
    slot_cleanup_errors: list[str],
    all_cleanup_errors: list[str],
) -> None:
    """Delete every technically-failed candidate's file, updating ``final_records`` in place."""
    winner_resolved: Path | None = None
    if winner.path:
        try:
            winner_resolved = Path(winner.path).resolve(strict=False)
        except OSError:
            winner_resolved = Path(winner.path).absolute()
    for candidate_no, asset in state.candidate_cleanup_pool.get(slot_key, []):
        delete_failed = False
        if asset.path:
            try:
                technical_resolved = Path(asset.path).resolve(strict=False)
            except OSError:
                technical_resolved = Path(asset.path).absolute()
            if winner_resolved is None or technical_resolved != winner_resolved:
                try:
                    Path(asset.path).unlink(missing_ok=True)
                except OSError as exc:
                    delete_failed = True
                    message = f"{slot_key} candidate {candidate_no}: {exc}"
                    slot_cleanup_errors.append(message)
                    all_cleanup_errors.append(message)
        if delete_failed:
            final_records[candidate_no] = {
                **(final_records.get(candidate_no) or {}),
                "candidate_no": candidate_no,
                "id": asset.id,
                "status": "cleanup_pending",
                "path": asset.path,
            }


def _persist_slot_winner(
    state: _ReferenceBuildState,
    slot_key: str,
    winner: ReferenceImageAsset,
    winner_no: int,
    winner_status: str,
    final_records: dict[int, dict[str, Any]],
    slot_cleanup_errors: list[str],
) -> None:
    """Persist the final per-candidate ledger and the winner's own fields."""
    state.slot_state[slot_key] = {
        **state.slot_state[slot_key],
        "status": winner_status if not slot_cleanup_errors else "selection_pending_cleanup",
        "candidate_target": state.candidate_targets.get(slot_key, len(final_records)),
        "candidate_count": state.candidate_targets.get(slot_key, len(final_records)),
        "candidates": [final_records[n] for n in sorted(final_records)],
        "winner_candidate_no": winner_no,
        "path": winner.path,
        "qa": winner.qa,
        "quality_score": winner.qualityScore,
    }
    state.existing_meta["reference_slots"] = state.slot_state
    state.publish_progress()
