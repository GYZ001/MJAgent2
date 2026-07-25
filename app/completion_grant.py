"""分镜完成授权（StoryboardCompletionGrant）。

用户显式选择「生成完成后自动确认」时签发；仅授权在预期输入版本范围内生成/修订本集分镜并确认一次。
不授权修改剧本/人物谱、不授权付费视频、不授权确认其他剧集。
"""
from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.db import get_conn, new_id, now

GRANT_TTL_S = 6 * 3600  # 6 小时
PERMISSION = "storyboard.generate_and_confirm"


class StoryboardCompletionGrant(BaseModel):
    grant_id: str
    episode_id: str
    project_id: str
    screenplay_artifact_id: str
    bible_artifact_id: str | None = None
    permission: Literal["storyboard.generate_and_confirm"] = PERMISSION
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
    """签发授权；返回 (grant, plaintext_token)。明文 token 只返回一次，库内只存哈希。"""
    import json

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
            impact_snapshot_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,NULL,NULL,?)""",
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


def _row_to_grant(row) -> StoryboardCompletionGrant:
    return StoryboardCompletionGrant(
        grant_id=row["id"],
        episode_id=row["episode_id"],
        project_id=row["project_id"],
        screenplay_artifact_id=row["screenplay_artifact_id"] or "",
        bible_artifact_id=row["bible_artifact_id"],
        permission=row["permission"],
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
