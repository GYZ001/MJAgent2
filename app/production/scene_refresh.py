"""场景判定的并发复核（2026-09-06 第 11 轮场景库核查：三个广场、四个东峰、三个平顶山各建一条）。

``ensure_scenes_for_labels`` 把 ``assess_new_scene`` 的「已有场景」名单固定在函数入口那一刻的
人物谱快照；30 集映射台并发时，别的集在这几十秒里刚建的场景不在名单里，模型看不见就判成新场景。
与人物卡的 ``card_commit`` 同一纪律：裁决之后、落库之前重读一次人物谱，场景名单变了（多了本快照没有
的场景）就拿新名单再判一次——只在真的发生竞态时才多一次模型调用。判据仍是模型看着完整名单做的
选择，代码不猜两条场景是不是同一个地方。
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from app.schemas import Bible


async def refresh_verdict_if_scenes_changed(
    conn, project_id: str, scenes: list[Any], verdict: dict, *,
    assess: Callable[[list[Any]], Awaitable[dict]],
) -> tuple[list[Any], dict]:
    """返回 (最新场景名单, 判定)。名单没变原样返回；变了则用新名单重判并返回新判定。"""
    row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row or not (row["bible_json"] or "").strip():
        return scenes, verdict
    fresh = list(Bible.model_validate(json.loads(row["bible_json"])).scenes)
    known = {str(getattr(s, "name", "") or "") for s in scenes}
    if not any(str(getattr(s, "name", "") or "") not in known for s in fresh):
        return scenes, verdict
    return fresh, await assess(fresh)
