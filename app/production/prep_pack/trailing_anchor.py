"""原文写法尾部退让锚定（2026-10-01，PREP_PACK_VERSION 2.0.10）。

真实现场（我欲封天系列 EP1 用 2.0.9 重跑映射）：模型给的 label/source_wording
经常是概括/规范化写法，原文却只在声明段落里写了更短的字面——
"旧笔记本"（原文「一本封面陈旧、锁扣锈迹斑斑的笔记本」，第42段只剩"笔记本"
逐字命中）、"顾屿家书房"（原文「路过一间半开着门的书房」，第42段只剩"书房"）、
"小木星星"（原文「那枚旧旧的木星星」，只剩"木星星"）。既有逐字核验（label/
source_wording 整串必须出现）因此把这些真实存在的道具/场景当成"没有本集
依据"，道具侧静默丢弃、场景侧判未解析。

退让口径照抄本仓已有先例 ``app/production/scene_evidence.py::
scene_label_evidence``（地点称呼尾部退让："外宗中心广场"→"中心广场"→
"广场"，退到 2 字为止）：依次尝试每个候选字符串的全长，再逐字砍掉词头，
直到剩 2 个字；第一个在限定范围内逐字命中的子串就是 anchor_phrase。不放宽
任何既有逐字核验——退让出来的子串本身仍必须逐字出现在限定文本里，只是
这个限定范围从"候选整串"缩小到"候选的某个尾部子串"。

两处调用点（道具侧 ``discovery.py``、场景侧 ``resolve_assets.py``）都把
这当作**既有候选全部落空之后的最后一步**，范围严格限定在这条提及自己
声明的段落（不是全集检索）——只恢复"证据其实都在，只是措辞比原文更概括"
这一种情形，不越权替这条提及去别处找证据。命中后调用方会在 provenance 里
标记 ``trailing_anchor: True``（见 ``.provenance._prep_pack_provenance``），
可观测、不默认静默发布。
"""
from __future__ import annotations

from app.source_excerpt import SourceSegment

_MIN_TAIL_LEN = 2
# label/source_wording/卡名按提示词契约本就该是"名词短语"，不是完整分句
# （见 chunk_extraction.py 对应字段说明）；真实三个形状最长 5 字（"顾屿家
# 书房"）。退让只服务这类短语，更长的候选更可能是句子或被拼接出来的伪造
# 引用——反面实测 test_source_wording_stitched_from_nonadjacent_text_is_
# not_anchored：把原文里两处真实但不相邻的文字接成 12 字的 source_wording，
# 退让会把前半截"修剪掉"、只剩后半截恰好逐字命中，而那正是
# _prep_pack_locate_stitched_quote 已经在 quote 字段上专门拦截的同一类
# 拼接伪造，不能在 source_wording 这条新路上重新把它放开。上限定得比三个
# 真实形状宽松一倍以上（10 对 5），仅排除这类分句级长度的候选。
_MAX_CANDIDATE_LEN = 10


def trailing_anchor_phrase(candidates: list[str], declared_text: str) -> str:
    """在 ``declared_text``（提及自己声明段落拼接出的原文）里找 ``candidates``
    的尾部退让子串：按传入顺序逐个候选尝试，每个候选先试全长，再逐字砍掉
    词头，退到剩 2 个字为止；第一个逐字命中的即返回。都不命中返回空串。
    候选为空/空白字符串，或长度超过 ``_MAX_CANDIDATE_LEN``（不是名词短语，
    见模块级常量上方说明）直接跳过，不对任何具体名字做特判。
    """
    for raw in candidates:
        candidate = str(raw or "").strip()
        if not candidate or len(candidate) > _MAX_CANDIDATE_LEN:
            continue
        for start in range(0, max(1, len(candidate) - 1)):
            needle = candidate[start:]
            if len(needle) < _MIN_TAIL_LEN:
                break
            if needle in declared_text:
                return needle
    return ""


def prop_literal_or_trailing_anchor(
    label: str, source_wording: str, valid_indexes: list[int],
    segments: list[SourceSegment], evidence_text: str, card_bound: bool,
) -> tuple[list[int], str, bool]:
    """道具侧字面锚定：label/source_wording 整串命中优先（``trailing`` 置
    False，既有判据不变）。都不命中、且这条提及**没有**经
    ``app.props.card_match.match_existing_prop_card`` 绑定既有卡
    （``card_bound=False``）时，才退让到尾部子串，范围限定在这条道具提及
    自己声明的段落（``evidence_text``，调用方已按 valid_indexes 拼接好）。

    ``card_bound=True`` 时不在这里退让——真实回归（2026-10-01，
    tests/test_prop_card_binding.py::
    test_manifest_card_match_branch_anchors_within_its_own_segment）：label
    "那只旧行李箱" 退让到"行李箱"恰好与既有卡同名，会抢在
    ``_prep_pack_prop_card_anchor`` 前面把 provenance.method 从更精确的
    "card_match" 错判成"direct"。已绑定卡的尾部退让改在
    ``_prep_pack_prop_card_anchor`` 内部对 card.name/aliases 单独做，不在
    这条路径上跟 card_match 自身的单一胜者/消歧判据赛跑。

    都不命中（或 card_bound）返回 ``([], "", False)``——调用方据此继续走
    card_match 判据，不是直接判定整条提及失败，见
    ``app.production.prep_pack.discovery._prep_pack_prop_mention_binding``
    调用点。
    """
    label_indexes = [i for i in valid_indexes if label in segments[i - 1].text]
    if label_indexes:
        return label_indexes, label, False
    wording_indexes = [
        i for i in valid_indexes if source_wording and source_wording in segments[i - 1].text
    ]
    if wording_indexes:
        return wording_indexes, source_wording, False
    if card_bound:
        return [], "", False
    phrase = trailing_anchor_phrase([label, source_wording], evidence_text)
    if not phrase:
        return [], "", False
    return [i for i in valid_indexes if phrase in segments[i - 1].text], phrase, True


def scene_anchor_with_trailing_fallback(
    method: str, anchor_segments: list[int], anchor_phrase: str,
    segments: list[SourceSegment], mention_segment_indexes: list[int],
    name: str, source_wording: str,
) -> tuple[list[int], str, bool]:
    """场景侧锚点候选的最后一步：既有候选（``_prep_pack_local_text_anchor``
    等既有判据，见 resolve_assets.py 调用点）已经找到锚点就原样返回
    （``trailing`` 置 False）。只在 method 是 direct/discovery 且仍然空锚时
    才退让。

    ``resolution``（绑定到**已登记的既有场景**，不是本次新建）刻意不在这
    条退让范围内——真实回归（2026-10-01，
    tests/test_prep_pack_asset_discovery.py::
    test_synthetic_scene_label_without_any_independent_evidence_still_
    blocked）：场景名"大青山山顶"退让到"山顶"这个通用词，在真实长篇原文里
    极易与完全无关的段落巧合命中，而 resolution 绑定的是全书共享的既有
    场景库条目——一次巧合命中就会把本集内容错误地挂到别的地方已经登记的
    场景上，风险跟"discovery/direct 只影响这条提及自己"完全不对称。alias/
    alias_inherited/resolution_forward 三个 method 的空锚同样是刻意设计
    （``_prep_pack_scene_alias_provenance`` 故意不把 ``name`` 自身当候选
    以避免同义反复；resolution_forward 的证据本就在前瞻窗口、不在本集），
    不在这里回填，否则会悄悄破坏那两条路径已经验证过的降级语义。

    退让范围限定在这条提及自己声明的段落（``mention_segment_indexes``），
    不是全集搜索——同道具侧 ``prop_literal_or_trailing_anchor`` 同一限定
    口径，只恢复"这条提及自己的证据足够、只是措辞比原文更概括"这一种情形。
    """
    if anchor_phrase or method not in ("direct", "discovery"):
        return anchor_segments, anchor_phrase, False
    declared_text = "".join(
        segments[i - 1].text for i in mention_segment_indexes if 1 <= i <= len(segments)
    )
    phrase = trailing_anchor_phrase([name, source_wording], declared_text)
    if not phrase:
        return anchor_segments, anchor_phrase, False
    hit = [
        i for i in mention_segment_indexes
        if 1 <= i <= len(segments) and phrase in segments[i - 1].text
    ]
    return (hit or anchor_segments), phrase, True
