"""Pre-writing narrative authority contract for screenplay generation package.

``app/narrative_blueprint.py`` (5,128 lines / 81 top-level defs) was one file
until it was split by concern into this package: provider-payload
normalization, the shard provider schema, the core Pydantic models (including
the NarrativeBlueprint/NarrativeBlueprintShard envelopes), shard structural
validation, semantic-review schema/validation, voice/identity issues,
state-subject issue/perception/ownership/misclassification handling, patch
application, scene-plan derivation, the top-level structural validator, scene
contract validation, and the prompt contract.

This file is the sole stable entry point: every existing
``from app.narrative_blueprint import X`` / ``import app.narrative_blueprint``
/ ``narrative_blueprint.X`` call site across the repo must keep working
unmodified -- every symbol (including every plain stdlib/third-party name the
original file imported at module level, since those were reachable as
``narrative_blueprint.<name>`` too) is explicitly re-exported below using the
``name as name`` PEP 484 explicit-re-export form, matching the precedent set
by ``app/validators/__init__.py`` (``from .x import *`` is forbidden by the
``star_import`` gate in ``app/FILE_CONVENTIONS.toml``). Add new blueprint
logic to the concern-matching submodule, not back into this file.
"""
from __future__ import annotations

import hashlib as hashlib
import json as json
import re as re

from collections import defaultdict as defaultdict
from typing import Any as Any, Literal as Literal

from pydantic import (
    BaseModel as BaseModel,
    ConfigDict as ConfigDict,
    Field as Field,
    field_validator as field_validator,
    model_validator as model_validator,
)

from app.source_excerpt import (
    index_source_segments as index_source_segments,
    structural_front_matter_ids as structural_front_matter_ids,
)
from app.source_facts import (
    SOURCE_FACT_VERSION as SOURCE_FACT_VERSION,
    SourceFact as SourceFact,
    source_facts as source_facts,
)

from .constants import (
    AUDIBLE_SOURCE_DELIVERY_MODES as AUDIBLE_SOURCE_DELIVERY_MODES,
    BLUEPRINT_MAX_SOURCE_SEGMENTS_PER_NODE as BLUEPRINT_MAX_SOURCE_SEGMENTS_PER_NODE,
    BLUEPRINT_PROMPT_VERSION as BLUEPRINT_PROMPT_VERSION,
    BLUEPRINT_SHARD_LOCAL_AUTHORITY_VERSION as BLUEPRINT_SHARD_LOCAL_AUTHORITY_VERSION,
    BLUEPRINT_SHARD_POLICY_VERSION as BLUEPRINT_SHARD_POLICY_VERSION,
    BLUEPRINT_SPLIT_MANIFEST_VERSION as BLUEPRINT_SPLIT_MANIFEST_VERSION,
    BLUEPRINT_TARGET_SOURCE_FACTS_PER_SHARD as BLUEPRINT_TARGET_SOURCE_FACTS_PER_SHARD,
    BLUEPRINT_TARGET_SOURCE_SEGMENTS_PER_SHARD as BLUEPRINT_TARGET_SOURCE_SEGMENTS_PER_SHARD,
    BLUEPRINT_VERSION as BLUEPRINT_VERSION,
    STATE_SUBJECT_ADJUDICATION_VERSION as STATE_SUBJECT_ADJUDICATION_VERSION,
    _CANONICAL_SOURCE_UNIT_REFERENCE_RE as _CANONICAL_SOURCE_UNIT_REFERENCE_RE,
)
from .provider_normalize import (
    _PARATEXT_EMPTY_LIST_FIELDS as _PARATEXT_EMPTY_LIST_FIELDS,
    _evidence_segment_ids_from_units as _evidence_segment_ids_from_units,
    _normalize_source_segment_id as _normalize_source_segment_id,
    blueprint_authority_validator_fingerprint as blueprint_authority_validator_fingerprint,
    normalize_blueprint_provider_payload as normalize_blueprint_provider_payload,
    normalize_blueprint_raw_json as normalize_blueprint_raw_json,
)
from .shard_schema import (
    blueprint_shard_provider_schema as blueprint_shard_provider_schema,
    recover_complete_blueprint_prefix as recover_complete_blueprint_prefix,
)
from .models_core import (
    BlueprintDecision as BlueprintDecision,
    BlueprintSceneDerivation as BlueprintSceneDerivation,
    BlueprintScenePlan as BlueprintScenePlan,
    BlueprintSourceAuditAnnotation as BlueprintSourceAuditAnnotation,
    BlueprintSourceOccurrenceError as BlueprintSourceOccurrenceError,
    BlueprintSourceOccurrenceIssue as BlueprintSourceOccurrenceIssue,
    BlueprintSourceOwnershipError as BlueprintSourceOwnershipError,
    BlueprintSourceSemantics as BlueprintSourceSemantics,
    BlueprintStateChange as BlueprintStateChange,
    BlueprintStateRequirement as BlueprintStateRequirement,
    NarrativeBlueprint as NarrativeBlueprint,
    NarrativeBlueprintShard as NarrativeBlueprintShard,
    NarrativeNode as NarrativeNode,
    NarrativeParticipantEvidence as NarrativeParticipantEvidence,
    NarrativeSourceUnitDelivery as NarrativeSourceUnitDelivery,
    NarrativeStateSubjectAssignment as NarrativeStateSubjectAssignment,
    blueprint_source_occurrence_issues as blueprint_source_occurrence_issues,
)
from .shard_validate import validate_narrative_blueprint_shard as validate_narrative_blueprint_shard
from .models_patch import (
    BlueprintSemanticIssue as BlueprintSemanticIssue,
    BlueprintSemanticReview as BlueprintSemanticReview,
    BlueprintStateSubjectOwnershipPatch as BlueprintStateSubjectOwnershipPatch,
    BlueprintStateSubjectOwnershipRepair as BlueprintStateSubjectOwnershipRepair,
    NarrativeBlueprintPatch as NarrativeBlueprintPatch,
    NarrativeNodeReplacement as NarrativeNodeReplacement,
    render_blueprint_shard_semantic_issue as render_blueprint_shard_semantic_issue,
)
from .semantic_review_schema import (
    blueprint_patch_schema as blueprint_patch_schema,
    blueprint_semantic_review_schema as blueprint_semantic_review_schema,
    normalize_blueprint_fact_versions as normalize_blueprint_fact_versions,
    normalize_blueprint_requirement_state_keys as normalize_blueprint_requirement_state_keys,
    normalize_blueprint_semantic_review_payload as normalize_blueprint_semantic_review_payload,
)
from .semantic_review_validate import (
    _blueprint_environment_subject_issue_contract_errors as _blueprint_environment_subject_issue_contract_errors,
    blueprint_environment_subject_issue_has_exact_authority as blueprint_environment_subject_issue_has_exact_authority,
    blueprint_semantic_issue_is_resolved as blueprint_semantic_issue_is_resolved,
    blueprint_semantic_voice_issue_has_dialogue_authority as blueprint_semantic_voice_issue_has_dialogue_authority,
    filter_blueprint_semantic_review_voice_issues as filter_blueprint_semantic_review_voice_issues,
    normalize_blueprint_agency_continuity as normalize_blueprint_agency_continuity,
    validate_blueprint_semantic_review as validate_blueprint_semantic_review,
)
from .voice_identity_issues import (
    blueprint_voice_identity_issues as blueprint_voice_identity_issues,
    effective_source_unit_deliveries as effective_source_unit_deliveries,
)
from .state_subject_issues import (
    blueprint_state_subject_issues as blueprint_state_subject_issues,
    normalize_blueprint_state_subject_evidence_projection as normalize_blueprint_state_subject_evidence_projection,
)
from .state_subject_perception import (
    _node_identity_has_perception_evidence as _node_identity_has_perception_evidence,
    _node_state_subject_repairable_identities as _node_state_subject_repairable_identities,
    blueprint_candidate_hash as blueprint_candidate_hash,
    blueprint_shard_candidate_hash as blueprint_shard_candidate_hash,
    normalize_blueprint_state_subject_perception as normalize_blueprint_state_subject_perception,
)
from .state_subject_ownership_patch import (
    _blueprint_state_subject_repair_contract as _blueprint_state_subject_repair_contract,
    apply_blueprint_state_subject_ownership_patch as apply_blueprint_state_subject_ownership_patch,
    blueprint_state_subject_ownership_patch_schema as blueprint_state_subject_ownership_patch_schema,
)
from .state_subject_misclassification_patch import (
    _blueprint_non_ownership_projection as _blueprint_non_ownership_projection,
    _blueprint_non_target_ownership_projection as _blueprint_non_target_ownership_projection,
    _blueprint_state_subject_misclassification_contract as _blueprint_state_subject_misclassification_contract,
    apply_blueprint_state_subject_misclassification_patch as apply_blueprint_state_subject_misclassification_patch,
    blueprint_state_subject_misclassification_patch_schema as blueprint_state_subject_misclassification_patch_schema,
)
from .patch_apply import (
    apply_narrative_blueprint_patch as apply_narrative_blueprint_patch,
    normalize_blueprint_source_order as normalize_blueprint_source_order,
    validate_narrative_blueprint_patch_projection as validate_narrative_blueprint_patch_projection,
)
from .scene_plans import (
    derive_blueprint_scene_plans as derive_blueprint_scene_plans,
    validate_blueprint_scene_partition as validate_blueprint_scene_partition,
)
from .validate import validate_narrative_blueprint as validate_narrative_blueprint
from .scene_contract import validate_and_apply_blueprint_scene_contract as validate_and_apply_blueprint_scene_contract
from .prompt_contract import blueprint_prompt_contract as blueprint_prompt_contract
