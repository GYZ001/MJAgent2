"""WS1：标题正面定义、小节边界、章节尺寸拆分/合并。

覆盖 app/ingest.py + app/novel/structure.py 的三条修复：
  B. 标题正面定义（第X[章卷回节集部幕篇]/英文 Chapter 系列/分隔线包夹短行），
     独占一行的纯序号是小节不是章；
  C. 前言/楔子命名（小比例→楔子，大比例且不可再拆→用自己的标题行命名）；
  D. 章节尺寸上下限拆分/合并。

跨项目回归样本取自 B 库只读查询（2026-09-02，见 WS1 派单），标题字符串逐字复制，
不改写、不省略空白，确保新规则不误伤这些项目已经工作正常的识别结果。
"""
from __future__ import annotations

import json

from app.ingest import ingest_novel
from app.novel.structure import (
    CHAPTER_RE,
    _merge_undersized_ending_chapters,
    _preamble_chapters,
    _split_oversized_chapter,
)

SEP = "═" * 12


def _ji_block(number_cn: str, name: str, sections: list[str]) -> str:
    body = f"{SEP}\n第{number_cn}集　{name}\n{SEP}\n\n"
    numerals = ["一", "二", "三", "四", "五"]
    for i, text in enumerate(sections):
        body += f"{numerals[i]}\n\n{text}\n\n"
    return body


# ---------------------------------------------------------------------------
# B. 标题正面定义——新增结构信号
# ---------------------------------------------------------------------------

def test_chapter_re_recognizes_ji_unit() -> None:
    """「跑不快的孩子」proj_ce9fcf749b23 病灶：全书按「第X集」分部，此前 CHAPTER_RE
    只认章/卷/回/节，7009 字被整体错认成「楔子」。"""
    m = CHAPTER_RE.match("第一集　罗萨里奥的雨")
    assert m is not None
    assert m.group(1).strip() == "第一集　罗萨里奥的雨"


def test_chapter_re_recognizes_waipian_keyword() -> None:
    """「神墓》作者：辰东」proj_f28fc90b014d：楔子内嵌「外篇——战天时代」，此前
    词表只有「番外」没有「外篇」，这段被静默吞进楔子正文。"""
    m = CHAPTER_RE.match("外篇——战天时代")
    assert m is not None
    assert m.group(1).strip().startswith("外篇")


def test_chapter_re_recognizes_english_chapter_markers() -> None:
    for line in ("Chapter 12", "Episode 3", "EP.5", "Part 4"):
        assert CHAPTER_RE.match(line) is not None, line


def test_separator_sandwiched_title_without_keyword_becomes_a_chapter() -> None:
    """分隔线上下包夹的短行也是标题，即使它不落在任何关键词/序号词表里——
    正面定义结构信号，不靠关键词穷举。"""
    text = (
        "开场白，与后面的深潜行动无关。\n\n"
        f"{SEP}\n深潜\n{SEP}\n\n"
        "他们潜入海底基地，寻找失踪的潜水员。这一段描写足够长，"
        "确保不会被当成正文之前的引子而被合并进楔子。" * 4 + "\n\n"
        f"{SEP}\n归途\n{SEP}\n\n"
        "任务结束，他们浮出水面，看见了久违的阳光，故事在这里落下帷幕。" * 4
    )
    result = ingest_novel(text.encode("utf-8"))
    titles = [c["title"] for c in result["chapters"]]
    assert "深潜" in titles
    assert "归途" in titles


# ---------------------------------------------------------------------------
# C + D：小节记录、尺寸拆分/合并——「跑不快的孩子」端到端场景
# ---------------------------------------------------------------------------

def _rebuilt_paotai_text() -> str:
    ji_bodies = [
        _ji_block("一", "罗萨里奥的雨", [
            "土场下雨就变成泥塘，孩子们从不介意，只在乎球在不在自己脚下。",
            "诊断书下来的时候，医生说的词他听不懂，走出医院时罗萨里奥在下雨。",
        ]),
        _ji_block("二", "红蓝色的王座", [
            "他在替补席上站起来的时候，十七岁，脸还是圆的，草皮绿得不像真的。",
            "罗纳尔迪尼奥把球分给他，他接球、转身、过人，球滚进网窝。",
        ]),
        _ji_block("三", "咫尺天涯", [
            "阿根廷 0:4 输给德国，他在中圈站着，从开场站到结束。",
            "大力神杯就放在通道一侧的展柜里，距离他不到三米，他看了三秒，转身走了。",
        ]),
        _ji_block("四", "天光", [
            "三十五岁，所有人都说这是他的最后一次世界杯，他只说一场一场来。",
            "他抬起头，看了门将一眼，助跑，射门，球进，网窝颤动。",
        ]),
    ]
    tail = "尾声\n\n很多年以后，人们会这样介绍他：史上最伟大的球员，没有之一。"
    return "那个跑不快的小孩\n——梅西成长史 · 四集中篇\n\n" + "".join(ji_bodies) + "\n\n" + tail


def test_ingest_splits_ji_units_and_merges_tiny_ending_chapter() -> None:
    result = ingest_novel(_rebuilt_paotai_text().encode("utf-8"))
    titles = [c["title"] for c in result["chapters"]]

    assert result["chapter_count"] == 4, titles
    assert titles[0].startswith("第一集")
    assert titles[1].startswith("第二集")
    assert titles[2].startswith("第三集")
    assert titles[3].startswith("第四集")
    # 尾声 366-字量级的收尾内容并入第四集，不再单独成为一集。
    assert titles[3].endswith(" · 尾声")
    assert "史上最伟大的球员" in result["chapters"][3]["content"]
    # 「楔子」书名/副标题（2 行、约 30 字）低于 200 字丢弃阈值，不应出现。
    assert not any("楔子" in t for t in titles)


def test_ingest_records_sections_in_paratext_json() -> None:
    result = ingest_novel(_rebuilt_paotai_text().encode("utf-8"))
    first = result["chapters"][0]
    payload = json.loads(first["paratext_json"])
    labels = [s["label"] for s in payload["sections"]]
    assert labels == ["一", "二"]
    for section in payload["sections"]:
        assert 0 <= section["start"] < section["end"] <= len(first["content"])


# ---------------------------------------------------------------------------
# D. 章节尺寸拆分/合并——直接单元测试（不依赖真实达到 16000/800 字量级的正文）
# ---------------------------------------------------------------------------

def test_split_oversized_chapter_by_recorded_sections() -> None:
    sections_text = "".join(f"{n}\n\n{'内容片段。' * 20}\n\n" for n in ("一", "二", "三"))
    chapter = {"title": "第一章 超长", "content": sections_text}
    pieces = _split_oversized_chapter(chapter, upper_bound=200)

    assert len(pieces) >= 2
    assert all(p["title"].startswith("第一章 超长（") for p in pieces)
    # 不丢字：各片段拼接总长与原文长度相差不超过 strip() 造成的边界空白。
    assert sum(len(p["content"]) for p in pieces) <= len(sections_text)
    assert sum(len(p["content"]) for p in pieces) > len(sections_text) - 20


def test_split_oversized_chapter_without_sections_stays_whole() -> None:
    """没有可识别小节的长章节维持整章——如西游记单章 15885 字，不误拆。"""
    chapter = {"title": "第三十七回 无小节的长回目", "content": "正文" * 5000}
    pieces = _split_oversized_chapter(chapter, upper_bound=200)
    assert pieces == [chapter]


def test_merge_undersized_ending_chapter_into_predecessor() -> None:
    chapters = [
        {"idx": 1, "title": "第四集　天光", "content": "正文" * 500},
        {"idx": 2, "title": "尾声", "content": "很短的收尾内容"},
    ]
    merged = _merge_undersized_ending_chapters(chapters, lower_bound=800)
    assert len(merged) == 1
    assert merged[0]["title"] == "第四集　天光 · 尾声"
    assert "很短的收尾内容" in merged[0]["content"]


def test_merge_does_not_touch_ending_chapter_above_lower_bound() -> None:
    """尾声本身够长时不合并——只拦「小到不足以独立成章」的情形，不是拦关键词本身。"""
    chapters = [
        {"idx": 1, "title": "第一章 正文", "content": "正文" * 500},
        {"idx": 2, "title": "尾声", "content": "足够长的收尾内容。" * 200},
    ]
    merged = _merge_undersized_ending_chapters(chapters, lower_bound=800)
    assert len(merged) == 2


def test_merge_does_not_touch_first_chapter() -> None:
    """结尾类关键词若恰好是全文唯一章节，没有「前一章」可并，原样保留。"""
    chapters = [{"idx": 1, "title": "尾声", "content": "短"}]
    merged = _merge_undersized_ending_chapters(chapters, lower_bound=800)
    assert merged == chapters


# ---------------------------------------------------------------------------
# C. 前言/楔子命名
# ---------------------------------------------------------------------------

def test_small_preamble_before_real_chapters_is_named_prologue() -> None:
    preamble = "作者的话：" + "这是一段不算短但明显小于全文占比的开场白。" * 10
    text = preamble + "\n\n第一章 正式开始\n" + "正文内容。" * 300
    chapters = _preamble_chapters(text, preamble)
    assert len(chapters) == 1
    assert chapters[0]["title"] == "楔子"


def test_large_unheaded_block_uses_its_own_first_line_not_prologue() -> None:
    """唯一识别到的标题是结尾类且前面的块占比 >=50%：不得叫楔子，
    先尝试按小节再切一次；本用例没有可识别小节，故退回用首行命名。"""
    first_line = "无法识别的自定义标题"
    body = first_line + "\n\n" + ("这一大段没有任何可识别的章节标题结构。" * 80)
    chapters = _preamble_chapters(body + "XX", body)  # len(text) 略大于 preamble，占比 >=50%
    assert len(chapters) == 1
    assert chapters[0]["title"] == first_line
    assert chapters[0]["title"] != "楔子"


def test_short_preamble_is_discarded() -> None:
    assert _preamble_chapters("第一章 正文\n" + "x" * 500, "太短的引子") == []


# ---------------------------------------------------------------------------
# E. 跨项目回归样本——标题字符串逐字取自 B 库真实数据，不得倒退
# ---------------------------------------------------------------------------

_REGRESSION_TITLES = [
    # 西游记_简体_UTF-8 proj_a5d711b0a337
    "第一回     灵根育孕源流出　心性修持大道生",
    "第二回     悟彻菩提真妙理　断魔归本合元神",
    # 《神墓》作者：辰东 proj_f28fc90b014d
    "楔子",
    "第一章 远古神墓",
    # 我欲封天 proj_f8cf2eeb2e66（四位补零序号）
    "第0001章 书生孟浩",
    "第0002章 靠山宗",
    # 三国演义_白话文版_前二十回 proj_ecabd38b7261
    "第一回 刘关张桃园结义",
    "第二回 谋董卓曹操献刀",
]


def test_real_project_titles_still_recognized_no_regression() -> None:
    for title in _REGRESSION_TITLES:
        m = CHAPTER_RE.match(title)
        assert m is not None, f"回归失败，未识别：{title!r}"
        assert m.group(1).strip() == title
