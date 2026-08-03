"""LLM 输出合同（PRD 原则 P5：一切 LLM 输出有 Schema）。对应 docs/PROMPT_SPEC.md。"""
from __future__ import annotations

import json
import re

from typing import TypeVar

from pydantic import BaseModel, Field, ValidationError, model_validator

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

# 镜头连续性模式（PRD：删除「同场景即连续」布尔推断）
CONTINUITY_MODES = {
    "action_continuation",  # 同一人物同一动作跨镜延续；唯一可传上一镜尾帧
    "same_scene_cut",       # 同场景换景别/构图
    "reaction_cut",         # 切到另一人物或人群反应
    "reverse_angle",        # 正反打
    "insert_detail",        # 道具/手部/文字特写
    "scene_change",         # 时间或地点改变
}

DELIVERY_OWNERS = {
    "visual_action",
    "spoken_dialogue",
    "offscreen_voice",
    "narration",
    "on_screen_text",
    "ambient_sound",
}

AUDIO_TIMELINE_TYPES = {
    "spoken_dialogue",
    "offscreen_voice",
    "narration",
    "ambient_sound",
}

PROMPT_CONTRACT_VERSION = "seedance_structured_continuity_v4"

# 主线节拍 ID（S*）与剧本事件 ID（E*）长得像但语义不同，历史数据把 S07 写进了 story_event_id。
# 这两个正则是四类 ID 分离（PRD VAL-422 §4.4.1）的判定底座。
SPINE_BEAT_ID_RE = re.compile(r"^S\d{1,3}$", re.I)
STORY_EVENT_ID_RE = re.compile(r"^E\d{1,3}(?:\.\d{1,3})?$", re.I)
KEY_LINE_ID_RE = re.compile(r"^KL\d{1,3}$", re.I)

_IdCarrier = TypeVar("_IdCarrier", bound=BaseModel)


def _normalize_information_ids(model: _IdCarrier) -> _IdCarrier:
    """把 `new_information_ids` 归一到 `information_ids`，两侧保持同一份内容。

    PRD §7.1 要求兼容旧字段一个版本周期，但内部立即以 `information_ids` 为准。
    双向同步而不是单向覆盖，才能让仍在读旧字段的调用方继续正确工作。
    """
    new_ids = [str(x).strip() for x in (model.new_information_ids or []) if str(x).strip()]
    info_ids = [str(x).strip() for x in (model.information_ids or []) if str(x).strip()]
    merged = list(dict.fromkeys([*info_ids, *new_ids]))
    if merged != info_ids:
        model.information_ids = merged
    if merged != new_ids:
        model.new_information_ids = merged
    return model


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
    # 渐进式结构化锚点与自动发现血缘；旧数据缺字段时保持兼容。
    space: str = ""
    time_of_day: str = ""
    lighting: str = ""
    landmarks: list[str] = Field(default_factory=list)
    forbidden_elements: list[str] = Field(default_factory=list)
    first_episode: int | None = None
    required_views: list[str] = Field(default_factory=list)
    discovery_sources: list[str] = Field(default_factory=list)
    # 剧本场次标题可能使用同一地点的简称/旧称。别名只用于把剧本地点稳定解析到
    # 同一规范场景，避免为了一个称谓差异重复建场景或误借其它场景图。
    aliases: list[str] = Field(default_factory=list)
    # 待审状态变化获批后先记录目标锚点和生效集；完成费用确认与整包重绘后才转为正式锚点。
    pending_state_canonical: str | None = None
    pending_state_ep_start: int | None = None


class Bible(BaseModel):
    characters: list[Character]
    world: World
    scenes: list[Scene] = Field(default_factory=list)


class ScriptScene(BaseModel):
    scene_no: int
    scene_heading: str
    story_function: str
    characters: list[str] = Field(default_factory=list)
    summary: str
    conflict: str = ""
    turn: str = ""
    source_basis: str = ""


class StoryEvent(BaseModel):
    """剧情事件台账：描述一次可见/可听的状态变化，而非原文摘要。"""

    event_id: str
    source_span: str = ""
    source_fact: str = ""
    state_in: str = ""
    trigger: str = ""
    visible_change: str = ""
    state_out: str = ""
    must_keep: bool = True
    adaptation_addition: bool = False
    adaptation_reason: str = ""
    approved: bool = False


class InformationItem(BaseModel):
    """信息交付台账：每项观众必须获得的信息只能有一个主交付镜头。"""

    info_id: str
    event_id: str = ""
    content: str = ""
    delivery_owner: str = "visual_action"
    speaker_id: str | None = None
    exact_text: str | None = None
    reinforcement_allowed: bool = False
    status: str = "unassigned"  # unassigned | assigned | delivered
    assigned_shot_no: int | None = None


class VoiceCanonical(BaseModel):
    speaker_id: str
    voice_canonical: str
    language: str = "普通话"
    role_type: str = "named_character"  # named_character | functional_character | narrator


class PlotSpineBeat(BaseModel):
    """主线骨架节拍：谁做了什么 → 局势变化（Renderability First）。"""

    beat_id: str
    who: str = ""
    does: str = ""
    turn: str = ""
    must_keep: bool = True
    # VAL-422 §4.4.3：可选绑定信息原子/关键台词；跨镜聚合校验时按这些 ID 核对交付。
    information_ids: list[str] = Field(default_factory=list)
    key_line_ids: list[str] = Field(default_factory=list)


class PlotSpine(BaseModel):
    """嵌入剧本输出的主线骨架：只保改变局势的事件，显式列出不拍内容。"""

    episode_premise: str = ""
    spine_beats: list[PlotSpineBeat] = Field(default_factory=list)
    must_keep_ending: str = ""
    drop_list: list[str] = Field(default_factory=list)


class KeyDialogueTurn(BaseModel):
    """One source-grounded turn in an ordered main-dialogue chain."""

    speaker: str = ""
    line: str = ""
    function: str = "statement"  # trigger|announcement|question|response|decision|statement
    source_text: str = ""         # exact source utterance used as adaptation evidence


class KeyDialogueChain(BaseModel):
    """A trigger and its dependent replies; downstream must preserve the whole chain."""

    chain_id: str = ""
    topic: str = ""
    turns: list[KeyDialogueTurn] = Field(default_factory=list)


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
    # 主线台词/剧情点（Renderability First）：只保留推动 spine 的内容，禁止全量原文台词入库。
    key_lines: list[str] = Field(default_factory=list)        # 由 dialogue_chains 按话轮顺序确定性派生
    # 新生成合同：结构化对白链是 key_lines 的权威来源；key_lines 由后端按 turns 顺序回填。
    dialogue_chains: list[KeyDialogueChain] = Field(default_factory=list)
    key_plot_points: list[str] = Field(default_factory=list)  # 与 spine 对齐的局势变化
    plot_spine: PlotSpine | None = None
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
    # PRD：信息唯一归属契约（下游只消费结构化事件/台账，不把散文剧本当公共上下文）
    episode_premise: str = ""
    events: list[StoryEvent] = Field(default_factory=list)
    information_ledger: list[InformationItem] = Field(default_factory=list)
    voice_bible: list[VoiceCanonical] = Field(default_factory=list)
    approved_adaptations: list[str] = Field(default_factory=list)
    forbidden_additions: list[str] = Field(default_factory=list)
    created_at: float | None = None
    updated_at: float | None = None


def normalize_screenplay_json_shape(obj: dict) -> tuple[dict, list[str]]:
    """Hoist screenplay fields accidentally nested under ``plot_spine``.

    A missing close brace after ``plot_spine.drop_list`` leaves all following
    root fields inside ``plot_spine``.  The payload can become syntactically
    valid after its final brace is closed, while Pydantic would silently ignore
    those misplaced extras.  Only fields declared by ``EpisodeScreenplay`` and
    not declared by ``PlotSpine`` are eligible; explicit root values win.
    """
    spine = obj.get("plot_spine")
    if not isinstance(spine, dict):
        return obj, []
    misplaced = [
        key
        for key in spine
        if key in EpisodeScreenplay.model_fields and key not in PlotSpine.model_fields
    ]
    if not misplaced:
        return obj, []

    normalized = dict(obj)
    normalized_spine = dict(spine)
    for key in misplaced:
        value = normalized_spine.pop(key)
        normalized.setdefault(key, value)
    normalized["plot_spine"] = normalized_spine
    return normalized, misplaced


class Dialogue(BaseModel):
    speaker: str
    line: str
    emotion: str = "平静"
    # 画外角色发声：保留 speaker 身份，但不要求画面口型
    delivery: str = "spoken_dialogue"  # spoken_dialogue | offscreen_voice | narration


class AudioTimelineItem(BaseModel):
    start_s: float = 0.0
    end_s: float = 0.0
    type: str = "ambient_sound"
    speaker_id: str | None = None
    text: str = ""
    lip_sync: bool = False
    emotion: str = "平静"
    voice_canonical: str = ""


class SceneContinuityState(BaseModel):
    scene_revision_id: str = ""
    time_of_day: str = ""
    lighting_state: str = ""
    axis_id: str = ""
    landmarks: dict[str, str] = Field(default_factory=dict)


class CharacterContinuityState(BaseModel):
    look_revision_id: str = ""
    outfit_revision_id: str = ""
    visibility: str = "visible"
    screen_side: str = ""
    pose: str = ""
    facing: str = ""
    gaze_target: str = ""
    left_hand: str = ""
    right_hand: str = ""


class PropContinuityState(BaseModel):
    canonical_name: str = ""
    revision_id: str = ""
    owner: str = ""
    location: str = ""
    form: str = ""
    visibility: str = "optional"
    text_state: str = "none"
    required: bool = False


class ContinuityState(BaseModel):
    """可比较的镜头状态快照；自然语言 state_in/state_out 仍作为生成提示。"""

    scene: SceneContinuityState = Field(default_factory=SceneContinuityState)
    characters: dict[str, CharacterContinuityState] = Field(default_factory=dict)
    props: dict[str, PropContinuityState] = Field(default_factory=dict)


class RequiredOnScreenText(BaseModel):
    surface: str = ""
    exact_text: str = ""
    strategy: str = "deterministic_insert"
    delivery_owner_shot_no: int | None = None
    appear_start_s: float = 0.0
    stable_until_s: float | None = None
    style: str = ""
    allow_other_text: bool = False
    max_other_text: int = 0
    font_role: str = "classical_serif"
    reading_priority: str = "plot_critical"


class Shot(BaseModel):
    shot_no: int
    duration_s: int
    shot_size: str
    camera_move: str
    # 时间与场景图身份是两个独立维度。scene_time 允许早/中/晚/黄昏/具体时刻。
    scene_time: str = ""
    # 旧的「时间，地点」混合字段，只作兼容显示；新流程不再用它选场景图。
    scene_setting: str = ""
    # 库内规范场景名，与场景图一一对应。模糊输入命中后必须回填成该规范名。
    scene_name: str = ""
    characters: list[str] = Field(default_factory=list)
    action_desc: str
    # 首尾帧画面描述：本镜【开始】与【结束】两个静止画面，必须明显不同（5~10s 视频的起点/终点）
    first_frame_desc: str = ""
    last_frame_desc: str = ""
    source_excerpt: str = ""
    narration: str | None = None
    dialogues: list[Dialogue] = Field(default_factory=list)
    transition: str = "硬切"
    continuity_from_prev: bool = False
    # ---- PRD 连续性生产契约（缺省时由 first/last_frame / action_desc / characters 回填）----
    story_event_id: str = ""
    purpose: str = ""
    # PRD VAL-422 §4.4.1：E/S/I/KL 四类 ID 分属四个空间，禁止互相混写。
    # story_event_id 只放剧本事件 E*；主线节拍走 spine_beat_ids；关键台词走 key_line_ids。
    spine_beat_ids: list[str] = Field(default_factory=list)
    key_line_ids: list[str] = Field(default_factory=list)
    information_ids: list[str] = Field(default_factory=list)
    # 兼容旧字段一个版本周期；内部立即归一到 information_ids（见 _sync_information_ids）。
    new_information_ids: list[str] = Field(default_factory=list)
    reinforcement_info_ids: list[str] = Field(default_factory=list)
    spoken_contract_status: str = "legacy"  # coherent | conflict | legacy
    state_in: str = ""
    primary_action: str = ""
    emotion_beat: str = ""
    state_out: str = ""
    observed_state_out: str = ""
    continuity_mode: str = ""
    characters_visible: list[str] = Field(default_factory=list)
    audio_cast: list[str] = Field(default_factory=list)
    audio_timeline: list[AudioTimelineItem] = Field(default_factory=list)
    required_text: RequiredOnScreenText | None = None
    continuity_state_in: ContinuityState = Field(default_factory=ContinuityState)
    continuity_state_out: ContinuityState = Field(default_factory=ContinuityState)
    reference_roles: list[str] = Field(default_factory=list)
    do_not_repeat: list[str] = Field(default_factory=list)
    risk_tags: list[str] = Field(default_factory=list)
    prompt_contract_version: str = ""
    legacy_unvalidated: bool = False
    camera_angle: str = ""
    spatial_anchor: str = ""
    is_final: bool = False

    @model_validator(mode="after")
    def _sync_information_ids(self) -> "Shot":
        return _normalize_information_ids(self)


class Storyboard(BaseModel):
    episode_no: int
    shots: list[Shot]


class StoryboardOutlineShot(BaseModel):
    """分镜大纲里的一条镜头节拍：按状态变化一次规划原子镜头。
    逐镜填充阶段据此把整集剧情均匀铺满，避免多镜停留在同一情绪/同一句原文。"""

    shot_no: int
    scene_time: str = ""      # 独立时间标签，可为具体时刻
    scene_name: str = ""      # 场景图素材库的规范名
    scene_setting: str = ""   # 旧数据/显示兼容字段
    beat: str = ""            # 本镜推进的剧情（一句话：谁做了什么 / 局势如何变化 / 与上一镜的区别）
    covers: str = ""          # 本镜落实的必保留关键台词/剧情点（可空）
    # 原子分镜规划字段（PRD §7）：大纲阶段即锁定状态链与连续性模式
    story_event_id: str = ""
    # PRD VAL-422 §4.2：大纲阶段就把关键台词/主线节拍按稳定 ID 分配到镜头，
    # 容量预检据此判断「这一镜的必保留口播是否念得完」。
    spine_beat_ids: list[str] = Field(default_factory=list)
    key_line_ids: list[str] = Field(default_factory=list)
    information_ids: list[str] = Field(default_factory=list)
    new_information_ids: list[str] = Field(default_factory=list)
    state_in: str = ""
    primary_action: str = ""
    emotion_beat: str = ""
    state_out: str = ""
    continuity_mode: str = ""
    duration_s: int | None = None
    characters_visible: list[str] = Field(default_factory=list)
    audio_cast: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _sync_information_ids(self) -> "StoryboardOutlineShot":
        return _normalize_information_ids(self)


class StoryboardOutline(BaseModel):
    """整集分镜大纲：一次性把剧本铺成有序的 N 条镜头节拍，先定全局节奏再逐镜填充。"""

    episode_no: int
    shots: list[StoryboardOutlineShot] = Field(default_factory=list)


def _escape_unescaped_inner_quotes(text: str) -> str:
    """只修复能明确判定为 JSON 字符串内容的裸双引号。

    模型偶尔把原文弯引号改成 ASCII 双引号，例如
    ``"这个"天才"仍在原地"``。字符串真正的结束引号后只能跟
    ``:``, ``,``、``]``、``}`` 或文本结束；其他位置的裸引号可安全
    视为字符串内容并转义。缺逗号、括号错误等结构问题不会被放行。
    """
    repaired: list[str] = []
    in_string = False
    escaped = False
    length = len(text)
    for index, char in enumerate(text):
        if not in_string:
            repaired.append(char)
            if char == '"':
                in_string = True
            continue
        if escaped:
            repaired.append(char)
            escaped = False
            continue
        if char == "\\":
            repaired.append(char)
            escaped = True
            continue
        if char != '"':
            repaired.append(char)
            continue

        next_index = index + 1
        while next_index < length and text[next_index].isspace():
            next_index += 1
        next_char = text[next_index] if next_index < length else ""
        if next_char in {":", ",", "]", "}"} or not next_char:
            repaired.append(char)
            in_string = False
        else:
            repaired.extend(("\\", char))
    return "".join(repaired)


def _close_missing_root_object(text: str) -> str:
    """Close one unambiguous missing root ``}`` at end-of-output.

    Providers occasionally emit an otherwise complete object but stop after the
    final child value (commonly an array), omitting only the outermost closing
    brace.  This repair is intentionally narrower than a general JSON repairer:
    strings must be closed, every nested container must already be balanced,
    and the root object must be the sole remaining open container.
    """
    expected_closers: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            expected_closers.append("}")
        elif char == "[":
            expected_closers.append("]")
        elif char in "}]":
            if not expected_closers or expected_closers[-1] != char:
                return text
            expected_closers.pop()

    if not in_string and not escaped and expected_closers == ["}"]:
        return text + "}"
    return text


def extract_json(text: str, *, repair_unescaped_inner_quotes: bool = False) -> dict:
    """从模型输出中提取第一个完整 JSON 对象。失败抛 ValueError（含原文摘要）。"""
    cleaned = re.sub(r"```(?:json)?\s*", "", text, flags=re.IGNORECASE).replace("```", "").strip()
    first_start = cleaned.find("{")
    if first_start == -1:
        raise ValueError(f"输出中找不到 JSON 对象。原文开头：{text[:200]}")

    first_error: json.JSONDecodeError | None = None
    for match in re.finditer(r"{", cleaned):
        start = match.start()
        # 只把形如 JSON 对象开头的花括号当候选；这样仍能跳过说明文字里的
        # “{不是 JSON}”。一旦遇到第一个真正的 JSON 根对象候选，就必须以它
        # 为准：若它因字符串内双引号未转义等原因损坏，应把解析错误回喂模型，
        # 不能继续向内扫描并误把 dialogues 中的小对象当成整份输出。
        remainder = cleaned[start + 1:].lstrip()
        if remainder and not (remainder.startswith('"') or remainder.startswith("}")):
            continue
        try:
            obj, _ = json.JSONDecoder().raw_decode(cleaned[start:])
        except json.JSONDecodeError as exc:
            candidate = cleaned[start:]
            candidate_error = exc
            if repair_unescaped_inner_quotes:
                repaired = _escape_unescaped_inner_quotes(candidate)
                if repaired != candidate:
                    try:
                        obj, _ = json.JSONDecoder().raw_decode(repaired)
                    except json.JSONDecodeError as repaired_exc:
                        candidate_error = repaired_exc
                    else:
                        if isinstance(obj, dict):
                            return obj
                    candidate = repaired
            # Only an EOF failure may be eligible.  Missing commas and damaged
            # inner structure fail before EOF and must still enter the repair
            # loop instead of being silently guessed here.
            if candidate_error.pos >= len(candidate):
                repaired = _close_missing_root_object(candidate)
                if repaired != candidate:
                    try:
                        obj, _ = json.JSONDecoder().raw_decode(repaired)
                    except json.JSONDecodeError:
                        pass
                    else:
                        if isinstance(obj, dict):
                            return obj
            first_error = exc
            break
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
