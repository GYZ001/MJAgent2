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
from app.project_settings import canvas_phrase
from app.refs import scene_visual_style_lock

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


def compose_seeded_prompt(*, visual_style: str, scene_name: str, aspect_ratio: str, draft: dict[str, str]) -> str:
    """带主视角图作种子的第一次生成：「编辑指令 + 左右对调清单」（B 沙箱 2026-09-27 实测最优）。

    不放主视角的场景描述——那段文字描述的是主视角构图，会把模型拉回原方向（实测
    带它的写法 0/4）。起草缺关键项时返回空串，调用方退回基础提示词。
    """
    required = ("invisible_elements", "back_wall_content", "left_becomes_right")
    if not all(draft.get(key) for key in required):
        return ""
    facing = draft.get("furniture_facing", "")
    facing_clause = f"画面里有明确朝向的家具需展示与参考图相反的那一面：{facing}。" if facing and facing != "无" else ""
    return (
        f"{scene_visual_style_lock(visual_style)}。这是一次基于参考图的机位镜像编辑任务，不是重新构图。"
        f"参考图是「{scene_name}」这个空间的建立镜头（主视角）。请把摄像机搬到参考图画面最深处一侧"
        "（例如画面里最远处的那扇门、那面墙或那个角落跟前），原地转身 180°，面朝参考图里原摄像机所在的方向"
        "拍摄同一个空间。新画面中，参考图里位于画面深处、正对原摄像机的以下内容必须完全从画面中消失，"
        f"因为它们现在在镜头身后：{draft['invisible_elements']}。新画面的背景应是参考图摄像机原本站立"
        f"那一侧的真实内容：{draft['back_wall_content']}。{facing_clause}"
        f"左右对调清单（必须遵守）：参考图画面左侧的这些内容——{draft['left_becomes_right']}——在新画面里"
        "应出现在画面右侧；参考图画面右侧的内容对应出现在新画面左侧；不得整张图直接左右镜像（背景元素仍须"
        f"移到镜头身后消失，不是简单翻转）。不出现人物，不出现文字、字幕、水印、logo。{canvas_phrase(aspect_ratio)}。"
    )


def compose_unseeded_prompt(*, visual_style: str, scene_name: str, aspect_ratio: str, draft: dict[str, str]) -> str:
    """判否后不带种子的第二次生成：只用起草的完整画面描述，不写「参考图」这类没有图时自相矛盾的措辞。"""
    view = draft.get("reverse_view", "")
    if not view:
        return ""
    return (
        f"{scene_visual_style_lock(visual_style)}。场景多视角定场图（反打）：「{scene_name}」这个空间从另一侧"
        f"看到的画面：{view}。画面中不出现任何人物，禁止文字、字幕、水印、logo。{canvas_phrase(aspect_ratio)}。"
    )


async def produce_reverse_angle_view(
    *, scene_canonical: str, visual_style: str, base_prompt: str, op_identity: str, establishing_image_path: str, aspect_ratio: str,
    scene_reference_id: str, scene_name: str, size: str, make_path: Callable[[], str],
    generate_image: GenerateImage, save_image_item: SaveImageItem, discard_path: Callable[[str], None],
    draft_fn: Callable[..., Awaitable[dict[str, str]]], judge_fn: JudgeFn,
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
    shape = dict(visual_style=visual_style, scene_name=scene_name, aspect_ratio=aspect_ratio, draft=draft or {})
    prompt = compose_seeded_prompt(**shape) or base_prompt
    common = dict(
        op_identity=op_identity, establishing_path=establishing_image_path,
        scene_reference_id=scene_reference_id, scene_name=scene_name, size=size, make_path=make_path,
        generate_image=generate_image, save_image_item=save_image_item, judge_fn=judge_fn,
    )
    path, verdict = await _attempt(seeded=True, prompt=prompt, **common)
    attempts = [{"seeded": True, "passed": verdict.get("passed"), "reason": verdict.get("reason", "")}]
    if verdict.get("checked") is True and verdict.get("passed") is False:
        discard_path(path)
        prompt = compose_unseeded_prompt(**shape) or base_prompt
        path, verdict = await _attempt(seeded=False, prompt=prompt, **common)
        attempts.append({"seeded": False, "passed": verdict.get("passed"), "reason": verdict.get("reason", "")})
    return {"image_path": path, "prompt": prompt, "qa": {"reverse_check": verdict, "attempts": attempts, "draft": draft}}
