"""冻结判据：挂产物信号，不挂状态字面值（CLAUDE.md「Gates and Criteria」）。

命中任一即冻结：``screenplay_status<>'pending'``、``delivery_status<>
'not_ready'``、任何以 ``_artifact_id`` 结尾或形如 ``active_*_run_id`` 的列非
空、或该集下存在任何 ``shots`` 行。按列名模式动态发现这些信号列，而不是手
写穷举——新迁移加一列 ``*_artifact_id`` 会自动被纳入冻结判据，不需要同步改
这个文件（否则就是本仓库反复踩过的"判据漏了新字段"同类坑）。
"""
from __future__ import annotations

import json
from typing import Any

_ARTIFACT_ID_SUFFIX = "_artifact_id"
_RUN_ID_SUFFIX = "_run_id"


def _episode_signal_columns(conn: Any) -> list[str]:
    columns = [row[1] for row in conn.execute("PRAGMA table_info(episodes)").fetchall()]
    return [
        c for c in columns
        if c.endswith(_ARTIFACT_ID_SUFFIX)
        or (c.startswith("active_") and c.endswith(_RUN_ID_SUFFIX))
    ]


def frozen_episode_rows(conn: Any, project_id: str) -> list[dict]:
    """返回该项目里命中任一产物信号、必须一个字节不改的 episodes 行。"""
    signal_cols = _episode_signal_columns(conn)
    signal_clause = "".join(f" OR {c} IS NOT NULL" for c in signal_cols)
    sql = (
        "SELECT * FROM episodes WHERE project_id=? AND ("
        "screenplay_status<>'pending' OR delivery_status<>'not_ready'"
        f"{signal_clause}"
        " OR EXISTS(SELECT 1 FROM shots WHERE episode_id=episodes.id))"
    )
    return [dict(row) for row in conn.execute(sql, (project_id,)).fetchall()]


def frozen_chapter_idx(conn: Any, project_id: str, frozen_episodes: list[dict]) -> set[int]:
    """冻结章节 = 冻结集绑定的章节 ∪ 被 ``storyboard_source_bindings`` 引用的章节。

    后半部分是安全网：万一某个未被判定为"冻结集"的集，其 shots 仍残留着对
    某章节的原文证据绑定（例如集被删但绑定行没清干净），也不能动那一章。
    """
    idx: set[int] = set()
    for ep in frozen_episodes:
        try:
            idx.update(int(v) for v in json.loads(ep["source_chapters"] or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    bound_rows = conn.execute(
        """SELECT DISTINCT c.idx FROM storyboard_source_bindings b
               JOIN chapters c ON c.id = b.chapter_id
              WHERE c.project_id=?""",
        (project_id,),
    ).fetchall()
    idx.update(int(row[0]) for row in bound_rows)
    return idx
