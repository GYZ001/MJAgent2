"""Authoritative cleanup and invalidation of generated media artifacts."""
from __future__ import annotations

import asyncio
import json
import shutil
import threading
from pathlib import Path

from app import config
from app.atomic_io import atomic_write_text
from app.db import get_conn, new_id, now, rows_to_dicts

_CLEAR_TERMINAL_RUN_STATES = {
    "SUCCEEDED", "FAILED", "CANCELLED", "COMPLETED", "PARTIAL",
    "succeeded", "failed", "cancelled", "completed", "partial",
}
_MEDIA_CLEANUP_SWEEP_INTERVAL_SECONDS = 30.0
_MEDIA_CLEANUP_SWEEP_LIMIT = 25
_MEDIA_CLEANUP_FLUSH_LOCK = threading.Lock()


def _begin_clear_transaction(
    conn,
    episode_id: str,
    *,
    active_storyboard_run_id: str | None = None,
    allow_storyboard_workspace_mutation: bool = False,
) -> None:
    """Serialize the final upstream check and media purge in SQLite."""
    # Supervisor 局部修复可能在同一连接已有事务（例如先写修复计划再清理相邻镜）。
    # 复用该事务，避免 ``cannot start a transaction within a transaction``；独立的
    # 生成台清空仍以 BEGIN IMMEDIATE 获取写锁。
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    ep = conn.execute(
        "SELECT status, active_screenplay_run_id, active_storyboard_run_id, active_video_run_id "
        "FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    if not ep:
        conn.rollback()
        raise ValueError("分集不存在")
    authorized_storyboard_run = False
    if active_storyboard_run_id and ep["active_storyboard_run_id"] == active_storyboard_run_id:
        allowed = conn.execute(
            "SELECT workflow_type,scope_type,scope_id,status FROM workflow_runs WHERE id=?",
            (active_storyboard_run_id,),
        ).fetchone()
        authorized_storyboard_run = bool(
            allowed
            and allowed["workflow_type"] == "storyboard"
            and allowed["scope_type"] == "episode"
            and allowed["scope_id"] == episode_id
            and allowed["status"] not in _CLEAR_TERMINAL_RUN_STATES
        )
    active: list[str] = []
    for run_id in (
        ep["active_screenplay_run_id"],
        ep["active_storyboard_run_id"],
        ep["active_video_run_id"],
    ):
        if not run_id:
            continue
        if authorized_storyboard_run and run_id == active_storyboard_run_id:
            continue
        run = conn.execute("SELECT status FROM workflow_runs WHERE id=?", (run_id,)).fetchone()
        if not run or run["status"] not in _CLEAR_TERMINAL_RUN_STATES:
            active.append(str(run_id))
    writing_status = ep["status"] in {"planned", "scripting", "storyboarding", "generating"}
    if (
        writing_status
        and not authorized_storyboard_run
        and not allow_storyboard_workspace_mutation
    ) or active:
        conn.rollback()
        raise ValueError("编剧或分镜任务仍在写入，清空已原子拒绝")

def _delete_version_files(video_path: str | None) -> None:
    """删除版本视频及旧链路遗留的缓存尾帧。"""
    if not video_path:
        return
    p = Path(video_path)
    for f in (p, Path(str(p.with_suffix("")) + "_last.jpg")):
        try:
            f.unlink()
        except OSError:
            pass


def _delete_shot_boundary_assets(conn, shot_id: str) -> int:
    """Delete durable first/last-frame records and their local files."""
    rows = conn.execute(
        "SELECT path FROM video_boundary_assets WHERE shot_id=?",
        (shot_id,),
    ).fetchall()
    for row in rows:
        path = str(row["path"] or "").strip()
        if not path:
            continue
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass
    conn.execute("DELETE FROM video_boundary_assets WHERE shot_id=?", (shot_id,))
    return len(rows)


def _purge_shots(
    conn,
    shots: list[dict],
    *,
    preserve_video_audit: bool = False,
) -> tuple[int, set[str]]:
    """删除给定镜头的全部版本、关键帧、任务与采用标记。
    返回 (删除版本数, 受影响剧集 id 集合)。"""
    versions_removed = 0
    affected_eps: set[str] = set()
    for s in shots:
        affected_eps.add(s["episode_id"])
        _delete_shot_boundary_assets(conn, s["id"])
        versions = conn.execute(
            "SELECT id, video_path FROM shot_versions WHERE shot_id=?", (s["id"],)).fetchall()
        for v in versions:
            _delete_version_files(v["video_path"])
        scenes = conn.execute("SELECT image_path FROM shot_scenes WHERE shot_id=?", (s["id"],)).fetchall()
        for sc in scenes:
            if sc["image_path"]:
                try:
                    Path(sc["image_path"]).unlink()
                except OSError:
                    pass
        if preserve_video_audit:
            conn.execute(
                """UPDATE shot_versions
                      SET status='cleared',video_path=NULL,
                          error='用户已清空本集生成资源'
                    WHERE shot_id=?""",
                (s["id"],),
            )
        else:
            conn.execute("DELETE FROM shot_versions WHERE shot_id=?", (s["id"],))
        conn.execute("DELETE FROM shot_scenes WHERE shot_id=?", (s["id"],))
        conn.execute("DELETE FROM jobs WHERE shot_id=?", (s["id"],))
        conn.execute(
            "UPDATE shots SET adopted_version_id=NULL, approved_scene_id=NULL, "
            "approved_head_scene_id=NULL, approved_tail_scene_id=NULL, scene_status='none' WHERE id=?",
            (s["id"],))
        versions_removed += len(versions)
    return versions_removed, affected_eps


def _rollback_episodes(conn, ep_ids: set[str]) -> None:
    for ep_id in ep_ids:
        ep = conn.execute(
            "SELECT project_id, episode_no FROM episodes WHERE id=?", (ep_id,)).fetchone()
        if ep:
            _invalidate_final_video(ep["project_id"], ep["episode_no"])
        conn.execute("UPDATE episodes SET status='confirmed' WHERE id=? AND status IN ('generating','done')", (ep_id,))


def purge_character_video_artifacts(project_id: str, character_names: list[str]) -> dict:
    """角色定妆照重做后，清理所有用到该角色的镜头已生成产物：
    生成台关键帧、各版本视频和相关任务，标记整集成品待更新，并把对应剧集回退到“已确认”，
    强制后续基于新定妆照重新生成，避免新旧画风/形象混用。"""
    targets = {n for n in character_names if n}
    if not targets:
        return {"shots": 0, "versions": 0, "episodes": 0}
    conn = get_conn()
    shots = rows_to_dicts(conn.execute(
        """SELECT s.id, s.episode_id, s.characters
           FROM shots s JOIN episodes e ON e.id = s.episode_id
           WHERE e.project_id = ?""", (project_id,)).fetchall())
    affected_shots = [s for s in shots if set(json.loads(s["characters"] or "[]")) & targets]
    versions_removed, affected_eps = _purge_shots(conn, affected_shots)
    _rollback_episodes(conn, affected_eps)
    conn.commit()
    return {"shots": len(affected_shots), "versions": versions_removed, "episodes": len(affected_eps)}


def delete_project_episodes(project_id: str) -> int:
    """重新分集时整体清空本项目所有剧集及其衍生数据（镜头/版本/视频/任务/成片目录）。
    旧逻辑只删 status='planned' 的剧集，导致已进入分镜/确认的旧集残留、与新集 episode_no 撞号，
    前端就出现“同一集号有两三条、剧情重复”。重新分集应是干净替换。"""
    conn = get_conn()
    eps = conn.execute("SELECT id, episode_no FROM episodes WHERE project_id=?", (project_id,)).fetchall()
    shots = rows_to_dicts(conn.execute(
        "SELECT s.id, s.episode_id FROM shots s JOIN episodes e ON e.id=s.episode_id WHERE e.project_id=?",
        (project_id,)).fetchall())
    _purge_shots(conn, shots)  # 删版本文件/尾帧、jobs、清采用标记
    conn.execute("DELETE FROM shots WHERE episode_id IN (SELECT id FROM episodes WHERE project_id=?)", (project_id,))
    conn.execute("DELETE FROM episodes WHERE project_id=?", (project_id,))
    conn.commit()
    ep_dir = config.PROJECTS_DIR / project_id / "episodes"
    if ep_dir.exists():
        shutil.rmtree(ep_dir, ignore_errors=True)
    return len(eps)


def delete_episode_shots(
    episode_id: str,
    *,
    conn=None,
    commit: bool = True,
) -> int:
    """清空单集分镜及其衍生产物。用于剧本重生/编辑后让下游重新展开。"""
    conn = conn or get_conn()
    ep = conn.execute("SELECT project_id, episode_no FROM episodes WHERE id=?", (episode_id,)).fetchone()
    shots = rows_to_dicts(conn.execute(
        "SELECT id, episode_id, shot_no FROM shots WHERE episode_id=?", (episode_id,)).fetchall())
    for shot in shots:
        _delete_shot_reference_dir(conn, shot)
        _delete_shot_reference_records(conn, shot["id"])
    _purge_shots(conn, shots)
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='shot_audio'"
    ).fetchone():
        conn.execute("DELETE FROM shot_audio WHERE episode_id=?", (episode_id,))
    conn.execute("DELETE FROM shots WHERE episode_id=?", (episode_id,))
    conn.execute("DELETE FROM jobs WHERE episode_id=?", (episode_id,))
    conn.execute(
        "UPDATE episodes SET status='planned', script_error=NULL WHERE id=? AND status NOT IN ('planned','scripting','script_failed')",
        (episode_id,))
    if commit:
        conn.commit()
    if ep:
        _invalidate_final_video(ep["project_id"], ep["episode_no"])
    return len(shots)


def purge_project_video_artifacts(project_id: str) -> dict:
    """画风切换后全项目作废：旧画风的定妆照、旧关键帧与旧视频
    是比文字 prompt 更强的画风信号，残留任何一环都会把新画风拉回旧画风，必须整体清理。"""
    conn = get_conn()
    shots = rows_to_dicts(conn.execute(
        """SELECT s.id, s.episode_id FROM shots s JOIN episodes e ON e.id = s.episode_id
           WHERE e.project_id = ?""", (project_id,)).fetchall())
    versions_removed, affected_eps = _purge_shots(conn, shots)
    _rollback_episodes(conn, affected_eps)
    conn.commit()
    return {"shots": len(shots), "versions": versions_removed, "episodes": len(affected_eps)}


def delete_video_version(version_id: str) -> str | None:
    """删除单个视频版本（含视频/尾帧文件、相关任务）；若是采用版则清空采用并标记该集成品待更新。
    返回所属 shot_id。"""
    conn = get_conn()
    v = conn.execute("SELECT * FROM shot_versions WHERE id=?", (version_id,)).fetchone()
    if not v:
        return None
    shot_id = v["shot_id"]
    _delete_version_files(v["video_path"])
    conn.execute("DELETE FROM shot_versions WHERE id=?", (version_id,))
    conn.execute("DELETE FROM jobs WHERE version_id=?", (version_id,))
    conn.execute("UPDATE shots SET adopted_version_id=NULL WHERE id=? AND adopted_version_id=?", (shot_id, version_id))
    shot = conn.execute("SELECT episode_id FROM shots WHERE id=?", (shot_id,)).fetchone()
    if shot:
        ep = conn.execute("SELECT project_id, episode_no FROM episodes WHERE id=?", (shot["episode_id"],)).fetchone()
        if ep:
            _invalidate_final_video(ep["project_id"], ep["episode_no"])
    conn.commit()
    return shot_id


def purge_shot_videos(shot_id: str) -> int:
    """删除某镜的全部视频版本（含视频/尾帧文件、相关任务），清空采用标记，并标记该集成品待更新。
    用于：该镜关键帧被删空后，旧成片已无首尾帧依据，但仍作为历史成品保留。返回删除的版本数。"""
    conn = get_conn()
    shot = conn.execute("SELECT episode_id FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot:
        return 0
    versions = conn.execute("SELECT id, video_path FROM shot_versions WHERE shot_id=?", (shot_id,)).fetchall()
    for v in versions:
        _delete_version_files(v["video_path"])
    conn.execute("DELETE FROM shot_versions WHERE shot_id=?", (shot_id,))
    conn.execute("DELETE FROM jobs WHERE shot_id=? AND kind='video'", (shot_id,))
    conn.execute("UPDATE shots SET adopted_version_id=NULL WHERE id=?", (shot_id,))
    ep = conn.execute("SELECT project_id, episode_no FROM episodes WHERE id=?", (shot["episode_id"],)).fetchone()
    if ep:
        _invalidate_final_video(ep["project_id"], ep["episode_no"])
    conn.commit()
    return len(versions)


def _reference_meta(image_inputs: str | None) -> tuple[dict, bool]:
    try:
        meta = json.loads(image_inputs or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, bool(meta.get("reference_images"))


def _clear_shot_video_assets(conn, shot_row) -> int:
    """Delete video assets while retaining one reference-gallery carrier row.

    Reference images historically live in ``shot_versions.image_inputs``.  A
    video-only clear must therefore keep one metadata-only version, otherwise
    the separate reference-image tab would silently lose its assets too.
    """
    versions = conn.execute(
        "SELECT * FROM shot_versions WHERE shot_id=? ORDER BY version_no DESC",
        (shot_row["id"],),
    ).fetchall()
    keeper = None
    for version in versions:
        _, has_references = _reference_meta(version["image_inputs"])
        if has_references:
            keeper = version
            break
    for version in versions:
        _delete_version_files(version["video_path"])
        if keeper is not None and version["id"] == keeper["id"]:
            conn.execute(
                """UPDATE shot_versions
                   SET provider_task_id=NULL, status='references_ready', error=NULL,
                       video_path=NULL, last_frame_url=NULL, qa_json=NULL,
                       technical_validation_json=NULL, adoption_reason=NULL,
                       cost_cny=0, latency_s=0
                   WHERE id=?""",
                (version["id"],),
            )
        else:
            conn.execute("DELETE FROM shot_versions WHERE id=?", (version["id"],))
    conn.execute("DELETE FROM jobs WHERE shot_id=? AND kind='video'", (shot_row["id"],))
    conn.execute("UPDATE shots SET adopted_version_id=NULL WHERE id=?", (shot_row["id"],))
    if keeper is not None:
        conn.execute(
            "UPDATE reference_sets SET source_version_id=? WHERE shot_id=?",
            (keeper["id"], shot_row["id"]),
        )
    return len(versions)


def clear_shot_video_assets(shot_id: str, *, commit: bool = True) -> dict:
    """Clear only one shot's created video assets; keep its reference gallery."""
    conn = get_conn()
    shot = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot:
        return {"shot_id": shot_id, "videos": 0, "references_preserved": True}
    _begin_clear_transaction(conn, shot["episode_id"])
    try:
        videos = _clear_shot_video_assets(conn, shot)
        _rollback_episodes(conn, {shot["episode_id"]})
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"shot_id": shot_id, "videos": videos, "references_preserved": True}


def clear_episode_video_assets(episode_id: str) -> dict:
    """Clear every shot video in an episode without deleting reference images."""
    conn = get_conn()
    _begin_clear_transaction(conn, episode_id)
    try:
        shots = conn.execute(
            "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)
        ).fetchall()
        videos = sum(_clear_shot_video_assets(conn, shot) for shot in shots)
        _rollback_episodes(conn, {episode_id})
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "episode_id": episode_id,
        "shots": len(shots),
        "videos": videos,
        "references_preserved": True,
    }


_REFERENCE_META_KEYS = {
    "reference_images", "reference_failure_logs", "reference_manifest",
    "reference_gallery_revision", "reference_gallery_edited",
    "reference_gallery_contract_override", "reference_gallery_source_version_id",
    "reference_gallery_fingerprint", "keyframe_contract_fingerprint",
    "keyframe_sequence", "reference_image_used", "first_frame_used",
    "first_frame_src", "first_frame_path", "first_frame_scene_id",
    "last_frame_used", "last_frame_src", "last_frame_path", "last_frame_scene_id",
}


def clear_shot_reference_assets(shot_id: str) -> dict:
    """Clear only generated shot reference/keyframe images; keep video assets."""
    conn = get_conn()
    shot = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot:
        return {"shot_id": shot_id, "references": 0, "videos_preserved": True}
    _begin_clear_transaction(conn, shot["episode_id"])
    try:
        references = _delete_shot_reference_dir(conn, shot)
        scenes = conn.execute(
            "SELECT image_path FROM shot_scenes WHERE shot_id=?", (shot_id,)
        ).fetchall()
        for scene in scenes:
            if scene["image_path"]:
                try:
                    Path(scene["image_path"]).unlink()
                except OSError:
                    pass
        references += sum(1 for scene in scenes if scene["image_path"])
        conn.execute("DELETE FROM shot_scenes WHERE shot_id=?", (shot_id,))
        conn.execute(
            "DELETE FROM reference_assets WHERE reference_set_id IN "
            "(SELECT id FROM reference_sets WHERE shot_id=?)",
            (shot_id,),
        )
        conn.execute("DELETE FROM reference_sets WHERE shot_id=?", (shot_id,))
        versions = conn.execute(
            "SELECT id, image_inputs FROM shot_versions WHERE shot_id=?", (shot_id,)
        ).fetchall()
        for version in versions:
            meta, _ = _reference_meta(version["image_inputs"])
            for key in _REFERENCE_META_KEYS:
                meta.pop(key, None)
            conn.execute(
                "UPDATE shot_versions SET image_inputs=? WHERE id=?",
                (json.dumps(meta, ensure_ascii=False), version["id"]),
            )
        # A metadata-only carrier has no purpose once its gallery is gone.
        conn.execute(
            "DELETE FROM shot_versions WHERE shot_id=? AND status='references_ready'",
            (shot_id,),
        )
        conn.execute(
            """UPDATE shots
               SET approved_scene_id=NULL, approved_head_scene_id=NULL,
                   approved_tail_scene_id=NULL, scene_status='none'
               WHERE id=?""",
            (shot_id,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"shot_id": shot_id, "references": references, "videos_preserved": True}


def invalidate_shot_video_derivatives(shot_id: str) -> dict:
    """关键帧/参考依据变化后，作废该镜的视频侧衍生产物，但保留关键帧候选。

    参考图画廊是从当前关键帧与人物/场景锚点派生的；仅删除视频而保留画廊会让
    下一次生成继续吃到旧依据，因此二者必须一起失效。
    """
    conn = get_conn()
    shot = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot:
        return {"shot_id": shot_id, "videos": 0, "references": 0}
    references = _delete_shot_reference_dir(conn, shot)
    videos = purge_shot_videos(shot_id)
    conn.execute("UPDATE shots SET mode_plan=NULL WHERE id=?", (shot_id,))
    conn.commit()
    return {"shot_id": shot_id, "videos": videos, "references": references}


def invalidate_episode_final(episode_id: str) -> bool:
    """采用版本变化后标记旧整集合成为待更新；旧成品本身仍保留。"""
    conn = get_conn()
    ep = conn.execute(
        "SELECT project_id, episode_no FROM episodes WHERE id=?", (episode_id,)
    ).fetchone()
    if not ep:
        return False
    _invalidate_final_video(ep["project_id"], ep["episode_no"])
    return True


def _delete_shot_reference_dir(conn, shot_row) -> int:
    """删除某镜的历史参考图缓存目录。返回删除的文件数。"""
    ep = conn.execute(
        "SELECT project_id, episode_no FROM episodes WHERE id=?", (shot_row["episode_id"],)).fetchone()
    if not ep:
        return 0
    ref_dir = (config.PROJECTS_DIR / ep["project_id"] / "episodes" / str(ep["episode_no"])
               / "shots" / str(shot_row["shot_no"]) / "references")
    if not ref_dir.exists():
        return 0
    count = sum(1 for p in ref_dir.glob("*") if p.is_file())
    shutil.rmtree(ref_dir, ignore_errors=True)
    return count


def _delete_shot_reference_records(conn, shot_id: str) -> None:
    """删除单镜参考图集合及其资产索引。

    参考图文件主要位于镜头 references 目录，但 reference_sets 还会持久化
    画廊指针。只删文件/版本会留下指向旧版本的孤儿记录，后续可能被误判为可复用资产。
    """
    conn.execute(
        "DELETE FROM reference_assets WHERE reference_set_id IN "
        "(SELECT id FROM reference_sets WHERE shot_id=?)",
        (shot_id,),
    )
    conn.execute("DELETE FROM reference_sets WHERE shot_id=?", (shot_id,))


def clear_shot_artifacts(
    shot_id: str,
    *,
    active_storyboard_run_id: str | None = None,
    commit: bool = True,
) -> dict:
    """清空单镜的参考图、关键帧（首/尾图）、视频版本与模型分析（mode_plan），并使该集成品失效。
    用于生成台的「清空」操作。"""
    conn = get_conn()
    shot = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot:
        return {"shot_id": shot_id, "videos": 0, "references": 0,
                "keyframes_cleared": False, "mode_plan_cleared": False}
    _begin_clear_transaction(
        conn,
        shot["episode_id"],
        active_storyboard_run_id=active_storyboard_run_id,
        allow_storyboard_workspace_mutation=True,
    )
    try:
        refs = _delete_shot_reference_dir(conn, shot)
        _delete_shot_reference_records(conn, shot_id)
        versions, affected_eps = _purge_shots(conn, [dict(shot)])  # 视频+关键帧+任务、采用/审批标记、scene_status 复位
        conn.execute("UPDATE shots SET mode_plan=NULL WHERE id=?", (shot_id,))
        _rollback_episodes(conn, affected_eps)  # 整集成品失效 + generating/done → confirmed
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"shot_id": shot_id, "videos": versions, "references": refs,
            "keyframes_cleared": True, "mode_plan_cleared": True}


def stage_shot_artifact_cleanup(
    conn,
    shot_id: str,
    *,
    active_storyboard_run_id: str | None = None,
) -> dict:
    """Invalidate one shot in the caller transaction and defer file deletion."""
    shot = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot:
        return {
            "shot_id": shot_id,
            "videos": 0,
            "references": 0,
            "outbox_id": None,
        }
    _begin_clear_transaction(
        conn,
        shot["episode_id"],
        active_storyboard_run_id=active_storyboard_run_id,
    )
    ep = conn.execute(
        "SELECT project_id,episode_no FROM episodes WHERE id=?",
        (shot["episode_id"],),
    ).fetchone()
    ref_dir = (
        config.PROJECTS_DIR / ep["project_id"] / "episodes" / str(ep["episode_no"])
        / "shots" / str(shot["shot_no"]) / "references"
        if ep else None
    )
    references = (
        sum(1 for path in ref_dir.glob("*") if path.is_file())
        if ref_dir and ref_dir.exists()
        else 0
    )
    versions = conn.execute(
        "SELECT id,video_path FROM shot_versions WHERE shot_id=?",
        (shot_id,),
    ).fetchall()
    files: list[str] = []
    for version in versions:
        if not version["video_path"]:
            continue
        video_path = Path(version["video_path"])
        files.extend([
            str(video_path),
            str(Path(str(video_path.with_suffix("")) + "_last.jpg")),
        ])
    files.extend(
        str(row["image_path"])
        for row in conn.execute(
            "SELECT image_path FROM shot_scenes WHERE shot_id=? AND image_path IS NOT NULL",
            (shot_id,),
        ).fetchall()
        if row["image_path"]
    )
    files.extend(
        str(row["path"])
        for row in conn.execute(
            "SELECT path FROM video_boundary_assets WHERE shot_id=? AND path IS NOT NULL",
            (shot_id,),
        ).fetchall()
        if row["path"]
    )
    _delete_shot_reference_records(conn, shot_id)
    conn.execute("DELETE FROM video_boundary_assets WHERE shot_id=?", (shot_id,))
    conn.execute("DELETE FROM shot_versions WHERE shot_id=?", (shot_id,))
    conn.execute("DELETE FROM shot_scenes WHERE shot_id=?", (shot_id,))
    conn.execute("DELETE FROM jobs WHERE shot_id=?", (shot_id,))
    conn.execute(
        """UPDATE shots
              SET adopted_version_id=NULL,approved_scene_id=NULL,
                  approved_head_scene_id=NULL,approved_tail_scene_id=NULL,
                  scene_status='none',mode_plan=NULL
            WHERE id=?""",
        (shot_id,),
    )
    outbox_id = new_id("cleanup")
    payload = {
        "files": list(dict.fromkeys(files)),
        "directories": [str(ref_dir)] if ref_dir else [],
        "invalidate_final": (
            {
                "project_id": ep["project_id"],
                "episode_no": int(ep["episode_no"]),
            }
            if ep else None
        ),
    }
    conn.execute(
        """INSERT INTO media_cleanup_outbox(
               id,episode_id,shot_id,payload_json,status,created_at
           ) VALUES(?,?,?,?,'pending',?)""",
        (
            outbox_id,
            shot["episode_id"],
            shot_id,
            json.dumps(payload, ensure_ascii=False),
            now(),
        ),
    )
    return {
        "shot_id": shot_id,
        "videos": len(versions),
        "references": references,
        "outbox_id": outbox_id,
    }


def stage_episode_artifact_cleanup(conn, episode_id: str) -> dict:
    """Invalidate an episode's media rows and persist file cleanup atomically."""
    if not conn.in_transaction:
        raise RuntimeError("整集媒体清理必须加入调用方事务")
    ep = conn.execute(
        "SELECT project_id,episode_no FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    if ep is None:
        raise ValueError("分集不存在")
    shots = rows_to_dicts(conn.execute(
        "SELECT id,shot_no FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall())
    files: list[str] = []
    directories: list[str] = []
    versions_removed = 0
    references_removed = 0
    for shot in shots:
        ref_dir = (
            config.PROJECTS_DIR / ep["project_id"] / "episodes"
            / str(ep["episode_no"]) / "shots" / str(shot["shot_no"])
            / "references"
        )
        directories.append(str(ref_dir))
        if ref_dir.exists():
            references_removed += sum(
                1 for path in ref_dir.glob("*") if path.is_file()
            )
        versions = conn.execute(
            "SELECT video_path FROM shot_versions WHERE shot_id=?",
            (shot["id"],),
        ).fetchall()
        versions_removed += len(versions)
        for version in versions:
            if not version["video_path"]:
                continue
            video_path = Path(version["video_path"])
            files.extend([
                str(video_path),
                str(Path(str(video_path.with_suffix("")) + "_last.jpg")),
            ])
        files.extend(
            str(row["image_path"])
            for row in conn.execute(
                "SELECT image_path FROM shot_scenes "
                "WHERE shot_id=? AND image_path IS NOT NULL",
                (shot["id"],),
            ).fetchall()
            if row["image_path"]
        )
        files.extend(
            str(row["path"])
            for row in conn.execute(
                "SELECT path FROM video_boundary_assets "
                "WHERE shot_id=? AND path IS NOT NULL",
                (shot["id"],),
            ).fetchall()
            if row["path"]
        )
        _delete_shot_reference_records(conn, str(shot["id"]))

    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='shot_audio'"
    ).fetchone():
        conn.execute("DELETE FROM shot_audio WHERE episode_id=?", (episode_id,))
    conn.execute("DELETE FROM shots WHERE episode_id=?", (episode_id,))
    conn.execute("DELETE FROM jobs WHERE episode_id=?", (episode_id,))

    outbox_id = new_id("cleanup")
    payload = {
        "files": list(dict.fromkeys(files)),
        "directories": list(dict.fromkeys(directories)),
        "invalidate_final": {
            "project_id": ep["project_id"],
            "episode_no": int(ep["episode_no"]),
        },
    }
    conn.execute(
        """INSERT INTO media_cleanup_outbox(
               id,episode_id,shot_id,payload_json,status,created_at
           ) VALUES(?,?,NULL,?,'pending',?)""",
        (
            outbox_id,
            episode_id,
            json.dumps(payload, ensure_ascii=False),
            now(),
        ),
    )
    return {
        "episode_id": episode_id,
        "shots": len(shots),
        "videos": versions_removed,
        "references": references_removed,
        "outbox_id": outbox_id,
    }


def flush_media_cleanup_outbox(outbox_id: str) -> bool:
    """Delete files recorded by a committed cleanup transaction."""
    with _MEDIA_CLEANUP_FLUSH_LOCK:
        conn = get_conn()
        row = conn.execute(
            "SELECT * FROM media_cleanup_outbox WHERE id=?",
            (outbox_id,),
        ).fetchone()
        if not row or row["status"] == "completed":
            return bool(row)
        try:
            payload = json.loads(row["payload_json"] or "{}")
            for raw_path in payload.get("files") or []:
                Path(str(raw_path)).unlink(missing_ok=True)
            for raw_path in payload.get("directories") or []:
                try:
                    shutil.rmtree(Path(str(raw_path)))
                except FileNotFoundError:
                    pass
            final = payload.get("invalidate_final") or {}
            if final.get("project_id") and final.get("episode_no") is not None:
                _invalidate_final_video(
                    str(final["project_id"]),
                    int(final["episode_no"]),
                    suppress_errors=False,
                    not_newer_than=float(row["created_at"]),
                )
            conn.execute(
                """UPDATE media_cleanup_outbox
                      SET status='completed',attempts=attempts+1,last_error=NULL,completed_at=?
                    WHERE id=?""",
                (now(), outbox_id),
            )
            conn.commit()
            return True
        except Exception as exc:
            conn.execute(
                """UPDATE media_cleanup_outbox
                      SET status='pending',attempts=attempts+1,last_error=?
                    WHERE id=?""",
                (str(exc)[:800], outbox_id),
            )
            conn.commit()
            return False


def sweep_pending_media_cleanup(limit: int = _MEDIA_CLEANUP_SWEEP_LIMIT) -> dict[str, int]:
    rows = get_conn().execute(
        """SELECT id FROM media_cleanup_outbox
            WHERE status='pending'
            ORDER BY attempts,created_at
            LIMIT ?""",
        (max(1, min(int(limit), 1000)),),
    ).fetchall()
    completed = sum(
        flush_media_cleanup_outbox(str(row["id"]))
        for row in rows
    )
    return {
        "attempted": len(rows),
        "completed": completed,
        "failed": len(rows) - completed,
    }


def flush_pending_media_cleanup(limit: int = 100) -> int:
    """Compatibility wrapper used by startup recovery."""
    return sweep_pending_media_cleanup(limit)["completed"]


async def media_cleanup_outbox_loop(
    *,
    interval_seconds: float = _MEDIA_CLEANUP_SWEEP_INTERVAL_SECONDS,
    batch_limit: int = _MEDIA_CLEANUP_SWEEP_LIMIT,
) -> None:
    """Retry a bounded pending batch after each idle interval."""
    interval = max(0.01, float(interval_seconds))
    while True:
        await asyncio.sleep(interval)
        try:
            report = sweep_pending_media_cleanup(batch_limit)
            if report["attempted"]:
                from app.observability.metrics import inc

                inc(
                    "media_cleanup_outbox_attempts_total",
                    value=report["attempted"],
                    completed=report["completed"],
                    failed=report["failed"],
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one pass must not kill the loop
            from app.errors import log_error

            log_error(
                exc,
                action="background.media_cleanup_outbox",
                context={"batch_limit": batch_limit},
                meta={"stage": "background_sweep"},
            )


def start_media_cleanup_outbox_loop() -> None:
    """Start the single owner-managed cleanup retry loop."""
    from app import task_registry

    if task_registry.active("system", "media_cleanup_outbox"):
        return
    task_registry.spawn(
        "system",
        "media_cleanup_outbox",
        media_cleanup_outbox_loop(),
    )


def clear_episode_artifacts(episode_id: str) -> dict:
    """清空整集每个镜头的参考图、关键帧、视频版本与模型分析（mode_plan），并把该集回退到「已确认」。
    用于生成台的「清空本集」操作。"""
    conn = get_conn()
    _begin_clear_transaction(conn, episode_id)
    try:
        shots = rows_to_dicts(conn.execute(
            "SELECT * FROM shots WHERE episode_id=?", (episode_id,)).fetchall())
        refs = 0
        for s in shots:
            refs += _delete_shot_reference_dir(conn, s)
            _delete_shot_reference_records(conn, s["id"])
            conn.execute("UPDATE shots SET mode_plan=NULL WHERE id=?", (s["id"],))
        # A resource clear is a new execution epoch. Keep historical plans and
        # attempts for audit, but never reactivate a failure-derived fallback
        # revision when the user starts the episode again.
        conn.execute(
            """UPDATE shot_video_generation_plans
                  SET status='stale',updated_at=strftime('%s','now')
                WHERE episode_video_plan_id IN (
                    SELECT id FROM episode_video_generation_plans
                    WHERE episode_id=?
                      AND status IN ('draft','valid','blocked','stale')
                )""",
            (episode_id,),
        )
        conn.execute(
            """UPDATE episode_video_generation_plans
                  SET status='superseded'
                WHERE episode_id=?
                  AND status IN ('draft','valid','blocked','stale')""",
            (episode_id,),
        )
        conn.execute(
            """UPDATE artifacts
                  SET status='superseded',
                      stale_reason='用户已清空本集生成资源'
                WHERE type='video_supervisor_checkpoint'
                  AND scope_type='episode' AND scope_id=?
                  AND status IN ('candidate','validated','approved')""",
            (episode_id,),
        )
        versions, affected_eps = _purge_shots(
            conn,
            shots,
            preserve_video_audit=True,
        )
        _rollback_episodes(conn, affected_eps or {episode_id})
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"episode_id": episode_id, "shots": len(shots), "videos": versions, "references": refs}


def _invalidate_final_video(
    project_id: str,
    episode_no: int,
    *,
    suppress_errors: bool = True,
    not_newer_than: float | None = None,
) -> None:
    """标记某集已合成的整集成品为待更新，但不删除用户已经得到的成品。

    生成任务与成片台刷新是并行的。过去这里直接删除 ``episode.mp4``，会让
    正在播放的成片在下一次状态轮询时突然消失。现在用同目录的轻量标记记录
    “当前采纳关系已变化”，旧成品继续可预览/下载，交付门禁则要求重新合成。
    """
    final_path = config.PROJECTS_DIR / project_id / "episodes" / str(episode_no) / "final" / "episode.mp4"
    stale_path = final_path.with_suffix(".stale")
    try:
        if (
            final_path.exists()
            and (
                not_newer_than is None
                or final_path.stat().st_mtime <= not_newer_than
            )
        ):
            atomic_write_text(stale_path, "outdated\n")
    except OSError:
        if not suppress_errors:
            raise


def _adopted_video_paths(episode_id: str) -> list[tuple[int, str]]:
    """按镜头顺序返回 (shot_no, video_path)，仅含已采纳且文件可用的镜头。"""
    conn = get_conn()
    rows = conn.execute(
        """SELECT s.shot_no, v.video_path
           FROM shots s
           JOIN shot_versions v ON v.id = s.adopted_version_id
           WHERE s.episode_id=?
             AND v.status='succeeded' AND v.video_path IS NOT NULL
             AND NOT (
               json_valid(v.image_inputs)
               AND COALESCE(json_extract(v.image_inputs,'$.delivery_fallback'),0)=1
             )
           ORDER BY s.shot_no""",
        (episode_id,)).fetchall()
    return [
        (r["shot_no"], r["video_path"])
        for r in rows
        if r["video_path"] and Path(r["video_path"]).is_file()
    ]
