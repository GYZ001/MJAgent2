"""人物视角（front_full 之外）图片产出的决策：face_closeup 走纯像素裁切，其它
视角仍走常规图生图。从 app.multiview 拆出——该文件在 app/FILE_CONVENTIONS.toml
的 line_count 棘轮基线上已经顶格（见该文件头部历次搬移先例，如
app.video_modes.character_look_selection 同一惯例），新增这段决策逻辑没有行数
空间可用。

``generate_image``/``save_image_item``/``operation_id`` 由调用方注入，而不是在
这里反向 import app.multiview 的私有函数：与
``app.scene_reverse.produce.produce_reverse_angle_view`` 同一取舍，避免这个新
模块反过来依赖 multiview 的内部实现（也会形成循环 import——multiview 要在模块
级 import 本模块来拿 crop_headshot_from_portrait 的落点）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable

from app import hiagent
from app.portraits.headshot_crop import crop_headshot_from_portrait


async def produce_character_side_view_image(
    view_role: str, prompt: str, *, path: str,
    front_image_path: str | None, base_image_path: str | None,
    character_name: str, portrait_id: str, fp: str,
    generate_image: Callable[..., Awaitable[dict[str, Any]]],
    save_image_item: Callable[[dict[str, Any], str], Awaitable[None]],
    operation_id: Callable[..., str],
) -> dict[str, Any] | None:
    """把图片落到 ``path``；返回 qa（face_closeup 裁切带几何/溯源信号，其它
    视角恒为 None——技术产物存在即 ready，不跑 VLM 评审）。"""
    if view_role == "face_closeup":
        if not front_image_path or not Path(front_image_path).exists():
            raise hiagent.ProviderError("头像照需要先有可用的全身定妆照（front_full）才能裁切")
        return await crop_headshot_from_portrait(
            front_image_path, dest_path=path,
            call_meta={"asset_kind": "character_view_headshot_crop", "character_name": character_name},
        )
    seeds = [hiagent.data_url_from_file(front_image_path)] if front_image_path and Path(front_image_path).exists() else []
    if base_image_path and Path(base_image_path).exists():
        seeds.append(hiagent.data_url_from_file(base_image_path))
    item = await generate_image(
        prompt, seed_inputs=seeds or None,
        call_meta={
            "asset_kind": "character_view", "view_role": view_role, "character_name": character_name,
            "operation_id": operation_id(
                asset_kind="character_view", view_role=view_role, prompt=prompt,
                seed_inputs=seeds, fallback_identity=f"{portrait_id}:{fp}",
            ),
            "reuse_successful_operation": True,
        },
    )
    await save_image_item(item, path)
    return None
