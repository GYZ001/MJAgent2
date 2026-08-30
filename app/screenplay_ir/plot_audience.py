"""Compiler phase: builds plot beats/audience setup, then the audience paths that consume them."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app import config
from app.renderability import chunk_dialogue_turns
from app.schemas import InformationItem, KeyDialogueChain, KeyDialogueTurn, PlotSpineBeat, ScriptScene

from .models_core import IRSceneUnit
from .models_event import IRAudiencePrior, IREvent, IRExperience, IRMetadata, ScreenplayGenerationIR
from .prompt_context import _screenplay_action_text


def _ir_build_plot_beats_and_audience_setup(
    value: ScreenplayGenerationIR,
    episode: dict[str, Any],
    episode_no: int,
    metadata: IRMetadata,
    beat_ids: dict[str, str],
    effective_render_policy: dict[str, str],
    units_by_event: "defaultdict[str, list[IRSceneUnit]]",
    event_by_key: dict[str, IREvent],
    event_keys_by_scene: "defaultdict[str, list[str]]",
    event_ids: dict[str, str],
    scene_outlines: list[ScriptScene],
    script_lines: list[str],
    information_ledger: list[InformationItem],
    key_line_event: dict[int, str],
    dialogue_chain_order: list[str],
    dialogue_chain_rows: dict[str, list[KeyDialogueTurn]],
    dialogue_chain_topics: dict[str, str],
    dialogue_chain_scenes: dict[str, str],
    prior_by_key: dict[str, IRAudiencePrior],
    final_adapted_prop_id: str,
    first_adapted_prop_id: str,
) -> tuple[
    list[KeyDialogueChain],
    list[PlotSpineBeat],
    str,
    list[str],
    str,
    list[dict[str, Any]],
    dict[str, str],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[str],
    str,
    list[dict[str, Any]],
    IRExperience,
    float,
]:
    dialogue_chains: list[KeyDialogueChain] = []
    for key in dialogue_chain_order:
        turns = dialogue_chain_rows[key]
        # 按**发言**而不是按片段数切分：上面 `_split_spoken_line` 刚把一句
        # 台词按单镜口播容量切成了多段，若在这里按片段数硬切，边界会落进
        # 一次发言内部，把半句话分给下一条 chain（EP4 实测 DC3/DC4、
        # DC5/DC6 两处都是这样切断的）。
        for offset, chunk_turns in enumerate(chunk_dialogue_turns(turns)):
            chunk = [turn.model_copy(deep=True) for turn in chunk_turns]
            if (
                offset
                and chunk
                and chunk[0].function == "response"
            ):
                chunk[0].function = "statement"
            dialogue_chains.append(KeyDialogueChain(
                chain_id=f"DC{len(dialogue_chains) + 1}",
                scene_id=dialogue_chain_scenes[key],
                topic=(
                    dialogue_chain_topics[key]
                    + ("（续）" if offset else "")
                ),
                turns=chunk,
            ))

    info_ids_by_event: defaultdict[str, list[str]] = defaultdict(list)
    for item in information_ledger:
        info_ids_by_event[item.event_id].append(item.info_id)
    key_ids_by_event: defaultdict[str, list[str]] = defaultdict(list)
    key_position = 0
    for chain in dialogue_chains:
        for _turn in chain.turns:
            key_position += 1
            local_event_key = key_line_event.get(key_position)
            if local_event_key:
                key_ids_by_event[event_ids[local_event_key]].append(
                    f"KL{key_position:02d}"
                )

    plot_beats = []
    for beat in value.beats:
        related_events = [
            event
            for event in value.events
            if set(event.source_segment_ids).intersection(
                beat.source_segment_ids
            )
        ]
        related_event_ids = [event_ids[event.key] for event in related_events]
        priority = (
            "causal"
            if any(event.event_priority == "causal" for event in related_events)
            else "supporting"
            if any(event.event_priority == "supporting" for event in related_events)
            else "connective"
        )
        related_render_policies = [
            effective_render_policy[event.key]
            for event in related_events
        ]
        render_policy = (
            "standalone"
            if "standalone" in related_render_policies
            else "merge_adjacent"
            if "merge_adjacent" in related_render_policies
            else "exclude_from_spine"
        )
        plot_beats.append(PlotSpineBeat(
            beat_id=beat_ids[beat.key],
            who=beat.who,
            does=beat.does,
            turn=beat.turn,
            must_keep=beat.must_keep,
            narrative_layer="story",
            event_priority=priority,
            render_policy=render_policy,
            source_segment_ids=beat.source_segment_ids,
            purpose=beat.purpose,
            information_ids=list(dict.fromkeys(
                info_id
                for event_id in related_event_ids
                for info_id in info_ids_by_event[event_id]
            )),
            key_line_ids=list(dict.fromkeys(
                key_id
                for event_id in related_event_ids
                for key_id in key_ids_by_event[event_id]
            )),
        ))

    full_script_text = "\n".join(script_lines).strip()
    for beat in plot_beats:
        delivery = f"{beat.who}{beat.does}"
        source_backed_units = [
            unit
            for event in value.events
            if set(event.source_segment_ids).intersection(
                beat.source_segment_ids
            )
            for unit in units_by_event.get(event.key, [])
            if unit.text.strip()
        ]
        if any(
            (
                _screenplay_action_text(unit.text)
                if unit.kind == "action"
                else unit.text.strip()
            ) in full_script_text
            for unit in source_backed_units
        ):
            continue
        if beat.must_keep and delivery and delivery not in full_script_text:
            scene_index = next(
                (
                    index for index, scene in enumerate(value.scenes)
                    if any(
                        set(event_by_key[event_key].source_segment_ids).intersection(
                            beat.source_segment_ids
                        )
                        for event_key in event_keys_by_scene.get(scene.key, [])
                    )
                ),
                0,
            )
            heading = scene_outlines[scene_index].scene_heading
            insertion = _screenplay_action_text(
                f"{beat.who}{beat.does}，{beat.turn}。"
            )
            marker = full_script_text.find(heading) + len(heading)
            full_script_text = (
                full_script_text[:marker]
                + "\n"
                + insertion
                + full_script_text[marker:]
            )

    # key_lines 与 document 投影共用同一派生算法：以 dialogue_chains 为权威源，
    # 依据已定稿的 full_script_text 正文出现顺序排列。必须在 full_script_text 完成
    # must_keep 插入后再派生，两条路径才能对同一输入逐字段相等（消除结构顺序漂移）。
    # 延迟 import：validators 在函数级反向引用本模块，顶层直连会成环。
    from app.validators import derive_key_lines

    key_lines = derive_key_lines(dialogue_chains, full_script_text)

    scope_id = str(episode.get("id") or f"episode-{episode_no}")
    dramatic_questions = [{
        "dramatic_question_id": "DQ-1",
        "question_text": metadata.dramatic_question,
        "target_proposition_ids": [final_adapted_prop_id],
        "open_anchor": {"type": "event", "id": event_ids[value.events[0].key]},
        "intended_resolution_scope_id": scope_id,
        "desired_state_while_open": "unknown",
        "resolution_anchor": {
            "type": "event",
            "id": event_ids[value.events[-1].key],
        },
        "status": "resolved",
    }]

    prior_ids = {
        key: f"AP-{position}"
        for position, key in enumerate(prior_by_key, start=1)
    }
    audience_priors: list[dict[str, Any]] = []
    audience_states: list[dict[str, Any]] = []
    audience_paths: list[dict[str, Any]] = []
    target_delta_ids: list[str] = []
    last_event_id = event_ids[value.events[-1].key]
    setup_memory = [{
        "proposition_id": first_adapted_prop_id,
        "retention_confidence": 1.0,
    }]
    experience = value.experience or IRExperience(
        director_objective="让观众理解本集完整因果链及最终局势变化",
        satisfaction_criteria="冷观众能复述关键事件、人物目标与最终状态变化",
    )
    experience_processing_s = min(
        float(config.VIDEO_DURATION_MAX_S),
        max(0.5, float(experience.required_processing_s or 0)),
    )
    return (
        dialogue_chains,
        plot_beats,
        full_script_text,
        key_lines,
        scope_id,
        dramatic_questions,
        prior_ids,
        audience_priors,
        audience_states,
        audience_paths,
        target_delta_ids,
        last_event_id,
        setup_memory,
        experience,
        experience_processing_s,
    )


def _ir_build_audience_paths(
    value: ScreenplayGenerationIR,
    prior_by_key: dict[str, IRAudiencePrior],
    prior_ids: dict[str, str],
    scope_id: str,
    event_ids: dict[str, str],
    event_source_prop_id: dict[str, str],
    event_evidence_ids: dict[str, str],
    event_adapted_prop_id: dict[str, str],
    adapted_ids_in_order: list[str],
    experience: IRExperience,
    experience_processing_s: float,
    setup_memory: list[dict[str, Any]],
    last_event_id: str,
    audience_priors: list[dict[str, Any]],
    audience_states: list[dict[str, Any]],
    audience_paths: list[dict[str, Any]],
    target_delta_ids: list[str],
) -> None:
    for position, (key, prior) in enumerate(prior_by_key.items(), start=1):
        prior_id = prior_ids[key]
        state_in_id = f"AS-{position}-IN"
        state_out_id = f"AS-{position}-OUT"
        audience_priors.append({
            "audience_prior_id": prior_id,
            "scope_id": scope_id,
            "audience_description": prior.description,
            "assumed_known_proposition_ids": (
                [event_source_prop_id[value.events[0].key]]
                if position > 1 else []
            ),
            "assumed_unknown_proposition_ids": adapted_ids_in_order,
            "familiarity_assumptions": prior.familiarity_assumptions,
            "language_and_context_assumptions": (
                prior.language_and_context_assumptions
            ),
            "attention_memory_assumptions": prior.attention_memory_assumptions,
            "calibration_source": "needs_review",
        })
        in_beliefs = [
            {
                "proposition_id": proposition_id,
                "stance": "unknown",
                "confidence": 0.0,
                "evidence_ids": [],
            }
            for proposition_id in adapted_ids_in_order
        ]
        out_beliefs = [
            {
                "proposition_id": proposition_id,
                "stance": prior.target_stance,
                "confidence": prior.target_confidence,
                "evidence_ids": [
                    event_evidence_ids[next(
                        event.key for event in value.events
                        if event_adapted_prop_id[event.key] == proposition_id
                    )]
                ],
            }
            for proposition_id in adapted_ids_in_order
        ]
        audience_states.extend([
            {
                "audience_state_id": state_in_id,
                "audience_prior_id": prior_id,
                "anchor": {
                    "type": "event",
                    "id": event_ids[value.events[0].key],
                },
                "beliefs": in_beliefs,
                "causal_hypotheses": [],
                "character_goal_hypotheses": {},
                "spatial_model": {},
                "temporal_model": {},
                "active_question_ids": ["DQ-1"],
                "working_memory": setup_memory,
                "attention_residue_ids": [],
                "affective_state": {},
            },
            {
                "audience_state_id": state_out_id,
                "audience_prior_id": prior_id,
                "anchor": {"type": "event", "id": last_event_id},
                "beliefs": out_beliefs,
                "causal_hypotheses": [],
                "character_goal_hypotheses": {},
                "spatial_model": {},
                "temporal_model": {},
                # Question closure is expressed by DramaticQuestion.status and
                # the episode arc. Keeping the active set stable avoids
                # inventing a second target delta solely for a mechanical
                # snapshot-field change.
                "active_question_ids": ["DQ-1"],
                "working_memory": setup_memory,
                "attention_residue_ids": [],
                "affective_state": {},
            },
        ])
        delta_id = f"XD-{position}-1"
        target_delta_ids.append(delta_id)
        audience_paths.append({
            "audience_path_id": f"XP-{position}-1",
            "audience_prior_id": prior_id,
            "audience_state_in_id": state_in_id,
            "audience_state_out_target_id": state_out_id,
            "target_deltas": [{
                "target_delta_id": delta_id,
                "dimension": "belief",
                "proposition_ids": adapted_ids_in_order,
                "description": experience.director_objective,
                "from_state": {
                    proposition_id: {
                        "stance": "unknown",
                        "confidence": 0.0,
                        "evidence_ids": [],
                    }
                    for proposition_id in adapted_ids_in_order
                },
                "to_state": {
                    proposition_id: {
                        "stance": prior.target_stance,
                        "confidence": prior.target_confidence,
                        "evidence_ids": [
                            event_evidence_ids[next(
                                event.key for event in value.events
                                if event_adapted_prop_id[event.key] == proposition_id
                            )]
                        ],
                    }
                    for proposition_id in adapted_ids_in_order
                },
                "target_confidence": prior.target_confidence,
                "required_processing_s": experience_processing_s,
                "deadline_event_id": last_event_id,
                "primary_delivery_window_id": f"RW-{len(value.events)}",
                "custom_dimension": None,
            }],
        })
