"""LLM 输出合同（PRD 原则 P5：一切 LLM 输出有 Schema）。对应 docs/PROMPT_SPEC.md。"""
from __future__ import annotations

import json
import re

from pydantic import BaseModel, Field, ValidationError

SHOT_SIZES = {"远景", "全景", "中景", "近景", "特写"}
CAMERA_MOVES = {"固定", "推近", "拉远", "横摇", "跟随"}
TRANSITIONS = {
    "硬切",
    "叠化",
    "淡出淡入",
    "黑场",
    "闪黑",
    "闪白",
    "甩镜",
    "遮挡转场",
    "匹配剪辑",
    "声音延续+叠化",
    "声音先行+淡入",
}
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
    # 定妆照（圣经定稿后由 Seedream 生成，跨集一致性的视觉锚点；LLM 输出中不含以下字段）
    ref_image_path: str | None = None
    # 画像描述覆盖：人工编辑的定妆照生成词；为空时用 锚点串+画风 合成的默认描述（refs.portrait_prompt）
    portrait_prompt_override: str | None = None


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


class Bible(BaseModel):
    characters: list[Character]
    world: World
    scenes: list[Scene] = Field(default_factory=list)


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


# 可拍剧本（分集之后、分镜之前）：把小说叙述改写为每 10s 一拍的场次剧本。
# 它不写景别/运镜/首尾帧，只锁定人物在场、可见动作、关键台词、局势变化和下一拍钩子。
class ScreenplayBeat(BaseModel):
    beat_no: int
    day_offset: int
    time_of_day: str
    location: str
    characters: list[str] = Field(default_factory=list)
    dramatic_event: str
    visible_action: str
    key_dialogues: list[str] = Field(default_factory=list)
    turn: str
    carry: str
    beat_type: str
    source_excerpt: str = ""


class ScriptScene(BaseModel):
    scene_no: int
    scene_heading: str
    story_function: str
    characters: list[str] = Field(default_factory=list)
    summary: str
    conflict: str = ""
    turn: str = ""
    source_basis: str = ""


class EpisodeScreenplay(BaseModel):
    episode_no: int
    # 完整剧本源数据（新格式）
    id: str | None = None
    mode: str = "full_script"
    title: str = ""
    source_text_range: str = ""
    logline: str = ""
    script_format_note: str = ""
    # 单集戏剧契约（对齐调研文档 §3.4/§3.5）：用于把"故事为什么发生、主角要什么、阻力与代价"
    # 显式锁定，避免压缩成 50s 时把方向性信息一起丢掉。
    dramatic_question: str = ""      # 本集观众心里追问的那个问题（§3.4）
    protagonist_goal: str = ""       # 主角本集外在目标（看得见、可完成）（§3.5）
    obstacle: str = ""               # 外部+内部阻力（§3.5）
    stakes: str = ""                 # 失败代价/成功代价（§3.5）
    # 必保留清单（防丢失核心）：剧本台先显式挑出"绝不能丢"的关键内容，
    # 写进正文后由 key-content 校验确认其在 full_script_text 中真实出现；
    # 分镜台再据此逐条落实到镜头，避免重要台词/剧情在压缩中被静默丢弃。
    key_lines: list[str] = Field(default_factory=list)        # 关键台词（金句/决定性对白/情绪爆点），含说话人更佳（§2.9/§3.11）
    key_plot_points: list[str] = Field(default_factory=list)  # 关键剧情点/反转/信息揭示（§3.6）
    scene_outline: list[ScriptScene] = Field(default_factory=list)
    full_script_text: str = ""
    character_state_changes: list[str] = Field(default_factory=list)
    emotional_curve: str = ""
    ending_hook: str = ""
    source_basis: str = ""
    adaptation_direction: str = ""
    opening: str = ""
    development: str = ""
    conflict: str = ""
    climax: str = ""
    created_at: float | None = None
    updated_at: float | None = None
    # 历史兼容：旧格式仍按 beat 列表存储
    beats: list[ScreenplayBeat] = Field(default_factory=list)


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
    # 归一化命中的库内规范场景名（由 validate_storyboard_scenes 回填；LLM 通常不输出）。
    # 渲染期据此取场景库图复用；为空时回退到用 scene_setting 文本匹配。
    scene_name: str = ""
    characters: list[str] = Field(default_factory=list)
    action_desc: str
    # 首尾帧画面描述：本镜【开始】与【结束】两个静止画面，必须明显不同（10s 视频的起点/终点）
    first_frame_desc: str = ""
    last_frame_desc: str = ""
    source_excerpt: str = ""
    narration: str | None = None
    dialogues: list[Dialogue] = Field(default_factory=list)
    transition: str = "硬切"
    continuity_from_prev: bool = False


class Storyboard(BaseModel):
    episode_no: int
    shots: list[Shot]


class StoryboardOutlineShot(BaseModel):
    """分镜大纲里的一条镜头节拍：只规划"本镜推进什么剧情"，不写执行细节。
    逐镜填充阶段据此把整集剧情均匀铺满，避免多镜停留在同一情绪/同一句原文。"""

    shot_no: int
    scene_setting: str = ""   # 时间+地点短标签
    beat: str = ""            # 本镜推进的剧情（一句话：谁做了什么 / 局势如何变化 / 与上一镜的区别）
    covers: str = ""          # 本镜落实的必保留关键台词/剧情点（可空）


class StoryboardOutline(BaseModel):
    """整集分镜大纲：一次性把剧本铺成有序的 N 条镜头节拍，先定全局节奏再逐镜填充。"""

    episode_no: int
    shots: list[StoryboardOutlineShot] = Field(default_factory=list)


class QaResult(BaseModel):
    character_match: float = 0
    action_match: float = 0
    clean_frame: float = 0
    overall: float = 0
    issues: list[str] = Field(default_factory=list)


def extract_json(text: str) -> dict:
    """从模型输出中提取第一个完整 JSON 对象。失败抛 ValueError（含原文摘要）。"""
    cleaned = re.sub(r"```(?:json)?\s*", "", text, flags=re.IGNORECASE).replace("```", "").strip()
    first_start = cleaned.find("{")
    if first_start == -1:
        raise ValueError(f"输出中找不到 JSON 对象。原文开头：{text[:200]}")

    first_error: json.JSONDecodeError | None = None
    for match in re.finditer(r"{", cleaned):
        start = match.start()
        try:
            obj, _ = json.JSONDecoder().raw_decode(cleaned[start:])
        except json.JSONDecodeError as exc:
            first_error = first_error or exc
            continue
        if isinstance(obj, dict):
            return obj
        raise ValueError(f"JSON 根节点不是对象。片段：{cleaned[start:start + 200]}")

    detail = f"（{first_error}）" if first_error else ""
    raise ValueError(f"JSON 解析失败{detail}。片段：{cleaned[first_start:first_start + 200]}")


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
