"""视频生成命令发起前的只读闸门合并入口。

``app.capabilities.handlers.video`` 的 ``generate_episode``/``generate_shot``
两个入口都要依次核验"场景状态图"（``app.video_modes.scene_state_ensure``）与
"道具补卡"（``app.props.card_pending_ensure``）两道闸门——合并成一次调用，
避免同一段 import + 调用 + 判断样板在 ``video.py`` 里重复两次，把那个文件推
过 ``app/FILE_CONVENTIONS.toml`` 的行数/单函数行数基线（CLAUDE.md「红线只降
不升」：已有基线不许因为新增代码被动调大，要么不新增要么挪地方装）。
"""
from __future__ import annotations


async def pending_video_dispatch_gate(
    project_id: str, episode_id: str, shot_ids: list[str] | None,
) -> tuple[str, str] | None:
    """依次核验场景状态图、道具补卡两道闸门；命中任意一道就返回
    ``(message, error_code)`` 供调用方直接 ``failed(*gate)``，两道都不拦
    返回 ``None``。"""
    # 函数内导入：避免本模块加载期连带初始化 app.video_modes/app.props 整条链
    # （同款理由见 app.capabilities.handlers.video 既有同类注释）。
    from app.video_modes.scene_state_ensure import pending_scene_state_gate
    from app.props.card_pending_ensure import pending_prop_card_gate  # 函数内导入：同上

    scene_message = await pending_scene_state_gate(project_id, episode_id, shot_ids)
    if scene_message is not None:
        return scene_message, "scene_state_pending"
    prop_message = await pending_prop_card_gate(project_id, episode_id, shot_ids)
    if prop_message is not None:
        return prop_message, "prop_card_pending"
    return None
