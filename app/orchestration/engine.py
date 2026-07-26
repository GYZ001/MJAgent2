from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from app.db import get_conn, now
from app.evidence import repository
from app.harness.contracts import get_contract
from app.harness.types import EvidenceArtifact
from app.orchestration.state_machine import transition_run, transition_step
from app.observability.tracing import bind_trace


T = TypeVar("T")


def fingerprint(*parts: Any) -> str:
    encoded = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class WorkflowRecorder:
    """Sidecar recorder for existing workflows during the Phase 1 migration.

    It owns persisted run/step state while the existing business coroutine remains
    the executor.  This keeps current behavior stable and lets later phases replace
    the executor without changing the evidence model.
    """

    def __init__(self, run_id: str):
        self.run_id = run_id

    @classmethod
    def create(
        cls,
        *,
        workflow_type: str,
        scope_type: str,
        scope_id: str,
        input_fingerprint: str,
        requested_by: str = "user",
        trigger_type: str = "manual",
        policy_snapshot: dict[str, Any] | None = None,
        config_snapshot: dict[str, Any] | None = None,
        budget_limit_cny: float | None = None,
        deadline_at: float | None = None,
        parent_run_id: str | None = None,
    ) -> "WorkflowRecorder":
        run_id = repository.create_run(
            workflow_type=workflow_type,
            scope_type=scope_type,
            scope_id=scope_id,
            input_fingerprint=input_fingerprint,
            requested_by=requested_by,
            trigger_type=trigger_type,
            policy_snapshot=policy_snapshot,
            config_snapshot=config_snapshot,
            budget_limit_cny=budget_limit_cny,
            deadline_at=deadline_at,
            parent_run_id=parent_run_id,
        )
        return cls(run_id)

    def start(self) -> None:
        transition_run(self.run_id, {"CREATED", "PAUSED_EXTERNAL", "WAITING_RETRY"}, "RUNNING", "运行开始")
        repository.append_event(self.run_id, "RUN_STARTED", "info", "运行开始")

    async def step(
        self,
        step_key: str,
        operation: Callable[[], Awaitable[T]],
        *,
        contract_key: str | None = None,
        agent_name: str | None = None,
        iteration_no: int = 1,
        input_artifact_ids: list[str] | None = None,
        context_manifest: dict[str, Any] | None = None,
    ) -> tuple[str, T]:
        contract = get_contract(contract_key) if contract_key else None
        step_id = repository.create_step(
            self.run_id,
            step_key,
            iteration_no=iteration_no,
            agent_name=agent_name,
            contract_version=contract.version if contract else None,
            input_artifact_ids=input_artifact_ids,
            context_manifest=context_manifest,
        )
        transition_step(step_id, "PENDING", "READY", "输入已就绪")
        transition_step(step_id, "READY", "RUNNING", "步骤开始")
        conn = get_conn()
        conn.execute(
            "UPDATE workflow_runs SET current_step_key=?, updated_at=? WHERE id=? AND status='RUNNING'",
            (step_key, now(), self.run_id),
        )
        conn.commit()
        repository.append_event(
            self.run_id, "STEP_STARTED", "info", f"步骤开始：{step_key}", step_run_id=step_id
        )
        try:
            with bind_trace(self.run_id, step_id):
                result = await operation()
        except asyncio.CancelledError:
            transition_step(step_id, "RUNNING", "CANCELLED", "运行被取消", decision="cancel")
            repository.append_event(
                self.run_id, "STEP_CANCELLED", "warning", f"步骤已取消：{step_key}", step_run_id=step_id
            )
            raise
        except Exception as exc:
            transition_step(
                step_id, "RUNNING", "FAILED", str(exc)[:1000], decision="escalate",
                error_code=type(exc).__name__.upper(),
            )
            repository.append_event(
                self.run_id, "STEP_FAILED", "error", f"步骤失败：{step_key}",
                step_run_id=step_id,
                payload={"error_type": type(exc).__name__, "message": str(exc)[:1000]},
            )
            raise
        transition_step(step_id, "RUNNING", "SUCCEEDED", "步骤完成", decision="accept")
        repository.append_event(
            self.run_id, "STEP_SUCCEEDED", "info", f"步骤完成：{step_key}", step_run_id=step_id
        )
        return step_id, result

    def artifact(self, step_run_id: str, artifact: EvidenceArtifact) -> dict[str, Any]:
        created = repository.create_artifact(artifact, step_run_id=step_run_id)
        conn = get_conn()
        conn.execute(
            "UPDATE step_runs SET output_artifact_id=? WHERE id=?",
            (created["id"], step_run_id),
        )
        conn.commit()
        return created

    def refresh_cost(self) -> float:
        """Project the currently attributable media spend onto the persisted run."""
        conn = get_conn()
        row = conn.execute(
            """SELECT COALESCE(SUM(v.cost_cny), 0) AS total
               FROM jobs j
               LEFT JOIN shot_versions v ON v.id=j.version_id
               WHERE j.run_id=?""",
            (self.run_id,),
        ).fetchone()
        total = float(row["total"] if row else 0)
        conn.execute(
            "UPDATE workflow_runs SET cost_cny=?, updated_at=? WHERE id=?",
            (total, now(), self.run_id),
        )
        conn.commit()
        return total

    def succeed(self, message: str = "运行完成") -> None:
        self.refresh_cost()
        transition_run(self.run_id, "RUNNING", "SUCCEEDED", message)
        repository.append_event(self.run_id, "RUN_SUCCEEDED", "info", message)

    def partial(self, message: str) -> None:
        self.refresh_cost()
        transition_run(self.run_id, "RUNNING", "PARTIAL", message, failure_code="PARTIAL_RESULT")
        repository.append_event(self.run_id, "RUN_PARTIAL", "warning", message)

    def fail(self, exc: BaseException) -> None:
        self.refresh_cost()
        transition_run(
            self.run_id, "RUNNING", "FAILED", str(exc)[:1000], failure_code=type(exc).__name__.upper()
        )
        repository.append_event(
            self.run_id, "RUN_FAILED", "error", "运行失败",
            payload={"error_type": type(exc).__name__, "message": str(exc)[:1000]},
        )

    def cancel(self, message: str = "运行已取消") -> None:
        self.refresh_cost()
        transition_run(
            self.run_id,
            {"CREATED", "RUNNING", "WAITING_RETRY", "WAITING_HUMAN", "PAUSED_BUDGET", "PAUSED_EXTERNAL"},
            "CANCELLED",
            message,
        )
        repository.append_event(self.run_id, "RUN_CANCELLED", "warning", message)
