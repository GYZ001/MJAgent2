"""Compiler phase: builds source-coverage rows from beats and finalizes any coverage rows still missing after that pass."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.schemas import SourceCoverageDecision

from .models_core import IRBeat, IRScene
from .models_event import ScreenplayGenerationIR
from .prompt_context import _beats_for_event, _retain_source_segment_as_scene_context, _segment_ordinal


def _ir_build_source_coverage_rows(
    value: ScreenplayGenerationIR,
    beats_were_derived: bool,
    beat_by_key: dict[str, IRBeat],
    beat_ids: dict[str, str],
    scene_by_key: dict[str, IRScene],
    segments: dict[str, Any],
    expected_segment_ids: set[str],
    compiler_audit: list[dict[str, Any]],
) -> tuple[set[str], list[Any], "defaultdict[str, list[str]]"]:
    inferred_context_by_scene: defaultdict[str, list[str]] = defaultdict(list)
    seen_coverage: set[str] = set()
    coverage_rows: list[SourceCoverageDecision] = []
    for group in value.coverage:
        unknown_segments = set(group.source_segment_ids) - expected_segment_ids
        if unknown_segments:
            raise ValueError(f"coverage 引用了不存在的来源段：{sorted(unknown_segments)}")
        unknown_beats = set(group.beat_keys) - set(beat_by_key)
        if unknown_beats and beats_were_derived:
            previous_beat_keys = list(group.beat_keys)
            group.beat_keys = (
                []
                if group.disposition in {
                    "context", "duplicate", "audit_only",
                }
                else [
                    beat.key
                    for beat in value.beats
                    if set(beat.source_segment_ids).intersection(
                        group.source_segment_ids
                    )
                ]
            )
            compiler_audit.append({
                "path": "coverage.beat_keys",
                "operation": "rebind_legacy_reference",
                "from": previous_beat_keys,
                "to": group.beat_keys,
                "reason": "beats_are_compiler_derived",
            })
            unknown_beats = set(group.beat_keys) - set(beat_by_key)
        if unknown_beats:
            raise ValueError(f"coverage 引用了不存在的 beat：{sorted(unknown_beats)}")
        for segment_id in group.source_segment_ids:
            if segment_id in seen_coverage:
                raise ValueError(f"coverage 重复覆盖 {segment_id}")
            seen_coverage.add(segment_id)
            disposition = group.disposition
            owning_beat_keys = list(group.beat_keys)
            if disposition in {"deliver", "merge"} and not owning_beat_keys:
                owning_beat_keys = [
                    beat.key for beat in value.beats
                    if segment_id in beat.source_segment_ids
                ]
                if not owning_beat_keys:
                    disposition = "context"
                    group.projection_policy = "context_only"
            reason = group.reason
            if disposition == "context":
                reason = _retain_source_segment_as_scene_context(
                    segment_id,
                    reason=reason,
                    events=value.events,
                    scene_by_key=scene_by_key,
                    segments=segments,
                    inferred_context_by_scene=inferred_context_by_scene,
                )
            coverage_rows.append(SourceCoverageDecision(
                source_segment_id=segment_id,
                disposition=disposition,
                projection_policy=group.projection_policy,
                beat_ids=[beat_ids[key] for key in owning_beat_keys],
                duplicate_of=group.duplicate_of,
                reason=reason,
            ))
            if disposition != group.disposition or owning_beat_keys != group.beat_keys:
                compiler_audit.append({
                    "path": f"source_coverage.{segment_id}",
                    "operation": "normalize",
                    "from": {
                        "disposition": group.disposition,
                        "beat_keys": group.beat_keys,
                    },
                    "to": {
                        "disposition": disposition,
                        "beat_keys": owning_beat_keys,
                    },
                    "reason": "deterministic_coverage_link",
                })
    return seen_coverage, coverage_rows, inferred_context_by_scene


def _ir_finalize_missing_coverage_rows(
    value: ScreenplayGenerationIR,
    expected_segment_ids: set[str],
    seen_coverage: set[str],
    beat_by_key: dict[str, IRBeat],
    beat_ids: dict[str, str],
    coverage_rows: list[Any],
    scene_by_key: dict[str, IRScene],
    segments: dict[str, Any],
    inferred_context_by_scene: "defaultdict[str, list[str]]",
    compiler_audit: list[dict[str, Any]],
) -> None:
    missing_coverage = expected_segment_ids - seen_coverage
    for segment_id in sorted(missing_coverage):
        owning_beats = [
            beat for beat in value.beats
            if segment_id in beat.source_segment_ids
        ]
        if owning_beats:
            coverage_rows.append(SourceCoverageDecision(
                source_segment_id=segment_id,
                disposition="merge" if len(owning_beats) > 1 else "deliver",
                projection_policy="picture",
                beat_ids=[beat_ids[beat.key] for beat in owning_beats],
                duplicate_of=None,
                reason="由已声明该来源段的主线节拍确定性补全覆盖回链",
            ))
            continue

        owning_events = [
            event for event in value.events
            if segment_id in event.source_segment_ids
        ]
        if owning_events:
            related_beats = list(dict.fromkeys(
                beat.key
                for event in owning_events
                for beat in _beats_for_event(event, value.beats)
            ))
            for beat_key in related_beats:
                beat = beat_by_key[beat_key]
                if segment_id not in beat.source_segment_ids:
                    beat.source_segment_ids.append(segment_id)
            coverage_rows.append(SourceCoverageDecision(
                source_segment_id=segment_id,
                disposition="merge",
                projection_policy="picture",
                beat_ids=[beat_ids[key] for key in related_beats],
                duplicate_of=None,
                reason="该来源段已进入事件语义，确定性合并到对应主线节拍",
            ))
            continue

        coverage_rows.append(SourceCoverageDecision(
            source_segment_id=segment_id,
            disposition="context",
            projection_policy="context_only",
            beat_ids=[],
            duplicate_of=None,
            reason=_retain_source_segment_as_scene_context(
                segment_id,
                events=value.events,
                scene_by_key=scene_by_key,
                segments=segments,
                inferred_context_by_scene=inferred_context_by_scene,
            ),
        ))
        compiler_audit.append({
            "path": f"source_coverage.{segment_id}",
            "operation": "derive_context",
            "reason": "source_segment_not_owned_by_event_or_beat",
        })
    coverage_rows.sort(
        key=lambda item: _segment_ordinal(item.source_segment_id)
    )
