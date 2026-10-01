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
from app.media_exec.identity_fence import assert_identity_revision

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
    assert_identity_revision(conn, shot_id=str(row["shot_id"] if row else ""), meta=meta, write_point=write_point)
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
        # 直接从真源导入，不再借道 app.api 门面转手（CLAUDE.md「再导出门面
        # 不得再长，且必须从真源导出」）。必须保持函数内延迟导入，改成模块
        # 级已实测会炸循环导入：app.media_exec.authority(模块级)
        # -> app.domain.review_wall -> app.domain.__init__ -> .common
        # -> app.domain.common -> app.worker -> app.media_exec(包 __init__)
        # -> .run_job -> .authority——回到本模块自身，此时它仍在初始化中，
        # 后续名字未定义，ImportError（`python -c "import app.media_exec.
        # authority"`/`import app.media_exec`/`import app.api` 三条入口均
        # 复现，只有 app.worker/app.main 因warm-up顺序凑巧未触发）。
        from app.domain.review_wall import _review_upstream_snapshot
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
    target_shot_id = row["shot_id"] if row else None

    # Subset check, not full-set equality: a sibling shot resolving/growing its
    # own gallery must not invalidate this job (EP1 shots 5/6/7, 2026-09-15).
    # Narrowed further to shared-library entities only (2026-10-01《顾念长安》
    # EP1 二次生产事故：段 20 自己改选哪几件道具、段 19 丢一张本镜用不到的
    # "反打"场景图，都把毫不相关的段 5/18 任务判成过期——见
    # _review_shared_asset_entities 文档）。disappearing/changed entries within
    # that narrowed scope still fail closed.
    shared_entities = _review_shared_asset_entities(expected_assets, target_shot_id)
    assets_equal = bool(
        not expected_assets
        or _review_asset_contract(expected_assets, target_shot_id, shared_entities)
        <= _review_asset_contract(current_assets, target_shot_id, shared_entities)
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


def _review_shared_asset_entities(items, target_shot_id):
    """本镜自己在捕获快照时实际依赖的「共享素材库」实体集合。

    只有 (entity_type, entity_name) 且带着真实 ``asset_version``（人物库
    portrait_id / 场景库 scene_reference_id）的条目才代表"多个镜头引用同一条
    库记录"；道具参考图在本仓库没有稳定库版本号（见
    ``app.video_modes.prop_references.prop_library_anchors``，从不写
    library_revision_id/library_view_id），它的 selectedForSeedance 勾选只是
    那一镜自己这次生成的输出，不是任何其它镜头的上游依赖。

    2026-10-01《顾念长安》EP1 两次生产事故实测复现：段 20 自己换了一次道具
    勾选（与本镜无关）、段 19 的参考图少了一张本镜根本不用的"反打"场景图，
    都曾把毫不相关的段 18/段 5 任务判成 REVIEW_DEPENDENCY_STALE——前者没有
    asset_version、后者的实体不在本镜自己的依赖集合里，两者都不该参与比较。
    """
    return {
        (item.get("entity_type"), item.get("entity_name"))
        for item in items
        if item.get("shot_id") == target_shot_id and item.get("asset_version")
    }


def _review_asset_contract(items, target_shot_id, shared_entities):
    """比较其他镜头里、本镜真正共享依赖的素材身份，忽略引用行编号。"""
    # version_id/ref_id 是每次任务新生成的行 id，同一素材再入队就换一个
    # （EP1 串接实测：链上相邻镜头先后重建参考图行，彼此把对方快照里的旧
    # ref_id 判成消失）；shared_entities 把比较范围收窄到本镜自己依赖、且真有
    # 库版本号的实体（见 _review_shared_asset_entities 文档）。
    return {
        json.dumps(
            {key: value for key, value in item.items() if key not in {"version_id", "ref_id"}},
            ensure_ascii=False, sort_keys=True,
        )
        for item in items
        if item.get("shot_id") != target_shot_id
        and item.get("asset_version")
        and (item.get("entity_type"), item.get("entity_name")) in shared_entities
    }
