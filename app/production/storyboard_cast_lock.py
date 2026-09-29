"""分镜台：同框人物清单锁定的确定性回填（2026-09-28，《顾念长安》第 1 集真实回归驱动）。

背景：成片审查发现第 9 段背景自动出现与顾屿撞脸的路人和陌生女性、真顾屿却不在温念桌边，
第 8 段也冒出未铺垫的顾客——``storyboard_dialects.SEEDANCE_DIALECT_INSTRUCTIONS`` 里已有
一条「群像要正向锁人数并加负向排除」的规则，但那只是教模型自己写；全片贯穿约束模板里的
「人数锁定」四个字在 29/30 段逐字相同、不含任何实际人数，没有约束力。

判据从数据推导（不是关键词枚举）：直接读模型自己产出的
``resources.characters``——同一次调用里模型已经按 identity 契约把这一段可见的人物列了出来
（``visibility == "visible"``），这里只是把这份数据换算成一句写死人数与正名的正面陈述追加进
``prompt_text``，与 ``storyboard_continuity_memo.ensure_travel_direction_in_prompt`` 同一
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
"""
from __future__ import annotations

import re
from typing import Any

#: 本函数自己固定生成的句式的首尾结构标记；``[^\n]*?`` 非贪婪，逐句独立匹配，
#: 不会跨行把无关内容也吃进去。只匹配这个精确的收尾短语，不影响「人数锁定（画面
#: 中只有……不出现其他客人或店员）」这类嵌在别处、收尾用词不同的正常文本。
_CAST_LOCK_SENTENCE_PATTERN = re.compile(r"\n?画面中只有[^\n]*?不出现其他人物或路人。")


def ensure_cast_lock_in_prompt(draft: Any) -> list[str]:
    """本段 ``resources.characters`` 里可见角色的实际数量与正名，写成「画面中只有
    @A、@B 共 2 人，不出现其他人物或路人」写进 ``prompt_text`` 末尾；幂等判断按
    ``_CAST_LOCK_SENTENCE_PATTERN`` 这个结构标记识别既有写法（不论格式是否与本次
    生成的逐字相同）先整体剥离再统一写回唯一一句——见模块 docstring 2026-09-28
    幂等判断改版。返回值恒为空列表——这是确定性回填，不是校验，不参与语义重试/
    失败判定，与 ``ensure_travel_direction_in_prompt`` 同一先例。
    """
    names = _visible_character_names(draft)
    if not names:
        return []
    prompt = str(getattr(draft, "prompt_text", "") or "")
    if not prompt.strip():
        return []
    lock_sentence = _cast_lock_sentence(names)
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


def _cast_lock_sentence(names: list[str]) -> str:
    mentions = "、".join(f"@{name}" for name in names)
    return f"画面中只有{mentions}共{len(names)}人，不出现其他人物或路人。"


__all__ = ["ensure_cast_lock_in_prompt"]
