"""短剧节奏档 schema 的忠实档冻结契约：`_AiBeatSheetDraft`/`_AiBeat`/
`_beat_sheet_rules`/`_generate_beat_sheet` 在 ``adaptation_mode="faithful"``
下必须与改造前逐字节相同——不能因为新增短剧档而让忠实档多长出一个字段或
一条规则。

指纹独立观察点（CLAUDE.md Investigation「验证要有独立观察点」）：改代码前
用 ``git show HEAD:app/production/storyboard_beat_sheet.py`` 取出修改前的源码，
在隔离命名空间里 exec 出一份"旧" `_generate_beat_sheet`，用同一个固定夹具和
打桩的 ``model_gateway.chat_structured`` 分别跑旧/新两版代码，比对捕获到的
``operation_id``（其尾部 24 位十六进制是 task_payload 的 sha256 指纹）——
两者逐字节相同：``996ff5821340998719d0e080``。这个值就是本文件下面
``_EXPECTED_FAITHFUL_FINGERPRINT`` 的来源，工具脚本见改造记录，不随本文件
提交（避免把 exec() 装载旧代码这种一次性手法留在正式测试里）。
"""
from __future__ import annotations

import json

import pytest

from app.harness import model_gateway
from app.production.storyboard_beat_sheet import _AiBeat, _AiBeatSheetDraft, _generate_beat_sheet
from app.production.storyboard_dialogue_ledger import DialogueQuote
from app.production.storyboard_short_drama_schemas import (
    _AiDroppedSourceSpan,
    _AiShortDramaBeat,
    _AiShortDramaBeatSheetDraft,
)
from app.source_excerpt import SourceSegment

# 见模块 docstring：改代码前用隔离命名空间跑旧代码得到的参考值，改完后断言不变。
_EXPECTED_FAITHFUL_FINGERPRINT = "996ff5821340998719d0e080"


def _fixture_segments() -> list[SourceSegment]:
    return [
        SourceSegment(segment_id="s1", text="孟浩扔掉了葫芦。他喃喃自语：“我命由我不由天。”", start_offset=0, end_offset=10),
        SourceSegment(segment_id="s2", text="黄总抓起猫，猫“喵”地叫了一声。", start_offset=10, end_offset=20),
    ]


def _fixture_payload() -> dict:
    return {
        "asset_manifest": {
            "characters": [{"identity_id": "bible:c1", "display_name": "孟浩", "aliases": [], "segment_indexes": [1]}],
            "scenes": [], "props": [],
        },
        "coverage_ledger": {"paratext": []},
    }


def _fixture_quotes() -> list[DialogueQuote]:
    return [DialogueQuote(quote_id="Q01", source_segment_index=1, text="我命由我不由天。", content_chars=8, speaker="孟浩")]


@pytest.mark.asyncio
async def test_faithful_operation_id_fingerprint_unchanged(monkeypatch):
    captured = {}

    async def _stub(*args, **kwargs):
        captured["operation_id"] = kwargs["operation_id"]
        return _AiBeatSheetDraft(
            beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1])],
            segments=[{"segment_no": 1, "synopsis": "x", "source_segment_indexes": [1]}],
        )

    monkeypatch.setattr(model_gateway, "chat_structured", _stub)
    await _generate_beat_sheet(
        episode_id="ep_fingerprint_test", episode_no=1, segments=_fixture_segments(), payload=_fixture_payload(),
        dialogue_quotes=_fixture_quotes(), contract_version="2.4.1", adaptation_mode="faithful",
    )
    fingerprint = captured["operation_id"].rsplit("_", 1)[-1]
    assert fingerprint == _EXPECTED_FAITHFUL_FINGERPRINT


@pytest.mark.asyncio
async def test_short_drama_operation_id_fingerprint_differs_from_faithful(monkeypatch):
    """短剧档的 task_payload（新增 rules 与 output_schema）必须与忠实档不同——
    否则短剧档规则根本没被接进去。"""
    captured = {}

    async def _stub(*args, **kwargs):
        captured["operation_id"] = kwargs["operation_id"]
        return _AiShortDramaBeatSheetDraft(
            beat_sheet=[_AiShortDramaBeat(beat_id="B1", summary="x", segment_indexes=[1], importance="key")],
            segments=[{"segment_no": 1, "synopsis": "x", "source_segment_indexes": [1]}],
        )

    monkeypatch.setattr(model_gateway, "chat_structured", _stub)
    await _generate_beat_sheet(
        episode_id="ep_fingerprint_test", episode_no=1, segments=_fixture_segments(), payload=_fixture_payload(),
        dialogue_quotes=_fixture_quotes(), contract_version="2.4.1", adaptation_mode="short_drama",
    )
    fingerprint = captured["operation_id"].rsplit("_", 1)[-1]
    assert fingerprint != _EXPECTED_FAITHFUL_FINGERPRINT


def test_faithful_schema_has_no_short_drama_fields():
    """忠实档 output_schema 里不能出现 importance/dropped_source_spans 字样——
    schema 允许的，校验就不许拒绝，反过来 schema 不该出现字段本不该存在的
    诱导（CLAUDE.md Prompts「schema 允许的，校验就不许拒绝」）。"""
    schema_text = json.dumps(_AiBeatSheetDraft.model_json_schema(), ensure_ascii=False)
    assert "importance" not in schema_text
    assert "dropped_source_spans" not in schema_text
    assert set(_AiBeatSheetDraft.model_fields.keys()) == {"beat_sheet", "segments", "kept_lines", "dropped_lines"}
    assert set(_AiBeat.model_fields.keys()) == {"beat_id", "summary", "segment_indexes"}


def test_short_drama_schema_requires_explicit_importance_and_has_dropped_spans():
    schema = _AiShortDramaBeatSheetDraft.model_json_schema()
    assert "dropped_source_spans" in _AiShortDramaBeatSheetDraft.model_fields
    beat_importance_required = "importance" in _AiShortDramaBeat.model_json_schema().get("required", [])
    assert beat_importance_required, "importance 不给默认值，模型必须显式分类"
    with pytest.raises(Exception):
        _AiShortDramaBeat(beat_id="B1", summary="x", segment_indexes=[1])  # 缺 importance 必须拒绝
    assert schema  # schema 本身可正常生成，不抛异常


def test_dropped_source_span_reason_is_required():
    with pytest.raises(Exception):
        _AiDroppedSourceSpan(source_segment_index=1, from_unit=1, to_unit=2, reason="")


def test_faithful_rules_are_historically_unchanged():
    """把 2026-09-23 改造前 _beat_sheet_rules() 的原始规则文本逐字抄进来做独立
    比对（CLAUDE.md「手写一份修复前的函数体副本」），而不是只信任新函数自己
    的输出。"""
    from app.production.storyboard_beat_sheet import _beat_sheet_rules
    from app.production.storyboard_dialogue_ledger import beat_sheet_dialogue_ledger_rules
    from app.production.storyboard_narrative_arc import beat_sheet_narrative_arc_rules

    historical_head = [
        "beat_sheet[].segment_indexes 与 segments[].source_segment_indexes 必须引用"
        "下方原文自带的 [段N] 编号，不得虚构或越界",
        "segments[].segment_no 必须从 1 开始连续递增",
        "segments[].synopsis 用一句话概括这个段落在讲什么",
        "段落数量由节拍的叙事单元数量决定，不是按原文段数或时长机械平分；剧情"
        "密度高、台词多的地方应该拆成更多段，宁多勿少，不要为了少分段而压缩剧情",
        "每段固定 15 秒、内含 2-4 个镜头：一个段必须承载足够撑满 15 秒的内容"
        "（多个节拍、一次完整的动作链、或一段对话交锋），内容单薄、不足以撑满"
        "15 秒的相邻节拍要合并进同一段，不要为了凑段数而把一场戏拆得支离破碎——"
        "只有台词容量（见下方规则）确实装不下时，才允许把同一场戏拆成多段",
        "原文自带的 [段N·S07] 是句单元编号（S 从 1 开始）：你引用的每一个非"
        "paratext 原文段号，都必须在这个段的 source_unit_ranges 里给出恰好一条 "
        "{source_segment_index, from_unit, to_unit} 范围，标出这一段具体占用了"
        "这个原文段的哪几个句单元——同一原文段落被拆给多个段时，各段必须各占"
        "一块不重叠的原文，不能让好几段都拿到同一整段原文、只靠 synopsis 描述"
        "区分该拍哪一块",
        "同一个原文段号被多个段引用时，按 segment_no 顺序看这些段各自声明的"
        "范围：后一段的 from_unit 必须大于等于前一段的 to_unit（允许两段共享"
        "恰好一个边界单元，不允许倒退，也不允许大段重叠）；这些段声明的范围"
        "合并起来必须覆盖这个原文段的全部句单元，不能留洞——洞意味着那几句话"
        "没有任何一段负责拍，等于被静默删掉了",
        "内心独白/叙述性交代如果既无法转成画面、也不影响读者理解因果，可以不进入"
        "任何节拍；但凡是承载因果关系、人物动机或关键设定的内心独白/叙述性交代，"
        "不能因为「无法视觉化」直接丢弃——保留进节拍，下一步会把它改写成一句简短的"
        "角色画外音说出来（这属于内容改编，不算 dialogue_targets 里的引号台词）",
    ]
    expected = [*historical_head, *beat_sheet_dialogue_ledger_rules(), *beat_sheet_narrative_arc_rules()]
    assert _beat_sheet_rules(set(), adaptation_mode="faithful") == expected
