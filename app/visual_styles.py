"""统一画风预设。

前端只展示 ``name``；``prompt`` 是后端内部合同，写入
``Bible.world.visual_style_canonical`` 后被场景、定妆和视频链路复用。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VisualStylePreset:
    name: str
    description: str
    prompt: str


VISUAL_STYLE_PRESETS: tuple[VisualStylePreset, ...] = (
    VisualStylePreset("现实电影风", "自然写实的动画电影质感，适合稳重叙事。", "高品质3D写实CG动画，明确虚构数字角色、非真人照片，自然光影，细节丰富，电影质感。"),
    VisualStylePreset("超写实风", "高精度CG材质和近景细节，角色仍明确为非真人。", "高精度3D动漫CG渲染，明确虚构数字角色、非真人照片，精细材质，自然光照，电影质感。"),
    VisualStylePreset("真人CG风", "接近真人比例的数字角色CG，不使用真人照片。", "电影级数字人CG动画，明确虚构数字角色、非真人照片，自然五官比例，稳定光影。"),
    VisualStylePreset("国风电影", "古风气质更强，适合山水和传统东方场面。", "中国古风3D动漫电影质感，明确虚构数字角色、非真人照片，自然光影，东方美术氛围。"),
    VisualStylePreset("唯美电影风", "色调柔和，适合情感戏和审美导向画面。", "唯美动漫电影画风，明确虚构数字角色、非真人照片，柔和光影，美术感强。"),
    VisualStylePreset("史诗电影风", "场面宏大厚重，适合战斗和大场景。", "史诗级3D动漫电影质感，明确虚构数字角色、非真人照片，大场景，厚重光影。"),
    VisualStylePreset("暗黑电影风", "光影冷峻压迫，适合危机和反派氛围。", "暗黑3D动漫电影画风，明确虚构数字角色、非真人照片，冷色光影，神秘压迫感。"),
    VisualStylePreset("清新电影风", "画面明亮通透，适合成长和轻快章节。", "清新3D动漫电影画风，明确虚构数字角色、非真人照片，自然阳光，画面通透。"),
    VisualStylePreset("古典水墨风", "偏山水意境和留白，适合雅致东方画面。", "古典水墨动画意境，明确插画角色、非真人照片，山水氛围，留白雅致。"),
    VisualStylePreset("国漫电影风", "兼顾国漫辨识度和电影质感，适合作为通用默认。", "国漫3D动画电影质感，明确虚构数字角色、非真人照片，精致光影，统一电影画面。"),
    VisualStylePreset("真人摄影风", "照片级真人摄影质感，全部风格中最贴近实拍效果，追求极致真实感。", "照片级人像摄影质感，虚构数字角色、非真人照片，自然光影，肌理清晰，电影质感。"),
    VisualStylePreset("精修真人风", "真人摄影基础上做轻度精修美化，真人相似度约八成，介于真人摄影风与CG动画风格之间。", "写实人像摄影质感，虚构数字角色、非真人照片，自然光影，肤质轻度精修，电影质感。"),
)

DEFAULT_VISUAL_STYLE_NAME = "国漫电影风"


def visual_style_names() -> list[str]:
    return [preset.name for preset in VISUAL_STYLE_PRESETS]


def visual_style_options() -> list[dict[str, str]]:
    return [
        {"name": preset.name, "description": preset.description}
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
