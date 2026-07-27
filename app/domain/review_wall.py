"""评审墙的稳定对象、上游资格与评审工作流契约。

这个模块由 ``app.api`` 兼容门面在其命名空间内执行，因此路由和
原有领域函数共用同一个 ``router``。安全校验函数也供视频写路径调用，
保证 UI、Agent 和直接 REST 调用的口径一致。
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

try:
    router
except NameError:  # pragma: no cover - used when importing this module directly
    from app.domain.common import *


_REVIEW_TERMINAL_RUN_STATES = {
    "SUCCEEDED", "FAILED", "CANCELLED", "COMPLETED", "PARTIAL",
    "succeeded", "failed", "cancelled", "completed", "partial",
}
_REVIEW_ITEM_STATUSES = {"open", "in_progress", "resolved", "wont_fix"}
_REVIEW_SEVERITIES = {"low", "medium", "high", "blocker"}
_REVIEW_SHOT_STATUSES = {"pending", "in_review", "completed", "needs_recheck"}


def _review_sha(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _review_json(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return default


def _ensure_review_wall_tables(conn=None) -> None:
    """存量数据库的进程内兼容迁移。"""
    db = conn or get_conn()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS shot_review_items (
            id TEXT PRIMARY KEY, shot_id TEXT NOT NULL,
            anchor_json TEXT NOT NULL DEFAULT '{}', issue_type TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'medium', comment TEXT NOT NULL,
            assignee TEXT, status TEXT NOT NULL DEFAULT 'open',
            content_version TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 1,
            created_by TEXT NOT NULL DEFAULT 'user', created_at REAL NOT NULL, updated_at REAL NOT NULL,
            FOREIGN KEY(shot_id) REFERENCES shots(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_shot_review_items_shot
            ON shot_review_items(shot_id, status, updated_at);
        CREATE TABLE IF NOT EXISTS shot_review_states (
            shot_id TEXT PRIMARY KEY, review_status TEXT NOT NULL DEFAULT 'pending',
            revision INTEGER NOT NULL DEFAULT 1, decided_by TEXT NOT NULL DEFAULT 'user',
            completed_at REAL, updated_at REAL NOT NULL,
            FOREIGN KEY(shot_id) REFERENCES shots(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS video_version_archives (
            version_id TEXT PRIMARY KEY, archived_by TEXT NOT NULL DEFAULT 'user',
            reason TEXT, archived_at REAL NOT NULL,
            FOREIGN KEY(version_id) REFERENCES shot_versions(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS review_action_audit (
            id TEXT PRIMARY KEY, action TEXT NOT NULL, scope_type TEXT NOT NULL,
            scope_id TEXT NOT NULL, target_version TEXT, idempotency_key TEXT,
            old_state_json TEXT NOT NULL DEFAULT '{}', new_state_json TEXT NOT NULL DEFAULT '{}',
            reason TEXT, decided_by TEXT NOT NULL DEFAULT 'user', request_id TEXT, created_at REAL NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_review_action_idempotency
            ON review_action_audit(action, idempotency_key)
            WHERE idempotency_key IS NOT NULL AND idempotency_key != '';
        """
    )
    db.commit()


def _review_shot_content_version(shot: Any) -> str:
    row = dict(shot)
    return _review_sha({
        "id": row.get("id"),
        "storyboard_artifact_id": row.get("storyboard_artifact_id"),
        "shot_no": row.get("shot_no"),
        "duration_s": row.get("duration_s"),
        "action_desc": row.get("action_desc"),
        "narration": row.get("narration"),
        "dialogues": row.get("dialogues"),
        "shot_contract_json": row.get("shot_contract_json"),
    })[:24]


def _review_asset_qualification(conn, episode_id: str) -> dict[str, Any]:
    """Inspect the exact shot gallery source used by ``enqueue_shot``.

    The adopted version wins when it owns a gallery; otherwise the newest
    version with a gallery wins.  Selected legacy references without an
    explicit gate verdict are unverified and therefore fail closed for new
    production, while their historical videos remain readable.
    """
    rows = conn.execute(
        """SELECT v.id AS version_id, v.shot_id, v.version_no, v.image_inputs,
                  s.adopted_version_id
             FROM shot_versions v JOIN shots s ON s.id=v.shot_id
            WHERE s.episode_id=?
            ORDER BY v.shot_id, v.version_no DESC""",
        (episode_id,),
    ).fetchall()
    by_shot: dict[str, list[Any]] = {}
    for row in rows:
        by_shot.setdefault(row["shot_id"], []).append(row)
    selected_rows: list[Any] = []
    for versions in by_shot.values():
        adopted_id = versions[0]["adopted_version_id"]
        adopted = next((row for row in versions if row["version_id"] == adopted_id), None)
        adopted_inputs = _review_json(adopted["image_inputs"], {}) if adopted else {}
        if adopted and adopted_inputs.get("reference_images"):
            selected_rows.append(adopted)
            continue
        fallback = next(
            (row for row in versions if _review_json(row["image_inputs"], {}).get("reference_images")),
            None,
        )
        if fallback:
            selected_rows.append(fallback)
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    qualified_inputs: list[dict[str, Any]] = []
    checked = 0
    for row in selected_rows:
        inputs = _review_json(row["image_inputs"], {})
        for ref in inputs.get("reference_images") or []:
            if ref.get("deleted") or not ref.get("selectedForSeedance"):
                continue
            checked += 1
            qa = ref.get("qa") or {}
            hard = list(qa.get("hard_failures") or ref.get("hard_failures") or [])
            gate = str(ref.get("gate_status") or ref.get("downstream_eligibility") or qa.get("status") or "").lower()
            if qa.get("qa_recovered"):
                gate = "unverified"
            payload = {
                "shot_id": row["shot_id"], "version_id": row["version_id"], "ref_id": ref.get("id"),
                "entity_type": ref.get("entity_type"), "entity_name": ref.get("entity_name"),
                "asset_version": ref.get("library_revision_id") or ref.get("library_view_id"),
                "rule_version": ref.get("rule_version") or qa.get("rule_version"),
                "hard_failures": hard,
            }
            if not gate:
                gate = "scored"
                payload["gate_status"] = gate
            payload["gate_status"] = gate
            payload["soft_warnings"] = [
                str(item) for item in (ref.get("soft_warnings") or qa.get("issues") or [])
            ]
            # Score-only：QA hard gate / unverified 只进 soft_warnings，不进 blockers（PRD QA-SO #32）。
            for msg in hard:
                warnings.append({**payload, "warning": f"qa_hard_failure:{msg}"})
            if qa.get("hard_gate_passed") is False:
                warnings.append({**payload, "warning": "hard_gate_not_passed_score_only"})
            if gate in {"failed", "hard_failed", "unverified", "unknown", "ineligible", "pending"}:
                warnings.append({**payload, "warning": f"gate_status:{gate}"})
            # 结构缺失才阻断：无实体引用或明确标记文件缺失
            missing_file = bool(ref.get("file_missing") or ref.get("missing"))
            if missing_file:
                payload["reason"] = "资产文件缺失"
                blockers.append(payload)
            else:
                qualified_inputs.append(payload)
            for warning in ref.get("soft_warnings") or qa.get("issues") or []:
                warnings.append({**payload, "warning": str(warning)})
    return {
        "eligible": not blockers,
        "status": "blocked" if blockers else ("passed" if checked else "no_selected_inputs"),
        "checked_inputs": checked,
        "inputs": qualified_inputs,
        "blockers": blockers,
        "soft_warnings": warnings,
    }


def _review_upstream_snapshot(episode_id: str) -> dict[str, Any]:
    conn = get_conn()
    ep_row = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep_row:
        raise HTTPException(404, "分集不存在")
    ep = dict(ep_row)
    published_screenplay = ep.get("published_screenplay_artifact_id") or ep.get("screenplay_artifact_id")
    published_storyboard = ep.get("published_storyboard_artifact_id") or ep.get("storyboard_artifact_id")
    active: list[dict[str, Any]] = []
    for kind, run_id in (
        ("screenplay", ep.get("active_screenplay_run_id")),
        ("storyboard", ep.get("active_storyboard_run_id")),
    ):
        if not run_id:
            continue
        run = conn.execute("SELECT id, status, current_step_key, updated_at FROM workflow_runs WHERE id=?", (run_id,)).fetchone()
        if not run or run["status"] not in _REVIEW_TERMINAL_RUN_STATES:
            active.append({
                "kind": kind, "run_id": run_id,
                "status": run["status"] if run else "unknown",
                "stage": run["current_step_key"] if run else None,
                "updated_at": run["updated_at"] if run else None,
            })
    # 旧任务没有 workflow_run 时，剧集状态仍要 fail-closed。
    if ep.get("status") in {"scripting", "storyboarding", "planned"} and not active:
        active.append({"kind": "upstream", "run_id": None, "status": ep.get("status"), "stage": None})
    confirmed = ep.get("status") in {"confirmed", "generating", "done", "mixed"}
    has_artifacts = bool(published_screenplay and published_storyboard)
    assets = _review_asset_qualification(conn, episode_id)
    blockers: list[str] = []
    if not published_screenplay:
        blockers.append("尚无已发布剧本")
    if not published_storyboard or not confirmed:
        blockers.append("分镜尚未完整确认")
    if active:
        blockers.append("编剧或分镜任务仍在运行")
    if not assets["eligible"]:
        blockers.append("人物/场景资产硬门禁未通过或未验证")
    raw = {
        "episode_id": episode_id,
        "episode_status": ep.get("status"),
        "published_screenplay_artifact_id": published_screenplay,
        "confirmed_storyboard_artifact_id": published_storyboard,
        "screenplay_revision": ep.get("screenplay_production_revision_id"),
        "storyboard_revision": ep.get("storyboard_production_revision_id"),
        "active_upstream_runs": active,
        "asset_status": assets["status"],
        "asset_inputs": assets["inputs"],
        "asset_blockers": assets["blockers"],
        "asset_soft_warnings": assets["soft_warnings"],
    }
    qualification_material = {
        **raw,
        # The gallery may be copied into a newly queued version. The version
        # row is lineage, not an upstream dependency; hashing it would make a
        # run invalidate itself even when every asset/rule verdict is equal.
        "asset_inputs": [
            {key: value for key, value in item.items() if key != "version_id"}
            for item in assets["inputs"]
        ],
    }
    return {
        **raw,
        "qualification_version": _review_sha(qualification_material)[:32],
        "eligible_for_production": bool(confirmed and has_artifacts and not active and assets["eligible"]),
        "blockers": blockers,
        "assets": assets,
        "server_time": now(),
    }


def _review_assert_positive_action(episode_id: str, expected_qualification_version: str | None = None) -> dict[str, Any]:
    snapshot = _review_upstream_snapshot(episode_id)
    if expected_qualification_version and expected_qualification_version != snapshot["qualification_version"]:
        raise HTTPException(409, {
            "code": "REVIEW_QUALIFICATION_CHANGED",
            "message": "上游或资产资格已变化，请重新预演",
            "qualification": snapshot,
        })
    if not snapshot["eligible_for_production"]:
        raise HTTPException(409, {
            "code": "REVIEW_PRODUCTION_BLOCKED",
            "message": "；".join(snapshot["blockers"]) or "当前不可执行正向媒体生产",
            "qualification": snapshot,
        })
    return snapshot


def _review_assert_shot_positive(shot_id: str, expected_qualification_version: str | None = None) -> dict[str, Any]:
    row = get_conn().execute("SELECT episode_id FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not row:
        raise HTTPException(404, "镜头不存在")
    return _review_assert_positive_action(row["episode_id"], expected_qualification_version)


def _review_assert_reference_restore(version_id: str, ref_id: str) -> dict[str, Any]:
    conn = get_conn()
    row = conn.execute(
        """SELECT s.episode_id, v.image_inputs FROM shot_versions v
             JOIN shots s ON s.id=v.shot_id WHERE v.id=?""",
        (version_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "视频版本不存在")
    snapshot = _review_assert_positive_action(row["episode_id"])
    refs = _review_json(row["image_inputs"], {}).get("reference_images") or []
    ref = next((item for item in refs if item.get("id") == ref_id), None)
    if ref is None:
        raise HTTPException(404, "参考图不存在")
    qa = ref.get("qa") or {}
    hard = qa.get("hard_failures") or ref.get("hard_failures") or []
    gate = str(ref.get("gate_status") or ref.get("downstream_eligibility") or qa.get("status") or "unverified").lower()
    if hard or gate in {"failed", "hard_failed", "unverified", "unknown", "ineligible", "pending"}:
        raise HTTPException(409, {
            "code": "REFERENCE_NOT_ELIGIBLE",
            "message": "该参考图硬门禁未通过或尚未验证，不能恢复为新生产输入",
            "ref_id": ref_id, "hard_failures": hard, "gate_status": gate,
        })
    return snapshot


def _review_write_audit(
    action: str, scope_type: str, scope_id: str, *, target_version: str | None = None,
    idempotency_key: str | None = None, old_state: Any = None, new_state: Any = None,
    reason: str | None = None, decided_by: str = "user", request_id: str | None = None,
) -> dict[str, Any]:
    _ensure_review_wall_tables()
    conn = get_conn()
    if idempotency_key:
        existing = conn.execute(
            "SELECT * FROM review_action_audit WHERE action=? AND idempotency_key=?",
            (action, idempotency_key),
        ).fetchone()
        if existing:
            return dict(existing)
    audit_id = new_id("review_audit")
    conn.execute(
        """INSERT INTO review_action_audit(
               id, action, scope_type, scope_id, target_version, idempotency_key,
               old_state_json, new_state_json, reason, decided_by, request_id, created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            audit_id, action, scope_type, scope_id, target_version, idempotency_key,
            json.dumps(old_state or {}, ensure_ascii=False),
            json.dumps(new_state or {}, ensure_ascii=False),
            reason, decided_by, request_id, now(),
        ),
    )
    conn.commit()
    return {"id": audit_id, "action": action, "scope_type": scope_type, "scope_id": scope_id}


def _review_idempotent_state(
    action: str, key: Any, *, scope_id: str | None = None,
) -> dict[str, Any] | None:
    """Return the original response state for a retried review-wall command."""
    token = str(key or "").strip()
    if not token:
        return None
    sql = "SELECT new_state_json FROM review_action_audit WHERE action=? AND idempotency_key=?"
    params: list[Any] = [action, token]
    if scope_id is not None:
        sql += " AND scope_id=?"
        params.append(scope_id)
    row = get_conn().execute(sql, params).fetchone()
    if not row:
        return None
    return _review_json(row["new_state_json"], {})


def _review_item_public(row: Any, current_content_version: str | None = None) -> dict[str, Any]:
    item = dict(row)
    item["anchor"] = _review_json(item.pop("anchor_json", "{}"), {})
    item["anchor_stale"] = bool(current_content_version and item.get("content_version") != current_content_version)
    return item


@router.get("/episodes/{episode_id}/review-context")
def review_wall_context(episode_id: str):
    _ensure_review_wall_tables()
    conn = get_conn()
    snapshot = _review_upstream_snapshot(episode_id)
    shots = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,),
    ).fetchall()
    shot_ids = [row["id"] for row in shots]
    states: dict[str, dict[str, Any]] = {}
    items_by_shot: dict[str, list[dict[str, Any]]] = {shot_id: [] for shot_id in shot_ids}
    if shot_ids:
        marks = ",".join("?" for _ in shot_ids)
        for row in conn.execute(f"SELECT * FROM shot_review_states WHERE shot_id IN ({marks})", shot_ids).fetchall():
            states[row["shot_id"]] = dict(row)
        content_versions = {row["id"]: _review_shot_content_version(row) for row in shots}
        for row in conn.execute(
            f"SELECT * FROM shot_review_items WHERE shot_id IN ({marks}) ORDER BY updated_at DESC", shot_ids,
        ).fetchall():
            items_by_shot[row["shot_id"]].append(_review_item_public(row, content_versions[row["shot_id"]]))
    archived = {
        row["version_id"]: dict(row)
        for row in conn.execute(
            """SELECT a.* FROM video_version_archives a JOIN shot_versions v ON v.id=a.version_id
                 JOIN shots s ON s.id=v.shot_id WHERE s.episode_id=?""", (episode_id,),
        ).fetchall()
    }
    summaries = []
    for row in shots:
        shot_id = row["id"]
        items = items_by_shot.get(shot_id, [])
        state = states.get(shot_id) or {"review_status": "pending", "revision": 0, "updated_at": None}
        summaries.append({
            "shot_id": shot_id,
            "content_version": _review_shot_content_version(row),
            "review_status": state.get("review_status", "pending"),
            "review_revision": state.get("revision", 0),
            "review_updated_at": state.get("updated_at"),
            "open_issue_count": sum(item["status"] in {"open", "in_progress"} for item in items),
            "blocker_count": sum(item["status"] in {"open", "in_progress"} and item["severity"] == "blocker" for item in items),
            "review_items": items,
        })
    return {
        "episode_id": episode_id,
        "object_version": _review_sha({"qualification": snapshot["qualification_version"], "shots": [(r["id"], _review_shot_content_version(r)) for r in shots]})[:32],
        "upstream": snapshot,
        "shots": summaries,
        "archived_versions": archived,
        "authorization_constraints": {
            "budget_cap_cny": {"type": "number", "unit": "CNY", "default": 150, "min": 1, "max": 100000, "step": 1, "finite": True},
            "wall_clock_cap_s": {"type": "number", "unit": "seconds", "default": 14400, "min": 60, "max": 604800, "step": 60, "finite": True},
            "add_budget_cny": {"type": "number", "unit": "CNY", "default": 50, "min": 1, "max": 100000, "step": 1, "finite": True},
            "add_wall_clock_s": {"type": "number", "unit": "seconds", "default": 3600, "min": 60, "max": 604800, "step": 60, "finite": True},
        },
        "server_time": now(),
    }


@router.post("/shots/{shot_id}/review-items")
def create_shot_review_item(shot_id: str, body: dict = Body(...)):
    _ensure_review_wall_tables()
    repeated = _review_idempotent_state(
        "review_item.create", body.get("idempotency_key"), scope_id=shot_id,
    )
    if repeated:
        return _review_item_public(repeated)
    conn = get_conn()
    shot = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot:
        raise HTTPException(404, "镜头不存在")
    comment = str(body.get("comment") or "").strip()
    issue_type = str(body.get("issue_type") or "other").strip()
    severity = str(body.get("severity") or "medium")
    if not comment:
        raise HTTPException(422, "批注内容不能为空")
    if severity not in _REVIEW_SEVERITIES:
        raise HTTPException(422, "severity 无效")
    content_version = _review_shot_content_version(shot)
    expected = body.get("content_version")
    if expected and expected != content_version:
        raise HTTPException(409, {"code": "REVIEW_CONTENT_CHANGED", "message": "镜头内容已更新，请重新定位批注", "content_version": content_version})
    item_id = new_id("review")
    ts = now()
    conn.execute(
        """INSERT INTO shot_review_items(
               id, shot_id, anchor_json, issue_type, severity, comment, assignee,
               status, content_version, revision, created_by, created_at, updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?)""",
        (
            item_id, shot_id, json.dumps(body.get("anchor") or {}, ensure_ascii=False),
            issue_type, severity, comment, (str(body.get("assignee") or "").strip() or None),
            "open", content_version, str(body.get("created_by") or "user"), ts, ts,
        ),
    )
    conn.execute(
        """INSERT INTO shot_review_states(shot_id, review_status, revision, decided_by, updated_at)
           VALUES(?, 'in_review', 1, ?, ?)
           ON CONFLICT(shot_id) DO UPDATE SET
             review_status=CASE WHEN review_status='completed' THEN 'needs_recheck' ELSE 'in_review' END,
             revision=revision+1, decided_by=excluded.decided_by, updated_at=excluded.updated_at""",
        (shot_id, str(body.get("created_by") or "user"), ts),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM shot_review_items WHERE id=?", (item_id,)).fetchone()
    _review_write_audit(
        "review_item.create", "shot", shot_id, target_version=content_version,
        idempotency_key=body.get("idempotency_key"), request_id=body.get("request_id"),
        new_state=dict(row),
    )
    return _review_item_public(row, content_version)


@router.put("/review-items/{item_id}")
def update_shot_review_item(item_id: str, body: dict = Body(...)):
    _ensure_review_wall_tables()
    conn = get_conn()
    row = conn.execute("SELECT * FROM shot_review_items WHERE id=?", (item_id,)).fetchone()
    if not row:
        raise HTTPException(404, "评审项不存在")
    repeated = _review_idempotent_state(
        "review_item.update", body.get("idempotency_key"), scope_id=row["shot_id"],
    )
    if repeated:
        if repeated.get("id") != item_id:
            raise HTTPException(409, "幂等键已用于其他评审项")
        return _review_item_public(repeated)
    expected = body.get("expected_revision")
    if expected is not None and int(expected) != int(row["revision"]):
        raise HTTPException(409, {"code": "REVIEW_ITEM_CONFLICT", "message": "评审项已被其他人更新", "latest": _review_item_public(row)})
    status = str(body.get("status", row["status"]))
    severity = str(body.get("severity", row["severity"]))
    comment = str(body.get("comment", row["comment"])).strip()
    if status not in _REVIEW_ITEM_STATUSES or severity not in _REVIEW_SEVERITIES or not comment:
        raise HTTPException(422, "评审项字段无效")
    assignee = body.get("assignee", row["assignee"])
    old = dict(row)
    conn.execute(
        """UPDATE shot_review_items SET status=?, severity=?, comment=?, assignee=?,
                  revision=revision+1, updated_at=? WHERE id=?""",
        (status, severity, comment, (str(assignee).strip() if assignee else None), now(), item_id),
    )
    conn.commit()
    latest = conn.execute("SELECT * FROM shot_review_items WHERE id=?", (item_id,)).fetchone()
    _review_write_audit(
        "review_item.update", "shot", row["shot_id"],
        target_version=row["content_version"], idempotency_key=body.get("idempotency_key"),
        request_id=body.get("request_id"), old_state=old, new_state=dict(latest),
    )
    return _review_item_public(latest)


@router.post("/shots/{shot_id}/review-state")
def set_shot_review_state(shot_id: str, body: dict = Body(...)):
    _ensure_review_wall_tables()
    repeated = _review_idempotent_state(
        "review_state.update", body.get("idempotency_key"), scope_id=shot_id,
    )
    if repeated:
        return repeated
    conn = get_conn()
    shot = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot:
        raise HTTPException(404, "镜头不存在")
    status = str(body.get("review_status") or "")
    if status not in _REVIEW_SHOT_STATUSES:
        raise HTTPException(422, "review_status 无效")
    expected = body.get("expected_revision")
    current = conn.execute("SELECT * FROM shot_review_states WHERE shot_id=?", (shot_id,)).fetchone()
    current_revision = int(current["revision"] if current else 0)
    if expected is not None and int(expected) != current_revision:
        raise HTTPException(409, {"code": "REVIEW_STATE_CONFLICT", "message": "镜头评审状态已变化", "latest": dict(current) if current else None})
    if status == "completed":
        blockers = conn.execute(
            """SELECT COUNT(*) AS c FROM shot_review_items
               WHERE shot_id=? AND status IN ('open','in_progress') AND severity='blocker'""",
            (shot_id,),
        ).fetchone()["c"]
        if blockers:
            raise HTTPException(409, "仍有未处理的阻断问题，不能标记评审完成")
    ts = now()
    conn.execute(
        """INSERT INTO shot_review_states(shot_id, review_status, revision, decided_by, completed_at, updated_at)
           VALUES(?,?,1,?,?,?)
           ON CONFLICT(shot_id) DO UPDATE SET review_status=excluded.review_status,
             revision=shot_review_states.revision+1, decided_by=excluded.decided_by,
             completed_at=excluded.completed_at, updated_at=excluded.updated_at""",
        (shot_id, status, str(body.get("decided_by") or "user"), ts if status == "completed" else None, ts),
    )
    conn.commit()
    latest = dict(conn.execute("SELECT * FROM shot_review_states WHERE shot_id=?", (shot_id,)).fetchone())
    _review_write_audit(
        "review_state.update", "shot", shot_id,
        target_version=_review_shot_content_version(shot),
        idempotency_key=body.get("idempotency_key"), request_id=body.get("request_id"),
        old_state=dict(current) if current else {}, new_state=latest,
    )
    return latest


@router.post("/versions/{version_id}/archive")
def archive_video_version(version_id: str, body: dict | None = Body(None)):
    _ensure_review_wall_tables()
    body = body or {}
    conn = get_conn()
    row = conn.execute(
        """SELECT v.*, s.adopted_version_id FROM shot_versions v
             JOIN shots s ON s.id=v.shot_id WHERE v.id=?""", (version_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "视频版本不存在")
    if row["adopted_version_id"] == version_id:
        raise HTTPException(409, "当前采用版不能归档")
    conn.execute(
        """INSERT INTO video_version_archives(version_id, archived_by, reason, archived_at)
           VALUES(?,?,?,?) ON CONFLICT(version_id) DO NOTHING""",
        (version_id, str(body.get("archived_by") or "user"), str(body.get("reason") or "").strip() or None, now()),
    )
    conn.commit()
    _review_write_audit("video_version.archive", "version", version_id, reason=body.get("reason"))
    return {"version_id": version_id, "archived": True}


@router.delete("/versions/{version_id}/archive")
def unarchive_video_version(version_id: str):
    _ensure_review_wall_tables()
    conn = get_conn()
    conn.execute("DELETE FROM video_version_archives WHERE version_id=?", (version_id,))
    conn.commit()
    _review_write_audit("video_version.unarchive", "version", version_id)
    return {"version_id": version_id, "archived": False}


@router.get("/review-wall/events")
def review_wall_events(episode_id: str, limit: int = 100):
    """脱敏埋点/审计投影：只返回稳定对象与状态，不返回批注正文。"""
    _ensure_review_wall_tables()
    limit = max(1, min(int(limit), 500))
    rows = get_conn().execute(
        """SELECT a.id, a.action, a.scope_type, a.scope_id, a.target_version,
                  a.decided_by, a.request_id, a.created_at
             FROM review_action_audit a
            WHERE (a.scope_type='episode' AND a.scope_id=?)
               OR (a.scope_type='shot' AND a.scope_id IN (SELECT id FROM shots WHERE episode_id=?))
            ORDER BY a.created_at DESC LIMIT ?""",
        (episode_id, episode_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def _review_validate_authorization_number(
    value: Any, *, field: str, minimum: float, maximum: float, allow_none: bool = True,
) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool):
        raise HTTPException(422, f"{field} 必须是数字")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, f"{field} 必须是数字") from exc
    if not math.isfinite(number):
        raise HTTPException(422, f"{field} 必须是有限数")
    if number < minimum or number > maximum:
        raise HTTPException(422, f"{field} 必须在 {minimum:g} 到 {maximum:g} 之间")
    return number
