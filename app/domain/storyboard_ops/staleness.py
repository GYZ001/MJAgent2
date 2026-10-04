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
        "SELECT artifact_id, image_inputs, created_at FROM shot_versions WHERE id=?", (adopted,)
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

def _adopted_reference_manifest(version_row) -> dict | None:
    """从采用版的 ``image_inputs`` 里取出冻结时的 reference_manifest；旧记录
    落在首张参考图的 ``dependency_manifest`` 里时做同等回退。"""
    try:
        meta = json.loads(version_row["image_inputs"] or "{}") if version_row["image_inputs"] else {}
    except (TypeError, ValueError, KeyError):
        meta = {}
    manifest = meta.get("reference_manifest") if isinstance(meta, dict) else None
    if not isinstance(manifest, dict):
        for ref in (meta.get("reference_images") or []) if isinstance(meta, dict) else []:
            if isinstance(ref, dict) and isinstance(ref.get("dependency_manifest"), dict):
                manifest = ref["dependency_manifest"]
                break
    return manifest if isinstance(manifest, dict) else None


def _shot_episode_row(conn, shot_row):
    try:
        episode_id = shot_row["episode_id"]
    except (KeyError, IndexError, TypeError):
        return None
    return conn.execute(
        "SELECT project_id, episode_no FROM episodes WHERE id=?", (episode_id,),
    ).fetchone()


def _selected_views_stale(conn, views, *, frozen_rev, current_id, table: str, parent_column: str) -> bool:
    """冻结视角逐条核对内容指纹；指纹与自身 revision id 相同的那条不是真实多
    视角内容哈希，是分镜包分支拿当前定妆照/场景图顶替多视角的伪视角（见
    ``_shot_adopted_assets_stale`` docstring），revision id 那层比较已经覆盖，
    这里跳过——否则伪视角的 portrait_id/scene_reference_id 字符串永远不可能
    等于视角表里的真实内容哈希，会把「资产其实没变」恒判成 stale。"""
    frozen_rev_str = str(frozen_rev or "")
    for view in views or []:
        role = str(view.get("view_role") or "")
        frozen_fp = str(view.get("input_fingerprint") or "")
        if not role or not frozen_fp or frozen_fp == frozen_rev_str:
            continue
        current = conn.execute(
            f"SELECT input_fingerprint FROM {table} "
            f"WHERE {parent_column}=? AND view_role=? AND status='ready'",
            (current_id, role),
        ).fetchone()
        current_fp = current["input_fingerprint"] if current else None
        if current_fp != frozen_fp:
            return True
    return False


def _shot_adopted_assets_stale(conn, shot_row, version_row) -> bool:
    """采用版 reference_manifest 中的人物/场景 revision 是否仍是本集当前生效版本。

    人物比较改用 ``current_portrait_ref``（带 ``identity_id`` 当
    ``visual_entity_id``）——与生成侧 ``app.multiview._storyboard_pack_asset_
    dependencies`` 冻结 manifest 时用的同一份判据，不再另用只按名字查、且不排除
    已作废负 ep_start 历史槽位的 ``portrait_row_for_episode``，两份判据不会再
    漂移（见 app.portraits.current_ref 模块 docstring）。"""
    try:
        # 延迟导入且包在 try 里：本模块被 app.domain.storyboard_ops 包 __init__
        # 全量 import，app.multiview 万一因局部改动导入失败会拖垮整个包；降级成
        # 「判不 stale」远比全站起不来安全（经验证：module 级导入本身没有循环依赖，
        # 这里是刻意的失败隔离，不是绕循环）。
        from app.multiview import character_multiview_enabled, scene_multiview_enabled, scene_row_for_episode
        # current_portrait_ref 同理：与上面 app.multiview 导入失败时一起降级。
        from app.portraits import current_portrait_ref
    except Exception:  # noqa: BLE001
        return False
    manifest = _adopted_reference_manifest(version_row)
    if manifest is None:
        return False
    ep = _shot_episode_row(conn, shot_row)
    if not ep:
        return False
    project_id, episode_no = ep["project_id"], ep["episode_no"]

    # 人物/场景多视角判据受这两个全局开关控制（都关掉时跳过下面两段循环），
    # 但道具判据（``_props_stale``）与多视角功能无关，必须始终执行——曾经把
    # ``_props_stale`` 的调用也挂在这同一道 if 之后，两个开关都被关掉时道具
    # 漂移会被整体吞掉（2026-10-03 用仓库内真实函数复现：构造 ready False→
    # True 的真实道具漂移，关闭两个开关后本函数仍返回 False）。
    if character_multiview_enabled() or scene_multiview_enabled():
        for ch in manifest.get("characters") or []:
            name = str(ch.get("name") or "")
            if not name:
                continue
            frozen_rev = ch.get("look_revision_id")
            identity_id = str(ch.get("identity_id") or "") or None
            current = current_portrait_ref(
                project_id, name, episode_no, visual_entity_id=identity_id, conn=conn,
            )
            current_id = current["portrait_id"] if current else None
            if current_id != frozen_rev:
                return True
            if _selected_views_stale(
                conn, ch.get("selected_views"), frozen_rev=frozen_rev, current_id=current_id,
                table="character_portrait_views", parent_column="portrait_id",
            ):
                return True

        scenes = [manifest.get("scene") or {}, *(manifest.get("additional_scenes") or [])]
        for scene in scenes:
            if not isinstance(scene, dict):
                continue
            name = str(scene.get("name") or "")
            if not name:
                continue
            frozen_rev = scene.get("scene_revision_id")
            row = scene_row_for_episode(project_id, name, episode_no, conn=conn)
            current_id = row["id"] if row else None
            if current_id != frozen_rev:
                return True
            if _selected_views_stale(
                conn, scene.get("selected_views"), frozen_rev=frozen_rev, current_id=current_id,
                table="scene_reference_views", parent_column="scene_reference_id",
            ):
                return True
    return _props_stale(conn, manifest, project_id, episode_no, adopted_at=version_row["created_at"])


def _prop_asset_updated_after(conn, project_id: str, episode_no: int, label: str, adopted_at) -> bool:
    """与 ``app.domain.video_ops.asset_drift._prop_asset_updated_after`` 同一
    判据（不另起一套，见该函数 docstring 的完整理由）：冻结条目缺
    ``prop_revision_id`` 字段时，比较道具参考图最近一次登记/重出图的时间
    （``prop_references.created_at``）与这个采用版本的生成时间
    （``shot_versions.created_at``）。"""
    try:
        # 延迟导入：与本文件其余资产类型同款的失败隔离策略（见
        # ``_shot_adopted_assets_stale`` 开头 try/except 注释）。
        from app.props import prop_reference_for_episode
    except Exception:  # noqa: BLE001
        return False
    row = prop_reference_for_episode(conn, project_id, label, episode_no)
    if row is None or adopted_at is None:
        return False
    try:
        # 两个时间戳恰好相等时按「未变化」处理（严格大于才判更新）：与
        # ``app.domain.video_ops.asset_drift._prop_asset_updated_after`` 同一
        # 保守方向的选择，理由见该函数 docstring。
        return float(row["created_at"]) > float(adopted_at)
    except (TypeError, ValueError):
        return False


def _props_stale(conn, manifest: dict, project_id: str, episode_no: int, *, adopted_at) -> bool:
    """冻结 manifest 里的道具参考（ready/外观卡版本）是否已落后于当前库状态。

    ``prop_revision_id`` 是 2026-10-01 才加入冻结 manifest 的字段（见
    ``app.video_modes.prop_references`` 模块 docstring）：更早冻结的条目里这
    个键根本不存在，不是显式 None。这种情况不再当"未知=不变"直接放行——
    2026-10-04 生产实测：《顾念长安》第 1 集同一件"浅灰色卫衣"卡被重新出图
    （删掉星盘压痕）后，缺这个字段的旧采用版本全部被漏报成不过期——改走
    ``_prop_asset_updated_after`` 的时间判据回退：卡的最近生效时间晚于这个
    版本的生成时间才判 stale，早于才维持"未重新登记过就不算变化"的原有保护
    （2026-10-03 实测：B 库里 `绿萝`/`手机`/`外套` 从未重新登记过，不能仅因
    代码升级补了新字段就把旧版本判过期）。``ready`` 字段在这次升级之前就已
    存在，可以放心直接比较。"""
    try:
        # 延迟导入：道具库 app.props 未就位时按"查不到就不算变化"降级，与本
        # 文件其余资产类型同款的失败隔离策略（见函数开头 try/except 注释）。
        from app.video_modes.prop_references import resolve_segment_prop_manifest_entries
    except Exception:  # noqa: BLE001
        return False
    for frozen_prop in manifest.get("props") or []:
        if not isinstance(frozen_prop, dict):
            continue
        label = str(frozen_prop.get("label") or "").strip()
        if not label:
            continue
        current = resolve_segment_prop_manifest_entries(
            [{"label": label, "description": frozen_prop.get("description")}],
            conn=conn, project_id=project_id, episode_no=episode_no,
        )[0]
        if bool(frozen_prop.get("ready")) != bool(current.get("ready")):
            return True
        if "prop_revision_id" in frozen_prop:
            if frozen_prop["prop_revision_id"] != current.get("prop_revision_id"):
                return True
        elif _prop_asset_updated_after(conn, project_id, episode_no, label, adopted_at):
            return True
    return False
