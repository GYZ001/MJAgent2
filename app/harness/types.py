from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class IssueSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


class Issue(BaseModel):
    code: str
    severity: IssueSeverity
    subject: str
    message: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    repair_hint: str | None = None
    repairable: bool = False

    @property
    def fingerprint(self) -> str:
        # ``span`` used to be the only discriminator.  Compatibility adapters
        # set it to the whole stage subject, so unrelated schema failures (for
        # example broken JSON followed by a bad field type) looked identical
        # and made the Agent Loop stop as ``stalled`` after two useful repairs.
        # Prefer the canonical field/rule identity emitted by the evaluator;
        # keep span as a fallback for older callers.
        path = self.evidence.get("path") or self.evidence.get("span", "")
        rule_id = self.evidence.get("rule_id", "")
        return f"{self.code}:{self.subject}:{path}:{rule_id}"


class StageContract(BaseModel):
    key: str
    version: str
    input_types: list[str]
    output_type: str
    invariants: list[str] = Field(default_factory=list)
    max_iterations: int = Field(default=1, ge=1)
    stall_rounds: int = Field(default=2, ge=1)
    min_quality_gain: float = Field(default=0.03, ge=0)
    requires_human_gate: bool = False


class EvidenceArtifact(BaseModel):
    id: str | None = None
    type: str
    scope_type: str
    scope_id: str
    status: Literal[
        "candidate", "needs_revision", "validated", "approved", "rejected", "superseded", "stale"
    ]
    trust_level: Literal["T0", "T1", "T2", "T3", "T4", "T5"]
    content: Any | None = None
    file_path: str | None = None
    parent_artifact_ids: list[str] = Field(default_factory=list)
    contract_version: str | None = None
    prompt_version: str | None = None
    model_snapshot: dict[str, Any] = Field(default_factory=dict)


class Evaluation(BaseModel):
    evaluator_type: Literal["deterministic", "model", "human", "file"]
    evaluator_name: str
    evaluator_version: str
    status: Literal["passed", "warning", "failed", "error"]
    hard_gate_passed: bool
    score: float | None = Field(default=None, ge=0, le=100)
    dimension_scores: dict[str, float] = Field(default_factory=dict)
    issues: list[Issue] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    raw_result_ref: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    recovered: bool = False


class Decision(BaseModel):
    action: Literal[
        "accept", "repair", "regenerate", "switch_model", "escalate", "reject", "cancel"
    ]
    reason: str
    accepted_risk: str | None = None
    decided_by: str = "system"
