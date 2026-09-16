"""分镜段草稿的确定性修补：群演/无参考图身份上的 @ 标记机械去掉。

@名字 是「打包时替换成 @图片N」的绑定标记，只对本段 resources.characters 里带
portrait_id 的人物谱角色与场景有意义（见 storyboard_dialects.reference_mention_errors）。
模型偶尔把它推广到群演——2026-09-14 第 12 集第 11 段连写三次「@那修士的对手」，
final_identity_prompt_errors 按规则拒绝、重试耗尽、整集失败。

这条修补只做一件机械的事：@X 里的 X 若是本段已知的群演（草稿 resources 里
subject_kind 为 extra/crowd、或 identity_id 不是 bible: 前缀的条目）或准备包
functional_extras 的标签，就把 @ 去掉，正文一字不改；X 不在这两个集合里（真正
写错的人物引用）仍交给校验拦截。validate 回调里调用，永远返回空错误列表。

2026-09-16（龙猫出爪连播第 3–6 集整批失败）补两条硬约束，都是实测的死锁：
**它剥掉的 @ 正是 reference_mention_errors 下一步要求必须在的那一个**，模型第
2、3 次重试都已照写 `@龙猫`，每次都被这里改掉再被拦下，三次预算烧完整集失败。

- **有参考图的角色名永不剥离**（``_protected_names``）。第 4/5 集准备包把
  ``bible:龙猫``（有 portrait_id）同时登记成 functional_extras 的 label「龙猫」
  「小龙」，同一个实体两套身份，原来的实现只看 label 就动手。角色卡优先：
  撞车时以「有参考图」为准，群演那一份登记视为重复。
- **按最长整名匹配 + 词边界改写，不做裸 replace**（``_rewrite_markers``）。
  第 6 集 label「王婶」是角色名「王婶的老狗」的前缀，``replace("@王婶", "王婶")``
  把一个合法的角色引用拦腰斩成 `王婶的老狗`。现在从 @ 处取能匹配到的最长已知
  名字，且只在名字后面确实是分隔符（标点/空格/行尾）时才剥离——「@他们的领队」
  这种以群演标签开头、后面还连着字的引用一律不动，交给校验如实报错。
"""
from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)

#: 汉字/字母/数字算「词内字符」：剥离只在名字后面不是这类字符时发生，避免把
#: 更长的合法名字（@王婶的老狗）按它的前缀标签（王婶）拆坏。
_WORD_CHAR_RE = re.compile(r"[0-9A-Za-z一-鿿]")


def _entry_names(entry: Any) -> set[str]:
    """一个角色条目真正能绑到参考图的写法：正名（identity_id 主体）与 display_name。

    **不含别名。** 打包侧 ``seedance_reference_notes._related_names`` 只按
    relatedCharacterIds/entity_name 建 @名字→@图片N 的映射，别名根本不在那张表里，
    保护一个绑不到图的写法只会让无绑定的 @ 混进供应商请求。判据也要跟两个校验器
    对齐：``reference_mention_errors`` 认 identity_id 主体，
    ``final_identity_prompt_errors`` 认 display_name 或 identity_id 主体——任何一侧
    比另一侧宽，宽的那部分就是必然发生的线上故障。实测第 5 集的角色别名里登记着
    代词「你」，按别名保护会让 ``@你`` 这种指向群演的写法逃过剥离。
    """
    get = entry.get if isinstance(entry, dict) else lambda key, default=None: getattr(entry, key, default)
    names = {str(get("identity_id", "") or "").split(":", 1)[-1], str(get("display_name", "") or "")}
    return {name.strip() for name in names if name and name.strip()}


def _portrait_entries(draft: Any, payload: dict[str, Any] | None) -> list[Any]:
    """本段草稿与准备包里「带参考图」的角色条目。"""
    entries = [
        character
        for character in getattr(getattr(draft, "resources", None), "characters", None) or []
        if getattr(character, "portrait_id", None)
    ]
    manifest = (payload or {}).get("asset_manifest") or {}
    entries.extend(
        character
        for character in manifest.get("characters") or []
        if isinstance(character, dict) and character.get("portrait_id")
    )
    return entries


def _protected_names(draft: Any, payload: dict[str, Any] | None) -> set[str]:
    """有参考图的角色名——@ 是它们绑图的唯一途径，任何情况下都不剥离。"""
    names: set[str] = set()
    for entry in _portrait_entries(draft, payload):
        names.update(_entry_names(entry))
    return names


def _known_extra_names(draft: Any, payload: dict[str, Any] | None) -> set[str]:
    """本段已知的无图群演标签：草稿里的 extra/crowd/非 bible 条目 + 准备包 label。"""
    names: set[str] = set()
    for character in getattr(getattr(draft, "resources", None), "characters", None) or []:
        identity_id = str(getattr(character, "identity_id", "") or "")
        display_name = str(getattr(character, "display_name", "") or "").strip()
        name = display_name or identity_id.split(":", 1)[-1].strip()
        is_extra = (
            getattr(character, "subject_kind", "") in {"extra", "crowd"}
            or not identity_id.startswith("bible:")
        )
        if name and is_extra and not getattr(character, "portrait_id", None):
            names.add(name)
    manifest = (payload or {}).get("asset_manifest") or {}
    for extra in manifest.get("functional_extras") or []:
        label = str((extra or {}).get("label") or "").strip()
        if label:
            names.add(label)
    return names


def _longest_name_at(prompt: str, pos: int, candidates: list[str]) -> str:
    """从 ``pos`` 起能匹配上的最长已知名字；``candidates`` 须按长度降序。"""
    return next((name for name in candidates if prompt.startswith(name, pos)), "")


def _rewrite_markers(prompt: str, extras: set[str], protected: set[str]) -> tuple[str, list[str]]:
    """逐个 @ 决定去留：命中群演标签且后面是分隔符才剥离，其余原样保留。"""
    candidates = sorted(extras | protected, key=len, reverse=True)
    out: list[str] = []
    stripped: list[str] = []
    index = 0
    while index < len(prompt):
        if prompt[index] != "@":
            out.append(prompt[index])
            index += 1
            continue
        name = _longest_name_at(prompt, index + 1, candidates)
        tail = index + 1 + len(name)
        bounded = tail >= len(prompt) or not _WORD_CHAR_RE.match(prompt[tail])
        if name and bounded and name in extras and name not in protected:
            out.append(name)
            stripped.append(name)
        else:
            out.append("@" + name)
        index = tail if name else index + 1
    return "".join(out), stripped


def strip_extra_reference_markers(draft: Any, payload: dict[str, Any] | None = None) -> list[str]:
    """把本段已知群演标签前的 @ 去掉；其它内容不动。返回空列表（不是校验）。"""
    prompt = str(getattr(draft, "prompt_text", "") or "")
    if "@" not in prompt:
        return []
    extras = _known_extra_names(draft, payload)
    protected = _protected_names(draft, payload)
    overlap = sorted(extras & protected)
    if overlap:
        log.warning(
            "[STORYBOARD_REFERENCE_REPAIR] 群演标签与有参考图的角色重名，按角色卡为准保留 @：%s",
            "、".join(overlap),
        )
    repaired, stripped = _rewrite_markers(prompt, extras, protected)
    if stripped:
        draft.prompt_text = repaired
        log.info("[STORYBOARD_REFERENCE_REPAIR] 群演上的 @ 已去掉：%s", "、".join(sorted(set(stripped))))
    return []


__all__ = ["strip_extra_reference_markers"]
