"""统一画风预设。

前端只展示 ``name``/``description``/``sample_image``；``prompt`` 是后端内部合同，写入
``Bible.world.visual_style_canonical`` 后被场景、定妆和视频链路复用。

``photographic`` 标记该风格是否为照片级真人摄影质感：定妆照/场景图的画风锁定文案
（见 ``app.refs.visual_style_lock`` 及其派生函数）据此在“必须保持 CG/动画渲染”与
“必须保持摄影级写实渲染”之间二选一，避免对真人摄影风预设仍然发出“不得像真人照片”
的自相矛盾指令。
"""
from __future__ import annotations

from dataclasses import dataclass


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


def visual_style_options() -> list[dict[str, str]]:
    return [
        {
            "name": preset.name,
            "description": preset.description,
            "sample_image": preset.sample_image,
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
