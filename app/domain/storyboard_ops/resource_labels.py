"""分镜台/生成台展示用：给段落资源清单里的人物/场景条目挂人类可读的名字。

段落资源清单（``storyboard_pack_segment.resources``）只存 ``identity_id`` /
``scene_id`` 这类内部键——具名角色是 ``bible:孟浩`` 还能看，群演是
``entity:ee1fb41c79e4e33d``（source_label 的 sha256 前 16 位，见
app.identity_authority.visual_entity_id_for_resolution），界面上就是一串哈希，
用户实测反馈"群演名字都没有，就一个 id"（2026-09-01）。

可读名字的真源是本集映射包 ``asset_manifest``：具名角色有 display_appellation /
display_name，群演有 ``functional_extras[].label`` + ``visual_entity_id``。这里
按 id 反查、就地挂上 ``display_name``，查不到就不挂——由前端显示中性占位并把
原始 id 放进 title，绝不按哈希编一个名字出来。

注意：生成台（view=wall）的投影里没有 prep_pack 字段（只有 script/board 有），
所以这里自己按 episode_id 读一次映射包，而不是读 detail["prep_pack"]——否则
生成台恒查不到，正是用户看到哈希的那一页。

提示词侧不受影响：发给视频供应商的 ``prompt_text`` 从来只有自然语言
（"@孟浩，十六七岁少年…"、"八九岁虎头虎脑的少年…"），没有任何 id。
"""
from __future__ import annotations

from typing import Any

from app.db import get_conn
from app.domain.common import episode_prep_pack_payload


def _put(names: dict[str, str], key: Any, value: Any) -> None:
    key_text = str(key or "").strip()
    value_text = str(value or "").strip()
    if key_text and value_text:
        names.setdefault(key_text, value_text)


def _display_names(episode_id: str) -> dict[str, str]:
    row = get_conn().execute(
        "SELECT screenplay_json FROM episodes WHERE id=?", (episode_id,),
    ).fetchone()
    pack = episode_prep_pack_payload(row) if row else None
    manifest = (pack or {}).get("asset_manifest") or {}
    names: dict[str, str] = {}
    for character in manifest.get("characters") or []:
        if not isinstance(character, dict):
            continue
        # 本集称谓优先于全局正名：段落清单描述的是"这一集这个人怎么被叫"，
        # 与映射台人物卡的展示口径一致（display_appellation 先于 display_name）。
        label = character.get("display_appellation") or character.get("display_name")
        _put(names, character.get("identity_id"), label)
        _put(names, character.get("visual_entity_id"), label)
    for extra in manifest.get("functional_extras") or []:
        if isinstance(extra, dict):
            _put(names, extra.get("visual_entity_id"), extra.get("label"))
    for scene in manifest.get("scenes") or []:
        if isinstance(scene, dict):
            _put(names, scene.get("scene_id"), scene.get("display_name"))
    return names


def attach_resource_display_names(detail: dict[str, Any], view: str | None) -> None:
    """就地给 detail["shots"] 的段落资源条目加 display_name；查不到就不加。"""
    del view  # shots 是否存在已由 _episode_detail_projection 按 view 决定
    shots = detail.get("shots") or []
    episode_id = str(detail.get("id") or "")
    if not shots or not episode_id:
        return
    names = _display_names(episode_id)
    if not names:
        return
    for shot in shots:
        resources = ((shot or {}).get("storyboard_pack_segment") or {}).get("resources") or {}
        for character in resources.get("characters") or []:
            if isinstance(character, dict):
                _put_display(character, names, character.get("identity_id"))
        for scene in resources.get("scenes") or []:
            if isinstance(scene, dict):
                _put_display(scene, names, scene.get("scene_id"))


def _put_display(entry: dict[str, Any], names: dict[str, str], key: Any) -> None:
    name = names.get(str(key or "").strip())
    if name:
        entry["display_name"] = name
