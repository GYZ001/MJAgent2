"""叙事蓝图语义双审——单轮输入构造与单份独立审稿人调用。

从 ``blueprint_semantic_review.py`` 拆出：

* ``_BlueprintReviewRoundInputs`` / ``_blueprint_semantic_review_round_inputs``
  —— 原来内联在主循环顶部的一段（按 targeted/full 计算投影、节点与来源引用
  合同、审稿 Schema、Prompt），现在打包成一个只读快照，供本文件与
  ``blueprint_semantic_review_round.py`` 按值传递，不再靠闭包跨轮次共享。
* ``_run_blueprint_reviewer`` / ``_run_blueprint_reviewer_resilient`` /
  ``_record_blueprint_review`` —— 原来嵌套在 ``_semantic_review_narrative_
  blueprint`` 循环体内的三个闭包，逐个提升为顶层函数。``reviews`` /
  ``review_artifact_ids`` / ``dropped_voice_issue_counts`` 原来是闭包捕获的
  可变容器，现在显式作为参数传入并原地 mutate（``list.append`` /
  ``dict[key] = ...``），调用方持有的同一个对象立刻可见——不是重新绑定，不会
  出现「下一轮迭代读到孤儿副本」的问题。``trace`` 原来只在外层取一次再靠闭包
  复用，这里改成各自调用 ``current_trace()``：它只读一个 ContextVar，在同一个
  异步任务里多次调用返回同一个 TraceContext，语义不变。
* ``_blueprint_review_sample_is_undelivered`` —— 判断一次失败的审稿调用是否
  「从未真正发表过意见」，从而值得重新抽样。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from app import hiagent
from app.harness import model_gateway

from .blueprint_semantic_review_inputs import _BlueprintReviewRoundInputs
from app.narrative_blueprint import (
    BLUEPRINT_VERSION,
    BlueprintSemanticReview,
    NarrativeBlueprint,
    blueprint_semantic_issue_is_resolved,
    filter_blueprint_semantic_review_voice_issues,
    normalize_blueprint_semantic_review_payload,
    validate_blueprint_semantic_review,
)

from .blueprint_budget import _BlueprintGenerationBudget
from .blueprint_prompt import (
    _blueprint_format_repair_reservation_operation_id,
    _blueprint_structured_operation_id,
)
from .common import StageError
from .constants import (
    BLUEPRINT_REVIEW_FORMAT_RETRY_LIMIT,
    BLUEPRINT_REVIEW_MAX_TOKENS,
    BLUEPRINT_REVIEW_PROVIDER_RETRY_LIMIT,
    SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
    SYSTEM_PREFIX,
)


async def _run_blueprint_reviewer(
    sample_no: int,
    round_inputs: _BlueprintReviewRoundInputs,
    *,
    blueprint: NarrativeBlueprint,
    source_text: str,
    episode: dict[str, Any],
    generation_budget: _BlueprintGenerationBudget | None,
    dropped_voice_issue_counts: dict[int, int],
) -> BlueprintSemanticReview:
    last_validated_review: BlueprintSemanticReview | None = None
    validated_drop_count = 0

    def validate_review(candidate_review: BlueprintSemanticReview) -> list[str]:
        nonlocal last_validated_review, validated_drop_count
        dropped = filter_blueprint_semantic_review_voice_issues(
            candidate_review,
            blueprint,
            source_text,
        )
        if candidate_review is last_validated_review:
            validated_drop_count += dropped
        else:
            last_validated_review = candidate_review
            validated_drop_count = dropped
        errors = validate_blueprint_semantic_review(
            candidate_review,
            blueprint,
            source_text,
        )
        if round_inputs.targeted_review:
            allowed = set(round_inputs.projected_node_keys)
            errors.extend(
                f"风险审稿引用了范围外节点：{node_key}"
                for issue in candidate_review.issues
                for node_key in issue.node_keys
                if node_key not in allowed
            )
        return errors

    review_messages = [
        {"role": "system", "content": SYSTEM_PREFIX},
        {
            "role": "user",
            "content": f"{round_inputs.prompt}\n独立审稿样本编号：{sample_no}",
        },
    ]
    operation_id, effective_max_tokens = (
        _blueprint_structured_operation_id(
            operation_kind="review",
            episode_id=str(episode.get("id") or ""),
            semantic_input_hash=round_inputs.current_blueprint_hash,
            ordinal=(
                f"{round_inputs.review_round}:{sample_no}:"
                f"{'targeted' if round_inputs.targeted_review else 'full'}"
            ),
            messages=review_messages,
            output_schema=round_inputs.review_schema,
            requested_max_tokens=BLUEPRINT_REVIEW_MAX_TOKENS,
            temperature=0.1,
        )
    )
    format_retry_limit = BLUEPRINT_REVIEW_FORMAT_RETRY_LIMIT
    durable_base_replay = bool(
        generation_budget is not None
        and operation_id
        in generation_budget._durable_successful_operations
    )
    reservation_operation_id = operation_id
    if durable_base_replay and format_retry_limit > 0:
        reservation_operation_id = (
            _blueprint_format_repair_reservation_operation_id(
                operation_id
            )
        )
    reservation_id: int | None = None
    remaining_seconds: float | None = None
    legacy_retry_call_id: int | None = None
    if generation_budget is not None:
        legacy_retry_call_id = (
            generation_budget.explicit_retry_call_id(
                "screenplay_blueprint_review"
            )
        )
        reservation_id = generation_budget.claim(
            max_tokens=effective_max_tokens,
            requested_max_tokens=BLUEPRINT_REVIEW_MAX_TOKENS,
            operation_id=reservation_operation_id,
        )
        remaining_seconds = generation_budget.remaining_seconds()
    review_call = model_gateway.chat_structured(
        review_messages,
        model_type=BlueprintSemanticReview,
        validate=validate_review,
        operation_id=operation_id,
        temperature=0.1,
        max_tokens=BLUEPRINT_REVIEW_MAX_TOKENS,
        format_retry_limit=format_retry_limit,
        semantic_retry_limit=0,
        call_meta={
            "stage": "剧本蓝图语义审稿",
            "stage_key": "screenplay_blueprint_review",
            "call_role": "stage_critic",
            "call_role_label": "蓝图独立语义审稿",
            "review_round": round_inputs.review_round,
            "review_sample": sample_no,
            "supersedes_provider_call_id": legacy_retry_call_id,
            "episode_id": str(episode.get("id") or ""),
            "production_grant_id": (
                generation_budget.retry_grant_id
                if generation_budget is not None else ""
            ),
            "contract_version": BLUEPRINT_VERSION,
            "substage": "risk_nodes" if round_inputs.targeted_review else "full",
            "source_count": len(round_inputs.projected_source.splitlines()),
            "reuse_successful_operation": True,
            "require_cached_successful_operation": (
                durable_base_replay and format_retry_limit <= 0
            ),
            "disable_reasoning_fallback": True,
            "disable_provider_retries": True,
            "disable_provider_candidate_fallback": True,
        },
        repair_context=json.dumps(
            {
                "node_reference_contract": round_inputs.node_reference_contract,
                "source_reference_contract": (
                    round_inputs.source_reference_contract
                ),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        output_schema=round_inputs.review_schema,
        normalize_payload=lambda payload: (
            normalize_blueprint_semantic_review_payload(
                payload,
                round_inputs.projected_node_keys,
            )
        ),
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
        review = (
            await review_call
            if remaining_seconds is None
            else await asyncio.wait_for(
                review_call,
                timeout=max(0.001, remaining_seconds),
            )
        )
    except hiagent.ProviderError as exc:
        if reservation_id is not None:
            generation_budget.settle(
                reservation_id,
                unreported_outcome=(
                    "not_sent"
                    if exc.delivery_state == "not_sent"
                    and exc.replay_safe
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
    # The real gateway invokes validate_review, but test/replay
    # adapters are allowed to return a typed cached value directly.
    # Reapply the deterministic authority filter at the boundary so an
    # unsupported delivery/state guess can never reach consensus.  If
    # the same object was already filtered by the callback, retain its
    # prior count instead of counting the boundary no-op twice.
    boundary_dropped = filter_blueprint_semantic_review_voice_issues(
        review,
        blueprint,
        source_text,
    )
    dropped_voice_issue_counts[sample_no] = (
        validated_drop_count + boundary_dropped
        if review is last_validated_review
        else boundary_dropped
    )
    review.issues = [
        issue
        for issue in review.issues
        if not blueprint_semantic_issue_is_resolved(
            issue,
            blueprint,
        )
    ]
    return review


async def _run_blueprint_reviewer_resilient(
    sample_no: int,
    round_inputs: _BlueprintReviewRoundInputs,
    *,
    blueprint: NarrativeBlueprint,
    source_text: str,
    episode: dict[str, Any],
    generation_budget: _BlueprintGenerationBudget | None,
    dropped_voice_issue_counts: dict[int, int],
) -> BlueprintSemanticReview:
    # Retry a single reviewer ONLY when the provider never received the
    # request (not_sent + replay_safe): that cannot double-charge or
    # leave unknown liability, and re-uses the same deterministic
    # operation_id. Timeouts / mid-stream cuts (unknown outcome) are not
    # ProviderError-not_sent, so they still propagate and fail closed.
    from app.evidence import repository as evidence_repository
    from app.observability.tracing import current_trace

    trace = current_trace()
    attempts = BLUEPRINT_REVIEW_PROVIDER_RETRY_LIMIT + 1
    for attempt in range(1, attempts + 1):
        try:
            return await _run_blueprint_reviewer(
                sample_no,
                round_inputs,
                blueprint=blueprint,
                source_text=source_text,
                episode=episode,
                generation_budget=generation_budget,
                dropped_voice_issue_counts=dropped_voice_issue_counts,
            )
        except hiagent.ProviderError as exc:
            replay_safe = bool(
                getattr(exc, "delivery_state", None) == "not_sent"
                and getattr(exc, "replay_safe", False)
            )
            if not replay_safe or attempt >= attempts:
                raise
            if trace.run_id:
                evidence_repository.append_event(
                    trace.run_id,
                    "BLUEPRINT_REVIEWER_RETRY",
                    "info",
                    "独立审稿样本未送达，按 replay-safe 重试同一确定性 operation",
                    step_run_id=trace.step_run_id,
                    trace_id=trace.trace_id,
                    payload={
                        "review_round": round_inputs.review_round,
                        "review_sample": sample_no,
                        "attempt": attempt,
                    },
                )
    raise AssertionError("unreachable reviewer retry exhaustion")


def _record_blueprint_review(
    sample_no: int,
    result: Any,
    *,
    review_round: int,
    episode: dict[str, Any],
    reviews: list[BlueprintSemanticReview],
    review_artifact_ids: list[str],
    dropped_voice_issue_counts: dict[int, int],
) -> bool:
    from app.evidence import repository as evidence_repository
    from app.harness.types import EvidenceArtifact
    from app.observability.tracing import current_trace

    trace = current_trace()
    if isinstance(result, BaseException):
        evidence_repository.append_event(
            trace.run_id,
            "BLUEPRINT_REVIEWER_UNAVAILABLE",
            "warning",
            "蓝图独立审稿样本不可用，已按 operational fail-closed 处理",
            step_run_id=trace.step_run_id,
            trace_id=trace.trace_id,
            payload={
                "review_round": review_round,
                "review_sample": sample_no,
                "error_type": type(result).__name__,
            },
        ) if trace.run_id else None
        return False
    review = result
    reviews.append(review)
    artifact = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_narrative_blueprint_review",
            scope_type="episode",
            scope_id=str(episode.get("id") or ""),
            status="candidate",
            trust_level="T1",
            content=review.model_dump(mode="json"),
            contract_version=BLUEPRINT_VERSION,
            prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
            model_snapshot={
                "review_round": review_round,
                "review_sample": sample_no,
                "dropped_unsupported_voice_issue_count": (
                    dropped_voice_issue_counts.get(sample_no, 0)
                ),
            },
        ),
        step_run_id=trace.step_run_id,
    )
    review_artifact_ids.append(artifact["id"])
    return True


def _blueprint_review_sample_is_undelivered(exc: BaseException) -> bool:
    """Whether a reviewer failed without ever authoring a review opinion.

    Only these are worth drawing again.  A transport failure (timeout, cut
    stream) and a body that never decoded into JSON both mean the reviewer
    never said anything, so a fresh sample restores the missing opinion without
    overruling one.

    Deliberately excluded:

    * ``StructuredSemanticError`` -- the reviewer *did* author an opinion and it
      failed the review contract.  Re-drawing until some sample passes is
      exactly the coached-compliance failure the strict contracts forbid.
    * ``StructuredFormatError`` with ``unparseable=False`` -- a decoded but
      off-schema answer is likewise authored, and the gateway already spent its
      one bounded format repair on it.
    * ``StructuredProviderRejection`` -- an explicit refusal envelope is
      normally persistent; another sample just burns wall clock.
    * ``StageError`` -- generation breakers must surface, not be re-drawn.
    """
    if isinstance(exc, StageError):
        return False
    if isinstance(exc, hiagent.ProviderError):
        return True
    if isinstance(exc, model_gateway.StructuredProviderRejection):
        return False
    if isinstance(exc, model_gateway.StructuredFormatError):
        return bool(getattr(exc, "unparseable", False))
    return False
