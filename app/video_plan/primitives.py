"""Shared video-plan primitives: the validation error type and canonical
JSON/hash/row-access helpers used across every other file in this package.

Moved verbatim out of the pre-split ``app/video_plan.py`` (see
``app/video_plan/__init__.py`` for the package-split rationale).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


class VideoPlanValidationError(ValueError):
    def __init__(self, issues: list[dict[str, Any]]):
        self.issues = issues
        super().__init__(json.dumps(issues, ensure_ascii=False))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key] if key in row.keys() else default
    except (AttributeError, KeyError, TypeError):
        return default
