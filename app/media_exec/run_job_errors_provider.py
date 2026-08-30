"""``_run_job``'s ``except (ProviderError, Exception)`` handler (see
``run_job.py``'s module docstring for the split map) -- split out of
``run_job_errors.py`` because this branch alone, plus its helpers, already
filled that file past the 500-line file cap.

Does not call ``_assert_review_dependency_fence_async`` / ``ensure_source_
excerpt_in_prompt`` / ``_provider_wait_policy`` / ``_load_shot_model`` (see
``run_job_errors.py``'s module docstring for why that matters). Moved
verbatim out of the pre-split single function otherwise.
"""
from __future__ import annotations

from typing import Any

from app import errors, hiagent
from app.db import now
from app.orchestration import media_scheduler
from app.orchestration.media_runs import mark_media_job_state

from .enqueue import _row_value, reconcile_episode_generation_status
from .checkpoints import _commit_provider_terminal_failure
from .job_state import _set_job, _set_version, _video_model_rejection_guidance
from .retry_scheduling import _defer_provider_poll, _schedule_job_retry


async def _handle_provider_or_generic_error(
    conn: Any,
    job: Any,
    job_id: str,
    owner: str,
    version: Any,
    meta: dict[str, Any],
    task_id: str | None,
    provider_operation_id: str,
    exc: BaseException,
) -> None:  # noqa: BLE001 失败要响：原文进日志，前端给码+分类
    """Handle every other ``ProviderError``/generic exception raised while running the job."""
    from app.hiagent import ProviderError

    exc = _normalize_provider_exception(exc)
    if not media_scheduler.renew_lease(job_id, owner, lease_seconds=180.0):
        return
    record = errors.log_error(
        exc, action="shot_video_generate",
        context={"shot_id": job["shot_id"], "version_id": version["id"], "job_id": job_id})
    provider_failure = exc.failure if isinstance(exc, ProviderError) else None
    reason_code, public = _derive_provider_error_message(meta, exc, record, provider_failure)
    poll_state = conn.execute(
        "SELECT provider_poll_required FROM jobs WHERE id=?",
        (job_id,),
    ).fetchone()
    provider_poll_pending = bool(
        task_id
        and poll_state is not None
        and poll_state["provider_poll_required"]
    )
    if _defer_or_retry_provider_error(job_id, owner, version, task_id, exc, provider_failure, provider_poll_pending):
        return
    external_terminal = bool(
        provider_failure
        and provider_failure.disposition
        is hiagent.ProviderFailureDisposition.EXTERNAL_TERMINAL
    )
    if provider_poll_pending and external_terminal:
        await _finalize_provider_terminal_failure(
            conn, job, job_id, owner, version, provider_operation_id, public, reason_code, provider_failure,
        )
        return
    _finalize_provider_final_status(
        conn, job, job_id, owner, version, meta, exc, public, reason_code,
        provider_failure, provider_poll_pending, external_terminal,
    )


def _normalize_provider_exception(exc: BaseException) -> BaseException:
    """Reclassify a raw structured-provider-rejection as a ``ProviderError``."""
    from app.harness.model_gateway import StructuredProviderRejection
    from app.hiagent import ProviderError

    if isinstance(exc, StructuredProviderRejection):
        return ProviderError(
            "AI 视频提示词服务拒绝当前内容",
            raw=str(exc),
            failure=hiagent.ProviderFailure.model_rejection(
                hiagent.ProviderFailureKind.PROMPT_PROVIDER_REJECTED
            ),
            delivery_state="not_sent",
            replay_safe=True,
        )
    return exc


def _derive_provider_error_message(
    meta: dict[str, Any], exc: BaseException, record: Any, provider_failure: Any,
) -> tuple[str | None, str]:
    """Derive the reason code and the public-facing message for this failure.

    Returns ``(reason_code, public)``.
    """
    from app.hiagent import ProviderError

    guidance = (
        _video_model_rejection_guidance(meta, exc)
        if isinstance(exc, ProviderError)
        else None
    )
    reason_code = (
        guidance[0]
        if guidance
        else (provider_failure.reason_code if provider_failure else None)
    )
    public = (
        f"{guidance[1]}（{guidance[0]} · {record.error_id}）"
        if guidance else record.public
    )
    return reason_code, public


def _defer_or_retry_provider_error(
    job_id: str,
    owner: str,
    version: Any,
    task_id: str | None,
    exc: BaseException,
    provider_failure: Any,
    provider_poll_pending: bool,
) -> bool:
    """Defer a still-pending poll, or auto-reschedule a retryable structured failure.

    Returns ``True`` when this fully handled the error (caller must return).
    """
    from app.hiagent import ProviderError

    if (
        provider_poll_pending
        and (
            not isinstance(exc, ProviderError)
            or bool(provider_failure and provider_failure.retryable)
        )
        and _defer_provider_poll(
            job_id,
            task_id,
            lease_owner=owner,
        )
    ):
        return True
    # 仅结构化 retryable 故障自动重排；重试耗尽后转人工，不改变原始类别。
    if isinstance(exc, ProviderError) and _schedule_job_retry(
        job_id, exc, lease_owner=owner
    ):
        _set_version(version["id"], status="queued")
        return True
    return False


async def _finalize_provider_terminal_failure(
    conn: Any,
    job: Any,
    job_id: str,
    owner: str,
    version: Any,
    provider_operation_id: str,
    public: str,
    reason_code: str | None,
    provider_failure: Any,
) -> None:
    """Commit an externally-terminal provider failure while a poll is still pending."""
    await _commit_provider_terminal_failure(
        conn,
        job_id=job_id,
        version_id=version["id"],
        owner=owner,
        operation_id=provider_operation_id,
        message=public,
        reason_code=reason_code or provider_failure.reason_code,
        failure=provider_failure,
    )
    if (
        provider_failure.kind
        != hiagent.ProviderFailureKind.PROMPT_PROVIDER_REJECTED.value
    ):
        conn.execute(
            """UPDATE jobs SET provider_create_state='model_rejected'
                WHERE id=? AND status='failed'""",
            (job_id,),
        )
        conn.commit()
    mark_media_job_state(
        _row_value(job, "run_id"),
        _row_value(job, "step_run_id"),
        "failed",
        public,
    )
    reconcile_episode_generation_status(job["episode_id"])


def _finalize_provider_final_status(
    conn: Any,
    job: Any,
    job_id: str,
    owner: str,
    version: Any,
    meta: dict[str, Any],
    exc: BaseException,
    public: str,
    reason_code: str | None,
    provider_failure: Any,
    provider_poll_pending: bool,
    external_terminal: bool,
) -> None:
    """Set the job/version to its final failed/waiting_human status and settle the budget."""
    final_status = (
        "waiting_human"
        if provider_failure and not external_terminal
        else "failed"
    )
    if not _set_job(job_id, final_status, public, lease_owner=owner):
        return
    _persist_provider_failure_details(
        conn, job_id, version, meta, exc, reason_code, public, provider_failure, final_status, external_terminal,
    )
    conn.commit()
    _set_version(version["id"], status=final_status, error=public)
    if provider_poll_pending:
        conn.execute(
            """UPDATE budget_reservations
                  SET status='reserved'
                WHERE job_id=? AND status='running'""",
            (job_id,),
        )
        conn.commit()
    else:
        media_scheduler.settle_budget(job_id, 0.0, success=False)
    reconcile_episode_generation_status(job["episode_id"])


def _persist_provider_failure_details(
    conn: Any,
    job_id: str,
    version: Any,
    meta: dict[str, Any],
    exc: BaseException,
    reason_code: str | None,
    public: str,
    provider_failure: Any,
    final_status: str,
    external_terminal: bool,
) -> None:
    """Persist the failed attempt/plan status and the structured provider-failure fields."""
    conn.execute(
        """UPDATE video_generation_attempts
              SET status='failed',error=?,updated_at=?
            WHERE version_id=? AND status='provider_running'""",
        (str(exc)[:2000], now(), version["id"]),
    )
    if meta.get("shot_plan_id"):
        conn.execute(
            """UPDATE shot_video_generation_plans
                  SET status='failed',updated_at=? WHERE id=?""",
            (now(), str(meta["shot_plan_id"])),
        )
    if not provider_failure:
        return
    persisted_disposition = (
        hiagent.ProviderFailureDisposition.EXTERNAL_TERMINAL
        if external_terminal
        else hiagent.ProviderFailureDisposition.MANUAL_REVIEW
    )
    conn.execute(
        """UPDATE jobs
              SET reason_code=?,reason_text=?,
                  provider_failure_category=?,
                  provider_failure_kind=?,
                  provider_failure_disposition=?,
                  provider_failure_retryable=?
            WHERE id=? AND status=?""",
        (
            reason_code,
            public,
            provider_failure.category.value,
            provider_failure.kind,
            persisted_disposition.value,
            int(provider_failure.retryable),
            job_id,
            final_status,
        ),
    )
    if (
        external_terminal
        and provider_failure.kind
        != hiagent.ProviderFailureKind.PROMPT_PROVIDER_REJECTED.value
    ):
        conn.execute(
            """UPDATE jobs SET provider_create_state='model_rejected'
                WHERE id=? AND status='failed'""",
            (job_id,),
        )
