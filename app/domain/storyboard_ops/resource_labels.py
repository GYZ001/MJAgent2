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

WS12（群演描述性措辞归并）：``resources.characters[].identity_id`` 直接命中
不了 ``names`` 时（模型没有逐字复制 functional_extras 的 visual_entity_id，
写的是一段描述性文字），在放弃之前再试一次结构性归并——判据与持久化侧
``app.production.storyboard_extras_reconcile`` 完全同一份（段落范围重叠 +
label 逐字互为子串，两者缺一不可，多候选不猜），这里额外覆盖两类持久化侧
够不到的情形：① 归并侧的 ``resolve_persisted_character_ids`` 只改写投影到
``shots.characters`` 那份派生列表，不回写 ``resources.characters[]`` 本身
（后者是模型自己的结构化自报，持久化时刻意保持原样，见
``persist_storyboard_pack`` 调用点注释）；② 已经落库的历史分集，重新跑一遍
归并只需要读，不需要重新持久化。

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
from app.production.storyboard_extras_reconcile import reconcile_descriptive_extra


def _put(names: dict[str, str], key: Any, value: Any) -> None:
    key_text = str(key or "").strip()
    value_text = str(value or "").strip()
    if key_text and value_text:
        names.setdefault(key_text, value_text)


def _display_names_and_extras(episode_id: str) -> tuple[dict[str, str], list[dict[str, Any]]]:
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
    functional_extras = [e for e in (manifest.get("functional_extras") or []) if isinstance(e, dict)]
    for extra in functional_extras:
        _put(names, extra.get("visual_entity_id"), extra.get("label"))
    for scene in manifest.get("scenes") or []:
        if isinstance(scene, dict):
            _put(names, scene.get("scene_id"), scene.get("display_name"))
    return names, functional_extras


def attach_resource_display_names(detail: dict[str, Any], view: str | None) -> None:
    """就地给 detail["shots"] 的段落资源条目加 display_name；查不到就不加。"""
    del view  # shots 是否存在已由 _episode_detail_projection 按 view 决定
    shots = detail.get("shots") or []
    episode_id = str(detail.get("id") or "")
    if not shots or not episode_id:
        return
    names, functional_extras = _display_names_and_extras(episode_id)
    if not names:
        return
    for shot in shots:
        segment = ((shot or {}).get("storyboard_pack_segment")) or {}
        resources = segment.get("resources") or {}
        segment_source_indexes = segment.get("source_segment_indexes") or []
        for character in resources.get("characters") or []:
            if isinstance(character, dict):
                _put_character_display(
                    character, names, functional_extras, segment_source_indexes,
                )
        for scene in resources.get("scenes") or []:
            if isinstance(scene, dict):
                _put_display(scene, names, scene.get("scene_id"))


def _put_display(entry: dict[str, Any], names: dict[str, str], key: Any) -> None:
    name = names.get(str(key or "").strip())
    if name:
        entry["display_name"] = name


def _put_character_display(
    entry: dict[str, Any], names: dict[str, str],
    functional_extras: list[dict[str, Any]], segment_source_indexes: list[int],
) -> None:
    identity_id = str(entry.get("identity_id") or "").strip()
    name = names.get(identity_id)
    if not name and functional_extras:
        merge = reconcile_descriptive_extra(identity_id, segment_source_indexes, functional_extras)
        if merge.merged:
            name = names.get(merge.resolved_id)
    if name:
        entry["display_name"] = name
