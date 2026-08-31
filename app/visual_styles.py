"""统一画风预设。

前端只展示 ``name``/``description``/``sample_image``；``prompt`` 是后端内部合同，写入
``Bible.world.visual_style_canonical`` 后被场景、定妆和视频链路复用。

``photographic`` 标记该风格是否为照片级真人摄影质感：定妆照/场景图的画风锁定文案
（见 ``app.refs.visual_style_lock`` 及其派生函数）据此在“必须保持 CG/动画渲染”与
“必须保持摄影级写实渲染”之间二选一，避免对真人摄影风预设仍然发出“不得像真人照片”
的自相矛盾指令。

``FALLBACK_VISUAL_STYLE``/``_placeholder_bible``/``_project_bible_or_placeholder`` 从
``app/domain/common.py`` 按原样搬移到这里（2026-08-30，见
``docs/layer_violations_plan_2026-08-30.md`` 组 7a）：三者只依赖 ``app.schemas.Bible``
（L0）+ ``json``，但原来待在 L5 的 ``app.domain.common`` 里，逼着 ``app.video_plan.generate``
（L4）和 ``app.storyboard_supervisor``（L4）为了这一个占位圣经构造函数越级延迟 import
整个 domain 包。``app.domain.common`` 继续从本模块重新导入并保持这三个名字可从
``app.domain.common``/``app.domain`` 原样导入，不影响任何既有调用点。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from app.schemas import Bible


@dataclass(frozen=True)
class VisualStylePreset:
    name: str
    description: str
    prompt: str
    photographic: bool = False
    sample_image: str = ""


VISUAL_STYLE_PRESETS: tuple[VisualStylePreset, ...] = (
    VisualStylePreset(
        "真人摄影风",
        "照片级真人摄影质感，全部风格中最贴近实拍效果，追求极致真实感。",
        "照片级人像摄影质感，虚构数字角色、非真人照片，自然光影，肌理清晰，电影质感。",
        photographic=True,
        sample_image="/visual-styles/real-photo.jpg",
    ),
    VisualStylePreset(
        "精修真人风",
        "真人摄影基础上做轻度精修美化，真人相似度约八成，介于真人摄影风与CG动画风格之间。",
        "写实人像摄影质感，虚构数字角色、非真人照片，自然光影，肤质轻度精修，电影质感。",
        photographic=True,
        sample_image="/visual-styles/retouched-real.jpg",
    ),
    VisualStylePreset(
        "国漫电影风",
        "兼顾国漫辨识度和电影质感，适合作为通用默认。",
        "国漫3D动画电影质感，明确虚构数字角色、非真人照片，精致光影，统一电影画面。",
        sample_image="/visual-styles/guoman-cinematic.jpg",
    ),
    VisualStylePreset(
        "古典水墨风",
        "偏山水意境和留白，适合雅致东方画面。",
        "古典水墨动画意境，明确插画角色、非真人照片，山水氛围，留白雅致。",
        sample_image="/visual-styles/ink-wash.jpg",
    ),
)

DEFAULT_VISUAL_STYLE_NAME = "国漫电影风"


def visual_style_options() -> list[dict[str, str | bool]]:
    """前端导入面板选画风用。``photographic`` 随预设透出（而不是让前端另建一份
    名单）：照片级真人摄影预设在视频供应商侧有较高概率因疑似真人触发隐私政策
    拒收（``InputImageSensitiveContentDetected.PrivacyInformation``，见
    ``app.harness.hiagent_input_image_privacy``），前端据此在选择时如实提示，
    不禁止选择——用户可能确实只要图不要视频。"""
    return [
        {
            "name": preset.name,
            "description": preset.description,
            "sample_image": preset.sample_image,
            "photographic": preset.photographic,
        }
        for preset in VISUAL_STYLE_PRESETS
    ]


def visual_style_prompt(style_name: str | None) -> str | None:
    name = (style_name or "").strip()
    for preset in VISUAL_STYLE_PRESETS:
        if preset.name == name:
            return preset.prompt
    return None


def default_visual_style_prompt() -> str:
    prompt = visual_style_prompt(DEFAULT_VISUAL_STYLE_NAME)
    if prompt is None:  # pragma: no cover - protects accidental catalog edits
        raise RuntimeError("默认统一画风不存在")
    return prompt


FALLBACK_VISUAL_STYLE = "国漫风格，非真人CG渲染，统一电影感光影，暖灰色调"


def _placeholder_bible() -> Bible:
    """剧本/分镜可在人物谱未完成时先独立跑；此处提供最小占位圣经供文本阶段使用。"""
    return Bible.model_validate({
        "characters": [],
        "world": {
            "era": "",
            "genre": "",
            "visual_style_canonical": FALLBACK_VISUAL_STYLE,
        },
    })


def _project_bible_or_placeholder(project_row) -> Bible:
    raw = (project_row["bible_json"] or "").strip() if project_row else ""
    if raw:
        return Bible.model_validate(json.loads(raw))
    return _placeholder_bible()


def is_photographic_style_prompt(prompt: str | None) -> bool:
    """该已解析画风串是否对应照片级真人摄影预设（按 prompt 逐字匹配，非按名称）。

    ``Bible.world.visual_style_canonical`` 落库后只保留 prompt 串本身、不保留预设名，
    所以下游（``app.refs``/``app.stages``）只能拿到这串文本；只要它逐字等于某个
    ``photographic=True`` 预设的 prompt，就判定为照片级摄影风。未命中（自由文本、
    历史遗留画风或已下线预设的旧值）一律按非摄影处理，与改动前的保守默认一致。
    """
    text = (prompt or "").strip()
    if not text:
        return False
    for preset in VISUAL_STYLE_PRESETS:
        if preset.prompt == text:
            return preset.photographic
    return False
