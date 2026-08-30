"""Unresolved functional-label candidate dossier building: the roster/anchor-
pool/dossier construction used to disambiguate a functional-extra label
against candidate bible identities.

Split out of app/production/prep_pack.py.
"""
from __future__ import annotations

import re
from app.schemas import Bible
from app.source_excerpt import SourceSegment
from typing import Any


_PREP_PACK_FUNCTIONAL_CANDIDATE_NO_MATCH_LABEL = "都不是/无法确定"
_PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MAX_ENTRIES = 12  # 单条候选判别卷宗最多收录的段落数
_PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MAX_CHARS = 6000  # 单条候选判别卷宗最多收录的总字符数
# A 侧（事件跨度段+标签字面段）保底配额——见 PREP_PACK_VERSION 上方 1.8.2
# 大注释的完整根因。1.8.3 起这个常量只约束 A 侧一家：B 侧的保底改为"每个
# 候选至少一段"（见下面 _prep_pack_functional_candidate_dossier 的按候选
# 保底逻辑），不再是一个笼统的 B 侧总量数字——1.8.2 用同一个常量给 A、B
# 两侧各分 4 条，A 侧那 11 段大段外貌/环境描写几乎吃光 MAX_CHARS 时，B 侧
# 名义上保底 4 条实际只有 1 条真正挤进卷宗（1.8.3 大注释根因一）；同时
# 4 条位置数字本身也无法阻止候选轮转顺序里排最前的候选独占那唯一挤进去的
# 名额（1.8.3 大注释根因二）。取值 4 保留给 A 侧：标签指涉对象所在的现场
# 本身（事件跨度/标签字面）同样是判别必需材料，不能因为改成"每候选保底"
# 就彻底不留位置；A 侧段落多时这份保底优先让位给"每候选至少一段"这个更
# 具体的硬要求（见 dossier 函数 reserve_a 的计算）。
_PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MIN_SIDE_ENTRIES = 4
# 1.8.3 新增：保底层（A 侧保底 + 每候选保底）单段文本被截断时的目标长度
# 上限，见 PREP_PACK_VERSION 上方 1.8.3 大注释根因一、
# _prep_pack_functional_candidate_truncate_segment 完整说明。取值 260：
# 真实事故的决定性证据句"绿袍男子对着她躬身行礼，口称许师姐，随后请四人
# 随他回宗门"不到 40 字，260 字对绝大多数单句/单个小段落都绰绰有余、
# 几乎不会触发截断；即使 MAX_ENTRIES(12) 全部落在保底层这种极端场景，
# 12×260=3120 字仍明显小于 MAX_CHARS(6000)，保底层因此永远不需要再跟
# flex 层抢字数预算——这正是修复根因一的关键：保底层的收录与截断只取决于
# 单段自身长度，不取决于其它段落已经用掉多少字数，天然确定性、跟处理
# 顺序无关。不是靠放大 MAX_ENTRIES/MAX_CHARS 绕过问题，两个既有上限常量
# 原样不变。
_PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_GUARANTEED_ENTRY_MAX_CHARS = 260
_PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_TRUNCATION_MARK = "…"


def _prep_pack_functional_candidate_roster(bible: Bible) -> dict[str, list[str]]:
    """候选面快照：规范名 -> [规范名, 已确认别名...]。``bible.characters[].
    aliases`` 里的每一条本就是全书分析阶段模型申报 + 代码核验通过后才落库
    的确认别名（app.schemas.CharacterAlias，见该类 docstring），这里不需要
    重新核验证据锚点，直接读取文本即可——核验是全书分析阶段
    （app.stages.generate_bible）的职责，不在本文件重复。跟
    app.stages._alias_verdict_roster 同一构造，两个模块不互相导入内部
    函数，各自独立实现一份。"""
    return {
        character.name: [character.name, *(alias.text for alias in character.aliases)]
        for character in bible.characters
    }


def _prep_pack_functional_candidate_names(
    source_text: str, roster: dict[str, list[str]],
) -> list[str]:
    """结构判据，零语义：规范名或其任一已确认别名逐字子串命中本集
    ``source_text``，即算该角色在本集"出场"，候选入选。不针对任何具体
    人名/姓氏做特判。返回值按 ``roster``（即 bible.characters 原始登记
    顺序构造的字典，Python 字典保序）顺序去重——一个角色只要任一称谓命中
    就只计入一次。"""
    return [
        name for name, surface_forms in roster.items()
        if any(form and form in source_text for form in surface_forms)
    ]


def _prep_pack_functional_candidate_label_segments(
    character_mentions: list[dict[str, Any]], label: str,
) -> set[int]:
    """标签 -> 该标签自己申报的段号并集（2.0.0，直接替代 1.8.1 引入的
    ``_prep_pack_functional_candidate_event_span_segments``；见
    PREP_PACK_VERSION 上方 2.0.0 大注释"新增 props"一节之前那段、
    _prep_pack_functional_candidate_dossier 的完整说明）：结构判据，零语义。

    1.8.1 引入"事件跨度"这层间接锚点的根因是：标签本身常是模型转述/综合出
    的描述短语（真实事故："银色长袍女子"原文逐字出现 0 次），靠标签字面
    匹配原文定位卷宗会打空；2.0.0 下每条 ``_ModelCharacterMention`` 已经
    直接自报 ``segment_indexes``（这个提及自己声称在哪些段落画面出场，
    已经过 _prep_pack_gate_segment_indexes 的结构闸——落在它自己所属 chunk
    范围内，但刻意不要求逐字命中，见该函数说明：这正是"银色长袍女子"这类
    合成标签仍能被正确定位到自己声称的段落的原因），不再需要"先分事件、
    再从事件的粗粒度跨度反推段号"这层间接：同一个 label 字符串在多条提及
    （可能来自不同 chunk）里出现过的全部 segment_indexes 取并集，就是比
    事件跨度更精确（不含跨度内不相关的中间段落）的同一份材料。"""
    matched: set[int] = set()
    for mention in character_mentions:
        if str(mention.get("display_name") or "").strip() != label:
            continue
        matched.update(int(index) for index in mention.get("segment_indexes") or [])
    return matched


def _prep_pack_functional_candidate_anchor_pool(
    segments: list[SourceSegment], label: str,
    candidate_anchor_texts: dict[str, list[str]],
    event_span_indexes: list[int], event_span_index_set: set[int],
) -> tuple[list[int], list[int], dict[str, list[int]]]:
    """A 侧第二层（label 逐字命中段）与 B 侧（候选锚点段落，按候选公平
    轮转合并）的联合检索，1.8.2 新增，见 PREP_PACK_VERSION 上方 1.8.2
    大注释、_prep_pack_functional_candidate_dossier 的完整说明。两者共用
    同一次 segments 扫描（避免重复遍历）：``label`` 逐字命中的段落只在
    "不属于事件跨度"时才归入 A 侧的 ``label_text_indexes``（事件跨度内的
    label 命中已经算 A 侧了，不需要重复计入）；候选锚点匹配对全部段落
    执行，不因为某段已被 ``event_span_index_set`` 收录就跳过（1.8.4 修复，
    见 PREP_PACK_VERSION 上方 1.8.4 大注释：真实事故——候选"许清"确认别名
    "许师姐"命中的两处段落都恰好落在事件跨度并集内部，旧版在这里直接
    `continue` 跳过候选匹配，导致这个候选从"每候选保底"的输入集合
    ``per_candidate_indexes`` 里彻底消失，保底对它形同虚设）。这意味着
    某段落现在可能同时是事件跨度成员、又是某个候选的锚点段——这是
    有意允许的重叠（该候选的"每候选保底"需要知道这段属于它，不管这段
    是否也在事件跨度集合里；见 _prep_pack_functional_candidate_dossier
    对 ``guaranteed_b_anchor``/``primary_index_set`` 重叠时的优先级说明）。

    1.8.3 起额外返回 ``per_candidate_indexes``（分组结果本身，未经轮转
    合并）——供 ``_prep_pack_functional_candidate_dossier`` 计算"每个候选
    至少一段"这一按候选粒度的硬性保底（见该函数 docstring 与 PREP_PACK_
    VERSION 上方 1.8.3 大注释）：那个保底必须精确知道某个候选自己最近的
    锚点段落是哪一条，轮转合并之后的 ``anchor_pool_ordered`` 只是"混合好
    的一份列表"，不再能反查某一段究竟满足了哪个候选的保底，所以两者都要
    返回。

    B 侧排序是本函数真正新增的部分——1.8.1 把全部候选的锚点段落混在一起
    按"离案发现场的邻近度"整体排序，本轮真实事故的第二个根因正出在这里：
    整体排序下，本章出场次数越多的候选，越多段落挤进排序靠前的位置，一旦
    配额有限（受 A 侧保底挤压后更是如此），出场次数少的候选会被排到全部
    落选——跟"A 侧全收挤没 B 侧"是同一个"主角淹没预算"陷阱，只是这次发生
    在 B 侧内部的候选粒度，光靠 A/B 两侧保底配额堵不住。

    做法：先按候选分组，每个候选自己的锚点段落仍按"离主锚点（事件跨度
    段落；事件跨度为空时退回 label 命中段落）的邻近度升序、距离相同段号
    升序"排序——邻近度规则本身不变，只是分组后各自排序，不再混在一起
    整体排序；随后按"每个候选轮流各出一段"合并：候选①最近的一段、候选②
    最近的一段、……、候选①第二近的一段、候选②第二近的一段、……
    （``candidate_anchor_texts`` 的 key 顺序即调用方 ``candidates`` 的既有
    确定性顺序，见 _prep_pack_functional_candidate_names 的 roster 保序
    说明）。这样任何前缀配额下的候选出现次数最多相差 1——不管某个候选在
    原文里反复出场多少次，只要还没轮到把其它候选的锚点段落全部轮完，它
    都不会占用超过"自己的份额+1"的位置。同一段落同时命中多个候选（原文
    同段提到两人）按候选顺序谁先轮到归谁，其余候选这一轮空转、不影响
    自己后续轮次的进度，也不重复计入卷宗或重复占用配额。"""
    label_text_indexes: list[int] = []
    per_candidate_indexes: dict[str, list[int]] = {name: [] for name in candidate_anchor_texts}
    for index, seg in enumerate(segments):
        if index not in event_span_index_set:
            if label and label in seg.text:
                label_text_indexes.append(index)
                continue
        # 段落落在事件跨度内时不再跳过候选匹配（1.8.4 核心修复，见本函数
        # docstring"跨度吞并候选锚点"一节）：跳过只对 label 逐字匹配这一
        # 分支有意义（事件跨度本身已经是 A 侧主锚点，label 命中不需要再
        # 重复计入 label_text_indexes），候选锚点匹配必须覆盖全部段落，
        # 否则候选自己唯一的证据段落只要恰好落在事件跨度范围内，就永远
        # 进不了 per_candidate_indexes，每候选保底对它形同虚设。
        for name, forms in candidate_anchor_texts.items():
            if any(form and form in seg.text for form in forms):
                per_candidate_indexes[name].append(index)
    # 邻近度参照点：优先事件跨度段落（"离案发现场的远近"）；事件跨度为空
    # 时退回 label 命中段落——事件跨度缺失/为空的防御性回退，跟 1.8.1 的
    # 既有语义完全一致。两者都为空时（label 是转述短语、原文无字面）候选
    # 段落没有邻近度参照点，保持扫描得到的文档顺序，等价于改造前的既有
    # 行为，不引入新的失败模式。
    proximity_anchor = event_span_indexes or label_text_indexes
    if proximity_anchor:
        for indexes in per_candidate_indexes.values():
            indexes.sort(
                key=lambda index: (min(abs(index - anchor) for anchor in proximity_anchor), index),
            )
    candidate_order = list(candidate_anchor_texts.keys())
    seen: set[int] = set()
    anchor_pool_ordered: list[int] = []
    max_round = max((len(indexes) for indexes in per_candidate_indexes.values()), default=0)
    for round_idx in range(max_round):
        for name in candidate_order:
            indexes = per_candidate_indexes[name]
            if round_idx >= len(indexes):
                continue
            index = indexes[round_idx]
            if index in seen:
                continue
            seen.add(index)
            anchor_pool_ordered.append(index)
    return label_text_indexes, anchor_pool_ordered, per_candidate_indexes


def _prep_pack_functional_candidate_truncate_segment(text: str, anchor: str) -> str:
    """确定性截断（1.8.3 新增，见 PREP_PACK_VERSION 上方 1.8.3 大注释根因一、
    _prep_pack_functional_candidate_dossier 的完整说明）：保底层的段落绝不
    因为字数超限被整条丢弃——某个候选唯一的锚点证据段如果恰好很长（大段
    外貌/环境描写），必须截断而不是排除，模型才有机会看到它。

    ``anchor`` 是这段文本之所以入选保底层的那个具体触发词（A 侧：``label``
    本身；B 侧：命中该候选的那个规范名/别名字面串），用来定位"核心句"——
    先用中文常见句子终止符（。！？换行）把 ``text`` 切成句子，取包含
    ``anchor`` 的那一句；这句本身仍超过目标长度时，以 ``anchor`` 在句中的
    位置为中心继续裁剪，保证锚点词始终留在截断结果里（截掉的是锚点词
    两侧的上下文，不是锚点词本身）。裁剪掉的一侧加省略标记，让下游读者/
    模型知道这不是段落全文。``anchor`` 为空或在 ``text`` 里根本找不到
    （防御性：调用方按约定只会传入确实命中该段的锚点词，但不假设这个约定
    一定成立，找不到时不崩、不猜句子边界）时退回"从头部截断到目标长度"这
    个更保守的兜底，不做任何"哪句更重要"的语义判断。

    不针对任何具体人名/称谓做特判——``anchor`` 完全是调用方传入的字符串
    参数，本函数只做纯字符串定位与切片，是结构操作，不是语义理解。"""
    limit = _PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_GUARANTEED_ENTRY_MAX_CHARS
    if len(text) <= limit:
        return text
    mark = _PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_TRUNCATION_MARK
    anchor_pos = text.find(anchor) if anchor else -1
    if anchor_pos < 0:
        return text[:limit].rstrip() + mark
    # 用常见中文句子终止符切句边界，取锚点词所在的那一句：从头扫描终止符，
    # 锚点词之前最近的终止符是句首，锚点词之后最近的终止符是句尾。
    start, end = 0, len(text)
    for match in re.finditer(r"[。！？\n]", text):
        boundary = match.end()
        if boundary <= anchor_pos:
            start = boundary
        else:
            end = boundary
            break
    if end - start > limit:
        # 核心句本身仍超限：以锚点词在句中的位置为中心继续裁剪，锚点词
        # 始终落在裁剪窗口内。
        local_pos = anchor_pos - start
        half = max(0, (limit - len(anchor)) // 2)
        crop_start = start + max(0, local_pos - half)
        crop_end = min(end, crop_start + limit)
        crop_start = max(start, crop_end - limit)
        start, end = crop_start, crop_end
    core = text[start:end].strip()
    prefix = mark if start > 0 else ""
    suffix = mark if end < len(text) else ""
    return f"{prefix}{core}{suffix}"


def _prep_pack_functional_candidate_dossier(
    segments: list[SourceSegment], label: str,
    candidate_anchor_texts: dict[str, list[str]],
    event_span_segments: set[int] = frozenset(),
) -> list[dict[str, Any]]:
    """裁决卷宗检索（1.8.3 保底粒度下沉到"每个候选"，见 PREP_PACK_VERSION
    上方 1.8.3 大注释的完整根因——真实事故：1.8.2 的 A/B 两侧保底配额确实
    让"许师姐"那一段挤进了卷宗，但只挤进 1 段，且被候选轮转顺序里排最前的
    主角类候选占了，真正的目标候选一段都没拿到）。

    两侧证据来源基本不变（跟 1.8.1/1.8.2 一致，这里不重复根因，只重复
    形状）：
    - A 侧＝``primary_indexes``＝两层主锚点的并集：①``event_span_segments``
      （该标签所属事件的 source_span 覆盖段落，见
      _prep_pack_functional_candidate_event_span_segments）②``label``
      逐字命中原文的段落（未被①收录的部分）；
    - B 侧＝候选（规范名∪已确认别名）逐字命中的段落，按候选分组，见
      _prep_pack_functional_candidate_anchor_pool 的完整说明。1.8.4 起
      B 侧不再排除事件跨度内的段落（见该函数 docstring 与 PREP_PACK_
      VERSION 上方 1.8.4 大注释）——A、B 两侧因此可能重叠：某个候选的
      锚点段恰好也落在事件跨度内是允许的、甚至是这次要修的真实事故本身
      （候选"许清"的锚点段落在事件跨度内部，1.8.1-1.8.3 因为 B 侧扫描
      跳过事件跨度内的段落而对它完全不可见）。

    1.8.3 的核心改动——按候选粒度的硬性保底：``candidate_anchor_texts``
    保序遍历，每个确有锚点证据（``per_candidate_indexes[name]`` 非空）的
    候选，独立取自己离主锚点最近的那一段（``indexes[0]``，已在
    anchor_pool 里按邻近度排好序）纳入保底层——不是"B 侧保底 N 条"这个
    笼统的位置数字，而是"每个候选各自的保底"，谁的锚点证据都不会被另一个
    候选或 A 侧挤没。两个候选的最近段恰好是同一段（原文同段提到两人）时
    天然去重——那一段同时满足两者的保底，不重复计入。A 侧的既有保底
    （``_PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MIN_SIDE_ENTRIES``）这次
    优先让位给每候选保底：先扣掉每候选保底已经用掉的条数名额，A 侧保底
    只取"剩余名额"与"自身可用条数"两者较小值——A 侧段落越多，让位越明显，
    但只要还有剩余名额就仍有代表段（标签指涉对象所在的现场本身也是判别
    必需材料，不能因为改成"每候选保底"就彻底清零）。

    保底层（A 侧保底 + 每候选保底）在收录阶段一律直接收录，绝不因为字数
    预算不够被跳过——这是修复的第二个关键点，也是本轮事故的直接原因（见
    1.8.3 大注释根因一：1.8.2 的"配额位置"并不保证"配额一定进得去卷宗"）。
    单段文本超过 ``_PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_GUARANTEED_
    ENTRY_MAX_CHARS`` 时用 ``_prep_pack_functional_candidate_truncate_
    segment`` 做确定性截断（保留含锚点词的核心句 + 省略标记），不整段
    丢弃——截断只取决于该段自身长度，不取决于其它段落已经用掉多少字数，
    因此天然与处理顺序无关、确定性可复现，也不会因为保底层本身而突破
    MAX_CHARS（见该常量上方注释的预算测算）。保底层收录完毕后，剩余的
    "flex"名额才按原有优先级规则继续分配：A 侧剩余部分（仍是"主锚点必须
    优先"的既有语义）在前、B 侧剩余部分（仍按候选轮转顺序）在后，这部分
    维持 1.8.2 既有语义——字数预算不够时按原样跳过、不截断（不是任何
    候选的唯一证据，缺了也不影响"每候选至少一段"这个硬要求）。这不是
    简单地把 MAX_ENTRIES/MAX_CHARS 两个上限放大，两个常量原样不变，只是
    同一份预算内部的分配规则更细颗粒度。

    退化场景（不崩、不引入新失败模式，跟 1.8.1/1.8.2 同一纪律）：某个
    候选没有任何锚点证据时（理论上不应发生，候选正是靠 anchor 命中本集
    才入选的——见 _prep_pack_functional_candidate_names，候选集单一来源，
    1.8.4 回退了 1.8.3 曾短暂引入的"人物谱注册区间"乙类来源，见
    PREP_PACK_VERSION 上方 1.8.4 大注释），该候选保底跳过；主锚点整体为空
    时 A 侧保底与 flex 都退化为 0，B 侧可用满 MAX_ENTRIES 预算；两侧都为空
    时返回空列表，交由调用方按既有防御性分支处理。"""
    total_segments = len(segments)
    # 主锚点第一层：段号来自调用方传入的事件跨度集合，可能包含越界/非法
    # 值（防御性输入，不假设调用方一定传的是干净数据）——落在
    # [1, total_segments] 之外的一律丢弃；转 0-based 并按段号升序排序，
    # 截断顺序完全由段号本身决定，不依赖 set 的遍历顺序（确定性纪律）。
    event_span_indexes = sorted(
        {index - 1 for index in event_span_segments if 1 <= index <= total_segments},
    )
    event_span_index_set = set(event_span_indexes)
    label_text_indexes, anchor_pool_ordered, per_candidate_indexes = (
        _prep_pack_functional_candidate_anchor_pool(
            segments, label, candidate_anchor_texts, event_span_indexes, event_span_index_set,
        )
    )
    primary_indexes = event_span_indexes + label_text_indexes
    primary_index_set = event_span_index_set | set(label_text_indexes)

    # 每候选保底（1.8.3 核心改动，见本函数 docstring）：candidate_anchor_
    # texts 保序遍历，每个确有锚点证据的候选取自己最近的一段；与另一候选
    # 共享同一段时天然去重（该段同时满足两者的保底）。同时记录命中该段的
    # 具体锚点词（该候选的哪个规范名/别名字面命中了这段文本），供下面截断
    # 时定位核心句。
    guaranteed_b_indexes: list[int] = []
    guaranteed_b_anchor: dict[int, str] = {}
    guaranteed_b_seen: set[int] = set()
    for name in candidate_anchor_texts:
        indexes = per_candidate_indexes.get(name) or []
        if not indexes:
            continue
        pick = indexes[0]
        if pick in guaranteed_b_seen:
            continue
        guaranteed_b_seen.add(pick)
        guaranteed_b_indexes.append(pick)
        pick_text = segments[pick].text
        guaranteed_b_anchor[pick] = next(
            (form for form in candidate_anchor_texts[name] if form and form in pick_text), "",
        )
    overflow_b = [index for index in anchor_pool_ordered if index not in guaranteed_b_seen]

    # A 侧保底名额优先让位给每候选保底（见本函数 docstring）：先扣掉每
    # 候选保底已经用掉的条数名额，A 侧保底只取剩余名额、自身可用条数、
    # 既有 MIN_SIDE_ENTRIES 三者的较小值。
    remaining_slots = max(
        0, _PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MAX_ENTRIES - len(guaranteed_b_indexes),
    )
    reserve_a = min(
        _PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MIN_SIDE_ENTRIES, len(primary_indexes), remaining_slots,
    )
    guaranteed_a, overflow_a = primary_indexes[:reserve_a], primary_indexes[reserve_a:]

    guaranteed_indexes = guaranteed_a + guaranteed_b_indexes
    flex_indexes = overflow_a + overflow_b

    selected: list[int] = []
    used_chars = 0
    rendered: dict[int, str] = {}

    # 保底层：一律收录，绝不因为字数超限被跳过（本轮修复的直接根因，见
    # 本函数 docstring）；单段超过 GUARANTEED_ENTRY_MAX_CHARS 时做确定性
    # 截断，不整段丢弃。
    for index in guaranteed_indexes:
        if len(selected) >= _PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MAX_ENTRIES:
            break
        if index in rendered:
            continue
        seg_text = segments[index].text
        # 候选自身锚点词优先于 label（1.8.4）：候选保底段现在可能同时落在
        # 事件跨度内（见 anchor_pool 的 docstring），此时截断核心句必须仍然
        # 围绕这个候选真正命中的锚点词（如"许师姐"），不能被"这段也在事件
        # 跨度里所以用 label 定位"覆盖掉——label 未必逐字出现在这段里，用它
        # 当锚点会退化成从头截断，可能把候选证据本身截没。
        anchor_hint = (
            guaranteed_b_anchor[index] if index in guaranteed_b_anchor
            else (label if index in primary_index_set else "")
        )
        text = _prep_pack_functional_candidate_truncate_segment(seg_text, anchor_hint)
        selected.append(index)
        rendered[index] = text
        used_chars += len(text)

    # flex 层：维持 1.8.2 既有语义——补充材料，不是任何候选的唯一证据，
    # 预算不够就跳过，不截断。
    for index in flex_indexes:
        if len(selected) >= _PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MAX_ENTRIES:
            break
        if index in rendered:
            continue
        seg_text = segments[index].text
        if selected and used_chars + len(seg_text) > _PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MAX_CHARS:
            continue
        selected.append(index)
        rendered[index] = seg_text
        used_chars += len(seg_text)

    selected.sort()
    return [
        {"segment_index": index + 1, "text": rendered[index]}
        for index in selected
    ]


