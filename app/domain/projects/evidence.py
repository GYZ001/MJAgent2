"""删除项目/分集时清理其名下的 Harness 证据（run/step/artifact/provider_call）。

供 :mod:`app.domain.projects.episode_delete`（单集删除）与
:mod:`app.domain.projects.lifecycle`（项目彻底清理）共用同一套「按 scope 递归
收集再批量删」的逻辑——两者只是 scope 粒度不同（project vs episode），删除的
表集合与递归子 run 收集完全一致，因此合并成一个模块而不是各写一份。
"""
from __future__ import annotations

from app.domain.projects.sql_helpers import (
    _delete_scope_rows,
    _execute_by_in,
    _ids_by_in,
    _in_chunks,
    _marks,
    _scope_ids,
)


def _present_refs_error(conn, value: str | None) -> str | None:
    """Legacy errors are immutable display data; prose never changes semantics."""
    _ = conn
    return value


def _delete_scoped_evidence(
    conn,
    *,
    scope_ids: list[str],
    scope_prefix: str,
    episode_ids: list[str],
) -> dict[str, int]:
    """Delete Harness evidence owned by one project or episode subtree."""
    run_ids = _scope_ids(
        conn,
        "workflow_runs",
        scope_ids=scope_ids,
        scope_prefix=scope_prefix,
    )
    # Recovery/child runs can use their own scope. Include the whole descendant
    # chain so no run keeps a parent pointer to a deleted project run.
    frontier = set(run_ids)
    while frontier:
        children: set[str] = set()
        for chunk in _in_chunks(frontier):
            marks = _marks(chunk)
            children.update(
                row["id"] for row in conn.execute(
                    f"""SELECT id FROM workflow_runs
                        WHERE parent_run_id IN ({marks})
                           OR recovered_by_run_id IN ({marks})""",
                    [*chunk, *chunk],
                ).fetchall()
            )
        children -= run_ids
        run_ids.update(children)
        frontier = children

    step_ids: set[str] = set()
    if run_ids:
        step_ids = _ids_by_in(
            conn,
            "SELECT id FROM step_runs WHERE run_id IN ({marks})",
            run_ids,
        )

    artifact_ids = _scope_ids(
        conn,
        "artifacts",
        scope_ids=scope_ids,
        scope_prefix=scope_prefix,
    )
    if step_ids:
        artifact_ids.update(_ids_by_in(
            conn,
            "SELECT id FROM artifacts WHERE created_by_step_run_id IN ({marks})",
            step_ids,
        ))
    provider_call_ids: set[object] = set()
    if run_ids:
        provider_call_ids.update(_ids_by_in(
            conn,
            "SELECT id FROM provider_calls WHERE run_id IN ({marks})",
            run_ids,
        ))
    if step_ids:
        provider_call_ids.update(_ids_by_in(
            conn,
            "SELECT id FROM provider_calls WHERE step_run_id IN ({marks})",
            step_ids,
        ))

    if episode_ids:
        _execute_by_in(
            conn,
            "DELETE FROM delivery_packages WHERE episode_id IN ({marks})",
            episode_ids,
        )
        _execute_by_in(
            conn,
            "DELETE FROM customer_feedback WHERE episode_id IN ({marks})",
            episode_ids,
        )
        _execute_by_in(
            conn,
            "DELETE FROM production_revisions WHERE episode_id IN ({marks})",
            episode_ids,
        )
        _execute_by_in(
            conn,
            "DELETE FROM production_grants WHERE episode_id IN ({marks})",
            episode_ids,
        )
        _execute_by_in(
            conn,
            "DELETE FROM completion_grants WHERE episode_id IN ({marks})",
            episode_ids,
        )
        _execute_by_in(
            conn,
            "DELETE FROM completion_certificates WHERE scope_id IN ({marks})",
            episode_ids,
        )

    if artifact_ids:
        _execute_by_in(
            conn,
            "DELETE FROM gate_decisions WHERE artifact_id IN ({marks})",
            artifact_ids,
        )
        _execute_by_in(
            conn,
            "DELETE FROM evaluations WHERE artifact_id IN ({marks})",
            artifact_ids,
        )
        _execute_by_in(
            conn,
            "DELETE FROM completion_certificates WHERE artifact_id IN ({marks})",
            artifact_ids,
        )
        _execute_by_in(
            conn,
            "UPDATE artifacts SET superseded_by_artifact_id=NULL "
            "WHERE superseded_by_artifact_id IN ({marks})",
            artifact_ids,
        )
        _execute_by_in(
            conn,
            "DELETE FROM artifacts WHERE id IN ({marks})",
            artifact_ids,
        )

    if run_ids:
        _execute_by_in(conn, "DELETE FROM gate_decisions WHERE run_id IN ({marks})", run_ids)
        _execute_by_in(conn, "DELETE FROM run_events WHERE run_id IN ({marks})", run_ids)
        _execute_by_in(conn, "DELETE FROM agent_tool_calls WHERE run_id IN ({marks})", run_ids)
    if step_ids:
        _execute_by_in(conn, "DELETE FROM evaluations WHERE step_run_id IN ({marks})", step_ids)
        _execute_by_in(conn, "DELETE FROM run_events WHERE step_run_id IN ({marks})", step_ids)
    if provider_call_ids:
        _execute_by_in(
            conn,
            "UPDATE provider_calls SET supersedes_call_id=NULL "
            "WHERE supersedes_call_id IN ({marks})",
            provider_call_ids,
        )
        _execute_by_in(
            conn,
            "UPDATE provider_calls SET superseded_by_call_id=NULL "
            "WHERE superseded_by_call_id IN ({marks})",
            provider_call_ids,
        )
        _execute_by_in(conn, "DELETE FROM provider_calls WHERE id IN ({marks})", provider_call_ids)
    if run_ids:
        _execute_by_in(conn, "DELETE FROM step_runs WHERE run_id IN ({marks})", run_ids)
        _execute_by_in(
            conn,
            "UPDATE workflow_runs SET parent_run_id=NULL "
            "WHERE parent_run_id IN ({marks})",
            run_ids,
        )
        _execute_by_in(
            conn,
            "UPDATE workflow_runs SET recovered_by_run_id=NULL "
            "WHERE recovered_by_run_id IN ({marks})",
            run_ids,
        )
        _execute_by_in(conn, "DELETE FROM workflow_runs WHERE id IN ({marks})", run_ids)

    _delete_scope_rows(
        conn,
        "review_action_audit",
        scope_ids=scope_ids,
        scope_prefix=scope_prefix,
    )
    return {
        "artifacts": len(artifact_ids),
        "runs": len(run_ids),
        "steps": len(step_ids),
    }


def _delete_project_evidence(conn, project_id: str) -> dict[str, int]:
    """Delete project-scoped Harness evidence before removing business rows."""
    episode_ids = [
        row["id"] for row in conn.execute(
            "SELECT id FROM episodes WHERE project_id=?", (project_id,)
        ).fetchall()
    ]
    shot_ids = [
        row["id"] for row in conn.execute(
            """SELECT s.id FROM shots s
               JOIN episodes e ON e.id=s.episode_id WHERE e.project_id=?""",
            (project_id,),
        ).fetchall()
    ]
    return _delete_scoped_evidence(
        conn,
        scope_ids=[project_id, *episode_ids, *shot_ids],
        scope_prefix=project_id,
        episode_ids=episode_ids,
    )


def _delete_episode_evidence(conn, episode_id: str) -> dict[str, int]:
    """Delete only the evidence rooted at one episode and its shots."""
    shot_ids = [
        row["id"] for row in conn.execute(
            "SELECT id FROM shots WHERE episode_id=?", (episode_id,)
        ).fetchall()
    ]
    return _delete_scoped_evidence(
        conn,
        scope_ids=[episode_id, *shot_ids],
        scope_prefix=episode_id,
        episode_ids=[episode_id],
    )
