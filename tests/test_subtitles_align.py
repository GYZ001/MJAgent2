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


def test_short_line_partial_match_missing():
    line = LineSpec(utterance_id="U01", text="是啊")
    tokens = _toks([("是", 0.0), ("嗯", 0.2)])
    result = align_shot([line], tokens)
    la = result.lines[0]
    assert la.status == "missing"
    assert la.reason == "short_line_partial"


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


def test_three_char_line_noncontiguous_hits_are_missing():
    """首尾字命中但中间字没命中：2 字命中但不连续，不满足短句规则。"""
    line = LineSpec(utterance_id="U01", text="甲乙丙")
    tokens = _toks([("甲", 0.0), ("戊", 0.2), ("丙", 0.4)])
    result = align_shot([line], tokens)
    la = result.lines[0]
    assert la.status == "missing"
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
    assert la.status == "missing"
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
