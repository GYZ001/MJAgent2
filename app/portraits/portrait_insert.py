"""定妆照候选行的幂等插入。

``character_portraits`` 有 ``UNIQUE(project_id, character_name, ep_start)``。映射台的
角色参考图任务与分镜台的「人物谱补图重试」会并行给同一个新角色出图：两边都在自己查
不到候选行之后各自出图，先落盘的一方赢，后到的一方原先直接 ``IntegrityError`` 把整集
分镜打死（2026-09-03 橘座在上「张姐」）。这里把「后到」改成「接着用已经存在的那一行」。
"""
from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger(__name__)


def insert_portrait_row_or_existing(conn: sqlite3.Connection, values: dict) -> sqlite3.Row:
    """插入候选行并返回它；撞上唯一约束时回滚，改为返回同槽位已存在的那一行。

    返回行的 ``id`` 不等于 ``values["id"]`` 即表示本次插入被别人抢先，调用方应在
    已存在的行上续做（多视角等），不要再动自己那张已经落盘的主图。
    """
    columns = list(values)
    try:
        conn.execute(
            f"INSERT INTO character_portraits({', '.join(columns)}) "
            f"VALUES({', '.join('?' for _ in columns)})",
            tuple(values[column] for column in columns),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        existing = conn.execute(
            "SELECT * FROM character_portraits WHERE project_id=? AND character_name=? AND ep_start=? "
            "ORDER BY created_at DESC LIMIT 1",
            (values["project_id"], values["character_name"], values["ep_start"]),
        ).fetchone()
        if existing is None:
            raise
        log.warning(
            "定妆照候选槽已被并行任务占用，改用已存在的行：project=%s name=%s ep_start=%s existing=%s",
            values["project_id"], values["character_name"], values["ep_start"], existing["id"],
        )
        return existing
    return conn.execute("SELECT * FROM character_portraits WHERE id=?", (values["id"],)).fetchone()
