"""分镜原文覆盖判据：整集原文有没有被镜头真正用完。

真实事故（2026-08-31，proj_f8cf2eeb2e66 EP10）：一集 3208 字的原文只切出 1 个
镜头，绑定区间 [0, 849]，后面 2359 字（74%）没有任何镜头。确认门照样放行，
付费视频照样生成，最后合出一条 15 秒的"成片"——只演了开头，整章后四分之三
静默消失。

原有完整性判据（``storyboard_pack_prompts_complete``）只问"每一镜有没有提示词、
尾镜有没有 is_final"，问的是这批镜头自身完不完整，从来没问过"这批镜头覆盖了
多少原文"。计划镜数本身就是 1，于是 1/1 也判终态通过。

判据零容忍，有实测支撑：同一轮里另外 8 个成功集的绑定并集**精确覆盖到章末**
（未覆盖尾部 0、内部空洞 0），EP10 是唯一例外。所以不设容差、不设比例阈值——
一旦有未覆盖的原文就是缺口。

不适用的情形一律不拦（fail open 只对"这条管线不产出绑定"成立）：本集一条绑定
都没有时返回 ``None``——老版逐镜叙事契约与历史 plan-null 兼容分集不写
``storyboard_source_bindings``，拿它们当缺口会把一整类分集永久判不过。
"""

from __future__ import annotations


def _merged_length(spans: list[tuple[int, int]]) -> int:
    """区间并集的总长度（区间可能重叠，相邻镜头的绑定实测会重叠）。"""
    total = 0
    end = -1
    for start, stop in sorted(spans):
        if stop <= end:
            continue
        total += stop - max(start, end)
        end = stop
    return total


def storyboard_source_coverage_gap(conn, episode_id: str) -> str | None:
    """本集镜头绑定没覆盖到的原文字数；没有缺口或不适用时返回 ``None``。"""
    import json

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
    for chapter_idx in chapter_indexes:
        length_row = conn.execute(
            "SELECT LENGTH(content) FROM chapters WHERE project_id=? AND idx=?",
            (row["project_id"], chapter_idx),
        ).fetchone()
        length = int((length_row or [0])[0] or 0)
        source_total += length
        spans = [
            (int(b["start_offset"] or 0), int(b["end_offset"] or 0))
            for b in bindings
            if int(b["chapter_idx"] or 0) == chapter_idx
        ]
        uncovered += max(0, length - _merged_length(spans))
    if uncovered <= 0:
        return None
    return (
        f"分镜没有覆盖整集原文：{source_total} 字里有 {uncovered} 字"
        f"（{uncovered * 100 // max(1, source_total)}%）没有任何镜头对应，"
        "按现在这批镜头生成出来的成片会漏掉这部分剧情；请回分镜台重做本集分镜。"
    )
