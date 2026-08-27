from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from app.db import get_conn, now


RUN_TRANSITIONS: dict[str, set[str]] = {
    "CREATED": {"RUNNING", "CANCELLED"},
    "RUNNING": {
        "WAITING_RETRY", "WAITING_HUMAN", "WAITING_AUTHORIZATION",
        "PAUSED_BUDGET", "PAUSED_EXTERNAL",
        "SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED",
    },
    "WAITING_RETRY": {"RUNNING", "WAITING_HUMAN", "FAILED", "CANCELLED"},
    "WAITING_HUMAN": {"RUNNING", "PARTIAL", "FAILED", "CANCELLED"},
    "WAITING_AUTHORIZATION": {"RUNNING", "WAITING_HUMAN", "PARTIAL", "FAILED", "CANCELLED"},
    "PAUSED_BUDGET": {"RUNNING", "WAITING_HUMAN", "PARTIAL", "FAILED", "CANCELLED"},
    "PAUSED_EXTERNAL": {"RUNNING", "WAITING_RETRY", "WAITING_HUMAN", "FAILED", "CANCELLED"},
    "SUCCEEDED": set(),
    "PARTIAL": set(),
    "FAILED": set(),
    "CANCELLED": set(),
}

STEP_TRANSITIONS: dict[str, set[str]] = {
    "PENDING": {"READY", "SKIPPED", "CANCELLED"},
    "READY": {"RUNNING", "SKIPPED", "CANCELLED"},
    "RUNNING": {"EVALUATING", "REPAIRING", "WAITING_HUMAN", "SUCCEEDED", "WARNING", "FAILED", "CANCELLED"},
    "EVALUATING": {"REPAIRING", "WAITING_HUMAN", "SUCCEEDED", "WARNING", "FAILED", "CANCELLED"},
    "REPAIRING": {"RUNNING", "FAILED", "CANCELLED"},
    "WAITING_HUMAN": {"SUCCEEDED", "WARNING", "FAILED", "CANCELLED"},
    "SUCCEEDED": set(),
    "WARNING": set(),
    "FAILED": set(),
    "CANCELLED": set(),
    "SKIPPED": set(),
}


class StateConflict(RuntimeError):
    def __init__(self, entity: str, entity_id: str, expected: set[str], actual: str | None):
        self.entity = entity
        self.entity_id = entity_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"{entity} {entity_id} state conflict: expected {sorted(expected)}, actual {actual}"
        )


def _expected_set(expected_from: str | Iterable[str]) -> set[str]:
    return {expected_from} if isinstance(expected_from, str) else set(expected_from)


def _validate_transition(graph: dict[str, set[str]], expected: set[str], target: str) -> None:
    invalid = [source for source in expected if target not in graph.get(source, set())]
    if invalid:
        raise ValueError(f"illegal state transition {sorted(invalid)} -> {target}")


def _cas_status(
    conn: sqlite3.Connection,
    *,
    table: str,
    entity: str,
    entity_id: str,
    expected: set[str],
    target: str,
    assignments: dict[str, object | None],
) -> None:
    columns = ["status=?", *(f"{name}=?" for name in assignments)]
    params = [target, *assignments.values(), entity_id, *sorted(expected)]
    placeholders = ",".join("?" for _ in expected)
    cursor = conn.execute(
        f"UPDATE {table} SET {', '.join(columns)} WHERE id=? AND status IN ({placeholders})",
        params,
    )
    if cursor.rowcount != 1:
        row = conn.execute(f"SELECT status FROM {table} WHERE id=?", (entity_id,)).fetchone()
        raise StateConflict(entity, entity_id, expected, row["status"] if row else None)


# ``conn`` has no default on either transition function below (was
# ``sqlite3.Connection | None = None`` -- callers that omitted it silently
# got ``get_conn()``'s ambient task-cached connection *and* an implicit
# commit on it, see the ``if conn is None: db.commit()`` branches). That
# silent fallback is exactly the "隐式提交家族" bug pattern that already
# corrupted real data three times (see app.db.insert_error_log's docstring
# and app.video_supervisor.save_checkpoint's history) -- a caller holding
# an uncommitted multi-statement transaction on that same connection would
# have it flushed the moment anything called ``transition_run``/
# ``transition_step`` without thinking about it. Making ``conn`` a required
# keyword (still accepts an explicit ``None`` to opt into the ambient-
# connection-and-commit-now behavior, or a specific connection to share the
# caller's already-open transaction and defer the commit to them) forces
# every call site to make that choice visibly, and turns a forgotten
# argument into a ``TypeError`` at the call site instead of a silently
# wrong commit boundary three frames down. Same precedent as
# ``app/multiview.py``'s ``conn`` becoming a hard dependency.
def transition_run(
    run_id: str,
    expected_from: str | Iterable[str],
    to: str,
    reason: str,
    *,
    failure_code: str | None = None,
    conn: sqlite3.Connection | None,
) -> None:
    expected = _expected_set(expected_from)
    _validate_transition(RUN_TRANSITIONS, expected, to)
    db = conn or get_conn()
    stamp = now()
    terminal = to in {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"}
    assignments: dict[str, object | None] = {
        "updated_at": stamp,
        "finished_at": stamp if terminal else None,
        "failure_code": failure_code,
        # ``failure_message`` is also the persisted user-facing wait reason for
        # active non-terminal states in the Run dock.
        "failure_message": reason if to in {
            "FAILED", "PARTIAL", "WAITING_RETRY", "WAITING_HUMAN",
            "WAITING_AUTHORIZATION", "PAUSED_EXTERNAL", "PAUSED_BUDGET",
        } else None,
    }
    if to == "RUNNING":
        # Resuming WAITING_RETRY/paused work must not reset elapsed time in the
        # Harness UI or destroy the original execution start timestamp.
        row = db.execute(
            "SELECT started_at FROM workflow_runs WHERE id=?", (run_id,)
        ).fetchone()
        assignments["started_at"] = row["started_at"] if row and row["started_at"] else stamp
    _cas_status(
        db, table="workflow_runs", entity="run", entity_id=run_id,
        expected=expected, target=to, assignments=assignments,
    )
    if conn is None:
        db.commit()


def transition_step(
    step_run_id: str,
    expected_from: str | Iterable[str],
    to: str,
    reason: str,
    *,
    decision: str | None = None,
    output_artifact_id: str | None = None,
    error_code: str | None = None,
    conn: sqlite3.Connection | None,
) -> None:
    expected = _expected_set(expected_from)
    _validate_transition(STEP_TRANSITIONS, expected, to)
    db = conn or get_conn()
    stamp = now()
    terminal = to in {"SUCCEEDED", "WARNING", "FAILED", "CANCELLED", "SKIPPED"}
    row = db.execute("SELECT started_at FROM step_runs WHERE id=?", (step_run_id,)).fetchone()
    started = row["started_at"] if row else None
    assignments: dict[str, object | None] = {
        "started_at": stamp if to == "RUNNING" and started is None else started,
        "finished_at": stamp if terminal else None,
        "latency_ms": max(0, int((stamp - started) * 1000)) if terminal and started else 0,
        "decision": decision,
        "exit_reason": reason,
        "output_artifact_id": output_artifact_id,
        "error_code": error_code,
        "error_message": reason if to == "FAILED" else None,
    }
    _cas_status(
        db, table="step_runs", entity="step", entity_id=step_run_id,
        expected=expected, target=to, assignments=assignments,
    )
    if conn is None:
        db.commit()
