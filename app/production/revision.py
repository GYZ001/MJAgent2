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


def rebind_input_fingerprint(
    revision_id: str,
    *,
    input_fingerprint: str,
    expected_working_artifact_id: str,
    conn=None,
    commit: bool = True,
) -> ProductionRevision:
    """CAS-bind an active, QA-verified working revision to current authority."""
    if not input_fingerprint or not expected_working_artifact_id:
        raise ValueError("revision 指纹重绑缺少权威指纹或 working artifact")
    db = conn or get_conn()
    cursor = db.execute(
        """UPDATE production_revisions
              SET input_fingerprint=?, updated_at=?
            WHERE id=? AND status='active'
              AND working_artifact_id=?
              AND published_artifact_id IS NULL""",
        (
            input_fingerprint,
            now(),
            revision_id,
            expected_working_artifact_id,
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError("revision 指纹重绑发生 CAS 冲突")
    if commit:
        db.commit()
    row = db.execute(
        "SELECT * FROM production_revisions WHERE id=?",
        (revision_id,),
    ).fetchone()
    revision = _row_to_revision(row)
    if revision is None:
        raise ValueError("revision 指纹重绑后记录不存在")
    return revision


def bind_unpublished_revision_metadata(
    revision_id: str,
    *,
    input_fingerprint: str,
    contract_version: str,
    qa_profile_version: str,
    conn=None,
    commit: bool = True,
) -> ProductionRevision:
    """Bind metadata omitted when a resumable revision was first initialized.

    Storyboard revisions are created before a final board exists, so their
    content fingerprint is not available at initialization. This CAS only
    fills blank values (or accepts exact matches) on an unpublished active
    revision; conflicting non-empty metadata still fails closed.
    """
    if not input_fingerprint or not contract_version or not qa_profile_version:
        raise ValueError("revision 元数据绑定缺少指纹、契约或评分版本")
    db = conn or get_conn()
    row = db.execute(
        "SELECT * FROM production_revisions WHERE id=?",
        (revision_id,),
    ).fetchone()
    if row is None:
        raise ValueError("revision 元数据绑定的记录不存在")
    if row["status"] != "active" or row["published_artifact_id"]:
        raise ValueError("只能绑定尚未发布的 active revision")
    requested = {
        "input_fingerprint": input_fingerprint,
        "contract_version": contract_version,
        "qa_profile_version": qa_profile_version,
    }
    for field, value in requested.items():
        current = str(row[field] or "")
        if current and current != value:
            raise ValueError(f"revision {field} 已绑定其他版本")
    cursor = db.execute(
        """UPDATE production_revisions
              SET input_fingerprint=?,contract_version=?,qa_profile_version=?,
                  updated_at=?
            WHERE id=? AND status='active' AND published_artifact_id IS NULL
              AND input_fingerprint IN ('',?)
              AND contract_version IN ('',?)
              AND qa_profile_version IN ('',?)""",
        (
            input_fingerprint,
            contract_version,
            qa_profile_version,
            now(),
            revision_id,
            input_fingerprint,
            contract_version,
            qa_profile_version,
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError("revision 元数据绑定发生 CAS 冲突")
    if commit:
        db.commit()
    revision = _row_to_revision(db.execute(
        "SELECT * FROM production_revisions WHERE id=?",
        (revision_id,),
    ).fetchone())
    if revision is None:
        raise ValueError("revision 元数据绑定后记录不存在")
    return revision


def screenplay_production_state(episode_id: str) -> dict[str, Any]:
    """Return the persisted screenplay stage and UI-safe recovery action."""
    from app import task_registry

    stage_order = [
        ("CHARACTER_DISCOVERY", "人物识别"),
        ("GENERATING_BASELINE", "生成首版"),
        ("STRUCTURE_VALIDATION", "结构校验"),
        ("QUALITY_SCORING", "质量评分"),
        ("PUBLISHING", "原子发布"),
        ("SUCCEEDED", "已完成"),
    ]
    rev = get_active_production_revision(episode_id, "screenplay")
    if rev is None:
        rev = _row_to_revision(get_conn().execute(
            """SELECT revision.*
                 FROM production_revisions AS revision
                 JOIN episodes AS episode
                   ON episode.screenplay_production_revision_id=revision.id
                WHERE episode.id=?
                  AND revision.episode_id=episode.id
                  AND revision.kind='screenplay'
                  AND revision.status='published'
                  AND episode.screenplay_artifact_id IS NOT NULL
                  AND episode.screenplay_artifact_id=revision.published_artifact_id
                LIMIT 1""",
            (episode_id,),
        ).fetchone())
    active = task_registry.active("screenplay", episode_id)
    if rev is None:
        return {
            "operation": "baseline",
            "phase": "CHARACTER_DISCOVERY",
            "phase_label": "人物识别",
            "stage_index": 0,
            "stage_count": len(stage_order),
            "stages": [
                {"key": key, "label": label, "status": "pending"}
                for key, label in stage_order
            ],
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
    published = bool(rev.published_artifact_id)
    phase = str(
        checkpoint.get("phase")
        or ("SUCCEEDED" if published else "STRUCTURE_VALIDATION" if has_working_baseline
            else "GENERATING_BASELINE")
    )
    phase_aliases = {
        "BASELINE": "GENERATING_BASELINE",
        "QA": "QUALITY_SCORING",
        "WAITING_HUMAN": "STRUCTURE_VALIDATION",
        "FAILED": "STRUCTURE_VALIDATION",
    }
    phase = phase_aliases.get(phase, phase)
    stage_keys = [key for key, _label in stage_order]
    stage_index = (
        stage_keys.index(phase)
        if phase in stage_keys
        else (len(stage_order) - 1 if published else 0)
    )
    stages = []
    for index, (key, label) in enumerate(stage_order):
        if published or index < stage_index:
            status = "completed"
        elif index == stage_index:
            status = "in_progress" if active else "paused"
        else:
            status = "pending"
        stages.append({"key": key, "label": label, "status": status})
    return {
        "revision_id": rev.id,
        "operation": (
            "complete" if published else "finalize" if has_working_baseline else "baseline"
        ),
        "phase": phase,
        "phase_label": dict(stage_order).get(phase, phase),
        "stage_index": stage_index,
        "stage_count": len(stage_order),
        "stages": stages,
        "baseline_done": rev.baseline_done,
        "first_evaluation_done": rev.first_evaluation_done,
        "task_active": active,
        "can_resume_repair": bool(has_working_baseline and not published and not active),
        "activation_count": int(checkpoint.get("activation_no") or 0),
        "patch_count": len(checkpoint.get("patch_artifact_ids") or []),
        "open_issue_count": len(checkpoint.get("open_issue_ids") or []),
        "quality_score": checkpoint.get("quality_score"),
        "quality_issue_count": int(checkpoint.get("quality_issue_count") or 0),
        "gate_retry_exhausted": bool(checkpoint.get("gate_retry_exhausted")),
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
    sync_episode_pointer: bool = True,
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
    if sync_episode_pointer:
        col = (
            "screenplay_production_revision_id"
            if kind == "screenplay"
            else "storyboard_production_revision_id"
        )
        conn.execute(f"UPDATE episodes SET {col}=? WHERE id=?", (revision_id, episode_id))
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
    conn=None,
    commit: bool = True,
) -> None:
    if conn is None:
        ensure_production_revisions_table()
    db = conn or get_conn()
    row = db.execute(
        "SELECT * FROM production_revisions WHERE id=?", (revision_id,)
    ).fetchone()
    if not row:
        raise ValueError(f"production revision not found: {revision_id}")
    stamp = now()
    db.execute(
        "UPDATE production_revisions SET published_artifact_id=?, working_artifact_id=?, "
        "status='published', updated_at=? WHERE id=?",
        (artifact_id, artifact_id, stamp, revision_id),
    )
    kind = row["kind"]
    episode_id = row["episode_id"]
    if kind == "screenplay":
        db.execute(
            "UPDATE episodes SET published_screenplay_artifact_id=?, "
            "working_screenplay_artifact_id=?, screenplay_artifact_id=?, "
            "screenplay_completion_certificate_id=?, "
            "screenplay_production_revision_id=? WHERE id=?",
            (
                artifact_id, artifact_id, artifact_id, certificate_id,
                revision_id, episode_id,
            ),
        )
    else:
        db.execute(
            "UPDATE episodes SET published_storyboard_artifact_id=?, "
            "working_storyboard_artifact_id=?, storyboard_artifact_id=?, "
            "storyboard_completion_certificate_id=?, "
            "storyboard_production_revision_id=? WHERE id=?",
            (
                artifact_id, artifact_id, artifact_id, certificate_id,
                revision_id, episode_id,
            ),
        )
    if commit:
        db.commit()
