"""按集解析「当前实际会用的那张定妆照」——生成侧与展示侧共用的唯一判据。

拆出自 portrait_io.py（文件行数棘轮逼的，见 app/FILE_CONVENTIONS.toml）。
``_current_portrait_row`` 是本模块唯一的查询实现；``portrait_for_episode``
（生成时取参考图，供 app.refs / app.video_modes / app.media_exec 使用）与
``current_portrait_ref``（映射台/分镜台/生成台展示当前定妆照，供
app.domain.storyboard_ops.current_portraits 使用）都只调用它，不允许再有
第二份相似查询——两份判据必然漂移，真实事故正是界面读了已发布产物里固化
的 portrait_id 快照、与生成时实际选中的那张不同。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from app.db import get_conn


def _current_portrait_row(
    project_id: str,
    name: str,
    episode_no: int,
    *,
    visual_entity_id: str | None = None,
):
    """覆盖 ``episode_no`` 的那一段定妆照整行——生成侧唯一的选段判据
    （``ep_start>=0 AND ep_start<=episode_no AND (ep_end IS NULL OR
    ep_end>=episode_no)``，命中里 ``ep_start`` 最大的那段胜出）。
    ``visual_entity_id`` 非空时优先按视觉实体 ID 查询——同一视觉实体跨集
    复用同一张脸，不受该集本次称谓/是否已具名影响（设计文档 §4.2）；未
    命中（含该列尚未迁移落地）时回退到既有的 ``character_name`` 路径。命中
    行的文件已从磁盘丢失时视为未命中，调用方不得回退到别的判据。

    显式 ``ep_start>=0``：``promote_staged_initial_portrait`` 把手工重新定妆
    前的旧包压成历史槽位时，从 -1 递减、``ep_end`` 恒置 0——真实数据里存在
    （``portrait_5889d5f70972``→-1、``portrait_383d7c8520ff``→-2）。这些槽位
    对 ``episode_no>=1`` 时已经会被 ``ep_end>=episode_no`` 条件排除，但那是
    ``ep_end=0`` 这个具体取值带来的隐式副作用，不是本查询自己声明的下限——
    显式加下限，不依赖另一个字段的巧合取值来防止把"其实没有有效图"误判成
    "有图"。
    """
    if visual_entity_id:
        try:
            row = get_conn().execute(
                "SELECT * FROM character_portraits "
                "WHERE project_id=? AND visual_entity_id=? AND ep_start>=0 AND ep_start<=? "
                "AND (ep_end IS NULL OR ep_end>=?) "
                "ORDER BY ep_start DESC LIMIT 1",
                (project_id, visual_entity_id, episode_no, episode_no),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        if row and row["image_path"] and Path(row["image_path"]).exists():
            return row
    try:
        row = get_conn().execute(
            "SELECT * FROM character_portraits "
            "WHERE project_id=? AND character_name=? AND ep_start>=0 AND ep_start<=? "
            "AND (ep_end IS NULL OR ep_end>=?) "
            "ORDER BY ep_start DESC LIMIT 1",
            (project_id, name, episode_no, episode_no)).fetchone()
    except sqlite3.OperationalError:
        return None
    if row and row["image_path"] and Path(row["image_path"]).exists():
        return row
    return None


def portrait_for_episode(
    project_id: str,
    name: str,
    episode_no: int | None,
    *,
    visual_entity_id: str | None = None,
) -> str | None:
    """返回覆盖该集的定妆照落盘路径；未命中返回 None（调用方回退到 bible.ref_image_path）。

    ``visual_entity_id`` 非空时优先按视觉实体 ID 查询——同一视觉实体跨集
    复用同一张脸，不受该集本次称谓/是否已具名影响（设计文档 §4.2）；未
    命中（含该列尚未迁移落地）时回退到既有的 ``character_name`` 路径。
    """
    if episode_no is None:
        return None
    row = _current_portrait_row(project_id, name, episode_no, visual_entity_id=visual_entity_id)
    return row["image_path"] if row else None


def current_portrait_ref(
    project_id: str,
    name: str,
    episode_no: int | None,
    *,
    visual_entity_id: str | None = None,
) -> dict | None:
    """映射台/分镜台/生成台展示用：覆盖该集「当前实际会用的那张」定妆照的
    id + 落盘路径。与 ``portrait_for_episode`` 共用同一份选段判据
    （``_current_portrait_row``），保证展示侧与生成侧永远读同一个答案。
    未命中（角色没有定妆照，或文件已从磁盘丢失）返回 None；调用方必须原样
    显示"无定妆照"，不得回退到快照 id 对应的那张图。
    """
    if episode_no is None:
        return None
    row = _current_portrait_row(project_id, name, episode_no, visual_entity_id=visual_entity_id)
    if not row:
        return None
    return {"portrait_id": str(row["id"]), "image_path": row["image_path"]}
