"""ensure_structural_identity_coverage：保证一段剧本的结构化身份覆盖
回执存在且当前。
"""

from __future__ import annotations

import json
import sqlite3

from collections.abc import Callable

from app.evidence import repository as evidence_repository
from app.db import get_conn, get_setting
from app.errors import ContentGenerationError
from app.harness.types import EvidenceArtifact
from app.orchestration.state_machine import StateConflict
from app.schemas import Bible

from ._db_probe import _has_column
from .cards_ensure import ensure_cards_for_text
from .constants import (
    CURRENT_IDENTITY_LITERAL_PROVENANCE,
    CURRENT_IDENTITY_SYNTHETIC_PROVENANCE,
    IDENTITY_DISCOVERY_CONTRACT_VERSION,
    STRUCTURAL_IDENTITY_COVERAGE_VERSION,
)
from .discovery_fragments import _non_character_skip_key
from .discovery_resample import screenplay_identity_scope_fingerprint
from .evidence_receipt import _validate_current_identity_receipt_bundle
from .resolution_store import (
    load_screenplay_character_resolutions,
    load_screenplay_character_resolutions_for_source,
    persist_screenplay_character_resolutions,
    screenplay_character_resolutions_for_source,
)
from .structural_coverage import (
    _project_bible_character_names,
    _structural_identity_candidate_semantic_hash,
    _structural_identity_catalog_input_hash,
    _structural_identity_catalog_receipt_is_valid,
    _structural_identity_required_bible_names,
    _structural_identity_resolution_receipt,
    _structural_identity_resolution_receipt_is_valid,
)
from .structural_coverage_audit import audit_identity_coverage_from_structural_evidence

async def ensure_structural_identity_coverage(
    project_id: str,
    episode_id: str,
    episode_no: int,
    source_text: str,
    bible: Bible,
    structural_evidence: list[dict],
    *,
    write_guard: Callable[[], None] | None = None,
    expected_active_run_id: str | None = None,
    expected_revision_id: str | None = None,
) -> dict:
    """Materialize only identity gaps evidenced by a validated Blueprint/IR.

    This is the replacement for the old unconditional third full-chapter scan:
    current/future candidates are reused from the normalized discovery Artifact,
    and the model sees only unresolved typed references plus their owned SRC.
    """
    conn = get_conn()
    source_hash = evidence_repository.content_hash(source_text)
    identity_scope_fingerprint = screenplay_identity_scope_fingerprint(
        episode_no, source_text
    )
    structural_hash = evidence_repository.content_hash({
        "policy_version": STRUCTURAL_IDENTITY_COVERAGE_VERSION,
        "source_hash": source_hash,
        "structural_evidence": structural_evidence,
    })
    rows = conn.execute(
        """SELECT id,content_json,content_hash FROM artifacts
             WHERE scope_type='episode' AND scope_id=?
               AND type='screenplay_identity_discovery' AND status='validated'
             ORDER BY created_at DESC LIMIT 20""",
        (episode_id,),
    ).fetchall() if _has_column(conn, "artifacts", "content_hash") else []
    parsed_rows: list[tuple[sqlite3.Row, dict]] = []
    for row in rows:
        try:
            payload = json.loads(row["content_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if (
            isinstance(payload, dict)
            and str(row["content_hash"] or "").strip()
            and str(row["content_hash"] or "").strip()
            == evidence_repository.content_hash(payload)
        ):
            parsed_rows.append((row, payload))
    base_candidates: list[dict] = []
    parent_artifact_id = ""
    for row, payload in parsed_rows:
        if (
            payload.get("mode") != "structural_coverage"
            and payload.get("contract_version")
            == IDENTITY_DISCOVERY_CONTRACT_VERSION
            and payload.get("structural_coverage_policy_version")
            == STRUCTURAL_IDENTITY_COVERAGE_VERSION
            and payload.get("structural_coverage_applied") is False
            and payload.get("source_hash") == source_hash
            and isinstance(payload.get("candidates"), list)
        ):
            if any(not isinstance(item, dict) for item in payload["candidates"]):
                continue
            candidate_rows = [dict(item) for item in payload["candidates"]]
            typed_current_rows = [
                item for item in candidate_rows
                if str(item.get("source_label_provenance") or "").strip()
                in {
                    CURRENT_IDENTITY_LITERAL_PROVENANCE,
                    CURRENT_IDENTITY_SYNTHETIC_PROVENANCE,
                }
            ]
            if (
                candidate_rows
                and len(typed_current_rows) != len(candidate_rows)
            ):
                continue
            try:
                for item in typed_current_rows:
                    if _validate_current_identity_receipt_bundle(
                        item,
                        source_text=source_text,
                    ) is None:
                        raise ContentGenerationError(
                            "structural base current candidate 缺少 v2 receipt"
                        )
            except ContentGenerationError:
                continue
            base_candidates = candidate_rows
            parent_artifact_id = str(row["id"])
            break
    invalid_cached_resolution_keys: set[tuple[str, str, str]] = set()
    matching_coverage_artifact_seen = False

    def verified_materialized_bible_names(candidates: list[dict]) -> list[str]:
        required = [
            name for name in _structural_identity_required_bible_names(
                candidates
            )
            # 卡层已判定"不是角色"（宗门、器物，或出现在章末旁白里的作者笔名）。
            # 那是正确的拒绝，不能反过来要求它必须有人物卡——生产上 EP3 就是被
            # 「耳根」这个作者笔名整集卡死的。
            if not str(
                get_setting(_non_character_skip_key(project_id, name)) or ""
            ).strip()
        ]
        available = _project_bible_character_names(conn, project_id, bible)
        missing = set(required) - available
        if missing:
            raise ContentGenerationError(
                "结构人物 coverage 的 named card 尚未物化："
                + ",".join(sorted(missing))
            )
        return required
    for _row, payload in parsed_rows:
        if (
            not matching_coverage_artifact_seen
            and payload.get("mode") == "structural_coverage"
            and payload.get("contract_version")
            == IDENTITY_DISCOVERY_CONTRACT_VERSION
            and payload.get("policy_version")
            == STRUCTURAL_IDENTITY_COVERAGE_VERSION
            and payload.get("source_hash") == source_hash
            and payload.get("structural_evidence_hash") == structural_hash
            and isinstance(payload.get("candidates"), list)
        ):
            matching_coverage_artifact_seen = True
            required_keys = {
                (
                    str(item.get("source_label") or "").strip(),
                    str(item.get("identity_group") or "").strip(),
                    identity_scope_fingerprint,
                )
                for item in payload["candidates"]
                if (
                    isinstance(item, dict)
                    and str(item.get("source_label") or "").strip()
                    and str(item.get("identity_group") or "").strip()
                )
            }
            cached_resolutions = load_screenplay_character_resolutions(
                conn, episode_id
            )
            current_cached_resolutions = (
                screenplay_character_resolutions_for_source(
                    cached_resolutions,
                    episode_no=episode_no,
                    source_text=source_text,
                )
            )
            expected_receipt = payload.get("materialized_resolution_receipt")
            try:
                expected_candidate_hash = str(
                    payload.get("candidate_semantic_hash") or ""
                )
                required_bible_names = (
                    _structural_identity_required_bible_names(
                        payload["candidates"]
                    )
                )
                current_bible_names = _project_bible_character_names(
                    conn, project_id, bible
                )
                actual_receipt = _structural_identity_resolution_receipt(
                    current_cached_resolutions,
                    candidates=payload["candidates"],
                    identity_scope_fingerprint=identity_scope_fingerprint,
                )
                actual_catalog_input_hash = (
                    _structural_identity_catalog_input_hash(
                        bible=bible,
                        base_candidates=base_candidates,
                        structural_evidence_hash=structural_hash,
                        existing_resolutions=current_cached_resolutions,
                        output_candidates=payload["candidates"],
                    )
                )
                cache_is_exact = bool(
                    expected_candidate_hash
                    and expected_candidate_hash
                    == _structural_identity_candidate_semantic_hash(
                        payload["candidates"]
                    )
                    and _structural_identity_resolution_receipt_is_valid(
                        expected_receipt
                    )
                    and expected_receipt == actual_receipt
                    and payload.get("materialized_bible_names")
                    == required_bible_names
                    and set(required_bible_names) <= current_bible_names
                    and str(payload.get("coverage_catalog_input_hash") or "")
                    == actual_catalog_input_hash
                    and _structural_identity_catalog_receipt_is_valid(
                        payload.get("coverage_catalog_receipt")
                    )
                )
            except (ContentGenerationError, TypeError, ValueError, KeyError):
                # A validated cache may be stale or corrupt.  Recovery must
                # re-audit once instead of making that bad marker permanently
                # sticky across retries.
                cache_is_exact = False
            if cache_is_exact:
                # A validated receipt can coexist with unrelated legacy rows.
                # Retire those rows at the successful recovery boundary before
                # exposing any authority to the screenplay compiler.
                persisted = persist_screenplay_character_resolutions(
                    conn,
                    episode_id,
                    [],
                    expected_active_run_id=expected_active_run_id,
                    expected_revision_id=expected_revision_id,
                    retire_stale_identity_scope_fingerprint=(
                        identity_scope_fingerprint
                    ),
                )
                persisted = screenplay_character_resolutions_for_source(
                    persisted,
                    episode_no=episode_no,
                    source_text=source_text,
                )
                if expected_receipt != _structural_identity_resolution_receipt(
                    persisted,
                    candidates=payload["candidates"],
                    identity_scope_fingerprint=identity_scope_fingerprint,
                ):
                    raise StateConflict(
                        "screenplay_identity_resolution_receipt",
                        episode_id,
                        {str(expected_receipt.get("hash") or "")},
                        "changed-during-cache-recovery",
                    )
                return {
                    "checked": 0,
                    "candidates": payload["candidates"],
                    "added": [],
                    "resolutions": persisted,
                    "errors": [],
                    "warnings": [],
                    "reused": True,
                }
            invalid_cached_resolution_keys.update(required_keys)
    existing_coverage_resolutions = [
        item
        for item in load_screenplay_character_resolutions_for_source(
            conn,
            episode_id,
            episode_no=episode_no,
            source_text=source_text,
        )
        if (
            str(item.get("source_label") or "").strip(),
            str(item.get("identity_group") or "").strip(),
            str(item.get("identity_scope_fingerprint") or "").strip(),
        ) not in invalid_cached_resolution_keys
    ]
    coverage_catalog_receipt: dict[str, object] = {}
    audited = await audit_identity_coverage_from_structural_evidence(
        base_candidates,
        structural_evidence=structural_evidence,
        source_text=source_text,
        bible=bible,
        episode_no=episode_no,
        existing_resolutions=existing_coverage_resolutions,
        catalog_receipt=coverage_catalog_receipt,
    )
    coverage_catalog_input_hash = _structural_identity_catalog_input_hash(
        bible=bible,
        base_candidates=base_candidates,
        structural_evidence_hash=structural_hash,
        existing_resolutions=existing_coverage_resolutions,
        output_candidates=audited,
    )
    if write_guard:
        write_guard()
    base_keys = {
        (
            str(item.get("source_label") or "").strip(),
            str(item.get("identity_group") or "").strip(),
        )
        for item in base_candidates
    }
    additions = [
        item for item in audited
        if (
            str(item.get("source_label") or "").strip(),
            str(item.get("identity_group") or "").strip(),
        ) not in base_keys
    ]
    recovery_candidates = [
        item
        for item in audited
        if (
            str(item.get("source_label") or "").strip(),
            str(item.get("identity_group") or "").strip(),
            identity_scope_fingerprint,
        ) in invalid_cached_resolution_keys
    ]
    materialization_candidates = list({
        (
            str(item.get("source_label") or "").strip(),
            str(item.get("identity_group") or "").strip(),
        ): item
        for item in [*additions, *recovery_candidates]
    }.values())
    if not materialization_candidates:
        if write_guard:
            write_guard()
        persisted = persist_screenplay_character_resolutions(
            conn,
            episode_id,
            [],
            expected_active_run_id=expected_active_run_id,
            expected_revision_id=expected_revision_id,
            retire_stale_structural_identity_policy=(
                STRUCTURAL_IDENTITY_COVERAGE_VERSION
            ),
            retire_stale_identity_scope_fingerprint=(
                identity_scope_fingerprint
            ),
            retire_automatic_identity_keys=invalid_cached_resolution_keys,
        )
        persisted = screenplay_character_resolutions_for_source(
            persisted,
            episode_no=episode_no,
            source_text=source_text,
        )
        materialized_bible_names = verified_materialized_bible_names(audited)
        if write_guard:
            write_guard()
        trace = None
        try:
            from app.observability.tracing import current_trace

            trace = current_trace()
        except Exception:  # noqa: BLE001
            pass
        raw_artifact = evidence_repository.create_artifact(
            EvidenceArtifact(
                type="screenplay_identity_discovery_raw",
                scope_type="episode",
                scope_id=episode_id,
                status="candidate",
                trust_level="T0",
                content={
                    "mode": "structural_coverage",
                    "policy_version": STRUCTURAL_IDENTITY_COVERAGE_VERSION,
                    "structural_evidence_hash": structural_hash,
                    "coverage_catalog_input_hash": (
                        coverage_catalog_input_hash
                    ),
                    "coverage_catalog_receipt": coverage_catalog_receipt,
                    "model_candidates": [],
                },
                parent_artifact_ids=[parent_artifact_id] if parent_artifact_id else [],
                contract_version=IDENTITY_DISCOVERY_CONTRACT_VERSION,
            ),
            step_run_id=getattr(trace, "step_run_id", None),
        )
        evidence_repository.create_artifact(
            EvidenceArtifact(
                type="screenplay_identity_discovery",
                scope_type="episode",
                scope_id=episode_id,
                status="validated",
                trust_level="T1",
                content={
                    "contract_version": IDENTITY_DISCOVERY_CONTRACT_VERSION,
                    "episode_no": episode_no,
                    "mode": "structural_coverage",
                    "policy_version": STRUCTURAL_IDENTITY_COVERAGE_VERSION,
                    "candidates": audited,
                    "candidate_semantic_hash": (
                        _structural_identity_candidate_semantic_hash(audited)
                    ),
                    "materialized_resolution_receipt": (
                        _structural_identity_resolution_receipt(
                            persisted,
                            candidates=audited,
                            identity_scope_fingerprint=(
                                identity_scope_fingerprint
                            ),
                        )
                    ),
                    "materialized_bible_names": materialized_bible_names,
                    "source_hash": source_hash,
                    "structural_evidence_hash": structural_hash,
                    "coverage_catalog_input_hash": (
                        coverage_catalog_input_hash
                    ),
                    "coverage_catalog_receipt": coverage_catalog_receipt,
                },
                parent_artifact_ids=[raw_artifact["id"]],
                contract_version=IDENTITY_DISCOVERY_CONTRACT_VERSION,
            ),
            step_run_id=getattr(trace, "step_run_id", None),
        )
        return {
            "checked": 0,
            "candidates": audited,
            "added": [],
            "resolutions": persisted,
            "errors": [],
            "warnings": [],
        }
    result = await ensure_cards_for_text(
        project_id,
        episode_no,
        source_text,
        bible,
        generate_portraits=False,
        _precomputed_candidates=materialization_candidates,
        write_guard=write_guard,
    )
    if write_guard:
        write_guard()
    if result.get("errors"):
        # Provider/schema validation is not the materialization boundary.  A
        # card failure (or any downstream identity error) must never mint a
        # validated coverage Artifact with an empty receipt: that would turn
        # the next run into a false cache success and bypass the identity gate.
        result["resolutions"] = screenplay_character_resolutions_for_source(
            result.get("resolutions") or [],
            episode_no=episode_no,
            source_text=source_text,
        )
        return result
    persisted = persist_screenplay_character_resolutions(
        conn,
        episode_id,
        result.get("resolutions") or [],
        expected_active_run_id=expected_active_run_id,
        expected_revision_id=expected_revision_id,
        retire_stale_structural_identity_policy=(
            STRUCTURAL_IDENTITY_COVERAGE_VERSION
        ),
        retire_stale_identity_scope_fingerprint=identity_scope_fingerprint,
        retire_automatic_identity_keys=invalid_cached_resolution_keys,
    )
    persisted = screenplay_character_resolutions_for_source(
        persisted,
        episode_no=episode_no,
        source_text=source_text,
    )
    materialized_bible_names = verified_materialized_bible_names(audited)
    if write_guard:
        write_guard()
    result["resolutions"] = persisted
    trace = None
    try:
        from app.observability.tracing import current_trace

        trace = current_trace()
    except Exception:  # noqa: BLE001
        pass
    raw_artifact = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_identity_discovery_raw",
            scope_type="episode",
            scope_id=episode_id,
            status="candidate",
            trust_level="T0",
            content={
                "mode": "structural_coverage",
                "policy_version": STRUCTURAL_IDENTITY_COVERAGE_VERSION,
                "structural_evidence_hash": structural_hash,
                "coverage_catalog_input_hash": coverage_catalog_input_hash,
                "coverage_catalog_receipt": coverage_catalog_receipt,
                "model_candidates": materialization_candidates,
            },
            parent_artifact_ids=[parent_artifact_id] if parent_artifact_id else [],
            contract_version=IDENTITY_DISCOVERY_CONTRACT_VERSION,
        ),
        step_run_id=getattr(trace, "step_run_id", None),
    )
    evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_identity_discovery",
            scope_type="episode",
            scope_id=episode_id,
            status="validated",
            trust_level="T1",
            content={
                "contract_version": IDENTITY_DISCOVERY_CONTRACT_VERSION,
                "episode_no": episode_no,
                "mode": "structural_coverage",
                "policy_version": STRUCTURAL_IDENTITY_COVERAGE_VERSION,
                "candidates": audited,
                "candidate_semantic_hash": (
                    _structural_identity_candidate_semantic_hash(audited)
                ),
                "materialized_resolution_receipt": (
                    _structural_identity_resolution_receipt(
                        persisted,
                        candidates=audited,
                        identity_scope_fingerprint=identity_scope_fingerprint,
                    )
                ),
                "materialized_bible_names": materialized_bible_names,
                "source_hash": source_hash,
                "structural_evidence_hash": structural_hash,
                "coverage_catalog_input_hash": coverage_catalog_input_hash,
                "coverage_catalog_receipt": coverage_catalog_receipt,
            },
            parent_artifact_ids=[raw_artifact["id"]],
            contract_version=IDENTITY_DISCOVERY_CONTRACT_VERSION,
        ),
        step_run_id=getattr(trace, "step_run_id", None),
    )
    return result

