"""映射台/分镜台展示用：给道具资产条目挂上「当前实际会用的那张」定物图。

与同目录 current_scene_refs.py 同形状、同理由：展示按道具名实时解析物件库里
现在真会用的那张图，不改写已发布产物、不写回数据库，只往运行期 dict 上加同级新键。
2026-09-05 用户在分镜台看到「葫芦」卡是占位图，而物件库里第 1 集的定物图早已
生成——前端注释还写着「道具没有世界书图像素材库（设计使然）」，那是 2026-09-04
物件库落地前的旧事实。
解析走 app.props.store.prop_reference_for_episode，与生成侧挑道具参考图用的
是同一条区间查询。
"""
from __future__ import annotations

from typing import Any

from app.db import get_conn
from app.domain.common import _media_url
from app.props.store import prop_reference_for_episode

_MISSING: dict[str, str | None] = {
    "current_prop_reference_id": None,
    "current_prop_image_url": None,
}


def attach_current_prop_references(detail: dict[str, Any], view: str | None) -> None:
    """就地给 detail["prep_pack"] 与 detail["shots"] 里的道具条目加
    current_prop_reference_id / current_prop_image_url。解析不到、或图虽登记但文件
    已不在盘上（``_media_url`` 返回 None）时两个字段都是 None，前端照实显示占位。"""
    del view
    project_id = detail.get("project_id")
    episode_no = detail.get("episode_no")
    if not project_id:
        return
    conn = get_conn()
    cache: dict[str, dict[str, str | None]] = {}

    def resolved(label: str) -> dict[str, str | None]:
        if label in cache:
            return cache[label]
        row = prop_reference_for_episode(conn, project_id, label, episode_no) if label else None
        image_url = _media_url(row["image_path"]) if row and row["image_path"] else None
        value = (
            {"current_prop_reference_id": str(row["id"]), "current_prop_image_url": image_url}
            if image_url
            else dict(_MISSING)
        )
        cache[label] = value
        return value

    prep_pack = detail.get("prep_pack")
    if isinstance(prep_pack, dict):
        for prop in ((prep_pack.get("asset_manifest") or {}).get("props")) or []:
            if isinstance(prop, dict):
                prop.update(resolved(str(prop.get("label") or "").strip()))
    for shot in detail.get("shots") or []:
        resources = ((shot or {}).get("storyboard_pack_segment") or {}).get("resources") or {}
        for prop in resources.get("props") or []:
            if isinstance(prop, dict):
                prop.update(resolved(str(prop.get("label") or "").strip()))
