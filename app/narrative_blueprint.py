"""Pre-writing narrative authority contract for screenplay generation.

The model identifies semantic timeline nodes. The server validates source
ownership and state transitions, then derives scene boundaries
deterministically. Screenplay prose is authored only after this contract
passes.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.source_excerpt import (
    index_source_segments,
    structural_front_matter_ids,
)


BLUEPRINT_VERSION = "screenplay-narrative-blueprint.v2"
BLUEPRINT_MAX_SOURCE_SEGMENTS_PER_NODE = 8


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
    usage: Literal["visible", "voice", "mentioned", "state_subject"]

    @field_validator("source_segment_ids", mode="before")
    @classmethod
    def _normalize_source_ids(cls, value: Any) -> list[str]:
        values = value if isinstance(value, list) else [value] if value else []
        return [_normalize_source_segment_id(item) for item in values]


class NarrativeNode(BaseModel):
    key: str
    source_segment_ids: list[str]
    summary: str
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


class BlueprintScenePlan(BaseModel):
    key: str
    node_keys: list[str]
    source_segment_ids: list[str]
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


class NarrativeBlueprint(BaseModel):
    format_version: str = BLUEPRINT_VERSION
    episode_no: int
    nodes: list[NarrativeNode]
    scene_plans: list[BlueprintScenePlan] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalize_version(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        normalized["format_version"] = BLUEPRINT_VERSION
        return normalized


class NarrativeBlueprintShard(BaseModel):
    format_version: str = BLUEPRINT_VERSION
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
        normalized["format_version"] = BLUEPRINT_VERSION
        normalized["source_segment_ids"] = [
            _normalize_source_segment_id(source_id)
            for source_id in (
                normalized.get("source_segment_ids") or []
            )
        ]
        return normalized


def validate_narrative_blueprint_shard(
    shard: NarrativeBlueprintShard,
    *,
    expected_episode_no: int,
    expected_shard_index: int,
    expected_source_segment_ids: list[str],
    optional_source_segment_ids: set[str] | None = None,
    boundary_state_facts: list[dict[str, Any]] | None = None,
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
        if (
            not node.source_segment_ids
            or len(node.source_segment_ids)
            > BLUEPRINT_MAX_SOURCE_SEGMENTS_PER_NODE
        ):
            errors.append(
                f"[BLUEPRINT_SHARD_NODE_SIZE] {node.key} 来源数量非法"
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
    code: Literal[
        "timeline_conflict",
        "spatial_action_gap",
        "persistent_state_conflict",
        "motivation_gap",
        "agency_conflict",
        "setup_missing",
        "identity_or_role_conflict",
        "ending_payoff_gap",
    ]
    node_keys: list[str]
    source_segment_ids: list[str] = Field(default_factory=list)
    message: str
    required_resolution: str
    must_fix: bool = True


class BlueprintSemanticReview(BaseModel):
    issues: list[BlueprintSemanticIssue] = Field(default_factory=list)


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


def apply_narrative_blueprint_patch(
    blueprint: NarrativeBlueprint,
    patch: NarrativeBlueprintPatch,
    *,
    allow_source_expansion: bool = False,
) -> int:
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
    derive_blueprint_scene_plans(blueprint)
    return changed


def derive_blueprint_scene_plans(
    blueprint: NarrativeBlueprint,
) -> list[BlueprintScenePlan]:
    groups: list[list[NarrativeNode]] = []
    for node in blueprint.nodes:
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
        source_segment_ids = list(dict.fromkeys(
            source_id
            for node in nodes
            for source_id in node.source_segment_ids
        ))
        plans.append(BlueprintScenePlan(
            key=f"bp-sc{index:03d}",
            node_keys=[node.key for node in nodes],
            source_segment_ids=source_segment_ids,
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
                for participant in node.participants
                if participant
            )),
            scene_heading=(
                f"【场{index}】{first.time_label} / {first.location_label}"
            ),
        ))
    blueprint.scene_plans = plans
    return plans


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
        if node.participant_evidence:
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

    plans = derive_blueprint_scene_plans(blueprint)
    planned_node_keys = [
        node_key for plan in plans for node_key in plan.node_keys
    ]
    if planned_node_keys != node_keys:
        errors.append(
            "[BLUEPRINT_SCENE_PARTITION_INVALID] 程序分场未完整保持节点顺序"
        )
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

    source_order: dict[str, int] = {}
    for node in blueprint.nodes:
        for source_id in node.source_segment_ids:
            source_order.setdefault(source_id, len(source_order))
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
        "program_derived": ["scene_plans", "scene_heading", "scene_order"],
        "participant_evidence_required": {
            "fields": ["identity_key", "source_segment_ids", "usage"],
            "usage": ["visible", "voice", "mentioned", "state_subject"],
            "ownership": "source_segment_ids must be owned by the same node",
        },
    }
