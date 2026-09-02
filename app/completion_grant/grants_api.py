"""completion grant 的读取、校验、改绑、加额与撤销。
"""
from __future__ import annotations

import math


from app.db import get_conn, new_id, now
from app.provider_task_clearance import (
    ProviderTasksNotTerminalError as ProviderTasksNotTerminalError,
    assert_provider_tasks_clearable as assert_provider_tasks_clearable,
    prepare_provider_tasks_for_clear as prepare_provider_tasks_for_clear,
)
from app.completion_grant.grants_issue import (
    _idempotent_video_grant,
    _record_video_budget_authority_event,
    _video_budget_authority_operation_id,
    ensure_completion_grants_table,
)
from app.completion_grant.ledger import ensure_video_budget_authority_tables
from app.completion_grant.models import (
    GRANT_TTL_S,
    VIDEO_PERMISSION,
    GrantValidationError,
    VideoCompletionGrant,
    VideoPlanGenerationError,
    _row_to_video_grant,
)
from app.completion_grant.qualification import (
    _canonical_json,
    _content_fingerprint,
    current_video_completion_qualification,
)

def get_video_grant(grant_id: str) -> VideoCompletionGrant | None:
    ensure_completion_grants_table(get_conn())
    row = get_conn().execute(
        "SELECT * FROM completion_grants WHERE id=?", (grant_id,)
    ).fetchone()
    if not row:
        return None
    try:
        kind = row["kind"]
    except (KeyError, IndexError, TypeError):
        kind = None
    if kind != "video" and row["permission"] != VIDEO_PERMISSION:
        return None
    return _row_to_video_grant(row)
def validate_video_grant(
    grant_id: str,
    *,
    episode_id: str,
    storyboard_artifact_id: str | None,
) -> VideoCompletionGrant:
    """视频补齐前校验授权。"""
    grant = get_video_grant(grant_id)
    if grant is None:
        raise GrantValidationError("GRANT_NOT_FOUND", "视频补齐授权不存在")
    if grant.episode_id != episode_id:
        raise GrantValidationError("GRANT_EPISODE_MISMATCH", "授权不属于本集")
    if grant.revoked_at is not None:
        raise GrantValidationError("GRANT_REVOKED", "视频补齐授权已撤销")
    if grant.consumed_at is not None:
        raise GrantValidationError("GRANT_CONSUMED", "视频补齐授权已使用")
    if now() > grant.expires_at:
        raise GrantValidationError("GRANT_EXPIRED", "视频补齐授权已过期")
    if (storyboard_artifact_id or "") != (grant.storyboard_artifact_id or ""):
        raise GrantValidationError(
            "UPSTREAM_VERSION_CHANGED",
            "分镜 Artifact 已变更，视频补齐授权失效",
        )
    stored = grant.release_qualification
    stored_hash = grant.release_qualification_hash
    if not stored or not stored_hash or _content_fingerprint(stored) != stored_hash:
        raise GrantValidationError(
            "GRANT_RELEASE_QUALIFICATION_MISSING",
            "视频补齐授权缺少可重算的发布资格绑定",
        )
    plan_binding = dict(stored.get("generation_plan") or {})
    plan_applicable = bool(plan_binding.get("applicable"))
    try:
        current, current_hash = current_video_completion_qualification(
            episode_id, conn=get_conn(),
            generation_plan_applicable=plan_applicable,
        )
    except ValueError as exc:
        raise GrantValidationError(
            "RELEASE_QUALIFICATION_INVALID",
            f"当前发布资格无法验证：{exc}",
        ) from exc
    if current_hash != stored_hash or current != stored:
        raise GrantValidationError(
            "RELEASE_QUALIFICATION_CHANGED",
            "剧本、分镜、审读、凭证、Shot 投影或视频计划已变更，请重新授权",
        )
    if plan_applicable and (
        grant.episode_video_plan_id != plan_binding.get("episode_video_plan_id")
        or grant.episode_video_plan_revision != plan_binding.get("plan_revision")
        or grant.video_plan_release_hash != plan_binding.get("release_qualification_hash")
        or grant.capability_snapshot_id != plan_binding.get("capability_snapshot_id")
    ):
        raise GrantValidationError(
            "GRANT_PLAN_BINDING_CORRUPT",
            "授权的视频计划绑定与内容指纹不一致",
        )
    return grant


def bind_video_grant_generation_plan(
    grant_id: str,
    *,
    episode_id: str,
    storyboard_artifact_id: str | None,
) -> VideoCompletionGrant:
    """Atomically bind the first valid plan before any paid media preparation.

    A grant may be issued before the asynchronous planner has run.  Its release
    facts are already immutable at that point; this operation may only replace
    the explicit ``plan_pending_at_grant_issue`` slot while every other release
    fact is byte-for-byte unchanged.
    """
    grant = validate_video_grant(
        grant_id,
        episode_id=episode_id,
        storyboard_artifact_id=storyboard_artifact_id,
    )
    old = grant.release_qualification
    old_plan = dict(old.get("generation_plan") or {})
    if old_plan.get("applicable"):
        return grant
    try:
        current, current_hash = current_video_completion_qualification(
            episode_id, conn=get_conn(),
            generation_plan_applicable=True,
        )
    except ValueError as exc:
        # 计划生成/校验失败，不是授权失败——用独立的异常类型，见
        # VideoPlanGenerationError 的 docstring（ERR-20260831-dd05c7）。
        raise VideoPlanGenerationError(str(exc)) from exc
    old_release = {key: value for key, value in old.items() if key != "generation_plan"}
    current_release = {
        key: value for key, value in current.items() if key != "generation_plan"
    }
    if old_release != current_release:
        raise GrantValidationError(
            "RELEASE_QUALIFICATION_CHANGED",
            "视频计划产生前发布资格已变更，不得继续绑定",
        )
    plan = current["generation_plan"]
    conn = get_conn()
    updated = conn.execute(
        """UPDATE completion_grants
              SET release_qualification_json=?,release_qualification_hash=?,
                  episode_video_plan_id=?,episode_video_plan_revision=?,
                  video_plan_release_hash=?,capability_snapshot_id=?
            WHERE id=? AND release_qualification_hash=?""",
        (
            _canonical_json(current),
            current_hash,
            plan["episode_video_plan_id"],
            plan["plan_revision"],
            plan["release_qualification_hash"],
            plan["capability_snapshot_id"],
            grant_id,
            grant.release_qualification_hash,
        ),
    )
    if updated.rowcount != 1:
        conn.rollback()
        raise GrantValidationError(
            "GRANT_CONCURRENTLY_CHANGED",
            "视频补齐授权在绑定计划时已被并发修改",
        )
    conn.commit()
    return validate_video_grant(
        grant_id,
        episode_id=episode_id,
        storyboard_artifact_id=storyboard_artifact_id,
    )


def bump_video_grant_wall_clock(
    grant_id: str,
    *,
    add_wall_s: float,
    idempotency_key: str | None = None,
) -> VideoCompletionGrant:
    """Atomically extend a grant's wall-clock cap and its audit ledger.

    金额不再构成生成拦截（会员分档时长制，非按金额计费）：追加授权只延长
    ``wall_clock_cap_s``，不再有 ``add_cny``——见 CLAUDE.md「Retiring
    Features」与本次「成本预算拦截体系退场」。调用点原名
    ``bump_video_grant_budget`` 已改名，语义从"追加预算"收窄为"追加时长"。
    """
    ensure_completion_grants_table(get_conn())
    add_wall_s = float(add_wall_s)
    if not math.isfinite(add_wall_s) or add_wall_s <= 0 or add_wall_s > 604800:
        raise GrantValidationError("INVALID_WALL_CLOCK", "追加时长必须是大于 0 且不超过 604800 秒的有限数")
    request_fingerprint = _content_fingerprint({
        "grant_id": grant_id,
        "add_wall_s": add_wall_s,
    })
    operation_id = _video_budget_authority_operation_id(
        event_type="grant_topped_up",
        scope_id=grant_id,
        idempotency_key=idempotency_key,
        fallback_id=f"completion-grant-topup:{new_id('topup')}",
    )
    conn = get_conn()
    ensure_video_budget_authority_tables(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        replay = _idempotent_video_grant(
            conn,
            operation_id=operation_id,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            conn.commit()
            return replay
        row = conn.execute(
            "SELECT * FROM completion_grants WHERE id=?",
            (grant_id,),
        ).fetchone()
        if row is None or (
            row["kind"] != "video" and row["permission"] != VIDEO_PERMISSION
        ):
            raise GrantValidationError(
                "GRANT_NOT_FOUND",
                "视频补齐授权不存在",
            )
        grant = _row_to_video_grant(row)
        if grant.revoked_at is not None:
            raise GrantValidationError(
                "GRANT_REVOKED",
                "视频补齐授权已撤销",
            )
        new_wall = float(grant.wall_clock_cap_s) + add_wall_s
        if new_wall > 604800:
            raise GrantValidationError(
                "GRANT_LIMIT_EXCEEDED",
                "追加后时长墙超过最大上限",
            )
        stamp = now()
        new_deadline = float(grant.issued_at) + new_wall
        new_expires = max(float(grant.expires_at), stamp + GRANT_TTL_S)
        updated = conn.execute(
            """UPDATE completion_grants
                  SET wall_clock_cap_s=?,deadline_at=?,
                      expires_at=?,consumed_at=NULL
                WHERE id=? AND wall_clock_cap_s=?
                  AND deadline_at=? AND expires_at=? AND revoked_at IS NULL""",
            (
                new_wall,
                new_deadline,
                new_expires,
                grant_id,
                grant.wall_clock_cap_s,
                grant.deadline_at,
                grant.expires_at,
            ),
        )
        if updated.rowcount != 1:
            raise GrantValidationError(
                "GRANT_CONCURRENTLY_CHANGED",
                "视频补齐授权在追加时长时已被并发修改",
            )
        # video_budget_authority_ledger 同时承担幂等去重职责，金额字段固定写 0。
        _record_video_budget_authority_event(
            conn,
            operation_id=operation_id,
            request_fingerprint=request_fingerprint,
            event_type="grant_topped_up",
            grant_id=grant_id,
            episode_id=grant.episode_id,
            project_id=grant.project_id,
            requested_add_cny=0.0,
            prior_grant_cap_cny=None,
            grant_cap_cny=0.0,
            prior_authority_cap_cny=None,
            authority_cap_cny=0.0,
            prior_wall_clock_cap_s=grant.wall_clock_cap_s,
            wall_clock_cap_s=new_wall,
            created_at=stamp,
        )
        stored = conn.execute(
            "SELECT * FROM completion_grants WHERE id=?",
            (grant_id,),
        ).fetchone()
        if stored is None:
            raise RuntimeError("追加后 completion grant 丢失")
        result = _row_to_video_grant(stored)
        conn.commit()
        return result
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def consume_grant(grant_id: str) -> None:
    conn = get_conn()
    ensure_completion_grants_table(conn)
    conn.execute(
        "UPDATE completion_grants SET consumed_at=? WHERE id=? AND consumed_at IS NULL",
        (now(), grant_id),
    )
    conn.commit()


def revoke_grant(grant_id: str) -> None:
    conn = get_conn()
    ensure_completion_grants_table(conn)
    conn.execute(
        "UPDATE completion_grants SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
        (now(), grant_id),
    )
    conn.commit()


def revoke_active_video_grants_for_episode(episode_id: str) -> int:
    conn = get_conn()
    ensure_completion_grants_table(conn)
    cur = conn.execute(
        """UPDATE completion_grants SET revoked_at=?
           WHERE episode_id=? AND kind='video' AND revoked_at IS NULL AND consumed_at IS NULL""",
        (now(), episode_id),
    )
    conn.commit()
    return int(cur.rowcount or 0)
