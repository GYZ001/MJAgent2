"""场景反打「机位背后一侧」构图起草：一次视觉模型调用，把主视角图交给模型，
写出从相反方向拍摄同一空间时应该看到的静态环境内容，供生成时拼进最终提示词。

只是提示词增强，不是判据——判据在 ``app.scene_reverse.judge``。失败（网络错误、
供应商拒收、JSON 解析失败）一律返回空字典，调用方据此退回不带起草内容的基础
提示词，不阻塞生成，与 ``app.media_exec.subtitle_gate``「未判定不拦」同一取舍。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app import hiagent

_LOGGER = logging.getLogger(__name__)

# 2026-09-27 B 沙箱实测（6 场景 × 6 种写法）：起草拆成下面五项、第一次生成用「编辑指令 + 左右
# 对调清单」时通过率最高（4/6，真人室内 4 个里 3 个，原写法 0/4）；第 5 项供判否后不带种子的重试用。
DRAFT_FIELDS = ("left_becomes_right", "invisible_elements", "back_wall_content", "furniture_facing", "reverse_view")

_PROMPT_TEMPLATE = (
    "图中是「{scene_canonical}」这个空间的建立镜头（主视角），画风：{visual_style}。"
    "现在要为图像编辑模型准备一份改机位说明：假设摄像机被搬到画面最深处一侧、原地转身 180°"
    "朝原机位方向回看同一个空间。请仔细观察这张图，逐项回答，除 reverse_view 外每项 20~40 字，"
    "必须点出画面里真实存在的具体物体，不写笼统描述：\n"
    "1) left_becomes_right：图中画面左侧有哪些具体物体/陈设，在新机位画面里应出现在右侧；\n"
    "2) invisible_elements：图中画面最深处（背景）的哪些具体物体，在新机位画面里应完全看不见"
    "（它们现在在镜头身后）；\n"
    "3) back_wall_content：新机位的背景——也就是原摄像机站立那一侧——按空间合理布局推断会有"
    "什么（墙面/门窗/陈设/地面）；某类物品在图中只有一件（例如只有一扇窗）时，不要在这一侧再"
    "写出第二件同类物品；\n"
    "4) furniture_facing：图中哪件有朝向的家具（沙发、床、桌椅、柜台等）在新机位画面里应露出"
    "与原图相反的那一面、是哪一面；没有这类家具写「无」；\n"
    "5) reverse_view：不依赖参考图也能单独成立的一段 120~220 字画面描述，写转身后完整画面的"
    "背景、左右陈设、材质与光源方向；只写画面内容，不写「反打」「180°」这类术语。\n"
    '只返回一个 JSON 对象，键为 left_becomes_right、invisible_elements、back_wall_content、'
    'furniture_facing、reverse_view，值都是字符串。'
)


def _build_messages(establishing_url: str, scene_canonical: str, visual_style: str) -> list[dict[str, Any]]:
    text = _PROMPT_TEMPLATE.format(scene_canonical=scene_canonical, visual_style=visual_style)
    return [
        {"role": "system", "content": "Return exactly one valid JSON object. No Markdown, no prose."},
        {"role": "user", "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": establishing_url}},
        ]},
    ]


def _parse_draft(raw: str) -> dict[str, str]:
    text = (raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        return {}
    return {key: str(data.get(key) or "").strip() for key in DRAFT_FIELDS}


async def draft_behind_camera_note(
    *, scene_canonical: str, visual_style: str, establishing_image_path: str, scene_reference_id: str,
) -> dict[str, str]:
    """起草反打改机位说明（键见 ``DRAFT_FIELDS``）；失败返回空字典，绝不抛出。"""
    try:
        messages = _build_messages(
            hiagent.data_url_from_file(establishing_image_path), scene_canonical, visual_style,
        )
        raw = await hiagent.chat(
            messages, temperature=0.4, max_tokens=1000,
            provider=hiagent.active_provider("vlm"),
            call_meta={"kind": "vlm_scene_reverse_draft", "scene_reference_id": scene_reference_id},
            response_format={"type": "json_object"},
        )
        return _parse_draft(raw)
    except Exception as exc:  # noqa: BLE001 起草失败不阻塞生成：见模块文档
        _LOGGER.warning("[SCENE_REVERSE_DRAFT][未生效] 场景 %s 起草失败：%s", scene_reference_id, exc)
        return {}
