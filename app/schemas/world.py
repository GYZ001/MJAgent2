"""世界观与场景图契约：World/Scene 是跨集视觉一致性锚点，Bible 是两者的聚合。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from .character import Character

class World(BaseModel):
    era: str = ""
    genre: str = ""
    visual_style_canonical: str


class Scene(BaseModel):
    """规范场景（场景图素材库的一条）：跨集场景一致性的视觉锚点（与 Character 同构）。
    name 是稳定短标签（如"宗门广场"），分镜的 scene_setting 收敛到它；scene_canonical 是
    固定场景锚点串（地点/时间/光线/陈设/氛围）；ref_image_path 是 Seedream 生成的定场图。"""

    name: str
    scene_canonical: str
    location_kind: str = ""        # 室内/室外/其他（可选，仅作分类提示）
    # 场景图（圣经定稿后由 Seedream 生成，跨集复用的环境锚点；LLM 输出中不含以下字段）
    ref_image_path: str | None = None
    # 场景图生成词覆盖：人工编辑值；为空时用 锚点串+画风 合成的默认描述（scenes.scene_ref_prompt）
    scene_prompt_override: str | None = None
    # 渐进式结构化锚点与自动发现血缘；旧数据缺字段时保持兼容。
    space: str = ""
    time_of_day: str = ""
    lighting: str = ""
    landmarks: list[str] = Field(default_factory=list)
    first_episode: int | None = None
    required_views: list[str] = Field(default_factory=list)
    discovery_sources: list[str] = Field(default_factory=list)
    # 剧本场次标题可能使用同一地点的简称/旧称。别名只用于把剧本地点稳定解析到
    # 同一规范场景，避免为了一个称谓差异重复建场景或误借其它场景图。
    aliases: list[str] = Field(default_factory=list)
    # 待审状态变化获批后先记录目标锚点和生效集；完成费用确认与整包重绘后才转为正式锚点。
    pending_state_canonical: str | None = None
    pending_state_ep_start: int | None = None


class Prop(BaseModel):
    """规范道具（物件库素材库的一条）：跨集道具一致性的视觉锚点（与 Scene 同构）。
    name 是稳定短标签（如"旧猫包"），映射台抽出的 asset_manifest.props 标签收敛到它；
    appearance_canonical 是三项以上可视觉验证特征（材质/颜色/结构/尺寸/标志物）拼成的
    固定道具锚点串；ref_image_path 是纯色背景单件道具参考图，跨集复用防止形态漂移
    （用户投诉根因：猫包一会儿网状一会儿透明——道具此前没有素材库，只有 label+
    description 文字描述，见 app/production/prep_pack/contracts.py 的相关注释）。"""

    name: str
    appearance_canonical: str
    aliases: list[str] = Field(default_factory=list)
    ref_image_path: str | None = None
    # 道具参考图生成词覆盖：人工编辑值；为空时用 appearance_canonical+画风 合成的默认描述。
    prop_prompt_override: str | None = None
    first_episode_no: int | None = None


class Bible(BaseModel):
    characters: list[Character]
    world: World
    scenes: list[Scene] = Field(default_factory=list)
    props: list[Prop] = Field(default_factory=list)
