"""completion grant 的建表与签发。
"""
from __future__ import annotations

import hashlib
import json
import math
import secrets
from typing import Any


from app.db import get_conn, new_id, now
from app.provider_task_clearance import (
    ProviderTasksNotTerminalError as ProviderTasksNotTerminalError,
    assert_provider_tasks_clearable as assert_provider_tasks_clearable,
    prepare_provider_tasks_for_clear as prepare_provider_tasks_for_clear,
)
from app.completion_grant.budget_authority import (
    _episode_video_budget_floor,
    authorize_episode_video_budget_absolute,
    episode_video_completion_budget_requirement,
)
from app.completion_grant.ledger import ensure_video_budget_authority_tables
from app.completion_grant.models import (
    DEFAULT_FALLBACK_QUOTA_FRACTION,
    DEFAULT_VIDEO_WALL_CLOCK_CAP_S,
    GRANT_TTL_S,
    VIDEO_PERMISSION,
    GrantValidationError,
    VideoCompletionGrant,
    _row_to_video_grant,
)
from app.completion_grant.qualification import (
    _canonical_json,
    _content_fingerprint,
    current_video_completion_qualification,
)

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def ensure_completion_grants_table(conn) -> None:
    db = conn
    db.execute(
        """CREATE TABLE IF NOT EXISTS completion_grants (
            id TEXT PRIMARY KEY,
            episode_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            screenplay_artifact_id TEXT NOT NULL,
            bible_artifact_id TEXT,
            permission TEXT NOT NULL,
            token_hash TEXT NOT NULL,
            issued_by TEXT NOT NULL,
            issued_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            consumed_at REAL,
            revoked_at REAL,
            impact_snapshot_json TEXT
        )"""
    )
    for stmt in (
        "ALTER TABLE completion_grants ADD COLUMN kind TEXT NOT NULL DEFAULT 'video'",
        "ALTER TABLE completion_grants ADD COLUMN storyboard_artifact_id TEXT",
        "ALTER TABLE completion_grants ADD COLUMN budget_cap_cny REAL",
        "ALTER TABLE completion_grants ADD COLUMN wall_clock_cap_s REAL",
        "ALTER TABLE completion_grants ADD COLUMN deadline_at REAL",
        "ALTER TABLE completion_grants ADD COLUMN allow_fallback_adopt INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE completion_grants ADD COLUMN max_fallback_shots INTEGER",
        "ALTER TABLE completion_grants ADD COLUMN allow_storyboard_edit INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE completion_grants ADD COLUMN release_qualification_json TEXT NOT NULL DEFAULT '{}'",
        "ALTER TABLE completion_grants ADD COLUMN release_qualification_hash TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE completion_grants ADD COLUMN episode_video_plan_id TEXT",
        "ALTER TABLE completion_grants ADD COLUMN episode_video_plan_revision INTEGER",
        "ALTER TABLE completion_grants ADD COLUMN video_plan_release_hash TEXT",
        "ALTER TABLE completion_grants ADD COLUMN capability_snapshot_id TEXT",
    ):
        try:
            db.execute(stmt)
            db.commit()
        except Exception:  # noqa: BLE001
            pass
    db.execute(
        "DELETE FROM completion_grants WHERE kind='storyboard' OR permission='storyboard.generate_and_confirm'"
    )
    db.commit()


def default_max_fallback_shots(shots_total: int) -> int:
    return max(1, int(math.ceil(max(0, shots_total) * DEFAULT_FALLBACK_QUOTA_FRACTION)))
def _video_budget_authority_operation_id(
    *,
    event_type: str,
    scope_id: str,
    idempotency_key: str | None,
    fallback_id: str,
) -> str:
    normalized = str(idempotency_key or "").strip()
    if not normalized:
        return fallback_id
    material = _canonical_json({
        "event_type": event_type,
        "scope_id": scope_id,
        "idempotency_key": normalized,
    })
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"video-budget-authority:{digest}"


def _idempotent_video_grant(
    conn,
    *,
    operation_id: str,
    request_fingerprint: str,
) -> VideoCompletionGrant | None:
    event = conn.execute(
        """SELECT grant_id,request_fingerprint
             FROM video_budget_authority_ledger
            WHERE operation_id=?""",
        (operation_id,),
    ).fetchone()
    if event is None:
        return None
    if str(event["request_fingerprint"]) != request_fingerprint:
        raise GrantValidationError(
            "GRANT_IDEMPOTENCY_CONFLICT",
            "同一幂等键已用于不同的视频预算授权请求",
        )
    row = conn.execute(
        "SELECT * FROM completion_grants WHERE id=?",
        (event["grant_id"],),
    ).fetchone()
    if row is None:
        raise RuntimeError(
            "视频预算授权审计事件引用的 completion grant 不存在"
        )
    return _row_to_video_grant(row)


def _record_video_budget_authority_event(
    conn,
    *,
    operation_id: str,
    request_fingerprint: str,
    event_type: str,
    grant_id: str,
    episode_id: str,
    project_id: str,
    requested_add_cny: float,
    prior_grant_cap_cny: float | None,
    grant_cap_cny: float,
    prior_authority_cap_cny: float | None,
    authority_cap_cny: float,
    prior_wall_clock_cap_s: float | None,
    wall_clock_cap_s: float,
    created_at: float,
) -> None:
    conn.execute(
        """INSERT INTO video_budget_authority_ledger(
               id,operation_id,request_fingerprint,event_type,grant_id,
               episode_id,project_id,requested_add_cny,
               prior_grant_cap_cny,grant_cap_cny,
               prior_authority_cap_cny,authority_cap_cny,
               prior_wall_clock_cap_s,wall_clock_cap_s,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            new_id("video_budget_authority"),
            operation_id,
            request_fingerprint,
            event_type,
            grant_id,
            episode_id,
            project_id,
            requested_add_cny,
            prior_grant_cap_cny,
            grant_cap_cny,
            prior_authority_cap_cny,
            authority_cap_cny,
            prior_wall_clock_cap_s,
            wall_clock_cap_s,
            created_at,
        ),
    )


def issue_video_completion_grant(
    *,
    episode_id: str,
    project_id: str,
    storyboard_artifact_id: str,
    budget_cap_cny: float | None = None,
    wall_clock_cap_s: float | None = None,
    allow_fallback_adopt: bool = True,
    max_fallback_shots: int | None = None,
    allow_storyboard_edit: bool = False,
    shots_total: int = 0,
    issued_by: str = "user",
    ttl_s: int = GRANT_TTL_S,
    impact_snapshot: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> tuple[VideoCompletionGrant, str]:
    """Atomically issue one completion grant and its payable budget authority."""
    conn = get_conn()
    ensure_completion_grants_table(conn)
    ensure_video_budget_authority_tables(conn)
    requested_budget = (
        float(budget_cap_cny) if budget_cap_cny is not None else None
    )
    requested_wall = (
        float(wall_clock_cap_s)
        if wall_clock_cap_s is not None
        else DEFAULT_VIDEO_WALL_CLOCK_CAP_S
    )
    requested_quota = (
        int(max_fallback_shots)
        if max_fallback_shots is not None
        else default_max_fallback_shots(shots_total)
    )
    request_fingerprint = _content_fingerprint({
        "episode_id": episode_id,
        "project_id": project_id,
        "storyboard_artifact_id": storyboard_artifact_id or "",
        "budget_cap_cny": requested_budget,
        "wall_clock_cap_s": requested_wall,
        "allow_fallback_adopt": bool(allow_fallback_adopt),
        "max_fallback_shots": requested_quota,
        "allow_storyboard_edit": bool(allow_storyboard_edit),
        "shots_total": int(shots_total),
        "issued_by": issued_by,
        "ttl_s": int(ttl_s),
        "impact_snapshot": impact_snapshot or {},
    })
    grant_id = new_id("grant")
    token = secrets.token_urlsafe(24)
    operation_id = _video_budget_authority_operation_id(
        event_type="grant_issued",
        scope_id=episode_id,
        idempotency_key=idempotency_key,
        fallback_id=f"completion-grant-issue:{grant_id}",
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        replay = _idempotent_video_grant(
            conn,
            operation_id=operation_id,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            conn.commit()
            return replay, ""
        episode = conn.execute(
            "SELECT project_id FROM episodes WHERE id=?", (episode_id,)
        ).fetchone()
        if episode is None:
            raise GrantValidationError(
                "GRANT_SCOPE_MISSING",
                "视频补齐授权的分集不存在",
            )
        if str(episode["project_id"]) != str(project_id):
            raise GrantValidationError(
                "GRANT_PROJECT_MISMATCH",
                "视频补齐授权的项目与分集不匹配",
            )
        qualification, qualification_hash = current_video_completion_qualification(
            episode_id,
            conn=conn,
        )
        bound_storyboard_id = str(
            qualification["storyboard_authority"][
                "published_storyboard_artifact_id"
            ]
        )
        if (storyboard_artifact_id or "") != bound_storyboard_id:
            raise GrantValidationError(
                "UPSTREAM_VERSION_CHANGED",
                "请求授权的分镜 Artifact 不是当前发布版",
            )
        generation_plan = qualification["generation_plan"]
        issued_at = now()
        budget_requirement = episode_video_completion_budget_requirement(
            episode_id,
            conn=conn,
        )
        required_cap = float(
            budget_requirement["required_completion_cap_cny"] or 0
        )
        cap = float(
            requested_budget
            if requested_budget is not None
            else max(1.0, required_cap)
        )
        if cap + 1e-9 < required_cap:
            raise GrantValidationError(
                "VIDEO_BUDGET_BELOW_AUTHORITY_PLAN",
                "视频授权低于当前权威镜头计划的一次完整生成成本："
                f"requested={cap:g}, required={required_cap:g}",
            )
        wall = requested_wall
        if not math.isfinite(cap) or not 1 <= cap <= 100000:
            raise GrantValidationError(
                "INVALID_BUDGET",
                "视频补齐预算必须是 1–100000 的有限数",
            )
        if not math.isfinite(wall) or not 60 <= wall <= 604800:
            raise GrantValidationError(
                "INVALID_WALL_CLOCK",
                "视频补齐时长墙必须是 60–604800 秒的有限数",
            )
        deadline_at = issued_at + wall
        expires_at = issued_at + max(60, int(ttl_s), int(wall) + 3600)
        prior_authority = conn.execute(
            """SELECT cap_cny FROM episode_video_budget_authorities
               WHERE episode_id=?""",
            (episode_id,),
        ).fetchone()
        prior_authority_cap = (
            float(prior_authority["cap_cny"])
            if prior_authority is not None
            else None
        )
        conn.execute(
            """INSERT INTO completion_grants(
                id, episode_id, project_id, screenplay_artifact_id, bible_artifact_id,
                permission, token_hash, issued_by, issued_at, expires_at, consumed_at, revoked_at,
                impact_snapshot_json, kind, storyboard_artifact_id, budget_cap_cny, wall_clock_cap_s, deadline_at,
                allow_fallback_adopt, max_fallback_shots, allow_storyboard_edit,
                release_qualification_json, release_qualification_hash,
                episode_video_plan_id, episode_video_plan_revision,
                video_plan_release_hash, capability_snapshot_id
            ) VALUES(?,?,?,?,NULL,?,?,?,?,?,NULL,NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                grant_id, episode_id, project_id, "",
                VIDEO_PERMISSION, _hash_token(token), issued_by, issued_at,
                expires_at, json.dumps(impact_snapshot or {}, ensure_ascii=False),
                "video", storyboard_artifact_id or "", cap, wall, deadline_at,
                1 if allow_fallback_adopt else 0, requested_quota,
                1 if allow_storyboard_edit else 0,
                _canonical_json(qualification), qualification_hash,
                generation_plan.get("episode_video_plan_id"),
                generation_plan.get("plan_revision"),
                generation_plan.get("release_qualification_hash"),
                generation_plan.get("capability_snapshot_id"),
            ),
        )
        # cap 是**本轮**批准的额度，而分集授权存的是累计上限，两者不是同一个量。
        # 首轮已承诺责任为 0 时恰好相等，长期掩盖了这个区别；重跑时旧责任已经
        # 占满上限，直接把 cap 当绝对总额写进去就等于零余量——扣款侧按
        # used = baseline + claimed 判断，第一次供应商调用就超限。实测
        # ep_0a70ec56e8e9：96 元历史 settled 认领 + 本轮批准 96，写进去的上限
        # 仍是 96，八个镜头全部 paused_budget、整集停在 WAITING_AUTHORIZATION。
        # 加上已承诺责任后，可用 = 上限 - 已用 = cap，正好是用户这次批的数。
        _baseline, committed = _episode_video_budget_floor(episode_id, conn=conn)
        authority_cap = authorize_episode_video_budget_absolute(
            episode_id,
            committed + cap,
            source=f"completion_grant:{grant_id}",
            conn=conn,
        )
        _record_video_budget_authority_event(
            conn,
            operation_id=operation_id,
            request_fingerprint=request_fingerprint,
            event_type="grant_issued",
            grant_id=grant_id,
            episode_id=episode_id,
            project_id=project_id,
            requested_add_cny=cap,
            prior_grant_cap_cny=None,
            grant_cap_cny=cap,
            prior_authority_cap_cny=prior_authority_cap,
            authority_cap_cny=authority_cap,
            prior_wall_clock_cap_s=None,
            wall_clock_cap_s=wall,
            created_at=issued_at,
        )
        grant = VideoCompletionGrant(
            grant_id=grant_id,
            episode_id=episode_id,
            project_id=project_id,
            storyboard_artifact_id=storyboard_artifact_id or "",
            release_qualification_hash=qualification_hash,
            release_qualification=qualification,
            episode_video_plan_id=generation_plan.get("episode_video_plan_id"),
            episode_video_plan_revision=generation_plan.get("plan_revision"),
            video_plan_release_hash=generation_plan.get(
                "release_qualification_hash"
            ),
            capability_snapshot_id=generation_plan.get(
                "capability_snapshot_id"
            ),
            budget_cap_cny=cap,
            wall_clock_cap_s=wall,
            deadline_at=deadline_at,
            allow_fallback_adopt=allow_fallback_adopt,
            max_fallback_shots=requested_quota,
            allow_storyboard_edit=allow_storyboard_edit,
            issued_by=issued_by,
            issued_at=issued_at,
            expires_at=expires_at,
        )
        conn.commit()
        return grant, token
    except ValueError as exc:
        if conn.in_transaction:
            conn.rollback()
        if isinstance(exc, GrantValidationError):
            raise
        raise GrantValidationError(
            "RELEASE_QUALIFICATION_INVALID",
            str(exc),
        ) from exc
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
