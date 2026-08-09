"""AI compiler from a continuity contract to a physical H3 generation prompt."""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from app import config
from app.compiler import sanitize_seedance_prompt
from app.continuity import effective_characters_visible, prompt_source_provenance_errors
from app.harness import model_gateway
from app.schemas import Bible, Shot


AI_VIDEO_PROMPT_CONTRACT_VERSION = "ai_physical_performance_v1"
_TIME_EPSILON = 0.02


class CharacterPoseDirection(BaseModel):
    character: str
    visibility: Literal["visible", "offscreen", "entering", "exiting"] = "visible"
    body_pose: str
    weight_balance: str
    facing: str
    gaze: str
    left_hand: str = ""
    right_hand: str = ""
    facial_muscles: str
    breathing: str


class MotionBeatDirection(BaseModel):
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    physical_action: str
    body_mechanics: str
    camera_behavior: str
    dialogue_sync: str = ""


class DialogueDirection(BaseModel):
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    delivery: Literal["spoken_dialogue", "offscreen_voice", "narration"]
    speaker: str
    text: str
    physical_delivery: str


class CameraDirection(BaseModel):
    shot_size: str
    angle: str
    movement: str
    framing: str
    action_visibility: str


class AIVideoPromptDraft(BaseModel):
    visible_characters: list[str] = Field(default_factory=list)
    character_direction: str
    scene_direction: str
    reference_strategy: str
    interaction_kind: Literal[
        "none",
        "person_person_contact",
        "person_object_contact",
        "spatial_interaction",
    ] = "none"
    interaction_participants: list[str] = Field(default_factory=list)
    contact_point: str = ""
    contact_point_visible: bool = False
    start_pose: list[CharacterPoseDirection] = Field(default_factory=list)
    start_environment: str
    motion_beats: list[MotionBeatDirection] = Field(min_length=1)
    end_pose: list[CharacterPoseDirection] = Field(default_factory=list)
    end_environment: str
    camera: CameraDirection
    performance_direction: str
    dialogue: list[DialogueDirection] = Field(default_factory=list)
    on_screen_text: str
    negative_constraints: list[str] = Field(min_length=1, max_length=6)


def _audio_value(item: object, field: str, default: object = "") -> object:
    if isinstance(item, dict):
        return item.get(field, default)
    return getattr(item, field, default)


def _expected_dialogue(shot: Shot) -> list[dict[str, Any]]:
    return [
        {
            "start_s": float(_audio_value(item, "start_s", 0.0) or 0.0),
            "end_s": float(_audio_value(item, "end_s", 0.0) or 0.0),
            "delivery": str(_audio_value(item, "type") or ""),
            "speaker": str(_audio_value(item, "speaker_id") or ""),
            "text": str(_audio_value(item, "text") or ""),
        }
        for item in (shot.audio_timeline or [])
        if str(_audio_value(item, "type") or "") != "ambient_sound"
        and str(_audio_value(item, "text") or "").strip()
    ]


def _same_time(left: float, right: float) -> bool:
    return abs(float(left) - float(right)) <= _TIME_EPSILON


def validate_ai_video_prompt(
    draft: AIVideoPromptDraft,
    *,
    shot: Shot,
) -> list[str]:
    errors: list[str] = []
    visible = effective_characters_visible(shot)
    if draft.visible_characters != visible:
        errors.append(
            "visible_characters 必须与权威画面角色顺序完全一致："
            + json.dumps(visible, ensure_ascii=False)
        )

    for label, poses in (("start_pose", draft.start_pose), ("end_pose", draft.end_pose)):
        pose_names = [item.character for item in poses]
        if pose_names != visible:
            errors.append(
                f"{label} 必须按顺序为每个画面角色提供一次身体姿态："
                + json.dumps(visible, ensure_ascii=False)
            )

    duration = float(shot.duration_s)
    cursor = 0.0
    for index, beat in enumerate(draft.motion_beats):
        if beat.end_s <= beat.start_s:
            errors.append(f"motion_beats[{index}] end_s 必须大于 start_s")
        if not _same_time(beat.start_s, cursor):
            errors.append(
                f"motion_beats[{index}] 必须从 {cursor:g}s 连续开始，"
                f"当前为 {beat.start_s:g}s"
            )
        if beat.end_s > duration + _TIME_EPSILON:
            errors.append(f"motion_beats[{index}] 超出镜头时长 {duration:g}s")
        cursor = beat.end_s
    if not _same_time(cursor, duration):
        errors.append(
            f"motion_beats 必须连续覆盖 0–{duration:g}s，当前结束于 {cursor:g}s"
        )

    expected_dialogue = _expected_dialogue(shot)
    if len(draft.dialogue) != len(expected_dialogue):
        errors.append(
            f"dialogue 数量必须为 {len(expected_dialogue)}，当前为 {len(draft.dialogue)}"
        )
    for index, expected in enumerate(expected_dialogue):
        if index >= len(draft.dialogue):
            break
        actual = draft.dialogue[index]
        if (
            not _same_time(actual.start_s, expected["start_s"])
            or not _same_time(actual.end_s, expected["end_s"])
            or actual.delivery != expected["delivery"]
            or actual.speaker != expected["speaker"]
            or actual.text != expected["text"]
        ):
            errors.append(
                f"dialogue[{index}] 必须逐字、逐时码保留权威声轨："
                + json.dumps(expected, ensure_ascii=False)
            )

    participants = list(dict.fromkeys(draft.interaction_participants))
    if participants != draft.interaction_participants:
        errors.append("interaction_participants 不得重复")
    undeclared = [name for name in participants if name not in visible]
    if undeclared:
        errors.append(
            "interaction_participants 含非画面角色："
            + "、".join(undeclared)
        )
    if draft.interaction_kind == "person_person_contact":
        if len(participants) < 2:
            errors.append("person_person_contact 必须声明至少两名接触参与者")
        if not draft.contact_point.strip() or not draft.contact_point_visible:
            errors.append("双人接触必须明确接触点，并保证接触点在构图中可见")
    elif draft.interaction_kind == "none" and participants:
        errors.append("interaction_kind=none 时不得声明互动参与者")

    dialogue_texts = {
        str(item["text"]).strip()
        for item in expected_dialogue
        if str(item["text"]).strip()
    }
    for pose in [*draft.start_pose, *draft.end_pose]:
        if pose.body_pose.strip() in dialogue_texts:
            errors.append(f"{pose.character} 的身体姿态不能直接填台词")

    required_text = (
        str(shot.required_text.exact_text or "").strip()
        if shot.required_text is not None
        else ""
    )
    if required_text and required_text not in draft.on_screen_text:
        errors.append(f"on_screen_text 必须保留精确文字「{required_text}」")
    return errors


def _render_pose(items: list[CharacterPoseDirection], environment: str) -> str:
    lines = [
        (
            f"{item.character}（{item.visibility}）：{item.body_pose}；"
            f"重心 {item.weight_balance}；朝向 {item.facing}；视线 {item.gaze}；"
            f"左手 {item.left_hand or '按姿态自然放置'}；"
            f"右手 {item.right_hand or '按姿态自然放置'}；"
            f"面部 {item.facial_muscles}；呼吸 {item.breathing}。"
        )
        for item in items
    ]
    if environment.strip():
        lines.append(f"环境状态：{environment.strip()}")
    return "\n".join(lines)


def render_ai_video_prompt(
    draft: AIVideoPromptDraft,
    *,
    shot: Shot,
    aspect_ratio: str = "9:16",
) -> str:
    motion = "\n".join(
        (
            f"{beat.start_s:g}–{beat.end_s:g}秒：{beat.physical_action}；"
            f"身体力学：{beat.body_mechanics}；摄影同步：{beat.camera_behavior}"
            + (f"；对白同步：{beat.dialogue_sync}" if beat.dialogue_sync.strip() else "")
            + "。"
        )
        for beat in draft.motion_beats
    )
    dialogue = "\n".join(
        (
            f"{item.start_s:g}–{item.end_s:g}秒："
            f"{item.speaker}说「{item.text}」；{item.physical_delivery}。"
        )
        for item in draft.dialogue
    ) or "无台词、画外音或旁白；人物不做说话口型。"
    interaction = ""
    if draft.interaction_kind != "none":
        interaction = (
            f"互动类型：{draft.interaction_kind}；"
            f"参与者：{'、'.join(draft.interaction_participants)}；"
            f"接触点：{draft.contact_point or '无实体接触'}；"
            f"接触点可见：{'是' if draft.contact_point_visible else '否'}。"
        )
    camera = (
        f"{draft.camera.shot_size}；{draft.camera.angle}；{draft.camera.movement}；"
        f"构图：{draft.camera.framing}；动作可见性：{draft.camera.action_visibility}。"
    )
    sections = [
        ("CHARACTERS", draft.character_direction),
        ("SCENE", draft.scene_direction),
        ("REFERENCE STRATEGY", draft.reference_strategy),
        ("START POSE | 0.0s", _render_pose(draft.start_pose, draft.start_environment)),
        ("MOTION", motion),
        ("PHYSICAL INTERACTION", interaction),
        (
            f"END POSE | {shot.duration_s}.0s",
            _render_pose(draft.end_pose, draft.end_environment),
        ),
        ("CAMERA", camera),
        ("PERFORMANCE", draft.performance_direction),
        ("DIALOGUE", dialogue),
        ("ON-SCREEN TEXT", draft.on_screen_text),
        ("NEGATIVE", "；".join(draft.negative_constraints)),
    ]
    body = "\n\n".join(
        f"[{title}]\n{content.strip()}"
        for title, content in sections
        if content.strip()
    )
    return sanitize_seedance_prompt(
        f"{body} --ratio {aspect_ratio} --dur {shot.duration_s}"
    )


def _visible_character_bible(shot: Shot, bible: Bible) -> list[dict[str, str]]:
    by_name = {item.name: item for item in bible.characters}
    return [
        {
            "name": name,
            "appearance": by_name[name].appearance_canonical,
        }
        for name in effective_characters_visible(shot)
        if name in by_name
    ]


async def generate_ai_video_prompt(
    *,
    shot: Shot,
    bible: Bible,
    continuity_contract: str,
    video_generation_mode: str,
    operation_scope: str,
    user_instruction: str = "",
    critique: list[str] | None = None,
) -> tuple[str, AIVideoPromptDraft]:
    _debug_payload_started = time.monotonic()
    # #region debug-point E:prompt-payload
    with __import__("contextlib").suppress(Exception): __import__("urllib.request").request.urlopen(__import__("urllib.request").request.Request("http://127.0.0.1:7777/event", data=json.dumps({"sessionId":"video-dispatch-block","runId":"post-fix","hypothesisId":"E","location":"app/video_prompt_ai.py:generate_ai_video_prompt","msg":"[DEBUG] prompt payload start","data":{"shot_no":shot.shot_no},"ts":int(time.time()*1000)}).encode(), headers={"Content-Type":"application/json"}), timeout=0.2).read()
    # #endregion
    payload = {
        "task": (
            "将内部 Cinematic Continuity Contract 编译成一条可直接提交 MiniMax H3 的"
            " Physical Performance Generation Prompt。所有创作字段必须由你重新导演和生成，"
            "不要复制长合同的重复约束。重点写清可见骨架姿态、重心、手部、视线、呼吸、"
            "连续动作力学、摄影可见范围，以及动作与权威声轨的同一节奏。"
        ),
        "hard_rules": [
            "start_pose/end_pose 只能写可直接看到的身体与环境状态，不能写剧情态度或台词",
            "motion_beats 必须无缝覆盖完整镜头时长，不能用脸部或口型替代主动作",
            "权威 dialogue 的说话人、文本、delivery、start_s、end_s 必须逐项原样返回",
            "有双人肢体接触时必须选择 person_person_contact，保留双方入画并让接触点可见",
            "camera 必须覆盖主动作真正发生的身体区域；不能同时要求大动作和单人大头特写",
            "negative_constraints 只写本镜最关键的 1–6 条风险，禁止复述整份长合同",
            "禁止添加合同外人物、台词、动作结果、道具、文字或下一镜内容",
        ],
        "video_generation_mode": video_generation_mode,
        "duration_s": shot.duration_s,
        "visible_characters": effective_characters_visible(shot),
        "character_bible": _visible_character_bible(shot, bible),
        "visual_style": bible.world.visual_style_canonical,
        "shot_contract": {
            "scene_time": shot.scene_time,
            "scene_name": shot.scene_name,
            "scene_setting": shot.scene_setting,
            "action_desc": shot.action_desc,
            "first_frame_desc": shot.first_frame_desc,
            "last_frame_desc": shot.last_frame_desc,
            "state_in": shot.state_in,
            "primary_action": shot.primary_action,
            "state_out": shot.state_out,
            "emotion_beat": shot.emotion_beat,
            "camera": {
                "shot_size": shot.shot_size,
                "angle": shot.camera_angle,
                "movement": shot.camera_move,
                "motivation": shot.camera_motivation,
            },
            "spatial_anchor": shot.spatial_anchor,
            "continuity_state_in": shot.continuity_state_in.model_dump(mode="json"),
            "continuity_state_out": shot.continuity_state_out.model_dump(mode="json"),
            "audio_timeline": [
                item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
                for item in (shot.audio_timeline or [])
            ],
            "required_text": (
                shot.required_text.model_dump(mode="json")
                if shot.required_text is not None else None
            ),
        },
        "continuity_contract": continuity_contract,
        "user_instruction": user_instruction,
        "quality_critique": list(critique or []),
        "output_schema": AIVideoPromptDraft.model_json_schema(),
    }
    fingerprint = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:24]
    # #region debug-point E:prompt-payload
    with __import__("contextlib").suppress(Exception): __import__("urllib.request").request.urlopen(__import__("urllib.request").request.Request("http://127.0.0.1:7777/event", data=json.dumps({"sessionId":"video-dispatch-block","runId":"post-fix","hypothesisId":"E","location":"app/video_prompt_ai.py:generate_ai_video_prompt","msg":"[DEBUG] prompt payload before chat","data":{"shot_no":shot.shot_no,"elapsed_ms":round((time.monotonic()-_debug_payload_started)*1000,1),"continuity_chars":len(continuity_contract)},"ts":int(time.time()*1000)}).encode(), headers={"Content-Type":"application/json"}), timeout=0.2).read()
    # #endregion
    draft = await model_gateway.chat_structured(
        [
            {
                "role": "system",
                "content": (
                    "你是电影动作导演和 AI 视频提示词编译器。"
                    "只输出符合 Schema 的一个 JSON 对象，不输出 Markdown 或解释。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False),
            },
        ],
        model_type=AIVideoPromptDraft,
        validate=lambda value: validate_ai_video_prompt(value, shot=shot),
        operation_id=f"video_prompt_{operation_scope}_{fingerprint}",
        max_tokens=5000,
        format_retry_limit=1,
        semantic_retry_limit=2,
        temperature=0.35,
        call_meta={
            "stage_key": "video_prompt_generate",
            "call_role": "video_prompt_compiler",
            "initiator_label": "AI 视频提示词编译",
            "shot_no": shot.shot_no,
            "contract_version": AI_VIDEO_PROMPT_CONTRACT_VERSION,
        },
        repair_context=(
            f"镜头时长={shot.duration_s}s；"
            f"权威画面角色={json.dumps(effective_characters_visible(shot), ensure_ascii=False)}；"
            f"权威声轨={json.dumps(_expected_dialogue(shot), ensure_ascii=False)}"
        ),
    )
    prompt = render_ai_video_prompt(draft, shot=shot)
    if len(prompt) > config.PROMPT_CHAR_LIMIT:
        raise model_gateway.StructuredSemanticError(
            f"AI 视频提示词长度 {len(prompt)} 超过上限 {config.PROMPT_CHAR_LIMIT}"
        )
    provenance_errors = prompt_source_provenance_errors(prompt, shot)
    if provenance_errors:
        raise model_gateway.StructuredSemanticError("；".join(provenance_errors))
    return prompt, draft
