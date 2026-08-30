"""Runs the dual-reviewer semantic review/repair loop for one scene-shard
draft: chunked review calls, consensus/canonicalization of findings, and
repair rounds up to the configured retry limits.

Moved verbatim from ``app/screenplay_scene_shards.py`` (see
``app/screenplay_scene_shards/__init__.py`` for the full re-export map).
"""
from __future__ import annotations

import asyncio
import json
from app import hiagent
from app.harness import model_gateway
from collections.abc import Callable
from pydantic import ValidationError
from typing import Any

from .common import (
    _SceneStructuredOperationGate,
    _gather_fail_fast,
    _hash,
    _scene_structured_with_undelivered_retry,
)
from .constants import (
    SCREENPLAY_SCENE_JSON_ONLY_SYSTEM_PROMPT,
    SCREENPLAY_SCENE_SEMANTIC_INITIAL_FORMAT_RETRY_LIMIT,
    SCREENPLAY_SCENE_SEMANTIC_MAX_REPAIR_ROUNDS,
    SCREENPLAY_SCENE_SEMANTIC_POST_REPAIR_FORMAT_RETRY_LIMIT,
    SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION,
)
from .duplication_filter import (
    _scene_shard_canonicalize_cross_slot_findings,
    _scene_shard_filter_distinct_source_ownership_duplication,
    _scene_shard_filter_exact_source_duplication,
)
from .identity_registry import ScreenplaySceneShardError
from .models import (
    ScreenplaySceneInputContract,
    ScreenplaySceneShardCreativeIR,
    ScreenplaySceneShardCreativeUnit,
    ScreenplaySceneShardSemanticFinding,
    ScreenplaySceneShardSemanticReview,
)
from .review_consensus import (
    _scene_shard_canonicalize_review_unit_references,
    _scene_shard_normalize_peer_review_unit_scopes,
    _scene_shard_review_reference_errors,
    screenplay_scene_semantic_consensus,
)
from .review_prompt import (
    _scene_shard_reviewer_findings_payload,
    _scene_shard_semantic_repair_budget,
    _scene_shard_semantic_repair_prompt,
    _scene_shard_semantic_repair_subset_schema,
    _scene_shard_semantic_review_chunks,
    _scene_shard_semantic_review_response_format,
    _scene_shard_strict_response_format,
)
from .scene_prompt import _scene_shard_semantic_authority_payload


async def _semantic_review_scene_shard_draft(
    *,
    draft: ScreenplaySceneShardCreativeIR,
    scene_input_contracts: list[ScreenplaySceneInputContract],
    identity_registry: list[dict[str, Any]],
    operation_id: str,
    shard_id: str,
    validate_draft: Callable[[ScreenplaySceneShardCreativeIR], list[str]],
    batch_abort: asyncio.Event | None = None,
    abort_batch: Callable[[], None] | None = None,
    structured_operation_gate: _SceneStructuredOperationGate | None = None,
    full_creative_schema: dict[str, Any] | None = None,
) -> tuple[ScreenplaySceneShardCreativeIR, list[dict[str, Any]]]:
    """Consensus-review creative prose without allowing structural rewrites."""

    async def review(
        candidate: ScreenplaySceneShardCreativeIR,
        reviewer_no: int,
        phase: str,
        unit_keys: list[str],
        review_prompt: str,
        review_schema: dict[str, Any],
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
            return _scene_shard_review_reference_errors(
                value,
                known_unit_keys,
                allow_local_omitted_unit_key=True,
            )

        async def execute_review() -> ScreenplaySceneShardSemanticReview:
            async def issue_review(
                attempt_operation_id: str,
            ) -> ScreenplaySceneShardSemanticReview:
                return await model_gateway.chat_structured(
                    [
                        {
                            "role": "system",
                            "content": (
                                SCREENPLAY_SCENE_JSON_ONLY_SYSTEM_PROMPT
                            ),
                        },
                        {
                            "role": "user",
                            "content": review_prompt,
                        },
                    ],
                    model_type=ScreenplaySceneShardSemanticReview,
                    validate=validate_review,
                    operation_id=attempt_operation_id,
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
                        "output_reserve_percent": budget[
                            "output_reserve_percent"
                        ],
                        "input_estimate": budget["input_estimate"],
                        "required": budget["required"],
                        "ceiling": budget["ceiling"],
                    },
                    output_schema=review_schema,
                    response_format=(
                        _scene_shard_semantic_review_response_format(
                            review_schema
                        )
                    ),
                    require_response_format=True,
                )

            result = await _scene_structured_with_undelivered_retry(
                issue_review,
                operation_id=(
                    f"{operation_id}:semantic:"
                    f"{SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION}:{phase}:"
                    f"reviewer-{reviewer_no}:"
                    f"chunk-{chunk_index}-of-{chunk_count}:{chunk_hash}"
                ),
            )
            if batch_abort is not None and batch_abort.is_set():
                raise asyncio.CancelledError
            validation_errors = validate_review(result)
            if validation_errors:
                raise ScreenplaySceneShardError(
                    shard_id,
                    validation_errors,
                )
            return result

        if batch_abort is not None and batch_abort.is_set():
            raise asyncio.CancelledError
        try:
            if structured_operation_gate is None:
                return await execute_review()
            return await structured_operation_gate.run(
                execute_review,
                on_failure=abort_batch,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            if batch_abort is not None:
                batch_abort.set()
            raise

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
        semantic_abstentions: list[dict[str, Any]] = []
        for chunk_index, chunk in enumerate(chunks, start=1):
            async def review_one(
                reviewer_no: int,
            ) -> ScreenplaySceneShardSemanticReview | None:
                try:
                    return await review(
                        candidate,
                        reviewer_no,
                        phase,
                        chunk["unit_keys"],
                        chunk["review_prompt"],
                        chunk["review_schema"],
                        chunk["budget"],
                        chunk_index,
                        len(chunks),
                        chunk["chunk_hash"],
                    )
                except hiagent.ProviderError as exc:
                    if str(
                        getattr(exc, "failure_kind", "") or ""
                    ) != "deterministic_rejection":
                        raise
                    # 供应商反复以同一方式拒绝审阅这一块内容。那是它不肯看，
                    # 不是稿件的质量证据，所以按弃权处理而不是让整集失败。
                    semantic_abstentions.append({
                        "phase": phase,
                        "chunk_index": chunk_index,
                        "chunk_count": len(chunks),
                        "chunk_hash": str(chunk["chunk_hash"]),
                        "reviewer_no": reviewer_no,
                        "reason": str(exc)[:300],
                    })
                    return None

            chunk_reviews = await _gather_fail_fast(
                lambda: review_one(1),
                lambda: review_one(2),
                on_failure=abort_batch,
            )
            delivered = [item for item in chunk_reviews if item is not None]
            if not delivered:
                raise ScreenplaySceneShardError(
                    shard_id,
                    [
                        f"语义审查第 {chunk_index}/{len(chunks)} 块的两名审稿人"
                        "都被供应商丢弃，本块没有经过任何审查"
                    ],
                )
            if len(delivered) < len(chunk_reviews):
                # 共识规则是两名审稿人取交集，所以退回单审稿人只会让门禁更严：
                # 幸存者报出的每一条都会通过交集，绝不会放过共识本可拦下的问题。
                # 把幸存者的判断同时计入双方，弃权本身留在审计里可查。
                chunk_reviews = [delivered[0] for _ in chunk_reviews]
            scope_errors = _scene_shard_normalize_peer_review_unit_scopes(
                chunk_reviews,
                set(chunk["unit_keys"]),
            )
            if scope_errors:
                raise ScreenplaySceneShardError(
                    shard_id,
                    scope_errors,
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
            [
                *(item.model_dump(mode="json") for item in reviews),
                *(
                    [{"provider_abstentions": semantic_abstentions}]
                    if semantic_abstentions else []
                ),
            ],
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
            full_creative_schema=full_creative_schema,
        )
        subset_schema_hash = _hash(subset_schema)

        def validate_repair(
            candidate: ScreenplaySceneShardCreativeIR,
        ) -> list[str]:
            if full_creative_schema is not None:
                required_root_fields = {"contract_version", "slots"}
                if candidate.model_fields_set != required_root_fields:
                    return [
                        "语义 repair root 必须显式提供且仅提供 "
                        "contract_version、slots"
                    ]
                required_unit_fields = set(
                    ScreenplaySceneShardCreativeUnit.model_fields
                )
                incomplete_unit_keys = [
                    unit_key
                    for unit_key, unit in candidate.slots.items()
                    if unit.model_fields_set != required_unit_fields
                ]
                if incomplete_unit_keys:
                    return [
                        "语义 repair slot 必须显式提供全部 creative fields："
                        + ",".join(incomplete_unit_keys)
                    ]
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

        async def execute_repair() -> ScreenplaySceneShardCreativeIR:
            repair_response_format = _scene_shard_strict_response_format(
                name="screenplay_scene_semantic_repair",
                local_schema=subset_schema,
            )

            def repair_schema(
                _candidate: ScreenplaySceneShardCreativeIR,
            ) -> dict[str, Any]:
                return subset_schema

            async def issue_repair(
                attempt_operation_id: str,
            ) -> ScreenplaySceneShardCreativeIR:
                return await model_gateway.chat_structured(
                    repair_messages,
                    model_type=ScreenplaySceneShardCreativeIR,
                    validate=validate_repair,
                    operation_id=attempt_operation_id,
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
                    response_format=repair_response_format,
                    require_response_format=True,
                    repair_schema=repair_schema,
                )

            repaired_candidate = await _scene_structured_with_undelivered_retry(
                issue_repair,
                operation_id=(
                    f"{operation_id}:semantic:"
                    f"{SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION}:repair:"
                    f"round-{repair_round}:{current_draft_hash}:"
                    f"{subset_schema_hash}"
                ),
            )
            if batch_abort is not None and batch_abort.is_set():
                raise asyncio.CancelledError
            repair_errors = validate_repair(repaired_candidate)
            if repair_errors:
                raise ScreenplaySceneShardError(
                    shard_id,
                    repair_errors,
                )
            return repaired_candidate

        try:
            if structured_operation_gate is None:
                repaired = await execute_repair()
            else:
                repaired = await structured_operation_gate.run(
                    execute_repair,
                    on_failure=abort_batch,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            if batch_abort is not None:
                batch_abort.set()
            if abort_batch is not None:
                abort_batch()
            raise
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
        unresolved_semantic_units={
            item.unit_key: [item.message]
            for item in findings
        },
    )
