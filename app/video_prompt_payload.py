"""视频提示词模型调用的载荷装配：权威声轨、硬规则、shot_contract 投影。

从 ``app.video_prompt_ai`` 拆出来的纯数据装配部分——那边的
``generate_ai_video_prompt`` 曾经 132 个代码行，绝大多数是这一坨字典字面量，
文件也已经在 ``app/FILE_CONVENTIONS.toml`` 的行数基线上（基线是欠账不是许可，
只降不升）。这里不做任何模型调用、不碰渲染，只把「喂给模型的那份 JSON」组装出来。

依赖方向单向：``video_prompt_ai`` → 本模块。因此本模块不 import
``video_prompt_ai``——``visible_characters`` / ``character_bible`` /
``output_schema`` 这几项由调用方算好传进来，而不是反向 import 回去取。
层号随 ``app.video_prompt_ai`` 同为 L4（见 app/LAYERS.toml）。
"""
from __future__ import annotations

from typing import Any

from app.schemas import Shot
from app.spoken_contract import effective_spoken_segments


def _audio_value(item: object, field: str, default: object = "") -> object:
    if isinstance(item, dict):
        return item.get(field, default)
    return getattr(item, field, default)


def authoritative_dialogue(shot: Shot) -> list[dict[str, Any]]:
    """本镜的权威声轨：说话人、原话、发声方式，以及（只有 timeline 真有口播轨时才有的）时间码。

    收敛到 ``app.spoken_contract.effective_spoken_segments``——它的 docstring 就写着
    「本镜的唯一有效口播内容」，全仓其余消费者（``spoken_text_of`` /
    ``spoken_char_total`` / ``spoken_speakers`` / 容量校验）都走它，只有视频提示词
    这一处曾经直读 ``shot.audio_timeline`` 裸字段。

    2026-09-10 实测「我欲封天」EP2-EP10：197 个镜头的 ``shot_contract_json`` 里
    ``audio_timeline`` **全为空**（字段在、内容空），而 ``dialogues`` 有词。于是
    ``validate_ai_video_prompt`` 里那条「dialogue 必须逐字、逐时码保留权威声轨」
    拿一个空列表当标准答案，实际生效的断言退化成「dialogue 数量必须为 0」——恒真；
    同一个空字段还被塞进 payload，于是模型连这一镜该说什么都从没收到过。250 条
    分镜台词里 13 条因此没能逐字进入最终提示词：有的整句消失（第 9 集第 21 镜两条
    旁白全丢，提示词只剩「嘴唇开合，露出傻笑」——嘴在动、没有词），有的被"顺手
    改正"了原著自带的错别字（第 10 集「整个外宗五人不知」是原文逐字，提示词写成
    「无人不敬佩」，而提示词规则明写「错别字均照录」）。

    ``segments_from_dialogues`` 派生出的段没有时间码（``start_s``/``end_s`` 为
    ``None``），这是诚实的：原文台词本来就不带时码，编一个出来再拿它当判据，就是
    用猜测换猜测。时间码只在 timeline 真有口播轨时才是权威——比对按需进行，见
    ``app.video_prompt_ai.validate_ai_video_prompt``。
    """
    return [
        {
            "start_s": segment.start_s,
            "end_s": segment.end_s,
            "delivery": segment.delivery,
            "speaker": segment.speaker_id,
            "text": segment.text,
        }
        for segment in effective_spoken_segments(shot)
        if segment.text.strip()
    ]


def hard_rules(profile: Any) -> list[str]:
    """模型必须遵守的硬规则；写成完整正面陈述，不写「不要编造」这类只堵一种写法的禁令。"""
    return [
        "start_pose/end_pose 只能写可直接看到的身体与环境状态，不能写剧情态度或台词",
        "motion_beats 必须无缝覆盖完整镜头时长，不能用脸部或口型替代主动作",
        "authoritative_dialogue 是本镜必须说出口的全部台词：说话人、文本、delivery 逐字原样返回，"
        "一句都不能少、不能改写、不能合并；原文自带的错别字、异体字、拼音一律照录，不要顺手改正。"
        "带 start_s/end_s 的条目连时间码一起原样返回；没有时间码的（原文台词本来就不带时间）"
        "由你按镜头节奏安排，但文本本身不得变动。authoritative_dialogue 为空时本镜不出现任何台词",
        "有双人肢体接触时必须选择 person_person_contact，保留双方入画并让接触点可见",
        "camera 必须覆盖主动作真正发生的身体区域；不能同时要求大动作和单人大头特写",
        "negative_constraints 只写本镜最关键的 1–6 条风险，禁止复述整份长合同",
        "禁止添加合同外人物、台词、动作结果、道具、文字或下一镜内容",
        *profile.generation_rules,
    ]


def shot_contract_payload(shot: Shot) -> dict[str, Any]:
    """喂给模型的镜头合同投影。``authoritative_dialogue`` 与校验判据同源。"""
    return {
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
        # 模型答不出来时先查它有没有收到标准答案——这里曾经喂的是恒空的
        # shot.audio_timeline 裸字段，模型从没见过这一镜要说的话。
        "authoritative_dialogue": authoritative_dialogue(shot),
        "ambient_sound": [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
            for item in (shot.audio_timeline or [])
            if str(_audio_value(item, "type") or "") == "ambient_sound"
        ],
        "required_text": (
            shot.required_text.model_dump(mode="json")
            if shot.required_text is not None else None
        ),
    }


def build_payload(
    *,
    shot: Shot,
    profile: Any,
    visible_characters: list[str],
    character_bible: list[dict[str, str]],
    visual_style: str,
    video_generation_mode: str,
    continuity_contract: str,
    user_instruction: str,
    critique: list[str] | None,
    output_schema: dict[str, Any],
    target_provider: str,
    target_model: str,
) -> dict[str, Any]:
    """组装完整载荷。全部参数必传——``conn=None`` 那类可选参数是缺陷的温床。"""
    return {
        "task": (
            "将内部 Cinematic Continuity Contract 编译成一条可直接提交"
            f" {profile.model_family} 的漫剧视频提示词。先形成共享导演结构化草稿，"
            "所有创作字段必须重新导演和生成，不要复制长合同的重复约束。重点写清"
            "可见骨架姿态、重心、手部、视线、呼吸、连续动作力学、动作因果、"
            "摄影可见范围，以及动作与权威声轨的同一节奏。"
        ),
        "hard_rules": hard_rules(profile),
        "target_prompt_profile": {
            "provider": target_provider,
            "model": target_model,
            "profile_id": profile.profile_id,
            "profile_version": profile.version,
            "model_family": profile.model_family,
            "output_language": profile.output_language,
            "render_format": profile.render_format,
        },
        "video_generation_mode": video_generation_mode,
        "duration_s": shot.duration_s,
        "visible_characters": visible_characters,
        "character_bible": character_bible,
        "visual_style": visual_style,
        "shot_contract": shot_contract_payload(shot),
        "continuity_contract": continuity_contract,
        "user_instruction": user_instruction,
        "quality_critique": list(critique or []),
        "output_schema": output_schema,
    }
