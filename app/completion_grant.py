"""完成授权（StoryboardCompletionGrant / VideoCompletionGrant）。

分镜：用户显式选择「生成完成后自动确认」时签发。
视频：用户选择「补齐到全片可用」时签发。
token 只存哈希，不存明文。
"""
from __future__ import annotations

import hashlib
import json
import math
import secrets
from typing import Any, Literal

from pydantic import BaseModel

from app.db import get_conn, new_id, now

GRANT_TTL_S = 6 * 3600  # 6 小时
PERMISSION = "storyboard.generate_and_confirm"
VIDEO_PERMISSION = "video.complete_episode"
DEFAULT_VIDEO_BUDGET_CAP_CNY = 150.0
DEFAULT_VIDEO_WALL_CLOCK_CAP_S = 4 * 3600
DEFAULT_FALLBACK_QUOTA_FRACTION = 0.2


class StoryboardCompletionGrant(BaseModel):
    grant_id: str
    episode_id: str
    project_id: str
    screenplay_artifact_id: str
    bible_artifact_id: str | None = None
    permission: Literal["storyboard.generate_and_confirm"] = PERMISSION
    kind: Literal["storyboard", "video"] = "storyboard"
    issued_by: str = "user"
    issued_at: float
    expires_at: float
    consumed_at: float | None = None
    revoked_at: float | None = None


class VideoCompletionGrant(BaseModel):
    grant_id: str
    episode_id: str
    project_id: str
    storyboard_artifact_id: str
    permission: Literal["video.complete_episode"] = VIDEO_PERMISSION
    kind: Literal["video"] = "video"
    budget_cap_cny: float = DEFAULT_VIDEO_BUDGET_CAP_CNY
    wall_clock_cap_s: float = DEFAULT_VIDEO_WALL_CLOCK_CAP_S
    deadline_at: float
    allow_fallback_adopt: bool = True
    max_fallback_shots: int = 0
    allow_storyboard_edit: bool = False
    issued_by: str = "user"
    issued_at: float
    expires_at: float
    consumed_at: float | None = None
    revoked_at: float | None = None


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def ensure_completion_grants_table(conn=None) -> None:
    db = conn or get_conn()
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
        "ALTER TABLE completion_grants ADD COLUMN kind TEXT NOT NULL DEFAULT 'storyboard'",
        "ALTER TABLE completion_grants ADD COLUMN storyboard_artifact_id TEXT",
        "ALTER TABLE completion_grants ADD COLUMN budget_cap_cny REAL",
        "ALTER TABLE completion_grants ADD COLUMN wall_clock_cap_s REAL",
        "ALTER TABLE completion_grants ADD COLUMN deadline_at REAL",
        "ALTER TABLE completion_grants ADD COLUMN allow_fallback_adopt INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE completion_grants ADD COLUMN max_fallback_shots INTEGER",
        "ALTER TABLE completion_grants ADD COLUMN allow_storyboard_edit INTEGER NOT NULL DEFAULT 0",
    ):
        try:
            db.execute(stmt)
            db.commit()
        except Exception:  # noqa: BLE001
            pass
    db.commit()


def issue_completion_grant(
    *,
    episode_id: str,
    project_id: str,
    screenplay_artifact_id: str,
    bible_artifact_id: str | None = None,
    issued_by: str = "user",
    ttl_s: int = GRANT_TTL_S,
    impact_snapshot: dict[str, Any] | None = None,
) -> tuple[StoryboardCompletionGrant, str]:
    """签发分镜授权；返回 (grant, plaintext_token)。明文 token 只返回一次，库内只存哈希。"""
    ensure_completion_grants_table()
    conn = get_conn()
    grant_id = new_id("grant")
    token = secrets.token_urlsafe(24)
    issued_at = now()
    expires_at = issued_at + max(60, int(ttl_s))
    conn.execute(
        """INSERT INTO completion_grants(
            id, episode_id, project_id, screenplay_artifact_id, bible_artifact_id,
            permission, token_hash, issued_by, issued_at, expires_at, consumed_at, revoked_at,
            impact_snapshot_json, kind
        ) VALUES(?,?,?,?,?,?,?,?,?,?,NULL,NULL,?,'storyboard')""",
        (
            grant_id, episode_id, project_id, screenplay_artifact_id or "",
            bible_artifact_id, PERMISSION, _hash_token(token), issued_by,
            issued_at, expires_at,
            json.dumps(impact_snapshot or {}, ensure_ascii=False),
        ),
    )
    conn.commit()
    grant = StoryboardCompletionGrant(
        grant_id=grant_id,
        episode_id=episode_id,
        project_id=project_id,
        screenplay_artifact_id=screenplay_artifact_id or "",
        bible_artifact_id=bible_artifact_id,
        issued_by=issued_by,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    return grant, token


def default_max_fallback_shots(shots_total: int) -> int:
    return max(1, int(math.ceil(max(0, shots_total) * DEFAULT_FALLBACK_QUOTA_FRACTION)))


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
) -> tuple[VideoCompletionGrant, str]:
    """签发视频补齐授权。"""
    ensure_completion_grants_table()
    conn = get_conn()
    grant_id = new_id("grant")
    token = secrets.token_urlsafe(24)
    issued_at = now()
    cap = float(budget_cap_cny if budget_cap_cny is not None else DEFAULT_VIDEO_BUDGET_CAP_CNY)
    wall = float(wall_clock_cap_s if wall_clock_cap_s is not None else DEFAULT_VIDEO_WALL_CLOCK_CAP_S)
    if not math.isfinite(cap) or not 1 <= cap <= 100000:
        raise GrantValidationError("INVALID_BUDGET", "视频补齐预算必须是 1–100000 的有限数")
    if not math.isfinite(wall) or not 60 <= wall <= 604800:
        raise GrantValidationError("INVALID_WALL_CLOCK", "视频补齐时长墙必须是 60–604800 秒的有限数")
    deadline_at = issued_at + wall
    expires_at = issued_at + max(60, int(ttl_s), int(wall) + 3600)
    fallback_quota = (
        int(max_fallback_shots)
        if max_fallback_shots is not None
        else default_max_fallback_shots(shots_total)
    )
    conn.execute(
        """INSERT INTO completion_grants(
            id, episode_id, project_id, screenplay_artifact_id, bible_artifact_id,
            permission, token_hash, issued_by, issued_at, expires_at, consumed_at, revoked_at,
            impact_snapshot_json, kind, storyboard_artifact_id, budget_cap_cny, wall_clock_cap_s, deadline_at,
            allow_fallback_adopt, max_fallback_shots, allow_storyboard_edit
        ) VALUES(?,?,?,?,NULL,?,?,?,?,?,NULL,NULL,?,?,?,?,?,?,?,?,?)""",
        (
            grant_id, episode_id, project_id, "",
            VIDEO_PERMISSION, _hash_token(token), issued_by, issued_at, expires_at,
            json.dumps(impact_snapshot or {}, ensure_ascii=False),
            "video", storyboard_artifact_id or "", cap, wall, deadline_at,
            1 if allow_fallback_adopt else 0, fallback_quota,
            1 if allow_storyboard_edit else 0,
        ),
    )
    conn.commit()
    grant = VideoCompletionGrant(
        grant_id=grant_id,
        episode_id=episode_id,
        project_id=project_id,
        storyboard_artifact_id=storyboard_artifact_id or "",
        budget_cap_cny=cap,
        wall_clock_cap_s=wall,
        deadline_at=deadline_at,
        allow_fallback_adopt=allow_fallback_adopt,
        max_fallback_shots=fallback_quota,
        allow_storyboard_edit=allow_storyboard_edit,
        issued_by=issued_by,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    return grant, token


def _row_to_grant(row) -> StoryboardCompletionGrant:
    kind = "storyboard"
    try:
        kind = row["kind"] or "storyboard"
    except (KeyError, IndexError, TypeError):
        kind = "storyboard"
    return StoryboardCompletionGrant(
        grant_id=row["id"],
        episode_id=row["episode_id"],
        project_id=row["project_id"],
        screenplay_artifact_id=row["screenplay_artifact_id"] or "",
        bible_artifact_id=row["bible_artifact_id"],
        permission=row["permission"],
        kind=kind if kind in {"storyboard", "video"} else "storyboard",
        issued_by=row["issued_by"],
        issued_at=row["issued_at"],
        expires_at=row["expires_at"],
        consumed_at=row["consumed_at"],
        revoked_at=row["revoked_at"],
    )


def _row_to_video_grant(row) -> VideoCompletionGrant:
    def _col(name, default=None):
        try:
            return row[name]
        except (KeyError, IndexError, TypeError):
            return default

    return VideoCompletionGrant(
        grant_id=row["id"],
        episode_id=row["episode_id"],
        project_id=row["project_id"],
        storyboard_artifact_id=_col("storyboard_artifact_id") or "",
        budget_cap_cny=float(_col("budget_cap_cny") or DEFAULT_VIDEO_BUDGET_CAP_CNY),
        wall_clock_cap_s=float(_col("wall_clock_cap_s") or DEFAULT_VIDEO_WALL_CLOCK_CAP_S),
        deadline_at=float(
            _col("deadline_at")
            or (float(row["issued_at"]) + float(_col("wall_clock_cap_s") or DEFAULT_VIDEO_WALL_CLOCK_CAP_S))
        ),
        allow_fallback_adopt=bool(int(_col("allow_fallback_adopt", 1) or 0)),
        max_fallback_shots=int(_col("max_fallback_shots") or 0),
        allow_storyboard_edit=bool(int(_col("allow_storyboard_edit", 0) or 0)),
        issued_by=row["issued_by"],
        issued_at=row["issued_at"],
        expires_at=row["expires_at"],
        consumed_at=row["consumed_at"],
        revoked_at=row["revoked_at"],
    )


def get_grant(grant_id: str) -> StoryboardCompletionGrant | None:
    ensure_completion_grants_table()
    row = get_conn().execute(
        "SELECT * FROM completion_grants WHERE id=?", (grant_id,)
    ).fetchone()
    return _row_to_grant(row) if row else None


def get_video_grant(grant_id: str) -> VideoCompletionGrant | None:
    ensure_completion_grants_table()
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


class GrantValidationError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def validate_grant_for_confirm(
    grant_id: str,
    *,
    episode_id: str,
    screenplay_artifact_id: str | None,
    bible_artifact_id: str | None,
) -> StoryboardCompletionGrant:
    """确认前校验授权；失败抛 GrantValidationError。"""
    grant = get_grant(grant_id)
    if grant is None:
        raise GrantValidationError("GRANT_NOT_FOUND", "自动确认授权不存在")
    if grant.episode_id != episode_id:
        raise GrantValidationError("GRANT_EPISODE_MISMATCH", "授权不属于本集")
    if grant.revoked_at is not None:
        raise GrantValidationError("GRANT_REVOKED", "自动确认授权已撤销")
    if grant.consumed_at is not None:
        raise GrantValidationError("GRANT_CONSUMED", "自动确认授权已使用")
    if now() > grant.expires_at:
        raise GrantValidationError("GRANT_EXPIRED", "自动确认授权已过期")
    if (screenplay_artifact_id or "") != (grant.screenplay_artifact_id or ""):
        raise GrantValidationError(
            "UPSTREAM_VERSION_CHANGED",
            "剧本 Artifact 已变更，自动确认授权失效",
        )
    if (bible_artifact_id or "") != (grant.bible_artifact_id or ""):
        raise GrantValidationError(
            "UPSTREAM_VERSION_CHANGED",
            "人物谱 Artifact 已变更，自动确认授权失效",
        )
    return grant


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
    return grant


def refresh_video_grant_storyboard_artifact(grant_id: str, storyboard_artifact_id: str) -> None:
    """Supervisor 自身微调分镜后刷新绑定的 artifact id（外部改动仍失效）。"""
    ensure_completion_grants_table()
    conn = get_conn()
    conn.execute(
        "UPDATE completion_grants SET storyboard_artifact_id=? WHERE id=?",
        (storyboard_artifact_id or "", grant_id),
    )
    conn.commit()


def bump_video_grant_budget(
    grant_id: str, *, add_cny: float, add_wall_s: float = 0
) -> VideoCompletionGrant:
    """追加预算/时长并返回更新后的 grant。"""
    ensure_completion_grants_table()
    grant = get_video_grant(grant_id)
    if grant is None:
        raise GrantValidationError("GRANT_NOT_FOUND", "视频补齐授权不存在")
    if grant.revoked_at is not None:
        raise GrantValidationError("GRANT_REVOKED", "视频补齐授权已撤销")
    add_cny = float(add_cny)
    add_wall_s = float(add_wall_s)
    if not math.isfinite(add_cny) or add_cny < 0 or add_cny > 100000:
        raise GrantValidationError("INVALID_BUDGET", "追加预算必须是 0–100000 的有限数")
    if not math.isfinite(add_wall_s) or add_wall_s < 0 or add_wall_s > 604800:
        raise GrantValidationError("INVALID_WALL_CLOCK", "追加时长必须是 0–604800 秒的有限数")
    if add_cny == 0 and add_wall_s == 0:
        raise GrantValidationError("EMPTY_TOPUP", "追加预算和时长不能同时为 0")
    new_cap = float(grant.budget_cap_cny) + add_cny
    new_wall = float(grant.wall_clock_cap_s) + add_wall_s
    if new_cap > 100000 or new_wall > 604800:
        raise GrantValidationError("GRANT_LIMIT_EXCEEDED", "追加后授权超过最大上限")
    new_deadline = float(grant.issued_at) + new_wall
    new_expires = max(float(grant.expires_at), now() + GRANT_TTL_S)
    conn = get_conn()
    conn.execute(
        """UPDATE completion_grants
           SET budget_cap_cny=?, wall_clock_cap_s=?, deadline_at=?, expires_at=?, consumed_at=NULL
           WHERE id=?""",
        (new_cap, new_wall, new_deadline, new_expires, grant_id),
    )
    conn.commit()
    updated = get_video_grant(grant_id)
    assert updated is not None
    return updated


def consume_grant(grant_id: str) -> None:
    ensure_completion_grants_table()
    conn = get_conn()
    conn.execute(
        "UPDATE completion_grants SET consumed_at=? WHERE id=? AND consumed_at IS NULL",
        (now(), grant_id),
    )
    conn.commit()


def revoke_grant(grant_id: str) -> None:
    ensure_completion_grants_table()
    conn = get_conn()
    conn.execute(
        "UPDATE completion_grants SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
        (now(), grant_id),
    )
    conn.commit()


def revoke_active_grants_for_episode(episode_id: str) -> int:
    ensure_completion_grants_table()
    conn = get_conn()
    cur = conn.execute(
        """UPDATE completion_grants SET revoked_at=?
           WHERE episode_id=? AND revoked_at IS NULL AND consumed_at IS NULL""",
        (now(), episode_id),
    )
    conn.commit()
    return int(cur.rowcount or 0)


def active_video_grant_budget_cap(episode_id: str) -> float | None:
    """若本集有未撤销的视频 grant，返回其 budget_cap_cny，供 enqueue 优先读取。"""
    ensure_completion_grants_table()
    row = get_conn().execute(
        """SELECT budget_cap_cny FROM completion_grants
           WHERE episode_id=? AND kind='video' AND revoked_at IS NULL
             AND (consumed_at IS NULL OR consumed_at=0)
             AND expires_at > ?
           ORDER BY issued_at DESC LIMIT 1""",
        (episode_id, now()),
    ).fetchone()
    if not row:
        return None
    try:
        return float(row["budget_cap_cny"]) if row["budget_cap_cny"] is not None else None
    except (TypeError, ValueError, KeyError):
        return None
