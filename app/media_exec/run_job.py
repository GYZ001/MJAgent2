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
    expected_authority = (
        str(expected).split(":", 1)[0]
        if ":" in str(expected)
        else None
    )
    current_authority = current.get("narrative_authority_version")
    current_requires_authority = bool(current.get("narrative_authority_required"))
    if current_requires_authority:
        _assert_current_storyboard_completion_authority(
            conn,
            episode_id=episode_id,
            write_point=write_point,
        )
    authority_equal = bool(
        (not current_requires_authority and expected_authority is None)
        or (
            current_requires_authority
            and expected_authority
            and expected_authority == current_authority
            and current.get("narrative_authority_verified")
        )
    )
    expected_assets = captured.get("asset_inputs") or []
    current_assets = current.get("asset_inputs") or []
    # The current shot's gallery is produced/updated by this very job.  It is
    # an output of the run, not an upstream dependency: comparing it here
    # makes a successful reference build invalidate its own captured token.
    # Other shots remain fenced, so unrelated asset edits still stop a stale
    # provider result from becoming a candidate.
    target_shot_id = row["shot_id"] if row else None

    def asset_contract(items):
        return sorted(
            json.dumps(
                {key: value for key, value in item.items() if key not in {"version_id"}},
                ensure_ascii=False, sort_keys=True,
            )
            for item in items
            if item.get("shot_id") != target_shot_id
        )
    # Modern narrative jobs bind exact asset revisions in the validated video
    # plan and recheck them again at provider submission. Shot galleries are
    # downstream outputs: parallel sibling jobs naturally add images and must
    # not invalidate one another's captured qualification snapshot.
    assets_equal = bool(
        current_requires_authority
        or not expected_assets
        or asset_contract(current_assets) == asset_contract(expected_assets)
    )
    if (
        current.get("eligible_for_production")
        and upstream_equal
        and authority_equal
        and assets_equal
    ):
        return
    detail = {
        "code": "REVIEW_DEPENDENCY_STALE",
        "write_point": write_point,
        "expected_qualification_version": expected,
        "current_qualification_version": current.get("qualification_version"),
        "expected_narrative_authority_version": expected_authority,
        "current_narrative_authority_version": current_authority,
        "narrative_authority_required": current_requires_authority,
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

def _assert_job_lease(job_id: str, owner: str, *, lease_seconds: float = 180.0) -> None:
    if not media_scheduler.renew_lease(job_id, owner, lease_seconds=lease_seconds):
        raise LeaseLost(f"job lease lost: {job_id} / {owner}")


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
        _poll_queue.put_nowait(job_id)
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
        _video_ready_queue.put_nowait(job_id)
    else:
        _queue.put_nowait(job_id)


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
        _poll_queue.put_nowait(row["id"])
        poll_enqueued += 1

    chosen = [row for _, row in main_candidates[:main_slots]]
    remaining = max(0, main_slots - len(chosen))
    if remaining:
        from app.media_pipeline.retry_policy import prepared_reference_backlog
        speculative_limit = min(remaining, prepared_reference_backlog())
        chosen.extend(row for _, row in blocked_reference_candidates[:speculative_limit])
    for row in chosen:
        _queue.put_nowait(row["id"])

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
        # 持久化车道（轻量，失败忽略）
        try:
            if true_ready and ready:
                set_pipeline_stage(
                    row["id"], media_stages.STAGE_VIDEO_READY,
                    scheduler_lane=media_stages.LANE_VIDEO_READY,
                    priority_class="first_pass" if not is_retake else "retake",
                    conn=conn,
                )
            elif not ready and (refs_ready or static_waiting):
                set_pipeline_stage(
                    row["id"],
                    (
                        media_stages.STAGE_WAITING_DEPENDENCY
                        if meta.get("shot_plan_id")
                        else media_stages.STAGE_WAITING_CONTINUITY
                    ),
                    reason_code=(
                        "WAITING_VIDEO_PLAN_DEPENDENCY"
                        if meta.get("shot_plan_id")
                        else "WAITING_CONTINUITY_ANCHOR"
                    ),
                    reason_text=(
                        "等待上一镜采用素材"
                        if meta.get("shot_plan_id")
                        else (
                            f"等待镜尾帧（{after_shot_id}）"
                            if after_shot_id else "等待上一镜尾帧"
                        )
                    ),
                    scheduler_lane=media_stages.LANE_REFERENCE_CRITICAL,
                    conn=conn,
                )
        except Exception:  # noqa: BLE001
            pass

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

    conn.commit()

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
        _poll_queue.put_nowait(row["id"])
        poll_enqueued += 1

    vr_enqueued = 0
    for _, row in video_ready[:vr_slots]:
        _video_ready_queue.put_nowait(row["id"])
        vr_enqueued += 1

    # 参考图：cohort + 高低水位；关键路径优先
    allow, demand = should_start_more_reference_work(conn=conn)
    ref_enqueued = 0
    if allow and demand > 0 and ref_slots > 0:
        budget = min(demand, ref_slots)
        ordered = reference_critical + reference_normal + retake_jobs
        for _, row in ordered[:budget]:
            _queue.put_nowait(row["id"])
            ref_enqueued += 1
    elif ref_slots > 0 and reference_critical:
        # 水位满时仍允许完成已接近完成的关键路径（只取 critical，且仅当已有 slot 进度）
        for _, row in reference_critical:
            if ref_enqueued >= ref_slots:
                break
            if _completed_reference_slots(row.get("image_inputs")) > 0:
                _queue.put_nowait(row["id"])
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
                _dispatch_due_jobs()
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
    updated = conn.execute(
        """UPDATE jobs SET status='queued', error=?, retry_count=?, next_retry_at=?,
                  lease_owner=NULL, lease_expires_at=NULL, updated_at=? WHERE id=?"""
        + (" AND lease_owner=?" if lease_owner else ""),
        (note, attempt, now() + delay, now(), job_id, *([lease_owner] if lease_owner else [])),
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


def _visual_anchors_from_version_meta(meta: dict) -> list[dict]:
    """从冻结 reference set / image_inputs 读取关键帧与人物/场景视觉锚点。"""
    from app.multiview import PURPOSE_QA_ANCHOR, purpose_list, library_anchor_assets_from_manifest
    anchors: list[dict] = []
    manifest = meta.get("reference_manifest") if isinstance(meta, dict) else None
    if isinstance(manifest, dict):
        anchors.extend(library_anchor_assets_from_manifest(manifest))
    for ref in (meta.get("reference_images") or []):
        if not isinstance(ref, dict):
            continue
        if ref.get("deleted"):
            continue
        purposes = purpose_list(ref)
        rtype = str(ref.get("type") or "")
        is_keyframe = (
            rtype == "plot_key_frame"
            or video_modes.is_narrative_keyframe_slot(str(ref.get("slot_key") or ""))
        )
        if is_keyframe and not ref.get("selectedForSeedance"):
            continue
        is_anchor = PURPOSE_QA_ANCHOR in purposes or rtype in {"character", "scene", "previous_shot_frame"}
        if not (is_keyframe or is_anchor):
            continue
        path = ref.get("path") or ref.get("image_path")
        if not path or not Path(str(path)).is_file():
            continue
        item = {
            "type": rtype,
            "path": path,
            "image_path": path,
            "entity_type": ref.get("entity_type"),
            "entity_name": ref.get("entity_name"),
            "view_role": ref.get("view_role"),
            "library_revision_id": ref.get("library_revision_id"),
            "library_view_id": ref.get("library_view_id"),
            "role": "keyframe" if is_keyframe else None,
        }
        anchors.append(item)
    # 去重 path
    seen = set()
    out = []
    for a in anchors:
        p = a.get("image_path") or a.get("path")
        if not p or p in seen:
            continue
        seen.add(p)
        out.append(a)
    return out


def _video_qa_shot_model(shot_row):
    """Load the same shot contract used by paid prompt compilation for video QA."""
    from app.continuity import apply_shot_contract
    from app.schemas import Shot

    shot_model = Shot(
        shot_no=shot_row["shot_no"], duration_s=shot_row["duration_s"],
        shot_size=shot_row["shot_size"] or "中景",
        camera_move=shot_row["camera_move"] or "固定",
        scene_time=(shot_row["scene_time"] if "scene_time" in shot_row.keys() else "") or "",
        scene_setting=shot_row["scene_setting"] or "",
        scene_name=(shot_row["scene_name"] if "scene_name" in shot_row.keys() else "") or "",
        characters=json.loads(shot_row["characters"] or "[]"),
        action_desc=shot_row["action_desc"] or "",
        first_frame_desc=(shot_row["first_frame_desc"] if "first_frame_desc" in shot_row.keys() else "") or "",
        last_frame_desc=(shot_row["last_frame_desc"] if "last_frame_desc" in shot_row.keys() else "") or "",
        source_excerpt=shot_row["source_excerpt"] or "",
        narration=shot_row["narration"], dialogues=json.loads(shot_row["dialogues"] or "[]"),
        transition=shot_row["transition"] or "硬切",
        continuity_from_prev=bool(shot_row["continuity_from_prev"]),
        continuity_mode=(shot_row["continuity_mode"] if "continuity_mode" in shot_row.keys() else "") or "",
        observed_state_out=(shot_row["observed_state_out"] if "observed_state_out" in shot_row.keys() else "") or "",
    )
    if "shot_contract_json" in shot_row.keys() and shot_row["shot_contract_json"]:
        apply_shot_contract(shot_model, shot_row["shot_contract_json"])
    return shot_model


def _video_qa_contract(shot_model, bible_names: list[str]) -> dict[str, Any]:
    """Project narrative state onto the exact visible-cast contract sent to video.

    Story continuity may mention an off-screen listener and even describe their
    reaction.  Those narrative facts must not become QA requirements when the
    paid prompt explicitly forbids that listener from appearing.
    """
    from app.compiler import project_visual_contract_to_visible_cast
    from app.continuity import (
        derive_continuity_mode,
        effective_characters_visible,
        effective_primary_action,
        effective_state_in,
        planned_state_out,
        structured_state_prompt,
    )

    primary_action = effective_primary_action(shot_model)
    full_action = (shot_model.action_desc or primary_action).strip()
    state_in = effective_state_in(shot_model)
    state_out = planned_state_out(shot_model)
    visible_names = effective_characters_visible(shot_model)
    mode = (shot_model.continuity_mode or derive_continuity_mode(shot_model)).strip()
    state_in, state_out, primary_action, full_action, visible_contract = (
        project_visual_contract_to_visible_cast(
            shot_model,
            state_in=state_in,
            state_out=state_out,
            primary_action=primary_action,
            full_action=full_action,
            visible_names=visible_names,
            bible_names=bible_names,
            continuity_mode=mode,
        )
    )
    action_desc = full_action or primary_action
    if visible_contract:
        action_desc = f"{action_desc}\n画内角色合同：{visible_contract}"
    required_dialogue = "\n".join(
        f"{dialogue.speaker}：{dialogue.line}"
        for dialogue in (shot_model.dialogues or [])
        if (dialogue.line or "").strip()
    )
    from app.continuity import required_text_strategy

    required_text = ""
    if shot_model.required_text and required_text_strategy(shot_model) == "embedded_prop":
        required_text = (
            shot_model.required_text.exact_text or shot_model.required_text.surface or ""
        ).strip()
    structured_state = structured_state_prompt(shot_model)
    tracked_props = bool(
        shot_model.continuity_state_in.props or shot_model.continuity_state_out.props
    )
    tracked_axis = bool(
        (shot_model.continuity_state_in.scene.axis_id or "").strip()
        or (shot_model.continuity_state_out.scene.axis_id or "").strip()
    )
    return {
        "action_desc": action_desc,
        "state_in": state_in,
        "state_out": state_out,
        "visible_cast_contract": visible_contract,
        "required_dialogue": required_dialogue,
        "required_text": required_text,
        "structured_state": structured_state,
        "tracked_props": tracked_props,
        "tracked_axis": tracked_axis,
    }


async def critique_version(version_id: str) -> list[str]:
    """取某视频版本的问题清单（AI 评语）：优先用已存的 QA issues；
    若该版本还没质检过，则现场抽帧跑一次 VLM 评审，并回存。供「带评语重生」避免重复犯错。"""
    conn = get_conn()
    v = conn.execute("SELECT * FROM shot_versions WHERE id=?", (version_id,)).fetchone()
    if not v:
        return []
    if v["qa_json"]:
        issues = (json.loads(v["qa_json"]) or {}).get("issues") or []
        if issues:
            return list(issues)
    if not v["video_path"] or not Path(v["video_path"]).exists():
        return []
    shot = conn.execute("SELECT * FROM shots WHERE id=?", (v["shot_id"],)).fetchone()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (shot["episode_id"],)).fetchone()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    try:
        from app.stages import qa_shot
        bible = json.loads(project["bible_json"])
        anchor_map = {c["name"]: c["appearance_canonical"] for c in bible["characters"]}
        anchors = [anchor_map[n] for n in json.loads(shot["characters"] or "[]") if n in anchor_map]
        shot_model = _video_qa_shot_model(shot)
        qa_contract = _video_qa_contract(shot_model, list(anchor_map))
        frames = _extract_qa_frames(v["video_path"], high_risk=_shot_high_risk_for_qa(shot))
        if not frames:
            return []
        meta = json.loads(v["image_inputs"] or "{}") if v["image_inputs"] else {}
        if meta.get("reference_set_id"):
            try:
                from app.media_pipeline.reference_store import apply_set_to_meta
                meta = apply_set_to_meta(meta, meta["reference_set_id"], conn=conn)
            except Exception:
                pass
        visual_anchors = _visual_anchors_from_version_meta(meta)
        qa = await qa_shot(
            frames, qa_contract["action_desc"], shot["scene_setting"], anchors,
            state_in=qa_contract["state_in"], state_out=qa_contract["state_out"],
            structured_state=qa_contract["structured_state"],
            tracked_props=qa_contract["tracked_props"],
            tracked_axis=qa_contract["tracked_axis"],
            visual_anchors=visual_anchors,
        )
        _set_version(version_id, qa_json=json.dumps(qa, ensure_ascii=False))
        return list(qa.get("issues") or [])
    except Exception:  # noqa: BLE001 评语失败不阻塞重生
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
            "UPDATE jobs SET status=?, error=?, updated_at=?, lease_owner=NULL, lease_expires_at=NULL "
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
    conn.commit()
    row = conn.execute("SELECT run_id, step_run_id FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row:
        mark_media_job_state(row["run_id"], row["step_run_id"], status, error)
    return True


def _set_version(version_id: str, **fields) -> None:
    conn = get_conn()
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE shot_versions SET {cols} WHERE id=?", (*fields.values(), version_id))
    conn.commit()


def _is_seedance_text_sensitive(message: str | None) -> bool:
    text = (message or "").lower()
    if (
        "inputimagesensitivecontentdetected" in text
        or "input image" in text
        or "输入图片" in (message or "")
        or "输入图像" in (message or "")
    ):
        return False
    return (
        "inputtextsensitivecontentdetected" in text
        or "sensitive information" in text
        or "sensitive content" in text
        or "输入文本" in (message or "")
        or "敏感" in (message or "")
    )


def _is_seedance_input_image_sensitive(message: str | None) -> bool:
    text = (message or "").lower()
    original = message or ""
    return (
        "inputimagesensitivecontentdetected" in text
        or (
            "input image" in text
            and (
                "real person" in text
                or "privacyinformation" in text
                or "privacy information" in text
            )
        )
        or (
            ("输入图片" in original or "输入图像" in original)
            and ("真人" in original or "隐私" in original or "敏感" in original)
        )
    )


def _video_model_rejection_guidance(
    meta: dict[str, Any],
    message: str,
) -> tuple[str, str] | None:
    """Return an actionable public error only for an explicit model rejection."""
    mode = str(meta.get("mode") or meta.get("planned_mode") or "")
    if _is_seedance_input_image_sensitive(message):
        mode_label = {
            video_modes.FIRST_LAST_FRAME_MODE: "首尾帧",
            video_modes.REFERENCE_IMAGE_MODE: "参考图",
            video_modes.VIDEO_INPUT_MODE: "参考视频",
        }.get(mode, "视觉")
        return (
            "VIDEO_INPUT_PRIVACY_REJECTED",
            f"当前视频模型拒绝了{mode_label}输入，系统已保持 {mode or '原计划模式'} "
            "失败且没有切换生成方式。请到「系统设置 → 模型中心」更换视频模型后重试。",
        )
    if _is_seedance_text_sensitive(message):
        return (
            "VIDEO_TEXT_REJECTED",
            f"当前视频模型拒绝了文本输入，系统已保持 {mode or '原计划模式'} "
            "失败且没有切换生成方式。请修订输入或更换视频模型后重试。",
        )
    if _is_seedance_copyright_restricted(message):
        return (
            "VIDEO_COPYRIGHT_REJECTED",
            f"当前视频模型终态拒绝了生成请求，系统已保持 {mode or '原计划模式'} "
            "失败且没有切换生成方式。请更换视频模型或处理版权输入后重试。",
        )
    return None


_SEEDANCE_COPYRIGHT_MAX_RETRIES = 2


def _is_seedance_copyright_restricted(message: str | None) -> bool:
    text = (message or "").lower()
    return "copyright" in text or "版权" in (message or "")


def _provider_submitted_at(conn, job, task_id: str) -> float:
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
    conn.execute(
        "UPDATE jobs SET provider_submitted_at=? WHERE id=?",
        (submitted_at, job["id"]),
    )
    conn.commit()
    return submitted_at


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
               lease_owner=NULL, lease_expires_at=NULL
           WHERE id=? AND status='running' AND lease_owner=?""",
        (message, now(), job["id"], owner),
    )
    if changed.rowcount == 1:
        conn.execute(
            "UPDATE shot_versions SET status='paused_budget', error=? WHERE id=?",
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
               provider_non_cancellable=0, provider_submitted_at=NULL, updated_at=?
           WHERE id=?""",
        (operation_id, now(), job_id),
    )
    conn.commit()


def _ip_genericization_terms(conn, project_id: str) -> tuple[tuple[str, str], ...]:
    """把版权角色专名替换成中性代称（角色甲/乙…），降低 Seedance 输出版权误判概率。
    仅在平台已返回版权限制后的自动重提里使用。"""
    project = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project or not project["bible_json"]:
        return ()
    try:
        chars = json.loads(project["bible_json"]).get("characters", [])
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    labels = "甲乙丙丁戊己庚辛壬癸"
    names = sorted({(c.get("name") or "").strip() for c in chars if (c.get("name") or "").strip()},
                   key=len, reverse=True)  # 先长后短，避免短名先替换截断长名
    return tuple((name, f"角色{labels[i]}" if i < len(labels) else f"角色{i + 1}")
                 for i, name in enumerate(names))


def _video_image_inputs_from_meta(meta: dict) -> list[tuple[str, str]]:
    try:
        return video_modes.build_seedance_image_inputs(meta)
    except ProviderError as exc:
        if meta.get("mode") in {
            video_modes.REFERENCE_IMAGE_MODE,
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
            # prompt_ready 时画廊通常只有人物/场景 evidence，尚未产出关键帧。
            # 这里只校验 checkpoint 合同版本，不能套用「最终画廊必须有关键帧」的门禁。
            checkpoint_matches = (
                str(meta.get("keyframe_prompt_contract_version") or "")
                == video_modes.KEYFRAME_PROMPT_CONTRACT_VERSION
            )
            if not checkpoint_matches:
                _invalidate_reference_checkpoint("keyframe_prompt_checkpoint_contract_invalid")
            elif (
                meta.get("reference_static_ready")
                and not video_modes.reference_gallery_matches_keyframe_contract(meta)
            ):
                # static_ready 意味着必需关键帧已经落盘；若只剩 evidence
                # 或 path 已丢失，必须回到生成阶段，不能被连续性快路伪装完成。
                _invalidate_reference_checkpoint("static_keyframe_contract_or_file_invalid")
        else:
            gallery_matches = video_modes.reference_gallery_matches_keyframe_contract(meta)
            if gallery_matches:
                complete_gallery_candidate = True
            else:
                _invalidate_reference_checkpoint("keyframe_prompt_contract_or_file_invalid")
    from app.schemas import Bible
    from app.media_pipeline import stages as media_stages
    from app.media_pipeline.stage_state import set_pipeline_stage
    from app.continuity import derive_continuity_mode, uses_previous_tail_frame

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
    needs_tail = (
        not meta.get("shot_plan_id")
        and uses_previous_tail_frame(derive_continuity_mode(shot_model, prev=prev_shot))
    )
    current_keyframe_fingerprint = video_modes.keyframe_contract_fingerprint(
        shot_model, bible, screenplay=screenplay,
    )
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
        if not video_modes.reference_gallery_matches_keyframe_contract(
            meta, expected_fingerprint=current_keyframe_fingerprint,
        ):
            _invalidate_reference_checkpoint("shot_keyframe_contract_changed")
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
        elif not video_modes.reference_gallery_matches_keyframe_contract(meta):
            # 静态预取点可能在 worker 崩溃后只剩 evidence，或关键帧文件已丢失。
            # 连续性快路不能只装配尾帧就把这组资产标成完成。
            _invalidate_reference_checkpoint("static_keyframe_contract_or_file_invalid")
    rejection_details: list[dict[str, Any]] = []
    rejected_assets: list = []

    from app.multiview import narrative_keyframe_required, PURPOSE_VIDEO_INPUT, purpose_list

    def _reference_keyframe_gate_passed(current_assets: list) -> bool:
        """Validate the files returned by the builder, not only its DB checkpoint."""
        refs = [a.public_dict() for a in current_assets]

        def _has_usable_keyframe(ref: dict) -> bool:
            path = str(ref.get("path") or ref.get("image_path") or "").strip()
            url = str(ref.get("url") or "").strip()
            return (
                (
                    str(ref.get("type") or "") == "plot_key_frame"
                    or video_modes.is_narrative_keyframe_slot(str(ref.get("slot_key") or ""))
                )
                and not ref.get("deleted")
                and ref.get("selectedForSeedance")
                and PURPOSE_VIDEO_INPUT in purpose_list(ref)
                and ((bool(path) and Path(path).is_file()) or url.startswith("data:image"))
            )

        keyframe_ok = any(_has_usable_keyframe(ref) for ref in refs if isinstance(ref, dict))
        if isinstance(meta.get("keyframe_sequence"), dict):
            # The sequence contract also understands the explicit structural
            # fallback where all candidates failed hard-shape QA.
            keyframe_ok = video_modes.reference_gallery_matches_keyframe_contract(
                {**meta, "reference_images": refs},
                expected_fingerprint=current_keyframe_fingerprint,
            )
        return not meta.get("narrative_keyframe_missing") and (
            not narrative_keyframe_required() or keyframe_ok
        )

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
                "unit": "keyframe_candidates",
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
            if not video_modes.reference_gallery_matches_keyframe_contract(
                assembled_meta, expected_fingerprint=current_keyframe_fingerprint,
            ):
                _invalidate_reference_checkpoint("continuity_assembly_keyframe_missing_or_stale")
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
            prompt_text = video_modes.append_reference_prompt_notes(prompt_text, assets)
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
                "参考图关键帧文件或生成前质量门禁未通过"
            )
        meta["reference_group_gate_passed"] = True
        meta["video_input_manifest_frozen"] = True
        meta.pop("first_frame_path", None)
        meta.pop("last_frame_path", None)
        meta.pop("first_frame_scene_id", None)
        meta.pop("last_frame_scene_id", None)
        prompt_text = video_modes.append_reference_prompt_notes(prompt_text, assets)
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
    meta["narrative_keyframe_missing"] = True
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
        reason_code="PREPARING_BOUNDARY_FRAMES",
        reason_text="正在按真实首帧生成连续尾帧",
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
            asset = await video_modes._generate_one_reference(
                project_id=job["project_id"],
                episode_no=ep["episode_no"],
                shot=shot_model,
                bible=bible,
                ref_type="plot_key_frame",
                index=index + pair_attempt - 1,
                content_override=description,
                seed_inputs=seed_inputs,
                extra_instruction=(
                    "FIRST/LAST CONTINUITY ENDPOINT: reference image 1 is the exact "
                    "first-frame boundary for this shot. Render only the contracted "
                    "final state reached after one physically continuous take. Preserve "
                    "identity, outfit, environment, fixed landmarks, screen direction, "
                    "and scale from that boundary while allowing the scripted action and "
                    "camera relation to progress. Do not blend endpoints, morph faces, "
                    "merge people, teleport, hard-cut, or copy the start pose. "
                    f"Edit relation: {relation_edit}; "
                    f"action relation: {relation_action}."
                ),
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

    tail_attempt_limit = max(1, min(3, int(shot_plan.max_attempts or 1)))
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
    first_seed = hiagent.data_url_from_file(first_path)
    tail_seed_inputs = list(dict.fromkeys([
        first_seed,
        *boundary_seed_inputs[:3],
    ]))
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
                pair_start_fingerprint=first_fingerprint,
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
                pair_start_fingerprint=first_fingerprint,
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
        "tail_conditioned_on_first_frame": True,
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


async def _prepare_planned_mode_inputs(
    conn, job, version, shot, ep, meta: dict, prompt_text: str,
    *, lease_owner: str,
) -> tuple[dict, str]:
    mode = meta.get("mode") or video_modes.REFERENCE_IMAGE_MODE
    # Reference/keyframe generation, boundary-frame generation, and provider
    # media publication can all incur external work before the final video
    # submit.  One mode-neutral authority fence must therefore run before mode
    # dispatch, not merely before create_video_task.
    selected_plan = _assert_video_provider_submission_authority(
        conn,
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
    conn = get_conn()
    owner = lease_owner or f"direct-{id(asyncio.current_task())}"
    if lease_owner is None:
        if not media_scheduler.claim_job(job_id, owner, lease_seconds=180.0):
            return
        run_row = conn.execute(
            "SELECT run_id, step_run_id FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if run_row:
            mark_media_job_state(run_row["run_id"], run_row["step_run_id"], "running")
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job or job["status"] != "running" or job["lease_owner"] != owner:
        return
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

    started = time.time()
    try:
        _assert_review_dependency_fence(job, version["id"], "worker_start")
        provider_operation_id = (
            _row_value(job, "provider_operation_id")
            or f"video-create-{version['id']}"
        )
        task_id = version["provider_task_id"]
        recovered_at = None
        if not task_id:
            recovered = _recover_paid_video_task(conn, provider_operation_id)
            if recovered:
                task_id, recovered_at = recovered
                conn.execute(
                    "UPDATE shot_versions SET provider_task_id=? WHERE id=?",
                    (task_id, version["id"]),
                )
                conn.execute(
                    """UPDATE jobs
                       SET provider_operation_id=?, provider_create_state='accepted',
                           provider_non_cancellable=1, provider_submitted_at=?,
                           updated_at=?
                       WHERE id=?""",
                    (provider_operation_id, recovered_at, now(), job_id),
                )
                conn.commit()
        provider_submitted_at = (
            recovered_at
            if recovered_at is not None
            else (_provider_submitted_at(conn, job, task_id) if task_id else None)
        )
        result = None
        if task_id:
            conn.execute(
                "UPDATE jobs SET provider_operation_id=?, provider_create_state='accepted', "
                "provider_non_cancellable=1 WHERE id=?",
                (provider_operation_id, job_id),
            )
            conn.commit()
        _set_version(version["id"], status="running", error=None)
        prompt_text = ensure_source_excerpt_in_prompt(version["prompt_text"], _load_shot_model(shot))
        if prompt_text != version["prompt_text"]:
            _set_version(version["id"], prompt_text=prompt_text)
        try:
            if not task_id:
                meta, prompt_text = await _await_with_job_lease_heartbeat(
                    _prepare_planned_mode_inputs(
                        conn, job, version, shot, ep, meta, prompt_text,
                        lease_owner=owner,
                    ),
                    job_id=job_id,
                    owner=owner,
                )
        except _ContinuityWait as wait_exc:
            wait = 15.0
            note = wait_exc.reason
            from app.media_pipeline import stages as media_stages
            from app.media_pipeline.stage_state import set_pipeline_stage
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
        _assert_review_dependency_fence(
            job, version["id"], "provider_input_adoption",
        )

        # 连续镜调度级依赖：无可用尾帧时不得提交 Seedance
        if job["after_shot_id"] and not task_id:
            from app.media_pipeline.scheduler import continuity_anchor_ready
            from app.media_pipeline import stages as media_stages
            from app.media_pipeline.stage_state import set_pipeline_stage
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
            from app.media_pipeline.stage_state import set_pipeline_stage
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

        safety_retry_used = bool(meta.get("seedance_safety_retry"))
        copyright_retries = int(meta.get("seedance_copyright_retries") or 0)
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
                    from app.media_pipeline.stage_state import set_pipeline_stage
                    set_pipeline_stage(job_id, media_stages.STAGE_VIDEO_SUBMITTING, conn=conn)
                    conn.execute(
                        "UPDATE jobs SET provider_operation_id=?, provider_create_state='submitting', "
                        "updated_at=? WHERE id=?",
                        (provider_operation_id, now(), job_id),
                    )
                    conn.commit()
                    from app.media_pipeline.concurrency import (
                        report_congestion, report_healthy, semaphore_for,
                    )
                    async with semaphore_for(media_stages.RESOURCE_VIDEO_SUBMIT):
                        _assert_job_lease(job_id, owner)
                        _assert_review_dependency_fence(
                            job, version["id"], "provider_submit",
                        )
                        _assert_video_provider_submission_authority(
                            conn,
                            job=job,
                            meta=meta,
                            actual_mode=actual_mode,
                            write_point="provider_non_cancellable",
                        )
                        from app.completion_grant import reserve_provider_video_budget

                        budget_claimed = reserve_provider_video_budget(
                            episode_id=str(job["episode_id"]),
                            job_id=job_id,
                            version_id=str(version["id"]),
                            operation_id=provider_operation_id,
                            amount_cny=shot_cost_cny(int(shot["duration_s"] or 0)),
                            conn=conn,
                        )
                        if budget_claimed is False:
                            raise VideoBudgetAuthorizationError(
                                "本次供应商视频调用将超过用户已批准的费用上限；"
                                "任务已在付费调用前暂停"
                            )
                        marked = conn.execute(
                            """UPDATE jobs
                               SET provider_non_cancellable=1, updated_at=?
                               WHERE id=? AND status='running' AND lease_owner=?
                                 AND cancellation_requested=0""",
                            (now(), job_id, owner),
                        )
                        if marked.rowcount != 1:
                            conn.rollback()
                            raise LeaseLost(
                                f"video submit cancelled before provider call: {job_id}"
                            )
                        conn.commit()
                        try:
                            try:
                                _assert_video_provider_submission_authority(
                                    conn,
                                    job=job,
                                    meta=meta,
                                    actual_mode=actual_mode,
                                    write_point="provider_create",
                                )
                            except VideoPlanStaleFence:
                                # The provider has not been called yet. Undo the
                                # short cancellation lock so recovery never
                                # mistakes this fenced job for accepted paid work.
                                conn.execute(
                                    """UPDATE jobs
                                          SET provider_non_cancellable=0,
                                              provider_create_state='not_started',
                                              updated_at=?
                                        WHERE id=? AND provider_create_state='submitting'""",
                                    (now(), job_id),
                                )
                                conn.commit()
                                raise
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
                                    "version_id": version["id"],
                                    "version_no": version["version_no"],
                                    "operation_id": provider_operation_id,
                                })
                            from app.completion_grant import mark_provider_video_budget_claim

                            mark_provider_video_budget_claim(
                                provider_operation_id,
                                "accepted",
                                conn=conn,
                            )
                            conn.commit()
                            report_healthy(media_stages.RESOURCE_VIDEO_SUBMIT)
                        except ProviderError as submit_exc:
                            if getattr(submit_exc, "retryable", False) or "429" in str(submit_exc):
                                report_congestion(media_stages.RESOURCE_VIDEO_SUBMIT, reason="submit")
                            raise
                    _assert_job_lease(job_id, owner)
                except ProviderError as exc:
                    _assert_job_lease(job_id, owner)
                    if not exc.retryable:
                        from app.completion_grant import mark_provider_video_budget_claim

                        mark_provider_video_budget_claim(
                            provider_operation_id,
                            "released",
                            conn=conn,
                        )
                    conn.execute(
                        "UPDATE jobs SET provider_create_state=?, provider_non_cancellable=?, "
                        "updated_at=? WHERE id=?",
                        (
                            "unknown" if exc.retryable else "not_started",
                            int(bool(exc.retryable)),
                            now(),
                            job_id,
                        ),
                    )
                    conn.commit()
                    if (
                        not exc.retryable
                        and _is_seedance_text_sensitive(str(exc))
                        and not safety_retry_used
                    ):
                        prompt_text = sanitize_seedance_prompt(prompt_text, aggressive=True)
                        safety_retry_used = True
                        provider_operation_id = f"video-create-{version['id']}-safety-1"
                        meta["seedance_safety_retry"] = True
                        meta["seedance_safety_reason"] = str(exc)[:300]
                        if not _reserve_or_pause_video_resubmit(
                            job, version, shot, owner,
                        ):
                            return
                        _persist_video_resubmit(
                            conn, job_id=job_id, version_id=version["id"],
                            prompt_text=prompt_text, meta=meta,
                            operation_id=provider_operation_id,
                        )
                        continue
                    if (
                        not exc.retryable
                        and _is_seedance_copyright_restricted(str(exc))
                        and copyright_retries < _SEEDANCE_COPYRIGHT_MAX_RETRIES
                    ):
                        copyright_retries += 1
                        provider_operation_id = (
                            f"video-create-{version['id']}-copyright-{copyright_retries}"
                        )
                        if copyright_retries == 1:
                            prompt_text = sanitize_seedance_prompt(
                                prompt_text, aggressive=True,
                                extra_terms=_ip_genericization_terms(conn, job["project_id"]))
                        meta["seedance_copyright_retries"] = copyright_retries
                        meta["seedance_copyright_reason"] = str(exc)[:300]
                        if not _reserve_or_pause_video_resubmit(
                            job, version, shot, owner,
                        ):
                            return
                        _persist_video_resubmit(
                            conn, job_id=job_id, version_id=version["id"],
                            prompt_text=prompt_text, meta=meta,
                            operation_id=provider_operation_id,
                        )
                        continue
                    raise
                # Persist the paid provider handle and the non-cancellable flag in
                # one local transaction. The stable Idempotency-Key covers the
                # unavoidable provider-accepted/local-commit crash window.
                conn.execute(
                    "UPDATE shot_versions SET provider_task_id=? WHERE id=?",
                    (task_id, version["id"]),
                )
                conn.execute(
                    "UPDATE jobs SET provider_operation_id=?, provider_create_state='accepted', "
                    "provider_non_cancellable=1, provider_submitted_at=?, updated_at=? WHERE id=?",
                    (provider_operation_id, now(), now(), job_id),
                )
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
                    from app.media_pipeline.stage_state import set_pipeline_stage
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
                _assert_review_dependency_fence(
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
                    if getattr(poll_exc, "retryable", False) or "429" in str(poll_exc):
                        report_congestion(media_stages.RESOURCE_VIDEO_POLL, reason="poll")
                    raise
            _assert_job_lease(job_id, owner)
            if result is None or result["status"] not in ("succeeded", "failed"):
                provider_age = time.time() - float(provider_submitted_at or time.time())
                if provider_age >= config.VIDEO_PROVIDER_MAX_WAIT:
                    raise ProviderError(
                        f"供应商任务 {task_id} 已持续运行 "
                        f"{provider_age / 3600:.1f} 小时，超过系统保护上限；"
                        "任务可能卡在上游，请联系供应商核查"
                    )
                if _defer_provider_poll(job_id, task_id, lease_owner=owner):
                    return
                raise LeaseLost(f"provider poll defer lost lease: {job_id} / {owner}")
            if result["status"] == "failed":
                error_text = result["error"][:400]
                if _is_seedance_text_sensitive(error_text) and not safety_retry_used:
                    prompt_text = sanitize_seedance_prompt(prompt_text, aggressive=True)
                    safety_retry_used = True
                    task_id = None
                    provider_operation_id = f"video-create-{version['id']}-safety-1"
                    meta["seedance_safety_retry"] = True
                    meta["seedance_safety_reason"] = error_text
                    if not _reserve_or_pause_video_resubmit(
                        job, version, shot, owner,
                    ):
                        return
                    _persist_video_resubmit(
                        conn, job_id=job_id, version_id=version["id"],
                        prompt_text=prompt_text, meta=meta,
                        operation_id=provider_operation_id,
                    )
                    continue
                if _is_seedance_copyright_restricted(error_text) and copyright_retries < _SEEDANCE_COPYRIGHT_MAX_RETRIES:
                    copyright_retries += 1
                    provider_operation_id = (
                        f"video-create-{version['id']}-copyright-{copyright_retries}"
                    )
                    if copyright_retries == 1:  # 首次重提：去掉版权专名 + 激进改写，降低输出与原 IP 相似度
                        prompt_text = sanitize_seedance_prompt(
                            prompt_text, aggressive=True,
                            extra_terms=_ip_genericization_terms(conn, job["project_id"]))
                    task_id = None  # 再次重提靠重新生成的随机性（同一镜其它版本可成功即说明判定是概率性的）
                    meta["seedance_copyright_retries"] = copyright_retries
                    meta["seedance_copyright_reason"] = error_text
                    if not _reserve_or_pause_video_resubmit(
                        job, version, shot, owner,
                    ):
                        return
                    _persist_video_resubmit(
                        conn, job_id=job_id, version_id=version["id"],
                        prompt_text=prompt_text, meta=meta,
                        operation_id=provider_operation_id,
                    )
                    continue
                raise ProviderError(f"Seedance 任务失败：{error_text}")
            break

        _assert_job_lease(job_id, owner)
        meta["provider_video_source_url"] = result["video_url"]
        # Current provider contract advertises a seven-day URL. Keep a
        # conservative six-day reuse window so downstream jobs never race expiry.
        meta["provider_video_source_url_expires_at"] = now() + 6 * 24 * 3600
        dest = _video_path(job["project_id"], ep["episode_no"], shot["shot_no"], version["version_no"])
        await hiagent.download(result["video_url"], str(dest))
        _assert_job_lease(job_id, owner)
        if meta.get("shot_plan_id"):
            from app.video_plan import active_plan_is_current
            if not active_plan_is_current(str(meta["shot_plan_id"]), conn=conn):
                raise VideoPlanStaleFence("视频生成完成时计划已失效，候选已隔离")
        supervisor_owner = _row_value(job, "owner_run_id")
        if supervisor_owner:
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
        _assert_review_dependency_fence(job, version["id"], "candidate")
        latency = round(time.time() - started, 1)
        paid_attempts = max(
            1,
            int(meta.get("provider_paid_attempts") or 0),
            _paid_video_attempt_count(conn, version["id"]),
        )
        meta["provider_paid_attempts"] = paid_attempts
        cost = shot_cost_cny(shot["duration_s"]) * paid_attempts
        _set_version(version["id"], status="succeeded", error=None, video_path=str(dest),
                     last_frame_url=result["last_frame_url"], cost_cny=cost, latency_s=latency,
                     image_inputs=json.dumps(meta, ensure_ascii=False))
        from app.completion_grant import mark_provider_video_budget_claim

        mark_provider_video_budget_claim(provider_operation_id, "settled")
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
        _assert_review_dependency_fence(job, version["id"], "candidate_evidence")
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
                enqueue_shot(
                    job["shot_id"],
                    reroll=True,
                    after_shot_id=job["after_shot_id"],
                    auto_retake_count=resubmits + 1,
                    dependency_snapshot=meta.get("review_dependency_snapshot"),
                )
                # 标记新版本的 technical_resubmit_count（尽力而为）
                try:
                    new_ver = get_conn().execute(
                        """SELECT id, image_inputs FROM shot_versions
                           WHERE shot_id=? ORDER BY version_no DESC LIMIT 1""",
                        (job["shot_id"],),
                    ).fetchone()
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
                if _set_job(job_id, "succeeded", lease_owner=owner):
                    media_scheduler.settle_budget(job_id, cost, success=True)
                    reconcile_episode_generation_status(job["episode_id"])
                return
            raise ProviderError("视频文件技术校验失败，候选不可采用")
        if not supervisor_controlled:
            _assert_review_dependency_fence(job, version["id"], "adoption_relation")
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
    except VideoBudgetAuthorizationError as exc:
        message = str(exc)
        conn.execute(
            """UPDATE jobs
                  SET status='paused_budget',error=?,reason_code='VIDEO_BUDGET_NOT_AUTHORIZED',
                      reason_text=?,provider_non_cancellable=0,
                      provider_create_state='not_started',
                      lease_owner=NULL,lease_expires_at=NULL,next_retry_at=NULL,
                      updated_at=?
                WHERE id=? AND status='running' AND lease_owner=?""",
            (message, message, now(), job_id, owner),
        )
        conn.execute(
            "UPDATE shot_versions SET status='paused_budget',error=? WHERE id=?",
            (message, version["id"]),
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
        _set_version(version["id"], status="stale", error=public)
        if _set_job(job_id, "stale", public, lease_owner=owner):
            media_scheduler.settle_budget(job_id, 0.0, success=False)
            reconcile_episode_generation_status(job["episode_id"])
            # A sibling-only replan may preserve this shot's complete execution
            # contract. Recover the accepted provider handle in place when that
            # equivalence can be proven; never issue another create call.
            recover_equivalent_stale_provider_jobs(job["episode_id"])
        return
    except ReviewDependencyFence as exc:
        public = str(exc)
        _set_version(version["id"], status="failed", error=public)
        if _set_job(job_id, "failed", public, lease_owner=owner):
            media_scheduler.settle_budget(job_id, 0.0, success=False)
            reconcile_episode_generation_status(job["episode_id"])
        return
    except VideoInputRepairRequired as exc:
        repair_mode = str(
            meta.get("mode") or meta.get("planned_mode")
            or video_modes.REFERENCE_IMAGE_MODE
        )
        repair_label = {
            video_modes.FIRST_LAST_FRAME_MODE: "首尾帧",
            video_modes.REFERENCE_IMAGE_MODE: "参考图",
            video_modes.VIDEO_INPUT_MODE: "参考视频",
        }.get(repair_mode, "视频输入")
        repair_code = {
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
        conn.execute(
            """UPDATE jobs
                  SET status='waiting_human',error=?,
                      reason_code=?,
                      reason_text=?,lease_owner=NULL,lease_expires_at=NULL,
                      next_retry_at=NULL,updated_at=?
                WHERE id=? AND lease_owner=?""",
            (message, repair_code, message, now(), job_id, owner),
        )
        conn.execute(
            "UPDATE shot_versions SET status='waiting_human',error=? WHERE id=?",
            (message, version["id"]),
        )
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
    except (ProviderError, Exception) as exc:  # noqa: BLE001 失败要响：原文进日志，前端给码+分类
        if not media_scheduler.renew_lease(job_id, owner, lease_seconds=180.0):
            return
        record = errors.log_error(
            exc, action="shot_video_generate",
            context={"shot_id": job["shot_id"], "version_id": version["id"], "job_id": job_id})
        guidance = (
            _video_model_rejection_guidance(meta, str(exc))
            if isinstance(exc, ProviderError)
            else None
        )
        reason_code = guidance[0] if guidance else None
        public = (
            f"{guidance[1]}（{guidance[0]} · {record.error_id}）"
            if guidance else record.public
        )
        # 上游瞬时故障（超时/网络/限流/5xx）先 job 级延迟重排，扛过分钟级抖动；
        # 重试次数耗尽或不可重试的错误才永久判失败。
        if isinstance(exc, ProviderError) and _schedule_job_retry(
            job_id, exc, lease_owner=owner
        ):
            _set_version(version["id"], status="queued")
            return
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
        if reason_code:
            conn.execute(
                "UPDATE jobs SET reason_code=?,reason_text=? WHERE id=?",
                (reason_code, public, job_id),
            )
        conn.commit()
        _set_version(version["id"], status="failed", error=public)
        if _set_job(job_id, "failed", public, lease_owner=owner):
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
    """旁路评分现有视频，不得据此创建新版本或延迟采用。"""
    del allow_autonomous_retake  # 兼容旧调用签名；QA 已永久退出控制流。
    if get_setting("auto_qa") != "true" or not shutil.which("ffmpeg"):
        return True
    conn = get_conn()
    try:
        shot = conn.execute("SELECT * FROM shots WHERE id=?", (job["shot_id"],)).fetchone()
        project = conn.execute("SELECT * FROM projects WHERE id=?", (job["project_id"],)).fetchone()
        bible = json.loads(project["bible_json"])
        anchor_map = {c["name"]: c["appearance_canonical"] for c in bible["characters"]}
        anchors = [anchor_map[n] for n in json.loads(shot["characters"] or "[]") if n in anchor_map]
        from app.stages import qa_shot
        from app.continuity import classify_video_hard_failures
        shot_model = _video_qa_shot_model(shot)
        qa_contract = _video_qa_contract(shot_model, list(anchor_map))
        frames = _extract_qa_frames(
            video_path, high_risk=_shot_high_risk_for_qa(shot, shot_model),
        )
        version_row = conn.execute("SELECT * FROM shot_versions WHERE id=?", (version_id,)).fetchone()
        meta_for_qa = json.loads(version_row["image_inputs"] or "{}") if version_row and version_row["image_inputs"] else {}
        if meta_for_qa.get("reference_set_id"):
            try:
                from app.media_pipeline.reference_store import apply_set_to_meta
                meta_for_qa = apply_set_to_meta(meta_for_qa, meta_for_qa["reference_set_id"], conn=conn)
            except Exception:
                pass
        visual_anchors = _visual_anchors_from_version_meta(meta_for_qa)
        _assert_review_dependency_fence(job, version_id, "qa_start")
        qa = await qa_shot(
            frames,
            qa_contract["action_desc"],
            shot["scene_setting"],
            anchors,
            state_in=qa_contract["state_in"],
            state_out=qa_contract["state_out"],
            required_dialogue=qa_contract["required_dialogue"],
            required_text=qa_contract["required_text"],
            structured_state=qa_contract["structured_state"],
            tracked_props=qa_contract["tracked_props"],
            tracked_axis=qa_contract["tracked_axis"],
            duration_s=int(shot_model.duration_s or shot["duration_s"] or 0) or None,
            duration_needs_review=(
                "duration_gt5_needs_review" in (shot_model.risk_tags or [])
                or int(shot_model.duration_s or shot["duration_s"] or 0) > 5
            ),
            visual_anchors=visual_anchors,
        )
        dependency = meta_for_qa.get("review_dependency_snapshot") or {}
        if dependency.get("asset_soft_warnings"):
            qa["input_asset_soft_warnings"] = dependency["asset_soft_warnings"]
        if dependency.get("asset_inputs"):
            qa["input_asset_qualification"] = dependency["asset_inputs"]
        version = conn.execute("SELECT * FROM shot_versions WHERE id=?", (version_id,)).fetchone()
        hard_failures = classify_video_hard_failures(
            qa,
            technical=json.loads(version["technical_validation_json"] or "{}") if version else {},
        )
        qa_unavailable = bool(
            qa.get("overall") is None
            or qa.get("status") == "unverified"
            or qa.get("qa_recovered")
        )
        qa["evaluation_role"] = "score_only"
        qa["runtime_blocking"] = False
        qa["retry_eligible"] = False
        qa["score_status"] = "unavailable" if qa_unavailable else "scored"
        _assert_review_dependency_fence(job, version_id, "qa_result")
        _set_version(version_id, qa_json=json.dumps(qa, ensure_ascii=False))
        log_provider_call(
            "vlm_qa", config.MODEL_VLM, "QA_SCORE_ONLY", None, 0,
            meta={
                "shot_id": job["shot_id"],
                "version_id": version_id,
                "overall": qa.get("overall"),
                "score_status": qa.get("score_status"),
                "hard_failures": hard_failures,
                "retake": False,
            },
        )
        return True
    except ReviewDependencyFence:
        raise
    except Exception as exc:  # noqa: BLE001 QA 异常只记录，不影响已落盘的视频
        _set_version(
            version_id,
            qa_json=json.dumps({
                "overall": None,
                "issues": [f"质检未完成：{exc}"],
                "evaluation_role": "score_only",
                "score_status": "unavailable",
                "runtime_blocking": False,
                "retry_eligible": False,
                "diagnostic": "qa_exception",
            }, ensure_ascii=False),
        )
        log_provider_call("vlm_qa", config.MODEL_VLM, "QA_ERROR", None, 0, error=str(exc))
        return True


def _extract_frames(video_path: str, *, high_risk: bool = False) -> list[str]:
    """ffmpeg 抽帧，返回 base64 列表。普通镜头首/中/尾 3 帧；高风险镜头 0/25/50/75/95% 五帧。"""
    from app.multiview import video_qa_sample_positions
    positions = video_qa_sample_positions(high_risk=high_risk)
    frames = []
    with tempfile.TemporaryDirectory() as td:
        dur = None
        for i, frac in enumerate(positions):
            out = Path(td) / f"f{i}.jpg"
            cmd = ["ffmpeg", "-y", "-loglevel", "error"]
            if frac <= 0.0:
                cmd += ["-i", video_path, "-vf", "select=eq(n\\,0)", "-vframes", "1"]
            else:
                if dur is None:
                    dur = float(subprocess.run(
                        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                         "-of", "csv=p=0", video_path],
                        capture_output=True, text=True, check=True).stdout.strip() or 5)
                ts = max(0.0, min(float(dur) * float(frac), max(float(dur) - 0.05, 0.0)))
                cmd += ["-ss", f"{ts:.2f}", "-i", video_path, "-vframes", "1"]
            cmd += ["-q:v", "4", str(out)]
            subprocess.run(cmd, check=True, capture_output=True)
            frames.append(hiagent.encode_image_file(str(out)))
    return frames


def _extract_qa_frames(video_path: str, *, high_risk: bool) -> list[str]:
    """兼容旧扩展/测试替身的一参数抽帧函数，同时保留高风险五帧策略。"""
    if not high_risk:
        return _extract_frames(video_path)
    try:
        return _extract_frames(video_path, high_risk=True)
    except TypeError as exc:
        if "high_risk" not in str(exc):
            raise
        return _extract_frames(video_path)


def _shot_high_risk_for_qa(shot_row, shot_model=None) -> bool:
    from app.multiview import shot_needs_high_risk_frame_sample
    if shot_model is not None and shot_needs_high_risk_frame_sample(shot_model):
        return True
    return shot_needs_high_risk_frame_sample(shot_row)


# ---------- worker 生命周期 ----------

async def _worker_loop(name: str, queue: asyncio.Queue[str] | None = None) -> None:
    work_queue = queue or _queue
    while True:
        job_id = await work_queue.get()
        try:
            claim = media_scheduler.claim_job(job_id, name, lease_seconds=180.0)
            if claim:
                row = get_conn().execute(
                    "SELECT run_id, step_run_id FROM jobs WHERE id=?", (job_id,)
                ).fetchone()
                if row:
                    mark_media_job_state(row["run_id"], row["step_run_id"], "running")
                await _run_job(job_id, lease_owner=name)
        except Exception as exc:  # noqa: BLE001 worker 永不死亡，但错误必须落库
            public = errors.record_and_format(exc, action="worker_loop", context={"job_id": job_id})
            if _set_job(job_id, "failed", public, lease_owner=name):
                media_scheduler.settle_budget(job_id, 0.0, success=False)
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
    """把一个卡住的媒体 job 复位回 queued，等待持久调度器接管：
    - running/queued job 统一回到 queued，清空旧 lease；持久化 retry 到期时间保留
    - Run 立即进入 WAITING_RETRY，监控页显示“恢复排队中”
    - 被中断的 Step 保持 FAILED 审计终态，并创建 iteration+1 的 READY attempt
    返回 True 表示实际复位过；False 表示 job 已不存在或被并发改动（调用方忽略）。"""
    cursor = conn.execute(
        "UPDATE jobs SET status='queued', lease_owner=NULL, lease_expires_at=NULL, "
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
    decommission_legacy_keyframe_jobs()
    conn = get_conn()
    rows = rows_to_dicts(conn.execute(
        """SELECT j.id AS job_id, j.run_id, j.step_run_id
           FROM jobs j
           JOIN workflow_runs wr ON wr.id=j.run_id
           WHERE j.status IN ('running','queued')
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
    if resumed:
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
    """周期修复没有 worker 能消费的业务级卡死状态。"""
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
    if redundant:
        conn.commit()
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
             AND (
               (status='waiting_retry' AND (next_retry_at IS NULL OR next_retry_at<=?))
               OR (
                 status='waiting_human'
                 AND reason_code='VIDEO_PREFLIGHT_BLOCKED'
                 AND retry_count<?
                 AND (error LIKE '%source_excerpt 原文内容%'
                      OR reason_text LIKE '%source_excerpt 原文内容%')
               )
             )
             AND cancellation_requested=0 AND abandoned=0
           ORDER BY updated_at LIMIT ?""",
        (stamp, int(config.VIDEO_PREFLIGHT_MAX_RETRIES), max(1, int(limit))),
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

    paused_budget_episodes = conn.execute(
        """SELECT DISTINCT episode_id FROM jobs
           WHERE kind='video' AND status='paused_budget'
             AND cancellation_requested=0 AND abandoned=0"""
    ).fetchall()
    for row in paused_budget_episodes:
        try:
            report["budget_resumed"] += retry_paused(str(row["episode_id"]))
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
                if resumed:
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

    def _resize(pool: list[asyncio.Task], target: int, prefix: str, queue: asyncio.Queue[str]) -> None:
        alive = [t for t in pool if not t.done()]
        pool.clear()
        pool.extend(alive)
        while len(pool) < target:
            pool.append(loop.create_task(_worker_loop(f"{prefix}{len(pool)}", queue)))
        while len(pool) > target:
            task = pool.pop()
            task.cancel()

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
        t.cancel()
    for t in (*_workers, *_video_ready_workers, *_poll_workers):
        try:
            await t
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _workers.clear()
    _video_ready_workers.clear()
    _poll_workers.clear()
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
