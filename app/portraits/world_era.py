"""世界书年代 → 定妆照的年代服饰约束（角色自己没有 period_costume_canonical 时的退路）。

2026-09-15《龙猫出爪》：现代都市宠物医院的故事，阿凯的卡只有「二十岁出头的年轻男性」，
世界书 era 为空，国漫画风默认把他画成了古装、挂玉佩，视频里的兽医诊室站着一个古人。
era 只由用户在世界书里填、或由 timeline_anchors 从原文逐字锚点推导，这里不推断、不编造：
空就还是空（提示词退回「服从锚点与世界年代」的既有措辞）。纯标准库 + sqlite 行读取。
"""
from __future__ import annotations

import json
from typing import Any


def world_era_from_bible_json(bible_json: str | None) -> str:
    try:
        bible = json.loads(bible_json or "{}")
    except ValueError:
        return ""
    world = bible.get("world") if isinstance(bible, dict) else None
    return str((world or {}).get("era") or "").strip()


def project_world_era(conn: Any, project_id: str) -> str:
    row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    return world_era_from_bible_json(row["bible_json"] if row else None)


__all__ = ["project_world_era", "world_era_from_bible_json"]
