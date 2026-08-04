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
    VisualStylePreset("现实电影风", "偏自然写实，适合稳重、贴近真人电影的仙侠叙事。", "电影级真实质感，现实人物建模，自然光影，细节丰富，东方仙侠风。"),
    VisualStylePreset("超写实风", "皮肤、材质和近景细节更强，适合强调角色质感。", "超写实人物建模，真实皮肤纹理，自然光照，东方仙侠，电影质感。"),
    VisualStylePreset("真人CG风", "介于真实和数字角色之间，适合统一人物建模。", "真人CG建模，真实五官比例，自然光影，电影画面，东方仙侠。"),
    VisualStylePreset("国风电影", "古风气质更强，适合门派、山水和传统东方场面。", "中国古风电影质感，真实人物建模，自然光影，东方仙侠氛围。"),
    VisualStylePreset("唯美电影风", "色调更柔和，适合情感戏和审美导向画面。", "唯美电影画风，真实人物建模，柔和光影，东方仙侠，美术感强。"),
    VisualStylePreset("史诗电影风", "场面更宏大厚重，适合宗门、战斗和大场景。", "史诗电影质感，真实人物建模，大场景，厚重光影，东方仙侠。"),
    VisualStylePreset("暗黑电影风", "光影更冷更压迫，适合秘境、危机和反派氛围。", "暗黑电影画风，真实人物建模，冷色光影，神秘压迫感，东方仙侠。"),
    VisualStylePreset("清新电影风", "画面更明亮通透，适合少年成长和轻快章节。", "清新电影画风，真实人物建模，自然阳光，画面通透，东方仙侠。"),
    VisualStylePreset("古典水墨风", "偏山水意境和留白，适合雅致、仙气的东方画面。", "古典水墨意境，真实人物建模，山水氛围，东方仙侠，雅致唯美。"),
    VisualStylePreset("国漫电影风", "兼顾国漫辨识度和电影质感，适合作为通用默认。", "国漫电影质感，真实人物建模，精致光影，东方仙侠，电影画面。"),
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
