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

2026-09-03 扩展（用户投诉 EP1「橘座在上」成片相邻两段之间猫一会儿在车底、
一会儿在后备箱，猫包一会儿是网状背包、一会儿是透明背包驱动）：时段与人物
状态之外，「道具形态」与「人物/道具相对空间位置」是另外两类此前完全没有
信号的维度，同样搭车进这个备忘对象——``props`` 记录本段结束时每件关键道具
的外观（form）/位置（location）/状态（state），``layout`` 记录本段结束时
人物与人物、人物与家具的相对位置。与人物 location/wardrobe/emotion 不同，
这两类这次直接给阻断式校验（见 ``continuity_memo_errors`` 里的道具外观、
布局变化两条判据），因为它们正是本次真实投诉的根因，不是先落库积累数据的
阶段；判据形状照抄 ``time_of_day``/``time_of_day_source_quote`` 那一套——
默认逐字沿用，改变必须能在本段原文里逐字找到依据。
"""
from __future__ import annotations

import logging

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class _AiCharacterState(BaseModel):
    identity_id: str
    location: str = ""
    wardrobe: str = ""
    emotion: str = ""


class _AiPropState(BaseModel):
    """本段结束时一件关键道具的外观/位置/状态——见模块 docstring 2026-09-03
    扩展段：道具形态（form）不允许无原文依据地改变，位置（location）与内容物
    /开合这类状态（state）是剧情动作，正常随段变化。"""

    name: str
    form: str = ""
    location: str = ""
    state: str = ""


class _AiContinuityMemo(BaseModel):
    """本段结束时的时段/人物状态/道具状态/空间布局，供下一段续接（只在生成期
    跨调用传递 + 落库审计；time_of_day、props 的 form、layout 参与阻断式
    校验，其余字段只做 advisory——见模块 docstring）。
    """

    time_of_day: str = ""
    time_of_day_basis: Literal["source_text", "inherited", "inferred"] = "inferred"
    time_of_day_source_quote: str = ""
    characters: list[_AiCharacterState] = Field(default_factory=list)
    props: list[_AiPropState] = Field(default_factory=list)
    layout: str = ""
    layout_change_source_quote: str = ""


def continuity_memo_payload(previous_memo: _AiContinuityMemo | None) -> dict[str, Any] | None:
    """task_payload["previous_continuity_memo"] 的值：本集第一段没有上一段，
    诚实地传 None，不伪造一个空备忘让模型误以为「上一段时段是空字符串」。
    """
    return previous_memo.model_dump(mode="json") if previous_memo is not None else None


def _continuity_memo_rules_with_previous(previous_memo: _AiContinuityMemo) -> list[str]:
    """有上一段时的四条正面陈述：时段、人物状态、道具形态、空间布局各一条，
    都是「默认逐字沿用、只有本段原文明确写到才允许改变、改变要引用原文」的
    同一形状——拆成独立函数只是为了不撞单函数 50 行的文件规范红线，规则内容
    与 ``continuity_memo_rules`` 合并前完全一致。"""
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
        (
            "continuity_memo.props 覆盖本段结束时每件关键道具的外观（form，例如网状/透明、"
            "颜色材质）、位置（location，谁手里/哪把椅子上/桌面哪一侧）与状态（state，拉链"
            "开合、里面有没有猫）：默认逐字沿用上一段同名道具的 form/location/state；只有"
            "本段原文写到具体动作（拿起、放下、拉开、跳上、走到……这类动词）时，才允许改变"
            "对应道具的 location 或 state。道具的外观形态（form）在同一集内不允许无原文依据"
            "地改变——网状包不会自己变成透明包，除非本段原文明确写出更换道具本身这件事。"
        ),
        (
            "continuity_memo.layout 记录本段结束时人物与人物、人物与家具的相对位置，一两句"
            "话（例如「黄总站在长桌远端，李麦麦坐在近端角落，猫包在她左手边椅子上」）：默认"
            "逐字沿用上一段的 layout；只有本段原文写到具体的走动/移动/放置动作时才允许改变，"
            "改变时必须把 layout_change_source_quote 填成本段原文里写明这次移动的那一句原话"
            "——判据与 time_of_day_source_quote 完全一样，找不到逐字匹配会被判定为编造。"
        ),
    ]


def _continuity_memo_rules_first_segment() -> list[str]:
    """没有上一段（本集第一段）时的三条正面陈述：时段原文交代就引用、没
    交代就自行判断；人物状态与 props/layout 均由本段画面本身确定，供之后
    各段沿用。拆分理由同 ``_continuity_memo_rules_with_previous``。"""
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
        (
            "本段是本集第一段，同样没有上一段 props/layout 可以沿用：continuity_memo.props 由"
            "本段画面本身确定每件关键道具的外观（form）、位置（location）、状态（state），"
            "continuity_memo.layout 由本段画面本身确定人物与人物、人物与家具的相对位置；这两个"
            "字段一旦在本段定下，之后各段默认逐字沿用，不得无原文依据地改变。"
        ),
    ]


def continuity_memo_rules(previous_memo: _AiContinuityMemo | None) -> list[str]:
    """continuity_memo 正面陈述：有上一段时是「默认沿用、原文驱动的改变」
    （时段、人物状态、道具形态与空间布局各一条，见
    ``_continuity_memo_rules_with_previous``），没有上一段（本集第一段）时是
    「原文交代就引用、没交代就自行判断」+「本段画面确定 props/layout，之后
    各段沿用」（见 ``_continuity_memo_rules_first_segment``）。
    """
    if previous_memo is not None:
        return _continuity_memo_rules_with_previous(previous_memo)
    return _continuity_memo_rules_first_segment()


def continuity_memo_output_contract_text() -> str:
    """output_contract["continuity_memo"] 的说明文案，写法参照 camera_digest 那条。"""
    return (
        "本段结束时的时段与人物状态，供下一段续接：time_of_day 是本段画面的时段（开放词汇，"
        "不设枚举）；time_of_day_basis 说明这个时段是怎么来的——source_text=本段原文明确写出、"
        "inherited=逐字沿用上一段、inferred=没有上一段或原文都没交代时自行判断；"
        "time_of_day_basis=source_text 时 time_of_day_source_quote 必须是本段原文里写明时间"
        "的那句原话；characters[] 是本段结束时在场人物各自的 location/wardrobe/emotion，"
        "identity_id 必须来自本段 resources.characters；props[] 是本段结束时每件关键道具的"
        "外观（form，例如网状/透明、颜色材质）、位置（location，谁手里/哪把椅子上/桌面哪一"
        "侧）与状态（state，拉链开合、里面有没有猫）；layout 是本段结束时人物之间以及人物与"
        "家具的相对位置，一两句话；layout 与上一段不同时，layout_change_source_quote 必须是"
        "本段原文里写明这次移动/变化的那句原话。"
    )


def _normalize_for_quote_match(text: str) -> str:
    """空白归一后逐字比对——不做同义改写归并，「夜晚」与「深夜」必须视为不同。"""
    without_segment_tags = re.sub(r"\[段\d+\]\s*", "", text)
    return re.sub(r"\s+", "", without_segment_tags)


def _quote_found_in_source(quote: str, segment_source_text: str) -> bool:
    return _normalize_for_quote_match(quote) in _normalize_for_quote_match(segment_source_text)


def _prop_form_errors(
    memo: _AiContinuityMemo, previous_memo: _AiContinuityMemo | None,
) -> list[str]:
    """道具外观（form）阻断判据：同一集内无原文依据不允许改变，只挂在上一段
    与本段同名道具的 form 字段上。location/state 变化不在此列——那是剧情
    动作（拿起、放下、拉开），正常随段变化，不需要引用原文即可改变；上一段
    有的道具本段消失也不在此列（可能真的离场了）不做阻断。

    「上一段的道具本段消失、但 layout/props 都没交代它去哪了」本可以做成一条
    advisory，本次不做：判定"道具去哪了"需要先有"离场"的正面判据（谁把它带
    走了/它被放下留在原地了），而现有字段（location/state）只记录道具还在场
    时的状态，不记录道具退场这件事本身；勉强用"消失即报"会把大量正常收尾的
    段落（道具用完就不再提及）一起标记，变成新的一刀切噪音源。留给道具库
    工作流接入、有了更结构化的道具生命周期字段后再评估要不要做。"""
    if previous_memo is None:
        return []
    previous_forms = {prop.name: prop.form for prop in previous_memo.props if prop.form.strip()}
    errors: list[str] = []
    for prop in memo.props:
        previous_form = previous_forms.get(prop.name)
        if previous_form and prop.form.strip() and prop.form != previous_form:
            errors.append(
                f"continuity_memo.props『{prop.name}』的外观（form）从上一段的『{previous_form}』"
                f"变成了本段的『{prop.form}』：道具外观在同一集内不会无原文依据地改变，唯一修法"
                f"是把 form 改回『{previous_form}』沿用上一段；如果这件道具确实换了形态，当前"
                "判据不支持这种改变，请先确认本段是否真的写错了道具名。"
            )
    return errors


def layout_change_advisories(
    memo: _AiContinuityMemo,
    previous_memo: _AiContinuityMemo | None,
    segment_source_text: str,
) -> list[str]:
    """空间布局（layout）变化的**告警**判据（不阻断）。

    起初与 time_of_day 一样做成阻断（引用找不到就打回）。EP1 试验跑实测（2026-09-04）：
    段 4 的布局变化「把猫抱进猫包」在原文里没有一句能逐字引用——原文只写了「翻出旧猫包，
    拉开拉链」，猫进包是隐含的过场，模型三次都引用了自己写的提示词，整集分镜失败。
    这类合理推断的布局变化不是编造剧情，用「必须逐字引用」去拦会把一整集打死；改成
    记告警日志供观测，布局连贯性靠「默认逐字沿用」的正面规则与上一段画面参考去保证。
    道具外观（form）无据改变仍由 _prop_form_errors 阻断——那才是投诉的形态漂移。
    """
    if previous_memo is None or not previous_memo.layout.strip():
        return []
    if memo.layout == previous_memo.layout:
        return []
    quote = memo.layout_change_source_quote.strip()
    if not quote:
        return ["continuity_memo.layout 与上一段不同但没有给出 layout_change_source_quote（未拦截）"]
    if not _quote_found_in_source(quote, segment_source_text):
        return [f"continuity_memo.layout_change_source_quote『{quote}』在本段原文里找不到逐字匹配（未拦截）"]
    return []
    if memo.layout == previous_memo.layout:
        return []
    quote = memo.layout_change_source_quote.strip()
    if not quote:
        return [
            "continuity_memo.layout 与上一段不同，但 layout_change_source_quote 为空：必须"
            "逐字引用本段原文里写明这次移动/变化的那句话；如果本段布局其实没有变化，请把 "
            "layout 改回与上一段逐字相同。"
        ]
    if not _quote_found_in_source(quote, segment_source_text):
        return [
            f"continuity_memo.layout_change_source_quote『{quote}』在本段原文里找不到逐字"
            "匹配：只能是本段原文中真实存在的一句，不得改写或编造；如果本段布局其实没有变化，"
            "请把 layout 改回与上一段逐字相同。"
        ]
    return []


def continuity_memo_errors(
    memo: _AiContinuityMemo,
    previous_memo: _AiContinuityMemo | None,
    segment_source_text: str,
) -> list[str]:
    """阻断式闸门：判据只挂在 continuity_memo 自己的数据与本段原文上（不做
    同义归并，沿用必须逐字相同——见模块 docstring）。人物字段不在这里检查，
    只做 advisory，见 ``continuity_memo_character_advisories``。2026-09-03
    追加道具外观（``_prop_form_errors``，阻断）与空间布局（``layout_change_advisories``，只告警）
    两条阻断判据。
    """
    errors: list[str] = []
    errors.extend(_prop_form_errors(memo, previous_memo))
    for advisory in layout_change_advisories(memo, previous_memo, segment_source_text):
        log.warning("[STORYBOARD_CONTINUITY_MEMO_LAYOUT][未拦截] %s", advisory)
    if not memo.time_of_day.strip():
        errors.append("continuity_memo.time_of_day 不能为空：每一帧画面都有时段")
    if memo.time_of_day_basis == "inherited":
        if previous_memo is None:
            errors.append(
                "continuity_memo.time_of_day_basis=inherited，但本段是第一段、没有上一段可"
                "沿用：第一段只能是 inferred（自行判断）或 source_text（原文写明）。"
            )
        elif memo.time_of_day != previous_memo.time_of_day:
            _inherit_time_of_day(memo, previous_memo, f"basis=inherited 却写成近义表述『{memo.time_of_day}』")
    elif memo.time_of_day_basis == "inferred" and previous_memo is not None:
        _inherit_time_of_day(memo, previous_memo, "已有上一段时段却 basis=inferred 重新判断")
    elif memo.time_of_day_basis == "source_text":
        quote = memo.time_of_day_source_quote.strip()
        if not quote or not _quote_found_in_source(quote, segment_source_text):
            # 2026-09-05 第 13 集：引文『光线从暖调白日缓慢渐变到正午暖金』是编造的，打回三次仍如此，
            # 整集失败。没有逐字原文证据就等于「本段没写明时间变化」——按错误提示自己给出的出路
            # 确定性处理：有上一段就沿用（inherited），第一段就退回自行判断（inferred）。
            if previous_memo is not None:
                _inherit_time_of_day(memo, previous_memo, f"time_of_day_source_quote『{quote}』在本段原文里找不到逐字匹配")
            else:
                log.info("[STORYBOARD_CONTINUITY_MEMO_REPAIR] 第一段引文『%s』找不到逐字匹配，退回 inferred", quote)
                memo.time_of_day_basis = "inferred"
                memo.time_of_day_source_quote = ""
    return errors


def _inherit_time_of_day(memo: _AiContinuityMemo, previous_memo: _AiContinuityMemo, why: str) -> None:
    """时段沿用上一段（逐字）、basis=inherited、清空引文，并记一条修补日志。"""
    log.info("[STORYBOARD_CONTINUITY_MEMO_REPAIR] %s → 沿用上一段『%s』", why, previous_memo.time_of_day)
    memo.time_of_day = previous_memo.time_of_day
    memo.time_of_day_basis = "inherited"
    memo.time_of_day_source_quote = ""


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
