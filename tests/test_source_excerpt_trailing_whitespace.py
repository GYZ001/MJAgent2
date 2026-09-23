"""Regression tests for index_source_segments dropping trailing paragraphs.

Bug: the paragraph-splitting regex's end-of-string alternative was bare
``\\Z``, which can only align right after a non-whitespace character (the
pattern always ends on a mandatory ``\\S``). Any source text ending in
anything short of a full blank-line separator -- a single trailing
newline, bare trailing spaces, no trailing separator at all -- made the
last paragraph structurally unmatchable, so it silently vanished:
``"hello\\n"`` indexed to ``[]`` and a 3-paragraph chapter ending in one
``\\n`` only produced 2 segments. Fixed by widening the alternative to
``\\s*\\Z`` (see app.source_excerpt.index_source_segments's docstring for
the full mechanics and why this is provably a no-op for text that does
NOT end in whitespace).

This file only tests the paragraph-splitting behavior itself. Unrelated
prior-existing behavior (chunking by max_chars, cross-paragraph quote
merging as a mechanism, single-character-paragraph merging into its
neighbor) is covered elsewhere / documented in the function's own
docstring and is deliberately not re-litigated here except where needed
to prove the fix doesn't disturb it.
"""
from __future__ import annotations

import re

import pytest

from app.source_excerpt import SourceSegment, index_source_segments, unclosed_quotation

# 独立观察点：修复前的正则字面量，原样手抄（不是从 app.source_excerpt import
# 出来再改），确保这条比对不会因为将来两边被一起改动而失去意义。
_PRE_FIX_PARAGRAPH_RE = re.compile(r"\S(?:.*?\S)?(?=\n\s*\n|\Z)", flags=re.S)


def _pre_fix_index_source_segments(source: str) -> list[SourceSegment]:
    """复刻 index_source_segments 在 max_chars 分片之前的两步（找段落、跨段引号
    合并），只把段落正则换回修复前的版本。本文件里的用例都远短于默认
    max_chars=900，分片那一步对两边都是恒等操作，因此不需要复刻。"""
    raw = source or ""
    if not raw.strip():
        return []
    spans: list[tuple[int, int]] = []
    for match in _PRE_FIX_PARAGRAPH_RE.finditer(raw):
        start, end = match.span()
        text = raw[start:end].strip()
        if not text:
            continue
        left = raw.find(text, start, end)
        spans.append((left, left + len(text)))

    if spans:
        merged_spans: list[tuple[int, int]] = []
        index = 0
        while index < len(spans):
            start, end = spans[index]
            while (
                unclosed_quotation(raw, start=start, end=end) is not None
                and index + 1 < len(spans)
            ):
                index += 1
                end = spans[index][1]
            merged_spans.append((start, end))
            index += 1
        spans = merged_spans

    return [
        SourceSegment(
            segment_id=f"SRC{index:04d}",
            text=raw[start:end].strip(),
            start_offset=start,
            end_offset=end,
        )
        for index, (start, end) in enumerate(spans, start=1)
        if raw[start:end].strip()
    ]


_PARA_1 = "第一段落，讲述今天的天气和心情，窗外的雨还没有停。"
_PARA_2 = "第二段落，主角走进屋子，环顾四周，把伞放在门边晾干。"
_PARA_3 = "第三段落，故事在此收尾，灯光渐暗，只剩钟表的滴答声。"

_TRAILING_WHITESPACE_VARIANTS = [
    pytest.param("", id="no-trailing-ws"),
    pytest.param("\n", id="single-newline"),
    pytest.param("\n\n", id="double-newline"),
    pytest.param("   ", id="trailing-spaces"),
    pytest.param(" \n ", id="space-newline-space"),
]


@pytest.mark.parametrize("suffix", _TRAILING_WHITESPACE_VARIANTS)
def test_single_paragraph_survives_any_trailing_whitespace(suffix: str) -> None:
    segments = index_source_segments(_PARA_1 + suffix)
    assert [s.text for s in segments] == [_PARA_1]
    assert [s.segment_id for s in segments] == ["SRC0001"]


@pytest.mark.parametrize("suffix", _TRAILING_WHITESPACE_VARIANTS)
def test_multi_paragraph_survives_any_trailing_whitespace(suffix: str) -> None:
    source = "\n\n".join([_PARA_1, _PARA_2, _PARA_3]) + suffix
    segments = index_source_segments(source)
    assert [s.text for s in segments] == [_PARA_1, _PARA_2, _PARA_3]
    assert [s.segment_id for s in segments] == ["SRC0001", "SRC0002", "SRC0003"]


def test_run_regression_hello_newline_no_longer_returns_empty() -> None:
    """主会话实测的最小复现：`"hello\\n"` 修复前索引出 []。"""
    segments = index_source_segments("hello\n")
    assert [s.text for s in segments] == ["hello"]


def test_run_regression_third_of_three_paragraphs_no_longer_dropped() -> None:
    """主会话实测：三段文本尾部只有一个换行时，修复前只索引出前两段。"""
    source = "第一段内容。\n\n第二段内容。\n\n第三段内容。\n"
    segments = index_source_segments(source)
    assert [s.text for s in segments] == ["第一段内容。", "第二段内容。", "第三段内容。"]


def test_single_char_paragraph_still_merges_into_next_by_design() -> None:
    """已知怪癖，故意不修：修了会挪动存量产物引用的 segment_id/偏移量
    （见 index_source_segments 的 docstring）。这条用例把当前行为钉住，
    避免将来有人在不知情的情况下把它当 bug 改掉。"""
    segments = index_source_segments("a\n\nb")
    assert [s.text for s in segments] == ["a\n\nb"]


def test_fix_matches_pre_fix_regex_byte_for_byte_when_source_has_no_trailing_ws() -> None:
    """对比用例：4 段真实风格的中文（含跨段引号、含对话），source 本身不以
    空白结尾。断言修复后的实现与手抄的修复前正则重算完全一致——证明修复
    对"不以空白结尾"的既有输入是逐字节恒等操作，不只是靠推理。"""
    block1 = "夜里下起小雨，谷言站在窗前，看着楼下的车流发呆，一时没有说话。"
    block2 = "曲惜靠在门框上，压低声音说：“这件事牵涉太广，"
    block3 = "我们谁都不能先声张，等消息核实了再说。”她说完转身离开，脚步很轻。"
    block4 = "谷言独自留在原地，把刚才的话在心里又想了一遍，觉得还是不踏实。"
    source = "\n\n".join([block1, block2, block3, block4])
    assert not source[-1].isspace()  # 确保这条用例真的落在"不以空白结尾"这一支

    fixed = index_source_segments(source)
    pre_fix = _pre_fix_index_source_segments(source)

    def _shape(segments: list[SourceSegment]) -> list[tuple[str, str, int, int]]:
        return [(s.segment_id, s.text, s.start_offset, s.end_offset) for s in segments]

    assert _shape(fixed) == _shape(pre_fix)

    # 且这条用例真的触发了跨段引号合并（block2/block3 因未闭合的"“”"并成一段），
    # 不是巧合地各自独立——否则上面的相等断言测不出这条比对声称要测的东西。
    assert len(fixed) == 3
    assert fixed[1].text == block2 + "\n\n" + block3
