from __future__ import annotations

import pytest

from app.compiler import compile_prompt, keyframe_visual_contract
from app.continuity import (
    dialogue_action_staging_kind,
    dialogue_focus_subject,
    dialogue_framing_errors,
    effective_characters_visible,
    preflight_seedance_gates,
    reference_role_plan,
)
from app.evaluations.issues import issue_code
from app.repair_router import route_issues
from app.domain.storyboard_ops import _storyboard_issue_targets_shot
from app.schemas import (
    Bible,
    Character,
    Dialogue,
    EpisodeScreenplay,
    NarrativeContinuityPlan,
    Shot,
    Storyboard,
    StoryboardOutline,
    StoryboardOutlineShot,
    VoiceCanonical,
    World,
)
from app.validators import (
    defer_establishing_covers,
    normalize_dialogue_focus_offscreen_mentions,
    outline_key_line_speaker_errors,
    split_outline_on_speaker_changes,
    validate_storyboard,
)
from app.video_modes import reference_generation_prompt
from app.storyboard_supervisor import _is_structural_storyboard_issue
from app.stages import _filter_partial_storyboard_errors


def _bible() -> Bible:
    return Bible(
        characters=[
            Character(
                name="甲",
                role="主角",
                appearance_canonical="青年男性，黑色短发，玄色窄袖劲装，身形修长，眉目冷静坚定",
            ),
            Character(
                name="乙",
                role="配角",
                appearance_canonical="青年女性，乌黑长发，月白色长裙，身形纤细，神情沉静温和",
            ),
            Character(
                name="丙",
                role="配角",
                appearance_canonical="中年男性，灰色束发，深青长袍，肩背宽阔，神情严肃克制",
            ),
        ],
        world=World(visual_style_canonical="国风竖屏漫剧，人物线条清晰，电影级光影，色彩克制统一"),
    )


def _shot(**overrides) -> Shot:
    data = {
        "shot_no": 2,
        "duration_s": 5,
        "shot_size": "近景",
        "camera_move": "固定",
        "scene_setting": "日，议事厅",
        "characters": ["甲"],
        "characters_visible": ["甲"],
        "action_desc": "甲面向画外的听者站定，以克制语气说出自己的决定。",
        "first_frame_desc": "甲独自处于近景中央，视线落向画外听者，嘴唇尚未张开。",
        "last_frame_desc": "同一机位，甲说完后仍看向画外，神情从迟疑转为坚定。",
        "state_in": "甲独自看向画外听者，尚未开口。",
        "primary_action": "甲说出自己的决定。",
        "state_out": "甲说完后等待画外听者回应。",
        "continuity_mode": "reverse_angle",
        "source_excerpt": "甲抬起头，终于说出了自己的决定。",
        "dialogues": [Dialogue(speaker="甲", line="这件事由我来做。", emotion="坚定")],
        "transition": "硬切",
    }
    data.update(overrides)
    return Shot(**data)


def test_single_speaker_filters_group_from_production_visibility() -> None:
    shot = _shot(
        characters=["甲", "乙", "丙"],
        characters_visible=["甲", "乙", "丙"],
    )

    assert dialogue_focus_subject(shot) == "甲"
    assert effective_characters_visible(shot) == ["甲"]
    assert reference_role_plan(shot, individual_names={"甲", "乙", "丙"}) == [
        "scene_reference",
        "character_identity:甲",
    ]
    contract = keyframe_visual_contract(shot, _bible())
    assert contract["visible_characters"] == ["甲"]
    assert contract["dialogue_focus_subject"] == "甲"
    assert contract["collective_presence_forbidden"] is True


def test_storyboard_dialogue_requires_single_speaker_closeup() -> None:
    group_shot = _shot(
        shot_size="全景",
        camera_move="跟随",
        characters=["甲", "乙", "丙"],
        characters_visible=["甲", "乙", "丙"],
    )
    errors = dialogue_framing_errors(group_shot)

    assert any("只保留说话人" in error for error in errors)
    assert any("近景或特写" in error for error in errors)
    assert any("固定或推近" in error for error in errors)
    assert dialogue_framing_errors(_shot()) == []


def test_full_storyboard_gate_allows_group_dialogue_composition() -> None:
    shot = _shot(
        shot_size="全景",
        camera_move="跟随",
        characters=["甲", "乙", "丙"],
        characters_visible=["甲", "乙", "丙"],
    )

    errors = validate_storyboard(
        Storyboard(episode_no=1, shots=[shot]),
        _bible(),
        target_duration_s=40,
    )

    assert not any(
        "DIALOGUE_FRAMING_INVALID" in error
        for error in errors
    )


def test_seedance_preflight_allows_group_dialogue_composition() -> None:
    shot = _shot(
        shot_size="全景",
        camera_move="跟随",
        characters=["甲", "乙", "丙"],
        characters_visible=["甲", "乙", "丙"],
    )

    errors = preflight_seedance_gates(shot)

    assert not any(
        "DIALOGUE_FRAMING_INVALID" in error
        for error in errors
    )


def test_storyboard_gate_has_no_arbitrary_three_character_limit() -> None:
    shot = _shot(
        characters=["甲", "乙", "丙", "路人甲"],
        characters_visible=["甲", "乙", "丙", "路人甲"],
    )

    errors = validate_storyboard(
        Storyboard(episode_no=1, shots=[shot]),
        _bible(),
        target_duration_s=40,
    )

    assert not any(
        "单镜可渲染上限 3" in error
        for error in errors
    )


def test_dialogue_focus_normalization_marks_listener_offscreen_before_validation() -> None:
    shot = _shot(
        characters=["甲", "乙"],
        characters_visible=["甲", "乙"],
        action_desc="甲正对乙站定，以克制语气说出自己的最终决定。",
        first_frame_desc="甲与乙同处近景，甲看向乙，嘴唇尚未张开。",
        last_frame_desc="同一机位，甲说完后仍注视乙，神情已经转为坚定。",
        state_in="甲看向乙，尚未开口。",
        primary_action="甲向乙说出自己的决定。",
        state_out="甲说完后等待乙回应。",
    )
    board = Storyboard(episode_no=1, shots=[shot])

    assert any("只保留说话人" in error for error in dialogue_framing_errors(shot))

    changes = normalize_dialogue_focus_offscreen_mentions(board, _bible())

    assert shot.characters == ["甲"]
    assert shot.characters_visible == ["甲"]
    assert "画外乙" in shot.action_desc
    assert "画外乙" in shot.first_frame_desc
    assert "画外乙" in shot.last_frame_desc
    assert changes == [{
        "shot_no": 2,
        "dialogue_focus": "甲",
        "marked_offscreen": ["乙"],
        "fields": [
            "characters",
            "characters_visible",
            "action_desc",
            "state_in",
            "primary_action",
            "state_out",
            "first_frame_desc",
            "last_frame_desc",
        ],
    }]

    errors = validate_storyboard(board, _bible(), target_duration_s=40)
    assert not any("只保留说话人" in error for error in errors)
    assert not any("单人对白近景" in error for error in errors)


def test_two_visible_speakers_must_split_into_reverse_shots() -> None:
    shot = _shot(
        characters=["甲", "乙"],
        characters_visible=["甲", "乙"],
        dialogues=[
            Dialogue(speaker="甲", line="你留下。", emotion="坚定"),
            Dialogue(speaker="乙", line="我拒绝。", emotion="平静"),
        ],
    )

    errors = dialogue_framing_errors(shot)

    assert len(errors) == 1
    assert "多个画内说话人" in errors[0]
    assert "正反打" in errors[0]


def test_dialogue_framing_issue_routes_to_current_shot_repair() -> None:
    message = (
        "[DIALOGUE_FRAMING_INVALID] shot_no=6 同一镜包含多个画内说话人 ['甲', '乙']；"
        "请按话轮拆成相邻正反打"
    )

    assert issue_code(message) == "DIALOGUE_FRAMING_INVALID"
    assert _is_structural_storyboard_issue("quality") is False
    plan = route_issues(
        [message],
        validated_prefix_end=5,
        next_shot_no=6,
        semantic_diagnosis={
            "scope": "current_shot",
            "selected_strategy": "repair_current",
            "reason": "构图合同可在当前镜内重写，不需要新增剧情镜头",
        },
    )
    assert plan.level == "L1"
    assert plan.strategy == "repair_current"
    assert plan.invalidation_frontier == 6


@pytest.mark.parametrize(
    "message, shot_no",
    [
        (
            "[DIALOGUE_FRAMING_INVALID] shot_no=11 的对白同时包含剧情道具操作，shot_size 不得为特写；"
            "请至少使用近景并完整保留双手、道具和接触关系",
            11,
        ),
        (
            "[DIALOGUE_FRAMING_INVALID] shot_no=13 的对白同时包含走位/离场等大形体动作，shot_size 应为中景、"
            "全景或远景，当前为「近景」；必须完整拍出动作，不能用单人大近景替代",
            13,
        ),
    ],
)
def test_action_dialogue_framing_messages_route_to_targeted_repair(
    message: str, shot_no: int,
) -> None:
    assert issue_code(message) == "DIALOGUE_FRAMING_INVALID"
    plan = route_issues(
        [message],
        validated_prefix_end=14,
        semantic_diagnosis={
            "scope": "current_shot",
            "selected_strategy": "repair_current",
            "reason": "当前镜的景别/构图可局部修正且不改变叙事所有权",
        },
    )
    assert plan.level == "L1"
    assert plan.strategy == "repair_current"
    assert plan.invalidation_frontier == shot_no


def test_storyboard_issue_localization_does_not_prefix_match_shot_numbers() -> None:
    message = "[FRAME_STATE_INVALID] shots[9](shot_no=10).last_frame_desc 与 planned_state_out 不一致"

    assert _storyboard_issue_targets_shot(message, index=9, shot_no=10) is True
    assert _storyboard_issue_targets_shot(message, index=0, shot_no=1) is False


def test_partial_filter_drops_prior_shot_no_errors() -> None:
    errors = [
        "shot_no=5 是单人对白镜头，shot_size 应为近景或特写",
        "[SHOT_SPOKEN_TEXT_CAPACITY_EXCEEDED] SH013(shot_no=13) "
        "口播/屏幕文字最少需要 1.100s",
        "shot_no=16 是单人对白镜头，camera_move 应为固定或推近",
        "shots[15](shot_no=16).action_desc 缺少当前角色名",
    ]

    assert _filter_partial_storyboard_errors(
        errors,
        current_index=15,
        current_shot_no=16,
    ) == errors[2:]


def test_typed_two_shot_contract_allows_exactly_two_people() -> None:
    shot = _shot(
        shot_size="中景",
        characters=["甲", "乙"],
        characters_visible=["甲", "乙"],
        action_desc="甲伸手拉住正要离开的乙，同时开口要求她留下。",
        first_frame_desc="甲在画面左侧伸手靠近乙的手腕，乙身体朝门口转去。",
        last_frame_desc="同一机位，甲已经拉住乙的手腕，乙停下脚步回头。",
        primary_action="甲拉住乙并要求她留下。",
        risk_tags=["dialogue_two_shot_required"],
    )

    assert dialogue_focus_subject(shot) is None
    assert effective_characters_visible(shot) == ["甲", "乙"]
    assert dialogue_framing_errors(shot) == []


def test_dialogue_with_spatial_action_is_not_collapsed_to_static_closeup() -> None:
    shot = _shot(
        shot_size="近景",
        characters=["甲", "乙"],
        characters_visible=["甲", "乙"],
        action_desc="甲一边说出决定，一边转身穿过人群走向门外，乙留在原地目送。",
        first_frame_desc="甲站在乙面前尚未转身，门位于画面右后方。",
        last_frame_desc="同一机位，甲已走向右后方门口，乙仍留在原位。",
        primary_action="甲说完后转身走向门外。",
        risk_tags=["dialogue_action_staging"],
    )

    assert dialogue_action_staging_kind(shot) == "spatial"
    assert dialogue_focus_subject(shot) is None
    assert effective_characters_visible(shot) == ["甲", "乙"]
    assert any("不能用单人大近景替代" in error for error in dialogue_framing_errors(shot))

    prompt = compile_prompt(shot, _bible())
    assert "动作对白构图" in prompt
    assert "不得只拍站立说话、口型或表情变化来替代动作" in prompt
    assert "全景" in prompt
    assert "对白镜头只允许「甲」一人入画" not in prompt


def test_narrative_dialogue_compile_ignores_same_speaker_legacy_state_delta() -> None:
    shot = _shot(
        continuity_state_in={
            "characters": {
                "甲": {"visibility": "visible", "pose": "站立"},
            },
        },
        continuity_state_out={
            "characters": {
                "甲": {"visibility": "visible", "pose": "抬头"},
            },
        },
    )
    screenplay = EpisodeScreenplay(
        episode_no=1,
        narrative_plan=NarrativeContinuityPlan(scope_id="episode-1"),
        voice_bible=[
            VoiceCanonical(
                speaker_id="甲",
                voice_canonical="克制坚定的青年男声",
            ),
        ],
    )

    assert dialogue_framing_errors(
        shot,
        narrative_authority=True,
    ) == []

    prompt = compile_prompt(shot, _bible(), screenplay=screenplay)

    assert "dialogue_action_staging" not in shot.risk_tags
    assert "动作对白构图" not in prompt
    assert "近景" in prompt
    assert "画面可见角色仅限：甲" in prompt


def test_dialogue_with_story_prop_keeps_hands_and_prop_in_frame() -> None:
    shot = _shot(
        shot_size="近景",
        action_desc="甲翻开手中名册，抬头朝画外宣布下一位测试者的名字。",
        first_frame_desc="甲站在石碑左侧，双手托住尚未翻开的名册。",
        last_frame_desc="同一机位，甲保持名册翻开，抬头看向画外。",
        primary_action="甲翻开名册并宣布名字。",
        risk_tags=["dialogue_action_staging"],
    )

    assert dialogue_action_staging_kind(shot) == "prop"
    prompt = compile_prompt(shot, _bible())
    assert "中景" in prompt
    assert "双手、剧情道具与接触关系" in prompt


def test_prop_dialogue_prompt_does_not_visualize_unreferenced_listener_state() -> None:
    original_state_in = "乙刚质问完毕，甲面对乙，双方对峙持续。"
    original_state_out = "甲说完，乙沉默，态度从愤怒转向理性思考。"
    shot = _shot(
        characters=["甲", "乙"],
        characters_visible=["甲"],
        action_desc="甲面对乙抬手说明缘由，随后收回手等待回应。",
        first_frame_desc="甲独自处于中景，面朝乙方向，右手尚未抬起。",
        last_frame_desc="同一机位，甲已收回右手，目光仍望向乙方向。",
        state_in=original_state_in,
        primary_action="甲抬手向乙说明缘由。",
        state_out=original_state_out,
        risk_tags=["dialogue_action_staging"],
    )

    assert dialogue_action_staging_kind(shot) == "prop"
    assert dialogue_focus_subject(shot) is None

    prompt = compile_prompt(shot, _bible(), with_refs=True)

    # 叙事连续性仍保存在分镜数据里，但不再作为矛盾的可视要求发送给视频模型。
    assert shot.state_in == original_state_in
    assert shot.state_out == original_state_out
    assert "乙刚质问完毕" not in prompt
    assert "乙沉默" not in prompt
    assert "[VISIBLE CAST]" in prompt
    assert "可辨识画面人物仅限：甲" in prompt
    assert "乙只作为画外叙事关系" in prompt
    assert "面朝画外乙方向" in prompt
    assert "目光仍望向画外乙方向" in prompt
    assert "甲抬手向画外乙说明缘由" in prompt
    assert "character_identity:甲" in prompt
    assert "character_identity:乙" not in prompt


def test_action_staging_recovers_typed_visible_partner_from_continuity_state() -> None:
    shot = _shot(
        shot_size="全景",
        characters=["甲", "乙"],
        characters_visible=["甲"],
        action_desc="甲打开门侧身让开，乙迈步进入房间。",
        first_frame_desc="甲站在门内，乙站在门外准备进入。",
        last_frame_desc="甲仍扶着门，乙已经迈入房间。",
        primary_action="甲开门迎接乙进入房间。",
        risk_tags=["dialogue_action_staging"],
        continuity_state_in={
            "characters": {
                "甲": {"visibility": "visible", "pose": "扶门"},
                "乙": {"visibility": "visible", "pose": "站在门外"},
            },
        },
        continuity_state_out={
            "characters": {
                "甲": {"visibility": "visible", "pose": "侧身让开"},
                "乙": {"visibility": "visible", "pose": "迈入房间"},
            },
        },
    )

    assert dialogue_focus_subject(shot) is None
    assert effective_characters_visible(shot) == ["甲", "乙"]

    prompt = compile_prompt(shot, _bible(), with_refs=True)

    assert "character_identity:甲" in prompt
    assert "character_identity:乙" in prompt
    assert "画外乙" not in prompt
    assert "可辨识画面人物仅限：甲" not in prompt


def test_visible_cast_projection_is_inactive_when_visual_states_match_cast() -> None:
    shot = _shot()

    prompt = compile_prompt(shot, _bible(), with_refs=True)

    assert "[VISIBLE CAST]" not in prompt
    assert shot.state_in in prompt
    assert shot.state_out in prompt


def test_prose_does_not_infer_an_undeclared_background_collective() -> None:
    shot = _shot(
        dialogues=[],
        characters=["甲"],
        characters_visible=["甲"],
        action_desc="甲退到队伍最后一排独自站立。",
        primary_action="甲退到队伍最后一排独自站立。",
        last_frame_desc="甲独自站在队伍最后一排，背对前方人群与石碑，与热闹人群形成对比。",
        state_out="甲站在队尾，与前方人群拉开距离。",
    )

    contract = keyframe_visual_contract(shot, _bible())
    assert contract["collective_presence_forbidden"] is False
    assert contract["collective_presence_required"] is False


def test_offscreen_voice_keeps_listener_as_visual_subject() -> None:
    shot = _shot(
        characters=["甲"],
        characters_visible=["甲"],
        audio_cast=["乙"],
        dialogues=[
            Dialogue(
                speaker="乙",
                line="别回头。",
                emotion="惊恐",
                delivery="offscreen_voice",
            )
        ],
    )

    assert dialogue_focus_subject(shot) is None
    assert effective_characters_visible(shot) == ["甲"]
    assert dialogue_framing_errors(shot) == []


def test_outline_splits_every_speaker_change() -> None:
    screenplay = EpisodeScreenplay(
        episode_no=1,
        key_lines=["甲：你留下。", "乙：我拒绝。", "甲：那就到此为止。"],
    )
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=1,
                scene_id="SC-authority",
                scene_setting="日，议事厅",
                beat="甲要求乙留下，乙拒绝后甲结束谈话",
                covers="甲：你留下；乙：我拒绝；甲：那就到此为止",
                key_line_ids=["KL01", "KL02", "KL03"],
                state_in="三人仍在议事厅内对峙。",
                primary_action="三人完成一轮争执。",
                state_out="甲结束谈话。",
                continuity_mode="same_scene_cut",
                duration_s=5,
                characters_visible=["甲", "乙", "丙"],
                audio_cast=["甲", "乙"],
            )
        ],
    )

    speaker_errors = outline_key_line_speaker_errors(outline, screenplay)
    assert speaker_errors
    assert all(
        error.startswith("[OUTLINE_KEY_LINE_SPEAKER_MIXED]")
        for error in speaker_errors
    )
    events = split_outline_on_speaker_changes(outline, screenplay, max_shots=8)

    assert len(events) == 1
    assert [shot.key_line_ids for shot in outline.shots] == [
        ["KL01"],
        ["KL02"],
        ["KL03"],
    ]
    assert [shot.characters_visible for shot in outline.shots] == [["甲"], ["乙"], ["甲"]]
    assert [shot.audio_cast for shot in outline.shots] == [["甲"], ["乙"], ["甲"]]
    assert [shot.scene_id for shot in outline.shots] == [
        "SC-authority", "SC-authority", "SC-authority",
    ]
    assert [shot.continuity_mode for shot in outline.shots] == [
        "same_scene_cut",
        "reverse_angle",
        "reverse_angle",
    ]
    assert outline_key_line_speaker_errors(outline, screenplay) == []


def test_outline_keeps_required_two_person_contact() -> None:
    screenplay = EpisodeScreenplay(
        episode_no=1,
        key_lines=["甲：你别走。"],
    )
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=1,
                scene_setting="日，议事厅",
                beat="甲拉住准备离开的乙并要求她留下",
                covers="甲拉住乙的手腕；甲：你别走",
                key_line_ids=["KL01"],
                primary_action="甲伸手拉住乙的手腕并开口",
                characters_visible=["甲", "乙"],
                audio_cast=["甲"],
            )
        ],
    )

    events = split_outline_on_speaker_changes(outline, screenplay, max_shots=8)

    assert events == []
    assert outline.shots[0].characters_visible == ["甲", "乙"]
    assert outline.shots[0].audio_cast == ["甲"]


def test_first_episode_establishing_shot_defers_dialogue_ids_and_cast() -> None:
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=1,
                scene_setting="日，议事厅",
                beat="先建立议事厅与人物位置",
                covers="甲：你留下。",
                key_line_ids=["KL01"],
                audio_cast=["甲"],
            ),
            StoryboardOutlineShot(
                shot_no=2,
                scene_setting="日，议事厅",
                beat="切入甲的单人对白近景",
                covers="甲抬眼看向画外",
                key_line_ids=[],
                audio_cast=[],
            ),
        ],
    )

    changes = defer_establishing_covers(outline, 1)

    assert changes
    assert outline.shots[0].covers == ""
    assert outline.shots[0].key_line_ids == []
    assert outline.shots[0].audio_cast == []
    assert outline.shots[1].key_line_ids == ["KL01"]
    assert outline.shots[1].audio_cast == ["甲"]
    assert "甲：你留下" in outline.shots[1].covers


def test_video_and_keyframe_prompts_enforce_speaker_only_closeup() -> None:
    shot = _shot(
        shot_size="全景",
        camera_move="跟随",
        characters=["甲", "乙", "丙"],
        characters_visible=["甲", "乙", "丙"],
        action_desc="甲面对乙与丙说出自己的决定，二人留在画外听完。",
        first_frame_desc="甲站在厅内看向画外的乙与丙，尚未开口。",
        last_frame_desc="同一机位，甲说完决定，乙与丙仍留在画外。",
    )

    video_prompt = compile_prompt(shot, _bible(), with_refs=True)
    keyframe_prompt = reference_generation_prompt(
        shot, _bible(), "plot_key_frame", 1,
    )

    assert "竖屏单人对白构图" in video_prompt
    assert "画面可见角色仅限：甲" in video_prompt
    assert "听者、其他说话人和人群全部留在画外" in video_prompt
    assert "character_identity:甲" in video_prompt
    assert "character_identity:乙" not in video_prompt
    assert "SPEAKER CLOSE-UP HARD CONTRACT" in keyframe_prompt
    assert "'甲' is the ONLY visible person" in keyframe_prompt


def test_dialogue_cut_keeps_same_scene_previous_video_tail_input() -> None:
    shot = _shot(
        continuity_mode="action_continuation",
        characters=["甲", "乙"],
        characters_visible=["甲", "乙"],
    )

    prompt = compile_prompt(
        shot,
        _bible(),
        with_refs=True,
        chained=True,
        continuity_mode="action_continuation",
        prev_state_out="上一镜甲乙同框站在议事厅中央。",
    )

    assert shot.continuity_mode == "same_scene_cut"
    assert "强制起始状态" in prompt
    assert "画面可见角色仅限：甲" in prompt
