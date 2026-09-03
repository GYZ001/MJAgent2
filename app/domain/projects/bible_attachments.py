"""把 ``character_portraits`` / ``scene_references`` 表的分段定妆照/场景图挂到
项目详情投影的 ``bible.characters`` / ``bible.scenes`` 上，供 :mod:`app.domain.
projects.detail` 的 ``project_detail`` 使用。"""
from __future__ import annotations

import json

from app.db import rows_to_dicts
from app.domain.bible_ops.portrait_status import attach_portrait_projection
from app.domain.common import _media_url
from app.evidence import repository as evidence_repository


def _attach_character_portraits(
    conn, project_id: str, bible: dict, bible_auto_changes_json: str | None = None,
) -> None:
    """为 bible.characters 挂上 character_portraits 表里的分段定妆照（含多视角），
    并投出 portrait_status/portrait_reason（WS13：见 bible_ops.portrait_status
    模块 docstring——「未出图」角标缺理由，用户误以为出图失败反复重试）。

    ``bible_auto_changes_json`` 由调用方传入（``projects`` 表原始列，调用方
    ``project_detail`` 已经整行取过，这里不重新查库）；未传时按"没有队列数据"
    处理，portrait_status 只会落在 ready/missing 两态，不影响既有调用方
    （历史测试/脚本仍可只传 3 个位置参数）。"""
    from app.portraits import STAGED_INITIAL_EP_START

    try:
        rows = rows_to_dicts(conn.execute(
            "SELECT id, character_name, ep_start, ep_end, appearance, base_portrait_id, image_path, "
            "pack_status, group_qa_json, change_json "
            "FROM character_portraits WHERE project_id=? AND ep_start<>? ORDER BY character_name, ep_start",
            (project_id, STAGED_INITIAL_EP_START)).fetchall())
    except Exception:  # noqa: BLE001
        rows = rows_to_dicts(conn.execute(
            "SELECT id, character_name, ep_start, ep_end, appearance, base_portrait_id, image_path "
            "FROM character_portraits WHERE project_id=? AND ep_start<>? ORDER BY character_name, ep_start",
            (project_id, STAGED_INITIAL_EP_START)).fetchall())
    view_rows = []
    try:
        view_rows = rows_to_dicts(conn.execute(
            """SELECT v.* FROM character_portrait_views v
               JOIN character_portraits p ON p.id=v.portrait_id
               WHERE p.project_id=? AND p.ep_start<>?
               ORDER BY v.portrait_id, v.view_role, v.selected DESC,
                        (v.status='ready') DESC, v.created_at DESC""",
            (project_id, STAGED_INITIAL_EP_START),
        ).fetchall())
    except Exception:  # noqa: BLE001
        view_rows = []
    views_by_portrait: dict[str, list[dict]] = {}
    seen_view_roles: set[tuple[str, str]] = set()
    for v in view_rows:
        view_key = (str(v["portrait_id"]), str(v.get("view_role") or ""))
        if view_key in seen_view_roles:
            continue
        seen_view_roles.add(view_key)
        qa = None
        if v.get("qa_json"):
            try:
                qa = json.loads(v["qa_json"])
            except (TypeError, ValueError):
                qa = None
        views_by_portrait.setdefault(v["portrait_id"], []).append({
            "id": v["id"],
            "view_role": v.get("view_role"),
            "framing": v.get("framing"),
            "status": v.get("status"),
            "image_url": _media_url(v.get("image_path")),
            "qa": qa,
            "qa_overall": (qa or {}).get("overall") if isinstance(qa, dict) else None,
        })
    by_name: dict[str, list[dict]] = {}
    for r in rows:
        group_qa = None
        if r.get("group_qa_json"):
            try:
                group_qa = json.loads(r["group_qa_json"])
            except (TypeError, ValueError):
                group_qa = None
        change = None
        if r.get("change_json"):
            try:
                change = json.loads(r["change_json"])
            except (TypeError, ValueError):
                change = None
        by_name.setdefault(r["character_name"], []).append({
            "id": r["id"], "ep_start": r["ep_start"], "ep_end": r["ep_end"],
            "appearance": r["appearance"], "base_portrait_id": r["base_portrait_id"],
            "image_url": _media_url(r["image_path"]),
            "pack_status": r.get("pack_status"),
            "group_qa": group_qa,
            "change": change,
            "views": views_by_portrait.get(r["id"], []),
        })
    for c in bible.get("characters", []):
        portraits = by_name.get(c.get("name"), [])
        c["portraits"] = portraits
        # ``bible_json.characters[].ref_image_path`` is a compatibility cache,
        # not the source of truth for versioned portraits. A ready pack is
        # committed to ``character_portraits`` before a long batch finishes, so
        # expose that checkpoint immediately instead of leaving the UI gated on
        # a later Bible merge (or process restart).
        if not c.get("ref_image_url"):
            latest_ready = next((
                portrait for portrait in reversed(portraits)
                if portrait.get("pack_status") in (None, "ready")
                and portrait.get("image_url")
            ), None)
            if latest_ready:
                c["ref_image_url"] = latest_ready["image_url"]
    attach_portrait_projection(bible, bible_auto_changes_json)


def _attach_scene_refs(conn, project_id: str, bible: dict) -> None:
    """为 bible.scenes 挂上 scene_references 表里的分段场景图（含多视角与 QA）。"""
    try:
        rows = rows_to_dicts(conn.execute(
            "SELECT id, scene_name, ep_start, ep_end, scene_canonical, image_path, qa_json, artifact_id, "
            "pack_status, group_qa_json, change_json "
            "FROM scene_references WHERE project_id=? ORDER BY scene_name, ep_start", (project_id,)).fetchall())
    except Exception:  # noqa: BLE001 旧库缺列
        rows = rows_to_dicts(conn.execute(
            "SELECT scene_name, ep_start, ep_end, scene_canonical, image_path, qa_json, artifact_id "
            "FROM scene_references WHERE project_id=? ORDER BY scene_name, ep_start", (project_id,)).fetchall())
    view_rows = []
    try:
        view_rows = rows_to_dicts(conn.execute(
            """SELECT v.* FROM scene_reference_views v
               JOIN scene_references s ON s.id=v.scene_reference_id
               WHERE s.project_id=? ORDER BY v.scene_reference_id, v.created_at""",
            (project_id,),
        ).fetchall())
    except Exception:  # noqa: BLE001
        view_rows = []
    views_by_scene: dict[str, list[dict]] = {}
    for v in view_rows:
        qa = None
        if v.get("qa_json"):
            try:
                qa = json.loads(v["qa_json"])
            except (TypeError, ValueError):
                qa = None
        views_by_scene.setdefault(v["scene_reference_id"], []).append({
            "id": v["id"],
            "view_role": v.get("view_role"),
            "camera_axis": v.get("camera_axis"),
            "status": v.get("status"),
            "image_url": _media_url(v.get("image_path")),
            "qa": qa,
            "qa_overall": (qa or {}).get("overall") if isinstance(qa, dict) else None,
        })
    try:
        reference_rows = rows_to_dicts(conn.execute(
            "SELECT s.scene_name,e.id AS episode_id,e.episode_no,COUNT(*) AS shot_count FROM shots s "
            "JOIN episodes e ON e.id=s.episode_id WHERE e.project_id=? AND s.scene_name IS NOT NULL "
            "AND s.scene_name!='' GROUP BY s.scene_name,e.episode_no ORDER BY e.episode_no",
            (project_id,),
        ).fetchall())
    except Exception:  # noqa: BLE001 兼容精简测试库/历史库
        reference_rows = []
    references_by_name: dict[str, list[dict]] = {}
    for item in reference_rows:
        references_by_name.setdefault(item["scene_name"], []).append(item)
    by_name: dict[str, list[dict]] = {}
    for r in rows:
        qa = None
        if r.get("qa_json"):
            try:
                qa = json.loads(r["qa_json"])
            except (TypeError, ValueError):
                qa = None
        evidence = evidence_repository.get_artifact(r["artifact_id"]) if r.get("artifact_id") else None
        if evidence:
            evidence["evaluations"] = evidence_repository.get_evaluations(evidence["id"])
        group_qa = None
        if r.get("group_qa_json"):
            try:
                group_qa = json.loads(r["group_qa_json"])
            except (TypeError, ValueError):
                group_qa = None
        change = None
        if r.get("change_json"):
            try:
                change = json.loads(r["change_json"])
            except (TypeError, ValueError):
                change = None
        segment_references = [
            item for item in references_by_name.get(r["scene_name"], [])
            if int(item["episode_no"]) >= int(r["ep_start"] or 1)
            and (r["ep_end"] is None or int(item["episode_no"]) <= int(r["ep_end"]))
        ]
        by_name.setdefault(r["scene_name"], []).append({
            "id": r.get("id"),
            "ep_start": r["ep_start"], "ep_end": r["ep_end"],
            "scene_canonical": r["scene_canonical"], "image_url": _media_url(r["image_path"]),
            "qa": qa, "qa_overall": (qa or {}).get("overall") if isinstance(qa, dict) else None,
            "artifact_id": r.get("artifact_id"), "evidence": evidence,
            "pack_status": r.get("pack_status"),
            "group_qa": group_qa,
            "change": change,
            "reference_summary": {
                "episode_numbers": [int(item["episode_no"]) for item in segment_references],
                "episodes": [{"id": item["episode_id"], "episode_no": int(item["episode_no"])}
                             for item in segment_references],
                "shot_count": sum(int(item["shot_count"] or 0) for item in segment_references),
            },
            "views": views_by_scene.get(r.get("id") or "", []),
        })
    # 候选图能力已退场（用户拍板 2026-09-01，见 app/scenes.py 的墓碑注释）：这里
    # 曾把该项目全部 scene_reference 产物连同它们的评估记录一并下发给场景库供人工
    # 挑选采纳。现在场景卡只需要"当前这一版"，历史版本仍可在场景版本里回滚。
    for s in bible.get("scenes", []):
        segs = by_name.get(s.get("name"), [])
        s["scene_refs"] = segs
        if not s.get("ref_image_url"):
            latest = next((seg for seg in reversed(segs) if seg.get("image_url")), None)
            if latest:
                s["ref_image_url"] = latest["image_url"]
