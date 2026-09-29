"""app.subtitles.align 的表驱动测试：真实夹具 + 合成场景。

覆盖点见派单：真实命中率/窗口、整句未念、两句其中一句未念、短句全命中/半命中/
非连续命中、多余语音、无音轨、同音字命中（含单调性）、JSON 往返。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.subtitles.align import (
    MIN_MATCH_RATIO,
    AsrToken,
    LineSpec,
    align_shot,
    alignment_from_dict,
    alignment_to_dict,
    normalize_chars,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "subtitles"


def _toks(pairs: list[tuple[str, float]]) -> list[AsrToken]:
    return [AsrToken(text=text, start_s=start) for text, start in pairs]


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 真实夹具：B 现网 Seedance 真实音轨的 SenseVoice 输出
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "fixture_name, expect_matched, expect_total, expect_exact, expect_start, expect_end_min, expect_end_max",
    [
        ("asr_shot7.json", 52, 52, 51, 0.42, 11.5, 12.0),
        ("asr_shot10.json", 42, 43, 42, 0.24, 7.1, 7.6),
    ],
)
def test_real_fixture_alignment(
    fixture_name, expect_matched, expect_total, expect_exact, expect_start, expect_end_min, expect_end_max
):
    data = _load_fixture(fixture_name)
    lines = [
        LineSpec(
            utterance_id=item["utterance_id"], text=item["text"],
            speaker=item.get("speaker", ""), delivery_kind=item.get("delivery_kind", ""),
        )
        for item in data["lines"]
    ]
    tokens = _toks([(t[0], t[1]) for t in data["tokens"]])
    result = align_shot(lines, tokens)
    assert len(result.lines) == 1
    line = result.lines[0]
    assert line.status == "aligned"
    assert line.reason == ""
    assert line.matched_chars == expect_matched
    assert line.total_chars == expect_total
    assert line.exact_chars == expect_exact
    assert line.start_s == pytest.approx(expect_start, abs=0.01)
    assert expect_end_min <= line.end_s <= expect_end_max
    assert result.extra_speech == ()
    # 逐字时间单调不减（tail 插值不应制造时间倒流）
    times = [ct.start_s for ct in line.char_times]
    assert times == sorted(times)


def test_real_fixture_round_trip_json():
    data = _load_fixture("asr_shot7.json")
    lines = [LineSpec(utterance_id=data["lines"][0]["utterance_id"], text=data["lines"][0]["text"])]
    tokens = _toks([(t[0], t[1]) for t in data["tokens"]])
    result = align_shot(lines, tokens)
    payload = json.loads(json.dumps(alignment_to_dict(result)))
    restored = alignment_from_dict(payload)
    assert restored == result


# ---------------------------------------------------------------------------
# normalize_chars：去标点/空白/引号，保留汉字字母数字
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("是啊，当年", ["是", "啊", "当", "年"]),
        ("“你好”", ["你", "好"]),
        ("A1 b2　c3", ["A", "1", "b", "2", "c", "3"]),
        ("……", []),
        ("", []),
    ],
)
def test_normalize_chars(raw, expected):
    assert normalize_chars(raw) == expected


# ---------------------------------------------------------------------------
# 整句未念 / 两句其中一句未念
# ---------------------------------------------------------------------------

def test_whole_line_not_spoken_is_missing_not_found():
    line = LineSpec(utterance_id="U01", text="今天天气真的非常好")
    unrelated = _toks([(c, i * 0.3) for i, c in enumerate("完全不相关的另一段话内容")])
    result = align_shot([line], unrelated)
    la = result.lines[0]
    assert la.status == "missing"
    assert la.reason == "not_found"
    assert la.match_ratio < 0.3


def _global_pass_only(lines, tokens):
    """修复前逻辑的手写副本（红绿验证用，不回退线上代码）：只做一次全局单调
    匹配，没有局部窗口重试。逐字展开 + 拼音转换 + SequenceMatcher 一次性匹配，
    与 ``align.align_shot`` 2026-09-28 之前的实现等价。"""
    from difflib import SequenceMatcher as _SM

    from app.subtitles import align as _a

    asr_pairs = _a._expand_asr_chars(tokens)
    known_chars, spans = _a._known_chars_and_spans(lines)
    known_pinyin = _a._pinyin_units(known_chars)
    asr_pinyin = _a._pinyin_units([c for c, _t in asr_pairs])
    blocks = _SM(None, known_pinyin, asr_pinyin, autojunk=False).get_matching_blocks()
    hit_time, hit_exact, _asr_matched = _a._apply_matching_blocks(blocks, known_chars, asr_pairs)
    return tuple(_a._build_line_alignment(line, span, hit_time, hit_exact) for line, span in zip(lines, spans))


def test_interruption_lost_by_global_pass_recovered_by_local_retry():
    """插话被挤掉的真实机制复现：台账顺序是 [房东完整句][插话]，但实际语序是
    [房东前半][插话][房东后半]——已知文字里插话排在房东句子之后（两者互不
    相交），而插话的真实音轨却夹在房东句子中间；全局单调匹配的递归会把「房东
    前半」「房东后半」各自匹配掉，插话在已知序列里排在两者之后、可用的 ASR
    区间却是空的（结构性找不到），即使插话的音轨原样存在于残留字符里。"""
    landlord = LineSpec(utterance_id="U01", text="甲乙丙丁戊己庚辛")  # 台账记的是完整句
    interruption = LineSpec(utterance_id="U02", text="壬癸")  # 台账另起一行
    tokens = _toks([
        ("甲", 0.0), ("乙", 0.1), ("丙", 0.2), ("丁", 0.3),  # 房东前半（实际先说）
        ("壬", 0.4), ("癸", 0.5),  # 插话真实音轨（实际中间说）
        ("戊", 0.6), ("己", 0.7), ("庚", 0.8), ("辛", 0.9),  # 房东后半（实际接着说完）
    ])

    # 红：手写的修复前逻辑（只有全局单调匹配）——插话 0 命中，真被挤掉。
    before = _global_pass_only([landlord, interruption], tokens)
    assert before[0].status == "aligned"
    assert before[1].status == "missing" and before[1].matched_chars == 0

    # 绿：线上代码（含局部窗口重试）——插话从「没人认领」的残留音轨里找回真实时间戳。
    result = align_shot([landlord, interruption], tokens)
    assert result.lines[0].status == "aligned"
    interjection = result.lines[1]
    assert interjection.status == "aligned"
    assert interjection.matched_chars == 2
    assert interjection.char_times[0].start_s == pytest.approx(0.4)
    assert interjection.char_times[1].start_s == pytest.approx(0.5)
    # 插话的字符被局部重试认领后，不应该在 extra_speech 里被重复计一遍。
    assert result.extra_speech == ()


def _retry_shared_pool_before_fix(lines, alignments, asr_pairs, asr_matched):
    """修复前逻辑的手写副本（红绿验证用，不回退线上代码）：``residual_pairs``/
    ``residual_indices`` 只在循环外算一次、循环内从不收窄——同一镜头内多条待
    重试的行共享同一份不变的候选池，可能重复认领同一段残留音轨。与
    ``align._retry_unmatched_lines`` 2026-09-28 之前的实现等价。"""
    from app.subtitles import align as _a

    residual_indices = [i for i, matched in enumerate(asr_matched) if not matched]
    residual_pairs = [asr_pairs[i] for i in residual_indices]
    result = list(alignments)
    updated_matched = list(asr_matched)
    for i, la in enumerate(result):
        if la.status == "aligned":
            continue
        retried = _a._retry_line_in_window(lines[i], residual_pairs, residual_indices)
        if retried is None:
            continue
        candidate, claimed_indices = retried
        if candidate.matched_chars > la.matched_chars:
            result[i] = candidate
            for idx in claimed_indices:
                updated_matched[idx] = True
    return tuple(result), updated_matched


def test_two_interruptions_do_not_double_claim_same_residual_audio():
    """一镜内两条短插话若共享一份不收窄的候选池，会各自独立匹配到同一段残留
    音轨、都拿到相同的真实时间戳、都被判成 aligned——下游 cues.py 的重叠裁剪
    会让其中一条完全消失，而它的 status 仍是 aligned，没有任何可见信号
    （2026-09-28 实测复现）。修复后候选池按行逐次收窄，两条插话互斥分配。"""
    from difflib import SequenceMatcher as _SM

    from app.subtitles import align as _a

    landlord = LineSpec(utterance_id="U01", text="甲乙丙丁戊己庚辛")
    interruption_a = LineSpec(utterance_id="U02", text="那那")
    interruption_b = LineSpec(utterance_id="U03", text="那那")
    lines = [landlord, interruption_a, interruption_b]
    tokens = _toks([
        ("甲", 0.0), ("乙", 0.1), ("丙", 0.2),
        ("那", 0.4), ("那", 0.5),  # 两条插话唯一能用的一段残留音轨
        ("丁", 0.7), ("戊", 0.8), ("己", 0.9), ("庚", 1.0), ("辛", 1.1),
    ])
    asr_pairs = _a._expand_asr_chars(tokens)
    known_chars, spans = _a._known_chars_and_spans(lines)
    known_pinyin = _a._pinyin_units(known_chars)
    asr_pinyin = _a._pinyin_units([c for c, _t in asr_pairs])
    blocks = _SM(None, known_pinyin, asr_pinyin, autojunk=False).get_matching_blocks()
    hit_time, hit_exact, asr_matched = _a._apply_matching_blocks(blocks, known_chars, asr_pairs)
    pre_retry = tuple(_a._build_line_alignment(line, span, hit_time, hit_exact) for line, span in zip(lines, spans))

    # 红：手写的修复前逻辑——两条插话共享同一份不收窄的候选池，都判成 aligned，
    # 且用的是同一段残留音轨（时间戳完全相同）。
    before, _ = _retry_shared_pool_before_fix(lines, pre_retry, asr_pairs, asr_matched)
    assert before[1].status == "aligned" and before[2].status == "aligned"
    assert before[1].char_times[0].start_s == before[2].char_times[0].start_s

    # 绿：线上代码——候选池逐次收窄，同一段残留音轨只能被一条插话认领。
    result = align_shot(lines, tokens)
    claimants = [la for la in result.lines[1:] if la.status in ("aligned", "partial")]
    assert len(claimants) == 1


def test_one_of_two_lines_not_spoken():
    spoken = LineSpec(utterance_id="U01", text="师父今天要出门")
    silent = LineSpec(utterance_id="U02", text="外面风雨欲来")
    tokens = _toks([(c, i * 0.3) for i, c in enumerate(spoken.text)])
    result = align_shot([spoken, silent], tokens)
    assert result.lines[0].status == "aligned"
    assert result.lines[0].matched_chars == result.lines[0].total_chars
    assert result.lines[1].status == "missing"
    assert result.lines[1].reason == "not_found"
    assert result.lines[1].matched_chars == 0


# ---------------------------------------------------------------------------
# 短句规则：<=2 字须全命中；3 字须有长度>=2 的连续命中块
# ---------------------------------------------------------------------------

def test_short_line_full_match_aligned():
    line = LineSpec(utterance_id="U01", text="是啊")
    tokens = _toks([("是", 0.0), ("啊", 0.2)])
    result = align_shot([line], tokens)
    assert result.lines[0].status == "aligned"
    assert result.lines[0].matched_chars == 2


def test_short_line_partial_match_keeps_real_hit_timestamp():
    """2026-09-28：短句未达 aligned 阈值但确有真实命中时不再整行判 missing、
    丢弃时间戳——「是」这个字的真实命中时间必须保留在 char_times 里，供
    build_cues 产出 cue（不丢），只是整行判为置信度更低的 partial。"""
    line = LineSpec(utterance_id="U01", text="是啊")
    tokens = _toks([("是", 0.0), ("嗯", 0.2)])
    result = align_shot([line], tokens)
    la = result.lines[0]
    assert la.status == "partial"
    assert la.reason == "short_line_partial"
    assert la.matched_chars == 1
    assert la.char_times[0].matched is True and la.char_times[0].start_s == pytest.approx(0.0)


def test_two_char_line_zero_match_is_missing():
    """协调方样例：「你……你……」标点剥离后剩 2 字，完全没被念出。"""
    line = LineSpec(utterance_id="U01", text="你……你……")
    tokens = _toks([("走", 0.0), ("吧", 0.3)])
    result = align_shot([line], tokens)
    la = result.lines[0]
    assert la.total_chars == 2
    assert la.matched_chars == 0
    assert la.status == "missing"
    assert la.reason == "short_line_partial"


def test_three_char_line_contiguous_block_aligned():
    """协调方样例：「火蛇术」被识别成「我蛇术」，2/3 命中且连续 -> aligned。"""
    line = LineSpec(utterance_id="U01", text="火蛇术")
    tokens = _toks([("我", 0.0), ("蛇", 0.2), ("术", 0.4)])
    result = align_shot([line], tokens)
    la = result.lines[0]
    assert la.status == "aligned"
    assert la.reason == ""
    assert la.matched_chars == 2
    assert la.total_chars == 3


def test_three_char_line_noncontiguous_hits_are_partial():
    """首尾字命中但中间字没命中：2 字命中但不连续，不满足短句 aligned 规则，
    但仍是 partial（有真实命中，不是 missing）——2026-09-28 改判。"""
    line = LineSpec(utterance_id="U01", text="甲乙丙")
    tokens = _toks([("甲", 0.0), ("戊", 0.2), ("丙", 0.4)])
    result = align_shot([line], tokens)
    la = result.lines[0]
    assert la.status == "partial"
    assert la.reason == "short_line_partial"
    assert la.matched_chars == 2


# ---------------------------------------------------------------------------
# 多余语音 / 无音轨
# ---------------------------------------------------------------------------

def test_extra_speech_detected_for_long_unmatched_run():
    line = LineSpec(utterance_id="U01", text="你好")
    extra_text = "这是多余的语音内容"  # 9 字 >= EXTRA_SPEECH_MIN_CHARS
    tokens = _toks([("你", 0.0), ("好", 0.2)] + [(c, 0.4 + i * 0.1) for i, c in enumerate(extra_text)])
    result = align_shot([line], tokens)
    assert result.lines[0].status == "aligned"
    assert len(result.extra_speech) == 1
    extra = result.extra_speech[0]
    assert extra.text == extra_text
    assert extra.start_s == pytest.approx(0.4)
    assert extra.end_s > extra.start_s


def test_short_unmatched_run_is_not_extra_speech():
    line = LineSpec(utterance_id="U01", text="你好")
    tokens = _toks([("你", 0.0), ("好", 0.2), ("哦", 0.4), ("嗯", 0.5)])  # 只 2 个多余字，< 6
    result = align_shot([line], tokens)
    assert result.extra_speech == ()


def test_no_audio_all_missing_no_extra_speech():
    line = LineSpec(utterance_id="U01", text="随便什么内容")
    result = align_shot([line], [], has_audio=False)
    la = result.lines[0]
    assert la.status == "missing"
    assert la.reason == "no_audio"
    assert la.match_ratio == 0.0
    assert la.char_times == ()
    assert la.start_s is None and la.end_s is None
    assert result.extra_speech == ()


# ---------------------------------------------------------------------------
# 同音字等价（拼音匹配）与单调性
# ---------------------------------------------------------------------------

def test_homophone_full_match_but_not_exact():
    """「灵石」被识别成「零食」：拼音相同应全命中，但字面不同 exact 应低于 matched。"""
    line = LineSpec(utterance_id="U01", text="灵石")
    tokens = _toks([("零", 0.0), ("食", 0.2)])
    result = align_shot([line], tokens)
    la = result.lines[0]
    assert la.status == "aligned"
    assert la.match_ratio == 1.0
    assert la.matched_chars == 2
    assert la.exact_chars < la.matched_chars
    assert la.exact_chars == 0


def test_homophone_order_is_monotonic_not_commutative():
    """拼音顺序颠倒不应被当成命中：「灵石」vs 反序的「食零」只能命中一个字。"""
    line = LineSpec(utterance_id="U01", text="灵石")
    tokens = _toks([("食", 0.0), ("零", 0.2)])
    result = align_shot([line], tokens)
    la = result.lines[0]
    assert la.matched_chars == 1
    assert la.status == "partial"  # 2026-09-28 改判：短句 1 字真实命中不再判 missing
    assert la.reason == "short_line_partial"


@pytest.mark.parametrize(
    "script_char, asr_char",
    [("名", "鸣"), ("师", "熙")],
)
def test_known_homophone_pairs_from_real_data(script_char, asr_char):
    """PRD §0 实测的两个真实错字对：名/鸣同音应命中，师/熙不同音（对照组）。"""
    from pypinyin import Style, lazy_pinyin

    same_pinyin = lazy_pinyin(script_char, style=Style.NORMAL) == lazy_pinyin(asr_char, style=Style.NORMAL)
    line = LineSpec(utterance_id="U01", text=f"甲{script_char}乙丙丁")
    tokens = _toks([("甲", 0.0), (asr_char, 0.2), ("乙", 0.4), ("丙", 0.6), ("丁", 0.8)])
    result = align_shot([line], tokens)
    la = result.lines[0]
    if same_pinyin:
        assert la.matched_chars == 5
    else:
        assert la.matched_chars == 4


def test_min_match_ratio_constant_is_060():
    assert MIN_MATCH_RATIO == 0.60
