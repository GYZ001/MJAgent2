"""编排计划的指纹缓存：JSON 边车文件，不新开数据库表。

"挂产物信号，不挂状态字段"：缓存是否命中只看指纹是否相等，不看任何"是否已
生成过"的布尔状态字段——指纹本身已经把版本集合/节拍表/曲库/开关状态编码
进去了，指纹相等就意味着这些输入全部没变，重新生成只会得到同一份答案。
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.atomic_io import atomic_write_text
from app.final_edit_enhance.plan_generate import (
    EnhancementPlan, ResolvedMonologueLine, ResolvedMusicCue, ResolvedTeaserClip,
)


def cache_path(final_path: Path) -> Path:
    return final_path.with_name("episode.enhancement-plan.json")


def load_cached_plan(path: Path, fingerprint: str) -> EnhancementPlan | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("fingerprint") != fingerprint:
        return None
    # 第二道防线（第一道是上面的指纹比对，指纹已经把 ``_PLAN_RULES_VERSION``
    # 编码进去，规则变了指纹必变）：就算指纹意外撞车，旧缓存里 dataclass 的
    # 字段集合若与当前代码不一致，下面的 ``ResolvedTeaserClip(**c)`` 之类
    # 构造调用会因未知/缺失关键字参数直接报 ``TypeError``——2026-09-29
    # 把 ``end_s`` 从 ``ResolvedTeaserClip`` 里去掉之后，任何仍带着旧
    # ``end_s`` 字段的缓存 JSON 走到这里都会被下面的 except 捕获，安全地
    # 当作"没有可用缓存"处理，退回重新生成，不会把半旧的形状"半读"进来。
    try:
        return EnhancementPlan(
            music_cues=tuple(ResolvedMusicCue(**c) for c in payload["music_cues"]),
            teaser_clips=tuple(ResolvedTeaserClip(**c) for c in payload["teaser_clips"]),
            monologue_lines=tuple(ResolvedMonologueLine(**m) for m in payload["monologue_lines"]),
            dropped=tuple(payload["dropped"]),
            teaser_total_duration_s=float(payload["teaser_total_duration_s"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def save_plan(path: Path, fingerprint: str, plan: EnhancementPlan) -> None:
    payload: dict[str, Any] = {
        "fingerprint": fingerprint,
        "music_cues": [asdict(c) for c in plan.music_cues],
        "teaser_clips": [asdict(c) for c in plan.teaser_clips],
        "monologue_lines": [asdict(m) for m in plan.monologue_lines],
        "dropped": list(plan.dropped),
        "teaser_total_duration_s": plan.teaser_total_duration_s,
    }
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
