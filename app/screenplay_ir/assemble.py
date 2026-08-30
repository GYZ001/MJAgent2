"""Compiler phase: assembles the final EpisodeScreenplay from every upstream phase's output."""
from __future__ import annotations

from typing import Any

from app.schemas import (
    EpisodeScreenplay,
    InformationItem,
    KeyDialogueChain,
    NARRATIVE_CONTRACT_VERSION,
    NarrativeContinuityPlan,
    PlotSpine,
    PlotSpineBeat,
    ScriptScene,
    StoryEvent,
    VoiceCanonical,
)

from .constants import IR_COMPILER_VERSION, IR_VERSION
from .models_event import IRMetadata, ScreenplayGenerationIR


def _ir_assemble_episode_screenplay(
    value: ScreenplayGenerationIR,
    episode_no: int,
    metadata: IRMetadata,
    scope_id: str,
    key_lines: list[str],
    dialogue_chains: list[KeyDialogueChain],
    plot_beats: list[PlotSpineBeat],
    coverage_rows: list[Any],
    scene_outlines: list[ScriptScene],
    full_script_text: str,
    source_evidence: list[dict[str, Any]],
    propositions: list[dict[str, Any]],
    adaptation_decisions: list[dict[str, Any]],
    state_facts: list[dict[str, Any]],
    narrative_evidence: list[dict[str, Any]],
    dramatic_questions: list[dict[str, Any]],
    narrative_events: list[dict[str, Any]],
    atomic_actions: list[dict[str, Any]],
    character_states: list[dict[str, Any]],
    character_beliefs: list[dict[str, Any]],
    audience_priors: list[dict[str, Any]],
    audience_states: list[dict[str, Any]],
    experience_intents: list[dict[str, Any]],
    assimilation_tasks: list[dict[str, Any]],
    readability_windows: list[dict[str, Any]],
    setup_payoff_contracts: list[dict[str, Any]],
    scene_contracts: list[dict[str, Any]],
    arc_contracts: list[dict[str, Any]],
    identity_contracts: list[dict[str, Any]],
    information_ledger: list[InformationItem],
    voice_bible: list[VoiceCanonical],
    legacy_events: list[StoryEvent],
    compiler_audit: list[dict[str, Any]],
) -> EpisodeScreenplay:
    narrative_plan = NarrativeContinuityPlan.model_validate({
        "contract_version": NARRATIVE_CONTRACT_VERSION,
        "scope_id": scope_id,
        "source_evidence": source_evidence,
        "propositions": propositions,
        "adaptation_decisions": adaptation_decisions,
        "state_facts": state_facts,
        "initial_state_fact_ids": ["F-0"],
        "evidence": narrative_evidence,
        "dramatic_questions": dramatic_questions,
        "events": narrative_events,
        "atomic_actions": atomic_actions,
        "action_relation_audits": [],
        "character_states": character_states,
        "character_beliefs": character_beliefs,
        "audience_priors": audience_priors,
        "audience_states": audience_states,
        "experience_intents": experience_intents,
        "assimilation_tasks": assimilation_tasks,
        "readability_windows": readability_windows,
        "setup_payoff_contracts": setup_payoff_contracts,
        "scene_contracts": scene_contracts,
        "arc_contracts": arc_contracts,
        "identity_contracts": identity_contracts,
    })

    script = EpisodeScreenplay(
        episode_no=episode_no,
        id=scope_id,
        mode="full_script",
        source_text_range=value.format_version or IR_VERSION,
        title=metadata.title,
        logline=metadata.logline,
        script_format_note=metadata.script_format_note,
        dramatic_question=metadata.dramatic_question,
        protagonist_goal=metadata.protagonist_goal,
        obstacle=metadata.obstacle,
        stakes=metadata.stakes,
        key_lines=key_lines,
        dialogue_chains=dialogue_chains,
        key_plot_points=[
            f"{beat.who}{beat.does}，{beat.turn}"
            for beat in plot_beats if beat.must_keep
        ],
        plot_spine=PlotSpine(
            episode_premise=metadata.episode_premise,
            spine_beats=plot_beats,
            must_keep_ending=metadata.must_keep_ending,
            drop_list=metadata.drop_list,
        ),
        source_coverage=coverage_rows,
        scene_outline=scene_outlines,
        full_script_text=full_script_text,
        character_state_changes=[
            f"{event.precondition_state} → {event.resulting_state}"
            for event in value.events
        ],
        emotional_curve=metadata.emotional_curve,
        ending_hook=metadata.ending_hook,
        source_basis=metadata.source_basis,
        adaptation_direction=metadata.adaptation_direction,
        opening=metadata.opening,
        development=metadata.development,
        conflict=metadata.conflict,
        climax=metadata.climax,
        episode_premise=metadata.episode_premise,
        events=legacy_events,
        information_ledger=information_ledger,
        voice_bible=voice_bible,
        approved_adaptations=metadata.approved_adaptations,
        forbidden_additions=metadata.forbidden_additions,
        narrative_plan=narrative_plan,
    )
    object.__setattr__(
        script,
        "_ir_compiler_audit",
        [*value.normalization_log, *compiler_audit],
    )
    object.__setattr__(script, "_ir_compiler_version", IR_COMPILER_VERSION)
    return script
