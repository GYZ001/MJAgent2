"""分镜台：同框人物清单锁定的确定性回填（2026-09-28，《顾念长安》第 1 集真实回归驱动）。

背景：成片审查发现第 9 段背景自动出现与顾屿撞脸的路人和陌生女性、真顾屿却不在温念桌边，
第 8 段也冒出未铺垫的顾客——``storyboard_dialects.SEEDANCE_DIALECT_INSTRUCTIONS`` 里已有
一条「群像要正向锁人数并加负向排除」的规则，但那只是教模型自己写；全片贯穿约束模板里的
「人数锁定」四个字在 29/30 段逐字相同、不含任何实际人数，没有约束力。

判据从数据推导（不是关键词枚举）：直接读模型自己产出的
``resources.characters``——同一次调用里模型已经按 identity 契约把这一段可见的人物列了出来
（``visibility == "visible"``），这里只是把这份数据换算成一句写死人数与正名的正面陈述追加进
``prompt_text``，与 ``storyboard_travel_direction.ensure_travel_direction_in_prompt`` 同一
形状：不发明内容，只是把模型自己已经给出的结构化事实，确定性地转成视频模型真正会读的自由
文本。没有任何可见角色的段（纯画外音/旁白段）不写这句话——“无可见角色”本身就是诚实的事实，
不是需要补一句空话的缺口。
2026-09-28 幂等判断改版（《顾念长安》第 1 集真实回归发现的重复追加）：真实数据里
29 段中 24 段这句话逐字重复了两次，另有一段两次写法不同（其一缺 @、多一个空格）。
根因是 ``storyboard_dialects`` 教模型自己在正文里也写一句同形状的话（模块 docstring
第一段那条规则），模型时常照做——原判据按 ``lock_sentence in prompt`` 做逐字包含
检查，只要模型自己写的那句与本函数即将生成的 canonical 文本有一丝格式差异（缺
``@``、多一个空格、名字顺序不同……），包含检查就会失败而重复追加，即使两句表达的
是完全相同的「这段只有这几个人」这件事。改法：幂等判断不再比较逐字字符串，而是
用这句话固定的首尾结构标记（``画面中只有`` … ``不出现其他人物或路人。``——这是
本函数自己定义的模板边界，不是对模型自由文本的关键词猜测）识别出所有既有的同形状
写法（不论是模型自己写的、还是本函数上一次写的），先整体剥离，再统一写回唯一一句
canonical 文本，天然收敛到「同一段落只有一句」，不必判断两句在语义上是否说的是
同一件事——反正最终都要重写成同一句话。

2026-09-29「闪回人物」（真实回归 proj_ca86b15ab7d7 EP1 段16）：闪回/回忆中的人物
不进 ``resources.characters``（见 ``resources.flashback_figures``，
``app.schemas.segment_identity.FlashbackFigure``），人数锁定句因此需要把「现实
画面」与「闪回画面」的人物分开各自锁定，不能把两组人混进同一句「共N人」——那会
把闪回人物也算进现实同框人数，或者反过来把现实人物漏进闪回人数。``resources.
flashback_figures`` 为空时行为逐字不变（见 ``_cast_lock_sentence``）。首尾结构
标记同步放宽为可选的「现实」「闪回」前缀（``_CAST_LOCK_SENTENCE_PATTERN``），
否则新格式的幂等剥离会漏掉这两个前缀、让它们在下一次调用时越攒越多。

同一次真实回归还发现：即使人数锁定句写对了，`@顾屿` 只出现在这句锁定文本里、
正文其余部分从未点过这个人的名字，也足以说明这一镜实际画的可能不是这个角色
当前定妆照该有的样子——``unmentioned_visible_character_advisories`` 把这个信号
挂进 ``degraded_capabilities``，只提示、不阻断、不改写（判据从数据推导：直接读
``resources.characters``/``prompt_text``，不是关键词黑名单）。

2026-09-30「开放前缀残留」（B 机 provider_calls id=81302，第 1 集第 8/22 段 opus
原始输出）：模型自写的锁定句前面常带一个场景/时空词（「咖啡馆画面中只有……」
「当下画面中只有……」），``(?:现实|闪回)?`` 这个可选前缀只认死这两个词，模型
用别的开放词时，旧判据的 ``.sub("", prompt)`` 只剥掉从「画面中只有」起的部分，
把前缀词原样留在原处，落库后变成一个孤立的残词行（「咖啡馆」「当下」）。场景/
时空词是开放集合，枚举不完，改成按**结构**判断：锁定句若是从行首开始写的（它
前面、同一行内没有任何句末标点），前面的残留文字只能是模型自己加在这句话前面
的开放前缀，连同整行一起剥掉；锁定句前面、同一行内已经有一句带句末标点的完整
话（即锁定句是这一行里新起的第二句）时，前面那句是正文内容，保持剥离前的现有
行为、只剥锁定句本身。见 ``_strip_cast_lock_sentences``。

2026-10-01「每镜同人数」误读与约束行键名展开（第 1 集重做第二轮分镜独立核查，
``/tmp/mjtest/ep1_redo/segments_r2.json`` 第 14/30/31 段）：canonical 句「画面中
只有……共 2 人」是整段的出场名单，但视频模型逐帧执行时把「共 2 人」读成「每个
镜头都要有 2 人」，第 14 段镜头 2/3/4 是单人反打特写，锁定句却要求共 2 人，有
把另一人塞进单人特写的风险。改写为名单语义：``_cast_lock_sentence`` 产出「本段
(现实/闪回)画面出场人物共 N 人：……；每个镜头只画出该镜头文字写到的人，不出现
其他人物或路人。」——「共 N 人」明确是全段名单人数，不是单镜人数上限，真正的
单镜人数由每一镜自己的文字决定（``storyboard_dialects`` 同步改为要求模型在
镜头描述本身正向写出这一镜的人数与身份）。``_CAST_LOCK_SENTENCE_PATTERN`` 同时
收纳旧收尾「画面中只有……」与新收尾「本段……画面出场人物……」两种写法——存量
分镜（``storyboard_pack_prompts``/``shots`` 里已落库的旧写法）与新生成的产物都
要能被正确幂等剥离，不是二选一替换。

另一处真实缺陷（同一批段落，约束行）：模型把 ``storyboard_dialects`` 约束行里
系统自己的固定词「人数锁定」自行展开成带名单的括号说明（「人数锁定（每个镜头
画面中只有@温念 与@顾屿 共2人，不出现其他住户、房东或路人）」），这句以「…
或路人）」收尾、不是句号，``_CAST_LOCK_SENTENCE_PATTERN`` 本就不匹配（上面
``_CAST_LOCK_SENTENCE_PATTERN`` 注释里一直如此声明），于是两句锁定并存、互不
核对，而且这类展开句本身又是一句以「不出现……」开头的否定句，还会被
``storyboard_prose_review`` 的 ``negated_action`` 判据误当成对人物动作的否定
描写。判据仍从结构推导，不枚举名字：「人数锁定」这四个字是方言指令自己定义的
固定词，后面紧跟的括号展开整体替换回「人数锁定」四字本身，不论括号里写了谁、
写了几人——见 ``_CAST_LOCK_KEYWORD_EXPANSION_RE``。这一步并入
``_strip_cast_lock_sentences`` 而不是只在 ``ensure_cast_lock_in_prompt`` 里做：
``unmentioned_visible_character_advisories`` 也靠 ``_strip_cast_lock_sentences``
算「正文去掉锁定句之后还剩什么」，这份约束行展开句里同样带着 @ 提及，不先剥掉
会被误算成「正文里确实点过这个人的名」，掩盖真正的未点名问题——与 canonical
锁定句本身必须先剥掉再判定是同一个理由。
"""
from __future__ import annotations

import re
from typing import Any

#: 本函数自己固定生成的句式的首尾结构标记；``[^\n]*?`` 非贪婪，逐句独立匹配，
#: 不会跨行把无关内容也吃进去。``(?:现实|闪回)?`` 可选前缀兼容 2026-09-29 新增的
#: 「现实画面中只有……；闪回画面中只有……」复合写法——不加前缀时行为与旧版完全
#: 相同。只匹配这个精确的收尾短语，不影响「人数锁定（画面中只有……不出现其他
#: 客人或店员）」这类嵌在别处、收尾用词不同的正常文本——那类文本现在由
#: ``_CAST_LOCK_KEYWORD_EXPANSION_RE`` 单独处理，见模块 docstring 2026-10-01 条。
#: 2026-09-30 起不再在正则里内置前导 ``\n?``——是否连带剥掉前面同一行的内容，由
#: ``_strip_cast_lock_sentences`` 按这一行是否已有完整句子（句末标点）独立判断，
#: 不是正则能表达的结构，见模块 docstring 2026-09-30 条。2026-10-01 新增第二条
#: 可选收尾「本段(?:现实|闪回)?画面出场人物……不出现其他人物或路人。」（名单
#: 语义改写，见模块 docstring 2026-10-01 条）：旧收尾「画面中只有」与新收尾
#: 「本段……画面出场人物」用 ``|`` 并列，两种写法都要能被识别、整体剥离——存量
#: 分镜还是旧写法，新生成的是新写法，缺一种都会在下一次调用时重复追加。
_CAST_LOCK_SENTENCE_PATTERN = re.compile(
    r"(?:(?:现实|闪回)?画面中只有[^\n]*?不出现其他人物或路人。)"
    r"|(?:本段(?:现实|闪回)?画面出场人物[^\n]*?不出现其他人物或路人。)"
)

#: 约束行里系统自己的固定词「人数锁定」被模型自行展开成带名单的括号说明时
#: （「人数锁定（每个镜头画面中只有@温念 与@顾屿 共2人，不出现其他住户、
#: 房东或路人）」），整体替换回「人数锁定」四字本身——判据是结构（「人数锁定」
#: 这四个字后面紧跟的括号展开，全角半角括号都认），不枚举括号里出现过哪些
#: 名字，不论名字是谁、写了几人都剥，见模块 docstring 2026-10-01 条。
_CAST_LOCK_KEYWORD_EXPANSION_RE = re.compile(r"人数锁定[（(][^）)]*[）)]")

#: 判断「锁定句前面、同一行内是否已经写完一句独立的话」的句末标点——命中就说明
#: 前面的文字是另一句完整表达，不是锁定句自己的残留前缀，不剥；中文标点三个，
#: 这是本函数自己定义的句子边界判据，不是对模型自由文本的关键词枚举。
_SENTENCE_END_RE = re.compile(r"[。！？]")

#: ``unmentioned_visible_character_advisories`` 提取 prompt_text 里 @ 引用的
#: 完整词——与 storyboard_identity_validation.final_identity_prompt_errors 用
#: 同一条正则，取最长连续词字符（含中文）：「@顾屿家客房」整体只提取出
#: 「顾屿家客房」一个词，不会被误判成对「顾屿」的点名（CLAUDE.md「按数据推导，
#: 不做前缀猜测」）。
_MENTION_TOKEN_RE = re.compile(r"@([\w:-]+)")


def ensure_cast_lock_in_prompt(draft: Any) -> list[str]:
    """本段 ``resources.characters`` 里可见角色的实际数量与正名，写成「本段画面
    出场人物共 2 人：@A、@B；每个镜头只画出该镜头文字写到的人，不出现其他人物
    或路人」写进 ``prompt_text`` 末尾；有 ``resources.flashback_figures`` 时改写
    成「本段现实画面出场人物共……；闪回画面出场人物：……」分组锁定（见模块
    docstring 2026-10-01 条——「共 N 人」是全段名单人数，不是单镜人数，这是
    2026-10-01 从「画面中只有……共 N 人」改写的直接原因）。幂等判断靠
    ``_strip_cast_lock_sentences`` 按 ``_CAST_LOCK_SENTENCE_PATTERN`` 这个结构
    标记识别既有写法（新旧两种收尾都认，不论格式是否与本次生成的逐字相同，也
    不论前面是否带着模型自己加的开放前缀词）先整体剥离再统一写回唯一一句——见
    模块 docstring 2026-09-28/2026-09-30/2026-10-01 三条幂等判断改版，其中
    2026-10-01 一条同时把约束行里「人数锁定（……）」的展开句剥成短词（见
    ``_strip_cast_lock_sentences``）。返回值恒为空列表——这是确定性回填，不是
    校验，不参与语义重试/失败判定，与 ``ensure_travel_direction_in_prompt``
    同一先例。
    """
    names = _visible_character_names(draft)
    flashback_labels = _flashback_figure_labels(draft)
    if not names and not flashback_labels:
        return []
    prompt = str(getattr(draft, "prompt_text", "") or "")
    if not prompt.strip():
        return []
    lock_sentence = _cast_lock_sentence(names, flashback_labels)
    deduped = _strip_cast_lock_sentences(prompt).rstrip()
    normalized = (deduped + "\n" if deduped else "") + lock_sentence
    if normalized == prompt:
        return []
    draft.prompt_text = normalized
    return []


def _strip_cast_lock_sentences(prompt: str) -> str:
    """剥掉 ``prompt`` 里所有符合 ``_CAST_LOCK_SENTENCE_PATTERN`` 收尾结构的锁定句，
    并把约束行里「人数锁定（……）」这种模型自行展开的括号说明剥回「人数锁定」
    四字本身（``_CAST_LOCK_KEYWORD_EXPANSION_RE``，见模块 docstring 2026-10-01
    条）；供 ``ensure_cast_lock_in_prompt`` 的幂等回写与 ``unmentioned_visible_
    character_advisories`` 的未点名核验共用——后者正因为共用这个函数，约束行
    展开句里的 @ 提及也会先被剥掉，不会被误算成正文点过这个人的名。

    判据从结构推导，不枚举前缀词（见模块 docstring 2026-09-30 条）：锁定句前面、
    同一行内没有任何句末标点时，前面的文字只能是模型自己加在这句话前面的开放
    前缀（场景名、时空词……开放集合，枚举不完），连同整行一起剥掉，顺带吞掉
    行首那个换行以免留下空行；锁定句前面、同一行内已经有一句带句末标点的完整
    话时，说明锁定句是这一行里新起的第二句，前面那句是正文内容，只剥锁定句
    本身，与改版前逐字相同的行为。
    """
    prompt = _CAST_LOCK_KEYWORD_EXPANSION_RE.sub("人数锁定", prompt)
    pieces: list[str] = []
    cursor = 0
    for match in _CAST_LOCK_SENTENCE_PATTERN.finditer(prompt):
        start = match.start()
        line_start = prompt.rfind("\n", 0, start) + 1
        prefix = prompt[line_start:start]
        if _SENTENCE_END_RE.search(prefix):
            pieces.append(prompt[cursor:start])
        else:
            drop_from = line_start - 1 if line_start > 0 else 0
            pieces.append(prompt[cursor:drop_from])
        cursor = match.end()
    pieces.append(prompt[cursor:])
    return "".join(pieces)


def _visible_character_names(draft: Any) -> list[str]:
    """去重、保序的可见角色正名列表；跳过没有 display_name 的条目（画面上认不出的
    人不该被写进「只有这几个人」的正面清单，会让清单本身失真）。"""
    seen: dict[str, None] = {}
    for character in getattr(draft.resources, "characters", None) or []:
        if getattr(character, "visibility", "") != "visible":
            continue
        name = str(getattr(character, "display_name", "") or "").strip()
        if name:
            seen.setdefault(name, None)
    return list(seen)


def _flashback_figure_labels(draft: Any) -> list[str]:
    """去重、保序的闪回人物称呼列表；旧行没有 ``resources.flashback_figures``
    这个键时 ``getattr`` 拿不到属性，``or []`` 按"没有闪回人物"处理，与
    ``_visible_character_names`` 对旧行缺字段的兼容方式一致。"""
    seen: dict[str, None] = {}
    for figure in getattr(draft.resources, "flashback_figures", None) or []:
        label = str(getattr(figure, "label", "") or "").strip()
        if label:
            seen.setdefault(label, None)
    return list(seen)


#: 每个镜头只画出该镜头文字实际写到的人物——这是本段名单（「共 N 人」）与单镜
#: 人数的分界线，不许理解成「每一镜都要凑够 N 人」，见模块 docstring 2026-10-01
#: 条与真实回归（单人反打特写被要求共 2 人）。
_PER_SHOT_SCOPE_CLAUSE = "每个镜头只画出该镜头文字写到的人，不出现其他人物或路人。"


def _cast_lock_sentence(names: list[str], flashback_labels: list[str]) -> str:
    """名单语义（见模块 docstring 2026-10-01 条，取代旧版「画面中只有……共 N 人」
    的每镜人数误读）：无闪回人物时「本段画面出场人物共 N 人：……」；有闪回人物
    时现实/闪回两组分开各自列名单，任一组为空就只写非空的那一组，不写「共0人」
    这种空话；``_PER_SHOT_SCOPE_CLAUSE`` 固定收尾，明确「共 N 人」是全段名单
    人数，每一镜画哪些人由该镜自己的文字决定。"""
    if not flashback_labels:
        mentions = "、".join(f"@{name}" for name in names)
        return f"本段画面出场人物共{len(names)}人：{mentions}；{_PER_SHOT_SCOPE_CLAUSE}"
    flashback_clause = f"闪回画面出场人物：{'、'.join(flashback_labels)}"
    if not names:
        return f"本段{flashback_clause}；{_PER_SHOT_SCOPE_CLAUSE}"
    real_mentions = "、".join(f"@{name}" for name in names)
    return f"本段现实画面出场人物共{len(names)}人：{real_mentions}；{flashback_clause}；{_PER_SHOT_SCOPE_CLAUSE}"


def unmentioned_visible_character_advisories(draft: Any) -> list[str]:
    """可见角色（``resources.characters[].visibility == "visible"``）的参考图
    会被无条件发给视频模型，即使正文里除本模块追加的人数锁定句外再没有点过这个
    人的名字——人数锁定句本身一定会写一次 ``@正名``，单纯检查「@正名 是否出现在
    prompt_text 里」发现不了这种情况。只提示，不阻断、不改写：判据从数据推导
    （直接读 ``resources.characters``/``prompt_text``），不是关键词黑名单。

    真实回归（proj_ca86b15ab7d7 EP1 段16）：闪回中六岁的顾屿被模型登记进
    resources.characters 并绑定成年顾屿的定妆照，正文只描述了"六岁男孩"，
    「@顾屿」只出现在人数锁定句里——这条 advisory 本该是那次事故的信号，但
    事发时它还不存在。不止服务闪回这一种成因：任何"可见角色没有被正文实际
    点名"都会命中，闪回只是最容易复现的一种。

    ``@顾屿家客房`` 这类场景提及不会被误算成对「顾屿」的点名——``_MENTION_TOKEN_RE``
    与 ``final_identity_prompt_errors`` 用同一条正则，取 @ 后连续的完整词，
    「顾屿家客房」整体只是一个词，不等于「顾屿」。"画外音（{name}）"这个既有
    写法（见 storyboard_dialects.reference_mention_errors）与 @ 点名同等有效，
    不重复报告。
    """
    names = _visible_character_names(draft)
    if not names:
        return []
    prompt = str(getattr(draft, "prompt_text", "") or "")
    body = _strip_cast_lock_sentences(prompt)
    mentioned = set(_MENTION_TOKEN_RE.findall(body))
    missing = [name for name in names if name not in mentioned and f"画外音（{name}）" not in body]
    if not missing:
        return []
    shown = "、".join(missing)
    return [
        f"[STORYBOARD_PACK_RESOURCE_CHARACTER_UNMENTIONED][未拦截] 「{shown}」标记为本段实际出镜"
        "（resources.characters[].visibility=visible），但镜头正文里除人数锁定句外没有用 @ 点过名"
        "（\"画外音（姓名）\"写法同样算点名）：这个人的定妆照仍会原样发给视频模型；若这一镜画的其实是"
        "这个角色的另一个年龄或形态（例如闪回），请核对是否应改写进 resources.flashback_figures，"
        "不要继续绑定当前定妆照"
    ]


__all__ = ["ensure_cast_lock_in_prompt", "unmentioned_visible_character_advisories"]
