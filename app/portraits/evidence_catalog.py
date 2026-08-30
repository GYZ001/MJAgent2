"""当前身份证据目录：从正文抽取候选片段、封装证据记录、按预算分批、
生成目录哈希，以及已知/在途决议目录的构建。
"""

from __future__ import annotations

import json

from app.evidence import repository as evidence_repository
from app.errors import ContentGenerationError
from app.source_excerpt import index_source_segments

from ._identity_tokens import _identity_source_label_has_list_separator
from .constants import (
    CAST_DISCOVERY_SOURCE_BUDGET,
    CURRENT_IDENTITY_DECISION_VERSION,
    CURRENT_IDENTITY_EVIDENCE_RECEIPT_VERSION,
    CURRENT_IDENTITY_SYNTHETIC_PROVENANCE,
    IDENTITY_SOURCE_LABEL_DEFENSIVE_MAX_LENGTH,
)
from .discovery_fragments import _draft_identity_projection
from .discovery_resample import _canonical_named_authority_id

def _current_identity_evidence_payload(record: dict) -> dict:
    """Canonical payload sealed into one backend-owned current evidence ID."""
    return {
        "receipt_version": CURRENT_IDENTITY_EVIDENCE_RECEIPT_VERSION,
        "contract_version": CURRENT_IDENTITY_DECISION_VERSION,
        "origin": str(record.get("origin") or ""),
        "source_hash": str(record.get("source_hash") or ""),
        "source_segment_id": str(record.get("source_segment_id") or ""),
        "start_offset": int(record.get("start_offset") or 0),
        "end_offset": int(record.get("end_offset") or 0),
        "path": str(record.get("path") or ""),
        "text": str(record.get("text") or ""),
    }


def _seal_current_identity_evidence(record: dict) -> dict:
    payload = _current_identity_evidence_payload(record)
    if (
        payload["origin"] not in {"current_source", "draft_identity_projection"}
        or not payload["source_hash"]
        or not payload["source_segment_id"]
        or not payload["text"].strip()
        or payload["end_offset"] <= payload["start_offset"]
    ):
        raise ValueError("current identity evidence receipt is incomplete")
    evidence_id = "CE:" + evidence_repository.content_hash(payload)[:24]
    return {**payload, "evidence_id": evidence_id}


def _current_identity_evidence_records(
    source_text: str,
    *,
    draft_text: str = "",
) -> list[dict]:
    """Build owned raw-source or typed-draft evidence, never prompt prose."""
    if str(draft_text or "").strip():
        try:
            projection = json.loads(_draft_identity_projection(draft_text))
        except (TypeError, ValueError, json.JSONDecodeError):
            projection = {}
        if projection.get("parse_status") != "typed":
            return []
        source_hash = evidence_repository.content_hash(draft_text)
        records: list[dict] = []
        for index, raw in enumerate(
            projection.get("identity_mentions") or [], start=1
        ):
            if not isinstance(raw, dict):
                continue
            value = str(raw.get("value") or "").strip()
            path = str(raw.get("path") or "").strip()
            context = str(raw.get("line_context") or "").strip()
            if not value or not path:
                continue
            text = value if not context else f"{value}\n{context}"
            records.append(_seal_current_identity_evidence({
                "origin": "draft_identity_projection",
                "source_hash": source_hash,
                "source_segment_id": f"DRF{index:04d}",
                # DRF offsets are stable positions in the typed projection,
                # not byte offsets into JSON serialization.
                "start_offset": index - 1,
                "end_offset": index,
                "path": path,
                "text": text,
            }))
        return records

    source_hash = evidence_repository.content_hash(source_text)
    return [
        _seal_current_identity_evidence({
            "origin": "current_source",
            "source_hash": source_hash,
            "source_segment_id": segment.segment_id,
            "start_offset": segment.start_offset,
            "end_offset": segment.end_offset,
            "path": "",
            "text": segment.text,
        })
        for segment in index_source_segments(source_text)
        if str(segment.text or "").strip()
    ]


def _current_identity_evidence_batches(
    source_text: str,
    *,
    draft_text: str = "",
) -> list[list[dict]]:
    """Pack owned evidence into bounded calls without resending raw source."""
    records = _current_identity_evidence_records(
        source_text,
        draft_text=draft_text,
    )
    batches: list[list[dict]] = []
    current: list[dict] = []
    current_chars = 0
    for record in records:
        projected = {
            key: record[key]
            for key in (
                "evidence_id",
                "origin",
                "source_segment_id",
                "start_offset",
                "end_offset",
                "path",
                "text",
            )
        }
        record_chars = len(json.dumps(
            projected, ensure_ascii=False, separators=(",", ":")
        ))
        if current and current_chars + record_chars > CAST_DISCOVERY_SOURCE_BUDGET:
            batches.append(current)
            current = []
            current_chars = 0
        current.append(record)
        current_chars += record_chars
    if current:
        batches.append(current)
    return batches


def _current_identity_evidence_catalog_hash(
    source_text: str,
    *,
    draft_text: str = "",
) -> str:
    return evidence_repository.content_hash({
        "contract_version": CURRENT_IDENTITY_DECISION_VERSION,
        "batches": _current_identity_evidence_batches(
            source_text,
            draft_text=draft_text,
        ),
    })


def _current_identity_known_decision_catalog(
    evidence_by_ref: dict[str, dict],
    *,
    authorities: list[dict],
) -> dict[str, dict]:
    """Sign exact current-evidence/registered-authority label decisions."""
    decisions: dict[str, dict] = {}
    authority_by_ref_label: dict[tuple[str, str], str] = {}
    for evidence_ref, record in evidence_by_ref.items():
        evidence_text = str(record.get("text") or "")
        for authority in authorities:
            if str(authority.get("identity_kind") or "") == "functional":
                continue
            authority_id = str(authority.get("authority_id") or "").strip()
            canonical_name = str(
                authority.get("canonical_name") or ""
            ).strip()
            if not authority_id or not canonical_name:
                continue
            signed_identity_group = str(
                authority.get("identity_group") or authority_id
            ).strip()
            registered_labels = list(dict.fromkeys(
                str(label or "").strip()
                for label in (
                    canonical_name,
                    *(authority.get("source_labels") or []),
                )
                if str(label or "").strip()
            ))
            for source_label in registered_labels:
                if (
                    len(source_label) > IDENTITY_SOURCE_LABEL_DEFENSIVE_MAX_LENGTH
                    or _identity_source_label_has_list_separator(source_label)
                    or source_label not in evidence_text
                ):
                    continue
                pair = (evidence_ref, source_label)
                previous_authority = authority_by_ref_label.setdefault(
                    pair, authority_id
                )
                if previous_authority != authority_id:
                    raise ContentGenerationError(
                        "current registered label 对应多个 authority："
                        f"{source_label}"
                    )
                payload = {
                    "contract_version": CURRENT_IDENTITY_DECISION_VERSION,
                    "decision_type": "registered_authority",
                    "evidence_ref": evidence_ref,
                    "evidence_id": str(record.get("evidence_id") or ""),
                    "authority_id": authority_id,
                    "canonical_name": canonical_name,
                    "source_label": source_label,
                    "materialization_compatible": bool(
                        authority_id
                        == _canonical_named_authority_id(canonical_name)
                        and signed_identity_group == authority_id
                    ),
                }
                decision_id = (
                    f"K:{evidence_ref}:"
                    + evidence_repository.content_hash(payload)[:24]
                )
                decisions[decision_id] = {
                    **payload,
                    "decision_id": decision_id,
                    "identity_group": str(
                        signed_identity_group
                    ),
                    "source_instance_key": str(
                        authority.get("source_instance_key") or authority_id
                    ).strip(),
                }
    return decisions


def _current_identity_prior_decision_catalog(
    evidence_by_ref: dict[str, dict],
    *,
    prior_candidates: list[dict],
) -> tuple[dict[str, dict], dict[str, dict]]:
    """Sign explicit cross-batch reuse choices without label auto-merging."""
    prior_named: dict[str, dict] = {}
    functional_groups: dict[str, dict] = {}
    for candidate in prior_candidates:
        if (
            candidate.get("source_label_provenance")
            == CURRENT_IDENTITY_SYNTHETIC_PROVENANCE
        ):
            continue
        source_label = str(candidate.get("source_label") or "").strip()
        identity_group = str(candidate.get("identity_group") or "").strip()
        identity_kind = str(candidate.get("identity_kind") or "").strip()
        if not source_label or not identity_group:
            continue
        if identity_kind == "named":
            canonical_name = str(candidate.get("name") or "").strip()
            # Registered authorities are re-signed against each batch by the
            # normal K catalog.  P:N is only for a request-local new literal
            # name that has no authority yet; exposing both would make two
            # different tokens claim the same registered decision.
            if (
                not canonical_name
                or str(candidate.get("authority_id") or "").strip()
                or canonical_name != source_label
            ):
                continue
            for evidence_ref, record in evidence_by_ref.items():
                if source_label not in str(record.get("text") or ""):
                    continue
                payload = {
                    "contract_version": CURRENT_IDENTITY_DECISION_VERSION,
                    "decision_type": "prior_named",
                    "evidence_ref": evidence_ref,
                    "evidence_id": str(record.get("evidence_id") or ""),
                    "source_label": source_label,
                    "canonical_name": canonical_name,
                    "identity_group": identity_group,
                    "authority_id": str(
                        candidate.get("authority_id") or ""
                    ).strip(),
                    "known_authority": bool(
                        str(candidate.get("authority_id") or "").strip()
                    ),
                    "materialization_compatible": bool(
                        candidate.get("_current_materialization_compatible")
                    ),
                }
                decision_id = (
                    f"P:N:{evidence_ref}:"
                    + evidence_repository.content_hash(payload)[:20]
                )
                prior_named[decision_id] = {
                    **payload,
                    "decision_id": decision_id,
                }
        elif identity_kind == "functional":
            existing = next((
                item for item in functional_groups.values()
                if item["identity_group"] == identity_group
            ), None)
            if existing is not None:
                if source_label not in existing["source_labels"]:
                    existing["source_labels"].append(source_label)
                response_group_key = str(
                    candidate.get("_current_response_group_key") or ""
                ).strip()
                if (
                    response_group_key
                    and response_group_key
                    not in existing["response_group_keys"]
                ):
                    existing["response_group_keys"].append(response_group_key)
                continue
            payload = {
                "contract_version": CURRENT_IDENTITY_DECISION_VERSION,
                "decision_type": "prior_functional_group",
                "identity_group": identity_group,
                "existing_route_name": str(
                    candidate.get("existing_route_name") or ""
                ).strip(),
            }
            decision_id = (
                "P:F:" + evidence_repository.content_hash(payload)[:24]
            )
            functional_groups[decision_id] = {
                **payload,
                "decision_id": decision_id,
                "source_labels": [source_label],
                "response_group_keys": [
                    value for value in [str(
                        candidate.get("_current_response_group_key") or ""
                    ).strip()] if value
                ],
            }
    return prior_named, functional_groups


def _current_identity_evidence_receipt_is_valid(
    value: object,
    *,
    source_text: str = "",
    draft_text: str = "",
) -> bool:
    """Verify the backend seal and, when available, its owned input epoch."""
    if not isinstance(value, dict):
        return False
    if (
        value.get("receipt_version")
        != CURRENT_IDENTITY_EVIDENCE_RECEIPT_VERSION
        or value.get("contract_version")
        != CURRENT_IDENTITY_DECISION_VERSION
    ):
        return False
    try:
        payload = _current_identity_evidence_payload(value)
    except (TypeError, ValueError):
        return False
    if str(value.get("evidence_id") or "") != (
        "CE:" + evidence_repository.content_hash(payload)[:24]
    ):
        return False
    origin = payload["origin"]
    if origin == "current_source":
        if payload["source_hash"] != evidence_repository.content_hash(source_text):
            return False
        start = payload["start_offset"]
        end = payload["end_offset"]
        segments_by_id = {
            segment.segment_id: segment
            for segment in index_source_segments(source_text)
        }
        owned_segment = segments_by_id.get(payload["source_segment_id"])
        return bool(
            0 <= start < end <= len(source_text)
            and source_text[start:end].strip() == payload["text"]
            and owned_segment is not None
            and owned_segment.start_offset == start
            and owned_segment.end_offset == end
            and owned_segment.text == payload["text"]
        )
    if origin == "draft_identity_projection":
        if draft_text and payload["source_hash"] != evidence_repository.content_hash(
            draft_text
        ):
            return False
        if not payload["path"] or not payload["text"].strip():
            return False
        if not draft_text:
            # The seal was already checked against the owned draft at the
            # current-discovery boundary.  Later stages only need to preserve
            # that immutable backend receipt.
            return True
        return any(
            str(record.get("evidence_id") or "")
            == str(value.get("evidence_id") or "")
            and record == value
            for record in _current_identity_evidence_records(
                source_text,
                draft_text=draft_text,
            )
        )
    return False

