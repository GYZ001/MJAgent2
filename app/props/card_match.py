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

2026-09-30 第二轮修复，主会话复核后改为「模型提名、代码核验」（no-blacklist-
fixes 纪律的同一精神：判据不能靠代码自己猜语义，合法值域/归属要由模型明确
提名，代码只核验证据）：最初曾尝试过纯包含关系兜底（source_wording/label
与卡名存在子串关系即判定候选）——真实数据证伪了这条路：`水晶球` ⊂ `老式
水晶球`（博物馆展出的另一件东西）与 `木星星` ⊂ `小木星星`（同一件东西的
省略说法）在字符串结构上完全同形，包含关系本身无法分辨"这是同一件物品的
简称"还是"这只是恰好包含该词的另一件东西"——这需要语义判断，纯字符串判据
做不到，若强行兜底会把 `test_ensure_props_for_labels_ignores_unrelated_
card_mentioned_outside_own_segment`（证据只含"水晶球"，卡是"老式水晶球"，
二者是不同实物）误判成绑定同一张卡。

修法：新增 ``nominated_card`` 参数——模型自己在 ``known_prop_name``
（见 app.production.prep_pack.schemas._ModelPropMention）里声明"这就是
已登记道具名单里的哪一件"，不确定/不是就留空，不猜。``match_existing_
prop_card`` 收到非空提名时，先核验这张卡是否真的存在、且这条提及自己的
label/source_wording 至少有一个真的逐字出现在证据里（提名不能凭空信任，
仍要有本地证据支撑），核验通过直接绑定；提名的卡不存在、或证据核验不过，
打 ``[PROP_CARD_NOMINATION_UNKNOWN][未拦截]`` 前缀日志，退回常规判据（不
因为提名失败就整条阻断——提名是加分项，不是前置门槛）。

已知局限：模型没有提名时（``known_prop_name`` 留空），纯粹的字面缩略/概括
关系不会被自动归并——例如证据只写「木星星」、卡名是「小木星星」，模型这次
没有把它填进 known_prop_name，这条提及不会被绑定，会按新道具处理（或在
``label``/``source_wording`` 本身就逐字等于某张卡 identifier 时，仍能通过
下面的常规判据绑定）。这是刻意的：宁可少绑一次、多建一张卡，也不猜一个
可能是不同实物的归属——no-blacklist-fixes 纪律要求"模型提名、代码核验"，
不允许代码自己用字符串相似度替模型做语义判断。

常规判据（未变，长期存在）：卡片的 ``name``/某个 ``alias`` 逐字出现在
``evidence_text`` 里，且它与这次的模型标签 ``label`` 存在字符串包含关系
（任一方向）。多张卡片同时命中仍是结构性歧义，记 ``[PROP_CARD_MATCH_
AMBIGUOUS][未拦截]`` 并返回 ``None``，不猜。

两个调用点必须传同一份 ``source_wording``/``nominated_card``（discovery.py
读的是这条提及自己的 mention["source_wording"]/mention["known_prop_name"]，
service.py 读的是同一个 mention 字典的同一组键——见 app.production.
prep_pack.discovery._prep_pack_prop_mention_binding 与 app.props.service.
ensure_props_for_labels 各自的调用点），否则两侧又会分叉出"哪边信提名、
哪边不信"这种新的标准不对齐。
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


def _resolve_nominated_card(
    nominated_card: str, cards: Sequence[Prop], label: str, source_wording: str, evidence_text: str,
) -> Prop | None:
    """``nominated_card``（模型自报"这就是已登记道具名单里的哪一件"）核验：
    这张卡必须真的存在于 ``cards``，且这条提及自己的 ``label``/
    ``source_wording`` 至少有一个真的逐字出现在 ``evidence_text`` 里——提名
    本身不是免核验通道，只是把"归属"这件需要语义判断的事交给模型明确声明，
    代码仍然核验本地证据是否支撑这次绑定。两条核验都不过时打同一前缀日志，
    调用方退回常规判据，不因提名失败而整条阻断。
    """
    card = next((c for c in cards if nominated_card in _prop_identifiers(c)), None)
    if card is None:
        reason = "素材库里没有同名/同别名的卡片"
    elif not ((label and label in evidence_text) or (source_wording and source_wording in evidence_text)):
        reason = "label/source_wording 均未逐字出现在证据原文里"
    else:
        return card
    log.warning(
        "[PROP_CARD_NOMINATION_UNKNOWN][未拦截] 模型提名的既有道具「%s」未通过核验"
        "（%s），按未提名处理，退回常规判据，请人工核查",
        nominated_card, reason,
    )
    return None


def match_existing_prop_card(
    label: str, evidence_text: str, cards: Sequence[Prop], *,
    source_wording: str = "", nominated_card: str = "",
) -> Prop | None:
    """``label``（模型这次的道具提及标签）是否应绑定到 ``cards`` 中的某一张。

    ``nominated_card`` 非空时先走模型提名核验（见 ``_resolve_nominated_
    card``），核验通过直接返回；核验不过或未提名时，走常规判据——卡片的
    ``name``/某个 ``alias`` 逐字出现在 ``evidence_text`` 里，且与 ``label``
    存在包含关系（任一方向）。多张卡片同时命中常规判据时结构上无法唯一
    裁决，记一条可见信号并返回 ``None``——不猜、不按卡片列表顺序挑第一个。

    ``source_wording`` 只用于提名核验的证据支撑（见上），不参与常规判据的
    包含关系比对——见模块 docstring「已知局限」一节：纯包含关系无法分辨
    "同一件物品的缩略说法"与"恰好包含该词的另一件东西"，2026-09-30 已从
    生产回归中证伪（水晶球 vs 老式水晶球），不再作为独立判据分支。
    """
    label = (label or "").strip()
    source_wording = (source_wording or "").strip()
    nominated_card = (nominated_card or "").strip()
    if not label or not evidence_text or not cards:
        return None
    if nominated_card:
        card = _resolve_nominated_card(nominated_card, cards, label, source_wording, evidence_text)
        if card is not None:
            return card
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
