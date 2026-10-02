"""分镜包发声、可见主体与引用的版本化合同；兼容旧 JSON 缺省字段。"""
from typing import Literal

from pydantic import BaseModel, Field

IDENTITY_CONTRACT_VERSION = "segment_identity/1.0"
DeliveryKind = Literal["spoken_dialogue", "offscreen_dialogue", "inner_monologue", "narration", ""]


class SegmentDialogue(BaseModel):
    utterance_id: str = ""
    speaker_identity_id: str = Field(description="人物使用输入身份 ID；叙述者固定为旁白。旁白只在 dialogue，不进入 resources.characters。")
    line: str
    source_segment_index: int = Field(description="原文 [段N] 编号，沿用 required_dialogue；不是视频分镜 segment_no。")
    delivery: Literal["spoken_dialogue", "offscreen_voice"] = "spoken_dialogue"
    delivery_kind: DeliveryKind = ""
    source_quote_id: str = ""
    source_start: int = -1
    source_end: int = -1
    attribution_evidence: str = ""


class SegmentCharacter(BaseModel):
    identity_id: str
    portrait_id: str | None = None
    description: str = ""
    display_name: str = ""
    visibility: Literal["visible", "voice_only", "unknown"] = "unknown"
    subject_kind: Literal["character", "extra", "crowd", "unknown"] = "unknown"
    #: 本段这个人物的穿着是否就是人物谱定妆照默认造型（取值定义见
    #: app.production.storyboard_identity_generation.IDENTITY_GENERATION_RULES）。
    #: 资产解析（app.video_modes.character_look_selection）据此在全身照/头像照
    #: 之间选图：yes 送全身照（服装也要锁定），no/unsure 送头像照（只锁长相，
    #: 服装以本段文字为准）——缺省/旧数据落在 unsure，保守不兜底。
    wardrobe_matches_default: Literal["yes", "no", "unsure"] = "unsure"


class FlashbackFigure(BaseModel):
    """闪回/回忆画面里以明显不同年龄或形态出现的人物（2026-09-29，真实回归
    proj_ca86b15ab7d7 EP1 段16：模型把闪回中六岁的顾屿登记进 resources.characters
    并绑定成年顾屿的定妆照，画面参考图与文字描述的年龄互相矛盾）。

    与 ``SegmentCharacter`` 分属两个不同的列表（``resources.characters`` /
    ``resources.flashback_figures``），不是同一模型的两种状态：
    ``SegmentCharacter`` 的 ``portrait_id``/``visibility`` 语义是"这一段用哪张
    角色当前定妆照出镜"，闪回人物在这一刻没有任何可用的当前定妆照（角色卡上的
    定妆照是他/她当前年龄的样子，与闪回年龄不符）——结构上不给这个模型
    identity_id/portrait_id 字段，模型就没有字段可以把闪回人物错误绑定到当前
    定妆照上。
    """

    label: str = Field(description="这个闪回人物的称呼，例如「六岁的顾屿」；镜头正文用这个称呼指代他，不加 @")
    description: str = Field(default="", description="至少三项可视觉验证特征：年龄区间、脸型、发型、服装颜色材质等")
