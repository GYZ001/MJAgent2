"""Pre-writing narrative authority contract for screenplay generation.

The model identifies semantic timeline nodes. The server validates source
ownership and state transitions, then derives scene boundaries
deterministically. Screenplay prose is authored only after this contract
passes.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.source_excerpt import (
    index_source_segments,
    structural_front_matter_ids,
)
from app.source_facts import SOURCE_FACT_VERSION, SourceFact, source_facts


BLUEPRINT_VERSION = "screenplay-narrative-blueprint.v7"
BLUEPRINT_PROMPT_VERSION = "screenplay-blueprint-1.8.0"
BLUEPRINT_MAX_SOURCE_SEGMENTS_PER_NODE = 8
# Provider-facing Blueprint shards are deliberately smaller than the final
# scene/node ownership limit.  A production 28-SRC shard exhausted 10K output
# tokens before closing its JSON object; 14 sequential SRCs leaves enough
# bounded headroom for the full typed node contract without accepting a
# truncated prefix.
BLUEPRINT_TARGET_SOURCE_SEGMENTS_PER_SHARD = 14
BLUEPRINT_TARGET_SOURCE_FACTS_PER_SHARD = 18
BLUEPRINT_SHARD_POLICY_VERSION = "blueprint-shard-policy.v8"
BLUEPRINT_SHARD_LOCAL_AUTHORITY_VERSION = (
    "blueprint-shard-local-authority.v6"
)
BLUEPRINT_SPLIT_MANIFEST_VERSION = "blueprint-split-manifest.v1"


def blueprint_authority_validator_fingerprint() -> str:
    material = {
        "contract_version": BLUEPRINT_VERSION,
        "prompt_version": BLUEPRINT_PROMPT_VERSION,
        "source_fact_version": SOURCE_FACT_VERSION,
        "shard_policy_version": BLUEPRINT_SHARD_POLICY_VERSION,
        "local_authority_validator_version": (
            BLUEPRINT_SHARD_LOCAL_AUTHORITY_VERSION
        ),
        "split_manifest_version": BLUEPRINT_SPLIT_MANIFEST_VERSION,
    }
    return hashlib.sha256(
        json.dumps(
            material,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _normalize_source_segment_id(value: Any) -> str:
    raw = str(value or "").strip()
    match = re.fullmatch(r"SRC0*(\d+)", raw, flags=re.IGNORECASE)
    if match is None:
        return raw
    return f"SRC{int(match.group(1)):04d}"


def normalize_blueprint_raw_json(raw: str) -> str:
    """Repair a provider's redundant node-closing brace mechanically."""
    normalized = re.sub(
        r"\}\}\},\s*(\{\"key\"\s*:)",
        r"}},\1",
        raw,
    )
    return re.sub(
        r"\}\}\]\},\s*(\"delete_node_keys\"\s*:)",
        r"}]}],\1",
        normalized,
    )


_PARATEXT_EMPTY_LIST_FIELDS = (
    "participants",
    "participant_evidence",
    "state_subject_assignments",
    "environment_source_unit_keys",
    "source_unit_deliveries",
    "state_requirements",
    "state_changes",
    "released_constraints_for",
)


def normalize_blueprint_provider_payload(payload: Any) -> Any:
    """Normalize provider-only cross-field drift without inventing authority.

    Provider bytes remain preserved in the raw T0 artifact.  This projection is
    limited to explicit provider claims: paratext fields are emptied, evidence
    identities are added to the participant roster, and voice claims are
    removed only for the exact units explicitly classified as non-audible.
    Missing evidence is never synthesized and participants are never deleted.
    """
    if not isinstance(payload, dict):
        return payload
    normalized = dict(payload)
    nodes = payload.get("nodes")
    if not isinstance(nodes, list):
        return normalized
    normalized_nodes: list[Any] = []
    for value in nodes:
        if not isinstance(value, dict):
            normalized_nodes.append(value)
            continue
        node = dict(value)
        if value.get("narrative_layer") == "paratext":
            for field_name in _PARATEXT_EMPTY_LIST_FIELDS:
                node[field_name] = []
            node["decision"] = None
            node["exit_state"] = ""
            normalized_nodes.append(node)
            continue

        non_audible_units = {
            str(delivery.get("source_unit_key") or "")
            for delivery in value.get("source_unit_deliveries") or []
            if (
                isinstance(delivery, dict)
                and delivery.get("mode") not in AUDIBLE_SOURCE_DELIVERY_MODES
            )
        }
        evidence_values: list[Any] = []
        evidence_identities: list[str] = []
        for evidence_value in value.get("participant_evidence") or []:
            if not isinstance(evidence_value, dict):
                evidence_values.append(evidence_value)
                continue
            evidence = dict(evidence_value)
            if evidence.get("usage") == "voice":
                source_unit_keys = evidence.get("source_unit_keys") or []
                retained_keys = [
                    key
                    for key in source_unit_keys
                    if str(key) not in non_audible_units
                ]
                if source_unit_keys and not retained_keys:
                    continue
                evidence["source_unit_keys"] = retained_keys
            evidence_values.append(evidence)
            identity_key = str(evidence.get("identity_key") or "").strip()
            if identity_key and identity_key not in evidence_identities:
                evidence_identities.append(identity_key)
        for assignment in value.get("state_subject_assignments") or []:
            if not isinstance(assignment, dict):
                continue
            for identity_key_value in assignment.get("identity_keys") or []:
                identity_key = str(identity_key_value or "").strip()
                if identity_key and identity_key not in evidence_identities:
                    evidence_identities.append(identity_key)
        node["participant_evidence"] = evidence_values
        # Evidence rows and exact-unit joint assignments are the two typed
        # source-backed identity authorities. An independently authored roster
        # would create a third truth that can survive into downstream IR.
        node["participants"] = evidence_identities
        normalized_nodes.append(node)
    normalized["nodes"] = normalized_nodes
    return normalized


def blueprint_shard_provider_schema(
    source_payload: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return the provider schema with explicit delivery evidence surfaces."""
    schema = NarrativeBlueprintShard.model_json_schema()
    definitions = schema.get("$defs", {})
    node_schema = definitions.get("NarrativeNode")
    if not isinstance(node_schema, dict):
        return schema
    source_ids: list[str] = []
    source_unit_keys: list[str] = []
    action_unit_keys: list[str] = []
    quoted_unit_keys: list[str] = []
    for source in source_payload or []:
        source_id = str(source.get("source_segment_id") or "")
        if source_id and source_id not in source_ids:
            source_ids.append(source_id)
        for fact in source.get("source_facts") or []:
            source_unit_key = str(fact.get("source_unit_key") or "")
            if not source_unit_key or source_unit_key in source_unit_keys:
                continue
            source_unit_keys.append(source_unit_key)
            if fact.get("projection") == "action":
                action_unit_keys.append(source_unit_key)
            elif fact.get("projection") == "quoted":
                quoted_unit_keys.append(source_unit_key)

    if source_ids:
        schema["properties"]["source_segment_ids"]["items"] = {
            "enum": source_ids,
        }
    node_properties = node_schema.get("properties", {})
    if source_ids:
        node_properties["source_segment_ids"]["items"] = {
            "enum": source_ids,
        }
    node_properties["source_segment_ids"]["minItems"] = 1
    node_properties["source_segment_ids"]["maxItems"] = (
        BLUEPRINT_MAX_SOURCE_SEGMENTS_PER_NODE
    )
    if action_unit_keys:
        node_properties["environment_source_unit_keys"]["items"] = {
            "enum": action_unit_keys,
        }
    else:
        node_properties["environment_source_unit_keys"]["maxItems"] = 0
        node_properties["state_subject_assignments"]["maxItems"] = 0
    node_properties.get("participants", {})["description"] = (
        "Ordered identity roster. Its unique identity set must exactly equal "
        "the union of participant_evidence.identity_key and "
        "state_subject_assignments.identity_keys; never add an identity "
        "without owned source evidence."
    )
    node_properties.get("participant_evidence", {})["description"] = (
        "Source-backed identity evidence. Together with exact-unit joint "
        "state_subject assignments, its identity set must exactly equal "
        "participants."
    )
    location_schema = node_properties.get("location_label")
    if isinstance(location_schema, dict):
        location_schema["pattern"] = r"^(?!.*(?:、|/|\+|内外)).+$"
        location_schema["description"] = (
            "Exactly one primary location; never combine locations."
        )
    evidence_schema = definitions.get("NarrativeParticipantEvidence")
    if isinstance(evidence_schema, dict):
        evidence_properties = evidence_schema.get("properties", {})
        evidence_properties.get("identity_key", {})["minLength"] = 1
        evidence_properties.get("source_segment_ids", {})["minItems"] = 1
        if source_ids:
            evidence_properties["source_segment_ids"]["items"] = {
                "enum": source_ids,
            }
        if source_unit_keys:
            evidence_properties["source_unit_keys"]["items"] = {
                "enum": source_unit_keys,
            }
        else:
            evidence_properties["source_unit_keys"]["maxItems"] = 0
        evidence_schema["description"] = (
            "Every participants identity must have at least one matching "
            "evidence object with owned source_segment_ids."
        )
        evidence_schema.setdefault("allOf", []).append({
            "if": {
                "properties": {
                    "usage": {
                        "enum": ["voice", "state_subject"],
                    },
                },
                "required": ["usage"],
            },
            "then": {
                "properties": {
                    "identity_key": {"minLength": 1},
                    "source_segment_ids": {"minItems": 1},
                    "source_unit_keys": {"minItems": 1},
                },
                "required": [
                    "identity_key",
                    "source_segment_ids",
                    "source_unit_keys",
                ],
            },
        })
    assignment_schema = definitions.get("NarrativeStateSubjectAssignment")
    if isinstance(assignment_schema, dict) and action_unit_keys:
        assignment_schema["properties"]["source_unit_key"] = {
            "enum": action_unit_keys,
        }
    delivery_schema = definitions.get("NarrativeSourceUnitDelivery")
    if isinstance(delivery_schema, dict):
        if quoted_unit_keys:
            delivery_schema["properties"]["source_unit_key"] = {
                "enum": quoted_unit_keys,
            }
        else:
            node_properties["source_unit_deliveries"]["maxItems"] = 0
        delivery_schema.setdefault("allOf", []).append({
            "if": {
                "properties": {
                    "mode": {
                        "enum": [
                            "spoken_dialogue",
                            "offscreen_voice",
                        ],
                    },
                },
                "required": ["mode"],
            },
            "then": {
                "properties": {
                    "performer_key": {"minLength": 1},
                },
                "required": ["performer_key"],
            },
        })
    required = node_schema.setdefault("required", [])
    for field_name in (
        "participants",
        "participant_evidence",
        "state_subject_assignments",
        "environment_source_unit_keys",
        "source_unit_deliveries",
    ):
        if field_name not in required:
            required.append(field_name)
    node_schema.setdefault("allOf", []).append({
        "if": {
            "properties": {
                "source_unit_deliveries": {
                    "contains": {
                        "properties": {
                            "mode": {
                                "enum": [
                                    "spoken_dialogue",
                                    "offscreen_voice",
                                ],
                            },
                        },
                        "required": ["mode"],
                    },
                },
            },
            "required": ["source_unit_deliveries"],
        },
        "then": {
            "properties": {
                "participant_evidence": {
                    "contains": {
                        "properties": {
                            "usage": {"const": "voice"},
                        },
                        "required": [
                            "identity_key",
                            "source_unit_keys",
                            "usage",
                        ],
                    },
                },
            },
            "required": ["participant_evidence"],
        },
    })
    node_schema.setdefault("allOf", []).append({
        "if": {
            "properties": {
                "participants": {"minItems": 1},
            },
            "required": ["participants"],
        },
        "then": {
            "properties": {
                "participant_evidence": {"minItems": 1},
            },
            "required": ["participant_evidence"],
        },
    })
    paratext_properties: dict[str, Any] = {
        field_name: {"const": []}
        for field_name in _PARATEXT_EMPTY_LIST_FIELDS
    }
    paratext_properties.update({
        "decision": {"const": None},
        "exit_state": {"const": ""},
    })
    node_schema.setdefault("allOf", []).append({
        "if": {
            "properties": {"narrative_layer": {"const": "paratext"}},
            "required": ["narrative_layer"],
        },
        "then": {"properties": paratext_properties},
    })
    return schema


def recover_complete_blueprint_prefix(raw: str) -> dict[str, Any] | None:
    """Recover complete timeline nodes when a long blueprint hits max_tokens."""
    text = normalize_blueprint_raw_json(str(raw or ""))
    nodes_match = re.search(r'"nodes"\s*:\s*\[', text)
    if nodes_match is None:
        return None
    decoder = json.JSONDecoder()
    cursor = nodes_match.end()
    nodes: list[dict[str, Any]] = []
    while cursor < len(text):
        while cursor < len(text) and (
            text[cursor].isspace() or text[cursor] == ","
        ):
            cursor += 1
        if cursor >= len(text) or text[cursor] == "]":
            break
        try:
            value, cursor = decoder.raw_decode(text, cursor)
            node = NarrativeNode.model_validate(value)
        except (json.JSONDecodeError, TypeError, ValueError):
            break
        nodes.append(node.model_dump(mode="json"))
    if not nodes:
        return None
    episode_match = re.search(r'"episode_no"\s*:\s*(\d+)', text)
    return {
        "format_version": BLUEPRINT_VERSION,
        "episode_no": (
            int(episode_match.group(1))
            if episode_match is not None
            else 1
        ),
        "nodes": nodes,
        "scene_plans": [],
    }


class BlueprintStateRequirement(BaseModel):
    state_key: str
    required_fact_key: str = ""
    expected_value: str = ""
    reason: str
    assumed_prior: bool = False

    @model_validator(mode="before")
    @classmethod
    def _normalize_expected_value(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        normalized["expected_value"] = str(
            normalized.get("expected_value")
            or normalized.get("value")
            or normalized.get("required_value")
            or normalized.get("state_value")
            or ""
        )
        normalized.setdefault(
            "required_fact_key",
            normalized.get("fact_key")
            or normalized.get("depends_on_fact_key")
            or "",
        )
        return normalized


class BlueprintStateChange(BaseModel):
    fact_key: str
    state_key: str
    value: str
    reason: str
    supersedes_fact_keys: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalize_value(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        normalized.setdefault(
            "value",
            normalized.get("new_value")
            or normalized.get("expected_value")
            or normalized.get("state_value")
            or normalized.get("current_value"),
        )
        return normalized


class BlueprintDecision(BaseModel):
    actor_key: str
    choice: str
    impact: Literal["routine", "major"] = "routine"
    setup_node_keys: list[str] = Field(default_factory=list)
    pressure: str = ""
    desire: str = ""
    agency_mode: Literal[
        "voluntary", "reluctant", "coerced", "incapacitated", "unclear",
    ] = "unclear"
    agency_change_reason: str = ""
    constraint_fact_key: str = ""
    constraint_release_node_keys: list[str] = Field(default_factory=list)
    narrative_attribution: Literal[
        "voluntary_choice",
        "external_coercion",
        "impaired_capacity",
        "unclear",
    ] = "unclear"

    @model_validator(mode="after")
    def _bind_agency_attribution(self) -> BlueprintDecision:
        self.narrative_attribution = {
            "voluntary": "voluntary_choice",
            "reluctant": "voluntary_choice",
            "coerced": "external_coercion",
            "incapacitated": "impaired_capacity",
            "unclear": "unclear",
        }[self.agency_mode]
        return self


class NarrativeParticipantEvidence(BaseModel):
    identity_key: str
    source_segment_ids: list[str] = Field(default_factory=list)
    source_unit_keys: list[str] = Field(default_factory=list)
    usage: Literal["visible", "voice", "mentioned", "state_subject"]

    @field_validator("source_segment_ids", mode="before")
    @classmethod
    def _normalize_source_ids(cls, value: Any) -> list[str]:
        values = value if isinstance(value, list) else [value] if value else []
        return [_normalize_source_segment_id(item) for item in values]


class NarrativeStateSubjectAssignment(BaseModel):
    """Typed ownership for one structurally indivisible joint action."""

    model_config = ConfigDict(extra="forbid")

    source_unit_key: str
    mode: Literal["joint"]
    identity_keys: list[str] = Field(min_length=2)

    @model_validator(mode="after")
    def _validate_joint_identities(self) -> NarrativeStateSubjectAssignment:
        normalized = [
            str(identity_key or "").strip()
            for identity_key in self.identity_keys
        ]
        if any(not identity_key for identity_key in normalized):
            raise ValueError("joint state subject identity_keys 不得为空")
        if len(normalized) != len(set(normalized)):
            raise ValueError("joint state subject identity_keys 不得重复")
        self.identity_keys = normalized
        return self


class NarrativeSourceUnitDelivery(BaseModel):
    """Semantic delivery decision for one structurally quoted source unit."""

    model_config = ConfigDict(extra="forbid")

    source_unit_key: str
    mode: Literal[
        "spoken_dialogue",
        "offscreen_voice",
        "written_text",
        "sound_effect",
        "unspoken_reference",
    ]
    content_owner_key: str = ""
    performer_key: str = ""

    @model_validator(mode="after")
    def _validate_delivery_roles(self) -> "NarrativeSourceUnitDelivery":
        audible = self.mode in {
            "spoken_dialogue",
            "offscreen_voice",
        }
        if audible and not self.performer_key.strip():
            raise ValueError(
                "可听引用单元必须填写 performer_key"
            )
        if not audible and self.performer_key.strip():
            raise ValueError(
                "非声音交付不得填写 performer_key"
            )
        return self


class BlueprintSourceSemantics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    narrative_layer: Literal["story", "paratext"]
    event_priority: Literal["causal", "connective"]
    render_policy: Literal["standalone", "exclude_from_spine"]
    disposition: Literal["deliver", "audit_only"]
    projection_policy: Literal["picture", "audit_only"]

    @model_validator(mode="after")
    def _validate_projection(self) -> BlueprintSourceSemantics:
        expected = (
            ("causal", "standalone", "deliver", "picture")
            if self.narrative_layer == "story"
            else (
                "connective",
                "exclude_from_spine",
                "audit_only",
                "audit_only",
            )
        )
        actual = (
            self.event_priority,
            self.render_policy,
            self.disposition,
            self.projection_policy,
        )
        if actual != expected:
            raise ValueError(
                f"{self.narrative_layer} 来源语义必须为 {expected}"
            )
        return self


class NarrativeNode(BaseModel):
    key: str
    source_segment_ids: list[str]
    summary: str
    narrative_layer: Literal["story", "paratext"]
    event_priority: Literal["causal", "connective"]
    render_policy: Literal["standalone", "exclude_from_spine"]
    temporal_domain_key: str
    time_label: str
    time_relation: Literal[
        "episode_start",
        "continuous",
        "elapsed",
        "jump",
        "flashback_enter",
        "flashback_continue",
        "flashback_exit",
        "montage",
    ]
    location_key: str
    location_label: str
    participants: list[str] = Field(default_factory=list)
    participant_evidence: list[NarrativeParticipantEvidence] = Field(
        default_factory=list,
    )
    state_subject_assignments: list[
        NarrativeStateSubjectAssignment
    ] = Field(default_factory=list)
    # Every prose/action source unit must either have one identity-bearing
    # state_subject evidence row or be explicitly classified as environment.
    # Absence is never interpreted as environment: that used to turn missing
    # character attribution into a synthetic environment state silently.
    environment_source_unit_keys: list[str] = Field(default_factory=list)
    source_unit_deliveries: list[NarrativeSourceUnitDelivery] = Field(
        default_factory=list,
    )
    scene_boundary_before: bool = False
    transition_cue: str = ""
    opening_image: str = ""
    exit_state: str = ""
    scene_role: Literal["bridge", "setup", "action", "turn", "reaction"] = (
        "action"
    )
    dramatic_load: int = Field(default=1, ge=1, le=3)
    action_logic: str
    adaptation_kind: Literal[
        "source_direct", "source_inferred", "logic_bridge",
    ] = "source_direct"
    bridge_rationale: str = ""
    state_requirements: list[BlueprintStateRequirement] = Field(
        default_factory=list,
    )
    state_changes: list[BlueprintStateChange] = Field(default_factory=list)
    released_constraints_for: list[str] = Field(default_factory=list)
    decision: BlueprintDecision | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_location_label(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        normalized["source_segment_ids"] = [
            _normalize_source_segment_id(source_id)
            for source_id in (
                normalized.get("source_segment_ids") or []
            )
        ]
        if not normalized.get("location_label"):
            normalized["location_label"] = str(
                normalized.get("location_key") or "未标注地点"
            )
        return normalized

    @model_validator(mode="after")
    def _validate_narrative_semantics(self) -> NarrativeNode:
        expected = (
            ("causal", "standalone")
            if self.narrative_layer == "story"
            else ("connective", "exclude_from_spine")
        )
        if (self.event_priority, self.render_policy) != expected:
            raise ValueError(
                f"{self.narrative_layer} 节点必须使用 "
                f"event_priority={expected[0]}、render_policy={expected[1]}"
            )
        if self.narrative_layer == "paratext" and any((
            self.participants,
            self.participant_evidence,
            self.state_subject_assignments,
            self.environment_source_unit_keys,
            self.source_unit_deliveries,
            self.exit_state.strip(),
            self.state_requirements,
            self.state_changes,
            self.released_constraints_for,
            self.decision is not None,
        )):
            raise ValueError(
                "paratext 节点不得承载人物、决定或剧情状态合同"
            )
        return self

    def source_semantics(self) -> BlueprintSourceSemantics:
        return BlueprintSourceSemantics(
            narrative_layer=self.narrative_layer,
            event_priority=self.event_priority,
            render_policy=self.render_policy,
            disposition=(
                "deliver"
                if self.narrative_layer == "story"
                else "audit_only"
            ),
            projection_policy=(
                "picture"
                if self.narrative_layer == "story"
                else "audit_only"
            ),
        )


class BlueprintScenePlan(BaseModel):
    key: str
    node_keys: list[str]
    source_segment_ids: list[str]
    source_semantics: dict[str, BlueprintSourceSemantics]
    temporal_domain_key: str
    time_label: str
    location_key: str
    location_label: str
    transition_cue: str
    previous_scene_exit_state: str = ""
    opening_image: str = ""
    exit_state: str = ""
    dramatic_load: int = 1
    agency_contracts: list[dict[str, str]] = Field(default_factory=list)
    participant_keys: list[str] = Field(default_factory=list)
    scene_heading: str


class BlueprintSourceAuditAnnotation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_key: str
    source_segment_ids: list[str]
    narrative_layer: Literal["paratext"] = "paratext"
    render_policy: Literal["exclude_from_spine"] = "exclude_from_spine"
    disposition: Literal["audit_only"] = "audit_only"
    projection_policy: Literal["audit_only"] = "audit_only"


class BlueprintSceneDerivation(BaseModel):
    relation_key: str
    relation_type: str
    source_scene_plan_key: str
    target_scene_plan_key: str
    source_node_key: str
    target_node_key: str
    reference_key: str = ""
    summary: str = ""


class BlueprintSourceOwnershipError(ValueError):
    def __init__(self, conflicts: dict[str, list[str]]):
        self.conflicts = {
            source_id: list(scene_keys)
            for source_id, scene_keys in conflicts.items()
        }
        self.errors = [
            "[BLUEPRINT_SOURCE_OWNER_CONFLICT] "
            f"{source_id} 同时归属 " + "、".join(scene_keys)
            for source_id, scene_keys in self.conflicts.items()
        ]
        super().__init__("；".join(self.errors))


class BlueprintSourceOccurrenceError(ValueError):
    def __init__(
        self,
        duplicates: dict[str, list[str]],
        *,
        partition_conflicts: set[str] | None = None,
    ):
        self.duplicates = {
            source_id: list(node_keys)
            for source_id, node_keys in duplicates.items()
        }
        conflicts = set(partition_conflicts or ())
        self.errors = []
        for source_id, node_keys in self.duplicates.items():
            code = (
                "[BLUEPRINT_SOURCE_PARTITION_CONFLICT] "
                if source_id in conflicts
                else "[BLUEPRINT_SOURCE_OCCURRENCE_DUPLICATE] "
            )
            self.errors.append(
                code
                + f"{source_id} 被 picture/audit timeline nodes 重复拥有："
                + "、".join(node_keys)
            )
        super().__init__("；".join(self.errors))


class BlueprintSourceOccurrenceIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_segment_id: str
    node_keys: list[str]
    error: str


def blueprint_source_occurrence_issues(
    nodes: list[NarrativeNode],
    *,
    prefix: str = "BLUEPRINT",
) -> list[BlueprintSourceOccurrenceIssue]:
    owners: defaultdict[str, list[str]] = defaultdict(list)
    partitions: defaultdict[str, list[str]] = defaultdict(list)
    for node in nodes:
        partition = node.source_semantics().projection_policy
        for source_id in node.source_segment_ids:
            owners[source_id].append(node.key)
            partitions[source_id].append(partition)
    issues: list[BlueprintSourceOccurrenceIssue] = []
    for source_id, node_keys in owners.items():
        if len(node_keys) <= 1:
            continue
        partition_names = set(partitions[source_id])
        if len(partition_names) > 1:
            code = f"{prefix}_SOURCE_PARTITION_CONFLICT"
        elif partition_names == {"audit_only"}:
            code = f"{prefix}_AUDIT_SOURCE_DUPLICATE"
        else:
            code = f"{prefix}_PICTURE_SOURCE_DUPLICATE"
        issues.append(
            BlueprintSourceOccurrenceIssue(
                source_segment_id=source_id,
                node_keys=node_keys,
                error=(
                    f"[{code}] {source_id} 必须恰由一个 timeline node 拥有，"
                    "实际：" + "、".join(node_keys)
                ),
            )
        )
    return issues


class NarrativeBlueprint(BaseModel):
    format_version: Literal["screenplay-narrative-blueprint.v7"] = (
        BLUEPRINT_VERSION
    )
    episode_no: int
    nodes: list[NarrativeNode]
    scene_plans: list[BlueprintScenePlan] = Field(default_factory=list)
    source_scene_owners: dict[str, str] = Field(default_factory=dict)
    source_semantics: dict[str, BlueprintSourceSemantics] = Field(
        default_factory=dict,
    )
    source_audit_annotations: list[BlueprintSourceAuditAnnotation] = Field(
        default_factory=list,
    )
    scene_derivations: list[BlueprintSceneDerivation] = Field(
        default_factory=list,
    )


class NarrativeBlueprintShard(BaseModel):
    format_version: Literal["screenplay-narrative-blueprint.v7"] = (
        BLUEPRINT_VERSION
    )
    episode_no: int
    shard_index: int
    source_segment_ids: list[str]
    nodes: list[NarrativeNode]
    source_hash: str = ""
    boundary_hash: str = ""

    @model_validator(mode="before")
    @classmethod
    def _normalize_version(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        normalized["source_segment_ids"] = [
            _normalize_source_segment_id(source_id)
            for source_id in (
                normalized.get("source_segment_ids") or []
            )
        ]
        return normalized

    @model_validator(mode="after")
    def _require_typed_voice_delivery_evidence(
        self,
    ) -> "NarrativeBlueprintShard":
        """Make audible performer ownership part of the v7 typed response."""
        for node in self.nodes:
            voice_claims: defaultdict[
                str,
                list[NarrativeParticipantEvidence],
            ] = defaultdict(list)
            for evidence in node.participant_evidence:
                if evidence.usage != "voice":
                    continue
                for source_unit_key in dict.fromkeys(
                    evidence.source_unit_keys
                ):
                    voice_claims[source_unit_key].append(evidence)

            deliveries = {
                delivery.source_unit_key: delivery
                for delivery in node.source_unit_deliveries
            }
            for delivery in node.source_unit_deliveries:
                claims = voice_claims.get(delivery.source_unit_key, [])
                audible = delivery.mode in AUDIBLE_SOURCE_DELIVERY_MODES
                if not audible and claims:
                    raise ValueError(
                        f"{delivery.source_unit_key} 非声音 delivery "
                        "不得包含 usage=voice participant_evidence"
                    )
                if not audible:
                    continue
                if len(claims) != 1:
                    raise ValueError(
                        f"{delivery.source_unit_key} 声音 delivery 必须恰有一条"
                        "精确 source_unit_key 的 usage=voice "
                        "participant_evidence；performer_key 不能替代该证据"
                    )
                claim = claims[0]
                if claim.identity_key != delivery.performer_key:
                    raise ValueError(
                        f"{delivery.source_unit_key} 的 usage=voice "
                        "identity_key 必须等于 performer_key"
                    )
            for source_unit_key in voice_claims:
                delivery = deliveries.get(source_unit_key)
                if (
                    delivery is None
                    or delivery.mode not in AUDIBLE_SOURCE_DELIVERY_MODES
                ):
                    raise ValueError(
                        f"{source_unit_key} 的 usage=voice "
                        "participant_evidence 缺少对应声音 delivery"
                    )
        return self


def validate_narrative_blueprint_shard(
    shard: NarrativeBlueprintShard,
    *,
    expected_episode_no: int,
    expected_shard_index: int,
    expected_source_segment_ids: list[str],
    optional_source_segment_ids: set[str] | None = None,
    boundary_state_facts: list[dict[str, Any]] | None = None,
    source_text: str | None = None,
) -> list[str]:
    errors: list[str] = []
    expected = list(expected_source_segment_ids)
    expected_set = set(expected)
    optional = set(optional_source_segment_ids or ())
    if shard.episode_no != expected_episode_no:
        errors.append("[BLUEPRINT_SHARD_EPISODE] episode_no 不匹配")
    if shard.shard_index != expected_shard_index:
        errors.append("[BLUEPRINT_SHARD_INDEX] shard_index 不匹配")
    if shard.source_segment_ids != expected:
        errors.append("[BLUEPRINT_SHARD_SOURCE_CONTRACT] 分片来源清单不匹配")
    owned = [
        source_id
        for node in shard.nodes
        for source_id in node.source_segment_ids
    ]
    errors.extend(
        issue.error
        for issue in blueprint_source_occurrence_issues(
            shard.nodes,
            prefix="BLUEPRINT_SHARD",
        )
    )
    escaped = set(owned) - expected_set
    if escaped:
        errors.append(
            "[BLUEPRINT_SHARD_SOURCE_ESCAPE] 节点引用分片外来源："
            + "、".join(sorted(escaped))
        )
    missing = expected_set - set(owned) - optional
    if missing:
        errors.append(
            "[BLUEPRINT_SHARD_SOURCE_MISSING] 分片漏掉来源："
            + "、".join(sorted(missing))
        )
    source_positions = {
        source_id: position
        for position, source_id in enumerate(expected)
    }
    active_facts = {
        str(fact.get("fact_key") or ""): str(
            fact.get("state_key") or ""
        )
        for fact in (boundary_state_facts or [])
        if str(fact.get("fact_key") or "")
    }
    prior_position = -1
    for node_index, node in enumerate(shard.nodes):
        previous = shard.nodes[node_index - 1] if node_index else None
        if not node.source_segment_ids:
            errors.append(
                f"[BLUEPRINT_SHARD_NODE_UNGROUNDED] {node.key} 没有来源段"
            )
            continue
        if (
            len(node.source_segment_ids)
            > BLUEPRINT_MAX_SOURCE_SEGMENTS_PER_NODE
        ):
            errors.append(
                f"[BLUEPRINT_SHARD_NODE_SIZE] {node.key} 合并了"
                f"{len(node.source_segment_ids)} 个来源段，最多允许 "
                f"{BLUEPRINT_MAX_SOURCE_SEGMENTS_PER_NODE} 个；"
                "source-fact unit 数量不计入 node size"
            )
            continue
        positions = [
            source_positions[source_id]
            for source_id in node.source_segment_ids
            if source_id in source_positions
        ]
        if positions and positions != list(
            range(min(positions), max(positions) + 1)
        ):
            errors.append(
                f"[BLUEPRINT_SHARD_SOURCE_GAP] {node.key} 来源不连续"
            )
        if positions and min(positions) < prior_position:
            errors.append(
                f"[BLUEPRINT_SHARD_SOURCE_ORDER] {node.key} 来源顺序倒退"
            )
        if positions:
            prior_position = min(positions)
        if re.search(r"[、+/]|内外", node.location_label):
            errors.append(
                f"[BLUEPRINT_SHARD_LOCATION_COMPOSITE] {node.key} "
                f"包含多个主要地点：{node.location_label}"
            )
        if previous is not None:
            changed_domain = (
                node.temporal_domain_key
                != previous.temporal_domain_key
            )
            changed_location = node.location_key != previous.location_key
            if changed_domain and node.time_relation == "continuous":
                errors.append(
                    f"[BLUEPRINT_SHARD_TIME_RELATION] {node.key} "
                    "时间域变化却标记 continuous"
                )
            if (
                (changed_domain or changed_location)
                and not node.transition_cue.strip()
            ):
                errors.append(
                    f"[BLUEPRINT_SHARD_TRANSITION_REQUIRED] {node.key} "
                    "时空变化缺少可见/可听转场"
                )
        if (
            node.decision is not None
            and node.decision.impact == "major"
            and (
                not node.decision.setup_node_keys
                or not node.decision.pressure.strip()
                or not node.decision.desire.strip()
            )
        ):
            errors.append(
                f"[BLUEPRINT_SHARD_MOTIVATION_REQUIRED] {node.key} "
                "重大决定缺少前置节点、压力或欲望"
            )
        for requirement in node.state_requirements:
            if (
                not requirement.assumed_prior
                and not requirement.required_fact_key.strip()
            ):
                errors.append(
                    f"[BLUEPRINT_SHARD_FACT_REQUIRED] {node.key} "
                    f"状态 {requirement.state_key} 缺少 fact_key"
                )
            elif (
                not requirement.assumed_prior
                and requirement.required_fact_key not in active_facts
            ):
                errors.append(
                    f"[BLUEPRINT_SHARD_FACT_UNKNOWN] {node.key} "
                    f"引用未建立事实 {requirement.required_fact_key}"
                )
        for change in node.state_changes:
            for superseded_key in change.supersedes_fact_keys:
                if superseded_key not in active_facts:
                    errors.append(
                        f"[BLUEPRINT_SHARD_SUPERSEDE_UNKNOWN] {node.key} "
                        f"不能替代未建立事实 {superseded_key}"
                    )
                elif (
                    active_facts[superseded_key] != change.state_key
                    and not node.released_constraints_for
                ):
                    errors.append(
                        f"[BLUEPRINT_SHARD_SUPERSEDE_STATE] {node.key} "
                        f"替代事实 {superseded_key} 的 state_key 不一致"
                    )
                active_facts.pop(superseded_key, None)
            active_facts[change.fact_key] = change.state_key
    node_keys = [node.key for node in shard.nodes]
    if len(node_keys) != len(set(node_keys)):
        errors.append("[BLUEPRINT_SHARD_NODE_DUPLICATE] 节点 key 重复")
    for node in shard.nodes:
        participant_keys = set(node.participants)
        evidence_keys = {
            evidence.identity_key
            for evidence in node.participant_evidence
            if evidence.identity_key
        } | {
            identity_key
            for assignment in node.state_subject_assignments
            for identity_key in assignment.identity_keys
        }
        for evidence in node.participant_evidence:
            escaped_sources = (
                set(evidence.source_segment_ids)
                - set(node.source_segment_ids)
            )
            if escaped_sources:
                errors.append(
                    f"[BLUEPRINT_SHARD_PARTICIPANT_EVIDENCE_OUT_OF_SCOPE] "
                    f"{node.key} identity_key={evidence.identity_key} "
                    "引用非 owned SRC："
                    + "、".join(sorted(escaped_sources))
                )
        orphan_evidence = evidence_keys - participant_keys
        if orphan_evidence:
            errors.append(
                f"[BLUEPRINT_SHARD_PARTICIPANT_EVIDENCE_ORPHAN] "
                f"{node.key} evidence identity 未列入 participants："
                + "、".join(sorted(orphan_evidence))
            )
        missing_evidence = participant_keys - evidence_keys
        if missing_evidence:
            errors.append(
                f"[BLUEPRINT_SHARD_PARTICIPANT_EVIDENCE_MISSING] "
                f"{node.key} participants 缺少同 identity_key 的来源证据"
                "或 exact-unit joint assignment："
                + "、".join(sorted(missing_evidence))
                + "；保留有来源角色并补 participant_evidence，"
                "不得删除角色或改用默认身份"
            )
    if source_text is not None:
        local_blueprint = NarrativeBlueprint(
            episode_no=shard.episode_no,
            nodes=shard.nodes,
        )
        for issue in (
            blueprint_voice_identity_issues(local_blueprint, source_text)
            + blueprint_state_subject_issues(local_blueprint, source_text)
        ):
            errors.append(
                f"[BLUEPRINT_SHARD_{issue.code.upper()}] "
                f"{'、'.join(issue.node_keys)} "
                f"{'、'.join(issue.source_segment_ids)}：{issue.message}；"
                f"必须：{issue.required_resolution}"
            )
    try:
        derive_blueprint_scene_plans(NarrativeBlueprint(
            episode_no=shard.episode_no,
            nodes=shard.nodes,
        ))
    except (
        BlueprintSourceOccurrenceError,
        BlueprintSourceOwnershipError,
    ) as exc:
        errors.extend(
            error.replace(
                "[BLUEPRINT_SOURCE_OWNER_CONFLICT]",
                "[BLUEPRINT_SHARD_SOURCE_OWNER_CONFLICT]",
            )
            for error in exc.errors
        )
    return errors


class NarrativeNodeReplacement(BaseModel):
    node_key: str
    node: NarrativeNode | None = None
    nodes: list[NarrativeNode] = Field(default_factory=list)

    @model_validator(mode="after")
    def _normalize_nodes(self) -> NarrativeNodeReplacement:
        if not self.nodes and self.node is not None:
            self.nodes = [self.node]
        if not self.nodes:
            raise ValueError("replacement 必须提供 node 或 nodes")
        return self


class NarrativeBlueprintPatch(BaseModel):
    replacements: list[NarrativeNodeReplacement] = Field(
        default_factory=list,
    )
    delete_node_keys: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_change(self) -> NarrativeBlueprintPatch:
        if not self.replacements and not self.delete_node_keys:
            raise ValueError("蓝图补丁必须替换或删除至少一个节点")
        return self


class BlueprintSemanticIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Literal[
        "timeline_conflict",
        "spatial_action_gap",
        "persistent_state_conflict",
        "motivation_gap",
        "agency_conflict",
        "setup_missing",
        "identity_or_role_conflict",
        "voice_identity_missing",
        "voice_identity_ambiguous",
        "voice_identity_conflict",
        "source_delivery_missing",
        "source_delivery_conflict",
        "source_delivery_identity_conflict",
        "state_subject_missing",
        "state_subject_ambiguous",
        "state_subject_unit_missing",
        "state_subject_unit_invalid",
        "state_subject_assignment_invalid",
        "state_subject_assignment_ambiguous",
        "state_subject_assignment_conflict",
        "state_subject_environment_duplicate",
        "state_subject_environment_invalid",
        "state_subject_environment_conflict",
        "state_subject_environment_non_picture",
        "ending_payoff_gap",
    ]
    node_keys: list[str]
    source_segment_ids: list[str] = Field(default_factory=list)
    message: str
    required_resolution: str
    must_fix: bool = True


class BlueprintSemanticReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issues: list[BlueprintSemanticIssue] = Field(default_factory=list)


def blueprint_semantic_review_schema(
    canonical_node_keys: list[str],
    canonical_source_segment_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Bind reviewer node references to one ordered Blueprint projection."""
    identities = [str(key).strip() for key in canonical_node_keys]
    if (
        not identities
        or any(not key for key in identities)
        or len(identities) != len(set(identities))
    ):
        raise ValueError("canonical node identities must be non-empty and unique")

    schema = BlueprintSemanticReview.model_json_schema()
    schema["x-canonical-timeline-node-keys"] = identities
    issue_schema = schema["$defs"]["BlueprintSemanticIssue"]
    issue_schema["properties"]["node_keys"] = {
        "title": "Canonical Node References",
        "type": "array",
        "minItems": 1,
        "items": {
            "oneOf": [
                {
                    "type": "string",
                    "enum": identities,
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "ordinal": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": len(identities),
                        },
                    },
                    "required": ["ordinal"],
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "identity": {
                            "type": "string",
                            "enum": identities,
                        },
                    },
                    "required": ["identity"],
                },
            ],
        },
    }
    if canonical_source_segment_ids is not None:
        source_ids = [
            _normalize_source_segment_id(source_id)
            for source_id in canonical_source_segment_ids
        ]
        if (
            any(not source_id for source_id in source_ids)
            or len(source_ids) != len(set(source_ids))
        ):
            raise ValueError(
                "canonical source segment identities must be non-empty "
                "and unique"
            )
        issue_schema["properties"]["source_segment_ids"] = {
            "title": "Canonical Source Segment References",
            "type": "array",
            "items": {
                "type": "string",
                "enum": source_ids,
            },
        }
    return schema


def blueprint_patch_schema(
    blueprint: NarrativeBlueprint,
    replaceable_node_keys: list[str],
) -> dict[str, Any]:
    """Bind each replacement to its current projection authority."""
    node_map = {node.key: node for node in blueprint.nodes}
    keys = [str(key).strip() for key in replaceable_node_keys]
    if (
        not keys
        or any(not key or key not in node_map for key in keys)
        or len(keys) != len(set(keys))
    ):
        raise ValueError(
            "replaceable node identities must exist and be unique"
        )

    schema = NarrativeBlueprintPatch.model_json_schema()
    canonical_node_keys = [node.key for node in blueprint.nodes]
    schema["x-canonical-timeline-node-keys"] = canonical_node_keys
    alternatives: list[dict[str, Any]] = []
    for key in keys:
        node = node_map[key]
        semantics = node.source_semantics()
        node_contract = {
            "type": "object",
            "properties": {
                "key": {"const": key},
                "source_segment_ids": {
                    "const": list(node.source_segment_ids),
                },
                "narrative_layer": {
                    "const": semantics.narrative_layer,
                },
                "event_priority": {
                    "const": semantics.event_priority,
                },
                "render_policy": {
                    "const": semantics.render_policy,
                },
            },
            "required": list(NarrativeNode.model_fields),
        }
        if semantics.narrative_layer == "paratext":
            node_contract["properties"].update({
                field_name: {"const": []}
                for field_name in _PARATEXT_EMPTY_LIST_FIELDS
            })
            node_contract["properties"].update({
                "decision": {"const": None},
                "exit_state": {"const": ""},
            })
        alternatives.append({
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "node_key": {"const": key},
                "node": {
                    "allOf": [
                        {"$ref": "#/$defs/NarrativeNode"},
                        node_contract,
                    ],
                },
            },
            "required": ["node_key", "node"],
        })
    schema["properties"]["replacements"]["items"] = {
        "oneOf": alternatives,
    }
    schema["properties"]["delete_node_keys"] = {
        "type": "array",
        "maxItems": 0,
    }
    return schema


def normalize_blueprint_semantic_review_payload(
    payload: dict[str, Any],
    canonical_node_keys: list[str],
) -> dict[str, Any]:
    """Resolve only exact identity or one-based ordinal node references."""
    identities = tuple(str(key).strip() for key in canonical_node_keys)
    identity_set = set(identities)
    normalized = dict(payload)
    issues = payload.get("issues")
    if not isinstance(issues, list):
        return normalized

    normalized_issues: list[Any] = []
    for issue in issues:
        if not isinstance(issue, dict):
            normalized_issues.append(issue)
            continue
        normalized_issue = dict(issue)
        references = issue.get("node_keys")
        if not isinstance(references, list):
            normalized_issues.append(normalized_issue)
            continue

        normalized_references: list[str] = []
        for reference in references:
            resolved: str | None = None
            if isinstance(reference, str):
                resolved = reference
            elif isinstance(reference, dict):
                if set(reference) == {"identity"}:
                    identity = reference.get("identity")
                    if isinstance(identity, str) and identity in identity_set:
                        resolved = identity
                elif set(reference) == {"ordinal"}:
                    ordinal = reference.get("ordinal")
                    if (
                        isinstance(ordinal, int)
                        and not isinstance(ordinal, bool)
                        and 1 <= ordinal <= len(identities)
                    ):
                        resolved = identities[ordinal - 1]
            if resolved is None:
                resolved = (
                    "[INVALID_BLUEPRINT_NODE_REFERENCE]"
                    + json.dumps(
                        reference,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
            normalized_references.append(resolved)
        normalized_issue["node_keys"] = normalized_references
        normalized_issues.append(normalized_issue)

    normalized["issues"] = normalized_issues
    return normalized


def normalize_blueprint_fact_versions(
    blueprint: NarrativeBlueprint,
) -> int:
    """Convert repeated authored fact handles into deterministic SSA keys."""
    latest_versions: dict[str, str] = {}
    used_keys: set[str] = set()
    changes = 0
    for node in blueprint.nodes:
        for requirement in node.state_requirements:
            requirement.required_fact_key = latest_versions.get(
                requirement.required_fact_key,
                requirement.required_fact_key,
            )
        for change_index, change in enumerate(
            node.state_changes,
            start=1,
        ):
            original_key = change.fact_key
            change.supersedes_fact_keys = [
                latest_versions.get(fact_key, fact_key)
                for fact_key in change.supersedes_fact_keys
            ]
            if original_key in used_keys:
                versioned_key = (
                    f"{original_key}--{node.key}-{change_index}"
                )
                while versioned_key in used_keys:
                    versioned_key += "x"
                change.fact_key = versioned_key
                changes += 1
            used_keys.add(change.fact_key)
            latest_versions[original_key] = change.fact_key
        if node.decision is not None:
            node.decision.constraint_fact_key = latest_versions.get(
                node.decision.constraint_fact_key,
                node.decision.constraint_fact_key,
            )
    return changes


def normalize_blueprint_agency_continuity(
    blueprint: NarrativeBlueprint,
) -> int:
    """Project unresolved coercion forward until its fact is explicitly released."""
    active_constraints: dict[str, tuple[str, str]] = {}
    release_nodes: defaultdict[str, set[str]] = defaultdict(set)
    changes = 0
    for node in blueprint.nodes:
        decision = node.decision
        if (
            decision is not None
            and decision.agency_mode in {"coerced", "incapacitated"}
            and decision.constraint_fact_key
        ):
            constraint_fact_key = decision.constraint_fact_key
            for state_change in node.state_changes:
                filtered = [
                    fact_key
                    for fact_key in state_change.supersedes_fact_keys
                    if fact_key != constraint_fact_key
                ]
                if filtered != state_change.supersedes_fact_keys:
                    state_change.supersedes_fact_keys = filtered
                    changes += 1
            filtered_releases = [
                value
                for value in node.released_constraints_for
                if value not in {
                    decision.actor_key,
                    constraint_fact_key,
                }
            ]
            if filtered_releases != node.released_constraints_for:
                node.released_constraints_for = filtered_releases
                changes += 1
        released_values = set(node.released_constraints_for)
        for actor_key, (fact_key, _mode) in list(
            active_constraints.items()
        ):
            fact_released = any(
                fact_key in change.supersedes_fact_keys
                for change in node.state_changes
            )
            if (
                fact_released
                and (
                    actor_key in released_values
                    or fact_key in released_values
                )
            ):
                active_constraints.pop(actor_key, None)
                release_nodes[actor_key].add(node.key)

        if decision is None:
            continue
        valid_release_keys = [
            key
            for key in decision.constraint_release_node_keys
            if key in release_nodes[decision.actor_key]
        ]
        if valid_release_keys != decision.constraint_release_node_keys:
            decision.constraint_release_node_keys = valid_release_keys
            changes += 1
        active = active_constraints.get(decision.actor_key)
        if active is not None and decision.agency_mode == "voluntary":
            constraint_fact_key, agency_mode = active
            decision.agency_mode = agency_mode
            decision.constraint_fact_key = constraint_fact_key
            decision.narrative_attribution = "external_coercion"
            decision.agency_change_reason = (
                "程序继承尚未解除的约束事实，禁止提前恢复自主"
            )
            changes += 1
        if (
            decision.agency_mode in {"coerced", "incapacitated"}
            and decision.constraint_fact_key
        ):
            active_constraints[decision.actor_key] = (
                decision.constraint_fact_key,
                decision.agency_mode,
            )
    return changes


def validate_blueprint_semantic_review(
    review: BlueprintSemanticReview,
    blueprint: NarrativeBlueprint,
    source_text: str,
) -> list[str]:
    errors: list[str] = []
    node_keys = {node.key for node in blueprint.nodes}
    source_ids = {
        segment.segment_id
        for segment in index_source_segments(source_text)
    }
    for index, issue in enumerate(review.issues, start=1):
        if not issue.node_keys:
            errors.append(
                f"[BLUEPRINT_REVIEW_NODE_REQUIRED] issue {index} 没有节点"
            )
        unknown_nodes = set(issue.node_keys) - node_keys
        if unknown_nodes:
            errors.append(
                f"[BLUEPRINT_REVIEW_NODE_UNKNOWN] issue {index} 引用未知节点："
                + "、".join(sorted(unknown_nodes))
            )
        unknown_sources = set(issue.source_segment_ids) - source_ids
        if unknown_sources:
            errors.append(
                f"[BLUEPRINT_REVIEW_SOURCE_UNKNOWN] issue {index} "
                "引用未知来源："
                + "、".join(sorted(unknown_sources))
            )
    return errors


def blueprint_semantic_issue_is_resolved(
    issue: BlueprintSemanticIssue,
    blueprint: NarrativeBlueprint,
) -> bool:
    """Recognize a structurally completed setup bridge despite stale review text."""
    if issue.code != "setup_missing" or not issue.node_keys:
        return False
    nodes = {node.key: node for node in blueprint.nodes}
    targets = [nodes.get(key) for key in issue.node_keys]
    if any(node is None for node in targets):
        return False
    issue_sources = set(issue.source_segment_ids)
    return all(
        node is not None
        and node.adaptation_kind == "logic_bridge"
        and bool(node.bridge_rationale.strip())
        and bool(
            node.transition_cue.strip()
            or node.opening_image.strip()
        )
        and (
            not issue_sources
            or issue_sources.issubset(node.source_segment_ids)
        )
        for node in targets
    )


def blueprint_semantic_voice_issue_has_dialogue_authority(
    issue: BlueprintSemanticIssue,
    blueprint: NarrativeBlueprint,
    source_text: str,
) -> bool:
    """Require exact deterministic support for contract-shaped findings."""
    if issue.code.startswith(("voice_identity_", "source_delivery_")):
        candidate_issues = blueprint_voice_identity_issues(
            blueprint,
            source_text,
        )
    elif issue.code.startswith("state_subject_"):
        candidate_issues = blueprint_state_subject_issues(
            blueprint,
            source_text,
        )
    else:
        return True
    deterministic_issues = [
        deterministic_issue
        for deterministic_issue in candidate_issues
        if deterministic_issue.code == issue.code
    ]
    issue_node_keys = set(issue.node_keys)
    supported_node_keys = {
        node_key
        for deterministic_issue in deterministic_issues
        for node_key in deterministic_issue.node_keys
    }
    if (
        not issue_node_keys
        or not issue_node_keys.issubset(supported_node_keys)
    ):
        return False
    relevant_deterministic_issues = [
        deterministic_issue
        for deterministic_issue in deterministic_issues
        if issue_node_keys.intersection(deterministic_issue.node_keys)
    ]
    supported_source_ids = {
        source_id
        for deterministic_issue in relevant_deterministic_issues
        for source_id in deterministic_issue.source_segment_ids
    }
    return bool(issue.source_segment_ids) and set(
        issue.source_segment_ids
    ).issubset(supported_source_ids)


def filter_blueprint_semantic_review_voice_issues(
    review: BlueprintSemanticReview,
    blueprint: NarrativeBlueprint,
    source_text: str,
) -> int:
    """Drop unsupported delivery/subject guesses before reviewer consensus."""
    retained = [
        issue
        for issue in review.issues
        if blueprint_semantic_voice_issue_has_dialogue_authority(
            issue,
            blueprint,
            source_text,
        )
    ]
    removed = len(review.issues) - len(retained)
    review.issues = retained
    return removed


AUDIBLE_SOURCE_DELIVERY_MODES = {
    "spoken_dialogue",
    "offscreen_voice",
}


def effective_source_unit_deliveries(
    node: NarrativeNode,
) -> list[NarrativeSourceUnitDelivery]:
    """Return explicit delivery decisions plus exact legacy voice bindings."""
    deliveries = [
        item.model_copy(deep=True)
        for item in node.source_unit_deliveries
    ]
    explicit_keys = {
        item.source_unit_key for item in deliveries
    }
    for evidence in node.participant_evidence:
        if evidence.usage != "voice":
            continue
        for key in evidence.source_unit_keys:
            if key in explicit_keys:
                continue
            deliveries.append(NarrativeSourceUnitDelivery(
                source_unit_key=key,
                mode="spoken_dialogue",
                content_owner_key=evidence.identity_key,
                performer_key=evidence.identity_key,
            ))
    return deliveries


def blueprint_voice_identity_issues(
    blueprint: NarrativeBlueprint,
    source_text: str,
) -> list[BlueprintSemanticIssue]:
    """Validate quoted-unit delivery and its exact performer identity."""
    facts = source_facts(source_text)
    facts_by_key = {fact.source_unit_key: fact for fact in facts}
    quoted_by_source: defaultdict[str, list[SourceFact]] = defaultdict(list)
    for fact in facts:
        if fact.projection == "quoted":
            quoted_by_source[fact.source_segment_id].append(fact)

    issues: list[BlueprintSemanticIssue] = []
    for node in blueprint.nodes:
        if node.source_semantics().projection_policy != "picture":
            if node.environment_source_unit_keys:
                issues.append(BlueprintSemanticIssue(
                    code="state_subject_environment_non_picture",
                    node_keys=[node.key],
                    source_segment_ids=list(node.source_segment_ids),
                    message=(
                        "paratext/audit-only node 不得携带 "
                        "environment_source_unit_keys"
                    ),
                    required_resolution=(
                        "移除非画面节点的 environment 主体标记；"
                        "其 source ownership 与顺序保持不变"
                    ),
                ))
            continue
        owned_sources = set(node.source_segment_ids)
        owned_quotes = [
            fact
            for source_id in node.source_segment_ids
            for fact in quoted_by_source.get(source_id, [])
        ]
        deliveries: defaultdict[
            str,
            list[NarrativeSourceUnitDelivery],
        ] = defaultdict(list)
        for delivery in effective_source_unit_deliveries(node):
            fact = facts_by_key.get(delivery.source_unit_key)
            if (
                fact is None
                or fact.projection != "quoted"
                or fact.source_segment_id not in owned_sources
            ):
                issues.append(BlueprintSemanticIssue(
                    code="source_delivery_conflict",
                    node_keys=[node.key],
                    source_segment_ids=list(node.source_segment_ids),
                    message=(
                        f"{delivery.source_unit_key} 不是本节点拥有的 "
                        "quoted source unit"
                    ),
                    required_resolution=(
                        "仅为本节点实际拥有的 quoted source unit "
                        "声明交付方式"
                    ),
                ))
                continue
            deliveries[delivery.source_unit_key].append(delivery)

        claims: defaultdict[
            str,
            list[NarrativeParticipantEvidence],
        ] = defaultdict(list)
        for evidence in node.participant_evidence:
            if evidence.usage != "voice":
                continue
            effective_source_ids = list(
                evidence.source_segment_ids or node.source_segment_ids
            )
            effective_source_set = set(effective_source_ids)
            invalid_keys = [
                key
                for key in evidence.source_unit_keys
                if (
                    key not in facts_by_key
                    or facts_by_key[key].projection != "quoted"
                    or facts_by_key[key].source_segment_id
                    not in owned_sources
                    or facts_by_key[key].source_segment_id
                    not in effective_source_set
                )
            ]
            if invalid_keys:
                issues.append(BlueprintSemanticIssue(
                    code="voice_identity_conflict",
                    node_keys=[node.key],
                    source_segment_ids=list(
                        evidence.source_segment_ids
                    ),
                    message=(
                        f"{evidence.identity_key} 的 voice evidence 引用了"
                        "非本节点 quoted source unit："
                        + "、".join(invalid_keys)
                    ),
                    required_resolution=(
                        "保留节点、来源 ownership 与语义，只把 voice "
                        "evidence 绑定到本节点实际拥有的 dialogue unit"
                    ),
                ))
                continue
            segment_scoped_non_dialogue_voice = (
                bool(effective_source_ids)
                and effective_source_set.issubset(owned_sources)
                and not any(
                    quoted_by_source.get(source_id, [])
                    for source_id in effective_source_ids
                )
            )
            if (
                not evidence.source_unit_keys
                and segment_scoped_non_dialogue_voice
            ):
                # A segment-scoped offscreen voice can be valid evidence for
                # an audible action even when the source has no dialogue unit.
                continue
            target_keys = evidence.source_unit_keys
            if not target_keys:
                issues.append(BlueprintSemanticIssue(
                    code="voice_identity_conflict",
                    node_keys=[node.key],
                    source_segment_ids=list(evidence.source_segment_ids),
                    message=(
                        f"{evidence.identity_key} 的 voice evidence 缺少 "
                        "source_unit_keys，没有绑定本节点 "
                        "quoted source unit"
                    ),
                    required_resolution=(
                        "保留节点、来源 ownership 与语义，为该 voice evidence "
                        "填写精确 source_unit_keys"
                    ),
                ))
                continue
            for key in dict.fromkeys(target_keys):
                claims[key].append(evidence)

        participant_keys = set(node.participants)
        for fact in owned_quotes:
            unit_deliveries = deliveries.get(fact.source_unit_key, [])
            if not unit_deliveries:
                issues.append(BlueprintSemanticIssue(
                    code="source_delivery_missing",
                    node_keys=[node.key],
                    source_segment_ids=[fact.source_segment_id],
                    message=(
                        f"{fact.source_unit_key} 缺少 quoted source unit "
                        "交付决策"
                    ),
                    required_resolution=(
                        "根据来源语义显式选择声音、书面文字、声音效果或"
                        "非口播引用；引号本身不得自动等同对白"
                    ),
                ))
                continue
            if len(unit_deliveries) != 1:
                issues.append(BlueprintSemanticIssue(
                    code="source_delivery_conflict",
                    node_keys=[node.key],
                    source_segment_ids=[fact.source_segment_id],
                    message=(
                        f"{fact.source_unit_key} 同时声明多个交付决策"
                    ),
                    required_resolution=(
                        "每个 quoted source unit 只保留一个交付决策"
                    ),
                ))
                continue
            delivery = unit_deliveries[0]
            referenced_delivery_identities = {
                delivery.performer_key
            } if delivery.performer_key else set()
            unknown_delivery_identities = (
                referenced_delivery_identities - participant_keys
            )
            if unknown_delivery_identities:
                issues.append(BlueprintSemanticIssue(
                    code="source_delivery_identity_conflict",
                    node_keys=[node.key],
                    source_segment_ids=[fact.source_segment_id],
                    message=(
                        f"{fact.source_unit_key} 的表演身份未列入 "
                        f"participants：{sorted(unknown_delivery_identities)}"
                    ),
                    required_resolution=(
                        "声音 delivery 的 performer_key 必须精确引用"
                        "本节点参与者；content_owner_key 可以是文字或物件归属"
                    ),
                ))

            unit_claims = claims.get(fact.source_unit_key, [])
            identities = {
                evidence.identity_key
                for evidence in unit_claims
                if evidence.identity_key
            }
            if delivery.mode not in AUDIBLE_SOURCE_DELIVERY_MODES:
                if unit_claims:
                    issues.append(BlueprintSemanticIssue(
                        code="voice_identity_conflict",
                        node_keys=[node.key],
                        source_segment_ids=[fact.source_segment_id],
                        message=(
                            f"{fact.source_unit_key} 的 delivery mode="
                            f"{delivery.mode}，不得声明 voice performer"
                        ),
                        required_resolution=(
                            "移除非声音交付上的 voice evidence，保留内容归属"
                        ),
                    ))
                continue
            if not unit_claims:
                code = "voice_identity_missing"
                message = (
                    f"{fact.source_unit_key} 缺少结构化 voice performer identity evidence"
                )
            elif len(unit_claims) > 1 and len(identities) > 1:
                code = "voice_identity_ambiguous"
                message = (
                    f"{fact.source_unit_key} 同时声明多个 voice identity："
                    + "、".join(sorted(identities))
                )
            elif len(unit_claims) != 1 or len(identities) != 1:
                code = "voice_identity_conflict"
                message = (
                    f"{fact.source_unit_key} 必须恰有一个非空 voice identity evidence"
                )
            elif next(iter(identities)) != delivery.performer_key:
                code = "voice_identity_conflict"
                message = (
                    f"{fact.source_unit_key} 的 voice identity 与 "
                    "performer_key 不一致"
                )
            else:
                continue
            issues.append(BlueprintSemanticIssue(
                code=code,
                node_keys=[node.key],
                source_segment_ids=[fact.source_segment_id],
                message=message,
                required_resolution=(
                    "保持完整 node、source ownership、来源顺序和语义三元不变；"
                    "仅为声音交付的 quoted source unit 提供恰一个 voice evidence，"
                    "identity_key 使用人物 registry 的 typed reference"
                ),
            ))
    return issues


def blueprint_state_subject_issues(
    blueprint: NarrativeBlueprint,
    source_text: str,
) -> list[BlueprintSemanticIssue]:
    """Require one typed owner for every prose unit before scene generation."""
    facts = source_facts(source_text)
    facts_by_key = {fact.source_unit_key: fact for fact in facts}
    issues: list[BlueprintSemanticIssue] = []
    for node in blueprint.nodes:
        if node.source_semantics().projection_policy != "picture":
            continue
        owned_sources = set(node.source_segment_ids)
        action_facts = [
            fact for fact in facts
            if (
                fact.projection == "action"
                and fact.source_segment_id in owned_sources
            )
        ]
        environment_keys = list(node.environment_source_unit_keys)
        if len(environment_keys) != len(set(environment_keys)):
            issues.append(BlueprintSemanticIssue(
                code="state_subject_environment_duplicate",
                node_keys=[node.key],
                source_segment_ids=list(node.source_segment_ids),
                message="environment_source_unit_keys 含重复 source unit",
                required_resolution="每个环境 source unit 只能显式声明一次",
            ))
        invalid_environment_keys = [
            key for key in environment_keys
            if (
                key not in facts_by_key
                or facts_by_key[key].projection != "action"
                or facts_by_key[key].source_segment_id not in owned_sources
            )
        ]
        if invalid_environment_keys:
            issues.append(BlueprintSemanticIssue(
                code="state_subject_environment_invalid",
                node_keys=[node.key],
                source_segment_ids=list(node.source_segment_ids),
                message=(
                    "environment_source_unit_keys 引用非本节点 prose unit："
                    + "、".join(invalid_environment_keys)
                ),
                required_resolution="只标记本节点拥有的 prose/action source unit",
            ))

        claims_by_unit: defaultdict[
            str, list[NarrativeParticipantEvidence]
        ] = defaultdict(list)
        for evidence in node.participant_evidence:
            if evidence.usage != "state_subject":
                continue
            if not evidence.source_unit_keys:
                issues.append(BlueprintSemanticIssue(
                    code="state_subject_unit_missing",
                    node_keys=[node.key],
                    source_segment_ids=list(evidence.source_segment_ids),
                    message=(
                        f"{evidence.identity_key} 的 state_subject evidence "
                        "缺少精确 source_unit_keys"
                    ),
                    required_resolution=(
                        "把状态主体绑定到本节点具体 prose source unit"
                    ),
                ))
                continue
            for key in evidence.source_unit_keys:
                fact = facts_by_key.get(key)
                if (
                    fact is None
                    or fact.projection != "action"
                    or fact.source_segment_id not in owned_sources
                    or fact.source_segment_id not in evidence.source_segment_ids
                    or bool(
                        set(evidence.source_segment_ids) - owned_sources
                    )
                ):
                    issues.append(BlueprintSemanticIssue(
                        code="state_subject_unit_invalid",
                        node_keys=[node.key],
                        source_segment_ids=list(evidence.source_segment_ids),
                        message=(
                            f"{evidence.identity_key} 的 state_subject "
                            f"引用非本节点 prose unit {key}"
                        ),
                        required_resolution=(
                            "只绑定本节点拥有的 prose/action source unit"
                        ),
                    ))
                    continue
                claims_by_unit[key].append(evidence)

        assignments_by_unit: defaultdict[
            str, list[NarrativeStateSubjectAssignment]
        ] = defaultdict(list)
        for assignment in node.state_subject_assignments:
            fact = facts_by_key.get(assignment.source_unit_key)
            invalid_identities = (
                set(assignment.identity_keys) - set(node.participants)
            )
            if (
                fact is None
                or fact.projection != "action"
                or fact.source_segment_id not in owned_sources
                or invalid_identities
            ):
                issues.append(BlueprintSemanticIssue(
                    code="state_subject_assignment_invalid",
                    node_keys=[node.key],
                    source_segment_ids=list(node.source_segment_ids),
                    message=(
                        f"{assignment.source_unit_key} 的 joint state subject "
                        "引用非本节点 action unit 或非 participants identity"
                    ),
                    required_resolution=(
                        "joint assignment 只绑定本节点 action unit，"
                        "identity_keys 必须是有来源证据的 participants"
                    ),
                ))
                continue
            assignments_by_unit[assignment.source_unit_key].append(assignment)

        for fact in action_facts:
            claims = claims_by_unit.get(fact.source_unit_key, [])
            assignments = assignments_by_unit.get(fact.source_unit_key, [])
            explicit = list(dict.fromkeys(
                evidence.identity_key
                for evidence in claims
                if evidence.usage == "state_subject"
            ))
            environment = fact.source_unit_key in environment_keys
            if len(assignments) > 1:
                issues.append(BlueprintSemanticIssue(
                    code="state_subject_assignment_ambiguous",
                    node_keys=[node.key],
                    source_segment_ids=[fact.source_segment_id],
                    message=(
                        f"{fact.source_unit_key} 存在多个 joint state subject "
                        "assignment"
                    ),
                    required_resolution="每个共同动作 unit 只能有一条 joint assignment",
                ))
            elif environment and (explicit or assignments):
                issues.append(BlueprintSemanticIssue(
                    code="state_subject_environment_conflict",
                    node_keys=[node.key],
                    source_segment_ids=[fact.source_segment_id],
                    message=(
                        f"{fact.source_unit_key} 同时声明人物主体与 environment"
                    ),
                    required_resolution="人物主体和纯环境标记必须二选一",
                ))
            elif assignments and claims:
                issues.append(BlueprintSemanticIssue(
                    code="state_subject_assignment_conflict",
                    node_keys=[node.key],
                    source_segment_ids=[fact.source_segment_id],
                    message=(
                        f"{fact.source_unit_key} 同时声明 single 与 joint "
                        "state subject"
                    ),
                    required_resolution=(
                        "可拆单主体动作使用唯一 state_subject；"
                        "结构上不可拆的共同动作仅使用 joint assignment"
                    ),
                ))
            elif len(claims) > 1:
                issues.append(BlueprintSemanticIssue(
                    code="state_subject_ambiguous",
                    node_keys=[node.key],
                    source_segment_ids=[fact.source_segment_id],
                    message=(
                        f"{fact.source_unit_key} 存在多个候选状态主体："
                        + "、".join(
                            evidence.identity_key for evidence in claims
                        )
                    ),
                    required_resolution=(
                        "仅修此报错 unit：可拆动作保留唯一 "
                        "usage=state_subject evidence；结构切分后仍不可拆的"
                        "共同动作移除该 unit 的全部 single state_subject claims，"
                        "建立唯一 mode=joint assignment，identity_keys 列出全部"
                        "有来源共同主体且至少 2 个；其他 unit ownership 不得变化"
                    ),
                ))
            elif not explicit and not assignments and not environment:
                issues.append(BlueprintSemanticIssue(
                    code="state_subject_missing",
                    node_keys=[node.key],
                    source_segment_ids=[fact.source_segment_id],
                    message=f"{fact.source_unit_key} 缺少结构化状态主体",
                    required_resolution=(
                        "人物思考/动作/反应填唯一 state_subject evidence；"
                        "结构上不可拆的共同动作填唯一 joint assignment；"
                        "真正无人物的环境单元填 "
                        "environment_source_unit_keys"
                    ),
                ))
    return issues


def normalize_blueprint_source_order(
    blueprint: NarrativeBlueprint,
    source_text: str,
) -> int:
    """Restore authoritative source order after independent node replacements."""
    source_order = {
        segment.segment_id: index
        for index, segment in enumerate(index_source_segments(source_text))
    }
    ranked_nodes: list[tuple[int, int, NarrativeNode]] = []
    for original_index, node in enumerate(blueprint.nodes):
        positions = [
            source_order[source_id]
            for source_id in node.source_segment_ids
            if source_id in source_order
        ]
        if not positions or len(positions) != len(node.source_segment_ids):
            return 0
        ranked_nodes.append((min(positions), original_index, node))
    ordered_nodes = [
        node
        for _source_position, _original_index, node
        in sorted(ranked_nodes)
    ]
    moved = sum(
        node is not blueprint.nodes[index]
        for index, node in enumerate(ordered_nodes)
    )
    if moved:
        blueprint.nodes = ordered_nodes
    return moved


def validate_narrative_blueprint_patch_projection(
    patch: NarrativeBlueprintPatch,
    blueprint: NarrativeBlueprint,
) -> list[str]:
    """Keep repair inside the canonical timeline and source authority."""
    node_map = {node.key: node for node in blueprint.nodes}
    errors: list[str] = []
    replacement_keys = [
        replacement.node_key for replacement in patch.replacements
    ]
    if len(replacement_keys) != len(set(replacement_keys)):
        errors.append(
            "[BLUEPRINT_PATCH_NODE_DUPLICATE] "
            "同一 canonical node 不得重复替换"
        )
    if patch.delete_node_keys:
        errors.append(
            "[BLUEPRINT_PATCH_TIMELINE_DELETE] "
            "repair 不得删除 canonical timeline node"
        )
    for replacement in patch.replacements:
        original = node_map.get(replacement.node_key)
        if original is None:
            errors.append(
                "[BLUEPRINT_PATCH_NODE_UNKNOWN] "
                f"{replacement.node_key} 不在修复窗口"
            )
            continue
        if len(replacement.nodes) != 1:
            errors.append(
                "[BLUEPRINT_PATCH_TIMELINE_CARDINALITY] "
                f"{replacement.node_key} 必须一对一替换"
            )
            continue
        for node in replacement.nodes:
            if node.key != original.key:
                errors.append(
                    "[BLUEPRINT_PATCH_NODE_IDENTITY_CHANGE] "
                    f"{replacement.node_key} 不得改写 canonical key"
                )
            if node.source_segment_ids != original.source_segment_ids:
                errors.append(
                    "[BLUEPRINT_PATCH_SOURCE_OWNERSHIP_CHANGE] "
                    f"{replacement.node_key} 必须保持完整有序来源 ownership"
                )
            expected_semantics = (
                original.narrative_layer,
                original.event_priority,
                original.render_policy,
            )
            actual_semantics = (
                node.narrative_layer,
                node.event_priority,
                node.render_policy,
            )
            if actual_semantics != expected_semantics:
                errors.append(
                    "[BLUEPRINT_PATCH_SOURCE_SEMANTICS_CHANGE] "
                    f"{replacement.node_key} 必须保持来源语义三元"
                )
    return list(dict.fromkeys(errors))


def apply_narrative_blueprint_patch(
    blueprint: NarrativeBlueprint,
    patch: NarrativeBlueprintPatch,
    *,
    allow_source_expansion: bool = False,
    source_text: str | None = None,
) -> int:
    canonical_contract = [
        (
            node.key,
            tuple(node.source_segment_ids),
            node.narrative_layer,
            node.event_priority,
            node.render_policy,
        )
        for node in blueprint.nodes
    ]
    projection_errors = validate_narrative_blueprint_patch_projection(
        patch,
        blueprint,
    )
    if projection_errors:
        raise ValueError("；".join(projection_errors))
    original_keys = {node.key for node in blueprint.nodes}
    normalized_replacements: list[NarrativeNodeReplacement] = []
    replacement_by_target: dict[str, NarrativeNodeReplacement] = {}
    for replacement in patch.replacements:
        target_key = replacement.node_key
        if target_key not in original_keys:
            replacement_sources = {
                source_id
                for node in replacement.nodes
                for source_id in node.source_segment_ids
            }
            scored_targets: list[tuple[float, str]] = []
            for node in blueprint.nodes:
                original_sources = set(node.source_segment_ids)
                overlap = replacement_sources.intersection(
                    original_sources
                )
                union = replacement_sources.union(original_sources)
                if overlap and union:
                    scored_targets.append((
                        len(overlap) / len(union),
                        node.key,
                    ))
            best_score = max(
                (score for score, _key in scored_targets),
                default=0.0,
            )
            best_keys = [
                key
                for score, key in scored_targets
                if score == best_score and score >= 0.5
            ]
            if len(best_keys) != 1:
                raise ValueError(
                    "蓝图局部修复引用未知节点且无法按来源唯一重绑定："
                    f"{target_key}"
                )
            target_key = best_keys[0]
        existing = replacement_by_target.get(target_key)
        if existing is not None:
            existing.nodes.extend(replacement.nodes)
            continue
        replacement.node_key = target_key
        replacement_by_target[target_key] = replacement
        normalized_replacements.append(replacement)
    patch.replacements = normalized_replacements
    replacements = {
        replacement.node_key: replacement
        for replacement in patch.replacements
    }
    # Replacing a node already removes the original. Models occasionally also
    # list that key under delete_node_keys; replacement is the more specific
    # instruction and must win or the repaired source span disappears.
    delete_node_keys = set(patch.delete_node_keys) - set(replacements)
    delete_node_keys.intersection_update(original_keys)
    reserved_fact_keys = {
        change.fact_key
        for node in blueprint.nodes
        if (
            node.key not in replacements
            and node.key not in delete_node_keys
        )
        for change in node.state_changes
    }
    facts_by_key = {
        change.fact_key: change
        for node in blueprint.nodes
        for change in node.state_changes
    }
    removed_fact_keys = {
        change.fact_key
        for node in blueprint.nodes
        if node.key in replacements or node.key in delete_node_keys
        for change in node.state_changes
    }
    constraint_actor_by_fact = {
        node.decision.constraint_fact_key: node.decision.actor_key
        for node in blueprint.nodes
        if (
            node.decision is not None
            and node.decision.constraint_fact_key
        )
    }
    fact_key_renames: dict[str, str] = {}
    for replacement in patch.replacements:
        for replacement_node in replacement.nodes:
            if not replacement_node.transition_cue.strip():
                replacement_node.transition_cue = (
                    replacement_node.opening_image.strip()
                    or replacement_node.action_logic.strip()
                )
            for change_index, change in enumerate(
                replacement_node.state_changes,
                start=1,
            ):
                explicit_releases = set(
                    replacement_node.released_constraints_for
                )
                change.supersedes_fact_keys = [
                    fact_key
                    for fact_key in change.supersedes_fact_keys
                    if (
                        fact_key not in removed_fact_keys
                        and (
                            fact_key not in facts_by_key
                            or facts_by_key[fact_key].state_key
                            == change.state_key
                            or fact_key in explicit_releases
                            or constraint_actor_by_fact.get(fact_key)
                            in explicit_releases
                        )
                    )
                ]
                if change.fact_key in reserved_fact_keys:
                    original_key = change.fact_key
                    new_key = (
                        f"repair-{replacement_node.key}-{change_index}"
                    )
                    while new_key in reserved_fact_keys:
                        new_key += "x"
                    fact_key_renames[original_key] = new_key
                    change.fact_key = new_key
                reserved_fact_keys.add(change.fact_key)
    if fact_key_renames:
        for replacement in patch.replacements:
            for node in replacement.nodes:
                for requirement in node.state_requirements:
                    requirement.required_fact_key = fact_key_renames.get(
                        requirement.required_fact_key,
                        requirement.required_fact_key,
                    )
                for change in node.state_changes:
                    change.supersedes_fact_keys = [
                        fact_key_renames.get(fact_key, fact_key)
                        for fact_key in change.supersedes_fact_keys
                    ]
                node.released_constraints_for = [
                    fact_key_renames.get(value, value)
                    for value in node.released_constraints_for
                ]
                if node.decision is not None:
                    node.decision.constraint_fact_key = (
                        fact_key_renames.get(
                            node.decision.constraint_fact_key,
                            node.decision.constraint_fact_key,
                        )
                    )
    changed = 0
    rebuilt_nodes: list[NarrativeNode] = []
    existing_keys = {
        node.key
        for node in blueprint.nodes
        if (
            node.key not in replacements
            and node.key not in delete_node_keys
        )
    }
    for node in blueprint.nodes:
        if node.key in delete_node_keys:
            changed += 1
            continue
        replacement = replacements.get(node.key)
        if replacement is None:
            rebuilt_nodes.append(node)
            continue
        replacement_source_ids = {
            source_id
            for replacement_node in replacement.nodes
            for source_id in replacement_node.source_segment_ids
        }
        if (
            not replacement_source_ids
            or (
                not allow_source_expansion
                and not replacement_source_ids.issubset(
                    set(node.source_segment_ids),
                )
            )
        ):
            rebuilt_nodes.append(node)
            continue
        for replacement_node in replacement.nodes:
            if replacement_node.key in existing_keys:
                raise ValueError(
                    f"蓝图局部修复产生重复节点 key："
                    f"{replacement_node.key}"
                )
            existing_keys.add(replacement_node.key)
            rebuilt_nodes.append(replacement_node)
        changed += 1
    blueprint.nodes = rebuilt_nodes
    replacement_key_map = {
        old_key: replacement.nodes[0].key
        for old_key, replacement in replacements.items()
        if replacement.nodes and replacement.nodes[0].key != old_key
    }
    if replacement_key_map:
        for rebuilt_node in blueprint.nodes:
            if rebuilt_node.decision is None:
                continue
            rebuilt_node.decision.setup_node_keys = [
                replacement_key_map.get(node_key, node_key)
                for node_key in rebuilt_node.decision.setup_node_keys
            ]
            rebuilt_node.decision.constraint_release_node_keys = [
                replacement_key_map.get(node_key, node_key)
                for node_key
                in rebuilt_node.decision.constraint_release_node_keys
            ]
    normalize_blueprint_fact_versions(blueprint)
    repaired_contract = [
        (
            node.key,
            tuple(node.source_segment_ids),
            node.narrative_layer,
            node.event_priority,
            node.render_policy,
        )
        for node in blueprint.nodes
    ]
    if repaired_contract != canonical_contract:
        raise ValueError(
            "[BLUEPRINT_PATCH_CANONICAL_TIMELINE_CHANGE] repair 前后 "
            "timeline key、顺序、source ownership 与语义三元必须完全一致"
        )
    derive_blueprint_scene_plans(blueprint)
    return changed


def derive_blueprint_scene_plans(
    blueprint: NarrativeBlueprint,
) -> list[BlueprintScenePlan]:
    def operational_participants(node: NarrativeNode) -> list[str]:
        if not node.participant_evidence and not node.state_subject_assignments:
            return [
                participant
                for participant in node.participants
                if participant
            ]
        return list(dict.fromkeys([
            evidence.identity_key
            for evidence in node.participant_evidence
            if (
                evidence.identity_key
                and evidence.usage
                in {"visible", "voice", "state_subject"}
            )
        ] + [
            identity_key
            for assignment in node.state_subject_assignments
            for identity_key in assignment.identity_keys
            if identity_key
        ]))

    occurrence_nodes: defaultdict[str, list[str]] = defaultdict(list)
    occurrence_partitions: defaultdict[str, set[str]] = defaultdict(set)
    for node in blueprint.nodes:
        projection_policy = node.source_semantics().projection_policy
        for source_id in node.source_segment_ids:
            occurrence_nodes[source_id].append(node.key)
            occurrence_partitions[source_id].add(projection_policy)
    partition_conflicts = {
        source_id
        for source_id, partitions in occurrence_partitions.items()
        if len(partitions) > 1
    }
    if partition_conflicts:
        raise BlueprintSourceOccurrenceError(
            {
                source_id: occurrence_nodes[source_id]
                for source_id in partition_conflicts
            },
            partition_conflicts=partition_conflicts,
        )

    source_semantics: dict[str, BlueprintSourceSemantics] = {}
    for node in blueprint.nodes:
        semantics = node.source_semantics()
        for source_id in node.source_segment_ids:
            existing = source_semantics.get(source_id)
            if existing is not None and existing != semantics:
                raise ValueError(
                    "[BLUEPRINT_SOURCE_SEMANTIC_CONFLICT] "
                    f"{source_id} 被赋予互相冲突的叙事语义"
                )
            source_semantics[source_id] = semantics

    picture_nodes = [
        node
        for node in blueprint.nodes
        if node.source_semantics().projection_policy == "picture"
    ]
    groups: list[list[NarrativeNode]] = []
    for node in picture_nodes:
        previous = groups[-1][-1] if groups else None
        current_group = groups[-1] if groups else []
        starts_scene = (
            previous is None
            or node.scene_boundary_before
            or node.temporal_domain_key != previous.temporal_domain_key
            or node.location_key != previous.location_key
            or node.time_relation in {
                "elapsed",
                "jump",
                "flashback_enter",
                "flashback_exit",
                "montage",
            }
            or sum(item.dramatic_load for item in current_group)
            + node.dramatic_load > 3
            or len({
                source_id
                for item in current_group
                for source_id in item.source_segment_ids
            } | set(node.source_segment_ids)) > 8
        )
        if starts_scene:
            groups.append([node])
        else:
            groups[-1].append(node)

    plans: list[BlueprintScenePlan] = []
    for index, nodes in enumerate(groups, start=1):
        first = nodes[0]
        previous_exit_state = (
            groups[index - 2][-1].exit_state
            or groups[index - 2][-1].summary
            if index > 1
            else ""
        )
        plans.append(BlueprintScenePlan(
            key=f"bp-sc{index:03d}",
            node_keys=[node.key for node in nodes],
            source_segment_ids=[],
            source_semantics={},
            temporal_domain_key=first.temporal_domain_key,
            time_label=first.time_label,
            location_key=first.location_key,
            location_label=first.location_label,
            transition_cue=first.transition_cue,
            previous_scene_exit_state=previous_exit_state,
            opening_image=(
                first.opening_image
                or first.transition_cue
                or first.summary
            ),
            exit_state=nodes[-1].exit_state or nodes[-1].summary,
            dramatic_load=sum(node.dramatic_load for node in nodes),
            agency_contracts=[
                {
                    "node_key": node.key,
                    "actor_key": node.decision.actor_key,
                    "agency_mode": node.decision.agency_mode,
                    "narrative_attribution": (
                        node.decision.narrative_attribution
                    ),
                    "constraint_fact_key": (
                        node.decision.constraint_fact_key
                    ),
                }
                for node in nodes
                if node.decision is not None
            ],
            participant_keys=list(dict.fromkeys(
                participant
                for node in nodes
                for participant in operational_participants(node)
            )),
            scene_heading=(
                f"【场{index}】{first.time_label} / {first.location_label}"
            ),
        ))

    node_scene_owners: dict[str, str] = {}
    source_scene_owners: dict[str, str] = {}
    conflicts: dict[str, list[str]] = {}
    for plan, nodes in zip(plans, groups, strict=True):
        for node in nodes:
            node_scene_owners[node.key] = plan.key
            for source_id in node.source_segment_ids:
                current_owner = source_scene_owners.get(source_id)
                if current_owner is None:
                    source_scene_owners[source_id] = plan.key
                    continue
                if current_owner == plan.key:
                    continue
                scene_keys = conflicts.setdefault(
                    source_id,
                    [current_owner],
                )
                if plan.key not in scene_keys:
                    scene_keys.append(plan.key)
    if conflicts:
        raise BlueprintSourceOwnershipError(conflicts)

    occurrence_owners: defaultdict[str, list[str]] = defaultdict(list)
    for node in blueprint.nodes:
        for source_id in node.source_segment_ids:
            occurrence_owners[source_id].append(node.key)
    occurrence_duplicates = {
        source_id: node_keys
        for source_id, node_keys in occurrence_owners.items()
        if len(node_keys) > 1
    }
    if occurrence_duplicates:
        raise BlueprintSourceOccurrenceError(occurrence_duplicates)

    for plan in plans:
        plan.source_segment_ids = [
            source_id
            for source_id, owner_scene_key in source_scene_owners.items()
            if owner_scene_key == plan.key
        ]
        plan.source_semantics = {
            source_id: source_semantics[source_id]
            for source_id in plan.source_segment_ids
        }

    node_map = {node.key: node for node in blueprint.nodes}
    derivations: list[BlueprintSceneDerivation] = []

    def append_derivation(
        relation_type: str,
        source_node_key: str,
        target_node_key: str,
        *,
        reference_key: str = "",
        summary: str = "",
    ) -> None:
        source_scene_key = node_scene_owners.get(source_node_key)
        target_scene_key = node_scene_owners.get(target_node_key)
        if (
            not source_scene_key
            or not target_scene_key
            or source_scene_key == target_scene_key
        ):
            return
        derivations.append(BlueprintSceneDerivation(
            relation_key=f"BD{len(derivations) + 1:04d}",
            relation_type=relation_type,
            source_scene_plan_key=source_scene_key,
            target_scene_plan_key=target_scene_key,
            source_node_key=source_node_key,
            target_node_key=target_node_key,
            reference_key=reference_key,
            summary=summary,
        ))

    for previous_nodes, current_nodes in zip(groups, groups[1:]):
        append_derivation(
            "scene_transition",
            previous_nodes[-1].key,
            current_nodes[0].key,
            summary=(
                current_nodes[0].transition_cue
                or current_nodes[0].opening_image
                or current_nodes[0].summary
            ),
        )

    fact_owner_nodes = {
        change.fact_key: node.key
        for node in blueprint.nodes
        for change in node.state_changes
        if change.fact_key
    }
    for target_node in blueprint.nodes:
        for requirement in target_node.state_requirements:
            source_node_key = fact_owner_nodes.get(
                requirement.required_fact_key,
            )
            if source_node_key:
                append_derivation(
                    "state_requirement",
                    source_node_key,
                    target_node.key,
                    reference_key=requirement.required_fact_key,
                    summary=requirement.reason,
                )
        if target_node.decision is None:
            continue
        for source_node_key in target_node.decision.setup_node_keys:
            if source_node_key in node_map:
                append_derivation(
                    "decision_setup",
                    source_node_key,
                    target_node.key,
                    summary=target_node.decision.pressure,
                )
        for source_node_key in (
            target_node.decision.constraint_release_node_keys
        ):
            if source_node_key in node_map:
                append_derivation(
                    "constraint_release",
                    source_node_key,
                    target_node.key,
                    reference_key=target_node.decision.constraint_fact_key,
                    summary=target_node.decision.agency_change_reason,
                )

    blueprint.scene_plans = plans
    blueprint.source_scene_owners = source_scene_owners
    blueprint.source_semantics = source_semantics
    blueprint.source_audit_annotations = [
        BlueprintSourceAuditAnnotation(
            node_key=node.key,
            source_segment_ids=list(node.source_segment_ids),
        )
        for node in blueprint.nodes
        if node.source_semantics().projection_policy == "audit_only"
    ]
    blueprint.scene_derivations = derivations
    return plans


def validate_blueprint_scene_partition(
    blueprint: NarrativeBlueprint,
    plans: list[BlueprintScenePlan] | None = None,
) -> list[str]:
    """Validate the exact ordered picture-node partition."""
    current_plans = blueprint.scene_plans if plans is None else plans
    picture_node_keys = [
        node.key
        for node in blueprint.nodes
        if node.source_semantics().projection_policy == "picture"
    ]
    audit_node_keys = {
        node.key
        for node in blueprint.nodes
        if node.source_semantics().projection_policy == "audit_only"
    }
    planned_node_keys = [
        node_key
        for plan in current_plans
        for node_key in plan.node_keys
    ]
    errors: list[str] = []
    leaked_audit_keys = [
        node_key
        for node_key in planned_node_keys
        if node_key in audit_node_keys
    ]
    if leaked_audit_keys:
        errors.append(
            "[BLUEPRINT_AUDIT_NODE_IN_SCENE] audit_only 节点不得进入 scene plan："
            + "、".join(leaked_audit_keys)
        )
    if planned_node_keys != picture_node_keys:
        errors.append(
            "[BLUEPRINT_SCENE_PARTITION_INVALID] picture 节点必须被 scene plans "
            "精确覆盖并保持相对顺序，禁止重复或遗漏"
        )

    audit_source_ids = [
        source_id
        for node in blueprint.nodes
        if node.source_semantics().projection_policy == "audit_only"
        for source_id in node.source_segment_ids
    ]
    annotated_source_ids = [
        source_id
        for annotation in blueprint.source_audit_annotations
        for source_id in annotation.source_segment_ids
    ]
    annotated_node_keys = [
        annotation.node_key
        for annotation in blueprint.source_audit_annotations
    ]
    expected_audit_node_keys = [
        node.key
        for node in blueprint.nodes
        if node.source_semantics().projection_policy == "audit_only"
    ]
    if (
        annotated_source_ids != audit_source_ids
        or annotated_node_keys != expected_audit_node_keys
    ):
        errors.append(
            "[BLUEPRINT_AUDIT_COVERAGE_INVALID] source_audit_annotations "
            "必须精确覆盖 audit_only timeline nodes 与来源"
        )
    picture_source_ids = {
        source_id
        for plan in current_plans
        for source_id in plan.source_segment_ids
    }
    leaked_audit_sources = [
        source_id
        for source_id in audit_source_ids
        if source_id in picture_source_ids
    ]
    if leaked_audit_sources:
        errors.append(
            "[BLUEPRINT_AUDIT_SOURCE_IN_SCENE] audit_only 来源不得进入 scene plan："
            + "、".join(leaked_audit_sources)
        )
    return list(dict.fromkeys(errors))


def validate_narrative_blueprint(
    blueprint: NarrativeBlueprint,
    source_text: str,
) -> list[str]:
    errors: list[str] = []
    segments = index_source_segments(source_text)
    source_order = {
        segment.segment_id: index
        for index, segment in enumerate(segments)
    }
    expected_source_ids = {
        segment.segment_id for segment in segments
    } - structural_front_matter_ids(segments)

    if not blueprint.nodes:
        return ["[BLUEPRINT_EMPTY] 叙事蓝图没有任何时间线节点"]

    errors.extend(
        (
            f"[BLUEPRINT_{issue.code.upper()}] "
            f"{'、'.join(issue.node_keys)} "
            f"{'、'.join(issue.source_segment_ids)}：{issue.message}；"
            f"必须：{issue.required_resolution}"
        )
        for issue in blueprint_voice_identity_issues(
            blueprint,
            source_text,
        )
    )
    errors.extend(
        (
            f"[BLUEPRINT_{issue.code.upper()}] "
            f"{'、'.join(issue.node_keys)} "
            f"{'、'.join(issue.source_segment_ids)}：{issue.message}；"
            f"必须：{issue.required_resolution}"
        )
        for issue in blueprint_state_subject_issues(
            blueprint,
            source_text,
        )
    )

    node_keys = [node.key for node in blueprint.nodes]
    if len(node_keys) != len(set(node_keys)):
        errors.append("[BLUEPRINT_NODE_KEY_DUPLICATE] 时间线节点 key 重复")

    unknown_source_ids = {
        source_id
        for node in blueprint.nodes
        for source_id in node.source_segment_ids
        if source_id not in source_order
    }
    if unknown_source_ids:
        errors.append(
            "[BLUEPRINT_SOURCE_UNKNOWN] 节点引用未知来源段："
            + "、".join(sorted(unknown_source_ids)[:20])
        )

    errors.extend(
        issue.error
        for issue in blueprint_source_occurrence_issues(blueprint.nodes)
    )

    owned_source_ids = {
        source_id
        for node in blueprint.nodes
        for source_id in node.source_segment_ids
    }
    missing_source_ids = expected_source_ids - owned_source_ids
    if missing_source_ids:
        errors.append(
            "[BLUEPRINT_SOURCE_MISSING] 时间线漏掉原文段："
            + "、".join(sorted(missing_source_ids)[:20])
        )

    first_owner_positions: dict[str, int] = {}
    for node_position, node in enumerate(blueprint.nodes):
        if not node.source_segment_ids:
            errors.append(
                f"[BLUEPRINT_NODE_UNGROUNDED] {node.key} 没有来源段"
            )
            continue
        if (
            len(node.source_segment_ids)
            > BLUEPRINT_MAX_SOURCE_SEGMENTS_PER_NODE
        ):
            errors.append(
                f"[BLUEPRINT_NODE_OVERBROAD] {node.key} 合并了"
                f"{len(node.source_segment_ids)} 个来源段"
            )
        positions = [
            source_order[source_id]
            for source_id in node.source_segment_ids
            if source_id in source_order
        ]
        if positions != sorted(set(positions)):
            errors.append(
                f"[BLUEPRINT_SOURCE_ORDER] {node.key} 来源顺序错误或重复"
            )
        if positions and positions[-1] - positions[0] + 1 != len(positions):
            errors.append(
                f"[BLUEPRINT_SOURCE_DISCONTIGUOUS] {node.key} 合并非连续来源"
            )
        for source_id in node.source_segment_ids:
            first_owner_positions.setdefault(source_id, node_position)
        if not node.temporal_domain_key.strip() or not node.time_label.strip():
            errors.append(
                f"[BLUEPRINT_TIME_MISSING] {node.key} 缺少时间域或时间标签"
            )
        if not node.location_key.strip() or not node.location_label.strip():
            errors.append(
                f"[BLUEPRINT_LOCATION_MISSING] {node.key} 缺少单一地点"
            )
        elif re.search(
            r"[、+/]|内外",
            node.location_label,
        ):
            errors.append(
                f"[BLUEPRINT_LOCATION_COMPOSITE] {node.key} 把多个空间"
                f"合并为一个节点：{node.location_label}"
            )
        if (
            node.adaptation_kind == "logic_bridge"
            and len(node.bridge_rationale.strip()) < 8
        ):
            errors.append(
                f"[BLUEPRINT_BRIDGE_RATIONALE_MISSING] {node.key} 的"
                "逻辑补桥没有说明必要性和不改变原文结果的依据"
            )
        expected_semantics = (
            ("causal", "standalone")
            if node.narrative_layer == "story"
            else ("connective", "exclude_from_spine")
        )
        if (
            node.event_priority,
            node.render_policy,
        ) != expected_semantics:
            errors.append(
                f"[BLUEPRINT_NODE_SEMANTICS_INVALID] {node.key} 的"
                "叙事层、事件优先级与渲染策略不一致"
            )

    expected_positions = [
        first_owner_positions[source_id]
        for source_id in sorted(
            expected_source_ids,
            key=lambda source_id: source_order[source_id],
        )
        if source_id in first_owner_positions
    ]
    if expected_positions != sorted(expected_positions):
        ordered_source_ids = [
            source_id
            for source_id in sorted(
                expected_source_ids,
                key=lambda source_id: source_order[source_id],
            )
            if source_id in first_owner_positions
        ]
        inversion = next(
            (
                (previous_source_id, source_id)
                for previous_source_id, source_id in zip(
                    ordered_source_ids,
                    ordered_source_ids[1:],
                )
                if (
                    first_owner_positions[source_id]
                    < first_owner_positions[previous_source_id]
                )
            ),
            None,
        )
        owner_node_keys = {
            source_id: blueprint.nodes[position].key
            for source_id, position in first_owner_positions.items()
        }
        detail = (
            f"：{inversion[0]}@{owner_node_keys[inversion[0]]} 与 "
            f"{inversion[1]}@{owner_node_keys[inversion[1]]}"
            if inversion else ""
        )
        errors.append(
            "[BLUEPRINT_FIRST_CONSUMPTION_ORDER] 来源首次消费顺序违背原文"
            + detail
        )

    first = blueprint.nodes[0]
    if first.time_relation != "episode_start":
        errors.append(
            "[BLUEPRINT_EPISODE_START] 首节点必须标记 episode_start"
        )

    flashback_active = False
    known_node_keys: set[str] = set()
    active_state_facts: defaultdict[str, set[str]] = defaultdict(set)
    facts: dict[str, BlueprintStateChange] = {}
    participant_locations: dict[str, str] = {}
    constrained_since: dict[str, int] = {}
    constraint_facts: dict[str, str] = {}
    release_nodes: defaultdict[str, set[str]] = defaultdict(set)
    for index, node in enumerate(blueprint.nodes):
        previous = blueprint.nodes[index - 1] if index else None
        if previous is not None:
            time_changed = (
                node.temporal_domain_key != previous.temporal_domain_key
            )
            location_changed = node.location_key != previous.location_key
            if (
                (time_changed or location_changed)
                and not node.transition_cue.strip()
            ):
                errors.append(
                    f"[BLUEPRINT_TRANSITION_CUE_MISSING] {node.key} "
                    "发生时空变化但没有可见/可听转场依据"
                )
            if time_changed and node.time_relation in {
                "continuous", "flashback_continue",
            }:
                errors.append(
                    f"[BLUEPRINT_TIME_RELATION_INVALID] {node.key} "
                    "时间域变化却标记为连续"
                )

        if node.time_relation == "flashback_enter":
            if flashback_active:
                errors.append(
                    f"[BLUEPRINT_FLASHBACK_NESTED] {node.key} 重复进入回忆"
                )
            flashback_active = True
        elif node.time_relation == "flashback_continue" and not flashback_active:
            errors.append(
                f"[BLUEPRINT_FLASHBACK_ORPHAN] {node.key} 未进入回忆却延续回忆"
            )
        elif node.time_relation == "flashback_exit":
            if not flashback_active:
                errors.append(
                    f"[BLUEPRINT_FLASHBACK_EXIT_ORPHAN] {node.key} "
                    "没有可退出的回忆"
                )
            flashback_active = False

        for participant in node.participants:
            previous_location = participant_locations.get(participant)
            if (
                previous_location
                and previous_location != node.location_key
                and not node.transition_cue.strip()
            ):
                errors.append(
                    f"[BLUEPRINT_CHARACTER_TELEPORT] {node.key} 中 "
                    f"{participant} 从 {previous_location} 无衔接到 "
                    f"{node.location_key}"
                )
            participant_locations[participant] = node.location_key

        participant_keys = set(node.participants)
        evidence_keys = {
            evidence.identity_key for evidence in node.participant_evidence
            if evidence.identity_key
        } | {
            identity_key
            for assignment in node.state_subject_assignments
            for identity_key in assignment.identity_keys
        }
        for evidence in node.participant_evidence:
            unknown_evidence_sources = (
                set(evidence.source_segment_ids) - set(node.source_segment_ids)
            )
            if unknown_evidence_sources:
                errors.append(
                    f"[BLUEPRINT_PARTICIPANT_EVIDENCE_OUT_OF_SCOPE] {node.key} "
                    f"{evidence.identity_key} 引用非 owned SRC："
                    + "、".join(sorted(unknown_evidence_sources))
                )
            if evidence.identity_key not in participant_keys:
                errors.append(
                    f"[BLUEPRINT_PARTICIPANT_EVIDENCE_ORPHAN] {node.key} "
                    f"{evidence.identity_key} 未列入 participants"
                )
        # Presence of the list itself is not authority.  A node with declared
        # participants and an empty evidence list used to bypass this gate,
        # even though the equivalent non-empty/partial list was rejected.
        missing_evidence = participant_keys - evidence_keys
        if missing_evidence:
            errors.append(
                f"[BLUEPRINT_PARTICIPANT_EVIDENCE_MISSING] {node.key} 缺少"
                "参与者来源证据：" + "、".join(sorted(missing_evidence))
            )

        for requirement in node.state_requirements:
            if requirement.assumed_prior:
                active_state_facts[requirement.state_key].add(
                    f"assumed:{node.key}:{requirement.state_key}"
                )
                continue
            fact = facts.get(requirement.required_fact_key)
            if fact is None:
                errors.append(
                    f"[BLUEPRINT_STATE_UNESTABLISHED] {node.key} 依赖未建立状态 "
                    f"{requirement.state_key}；required_fact_key="
                    f"{requirement.required_fact_key or '（空）'}"
                )
            elif fact.state_key != requirement.state_key:
                errors.append(
                    f"[BLUEPRINT_STATE_KEY_MISMATCH] {node.key} 引用事实 "
                    f"{fact.fact_key}，但 state_key 不一致"
                )
            elif (
                fact.fact_key
                not in active_state_facts[requirement.state_key]
            ):
                errors.append(
                    f"[BLUEPRINT_STATE_SUPERSEDED] {node.key} 依赖的事实 "
                    f"{fact.fact_key} 已被后续状态替代"
                )
        for change in node.state_changes:
            if change.fact_key in facts:
                errors.append(
                    f"[BLUEPRINT_FACT_KEY_DUPLICATE] {change.fact_key} 重复"
                )
                continue
            for superseded_key in change.supersedes_fact_keys:
                superseded = facts.get(superseded_key)
                is_active = (
                    superseded is not None
                    and superseded_key
                    in active_state_facts[superseded.state_key]
                )
                is_explicit_constraint_release = (
                    is_active
                    and bool(node.released_constraints_for)
                    and superseded_key in constraint_facts.values()
                )
                if not is_active or (
                    superseded.state_key != change.state_key
                    and not is_explicit_constraint_release
                ):
                    errors.append(
                        f"[BLUEPRINT_STATE_SUPERSEDE_INVALID] {node.key} "
                        f"不能替代事实 {superseded_key}"
                    )
                    continue
                active_state_facts[superseded.state_key].discard(
                    superseded_key
                )
            facts[change.fact_key] = change
            active_state_facts[change.state_key].add(change.fact_key)

        for release_key in node.released_constraints_for:
            actor_key = release_key
            if release_key not in constraint_facts:
                actor_key = next(
                    (
                        actor
                        for actor, fact_key in constraint_facts.items()
                        if fact_key == release_key
                    ),
                    release_key,
                )
            constraint_fact_key = constraint_facts.get(actor_key)
            fact_released = any(
                constraint_fact_key in change.supersedes_fact_keys
                for change in node.state_changes
            )
            if not constraint_fact_key or not fact_released:
                errors.append(
                    f"[BLUEPRINT_AGENCY_RELEASE_UNGROUNDED] {node.key} "
                    f"声称解除 {actor_key} 的约束，但没有替代有效约束事实"
                )
                continue
            constrained_since.pop(actor_key, None)
            constraint_facts.pop(actor_key, None)
            release_nodes[actor_key].add(node.key)

        decision = node.decision
        if decision is not None:
            if decision.actor_key not in set(node.participants):
                errors.append(
                    f"[BLUEPRINT_DECISION_ACTOR_NOT_PARTICIPANT] {node.key} "
                    f"的 decision actor {decision.actor_key} 不在 participants"
                )
            if (
                node.participant_evidence
                and decision.actor_key not in {
                    evidence.identity_key
                    for evidence in node.participant_evidence
                }
            ):
                errors.append(
                    f"[BLUEPRINT_DECISION_ACTOR_EVIDENCE_MISSING] {node.key} "
                    f"的 decision actor {decision.actor_key} 没有 participant evidence"
                )
            unknown_setup = (
                set(decision.setup_node_keys)
                - known_node_keys
                - {node.key}
            )
            if unknown_setup:
                errors.append(
                    f"[BLUEPRINT_MOTIVATION_FUTURE] {node.key} 的动机依据"
                    "尚未发生："
                    + "、".join(sorted(unknown_setup))
                )
            if (
                decision.impact == "major"
                and not decision.setup_node_keys
            ):
                errors.append(
                    f"[BLUEPRINT_MOTIVATION_MISSING] {node.key} 的重大决定"
                    "没有前置压力、欲望或认知依据"
                )
            constrained_at = constrained_since.get(decision.actor_key)
            if (
                decision.agency_mode == "voluntary"
                and constrained_at is not None
            ):
                errors.append(
                    f"[BLUEPRINT_AGENCY_RELEASE_MISSING] {node.key} 将"
                    f"{decision.actor_key} 从被迫/无行为能力改为自主，"
                    "但中间没有约束解除节点"
                )
            elif decision.agency_mode in {
                "coerced", "incapacitated",
            }:
                constrained_since[decision.actor_key] = index
                constraint_fact = facts.get(
                    decision.constraint_fact_key,
                )
                if (
                    constraint_fact is None
                    or decision.constraint_fact_key
                    not in active_state_facts[
                        constraint_fact.state_key
                    ]
                ):
                    errors.append(
                        f"[BLUEPRINT_AGENCY_CONSTRAINT_FACT_MISSING] "
                        f"{node.key} 标记为 {decision.agency_mode}，但没有"
                        "建立有效 constraint_fact_key"
                    )
                else:
                    constraint_facts[decision.actor_key] = (
                        decision.constraint_fact_key
                    )
            unknown_release_keys = (
                set(decision.constraint_release_node_keys)
                - release_nodes[decision.actor_key]
            )
            if unknown_release_keys:
                errors.append(
                    f"[BLUEPRINT_AGENCY_RELEASE_REFERENCE_INVALID] {node.key} "
                    "引用的约束解除节点无效："
                    + "、".join(sorted(unknown_release_keys))
                )
        known_node_keys.add(node.key)

    if flashback_active:
        errors.append("[BLUEPRINT_FLASHBACK_UNCLOSED] 回忆时间域没有返回现在")

    try:
        plans = derive_blueprint_scene_plans(blueprint)
    except (
        BlueprintSourceOccurrenceError,
        BlueprintSourceOwnershipError,
    ) as exc:
        errors.extend(exc.errors)
    else:
        errors.extend(validate_blueprint_scene_partition(blueprint, plans))
    return list(dict.fromkeys(errors))


def validate_and_apply_blueprint_scene_contract(
    candidate: Any,
    blueprint: NarrativeBlueprint,
    *,
    allow_prefix: bool = False,
) -> list[str]:
    """Validate authored IR scenes and apply program-owned headings/order."""
    errors: list[str] = []
    entity_keys = list(dict.fromkeys(
        participant
        for node in blueprint.nodes
        for participant in node.participants
        if participant
    ))
    entity_components: defaultdict[str, list[str]] = defaultdict(list)
    for entity_key in entity_keys:
        for component in entity_key.split("_"):
            if len(component) >= 2:
                entity_components[component].append(entity_key)
    for identity in list(getattr(candidate, "identities", []) or []):
        identity_key = str(getattr(identity, "key", "") or "")
        if (
            identity_key.startswith("context_actor_")
            or getattr(identity, "role_type", "")
            == "source_backed_scene_context_actor"
        ):
            continue
        current_display_name = str(
            getattr(identity, "display_name", "") or ""
        )
        if any(
            current_display_name == entity_key.replace("_", "")
            or current_display_name in entity_key.split("_")
            for entity_key in entity_keys
        ):
            continue
        identity_tokens = " ".join([
            identity_key,
            current_display_name,
        ])
        full_matches = [
            entity_key
            for entity_key in entity_keys
            if entity_key.replace("_", "") in identity_tokens
        ]
        component_matches = [
            (component, keys[0])
            for component, keys in entity_components.items()
            if len(keys) == 1 and component in identity_tokens
        ]
        candidate_names = {
            entity_key.replace("_", "")
            for entity_key in full_matches
        } | {
            component
            for component, _entity_key in component_matches
        }
        if len(candidate_names) == 1:
            identity.display_name = next(iter(candidate_names))
    plans = derive_blueprint_scene_plans(blueprint)
    scenes = list(getattr(candidate, "scenes", []) or [])
    if len(scenes) > len(plans):
        errors.append(
            "[BLUEPRINT_SCENE_COUNT_OVERFLOW] 剧本场次数超过程序蓝图："
            f"{len(scenes)}>{len(plans)}"
        )
        return errors
    if not allow_prefix and len(scenes) != len(plans):
        errors.append(
            "[BLUEPRINT_SCENE_PREFIX_INCOMPLETE] 剧本没有完成全部蓝图场次："
            f"{len(scenes)}/{len(plans)}"
        )

    if hasattr(candidate, "source_scene_owners"):
        candidate.source_scene_owners = dict(
            blueprint.source_scene_owners
        )
    if hasattr(candidate, "scene_derivations"):
        candidate.scene_derivations = [
            relation.model_dump(mode="json")
            for relation in blueprint.scene_derivations
        ]

    actual_source_scenes: defaultdict[str, list[str]] = defaultdict(list)
    for scene_index, scene in enumerate(scenes):
        if scene_index >= len(plans):
            continue
        scene_key = plans[scene_index].key
        for unit in (getattr(scene, "units", []) or []):
            for source_id in (
                getattr(unit, "source_segment_ids", []) or []
            ):
                if scene_key not in actual_source_scenes[source_id]:
                    actual_source_scenes[source_id].append(scene_key)
    for source_id, scene_keys in actual_source_scenes.items():
        if len(scene_keys) > 1:
            errors.append(
                "[BLUEPRINT_SOURCE_REUSED_ACROSS_SCENES] "
                f"{source_id} 同时被 " + "、".join(scene_keys) + " 消费"
            )
    if errors:
        return errors

    source_order = {
        source_id: index
        for index, source_id in enumerate(
            blueprint.source_scene_owners
        )
    }
    allowed_by_plan = [
        set(plan.source_segment_ids) for plan in plans
    ]
    reassigned_units: list[list[Any]] = [
        [] for _scene in scenes
    ]
    for scene_index, scene in enumerate(scenes):
        for unit in (getattr(scene, "units", []) or []):
            unit_source_ids = set(
                getattr(unit, "source_segment_ids", []) or []
            )
            candidate_indexes = [
                plan_index
                for plan_index, allowed_source_ids
                in enumerate(allowed_by_plan[:len(scenes)])
                if unit_source_ids.issubset(allowed_source_ids)
            ]
            if (
                not candidate_indexes
                and getattr(unit, "kind", "") == "action"
                and unit_source_ids
            ):
                source_groups: list[tuple[int, list[str]]] = []
                for source_id in (
                    getattr(unit, "source_segment_ids", []) or []
                ):
                    owning_indexes = [
                        plan_index
                        for plan_index, allowed_source_ids
                        in enumerate(allowed_by_plan[:len(scenes)])
                        if source_id in allowed_source_ids
                    ]
                    owner_index = min(
                        owning_indexes,
                        key=lambda index: abs(index - scene_index),
                        default=scene_index,
                    )
                    if (
                        not source_groups
                        or source_groups[-1][0] != owner_index
                    ):
                        source_groups.append((owner_index, [source_id]))
                    else:
                        source_groups[-1][1].append(source_id)
                clauses = [
                    clause.strip()
                    for clause in re.findall(
                        r"[^，。！？；]+[，。！？；]?",
                        str(getattr(unit, "text", "")),
                    )
                    if clause.strip()
                ]
                if (
                    len(source_groups) > 1
                    and len(clauses) >= len(source_groups)
                ):
                    clause_start = 0
                    total_sources = sum(
                        len(source_ids)
                        for _index, source_ids in source_groups
                    )
                    consumed_sources = 0
                    for part_index, (
                        owner_index,
                        source_ids,
                    ) in enumerate(source_groups, start=1):
                        consumed_sources += len(source_ids)
                        clause_end = (
                            len(clauses)
                            if part_index == len(source_groups)
                            else max(
                                clause_start + 1,
                                round(
                                    len(clauses)
                                    * consumed_sources
                                    / max(total_sources, 1)
                                ),
                            )
                        )
                        split_unit = unit.model_copy(deep=True)
                        split_unit.event_key = (
                            f"{unit.event_key}-bp-part-{part_index}"
                        )
                        split_unit.text = "".join(
                            clauses[clause_start:clause_end]
                        )
                        split_unit.source_segment_ids = source_ids
                        reassigned_units[owner_index].append(split_unit)
                        clause_start = clause_end
                    continue
            target_index = (
                scene_index
                if scene_index in candidate_indexes
                else min(
                    candidate_indexes,
                    key=lambda index: abs(index - scene_index),
                    default=scene_index,
                )
            )
            reassigned_units[target_index].append(unit)
    for scene_index, scene in enumerate(scenes):
        scene.units = sorted(
            reassigned_units[scene_index],
            key=lambda unit: min(
                (
                    source_order.get(source_id, len(source_order))
                    for source_id in (
                        getattr(unit, "source_segment_ids", []) or []
                    )
                ),
                default=len(source_order),
            ),
        )

    ordered_scenes = []
    for scene, plan in zip(scenes, plans):
        allowed_source_ids = set(plan.source_segment_ids)
        invalid_units = [
            str(getattr(unit, "event_key", ""))
            for unit in (getattr(scene, "units", []) or [])
            if not set(getattr(unit, "source_segment_ids", []) or []).issubset(
                allowed_source_ids,
            )
        ]
        if invalid_units:
            errors.append(
                f"[BLUEPRINT_SCENE_SOURCE_ESCAPE] {plan.key} 的 units 引用了"
                "其他时空节点来源："
                + "、".join(invalid_units[:10])
            )
        scene.key = plan.key
        scene.scene_heading = plan.scene_heading
        if hasattr(scene, "previous_scene_exit_state"):
            scene.previous_scene_exit_state = (
                plan.previous_scene_exit_state
            )
        if hasattr(scene, "opening_image"):
            scene.opening_image = plan.opening_image
        if hasattr(scene, "entry_state"):
            scene.entry_state = plan.opening_image
        if hasattr(scene, "exit_state"):
            scene.exit_state = plan.exit_state
        if hasattr(scene, "agency_contracts"):
            scene.agency_contracts = plan.agency_contracts
        ordered_scenes.append(scene)
    candidate.scenes = ordered_scenes
    return errors


def blueprint_prompt_contract() -> dict[str, Any]:
    return {
        "format_version": BLUEPRINT_VERSION,
        "node_source_limit": BLUEPRINT_MAX_SOURCE_SEGMENTS_PER_NODE,
        "time_relations": list(
            NarrativeNode.model_fields["time_relation"].annotation.__args__
        ),
        "required_source_semantics": {
            "fields": [
                "narrative_layer",
                "event_priority",
                "render_policy",
            ],
            "story": {
                "event_priority": "causal",
                "render_policy": "standalone",
                "meaning": "可表演、可形成画面状态变化的剧情语义",
            },
            "paratext": {
                "event_priority": "connective",
                "render_policy": "exclude_from_spine",
                "meaning": (
                    "仅保留完整来源审计，不生成 scene/event/beat/"
                    "scene_outline/shot，也不注入剧情上下文"
                ),
            },
        },
        "program_derived": [
            "scene_plans",
            "scene_heading",
            "scene_order",
            "source_scene_owners",
            "source_semantics.disposition",
            "source_semantics.projection_policy",
            "source_audit_annotations",
            "scene_derivations",
        ],
        "source_ownership": {
            "contract": "each source_id has exactly one scene owner",
            "node_split_boundary": (
                "nodes may split only between source_ids; one source_id "
                "must never be split even when it crosses locations"
            ),
            "single_primary_location": (
                "location_key and location_label identify exactly one primary "
                "location; movement inside one source_id stays in transition "
                "semantics and never becomes a composite location"
            ),
            "cross_scene_context": (
                "state/setup/transition information uses scene_derivations "
                "and never consumes the original source_id again"
            ),
        },
        "participant_evidence_required": {
            "fields": [
                "identity_key",
                "source_segment_ids",
                "source_unit_keys",
                "usage",
            ],
            "usage": ["visible", "voice", "mentioned", "state_subject"],
            "ownership": "source_segment_ids must be owned by the same node",
            "participant_identity_contract": (
                "every participants identity has either an evidence object "
                "with the exact same identity_key and non-empty owned "
                "source_segment_ids, or an exact-unit joint assignment"
            ),
            "dialogue_voice_contract": (
                "every audible source_unit_delivery has exactly one "
                "usage=voice evidence whose identity_key equals performer_key"
            ),
            "non_dialogue_voice_contract": (
                "written_text, sound_effect and unspoken_reference delivery "
                "must not carry voice evidence"
            ),
            "state_subject_contract": (
                "every prose/action source unit must have exactly one "
                "usage=state_subject evidence with an exact source_unit_key, "
                "or its source_unit_key must be listed in "
                "environment_source_unit_keys; missing or ambiguous ownership "
                "is a hard failure"
            ),
            "environment_contract": (
                "environment_source_unit_keys is reserved for genuinely "
                "non-character establishing, weather, place or object state; "
                "never use it for a person's thought, reaction, question or action"
            ),
        },
        "source_unit_delivery_required": {
            "surface_authority": (
                "SourceFact projection=quoted records syntax only"
            ),
            "fields": [
                "source_unit_key",
                "mode",
                "content_owner_key",
                "performer_key",
            ],
            "modes": [
                "spoken_dialogue",
                "offscreen_voice",
                "written_text",
                "sound_effect",
                "unspoken_reference",
            ],
            "contract": (
                "every quoted unit in a picture node has exactly one semantic "
                "delivery decision; quotation marks never imply speech"
            ),
        },
    }
