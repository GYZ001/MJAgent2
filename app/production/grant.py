"""Production Grant：剧集范围一次性授权，修复内部调用不逐次弹窗。"""
from __future__ import annotations

import json
import secrets
from typing import Literal

from pydantic import BaseModel, Field

from app.db import get_conn, new_id, now

GRANT_TTL_S = 6 * 3600

ALLOWED_COMMANDS = frozenset({
    "resource.read",
    "evaluation.run",
    "artifact.diff",
    "screenplay.patch",
    "bible.ensure_source_characters",
    "storyboard.outline.patch",
    "storyboard.patch_shot",
    "storyboard.patch_window",
    "storyboard.insert_shot",
    "storyboard.split_shot",
    "storyboard.delete_shot",
    "storyboard.move_shot",
    "completion.evaluate",
    "completion.publish",
    "run.control",
})

class ProductionGrant(BaseModel):
    grant_id: str
    episode_id: str
    project_id: str
    production_revision_id: str
    kind: Literal["screenplay", "storyboard"]
    input_artifact_hash: str = ""
    allowed_commands: list[str] = Field(default_factory=lambda: sorted(ALLOWED_COMMANDS))
    max_touched_nodes: int = 8
    issued_by: str = "user"
    issued_at: float = 0.0
    expires_at: float = 0.0
    revoked_at: float | None = None
    consumed_at: float | None = None


def ensure_production_grants_table(conn=None) -> None:
    db = conn or get_conn()
    db.execute(
        """CREATE TABLE IF NOT EXISTS production_grants (
            id TEXT PRIMARY KEY,
            episode_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            production_revision_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            input_artifact_hash TEXT NOT NULL DEFAULT '',
            allowed_commands_json TEXT NOT NULL,
            max_touched_nodes INTEGER NOT NULL DEFAULT 8,
            token_hash TEXT NOT NULL,
            issued_by TEXT NOT NULL,
            issued_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            revoked_at REAL,
            consumed_at REAL
        )"""
    )
    db.commit()


def _hash_token(token: str) -> str:
    import hashlib
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_production_grant(
    *,
    episode_id: str,
    project_id: str,
    production_revision_id: str,
    kind: Literal["screenplay", "storyboard"],
    input_artifact_hash: str = "",
    issued_by: str = "user",
    ttl_s: int = GRANT_TTL_S,
    max_touched_nodes: int = 8,
    conn=None,
    commit: bool = True,
) -> tuple[ProductionGrant, str]:
    """签发一次性剧集范围授权。

    ``allowed_commands``/``max_touched_nodes`` 目前只做审计记录——唯一的校验
    消费方 ``assert_grant_allows`` 已随死代码清理一并删除（全仓零引用，实际
    校验在 ``app/domain/screenplay_ops/activation.py`` 用内联 SQL 只查
    ``issued_by``/``input_artifact_hash``/``revoked_at``/``expires_at``/
    ``consumed_at``，从不读这两个字段）。这里不新增校验逻辑去补上这个缺口——
    是否需要补，是产品/安全侧的决策，不在本轮死代码清理范围内。
    """
    db = conn or get_conn()
    if conn is None:
        ensure_production_grants_table()
    grant_id = new_id("pgrant")
    token = secrets.token_urlsafe(24)
    issued_at = now()
    expires_at = issued_at + max(60, int(ttl_s))
    grant = ProductionGrant(
        grant_id=grant_id,
        episode_id=episode_id,
        project_id=project_id,
        production_revision_id=production_revision_id,
        kind=kind,
        input_artifact_hash=input_artifact_hash,
        allowed_commands=sorted(ALLOWED_COMMANDS),
        max_touched_nodes=max_touched_nodes,
        issued_by=issued_by,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    try:
        db.execute(
            """INSERT INTO production_grants(
                id, episode_id, project_id, production_revision_id, kind,
                input_artifact_hash, allowed_commands_json, max_touched_nodes,
                token_hash, issued_by, issued_at, expires_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                grant_id, episode_id, project_id, production_revision_id, kind,
                input_artifact_hash,
                json.dumps(grant.allowed_commands, ensure_ascii=False),
                max_touched_nodes, _hash_token(token), issued_by, issued_at,
                expires_at,
            ),
        )
        # Bind in the caller's transaction.  A detached grant is not authority:
        # missing/stale revisions must fail closed so the outer activation can
        # rollback the grant insert, receipt binding and episode owner together.
        cursor = db.execute(
            "UPDATE production_revisions SET grant_id=?, updated_at=? "
            "WHERE id=? AND episode_id=? AND kind=? AND status='active'",
            (
                grant_id,
                issued_at,
                production_revision_id,
                episode_id,
                kind,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(
                "Production Grant revision authority changed during issuance"
            )
        if commit:
            db.commit()
    except BaseException:
        if conn is None and db.in_transaction:
            db.rollback()
        raise
    return grant, token


def get_production_grant(grant_id: str) -> ProductionGrant | None:
    ensure_production_grants_table()
    row = get_conn().execute(
        "SELECT * FROM production_grants WHERE id=?", (grant_id,)
    ).fetchone()
    if not row:
        return None
    try:
        allowed = json.loads(row["allowed_commands_json"] or "[]")
    except json.JSONDecodeError:
        allowed = sorted(ALLOWED_COMMANDS)
    return ProductionGrant(
        grant_id=row["id"],
        episode_id=row["episode_id"],
        project_id=row["project_id"],
        production_revision_id=row["production_revision_id"],
        kind=row["kind"],
        input_artifact_hash=row["input_artifact_hash"] or "",
        allowed_commands=allowed,
        max_touched_nodes=int(row["max_touched_nodes"] or 8),
        issued_by=row["issued_by"],
        issued_at=float(row["issued_at"] or 0),
        expires_at=float(row["expires_at"] or 0),
        revoked_at=row["revoked_at"],
        consumed_at=row["consumed_at"],
    )


# app.db.init_db() no longer imports this module directly (P0-3 dependency
# inversion, docs/coupling_review_2026-08-29.md 第2步) — it looks this up by
# name through app.db_schema instead.
from app.db_schema import register_table as _register_table  # noqa: E402

_register_table("production_grants_table", ensure_production_grants_table)
