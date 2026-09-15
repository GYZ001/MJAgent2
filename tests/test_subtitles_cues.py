"""app.subtitles.cues 的表驱动测试：切分/合并/拆分/倍速换算/重叠消解。"""
from __future__ import annotations

import pytest

from app.subtitles.align import AsrToken, LineSpec, align_shot
from app.subtitles.cues import GAP_S, MIN_CUE_S, build_cues


def _toks(pairs: list[tuple[str, float]]) -> list[AsrToken]:
    return [AsrToken(text=text, start_s=start) for text, start in pairs]


def _align_one_line(text: str, tokens: list[AsrToken]):
    line = LineSpec(utterance_id="U01", text=text)
    return align_shot([line], tokens)


# ---------------------------------------------------------------------------
# 真实夹具：只验证「能出 cue、覆盖全部台词、时间在有效时长内」这类结构性质，
# 具体逐字时间已在 test_subtitles_align.py 里断言过。
# ---------------------------------------------------------------------------

def test_real_fixture_produces_non_overlapping_cues_within_bounds():
    import json
    from pathlib import Path

    data = json.loads((Path(__file__).parent / "fixtures" / "subtitles" / "asr_shot7.json").read_text())
    line = LineSpec(utterance_id=data["lines"][0]["utterance_id"], text=data["lines"][0]["text"])
    tokens = _toks([(t[0], t[1]) for t in data["tokens"]])
    alignment = align_shot([line], tokens)
    cues = build_cues(7, alignment, rate=1.0, effective_duration_s=15.0, max_chars_per_line=14)
    assert len(cues) > 1
    assert all("\\N" not in c.text and len(c.text) <= 14 for c in cues)
    assert "".join(c.text for c in cues).count("，") >= 1
    for a, b in zip(cues, cues[1:]):
        assert b.start_s >= a.end_s + GAP_S - 1e-9
    for c in cues:
        assert 0.0 <= c.start_s < c.end_s <= 15.0


# ---------------------------------------------------------------------------
# 只对 aligned 出 cue
# ---------------------------------------------------------------------------

def test_only_aligned_lines_produce_cues():
    spoken = LineSpec(utterance_id="U01", text="师父今天要出门")
    silent = LineSpec(utterance_id="U02", text="外面风雨欲来")
    tokens = _toks([(c, i * 0.3) for i, c in enumerate(spoken.text)])
    alignment = align_shot([spoken, silent], tokens)
    cues = build_cues(1, alignment, rate=1.0, effective_duration_s=100.0, max_chars_per_line=14)
    assert {c.utterance_id for c in cues} == {"U01"}


# ---------------------------------------------------------------------------
# 合并：间隔 < MERGE_GAP_S 且合并后 <= max_chars 才合并
# ---------------------------------------------------------------------------

def test_clauses_merge_when_gap_small():
    alignment = _align_one_line("甲乙，丙丁", _toks([("甲", 0.0), ("乙", 0.2), ("丙", 0.6), ("丁", 0.8)]))
    cues = build_cues(1, alignment, rate=1.0, effective_duration_s=100.0, max_chars_per_line=14)
    assert len(cues) == 1
    assert cues[0].text == "甲乙，丙丁"


def test_clauses_do_not_merge_when_gap_exceeds_merge_gap_s():
    alignment = _align_one_line("甲乙，丙丁", _toks([("甲", 0.0), ("乙", 0.2), ("丙", 5.0), ("丁", 5.2)]))
    cues = build_cues(1, alignment, rate=1.0, effective_duration_s=100.0, max_chars_per_line=14)
    assert len(cues) == 2
    assert [c.text for c in cues] == ["甲乙，", "丙丁"]


def test_clauses_do_not_merge_when_combined_exceeds_max_chars():
    text = "甲乙丙丁戊，己庚辛壬癸"  # 两个小句各 5/6 字，合并后 11 字 > max_chars=8
    alignment = _align_one_line(text, _toks([(c, i * 0.1) for i, c in enumerate(text.replace("，", ""))]))
    cues = build_cues(1, alignment, rate=1.0, effective_duration_s=100.0, max_chars_per_line=8)
    assert len(cues) == 2


# ---------------------------------------------------------------------------
# 超长拆分（2026-09-15 起不再折两行插 \N：B 端到端实测无标点长小句按中点折行
# 会把「赵某」「别人的」这类词从中间拆断，观感不可接受；改成每条 cue 恒为单行、
# <= max_chars，超长的按逐字时间最大停顿拆成多条顺序 cue）
# ---------------------------------------------------------------------------

_LONG_PLAIN_TEXT = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未"  # 18 字无标点


def test_every_cue_is_single_line_and_within_max_chars():
    """任何输出 cue 文本都不含 \\N；长度恒 <= max_chars_per_line。"""
    alignment = _align_one_line(_LONG_PLAIN_TEXT, _toks([(c, i * 0.2) for i, c in enumerate(_LONG_PLAIN_TEXT)]))
    cues = build_cues(1, alignment, rate=1.0, effective_duration_s=100.0, max_chars_per_line=14)
    for c in cues:
        assert "\\N" not in c.text
        assert len(c.text) <= 14
    assert "".join(c.text for c in cues) == _LONG_PLAIN_TEXT


def test_long_clause_splits_at_largest_pause_not_midpoint():
    """18 字无标点小句，第 8 字（0-indexed 索引 7）之后有 0.6s 停顿，其余均匀
    0.2s 间隔——应在停顿处拆成 8+10 两条单行 cue，而不是按字数中点对半拆。
    拆点 8 落在中间三分之一候选窗口 [ceil(18/3), floor(2*18/3)] = [6,12] 内，
    这条用例在加入「候选先限制在中间三分之一」规则后应继续通过。"""
    times = []
    t = 0.0
    for i in range(len(_LONG_PLAIN_TEXT)):
        times.append(t)
        t += 0.6 if i == 7 else 0.2
    alignment = _align_one_line(_LONG_PLAIN_TEXT, _toks(list(zip(_LONG_PLAIN_TEXT, times))))
    cues = build_cues(1, alignment, rate=1.0, effective_duration_s=100.0, max_chars_per_line=14)
    assert [c.text for c in cues] == ["甲乙丙丁戊己庚辛", "壬癸子丑寅卯辰巳午未"]
    assert [len(c.text) for c in cues] == [8, 10]
    # 拆点确实落在停顿处：第一条末字（辛，索引7）与第二条首字（壬，索引8）间隔 0.6s
    assert cues[1].text[0] == "壬"
    assert cues[0].text[-1] == "辛"


def test_long_clause_split_ignores_pause_outside_middle_third():
    """回归点：「甚 | 至引起了其他三大宗门的干扰，」这类偏到句首/句尾的拆分——
    15 字无标点小句，最大停顿（0.6s）刻意放在第 1/2 字之间（窗口外的局部噪声，
    对应真实数据里「甚」到「至」偶然比中段更大的那次停顿），次大停顿（0.3s）
    放在第 8/9 字之间（中间三分之一候选窗口 [5,10] 内）。拆点必须落在候选窗口
    内选中次大停顿，不能被窗口外偶然更大的噪声牵着走去在第 1 字后拆分。"""
    text = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰"  # 15 字无标点
    times, t = [], 0.0
    for i in range(len(text)):
        times.append(t)
        if i == 0:
            t += 0.6  # 窗口外的全局最大停顿（第1/2字之间）
        elif i == 7:
            t += 0.3  # 窗口 [5,10] 内的次大停顿（第8/9字之间）
        else:
            t += 0.1
    alignment = _align_one_line(text, _toks(list(zip(text, times))))
    cues = build_cues(1, alignment, rate=1.0, effective_duration_s=100.0, max_chars_per_line=14)
    assert [c.text for c in cues] == ["甲乙丙丁戊己庚辛", "壬癸子丑寅卯辰"]
    assert [len(c.text) for c in cues] == [8, 7]
    assert cues[0].text[-1] == "辛" and cues[1].text[0] == "壬"  # 拆点在第 8/9 字之间，不在第 1 字后


def test_long_clause_falls_back_to_midpoint_when_all_gaps_equal():
    """逐字间隔全相等（没有任何真实停顿信号）时退回中点，不假装某处更有依据。"""
    alignment = _align_one_line(_LONG_PLAIN_TEXT, _toks([(c, i * 0.2) for i, c in enumerate(_LONG_PLAIN_TEXT)]))
    cues = build_cues(1, alignment, rate=1.0, effective_duration_s=100.0, max_chars_per_line=14)
    assert [len(c.text) for c in cues] == [9, 9]
    assert "".join(c.text for c in cues) == _LONG_PLAIN_TEXT


def test_split_recurses_for_more_than_two_pieces():
    """span 远超 max_chars 时递归拆成 3+ 条，每条仍 <= max_chars。"""
    text = "甲乙丙丁戊己庚辛壬癸子丑"  # 12 字无标点，max_chars=5 时至少需要 3 条
    alignment = _align_one_line(text, _toks([(c, i * 0.2) for i, c in enumerate(text)]))
    cues = build_cues(1, alignment, rate=1.0, effective_duration_s=100.0, max_chars_per_line=5)
    assert len(cues) >= 3
    assert "".join(c.text for c in cues) == text
    for c in cues:
        assert len(c.text) <= 5


def test_trailing_punctuation_run_fully_stripped():
    """「效果……」现在必须把整串省略号都去掉，不能只去一个点变成「效果…」。"""
    line = LineSpec(utterance_id="U01", text="效果……")
    tokens = _toks([("效", 0.0), ("果", 0.2)])
    alignment = align_shot([line], tokens)
    cues = build_cues(1, alignment, rate=1.0, effective_duration_s=100.0, max_chars_per_line=14)
    assert len(cues) == 1
    assert cues[0].text == "效果"


def test_no_backslash_n_in_previously_problematic_real_clauses():
    """B 端到端实测报告的两个具体坏例：「你将是第一个死在赵某三层妖化术下之人」
    与「就等于是用别人的灵石来买丹药」，现在都不应再出现 \\N。"""
    for text in ["你将是第一个死在赵某三层妖化术下之人，", "就等于是用别人的灵石来买丹药，"]:
        plain = text.replace("，", "")
        alignment = _align_one_line(plain, _toks([(c, i * 0.2) for i, c in enumerate(plain)]))
        cues = build_cues(1, alignment, rate=1.0, effective_duration_s=100.0, max_chars_per_line=14)
        for c in cues:
            assert "\\N" not in c.text
            assert len(c.text) <= 14


# ---------------------------------------------------------------------------
# 倍速换算
# ---------------------------------------------------------------------------

def test_rate_scaling_divides_times_by_rate():
    # 首字原始时间 10.0s；LEAD_S=0.10 是播放时间轴常量、不随 rate 缩放，
    # 其余（首字时间本身）按 rate 换算：start = raw/rate - LEAD_S。
    alignment = _align_one_line("甲乙", _toks([("甲", 10.0), ("乙", 10.2)]))
    cue_1x = build_cues(1, alignment, rate=1.0, effective_duration_s=100.0, max_chars_per_line=14)[0]
    cue_2x = build_cues(1, alignment, rate=2.0, effective_duration_s=100.0, max_chars_per_line=14)[0]
    assert cue_1x.start_s == pytest.approx(10.0 - 0.10, abs=1e-6)
    assert cue_2x.start_s == pytest.approx(10.0 / 2 - 0.10, abs=1e-6)


# ---------------------------------------------------------------------------
# 最短时长与裁剪
# ---------------------------------------------------------------------------

def test_short_cue_extends_to_min_cue_s_when_no_next_cue():
    alignment = _align_one_line("甲乙", _toks([("甲", 5.0), ("乙", 5.05)]))
    cues = build_cues(1, alignment, rate=1.0, effective_duration_s=100.0, max_chars_per_line=14)
    assert len(cues) == 1
    assert cues[0].end_s - cues[0].start_s == pytest.approx(MIN_CUE_S, abs=1e-6)


def test_cue_beyond_effective_duration_is_dropped():
    alignment = _align_one_line("甲乙，丙丁", _toks([("甲", 0.0), ("乙", 0.2), ("丙", 9.0), ("丁", 9.2)]))
    cues = build_cues(1, alignment, rate=1.0, effective_duration_s=5.0, max_chars_per_line=14)
    assert len(cues) == 1
    assert cues[0].text == "甲乙，"
    assert cues[0].end_s <= 5.0
