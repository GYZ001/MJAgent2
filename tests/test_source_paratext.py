"""作者的话不得被当成正文喂给「造人物」的两条路径。

生产缺陷 R9（用户报告：人物谱里出现作者「耳根」）：

* 网文章节正文里直接粘着作者的话（求票、感谢读者、活动公告）。
  「我欲封天」1616 章里 209 章（12.9%）如此。
* `_recurring_character_names` 按**原文逐字出现次数**产出「必收名单」，
  提示词明令「名单里的每个名字…不得改写、合并或省略」。
  作者笔名在统计窗口里出现 27 次、排第 4（真配角王有材才 17 次），
  于是**模型是被程序命令**建出那张人物卡的——不是模型幻觉。
* `identity_authority_registry` 再把每个人物谱条目无条件注册成
  **所有分集**的可引用身份，证据写「角色圣经已登记身份」，
  条目自己就是自己的证据，于是污染扩散到 149 个产物。

剧本链路本身没问题：叙事蓝图会把这些段判成 paratext（实测 1736 个 paratext
节点 vs 15748 story），来源覆盖记 audit_only，剧本正文零污染。
问题是造人物的两条路径跑在这套分类之前且不看它。

本文件测的是**程序那一半**：锚点定位与切割必须确定性、有界、可证。
判断哪段是旁文本由模型做，不在这里测。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import db, source_paratext
from app.harness import model_gateway
from app.source_excerpt import index_source_segments
from app.source_paratext import (
    MAX_REGION_FRACTION,
    MAX_REMOVED_FRACTION,
    MIN_ANCHOR_CHARS,
    ParatextAnchor,
    ParatextSpans,
    PARATEXT_RULE,
    chapter_paratext_offsets,
    paratext_segment_indexes,
    paratext_spans,
    remove_offsets,
    remove_spans,
)

STORY = (
    "孟浩推开院门走进屋舍，桌上摊着一卷未读完的书。"
    "他坐下翻了两页，忽然听见院外传来脚步声。"
    "「谁在外面？」孟浩起身问道，手已按在桌角。"
)
NOTE = "新书急需收藏，推荐票不要少，诸位道友，耳根在此谢过大家！"


def _anchor(text: str, head: int = 12, tail: int = 12) -> ParatextAnchor:
    return ParatextAnchor(start=text[:head], end=text[-tail:])


def test_anchors_cut_exactly_the_note_and_keep_the_story() -> None:
    raw = STORY + NOTE
    out = remove_spans(raw, [_anchor(NOTE)])

    assert "耳根" not in out
    assert "推荐票" not in out
    assert "孟浩推开院门走进屋舍" in out
    assert "「谁在外面？」" in out


def test_note_in_the_middle_is_cut_without_touching_either_side() -> None:
    raw = STORY[:20] + NOTE + STORY[20:]
    out = remove_spans(raw, [_anchor(NOTE)])

    assert NOTE not in out
    assert out == STORY[:20] + STORY[20:]


def test_unfindable_anchor_removes_nothing() -> None:
    """模型抄错锚点时必须整段放弃，宁可漏删也不能乱删。"""
    raw = STORY + NOTE
    bogus = ParatextAnchor(start="这段文字并不存在于原文", end="同样也不存在于原文")

    assert remove_spans(raw, [bogus]) == raw


def test_end_anchor_before_start_anchor_removes_nothing() -> None:
    raw = STORY + NOTE
    reversed_anchor = ParatextAnchor(start=NOTE[-12:], end=NOTE[:12])

    assert remove_spans(raw, [reversed_anchor]) == raw


@pytest.mark.parametrize("length", [0, 1, MIN_ANCHOR_CHARS - 1])
def test_too_short_anchors_are_refused(length: int) -> None:
    """短锚点会在正文里撞上同名片段，必须拒绝。"""
    raw = STORY + NOTE
    short = ParatextAnchor(start=NOTE[:length], end=NOTE[-length:] if length else "")

    assert remove_spans(raw, [short]) == raw


def test_removal_is_capped_so_a_wrong_call_cannot_eat_the_story() -> None:
    """判错一次不得把正文删掉大半——超过上限整体放弃。"""
    raw = STORY + NOTE
    whole = ParatextAnchor(start=raw[:12], end=raw[-12:])

    assert remove_spans(raw, [whole]) == raw


def test_cap_is_a_fraction_not_a_constant() -> None:
    assert 0 < MAX_REMOVED_FRACTION < 1


def test_overlapping_regions_are_merged_not_double_cut() -> None:
    raw = STORY + NOTE
    a = _anchor(NOTE)
    b = ParatextAnchor(start=NOTE[:14], end=NOTE[-10:])

    assert remove_spans(raw, [a, b]) == remove_spans(raw, [a])


def test_multiple_notes_are_all_cut() -> None:
    """真实比例：章节 3600 字、每段作者的话百余字，占比远低于上限。"""
    second = "今天两更，月票榜掉得厉害，恳请诸位道友支援一张月票！"
    body = STORY * 6  # 放大正文，让两段旁文本的占比接近真实章节
    raw = body[:120] + NOTE + body[120:] + second
    out = remove_spans(raw, [_anchor(NOTE), _anchor(second)])

    assert NOTE not in out
    assert second not in out
    assert "孟浩推开院门走进屋舍" in out


def test_one_bad_anchor_does_not_discard_the_good_removals() -> None:
    """一个定歪的锚点只丢它自己，其余有效删除必须照常生效。"""
    body = STORY * 6
    raw = body + NOTE
    good = _anchor(NOTE)
    runaway = ParatextAnchor(start=raw[:12], end=raw[-12:])  # 圈住整篇

    out = remove_spans(raw, [runaway, good])

    assert NOTE not in out
    assert "孟浩推开院门走进屋舍" in out


def test_caps_are_fractions_and_region_cap_is_tighter() -> None:
    assert 0 < MAX_REGION_FRACTION <= MAX_REMOVED_FRACTION < 1


def test_no_spans_returns_the_text_unchanged() -> None:
    raw = STORY + NOTE
    assert remove_spans(raw, []) == raw


def test_single_sentence_note_where_anchors_overlap() -> None:
    """整段就是一句话时，首尾锚点会重叠，仍必须能删掉。"""
    note = "求推荐票，谢谢诸位道友！"
    raw = STORY + note
    out = remove_spans(raw, [ParatextAnchor(start=note, end=note)])

    assert note not in out
    assert "孟浩推开院门走进屋舍" in out


def test_paratext_spans_tags_its_own_stage_key(monkeypatch) -> None:
    """副文本识别此前完全没有 call_meta/stage_key，读超时因此落在通用
    TIMEOUT_CHAT_READ（300s）兜底上——全库里因此白等到 300s 才失败的记录
    里它占比最大（见 app/config.py::TIMEOUT_CHAT_PARATEXT_READ 的实测口径）。
    它必须带自己的 stage_key，才能走 app/hiagent.py::_chat_read_timeout_s
    里那条专属、更短的读超时分支。"""
    captured: dict = {}

    async def fake_chat_structured(_messages, **kwargs):
        captured.update(kwargs)
        return ParatextSpans(spans=[])

    monkeypatch.setattr(model_gateway, "chat_structured", fake_chat_structured)
    source_paratext.paratext_cache_clear()

    body = (STORY + NOTE) * 3  # >= 200 字，避免短文本早退不发起调用
    asyncio.run(paratext_spans(body, operation_id="op_test_paratext_stage_key"))

    assert captured["call_meta"] == {"stage_key": "screenplay_source_paratext"}


def test_rule_text_forbids_keyword_classification() -> None:
    """判据必须和叙事蓝图同源：只按「是否在讲故事」判，不按关键词判。

    蓝图那层明文禁止关键词/位置分类（会误伤），这里不能自己另立一套。
    """
    assert "不得按段落位置、长度或是否出现某个词来判断" in PARATEXT_RULE
    assert "故事叙述本身" in PARATEXT_RULE


# ---------------------------------------------------------------------------
# chapters.paratext_json 持久化入口（logs/paratext_single_source_plan.md）：
# 按章取/算/落库，命中缓存零模型调用，未命中调用一次并原子写回。
# ---------------------------------------------------------------------------


def _seed_chapter(conn, *, project_id: str, content: str, paratext_json: str | None = None) -> dict:
    conn.execute(
        "INSERT OR IGNORE INTO projects(id, name, status, created_at) "
        "VALUES(?,?,?,0)", (project_id, "P", "ingested"),
    )
    cur = conn.execute(
        "INSERT INTO chapters(project_id, idx, title, content, paratext_json) "
        "VALUES(?,1,'第一章',?,?)",
        (project_id, content, paratext_json),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM chapters WHERE id=?", (cur.lastrowid,),
    ).fetchone()
    return dict(row)


def test_chapter_paratext_offsets_cache_miss_computes_and_persists(monkeypatch) -> None:
    """未命中缓存（`paratext_json` 为 NULL）：调用模型算一次，原子写回，
    `cache_hit=False`。"""
    conn = db.get_conn()
    # >= 200 字，避免撞上 paratext_spans 的短文本早退（见
    # test_paratext_spans_tags_its_own_stage_key 同一处理）。
    content = STORY * 3 + NOTE
    chapter = _seed_chapter(conn, project_id="p_paratext_1", content=content)

    calls = 0

    async def fake_chat_structured(_messages, **_kwargs):
        nonlocal calls
        calls += 1
        return ParatextSpans(spans=[_anchor(NOTE)])

    monkeypatch.setattr(model_gateway, "chat_structured", fake_chat_structured)
    source_paratext.paratext_cache_clear()

    regions, cache_hit = asyncio.run(
        chapter_paratext_offsets(conn, chapter, operation_id="op_test_cache_miss")
    )

    assert calls == 1, "未命中缓存必须真的发起一次模型调用"
    assert cache_hit is False
    assert regions, "锚点能在原文里定位到，必须产出至少一个区间"
    assert remove_offsets(content, regions) == remove_spans(content, [_anchor(NOTE)])

    persisted = conn.execute(
        "SELECT paratext_json FROM chapters WHERE id=?", (chapter["id"],),
    ).fetchone()["paratext_json"]
    payload = json.loads(persisted)
    assert payload["content_hash"] == source_paratext._cache_key(content)
    assert payload["spans"] == [{"start": s, "end": e} for s, e in regions]


def test_chapter_paratext_offsets_cache_hit_skips_model_call(monkeypatch) -> None:
    """命中缓存（`content_hash` 与当前 `content` 匹配）：零模型调用，直接
    返回持久化的区间，`cache_hit=True`。"""
    conn = db.get_conn()
    content = STORY + NOTE
    region = source_paratext._anchor_region(content, NOTE[:12], NOTE[-12:])
    assert region is not None
    cached_payload = json.dumps({
        "content_hash": source_paratext._cache_key(content),
        "spans": [{"start": region[0], "end": region[1]}],
        "computed_at": 0.0,
    })
    chapter = _seed_chapter(
        conn, project_id="p_paratext_2", content=content, paratext_json=cached_payload,
    )

    async def fail_chat_structured(*_args, **_kwargs):
        raise AssertionError("命中缓存不应该发起任何模型调用")

    monkeypatch.setattr(model_gateway, "chat_structured", fail_chat_structured)

    regions, cache_hit = asyncio.run(
        chapter_paratext_offsets(conn, chapter, operation_id="op_test_cache_hit")
    )

    assert cache_hit is True
    assert regions == [region]


def test_chapter_paratext_offsets_hash_mismatch_recomputes(monkeypatch) -> None:
    """`content_hash` 对不上当前 `content`（陈旧缓存）：视为未命中，重新
    发起模型调用，不信一份读不懂来源的缓存。"""
    conn = db.get_conn()
    content = STORY * 3 + NOTE  # >= 200 字，见上一条测试同样的处理
    stale_payload = json.dumps({
        "content_hash": "stale-hash-does-not-match",
        "spans": [{"start": 0, "end": 5}],
        "computed_at": 0.0,
    })
    chapter = _seed_chapter(
        conn, project_id="p_paratext_3", content=content, paratext_json=stale_payload,
    )

    calls = 0

    async def fake_chat_structured(_messages, **_kwargs):
        nonlocal calls
        calls += 1
        return ParatextSpans(spans=[])

    monkeypatch.setattr(model_gateway, "chat_structured", fake_chat_structured)
    source_paratext.paratext_cache_clear()

    regions, cache_hit = asyncio.run(
        chapter_paratext_offsets(conn, chapter, operation_id="op_test_hash_mismatch")
    )

    assert calls == 1, "哈希不匹配必须重新发起模型调用，不能信旧缓存"
    assert cache_hit is False
    assert regions == []


def test_chapter_paratext_offsets_no_id_computes_but_does_not_persist(monkeypatch) -> None:
    """合成 chapter（无 `id`，单测常见形态）：照常计算，但不落库——没有主键
    无法定位 UPDATE 目标，静默跳过写入而不是报错，行为退化成"每次都重算"，
    与改造前的调用方完全一致。"""
    conn = db.get_conn()
    content = STORY * 3 + NOTE  # >= 200 字，见上面同样的处理

    async def fake_chat_structured(_messages, **_kwargs):
        return ParatextSpans(spans=[_anchor(NOTE)])

    monkeypatch.setattr(model_gateway, "chat_structured", fake_chat_structured)
    source_paratext.paratext_cache_clear()

    regions, cache_hit = asyncio.run(
        chapter_paratext_offsets(
            conn, {"content": content}, operation_id="op_test_no_id",
        )
    )

    assert cache_hit is False
    assert regions


def test_remove_offsets_matches_remove_spans_on_the_same_region() -> None:
    """偏移版删除是纯算术切割：给它 `remove_spans` 内部算出的同一个区间，
    产出必须逐字节相同——不是另起一套判据。"""
    raw = STORY + NOTE
    region = source_paratext._anchor_region(raw, NOTE[:12], NOTE[-12:])
    assert region is not None

    assert remove_offsets(raw, [region]) == remove_spans(raw, [_anchor(NOTE)])


def test_remove_offsets_no_regions_returns_text_unchanged() -> None:
    raw = STORY + NOTE
    assert remove_offsets(raw, []) == raw


def test_paratext_segment_indexes_tags_overlapping_segments_only() -> None:
    """区间重叠判断：只有与 paratext 区间有重叠的段号才应该入账，1-based，
    口径与 `enumerate(segments, start=1)` 一致。"""
    text = STORY + "\n\n" + NOTE
    segments = index_source_segments(text)
    note_start = text.index(NOTE)
    note_end = note_start + len(NOTE)

    indexes = paratext_segment_indexes(segments, [(note_start, note_end)])

    covered = {
        index for index, segment in enumerate(segments, start=1)
        if segment.start_offset < note_end and note_start < segment.end_offset
    }
    assert indexes == covered
    assert indexes, "夹具必须至少覆盖一个段，否则这条测试没有断言到任何东西"


def test_paratext_segment_indexes_empty_regions_returns_empty_set() -> None:
    text = STORY + NOTE
    segments = index_source_segments(text)
    assert paratext_segment_indexes(segments, []) == set()
