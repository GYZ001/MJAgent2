"""分镜台阶段一：节拍表 + 分段（2.4.0 从 ``app.production.storyboard_pack``
搬移，见该模块 ``STORYBOARD_PACK_VERSION`` 的 2.4.0 changelog）。

拆出原因：``storyboard_pack.py`` 在 ``app/FILE_CONVENTIONS.toml`` 的
``line_count`` 棘轮基线上零余量（2072 行），本次要新增的句单元范围声明、
跨段台词去重两项闸门需要新行数。阶段一（节拍表草稿的 schema、生成、校验、
paratext 账处理）是一个自成一体、只在生成入口 ``generate_storyboard_pack``
被调用一次的子系统，纯搬移到这里不改变行为——``storyboard_pack.py`` 通过
``from .storyboard_beat_sheet import X as X`` 从这个真源再导出被测试直接
引用的名字，不借道任何第三方模块转手（CLAUDE.md「拆包用真包」）。

依赖方向是单向的：本模块不 import ``app.production.storyboard_pack`` 或
``storyboard_capacity_normalize``（那两个模块反过来 import 本模块的名字），
避免循环导入——``_generate_beat_sheet`` 因此把 ``contract_version`` 收成
显式参数，而不是像旧代码那样直接引用 ``storyboard_pack.
STORYBOARD_PACK_VERSION`` 模块常量。

2.4.0 新增：阶段一必须为每段引用的每个非 paratext 原文段号声明一段句单元
范围（``_AiSegmentPlan.source_unit_ranges``），校验交给
``app.production.storyboard_segment_ranges.segment_unit_range_errors``/
``kept_line_unit_binding_errors``；喂给模型的原文块从"整段原文"改成
"[段N·S07] 句子原文"的单元编号形式（``render_source_units``），让模型能够
用单元号而不是自然语言描述来划定每段该占哪一块；同时接入另一位代理新增的
``palette_scene_consistency_errors``（同一场戏的色温方向必须一致）。真实
故障与完整判据见 ``storyboard_segment_ranges`` 模块 docstring，不在这里
重复。时长口径不变：仍是固定 15 秒/段，不引入分档（用户拍板保留原设计）。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, Field

from app import config
from app.harness import model_gateway
from app.production.storyboard_context_segments import (
    context_only_segment_errors, context_segment_indexes, context_segment_rule,
)
from app.production.storyboard_dialogue_ledger import (
    DialogueQuote,
    _AiDroppedLine,
    _AiKeptLine,
    beat_sheet_dialogue_ledger_rules,
    dialogue_density_by_source_segment,
    dialogue_ledger_errors,
)
from app.production.storyboard_narrative_arc import (
    beat_sheet_narrative_arc_rules, palette_scene_consistency_errors,
)
from app.production.storyboard_segment_ranges import (
    _AiSourceUnitRange,
    _PARATEXT_PLACEHOLDER_TEXT,
    kept_line_unit_binding_errors,
    render_source_units,
    segment_unit_range_errors,
)
from app.source_excerpt import SourceSegment


class _AiBeat(BaseModel):
    beat_id: str
    summary: str
    segment_indexes: list[int] = Field(min_length=1)


class _AiSegmentPlan(BaseModel):
    segment_no: int
    synopsis: str
    source_segment_indexes: list[int] = Field(min_length=1)
    beat_ids: list[str] = Field(default_factory=list)
    #: 2.2.0 色温弧线：开放词汇，不设枚举；默认空串兼容模型截断导致的漏填。
    palette: str = ""
    #: 2.4.0：这一段对它引用的每个非 paratext 原文段号声明的句单元范围，
    #: 每个 source_segment_index 恰好一条；校验见 segment_unit_range_errors。
    source_unit_ranges: list[_AiSourceUnitRange] = Field(default_factory=list)


class _AiBeatSheetDraft(BaseModel):
    beat_sheet: list[_AiBeat] = Field(min_length=1)
    segments: list[_AiSegmentPlan] = Field(min_length=1)
    #: 2.1.0 对白台账：平铺列表，不用条件 schema；"逐一决定去留" 由
    #: _validate_beat_sheet_draft 的 dialogue_ledger_errors 检查兜底。
    kept_lines: list[_AiKeptLine] = Field(default_factory=list)
    dropped_lines: list[_AiDroppedLine] = Field(default_factory=list)


def _validate_beat_sheet_draft(
    draft: _AiBeatSheetDraft, *, source_segments: list[SourceSegment], dialogue_quotes: list[DialogueQuote],
    context_indexes: set[int] = frozenset(), paratext_indexes: set[int] = frozenset(),
) -> list[str]:
    """阶段一 blocking 校验：格式合法性 + 对白台账 + 2.4.0 单元范围/色温一致性。

    ``source_segments`` 2.4.0 起取代旧的 ``total_segments: int``——单元范围
    校验需要真实原文文本（算每个原文段有几个句单元），只给个数不够用；
    ``total_segments`` 本身仍然等价于 ``len(source_segments)``，不重复传参。
    """
    total_segments = len(source_segments)
    errors = context_only_segment_errors(draft.segments, set(context_indexes), set(paratext_indexes))
    beat_ids = {beat.beat_id for beat in draft.beat_sheet}
    if len(beat_ids) != len(draft.beat_sheet):
        errors.append("beat_sheet 中 beat_id 必须唯一")
    for beat in draft.beat_sheet:
        bad = [i for i in beat.segment_indexes if i < 1 or i > total_segments]
        if bad:
            errors.append(f"beat {beat.beat_id} 引用了不存在的原文段号 {bad}")
    expected_nos = list(range(1, len(draft.segments) + 1))
    actual_nos = [s.segment_no for s in draft.segments]
    if actual_nos != expected_nos:
        errors.append(f"segments[].segment_no 必须为连续递增 1..{len(draft.segments)}，当前为 {actual_nos}")
    for seg in draft.segments:
        bad = [i for i in seg.source_segment_indexes if i < 1 or i > total_segments]
        if bad:
            errors.append(f"段 {seg.segment_no} 引用了不存在的原文段号 {bad}")
        unknown_beats = [b for b in seg.beat_ids if b not in beat_ids]
        if unknown_beats:
            errors.append(f"段 {seg.segment_no} 引用了不存在的 beat_id {unknown_beats}")
    segment_source_indexes = {s.segment_no: s.source_segment_indexes for s in draft.segments}
    # 2.1.2：容量检查不在这里跑，改由 storyboard_capacity_normalize 兜底。
    errors.extend(undroppable_quote_errors(draft.dropped_lines, dialogue_quotes))
    errors.extend(dialogue_ledger_errors(
        quotes=dialogue_quotes,
        kept_lines=draft.kept_lines,
        dropped_lines=draft.dropped_lines,
        segment_source_indexes=segment_source_indexes,
        max_chars_per_segment=config.MAX_SPOKEN_CHARS_PER_SHOT,
        include_capacity=False,
    ))
    errors.extend(segment_unit_range_errors(draft.segments, source_segments, set(paratext_indexes)))
    errors.extend(kept_line_unit_binding_errors(draft.kept_lines, dialogue_quotes, draft.segments, source_segments))
    errors.extend(palette_scene_consistency_errors(draft.segments))
    return errors


def _manifest_brief_for_prompt(payload: dict[str, Any]) -> dict[str, Any]:
    """Compact asset_manifest summary handed to the model as light context.

    Only names/ids/segment_indexes -- not portrait binaries or provenance --
    so phase 1 (which only needs to recognize named entities while drafting
    the beat sheet) doesn't pay for the full manifest payload twice.
    """
    manifest = payload.get("asset_manifest") or {}
    return {
        "characters": [
            {
                "identity_id": c.get("identity_id"),
                "display_name": c.get("display_name"),
                "aliases": c.get("aliases") or [],
                "segment_indexes": c.get("segment_indexes") or [],
            }
            for c in (manifest.get("characters") or [])
        ],
        "scenes": [
            {
                "scene_id": s.get("scene_id"),
                "display_name": s.get("display_name"),
                "segment_indexes": s.get("segment_indexes") or [],
            }
            for s in (manifest.get("scenes") or [])
        ],
        "props": [
            {
                "label": p.get("label"),
                "segment_indexes": p.get("segment_indexes") or [],
            }
            for p in (manifest.get("props") or [])
        ],
    }


def _paratext_segment_indexes(payload: dict[str, Any]) -> set[int]:
    """映射台已经算好的 paratext 段号集合，直接读、不重新判定。

    旧契约分集兜底：``coverage_ledger`` 缺失、不是 dict，或者
    ``coverage_ledger.paratext`` 缺失、不是 list 时，返回空集——效果是
    ``_source_block_for_prompt`` 退化成"每个段号都不是 paratext"，即改造前的
    全量路径。绝不能把"账不存在"误读成"全部段落都是 paratext"（那会让模型看
    到的原文变成清一色占位符，属于比不过滤更差的静默失效）。
    """
    ledger = payload.get("coverage_ledger")
    if not isinstance(ledger, dict):
        return set()
    raw = ledger.get("paratext")
    if not isinstance(raw, list):
        return set()
    result: set[int] = set()
    for item in raw:
        try:
            result.add(int(item))
        except (TypeError, ValueError):
            continue
    return result


def _source_block_for_prompt(segments: list[SourceSegment], paratext_indexes: set[int]) -> str:
    """拼出喂给模型的原文文本块，paratext 段落原文替换成占位说明。

    2.4.0：改用 ``render_source_units`` 输出 "[段N·S07] 句子原文" 的单元编号
    形式（原来是整段一行）——模型必须用这些单元号来填 segments[].
    source_unit_ranges，不切开就没有单元号可引用。段号本身仍然照旧不重新
    编号（``source_segment_indexes``/``storyboard_source_bindings`` 整条链路
    都靠"段号=index_source_segments 的 1-based 位置"这个假设成立）。
    """
    lines = []
    for index, segment in enumerate(segments, start=1):
        placeholder = _PARATEXT_PLACEHOLDER_TEXT if index in paratext_indexes else None
        lines.append(render_source_units(index, segment.text, placeholder))
    return "\n".join(lines)


def _paratext_exclusion_rule(paratext_indexes: set[int]) -> str | None:
    """给 rules[] 用的显式禁令文案；没有 paratext 段落时返回 None（不往
    rules[] 里塞一条空列表的噪音）。"""
    if not paratext_indexes:
        return None
    return (
        f"以下原文段号是作者的话/非正文（映射台已判定为 paratext，原文已略去，"
        f"只剩占位说明）：{sorted(paratext_indexes)}——不得把它们编入任何 beat 的 "
        "segment_indexes 或任何 segment 的 source_segment_indexes，也不得据此"
        "编造情节"
    )


def _dialogue_targets_payload(dialogue_quotes: list[DialogueQuote]) -> dict[str, Any]:
    """阶段一 payload 里对白台账相关的两个字段：台账本身 + 2.1.2 新增的密度表。"""
    return {
        "dialogue_targets": [q.model_dump(mode="json") for q in dialogue_quotes],
        "dialogue_density_by_source_segment": dialogue_density_by_source_segment(
            dialogue_quotes, max_chars_per_segment=config.MAX_SPOKEN_CHARS_PER_SHOT,
        ),
    }


def _beat_sheet_rules(paratext_indexes: set[int], context_indexes: set[int] = frozenset()) -> list[str]:
    """阶段一 rules[]：段落归组形状要求 + 2.4.0 单元范围声明规则 + 2.1.0 对白
    台账正面陈述（见 beat_sheet_dialogue_ledger_rules）。
    """
    rules = [
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
        *beat_sheet_dialogue_ledger_rules(),
        *beat_sheet_narrative_arc_rules(),
    ]
    extra = (_paratext_exclusion_rule(paratext_indexes), context_segment_rule(set(context_indexes)))
    rules.extend(rule for rule in extra if rule is not None)
    return rules


async def _generate_beat_sheet(
    *,
    episode_id: str,
    episode_no: int,
    segments: list[SourceSegment],
    payload: dict[str, Any],
    dialogue_quotes: list[DialogueQuote],
    contract_version: str,
) -> _AiBeatSheetDraft:
    """``contract_version`` 由调用方传入（``storyboard_pack.
    STORYBOARD_PACK_VERSION``），不在本模块内引用那个模块级常量——本模块不
    import ``storyboard_pack``，避免与它对本模块的再导出构成循环导入。
    """
    paratext_indexes = _paratext_segment_indexes(payload)
    context_indexes = context_segment_indexes(payload)
    source_block = _source_block_for_prompt(segments, paratext_indexes)
    task_payload = {
        "task": (
            "通读本章原文，列出节拍表（beat_sheet）：每个节拍是一次情绪或信息的变化，"
            "不是一个句子；合并同质描写。然后把节拍按叙事单元归入段（一个段要能用"
            "一句话概括，例如「他扔掉了理想」「反派现身」），不是按时长平均切；段与段"
            "之间硬切。每段固定 15 秒、内含 2-4 个镜头（对话交锋段可用 2 镜正反打，"
            "叙事推进段用 3-4 镜；这不是你要填的字段，是下一步的产出约束，这里只需要"
            "正确分段）。每个段还要为它引用的每个原文段号声明具体占用了哪几个句单元"
            "（source_unit_ranges），同一原文段被多段引用时各段范围必须首尾相接、"
            "不回退、不留洞——见 rules。dialogue_targets 里的每一句原文台词都要显式"
            "决定去留，见 rules。"
        ),
        "rules": _beat_sheet_rules(paratext_indexes, context_indexes),
        "episode_no": episode_no,
        "known_assets": _manifest_brief_for_prompt(payload),
        "source_text_by_segment": source_block,
        **_dialogue_targets_payload(dialogue_quotes),
        "output_schema": _AiBeatSheetDraft.model_json_schema(),
    }
    fingerprint = hashlib.sha256(
        json.dumps(task_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:24]
    return await model_gateway.chat_structured(
        [
            {"role": "system", "content": "你是短剧分镜师。只输出符合 Schema 的一个 JSON 对象，不输出 Markdown或解释。"},
            {"role": "user", "content": json.dumps(task_payload, ensure_ascii=False)},
        ],
        model_type=_AiBeatSheetDraft,
        validate=lambda value: _validate_beat_sheet_draft(
            value, source_segments=segments, dialogue_quotes=dialogue_quotes,
            context_indexes=context_indexes, paratext_indexes=paratext_indexes,
        ),
        operation_id=f"storyboard_pack_beat_sheet_{episode_id}_{fingerprint}",
        max_tokens=6000,
        format_retry_limit=1,
        semantic_retry_limit=2,
        temperature=0.4,
        call_meta={
            "stage_key": "storyboard_pack_beat_sheet",
            "call_role": "storyboard_beat_sheet",
            "initiator_label": "分镜台节拍表",
            "episode_id": episode_id,
            "contract_version": contract_version,
        },
        repair_context=f"原文共 {len(segments)} 段，段号范围 1..{len(segments)}",
    )


#: 弃置只对语气词/寒暄这类短句成立；有说话人、正文超过这个字数的整句台词不是那三类。
DROPPABLE_MAX_CHARS = 4


def undroppable_quote_errors(dropped: list, quotes: list[DialogueQuote]) -> list[str]:
    """剧本格式抽出的整句台词（有说话人、正文超过 4 字）不能进 dropped_lines。

    EP1 第三次重跑实测：第一轮漏了 Q10 被打回后，模型把它塞进 dropped_lines、理由
    写「未在当前剧情节拍中保留」——这不是三类可弃置内容中的任何一种，只是把校验
    错误换了个地方。判据从数据推导：DialogueQuote.speaker 非空说明它是原文里的
    说话人行，content_chars 超过语气词长度说明它不是「啊」「哦」。
    """
    by_id = {quote.quote_id: quote for quote in quotes}
    errors: list[str] = []
    for item in dropped:
        quote = by_id.get(item.quote_id)
        if quote is None or not getattr(quote, "speaker", "") or quote.content_chars <= DROPPABLE_MAX_CHARS:
            continue
        errors.append(
            f"dropped_lines 里的 {quote.quote_id} 是 {quote.speaker} 的整句台词（{quote.content_chars} 字）"
            f"「{quote.text[:30]}」，不是语气词、寒暄或屏上文字，不能弃置；请放回 kept_lines 并分到覆盖它"
            "原文单元的段，容量装不下就新增段落"
        )
    return errors
