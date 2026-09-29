"""分镜台阶段二 resources 契约的场景/道具/整体容器模型（从 storyboard_pack.py 搬出）。

搬出原因：``app/production/storyboard_pack.py`` 在 ``app/FILE_CONVENTIONS.toml`` 的
line_count 棘轮基线上零余量（见该文件头部 STORYBOARD_PACK_VERSION changelog 历次
搬移的先例），2026-09-29 新增「闪回人物」（``resources.flashback_figures``，真实
回归 proj_ca86b15ab7d7 EP1 段16：模型把闪回中六岁的顾屿绑到了成年顾屿的定妆照）
需要给 ``_AiSegmentResources`` 添一个字段，本文件把场景/道具/容器三个 pydantic
类整体搬出去腾行数——纯搬移 + 一个新字段，行为不变；``storyboard_pack.py`` 用
``as`` 自别名重新导出（``from app.production.storyboard_segment_resources import
X as X``），测试与其余调用方 import 路径不变（tests/test_storyboard_pack*.py 一律
仍从 ``app.production.storyboard_pack`` 导入这几个名字）。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.segment_identity import FlashbackFigure, SegmentCharacter as _AiResourceCharacter


class _AiResourceScene(BaseModel):
    scene_id: str
    scene_reference_id: str | None = None
    description: str = ""


class _AiResourceProp(BaseModel):
    label: str
    description: str = ""


class _AiSegmentResources(BaseModel):
    characters: list[_AiResourceCharacter] = Field(default_factory=list)
    scenes: list[_AiResourceScene] = Field(default_factory=list)
    props: list[_AiResourceProp] = Field(default_factory=list)
    #: 闪回/回忆里以明显不同年龄或形态出现的人物：不进 characters、不用 @人名——
    #: @ 的意思是"这一镜用这个角色当前的定妆照"，闪回人物没有这张图可用，只能靠
    #: 文字描述钉住长相。取值规则见 app.production.storyboard_identity_generation.
    #: IDENTITY_GENERATION_RULES；旧行没有这个字段，Field 默认空列表兼容。
    flashback_figures: list[FlashbackFigure] = Field(default_factory=list)


__all__ = ["_AiResourceScene", "_AiResourceProp", "_AiSegmentResources", "FlashbackFigure"]
