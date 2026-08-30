"""常驻 worker 协程主循环（拆分自 ``run_job.py``）。

``_worker_loop`` 是 ``ensure_workers()``（``.worker_lifecycle``）拉起的长生命周
期 asyncio 任务本体：不断领取一个 job（``_claim_job_without_blocking_loop``，
``.authority``）、跑 ``_run_job``、处理中断（``_release_interrupted_worker_job``）。
``_run_job`` 定义在 ``.run_job``（该模块名字本身就是这个函数），但
``.run_job`` 顶层需要 ``_maybe_auto_qa``/``_video_mode_input_roles_valid``（本文
件）供自己内部调用——若本文件在顶层 ``from .run_job import _run_job`` 会与
``.run_job`` 反向成环，因此对 ``_run_job`` 的引用推迟到 ``_worker_loop`` 函数体
内部按需导入（做法与 ``.enqueue`` 需要 ``.run_job._enqueue_for_current_status``
的既有惰性导入一致，见 ``app/media_exec/__init__.py`` 模块 docstring）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from app import errors
from app.db import get_conn, now
from app.orchestration import media_scheduler
from app.orchestration.media_runs import mark_media_job_state

from .authority import _claim_job_without_blocking_loop
from .common import _queue
from .job_state import _set_job


def _video_mode_input_roles_valid(meta: dict[str, Any]) -> bool:
    """供应商本次调用实际消费的输入角色是否与已发布的视频模式在结构上吻合。

    这不是内容质检——不判断画面像不像、动作对不对，只核对
    first_frame_used/last_frame_used/reference_image_used/reference_video_used
    这几个"调用发起时就记录下来的事实标记"是否符合 actual_mode 声明的输入合同。
    原属已下线的 ``evaluate_video_mode_qa``（该函数还混了一份基于 VLM 打分的
    ``semantic_success``/``boundary_*_match``，随 VLM 质检一并下线且从未真正
    产生过可信数据——`video_mode_qa_results` 表因此整表删除，见 db 迁移）；
    这里只保留其中这一条纯结构判据，因为下面的调用方确实拿它当硬门禁用。
    """
    mode = str(meta.get("actual_mode") or meta.get("mode") or "")
    if mode == "REFERENCE_IMAGE_MODE":
        return bool(
            not meta.get("first_frame_used")
            and not meta.get("last_frame_used")
            and not meta.get("reference_video_used")
        )
    if mode == "FIRST_LAST_FRAME_MODE":
        return bool(
            meta.get("first_frame_used")
            and meta.get("last_frame_used")
            and not meta.get("reference_image_used")
            and not meta.get("reference_video_used")
        )
    if mode == "FIRST_FRAME_MODE":
        return bool(
            meta.get("first_frame_used")
            and not meta.get("last_frame_used")
            and not meta.get("reference_image_used")
            and not meta.get("reference_video_used")
        )
    if mode == "VIDEO_INPUT_MODE":
        return bool(
            meta.get("reference_video_used")
            and not meta.get("reference_image_used")
            and not meta.get("first_frame_used")
            and not meta.get("last_frame_used")
        )
    return False


async def _maybe_auto_qa(
    job,
    version_id: str,
    video_path: str,
    *,
    allow_autonomous_retake: bool = True,
) -> bool:
    """VLM 视觉质检已整体下线：候选是否可采用只看技术校验
    （app.evidence.media.validate_video_file：文件是否存在、容器格式、时长），
    不再调用模型评分/评语，不再产生模型调用延迟与费用。保留函数签名与调用点，
    避免调用方大改；不再读取 auto_qa 设置。"""
    del job, version_id, video_path, allow_autonomous_retake
    return True


async def _wait_for_worker_job(
    work_queue: asyncio.Queue[str],
    retire_event: asyncio.Event,
) -> str | None:
    """Wake an idle worker for either work or retirement without cancelling it."""
    if retire_event.is_set():
        return None
    job_waiter = asyncio.create_task(work_queue.get())
    retire_waiter = asyncio.create_task(retire_event.wait())
    delivered = False
    try:
        done, _ = await asyncio.wait(
            (job_waiter, retire_waiter),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if job_waiter in done and not retire_event.is_set():
            delivered = True
            return job_waiter.result()
        return None
    finally:
        for waiter in (job_waiter, retire_waiter):
            if not waiter.done():
                waiter.cancel()
        await asyncio.gather(job_waiter, retire_waiter, return_exceptions=True)
        if not delivered and not job_waiter.cancelled():
            try:
                job_id = job_waiter.result()
            except (asyncio.CancelledError, Exception):
                pass
            else:
                # Retirement won after queue.get() consumed an item. Put the
                # durable job back and balance unfinished_tasks.
                work_queue.put_nowait(job_id)
                work_queue.task_done()


def _release_interrupted_worker_job(job_id: str, owner: str) -> bool:
    """Release a shutdown-cancelled claim into a restart-safe durable state."""
    conn = get_conn()
    try:
        if conn.in_transaction:
            conn.rollback()
        row = conn.execute(
            """SELECT j.status,j.lease_owner,j.provider_non_cancellable,
                      j.provider_create_state,j.run_id,j.step_run_id,
                      v.provider_task_id
                 FROM jobs j
                 LEFT JOIN shot_versions v ON v.id=j.version_id
                WHERE j.id=?""",
            (job_id,),
        ).fetchone()
        if not row or row["status"] != "running" or row["lease_owner"] != owner:
            return False
        provider_may_exist = bool(row["provider_task_id"]) or (
            bool(row["provider_non_cancellable"])
            and row["provider_create_state"] in {"submitting", "accepted", "unknown"}
        )
        recoverable_status = "waiting_provider" if provider_may_exist else "queued"
        message = "媒体服务停机，任务已释放并等待恢复"
        stamp = now()
        changed = conn.execute(
            """UPDATE jobs
                  SET status=?,error=?,next_retry_at=?,
                      lease_owner=NULL,lease_expires_at=NULL,updated_at=?
                WHERE id=? AND status='running' AND lease_owner=?""",
            (recoverable_status, message, stamp, stamp, job_id, owner),
        )
        if changed.rowcount != 1:
            conn.rollback()
            return False
        conn.execute(
            """UPDATE budget_reservations
                  SET status='reserved'
                WHERE job_id=? AND status='running'""",
            (job_id,),
        )
        conn.commit()
        mark_media_job_state(
            row["run_id"], row["step_run_id"], "queued", message,
        )
        return True
    except Exception as exc:  # noqa: BLE001 shutdown cleanup remains best-effort
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        errors.log_error(
            exc,
            action="media_worker_shutdown_release",
            context={"job_id": job_id, "worker": owner},
        )
        return False


async def _worker_loop(
    name: str,
    queue: asyncio.Queue[str] | None = None,
    retire_event: asyncio.Event | None = None,
) -> None:
    work_queue = queue or _queue
    retirement = retire_event or asyncio.Event()
    while not retirement.is_set():
        job_id = await _wait_for_worker_job(work_queue, retirement)
        if job_id is None:
            return
        claimed = False
        try:
            claim = await _claim_job_without_blocking_loop(
                job_id,
                name,
                lease_seconds=180.0,
            )
            if claim:
                claimed = True
                row = get_conn().execute(
                    "SELECT run_id, step_run_id FROM jobs WHERE id=?", (job_id,)
                ).fetchone()
                if row:
                    mark_media_job_state(row["run_id"], row["step_run_id"], "running")

                from .run_job import _run_job

                await _run_job(job_id, lease_owner=name)
        except asyncio.CancelledError:
            if claimed:
                _release_interrupted_worker_job(job_id, name)
            raise
        except Exception as exc:  # noqa: BLE001 worker 永不死亡，但错误必须落库
            public = errors.record_and_format(exc, action="worker_loop", context={"job_id": job_id})
            try:
                if _set_job(job_id, "failed", public, lease_owner=name):
                    media_scheduler.settle_budget(job_id, 0.0, success=False)
            except Exception as persist_exc:  # noqa: BLE001 worker 本身不能因落库失败退出
                try:
                    get_conn().rollback()
                except Exception:  # noqa: BLE001 best-effort lock release
                    pass
                errors.log_error(
                    persist_exc,
                    action="worker_loop_error_persist",
                    context={"job_id": job_id, "worker": name},
                )
        finally:
            work_queue.task_done()

__all__ = [name for name in globals() if not name.startswith("__")]
