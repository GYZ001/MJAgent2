"""对照道具素材库既有卡片：判断一次模型道具提及是不是某张已建卡道具的另一种
原文写法，命中就复用那张卡，不再当成全新道具登记。

唯一胜者规则照抄 ``app.validators.scene_match.match_scene_name`` 的「包含关系
优先、唯一胜者」：若某张既有道具卡的 ``name`` 或某个 ``alias`` 确实逐字出现在
``evidence_text``（本次提及的原文依据）里，且它与这次的模型标签存在字符串
包含关系（任一方向——这自然覆盖"模型标签就是卡名"这一子情形，等价关系是
包含关系的特例），这条提及就判给那张卡；若同时有不止一张卡满足条件，判据
结构上无法唯一裁决，宁可不绑（不静默挑一个），只记一条可见信号供人工核查。
不针对任何具体道具名做特判，也不用词表/关键词黑白名单（no-blacklist-fixes
纪律）。

真实回归案例（proj_ca86b15ab7d7，见 2026-09-30 派单只读核实结论）：素材库里
第1集建的卡叫「行李箱」，第2集模型把同一件东西报成新标签「旧行李箱」——旧
逻辑（``app.production.prep_pack.discovery._prep_pack_build_prop_manifest``）
按模型 label 精确去重、从不查 ``bible.props``，结果第2集把它当成一件全新
道具，外观/参考图都查不到，分镜台只能各自现编。「行李箱」是「旧行李箱」的
包含子串，唯一胜者，本模块把这条提及判给既有卡。

``app.production.prep_pack.discovery``（映射台清单构建）与 ``app.props.
service``（道具库反应式登记）两个调用点共用同一份判据，不允许出现"哪边按
字面比对、哪边按包含关系比对"这种两侧标准不对齐的局面（CLAUDE.md「模型契约
两侧必须对齐」）。

2026-09-30 修复评审：两个调用点曾经"判据函数相同、但喂给它的 evidence_text
范围不对齐"——discovery.py 只用该条提及自己声明的 segment_indexes 对应原文
（窄），service.py 却传整集 source_text（宽）。宽证据面会让恰好出现在本集
任意无关段落里的既有卡片 name/alias 被当成"证据"命中，把两个不相关的道具
静默合并成一张卡，且因为只命中 1 张卡不会触发下面的歧义告警——完全静默。
``evidence_text_for_segments`` 是两个调用点现在共用的窄证据计算，确保同一
条提及在两处得到同一个判定结果。
"""
from __future__ import annotations

import logging
from typing import Sequence

from app.schemas import Prop
from app.source_excerpt import SourceSegment

log = logging.getLogger(__name__)


def _prop_identifiers(card: Prop) -> list[str]:
    identifiers = [str(card.name or "").strip()]
    identifiers += [str(alias or "").strip() for alias in card.aliases]
    return [identifier for identifier in identifiers if identifier]


def evidence_text_for_segments(
    segments: Sequence[SourceSegment], segment_indexes: Sequence[int],
) -> str:
    """把一条提及自己声明的 ``segment_indexes``（1-indexed）收窄成对应原文，
    供两个调用点喂给 ``match_existing_prop_card`` 时使用同一份证据范围。

    越界或未声明的下标一律丢弃、不报错（与 discovery.py 的既有过滤规则
    一致）；``segment_indexes`` 为空或全部越界时返回空字符串——
    ``match_existing_prop_card`` 对空证据直接判不绑，不回退到更大范围的原文：
    回退等于重新放开本模块要堵住的那个漏洞（宽证据把无关既有卡片的字面命中
    当证据）。
    """
    valid = sorted({int(i) for i in segment_indexes} & set(range(1, len(segments) + 1)))
    return "\n".join(segments[i - 1].text for i in valid)


def match_existing_prop_card(
    label: str, evidence_text: str, cards: Sequence[Prop],
) -> Prop | None:
    """``label``（模型这次的道具提及标签）是否应绑定到 ``cards`` 中的某一张。

    两个条件都要满足：卡片的 ``name``/某个 ``alias`` 逐字出现在
    ``evidence_text`` 里；且该命中的原文写法与 ``label`` 存在包含关系（任一
    方向）。多张卡片同时满足时结构上无法唯一裁决，记一条可见信号并返回
    ``None``——不猜、不按卡片列表顺序挑第一个。
    """
    label = (label or "").strip()
    if not label or not evidence_text or not cards:
        return None
    winners: dict[str, Prop] = {}
    for card in cards:
        for identifier in _prop_identifiers(card):
            if identifier not in evidence_text:
                continue
            if identifier in label or label in identifier:
                winners[str(card.name or "").strip()] = card
                break
    if len(winners) == 1:
        return next(iter(winners.values()))
    if len(winners) > 1:
        log.warning(
            "[PROP_CARD_MATCH_AMBIGUOUS][未拦截] 道具标签「%s」同时与包含关系命中的"
            "既有卡片有 %d 张（%s），无法唯一判定归属，按未绑定处理，请人工核查",
            label, len(winners), "、".join(sorted(winners)),
        )
    return None
