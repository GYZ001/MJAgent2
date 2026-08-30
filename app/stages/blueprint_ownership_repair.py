"""叙事蓝图——状态主体归属精确复核与专项修复。"""
from __future__ import annotations

import asyncio
from collections import defaultdict
import hashlib
import json
from typing import Any


from app import hiagent
from app.harness import model_gateway
from app.narrative_blueprint import (
    BLUEPRINT_VERSION,
    BlueprintStateSubjectOwnershipPatch,
    NarrativeBlueprint,
    apply_blueprint_state_subject_ownership_patch,
    blueprint_authority_validator_fingerprint,
    blueprint_state_subject_ownership_patch_schema,
)
from app.source_facts import (
    source_facts,
)

from .blueprint_budget import _BlueprintGenerationBudget
from .blueprint_prompt import _blueprint_structured_operation_id
from .constants import (
    BLUEPRINT_SEMANTIC_REVIEW_POLICY_VERSION,
    SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
    SYSTEM_PREFIX,
)
from .ir_snapshot import _narrative_blueprint_content_hash


def _blueprint_exact_ownership_claims(
    blueprint: NarrativeBlueprint,
    target_unit_keys: list[str],
) -> dict[str, dict[str, Any]]:
    """Project only the ownership fields protected by exact-unit repair."""
    return {
        unit_key: {
            "single": [
                {
                    "node_key": node.key,
                    "identity_key": evidence.identity_key,
                }
                for node in blueprint.nodes
                for evidence in node.participant_evidence
                if (
                    evidence.usage == "state_subject"
                    and unit_key in evidence.source_unit_keys
                )
            ],
            "joint": [
                {
                    "node_key": node.key,
                    "identity_keys": list(assignment.identity_keys),
                }
                for node in blueprint.nodes
                for assignment in node.state_subject_assignments
                if assignment.source_unit_key == unit_key
            ],
            "environment_node_keys": [
                node.key
                for node in blueprint.nodes
                if unit_key in node.environment_source_unit_keys
            ],
            "adjudicated_node_keys": [
                node.key
                for node in blueprint.nodes
                if unit_key in node.state_subject_adjudicated_unit_keys
            ],
        }
        for unit_key in target_unit_keys
    }


async def _repair_reviewed_blueprint_state_subject_ownership(
    blueprint: NarrativeBlueprint,
    *,
    issues: list[Any],
    episode: dict[str, Any],
    source_text: str,
    generation_budget: _BlueprintGenerationBudget | None = None,
) -> tuple[NarrativeBlueprint, str]:
    """Adjudicate consensus environment findings through one exact-only call."""
    from app.evidence import repository as evidence_repository
    from app.harness.types import EvidenceArtifact
    from app.observability.tracing import current_trace

    target_unit_keys = list(dict.fromkeys(
        unit_key
        for issue in issues
        for unit_key in issue.source_unit_keys
    ))
    if not target_unit_keys:
        raise ValueError("environment ownership consensus 缺少 exact unit keys")

    patch_schema = blueprint_state_subject_ownership_patch_schema(
        blueprint,
        target_unit_keys,
        source_text,
    )
    facts = source_facts(source_text)
    facts_by_source: defaultdict[str, list[Any]] = defaultdict(list)
    for fact in facts:
        facts_by_source[fact.source_segment_id].append(fact)
    facts_by_key = {fact.source_unit_key: fact for fact in facts}
    nodes_by_source = {
        source_id: node
        for node in blueprint.nodes
        for source_id in node.source_segment_ids
    }
    source_context: dict[str, Any] = {}
    allowed_identities: dict[str, list[str]] = {}
    node_context: dict[str, Any] = {}
    for unit_key in target_unit_keys:
        fact = facts_by_key[unit_key]
        source_group = facts_by_source[fact.source_segment_id]
        fact_index = next(
            index
            for index, candidate in enumerate(source_group)
            if candidate.source_unit_key == unit_key
        )
        owner = nodes_by_source[fact.source_segment_id]
        source_context[unit_key] = {
            "source_fact": fact.model_dump(mode="json"),
            "adjacent_source_units": [
                candidate.model_dump(mode="json")
                for candidate in source_group[
                    max(0, fact_index - 1):fact_index
                ] + source_group[fact_index + 1:fact_index + 2]
            ],
        }
        allowed_identities[unit_key] = [
            identity_key
            for identity_key in owner.participants
            if any(
                evidence.identity_key == identity_key
                and evidence.usage in {"visible", "voice"}
                and fact.source_segment_id in evidence.source_segment_ids
                and (
                    not evidence.source_unit_keys
                    or unit_key in evidence.source_unit_keys
                )
                for evidence in owner.participant_evidence
            )
        ]
        node_context[owner.key] = owner.model_dump(mode="json")

    compact = lambda value: json.dumps(  # noqa: E731
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    repair_prompt = (
        "仅输出 exact-unit state-subject ownership patch JSON，不得输出或改写"
        "完整 Blueprint。repairs 必须恰好覆盖 schema 要求的全部 source unit key。"
        "对每个 target 只依据 source_fact、相邻 source units 与 owning node 的完整"
        "语义，独立选择 single、joint 或 environment；不得按文本关键词、姓名、"
        "内容类别或固定列表判断。single 必须是唯一人物主体，joint 只用于语义上"
        "不可拆的共同主体，environment 只用于确实没有人物状态主体的环境变化。"
        "identity_keys 只能取对应 allowed_identities。除这些 exact target 的"
        "single/joint/environment ownership 外不得修改任何字段。本调用不重试。\n"
        f"base_candidate_hash={patch_schema['properties']['base_candidate_hash']['const']}\n"
        f"review_consensus={compact([issue.model_dump(mode='json') for issue in issues])}\n"
        f"target_source_context={compact(source_context)}\n"
        f"current_ownership={compact(_blueprint_exact_ownership_claims(blueprint, target_unit_keys))}\n"
        f"allowed_identities={compact(allowed_identities)}\n"
        f"owning_nodes={compact(node_context)}\n"
        f"schema={compact(patch_schema)}"
    )
    messages = [
        {"role": "system", "content": SYSTEM_PREFIX},
        {"role": "user", "content": repair_prompt},
    ]
    semantic_input_hash = hashlib.sha256(
        json.dumps(
            {
                "blueprint_hash": _narrative_blueprint_content_hash(blueprint),
                "target_unit_keys": target_unit_keys,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    requested_max_tokens = 8192
    operation_id, effective_max_tokens = _blueprint_structured_operation_id(
        operation_kind="review_ownership_patch",
        episode_id=str(episode.get("id") or ""),
        semantic_input_hash=semantic_input_hash,
        ordinal="exact",
        messages=messages,
        output_schema=patch_schema,
        requested_max_tokens=requested_max_tokens,
        temperature=0.1,
    )

    def validate_patch(
        patch: BlueprintStateSubjectOwnershipPatch,
    ) -> list[str]:
        try:
            apply_blueprint_state_subject_ownership_patch(
                blueprint,
                patch,
                target_unit_keys=target_unit_keys,
                source_text=source_text,
            )
        except (TypeError, ValueError) as exc:
            return [str(exc)]
        return []

    reservation_id: int | None = None
    remaining_seconds: float | None = None
    legacy_retry_call_id: int | None = None
    if generation_budget is not None:
        legacy_retry_call_id = generation_budget.explicit_retry_call_id(
            "screenplay_blueprint_patch"
        )
        reservation_id = generation_budget.claim(
            max_tokens=effective_max_tokens,
            requested_max_tokens=requested_max_tokens,
            operation_id=operation_id,
        )
        remaining_seconds = generation_budget.remaining_seconds()

    patch_call = model_gateway.chat_structured(
        messages,
        model_type=BlueprintStateSubjectOwnershipPatch,
        validate=validate_patch,
        operation_id=operation_id,
        temperature=0.1,
        max_tokens=requested_max_tokens,
        format_retry_limit=0,
        semantic_retry_limit=0,
        call_meta={
            "stage": "剧本蓝图精确主体归属裁决",
            "stage_key": "screenplay_blueprint_patch",
            "call_role": "stage_repair",
            "call_role_label": "蓝图精确主体归属裁决",
            "supersedes_provider_call_id": legacy_retry_call_id,
            "episode_id": str(episode.get("id") or ""),
            "production_grant_id": (
                generation_budget.retry_grant_id
                if generation_budget is not None else ""
            ),
            "contract_version": BLUEPRINT_VERSION,
            "expected_json": True,
            "repair_mode": "exact_state_subject_ownership",
            "reuse_successful_operation": True,
            "require_cached_successful_operation": bool(
                generation_budget is not None
                and operation_id
                in generation_budget._durable_successful_operations
            ),
            "disable_reasoning_fallback": True,
            "disable_provider_retries": True,
            "disable_provider_candidate_fallback": True,
        },
        repair_context=compact({
            "target_source_unit_keys": target_unit_keys,
            "allowed_identities": allowed_identities,
        }),
        output_schema=patch_schema,
        usage_callback=(
            None
            if reservation_id is None
            else lambda usage_event: generation_budget.record_usage(
                reservation_id,
                usage_event,
            )
        ),
    )
    try:
        patch = (
            await patch_call
            if remaining_seconds is None
            else await asyncio.wait_for(
                patch_call,
                timeout=max(0.001, remaining_seconds),
            )
        )
    except hiagent.ProviderError as exc:
        if reservation_id is not None:
            generation_budget.settle(
                reservation_id,
                unreported_outcome=(
                    "not_sent"
                    if exc.delivery_state == "not_sent" and exc.replay_safe
                    else "unknown"
                ),
            )
        raise
    except BaseException:
        if reservation_id is not None:
            generation_budget.settle(reservation_id)
        raise
    else:
        if reservation_id is not None:
            generation_budget.settle(reservation_id)

    repaired = apply_blueprint_state_subject_ownership_patch(
        blueprint,
        patch,
        target_unit_keys=target_unit_keys,
        source_text=source_text,
    )
    if not isinstance(repaired, NarrativeBlueprint):
        repaired = NarrativeBlueprint.model_validate(
            repaired.model_dump(mode="json")
        )
    trace = current_trace()
    artifact = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_narrative_blueprint_ownership_patch",
            scope_type="episode",
            scope_id=str(episode.get("id") or ""),
            status="validated",
            trust_level="T1",
            content={
                "target_source_unit_keys": target_unit_keys,
                "patch": patch.model_dump(mode="json"),
            },
            contract_version=BLUEPRINT_VERSION,
            prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
            model_snapshot={
                "review_policy_version": (
                    BLUEPRINT_SEMANTIC_REVIEW_POLICY_VERSION
                ),
                "authority_fingerprint": (
                    blueprint_authority_validator_fingerprint()
                ),
            },
        ),
        step_run_id=trace.step_run_id,
    )
    return repaired, str(artifact["id"])
