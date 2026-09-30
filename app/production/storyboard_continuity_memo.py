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

2026-09-28 两处修法（《顾念长安（第二版）》EP1 实测）：① travel_direction
此前也走「默认沿用上一段」的形状，与 wardrobe/layout 同一套逻辑——但走位是
一次性动作不是持续状态，默认继承会把陈旧走位钉进后续多段提示词、还常与
本段正文自己写的静止描述矛盾；规则文案与回填判据改写后放进新拆的
``app.production.storyboard_travel_direction``（本文件已在 500 行硬顶，
拆分原因见该模块 docstring），本文件只保留 ``travel_direction`` 字段本身。
② ``continuity_memo_character_advisories`` 的 identity_id
比对只做精确字符串相等，模型在 ``continuity_memo.characters`` 里偶尔省略
resources.characters 已解析出的 ``bible:``/``entity:`` 前缀，会被误判成
「不在本段内」，与实际数据矛盾；比对改成去前缀后的主体值也参与匹配。
"""
from __future__ import annotations

import logging

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.production.storyboard_travel_direction import travel_direction_rule

log = logging.getLogger(__name__)

#: wardrobe 字段的正面陈述规则（2026-09-28 真实回归：模型把「外观锚点未写服装（二十
#: 余岁青年男性，身形高挑，留乌黑短发）」这类内部说明文字原样写进 wardrobe，被回填进
#: 提示词发给了视频模型）——规定只写什么、遇到锚点没服装信息时给出「留空」的出路，
#: 不靠关键词黑名单去猜模型写的文字是不是服装。2026-09-29（P0-D）追加一句：全集服装表
#: （见 app.production.storyboard_wardrobe_plan）上线后，跨段服装连贯性的权威从「默认
#: 沿用上一段 continuity_memo」变成「全集服装表规划的本段着装」——本段任务里如果给出了
#: 「本段着装（全集服装表）」/「本段内着装变化」规则，wardrobe 必须以它为准；
#: continuity_memo 自己退回"如实记录本段结束时画面上实际呈现的样子"这个角色，不再是
#: 跨段权威本身，不能再靠「默认沿用上一段」压掉计划里安排的换装。
_WARDROBE_FIELD_RULE = (
    "wardrobe 字段只写本段画面里实际能看到的具体服装/配饰（款式、颜色、材质、佩戴方式，"
    "例如「米白色针织开衫，内搭浅蓝色碎花长裙」「颈间绕着深灰色围巾」）；本段任务里如果给出"
    "了「本段着装（全集服装表）」或「本段内着装变化」规则，wardrobe 必须以它为准——全集"
    "服装表是跨段权威，不能因为「默认沿用上一段」而忽略计划里安排的换装。没有全集服装表"
    "规则可参考时，relevant_assets 的外观锚点（appearance）如果已经写明服装，本集第一次"
    "出场时沿用锚点原文里的服装描述，之后各段沿用 continuity_memo 里的记录。外观锚点没有"
    "写服装信息、且本段画面也没有交代任何服装细节时，wardrobe 留空——不要转述外观锚点里的"
    "体貌特征（年龄、身高、发型这类不是服装的信息），也不要写「外观锚点未写服装」这类"
    "说明性文字，空着才是诚实的：这个字段目前确实没有可见服装信息。只正面描述此刻确实穿着/"
    "佩戴的东西，不写「没有穿……」「不再系……」这类否定句式——视频模型不理解否定，看到"
    "「开衫」「围巾」这些词就会把它画出来；某件东西这段不再穿了，只需要不再提它、直接描述"
    "现在穿的是什么，不必专门声明它的缺席。某件随身物/配饰如果贴身佩戴、被外层衣物盖住看"
    "不见（例如「隔着卫衣贴胸挂着」），不要写成外层衣物上能看见的花纹或图案——按「衣服下"
    "看不见」处理，能不写就不写。"
)


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
    # 屏幕行进方向：本段镜头里真实发生的位移（例如「一行人自画左向画右沿山路行进」），
    # 没有位移写「静止」；不像 wardrobe/layout 那样默认沿用上一段——规则文案与回填判据见
    # app.production.storyboard_travel_direction（拆分原因见该模块 docstring）。
    travel_direction: str = ""


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
            "resources.characters 保持一致；每个人物的 location/emotion 以上一段"
            "（continuity_memo）里同一人物的记录为起点，只有本段原文写到的具体动作或事件"
            "才能改变它们——没有原文依据就照抄上一段的值，不要凭空推进人物状态。"
            + _WARDROBE_FIELD_RULE
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
        travel_direction_rule(previous_memo.travel_direction),
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
            "continuity_memo.characters 记录本段结束时所有在场人物的 location/emotion，"
            "identity_id 与本段 resources.characters 保持一致。"
            + _WARDROBE_FIELD_RULE
        ),
        (
            "本段是本集第一段，同样没有上一段 props/layout 可以沿用：continuity_memo.props 由"
            "本段画面本身确定每件关键道具的外观（form）、位置（location）、状态（state），"
            "continuity_memo.layout 由本段画面本身确定人物与人物、人物与家具的相对位置；这两个"
            "字段一旦在本段定下，之后各段默认逐字沿用，不得无原文依据地改变。"
        ),
        travel_direction_rule(""),
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
        "identity_id 必须来自本段 resources.characters；wardrobe 只写画面里实际可见的具体"
        "服装/配饰，没有可见服装信息时留空，不写体貌特征或说明性文字；props[] 是本段结束时每件关键道具的"
        "外观（form，例如网状/透明、颜色材质）、位置（location，谁手里/哪把椅子上/桌面哪一"
        "侧）与状态（state，拉链开合、里面有没有猫）；layout 是本段结束时人物之间以及人物与"
        "家具的相对位置，一两句话；layout 与上一段不同时，layout_change_source_quote 必须是"
        "本段原文里写明这次移动/变化的那句原话；travel_direction 是本段镜头里真实发生的屏幕"
        "行进方向（例如「一行人自画左向画右沿山路行进」），没有位移写「静止」——只描述本段"
        "自己的位移，不是上一段的默认延续，哪怕恰好与上一段方向相同也要由本段画面自行确认。"
    )


def _normalize_for_quote_match(text: str) -> str:
    """空白归一后逐字比对——不做同义改写归并，「夜晚」与「深夜」必须视为不同。

    原文窗口 2.4.0 起按句单元渲染成「[段N·S07] 句子」（storyboard_segment_ranges.
    render_source_units），旧格式是「[段N] 整段」；两种标签都要剥掉，否则跨句的引用
    中间夹着单元标签就永远匹配不上。2026-09-14 我欲封天第 1–3 集 19 条「找不到逐字匹配
    （未拦截）」实测：9 条是这种假阳性，另 9 条是模型把两句压缩拼接（丢了中间小句），
    剥标签只消除前者，后者仍如实告警。
    """
    without_segment_tags = re.sub(r"\[段\d+(?:·S\d+)?\]\s*", "", text)
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


def _identity_id_core(identity_id: str) -> str:
    """identity_id 去掉 ``bible:``/``entity:`` 前缀后的主体。

    整个身份体系的前缀集合是封闭的（只有这两种，见 ``app.schemas.segment_
    identity``），从数据结构本身推导，不是猜测式关键词枚举——与
    ``storyboard_reference_repair._entry_names``、``storyboard_dialects.
    reference_mention_errors`` 里 ``identity_id.split(":", 1)`` 同一种归一。
    """
    return identity_id.split(":", 1)[-1].strip() if identity_id else identity_id


def continuity_memo_character_advisories(
    memo: _AiContinuityMemo, segment_character_ids: set[str],
) -> list[str]:
    """人物字段只做 advisory，不参与 chat_structured 的语义重试/失败判定
    ——写法与 ``storyboard_pack._segment_content_advisories`` 里其余
    ``[未拦截]`` 类信号一致（tag 名同源、可搜索）。

    2026-09-28 修正误判：identity_id 比对此前只认精确字符串相等，模型在
    ``continuity_memo.characters`` 里偶尔省略 resources.characters 已解析出的
    ``bible:``/``entity:`` 前缀（例如写「顾屿」而不是「bible:顾屿」），会被判成
    「不在本段内」，但这个人物明明在场——只是前缀被省略；比对同时看去前缀后的
    主体值，真正不在场的人物才报。
    """
    known_cores = {_identity_id_core(cid) for cid in segment_character_ids}
    return [
        f"[STORYBOARD_PACK_CONTINUITY_CHARACTER_UNKNOWN][未拦截] "
        f"continuity_memo.characters[{index}].identity_id=「{character.identity_id}」"
        "不在本段 resources.characters 内，无法确认这是哪个已登记角色的状态"
        for index, character in enumerate(memo.characters)
        if character.identity_id not in segment_character_ids
        and _identity_id_core(character.identity_id) not in known_cores
    ]


def _wardrobe_line_pattern(name: str) -> re.Pattern[str]:
    """某角色「续接服装：@名字 ……。」整行；幂等判断按角色识别既有写法，不按逐字字符串。

    量词用非贪婪 ``[^\\n]*?``（同 ``storyboard_cast_lock._CAST_LOCK_SENTENCE_PATTERN``
    的写法）只吃到第一个句号为止：真实回归里「续接服装：……。」经常与同一物理行里
    其它句子挤在一起，贪婪量词会一路吃到该行最后一个句号，把后面无关内容一并
    静默删掉。
    """
    return re.compile(rf"\n?续接服装：@{re.escape(name)} [^\n]*?。")


def _character_appearance_anchor(prompt: str, name: str) -> str:
    """从 ``prompt_text`` 里模型自己写的「@{name} 的外观：……。」提取外观锚点原文；
    提取不到（没写，或本段不是这个角色的出场镜）返回空串。数据来自提示词自身，
    供 ``_wardrobe_not_clothing_advisory`` 判断 wardrobe 是不是在转述体貌特征。"""
    match = re.search(rf"@{re.escape(name)}\s*的外观：([^。]+)。", prompt)
    return match.group(1).strip() if match else ""


def _wardrobe_not_clothing_advisory(name: str, wardrobe: str, anchor: str) -> None:
    """软检查（不阻断、不静默吞、不兜底改写）：wardrobe 与外观锚点高度重合时多半是
    在复述体貌特征或写说明性文字，记一条告警供人工核查——判据是与本段自己已有
    数据的重合度，不是关键词猜测。"""
    if anchor and (anchor in wardrobe or wardrobe in anchor):
        log.warning(
            "[STORYBOARD_WARDROBE_NOT_CLOTHING][未拦截] continuity_memo.characters「%s」的 "
            "wardrobe「%s」与其外观锚点「%s」高度重合，像是在复述体貌特征或写说明性文字，"
            "不是具体服装描述，请人工核对分镜提示词", name, wardrobe[:60], anchor[:60],
        )


def ensure_wardrobe_continuity_in_prompt(draft: Any, *, prop_factory: Any) -> list[str]:
    """服装延续的确定性回填：``continuity_memo.characters[].wardrobe``（规则见
    ``_WARDROBE_FIELD_RULE``）写进提示词末尾，按正名一句一行；并登记进
    ``resources.props``。``prop_factory`` 由调用方传入其构造器，避免循环导入。

    幂等判断按角色识别既有写法，不按逐字字符串（2026-09-28 改版，真实回归同一角色
    的服装描述被写了两次、只差几个字）：先剥掉该角色此前写入的整行，若剩下的正文
    本身已经提到这件服装就不再补一行，否则写回最新一行——见 ``_wardrobe_line_pattern``。
    """
    memo = getattr(draft, "continuity_memo", None)
    characters = getattr(memo, "characters", None) or []
    prompt = str(getattr(draft, "prompt_text", "") or "")
    if not characters or not prompt.strip():
        return []
    name_by_id = {
        c.identity_id: c.display_name
        for c in getattr(draft.resources, "characters", None) or []
        if getattr(c, "visibility", "") == "visible" and str(getattr(c, "display_name", "") or "").strip()
    }
    appended: list[str] = []
    changed = False
    for character in characters:
        name = name_by_id.get(character.identity_id)
        wardrobe = character.wardrobe.strip()
        if not name or not wardrobe:
            continue
        _wardrobe_not_clothing_advisory(name, wardrobe, _character_appearance_anchor(prompt, name))
        # 先剥掉这个角色此前写入的整行（不论文字是否与本次相同），再判断剩下的正文本身
        # 是否已经用别的方式提到这件服装——两者都成立时优先信正文，不重复追加一行。
        deduped = _wardrobe_line_pattern(name).sub("", prompt).rstrip()
        normalized = deduped if wardrobe in deduped else deduped + f"\n续接服装：@{name} {wardrobe}。"
        if normalized != deduped:
            appended.append(name)
        if normalized != prompt:
            prompt = normalized
            changed = True
        if not any(wardrobe in (str(getattr(p, "description", "") or "")) for p in draft.resources.props):
            draft.resources.props.append(prop_factory(label=f"{name}的服装", description=wardrobe))
    if changed:
        draft.prompt_text = prompt
    if appended:
        log.info("[STORYBOARD_WARDROBE_APPENDED] 提示词缺服装延续，已按备忘追加：%s", "、".join(appended))
    return []

