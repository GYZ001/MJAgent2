"""Narrative review-report pass/fail and audience-facing payload hashing.

Moved verbatim out of the pre-split ``app/narrative.py`` (see
``app/narrative/__init__.py`` for the package-split rationale).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from app.schemas import NarrativeReviewReport


def narrative_review_passes(report: NarrativeReviewReport | None) -> bool:
    return bool(report and report.decision == "pass" and all(
        result.result == "satisfied" for result in report.target_delta_results
    ))


def audience_perceptual_surface_hash(payload: dict[str, Any]) -> str:
    """Return the stable identity of one exact audience-facing payload."""
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
