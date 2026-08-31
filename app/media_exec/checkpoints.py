"""媒体作业结果落库：供应商受理、终态成功/失败的事务提交（拆分自 ``run_job.py``）。

九个函数分两层：``_in_transaction`` 结尾的三个（``_commit_provider_acceptance``/
``_commit_video_result_checkpoint``/``_commit_provider_terminal_failure`` 各自
的 ``_in_transaction`` 版本）只做纯 SQL 写入、不管连接是谁的、不提交；外层的
异步版本负责判定当前调用能否安全占用事件循环线程
（``_authority_checks_can_use_worker_thread``，来自 ``.authority``），能则直接调
用，不能则经 ``_run_in_memory_write_transaction``/``run_write_transaction`` 转交
专用写事务连接。``_commit_provider_create_unresolved`` 单独处理「供应商可能已
接单但没有可持久化任务句柄」的中间态。``_await_with_job_lease_heartbeat`` 是这
一层之外的共用工具：包一个协程、在其运行期间定期续租，续租失败即转译为
``LeaseLost``（``.common``）。
"""

from __future__ import annotations

import asyncio

from app.db import now, run_write_transaction
from app.orchestration import media_scheduler

from .authority import _authority_checks_can_use_worker_thread
from .common import LeaseLost


def _run_in_memory_write_transaction(conn, operation):
    """Keep private in-memory DB tests on their only usable connection."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = operation(conn)
        conn.commit()
        return result
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


def _commit_provider_create_unresolved(
    conn,
    *,
    job_id: str,
    version_id: str,
    owner: str,
    message: str,
) -> bool:
    """Atomically fence and persist the unresolved-create human handoff."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        changed = conn.execute(
            """UPDATE jobs
                  SET status='waiting_human',error=?,
                      reason_code='VIDEO_PROVIDER_CREATE_UNRESOLVED',
                      reason_text=?,lease_owner=NULL,lease_expires_at=NULL,
                      next_retry_at=NULL,video_slot_active=0,updated_at=?
                WHERE id=? AND status='running' AND lease_owner=?
                  AND cancellation_requested=0""",
            (message, message, now(), job_id, owner),
        )
        if changed.rowcount != 1:
            conn.rollback()
            return False
        changed = conn.execute(
            """UPDATE shot_versions
                  SET status='waiting_human',error=?,video_slot_active=0
                WHERE id=?""",
            (message, version_id),
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
        return True
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


def _commit_provider_acceptance_in_transaction(
    conn,
    *,
    job_id: str,
    version_id: str,
    owner: str,
    operation_id: str,
    task_id: str,
    submitted_at: float | None = None,
) -> None:
    """Write paid provider acceptance while the caller owns the transaction."""
    from app.completion_grant import ensure_video_budget_authority_tables

    ensure_video_budget_authority_tables(conn)
    stamp = now()
    accepted_at = float(submitted_at or stamp)
    claimed = conn.execute(
        """UPDATE jobs
              SET provider_operation_id=?,provider_create_state='accepted',
                  provider_non_cancellable=1,provider_submitted_at=?,
                  provider_poll_required=1,
                  provider_failure_category=NULL,provider_failure_kind=NULL,
                  provider_failure_disposition=NULL,provider_failure_retryable=NULL,
                  updated_at=?
            WHERE id=? AND status='running' AND lease_owner=?
              AND cancellation_requested=0""",
        (operation_id, accepted_at, stamp, job_id, owner),
    )
    if claimed.rowcount != 1:
        raise LeaseLost(f"provider acceptance lost lease: {job_id} / {owner}")
    scope = conn.execute(
        """SELECT j.project_id,j.episode_id,j.shot_id,j.version_id,
                  COALESCE(br.amount_cny,j.reserved_cost_cny,0) AS amount_cny
             FROM jobs j
             LEFT JOIN budget_reservations br ON br.job_id=j.id
            WHERE j.id=? AND j.version_id=?""",
        (job_id, version_id),
    ).fetchone()
    if (
        scope is not None
        and scope["project_id"]
        and scope["episode_id"]
        and scope["shot_id"]
        and scope["version_id"]
    ):
        conn.execute(
            """INSERT OR IGNORE INTO provider_video_budget_claims(
                   operation_id,project_id,episode_id,shot_id,job_id,version_id,
                   origin_episode_id,origin_shot_id,origin_job_id,origin_version_id,
                   amount_cny,status,created_at,updated_at,accepted_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,'accepted',?,?,?)""",
            (
                operation_id,
                scope["project_id"],
                scope["episode_id"],
                scope["shot_id"],
                job_id,
                version_id,
                scope["episode_id"],
                scope["shot_id"],
                job_id,
                version_id,
                max(0.0, float(scope["amount_cny"] or 0)),
                accepted_at,
                stamp,
                accepted_at,
            ),
        )
    conn.execute(
        """UPDATE shot_versions
              SET provider_task_id=?,status='running',error=NULL
            WHERE id=?""",
        (task_id, version_id),
    )
    conn.execute(
        """UPDATE provider_video_budget_claims
              SET status='accepted',updated_at=?,
                  accepted_at=COALESCE(accepted_at,?)
            WHERE operation_id=? AND job_id=?
              AND status NOT IN ('released','settled','closed_liability')""",
        (stamp, stamp, operation_id, job_id),
    )


async def _commit_provider_acceptance(
    conn,
    *,
    job_id: str,
    version_id: str,
    owner: str,
    operation_id: str,
    task_id: str,
    submitted_at: float | None = None,
) -> None:
    """Commit paid provider acceptance off-loop when the DB is reopenable."""
    def operation(write_conn) -> None:
        _commit_provider_acceptance_in_transaction(
            write_conn,
            job_id=job_id,
            version_id=version_id,
            owner=owner,
            operation_id=operation_id,
            task_id=task_id,
            submitted_at=submitted_at,
        )

    if _authority_checks_can_use_worker_thread(conn):
        await run_write_transaction(operation)
        return
    _run_in_memory_write_transaction(conn, operation)


def _commit_video_result_checkpoint_in_transaction(
    conn,
    *,
    job_id: str,
    version_id: str,
    owner: str,
    operation_id: str,
    video_path: str,
    last_frame_url: str | None,
    cost_cny: float,
    latency_s: float,
    image_inputs: str,
) -> bool:
    """Persist one provider success and return whether it may become a candidate."""
    stamp = now()
    job = conn.execute(
        """SELECT video_slot_active,provider_result_adoptable
             FROM jobs
            WHERE id=? AND status='running' AND lease_owner=?
              AND cancellation_requested=0 AND provider_poll_required=1""",
        (job_id, owner),
    ).fetchone()
    if job is None:
        raise LeaseLost(f"provider result lost lease: {job_id} / {owner}")
    adoptable = bool(
        job["video_slot_active"] and job["provider_result_adoptable"]
    )
    result_status = "succeeded" if adoptable else "quarantined"
    terminal_message = (
        None
        if adoptable
        else "历史供应商任务已完成；结果与费用已记录，素材保持隔离且不可采用"
    )
    claimed = conn.execute(
        """UPDATE jobs
              SET status=CASE WHEN ? THEN status ELSE 'succeeded' END,
                  error=?,provider_poll_required=0,
                  video_slot_active=CASE WHEN ? THEN video_slot_active ELSE 0 END,
                  lease_owner=CASE WHEN ? THEN lease_owner ELSE NULL END,
                  lease_expires_at=CASE WHEN ? THEN lease_expires_at ELSE NULL END,
                  next_retry_at=NULL,reserved_cost_cny=CASE WHEN ? THEN reserved_cost_cny ELSE 0 END,
                  updated_at=?
            WHERE id=? AND status='running' AND lease_owner=?
              AND cancellation_requested=0 AND provider_poll_required=1""",
        (
            int(adoptable),
            terminal_message,
            int(adoptable),
            int(adoptable),
            int(adoptable),
            int(adoptable),
            stamp,
            job_id,
            owner,
        ),
    )
    if claimed.rowcount != 1:
        raise LeaseLost(f"provider result lost lease: {job_id} / {owner}")
    version = conn.execute(
        """UPDATE shot_versions
              SET status=?,error=?,video_path=?,
                  last_frame_url=?,cost_cny=?,latency_s=?,image_inputs=?,
                  video_slot_active=CASE WHEN ? THEN video_slot_active ELSE 0 END
            WHERE id=?""",
        (
            result_status,
            terminal_message,
            video_path,
            last_frame_url,
            cost_cny,
            latency_s,
            image_inputs,
            int(adoptable),
            version_id,
        ),
    )
    if version.rowcount != 1:
        raise LeaseLost(f"provider result version fenced: {job_id} / {owner}")
    conn.execute(
        """UPDATE provider_video_budget_claims
              SET status='settled',updated_at=?,settled_at=?
            WHERE operation_id=? AND job_id=?""",
        (stamp, stamp, operation_id, job_id),
    )
    if not adoptable:
        conn.execute(
            """UPDATE budget_reservations
                  SET status='settled',settled_at=?,actual_cost_cny=?
                WHERE job_id=?""",
            (stamp, max(0.0, float(cost_cny)), job_id),
        )
    return adoptable


async def _commit_video_result_checkpoint(
    conn,
    *,
    job_id: str,
    version_id: str,
    owner: str,
    operation_id: str,
    video_path: str,
    last_frame_url: str | None,
    cost_cny: float,
    latency_s: float,
    image_inputs: str,
) -> bool:
    """Commit the provider result checkpoint off-loop when possible."""
    def operation(write_conn) -> bool:
        return _commit_video_result_checkpoint_in_transaction(
            write_conn,
            job_id=job_id,
            version_id=version_id,
            owner=owner,
            operation_id=operation_id,
            video_path=video_path,
            last_frame_url=last_frame_url,
            cost_cny=cost_cny,
            latency_s=latency_s,
            image_inputs=image_inputs,
        )

    if _authority_checks_can_use_worker_thread(conn):
        return await run_write_transaction(operation)
    return _run_in_memory_write_transaction(conn, operation)


def _commit_provider_terminal_failure_in_transaction(
    conn,
    *,
    job_id: str,
    version_id: str,
    owner: str,
    operation_id: str,
    message: str,
    reason_code: str,
    failure,
) -> float:
    """Settle an accepted provider task that reached an explicit failed terminal."""
    stamp = now()
    job = conn.execute(
        """SELECT 1 FROM jobs
            WHERE id=? AND status='running' AND lease_owner=?
              AND cancellation_requested=0 AND provider_poll_required=1""",
        (job_id, owner),
    ).fetchone()
    if job is None:
        raise LeaseLost(f"provider failure lost lease: {job_id} / {owner}")
    # 产物信号：该版本累计有没有产出（非"这次尝试"），用权威 version_id 单查。
    produced = conn.execute("SELECT video_path FROM shot_versions WHERE id=?", (version_id,)).fetchone()
    claim = conn.execute(
        "SELECT amount_cny FROM provider_video_budget_claims WHERE operation_id=? AND job_id=?",
        (operation_id, job_id),
    ).fetchone()
    reservation = conn.execute(
        "SELECT amount_cny FROM budget_reservations WHERE job_id=?", (job_id,),
    ).fetchone()
    # 零产出（从未下载）不按预留估算全价结算。
    settled_cost = max(0.0, float(
        claim["amount_cny"] if claim is not None
        else (reservation["amount_cny"] if reservation is not None else 0)
    )) if produced["video_path"] else 0.0
    changed = conn.execute(
        """UPDATE jobs
              SET status='failed',error=?,reason_code=?,reason_text=?,
                  provider_failure_category=?,provider_failure_kind=?,
                  provider_failure_disposition=?,provider_failure_retryable=?,
                  provider_poll_required=0,provider_result_adoptable=0,
                  video_slot_active=0,reserved_cost_cny=0,
                  lease_owner=NULL,lease_expires_at=NULL,next_retry_at=NULL,
                  updated_at=?
            WHERE id=? AND status='running' AND lease_owner=?
              AND cancellation_requested=0 AND provider_poll_required=1""",
        (
            message,
            reason_code,
            message,
            failure.category.value,
            failure.kind,
            failure.disposition.value,
            int(failure.retryable),
            stamp,
            job_id,
            owner,
        ),
    )
    if changed.rowcount != 1:
        raise LeaseLost(f"provider failure lost lease: {job_id} / {owner}")
    conn.execute(
        """UPDATE shot_versions
              SET status='failed',error=?,cost_cny=?,video_slot_active=0
            WHERE id=?""",
        (message, settled_cost, version_id),
    )
    conn.execute(
        """UPDATE provider_video_budget_claims
              SET status='settled',updated_at=?,settled_at=?
            WHERE operation_id=? AND job_id=?""",
        (stamp, stamp, operation_id, job_id),
    )
    conn.execute(
        """UPDATE budget_reservations
              SET status='settled',settled_at=?,actual_cost_cny=?
            WHERE job_id=?""",
        (stamp, settled_cost, job_id),
    )
    return settled_cost


async def _commit_provider_terminal_failure(
    conn,
    *,
    job_id: str,
    version_id: str,
    owner: str,
    operation_id: str,
    message: str,
    reason_code: str,
    failure,
) -> float:
    def operation(write_conn) -> float:
        return _commit_provider_terminal_failure_in_transaction(
            write_conn,
            job_id=job_id,
            version_id=version_id,
            owner=owner,
            operation_id=operation_id,
            message=message,
            reason_code=reason_code,
            failure=failure,
        )

    if _authority_checks_can_use_worker_thread(conn):
        return await run_write_transaction(operation)
    return _run_in_memory_write_transaction(conn, operation)


async def _await_with_job_lease_heartbeat(
    awaitable,
    *,
    job_id: str,
    owner: str,
    lease_seconds: float = 180.0,
    heartbeat_interval_s: float = 30.0,
):
    """Keep ownership alive while one provider-heavy stage is awaiting I/O.

    Reference preparation may contain several image and VLM calls, each of
    which can legitimately outlive the normal job lease.  Run renewals on a
    worker thread so the heartbeat uses its own thread-local SQLite connection
    instead of committing work on the media coroutine's connection.

    If ownership has genuinely moved, cancel the in-flight stage immediately:
    a fenced worker must not publish a stale checkpoint or delete files that a
    newer attempt has already adopted.
    """
    interval = max(0.01, float(heartbeat_interval_s))
    # A newly-created thread-local SQLite connection can briefly collide with
    # another checkpoint commit.  That is not evidence that ownership moved.
    # Keep enough renewal opportunities inside the lease window while still
    # cancelling before an actually unrenewable worker can be swept/reclaimed.
    max_missed_renewals = max(2, int(float(lease_seconds) // interval) - 1)

    async def _renew_once() -> bool | None:
        try:
            return await asyncio.to_thread(
                media_scheduler.renew_lease,
                job_id,
                owner,
                lease_seconds=lease_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # None means "temporarily unconfirmed".  Explicit False remains
            # the authoritative CAS signal that another owner took over.
            return None

    async def _heartbeat() -> bool:
        missed_renewals = 0
        while True:
            await asyncio.sleep(interval)
            owned = await _renew_once()
            if owned is None:
                missed_renewals += 1
                if missed_renewals < max_missed_renewals:
                    continue
                return False
            if not owned:
                return False
            missed_renewals = 0

    operation_task = asyncio.create_task(awaitable)
    heartbeat_task = asyncio.create_task(_heartbeat())
    try:
        done, _pending = await asyncio.wait(
            {operation_task, heartbeat_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if heartbeat_task in done:
            owned = heartbeat_task.result()
            if not owned:
                operation_task.cancel()
                await asyncio.gather(operation_task, return_exceptions=True)
                raise LeaseLost(f"job lease lost during provider stage: {job_id} / {owner}")

        result = await operation_task
        owned = await _renew_once()
        if owned is False:
            raise LeaseLost(f"job lease lost after provider stage: {job_id} / {owner}")
        # A transient DB error here is retried by the synchronous lease fence
        # immediately following this stage in _run_job.
        return result
    finally:
        for task in (operation_task, heartbeat_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(operation_task, heartbeat_task, return_exceptions=True)

__all__ = [name for name in globals() if not name.startswith("__")]
