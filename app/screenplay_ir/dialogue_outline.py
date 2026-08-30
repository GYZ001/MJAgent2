"""Compiler phase: builds dialogue chains and the per-scene script outline."""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from app import config, textmatch
from app.schemas import KeyDialogueTurn, ScriptScene

from .constants import _DIALOGUE_FUNCTIONS
from .identity_resolver import _IRIdentityResolver
from .models_core import IRIdentity
from .models_event import IREvent, ScreenplayGenerationIR
from .prompt_context import _dialogue_source_text, _screenplay_action_text, _split_spoken_line


def _ir_build_dialogue_and_scene_outline(
    value: ScreenplayGenerationIR,
    source_text: str,
    identity_resolver: _IRIdentityResolver,
    identity_by_key: dict[str, IRIdentity],
    event_by_key: dict[str, IREvent],
    inferred_context_by_scene: "defaultdict[str, list[str]]",
    compiler_audit: list[dict[str, Any]],
) -> tuple[
    dict[str, list[KeyDialogueTurn]],
    list[str],
    dict[str, str],
    dict[str, str],
    dict[int, str],
    list[str],
    list[ScriptScene],
    "defaultdict[str, list[str]]",
]:
    dialogue_chain_rows: dict[str, list[KeyDialogueTurn]] = {}
    dialogue_chain_order: list[str] = []
    dialogue_chain_topics: dict[str, str] = {}
    dialogue_chain_scenes: dict[str, str] = {}
    key_line_event: dict[int, str] = {}
    script_lines: list[str] = []
    scene_outlines: list[ScriptScene] = []
    event_keys_by_scene: defaultdict[str, list[str]] = defaultdict(list)
    for event in value.events:
        event_keys_by_scene[event.scene_key].append(event.key)

    for scene_position, scene in enumerate(value.scenes, start=1):
        original_heading = scene.scene_heading.strip()
        heading_suffix = re.sub(
            r"^【场[^】]*】\s*",
            "",
            original_heading,
        )
        heading = f"【场{scene_position}】{heading_suffix}"
        if heading != original_heading:
            compiler_audit.append({
                "path": f"scenes.{scene.key}.scene_heading",
                "operation": "renumber",
                "from": original_heading,
                "to": heading,
                "reason": "published_scene_numbers_are_contiguous",
            })
            scene.scene_heading = heading
        script_lines.append(heading)
        for unit in scene.units:
            if unit.kind == "action":
                action_text = _screenplay_action_text(unit.text)
                if action_text:
                    script_lines.append(action_text)
                if action_text != unit.text.strip():
                    compiler_audit.append({
                        "path": f"scenes.{scene.key}.units.action",
                        "operation": "remove_directing_vocabulary",
                        "from": unit.text.strip(),
                        "to": action_text,
                        "reason": "screenplay_body_contract",
                    })
                continue
            speaker = identity_resolver.display(unit.speaker_key or "")
            local_chain_key = (
                unit.chain_key.strip() or f"scene-{scene_position}"
            )
            chain_key = f"{scene.key}:{local_chain_key}"
            if chain_key not in dialogue_chain_rows:
                dialogue_chain_rows[chain_key] = []
                dialogue_chain_order.append(chain_key)
                dialogue_chain_topics[chain_key] = (
                    scene.story_function or scene.summary
                )
                dialogue_chain_scenes[chain_key] = f"SC{scene_position:02d}"
            dialogue_source_evidence = _dialogue_source_text(
                unit.source_text,
                source_text,
            )
            spoken_parts = _split_spoken_line(
                unit.text,
                max_chars=config.MAX_SPOKEN_CHARS_PER_SHOT,
            )
            if len(spoken_parts) > 1:
                compiler_audit.append({
                    "path": f"scenes.{scene.key}.units.dialogue",
                    "operation": "split_by_spoken_capacity",
                    "parts": len(spoken_parts),
                    "reason": "downstream_single_shot_voice_capacity",
                })
            for part_index, line in enumerate(spoken_parts):
                script_lines.append(f"{speaker}：{line}")
                dialogue_chain_rows[chain_key].append(KeyDialogueTurn(
                    speaker=speaker,
                    line=line,
                    function=(
                        unit.function
                        if (
                            part_index == 0
                            and unit.function in _DIALOGUE_FUNCTIONS
                        )
                        else "statement"
                    ),
                    source_text=dialogue_source_evidence,
                ))
                key_line_event[
                    sum(
                        len(dialogue_chain_rows[key])
                        for key in dialogue_chain_order
                    )
                ] = unit.event_key
        script_lines.append("")

        source_basis = scene.source_basis.strip()
        if len(source_basis) < 8:
            source_ids = list(dict.fromkeys(
                source_id
                for event_key in event_keys_by_scene.get(scene.key, [])
                for source_id in event_by_key[event_key].source_segment_ids
            ))
            source_basis = (
                "、".join(source_ids)
                + " 对应的授权原文事件与场次状态"
            )
            compiler_audit.append({
                "path": f"scenes.{scene.key}.source_basis",
                "operation": "derive",
                "reason": "scene_event_source_ownership",
            })
        scene_event_keys = event_keys_by_scene.get(scene.key, [])
        entry_state = scene.entry_state.strip() or (
            event_by_key[scene_event_keys[0]].precondition_state
            if scene_event_keys else ""
        )
        exit_state = scene.exit_state.strip() or (
            event_by_key[scene_event_keys[-1]].resulting_state
            if scene_event_keys else ""
        )
        if entry_state != scene.entry_state.strip():
            compiler_audit.append({
                "path": f"scenes.{scene.key}.entry_state",
                "operation": "derive",
                "reason": "first_scene_event_precondition",
            })
        if exit_state != scene.exit_state.strip():
            compiler_audit.append({
                "path": f"scenes.{scene.key}.exit_state",
                "operation": "derive",
                "reason": "last_scene_event_result",
            })
        scene.entry_state = entry_state
        scene.exit_state = exit_state
        scene_character_tokens = list(dict.fromkeys([
            *scene.character_keys,
            *[
                token
                for event_key in event_keys_by_scene.get(scene.key, [])
                for token in (
                    *event_by_key[event_key].actor_keys,
                    *event_by_key[event_key].target_keys,
                )
                if str(token).strip() != "audience"
            ],
        ]))
        visible_scene_characters = list(dict.fromkeys(
            identity_resolver.display(token)
            for token in scene_character_tokens
            if (
                identity_by_key[identity_resolver.key(token)].visual_policy
                != "offscreen_only"
            )
        ))
        context_requirements = list(dict.fromkeys([
            *scene.context_requirements,
            *inferred_context_by_scene.get(scene.key, []),
        ]))
        if not context_requirements:
            context_requirements = [
                f"先建立{heading}的时间、地点与空间关系",
                (
                    "本场人物关系与当前局势："
                    + (scene.summary or scene.story_function)
                ),
            ]
            compiler_audit.append({
                "path": f"scenes.{scene.key}.context_requirements",
                "operation": "derive",
                "reason": "scene_heading_and_summary_define_required_context",
            })
        scene_turn = scene.turn.strip()
        if len(textmatch.condense(scene_turn)) < 8:
            scene_turn = (
                f"本场结束时，{exit_state}，"
                f"并完成「{scene.story_function or scene.summary}」"
            )
            compiler_audit.append({
                "path": f"scenes.{scene.key}.turn",
                "operation": "derive",
                "reason": "scene_exit_state_defines_handoff_change",
            })
            scene.turn = scene_turn
        scene_outlines.append(ScriptScene(
            scene_no=scene_position,
            scene_heading=heading,
            story_function=scene.story_function,
            characters=visible_scene_characters,
            summary=scene.summary,
            conflict=scene.conflict,
            turn=scene_turn,
            source_basis=source_basis,
            previous_scene_exit_state=scene.previous_scene_exit_state,
            opening_image=scene.opening_image or entry_state,
            agency_contracts=scene.agency_contracts,
            entry_state=entry_state,
            exit_state=exit_state,
            context_requirements=context_requirements,
        ))
    return (
        dialogue_chain_rows,
        dialogue_chain_order,
        dialogue_chain_topics,
        dialogue_chain_scenes,
        key_line_event,
        script_lines,
        scene_outlines,
        event_keys_by_scene,
    )
