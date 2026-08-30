"""叙事蓝图语义双审——审稿会话指纹、缓存复用与权威产物持久化。

从 ``blueprint_semantic_review.py`` 拆出三段原来内联在 ``_semantic_review_
narrative_blueprint`` 里的逻辑：

* ``_blueprint_semantic_review_fingerprints`` —— 计算蓝图/来源/输入指纹（原函数
  顶部的三段 hash 计算）。
* ``_reuse_cached_blueprint_semantic_review`` —— 命中已缓存的「无权威问题」共识
  产物时直接复用，不重新起审稿调用（原来的 ``cached_rows`` 循环）。命中时返回
  该产物 id；未命中返回 ``None``，调用方据此决定是否继续正常审稿流程。
* ``_persist_reviewed_blueprint_authority`` —— 原来的嵌套函数
  ``persist_reviewed_authority``，改成显式接收 ``blueprint``/``episode``/
  ``source_text``/``generation_budget`` 参数，不再依赖闭包捕获。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from app.db import get_conn
from app.narrative_blueprint import (
    BLUEPRINT_VERSION,
    NarrativeBlueprint,
    blueprint_authority_validator_fingerprint,
)

from .blueprint_budget import _BlueprintGenerationBudget
from .constants import (
    BLUEPRINT_SEMANTIC_REVIEW_POLICY_VERSION,
    SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
)
from .ir_snapshot import _artifact_json_content_is_sealed, _current_blueprint_authority_snapshot


def _blueprint_semantic_review_fingerprints(
    blueprint: NarrativeBlueprint,
    episode: dict[str, Any],
    source_text: str,
) -> tuple[str, str, str]:
    """返回 (initial_blueprint_hash, review_source_corpus_hash, review_input_fingerprint)。"""
    initial_blueprint_hash = hashlib.sha256(
        json.dumps(
            blueprint.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    review_source_corpus_hash = hashlib.sha256(
        source_text.encode("utf-8")
    ).hexdigest()
    review_input_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "episode_id": str(episode.get("id") or ""),
                "blueprint_hash": initial_blueprint_hash,
                "source_corpus_hash": review_source_corpus_hash,
                "review_policy_version": BLUEPRINT_SEMANTIC_REVIEW_POLICY_VERSION,
                "authority_fingerprint": blueprint_authority_validator_fingerprint(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return initial_blueprint_hash, review_source_corpus_hash, review_input_fingerprint


def _reuse_cached_blueprint_semantic_review(
    episode: dict[str, Any],
    *,
    initial_blueprint_hash: str,
    review_source_corpus_hash: str,
    review_input_fingerprint: str,
) -> str | None:
    """命中可复用的「无权威问题」历史共识产物时返回其 artifact id，否则 None。"""
    cached_rows = get_conn().execute(
        """SELECT id,content_json,content_hash,model_snapshot_json
             FROM artifacts
            WHERE scope_type='episode' AND scope_id=?
              AND type='screenplay_narrative_blueprint_review_consensus'
              AND status='validated'
              AND contract_version=? AND prompt_version=?
            ORDER BY created_at DESC LIMIT 20""",
        (
            str(episode.get("id") or ""),
            BLUEPRINT_VERSION,
            SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
        ),
    ).fetchall()
    for row in cached_rows:
        try:
            cached = json.loads(row["content_json"] or "{}")
            if not _artifact_json_content_is_sealed(row, cached):
                continue
            cached_snapshot = json.loads(
                row["model_snapshot_json"] or "{}"
            )
            cached_authoritative_issue_count = int(
                cached.get("authoritative_issue_count")
            )
            cached_residual_issue_count = int(
                cached.get("non_authoritative_residual_issue_count")
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        cached_outcome = str(cached.get("review_outcome") or "")
        reusable_no_authority_outcome = bool(
            (
                cached_outcome == "clean"
                and cached_residual_issue_count == 0
            )
            or (
                cached_outcome
                == "non_authoritative_one_sided_residual"
                and cached.get("review_mode") == "full"
                and cached_residual_issue_count > 0
            )
        )
        if (
            cached.get("blueprint_hash") == initial_blueprint_hash
            and not cached.get("consensus_issue_keys")
            and not cached.get("deterministic_authority_issue_keys")
            and cached_authoritative_issue_count == 0
            and reusable_no_authority_outcome
            and cached_snapshot.get("review_policy_version")
            == BLUEPRINT_SEMANTIC_REVIEW_POLICY_VERSION
            and cached_snapshot.get("authority_fingerprint")
            == blueprint_authority_validator_fingerprint()
            and cached_snapshot.get("source_corpus_hash")
            == review_source_corpus_hash
            and cached_snapshot.get("review_input_fingerprint")
            == review_input_fingerprint
        ):
            return str(row["id"])
    return None


def _persist_reviewed_blueprint_authority(
    blueprint: NarrativeBlueprint,
    *,
    episode: dict[str, Any],
    source_text: str,
    generation_budget: _BlueprintGenerationBudget | None,
    parent_artifact_ids: list[str] | None = None,
) -> None:
    """Persist reviewed authority, then terminalize old unknown retries.

    The artifact commit deliberately happens first.  A crash between the
    two writes leaves the historical provider outcome unresolved (safe);
    the inverse state -- resolving without durable reviewed authority --
    is impossible.
    """
    from app.evidence import repository as evidence_repository
    from app.harness.types import EvidenceArtifact
    from app.observability.tracing import current_trace

    episode_id = str(episode.get("id") or "")
    trace = current_trace()
    run_id = str(trace.run_id or "")
    if not episode_id or not run_id:
        return
    content = blueprint.model_dump(mode="json")
    content_digest = evidence_repository.content_hash(content)
    source_digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    existing = get_conn().execute(
        """SELECT id FROM artifacts
             WHERE type='screenplay_narrative_blueprint'
               AND scope_type='episode' AND scope_id=?
               AND status='validated' AND content_hash=?
               AND contract_version=? AND prompt_version=?
               AND json_extract(
                   model_snapshot_json,'$.generation_mode'
               )='semantic_reviewed'
               AND json_extract(
                   model_snapshot_json,'$.source_corpus_hash'
               )=?
               AND json_extract(
                   model_snapshot_json,'$.review_policy_version'
               )=?
             ORDER BY created_at DESC LIMIT 1""",
        (
            episode_id,
            content_digest,
            BLUEPRINT_VERSION,
            SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
            source_digest,
            BLUEPRINT_SEMANTIC_REVIEW_POLICY_VERSION,
        ),
    ).fetchone()
    if existing is None:
        evidence_repository.create_artifact(
            EvidenceArtifact(
                type="screenplay_narrative_blueprint",
                scope_type="episode",
                scope_id=episode_id,
                status="validated",
                trust_level="T1",
                content=content,
                parent_artifact_ids=list(parent_artifact_ids or []),
                contract_version=BLUEPRINT_VERSION,
                prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
                model_snapshot=_current_blueprint_authority_snapshot(
                    source_text,
                    generation_mode="semantic_reviewed",
                    generation_budget=generation_budget,
                ),
            ),
            step_run_id=trace.step_run_id,
        )

    # Historical unknown provider outcomes are resolved only after this
    # reviewed artifact has been selected as current authority and written
    # into the active revision checkpoint by the downstream boundary.
