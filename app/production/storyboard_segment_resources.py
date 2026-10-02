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

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.segment_identity import FlashbackFigure, SegmentCharacter as _AiResourceCharacter


class _AiResourceScene(BaseModel):
    scene_id: str
    scene_reference_id: str | None = None
    description: str = ""
    #: 本段这个场景此刻的物理状态/陈设是否就是场景卡默认状态（取值规则见
    #: app.production.storyboard_narrative_arc._segment_shared_rules）。
    #: 资产装配（app.video_modes.scene_state_selection）据此决定要不要把
    #: 场景卡参考图发给视频模型：yes 照常发送；no/unsure 保守按"不一致"处理、
    #: 不发送，场景全凭正文——与人物 wardrobe_matches_default 同一思路，但
    #: 场景没有退一步仍然诚实的降级素材，unsure 的保守方向因此是不发图而
    #: 不是发图。此字段上线前生成的存量分镜没有这个 key（读出来是空字符串，
    #: 不是这里的 pydantic 默认值 "unsure"）：那是"从未被问过"，不是"问了
    #: 答不出来"，装配时按原有行为照常发送，不对存量分镜追溯新增限制。
    scene_state_matches_card: Literal["yes", "no", "unsure"] = "unsure"


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
