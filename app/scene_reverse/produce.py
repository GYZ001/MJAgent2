"""场景反打图生成策略：先起草再生成、生成后判定，判否再补一次不带种子的生成。

只负责「怎么产出一张反打候选图」，不碰数据库/文件路径规则——落盘路径、图像生成、
判定这三类协作者全部按参数注入（``make_path``/``generate_image``/``save_image_item``/
``draft_fn``/``judge_fn``），本模块不 import ``app.multiview``，避免包间循环依赖；
调用方把自己的 ``_generate_image``/``_save_image_item``/``draft_behind_camera_note``/
``judge_reverse_angle`` 按裸名字传进来，测试对调用方打桩时才能透传到这里
（CLAUDE.md「拆包静默废掉 monkeypatch」）。

幂等边界：起草文本与最终提示词只体现在返回值的 ``prompt``/``qa`` 里，落库指纹
必须由调用方按不含起草文本的稳定输入单独计算——本模块不参与、也不应该参与
指纹计算。
"""
from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app import hiagent

GenerateImage = Callable[..., Awaitable[dict[str, Any]]]
SaveImageItem = Callable[[dict[str, Any], str], Awaitable[None]]
JudgeFn = Callable[..., Awaitable[dict[str, Any]]]


async def _attempt(
    *, seeded: bool, prompt: str, op_identity: str, establishing_path: str, scene_reference_id: str, scene_name: str,
    size: str, make_path: Callable[[], str], generate_image: GenerateImage, save_image_item: SaveImageItem,
    judge_fn: JudgeFn,
) -> tuple[str, dict[str, Any]]:
    """一次生成 + 落盘 + 判定；``seeded`` 为真且主视角图存在时带它作种子。

    去重用的 operation_id 按调用方给的 ``op_identity`` 计算：它必须覆盖全部真实
    输入（含主视角图，调用方传落库指纹），又不能含起草文本——``draft_fn`` 温度
    非零，把起草文本算进去会让异常重试命中不上 hiagent 侧已有的成功记录、重复
    付费；只按提示词算又会在主视角图换了、或人工要求重做时复用旧图。
    """
    seed_url = (
        hiagent.data_url_from_file(establishing_path)
        if seeded and establishing_path and Path(establishing_path).exists() else None
    )
    op = hashlib.sha256(f"{scene_reference_id}:reverse:{seeded}:{op_identity}".encode("utf-8")).hexdigest()[:32]
    path = make_path()
    item = await generate_image(
        prompt, seed_inputs=[seed_url] if seed_url else None, size=size,
        call_meta={
            "asset_kind": "scene_view", "view_role": "reverse_angle", "scene_name": scene_name,
            "operation_id": "op_scene_view_" + op, "reuse_successful_operation": True,
        },
    )
    await save_image_item(item, path)
    verdict = await judge_fn(
        establishing_path=establishing_path, candidate_path=path,
        call_meta={"scene_reference_id": scene_reference_id, "scene_name": scene_name},
    )
    return path, verdict


async def produce_reverse_angle_view(
    *, scene_canonical: str, visual_style: str, base_prompt: str, op_identity: str, establishing_image_path: str,
    scene_reference_id: str, scene_name: str, size: str, make_path: Callable[[], str],
    generate_image: GenerateImage, save_image_item: SaveImageItem, discard_path: Callable[[str], None],
    draft_fn: Callable[..., Awaitable[str]], judge_fn: JudgeFn,
) -> dict[str, Any]:
    """返回 ``{"image_path", "prompt", "qa": {"reverse_check", "attempts", "draft"}}``。

    第一次生成带主视角图作种子（构图服从起草描述）；判定明确不通过（``checked``
    且 ``passed`` 为 False）才补第二次纯文生图；判定调用失败（``checked`` 为 False）
    不重试——与 ``app.media_exec.subtitle_gate``「未判定放行」同一取舍。最多两次生成。
    ``op_identity``：自动补包传落库指纹（输入不变才复用），人工重做传每次不同的指纹。
    """
    draft = await draft_fn(
        scene_canonical=scene_canonical, visual_style=visual_style,
        establishing_image_path=establishing_image_path, scene_reference_id=scene_reference_id,
    )
    prompt = f"{base_prompt}原机位背后一侧的真实内容：{draft}。" if draft else base_prompt
    common = dict(
        prompt=prompt, op_identity=op_identity, establishing_path=establishing_image_path,
        scene_reference_id=scene_reference_id, scene_name=scene_name, size=size, make_path=make_path,
        generate_image=generate_image, save_image_item=save_image_item, judge_fn=judge_fn,
    )
    path, verdict = await _attempt(seeded=True, **common)
    attempts = [{"seeded": True, "passed": verdict.get("passed"), "reason": verdict.get("reason", "")}]
    if verdict.get("checked") is True and verdict.get("passed") is False:
        discard_path(path)
        path, verdict = await _attempt(seeded=False, **common)
        attempts.append({"seeded": False, "passed": verdict.get("passed"), "reason": verdict.get("reason", "")})
    return {"image_path": path, "prompt": prompt, "qa": {"reverse_check": verdict, "attempts": attempts, "draft": draft}}
