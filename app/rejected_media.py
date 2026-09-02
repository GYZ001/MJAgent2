"""Permanently remove rejected visual assets and every downstream reference."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.db import get_conn


REJECTED_CANDIDATE_STATUSES = {
    "blocked_deleted",
    "cleanup_pending",
    "discarded",
    "discarded_deleted",
    "discarded_pending_cleanup",
    "technical_failed",
}


def qa_is_rejected(qa: Any) -> bool:
    """Only the persisted typed runtime gate authorizes destructive purge."""
    return isinstance(qa, dict) and qa.get("runtime_blocking") is True


def reference_dict_is_rejected(ref: Any) -> bool:
    if not isinstance(ref, dict):
        return False
    return bool(ref.get("deleted") or qa_is_rejected(ref.get("qa")))


def discard_file(path: str | None) -> bool:
    """Best-effort unlink. A missing file already satisfies the purge policy."""
    if not path:
        return False
    target = Path(path)
    existed = target.exists()
    try:
        target.unlink(missing_ok=True)
    except OSError:
        return False
    return existed


def _json(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _evaluation_rejected(row: Any) -> bool:
    status = str(row["status"] or "").lower()
    role = str(row["evaluation_role"] or "") if "evaluation_role" in row.keys() else ""
    runtime_blocking = bool(row["runtime_blocking"]) if "runtime_blocking" in row.keys() else False
    if role == "score_only":
        return False
    if status in {"failed", "error", "rejected"} or runtime_blocking:
        return True
    evidence = _json(row["evidence_json"], {})
    qa = evidence.get("qa") if isinstance(evidence, dict) else None
    if isinstance(qa, dict) and qa.get("evaluation_role") == "score_only":
        return False
    if qa_is_rejected(qa):
        return True
    issues = _json(row["issues_json"], [])
    return qa_is_rejected({"issues": issues})


def _artifact_is_rejected(conn, artifact: Any) -> bool:
    if str(artifact["status"] or "").lower() == "rejected":
        return True
    evaluations = conn.execute(
        "SELECT * FROM evaluations WHERE artifact_id=? ORDER BY created_at DESC",
        (artifact["id"],),
    ).fetchall()
    return any(_evaluation_rejected(row) for row in evaluations)


def _portrait_paths(conn, portrait_id: str) -> list[str]:
    row = conn.execute(
        "SELECT image_path FROM character_portraits WHERE id=?", (portrait_id,),
    ).fetchone()
    views = conn.execute(
        "SELECT image_path FROM character_portrait_views WHERE portrait_id=?", (portrait_id,),
    ).fetchall()
    return [
        str(item["image_path"])
        for item in [*([row] if row else []), *views]
        if item["image_path"]
    ]


def _scene_paths(conn, scene_reference_id: str) -> list[str]:
    row = conn.execute(
        "SELECT image_path FROM scene_references WHERE id=?", (scene_reference_id,),
    ).fetchone()
    views = conn.execute(
        "SELECT image_path FROM scene_reference_views WHERE scene_reference_id=?",
        (scene_reference_id,),
    ).fetchall()
    return [
        str(item["image_path"])
        for item in [*([row] if row else []), *views]
        if item["image_path"]
    ]


def purge_character_portrait(conn, portrait_id: str, *, commit: bool = True) -> dict[str, Any]:
    row = conn.execute(
        "SELECT project_id,character_name FROM character_portraits WHERE id=?",
        (portrait_id,),
    ).fetchone()
    if not row:
        return {"records": 0, "files": 0}
    paths = _portrait_paths(conn, portrait_id)
    conn.execute("DELETE FROM character_portrait_views WHERE portrait_id=?", (portrait_id,))
    conn.execute("DELETE FROM character_portraits WHERE id=?", (portrait_id,))
    _refresh_bible_reference(conn, row["project_id"], "characters", row["character_name"])
    if commit:
        conn.commit()
    return {"records": 1, "files": sum(discard_file(path) for path in set(paths))}


def purge_scene_reference(conn, scene_reference_id: str, *, commit: bool = True) -> dict[str, Any]:
    row = conn.execute(
        "SELECT project_id,scene_name FROM scene_references WHERE id=?",
        (scene_reference_id,),
    ).fetchone()
    if not row:
        return {"records": 0, "files": 0}
    paths = _scene_paths(conn, scene_reference_id)
    conn.execute(
        "DELETE FROM scene_reference_views WHERE scene_reference_id=?",
        (scene_reference_id,),
    )
    conn.execute("DELETE FROM scene_references WHERE id=?", (scene_reference_id,))
    _refresh_bible_reference(conn, row["project_id"], "scenes", row["scene_name"])
    if commit:
        conn.commit()
    return {"records": 1, "files": sum(discard_file(path) for path in set(paths))}


def _refresh_bible_reference(
    conn, project_id: str, collection: str, name: str,
) -> None:
    project = conn.execute(
        "SELECT bible_json FROM projects WHERE id=?", (project_id,),
    ).fetchone()
    if not project or not project["bible_json"]:
        return
    bible = _json(project["bible_json"], {})
    if collection == "characters":
        replacement = conn.execute(
            "SELECT image_path FROM character_portraits "
            "WHERE project_id=? AND character_name=? AND ep_end IS NULL AND pack_status='ready' "
            "ORDER BY ep_start DESC,created_at DESC LIMIT 1",
            (project_id, name),
        ).fetchone()
    else:
        replacement = conn.execute(
            "SELECT image_path FROM scene_references "
            "WHERE project_id=? AND scene_name=? AND ep_end IS NULL AND pack_status='ready' "
            "ORDER BY ep_start DESC,created_at DESC LIMIT 1",
            (project_id, name),
        ).fetchone()
    for item in bible.get(collection, []):
        if item.get("name") == name:
            item["ref_image_path"] = replacement["image_path"] if replacement else None
            break
    conn.execute(
        "UPDATE projects SET bible_json=? WHERE id=?",
        (json.dumps(bible, ensure_ascii=False), project_id),
    )


def _purge_artifact(conn, artifact: Any) -> tuple[int, int]:
    from app.evidence.repository import protected_release_lineage_ids

    if str(artifact["id"]) in protected_release_lineage_ids(conn=conn):
        raise ValueError(
            "Artifact 已被发布指针、production revision 或完成凭证引用，"
            "禁止物理清理"
        )
    paths = {str(artifact["file_path"])} if artifact["file_path"] else set()
    portrait_rows = conn.execute(
        "SELECT id FROM character_portraits WHERE artifact_id=?", (artifact["id"],),
    ).fetchall()
    portrait_rows += conn.execute(
        "SELECT DISTINCT portrait_id AS id FROM character_portrait_views WHERE artifact_id=?",
        (artifact["id"],),
    ).fetchall()
    scene_rows = conn.execute(
        "SELECT id FROM scene_references WHERE artifact_id=?", (artifact["id"],),
    ).fetchall()
    scene_rows += conn.execute(
        "SELECT DISTINCT scene_reference_id AS id FROM scene_reference_views WHERE artifact_id=?",
        (artifact["id"],),
    ).fetchall()
    records = 0
    for row in {item["id"]: item for item in portrait_rows}.values():
        paths.update(_portrait_paths(conn, row["id"]))
        records += purge_character_portrait(conn, row["id"], commit=False)["records"]
    for row in {item["id"]: item for item in scene_rows}.values():
        paths.update(_scene_paths(conn, row["id"]))
        records += purge_scene_reference(conn, row["id"], commit=False)["records"]
    conn.execute("DELETE FROM gate_decisions WHERE artifact_id=?", (artifact["id"],))
    conn.execute("DELETE FROM evaluations WHERE artifact_id=?", (artifact["id"],))
    conn.execute(
        "UPDATE artifacts SET superseded_by_artifact_id=NULL WHERE superseded_by_artifact_id=?",
        (artifact["id"],),
    )
    conn.execute("DELETE FROM artifacts WHERE id=?", (artifact["id"],))
    return records + 1, sum(discard_file(path) for path in paths)


def _scrub_shot_galleries(conn) -> tuple[int, int, set[str]]:
    removed = 0
    files = 0
    rejected_paths: set[str] = set()
    rows = conn.execute(
        "SELECT id,image_inputs FROM shot_versions WHERE image_inputs IS NOT NULL",
    ).fetchall()
    for row in rows:
        meta = _json(row["image_inputs"], {})
        if not isinstance(meta, dict):
            continue
        refs = list(meta.get("reference_images") or [])
        kept = []
        changed = False
        for ref in refs:
            if reference_dict_is_rejected(ref):
                path = str((ref or {}).get("path") or (ref or {}).get("image_path") or "")
                if path:
                    rejected_paths.add(path)
                removed += 1
                changed = True
            else:
                kept.append(ref)
        if changed:
            meta["reference_images"] = kept
        for slot in (meta.get("reference_slots") or {}).values():
            if not isinstance(slot, dict):
                continue
            candidates = list(slot.get("candidates") or [])
            clean_candidates = [
                candidate for candidate in candidates
                if str((candidate or {}).get("status") or "") not in REJECTED_CANDIDATE_STATUSES
            ]
            for candidate in candidates:
                if candidate not in clean_candidates:
                    path = str((candidate or {}).get("path") or "")
                    if path:
                        rejected_paths.add(path)
                    removed += 1
                    changed = True
            slot["candidates"] = clean_candidates
        if changed:
            conn.execute(
                "UPDATE shot_versions SET image_inputs=? WHERE id=?",
                (json.dumps(meta, ensure_ascii=False), row["id"]),
            )
    if rejected_paths:
        placeholders = ",".join("?" for _ in rejected_paths)
        conn.execute(
            f"DELETE FROM reference_assets WHERE path IN ({placeholders})",
            tuple(rejected_paths),
        )
    try:
        stale_assets = conn.execute(
            "SELECT id,path FROM reference_assets WHERE deleted=1 "
            "OR qa_status='technical_failed' "
            "OR generation_status IN ('technical_failed','discarded')",
        ).fetchall()
    except Exception:  # noqa: BLE001 - legacy databases may not have lifecycle columns
        stale_assets = conn.execute(
            "SELECT id,path FROM reference_assets WHERE deleted=1",
        ).fetchall()
    for asset in stale_assets:
        if asset["path"]:
            rejected_paths.add(str(asset["path"]))
        conn.execute("DELETE FROM reference_assets WHERE id=?", (asset["id"],))
        removed += 1
    conn.execute(
        "DELETE FROM reference_sets WHERE NOT EXISTS("
        "SELECT 1 FROM reference_assets WHERE reference_set_id=reference_sets.id)",
    )
    for path in rejected_paths:
        files += int(discard_file(path))
    return removed, files, rejected_paths


def purge_rejected_media(conn=None) -> dict[str, int]:
    """Purge historical QA rejects. Safe to run repeatedly during startup recovery."""
    db = conn or get_conn()
    gallery_records, gallery_files, _ = _scrub_shot_galleries(db)
    artifact_rows = db.execute(
        "SELECT * FROM artifacts WHERE scope_type='reference_asset' "
        "AND type IN ('character_portrait','scene_reference')",
    ).fetchall()
    records = gallery_records
    files = gallery_files
    artifacts = 0
    for artifact in artifact_rows:
        if not _artifact_is_rejected(db, artifact):
            continue
        purged_records, purged_files = _purge_artifact(db, artifact)
        records += purged_records
        files += purged_files
        artifacts += 1
    db.commit()
    return {
        "artifacts": artifacts,
        "records": records,
        "files": files,
    }
