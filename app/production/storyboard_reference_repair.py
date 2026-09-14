"""分镜段草稿的确定性修补：群演/无参考图身份上的 @ 标记机械去掉。

@名字 是「打包时替换成 @图片N」的绑定标记，只对本段 resources.characters 里带
portrait_id 的人物谱角色与场景有意义（见 storyboard_dialects.reference_mention_errors）。
模型偶尔把它推广到群演——2026-09-14 第 12 集第 11 段连写三次「@那修士的对手」，
final_identity_prompt_errors 按规则拒绝、重试耗尽、整集失败。

这条修补只做一件机械的事：@X 里的 X 若是本段已知的群演（草稿 resources 里
subject_kind 为 extra/crowd、或 identity_id 不是 bible: 前缀的条目）或准备包
functional_extras 的标签，就把 @ 去掉，正文一字不改；X 不在这两个集合里（真正
写错的人物引用）仍交给校验拦截。validate 回调里调用，永远返回空错误列表。
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def _known_extra_names(draft: Any, payload: dict[str, Any] | None) -> list[str]:
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
    return sorted(names, key=len, reverse=True)


def strip_extra_reference_markers(draft: Any, payload: dict[str, Any] | None = None) -> list[str]:
    """把本段已知群演标签前的 @ 去掉；其它内容不动。返回空列表（不是校验）。"""
    prompt = str(getattr(draft, "prompt_text", "") or "")
    if "@" not in prompt:
        return []
    repaired = prompt
    stripped: list[str] = []
    for name in _known_extra_names(draft, payload):
        marker = f"@{name}"
        if marker in repaired:
            repaired = repaired.replace(marker, name)
            stripped.append(name)
    if stripped:
        draft.prompt_text = repaired
        log.info("[STORYBOARD_REFERENCE_REPAIR] 群演上的 @ 已去掉：%s", "、".join(stripped))
    return []


__all__ = ["strip_extra_reference_markers"]
