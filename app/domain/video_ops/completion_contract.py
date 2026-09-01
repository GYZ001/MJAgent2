"""成片完成态对用户可见的契约投影、查询与修复路由。

从 app/domain/video_ops.py 按原样搬移；依赖 completion_core 与 generate。
"""
from __future__ import annotations

from app import task_registry
from app.db import (
    get_conn,
    now,
)
from app.domain.common import (
    _episode_or_404,
    router,
)
from fastapi import HTTPException
from typing import Any

from .completion_core import (
    _ensure_video_episode_columns,
    _recorded_video_completion_task,
)
from .completion_freshness import (
    TERMINAL_SUCCESS_PHASES,
    terminal_success_contract,
)
from .generate import _shot_by_no


def _resume_prepared_complete_episode_operation(
    episode_id: str,
    body: dict,
    prepared: dict,
) -> dict:
    """Register the exact durable run left between commit and task spawn.

    A hard crash can happen after the run/episode owner and domain receipt are
    committed but before the in-memory task is registered.  Recovery may only
    resume those exact persisted IDs; it must never re-plan a second run.
    """
    from app.orchestration.engine import WorkflowRecorder
    from app.video_command_operations import bind_video_command_operation

    result = prepared.get("result")
    spawn = prepared.get("spawn")
    if not isinstance(result, dict) or not isinstance(spawn, dict):
        raise HTTPException(409, "视频补齐恢复回执绑定不完整")
    run_id = str(prepared.get("run_id") or result.get("run_id") or "")
    if (
        not run_id
        or str(spawn.get("episode_id") or "") != episode_id
        or str(result.get("run_id") or "") != run_id
    ):
        raise HTTPException(409, "视频补齐恢复回执范围不匹配")

    conn = get_conn()
    run = conn.execute(
        """SELECT id,status,workflow_type,scope_type,scope_id
             FROM workflow_runs WHERE id=?""",
        (run_id,),
    ).fetchone()
    episode = conn.execute(
        "SELECT project_id,active_video_run_id FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    if (
        run is None
        or episode is None
        or run["workflow_type"] != "episode_video_completion"
        or run["scope_type"] != "episode"
        or run["scope_id"] != episode_id
        or str(episode["project_id"]) != str(spawn.get("project_id") or "")
    ):
        raise HTTPException(409, "视频补齐恢复回执的持久化运行无效")

    status = str(run["status"] or "").upper()
    if status == "CREATED":
        if episode["active_video_run_id"] != run_id:
            raise HTTPException(409, "视频补齐恢复启动权已变更")
        if not task_registry.active("video_completion", episode_id):
            recorder = WorkflowRecorder(run_id)
            coro = _recorded_video_completion_task(
                episode_id,
                recorder,
                resume=bool(spawn.get("resume")),
                grant_id=spawn.get("grant_id"),
                budget_cap_cny=spawn.get("budget_cap_cny"),
                wall_clock_cap_s=spawn.get("wall_clock_cap_s"),
                allow_fallback_adopt=bool(spawn.get("allow_fallback_adopt", True)),
                max_fallback_shots=spawn.get("max_fallback_shots"),
                allow_storyboard_edit=bool(spawn.get("allow_storyboard_edit")),
            )
            try:
                task_registry.spawn(
                    "video_completion",
                    episode_id,
                    coro,
                    project_id=episode["project_id"],
                )
            except Exception:
                coro.close()
                raise
    elif status not in {
        "RUNNING", "WAITING_RETRY", "WAITING_HUMAN", "WAITING_AUTHORIZATION",
        "PAUSED_BUDGET", "PAUSED_EXTERNAL", "SUCCEEDED", "PARTIAL",
    }:
        raise HTTPException(409, "视频补齐原运行已失效，不得伪造恢复成功")

    bind_video_command_operation(
        command=str(body.get("operation_command") or ""),
        idempotency_key=str(body.get("idempotency_key") or ""),
        request_fingerprint=str(body.get("operation_request_fingerprint") or ""),
        claim_token=str(body.get("operation_claim_token") or ""),
        binding={
            "operation_complete": True,
            "phase": "spawn_registered",
            "result": result,
        },
        conn=conn,
        merge=True,
    )
    conn.commit()
    return dict(result)

# FAILED_CLOSED 的 checkpoint.outcome -> 用户可读细节。只收录目前已知会走到
# FAILED_CLOSED 的 code：来源见 app/video_supervisor/run_loop.py
# ``_resolve_grant_failure``（VideoPlanGenerationError，默认 code
# VIDEO_PLAN_INVALID，ERR-20260831-dd05c7）。resilience.py 的
# ``_mark_failed_closed``（韧性外壳自身也失败时的最后防线）把 outcome 设成字
# 面量 "FAILED_CLOSED"，本身不携带比 phase 更多的信息，故不收录——命中不到
# 表里的 code 一律走下面的通用说法，不得编造更具体的原因。
_FAILED_CLOSED_OUTCOME_DETAIL = {
    "VIDEO_PLAN_INVALID": "模型生成的整集视频计划未通过校验",
}

def _failed_closed_contract(cp: Any, projection: dict[str, Any], *, base: str, action) -> dict[str, Any]:
    outcome = getattr(cp, "outcome", None) or projection.get("outcome")
    detail = _FAILED_CLOSED_OUTCOME_DETAIL.get(outcome)
    suffix = f"（{detail}）" if detail else ""
    return {
        "user_state": "failed",
        "message": f"全片补齐已安全停止{suffix}，现有采用版不会丢失",
        "next_actions": [
            action("repair_preview", "查看修复预演", "GET", f"{base}/repair-preview"),
            action("start_completion", "重新授权并补齐", "POST", base, True),
        ],
    }

def _video_completion_user_contract(
    episode_id: str,
    cp: Any,
    projection: dict[str, Any],
    *,
    running: bool,
) -> dict[str, Any]:
    phase = str(getattr(cp, "phase", "") or projection.get("phase") or "")
    checkpoint_run_id = getattr(cp, "run_id", None) or projection.get("run_id")
    run_id = (
        projection.get("active_video_run_id") or checkpoint_run_id
        if running else checkpoint_run_id
    )
    grant_id = getattr(cp, "grant_id", None) or projection.get("grant_id")
    base = f"/api/episodes/{episode_id}/video-completion"

    def action(action_id, label, method, endpoint, confirm=False):
        return {
            "id": action_id,
            "label": label,
            "method": method,
            "endpoint": endpoint,
            "requires_confirm": confirm,
        }

    def running_contract():
        actions = [action("view_progress", "查看进度", "GET", base)]
        if run_id:
            actions.append(action("pause", "暂停", "POST", f"/api/runs/{run_id}/pause", True))
        return {
            "user_state": "running",
            "message": "正在补齐全片视频，已完成内容会持续保留",
            "next_actions": actions,
        }

    active_run_id = projection.get("active_video_run_id")
    if running and (cp is None or (active_run_id and active_run_id != checkpoint_run_id)):
        return running_contract()
    active_run_status = str(projection.get("active_run_status") or "")
    if (
        not running
        and active_run_id
        and active_run_id != checkpoint_run_id
        and (
            str(active_run_id).startswith("starting:")
            or active_run_status in {
                "CREATED", "RUNNING", "WAITING_RETRY", "WAITING_HUMAN",
                "WAITING_AUTHORIZATION", "PAUSED_BUDGET", "PAUSED_EXTERNAL",
            }
        )
    ):
        actions = [action("repair_preview", "查看恢复状态", "GET", f"{base}/repair-preview")]
        if not str(active_run_id).startswith("starting:"):
            actions.insert(0, action("open_run", "查看运行", "GET", f"/api/runs/{active_run_id}"))
        return {
            "user_state": "recovering",
            "message": "检测到未完成的补齐运行，系统正在恢复持久化进度",
            "next_actions": actions,
        }
    if cp is None:
        return {
            "user_state": "not_started",
            "message": "尚未开始全片视频补齐",
            "next_actions": [
                action("start_completion", "开始全片补齐", "POST", base, True),
            ],
        }
    if phase in TERMINAL_SUCCESS_PHASES:
        # 终态只在它仍描述当前这版分镜时才算数，判据见 completion_freshness。
        return terminal_success_contract(
            episode_id, phase, projection, base=base, action=action,
        )
    if phase == "PARTIAL_NO_USABLE_CANDIDATE":
        missing = len(projection.get("missing_shots") or [])
        suffix = f"，仍有 {missing} 个镜头未能生成技术可播版" if missing else ""
        return {
            "user_state": "failed",
            "message": f"确定性缺镜兜底遇到技术故障{suffix}，已保留所有现有结果",
            "next_actions": [
                action("repair_preview", "查看修复预演", "GET", f"{base}/repair-preview"),
                action("start_completion", "重新授权并补齐", "POST", base, True),
            ],
        }
    if phase == "FAILED_CLOSED":
        return _failed_closed_contract(cp, projection, base=base, action=action)
    if phase == "CANCELLED":
        return {
            "user_state": "cancelled",
            "message": "全片补齐已取消，已完成内容仍然保留",
            "next_actions": [
                action("start_completion", "重新开始", "POST", base, True),
            ],
        }
    if running:
        return running_contract()
    if phase in {"WAITING_AUTHORIZATION", "PAUSED_BUDGET"}:
        return {
            "user_state": "waiting_authorization",
            "message": "任务已暂停，需要追加授权或预算后继续",
            "next_actions": [{
                **action("authorize_continue", "追加授权并继续", "POST", base, True),
                "required_fields": ["add_budget_cny", "add_wall_clock_s"],
                "required_rule": "至少填写一项",
                "request_body": {
                    "mode": "resume",
                    "completion_grant_id": grant_id,
                },
            }, action("start_completion", "重新授权并开始", "POST", base, True)],
        }
    if phase in {"WAITING_HUMAN", "PAUSED_EXTERNAL", "WAITING_RETRY"}:
        # WAITING_HUMAN/PAUSED_EXTERNAL 不总是"供应商拒绝"——同样的 phase 也
        # 用于用户手动暂停、资产待补齐等可以直接"继续补齐"恢复的场景。只有
        # cp.last_plan 明确记录了这次暂停由不可修复的供应商终态判决触发时，
        # 才需要换文案：对已经被供应商拒绝的镜头，"继续补齐"不会重试它，
        # 展示这个按钮就是给假出路（CLAUDE.md「User-Facing Behavior」）。
        provider_rejection_codes = {
            "VIDEO_PROVIDER_MODEL_REJECTED",
            "VIDEO_PROMPT_PROVIDER_REJECTED",
            "VIDEO_PROVIDER_TECHNICAL_FAILURE",
        }
        last_plan = cp.last_plan if isinstance(getattr(cp, "last_plan", None), dict) else {}
        rejected_codes = set(last_plan.get("issue_codes") or []) & provider_rejection_codes
        provider_rejected = (
            last_plan.get("strategy") == "handoff_human" and bool(rejected_codes)
        )
        if provider_rejected:
            shot_no = last_plan.get("shot_no")
            shot_row = _shot_by_no(episode_id, shot_no) if shot_no is not None else None
            shot_id = shot_row["id"] if shot_row else None
            shot_label = f"第 {shot_no} 镜" if shot_no is not None else "该镜"
            actions = [action("repair_preview", "查看恢复预演", "GET", f"{base}/repair-preview")]
            if shot_id:
                actions.insert(0, action(
                    "edit_shot_prompt", "编辑本镜提示词重抽", "POST",
                    f"/api/shots/{shot_id}/generate",
                ))
            return {
                "user_state": "waiting_human",
                "message": (
                    f"{shot_label}已被供应商明确拒绝，继续补齐不会重试此镜；"
                    "请编辑该镜提示词后重抽，或切换视频供应商。"
                ),
                "next_actions": actions,
            }
        actions = [action("repair_preview", "查看恢复预演", "GET", f"{base}/repair-preview")]
        if run_id:
            actions.insert(0, action("resume", "继续补齐", "POST", f"/api/runs/{run_id}/resume"))
        return {
            "user_state": "waiting_human",
            "message": "任务已暂停，检查评审意见或恢复条件后可继续",
            "next_actions": actions,
        }
    return {
        "user_state": "interrupted",
        "message": "补齐任务当前未运行，请先查看恢复预演再继续",
        "next_actions": [
            action("repair_preview", "查看恢复预演", "GET", f"{base}/repair-preview"),
            action("start_completion", "重新授权并补齐", "POST", base, True),
        ],
    }

@router.get("/episodes/{episode_id}/video-completion")
def get_video_completion(episode_id: str):
    """只读：最新 checkpoint 公开投影 + 覆盖台账。"""
    _episode_or_404(episode_id)
    _ensure_video_episode_columns()
    from app.video_supervisor import (
        load_latest_checkpoint,
        public_checkpoint_projection,
        rebuild_coverage_ledger,
    )
    from app.video_cost_model import predict_episode_completion_cost
    cp = load_latest_checkpoint(episode_id)
    try:
        ledger = rebuild_coverage_ledger(episode_id, cp=cp)
        proj = public_checkpoint_projection(cp) or {}
        proj["ledger"] = {
            "shots_total": ledger.shots_total,
            "grades": ledger.grades,
            "coverage_rate": ledger.coverage_rate,
            "fallback_quota": ledger.fallback_quota,
            "cost_spent": ledger.cost_spent,
            "entries": [e.model_dump(mode="json") for e in ledger.entries],
        }
        adopted_count = sum(1 for entry in ledger.entries if entry.adopted_version_id)
        proj["coverage"] = {
            **(proj.get("coverage") or {}),
            "A": ledger.grades.get("A", 0),
            "B": ledger.grades.get("B", 0),
            "C": ledger.grades.get("C", 0),
            "total": ledger.shots_total,
            "adopted": adopted_count,
            "unadopted": max(0, ledger.shots_total - adopted_count),
            "coverage_rate": ledger.coverage_rate,
            "fallback_quota": ledger.fallback_quota,
        }
        try:
            uncovered_ids = [e.shot_id for e in ledger.entries if not e.adopted_version_id]
            proj["cost_forecast"] = predict_episode_completion_cost(
                episode_id, uncovered_shot_ids=uncovered_ids,
            )
        except Exception:  # noqa: BLE001
            proj["cost_forecast"] = None
    except Exception as exc:  # noqa: BLE001 — 台账失败时仍返回 checkpoint，避免面板整页 500
        proj = public_checkpoint_projection(cp) or {}
        proj["ledger"] = {"shots_total": 0, "grades": {}, "coverage_rate": 0.0, "entries": []}
        proj["cost_forecast"] = None
        proj["ledger_error"] = str(exc)
    conn = get_conn()
    ep = conn.execute(
        "SELECT active_video_run_id, video_completion_mode FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    try:
        proj["active_video_run_id"] = ep["active_video_run_id"] if ep else None
        proj["video_completion_mode"] = ep["video_completion_mode"] if ep else "quick"
    except (KeyError, IndexError, TypeError):
        proj["active_video_run_id"] = None
        proj["video_completion_mode"] = "quick"
    active_run = conn.execute(
        "SELECT status FROM workflow_runs WHERE id=?",
        (proj["active_video_run_id"],),
    ).fetchone() if proj["active_video_run_id"] else None
    proj["active_run_status"] = active_run["status"] if active_run else None
    running = task_registry.active("video_completion", episode_id)
    proj["running"] = running
    proj.update(_video_completion_user_contract(
        episode_id, cp, proj, running=running,
    ))
    return proj

@router.get("/episodes/{episode_id}/video-completion/repair-preview")
def preview_video_completion_repair_route(episode_id: str):
    """只读：预演遗留 Supervisor 的收口动作。"""
    _episode_or_404(episode_id)
    from app.video_supervisor import preview_video_completion_repair
    return preview_video_completion_repair(episode_id)

@router.post("/episodes/{episode_id}/video-completion/repair")
def repair_video_completion_route(episode_id: str, body: dict | None = None):
    """显式确认后收口遗留 run；不会启动任何新视频生成。"""
    from app.completion_grant import get_video_grant
    from app.orchestration.engine import WorkflowRecorder, fingerprint
    from app.video_supervisor import (
        VideoSupervisorCheckpoint,
        _deadline_closeout,
        _mark_failed_closed,
        load_latest_checkpoint,
        preview_video_completion_repair,
        public_checkpoint_projection,
    )

    ep = _episode_or_404(episode_id)
    if not body or body.get("confirm") is not True:
        raise HTTPException(409, "必须先查看 repair-preview，并显式提交 confirm=true")
    if task_registry.active("video_completion", episode_id):
        raise HTTPException(409, "Supervisor 仍在真实运行，不能执行遗留收口")
    preview = preview_video_completion_repair(episode_id)
    cp = load_latest_checkpoint(episode_id) or VideoSupervisorCheckpoint(
        episode_id=episode_id,
        started_at=now(),
    )
    if cp.deadline_at is None and cp.grant_id:
        grant = get_video_grant(cp.grant_id)
        if grant:
            cp.deadline_at = float(grant.deadline_at)
    parent_run_id = ep["active_video_run_id"]
    recorder = WorkflowRecorder.create(
        workflow_type="episode_video_completion",
        scope_type="episode",
        scope_id=episode_id,
        input_fingerprint=fingerprint(
            ep["storyboard_artifact_id"], cp.grant_id, "confirmed_legacy_closeout",
        ),
        requested_by="user",
        trigger_type="repair",
        policy_snapshot={"supervisor": "video_completion", "confirmed_legacy_closeout": True},
        deadline_at=cp.deadline_at or now(),
        parent_run_id=parent_run_id,
    )
    recorder.start()
    conn = get_conn()
    conn.execute(
        """UPDATE episodes
           SET active_video_run_id=?, video_completion_mode='complete', status='generating'
           WHERE id=?""",
        (recorder.run_id, episode_id),
    )
    conn.commit()
    cp.run_id = recorder.run_id
    try:
        result = _deadline_closeout(
            cp,
            run_id=recorder.run_id,
            reason="CONFIRMED_LEGACY_INCIDENT_CLOSEOUT",
        )
        recorder.partial(result.outcome or result.phase, conn=None)
    except Exception as exc:  # noqa: BLE001
        # 必须在 _mark_failed_closed / recorder.fail 之前回滚，且回滚要放在这
        # 个 except 块的第一条语句：_deadline_closeout 内部与本函数共用同一
        # 个 task 缓存连接，它对每个镜头调用
        # app.evidence.media.select_best_video_candidate 采用最佳候选——那
        # 个函数先 UPDATE shots.adopted_version_id 与
        # shot_versions.adoption_reason，再调用
        # invalidate_episode_delivery_authority 写 delivery_packages，最后才
        # 一次性 conn.commit()；这几条语句之间没有中间提交点。如果
        # _deadline_closeout 在这个窗口内抛出异常，这份半途的采用写入就会挂
        # 在 conn 上。_mark_failed_closed 内部会调用 save_checkpoint，
        # 它的写入逻辑是「如果 conn 已经在事务中就不再开新事务，直接复用」
        # （见 app/video_supervisor.py::save_checkpoint），所以它的
        # conn.commit() 会把上面挂起的半途采用一并提交下去；recorder.fail()
        # 的 refresh_cost()/transition_run() 同理。回滚只丢弃这次失败尝试自
        # 己产生的未提交写入，不影响 _deadline_closeout 已经在别处提交过的
        # 状态（例如它自己那次 conn.commit() 已经落盘的镜头）。
        if conn.in_transaction:
            conn.rollback()
        _mark_failed_closed(
            cp,
            run_id=recorder.run_id,
            reason=f"CONFIRMED_REPAIR_FAILED: {type(exc).__name__}: {exc}",
        )
        recorder.fail(exc, conn=None)
        raise HTTPException(500, f"遗留 run 收口失败：{exc}") from exc
    return {
        "status": "closed",
        "run_id": recorder.run_id,
        "preview": preview,
        "result": public_checkpoint_projection(result),
    }
