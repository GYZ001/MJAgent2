"""Authoritative cleanup and invalidation of generated media artifacts."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from app import config
from app.db import get_conn, rows_to_dicts

_CLEAR_TERMINAL_RUN_STATES = {
    "SUCCEEDED", "FAILED", "CANCELLED", "COMPLETED", "PARTIAL",
    "succeeded", "failed", "cancelled", "completed", "partial",
}


def _begin_clear_transaction(
    conn,
    episode_id: str,
    *,
    active_storyboard_run_id: str | None = None,
) -> None:
    """Serialize the final upstream check and media purge in SQLite."""
    # Supervisor 局部修复可能在同一连接已有事务（例如先写修复计划再清理相邻镜）。
    # 复用该事务，避免 ``cannot start a transaction within a transaction``；独立的
    # 评审墙清空仍以 BEGIN IMMEDIATE 获取写锁。
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    ep = conn.execute(
        "SELECT status, active_screenplay_run_id, active_storyboard_run_id FROM episodes WHERE id=?",
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
    for run_id in (ep["active_screenplay_run_id"], ep["active_storyboard_run_id"]):
        if not run_id:
            continue
        if authorized_storyboard_run and run_id == active_storyboard_run_id:
            continue
        run = conn.execute("SELECT status FROM workflow_runs WHERE id=?", (run_id,)).fetchone()
        if not run or run["status"] not in _CLEAR_TERMINAL_RUN_STATES:
            active.append(str(run_id))
    writing_status = ep["status"] in {"planned", "scripting", "storyboarding"}
    if (writing_status and not authorized_storyboard_run) or active:
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


def _purge_shots(conn, shots: list[dict]) -> tuple[int, set[str]]:
    """删除给定镜头的全部版本、关键帧、任务与采用标记。
    返回 (删除版本数, 受影响剧集 id 集合)。"""
    versions_removed = 0
    affected_eps: set[str] = set()
    for s in shots:
        affected_eps.add(s["episode_id"])
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
    评审墙关键帧、各版本视频、相关任务、整集成品，并把对应剧集回退到“已确认”，
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


def delete_episode_shots(episode_id: str) -> int:
    """清空单集分镜及其衍生产物。用于剧本重生/编辑后让下游重新展开。"""
    conn = get_conn()
    ep = conn.execute("SELECT project_id, episode_no FROM episodes WHERE id=?", (episode_id,)).fetchone()
    shots = rows_to_dicts(conn.execute(
        "SELECT id, episode_id FROM shots WHERE episode_id=?", (episode_id,)).fetchall())
    _purge_shots(conn, shots)
    conn.execute("DELETE FROM shots WHERE episode_id=?", (episode_id,))
    conn.execute("DELETE FROM jobs WHERE episode_id=?", (episode_id,))
    conn.execute(
        "UPDATE episodes SET status='planned', script_error=NULL WHERE id=? AND status NOT IN ('planned','scripting','script_failed')",
        (episode_id,))
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
    """删除单个视频版本（含视频/尾帧文件、相关任务）；若是采用版则清空采用并使该集成品失效。
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
    """删除某镜的全部视频版本（含视频/尾帧文件、相关任务），清空采用标记，并使该集成品失效。
    用于：该镜关键帧被删空后，旧成片已无首尾帧依据，应一并删除。返回删除的版本数。"""
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
    """采用版本变化后删除旧整集合成；镜头版本本身仍保留。"""
    conn = get_conn()
    ep = conn.execute(
        "SELECT project_id, episode_no FROM episodes WHERE id=?", (episode_id,)
    ).fetchone()
    if not ep:
        return False
    _invalidate_final_video(ep["project_id"], ep["episode_no"])
    return True


def _delete_shot_reference_dir(conn, shot_row) -> int:
    """删除某镜的参考图目录（REFERENCE_IMAGE_MODE 生成的参考图都落在此处）。返回删除的文件数。"""
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


def clear_shot_artifacts(
    shot_id: str,
    *,
    active_storyboard_run_id: str | None = None,
    commit: bool = True,
) -> dict:
    """清空单镜的参考图、关键帧（首/尾图）、视频版本与模型分析（mode_plan），并使该集成品失效。
    用于评审墙的「清空」操作。"""
    conn = get_conn()
    shot = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot:
        return {"shot_id": shot_id, "videos": 0, "references": 0,
                "keyframes_cleared": False, "mode_plan_cleared": False}
    _begin_clear_transaction(
        conn,
        shot["episode_id"],
        active_storyboard_run_id=active_storyboard_run_id,
    )
    try:
        refs = _delete_shot_reference_dir(conn, shot)
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


def clear_episode_artifacts(episode_id: str) -> dict:
    """清空整集每个镜头的参考图、关键帧、视频版本与模型分析（mode_plan），并把该集回退到「已确认」。
    用于评审墙的「清空本集」操作。"""
    conn = get_conn()
    _begin_clear_transaction(conn, episode_id)
    try:
        shots = rows_to_dicts(conn.execute(
            "SELECT * FROM shots WHERE episode_id=?", (episode_id,)).fetchall())
        refs = 0
        for s in shots:
            refs += _delete_shot_reference_dir(conn, s)
            conn.execute("UPDATE shots SET mode_plan=NULL WHERE id=?", (s["id"],))
        versions, affected_eps = _purge_shots(conn, shots)
        _rollback_episodes(conn, affected_eps or {episode_id})
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"episode_id": episode_id, "shots": len(shots), "videos": versions, "references": refs}


def _invalidate_final_video(project_id: str, episode_no: int) -> None:
    """删除某集已合成的整集成品（如存在）。在评审墙产生新片段后调用，
    使成片台回到“需重新合成”的状态，而非展示与当前片段不一致的旧成品。"""
    final_path = config.PROJECTS_DIR / project_id / "episodes" / str(episode_no) / "final" / "episode.mp4"
    try:
        if final_path.exists():
            final_path.unlink()
    except OSError:
        pass


def _adopted_video_paths(episode_id: str) -> list[tuple[int, str]]:
    """按镜头顺序返回 (shot_no, video_path)，仅含已有成片的镜头。"""
    conn = get_conn()
    rows = conn.execute(
        """SELECT s.shot_no, v.video_path
           FROM shots s
           JOIN shot_versions v ON v.id = s.adopted_version_id
           WHERE s.episode_id=? AND v.status='succeeded' AND v.video_path IS NOT NULL
           ORDER BY s.shot_no""",
        (episode_id,)).fetchall()
    return [(r["shot_no"], r["video_path"]) for r in rows]
