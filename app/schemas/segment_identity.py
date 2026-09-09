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
