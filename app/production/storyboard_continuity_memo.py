"""分镜台 2.3.0：跨段连贯性备忘（用户拍板 2026-09-02，真实回归「人物白天说着
说着就变成黑夜了」驱动）。

背景：``_generate_all_segment_prompts`` 逐段独立调用模型（见
``app.production.storyboard_pack`` 模块 docstring 的 2.0.8 changelog），
已有的三层续接上下文（上一段 prompt_text 全文、最近几段镜头语言、色温弧线）
都没有显式约束「本段发生在什么时段」——时段只能靠模型读 prompt_text 全文自己
反推，真实回归显示模型会在没有任何原文依据的情况下把时段悄悄推进（白天写着
写着变成黑夜），比色温漂移更严重：色温是氛围，时段是「现在是几点」这个客观
事实，错了会让相邻两段的画面直接对不上。

做法与色温弧线（``storyboard_narrative_arc.segment_narrative_arc_rules``）
同一结构：模型自己在 ``continuity_memo`` 里报告本段结束时的时段与在场人物
状态，下一段调用时把上一段的备忘原样喂回去，默认要求逐字沿用，只有本段原文
明确写出时间推移才允许改变——且必须引用原文原话（``time_of_day_source_quote``），
不允许凭「剧情需要」自己判断该往前推进了。这是本模块存在的唯一理由：给
「时段」这个此前完全没有信号的维度补一条阻断式校验，同时把新增的模型/规则/
校验都放在这里，不占用 ``storyboard_pack.py`` 与
``_generate_all_segment_prompts`` 的行数预算（两者都已在
``app/FILE_CONVENTIONS.toml`` 的棘轮基线上，零余量）。

人物 location/wardrobe/emotion 三个字段搭车放进同一个备忘对象：跨段的人物
状态（在哪、穿什么、什么情绪）与时段是同一类「本段结束时的世界状态」，值得
同一次模型自报里一起收集，但闸门只做 advisory（不阻断）——见
``continuity_memo_character_advisories``，P0 范围只解决时段这一个真实回归
过的缺陷，人物状态先落库积累数据，不引入新的阻断风险。
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field


class _AiCharacterState(BaseModel):
    identity_id: str
    location: str = ""
    wardrobe: str = ""
    emotion: str = ""


class _AiContinuityMemo(BaseModel):
    """本段结束时的时段与人物状态，供下一段续接（只在生成期跨调用传递 +
    落库审计，不参与除时段外的任何阻断式校验——见模块 docstring）。
    """

    time_of_day: str = ""
    time_of_day_basis: Literal["source_text", "inherited", "inferred"] = "inferred"
    time_of_day_source_quote: str = ""
    characters: list[_AiCharacterState] = Field(default_factory=list)


def continuity_memo_payload(previous_memo: _AiContinuityMemo | None) -> dict[str, Any] | None:
    """task_payload["previous_continuity_memo"] 的值：本集第一段没有上一段，
    诚实地传 None，不伪造一个空备忘让模型误以为「上一段时段是空字符串」。
    """
    return previous_memo.model_dump(mode="json") if previous_memo is not None else None


def continuity_memo_rules(previous_memo: _AiContinuityMemo | None) -> list[str]:
    """continuity_memo 两条正面陈述：有上一段时的「默认沿用、原文驱动的改变」，
    没有上一段（本集第一段）时的「原文交代就引用、没交代就自行判断」。
    """
    if previous_memo is not None:
        return [
            (
                f"上一段记录的时段是「{previous_memo.time_of_day}」：本段默认逐字复制这个值到 "
                "continuity_memo.time_of_day，并把 time_of_day_basis 填 inherited；剧情氛围的"
                "变化（悲伤、紧张、追逐）不构成改变时段的理由，情绪交给色温与镜头语言表达，不要"
                "靠切换时段渲染氛围。只有本段 source_text_by_segment 的原文明确写出时间推移或"
                "时段变化（不限具体说法，例如提到天色、钟点、下一顿饭、夜幕等）时，才把 "
                "time_of_day 改成新值、time_of_day_basis 改成 source_text，并把 "
                "time_of_day_source_quote 填成本段原文里写明这次变化的那一句原话；这种情况下"
                "本段开头也要先画出时间过渡本身（光线变化、影子拉长、灯火次第亮起这类细节），"
                "不能直接从新时段的画面起手。"
            ),
            (
                "continuity_memo.characters 覆盖本段结束时所有在场人物，identity_id 与本段 "
                "resources.characters 保持一致；每个人物的 location/wardrobe/emotion 以上一段"
                "（continuity_memo）里同一人物的记录为起点，只有本段原文写到的具体动作或事件"
                "才能改变它们——没有原文依据就照抄上一段的值，不要凭空推进人物状态；wardrobe "
                "与 relevant_assets 的外观锚点不一致时，以外观锚点为准。"
            ),
        ]
    return [
        (
            "本段是本集第一段，没有上一段 continuity_memo 可以沿用：本段原文如果明确写出时段"
            "（清晨、正午、黄昏、深夜、三更……不限具体说法），就把 time_of_day 填成这个时段、"
            "time_of_day_basis 填 source_text，并把原文里写明时间的那句话逐字抄进 "
            "time_of_day_source_quote；原文没有写明时段时，由你自行判断一个合理的时段、"
            "time_of_day_basis 填 inferred，并在后续段落里保持这个判断，不要中途无端改变。"
        ),
        (
            "continuity_memo.characters 记录本段结束时所有在场人物的 location/wardrobe/"
            "emotion，identity_id 与本段 resources.characters 保持一致；wardrobe 与 "
            "relevant_assets 的外观锚点不一致时，以外观锚点为准。"
        ),
    ]


def continuity_memo_output_contract_text() -> str:
    """output_contract["continuity_memo"] 的说明文案，写法参照 camera_digest 那条。"""
    return (
        "本段结束时的时段与人物状态，供下一段续接：time_of_day 是本段画面的时段（开放词汇，"
        "不设枚举）；time_of_day_basis 说明这个时段是怎么来的——source_text=本段原文明确写出、"
        "inherited=逐字沿用上一段、inferred=没有上一段或原文都没交代时自行判断；"
        "time_of_day_basis=source_text 时 time_of_day_source_quote 必须是本段原文里写明时间"
        "的那句原话；characters[] 是本段结束时在场人物各自的 location/wardrobe/emotion，"
        "identity_id 必须来自本段 resources.characters。"
    )


def _normalize_for_quote_match(text: str) -> str:
    """空白归一后逐字比对——不做同义改写归并，「夜晚」与「深夜」必须视为不同。"""
    without_segment_tags = re.sub(r"\[段\d+\]\s*", "", text)
    return re.sub(r"\s+", "", without_segment_tags)


def _quote_found_in_source(quote: str, segment_source_text: str) -> bool:
    return _normalize_for_quote_match(quote) in _normalize_for_quote_match(segment_source_text)


def continuity_memo_errors(
    memo: _AiContinuityMemo,
    previous_memo: _AiContinuityMemo | None,
    segment_source_text: str,
) -> list[str]:
    """阻断式闸门：判据只挂在 continuity_memo 自己的数据与本段原文上（不做
    同义归并，沿用必须逐字相同——见模块 docstring）。人物字段不在这里检查，
    只做 advisory，见 ``continuity_memo_character_advisories``。
    """
    errors: list[str] = []
    if not memo.time_of_day.strip():
        errors.append("continuity_memo.time_of_day 不能为空：每一帧画面都有时段")
    if memo.time_of_day_basis == "inherited":
        if previous_memo is None:
            errors.append(
                "continuity_memo.time_of_day_basis=inherited，但本段是第一段、没有上一段可"
                "沿用：第一段只能是 inferred（自行判断）或 source_text（原文写明）。"
            )
        elif memo.time_of_day != previous_memo.time_of_day:
            errors.append(
                f"continuity_memo.time_of_day_basis=inherited 要求逐字复制上一段的 "
                f"time_of_day『{previous_memo.time_of_day}』，但本段写的是『{memo.time_of_day}』"
                "——沿用必须逐字相同，不允许改写成近义表述；如果本段确有时间推移，请改成 "
                "basis=source_text 并引用本段原文里写明时间变化的那句。"
            )
    elif memo.time_of_day_basis == "inferred" and previous_memo is not None:
        errors.append(
            f"continuity_memo.time_of_day_basis=inferred，但已有上一段时段"
            f"『{previous_memo.time_of_day}』：本段只能沿用（basis=inherited，逐字复制）或"
            "引用本段原文中写明时间变化的句子（basis=source_text），不能凭空重新判断。"
        )
    elif memo.time_of_day_basis == "source_text":
        quote = memo.time_of_day_source_quote.strip()
        if not quote:
            errors.append(
                "continuity_memo.time_of_day_basis=source_text，但 time_of_day_source_quote "
                "为空：必须逐字引用本段原文里写明时间的那句。"
            )
        elif not _quote_found_in_source(quote, segment_source_text):
            fallback = (
                f"沿用上一段『{previous_memo.time_of_day}』（basis=inherited）"
                if previous_memo is not None else "自行判断一个时段（basis=inferred）"
            )
            errors.append(
                f"continuity_memo.time_of_day_source_quote『{quote}』在本段原文里找不到逐字"
                f"匹配：只能是本段原文中真实存在的一句，不得改写或编造；如果本段其实没有时间"
                f"变化，请改成{fallback}。"
            )
    return errors


def continuity_memo_character_advisories(
    memo: _AiContinuityMemo, segment_character_ids: set[str],
) -> list[str]:
    """人物字段只做 advisory，不参与 chat_structured 的语义重试/失败判定
    ——写法与 ``storyboard_pack._segment_content_advisories`` 里其余
    ``[未拦截]`` 类信号一致（tag 名同源、可搜索）。"""
    return [
        f"[STORYBOARD_PACK_CONTINUITY_CHARACTER_UNKNOWN][未拦截] "
        f"continuity_memo.characters[{index}].identity_id=「{character.identity_id}」"
        "不在本段 resources.characters 内，无法确认这是哪个已登记角色的状态"
        for index, character in enumerate(memo.characters)
        if character.identity_id not in segment_character_ids
    ]
