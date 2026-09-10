from __future__ import annotations

import asyncio

import pytest

from app import video_modes, video_prompt_ai
from app.video_prompt_profiles import (
    MINIMAX_H3_PROFILE,
    SEEDANCE_2_PROFILE,
    resolve_video_prompt_profile,
)
from app.hiagent import ProviderError
from app.schemas import (
    AudioTimelineItem,
    Bible,
    Dialogue,
    Character,
    CharacterContinuityState,
    ContinuityState,
    Shot,
    World,
)
from tests.conftest import patch_video_modes_everywhere
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


def test_video_prompt_profile_follows_video_provider() -> None:
    assert resolve_video_prompt_profile(
        provider="hiagent",
        model="seedance-2.0",
    ) == SEEDANCE_2_PROFILE
    assert resolve_video_prompt_profile(
        provider="minimax_h3",
        model="MiniMax-H3",
    ) == MINIMAX_H3_PROFILE


def test_h3_keyframe_prompt_uses_native_three_field_contract() -> None:
    prompt = video_prompt_ai.render_ai_video_prompt(
        _draft(),
        shot=_shot(),
        prompt_profile=MINIMAX_H3_PROFILE,
        video_generation_mode=video_modes.FIRST_LAST_FRAME_MODE,
    )

    assert prompt.startswith(
        "How the reference pictures align with the target video"
    )
    assert "0.00-second mark" in prompt
    assert "5.00-second mark" in prompt
    assert "integrated_multimodal_description:" in prompt
    assert "overall_soundscape:" in prompt
    assert "non_diegetic_music:" in prompt
    assert "<d>[Chinese] 别走。</d>" in prompt
    assert prompt.endswith("--ratio 9:16 --dur 5")


def test_h3_reference_prompt_uses_native_six_section_contract() -> None:
    prompt = video_prompt_ai.render_ai_video_prompt(
        _draft(),
        shot=_shot(),
        prompt_profile=MINIMAX_H3_PROFILE,
        video_generation_mode=video_modes.REFERENCE_IMAGE_MODE,
    )

    headings = [
        "subject_definitions:",
        "summary:",
        "retention_analysis:",
        "detailed_description:",
        "overall_soundscape:",
        "non_diegetic_music:",
    ]
    positions = [prompt.index(heading) for heading in headings]
    assert positions == sorted(positions)
    assert "[reference generation]" in prompt


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
    assert calls[0]["call_meta"]["prompt_profile_id"] == (
        SEEDANCE_2_PROFILE.profile_id
    )
    assert "别走。" in prompt


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
    patch_video_modes_everywhere(monkeypatch, "max_reference_images", lambda: 1)
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


# ---------------------------------------------------------------------------
# 权威声轨来源：2026-09-10 实测「我欲封天」EP2-EP10 的 197 个镜头，
# shot_contract_json 里 audio_timeline 全为空而 dialogues 有词。_expected_dialogue
# 曾直读 audio_timeline 裸字段，于是「dialogue 必须逐字保留权威声轨」这条比对拿空
# 列表当标准答案，实际生效的断言退化成「dialogue 数量必须为 0」——恒真。
# ---------------------------------------------------------------------------

def _shot_without_timeline() -> Shot:
    """线上真实形态：dialogues 有词、audio_timeline 空。"""
    shot = _shot()
    shot.audio_timeline = []
    shot.dialogues = [
        Dialogue(speaker="甲", line="别走。", emotion="坚定"),
    ]
    return shot


def test_empty_audio_timeline_falls_back_to_shot_dialogues() -> None:
    expected = video_prompt_ai._expected_dialogue(_shot_without_timeline())
    assert [item["text"] for item in expected] == ["别走。"]
    assert [item["speaker"] for item in expected] == ["甲"]
    assert expected[0]["start_s"] is None and expected[0]["end_s"] is None


def test_empty_timeline_no_longer_lets_a_silent_prompt_pass() -> None:
    """这正是恒真断言放过去的形态：台账记了台词，草稿一句不说。"""
    shot = _shot_without_timeline()
    draft = _draft()
    draft.dialogue = []

    errors = video_prompt_ai.validate_ai_video_prompt(draft, shot=shot)

    assert any("dialogue 数量必须为 1" in error for error in errors)


def test_reworded_line_is_rejected_even_without_a_timeline() -> None:
    """第 10 集实测：原文自带错别字「整个外宗五人不知」被提示词"顺手改正"成
    「无人不敬佩」。提示词规则明写「错别字均照录」，这里必须拦住。"""
    shot = _shot_without_timeline()
    draft = _draft()
    draft.dialogue[0].text = "别走啊。"

    errors = video_prompt_ai.validate_ai_video_prompt(draft, shot=shot)

    assert any("逐字" in error for error in errors)


def test_untimed_authority_does_not_demand_time_codes() -> None:
    """原文台词本来就不带时码，编一个出来再拿它当判据就是用猜测换猜测。
    说话人/原话/发声方式仍然逐字比。"""
    shot = _shot_without_timeline()
    draft = _draft()
    draft.dialogue[0].start_s = 1.9
    draft.dialogue[0].end_s = 3.3

    errors = video_prompt_ai.validate_ai_video_prompt(draft, shot=shot)

    assert not any("逐字" in error for error in errors)


def test_timeline_still_wins_when_it_has_real_speech() -> None:
    """timeline 有真实口播轨时它仍是权威（带时码），回退只在它为空时发生。"""
    shot = _shot()
    assert shot.audio_timeline
    expected = video_prompt_ai._expected_dialogue(shot)
    assert [item["text"] for item in expected] == ["别走。"]
    assert expected[0]["start_s"] == pytest.approx(0.4)
    draft = _draft()
    draft.dialogue[0].start_s = 1.9
    assert any("逐时码" in e for e in video_prompt_ai.validate_ai_video_prompt(draft, shot=shot))


def test_payload_carries_the_authoritative_dialogue_when_timeline_is_empty(monkeypatch) -> None:
    """模型答不出来时先查它有没有收到标准答案：payload 曾经喂的是恒空的
    shot.audio_timeline 裸字段，模型从没见过这一镜要说的话。"""
    import json

    shot = _shot_without_timeline()
    draft = _draft()
    captured: list[dict] = []

    async def fake_chat_structured(messages, **kwargs):
        captured.append(json.loads(messages[-1]["content"]))
        return draft

    monkeypatch.setattr(video_prompt_ai.model_gateway, "chat_structured", fake_chat_structured)
    asyncio.run(video_prompt_ai.generate_ai_video_prompt(
        shot=shot, bible=_bible(), continuity_contract="[START STATE]\n两人尚未接触。",
        video_generation_mode="REFERENCE_IMAGE_MODE", operation_scope="ver_test",
    ))

    authority = captured[0]["shot_contract"]["authoritative_dialogue"]
    assert [item["text"] for item in authority] == ["别走。"]
    assert "audio_timeline" not in captured[0]["shot_contract"]


def test_contract_version_bumped_so_v2_drafts_are_recomputed() -> None:
    """旧草稿是在「权威恒空」下生成的，必须重算而不是命中缓存——
    app/media_exec/input_video_mode.py 按这个常量判定要不要复用。"""
    assert video_prompt_ai.AI_VIDEO_PROMPT_CONTRACT_VERSION == "ai_model_adaptive_prompt_v3"


def test_authority_helper_is_shared_not_duplicated() -> None:
    """校验判据与 payload 必须是同一个函数——两份副本迟早会分叉，而分叉的那一份
    就是模型收到的标准答案与验收标准不一致的那次事故。"""
    from app import video_prompt_payload

    assert video_prompt_ai._expected_dialogue is video_prompt_payload.authoritative_dialogue


def test_payload_module_does_not_import_back_into_video_prompt_ai() -> None:
    """依赖单向：video_prompt_ai → video_prompt_payload。反向 import 会成环。"""
    import ast
    import pathlib

    source = (pathlib.Path(__file__).resolve().parent.parent / "app" / "video_prompt_payload.py").read_text(encoding="utf-8")
    modules = {
        node.module for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import) for alias in node.names
    }
    assert "app.video_prompt_ai" not in modules
