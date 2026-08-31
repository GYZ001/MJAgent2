"""角色圣经（Bible）与场景图素材库自身的结构校验。
"""
from __future__ import annotations

import re

from app.schemas import Bible

def validate_bible(bible: Bible) -> list[str]:
    from app.refs import (
        PRODUCTION_APPEARANCE_MAX_CHARS,
        PRODUCTION_APPEARANCE_MIN_CHARS,
    )

    errors = []
    # 初始人物谱由 prompt 约束为 ≤8 个；上限放宽到 60，给「按 20 集补录新登场角色」留出增长空间。
    if not 1 <= len(bible.characters) <= 60:
        errors.append(f"characters 数量 {len(bible.characters)}，要求 1~60 个")
    names = [c.name for c in bible.characters]
    if len(names) != len(set(names)):
        errors.append("characters.name 存在重复")
    # 规则②——硬失败：某角色的 name 精确等于另一角色某个 alias.text，直接意味着
    # 两张卡是同一个人。与「同一 alias 被 ≥2 角色登记」（规则①）不是一回事，处置
    # 也必须不同：规则①是真实存在的合法数据（如"大汉"同时是两个不同角色的非排他
    # 描述性别名），做成硬失败会拒收正确的人物谱、阻塞该项目全部写入，因此不在
    # 这里校验，只交给 app.portraits.card_owner.resolve_card_owner 在建卡时判
    # conflict、fail closed。
    name_set = set(names)
    for i, c in enumerate(bible.characters):
        for alias in c.aliases:
            alias_text = (alias.text or "").strip()
            if alias_text and alias_text != c.name and alias_text in name_set:
                errors.append(
                    f"characters[{i}]({c.name}).aliases 别名「{alias_text}」与角色"
                    f"「{alias_text}」的 name 完全相同，两者应是同一个人，人物谱不能"
                    "把同一个人登记成两张卡"
                )
    # 无证据兜底防线：模型在候选缺失时会编出「待定主角」「未知角色」这类占位人物，
    # 这类名字既没有原文依据，又会被下游当成真人拿去定妆，必须在产物层直接拒收。
    placeholder_pattern = re.compile(r"(待定|待补|未知|未命名|占位|暂定|TBD|unknown|placeholder)", re.I)
    for i, c in enumerate(bible.characters):
        if placeholder_pattern.search(c.name or ""):
            errors.append(f"characters[{i}] 名称「{c.name}」是占位名，人物谱不接受无原文依据的角色")
    if placeholder_pattern.search(bible.world.era or ""):
        errors.append(f"world.era「{bible.world.era}」是占位值，必须依据原文判定年代")
    if placeholder_pattern.search(bible.world.genre or ""):
        errors.append(f"world.genre「{bible.world.genre}」是占位值，必须依据原文判定题材")
    for i, c in enumerate(bible.characters):
        if c.appearance_status == "deferred":
            if c.portrait_eligible:
                errors.append(
                    f"characters[{i}]({c.name}) 外观为 deferred 时不得自动定妆"
                )
        elif not PRODUCTION_APPEARANCE_MIN_CHARS <= len(c.appearance_canonical) <= PRODUCTION_APPEARANCE_MAX_CHARS:
            errors.append(
                f"characters[{i}]({c.name}).appearance_canonical 长度 "
                f"{len(c.appearance_canonical)} 字，要求 "
                f"{PRODUCTION_APPEARANCE_MIN_CHARS}~{PRODUCTION_APPEARANCE_MAX_CHARS} 字"
            )
        for r in c.relationships:
            if r.to not in names:
                errors.append(f"characters[{i}]({c.name}).relationships 指向「{r.to}」不在角色列表中")
    if not 15 <= len(bible.world.visual_style_canonical) <= 60:
        errors.append(f"world.visual_style_canonical 长度 {len(bible.world.visual_style_canonical)} 字，要求 15~60 字")
    return errors


def validate_scene_bible(scenes: list) -> list[str]:
    """场景圣经业务校验（与 validate_bible 同构）：数量 1~40、name 唯一非空、
    scene_canonical 长度取自 SCENE_CANONICAL_MIN/MAX_CHARS（足以稳定定场又不冗长）。"""
    from app.refs import SCENE_CANONICAL_MAX_CHARS, SCENE_CANONICAL_MIN_CHARS

    errors: list[str] = []
    if not 1 <= len(scenes) <= 40:
        errors.append(f"scenes 数量 {len(scenes)}，要求 1~40 个")
    names = [(getattr(s, "name", "") or "").strip() for s in scenes]
    if any(not n for n in names):
        errors.append("scenes.name 不能为空")
    if len(names) != len(set(names)):
        errors.append("scenes.name 存在重复")
    for i, s in enumerate(scenes):
        canonical = getattr(s, "scene_canonical", "") or ""
        if not SCENE_CANONICAL_MIN_CHARS <= len(canonical) <= SCENE_CANONICAL_MAX_CHARS:
            errors.append(
                f"scenes[{i}]({names[i] or '?'}).scene_canonical 长度 {len(canonical)} 字，"
                f"要求 {SCENE_CANONICAL_MIN_CHARS}~{SCENE_CANONICAL_MAX_CHARS} 字"
            )
    return errors
