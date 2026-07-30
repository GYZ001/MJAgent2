"""Production Revision：以 revision 为粒度冻结一次 Baseline 生成配额。"""
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.db import get_conn, new_id, now

Kind = Literal["screenplay", "storyboard"]


class ProductionRevision(BaseModel):
    id: str
    episode_id: str
    kind: Kind
    status: str = "active"
    baseline_generation_count: int = 0
    first_evaluation_id: str | None = None
    baseline_artifact_id: str | None = None
    working_artifact_id: str | None = None
    published_artifact_id: str | None = None
    grant_id: str | None = None
    input_fingerprint: str = ""
    contract_version: str = ""
    qa_profile_version: str = ""
    checkpoint_json: dict[str, Any] = Field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def first_evaluation_done(self) -> bool:
        return bool(self.first_evaluation_id)

    @property
    def baseline_done(self) -> bool:
        return self.baseline_generation_count >= 1


def ensure_production_revisions_table(conn=None) -> None:
    db = conn or get_conn()
    db.execute(
        """CREATE TABLE IF NOT EXISTS production_revisions (
            id TEXT PRIMARY KEY,
            episode_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            baseline_generation_count INTEGER NOT NULL DEFAULT 0,
            first_evaluation_id TEXT,
            baseline_artifact_id TEXT,
            working_artifact_id TEXT,
            published_artifact_id TEXT,
            grant_id TEXT,
            input_fingerprint TEXT NOT NULL DEFAULT '',
            contract_version TEXT NOT NULL DEFAULT '',
            qa_profile_version TEXT NOT NULL DEFAULT '',
            checkpoint_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(episode_id, kind, id)
        )"""
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_production_revisions_episode_kind "
        "ON production_revisions(episode_id, kind, updated_at DESC)"
    )
    db.commit()


def _row_to_revision(row) -> ProductionRevision | None:
    if row is None:
        return None
    data = dict(row)
    raw = data.get("checkpoint_json") or "{}"
    try:
        checkpoint = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except json.JSONDecodeError:
        checkpoint = {}
    return ProductionRevision(
        id=data["id"],
        episode_id=data["episode_id"],
        kind=data["kind"],
        status=data.get("status") or "active",
        baseline_generation_count=int(data.get("baseline_generation_count") or 0),
        first_evaluation_id=data.get("first_evaluation_id"),
        baseline_artifact_id=data.get("baseline_artifact_id"),
        working_artifact_id=data.get("working_artifact_id"),
        published_artifact_id=data.get("published_artifact_id"),
        grant_id=data.get("grant_id"),
        input_fingerprint=data.get("input_fingerprint") or "",
        contract_version=data.get("contract_version") or "",
        qa_profile_version=data.get("qa_profile_version") or "",
        checkpoint_json=checkpoint if isinstance(checkpoint, dict) else {},
        created_at=float(data.get("created_at") or 0),
        updated_at=float(data.get("updated_at") or 0),
    )


def get_production_revision(revision_id: str) -> ProductionRevision | None:
    ensure_production_revisions_table()
    row = get_conn().execute(
        "SELECT * FROM production_revisions WHERE id=?", (revision_id,)
    ).fetchone()
    return _row_to_revision(row)


def get_active_production_revision(episode_id: str, kind: Kind) -> ProductionRevision | None:
    ensure_production_revisions_table()
    row = get_conn().execute(
        "SELECT * FROM production_revisions WHERE episode_id=? AND kind=? AND status='active' "
        "ORDER BY updated_at DESC LIMIT 1",
        (episode_id, kind),
    ).fetchone()
    return _row_to_revision(row)


def screenplay_production_state(episode_id: str) -> dict[str, Any]:
    """Return the UI-safe distinction between Baseline generation and Patch repair."""
    from app import task_registry

    rev = get_active_production_revision(episode_id, "screenplay")
    active = task_registry.active("screenplay", episode_id)
    if rev is None:
        return {
            "operation": "baseline",
            "phase": "BASELINE",
            "baseline_done": False,
            "first_evaluation_done": False,
            "task_active": active,
            "can_resume_repair": False,
            "activation_count": 0,
            "patch_count": 0,
            "open_issue_count": 0,
            "yield_reason": "",
        }
    checkpoint = dict(rev.checkpoint_json or {})
    has_working_baseline = bool(rev.baseline_done and rev.working_artifact_id)
    return {
        "revision_id": rev.id,
        "operation": "repair" if has_working_baseline else "baseline",
        "phase": str(
            checkpoint.get("phase") or ("QA" if has_working_baseline else "BASELINE")
        ),
        "baseline_done": rev.baseline_done,
        "first_evaluation_done": rev.first_evaluation_done,
        "task_active": active,
        "can_resume_repair": bool(has_working_baseline and not active),
        "activation_count": int(checkpoint.get("activation_no") or 0),
        "patch_count": len(checkpoint.get("patch_artifact_ids") or []),
        "open_issue_count": len(checkpoint.get("open_issue_ids") or []),
        "yield_reason": str(checkpoint.get("yield_reason") or ""),
    }


def ensure_production_revision(
    *,
    episode_id: str,
    kind: Kind,
    input_fingerprint: str = "",
    contract_version: str = "",
    qa_profile_version: str = "",
    grant_id: str | None = None,
    resume: bool = True,
) -> ProductionRevision:
    """获取或创建 active revision。resume=True 时复用已有 active；False 时归档旧的并新建。"""
    ensure_production_revisions_table()
    conn = get_conn()
    if resume:
        existing = get_active_production_revision(episode_id, kind)
        if existing:
            return existing

    # 归档旧 active
    stamp = now()
    conn.execute(
        "UPDATE production_revisions SET status='superseded', updated_at=? "
        "WHERE episode_id=? AND kind=? AND status='active'",
        (stamp, episode_id, kind),
    )
    revision_id = new_id("rev")
    conn.execute(
        """INSERT INTO production_revisions(
            id, episode_id, kind, status, baseline_generation_count,
            input_fingerprint, contract_version, qa_profile_version, grant_id,
            checkpoint_json, created_at, updated_at
        ) VALUES(?,?,?,'active',0,?,?,?,?, '{}',?,?)""",
        (
            revision_id, episode_id, kind, input_fingerprint,
            contract_version, qa_profile_version, grant_id, stamp, stamp,
        ),
    )
    # 同步 episode 指针
    col = (
        "screenplay_production_revision_id"
        if kind == "screenplay"
        else "storyboard_production_revision_id"
    )
    try:
        conn.execute(f"UPDATE episodes SET {col}=? WHERE id=?", (revision_id, episode_id))
    except Exception:  # noqa: BLE001 — 列可能尚未迁移
        pass
    conn.commit()
    return get_production_revision(revision_id)  # type: ignore[return-value]


def mark_baseline_generated(
    revision_id: str,
    *,
    baseline_artifact_id: str | None = None,
    working_artifact_id: str | None = None,
) -> ProductionRevision:
    ensure_production_revisions_table()
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM production_revisions WHERE id=?", (revision_id,)
    ).fetchone()
    if not row:
        raise ValueError(f"production revision not found: {revision_id}")
    count = int(row["baseline_generation_count"] or 0) + 1
    stamp = now()
    conn.execute(
        """UPDATE production_revisions SET
            baseline_generation_count=?,
            baseline_artifact_id=COALESCE(?, baseline_artifact_id),
            working_artifact_id=COALESCE(?, working_artifact_id),
            updated_at=?
        WHERE id=?""",
        (count, baseline_artifact_id, working_artifact_id or baseline_artifact_id, stamp, revision_id),
    )
    # episode working pointer
    kind = row["kind"]
    episode_id = row["episode_id"]
    art = working_artifact_id or baseline_artifact_id
    if art:
        col = (
            "working_screenplay_artifact_id"
            if kind == "screenplay"
            else "working_storyboard_artifact_id"
        )
        try:
            conn.execute(f"UPDATE episodes SET {col}=? WHERE id=?", (art, episode_id))
        except Exception:  # noqa: BLE001
            pass
    conn.commit()
    return get_production_revision(revision_id)  # type: ignore[return-value]


def mark_first_evaluation(revision_id: str, evaluation_id: str) -> ProductionRevision:
    ensure_production_revisions_table()
    conn = get_conn()
    row = conn.execute(
        "SELECT first_evaluation_id FROM production_revisions WHERE id=?", (revision_id,)
    ).fetchone()
    if not row:
        raise ValueError(f"production revision not found: {revision_id}")
    if row["first_evaluation_id"]:
        return get_production_revision(revision_id)  # type: ignore[return-value]
    stamp = now()
    conn.execute(
        "UPDATE production_revisions SET first_evaluation_id=?, updated_at=? WHERE id=?",
        (evaluation_id, stamp, revision_id),
    )
    conn.commit()
    return get_production_revision(revision_id)  # type: ignore[return-value]


def update_working_artifact(revision_id: str, artifact_id: str, *, expected_hash: str | None = None) -> None:
    """CAS 更新 working_artifact_id。"""
    ensure_production_revisions_table()
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM production_revisions WHERE id=?", (revision_id,)
    ).fetchone()
    if not row:
        raise ValueError(f"production revision not found: {revision_id}")
    if expected_hash:
        current_id = row["working_artifact_id"]
        if current_id:
            art = conn.execute(
                "SELECT content_hash FROM artifacts WHERE id=?", (current_id,)
            ).fetchone()
            if art and art["content_hash"] and art["content_hash"] != expected_hash:
                raise RuntimeError("working artifact hash conflict")
    stamp = now()
    conn.execute(
        "UPDATE production_revisions SET working_artifact_id=?, updated_at=? WHERE id=?",
        (artifact_id, stamp, revision_id),
    )
    kind = row["kind"]
    episode_id = row["episode_id"]
    col = (
        "working_screenplay_artifact_id"
        if kind == "screenplay"
        else "working_storyboard_artifact_id"
    )
    try:
        conn.execute(f"UPDATE episodes SET {col}=? WHERE id=?", (artifact_id, episode_id))
    except Exception:  # noqa: BLE001
        pass
    conn.commit()


def save_checkpoint(revision_id: str, checkpoint: dict[str, Any]) -> None:
    ensure_production_revisions_table()
    conn = get_conn()
    conn.execute(
        "UPDATE production_revisions SET checkpoint_json=?, updated_at=? WHERE id=?",
        (json.dumps(checkpoint, ensure_ascii=False), now(), revision_id),
    )
    conn.commit()


def set_published_artifact(
    revision_id: str,
    artifact_id: str,
    *,
    certificate_id: str | None = None,
) -> None:
    ensure_production_revisions_table()
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM production_revisions WHERE id=?", (revision_id,)
    ).fetchone()
    if not row:
        raise ValueError(f"production revision not found: {revision_id}")
    stamp = now()
    conn.execute(
        "UPDATE production_revisions SET published_artifact_id=?, working_artifact_id=?, "
        "status='published', updated_at=? WHERE id=?",
        (artifact_id, artifact_id, stamp, revision_id),
    )
    kind = row["kind"]
    episode_id = row["episode_id"]
    if kind == "screenplay":
        try:
            conn.execute(
                "UPDATE episodes SET published_screenplay_artifact_id=?, "
                "working_screenplay_artifact_id=?, screenplay_artifact_id=?, "
                "screenplay_completion_certificate_id=? WHERE id=?",
                (artifact_id, artifact_id, artifact_id, certificate_id, episode_id),
            )
        except Exception:  # noqa: BLE001
            conn.execute(
                "UPDATE episodes SET screenplay_artifact_id=? WHERE id=?",
                (artifact_id, episode_id),
            )
    else:
        try:
            conn.execute(
                "UPDATE episodes SET published_storyboard_artifact_id=?, "
                "working_storyboard_artifact_id=?, storyboard_artifact_id=?, "
                "storyboard_completion_certificate_id=? WHERE id=?",
                (artifact_id, artifact_id, artifact_id, certificate_id, episode_id),
            )
        except Exception:  # noqa: BLE001
            conn.execute(
                "UPDATE episodes SET storyboard_artifact_id=? WHERE id=?",
                (artifact_id, episode_id),
            )
    conn.commit()
