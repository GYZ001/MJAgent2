from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import time
from typing import Any

from app.db import get_conn, new_id, now, rows_to_dicts, run_write_transaction
from app.harness.types import Evaluation, EvidenceArtifact


ACTIVE_RUN_STATUSES = {
    "CREATED", "RUNNING", "WAITING_RETRY", "WAITING_HUMAN",
    "WAITING_AUTHORIZATION", "PAUSED_BUDGET", "PAUSED_EXTERNAL",
}
JSON_FIELDS = {
    "policy_snapshot_json", "config_snapshot_json", "input_artifact_ids_json",
    "context_manifest_json", "parent_artifact_ids_json", "model_snapshot_json",
    "dimension_scores_json", "issues_json", "evidence_json", "payload_json", "content_json",
}
_EVENT_LOCK_RETRY_DELAYS_S = (0.05, 0.1, 0.25)
_LOGGER = logging.getLogger(__name__)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for row in rows:
        for field in JSON_FIELDS.intersection(row):
            value = row.get(field)
            if value is not None:
                try:
                    row[field.removesuffix("_json")] = json.loads(value)
                except (TypeError, json.JSONDecodeError):
                    row[field.removesuffix("_json")] = None
    return rows


def _is_score_only_evaluation(evaluation: Evaluation) -> bool:
    if evaluation.evaluation_role == "score_only":
        return True
    return (
        evaluation.evaluator_type == "model"
        and evaluation.runtime_blocking is False
        and _is_model_qa_name(evaluation.evaluator_name)
    )


def _is_score_only_evaluation_row(row: dict[str, Any]) -> bool:
    if row.get("evaluation_role") == "score_only":
        return True
    return (
        row.get("evaluator_type") == "model"
        and not bool(row.get("runtime_blocking"))
        and _is_model_qa_name(str(row.get("evaluator_name") or ""))
    )


def _is_model_qa_name(evaluator_name: str) -> bool:
    lowered = evaluator_name.lower()
    return "qa" in lowered or "quality" in lowered


_WORKFLOW_LABELS: dict[str, str] = {
    "character_bible": "人物谱生成",
    "character_references": "人物定妆照",
    "scene_bible": "场景设定",
    "scene_references": "场景参考图",
    "episode_mapping": "分集规划",
    "screenplay": "剧本生成",
    "storyboard": "分镜生成",
    "scene_generation": "关键帧生成",
    "video_generation": "视频生成",
    "episode_video_completion": "全片视频补齐",
    "delivery": "交付",
    "delivery_package": "交付候选生成",
}


def _workflow_label(workflow_type: str) -> str:
    if not workflow_type:
        return "未命名流程"
    return _WORKFLOW_LABELS.get(workflow_type, workflow_type)


def content_hash(content: Any | None = None, file_path: str | None = None) -> str:
    digest = hashlib.sha256()
    if content is not None:
        digest.update(b"json\0")
        digest.update(_json(content).encode("utf-8"))
    if file_path:
        digest.update(b"file\0")
        with Path(file_path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    if content is None and not file_path:
        digest.update(b"empty")
    return digest.hexdigest()


def create_run(
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
) -> str:
    run_id = new_id("run")
    stamp = now()
    conn = get_conn()
    conn.execute(
        """INSERT INTO workflow_runs(
            id, workflow_type, scope_type, scope_id, parent_run_id, status,
            requested_by, trigger_type, input_fingerprint, policy_snapshot_json,
            config_snapshot_json, budget_limit_cny, deadline_at, updated_at
        ) VALUES(?,?,?,?,?,'CREATED',?,?,?,?,?,?,?,?)""",
        (
            run_id, workflow_type, scope_type, scope_id, parent_run_id, requested_by,
            trigger_type, input_fingerprint, _json(policy_snapshot or {}),
            _json(config_snapshot or {}), budget_limit_cny, deadline_at, stamp,
        ),
    )
    if parent_run_id:
        # Keep the interrupted attempt immutable and link it to the new attempt.
        # This lets monitoring show "resumed" instead of leaving the parent as a
        # permanently actionable PAUSED_EXTERNAL row.
        conn.execute(
            """UPDATE workflow_runs
               SET recovered_by_run_id=?, recovered_at=?,
                   recovery_count=COALESCE(recovery_count,0)+1,
                   failure_message=CASE
                       WHEN status='PAUSED_EXTERNAL' THEN '服务重启后已自动创建续跑任务'
                       ELSE failure_message
                   END
               WHERE id=? AND recovered_by_run_id IS NULL""",
            (run_id, stamp, parent_run_id),
        )
    conn.commit()
    append_event(run_id, "RUN_CREATED", "info", f"创建运行：{_workflow_label(workflow_type)}")
    if parent_run_id:
        append_event(
            parent_run_id, "RUN_RECOVERED", "info", "已自动创建续跑任务",
            payload={"recovered_by_run_id": run_id},
        )
    return run_id


def create_step(
    run_id: str,
    step_key: str,
    *,
    iteration_no: int = 1,
    parent_step_run_id: str | None = None,
    agent_name: str | None = None,
    contract_version: str | None = None,
    prompt_version: str | None = None,
    policy_version: str | None = None,
    input_artifact_ids: list[str] | None = None,
    context_manifest: dict[str, Any] | None = None,
    conn=None,
) -> str:
    step_id = new_id("step")
    db = conn or get_conn()
    db.execute(
        """INSERT INTO step_runs(
            id, run_id, step_key, iteration_no, parent_step_run_id, status, agent_name,
            contract_version, prompt_version, policy_version, input_artifact_ids_json,
            context_manifest_json
        ) VALUES(?,?,?,?,?,'PENDING',?,?,?,?,?,?)""",
        (
            step_id, run_id, step_key, iteration_no, parent_step_run_id, agent_name,
            contract_version, prompt_version, policy_version, _json(input_artifact_ids or []),
            _json(context_manifest or {}),
        ),
    )
    if conn is None:
        db.commit()
    return step_id


def _event_values(
    run_id: str,
    event_type: str,
    severity: str,
    message: str,
    *,
    step_run_id: str | None,
    payload: dict[str, Any] | None,
    trace_id: str | None,
) -> tuple[Any, ...]:
    return (
        new_id("evt"),
        run_id,
        step_run_id,
        now(),
        event_type,
        severity,
        message,
        _json(payload or {}),
        trace_id,
    )


def _insert_event(conn, values: tuple[Any, ...]) -> None:
    conn.execute(
        "INSERT INTO run_events(id, run_id, step_run_id, ts, event_type, severity, message, payload_json, trace_id) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        values,
    )


def append_event(
    run_id: str,
    event_type: str,
    severity: str,
    message: str,
    *,
    step_run_id: str | None = None,
    payload: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> str:
    conn = get_conn()
    values = _event_values(
        run_id,
        event_type,
        severity,
        message,
        step_run_id=step_run_id,
        payload=payload,
        trace_id=trace_id,
    )
    event_id = str(values[0])
    for attempt in range(len(_EVENT_LOCK_RETRY_DELAYS_S) + 1):
        try:
            _insert_event(conn, values)
            conn.commit()
            return event_id
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
            conn.rollback()
            if attempt >= len(_EVENT_LOCK_RETRY_DELAYS_S):
                _LOGGER.warning(
                    "Dropped run event after SQLite lock retries: run=%s type=%s",
                    run_id,
                    event_type,
                )
                return ""
            time.sleep(_EVENT_LOCK_RETRY_DELAYS_S[attempt])
    return ""


async def async_append_event(
    run_id: str,
    event_type: str,
    severity: str,
    message: str,
    *,
    step_run_id: str | None = None,
    payload: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> str:
    """Append an event off-loop while preserving the sync API's drop semantics."""
    values = _event_values(
        run_id,
        event_type,
        severity,
        message,
        step_run_id=step_run_id,
        payload=payload,
        trace_id=trace_id,
    )
    event_id = str(values[0])
    for attempt in range(len(_EVENT_LOCK_RETRY_DELAYS_S) + 1):
        try:
            await run_write_transaction(
                lambda conn: _insert_event(conn, values),
                retry_delays=(),
            )
            return event_id
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
            if attempt >= len(_EVENT_LOCK_RETRY_DELAYS_S):
                _LOGGER.warning(
                    "Dropped run event after SQLite lock retries: run=%s type=%s",
                    run_id,
                    event_type,
                )
                return ""
            await asyncio.sleep(_EVENT_LOCK_RETRY_DELAYS_S[attempt])
    return ""


def create_artifact(
    artifact: EvidenceArtifact,
    *,
    step_run_id: str | None = None,
    conn=None,
    commit: bool = True,
) -> dict[str, Any]:
    if artifact.content is None and artifact.file_path and not Path(artifact.file_path).is_file():
        raise FileNotFoundError(artifact.file_path)
    db = conn or get_conn()
    version = db.execute(
        "SELECT COALESCE(MAX(version),0)+1 AS version FROM artifacts "
        "WHERE type=? AND scope_type=? AND scope_id=?",
        (artifact.type, artifact.scope_type, artifact.scope_id),
    ).fetchone()["version"]
    artifact_id = artifact.id or new_id("art")
    digest = content_hash(artifact.content, artifact.file_path)
    db.execute(
        """INSERT INTO artifacts(
            id, type, scope_type, scope_id, version, status, trust_level, content_json,
            file_path, content_hash, created_by_step_run_id, parent_artifact_ids_json,
            contract_version, prompt_version, model_snapshot_json, created_at, approved_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            artifact_id, artifact.type, artifact.scope_type, artifact.scope_id, version,
            artifact.status, artifact.trust_level,
            _json(artifact.content) if artifact.content is not None else None,
            artifact.file_path, digest, step_run_id, _json(artifact.parent_artifact_ids),
            artifact.contract_version, artifact.prompt_version, _json(artifact.model_snapshot),
            now(), now() if artifact.status == "approved" else None,
        ),
    )
    if commit:
        db.commit()
    if step_run_id:
        if not commit:
            raise ValueError("事务内创建 Artifact 不支持立即写入 run event")
        row = db.execute("SELECT run_id FROM step_runs WHERE id=?", (step_run_id,)).fetchone()
        if row:
            append_event(
                row["run_id"], "ARTIFACT_CREATED", "info", f"产物已创建：{artifact.type} v{version}",
                step_run_id=step_run_id, payload={"artifact_id": artifact_id, "content_hash": digest},
            )
    return get_artifact(artifact_id, conn=db) or {}


def create_evaluation(
    artifact_id: str,
    evaluation: Evaluation,
    *,
    step_run_id: str | None = None,
    conn=None,
    commit: bool = True,
) -> dict[str, Any]:
    evaluation_id = new_id("eval")
    db = conn or get_conn()
    db.execute(
        """INSERT INTO evaluations(
            id, artifact_id, step_run_id, evaluator_type, evaluator_name, evaluator_version,
            status, hard_gate_passed, evaluation_role, score_status, runtime_blocking,
            retry_eligible, score, dimension_scores_json, issues_json, evidence_json,
            raw_result_ref, confidence, recovered, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            evaluation_id, artifact_id, step_run_id, evaluation.evaluator_type,
            evaluation.evaluator_name, evaluation.evaluator_version, evaluation.status,
            int(evaluation.hard_gate_passed), evaluation.evaluation_role,
            evaluation.score_status, int(evaluation.runtime_blocking),
            int(evaluation.retry_eligible), evaluation.score, _json(evaluation.dimension_scores),
            _json([issue.model_dump(mode="json") for issue in evaluation.issues]),
            _json(evaluation.evidence), evaluation.raw_result_ref, evaluation.confidence,
            int(evaluation.recovered), now(),
        ),
    )
    if commit:
        db.commit()
    row = db.execute(
        "SELECT * FROM evaluations WHERE id=?",
        (evaluation_id,),
    ).fetchone()
    return _decode_rows([dict(row)])[0] if row else {}


def _active_published_release_ids(conn) -> set[str]:
    """Return artifacts protected by an atomically consumed release."""
    try:
        rows = conn.execute(
            """SELECT DISTINCT pr.published_artifact_id AS artifact_id
                 FROM production_revisions pr
                 JOIN episodes e ON e.id=pr.episode_id
                 JOIN completion_certificates cert
                   ON cert.production_revision_id=pr.id
                  AND cert.artifact_id=pr.published_artifact_id
                  AND cert.kind=pr.kind
                  AND cert.scope_id=pr.episode_id
                WHERE pr.status='published'
                  AND pr.published_artifact_id IS NOT NULL
                  AND pr.published_artifact_id!=''
                  AND cert.consumed_at IS NOT NULL
                  AND (
                    (
                      pr.kind='screenplay'
                      AND e.published_screenplay_artifact_id=pr.published_artifact_id
                      AND e.screenplay_completion_certificate_id=cert.id
                    )
                    OR
                    (
                      pr.kind='storyboard'
                      AND e.published_storyboard_artifact_id=pr.published_artifact_id
                      AND e.storyboard_completion_certificate_id=cert.id
                    )
                  )"""
        ).fetchall()
    except sqlite3.OperationalError:
        # Small unit-test and legacy schemas may not own release tables yet.
        return set()
    return {
        str(row["artifact_id"])
        for row in rows
        if row["artifact_id"]
    }


def create_and_commit_artifact_in_transaction(
    conn,
    artifact: EvidenceArtifact,
    evaluations: list[Evaluation],
    *,
    step_run_id: str | None = None,
) -> dict[str, Any]:
    """Create and adopt an artifact without committing the caller's transaction."""
    if artifact.content is None and artifact.file_path and not Path(artifact.file_path).is_file():
        raise FileNotFoundError(artifact.file_path)
    if not evaluations:
        raise ValueError("artifact commit requires at least one evaluation")
    gate_evaluations = [
        evaluation for evaluation in evaluations
        if not _is_score_only_evaluation(evaluation)
        and not (
            evaluation.evaluator_type == "human"
            and evaluation.evaluator_name == "storyboard_editor"
            and not evaluation.hard_gate_passed
        )
    ]
    if any(
        not evaluation.hard_gate_passed or evaluation.status in {"failed", "error"}
        for evaluation in gate_evaluations
    ):
        raise ValueError("hard gate evaluation failed")
    if any(
        issue.severity.value == "blocker"
        for evaluation in gate_evaluations
        for issue in evaluation.issues
    ):
        raise ValueError("unresolved blocker prevents artifact commit")
    if any(evaluation.recovered for evaluation in gate_evaluations):
        raise ValueError("recovered evaluation cannot independently commit an artifact")

    version = conn.execute(
        "SELECT COALESCE(MAX(version),0)+1 AS version FROM artifacts "
        "WHERE type=? AND scope_type=? AND scope_id=?",
        (artifact.type, artifact.scope_type, artifact.scope_id),
    ).fetchone()["version"]
    artifact_id = artifact.id or new_id("art")
    artifact_hash = content_hash(artifact.content, artifact.file_path)
    evaluator_types = {evaluation.evaluator_type for evaluation in gate_evaluations}
    if artifact.type == "delivery_package" and {"human", "file"}.issubset(evaluator_types):
        trust_level = "T5"
    elif "human" in evaluator_types:
        trust_level = "T4"
    elif evaluator_types.intersection({"model", "file"}):
        trust_level = "T3"
    else:
        trust_level = "T2"
    stamp = now()
    conn.execute(
        """INSERT INTO artifacts(
               id,type,scope_type,scope_id,version,status,trust_level,content_json,
               file_path,content_hash,created_by_step_run_id,parent_artifact_ids_json,
               contract_version,prompt_version,model_snapshot_json,created_at,approved_at
           ) VALUES(?,?,?,?,?,'approved',?,?,?,?,?,?,?,?,?,?,?)""",
        (
            artifact_id, artifact.type, artifact.scope_type, artifact.scope_id, version,
            trust_level, _json(artifact.content) if artifact.content is not None else None,
            artifact.file_path, artifact_hash, step_run_id, _json(artifact.parent_artifact_ids),
            artifact.contract_version, artifact.prompt_version, _json(artifact.model_snapshot),
            stamp, stamp,
        ),
    )
    for evaluation in evaluations:
        conn.execute(
            """INSERT INTO evaluations(
                   id,artifact_id,step_run_id,evaluator_type,evaluator_name,evaluator_version,
                   status,hard_gate_passed,evaluation_role,score_status,runtime_blocking,
                   retry_eligible,score,dimension_scores_json,issues_json,evidence_json,
                   raw_result_ref,confidence,recovered,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                new_id("eval"), artifact_id, step_run_id, evaluation.evaluator_type,
                evaluation.evaluator_name, evaluation.evaluator_version, evaluation.status,
                int(evaluation.hard_gate_passed), evaluation.evaluation_role,
                evaluation.score_status, int(evaluation.runtime_blocking),
                int(evaluation.retry_eligible), evaluation.score,
                _json(evaluation.dimension_scores),
                _json([issue.model_dump(mode="json") for issue in evaluation.issues]),
                _json(evaluation.evidence), evaluation.raw_result_ref,
                evaluation.confidence, int(evaluation.recovered), stamp,
            ),
        )
    release_fences = _active_published_release_ids(conn)
    previous = [
        str(row["id"])
        for row in conn.execute(
            """SELECT id FROM artifacts
                 WHERE type=? AND scope_type=? AND scope_id=?
                   AND status='approved' AND id!=?""",
            (artifact.type, artifact.scope_type, artifact.scope_id, artifact_id),
        ).fetchall()
        if str(row["id"]) not in release_fences
    ]
    conn.executemany(
        "UPDATE artifacts SET status='superseded',superseded_by_artifact_id=? WHERE id=?",
        [(artifact_id, previous_id) for previous_id in previous],
    )
    for previous_id in previous:
        descendants = list_descendants(
            previous_id,
            exclude_ids={artifact_id, *release_fences},
        )
        conn.executemany(
            "UPDATE artifacts SET status='stale',stale_reason=? "
            "WHERE id=? AND status!='rejected'",
            [
                (f"上游产物已由 {artifact_id} 替代", descendant_id)
                for descendant_id in descendants
            ],
        )
    if step_run_id:
        conn.execute(
            "UPDATE step_runs SET output_artifact_id=?,decision='accept' WHERE id=?",
            (artifact_id, step_run_id),
        )
    row = conn.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
    return _decode_rows([dict(row)])[0] if row else {}


def commit_artifact(
    step_run_id: str | None,
    artifact_id: str,
    evaluations: list[Evaluation],
) -> dict[str, Any]:
    """Adopt an artifact only when explicit evidence satisfies the hard gates."""
    artifact = get_artifact(artifact_id)
    if not artifact:
        raise KeyError(f"artifact not found: {artifact_id}")
    if artifact["status"] == "stale":
        raise ValueError("stale artifact cannot be committed")
    if not evaluations:
        raise ValueError("artifact commit requires at least one evaluation")
    gate_evaluations = [
        evaluation for evaluation in evaluations
        if not _is_score_only_evaluation(evaluation)
    ]
    if any(not evaluation.hard_gate_passed or evaluation.status in {"failed", "error"}
           for evaluation in gate_evaluations):
        raise ValueError("hard gate evaluation failed")
    if any(issue.severity.value == "blocker" for evaluation in gate_evaluations for issue in evaluation.issues):
        raise ValueError("unresolved blocker prevents artifact commit")
    if any(evaluation.recovered for evaluation in gate_evaluations):
        raise ValueError("recovered evaluation cannot independently commit an artifact")

    created_evaluations = [
        create_evaluation(artifact_id, evaluation, step_run_id=step_run_id)
        for evaluation in evaluations
    ]
    evaluator_types = {
        evaluation.evaluator_type for evaluation in gate_evaluations
    } | {
        row["evaluator_type"] for row in get_evaluations(artifact_id)
        if row["hard_gate_passed"] and row["status"] not in {"failed", "error"}
        and not _is_score_only_evaluation_row(row)
    }
    if artifact["type"] == "delivery_package" and {"human", "file"}.issubset(evaluator_types):
        trust_level = "T5"
    elif "human" in evaluator_types:
        trust_level = "T4"
    elif evaluator_types.intersection({"model", "file"}):
        trust_level = "T3"
    else:
        trust_level = "T2"

    conn = get_conn()
    release_fences = _active_published_release_ids(conn)
    previous = rows_to_dicts(conn.execute(
        """SELECT id FROM artifacts
           WHERE type=? AND scope_type=? AND scope_id=?
             AND status='approved' AND id!=?""",
        (artifact["type"], artifact["scope_type"], artifact["scope_id"], artifact_id),
    ).fetchall())
    previous = [
        row for row in previous
        if str(row["id"]) not in release_fences
    ]
    conn.execute(
        "UPDATE artifacts SET status='approved', trust_level=?, approved_at=? WHERE id=?",
        (trust_level, now(), artifact_id),
    )
    conn.executemany(
        "UPDATE artifacts SET status='superseded', superseded_by_artifact_id=? WHERE id=?",
        [(artifact_id, row["id"]) for row in previous],
    )
    if step_run_id:
        conn.execute(
            "UPDATE step_runs SET output_artifact_id=?, decision='accept' WHERE id=?",
            (artifact_id, step_run_id),
        )
    conn.commit()
    for row in previous:
        invalidate_descendants(
            row["id"],
            f"上游产物已由 {artifact_id} 替代",
            exclude_ids={artifact_id},
        )
    step = (
        conn.execute("SELECT run_id FROM step_runs WHERE id=?", (step_run_id,)).fetchone()
        if step_run_id
        else None
    )
    if step:
        append_event(
            step["run_id"], "ARTIFACT_COMMITTED", "info", f"产物已采用：{artifact_id}",
            step_run_id=step_run_id,
            payload={
                "artifact_id": artifact_id,
                "trust_level": trust_level,
                "evaluation_ids": [item["id"] for item in created_evaluations],
            },
        )
    return get_artifact(artifact_id) or {}


def get_run(run_id: str) -> dict[str, Any] | None:
    row = get_conn().execute("SELECT * FROM workflow_runs WHERE id=?", (run_id,)).fetchone()
    return _decode_rows([dict(row)])[0] if row else None


def get_active_scoped_run(
    run_id: str | None,
    *,
    workflow_type: str,
    scope_type: str,
    scope_id: str,
    conn=None,
) -> dict[str, Any] | None:
    """Return an active run only when its persisted identity matches the pointer."""
    if not run_id:
        return None
    db = conn or get_conn()
    row = db.execute(
        "SELECT * FROM workflow_runs WHERE id=? AND workflow_type=? "
        "AND scope_type=? AND scope_id=?",
        (run_id, workflow_type, scope_type, scope_id),
    ).fetchone()
    if row is None or row["status"] not in ACTIVE_RUN_STATUSES:
        return None
    return _decode_rows([dict(row)])[0]


def list_runs(*, active: bool | None = None, project_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if active is True:
        clauses.append(f"status IN ({','.join('?' for _ in ACTIVE_RUN_STATUSES)})")
        params.extend(sorted(ACTIVE_RUN_STATUSES))
    elif active is False:
        clauses.append(f"status NOT IN ({','.join('?' for _ in ACTIVE_RUN_STATUSES)})")
        params.extend(sorted(ACTIVE_RUN_STATUSES))
    if project_id:
        clauses.append("(scope_type='project' AND scope_id=?)")
        params.append(project_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(limit, 500)))
    rows = rows_to_dicts(get_conn().execute(
        f"SELECT * FROM workflow_runs {where} ORDER BY updated_at DESC LIMIT ?", params
    ).fetchall())
    return _decode_rows(rows)


def get_steps(run_id: str) -> list[dict[str, Any]]:
    rows = rows_to_dicts(get_conn().execute(
        "SELECT * FROM step_runs WHERE run_id=? ORDER BY COALESCE(started_at, 0), iteration_no, id",
        (run_id,),
    ).fetchall())
    return _decode_rows(rows)


def get_events(run_id: str, *, after: float | None = None, limit: int = 500) -> list[dict[str, Any]]:
    params: list[Any] = [run_id]
    clause = ""
    if after is not None:
        clause = "AND ts>?"
        params.append(after)
    params.append(max(1, min(limit, 1000)))
    rows = rows_to_dicts(get_conn().execute(
        f"SELECT * FROM run_events WHERE run_id=? {clause} ORDER BY ts, id LIMIT ?", params
    ).fetchall())
    return _decode_rows(rows)


def get_artifact(artifact_id: str, *, conn=None) -> dict[str, Any] | None:
    db = conn or get_conn()
    row = db.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
    return _decode_rows([dict(row)])[0] if row else None


def latest_artifact(
    artifact_type: str,
    scope_type: str,
    scope_id: str,
    *,
    include_stale: bool = False,
) -> dict[str, Any] | None:
    stale_clause = "" if include_stale else "AND status!='stale'"
    row = get_conn().execute(
        f"""SELECT * FROM artifacts
            WHERE type=? AND scope_type=? AND scope_id=? {stale_clause}
            ORDER BY version DESC LIMIT 1""",
        (artifact_type, scope_type, scope_id),
    ).fetchone()
    return _decode_rows([dict(row)])[0] if row else None


def get_evaluations(artifact_id: str) -> list[dict[str, Any]]:
    rows = rows_to_dicts(get_conn().execute(
        "SELECT * FROM evaluations WHERE artifact_id=? ORDER BY created_at, id", (artifact_id,)
    ).fetchall())
    return _decode_rows(rows)


def get_lineage(artifact_id: str) -> dict[str, Any]:
    """Return immutable ancestors and descendants for evidence UI/review."""
    rows = _decode_rows(rows_to_dicts(get_conn().execute(
        "SELECT * FROM artifacts ORDER BY created_at, version"
    ).fetchall()))
    by_id = {row["id"]: row for row in rows}
    ancestors: list[dict[str, Any]] = []
    descendants: list[dict[str, Any]] = []
    pending = list((by_id.get(artifact_id) or {}).get("parent_artifact_ids") or [])
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current in seen or current not in by_id:
            continue
        seen.add(current)
        ancestors.append(by_id[current])
        pending.extend(by_id[current].get("parent_artifact_ids") or [])
    pending = [artifact_id]
    seen = {artifact_id}
    while pending:
        parent = pending.pop()
        for row in rows:
            if row["id"] in seen or parent not in (row.get("parent_artifact_ids") or []):
                continue
            seen.add(row["id"])
            descendants.append(row)
            pending.append(row["id"])
    return {"artifact": by_id.get(artifact_id), "ancestors": ancestors, "descendants": descendants}


def pending_human_gates(*, project_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    clauses = ["a.status IN ('candidate','validated')"]
    params: list[Any] = []
    if project_id:
        clauses.append("(a.scope_type='project' AND a.scope_id=? OR e.project_id=?)")
        params.extend([project_id, project_id])
    params.append(max(1, min(limit, 500)))
    rows = rows_to_dicts(get_conn().execute(
        f"""SELECT a.*, COALESCE(e.project_id, scope_project.id) AS project_id,
                    COALESCE(episode_project.name, scope_project.name) AS project_name,
                    e.episode_no, e.title AS episode_title,
                    sr.run_id
            FROM artifacts a
            LEFT JOIN episodes e ON a.scope_type='episode' AND e.id=a.scope_id
            LEFT JOIN projects episode_project ON episode_project.id=e.project_id
            LEFT JOIN projects scope_project ON a.scope_type='project' AND scope_project.id=a.scope_id
            LEFT JOIN step_runs sr ON sr.id=a.created_by_step_run_id
            WHERE {' AND '.join(clauses)}
              AND a.type IN ('character_bible','episode_screenplay','storyboard','delivery_package')
              AND NOT EXISTS (
                SELECT 1 FROM gate_decisions g WHERE g.artifact_id=a.id
                  AND g.decision IN ('approve','approve_with_risk','reject','ignore')
              )
            ORDER BY a.created_at LIMIT ?""",
        params,
    ).fetchall())
    return _decode_rows(rows)


def list_descendants(
    artifact_id: str,
    *,
    exclude_ids: set[str] | None = None,
    conn=None,
) -> list[str]:
    """Return descendant artifact ids without mutating status (impact preview)."""
    if not artifact_id:
        return []
    db = conn or get_conn()
    all_rows = rows_to_dicts(db.execute(
        "SELECT id, parent_artifact_ids_json FROM artifacts"
    ).fetchall())
    children: dict[str, list[str]] = {}
    for row in all_rows:
        try:
            parents = json.loads(row["parent_artifact_ids_json"] or "[]")
        except json.JSONDecodeError:
            parents = []
        for parent in parents:
            children.setdefault(parent, []).append(row["id"])
    found: list[str] = []
    excluded = exclude_ids or set()
    pending = list(children.get(artifact_id, []))
    seen: set[str] = set()
    while pending:
        child_id = pending.pop()
        if child_id in seen or child_id in excluded:
            continue
        seen.add(child_id)
        found.append(child_id)
        pending.extend(children.get(child_id, []))
    return found


def invalidate_descendants(
    artifact_id: str,
    reason: str,
    *,
    exclude_ids: set[str] | None = None,
    conn=None,
    commit: bool = True,
) -> list[str]:
    """Mark all descendants stale while preserving their immutable evidence rows."""
    db = conn or get_conn()
    excluded = set(exclude_ids or set())
    excluded.update(_active_published_release_ids(db))
    stale = list_descendants(artifact_id, exclude_ids=excluded, conn=db)
    if stale:
        db.executemany(
            "UPDATE artifacts SET status='stale', stale_reason=? WHERE id=? AND status!='rejected'",
            [(reason, child_id) for child_id in stale],
        )
        if commit:
            db.commit()
    return stale
