"""分镜必须覆盖整集原文，否则不得进入付费生成（判据见 source_coverage 模块）。"""
from __future__ import annotations

import sqlite3

from app import db
from app.domain.video_ops.source_coverage import (
    _merged_length,
    storyboard_source_coverage_gap,
)


def _conn(chapter_text: str) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for statement in db.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,status,source_chapters,created_at)"
        " VALUES('e','p',1,'scripted','[1]',0)"
    )
    conn.execute(
        "INSERT INTO chapters(project_id,idx,title,content) VALUES('p',1,'第1章',?)",
        (chapter_text,),
    )
    conn.commit()
    return conn


def _add_shot(conn, shot_id: str, shot_no: int, span: tuple[int, int] | None) -> None:
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s) VALUES(?,?,?,15)",
        (shot_id, "e", shot_no),
    )
    if span is not None:
        chapter_id = conn.execute(
            "SELECT id FROM chapters WHERE project_id='p' AND idx=1",
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO storyboard_source_bindings(
                   shot_id,binding_kind,chapter_id,chapter_idx,source_version_hash,
                   start_offset,end_offset,excerpt_hash,updated_at
               ) VALUES(?,'source_excerpt',?,1,'h',?,?,'x',0)""",
            (shot_id, chapter_id, span[0], span[1]),
        )
    conn.commit()


def test_merged_length_handles_overlapping_spans() -> None:
    """相邻镜头的绑定实测会重叠，并集不能重复计。"""
    assert _merged_length([(0, 841), (0, 1731), (841, 2618), (1731, 2618), (2618, 3176)]) == 3176
    assert _merged_length([(0, 849)]) == 849
    assert _merged_length([]) == 0
    # 有空洞：中间 100..200 没人覆盖
    assert _merged_length([(0, 100), (200, 300)]) == 200


def test_full_coverage_passes() -> None:
    conn = _conn("甲" * 300)
    _add_shot(conn, "s1", 1, (0, 150))
    _add_shot(conn, "s2", 2, (150, 300))

    assert storyboard_source_coverage_gap(conn, "e") is None


def test_truncated_storyboard_is_reported_with_numbers() -> None:
    """真实事故形态：整章 3208 字只有一镜绑了开头 849 字，后 74% 静默消失。"""
    conn = _conn("甲" * 3208)
    _add_shot(conn, "s1", 1, (0, 849))

    gap = storyboard_source_coverage_gap(conn, "e")

    assert gap is not None
    assert "2359" in gap and "3208" in gap
    # 拦住用户时必须给出路
    assert "分镜台" in gap


def test_internal_hole_is_reported() -> None:
    """尾部到位但中间漏掉一段，同样是缺口。"""
    conn = _conn("甲" * 300)
    _add_shot(conn, "s1", 1, (0, 100))
    _add_shot(conn, "s2", 2, (200, 300))

    gap = storyboard_source_coverage_gap(conn, "e")

    assert gap is not None and "100" in gap


def test_episode_without_bindings_is_not_blocked() -> None:
    """老版逐镜叙事契约不写 storyboard_source_bindings，不能拿它们当缺口。"""
    conn = _conn("甲" * 300)
    _add_shot(conn, "s1", 1, None)

    assert storyboard_source_coverage_gap(conn, "e") is None


def test_unknown_episode_is_not_blocked() -> None:
    conn = _conn("甲" * 300)

    assert storyboard_source_coverage_gap(conn, "missing") is None
