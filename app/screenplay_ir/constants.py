"""Module-level IR version/tuning constants and the two typed exceptions the compiler raises."""
from __future__ import annotations

from typing import Any

from app.narrative_blueprint import BlueprintSourceSemantics


IR_VERSION = "screenplay-generation-ir.v4"
IR_COMPILER_VERSION = "screenplay-ir-compiler.v8"
IR_MAX_SOURCE_SEGMENTS_PER_UNIT = 16
IR_MIN_ADAPTED_SOURCE_RATIO = 0.35
IR_MIN_LOCAL_ADAPTED_SOURCE_RATIO = 0.18
IR_LOCAL_SOURCE_WINDOW = 12
_DIALOGUE_FUNCTIONS = {
    "trigger", "announcement", "question", "response", "decision", "statement",
}
_AUDIT_SOURCE_SEMANTICS = BlueprintSourceSemantics(
    narrative_layer="paratext",
    event_priority="connective",
    render_policy="exclude_from_spine",
    disposition="audit_only",
    projection_policy="audit_only",
)
_SourceSemanticIdentity = tuple[str, str, str, str, str, str]
_SourceAuditAnnotationIdentity = tuple[
    str,
    tuple[str, ...],
    str,
    str,
    str,
    str,
]


class ScreenplayIRIdentityConflictError(ValueError):
    """Typed preflight identities still disagree after structural resolution."""

    def __init__(
        self,
        message: str,
        *,
        issues: list[dict[str, Any]] | None = None,
    ) -> None:
        self.issues = list(issues or [])
        super().__init__(message)


class ScreenplayIRFidelityError(ValueError):
    """Typed signal that a structurally valid IR needs bounded fidelity repair."""
