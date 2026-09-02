"""整项目批量视频补齐主入口（对外路由 + 编排主体）。

从 app/domain/video_ops.py 按原样搬移；依赖 completion_contract/completion_core/project_queue_core/project_queue_run。
单个函数 _complete_project_videos_core 493 行，是跨集队列判据与预算收口的唯一权威顺序，不做拆分（移动未拆分）。
"""
from __future__ import annotations

import json

from app import (
    errors,
    task_registry,
)
from app.db import get_conn
from app.domain.common import router
from fastapi import HTTPException

from .completion_contract import _resume_prepared_complete_episode_operation
from .completion_core import _complete_episode_core
from .project_queue_run import _run_project_video_completion_queue


@router.post("/projects/{project_id}/video-completion")
async def complete_project_videos(project_id: str, body: dict | None = None):
    """跨集批量补齐：在全局预算内按集顺序启动 Supervisor。"""
    from app.capabilities.dispatch import ui_route
    payload = {"project_id": project_id, **(body or {})}
    routed = await ui_route("video.complete_project", payload)
    if routed is not None:
        return routed
    return await _complete_project_videos_core(project_id, body or {})

async def _complete_project_videos_core(project_id: str, body: dict) -> dict:
    """全局预算编排：按 episode_no 顺序分配 per-episode cap，串行启动未覆盖集。"""
    from app.domain.review_wall import _review_validate_authorization_number
    from app.orchestration.engine import WorkflowRecorder, fingerprint
    from app.video_command_operations import (
        bind_video_command_operation,
        claim_video_command_operation,
        finish_video_command_operation,
        read_video_command_operation_binding,
    )

    conn = get_conn()
    project = conn.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project:
        raise HTTPException(404, "项目不存在")
    operation_fingerprint = str(body.get("operation_request_fingerprint") or "")
    operation_claim_token = str(body.get("operation_claim_token") or "")
    operation_command = str(body.get("operation_command") or "")
    operation_idempotency_key = str(body.get("idempotency_key") or "").strip()
    has_operation_receipt = bool(
        operation_fingerprint
        and operation_claim_token
        and operation_command
        and operation_idempotency_key
    )

    operation_binding = (
        read_video_command_operation_binding(
            command=operation_command,
            idempotency_key=operation_idempotency_key,
            request_fingerprint=operation_fingerprint,
        )
        if has_operation_receipt
        else {}
    )
    frozen = operation_binding.get("project_plan")
    if not isinstance(frozen, dict):
        active_queue = conn.execute(
            """SELECT id FROM workflow_runs
               WHERE workflow_type='project_video_completion_queue'
                 AND scope_type='project' AND scope_id=?
                 AND recovered_by_run_id IS NULL
                 AND status IN (
                   'CREATED','RUNNING','WAITING_RETRY','WAITING_HUMAN',
                   'WAITING_AUTHORIZATION','PAUSED_EXTERNAL'
                 )
               ORDER BY updated_at DESC LIMIT 1""",
            (project_id,),
        ).fetchone()
        if task_registry.active("video_completion_project", project_id) or active_queue:
            raise HTTPException(409, {
                "code": "PROJECT_VIDEO_COMPLETION_ALREADY_ACTIVE",
                "message": "项目补齐队列已在运行或等待恢复，请查看现有进度",
                "run_id": active_queue["id"] if active_queue else None,
                "action": "view_progress",
            })

        wall_cap = float(_review_validate_authorization_number(
            body.get("wall_clock_cap_s", 4 * 3600), field="wall_clock_cap_s", minimum=60, maximum=604800, allow_none=False,
        ))
        allow_fallback = bool(body.get("allow_fallback_adopt", True))
        allow_edit = bool(body.get("allow_storyboard_edit", False))
        episode_ids = body.get("episode_ids")
        project_idempotency_key = operation_idempotency_key or None

        rows = conn.execute(
            """SELECT id, episode_no, status, storyboard_artifact_id FROM episodes
               WHERE project_id=? ORDER BY episode_no""",
            (project_id,),
        ).fetchall()
        if episode_ids:
            wanted = set(episode_ids)
            rows = [r for r in rows if r["id"] in wanted]
        eligible = [
            r for r in rows
            if r["status"] in {"confirmed", "generating", "done"}
        ]
        if not eligible:
            raise HTTPException(409, "没有可补齐的已确认剧集")

        # 金额不再构成跳集判断（会员分档时长制）：每个可补齐集直接 queued，
        # 不再按全局/单集金额上限截断——见 CLAUDE.md「Retiring Features」与
        # 本次「成本预算拦截体系退场」。
        plan = []
        from app.video_supervisor import rebuild_coverage_ledger
        for r in eligible:
            if task_registry.active("video_completion", r["id"]):
                plan.append({
                    "episode_id": r["id"], "episode_no": r["episode_no"],
                    "status": "already_running",
                })
                continue
            try:
                ledger = rebuild_coverage_ledger(r["id"])
                if ledger.covered_within_quota():
                    plan.append({
                        "episode_id": r["id"], "episode_no": r["episode_no"],
                        "status": "already_covered",
                    })
                    continue
            except Exception:  # noqa: BLE001
                pass
            plan.append({
                "episode_id": r["id"], "episode_no": r["episode_no"],
                "status": "queued",
            })

        frozen = {
            "project_id": project_id,
            "wall_clock_cap_s": wall_cap,
            "allow_fallback_adopt": allow_fallback,
            "allow_storyboard_edit": allow_edit,
            "idempotency_key": project_idempotency_key,
            "eligible_episode_ids": [str(row["id"]) for row in eligible],
            "plan": json.loads(json.dumps(plan, ensure_ascii=False)),
        }
        if has_operation_receipt:
            bind_video_command_operation(
                command=operation_command,
                idempotency_key=operation_idempotency_key,
                request_fingerprint=operation_fingerprint,
                claim_token=operation_claim_token,
                binding={"phase": "project_plan_frozen", "project_plan": frozen},
                conn=conn,
                merge=True,
            )
            conn.commit()
            operation_binding = {**operation_binding, "project_plan": frozen}
    else:
        if str(frozen.get("project_id") or "") != project_id:
            raise RuntimeError("项目补齐 receipt 绑定的项目已漂移")
        wall_cap = float(frozen["wall_clock_cap_s"])
        allow_fallback = bool(frozen["allow_fallback_adopt"])
        allow_edit = bool(frozen["allow_storyboard_edit"])
        project_idempotency_key = str(frozen.get("idempotency_key") or "") or None
        plan = json.loads(json.dumps(frozen["plan"], ensure_ascii=False))

    started = []
    queue = [p for p in plan if p["status"] == "queued"]
    rest: list[dict] = []
    project_queue_run_id = None

    async def _run_one(item: dict) -> dict:
        child_body = {
            "mode": "fresh",
            "wall_clock_cap_s": wall_cap,
            "allow_fallback_adopt": allow_fallback,
            "allow_storyboard_edit": allow_edit,
            "idempotency_key": (
                f"{project_idempotency_key}:episode:{item['episode_id']}"
                if project_idempotency_key
                else None
            ),
        }
        child_owner = None
        result = None
        finish_child = False
        if has_operation_receipt:
            child_fingerprint = fingerprint(
                "video.complete_project.child",
                project_id,
                item["episode_id"],
                child_body,
            )
            child_owner, result = claim_video_command_operation(
                command="video.complete_episode",
                idempotency_key=str(child_body["idempotency_key"] or ""),
                request_fingerprint=child_fingerprint,
                scope_type="episode",
                scope_id=item["episode_id"],
            )
            if result is None or result.get("_resume_prepared") is True:
                child_body.update({
                    "operation_request_fingerprint": child_fingerprint,
                    "operation_claim_token": child_owner,
                    "operation_command": "video.complete_episode",
                })
            if result is not None and result.get("_resume_prepared") is True:
                result = _resume_prepared_complete_episode_operation(
                    item["episode_id"], child_body, result,
                )
                finish_child = True
        if result is None:
            result = await _complete_episode_core(item["episode_id"], child_body)
            finish_child = has_operation_receipt
        if finish_child:
            finish_video_command_operation(
                command="video.complete_episode",
                idempotency_key=str(child_body["idempotency_key"] or ""),
                request_fingerprint=child_fingerprint,
                claim_token=str(child_owner or ""),
                result=result,
            )
        item["status"] = "started"
        item["run_id"] = result.get("run_id")
        item["completion_grant_id"] = result.get("completion_grant_id")
        item["_operation_result"] = dict(result)
        return item

    if queue:
        bound_first = operation_binding.get("first_episode")
        if isinstance(bound_first, dict) and isinstance(bound_first.get("result"), dict):
            first = queue[0]
            if str(bound_first.get("episode_id") or "") != str(first["episode_id"]):
                raise RuntimeError("项目补齐 receipt 绑定的首集已漂移")
            first["status"] = "started"
            first["run_id"] = bound_first["result"].get("run_id")
            first["completion_grant_id"] = bound_first["result"].get(
                "completion_grant_id"
            )
        else:
            first = await _run_one(queue[0])
            if has_operation_receipt and first.get("status") == "started":
                first_result = dict(first.pop("_operation_result"))
                bind_video_command_operation(
                    command=operation_command,
                    idempotency_key=operation_idempotency_key,
                    request_fingerprint=operation_fingerprint,
                    claim_token=operation_claim_token,
                    binding={
                        "phase": "project_first_episode_started",
                        "first_episode": {
                            "episode_id": first["episode_id"],
                            "run_id": first.get("run_id"),
                            "completion_grant_id": first.get("completion_grant_id"),
                            "result": first_result,
                        },
                    },
                    conn=conn,
                    merge=True,
                )
                conn.commit()
                operation_binding["first_episode"] = {
                    "episode_id": first["episode_id"],
                    "result": first_result,
                }
            else:
                first.pop("_operation_result", None)
        started.append(first)
        rest = queue[1:]
        if rest:
            queue_state = {
                "wall_clock_cap_s": wall_cap,
                "allow_fallback_adopt": allow_fallback,
                "allow_storyboard_edit": allow_edit,
                "idempotency_key": project_idempotency_key,
                "plan": plan,
            }
            if has_operation_receipt:
                queue_state["operation_receipt"] = {
                    "command": operation_command,
                    "operation_key": f"{operation_command}:{operation_idempotency_key}",
                    "request_fingerprint": operation_fingerprint,
                }
            recorder = None
            chain_coro = None
            try:
                queue_fingerprint = fingerprint(project_id, queue_state)
                bound_queue = operation_binding.get("project_queue")
                if isinstance(bound_queue, dict) and bound_queue.get("run_id"):
                    project_queue_run_id = str(bound_queue["run_id"])
                    queue_row = conn.execute(
                        "SELECT id,status FROM workflow_runs WHERE id=?",
                        (project_queue_run_id,),
                    ).fetchone()
                    if queue_row is None:
                        raise RuntimeError("项目补齐 receipt 绑定的队列运行已丢失")
                    recorder = WorkflowRecorder(project_queue_run_id)
                else:
                    queue_row = None
                    if has_operation_receipt:
                        queue_candidates = conn.execute(
                            """SELECT id,status,config_snapshot_json FROM workflow_runs
                               WHERE workflow_type='project_video_completion_queue'
                                 AND scope_type='project' AND scope_id=?
                                 AND input_fingerprint=?
                                 AND status='CREATED' AND recovered_by_run_id IS NULL
                               ORDER BY updated_at DESC""",
                            (project_id, queue_fingerprint),
                        ).fetchall()
                        expected_receipt = queue_state["operation_receipt"]
                        for candidate in queue_candidates:
                            try:
                                snapshot = json.loads(
                                    candidate["config_snapshot_json"] or "{}"
                                )
                            except json.JSONDecodeError:
                                continue
                            if (
                                snapshot.get("queue_state", {}).get("operation_receipt")
                                == expected_receipt
                            ):
                                queue_row = candidate
                                break
                    if queue_row is not None:
                        project_queue_run_id = str(queue_row["id"])
                        recorder = WorkflowRecorder(project_queue_run_id)
                    else:
                        recorder = WorkflowRecorder.create(
                            workflow_type="project_video_completion_queue",
                            scope_type="project",
                            scope_id=project_id,
                            input_fingerprint=queue_fingerprint,
                            requested_by="user",
                            trigger_type="manual",
                            policy_snapshot={"serial": True},
                            config_snapshot={"queue_state": queue_state},
                        )
                        project_queue_run_id = recorder.run_id
                        # create() is the authority for this just-created run.
                        # Do not require a second connection to observe it before
                        # registering the in-memory task.
                        queue_row = {
                            "id": project_queue_run_id,
                            "status": "CREATED",
                        }
                    if has_operation_receipt:
                        bind_video_command_operation(
                            command=operation_command,
                            idempotency_key=operation_idempotency_key,
                            request_fingerprint=operation_fingerprint,
                            claim_token=operation_claim_token,
                            binding={
                                "phase": "project_queue_created",
                                "project_queue": {
                                    "run_id": project_queue_run_id,
                                    "phase": "created",
                                },
                            },
                            conn=conn,
                            merge=True,
                        )
                        conn.commit()
                        operation_binding["project_queue"] = {
                            "run_id": project_queue_run_id,
                            "phase": "created",
                        }
                queue_status = str(queue_row["status"] if queue_row else "")
                if (
                    queue_status == "CREATED"
                    and not task_registry.active("video_completion_project", project_id)
                ):
                    chain_coro = _run_project_video_completion_queue(
                        project_id, queue_state, recorder,
                    )
                    task_registry.spawn(
                        "video_completion_project", project_id, chain_coro, project_id=project_id,
                    )
                    if has_operation_receipt:
                        bind_video_command_operation(
                            command=operation_command,
                            idempotency_key=operation_idempotency_key,
                            request_fingerprint=operation_fingerprint,
                            claim_token=operation_claim_token,
                            binding={
                                "phase": "project_queue_submitted",
                                "project_queue": {
                                    "run_id": project_queue_run_id,
                                    "phase": "submitted",
                                },
                            },
                            conn=conn,
                            merge=True,
                        )
                        conn.commit()
            except Exception as exc:
                if chain_coro is not None:
                    chain_coro.close()
                if recorder is not None:
                    try:
                        recorder.cancel("项目补齐队列未能启动", conn=None)
                    except Exception:  # noqa: BLE001
                        pass
                for item in rest:
                    item["status"] = "failed_to_schedule"
                    item["error"] = "项目级排队任务未能启动，可重新提交项目补齐；已启动集不受影响"
                errors.record_and_format(
                    exc,
                    action="video_completion_project_spawn",
                    context={
                        "project_id": project_id,
                        "already_started_episode_ids": [item["episode_id"] for item in started],
                        "pending_episode_ids": [item["episode_id"] for item in rest],
                    },
                )

    project_result = {
        "status": "accepted",
        "project_id": project_id,
        "plan": plan,
        "started": started,
        "project_queue_active": bool(rest) and all(
            item.get("status") != "failed_to_schedule" for item in rest
        ),
        "project_queue_run_id": project_queue_run_id,
        "project_queue_poll_url": (
            f"/api/runs/{project_queue_run_id}" if project_queue_run_id else None
        ),
        "retryable_schedule_failures": [
            item["episode_id"] for item in plan if item.get("status") == "failed_to_schedule"
        ],
    }
    if has_operation_receipt:
        bind_video_command_operation(
            command=operation_command,
            idempotency_key=operation_idempotency_key,
            request_fingerprint=operation_fingerprint,
            claim_token=operation_claim_token,
            binding={
                "operation_complete": True,
                "project_queue_run_id": project_queue_run_id,
                "started": started,
                "result": project_result,
            },
            conn=conn,
            merge=True,
        )
        conn.commit()
    return project_result
