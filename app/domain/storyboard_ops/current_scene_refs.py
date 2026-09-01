"""映射台/分镜台/生成台展示用：给场景资产条目挂上「当前实际会用的那张」场景图。

与同目录 current_portraits.py 是同一形状、同一理由的对称件（用户拍板
2026-08-30：展示按身份/场景名实时解析出"现在真会用的那张"，产物里固化的
scene_reference_id 快照只做溯源，不当作当前状态渲染）。场景侧此前根本没有这
一步，前端只能拿快照 id 去人物谱/场景库里查图——而出图解耦到后台之后，映射
跑完那一刻场景卡刚建、图还没出，快照恒为 null，于是场景缩略图永远停在"场景
图待生成"，硬刷新也救不回来。

解析逻辑本身不在这里：只调用 app.multiview.scene_row_for_episode，与生成侧
app.multiview._storyboard_pack_asset_dependencies 挑场景参考图用的是同一个
函数，不在这里另写一条相似查询。

不改写已发布产物：只往运行期 dict（prep_pack 反序列化出的新 dict、shots 的行
拷贝）上加同级新键，从不覆盖 scene_reference_id 本身，也从不写回数据库。
"""
from __future__ import annotations

from typing import Any

from app.domain.common import _media_url
from app.multiview import scene_row_for_episode

_SCENE_PREFIX = "scene:"

_MISSING: dict[str, str | None] = {
    "current_scene_reference_id": None,
    "current_scene_image_url": None,
}


def _scene_name_from_scene_id(scene_id: str) -> str | None:
    """resources.scenes[]/asset_manifest.scenes[] 的 scene_id 恒为
    ``f"scene:{name}"``（app.production.prep_pack.resolve_assets 装配处），
    据此反推场景名；前缀不匹配（含空串）返回 None，不猜。
    """
    if not scene_id.startswith(_SCENE_PREFIX):
        return None
    name = scene_id[len(_SCENE_PREFIX):]
    return name or None


def attach_current_scene_references(detail: dict[str, Any], view: str | None) -> None:
    """就地给 detail["prep_pack"] 与 detail["shots"] 里的场景资产条目加
    current_scene_reference_id / current_scene_image_url。解析不到、或图虽然
    登记了但文件已不在盘上（``_media_url`` 返回 None 的既有语义）时两个字段
    都是 None——调用方必须原样显示"没有场景图"，不得回退到 scene_reference_id
    快照对应的图，也不得只给 id 让前端误以为有图。
    """
    del view  # 两个来源字段是否存在已经由 _episode_detail_projection 按 view 决定
    project_id = detail.get("project_id")
    episode_no = detail.get("episode_no")
    if not project_id:
        return
    resolved_cache: dict[str, dict[str, str | None]] = {}

    def resolved(scene_id: str | None) -> dict[str, str | None]:
        key = scene_id or ""
        if key in resolved_cache:
            return resolved_cache[key]
        name = _scene_name_from_scene_id(key)
        row = scene_row_for_episode(project_id, name, episode_no) if name else None
        image_url = _media_url(row["image_path"]) if row else None
        value = (
            {
                "current_scene_reference_id": str(row["id"]),
                "current_scene_image_url": image_url,
            }
            if image_url
            else dict(_MISSING)
        )
        resolved_cache[key] = value
        return value

    prep_pack = detail.get("prep_pack")
    if isinstance(prep_pack, dict):
        scenes = ((prep_pack.get("asset_manifest") or {}).get("scenes")) or []
        for scene in scenes:
            if isinstance(scene, dict):
                scene.update(resolved(str(scene.get("scene_id") or "")))

    for shot in detail.get("shots") or []:
        segment = (shot or {}).get("storyboard_pack_segment") or {}
        resources = segment.get("resources") or {}
        for scene in resources.get("scenes") or []:
            if isinstance(scene, dict):
                scene.update(resolved(str(scene.get("scene_id") or "")))
