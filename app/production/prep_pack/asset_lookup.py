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


# 场景侧多提及引文聚合（真实故障 ERR-20260901-2e124f 排查结论）：
# _prep_pack_scene_alias_provenance 的 scene_event_evidence_quotes 形参
# 本是复数（isomorphic 于旧 event_chain[].source_evidence[].quote——一个
# 场景可能对应多个事件，各自一条独立引文），resolve_assets.py 2.0.2 砍
# event_chain 后调用方退化成只传"这一条提及自己的 quote"，丢了"同一
# 地点在本集被提到不止一次、只有其中一条真的带上逐字引文"这种聚合信息。
# 真实现场：同一次运行里 chunk 抽取重跑一遍，前一遍某场景的 quote 完整
# 逐字命中原文，后一遍同一场景因为流式响应中途截断、JSON 格式修复调用
# 诚实地把这个必填字段留空——最终被采纳的只有后一遍那条空引文的提及，
# 三路锚点候选全灭，拦停整集，即便"这地方在原文里确有依据"这件事本身
# 从未被推翻。这个函数把本集全部 scene_mentions 按"最终解析到哪个规范
# 场景"分组，收集每个规范场景名下全部非空引文——不重复解析合法性判断
# （不合法/未解析的提及在这里被安静跳过，主循环 _pass() 自己会正确
# 拦截它们，这里只负责聚合"已经合法解析成功"的提及各自申报的引文，不
# 越权判定某条提及是否成立），供调用方把同一场景的姐妹提及的引文一并
# 纳入锚点候选——不是编造，每一条都仍要经 _prep_pack_local_text_anchor
# 逐字核验，找不到照样判定没有本集依据。
def _prep_pack_group_scene_quotes_by_canonical(
    conn, project_id: str, episode_no: int, bible: Bible,
    scene_mentions: list[dict], scene_rename: dict[str, str],
) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for mention in scene_mentions:
        name = str(mention.get("display_name") or "").strip()
        quote = str(mention.get("quote") or "").strip()
        if not name or not quote:
            continue
        resolved_name = scene_rename.get(name, name)
        _scene_reference_id, canonical_name = (
            _prep_pack_resolve_scene_reference_with_alias(
                conn, project_id, episode_no, resolved_name, bible,
            )
        )
        quotes = grouped.setdefault(canonical_name, [])
        if quote not in quotes:
            quotes.append(quote)
    return grouped


