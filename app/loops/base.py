from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

from pydantic import BaseModel

from app.db import get_conn
from app.evaluations.issues import issue_fingerprint
from app.evidence import repository
from app.harness.contracts import get_contract
from app.harness.types import Evaluation, EvidenceArtifact, Issue, IssueSeverity
from app.observability.tracing import bind_trace, current_trace
from app.orchestration.state_machine import transition_step


T = TypeVar("T", bound=BaseModel)
Producer = Callable[[int, str | None, list[Issue], list[list[Issue]]], Awaitable[str]]
Evaluator = Callable[[str], tuple[T | None, list[Issue]]]


@dataclass(frozen=True, slots=True)
class AgentLoopPolicy:
    max_iterations: int = 4
    stall_rounds: int = 2
    min_quality_gain: float = 0.03
    no_gain_rounds: int = 2
    allow_warning_candidate: bool = False


@dataclass(slots=True)
class AgentLoopResult(Generic[T]):
    value: T
    status: str
    exit_reason: str
    issues: list[Issue] = field(default_factory=list)
    iterations: int = 0
    artifact_id: str | None = None


class AgentLoopFailure(RuntimeError):
    def __init__(self, stage_key: str, issues: list[Issue], exit_reason: str, iterations: int):
        self.stage_key = stage_key
        self.issues = issues
        self.exit_reason = exit_reason
        self.iterations = iterations
        super().__init__(
            f"{stage_key} loop failed ({exit_reason}): "
            + "; ".join(issue.message for issue in issues[:5])
        )


def _quality(issues: list[Issue]) -> float:
    weights = {"blocker": 1.0, "warning": 0.35, "info": 0.1}
    penalty = sum(weights[issue.severity.value] for issue in issues)
    return 1.0 / (1.0 + penalty)


class AgentLoop(Generic[T]):
    def __init__(
        self,
        *,
        stage_key: str,
        contract_key: str,
        goal: str,
        scope_type: str,
        scope_id: str,
        artifact_type: str,
        policy: AgentLoopPolicy | None = None,
        input_artifact_ids: list[str] | None = None,
    ):
        self.stage_key = stage_key
        self.contract = get_contract(contract_key)
        self.goal = goal
        self.scope_type = scope_type
        self.scope_id = scope_id
        self.artifact_type = artifact_type
        self.policy = policy or AgentLoopPolicy(
            max_iterations=self.contract.max_iterations,
            stall_rounds=self.contract.stall_rounds,
            min_quality_gain=self.contract.min_quality_gain,
        )
        self.input_artifact_ids = input_artifact_ids or []

    async def run(self, producer: Producer, evaluator: Evaluator) -> AgentLoopResult[T]:
        issue_history: list[list[Issue]] = []
        fingerprint_history: list[str] = []
        previous_raw: str | None = None
        previous_quality: float | None = None
        no_gain_count = 0
        last_value: T | None = None
        last_value_issues: list[Issue] = []
        last_value_artifact_id: str | None = None
        exit_reason = "max_iterations"

        for iteration_no in range(1, self.policy.max_iterations + 1):
            iteration_step_id = self._start_iteration(iteration_no)
            try:
                if iteration_step_id:
                    trace = current_trace()
                    with bind_trace(trace.run_id or "", iteration_step_id):
                        raw = await producer(
                            iteration_no, previous_raw, issue_history[-1] if issue_history else [], issue_history
                        )
                else:
                    raw = await producer(
                        iteration_no, previous_raw, issue_history[-1] if issue_history else [], issue_history
                    )
                value, issues = evaluator(raw)
            except asyncio.CancelledError:
                if iteration_step_id:
                    transition_step(
                        iteration_step_id, "RUNNING", "CANCELLED", "cancelled",
                        decision="cancel",
                    )
                raise
            except Exception as exc:
                if iteration_step_id:
                    transition_step(
                        iteration_step_id, "RUNNING", "FAILED", str(exc)[:1000],
                        decision="escalate", error_code=type(exc).__name__.upper(),
                    )
                raise

            previous_raw = raw
            issue_history.append(issues)
            fingerprint = issue_fingerprint(issues)
            fingerprint_history.append(fingerprint)
            # Structural failures have no comparable candidate quality.  Do
            # not count malformed JSON / schema-invalid output as a no-gain
            # round: a later repair may expose a different structural issue,
            # which is progress and must receive the remaining repair budget.
            quality = _quality(issues) if value is not None else 0.0
            if value is not None:
                if previous_quality is not None:
                    gain = quality - previous_quality
                    no_gain_count = no_gain_count + 1 if gain < self.policy.min_quality_gain else 0
                previous_quality = quality
            artifact_id = self._record_candidate(
                iteration_step_id, iteration_no, raw, value, issues, quality
            )
            if value is not None:
                # Keep the fallback value, its evaluation, and its artifact as
                # one coherent snapshot.  A later T0 response must never be
                # attached to an earlier schema-valid value.
                last_value = value
                last_value_issues = list(issues)
                last_value_artifact_id = artifact_id

            if not issues and value is not None:
                if iteration_step_id:
                    transition_step(
                        iteration_step_id, "RUNNING", "SUCCEEDED", "contract_passed",
                        decision="accept", output_artifact_id=artifact_id,
                    )
                return AgentLoopResult(
                    value=value,
                    status="accepted",
                    exit_reason="contract_passed",
                    iterations=iteration_no,
                    artifact_id=artifact_id,
                )

            stalled = (
                len(fingerprint_history) >= self.policy.stall_rounds
                and len(set(fingerprint_history[-self.policy.stall_rounds:])) == 1
            )
            no_gain = value is not None and no_gain_count >= self.policy.no_gain_rounds
            exhausted = iteration_no >= self.policy.max_iterations
            if stalled:
                exit_reason = "stalled"
            elif no_gain:
                exit_reason = "no_quality_gain"
            elif exhausted:
                exit_reason = "max_iterations"
            else:
                exit_reason = "repair_requested"

            if iteration_step_id:
                conn = get_conn()
                conn.execute(
                    "UPDATE step_runs SET issue_fingerprint=? WHERE id=?",
                    (fingerprint, iteration_step_id),
                )
                conn.commit()
                transition_step(
                    iteration_step_id,
                    "RUNNING",
                    "WARNING",
                    exit_reason,
                    decision="escalate" if exit_reason != "repair_requested" else "repair",
                    output_artifact_id=artifact_id,
                )
            if stalled or no_gain or exhausted:
                break

        if self.policy.allow_warning_candidate and last_value is not None:
            blockers = [
                issue for issue in last_value_issues
                if issue.severity == IssueSeverity.BLOCKER
            ]
            # PRD VAL-422：warning candidate 只能接受非 blocker；带 blocker 不得冒充通过。
            if not blockers:
                return AgentLoopResult(
                    value=last_value,
                    status="warning",
                    exit_reason=exit_reason,
                    issues=last_value_issues,
                    iterations=len(issue_history),
                    artifact_id=last_value_artifact_id,
                )
            # 不可满足容量等 blocker 连续 stalled：标记 NEEDS_REPLAN，由 Supervisor 接管。
            needs_replan = exit_reason in {"stalled", "no_quality_gain", "max_iterations"}
            capacity_codes = {
                "SPOKEN_CAPACITY_EXCEEDED",
                "SHOT_OUTLINE_COVERAGE",
                "KEY_LINE_MISSING",
                "SPINE_MISSING",
            }
            if needs_replan and any(
                issue.code in capacity_codes
                or "口播" in issue.message
                or "容量" in issue.message
                or ("超过" in issue.message and "字" in issue.message)
                for issue in blockers
            ):
                object.__setattr__(last_value, "disposition", "NEEDS_REPLAN")
                return AgentLoopResult(
                    value=last_value,
                    status="needs_replan",
                    exit_reason="needs_replan",
                    issues=blockers,
                    iterations=len(issue_history),
                    artifact_id=last_value_artifact_id,
                )
        raise AgentLoopFailure(
            self.stage_key,
            issue_history[-1] if issue_history else [],
            exit_reason,
            len(issue_history),
        )

    def _start_iteration(self, iteration_no: int) -> str | None:
        trace = current_trace()
        if not trace.run_id:
            return None
        input_artifact_ids = list(self.input_artifact_ids)
        if not input_artifact_ids and trace.step_run_id:
            row = get_conn().execute(
                "SELECT input_artifact_ids_json FROM step_runs WHERE id=?",
                (trace.step_run_id,),
            ).fetchone()
            if row:
                try:
                    input_artifact_ids = json.loads(row["input_artifact_ids_json"] or "[]")
                except json.JSONDecodeError:
                    input_artifact_ids = []
        self.input_artifact_ids = input_artifact_ids
        step_id = repository.create_step(
            trace.run_id,
            f"{self.stage_key}.iteration",
            iteration_no=iteration_no,
            parent_step_run_id=trace.step_run_id,
            agent_name=self.stage_key,
            contract_version=self.contract.version,
            input_artifact_ids=input_artifact_ids,
            context_manifest={"goal": self.goal},
        )
        transition_step(step_id, "PENDING", "READY", "context_ready")
        transition_step(step_id, "READY", "RUNNING", "iteration_started")
        repository.append_event(
            trace.run_id,
            "AGENT_ITERATION_STARTED",
            "info",
            f"{self.stage_key} 第 {iteration_no} 轮开始",
            step_run_id=step_id,
        )
        return step_id

    def _record_candidate(
        self,
        step_run_id: str | None,
        iteration_no: int,
        raw: str,
        value: T | None,
        issues: list[Issue],
        quality: float,
    ) -> str | None:
        if not step_run_id:
            return None
        artifact = repository.create_artifact(
            EvidenceArtifact(
                type=self.artifact_type,
                scope_type=self.scope_type,
                scope_id=self.scope_id,
                status="validated" if value is not None and not issues else "candidate",
                trust_level="T2" if value is not None and not issues else ("T1" if value is not None else "T0"),
                content=value.model_dump(mode="json") if value is not None else {"raw_output": raw},
                parent_artifact_ids=self.input_artifact_ids,
                contract_version=self.contract.version,
            ),
            step_run_id=step_run_id,
        )
        evaluation = Evaluation(
            evaluator_type="deterministic",
            evaluator_name=f"{self.stage_key}_validator",
            evaluator_version=self.contract.version,
            status="passed" if not issues else "failed",
            hard_gate_passed=not issues,
            score=round(quality * 100, 2),
            issues=issues,
            evidence={"iteration_no": iteration_no, "goal": self.goal},
        )
        if not issues:
            committed = repository.commit_artifact(step_run_id, artifact["id"], [evaluation])
            return str(committed["id"])
        repository.create_evaluation(artifact["id"], evaluation, step_run_id=step_run_id)
        return str(artifact["id"])
