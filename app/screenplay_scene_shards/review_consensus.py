"""Peer-review finding normalization and dual-reviewer consensus for scene-shard
semantic review: reference-scope validation, finding/issue signatures, unit-key
canonicalization, worst-case token accounting, and the consensus merge used to
decide whether two reviewers agree.

Moved verbatim from ``app/screenplay_scene_shards.py`` (see
``app/screenplay_scene_shards/__init__.py`` for the full re-export map).
"""
from __future__ import annotations

import json
import math
import re
from pydantic import ValidationError

from .constants import (
    SCREENPLAY_SCENE_SEMANTIC_FINDING_CODES,
    SCREENPLAY_SCENE_SEMANTIC_FINDING_MESSAGE_MAX_CHARS,
    SCREENPLAY_SCENE_SEMANTIC_REPAIR_MIN_OUTPUT_TOKENS,
    SCREENPLAY_SCENE_SEMANTIC_REPAIR_ROOT_RESERVE_PERCENT,
    SCREENPLAY_SCENE_SEMANTIC_REVIEW_MIN_OUTPUT_TOKENS,
    SCREENPLAY_SCENE_SEMANTIC_REVIEW_OUTPUT_RESERVE_PERCENT,
    SCREENPLAY_SCENE_SEMANTIC_VIOLATION_KINDS,
    _SCREENPLAY_SCENE_SEMANTIC_OPTIONAL_UNIT_KINDS,
)
from .models import (
    ScreenplaySceneShardSemanticFinding,
    ScreenplaySceneShardSemanticReview,
)


def _scene_shard_finding_allows_omitted_unit_key(
    finding: ScreenplaySceneShardSemanticFinding,
) -> bool:
    """Whether a blank local observation may be denied repair authority.

    These finding kinds can be observed without safely identifying a repair
    target. A blank reference is never treated as global scope: it must either
    be uniquely aligned with the peer review or be removed before audit and
    consensus. Subject, contradiction, and cross-slot findings always require
    explicit deterministic scope.
    """
    kinds = set(finding.violation_kinds)
    return bool(kinds) and (
        finding.code == "source_semantic_drift"
        and not finding.related_unit_keys
        and kinds.issubset(
            _SCREENPLAY_SCENE_SEMANTIC_OPTIONAL_UNIT_KINDS
        )
    )


def _scene_shard_review_reference_errors(
    review: ScreenplaySceneShardSemanticReview,
    known_unit_keys: set[str],
    *,
    allow_local_omitted_unit_key: bool,
) -> list[str]:
    finding_limit = (
        len(known_unit_keys) * len(SCREENPLAY_SCENE_SEMANTIC_FINDING_CODES)
    )
    unknown_finding_keys = {
        finding.unit_key
        for finding in review.findings
        if finding.unit_key and finding.unit_key not in known_unit_keys
    }
    unknown_related_keys = {
        related_unit_key
        for finding in review.findings
        for related_unit_key in finding.related_unit_keys
        if related_unit_key and related_unit_key not in known_unit_keys
    }
    missing_finding_scopes = [
        finding
        for finding in review.findings
        if (
            not finding.unit_key
            and not (
                allow_local_omitted_unit_key
                and _scene_shard_finding_allows_omitted_unit_key(finding)
            )
        )
    ]
    missing_related_scopes = [
        finding
        for finding in review.findings
        if any(not unit_key for unit_key in finding.related_unit_keys)
    ]
    errors: list[str] = []
    if len(review.findings) > finding_limit:
        errors.append(
            "语义审查 findings 超过当前 chunk 的确定性上限："
            f"actual={len(review.findings)}，limit={finding_limit}"
        )
    if missing_finding_scopes:
        errors.append(
            "语义审查 finding 缺少必需 unit_key scope："
            + ",".join(sorted({
                finding.code for finding in missing_finding_scopes
            }))
        )
    if missing_related_scopes:
        errors.append(
            "语义审查 finding 缺少必需 related_unit_key scope："
            + ",".join(sorted({
                finding.code for finding in missing_related_scopes
            }))
        )
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


def _scene_shard_review_finding_signature(
    finding: ScreenplaySceneShardSemanticFinding,
) -> tuple[str, str, tuple[str, ...], tuple[str, ...]]:
    return (
        finding.unit_key,
        finding.code,
        tuple(finding.violation_kinds),
        tuple(finding.related_unit_keys),
    )


def _scene_shard_review_issue_signature(
    finding: ScreenplaySceneShardSemanticFinding,
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    return (
        finding.code,
        tuple(finding.violation_kinds),
        tuple(finding.related_unit_keys),
    )


def _scene_shard_normalize_peer_review_unit_scopes(
    reviews: list[ScreenplaySceneShardSemanticReview],
    known_unit_keys: set[str],
) -> list[str]:
    """Resolve one peer-proven blank scope, then deny unresolved blanks authority."""
    blank_findings = [
        (review_index, finding)
        for review_index, review in enumerate(reviews)
        for finding in review.findings
        if not finding.unit_key
    ]
    invalid_blank_findings = [
        finding
        for _, finding in blank_findings
        if not _scene_shard_finding_allows_omitted_unit_key(finding)
    ]
    if invalid_blank_findings:
        return _scene_shard_review_reference_errors(
            ScreenplaySceneShardSemanticReview(
                findings=invalid_blank_findings,
            ),
            known_unit_keys,
            allow_local_omitted_unit_key=False,
        )

    aligned = False
    if len(reviews) == 2 and len(blank_findings) == 1:
        blank_review_index, blank_finding = blank_findings[0]
        peer_review = reviews[1 - blank_review_index]
        blank_scoped_signatures = {
            _scene_shard_review_finding_signature(finding)
            for finding in reviews[blank_review_index].findings
            if finding.unit_key
        }
        peer_by_signature = {
            _scene_shard_review_finding_signature(finding): finding
            for finding in peer_review.findings
        }
        peer_only_signatures = (
            set(peer_by_signature) - blank_scoped_signatures
        )
        blank_only_signatures = (
            blank_scoped_signatures - set(peer_by_signature)
        )
        if not blank_only_signatures and len(peer_only_signatures) == 1:
            peer_candidate = peer_by_signature[
                next(iter(peer_only_signatures))
            ]
            if (
                peer_candidate.unit_key in known_unit_keys
                and _scene_shard_review_issue_signature(peer_candidate)
                == _scene_shard_review_issue_signature(blank_finding)
            ):
                blank_finding.unit_key = peer_candidate.unit_key
                aligned = True

    if blank_findings and not aligned:
        for review in reviews:
            review.findings = [
                finding
                for finding in review.findings
                if finding.unit_key
            ]

    errors: list[str] = []
    for review in reviews:
        try:
            normalized = ScreenplaySceneShardSemanticReview.model_validate(
                review.model_dump(mode="json"),
            )
        except ValidationError as exc:
            errors.append(
                "语义审查 unit scope 规范化后的 finding 合同无效："
                + str(exc)
            )
            continue
        review.findings = normalized.findings
        errors.extend(_scene_shard_review_reference_errors(
            review,
            known_unit_keys,
            allow_local_omitted_unit_key=False,
        ))
    return errors


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
    *,
    output_reserve_percent: int = (
        SCREENPLAY_SCENE_SEMANTIC_REVIEW_OUTPUT_RESERVE_PERCENT
    ),
) -> int:
    payload = screenplay_scene_semantic_review_worst_case_payload(
        unit_keys
    )
    compact_required = _screenplay_scene_semantic_token_estimate(
        len(payload)
    )
    if compact_required <= SCREENPLAY_SCENE_SEMANTIC_REVIEW_MIN_OUTPUT_TOKENS:
        # Do not make ordinary 1-2 unit reviews pay the large-review reserve.
        return SCREENPLAY_SCENE_SEMANTIC_REVIEW_MIN_OUTPUT_TOKENS
    pretty_required = _screenplay_scene_semantic_token_estimate(
        len(json.dumps(json.loads(payload), ensure_ascii=False, indent=2))
    )
    bounded_reserve_percent = max(
        0,
        min(200, int(output_reserve_percent)),
    )
    reserved_required = math.ceil(
        compact_required * (100 + bounded_reserve_percent) / 100
    )
    return max(
        SCREENPLAY_SCENE_SEMANTIC_REVIEW_MIN_OUTPUT_TOKENS,
        pretty_required,
        reserved_required,
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
