"""证据窗口以命中位置为中心截取，不取段首前缀。

根因（2026-09-30 只读排查，proj_ca86b15ab7d7 第2集）：``scene_label_evidence`` 曾经
无条件返回 ``seg.text.strip()[:EVIDENCE_SEGMENT_CHARS]``——命中判定用的是整段原文，
证据却只给段首前 300 字。原文「回民街」在该段的偏移约 365 字，落在前 300 字之外，
证据里因此不含判定依据的地点字面本身，喂给 ``assess_new_scene`` 的
``spatial_context`` 看不到"回民街"三个字 → 新场景判定失败 → 该场景全集未解析、
分镜台对应段落拿不到任何场景资产。见 ``app/production/scene_evidence.py``
``_centered_excerpt`` 的完整说明。
"""
from __future__ import annotations

from app.production.scene_evidence import EVIDENCE_SEGMENT_CHARS, scene_label_evidence
from app.source_excerpt import SourceSegment


def _segment(text: str, segment_id: str = "SRC0001") -> SourceSegment:
    return SourceSegment(segment_id=segment_id, text=text, start_offset=0, end_offset=len(text))


def test_hit_after_prefix_still_enters_evidence() -> None:
    """命中点在前缀之后仍进证据：地点字面出现在 300 字之后，旧实现的前缀截断会
    把它漏在证据外；证据必须包含判定依据的那个字面串本身。"""
    prefix = "温念望着窗外发呆，" * 40  # 远超过 EVIDENCE_SEGMENT_CHARS(300)，不含地点字样
    assert len(prefix) > EVIDENCE_SEGMENT_CHARS
    text = prefix + "半小时后，两人走在回民街的青石板路上，摊贩的吆喝声混在一起。" + "后续闲话。" * 20
    evidence = scene_label_evidence("日 回民街", [_segment(text)])
    assert "回民街" in evidence
    assert len(evidence) <= EVIDENCE_SEGMENT_CHARS


def test_multiple_hits_take_first_occurrence_deterministically() -> None:
    """多次命中：同段内地点字样出现两次，取第一次出现位置为中心，两次调用结果一致
    （确定性，不随机）。"""
    text = "很久以前，回民街还只是一条小巷。" + "无关叙述。" * 60 + "多年以后，回民街已经是网红打卡地了。"
    assert text.count("回民街") == 2
    first = scene_label_evidence("回民街", [_segment(text)])
    second = scene_label_evidence("回民街", [_segment(text)])
    assert first == second
    assert "回民街还只是一条小巷" in first  # 中心在第一次出现的位置，不是第二次


def test_short_segment_returned_verbatim() -> None:
    """短段：段落本身不超过 EVIDENCE_SEGMENT_CHARS 时原样返回，不做任何窗口裁剪。"""
    text = "孟浩走出洞府，沿着山路来到外宗广场。"
    evidence = scene_label_evidence("外宗广场", [_segment(text)])
    assert evidence == text


def test_hit_near_segment_end_window_stays_within_bounds() -> None:
    """命中点靠近段落末尾：居中窗口不得越界（起点被夹到 0 到 len-limit 之间），
    地点字面仍完整落在窗口内。"""
    text = "无关内容。" * 170 + "终于到了回民街。"
    assert len(text) > EVIDENCE_SEGMENT_CHARS
    evidence = scene_label_evidence("回民街", [_segment(text)])
    assert "回民街" in evidence
    assert len(evidence) <= EVIDENCE_SEGMENT_CHARS
