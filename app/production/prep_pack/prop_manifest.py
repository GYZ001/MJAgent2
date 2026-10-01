"""道具清单构建：把抽取阶段的 props 提及核验/锚定/合并成 asset_manifest.props。

从 ``discovery.py`` 拆出（2026-10-01，defect② 第三轮续修需要给
``_prep_pack_prop_mention_binding``/``_prep_pack_build_prop_manifest`` 加
``cards_with_prior_evidence`` 参数——discovery.py 当时已经在 500 行基线的
棘轮上顶格，见该文件模块 docstring"这个文件还有空间"一句已经不成立；这组
函数本身是自成一体的子关注点，拆分不改变任何判据，逐字搬移，只是换了文件）。

判据本身见各函数自己的 docstring；两道硬性约束搬过来也没有变：道具的 label
逐字出现在段落原文里走"裸直接命中"，或经 ``app.props.card_match.
match_existing_prop_card`` 绑定既有卡，两条任一满足即可，都不满足就丢弃
（记入 ``unanchored`` 出参，不静默 continue）。
"""
from __future__ import annotations

import logging
from typing import Any, Sequence

from app.schemas import Prop
from app.source_excerpt import SourceSegment

from .provenance import _prep_pack_provenance
from .trailing_anchor import prop_literal_or_trailing_anchor, trailing_anchor_phrase

log = logging.getLogger(__name__)


# 2.0.0 新增：道具没有世界书图像素材库，不需要身份消歧/发现，也不需要
# suspected_true_name 声明-核验通道——一个道具就是它自己（结构判据，零
# 语义），按 label 精确字符串去重合并 segment_indexes 即可。
#
# 道具的 label 真的逐字出现在该段落原文里时，走跟角色侧"称谓证据闸"同一判据
# 的"裸直接命中"（method="direct"，_prep_pack_gate_segment_indexes 的结构闸
# 不做这一步是因为它对全部三种资产统一处理、且要给 characters/scenes 的解析
# 路径留豁免空间——道具没有这个豁免需求，在这里单独把关不冲突）。
#
# 2026-09-30 新增第二条独立判据（method="card_match"）：道具现在也有一条"经
# 解析路径绑定可豁免逐字"的路，同 characters/scenes 的别名解析同一先例——
# 世界书已有道具卡与这条提及存在绑定关系（判据本身见 app.props.card_match.
# match_existing_prop_card 模块 docstring「模型提名、代码核验」一节：既可能
# 是模型经 known_prop_name 明确提名 + 代码核验证据，也可能是卡名/别名本身
# 逐字出现在证据里并与 label 存在包含关系）时，这条提及绑定到那张卡：
# ``canonical_name`` 记卡的规范名，``label`` 仍保留模型这次的原文写法（不
# 篡改，供分镜台 known_assets.props 展示 + 下游别名登记）。真实事故
# （proj_ca86b15ab7d7 系列，见 2026-09-30 派单）：素材库第1集建的卡叫「行李
# 箱」，第2集模型把同一件东西报成新标签「旧行李箱」——旧逻辑按 label 精确
# 去重、从不查 bible.props，结果第2集当成全新道具，外观/参考图都查不到。
# 两条判据（本函数的字面锚定 + card_match 的卡片绑定）是"任一满足即可"：一
# 条提及若两条都不满足，整条丢弃（不计入清单，不阻断发布——跟 scene 侧
# "没证据就当未解析"同一处置，不是"空口提名也发布"）。
#
# 已知局限（P2，本次不解决）：合并键是 canonical_name（绑定时）或 label（未绑定时）——
# 同一个原文写法在不同 mention 里若因各自声明的段落证据不同而时而绑上卡、时而绑不上，
# 会拆成两条独立清单条目而不是合并成一条。card_match 判据本身是纯函数、按同一份 cards
# 与各自的段落证据独立运算，不做跨 mention 的二次合并——同一物件反复出现时措辞通常一致，
# 这类拆分预计罕见；需要更强一致性时留给后续有专门预算时再评估。
#
# plot_significant/plot_significant_quote（2026-09-28 新增，见
# .chunk_extraction 提示词与 app.props.judge.is_key_prop_mention 的同名
# 判据）：原样透传模型这次申报的两个字段，不在这里做任何核验——逐字核验
# 是 is_key_prop_mention 消费时的职责（它同时还需要 source_text，本函数
# 不持有），这里只负责把模型的申报值带到 props_payload 里，缺省时按假/空
# 兜底（旧调用方构造的 mention dict 没有这两个键时不报错，向后兼容
# tests/test_props_library.py 里手写的 mention 夹具）。
def _prep_pack_prop_card_anchor(
    card: Prop, valid_indexes: list[int], segments: list[SourceSegment],
) -> tuple[int, str, bool] | None:
    """card_match 分支的 provenance 锚点：``card`` 的 name/alias 里第一个在
    ``valid_indexes`` 某段原文里逐字出现的那个，连同段号、是否经尾部退让
    一起返回。全串定位不到时退让到尾部子串再试一遍（2026-10-01，见
    .trailing_anchor 模块），范围仍限定在 valid_indexes；都落空才 None。"""
    identifiers = [str(card.name or "").strip(), *(str(a or "").strip() for a in card.aliases)]
    for index in valid_indexes:
        text = segments[index - 1].text
        for identifier in identifiers:
            if identifier and identifier in text:
                return index, identifier, False
    evidence_text = "\n".join(segments[i - 1].text for i in valid_indexes)
    phrase = trailing_anchor_phrase(identifiers, evidence_text)
    if not phrase:
        return None
    for index in valid_indexes:
        if phrase in segments[index - 1].text:
            return index, phrase, True
    return None


def _prep_pack_prop_mention_binding(
    label: str, source_wording: str, nominated_card: str, valid_indexes: list[int],
    segments: list[SourceSegment], cards: Sequence[Prop],
    cards_with_prior_evidence: frozenset[str],
) -> tuple[list[int], Prop | None, str, list[int], str, bool] | None:
    """核验一条道具提及：返回 (segment_indexes, 绑定的卡或 None, provenance.method,
    anchor_segments, anchor_phrase, 是否经尾部退让锚定)；两条判据都不满足时返回
    None（整条丢弃，调用方须记入可见的 unanchored 出参，不静默 continue）。

    字面候选依次试 label、source_wording（schemas._ModelPropMention.
    source_wording 上方注释）；都不命中且未绑定既有卡时退让到尾部子串，见
    ``.trailing_anchor.prop_literal_or_trailing_anchor``（2026-10-01，真实
    案例"旧笔记本"→"笔记本"）；已绑定卡的退让改在 _prep_pack_prop_card_
    anchor 内部做（不跟 card_match 自身的单一胜者判据赛跑）。``nominated_
    card`` 与 ``cards_with_prior_evidence``（必传）原样透传给
    ``match_existing_prop_card``（见 app.props.card_match 模块 docstring）。
    """
    # 延迟导入：避免给 app.production.prep_pack（映射台核心链路）加一条模块级
    # 常驻依赖到 app.props 的模型/出图调用链——import app.props.card_match 前
    # Python 必须先跑 app/props/__init__.py，它无条件 import .service，而
    # service 又模块级 import .image → app.hiagent（HiAgent 网关客户端）。与
    # 相邻 _discover_new_characters/_discover_new_scenes/_discover_new_props
    # 的既有写法保持一致（同一文件里三个函数都是函数内 import）。
    from app.props.card_match import match_existing_prop_card

    evidence_text = "\n".join(segments[i - 1].text for i in valid_indexes)
    card = match_existing_prop_card(
        label, evidence_text, cards, source_wording=source_wording, nominated_card=nominated_card,
        cards_with_prior_evidence=cards_with_prior_evidence,
    )
    literal_indexes, literal_phrase, trailing = prop_literal_or_trailing_anchor(
        label, source_wording, valid_indexes, segments, evidence_text, card is not None,
    )
    if not literal_indexes and card is None:
        return None
    segment_indexes = literal_indexes or valid_indexes
    if literal_indexes:
        return segment_indexes, card, "direct", [segment_indexes[0]], literal_phrase, trailing
    anchor = _prep_pack_prop_card_anchor(card, valid_indexes, segments)
    anchor_segments = [anchor[0]] if anchor else [segment_indexes[0]]
    anchor_phrase = anchor[1] if anchor else ""
    card_trailing = anchor[2] if anchor else False
    return segment_indexes, card, "card_match", anchor_segments, anchor_phrase, card_trailing


def _prep_pack_record_unanchored_prop(
    unanchored: list[dict[str, Any]] | None,
    label: str, source_wording: str, valid_indexes: list[int],
) -> None:
    """道具提及两条判据（label/source_wording 逐字命中声明段落、或对照既有
    卡片）都不满足时的可见记录（2026-09-30，见派单真实案例「木星星」「缠着
    细银丝的木簪」「妈妈的字条」）：此前 ``_prep_pack_build_prop_manifest``
    直接 ``continue``，没有任何日志或可见记录，这三件真实道具从
    asset_manifest.props 静默消失。现在打一条固定前缀日志（同 card_match.
    match_existing_prop_card 的 PROP_CARD_MATCH_AMBIGUOUS 同一惯例，供日志
    检索）+ 写入调用方传入的 ``unanchored`` 列表（生成台 payload 的
    asset_manifest.unanchored_prop_mentions，供人工核查；不进 props 清单，
    不阻断发布——跟场景侧 degrade_unresolved_scene 同一处置）。``unanchored``
    为 None（旧调用点未接线，例如既有测试夹具）时只记日志，不因此报错。"""
    reason = (
        "source_wording 非原文字面" if source_wording
        else "缺少 source_wording 且 label 非原文字面，也未匹配到既有道具卡"
    )
    log.warning(
        "[PREP_PACK_PROP_UNANCHORED][未拦截] 道具「%s」在声明段落 %s 未能锚定"
        "（%s），不计入 props 清单，请人工核查",
        label, valid_indexes, reason,
    )
    if unanchored is not None:
        unanchored.append({
            "label": label, "source_wording": source_wording,
            "segment_indexes": valid_indexes, "reason": reason,
        })


def _prep_pack_build_prop_manifest(
    prop_mentions: list[dict[str, Any]], segments: list[SourceSegment],
    *, cards: Sequence[Prop] = (), unanchored: list[dict[str, Any]] | None = None,
    cards_with_prior_evidence: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """``unanchored``（2026-09-30 出参，可选，默认 None 不记录——同
    ``_resolve_assets`` 的 ``appellation_resolutions`` 同一模式，保持既有
    调用点/测试签名不变）：两条判据都不满足、被丢弃的提及原地写入这个列表，
    见 _prep_pack_record_unanchored_prop。``cards_with_prior_evidence``
    （2026-10-01）默认空集合（同 ``cards``，不强迫无关测试改签名），转发给
    ``_prep_pack_prop_mention_binding``（必传，见 app.props.card_match）。
    """
    props: dict[str, dict[str, Any]] = {}
    for mention in prop_mentions:
        label = str(mention.get("label") or "").strip()
        source_wording = str(mention.get("source_wording") or "").strip()
        known_prop_name = str(mention.get("known_prop_name") or "").strip()
        valid_indexes = sorted(
            index for index in {int(i) for i in mention.get("segment_indexes") or []}
            if 1 <= index <= len(segments)
        )
        if not label or not valid_indexes:
            continue
        binding = _prep_pack_prop_mention_binding(
            label, source_wording, known_prop_name, valid_indexes, segments, cards,
            cards_with_prior_evidence,
        )
        if binding is None:
            _prep_pack_record_unanchored_prop(unanchored, label, source_wording, valid_indexes)
            continue
        segment_indexes, card, method, anchor_segments, anchor_phrase, trailing = binding
        canonical_name = card.name if card else None
        key = canonical_name or label
        entry = props.setdefault(key, {
            "label": label,
            "canonical_name": canonical_name,
            "description": str(mention.get("description") or "").strip(),
            "segment_indexes": [],
            "provenance": _prep_pack_provenance(
                method, anchor_segments, anchor_phrase, trailing_anchor=trailing,
            ),
            "plot_significant": bool(mention.get("plot_significant")),
            "plot_significant_quote": str(mention.get("plot_significant_quote") or "").strip(),
            "source_wording": source_wording,
            "known_prop_name": known_prop_name,
        })
        entry["segment_indexes"] = sorted(
            set(entry["segment_indexes"]) | set(segment_indexes)
        )
        # plot_significant/plot_significant_quote 必须跨同 label 的多条提及做
        # "任一为真即采纳"的合并，不能只取 setdefault 首次插入时那一条
        # （2026-09-28 code review 实测发现：同一道具先在早期 chunk 里被平淡
        # 提及、后在更晚的 chunk 里才因交接/特写/伏笔揭示被模型正确标记
        # plot_significant=True，是 Part C 明确要接住的形状——黄铜旧星盘、
        # 童年合影都是这种贴身出现多次、其中一次才是剧情重要时刻的道具。按
        # 处理顺序固定取第一条会让靠后到达的真实证据被静默丢弃，且没有任何
        # 信号提示丢弃发生过）。一旦某条提及命中就不再被后续 False 的提及
        # 覆盖回去——先到的真证据比后到的"这条不重要"更可信。
        if mention.get("plot_significant") and not entry["plot_significant"]:
            entry["plot_significant"] = True
            entry["plot_significant_quote"] = str(
                mention.get("plot_significant_quote") or ""
            ).strip()
    return list(props.values())
