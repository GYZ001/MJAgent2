"""映射台/分镜台/生成台展示用：给每个人物谱身份的角色资产条目挂上「当前实际
会用的那张」定妆照，供前端渲染缩略图、并在快照与当前不同时提示"已更新"。

挂不挂这两个字段只看 identity_id 是不是人物谱身份（``bible:`` 前缀），**不看
产物里固化的 portrait_id 快照**。旧版以 ``if character.get("portrait_id")`` 为
门槛，在出图解耦到后台之后必然失效：映射跑完那一刻角色卡刚建、图还没出，
快照恒为 null，于是这三个角色的缩略图永远停在"定妆照待生成"——连硬刷新都救
不回来，因为门槛卡在展示层而不是数据本身。生成侧同一处病灶已由
app.multiview._storyboard_pack_asset_dependencies 修掉（asset_required 改挂
人物谱的卡、不挂快照），展示侧这里是同一个修法。

不改写已发布产物：episode_prep_pack_payload() 每次调用都从 screenplay_json
反序列化出一份新 dict，shots 同理来自 SELECT * 的行拷贝——本模块只往这些
运行期字典上加同级新键，从不覆盖 portrait_id 本身，也从不写回数据库，落盘
的 prep_pack/storyboard_pack 产物字节不变（可复现性不受影响）。

解析逻辑本身不在这里：只调用 app.portraits.current_portrait_ref，与生成时
app.media_exec/app.production.storyboard_pack 用的是同一份选段判据
（app.portraits.current_ref._current_portrait_row），不允许在这里另写一条
相似查询。
"""
from __future__ import annotations

from typing import Any

from app.domain.common import _media_url
from app.portraits import current_portrait_ref

_IDENTITY_PREFIX = "bible:"


def _character_name_from_identity(identity_id: str) -> str | None:
    """已绑定 portrait_id 的角色条目，identity_id 恒为 ``f"bible:{name}"``
    （见 app.production.prep_pack.resolve_assets._resolve_assets 与
    app.portraits.portrait_io.bible_for_episode 同一构造），据此反推角色名。
    """
    if not identity_id.startswith(_IDENTITY_PREFIX):
        return None
    name = identity_id[len(_IDENTITY_PREFIX):]
    return name or None


def _is_bible_character(entry: Any) -> bool:
    """这个资产条目要不要挂当前定妆照：只看 identity_id 是不是人物谱身份。"""
    return isinstance(entry, dict) and bool(
        _character_name_from_identity(str(entry.get("identity_id") or "")),
    )


def attach_current_character_portraits(detail: dict[str, Any], view: str | None) -> None:
    """就地给 detail["prep_pack"] 与 detail["shots"] 里的角色资产条目加
    current_portrait_id / current_portrait_image_url。未命中时两个字段都是
    None——调用方（前端）必须原样显示"无定妆照"，不得回退到 portrait_id
    快照对应的图。群演/未收录称谓（identity_id 不带 bible: 前缀）连查都不查，
    两个字段一个都不加：它们没有定妆照是设计使然，不是"当前解析不到"。
    """
    del view  # 两个来源字段是否存在已经由 _episode_detail_projection 按 view 决定
    project_id = detail.get("project_id")
    episode_no = detail.get("episode_no")
    if not project_id:
        return
    resolved_cache: dict[str, dict[str, str | None]] = {}

    def resolved(identity_id: str | None) -> dict[str, str | None]:
        key = identity_id or ""
        if key in resolved_cache:
            return resolved_cache[key]
        name = _character_name_from_identity(key) if key else None
        current = (
            current_portrait_ref(project_id, name, episode_no, visual_entity_id=key)
            if name else None
        )
        value = (
            {
                "current_portrait_id": current["portrait_id"],
                "current_portrait_image_url": _media_url(current["image_path"]),
            }
            if current
            else {"current_portrait_id": None, "current_portrait_image_url": None}
        )
        resolved_cache[key] = value
        return value

    prep_pack = detail.get("prep_pack")
    if isinstance(prep_pack, dict):
        characters = ((prep_pack.get("asset_manifest") or {}).get("characters")) or []
        for character in characters:
            if _is_bible_character(character):
                character.update(resolved(character.get("identity_id")))

    for shot in detail.get("shots") or []:
        segment = (shot or {}).get("storyboard_pack_segment") or {}
        resources = segment.get("resources") or {}
        for character in resources.get("characters") or []:
            if _is_bible_character(character):
                character.update(resolved(character.get("identity_id")))
