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
"""
from __future__ import annotations

import re
from typing import Any

#: 本函数自己固定生成的句式的首尾结构标记；``[^\n]*?`` 非贪婪，逐句独立匹配，
#: 不会跨行把无关内容也吃进去。``(?:现实|闪回)?`` 可选前缀兼容 2026-09-29 新增的
#: 「现实画面中只有……；闪回画面中只有……」复合写法——不加前缀时行为与旧版完全
#: 相同。只匹配这个精确的收尾短语，不影响「人数锁定（画面中只有……不出现其他
#: 客人或店员）」这类嵌在别处、收尾用词不同的正常文本。
_CAST_LOCK_SENTENCE_PATTERN = re.compile(r"\n?(?:现实|闪回)?画面中只有[^\n]*?不出现其他人物或路人。")

#: ``unmentioned_visible_character_advisories`` 提取 prompt_text 里 @ 引用的
#: 完整词——与 storyboard_identity_validation.final_identity_prompt_errors 用
#: 同一条正则，取最长连续词字符（含中文）：「@顾屿家客房」整体只提取出
#: 「顾屿家客房」一个词，不会被误判成对「顾屿」的点名（CLAUDE.md「按数据推导，
#: 不做前缀猜测」）。
_MENTION_TOKEN_RE = re.compile(r"@([\w:-]+)")


def ensure_cast_lock_in_prompt(draft: Any) -> list[str]:
    """本段 ``resources.characters`` 里可见角色的实际数量与正名，写成「画面中只有
    @A、@B 共 2 人，不出现其他人物或路人」写进 ``prompt_text`` 末尾；有
    ``resources.flashback_figures`` 时改写成「现实画面中只有……；闪回画面中只有
    ……」分组锁定（见模块 docstring）。幂等判断按 ``_CAST_LOCK_SENTENCE_PATTERN``
    这个结构标记识别既有写法（不论格式是否与本次生成的逐字相同）先整体剥离再
    统一写回唯一一句——见模块 docstring 2026-09-28 幂等判断改版。返回值恒为空
    列表——这是确定性回填，不是校验，不参与语义重试/失败判定，与
    ``ensure_travel_direction_in_prompt`` 同一先例。
    """
    names = _visible_character_names(draft)
    flashback_labels = _flashback_figure_labels(draft)
    if not names and not flashback_labels:
        return []
    prompt = str(getattr(draft, "prompt_text", "") or "")
    if not prompt.strip():
        return []
    lock_sentence = _cast_lock_sentence(names, flashback_labels)
    deduped = _CAST_LOCK_SENTENCE_PATTERN.sub("", prompt).rstrip()
    normalized = (deduped + "\n" if deduped else "") + lock_sentence
    if normalized == prompt:
        return []
    draft.prompt_text = normalized
    return []


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


def _cast_lock_sentence(names: list[str], flashback_labels: list[str]) -> str:
    """无闪回人物时逐字保持旧格式（见模块 docstring 2026-09-29 条）；有闪回人物
    时现实/闪回两组分开各自锁定，任一组为空就只写非空的那一组，不写「共0人」
    这种空话。"""
    if not flashback_labels:
        mentions = "、".join(f"@{name}" for name in names)
        return f"画面中只有{mentions}共{len(names)}人，不出现其他人物或路人。"
    flashback_clause = f"闪回画面中只有{'、'.join(flashback_labels)}"
    if not names:
        return f"{flashback_clause}，不出现其他人物或路人。"
    real_mentions = "、".join(f"@{name}" for name in names)
    return f"现实画面中只有{real_mentions}共{len(names)}人；{flashback_clause}，不出现其他人物或路人。"


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
    body = _CAST_LOCK_SENTENCE_PATTERN.sub("", prompt)
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
