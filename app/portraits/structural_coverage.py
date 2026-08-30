"""结构化身份覆盖回执的生成与「决议是否仍然当前」判定：
screenplay_identity_resolution_is_current_for_source/_scope 等。
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from app.evidence import repository as evidence_repository
from app.errors import ContentGenerationError
from app.identity_authority import normalize_character_resolution, normalize_character_resolutions
from app.schemas import Bible
from app.source_excerpt import index_source_segments

from ._db_probe import _has_column
from .constants import (
    AUTOMATIC_IDENTITY_DECISION_PROVENANCE,
    CURRENT_IDENTITY_LITERAL_PROVENANCE,
    CURRENT_IDENTITY_SYNTHETIC_PROVENANCE,
    DURABLE_IDENTITY_DECISION_PROVENANCE,
    FUTURE_IDENTITY_DECISION_VERSION,
    IDENTITY_ADJUDICATION_SOURCE_PROVENANCE,
    IDENTITY_DISCOVERY_CONTRACT_VERSION,
    STRUCTURAL_IDENTITY_COVERAGE_VERSION,
)
from .discovery_resample import screenplay_identity_scope_fingerprint
from .evidence_receipt import _validate_current_identity_receipt_bundle

def _identity_resolution(
    item: dict,
    canonical_name: str,
    resolution: str,
    *,
    reason: str = "",
) -> dict:
    receipt_bundle = _validate_current_identity_receipt_bundle(
        item,
        source_text=None,
    )
    primary_receipt = receipt_bundle[0] if receipt_bundle is not None else None
    receipt_list = receipt_bundle[1] if receipt_bundle is not None else []
    receipt_source_ids = (
        receipt_bundle[2]
        if receipt_bundle is not None
        else list(dict.fromkeys(
            str(value).strip()
            for value in item.get("source_segment_ids") or []
            if str(value).strip()
        ))
    )
    payload = {
        "source_label": str(item.get("source_label") or item.get("name") or "").strip(),
        "canonical_name": canonical_name,
        "resolution": resolution,
        "reason": reason,
        "evidence": str(item.get("evidence") or "").strip()[:80],
        "future_evidence": str(item.get("future_evidence") or "").strip()[:120],
        "identity_group": str(item.get("identity_group") or "").strip()[:96],
        "identity_scope_fingerprint": str(
            item.get("identity_scope_fingerprint") or ""
        ).strip(),
        "decision_provenance": str(
            item.get("decision_provenance")
            or AUTOMATIC_IDENTITY_DECISION_PROVENANCE
        ).strip(),
        "decision_contract_version": FUTURE_IDENTITY_DECISION_VERSION,
        "structural_identity_policy_version": (
            STRUCTURAL_IDENTITY_COVERAGE_VERSION
        ),
        "authority_id": str(item.get("authority_id") or "").strip(),
        "source_label_provenance": str(
            item.get("source_label_provenance") or ""
        ).strip(),
        "source_segment_ids": receipt_source_ids,
    }
    if primary_receipt is not None:
        payload.update({
            "source_evidence_receipt": dict(primary_receipt),
            "source_evidence_receipts": receipt_list,
            "source_segment_id": str(
                primary_receipt.get("source_segment_id") or ""
            ),
            "source_quote": str(primary_receipt.get("text") or ""),
        })
    return normalize_character_resolution(payload)


def structural_identity_resolution_is_current(value: dict) -> bool:
    """Whether a durable resolution may suppress the current coverage gate."""
    provenance = str(value.get("decision_provenance") or "").strip()
    return bool(
        provenance in DURABLE_IDENTITY_DECISION_PROVENANCE
        or str(
            value.get("structural_identity_policy_version") or ""
        ).strip() == STRUCTURAL_IDENTITY_COVERAGE_VERSION
    )


def screenplay_identity_resolution_is_current_for_source(
    value: dict,
    *,
    episode_no: int,
    source_text: str,
) -> bool:
    """Fence automatic identity authority by wire versions and source epoch."""
    current = screenplay_identity_resolution_is_current_for_scope(
        value,
        identity_scope_fingerprint=screenplay_identity_scope_fingerprint(
            episode_no, source_text
        ),
    )
    provenance = str(value.get("decision_provenance") or "").strip()
    label_provenance = str(
        value.get("source_label_provenance") or ""
    ).strip()
    if (
        current
        and provenance not in DURABLE_IDENTITY_DECISION_PROVENANCE
        and label_provenance in {
            CURRENT_IDENTITY_LITERAL_PROVENANCE,
            CURRENT_IDENTITY_SYNTHETIC_PROVENANCE,
        }
    ):
        try:
            return _validate_current_identity_receipt_bundle(
                value,
                source_text=source_text,
            ) is not None
        except ContentGenerationError:
            return False
    if (
        current
        and provenance not in DURABLE_IDENTITY_DECISION_PROVENANCE
        and label_provenance == IDENTITY_ADJUDICATION_SOURCE_PROVENANCE
    ):
        return _identity_adjudication_receipt_is_valid(
            value,
            source_text=source_text,
        )
    return current


def screenplay_identity_resolution_is_current_for_scope(
    value: dict,
    *,
    identity_scope_fingerprint: str,
) -> bool:
    """Fence automatic authority by wire versions and an owned-source epoch."""
    provenance = str(value.get("decision_provenance") or "").strip()
    if provenance in DURABLE_IDENTITY_DECISION_PROVENANCE:
        return True
    current = bool(
        str(value.get("decision_contract_version") or "").strip()
        == FUTURE_IDENTITY_DECISION_VERSION
        and structural_identity_resolution_is_current(value)
        and str(
            value.get("identity_scope_fingerprint") or ""
        ).strip() == str(identity_scope_fingerprint or "").strip()
    )
    label_provenance = str(
        value.get("source_label_provenance") or ""
    ).strip()
    if current and label_provenance in {
        CURRENT_IDENTITY_LITERAL_PROVENANCE,
        CURRENT_IDENTITY_SYNTHETIC_PROVENANCE,
    }:
        try:
            return _validate_current_identity_receipt_bundle(
                value,
                source_text=None,
            ) is not None
        except ContentGenerationError:
            return False
    if (
        current
        and label_provenance == IDENTITY_ADJUDICATION_SOURCE_PROVENANCE
    ):
        return _identity_adjudication_receipt_is_valid(
            value,
            source_text=None,
        )
    return current


def _identity_adjudication_receipt_is_valid(
    value: dict,
    *,
    source_text: str | None,
) -> bool:
    receipt = value.get("identity_adjudication_receipt")
    if not isinstance(receipt, dict):
        return False

    def exact_source_ids(raw: object) -> list[str] | None:
        if (
            not isinstance(raw, list)
            or not raw
            or any(
                not isinstance(item, str)
                or not item.strip()
                or item != item.strip()
                for item in raw
            )
        ):
            return None
        normalized = list(raw)
        return normalized if len(normalized) == len(set(normalized)) else None

    source_ids = exact_source_ids(receipt.get("source_segment_ids"))
    item_source_ids = exact_source_ids(value.get("source_segment_ids"))
    evidence_source_ids = exact_source_ids(value.get("evidence_source_ids"))
    if (
        receipt.get("version") != "screenplay-ir-identity-adjudicator.v2"
        or source_ids is None
        or item_source_ids is None
        or evidence_source_ids is None
    ):
        return False
    payload = {
        "version": receipt["version"],
        "source_hash": str(receipt.get("source_hash") or "").strip(),
        "source_segment_ids": source_ids,
    }
    if (
        not payload["source_hash"]
        or str(receipt.get("hash") or "").strip()
        != evidence_repository.content_hash(payload)
        or source_ids != item_source_ids
        or source_ids != evidence_source_ids
    ):
        return False
    if source_text is None:
        # Persistence compares validity classes without necessarily owning the
        # episode source.  The source-aware read fence below performs the
        # stronger membership/order proof whenever the source is available.
        return True
    if payload["source_hash"] != evidence_repository.content_hash(source_text):
        return False
    indexed_source_ids = [
        segment.segment_id for segment in index_source_segments(source_text)
    ]
    selected_source_ids = set(source_ids)
    return bool(
        selected_source_ids.issubset(indexed_source_ids)
        and source_ids
        == [
            source_id
            for source_id in indexed_source_ids
            if source_id in selected_source_ids
        ]
    )


_STRUCTURAL_IDENTITY_RECEIPT_VERSION = (
    "screenplay-identity-structural-resolution-receipt.v3"
)
_STRUCTURAL_IDENTITY_CATALOG_RECEIPT_VERSION = (
    "screenplay-identity-structural-catalog-receipt.v1"
)


def _structural_identity_candidate_semantic_rows(
    candidates: list[dict] | None,
) -> list[dict]:
    """Canonical semantic projection bound into a validated coverage Artifact."""
    fields = (
        "source_label",
        "name",
        "identity_kind",
        "kind",
        "identity_group",
        "authority_id",
        "source_segment_id",
        "source_quote",
        "source_label_provenance",
    )
    rows: list[dict] = []
    for item in (candidates or []):
        if not isinstance(item, dict):
            raise ContentGenerationError(
                "结构人物 candidate semantic receipt 含非对象项"
            )
        if not str(item.get("source_label") or "").strip() or not str(
            item.get("identity_group") or ""
        ).strip():
            raise ContentGenerationError(
                "结构人物 candidate semantic receipt 缺少身份键"
            )
        bundle = _validate_current_identity_receipt_bundle(
            item,
            source_text=None,
        )
        primary_receipt = bundle[0] if bundle is not None else None
        receipts = bundle[1] if bundle is not None else []
        receipt_source_ids = bundle[2] if bundle is not None else None
        source_segment_ids = (
            receipt_source_ids
            if receipt_source_ids is not None
            else [
                str(value).strip()
                for value in item.get("source_segment_ids") or []
                if str(value).strip()
            ]
        )
        rows.append({
            **{
                field: str(item.get(field) or "").strip()
                for field in fields
            },
            "source_segment_ids": source_segment_ids,
            "source_evidence_receipt_hash": (
                evidence_repository.content_hash(primary_receipt)
                if primary_receipt is not None
                else ""
            ),
            "source_evidence_receipts_hash": (
                evidence_repository.content_hash(receipts)
                if bundle is not None else ""
            ),
        })
    return sorted(
        rows,
        key=lambda item: json.dumps(
            item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
    )


def _structural_identity_candidate_semantic_hash(
    candidates: list[dict] | None,
) -> str:
    return evidence_repository.content_hash(
        _structural_identity_candidate_semantic_rows(candidates)
    )


def _structural_identity_catalog_input_hash(
    *,
    bible: Bible,
    base_candidates: list[dict] | None,
    structural_evidence_hash: str,
    existing_resolutions: list[dict] | None,
    output_candidates: list[dict] | None,
) -> str:
    """Fingerprint every backend-owned input that can change coverage options.

    Automatic rows materialized by ``output_candidates`` are excluded so the
    pre-audit fingerprint remains stable after a successful coverage result is
    persisted.  Their complete semantics are already bound separately by the
    materialization receipt.  Durable/manual rows remain inputs even when they
    share a key with an output candidate.
    """
    output_keys = {
        (
            str(item.get("source_label") or "").strip(),
            str(item.get("identity_group") or "").strip(),
        )
        for item in (output_candidates or [])
        if isinstance(item, dict)
        and str(item.get("source_label") or "").strip()
        and str(item.get("identity_group") or "").strip()
    }
    resolution_fields = (
        "source_label",
        "canonical_name",
        "authority_id",
        "resolution",
        "identity_group",
        "identity_scope_fingerprint",
        "decision_provenance",
        "decision_contract_version",
        "structural_identity_policy_version",
    )
    resolution_rows = []
    for item in normalize_character_resolutions(existing_resolutions):
        key = (
            str(item.get("source_label") or "").strip(),
            str(item.get("identity_group") or "").strip(),
        )
        provenance = str(item.get("decision_provenance") or "").strip()
        if (
            provenance not in DURABLE_IDENTITY_DECISION_PROVENANCE
            and key in output_keys
        ):
            continue
        resolution_rows.append({
            field: str(item.get(field) or "").strip()
            for field in resolution_fields
        })
    resolution_rows.sort(
        key=lambda item: tuple(item[field] for field in resolution_fields)
    )
    output_named_authorities = {
        str(item.get("name") or "").strip()
        for item in (output_candidates or [])
        if isinstance(item, dict)
        and str(item.get("identity_kind") or "").strip() == "named"
        and str(item.get("name") or "").strip()
    }
    bible_authorities = sorted({
        str(character.name or "").strip()
        for character in bible.characters
        if str(character.name or "").strip()
        and str(character.name or "").strip()
        not in output_named_authorities
    })
    return evidence_repository.content_hash({
        "contract_version": IDENTITY_DISCOVERY_CONTRACT_VERSION,
        "policy_version": STRUCTURAL_IDENTITY_COVERAGE_VERSION,
        "structural_evidence_hash": structural_evidence_hash,
        "bible_authorities": bible_authorities,
        "base_candidate_semantics": (
            _structural_identity_candidate_semantic_rows(base_candidates)
        ),
        "external_resolution_semantics": resolution_rows,
    })


def _structural_identity_catalog_receipt_is_valid(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    fields = (
        "authority_catalog_hash",
        "group_catalog_hash",
        "decision_catalog_hash",
        "evidence_catalog_hash",
    )
    payload = {field: str(value.get(field) or "") for field in fields}
    return bool(
        value.get("version")
        == _STRUCTURAL_IDENTITY_CATALOG_RECEIPT_VERSION
        and all(payload.values())
        and str(value.get("hash") or "")
        == evidence_repository.content_hash(payload)
    )


def _structural_identity_required_bible_names(
    candidates: list[dict] | None,
) -> list[str]:
    """Named visible identities require a committed card before cache success."""
    return sorted({
        str(item.get("name") or "").strip()
        for item in (candidates or [])
        if isinstance(item, dict)
        and str(item.get("identity_kind") or "").strip() == "named"
        and str(item.get("kind") or "onscreen").strip() != "mentioned"
        and str(item.get("name") or "").strip()
    })


def _project_bible_character_names(
    conn,
    project_id: str,
    fallback_bible: Bible,
) -> set[str]:
    """Read the post-materialization Bible, with isolated-test compatibility."""
    if _has_column(conn, "projects", "bible_json"):
        row = conn.execute(
            "SELECT bible_json FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        if row and row["bible_json"]:
            try:
                current_bible = Bible.model_validate(
                    json.loads(row["bible_json"])
                )
            except (TypeError, ValueError, ValidationError, json.JSONDecodeError):
                return set()
            return {
                str(character.name or "").strip()
                for character in current_bible.characters
                if str(character.name or "").strip()
            }
    return {
        str(character.name or "").strip()
        for character in fallback_bible.characters
        if str(character.name or "").strip()
    }


def _structural_identity_resolution_receipt(
    resolutions: list[dict] | None,
    *,
    candidates: list[dict] | None,
    identity_scope_fingerprint: str,
) -> dict:
    """Bind a coverage Artifact to the exact durable rows it materialized.

    Candidate keys select only rows owned by this coverage result; every
    authority-bearing field is retained so a same-label/group row with a
    different canonical identity can never satisfy replay recovery.
    """
    candidate_keys = {
        (
            str(item.get("source_label") or "").strip(),
            str(item.get("identity_group") or "").strip(),
        )
        for item in (candidates or [])
        if isinstance(item, dict)
        and str(item.get("source_label") or "").strip()
        and str(item.get("identity_group") or "").strip()
    }
    fields = (
        "source_label",
        "canonical_name",
        "authority_id",
        "authority_version",
        "resolution",
        "identity_group",
        "identity_scope_fingerprint",
        "source_instance_key",
        "decision_provenance",
        "decision_contract_version",
        "structural_identity_policy_version",
    )
    rows: list[dict] = []
    for item in normalize_character_resolutions(resolutions):
        key = (
            str(item.get("source_label") or "").strip(),
            str(item.get("identity_group") or "").strip(),
        )
        if key not in candidate_keys or not (
            screenplay_identity_resolution_is_current_for_scope(
            item,
            identity_scope_fingerprint=identity_scope_fingerprint,
            )
        ):
            continue
        bundle = _validate_current_identity_receipt_bundle(
            item,
            source_text=None,
        )
        primary_receipt = bundle[0] if bundle is not None else None
        receipt_list = bundle[1] if bundle is not None else []
        receipt_source_ids = bundle[2] if bundle is not None else [
            str(value).strip()
            for value in item.get("source_segment_ids") or []
            if str(value).strip()
        ]
        rows.append({
            **{
                field: str(item.get(field) or "").strip()
                for field in fields
            },
            "source_segment_ids": receipt_source_ids,
            "source_evidence_receipt_hash": (
                evidence_repository.content_hash(primary_receipt)
                if primary_receipt is not None else ""
            ),
            "source_evidence_receipts_hash": (
                evidence_repository.content_hash(receipt_list)
                if bundle is not None else ""
            ),
        })
    rows.sort(key=lambda item: json.dumps(
        item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ))
    return {
        "version": _STRUCTURAL_IDENTITY_RECEIPT_VERSION,
        "rows": rows,
        "hash": evidence_repository.content_hash(rows),
    }


def _structural_identity_resolution_receipt_is_valid(value: object) -> bool:
    return bool(
        isinstance(value, dict)
        and value.get("version") == _STRUCTURAL_IDENTITY_RECEIPT_VERSION
        and isinstance(value.get("rows"), list)
        and str(value.get("hash") or "")
        == evidence_repository.content_hash(value["rows"])
    )

