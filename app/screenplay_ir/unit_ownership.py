"""Compiler phase: validates, narrows and normalizes which source segments each scene unit is allowed to own."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any

from app import textmatch
from app.source_excerpt import align_source_excerpt

from .compile_setup import _ir_split_discontinuous_units
from .constants import (
    IR_LOCAL_SOURCE_WINDOW,
    IR_MAX_SOURCE_SEGMENTS_PER_UNIT,
    IR_MIN_ADAPTED_SOURCE_RATIO,
    IR_MIN_LOCAL_ADAPTED_SOURCE_RATIO,
    ScreenplayIRFidelityError,
)
from .models_core import IRScene, IRSceneUnit
from .models_event import ScreenplayGenerationIR


def _ir_validate_unit_source_ownership(
    value: ScreenplayGenerationIR,
    flat_units: list[tuple[IRScene, IRSceneUnit]],
    all_source_ids: set[str],
    expected_source_ids: set[str],
) -> "defaultdict[str, int]":
    unknown_source_ids = {
        source_id
        for _scene, unit in flat_units
        for source_id in unit.source_segment_ids
        if source_id not in all_source_ids
    }
    if unknown_source_ids:
        raise ScreenplayIRFidelityError(
            "IR units 引用了不存在的细粒度来源段："
            + "、".join(sorted(unknown_source_ids)[:20])
        )
    if value.source_scene_owners:
        ownership_payload = {
            "source_scene_owners": value.source_scene_owners,
                "source_semantics": value.source_semantics,
            "scene_derivations": value.scene_derivations,
        }
        actual_ownership_hash = hashlib.sha256(
            json.dumps(
                ownership_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if (
            value.source_ownership_hash
            and value.source_ownership_hash
            != actual_ownership_hash
        ):
            raise ScreenplayIRFidelityError(
                "IR source_ownership_hash 与结构化 owner 合同不一致"
            )
        missing_owner_contract = (
            expected_source_ids - set(value.source_scene_owners)
        )
        if missing_owner_contract:
            raise ScreenplayIRFidelityError(
                "IR source owner 合同漏掉来源段："
                + "、".join(sorted(missing_owner_contract)[:20])
            )
        owner_conflicts = [
            (
                source_id,
                value.source_scene_owners.get(source_id),
                scene.key,
            )
            for scene, unit in flat_units
            for source_id in unit.source_segment_ids
            if value.source_scene_owners.get(source_id) != scene.key
        ]
        if owner_conflicts:
            source_id, expected_owner, consumer = owner_conflicts[0]
            raise ScreenplayIRFidelityError(
                f"IR 来源唯一归属冲突：{source_id} owner="
                f"{expected_owner or '未定义'}，consumer={consumer}"
            )
    units_without_source = [
        f"{scene.key}:{unit.event_key or index}"
        for index, (scene, unit) in enumerate(flat_units, start=1)
        if not unit.source_segment_ids
    ]
    if units_without_source:
        raise ScreenplayIRFidelityError(
            "IR v1.3 每个 unit 必须声明 source_segment_ids："
            + "、".join(units_without_source[:20])
        )
    owned_source_ids = {
        source_id
        for _scene, unit in flat_units
        for source_id in unit.source_segment_ids
    }
    missing_source_ids = expected_source_ids - owned_source_ids
    if missing_source_ids:
        raise ScreenplayIRFidelityError(
            "IR v1.3 正文 units 漏掉细粒度来源段："
            + "、".join(sorted(missing_source_ids)[:20])
            + (
                f"（另有 {len(missing_source_ids) - 20} 段）"
                if len(missing_source_ids) > 20 else ""
            )
        )
    source_owner_counts: defaultdict[str, int] = defaultdict(int)
    return source_owner_counts


def _ir_narrow_redundant_unit_ownership(
    compiler_audit: list[dict[str, Any]],
    flat_units: list[tuple[IRScene, IRSceneUnit]],
    source_order: dict[str, int],
    source_owner_counts: "defaultdict[str, int]",
    expected_source_ids: set[str],
    segments: dict[str, Any],
) -> None:
    for _scene, unit in flat_units:
        for source_id in set(unit.source_segment_ids):
            source_owner_counts[source_id] += 1
    for scene, unit in flat_units:
        source_ids = list(dict.fromkeys(unit.source_segment_ids))
        source_positions = [
            source_order[source_id] for source_id in source_ids
        ]
        if source_positions == sorted(source_positions):
            contiguous_runs: list[list[str]] = []
            for source_id in source_ids:
                if (
                    not contiguous_runs
                    or source_order[source_id]
                    != (
                        source_order[contiguous_runs[-1][-1]] + 1
                    )
                ):
                    contiguous_runs.append([source_id])
                else:
                    contiguous_runs[-1].append(source_id)
            viable_runs = [
                run
                for run in contiguous_runs
                if all(
                    (
                        source_id in run
                        or source_owner_counts[source_id] > 1
                    )
                    for source_id in source_ids
                )
            ]
            if len(contiguous_runs) > 1:
                candidate_runs = viable_runs or contiguous_runs
                retained_run = max(
                    candidate_runs,
                    key=lambda run: (
                        int(
                            unit.kind == "dialogue"
                            and bool(unit.source_text.strip())
                            and unit.source_text.strip() in "\n".join(
                                segments[source_id].text
                                for source_id in run
                            )
                        ),
                        sum(
                            source_owner_counts[source_id] == 1
                            for source_id in run
                        ),
                        len(run),
                        max(
                            (
                                textmatch.bigram_coverage(
                                    unit.text,
                                    segments[source_id].text,
                                )
                                for source_id in run
                            ),
                            default=0.0,
                        ),
                    ),
                )
                unit.source_segment_ids = retained_run
                compiler_audit.append({
                    "path": (
                        f"scenes.{scene.key}.units."
                        f"{unit.event_key}.source_segment_ids"
                    ),
                    "operation": (
                        "drop_redundant_noncontiguous_ownership"
                    ),
                    "from": source_ids,
                    "to": retained_run,
                    "reason": (
                        "retain_single_best_matching_contiguous_source_run"
                    ),
                })
                source_ids = retained_run
        if (
            len(source_ids) <= IR_MAX_SOURCE_SEGMENTS_PER_UNIT
            or not all(
                source_owner_counts[source_id] > 1
                for source_id in source_ids
            )
        ):
            continue
        scored_source_ids = sorted(
            source_ids,
            key=lambda source_id: max(
                textmatch.longest_run_ratio(
                    unit.text,
                    segments[source_id].text,
                ),
                textmatch.bigram_coverage(
                    unit.text,
                    segments[source_id].text,
                ),
            ),
            reverse=True,
        )
        anchor_index = source_ids.index(scored_source_ids[0])
        window_start = min(
            max(0, anchor_index - 1),
            max(0, len(source_ids) - 4),
        )
        unit.source_segment_ids = source_ids[
            window_start:window_start + 4
        ]
        compiler_audit.append({
            "path": (
                f"scenes.{scene.key}.units.{unit.event_key}."
                "source_segment_ids"
            ),
            "operation": "narrow_redundant_source_ownership",
            "from_count": len(source_ids),
            "to_count": len(unit.source_segment_ids),
            "reason": (
                "detailed_units_already_own_every_source_segment"
            ),
        })
    normalized_owned_source_ids = {
        source_id
        for _scene, unit in flat_units
        for source_id in unit.source_segment_ids
    }
    normalized_missing_source_ids = (
        expected_source_ids - normalized_owned_source_ids
    )
    if normalized_missing_source_ids:
        raise ScreenplayIRFidelityError(
            "IR v1.3 正文 units 漏掉细粒度来源段："
            + "、".join(sorted(normalized_missing_source_ids)[:20])
            + (
                f"（另有 {len(normalized_missing_source_ids) - 20} 段）"
                if len(normalized_missing_source_ids) > 20 else ""
            )
        )


def _ir_verify_unit_source_fidelity(
    compiler_audit: list[dict[str, Any]],
    flat_units: list[tuple[IRScene, IRSceneUnit]],
    source_order: dict[str, int],
    source_owner_counts: "defaultdict[str, int]",
    segments: dict[str, Any],
    segments_list: list[Any],
    dramatic_segments: list[Any],
) -> None:
    first_owner_position: dict[str, int] = {}
    for unit_position, (_scene, unit) in enumerate(flat_units):
        unit.source_segment_ids = list(dict.fromkeys(
            unit.source_segment_ids
        ))
        unit_positions = [
            source_order[source_id]
            for source_id in unit.source_segment_ids
        ]
        if unit_positions != sorted(unit_positions):
            raise ValueError(
                "IR v1.3 unit 内 source_segment_ids 必须按原文顺序："
                f"{unit.event_key}"
            )
        if len(unit_positions) > IR_MAX_SOURCE_SEGMENTS_PER_UNIT:
            raise ValueError(
                "IR v1.3 单个 unit 合并来源段过多："
                f"{unit.event_key}={len(unit_positions)}"
            )
        if unit_positions and (
            unit_positions[-1] - unit_positions[0] + 1
            != len(unit_positions)
        ):
            raise ValueError(
                "IR v1.3 单个 unit 只能合并连续来源段："
                f"{unit.event_key}"
            )
        if unit.kind == "dialogue" and unit.source_text.strip():
            declared_source = "\n".join(
                segments[source_id].text
                for source_id in unit.source_segment_ids
            )
            if unit.source_text.strip() not in declared_source:
                aligned = align_source_excerpt(
                    unit.source_text,
                    declared_source,
                    min_match_chars=2,
                )
                if aligned is None:
                    scene_source_ids = {
                        source_id
                            for scene_unit in _scene.units
                        for source_id
                        in scene_unit.source_segment_ids
                    }
                    exact_matches = [
                        source_id
                        for source_id, segment in segments.items()
                        if (
                            unit.source_text.strip()
                            in segment.text
                            and source_id in scene_source_ids
                        )
                    ]
                    global_exact_matches = [
                        source_id
                        for source_id, segment in segments.items()
                        if unit.source_text.strip() in segment.text
                    ]
                    selected_exact_matches = (
                        exact_matches
                        if len(exact_matches) == 1
                        else global_exact_matches
                    )
                    if (
                        len(selected_exact_matches) != 1
                        or not all(
                            source_owner_counts[source_id] > 1
                            for source_id
                            in unit.source_segment_ids
                        )
                    ):
                        raise ValueError(
                            "IR v1.3 对白 source_text 不属于声明的来源段："
                            f"{unit.event_key}"
                        )
                    before_source_ids = list(
                        unit.source_segment_ids
                    )
                    unit.source_segment_ids = selected_exact_matches
                    compiler_audit.append({
                        "path": (
                            f"scenes.units.{unit.event_key}."
                            "source_segment_ids"
                        ),
                        "operation": (
                            "rebind_dialogue_exact_source"
                        ),
                        "from": before_source_ids,
                        "to": selected_exact_matches,
                        "reason": (
                            "verbatim_dialogue_uniquely_owned_by_"
                            "another_source"
                        ),
                    })
                else:
                    compiler_audit.append({
                        "path": (
                            f"scenes.units.{unit.event_key}.source_text"
                        ),
                        "operation": "align_within_declared_source",
                        "from": unit.source_text,
                        "to": aligned.excerpt,
                        "reason": (
                            "citation_joined_discontinuous_source_phrases"
                        ),
                    })
                    unit.source_text = aligned.excerpt
        for source_id in unit.source_segment_ids:
            first_owner_position.setdefault(
                source_id, unit_position,
            )
    ownership_positions = [
        first_owner_position[segment.segment_id]
        for segment in dramatic_segments
    ]
    if ownership_positions != sorted(ownership_positions):
        raise ValueError(
            "IR v1.3 来源段首次进入正文的顺序与原文不一致"
        )
    source_chars = sum(
        len(textmatch.condense(segment.text))
        for segment in dramatic_segments
    )
    adapted_chars = sum(
        len(textmatch.condense(unit.text))
        for _scene, unit in flat_units
    )
    adapted_ratio = adapted_chars / max(source_chars, 1)
    if (
        source_chars >= 200
        and adapted_ratio < IR_MIN_ADAPTED_SOURCE_RATIO
    ):
        raise ScreenplayIRFidelityError(
            "IR v1.3 正文过度压缩："
            f"改编净文本/原文={adapted_ratio:.1%}，"
            f"最低要求={IR_MIN_ADAPTED_SOURCE_RATIO:.0%}"
        )
    weak_windows: list[str] = []
    for start in range(0, len(dramatic_segments), IR_LOCAL_SOURCE_WINDOW):
        window = dramatic_segments[
            start:start + IR_LOCAL_SOURCE_WINDOW
        ]
        window_ids = {segment.segment_id for segment in window}
        window_source_chars = sum(
            len(textmatch.condense(segment.text))
            for segment in window
        )
        owner_units = [
            unit
            for _scene, unit in flat_units
            if window_ids.intersection(unit.source_segment_ids)
        ]
        window_adapted_chars = sum(
            len(textmatch.condense(unit.text))
            for unit in owner_units
        )
        window_ratio = (
            window_adapted_chars / max(window_source_chars, 1)
        )
        if (
            window_source_chars >= 300
            and window_ratio < IR_MIN_LOCAL_ADAPTED_SOURCE_RATIO
        ):
            weak_windows.append(
                f"{window[0].segment_id}-{window[-1].segment_id}"
                f"={window_ratio:.1%}"
            )
    if weak_windows:
        raise ScreenplayIRFidelityError(
            "IR v1.3 存在局部剧情过度压缩："
            + "、".join(weak_windows[:10])
        )
    compiler_audit.append({
        "path": "scenes.units",
        "operation": "verify_source_fidelity",
        "source_segment_count": len(segments_list),
        "adapted_source_ratio": round(adapted_ratio, 4),
        "local_window_size": IR_LOCAL_SOURCE_WINDOW,
        "reason": "prevent_declared_coverage_without_dramatization",
    })


def _ir_normalize_strict_unit_ownership(
    value: ScreenplayGenerationIR,
    compiler_audit: list[dict[str, Any]],
    segments: dict[str, Any],
    segments_list: list[Any],
    dramatic_segments: list[Any],
    expected_source_ids: set[str],
    all_source_ids: set[str],
) -> list[tuple[IRScene, IRSceneUnit]]:
    """Verbatim orchestration of the original `if strict_unit_ownership:`
    block (2,634-3,152 lines in the pre-refactor source): split
    discontinuous units, validate ownership, narrow redundant ownership,
    then verify source fidelity."""
    flat_units, source_order = _ir_split_discontinuous_units(
        value, compiler_audit, segments, segments_list,
    )
    source_owner_counts = _ir_validate_unit_source_ownership(
        value, flat_units, all_source_ids, expected_source_ids,
    )
    _ir_narrow_redundant_unit_ownership(
        compiler_audit, flat_units, source_order, source_owner_counts,
        expected_source_ids, segments,
    )
    _ir_verify_unit_source_fidelity(
        compiler_audit, flat_units, source_order, source_owner_counts,
        segments, segments_list, dramatic_segments,
    )
    return flat_units
