"""角色/场景单视图重绘（redo）的任务体、恢复与候选采纳/回滚路由。

从 app/domain/bible_ops.py 按原样搬移。
"""
from __future__ import annotations

import asyncio
import json

from app import (
    task_registry,
)
from app.auth.principal import current_actor_name
from app.db import (
    get_conn,
    new_id,
    now,
)
from app.domain.common import (
    _as_body_dict,
    _project_or_404,
    router,
)
from app.evidence import repository as evidence_repository
from app.orchestration.engine import (
    WorkflowRecorder,
    fingerprint,
)
from fastapi import HTTPException

from .precheck import compute_refs_cost_precheck
from .primitives import (
    _consume_payment_quote,
    _parse_json_value,
    _payment_confirm_required,
    _validate_payment_quote,
)
from .scene_assets import compute_scene_cost_precheck


async def _run_portrait_view_redo(
    project_id: str,
    character_name: str,
    portrait_id: str,
    view_role: str,
    recorder: WorkflowRecorder,
) -> None:
    from app.multiview import regenerate_character_view, pack_result_ok

    recorder.start()
    try:
        async def _op():
            return await regenerate_character_view(
                project_id=project_id, portrait_id=portrait_id, view_role=view_role,
            )

        result = await recorder.step(
            "portrait_view_redo", _op, agent_name="portrait_view_redo",
        )
        if isinstance(result, tuple):
            result = result[1]
        if not pack_result_ok(result):
            recorder.fail(RuntimeError(
                f"视角重做未通过：{view_role}（status={(result or {}).get('status')}）"
            ), conn=None)
            return
        recorder.succeed(f"{character_name}/{view_role} 视角已重做并通过整包 QA", conn=None)
    except asyncio.CancelledError:
        recorder.cancel(conn=None)
        raise
    except Exception as exc:  # noqa: BLE001
        recorder.fail(exc, conn=None)

def _start_portrait_view_redo(
    project_id: str,
    character_name: str,
    portrait_id: str,
    view_role: str,
    *,
    quote_id: str | None,
    budget_limit_cny: float,
    parent_run_id: str | None = None,
    requested_by: str = "user",
    trigger_type: str = "manual",
) -> dict | None:
    task_key = f"{portrait_id}:{view_role}"
    if task_registry.active("portrait_view_redo", task_key):
        return None
    recorder = WorkflowRecorder.create(
        workflow_type="portrait_view_redo",
        scope_type="project",
        scope_id=project_id,
        input_fingerprint=fingerprint(project_id, portrait_id, view_role, quote_id),
        requested_by=requested_by,
        trigger_type=trigger_type,
        config_snapshot={
            "task_key": task_key, "character_name": character_name,
            "portrait_id": portrait_id, "view_role": view_role, "quote_id": quote_id,
            "budget_limit_cny": budget_limit_cny,
        },
        budget_limit_cny=budget_limit_cny,
        parent_run_id=parent_run_id,
    )
    coro = _run_portrait_view_redo(
        project_id, character_name, portrait_id, view_role, recorder,
    )
    try:
        task_registry.spawn(
            "portrait_view_redo", task_key, coro, project_id=project_id,
        )
    except Exception as exc:
        coro.close()
        try:
            recorder.cancel("人物单视角重做未能启动", conn=None)
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError("人物单视角重做任务未能启动，旧定妆包和费用凭证均已保留") from exc
    return {
        "status": "accepted", "task_id": f"portrait_view_redo:{task_key}",
        "run_id": recorder.run_id, "portrait_id": portrait_id,
        "view_role": view_role, "character_name": character_name,
    }

def recover_portrait_view_redo_tasks() -> int:
    """重建进程重启时丢失的单视角异步任务。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, scope_id, config_snapshot_json FROM workflow_runs "
        "WHERE workflow_type='portrait_view_redo' AND status='PAUSED_EXTERNAL' "
        "AND recovered_by_run_id IS NULL ORDER BY updated_at"
    ).fetchall()
    resumed = 0
    for row in rows:
        config = _parse_json_value(row["config_snapshot_json"], {})
        if not isinstance(config, dict):
            continue
        character_name = str(config.get("character_name") or "").strip()
        portrait_id = str(config.get("portrait_id") or "").strip()
        view_role = str(config.get("view_role") or "").strip()
        if not character_name or not portrait_id or not view_role:
            continue
        try:
            started = _start_portrait_view_redo(
                row["scope_id"], character_name, portrait_id, view_role,
                quote_id=config.get("quote_id"),
                budget_limit_cny=float(config.get("budget_limit_cny") or 1),
                parent_run_id=row["id"], requested_by="system", trigger_type="resume",
            )
            if started:
                resumed += 1
        except Exception:
            continue
    return resumed

@router.post("/projects/{project_id}/characters/{character_name}/portraits/{portrait_id}/views/{view_role}/regenerate")
async def regenerate_character_view_route(
    project_id: str, character_name: str, portrait_id: str, view_role: str,
    body: dict | None = None,
):
    """人物谱单视角重做：持久异步任务，立即返回 accepted + run_id。"""
    from app.capabilities.dispatch import ui_route

    payload = body or {}
    routed = await ui_route(
        "portrait.regenerate_view",
        {
            "project_id": project_id, "character_name": character_name,
            "portrait_id": portrait_id, "view_role": view_role,
            "confirm": payload.get("confirm") is True,
            "quote_id": payload.get("quote_id"),
            "idempotency_key": payload.get("idempotency_key") or payload.get("quote_id"),
        },
    )
    if routed is not None:
        return routed
    _project_or_404(project_id)
    if payload.get("confirm") is not True:
        raise HTTPException(
            409,
            detail={
                "code": "PAYMENT_CONFIRM_REQUIRED",
                "message": "必须先完成费用预检并显式确认（confirm=true）",
            },
        )
    quote = compute_refs_cost_precheck(
        project_id, character=character_name, view_role=view_role,
    )
    quote_id = payload.get("quote_id")
    quote_row = _validate_payment_quote(project_id, quote_id, quote)
    if quote_row["consumed_at"] is not None:
        return {
            "status": "accepted", "idempotent_replay": True,
            "task_id": quote_row["consumed_task_id"], "run_id": quote_row["consumed_run_id"],
            "portrait_id": portrait_id, "view_role": view_role,
            "character_name": character_name, "precheck": quote,
        }
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM character_portraits WHERE id=? AND project_id=? AND character_name=?",
        (portrait_id, project_id, character_name),
    ).fetchone()
    if not row:
        raise HTTPException(404, "造型版本不存在")

    task_key = f"{portrait_id}:{view_role}"
    if task_registry.active("portrait_view_redo", task_key):
        active_runs = evidence_repository.list_runs(active=True, project_id=project_id, limit=20)
        existing = next(
            (
                r for r in active_runs
                if r.get("workflow_type") == "portrait_view_redo"
                and (r.get("config_snapshot") or {}).get("task_key") == task_key
            ),
            None,
        )
        return {
            "status": "accepted",
            "task_id": f"portrait_view_redo:{task_key}",
            "run_id": (existing or {}).get("id"),
            "portrait_id": portrait_id,
            "view_role": view_role,
            "message": "该视角重做任务已在运行",
        }

    try:
        started = _start_portrait_view_redo(
            project_id, character_name, portrait_id, view_role,
            quote_id=str(quote_id),
            budget_limit_cny=float(quote.get("max_retry_budget_cny") or 1),
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not started:
        raise HTTPException(409, "该视角重做任务已在运行")
    _consume_payment_quote(
        str(quote_id), task_id=started["task_id"], run_id=started["run_id"],
    )
    return {
        **started, "precheck": quote,
        "message": "单视角重做任务已受理，可刷新查看进度",
    }

async def _run_scene_view_redo(
    project_id: str,
    scene_name: str,
    scene_reference_id: str,
    view_role: str,
    recorder: WorkflowRecorder,
) -> None:
    from app.multiview import pack_result_ok, regenerate_scene_view

    recorder.start()
    try:
        result = await recorder.step(
            "generate_and_single_view_qa_and_pack_qa",
            lambda: regenerate_scene_view(
                project_id=project_id, scene_reference_id=scene_reference_id, view_role=view_role,
            ),
            agent_name="scene_view_redo",
        )
        if not pack_result_ok(result):
            recorder.fail(RuntimeError(
                f"视角重做未通过：{view_role}（status={(result or {}).get('status')}）"
            ), conn=None)
            return
        recorder.succeed(f"{scene_name}/{view_role} 已通过单图及整包 QA 并原子替换", conn=None)
    except asyncio.CancelledError:
        if task_registry.shutdown_in_progress():
            recorder.pause_external("服务重启，场景单视角重做等待自动恢复", conn=None)
        else:
            recorder.cancel(conn=None)
        raise
    except Exception as exc:  # noqa: BLE001
        recorder.fail(exc, conn=None)

def _start_scene_view_redo(
    project_id: str,
    scene_name: str,
    scene_reference_id: str,
    view_role: str,
    *,
    quote_id: str | None,
    budget_limit_cny: float,
    parent_run_id: str | None = None,
    requested_by: str = "user",
    trigger_type: str = "manual",
) -> dict | None:
    task_key = f"{scene_reference_id}:{view_role}"
    if task_registry.active("scene_view_redo", task_key):
        return None
    recorder = WorkflowRecorder.create(
        workflow_type="scene_view_redo", scope_type="project", scope_id=project_id,
        input_fingerprint=fingerprint(project_id, scene_reference_id, view_role, quote_id),
        requested_by=requested_by, trigger_type=trigger_type,
        config_snapshot={
            "task_key": task_key, "scene_name": scene_name,
            "scene_reference_id": scene_reference_id, "view_role": view_role,
            "quote_id": quote_id, "budget_limit_cny": budget_limit_cny,
        },
        budget_limit_cny=budget_limit_cny,
        parent_run_id=parent_run_id,
    )
    coro = _run_scene_view_redo(
        project_id, scene_name, scene_reference_id, view_role, recorder,
    )
    try:
        task_registry.spawn(
            "scene_view_redo", task_key, coro, project_id=project_id,
        )
    except Exception as exc:
        coro.close()
        try:
            recorder.cancel("场景单视角重做未能启动", conn=None)
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError("场景单视角重做任务未能启动，旧场景包和费用凭证均已保留") from exc
    return {
        "status": "accepted", "task_id": f"scene_view_redo:{task_key}",
        "run_id": recorder.run_id, "scene_reference_id": scene_reference_id,
        "scene_name": scene_name, "view_role": view_role,
    }

def recover_scene_view_redo_tasks() -> int:
    """服务重启后从持久运行记录恢复场景单视角重做。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id,scope_id,config_snapshot_json FROM workflow_runs "
        "WHERE workflow_type='scene_view_redo' AND status='PAUSED_EXTERNAL' "
        "AND recovered_by_run_id IS NULL ORDER BY updated_at"
    ).fetchall()
    resumed = 0
    for row in rows:
        snapshot = _parse_json_value(row["config_snapshot_json"], {})
        if not isinstance(snapshot, dict):
            continue
        scene_name = str(snapshot.get("scene_name") or "").strip()
        scene_reference_id = str(snapshot.get("scene_reference_id") or "").strip()
        view_role = str(snapshot.get("view_role") or "").strip()
        if not scene_name or not scene_reference_id or not view_role:
            continue
        try:
            started = _start_scene_view_redo(
                row["scope_id"], scene_name, scene_reference_id, view_role,
                quote_id=snapshot.get("quote_id"),
                budget_limit_cny=float(snapshot.get("budget_limit_cny") or 1),
                parent_run_id=row["id"], requested_by="system", trigger_type="resume",
            )
            if started:
                resumed += 1
        except Exception:
            continue
    return resumed

@router.post(
    "/projects/{project_id}/scenes/{scene_name}/refs/{scene_reference_id}/views/{view_role}/regenerate",
    status_code=202,
)
async def regenerate_scene_view_route(
    project_id: str, scene_name: str, scene_reference_id: str, view_role: str,
    body: dict | None = None,
):
    """场景库单视角重做：预检后异步受理，不在 HTTP 请求中等待生成/整包 QA。"""
    from app.capabilities.dispatch import ui_route
    payload = _as_body_dict(body)
    if not payload.get("quote_id"):
        routed = await ui_route(
            "scene.regenerate_view",
            {
                "project_id": project_id, "scene_name": scene_name,
                "scene_reference_id": scene_reference_id, "view_role": view_role,
                "confirm": payload.get("confirm") is True, "quote_id": payload.get("quote_id"),
            },
        )
        if routed is not None:
            return routed
    _project_or_404(project_id)
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM scene_references WHERE id=? AND project_id=? AND scene_name=?",
        (scene_reference_id, project_id, scene_name),
    ).fetchone()
    if not row:
        raise HTTPException(404, "场景版本不存在")
    quote = compute_scene_cost_precheck(
        project_id, scenes=[scene_name], view_role=view_role,
        scene_reference_id=scene_reference_id, action="regenerate_view",
    )
    if payload.get("confirm") is not True:
        raise _payment_confirm_required(quote)
    quote_row = _validate_payment_quote(project_id, payload.get("quote_id"), quote)
    if quote_row["consumed_at"] is not None:
        return {
            "status": "accepted", "idempotent_replay": True,
            "task_id": quote_row["consumed_task_id"], "run_id": quote_row["consumed_run_id"],
            "precheck": quote,
        }
    task_key = f"{scene_reference_id}:{view_role}"
    if task_registry.active("scene_view_redo", task_key):
        active_runs = evidence_repository.list_runs(active=True, project_id=project_id, limit=50)
        existing = next((run for run in active_runs if run.get("workflow_type") == "scene_view_redo"
                         and (run.get("config_snapshot") or {}).get("task_key") == task_key), None)
        return {
            "status": "accepted", "task_id": f"scene_view_redo:{task_key}",
            "run_id": (existing or {}).get("id"), "precheck": quote,
        }
    started = _start_scene_view_redo(
        project_id, scene_name, scene_reference_id, view_role,
        quote_id=str(payload.get("quote_id")),
        budget_limit_cny=float(quote.get("max_retry_budget_cny") or 1),
    )
    if not started:
        raise HTTPException(409, "该场景视角重做任务已在运行")
    _consume_payment_quote(
        str(payload.get("quote_id")), task_id=started["task_id"], run_id=started["run_id"],
    )
    return {
        **started,
        "precheck": quote, "message": "单视角重做任务已受理，可刷新恢复进度",
    }

@router.post("/projects/{project_id}/scenes/{scene_name}/refs/{scene_reference_id}/views/{view_role}/regenerate/cancel")
async def cancel_scene_view_regeneration(
    project_id: str, scene_name: str, scene_reference_id: str, view_role: str,
):
    _project_or_404(project_id)
    task_key = f"{scene_reference_id}:{view_role}"
    stopped = await task_registry.cancel_and_wait("scene_view_redo", task_key)
    return {"stopped": stopped, "task_id": f"scene_view_redo:{task_key}", "old_asset_preserved": True}

@router.post("/projects/{project_id}/scenes/{scene_name}/candidates/{artifact_id}/adopt")
async def adopt_scene_candidate_route(
    project_id: str, scene_name: str, artifact_id: str, body: dict | None = None,
):
    """手动采纳场景候选图为主图。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route(
        "scene.adopt_candidate",
        {
            "project_id": project_id,
            "scene_name": scene_name,
            "artifact_id": artifact_id,
            "reason": (body or {}).get("reason") or "",
        },
    )
    if routed is not None:
        return routed
    _project_or_404(project_id)
    from app.scenes import adopt_scene_candidate
    try:
        return await adopt_scene_candidate(
            project_id,
            scene_name,
            artifact_id,
            reason=str((body or {}).get("reason") or ""),
            decided_by=current_actor_name(),
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc) or "候选不存在") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc

@router.post("/projects/{project_id}/scenes/{scene_name}/refs/{scene_reference_id}/rollback")
async def rollback_scene_reference(
    project_id: str, scene_name: str, scene_reference_id: str, body: dict | None = None,
):
    """将历史通过包复制为当前包；同一事务更新视角、证据和审计原因。"""
    _project_or_404(project_id)
    conn = get_conn()
    target = conn.execute(
        "SELECT * FROM scene_references WHERE id=? AND project_id=? AND scene_name=?",
        (scene_reference_id, project_id, scene_name),
    ).fetchone()
    if not target:
        raise HTTPException(404, "场景历史版本不存在")
    # Score-only：回滚只要求目标包存在且必需视角齐全，不复跑 QA 硬门禁（PRD QA-SO #21）。
    from app.multiview import SCENE_REQUIRED_VIEWS, list_scene_views, missing_required_views
    views = list_scene_views(target["id"], conn=conn)
    if missing_required_views(views, SCENE_REQUIRED_VIEWS):
        raise HTTPException(409, "历史包缺少必需视角文件，不能回滚为当前版本")
    current = conn.execute(
        "SELECT * FROM scene_references WHERE project_id=? AND scene_name=? AND ep_end IS NULL "
        "ORDER BY ep_start DESC LIMIT 1", (project_id, scene_name),
    ).fetchone()
    if not current:
        raise HTTPException(409, "当前场景版本不存在")
    if current["id"] == target["id"]:
        return {"rolled_back": True, "idempotent_replay": True, "scene_reference_id": current["id"]}
    reason = str(_as_body_dict(body).get("reason") or "回滚到历史通过场景包").strip()
    # 覆盖当前行前先复制完整当前包到新的负数历史槽，确保回滚也可反向回滚。
    from app.multiview import clone_scene_views
    minimum = conn.execute(
        "SELECT MIN(ep_start) AS value FROM scene_references "
        "WHERE project_id=? AND scene_name=? AND ep_start<=0",
        (project_id, scene_name),
    ).fetchone()
    history_start = int(minimum["value"] if minimum and minimum["value"] is not None else 0) - 1
    prior_history_id = new_id("scene")
    columns = [
        "id", "project_id", "scene_name", "ep_start", "ep_end", "scene_canonical", "prompt",
        "image_path", "qa_json", "base_scene_id", "bible_version", "artifact_id", "pack_status",
        "group_qa_json", "state_canonical", "input_fingerprint", "change_json", "created_at",
    ]
    available = {item[1] for item in conn.execute("PRAGMA table_info(scene_references)").fetchall()}
    columns = [column for column in columns if column in available]
    values = {column: current[column] if column in current.keys() else None for column in columns}
    values.update({
        "id": prior_history_id, "ep_start": history_start, "ep_end": 0,
        "base_scene_id": current["id"], "created_at": now(),
    })
    conn.execute(
        f"INSERT INTO scene_references({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
        tuple(values[column] for column in columns),
    )
    clone_scene_views(conn, source_scene_id=current["id"], dest_scene_id=prior_history_id)
    fields = (
        "scene_canonical", "prompt", "image_path", "qa_json", "bible_version", "artifact_id",
        "pack_status", "group_qa_json", "state_canonical", "input_fingerprint",
    )
    change = _parse_json_value(target["change_json"], {}) if "change_json" in target.keys() else {}
    if not isinstance(change, dict):
        change = {}
    change.update({
        "rollback_from": prior_history_id, "rollback_source": target["id"],
        "reason": reason, "rolled_back_at": now(),
    })
    assignments = ",".join(f"{field}=?" for field in fields)
    values = [target[field] if field in target.keys() else None for field in fields]
    conn.execute(
        f"UPDATE scene_references SET {assignments},change_json=? WHERE id=?",
        (*values, json.dumps(change, ensure_ascii=False), current["id"]),
    )
    conn.execute("DELETE FROM scene_reference_views WHERE scene_reference_id=?", (current["id"],))
    target_views = conn.execute(
        "SELECT * FROM scene_reference_views WHERE scene_reference_id=?", (target["id"],),
    ).fetchall()
    for view in target_views:
        conn.execute(
            "INSERT INTO scene_reference_views(id,scene_reference_id,view_role,camera_axis,image_path,prompt,"
            "qa_json,artifact_id,base_view_id,status,selected,input_fingerprint,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (new_id("sview"), current["id"], view["view_role"], view["camera_axis"], view["image_path"],
             view["prompt"], view["qa_json"], view["artifact_id"], view["id"], view["status"],
             view["selected"], view["input_fingerprint"], now()),
        )
    if target["artifact_id"]:
        conn.execute(
            "INSERT INTO gate_decisions(id,artifact_id,gate_key,decision,decided_by,reason,created_at) VALUES(?,?,?,?,?,?,?)",
            (new_id("gate"), target["artifact_id"], "scene_reference_rollback", "rollback", "scene_editor", reason, now()),
        )
    conn.commit()
    return {"rolled_back": True, "scene_reference_id": current["id"], "source_scene_reference_id": target["id"], "reason": reason}
