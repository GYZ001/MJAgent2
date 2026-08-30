"""Compiler phase: builds experience/assimilation/readability/setup-payoff/scene/arc contracts, then voice and identity contracts."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app import config
from app.schemas import VoiceCanonical

from .identity_resolver import _IRIdentityResolver
from .models_core import IRIdentity
from .models_event import IRAudiencePrior, IREvent, IRExperience, ScreenplayGenerationIR


def _ir_build_experience_and_scene_contracts(
    value: ScreenplayGenerationIR,
    identity_resolver: _IRIdentityResolver,
    scope_id: str,
    event_ids: dict[str, str],
    event_evidence_ids: dict[str, str],
    adapted_ids_in_order: list[str],
    audience_paths: list[dict[str, Any]],
    experience: IRExperience,
    last_event_id: str,
    target_delta_ids: list[str],
    first_adapted_prop_id: str,
    final_adapted_prop_id: str,
    event_by_key: dict[str, IREvent],
    event_keys_by_scene: "defaultdict[str, list[str]]",
    event_character_state_ids: "defaultdict[str, list[str]]",
    event_state_subject_ids: dict[str, list[str]],
    event_adapted_prop_id: dict[str, str],
    environment_subject_id: str,
    prior_by_key: dict[str, IRAudiencePrior],
    prior_ids: dict[str, str],
    scene_ids: dict[str, str],
    experience_processing_s: float,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    experience_intents = [{
        "experience_intent_id": "XI-1",
        "scope_id": scope_id,
        "anchor_event_ids": [event_ids[event.key] for event in value.events],
        "director_objective": experience.director_objective,
        "attention_target_ids": adapted_ids_in_order,
        "audience_paths": audience_paths,
        "withheld_propositions": [],
        "forbidden_misconceptions": experience.forbidden_misconceptions,
    }]
    assimilation_tasks = [
        {
            "assimilation_task_id": f"AT-{position}",
            "experience_intent_id": "XI-1",
            "audience_path_id": path["audience_path_id"],
            "target_delta_id": path["target_deltas"][0]["target_delta_id"],
            "required_prior_proposition_ids": [],
            "downstream_dependency_event_ids": [last_event_id],
            "satisfaction_criteria": experience.satisfaction_criteria,
            "status": "planned",
        }
        for position, path in enumerate(audience_paths, start=1)
    ]

    readability_windows: list[dict[str, Any]] = []
    for position, event in enumerate(value.events, start=1):
        is_last = position == len(value.events)
        window_delta_ids = target_delta_ids if is_last else []
        event_readability_s = min(
            float(config.VIDEO_DURATION_MAX_S),
            max(0.5, float(event.readability_s or 0)),
        )
        scheduled = max(
            event_readability_s,
            experience_processing_s if is_last else 0.0,
        )
        readability_windows.append({
            "readability_window_id": f"RW-{position}",
            "event_ids": [event_ids[event.key]],
            "proposition_ids": [event_adapted_prop_id[event.key]],
            "target_delta_ids": window_delta_ids,
            "shot_ids": [],
            "attention_target_ids": [event_adapted_prop_id[event.key]],
            "evidence_ids": [event_evidence_ids[event.key]],
            "scheduled_processing_s": scheduled,
            "planned_available_s": scheduled,
            "competing_attention_ids": [],
            "readability_reason": (
                f"在后续事件使用前交付并留出处理时间：{event.observable_claim}"
            ),
            "status": "planned",
        })

    setup_payoff_contracts: list[dict[str, Any]] = []
    if len(value.events) > 1:
        setup_payoff_contracts.append({
            "setup_payoff_id": "SP-1",
            "setup_proposition_ids": [first_adapted_prop_id],
            "setup_event_ids": [event_ids[value.events[0].key]],
            "payoff_event_ids": [last_event_id],
            "intended_inference_ids": [final_adapted_prop_id],
            "retention_deadline_event_id": last_event_id,
            "minimum_retention_confidence": 0.5,
            "recall_needed": False,
            "status": "paid_off",
        })

    scene_contracts: list[dict[str, Any]] = []
    for position, scene in enumerate(value.scenes, start=1):
        scene_event_keys = event_keys_by_scene.get(scene.key) or [
            value.events[min(position - 1, len(value.events) - 1)].key
        ]
        turn_event_key = scene_event_keys[-1]
        scene_prop_id = event_adapted_prop_id[turn_event_key]
        state_ids = [
            state_id
            for event_key in scene_event_keys
            for state_id in event_character_state_ids[event_key]
        ]
        scene_character_ids = [
            identity_resolver.id(key) for key in scene.character_keys
        ]
        scene_state_subject_ids = [
            subject_id
            for event_key in scene_event_keys
            for subject_id in event_state_subject_ids[event_key]
        ]
        pov_id = (
            scene_character_ids
            or [
                subject_id
                for subject_id in scene_state_subject_ids
                if subject_id != environment_subject_id
            ]
        )
        point_of_view_character_id = pov_id[0] if pov_id else None
        relationship_deltas = (
            []
            if scene.entry_state.strip() != scene.exit_state.strip()
            else [{"description": scene.turn or "场次关系发生变化"}]
        )
        scene_contracts.append({
            "scene_id": scene_ids[scene.key],
            "applicability": "applies",
            "not_applicable_reason": None,
            "alternative_dramatic_function": None,
            "scene_question_id": "DQ-1",
            "point_of_view_character_id": point_of_view_character_id,
            "audience_state_paths": [
                {
                    "audience_prior_id": prior_ids[key],
                    "audience_state_in_id": f"AS-{prior_position}-IN",
                    "audience_state_out_target_id": f"AS-{prior_position}-OUT",
                }
                for prior_position, key in enumerate(prior_by_key, start=1)
            ],
            "character_state_in_ids": state_ids[:1],
            "goal_proposition_ids": [scene_prop_id],
            "obstacle_proposition_ids": [scene_prop_id],
            "stakes_proposition_ids": [scene_prop_id],
            "pressure_curve": [{
                "anchor": {
                    "type": "event",
                    "id": event_ids[turn_event_key],
                },
                "value": event_by_key[turn_event_key].salience,
            }],
            "turn_event_ids": [event_ids[turn_event_key]],
            "value_polarity_in": scene.entry_state,
            "value_polarity_out": scene.exit_state,
            "relationship_deltas": relationship_deltas,
            "character_state_out_ids": state_ids[-1:],
            "scene_button": scene.turn,
        })

    if len(value.events) > 1:
        arc_contracts = [{
            "arc_id": "ARC-EPISODE",
            "scope": "episode",
            "applicability": "applies",
            "not_applicable_reason": None,
            "alternative_dramatic_function": None,
            "core_question_ids": ["DQ-1"],
            "promise_proposition_ids": [first_adapted_prop_id],
            "escalation_event_ids": [
                event_ids[event.key] for event in value.events[:-1]
            ],
            "climax_event_ids": [last_event_id],
            "payoff_contract_ids": ["SP-1"],
            "pressure_curve": [
                {
                    "anchor": {"type": "event", "id": event_ids[event.key]},
                    "value": event.salience,
                }
                for event in value.events
            ],
            "information_density_curve": [
                {
                    "anchor": {"type": "event", "id": event_ids[event.key]},
                    "value": min(1.0, 0.4 + 0.6 * event.salience),
                }
                for event in value.events
            ],
            "processing_beats": [{
                "anchor": {
                    "type": "event",
                    "id": event_ids[value.events[0].key],
                },
                "purpose": "建立本集因果起点并供观众处理关键信息",
            }],
            "ending_hook_question_ids": [],
            "resolved_question_ids": ["DQ-1"],
            "carried_question_ids": [],
        }]
    else:
        arc_contracts = [{
            "arc_id": "ARC-EPISODE",
            "scope": "episode",
            "applicability": "not_applicable",
            "not_applicable_reason": "本集仅包含一个不可再拆的完整事件",
            "alternative_dramatic_function": "以单一状态变化完成本集交付",
            "core_question_ids": ["DQ-1"],
            "promise_proposition_ids": [],
            "escalation_event_ids": [],
            "climax_event_ids": [],
            "payoff_contract_ids": [],
            "pressure_curve": [],
            "information_density_curve": [],
            "processing_beats": [],
            "ending_hook_question_ids": [],
            "resolved_question_ids": ["DQ-1"],
            "carried_question_ids": [],
        }]
    return (
        experience_intents,
        assimilation_tasks,
        readability_windows,
        setup_payoff_contracts,
        scene_contracts,
        arc_contracts,
    )


def _ir_build_voice_and_identity_contracts(
    value: ScreenplayGenerationIR,
    identity_resolver: _IRIdentityResolver,
    identity_by_key: dict[str, IRIdentity],
    bible_by_name: dict[str, Any],
    final_identity_ids: dict[str, str],
    ordered_used_keys: list[str],
    event_speaker_keys: "defaultdict[str, list[str]]",
    event_source_evidence_id: dict[str, str],
    event_adapted_prop_id: dict[str, str],
    event_decision_id: dict[str, str],
) -> tuple[list[VoiceCanonical], list[dict[str, Any]]]:
    spoken_keys = {
        identity_resolver.key(unit.speaker_key or "")
        for scene in value.scenes
        for unit in scene.units
        if unit.kind == "dialogue" and unit.speaker_key
    }
    voice_bible = [
        VoiceCanonical(
            speaker_id=final_identity_ids[key],
            display_name=identity.display_name,
            voice_canonical=(
                identity.voice_canonical
                or (
                    bible_by_name[identity.display_name].speech_style
                    if identity.display_name in bible_by_name else ""
                )
                or "符合当前身份与情境的稳定普通话声线"
            ),
            language="普通话",
            role_type=identity.role_type,
        )
        for key, identity in identity_by_key.items()
        if key in spoken_keys
    ]

    identity_contracts: list[dict[str, Any]] = []
    for key in ordered_used_keys:
        identity = identity_by_key[key]
        related_events = [
            event for event in value.events
            if key in {
                *[
                    identity_resolver.key(token) for token in event.actor_keys
                    if str(token).strip() != "audience"
                ],
                *[
                    identity_resolver.key(token) for token in event.target_keys
                    if str(token).strip() != "audience"
                ],
                *[
                    identity_resolver.key(token)
                    for token
                    in event.text_provenance.content_owner_keys
                ],
                *event_speaker_keys.get(event.key, []),
            }
        ]
        if not related_events:
            related_events = [value.events[0]]
        related_source_ids = list(dict.fromkeys(
            event_source_evidence_id[event.key] for event in related_events
        ))
        related_prop_ids = list(dict.fromkeys(
            event_adapted_prop_id[event.key] for event in related_events
        ))
        related_decision_ids = list(dict.fromkeys(
            event_decision_id[event.key] for event in related_events
        ))
        is_bible = identity.display_name in bible_by_name
        visual_policy = "canonical" if is_bible else identity.visual_policy
        asset_requirement = "required" if is_bible else identity.asset_requirement
        visual_canonical = (
            bible_by_name[identity.display_name].appearance_canonical
            if is_bible else identity.visual_canonical
        )
        if visual_policy == "offscreen_only":
            asset_requirement = "forbidden"
            visual_canonical = ""
        identity_contracts.append({
            "identity_id": final_identity_ids[key],
            "display_name": identity.display_name,
            "kind": identity.kind,
            "visual_policy": visual_policy,
            "visual_canonical": visual_canonical,
            "asset_requirement": asset_requirement,
            "voice_ids": (
                [final_identity_ids[key]] if key in spoken_keys else []
            ),
            "evidence": {
                "source_evidence_ids": related_source_ids,
                "proposition_ids": related_prop_ids,
                "adaptation_decision_ids": related_decision_ids,
                "rationale": (
                    identity.rationale
                    or "该身份被本集事件、场次或对白实际引用"
                ),
            },
        })
    return voice_bible, identity_contracts
