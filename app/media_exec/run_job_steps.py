"""``_run_job`` 的非核心状态机片段：进入供应商提交/轮询循环前的三处「等待并
重排」守卫，以及循环 ``break`` 之后的结果落库/质检/采纳尾段。

移动未重写，拆自 ``run_job.py``（见其模块 docstring 的完整拆分地图与「为何
核心 while 循环不再往下拆」的说明）。本文件不触碰 ``task_id``/``prompt_text``
在 while 循环内的重新绑定，只处理循环外、状态转移边界清晰的部分。

``evaluate_technical_validation`` 是个例外，需要额外注意：原实现里
``meta = json.loads(version["image_inputs"] or "{}")`` 会重新绑定 ``_run_job``
自己的 ``meta`` 局部变量，而这个变量在技术校验失败且重提次数耗尽时会被后面的
``except (ProviderError, Exception)`` 处理器读到（``_handle_provider_or_generic_
error`` 的入参之一）。拆到独立函数后，函数体内部的重新赋值只是这个函数自己的
局部变量，不会传回调用方——因此本函数把新的 ``meta`` 作为返回值的一部分交回，
调用方必须显式 ``meta = ...`` 重新绑定自己的同名变量，而不是指望副作用穿透
函数边界（CLAUDE.md「``global`` 重绑定的名字必须与它的写入者同模块」的同类
教训：这里不是 ``global``，但同样是「重新绑定必须发生在读取方自己的作用域」）。
"""
from __future__ import annotations

import asyncio
import json
import time

from app.compiler import shot_cost_cny
from app.db import get_conn, now
from app.evidence import media as media_evidence
from app.orchestration import media_scheduler

from .common import _retry_tasks
from .job_state import _paid_video_attempt_count, _set_job


async def defer_job_with_wait(
    conn, job_id: str, owner: str, *, status: str, note: str, wait: float,
    stage, reason_code: str, schedule_retry: bool,
) -> None:
    """把 job 挂回等待态，按需安排延迟重排；调用方在此之后必须 ``return``。"""
    from .retry_scheduling import _requeue_after
    from app.media_pipeline.stage_state import set_pipeline_stage

    set_pipeline_stage(job_id, stage, reason_code=reason_code, reason_text=note, conn=conn)
    conn.execute(
        """UPDATE jobs SET status=?, error=?, next_retry_at=?,
                  lease_owner=NULL, lease_expires_at=NULL, updated_at=?
           WHERE id=? AND lease_owner=?""",
        (status, note, now() + wait, now(), job_id, owner),
    )
    conn.execute(
        "UPDATE budget_reservations SET status='reserved' WHERE job_id=? AND status='running'",
        (job_id,),
    )
    conn.commit()
    if schedule_retry:
        task = asyncio.get_running_loop().create_task(_requeue_after(job_id, wait))
        _retry_tasks.add(task)
        task.add_done_callback(_retry_tasks.discard)


async def download_provider_result(conn, meta: dict, job, ep, shot, version, result, provider_recovery_only: bool) -> str:
    """下载供应商产出视频到本地路径；叙事计划已失效则围栏拒绝。返回本地路径。"""
    from app import hiagent
    from .enqueue import _row_value, _video_path

    meta["provider_video_source_url"] = result["video_url"]
    # Current provider contract advertises a seven-day URL. Keep a
    # conservative six-day reuse window so downstream jobs never race expiry.
    meta["provider_video_source_url_expires_at"] = now() + 6 * 24 * 3600
    dest = _video_path(job["project_id"], ep["episode_no"], shot["shot_no"], version["version_no"])
    try:
        await hiagent.download(result["video_url"], str(dest))
    except hiagent.ProviderError as exc:
        if exc.retryable:
            # 任务已完成但产出连续取不到（对象存储 5xx，hiagent.download 内部已重试 3 次）：
            # 这条任务不会再变，不能每 10 秒复轮再重下（2026-09-05 第 2/23 集 3 镜各刷
            # 6 条/分钟报错近 1 小时）；清掉轮询标记让错误处理器换新任务，受 max_retries 封顶。
            from .job_state import release_provider_poll  # job_state 依赖 enqueue，避免模块级环
            release_provider_poll(conn, job["id"], _row_value(job, "lease_owner"), version_id=version["id"])
        raise
    if not provider_recovery_only and meta.get("shot_plan_id"):
        from app.video_plan import active_plan_is_current
        from .fences import VideoPlanStaleFence

        if not active_plan_is_current(str(meta["shot_plan_id"]), conn=conn):
            raise VideoPlanStaleFence("视频生成完成时计划已失效，候选已隔离")
    return dest


def check_supervisor_ownership_fence(job, job_id: str, provider_recovery_only: bool) -> bool:
    """结果到达时校验所属 Supervisor 是否已收口；已收口则取消任务。

    返回 True 表示已取消，调用方必须 ``return``。
    """
    from .enqueue import _row_value

    supervisor_owner = _row_value(job, "owner_run_id")
    if not supervisor_owner or provider_recovery_only:
        return False
    current_owner = get_conn().execute(
        "SELECT active_video_run_id, video_completion_mode FROM episodes WHERE id=?",
        (job["episode_id"],),
    ).fetchone()
    fenced = (
        not current_owner
        or current_owner["video_completion_mode"] != "complete"
        or current_owner["active_video_run_id"] != supervisor_owner
    )
    if not fenced:
        try:
            from app.video_supervisor import TERMINAL_SUPERVISOR_PHASES, load_latest_checkpoint
            owner_cp = load_latest_checkpoint(job["episode_id"])
            fenced = bool(
                owner_cp
                and (
                    owner_cp.dispatch_fenced_at is not None
                    or owner_cp.phase in TERMINAL_SUPERVISOR_PHASES
                )
            )
        except Exception:  # noqa: BLE001 — active run ownership remains the fallback fence
            pass
    if fenced:
        from app.observability.metrics import inc
        inc(
            "video_supervisor_orphan_provider_result_total",
            episode_id=job["episode_id"], owner_run_id=supervisor_owner,
        )
        media_scheduler.request_cancel(
            job_id, reason="结果到达时所属 Supervisor 已收口；候选已隔离，不参与自动采用",
        )
    return fenced


async def commit_result_checkpoint(conn, job_id, version, owner, provider_operation_id, meta, dest, result, shot, started):
    """计算成本/延迟并提交结果 checkpoint。返回 ``(result_adoptable, cost)``。"""
    from .checkpoints import _commit_video_result_checkpoint

    latency = round(time.time() - started, 1)
    paid_attempts = max(
        1,
        int(meta.get("provider_paid_attempts") or 0),
        _paid_video_attempt_count(conn, version["id"]),
    )
    meta["provider_paid_attempts"] = paid_attempts
    cost = shot_cost_cny(shot["duration_s"]) * paid_attempts
    result_adoptable = await _commit_video_result_checkpoint(
        conn,
        job_id=job_id,
        version_id=version["id"],
        owner=owner,
        operation_id=provider_operation_id,
        video_path=str(dest),
        last_frame_url=result["last_frame_url"],
        cost_cny=cost,
        latency_s=latency,
        image_inputs=json.dumps(meta, ensure_ascii=False),
    )
    return result_adoptable, cost


def record_success_mode_attempt(conn, job, version, meta: dict, task_id) -> None:
    """有已发布视频计划时，记一条成功态的模式尝试。"""
    if not meta.get("shot_plan_id"):
        return
    from app.video_plan import VideoGenerationMode, get_shot_plan, record_mode_attempt

    active_shot_plan = get_shot_plan(job["shot_id"], conn=conn)
    if active_shot_plan and active_shot_plan.shot_plan_id == meta.get("shot_plan_id"):
        record_mode_attempt(
            version_id=version["id"],
            shot_plan=active_shot_plan,
            actual_mode=VideoGenerationMode(meta["actual_mode"]),
            status="succeeded",
            provider_task_id=task_id,
            conn=conn,
        )


async def run_auto_qa(job, version, dest) -> bool:
    """判定完整补齐模式是否由 Supervisor 掌控自动重抽/采用。

    VLM 视觉质检已整体下线，不再有独立的"自动 QA"步骤要跑；``version``/
    ``dest`` 参数保留仅为调用方兼容。返回 ``supervisor_controlled``。
    """
    del version, dest
    supervisor_controlled = False
    try:
        ep_mode = get_conn().execute(
            "SELECT video_completion_mode FROM episodes WHERE id=?", (job["episode_id"],),
        ).fetchone()
        supervisor_controlled = bool(
            ep_mode and ep_mode["video_completion_mode"] == "complete"
        )
    except Exception:  # noqa: BLE001
        pass
    return supervisor_controlled


def evaluate_technical_validation(conn, version, meta: dict) -> tuple[bool, dict, int]:
    """校验技术门槛。返回 ``(passed, meta, resubmits)``。

    ``meta`` 在校验未通过时会被替换为版本行最新的 image_inputs——调用方必须用
    返回值重新绑定自己的 ``meta``（见本模块 docstring）。
    """
    from .worker_loop import _video_mode_input_roles_valid
    from app.hiagent import ProviderError

    technical = json.loads(conn.execute(
        "SELECT technical_validation_json FROM shot_versions WHERE id=?", (version["id"],)
    ).fetchone()["technical_validation_json"] or "{}")
    if meta.get("shot_plan_id") and not _video_mode_input_roles_valid(meta):
        raise ProviderError("视频供应商输入角色与已发布模式计划不一致")
    if technical.get("passed"):
        return True, meta, 0
    # 技术校验失败：resubmits 取新建版本上已有的重提计数（供上限判断）
    resubmits = 0
    try:
        meta = json.loads(version["image_inputs"] or "{}")
        resubmits = int(meta.get("technical_resubmit_count") or 0)
    except Exception:  # noqa: BLE001
        resubmits = 0
    return False, meta, resubmits


def resubmit_after_technical_failure(job, resubmits: int, meta: dict) -> None:
    """技术校验失败但未超重提上限：自动新建重提版本（尽力标记其重提计数）。"""
    from .enqueue import enqueue_shot

    replacement = enqueue_shot(
        job["shot_id"],
        reroll=True,
        after_shot_id=job["after_shot_id"],
        auto_retake_count=resubmits + 1,
        dependency_snapshot=meta.get("review_dependency_snapshot"),
    )
    try:
        new_version_id = replacement.get("version_id")
        new_ver = (
            get_conn().execute(
                "SELECT id,image_inputs FROM shot_versions WHERE id=?",
                (new_version_id,),
            ).fetchone()
            if new_version_id else None
        )
        if new_ver:
            m = json.loads(new_ver["image_inputs"] or "{}")
            if isinstance(m, dict):
                m["technical_resubmit_count"] = resubmits + 1
                get_conn().execute(
                    "UPDATE shot_versions SET image_inputs=? WHERE id=?",
                    (json.dumps(m, ensure_ascii=False), new_ver["id"]),
                )
                get_conn().commit()
    except Exception:  # noqa: BLE001
        pass


async def adopt_and_settle_candidate(conn, job, job_id, owner, version, cost, supervisor_controlled) -> None:
    """非 Supervisor 掌控时采用最佳候选；随后统一结算预算并推进分集状态。"""
    from .authority import _assert_review_dependency_fence_async
    from .enqueue import reconcile_episode_generation_status

    if not supervisor_controlled:
        await _assert_review_dependency_fence_async(
            job, version["id"], "adoption_relation",
        )
        media_evidence.select_best_video_candidate(job["shot_id"])
        adopted = conn.execute(
            "SELECT adopted_version_id FROM shots WHERE id=?",
            (job["shot_id"],),
        ).fetchone()
        if adopted and adopted["adopted_version_id"]:
            from app.video_plan import reconcile_adopted_revision
            reconcile_adopted_revision(
                job["shot_id"], adopted["adopted_version_id"], conn=conn,
            )
    if _set_job(job_id, "succeeded", lease_owner=owner):
        media_scheduler.settle_budget(job_id, cost, success=True)
        reconcile_episode_generation_status(job["episode_id"])
