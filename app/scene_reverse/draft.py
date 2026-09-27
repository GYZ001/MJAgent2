"""场景反打「机位背后一侧」构图起草：一次视觉模型调用，把主视角图交给模型，
写出从相反方向拍摄同一空间时应该看到的静态环境内容，供生成时拼进最终提示词。

只是提示词增强，不是判据——判据在 ``app.scene_reverse.judge``。失败（网络错误、
供应商拒收、JSON 解析失败）一律返回空串，调用方据此退回不带这段描述的基础
提示词，不阻塞生成，与 ``app.media_exec.subtitle_gate``「未判定不拦」同一取舍。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app import hiagent

_LOGGER = logging.getLogger(__name__)

_PROMPT_TEMPLATE = (
    "图中是「{scene_canonical}」这个场景的建立镜头（主视角），画风：{visual_style}。"
    "请描述从与该机位大致相反（约180°）方向拍摄同一空间时，画面里应该出现什么："
    "机位放在画面深处一侧、朝原机位方向回看；写出原机位背后那一侧的墙面、门窗、"
    "陈设、地面等静态环境内容；光源方位与主视角保持同一物理方向；材质与色调与"
    "主视角一致；不要写人物。只写这段构图描述本身，80~160字，不要解释。"
    '只返回一个 JSON 对象：{{"note": "描述文字"}}。'
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


def _parse_note(raw: str) -> str:
    text = (raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return ""
    data = json.loads(text[start:end + 1])
    return str(data.get("note") or "").strip() if isinstance(data, dict) else ""


async def draft_behind_camera_note(
    *, scene_canonical: str, visual_style: str, establishing_image_path: str, scene_reference_id: str,
) -> str:
    """写出反打方向该看到的静态环境内容；失败返回空串，绝不抛出。"""
    try:
        messages = _build_messages(
            hiagent.data_url_from_file(establishing_image_path), scene_canonical, visual_style,
        )
        raw = await hiagent.chat(
            messages, temperature=0.4, max_tokens=600,
            provider=hiagent.active_provider("vlm"),
            call_meta={"kind": "vlm_scene_reverse_draft", "scene_reference_id": scene_reference_id},
            response_format={"type": "json_object"},
        )
        return _parse_note(raw)
    except Exception as exc:  # noqa: BLE001 起草失败不阻塞生成：见模块文档
        _LOGGER.warning("[SCENE_REVERSE_DRAFT][未生效] 场景 %s 起草失败：%s", scene_reference_id, exc)
        return ""
