"""P0-D：全集服装表（wardrobe_plan）——跨段服装/配饰连贯性的全集视野规划
（2026-09-29，真实生产回归：proj_ca86b15ab7d7 EP1，28 段逐段生成）。

背景：跨段服装连贯性此前完全靠 ``app.production.storyboard_continuity_memo``
的"默认沿用上一段"实现——每段只看得见上一段的备忘和自己这一段的原文，天生
只能把服装状态原样复制到下一段，既不知道"这段该不该换装"，也不知道"后面
某段要用到的东西现在该不该先带在身上"。三处真实故障直接由此产生：
① 温念在第 12 段被顾屿围上围巾（原文只写"绕在她脖子上"，没写摘下），
   围巾与开衫扣子就被逐段续接到她到家（第 15 段）、上床睡觉（20-23 段，
   穿着系扣开衫系着围巾睡觉）、次日做早饭（24-28 段，围裙外面还系着围巾）；
② 顾屿在第 12 段解下"自己的"围巾，但第 4-11 段（咖啡馆/巷子/走廊，逐段
   独立生成、互相看不见）从未让他围过围巾——围巾凭空出现；
③ 第 14 段温念身边突然多出一只水浸行李箱，而第 11-13 段原文明确写两人
   还在门槛外，没有任何镜头交代箱子被搬出来。
根因是同一个：没有全集范围的服装/道具规划，逐段续接只能"复制"，不能
"预见"。

设计与 ``app.production.storyboard_beat_foreshadowing``（伏笔/类型信号保全，
P0-C）结构同构：阶段一模型在 ``_AiBeatSheetDraft.wardrobe_plan``
（``_AiWardrobeState`` 列表，见 ``storyboard_beat_sheet_schemas``）里逐条
提名"这个人物在这个节拍开始穿这套衣服/为什么换"，本模块负责把这份全集
计划按段拆解成阶段二的规则文本，并核验提名是否指向真实存在的人物/节拍。

与 causality/foreshadowing 的一处刻意不同：那两者的核验走
``EmotionalTurnSoftCheck``/``ForeshadowingSoftCheck`` 多轮打回模型重试、
最后一次放行不再报错，无效提名仍原样留在 draft 里，只在事后 advisory 里
体现。全集服装表/道具入场计划的提名频次低（一个人物一集通常只换装
一两次），不值得为它单开一轮 chat_structured 重试预算；本模块选择更简单
的策略——引用了未知 identity_id/beat_id 的条目在构造推进状态那一刻就被
剔除、不参与任何段的着装推进（``build_wardrobe_state``），同时记一条
``log.warning``（不静默）供人工核查，事后 ``wardrobe_plan_summary`` 里也能
看到这个数字（同一份判据复用，不重复实现）——CLAUDE.md「不得兜底填充」：
剔除之后就是"这个人物这段没有计划外的着装信息"，不会拿一个编造的默认值
顶替。

2026-10-01「人物谱默认造型」误用修复（真实回归 proj_ca86b15ab7d7《顾念长安》
EP1，视频台把这段全集服装表拿去决定定妆照送全身照还是头像照之后发现）：
``WardrobePlanState`` 此前把每个人物在 ``wardrobe_plan`` 里的"第一条记录"
无条件当成"人物谱定妆照默认造型"，这个假设只在"阶段一规则第一条——锚点
写了服装就逐字沿用锚点"真的被遵守时才成立。顾屿的人物谱外观锚点只有
「二十余岁青年男性，身形高挑，留乌黑短发」，没有任何服装信息；阶段一规则
在锚点没写服装时明确要求模型"按原文对这个人物当下情境的描写给出一套合理
装扮"——这是正当行为，不是模型犯错，但产出的"大衣+深灰围巾"纯属按情境
虚构，不是任何人验证过的"默认造型"。旧逻辑把这条虚构记录当成默认造型，
会让视频台把灰衬衫定妆照（出图模型自己补的、与这件虚构大衣毫无关系）当
"默认造型吻合"全身照一起送进生成，灰衬衫因此混进了成片画面。温念的锚点
写明了服装（米白色针织开衫……），她的第一条记录确实逐字取自锚点，判定
本就是对的。

修法：default_flags 只对"第一条记录的着装文字确实是外观锚点的逐字（连续
子串，不是关键词/服装词表）产物"的人物给 True 的机会，见
``default_look_grounded_identity_ids``/``_wardrobe_grounded_in_appearance``。
比对用的锚点文本优先取 ``storyboard_physical_anchor.apply_physical_anchor_
overrides`` 覆盖前留下的快照（``appearance_at_beat_sheet``）——那个函数会
把 ``appearance`` 本身覆盖成剥离了服装的体貌专用文本，是阶段一模型实际
看到的原文，不是阶段二此刻能读到的字段；没有这份快照时（未触发覆盖、或
旧调用点尚未产出）``appearance`` 本身就还是阶段一看到的原文，直接用。
"""
from __future__ import annotations

import logging
from typing import Any

from app.production.storyboard_repair_context import known_character_identities

log = logging.getLogger(__name__)


def wardrobe_plan_beat_sheet_rules() -> list[str]:
    """阶段一 rules[]，两档都无条件追加（不按 adaptation_mode 分支）。"""
    return [
        "为本集每个出场人物规划一张全集服装表（wardrobe_plan）：identity_id 必须逐字取自 "
        "known_assets.characters 里的某个 identity_id（合法取值就是这份人物名单，不要自造，"
        "也不要用人物姓名代替）。每个人物先给出第一次出场时的整体着装：beat_id 填这个人物第一次"
        "出场所在的节拍，wardrobe 如果这个人物在 known_assets.characters 里的 appearance（外观锚点）已经写明服装，就逐字沿用锚点里的服装描述，"
        "锚点没写服装信息时按原文对这个人物当下情境的描写给出一套合理装扮，change_reason 填"
        "「首次出场」。锚点里如果写到某件随身物贴身佩戴、被外层衣物盖住看不见（例如「隔着"
        "卫衣贴胸挂着」），沿用锚点服装时不要把这类看不见的随身物写成外层衣物上能看见的花纹——"
        "按「衣服下看不见」处理，能不写就不写。",
        "之后，只要故事跨过一次现实生活里会让人换装的边界——从户外进入室内、准备就寝、次日"
        "起床、场景切换到相隔较久的新地点——就在 wardrobe_plan 里为对应人物追加一条新记录："
        "beat_id 填这次改变最先在哪个节拍变得可见，wardrobe 只正面描述换装后穿着/佩戴的东西"
        "（款式、颜色、材质、佩戴方式），不写「不再穿……」「没有……」这类否定句式——某件东西被"
        "摘下换下后，只需要不再提它，不必专门声明它的缺席；change_reason 优先逐字引用原文写出"
        "这次换装/脱下/交还的那句话，原文没有明写但按日常生活逻辑必然发生时，change_reason "
        "如实写清楚是哪种情境触发的（例如「进门」「入睡」「次日」「换场」）。",
        "一件之后会被摘下、交还或使用的服装或配饰（例如后来被别人解下来围上的围巾），必须提前"
        "在它第一次应该已经穿在身上/带在身边的那个更早节拍就规划进 wardrobe_plan，不能等到"
        "要用到的那一刻才让它凭空出现——回头去更早的节拍给对应人物补一条记录。同一场连续戏份里"
        "人物的着装保持不变，不要在没有情境边界跨越的情况下多给几条记录。",
    ]


def known_identity_ids(payload: dict[str, Any]) -> set[str]:
    """本集 asset_manifest 里登记的人物 identity_id 集合——wardrobe_plan 的
    identity_id 合法取值域，与阶段一 rules 里说的"known_assets.characters"
    同一份数据。"""
    return {
        str(c.get("identity_id") or "")
        for c in known_character_identities(payload)
        if c.get("identity_id")
    }


def _character_appearance_by_identity(payload: dict[str, Any]) -> dict[str, str]:
    """本集 asset_manifest.characters 的 identity_id -> 阶段一模型实际看到的外观
    锚点全文——优先取 ``storyboard_physical_anchor.apply_physical_anchor_overrides``
    覆盖前留下的快照（``appearance_at_beat_sheet``，该人物的锚点已被覆盖成剥离了
    服装的体貌专用文本），没有这份快照时（未触发覆盖，或旧调用点未产出）
    ``appearance`` 本身就还是阶段一看到的原文，直接用（见模块 docstring 2026-10-01
    一节的时序说明）。"""
    manifest = payload.get("asset_manifest") or {}
    return {
        str(c.get("identity_id") or ""): str(c.get("appearance_at_beat_sheet") or c.get("appearance") or "")
        for c in manifest.get("characters") or []
        if c.get("identity_id")
    }


def _wardrobe_grounded_in_appearance(wardrobe: str, appearance_anchor: str) -> bool:
    """全集服装表某人物"第一条记录"的着装文字，是否真的是阶段一规则第一条
    （"锚点写了服装就逐字沿用锚点里的服装描述"）从该人物外观锚点里抄出来的——
    而不是锚点没写服装时"按原文情境给出一套合理装扮"的虚构结果（真实回归：
    proj_ca86b15ab7d7《顾念长安》EP1，顾屿的外观锚点只有体貌特征，服装表第
    一条却是模型按情境编的大衣+围巾，被旧逻辑当成人物谱定妆照默认造型误用，
    见模块 docstring）。判据：去空白后 ``wardrobe`` 是 ``appearance_anchor``
    的连续子串——"逐字沿用"是整段抄写，不是东拼西凑，用连续子串而不是跳字
    子序列；不用关键词/服装词表（CLAUDE.md「禁止黑白名单」），服装描述的
    用词本就是开放集合，枚举不完。"""
    candidate = "".join(wardrobe.split())
    source = "".join(appearance_anchor.split())
    return bool(candidate) and candidate in source


def default_look_grounded_identity_ids(states: list[Any], payload: dict[str, Any]) -> set[str]:
    """全集服装表里，"第一条记录"（按声明顺序，与 ``WardrobePlanState`` 选
    "第一条"的口径一致）着装文字确实是外观锚点逐字产物的人物 identity_id
    集合——只有这些人物才有"已知的人物谱定妆照默认造型"，供 ``WardrobePlanState``
    过滤 ``wardrobe_matches_default`` 的 True 分支。"""
    anchors = _character_appearance_by_identity(payload)
    first_by_identity: dict[str, Any] = {}
    for state in states:
        first_by_identity.setdefault(state.identity_id, state)
    return {
        identity_id for identity_id, state in first_by_identity.items()
        if _wardrobe_grounded_in_appearance(state.wardrobe, anchors.get(identity_id, ""))
    }


def _split_valid_wardrobe_states(
    states: list[Any], identity_ids: set[str], beat_ids: set[str],
) -> tuple[list[Any], list[Any]]:
    """按 identity_id/beat_id 是否都合法拆成 (valid, invalid) 两个列表，供
    ``build_wardrobe_state``（推进用）与 ``wardrobe_plan_summary``（事后计数）
    共用同一份判据，不重复实现。"""
    valid, invalid = [], []
    for state in states:
        target = valid if state.identity_id in identity_ids and state.beat_id in beat_ids else invalid
        target.append(state)
    return valid, invalid


def _log_dropped(invalid: list[Any]) -> None:
    for state in invalid:
        log.warning(
            "[STORYBOARD_WARDROBE_PLAN_DROPPED][未拦截] 全集服装表条目 identity_id="
            "「%s」beat_id=「%s」引用了未知的人物或节拍，已从着装推进中剔除，不参与任何段的"
            "着装规则——如果这是遗漏的正确人物/节拍，需要人工核对本集生成结果",
            state.identity_id, state.beat_id,
        )


class WardrobePlanState:
    """跨段推进全集服装表：每段调用一次 ``advance()``，按声明顺序把落在本段
    beat_ids 里的变化记录认领并更新"当前着装"，供本段与之后各段使用。构造时
    传入的必须是已核验过的条目（见 ``build_wardrobe_state``），本类自身不再
    做合法性判断，只负责按段推进状态——与 ``storyboard_beat_foreshadowing.
    moments_for_segment`` 的"认领一次即不再重复索要"同一条纪律，但额外维护
    "每个人物当前穿什么"这份状态，因为下一段需要知道"本段开场时穿的是什么"，
    不只是"本段有没有新变化"。
    """

    def __init__(self, states: list[Any], grounded_identity_ids: set[str] | None = None) -> None:
        self._plan = list(states)
        self._claimed: set[int] = set()
        self._current_look: dict[str, Any] = {}
        # 每个人物在全集服装表里的第一条记录（= 人物谱定妆照默认造型）——但只有
        # 这条记录经 default_look_grounded_identity_ids 核验过、确实是外观锚点
        # 逐字产物的人物才记录（2026-10-01 顾屿反例，见模块 docstring）：锚点没写
        # 服装、模型按情境自行编造的首条记录不算"已知默认造型"，这个人物在
        # advance() 里永远拿不到 True，不在 grounded_identity_ids 里的人物不记录
        # 任何"第一条"，之后任何一次认领都按对象恒等比较落空（False）。
        grounded = grounded_identity_ids or set()
        self._first_state_by_identity: dict[str, Any] = {}
        for state in self._plan:
            if state.identity_id in grounded:
                self._first_state_by_identity.setdefault(state.identity_id, state)

    def advance(self, segment_beat_ids: list[str]) -> tuple[dict[str, Any], list[Any], dict[str, bool]]:
        """返回 (本段开场时的着装快照, 本段新认领的变化列表, 本段结束时每个
        人物"当前是否默认造型"的判定)；第二项按 ``wardrobe_plan`` 声明顺序
        保留，调用方据此更新提示词规则。第三项只覆盖"已经在全集服装表里出现
        过至少一条记录"的人物——从未出现过的人物不在这份字典里，调用方需要
        按"没有依据"处理，不得当成 False。"""
        start_snapshot = dict(self._current_look)
        changes_here: list[Any] = []
        for index, state in enumerate(self._plan):
            if index in self._claimed:
                continue
            if state.beat_id in segment_beat_ids:
                self._claimed.add(index)
                changes_here.append(state)
                self._current_look[state.identity_id] = state
        default_flags = {
            identity_id: state is self._first_state_by_identity.get(identity_id)
            for identity_id, state in self._current_look.items()
        }
        return start_snapshot, changes_here, default_flags


def build_wardrobe_state(states: list[Any], payload: dict[str, Any], known_beat_ids: set[str]) -> WardrobePlanState:
    """核验 + 构造：剔除未知 identity_id/beat_id 的条目（记日志，见模块
    docstring「与 causality/foreshadowing 的一处刻意不同」），用剩下的合法
    条目算出"哪些人物的首条记录有已知默认造型依据"（2026-10-01，见
    ``default_look_grounded_identity_ids``），一并交给推进器。"""
    valid, invalid = _split_valid_wardrobe_states(states, known_identity_ids(payload), known_beat_ids)
    _log_dropped(invalid)
    return WardrobePlanState(valid, default_look_grounded_identity_ids(valid, payload))


def segment_rule_text(
    look_start: dict[str, Any], changes_here: list[Any], default_flags: dict[str, bool],
    payload: dict[str, Any], relevant_characters: list[dict[str, Any]],
) -> list[str]:
    """阶段二 per-segment 正面陈述：本段开场着装（全集服装表规划、且与本段
    相关的人物）+ 本段内着装变化（附原因）+ 预先算好的 wardrobe_matches_default
    取值（模型只需转抄，不需要自己判断本段是否默认造型，见
    ``app.schemas.segment_identity.SegmentCharacter``）。只报告与本段有关的
    人物（见 ``relevant_characters``，与其余 per-segment 规则同一份
    relevant_assets 数据），不把全集所有人物的着装都塞进每一段的提示词。
    ``default_flags`` 没有覆盖到的人物（没有在全集服装表里出现过任何记录）
    不追加 yes/no 规则，交给模型按通用定义自行判断或填 unsure——不兜底。"""
    names = {c.get("identity_id"): c.get("display_name") for c in known_character_identities(payload)}
    relevant_ids = {c.get("identity_id") for c in relevant_characters}
    lines: list[str] = []
    visible = {iid: s for iid, s in look_start.items() if iid in relevant_ids and names.get(iid)}
    if visible:
        parts = "；".join(f"@{names[iid]} {state.wardrobe}" for iid, state in visible.items())
        lines.append(
            f"本段着装（全集服装表）：{parts}。这是全集服装表规划的本段开场着装，"
            "continuity_memo.characters[].wardrobe 默认与此一致，本段原文如果另有更细的可见"
            "细节可以补充，但不能违背这个大方向。"
        )
    for state in changes_here:
        name = names.get(state.identity_id)
        if not name:
            continue
        previous = look_start.get(state.identity_id)
        previous_desc = previous.wardrobe if previous else "本集此前尚未确定的着装"
        lines.append(
            f"本段内着装变化：@{name} 从「{previous_desc}」变为「{state.wardrobe}」"
            f"（原因：{state.change_reason}）——continuity_memo.characters[].wardrobe 要体现"
            "这次变化后的样子，本段画面也要有相应的动作或过程，不能只在下一段凭空换装。"
        )
    default_names = [names[iid] for iid in relevant_ids if names.get(iid) and default_flags.get(iid) is True]
    changed_names = [names[iid] for iid in relevant_ids if names.get(iid) and default_flags.get(iid) is False]
    if default_names:
        lines.append(
            "根据全集服装表，本段下列人物穿着与人物谱定妆照默认造型完全一致，"
            f"resources.characters 对应条目的 wardrobe_matches_default 必须填 yes：{'、'.join(default_names)}。"
        )
    if changed_names:
        lines.append(
            "根据全集服装表，本段下列人物穿着与默认造型不同，"
            f"resources.characters 对应条目的 wardrobe_matches_default 必须填 no：{'、'.join(changed_names)}。"
        )
    return lines


def wardrobe_plan_summary(draft: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """按最终持久化 beat_draft 事后重算，供 ``StoryboardPack.adaptation``
    留档——三态同 ``foreshadowing_summary``：
    ``{"status": "no_plan_nominated", "problem_count": 0}``（模型完全没提名）；
    ``{"status": "ok"/"warning", "problem_count": N}``（N = identity_id/
    beat_id 无效、已被 ``build_wardrobe_state`` 剔除的条目数）。
    """
    states = draft.wardrobe_plan
    if not states:
        return {"status": "no_plan_nominated", "problem_count": 0}
    beat_ids = {beat.beat_id for beat in draft.beat_sheet}
    _, invalid = _split_valid_wardrobe_states(states, known_identity_ids(payload), beat_ids)
    return {"status": "warning" if invalid else "ok", "problem_count": len(invalid)}
