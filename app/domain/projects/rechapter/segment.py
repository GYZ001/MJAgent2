"""把非冻结章节切成「最大连续段」，对每段用修好的切章器重切。

只调用 ``app.ingest`` 的 ``_find_heading_matches``（找段内第一个标题，定位要
剔除的残片前缀）与 ``_split_chapters_with_removed``（对剔除残片之后的文本
重切）——不复刻切章算法本身，``app/novel/structure.py``/``app/ingest.py``
已定稿，这里不改也不重新编排它们内部的拆分/合并/去重顺序。
"""
from __future__ import annotations

import re

from app.ingest import _find_heading_matches, _split_chapters_with_removed

from app.domain.projects.rechapter.conservation import check_conservation
from app.domain.projects.rechapter.models import ChapterRow, ConservationResult, SegmentPlan

#: 段首残留文本超过这个字数就不是"上一章末行残片"，是实质内容——拒绝重切
#: 这一段，保持原样并报告，不猜（CLAUDE.md「不得兜底填充」）。
_RESIDUAL_PREFIX_MAX_CHARS = 200

#: 旧标题取最后一个「第N章」序号——与 ``app.ingest._chapter_ordinal`` 的语义
#: 一致，但只认阿拉伯数字：实测样本（魂穿刘关张、刚准备高考）标题序号全部是
#: 阿拉伯数字。两侧都解析不出序号时不直接判失败——「楔子」「序章」这类无
#: 序号标题是正面定义里本就承认的合法形态（见 ``app.novel.structure``
#: ``_CHAPTER_CORE``），不能让它们被这条校验误伤；退化为按标题完全相等比较
#: （见 ``_chapters_match``）。一侧有序号一侧没有，仍然是真实的不一致。
_ORDINAL_RE = re.compile(r"第(\d+)章")


def _last_ordinal(title: str) -> int | None:
    matches = _ORDINAL_RE.findall(title or "")
    return int(matches[-1]) if matches else None


def maximal_segments(all_idx: list[int], frozen_idx: set[int]) -> list[tuple[int, int]]:
    """把全部章节 idx 切成"非冻结章节最大连续段"的 (start, end) 闭区间列表。

    数值不连续（idx 本身有空洞，比如历史手删留下的缺口）也要切断——调用方
    按 ``range(start, end+1)`` 取段内文本，空洞处没有对应的 ``chapters`` 行，
    误把它当一个连续段会直接 KeyError。"""
    segments: list[tuple[int, int]] = []
    start: int | None = None
    prev: int | None = None
    for idx in all_idx:
        if idx in frozen_idx:
            if start is not None:
                segments.append((start, prev))
                start = None
        else:
            if start is not None and idx != prev + 1:
                segments.append((start, prev))
                start = None
            if start is None:
                start = idx
            prev = idx
    if start is not None:
        segments.append((start, prev))
    return segments


def _segment_text(chapters_by_idx: dict[int, ChapterRow], start: int, end: int) -> str:
    return "\n".join(chapters_by_idx[i].content for i in range(start, end + 1))


def _strip_leading_residual(text: str) -> tuple[str, str, bool]:
    """返回 (残片前缀, 从第一个标题起的文本, 是否找到标题)。"""
    matches = _find_heading_matches(text)
    if not matches:
        return "", text, False
    first_start = matches[0][0]
    return text[:first_start], text[first_start:], True


def _chapters_match(old_title: str, new_title: str) -> bool:
    """两侧都能解析出"第N章"序号——序号相等才算通过；两侧都没有序号（如
    "楔子"）——退化为 strip 后标题完全相等；一侧有序号一侧没有——不一致，
    不管标题是否碰巧相等，都判不通过（结构本身已经变了，不能靠文本相等
    掩盖）。"""
    old_ord, new_ord = _last_ordinal(old_title), _last_ordinal(new_title)
    if old_ord is not None and new_ord is not None:
        return old_ord == new_ord
    if old_ord is None and new_ord is None:
        return old_title.strip() == new_title.strip()
    return False


def _stability_ok(old_chapters: list[ChapterRow], new_chapters: list[dict]) -> bool:
    """数量必须相等，且逐章按位置用 ``_chapters_match`` 比较。"""
    if len(old_chapters) != len(new_chapters):
        return False
    return all(
        _chapters_match(old.title, str(new.get("title") or ""))
        for old, new in zip(old_chapters, new_chapters)
    )


def _prefix_and_remainder(
    segment_text: str, *, starts_at_beginning: bool,
) -> tuple[str, str, str | None]:
    """返回 (喂给切章器的文本, 剔除的残片前缀, 拒绝原因)——拒绝原因非空时
    前两者无效，调用方应直接拒绝整段。"""
    if starts_at_beginning:
        return segment_text, "", None
    prefix, remainder, found = _strip_leading_residual(segment_text)
    if not found:
        return "", "", "段内未找到任何章节标题，保持原样"
    if len(prefix) > _RESIDUAL_PREFIX_MAX_CHARS:
        return "", "", (
            f"段首残留文本 {len(prefix)} 字超过 {_RESIDUAL_PREFIX_MAX_CHARS} 字上限，"
            "疑似实质内容而非上一章残片，保持原样"
        )
    return remainder, prefix, None


def _validate_resplit(
    process_text: str, old_chapters: list[ChapterRow], new_chapters: list[dict], *, is_tail: bool,
) -> tuple[str, ConservationResult]:
    """重切结果的两道校验：先守恒（任何丢字都拒绝，原文不可逆，不能靠章数/
    序号对比侥幸放过），再——仅非尾段——序号稳定性。返回 (拒绝原因或空串,
    守恒校验结果)。"""
    conservation = check_conservation(process_text, new_chapters)
    if conservation.lost_chars:
        samples = "；".join(conservation.lost_samples)
        return (
            f"重切丢失 {conservation.lost_chars} 字，原文不可逆，拒绝重切、保持原样。"
            f"丢失片段：{samples}"
        ), conservation
    if not is_tail and not _stability_ok(old_chapters, new_chapters):
        return (
            f"重切后章数 {len(new_chapters)}（原 {len(old_chapters)}）或序号与原章不一致，"
            "之后还跟着冻结章节，无法安全重新编号，保持原样"
        ), conservation
    return "", conservation


def plan_one_segment(
    chapters_by_idx: dict[int, ChapterRow], start: int, end: int, *, is_tail: bool,
) -> SegmentPlan:
    """规划单个非冻结段：决定怎么切、切完要不要接受、不接受时给出原因。"""
    old_chapters = [chapters_by_idx[i] for i in range(start, end + 1)]
    starts_at_beginning = start == 1
    base = dict(
        start_idx=start, end_idx=end, is_tail=is_tail,
        starts_at_beginning=starts_at_beginning, old_chapters=old_chapters,
    )
    segment_text = _segment_text(chapters_by_idx, start, end)
    process_text, excluded_prefix, reject_reason = _prefix_and_remainder(
        segment_text, starts_at_beginning=starts_at_beginning,
    )
    if reject_reason:
        return SegmentPlan(accepted=False, reason=reject_reason, **base)
    new_chapters, _removed = _split_chapters_with_removed(process_text)
    if not new_chapters:
        return SegmentPlan(accepted=False, reason="重切结果为空，保持原样", **base)
    reject_reason, conservation = _validate_resplit(
        process_text, old_chapters, new_chapters, is_tail=is_tail,
    )
    if reject_reason:
        return SegmentPlan(
            accepted=False, reason=reject_reason, lost_samples=conservation.lost_samples, **base,
        )
    return SegmentPlan(
        accepted=True, reason="", new_chapters=new_chapters, excluded_prefix=excluded_prefix,
        gained_chars=conservation.gained_chars, **base,
    )
