"""``scripts/audit_episode_dialogue_fidelity.py`` 的判据守卫。

这个外部复核工具是「交付判据是原文比对」的可重复形态：闸门全绿不算交付证据，
成片里说出口的话得逐条对得上原文。2026-09-10 用它在计算服务器 B 上复算「我欲封天」
EP2-EP10，逐条重现了手工审查的结论（16 条自造台词 + 13 条提示词不忠实），两次独立
路径得到同一组结果。
"""
from __future__ import annotations

import json

import pytest

from scripts.audit_episode_dialogue_fidelity import (
    check_prompt_fidelity,
    check_spoken_provenance,
    source_coverage,
    spoken_value_domain,
)


class _Shot(dict):
    """``sqlite3.Row`` 在这些函数里只被当映射用，dict 足够替身。"""


def _shot(shot_no: int, lines: list[dict], excerpt: str = "") -> _Shot:
    return _Shot(shot_no=shot_no, dialogues=json.dumps(lines, ensure_ascii=False), source_excerpt=excerpt)


NOVEL = (
    "少年抬头看天。\n\n"
    "“又落榜了……”少年叹了口气，他叫孟浩。\n\n"
    "孟浩看着手中的葫芦，心中一片茫然，不知未来的路在哪里。\n"
)
SCRIPT_FORMAT = (
    "李麦麦（叹气，os）：我自己都快养不活了……\n\n"
    "（李麦麦起身走向门口，猫跳上桌）\n"
)


def test_prose_value_domain_takes_quoted_lines_only() -> None:
    domain = spoken_value_domain(NOVEL, ["孟浩"])
    assert "又落榜了" in domain
    # 叙述句不是任何人说出口的话——它的任意一截都不该进取值域
    assert "心中一片茫然" not in domain


def test_script_format_value_domain_takes_the_speaker_line() -> None:
    """剧本格式原文没有引号：判据必须跟着台账的抽取器走，不能自己写一套引号正则。"""
    domain = spoken_value_domain(SCRIPT_FORMAT, ["李麦麦"])
    assert "我自己都快养不活了" in domain


def test_fabricated_spoken_line_is_reported() -> None:
    domain = spoken_value_domain(NOVEL, ["孟浩"])
    shots = [_shot(1, [{"speaker": "bible:孟浩", "line": "又落榜了……", "delivery": "spoken_dialogue"}])]
    assert check_spoken_provenance(shots, domain) == []
    rewritten = [_shot(1, [{"speaker": "bible:孟浩", "line": "唉，我又没考上。", "delivery": "spoken_dialogue"}])]
    findings = check_spoken_provenance(rewritten, domain)
    assert len(findings) == 1 and "不是本集原文里说过的话" in findings[0]


def test_narration_turned_into_speech_is_reported() -> None:
    """叙述句的逐字一截也不合法——「原文说过的话」不等于「原文出现过的字」。"""
    domain = spoken_value_domain(NOVEL, ["孟浩"])
    shots = [_shot(1, [{"speaker": "bible:孟浩", "line": "心中一片茫然", "delivery": "spoken_dialogue"}])]
    assert len(check_spoken_provenance(shots, domain)) == 1


def test_offscreen_voice_is_exempt_from_the_verbatim_rule() -> None:
    """画外音是叙述者概括，本来就允许不逐字；它自己那条可追溯规则另管。"""
    domain = spoken_value_domain(NOVEL, ["孟浩"])
    shots = [_shot(1, [{"speaker": "旁白", "line": "他不知道未来在哪里", "delivery": "offscreen_voice"}])]
    assert check_spoken_provenance(shots, domain) == []


def test_prompt_fidelity_catches_dropped_and_reworded_lines() -> None:
    shots = [_shot(1, [{"speaker": "甲", "line": "别走。"}, {"speaker": "乙", "line": "我留下。"}])]
    faithful = {1: "镜头1：甲开口说出：“别走。” 镜头2：乙回答：“我留下。”"}
    assert check_prompt_fidelity(shots, faithful) == []
    dropped = {1: "镜头1：甲开口说出：“别走。” 镜头2：乙沉默。"}
    assert len(check_prompt_fidelity(shots, dropped)) == 1
    # 句中改写——线上真实形态（把原著自带的错别字「五人不知」改成「无人不敬佩」）
    reworded = {1: "镜头1：甲开口说出：“别走啊。” 镜头2：乙回答：“我留下。”"}
    assert len(check_prompt_fidelity(shots, reworded)) == 1


def test_prompt_fidelity_blind_spot_is_prefix_only_additions() -> None:
    """已知盲区，写成断言而不是注释——盲区悄悄扩大比盲区本身危险。

    判据是「台账原话逐字出现在提示词里」，所以在原话**前后**加字（「你别走。」
    包含「别走。」）查不出来。改成比对提示词里的引号跨度可以覆盖它，但实测会被
    引号不配对的产物（第 6 集 ``说出："…”`` 前英文直引号后中文右引号）打出误报，
    误报会让整份复核报告失去可信度。两害相权取「不漏掉真实事故形态」：整句丢失
    与句中改写这两类都能抓，且零误报。
    """
    shots = [_shot(1, [{"speaker": "甲", "line": "别走。"}])]
    assert check_prompt_fidelity(shots, {1: "甲说出：“你别走。”"}) == []


def test_prompt_fidelity_skips_shots_without_a_generated_prompt() -> None:
    """还没出提示词的镜头不算问题——「没有」与「不忠实」是两件事，不能混report。"""
    shots = [_shot(1, [{"speaker": "甲", "line": "别走。"}])]
    assert check_prompt_fidelity(shots, {}) == []


def test_source_coverage_counts_only_excerpts_found_in_the_source() -> None:
    shots = [_shot(1, [], excerpt="少年抬头看天。"), _shot(2, [], excerpt="原文里没有这句")]
    hit, total = source_coverage(shots, NOVEL)
    assert 0 < hit < total
    assert source_coverage([], NOVEL) == (0, total)


def test_empty_source_does_not_divide_by_zero() -> None:
    assert source_coverage([_shot(1, [], excerpt="x")], "") == (0, 0)
    assert spoken_value_domain("", ["孟浩"]) == ""


@pytest.mark.parametrize("quote_style", ["“{}”", "「{}」"])
def test_quote_style_difference_is_not_read_as_a_rewrite(quote_style: str) -> None:
    """原文用「」、产物用“”是排版差异，不是台词改写。"""
    source = "孟浩开口：" + quote_style.format("又落榜了") + "他叹了口气。"
    domain = spoken_value_domain(source, ["孟浩"])
    shots = [_shot(1, [{"speaker": "bible:孟浩", "line": "“又落榜了”", "delivery": "spoken_dialogue"}])]
    assert check_spoken_provenance(shots, domain) == []
