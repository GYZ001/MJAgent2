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
表述漂移（CLAUDE.md「模型契约两侧必须对齐」的同一精神）。
"""
from __future__ import annotations

import hashlib
import re

from pydantic import BaseModel, ConfigDict, Field

from app.harness import model_gateway

_CLAUSE_SPLIT_RE = re.compile(r"[、，,;；\s]+")
_CLAUSE_TOKEN_RE = re.compile(r"[^、，,;；\s]+")
MIN_SEGMENT_COUNT = 2
MIN_DESCRIPTION_CLAUSES = 3
MIN_APPEARANCE_FEATURES = 3
MIN_SOURCE_OCCURRENCES = 2
MIN_HEAD_NOUN_CHARS = 2
PROP_CARD_RULES_VERSION = "2026-10-03-v2"

PROP_APPEARANCE_OWN_RULE_TEXT = (
    "appearance_canonical 只写「这件道具单独摆出来、周围没有别的东西时，它自己身上\n"
    "看得到」的材质、颜色、结构、版型、以及它表面本来就有的图案花纹。描述里提到的\n"
    "其它物件——藏在它里面的、挂在它上面的、放在它旁边的、被它包住或遮住的东西，\n"
    "以及人物动作在它上面留下的印痕（按压出的凹痕、蹭上的痕迹等）——如果脱离这件\n"
    "道具之后自己还能单独搬走、单独存在、有独立的呈现意义，都属于那件别的物件或\n"
    "当时的剧情本身，它们各自有自己的道具卡或分镜画面去表现，不写进这件道具的\n"
    "appearance_canonical；但如果是和这件道具连在一起随它一起移动、脱离这件道具\n"
    "自己没有独立呈现意义的配件（比如穿在它上面的绳、盖子、装它的表袋、挂牌），\n"
    "这些即使没有自己的道具卡，也要写进这件道具的 appearance_canonical，算这件\n"
    "道具在故事里呈现的一部分，不算「别的物件」。"
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

#: 2026-10-03 新增、2026-10-03-v2 改写为两轴判据（本次改写原因：原版只问"能不能
#: 被单独拿走"一个轴，漏判了"搬不走但是剧情事件之后才有"的内容——曾为了保住玉简
#: 内置地图纹路，把豁免写成"搬不走就保留"，结果把长安地图上事后画的标注线、手机
#: 只在特定剧情段才显示的亮屏内容也一并保住，这两处原本第一轮是判对的，第二轮反而
#: 判错。两轴同时满足才算固有外观，缺一不可：
PROP_APPEARANCE_INHERENT_FEATURE_RULE_TEXT = (
    "固有外观必须同时满足两条，缺一都不算：\n"
    "① 它是这件物件身上的东西，不是脱离这件道具之后自己还能单独搬走、单独存在、\n"
    "有独立呈现意义的另一件东西（放在旁边的配饰——这些算「别的物件」，要按上一条\n"
    "规则处理）；和这件道具连在一起随它一起移动、脱离它没有独立呈现意义的配件\n"
    "（绳、盖子、表袋、挂牌），即使搬得走，也算这件道具呈现的一部分，不算「别的\n"
    "物件」；\n"
    "② 它不是某个剧情事件之后才出现或改变的——事后画上去的标注、只在某一段剧情里\n"
    "才会显示的屏幕/界面画面内容、泡水/摔坏/弄脏之后才有的样子，这些即使搬不走，\n"
    "也归剧情时点状态（上一条规则管），不算固有外观。\n"
    "两条都满足才保留：物件表面本来就有的图案花纹、印刷/刻写/内置在它身上的图文\n"
    "信息（比如玉简内置的地图纹路、一张照片本身印着的合影画面）、出厂就有的设计、\n"
    "长期使用形成的旧化/包浆/磨损（表面因摩挲发亮、边角因摩擦发白、常年使用留下的\n"
    "浅划痕）——这些既搬不走，也不是哪次剧情事件之后才有，一直都在，要保留。"
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


def split_appearance_clauses(text: str) -> list[str]:
    """把 ``appearance_canonical`` 按与 ``_description_clause_count`` 相同的
    分隔口径切成编号子句（供 ``app.props.card_audit`` 的子句复核使用，两处
    必须共用同一套切分逻辑，不得另写一套——CLAUDE.md「模型契约两侧必须对齐」）。
    """
    return [m.group(0) for m in _CLAUSE_TOKEN_RE.finditer((text or "").strip())]


def rebuild_appearance_excluding(text: str, removed_indexes: set[int]) -> str:
    """按原文顺序删除 ``removed_indexes``（1-indexed，对应
    ``split_appearance_clauses`` 的下标）对应的子句后重新拼接。

    不新增、不改写任何字符：保留的子句之间用原文里紧跟在前一个子句后面的那段
    分隔符本身拼接（无论它后面原来跟的子句是否被删），输出的每一个字符都来自
    原字符串的某个位置——这是 CLAUDE.md「代码核验」对本功能的硬要求：模型只
    负责判定删哪些子句，新外观必须是原文本身的子串拼接，不能让模型顺带改写
    或让代码自己发明新的分隔符。
    """
    stripped = (text or "").strip()
    spans = [(m.start(), m.end()) for m in _CLAUSE_TOKEN_RE.finditer(stripped)]
    if not spans:
        return stripped
    kept = [i for i in range(len(spans)) if (i + 1) not in removed_indexes]
    if not kept:
        return ""
    parts: list[str] = []
    for pos, i in enumerate(kept):
        start, end = spans[i]
        parts.append(stripped[start:end])
        if pos < len(kept) - 1:
            next_start = spans[i + 1][0] if i + 1 < len(spans) else end
            parts.append(stripped[end:next_start])
    return "".join(parts)


_NEGATION_LEAD_RE = re.compile(r"^(无|没有|未见|不含|不带|没)")
_CLAUSE_HARD_BREAK_RE = re.compile(r"[，,;；]")


def _clause_trailing_separators(text: str) -> list[str]:
    """``split_appearance_clauses`` 每条子句后面紧跟的原始分隔符文本（最后一条
    为空串）——供 ``negation_linked_clause_groups`` 判断两条相邻子句之间是被
    「、」（顿号，并列结构内部）还是「，/,/;/；」（句子边界）隔开。"""
    stripped = (text or "").strip()
    spans = [(m.start(), m.end()) for m in _CLAUSE_TOKEN_RE.finditer(stripped)]
    seps: list[str] = []
    for i, (_start, end) in enumerate(spans):
        next_start = spans[i + 1][0] if i + 1 < len(spans) else len(stripped)
        seps.append(stripped[end:next_start])
    return seps


def negation_linked_clause_groups(text: str) -> list[frozenset[int]]:
    """识别"无A、B"这类共享否定的两条子句组合（1-indexed，对应
    ``split_appearance_clauses`` 的下标；每组恰好 2 条，否定子句本身 + 紧跟
    它的下一条）。

    真实缺陷（2026-10-03 沙箱实测 123 张卡命中 3 次，都恰好是 2 条子句的切分
    残留）：``split_appearance_clauses`` 按标点切分，不理解否定词的作用范围
    ——"无印花、涂鸦等额外装饰"会被切成「无印花」「涂鸦等额外装饰」两条独立
    子句；模型复核时若只删除后半句、保留前半句（或反过来），
    ``rebuild_appearance_excluding`` 原样拼接出的新外观会让字面意思反转（本来
    "没有涂鸦"，拼完变成"有涂鸦"）。这里识别出这类相邻子句对（前一条以否定词
    开头、与下一条之间只用「、」连接——没有被逗号/分号这类句子边界打断、下一条
    自己也不是新的否定起句），供 ``app.props.card_audit`` 在采信模型判定之前做
    "组内必须一致"的安全网：组内判定不一致时整组强制改判"不删"（CLAUDE.md
    「不要给以后的生成埋雷」——宁可少删几个字保留原状，也不让半删产生语义
    反转）。

    刻意只配对"恰好下一条"、不向后无限延伸：外观文本里顿号常被当成全篇统一的
    分隔符使用（见真实样本，整句材质/颜色/结构描述全靠顿号连接），如果否定
    组一路延伸到下一个句子边界为止，会把否定词之后、本来互不相关的大段后续
    描述全部卷入"只能整体保留"，反而放大了副作用面；已知真实缺陷样本都只
    跨 2 条子句，按最小必要范围处理。

    纯结构信号（否定词+顿号连接），不针对任何具体道具名或词语做特判；刻意只认
    "无/没有/未见/不含/不带/没"这几个完整的否定词，不认单字"不"——"不规则""不
    锈钢""不透明"这类材质/形状描述词本身就以"不"开头，若把单字"不"也算作否定
    词前缀，会把这些合法描述错误并入否定组。
    """
    clauses = split_appearance_clauses(text)
    seps = _clause_trailing_separators(text)
    groups: list[frozenset[int]] = []
    for i, clause in enumerate(clauses, start=1):
        if not _NEGATION_LEAD_RE.match(clause) or i >= len(clauses):
            continue
        next_clause = clauses[i]
        if _NEGATION_LEAD_RE.match(next_clause):
            continue  # 下一条自己也是新的否定起句，不归并
        if not _CLAUSE_HARD_BREAK_RE.search(seps[i - 1]):
            groups.append(frozenset({i, i + 1}))
    return groups


def _description_clause_count(description: str) -> int:
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
    if _description_clause_count(description) >= MIN_DESCRIPTION_CLAUSES:
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
    if _description_clause_count(appearance) < MIN_APPEARANCE_FEATURES:
        appearance = f"{appearance}。{description.strip()}" if appearance else description.strip()
    aliases = [a.strip() for a in response.aliases if a.strip() and a.strip() != label]
    return {"appearance_canonical": appearance, "aliases": aliases}
