"""短剧节奏档留档：``adaptation_summary``/``dropped_line_quote_ids``（生成期可算
的部分）+ ``storyboard_pack_evidence`` 的章节偏移换算与持久化（``chapters.
content[start:end]`` 必须与声明区间原文逐字相同）。
"""
from __future__ import annotations

import json

from app.production.storyboard_pack import persist_storyboard_pack
from app.production.storyboard_pack_evidence import _adaptation_evidence_content, _dropped_span_evidence
from app.production.storyboard_short_drama import adaptation_summary, dropped_line_quote_ids
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
    )
    assert summary["adaptation_mode"] == "faithful"
    assert summary["target_duration_s"] is None
    assert summary["target_segment_count"] is None
    assert summary["max_segment_count"] is None
    assert summary["over_target"] is False
    assert summary["dropped_source_spans"] == []
    assert summary["dropped_line_quote_ids"] == []


def test_adaptation_summary_short_drama_over_target_true():
    summary = adaptation_summary(
        adaptation_mode="short_drama", planned_segment_count=10, segment_count=8,
        dropped_spans=[_AiDroppedSourceSpan(source_segment_index=1, from_unit=1, to_unit=2, reason="闲笔")],
        dropped_quote_ids=["Q01"],
    )
    assert summary["target_duration_s"] == 90
    assert summary["target_segment_count"] == 6
    assert summary["max_segment_count"] == 8
    assert summary["planned_segment_count"] == 10
    assert summary["over_target"] is True
    assert summary["dropped_source_spans"] == [{"source_segment_index": 1, "from_unit": 1, "to_unit": 2, "reason": "闲笔"}]
    assert summary["dropped_line_quote_ids"] == ["Q01"]


def test_adaptation_summary_short_drama_within_target_not_over():
    summary = adaptation_summary(
        adaptation_mode="short_drama", planned_segment_count=6, segment_count=6, dropped_spans=[], dropped_quote_ids=[],
    )
    assert summary["over_target"] is False


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
        dropped_spans=[], dropped_quote_ids=[],
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
        dropped_spans=[_AiDroppedSourceSpan(source_segment_index=1, from_unit=unit_no, to_unit=unit_no, reason="闲笔可删")],
        dropped_quote_ids=["Q09"],
    )
    persist_storyboard_pack(conn, episode_id, ep, payload, pack, segments=segments)

    row = conn.execute(
        "SELECT content_json FROM artifacts WHERE type='storyboard_pack_adaptation' AND scope_id=?", (episode_id,),
    ).fetchone()
    content = json.loads(row["content_json"])
    assert content["adaptation_mode"] == "short_drama"
    assert content["planned_segment_count"] == 10
    assert content["over_target"] is True
    assert content["dropped_line_quote_ids"] == ["Q09"]
    span = content["dropped_source_spans"][0]
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
