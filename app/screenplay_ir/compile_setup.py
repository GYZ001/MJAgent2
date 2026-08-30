"""Compiler phase: prepares compile-time setup (metadata, segments, ownership mode) and splits discontinuous scene units."""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from app import textmatch
from app.narrative_blueprint import _normalize_source_segment_id
from app.source_excerpt import align_source_excerpt, index_compact_source_segments, index_source_segments

from .constants import ScreenplayIRFidelityError, _AUDIT_SOURCE_SEMANTICS
from .contract_validation import (
    _canonical_source_semantic_identity,
    scene_heading_has_multiple_locations,
    screenplay_ir_version_key,
)
from .models_core import IRScene, IRSceneUnit
from .models_event import IRMetadata, ScreenplayGenerationIR
from .prompt_context import _default_metadata


def _ir_prepare_compile_setup(
    value: ScreenplayGenerationIR,
    episode: dict[str, Any],
    source_text: str,
    compiler_audit: list[dict[str, Any]],
) -> tuple[
    IRMetadata,
    dict[str, Any],
    int,
    str,
    dict[str, Any],
    list[Any],
    set[str],
    bool,
    bool,
]:
    metadata = value.metadata or _default_metadata(episode)
    episode_no = int(episode.get("episode_no") or value.episode_no or 0)
    from app.portraits import screenplay_character_resolutions_for_source

    episode = dict(episode)
    episode["character_resolutions"] = (
        screenplay_character_resolutions_for_source(
            list(episode.get("character_resolutions") or []),
            episode_no=episode_no,
            source_text=source_text,
        )
    )
    if value.episode_no and value.episode_no != episode_no:
        compiler_audit.append({
            "path": "episode_no",
            "operation": "bind_to_authority_input",
            "from": value.episode_no,
            "to": episode_no,
            "reason": "request_scope_is_authoritative",
        })
        value.episode_no = episode_no
    if not value.scenes:
        raise ValueError("IR scenes 不能为空")
    format_version = str(value.format_version or "")
    version_key = screenplay_ir_version_key(format_version)
    strict_unit_ownership = version_key >= (1, 3)
    typed_visual_unit_contract = version_key >= (1, 5)
    segments_list = (
        index_compact_source_segments(source_text)
        if format_version.startswith("screenplay-generation-ir.v1.2")
        else index_source_segments(source_text)
    )
    segments = {item.segment_id: item for item in segments_list}
    audit_only_source_ids = {
        _normalize_source_segment_id(source_id)
        for group in value.coverage
        if group.disposition == "audit_only"
        for source_id in group.source_segment_ids
    }
    annotated_audit_identities = {
        _canonical_source_semantic_identity(
            source_id,
            _AUDIT_SOURCE_SEMANTICS,
        )
        for annotation in value.source_audit_annotations
        for source_id in annotation.source_segment_ids
    }
    coverage_audit_identities = {
        _canonical_source_semantic_identity(
            source_id,
            _AUDIT_SOURCE_SEMANTICS,
        )
        for group in value.coverage
        if group.disposition == "audit_only"
        for source_id in group.source_segment_ids
    }
    if (
        value.source_audit_annotations
        and annotated_audit_identities != coverage_audit_identities
    ):
        raise ValueError(
            "source_audit_annotations 与 audit-only coverage 不一致"
        )
    unknown_audit_sources = audit_only_source_ids - set(segments)
    if unknown_audit_sources:
        raise ValueError(
            "audit-only coverage 引用了不存在的来源段："
            + "、".join(sorted(unknown_audit_sources))
        )
    if strict_unit_ownership:
        leaked_audit_units = [
            unit.unit_key or unit.event_key
            for scene in value.scenes
            for unit in scene.units
            if audit_only_source_ids.intersection(
                _normalize_source_segment_id(source_id)
                for source_id in unit.source_segment_ids
            )
        ]
        if leaked_audit_units:
            raise ValueError(
                "audit-only 来源不得进入 scene units："
                + "、".join(leaked_audit_units)
            )
        multi_location_scenes = [
            scene.key
            for scene in value.scenes
            if scene_heading_has_multiple_locations(scene.scene_heading)
        ]
        if multi_location_scenes:
            raise ScreenplayIRFidelityError(
                "IR v1.3 场次标题包含多个不连续地点，需要按连续时空重新分场："
                + "、".join(multi_location_scenes)
            )
    if strict_unit_ownership and value.events:
        compiler_audit.append({
            "path": "events",
            "operation": "discard_model_projection",
            "count": len(value.events),
            "reason": "v1.3_scene_units_are_the_only_authored_timeline",
        })
        value.events = []
    if strict_unit_ownership and value.beats:
        compiler_audit.append({
            "path": "beats",
            "operation": "discard_persisted_derived_projection",
            "count": len(value.beats),
            "reason": "v1.3_events_and_beats_are_rebuilt_from_scene_units",
        })
        value.beats = []
    return (
        metadata,
        episode,
        episode_no,
        format_version,
        segments,
        segments_list,
        audit_only_source_ids,
        strict_unit_ownership,
        typed_visual_unit_contract,
    )


def _ir_split_discontinuous_units(
    value: ScreenplayGenerationIR,
    compiler_audit: list[dict[str, Any]],
    segments: dict[str, Any],
    segments_list: list[Any],
) -> tuple[list[tuple[IRScene, IRSceneUnit]], dict[str, int]]:
    source_order = {
        segment.segment_id: index
        for index, segment in enumerate(segments_list)
    }
    for scene in value.scenes:
        normalized_units: list[IRSceneUnit] = []
        for unit in scene.units:
            positions = [
                source_order[source_id]
                for source_id in unit.source_segment_ids
                if source_id in source_order
            ]
            discontinuous = (
                len(positions) > 1
                and positions[-1] - positions[0] + 1
                != len(set(positions))
            )
            if not discontinuous:
                normalized_units.append(unit)
                continue
            clauses = [
                clause.strip()
                for clause in re.split(
                    r"(?<=[，。！？；])",
                    unit.text,
                )
                if clause.strip()
            ]
            assignments: dict[str, list[str]] = defaultdict(list)
            for clause in clauses:
                ranked = sorted(
                    (
                        (
                            max(
                                textmatch.longest_run_ratio(
                                    clause,
                                    segments[source_id].text,
                                ),
                                textmatch.bigram_coverage(
                                    clause,
                                    segments[source_id].text,
                                ),
                            ),
                            source_id,
                        )
                        for source_id in unit.source_segment_ids
                    ),
                    reverse=True,
                )
                if ranked and ranked[0][0] >= 0.08:
                    assignments[ranked[0][1]].append(clause)
            declared_ids = list(dict.fromkeys(
                unit.source_segment_ids
            ))
            if not all(assignments.get(source_id) for source_id in declared_ids):
                normalized_units.append(unit)
                continue
            split_units: list[IRSceneUnit] = []
            dialogue_excerpts: dict[str, str] = {}
            if unit.kind == "dialogue":
                for source_id in declared_ids:
                    aligned = align_source_excerpt(
                        unit.source_text or unit.text,
                        segments[source_id].text,
                        min_match_chars=2,
                    )
                    if aligned is None:
                        dialogue_excerpts = {}
                        break
                    dialogue_excerpts[source_id] = aligned.excerpt
                if len(dialogue_excerpts) != len(declared_ids):
                    normalized_units.append(unit)
                    continue
            for part_no, source_id in enumerate(
                sorted(declared_ids, key=source_order.__getitem__),
                start=1,
            ):
                split_unit = unit.model_copy(deep=True)
                split_unit.event_key = (
                    f"{unit.event_key}-source-part-{part_no}"
                )
                split_unit.source_segment_ids = [source_id]
                split_unit.text = "".join(assignments[source_id])
                if unit.kind == "dialogue":
                    split_unit.source_text = dialogue_excerpts[source_id]
                    if part_no > 1 and split_unit.function == "response":
                        split_unit.function = "statement"
                split_units.append(split_unit)
            normalized_units.extend(split_units)
            compiler_audit.append({
                "path": f"scenes.{scene.key}.units.{unit.event_key}",
                "operation": "split_discontinuous_source_unit",
                "from": unit.source_segment_ids,
                "to": [
                    split_unit.source_segment_ids
                    for split_unit in split_units
                ],
                "reason": (
                    "restore_intervening_source_playback_order"
                ),
            })
        scene.units = sorted(
            normalized_units,
            key=lambda item: min(
                (
                    source_order[source_id]
                    for source_id in item.source_segment_ids
                    if source_id in source_order
                ),
                default=len(segments_list),
            ),
        )
    flat_units = [
        (scene, unit)
        for scene in value.scenes
        for unit in scene.units
    ]
    return flat_units, source_order
