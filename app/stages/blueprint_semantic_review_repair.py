"""叙事蓝图语义双审——按权威问题修复蓝图（普通节点 + state-subject 归属两条通路）。

从 ``blueprint_semantic_review.py`` 拆出：原来内联在主循环尾部的修复分支——把
``consensus.authoritative_issues`` 拆成 ownership（``state_subject_environment_
misclassified``）与 mixed 两组，分别走 ``_repair_narrative_blueprint`` 与
``_repair_reviewed_blueprint_state_subject_ownership``，再落一条
``screenplay_narrative_blueprint_review_repair_link`` 证据产物。返回更新后的
``(blueprint, targeted_review)``——``targeted_review`` 在原函数里是跨轮次的可变
局部变量，这里通过返回值显式回写，调用方（编排函数）负责把它带到下一轮，而不是
像闭包那样隐式共享。
"""
from __future__ import annotations

from typing import Any

from app.errors import ContentGenerationError
from app.narrative_blueprint import BLUEPRINT_VERSION, NarrativeBlueprint

from .blueprint_budget import _BlueprintGenerationBudget
from .blueprint_ownership_repair import (
    _blueprint_exact_ownership_claims,
    _repair_reviewed_blueprint_state_subject_ownership,
)
from .blueprint_repair import _repair_narrative_blueprint
from .blueprint_semantic_review_consensus import _BlueprintReviewConsensus
from .constants import SCREENPLAY_BLUEPRINT_PROMPT_VERSION


async def _repair_blueprint_from_review(
    blueprint: NarrativeBlueprint,
    consensus: _BlueprintReviewConsensus,
    *,
    episode: dict[str, Any],
    source_text: str,
    generation_budget: _BlueprintGenerationBudget | None,
    review_artifact_ids: list[str],
    targeted_review: bool,
) -> tuple[NarrativeBlueprint, bool]:
    from app.evidence import repository as evidence_repository
    from app.harness.types import EvidenceArtifact
    from app.observability.tracing import current_trace

    trace = current_trace()
    authoritative_issues = consensus.authoritative_issues
    semantic_errors = [
        (
            f"[BLUEPRINT_SEMANTIC_{issue.code.upper()}] "
            f"{'、'.join(issue.node_keys)} "
            f"{'、'.join(issue.source_segment_ids)} "
            f"{'、'.join(issue.source_unit_keys)}："
            f"{issue.message}；必须：{issue.required_resolution}"
        )
        for issue in authoritative_issues
    ]
    ownership_issues = [
        issue
        for issue in authoritative_issues
        if issue.code == "state_subject_environment_misclassified"
    ]
    mixed_issues = [
        issue
        for issue in authoritative_issues
        if issue.code != "state_subject_environment_misclassified"
    ]
    ownership_artifact_ids: list[str] = []
    if mixed_issues:
        protected_unit_keys = list(dict.fromkeys(
            unit_key
            for issue in ownership_issues
            for unit_key in issue.source_unit_keys
        ))
        protected_claims = _blueprint_exact_ownership_claims(
            blueprint,
            protected_unit_keys,
        )
        blueprint = await _repair_narrative_blueprint(
            blueprint,
            episode=episode,
            source_text=source_text,
            additional_errors=[
                error
                for error, issue in zip(
                    semantic_errors,
                    authoritative_issues,
                )
                if issue.code
                != "state_subject_environment_misclassified"
            ],
            generation_budget=generation_budget,
        )
        if protected_claims != _blueprint_exact_ownership_claims(
            blueprint,
            protected_unit_keys,
        ):
            raise ContentGenerationError(
                "蓝图普通节点修复越权改写 exact-unit ownership"
            )
    if ownership_issues:
        blueprint, ownership_artifact_id = (
            await _repair_reviewed_blueprint_state_subject_ownership(
                blueprint,
                issues=ownership_issues,
                episode=episode,
                source_text=source_text,
                generation_budget=generation_budget,
            )
        )
        ownership_artifact_ids.append(ownership_artifact_id)
        targeted_review = False
    elif consensus.non_consensus_issue_count:
        targeted_review = False
    evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_narrative_blueprint_review_repair_link",
            scope_type="episode",
            scope_id=str(episode.get("id") or ""),
            status="validated",
            trust_level="T1",
            content={
                "review_artifact_ids": review_artifact_ids,
                "repaired_issue_count": len(authoritative_issues),
                "consensus_repaired_issue_count": len(
                    consensus.consensus_issues
                ),
                "deterministic_authority_repaired_issue_count": len(
                    consensus.deterministic_authority_issues
                ),
                "ownership_repaired_issue_count": len(ownership_issues),
                "mixed_repaired_issue_count": len(mixed_issues),
                "ownership_source_unit_keys": list(dict.fromkeys(
                    unit_key
                    for issue in ownership_issues
                    for unit_key in issue.source_unit_keys
                )),
            },
            parent_artifact_ids=(
                review_artifact_ids + ownership_artifact_ids
            ),
            contract_version=BLUEPRINT_VERSION,
            prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
        ),
        step_run_id=trace.step_run_id,
    )
    return blueprint, targeted_review
