"""按锚点选段的定妆照/外观查询（WS4，2026-09-03）——在既有「按集分段」
（``character_portraits.ep_start``/``ep_end``）判据之上叠加一个正交的「按
锚点选段」维度，例如 ``age:8``/``year:2022``。

真实场景（B 库 proj_ce9fcf749b23《跑不快的孩子》）：主角"里奥"只有一条覆盖
13~17 岁的 ``appearance_canonical``，但全书跨度是 8 岁到 35 岁；平台已有的
按集分段机制假设"造型随集数单调推进"，而这个项目只有 4 集、且同一集内就
跨年代（回忆/闪回），单靠 ep_start/ep_end 表达不了。

``character_portraits.anchor_key`` 是本模块懒迁移加的新列（``_has_column``
探测 + 按需 ``ALTER TABLE ... ADD COLUMN``，同 ``app.portraits.cards``
给 ``projects.bible_auto_changes_json`` 加列的写法），不改 ``app/db.py``
本身——该文件当前处于 ``app/FILE_CONVENTIONS.toml`` 行数基线零余量（实测
行数与基线完全相等），子代理不得为了塞进一条 ALTER 语句去调基线。

不改 :func:`app.portraits.current_ref.portrait_for_episode` /
:func:`app.portraits.portrait_io.appearance_for_episode` 的签名与返回类型
（``str | None``，全仓 20+ 处调用方依赖它们不变）——新增能力独立放在本模块，
返回结构化信号供未来分镜台消费。自动生成新年龄段的定妆照不在本次范围（那
是图像调用，按 WS4 派单由用户在人物谱手动触发）；本模块只负责"如果已经有
锚点造型，查询时优先选它，并告诉调用方有没有选到"。
"""
from __future__ import annotations

from pathlib import Path

from app.db import get_conn

from ._db_probe import _has_column
from .current_ref import _current_portrait_row


def _ensure_anchor_key_column(conn) -> None:
    """懒迁移，幂等；不在调用方连接上隐式 commit——ALTER 在 sqlite 里对同连接
    的后续语句立即可见，是否落盘由连接的所有者按自己的事务边界决定（同
    ``app.portraits.cards._card_owner_lookup`` 里 ``bible_auto_changes_json``
    的加列写法，CLAUDE.md「不得在调用方的连接上隐式提交」）。"""
    if _has_column(conn, "character_portraits", "anchor_key"):
        return
    conn.execute("ALTER TABLE character_portraits ADD COLUMN anchor_key TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_character_portraits_anchor_key "
        "ON character_portraits(project_id, character_name, anchor_key)"
    )


def _row_file_exists(row) -> bool:
    path = row["image_path"] if "image_path" in row.keys() else None
    return bool(path) and Path(path).exists()


def _anchor_portrait_row(
    project_id: str, character_name: str, anchor_key: str,
    *, visual_entity_id: str | None = None, conn=None,
):
    """``anchor_key`` 精确命中的那一行；有 ``visual_entity_id`` 时优先按它查
    （同 ``_current_portrait_row`` 的优先级：跨集稳定，不受当集称谓影响）。
    命中行文件已从磁盘丢失时视为未命中。"""
    db = conn if conn is not None else get_conn()
    _ensure_anchor_key_column(db)
    if visual_entity_id:
        row = db.execute(
            "SELECT * FROM character_portraits WHERE project_id=? AND visual_entity_id=? "
            "AND anchor_key=? ORDER BY created_at DESC LIMIT 1",
            (project_id, visual_entity_id, anchor_key),
        ).fetchone()
        if row and _row_file_exists(row):
            return row
    row = db.execute(
        "SELECT * FROM character_portraits WHERE project_id=? AND character_name=? "
        "AND anchor_key=? ORDER BY created_at DESC LIMIT 1",
        (project_id, character_name, anchor_key),
    ).fetchone()
    return row if row and _row_file_exists(row) else None


def portrait_lookup_for_episode(
    project_id: str, name: str, episode_no: int | None,
    *, visual_entity_id: str | None = None, time_anchor: str | None = None, conn=None,
) -> dict:
    """本集有效的定妆照 + 外观锚点，``time_anchor`` 命中优先于集段判据。

    返回 ``{"image_path", "appearance", "portrait_id", "look_mismatch"}``：
    命中 ``time_anchor`` 时 ``look_mismatch`` 为 None；请求了 ``time_anchor``
    但未命中时回退集段判据，``look_mismatch`` 非空表示"想要这个锚点的造型，
    实际用的是别的"（``{"wanted": ..., "used": "episode_segment"|"none"}``）
    ——调用方（分镜台）据此写警告，本函数只提供信号，不做展示、不发起生成。
    """
    from app.refs import production_appearance_anchor

    anchor_row = None
    if time_anchor:
        anchor_row = _anchor_portrait_row(
            project_id, name, time_anchor, visual_entity_id=visual_entity_id, conn=conn,
        )
    if anchor_row is not None:
        return {
            "image_path": anchor_row["image_path"],
            "appearance": production_appearance_anchor(anchor_row["appearance"] or "") or None,
            "portrait_id": str(anchor_row["id"]),
            "look_mismatch": None,
        }
    fallback_row = None
    if episode_no is not None:
        fallback_row = _current_portrait_row(
            project_id, name, episode_no, visual_entity_id=visual_entity_id, conn=conn,
        )
    look_mismatch = None
    if time_anchor:
        look_mismatch = {
            "wanted": time_anchor,
            "used": "episode_segment" if fallback_row is not None else "none",
        }
    if fallback_row is None:
        return {"image_path": None, "appearance": None, "portrait_id": None, "look_mismatch": look_mismatch}
    return {
        "image_path": fallback_row["image_path"],
        "appearance": production_appearance_anchor(fallback_row["appearance"] or "") or None,
        "portrait_id": str(fallback_row["id"]),
        "look_mismatch": look_mismatch,
    }
