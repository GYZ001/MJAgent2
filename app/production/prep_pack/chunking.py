"""Source-segment chunking for model calls (chunk sizing/rendering, the
segment-index structural gate, and known-name/chapter-title lookups used to
seed a chunk's prompt).

Split out of app/production/prep_pack.py.
"""
from __future__ import annotations

from app.source_excerpt import SourceSegment

from .contracts import _CHUNK_MAX_CHARS


def _chunk_segments(
    segments: list[SourceSegment], *, max_chars: int = _CHUNK_MAX_CHARS,
) -> list[list[tuple[int, SourceSegment]]]:
    """Group indexed segments into model-call-sized chunks (长章节切块)."""
    indexed = list(enumerate(segments, start=1))
    if not indexed:
        return []
    chunks: list[list[tuple[int, SourceSegment]]] = []
    current: list[tuple[int, SourceSegment]] = []
    current_chars = 0
    for item in indexed:
        _, segment = item
        segment_chars = len(segment.text)
        if current and current_chars + segment_chars > max_chars:
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(item)
        current_chars += segment_chars
    if current:
        chunks.append(current)
    return chunks


def _render_chunk(chunk: list[tuple[int, SourceSegment]]) -> str:
    return "\n\n".join(f"【{index}】\n{segment.text}" for index, segment in chunk)


# 段号结构闸（2.0.0，见 PREP_PACK_VERSION 上方 2.0.0 大注释"锚点从
# event_ids 换成 segment_indexes"一节）：一个提及（角色/场景/道具）自报的
# 每一个 segment_index，必须落在本次 chunk 自己的全局段号范围内——防止模型
# 把别的 chunk 的段号写到这里，每次 chunk 调用只看得到自己那一段原文，
# 声称之外的段号结构上不可信、必须丢弃。
#
# 刻意不在这里额外要求 display_name/label 逐字出现在该段落原文里：那道
# 逐字证据闸本来就已经存在（_prep_pack_mention_has_text_evidence，
# _resolve_assets 内"称谓证据闸"一节），但只对"裸直接命中"（没有经过
# alias/discovery/candidate_verdict 任何一条解析路径）生效，长期以来
# （1.5.x task②、1.8.0-1.8.5 五轮真实回归）刻意豁免经解析路径绑定的合成
# 描述性标签——例如真实 EP1 案例"银色长袍女子"从未逐字出现在原文（原文写
# "穿着一身银色长袍"），要靠候选判别（_prep_pack_resolve_functional_
# extra_candidate）独立的卷宗检索+钉证才能正确绑定许清；如果在这里（比
# _resolve_assets 更早的入口）就要求 display_name 逐字命中它自己声明的
# 段落，会在候选判别机会到来之前就把这整条提及连同它的 segment_indexes
# 一并丢弃，直接堵死候选判别机制——不是收紧反幻觉防线，是重新引入五轮
# 真实回归修过的同一个缺陷。评估过、放弃：per-segment 逐字闸看似能"更
# 精确"，但精确的代价是打断已经证明有效、职责单一的既有分工（模型申报语义
# 判断 -> _resolve_assets 按 method 分支各自核验）。
#
# "这段文字里出现了这个名字"从来不是也不该是"这个人真的在画面里出场"的
# 判据本身——后者是模型的语义职责（_extract_chunk 的提示词明确只要求申报
# "画面中出场"的段号，不是被提及/回忆/转述的段落），不针对任何具体人名/
# 称谓做特判，也不使用任何人名/称谓硬编码名单（no-blacklist-fixes 纪律）。
def _prep_pack_gate_segment_indexes(
    label: str, declared_indexes: list[int],
    chunk_global_indexes: set[int], chunk_by_index: dict[int, SourceSegment],
) -> list[int]:
    label = str(label or "").strip()
    if not label:
        return []
    verified: set[int] = set()
    for raw in declared_indexes:
        try:
            index = int(raw)
        except (TypeError, ValueError):
            continue
        if index in chunk_global_indexes and index in chunk_by_index:
            verified.add(index)
    return sorted(verified)


def _known_character_names(conn, project_id: str, episode_no: int) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT character_name FROM character_portraits "
        "WHERE project_id=? AND ep_start<=? AND (ep_end IS NULL OR ep_end>=?) "
        "ORDER BY character_name",
        (project_id, episode_no, episode_no),
    ).fetchall()
    return [str(row["character_name"]) for row in rows]


def _known_scene_names(conn, project_id: str, episode_no: int) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT scene_name FROM scene_references "
        "WHERE project_id=? AND ep_start<=? AND (ep_end IS NULL OR ep_end>=?) "
        "ORDER BY scene_name",
        (project_id, episode_no, episode_no),
    ).fetchall()
    return [str(row["scene_name"]) for row in rows]


def _prep_pack_chapter_titles(
    conn, project_id: str, chapter_indexes: list[int],
) -> list[str]:
    """This episode's own DB-anchored chapter titles (1.9.0, see
    PREP_PACK_VERSION's 1.9.0 note above). Only non-NULL, non-blank titles
    are returned -- a chapter whose ``chapters.title`` is NULL/blank is
    simply absent from the result, which is exactly the signal
    app.source_excerpt.chapter_title_segment_indexes and
    app.validators.build_prep_pack_span_ledger's chapter_titles parameter
    need to fall back to the pre-1.9.0 regex+model-declare path for that
    one chapter (see build_prep_pack_span_ledger's docstring)."""
    if not chapter_indexes:
        return []
    placeholders = ",".join("?" for _ in chapter_indexes)
    rows = conn.execute(
        f"SELECT title FROM chapters WHERE project_id=? AND idx IN ({placeholders})",
        (project_id, *chapter_indexes),
    ).fetchall()
    return [
        str(row["title"]) for row in rows
        if row["title"] is not None and str(row["title"]).strip()
    ]


