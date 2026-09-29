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
"""
from __future__ import annotations

from typing import Any


def ensure_cast_lock_in_prompt(draft: Any) -> list[str]:
    """本段 ``resources.characters`` 里可见角色的实际数量与正名，写成「画面中只有
    @A、@B 共 2 人，不出现其他人物或路人」追加进 ``prompt_text``；句子已存在（例如模型自己
    按方言规则写过一遍）则不重复追加。返回值恒为空列表——这是确定性回填，不是校验，
    不参与语义重试/失败判定，与 ``ensure_travel_direction_in_prompt`` 同一先例。
    """
    names = _visible_character_names(draft)
    if not names:
        return []
    prompt = str(getattr(draft, "prompt_text", "") or "")
    if not prompt.strip():
        return []
    lock_sentence = _cast_lock_sentence(names)
    if lock_sentence in prompt:
        return []
    draft.prompt_text = prompt.rstrip() + "\n" + lock_sentence
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
