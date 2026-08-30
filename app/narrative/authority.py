"""Storyboard authority projection: the display-agnostic canonical facts view.

Moved verbatim out of the pre-split ``app/narrative.py`` (see
``app/narrative/__init__.py`` for the package-split rationale). This is the
sole function in the package with zero in-package callers -- it is a public
API consumed only by ``app.production.certificate`` and ``app.domain.*`` for
authority comparisons and completion certificates, kept in its own file
because nothing else in this package depends on it.
"""
from __future__ import annotations

from typing import Any

from app.schemas import Storyboard


def storyboard_authority_projection(
    value: Storyboard | dict[str, Any],
) -> dict[str, Any]:
    """Return authored storyboard facts, excluding display-only numbering.

    Episode identity is the stable episode scope id carried by Artifacts and
    certificates. ``episode_no`` controls ordering and directory presentation;
    project compaction may change it without authoring a new story.  Every
    authority comparison must therefore bind the complete typed shot contracts
    while treating that display number as non-narrative metadata.
    """
    board = value if isinstance(value, Storyboard) else Storyboard.model_validate(value)
    payload = board.model_dump(mode="json")
    payload.pop("episode_no", None)
    from app.continuity import PROMPT_CONTRACT_VERSION

    for shot in payload.get("shots") or []:
        # QA/display annotations are mutable sidecar evidence. They must never
        # revoke an immutable storyboard release or trigger paid regeneration.
        shot.pop("risk_tags", None)
        if not shot.get("prompt_contract_version"):
            shot["prompt_contract_version"] = PROMPT_CONTRACT_VERSION
    return payload
