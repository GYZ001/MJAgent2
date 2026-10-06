"""装配期：把场景状态图接进 ``app.multiview._storyboard_pack_asset_dependencies``
的场景条目解析（从那里搬出的理由同 ``scene_state_selection``——该文件已逼近
``app/FILE_CONVENTIONS.toml`` 的行数棘轮基线，零余量）。

``resolve_scene_entry_with_state`` 是 ``app.multiview._resolve_scene_entry``
实际留在 multiview.py 里的唯一一次函数调用：先用既有的 ``resolve_scene_
reference_entry`` 算出 establishing-only 条目，若它判定省略（``scene_state_
matches_card`` 为 no/unsure）且本段所属状态串恰好有 ready 且指纹匹配的状态图，
就地换成这张状态图（``scene_revision_id`` 仍是场景卡 id 不变，``primary_usable``
等字段与正常发送一致）；否则保持原有省略行为，再按原逻辑决定是否叠加反打视角。
状态图生效时不再叠加反打——反打图是"默认状态"的另一侧视角，这里已经不是默认
状态，叠加反打只会把两种互相矛盾的状态糊在一起发给模型。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.scene_reverse.segment_views import augment_scene_entry_with_reverse_angle, mentioned_reverse_scene_names
from app.video_modes.scene_state_prop_states import character_display_names_from_bible
from app.video_modes.scene_state_selection import resolve_scene_reference_entry
from app.video_modes.scene_state_views import resolve_scene_state_view_for_shot


def _state_override(state_row: dict[str, Any], entry: dict[str, Any], purposes: list[str]) -> dict[str, Any]:
    selected_view = {
        "id": state_row["id"], "view_role": "scene_state", "image_path": state_row["image_path"],
        "input_fingerprint": state_row["input_fingerprint"], "purposes": list(purposes),
    }
    return {
        **entry,
        "pack_status": "ready", "asset_usable": True, "pack_usable": True, "primary_usable": True,
        "selected_view_ids": [state_row["id"]], "selected_views": [selected_view],
        "available_view_roles": ["scene_state"], "missing_required": [], "scene_state_omitted_reason": None,
    }


def resolve_scene_entry_with_state(
    *, conn: Any, shot_id: str, scene_name: str, has_card: bool, scene_reference_id: str | None,
    image_path: str, bible: Any, scene_state_matches_card: str, purposes: list[str],
    prompt_text: str, scene_entries: list[dict[str, Any]], display_name: Callable[[str], str],
) -> dict[str, Any]:
    entry = resolve_scene_reference_entry(
        scene_name=scene_name, has_card=has_card, scene_reference_id=scene_reference_id,
        image_path=image_path, scene_state_matches_card=scene_state_matches_card, purposes=purposes,
    )
    state_row = None
    if entry["scene_state_omitted_reason"] and scene_reference_id:
        shot_row = conn.execute("SELECT episode_id, shot_no FROM shots WHERE id=?", (shot_id,)).fetchone()
        if shot_row is not None:
            state_row = resolve_scene_state_view_for_shot(
                conn=conn, episode_id=shot_row["episode_id"], shot_no=int(shot_row["shot_no"]),
                scene_reference_id=scene_reference_id, establishing_image_path=image_path,
                visual_style=bible.world.visual_style_canonical, props=bible.props,
                character_display_names=character_display_names_from_bible(bible),
            )
    if state_row is not None:
        return _state_override(state_row, entry, purposes)
    mentioned = mentioned_reverse_scene_names(prompt_text, scene_entries, display_name=display_name)
    return augment_scene_entry_with_reverse_angle(
        entry, conn=conn,
        scene_reference_id=(None if entry["scene_state_omitted_reason"] else entry["scene_revision_id"]),
        scene_name=scene_name, purposes=purposes, mentioned_scene_names=mentioned,
    )
