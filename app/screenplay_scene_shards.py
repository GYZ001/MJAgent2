"""Typed pre-document screenplay envelope and scene-writing shards.

These artifacts are resumable generation evidence only.  They never become a
working/published screenplay pointer; the public authority remains the compiled
``ScreenplayDocument`` created by Production Repair.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from app import hiagent
from app.character_policy import functional_extra_anchor
from app.db import get_conn, get_setting
from app.evidence import repository as evidence_repository
from app.harness import model_gateway
from app.harness.types import EvidenceArtifact
from app.identity_authority import identity_authority_registry
from app.narrative_blueprint import (
    BlueprintSceneDerivation,
    BlueprintScenePlan,
    BlueprintSourceSemantics,
    NarrativeNode,
    NarrativeBlueprint,
    derive_blueprint_scene_plans,
    effective_source_unit_deliveries,
)
from app.observability.tracing import current_trace
from app.renderability import SCENE_STORY_FUNCTION_MIN_CHARS
from app.schemas import ActionAgency, Bible, TextProvenance
from app.screenplay_ir import (
    IRActionParticipantDelivery,
    IRCoverageGroup,
    IRExperience,
    IRIdentity,
    IRMetadata,
    IRScene,
    IRSceneUnit,
    ScreenplayGenerationIR,
    IR_VERSION,
    screenplay_ir_source_audit_contract_errors,
)
from app.source_excerpt import (
    index_source_segments,
    quotation_closing,
    quotation_opening,
    structural_front_matter_ids,
)
from app.source_facts import SourceFact, source_segment_facts


SCREENPLAY_ENVELOPE_VERSION = "screenplay-envelope.v1"
SCREENPLAY_SCENE_SHARD_VERSION = "screenplay-scene-shard.v10"
SCREENPLAY_SHARD_PLAN_VERSION = "screenplay-scene-shard-plan.v6"
SCREENPLAY_SCENE_INPUT_VERSION = "screenplay-scene-input.v10"
SCREENPLAY_SCENE_CREATIVE_VERSION = "screenplay-scene-creative.v7"
SCREENPLAY_MERGED_IR_VERSION = "screenplay-generation-ir-merged.v9"
SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION = (
    "screenplay-scene-semantic-review.v12"
)
SCREENPLAY_SCENE_JSON_ONLY_SYSTEM_PROMPT = (
    "只返回一个符合用户消息内 JSON Schema 的 JSON 对象。"
    "不得返回 Markdown、解释或对象外文本。"
)
SCREENPLAY_SCENE_SEMANTIC_REVIEW_MIN_OUTPUT_TOKENS = 2048
SCREENPLAY_SCENE_SEMANTIC_REVIEW_CONTEXT_RESERVE_TOKENS = 1024
SCREENPLAY_SCENE_SEMANTIC_REPAIR_MIN_OUTPUT_TOKENS = 4096
SCREENPLAY_SCENE_SEMANTIC_REPAIR_ROOT_RESERVE_PERCENT = 20
SCREENPLAY_SCENE_SEMANTIC_MAX_REPAIR_ROUNDS = 3
SCREENPLAY_SCENE_SEMANTIC_INITIAL_FORMAT_RETRY_LIMIT = 2
SCREENPLAY_SCENE_SEMANTIC_POST_REPAIR_FORMAT_RETRY_LIMIT = 2
SCREENPLAY_SCENE_SEMANTIC_FINDING_MESSAGE_MAX_CHARS = 160
SCREENPLAY_SCENE_SEMANTIC_FINDING_CODES = (
    "state_subject_semantic_drift",
    "source_semantic_drift",
)
SCREENPLAY_SCENE_SEMANTIC_VIOLATION_KINDS = (
    "wrong_subject",
    "unsupported_action",
    "source_contradiction",
    "cross_slot_duplication",
    "environment_personification",
)
SCREENPLAY_SCENE_SHARD_MIN_OUTPUT_TOKENS = 4096
SCREENPLAY_SCENE_SHARD_MAX_OUTPUT_TOKENS = 16384
SCREENPLAY_SCENE_SHARD_SCENE_RESERVE_TOKENS = 512
SCREENPLAY_SCENE_SHARD_UNIT_RESERVE_TOKENS = 128
SCREENPLAY_SCENE_SHARD_REASONING_RESERVE_PERCENT = 20


async def _gather_fail_fast(*awaitables: Awaitable[Any]) -> list[Any]:
    """Run a batch in input order, cancelling and joining it on first failure.

    ``asyncio.gather`` propagates a child exception without cancelling its
    siblings.  That is unsafe for paid generation: those detached siblings can
    continue into later provider calls after the batch has already failed.
    """
    tasks = [asyncio.ensure_future(awaitable) for awaitable in awaitables]
    pending = set(tasks)
    try:
        while pending:
            done, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
            )
            failed = next(
                (
                    task
                    for task in done
                    if not task.cancelled() and task.exception() is not None
                ),
                None,
            )
            if failed is not None:
                failed.result()
            for task in done:
                # result() preserves the original exception (including
                # CancelledError) instead of wrapping it in an ExceptionGroup.
                task.result()
        return [task.result() for task in tasks]
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        # Retrieve every terminal outcome before propagating the original one;
        # this prevents detached work and "exception was never retrieved".
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _setting_int(key: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(get_setting(key) or default)))
    except (TypeError, ValueError):
        return default


class ScreenplayEnvelopeMetadata(BaseModel):
    title: str = ""
    logline: str = ""
    dramatic_question: str = ""
    protagonist_goal: str = ""
    obstacle: str = ""
    stakes: str = ""
    emotional_curve: str = ""
    ending_hook: str = ""
    source_basis: str = ""
    adaptation_direction: str = ""
    opening: str = ""
    development: str = ""
    conflict: str = ""
    climax: str = ""
    episode_premise: str = ""
    approved_adaptations: list[str] = Field(default_factory=list)
    forbidden_additions: list[str] = Field(default_factory=list)
    script_format_note: str = "场次化台本稿"
    must_keep_ending: str = ""
    drop_list: list[str] = Field(default_factory=list)

    def to_ir(self) -> IRMetadata:
        return IRMetadata.model_validate(self.model_dump(mode="json"))


class ScreenplayEnvelopeExperience(BaseModel):
    director_objective: str = ""
    satisfaction_criteria: str = ""
    required_processing_s: float = Field(default=1.0, ge=0)
    forbidden_misconceptions: list[str] = Field(default_factory=list)

    def to_ir(self) -> IRExperience:
        return IRExperience.model_validate(self.model_dump(mode="json"))


class ScreenplayEnvelopeIR(BaseModel):
    contract_version: Literal["screenplay-envelope.v1"] = SCREENPLAY_ENVELOPE_VERSION
    episode_no: int
    metadata: ScreenplayEnvelopeMetadata
    experience: ScreenplayEnvelopeExperience
    blueprint_hash: str
    identity_registry_hash: str


class ScreenplaySceneUnitSlotPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit_key: str
    event_key: str
    scene_key: str
    scene_order: int = Field(ge=1)
    unit_order: int = Field(ge=1)
    scene_unit_order: int = Field(ge=1)
    kind: Literal["action", "dialogue"]
    narrative_layer: Literal["story", "paratext"]
    event_priority: Literal["causal", "supporting", "connective"]
    render_policy: Literal[
        "standalone", "merge_adjacent", "exclude_from_spine",
    ]
    source_segment_ids: list[str] = Field(min_length=1)
    source_unit_key: str = ""
    source_text: str = ""
    source_surface: Literal["prose", "quoted_span"] = "prose"
    delivery_mode: Literal[
        "action",
        "spoken_dialogue",
        "offscreen_voice",
        "written_text",
        "sound_effect",
        "unspoken_reference",
    ] = "action"
    content_owner_key: str = ""
    performer_key: str = ""
    state_subject_key: str = ""
    environment_only: bool = False


class ScreenplaySceneCompiledUnitSlot(ScreenplaySceneUnitSlotPlan):
    actor_keys: list[str] = Field(default_factory=list)
    target_keys: list[str] = Field(default_factory=list)
    onscreen_entity_keys: list[str] = Field(default_factory=list)
    participant_deliveries: list[IRActionParticipantDelivery] = Field(
        default_factory=list,
    )
    speaker_key: str | None = None
    state_subject_keys: list[str] = Field(default_factory=list)
    action_agency: ActionAgency | None = None

    @model_validator(mode="after")
    def _derive_action_agency(self) -> "ScreenplaySceneCompiledUnitSlot":
        identity_bearing = bool(
            self.actor_keys or self.target_keys or self.speaker_key
        )
        if self.action_agency is None:
            self.action_agency = ActionAgency(
                kind=(
                    "character"
                    if identity_bearing
                    else "unattributed"
                ),
                identity_bearing=identity_bearing,
                source_segment_ids=list(self.source_segment_ids),
            )
        if self.action_agency.identity_bearing != identity_bearing:
            raise ValueError(
                "compiled slot action_agency.identity_bearing 必须与 "
                "actor_keys/target_keys/speaker_key 等价"
            )
        if self.action_agency.is_character_agency and not identity_bearing:
            raise ValueError(
                "compiled slot character action_agency 必须由 "
                "actor_keys/target_keys/speaker_key 承载"
            )
        if self.action_agency.source_segment_ids != self.source_segment_ids:
            raise ValueError(
                "compiled slot action_agency.source_segment_ids 必须与来源等价"
            )
        if self.state_subject_key and self.state_subject_keys != [
            self.state_subject_key
        ]:
            raise ValueError(
                "compiled slot state_subject_key 必须等于唯一 "
                "state_subject_keys 成员"
            )
        if self.environment_only and self.state_subject_keys:
            raise ValueError(
                "compiled slot 不得同时声明 state_subject_keys "
                "与 environment_only"
            )
        return self


class ScreenplaySceneShardPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["screenplay-scene-shard-plan.v6"] = SCREENPLAY_SHARD_PLAN_VERSION
    shard_id: str
    scene_plan_keys: list[str]
    source_segment_ids: list[str]
    source_scene_owners: dict[str, str]
    unit_slots: list[ScreenplaySceneUnitSlotPlan]
    derived_relations: list[BlueprintSceneDerivation] = Field(
        default_factory=list,
    )
    source_ownership_hash: str
    estimated_units: int = Field(ge=1)
    estimated_output_chars: int = Field(ge=1)
    boundary_state_in: dict[str, Any] = Field(default_factory=dict)
    boundary_state_out: dict[str, Any] = Field(default_factory=dict)
    source_hash: str
    boundary_hash: str
    blueprint_hash: str
    identity_registry_hash: str


class ScreenplaySceneSourceSegment(BaseModel):
    source_segment_id: str
    text: str


class ScreenplaySceneParticipantBinding(BaseModel):
    blueprint_key: str
    identity_key: str


class ScreenplaySceneActionParticipantEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity_key: str
    source_segment_ids: list[str] = Field(default_factory=list)
    source_unit_keys: list[str] = Field(default_factory=list)
    usage: Literal["visible", "voice", "mentioned", "state_subject"]
    perception_channels: list[
        Literal["audible", "visible_effect", "visible_reaction"]
    ] = Field(default_factory=list)


class ScreenplaySceneStateSubjectAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_unit_key: str
    mode: Literal["joint"]
    identity_keys: list[str] = Field(min_length=2)


class ScreenplaySceneActionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_key: str
    source_segment_ids: list[str] = Field(default_factory=list)
    participants: list[ScreenplaySceneActionParticipantEvidence] = Field(
        default_factory=list,
    )
    state_subject_assignments: list[
        ScreenplaySceneStateSubjectAssignment
    ] = Field(default_factory=list)
    decision_actor_key: str | None = None
    environment_source_unit_keys: list[str] = Field(default_factory=list)


class ScreenplayActionParticipantDeliveryContract(BaseModel):
    contract_version: Literal["screenplay-generation-ir.v4"] = IR_VERSION
    evidence_schema: dict[str, Any] = Field(
        default_factory=IRActionParticipantDelivery.model_json_schema,
    )
    unit_field_required: Literal[True] = True
    offscreen_relation_requires_evidence: Literal[True] = True
    observable_claim_required: Literal[True] = True
    perceivable_channel_required: Literal[True] = True


class ScreenplaySceneInputContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["screenplay-scene-input.v10"] = (
        SCREENPLAY_SCENE_INPUT_VERSION
    )
    scene_plan_key: str
    node_keys: list[str]
    source_segment_ids: list[str]
    source_semantics: dict[str, BlueprintSourceSemantics]
    source_segments: list[ScreenplaySceneSourceSegment]
    participant_bindings: list[ScreenplaySceneParticipantBinding]
    source_scene_owners: dict[str, str]
    derived_relations: list[BlueprintSceneDerivation] = Field(
        default_factory=list,
    )
    action_participant_delivery_contract: (
        ScreenplayActionParticipantDeliveryContract
    ) = Field(default_factory=ScreenplayActionParticipantDeliveryContract)
    action_evidence: list[ScreenplaySceneActionEvidence] = Field(
        default_factory=list,
    )
    unit_slots: list[ScreenplaySceneCompiledUnitSlot]
    identity_scaffold_hash: str = ""
    source_ownership_hash: str


class UnresolvedParticipant(BaseModel):
    source_label: str
    source_segment_ids: list[str] = Field(default_factory=list)
    scene_key: str = ""
    usage: str = "visible"
    reason: str = ""


class ScreenplaySceneShardCreativeUnit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    performance: str = ""
    resulting_state: str = ""
    function: str = "statement"
    required_text: str = ""
    prop_text: str = ""
    on_screen_text: str = ""

    @model_validator(mode="after")
    def _validate_text_content_shape(
        self,
    ) -> "ScreenplaySceneShardCreativeUnit":
        explicit_text_fields = sum(bool(value.strip()) for value in (
            self.required_text,
            self.prop_text,
            self.on_screen_text,
        ))
        if explicit_text_fields > 1:
            raise ValueError(
                "required_text/prop_text/on_screen_text 每个 slot 最多填写一种"
            )
        return self


class ScreenplaySceneShardCreativeIR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["screenplay-scene-creative.v7"] = (
        SCREENPLAY_SCENE_CREATIVE_VERSION
    )
    slots: dict[str, ScreenplaySceneShardCreativeUnit]


class ScreenplaySceneShardSemanticFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit_key: str
    related_unit_keys: list[str]
    code: Literal[
        "state_subject_semantic_drift",
        "source_semantic_drift",
    ]
    violation_kinds: list[
        Literal[
            "wrong_subject",
            "unsupported_action",
            "source_contradiction",
            "cross_slot_duplication",
            "environment_personification",
        ]
    ] = Field(min_length=1, max_length=5)
    message: str = Field(
        min_length=1,
        max_length=SCREENPLAY_SCENE_SEMANTIC_FINDING_MESSAGE_MAX_CHARS,
    )

    @model_validator(mode="before")
    @classmethod
    def _fill_missing_non_cross_related_unit_keys(
        cls,
        value: Any,
    ) -> Any:
        if (
            isinstance(value, dict)
            and "related_unit_keys" not in value
            and isinstance(value.get("violation_kinds"), list)
            and "cross_slot_duplication"
            not in value["violation_kinds"]
        ):
            return {**value, "related_unit_keys": []}
        return value

    @model_validator(mode="after")
    def _validate_typed_finding(
        self,
    ) -> "ScreenplaySceneShardSemanticFinding":
        if len(self.violation_kinds) != len(set(self.violation_kinds)):
            raise ValueError("violation_kinds 不得重复")
        self.violation_kinds = [
            kind
            for kind in SCREENPLAY_SCENE_SEMANTIC_VIOLATION_KINDS
            if kind in self.violation_kinds
        ]
        if len(self.related_unit_keys) != len(
            set(self.related_unit_keys)
        ):
            raise ValueError("related_unit_keys 不得重复")
        if self.unit_key in self.related_unit_keys:
            raise ValueError("related_unit_keys 不得包含 finding 自身 unit_key")
        has_cross_slot_duplication = (
            "cross_slot_duplication" in self.violation_kinds
        )
        if (
            has_cross_slot_duplication
            and len(self.related_unit_keys) != 1
        ):
            raise ValueError(
                "cross_slot_duplication 必须恰好声明一个 related_unit_key"
            )
        if not has_cross_slot_duplication and self.related_unit_keys:
            raise ValueError(
                "非 cross_slot_duplication finding 的 "
                "related_unit_keys 必须为空"
            )
        return self


class ScreenplaySceneShardSemanticReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[ScreenplaySceneShardSemanticFinding]

    @model_validator(mode="after")
    def _validate_unique_finding_keys(
        self,
    ) -> "ScreenplaySceneShardSemanticReview":
        finding_keys = [
            (finding.unit_key, finding.code)
            for finding in self.findings
        ]
        if len(finding_keys) != len(set(finding_keys)):
            raise ValueError("findings 中 (unit_key, code) 必须唯一")
        return self


_SCENE_SHARD_UNIT_ORDINAL_RE = re.compile(r":(?P<ordinal>\d+):unit$")


def _scene_shard_canonicalize_review_unit_references(
    review: ScreenplaySceneShardSemanticReview,
    known_unit_keys: set[str],
) -> int:
    """Repair only uniquely identifiable structured unit-key references."""
    canonical_by_ordinal: dict[str, list[str]] = {}
    for unit_key in known_unit_keys:
        match = _SCENE_SHARD_UNIT_ORDINAL_RE.search(unit_key)
        if match is None:
            continue
        canonical_by_ordinal.setdefault(
            match.group("ordinal"),
            [],
        ).append(unit_key)

    def canonicalize(unit_key: str) -> str:
        if unit_key in known_unit_keys:
            return unit_key
        match = _SCENE_SHARD_UNIT_ORDINAL_RE.search(unit_key)
        if match is None:
            return unit_key
        candidates = canonical_by_ordinal.get(
            match.group("ordinal"),
            [],
        )
        return candidates[0] if len(candidates) == 1 else unit_key

    changes = 0
    for finding in review.findings:
        canonical_unit_key = canonicalize(finding.unit_key)
        canonical_related_keys = [
            canonicalize(unit_key)
            for unit_key in finding.related_unit_keys
        ]
        if canonical_unit_key != finding.unit_key:
            finding.unit_key = canonical_unit_key
            changes += 1
        if canonical_related_keys != finding.related_unit_keys:
            finding.related_unit_keys = canonical_related_keys
            changes += 1
    return changes


_SCENE_SHARD_UNIT_ORDINAL_RE = re.compile(r":(?P<ordinal>\d+):unit$")


def _scene_shard_canonicalize_review_unit_references(
    review: ScreenplaySceneShardSemanticReview,
    known_unit_keys: set[str],
) -> int:
    """Repair only uniquely identifiable structured unit-key references."""
    canonical_by_ordinal: dict[str, list[str]] = {}
    for unit_key in known_unit_keys:
        match = _SCENE_SHARD_UNIT_ORDINAL_RE.search(unit_key)
        if match is None:
            continue
        canonical_by_ordinal.setdefault(
            match.group("ordinal"),
            [],
        ).append(unit_key)

    def canonicalize(unit_key: str) -> str:
        if unit_key in known_unit_keys:
            return unit_key
        match = _SCENE_SHARD_UNIT_ORDINAL_RE.search(unit_key)
        if match is None:
            return unit_key
        candidates = canonical_by_ordinal.get(
            match.group("ordinal"),
            [],
        )
        return candidates[0] if len(candidates) == 1 else unit_key

    changes = 0
    for finding in review.findings:
        canonical_unit_key = canonicalize(finding.unit_key)
        canonical_related_keys = [
            canonicalize(unit_key)
            for unit_key in finding.related_unit_keys
        ]
        if canonical_unit_key != finding.unit_key:
            finding.unit_key = canonical_unit_key
            changes += 1
        if canonical_related_keys != finding.related_unit_keys:
            finding.related_unit_keys = canonical_related_keys
            changes += 1
    return changes


def _screenplay_scene_semantic_consensus_message(
    code: str,
    canonical_kinds: list[str],
) -> str:
    return (
        f"{code}：仅修复双审共识类型"
        f"[{','.join(canonical_kinds)}]；依据冻结来源。"
    )


def screenplay_scene_semantic_consensus(
    reviewer1: ScreenplaySceneShardSemanticReview,
    reviewer2: ScreenplaySceneShardSemanticReview,
) -> list[ScreenplaySceneShardSemanticFinding]:
    """Intersect typed reviewer kinds by finding identity."""
    finding_maps = [
        {
            (finding.unit_key, finding.code): finding
            for finding in review.findings
        }
        for review in (reviewer1, reviewer2)
    ]
    consensus: list[ScreenplaySceneShardSemanticFinding] = []
    for key in sorted(set(finding_maps[0]).intersection(finding_maps[1])):
        first_finding = finding_maps[0][key]
        second_finding = finding_maps[1][key]
        shared_kinds = (
            set(first_finding.violation_kinds)
            & set(second_finding.violation_kinds)
        )
        if (
            "cross_slot_duplication" in shared_kinds
            and first_finding.related_unit_keys
            != second_finding.related_unit_keys
        ):
            shared_kinds.remove("cross_slot_duplication")
        canonical_kinds = [
            kind
            for kind in SCREENPLAY_SCENE_SEMANTIC_VIOLATION_KINDS
            if kind in shared_kinds
        ]
        if canonical_kinds:
            consensus.append(
                ScreenplaySceneShardSemanticFinding(
                    unit_key=key[0],
                    related_unit_keys=(
                        list(first_finding.related_unit_keys)
                        if "cross_slot_duplication" in canonical_kinds
                        else []
                    ),
                    code=key[1],
                    violation_kinds=canonical_kinds,
                    message=_screenplay_scene_semantic_consensus_message(
                        key[1],
                        canonical_kinds,
                    ),
                )
            )
    return consensus


def screenplay_scene_semantic_review_worst_case_payload(
    unit_keys: list[str],
) -> str:
    """Build the largest valid compact review for the declared slots."""
    findings = [
        {
            "unit_key": unit_key,
            "related_unit_keys": (
                [unit_keys[(unit_index + 1) % len(unit_keys)]]
                if len(unit_keys) > 1
                else []
            ),
            "code": code,
            "violation_kinds": [
                kind
                for kind in SCREENPLAY_SCENE_SEMANTIC_VIOLATION_KINDS
                if len(unit_keys) > 1 or kind != "cross_slot_duplication"
            ],
            "message": (
                "冲"
                * SCREENPLAY_SCENE_SEMANTIC_FINDING_MESSAGE_MAX_CHARS
            ),
        }
        for unit_index, unit_key in enumerate(unit_keys)
        for code in SCREENPLAY_SCENE_SEMANTIC_FINDING_CODES
    ]
    return json.dumps(
        {"findings": findings},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _screenplay_scene_semantic_token_estimate(chars: int) -> int:
    return math.ceil(max(0, chars) / 1.5 * 1.2)


def screenplay_scene_semantic_review_required_tokens(
    unit_keys: list[str],
) -> int:
    payload = screenplay_scene_semantic_review_worst_case_payload(
        unit_keys
    )
    return max(
        SCREENPLAY_SCENE_SEMANTIC_REVIEW_MIN_OUTPUT_TOKENS,
        _screenplay_scene_semantic_token_estimate(len(payload)),
    )


def screenplay_scene_semantic_repair_required_tokens(
    *,
    draft_json: str,
    repair_prompt: str,
) -> int:
    root_tokens = math.ceil(len(draft_json) / 1.5)
    root_with_reserve = math.ceil(
        root_tokens
        * (100 + SCREENPLAY_SCENE_SEMANTIC_REPAIR_ROOT_RESERVE_PERCENT)
        / 100
    )
    return max(
        SCREENPLAY_SCENE_SEMANTIC_REPAIR_MIN_OUTPUT_TOKENS,
        root_with_reserve,
        len(repair_prompt) // 2,
    )


class ScreenplaySceneShardUnit(IRSceneUnit):
    model_config = ConfigDict(extra="forbid")

    participant_deliveries: list[IRActionParticipantDelivery]


class ScreenplaySceneShardScene(IRScene):
    model_config = ConfigDict(extra="forbid")

    units: list[ScreenplaySceneShardUnit] = Field(default_factory=list)


class ScreenplaySceneShardIR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["screenplay-scene-shard.v10"] = SCREENPLAY_SCENE_SHARD_VERSION
    episode_no: int
    shard_id: str
    scene_plan_keys: list[str]
    scenes: list[ScreenplaySceneShardScene]
    consumed_source_ids: list[str] = Field(default_factory=list)
    unresolved_participants: list[UnresolvedParticipant] = Field(default_factory=list)
    source_hash: str = ""
    boundary_hash: str = ""
    blueprint_hash: str = ""
    identity_registry_hash: str = ""
    source_ownership_hash: str = ""
    identity_scaffold_hash: str = ""
    generation_scaffold_hash: str = ""


def _artifact_content(
    artifact: dict[str, Any],
) -> dict[str, Any] | None:
    content = artifact.get("content")
    if isinstance(content, dict):
        return content
    raw_content = artifact.get("content_json")
    try:
        content = (
            json.loads(raw_content)
            if isinstance(raw_content, str)
            else raw_content
        )
    except (TypeError, json.JSONDecodeError):
        return None
    return content if isinstance(content, dict) else None


def _artifact_parent_ids(
    artifact: dict[str, Any],
) -> set[str] | None:
    parents = artifact.get("parent_artifact_ids")
    if parents is None:
        raw_parents = artifact.get("parent_artifact_ids_json")
        try:
            parents = (
                json.loads(raw_parents)
                if isinstance(raw_parents, str)
                else raw_parents
            )
        except (TypeError, json.JSONDecodeError):
            return None
    if not isinstance(parents, list):
        return None
    return {str(parent_id) for parent_id in parents if str(parent_id)}


def _artifact_model_snapshot(
    artifact: dict[str, Any],
) -> dict[str, Any] | None:
    snapshot = artifact.get("model_snapshot")
    if isinstance(snapshot, dict):
        return snapshot
    raw_snapshot = artifact.get("model_snapshot_json")
    try:
        snapshot = (
            json.loads(raw_snapshot)
            if isinstance(raw_snapshot, str)
            else raw_snapshot
        )
    except (TypeError, json.JSONDecodeError):
        return None
    return snapshot if isinstance(snapshot, dict) else None


def _scene_shard_semantic_review_compatibility(
    artifact: dict[str, Any],
    raw_artifact: dict[str, Any] | None,
    *,
    current_shard_content_hash: str,
) -> tuple[bool, str]:
    """Bind a reusable shard to a clean review of the exact creative root."""
    if raw_artifact is None:
        return False, "semantic_review_raw_missing"
    raw_content = _artifact_content(raw_artifact)
    snapshot = _artifact_model_snapshot(artifact)
    if not isinstance(raw_content, dict) or not isinstance(snapshot, dict):
        return False, "semantic_review_metadata_missing"
    evidence = raw_content.get("semantic_review_evidence")
    if not isinstance(evidence, dict):
        return False, "semantic_review_evidence_missing"
    if (
        evidence.get("contract_version")
        != SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION
        or snapshot.get("semantic_review_version")
        != SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION
    ):
        return False, "semantic_review_version"
    reviewed_shard_content_hash = str(
        evidence.get("reviewed_shard_content_hash") or ""
    )
    snapshot_shard_content_hash = str(
        snapshot.get("reviewed_shard_content_hash") or ""
    )
    if (
        not reviewed_shard_content_hash
        or not snapshot_shard_content_hash
        or reviewed_shard_content_hash != snapshot_shard_content_hash
        or reviewed_shard_content_hash != current_shard_content_hash
    ):
        return False, "semantic_review_shard_hash"
    initial_hash = str(evidence.get("initial_creative_hash") or "")
    reviewed_hash = str(evidence.get("reviewed_creative_hash") or "")
    if len(initial_hash) != 64 or len(reviewed_hash) != 64:
        return False, "semantic_review_hash_missing"
    if snapshot.get("reviewed_creative_hash") != reviewed_hash:
        return False, "semantic_review_hash_binding"
    phases = evidence.get("phases")
    if not isinstance(phases, list) or not phases:
        return False, "semantic_review_artifacts_missing"
    if any(not isinstance(phase, dict) for phase in phases):
        return False, "semantic_review_artifacts_missing"
    phase_names = [phase.get("phase") for phase in phases]
    if (
        len(phase_names) > SCREENPLAY_SCENE_SEMANTIC_MAX_REPAIR_ROUNDS + 1
        or phase_names[0] != "initial"
        or any(phase != "post_repair" for phase in phase_names[1:])
    ):
        return False, "semantic_review_phase"
    if str(phases[0].get("creative_hash") or "") != initial_hash:
        return False, "semantic_review_initial_candidate"
    if str(phases[-1].get("creative_hash") or "") != reviewed_hash:
        return False, "semantic_review_final_candidate"
    artifact_content = _artifact_content(artifact)
    try:
        validated_shard = ScreenplaySceneShardIR.model_validate(
            artifact_content
        )
    except (TypeError, ValidationError):
        return False, "semantic_review_artifact_schema"
    valid_unit_keys = {
        unit.unit_key
        for scene in validated_shard.scenes
        for unit in scene.units
    }
    recomputed_consensus: list[list[dict[str, Any]]] = []
    for phase_index, phase in enumerate(phases):
        reviews = phase.get("reviews")
        if not isinstance(reviews, list) or len(reviews) != 2:
            return False, "semantic_review_artifacts_missing"
        for review in reviews:
            findings = (
                review.get("findings")
                if isinstance(review, dict)
                else None
            )
            try:
                validated_findings = (
                    [
                        ScreenplaySceneShardSemanticFinding.model_validate(
                            finding
                        )
                        for finding in findings
                    ]
                    if isinstance(findings, list)
                    else []
                )
            except ValidationError:
                validated_findings = []
            if validated_findings:
                finding_keys = [
                    (finding.unit_key, finding.code)
                    for finding in validated_findings
                ]
                if len(finding_keys) != len(set(finding_keys)):
                    return False, "semantic_review_duplicate_finding"
        validated_reviews: list[ScreenplaySceneShardSemanticReview] = []
        for review in reviews:
            try:
                validated_reviews.append(
                    ScreenplaySceneShardSemanticReview.model_validate(review)
                )
            except ValidationError:
                return False, "semantic_review_schema"
        if any(
            finding.unit_key not in valid_unit_keys
            or any(
                related_unit_key not in valid_unit_keys
                for related_unit_key in finding.related_unit_keys
            )
            for review in validated_reviews
            for finding in review.findings
        ):
            return False, "semantic_review_unit_key"
        finding_keys = [
            [(finding.unit_key, finding.code) for finding in review.findings]
            for review in validated_reviews
        ]
        if any(len(keys) != len(set(keys)) for keys in finding_keys):
            return False, "semantic_review_duplicate_finding"
        expected_consensus = [
            finding.model_dump(mode="json")
            for finding in screenplay_scene_semantic_consensus(
                validated_reviews[0],
                validated_reviews[1],
            )
        ]
        if phase_index:
            allowed_unit_keys = {
                finding["unit_key"]
                for finding in recomputed_consensus[-1]
            }
            current_findings = [
                finding
                for review in validated_reviews
                for finding in review.findings
            ]
            if any(
                finding.unit_key not in allowed_unit_keys
                and set(finding.related_unit_keys).isdisjoint(
                    allowed_unit_keys
                )
                for finding in current_findings
            ):
                return False, "semantic_review_phase_contract"
        if phase.get("consensus") != expected_consensus:
            return False, "semantic_review_consensus"
        recomputed_consensus.append(expected_consensus)
    if (
        len(phase_names) == 1
        and recomputed_consensus[0]
    ) or (
        len(phase_names) > 1
        and (
            not all(recomputed_consensus[:-1])
            or recomputed_consensus[-1]
        )
    ):
        return False, "semantic_review_phase_contract"
    return True, ""


def screenplay_normalized_artifact_lineage_compatibility(
    artifact: dict[str, Any],
    raw_artifact: dict[str, Any] | None,
    *,
    expected_raw_type: str,
    expected_authority_artifact_ids: set[str],
) -> tuple[bool, str]:
    """Match the generator's normalized -> raw -> authority lineage."""
    normalized_parents = _artifact_parent_ids(artifact)
    if raw_artifact is None or normalized_parents != {str(raw_artifact.get("id") or "")}:
        return False, "normalized_parent"
    if raw_artifact.get("type") != expected_raw_type:
        return False, "raw_artifact_type"
    if raw_artifact.get("status") != "candidate":
        return False, "raw_artifact_status"
    if (
        raw_artifact.get("scope_type") != artifact.get("scope_type")
        or raw_artifact.get("scope_id") != artifact.get("scope_id")
    ):
        return False, "raw_artifact_scope"
    if (
        str(raw_artifact.get("contract_version") or "")
        != str(artifact.get("contract_version") or "")
    ):
        return False, "raw_artifact_contract_version"
    raw_content = _artifact_content(raw_artifact)
    try:
        raw_content_hash = evidence_repository.content_hash(
            raw_content,
            raw_artifact.get("file_path"),
        )
    except (OSError, TypeError, ValueError):
        return False, "raw_artifact_content_hash"
    if raw_content_hash != str(raw_artifact.get("content_hash") or ""):
        return False, "raw_artifact_content_hash"
    if _artifact_parent_ids(raw_artifact) != expected_authority_artifact_ids:
        return False, "raw_authority_parents"
    return True, ""


def screenplay_envelope_artifact_compatibility(
    artifact: dict[str, Any],
    *,
    expected_blueprint_hash: str,
    expected_identity_registry_hash: str,
    raw_artifact: dict[str, Any] | None = None,
    expected_authority_artifact_ids: set[str] | None = None,
) -> tuple[bool, str]:
    content = _artifact_content(artifact)
    if artifact.get("status") != "validated":
        return False, "artifact_status"
    if str(artifact.get("contract_version") or "") != SCREENPLAY_ENVELOPE_VERSION:
        return False, "artifact_contract_version"
    if not isinstance(content, dict):
        return False, "artifact_content"
    if evidence_repository.content_hash(content) != str(
        artifact.get("content_hash") or ""
    ):
        return False, "artifact_content_hash"
    try:
        envelope = ScreenplayEnvelopeIR.model_validate(content)
    except ValidationError:
        return False, "content_schema"
    if envelope.blueprint_hash != expected_blueprint_hash:
        return False, "blueprint_hash"
    if envelope.identity_registry_hash != expected_identity_registry_hash:
        return False, "identity_registry_hash"
    if expected_authority_artifact_ids is not None:
        return screenplay_normalized_artifact_lineage_compatibility(
            artifact,
            raw_artifact,
            expected_raw_type="screenplay_envelope_raw",
            expected_authority_artifact_ids=expected_authority_artifact_ids,
        )
    return True, ""


def screenplay_scene_shard_artifact_compatibility(
    artifact: dict[str, Any],
    *,
    expected_blueprint_hash: str = "",
    expected_identity_registry_hash: str = "",
    expected_generation_scaffold_hash: str = "",
    raw_artifact: dict[str, Any] | None = None,
    expected_authority_artifact_ids: set[str] | None = None,
) -> tuple[bool, str]:
    """Validate one persisted shard against the current resumable contract."""
    content = _artifact_content(artifact)
    if artifact.get("status") != "validated":
        return False, "artifact_status"
    if str(artifact.get("contract_version") or "") != SCREENPLAY_SCENE_SHARD_VERSION:
        return False, "artifact_contract_version"
    if not isinstance(content, dict):
        return False, "artifact_content"
    try:
        current_shard_content_hash = evidence_repository.content_hash(
            content,
            artifact.get("file_path"),
        )
    except (OSError, TypeError, ValueError):
        return False, "artifact_content_hash"
    if current_shard_content_hash != str(artifact.get("content_hash") or ""):
        return False, "artifact_content_hash"
    if str(content.get("contract_version") or "") != SCREENPLAY_SCENE_SHARD_VERSION:
        return False, "content_contract_version"
    if not expected_blueprint_hash or not expected_identity_registry_hash:
        return False, "expected_authority_hash_missing"
    if str(content.get("blueprint_hash") or "") != expected_blueprint_hash:
        return False, "blueprint_hash"
    if (
        str(content.get("identity_registry_hash") or "")
        != expected_identity_registry_hash
    ):
        return False, "identity_registry_hash"
    identity_hash = str(content.get("identity_scaffold_hash") or "")
    generation_hash = str(content.get("generation_scaffold_hash") or "")
    if not identity_hash or not generation_hash:
        return False, "scaffold_hash_missing"
    if (
        expected_generation_scaffold_hash
        and generation_hash != expected_generation_scaffold_hash
    ):
        return False, "generation_scaffold_hash"
    try:
        ScreenplaySceneShardIR.model_validate(content)
    except ValidationError:
        return False, "content_schema"
    if expected_authority_artifact_ids is not None:
        compatible, reason = screenplay_normalized_artifact_lineage_compatibility(
            artifact,
            raw_artifact,
            expected_raw_type="screenplay_scene_shard_raw",
            expected_authority_artifact_ids=expected_authority_artifact_ids,
        )
        if not compatible:
            return compatible, reason
        return _scene_shard_semantic_review_compatibility(
            artifact,
            raw_artifact,
            current_shard_content_hash=current_shard_content_hash,
        )
    return True, ""


_PARTICIPANT_PERCEPTION_CHANNELS = (
    "audible",
    "visible_effect",
    "visible_reaction",
)


def _contract_identity_scaffold_hash(
    contract: ScreenplaySceneInputContract,
) -> str:
    return _hash({
        "contract_version": SCREENPLAY_SCENE_INPUT_VERSION,
        "scene_plan_key": contract.scene_plan_key,
        "source_segment_ids": contract.source_segment_ids,
        "source_semantics": {
            source_id: semantics.model_dump(mode="json")
            for source_id, semantics in contract.source_semantics.items()
        },
        "participant_bindings": [
            binding.model_dump(mode="json")
            for binding in contract.participant_bindings
        ],
        "action_evidence": [
            evidence.model_dump(mode="json")
            for evidence in contract.action_evidence
        ],
        "unit_slots": [
            slot.model_dump(mode="json")
            for slot in contract.unit_slots
        ],
    })


def screenplay_scene_identity_scaffold_hash(
    scene_input_contracts: list[ScreenplaySceneInputContract],
) -> str:
    return _hash({
        "contract_version": SCREENPLAY_SCENE_INPUT_VERSION,
        "scenes": [
            {
                "scene_plan_key": contract.scene_plan_key,
                "identity_scaffold_hash": (
                    contract.identity_scaffold_hash
                    or _contract_identity_scaffold_hash(contract)
                ),
            }
            for contract in scene_input_contracts
        ],
    })


def screenplay_scene_generation_scaffold_hash(
    plan: ScreenplaySceneShardPlan,
    scene_input_contracts: list[ScreenplaySceneInputContract],
) -> str:
    return _hash({
        "shard_contract_version": SCREENPLAY_SCENE_SHARD_VERSION,
        "plan_contract_version": SCREENPLAY_SHARD_PLAN_VERSION,
        "input_contract_version": SCREENPLAY_SCENE_INPUT_VERSION,
        "creative_contract_version": SCREENPLAY_SCENE_CREATIVE_VERSION,
        "shard_id": plan.shard_id,
        "scene_plan_keys": plan.scene_plan_keys,
        "source_segment_ids": plan.source_segment_ids,
        "source_scene_owners": plan.source_scene_owners,
        "unit_slots": [
            slot.model_dump(mode="json")
            for slot in plan.unit_slots
        ],
        "scene_contracts": [
            {
                "scene_plan_key": contract.scene_plan_key,
                "identity_scaffold_hash": (
                    contract.identity_scaffold_hash
                    or _contract_identity_scaffold_hash(contract)
                ),
                "unit_slots": [
                    slot.model_dump(mode="json")
                    for slot in contract.unit_slots
                ],
            }
            for contract in scene_input_contracts
        ],
    })


def build_screenplay_scene_shard_repair_schema(
    shard: ScreenplaySceneShardIR | None = None,
    *,
    plan: ScreenplaySceneShardPlan,
    scene_input_contracts: list[ScreenplaySceneInputContract],
) -> dict[str, Any]:
    """Build one closed slot-content schema for initial and repair attempts."""
    del shard
    schema = deepcopy(ScreenplaySceneShardCreativeIR.model_json_schema())
    slot_schemas: dict[str, dict[str, Any]] = {}
    for slot in plan.unit_slots:
        constraints: dict[str, Any] = {"type": "object"}
        if slot.kind == "dialogue":
            constraints["properties"] = {
                "text": {"const": slot.source_text},
            }
        slot_schemas[slot.unit_key] = {
            "allOf": [
                {"$ref": "#/$defs/ScreenplaySceneShardCreativeUnit"},
                constraints,
            ],
        }
    slot_keys = [slot.unit_key for slot in plan.unit_slots]
    schema["properties"]["slots"] = {
        "type": "object",
        "properties": slot_schemas,
        "required": slot_keys,
        "additionalProperties": False,
        "minProperties": len(slot_keys),
        "maxProperties": len(slot_keys),
    }
    schema["x-schema-purpose"] = (
        "creative-content-for-deterministic-generation-slots"
    )
    schema["x-identity-scaffold-hash"] = (
        screenplay_scene_identity_scaffold_hash(scene_input_contracts)
    )
    schema["x-generation-scaffold-hash"] = (
        screenplay_scene_generation_scaffold_hash(
            plan,
            scene_input_contracts,
        )
    )
    return schema


def _ordered_unique(values: list[str]) -> list[str]:
    return [
        value
        for value in dict.fromkeys(
            str(item or "").strip() for item in values
        )
        if value
    ]


def _compile_unit_identity_scaffold(
    slot: ScreenplaySceneUnitSlotPlan,
    *,
    contract: ScreenplaySceneInputContract,
) -> tuple[ScreenplaySceneCompiledUnitSlot, list[str]]:
    errors: list[str] = []
    source_ids = _ordered_unique(slot.source_segment_ids)
    source_set = set(source_ids)
    participant_usages: dict[str, set[str]] = {}
    participant_channels: dict[str, list[str]] = {}
    scene_visible_keys: set[str] = set()
    voice_claims: list[str] = []
    decision_actor_keys: list[str] = []
    state_subject_claims: list[str] = []
    joint_state_subject_claims: list[list[str]] = []
    exact_decision_actor_keys: list[str] = []
    environment_only = False
    source_text_by_id = {
        segment.source_segment_id: segment.text
        for segment in contract.source_segments
    }

    for action in contract.action_evidence:
        if not source_set.intersection(action.source_segment_ids):
            continue
        if slot.source_unit_key in action.environment_source_unit_keys:
            environment_only = True
        for assignment in action.state_subject_assignments:
            if assignment.source_unit_key == slot.source_unit_key:
                joint_state_subject_claims.append(
                    _ordered_unique(assignment.identity_keys)
                )
        for participant in action.participants:
            if not source_set.intersection(
                participant.source_segment_ids
            ):
                continue
            if participant.usage == "visible":
                scene_visible_keys.add(participant.identity_key)
            if (
                participant.usage == "voice"
                and not participant.source_unit_keys
                and slot.kind == "dialogue"
            ):
                errors.append(
                    f"{slot.unit_key} voice identity evidence "
                    "缺少精确 source_unit_keys"
                )
                continue
            if (
                participant.usage == "state_subject"
                and not participant.source_unit_keys
            ):
                errors.append(
                    f"{slot.unit_key} state_subject identity evidence "
                    "缺少精确 source_unit_keys"
                )
                continue
            if (
                participant.source_unit_keys
                and slot.source_unit_key
                not in participant.source_unit_keys
            ):
                continue
            participant_usages.setdefault(
                participant.identity_key,
                set(),
            ).add(participant.usage)
            if participant.usage == "voice":
                voice_claims.append(participant.identity_key)
            if (
                participant.usage == "state_subject"
                and participant.identity_key not in state_subject_claims
            ):
                state_subject_claims.append(participant.identity_key)
            channels = participant_channels.setdefault(
                participant.identity_key,
                [],
            )
            for channel in participant.perception_channels:
                if channel not in channels:
                    channels.append(channel)
        if (
            action.decision_actor_key
            and action.decision_actor_key in participant_usages
            and action.decision_actor_key not in decision_actor_keys
        ):
            decision_actor_keys.append(action.decision_actor_key)
            if any(
                participant.identity_key == action.decision_actor_key
                and slot.source_unit_key in participant.source_unit_keys
                for participant in action.participants
            ):
                exact_decision_actor_keys.append(action.decision_actor_key)

    visible_keys = [
        identity_key
        for identity_key, usages in participant_usages.items()
        if "visible" in usages
    ]
    voice_keys = [
        identity_key
        for identity_key, usages in participant_usages.items()
        if "voice" in usages
    ]
    speaker_key: str | None = None
    actor_keys: list[str] = []
    target_keys: list[str] = []

    if slot.kind == "dialogue":
        if len(voice_claims) == 1 and voice_claims[0]:
            speaker_key = voice_claims[0]
        elif len(voice_claims) > 1:
            errors.append(
                f"{slot.unit_key} dialogue source unit 含多个 voice "
                "speaker evidence"
            )
        else:
            errors.append(
                f"{slot.unit_key} dialogue 缺少唯一 speaker "
                "voice identity evidence"
            )
        if speaker_key:
            actor_keys = [speaker_key]
            if decision_actor_keys == [speaker_key]:
                target_keys = [
                    key for key in visible_keys if key != speaker_key
                ]
    else:
        if len(decision_actor_keys) > 1:
            errors.append(
                f"{slot.unit_key} 来源含多个 decision actor，"
                "必须在 Blueprint 中拆分来源动作"
            )
        elif decision_actor_keys:
            actor_keys = list(decision_actor_keys)
            target_keys = [
                key for key in visible_keys
                if key not in decision_actor_keys
            ]
        source_has_dialogue_slot = slot.kind == "dialogue"
        if voice_keys and not source_has_dialogue_slot:
            if len(voice_keys) == 1:
                speaker_key = voice_keys[0]
            else:
                errors.append(
                    f"{slot.unit_key} 无对白结构的来源含多个 voice identity，"
                    "必须在 Blueprint 中拆分来源"
                )

    typed_actor_claims = _ordered_unique(exact_decision_actor_keys)
    unspoken_owner_key = (
        slot.content_owner_key
        if (
            slot.delivery_mode == "unspoken_reference"
            and slot.content_owner_key
        )
        else ""
    )
    if (
        unspoken_owner_key
        and unspoken_owner_key in scene_visible_keys
        and unspoken_owner_key not in visible_keys
    ):
        visible_keys.append(unspoken_owner_key)
    if unspoken_owner_key:
        if joint_state_subject_claims:
            if (
                len(joint_state_subject_claims) != 1
                or joint_state_subject_claims[0]
                != [unspoken_owner_key]
            ):
                errors.append(
                    f"{slot.unit_key} unspoken content owner 与 joint "
                    "state_subject 冲突"
                )
        elif state_subject_claims:
            if set(state_subject_claims) != {unspoken_owner_key}:
                errors.append(
                    f"{slot.unit_key} unspoken content owner 与 single "
                    "state_subject 冲突"
                )
        else:
            state_subject_claims.append(unspoken_owner_key)
    state_subject_keys: list[str] = []
    state_subject_key = ""
    if len(joint_state_subject_claims) > 1:
        errors.append(
            f"{slot.unit_key} 存在多个 joint state_subject assignment"
        )
    elif joint_state_subject_claims and state_subject_claims:
        errors.append(
            f"{slot.unit_key} 同时声明 single 与 joint state_subject"
        )
    elif joint_state_subject_claims:
        state_subject_keys = joint_state_subject_claims[0]
    elif len(state_subject_claims) > 1:
        errors.append(
            f"{slot.unit_key} 存在多个 state_subject identity evidence："
            f"{state_subject_claims}"
        )
    elif state_subject_claims:
        state_subject_keys = [state_subject_claims[0]]
    elif slot.kind == "dialogue" and speaker_key:
        state_subject_keys = [speaker_key]
    elif len(typed_actor_claims) == 1:
        state_subject_keys = [typed_actor_claims[0]]
    elif len(typed_actor_claims) > 1:
        errors.append(
            f"{slot.unit_key} 存在多个 exact-unit typed actor "
            f"{typed_actor_claims}，必须由 Blueprint "
            "usage=state_subject 唯一冻结"
        )
    if len(state_subject_keys) == 1:
        state_subject_key = state_subject_keys[0]
    if (
        slot.kind == "action"
        and state_subject_keys
    ):
        actor_keys = _ordered_unique([*actor_keys, *state_subject_keys])
    if environment_only and (
        state_subject_keys
        or state_subject_claims
        or joint_state_subject_claims
        or typed_actor_claims
        or (slot.kind == "dialogue" and speaker_key)
    ):
        errors.append(
            f"{slot.unit_key} 同时声明人物主体与 environment_only"
        )
    elif not environment_only and not state_subject_keys:
        errors.append(
            f"{slot.unit_key} 缺少 single/joint state_subject 结构证据，"
            "且未显式声明 environment_only"
        )
    relation_keys = _ordered_unique([
        *actor_keys,
        *target_keys,
        *([speaker_key] if speaker_key else []),
    ])
    participant_deliveries: list[IRActionParticipantDelivery] = []
    observable_basis = slot.source_text.strip() or " ".join(
        source_text_by_id.get(source_id, "")
        for source_id in source_ids
    ).strip()
    for participant_key in relation_keys:
        if participant_key in visible_keys:
            continue
        channels = participant_channels.get(participant_key, [])
        if not channels:
            errors.append(
                f"{slot.unit_key} 画外参与者 "
                f"{participant_key} 缺少确定性可感知通道"
            )
            continue
        participant_deliveries.append(IRActionParticipantDelivery(
            participant_key=participant_key,
            observable_claim=(
                f"{participant_key} 通过 {','.join(channels)} 交付来源 "
                f"{','.join(source_ids)}：{observable_basis[:120]}"
            ),
            audible="audible" in channels,
            visible_effect="visible_effect" in channels,
            visible_reaction="visible_reaction" in channels,
        ))

    return ScreenplaySceneCompiledUnitSlot(
        **slot.model_dump(
            mode="python",
            exclude={
                "state_subject_key",
                "state_subject_keys",
                "environment_only",
            },
        ),
        actor_keys=actor_keys,
        target_keys=target_keys,
        onscreen_entity_keys=visible_keys,
        participant_deliveries=participant_deliveries,
        speaker_key=speaker_key,
        state_subject_key=state_subject_key,
        state_subject_keys=state_subject_keys,
        environment_only=environment_only,
        action_agency=ActionAgency(
            kind="character" if relation_keys else "unattributed",
            identity_bearing=bool(relation_keys),
            source_segment_ids=source_ids,
        ),
    ), errors


def _structural_slot(
    slot: ScreenplaySceneCompiledUnitSlot,
) -> ScreenplaySceneUnitSlotPlan:
    compiler_owned = {
        "state_subject_key",
        "state_subject_keys",
        "environment_only",
    }
    return ScreenplaySceneUnitSlotPlan.model_validate(
        slot.model_dump(
            mode="python",
            include=(
                set(ScreenplaySceneUnitSlotPlan.model_fields)
                - compiler_owned
            ),
        )
    )


def _compile_text_provenance(
    *,
    creative_unit: ScreenplaySceneShardCreativeUnit,
    compiled_slot: ScreenplaySceneCompiledUnitSlot,
) -> tuple[TextProvenance, str]:
    if creative_unit.required_text.strip():
        provenance_kind = "required_text"
    elif creative_unit.prop_text.strip():
        provenance_kind = "prop_text"
    elif creative_unit.on_screen_text.strip():
        provenance_kind = "on_screen_text"
    elif compiled_slot.kind == "dialogue":
        provenance_kind = "dialogue"
    else:
        provenance_kind = "creative_action"
    relation_identity_keys = _ordered_unique([
        *compiled_slot.actor_keys,
        *compiled_slot.target_keys,
        *(
            [compiled_slot.speaker_key]
            if compiled_slot.speaker_key
            else []
        ),
    ])
    provenance = TextProvenance(
        kind=provenance_kind,
        identity_keys=(
            []
            if provenance_kind in (
                "required_text", "prop_text", "on_screen_text",
            )
            else relation_identity_keys
        ),
        content_owner_keys=(
            [compiled_slot.content_owner_key]
            if compiled_slot.content_owner_key
            else []
        ),
        source_segment_ids=list(compiled_slot.source_segment_ids),
    )
    agency_kind = (
        compiled_slot.action_agency.kind
        if relation_identity_keys
        else provenance_kind
        if provenance_kind in (
            "required_text", "prop_text", "on_screen_text",
        )
        else "unattributed"
    )
    return provenance, agency_kind


def compile_screenplay_scene_shard_draft(
    draft: ScreenplaySceneShardCreativeIR,
    *,
    episode_no: int,
    plan: ScreenplaySceneShardPlan,
    scene_plans: dict[str, BlueprintScenePlan],
    scene_input_contracts: list[ScreenplaySceneInputContract],
) -> ScreenplaySceneShardIR:
    """Join creative slot content to the immutable generation scaffold."""
    errors: list[str] = []
    expected_slot_keys = [
        slot.unit_key for slot in plan.unit_slots
    ]
    actual_slot_keys = set(draft.slots)
    missing_slot_keys = [
        unit_key
        for unit_key in expected_slot_keys
        if unit_key not in actual_slot_keys
    ]
    extra_slot_keys = sorted(
        actual_slot_keys - set(expected_slot_keys)
    )
    if missing_slot_keys:
        errors.append(
            "[GENERATION_CONTRACT] 缺失 slot："
            + ",".join(missing_slot_keys)
        )
    if extra_slot_keys:
        errors.append(
            "[GENERATION_CONTRACT] 多余 slot："
            + ",".join(extra_slot_keys)
        )
    for planned_slot in plan.unit_slots:
        creative_unit = draft.slots.get(planned_slot.unit_key)
        if (
            creative_unit is not None
            and planned_slot.kind == "dialogue"
            and creative_unit.text.strip() != planned_slot.source_text.strip()
        ):
            errors.append(
                f"{planned_slot.unit_key} dialogue.text 必须等于 "
                "scaffold source_text"
            )

    contracts_by_scene = {
        contract.scene_plan_key: contract
        for contract in scene_input_contracts
    }
    compiled_slots_by_key: dict[
        str, ScreenplaySceneCompiledUnitSlot
    ] = {}
    plan_slots_by_key = {
        slot.unit_key: slot for slot in plan.unit_slots
    }
    for contract in scene_input_contracts:
        for compiled_slot in contract.unit_slots:
            if compiled_slot.unit_key in compiled_slots_by_key:
                errors.append(
                    "[GENERATION_CONTRACT] unit_key 重复："
                    + compiled_slot.unit_key
                )
                continue
            compiled_slots_by_key[compiled_slot.unit_key] = compiled_slot
            planned_slot = plan_slots_by_key.get(compiled_slot.unit_key)
            if planned_slot is None:
                errors.append(
                    "[GENERATION_CONTRACT] 输入合同含未计划 slot："
                    + compiled_slot.unit_key
                )
            elif _structural_slot(compiled_slot) != planned_slot:
                errors.append(
                    "[GENERATION_CONTRACT] slot 结构漂移："
                    + compiled_slot.unit_key
                )
    missing_compiled_slots = [
        unit_key
        for unit_key in expected_slot_keys
        if unit_key not in compiled_slots_by_key
    ]
    if missing_compiled_slots:
        errors.append(
            "[GENERATION_CONTRACT] 输入合同缺失 slot："
            + ",".join(missing_compiled_slots)
        )
    if errors:
        raise ScreenplaySceneShardError(plan.shard_id, errors)

    scenes: list[ScreenplaySceneShardScene] = []
    consumed_source_ids: list[str] = []
    for scene_key in plan.scene_plan_keys:
        scene_plan = scene_plans.get(scene_key)
        contract = contracts_by_scene.get(scene_key)
        if scene_plan is None or contract is None:
            errors.append(f"{scene_key} 缺少 scene plan 或 identity scaffold")
            continue
        units: list[ScreenplaySceneShardUnit] = []
        character_keys: list[str] = []
        for planned_slot in (
            slot for slot in plan.unit_slots
            if slot.scene_key == scene_key
        ):
            compiled_slot = compiled_slots_by_key[planned_slot.unit_key]
            creative_unit = draft.slots[planned_slot.unit_key]
            if planned_slot.delivery_mode == "written_text":
                exact_text = planned_slot.source_text.strip().strip(
                    "“”「」『』\"'"
                )
                creative_unit = creative_unit.model_copy(update={
                    "required_text": exact_text,
                    "prop_text": "",
                    "on_screen_text": "",
                })
            text = creative_unit.text.strip()
            text_provenance, agency_kind = _compile_text_provenance(
                creative_unit=creative_unit,
                compiled_slot=compiled_slot,
            )
            unit = ScreenplaySceneShardUnit(
                unit_key=planned_slot.unit_key,
                kind=planned_slot.kind,
                text=text,
                event_key=planned_slot.event_key,
                narrative_layer=planned_slot.narrative_layer,
                event_priority=planned_slot.event_priority,
                render_policy=planned_slot.render_policy,
                source_segment_ids=list(
                    planned_slot.source_segment_ids
                ),
                actor_keys=list(compiled_slot.actor_keys),
                target_keys=list(compiled_slot.target_keys),
                onscreen_entity_keys=list(
                    compiled_slot.onscreen_entity_keys
                ),
                participant_deliveries=[
                    delivery.model_copy(deep=True)
                    for delivery in compiled_slot.participant_deliveries
                ],
                action_agency=ActionAgency(
                    kind=agency_kind,
                    identity_bearing=(
                        compiled_slot.action_agency.identity_bearing
                    ),
                    source_segment_ids=list(
                        compiled_slot.action_agency.source_segment_ids
                    ),
                ),
                text_provenance=text_provenance,
                required_text=creative_unit.required_text,
                prop_text=creative_unit.prop_text,
                on_screen_text=creative_unit.on_screen_text,
                resulting_state=creative_unit.resulting_state,
                speaker_key=compiled_slot.speaker_key,
                state_subject_key=compiled_slot.state_subject_key,
                state_subject_keys=list(compiled_slot.state_subject_keys),
                environment_only=compiled_slot.environment_only,
                function=creative_unit.function,
                source_text=planned_slot.source_text,
                chain_key="",
                performance=creative_unit.performance,
            )
            units.append(unit)
            for identity_key in [
                *unit.actor_keys,
                *unit.target_keys,
                *unit.onscreen_entity_keys,
                *([unit.speaker_key] if unit.speaker_key else []),
                *[
                    delivery.participant_key
                    for delivery in unit.participant_deliveries
                ],
            ]:
                if identity_key not in character_keys:
                    character_keys.append(identity_key)
            for source_id in unit.source_segment_ids:
                if source_id not in consumed_source_ids:
                    consumed_source_ids.append(source_id)
        context_requirements = _ordered_unique([
            relation.summary
            for relation in contract.derived_relations
            if relation.summary.strip()
        ])
        opening = scene_plan.opening_image.strip()
        exit_state = scene_plan.exit_state.strip()
        summary = "；".join(
            value for value in (opening, exit_state) if value
        ) or scene_plan.scene_heading
        story_function = "推进本场事件并完成状态变化：" + summary
        scenes.append(ScreenplaySceneShardScene(
            key=scene_key,
            scene_heading=scene_plan.scene_heading,
            story_function=story_function,
            character_keys=character_keys,
            summary=summary,
            conflict="",
            turn=exit_state,
            source_basis=",".join(scene_plan.source_segment_ids),
            previous_scene_exit_state=scene_plan.previous_scene_exit_state,
            opening_image=scene_plan.opening_image,
            agency_contracts=deepcopy(scene_plan.agency_contracts),
            entry_state=scene_plan.previous_scene_exit_state,
            exit_state=scene_plan.exit_state,
            context_requirements=context_requirements,
            units=units,
        ))
    if errors:
        raise ScreenplaySceneShardError(plan.shard_id, errors)
    return ScreenplaySceneShardIR(
        episode_no=episode_no,
        shard_id=plan.shard_id,
        scene_plan_keys=list(plan.scene_plan_keys),
        scenes=scenes,
        consumed_source_ids=consumed_source_ids,
        unresolved_participants=[],
        source_hash=plan.source_hash,
        boundary_hash=plan.boundary_hash,
        blueprint_hash=plan.blueprint_hash,
        identity_registry_hash=plan.identity_registry_hash,
        source_ownership_hash=plan.source_ownership_hash,
        identity_scaffold_hash=screenplay_scene_identity_scaffold_hash(
            scene_input_contracts
        ),
        generation_scaffold_hash=(
            screenplay_scene_generation_scaffold_hash(
                plan,
                scene_input_contracts,
            )
        ),
    )


class ScreenplaySceneShardError(ValueError):
    def __init__(self, shard_id: str, errors: list[str]):
        self.shard_id = shard_id
        self.errors = list(errors)
        super().__init__(f"{shard_id}: " + "；".join(errors[:10]))


class ScreenplaySceneMergeError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("；".join(errors[:20]))


class ScreenplaySceneShardOwnershipLost(RuntimeError):
    """A provider response returned after another run acquired the episode."""


def _assert_episode_owner(episode_id: str) -> None:
    trace = current_trace()
    if not trace.run_id:
        return
    row = get_conn().execute(
        "SELECT active_screenplay_run_id FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    if not row or row["active_screenplay_run_id"] != trace.run_id:
        raise ScreenplaySceneShardOwnershipLost(
            "场次分片返回时剧集 owner 已变化，旧 worker 不得持久化结果"
        )


def blueprint_content_hash(blueprint: NarrativeBlueprint) -> str:
    return _hash(blueprint.model_dump(mode="json"))


def _source_ownership_hash(blueprint: NarrativeBlueprint) -> str:
    return _hash({
        "source_scene_owners": blueprint.source_scene_owners,
        "source_semantics": {
            source_id: semantics.model_dump(mode="json")
            for source_id, semantics in blueprint.source_semantics.items()
        },
        "scene_derivations": [
            relation.model_dump(mode="json")
            for relation in blueprint.scene_derivations
        ],
    })


def build_frozen_identity_registry(
    bible: Bible,
    resolutions: list[dict[str, Any]] | None,
) -> tuple[list[IRIdentity], list[dict[str, Any]], str]:
    """Project durable authorities into stable IR identity keys."""
    authorities = identity_authority_registry(bible, resolutions)
    identities: list[IRIdentity] = []
    projected: list[dict[str, Any]] = []
    for authority in sorted(
        authorities,
        key=lambda item: str(item.get("authority_id") or ""),
    ):
        authority_id = str(authority.get("authority_id") or "").strip()
        if not authority_id:
            continue
        canonical_name = str(
            authority.get("canonical_name") or authority_id
        ).strip()
        source_names = list(dict.fromkeys(
            [canonical_name]
            + [
                str(value).strip()
                for value in authority.get("source_labels") or []
                if str(value).strip()
            ]
        ))
        digest = hashlib.sha256(authority_id.encode("utf-8")).hexdigest()[:12]
        identity_key = f"person_{digest}"
        named = str(authority.get("identity_kind") or "") == "named"
        reference_only = (
            str(authority.get("identity_kind") or "") == "reference"
        )
        identity = IRIdentity(
            key=identity_key,
            display_name=canonical_name,
            authority_id=authority_id,
            source_names=source_names,
            kind=(
                "referenced_identity"
                if reference_only
                else "named_character" if named else "functional_character"
            ),
            visual_policy=(
                "offscreen_only"
                if reference_only
                else "canonical" if named else "contextual"
            ),
            visual_canonical=(
                ""
                if named or reference_only
                else functional_extra_anchor(
                    canonical_name,
                    declared_functional_names={canonical_name},
                )
            ),
            asset_requirement=(
                "forbidden"
                if reference_only
                else "required" if named else "optional"
            ),
            voice_canonical="",
            role_type=(
                "named_character"
                if named or reference_only
                else "functional_character"
            ),
            rationale="来自冻结的人物谱/本集身份决议",
        )
        identities.append(identity)
        projected.append({
            **authority,
            "identity_key": identity_key,
            "source_instance_key": str(
                authority.get("source_instance_key")
                or authority.get("identity_group")
                or authority_id
            ),
        })
    registry_hash = _hash(projected)
    return identities, projected, registry_hash


def _identity_aliases(
    identity_registry: list[dict[str, Any]],
    *,
    identity_keys: set[str] | None = None,
) -> dict[str, str]:
    candidates: dict[str, set[str]] = {
        key: {key} for key in (identity_keys or set())
    }
    for item in identity_registry:
        identity_key = str(item.get("identity_key") or "").strip()
        if not identity_key:
            continue
        for value in (
            identity_key,
            item.get("authority_id"),
            item.get("identity_group"),
            item.get("source_instance_key"),
            item.get("canonical_name"),
            *(item.get("source_labels") or []),
        ):
            label = str(value or "").strip()
            if label:
                candidates.setdefault(label, set()).add(identity_key)
    conflicts = {
        reference: sorted(keys)
        for reference, keys in candidates.items()
        if len(keys) > 1
    }
    if conflicts:
        raise ScreenplaySceneShardError(
            "identity-registry",
            [
                "typed identity reference 指向多个 canonical identity："
                f"{reference}={keys}"
                for reference, keys in sorted(conflicts.items())
            ],
        )
    return {
        reference: next(iter(keys))
        for reference, keys in candidates.items()
        if keys
    }


def _scene_estimate(
    scene_plan: BlueprintScenePlan,
    source_by_id: dict[str, str],
) -> tuple[int, int]:
    source_chars = sum(
        len(re.sub(r"\s+", "", source_by_id.get(source_id, "")))
        for source_id in scene_plan.source_segment_ids
    )
    units = max(
        2,
        len(scene_plan.source_segment_ids),
        math.ceil(source_chars / 90) + max(0, scene_plan.dramatic_load - 1),
    )
    output_chars = max(1200, units * 460 + source_chars * 2)
    return units, output_chars


def _build_group_unit_slots(
    group: list[BlueprintScenePlan],
    *,
    source_by_id: dict[str, str],
    scene_order_by_key: dict[str, int],
    delivery_by_unit: dict[str, Any] | None = None,
) -> list[ScreenplaySceneUnitSlotPlan]:
    legacy_delivery_fallback = delivery_by_unit is None
    delivery_by_unit = delivery_by_unit or {}
    slots: list[ScreenplaySceneUnitSlotPlan] = []
    unit_order = 0
    for scene_plan in group:
        scene_unit_order = 0
        for source_id in scene_plan.source_segment_ids:
            semantics = scene_plan.source_semantics.get(source_id)
            if semantics is None:
                raise ValueError(
                    f"{scene_plan.key} 缺少 {source_id} 的显式来源语义"
                )
            if semantics.projection_policy != "picture":
                raise ValueError(
                    f"{scene_plan.key} 不得为 {source_id} 的 "
                    f"{semantics.projection_policy} 投影生成创作 slot"
                )
            for fact in source_segment_facts(
                source_id,
                source_by_id.get(source_id, ""),
            ):
                if fact.projection == "paratext":
                    continue
                unit_order += 1
                scene_unit_order += 1
                source_part_order = fact.unit_order
                source_part = fact.text
                delivery = (
                    delivery_by_unit.get(fact.source_unit_key)
                    if fact.projection == "quoted"
                    else None
                )
                if (
                    fact.projection == "quoted"
                    and delivery is None
                    and not legacy_delivery_fallback
                ):
                    raise ValueError(
                        f"{fact.source_unit_key} 缺少 quoted source delivery"
                    )
                delivery_mode = (
                    delivery.mode
                    if delivery is not None
                    else (
                        "spoken_dialogue"
                        if fact.projection == "quoted"
                        else "action"
                    )
                )
                kind = (
                    "dialogue"
                    if delivery_mode in {
                        "spoken_dialogue",
                        "offscreen_voice",
                    }
                    else "action"
                )
                key_base = (
                    f"{scene_plan.key}:{source_id}:"
                    f"{source_part_order:03d}"
                )
                slots.append(ScreenplaySceneUnitSlotPlan(
                    unit_key=f"{key_base}:unit",
                    event_key=f"{key_base}:event",
                    scene_key=scene_plan.key,
                    scene_order=scene_order_by_key[scene_plan.key],
                    unit_order=unit_order,
                    scene_unit_order=scene_unit_order,
                    kind=kind,
                    narrative_layer=semantics.narrative_layer,
                    event_priority=semantics.event_priority,
                    render_policy=semantics.render_policy,
                    source_segment_ids=[source_id],
                    source_unit_key=fact.source_unit_key,
                    source_text=(
                        source_part
                        if fact.projection == "quoted"
                        else ""
                    ),
                    source_surface=fact.surface_form,
                    delivery_mode=delivery_mode,
                    content_owner_key=(
                        delivery.content_owner_key
                        if delivery is not None else ""
                    ),
                    performer_key=(
                        delivery.performer_key
                        if delivery is not None else ""
                    ),
                ))
    return slots


def _screenplay_scene_shard_required_tokens(
    *,
    estimated_output_chars: int,
    estimated_units: int,
    scene_count: int,
) -> int:
    content_tokens = math.ceil(max(1, estimated_output_chars) / 1.5)
    structural_reserve = (
        max(1, scene_count) * SCREENPLAY_SCENE_SHARD_SCENE_RESERVE_TOKENS
        + max(1, estimated_units) * SCREENPLAY_SCENE_SHARD_UNIT_RESERVE_TOKENS
    )
    subtotal = content_tokens + structural_reserve
    return math.ceil(
        subtotal
        * (100 + SCREENPLAY_SCENE_SHARD_REASONING_RESERVE_PERCENT)
        / 100
    )


def screenplay_scene_shard_token_budget(
    plan: ScreenplaySceneShardPlan,
) -> int:
    """Return a bounded output budget derived from the shard structure."""
    required = _screenplay_scene_shard_required_tokens(
        estimated_output_chars=plan.estimated_output_chars,
        estimated_units=plan.estimated_units,
        scene_count=len(plan.scene_plan_keys),
    )
    return max(
        SCREENPLAY_SCENE_SHARD_MIN_OUTPUT_TOKENS,
        min(SCREENPLAY_SCENE_SHARD_MAX_OUTPUT_TOKENS, required),
    )


def _screenplay_scene_shard_budget_meta(
    plan: ScreenplaySceneShardPlan,
) -> dict[str, int | bool]:
    content_tokens = math.ceil(plan.estimated_output_chars / 1.5)
    structural_reserve = (
        len(plan.scene_plan_keys)
        * SCREENPLAY_SCENE_SHARD_SCENE_RESERVE_TOKENS
        + plan.estimated_units
        * SCREENPLAY_SCENE_SHARD_UNIT_RESERVE_TOKENS
    )
    required = _screenplay_scene_shard_required_tokens(
        estimated_output_chars=plan.estimated_output_chars,
        estimated_units=plan.estimated_units,
        scene_count=len(plan.scene_plan_keys),
    )
    return {
        "estimated_output_chars": plan.estimated_output_chars,
        "estimated_units": plan.estimated_units,
        "estimated_content_tokens": content_tokens,
        "structural_reserve_tokens": structural_reserve,
        "reasoning_reserve_tokens": (
            required - content_tokens - structural_reserve
        ),
        "required_output_tokens": required,
        "output_budget_tokens": screenplay_scene_shard_token_budget(plan),
        "output_budget_limited": (
            required > SCREENPLAY_SCENE_SHARD_MAX_OUTPUT_TOKENS
        ),
    }


def build_screenplay_scene_shard_plans(
    blueprint: NarrativeBlueprint,
    *,
    source_text: str,
    identity_registry_hash: str,
    identity_registry: list[dict[str, Any]] | None = None,
    max_units: int | None = None,
    max_output_chars: int | None = None,
) -> list[ScreenplaySceneShardPlan]:
    """Deterministically group consecutive Blueprint-owned scene plans."""
    derive_blueprint_scene_plans(blueprint)
    max_units = max_units or _setting_int(
        "screenplay_scene_shard_max_units", 24, minimum=8, maximum=64
    )
    max_output_chars = max_output_chars or _setting_int(
        "screenplay_scene_shard_max_output_chars", 12000,
        minimum=3000, maximum=30000,
    )
    source_by_id = {
        segment.segment_id: segment.text
        for segment in index_source_segments(source_text)
    }
    identity_aliases = (
        _identity_aliases(identity_registry)
        if identity_registry is not None
        else {}
    )
    delivery_by_unit: dict[str, Any] = {}
    for node in blueprint.nodes:
        if node.source_semantics().projection_policy != "picture":
            continue
        for delivery in effective_source_unit_deliveries(node):
            if delivery.source_unit_key in delivery_by_unit:
                raise ValueError(
                    f"{delivery.source_unit_key} 含多个 quoted source delivery"
                )
            content_owner_key = delivery.content_owner_key.strip()
            performer_key = delivery.performer_key.strip()
            if content_owner_key and identity_registry is not None:
                frozen_owner = identity_aliases.get(content_owner_key, "")
                if not frozen_owner:
                    raise ValueError(
                        f"{delivery.source_unit_key} content owner 未冻结："
                        f"{content_owner_key}"
                    )
                content_owner_key = frozen_owner
            if performer_key and identity_registry is not None:
                frozen_performer = identity_aliases.get(performer_key, "")
                if not frozen_performer:
                    raise ValueError(
                        f"{delivery.source_unit_key} performer 未冻结："
                        f"{performer_key}"
                    )
                performer_key = frozen_performer
            delivery_by_unit[delivery.source_unit_key] = (
                delivery.model_copy(update={
                    "content_owner_key": content_owner_key,
                    "performer_key": performer_key,
                })
            )
    blueprint_hash = blueprint_content_hash(blueprint)
    source_ownership_hash = _source_ownership_hash(blueprint)
    groups: list[list[BlueprintScenePlan]] = []
    current: list[BlueprintScenePlan] = []
    current_units = 0
    current_chars = 0
    current_domain = ""
    for plan in blueprint.scene_plans:
        units, output_chars = _scene_estimate(plan, source_by_id)
        candidate_required_tokens = _screenplay_scene_shard_required_tokens(
            estimated_output_chars=current_chars + output_chars,
            estimated_units=current_units + units,
            scene_count=len(current) + 1,
        )
        would_overflow = bool(current) and (
            current_units + units > max_units
            or current_chars + output_chars > max_output_chars
            or candidate_required_tokens
            > SCREENPLAY_SCENE_SHARD_MAX_OUTPUT_TOKENS
        )
        # A temporal-domain change is a natural retry boundary.  Never combine
        # a later domain back into an earlier shard.
        domain_break = bool(current) and plan.temporal_domain_key != current_domain
        if would_overflow or domain_break:
            groups.append(current)
            current = []
            current_units = 0
            current_chars = 0
        current.append(plan)
        current_units += units
        current_chars += output_chars
        current_domain = plan.temporal_domain_key
    if current:
        groups.append(current)

    plans: list[ScreenplaySceneShardPlan] = []
    previous_boundary: dict[str, Any] = {}
    scene_order_by_key = {
        scene_plan.key: scene_order
        for scene_order, scene_plan in enumerate(
            blueprint.scene_plans,
            start=1,
        )
    }
    for index, group in enumerate(groups, start=1):
        group_scene_keys = [plan.key for plan in group]
        source_ids = [
            source_id
            for source_id, owner_scene_key
            in blueprint.source_scene_owners.items()
            if owner_scene_key in group_scene_keys
        ]
        estimated = [_scene_estimate(plan, source_by_id) for plan in group]
        boundary_in = dict(previous_boundary)
        boundary_out = {
            "scene_key": group[-1].key,
            "temporal_domain_key": group[-1].temporal_domain_key,
            "location_key": group[-1].location_key,
            "exit_state": group[-1].exit_state,
        }
        source_hash = _hash({
            source_id: source_by_id.get(source_id, "") for source_id in source_ids
        })
        boundary_hash = _hash({"in": boundary_in, "out": boundary_out})
        unit_slots = _build_group_unit_slots(
            group,
            source_by_id=source_by_id,
            scene_order_by_key=scene_order_by_key,
            delivery_by_unit=delivery_by_unit,
        )
        plans.append(ScreenplaySceneShardPlan(
            shard_id=f"SS{index:03d}",
            scene_plan_keys=group_scene_keys,
            source_segment_ids=source_ids,
            source_scene_owners=dict(blueprint.source_scene_owners),
            unit_slots=unit_slots,
            derived_relations=[
                relation.model_copy(deep=True)
                for relation in blueprint.scene_derivations
                if relation.target_scene_plan_key in group_scene_keys
            ],
            source_ownership_hash=source_ownership_hash,
            estimated_units=len(unit_slots),
            estimated_output_chars=sum(value[1] for value in estimated),
            boundary_state_in=boundary_in,
            boundary_state_out=boundary_out,
            source_hash=source_hash,
            boundary_hash=boundary_hash,
            blueprint_hash=blueprint_hash,
            identity_registry_hash=identity_registry_hash,
        ))
        previous_boundary = boundary_out
    return plans


def build_screenplay_scene_input_contracts(
    *,
    plan: ScreenplaySceneShardPlan,
    scene_plans: list[BlueprintScenePlan],
    source_by_id: dict[str, str],
    identity_registry: list[dict[str, Any]],
    blueprint_nodes: list[NarrativeNode] | None = None,
) -> list[ScreenplaySceneInputContract]:
    """Bind source text and canonical action evidence before scene writing."""
    errors: list[str] = []
    scene_keys = [scene_plan.key for scene_plan in scene_plans]
    if scene_keys != plan.scene_plan_keys:
        errors.append(
            "逐场输入合同与 shard plan 场次不一致："
            f"expected={plan.scene_plan_keys}, actual={scene_keys}"
        )

    projected_source_ids = [
        source_id
        for source_id, owner_scene_key in plan.source_scene_owners.items()
        if owner_scene_key in scene_keys
    ]
    if projected_source_ids != plan.source_segment_ids:
        errors.append(
            "逐场输入合同的唯一 SRC 投影与 shard plan 不一致："
            f"expected={plan.source_segment_ids}, actual={projected_source_ids}"
        )

    aliases = _identity_aliases(identity_registry)
    nodes_by_key = {
        node.key: node for node in (blueprint_nodes or [])
    }
    contracts: list[ScreenplaySceneInputContract] = []
    for scene_plan in scene_plans:
        owned_source_ids = [
            source_id
            for source_id, owner_scene_key
            in plan.source_scene_owners.items()
            if owner_scene_key == scene_plan.key
        ]
        if scene_plan.source_segment_ids != owned_source_ids:
            conflicting_source_ids = [
                source_id
                for source_id in scene_plan.source_segment_ids
                if plan.source_scene_owners.get(source_id)
                != scene_plan.key
            ]
            if conflicting_source_ids:
                errors.extend(
                    f"{source_id} 唯一归属 "
                    f"{plan.source_scene_owners.get(source_id) or '未定义'}，"
                    f"不得由 {scene_plan.key} 消费"
                    for source_id in conflicting_source_ids
                )
            else:
                errors.append(
                    f"{scene_plan.key} source_segment_ids 与唯一 owner 投影不一致"
                )
        missing_source_ids = [
            source_id
            for source_id in owned_source_ids
            if source_id not in source_by_id
        ]
        if missing_source_ids:
            errors.append(
                f"{scene_plan.key} 输入合同缺少来源正文："
                + ",".join(missing_source_ids)
            )
        unresolved_participants = [
            participant
            for participant in scene_plan.participant_keys
            if participant not in aliases
        ]
        if unresolved_participants:
            errors.append(
                f"{scene_plan.key} Blueprint participant 未冻结："
                + ",".join(unresolved_participants)
            )
        action_evidence: list[ScreenplaySceneActionEvidence] = []
        for node_key in scene_plan.node_keys:
            node = nodes_by_key.get(node_key)
            if node is None:
                if blueprint_nodes is not None:
                    errors.append(
                        f"{scene_plan.key} identity scaffold 缺少 Blueprint "
                        f"node：{node_key}"
                    )
                continue
            participants: list[
                ScreenplaySceneActionParticipantEvidence
            ] = []
            for evidence in node.participant_evidence:
                if evidence.usage == "mentioned":
                    # Content ownership is preserved in the Blueprint delivery
                    # contract, but it is not an executable scene participant.
                    continue
                identity_key = aliases.get(evidence.identity_key, "")
                if not identity_key:
                    errors.append(
                        f"{scene_plan.key} action evidence identity 未冻结："
                        f"{evidence.identity_key}"
                    )
                    continue
                evidence_source_ids = list(
                    evidence.source_segment_ids
                    or node.source_segment_ids
                )
                escaped_source_ids = (
                    set(evidence_source_ids) - set(owned_source_ids)
                )
                if escaped_source_ids:
                    errors.append(
                        f"{scene_plan.key} action evidence 引用非本场来源："
                        f"{sorted(escaped_source_ids)}"
                    )
                    continue
                participants.append(
                    ScreenplaySceneActionParticipantEvidence(
                        identity_key=identity_key,
                        source_segment_ids=evidence_source_ids,
                        source_unit_keys=list(evidence.source_unit_keys),
                        usage=evidence.usage,
                        perception_channels=(
                            ["audible"]
                            if evidence.usage == "voice"
                            else []
                        ),
                    )
                )
            decision_actor_key = None
            if node.decision is not None:
                decision_actor_key = aliases.get(
                    node.decision.actor_key,
                    "",
                )
                if not decision_actor_key:
                    errors.append(
                        f"{scene_plan.key} decision actor 未冻结："
                        f"{node.decision.actor_key}"
                    )
            state_subject_assignments: list[
                ScreenplaySceneStateSubjectAssignment
            ] = []
            for assignment in node.state_subject_assignments:
                unresolved_assignment_identities = [
                    identity_key
                    for identity_key in assignment.identity_keys
                    if identity_key not in aliases
                ]
                if unresolved_assignment_identities:
                    errors.append(
                        f"{scene_plan.key} joint state subject identity 未冻结："
                        + ",".join(unresolved_assignment_identities)
                    )
                    continue
                state_subject_assignments.append(
                    ScreenplaySceneStateSubjectAssignment(
                        source_unit_key=assignment.source_unit_key,
                        mode=assignment.mode,
                        identity_keys=[
                            aliases[identity_key]
                            for identity_key in assignment.identity_keys
                        ],
                    )
                )
            action_evidence.append(ScreenplaySceneActionEvidence(
                node_key=node.key,
                source_segment_ids=list(node.source_segment_ids),
                participants=participants,
                state_subject_assignments=state_subject_assignments,
                decision_actor_key=decision_actor_key or None,
                environment_source_unit_keys=list(
                    node.environment_source_unit_keys
                ),
            ))

        contract = ScreenplaySceneInputContract(
            scene_plan_key=scene_plan.key,
            node_keys=list(scene_plan.node_keys),
            source_segment_ids=owned_source_ids,
            source_semantics={
                source_id: scene_plan.source_semantics[source_id]
                for source_id in owned_source_ids
            },
            source_segments=[
                ScreenplaySceneSourceSegment(
                    source_segment_id=source_id,
                    text=source_by_id[source_id],
                )
                for source_id in owned_source_ids
                if source_id in source_by_id
            ],
            participant_bindings=[
                ScreenplaySceneParticipantBinding(
                    blueprint_key=participant,
                    identity_key=aliases.get(participant, ""),
                )
                for participant in scene_plan.participant_keys
            ],
            source_scene_owners=dict(plan.source_scene_owners),
            derived_relations=[
                relation.model_copy(deep=True)
                for relation in plan.derived_relations
                if relation.target_scene_plan_key == scene_plan.key
            ],
            action_participant_delivery_contract=(
                ScreenplayActionParticipantDeliveryContract()
            ),
            action_evidence=action_evidence,
            unit_slots=[],
            source_ownership_hash=plan.source_ownership_hash,
        )
        scene_slots = [
            slot
            for slot in plan.unit_slots
            if slot.scene_key == scene_plan.key
        ]
        if not scene_slots:
            errors.append(
                f"{scene_plan.key} generation scaffold 缺少 unit slot"
            )
        for slot in scene_slots:
            invalid_slot_sources = [
                source_id
                for source_id in slot.source_segment_ids
                if (
                    source_id not in owned_source_ids
                    or plan.source_scene_owners.get(source_id)
                    != scene_plan.key
                )
            ]
            if invalid_slot_sources:
                errors.append(
                    f"{slot.unit_key} source owner 不匹配："
                    + ",".join(invalid_slot_sources)
                )
                continue
            compiled_slot, slot_errors = (
                _compile_unit_identity_scaffold(
                    slot,
                    contract=contract,
                )
            )
            errors.extend(slot_errors)
            contract.unit_slots.append(compiled_slot)
        contract.identity_scaffold_hash = (
            _contract_identity_scaffold_hash(contract)
        )
        contracts.append(contract)
    if errors:
        raise ScreenplaySceneShardError(plan.shard_id, errors)
    return contracts


def build_screenplay_scene_input_contract_set(
    *,
    plans: list[ScreenplaySceneShardPlan],
    blueprint: NarrativeBlueprint,
    source_text: str,
    identity_registry: list[dict[str, Any]],
) -> dict[str, list[ScreenplaySceneInputContract]]:
    """Build the scene-owned contract once for generation, retry, and merge."""
    expected_ownership_hash = _source_ownership_hash(blueprint)
    scene_plan_map = {scene_plan.key: scene_plan for scene_plan in blueprint.scene_plans}
    source_by_id = {
        segment.segment_id: segment.text
        for segment in index_source_segments(source_text)
    }
    contracts: dict[str, list[ScreenplaySceneInputContract]] = {}
    for plan in plans:
        if (
            plan.source_scene_owners != blueprint.source_scene_owners
            or plan.source_ownership_hash != expected_ownership_hash
        ):
            raise ScreenplaySceneShardError(
                plan.shard_id,
                ["shard plan 的 source owner 合同与 Blueprint 不一致"],
            )
        missing_scene_keys = [
            scene_key for scene_key in plan.scene_plan_keys
            if scene_key not in scene_plan_map
        ]
        if missing_scene_keys:
            raise ScreenplaySceneShardError(
                plan.shard_id,
                ["逐场输入合同缺少 Blueprint scene plan：" + ",".join(missing_scene_keys)],
            )
        contracts[plan.shard_id] = build_screenplay_scene_input_contracts(
            plan=plan,
            scene_plans=[
                scene_plan_map[scene_key] for scene_key in plan.scene_plan_keys
            ],
            source_by_id=source_by_id,
            identity_registry=identity_registry,
            blueprint_nodes=list(blueprint.nodes),
        )
    return contracts


def _validate_scene_input_contracts(
    *,
    plan: ScreenplaySceneShardPlan,
    scene_plans: dict[str, BlueprintScenePlan],
    scene_input_contracts: list[ScreenplaySceneInputContract],
    identity_keys: set[str],
) -> tuple[dict[str, ScreenplaySceneInputContract], list[str]]:
    errors: list[str] = []
    expected_plan_source_ids = [
        source_id
        for source_id, owner_scene_key in plan.source_scene_owners.items()
        if owner_scene_key in plan.scene_plan_keys
    ]
    if plan.source_segment_ids != expected_plan_source_ids:
        errors.append(
            "shard plan source_segment_ids 与唯一 owner 投影不一致"
        )
    unit_keys = [slot.unit_key for slot in plan.unit_slots]
    event_keys = [slot.event_key for slot in plan.unit_slots]
    if len(set(unit_keys)) != len(unit_keys):
        errors.append("shard plan unit_key 必须唯一")
    if len(set(event_keys)) != len(event_keys):
        errors.append("shard plan event_key 必须唯一")
    if [slot.unit_order for slot in plan.unit_slots] != list(
        range(1, len(plan.unit_slots) + 1)
    ):
        errors.append("shard plan unit_order 必须连续且按播放顺序递增")
    invalid_slot_owners = [
        slot.unit_key
        for slot in plan.unit_slots
        if (
            slot.scene_key not in plan.scene_plan_keys
            or any(
                plan.source_scene_owners.get(source_id)
                != slot.scene_key
                for source_id in slot.source_segment_ids
            )
        )
    ]
    if invalid_slot_owners:
        errors.append(
            "shard plan slot 来源归属不匹配："
            + ",".join(invalid_slot_owners)
        )
    invalid_relations = [
        relation.relation_key
        for relation in plan.derived_relations
        if (
            relation.target_scene_plan_key not in plan.scene_plan_keys
            or relation.source_scene_plan_key
            == relation.target_scene_plan_key
        )
    ]
    if invalid_relations:
        errors.append(
            "shard plan 含无效跨场派生关系："
            + ",".join(invalid_relations)
        )
    actual_scene_keys = [
        contract.scene_plan_key for contract in scene_input_contracts
    ]
    if actual_scene_keys != plan.scene_plan_keys:
        errors.append(
            "逐场参与者合同与 shard plan 不一致："
            f"expected={plan.scene_plan_keys}, actual={actual_scene_keys}"
        )
    contracts_by_scene: dict[str, ScreenplaySceneInputContract] = {}
    for contract in scene_input_contracts:
        if contract.scene_plan_key in contracts_by_scene:
            errors.append(
                "逐场参与者合同 scene_plan_key 必须唯一："
                + contract.scene_plan_key
            )
            continue
        contracts_by_scene[contract.scene_plan_key] = contract
    for scene_key in plan.scene_plan_keys:
        expected_scene = scene_plans.get(scene_key)
        contract = contracts_by_scene.get(scene_key)
        if expected_scene is None:
            errors.append(f"逐场参与者合同引用未知 scene：{scene_key}")
            continue
        if contract is None:
            errors.append(f"{scene_key} 缺少逐场参与者合同")
            continue
        if contract.node_keys != expected_scene.node_keys:
            errors.append(f"{scene_key} 逐场参与者合同 node_keys 与 Blueprint 不一致")
        expected_source_ids = [
            source_id
            for source_id, owner_scene_key
            in plan.source_scene_owners.items()
            if owner_scene_key == scene_key
        ]
        if expected_scene.source_segment_ids != expected_source_ids:
            conflicting_source_ids = [
                source_id
                for source_id in expected_scene.source_segment_ids
                if plan.source_scene_owners.get(source_id) != scene_key
            ]
            if conflicting_source_ids:
                errors.extend(
                    f"{source_id} 唯一归属 "
                    f"{plan.source_scene_owners.get(source_id) or '未定义'}，"
                    f"不得由 {scene_key} 消费"
                    for source_id in conflicting_source_ids
                )
            else:
                errors.append(
                    f"{scene_key} Blueprint source_segment_ids "
                    "与唯一 owner 投影不一致"
                )
        if contract.source_segment_ids != expected_source_ids:
            errors.append(
                f"{scene_key} 逐场参与者合同 source_segment_ids "
                "与唯一 owner 投影不一致"
            )
        expected_source_semantics = {
            source_id: expected_scene.source_semantics[source_id]
            for source_id in expected_source_ids
        }
        if contract.source_semantics != expected_source_semantics:
            errors.append(
                f"{scene_key} 逐场来源语义与 Blueprint 不一致"
            )
        contract_source_ids = [
            segment.source_segment_id
            for segment in contract.source_segments
        ]
        if contract_source_ids != expected_source_ids:
            errors.append(
                f"{scene_key} 逐场来源正文与唯一 owner 投影不一致"
            )
        if contract.source_scene_owners != plan.source_scene_owners:
            errors.append(
                f"{scene_key} 逐场 source owner 合同与 shard plan 不一致"
            )
        if contract.source_ownership_hash != plan.source_ownership_hash:
            errors.append(
                f"{scene_key} source_ownership_hash 与 shard plan 不一致"
            )
        expected_relations = [
            relation.model_dump(mode="json")
            for relation in plan.derived_relations
            if relation.target_scene_plan_key == scene_key
        ]
        actual_relations = [
            relation.model_dump(mode="json")
            for relation in contract.derived_relations
        ]
        if actual_relations != expected_relations:
            errors.append(
                f"{scene_key} 跨场派生关系与 shard plan 不一致"
            )
        expected_delivery_contract = (
            ScreenplayActionParticipantDeliveryContract()
        )
        if (
            contract.action_participant_delivery_contract
            != expected_delivery_contract
        ):
            errors.append(
                f"{scene_key} action participant delivery 合同与 "
                f"{IR_VERSION} 不一致"
            )
        expected_unit_slots = [
            slot for slot in plan.unit_slots
            if slot.scene_key == scene_key
        ]
        actual_structural_slots = [
            _structural_slot(slot)
            for slot in contract.unit_slots
        ]
        if actual_structural_slots != expected_unit_slots:
            errors.append(
                f"{scene_key} unit slot 与 shard plan 不一致"
            )
        for actual_slot in contract.unit_slots:
            planned_slot = next(
                (
                    slot for slot in expected_unit_slots
                    if slot.unit_key == actual_slot.unit_key
                ),
                None,
            )
            if planned_slot is None:
                continue
            expected_slot, slot_errors = (
                _compile_unit_identity_scaffold(
                    planned_slot,
                    contract=contract,
                )
            )
            errors.extend(slot_errors)
            if actual_slot != expected_slot:
                errors.append(
                    f"{actual_slot.unit_key} identity scaffold drift"
                )
        expected_scaffold_hash = _contract_identity_scaffold_hash(
            contract
        )
        if contract.identity_scaffold_hash != expected_scaffold_hash:
            errors.append(
                f"{scene_key} identity_scaffold_hash 与 unit scaffold "
                "不一致"
            )
        if contract.action_evidence:
            invalid_evidence_identities = [
                participant.identity_key
                for action in contract.action_evidence
                for participant in action.participants
                if participant.identity_key not in identity_keys
            ]
            if invalid_evidence_identities:
                errors.append(
                    f"{scene_key} action evidence 含未冻结 identity_key："
                    + ",".join(invalid_evidence_identities)
                )
            escaped_evidence_sources = sorted({
                source_id
                for action in contract.action_evidence
                for source_id in action.source_segment_ids
                if source_id not in expected_source_ids
            })
            if escaped_evidence_sources:
                errors.append(
                    f"{scene_key} action evidence 引用非本场来源："
                    f"{escaped_evidence_sources}"
                )
        expected_blueprint_keys = list(expected_scene.participant_keys)
        actual_blueprint_keys = [
            binding.blueprint_key for binding in contract.participant_bindings
        ]
        if actual_blueprint_keys != expected_blueprint_keys:
            errors.append(
                f"{scene_key} 逐场参与者合同 participant_bindings 与 Blueprint 不一致"
            )
        invalid_bindings = [
            binding.identity_key
            for binding in contract.participant_bindings
            if (
                not binding.identity_key
                or binding.identity_key not in identity_keys
            )
        ]
        if invalid_bindings:
            errors.append(
                f"{scene_key} 逐场参与者合同含未冻结 identity_key："
                + ",".join(invalid_bindings)
            )
    return contracts_by_scene, errors


_GENERIC_STORY_FUNCTION_LABELS = {
    "setup",
    "development",
    "complication",
    "turn",
    "climax",
    "resolution",
}


def normalize_screenplay_scene_shard_payload(
    payload: dict[str, Any],
    *,
    episode_no: int,
    plan: ScreenplaySceneShardPlan,
    scene_plans: dict[str, BlueprintScenePlan],
    blueprint: NarrativeBlueprint,
) -> dict[str, Any]:
    """Return an unchanged copy; structural drift must fail explicitly."""
    del episode_no, plan, scene_plans, blueprint
    return deepcopy(payload)


def normalize_screenplay_scene_creative_payload(
    payload: dict[str, Any],
    *,
    scene_plans: dict[str, BlueprintScenePlan],
    blueprint: NarrativeBlueprint,
) -> dict[str, Any]:
    """Return an unchanged copy; slot/schema violations are not repaired."""
    del scene_plans, blueprint
    return deepcopy(payload)


def normalize_screenplay_scene_shard(
    shard: ScreenplaySceneShardIR,
    *,
    episode_no: int,
    plan: ScreenplaySceneShardPlan,
    scene_plans: dict[str, BlueprintScenePlan],
    scene_input_contracts: list[ScreenplaySceneInputContract],
) -> ScreenplaySceneShardIR:
    """Validate a compiled shard without changing provider-authored content."""
    if shard.episode_no != episode_no:
        raise ScreenplaySceneShardError(
            plan.shard_id,
            ["episode_no 与 generation scaffold 不一致"],
        )
    identity_keys = {
        binding.identity_key
        for contract in scene_input_contracts
        for binding in contract.participant_bindings
        if binding.identity_key
    }
    errors = validate_screenplay_scene_shard(
        shard,
        plan=plan,
        scene_plans=scene_plans,
        scene_input_contracts=scene_input_contracts,
        identity_keys=identity_keys,
    )
    if errors:
        raise ScreenplaySceneShardError(plan.shard_id, errors)
    return shard


def validate_screenplay_scene_shard(
    shard: ScreenplaySceneShardIR,
    *,
    plan: ScreenplaySceneShardPlan,
    scene_plans: dict[str, BlueprintScenePlan],
    scene_input_contracts: list[ScreenplaySceneInputContract],
    identity_keys: set[str],
    front_matter_ids: set[str] | None = None,
) -> list[str]:
    del front_matter_ids
    errors: list[str] = []
    if shard.episode_no < 1:
        errors.append("episode_no 必须为正整数")
    if shard.shard_id != plan.shard_id:
        errors.append(f"shard_id 应为 {plan.shard_id}")
    if shard.scene_plan_keys != plan.scene_plan_keys:
        errors.append("scene_plan_keys 与计划不一致")
    actual_scene_keys = [scene.key for scene in shard.scenes]
    if actual_scene_keys != plan.scene_plan_keys:
        errors.append(
            "scenes 必须按计划恰好输出一次："
            f"expected={plan.scene_plan_keys}, actual={actual_scene_keys}"
        )
    if shard.unresolved_participants:
        errors.append(
            "存在未冻结参与者："
            + "、".join(item.source_label for item in shard.unresolved_participants)
        )
    expected_identity_hash = screenplay_scene_identity_scaffold_hash(
        scene_input_contracts
    )
    if shard.identity_scaffold_hash != expected_identity_hash:
        errors.append("identity_scaffold_hash 不匹配")
    expected_generation_hash = (
        screenplay_scene_generation_scaffold_hash(
            plan,
            scene_input_contracts,
        )
    )
    if shard.generation_scaffold_hash != expected_generation_hash:
        errors.append("generation_scaffold_hash 不匹配")
    contracts_by_scene, contract_errors = _validate_scene_input_contracts(
        plan=plan,
        scene_plans=scene_plans,
        scene_input_contracts=scene_input_contracts,
        identity_keys=identity_keys,
    )
    errors.extend(contract_errors)
    actual_consumed: list[str] = []
    actual_unit_keys = [
        unit.unit_key
        for scene in shard.scenes
        for unit in scene.units
    ]
    duplicate_unit_keys = [
        unit_key
        for unit_key in dict.fromkeys(actual_unit_keys)
        if actual_unit_keys.count(unit_key) > 1
    ]
    if duplicate_unit_keys:
        errors.append(
            "编译结果 unit_key 重复："
            + ",".join(duplicate_unit_keys)
        )
    expected_unit_keys = [
        slot.unit_key for slot in plan.unit_slots
    ]
    if actual_unit_keys != expected_unit_keys:
        errors.append(
            "编译结果 unit_key 顺序/归属与 shard plan 不一致："
            f"expected={expected_unit_keys}, actual={actual_unit_keys}"
        )
    compiled_slots_by_key = {
        slot.unit_key: slot
        for contract in scene_input_contracts
        for slot in contract.unit_slots
    }
    units_by_key = {
        unit.unit_key: (scene.key, unit)
        for scene in shard.scenes
        for unit in scene.units
        if unit.unit_key
    }
    for scene in shard.scenes:
        expected_scene = scene_plans.get(scene.key)
        if expected_scene is None:
            errors.append(f"未知 scene key：{scene.key}")
            continue
        if scene.scene_heading != expected_scene.scene_heading:
            errors.append(f"{scene.key} scene_heading 必须由 Blueprint 精确拥有")
        if len(scene.story_function.strip()) < SCENE_STORY_FUNCTION_MIN_CHARS:
            errors.append(
                f"{scene.key}.story_function 必须完整说明本场戏剧功能，"
                f"至少 {SCENE_STORY_FUNCTION_MIN_CHARS} 个字符"
            )
        contract = contracts_by_scene.get(scene.key)
        if contract is None:
            continue
        expected_scene_unit_keys = [
            slot.unit_key
            for slot in plan.unit_slots
            if slot.scene_key == scene.key
        ]
        if [unit.unit_key for unit in scene.units] != expected_scene_unit_keys:
            errors.append(
                f"{scene.key} unit slot 顺序与 plan 不一致"
            )
        expected_character_keys = _ordered_unique([
            identity_key
            for slot in contract.unit_slots
            for identity_key in [
                *slot.actor_keys,
                *slot.target_keys,
                *slot.onscreen_entity_keys,
                *([slot.speaker_key] if slot.speaker_key else []),
                *[
                    delivery.participant_key
                    for delivery in slot.participant_deliveries
                ],
            ]
        ])
        if scene.character_keys != expected_character_keys:
            errors.append(
                f"{scene.key}.character_keys 与 compiled slot 不一致"
            )
    for planned_slot in plan.unit_slots:
        actual_pair = units_by_key.get(planned_slot.unit_key)
        compiled_slot = compiled_slots_by_key.get(
            planned_slot.unit_key
        )
        if actual_pair is None:
            errors.append(
                f"缺失 compiled slot：{planned_slot.unit_key}"
            )
            continue
        if compiled_slot is None:
            errors.append(
                f"输入合同缺失 compiled slot：{planned_slot.unit_key}"
            )
            continue
        actual_scene_key, unit = actual_pair
        if actual_scene_key != planned_slot.scene_key:
            errors.append(
                f"{planned_slot.unit_key} scene_key 漂移："
                f"{actual_scene_key}"
            )
        structural_actual = {
            "unit_key": unit.unit_key,
            "event_key": unit.event_key,
            "kind": unit.kind,
            "narrative_layer": unit.narrative_layer,
            "event_priority": unit.event_priority,
            "render_policy": unit.render_policy,
            "source_segment_ids": list(unit.source_segment_ids),
            "source_text": unit.source_text,
            "chain_key": unit.chain_key,
        }
        structural_expected = {
            "unit_key": planned_slot.unit_key,
            "event_key": planned_slot.event_key,
            "kind": planned_slot.kind,
            "narrative_layer": planned_slot.narrative_layer,
            "event_priority": planned_slot.event_priority,
            "render_policy": planned_slot.render_policy,
            "source_segment_ids": list(
                planned_slot.source_segment_ids
            ),
            "source_text": planned_slot.source_text,
            "chain_key": "",
        }
        if structural_actual != structural_expected:
            errors.append(
                f"{planned_slot.unit_key} 结构字段漂移，禁止改写 "
                "event/scene/source/order/owner"
            )
        identity_actual = {
            "actor_keys": list(unit.actor_keys),
            "target_keys": list(unit.target_keys),
            "speaker_key": unit.speaker_key,
            "onscreen_entity_keys": list(
                unit.onscreen_entity_keys
            ),
            "participant_deliveries": [
                delivery.model_dump(mode="json")
                for delivery in unit.participant_deliveries
            ],
        }
        identity_expected = {
            "actor_keys": list(compiled_slot.actor_keys),
            "target_keys": list(compiled_slot.target_keys),
            "speaker_key": compiled_slot.speaker_key,
            "onscreen_entity_keys": list(
                compiled_slot.onscreen_entity_keys
            ),
            "participant_deliveries": [
                delivery.model_dump(mode="json")
                for delivery in compiled_slot.participant_deliveries
            ],
        }
        if identity_actual != identity_expected:
            errors.append(
                f"{planned_slot.unit_key} identity scaffold drift"
            )
        if (
            unit.kind == "dialogue"
            and unit.text.strip() != planned_slot.source_text.strip()
        ):
            errors.append(
                f"{planned_slot.unit_key} dialogue.text 必须等于 "
                "scaffold source_text"
            )
        for source_id in unit.source_segment_ids:
            planned_owner = plan.source_scene_owners.get(source_id)
            if planned_owner != planned_slot.scene_key:
                errors.append(
                    f"{planned_slot.unit_key} 来源唯一归属冲突："
                    f"{source_id} owner={planned_owner or '未定义'}"
                )
            elif source_id not in actual_consumed:
                actual_consumed.append(source_id)
    missing_sources = [
        source_id
        for source_id in plan.source_segment_ids
        if source_id not in actual_consumed
    ]
    if missing_sources:
        errors.append(
            "编译结果未覆盖 plan 来源：" + ",".join(missing_sources)
        )
    if shard.consumed_source_ids != actual_consumed:
        errors.append("consumed_source_ids 必须按首次消费顺序等于 units 的实际来源并集")
    for field, expected in (
        ("source_hash", plan.source_hash),
        ("boundary_hash", plan.boundary_hash),
        ("blueprint_hash", plan.blueprint_hash),
        ("identity_registry_hash", plan.identity_registry_hash),
        ("source_ownership_hash", plan.source_ownership_hash),
    ):
        actual = str(getattr(shard, field) or "")
        if actual != expected:
            errors.append(f"{field} 不匹配")
    return errors


def _recover_scene_shard_from_provider_calls(
    *,
    operation_id: str,
    episode_no: int,
    plan: ScreenplaySceneShardPlan,
    scene_plans: dict[str, BlueprintScenePlan],
    scene_input_contracts: list[ScreenplaySceneInputContract],
    blueprint: NarrativeBlueprint,
    identity_keys: set[str],
    front_matter_ids: set[str],
) -> tuple[ScreenplaySceneShardCreativeIR, dict[str, Any]] | None:
    """Revalidate a complete prior response before issuing another paid call."""
    rows = get_conn().execute(
        """SELECT id,response_json
             FROM provider_calls
            WHERE operation_id=? AND status='OK' AND response_json IS NOT NULL
            ORDER BY id DESC LIMIT 10""",
        (operation_id,),
    ).fetchall()
    for row in rows:
        try:
            envelope = json.loads(row["response_json"])
            raw = str(envelope["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            continue
        for payload in model_gateway._json_candidates(raw):
            try:
                normalized_payload = normalize_screenplay_scene_creative_payload(
                    payload,
                    scene_plans=scene_plans,
                    blueprint=blueprint,
                )
                draft = ScreenplaySceneShardCreativeIR.model_validate(
                    normalized_payload
                )
                shard = compile_screenplay_scene_shard_draft(
                    draft,
                    episode_no=episode_no,
                    plan=plan,
                    scene_plans=scene_plans,
                    scene_input_contracts=scene_input_contracts,
                )
            except (TypeError, ValueError, ValidationError):
                continue
            errors = validate_screenplay_scene_shard(
                shard,
                plan=plan,
                scene_plans=scene_plans,
                scene_input_contracts=scene_input_contracts,
                identity_keys=identity_keys,
                front_matter_ids=front_matter_ids,
            )
            if not errors:
                return draft, {
                    "outcome": "validated_provider_recovery",
                    "provider_call_id": int(row["id"]),
                    "local_recovery": True,
                    "validation_errors": [],
                }
    return None


def _namespace_shard_scene_keys(
    shard: ScreenplaySceneShardIR,
) -> list[IRScene]:
    return [
        IRScene.model_validate(scene.model_dump(mode="json"))
        for scene in shard.scenes
    ]


def merge_screenplay_scene_shards(
    *,
    envelope: ScreenplayEnvelopeIR,
    identities: list[IRIdentity],
    plans: list[ScreenplaySceneShardPlan],
    shards: list[ScreenplaySceneShardIR],
    scene_input_contracts: dict[
        str, list[ScreenplaySceneInputContract]
    ],
    blueprint: NarrativeBlueprint,
    source_text: str,
) -> ScreenplayGenerationIR:
    errors: list[str] = []
    expected_ownership_hash = _source_ownership_hash(blueprint)
    by_id = {shard.shard_id: shard for shard in shards}
    if len(by_id) != len(shards):
        errors.append("shard_id 必须全局唯一")
    if set(by_id) != {plan.shard_id for plan in plans}:
        errors.append("validated shards 与 shard plan 集合不一致")
    expected_blueprint_hash = blueprint_content_hash(blueprint)
    if envelope.blueprint_hash != expected_blueprint_hash:
        errors.append("Envelope blueprint_hash 不匹配")
    for plan in plans:
        if plan.blueprint_hash != expected_blueprint_hash:
            errors.append(f"{plan.shard_id} blueprint_hash 不匹配")
        if plan.source_scene_owners != blueprint.source_scene_owners:
            errors.append(f"{plan.shard_id} source owner 合同与 Blueprint 不一致")
        if plan.source_ownership_hash != expected_ownership_hash:
            errors.append(f"{plan.shard_id} source_ownership_hash 不匹配")
    expected_scenes = [plan.key for plan in blueprint.scene_plans]
    merged_scenes: list[IRScene] = []
    consumed: list[str] = []
    scene_plan_map = {plan.key: plan for plan in blueprint.scene_plans}
    identity_keys = {identity.key for identity in identities}
    segments = index_source_segments(source_text)
    for plan_index, plan in enumerate(plans):
        shard = by_id.get(plan.shard_id)
        if shard is None:
            continue
        shard_errors = validate_screenplay_scene_shard(
            shard,
            plan=plan,
            scene_plans=scene_plan_map,
            scene_input_contracts=scene_input_contracts.get(
                plan.shard_id, []
            ),
            identity_keys=identity_keys,
        )
        errors.extend(shard_errors)
        if plan_index and plan.boundary_state_in != plans[plan_index - 1].boundary_state_out:
            errors.append(f"{plan.shard_id} boundary state 与前一 shard 不闭合")
        if shard_errors:
            continue
        merged_scenes.extend(_namespace_shard_scene_keys(shard))
        consumed.extend(shard.consumed_source_ids)
    if [scene.key for scene in merged_scenes] != expected_scenes:
        errors.append("合并后 scene 顺序与 Blueprint 不一致")
    required_ids = [segment.segment_id for segment in segments]
    picture_source_ids = [
        source_id
        for source_id in required_ids
        if (
            blueprint.source_semantics.get(source_id) is not None
            and blueprint.source_semantics[source_id].projection_policy
            == "picture"
        )
    ]
    audit_only_source_ids = [
        source_id
        for source_id in required_ids
        if (
            blueprint.source_semantics.get(source_id) is not None
            and blueprint.source_semantics[source_id].projection_policy
            == "audit_only"
        )
    ]
    missing_semantics = [
        source_id
        for source_id in required_ids
        if source_id not in blueprint.source_semantics
    ]
    if missing_semantics:
        errors.append(
            "Blueprint 来源语义漏掉 SRC：" + ",".join(missing_semantics)
        )
    annotated_audit_source_ids = [
        source_id
        for annotation in blueprint.source_audit_annotations
        for source_id in annotation.source_segment_ids
    ]
    if annotated_audit_source_ids != audit_only_source_ids:
        errors.append(
            "Blueprint source_audit_annotations 未精确覆盖 audit-only SRC"
        )
    missing = [
        source_id
        for source_id in picture_source_ids
        if source_id not in consumed
    ]
    if missing:
        errors.append("合并 IR 未覆盖 picture SRC：" + ",".join(missing))
    leaked_audit_sources = [
        source_id
        for source_id in audit_only_source_ids
        if source_id in consumed
    ]
    if leaked_audit_sources:
        errors.append(
            "audit-only SRC 不得进入创作 unit："
            + ",".join(leaked_audit_sources)
        )
    source_order = {source_id: index for index, source_id in enumerate(required_ids)}
    first_owned = []
    already: set[str] = set()
    actual_source_owners: dict[str, str] = {}
    for scene in merged_scenes:
        for unit in scene.units:
            for source_id in unit.source_segment_ids:
                expected_owner = blueprint.source_scene_owners.get(source_id)
                if expected_owner != scene.key:
                    errors.append(
                        f"{source_id} 唯一归属 "
                        f"{expected_owner or '未定义'}，"
                        f"不得由 {scene.key} 消费"
                    )
                previous_owner = actual_source_owners.get(source_id)
                if previous_owner is None:
                    actual_source_owners[source_id] = scene.key
                elif previous_owner != scene.key:
                    errors.append(
                        f"{source_id} 被 {previous_owner} 与 "
                        f"{scene.key} 跨场重复消费"
                    )
                if source_id in source_order and source_id not in already:
                    already.add(source_id)
                    first_owned.append(source_order[source_id])
    if first_owned != sorted(first_owned):
        errors.append("来源首次所有权顺序不单调")
    if errors:
        raise ScreenplaySceneMergeError(errors)
    merged_ir = ScreenplayGenerationIR(
        format_version=IR_VERSION,
        episode_no=envelope.episode_no,
        metadata=envelope.metadata.to_ir(),
        identities=identities,
        coverage=[
            IRCoverageGroup(
                source_segment_ids=[source_id],
                disposition="audit_only",
                projection_policy="audit_only",
                reason="来源旁文本仅保留完整审计，不参与画面投影",
            )
            for annotation in blueprint.source_audit_annotations
            for source_id in annotation.source_segment_ids
        ],
        scenes=merged_scenes,
        experience=envelope.experience.to_ir(),
        source_scene_owners=dict(blueprint.source_scene_owners),
        source_semantics={
            source_id: semantics.model_dump(mode="json")
            for source_id, semantics in blueprint.source_semantics.items()
        },
        source_audit_annotations=list(
            blueprint.source_audit_annotations
        ),
        scene_derivations=[
            relation.model_dump(mode="json")
            for relation in blueprint.scene_derivations
        ],
        source_ownership_hash=expected_ownership_hash,
    )
    audit_authority_errors = screenplay_ir_source_audit_contract_errors(
        merged_ir.model_dump(mode="json"),
        expected_source_audit_annotations=list(
            blueprint.source_audit_annotations
        ),
    )
    if audit_authority_errors:
        raise ScreenplaySceneMergeError(audit_authority_errors)
    return merged_ir


def _latest_validated_artifact(
    *,
    episode_id: str,
    artifact_type: str,
    predicate: Callable[[dict[str, Any]], bool],
) -> dict[str, Any] | None:
    rows = get_conn().execute(
        """SELECT id,type,scope_type,scope_id,status,content_json,content_hash,
                  parent_artifact_ids_json,contract_version,prompt_version,
                  model_snapshot_json
             FROM artifacts
            WHERE scope_type='episode' AND scope_id=? AND type=?
              AND status='validated'
            ORDER BY created_at DESC LIMIT 100""",
        (episode_id, artifact_type),
    ).fetchall()
    for row in rows:
        try:
            content = json.loads(row["content_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if predicate(content):
            return {**dict(row), "content": content}
    return None


def _raw_parent_artifact(
    artifact: dict[str, Any],
) -> dict[str, Any] | None:
    parent_ids = _artifact_parent_ids(artifact)
    if parent_ids is None or len(parent_ids) != 1:
        return None
    row = get_conn().execute(
        "SELECT * FROM artifacts WHERE id=?",
        (next(iter(parent_ids)),),
    ).fetchone()
    return dict(row) if row else None


async def generate_screenplay_envelope(
    *,
    episode: dict[str, Any],
    blueprint: NarrativeBlueprint,
    identity_registry: list[dict[str, Any]],
    identity_registry_hash: str,
    blueprint_artifact_id: str | None = None,
    identity_artifact_id: str | None = None,
) -> tuple[ScreenplayEnvelopeIR, str]:
    episode_id = str(episode.get("id") or f"episode-{episode['episode_no']}")
    _assert_episode_owner(episode_id)
    blueprint_hash = blueprint_content_hash(blueprint)
    cached = _latest_validated_artifact(
        episode_id=episode_id,
        artifact_type="screenplay_envelope",
        predicate=lambda content: (
            content.get("blueprint_hash") == blueprint_hash
            and content.get("identity_registry_hash") == identity_registry_hash
            and content.get("contract_version") == SCREENPLAY_ENVELOPE_VERSION
        ),
    )
    if cached:
        compatible, _reason = screenplay_envelope_artifact_compatibility(
            cached,
            expected_blueprint_hash=blueprint_hash,
            expected_identity_registry_hash=identity_registry_hash,
            raw_artifact=_raw_parent_artifact(cached),
            expected_authority_artifact_ids={
                str(blueprint_artifact_id or ""),
                str(identity_artifact_id or ""),
            },
        )
        if compatible:
            return ScreenplayEnvelopeIR.model_validate(cached["content"]), str(cached["id"])
    node_summary = [
        {
            "key": node.key,
            "summary": node.summary,
            "time_relation": node.time_relation,
            "location": node.location_label,
            "participants": node.participants,
            "scene_role": node.scene_role,
            "dramatic_load": node.dramatic_load,
            "action_logic": node.action_logic,
            "decision": node.decision.model_dump(mode="json") if node.decision else None,
            "agency": (
                node.decision.narrative_attribution if node.decision else None
            ),
        }
        for node in blueprint.nodes
    ]
    prompt = (
        "任务：根据已验证叙事蓝图生成整集全局 Screenplay Envelope。"
        "这里只决定 metadata 与 experience，不写 scenes，不需要也不得索要完整原文。"
        "不得在 approved_adaptations 中伪造来源事实。\n集信息：\n"
        + json.dumps({
            key: episode.get(key)
            for key in (
                "episode_no", "title", "synopsis", "hook", "cliffhanger",
            )
        }, ensure_ascii=False, separators=(",", ":"))
        + "\n蓝图全局摘要：\n"
        + json.dumps(node_summary, ensure_ascii=False, separators=(",", ":"))
        + "\n冻结身份摘要：\n"
        + json.dumps(identity_registry, ensure_ascii=False, separators=(",", ":"))
        + "\n只输出 Schema 对象：\n"
        + json.dumps(ScreenplayEnvelopeIR.model_json_schema(), ensure_ascii=False)
        + f"\n固定字段：contract_version={SCREENPLAY_ENVELOPE_VERSION},"
        f" episode_no={episode['episode_no']}, blueprint_hash={blueprint_hash},"
        f" identity_registry_hash={identity_registry_hash}"
    )

    def validate_envelope(value: ScreenplayEnvelopeIR) -> list[str]:
        errors: list[str] = []
        if value.episode_no != int(episode["episode_no"]):
            errors.append("episode_no 不匹配")
        if value.blueprint_hash != blueprint_hash:
            errors.append("blueprint_hash 不匹配")
        if value.identity_registry_hash != identity_registry_hash:
            errors.append("identity_registry_hash 不匹配")
        expected_ending = str(episode.get("cliffhanger") or "").strip()
        if not expected_ending and value.metadata.ending_hook.strip():
            errors.append("本集无 cliffhanger，ending_hook 必须为空")
        return errors

    attempts: list[dict[str, Any]] = []
    envelope = await model_gateway.chat_structured(
        [{"role": "user", "content": prompt}],
        model_type=ScreenplayEnvelopeIR,
        validate=validate_envelope,
        operation_id=(
            f"screenplay.envelope:{SCREENPLAY_ENVELOPE_VERSION}:"
            f"{episode_id}:{blueprint_hash}:{identity_registry_hash}"
        ),
        max_tokens=6144,
        temperature=0.2,
        format_retry_limit=_setting_int(
            "screenplay_format_retry_limit", 1, minimum=0, maximum=3
        ),
        semantic_retry_limit=_setting_int(
            "screenplay_semantic_retry_limit", 1, minimum=0, maximum=3
        ),
        call_meta={
            "stage": "剧本全局包络",
            "stage_key": "screenplay_envelope",
            "substage": "envelope",
            "episode_id": episode_id,
            "input_chars": len(prompt),
            "source_count": 0,
        },
        on_attempt=attempts.append,
    )
    _assert_episode_owner(episode_id)
    trace = current_trace()
    raw_artifact = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_envelope_raw",
            scope_type="episode",
            scope_id=episode_id,
            status="candidate",
            trust_level="T0",
            content={
                "operation_id": (
                    f"screenplay.envelope:{blueprint_hash}:{identity_registry_hash}"
                ),
                "attempts": attempts,
            },
            parent_artifact_ids=[
                value for value in (blueprint_artifact_id, identity_artifact_id) if value
            ],
            contract_version=SCREENPLAY_ENVELOPE_VERSION,
        ),
        step_run_id=trace.step_run_id,
    )
    artifact = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_envelope",
            scope_type="episode",
            scope_id=episode_id,
            status="validated",
            trust_level="T1",
            content=envelope.model_dump(mode="json"),
            parent_artifact_ids=[raw_artifact["id"]],
            contract_version=SCREENPLAY_ENVELOPE_VERSION,
        ),
        step_run_id=trace.step_run_id,
    )
    return envelope, str(artifact["id"])


def _scene_shard_prompt(
    *,
    episode_no: int,
    plan: ScreenplaySceneShardPlan,
    blueprint_scene_plans: list[BlueprintScenePlan],
    blueprint_nodes: list[dict[str, Any]],
    scene_input_contracts: list[ScreenplaySceneInputContract],
    identity_registry: list[dict[str, Any]],
    output_schema: dict[str, Any],
) -> str:
    plan_payload = plan.model_dump(mode="json")
    plan_payload["source_scene_owners"] = {
        source_id: plan.source_scene_owners[source_id]
        for source_id in plan.source_segment_ids
    }
    contract_payloads: list[dict[str, Any]] = []
    bound_identity_keys: list[str] = []
    for contract in scene_input_contracts:
        payload = contract.model_dump(mode="json")
        payload["source_scene_owners"] = {
            source_id: contract.source_scene_owners[source_id]
            for source_id in contract.source_segment_ids
        }
        contract_payloads.append(payload)
        bound_identity_keys.extend(
            binding.identity_key for binding in contract.participant_bindings
        )
    registry_by_key = {
        str(item.get("identity_key") or ""): item
        for item in identity_registry
    }
    projected_identity_registry = [
        registry_by_key[identity_key]
        for identity_key in dict.fromkeys(bound_identity_keys)
        if identity_key in registry_by_key
    ]
    exact_slot_authority, identity_labels = (
        _scene_shard_semantic_authority_payload(
            scene_input_contracts=scene_input_contracts,
            identity_registry=identity_registry,
        )
    )
    generation_scaffold_hash = (
        screenplay_scene_generation_scaffold_hash(
            plan,
            scene_input_contracts,
        )
    )
    return (
        "任务：只填写程序预声明 generation slot 的动作、对白和表演内容。"
        "scene_key、unit_key、event_key、kind、source_segment_ids、播放顺序、"
        "来源归属以及 actor/target/speaker/onscreen/participant_deliveries "
        "均已由 Blueprint、shard plan 和 compiler 锁定，模型无权输出或修改。"
        "根对象只能包含 contract_version 与 slots；slots 必须是对象，属性名必须"
        "与 Shard plan 的 unit_key 集合完全相等。每个 slot 只能填写 text、"
        "performance、resulting_state、function、required_text、prop_text、"
        "on_screen_text。required_text、prop_text、on_screen_text 只填写需要"
        "准确出现在画面中的文字内容，每个 slot 最多使用一种；对白必须只写入"
        "dialogue slot 的 text，不得把口播放入 required_text。action_agency、"
        "source_surface 与 delivery_mode 已由来源交付合同锁定；"
        "delivery_mode=written_text 时，compiler 会把 source_text 确定性写入"
        "required_text，模型不得改写原文或把内容作者伪装成发声者。"
        "agency_kind、text_provenance、identity_keys 均由 compiler 根据 generation "
        "scaffold 关系、文字结构字段与 source IDs 生成，模型输出这些字段属于"
        "additionalProperties 越权并直接失败。文字中出现人物姓名不会创建人物关系。"
        "不得用数组位置匹配，不得增加、"
        "删除、重命名或重排结构主键。dialogue slot 的 text 已由 Schema 固定为"
        "来源原文。任何缺失 slot、多余 slot 或越权字段都会明确作为 "
        "generation_contract 失败，不会静默改写。逐 slot exact authority 是"
        "唯一创作权限：action slot 即使 source_text 为空也不授权自由改写，"
        "必须只展开自身 source_fact.text；每个 slot 只能改写自身 source_fact，"
        "不得借用相邻 unit 的事实。cross-slot 内容必须归因到最早越界 slot，"
        "不得提前或重复承载其他 slot 的来源事实。\nShard plan：\n"
        + json.dumps(
            plan_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\nBlueprint scene plans：\n"
        + json.dumps(
            [value.model_dump(mode="json") for value in blueprint_scene_plans],
            ensure_ascii=False, separators=(",", ":"),
        )
        + "\n相关 Blueprint nodes：\n"
        + json.dumps(blueprint_nodes, ensure_ascii=False, separators=(",", ":"))
        + "\n冻结 identity registry：\n"
        + json.dumps(
            projected_identity_registry,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n逐场输入合同（来源正文不得跨 scene_plan_key 使用）：\n"
        + json.dumps(
            contract_payloads,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n逐 slot exact authority：\n"
        + json.dumps(
            exact_slot_authority,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\nexact authority 身份标签：\n"
        + json.dumps(
            identity_labels,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n只输出 Schema 对象：\n"
        + json.dumps(output_schema, ensure_ascii=False)
        + f"\n程序固定上下文（禁止输出）：episode_no={episode_no}, "
        f"shard_id={plan.shard_id}, generation_scaffold_hash="
        f"{generation_scaffold_hash}"
    )


def _scene_shard_semantic_authority_payload(
    *,
    scene_input_contracts: list[ScreenplaySceneInputContract],
    identity_registry: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    source_facts_by_key = {
        fact.source_unit_key: fact
        for contract in scene_input_contracts
        for segment in contract.source_segments
        for fact in source_segment_facts(
            segment.source_segment_id,
            segment.text,
        )
    }
    authority_slots = {
        slot.unit_key: {
            "kind": slot.kind,
            "source_unit_key": slot.source_unit_key,
            "source_text": slot.source_text,
            "source_fact": (
                source_facts_by_key[slot.source_unit_key].model_dump(
                    mode="json"
                )
                if slot.source_unit_key in source_facts_by_key
                else None
            ),
            "state_subject_key": slot.state_subject_key,
            "state_subject_keys": slot.state_subject_keys,
            "environment_only": slot.environment_only,
            "actor_keys": slot.actor_keys,
            "target_keys": slot.target_keys,
            "speaker_key": slot.speaker_key,
            "onscreen_entity_keys": slot.onscreen_entity_keys,
        }
        for contract in scene_input_contracts
        for slot in contract.unit_slots
    }
    allowed_identity_keys = {
        value
        for slot in authority_slots.values()
        for field in (
            "actor_keys", "target_keys", "onscreen_entity_keys",
        )
        for value in slot[field]
    } | {
        str(slot.get("speaker_key") or "")
        for slot in authority_slots.values()
        if str(slot.get("speaker_key") or "")
    } | {
        str(slot.get("state_subject_key") or "")
        for slot in authority_slots.values()
        if str(slot.get("state_subject_key") or "")
    } | {
        str(identity_key)
        for slot in authority_slots.values()
        for identity_key in slot.get("state_subject_keys") or []
    }
    identity_labels = {
        str(item.get("identity_key") or ""): {
            "canonical_name": str(item.get("canonical_name") or ""),
            "source_labels": list(item.get("source_labels") or []),
            "authority_id": str(item.get("authority_id") or ""),
        }
        for item in identity_registry
        if str(item.get("identity_key") or "") in allowed_identity_keys
    }
    return authority_slots, identity_labels


def _scene_shard_exact_source_text(value: str) -> str:
    normalized = "".join(str(value or "").split())
    while (
        len(normalized) >= 2
        and quotation_opening(normalized[0])
        and quotation_closing(normalized[0], normalized[-1])
    ):
        normalized = normalized[1:-1]
    return normalized


def _scene_shard_creative_has_exact_source_support(
    creative: ScreenplaySceneShardCreativeUnit,
    source_fact: SourceFact,
) -> bool:
    source_text = _scene_shard_exact_source_text(source_fact.text)
    content_values = (
        creative.text,
        creative.performance,
        creative.resulting_state,
        creative.required_text,
        creative.prop_text,
        creative.on_screen_text,
    )
    non_empty_values = [
        value
        for value in content_values
        if str(value or "").strip()
    ]
    return bool(source_text and non_empty_values) and all(
        _scene_shard_exact_source_text(value) == source_text
        for value in non_empty_values
    )


def _scene_shard_source_facts_by_slot(
    scene_input_contracts: list[ScreenplaySceneInputContract],
) -> dict[str, SourceFact | None]:
    source_facts_by_key = {
        fact.source_unit_key: fact
        for contract in scene_input_contracts
        for segment in contract.source_segments
        for fact in source_segment_facts(
            segment.source_segment_id,
            segment.text,
        )
    }
    return {
        slot.unit_key: source_facts_by_key.get(slot.source_unit_key)
        for contract in scene_input_contracts
        for slot in contract.unit_slots
    }


def _scene_shard_canonicalize_cross_slot_findings(
    review: ScreenplaySceneShardSemanticReview,
    *,
    draft: ScreenplaySceneShardCreativeIR,
    scene_input_contracts: list[ScreenplaySceneInputContract],
) -> ScreenplaySceneShardSemanticReview:
    source_facts_by_slot = _scene_shard_source_facts_by_slot(
        scene_input_contracts
    )
    canonical_findings: list[ScreenplaySceneShardSemanticFinding] = []
    for finding in review.findings:
        if "cross_slot_duplication" not in finding.violation_kinds:
            canonical_findings.append(finding)
            continue
        related_unit_key = finding.related_unit_keys[0]
        target_creative = draft.slots.get(finding.unit_key)
        related_creative = draft.slots.get(related_unit_key)
        target_source_fact = source_facts_by_slot.get(finding.unit_key)
        related_source_fact = source_facts_by_slot.get(related_unit_key)
        if (
            target_creative is None
            or related_creative is None
            or target_source_fact is None
            or related_source_fact is None
        ):
            canonical_findings.append(finding)
            continue
        target_source_text = _scene_shard_exact_source_text(
            target_source_fact.text
        )
        related_source_text = _scene_shard_exact_source_text(
            related_source_fact.text
        )
        if (
            target_source_text
            and related_source_text
            and target_source_text != related_source_text
            and _scene_shard_exact_source_text(target_creative.text)
            == target_source_text
            and _scene_shard_exact_source_text(related_creative.text)
            != related_source_text
        ):
            canonical_findings.append(
                ScreenplaySceneShardSemanticFinding.model_validate({
                    **finding.model_dump(mode="json"),
                    "unit_key": related_unit_key,
                    "related_unit_keys": [finding.unit_key],
                })
            )
        else:
            canonical_findings.append(finding)
    merged_findings: dict[
        tuple[str, str],
        ScreenplaySceneShardSemanticFinding,
    ] = {}
    for finding in canonical_findings:
        key = (finding.unit_key, finding.code)
        existing = merged_findings.get(key)
        if existing is None:
            merged_findings[key] = finding
            continue
        merged_kinds = [
            kind
            for kind in SCREENPLAY_SCENE_SEMANTIC_VIOLATION_KINDS
            if (
                kind in existing.violation_kinds
                or kind in finding.violation_kinds
            )
        ]
        merged_related_keys = list(dict.fromkeys([
            *existing.related_unit_keys,
            *finding.related_unit_keys,
        ]))
        if (
            "cross_slot_duplication" in merged_kinds
            and len(merged_related_keys) != 1
        ):
            raise ValueError(
                "canonicalized cross-slot finding 必须具有唯一 counterpart"
            )
        if "cross_slot_duplication" not in merged_kinds:
            merged_related_keys = []
        merged_message = "；".join(dict.fromkeys([
            existing.message,
            finding.message,
        ]))
        merged_findings[key] = (
            ScreenplaySceneShardSemanticFinding.model_validate({
                **existing.model_dump(mode="json"),
                "related_unit_keys": merged_related_keys,
                "violation_kinds": merged_kinds,
                "message": merged_message[
                    :SCREENPLAY_SCENE_SEMANTIC_FINDING_MESSAGE_MAX_CHARS
                ],
            })
        )
    return ScreenplaySceneShardSemanticReview(
        findings=list(merged_findings.values())
    )


def _scene_shard_filter_exact_source_duplication(
    review: ScreenplaySceneShardSemanticReview,
    *,
    draft: ScreenplaySceneShardCreativeIR,
    scene_input_contracts: list[ScreenplaySceneInputContract],
) -> ScreenplaySceneShardSemanticReview:
    source_facts_by_slot = _scene_shard_source_facts_by_slot(
        scene_input_contracts
    )
    filtered: list[ScreenplaySceneShardSemanticFinding] = []
    for finding in review.findings:
        creative = draft.slots.get(finding.unit_key)
        source_fact = source_facts_by_slot.get(finding.unit_key)
        if (
            "cross_slot_duplication" not in finding.violation_kinds
            or creative is None
            or source_fact is None
            or not _scene_shard_creative_has_exact_source_support(
                creative,
                source_fact,
            )
        ):
            filtered.append(finding)
            continue
        remaining_kinds = [
            kind
            for kind in finding.violation_kinds
            if kind != "cross_slot_duplication"
        ]
        if remaining_kinds:
            filtered.append(
                ScreenplaySceneShardSemanticFinding(
                    unit_key=finding.unit_key,
                    related_unit_keys=[],
                    code=finding.code,
                    violation_kinds=remaining_kinds,
                    message=finding.message,
                )
            )
    return ScreenplaySceneShardSemanticReview(findings=filtered)


def _scene_shard_creative_reproduces_source_text(
    creative: ScreenplaySceneShardCreativeUnit,
    source_text: str,
) -> bool:
    """Whether any creative content field verbatim carries the given source."""
    normalized_source = _scene_shard_exact_source_text(source_text)
    if not normalized_source:
        return False
    content_values = (
        creative.text,
        creative.performance,
        creative.resulting_state,
        creative.required_text,
        creative.prop_text,
        creative.on_screen_text,
    )
    return any(
        _scene_shard_exact_source_text(value) == normalized_source
        for value in content_values
        if str(value or "").strip()
    )


def _scene_shard_filter_distinct_source_ownership_duplication(
    review: ScreenplaySceneShardSemanticReview,
    *,
    draft: ScreenplaySceneShardCreativeIR,
    scene_input_contracts: list[ScreenplaySceneInputContract],
) -> ScreenplaySceneShardSemanticReview:
    """Falsify cross_slot_duplication when both slots own distinct frozen source.

    Frozen source ownership is the single authority: every slot carries exactly
    one ``source_unit_key`` whose ``SourceFact`` text is the only thing it may
    reproduce. When a ``cross_slot_duplication`` finding pairs slot A with slot B
    but each owns a *different* ``source_unit_key`` whose normalized source text
    also differs, the two units are structurally distinct source units, so the
    kind cannot hold on ownership grounds. This complements
    ``_scene_shard_creative_has_exact_source_support`` (which requires the
    creative to equal its *own* source); here the decision rests on whether the
    two slots draw from different frozen source, not on whether either creative
    was expanded.

    A genuine cross-slot borrow (one slot verbatim reproducing the *other*
    slot's frozen source instead of its own) still owns a distinct source unit
    on paper, so ownership alone cannot separate it from a false positive. The
    finding is therefore only cleared when neither slot's creative verbatim
    carries the other slot's frozen source; any such borrow keeps the finding
    intact. If either slot cannot be resolved to a ``SourceFact`` the finding is
    conservatively preserved.
    """
    source_facts_by_slot = _scene_shard_source_facts_by_slot(
        scene_input_contracts
    )
    filtered: list[ScreenplaySceneShardSemanticFinding] = []
    for finding in review.findings:
        if "cross_slot_duplication" not in finding.violation_kinds:
            filtered.append(finding)
            continue
        related_unit_key = finding.related_unit_keys[0]
        target_creative = draft.slots.get(finding.unit_key)
        related_creative = draft.slots.get(related_unit_key)
        target_source_fact = source_facts_by_slot.get(finding.unit_key)
        related_source_fact = source_facts_by_slot.get(related_unit_key)
        if (
            target_creative is None
            or related_creative is None
            or target_source_fact is None
            or related_source_fact is None
        ):
            filtered.append(finding)
            continue
        target_source_text = _scene_shard_exact_source_text(
            target_source_fact.text
        )
        related_source_text = _scene_shard_exact_source_text(
            related_source_fact.text
        )
        distinct_source_ownership = (
            target_source_fact.source_unit_key
            != related_source_fact.source_unit_key
            and bool(target_source_text)
            and bool(related_source_text)
            and target_source_text != related_source_text
        )
        cross_slot_borrowing = (
            _scene_shard_creative_reproduces_source_text(
                target_creative,
                related_source_text,
            )
            or _scene_shard_creative_reproduces_source_text(
                related_creative,
                target_source_text,
            )
        )
        if not distinct_source_ownership or cross_slot_borrowing:
            filtered.append(finding)
            continue
        remaining_kinds = [
            kind
            for kind in finding.violation_kinds
            if kind != "cross_slot_duplication"
        ]
        if remaining_kinds:
            filtered.append(
                ScreenplaySceneShardSemanticFinding(
                    unit_key=finding.unit_key,
                    related_unit_keys=[],
                    code=finding.code,
                    violation_kinds=remaining_kinds,
                    message=finding.message,
                )
            )
    return ScreenplaySceneShardSemanticReview(findings=filtered)


def _scene_shard_semantic_review_prompt(
    *,
    draft: ScreenplaySceneShardCreativeIR,
    scene_input_contracts: list[ScreenplaySceneInputContract],
    identity_registry: list[dict[str, Any]],
    unit_keys: list[str] | None = None,
) -> str:
    authority_slots, identity_labels = (
        _scene_shard_semantic_authority_payload(
            scene_input_contracts=scene_input_contracts,
            identity_registry=identity_registry,
        )
    )
    projected_unit_keys = (
        list(draft.slots) if unit_keys is None else list(unit_keys)
    )
    projected_authority_slots = {
        unit_key: authority_slots[unit_key]
        for unit_key in projected_unit_keys
        if unit_key in authority_slots
    }
    projected_draft = draft.model_copy(update={
        "slots": {
            unit_key: draft.slots[unit_key]
            for unit_key in projected_unit_keys
        },
    })
    review_schema = ScreenplaySceneShardSemanticReview.model_json_schema()
    return (
        "你是剧本场次分片的独立语义审查员。必须逐 slot 穷举审查，不得抽样、"
        "提前停止或只报告部分冲突。逐 slot 对照原始 source_text 与"
        "程序冻结的 exact-unit state_subject/actor/speaker，检查 creative text、"
        "performance、resulting_state 是否把主体 A 改写成主体 B，或加入来源中"
        "不存在/相反的人物行为与反应。不能从姓名词面、visible、scene roster 猜主体；"
        "environment_only 也不能承载人物思考、发问、反应或动作。同一 (unit_key,"
        " code) 的全部适用类型必须放入同一个 violation_kinds 数组，只能从 "
        "wrong_subject、unsupported_action、source_contradiction、"
        "cross_slot_duplication、environment_personification 中选择，不得重复 "
        "kind 或为同一 pair 输出重复 finding；message 必须覆盖数组中的全部 kinds。"
        "每个 finding 都必须显式输出 related_unit_keys。若包含 "
        "cross_slot_duplication，related_unit_keys 必须恰好包含当前审查 payload "
        "内另一个且非自身的 unit_key；它是跨 slot 证据的唯一 typed 引用。"
        "不包含 cross_slot_duplication 时 related_unit_keys 必须为 []。不得引用"
        "当前 payload 外的 slot，跨 chunk 内容不能作为本 finding 的证据。"
        "finding 只能归因到 creative fields 与该 slot 自身 source_fact/冻结权威"
        "发生明确冲突的 slot，不能借用其他 slot 的 source_fact 给当前 slot 定罪。"
        "跨 slot 重复时，标记最早越界或没有自身来源承载该内容的 slot；不得标记后来"
        "正确承载其自身 source_fact 的 slot。若较早 slot 正确、后来 slot 无来源"
        "重复，则标记后来的重复 slot。每个 (unit_key, code) 最多一个 finding，"
        "但必须报告全部明确冲突。不得建议改结构、主体、时间线、source ownership "
        "或 audit。"
        '无问题时只输出合法 JSON 对象 {"findings":[]}。不得输出 Markdown、解释或'
        "任何对象外文本。\n冻结 slot 权威：\n"
        + json.dumps(
            projected_authority_slots,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n冻结身份最小映射：\n"
        + json.dumps(identity_labels, ensure_ascii=False, separators=(",", ":"))
        + "\n待审 creative fields：\n"
        + projected_draft.model_dump_json()
        + "\n完整输出 JSON Schema：\n"
        + json.dumps(
            review_schema,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def _scene_shard_semantic_review_budget(
    *,
    unit_keys: list[str],
    review_prompt: str,
) -> dict[str, int | str]:
    provider = hiagent.active_provider("text")
    model = hiagent.active_model("text", provider)
    limits = hiagent.active_model_token_limits(
        provider,
        model,
        get_setting,
    )
    messages = [
        {
            "role": "system",
            "content": SCREENPLAY_SCENE_JSON_ONLY_SYSTEM_PROMPT,
        },
        {"role": "user", "content": review_prompt},
    ]
    input_chars = len(json.dumps(
        messages,
        ensure_ascii=False,
        separators=(",", ":"),
    ))
    input_estimate = _screenplay_scene_semantic_token_estimate(input_chars)
    required = screenplay_scene_semantic_review_required_tokens(unit_keys)
    context_ceiling = max(
        0,
        int(limits["context_window_tokens"])
        - input_estimate
        - SCREENPLAY_SCENE_SEMANTIC_REVIEW_CONTEXT_RESERVE_TOKENS,
    )
    ceiling = min(
        int(limits["max_output_tokens"]),
        context_ceiling,
    )
    return {
        "provider": provider,
        "model": model,
        "unit_count": len(unit_keys),
        "input_estimate": input_estimate,
        "required": required,
        "ceiling": ceiling,
        "context_window": int(limits["context_window_tokens"]),
        "max_output": int(limits["max_output_tokens"]),
    }


def _scene_shard_semantic_review_chunks(
    *,
    draft: ScreenplaySceneShardCreativeIR,
    scene_input_contracts: list[ScreenplaySceneInputContract],
    identity_registry: list[dict[str, Any]],
    shard_id: str,
) -> list[dict[str, Any]]:
    unit_keys = list(draft.slots)

    def candidate(chunk_unit_keys: list[str]) -> dict[str, Any]:
        review_prompt = _scene_shard_semantic_review_prompt(
            draft=draft,
            scene_input_contracts=scene_input_contracts,
            identity_registry=identity_registry,
            unit_keys=chunk_unit_keys,
        )
        budget = _scene_shard_semantic_review_budget(
            unit_keys=chunk_unit_keys,
            review_prompt=review_prompt,
        )
        return {
            "unit_keys": chunk_unit_keys,
            "review_prompt": review_prompt,
            "budget": budget,
            "chunk_hash": _hash({
                "unit_keys": chunk_unit_keys,
                "review_prompt": review_prompt,
            }),
        }

    def fits(value: dict[str, Any]) -> bool:
        budget = value["budget"]
        return int(budget["required"]) <= int(budget["ceiling"])

    def raise_single_unit_budget_error(value: dict[str, Any]) -> None:
        budget = value["budget"]
        raise ScreenplaySceneShardError(
            shard_id,
            [
                "语义审查输出预算不足，provider 调用已阻断："
                f"unit_key={value['unit_keys'][0]}，"
                f"unit_count={budget['unit_count']}，"
                f"required={budget['required']}，"
                f"ceiling={budget['ceiling']}，"
                f"provider={budget['provider']}，"
                f"model={budget['model']}"
            ],
        )

    chunks: list[dict[str, Any]] = []
    start = 0
    while start < len(unit_keys):
        remaining = candidate(unit_keys[start:])
        if fits(remaining):
            chunks.append(remaining)
            break

        single = candidate(unit_keys[start:start + 1])
        if not fits(single):
            raise_single_unit_budget_error(single)

        best = single
        low = 2
        high = len(unit_keys) - start - 1
        while low <= high:
            size = (low + high) // 2
            projected = candidate(unit_keys[start:start + size])
            if fits(projected):
                best = projected
                low = size + 1
            else:
                high = size - 1
        chunks.append(best)
        start += len(best["unit_keys"])
    return chunks


def _scene_shard_reviewer_findings_payload(
    consensus_findings: list[ScreenplaySceneShardSemanticFinding],
    reviews: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    consensus_kinds = {
        (finding.unit_key, finding.code): set(finding.violation_kinds)
        for finding in consensus_findings
    }
    reviewer_findings: list[dict[str, Any]] = []
    for review_payload in reviews:
        review = ScreenplaySceneShardSemanticReview.model_validate(
            review_payload
        )
        for finding in review.findings:
            shared_kinds = consensus_kinds.get(
                (finding.unit_key, finding.code),
                set(),
            )
            if shared_kinds.intersection(finding.violation_kinds):
                reviewer_findings.append(
                    finding.model_dump(mode="json")
                )
    return reviewer_findings


def _scene_shard_semantic_repair_prompt(
    *,
    findings_payload: list[dict[str, Any]],
    reviewer_findings_payload: list[dict[str, Any]],
    frozen_slots: dict[str, dict[str, Any]],
    draft_json: str,
    creative_schema: dict[str, Any],
    identity_labels: dict[str, dict[str, Any]] | None = None,
) -> str:
    del identity_labels
    return (
        "只修复下列 consensus finding 对应 slot 的 creative fields：text、"
        "performance、resulting_state、function、required_text、prop_text、"
        "on_screen_text。必须忠于 source_text 与 exact state_subject/actor/speaker；"
        "不得输出未标记 slot，不得输出或改变任何结构、身份、timeline、"
        "source ownership 或 audit 字段。只返回当前标记 slot 的 subset creative "
        "root。\nfindings：\n"
        + json.dumps(
            findings_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\nreviewer_findings：\n"
        + json.dumps(
            reviewer_findings_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n冻结 slots：\n"
        + json.dumps(frozen_slots, ensure_ascii=False, separators=(",", ":"))
        + "\n当前 flagged creative slots：\n"
        + draft_json
        + "\nsubset creative 输出 JSON Schema：\n"
        + json.dumps(
            creative_schema,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def _scene_shard_semantic_repair_subset_schema(
    flagged_unit_keys: list[str],
) -> dict[str, Any]:
    schema = deepcopy(ScreenplaySceneShardCreativeIR.model_json_schema())
    slot_schemas = {
        unit_key: {
            "$ref": "#/$defs/ScreenplaySceneShardCreativeUnit",
        }
        for unit_key in flagged_unit_keys
    }
    schema["properties"]["slots"] = {
        "type": "object",
        "properties": slot_schemas,
        "required": flagged_unit_keys,
        "additionalProperties": False,
        "minProperties": len(flagged_unit_keys),
        "maxProperties": len(flagged_unit_keys),
    }
    return schema


def _scene_shard_semantic_repair_budget(
    *,
    draft_json: str,
    repair_prompt: str,
    unit_count: int,
) -> dict[str, int | str]:
    provider = hiagent.active_provider("text")
    model = hiagent.active_model("text", provider)
    limits = hiagent.active_model_token_limits(
        provider,
        model,
        get_setting,
    )
    messages = [
        {
            "role": "system",
            "content": SCREENPLAY_SCENE_JSON_ONLY_SYSTEM_PROMPT,
        },
        {"role": "user", "content": repair_prompt},
    ]
    input_chars = len(json.dumps(
        messages,
        ensure_ascii=False,
        separators=(",", ":"),
    ))
    input_estimate = _screenplay_scene_semantic_token_estimate(input_chars)
    required = screenplay_scene_semantic_repair_required_tokens(
        draft_json=draft_json,
        repair_prompt=repair_prompt,
    )
    context_ceiling = max(
        0,
        int(limits["context_window_tokens"])
        - input_estimate
        - SCREENPLAY_SCENE_SEMANTIC_REVIEW_CONTEXT_RESERVE_TOKENS,
    )
    ceiling = min(
        int(limits["max_output_tokens"]),
        context_ceiling,
    )
    return {
        "provider": provider,
        "model": model,
        "unit_count": unit_count,
        "input": input_estimate,
        "required": required,
        "ceiling": ceiling,
        "context_window": int(limits["context_window_tokens"]),
        "max_output": int(limits["max_output_tokens"]),
    }


async def _semantic_review_scene_shard_draft(
    *,
    draft: ScreenplaySceneShardCreativeIR,
    scene_input_contracts: list[ScreenplaySceneInputContract],
    identity_registry: list[dict[str, Any]],
    operation_id: str,
    shard_id: str,
    validate_draft: Callable[[ScreenplaySceneShardCreativeIR], list[str]],
    batch_abort: asyncio.Event | None = None,
) -> tuple[ScreenplaySceneShardCreativeIR, list[dict[str, Any]]]:
    """Consensus-review creative prose without allowing structural rewrites."""
    review_schema = ScreenplaySceneShardSemanticReview.model_json_schema()

    async def review(
        candidate: ScreenplaySceneShardCreativeIR,
        reviewer_no: int,
        phase: str,
        unit_keys: list[str],
        review_prompt: str,
        budget: dict[str, int | str],
        chunk_index: int,
        chunk_count: int,
        chunk_hash: str,
    ) -> ScreenplaySceneShardSemanticReview:
        known_unit_keys = set(unit_keys)

        def validate_review(
            value: ScreenplaySceneShardSemanticReview,
        ) -> list[str]:
            _scene_shard_canonicalize_review_unit_references(
                value,
                known_unit_keys,
            )
            try:
                ScreenplaySceneShardSemanticReview.model_validate(
                    value.model_dump(mode="json"),
                )
            except ValidationError as exc:
                return [
                    "语义审查规范化后的 finding 合同无效："
                    + str(exc),
                ]
            unknown_finding_keys = {
                finding.unit_key
                for finding in value.findings
                if finding.unit_key not in known_unit_keys
            }
            unknown_related_keys = {
                related_unit_key
                for finding in value.findings
                for related_unit_key in finding.related_unit_keys
                if related_unit_key not in known_unit_keys
            }
            errors: list[str] = []
            if unknown_finding_keys:
                errors.append(
                    "语义审查引用未知 unit_key："
                    + ",".join(sorted(unknown_finding_keys))
                )
            if unknown_related_keys:
                errors.append(
                    "语义审查引用未知 related_unit_key："
                    + ",".join(sorted(unknown_related_keys))
                )
            return errors

        if batch_abort is not None and batch_abort.is_set():
            raise asyncio.CancelledError
        try:
            result = await model_gateway.chat_structured(
                [
                    {
                        "role": "system",
                        "content": SCREENPLAY_SCENE_JSON_ONLY_SYSTEM_PROMPT,
                    },
                    {
                        "role": "user",
                        "content": review_prompt,
                    },
                ],
                model_type=ScreenplaySceneShardSemanticReview,
                validate=validate_review,
                operation_id=(
                    f"{operation_id}:semantic:"
                    f"{SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION}:{phase}:"
                    f"reviewer-{reviewer_no}:"
                    f"chunk-{chunk_index}-of-{chunk_count}:{chunk_hash}"
                ),
                max_tokens=int(budget["required"]),
                temperature=0.0,
                format_retry_limit=(
                    SCREENPLAY_SCENE_SEMANTIC_POST_REPAIR_FORMAT_RETRY_LIMIT
                    if phase == "post_repair"
                    else SCREENPLAY_SCENE_SEMANTIC_INITIAL_FORMAT_RETRY_LIMIT
                ),
                semantic_retry_limit=0,
                call_meta={
                    "stage": "剧本场次语义审查",
                    "stage_key": "screenplay_scene_shard_semantic_review",
                    "substage": phase,
                    "shard_id": shard_id,
                    "reviewer_no": reviewer_no,
                    "chunk_index": chunk_index,
                    "chunk_count": chunk_count,
                    "contract_version": (
                        SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION
                    ),
                    "reuse_successful_operation": True,
                    "provider": budget["provider"],
                    "model": budget["model"],
                    "unit_count": budget["unit_count"],
                    "input_estimate": budget["input_estimate"],
                    "required": budget["required"],
                    "ceiling": budget["ceiling"],
                },
                output_schema=review_schema,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            if batch_abort is not None:
                batch_abort.set()
            raise
        if batch_abort is not None and batch_abort.is_set():
            raise asyncio.CancelledError
        validation_errors = validate_review(result)
        if validation_errors:
            raise ScreenplaySceneShardError(
                shard_id,
                validation_errors,
            )
        return result

    async def consensus(
        candidate: ScreenplaySceneShardCreativeIR,
        phase: str,
        allowed_finding_unit_keys: set[str] | None = None,
    ) -> tuple[list[ScreenplaySceneShardSemanticFinding], list[dict[str, Any]]]:
        chunks = _scene_shard_semantic_review_chunks(
            draft=candidate,
            scene_input_contracts=scene_input_contracts,
            identity_registry=identity_registry,
            shard_id=shard_id,
        )

        reviewer_findings: list[list[ScreenplaySceneShardSemanticFinding]] = [
            [],
            [],
        ]
        for chunk_index, chunk in enumerate(chunks, start=1):
            chunk_reviews = await _gather_fail_fast(
                *(
                    review(
                        candidate,
                        reviewer_no,
                        phase,
                        chunk["unit_keys"],
                        chunk["review_prompt"],
                        chunk["budget"],
                        chunk_index,
                        len(chunks),
                        chunk["chunk_hash"],
                    )
                    for reviewer_no in (1, 2)
                ),
            )
            for reviewer_index, chunk_review in enumerate(chunk_reviews):
                reviewer_findings[reviewer_index].extend(
                    chunk_review.findings
                )
        reviews = [
            ScreenplaySceneShardSemanticReview(findings=findings)
            for findings in reviewer_findings
        ]
        known_unit_keys = set(candidate.slots)
        unknown_finding_keys = {
            finding.unit_key
            for item in reviews
            for finding in item.findings
            if finding.unit_key not in known_unit_keys
        }
        unknown_related_keys = {
            related_unit_key
            for item in reviews
            for finding in item.findings
            for related_unit_key in finding.related_unit_keys
            if related_unit_key not in known_unit_keys
        }
        if unknown_finding_keys or unknown_related_keys:
            errors: list[str] = []
            if unknown_finding_keys:
                errors.append(
                    "语义审查引用未知 unit_key："
                    + ",".join(sorted(unknown_finding_keys))
                )
            if unknown_related_keys:
                errors.append(
                    "语义审查引用未知 related_unit_key："
                    + ",".join(sorted(unknown_related_keys))
                )
            raise ScreenplaySceneShardError(
                shard_id,
                errors,
            )
        reviews = [
            _scene_shard_filter_distinct_source_ownership_duplication(
                _scene_shard_filter_exact_source_duplication(
                    _scene_shard_canonicalize_cross_slot_findings(
                        item,
                        draft=candidate,
                        scene_input_contracts=scene_input_contracts,
                    ),
                    draft=candidate,
                    scene_input_contracts=scene_input_contracts,
                ),
                draft=candidate,
                scene_input_contracts=scene_input_contracts,
            )
            for item in reviews
        ]
        if allowed_finding_unit_keys is not None:
            reviews = [
                ScreenplaySceneShardSemanticReview(findings=[
                    finding
                    for finding in item.findings
                    if (
                        finding.unit_key in allowed_finding_unit_keys
                        or not set(finding.related_unit_keys).isdisjoint(
                            allowed_finding_unit_keys
                        )
                    )
                ])
                for item in reviews
            ]
        return (
            screenplay_scene_semantic_consensus(reviews[0], reviews[1]),
            [item.model_dump(mode="json") for item in reviews],
        )

    initial_hash = _hash(draft.model_dump(mode="json"))
    findings, initial_reviews = await consensus(draft, "initial")
    audit = [{
        "phase": "initial",
        "creative_hash": initial_hash,
        "reviews": initial_reviews,
        "consensus": [item.model_dump(mode="json") for item in findings],
    }]
    if not findings:
        return draft, audit

    frozen_slots, _identity_labels = _scene_shard_semantic_authority_payload(
        scene_input_contracts=scene_input_contracts,
        identity_registry=identity_registry,
    )
    current_draft = draft
    current_reviews = initial_reviews
    for repair_round in range(
        1,
        SCREENPLAY_SCENE_SEMANTIC_MAX_REPAIR_ROUNDS + 1,
    ):
        flagged_unit_keys = {item.unit_key for item in findings}
        ordered_flagged_unit_keys = [
            unit_key
            for unit_key in current_draft.slots
            if unit_key in flagged_unit_keys
        ]
        subset_schema = _scene_shard_semantic_repair_subset_schema(
            ordered_flagged_unit_keys,
        )
        subset_schema_hash = _hash(subset_schema)

        def validate_repair(
            candidate: ScreenplaySceneShardCreativeIR,
        ) -> list[str]:
            candidate_unit_keys = set(candidate.slots)
            if candidate_unit_keys != flagged_unit_keys:
                return [
                    "语义 repair subset slots 必须完全等于 consensus 标记集合"
                ]
            merged = current_draft.model_copy(deep=True)
            for unit_key in ordered_flagged_unit_keys:
                merged.slots[unit_key] = candidate.slots[
                    unit_key
                ].model_copy(deep=True)
            return list(validate_draft(merged))

        findings_payload = [
            item.model_dump(mode="json")
            for item in findings
        ]
        reviewer_findings_payload = (
            _scene_shard_reviewer_findings_payload(
                findings,
                current_reviews,
            )
        )
        current_draft_payload = current_draft.model_dump(mode="json")
        current_draft_hash = _hash(current_draft_payload)
        subset_draft = ScreenplaySceneShardCreativeIR(
            slots={
                unit_key: current_draft.slots[unit_key].model_copy(deep=True)
                for unit_key in ordered_flagged_unit_keys
            },
        )
        subset_draft_payload = subset_draft.model_dump(mode="json")
        subset_draft_json = subset_draft.model_dump_json()
        flagged_frozen_slots = {
            unit_key: frozen_slots[unit_key]
            for unit_key in ordered_flagged_unit_keys
        }
        repair_context = json.dumps(
            {
                "consensus_findings": findings_payload,
                "reviewer_findings": reviewer_findings_payload,
                "frozen_slots": flagged_frozen_slots,
                "current_flagged_creative": subset_draft_payload,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        repair_prompt = _scene_shard_semantic_repair_prompt(
            findings_payload=findings_payload,
            reviewer_findings_payload=reviewer_findings_payload,
            frozen_slots=flagged_frozen_slots,
            draft_json=subset_draft_json,
            creative_schema=subset_schema,
        )
        repair_messages = [
            {
                "role": "system",
                "content": SCREENPLAY_SCENE_JSON_ONLY_SYSTEM_PROMPT,
            },
            {"role": "user", "content": repair_prompt},
        ]
        repair_budget = _scene_shard_semantic_repair_budget(
            draft_json=subset_draft_json,
            repair_prompt=repair_prompt,
            unit_count=len(ordered_flagged_unit_keys),
        )
        if int(repair_budget["required"]) > int(repair_budget["ceiling"]):
            raise ScreenplaySceneShardError(
                shard_id,
                [
                    "语义 repair 输出预算不足，provider 调用已阻断："
                    f"unit_count={repair_budget['unit_count']}，"
                    f"input={repair_budget['input']}，"
                    f"required={repair_budget['required']}，"
                    f"ceiling={repair_budget['ceiling']}，"
                    f"provider={repair_budget['provider']}，"
                    f"model={repair_budget['model']}"
                ],
            )
        if batch_abort is not None and batch_abort.is_set():
            raise asyncio.CancelledError
        try:
            repaired = await model_gateway.chat_structured(
                repair_messages,
                model_type=ScreenplaySceneShardCreativeIR,
                validate=validate_repair,
                operation_id=(
                    f"{operation_id}:semantic:"
                    f"{SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION}:repair:"
                    f"round-{repair_round}:{current_draft_hash}:"
                    f"{subset_schema_hash}"
                ),
                max_tokens=int(repair_budget["required"]),
                temperature=0.2,
                format_retry_limit=1,
                semantic_retry_limit=1,
                call_meta={
                    "stage": "剧本场次语义修复",
                    "stage_key": "screenplay_scene_shard_semantic_repair",
                    "substage": "consensus_repair",
                    "repair_round": repair_round,
                    "shard_id": shard_id,
                    "contract_version": (
                        SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION
                    ),
                    "schema_hash": subset_schema_hash,
                    "provider": repair_budget["provider"],
                    "model": repair_budget["model"],
                    "required": repair_budget["required"],
                    "ceiling": repair_budget["ceiling"],
                    "input": repair_budget["input"],
                    "unit_count": repair_budget["unit_count"],
                },
                repair_context=repair_context,
                output_schema=subset_schema,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            if batch_abort is not None:
                batch_abort.set()
            raise
        if batch_abort is not None and batch_abort.is_set():
            raise asyncio.CancelledError
        repair_errors = validate_repair(repaired)
        if repair_errors:
            raise ScreenplaySceneShardError(
                shard_id,
                repair_errors,
            )
        merged_repair = current_draft.model_copy(deep=True)
        for unit_key in ordered_flagged_unit_keys:
            merged_repair.slots[unit_key] = repaired.slots[
                unit_key
            ].model_copy(deep=True)
        remaining, final_reviews = await consensus(
            merged_repair,
            "post_repair",
            allowed_finding_unit_keys=flagged_unit_keys,
        )
        audit.append({
            "phase": "post_repair",
            "creative_hash": _hash(merged_repair.model_dump(mode="json")),
            "reviews": final_reviews,
            "consensus": [
                item.model_dump(mode="json")
                for item in remaining
            ],
        })
        if not remaining:
            return merged_repair, audit
        current_draft = merged_repair
        findings = remaining
        current_reviews = final_reviews

    raise ScreenplaySceneShardError(
        shard_id,
        [
            f"{item.unit_key} creative semantic gate 未收口：{item.message}"
            for item in findings
        ],
    )


async def generate_screenplay_scene_shards(
    *,
    episode: dict[str, Any],
    source_text: str,
    blueprint: NarrativeBlueprint,
    identity_registry: list[dict[str, Any]],
    identities: list[IRIdentity],
    plans: list[ScreenplaySceneShardPlan],
    scene_input_contracts: dict[
        str, list[ScreenplaySceneInputContract]
    ],
    blueprint_artifact_id: str | None = None,
    identity_artifact_id: str | None = None,
    progress: Callable[[list[dict[str, Any]]], Any] | None = None,
) -> tuple[list[ScreenplaySceneShardIR], list[str], list[dict[str, Any]]]:
    """Generate/reuse independent shards with a per-episode concurrency cap."""
    episode_id = str(episode.get("id") or f"episode-{episode['episode_no']}")
    scene_plan_map = {plan.key: plan for plan in blueprint.scene_plans}
    source_segments = index_source_segments(source_text)
    front_matter_ids = structural_front_matter_ids(source_segments)
    identity_keys = {identity.key for identity in identities}
    parallelism = _setting_int(
        "screenplay_scene_shard_parallelism", 2, minimum=1, maximum=2
    )
    semaphore = asyncio.Semaphore(parallelism)
    batch_abort = asyncio.Event()
    checkpoint_rows: dict[str, dict[str, Any]] = {
        plan.shard_id: {
            "shard_id": plan.shard_id,
            "status": "pending",
            "attempt": 0,
            "source_hash": plan.source_hash,
            "boundary_hash": plan.boundary_hash,
            "source_ownership_hash": plan.source_ownership_hash,
            "generation_scaffold_hash": (
                screenplay_scene_generation_scaffold_hash(
                    plan,
                    scene_input_contracts.get(plan.shard_id, []),
                )
            ),
        }
        for plan in plans
    }

    def emit_progress() -> None:
        if progress is not None:
            progress([checkpoint_rows[plan.shard_id] for plan in plans])

    async def generate_one(
        plan: ScreenplaySceneShardPlan,
    ) -> tuple[ScreenplaySceneShardIR, str]:
        if batch_abort.is_set():
            raise asyncio.CancelledError
        _assert_episode_owner(episode_id)
        plan_scene_input_contracts = scene_input_contracts.get(
            plan.shard_id, []
        )
        _, preflight_errors = _validate_scene_input_contracts(
            plan=plan,
            scene_plans=scene_plan_map,
            scene_input_contracts=plan_scene_input_contracts,
            identity_keys=identity_keys,
        )
        if preflight_errors:
            raise ScreenplaySceneShardError(
                plan.shard_id,
                preflight_errors,
            )
        identity_scaffold_hash = (
            screenplay_scene_identity_scaffold_hash(
                plan_scene_input_contracts
            )
        )
        generation_scaffold_hash = (
            screenplay_scene_generation_scaffold_hash(
                plan,
                plan_scene_input_contracts,
            )
        )
        cached = _latest_validated_artifact(
            episode_id=episode_id,
            artifact_type="screenplay_scene_shard",
            predicate=lambda content: all(
                content.get(field) == getattr(plan, field)
                for field in (
                    "shard_id", "source_hash", "boundary_hash",
                    "blueprint_hash", "identity_registry_hash",
                    "source_ownership_hash",
                )
            )
            and content.get("identity_scaffold_hash")
            == identity_scaffold_hash
            and content.get("generation_scaffold_hash")
            == generation_scaffold_hash
            and content.get("contract_version")
            == SCREENPLAY_SCENE_SHARD_VERSION,
        )
        if cached:
            compatible, _reason = screenplay_scene_shard_artifact_compatibility(
                cached,
                expected_blueprint_hash=plan.blueprint_hash,
                expected_identity_registry_hash=plan.identity_registry_hash,
                expected_generation_scaffold_hash=generation_scaffold_hash,
                raw_artifact=_raw_parent_artifact(cached),
                expected_authority_artifact_ids={
                    str(blueprint_artifact_id or ""),
                    str(identity_artifact_id or ""),
                },
            )
            try:
                shard = (
                    ScreenplaySceneShardIR.model_validate(cached["content"])
                    if compatible
                    else None
                )
            except ValidationError:
                shard = None
            if shard is not None:
                errors = validate_screenplay_scene_shard(
                    shard,
                    plan=plan,
                    scene_plans=scene_plan_map,
                    scene_input_contracts=plan_scene_input_contracts,
                    identity_keys=identity_keys,
                    front_matter_ids=front_matter_ids,
                )
                if not errors:
                    checkpoint_rows[plan.shard_id].update({
                        "status": "validated",
                        "attempt": 0,
                        "normalized_artifact_id": str(cached["id"]),
                        "identity_scaffold_hash": identity_scaffold_hash,
                        "generation_scaffold_hash": (
                            generation_scaffold_hash
                        ),
                        "reused": True,
                    })
                    emit_progress()
                    return shard, str(cached["id"])
        selected_scene_plans = [scene_plan_map[key] for key in plan.scene_plan_keys]
        selected_node_keys = {
            node_key for scene_plan in selected_scene_plans
            for node_key in scene_plan.node_keys
        }
        repair_contracts: list[dict[str, Any]] = []
        for contract in plan_scene_input_contracts:
            payload = contract.model_dump(mode="json")
            payload["source_scene_owners"] = {
                source_id: contract.source_scene_owners[source_id]
                for source_id in contract.source_segment_ids
            }
            repair_contracts.append(payload)
        exact_slot_authority, identity_labels = (
            _scene_shard_semantic_authority_payload(
                scene_input_contracts=plan_scene_input_contracts,
                identity_registry=identity_registry,
            )
        )
        creative_repair_context = json.dumps({
            "root_contract": {
                "contract_version": SCREENPLAY_SCENE_CREATIVE_VERSION,
                "required_root_fields": [
                    "contract_version", "slots",
                ],
                "structural_fields_owned_by": (
                    "deterministic_generation_scaffold"
                ),
                "generation_scaffold_hash": generation_scaffold_hash,
            },
            "scene_input_contracts": repair_contracts,
            "exact_slot_authority": exact_slot_authority,
            "identity_authority": identity_labels,
            "action_participant_delivery_contract": (
                ScreenplayActionParticipantDeliveryContract()
                .model_dump(mode="json")
            ),
            "final_gate_contract": [
                "slot keys must exactly equal the declared unit_key set",
                "missing or extra slots are generation_contract failures",
                "structural and identity fields are forbidden in slot content",
                "dialogue text must equal scaffold source_text",
                "action_agency, agency_kind, text_provenance and identity_keys are compiler-owned additional properties",
                "required_text, prop_text and on_screen_text are content fields and never create identity relations",
                "compiler derives agency and provenance from scaffold relations and source IDs",
                "empty action source_text does not authorize free rewriting; use only that slot's source_fact.text",
                "each slot may rewrite only its own source_fact and may not borrow adjacent units",
                "cross-slot content is attributed to the earliest overreaching slot",
            ],
        }, ensure_ascii=False, separators=(",", ":"))
        output_schema = build_screenplay_scene_shard_repair_schema(
            plan=plan,
            scene_input_contracts=plan_scene_input_contracts,
        )
        prompt = _scene_shard_prompt(
            episode_no=int(episode["episode_no"]),
            plan=plan,
            blueprint_scene_plans=selected_scene_plans,
            blueprint_nodes=[
                node.model_dump(mode="json")
                for node in blueprint.nodes if node.key in selected_node_keys
            ],
            scene_input_contracts=plan_scene_input_contracts,
            identity_registry=identity_registry,
            output_schema=output_schema,
        )
        operation_id = (
            f"screenplay.scene-shard:{SCREENPLAY_SCENE_SHARD_VERSION}:"
            f"{SCREENPLAY_SCENE_INPUT_VERSION}:"
            f"{episode_id}:{plan.shard_id}:{plan.source_hash}:"
            f"{plan.boundary_hash}:{plan.blueprint_hash}:"
            f"{plan.identity_registry_hash}:{plan.source_ownership_hash}:"
            f"{generation_scaffold_hash}"
        )

        def validate_draft(
            value: ScreenplaySceneShardCreativeIR,
        ) -> list[str]:
            try:
                compiled = compile_screenplay_scene_shard_draft(
                    value,
                    episode_no=int(episode["episode_no"]),
                    plan=plan,
                    scene_plans=scene_plan_map,
                    scene_input_contracts=plan_scene_input_contracts,
                )
            except ScreenplaySceneShardError as exc:
                return list(exc.errors)
            return validate_screenplay_scene_shard(
                compiled,
                plan=plan,
                scene_plans=scene_plan_map,
                scene_input_contracts=plan_scene_input_contracts,
                identity_keys=identity_keys,
                front_matter_ids=front_matter_ids,
            )

        def repair_schema(
            value: ScreenplaySceneShardCreativeIR,
        ) -> dict[str, Any]:
            del value
            return output_schema

        attempts: list[dict[str, Any]] = []
        recovered = _recover_scene_shard_from_provider_calls(
            operation_id=operation_id,
            episode_no=int(episode["episode_no"]),
            plan=plan,
            scene_plans=scene_plan_map,
            scene_input_contracts=plan_scene_input_contracts,
            blueprint=blueprint,
            identity_keys=identity_keys,
            front_matter_ids=front_matter_ids,
        )
        if recovered is not None:
            draft, recovery_attempt = recovered
            attempts.append(recovery_attempt)
        else:
            async with semaphore:
                if batch_abort.is_set():
                    raise asyncio.CancelledError
                checkpoint_rows[plan.shard_id].update({
                    "status": "running", "attempt": 1,
                })
                emit_progress()
                budget_meta = _screenplay_scene_shard_budget_meta(plan)
                try:
                    draft = await model_gateway.chat_structured(
                        [{"role": "user", "content": prompt}],
                        model_type=ScreenplaySceneShardCreativeIR,
                        validate=validate_draft,
                        operation_id=operation_id,
                        max_tokens=screenplay_scene_shard_token_budget(plan),
                        temperature=0.4,
                        format_retry_limit=_setting_int(
                            "screenplay_format_retry_limit",
                            1,
                            minimum=0,
                            maximum=3,
                        ),
                        semantic_retry_limit=_setting_int(
                            "screenplay_semantic_retry_limit",
                            1,
                            minimum=0,
                            maximum=3,
                        ),
                        call_meta={
                            "stage": "剧本场次分片",
                            "stage_key": "screenplay_scene_shards",
                            "substage": "scene_writing",
                            "shard_id": plan.shard_id,
                            "shard_count": len(plans),
                            "episode_id": episode_id,
                            "source_count": len(plan.source_segment_ids),
                            "scene_count": len(plan.scene_plan_keys),
                            "identity_scaffold_hash": identity_scaffold_hash,
                            "generation_scaffold_hash": (
                                generation_scaffold_hash
                            ),
                            "input_chars": len(prompt),
                            **budget_meta,
                        },
                        repair_context=creative_repair_context,
                        format_repair_context=creative_repair_context,
                        output_schema=output_schema,
                        repair_schema=repair_schema,
                        on_attempt=attempts.append,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Fence queued shards before releasing the semaphore.  The
                    # gather coordinator may not run until after a waiter wakes.
                    batch_abort.set()
                    raise
        if batch_abort.is_set():
            raise asyncio.CancelledError
        initial_creative_hash = _hash(draft.model_dump(mode="json"))
        draft, semantic_reviews = await _semantic_review_scene_shard_draft(
            draft=draft,
            scene_input_contracts=plan_scene_input_contracts,
            identity_registry=identity_registry,
            operation_id=operation_id,
            shard_id=plan.shard_id,
            validate_draft=validate_draft,
            batch_abort=batch_abort,
        )
        if batch_abort.is_set():
            raise asyncio.CancelledError
        reviewed_creative_hash = _hash(draft.model_dump(mode="json"))
        if (
            not semantic_reviews
            or semantic_reviews[-1].get("consensus") != []
            or semantic_reviews[0].get("creative_hash")
            != initial_creative_hash
            or semantic_reviews[-1].get("creative_hash")
            != reviewed_creative_hash
        ):
            raise ScreenplaySceneShardError(
                plan.shard_id,
                ["语义审查证据未绑定精确 creative candidate"],
            )
        shard = compile_screenplay_scene_shard_draft(
            draft,
            episode_no=int(episode["episode_no"]),
            plan=plan,
            scene_plans=scene_plan_map,
            scene_input_contracts=plan_scene_input_contracts,
        )
        shard_payload = shard.model_dump(mode="json")
        reviewed_shard_content_hash = evidence_repository.content_hash(
            shard_payload
        )
        _assert_episode_owner(episode_id)
        trace = current_trace()
        parents = [
            value for value in (blueprint_artifact_id, identity_artifact_id) if value
        ]
        raw_artifact = evidence_repository.create_artifact(
            EvidenceArtifact(
                type="screenplay_scene_shard_raw",
                scope_type="episode",
                scope_id=episode_id,
                status="candidate",
                trust_level="T0",
                content={
                    "shard_id": plan.shard_id,
                    "operation_id": operation_id,
                    "identity_scaffold_hash": identity_scaffold_hash,
                    "generation_scaffold_hash": (
                        generation_scaffold_hash
                    ),
                    "creative_contract_version": (
                        SCREENPLAY_SCENE_CREATIVE_VERSION
                    ),
                    "attempts": attempts,
                    "semantic_review_evidence": {
                        "contract_version": (
                            SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION
                        ),
                        "initial_creative_hash": initial_creative_hash,
                        "reviewed_creative_hash": reviewed_creative_hash,
                        "reviewed_shard_content_hash": (
                            reviewed_shard_content_hash
                        ),
                        "phases": semantic_reviews,
                    },
                },
                parent_artifact_ids=parents,
                contract_version=SCREENPLAY_SCENE_SHARD_VERSION,
            ),
            step_run_id=trace.step_run_id,
        )
        artifact = evidence_repository.create_artifact(
            EvidenceArtifact(
                type="screenplay_scene_shard",
                scope_type="episode",
                scope_id=episode_id,
                status="validated",
                trust_level="T1",
                content=shard_payload,
                parent_artifact_ids=[raw_artifact["id"]],
                contract_version=SCREENPLAY_SCENE_SHARD_VERSION,
                model_snapshot={
                    "shard_id": plan.shard_id,
                    "scene_count": len(shard.scenes),
                    "unit_count": sum(len(scene.units) for scene in shard.scenes),
                    "identity_scaffold_hash": identity_scaffold_hash,
                    "generation_scaffold_hash": (
                        generation_scaffold_hash
                    ),
                    "creative_contract_version": (
                        SCREENPLAY_SCENE_CREATIVE_VERSION
                    ),
                    "semantic_review_version": (
                        SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION
                    ),
                    "reviewed_creative_hash": reviewed_creative_hash,
                    "reviewed_shard_content_hash": (
                        reviewed_shard_content_hash
                    ),
                },
            ),
            step_run_id=trace.step_run_id,
        )
        checkpoint_rows[plan.shard_id].update({
            "status": "validated",
            "raw_artifact_id": raw_artifact["id"],
            "normalized_artifact_id": artifact["id"],
            "identity_scaffold_hash": identity_scaffold_hash,
            "generation_scaffold_hash": generation_scaffold_hash,
            "reused": False,
        })
        emit_progress()
        return shard, str(artifact["id"])

    async def generate_one_with_checkpoint(
        plan: ScreenplaySceneShardPlan,
    ) -> tuple[ScreenplaySceneShardIR, str]:
        try:
            return await generate_one(plan)
        except asyncio.CancelledError:
            # Cancellation (whether user-requested or fail-fast sibling
            # cleanup) is not a shard validation failure.
            raise
        except Exception as exc:
            batch_abort.set()
            checkpoint_rows[plan.shard_id]["status"] = "failed"
            checkpoint_rows[plan.shard_id]["error_type"] = type(exc).__name__
            emit_progress()
            raise

    results = await _gather_fail_fast(
        *(generate_one_with_checkpoint(plan) for plan in plans),
    )
    shards = [shard for shard, _artifact_id in results]
    artifact_ids = [artifact_id for _shard, artifact_id in results]
    emit_progress()
    return shards, artifact_ids, [checkpoint_rows[plan.shard_id] for plan in plans]


def persist_identity_registry(
    *,
    episode_id: str,
    identity_registry: list[dict[str, Any]],
    identity_registry_hash: str,
    parent_artifact_ids: list[str] | None = None,
) -> str:
    _assert_episode_owner(episode_id)
    cached = _latest_validated_artifact(
        episode_id=episode_id,
        artifact_type="screenplay_identity_registry",
        predicate=lambda content: (
            content.get("identity_registry_hash") == identity_registry_hash
        ),
    )
    expected_parents = {
        str(parent_id)
        for parent_id in parent_artifact_ids or []
        if str(parent_id)
    }
    if cached and _artifact_parent_ids(cached) == expected_parents:
        return str(cached["id"])
    trace = current_trace()
    artifact = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_identity_registry",
            scope_type="episode",
            scope_id=episode_id,
            status="validated",
            trust_level="T1",
            content={
                "contract_version": "screenplay-identity-registry.v1",
                "identity_registry_hash": identity_registry_hash,
                "identities": identity_registry,
            },
            parent_artifact_ids=list(parent_artifact_ids or []),
            contract_version="screenplay-identity-registry.v1",
        ),
        step_run_id=trace.step_run_id,
    )
    return str(artifact["id"])


def persist_merged_ir(
    *,
    episode_id: str,
    ir: ScreenplayGenerationIR,
    parent_artifact_ids: list[str],
    blueprint_hash: str,
    identity_registry_hash: str,
) -> str:
    _assert_episode_owner(episode_id)
    trace = current_trace()
    artifact = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_generation_ir_merged",
            scope_type="episode",
            scope_id=episode_id,
            status="validated",
            trust_level="T1",
            content=ir.model_dump(mode="json"),
            parent_artifact_ids=list(dict.fromkeys(parent_artifact_ids)),
            contract_version=SCREENPLAY_MERGED_IR_VERSION,
            model_snapshot={
                "generation_contract": IR_VERSION,
                "blueprint_hash": blueprint_hash,
                "identity_registry_hash": identity_registry_hash,
                "scene_count": len(ir.scenes),
                "unit_count": sum(len(scene.units) for scene in ir.scenes),
            },
        ),
        step_run_id=trace.step_run_id,
    )
    object.__setattr__(ir, "evidence_artifact_id", artifact["id"])
    return str(artifact["id"])


def shard_progress(rows: list[dict[str, Any]] | None) -> dict[str, int]:
    values = list(rows or [])
    return {
        "total": len(values),
        "validated": sum(item.get("status") == "validated" for item in values),
        "running": sum(item.get("status") == "running" for item in values),
        "failed": sum(item.get("status") == "failed" for item in values),
    }
