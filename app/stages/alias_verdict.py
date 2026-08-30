"""别名取证——桥接章原文独立裁决闸（模型裁决 + 代码钉证）。"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.harness import model_gateway
from app.source_excerpt import (
    index_source_segments,
)

from .bible_shared import _bible_short_json_call_meta
from .cognition import ChapterCognitionCard, _cognition_status_lines


# ---------- A1b. 裁决闸：桥接章原文独立裁决（补上"同章共现"证明不了"指同一人"的漏洞） ----------
#
# 真实误登记事故：全书别名回填写库后核验发现「孟浩←虎爷爷」（第 3 章）——第 3 章原文
# 里「虎爷爷」明确是欺负孟浩的另一个魁梧大汉，根本不是孟浩本人。根因：`_alias_
# declaration_verified`/`_find_alias_bridge_chapter`（条件 3）只证明"别名文本与角色
# 规范名在同一章出现"，证明不了"这个别名指的就是这个角色"——指代关系是模型在没看到
# 桥接章原文的情况下凭全书记忆断言的（它把这个称谓和另一段记忆搞混了）。且主角类角色
# 几乎每章都出场，共现闸对这类角色的过滤力接近零：随便一个同章出现的称谓，不管是不是
# 主角本人，都能通过共现闸。
#
# 修复：回到项目既有的裁决庭范式（`app/production/prep_pack.py` 的
# `_prep_pack_true_name_dossier` / `_prep_pack_true_name_verdict` /
# `_prep_pack_pin_dossier_quote`：代码检索卷宗 → 独立模型裁决 → 代码钉证），在代码
# 定位到桥接章（或模型自己申报的章节已经通过共现闸）之后，带着该章的真实原文段落
# 再做一次独立裁决调用，问模型"依据这些原文，称谓 X 是否指代角色 Y 本人"，三态回答
# same/different/uncertain，uncertain 与 different 一律拒绝登记（不确定不登记，安全
# 默认）。
#
# 钉证方式：引用卷宗段号，不要求模型逐字复述原文。最初的实现要求模型的
# supporting_quote 逐字（经引号规范化后）命中卷宗某条，线上复核暴露这个钉证方式本身
# 不可靠——"李富贵←小胖子"（第 10 章）与"上官修←上官师叔"两条本该通过的正确别名，
# 分别被 quote_not_pinned 误杀、以及同一输入两次复核给出不同结果，根因是模型转录
# 原文时会跨段拼接、加省略号、微调标点，这些噪音跟"证据是否成立"无关，却被当成了
# 拒绝理由。改为让模型在 verdict 之外只需引用卷宗目录里某一条的段号
# （supporting_segment_index），JSON Schema 用 enum 把候选值限定为本次卷宗实际收录的
# 段号集合（参照 `app/portraits.py` `_current_identity_schema()` 给 `evidence_ref`
# 注入 enum 的写法），钉证退化为一次整数是否落在集合内的结构性判断——模型选中的段落
# 本身就是代码检索出的真实原文，无从编造，也不存在转录误差。supporting_quote 保留为
# 可选的观测字段（写进裁决通过日志，便于人工复核），不再是钉证硬闸的一部分。
#
# 卷宗构造是确定性的：只从已经定位到的那一章取证据（不是整章、也不是全书），取该章
# 里包含别名文本 `text` 和/或角色规范名 `true_name` 的自然段（与
# `_prep_pack_true_name_dossier` 同一检索原则，缩小到已定位的单章）——两者共现的
# 段落、以及只含 `text` 的段落必须收录；只含 `true_name` 的段落按"离最近的别名相关
# 段落有多近"补足剩余预算（不是按章节开头起的文档顺序），因为桥接章里真正点明
# `true_name` 身份的那段，很多时候并不挨着 `text` 出现，而 `true_name` 若是主角，
# 几乎每段都会出现——按文档顺序截断会被开头大段无关独白占满预算，把真正有用的
# `true_name` 段落挤出去（真实回归：project proj_3ac0b627fa46 第 1 章"孟兄"只出现
# 一次，"孟浩"贯穿全章出现三十余次，见 `_alias_verdict_dossier` 的完整说明）。条数
# 与总字数超过上限时按"两者共现段落 / 只含别名段落全部优先、只含真名段落按接近别名
# 段落的程度补足预算"的确定性规则截断——不用随机采样，同一输入任何时候重跑都得到
# 同一份卷宗。

_ALIAS_VERDICT_DOSSIER_MAX_ENTRIES = 12  # 单条别名裁决卷宗最多收录的段落数
_ALIAS_VERDICT_DOSSIER_MAX_CHARS = 6000  # 单条别名裁决卷宗最多收录的总字符数
# 三层保底配额（移植自 app/production/prep_pack.py 已用两轮真实生产事故验证过的
# "按层保底配额、保底不受字数预算挤占"方案，见提交 0395a73「候选判别卷宗按层保底
# 配额，杜绝一侧饿死」与 1f15844「卷宗每候选保底不受字数挤占」；缺陷修复见下方
# `_alias_verdict_dossier` docstring"第二个真实回归"一节）：both/text_only/
# anchor_only 三层各自的保底名额，任一层都不能被其它层挤到 0。
#
# 取值 4：不是另起炉灶拍的新数字——prep_pack 那两次修复面对的是同一形状的问题，
# 且单卷宗最多收录条数上限恰好同为 12（`_PREP_PACK_FUNCTIONAL_CANDIDATE_
# DOSSIER_MAX_ENTRIES == _ALIAS_VERDICT_DOSSIER_MAX_ENTRIES == 12`），两轮真实
# 生产事故验证后收敛到的保底值就是 4，这里直接复用同一常量值，不重新调参。3 层
# × 4 = 12，恰好等于 MAX_ENTRIES：三层证据都充足（各自 >= 4 条可用）时保底阶段
# 直接占满预算，谁都不会被挤到 0；某一层可用证据不足 4 条时（如 both，按下方
# docstring 所述通常很少），它的保底天然只取到自己实际拥有的条数，节省下的名额
# 通过下面的 flex 阶段（仍按 both -> text_only -> anchor_only 既有优先级）分给
# 证据更多的层，不需要另写"回收"逻辑。
_ALIAS_VERDICT_DOSSIER_MIN_LAYER_ENTRIES = 4
# 保底段的单段截断上限（1f15844 同一根因：条数保底如果仍受字数预算约束，会被排
# 在它前面的长段落吃光字数额度，保底名额有位置却进不了卷宗——1f15844 提交信息
# 原话"保底的是'配额位置'，不是'配额一定进得去卷宗'"）。复用 prep_pack
# `_PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_GUARANTEED_ENTRY_MAX_CHARS` 同一
# 取值 260：三层保底最多 12 段（3x4=12，即上面注释的最坏情形），12x260=3120，
# 仍明显小于 MAX_CHARS(6000)，保底阶段因此不需要跟 flex 阶段抢字数预算。
_ALIAS_VERDICT_DOSSIER_GUARANTEED_ENTRY_MAX_CHARS = 260
_ALIAS_VERDICT_DOSSIER_TRUNCATION_MARK = "…"


def _alias_verdict_dossier_truncate_segment(text: str, anchor: str) -> str:
    """保底段确定性截断（移植自 prep_pack.py
    `_prep_pack_functional_candidate_truncate_segment`，逻辑不变，仅随宿主函数
    改名）：保底层的段落绝不因为字数超限被整条丢弃——某个层唯一/仅有的证据段
    如果恰好很长（大段环境描写、大段对话），必须截断而不是排除，模型才有机会
    看到它。

    `anchor` 是这段文本之所以入选保底层的具体触发词（调用方按层传入：both/
    text_only 传 `text` 本身——has_text 恒为真，必然能找到；anchor_only 传命中
    的那个具体 anchor 字面串），用来定位"核心句"：先用中文常见句子终止符
    （。！？换行）把 `text` 切成句子，取包含 `anchor` 的那一句；这句本身仍超过
    目标长度时，以 `anchor` 在句中的位置为中心继续裁剪，保证锚点词始终留在
    截断结果里（截掉的是锚点词两侧的上下文，不是锚点词本身）。裁剪掉的一侧加
    省略标记。`anchor` 为空或在 `text` 里根本找不到（防御性：调用方按约定只会
    传入确实命中该段的锚点词，但不假设这个约定一定成立）时退回"从头部截断到
    目标长度"这个更保守的兜底，不做任何"哪句更重要"的语义判断。不针对任何
    具体人名/称谓做特判——`anchor` 完全是调用方传入的字符串参数，本函数只做
    纯字符串定位与切片，是结构操作，不是语义理解。"""
    limit = _ALIAS_VERDICT_DOSSIER_GUARANTEED_ENTRY_MAX_CHARS
    if len(text) <= limit:
        return text
    mark = _ALIAS_VERDICT_DOSSIER_TRUNCATION_MARK
    anchor_pos = text.find(anchor) if anchor else -1
    if anchor_pos < 0:
        return text[:limit].rstrip() + mark
    start, end = 0, len(text)
    for match in re.finditer(r"[。！？\n]", text):
        boundary = match.end()
        if boundary <= anchor_pos:
            start = boundary
        else:
            end = boundary
            break
    if end - start > limit:
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


def _alias_verdict_dossier(
    chapter_idx: int, chapter_text: str, text: str, anchor_texts: set[str],
) -> list[dict[str, Any]]:
    """裁决卷宗检索：零语义，纯字符串包含判断，只从已定位到的这一章本身取证，不整章
    塞给模型。把该章按自然段切分（`index_source_segments`）后分三类：
    - both：同段同时含别名 `text` 与 `anchor_texts`（角色规范名、或本角色已确认的
      其它别名）中至少一项——最直接的证据；
    - text_only：只含 `text`——必须收录，模型至少要看到别名本身被怎么用的；
    - anchor_only：只含 `anchor_texts` 中至少一项、不含 `text`——用来在别名段落之外
      补充"这些已确认的称谓在这一章还出现在哪"，帮模型判断两者是否指同一人。

    为什么要搜整个 `anchor_texts` 而不是只搜角色规范名：真实回归——李富贵的别名
    "胖爷"在桥接章里的连接证据是"（胖爷）……此刻小胖子正蹲在那里"，这段原文压根
    没提"李富贵"三个字，"小胖子"（李富贵已确认的另一条别名）才是真正的桥梁。只搜
    角色规范名会把这段关键证据漏掉，模型看不到任何连接就只能回答 uncertain——与
    `_alias_declaration_verified` 条件 3 的共现闸本就允许"该章节找到角色规范名或
    已确认的其它别名"任一项是同一个道理，裁决闸的证据检索范围不能比共现闸更窄。

    真实回归暴露的另一个坑：`anchor_texts` 里若含主角规范名，几乎每段都会出现，
    如果只按"从章节开头数第几段"这种文档顺序截断，预算会被开头大段无关的独白占满，
    反而把本该收录的 `text` 段落、以及紧挨着 `text` 段落的关键 anchor_only 段落
    挤出预算之外（project proj_3ac0b627fa46 第 1 章："孟兄"只出现一次，"孟浩"出现
    三十余次贯穿全章——若不做优先级区分，裁决闸看到的会是章节开头孟浩独自坐在山顶
    的大段背景描写，反而看不到"孟兄"那句台词紧邻的对话）。排序规则的锚点顺序不变：
    both 优先 → text_only 次之（这两类条数通常很少，一般不会触顶）→ anchor_only
    按"离最近的 both/text_only 段落有多远"升序排列，越靠近别名实际出现的位置越
    优先，距离相同按文档顺序（下标升序）确定性打破平局。

    第二个真实回归（"主角淹没预算"第四次复发——本项目此前已在 prep_pack.py 里
    修过三次同类问题：卷宗整体、候选判别 B 侧内部、B 侧稀缺槽位，见该文件
    0395a73/1f15844 两次提交的完整说明）：`text`（别名场景下的别名本身；状态
    事实场景下调用方传入的是归属对象/关系对象，见 `_status_fact_evidence_
    resolution`）若恰好是章内高频词（结构上与主角名同样"近乎每段都出现"，
    例如某个宗门名反复被提及），text_only 段落数量可能远超 both、也远超
    anchor_only。旧实现"both 全部收录 → text_only 全部收录 → anchor_only 补足
    剩余预算"里，"全部收录"没有上限——text_only 可以在任何 anchor_only 段落被
    考虑之前，独自把 MAX_ENTRIES/MAX_CHARS 吃光。anchor_only 段落正是"这些已
    确认称谓在这一章还出现在哪"的关键证据，一旦被整体挤出卷宗，模型只能在残缺
    材料上判断候选是否与别名指代同一人。

    修复：移植 prep_pack.py 的按层保底配额方案（0395a73）并叠加"保底不受字数
    预算挤占"（1f15844）——both/text_only/anchor_only 三层各自先分到
    `min(_ALIAS_VERDICT_DOSSIER_MIN_LAYER_ENTRIES, 该层实际可用条数)` 的保底
    名额，谁都不能被其它层挤到 0；保底层的段落一律直接收录，不因为字数预算
    不够被跳过（1f15844 的核心教训：条数保底如果仍受字数预算约束，排在前面的
    长段落照样能把后面保底段的字数额度吃光，保底"有位置"不等于"进得去卷宗"），
    单段超过 `_ALIAS_VERDICT_DOSSIER_GUARANTEED_ENTRY_MAX_CHARS` 时用
    `_alias_verdict_dossier_truncate_segment` 做确定性截断（保留锚点词所在的
    核心句 + 省略标记），不整段丢弃。三层保底收录完毕后，剩余的"flex"名额才按
    both -> text_only -> anchor_only 既有优先级顺序（anchor_only 仍按上面的
    邻近度排序）继续分配，这部分维持原有语义——只用字数预算约束、不截断（缺了
    不影响"每层至少有保底代表"这个硬要求）。两个既有上限常量（MAX_ENTRIES/
    MAX_CHARS）原样不变，只是同一份预算内部的分配规则更细颗粒度，不是靠放大
    上限绕过问题。

    条数与总字数超过上限后按上述顺序截断，不用随机采样，同一输入任何时候重跑
    都得到同一份卷宗。调用方已确认 `text` 在 `chapter_text` 里，理论上 both/
    text_only 至少有一条命中；真的一条都没有（分段边界极端情况）就返回空列表，
    交由调用方兜底拒绝。"""
    segments = index_source_segments(chapter_text)
    both_indexes: list[int] = []
    text_only_indexes: list[int] = []
    anchor_only_indexes: list[int] = []
    anchor_only_matched_anchor: dict[int, str] = {}
    for index, seg in enumerate(segments):
        has_text = text in seg.text
        matched_anchor = next(
            (anchor for anchor in anchor_texts if anchor and anchor in seg.text), None,
        )
        if has_text and matched_anchor is not None:
            both_indexes.append(index)
        elif has_text:
            text_only_indexes.append(index)
        elif matched_anchor is not None:
            anchor_only_indexes.append(index)
            anchor_only_matched_anchor[index] = matched_anchor
    if not both_indexes and not text_only_indexes:
        return []
    priority_indexes = both_indexes + text_only_indexes
    anchor_only_by_proximity = sorted(
        anchor_only_indexes,
        key=lambda index: (min(abs(index - anchor) for anchor in priority_indexes), index),
    )

    # 按层保底配额 + 保底段免字数预算挤占（移植自 prep_pack.py 0395a73/1f15844，
    # 见本函数 docstring"第二个真实回归"一节）：三层各自先分到不超过自身可用
    # 条数、也不超过 MIN_LAYER_ENTRIES 的保底名额；保底之后剩余的名额（flex）
    # 仍按既有优先级顺序竞争分配。
    reserve_both = min(_ALIAS_VERDICT_DOSSIER_MIN_LAYER_ENTRIES, len(both_indexes))
    reserve_text_only = min(_ALIAS_VERDICT_DOSSIER_MIN_LAYER_ENTRIES, len(text_only_indexes))
    reserve_anchor_only = min(
        _ALIAS_VERDICT_DOSSIER_MIN_LAYER_ENTRIES, len(anchor_only_by_proximity),
    )
    guaranteed_both, overflow_both = both_indexes[:reserve_both], both_indexes[reserve_both:]
    guaranteed_text_only, overflow_text_only = (
        text_only_indexes[:reserve_text_only], text_only_indexes[reserve_text_only:]
    )
    guaranteed_anchor_only, overflow_anchor_only = (
        anchor_only_by_proximity[:reserve_anchor_only],
        anchor_only_by_proximity[reserve_anchor_only:],
    )

    selected: list[int] = []
    used_chars = 0
    resolved_text: dict[int, str] = {}
    # 保底层：both/text_only 以 `text` 本身为截断锚点（has_text 恒为真）；
    # anchor_only 以命中该段的具体 anchor 字面串为截断锚点。一律直接收录，
    # 不做字数预算判断——这正是 1f15844 相对 0395a73 的核心差异。
    for index in guaranteed_both + guaranteed_text_only:
        if len(selected) >= _ALIAS_VERDICT_DOSSIER_MAX_ENTRIES:
            break
        piece = _alias_verdict_dossier_truncate_segment(segments[index].text, text)
        selected.append(index)
        used_chars += len(piece)
        resolved_text[index] = piece
    for index in guaranteed_anchor_only:
        if len(selected) >= _ALIAS_VERDICT_DOSSIER_MAX_ENTRIES:
            break
        anchor_word = anchor_only_matched_anchor.get(index, "")
        piece = _alias_verdict_dossier_truncate_segment(segments[index].text, anchor_word)
        selected.append(index)
        used_chars += len(piece)
        resolved_text[index] = piece
    # flex 层：维持原有语义，按 both -> text_only -> anchor_only 优先级顺序，
    # 仍受字数预算约束（缺了不影响"每层至少有保底代表"这个硬要求）。
    for index in overflow_both + overflow_text_only + overflow_anchor_only:
        if len(selected) >= _ALIAS_VERDICT_DOSSIER_MAX_ENTRIES:
            break
        seg_text = segments[index].text
        if selected and used_chars + len(seg_text) > _ALIAS_VERDICT_DOSSIER_MAX_CHARS:
            continue
        selected.append(index)
        used_chars += len(seg_text)
    selected.sort()
    return [
        {
            "chapter_idx": chapter_idx, "segment_index": index + 1,
            "text": resolved_text.get(index, segments[index].text),
        }
        for index in selected
    ]


# 真实误登记事故 2：「王腾飞←王师弟」（第 189 章）——同一人工抽查发现的另一条误登记，
# 裁决闸两次都放行。原文里"看在王师弟的份上"这句话是血妖宗的李诗琪替另一个血妖宗
# 弟子王有材求情（王有材当章已经站到了孟浩一边），"王师弟"指王有材；王腾飞是同章
# 与孟浩敌对、正瞪着孟浩的另一个人，二者只是同姓。该章"王腾飞"出现 6 次、"王师弟"
# 只出现 1 次且恰好挨着王腾飞的戏份，卷宗按"离别名最近"的规则把王腾飞相关段落选进
# 去；根因不在卷宗检索，而在提问方式——"称谓 X 是否指代人名 Y 本人"是一道是非题，
# 模型看到卷宗里反复出现的是王腾飞，天然倾向对"是不是王腾飞"点头，这是确认偏误，
# 跟王腾飞与王有材同姓与否无关（换成任何两个同章出场、其中一个反复出现的角色都会
# 触发同样的偏误）。
#
# 修复：把裁决从"确认单一假设"改造成"从候选集中判别"。`selected_candidate` 取值
# 收紧为该章节里结构性命中的全部人物谱角色（角色规范名或其已确认别名的逐字子串命中，
# 见 `_alias_verdict_candidates`，零语义、不针对任何具体人名/姓氏特判）外加一个
# 显式的"都不是/无法确定"选项，schema 用 enum 同时限定候选集（与段号 enum 同一套
# 写法）。只有选中的候选恰好是本次申报的 `true_name` 才登记；选了候选集里的其他人、
# 选了"都不是/无法确定"、或候选集本身为空（不应该发生，防御性分支），一律拒绝。
# 这样"王师弟"这条会强迫模型在孟浩、王有材、李诗琪、王腾飞之间明确选一个并说出
# 理由，而不是回答一道"是不是"的确认题。


_ALIAS_VERDICT_NO_MATCH_LABEL = "都不是/无法确定"


def _alias_verdict_candidates(chapter_text: str, roster: dict[str, list[str]]) -> list[str]:
    """该章出现的全部人物谱候选人：结构判据，角色规范名或其任一已登记别名在章节原文
    里逐字子串命中即算该角色在这一章"出场"，零语义、不针对任何具体人名/姓氏做特判
    （见本节"真实误登记事故 2"）。`roster` 是调用方在本轮核验开始前对 `bible.characters`
    取的一次性快照（规范名 -> [规范名, 已登记别名...]），同一批核验内所有裁决调用
    共用同一份快照，不随本轮核验进度中途变化——避免同一批别名因处理顺序不同算出
    不同候选集，保证结构判据可复现。返回值按 `roster` 的登记顺序（即人物谱原始顺序）
    去重后的规范名列表；一个角色只要任一称谓命中就只计入一次，不按命中次数排序。"""
    return [
        name for name, surface_forms in roster.items()
        if any(form and form in chapter_text for form in surface_forms)
    ]


class _AliasVerdictResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # 候选判别（见本节"真实误登记事故 2"）：不再回答"是不是 true_name"的是非题，
    # 而是在候选集（该章出场的全部人物谱角色 + "都不是/无法确定"）里选一个。
    # schema 层面用 enum 收紧到 `_alias_verdict_call` 构造的候选集（与段号 enum
    # 同一写法），代码层面只有选中值恰好等于 true_name 才登记。
    selected_candidate: str
    # 钉证判据（见本节顶部大注释）：模型只需引用卷宗目录里某一条的段号，不再要求
    # 逐字复述原文。schema 层面用 enum 把候选值限定为本次卷宗实际收录的段号集合
    # （见 `_alias_verdict_call` 里对 `output_schema` 的 enum 注入），代码层面
    # `_alias_verdict_pin_segment` 再做一次结构性核验——两层防线都不依赖模型转录
    # 原文是否精确。
    supporting_segment_index: int
    # 可选的观测字段：模型仍可以给出一句引文供人工复核参考，但不再逐字比对、也不
    # 作为通过与否的判据（真实回归：this 字段之前叫钉证硬闸，"李富贵←小胖子"
    # "上官修←上官师叔"两条正确别名分别被误杀 / 同一输入两次结果不一致，根因是
    # 逐字复述本身脆弱，见本节顶部大注释）。
    supporting_quote: str = ""


class _AliasExclusivityVerdictResponse(_AliasVerdictResponse):
    """别名裁决专用响应：`_AliasVerdictResponse` 加一个排他性判据字段，只给
    `_alias_verdict_call`（别名场景）用——`_status_fact_verdict_call`（状态事实
    场景，见该函数）继续用不含这个字段的基类 `_AliasVerdictResponse`，两条路径各自
    的 schema 只包含各自 prompt 真正问过的字段。

    背景（子类拆分的直接原因）：这个字段最初直接加在 `_AliasVerdictResponse` 上，
    但该类被 `_status_fact_verdict_call` 共用同一份 `model_json_schema()` 下发——
    状态事实那条路径的 prompt 从未解释过这个字段是什么，模型却被结构性要求为一个
    没人问过的问题编一个布尔值，这正是 CLAUDE.md 记录过的根因类型（"模型答不出来
    时，先查它有没有收到标准答案"）：白烧 token、在无关字段上分散模型注意力、给
    未来埋"读到就是掷硬币"的陷阱。拆分后 `_AliasVerdictResponse` 恢复原样，
    `_status_fact_verdict_call` 零改动。

    排他性判据本身（真实事故：EP2「少年」、EP3/EP10「大汉」误登记为身份凭证；跨
    项目复现见 ERR-20260828-9fcabe，「大夫」被登记成主角马骥的别名）：与
    selected_candidate 结构上不同的问题——selected_candidate 判的是"这句话里的
    称谓具体指谁"（本段语境内的指代对象），is_exclusive_reference 判的是"这个
    称谓字面本身"脱离这句话、脱离这一段语境，能不能单独把这个人和任何符合同一类
    特征的陌生人区分开。必填（无默认值）：漏答即 Pydantic ValidationError，不允许
    模型对这道判据保持沉默。"""

    is_exclusive_reference: bool


async def _alias_verdict_call(
    *, alias: str, true_name: str, dossier: list[dict[str, Any]],
    candidates: list[str], project_id: str | None,
    cognition_card: ChapterCognitionCard | None = None,
) -> _AliasExclusivityVerdictResponse:
    """裁决：唯一一次独立模型调用，只给卷宗原文与候选人名单，不点名"你猜是不是
    true_name"——把"这称谓到底指代候选里的哪一位"这个判别完全交给模型自己独立
    做出，答案落在候选集之外（含"都不是/无法确定"）一律视为没有确认申报的假设，
    与 `_prep_pack_true_name_verdict` 同一范式（先给独立卷宗，再让模型做判断，
    不预设结论）。`candidates` 由调用方 `_alias_verdict_candidates` 结构性算出，
    保证包含 `true_name` 本人（该章一定命中，见 `_alias_evidence_resolution` 对
    候选集为空的防御性拒绝分支的说明）。

    `cognition_card`（可选，见 docs/CHARACTER_COGNITION_LAYER_DESIGN.md §4.3）：认知层
    章级认知卡，附带每个候选"截至本章"的归属/关系背景摘要。这是 §1.3 指出的缺口的
    直接修复——裁决闸原先只能看"这一章本身"的原文，现在额外看到候选人跨章建立的
    状态事实。注入的文本块与卷宗原文段落明确分区、分别标注，并显式声明"判定仍须
    基于原文段落，认知卡只能辅助区分候选，不得仅凭认知卡下结论"（§4.3 防幻觉纪律：
    属性错了比没有更糟），不放松段号钉证、候选 enum、"都不是/无法确定"即拒绝等既有
    闸门。`cognition_card` 为 `None`，或其中没有任何候选带归属/关系摘要（当前真实
    状态：状态事实回填尚未真实跑过，`affiliations`/`relations` 均为空）时，
    `_cognition_status_lines` 返回空列表，下面拼出的 `cognition_section` 为空字符串，
    prompt 与本次改造前逐字一致——不留空标题、不留占位噪声。"""
    catalog = "\n\n".join(
        f"[第{item['chapter_idx']}章·段{item['segment_index']}] {item['text']}"
        for item in dossier
    )
    segment_indexes = [item["segment_index"] for item in dossier]
    candidate_options = [*candidates, _ALIAS_VERDICT_NO_MATCH_LABEL]
    candidate_list = "、".join(candidates)
    cognition_lines = _cognition_status_lines(cognition_card)
    cognition_section = ""
    if cognition_lines:
        cognition_section = (
            "候选人已知状态（认知卡背景参考，来自结构化的归属/关系历史证据，不是本次"
            "卷宗原文）：\n" + "\n".join(cognition_lines) + "\n"
            "以上认知卡只用于辅助区分候选身份，本身不构成判定依据；下面才是本次判定"
            "必须依据的卷宗原文段落，若认知卡与原文段落冲突、或认知卡未提及，一律以"
            "原文段落为准，不得仅凭以上认知卡下结论。\n\n"
        )
    prompt = f"""{cognition_section}下面是原著第 {dossier[0]['chapter_idx']} 章中包含称谓"{alias}"的原文段落
（含前后语境，出现顺序不代表任何推断结论），每段前面标了段号：
{catalog}

该章出场的人物谱角色候选（判别范围仅限这些人，不要引入候选之外的人）：
{candidate_list}

任务一（选人）：仅依据以上原文段落本身，判断称谓"{alias}"最可能指代上面候选中的哪一位
本人。
- selected_candidate 必须从候选列表中选一个精确姓名，或者在证据不足以确定具体是谁时
  选"{_ALIAS_VERDICT_NO_MATCH_LABEL}"；不要因为某个候选在段落里出现次数多就倾向选他，
  只依据原文是否真的能确定"{alias}"说的就是他本人；
- supporting_segment_index 必须填上面某一段落标注的段号（取值只能是 {segment_indexes}
  之一），选你得出这个结论最主要依据的那一段，不要凭空填一个没在目录里出现的段号；
- supporting_quote 可选，若填写请给该段里的一句原文摘录供人工复核参考，不要求逐字
  精确，留空也可以。

任务二（判排他性，与任务一是两个不同的问题）：脱离本段语境单独考虑"{alias}"这个称谓
本身，判断它是由"类别词"单独构成，还是"类别词+修饰成分"、或者称谓本身就是专名/绰号。
类别词本身是对一类人共有特征的泛称（年龄段、性别、体型、身份、职业等），换成候选人
名单之外任何符合这些特征的人都能这样称呼，没有把任何具体的人从这一类里单独挑出来；
修饰成分是附加在类别词前后、把这个人从这一类人里单独标出来的额外信息：姓氏、本名、
本人绰号、这一处描述给出的穿戴/外貌/佩饰等具体细节（不要求这项细节是这个人一贯反复
出现的标志性特征——哪怕只在这一处描述里出现过一次，只要它不是这一类人人人都有的
泛泛描述，就算修饰成分）、排他性头衔或排行。只要能从"{alias}"里拆出至少一个这样的
修饰成分，整个称谓就是排他的，哪怕拆出之后仍剩下类别词本身；"{alias}"整体就是一个
专名/绰号（找不出类别词残留）也算排他。只有当"{alias}"整体就是一个不带任何修饰成分的
类别词本身时，才是非排他；如果一时无法判断某一部分算类别词还是修饰成分，就问它单独
拿出来是不是可以用来称呼很多不同的人——可以就是类别词，不太可能（这部分明显是针对
这一个人才会这样写）就是修饰成分。任务一问的是"这句话里的称谓具体指谁"（本段语境内
的指代对象——即使称谓本身是泛指，也可能在这句话的语境里确实指这个人）；任务二问的是
"这个称谓字面本身"的构成方式，选中了正确的人不代表这个称谓本身就是排他的，两个判断
互不预设对方的答案。
- is_exclusive_reference 填 true 表示能从"{alias}"里拆出至少一个修饰成分、或"{alias}"
  本身就是专名/绰号；填 false 表示"{alias}"整体就是一个不带修饰成分的类别词。
只输出符合 Schema 的 JSON。"""
    operation_id = "character_alias_backfill_verdict:" + hashlib.sha256(
        json.dumps(
            {
                "alias": alias, "true_name": true_name, "candidates": candidates,
                "dossier": [
                    (item["chapter_idx"], item["segment_index"]) for item in dossier
                ],
                "cognition": cognition_lines,
            },
            ensure_ascii=False, sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    # 用子类 `_AliasExclusivityVerdictResponse`（见其 docstring），不是基类
    # `_AliasVerdictResponse`——排他性字段只属于别名场景这条 prompt，状态事实那条
    # 路径（`_status_fact_verdict_call`）共用的是没有这个字段的基类，两条路径各自
    # 的 schema 只包含各自 prompt 真正问过的字段。
    schema = _AliasExclusivityVerdictResponse.model_json_schema()
    # 参照 app/portraits.py `_current_identity_schema()` 给 evidence_ref 注入 enum
    # 的写法：把候选段号、候选人名单都收紧到本次实际可用的集合，模型在协议层面就
    # 选不出卷宗外的段号或候选集之外的人；真正生效的核验仍在
    # `_alias_verdict_pin_segment` 与 `_alias_evidence_resolution` 里做代码侧结构
    # 校验（provider 对 enum 的遵守不是可证明保证，见这两处调用点的说明）。
    schema["properties"]["supporting_segment_index"]["enum"] = segment_indexes
    schema["properties"]["selected_candidate"]["enum"] = candidate_options
    return await model_gateway.chat_structured(
        [{"role": "user", "content": prompt}],
        model_type=_AliasExclusivityVerdictResponse,
        validate=None,
        operation_id=operation_id,
        max_tokens=500,
        # 低温：这道闸的语义判断要稳定——同一份卷宗重跑不该一次选中一次不确定。
        # 真实回归：0.2 时同一批别名重跑三次，多条会在 same/uncertain 之间摇摆；
        # 降到 0 后结论稳定下来（钉证已改为选段号/选候选人，不再依赖模型逐字复述
        # 原文，温度对钉证成功率不再有直接影响，但仍保留低温以稳定判别结论本身）。
        temperature=0.0,
        format_retry_limit=1,
        semantic_retry_limit=1,
        output_schema=schema,
        call_meta=_bible_short_json_call_meta({
            "stage": "别名回填桥接章裁决",
            "stage_key": "character_alias_backfill_verdict",
            "call_role": "stage_generate",
            "call_role_label": "别名桥接章裁决",
            "expected_json": True,
            "project_id": project_id,
            "alias": alias,
            "true_name": true_name,
            "candidates": candidates,
        }),
    )


def _alias_verdict_pin_segment(
    dossier: list[dict[str, Any]], segment_index: Any,
) -> dict[str, Any] | None:
    """钉证：结构性校验，不再要求模型逐字复述原文（原 `_alias_verdict_pin_quote` 的
    做法，见本节顶部大注释）。模型只需要在响应里选一个段号，这里核对该段号是否落在
    本次卷宗实际收录的段号集合内——命中即视为钉证通过，因为卷宗内容本身就是代码
    检索出的真实原文，模型选中某一条不存在"编造"或"转录出错"的空间，只可能选中
    （合法）或选错/瞎编（非法）。非法输入（不是整数、或不在集合内）一律返回 None，
    交由调用方按无效裁决拒绝——不确定不登记的安全默认在这里同样成立：宁可拒绝一个
    只是格式不对的合法裁决，也不放宽到"看起来像是"就算数。命中返回该条卷宗记录
    （自带 chapter_idx，供调用方记账）。"""
    try:
        target = int(segment_index)
    except (TypeError, ValueError):
        return None
    for item in dossier:
        if item["segment_index"] == target:
            return item
    return None
