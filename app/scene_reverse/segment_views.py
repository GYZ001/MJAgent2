"""分镜台 2.x → 视频请求：反打视角图的判定与装配辅助。

承载 ``app.production.storyboard_pack``（生成期：模型能不能看到这个选项、
正文点名是否核验通过）与 ``app.multiview``（装配期：真的把反打视角图塞进
参考图列表）两侧共用的具体查询/装配逻辑，两个调用方各自只留几行调用胶水
（见各自模块里的调用点注释）。只做纯函数与只读查询，不做任何写操作；判据
挂 ``app.scene_reverse.evidence.reverse_check_passed`` 的产物证据，不挂
``scene_reference_views.status``——两个不同调用点（生成期/装配期）共用同一个
私有查询函数 ``_ready_reverse_view``，避免各自实现出现分叉。
"""
from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from app.scene_reverse import evidence as reverse_evidence

_REVERSE_VIEW_SELECT = "SELECT * FROM scene_reference_views WHERE scene_reference_id=? AND view_role=?"


def _ready_reverse_view(conn: Any, scene_reference_id: str | None) -> dict[str, Any] | None:
    """这个场景此刻是否有一条带通过证据、文件确实存在的反打视角行；没有则 None。"""
    if not scene_reference_id:
        return None
    row = conn.execute(
        _REVERSE_VIEW_SELECT, (scene_reference_id, reverse_evidence.REVERSE_ANGLE_VIEW_ROLE),
    ).fetchone()
    if row is None:
        return None
    view = dict(row)
    if not reverse_evidence.reverse_check_passed(view):
        return None
    image_path = str(view.get("image_path") or "")
    return view if image_path and os.path.exists(image_path) else None


def annotate_manifest_scene(conn: Any, scene: dict, *, project_id: str | None, episode_no: int | None) -> None:
    """分镜生成期，给映射包里的一条场景原地写 ``reverse_angle_available``，可用时再写
    ``reverse_angle_view``（生成反打图时起草的「转过身看到的另一侧」描述）。

    只给一个布尔标记时，模型不知道反打画面里有什么、也就判断不了哪一镜该换——
    2026-09-27《顾念长安》第 2–10 集 42 次可用机会一次都没被点名。
    """
    view = _manifest_reverse_view(conn, scene, project_id=project_id, episode_no=episode_no)
    scene["reverse_angle_available"] = view is not None
    draft = reverse_evidence.reverse_view_draft(view) if view is not None else ""
    if draft:
        scene["reverse_angle_view"] = draft


def _manifest_reverse_view(
    conn: Any, scene: Mapping, *, project_id: str | None, episode_no: int | None,
) -> dict[str, Any] | None:
    if not reverse_evidence.reverse_angle_reference_enabled():
        return None
    ref_id = scene.get("scene_reference_id")
    if not ref_id and project_id and episode_no is not None:
        # 函数内导入：app.multiview 在模块级导入本模块，模块级反向导入会成环。
        from app.multiview import scene_row_for_episode

        row = scene_row_for_episode(project_id, str(scene.get("display_name") or ""), int(episode_no), conn=conn)
        ref_id = row["id"] if row else None
    return _ready_reverse_view(conn, ref_id)


def scene_reverse_angle_available_for_manifest(
    conn: Any, scene: Mapping, *, project_id: str | None, episode_no: int | None,
) -> bool:
    """分镜生成期，按映射包里的一条场景判定反打是否可用。

    映射包 ``asset_manifest.scenes[].scene_reference_id`` 在生产里通常为空（场景图在映射
    之后异步登记，2026-09-27《顾念长安》第 1 集 8 个场景全为 None），只认这个字段会让
    ``reverse_angle_available`` 恒为 false、反打永远用不上。缺失时按「场景名 + 集号」解析
    当集生效的场景记录——与装配期 ``_resolve_scene_entry`` 同一条规则，两边不会判出两个结果。
    """
    return _manifest_reverse_view(conn, scene, project_id=project_id, episode_no=episode_no) is not None


def scene_reverse_angle_available(conn: Any, scene_reference_id: str | None) -> bool:
    """分镜生成期：这个场景此刻是否有可用的反打视角图，写进
    ``relevant_assets.scenes[].reverse_angle_available`` 给模型看。总开关关闭时
    恒 False——模型看不到这个选项，不会写出没有对应图片的点名。
    """
    if not reverse_evidence.reverse_angle_reference_enabled():
        return False
    return _ready_reverse_view(conn, scene_reference_id) is not None


def reverse_mention_errors(prompt_text: str, relevant_scenes: Iterable[Mapping]) -> list[str]:
    """``_validate_segment_draft`` 阻断检查：正文里的 ``@名字·反打`` 必须对应本段
    ``relevant_assets.scenes`` 里 ``reverse_angle_available=True`` 的某个场景，
    合法名字集合完全来自本段数据，不维护任何名单。
    """
    ready_names = {
        str(scene.get("display_name") or "") for scene in relevant_scenes if scene.get("reverse_angle_available")
    }
    unmatched = reverse_evidence.unmatched_reverse_mentions(prompt_text, ready_names)
    if not unmatched:
        return []
    shown = "、".join(unmatched)
    return [
        f"prompt_text 里的反打点名 {shown} 不对应本段任何可用反打视角场景；合法名字只能取自 "
        "relevant_assets.scenes 里 reverse_angle_available=true 的 display_name，逐字沿用；"
        "这一镜若确实是主视角方向，直接写场景描述，不要加「·反打」后缀"
    ]


def mentioned_reverse_scene_names(
    prompt_text: str, scene_entries: Iterable[Mapping], *, display_name: Callable[[str], str],
) -> set[str]:
    """装配期：从已冻结持久化的 ``segment["prompt_text"]`` 里找出本段声明的场景中
    被 ``@场景名·反打`` 点到的那些（按 ``display_name(scene_id)`` 逐字匹配）。
    """
    names = {display_name(str(entry.get("scene_id") or "")) for entry in scene_entries}
    return reverse_evidence.mentioned_reverse_scenes(prompt_text, names)


def augment_scene_entry_with_reverse_angle(
    entry: dict[str, Any],
    *,
    conn: Any,
    scene_reference_id: str | None,
    scene_name: str,
    mentioned_scene_names: set[str],
    purposes: list[str],
) -> dict[str, Any]:
    """给 ``_resolve_scene_entry`` 已经算好的 establishing-only ``entry`` 追加
    反打视角（本段正文真的点了名、总开关开、且此刻确有可用反打图才追加）；
    没有命中时原样返回 ``entry``，不修改传入对象本身。
    """
    if scene_name not in mentioned_scene_names or not reverse_evidence.reverse_angle_reference_enabled():
        return entry
    view = _ready_reverse_view(conn, scene_reference_id)
    if view is None:
        return entry
    reverse_view = {
        "id": view.get("id"), "view_role": reverse_evidence.REVERSE_ANGLE_VIEW_ROLE,
        # input_fingerprint 必须是 scene_reference_views.input_fingerprint 列（真实内容
        # 哈希），不是 view.get("id")——那是这条视角行自己的主键，与 staleness 判据要比较
        # 的「视角表现状」概念不同，误用会让资产其实没变的镜头恒判 stale（2026-09-30
        # proj_ca86b15ab7d7 EP1 8 段反打误报同一根因的另一处）。
        "image_path": view.get("image_path"), "input_fingerprint": view.get("input_fingerprint"),
        "purposes": list(purposes),
    }
    return {
        **entry,
        "selected_view_ids": [*entry["selected_view_ids"], reverse_view["id"]],
        "selected_views": [*entry["selected_views"], reverse_view],
        "available_view_roles": [*entry["available_view_roles"], reverse_evidence.REVERSE_ANGLE_VIEW_ROLE],
    }


def scene_anchor_entity_name(scene_name: str | None, view_role: str | None) -> str:
    """``library_anchor_assets_from_manifest`` 场景锚点的 ``entity_name``：反打
    视角加「·反打」后缀（与 ``evidence.REVERSE_MENTION_SUFFIX`` 同一约定），
    供 ``@场景名·反打`` → ``@图片N`` 的点名替换、以及同场景多视角用途文案区分。
    """
    name = scene_name or ""
    if view_role == reverse_evidence.REVERSE_ANGLE_VIEW_ROLE:
        return f"{name}{reverse_evidence.REVERSE_MENTION_SUFFIX}"
    return name
