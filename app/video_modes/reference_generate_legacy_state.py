"""Mutable working state for the legacy per-shot reference-asset build
(``_build_generated_reference_assets_legacy``, see
``reference_generate_legacy.py``'s module docstring for the full phase map).

The pre-split single function held ~35 locals alive across its whole body
plus four nested closures (``_publish_progress``, ``_apply_keyframe_beat``,
``_candidate_record``, ``_checkpoint_candidates``) that each closed over a
different subset of them and were called from many different points in the
function. Splitting the function into phase functions while keeping those
four as closures would require re-declaring which subset of ~35 locals each
one captures at every call site -- exactly the "closure captured across a
phase boundary" hazard this kind of split is prone to. Making them methods
on one mutable ``_ReferenceBuildState`` instead removes the ambiguity: a
method always reads ``self.<field>``, so there is only one place that binds
each name, no matter which phase function calls it.

Every field below is either a read-only input captured once at construction
or a working value some later phase reads. Fields are moved verbatim
(unchanged types/initial values) out of the pre-split function's locals.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.schemas import Bible, EpisodeScreenplay, Shot

from .mode_selection import ReferenceImageAsset, ShotVideoModeDecision


@dataclass
class _ReferenceBuildState:
    """All state threaded across the legacy reference-asset build's phases."""

    # --- read-only inputs, captured once from the caller ---------------
    conn: Any
    project_id: str
    episode_no: int
    episode_id: str
    shot_id: str
    shot: Shot
    bible: Bible
    decision: ShotVideoModeDecision
    prev_shot: Any | None
    rejection_details: list[dict[str, Any]] | None
    rejected_out: list[ReferenceImageAsset] | None
    on_progress: Callable[[list[ReferenceImageAsset], list[ReferenceImageAsset]], None] | None
    allow_missing_continuity_tail: bool
    job_id: str | None
    existing_meta: dict[str, Any]
    screenplay: EpisodeScreenplay | None

    # --- working values, populated as phases run ------------------------
    plan: Any = None
    max_refs: int = 0
    current_keyframe_fingerprint: str = ""
    slot_state: dict[str, Any] = field(default_factory=dict)
    scene_name: str = ""
    identity_character_names: list[str] = field(default_factory=list)
    manifest: Any = None
    forced: list[ReferenceImageAsset] = field(default_factory=list)
    needs_tail: bool = False
    want_gen: int = 0
    evidence_assets: list[ReferenceImageAsset] = field(default_factory=list)
    selected: list[ReferenceImageAsset] = field(default_factory=list)
    continuity_slot_reserve: int = 0
    video_anchor_assets: list[ReferenceImageAsset] = field(default_factory=list)
    available_generated_slots: int = 0
    keyframe_plan: dict[str, Any] = field(default_factory=dict)
    generated_needed: int = 0
    temporal_beats: list[dict[str, Any]] = field(default_factory=list)
    beat_by_slot: dict[str, Any] = field(default_factory=dict)
    sequence_material: dict[str, Any] = field(default_factory=dict)
    specs: list[tuple[str, str, str | None, int]] = field(default_factory=list)
    checkpointed_prompt_slots: set[str] = field(default_factory=set)
    candidate_pool: dict[str, list[tuple[int, ReferenceImageAsset]]] = field(default_factory=dict)
    candidate_targets: dict[str, int] = field(default_factory=dict)
    candidate_ref_types: dict[str, str] = field(default_factory=dict)
    candidate_statuses: dict[tuple[str, int], str] = field(default_factory=dict)
    candidate_audit_records: dict[str, dict[int, dict[str, Any]]] = field(default_factory=dict)
    candidate_cleanup_pool: dict[str, list[tuple[int, ReferenceImageAsset]]] = field(default_factory=dict)
    selection_ready_slots: set[str] = field(default_factory=set)
    portrait_seeds: list[str] = field(default_factory=list)
    env_seeds: list[str] = field(default_factory=list)
    seed_order_note: str | None = None
    active_candidate_slots: set[str] = field(default_factory=set)

    def publish_progress(self) -> None:
        from app.multiview import PURPOSE_VIDEO_INPUT
        from .seedance_pack import _dedupe_assets
        from .mode_selection import _reference_runtime_blocking

        if self.on_progress is None:
            return
        gallery = _dedupe_assets(list(self.selected) + list(self.evidence_assets))
        for asset in gallery:
            asset.shotId = asset.shotId or self.shot_id
            asset.episodeId = asset.episodeId or self.episode_id
            # 仅 video_input 用途默认选中
            if PURPOSE_VIDEO_INPUT in (asset.purposes or []) or asset.type == "previous_shot_frame":
                asset.selectedForSeedance = (
                    not asset.deleted
                    and not _reference_runtime_blocking(asset)
                )
            else:
                asset.selectedForSeedance = False
        visible_rejected = self.rejected_out or []
        for asset in visible_rejected:
            asset.selectedForSeedance = False
            asset.shotId = asset.shotId or self.shot_id
            asset.episodeId = asset.episodeId or self.episode_id
        self.on_progress(list(gallery), list(visible_rejected))

    def apply_keyframe_beat(self, asset: ReferenceImageAsset, slot_key: str) -> None:
        beat = self.beat_by_slot.get(slot_key)
        if not beat:
            return
        asset.keyframe_index = int(beat["beat_index"])
        asset.keyframe_total = int(beat["beat_total"])
        asset.keyframe_time_ratio = float(beat["time_ratio"])
        asset.keyframe_target_desc = str(beat["target_desc"])
        asset.qa = {**(asset.qa or {}), "keyframe_beat": dict(beat)}

    def candidate_record(
        self,
        slot_key: str,
        candidate_no: int,
        asset: ReferenceImageAsset,
        *,
        include_path: bool = True,
        status: str | None = None,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "candidate_no": candidate_no,
            "id": asset.id,
            "status": status or self.candidate_statuses.get((slot_key, candidate_no), "qa_pending"),
            "qa": asset.qa,
            "quality_score": asset.qualityScore,
        }
        if include_path and asset.path:
            record["path"] = asset.path
        return record

    def checkpoint_candidates(self, slot_key: str, status: str) -> None:
        from .mode_selection import KEYFRAME_PROMPT_CONTRACT_VERSION

        records = dict(self.candidate_audit_records.get(slot_key) or {})
        for candidate_no, asset in self.candidate_pool.get(slot_key, []):
            records[candidate_no] = self.candidate_record(slot_key, candidate_no, asset)
        target = self.candidate_targets.get(slot_key, len(records) or 1)
        self.slot_state[slot_key] = {
            **(self.slot_state.get(slot_key) or {}),
            "status": status,
            "type": self.candidate_ref_types.get(slot_key, "plot_key_frame"),
            "candidate_target": target,
            "candidate_count": len(records),
            "candidates": [records[n] for n in sorted(records)],
            "prompt_contract_version": KEYFRAME_PROMPT_CONTRACT_VERSION,
            "keyframe_contract_fingerprint": self.current_keyframe_fingerprint,
            **({"keyframe_beat": dict(self.beat_by_slot[slot_key])} if slot_key in self.beat_by_slot else {}),
        }
        self.existing_meta["reference_slots"] = self.slot_state

    def seeds_for(self, ref_type: str) -> list[str]:
        from .mode_selection import _dedupe_str

        seeds = (self.portrait_seeds + self.env_seeds) if ref_type in {"character", "plot_key_frame"} else list(self.env_seeds)
        return _dedupe_str(seeds)

    def rehydrate_candidate(
        self,
        slot_key: str,
        ref_type: str,
        candidate_no: int,
        record: dict[str, Any],
    ) -> ReferenceImageAsset | None:
        from app.multiview import PURPOSE_QA_ANCHOR
        from .mode_selection import KEYFRAME_PROMPT_CONTRACT_VERSION
        from .asset_lookup import _asset_from_path

        path = str(record.get("path") or "")
        if not path or not Path(path).is_file():
            return None
        try:
            asset = _asset_from_path(
                path=path,
                ref_type=ref_type,
                source="seedream_generated",
                quality_score=(
                    float(record.get("quality_score"))
                    if record.get("quality_score") is not None else None
                ),
                qa=record.get("qa") or {"overall": None, "status": "qa_pending", "resumed": True},
                purposes=[PURPOSE_QA_ANCHOR],
                required=True,
                slot_key=slot_key,
                entity_type="shot",
            )
        except (OSError, TypeError, ValueError):
            return None
        asset.id = str(record.get("id") or asset.id)
        asset.candidate_no = candidate_no
        asset.selectedForSeedance = False
        asset.dependency_manifest = self.manifest
        asset.prompt_contract_version = KEYFRAME_PROMPT_CONTRACT_VERSION
        asset.keyframe_contract_fingerprint = self.current_keyframe_fingerprint
        self.apply_keyframe_beat(asset, slot_key)
        return asset
