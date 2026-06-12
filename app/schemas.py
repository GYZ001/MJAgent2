"""LLM 输出合同（PRD 原则 P5：一切 LLM 输出有 Schema）。对应 docs/PROMPT_SPEC.md。"""
from __future__ import annotations

import json
import re

from pydantic import BaseModel, Field, ValidationError

SHOT_SIZES = {"远景", "全景", "中景", "近景", "特写"}
CAMERA_MOVES = {"固定", "推近", "拉远", "横摇", "跟随"}
TRANSITIONS = {"硬切", "叠化", "黑场"}
EMOTIONS = {"平静", "愤怒", "悲伤", "惊恐", "喜悦", "讥讽", "坚定"}


class Relationship(BaseModel):
    to: str
    relation: str


class Character(BaseModel):
    name: str
    role: str
    appearance_canonical: str
    personality: str = ""
    speech_style: str = ""
    relationships: list[Relationship] = Field(default_factory=list)
    # 定妆照（圣经定稿后由 Seedream 生成，跨集一致性的视觉锚点；LLM 输出中不含此字段）
    ref_image_path: str | None = None


class World(BaseModel):
    era: str = ""
    genre: str = ""
    visual_style_canonical: str


class Bible(BaseModel):
    characters: list[Character]
    world: World


class EpisodePlanItem(BaseModel):
    episode_no: int
    title: str
    hook: str
    source_chapters: list[int]
    synopsis: str
    cliffhanger: str
    target_duration_s: int = 50


class EpisodePlan(BaseModel):
    key_timeline: list[str] = Field(default_factory=list)
    episodes: list[EpisodePlanItem]


# 节拍链（C1 阶段）：分镜的戏剧骨架。时间用 day_offset+time_of_day 数值化，校验单调，从机制上禁掉闪回。
TIME_OF_DAY_ORDER = ("清晨", "上午", "中午", "下午", "傍晚", "夜晚", "深夜")
BEAT_TYPES = {"钩子", "铺垫", "升级", "反转", "高潮", "尾钩"}


class Beat(BaseModel):
    beat_no: int
    day_offset: int          # 0=本集第一天，只能向前
    time_of_day: str         # TIME_OF_DAY_ORDER 之一
    location: str            # ≤10 字主地点标签
    characters: list[str] = Field(default_factory=list)
    event: str               # 谁做了什么（一句话，用圣经准确姓名）
    turn: str                # 这一拍改变了什么局势/揭示了什么新信息
    carry: str               # 留给下一拍的钩子/未完成动作
    beat_type: str           # BEAT_TYPES 之一


class BeatChain(BaseModel):
    beats: list[Beat]


class Dialogue(BaseModel):
    speaker: str
    line: str
    emotion: str = "平静"


class Shot(BaseModel):
    shot_no: int
    duration_s: int
    shot_size: str
    camera_move: str
    scene_setting: str
    characters: list[str] = Field(default_factory=list)
    action_desc: str
    narration: str | None = None
    dialogues: list[Dialogue] = Field(default_factory=list)
    transition: str = "硬切"
    continuity_from_prev: bool = False


class Storyboard(BaseModel):
    episode_no: int
    shots: list[Shot]


class QaResult(BaseModel):
    character_match: float = 0
    action_match: float = 0
    clean_frame: float = 0
    overall: float = 0
    issues: list[str] = Field(default_factory=list)


def extract_json(text: str) -> dict:
    """从模型输出中提取第一个完整 JSON 对象。失败抛 ValueError（含原文摘要）。"""
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    start = cleaned.find("{")
    if start == -1:
        raise ValueError(f"输出中找不到 JSON 对象。原文开头：{text[:200]}")
    try:
        obj, _ = json.JSONDecoder().raw_decode(cleaned[start:])
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 解析失败（{exc}）。片段：{cleaned[start:start + 200]}") from exc
    if not isinstance(obj, dict):
        raise ValueError(f"JSON 根节点不是对象。片段：{cleaned[start:start + 200]}")
    return obj


def schema_errors(model_cls: type[BaseModel], obj: dict) -> tuple[BaseModel | None, list[str]]:
    """返回 (实例, 错误列表)。错误消息具体到字段路径，供修复回路回喂。"""
    try:
        return model_cls.model_validate(obj), []
    except ValidationError as exc:
        errors = []
        for e in exc.errors():
            path = ".".join(str(p) for p in e["loc"])
            errors.append(f"字段 {path}：{e['msg']}")
        return None, errors
