"""Legacy per-shot reference-asset build: orchestrator.

``_build_generated_reference_assets_legacy`` was a single ~1,312-line async
function (moved verbatim, undecomposed, from the pre-split ``app/video_modes.py``)
built around ~35 locals kept alive across its whole body plus four nested
closures each capturing a different subset of them. It is now the
orchestrator, split by real phase boundary the same way ``storyboard_
validate.py`` was split (see that file's module docstring for the precedent
in this repo):

  - ``reference_generate_legacy_state.py``: the ``_ReferenceBuildState``
    dataclass every phase reads/writes, plus the four closures that used to
    be called from many different points in the function
    (``publish_progress``, ``apply_keyframe_beat``, ``candidate_record``,
    ``checkpoint_candidates``) reimplemented as its methods -- a method
    always resolves ``self.<field>``, removing the "closure captured across
    a phase boundary" ambiguity a plain nested function would keep.
  - ``reference_generate_legacy_setup.py``: keyframe-contract fingerprint/
    staleness, visible-identity names, the frozen/rebuilt asset-dependency
    manifest, the previous-shot continuity tail, and the reusable-asset
    evidence gallery.
  - ``reference_generate_legacy_anchors.py``: reusable person/scene anchor
    selection up to budget, and keyframe-sequence planning/fingerprinting.
  - ``reference_generate_legacy_specs.py``: per-slot candidate-plan
    resolution (resume a checkpoint, await a pending winner, or plan a spec).
  - ``reference_generate_legacy_prompts.py``: prompt resolution for every
    still-open spec, the ``prompt_ready`` checkpoint, and seed-image
    assembly.
  - ``reference_generate_legacy_candidates.py``: concurrent candidate
    generation, empty-slot flagging, and QA review.
  - ``reference_generate_legacy_selection.py``: per-slot winner selection
    and loser cleanup.
  - ``reference_generate_legacy_finalize.py``: cross-shot consistency/
    invariance enforcement (with single-keyframe fallback) and final
    gallery assembly.

The sequence and the data each phase reads is unchanged from the pre-split
source; only the decomposition into named, independently readable/testable
steps -- reading/writing through one explicit ``_ReferenceBuildState``
instead of ~35 bare locals -- is new. Add new legacy reference-build logic
to the concern-matching sibling file, not back into this one.
"""
from __future__ import annotations

from typing import Any, Callable

from app.schemas import Bible, EpisodeScreenplay, Shot

from .mode_selection import ReferenceImageAsset, ShotVideoModeDecision
from .reference_generate_legacy_anchors import _plan_keyframe_sequence, _select_reference_anchors
from .reference_generate_legacy_candidates import (
    _flag_empty_candidate_slots,
    _generate_candidates,
    _review_candidates,
)
from .reference_generate_legacy_finalize import _assemble_final_gallery, _enforce_cross_shot_consistency
from .reference_generate_legacy_prompts import (
    _assemble_seed_images,
    _checkpoint_prompt_ready_slots,
    _resolve_slot_prompts,
)
from .reference_generate_legacy_selection import _select_slot_winners
from .reference_generate_legacy_setup import (
    _prepare_contract_fingerprint,
    _prepare_continuity_tail,
    _prepare_evidence_assets,
    _prepare_identity_names,
    _prepare_manifest,
)
from .reference_generate_legacy_specs import _build_slot_candidates, _plan_reference_slots
from .reference_generate_legacy_state import _ReferenceBuildState


async def _build_generated_reference_assets_legacy(*, conn: Any, project_id: str, episode_no: int, episode_id: str,
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
    state = _ReferenceBuildState(
        conn=conn, project_id=project_id, episode_no=episode_no, episode_id=episode_id,
        shot_id=shot_id, shot=shot, bible=bible, decision=decision, prev_shot=prev_shot,
        rejection_details=rejection_details, rejected_out=rejected_out, on_progress=on_progress,
        allow_missing_continuity_tail=allow_missing_continuity_tail, job_id=job_id,
        existing_meta=existing_meta if existing_meta is not None else {},
        screenplay=screenplay,
    )

    prompt_contract_changed = _prepare_contract_fingerprint(state)
    _prepare_identity_names(state)
    await _prepare_manifest(state, prompt_contract_changed)
    _prepare_continuity_tail(state)
    _prepare_evidence_assets(state)

    _select_reference_anchors(state)
    _plan_keyframe_sequence(state)
    state.publish_progress()

    resumable_slots, planned_slots = _plan_reference_slots(state)
    _build_slot_candidates(state, resumable_slots, planned_slots)

    await _resolve_slot_prompts(state)
    _checkpoint_prompt_ready_slots(state)
    _assemble_seed_images(state)

    await _generate_candidates(state)
    _flag_empty_candidate_slots(state)
    await _review_candidates(state)

    _select_slot_winners(state)

    video_candidates = await _enforce_cross_shot_consistency(state)
    return _assemble_final_gallery(state, video_candidates)
