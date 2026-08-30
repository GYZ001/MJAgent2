"""镜头视频/已采纳素材的陈旧性判据。

从 app/domain/storyboard_ops.py 按原样搬移。
"""
from __future__ import annotations

import json


def _shot_video_is_stale(conn, shot_row, episode_storyboard_id: str | None) -> bool:
    """分镜 Artifact 不一致，或采用版冻结的人物/场景版本已落后于本集最新，均判 stale。"""
    try:
        adopted = shot_row["adopted_version_id"]
    except (KeyError, IndexError, TypeError):
        adopted = None
    if not adopted:
        return False
    try:
        shot_art = shot_row["storyboard_artifact_id"]
    except (KeyError, IndexError, TypeError):
        shot_art = None
    if episode_storyboard_id and shot_art and shot_art != episode_storyboard_id:
        episode_art = conn.execute(
            "SELECT parent_artifact_ids_json FROM artifacts WHERE id=?",
            (episode_storyboard_id,),
        ).fetchone()
        try:
            episode_parents = json.loads(
                episode_art["parent_artifact_ids_json"] or "[]"
            ) if episode_art else []
        except (TypeError, ValueError):
            episode_parents = []
        if shot_art not in episode_parents:
            return True
    ver = conn.execute(
        "SELECT artifact_id, image_inputs FROM shot_versions WHERE id=?", (adopted,)
    ).fetchone()
    if not ver or not ver["artifact_id"]:
        # 无 artifact 时仍可检查资产版本 stale
        if ver and _shot_adopted_assets_stale(conn, shot_row, ver):
            return True
        return False
    art = conn.execute(
        "SELECT parent_artifact_ids_json FROM artifacts WHERE id=?",
        (ver["artifact_id"],),
    ).fetchone()
    if art:
        try:
            parents = json.loads(art["parent_artifact_ids_json"] or "[]")
        except (TypeError, ValueError):
            parents = []
        if episode_storyboard_id and parents:
            valid_storyboard_parents = {episode_storyboard_id}
            if shot_art:
                valid_storyboard_parents.add(shot_art)
            if not any(parent in valid_storyboard_parents for parent in parents):
                return True
    return _shot_adopted_assets_stale(conn, shot_row, ver)

def _shot_adopted_assets_stale(conn, shot_row, version_row) -> bool:
    """采用版 reference_manifest 中的人物/场景 revision 是否仍是本集当前生效版本。"""
    try:
        from app.multiview import (
            character_multiview_enabled, scene_multiview_enabled,
            manifest_asset_revision_ids, manifest_asset_view_fingerprints,
            portrait_row_for_episode, scene_row_for_episode,
        )
    except Exception:  # noqa: BLE001
        return False
    if not character_multiview_enabled() and not scene_multiview_enabled():
        return False
    meta = {}
    try:
        meta = json.loads(version_row["image_inputs"] or "{}") if version_row["image_inputs"] else {}
    except (TypeError, ValueError, KeyError):
        meta = {}
    manifest = meta.get("reference_manifest") if isinstance(meta, dict) else None
    if not isinstance(manifest, dict):
        # 回退：从首张带 dependency_manifest 的参考图读取
        for ref in (meta.get("reference_images") or []) if isinstance(meta, dict) else []:
            if isinstance(ref, dict) and isinstance(ref.get("dependency_manifest"), dict):
                manifest = ref["dependency_manifest"]
                break
    if not isinstance(manifest, dict):
        return False
    frozen_ids = manifest_asset_revision_ids(manifest)
    if not frozen_ids:
        return False
    try:
        episode_id = shot_row["episode_id"]
    except (KeyError, IndexError, TypeError):
        return False
    ep = conn.execute("SELECT project_id, episode_no FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        return False
    project_id = ep["project_id"]
    episode_no = ep["episode_no"]
    for key, frozen_rev in frozen_ids.items():
        if key.startswith("character:"):
            name = key.split(":", 1)[1]
            row = portrait_row_for_episode(project_id, name, episode_no)
            current_id = row["id"] if row else None
            if current_id != frozen_rev:
                return True
        elif key.startswith("scene:"):
            name = key.split(":", 1)[1]
            row = scene_row_for_episode(project_id, name, episode_no)
            current_id = row["id"] if row else None
            if current_id != frozen_rev:
                return True
    frozen_views = manifest_asset_view_fingerprints(manifest)
    for (kind, name, role), frozen_fp in frozen_views.items():
        if kind == "character":
            parent = portrait_row_for_episode(project_id, name, episode_no)
            table = "character_portrait_views"
            parent_column = "portrait_id"
        else:
            parent = scene_row_for_episode(project_id, name, episode_no)
            table = "scene_reference_views"
            parent_column = "scene_reference_id"
        if not parent:
            return True
        current = conn.execute(
            f"SELECT input_fingerprint FROM {table} "
            f"WHERE {parent_column}=? AND view_role=? AND status='ready'",
            (parent["id"], role),
        ).fetchone()
        current_fp = current["input_fingerprint"] if current else None
        if current_fp != frozen_fp:
            return True
    return False
