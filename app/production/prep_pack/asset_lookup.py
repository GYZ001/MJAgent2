"""Deterministic DB lookups binding a resolved character/scene to its existing
portrait/scene-reference row, including scene-alias registration.

Split out of app/production/prep_pack.py.
"""
from __future__ import annotations

from app.schemas import Bible
from app.validators import match_scene_name


def _resolve_portrait_id(conn, project_id: str, character_name: str, episode_no: int) -> str | None:
    row = conn.execute(
        "SELECT id FROM character_portraits WHERE project_id=? AND character_name=? "
        "AND ep_start<=? AND (ep_end IS NULL OR ep_end>=?) ORDER BY ep_start DESC LIMIT 1",
        (project_id, character_name, episode_no, episode_no),
    ).fetchone()
    return str(row["id"]) if row else None


def _resolve_scene_reference_id(conn, project_id: str, scene_name: str, episode_no: int) -> str | None:
    row = conn.execute(
        "SELECT id FROM scene_references WHERE project_id=? AND scene_name=? "
        "AND ep_start<=? AND (ep_end IS NULL OR ep_end>=?) ORDER BY ep_start DESC LIMIT 1",
        (project_id, scene_name, episode_no, episode_no),
    ).fetchone()
    return str(row["id"]) if row else None


def _prep_pack_scene_reference_origin_episode(conn, scene_reference_id: str) -> int | None:
    """"来源集号"（第30轮②）：直接复用 scene_references.ep_start——这个
    场景参考在注册表里生效的起始集号，是现成数据，不另外发明新的追踪
    字段（alias_inherited 绑定的合法性来源于"这个场景本来就已经在注册表
    里"，ep_start 正是这件事本身的记录）。"""
    row = conn.execute(
        "SELECT ep_start FROM scene_references WHERE id=?", (scene_reference_id,),
    ).fetchone()
    if not row or row["ep_start"] is None:
        return None
    return int(row["ep_start"])


# 场景别名锚定（1.5.1，真实第18轮审计 A2 主病灶，47 条）：场景规范名（如
# "杂役处居所内"）往往是发现时铸造的标签，天然不在原文——本集若换了个
# 说法提这个场景（"杂役们住的地方"），_resolve_scene_reference_id 的裸精确
# 匹配（只查 scene_references.scene_name）找不到它，哪怕这个说法早就被
# app.scenes._append_scene_alias 登记成了该场景的别名
# （Bible.scenes[].aliases）也一样——写入和读取完全脱节：别名库在长，但
# 场景解析从来不读它，同一个说法每次都要重新走一遍发现（多余的模型调用，
# 也多一次误判机会）。
def _prep_pack_resolve_scene_reference_with_alias(
    conn, project_id: str, episode_no: int, resolved_name: str, bible: Bible,
) -> tuple[str | None, str]:
    """裸精确匹配优先；失败后复用 app.validators.match_scene_name（跟
    app.scenes 的发现路径同一套判定，含别名，allow_fuzzy=False 避免模糊
    误配）把 resolved_name 归一到已登记的规范场景名，再用规范名查表。
    返回 (scene_reference_id, canonical_name)：canonical_name 供调用方判断
    是否需要把这次的原文措辞记为新别名（不同才需要，见
    _prep_pack_register_scene_alias_if_new）。
    """
    scene_reference_id = _resolve_scene_reference_id(
        conn, project_id, resolved_name, episode_no,
    )
    if scene_reference_id:
        return scene_reference_id, resolved_name
    canonical = match_scene_name(resolved_name, bible.scenes, allow_fuzzy=False)
    if not canonical or canonical == resolved_name:
        return None, resolved_name
    scene_reference_id = _resolve_scene_reference_id(
        conn, project_id, canonical, episode_no,
    )
    return scene_reference_id, canonical


def _prep_pack_register_scene_alias_if_new(
    conn, project_id: str, *, canonical_name: str, wording: str,
) -> bool:
    """把本集实际用到的原文措辞记为该场景的新别名（幂等，见
    app.scenes._append_scene_alias：已登记过直接返回 False，不重复写）。
    别名库随集数增长越来越全，是通用设计，不认识任何具体场景/词形。
    """
    if not wording or wording == canonical_name:
        return False
    from app.scenes import _append_scene_alias

    return _append_scene_alias(conn, project_id, canonical_name, wording)


