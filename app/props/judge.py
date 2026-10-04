"""关键道具判据 + 外观锚点生成（模型申报 + 代码核验，不用道具名/关键词黑白名单）。

只登记"关键道具"，不是每一次提及都建库：一次性出现的背景物件（用户举例：路过
桌上的一只杯子）建库只会浪费出图成本、稀释真正需要跨集稳定的道具。判据从数据
推导，四选一（结构信号，零语义，不针对任何具体道具名做特判）：
  a) mention.segment_indexes 去重后覆盖 ≥2 个原文段——跨段落反复出现，说明
     不是一次性入镜；
  b) description 按中文顿号/逗号/分号/空白切分后，非空子句数 ≥3——结构上
     等价于"对材质/颜色/结构/尺寸/标志物做了多维度描述"（契约要求
     appearance_canonical 本身就是三项以上可视觉验证特征），不检查具体是
     哪些词、只数分句密度；一次性路过的背景物件描述往往只有一个笼统短语，
     分句数量天然达不到这个密度。
  c) 道具在本集原文里被反复提到：label 本身、或 label 的「中心词」（≥2 字的最长后缀，
     中文名词短语是修饰语 + 中心词的结构，「旧猫包」的中心词是「猫包」）在原文里出现
     ≥2 次——真实投诉的「旧猫包」在 EP1 原文里只占一个原文段、描述只有一句，但「猫包」
     出现 5 次，正是跨段被反复拍到、最容易漂移的那类道具。
  d) 模型提名 + 代码核验（2026-09-28 新增，真实缺陷：《顾念长安》第1集贴身佩戴的
     黄铜旧星盘、只出现一次的童年合影都漏建卡——它们是剧情伏笔/交接物，不是背景
     陈设，但只在原文里出现一两次、描述也往往只有一句，前三条结构信号天然覆盖
     不到）：mention.plot_significant=true 且 plot_significant_quote 逐字命中
     source_text——这不是"模型说重要就信"，是模型必须交出可核验的原文证据（这件
     物品在剧情里被拿起/交接/特写/作为伏笔反复强调），代码只核验证据是否真实存在，
     不判断"重要"这件事本身该不该成立。证据编造（quote 在原文里查无实据）一律
     不采信，不走这一条。
四条都不满足时不发起模型调用（省成本），标签维持"只有 label+description 文字
描述"的原状，与此前完全一致——不是退化，是本来就不该入库。

``PROP_CARD_RULES_VERSION``（2026-10-03 新增）：道具卡外观/别名规则的版本号。
规则改了就改这个字符串，``app.props.card_audit`` 据此判断存量卡是否需要按
新规则复核（见该模块 docstring）。``PROP_APPEARANCE_OWN_RULE_TEXT`` 是
"外观只写物件自身"规则的正面陈述，``assess_prop_appearance``（新建卡）与
``app.props.card_audit_rules``（存量卡复核）共用同一份文本，避免两处规则
表述漂移（CLAUDE.md「模型契约两侧必须对齐」的同一精神）。``2026-10-04-v4``：
owner 归属证据加了"卡名/别名逐字出现在子句原文里"这条数据推导路径（见
``app.props.card_audit_cooccurrence`` 模块 docstring）。``2026-10-04-v5``：
``split_appearance_clauses`` 改回只按句子边界（逗号/分号/句号/问号/感叹号/
换行）切分，不再按顿号「、」切、也不再做"否定词开头 + 下一个顿号单元"的
特判合并（见 ``_clause_spans``）——第 4 轮沙箱实测否定合并只认"否定词在句首"
这一种写法，"表面无印花、刺绣等额外装饰"这类否定词不在句首的真实写法识别
不到（4 张待救回卡只救回 1 张）；根因是顿号连接的本来就是同一句里的并列项，
不该在切分这一步被当成子句边界，改成只按句子边界切分后天然不再有这个问题，
子句粒度变粗但不丢信息——混合了"该留"与"该删"内容的粗粒度子句仍可通过
``keep_fragment`` 机制保留需要留下的逐字片段。
"""
from __future__ import annotations

import hashlib
import re

from pydantic import BaseModel, ConfigDict, Field

from app.harness import model_gateway

#: 新建卡"至少 3 项特征"的计数口径（``description_feature_count`` 专用）。
#: 与下面 ``_CLAUSE_TOKEN_RE``（复核子句切分）是两套故意不同用途的口径，
#: 2026-10-04-v5 起不再要求两者一致，各自管各自的事，见
#: ``split_appearance_clauses`` docstring。
_CLAUSE_SPLIT_RE = re.compile(r"[、，,;；\s]+")
#: 复核子句切分边界：中文/英文逗号、分号、中文句号、问号、感叹号、换行——不含
#: 顿号「、」、不含普通空格（2026-10-04-v5）。顿号连接的是同一句里的并列项，
#: 本就不该在这一步被当成子句边界，见 ``_clause_spans``。英文句点「.」也不算
#: 边界：外观里它是小数点（「6.7英寸」「直径约2.5cm」），切开会把数字拆成两条。
_CLAUSE_TOKEN_RE = re.compile(r"[^，,；;。！!？?\n]+")
MIN_SEGMENT_COUNT = 2
MIN_DESCRIPTION_CLAUSES = 3
MIN_APPEARANCE_FEATURES = 3
MIN_SOURCE_OCCURRENCES = 2
MIN_HEAD_NOUN_CHARS = 2
PROP_CARD_RULES_VERSION = "2026-10-04-v5"

PROP_APPEARANCE_OWN_RULE_TEXT = (
    "appearance_canonical 只写「这件道具单独摆出来、周围没有别的东西时，它自己身上\n"
    "看得到」的材质、颜色、结构、版型、以及它表面本来就有的图案花纹。描述里提到的\n"
    "其它物件——藏在它里面的、挂在它上面的、放在它旁边的、被它包住或遮住的东西——\n"
    "如果脱离这件道具之后自己还能单独搬走、单独存在、有独立的呈现意义，都属于那件\n"
    "别的物件，它自己有自己的道具卡或分镜画面去表现，不写进这件道具的\n"
    "appearance_canonical。人物身体、人物动作、或别的物件在这件道具上面留下的印痕（按压出的\n"
    "凹痕、蹭上的痕迹、沾染的印记等）——判断标准不是这处痕迹本身能不能被单独搬走\n"
    "（痕迹本来就搬不走，这个测试对痕迹无效），而是能不能指认出具体是哪一件别的\n"
    "物件、或哪一次人物身体/动作造成的：能指认出具体来源的，不论这处痕迹是长期\n"
    "形成的（比如长期贴身佩戴的另一件东西压出的印子）还是一次性造成的（比如一次\n"
    "蹭上的痕迹），都优先归那件别的物件或当时的动作/剧情本身，不写进这件道具的\n"
    "appearance_canonical，时长不改变这个判定；指认不出任何具体来源、纯粹是这件\n"
    "物件自己长期使用形成的旧化/包浆/磨损，才算它自己的固有外观（见下一条判据）。\n"
    "和这件道具连在一起随它一起移动、脱离这件道具自己没有独立呈现意义的配件（比如\n"
    "穿在它上面的绳、盖子、装它的表袋、挂牌），这些即使没有自己的道具卡，也要写进\n"
    "这件道具的 appearance_canonical，算这件道具在故事里呈现的一部分，不算「别的\n"
    "物件」。"
)

PROP_APPEARANCE_PLOT_STATE_RULE_TEXT = (
    "appearance_canonical 也不写剧情事件造成的时点状态——这件道具只是「在某一次\n"
    "事件发生之后」才变成这个样子，事件发生前它并不是这样（真实案例：被雨水泡过的\n"
    "纸箱「塌成一团、纸板发软」，泡水后发蔫的绿萝「叶片发黑、枝条软垂」，被水泡过的\n"
    "行李箱「箱体留有水渍干涸的印记」，长安地图上人物事后用朱红色笔画上去的标注线，\n"
    "手机只在某段剧情里亮屏显示的消息/地图界面内容——这些都只在事件发生后（或只在\n"
    "那一段剧情里）成立，事件之前/那段剧情之外道具是原来的样子，写进外观锚点会让\n"
    "道具提前或永久"
    + "“" + "变脸" + "”"
    + "）。这类状态归分镜正文描述，不归道具卡；手机/屏幕一类道具，熄屏时的外壳样子\n"
    "仍是固有外观，要保留，只有亮屏后界面显示的具体内容才按这条删。"
)

#: 2026-10-03 新增、2026-10-03-v2 改写为两轴判据、2026-10-03-v3 再加一条"优先级"
#: 前提（本次改写原因：v2 两轴各自独立判断时仍会互相打架——真实案例"浅灰色卫衣"
#: 胸前被"长期贴身佩戴的旧星盘"压出的印子，模型卡在"是它身上的、搬不走"（像满足①）
#: 与"不是剧情事件之后才有，长期放置不算事件"（像满足②）之间，判成了固有外观；
#: 根因是①的"能不能搬走"测试对"痕迹"本身从来不成立——痕迹当然搬不走，这个测试
#: 该问的是"痕迹"而不是"造成痕迹的东西"。v3 加一条前提明确优先级：能指认出具体
#: 来源的痕迹，不论长期还是一次性，一律先按"别的物件/动作痕迹"处理，不进入下面
#: 两条由"搬不走"/"是不是事件"互相打架：
PROP_APPEARANCE_INHERENT_FEATURE_RULE_TEXT = (
    "固有外观必须同时满足两条，缺一都不算；两条之间有优先级，不能互相打架——只要\n"
    "一处痕迹/样子能指认出具体来源（另一件东西压的、蹭的、沾染的，或人物身体/动作\n"
    "造成的），就优先按来源判定为「别的物件/动作痕迹」（上一条规则管），不论这处\n"
    "痕迹是长期形成的还是一次性造成的，时长不影响这个优先判定，不进入下面两条：\n"
    "① 指认不出任何具体来源、纯粹是这件物件自己使用/放置过程中逐渐形成的，才算它\n"
    "身上的东西；脱离这件道具后自己还能单独搬走、单独存在、有独立呈现意义的另一件\n"
    "东西（放在旁边的配饰），算「别的物件」，按上一条规则处理；和这件道具连在一起\n"
    "随它一起移动、脱离它没有独立呈现意义的配件（绳、盖子、表袋、挂牌），即使搬\n"
    "得走，也算这件道具呈现的一部分，不算「别的物件」；\n"
    "② 它不是某个剧情事件之后才出现或改变的——事后画上去的标注、只在某一段剧情里\n"
    "才会显示的屏幕/界面画面内容、泡水/摔坏/弄脏之后才有的样子，这些即使指认不出\n"
    "具体来源，也归剧情时点状态（上一条规则管），不算固有外观。\n"
    "两条都满足才保留：物件表面本来就有的图案花纹、印刷/刻写/内置在它身上的图文\n"
    "信息（比如玉简内置的地图纹路、一张照片本身印着的合影画面）、出厂就有的设计、\n"
    "长期使用形成的旧化/包浆/磨损（表面因摩挲发亮、边角因摩擦发白、常年使用留下的\n"
    "浅划痕——这些是物件自己经年使用造成的，指认不出是哪一件具体的别的东西压/蹭/\n"
    "染出来的）——这些既指认不出单独来源，也不是哪次剧情事件之后才有，一直都在，\n"
    "要保留。"
)

PROP_ALIAS_OWN_RULE_TEXT = (
    "别名必须单独报出来就能准确指向这一件道具本身，不能是脱离上下文也能指向场景里\n"
    "任何同类物件的说法——只剩下品类名的泛称（比如单独一个\"椅子\"\"杯子\"\"毛衫\"\n"
    "\"鞋子\"）本身不是别名，因为同一场景或别的场景里随时可能出现另一件同品类的\n"
    "东西，把泛称登记成别名会让那件不相关的东西错误复用这张卡的参考图；只有能排除\n"
    "歧义的具体说法才算这件道具的别名——判断标准不是"
    + "“" + "有没有加修饰词" + "”" +
    "，而是\n"
    "这个说法单独报出来，在同一项目、同一类常见场景里，是否仍有很大概率指向别的\n"
    "同品类东西：材质/颜色这类修饰词如果本身就很常见（比如\"木质椅子\"\"矮鞋柜\"——\n"
    "木质的椅子、矮的鞋柜现实场景里随处可见），同样没有排除歧义，不能算别名；只有\n"
    "数量/归属/这件道具独有的具体细节（比如\"主角房间那张断了一只脚的木椅子\"）才\n"
    "真正排除了歧义，才算这件道具的别名。"
)


def _clause_spans(stripped: str) -> list[tuple[int, int]]:
    """``split_appearance_clauses``/``rebuild_appearance_excluding`` 共用的
    下标口径（2026-10-04-v5 改名自 ``_merged_clause_spans``：此前"先按标点
    切分、再把否定词开头的相邻单元合并成一条"的两步走已经删除，现在只按
    句子边界切一遍，不需要再合并）。两个函数必须共用同一套 spans，否则
    下标会错位。

    真实缺陷（2026-10-03 沙箱实测 123 张卡命中 3 次；改成"按顿号切 + 否定词
    开头特判合并"的上一版修法后，第 4 轮实测 4 张待救回卡仍只救回 1 张）：
    "无印花、涂鸦等额外装饰"这类共享否定的并列短语，顿号连接的本来就是
    同一句里的并列项，不该在切分这一步被当成子句边界；上一版的"否定词开头 +
    下一个顿号单元合并"特判只认得否定词落在子句开头这一种写法，"表面无
    印花、刺绣等额外装饰""衣身无印花、刺绣等额外装饰"这类否定词不在句首的
    真实写法识别不到，依旧会被切成两条独立子句，依旧有"只删后半句、留下
    前半句"从而让字面意思反转的半删风险。根治办法是不再按顿号切分：改成只
    按句子边界（逗号/分号/句号/问号/感叹号/换行）切分后，顿号天然不再是
    边界，"无印花、刺绣等额外装饰"天然就是一条完整子句，不需要再识别任何
    否定词模式去补救。子句粒度因此变粗，但 ``rebuild_appearance_excluding``
    的 ``keep_fragment`` 机制（保留原句逐字连续片段）足以让模型在一条粗
    粒度子句里只保留需要留下的那一小段，不会丢信息（CLAUDE.md「退场要一次
    删干净」：否定词特判的整套逻辑和常量都已删除，不留历史包袱）。"""
    return [(m.start(), m.end()) for m in _CLAUSE_TOKEN_RE.finditer(stripped)]


def split_appearance_clauses(text: str) -> list[str]:
    """把 ``appearance_canonical`` 按句子边界（逗号/分号/句号/问号/感叹号/
    换行，不含顿号、不含普通空格，见 ``_CLAUSE_TOKEN_RE``）切成编号子句，供
    ``app.props.card_audit`` 的子句复核使用。

    与 ``description_feature_count``（新建卡"至少 3 项特征"的计数口径、也供
    ``app.props.card_audit_compute`` 判 ``feature_shortfall`` 复用，按
    顿号/逗号/分号/空白切）是两套故意不同的口径（2026-10-04-v5 起不再要求
    一致）：前者要给模型一条完整的、不被顿号腰斩的句子去判定删留，粒度必须
    粗到不破坏"无印花、刺绣等额外装饰"这类并列否定短语；后者只是数"结构
    信号密度"来判断一条描述够不够格（建卡/复核后是否特征不足），密度判据
    天然需要更细的切分粒度才能反映"多维度描述"。两者各管各的，不是
    CLAUDE.md「模型契约两侧必须对齐」要求对齐的同一件事——那条约束管的是
    "模型 schema 允许的取值"与"业务校验接受的取值"两侧不能一宽一严，不要求
    同一模块内任意两个用途不同的计数口径都相同。
    """
    stripped = (text or "").strip()
    return [stripped[s:e] for s, e in _clause_spans(stripped)]


def rebuild_appearance_excluding(
    text: str, removed_indexes: set[int], keep_fragments: dict[int, str] | None = None,
) -> str:
    """按原文顺序删除 ``removed_indexes``（1-indexed，对应
    ``split_appearance_clauses`` 的下标——两者必须共用 ``_clause_spans``，
    否则下标错位）对应的子句后重新拼接；``keep_fragments`` 里出现
    的下标（必须是 ``removed_indexes`` 的子集，由调用方
    ``app.props.card_audit_consensus`` 核验过「逐字连续子串、不等于整句、
    两次独立判定给出的片段完全一致」才会出现在这里）不整句删除，改成只保留
    对应的那段片段文字，其余部分仍按原顺序删除——2026-10-03-v3 新增，真实
    案例："绿萝"子句"原生心形翠绿色叶片约三分之二边缘发黑发蔫"整句删除后，
    "心形翠绿色叶片"这条植物固有外观信息跟着丢了，但只保留"约三分之二边缘
    发黑发蔫"又该删（剧情时点状态）；允许模型对这类混合子句给出要保留的
    片段，代替"整句删"或"整句留"的二元选择。

    不新增、不改写任何字符：保留的子句（或其片段）之间用原文里紧跟在前一个
    子句后面的那段分隔符本身拼接（无论它后面原来跟的子句是否被删），输出的
    每一个字符都来自原字符串的某个位置——片段本身也是对应子句原文的子串，
    同样满足这条约束（子句本身含有的顿号等内部符号同样是原文连续片段的
    一部分，依旧是子串拼接）——这是 CLAUDE.md「代码核验」对本功能的硬要求：
    模型只负责判定删哪些子句/保留哪段片段，新外观必须是原文本身的子串拼接，
    不能让模型顺带改写或让代码自己发明新的分隔符。

    末尾分隔符（2026-10-04-v5 审查发现并修复，真实数据验证暴露：B 上 123 张
    卡实测，123 张里 53 张"什么都不删"时结尾的句末标点「。」会被静默吞掉，
    43% 的卡一复核就丢字）：句子边界切分后，句末标点（比如收尾的"。"）落在
    最后一条子句的 span 之外，此前的循环只在"当前子句不是本次输出的最后一项"
    时才拼接它后面的分隔符，这对"删除尾部子句"的常见场景是对的（尾部子句被
    删后，前一条子句不该再带一个悬空的句末标点），但如果原文真正的最后一条
    子句本身被保留（覆盖"什么都没删"这个最常见的情况），它后面原本就有的
    句末标点会因为"它是本次输出的最后一项"而被同一条判断误伤。修复：只有
    当原文真正的最后一条子句确实被保留时，才把它后面直到原文末尾的那一段
    （句末标点、或者什么都没有）原样补回去——这段文字本身就是子串，不违反
    上面"不新增字符"的约束。
    """
    stripped = (text or "").strip()
    spans = _clause_spans(stripped)
    if not spans:
        return stripped
    keep_fragments = keep_fragments or {}
    fully_dropped = removed_indexes - set(keep_fragments)
    kept = [i for i in range(len(spans)) if (i + 1) not in fully_dropped]
    if not kept:
        return ""
    parts: list[str] = []
    for pos, i in enumerate(kept):
        start, end = spans[i]
        fragment = keep_fragments.get(i + 1)
        parts.append(fragment if fragment else stripped[start:end])
        if pos < len(kept) - 1:
            next_start = spans[i + 1][0] if i + 1 < len(spans) else end
            parts.append(stripped[end:next_start])
    if kept[-1] == len(spans) - 1:
        parts.append(stripped[spans[-1][1]:])
    return "".join(parts)


def description_feature_count(description: str) -> int:
    """按顿号/逗号/分号/空白切分后的非空子句数——新建卡「至少 3 项特征」
    的计数口径（见模块 docstring 判据 b），也供 ``app.props.card_audit_
    compute`` 判 ``feature_shortfall`` 复用同一口径（公开函数，2026-10-04-v5
    由 ``_description_clause_count`` 改名并跨模块导出）；与 ``split_
    appearance_clauses``（复核子句切分）是两套故意不同的口径，见该函数
    docstring。"""
    return len([part for part in _CLAUSE_SPLIT_RE.split(description.strip()) if part.strip()])


def source_occurrences(label: str, source_text: str) -> int:
    """label 或其中心词（≥2 字的最长后缀）在原文里的出现次数，取最大者。"""
    label = (label or "").strip()
    if not label or not source_text:
        return 0
    best = 0
    for start in range(0, len(label) - MIN_HEAD_NOUN_CHARS + 1):
        best = max(best, source_text.count(label[start:]))
    return best


def is_plot_significant_prop_mention(mention: dict, *, source_text: str) -> bool:
    """判据 d) 的独立核验：模型提名 + 代码核验，见模块 docstring。``source_text``
    为空时结构上不可能核验出任何一条 quote，直接返回 False（不是"宽松放过"，是
    "没有原文可核对，就不能采信"）。"""
    if not mention.get("plot_significant") or not source_text:
        return False
    quote = str(mention.get("plot_significant_quote") or "").strip()
    return bool(quote) and quote in source_text


def is_key_prop_mention(mention: dict, *, source_text: str = "") -> bool:
    """纯数据结构判据，不发模型调用；``source_text`` 为空时只看前两条。"""
    segment_indexes = {int(i) for i in mention.get("segment_indexes") or []}
    if len(segment_indexes) >= MIN_SEGMENT_COUNT:
        return True
    description = str(mention.get("description") or "")
    if description_feature_count(description) >= MIN_DESCRIPTION_CLAUSES:
        return True
    if source_occurrences(str(mention.get("label") or ""), source_text) >= MIN_SOURCE_OCCURRENCES:
        return True
    return is_plot_significant_prop_mention(mention, source_text=source_text)


class _PropAppearanceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    appearance_canonical: str
    aliases: list[str] = Field(default_factory=list)


async def assess_prop_appearance(
    label: str, description: str, *, style: str, ep_label: str,
) -> dict:
    """从 label+description 写出跨集稳定的道具锚点串（三项以上可视觉验证特征）
    与该道具在本集原文里可能出现的别称。返回 ``{"appearance_canonical", "aliases"}``；
    appearance_canonical 不满足最少特征数时兜底裁剪为 description 本身（不空转）。
    """
    prompt = f"""任务：为漫剧道具库写一条【规范外观锚点】，供后续跨集出图保持形态一致
（用户投诉根因：同一件道具在不同集画得不一样，因为此前没有素材库锚定）。

道具标签：{label}
本集描述：{description}
画风：{style}
所属集数：{ep_label}

要求：
- appearance_canonical 必须是至少 3 项可视觉验证特征的拼接（材质/颜色/结构/尺寸/
  标志物等，任选其中的具体项，不要求逐一覆盖这五类），30~120 字，只写可画出来的
  静态外观，不写动作/剧情。每一项特征都必须来自上面给出的「本集描述」，不得编造
  描述里没有提到的新特征。
- 「本集描述」里若出现多条互相不一致的描述（同一道具在不同镜头被写出不同细节），
  选取其中最连贯自洽的一版作为最终外观，不要把矛盾的细节硬拼在一起。
- {PROP_APPEARANCE_OWN_RULE_TEXT}
- {PROP_APPEARANCE_PLOT_STATE_RULE_TEXT}
- {PROP_APPEARANCE_INHERENT_FEATURE_RULE_TEXT}
- aliases 列出这件道具在描述中可能出现的其它称呼（无则给空数组），不得虚构。{PROP_ALIAS_OWN_RULE_TEXT}
输出 JSON：{{"appearance_canonical": str, "aliases": [str]}}"""
    response = await model_gateway.chat_structured(
        [{"role": "user", "content": prompt}],
        model_type=_PropAppearanceResponse,
        validate=None,
        operation_id="assess_prop_appearance:" + hashlib.sha256(
            f"{label}:{description}:{ep_label}".encode("utf-8")
        ).hexdigest(),
        temperature=0.2,
        max_tokens=500,
        call_meta={"stage": "assess_prop_appearance", "prop_label": label},
    )
    appearance = response.appearance_canonical.strip()
    if description_feature_count(appearance) < MIN_APPEARANCE_FEATURES:
        appearance = f"{appearance}。{description.strip()}" if appearance else description.strip()
    aliases = [a.strip() for a in response.aliases if a.strip() and a.strip() != label]
    return {"appearance_canonical": appearance, "aliases": aliases}
