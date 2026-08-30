"""身份证据回执（receipt）的核验与候选证据挂载，以及当前身份候选的
对外抽取入口 extract_current_identity_candidates。
"""

from __future__ import annotations


from typing import NoReturn

from app.evidence import repository as evidence_repository
from app import textmatch
from app.errors import ContentGenerationError
from app.schemas import Bible
from app.source_excerpt import index_source_segments

from .constants import (
    CURRENT_IDENTITY_DECISION_VERSION,
    CURRENT_IDENTITY_EVIDENCE_RECEIPT_VERSION,
    CURRENT_IDENTITY_LITERAL_PROVENANCE,
    CURRENT_IDENTITY_SYNTHETIC_PROVENANCE,
)
from .discovery_legacy import _discover_character_candidates_legacy
from .evidence_catalog import (
    _current_identity_evidence_payload,
    _current_identity_evidence_receipt_is_valid,
)
from .evidence_merge import _current_identity_receipt_sort_key

def _validate_current_identity_receipt_bundle(
    candidate: dict,
    *,
    source_text: str | None,
    draft_text: str | None = "",
) -> tuple[dict, list[dict], list[str]] | None:
    """Validate the complete RF11 receipt bundle, never only its primary."""
    current_receipt = candidate.get("source_evidence_receipt")
    current_receipts = candidate.get("source_evidence_receipts")
    if current_receipt is None and current_receipts is None:
        return None

    def invalid(reason: str) -> NoReturn:
        raise ContentGenerationError(
            f"current identity evidence receipt v2 无效：{reason}"
        )

    if (
        not isinstance(current_receipt, dict)
        or not isinstance(current_receipts, list)
        or not current_receipts
        or any(not isinstance(value, dict) for value in current_receipts)
    ):
        invalid("缺少完整 receipt list")
    receipts = [dict(value) for value in current_receipts]

    def seal_is_valid(value: dict) -> bool:
        if source_text is not None:
            return _current_identity_evidence_receipt_is_valid(
                value,
                source_text=source_text,
                draft_text=str(draft_text or ""),
            )
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
        return bool(
            payload["origin"] in {
                "current_source", "draft_identity_projection",
            }
            and payload["source_hash"]
            and payload["source_segment_id"]
            and payload["text"].strip()
            and payload["end_offset"] > payload["start_offset"]
            and str(value.get("evidence_id") or "")
            == "CE:" + evidence_repository.content_hash(payload)[:24]
        )

    if any(not seal_is_valid(value) for value in receipts):
        invalid("seal 或 owned source epoch 不匹配")
    evidence_ids = [
        str(value.get("evidence_id") or "").strip() for value in receipts
    ]
    if not all(evidence_ids) or len(evidence_ids) != len(set(evidence_ids)):
        invalid("evidence_id 空值或重复")
    canonical_receipts = sorted(receipts, key=_current_identity_receipt_sort_key)
    if receipts != canonical_receipts:
        invalid("receipt list 顺序不是 canonical")

    label = str(candidate.get("source_label") or "").strip()
    provenance = str(candidate.get("source_label_provenance") or "").strip()
    if provenance == CURRENT_IDENTITY_LITERAL_PROVENANCE:
        if not label or any(
            label not in str(value.get("text") or "") for value in receipts
        ):
            invalid("逐字 source_label 与 receipt 不匹配")
    elif provenance == CURRENT_IDENTITY_SYNTHETIC_PROVENANCE:
        if len(receipts) != 1 or (
            label and label in str(receipts[0].get("text") or "")
        ):
            invalid("synthetic receipt 语义不闭合")
    else:
        invalid("source_label provenance 不允许持有 v2 receipt")

    if current_receipt != receipts[0]:
        invalid("singular primary 不是 canonical 首项")
    expected_source_ids = list(dict.fromkeys(
        str(value.get("source_segment_id") or "").strip()
        for value in receipts
        if str(value.get("source_segment_id") or "").strip()
    ))
    raw_source_ids = candidate.get("source_segment_ids")
    if (
        not isinstance(raw_source_ids, list)
        or not raw_source_ids
        or any(
            not isinstance(value, str)
            or not value.strip()
            or value != value.strip()
            for value in raw_source_ids
        )
        or len(raw_source_ids) != len(set(raw_source_ids))
    ):
        invalid("source_segment_ids 必须为 exact nonempty unique string list")
    actual_source_ids = list(raw_source_ids)
    if actual_source_ids != expected_source_ids:
        invalid("source_segment_ids 投影不一致")
    primary_source_id = str(current_receipt.get("source_segment_id") or "")
    if str(candidate.get("source_segment_id") or "") != primary_source_id:
        invalid("singular source_segment_id 不一致")
    return dict(current_receipt), receipts, expected_source_ids


def _attach_candidate_source_evidence(
    candidates: list[dict],
    source_text: str,
    *,
    draft_text: str = "",
) -> list[dict]:
    """Bind candidate labels to one owned SRC without guessing from vocabulary."""
    segments = index_source_segments(source_text)
    by_id = {segment.segment_id: segment for segment in segments}
    for candidate in candidates:
        typed_owned = bool(candidate.pop("_typed_source_evidence_owned", False))
        candidate.pop("_current_materialization_compatible", None)
        candidate.pop("_current_response_group_key", None)
        candidate.pop("_current_identity_group_key_synthetic", None)
        current_receipt = candidate.get("source_evidence_receipt")
        current_receipts = candidate.get("source_evidence_receipts")
        label = str(candidate.get("source_label") or "").strip()
        cited_id = str(candidate.get("source_segment_id") or "").strip()
        cited = by_id.get(cited_id)
        if current_receipt is not None or current_receipts is not None:
            try:
                bundle = _validate_current_identity_receipt_bundle(
                    candidate,
                    source_text=source_text,
                    draft_text=draft_text,
                )
            except ContentGenerationError:
                candidate["source_evidence_receipt"] = None
                candidate["source_evidence_receipts"] = []
                candidate["source_segment_id"] = ""
                candidate["source_segment_ids"] = []
                candidate["source_quote"] = ""
                raise
            assert bundle is not None
            current_receipt, receipts, expected_source_ids = bundle
            primary_source_id = str(current_receipt.get("source_segment_id") or "")
            candidate["source_evidence_receipt"] = dict(current_receipt)
            candidate["source_evidence_receipts"] = receipts
            candidate["source_segment_id"] = primary_source_id
            candidate["source_segment_ids"] = expected_source_ids
            candidate["source_quote"] = str(current_receipt.get("text") or "")
            continue
        if typed_owned and cited is not None:
            candidate["source_segment_id"] = cited.segment_id
            candidate["source_quote"] = str(
                candidate.get("source_quote") or cited.text
            )
            continue
        owned = (
            [cited]
            if cited is not None and label and label in cited.text
            else [segment for segment in segments if label and label in segment.text]
        )
        # A short label is accepted only when the cited source span has one
        # occurrence.  Ambiguous spans remain unresolved for structural audit.
        if len(owned) == 1 and (
            len(textmatch.condense(label)) > 3
            or owned[0].text.count(label) == 1
        ):
            candidate["source_segment_id"] = owned[0].segment_id
            model_quote = str(candidate.get("source_quote") or "").strip()
            candidate["source_quote"] = (
                model_quote
                if model_quote and model_quote in owned[0].text and label in model_quote
                else owned[0].text
            )
        else:
            candidate["source_segment_id"] = ""
            candidate["source_quote"] = ""
    return candidates


async def extract_current_identity_candidates(
    source_text: str,
    bible: Bible,
    episode_no: int,
    *,
    draft_text: str = "",
    existing_resolutions: list[dict] | None = None,
    project_id: str | None = None,
) -> list[dict]:
    """Extract current-episode identities without future or coverage prompts."""
    candidates = await _discover_character_candidates_legacy(
        source_text,
        bible,
        episode_no,
        draft_text=draft_text,
        future_text="",
        existing_resolutions=existing_resolutions,
        project_id=project_id,
    )
    return _attach_candidate_source_evidence(
        candidates,
        source_text,
        draft_text=draft_text,
    )

