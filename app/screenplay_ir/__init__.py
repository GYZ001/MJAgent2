"""Compact screenplay generation IR and deterministic EpisodeScreenplay compiler package.

``app/screenplay_ir.py`` (6,916 lines / 100 top-level defs) was one file until it
was split by pipeline phase into this package: contract validation, compact-IR
Pydantic models, identity normalization/authority binding, prompt/context
helpers, and the compile_screenplay_ir orchestrator's phases (parse/validate ->
event derivation -> indexing -> evidence compilation -> contract assembly),
following the same phase boundaries the pre-existing helper-function
decomposition inside compile_screenplay_ir already used.

This file is the sole stable entry point: every existing
``from app.screenplay_ir import X`` / ``import app.screenplay_ir`` /
``screenplay_ir.X`` call site across the repo must keep working unmodified --
every symbol (including every plain stdlib/third-party name the original file
imported at module level, since those were reachable as
``screenplay_ir.<name>`` too) is explicitly re-exported below using the
``name as name`` PEP 484 explicit-re-export form, matching the precedent set
by ``app/validators/__init__.py`` (``from .x import *`` is forbidden by the
``star_import`` gate in ``app/FILE_CONVENTIONS.toml``). Add new compiler logic
to the concern-matching submodule, not back into this file.
"""
from __future__ import annotations

import hashlib as hashlib
import json as json
import re as re

from collections import defaultdict as defaultdict
from copy import deepcopy as deepcopy
from typing import Any as Any, Literal as Literal

from pydantic import (
    BaseModel as BaseModel,
    Field as Field,
    field_validator as field_validator,
    model_validator as model_validator,
)

from app import config as config, textmatch as textmatch
from app.character_policy import resolution_declares_functional_identity as resolution_declares_functional_identity
from app.identity_authority import (
    backend_owned_identity_authority as backend_owned_identity_authority,
    identity_authority_registry as identity_authority_registry,
    identity_resolution_is_authoritative as identity_resolution_is_authoritative,
    model_identity_authority_prompt_rule as model_identity_authority_prompt_rule,
)
from app.narrative_blueprint import (
    BlueprintSourceAuditAnnotation as BlueprintSourceAuditAnnotation,
    BlueprintSourceSemantics as BlueprintSourceSemantics,
    _normalize_source_segment_id as _normalize_source_segment_id,
)
from app.renderability import (
    SCENE_STORY_FUNCTION_MIN_CHARS as SCENE_STORY_FUNCTION_MIN_CHARS,
    chunk_dialogue_turns as chunk_dialogue_turns,
)
from app.schemas import (
    ActionAgency as ActionAgency,
    Bible as Bible,
    EpisodeScreenplay as EpisodeScreenplay,
    InformationItem as InformationItem,
    KeyDialogueChain as KeyDialogueChain,
    KeyDialogueTurn as KeyDialogueTurn,
    NARRATIVE_CONTRACT_VERSION as NARRATIVE_CONTRACT_VERSION,
    NarrativeContinuityPlan as NarrativeContinuityPlan,
    PlotSpine as PlotSpine,
    PlotSpineBeat as PlotSpineBeat,
    ScriptScene as ScriptScene,
    SourceCoverageDecision as SourceCoverageDecision,
    StoryEvent as StoryEvent,
    TextProvenance as TextProvenance,
    VoiceCanonical as VoiceCanonical,
    system_environment_entity_id as system_environment_entity_id,
)
from app.source_excerpt import (
    align_source_excerpt as align_source_excerpt,
    index_compact_source_segments as index_compact_source_segments,
    index_source_segments as index_source_segments,
    structural_front_matter_ids as structural_front_matter_ids,
)
from app.spoken_contract import content_char_count as content_char_count

from .constants import (
    IR_COMPILER_VERSION as IR_COMPILER_VERSION,
    IR_LOCAL_SOURCE_WINDOW as IR_LOCAL_SOURCE_WINDOW,
    IR_MAX_SOURCE_SEGMENTS_PER_UNIT as IR_MAX_SOURCE_SEGMENTS_PER_UNIT,
    IR_MIN_ADAPTED_SOURCE_RATIO as IR_MIN_ADAPTED_SOURCE_RATIO,
    IR_MIN_LOCAL_ADAPTED_SOURCE_RATIO as IR_MIN_LOCAL_ADAPTED_SOURCE_RATIO,
    IR_VERSION as IR_VERSION,
    ScreenplayIRFidelityError as ScreenplayIRFidelityError,
    ScreenplayIRIdentityConflictError as ScreenplayIRIdentityConflictError,
    _AUDIT_SOURCE_SEMANTICS as _AUDIT_SOURCE_SEMANTICS,
    _DIALOGUE_FUNCTIONS as _DIALOGUE_FUNCTIONS,
    _SourceAuditAnnotationIdentity as _SourceAuditAnnotationIdentity,
    _SourceSemanticIdentity as _SourceSemanticIdentity,
)
from .contract_validation import (
    _as_list as _as_list,
    _canonical_source_audit_annotation_identity as _canonical_source_audit_annotation_identity,
    _canonical_source_semantic_identity as _canonical_source_semantic_identity,
    _structural_context_authority_id as _structural_context_authority_id,
    _validate_text_provenance as _validate_text_provenance,
    derive_action_agency_payload as derive_action_agency_payload,
    derive_text_provenance_payload as derive_text_provenance_payload,
    scene_heading_has_multiple_locations as scene_heading_has_multiple_locations,
    screenplay_beat_fields_repeat as screenplay_beat_fields_repeat,
    screenplay_ir_missing_event_semantic_paths as screenplay_ir_missing_event_semantic_paths,
    screenplay_ir_missing_participant_delivery_paths as screenplay_ir_missing_participant_delivery_paths,
    screenplay_ir_version_key as screenplay_ir_version_key,
)
from .source_audit import screenplay_ir_source_audit_contract_errors as screenplay_ir_source_audit_contract_errors
from .models_core import (
    IRActionParticipantDelivery as IRActionParticipantDelivery,
    IRBeat as IRBeat,
    IRCoverageGroup as IRCoverageGroup,
    IRIdentity as IRIdentity,
    IRScene as IRScene,
    IRSceneUnit as IRSceneUnit,
)
from .models_event import (
    IRActionPhase as IRActionPhase,
    IRAudiencePrior as IRAudiencePrior,
    IREvent as IREvent,
    IRExperience as IRExperience,
    IRMetadata as IRMetadata,
    ScreenplayGenerationIR as ScreenplayGenerationIR,
    merge_scene_shards as merge_scene_shards,
)
from .identity_normalize import (
    _normalize_duplicate_ir_identity_displays as _normalize_duplicate_ir_identity_displays,
    normalize_screenplay_ir_payload as normalize_screenplay_ir_payload,
)
from .identity_authorities import (
    ATTRIBUTED_TEXT_PROVENANCE_KINDS as ATTRIBUTED_TEXT_PROVENANCE_KINDS,
    _apply_authoritative_ir_identity_resolutions as _apply_authoritative_ir_identity_resolutions,
    _bind_ir_identity_authority as _bind_ir_identity_authority,
    _merge_ir_identities_with_same_authority as _merge_ir_identities_with_same_authority,
    _rewrite_ir_identity_key as _rewrite_ir_identity_key,
    prepare_ir_identity_authorities as prepare_ir_identity_authorities,
)
from .prompt_context import (
    _beats_for_event as _beats_for_event,
    _default_metadata as _default_metadata,
    _dialogue_source_text as _dialogue_source_text,
    _first_sentence as _first_sentence,
    _nearest_event_for_segment as _nearest_event_for_segment,
    _retain_source_segment_as_scene_context as _retain_source_segment_as_scene_context,
    _screenplay_action_text as _screenplay_action_text,
    _segment_ordinal as _segment_ordinal,
    _semantic_key as _semantic_key,
    _source_location as _source_location,
    _split_spoken_line as _split_spoken_line,
    _state_fact_ids as _state_fact_ids,
    _unique_by_key as _unique_by_key,
    recover_complete_screenplay_ir_prefix as recover_complete_screenplay_ir_prefix,
    screenplay_ir_bible_context as screenplay_ir_bible_context,
    screenplay_ir_prompt_contract as screenplay_ir_prompt_contract,
)
from .identity_resolver import _IRIdentityResolver as _IRIdentityResolver
from .compile_setup import (
    _ir_prepare_compile_setup as _ir_prepare_compile_setup,
    _ir_split_discontinuous_units as _ir_split_discontinuous_units,
)
from .unit_ownership import (
    _ir_narrow_redundant_unit_ownership as _ir_narrow_redundant_unit_ownership,
    _ir_normalize_strict_unit_ownership as _ir_normalize_strict_unit_ownership,
    _ir_validate_unit_source_ownership as _ir_validate_unit_source_ownership,
    _ir_verify_unit_source_fidelity as _ir_verify_unit_source_fidelity,
)
from .unit_typed_validation import (
    _ir_build_identity_alias_index as _ir_build_identity_alias_index,
    _ir_compute_assigned_source_indices as _ir_compute_assigned_source_indices,
    _ir_validate_typed_scene_units as _ir_validate_typed_scene_units,
)
from .event_derivation import (
    _ir_assemble_events_from_units as _ir_assemble_events_from_units,
    _ir_derive_events_from_scene_units as _ir_derive_events_from_scene_units,
    _ir_isolate_paratext_events as _ir_isolate_paratext_events,
)
from .event_indexing import (
    _ir_derive_beats_and_priors as _ir_derive_beats_and_priors,
    _ir_derive_missing_event_fields as _ir_derive_missing_event_fields,
    _ir_index_scenes_events_identities as _ir_index_scenes_events_identities,
)
from .coverage_rows import (
    _ir_build_source_coverage_rows as _ir_build_source_coverage_rows,
    _ir_finalize_missing_coverage_rows as _ir_finalize_missing_coverage_rows,
)
from .event_relations import (
    _ir_build_identity_resolver as _ir_build_identity_resolver,
    _ir_index_scene_units_and_collect_identities as _ir_index_scene_units_and_collect_identities,
    _ir_validate_and_derive_event_relations as _ir_validate_and_derive_event_relations,
)
from .evidence_compile import (
    _ir_compile_event_evidence_and_adaptation as _ir_compile_event_evidence_and_adaptation,
    _ir_compute_prop_order_and_render_policy as _ir_compute_prop_order_and_render_policy,
)
from .state_facts import _ir_compile_state_facts_and_actions as _ir_compile_state_facts_and_actions
from .dialogue_outline import _ir_build_dialogue_and_scene_outline as _ir_build_dialogue_and_scene_outline
from .plot_audience import (
    _ir_build_audience_paths as _ir_build_audience_paths,
    _ir_build_plot_beats_and_audience_setup as _ir_build_plot_beats_and_audience_setup,
)
from .experience_contracts import (
    _ir_build_experience_and_scene_contracts as _ir_build_experience_and_scene_contracts,
    _ir_build_voice_and_identity_contracts as _ir_build_voice_and_identity_contracts,
)
from .assemble import _ir_assemble_episode_screenplay as _ir_assemble_episode_screenplay
from .compiler import compile_screenplay_ir as compile_screenplay_ir
