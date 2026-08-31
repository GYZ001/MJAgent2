"""断点续跑兼容层：把历史 ``bible_auto_changes_json`` 队列里的临时人物注入当前
剧本生成上下文。新流程在发现阶段直接自动入卡，这两个函数只服务旧运行记录的
向后兼容，不参与新卡的创建路径（那是 ``cards.py`` 的 ``ensure_character_card``）。

从 ``cards.py`` 搬出：该文件 line_count 已顶着 app/FILE_CONVENTIONS.toml 的棘轮
基线，这两个函数与"建卡"逻辑本身正交（没有其它函数依赖它们，没有测试直接引用
它们——只经 ``app/portraits/__init__.py`` 从 ``cards`` 重新导出），是最安全的
搬迁对象，腾出的行数预算留给建卡时机相关的新逻辑。``cards.py`` 继续
``from .bible_compat import (...)`` 把两个名字重新导入进自己的模块命名空间，
外部 ``from .cards import bible_with_provisional_characters`` 之类的既有导入不受
影响。
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from app.db import get_conn
from app.schemas import Bible, Character

from ._db_probe import _has_column


def bible_with_provisional_characters(bible: Bible, discovery: dict | None) -> Bible:
    """兼容旧运行记录：把历史临时人物注入当前剧本生成上下文。

    新流程会在发现阶段直接自动入卡；此函数只用于断点续跑的向后兼容。
    """
    cards = (discovery or {}).get("provisional_characters") or []
    if not cards:
        return bible
    characters = list(bible.characters)
    known = {character.name for character in characters}
    for card in cards:
        if not isinstance(card, dict):
            continue
        try:
            character = Character.model_validate(card)
        except ValidationError:
            continue
        if character.name in known:
            continue
        characters.append(character)
        known.add(character.name)
    return bible.model_copy(update={"characters": characters})


def bible_with_pending_characters_for_text(
    project_id: str,
    bible: Bible,
    text: str,
) -> Bible:
    """恢复/续跑时从历史队列恢复本章实际出现的临时人物约束。

    这是只读的旧数据兼容路径，不触发出图。
    """
    if not (text or "").strip():
        return bible
    conn = get_conn()
    if not _has_column(conn, "projects", "bible_auto_changes_json"):
        return bible
    row = conn.execute(
        "SELECT bible_auto_changes_json FROM projects WHERE id=?", (project_id,),
    ).fetchone()
    try:
        items = json.loads(row["bible_auto_changes_json"] or "[]") if row else []
    except (TypeError, ValueError, json.JSONDecodeError):
        items = []
    cards: list[dict] = []
    for item in items:
        if (
            not isinstance(item, dict)
            or item.get("status") != "pending"
            or item.get("kind") not in {"new_character", "character_discovery", "new_bible_character"}
        ):
            continue
        name = str(item.get("character") or "").strip()
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        card = payload.get("character_card")
        if name and name in text and isinstance(card, dict):
            cards.append(card)
    return bible_with_provisional_characters(
        bible, {"provisional_characters": cards},
    )
