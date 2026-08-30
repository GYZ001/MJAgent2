"""整集源章节的读取与拼接——唯一实现，禁止另起一份。

``_episode_chapters``/``_episode_source_blocks``/``_episode_source_text`` 从
``app/domain/common.py`` 按原样搬移到这里（2026-08-30，见
``docs/layer_violations_plan_2026-08-30.md`` 组 7b）：三者只依赖
``app.db.rows_to_dicts``（L2）+ ``app.ingest.chapter_is_stub``/
``chapter_titles_match``（L1），但原来待在 L5 的 ``app.domain.common`` 里，逼着
``app.production.storyboard_pack``/``app.production.prep_pack.generate_once``
（均 L4）为了这三个纯读取/拼接函数越级 import 整个 domain 包。

「读哪些章、按什么顺序」「章节行 -> 集源文本」只能有一份实现，
``app.production.prep_pack`` 的 paratext 偏移换算与集源文本本身都调用这一份，
两处各写一份会产生漂移风险（见 ``logs/paratext_single_source_plan.md``）。
``app.domain.common`` 继续从本模块重新导入并保持这三个名字可从
``app.domain.common``/``app.domain``/``app.domain.screenplay_ops`` 原样导入，
不影响任何既有调用点。
"""
from __future__ import annotations

import json

from app.db import rows_to_dicts
from app.ingest import chapter_is_stub, chapter_titles_match


def _episode_chapters(conn, ep) -> list[dict]:
    """本集源章节行（stub 修复后），供 `_episode_source_text` 和 paratext
    偏移换算（`app.production.prep_pack`）共用——"读哪些章、按什么顺序"
    只能有一份实现，两处各写一份会产生漂移风险（见
    logs/paratext_single_source_plan.md）。返回的每行是 `SELECT *`，含
    `id`/`title`/`content`/`paratext_json` 等全部列。
    """
    raw_source_chapters = ep["source_chapters"] or []
    source_chapters = (
        json.loads(raw_source_chapters)
        if isinstance(raw_source_chapters, str)
        else list(raw_source_chapters)
    )
    if not source_chapters:
        return []
    placeholders = ",".join("?" for _ in source_chapters)
    chapters = rows_to_dicts(conn.execute(
        f"SELECT * FROM chapters WHERE project_id=? AND idx IN ({placeholders}) ORDER BY idx",
        (ep["project_id"], *source_chapters)).fetchall())
    # Backward-compatible repair for already imported projects: if an episode points
    # at a title-only duplicate, use the adjacent rich copy with the same normalized
    # heading. New uploads are deduplicated in app.ingest before reaching the DB.
    if len(chapters) == 1 and chapter_is_stub(chapters[0]):
        following = conn.execute(
            "SELECT * FROM chapters WHERE project_id=? AND idx>? ORDER BY idx LIMIT 1",
            (ep["project_id"], chapters[0]["idx"]),
        ).fetchone()
        if following:
            following_dict = dict(following)
            if (
                not chapter_is_stub(following_dict)
                and chapter_titles_match(chapters[0], following_dict)
            ):
                chapters = [following_dict]
    return chapters


def _episode_source_blocks(chapters: list[dict]) -> tuple[str, list[int]]:
    """章节行 -> 集源文本 + 每章 `content` 在这段文本里的绝对起点。

    唯一的拼接实现——集源文本本身和"把 chapters.paratext_json 里以章为
    单位的偏移平移到集级坐标"（`app.production.prep_pack`）都调用这一份，
    禁止另起一份公式，否则又是"两处判据各自实现导致漂移"（见
    logs/paratext_single_source_plan.md）。`offsets[i]` = `chapters[i]`
    的 `content` 在返回文本里的起始下标（紧跟在 `【title】\\n` 前缀之后）。
    """
    parts: list[str] = []
    offsets: list[int] = []
    cursor = 0
    for index, ch in enumerate(chapters):
        if index > 0:
            parts.append("\n\n")
            cursor += 2
        prefix = f"【{ch['title']}】\n"
        parts.append(prefix)
        cursor += len(prefix)
        offsets.append(cursor)
        content = ch["content"]
        parts.append(content)
        cursor += len(content)
    return "".join(parts), offsets


def _episode_source_text(conn, ep) -> str:
    text, _content_offsets = _episode_source_blocks(_episode_chapters(conn, ep))
    return text
