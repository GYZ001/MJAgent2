"""角色候选发现的对外入口 discover_character_candidates：
整合 legacy 流程、current/future 候选抽取与结构化覆盖审计。
"""

from __future__ import annotations

import json

from app.evidence import repository as evidence_repository
from app.db import get_conn, get_setting
from app.errors import ContentGenerationError
from app.harness.types import EvidenceArtifact
from app.schemas import Bible

from ._db_probe import _has_column
from .constants import (
    CURRENT_IDENTITY_DECISION_VERSION,
    CURRENT_IDENTITY_LITERAL_PROVENANCE,
    CURRENT_IDENTITY_SYNTHETIC_PROVENANCE,
    IDENTITY_DISCOVERY_CONTRACT_VERSION,
    STRUCTURAL_IDENTITY_COVERAGE_VERSION,
)
from .discovery_legacy import _discover_character_candidates_legacy
from .evidence_catalog import _current_identity_evidence_catalog_hash
from .evidence_receipt import (
    _attach_candidate_source_evidence,
    extract_current_identity_candidates,
)
from .future_identity_resolution import resolve_future_identity_candidates
from .structural_coverage_audit import audit_identity_coverage_from_structural_evidence

async def discover_character_candidates(
    source_text: str,
    bible: Bible,
    episode_no: int,
    *,
    draft_text: str = "",
    future_text: str = "",
    future_label: str = "",
    existing_resolutions: list[dict] | None = None,
    structural_evidence: list[dict] | None = None,
    scope_id: str | None = None,
    project_id: str | None = None,
) -> list[dict]:
    """Targeted identity pipeline: current, unresolved future, typed audit."""
    artifact_scope_id = str(scope_id or f"episode-{episode_no}")
    targeted = str(
        get_setting("screenplay_targeted_identity_enabled") or "true"
    ).strip().lower() not in {"0", "false", "off", "no"}
    structural_coverage_applied = bool(
        targeted and structural_evidence
    )
    discovery_input = {
        "contract_version": IDENTITY_DISCOVERY_CONTRACT_VERSION,
        "current_identity_version": CURRENT_IDENTITY_DECISION_VERSION,
        "current_evidence_catalog_hash": (
            _current_identity_evidence_catalog_hash(
                source_text,
                draft_text=draft_text,
            )
        ),
        "mode": "targeted" if targeted else "legacy",
        "episode_no": episode_no,
        "source_text": source_text,
        "draft_text": draft_text,
        "future_text": future_text,
        "future_label": future_label,
        "bible": bible.model_dump(mode="json"),
        "existing_resolutions": existing_resolutions or [],
        "structural_evidence": structural_evidence or [],
    }
    if structural_coverage_applied:
        discovery_input.update({
            "structural_coverage_policy_version": (
                STRUCTURAL_IDENTITY_COVERAGE_VERSION
            ),
            "structural_coverage_applied": True,
        })
    input_hash = evidence_repository.content_hash(discovery_input)
    evidence_conn = get_conn()
    artifacts_available = bool(
        scope_id
        and evidence_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifacts'"
        ).fetchone()
    )
    artifact_seals_available = bool(
        artifacts_available
        and _has_column(evidence_conn, "artifacts", "content_hash")
    )
    cached_rows = (
        evidence_conn.execute(
            """SELECT content_json,content_hash FROM artifacts
                 WHERE scope_type='episode' AND scope_id=?
                   AND type='screenplay_identity_discovery' AND status='validated'
                 ORDER BY created_at DESC LIMIT 20""",
            (artifact_scope_id,),
        ).fetchall()
        if artifact_seals_available else []
    )
    for row in cached_rows:
        try:
            cached = json.loads(row["content_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if (
            not isinstance(cached, dict)
            or not str(row["content_hash"] or "").strip()
            or str(row["content_hash"] or "").strip()
            != evidence_repository.content_hash(cached)
        ):
            # `validated` is not an integrity seal by itself.  Never reuse a
            # payload whose bytes no longer match the repository-owned hash.
            continue
        if (
            cached.get("contract_version") == IDENTITY_DISCOVERY_CONTRACT_VERSION
            and cached.get("current_identity_version")
            == CURRENT_IDENTITY_DECISION_VERSION
            and cached.get("current_evidence_catalog_hash")
            == discovery_input["current_evidence_catalog_hash"]
            and (
                not structural_coverage_applied
                or (
                    cached.get("structural_coverage_policy_version")
                    == STRUCTURAL_IDENTITY_COVERAGE_VERSION
                    and cached.get("structural_coverage_applied") is True
                )
            )
            and cached.get("input_hash") == input_hash
            and isinstance(cached.get("candidates"), list)
        ):
            if any(not isinstance(item, dict) for item in cached["candidates"]):
                continue
            cached_candidates = [dict(item) for item in cached["candidates"]]
            typed_current_candidates = [
                item for item in cached_candidates
                if str(item.get("source_label_provenance") or "").strip()
                in {
                    CURRENT_IDENTITY_LITERAL_PROVENANCE,
                    CURRENT_IDENTITY_SYNTHETIC_PROVENANCE,
                }
            ]
            if (
                targeted
                and not structural_coverage_applied
                and len(typed_current_candidates) != len(cached_candidates)
            ):
                continue
            if any(
                item.get("source_evidence_receipt") is None
                or item.get("source_evidence_receipts") is None
                for item in typed_current_candidates
            ):
                continue
            try:
                _attach_candidate_source_evidence(
                    typed_current_candidates,
                    source_text,
                    draft_text=draft_text,
                )
                return cached_candidates
            except ContentGenerationError:
                # A validated marker cannot override a broken RF11 receipt.
                # Ignore the cache and rerun the strict discovery gate.
                continue

    if targeted:
        current = await extract_current_identity_candidates(
            source_text,
            bible,
            episode_no,
            draft_text=draft_text,
            existing_resolutions=existing_resolutions,
            project_id=project_id,
        )
        resolved = await resolve_future_identity_candidates(
            current,
            source_text=source_text,
            future_text=future_text,
            bible=bible,
            episode_no=episode_no,
            future_label=future_label,
        )
        audited = await audit_identity_coverage_from_structural_evidence(
            resolved,
            structural_evidence=structural_evidence,
            source_text=source_text,
            bible=bible,
            episode_no=episode_no,
            existing_resolutions=existing_resolutions,
        )
    else:
        audited = _attach_candidate_source_evidence(
            await _discover_character_candidates_legacy(
                source_text,
                bible,
                episode_no,
                draft_text=draft_text,
                future_text=future_text,
                future_label=future_label,
                existing_resolutions=existing_resolutions,
                project_id=project_id,
            ),
            source_text,
        )
    trace = None
    try:
        from app.observability.tracing import current_trace
        trace = current_trace()
    except Exception:  # noqa: BLE001 - evidence is optional outside workflows
        pass
    if not artifacts_available:
        return audited
    raw_artifact = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_identity_discovery_raw",
            scope_type="episode",
            scope_id=artifact_scope_id,
            status="candidate",
            trust_level="T0",
            content={
                "contract_version": IDENTITY_DISCOVERY_CONTRACT_VERSION,
                "current_identity_version": CURRENT_IDENTITY_DECISION_VERSION,
                "current_evidence_catalog_hash": discovery_input[
                    "current_evidence_catalog_hash"
                ],
                "structural_coverage_policy_version": (
                    STRUCTURAL_IDENTITY_COVERAGE_VERSION
                ),
                "structural_coverage_applied": structural_coverage_applied,
                "input_hash": input_hash,
                "mode": "targeted" if targeted else "legacy",
                "model_candidates": audited,
            },
            contract_version=IDENTITY_DISCOVERY_CONTRACT_VERSION,
        ),
        step_run_id=getattr(trace, "step_run_id", None),
    )
    evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_identity_discovery",
            scope_type="episode",
            scope_id=artifact_scope_id,
            status="validated",
            trust_level="T1",
            content={
                "contract_version": IDENTITY_DISCOVERY_CONTRACT_VERSION,
                "current_identity_version": CURRENT_IDENTITY_DECISION_VERSION,
                "current_evidence_catalog_hash": discovery_input[
                    "current_evidence_catalog_hash"
                ],
                "structural_coverage_policy_version": (
                    STRUCTURAL_IDENTITY_COVERAGE_VERSION
                ),
                "structural_coverage_applied": structural_coverage_applied,
                "episode_no": episode_no,
                "candidates": audited,
                "source_hash": evidence_repository.content_hash(source_text),
                "input_hash": input_hash,
                "mode": "targeted" if targeted else "legacy",
            },
            parent_artifact_ids=[raw_artifact["id"]],
            contract_version=IDENTITY_DISCOVERY_CONTRACT_VERSION,
        ),
        step_run_id=getattr(trace, "step_run_id", None),
    )
    return audited

