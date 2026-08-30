"""别名取证——别名声明核验、外观证据核验与桥接章确定性检索。"""
from __future__ import annotations



from app.schemas import (Bible)
from app.source_excerpt import (
    index_source_segments,
)


# 逐字比对时可选脱掉的成对引号：ASCII 直引号与全角引号都要覆盖，因为模型申报
# evidence_quote 时有时会给原本没有引号包裹（或已用另一种引号包裹）的原文自行加上
# 一层引号，导致本来逐字正确的引句因为多出的引号字符核验不过。只有首尾字符恰好配对
# 才脱这一层，不配对的原样保留——不能把原文本身的一部分误当引号脱掉。
_PAIRED_QUOTE_MARKS = (('"', '"'), ('“', '”'), ("'", "'"), ('‘', '’'))


def _quote_comparison_variants(quote: str) -> list[str]:
    """逐字比对用的候选引句形式：原始引句本身，以及（若首尾恰好是成对的引号字符）
    脱掉这对引号后的内文。只脱这一侧（模型申报的引句），原文一侧不做任何改写；
    两种形式中任一比对命中即算通过。"""
    variants = [quote]
    if len(quote) >= 2:
        for open_mark, close_mark in _PAIRED_QUOTE_MARKS:
            if quote[0] == open_mark and quote[-1] == close_mark:
                inner = quote[1:-1]
                if inner:
                    variants.append(inner)
                break
    return variants


def _alias_text_is_independent_appellation(text: str) -> bool:
    """别名是否是能独立指代一个人的称呼，而不是从更长短语里切出来的残片。

    结构性判据，不对任何具体称谓做特判：现代汉语里「的」只作结构助词，永远
    后接于修饰语，所以一个以「的」起头的字符串必然是从更长名词短语的中间切
    开的，它自己不指代任何人。判据只认这一个字——「地」「得」虽然也常作助词，
    却能合法起头（「地煞老祖」「得道真人」），纳进来会误伤真称呼。

    真实事故：模型从原文「杂役处的师兄」里截出「的师兄」登记成主角孟浩的别名，
    而那句话说的根本不是他。这类残片进人物谱之后，下游是按子串匹配用它的：
    app/production/prep_pack.py 的群演候选集（``form in source_text``）、认知卡
    的在场判定都会在任何含「……的师兄」字样的章节里命中，把无关角色拉进候选。
    """
    stripped = (text or "").strip()
    return bool(stripped) and stripped[0] != "的"


def _alias_declaration_verified(
    chapters_by_idx: dict[int, str],
    anchor_texts: set[str],
    text: str,
    evidence_chapter_index: int,
    evidence_quote: str,
) -> bool:
    """别名申报的代码核验：结构性判据，不对任何具体称谓做特判（禁止黑白名单式修复）。

    四个条件必须同时成立，任一不满足就不登记（不确定不登记是安全默认）：
    1. text 本身是个能独立指代人的称呼，不是从更长短语里切出来的残片——见
       `_alias_text_is_independent_appellation`；
    2. evidence_quote 是 evidence_chapter_index 对应章节原文的逐字子串；
    3. text（申报的别名本身）是 evidence_quote 的子串——证据必须真的提到这个别名，
       不能是一句不相干的话；
    4. 该章节原文里还能找到 anchor_texts（角色规范名或已确认的其它别名）中的至少一项——
       证明这条别名与该角色存在共现依据，不是张冠李戴。

    条件 1、2 都按 `_quote_comparison_variants` 产出的候选引句形式判断（原始引句 /
    脱掉一层配对引号后的内文），同一候选形式需要同时满足两个条件才算命中，避免
    "脱引号让子串关系对不上"这种格式噪音误判为证据不足。
    """
    text = (text or "").strip()
    quote = (evidence_quote or "").strip()
    if not text or not quote:
        return False
    if not _alias_text_is_independent_appellation(text):
        return False
    chapter_text = chapters_by_idx.get(evidence_chapter_index, "")
    if not chapter_text:
        return False
    if not any(anchor and anchor in chapter_text for anchor in anchor_texts):
        return False
    return any(
        text in candidate and candidate in chapter_text
        for candidate in _quote_comparison_variants(quote)
    )


# ---------- 外观标志性特征证据核验（王有材事故修复，见 logs/appearance_provenance_plan.md）----------
#
# 根因：`appearance_canonical` 生成 prompt 曾同时放"必须包含 1 个标志性特征"的正向配额和
# "原著未描写处按题材合理补全"的兜底授权——对一个原文毫无外貌描写的角色，这个组合逼模型
# 编造，模型选择的解法是"就近取材"，把同场另一个角色的特征安到了这个角色头上（王有材↔
# 小胖子）。修复分两半：prompt 删掉配额（见 generate_bible/_supplement_bible_characters/
# assess_new_character 的规则 2 文案），并新增本节的结构性核验，逐字核对模型申报的证据。
#
# 40 字上限是关键判据：用真实回归数据实测，王有材事故里"把同场角色特征安到王有材头上"
# 唯一可用的原文句子，从"王有材"三字开头到能覆盖那条借来的特征（"较胖"）为止，最短连续
# 引句需要 44 字——40 字上限让"把别人的描写和这个人的名字圈进同一条引文"在物理上不可能。

APPEARANCE_EVIDENCE_QUOTE_MAX_CHARS = 40


def _appearance_evidence_verified(
    chapters_by_idx: dict[int, str],
    anchor_texts: set[str],
    evidence_chapter_index: int,
    evidence_quote: str,
) -> bool:
    """标志性特征证据核验：结构性判据，不做任何语义分类（禁止黑白名单式修复）。

    两个条件必须同时成立：
    1. evidence_quote 长度 <= APPEARANCE_EVIDENCE_QUOTE_MAX_CHARS，且是
       evidence_chapter_index 对应章节原文的逐字连续子串（按 `_quote_comparison_variants`
       产出的候选引句形式判断，与别名核验同一套脱引号容错）；
    2. 角色规范名（或调用方传入的其它已确认锚点）出现在这条引句本身内部——不是出现在
       整章的其它位置。这是与 `_alias_declaration_verified` 条件 3（整章共现）的关键
       区别：外观证据要求"名字和描写在同一条不超过 40 字的短引句里"，因为整章共现挡不住
       "同一句里名字属于A、描写属于B"这种跨人借用（王有材事故的实际触发路径）。
    """
    quote = (evidence_quote or "").strip()
    if not quote or len(quote) > APPEARANCE_EVIDENCE_QUOTE_MAX_CHARS:
        return False
    chapter_text = chapters_by_idx.get(evidence_chapter_index, "")
    if not chapter_text:
        return False
    for candidate in _quote_comparison_variants(quote):
        if candidate in chapter_text and any(
            anchor and anchor in candidate for anchor in anchor_texts
        ):
            return True
    return False


def _validate_appearance_evidence(bible: Bible, chapters_by_idx: dict[int, str]) -> list[str]:
    """遍历每个角色的 source_evidence，只对非空条目核验；核验失败才产生 error（驱动
    AgentLoop 的修复重试）。空 source_evidence 数组永远不产生 error——诚实的"这个角色
    没有可举证的标志性特征"是安全默认值，不是缺陷信号，不能让"老实说没有"比"编一个能蒙混
    过关的"更差（那会复刻本次事故的激励结构）。

    锚点只用角色规范名，不用 aliases：本函数在 AgentLoop 校验闭包里对候选 Bible 逐轮调用，
    此时 aliases 只是模型本轮申报、尚未经过 `_verify_character_aliases_in_place` 代码核验，
    用未核验的申报去解锁另一项核验会开一个"自证"漏洞——与 `_verify_character_aliases_in_place`
    自身"只用已验证别名扩大锚点集合"的既有规则一致（不确定不采信）。
    """
    errors: list[str] = []
    for i, character in enumerate(bible.characters):
        anchor_texts = {character.name}
        for j, evidence in enumerate(character.source_evidence):
            if _appearance_evidence_verified(
                chapters_by_idx, anchor_texts,
                evidence.evidence_chapter_index, evidence.evidence_quote,
            ):
                continue
            errors.append(
                f"characters[{i}]({character.name}).source_evidence[{j}] 未通过核验：第 "
                f"{evidence.evidence_chapter_index} 章原文里找不到一条不超过 "
                f"{APPEARANCE_EVIDENCE_QUOTE_MAX_CHARS} 字、且与「{character.name}」同句"
                "出现的逐字引句；请换一条真实可查的原文短引句，或直接去掉这个特征，"
                "appearance_canonical 只写通用形态即可，不必凑数。"
            )
    return errors


# ---------- A1a. 桥接章确定性检索（分工修复：模型申报语义，代码检索证据所在地） ----------
#
# 真实回归发现的分工错误：模型申报「李富贵→小胖子」「许清→许师姐」这两条语义假设
# 完全正确，但 evidence_chapter_index 报错了章——它引用的章节里没有角色正式姓名，
# 共现闸（_alias_declaration_verified 条件 3）必然拒绝。根因是让模型去做"记住桥接章
# 在哪"这件事：这是确定性检索，代码扫全部章节又快又准，模型单次扫 15 万字反而漏。
# 参照 app/production/prep_pack.py 的裁决庭范式（_prep_pack_true_name_dossier /
# _prep_pack_true_name_verdict / _prep_pack_pin_dossier_quote：代码检索卷宗 → 模型
# 裁决 → 代码钉证），但这里比裁决庭少一步：裁决庭要解决的是"称谓 X 是否等于人名 Y"
# 这个开放语义判断，需要模型独立裁决；这里模型在申报 character_name+text 这对时，
# 已经做完了"这是不是同一个人/这是不是这个人的别名"的语义判断——剩下的只是纯字符串
# 问题："全书哪一章能同时证明这对申报"，不需要再发一次模型调用去问一个已经有答案的
# 语义问题，代码直接检索、钉证即可（见 _find_alias_bridge_chapter / _alias_bridge_quote）。
# 找不到桥接章 → 维持拒绝（不确定不登记，安全默认不因为多了这条兜底而放松）。
#
# 取句策略修复（事故：双锚定闸上线后状态事实产出率跌到个位数）：`_alias_declaration_
# verified`/`_find_alias_bridge_chapter` 的共现闸按整章判断（text 与 anchor_texts 之一
# 同时出现在章节任意位置即算通过），但旧版 `_alias_bridge_quote` 取句时只看"分段里有
# 没有 text（对象）"，完全不看主体——真实 dry-run 复现："王腾飞→靠山宗""赵武刚→靠山
# 宗""上官修→靠山宗""韩宗→靠山宗" 四条不同主体的归属申报，桥接检索给出的是同一句
# 关于王腾飞的引文，因为那是全书第一个出现"靠山宗"的分段，与被测主体是谁无关——这句
# 引文随后必然被 `_status_fact_quote_dual_anchor_verified`（第四闸）拒绝，不是这些申报
# 真的都是假的，是取句本身没找对句子。反证：同一条"王腾飞→韩宗"在另一次运行里，恰好
# 检索到一句同时提到两人的原文，就正确通过——同一条事实选对句子就过、选错句子就拒，
# 证明第四闸本身没问题，问题在取句优先级。
#
# 修复：`_alias_bridge_dual_anchor_quote` 在桥接章内优先找"同时包含主体锚点与 text"的
# 分段，`_find_alias_bridge_chapter` 相应优先选"存在这种双锚定分段"的桥接章（找不到
# 双锚定分段/双锚定章时逐级回落到原有行为：分段回落到 `_alias_bridge_quote`——第一个
# 含 text 的分段；章节回落到"第一个满足共现闸的章节"——两者都与修复前逐字节一致，不
# 改变找不到双锚定时的输出）。这不是放宽共现闸：章节/分段是否"合格"仍然只看 text 是否
# 逐字出现（不接受主体/对象的其它别名形式顶替 text 本身），双锚定只是在已经合格的分段
# 里，多一层"优先选哪一个"的偏好，不会让原本拒绝的申报被放行，也不会让原本能找到桥接章
# 的申报反而找不到。别名回填与状态事实回填共用同一套函数（`_alias_bridge_quote`/
# `_find_alias_bridge_chapter`），别名场景里"主体"是角色、"对象"是别名文本，双锚定同样
# 成立——回填 dry-run 对照见变更记录，通过条数与逐条明细未受影响。

_ALIAS_BRIDGE_QUOTE_MAX_CHARS = 200  # 引句长度上限：够定位上下文，不整段搬运


def _alias_bridge_quote(chapter_text: str, text: str) -> str | None:
    """从桥接章原文里确定性截取包含 text 的引句：复用 `index_source_segments`
    做自然段/句级切分（与裁决庭卷宗检索同一工具，已处理引号跨段等边界情况），
    取第一个包含 text 的分段——分段本身就是原文的逐字切片，天然满足
    `_alias_declaration_verified` 条件 1（逐字子串）与条件 2（text 是引句子串）。
    调用方已确认 text 在 chapter_text 里，理论上必有命中；找不到时返回 None
    交由调用方兜底拒绝，不强行拼一句可能不含 text 的引句。

    不看主体——这是 `_find_alias_bridge_chapter` 优先找不到双锚定分段/双锚定章时的
    回落取句（见模块顶部"取句策略修复"说明），行为与该函数改名/加主体优先级之前
    完全一致，不单独调用时不受影响。"""
    for segment in index_source_segments(chapter_text, max_chars=_ALIAS_BRIDGE_QUOTE_MAX_CHARS):
        if text in segment.text:
            return segment.text
    return None


def _alias_bridge_dual_anchor_quote(
    chapter_text: str, text: str, subject_anchor_texts: set[str],
) -> str | None:
    """桥接章内优先检索"同时包含主体与对象"的分段（取句策略修复，见模块顶部"取句
    策略修复"说明）：按段号升序找第一个同时满足 text（对象：别名文本，或归属组织/
    关系对象的申报文本）与 subject_anchor_texts（主体：被测角色规范名或已确认别名）
    中至少一项的分段。全书该章内找不到这样的分段时返回 None，交由调用方回落到
    `_alias_bridge_quote`（不看主体，取第一个含 text 的分段——原有行为，不变）。

    不在这里扩大 text 的匹配范围：text 是否出现在某分段，仍然只按逐字子串判断
    （不接受主体/对象的其它别名形式顶替 text 本身）——这是"桥接章/桥接分段是否合格"
    的共现判据，本次不放宽；本函数只是在"已经含 text"的分段集合里，额外多看一眼这一段
    是否也含主体，改变的只是"多个合格分段里优先选哪一个"，不改变"什么算合格"。"""
    if not subject_anchor_texts:
        return None
    for segment in index_source_segments(chapter_text, max_chars=_ALIAS_BRIDGE_QUOTE_MAX_CHARS):
        if text in segment.text and any(
            anchor and anchor in segment.text for anchor in subject_anchor_texts
        ):
            return segment.text
    return None


def _find_alias_bridge_chapter(
    chapters_by_idx: dict[int, str], anchor_texts: set[str], text: str,
) -> tuple[int, str] | None:
    """桥接章确定性检索：扫描全部章节（不受 ALIAS_BACKFILL_SOURCE_BUDGET_CHARS 预算
    限制——那个预算只约束喂给模型的上下文长度，不该约束代码自己的确定性检索范围），
    按章节序号升序找同时包含 text（申报的别名文本/归属组织/关系对象）与 anchor_texts
    （角色规范名或已确认别名）中至少一项的章节——"是否合格"这条共现判据本身不变。

    取句优先级（两轮扫描，取句策略修复见模块顶部说明）：
    1. 第一轮只看"合格章节中，是否存在同时包含 text 与 anchor_texts 之一的分段"
       （`_alias_bridge_dual_anchor_quote`），按章节序号升序取第一个命中的——不是
       任选一个双锚定章，是"最早出现双锚定证据"，与既有"最早共现即已构成充分证据"
       的确定性选择原则一致；
    2. 全部合格章节都没有这样的分段时，回落到原有行为：按章节序号升序取第一个能
       用 `_alias_bridge_quote` 取到引句（第一个含 text 的分段，不看主体）的合格
       章节——与双锚定优先级引入之前逐字节一致。

    两轮都找不到 → 返回 None，调用方维持拒绝。"""
    qualifying = [
        (idx, chapters_by_idx[idx])
        for idx in sorted(chapters_by_idx)
        if text in chapters_by_idx[idx]
        and any(anchor and anchor in chapters_by_idx[idx] for anchor in anchor_texts)
    ]
    for idx, content in qualifying:
        dual_quote = _alias_bridge_dual_anchor_quote(content, text, anchor_texts)
        if dual_quote:
            return idx, dual_quote
    for idx, content in qualifying:
        quote = _alias_bridge_quote(content, text)
        if quote:
            return idx, quote
    return None


# ---------- B. 章级认知卡（确定性组装，零模型调用，见 docs/CHARACTER_COGNITION_LAYER_DESIGN.md
# §4.2） ----------
#
# 三类事实的时间语义完全不同（设计文档 §3），认知卡把这条语义结构化摆出来，供下面 A1b
# 裁决闸（`_alias_verdict_call`）当候选判别的背景参考。本身不发起任何模型调用、不做
# 任何语义判断，只是纯字符串/区间运算：
# - 身份事实（`Character.aliases`）恒真，不受章节号 N 影响，只用于"在场判定"（角色
#   规范名或已确认别名逐字命中本章原文即算在场，与 `_alias_verdict_candidates`
#   同一判据，零语义、不针对具体称谓特判）；
# - 状态事实（`Character.affiliations`/`relations`）带有效区间，按"截至第 N 章"过滤
#   （`valid_from_chapter <= N` 且 `valid_to_chapter` 为空或 `>= N`），避免拿后期状态
#   描写当下（§3.2），区间重叠时同一归属对象/关系对象只取最近生效的一条，与
#   `character_portraits` 表 `ORDER BY ep_start DESC LIMIT 1` 的既有惯例同构
#   （`app/portraits.py` `portrait_for_episode`）；
# - 前瞻信号（`forward_appearance_hits`）复用既有 `CHARACTER_IMPORTANCE_FORWARD_
#   CHAPTERS` 前瞻窗口常量（`app/portraits.py`），统计方式与 `_recurring_character_
#   names` 的 `window_raw.count(name)` 同一惯例（§3.3），不新造常量、不重新发明统计
#   口径。本次只组装该字段供未来消费点使用（§9 P1 第 7 项——未具名角色建卡触发点，
#   本次不实现触发逻辑本身）；当前唯一接入的裁决闸注入（下方 A1b）不读取这个字段，
#   只用 affiliations_as_of/relations_as_of。
#
# 同一 (bible 快照, chapter_idx, forward_window_chapters) 输入任何时候重建结果逐字节
# 相同（§11 判据 2）：不依赖模型调用，`bible.characters`/`character.affiliations`/
# `character.relations` 都是列表，遍历顺序本身就是确定的。

CHAPTER_COGNITION_CARD_MAX_CHARACTERS = 8  # 单卡最多收录角色数：裁决闸候选集通常就是该
    # 章出场的人物谱角色，规模个位数（`_alias_verdict_candidates`）；8 留出冗余，同时
    # 防止人物扎堆的章节把提示词拖长——超限时按调用方给定范围内 bible.characters 的
    # 原始顺序截断，不做二次排序，保证可复现。
CHAPTER_COGNITION_FACTS_MAX_PER_KIND = 3  # 每个角色的 affiliations_as_of / relations_as_of
    # 各自最多展示的条数：每条状态事实入库前都要单独过一遍候选判别裁决（§4.1），门槛
    # 高，正常不会在单角色名下堆积大量条目；3 条留有冗余又不至于让单个角色的背景摘要
    # 过长——超限按角色 affiliations/relations 的原始登记顺序（区间去重取最新一条后）
    # 截断，不做二次排序。
CHAPTER_COGNITION_SUMMARY_MAX_CHARS = 60  # 单条归属/关系摘要（org/to + relation_kind
    # 拼装后）最长字符数：org/relation_kind 是模型自由文本，理论上无长度上限，必须有
    # 硬顶防止个别异常长文本拖长提示词；60 字足够容纳"血妖宗（效忠），第X章证据"这类
    # 正常长度还留有余量。
