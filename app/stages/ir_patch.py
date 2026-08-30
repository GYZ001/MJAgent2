"""剧本 IR 保真——IR 修复补丁的合同模型、上下文组装与合并。"""
from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, Field

from app import textmatch
from app.source_excerpt import (
    index_source_segments,
    structural_front_matter_ids,
)
from app.screenplay_ir import (
    IR_LOCAL_SOURCE_WINDOW,
    IR_MIN_ADAPTED_SOURCE_RATIO,
    IRScene,
    IRSceneUnit,
    ScreenplayGenerationIR,
)


class _IRFidelityInsertion(BaseModel):
    scene_key: str
    insert_after_event_key: str | None = None
    units: list[IRSceneUnit] = Field(default_factory=list)


class _IRFidelityPatch(BaseModel):
    insertions: list[_IRFidelityInsertion] = Field(default_factory=list)
    new_scenes: list[IRScene] = Field(default_factory=list)


class _IRScenePartition(BaseModel):
    key: str
    scene_heading: str
    story_function: str
    summary: str
    conflict: str
    turn: str
    unit_indexes: list[int]


class _IRSceneReplacement(BaseModel):
    scene_key: str
    scenes: list[_IRScenePartition]


class _IRScenePartitionPlan(BaseModel):
    replacements: list[_IRSceneReplacement]


def _ir_fidelity_patch_context(
    candidate: ScreenplayGenerationIR,
    source_text: str,
) -> dict[str, Any]:
    segments = index_source_segments(source_text)
    front_matter_ids = structural_front_matter_ids(segments)
    dramatic = [
        segment for segment in segments
        if segment.segment_id not in front_matter_ids
    ]
    flat_units = [
        (scene, unit)
        for scene in candidate.scenes
        for unit in scene.units
    ]
    owned = {
        source_id
        for _scene, unit in flat_units
        for source_id in unit.source_segment_ids
    }
    missing = [
        segment.segment_id
        for segment in dramatic
        if segment.segment_id not in owned
    ]
    windows: list[dict[str, Any]] = []
    for start in range(0, len(dramatic), IR_LOCAL_SOURCE_WINDOW):
        window = dramatic[start:start + IR_LOCAL_SOURCE_WINDOW]
        window_ids = {segment.segment_id for segment in window}
        source_chars = sum(
            len(textmatch.condense(segment.text))
            for segment in window
        )
        existing_units = [
            {
                "scene_key": scene.key,
                "event_key": unit.event_key,
                "kind": unit.kind,
                "text": unit.text,
                "source_segment_ids": unit.source_segment_ids,
                "speaker_key": unit.speaker_key,
            }
            for scene, unit in flat_units
            if window_ids.intersection(unit.source_segment_ids)
        ]
        adapted_chars = sum(
            len(textmatch.condense(str(item["text"])))
            for item in existing_units
        )
        target_chars = math.ceil(
            source_chars * IR_MIN_ADAPTED_SOURCE_RATIO
        )
        missing_here = [
            source_id for source_id in missing if source_id in window_ids
        ]
        if adapted_chars >= target_chars and not missing_here:
            continue
        windows.append({
            "source_range": (
                f"{window[0].segment_id}-{window[-1].segment_id}"
            ),
            "source_chars": source_chars,
            "existing_adapted_chars": adapted_chars,
            "minimum_final_adapted_chars": target_chars,
            "minimum_additional_chars": max(
                0, target_chars - adapted_chars,
            ),
            "missing_source_ids": missing_here,
            "source_segments": [
                {
                    "source_segment_id": segment.segment_id,
                    "text": segment.text,
                }
                for segment in window
            ],
            "existing_units": existing_units,
        })
    return {
        "missing_source_ids": missing,
        "identities": [
            {
                "key": identity.key,
                "display_name": identity.display_name,
                "authority_id": identity.authority_id,
                "voice_canonical": identity.voice_canonical,
            }
            for identity in candidate.identities
        ],
        "scenes": [
            {
                "key": scene.key,
                "scene_heading": scene.scene_heading,
                "summary": scene.summary,
            }
            for scene in candidate.scenes
        ],
        "windows_requiring_expansion": windows,
    }


def _select_fidelity_blueprint_plans(
    context: dict[str, Any],
    plans: list[Any],
    *,
    candidate_scene_count: int,
) -> tuple[list[Any], list[Any], list[Any], set[str]]:
    remaining_plans = plans[candidate_scene_count:]
    repair_source_ids = set(context["missing_source_ids"])
    repair_source_ids.update(
        str(segment["source_segment_id"])
        for window in context["windows_requiring_expansion"]
        for segment in window["source_segments"]
    )
    internal_plans = [
        plan
        for plan in plans[:candidate_scene_count]
        if repair_source_ids.intersection(plan.source_segment_ids)
    ]
    selected_plans = [] if internal_plans else remaining_plans[:6]
    return (
        remaining_plans,
        internal_plans,
        selected_plans,
        repair_source_ids,
    )


def _merge_ir_fidelity_patch(
    candidate: ScreenplayGenerationIR,
    patch: _IRFidelityPatch,
    source_text: str,
    *,
    round_no: int,
) -> int:
    segments = index_source_segments(source_text)
    source_order = {
        segment.segment_id: index
        for index, segment in enumerate(segments)
    }
    scenes = {scene.key: scene for scene in candidate.scenes}
    existing_units = [
        (scene, unit)
        for scene in candidate.scenes
        for unit in scene.units
    ]
    occupied_keys = {
        unit.event_key for _scene, unit in existing_units
    }
    inserted = 0
    existing_scene_keys = set(scenes)
    for new_scene in patch.new_scenes:
        if new_scene.key in existing_scene_keys or not new_scene.units:
            continue
        valid_units: list[IRSceneUnit] = []
        for patch_unit in new_scene.units:
            source_ids = list(patch_unit.source_segment_ids)
            if (
                not source_ids
                or any(source_id not in source_order for source_id in source_ids)
            ):
                continue
            if len(source_ids) != len(set(source_ids)):
                raise ValueError(
                    "保真补写 unit.source_segment_ids 不得重复"
                )
            if candidate.source_scene_owners:
                owner_scene_keys = {
                    candidate.source_scene_owners.get(source_id)
                    for source_id in source_ids
                }
                if owner_scene_keys != {new_scene.key}:
                    raise ValueError(
                        "保真补写 new_scene 违反 source 唯一归属："
                        f"{source_ids} -> {sorted(str(value) for value in owner_scene_keys)}，"
                        f"target={new_scene.key}"
                    )
            inserted += 1
            event_key = f"fidelity-r{round_no}-{inserted}"
            while event_key in occupied_keys:
                inserted += 1
                event_key = f"fidelity-r{round_no}-{inserted}"
            occupied_keys.add(event_key)
            patch_unit.event_key = event_key
            patch_unit.source_segment_ids = source_ids
            valid_units.append(patch_unit)
        if not valid_units:
            continue
        new_scene.units = valid_units
        candidate.scenes.append(new_scene)
        scenes[new_scene.key] = new_scene
        existing_scene_keys.add(new_scene.key)
        existing_units.extend(
            (new_scene, unit) for unit in valid_units
        )
    for insertion in patch.insertions:
        for patch_unit in insertion.units:
            source_ids = list(patch_unit.source_segment_ids)
            if (
                not source_ids
                or any(source_id not in source_order for source_id in source_ids)
            ):
                continue
            if len(source_ids) != len(set(source_ids)):
                raise ValueError(
                    "保真补写 unit.source_segment_ids 不得重复"
                )
            target_scene = None
            if candidate.source_scene_owners:
                owner_scene_keys = {
                    candidate.source_scene_owners.get(source_id)
                    for source_id in source_ids
                }
                if len(owner_scene_keys) != 1 or None in owner_scene_keys:
                    raise ValueError(
                        "保真补写 unit 跨越多个 source owner："
                        f"{source_ids} -> {sorted(str(value) for value in owner_scene_keys)}"
                    )
                owner_scene_key = next(iter(owner_scene_keys))
                target_scene = scenes.get(owner_scene_key)
                if target_scene is None:
                    raise ValueError(
                        f"保真补写缺少 source owner scene：{owner_scene_key}"
                    )
            else:
                target_index = min(
                    source_order[source_id] for source_id in source_ids
                )
                nearest_scene = min(
                    (
                        (
                            min(
                                abs(source_order[source_id] - target_index)
                                for source_id in unit.source_segment_ids
                                if source_id in source_order
                            ),
                            scene,
                        )
                        for scene, unit in existing_units
                        if any(
                            source_id in source_order
                            for source_id in unit.source_segment_ids
                        )
                    ),
                    key=lambda item: item[0],
                    default=(0, scenes.get(insertion.scene_key)),
                )[1]
                target_scene = (
                    nearest_scene or scenes.get(insertion.scene_key)
                )
            if target_scene is None:
                continue
            inserted += 1
            event_key = f"fidelity-r{round_no}-{inserted}"
            while event_key in occupied_keys:
                inserted += 1
                event_key = f"fidelity-r{round_no}-{inserted}"
            occupied_keys.add(event_key)
            patch_unit.event_key = event_key
            patch_unit.source_segment_ids = source_ids
            target_scene.units.append(patch_unit)
            existing_units.append((target_scene, patch_unit))

    for scene in candidate.scenes:
        scene.units.sort(
            key=lambda unit: min(
                (
                    source_order[source_id]
                    for source_id in unit.source_segment_ids
                    if source_id in source_order
                ),
                default=len(segments),
            )
        )
    candidate.events = []
    candidate.beats = []
    candidate.coverage = []
    return inserted
