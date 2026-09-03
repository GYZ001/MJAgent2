"""外观锚点里的画风泄漏清理（WS4，2026-09-03）。

真实事故（B 库实测，proj_ce9fcf749b23/proj_ecabd38b7261/proj_a5d711b0a337，
共 12 个人物）：`Character.appearance_canonical` 里混进了
`world.visual_style_canonical` 的片段（如「符合国漫3D电影质感」「国漫3D动画质感」），
不是逐字复制整段画风串，而是模型改写/倒序/省略连接词之后的近似片段——哈维
「国漫3D电影质感下，中年男性形象…」、莱曼「…光影精致的国漫3D动画电影质感」。
画风只应存在于 world.visual_style_canonical 一处；泄漏进外观字段后，换画风
重生定妆照时身份描述会继续带着旧画风走，且和「哈维 2009 年 29 岁被写成中年」
一样，都是把不该属于这个字段的信息焊死进去。

判据是字符串包含（画风是这个项目自己的已知常量，不是枚举猜测的词表，符合
CLAUDE.md「禁止黑白名单」——被剥离的片段逐一可追溯到 visual_style 本身的某个
分句）：把 `visual_style` 按标点切成自然分句，对 `appearance` 的每个分句分别
和这些画风分句做 `difflib.SequenceMatcher` 匹配；一个分句里若单次匹配长度
≥6 字、或匹配总字数占该分句 ≥40%，判定这段是画风描述而不是外观描述，整体
剥离（相邻的小段落先合并成一段，避免留下不成词的碎片），再吸掉边界残留的
纯结构助词（的/地/得/下/中/后/之/为/是/呈/符合）。样本核验见
``tests/test_appearance_style_leak.py``：12 个真实泄漏样本剥离后既不再含
画风片段、也仍是 20~80 字的通顺短句；2 个真实无泄漏样本原样不变（无假阳性）。
"""
from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

_CLAUSE_DELIMITERS = "，,、；;：:。."
_STRUCTURAL_LEAD_PARTICLES = ("的", "地", "得", "下", "中", "后", "之", "为", "是", "呈", "符合")
_MIN_MERGED_RUN_CHARS = 6   # 与 CLAUDE.md 派单原文「≥6 字的连续片段」同一个数字
_MIN_CLAUSE_COVERAGE = 0.4  # 短分句里即使单段匹配不到 6 字，占比过半也判定为泄漏
_MIN_SURVIVING_CLAUSE_CHARS = 3  # 剥离后剩余不足 3 字视为纯连接词残留，整段丢弃
_BLOCK_MERGE_GAP = 2


def _split(text: str) -> list[str]:
    parts = re.split(f"[{re.escape(_CLAUSE_DELIMITERS)}]", text)
    return [p for p in parts if p]


def _style_segments(visual_style: str) -> list[str]:
    """画风串按自身标点切出的自然分句——判据的比对对象只来自这份数据本身。"""
    segments = [seg for seg in _split(visual_style) if len(seg) >= 3]
    return segments or ([visual_style] if visual_style else [])


def _spans_against_style(clause: str, style_segments: list[str]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for segment in style_segments:
        matcher = SequenceMatcher(None, clause, segment, autojunk=False)
        for block in matcher.get_matching_blocks():
            if block.size >= 2:
                spans.append((block.a, block.a + block.size))
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start - merged[-1][1] <= _BLOCK_MERGE_GAP:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def _strip_particles(text: str) -> str:
    stripped = text
    changed = True
    while changed and stripped:
        changed = False
        for particle in _STRUCTURAL_LEAD_PARTICLES:
            if stripped.startswith(particle):
                stripped = stripped[len(particle):]
                changed = True
            if stripped.endswith(particle):
                stripped = stripped[: -len(particle)]
                changed = True
    return stripped


def _clean_clause(clause: str, style_segments: list[str]) -> str | None:
    """单个分句：命中画风就剥离并清理边界连接词；整句都是画风描述返回 None。"""
    spans = _spans_against_style(clause, style_segments)
    if not spans:
        return clause
    total_matched = sum(end - start for start, end in spans)
    biggest = max(end - start for start, end in spans)
    coverage = total_matched / len(clause) if clause else 0.0
    if biggest < _MIN_MERGED_RUN_CHARS and coverage < _MIN_CLAUSE_COVERAGE:
        return clause
    remaining = clause
    for start, end in sorted(spans, key=lambda span: -span[0]):
        remaining = remaining[:start] + remaining[end:]
    remaining = _strip_particles(remaining.strip())
    return remaining if len(remaining) >= _MIN_SURVIVING_CLAUSE_CHARS else None


def strip_visual_style_leak(appearance: str, visual_style: str) -> tuple[str, list[str]]:
    """剥离 ``appearance`` 里混入的 ``visual_style`` 片段，返回 (清理后文本, 被剥离的原始分句列表)。

    画风为空、外观为空、或清理后会把全部分句都清空（说明判据本身可能失灵，
    宁可保留原文也不能让外观锚点整体消失）时原样返回，不强行清空。剥离发生
    时记一条 warning，供排障时定位是哪个角色、哪段文本被改写过。
    """
    text = (appearance or "").strip()
    style = (visual_style or "").strip()
    if not text or not style:
        return text, []
    segments = _style_segments(style)
    kept: list[str] = []
    dropped: list[str] = []
    for raw_clause in _split(text):
        clause = raw_clause.strip()
        cleaned = _clean_clause(clause, segments)
        if cleaned is None:
            dropped.append(clause)
        elif cleaned != clause:
            dropped.append(clause)
            kept.append(cleaned)
        else:
            kept.append(clause)
    if not kept:
        return text, []
    result = "，".join(kept)
    if dropped:
        logger.warning("外观锚点剥离画风片段 %r：%r -> %r", dropped, appearance, result)
    return result, dropped
