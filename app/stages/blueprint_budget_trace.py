"""叙事蓝图分片重试预算——重试回执哈希、审计追踪与叶子缓存失效判定。"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any


from app.db import get_conn
from app.source_facts import (
    source_segment_facts,
)

from .blueprint_budget import _BlueprintGenerationBudget
from .common import StageError


def blueprint_retry_receipts_hash(receipts: list[dict[str, Any]]) -> str:
    """Canonical authority binding for one explicit unknown-retry grant."""
    raw = json.dumps(
        receipts,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _blueprint_generation_budget_for_trace(
    trace: Any,
    *,
    episode_id: str = "",
) -> _BlueprintGenerationBudget:
    run_id = getattr(trace, "run_id", None)
    run_started_at: float | None = None
    input_fingerprint = ""
    retry_grant_id = ""
    retry_receipts_hash = ""
    if run_id:
        run_row = get_conn().execute(
            "SELECT started_at,input_fingerprint,config_snapshot_json "
            "FROM workflow_runs WHERE id=?",
            (run_id,),
        ).fetchone()
        if run_row is not None:
            run_started_at = run_row["started_at"]
            input_fingerprint = str(run_row["input_fingerprint"] or "")
            try:
                config_snapshot = json.loads(
                    run_row["config_snapshot_json"] or "{}"
                )
                if episode_id and not str(
                    config_snapshot.get(
                        "blueprint_budget_lineage_fingerprint"
                    ) or ""
                ):
                    raise StageError(
                        "剧本时空因果蓝图分片",
                        [
                            "[BLUEPRINT_BUDGET_SNAPSHOT_INVALID] "
                            "运行缺少冻结的蓝图预算 lineage"
                        ],
                    )
                retry_grant_id = str(
                    config_snapshot.get("blueprint_retry_grant_id") or ""
                )
                retry_receipts_hash = str(
                    config_snapshot.get("blueprint_retry_receipts_hash") or ""
                )
                input_fingerprint = str(
                    config_snapshot.get(
                        "blueprint_budget_lineage_fingerprint",
                        input_fingerprint,
                    )
                    or input_fingerprint
                )
            except StageError:
                raise
            except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
                if episode_id:
                    raise StageError(
                        "剧本时空因果蓝图分片",
                        [
                            "[BLUEPRINT_BUDGET_SNAPSHOT_INVALID] "
                            "运行蓝图预算 snapshot 损坏"
                        ],
                    ) from exc
    if episode_id and not retry_grant_id:
        try:
            grant_row = get_conn().execute(
                """SELECT r.grant_id
                     FROM production_revisions r
                     JOIN production_grants g ON g.id=r.grant_id
                    WHERE r.episode_id=? AND r.kind='screenplay'
                      AND r.status='active'
                      AND g.revoked_at IS NULL AND g.expires_at>?
                    ORDER BY r.updated_at DESC LIMIT 1""",
                (episode_id, time.time()),
            ).fetchone()
            if grant_row is not None:
                retry_grant_id = str(grant_row["grant_id"] or "")
        except Exception:  # noqa: BLE001 - isolated legacy test schemas
            retry_grant_id = ""
    budget = _BlueprintGenerationBudget.from_durable_calls(
        run_id=run_id,
        started_at_epoch=run_started_at,
        episode_id=episode_id,
        input_fingerprint=input_fingerprint,
        retry_grant_id=retry_grant_id,
    )
    if retry_grant_id and budget.unknown_receipts:
        try:
            # Authorize on the authority facts frozen atomically at activation,
            # NOT on whether the grant's original revision is still the head.
            # ``_spawn_screenplay_activation`` mints this ``user_retry_approval``
            # grant, consumes it, and freezes ``blueprint_retry_grant_id`` /
            # ``blueprint_retry_receipts_hash`` into the run snapshot in one
            # transaction. The baseline task legitimately supersedes that
            # revision (unstable ``input_fingerprint``), which is orthogonal to
            # authority. Requiring ``r.status='active'`` here deadlocked every
            # retry (BLUEPRINT_PROVIDER_RETRY_GRANT_REQUIRED). Run-scope, the
            # covered receipt set and the input lineage are each frozen and
            # re-verified below; revocation/TTL and single-use (``consumed_at``)
            # stay enforced, so dropping the revision-head join is safe.
            grant_row = get_conn().execute(
                """SELECT g.issued_by,g.input_artifact_hash
                     FROM production_grants g
                    WHERE g.id=? AND g.episode_id=? AND g.kind='screenplay'
                      AND g.issued_by='user_retry_approval'
                      AND g.consumed_at IS NOT NULL
                      AND g.revoked_at IS NULL AND g.expires_at>?""",
                (retry_grant_id, episode_id, time.time()),
            ).fetchone()
            if (
                grant_row is not None
                and str(grant_row["issued_by"] or "") == "user_retry_approval"
                and str(grant_row["input_artifact_hash"] or "")
                == blueprint_retry_receipts_hash(budget.unknown_receipts)
                and (
                    not retry_receipts_hash
                    or retry_receipts_hash
                    == blueprint_retry_receipts_hash(budget.unknown_receipts)
                )
            ):
                budget.authorize_unknown_retry(retry_grant_id)
        except Exception:  # noqa: BLE001 - isolated legacy schemas
            pass
    return budget


def _cached_leaf_superseded_by_feedback(
    *,
    cached_source_hash: str,
    source_hash: str,
    source_payload: list[dict[str, Any]],
) -> bool:
    """Whether a cached leaf was built before this activation changed its input.

    A semantic rebuild deliberately injects ``downstream_semantic_conflicts``
    into the affected shard's source payload, which changes ``source_hash`` on
    purpose.  The previous leaf is then simply *not applicable* -- it is not
    authority drift.  Treating it as drift makes the rebuild die in
    ``BLUEPRINT_SPLIT_MANIFEST_AUTHORITY``, i.e. exactly inside the scenario the
    rebuild exists to rescue.  Shards without injected evidence keep the
    original strict drift check unchanged.
    """
    if cached_source_hash == source_hash:
        return False
    return any(
        item.get("downstream_semantic_conflicts")
        for item in source_payload
    )


def _blueprint_shard_source_entry(
    segment: Any,
    semantic_feedback: dict[str, list[str]] | None,
) -> dict[str, Any]:
    """One shard source entry, carrying any downstream semantic dead-end.

    The feedback rides inside the source payload on purpose: that payload is
    hashed into ``source_hash``, so a rebuild neither reuses the cached leaf nor
    replays the same provider operation, and it is serialised into the prompt,
    so the model sees exactly which unit could not be rendered and why.
    """
    entry: dict[str, Any] = {
        "source_segment_id": segment.segment_id,
        "text": segment.text,
        "source_facts": [
            fact.model_dump(mode="json")
            for fact in source_segment_facts(
                segment.segment_id,
                segment.text,
            )
        ],
    }
    prefix = f"{segment.segment_id}:"
    conflicts = {
        unit_key: list(messages)
        for unit_key, messages in (semantic_feedback or {}).items()
        if unit_key.startswith(prefix)
    }
    if conflicts:
        entry["downstream_semantic_conflicts"] = conflicts
    return entry
