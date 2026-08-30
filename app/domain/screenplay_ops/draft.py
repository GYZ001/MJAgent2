"""剧本草稿的查询、保存（含权威字段 payload 编排）与删除。

从 app/domain/screenplay_ops.py 按原样搬移；依赖 edit。
"""
from __future__ import annotations

import json

from app.db import (
    get_conn,
    new_id,
    now,
)
from app.domain.common import (
    _episode_or_404,
    router,
)
from app.schemas import (
    EpisodeScreenplay,
    schema_errors,
)
from fastapi import HTTPException

from .edit import _screenplay_payload_with_authority_fields


@router.get("/episodes/{episode_id}/screenplay/draft")
def get_screenplay_draft(episode_id: str):
    _episode_or_404(episode_id)
    row = get_conn().execute(
        "SELECT * FROM screenplay_drafts WHERE episode_id=?",
        (episode_id,),
    ).fetchone()
    if not row:
        return {"draft": None}
    value = dict(row)
    raw = value.pop("content_json")
    value.pop("constraint_json", None)
    if raw is None:
        return {"draft": None}
    try:
        value["content"] = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {"draft": None}
    return {"draft": value}

@router.put("/episodes/{episode_id}/screenplay/draft")
def save_screenplay_draft(episode_id: str, body: dict):
    ep = dict(_episode_or_404(episode_id))
    content = body.get("content")
    if content is None:
        raise HTTPException(422, "草稿内容不能为空")
    content = _screenplay_payload_with_authority_fields(episode_id, content)
    baseline = body.get("baseline_artifact_id")
    current = ep.get("screenplay_artifact_id")
    validation: dict = {"baseline_current": str(baseline or "") == str(current or "")}
    if content is not None:
        _, schema_validation = schema_errors(EpisodeScreenplay, content)
        validation["schema_errors"] = schema_validation
    stamp = now()
    conn = get_conn()
    draft_id = str(body.get("draft_id") or new_id("scrdraft"))
    conn.execute(
        """INSERT INTO screenplay_drafts(
               id, episode_id, baseline_artifact_id,
               content_json, constraint_json, dirty_at, updated_at
           ) VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(episode_id) DO UPDATE SET
               baseline_artifact_id=excluded.baseline_artifact_id,
               content_json=excluded.content_json,
               constraint_json=excluded.constraint_json,
               dirty_at=excluded.dirty_at, updated_at=excluded.updated_at""",
        (
            draft_id, episode_id, baseline,
            json.dumps(content, ensure_ascii=False),
            "{}",
            stamp, stamp,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM screenplay_drafts WHERE episode_id=?",
        (episode_id,),
    ).fetchone()
    return {"saved": True, "draft_id": row["id"], "updated_at": stamp, "validation": validation}

@router.delete("/episodes/{episode_id}/screenplay/draft")
def delete_screenplay_draft(episode_id: str):
    _episode_or_404(episode_id)
    conn = get_conn()
    cursor = conn.execute(
        "DELETE FROM screenplay_drafts WHERE episode_id=?",
        (episode_id,),
    )
    conn.commit()
    return {"deleted": bool(cursor.rowcount)}
