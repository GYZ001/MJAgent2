"""媒体作业授权判定：租约、供应商创建解析、复核依赖围栏（拆分自 ``run_job.py``）。

覆盖三类判据：(1) 谁能不阻塞事件循环地抢到一条 job 的 CAS 租约
（``_claim_job_without_blocking_loop``/``_authority_checks_can_use_worker_thread``/
``_connection_for_heartbeat_operation``/``_assert_job_lease``）；(2) 供应商
create 调用的结果是否已经落到可判定状态（``_provider_create_outcome_unknown``/
``_assert_provider_create_resolved``/``_release_pre_call_video_claim``）；(3) 提
交前的复核依赖与叙事权威快照是否仍然当前
（``_assert_review_dependency_fence[_async]``/
``_assert_current_storyboard_completion_authority``/
``_assert_video_provider_submission_authority[_async]``）。三类判据抛出的围栏
异常（``ProviderCreateUnresolved``/``ReviewDependencyFence``/
``VideoPlanStaleFence``）定义在 ``.fences``，本文件只判定、不重复定义。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app.db import get_conn, now, run_write_transaction
from app.hiagent import ProviderError
from app.orchestration import media_scheduler

from .common import LeaseLost
from .enqueue import _load_shot_model, _row_value
from .fences import (
    ProviderCreateUnresolved,
    ReviewDependencyFence,
    VideoPlanStaleFence,
)


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
        # 契约只看「哪个镜头用了哪个素材版本、门禁结果」：version_id/ref_id 是每次任务新生成的
        # 行 id，同一素材再入队就换一个。EP1 串接实测：链上相邻镜头先后重建参考图行，彼此把
        # 对方快照里的旧 ref_id 判成消失 → REVIEW_DEPENDENCY_STALE → 重入队 → 再互相打死。
        return {
            json.dumps(
                {key: value for key, value in item.items() if key not in {"version_id", "ref_id"}},
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

__all__ = [name for name in globals() if not name.startswith("__")]
