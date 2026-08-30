"""叙事蓝图语义双审——收集一轮的两份独立审稿样本（含未送达补采）。

从 ``blueprint_semantic_review.py`` 拆出：原来内联在主循环里的
``asyncio.gather(run_reviewer_resilient(1), run_reviewer_resilient(2), ...)``
及「恰好一份未送达时补采第三份」的分支，现在改成显式接收/返回
``reviews`` / ``review_artifact_ids`` / ``dropped_voice_issue_counts`` 三个
累积容器（在函数内部创建，返回给调用方，不再靠闭包跨越循环体共享）。
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.narrative_blueprint import BlueprintSemanticReview, NarrativeBlueprint

from .blueprint_semantic_review_inputs import _BlueprintReviewRoundInputs
from .blueprint_semantic_review_reviewer import (
    _blueprint_review_sample_is_undelivered,
    _record_blueprint_review,
    _run_blueprint_reviewer_resilient,
)
from .blueprint_budget import _BlueprintGenerationBudget
from .common import StageError
from .constants import BLUEPRINT_REVIEW_SUPPLEMENTARY_SAMPLE


async def _collect_blueprint_review_samples(
    round_inputs: _BlueprintReviewRoundInputs,
    *,
    blueprint: NarrativeBlueprint,
    source_text: str,
    episode: dict[str, Any],
    generation_budget: _BlueprintGenerationBudget | None,
) -> tuple[list[BlueprintSemanticReview], list[str], dict[int, int]]:
    from app.evidence import repository as evidence_repository
    from app.observability.tracing import current_trace

    trace = current_trace()
    reviews: list[BlueprintSemanticReview] = []
    review_artifact_ids: list[str] = []
    dropped_voice_issue_counts: dict[int, int] = {}

    def run_sample(sample_no: int):
        return _run_blueprint_reviewer_resilient(
            sample_no,
            round_inputs,
            blueprint=blueprint,
            source_text=source_text,
            episode=episode,
            generation_budget=generation_budget,
            dropped_voice_issue_counts=dropped_voice_issue_counts,
        )

    results = await asyncio.gather(
        run_sample(1),
        run_sample(2),
        return_exceptions=True,
    )
    for failure in results:
        # A generation breaker (call/token/wall budget) is not a reviewer
        # being unavailable.  Letting gather() swallow it would resurface it
        # as "审稿人不足两份" and send the operator after the wrong thing.
        if isinstance(failure, StageError):
            raise failure
    outcomes = list(enumerate(results, start=1))
    for sample_no, result in outcomes:
        _record_blueprint_review(
            sample_no,
            result,
            review_round=round_inputs.review_round,
            episode=episode,
            reviews=reviews,
            review_artifact_ids=review_artifact_ids,
            dropped_voice_issue_counts=dropped_voice_issue_counts,
        )

    undelivered = [
        result
        for _sample_no, result in outcomes
        if isinstance(result, BaseException)
        and _blueprint_review_sample_is_undelivered(result)
    ]
    if len(reviews) == 1 and len(undelivered) == 1:
        # Exactly one reviewer never delivered an opinion, so consensus is
        # one clean sample short rather than compromised.  Draw that one
        # sample again as a NEW deterministic operation (sample no 3), which
        # is not a replay of the unresolved call and cannot double-charge
        # it.  Bounded to a single supplementary sample per round, and the
        # call still goes through generation_budget.claim() plus the
        # activation's remaining wall clock, so it cannot outrun any
        # breaker.  Discarding a whole validated blueprint costs ~30
        # minutes; one more review sample costs ~45s.
        if trace.run_id:
            evidence_repository.append_event(
                trace.run_id,
                "BLUEPRINT_REVIEWER_SUPPLEMENTED",
                "info",
                "一名独立审稿样本未送达，补采一个新样本而非作废整份蓝图",
                step_run_id=trace.step_run_id,
                trace_id=trace.trace_id,
                payload={
                    "review_round": round_inputs.review_round,
                    "review_sample": BLUEPRINT_REVIEW_SUPPLEMENTARY_SAMPLE,
                    "undelivered_error_type": type(
                        undelivered[0]
                    ).__name__,
                },
            )
        try:
            supplementary = await run_sample(
                BLUEPRINT_REVIEW_SUPPLEMENTARY_SAMPLE,
            )
        except StageError:
            raise
        except BaseException as exc:  # noqa: BLE001 - fail closed below
            _record_blueprint_review(
                BLUEPRINT_REVIEW_SUPPLEMENTARY_SAMPLE,
                exc,
                review_round=round_inputs.review_round,
                episode=episode,
                reviews=reviews,
                review_artifact_ids=review_artifact_ids,
                dropped_voice_issue_counts=dropped_voice_issue_counts,
            )
        else:
            _record_blueprint_review(
                BLUEPRINT_REVIEW_SUPPLEMENTARY_SAMPLE,
                supplementary,
                review_round=round_inputs.review_round,
                episode=episode,
                reviews=reviews,
                review_artifact_ids=review_artifact_ids,
                dropped_voice_issue_counts=dropped_voice_issue_counts,
            )

    return reviews, review_artifact_ids, dropped_voice_issue_counts
