"""The public entry point: compile_screenplay_ir, the orchestrator that sequences every phase above."""
from __future__ import annotations

from typing import Any

from app.schemas import Bible, EpisodeScreenplay

from .assemble import _ir_assemble_episode_screenplay
from .compile_setup import _ir_prepare_compile_setup
from .coverage_rows import _ir_build_source_coverage_rows, _ir_finalize_missing_coverage_rows
from .dialogue_outline import _ir_build_dialogue_and_scene_outline
from .event_derivation import _ir_derive_events_from_scene_units, _ir_isolate_paratext_events
from .event_indexing import (
    _ir_derive_beats_and_priors,
    _ir_derive_missing_event_fields,
    _ir_index_scenes_events_identities,
)
from .event_relations import (
    _ir_build_identity_resolver,
    _ir_index_scene_units_and_collect_identities,
    _ir_validate_and_derive_event_relations,
)
from .evidence_compile import _ir_compile_event_evidence_and_adaptation, _ir_compute_prop_order_and_render_policy
from .experience_contracts import _ir_build_experience_and_scene_contracts, _ir_build_voice_and_identity_contracts
from .models_event import ScreenplayGenerationIR
from .plot_audience import _ir_build_audience_paths, _ir_build_plot_beats_and_audience_setup
from .state_facts import _ir_compile_state_facts_and_actions


def compile_screenplay_ir(
    value: ScreenplayGenerationIR,
    *,
    episode: dict[str, Any],
    source_text: str,
    bible: Bible,
    audit: list[dict[str, Any]] | None = None,
) -> EpisodeScreenplay:
    """Compile compact semantic IR into the unchanged published contract.

    This is an orchestrator: each phase of the original monolithic compiler
    is now a standalone helper function above.  The sequence and the data
    each phase reads/writes is unchanged from the pre-refactor source; only
    the decomposition into named, independently readable steps is new.
    """
    compiler_audit = audit if audit is not None else []
    if value.legacy_screenplay is not None:
        legacy = value.legacy_screenplay.model_copy(deep=True)
        legacy.id = legacy.id or str(episode.get("id") or "")
        return legacy

    (
        metadata,
        episode,
        episode_no,
        format_version,
        segments,
        segments_list,
        audit_only_source_ids,
        strict_unit_ownership,
        typed_visual_unit_contract,
    ) = _ir_prepare_compile_setup(value, episode, source_text, compiler_audit)

    # `identity_display` is only ever *populated* when events must be
    # derived from scene units; see the `else` branch below for why an
    # empty fallback is safe when the model supplied events directly.
    if not value.events:
        identity_display = _ir_derive_events_from_scene_units(
            value, episode, source_text, compiler_audit, segments,
            segments_list, audit_only_source_ids, format_version,
            strict_unit_ownership, typed_visual_unit_contract,
        )
    else:
        # The original nested-scope local was only ever read lazily, deep
        # inside a conditional branch of the next phase; on this path it was
        # never populated *and never dereferenced* by any reachable input.
        # Extracting that phase into a function with an explicit parameter
        # forces eager binding, so an empty dict (identical to
        # `dict.get(key, default)` behavior on an unpopulated mapping) is
        # substituted here instead of leaving the name unbound.
        identity_display = {}

    _ir_isolate_paratext_events(value, metadata, compiler_audit)

    (
        scene_by_key, event_by_key, identity_by_key,
        units_by_event, event_keys_by_scene,
    ) = _ir_index_scenes_events_identities(
        value, episode, bible, source_text, compiler_audit,
    )
    beats_were_derived = _ir_derive_missing_event_fields(
        value, scene_by_key, event_keys_by_scene, units_by_event,
        identity_display, metadata, compiler_audit,
    )
    beat_by_key, prior_by_key = _ir_derive_beats_and_priors(
        value, identity_by_key, beats_were_derived, compiler_audit,
    )

    expected_segment_ids = set(segments)
    beat_ids = {
        key: f"S{index:02d}" for index, key in enumerate(beat_by_key, start=1)
    }
    scene_ids = {
        key: f"SC{index:02d}"
        for index, key in enumerate(scene_by_key, start=1)
    }
    event_ids = {
        key: f"E{index}" for index, key in enumerate(event_by_key, start=1)
    }
    for beat in value.beats:
        unknown = set(beat.source_segment_ids) - expected_segment_ids
        if unknown:
            raise ValueError(f"beat {beat.key} 引用了不存在的来源段：{sorted(unknown)}")

    seen_coverage, coverage_rows, inferred_context_by_scene = (
        _ir_build_source_coverage_rows(
            value, beats_were_derived, beat_by_key, beat_ids, scene_by_key,
            segments, expected_segment_ids, compiler_audit,
        )
    )
    _ir_finalize_missing_coverage_rows(
        value, expected_segment_ids, seen_coverage, beat_by_key, beat_ids,
        coverage_rows, scene_by_key, segments, inferred_context_by_scene,
        compiler_audit,
    )
    event_order = _ir_validate_and_derive_event_relations(
        value, scene_by_key, identity_by_key, event_by_key,
        expected_segment_ids, typed_visual_unit_contract, compiler_audit,
    )

    identity_resolver, bible_by_name = _ir_build_identity_resolver(
        identity_by_key, bible, episode, compiler_audit,
    )
    ordered_used_keys, event_speaker_keys = (
        _ir_index_scene_units_and_collect_identities(
            value, identity_resolver, identity_by_key, event_by_key,
            event_order, compiler_audit,
        )
    )
    identity_resolver.finalize_ids(ordered_used_keys, bible_by_name)
    final_identity_ids = identity_resolver.final_identity_ids

    (
        source_evidence,
        propositions,
        adaptation_decisions,
        event_source_evidence_id,
        event_state_subject_ids,
        event_participant_ids,
        event_source_prop_id,
        event_adapted_prop_id,
        event_decision_id,
        environment_subject_id,
    ) = _ir_compile_event_evidence_and_adaptation(
        value, source_text, segments, episode, episode_no, format_version,
        typed_visual_unit_contract, identity_resolver, final_identity_ids,
        event_speaker_keys, scene_by_key, event_ids, ordered_used_keys,
        compiler_audit,
    )

    (
        adapted_ids_in_order, final_adapted_prop_id, first_adapted_prop_id,
        effective_render_policy,
    ) = _ir_compute_prop_order_and_render_policy(
        value, event_adapted_prop_id, strict_unit_ownership, compiler_audit,
    )

    (
        state_facts,
        narrative_events,
        atomic_actions,
        narrative_evidence,
        character_states,
        character_beliefs,
        legacy_events,
        information_ledger,
        event_evidence_ids,
        event_action_ids,
        event_character_state_ids,
    ) = _ir_compile_state_facts_and_actions(
        value, identity_resolver, episode, episode_no, event_ids,
        event_adapted_prop_id, event_state_subject_ids,
        effective_render_policy, first_adapted_prop_id, compiler_audit,
    )

    (
        dialogue_chain_rows,
        dialogue_chain_order,
        dialogue_chain_topics,
        dialogue_chain_scenes,
        key_line_event,
        script_lines,
        scene_outlines,
        event_keys_by_scene,
    ) = _ir_build_dialogue_and_scene_outline(
        value, source_text, identity_resolver, identity_by_key, event_by_key,
        inferred_context_by_scene, compiler_audit,
    )

    (
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
    ) = _ir_build_plot_beats_and_audience_setup(
        value, episode, episode_no, metadata, beat_ids,
        effective_render_policy, units_by_event, event_by_key,
        event_keys_by_scene, event_ids, scene_outlines, script_lines,
        information_ledger, key_line_event, dialogue_chain_order,
        dialogue_chain_rows, dialogue_chain_topics, dialogue_chain_scenes,
        prior_by_key, final_adapted_prop_id, first_adapted_prop_id,
    )

    _ir_build_audience_paths(
        value, prior_by_key, prior_ids, scope_id, event_ids,
        event_source_prop_id, event_evidence_ids, event_adapted_prop_id,
        adapted_ids_in_order, experience, experience_processing_s,
        setup_memory, last_event_id, audience_priors, audience_states,
        audience_paths, target_delta_ids,
    )

    (
        experience_intents,
        assimilation_tasks,
        readability_windows,
        setup_payoff_contracts,
        scene_contracts,
        arc_contracts,
    ) = _ir_build_experience_and_scene_contracts(
        value, identity_resolver, scope_id, event_ids, event_evidence_ids,
        adapted_ids_in_order, audience_paths, experience, last_event_id,
        target_delta_ids, first_adapted_prop_id, final_adapted_prop_id,
        event_by_key, event_keys_by_scene, event_character_state_ids,
        event_state_subject_ids, event_adapted_prop_id,
        environment_subject_id, prior_by_key, prior_ids, scene_ids,
        experience_processing_s,
    )

    voice_bible, identity_contracts = _ir_build_voice_and_identity_contracts(
        value, identity_resolver, identity_by_key, bible_by_name,
        final_identity_ids, ordered_used_keys, event_speaker_keys,
        event_source_evidence_id, event_adapted_prop_id, event_decision_id,
    )

    return _ir_assemble_episode_screenplay(
        value, episode_no, metadata, scope_id, key_lines, dialogue_chains,
        plot_beats, coverage_rows, scene_outlines, full_script_text,
        source_evidence, propositions, adaptation_decisions, state_facts,
        narrative_evidence, dramatic_questions, narrative_events,
        atomic_actions, character_states, character_beliefs,
        audience_priors, audience_states, experience_intents,
        assimilation_tasks, readability_windows, setup_payoff_contracts,
        scene_contracts, arc_contracts, identity_contracts,
        information_ledger, voice_bible, legacy_events, compiler_audit,
    )
