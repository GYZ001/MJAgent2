"""映射台预检的出图范围预估：只对结构上可判定的部分给出确切数字。

背景（2026-08-31）：出图入口全部收敛到映射台之后，`_discover_new_characters` /
`_discover_new_scenes`（app/production/prep_pack/discovery.py 106/151 行）一旦
把人物或场景映射进本集就会自动出图——这是既定行为，不是可选步骤（已有参考图的
走复用、不重复出图）。本模块回答"这次映射预检时，有多少张图是结构上已经能算准
的"：人物谱/场景库里已登记、本集原文逐字命中、但还没有覆盖本集参考图的条目——
按上面那条既定行为，这些一旦被映射进本集就会真出图，数量可以算准。本集真正
新出现、尚未进人物谱/场景库的角色/场景，具体数量取决于模型读完原文报出什么，
生成前结构上无法确知，本模块不猜、如实交给调用方标记为不可预知
（CLAUDE.md「不得兜底填充」「诚实的不确定优于精确的假数字」）。
"""
from __future__ import annotations

from app.schemas import Bible


def _prep_pack_literal_matches(
    entries: list[tuple[str, list[str]]], source_text: str,
) -> set[str]:
    """entries 是 (规范名, 别名列表) 的清单；返回规范名或任一别名在 source_text
    里逐字命中的规范名集合。判据形状与 app/production/prep_pack/chunking.py 的
    ``_prep_pack_character_shortlist``（逐字命中过滤，零语义、不做模糊匹配）一致，
    但不导入该文件的私有实现——避免本包跨包耦合到映射台内部结构，这里只是
    独立重实现同一个纯函数判据。"""
    return {
        name for name, aliases in entries
        if name and any(form and form in source_text for form in (name, *aliases))
    }


def _prep_pack_pending_characters(
    conn, *, project_id: str, episode_no: int, source_text: str, bible: Bible,
) -> list[str]:
    """人物谱里本集原文逐字命中、但 character_portraits 里还没有覆盖本集记录
    的规范名——按既定行为这些一旦被映射进本集就会真出定妆照。"""
    entries = [
        (c.name, [a.text for a in (c.aliases or [])])
        for c in bible.characters if c.name
    ]
    shortlist = _prep_pack_literal_matches(entries, source_text)
    if not shortlist:
        return []
    imaged = {
        str(row["character_name"]) for row in conn.execute(
            "SELECT DISTINCT character_name FROM character_portraits "
            "WHERE project_id=? AND ep_start<=? AND (ep_end IS NULL OR ep_end>=?)",
            (project_id, episode_no, episode_no),
        ).fetchall()
    }
    return sorted(shortlist - imaged)


def _prep_pack_pending_scenes(
    conn, *, project_id: str, episode_no: int, source_text: str, bible: Bible,
) -> list[str]:
    """场景库里本集原文逐字命中、但 scene_references 里还没有覆盖本集记录的
    规范名——按既定行为这些一旦被映射进本集就会真出场景参考图。"""
    entries = [(s.name, list(s.aliases or [])) for s in bible.scenes if s.name]
    shortlist = _prep_pack_literal_matches(entries, source_text)
    if not shortlist:
        return []
    imaged = {
        str(row["scene_name"]) for row in conn.execute(
            "SELECT DISTINCT scene_name FROM scene_references "
            "WHERE project_id=? AND ep_start<=? AND (ep_end IS NULL OR ep_end>=?)",
            (project_id, episode_no, episode_no),
        ).fetchall()
    }
    return sorted(shortlist - imaged)


def _prep_pack_known_pending_images(
    conn, *, project_id: str, episode_no: int, source_text: str, bible: Bible,
) -> dict:
    """本集"已知会出图"的确切部分：人物谱/场景库里已登记、本集原文逐字命中、
    但还没有覆盖本集参考图的条目——按既定行为这些一旦被映射进本集就会触发真实
    出图，所以这部分数量是可以算准的。已有参考图的条目走复用，不重复出图，不
    计入这里的数量。本集真正新角色/新场景的数量在生成前结构上无法确知（取决于
    模型读完原文实际报出什么），不在这里估算——``estimated_images`` 恒为
    None，调用方据此判断"完整范围"这部分是否可信，不得当作 0 或任何默认值使用。
    """
    from app.multiview import CHARACTER_REQUIRED_VIEWS, SCENE_REQUIRED_VIEWS

    kwargs = dict(
        conn=conn, project_id=project_id, episode_no=episode_no,
        source_text=source_text, bible=bible,
    )
    pending_characters = _prep_pack_pending_characters(**kwargs)
    pending_scenes = _prep_pack_pending_scenes(**kwargs)

    views_per_character = len(CHARACTER_REQUIRED_VIEWS)
    views_per_scene = len(SCENE_REQUIRED_VIEWS)
    known_image_count = (
        len(pending_characters) * views_per_character
        + len(pending_scenes) * views_per_scene
    )
    return {
        "deferred": False,
        "views_per_character": views_per_character,
        "views_per_scene": views_per_scene,
        "known_pending_characters": pending_characters,
        "known_pending_scenes": pending_scenes,
        "known_image_count": known_image_count,
        "estimated_images": None,
        "note": (
            "已登记角色/场景中本集原文命中且缺参考图的部分按既定行为必然出图，"
            "已算入 known_image_count；本集若出现尚未登记的新角色/新场景，映射台会"
            "自动建卡/登记并生成参考图，这是既定行为、不是可选步骤——具体新增"
            "数量在生成前无法确知，取决于模型读完原文实际报出什么，完整范围以"
            "生成后为准"
        ),
    }
