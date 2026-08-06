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


# stage_key 是稳定的技术标识，写入 message 时映射成对用户友好的中文名。
_STAGE_KEY_LABELS: dict[str, str] = {
    "character_bible": "人物谱",
    "character_references": "人物定妆照",
    "scene_bible": "场景设定",
    "scene_references": "场景参考图",
    "episode_mapping": "分集规划",
    "screenplay": "剧本",
    "storyboard": "分镜",
    "storyboard_outline": "分镜大纲",
    "storyboard_shot": "分镜镜头",
    "storyboard_repair": "分镜修复",
    "scene_generation": "关键帧",
    "video_generation": "视频",
}


def _stage_label(stage_key: str) -> str:
    if not stage_key:
        return "未命名阶段"
    return _STAGE_KEY_LABELS.get(stage_key, stage_key)


@dataclass(frozen=True, slots=True)
class AgentLoopPolicy:
    max_iterations: int = 4
    stall_rounds: int = 2
    min_quality_gain: float = 0.03
    no_gain_rounds: int = 2
    allow_warning_candidate: bool = False
    # Some business rules are hard production contracts for a specific loop,
    # even though Phase 3 normally records business QA as score-only.  Callers
    # opt those codes into model repair without changing the global QA policy.
    repair_issue_codes: frozenset[str] = field(default_factory=frozenset)
    # Authority artifacts may not downgrade business blockers to score-only
    # quality.  Their loop repairs every blocker until the contract passes or
    # fails closed.
    repair_all_blockers: bool = False
    # Production Repair：只跑一轮完整生成，无论 QA 是否通过都交出候选给局部 Patch Agent。
    # 禁止再用“重新输出完整 JSON”的修复轮。
    baseline_only: bool = False
    # Isolated repair candidates must not supersede approved upstream artifacts
    # before their enclosing transaction passes the full gate.
    commit_accepted_artifact: bool = True


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


_STRUCTURAL_CODE_NAMES = {
    "JSON",
    "SCHEMA",
    "PARSE",
    "REQUIRED",
    "MISSING_FIELD",
    "TYPE_ERROR",
    "INVALID_ENUM",
    "CONTRACT",
}
_STRUCTURAL_CODE_PREFIXES = tuple(f"{name}_" for name in _STRUCTURAL_CODE_NAMES)
_STRUCTURAL_CODE_MARKERS = (
    "schema",
    "json",
    "required_field",
    "missing_field",
    "type_error",
    "invalid_enum",
    "parse",
)


def is_structural_issue(issue: Issue) -> bool:
    """Return True for parse/schema/contract-shape failures only."""
    code = (issue.code or "").strip()
    upper_code = code.upper()
    lower_code = code.lower()
    return (
        upper_code == "SOURCE_FIDELITY"
        or upper_code in _STRUCTURAL_CODE_NAMES
        or upper_code.startswith(_STRUCTURAL_CODE_PREFIXES)
        or any(marker in lower_code for marker in _STRUCTURAL_CODE_MARKERS)
    )


def split_structural_quality_issues(issues: list[Issue]) -> tuple[list[Issue], list[Issue]]:
    structural: list[Issue] = []
    quality: list[Issue] = []
    for issue in issues:
        (structural if is_structural_issue(issue) else quality).append(issue)
    return structural, quality


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
        prompt_version: str | None = None,
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
        self.prompt_version = prompt_version

    async def run(self, producer: Producer, evaluator: Evaluator) -> AgentLoopResult[T]:
        issue_history: list[list[Issue]] = []
        fingerprint_history: list[str] = []
        previous_raw: str | None = None
        previous_quality: float | None = None
        no_gain_count = 0
        last_value: T | None = None
        last_value_issues: list[Issue] = []
        last_value_artifact_id: str | None = None
        latest_value: T | None = None
        latest_issues: list[Issue] = []
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

            latest_value = value
            latest_issues = list(issues)
            previous_raw = raw
            structural_issues, quality_issues = split_structural_quality_issues(issues)
            targeted_repair_issues = [
                issue
                for issue in quality_issues
                if (
                    issue.code in self.policy.repair_issue_codes
                    or (
                        self.policy.repair_all_blockers
                        and issue.severity == IssueSeverity.BLOCKER
                    )
                )
            ]
            repair_issues = (
                [*structural_issues, *targeted_repair_issues]
                if value is not None
                else (structural_issues or issues)
            )
            issue_history.append(repair_issues)
            fingerprint = issue_fingerprint(repair_issues)
            fingerprint_history.append(fingerprint)
            # Structural failures have no comparable candidate quality.  Do
            # not count malformed JSON / schema-invalid output as a no-gain
            # round: a later repair may expose a different structural issue,
            # which is progress and must receive the remaining repair budget.
            quality = _quality(repair_issues) if value is not None else 0.0
            if value is not None:
                if previous_quality is not None:
                    gain = quality - previous_quality
                    no_gain_count = no_gain_count + 1 if gain < self.policy.min_quality_gain else 0
                previous_quality = quality
            score_only_quality = value is not None and bool(issues) and not repair_issues
            artifact_id = self._record_candidate(
                iteration_step_id,
                iteration_no,
                raw,
                value,
                issues,
                _quality(issues) if value is not None else quality,
                score_only_quality=score_only_quality,
            )
            if value is not None:
                # Keep the fallback value, its evaluation, and its artifact as
                # one coherent snapshot.  A later T0 response must never be
                # attached to an earlier schema-valid value.
                last_value = value
                last_value_issues = list(issues)
                last_value_artifact_id = artifact_id

            if value is not None and not repair_issues:
                exit_reason = "contract_passed" if not issues else "score_only_quality"
                if iteration_step_id:
                    transition_step(
                        iteration_step_id, "RUNNING", "SUCCEEDED", exit_reason,
                        decision="accept", output_artifact_id=artifact_id,
                    )
                return AgentLoopResult(
                    value=value,
                    status="accepted",
                    exit_reason=exit_reason,
                    issues=list(issues),
                    iterations=iteration_no,
                    artifact_id=artifact_id,
                )

            # Baseline-only：首轮结束后立即交出可解析候选（含 blocker），交由 Production Repair。
            if self.policy.baseline_only and value is not None:
                if iteration_step_id:
                    transition_step(
                        iteration_step_id, "RUNNING", "WARNING", "baseline_handoff",
                        decision="repair", output_artifact_id=artifact_id,
                    )
                return AgentLoopResult(
                    value=value,
                    status="baseline",
                    exit_reason="baseline_handoff",
                    issues=list(issues),
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

        if last_value is not None:
            blockers = [
                issue for issue in last_value_issues
                if issue.severity == IssueSeverity.BLOCKER
            ]
            # 容量类先交给 Supervisor 尝试改规划；Supervisor 耗尽后会发布
            # 当前最佳分镜。其他结构/内容门禁在本循环耗尽后直接降为告警。
            needs_replan = exit_reason in {"stalled", "no_quality_gain", "max_iterations"}
            capacity_codes = {
                "SPOKEN_CAPACITY_EXCEEDED",
                "ACTION_CAPACITY_EXCEEDED",
                "SHOT_OUTLINE_COVERAGE",
                "KEY_LINE_MISSING",
                "SPINE_MISSING",
            }
            if self.policy.allow_warning_candidate and needs_replan and any(
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
            if self.policy.repair_all_blockers and blockers:
                reported: list[Issue] = []
                seen: set[tuple[str, str]] = set()
                for issue in (
                    [*latest_issues, *blockers]
                    if latest_value is None
                    else blockers
                ):
                    identity = (issue.fingerprint, issue.message)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    reported.append(issue)
                raise AgentLoopFailure(
                    self.stage_key,
                    reported,
                    "authority_blockers_exhausted",
                    len(issue_history),
                )
            if last_value_artifact_id and self.policy.commit_accepted_artifact:
                try:
                    repository.commit_artifact(
                        None,
                        last_value_artifact_id,
                        [Evaluation(
                            evaluator_type="deterministic",
                            evaluator_name=f"{self.stage_key}_retry_exhausted_fallback",
                            evaluator_version=self.contract.version,
                            status="warning",
                            hard_gate_passed=False,
                            evaluation_role="score_only",
                            score_status="scored",
                            runtime_blocking=False,
                            retry_eligible=False,
                            score=round(_quality(last_value_issues) * 100, 2),
                            issues=last_value_issues,
                            evidence={
                                "gate_retry_exhausted": True,
                                "original_exit_reason": exit_reason,
                                "iterations": len(issue_history),
                            },
                        )],
                    )
                except Exception:  # noqa: BLE001 - 保底值仍可交付，证据可后续补记
                    pass
            return AgentLoopResult(
                value=last_value,
                status="warning",
                exit_reason="gate_retry_exhausted_fallback",
                issues=last_value_issues,
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
            f"{_stage_label(self.stage_key)} 第 {iteration_no} 轮开始",
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
        *,
        score_only_quality: bool = False,
    ) -> str | None:
        if not step_run_id:
            return None
        accepted_candidate = value is not None and (not issues or score_only_quality)
        artifact = repository.create_artifact(
            EvidenceArtifact(
                type=self.artifact_type,
                scope_type=self.scope_type,
                scope_id=self.scope_id,
                status="validated" if accepted_candidate else "candidate",
                trust_level="T2" if accepted_candidate else ("T1" if value is not None else "T0"),
                content=value.model_dump(mode="json") if value is not None else {"raw_output": raw},
                parent_artifact_ids=self.input_artifact_ids,
                contract_version=self.contract.version,
                prompt_version=self.prompt_version,
            ),
            step_run_id=step_run_id,
        )
        evaluation = Evaluation(
            evaluator_type="deterministic",
            evaluator_name=f"{self.stage_key}_validator",
            evaluator_version=self.contract.version,
            status="passed" if not issues else ("warning" if score_only_quality else "failed"),
            hard_gate_passed=not issues or score_only_quality,
            evaluation_role="score_only" if score_only_quality else None,
            score_status="scored" if score_only_quality else None,
            runtime_blocking=False if score_only_quality else True,
            score=round(quality * 100, 2),
            issues=issues,
            evidence={"iteration_no": iteration_no, "goal": self.goal},
        )
        if accepted_candidate:
            if not self.policy.commit_accepted_artifact:
                repository.create_evaluation(
                    artifact["id"],
                    evaluation,
                    step_run_id=step_run_id,
                )
                return str(artifact["id"])
            committed = repository.commit_artifact(step_run_id, artifact["id"], [evaluation])
            return str(committed["id"])
        repository.create_evaluation(artifact["id"], evaluation, step_run_id=step_run_id)
        return str(artifact["id"])
