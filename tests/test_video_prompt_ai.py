from __future__ import annotations

import asyncio

import pytest

from app import video_modes, video_prompt_ai
from app.hiagent import ProviderError
from app.schemas import (
    AudioTimelineItem,
    Bible,
    Character,
    CharacterContinuityState,
    ContinuityState,
    Shot,
    World,
)
from app.stages import (
    DirectedPhysicalInteraction,
    _directed_interaction_risk_tags,
)


def _bible() -> Bible:
    return Bible(
        characters=[
            Character(
                name="甲",
                role="主角",
                appearance_canonical="黑色短发，深蓝外套，成年男性",
            ),
            Character(
                name="乙",
                role="配角",
                appearance_canonical="棕色长发，米白衬衫，成年女性",
            ),
        ],
        world=World(
            era="现代",
            genre="剧情",
            visual_style_canonical="电影感写实动画",
        ),
    )


def _shot() -> Shot:
    state_in = ContinuityState(characters={
        "甲": CharacterContinuityState(
            pose="站在画面左侧，右臂向前伸",
            facing="画面右侧",
            gaze_target="乙",
            right_hand="靠近乙的左手腕",
        ),
        "乙": CharacterContinuityState(
            pose="站在画面右侧，身体正要转开",
            facing="画面右后方",
            gaze_target="门口",
            left_hand="位于甲的右手前方",
        ),
    })
    state_out = ContinuityState(characters={
        "甲": CharacterContinuityState(
            pose="站稳并握住乙的左手腕",
            facing="乙",
            gaze_target="乙",
            right_hand="稳定接触乙的左手腕",
        ),
        "乙": CharacterContinuityState(
            pose="停止转身并回头",
            facing="甲",
            gaze_target="甲",
            left_hand="被甲握住",
        ),
    })
    return Shot(
        shot_no=3,
        duration_s=5,
        shot_size="中景",
        camera_move="固定",
        camera_angle="侧面",
        scene_setting="夜晚客厅",
        scene_name="客厅",
        characters=["甲", "乙"],
        characters_visible=["甲", "乙"],
        action_desc="甲伸手握住正要离开的乙，同时要求她留下。",
        first_frame_desc="甲伸手靠近乙的手腕，乙正向门口转身。",
        last_frame_desc="甲握住乙的手腕，乙停下并回头。",
        state_in="两人尚未接触。",
        primary_action="甲握住乙的手腕并说出要求。",
        state_out="两人保持手腕接触并相互注视。",
        continuity_state_in=state_in,
        continuity_state_out=state_out,
        risk_tags=[
            "dialogue_two_shot_required",
            "dialogue_action_staging",
            "contact_phase:established",
        ],
        audio_timeline=[
            AudioTimelineItem(
                start_s=0.4,
                end_s=2.6,
                type="spoken_dialogue",
                speaker_id="甲",
                text="别走。",
                lip_sync=True,
                emotion="坚定",
            ),
        ],
    )


def _draft() -> video_prompt_ai.AIVideoPromptDraft:
    pose_in = [
        video_prompt_ai.CharacterPoseDirection(
            character="甲",
            body_pose="站在左侧，右臂向乙伸出",
            weight_balance="重心前移到右脚",
            facing="画面右侧的乙",
            gaze="锁定乙的手腕",
            left_hand="垂在身侧",
            right_hand="张开并接近乙的左手腕",
            facial_muscles="眉间收紧，下颌轻微绷紧",
            breathing="短促吸气",
        ),
        video_prompt_ai.CharacterPoseDirection(
            character="乙",
            body_pose="站在右侧，躯干向门口转动",
            weight_balance="重心移向前脚",
            facing="画面右后方",
            gaze="看向门口",
            left_hand="摆在身体后侧",
            right_hand="靠近门把方向",
            facial_muscles="眼睑抬起，嘴角收紧",
            breathing="平稳呼吸",
        ),
    ]
    pose_out = [
        pose_in[0].model_copy(update={
            "body_pose": "右臂屈肘并稳定握住乙的左手腕",
            "right_hand": "握住乙的左手腕",
            "gaze": "看向乙的双眼",
        }),
        pose_in[1].model_copy(update={
            "body_pose": "停下脚步并回头面对甲",
            "left_hand": "手腕被甲握住",
            "facing": "甲",
            "gaze": "看向甲",
        }),
    ]
    return video_prompt_ai.AIVideoPromptDraft(
        visible_characters=["甲", "乙"],
        character_direction="甲与乙保持各自身份和服装，两人同处一个连续镜头。",
        scene_direction="夜晚客厅，沙发与门的位置在全镜保持固定。",
        reference_strategy="两张人物图分别绑定甲和乙，仅用于身份与服装。",
        interaction_kind="person_person_contact",
        interaction_participants=["甲", "乙"],
        contact_point="甲的右手与乙的左手腕",
        contact_point_visible=True,
        start_pose=pose_in,
        start_environment="门在画面右后方，沙发位于左后方。",
        motion_beats=[
            video_prompt_ai.MotionBeatDirection(
                start_s=0,
                end_s=0.4,
                physical_action="甲前移重心并让右手接近乙的左手腕",
                body_mechanics="肩、肘、腕按顺序向前，乙继续转身",
                camera_behavior="固定侧面中景保持双方上半身和手腕入画",
            ),
            video_prompt_ai.MotionBeatDirection(
                start_s=0.4,
                end_s=2.6,
                physical_action="甲握住乙的手腕并说出对白，乙停止前进",
                body_mechanics="接触后甲屈肘缓冲，乙的肩线随拉力回转",
                camera_behavior="保持接触点与甲的口型同时清晰",
                dialogue_sync="甲在握住手腕后开始说话，动作不中断",
            ),
            video_prompt_ai.MotionBeatDirection(
                start_s=2.6,
                end_s=5,
                physical_action="乙回头看向甲，两人稳定在结束姿态",
                body_mechanics="乙从脚步到躯干再到头部依次回转",
                camera_behavior="固定机位让动作自然收束",
            ),
        ],
        end_pose=pose_out,
        end_environment="门与沙发仍在原位，照明方向不变。",
        camera=video_prompt_ai.CameraDirection(
            shot_size="中景",
            angle="互动轴侧面",
            movement="固定",
            framing="双方上半身、甲的口型与手腕接触点同时入画",
            action_visibility="完整看清接近、握住、停步和回头",
        ),
        performance_direction="甲的呼吸和肩部发力与握腕同步，乙的惊讶从眼睑、肩线和重心变化表现。",
        dialogue=[
            video_prompt_ai.DialogueDirection(
                start_s=0.4,
                end_s=2.6,
                delivery="spoken_dialogue",
                speaker="甲",
                text="别走。",
                physical_delivery="握住手腕后开口，口型自然，身体动作继续",
            ),
        ],
        on_screen_text="画面中不生成文字。",
        negative_constraints=[
            "不要把乙裁出画面",
            "不要遮挡或悬空手腕接触点",
            "不要让对白替代握腕动作",
        ],
    )


def test_ai_video_prompt_is_physical_and_audio_aligned() -> None:
    shot = _shot()
    draft = _draft()

    assert video_prompt_ai.validate_ai_video_prompt(draft, shot=shot) == []

    prompt = video_prompt_ai.render_ai_video_prompt(draft, shot=shot)

    assert "[START POSE | 0.0s]" in prompt
    assert "[MOTION]" in prompt
    assert "0.4–2.6秒" in prompt
    assert "别走。" in prompt
    assert "[END POSE | 5.0s]" in prompt
    assert "甲的右手与乙的左手腕" in prompt
    assert "[CONSISTENCY]" not in prompt
    assert prompt.endswith("--ratio 9:16 --dur 5")


def test_ai_video_prompt_rejects_timeline_and_dialogue_drift() -> None:
    shot = _shot()
    draft = _draft()
    draft.motion_beats[1].start_s = 0.8
    draft.dialogue[0].text = "你别走。"

    errors = video_prompt_ai.validate_ai_video_prompt(draft, shot=shot)

    assert any("连续开始" in error for error in errors)
    assert any("逐字、逐时码" in error for error in errors)


def test_ai_prompt_generation_uses_structured_model_output(monkeypatch) -> None:
    draft = _draft()
    calls: list[dict] = []

    async def fake_chat_structured(messages, **kwargs):
        calls.append({"messages": messages, **kwargs})
        assert kwargs["validate"](draft) == []
        return draft

    monkeypatch.setattr(
        video_prompt_ai.model_gateway,
        "chat_structured",
        fake_chat_structured,
    )

    prompt, generated = asyncio.run(video_prompt_ai.generate_ai_video_prompt(
        shot=_shot(),
        bible=_bible(),
        continuity_contract="[START STATE]\n两人尚未接触。",
        video_generation_mode="REFERENCE_IMAGE_MODE",
        operation_scope="ver_test",
    ))

    assert generated == draft
    assert calls[0]["call_meta"]["call_role"] == "video_prompt_compiler"
    assert "AI 视频提示词编译" == calls[0]["call_meta"]["initiator_label"]
    assert "别走。" in prompt


def test_directed_contact_contract_preserves_two_shot_tags() -> None:
    interaction = DirectedPhysicalInteraction(
        kind="person_person_contact",
        participants=["甲", "乙"],
        phase="established",
        contact_point="甲的右手与乙的左手腕",
        contact_point_visible=True,
    )

    assert _directed_interaction_risk_tags(interaction) == [
        "dialogue_action_staging",
        "dialogue_two_shot_required",
        "contact_phase:established",
    ]


def _reference(name: str) -> dict:
    payload = "YQ==" if name == "甲" else "Yg=="
    return {
        "id": f"character-{name}",
        "url": f"data:image/jpeg;base64,{payload}",
        "type": "character",
        "source": "asset_library",
        "entity_name": name,
        "relatedCharacterIds": [name],
        "selectedForSeedance": True,
        "purposes": ["video_input"],
    }


def test_required_contact_identities_take_reference_slots_first() -> None:
    scene = {
        "id": "scene",
        "url": "data:image/jpeg;base64,cw==",
        "type": "scene",
        "source": "asset_library",
        "selectedForSeedance": True,
        "purposes": ["video_input"],
    }

    packed = video_modes.pack_reference_images_for_seedance(
        [scene, _reference("甲"), _reference("乙")],
        max_images=2,
        required_identity_names=["甲", "乙"],
    )

    assert [item["entity_name"] for item in packed] == ["甲", "乙"]


def test_missing_required_contact_identity_blocks_provider_input(
    monkeypatch,
) -> None:
    monkeypatch.setattr(video_modes, "max_reference_images", lambda: 1)
    meta = {
        "mode": video_modes.REFERENCE_IMAGE_MODE,
        "reference_input_policy_version": (
            video_modes.REFERENCE_INPUT_POLICY_VERSION
        ),
        "reference_images": [_reference("甲"), _reference("乙")],
        "required_reference_characters": ["甲", "乙"],
    }

    with pytest.raises(ProviderError, match="缺少必需人物身份参考图"):
        video_modes.build_seedance_image_inputs(meta)
