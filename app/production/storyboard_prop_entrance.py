"""P0-D：道具入场计划（prop_entrances）——与 ``storyboard_wardrobe_plan`` 同一次
真实回归驱动（proj_ca86b15ab7d7 EP1）：一只水浸行李箱在第 14 段突然出现在
温念身边，而第 11-13 段原文明确写两人还在门槛外，没有任何镜头交代箱子被
搬出来。逐段独立生成看不到"后面某段需要这件道具"，只能眼下写什么就是什么；
本模块给"这件道具第一次该怎么出现在画面里"补一个全集视野的计划。

与 ``storyboard_beat_foreshadowing`` 结构同构：阶段一模型在
``_AiBeatSheetDraft.prop_entrances``（``_AiPropEntrance`` 列表，见
``storyboard_beat_sheet_schemas``）里逐条提名，本模块核验 beat_id 是否真实
存在、按段拆解成规则文本，并对"计划要求出场但提示词里没写"给一条不阻断的
事后 advisory（同 P0-A/C 的能力边界：只能判断道具名是否以改写措辞出现，
判不出是否真的单独成镜）。核验策略与剔除哲学见 ``storyboard_wardrobe_plan``
模块 docstring「与 causality/foreshadowing 的一处刻意不同」，本模块的
``valid_prop_entrances`` 是同一套策略的道具版本。
"""
from __future__ import annotations

import logging
from typing import Any

from app.production.screenplay_markers import beat_is_shot

log = logging.getLogger(__name__)


def prop_entrance_beat_sheet_rules() -> list[str]:
    """阶段一 rules[]，两档都无条件追加（不按 adaptation_mode 分支）。"""
    return [
        "为需要交代出场的关键道具规划入场计划（prop_entrances）：label 用道具在原文里的称呼，"
        "beat_id 填这件道具第一次应该出现在画面里的节拍，entrance_description 说明它是怎么"
        "进入画面的（例如「从屋内拖出到门口」「从大衣口袋取出」「一直系在颈间，从本段开始入镜」）。",
        "一件道具如果要在后面某个节拍被使用、拿取或交给别人，必须先在更早的节拍规划好它的入场"
        "——不能让道具毫无来由地凭空出现在某一段画面里；如果一件道具从故事一开始就一直带在身上/"
        "随身携带，也要在它第一次入镜的节拍写清楚它当时就在场（例如「一直系在颈间」）。",
    ]


def _invalid_entrances(entrances: list[Any], known_beat_ids: set[str]) -> tuple[list[Any], list[Any]]:
    """按 beat_id 是否存在拆成 (valid, invalid)，供 ``valid_prop_entrances``
    与 ``prop_entrance_summary`` 共用同一份判据。"""
    valid, invalid = [], []
    for entry in entrances:
        target = valid if entry.beat_id in known_beat_ids else invalid
        target.append(entry)
    return valid, invalid


def valid_prop_entrances(entrances: list[Any], known_beat_ids: set[str]) -> list[Any]:
    """剔除引用未知 beat_id 的条目（记日志，不静默、不回退到"猜一个节拍"），
    剩下的合法条目参与任何段的认领。"""
    valid, invalid = _invalid_entrances(entrances, known_beat_ids)
    for entry in invalid:
        log.warning(
            "[STORYBOARD_PROP_ENTRANCE_DROPPED][未拦截] 道具入场条目 label=「%s」引用的 "
            "beat_id「%s」不存在于本集节拍表，已剔除，不参与任何段的入场提示——如果这是遗漏的"
            "正确节拍，需要人工核对本集生成结果", entry.label, entry.beat_id,
        )
    return valid


def moments_for_segment(segment_beat_ids: list[str], entrances: list[Any], covered: set[str]) -> list[Any]:
    """本段（source: ``plan.beat_ids``）首次认领的入场条目，同
    ``storyboard_beat_foreshadowing.moments_for_segment``——覆盖容量拆分续段
    完整继承 ``beat_ids`` 这一事实，避免同一件道具被连续几个续段反复索要。"""
    claimed: list[Any] = []
    for entry in entrances:
        if entry.beat_id in segment_beat_ids and entry.beat_id not in covered:
            claimed.append(entry)
            covered.add(entry.beat_id)
    return claimed


def segment_rule_text(entrances_here: list[Any]) -> list[str]:
    """阶段二 per-segment 正面陈述：本段必须交代这件道具是怎么第一次出现在
    画面里的。"""
    return [
        f"本段需要交代出场的道具：{entry.label}——{entry.entrance_description}（全集道具入场"
        "计划要求这件道具在本段第一次出现在画面里，交代清楚它是怎么进入这场戏的，不能毫无来由地"
        "凭空出现）"
        for entry in entrances_here
    ]


def segment_advisories(entrances_here: list[Any], prompt_text: str) -> list[str]:
    """非阻断，供 ``_segment_content_advisories`` 合并进 ``degraded_
    capabilities``。能力边界同 P0-A/C：只能判定"道具名是否以改写后的措辞出现"，
    判不出"是否真的单独成镜"。"""
    advisories: list[str] = []
    for entry in entrances_here:
        if not beat_is_shot(f"必现内容：{entry.label}", prompt_text):
            advisories.append(
                "[STORYBOARD_PACK_PROP_ENTRANCE_NOT_SHOWN][未拦截] 道具"
                f"「{entry.label}」计划在本段第一次出场（{entry.entrance_description}），但看起来"
                "没有被写进提示词（只能判断道具名是否以改写措辞出现，判不出是否真的入镜），"
                "请人工核查——可在分镜台编辑本段镜头稿补上"
            )
    return advisories


def prop_entrance_summary(draft: Any) -> dict[str, Any]:
    """按最终持久化 beat_draft 事后重算，供 ``StoryboardPack.adaptation``
    留档——三态同 ``foreshadowing_summary``：
    ``{"status": "no_entrances_nominated", "problem_count": 0}``（模型完全
    没提名）；``{"status": "ok"/"warning", "problem_count": N}``（N = beat_id
    无效、已被 ``valid_prop_entrances`` 剔除的条目数）。
    """
    entrances = draft.prop_entrances
    if not entrances:
        return {"status": "no_entrances_nominated", "problem_count": 0}
    beat_ids = {beat.beat_id for beat in draft.beat_sheet}
    _, invalid = _invalid_entrances(entrances, beat_ids)
    return {"status": "warning" if invalid else "ok", "problem_count": len(invalid)}
