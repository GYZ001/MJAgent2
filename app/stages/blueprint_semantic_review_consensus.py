"""叙事蓝图语义双审——双份审稿的共识计算与共识产物落盘。

从 ``blueprint_semantic_review.py`` 拆出：原来内联在主循环里的一段（issue_maps
交集求共识、确定性权威问题过滤、consensus artifact 落盘、以及「不足两份」时的
单独落盘分支），加上其唯一调用方在这一段的两个 issue 级别判据函数
（``_blueprint_semantic_issue_exact_scope`` / ``_blueprint_semantic_issue_has_
deterministic_authority``）。``_BlueprintReviewConsensus`` 把原来一串局部变量
（``issue_maps``/``consensus_keys``/``authoritative_issues``/...）打包成一个
只读快照，供编排函数按分支决策，不再靠函数体内散落的布尔局部变量。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.narrative_blueprint import (
    BLUEPRINT_VERSION,
    BlueprintSemanticReview,
    NarrativeBlueprint,
    blueprint_authority_validator_fingerprint,
    blueprint_semantic_voice_issue_has_dialogue_authority,
)

from .blueprint_semantic_review_inputs import _BlueprintReviewRoundInputs
from .constants import (
    BLUEPRINT_SEMANTIC_REVIEW_POLICY_VERSION,
    SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
)


def _blueprint_semantic_issue_exact_scope(
    issue: Any,
) -> tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Return the exact scope used to bind a review to local authority."""
    return (
        str(issue.code),
        tuple(sorted(str(key) for key in issue.node_keys)),
        tuple(sorted(str(key) for key in issue.source_segment_ids)),
        tuple(sorted(str(key) for key in issue.source_unit_keys)),
    )


def _blueprint_semantic_issue_has_deterministic_authority(
    issue: Any,
    blueprint: NarrativeBlueprint,
    source_text: str,
) -> bool:
    """Whether a typed one-sided finding has deterministic local authority.

    The shared validator accepts a reviewer sub-scope when every referenced
    node/source is covered by a server-derived delivery or state-subject issue.
    Its default ``True`` for ordinary craft findings is deliberately fenced
    out here. Environment misclassification is also excluded: that check only
    proves exact-unit scope, not the semantic identity of the state subject.
    """
    code = str(issue.code)
    if (
        code == "state_subject_environment_misclassified"
        or not code.startswith((
            "voice_identity_",
            "source_delivery_",
            "state_subject_",
        ))
    ):
        return False
    return blueprint_semantic_voice_issue_has_dialogue_authority(
        issue,
        blueprint,
        source_text,
    )


@dataclass
class _BlueprintReviewConsensus:
    issue_maps: list[dict[Any, Any]]
    consensus_keys: set[Any]
    consensus_issues: list[Any] = field(default_factory=list)
    non_consensus_issue_count: int = 0
    deterministic_authority_issues: list[Any] = field(default_factory=list)
    authoritative_issues: list[Any] = field(default_factory=list)
    non_authoritative_residual_issue_count: int = 0
    reviews_are_clean: bool = False
    needs_full_fallback: bool = False
    full_review_has_non_authoritative_residual: bool = False


def _blueprint_semantic_review_consensus(
    reviews: list[BlueprintSemanticReview],
    *,
    blueprint: NarrativeBlueprint,
    source_text: str,
    targeted_review: bool,
) -> _BlueprintReviewConsensus:
    issue_maps = [
        {
            (
                issue.code,
                tuple(sorted(issue.node_keys)),
                tuple(sorted(issue.source_unit_keys)),
            ): issue
            for issue in review.issues
            if issue.must_fix
        }
        for review in reviews
    ]
    consensus_keys = set(issue_maps[0]).intersection(issue_maps[1])
    consensus_issues = [
        issue_maps[0][key] for key in sorted(consensus_keys)
    ]
    non_consensus_issue_count = (
        sum(len(issue_map) for issue_map in issue_maps)
        - 2 * len(consensus_keys)
    )
    deterministic_authority_issues = sorted(
        (
            issue
            for issue_map in issue_maps
            for issue_key, issue in issue_map.items()
            if (
                issue_key not in consensus_keys
                and _blueprint_semantic_issue_has_deterministic_authority(
                    issue,
                    blueprint,
                    source_text,
                )
            )
        ),
        key=_blueprint_semantic_issue_exact_scope,
    )
    authoritative_issues = (
        consensus_issues + deterministic_authority_issues
    )
    non_authoritative_residual_issue_count = (
        non_consensus_issue_count
        - len(deterministic_authority_issues)
    )
    reviews_are_clean = not issue_maps[0] and not issue_maps[1]
    needs_full_fallback = bool(
        targeted_review
        and not authoritative_issues
        and non_authoritative_residual_issue_count
    )
    full_review_has_non_authoritative_residual = bool(
        not targeted_review
        and not authoritative_issues
        and non_authoritative_residual_issue_count
    )
    return _BlueprintReviewConsensus(
        issue_maps=issue_maps,
        consensus_keys=consensus_keys,
        consensus_issues=consensus_issues,
        non_consensus_issue_count=non_consensus_issue_count,
        deterministic_authority_issues=deterministic_authority_issues,
        authoritative_issues=authoritative_issues,
        non_authoritative_residual_issue_count=non_authoritative_residual_issue_count,
        reviews_are_clean=reviews_are_clean,
        needs_full_fallback=needs_full_fallback,
        full_review_has_non_authoritative_residual=full_review_has_non_authoritative_residual,
    )


def _create_insufficient_blueprint_review_artifact(
    round_inputs: _BlueprintReviewRoundInputs,
    *,
    episode: dict[str, Any],
    reviews: list[BlueprintSemanticReview],
    review_artifact_ids: list[str],
    dropped_voice_issue_counts: dict[int, int],
    review_source_corpus_hash: str,
    review_input_fingerprint: str,
) -> None:
    from app.evidence import repository as evidence_repository
    from app.harness.types import EvidenceArtifact
    from app.observability.tracing import current_trace

    trace = current_trace()
    evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_narrative_blueprint_review_consensus",
            scope_type="episode",
            scope_id=str(episode.get("id") or ""),
            status="needs_revision",
            trust_level="T1",
            content={
                "review_round": round_inputs.review_round,
                "blueprint_hash": round_inputs.current_blueprint_hash,
                "consensus_issue_keys": [],
                "non_consensus_issue_count": sum(
                    len(review.issues) for review in reviews
                ),
                "valid_review_sample_count": len(reviews),
                "unavailable_review_sample_count": 2 - len(reviews),
                "dropped_unsupported_voice_issue_count": sum(
                    dropped_voice_issue_counts.values()
                ),
            },
            parent_artifact_ids=review_artifact_ids,
            contract_version=BLUEPRINT_VERSION,
            prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
            model_snapshot={
                "review_policy_version": (
                    BLUEPRINT_SEMANTIC_REVIEW_POLICY_VERSION
                ),
                "authority_fingerprint": (
                    blueprint_authority_validator_fingerprint()
                ),
                "source_corpus_hash": review_source_corpus_hash,
                "review_input_fingerprint": review_input_fingerprint,
            },
        ),
        step_run_id=trace.step_run_id,
    )


def _create_blueprint_review_consensus_artifact(
    round_inputs: _BlueprintReviewRoundInputs,
    consensus: _BlueprintReviewConsensus,
    *,
    episode: dict[str, Any],
    review_artifact_ids: list[str],
    dropped_voice_issue_counts: dict[int, int],
    review_source_corpus_hash: str,
    review_input_fingerprint: str,
) -> dict[str, Any]:
    from app.evidence import repository as evidence_repository
    from app.harness.types import EvidenceArtifact
    from app.observability.tracing import current_trace

    trace = current_trace()
    return evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_narrative_blueprint_review_consensus",
            scope_type="episode",
            scope_id=str(episode.get("id") or ""),
            status=(
                "needs_revision"
                if consensus.needs_full_fallback or consensus.authoritative_issues
                else "validated"
            ),
            trust_level="T1",
            content={
                "review_round": round_inputs.review_round,
                "blueprint_hash": round_inputs.current_blueprint_hash,
                "consensus_issue_keys": [
                    {
                        "code": code,
                        "node_keys": list(node_keys),
                        "source_unit_keys": list(source_unit_keys),
                    }
                    for code, node_keys, source_unit_keys
                    in sorted(consensus.consensus_keys)
                ],
                "deterministic_authority_issue_keys": [
                    {
                        "code": issue.code,
                        "node_keys": sorted(issue.node_keys),
                        "source_segment_ids": sorted(
                            issue.source_segment_ids
                        ),
                        "source_unit_keys": sorted(
                            issue.source_unit_keys
                        ),
                    }
                    for issue in consensus.deterministic_authority_issues
                ],
                "authoritative_issue_count": len(
                    consensus.authoritative_issues
                ),
                "non_consensus_issue_count": consensus.non_consensus_issue_count,
                "non_authoritative_residual_issue_count": (
                    consensus.non_authoritative_residual_issue_count
                ),
                "dropped_unsupported_voice_issue_count": sum(
                    dropped_voice_issue_counts.values()
                ),
                "review_mode": "targeted" if round_inputs.targeted_review else "full",
                "review_outcome": (
                    "full_fallback_required"
                    if consensus.needs_full_fallback else
                    "consensus_issues"
                    if consensus.consensus_keys else
                    "deterministic_authority_issues"
                    if consensus.deterministic_authority_issues else
                    "non_authoritative_one_sided_residual"
                    if consensus.full_review_has_non_authoritative_residual else
                    "clean"
                ),
            },
            parent_artifact_ids=review_artifact_ids,
            contract_version=BLUEPRINT_VERSION,
            prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
            model_snapshot={
                "review_policy_version": (
                    BLUEPRINT_SEMANTIC_REVIEW_POLICY_VERSION
                ),
                "authority_fingerprint": (
                    blueprint_authority_validator_fingerprint()
                ),
                "source_corpus_hash": review_source_corpus_hash,
                "review_input_fingerprint": review_input_fingerprint,
            },
        ),
        step_run_id=trace.step_run_id,
    )
