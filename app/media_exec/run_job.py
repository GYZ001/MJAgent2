from __future__ import annotations

try:
    _queue
except NameError:  # pragma: no cover - used when importing this module directly
    from app.media_exec.common import *


class ReviewDependencyFence(RuntimeError):
    """The upstream/asset snapshot captured at enqueue is no longer current."""


class VideoPlanStaleFence(RuntimeError):
    """The provider result belongs to a superseded or stale video plan."""


class VideoInputRepairRequired(RuntimeError):
    """The planned mode is still valid, but its local input assets need repair."""


class ProviderCreateUnresolved(RuntimeError):
    """The provider may have accepted create, but no durable task handle exists."""


class VideoInflightAdmissionDeferred(RuntimeError):
    """The atomic submit-side inflight claim found no capacity."""


def _release_pre_call_video_claim(
    conn,
    *,
    job_id: str,
    owner: str,
    operation_id: str,
) -> None:
    """Release a slot/budget claim only while provider create is provably unsent."""
    if conn.in_transaction:
        conn.rollback()
    try:
        conn.execute("BEGIN IMMEDIATE")
        released_job = conn.execute(
            """UPDATE jobs
                  SET provider_non_cancellable=0,
                      provider_create_state='not_started',updated_at=?
                WHERE id=? AND provider_create_state='submitting'
                  AND provider_non_cancellable=1
                  AND status='running' AND lease_owner=?
                  AND cancellation_requested=0""",
            (now(), job_id, owner),
        )
        if released_job.rowcount != 1:
            raise LeaseLost(f"video pre-call claim lost ownership: {job_id}")
        released_at = now()
        released_budget = conn.execute(
            """UPDATE provider_video_budget_claims
                  SET status='released',updated_at=?,released_at=?
                WHERE operation_id=? AND job_id=? AND status='reserved'""",
            (released_at, released_at, operation_id, job_id),
        )
        if released_budget.rowcount != 1:
            raise LeaseLost(f"video pre-call budget claim lost ownership: {job_id}")
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def _provider_create_outcome_unknown(exc: ProviderError) -> bool:
    """Fail closed unless the provider response makes replay safety explicit."""
    delivery_state = str(getattr(exc, "delivery_state", "unknown") or "unknown")
    if bool(getattr(exc, "create_not_accepted", False)):
        return False
    return not (
        delivery_state == "not_sent"
        and bool(getattr(exc, "replay_safe", False))
    )


def _assert_provider_create_resolved(job, task_id: str | None) -> None:
    if task_id:
        return
    create_state = str(_row_value(job, "provider_create_state") or "not_started")
    provider_may_have_accepted = bool(
        _row_value(job, "provider_non_cancellable")
        or create_state in {"submitting", "unknown", "accepted"}
    )
    if provider_may_have_accepted:
        operation_id = str(_row_value(job, "provider_operation_id") or "")
        raise ProviderCreateUnresolved(
            "[VIDEO_PROVIDER_CREATE_UNRESOLVED] Seedance create 可能已被供应商接收，"
            f"但本地尚无 task id（operation_id={operation_id or 'missing'}，"
            f"state={create_state}）；已禁止自动重复 create，请先在页面核对供应商任务"
        )


async def _claim_job_without_blocking_loop(
    job_id: str,
    owner: str,
    *,
    lease_seconds: float,
):
    if not _authority_checks_can_use_worker_thread():
        return media_scheduler.claim_job(
            job_id,
            owner,
            lease_seconds=lease_seconds,
        )
    return await run_write_transaction(
        lambda conn: media_scheduler.claim_job(
            job_id,
            owner,
            lease_seconds=lease_seconds,
            conn=conn,
            commit=False,
        )
    )


def _authority_checks_can_use_worker_thread(conn=None) -> bool:
    """A private in-memory SQLite database cannot be reopened in a worker thread."""
    try:
        rows = (conn or get_conn()).execute("PRAGMA database_list").fetchall()
        return any(str(row[2] or "").strip() for row in rows)
    except Exception:
        return False


def _connection_for_heartbeat_operation(conn):
    """Let a child task own its SQLite connection when the DB is reopenable."""
    if _authority_checks_can_use_worker_thread(conn):
        return None
    return conn


def _assert_video_provider_submission_authority(
    conn,
    *,
    job,
    meta: dict[str, Any],
    actual_mode: str,
    write_point: str,
) -> Any | None:
    """Use one fail-closed authority check at every paid submission boundary."""
    shot_plan_id = str(meta.get("shot_plan_id") or "")
    if not shot_plan_id:
        # Compatibility boundary for legacy plan-null jobs. Narrative authority
        # jobs cannot reach this branch because enqueue requires a bound plan.
        return None
    try:
        from app.video_plan import (
            VideoPlanValidationError,
            assert_video_provider_submission_authority,
        )

        selected, _snapshot = assert_video_provider_submission_authority(
            shot_id=str(job["shot_id"]),
            shot_plan_id=shot_plan_id,
            actual_mode=actual_mode,
            expected_capability_snapshot_id=(
                str(meta["capability_snapshot_id"])
                if meta.get("capability_snapshot_id")
                else None
            ),
            conn=conn,
        )
        return selected
    except VideoPlanValidationError as exc:
        raise VideoPlanStaleFence(json.dumps({
            "code": "VIDEO_PROVIDER_SUBMISSION_AUTHORITY_STALE",
            "write_point": write_point,
            "shot_id": str(job["shot_id"]),
            "shot_plan_id": shot_plan_id,
            "issues": exc.issues,
        }, ensure_ascii=False)) from exc


async def _assert_video_provider_submission_authority_async(
    *,
    conn=None,
    job,
    meta: dict[str, Any],
    actual_mode: str,
    write_point: str,
) -> Any | None:
    if not _authority_checks_can_use_worker_thread(conn):
        return _assert_video_provider_submission_authority(
            conn or get_conn(),
            job=job,
            meta=meta,
            actual_mode=actual_mode,
            write_point=write_point,
        )

    def verify() -> Any | None:
        return _assert_video_provider_submission_authority(
            get_conn(),
            job=dict(job),
            meta=meta,
            actual_mode=actual_mode,
            write_point=write_point,
        )

    return await asyncio.to_thread(verify)


def _assert_current_storyboard_completion_authority(
    conn,
    *,
    episode_id: str,
    write_point: str,
) -> None:
    """Re-verify the consumed narrative release certificate at worker time."""
    episode = conn.execute(
        "SELECT * FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    if episode is None:
        raise ReviewDependencyFence(json.dumps({
            "code": "NARRATIVE_STORYBOARD_AUTHORITY_INVALID",
            "write_point": write_point,
            "message": "当前剧集不存在",
        }, ensure_ascii=False))
    raw_screenplay = _row_value(episode, "screenplay_json")
    if not raw_screenplay:
        from app.production.screenplay_authority import (
            episode_requires_immutable_screenplay_authority,
        )

        if not episode_requires_immutable_screenplay_authority(episode, conn=conn):
            return
    try:
        from app.production.screenplay_authority import resolve_downstream_screenplay
        from app.schemas import Storyboard

        screenplay_context = resolve_downstream_screenplay(
            episode_id,
            conn=conn,
        )
    except Exception as exc:  # noqa: BLE001 - paid boundary fails closed
        raise ReviewDependencyFence(json.dumps({
            "code": "NARRATIVE_STORYBOARD_AUTHORITY_INVALID",
            "write_point": write_point,
            "message": f"当前剧本权威链无法验证：{exc}",
        }, ensure_ascii=False)) from exc
    if not screenplay_context.narrative_authority_required:
        return
    try:
        rows = conn.execute(
            "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
            (episode_id,),
        ).fetchall()
        board = Storyboard(
            episode_no=int(_row_value(episode, "episode_no") or 1),
            shots=[_load_shot_model(row) for row in rows],
        )
        from app.production.certificate import (
            verify_current_storyboard_completion_authority,
        )

        verify_current_storyboard_completion_authority(
            episode=episode,
            current_storyboard_content=board.model_dump(mode="json"),
        )
    except Exception as exc:  # noqa: BLE001 - worker/provider boundary fails closed
        raise ReviewDependencyFence(json.dumps({
            "code": "NARRATIVE_STORYBOARD_AUTHORITY_INVALID",
            "write_point": write_point,
            "message": str(exc),
        }, ensure_ascii=False)) from exc


def _assert_review_dependency_fence(job, version_id: str, write_point: str) -> None:
    """Fail closed before a paid run can become a current candidate or adoption.

    Legacy plan-null rows without a snapshot remain readable/finishable for
    compatibility.  A typed narrative plan has no such fallback: its immutable
    review/certificate/projection authority must have been captured at enqueue.
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT shot_id, image_inputs FROM shot_versions WHERE id=?", (version_id,),
    ).fetchone()
    episode_id = _row_value(job, "episode_id")
    if not episode_id and row and row["shot_id"]:
        shot_scope = conn.execute(
            "SELECT episode_id FROM shots WHERE id=?",
            (row["shot_id"],),
        ).fetchone()
        episode_id = shot_scope["episode_id"] if shot_scope else None
    if not episode_id:
        raise ReviewDependencyFence(json.dumps({
            "code": "REVIEW_DEPENDENCY_EPISODE_MISSING",
            "write_point": write_point,
        }, ensure_ascii=False))
    try:
        meta = json.loads(row["image_inputs"] or "{}") if row else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        meta = {}
    captured = meta.get("review_dependency_snapshot") or {}
    expected = captured.get("qualification_version")
    if not expected:
        episode = conn.execute(
            "SELECT * FROM episodes WHERE id=?",
            (episode_id,),
        ).fetchone()
        from app.production.screenplay_authority import (
            episode_requires_immutable_screenplay_authority,
        )

        if episode is not None and episode_requires_immutable_screenplay_authority(
            episode,
            conn=conn,
        ):
            _assert_current_storyboard_completion_authority(
                conn,
                episode_id=str(episode_id),
                write_point=write_point,
            )
            raise ReviewDependencyFence(json.dumps({
                "code": "NARRATIVE_REVIEW_DEPENDENCY_SNAPSHOT_MISSING",
                "write_point": write_point,
                "message": "叙事权威项目的媒体任务缺少发布依赖快照",
            }, ensure_ascii=False))
        return
    try:
        from app.api import _review_upstream_snapshot
        current = _review_upstream_snapshot(episode_id)
    except Exception as exc:  # qualification service errors are fail-closed
        raise ReviewDependencyFence(
            f"依赖资格复核失败（{write_point}）：{exc}"
        ) from exc
    upstream_keys = (
        "published_screenplay_artifact_id", "confirmed_storyboard_artifact_id",
        "screenplay_revision", "storyboard_revision",
    )
    upstream_equal = all(current.get(key) == captured.get(key) for key in upstream_keys)
    expected_assets = captured.get("asset_inputs") or []
    current_assets = current.get("asset_inputs") or []
    # The current shot's gallery is produced/updated by this very job.  It is
    # an output of the run, not an upstream dependency: comparing it here
    # makes a successful reference build invalidate its own captured token.
    # Other shots remain fenced, so unrelated asset edits still stop a stale
    # provider result from becoming a candidate.
    target_shot_id = row["shot_id"] if row else None

    def asset_contract(items):
        return {
            json.dumps(
                {key: value for key, value in item.items() if key not in {"version_id"}},
                ensure_ascii=False, sort_keys=True,
            )
            for item in items
            if item.get("shot_id") != target_shot_id
        }
    # Modern narrative jobs bind exact asset revisions in the validated video
    # plan and recheck them again at provider submission. Shot galleries are
    # downstream outputs: parallel sibling jobs naturally add images and must
    # not invalidate one another's captured qualification snapshot — a sibling
    # shot resolving its own gallery for the first time only ever *adds* an
    # entry, it never touches this shot's own dependencies.
    #
    # This must therefore be a subset check (every asset this job's snapshot
    # depended on is still present, unchanged, right now), not a full-set
    # equality: exact equality also breaks the moment any sibling shot's
    # gallery grows mid-flight, even though nothing this job depends on
    # actually changed. Reproduced on EP1: shots 5/6/7 were technically valid
    # and already downloaded, but got fenced out purely because shot 5/6's
    # own galleries gained entries while an earlier sibling's job was still
    # awaiting its own later checkpoint. A previously-captured entry that
    # disappears or changes (a real edit/removal of a qualified asset) must
    # still fail closed; a snapshot merely gaining unrelated entries must not.
    assets_equal = bool(
        not expected_assets
        or asset_contract(expected_assets) <= asset_contract(current_assets)
    )
    if (
        current.get("eligible_for_production")
        and upstream_equal
        and assets_equal
    ):
        return
    detail = {
        "code": "REVIEW_DEPENDENCY_STALE",
        "write_point": write_point,
        "expected_qualification_version": expected,
        "current_qualification_version": current.get("qualification_version"),
        "blockers": current.get("blockers") or [],
    }
    try:
        from app.observability.metrics import inc
        inc(
            "video_run_dependency_fenced_total",
                episode_id=episode_id, write_point=write_point,
        )
    except Exception:  # observability must not weaken the fence
        pass
    raise ReviewDependencyFence(json.dumps(detail, ensure_ascii=False))


async def _assert_review_dependency_fence_async(
    job,
    version_id: str,
    write_point: str,
) -> None:
    if not _authority_checks_can_use_worker_thread():
        _assert_review_dependency_fence(job, version_id, write_point)
        return
    await asyncio.to_thread(
        _assert_review_dependency_fence,
        dict(job),
        version_id,
        write_point,
    )

def _assert_job_lease(job_id: str, owner: str, *, lease_seconds: float = 180.0) -> None:
    if not media_scheduler.renew_lease(job_id, owner, lease_seconds=lease_seconds):
        raise LeaseLost(f"job lease lost: {job_id} / {owner}")


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
                      next_retry_at=NULL,updated_at=?
                WHERE id=? AND status='running' AND lease_owner=?
                  AND cancellation_requested=0""",
            (message, message, now(), job_id, owner),
        )
        if changed.rowcount != 1:
            conn.rollback()
            return False
        changed = conn.execute(
            """UPDATE shot_versions
                  SET status='waiting_human',error=?
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
    claim = conn.execute(
        """SELECT amount_cny FROM provider_video_budget_claims
            WHERE operation_id=? AND job_id=?""",
        (operation_id, job_id),
    ).fetchone()
    reservation = conn.execute(
        "SELECT amount_cny FROM budget_reservations WHERE job_id=?",
        (job_id,),
    ).fetchone()
    settled_cost = max(
        0.0,
        float(
            claim["amount_cny"]
            if claim is not None
            else (reservation["amount_cny"] if reservation is not None else 0)
        ),
    )
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


def _enqueue_for_current_status(job_id: str) -> None:
    """按阶段路由到 finalize / video-ready / reference 通道。

    三通道仍共用同一 durable job 行与 CAS lease；拆分只影响调度优先级：
    已接单/待收尾绝不能排在整集参考图后面。
    """
    row = get_conn().execute(
        """SELECT j.status, j.pipeline_stage, v.provider_task_id, v.image_inputs, j.after_shot_id
           FROM jobs j LEFT JOIN shot_versions v ON v.id=j.version_id
           WHERE j.id=?""",
        (job_id,),
    ).fetchone()
    if not row:
        return
    if row["status"] == "waiting_provider" or row["provider_task_id"]:
        _queue_job(_poll_queue, job_id)
        return
    from app.media_pipeline.scheduler import continuity_anchor_ready, is_true_video_ready, scheduler_policy
    from app.media_pipeline import stages as media_stages
    meta = {}
    try:
        meta = json.loads(row["image_inputs"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        meta = {}
    continuity_ok = True
    if row["after_shot_id"]:
        continuity_ok = continuity_anchor_ready(
            get_conn(),
            row["after_shot_id"],
            require_adopted=bool(meta.get("shot_plan_id")),
        )[0]
    stage = row["pipeline_stage"]
    ready = (
        stage == media_stages.STAGE_VIDEO_READY
        or is_true_video_ready(meta, continuity_ok=continuity_ok)
    )
    if scheduler_policy() == "stage_aware" and ready:
        _queue_job(_video_ready_queue, job_id)
    else:
        _queue_job(_queue, job_id)


def _queue_job(queue: asyncio.Queue[str], job_id: str) -> None:
    """Route durable work to an asyncio queue from loop or worker threads."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        dispatcher = _dispatcher_task
        if dispatcher is not None and not dispatcher.done():
            dispatcher.get_loop().call_soon_threadsafe(
                queue.put_nowait,
                job_id,
            )
        else:
            queue.put_nowait(job_id)
        return
    queue.put_nowait(job_id)


def _reference_gallery_ready(raw_meta: str | None) -> bool:
    try:
        meta = json.loads(raw_meta or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if meta.get("reference_static_ready") and meta.get("reference_generation_complete") is False:
        return False
    return bool(meta.get("reference_images")) and meta.get("reference_generation_complete") is not False


def _auto_retake(raw_meta: str | None) -> bool:
    try:
        return int(json.loads(raw_meta or "{}").get("auto_retake_count") or 0) > 0
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


def _completed_reference_slots(raw_meta: str | None) -> int:
    try:
        meta = json.loads(raw_meta or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return 0
    slots = meta.get("reference_slots") or {}
    if isinstance(slots, dict):
        return sum(
            1
            for slot_key, slot in slots.items()
            if isinstance(slot, dict)
            and (
                video_modes.is_narrative_keyframe_slot(str(slot_key))
                or not str(slot_key).startswith("narrative_keyframe")
            )
            and slot.get("status") in {"passed", "unverified", "scored_warning"}
        )
    refs = meta.get("reference_images") or []
    return len([r for r in refs if r.get("selectedForSeedance", True) and not r.get("deleted")])


def _narrative_keyframe_candidate_progress(meta: dict[str, Any]) -> tuple[int, int]:
    """Aggregate generated candidates across every timeline keyframe slot.

    ``narrative_keyframe`` is the decisive master beat; sibling timeline beats
    use ``narrative_keyframe_*``.  Candidate records are intentionally kept out
    of ``reference_images`` until a winner is selected, so progress must come
    from the slot checkpoints rather than the public gallery.
    """
    slots = meta.get("reference_slots") or {}
    if not isinstance(slots, dict):
        slots = {}

    sequence = meta.get("keyframe_sequence")
    sequence_keys: list[str] = []
    if isinstance(sequence, dict) and isinstance(sequence.get("beats"), list):
        sequence_keys = list(dict.fromkeys(
            str(beat.get("slot_key") or "")
            for beat in sequence["beats"]
            if isinstance(beat, dict) and str(beat.get("slot_key") or "")
        ))
    if sequence_keys:
        slot_items = [(slot_key, slots.get(slot_key) or {}) for slot_key in sequence_keys]
    else:
        slot_items = [
            (str(slot_key), raw_slot)
            for slot_key, raw_slot in slots.items()
            if video_modes.is_narrative_keyframe_slot(str(slot_key))
        ]

    current = 0
    total = 0
    matched = False
    terminal_statuses = {"passed", "unverified", "scored_warning"}
    for slot_key, raw_slot in slot_items:
        if not isinstance(raw_slot, dict):
            raw_slot = {}
        matched = True
        default_target = (
            video_modes.keyframe_candidate_count()
            if str(slot_key) == "narrative_keyframe"
            else video_modes.supporting_keyframe_candidate_count()
        )
        try:
            target = max(1, int(raw_slot.get("candidate_target") or default_target))
        except (TypeError, ValueError):
            target = default_target

        records = raw_slot.get("candidates") or []
        candidate_nos: set[int] = set()
        if isinstance(records, list):
            for ordinal, record in enumerate(records, start=1):
                if not isinstance(record, dict):
                    continue
                try:
                    candidate_no = int(record.get("candidate_no") or ordinal)
                except (TypeError, ValueError):
                    candidate_no = ordinal
                if 1 <= candidate_no <= target:
                    candidate_nos.add(candidate_no)
        done = min(target, len(candidate_nos))
        # Legacy/final winner checkpoints may not retain the candidate audit
        # list.  A terminal logical slot is nevertheless complete.
        if done == 0 and raw_slot.get("status") in terminal_statuses:
            done = target
        current += done
        total += target

    if not matched:
        return 0, video_modes.estimated_keyframe_generation_count()
    return min(current, total), total


def _dispatch_due_jobs_legacy() -> dict[str, int]:
    """旧调度：poll 优先 + 主队列混合参考图/视频提交。"""
    conn = get_conn()
    stamp = now()
    rows = rows_to_dicts(conn.execute(
        """SELECT j.id, j.status, j.created_at, j.after_shot_id,
                  v.provider_task_id, v.image_inputs, s.shot_no
           FROM jobs j
           LEFT JOIN shot_versions v ON v.id=j.version_id
           LEFT JOIN shots s ON s.id=j.shot_id
           WHERE j.kind='video'
             AND j.status IN ('queued','waiting_provider')
             AND (j.next_retry_at IS NULL OR j.next_retry_at<=?)
             AND j.cancellation_requested=0 AND j.abandoned=0""",
        (stamp,),
    ).fetchall())

    poll_candidates: list[dict[str, Any]] = []
    main_candidates: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    blocked_reference_candidates: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    continuity_cache: dict[tuple[str, bool], bool] = {}

    for row in rows:
        if row.get("status") == "waiting_provider" or row.get("provider_task_id"):
            poll_candidates.append(row)
            continue
        try:
            dependency_meta = json.loads(row.get("image_inputs") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            dependency_meta = {}
        after_shot_id = row.get("after_shot_id")
        if after_shot_id:
            require_adopted = bool(dependency_meta.get("shot_plan_id"))
            cache_key = (after_shot_id, require_adopted)
            ready = continuity_cache.get(cache_key)
            if ready is None:
                from app.media_pipeline.scheduler import continuity_anchor_ready
                ready = continuity_anchor_ready(
                    conn, after_shot_id, require_adopted=require_adopted,
                )[0]
                continuity_cache[cache_key] = ready
        else:
            ready = True
        refs_ready = _reference_gallery_ready(row.get("image_inputs"))
        is_retake = _auto_retake(row.get("image_inputs"))
        age_key = float(row.get("created_at") or stamp)
        shot_key = int(row.get("shot_no") or 10**9)
        if ready:
            rank = 2 if is_retake else (0 if refs_ready else 1)
            main_candidates.append(((rank, age_key, shot_key), row))
        elif not refs_ready:
            rank = 1 if is_retake else 0
            blocked_reference_candidates.append(((rank, age_key, shot_key), row))

    poll_candidates.sort(key=lambda row: float(row.get("created_at") or stamp))
    main_candidates.sort(key=lambda item: item[0])
    blocked_reference_candidates.sort(key=lambda item: item[0])

    poll_capacity = max(1, _poll_worker_target or 1) * _DISPATCH_BACKLOG_PER_WORKER
    poll_slots = max(0, poll_capacity - _poll_queue.qsize())
    main_capacity = max(1, _worker_target or 1) * _DISPATCH_BACKLOG_PER_WORKER
    main_slots = max(0, main_capacity - _queue.qsize())

    poll_enqueued = 0
    for row in poll_candidates[:poll_slots]:
        _queue_job(_poll_queue, row["id"])
        poll_enqueued += 1

    chosen = [row for _, row in main_candidates[:main_slots]]
    remaining = max(0, main_slots - len(chosen))
    if remaining:
        from app.media_pipeline.retry_policy import prepared_reference_backlog
        speculative_limit = min(remaining, prepared_reference_backlog())
        chosen.extend(row for _, row in blocked_reference_candidates[:speculative_limit])
    for row in chosen:
        _queue_job(_queue, row["id"])

    return {"poll": poll_enqueued, "main": len(chosen), "due": len(rows), "video_ready": 0, "reference": len(chosen)}


def _dispatch_due_jobs_stage_aware() -> dict[str, int]:
    """QPSP：finalize > video_ready > reference(cohort) > retake；高低水位背压。"""
    from app.media_pipeline import stages as media_stages
    from app.media_pipeline.scheduler import (
        classify_scheduler_lane,
        continuity_anchor_ready,
        continuity_chain_remaining,
        is_true_video_ready,
        job_scheduler_score,
        should_start_more_reference_work,
    )
    from app.media_pipeline.stage_state import set_pipeline_stage

    conn = get_conn()
    stamp = now()
    rows = rows_to_dicts(conn.execute(
        """SELECT j.id, j.status, j.created_at, j.after_shot_id, j.episode_id, j.pipeline_stage,
                  v.provider_task_id, v.image_inputs, s.shot_no, s.id AS shot_pk
           FROM jobs j
           LEFT JOIN shot_versions v ON v.id=j.version_id
           LEFT JOIN shots s ON s.id=j.shot_id
           WHERE j.kind='video'
             AND j.status IN ('queued','waiting_provider')
             AND (j.next_retry_at IS NULL OR j.next_retry_at<=?)
             AND j.cancellation_requested=0 AND j.abandoned=0""",
        (stamp,),
    ).fetchall())

    poll_candidates: list[dict[str, Any]] = []
    video_ready: list[tuple[float, dict[str, Any]]] = []
    reference_critical: list[tuple[float, dict[str, Any]]] = []
    reference_normal: list[tuple[float, dict[str, Any]]] = []
    retake_jobs: list[tuple[float, dict[str, Any]]] = []
    continuity_cache: dict[tuple[str, bool], bool] = {}
    stage_updates: list[tuple[str, str, dict[str, Any]]] = []

    for row in rows:
        if row.get("status") == "waiting_provider" or row.get("provider_task_id"):
            poll_candidates.append(row)
            continue
        meta = {}
        try:
            meta = json.loads(row.get("image_inputs") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            meta = {}
        after_shot_id = row.get("after_shot_id")
        if after_shot_id:
            require_adopted = bool(meta.get("shot_plan_id"))
            cache_key = (after_shot_id, require_adopted)
            ready = continuity_cache.get(cache_key)
            if ready is None:
                ready = continuity_anchor_ready(
                    conn, after_shot_id, require_adopted=require_adopted,
                )[0]
                continuity_cache[cache_key] = ready
        else:
            ready = True
        refs_ready = _reference_gallery_ready(row.get("image_inputs"))
        static_waiting = bool(meta.get("reference_static_ready")) and not refs_ready
        true_ready = (
            row.get("pipeline_stage") == media_stages.STAGE_VIDEO_READY
            or is_true_video_ready(meta, continuity_ok=ready)
        )
        is_retake = _auto_retake(row.get("image_inputs"))
        age_min = max(0.0, (stamp - float(row.get("created_at") or stamp)) / 60.0)
        chain = 0
        if row.get("episode_id") and row.get("shot_pk"):
            chain = continuity_chain_remaining(conn, row["episode_id"], row["shot_pk"])
        completed = _completed_reference_slots(row.get("image_inputs"))
        score = job_scheduler_score(
            first_pass=not is_retake,
            continuity_remaining=chain,
            completed_slots=completed,
            wait_age_minutes=age_min,
            auto_retake=is_retake,
        )
        critical = chain > 0 or bool(after_shot_id)
        lane = classify_scheduler_lane(
            refs_ready=true_ready or refs_ready,
            continuity_ok=ready,
            is_retake=is_retake,
            static_ready_waiting=static_waiting,
            critical_path=critical,
        )
        if true_ready and ready:
            stage_updates.append((
                row["id"],
                media_stages.STAGE_VIDEO_READY,
                {
                    "scheduler_lane": media_stages.LANE_VIDEO_READY,
                    "priority_class": "first_pass" if not is_retake else "retake",
                },
            ))
        elif not ready and (refs_ready or static_waiting):
            stage_updates.append((
                row["id"],
                (
                    media_stages.STAGE_WAITING_DEPENDENCY
                    if meta.get("shot_plan_id")
                    else media_stages.STAGE_WAITING_CONTINUITY
                ),
                {
                    "reason_code": (
                        "WAITING_VIDEO_PLAN_DEPENDENCY"
                        if meta.get("shot_plan_id")
                        else "WAITING_CONTINUITY_ANCHOR"
                    ),
                    "reason_text": (
                        "等待上一镜采用素材"
                        if meta.get("shot_plan_id")
                        else (
                            f"等待镜尾帧（{after_shot_id}）"
                            if after_shot_id else "等待上一镜尾帧"
                        )
                    ),
                    "scheduler_lane": media_stages.LANE_REFERENCE_CRITICAL,
                },
            ))

        if true_ready and ready:
            video_ready.append((score, row))
        elif is_retake:
            retake_jobs.append((score, row))
        elif not ready and refs_ready:
            # 参考已齐但等尾帧：不占参考图 cohort，也不进 video_ready
            continue
        elif lane == media_stages.LANE_REFERENCE_CRITICAL or critical:
            reference_critical.append((score, row))
        else:
            reference_normal.append((score, row))

    # Keep recursive continuity reads outside the single-writer transaction.
    # Otherwise the first stage UPDATE holds SQLite's writer lock while the
    # remaining dependency chains are still being traversed.
    try:
        for job_id, stage, kwargs in stage_updates:
            set_pipeline_stage(job_id, stage, conn=conn, **kwargs)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    poll_candidates.sort(key=lambda row: float(row.get("created_at") or stamp))
    video_ready.sort(key=lambda item: -item[0])
    reference_critical.sort(key=lambda item: -item[0])
    reference_normal.sort(key=lambda item: -item[0])
    retake_jobs.sort(key=lambda item: -item[0])

    poll_capacity = max(1, _poll_worker_target or 1) * _DISPATCH_BACKLOG_PER_WORKER
    poll_slots = max(0, poll_capacity - _poll_queue.qsize())
    vr_capacity = max(1, _video_ready_worker_target or 1) * _DISPATCH_BACKLOG_PER_WORKER
    vr_slots = max(0, vr_capacity - _video_ready_queue.qsize())
    ref_capacity = max(1, _reference_worker_target or _worker_target or 1) * _DISPATCH_BACKLOG_PER_WORKER
    ref_slots = max(0, ref_capacity - _queue.qsize())

    poll_enqueued = 0
    for row in poll_candidates[:poll_slots]:
        _queue_job(_poll_queue, row["id"])
        poll_enqueued += 1

    vr_enqueued = 0
    for _, row in video_ready[:vr_slots]:
        _queue_job(_video_ready_queue, row["id"])
        vr_enqueued += 1

    # 参考图：cohort + 高低水位；关键路径优先
    allow, demand = should_start_more_reference_work(conn=conn)
    ref_enqueued = 0
    if allow and demand > 0 and ref_slots > 0:
        budget = min(demand, ref_slots)
        ordered = reference_critical + reference_normal + retake_jobs
        for _, row in ordered[:budget]:
            _queue_job(_queue, row["id"])
            ref_enqueued += 1
    elif ref_slots > 0 and reference_critical:
        # 水位满时仍允许完成已接近完成的关键路径（只取 critical，且仅当已有 slot 进度）
        for _, row in reference_critical:
            if ref_enqueued >= ref_slots:
                break
            if _completed_reference_slots(row.get("image_inputs")) > 0:
                _queue_job(_queue, row["id"])
                ref_enqueued += 1

    return {
        "poll": poll_enqueued,
        "main": vr_enqueued + ref_enqueued,
        "due": len(rows),
        "video_ready": vr_enqueued,
        "reference": ref_enqueued,
    }


def _dispatch_due_jobs() -> dict[str, int]:
    """Continuously rebuild the runnable queues from durable job state."""
    from app.media_pipeline.scheduler import scheduler_policy
    if scheduler_policy() == "legacy":
        return _dispatch_due_jobs_legacy()
    return _dispatch_due_jobs_stage_aware()


async def _durable_dispatcher() -> None:
    """DB-backed dispatcher; in-memory queue loss heals within one interval."""
    try:
        while True:
            try:
                await asyncio.to_thread(_dispatch_due_jobs)
                # Recreate an unexpectedly dead worker without changing the
                # configured target. Worker loops catch job errors themselves,
                # so this is primarily protection against lifecycle regressions.
                if _worker_target > 0 or _video_ready_worker_target > 0:
                    ensure_workers()
            except Exception as exc:  # noqa: BLE001 dispatcher must remain alive
                errors.record_and_format(exc, action="durable_media_dispatch")
            await asyncio.sleep(_DISPATCH_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        return


def _start_durable_dispatcher() -> None:
    global _dispatcher_task
    if _dispatcher_task is not None and not _dispatcher_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _dispatcher_task = loop.create_task(_durable_dispatcher(), name="durable-media-dispatcher")


def _drain_memory_queue(queue: asyncio.Queue[str]) -> None:
    """Drop startup duplicates; every durable row is rediscovered immediately."""
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        else:
            queue.task_done()


async def _requeue_after(job_id: str, delay: float) -> None:
    """冷却 delay 秒后把 job 重新投入队列。状态已先置回 queued，故进程重启时
    recover_and_start 也能兜底重排，不依赖本协程存活。"""
    try:
        await asyncio.sleep(delay)
        _enqueue_for_current_status(job_id)
    except asyncio.CancelledError:
        pass


def _schedule_job_retry(
    job_id: str, exc: ProviderError, *, lease_owner: str | None = None
) -> bool:
    """瞬时（可重试）上游故障时把 job 延迟重排，返回是否已安排重试。
    超过 VIDEO_JOB_MAX_RETRIES 后返回 False，交由调用方走永久失败逻辑。"""
    if not getattr(exc, "retryable", False):
        return False
    conn = get_conn()
    row = conn.execute(
        "SELECT retry_count, max_retries, lease_owner FROM jobs WHERE id=?", (job_id,)
    ).fetchone()
    if lease_owner and (not row or row["lease_owner"] != lease_owner):
        return False
    attempt = int(row["retry_count"] or 0) + 1 if row else 1
    max_retries = int(row["max_retries"] or config.VIDEO_JOB_MAX_RETRIES) if row else config.VIDEO_JOB_MAX_RETRIES
    if attempt > max_retries:
        return False
    delay = config.VIDEO_JOB_RETRY_BASE_DELAY * (2 ** (attempt - 1))
    note = (f"大模型/外部服务瞬时故障，已自动排队第 {attempt}/{max_retries} 次重试"
            f"（约 {int(delay)} 秒后）。无需处理；若多次重试后仍失败才需关注错误码。")
    failure = exc.failure
    updated = conn.execute(
        """UPDATE jobs SET status='queued', error=?, retry_count=?, next_retry_at=?,
                  provider_failure_category=?,provider_failure_kind=?,
                  provider_failure_disposition=?,provider_failure_retryable=?,
                  lease_owner=NULL, lease_expires_at=NULL, updated_at=? WHERE id=?"""
        + (" AND lease_owner=?" if lease_owner else ""),
        (
            note,
            attempt,
            now() + delay,
            failure.category.value,
            failure.kind,
            failure.disposition.value,
            int(failure.retryable),
            now(),
            job_id,
            *([lease_owner] if lease_owner else []),
        ),
    )
    if updated.rowcount != 1:
        conn.rollback()
        return False
    conn.execute(
        "UPDATE budget_reservations SET status='reserved' WHERE job_id=? AND status='running'",
        (job_id,),
    )
    conn.commit()
    job = conn.execute("SELECT run_id, step_run_id FROM jobs WHERE id=?", (job_id,)).fetchone()
    if job:
        mark_media_job_state(job["run_id"], job["step_run_id"], "queued", note)
    task = asyncio.get_running_loop().create_task(_requeue_after(job_id, delay))
    _retry_tasks.add(task)
    task.add_done_callback(_retry_tasks.discard)
    return True


def _defer_provider_poll(
    job_id: str,
    task_id: str,
    *,
    lease_owner: str,
    delay: float | None = None,
) -> bool:
    """供应商仍在生成时释放 worker，并持久化安排下一次状态查询。

    Phase 1：状态写入 waiting_provider（不再占 worker 槽）；单次 poll 后即调用本函数。
    这不是一次 provider retry：不会新建付费任务，也不消耗 retry_count。
    provider_task_id 已持久化，下一次只会继续轮询同一个任务。
    """
    conn = get_conn()
    wait = max(0.0, float(
        config.VIDEO_POLL_INTERVAL if delay is None else delay
    ))
    due = now() + wait
    note = (
        f"供应商任务 {task_id} 仍在生成，已释放本地 worker；"
        f"约 {int(wait)} 秒后自动继续查询，不会重复提交或产生新任务。"
    )
    updated = conn.execute(
        """UPDATE jobs SET status='waiting_provider', error=?, next_retry_at=?,
                  lease_owner=NULL, lease_expires_at=NULL, updated_at=?
           WHERE id=? AND status='running' AND lease_owner=?
             AND cancellation_requested=0 AND abandoned=0""",
        (note, due, now(), job_id, lease_owner),
    )
    if updated.rowcount != 1:
        conn.rollback()
        return False
    conn.execute(
        "UPDATE budget_reservations SET status='reserved' "
        "WHERE job_id=? AND status='running'",
        (job_id,),
    )
    conn.commit()
    job = conn.execute(
        "SELECT run_id, step_run_id FROM jobs WHERE id=?", (job_id,)
    ).fetchone()
    if job:
        mark_media_job_state(job["run_id"], job["step_run_id"], "queued", note)
    task = asyncio.get_running_loop().create_task(_requeue_after(job_id, wait))
    _retry_tasks.add(task)
    task.add_done_callback(_retry_tasks.discard)
    return True


async def critique_version(version_id: str) -> list[str]:
    """VLM 视觉质检已整体下线：不再产生评语，也不再现场调用模型评审。
    保留函数签名与调用点（「带评语重生」入口），避免上游报错——现在等价于
    普通重生，返回空评语列表。"""
    del version_id
    return []


# ---------- 执行 ----------

def _set_job(
    job_id: str,
    status: str,
    error: str | None = None,
    *,
    lease_owner: str | None = None,
) -> bool:
    conn = get_conn()
    terminal = status in {"succeeded", "failed", "cancelled", "abandoned", "paused_budget"}
    if terminal:
        cursor = conn.execute(
            "UPDATE jobs SET status=?, error=?, updated_at=?, video_slot_active=0, "
            "lease_owner=NULL, lease_expires_at=NULL "
            "WHERE id=?" + (" AND lease_owner=?" if lease_owner else ""),
            (status, error, now(), job_id, *([lease_owner] if lease_owner else [])),
        )
    else:
        cursor = conn.execute(
            "UPDATE jobs SET status=?, error=?, updated_at=? WHERE id=?"
            + (" AND lease_owner=?" if lease_owner else ""),
            (status, error, now(), job_id, *([lease_owner] if lease_owner else [])),
        )
    if cursor.rowcount != 1:
        conn.rollback()
        return False
    if terminal:
        conn.execute(
            """UPDATE shot_versions
                  SET video_slot_active=0
                WHERE id=(SELECT version_id FROM jobs WHERE id=?)""",
            (job_id,),
        )
    conn.commit()
    row = conn.execute("SELECT run_id, step_run_id FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row:
        mark_media_job_state(row["run_id"], row["step_run_id"], status, error)
    return True


def _set_version(version_id: str, **fields) -> bool:
    conn = get_conn()
    cols = ", ".join(f"{k}=?" for k in fields)
    cancellation_guard = (
        """ AND NOT EXISTS (
                SELECT 1 FROM jobs j
                 WHERE j.version_id=shot_versions.id
                   AND j.cancellation_requested=1
            )"""
        if "status" in fields
        else ""
    )
    cursor = conn.execute(
        f"UPDATE shot_versions SET {cols} WHERE id=?{cancellation_guard}",
        (*fields.values(), version_id),
    )
    conn.commit()
    return cursor.rowcount == 1


def _video_model_rejection_guidance(
    meta: dict[str, Any],
    exc: ProviderError,
) -> tuple[str, str] | None:
    """Build guidance from a typed provider outcome, never from error prose."""
    if exc.failure.category is not hiagent.ProviderFailureCategory.MODEL_REJECTION:
        return None
    if exc.failure.kind == hiagent.ProviderFailureKind.PROMPT_PROVIDER_REJECTED.value:
        return (
            "VIDEO_PROMPT_PROVIDER_REJECTED",
            "AI 视频提示词服务明确拒绝了当前内容；系统未改写内容、未切换生成方式，"
            "也未向视频服务提交本镜。请更换获准的提示词模型或人工调整内容后再继续。",
        )
    mode = str(meta.get("mode") or meta.get("planned_mode") or "")
    return (
        "VIDEO_PROVIDER_MODEL_REJECTED",
        f"当前视频模型明确拒绝了本次输入，系统已保持 {mode or '原计划模式'} "
        "失败且没有改写内容或切换生成方式。请检查供应商原始证据或更换视频模型后重试。",
    )


def _provider_submitted_at(
    conn,
    job,
    task_id: str,
    *,
    lease_owner: str | None = None,
) -> float:
    """返回 provider 首次接受当前视频 task 的时间，并为旧任务补齐持久字段。

    轮询预算必须基于这个绝对时间，不能在 worker 重启后重新开始计时。
    """
    persisted = _row_value(job, "provider_submitted_at")
    if persisted:
        return float(persisted)
    operation_id = _row_value(job, "provider_operation_id")
    provider_call = conn.execute(
        """SELECT MIN(ts) AS submitted_at FROM provider_calls
           WHERE kind='video_create' AND status='OK'
             AND (operation_id=? OR meta LIKE ?)""",
        (operation_id, f"%{task_id}%"),
    ).fetchone()
    submitted_at = (
        float(provider_call["submitted_at"])
        if provider_call and provider_call["submitted_at"] is not None
        else float(_row_value(job, "attempt_started_at") or time.time())
    )
    updated = conn.execute(
        "UPDATE jobs SET provider_submitted_at=? WHERE id=?"
        + (
            " AND status='running' AND lease_owner=? AND cancellation_requested=0"
            if lease_owner is not None
            else ""
        ),
        (submitted_at, job["id"], *([lease_owner] if lease_owner is not None else [])),
    )
    if updated.rowcount != 1:
        conn.rollback()
        raise LeaseLost(
            f"provider submission timestamp lost lease: {job['id']} / {lease_owner}"
        )
    conn.commit()
    return submitted_at


def _provider_wait_policy(
    task_id: str,
    result: dict[str, Any],
    meta: dict[str, Any],
    *,
    duration_s: float,
    provider_submitted_at: float,
    stamp: float | None = None,
) -> dict[str, Any]:
    """Choose queue-aware polling and timeout behavior for the active provider."""
    current = time.time() if stamp is None else float(stamp)
    provider_age = max(0.0, current - float(provider_submitted_at))
    policy = {
        "elapsed_s": provider_age,
        "timeout_s": float(config.VIDEO_PROVIDER_MAX_WAIT),
        "poll_delay_s": None,
        "scope": "供应商任务",
        "meta_changed": False,
        "stage_progress": None,
    }
    from app import video_providers

    adapter = video_providers.adapter_for_task_id(task_id)
    if adapter is None:
        return policy
    return adapter.apply_wait_policy(
        task_id,
        result,
        meta,
        policy,
        duration_s=duration_s,
        current=current,
    )


def _recover_paid_video_task(conn, operation_id: str | None) -> tuple[str, float] | None:
    """Recover a provider handle accepted before the local job commit."""
    if not operation_id:
        return None
    rows = conn.execute(
        """SELECT ts, response_json FROM provider_calls
           WHERE kind='video_create' AND status='OK' AND operation_id=?
             AND response_json IS NOT NULL
           ORDER BY id DESC""",
        (operation_id,),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["response_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        task_id = str(payload.get("id") or "").strip() if isinstance(payload, dict) else ""
        if task_id:
            return task_id, float(row["ts"])
    return None


def _paid_video_attempt_count(conn, version_id: str) -> int:
    prefix = f"video-create-{version_id}"
    row = conn.execute(
        """SELECT COUNT(DISTINCT operation_id) AS count
           FROM provider_calls
           WHERE kind='video_create' AND status='OK'
             AND response_json IS NOT NULL
             AND (operation_id=? OR operation_id LIKE ?)""",
        (prefix, f"{prefix}-%"),
    ).fetchone()
    return int(row["count"] or 0) if row else 0


def _reserve_video_resubmit(job, shot) -> bool:
    """Reserve one additional payable attempt before changing operation_id."""
    limit = episode_video_budget_limit(str(job["episode_id"]))
    return media_scheduler.extend_budget_reservation(
        job["id"],
        job["episode_id"],
        shot_cost_cny(shot["duration_s"]),
        limit,
        conn=get_conn(),
    )


def _reserve_or_pause_video_resubmit(job, version, shot, owner: str) -> bool:
    if _reserve_video_resubmit(job, shot):
        return True
    message = "追加提交会超过单集预算，任务已暂停；提高成本上限后可继续"
    conn = get_conn()
    changed = conn.execute(
        """UPDATE jobs
           SET status='paused_budget', error=?, updated_at=?,
               video_slot_active=0,
               lease_owner=NULL, lease_expires_at=NULL
           WHERE id=? AND status='running' AND lease_owner=?""",
        (message, now(), job["id"], owner),
    )
    if changed.rowcount == 1:
        conn.execute(
            """UPDATE shot_versions
                  SET status='paused_budget',error=?,video_slot_active=0
                WHERE id=?""",
            (message, version["id"]),
        )
        conn.commit()
        mark_media_job_state(
            _row_value(job, "run_id"),
            _row_value(job, "step_run_id"),
            "paused_budget",
            message,
        )
    else:
        conn.rollback()
    return False


def _persist_video_resubmit(
    conn,
    *,
    job_id: str,
    version_id: str,
    prompt_text: str,
    meta: dict,
    operation_id: str,
) -> None:
    """Persist the next intentional paid attempt as one recoverable checkpoint."""
    from app import video_providers

    for key in video_providers.all_wait_meta_keys():
        meta.pop(key, None)
    paid_attempts = max(
        int(meta.get("provider_paid_attempts") or 0),
        _paid_video_attempt_count(conn, version_id),
    )
    if paid_attempts:
        meta["provider_paid_attempts"] = paid_attempts
    conn.execute(
        """UPDATE shot_versions
           SET prompt_text=?, provider_task_id=NULL, image_inputs=?
           WHERE id=?""",
        (prompt_text, json.dumps(meta, ensure_ascii=False), version_id),
    )
    conn.execute(
        """UPDATE jobs
           SET provider_operation_id=?, provider_create_state='not_started',
               provider_non_cancellable=0, provider_submitted_at=NULL,
               provider_failure_category=NULL,provider_failure_kind=NULL,
               provider_failure_disposition=NULL,provider_failure_retryable=NULL,
               updated_at=?
           WHERE id=?""",
        (operation_id, now(), job_id),
    )
    conn.commit()


def _video_image_inputs_from_meta(meta: dict) -> list[tuple[str, str]]:
    try:
        return video_modes.build_seedance_image_inputs(meta)
    except ProviderError as exc:
        if meta.get("mode") in {
            video_modes.REFERENCE_IMAGE_MODE,
            video_modes.FIRST_FRAME_MODE,
            video_modes.FIRST_LAST_FRAME_MODE,
        }:
            raise VideoInputRepairRequired(str(exc)) from exc
        raise


async def _prepare_reference_mode_inputs(
    conn, job, version, shot, ep, meta: dict, prompt_text: str,
    *, lease_owner: str | None = None,
) -> tuple[dict, str]:
    if meta.get("mode") != video_modes.REFERENCE_IMAGE_MODE:
        return meta, prompt_text

    def _assert_reference_lease() -> None:
        if lease_owner is not None:
            _assert_job_lease(job["id"], lease_owner)

    def _invalidate_reference_checkpoint(reason: str) -> None:
        meta["stale_reference_reason"] = reason
        meta["stale_keyframe_prompt_contract_version"] = meta.get("keyframe_prompt_contract_version")
        meta["keyframe_prompt_contract_version"] = video_modes.KEYFRAME_PROMPT_CONTRACT_VERSION
        meta["reference_input_policy_version"] = video_modes.REFERENCE_INPUT_POLICY_VERSION
        meta.pop("keyframe_contract_fingerprint", None)
        meta["reference_images"] = []
        meta["reference_slots"] = {}
        meta.pop("keyframe_sequence", None)
        meta["reference_manifest_frozen"] = False
        meta["reference_manifest_asset_stale"] = True
        meta["reference_generation_complete"] = False
        meta["reference_static_ready"] = False
        meta["continuity_anchor_ready"] = False
        meta["reference_group_gate_passed"] = False
        meta["video_input_manifest_frozen"] = False
        meta.pop("narrative_keyframe_missing", None)
        # 新画廊不得沿用旧 fingerprint/refset，否则 reference_store 会早返并指回旧图。
        for stale_key in (
            "reference_set_id", "reference_gallery_fingerprint", "reference_gallery_revision",
            "reference_gallery_source_version_id", "reference_gallery_edited",
            "reference_gallery_contract_override", "video_input_fingerprint",
        ):
            meta.pop(stale_key, None)

    # Historical galleries predate this marker and are complete.  A gallery
    # explicitly marked incomplete is a streamed checkpoint from an interrupted
    # generation and must resume instead of being mistaken for the final set.
    complete_gallery_candidate = False
    if meta.get("reference_images"):
        incomplete_checkpoint = meta.get("reference_generation_complete") is False
        if incomplete_checkpoint:
            checkpoint_matches = video_modes.reference_gallery_matches_library_policy(meta)
            if not checkpoint_matches:
                _invalidate_reference_checkpoint("library_reference_checkpoint_invalid")
            elif (
                meta.get("reference_static_ready")
                and not video_modes.reference_gallery_matches_library_policy(meta)
            ):
                _invalidate_reference_checkpoint("library_reference_file_invalid")
        else:
            gallery_matches = video_modes.reference_gallery_matches_library_policy(meta)
            if gallery_matches:
                complete_gallery_candidate = True
            else:
                _invalidate_reference_checkpoint("reference_input_policy_or_file_invalid")
    from app.schemas import Bible
    from app.media_pipeline import stages as media_stages
    from app.media_pipeline.stage_state import set_pipeline_stage

    project = conn.execute("SELECT * FROM projects WHERE id=?", (job["project_id"],)).fetchone()
    bible = Bible.model_validate(json.loads(project["bible_json"]))
    # 本集视图：关键帧文字锚点与参考图按集取覆盖该集的分段定妆照（同段同源）
    from app.portraits import bible_for_episode
    bible = bible_for_episode(job["project_id"], bible, ep["episode_no"])
    screenplay = None
    # Real episode rows always carry ``id``.  Lightweight legacy unit-test
    # rows intentionally do not; keep those on the explicit legacy path.
    if _row_value(ep, "id") or _row_value(ep, "screenplay_json"):
        from app.production.screenplay_authority import resolve_downstream_screenplay

        screenplay = resolve_downstream_screenplay(
            job["episode_id"], conn=conn,
        ).screenplay
    shot_model = _load_shot_model(shot)
    # 入队时 compile_prompt 已把接触镜机位确定性归一为“侧面”。执行时必须使用
    # 该视频版本冻结的合同，不能只重读 shots 行中可能较旧的 camera_angle。
    from app.continuity import apply_shot_contract
    apply_shot_contract(shot_model, meta.get("shot_contract_json"))
    prev_shot = conn.execute("SELECT * FROM shots WHERE id=?", (meta.get("after_shot_id"),)).fetchone() if meta.get("after_shot_id") else None
    needs_tail = False
    if complete_gallery_candidate:
        # 提示词合同相同仍不代表人物/场景锚点未变。入队复用会把
        # manifest 一起带过来；兼容从关键帧 asset 内的冻结副本回退读取。
        frozen_manifest = meta.get("reference_manifest")
        if not isinstance(frozen_manifest, dict):
            frozen_manifest = next(
                (
                    ref.get("dependency_manifest") for ref in (meta.get("reference_images") or [])
                    if isinstance(ref, dict) and isinstance(ref.get("dependency_manifest"), dict)
                ),
                None,
            )
        if not video_modes.reference_gallery_matches_library_policy(meta):
            _invalidate_reference_checkpoint("reference_input_policy_changed")
            complete_gallery_candidate = False
        if complete_gallery_candidate and needs_tail:
            frozen_tail_contract = next(
                (
                    (ref.get("dependency_manifest") or {}).get("continuity_source")
                    for ref in (meta.get("reference_images") or [])
                    if isinstance(ref, dict) and ref.get("type") == "previous_shot_frame"
                ),
                None,
            )
            current_tail_contract = video_modes.previous_tail_source_contract(conn, prev_shot)
            if not isinstance(frozen_tail_contract, dict) or frozen_tail_contract != current_tail_contract:
                _invalidate_reference_checkpoint("continuity_tail_source_changed")
                complete_gallery_candidate = False
        from app.multiview import manifest_revisions_match, resolve_shot_asset_dependencies

        if complete_gallery_candidate:
            current_manifest = resolve_shot_asset_dependencies(
                project_id=job["project_id"], episode_no=ep["episode_no"], shot_id=job["shot_id"],
                shot=shot_model, scene_name=getattr(shot_model, "scene_name", "") or None,
                conn=conn, bible=bible, screenplay=screenplay,
            )
            if isinstance(frozen_manifest, dict) and manifest_revisions_match(frozen_manifest, current_manifest):
                meta["reference_manifest"] = frozen_manifest
                meta["reference_manifest_frozen"] = True
                if video_modes.REFERENCE_PROMPT_NOTE_MARKER not in prompt_text:
                    packed_refs = video_modes.pack_reference_images_for_seedance(
                        list(meta.get("reference_images") or []),
                        required_identity_names=list(
                            meta.get("required_reference_characters") or []
                        ),
                    )
                    prompt_text = (
                        video_modes.append_reference_prompt_notes_from_dicts(
                            prompt_text,
                            packed_refs,
                            duration_s=shot_model.duration_s,
                        )
                    )
                set_pipeline_stage(
                    job["id"],
                    media_stages.STAGE_VIDEO_READY,
                    scheduler_lane=media_stages.LANE_VIDEO_READY,
                    ready_at=now(),
                    conn=conn,
                )
                _set_version(
                    version["id"],
                    image_inputs=json.dumps(meta, ensure_ascii=False),
                    prompt_text=prompt_text,
                )
                conn.commit()
                return meta, prompt_text
            _invalidate_reference_checkpoint("reference_dependency_manifest_changed")
    # 复用入队时已确定的模式决策，不在生成时再跑一次 LLM 选择：既省每镜一次文本调用，
    # 又避免模式在入队与执行之间无谓翻转（决策应在入队时一次定死）。
    decision = video_modes.dict_to_decision(meta.get("mode_decision") or {})
    if decision.mode != video_modes.REFERENCE_IMAGE_MODE:
        raise ProviderError("参考图输入准备收到非参考图计划，禁止执行层改写模式")
    shot_id = job["shot_id"]
    if meta.get("reference_static_ready") and needs_tail and meta.get("reference_images"):
        from app.multiview import manifest_revisions_match, resolve_shot_asset_dependencies

        frozen_manifest = meta.get("reference_manifest")
        current_manifest = resolve_shot_asset_dependencies(
            project_id=job["project_id"], episode_no=ep["episode_no"], shot_id=shot_id,
            shot=shot_model, scene_name=getattr(shot_model, "scene_name", "") or None,
            conn=conn, bible=bible, screenplay=screenplay,
        )
        if not isinstance(frozen_manifest, dict) or not manifest_revisions_match(frozen_manifest, current_manifest):
            _invalidate_reference_checkpoint("reference_dependency_manifest_changed")
        elif not video_modes.reference_gallery_matches_library_policy(meta):
            # 静态预取点可能在 worker 崩溃后只剩 evidence，或关键帧文件已丢失。
            # 连续性快路不能只装配尾帧就把这组资产标成完成。
            _invalidate_reference_checkpoint("static_keyframe_contract_or_file_invalid")
    rejection_details: list[dict[str, Any]] = []
    rejected_assets: list = []

    def _reference_keyframe_gate_passed(current_assets: list) -> bool:
        """Validate the exact existing-library files returned by the builder."""
        return video_modes.reference_gallery_matches_library_policy({
            **meta,
            "reference_images": [a.public_dict() for a in current_assets],
        })

    def _delete_rejected_assets(items: list) -> None:
        # Never let a recovered/stale worker remove files owned by the new
        # attempt.  This check also extends the lease at every checkpoint.
        _assert_reference_lease()
        from app.rejected_media import discard_file
        for asset in items:
            discard_file(getattr(asset, "path", None))
            asset.path = None
            asset.url = None

    def _persist_reference_progress(current_assets: list, current_rejected: list) -> None:
        """Checkpoint usable references only; rejected images are irrecoverably removed."""
        _delete_rejected_assets(current_rejected)
        meta["mode_decision"] = video_modes.decision_to_dict(decision)
        meta["reference_generation_complete"] = False
        meta["reference_images"] = video_modes.dedupe_reference_dicts(
            [a.public_dict() for a in current_assets]
        )
        candidate_done, candidate_total = _narrative_keyframe_candidate_progress(meta)
        set_pipeline_stage(
            job["id"], media_stages.STAGE_REFERENCE_GENERATE,
            stage_progress={
                "current": candidate_done,
                "total": candidate_total,
                "unit": "library_assets",
            },
            scheduler_lane=media_stages.LANE_REFERENCE_CRITICAL if needs_tail else media_stages.LANE_REFERENCE_NORMAL,
            conn=conn,
        )
        _set_version(version["id"], image_inputs=json.dumps(meta, ensure_ascii=False))
        conn.commit()

    # 连续镜两段式：静态参考可预取；缺尾帧时不得宣称最终完成
    set_pipeline_stage(job["id"], media_stages.STAGE_REFERENCE_PROMPT, conn=conn)
    conn.commit()

    # 若已静态就绪、仅等尾帧：只做装配，不重跑整组生成
    if meta.get("reference_static_ready") and needs_tail and meta.get("reference_images"):
        from app.media_pipeline.scheduler import continuity_anchor_ready
        ready, reason = continuity_anchor_ready(conn, job["after_shot_id"] or (prev_shot["id"] if prev_shot else None))
        if not ready:
            set_pipeline_stage(
                job["id"], media_stages.STAGE_WAITING_CONTINUITY,
                reason_code="WAITING_CONTINUITY_ANCHOR",
                reason_text=reason or "参考图已备齐，等待上一镜尾帧",
                conn=conn,
            )
            conn.commit()
            raise _ContinuityWait(reason or "参考图已备齐，等待上一镜尾帧")
        set_pipeline_stage(job["id"], media_stages.STAGE_CONTINUITY_ASSEMBLING, conn=conn)
        conn.commit()
        assets = await video_modes.assemble_continuity_tail(
            conn=conn, project_id=job["project_id"], episode_no=ep["episode_no"], episode_id=job["episode_id"],
            shot_id=shot_id, shot=shot_model, bible=bible, meta=meta, prev_shot=prev_shot,
            rejection_details=rejection_details, rejected_out=rejected_assets,
            screenplay=screenplay,
        )
        if assets:
            _delete_rejected_assets(rejected_assets)
            assembled_refs = video_modes.dedupe_reference_dicts(
                [a.public_dict() for a in assets]
            )
            assembled_meta = {**meta, "reference_images": assembled_refs}
            if not video_modes.reference_gallery_matches_library_policy(assembled_meta):
                _invalidate_reference_checkpoint("continuity_assembly_library_asset_invalid")
                assets = []
        if assets:
            meta["reference_images"] = assembled_refs
            meta["reference_generation_complete"] = True
            meta["reference_static_ready"] = True
            meta["continuity_anchor_ready"] = True
            meta["reference_group_gate_passed"] = True
            meta["video_input_manifest_frozen"] = True
            meta.pop("first_frame_path", None)
            meta.pop("last_frame_path", None)
            prompt_text = video_modes.append_reference_prompt_notes(
                prompt_text,
                assets,
                duration_s=shot_model.duration_s,
                required_identity_names=list(
                    meta.get("required_reference_characters") or []
                ),
            )
            try:
                from app.media_pipeline.reference_store import upsert_reference_set_from_meta
                upsert_reference_set_from_meta(
                    shot_id=shot_id, version_id=version["id"], meta=meta, conn=conn,
                    static_ready=True, continuity_ready=True, group_gate_passed=True,
                )
            except Exception:  # noqa: BLE001
                pass
            set_pipeline_stage(
                job["id"], media_stages.STAGE_VIDEO_READY,
                scheduler_lane=media_stages.LANE_VIDEO_READY, ready_at=now(), conn=conn,
            )
            _set_version(version["id"], image_inputs=json.dumps(meta, ensure_ascii=False), prompt_text=prompt_text)
            conn.commit()
            return meta, prompt_text

    assets = await video_modes.build_reference_assets(
        conn=conn, project_id=job["project_id"], episode_no=ep["episode_no"], episode_id=job["episode_id"],
        shot_id=shot_id, shot=shot_model, bible=bible, decision=decision, prev_shot=prev_shot,
        rejection_details=rejection_details, rejected_out=rejected_assets,
        on_progress=_persist_reference_progress,
        allow_missing_continuity_tail=needs_tail,
        job_id=job["id"],
        existing_meta=meta,
        screenplay=screenplay,
    )

    # A pre-fix recovered worker could leave a selected/passed keyframe row
    # after another stale worker deleted the underlying file.  Anchors still
    # make ``assets`` truthy, so the ordinary empty-result retry cannot repair
    # this poisoned checkpoint.  Clear it durably and rebuild once in the same
    # task before surfacing an error or attempting a paid video submission.
    if assets and not _reference_keyframe_gate_passed(assets):
        _assert_reference_lease()
        log_provider_call(
            "reference_keyframe_checkpoint_auto_repair",
            config.MODEL_TEXT,
            "REFERENCE_CHECKPOINT_AUTO_REPAIR",
            None,
            0,
            meta={
                "shot_id": shot_id,
                "reason": "final_keyframe_file_missing",
                "repair_attempt": 1,
            },
        )
        _delete_rejected_assets(rejected_assets)
        rejected_assets = []
        _invalidate_reference_checkpoint("final_keyframe_file_missing")
        meta["keyframe_file_repair_count"] = int(meta.get("keyframe_file_repair_count") or 0) + 1
        _set_version(
            version["id"],
            image_inputs=json.dumps(meta, ensure_ascii=False),
            prompt_text=prompt_text,
        )
        conn.commit()

        repair_rejection: list[dict[str, Any]] = []
        assets = await video_modes.build_reference_assets(
            conn=conn, project_id=job["project_id"], episode_no=ep["episode_no"], episode_id=job["episode_id"],
            shot_id=shot_id, shot=shot_model, bible=bible, decision=decision, prev_shot=prev_shot,
            rejection_details=repair_rejection, rejected_out=rejected_assets,
            on_progress=_persist_reference_progress,
            allow_missing_continuity_tail=needs_tail,
            job_id=job["id"],
            existing_meta=meta,
            screenplay=screenplay,
        )
        rejection_details.extend(repair_rejection)

    # 静态完成但缺强制尾帧 → 停在 waiting_continuity，不标 complete
    if assets and needs_tail:
        has_tail = any(getattr(a, "type", None) == "previous_shot_frame" for a in assets)
        if not has_tail:
            meta["mode_decision"] = video_modes.decision_to_dict(decision)
            _delete_rejected_assets(rejected_assets)
            meta["reference_images"] = video_modes.dedupe_reference_dicts(
                [a.public_dict() for a in assets]
            )
            meta["reference_static_ready"] = True
            meta["reference_generation_complete"] = False
            meta["continuity_anchor_ready"] = False
            try:
                from app.media_pipeline.reference_store import upsert_reference_set_from_meta
                upsert_reference_set_from_meta(
                    shot_id=shot_id, version_id=version["id"], meta=meta, conn=conn,
                    static_ready=True, continuity_ready=False, group_gate_passed=False,
                )
            except Exception:  # noqa: BLE001
                pass
            set_pipeline_stage(
                job["id"], media_stages.STAGE_WAITING_CONTINUITY,
                reason_code="WAITING_CONTINUITY_ANCHOR",
                reason_text="参考图已备齐，等待上一镜尾帧",
                conn=conn,
            )
            _set_version(version["id"], image_inputs=json.dumps(meta, ensure_ascii=False), prompt_text=prompt_text)
            conn.commit()
            raise _ContinuityWait("参考图已备齐，等待上一镜尾帧")

    # ── 第 1 次失败：记录原始失败原因并重试 1 次 ──
    if not assets:
        log_provider_call(
            "reference_image_mode_attempt_1_failed", config.MODEL_TEXT, "REFERENCE_ATTEMPT_FAILED",
            None, 0, meta={
                "shot_id": shot_id,
                "attempt": 1,
                "original_failure_reason": f"第 1 次参考图生成未产出可用资产（{len(rejection_details)} 张被拒绝）",
                "rejection_details": rejection_details[:5],
            })

        retry_rejection: list[dict[str, Any]] = []
        _delete_rejected_assets(rejected_assets)
        rejected_assets = []
        assets = await video_modes.build_reference_assets(
            conn=conn, project_id=job["project_id"], episode_no=ep["episode_no"], episode_id=job["episode_id"],
            shot_id=shot_id, shot=shot_model, bible=bible, decision=decision, prev_shot=prev_shot,
            rejection_details=retry_rejection, rejected_out=rejected_assets,
            on_progress=_persist_reference_progress,
            allow_missing_continuity_tail=needs_tail,
            job_id=job["id"],
            existing_meta=meta,
            screenplay=screenplay,
        )
        rejection_details.extend(retry_rejection)

        if assets:
            log_provider_call(
                "reference_image_mode_retry_success", config.MODEL_TEXT, "REFERENCE_RETRY_SUCCESS",
                None, 0, meta={"shot_id": shot_id, "attempt": 2, "count": len(assets)})
        else:
            log_provider_call(
                "reference_image_mode_retry_failed", config.MODEL_TEXT, "REFERENCE_RETRY_FAILED",
                None, 0, meta={
                    "shot_id": shot_id,
                    "attempt": 2,
                    "total_rejection_count": len(rejection_details),
                    "rejection_details": rejection_details[:10],
                    "original_failure_reason": f"参考图模式 2 次尝试均未产出可用资产（共 {len(rejection_details)} 张被拒绝）",
                })

    # ── 参考图模式成功 ──
    if assets:
        meta["mode_decision"] = video_modes.decision_to_dict(decision)
        _delete_rejected_assets(rejected_assets)
        meta["reference_images"] = video_modes.dedupe_reference_dicts(
            [a.public_dict() for a in assets]
        )
        meta["reference_generation_complete"] = True
        meta["reference_static_ready"] = True
        meta["continuity_anchor_ready"] = True
        if not _reference_keyframe_gate_passed(assets):
            _assert_reference_lease()
            meta["reference_gate_retry_exhausted"] = True
            meta["reference_group_gate_passed"] = False
            meta["video_input_manifest_frozen"] = False
            log_provider_call(
                "reference_keyframe_gate_repair_required",
                config.MODEL_TEXT,
                "REPAIR_REQUIRED",
                None,
                0,
                meta={
                    "shot_id": shot_id,
                    "mode": video_modes.REFERENCE_IMAGE_MODE,
                },
            )
            _set_version(
                version["id"],
                image_inputs=json.dumps(meta, ensure_ascii=False),
            )
            raise VideoInputRepairRequired(
                "人物谱或场景库参考图文件不可用"
            )
        meta["reference_group_gate_passed"] = True
        meta["video_input_manifest_frozen"] = True
        meta.pop("first_frame_path", None)
        meta.pop("last_frame_path", None)
        meta.pop("first_frame_scene_id", None)
        meta.pop("last_frame_scene_id", None)
        prompt_text = video_modes.append_reference_prompt_notes(
            prompt_text,
            assets,
            duration_s=shot_model.duration_s,
            required_identity_names=list(
                meta.get("required_reference_characters") or []
            ),
        )
        _assert_reference_lease()
        try:
            from app.media_pipeline.reference_store import upsert_reference_set_from_meta
            upsert_reference_set_from_meta(
                shot_id=shot_id, version_id=version["id"], meta=meta, conn=conn,
                static_ready=True, continuity_ready=True, group_gate_passed=True,
            )
        except Exception:  # noqa: BLE001 参考图集落库失败不阻断视频
            pass
        set_pipeline_stage(
            job["id"], media_stages.STAGE_VIDEO_READY,
            scheduler_lane=media_stages.LANE_VIDEO_READY, ready_at=now(), conn=conn,
        )
        _set_version(version["id"], image_inputs=json.dumps(meta, ensure_ascii=False), prompt_text=prompt_text)
        conn.commit()
        return meta, prompt_text

    # ── 参考图模式两次均未得到文件：保留原模式并进入修复 ──
    _delete_rejected_assets(rejected_assets)
    ref_failure_reason = (
        f"参考图模式 2 次尝试均未产出可用资产 "
        f"（共 {len(rejection_details)} 张被拒绝）"
    )
    log_provider_call(
        "reference_image_mode_original_failure", config.MODEL_TEXT, "REFERENCE_MODE_ORIGINAL_FAILURE",
        None, 0, meta={
            "shot_id": shot_id,
            "original_failure_reason": ref_failure_reason,
            "rejection_count": len(rejection_details),
            "rejection_details": rejection_details[:10],
        })

    meta["reference_failure_logs"] = (meta.get("reference_failure_logs") or []) + [{
        "mode": video_modes.REFERENCE_IMAGE_MODE,
        "original_failure_reason": ref_failure_reason,
        "rejection_count": len(rejection_details),
        "rejection_details": rejection_details[:10],
        "prompt": prompt_text[:500],
    }]
    meta["reference_generation_complete"] = False
    meta["reference_static_ready"] = False
    meta["continuity_anchor_ready"] = False
    meta["reference_group_gate_passed"] = False
    meta["video_input_manifest_frozen"] = False
    meta["narrative_keyframe_missing"] = False
    meta["reference_gate_retry_exhausted"] = True
    meta["reference_images"] = []
    _set_version(
        version["id"],
        image_inputs=json.dumps(meta, ensure_ascii=False),
        prompt_text=prompt_text,
    )
    raise VideoInputRepairRequired(ref_failure_reason)


class _ContinuityWait(Exception):
    """Local inputs are progressing but one declared boundary is not ready."""

    def __init__(self, reason: str, *, reason_code: str = "WAITING_VIDEO_PLAN_DEPENDENCY"):
        super().__init__(reason)
        self.reason = reason
        self.reason_code = reason_code


def _image_dimensions(path: str) -> tuple[int, int]:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of", "json", path,
            ],
            capture_output=True, text=True, timeout=20, check=True,
        )
        stream = (json.loads(result.stdout or "{}").get("streams") or [{}])[0]
        return int(stream.get("width") or 0), int(stream.get("height") or 0)
    except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError, IndexError):
        return 0, 0


def _normalize_boundary_pair(
    first_path: str,
    last_path: str,
) -> tuple[str, str, tuple[int, int]]:
    """Normalize an aspect-compatible pair to one deterministic resolution."""
    sizes = {
        first_path: _image_dimensions(first_path),
        last_path: _image_dimensions(last_path),
    }
    if any(not all(size) for size in sizes.values()):
        raise VideoInputRepairRequired(
            "首尾帧尺寸不可识别："
            f"first={sizes[first_path]}, last={sizes[last_path]}"
        )
    first_size = sizes[first_path]
    last_size = sizes[last_path]
    cross_error = abs(
        first_size[0] * last_size[1] - last_size[0] * first_size[1]
    )
    cross_scale = max(
        first_size[0] * last_size[1],
        last_size[0] * first_size[1],
        1,
    )
    if cross_error / cross_scale > 0.005:
        raise VideoInputRepairRequired(
            "首尾帧宽高比不一致，禁止裁切后伪造边界合同："
            f"first={first_size}, last={last_size}"
        )
    target = min((first_size, last_size), key=lambda size: size[0] * size[1])
    for path, size in sizes.items():
        if size == target:
            continue
        source = Path(path)
        if not source.is_file():
            raise VideoInputRepairRequired(f"首尾帧文件不存在：{path}")
        with tempfile.NamedTemporaryFile(
            prefix=f".{source.stem}.normalized-",
            suffix=source.suffix or ".jpg",
            dir=source.parent,
            delete=False,
        ) as handle:
            normalized = Path(handle.name)
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error", "-i", str(source),
                    "-vf", f"scale={target[0]}:{target[1]}:flags=lanczos",
                    "-frames:v", "1", str(normalized),
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            )
            normalized.replace(source)
        except (OSError, subprocess.SubprocessError) as exc:
            normalized.unlink(missing_ok=True)
            raise VideoInputRepairRequired(
                f"首尾帧统一尺寸失败：{type(exc).__name__}: {exc}"
            ) from exc
    return first_path, last_path, target


def _load_boundary_asset(conn, shot_plan_id: str, role: str, fingerprint: str):
    row = conn.execute(
        """SELECT * FROM video_boundary_assets
           WHERE shot_plan_id=? AND role=? AND fingerprint=? AND qa_status='passed'
           ORDER BY created_at DESC LIMIT 1""",
        (shot_plan_id, role, fingerprint),
    ).fetchone()
    if row and row["path"] and Path(row["path"]).is_file():
        return row
    return None


def _persist_boundary_asset(
    conn,
    *,
    shot_plan,
    role: str,
    source: str,
    source_revision_id: str,
    source_shot_id: str | None,
    source_adopted_version_id: str | None,
    path: str,
    fingerprint: str,
    qa: dict[str, Any],
) -> None:
    raw = Path(path).read_bytes()
    width, height = _image_dimensions(path)
    conn.execute(
        """INSERT OR REPLACE INTO video_boundary_assets(
               id,episode_video_plan_id,shot_plan_id,shot_id,role,source,
               source_revision_id,source_shot_id,source_adopted_version_id,
               path,sha256,mime,width,height,qa_status,qa_json,fingerprint,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            new_id("vba"), shot_plan.episode_video_plan_id, shot_plan.shot_plan_id,
            shot_plan.shot_id, role, source, source_revision_id, source_shot_id,
            source_adopted_version_id, path, hashlib.sha256(raw).hexdigest(),
            "image/jpeg", width, height, "passed", json.dumps(qa, ensure_ascii=False),
            fingerprint, now(),
        ),
    )


def _resolve_current_execution_plan(
    conn,
    shot_id: str,
    meta: dict,
):
    """Rebind an equivalent sibling-replanned contract to the current plan."""
    from app.video_plan import active_plan_is_current, get_shot_plan

    current = get_shot_plan(shot_id, conn=conn)
    submitted_id = str(meta.get("shot_plan_id") or "")
    if current is None or not submitted_id:
        return None
    if current.shot_plan_id == submitted_id:
        return current
    if not active_plan_is_current(submitted_id, conn=conn):
        return None
    meta.setdefault("submitted_shot_plan_id", submitted_id)
    meta.setdefault(
        "submitted_episode_video_plan_id",
        meta.get("episode_video_plan_id"),
    )
    meta.update({
        "shot_plan_id": current.shot_plan_id,
        "episode_video_plan_id": current.episode_video_plan_id,
        "plan_revision": current.plan_revision,
        "source_storyboard_revision_id": current.source_storyboard_revision_id,
        "capability_snapshot_id": current.capability_snapshot_id,
        "input_revision_fingerprints": dict(current.input_revision_fingerprints),
        "planned_mode": current.mode.value,
        "equivalent_plan_rebound": True,
        "equivalent_plan_rebound_at": now(),
    })
    return current


async def _prepare_first_frame_mode_inputs(
    conn, job, version, shot, ep, meta: dict, prompt_text: str,
    *, lease_owner: str,
) -> tuple[dict, str]:
    """Use the immediately previous adopted video's real tail as the sole frame input."""
    from app.media_pipeline import stages as media_stages
    from app.media_pipeline.stage_state import set_pipeline_stage
    from app.video_plan import AssetSource

    shot_plan = _resolve_current_execution_plan(
        conn, str(job["shot_id"]), meta,
    )
    if shot_plan is None:
        raise VideoInputRepairRequired("首帧计划已过期，需要重新规划")
    requirements = list(shot_plan.required_assets)
    if len(requirements) != 1 or requirements[0].role != "first_frame":
        raise VideoInputRepairRequired("首帧计划必须且只能声明一个 first_frame")
    first_req = requirements[0]
    if first_req.source != AssetSource.PREVIOUS_ADOPTED_TAIL:
        raise VideoInputRepairRequired("首帧必须来自紧邻上一镜采用视频的真实尾帧")
    source_shot_id = first_req.source_shot_id or shot_plan.depends_on_shot_id
    if not source_shot_id or source_shot_id != shot_plan.depends_on_shot_id:
        raise VideoInputRepairRequired("首帧来源镜头与视频计划依赖不一致")

    previous = conn.execute(
        "SELECT * FROM shots WHERE id=? AND episode_id=?",
        (source_shot_id, job["episode_id"]),
    ).fetchone()
    source_contract = video_modes.previous_tail_source_contract(conn, previous)
    if not source_contract:
        raise _ContinuityWait("等待上一镜采用后提取真实尾帧")
    fingerprint = hashlib.sha256(json.dumps({
        "shot_plan_id": shot_plan.shot_plan_id,
        "role": "first_frame",
        "source": first_req.source.value,
        "continuity_source": source_contract,
        "policy": "previous_video_tail_first_frame_v1",
    }, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    cached = _load_boundary_asset(
        conn, shot_plan.shot_plan_id, "first_frame", fingerprint,
    )
    if cached:
        first_path = str(cached["path"])
    else:
        _assert_job_lease(job["id"], lease_owner)
        dest_dir = (
            config.PROJECTS_DIR / job["project_id"] / "episodes"
            / str(ep["episode_no"]) / "shots" / str(shot["shot_no"])
            / "boundaries"
        )
        asset = video_modes.previous_tail_reference_asset(
            conn, previous, dest_dir=dest_dir,
        )
        if not asset or not asset.path or not Path(asset.path).is_file():
            raise VideoInputRepairRequired("上一镜采用视频无法稳定抽取尾帧")
        first_path = str(asset.path)
        _persist_boundary_asset(
            conn,
            shot_plan=shot_plan,
            role="first_frame",
            source=first_req.source.value,
            source_revision_id=str(source_contract["adopted_version_id"]),
            source_shot_id=source_shot_id,
            source_adopted_version_id=str(source_contract["adopted_version_id"]),
            path=first_path,
            fingerprint=fingerprint,
            qa={
                "source_adopted_version_id": source_contract["adopted_version_id"],
                "extracted_from_previous_video": True,
            },
        )
        conn.commit()

    meta["first_frame_path"] = first_path
    meta["first_frame_source"] = AssetSource.PREVIOUS_ADOPTED_TAIL.value
    meta["first_frame_source_shot_id"] = source_shot_id
    meta["first_frame_fingerprint"] = fingerprint
    meta["upstream_adopted_video_revision"] = source_contract["adopted_version_id"]
    meta["reference_images"] = []
    meta.pop("last_frame_path", None)
    meta.pop("last_frame_url", None)
    meta.pop("video_input_url", None)
    meta["reference_generation_complete"] = True
    meta["video_input_manifest_frozen"] = True
    meta["plan_status"] = "ready"
    set_pipeline_stage(
        job["id"], media_stages.STAGE_VIDEO_READY,
        scheduler_lane=media_stages.LANE_VIDEO_READY,
        ready_at=now(),
        conn=conn,
    )
    _set_version(
        version["id"],
        image_inputs=json.dumps(meta, ensure_ascii=False),
        prompt_text=prompt_text,
    )
    conn.commit()
    return meta, prompt_text


async def _prepare_first_last_mode_inputs(
    conn, job, version, shot, ep, meta: dict, prompt_text: str,
    *, lease_owner: str,
) -> tuple[dict, str]:
    from app.schemas import Bible
    from app.video_plan import AssetSource
    from app.media_pipeline import stages as media_stages
    from app.media_pipeline.stage_state import set_pipeline_stage

    shot_plan = _resolve_current_execution_plan(
        conn, str(job["shot_id"]), meta,
    )
    if shot_plan is None:
        raise VideoInputRepairRequired("首尾帧计划已过期，需要重新规划")
    current_first = str(meta.get("first_frame_path") or "")
    current_last = str(meta.get("last_frame_path") or "")
    if current_first and current_last and Path(current_first).is_file() and Path(current_last).is_file():
        return meta, prompt_text

    project = conn.execute("SELECT * FROM projects WHERE id=?", (job["project_id"],)).fetchone()
    bible = Bible.model_validate(json.loads(project["bible_json"]))
    from app.portraits import bible_for_episode
    bible = bible_for_episode(job["project_id"], bible, ep["episode_no"])
    screenplay = None
    if _row_value(ep, "id") or _row_value(ep, "screenplay_json"):
        from app.production.screenplay_authority import resolve_downstream_screenplay

        screenplay = resolve_downstream_screenplay(
            job["episode_id"], conn=conn,
        ).screenplay
    shot_model = _load_shot_model(shot)
    from app.continuity import apply_shot_contract
    apply_shot_contract(shot_model, meta.get("shot_contract_json"))
    requirements = {item.role: item for item in shot_plan.required_assets}
    first_req = requirements.get("first_frame")
    last_req = requirements.get("last_frame")
    if not first_req or not last_req:
        raise ProviderError("首尾帧计划缺少 first_frame 或 last_frame 素材合同")
    plan_relations = getattr(shot_plan, "relations", None)
    boundary_prompt_contract = (
        meta.get("boundary_prompt_contract")
        if isinstance(meta.get("boundary_prompt_contract"), dict)
        else {}
    )
    relation_edit = str(
        boundary_prompt_contract.get("relation_edit")
        or getattr(plan_relations, "edit", "unknown")
        or "unknown"
    )
    relation_action = str(
        boundary_prompt_contract.get("relation_action")
        or getattr(plan_relations, "action", "unknown")
        or "unknown"
    )
    from app.multiview import keyframe_seed_paths, resolve_shot_asset_dependencies

    manifest = resolve_shot_asset_dependencies(
        project_id=job["project_id"],
        episode_no=ep["episode_no"],
        shot_id=job["shot_id"],
        shot=shot_model,
        scene_name=shot_model.scene_name or None,
        conn=conn,
        bible=bible,
        screenplay=screenplay,
    )
    boundary_seed_inputs = []
    for seed_path in keyframe_seed_paths(manifest):
        try:
            boundary_seed_inputs.append(hiagent.data_url_from_file(seed_path))
        except OSError:
            continue
    if not boundary_seed_inputs:
        from app.continuity import effective_characters_visible
        boundary_seed_inputs = video_modes._portrait_seed_inputs(
            bible,
            effective_characters_visible(shot_model),
            project_id=job["project_id"],
            episode_no=ep["episode_no"],
        )

    set_pipeline_stage(
        job["id"], media_stages.STAGE_REFERENCE_GENERATE,
        reason_code="PREFETCHING_STATIC_TAIL",
        reason_text="正在预生成可供下一镜复用的静态尾帧",
        conn=conn,
    )
    conn.commit()

    async def _resolve(
        role: str,
        requirement,
        description: str,
        index: int,
        *,
        pair_attempt: int,
        seed_inputs: list[str],
        pair_start_fingerprint: str | None = None,
    ) -> str:
        _assert_job_lease(job["id"], lease_owner)
        source_revision = str(requirement.asset_revision_id or shot_plan.source_storyboard_revision_id)
        upstream_version_id = None
        source_shot_id = requirement.source_shot_id or shot_plan.depends_on_shot_id
        upstream_static_asset = None
        fingerprint_material: dict[str, Any] = {
            "shot_plan_id": shot_plan.shot_plan_id,
            "role": role,
            "source": requirement.source.value,
            "source_revision": source_revision,
            "description": description,
            "boundary_contract": "shared_static_tail_v3",
            "generation_attempt": (
                pair_attempt
                if requirement.source == AssetSource.STATIC_BOUNDARY_ASSET
                else 1
            ),
        }
        if pair_start_fingerprint:
            fingerprint_material["pair_start_fingerprint"] = pair_start_fingerprint
        if requirement.source == AssetSource.PREVIOUS_ADOPTED_TAIL:
            previous = conn.execute(
                "SELECT * FROM shots WHERE id=? AND episode_id=?",
                (source_shot_id, job["episode_id"]),
            ).fetchone()
            upstream_version_id = previous["adopted_version_id"] if previous else None
            if not previous or not upstream_version_id:
                raise _ContinuityWait("等待上一镜采用后提取真实尾帧")
            fingerprint_material["upstream_adopted_version_id"] = upstream_version_id
        elif requirement.source == AssetSource.PREVIOUS_STATIC_TAIL:
            source_plan = conn.execute(
                """SELECT id FROM shot_video_generation_plans
                   WHERE episode_video_plan_id=? AND shot_id=?""",
                (shot_plan.episode_video_plan_id, source_shot_id),
            ).fetchone()
            if source_plan:
                upstream_static_asset = conn.execute(
                    """SELECT * FROM video_boundary_assets
                       WHERE episode_video_plan_id=? AND shot_plan_id=?
                         AND role='last_frame' AND qa_status='passed'
                       ORDER BY created_at DESC LIMIT 1""",
                    (shot_plan.episode_video_plan_id, source_plan["id"]),
                ).fetchone()
            if (
                not upstream_static_asset
                or not upstream_static_asset["path"]
                or not Path(upstream_static_asset["path"]).is_file()
            ):
                source_job = conn.execute(
                    """SELECT status FROM jobs
                       WHERE episode_id=? AND shot_id=? AND kind='video'
                       ORDER BY created_at DESC LIMIT 1""",
                    (job["episode_id"], source_shot_id),
                ).fetchone()
                if source_job and source_job["status"] in {
                    "failed", "waiting_human", "cancelled", "paused",
                }:
                    raise VideoInputRepairRequired(
                        "上一镜静态尾帧未生成成功，需要保持首尾帧模式修复该边界素材"
                    )
                raise _ContinuityWait(
                    "等待上一镜预生成静态尾帧",
                    reason_code="WAITING_STATIC_BOUNDARY_ASSET",
                )
            fingerprint_material.update({
                "upstream_boundary_fingerprint": upstream_static_asset["fingerprint"],
                "upstream_boundary_sha256": upstream_static_asset["sha256"],
            })
        fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_material, ensure_ascii=False, sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        cached = _load_boundary_asset(conn, shot_plan.shot_plan_id, role, fingerprint)
        if cached:
            return str(cached["path"])

        if requirement.source == AssetSource.PREVIOUS_ADOPTED_TAIL:
            dest_dir = (
                config.PROJECTS_DIR / job["project_id"] / "episodes"
                / str(ep["episode_no"]) / "shots" / str(shot["shot_no"]) / "boundaries"
            )
            asset = video_modes.previous_tail_reference_asset(
                conn, previous, dest_dir=dest_dir,
            )
            if not asset or not asset.path:
                raise VideoInputRepairRequired("上一镜采用视频无法稳定抽取尾帧")
            asset.qa = {**(asset.qa or {}), "source_adopted_version_id": upstream_version_id}
        elif requirement.source == AssetSource.PREVIOUS_STATIC_TAIL:
            source_path = Path(str(upstream_static_asset["path"]))
            dest_dir = (
                config.PROJECTS_DIR / job["project_id"] / "episodes"
                / str(ep["episode_no"]) / "shots" / str(shot["shot_no"])
                / "boundaries"
            )
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_path = dest_dir / (
                f"first-from-{source_shot_id}-"
                f"{str(upstream_static_asset['sha256'])[:12]}"
                f"{source_path.suffix or '.jpg'}"
            )
            if not dest_path.is_file():
                shutil.copy2(source_path, dest_path)
            asset = video_modes.ReferenceImageAsset(
                id=f"{shot_plan.shot_plan_id}:first_frame",
                url="",
                type="plot_key_frame",
                source="previous_static_tail",
                path=str(dest_path),
                qa={
                    "source_boundary_asset_id": upstream_static_asset["id"],
                    "source_boundary_fingerprint": upstream_static_asset["fingerprint"],
                    "shared_without_regeneration": True,
                },
            )
        elif requirement.source == AssetSource.STATIC_BOUNDARY_ASSET:
            if role == "last_frame":
                boundary_instruction = (
                    "STATIC TAIL PREFETCH: render only this shot's contracted final "
                    "state. This immutable image is also the next shot's exact first "
                    "frame when a next shot exists. Preserve every named identity, "
                    "outfit, environment, fixed landmark, screen direction, and scale "
                    "from the supplied character/scene truth anchors. Do not blend "
                    "endpoints, morph faces, merge people, teleport, hard-cut, or add "
                    "uncontracted people. The current shot's dynamic first frame may "
                    "not be available yet; do not wait for it or invent a start pose. "
                    f"Edit relation: {relation_edit}; "
                    f"action relation: {relation_action}."
                )
            else:
                boundary_instruction = (
                    "STATIC FIRST BOUNDARY: render only the contracted starting state "
                    "for this shot. Preserve every named identity, outfit, environment, "
                    "fixed landmark, screen direction, and scale from the supplied "
                    "character/scene truth anchors. Do not render the final action state."
                )
            asset = await video_modes._generate_one_reference(
                project_id=job["project_id"],
                episode_no=ep["episode_no"],
                shot=shot_model,
                bible=bible,
                ref_type="plot_key_frame",
                index=index + pair_attempt - 1,
                content_override=description,
                seed_inputs=seed_inputs,
                extra_instruction=boundary_instruction,
                skip_inline_qa=False,
                screenplay=screenplay,
            )
            if not asset.path or not Path(asset.path).is_file():
                raise VideoInputRepairRequired(f"{role} 边界帧生成后文件不可用")
            if not asset.selectedForSeedance:
                raise VideoInputRepairRequired(f"{role} 边界帧未通过生成前质量门禁")
        else:
            raise VideoInputRepairRequired(
                f"{role} 使用了不支持的素材来源：{requirement.source.value}"
            )
        qa = dict(asset.qa or {})
        _persist_boundary_asset(
            conn,
            shot_plan=shot_plan,
            role=role,
            source=requirement.source.value,
            source_revision_id=source_revision,
            source_shot_id=source_shot_id,
            source_adopted_version_id=upstream_version_id,
            path=str(asset.path),
            fingerprint=fingerprint,
            qa=qa,
        )
        conn.commit()
        return str(asset.path)

    # The tail is an independently frozen narrative boundary. Generate it before
    # resolving the dynamic first-frame dependency so the next shot can consume
    # it even while this shot is still waiting for an adopted/static upstream.
    tail_attempt_limit = max(1, min(3, int(shot_plan.max_attempts or 1)))
    tail_seed_inputs = list(dict.fromkeys(boundary_seed_inputs[:4]))
    last_path = ""
    first_size = (0, 0)
    last_repair_error = ""
    for tail_attempt in range(1, tail_attempt_limit + 1):
        try:
            last_path = await _resolve(
                "last_frame",
                last_req,
                shot_model.last_frame_desc,
                902,
                pair_attempt=tail_attempt,
                seed_inputs=tail_seed_inputs,
            )
        except VideoInputRepairRequired as exc:
            last_repair_error = str(exc)
            log_provider_call(
                "last_frame_same_mode_repair",
                config.MODEL_IMAGE,
                "REPAIRING",
                None,
                0,
                meta={
                    "shot_id": job["shot_id"],
                    "shot_plan_id": shot_plan.shot_plan_id,
                    "tail_attempt": tail_attempt,
                    "tail_attempt_limit": tail_attempt_limit,
                    "reason": last_repair_error,
                },
            )
            continue
        break
    else:
        raise VideoInputRepairRequired(
            f"{last_repair_error or '尾帧输入准备未通过'}；"
            f"已在 FIRST_LAST_FRAME_MODE 内修复 {tail_attempt_limit} 次，"
            "未更改生成模式"
        )

    meta["last_frame_path"] = last_path
    meta["boundary_tail_prefetched"] = True
    meta["boundary_tail_prefetched_at"] = now()
    meta["reference_generation_complete"] = False
    _set_version(
        version["id"],
        image_inputs=json.dumps(meta, ensure_ascii=False),
    )
    conn.commit()

    first_path = await _resolve(
        "first_frame",
        first_req,
        shot_model.first_frame_desc,
        901,
        pair_attempt=1,
        seed_inputs=boundary_seed_inputs,
    )
    first_bytes = Path(first_path).read_bytes()
    first_fingerprint = hashlib.sha256(first_bytes).hexdigest()

    while True:
        try:
            first_path, last_path, first_size = _normalize_boundary_pair(
                first_path, last_path,
            )
            break
        except VideoInputRepairRequired as exc:
            if tail_attempt >= tail_attempt_limit:
                raise
            conn.execute(
                """UPDATE video_boundary_assets
                      SET qa_status='failed'
                    WHERE shot_plan_id=? AND role='last_frame' AND path=?""",
                (shot_plan.shot_plan_id, last_path),
            )
            conn.commit()
            tail_attempt += 1
            log_provider_call(
                "last_frame_dimension_repair",
                config.MODEL_IMAGE,
                "REPAIRING",
                None,
                0,
                meta={
                    "shot_id": job["shot_id"],
                    "shot_plan_id": shot_plan.shot_plan_id,
                    "tail_attempt": tail_attempt,
                    "tail_attempt_limit": tail_attempt_limit,
                    "reason": str(exc),
                },
            )
            last_path = await _resolve(
                "last_frame",
                last_req,
                shot_model.last_frame_desc,
                902,
                pair_attempt=tail_attempt,
                seed_inputs=tail_seed_inputs,
            )
    for role, path in (
        ("first_frame", first_path),
        ("last_frame", last_path),
    ):
        raw = Path(path).read_bytes()
        conn.execute(
            """UPDATE video_boundary_assets
                  SET path=?,sha256=?,width=?,height=?
                WHERE shot_plan_id=? AND role=? AND path=?""",
            (
                path,
                hashlib.sha256(raw).hexdigest(),
                first_size[0],
                first_size[1],
                shot_plan.shot_plan_id,
                role,
                path,
            ),
        )
    boundary_contract = {
        "status": "deterministic_checks_only",
        "semantic_pair_review_performed": False,
        "first_frame_source": first_req.source.value,
        "last_frame_source": last_req.source.value,
        "shared_boundary_contract": "shared_static_tail_v3",
        "camera_bridge_contract": "continuous_endpoint_bridge_v2",
        "tail_conditioned_on_first_frame": False,
        "tail_prefetched_before_first_frame": True,
        "first_frame_sha256": first_fingerprint,
        "relation_edit": relation_edit,
        "relation_action": relation_action,
    }
    meta["first_frame_path"] = first_path
    meta["last_frame_path"] = last_path
    meta["reference_images"] = []
    meta.pop("video_input_url", None)
    meta["boundary_frame_dimensions"] = list(first_size)
    meta["boundary_pair_qa"] = boundary_contract
    meta["camera_bridge_contract"] = "continuous_endpoint_bridge_v2"
    meta["reference_generation_complete"] = True
    meta["video_input_manifest_frozen"] = True
    meta["plan_status"] = "ready"
    set_pipeline_stage(
        job["id"], media_stages.STAGE_VIDEO_READY,
        scheduler_lane=media_stages.LANE_VIDEO_READY, ready_at=now(), conn=conn,
    )
    _set_version(
        version["id"], image_inputs=json.dumps(meta, ensure_ascii=False),
        prompt_text=prompt_text,
    )
    conn.commit()
    return meta, prompt_text


async def _prepare_video_input_mode(
    conn, job, version, shot, ep, meta: dict, prompt_text: str,
    *, lease_owner: str,
) -> tuple[dict, str]:
    from app.video_plan import (
        ProviderMediaPublicationService,
        VideoGenerationMode,
        capability_allows,
        current_capability_snapshot,
    )
    from app.media_pipeline import stages as media_stages
    from app.media_pipeline.stage_state import set_pipeline_stage

    shot_plan = _resolve_current_execution_plan(
        conn, str(job["shot_id"]), meta,
    )
    if shot_plan is None:
        raise VideoInputRepairRequired("视频输入计划已过期，需要重新规划")
    snapshot = current_capability_snapshot(
        provider=None, model=None, conn=conn,
    )
    if snapshot.id != shot_plan.capability_snapshot_id or not capability_allows(
        snapshot, VideoGenerationMode.VIDEO_INPUT_MODE, shot_plan.video_input_intent,
    ):
        raise VideoInputRepairRequired("当前能力快照未准入该视频输入意图")
    upstream_id = shot_plan.depends_on_shot_id
    previous = conn.execute(
        "SELECT * FROM shots WHERE id=? AND episode_id=?",
        (upstream_id, job["episode_id"]),
    ).fetchone()
    adopted_id = previous["adopted_version_id"] if previous else None
    if not previous or not adopted_id:
        raise _ContinuityWait("等待上一镜采用后绑定真实参考视频")
    adopted = conn.execute(
        """SELECT * FROM shot_versions
           WHERE id=? AND shot_id=? AND status='succeeded' AND video_path IS NOT NULL""",
        (adopted_id, upstream_id),
    ).fetchone()
    if not adopted or not Path(adopted["video_path"]).is_file():
        raise _ContinuityWait("上一镜采用视频尚不可读取")

    existing = conn.execute(
        """SELECT * FROM provider_media_publications
           WHERE source_revision_id=? AND status='ready' AND url_expires_at>?
           ORDER BY created_at DESC LIMIT 1""",
        (adopted_id, now() + 1800),
    ).fetchone()
    if existing:
        publication = {
            "id": existing["id"],
            "published_url": existing["published_url"],
            "sha256": existing["sha256"],
            "url_expires_at": existing["url_expires_at"],
        }
    else:
        try:
            adopted_meta = json.loads(adopted["image_inputs"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            adopted_meta = {}
        source_url = str(adopted_meta.get("provider_video_source_url") or "")
        source_expiry = float(
            adopted_meta.get("provider_video_source_url_expires_at") or 0
        )
        publication = None
        if source_url and source_expiry > now() + 1800:
            try:
                publication = await ProviderMediaPublicationService().publish(
                    source_revision_id=adopted_id,
                    source_url=source_url,
                    expires_at=source_expiry,
                    conn=conn,
                )
            except Exception as exc:  # noqa: BLE001 - expired source falls back to owned storage
                meta["provider_source_url_reuse_error"] = (
                    f"{type(exc).__name__}: {exc}"
                )[:500]
        if publication is None:
            publication = await ProviderMediaPublicationService().publish(
                source_revision_id=adopted_id,
                local_path=adopted["video_path"],
                conn=conn,
            )
    _assert_job_lease(job["id"], lease_owner)
    meta["video_input_url"] = publication["published_url"]
    meta["provider_media_publication_id"] = publication["id"]
    meta["upstream_adopted_video_revision"] = adopted_id
    meta["video_input_fingerprint"] = publication["sha256"]
    meta["reference_images"] = []
    meta.pop("first_frame_path", None)
    meta.pop("last_frame_path", None)
    meta["reference_generation_complete"] = True
    meta["video_input_manifest_frozen"] = True
    meta["plan_status"] = "ready"
    set_pipeline_stage(
        job["id"], media_stages.STAGE_VIDEO_READY,
        scheduler_lane=media_stages.LANE_VIDEO_READY, ready_at=now(), conn=conn,
    )
    _set_version(
        version["id"], image_inputs=json.dumps(meta, ensure_ascii=False),
        prompt_text=prompt_text,
    )
    conn.commit()
    return meta, prompt_text


async def _ensure_ai_video_prompt(
    conn,
    job,
    version,
    shot,
    ep,
    meta: dict,
    prompt_text: str,
) -> tuple[dict, str]:
    """Generate the creative provider prompt once, before preparing video inputs."""
    conn = conn or get_conn()
    if not meta.get("ai_video_prompt_required"):
        return meta, prompt_text

    from app.video_prompt_ai import (
        AI_VIDEO_PROMPT_CONTRACT_VERSION,
        generate_ai_video_prompt,
    )
    from app.video_prompt_profiles import resolve_video_prompt_profile

    target_provider = hiagent.active_provider("video")
    target_model = hiagent.active_model("video", target_provider)
    target_profile = resolve_video_prompt_profile(
        provider=target_provider,
        model=target_model,
    )

    if (
        meta.get("ai_video_prompt_contract_version")
        == AI_VIDEO_PROMPT_CONTRACT_VERSION
        and meta.get("ai_video_prompt_profile_id") == target_profile.profile_id
        and meta.get("ai_video_prompt_profile_version") == target_profile.version
        and meta.get("ai_video_prompt_target_provider") == target_provider
        and meta.get("ai_video_prompt_target_model") == target_model
        and isinstance(meta.get("ai_video_prompt_draft"), dict)
        and str(meta.get("ai_video_prompt_base") or "").strip()
    ):
        return meta, prompt_text

    from app.media_pipeline import stages as media_stages
    from app.media_pipeline.stage_state import set_pipeline_stage
    from app.schemas import Bible

    set_pipeline_stage(
        job["id"],
        media_stages.STAGE_VIDEO_PROMPT,
        conn=conn,
    )
    conn.commit()

    project = conn.execute(
        "SELECT * FROM projects WHERE id=?",
        (job["project_id"],),
    ).fetchone()
    bible = Bible.model_validate(json.loads(project["bible_json"]))
    from app.portraits import bible_for_episode

    bible = bible_for_episode(job["project_id"], bible, ep["episode_no"])
    shot_model = _load_shot_model(shot)
    from app.continuity import apply_shot_contract

    apply_shot_contract(shot_model, meta.get("shot_contract_json"))
    continuity_contract = str(
        meta.get("continuity_contract_prompt") or prompt_text
    ).strip()
    prompt, draft = await generate_ai_video_prompt(
        shot=shot_model,
        bible=bible,
        continuity_contract=continuity_contract,
        video_generation_mode=str(
            meta.get("planned_mode")
            or meta.get("mode")
            or video_modes.REFERENCE_IMAGE_MODE
        ),
        operation_scope=str(version["id"]),
        target_provider=target_provider,
        target_model=target_model,
        user_instruction=str(meta.get("prompt_user_instruction") or ""),
        critique=[
            str(item).strip()
            for item in (meta.get("prompt_critique") or [])
            if str(item).strip()
        ],
    )
    meta["continuity_contract_prompt"] = continuity_contract
    meta["ai_video_prompt_contract_version"] = (
        AI_VIDEO_PROMPT_CONTRACT_VERSION
    )
    meta["ai_video_prompt_profile_id"] = target_profile.profile_id
    meta["ai_video_prompt_profile_version"] = target_profile.version
    meta["ai_video_prompt_target_provider"] = target_provider
    meta["ai_video_prompt_target_model"] = target_model
    meta["ai_video_prompt_draft"] = draft.model_dump(mode="json")
    meta["ai_video_prompt_base"] = prompt
    meta["ai_video_prompt_generated_at"] = now()
    bible_character_names = {item.name for item in bible.characters}
    meta["required_reference_characters"] = [
        name
        for name in draft.visible_characters
        if name in bible_character_names
    ]
    if draft.interaction_kind == "person_person_contact":
        meta["required_interaction_reference_characters"] = [
            name
            for name in draft.interaction_participants
            if name in bible_character_names
        ]
    else:
        meta.pop("required_interaction_reference_characters", None)
    _set_version(
        version["id"],
        image_inputs=json.dumps(meta, ensure_ascii=False),
        prompt_text=prompt,
    )
    conn.commit()
    return meta, prompt


async def _prepare_planned_mode_inputs(
    conn, job, version, shot, ep, meta: dict, prompt_text: str,
    *, lease_owner: str,
) -> tuple[dict, str]:
    conn = conn or get_conn()
    mode = meta.get("mode") or video_modes.REFERENCE_IMAGE_MODE
    # Reference/keyframe generation, boundary-frame generation, and provider
    # media publication can all incur external work before the final video
    # submit.  One mode-neutral authority fence must therefore run before mode
    # dispatch, not merely before create_video_task.
    selected_plan = await _assert_video_provider_submission_authority_async(
        conn=conn,
        job=job,
        meta=meta,
        actual_mode=str(mode),
        write_point="planned_mode_input_prepare",
    )
    if (
        selected_plan is not None
        and selected_plan.shot_plan_id != meta.get("shot_plan_id")
    ):
        # A fallback/local replan publishes a new episode revision. Unchanged
        # sibling contracts remain executable, but every persisted identity
        # must be rebound before preparing assets or recording attempts.
        meta["submitted_shot_plan_id"] = meta.get("shot_plan_id")
        meta["submitted_episode_video_plan_id"] = meta.get(
            "episode_video_plan_id"
        )
        meta.update({
            "shot_plan_id": selected_plan.shot_plan_id,
            "episode_video_plan_id": selected_plan.episode_video_plan_id,
            "plan_revision": selected_plan.plan_revision,
            "source_storyboard_revision_id": (
                selected_plan.source_storyboard_revision_id
            ),
            "capability_snapshot_id": selected_plan.capability_snapshot_id,
            "input_revision_fingerprints": dict(
                selected_plan.input_revision_fingerprints
            ),
            "planned_mode": selected_plan.mode.value,
            "actual_mode": selected_plan.mode.value,
            "video_input_intent": (
                selected_plan.video_input_intent.value
                if selected_plan.video_input_intent is not None
                else None
            ),
            "depends_on_shot_id": selected_plan.depends_on_shot_id,
            "stale_plan_recovered": True,
            "stale_plan_recovered_at": now(),
        })
        _set_version(
            version["id"],
            image_inputs=json.dumps(meta, ensure_ascii=False),
        )
    if mode == video_modes.REFERENCE_IMAGE_MODE:
        return await _prepare_reference_mode_inputs(
            conn, job, version, shot, ep, meta, prompt_text,
            lease_owner=lease_owner,
        )
    if mode == video_modes.FIRST_FRAME_MODE:
        return await _prepare_first_frame_mode_inputs(
            conn, job, version, shot, ep, meta, prompt_text,
            lease_owner=lease_owner,
        )
    if mode == video_modes.FIRST_LAST_FRAME_MODE:
        return await _prepare_first_last_mode_inputs(
            conn, job, version, shot, ep, meta, prompt_text,
            lease_owner=lease_owner,
        )
    if mode == video_modes.VIDEO_INPUT_MODE:
        return await _prepare_video_input_mode(
            conn, job, version, shot, ep, meta, prompt_text,
            lease_owner=lease_owner,
        )
    raise ProviderError(f"未知视频生成模式：{mode}")


async def _run_job(job_id: str, *, lease_owner: str | None = None) -> None:
    from app.media_pipeline.stage_state import set_pipeline_stage

    # Workers are spawned during application recovery.  Give the lifespan and
    # HTTP server a scheduling boundary before any JSON decoding, authority
    # verification, or reference preparation below; otherwise a recovered
    # cohort can monopolize the event loop before the socket starts listening.
    await asyncio.sleep(1.0)
    conn = get_conn()
    owner = lease_owner or f"direct-{id(asyncio.current_task())}"
    if lease_owner is None:
        if not await _claim_job_without_blocking_loop(
            job_id,
            owner,
            lease_seconds=180.0,
        ):
            return
        run_row = conn.execute(
            "SELECT run_id, step_run_id FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if run_row:
            mark_media_job_state(run_row["run_id"], run_row["step_run_id"], "running")
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job or job["status"] != "running" or job["lease_owner"] != owner:
        return
    # 这个 worker task 会在同一个 asyncio Task/Context 里连续 await 很多个 job；
    # 不重新绑定的话，provider_calls（尤其 video_create/video_poll）会带着上一个
    # job 的 trace，或者干脆一直是启动时的空 trace，链路树永远关联不到它们。
    # 详见 set_worker_trace 的说明：这里必须在本 job 的第一次供应商调用之前、
    # 每个 job 都调用一次，即使 run_id 为空也要显式清空上一个 job 留下的痕迹。
    set_worker_trace(job["run_id"], job["step_run_id"])
    if job["kind"] != "video":
        # 旧版关键帧 job 可能在升级前已持久化。它们不再恢复或执行，避免继续消耗图片额度，
        # 同时清除造成前端长期显示“生成中”的遗留状态。
        conn.execute("UPDATE shots SET scene_status='none' WHERE id=?", (job["shot_id"],))
        conn.commit()
        if _set_job(
            job["id"], "cancelled", "关键帧功能已下线；请从参考图视频入口重新生成",
            lease_owner=owner,
        ):
            media_scheduler.settle_budget(job["id"], 0.0, success=False)
        return
    version = conn.execute("SELECT * FROM shot_versions WHERE id=?", (job["version_id"],)).fetchone()
    shot = conn.execute("SELECT * FROM shots WHERE id=?", (job["shot_id"],)).fetchone()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (job["episode_id"],)).fetchone()

    meta = json.loads(version["image_inputs"] or "{}")
    result_adoptable = bool(
        job["video_slot_active"] and job["provider_result_adoptable"]
    )
    provider_recovery_only = bool(
        job["provider_poll_required"] and not result_adoptable
    )
    task_id = version["provider_task_id"]

    started = time.time()
    try:
        if not provider_recovery_only:
            await _assert_review_dependency_fence_async(
                job, version["id"], "worker_start",
            )
        provider_operation_id = (
            _row_value(job, "provider_operation_id")
            or f"video-create-{version['id']}"
        )
        recovered_at = None
        if not task_id:
            recovered = _recover_paid_video_task(conn, provider_operation_id)
            if recovered:
                task_id, recovered_at = recovered
        _assert_provider_create_resolved(job, task_id)
        provider_submitted_at = (
            recovered_at
            if recovered_at is not None
            else (
                _provider_submitted_at(
                    conn,
                    job,
                    task_id,
                    lease_owner=owner,
                )
                if task_id
                else None
            )
        )
        result = None
        if task_id:
            await _commit_provider_acceptance(
                conn,
                job_id=job_id,
                version_id=version["id"],
                owner=owner,
                operation_id=provider_operation_id,
                task_id=task_id,
                submitted_at=provider_submitted_at,
            )
        prompt_text = version["prompt_text"]
        if not provider_recovery_only:
            shot_model_for_prompt = _load_shot_model(shot)
            # 分镜台 2.0.0（app.production.storyboard_pack）行：prompt_text 已由
            # 模型直接产出并原样持久化（见该模块 persist_storyboard_pack 的文
            # 档），必须逐字送达供应商。ensure_source_excerpt_in_prompt 末尾会
            # 跑 sanitize_seedance_prompt——这类段落的 prompt_text 不含旧架构的
            # "[...]" 分段标记，会落进它的兜底分支 ``re.sub(r"\s+", " ", body)``，
            # 把模型写的镜头换行全部压成空格，等于在最后一公里悄悄改写了模型
            # 产出（实测复现：EP1 第 2 段入队时 858 字符、四行分镜头文本，经这
            # 一步变成 1143 字符的单行文本）。这道防线本身是为旧架构"一行 = 一
            # 个连续镜头"设计的原文重合擦除，对这类一段 = 3-4 镜的自由文本不适
            # 用也不必要，跳过。``getattr`` 防的是测试把 ``_load_shot_model``
            # 换成不带这个字段的替身对象。
            is_storyboard_pack_shot = (
                getattr(shot_model_for_prompt, "storyboard_pack_segment", None)
                is not None
            )
            if not is_storyboard_pack_shot:
                prompt_text = ensure_source_excerpt_in_prompt(
                    prompt_text,
                    shot_model_for_prompt,
                )
                if prompt_text != version["prompt_text"]:
                    _set_version(version["id"], prompt_text=prompt_text)
        try:
            if not task_id:
                operation_conn = _connection_for_heartbeat_operation(conn)
                await _assert_video_provider_submission_authority_async(
                    conn=conn,
                    job=job,
                    meta=meta,
                    actual_mode=str(
                        meta.get("planned_mode")
                        or meta.get("mode")
                        or video_modes.REFERENCE_IMAGE_MODE
                    ),
                    write_point="video_prompt_generate",
                )
                meta, prompt_text = await _await_with_job_lease_heartbeat(
                    _ensure_ai_video_prompt(
                        operation_conn, job, version, shot, ep, meta, prompt_text,
                    ),
                    job_id=job_id,
                    owner=owner,
                )
                meta, prompt_text = await _await_with_job_lease_heartbeat(
                    _prepare_planned_mode_inputs(
                        operation_conn, job, version, shot, ep, meta, prompt_text,
                        lease_owner=owner,
                    ),
                    job_id=job_id,
                    owner=owner,
                )
        except _ContinuityWait as wait_exc:
            wait = 15.0
            note = wait_exc.reason
            from app.media_pipeline import stages as media_stages
            set_pipeline_stage(
                job_id,
                (
                    media_stages.STAGE_WAITING_DEPENDENCY
                    if meta.get("shot_plan_id")
                    else media_stages.STAGE_WAITING_CONTINUITY
                ),
                reason_code=(
                    wait_exc.reason_code
                    if meta.get("shot_plan_id")
                    else "WAITING_CONTINUITY_ANCHOR"
                ),
                reason_text=note,
                conn=conn,
            )
            conn.execute(
                """UPDATE jobs SET status='queued', error=?, next_retry_at=?,
                          lease_owner=NULL, lease_expires_at=NULL, updated_at=?
                   WHERE id=? AND lease_owner=?""",
                (note, now() + wait, now(), job_id, owner),
            )
            conn.execute(
                "UPDATE budget_reservations SET status='reserved' WHERE job_id=? AND status='running'",
                (job_id,),
            )
            conn.commit()
            task = asyncio.get_running_loop().create_task(_requeue_after(job_id, wait))
            _retry_tasks.add(task)
            task.add_done_callback(_retry_tasks.discard)
            return
        _assert_job_lease(job_id, owner)
        if not provider_recovery_only:
            await _assert_review_dependency_fence_async(
                job, version["id"], "provider_input_adoption",
            )

        # 连续镜调度级依赖：无可用尾帧时不得提交 Seedance
        if job["after_shot_id"] and not task_id:
            from app.media_pipeline.scheduler import continuity_anchor_ready
            from app.media_pipeline import stages as media_stages
            ready, reason = continuity_anchor_ready(
                conn,
                job["after_shot_id"],
                require_adopted=bool(meta.get("shot_plan_id")),
            )
            if not ready:
                wait = 15.0
                note = reason or "等待上一镜连续锚点"
                status = "waiting_human" if "人工" in note else "queued"
                set_pipeline_stage(
                    job_id,
                    (
                        media_stages.STAGE_WAITING_HUMAN
                        if status == "waiting_human"
                        else (
                            media_stages.STAGE_WAITING_DEPENDENCY
                            if meta.get("shot_plan_id")
                            else media_stages.STAGE_WAITING_CONTINUITY
                        )
                    ),
                    reason_code=(
                        "WAITING_VIDEO_PLAN_DEPENDENCY"
                        if meta.get("shot_plan_id")
                        else "WAITING_CONTINUITY_ANCHOR"
                    ),
                    reason_text=note,
                    conn=conn,
                )
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
                if status == "queued":
                    task = asyncio.get_running_loop().create_task(_requeue_after(job_id, wait))
                    _retry_tasks.add(task)
                    task.add_done_callback(_retry_tasks.discard)
                return

        # 视频提交配额：首轮优先，重抽限额
        if not task_id:
            from app.media_pipeline.scheduler import can_admit_video_submit
            from app.media_pipeline import stages as media_stages
            is_retake = int(meta.get("auto_retake_count") or 0) > 0
            ok, reason = can_admit_video_submit(
                episode_id=job["episode_id"], project_id=job["project_id"], is_auto_retake=is_retake,
            )
            if not ok:
                wait = 20.0
                set_pipeline_stage(
                    job_id, media_stages.STAGE_WAITING_VIDEO_SLOT,
                    reason_code="EPISODE_VIDEO_INFLIGHT_FULL",
                    reason_text=reason or "等待视频槽位",
                    conn=conn,
                )
                conn.execute(
                    """UPDATE jobs SET status='queued', error=?, next_retry_at=?,
                              lease_owner=NULL, lease_expires_at=NULL, updated_at=?
                       WHERE id=? AND lease_owner=?""",
                    (reason or "等待视频槽位", now() + wait, now(), job_id, owner),
                )
                conn.execute(
                    "UPDATE budget_reservations SET status='reserved' WHERE job_id=? AND status='running'",
                    (job_id,),
                )
                conn.commit()
                task = asyncio.get_running_loop().create_task(_requeue_after(job_id, wait))
                _retry_tasks.add(task)
                task.add_done_callback(_retry_tasks.discard)
                return

        image_inputs: list[tuple[str, str]] | None = None
        video_inputs: list[tuple[str, str]] | None = None

        while True:
            if not task_id:  # 重启恢复时可能已有 task_id，直接续轮询
                _assert_job_lease(job_id, owner)
                if image_inputs is None:
                    image_inputs = _video_image_inputs_from_meta(meta)
                    video_inputs = video_modes.build_seedance_video_inputs(meta)
                    actual_mode = str(meta.get("mode") or video_modes.REFERENCE_IMAGE_MODE)
                    meta["actual_mode"] = actual_mode
                    if meta.get("mode") == video_modes.REFERENCE_IMAGE_MODE:
                        meta["reference_image_used"] = bool(image_inputs)
                        meta["first_frame_used"] = False
                        meta["last_frame_used"] = False
                        meta["reference_video_used"] = False
                    elif meta.get("mode") == video_modes.FIRST_FRAME_MODE:
                        meta["reference_image_used"] = False
                        meta["first_frame_used"] = any(
                            role == "first_frame" for _, role in image_inputs
                        )
                        meta["last_frame_used"] = False
                        meta["reference_video_used"] = False
                    elif meta.get("mode") == video_modes.FIRST_LAST_FRAME_MODE:
                        meta["reference_image_used"] = False
                        meta["first_frame_used"] = any(
                            role == "first_frame" for _, role in image_inputs
                        )
                        meta["last_frame_used"] = any(role == "last_frame" for _, role in image_inputs)
                        meta["reference_video_used"] = False
                    else:
                        meta["reference_image_used"] = False
                        meta["first_frame_used"] = False
                        meta["last_frame_used"] = False
                        meta["reference_video_used"] = bool(video_inputs)
                    _set_version(version["id"], image_inputs=json.dumps(meta, ensure_ascii=False))
                try:
                    from app.media_pipeline import stages as media_stages
                    set_pipeline_stage(job_id, media_stages.STAGE_VIDEO_SUBMITTING, conn=conn)
                    submitting = conn.execute(
                        "UPDATE jobs SET provider_operation_id=?, provider_create_state='submitting', "
                        "updated_at=? WHERE id=? AND status='running' AND lease_owner=? "
                        "AND cancellation_requested=0",
                        (provider_operation_id, now(), job_id, owner),
                    )
                    if submitting.rowcount != 1:
                        conn.rollback()
                        raise LeaseLost(f"video submit lost lease: {job_id} / {owner}")
                    conn.commit()
                    from app.media_pipeline.concurrency import (
                        report_congestion, report_healthy, semaphore_for,
                    )
                    async with semaphore_for(media_stages.RESOURCE_VIDEO_SUBMIT):
                        _assert_job_lease(job_id, owner)
                        await _assert_review_dependency_fence_async(
                            job, version["id"], "provider_submit",
                        )
                        await _assert_video_provider_submission_authority_async(
                            conn=conn,
                            job=job,
                            meta=meta,
                            actual_mode=actual_mode,
                            write_point="provider_non_cancellable",
                        )
                        from app.media_pipeline.scheduler import claim_video_submit_slot

                        slot_claimed, slot_reason = claim_video_submit_slot(
                            job_id=job_id,
                            lease_owner=owner,
                            episode_id=str(job["episode_id"]),
                            project_id=str(job["project_id"]),
                            version_id=str(version["id"]),
                            operation_id=provider_operation_id,
                            amount_cny=shot_cost_cny(int(shot["duration_s"] or 0)),
                            is_auto_retake=int(meta.get("auto_retake_count") or 0) > 0,
                            conn=conn,
                        )
                        if not slot_claimed and slot_reason == "VIDEO_BUDGET_NOT_AUTHORIZED":
                            raise VideoBudgetAuthorizationError(
                                "本集缺少有效的视频费用授权，或本次供应商视频调用将超过"
                                "用户已批准的费用上限；任务已在付费调用前暂停"
                            )
                        if not slot_claimed:
                            raise VideoInflightAdmissionDeferred(
                                slot_reason or "等待视频槽位"
                            )
                        try:
                            try:
                                await _assert_video_provider_submission_authority_async(
                                    conn=conn,
                                    job=job,
                                    meta=meta,
                                    actual_mode=actual_mode,
                                    write_point="provider_create",
                                )
                            except BaseException:
                                # Provider create has not started. Every fence,
                                # cancellation and local failure must atomically
                                # release both the slot and payable budget claim.
                                _release_pre_call_video_claim(
                                    conn,
                                    job_id=job_id,
                                    owner=owner,
                                    operation_id=provider_operation_id,
                                )
                                raise
                            # From this line onward the transport may have sent
                            # the request. Unknown outcomes retain the durable
                            # claim and require explicit reconciliation.
                            task_id = await hiagent.create_video_task(
                                prompt_text,
                                image_urls=image_inputs,
                                video_urls=video_inputs,
                                return_last_frame=False,
                                call_meta={
                                    "asset_kind": "video",
                                    "planned_mode": meta.get("planned_mode"),
                                    "actual_mode": meta.get("actual_mode"),
                                    "video_input_intent": meta.get("video_input_intent"),
                                    "shot_plan_id": meta.get("shot_plan_id"),
                                    "capability_snapshot_id": meta.get("capability_snapshot_id"),
                                    "episode_id": ep["id"],
                                    "episode_no": ep["episode_no"],
                                    "shot_id": shot["id"],
                                    "shot_no": shot["shot_no"],
                                    "duration_s": shot["duration_s"],
                                    "version_id": version["id"],
                                    "version_no": version["version_no"],
                                    "operation_id": provider_operation_id,
                                })
                            await _commit_provider_acceptance(
                                conn,
                                job_id=job_id,
                                version_id=version["id"],
                                owner=owner,
                                operation_id=provider_operation_id,
                                task_id=task_id,
                            )
                            report_healthy(media_stages.RESOURCE_VIDEO_SUBMIT)
                        except ProviderError as submit_exc:
                            if submit_exc.retryable:
                                report_congestion(media_stages.RESOURCE_VIDEO_SUBMIT, reason="submit")
                            raise
                    _assert_job_lease(job_id, owner)
                except ProviderError as exc:
                    _assert_job_lease(job_id, owner)
                    create_outcome_unknown = _provider_create_outcome_unknown(exc)
                    create_state = (
                        "unknown" if create_outcome_unknown else "not_started"
                    )
                    changed = conn.execute(
                        """UPDATE jobs
                              SET provider_create_state=?,provider_non_cancellable=?,
                                  updated_at=?
                            WHERE id=? AND status='running' AND lease_owner=?
                              AND cancellation_requested=0""",
                        (
                            create_state,
                            int(create_outcome_unknown),
                            now(),
                            job_id,
                            owner,
                        ),
                    )
                    if changed.rowcount != 1:
                        conn.rollback()
                        raise LeaseLost(
                            f"video submit error lost lease: {job_id} / {owner}"
                        )
                    if not create_outcome_unknown:
                        released_at = now()
                        conn.execute(
                            """UPDATE provider_video_budget_claims
                                  SET status='released',updated_at=?,released_at=?
                                WHERE operation_id=? AND job_id=?""",
                            (
                                released_at,
                                released_at,
                                provider_operation_id,
                                job_id,
                            ),
                        )
                    conn.commit()
                    if create_outcome_unknown:
                        raise ProviderCreateUnresolved(
                            "[VIDEO_PROVIDER_CREATE_UNRESOLVED] Seedance create "
                            "结果不确定且本地没有 task id，已禁止自动重复 create；"
                            f"请在页面核对供应商任务（operation_id={provider_operation_id}, "
                            f"delivery_state={exc.delivery_state}, "
                            f"replay_safe={exc.replay_safe}, "
                            "requires_explicit_retry="
                            f"{exc.requires_explicit_retry}）"
                        ) from exc
                    raise
                if meta.get("shot_plan_id"):
                    from app.video_plan import (
                        VideoGenerationMode,
                        get_shot_plan,
                        record_mode_attempt,
                    )
                    active_shot_plan = get_shot_plan(job["shot_id"], conn=conn)
                    if (
                        not active_shot_plan
                        or active_shot_plan.shot_plan_id != meta.get("shot_plan_id")
                    ):
                        raise VideoPlanStaleFence("供应商接单后计划已失效，结果不得自动采用")
                    record_mode_attempt(
                        version_id=version["id"],
                        shot_plan=active_shot_plan,
                        actual_mode=VideoGenerationMode(meta["actual_mode"]),
                        status="provider_running",
                        provider_task_id=task_id,
                        conn=conn,
                    )
                try:
                    from app.media_pipeline import stages as media_stages
                    set_pipeline_stage(job_id, media_stages.STAGE_VIDEO_GENERATING, conn=conn)
                except Exception:  # noqa: BLE001
                    pass
                conn.commit()
                provider_submitted_at = conn.execute(
                    "SELECT provider_submitted_at FROM jobs WHERE id=?", (job_id,)
                ).fetchone()["provider_submitted_at"]

            # Phase 1：单次查询后立即释放 worker；供应商仍在跑则写入 waiting_provider。
            # 不再用 15 分钟连续占槽窗口（VIDEO_POLL_BUDGET 已置 0）。
            state = conn.execute(
                "SELECT cancellation_requested FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if state and state["cancellation_requested"]:
                media_scheduler.settle_budget(job_id, 0.0, success=False)
                return
            _assert_job_lease(job_id, owner)
            from app.media_pipeline.concurrency import (
                report_congestion, report_healthy, semaphore_for,
            )
            from app.media_pipeline import stages as media_stages
            async with semaphore_for(media_stages.RESOURCE_VIDEO_POLL):
                if not provider_recovery_only:
                    await _assert_review_dependency_fence_async(
                        job, version["id"], "provider_poll",
                    )
                try:
                    result = await hiagent.poll_video_task(
                        task_id,
                        call_meta={
                            "asset_kind": "video",
                            "episode_id": ep["id"],
                            "episode_no": ep["episode_no"],
                            "shot_id": shot["id"],
                            "shot_no": shot["shot_no"],
                            "version_id": version["id"],
                            "version_no": version["version_no"],
                            "task_id": task_id,
                        })
                    report_healthy(media_stages.RESOURCE_VIDEO_POLL)
                except ProviderError as poll_exc:
                    if poll_exc.retryable:
                        report_congestion(media_stages.RESOURCE_VIDEO_POLL, reason="poll")
                    raise
            _assert_job_lease(job_id, owner)
            if result is None or result["status"] not in ("succeeded", "failed"):
                policy = _provider_wait_policy(
                    task_id,
                    result or {},
                    meta,
                    duration_s=float(shot["duration_s"] or 5),
                    provider_submitted_at=float(
                        provider_submitted_at or time.time()
                    ),
                )
                if policy["meta_changed"]:
                    _set_version(
                        version["id"],
                        image_inputs=json.dumps(meta, ensure_ascii=False),
                    )
                if policy.get("stage_progress"):
                    set_pipeline_stage(
                        job_id,
                        media_stages.STAGE_VIDEO_GENERATING,
                        stage_progress=policy["stage_progress"],
                        conn=conn,
                    )
                    conn.commit()
                if policy["elapsed_s"] >= policy["timeout_s"]:
                    raise ProviderError(
                        f"{policy['scope']} {task_id} 已持续 "
                        f"{policy['elapsed_s'] / 60:.1f} 分钟，超过 "
                        f"{policy['timeout_s'] / 60:.1f} 分钟保护上限；"
                        "任务可能卡在上游，请联系供应商核查"
                    )
                if _defer_provider_poll(
                    job_id,
                    task_id,
                    lease_owner=owner,
                    delay=policy["poll_delay_s"],
                ):
                    return
                raise LeaseLost(f"provider poll defer lost lease: {job_id} / {owner}")
            if result["status"] == "failed":
                error_text = result["error"][:400]
                provider_label = str(
                    result.get("provider_label") or "视频模型"
                )
                failure = hiagent.ProviderFailure.from_provider_payload(
                    result.get("failure")
                )
                raise ProviderError(
                    f"{provider_label} 任务失败：{error_text}",
                    raw=error_text,
                    failure=failure,
                )
            break

        _assert_job_lease(job_id, owner)
        meta["provider_video_source_url"] = result["video_url"]
        # Current provider contract advertises a seven-day URL. Keep a
        # conservative six-day reuse window so downstream jobs never race expiry.
        meta["provider_video_source_url_expires_at"] = now() + 6 * 24 * 3600
        dest = _video_path(job["project_id"], ep["episode_no"], shot["shot_no"], version["version_no"])
        await hiagent.download(result["video_url"], str(dest))
        _assert_job_lease(job_id, owner)
        if not provider_recovery_only and meta.get("shot_plan_id"):
            from app.video_plan import active_plan_is_current
            if not active_plan_is_current(str(meta["shot_plan_id"]), conn=conn):
                raise VideoPlanStaleFence("视频生成完成时计划已失效，候选已隔离")
        supervisor_owner = _row_value(job, "owner_run_id")
        if supervisor_owner and not provider_recovery_only:
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
                    episode_id=job["episode_id"],
                    owner_run_id=supervisor_owner,
                )
                media_scheduler.request_cancel(
                    job_id,
                    reason="结果到达时所属 Supervisor 已收口；候选已隔离，不参与自动采用",
                )
                return
        if not provider_recovery_only:
            await _assert_review_dependency_fence_async(
                job, version["id"], "candidate",
            )
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
        if not result_adoptable:
            mark_media_job_state(
                _row_value(job, "run_id"),
                _row_value(job, "step_run_id"),
                "succeeded",
                "历史供应商任务结果已隔离",
            )
            reconcile_episode_generation_status(job["episode_id"])
            return
        if meta.get("shot_plan_id"):
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
        # 生成台产生了新片段，旧的整集合成视频即过期 → 删除，避免成片台展示陈旧成品
        _invalidate_final_video(job["project_id"], ep["episode_no"])
        # 自动 QA 可能跑满 VLM 读超时（默认 300s），超过默认 180s lease 会被 sweeper
        # 抢占：原协程仍会跑完但无法 settle，新 worker 则对已成功版本重跑付费链路。
        _assert_job_lease(
            job_id,
            owner,
            lease_seconds=max(180.0, float(config.TIMEOUT_VLM_READ) + 60.0),
        )
        # 完整补齐模式只有 Supervisor 有权重抽和采用；Worker 只执行、校验并产出候选。
        supervisor_controlled = False
        try:
            ep_mode = get_conn().execute(
                "SELECT video_completion_mode FROM episodes WHERE id=?",
                (job["episode_id"],),
            ).fetchone()
            supervisor_controlled = bool(
                ep_mode and ep_mode["video_completion_mode"] == "complete"
            )
        except Exception:  # noqa: BLE001
            pass
        force_best = await _maybe_auto_qa(
            job,
            version["id"],
            str(dest),
            allow_autonomous_retake=not supervisor_controlled,
        )
        if supervisor_controlled:
            force_best = False
        _assert_job_lease(job_id, owner)
        await _assert_review_dependency_fence_async(
            job, version["id"], "candidate_evidence",
        )
        media_evidence.record_video_candidate(
            version["id"], step_run_id=_row_value(job, "step_run_id")
        )
        technical = json.loads(conn.execute(
            "SELECT technical_validation_json FROM shot_versions WHERE id=?", (version["id"],)
        ).fetchone()["technical_validation_json"] or "{}")
        mode_qa = _persist_video_mode_qa(
            conn,
            version_id=version["id"],
            meta=meta,
            technical=technical,
        )
        if mode_qa and not mode_qa.get("input_roles_valid"):
            raise ProviderError("视频供应商输入角色与已发布模式计划不一致")
        if not technical.get("passed"):
            # 技术校验失败：在 technical_resubmit_limit 内自动新建版本重提
            from app.media_pipeline.retry_policy import technical_resubmit_limit
            resubmits = 0
            try:
                meta = json.loads(version["image_inputs"] or "{}")
                resubmits = int(meta.get("technical_resubmit_count") or 0)
            except Exception:  # noqa: BLE001
                resubmits = 0
            if not supervisor_controlled and resubmits < technical_resubmit_limit():
                if _set_job(job_id, "succeeded", lease_owner=owner):
                    media_scheduler.settle_budget(job_id, cost, success=True)
                    reconcile_episode_generation_status(job["episode_id"])
                    replacement = enqueue_shot(
                        job["shot_id"],
                        reroll=True,
                        after_shot_id=job["after_shot_id"],
                        auto_retake_count=resubmits + 1,
                        dependency_snapshot=meta.get("review_dependency_snapshot"),
                    )
                    # 标记新版本的 technical_resubmit_count（尽力而为）
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
                            import json as _json
                            m = _json.loads(new_ver["image_inputs"] or "{}")
                            if isinstance(m, dict):
                                m["technical_resubmit_count"] = resubmits + 1
                                get_conn().execute(
                                    "UPDATE shot_versions SET image_inputs=? WHERE id=?",
                                    (_json.dumps(m, ensure_ascii=False), new_ver["id"]),
                                )
                                get_conn().commit()
                    except Exception:  # noqa: BLE001
                        pass
                return
            raise ProviderError("视频文件技术校验失败，候选不可采用")
        if not supervisor_controlled:
            await _assert_review_dependency_fence_async(
                job, version["id"], "adoption_relation",
            )
            media_evidence.select_best_video_candidate(
                job["shot_id"], force_best=force_best
            )
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
    except LeaseLost:
        return
    except VideoInflightAdmissionDeferred as exc:
        message = str(exc)
        changed = conn.execute(
            """UPDATE jobs
                  SET status='queued',error=?,reason_code='EPISODE_VIDEO_INFLIGHT_FULL',
                      reason_text=?,provider_non_cancellable=0,
                      provider_create_state='not_started',
                      lease_owner=NULL,lease_expires_at=NULL,next_retry_at=?,updated_at=?
                WHERE id=? AND status='running' AND lease_owner=?
                  AND provider_non_cancellable=0""",
            (message, message, now() + 20.0, now(), job_id, owner),
        )
        if changed.rowcount != 1:
            conn.rollback()
            return
        conn.execute(
            "UPDATE budget_reservations SET status='reserved' WHERE job_id=? AND status='running'",
            (job_id,),
        )
        conn.commit()
        task = asyncio.get_running_loop().create_task(_requeue_after(job_id, 20.0))
        _retry_tasks.add(task)
        task.add_done_callback(_retry_tasks.discard)
        return
    except VideoBudgetAuthorizationError as exc:
        message = str(exc)
        changed = conn.execute(
            """UPDATE jobs
                  SET status='paused_budget',error=?,reason_code='VIDEO_BUDGET_NOT_AUTHORIZED',
                      reason_text=?,provider_non_cancellable=0,
                      provider_create_state='not_started',
                      video_slot_active=0,
                      lease_owner=NULL,lease_expires_at=NULL,next_retry_at=NULL,
                      updated_at=?
                WHERE id=? AND status='running' AND lease_owner=?""",
            (message, message, now(), job_id, owner),
        )
        if changed.rowcount != 1:
            conn.rollback()
            return
        _set_version(version["id"], status="paused_budget", error=message)
        conn.execute(
            "UPDATE shot_versions SET video_slot_active=0 WHERE id=?",
            (version["id"],),
        )
        conn.execute(
            """UPDATE budget_reservations
                  SET status='released',settled_at=?,actual_cost_cny=0
                WHERE job_id=? AND status IN ('reserved','running')""",
            (now(), job_id),
        )
        conn.commit()
        mark_media_job_state(
            _row_value(job, "run_id"),
            _row_value(job, "step_run_id"),
            "paused_budget",
            message,
        )
        reconcile_episode_generation_status(job["episode_id"])
        return
    except VideoPlanStaleFence as exc:
        public = str(exc)
        if _set_job(job_id, "stale", public, lease_owner=owner):
            _set_version(version["id"], status="stale", error=public)
            media_scheduler.settle_budget(job_id, 0.0, success=False)
            reconcile_episode_generation_status(job["episode_id"])
            # A sibling-only replan may preserve this shot's complete execution
            # contract. Recover the accepted provider handle in place when that
            # equivalence can be proven; never issue another create call.
            recover_equivalent_stale_provider_jobs(job["episode_id"])
        return
    except ReviewDependencyFence as exc:
        public = str(exc)
        if _set_job(job_id, "failed", public, lease_owner=owner):
            _set_version(version["id"], status="failed", error=public)
            media_scheduler.settle_budget(job_id, 0.0, success=False)
            reconcile_episode_generation_status(job["episode_id"])
        return
    except VideoInputRepairRequired as exc:
        repair_mode = str(
            meta.get("mode") or meta.get("planned_mode")
            or video_modes.REFERENCE_IMAGE_MODE
        )
        repair_label = {
            video_modes.FIRST_FRAME_MODE: "上一视频尾帧首帧",
            video_modes.FIRST_LAST_FRAME_MODE: "首尾帧",
            video_modes.REFERENCE_IMAGE_MODE: "参考图",
            video_modes.VIDEO_INPUT_MODE: "参考视频",
        }.get(repair_mode, "视频输入")
        repair_code = {
            video_modes.FIRST_FRAME_MODE: "FIRST_FRAME_REPAIR_REQUIRED",
            video_modes.FIRST_LAST_FRAME_MODE: "FIRST_LAST_FRAME_REPAIR_REQUIRED",
            video_modes.REFERENCE_IMAGE_MODE: "REFERENCE_IMAGE_REPAIR_REQUIRED",
            video_modes.VIDEO_INPUT_MODE: "VIDEO_INPUT_REPAIR_REQUIRED",
        }.get(repair_mode, "VIDEO_INPUT_REPAIR_REQUIRED")
        record = errors.log_error(
            exc,
            action="video_mode_input_repair",
            context={
                "shot_id": job["shot_id"],
                "version_id": version["id"],
                "job_id": job_id,
                "mode": meta.get("mode"),
            },
        )
        message = (
            f"{repair_label}输入仍需修复：{exc}。本镜保持 "
            f"{repair_mode}，"
            "未切换生成方式，也未提交不合格输入。"
            f"（{repair_code} · {record.error_id}）"
        )
        changed = conn.execute(
            """UPDATE jobs
                  SET status='waiting_human',error=?,
                      reason_code=?,
                      reason_text=?,lease_owner=NULL,lease_expires_at=NULL,
                      next_retry_at=NULL,updated_at=?
                WHERE id=? AND status='running' AND lease_owner=?""",
            (message, repair_code, message, now(), job_id, owner),
        )
        if changed.rowcount != 1:
            conn.rollback()
            return
        _set_version(version["id"], status="waiting_human", error=message)
        conn.execute(
            """UPDATE shot_video_generation_plans
                  SET status='waiting_asset',updated_at=?
                WHERE id=?""",
            (now(), str(meta.get("shot_plan_id") or "")),
        )
        conn.commit()
        media_scheduler.settle_budget(job_id, 0.0, success=False)
        mark_media_job_state(
            _row_value(job, "run_id"),
            _row_value(job, "step_run_id"),
            "waiting_human",
            message,
        )
        reconcile_episode_generation_status(job["episode_id"])
        return
    except ProviderCreateUnresolved as exc:
        message = str(exc)
        if not _commit_provider_create_unresolved(
            conn,
            job_id=job_id,
            version_id=version["id"],
            owner=owner,
            message=message,
        ):
            return
        mark_media_job_state(
            _row_value(job, "run_id"),
            _row_value(job, "step_run_id"),
            "waiting_human",
            message,
        )
        reconcile_episode_generation_status(job["episode_id"])
        return
    except (ProviderError, Exception) as exc:  # noqa: BLE001 失败要响：原文进日志，前端给码+分类
        from app.harness.model_gateway import StructuredProviderRejection

        if isinstance(exc, StructuredProviderRejection):
            exc = ProviderError(
                "AI 视频提示词服务拒绝当前内容",
                raw=str(exc),
                failure=hiagent.ProviderFailure.model_rejection(
                    hiagent.ProviderFailureKind.PROMPT_PROVIDER_REJECTED
                ),
                delivery_state="not_sent",
                replay_safe=True,
            )
        if not media_scheduler.renew_lease(job_id, owner, lease_seconds=180.0):
            return
        record = errors.log_error(
            exc, action="shot_video_generate",
            context={"shot_id": job["shot_id"], "version_id": version["id"], "job_id": job_id})
        poll_state = conn.execute(
            "SELECT provider_poll_required FROM jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        guidance = (
            _video_model_rejection_guidance(meta, exc)
            if isinstance(exc, ProviderError)
            else None
        )
        provider_failure = exc.failure if isinstance(exc, ProviderError) else None
        reason_code = (
            guidance[0]
            if guidance
            else (provider_failure.reason_code if provider_failure else None)
        )
        public = (
            f"{guidance[1]}（{guidance[0]} · {record.error_id}）"
            if guidance else record.public
        )
        provider_poll_pending = bool(
            task_id
            and poll_state is not None
            and poll_state["provider_poll_required"]
        )
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
            return
        # 仅结构化 retryable 故障自动重排；重试耗尽后转人工，不改变原始类别。
        if isinstance(exc, ProviderError) and _schedule_job_retry(
            job_id, exc, lease_owner=owner
        ):
            _set_version(version["id"], status="queued")
            return
        external_terminal = bool(
            provider_failure
            and provider_failure.disposition
            is hiagent.ProviderFailureDisposition.EXTERNAL_TERMINAL
        )
        if provider_poll_pending and external_terminal:
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
            return
        final_status = (
            "waiting_human"
            if provider_failure and not external_terminal
            else "failed"
        )
        if _set_job(job_id, final_status, public, lease_owner=owner):
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
            if provider_failure:
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


def _persist_video_mode_qa(
    conn,
    *,
    version_id: str,
    meta: dict[str, Any],
    technical: dict[str, Any],
) -> dict[str, Any] | None:
    shot_plan_id = str(meta.get("shot_plan_id") or "")
    if not shot_plan_id:
        return None
    row = conn.execute(
        "SELECT qa_json FROM shot_versions WHERE id=?",
        (version_id,),
    ).fetchone()
    try:
        qa = json.loads(row["qa_json"] or "{}") if row else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        qa = {}
    from app.stages import evaluate_video_mode_qa

    result = evaluate_video_mode_qa(meta=meta, qa=qa, technical=technical)
    qa["video_mode_qa"] = result
    _set_version(version_id, qa_json=json.dumps(qa, ensure_ascii=False))
    conn.execute(
        """INSERT OR REPLACE INTO video_mode_qa_results(
               id,shot_plan_id,version_id,planned_mode,actual_mode,
               technical_success,semantic_success,boundary_start_match,
               boundary_end_match,result_json,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            new_id("vmqa"), shot_plan_id, version_id,
            str(result.get("planned_mode") or ""),
            str(result.get("actual_mode") or ""),
            int(bool(result.get("technical_success"))),
            (
                None if result.get("semantic_success") is None
                else int(bool(result.get("semantic_success")))
            ),
            result.get("boundary_start_match"),
            result.get("boundary_end_match"),
            json.dumps(result, ensure_ascii=False), now(),
        ),
    )
    conn.commit()
    return result


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


# ---------- worker 生命周期 ----------

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


def recover_and_start(loop_concurrency: int | None = None) -> None:
    """启动时恢复队列（PRD §4.5 验收：中途杀进程重启后队列状态可恢复）。"""
    from app.media_pipeline.bootstrap import start_media_pipeline

    start_media_pipeline()
    decommission_legacy_keyframe_jobs()
    # Reconcile expired durable leases, then rebuild scheduling exclusively from
    # DB state. Startup recovery may have pre-enqueued dozens of duplicate IDs;
    # discarding those in-memory copies is safe because jobs are durable.
    media_scheduler.recoverable_jobs()
    _drain_memory_queue(_queue)
    _drain_memory_queue(_video_ready_queue)
    _drain_memory_queue(_poll_queue)
    conn = get_conn()
    stale_provider_episode_ids = [
        row["episode_id"]
        for row in conn.execute(
            """SELECT DISTINCT episode_id FROM jobs
                WHERE kind='video' AND status='stale'
                  AND provider_non_cancellable=1
                  AND cancellation_requested=0 AND abandoned=0"""
        ).fetchall()
    ]
    for episode_id in stale_provider_episode_ids:
        recover_equivalent_stale_provider_jobs(episode_id)
    generating_episode_ids = [
        row["id"] for row in conn.execute(
            "SELECT id FROM episodes WHERE status='generating'"
        ).fetchall()
    ]
    for episode_id in generating_episode_ids:
        reconcile_episode_generation_status(episode_id)
    # 启动时按通道分别取并发，不再用 max(submit, reference) 混成一个池
    n = loop_concurrency  # 若显式传入，仍作为参考图 worker 目标
    ensure_workers(n)
    _start_durable_dispatcher()
    _dispatch_due_jobs()


def _recover_one_media_job(
    conn, job_id: str, run_id: str | None, step_run_id: str | None, reason: str
) -> bool:
    """把一个卡住的媒体 job 复位给持久调度器：
    - accepted provider task 回到 waiting_provider，其他任务回到 queued；
      provider_task_id、轮询责任与持久化 retry 到期时间保留
    - Run 立即进入 WAITING_RETRY，监控页显示“恢复排队中”
    - 被中断的 Step 保持 FAILED 审计终态，并创建 iteration+1 的 READY attempt
    返回 True 表示实际复位过；False 表示 job 已不存在或被并发改动（调用方忽略）。"""
    cursor = conn.execute(
        "UPDATE jobs SET status=CASE WHEN provider_poll_required=1 "
        "THEN 'waiting_provider' ELSE 'queued' END, "
        "lease_owner=NULL, lease_expires_at=NULL, "
        "error=NULL, updated_at=? "
        "WHERE id=? AND status IN ('running','queued','waiting_provider') "
        "AND cancellation_requested=0 AND abandoned=0",
        (now(), job_id),
    )
    if cursor.rowcount != 1:
        return False
    try:
        from app.orchestration.state_machine import transition_run, transition_step

        run = conn.execute(
            "SELECT status FROM workflow_runs WHERE id=?", (run_id,)
        ).fetchone() if run_id else None
        if run and run["status"] in {"RUNNING", "PAUSED_EXTERNAL"}:
            transition_run(
                run_id, run["status"], "WAITING_RETRY", reason,
                failure_code=(
                    "SERVICE_RESTART" if run["status"] == "PAUSED_EXTERNAL" else "LEASE_EXPIRED"
                ),
                conn=conn,
            )
        old_step = conn.execute(
            "SELECT * FROM step_runs WHERE id=?", (step_run_id,)
        ).fetchone() if step_run_id else None
        if old_step:
            previous_status = old_step["status"]
            if previous_status == "RUNNING":
                transition_step(
                    step_run_id, "RUNNING", "FAILED", reason,
                    decision="retry", error_code="LEASE_EXPIRED", conn=conn,
                )
            if previous_status in {"RUNNING", "FAILED"}:
                iteration = conn.execute(
                    "SELECT COALESCE(MAX(iteration_no),0)+1 AS n FROM step_runs "
                    "WHERE run_id=? AND step_key=?",
                    (run_id, old_step["step_key"]),
                ).fetchone()["n"]
                new_step_id = new_id("step")
                conn.execute(
                    """INSERT INTO step_runs(
                           id, run_id, step_key, iteration_no, parent_step_run_id, status,
                           agent_name, contract_version, prompt_version, policy_version,
                           input_artifact_ids_json, context_manifest_json
                       ) VALUES(?,?,?,?,?,'PENDING',?,?,?,?,?,?)""",
                    (
                        new_step_id, run_id, old_step["step_key"], int(iteration), step_run_id,
                        old_step["agent_name"], old_step["contract_version"],
                        old_step["prompt_version"], old_step["policy_version"],
                        old_step["input_artifact_ids_json"] or "[]",
                        old_step["context_manifest_json"] or "{}",
                    ),
                )
                transition_step(new_step_id, "PENDING", "READY", reason, conn=conn)
                conn.execute(
                    "UPDATE jobs SET step_run_id=? WHERE id=?", (new_step_id, job_id)
                )
                conn.execute(
                    "INSERT INTO run_events(id, run_id, step_run_id, ts, event_type, severity, "
                    "message, payload_json) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        new_id("evt"), run_id, new_step_id, now(), "MEDIA_RECOVERY_QUEUED",
                        "warning", reason,
                        json.dumps(
                            {"job_id": job_id, "previous_step_run_id": step_run_id},
                            ensure_ascii=False,
                        ),
                    ),
                )
    except Exception:  # noqa: BLE001 legacy/minimal schemas still recover the durable job itself
        pass
    # The durable dispatcher will see this row within one second. Avoid directly
    # flooding the FIFO when startup/sweeper recovers an entire episode.
    return True

def recover_media_jobs() -> int:
    """启动时恢复因服务重启被中断的媒体任务。

    init_db() 在重启时把所有 status='RUNNING' 的 workflow_runs 标为 PAUSED_EXTERNAL +
    failure_code='SERVICE_RESTART'，同时把对应 step_runs 标 FAILED；但底层 jobs 表的
    lease（默认 180s）在重启那一刻往往还没过期，media_scheduler.recoverable_jobs()
    只扫 status='running' AND lease_expires_at<now 的 job，因此不会重新入队——
    结果就是用户看到的"任务卡在'服务重启，可从安全检查点恢复'"。

    本函数把这些 job 显式复位回 queued；数据库驱动的持久调度器会在下一轮重新
    发现它们。run 从 PAUSED_EXTERNAL 转回 WAITING_RETRY，旧 FAILED step 保留为
    审计历史，并创建 iteration+1 的 READY step 供 worker 接管。

    边界：不恢复 PAUSED_BUDGET（预算不足，需显式 retry_paused 释放预算后重试）；
         不恢复 FAILED/CANCELLED（真正报错或人工取消）。"""
    media_scheduler.reconcile_cancelled_version_states()
    decommission_legacy_keyframe_jobs()
    conn = get_conn()
    rows = rows_to_dicts(conn.execute(
        """SELECT j.id AS job_id, j.run_id, j.step_run_id
           FROM jobs j
           JOIN workflow_runs wr ON wr.id=j.run_id
           WHERE j.status IN ('running','queued','waiting_provider')
             AND wr.status='PAUSED_EXTERNAL'
             AND wr.failure_code='SERVICE_RESTART'
             AND j.cancellation_requested=0
             AND j.abandoned=0""",
    ))
    resumed = 0
    for r in rows:
        if _recover_one_media_job(
            conn, r["job_id"], r["run_id"], r["step_run_id"], "服务重启后自动恢复任务"
        ):
            resumed += 1
    conn.commit()
    try:
        reconcile_stalled_video_jobs()
    except Exception as exc:  # noqa: BLE001 启动恢复各子域隔离，媒体 lease 恢复仍需成功
        errors.record_and_format(
            exc,
            action="startup_recovery.media_stalls",
            context={"resumed_media_jobs": resumed},
        )
    return resumed


def _block_orphaned_continuity_job(conn, row) -> bool:
    """Keep the planned dependency and surface repair instead of degrading."""
    from app.media_pipeline import stages as media_stages
    from app.media_pipeline.scheduler import continuity_anchor_ready
    from app.media_pipeline.stage_state import set_pipeline_stage

    after_shot_id = row["after_shot_id"]
    if not after_shot_id or not row["version_id"]:
        return False
    ready, reason = continuity_anchor_ready(conn, after_shot_id)
    if ready:
        return False
    # 上游还有活跃任务时继续等；只处理已明确需要人工或上游已不存在的孤儿链。
    if "人工" not in str(reason or "") and "不存在" not in str(reason or ""):
        return False

    version = conn.execute(
        "SELECT * FROM shot_versions WHERE id=?", (row["version_id"],)
    ).fetchone()
    if version is None:
        return False

    try:
        planned_meta = json.loads(version["image_inputs"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        planned_meta = {}
    shot_plan_id = str(planned_meta.get("shot_plan_id") or "")
    if shot_plan_id:
        message = (
            "计划依赖的上一镜采用视频或真实尾帧当前不可恢复；"
            "本镜保持原生成模式等待修复，系统未改用其他模式。"
        )
        conn.execute(
            """UPDATE jobs
                  SET status='waiting_human',error=?,
                      reason_code='VIDEO_PLAN_DEPENDENCY_REPAIR_REQUIRED',
                      reason_text=?,next_retry_at=NULL,updated_at=?
                WHERE id=?""",
            (message, message, now(), row["id"]),
        )
        conn.execute(
            "UPDATE shot_versions SET status='waiting_human',error=? WHERE id=?",
            (message, row["version_id"]),
        )
        set_pipeline_stage(
            row["id"],
            media_stages.STAGE_WAITING_HUMAN,
            reason_code="VIDEO_PLAN_DEPENDENCY_REPAIR_REQUIRED",
            reason_text=message,
            conn=conn,
        )
        conn.commit()
        return True

    message = (
        "历史连续性任务缺少可恢复的上一镜尾帧；系统未改写提示词、"
        "未移除依赖，也未切换生成模式。请修复上游镜头后重新生成。"
    )
    conn.execute(
        """UPDATE jobs
              SET status='waiting_human',error=?,
                  reason_code='VIDEO_DEPENDENCY_REPAIR_REQUIRED',
                  reason_text=?,next_retry_at=NULL,updated_at=?
            WHERE id=?""",
        (message, message, now(), row["id"]),
    )
    conn.execute(
        "UPDATE shot_versions SET status='waiting_human',error=? WHERE id=?",
        (message, row["version_id"]),
    )
    set_pipeline_stage(
        row["id"],
        media_stages.STAGE_WAITING_HUMAN,
        reason_code="VIDEO_DEPENDENCY_REPAIR_REQUIRED",
        reason_text=message,
        conn=conn,
    )
    conn.commit()
    return True


def reconcile_stalled_video_jobs(limit: int = 50) -> dict[str, int]:
    """周期修复没有 worker 能消费的业务级卡死状态。

    ``paused_budget`` is an intentional user gate, not a stalled state. Only
    an explicit page action may call ``retry_paused`` and move it to queued.
    """
    from app.observability.metrics import inc

    conn = get_conn()
    stamp = now()
    report = {
        "redundant_preflight_closed": 0,
        "legacy_jobless_recovered": 0,
        "legacy_preflight_reactivated": 0,
        "preflight_retried": 0,
        "continuity_degraded": 0,
        "dependency_repair_required": 0,
        "budget_resumed": 0,
        "episodes_reconciled": 0,
    }

    redundant = conn.execute(
        """UPDATE jobs
           SET status='cancelled', cancellation_requested=1,
               reason_code='SUPERSEDED_PREFLIGHT',
               reason_text='已有成功采用版，关闭并发产生的冗余校验任务',
               error='已有成功采用版，关闭并发产生的冗余校验任务',
               next_retry_at=NULL, stage_status='complete', updated_at=?
           WHERE kind='video' AND version_id IS NULL
             AND status IN ('waiting_retry','waiting_human')
             AND cancellation_requested=0 AND abandoned=0
             AND EXISTS (
               SELECT 1
               FROM shots s
               JOIN shot_versions v ON v.id=s.adopted_version_id
               WHERE s.id=jobs.shot_id AND v.status='succeeded'
             )""",
        (stamp,),
    ).rowcount
    # SQLite starts a write transaction even when UPDATE affects zero rows.
    # Release it before the read-heavy reconciliation passes below.
    conn.commit()
    if redundant:
        report["redundant_preflight_closed"] = int(redundant)

    # 兼容修复上线前的历史事故：当时 preflight 发生在 jobs INSERT 之前，
    # 因而只留下 issue artifact。仅恢复 24 小时内、整集仍处于 generating、
    # 且从未创建过版本或任务的明确 VIDEO_PREFLIGHT_BLOCKED 镜头。
    legacy_rows = rows_to_dicts(conn.execute(
        """SELECT a.scope_id AS shot_id, a.content_json
           FROM artifacts a
           JOIN shots s ON s.id=a.scope_id
           JOIN episodes e ON e.id=s.episode_id
           WHERE a.type='video_shot_issue'
             AND a.scope_type='shot'
             AND a.status IN ('candidate','validated','approved')
             AND a.created_at>=?
             AND e.status='generating'
             AND NOT EXISTS (
               SELECT 1 FROM shot_versions v WHERE v.shot_id=s.id
             )
             AND NOT EXISTS (
               SELECT 1 FROM jobs j WHERE j.shot_id=s.id AND j.kind='video'
             )
           ORDER BY a.created_at DESC LIMIT ?""",
        (stamp - 86400.0, max(1, int(limit))),
    ))
    seen_legacy: set[str] = set()
    for row in legacy_rows:
        shot_id = row["shot_id"]
        if shot_id in seen_legacy:
            continue
        seen_legacy.add(shot_id)
        try:
            payload = json.loads(row["content_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        codes = {
            str(item.get("code") or "")
            for item in (payload.get("issues") or [])
            if isinstance(item, dict)
        }
        if "VIDEO_PREFLIGHT_BLOCKED" not in codes:
            continue
        try:
            enqueue_shot(shot_id)
        except Exception:
            # 新版 enqueue 已经把失败镜头纳入可见任务状态。
            pass
        if conn.execute(
            "SELECT 1 FROM jobs WHERE shot_id=? AND kind='video' LIMIT 1",
            (shot_id,),
        ).fetchone():
            report["legacy_jobless_recovered"] += 1

    preflight_rows = rows_to_dicts(conn.execute(
        """SELECT id, shot_id, status FROM jobs
           WHERE kind='video' AND version_id IS NULL
             AND status='waiting_retry'
             AND (next_retry_at IS NULL OR next_retry_at<=?)
             AND cancellation_requested=0 AND abandoned=0
           ORDER BY updated_at LIMIT ?""",
        (stamp, max(1, int(limit))),
    ))
    for row in preflight_rows:
        try:
            result = enqueue_shot(row["shot_id"])
            if result.get("task_accepted") or result.get("reused"):
                report["preflight_retried"] += 1
                if row["status"] == "waiting_human":
                    report["legacy_preflight_reactivated"] += 1
        except Exception:
            # enqueue_shot 已持久化新的 retry / waiting_human 状态。
            continue

    cutoff = stamp - float(config.VIDEO_CONTINUITY_ORPHAN_TIMEOUT)
    continuity_rows = rows_to_dicts(conn.execute(
        """SELECT id, shot_id, version_id, episode_id, project_id, after_shot_id
           FROM jobs
           WHERE kind='video' AND version_id IS NOT NULL
             AND pipeline_stage IN ('waiting_continuity_anchor','waiting_dependency')
             AND status IN ('queued','waiting_retry','waiting_human')
             AND COALESCE(stage_started_at, updated_at, created_at)<=?
             AND cancellation_requested=0 AND abandoned=0
           ORDER BY COALESCE(stage_started_at, updated_at, created_at)
           LIMIT ?""",
        (cutoff, max(1, int(limit))),
    ))
    for row in continuity_rows:
        try:
            if _block_orphaned_continuity_job(conn, row):
                report["dependency_repair_required"] += 1
        except Exception:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass

    episode_rows = conn.execute(
        "SELECT id FROM episodes WHERE status='generating'"
    ).fetchall()
    for row in episode_rows:
        try:
            if reconcile_episode_generation_status(row["id"]):
                report["episodes_reconciled"] += 1
        except Exception:
            continue
    total = sum(report.values())
    if total:
        inc("video_stall_sweeper_repairs_total", value=total, **report)
    return report


_SWEEPER_INTERVAL_SECONDS = 60.0
_sweeper_task: asyncio.Task | None = None


async def _stale_lease_sweeper(interval_seconds: float = _SWEEPER_INTERVAL_SECONDS) -> None:
    """周期性回收卡死的媒体 job 的过期 lease。

    worker 进程被 kill -9、容器 OOM、协程异常退出等情况会让 job 卡在
    status='running' 且 lease_expires_at<now；recoverable_jobs() 只在启动时扫一次，
    启动后过期的 lease 不会被自动回收。本协程每 interval_seconds 秒扫一次，
    把过期 lease 的 job 复位回 queued，交给持久调度器在下一轮重新发现。

    幂等：多次扫到同一 job 时，第二次 CAS 会因 status 已是 'queued' 而 rowcount=0，
    不会重复恢复。"""
    try:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                conn = get_conn()
                stamp = now()
                rows = rows_to_dicts(conn.execute(
                    """SELECT id, run_id, step_run_id FROM jobs
                       WHERE status='running'
                         AND lease_expires_at IS NOT NULL
                         AND lease_expires_at < ?
                         AND cancellation_requested=0
                         AND abandoned=0""",
                    (stamp,),
                ))
                resumed = 0
                for r in rows:
                    if _recover_one_media_job(
                        conn, r["id"], r["run_id"], r["step_run_id"],
                        "lease 过期，自动回收并重新入队",
                    ):
                        resumed += 1
                conn.commit()
                reconcile_stalled_video_jobs()
            except Exception:  # noqa: BLE001 周期任务不能死
                pass
    except asyncio.CancelledError:
        return


def start_stale_lease_sweeper(interval_seconds: float = _SWEEPER_INTERVAL_SECONDS) -> None:
    """启动周期 lease 回收协程；多次调用幂等（已有任务在跑则不重启）。
    覆盖 worker 崩溃/OOM 等非服务重启场景下的中断恢复需求。"""
    global _sweeper_task
    if _sweeper_task is not None and not _sweeper_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _sweeper_task = loop.create_task(_stale_lease_sweeper(interval_seconds))
    _retry_tasks.add(_sweeper_task)
    _sweeper_task.add_done_callback(_retry_tasks.discard)


def ensure_workers(n: int | None = None) -> None:
    """分别维护参考图 / 视频提交 / 轮询三通道 worker。

    ``n`` 若给出，覆盖参考图通道目标；视频提交与 poll 始终读通道配置，
    修复「热更新只跟 video_submit、启动却取 max」的不一致。
    """
    global _worker_target, _reference_worker_target, _video_ready_worker_target, _poll_worker_target
    from app.media_pipeline import stages as media_stages
    from app.media_pipeline.concurrency import channel_limit

    ref_n = max(0, int(n if n is not None else channel_limit(media_stages.RESOURCE_REFERENCE)))
    video_n = max(0, int(channel_limit(media_stages.RESOURCE_VIDEO_SUBMIT)))
    poll_n = max(0, int(channel_limit(media_stages.RESOURCE_VIDEO_POLL)))

    _reference_worker_target = ref_n
    _worker_target = ref_n  # 兼容旧字段：代表参考图 worker
    _video_ready_worker_target = video_n
    _poll_worker_target = poll_n

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    def _discard_worker(pool: list[asyncio.Task], task: asyncio.Task) -> None:
        _worker_retire_events.pop(task, None)
        try:
            pool.remove(task)
        except ValueError:
            pass

    def _resize(pool: list[asyncio.Task], target: int, prefix: str, queue: asyncio.Queue[str]) -> None:
        for task in tuple(pool):
            if task.done():
                _discard_worker(pool, task)
        accepting = [
            task for task in pool
            if not _worker_retire_events[task].is_set()
        ]
        while len(accepting) < target:
            used_names = {task.get_name() for task in pool}
            index = 0
            while f"{prefix}{index}" in used_names:
                index += 1
            name = f"{prefix}{index}"
            retirement = asyncio.Event()
            task = loop.create_task(
                _worker_loop(name, queue, retirement),
                name=name,
            )
            _worker_retire_events[task] = retirement
            task.add_done_callback(
                lambda done, worker_pool=pool: _discard_worker(worker_pool, done)
            )
            pool.append(task)
            accepting.append(task)
        for task in reversed(accepting[target:]):
            _worker_retire_events[task].set()

    _resize(_workers, ref_n, "ref", _queue)
    _resize(_video_ready_workers, video_n, "vr", _video_ready_queue)
    _resize(_poll_workers, poll_n, "poll", _poll_queue)


async def stop() -> None:
    """优雅停机：取消常驻 worker 循环。否则 uvicorn --reload/退出时会卡在
    'Waiting for connections to close'——常驻 while-True 任务不退出，停机就挂起。"""
    global _sweeper_task, _dispatcher_task, _worker_target, _poll_worker_target
    global _reference_worker_target, _video_ready_worker_target
    try:
        from app.media_pipeline.bootstrap import stop_media_pipeline
        await stop_media_pipeline()
    except Exception:  # noqa: BLE001
        pass
    if _sweeper_task is not None:
        _sweeper_task.cancel()
    if _dispatcher_task is not None:
        _dispatcher_task.cancel()
        try:
            await _dispatcher_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _dispatcher_task = None
    for t in _retry_tasks:
        t.cancel()
    if _retry_tasks:
        await asyncio.gather(*tuple(_retry_tasks), return_exceptions=True)
    _retry_tasks.clear()
    for t in (*_workers, *_video_ready_workers, *_poll_workers):
        retirement = _worker_retire_events.get(t)
        if retirement is not None:
            retirement.set()
        t.cancel()
    for t in (*_workers, *_video_ready_workers, *_poll_workers):
        try:
            await t
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _workers.clear()
    _video_ready_workers.clear()
    _poll_workers.clear()
    _worker_retire_events.clear()
    _worker_target = 0
    _reference_worker_target = 0
    _video_ready_worker_target = 0
    _poll_worker_target = 0
    _drain_memory_queue(_queue)
    _drain_memory_queue(_video_ready_queue)
    _drain_memory_queue(_poll_queue)


def retry_paused(episode_id: str, *, job_id: str | None = None) -> int:
    """Resume budget-paused work against the current user-approved cap."""
    conn = get_conn()
    budget_limit = episode_video_budget_limit(episode_id)
    if job_id:
        rows = conn.execute(
            """SELECT j.id,j.reserved_cost_cny,j.kind,s.duration_s
                 FROM jobs j LEFT JOIN shots s ON s.id=j.shot_id
                WHERE j.episode_id=? AND j.id=? AND j.status='paused_budget'""",
            (episode_id, job_id),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT j.id,j.reserved_cost_cny,j.kind,s.duration_s
                 FROM jobs j LEFT JOIN shots s ON s.id=j.shot_id
                WHERE j.episode_id=? AND j.status='paused_budget'""",
            (episode_id,),
        ).fetchall()
    resumed = 0
    for r in rows:
        estimate = float(r["reserved_cost_cny"] or 0)
        if estimate <= 0:
            estimate = (
                config.IMAGE_PRICE_PER_UNIT
                if r["kind"] == "scene"
                else (
                    shot_cost_cny(int(r["duration_s"] or 5))
                    + config.IMAGE_PRICE_PER_UNIT
                    * video_modes.estimated_keyframe_generation_count()
                )
            )
        if media_scheduler.reserve_budget(
            r["id"], episode_id, estimate,
            budget_limit, conn=conn,
        ):
            changed = conn.execute(
                """UPDATE jobs SET status='queued', error=NULL, next_retry_at=NULL, updated_at=?
                   WHERE id=? AND status='paused_budget'""",
                (now(), r["id"]),
            )
            conn.commit()
            if changed.rowcount != 1:
                continue
            try:
                _enqueue_for_current_status(r["id"])
            except Exception as exc:
                errors.record_and_format(
                    exc,
                    action="budget_resume_dispatch",
                    context={"episode_id": episode_id, "job_id": r["id"]},
                )
            resumed += 1
    return resumed

__all__ = [name for name in globals() if not name.startswith("__")]
