"""短剧节奏档留档：``adaptation_summary``/``dropped_line_quote_ids``（生成期可算
的部分）+ ``storyboard_pack_evidence`` 的章节偏移换算与持久化（``chapters.
content[start:end]`` 必须与声明区间原文逐字相同）。
"""
from __future__ import annotations

import json

from app.production.storyboard_pack import StoryboardPackBeat, persist_storyboard_pack
from app.production.storyboard_pack_evidence import _adaptation_evidence_content, _dropped_span_evidence
from app.production.storyboard_short_drama import DIALOGUE_BUDGET_CHARS, MAX_SEGMENT_COUNT, adaptation_summary, dropped_line_quote_ids
from app.production.storyboard_short_drama_schemas import _AiDroppedSourceSpan
from app.production.storyboard_dialogue_ledger import _AiDroppedLine
from app.production.storyboard_segment_ranges import split_source_units
from app.source_chapters import _episode_source_blocks
from app.source_excerpt import index_source_segments
from tests.test_storyboard_pack import _pack, _prep_pack_2_0_0_payload, _real_segments, _seed_episode
from app import db


# ---------------------------------------------------------------------------
# adaptation_summary / dropped_line_quote_ids（纯函数，生成期可算的部分）
# ---------------------------------------------------------------------------

def test_adaptation_summary_faithful_mode_has_empty_targets_and_spans():
    summary = adaptation_summary(
        adaptation_mode="faithful", planned_segment_count=9, segment_count=9, dropped_spans=[], dropped_quote_ids=[],
        kept_dialogue_chars=123, projected_segment_count=None,
    )
    assert summary["adaptation_mode"] == "faithful"
    assert summary["target_duration_s"] is None
    assert summary["target_segment_count"] is None
    assert summary["max_segment_count"] is None
    assert summary["max_duration_s"] is None
    assert summary["over_target"] is False
    assert summary["planned_over_cap"] is False
    assert summary["final_duration_s"] == 135, "9 段 * 15 秒/段，忠实档同样无条件计算"
    assert summary["kept_dialogue_chars"] == 123, "计数字段忠实档同样无条件透传"
    assert summary["dialogue_budget_chars"] is None
    assert summary["dropped_source_spans"] == []
    assert summary["dropped_line_quote_ids"] == []
    assert summary["projected_segment_count"] is None


def test_adaptation_summary_short_drama_over_target_true_by_final_segment_count():
    """2026-09-24 真实三集验证复现的根因：模型规划段数达标（7 <= 8，
    planned_over_cap 应为 False），但容量归一化按台词量把段数撑到 16——
    over_target 必须按*最终*段数判定，不能再看规划段数（旧语义否则恒 false，
    界面「超出短剧上限」的提示永远不出现）。"""
    summary = adaptation_summary(
        adaptation_mode="short_drama", planned_segment_count=7, segment_count=16,
        dropped_spans=[_AiDroppedSourceSpan(source_segment_index=1, from_unit=1, to_unit=2, reason="闲笔", beat_id="B1")],
        dropped_quote_ids=["Q01"], kept_dialogue_chars=900, projected_segment_count=15,
    )
    assert summary["target_duration_s"] == 90
    assert summary["target_segment_count"] == 6
    assert summary["max_segment_count"] == 8
    assert summary["max_duration_s"] == 120
    assert summary["planned_segment_count"] == 7
    assert summary["projected_segment_count"] == 15, "SegmentCountSoftCap 最后一次校验时算出的预计段数，原样透传"
    assert summary["segment_count"] == 16
    assert summary["final_duration_s"] == 240
    assert summary["over_target"] is True, "按最终段数 16 > 8 判定"
    assert summary["planned_over_cap"] is False, "模型规划的 7 段本身没超上限"
    assert summary["kept_dialogue_chars"] == 900
    assert summary["dialogue_budget_chars"] == DIALOGUE_BUDGET_CHARS == 432
    assert summary["dropped_source_spans"] == [{"source_segment_index": 1, "from_unit": 1, "to_unit": 2, "reason": "闲笔", "beat_id": "B1"}]
    assert summary["dropped_line_quote_ids"] == ["Q01"]


def test_adaptation_summary_short_drama_planned_over_cap_true_when_model_itself_over_planned():
    """旧语义（模型规划阶段就超上限）继续存在，改名 planned_over_cap；此时
    segment_count 恒 >= planned_segment_count（归一化只拆不并），over_target
    结构上也必为真。"""
    summary = adaptation_summary(
        adaptation_mode="short_drama", planned_segment_count=10, segment_count=10,
        dropped_spans=[], dropped_quote_ids=[], kept_dialogue_chars=0, projected_segment_count=10,
    )
    assert summary["over_target"] is True
    assert summary["planned_over_cap"] is True


def test_adaptation_summary_short_drama_within_target_not_over():
    summary = adaptation_summary(
        adaptation_mode="short_drama", planned_segment_count=6, segment_count=6, dropped_spans=[], dropped_quote_ids=[],
        kept_dialogue_chars=0, projected_segment_count=6,
    )
    assert summary["over_target"] is False
    assert summary["planned_over_cap"] is False
    assert summary["final_duration_s"] == 6 * 15 == 90
    assert MAX_SEGMENT_COUNT == 8, "本测试其它断言假定这个值，变了要连带核对上面几个测试"


def test_dropped_line_quote_ids_filters_by_reason_prefix():
    class _Draft:
        dropped_lines = [
            _AiDroppedLine(quote_id="Q1", reason="随原文区间删减：闲笔"),
            _AiDroppedLine(quote_id="Q2", reason="语气词"),
            _AiDroppedLine(quote_id="Q3", reason="随原文区间删减：重复"),
        ]

    assert dropped_line_quote_ids(_Draft()) == ["Q1", "Q3"]


def test_dropped_line_quote_ids_empty_for_draft_without_dropped_lines():
    class _Draft:
        pass

    assert dropped_line_quote_ids(_Draft()) == []


# ---------------------------------------------------------------------------
# _dropped_span_evidence：章节偏移换算，content[start:end] 必须逐字匹配声明区间
# ---------------------------------------------------------------------------

def test_dropped_span_evidence_offsets_match_chapter_content_slice():
    content = "老板走进办公室。他自言自语道：“我们一定要赢。”他想起昨天下过雨。他嘀咕道：“这有什么用啊。”"
    source = {"id": 1, "idx": 0, "title": "第一章", "content": content}
    full_source_text, _ = _episode_source_blocks([source])
    segments = index_source_segments(full_source_text)
    units = split_source_units(segments[0].text)
    target_text = "他想起昨天下过雨。"
    unit_no = next(i for i, (a, b) in enumerate(units, 1) if segments[0].text[a:b] == target_text)
    span = {"source_segment_index": 1, "from_unit": unit_no, "to_unit": unit_no, "reason": "闲笔可删"}
    evidence = _dropped_span_evidence(span, segments=segments, full_source_text=full_source_text, authorized_sources=[source])
    assert evidence is not None
    assert content[evidence["start_offset"]:evidence["end_offset"]] == target_text
    assert evidence["chapter_idx"] == 0
    assert evidence["excerpt"] == target_text[:24]
    assert evidence["chars"] == len(target_text), "chars 是非空白字数，此句无空白"
    assert evidence["reason"] == "闲笔可删"
    assert evidence["from_unit"] == unit_no and evidence["to_unit"] == unit_no


def test_dropped_span_evidence_multi_unit_span_covers_full_range():
    content = "甲句子一。乙句子二。丙句子三。丁句子四。"
    source = {"id": 5, "idx": 0, "title": "第二章", "content": content}
    full_source_text, _ = _episode_source_blocks([source])
    segments = index_source_segments(full_source_text)
    units = split_source_units(segments[0].text)
    start_unit = next(i for i, (a, b) in enumerate(units, 1) if segments[0].text[a:b] == "乙句子二。")
    end_unit = next(i for i, (a, b) in enumerate(units, 1) if segments[0].text[a:b] == "丙句子三。")
    span = {"source_segment_index": 1, "from_unit": start_unit, "to_unit": end_unit, "reason": "连续两句闲笔"}
    evidence = _dropped_span_evidence(span, segments=segments, full_source_text=full_source_text, authorized_sources=[source])
    assert evidence is not None
    assert content[evidence["start_offset"]:evidence["end_offset"]] == "乙句子二。丙句子三。"


def test_adaptation_evidence_content_enriches_all_spans_and_keeps_other_keys():
    content = "甲句子一。乙句子二。"
    source = {"id": 9, "idx": 0, "title": "章九", "content": content}
    full_source_text, _ = _episode_source_blocks([source])
    segments = index_source_segments(full_source_text)
    units = split_source_units(segments[0].text)
    unit_no = next(i for i, (a, b) in enumerate(units, 1) if segments[0].text[a:b] == "甲句子一。")
    raw = {
        "adaptation_mode": "short_drama", "over_target": False,
        "dropped_source_spans": [{"source_segment_index": 1, "from_unit": unit_no, "to_unit": unit_no, "reason": "x"}],
        "dropped_line_quote_ids": [],
    }

    class _Pack:
        adaptation = raw

    enriched = _adaptation_evidence_content(_Pack(), segments=segments, full_source_text=full_source_text, authorized_sources=[source])
    assert enriched["adaptation_mode"] == "short_drama"
    assert len(enriched["dropped_source_spans"]) == 1
    assert "chapter_idx" in enriched["dropped_source_spans"][0]


# ---------------------------------------------------------------------------
# 持久化整合：persist_storyboard_pack 写出 storyboard_pack_adaptation 产物
# ---------------------------------------------------------------------------

def test_persist_storyboard_pack_writes_adaptation_artifact_faithful_mode_empty():
    conn = db.get_conn()
    episode_id = "ep-short-drama-adaptation-faithful"
    _seed_episode(conn, episode_id=episode_id)
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    payload = _prep_pack_2_0_0_payload()
    segments = _real_segments(conn, ep)
    pack = _pack()
    pack.adaptation = adaptation_summary(
        adaptation_mode="faithful", planned_segment_count=len(pack.segments), segment_count=len(pack.segments),
        dropped_spans=[], dropped_quote_ids=[], kept_dialogue_chars=0, projected_segment_count=None,
    )
    persist_storyboard_pack(conn, episode_id, ep, payload, pack, segments=segments)

    row = conn.execute(
        "SELECT content_json FROM artifacts WHERE type='storyboard_pack_adaptation' AND scope_id=?", (episode_id,),
    ).fetchone()
    assert row is not None
    content = json.loads(row["content_json"])
    assert content["adaptation_mode"] == "faithful"
    assert content["dropped_source_spans"] == []
    assert content["over_target"] is False
    assert content["planned_over_cap"] is False
    assert content["projected_segment_count"] is None


def test_persist_storyboard_pack_writes_adaptation_artifact_short_drama_with_real_offsets():
    conn = db.get_conn()
    episode_id = "ep-short-drama-adaptation-real"
    _seed_episode(conn, episode_id=episode_id)
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    payload = _prep_pack_2_0_0_payload()
    segments = _real_segments(conn, ep)
    units = split_source_units(segments[0].text)
    target_text = "少年站在山顶。"
    unit_no = next(i for i, (a, b) in enumerate(units, 1) if segments[0].text[a:b] == target_text)

    pack = _pack()
    pack.adaptation = adaptation_summary(
        adaptation_mode="short_drama", planned_segment_count=10, segment_count=len(pack.segments),
        dropped_spans=[_AiDroppedSourceSpan(source_segment_index=1, from_unit=unit_no, to_unit=unit_no, reason="闲笔可删", beat_id="B1")],
        dropped_quote_ids=["Q09"], kept_dialogue_chars=40, projected_segment_count=13,
    )
    persist_storyboard_pack(conn, episode_id, ep, payload, pack, segments=segments)

    row = conn.execute(
        "SELECT content_json FROM artifacts WHERE type='storyboard_pack_adaptation' AND scope_id=?", (episode_id,),
    ).fetchone()
    content = json.loads(row["content_json"])
    assert content["adaptation_mode"] == "short_drama"
    assert content["planned_segment_count"] == 10
    assert content["projected_segment_count"] == 13, "留档字段原样透传，不是从 segment_count 派生"
    # _pack() 只有 1 个最终段：over_target 按*最终*段数（1）判定为 False，
    # 但模型规划阶段的 10 段本身已经超上限，planned_over_cap 为 True——
    # 这正是两个字段分拆之后要能表达的差异（2026-09-24 真实三集验证的根因）。
    assert content["over_target"] is False
    assert content["planned_over_cap"] is True
    assert content["kept_dialogue_chars"] == 40
    assert content["dialogue_budget_chars"] == 432
    assert content["dropped_line_quote_ids"] == ["Q09"]
    span = content["dropped_source_spans"][0]
    assert span["beat_id"] == "B1", "留档区间带 beat_id（2026-09-24 区间 beat 归属核验落库）"
    chapter_row = conn.execute("SELECT content FROM chapters WHERE project_id=?", (ep["project_id"],)).fetchone()
    assert chapter_row["content"][span["start_offset"]:span["end_offset"]] == target_text


def test_persist_storyboard_pack_skips_adaptation_artifact_when_pack_adaptation_is_empty():
    """pack.adaptation 为空（既有测试直接用 _pack() 构造、不显式设置的常见
    形态）时不写这一条残缺产物——门禁对「没有这条留档」本就按忠实档处理，
    语义等价，但不会把没有 adaptation_mode 的半成品数据落进 artifacts 表。"""
    conn = db.get_conn()
    episode_id = "ep-short-drama-adaptation-empty-skip"
    _seed_episode(conn, episode_id=episode_id)
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    payload = _prep_pack_2_0_0_payload()
    segments = _real_segments(conn, ep)
    pack = _pack()
    assert pack.adaptation == {}, "_pack() 不显式设置 adaptation，字段默认空 dict"
    persist_storyboard_pack(conn, episode_id, ep, payload, pack, segments=segments)

    row = conn.execute(
        "SELECT content_json FROM artifacts WHERE type='storyboard_pack_adaptation' AND scope_id=?", (episode_id,),
    ).fetchone()
    assert row is None
    # 另外两份产物不受影响，照常写入。
    other = conn.execute(
        "SELECT content_json FROM artifacts WHERE type='storyboard_pack_beat_sheet' AND scope_id=?", (episode_id,),
    ).fetchone()
    assert other is not None


def test_persist_storyboard_pack_beat_sheet_artifact_carries_importance_for_short_drama():
    """短剧档节拍表产物保留每个节拍的 importance，供事后核对被删台词属于哪类
    节拍；忠实档没有这个字段就不写（StoryboardPackBeat.importance 默认
    None，storyboard_pack_evidence 用 exclude_none 排除，见该模块「beat_sheet」
    字典字面量）。"""
    conn = db.get_conn()
    episode_id = "ep-short-drama-beat-importance"
    _seed_episode(conn, episode_id=episode_id)
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    payload = _prep_pack_2_0_0_payload()
    segments = _real_segments(conn, ep)
    pack = _pack()
    pack.beat_sheet = [
        StoryboardPackBeat(beat_id="B1", summary="他扔掉了理想", segment_indexes=[1, 2], importance="key"),
    ]
    persist_storyboard_pack(conn, episode_id, ep, payload, pack, segments=segments)

    row = conn.execute(
        "SELECT content_json FROM artifacts WHERE type='storyboard_pack_beat_sheet' AND scope_id=?", (episode_id,),
    ).fetchone()
    content = json.loads(row["content_json"])
    assert content["beat_sheet"] == [
        {"beat_id": "B1", "summary": "他扔掉了理想", "segment_indexes": [1, 2], "importance": "key"},
    ]
