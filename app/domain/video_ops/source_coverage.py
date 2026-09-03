"""分镜原文覆盖判据：整集原文有没有被镜头真正用完。

真实事故（2026-08-31，proj_f8cf2eeb2e66 EP10）：一集 3208 字的原文只切出 1 个
镜头，绑定区间 [0, 849]，后面 2359 字（74%）没有任何镜头。确认门照样放行，
付费视频照样生成，最后合出一条 15 秒的"成片"——只演了开头，整章后四分之三
静默消失。

原有完整性判据（``storyboard_pack_prompts_complete``）只问"每一镜有没有提示词、
尾镜有没有 is_final"，问的是这批镜头自身完不完整，从来没问过"这批镜头覆盖了
多少原文"。计划镜数本身就是 1，于是 1/1 也判终态通过。

判据对**剧情正文**零容忍：一旦有正文字没被任何镜头绑定就是缺口，不设容差、
不设比例阈值。但只数剧情正文——章节标题、段落之间的空行、以及映射台已经判定并
落库的副文本（``chapters.paratext_json``）不是剧情，分镜管线本来就不会给它们绑
镜头。2026-09-03 实测（B 库 16 个有绑定的集）：橘座在上 EP1 被拦的 18 字是标题
14 字 + 两处段落空行 4 字，三国 EP1 的 33 字、神墓 EP1 的 2 字同样全是标题与空
白；而西游 EP1（198 字正文没镜头）与跑不快的孩子 EP1（813 字）是真漏戏。三条
排除规则都从数据推导（``chapters.title`` / 空白 / 已落库判定），不是关键词黑名单。

缺口消息要把漏掉的原文摘出来给用户看——只报"18 字"没人知道该回分镜台修哪里。

不适用的情形一律不拦（fail open 只对"这条管线不产出绑定"成立）：本集一条绑定
都没有时返回 ``None``——老版逐镜叙事契约与历史 plan-null 兼容分集不写
``storyboard_source_bindings``，拿它们当缺口会把一整类分集永久判不过。
"""

from __future__ import annotations

import json
import re

from app.source_excerpt import chapter_title_segment_ids, index_source_segments
from app.source_paratext import cached_chapter_paratext_offsets

Interval = tuple[int, int]

#: 缺口消息里最多摘几段、每段最多多少字——够用户定位，不把整章塞进错误文案。
GAP_SAMPLE_LIMIT = 3
GAP_SAMPLE_CHARS = 24


def _merged(spans: list[Interval]) -> list[Interval]:
    """区间并集（区间可能重叠，相邻镜头的绑定实测会重叠）。"""
    merged: list[list[int]] = []
    for start, stop in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], stop)
        else:
            merged.append([start, stop])
    return [(start, stop) for start, stop in merged]


def _merged_length(spans: list[Interval]) -> int:
    """区间并集的总长度。"""
    return sum(stop - start for start, stop in _merged(spans))


def _complement(length: int, merged: list[Interval]) -> list[Interval]:
    """[0, length) 里没被 merged 覆盖的区间。"""
    gaps: list[Interval] = []
    cursor = 0
    for start, stop in merged:
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, stop)
    if cursor < length:
        gaps.append((cursor, length))
    return gaps


def _subtract(intervals: list[Interval], regions: list[Interval]) -> list[Interval]:
    """从 intervals 里挖掉 regions（regions 需已并集化、有序）。"""
    result: list[Interval] = []
    for start, stop in intervals:
        cursor = start
        for r_start, r_stop in regions:
            if r_stop <= cursor or r_start >= stop:
                continue
            if r_start > cursor:
                result.append((cursor, r_start))
            cursor = max(cursor, r_stop)
        if cursor < stop:
            result.append((cursor, stop))
    return result


def _structural_regions(content: str, title: str | None, chapter_row) -> list[Interval]:
    """不算剧情的区间：与本章 ``chapters.title`` 逐字相同的标题段 + 已落库的副文本。"""
    regions: list[Interval] = []
    if title and title.strip():
        segments = index_source_segments(content)
        title_ids = chapter_title_segment_ids(segments, [title])
        regions.extend(
            (segment.start_offset, segment.end_offset)
            for segment in segments
            if segment.segment_id in title_ids
        )
    regions.extend(cached_chapter_paratext_offsets(chapter_row))
    return _merged(regions)


def _content_gaps(content: str, intervals: list[Interval]) -> tuple[int, list[str]]:
    """区间里的非空白字数，以及给用户看的原文摘录。"""
    count = 0
    samples: list[str] = []
    for start, stop in intervals:
        piece = content[start:stop]
        compact = re.sub(r"\s+", "", piece)
        if not compact:
            continue
        count += len(compact)
        if len(samples) < GAP_SAMPLE_LIMIT:
            samples.append(re.sub(r"\s+", " ", piece.strip())[:GAP_SAMPLE_CHARS])
    return count, samples


def _chapter_gap(conn, project_id: str, chapter_idx: int, bindings) -> tuple[int, int, list[str]]:
    """一章的 (原文总字数, 未覆盖的正文字数, 摘录)。"""
    row = conn.execute(
        "SELECT * FROM chapters WHERE project_id=? AND idx=?", (project_id, chapter_idx),
    ).fetchone()
    if row is None:
        return 0, 0, []
    content = row["content"] or ""
    spans = [
        (int(b["start_offset"] or 0), int(b["end_offset"] or 0))
        for b in bindings
        if int(b["chapter_idx"] or 0) == chapter_idx
    ]
    gaps = _complement(len(content), _merged(spans))
    gaps = _subtract(gaps, _structural_regions(content, row["title"], row))
    uncovered, samples = _content_gaps(content, gaps)
    return len(content), uncovered, samples


def storyboard_source_coverage_gap(conn, episode_id: str) -> str | None:
    """本集镜头绑定没覆盖到的剧情正文；没有缺口或不适用时返回 ``None``。"""
    row = conn.execute(
        "SELECT project_id, source_chapters FROM episodes WHERE id=?", (episode_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        chapter_indexes = [int(v) for v in json.loads(row["source_chapters"] or "[]")]
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    bindings = conn.execute(
        """SELECT b.chapter_idx, b.start_offset, b.end_offset
             FROM storyboard_source_bindings b
             JOIN shots s ON s.id=b.shot_id
            WHERE s.episode_id=?""",
        (episode_id,),
    ).fetchall()
    if not bindings:
        return None
    uncovered = 0
    source_total = 0
    samples: list[str] = []
    for chapter_idx in chapter_indexes:
        length, missing, chapter_samples = _chapter_gap(conn, row["project_id"], chapter_idx, bindings)
        source_total += length
        uncovered += missing
        samples.extend(chapter_samples)
    if uncovered <= 0:
        return None
    shown = "、".join(f"「{sample}」" for sample in samples[:GAP_SAMPLE_LIMIT])
    return (
        f"分镜没有覆盖整集原文：{source_total} 字里有 {uncovered} 字"
        f"（{uncovered * 100 // max(1, source_total)}%）的剧情正文没有任何镜头对应"
        f"（章节标题、空行与已判定的副文本不计），例如 {shown}；"
        "按现在这批镜头生成出来的成片会漏掉这部分剧情；请回分镜台重做本集分镜。"
    )
