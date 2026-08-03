"""叙事关键帧与视频编译器共用的接触机位/人物尺度合同。"""
from __future__ import annotations

import asyncio
import base64
import json

import pytest

from app import video_modes
from app.character_policy import is_collective_role
from app.compiler import (
    contact_action_phase, explicit_height_difference_evidence, keyframe_visual_contract,
    narrative_keyframe_target,
)
from app.multiview import (
    keyframe_gate_passed, resolve_shot_asset_dependencies, review_keyframe_with_evidence,
    select_character_view_roles,
)
from app.schemas import Bible, Character, RequiredOnScreenText, Shot, World


def _bible() -> Bible:
    return Bible(
        characters=[
            Character(name="萧炎", role="主角", appearance_canonical="十五岁少年，黑发，灰黑劲装"),
            Character(name="萧薰儿", role="女主", appearance_canonical="十五岁少女，黑发云髻，月白长裙"),
        ],
        world=World(visual_style_canonical="3D 国漫电影风"),
    )


def _contact_shot(**overrides) -> Shot:
    data = {
        "shot_no": 4,
        "duration_s": 5,
        "shot_size": "近景",
        "camera_move": "固定",
        "camera_angle": "平视",
        "scene_setting": "日，测验广场",
        "characters": ["萧炎", "测验员"],
        "characters_visible": ["萧炎", "测验员"],
        "action_desc": "萧炎走到黑色石碑前，抬起右手按住石碑，测验员在侧方观察。",
        "first_frame_desc": "萧炎从人群中走出，右手尚未碰到石碑。",
        "last_frame_desc": "萧炎右掌已贴住石碑，测验员侧身看向接触点。",
        "state_in": "萧炎靠近石碑。",
        "primary_action": "萧炎抬手按住石碑。",
        "state_out": "萧炎右掌仍贴住石碑。",
        "source_excerpt": "他走到石碑之前，缓缓把手掌贴在冰冷的石面上。",
        "dialogues": [],
    }
    data.update(overrides)
    return Shot(**data)


def test_provider_prompt_overrides_conflicting_front_view_and_scale() -> None:
    shot = _contact_shot(characters=["萧炎", "萧薰儿"], characters_visible=["萧炎", "萧薰儿"])
    prompt = video_modes.reference_generation_prompt(
        shot,
        _bible(),
        "plot_key_frame",
        1,
        content_override=(
            "Front-facing portrait. Xiao Yan is huge in the foreground while Xun'er is tiny in the background."
        ),
    )

    assert "MANDATORY KEYFRAME CONTRACT (overrides conflicts above)" in prompt
    assert "SIDE CAMERA REQUIRED" in prompt
    assert "exact touch/hold/impact point" in prompt
    assert "same canonical upright standing height" in prompt
    assert "foreground-giant, or background-miniature" in prompt
    assert video_modes.KEYFRAME_PROMPT_CONTRACT_VERSION in prompt


def test_keyframe_contract_fingerprint_changes_with_dialogue_emotion() -> None:
    winning = _contact_shot(dialogues=[{
        "speaker": "萧炎", "line": "我赢了", "emotion": "兴奋",
    }])
    losing = winning.model_copy(update={"dialogues": [{
        "speaker": "萧炎", "line": "我输了", "emotion": "绝望",
    }]})

    assert video_modes.keyframe_contract_fingerprint(
        winning, _bible(),
    ) != video_modes.keyframe_contract_fingerprint(losing, _bible())


def test_explicit_story_height_difference_is_preserved_without_forced_perspective() -> None:
    shot = _contact_shot(
        characters=["萧炎", "萧薰儿"],
        characters_visible=["萧炎", "萧薰儿"],
        action_desc="萧薰儿仰头看向高她一头的萧炎，伸手扶住他的手臂。",
        last_frame_desc="萧薰儿仍仰头，手已扶住萧炎手臂。",
    )
    prompt = video_modes.reference_generation_prompt(shot, _bible(), "plot_key_frame", 1)

    assert "Preserve only the relative height difference explicitly stated" in prompt
    assert "高她一头" in prompt
    assert "approximately equal standing height" not in prompt
    assert "do not exaggerate it" in prompt


def test_single_character_keyframe_does_not_invent_equal_height_rule() -> None:
    shot = _contact_shot(characters=["萧炎"], characters_visible=["萧炎"])
    prompt = video_modes.reference_generation_prompt(shot, _bible(), "plot_key_frame", 1)

    assert "SIDE CAMERA REQUIRED" in prompt
    assert "approximately equal standing height" not in prompt
    assert "upright standing-height baseline" not in prompt


def test_contact_target_is_one_established_contact_instant() -> None:
    shot = _contact_shot()
    target = narrative_keyframe_target(shot)
    contract = keyframe_visual_contract(shot, _bible())

    assert target == shot.last_frame_desc
    assert "已贴住" in target
    assert "尚未碰到" not in target
    assert contract["contact_required"] is True
    assert contract["target_contact_phase"] == "established"
    assert contract["target_source"] == "last_frame_desc"
    assert contract["camera_angle"] == "侧面"


def test_every_timeline_beat_of_contact_shot_inherits_side_axis() -> None:
    shot = _contact_shot(
        first_frame_desc="萧炎站在人群前抬眼看向黑色石碑。",
        state_in="萧炎站在石碑前准备测试。",
    )
    beats = video_modes.narrative_keyframe_beats(shot, 7)
    contracts = [
        keyframe_visual_contract(video_modes._shot_for_keyframe_beat(shot, beat), _bible())
        for beat in beats
    ]

    assert all(contract["camera_angle"] == "侧面视角" for contract in contracts)
    assert all(contract["contact_camera_required"] is True for contract in contracts)
    # 开场帧本身没有接触事实：只继承侧面轴，不得被伪判成已接触。
    assert contracts[0]["contact_axis_inherited"] is True
    assert contracts[0]["target_contact_phase"] == "none"
    assert contracts[0]["established_contact_required"] is False


def test_every_timeline_beat_preserves_explicit_height_difference() -> None:
    shot = _contact_shot(
        characters=["萧炎", "萧薰儿"],
        characters_visible=["萧炎", "萧薰儿"],
        first_frame_desc="两人站在广场入口。",
        primary_action="萧薰儿仰头看向高她一头的萧炎，伸手扶住他的手臂。",
        state_out="两人转身看向石碑。",
        last_frame_desc="两人并肩看向石碑。",
    )

    contracts = [
        keyframe_visual_contract(video_modes._shot_for_keyframe_beat(shot, beat), _bible())
        for beat in video_modes.narrative_keyframe_beats(shot, 7)
    ]

    assert all(contract["relative_height_policy"] == "preserve_explicit_difference" for contract in contracts)
    assert all(
        any("高她一头" in str(item) for item in contract["height_difference_evidence"])
        for contract in contracts
    )


def test_functional_extra_is_kept_in_keyframe_roster_and_anchor() -> None:
    prompt = video_modes.reference_generation_prompt(_contact_shot(), _bible(), "plot_key_frame", 1)

    assert "测验员" in prompt
    assert "Named/individual visible identities, each exactly once" in prompt
    assert "功能性路人" in prompt


def test_contact_action_selects_profile_seed_even_for_closeup() -> None:
    roles = select_character_view_roles(_contact_shot(shot_size="特写"), "萧炎")
    assert roles == ["profile", "three_quarter"]


def test_batch_writer_uses_actual_slot_and_keeps_more_than_600_chars(monkeypatch) -> None:
    calls: list[dict] = []
    long_prompt = "P" * 1000

    async def fake_chat(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        calls.append(payload)
        return json.dumps({
            "slots": [{"slot": "narrative_keyframe", "type": "plot_key_frame", "prompt": long_prompt}],
        })

    monkeypatch.setattr(video_modes.model_gateway, "chat", fake_chat)
    prompts = asyncio.run(video_modes.write_reference_prompt_batch(
        _contact_shot(), _bible(), [("narrative_keyframe", "plot_key_frame")],
    ))

    assert len(calls) == 1, "模型按真实槽位返回时不应再浪费一次单图回退调用"
    assert prompts == [long_prompt]
    assert calls[0]["output_schema"]["slots"][0]["slot"] == "narrative_keyframe"
    planned = calls[0]["slots"][0]
    assert planned["shot"]["camera_angle"] == "侧面"
    assert planned["shot"]["target_keyframe_desc"] == _contact_shot().last_frame_desc
    assert planned["geometry_contract"]["target_contact_phase"] == "established"
    assert planned["geometry_contract"]["contact_camera_required"] is True


def test_required_text_contract_is_consistent_across_beat_prompt_generation_and_qa(monkeypatch) -> None:
    calls: list[dict] = []

    async def fake_chat(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        calls.append(payload)
        return json.dumps({
            "slots": [
                {"slot": item["slot"], "type": item["type"], "prompt": f"prompt-{item['slot']}"}
                for item in payload["slots"]
            ],
        })

    monkeypatch.setattr(video_modes.model_gateway, "chat", fake_chat)
    shot = _contact_shot(
        last_frame_desc="萧炎手掌贴住石碑，碑面显示金色文字。",
        required_text=RequiredOnScreenText(
            surface="石碑", exact_text="斗之力三段", strategy="embedded_prop",
            appear_start_s=4.0, stable_until_s=5.0,
        ),
    )
    beats = video_modes.narrative_keyframe_beats(shot, 1)
    beat_shot = video_modes._shot_for_keyframe_beat(shot, beats[0])

    assert beats[0]["time_s"] == 4.0
    assert keyframe_visual_contract(beat_shot, _bible())["required_text_expected"] is True
    provider_prompt = video_modes.reference_generation_prompt(
        beat_shot, _bible(), "plot_key_frame", 1,
    )
    assert "the only permitted text is the exact string '斗之力三段'" in provider_prompt
    assert "Clean 9:16 portrait still; no text or subtitles." not in provider_prompt

    prompts = asyncio.run(video_modes.write_reference_prompt_batch(
        shot,
        _bible(),
        [("narrative_keyframe", "plot_key_frame")],
        intents=[beats[0]["prompt_intent"]],
        beats=beats,
    ))

    assert prompts == ["prompt-narrative_keyframe"]
    planned = calls[0]["slots"][0]
    assert planned["geometry_contract"]["required_text_expected"] is True
    assert "the only permitted text is the exact string '斗之力三段'" in planned["text_constraint"]


def test_batch_prompt_uses_each_timeline_beats_own_text_contract(monkeypatch) -> None:
    captured: dict = {}

    async def fake_chat(messages, **kwargs):
        captured.update(json.loads(messages[1]["content"]))
        return json.dumps({
            "slots": [
                {"slot": item["slot"], "type": item["type"], "prompt": f"prompt-{item['slot']}"}
                for item in captured["slots"]
            ],
        })

    monkeypatch.setattr(video_modes.model_gateway, "chat", fake_chat)
    shot = _contact_shot(required_text=RequiredOnScreenText(
        surface="石碑", exact_text="斗之力三段", strategy="embedded_prop",
        appear_start_s=4.0, stable_until_s=5.0,
    ))
    beats = video_modes.narrative_keyframe_beats(shot, 2)
    slots = [(beat["slot_key"], "plot_key_frame") for beat in beats]

    asyncio.run(video_modes.write_reference_prompt_batch(
        shot,
        _bible(),
        slots,
        intents=[beat["prompt_intent"] for beat in beats],
        beats=beats,
    ))

    opening, decisive = captured["slots"]
    assert opening["geometry_contract"]["required_text_expected"] is False
    assert opening["text_constraint"] == "no text or subtitles."
    assert decisive["geometry_contract"]["required_text_expected"] is True
    assert "the only permitted text is the exact string '斗之力三段'" in decisive["text_constraint"]


def test_keyframe_qa_contract_checks_side_contact_height_and_target(monkeypatch) -> None:
    captured: dict = {}

    async def fake_vlm(frames, expectation, call_meta=None):
        captured.update(json.loads(expectation))
        return json.dumps({
            "action_match": 0.9,
            "body_proportion": 0.9,
            "side_view_match": 0.4,
            "contact_visibility": 0.5,
            "contact_phase_match": 0.4,
            "relative_height_match": 0.3,
            "face_identity": 0.9,
            "outfit_match": 0.9,
            "hair_match": 0.9,
            "scene_match": 0.9,
            "hard_failures": [],
            "issues": [],
        })

    monkeypatch.setattr("app.hiagent.vlm_check", fake_vlm)
    monkeypatch.setattr("app.multiview.visual_evidence_qa_enabled", lambda: True)
    shot = _contact_shot(characters=["萧炎", "萧薰儿"], characters_visible=["萧炎", "萧薰儿"])
    qa = asyncio.run(review_keyframe_with_evidence(
        "candidate", shot=shot, bible=_bible(), visual_anchors=[],
    ))

    requirements = " ".join(captured["geometry_requirements"])
    assert "互动轴侧面" in requirements
    assert "接触点必须真实连接" in requirements
    assert "站直基准身高、头身比和骨架尺度必须一致" in requirements
    assert captured["shot"]["target_keyframe_desc"] == shot.last_frame_desc
    assert {
        "wrong_camera_angle", "contact_missing", "contact_phase_mismatch", "relative_scale_mismatch",
    } <= set(qa["hard_failures"])
    assert qa["rule_version"] == "keyframe_geometry_qa_v3"


def test_independent_geometry_guard_overrides_false_high_height_score(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_vlm(_frames, expectation, call_meta=None):
        payload = json.loads(expectation)
        calls.append(payload["task"])
        if payload["task"].startswith("Strict independent"):
            return json.dumps({
                "postures": [
                    {"character": "萧炎", "posture": "standing"},
                    {"character": "萧薰儿", "posture": "standing"},
                ],
                "same_depth_plane": True,
                "max_height_div_min_height": 1.55,
                "childlike_body_scale_mismatch": True,
                "forced_perspective_scale_mismatch": False,
                "scripted_height_relation_match": False,
                "confidence": 0.98,
                "verdict": "fail",
                "issues": ["男角色呈儿童体型，头顶仅到女角色肩部"],
            })
        return json.dumps({
            "action_match": 0.9,
            "body_proportion": 0.95,
            "side_view_match": 0.95,
            "contact_visibility": 0.95,
            "contact_phase_match": 0.95,
            # 模拟截图中的错误通用结论：先把身高匹配误打为 0.9。
            "relative_height_match": 0.9,
            "face_identity": 0.9,
            "outfit_match": 0.95,
            "hair_match": 0.95,
            "scene_match": 0.95,
            "hard_failures": [],
            "issues": [],
        })

    monkeypatch.setattr("app.hiagent.vlm_check", fake_vlm)
    monkeypatch.setattr("app.multiview.visual_evidence_qa_enabled", lambda: True)
    shot = _contact_shot(characters=["萧炎", "萧薰儿"], characters_visible=["萧炎", "萧薰儿"])
    qa = asyncio.run(review_keyframe_with_evidence(
        "candidate", shot=shot, bible=_bible(), visual_anchors=[],
    ))

    assert len(calls) == 2
    assert qa["geometry_guard"]["passed"] is False
    assert qa["relative_height_match"] == 0.2
    assert "relative_scale_mismatch" in qa["hard_failures"]
    assert keyframe_gate_passed(qa) is True


@pytest.mark.parametrize(
    "text,expected",
    [
        ("手没有真正碰到石碑", "approach"),
        ("还没能碰到对方", "approach"),
        ("差一点碰到石碑", "approach"),
        ("几乎碰到他的手", "approach"),
        ("即将触碰石碑", "approach"),
        ("试图抓住对方", "approach"),
        ("禁止触碰石碑", "none"),
        ("两人牵着手", "established"),
        ("她靠在他肩头", "established"),
        ("毫不费力抓住对方", "established"),
        ("忍不住抓住对方", "established"),
        ("手掌紧贴石碑", "established"),
        ("手掌贴着石碑", "established"),
        ("椅子靠在墙边", "none"),
        ("并不触碰石碑", "approach"),
        ("未触及石碑", "approach"),
        ("没有退缩便抓住对方", "established"),
        ("他想也不想便抓住她", "established"),
        ("他想都没想便抓住她", "established"),
        ("她靠着他肩头", "established"),
        ("接触到事情的真相", "none"),
        ("手掌悬停在他肩前", "approach"),
        ("她握紧他的手", "established"),
        ("她握着他的手", "established"),
        ("她牵着他的手", "established"),
        ("她抱起受伤的他", "established"),
        ("她攥住他的手腕", "established"),
        ("她拽住他的衣袖", "established"),
        ("她打了他一巴掌", "established"),
        ("她揪住他的衣领", "established"),
        ("她扣住他的手腕", "established"),
        ("她踩住他的脚", "established"),
        ("她托起他的下巴", "established"),
        ("她捧着他的脸", "established"),
        ("她扑进他怀里", "established"),
        ("她吻上他的嘴唇", "established"),
        ("她抓着他的手", "established"),
        ("她抱着他", "established"),
        ("她搂着他的肩", "established"),
        ("她推了对方一把", "established"),
        ("一拳打在对方胸口", "established"),
        ("他一巴掌扇在对方脸上", "established"),
        ("他一巴掌扇向对方", "approach"),
        ("他踢中对方膝盖", "established"),
        ("她把手搭在他肩上", "established"),
        ("他掐住对方的脖子", "established"),
        ("他背起受伤的同伴", "established"),
        ("她抓住他后又松开手", "separated"),
        ("她松开原本握住他的手", "separated"),
        ("她不肯放开他的手", "established"),
        ("她不愿松开他的手", "established"),
        ("她紧紧抓住他，绝不放开", "established"),
        ("她甩开他的手", "separated"),
        ("她缩回手", "separated"),
        ("她撤回手", "separated"),
        ("她抽出被握住的手", "separated"),
        ("阳光打在她脸上", "none"),
        ("灯光投影打在墙上", "none"),
        ("雨点打在窗上", "none"),
        ("他背着书包走进教室", "none"),
    ],
)
def test_contact_phase_does_not_turn_attempts_into_established_contact(text: str, expected: str) -> None:
    assert contact_action_phase(text) == expected


def test_aborted_contact_keeps_visible_gap_and_side_camera() -> None:
    shot = _contact_shot(
        primary_action="萧炎试图抓住落下的令牌。",
        action_desc="萧炎伸手抓向令牌，但未能接住。",
        last_frame_desc="令牌从萧炎指尖前落下，他仍未碰到它。",
        state_out="萧炎的手与令牌仍有清晰缝隙。",
    )
    contract = keyframe_visual_contract(shot, _bible())
    prompt = video_modes.reference_generation_prompt(shot, _bible(), "plot_key_frame", 1)

    assert contract["target_source"] == "last_frame_desc"
    assert contract["target_contact_phase"] == "approach"
    assert contract["contact_camera_required"] is True
    assert contract["established_contact_required"] is False
    assert "do not invent a touch, catch, or impact" in prompt


def test_release_end_state_beats_earlier_established_contact() -> None:
    shot = _contact_shot(
        primary_action="萧炎抓住对方的手。",
        action_desc="萧炎先抓住对方，随后松开手。",
        last_frame_desc="萧炎已松开对方的手，两手之间留有空隙。",
        state_out="两人的手已分开。",
    )
    contract = keyframe_visual_contract(shot, _bible())
    prompt = video_modes.reference_generation_prompt(shot, _bible(), "plot_key_frame", 1)

    assert contract["target_source"] == "last_frame_desc"
    assert contract["target_contact_phase"] == "separated"
    assert contract["established_contact_required"] is False
    assert "after release/separation" in prompt


def test_incidental_start_contact_does_not_hijack_new_keyframe() -> None:
    shot = _contact_shot(
        primary_action="萧炎起身走到窗边。",
        action_desc="萧炎放下茶杯，起身走到窗边看雨。",
        first_frame_desc="萧炎仍握着茶杯坐在桌前。",
        state_in="萧炎仍握着茶杯坐在桌前。",
        last_frame_desc="萧炎已站在窗前看雨。",
        state_out="萧炎站在窗前。",
    )
    contract = keyframe_visual_contract(shot, _bible())

    assert contract["target_keyframe_desc"] == shot.last_frame_desc
    assert contract["target_contact_phase"] == "none"
    assert contract["contact_camera_required"] is False


def test_height_evidence_requires_a_real_relative_height_relation() -> None:
    for description in ("萧炎仰头看向天空", "萧炎俯身看地上的信", "萧炎比起昨天更高兴"):
        shot = _contact_shot(
            characters=["萧炎", "萧薰儿"], characters_visible=["萧炎", "萧薰儿"],
            action_desc=description, primary_action=description,
            first_frame_desc=description, last_frame_desc=description,
            state_in=description, state_out=description,
        )
        assert explicit_height_difference_evidence(shot, _bible()) == []
        assert keyframe_visual_contract(shot, _bible())["relative_height_policy"] == "equal_scale"

    taller = _contact_shot(
        characters=["萧炎", "萧薰儿"], characters_visible=["萧炎", "萧薰儿"],
        action_desc="萧炎比萧薰儿高一头。", primary_action="两人并肩站立。",
        last_frame_desc="两人并肩站立。",
    )
    assert explicit_height_difference_evidence(taller, _bible())


def test_offscreen_child_does_not_disable_equal_scale_for_visible_adults() -> None:
    shot = _contact_shot(
        characters=["萧炎", "萧薰儿", "孩童"],
        characters_visible=["萧炎", "萧薰儿"],
    )
    contract = keyframe_visual_contract(shot, _bible())
    prompt = video_modes.reference_generation_prompt(shot, _bible(), "plot_key_frame", 1)

    assert contract["visible_characters"] == ["萧炎", "萧薰儿"]
    assert contract["relative_height_policy"] == "equal_scale"
    assert "孩童" not in prompt


def test_collective_roster_is_a_group_not_one_identity(monkeypatch) -> None:
    shot = _contact_shot(
        characters=["萧家子弟", "萧薰儿"],
        characters_visible=["萧家子弟", "萧薰儿"],
        primary_action="几名萧家子弟交头接耳。",
        action_desc="萧家子弟们纷纷议论，其中一人回头，背景中萧薰儿沉默站立。",
        last_frame_desc="众人仍在议论，其中一人回头，背景中萧薰儿沉默站立。",
        state_out="萧家子弟们仍在议论。",
    )
    contract = keyframe_visual_contract(shot, _bible())
    prompt = video_modes.reference_generation_prompt(shot, _bible(), "plot_key_frame", 1)

    assert is_collective_role("萧家子弟") is True
    assert is_collective_role("一名观众") is False
    assert is_collective_role("两名弟子") is True
    assert contract["individual_visible_characters"] == ["萧薰儿"]
    assert contract["collective_visible_roles"] == ["萧家子弟"]
    assert contract["collective_presence_required"] is True
    assert contract["relative_height_policy"] == "single_subject"
    assert "Scripted collective/group roles: 萧家子弟" in prompt
    assert "never as one fixed identity" in prompt
    assert "Named/individual visible identities, each exactly once: 萧家子弟" not in prompt
    assert "Named/individual visible identities, each exactly once: 萧薰儿" in prompt

    monkeypatch.setattr(
        "app.multiview.project_bible_asset_names",
        lambda _project, **_kwargs: ({"萧薰儿"}, set()),
    )
    monkeypatch.setattr("app.multiview.portrait_views_for_episode", lambda *_a, **_k: [])
    monkeypatch.setattr("app.multiview.portrait_row_for_episode", lambda *_a, **_k: None)
    manifest = resolve_shot_asset_dependencies(
        project_id="p", episode_no=1, shot_id="s", shot=shot,
    )
    group = next(item for item in manifest["characters"] if item["name"] == "萧家子弟")
    assert group["role_kind"] == "collective"
    assert group["asset_required"] is False
    assert group["selected_views"] == []


def test_background_crowd_permission_is_not_a_false_presence_requirement() -> None:
    optional = _contact_shot(
        characters=["萧炎"], characters_visible=["萧炎"],
        scene_setting="萧炎当众站在广场上。",
        action_desc="萧炎抬头看向石碑。",
        primary_action="萧炎抬头看向石碑。",
        last_frame_desc="萧炎独自站在石碑前。",
        state_out="萧炎独自站在石碑前。",
    )
    optional_contract = keyframe_visual_contract(optional, _bible())
    assert optional_contract["anonymous_background_allowed"] is True
    assert optional_contract["collective_presence_required"] is False

    required = optional.model_copy(update={"last_frame_desc": "众人围观中，萧炎站在石碑前。"})
    required_contract = keyframe_visual_contract(required, _bible())
    required_prompt = video_modes.reference_generation_prompt(required, _bible(), "plot_key_frame", 1)
    assert required_contract["collective_presence_required"] is True
    assert "anonymous crowd is REQUIRED" in required_prompt

    real_shot_wording = optional.model_copy(update={
        "last_frame_desc": "身后族人纷纷跟着发出嘲笑声，萧炎仍站在石碑前。",
    })
    assert keyframe_visual_contract(real_shot_wording, _bible())["collective_presence_required"] is True

    for wording in (
        "萧炎站立，身后密集的家族子弟交头接耳。",
        "萧炎站立，周围族人跟着发出嘲笑声。",
    ):
        variant = optional.model_copy(update={"last_frame_desc": wording})
        variant_contract = keyframe_visual_contract(variant, _bible())
        variant_prompt = video_modes.reference_generation_prompt(variant, _bible(), "plot_key_frame", 1)
        assert variant_contract["collective_presence_required"] is True
        assert "No additional recognizable person." not in variant_prompt

    for wording in (
        "一群族人站在萧炎身后。",
        "几名弟子站在门口。",
        "身后站着几名弟子。",
        "周围站满了族人。",
        "族人列队站在两侧。",
    ):
        variant = optional.model_copy(update={"last_frame_desc": wording})
        assert keyframe_visual_contract(variant, _bible())["collective_presence_required"] is True


@pytest.mark.parametrize(
    "wording",
    [
        "众人已经散去，萧炎独自站在石碑前。",
        "人群已经全部离开画面。",
        "家族子弟已退到画外，只剩萧炎一人。",
        "画外传来人群的嘲笑声。",
        "萧炎回忆起众人的嘲笑。",
    ],
)
def test_absent_offscreen_or_remembered_crowd_is_forbidden_not_required(wording: str) -> None:
    shot = _contact_shot(
        characters=["萧炎"], characters_visible=["萧炎"],
        primary_action="萧炎独立。", action_desc=wording,
        last_frame_desc=wording, state_out=wording,
    )
    contract = keyframe_visual_contract(shot, _bible())
    prompt = video_modes.reference_generation_prompt(shot, _bible(), "plot_key_frame", 1)

    assert contract["collective_presence_required"] is False
    assert contract["collective_presence_forbidden"] is True
    assert "anonymous crowd is REQUIRED" not in prompt
    assert "NO CROWD IN FRAME" in prompt


def test_real_group_wording_is_a_required_qa_diagnostic(monkeypatch) -> None:
    captured: dict = {}

    async def fake_vlm(_frames, expectation, call_meta=None):
        captured.update(json.loads(expectation))
        return json.dumps({
            "action_match": 0.9, "body_proportion": 0.9,
            "collective_presence_match": 0.2,
            "face_identity": 0.9, "outfit_match": 0.9, "hair_match": 0.9,
            "scene_match": 0.9, "hard_failures": [], "issues": [],
        })

    monkeypatch.setattr("app.hiagent.vlm_check", fake_vlm)
    monkeypatch.setattr("app.multiview.visual_evidence_qa_enabled", lambda: True)
    shot = _contact_shot(
        characters=["萧炎"], characters_visible=["萧炎"],
        primary_action="萧炎沉默地站在石碑前。",
        action_desc="萧炎沉默地站在石碑前，族人从身后走来。",
        last_frame_desc="身后族人纷纷跟着发出嘲笑声，萧炎仍站在石碑前。",
        state_out="萧炎站在石碑前。",
    )

    qa = asyncio.run(review_keyframe_with_evidence(
        "candidate", shot=shot, bible=_bible(), visual_anchors=[],
    ))

    assert captured["shot"]["collective_presence_required"] is True
    assert captured["output_schema"]["collective_presence_match"] == 0.0
    assert "collective_group_missing" in qa["hard_failures"]


def test_required_text_is_only_forced_when_active_at_target_instant() -> None:
    early = _contact_shot(
        primary_action="萧炎抬手按住尚未发光的石碑。",
        last_frame_desc="",
        state_out="",
        required_text=RequiredOnScreenText(
            surface="石碑", exact_text="斗之气：七段", strategy="embedded_prop",
            appear_start_s=4.0,
        ),
    )
    early_contract = keyframe_visual_contract(early, _bible())
    early_prompt = video_modes.reference_generation_prompt(early, _bible(), "plot_key_frame", 1)
    assert early_contract["target_source"] == "primary_action"
    assert early_contract["required_text_expected"] is False
    assert "the only permitted text" not in early_prompt

    end = _contact_shot(required_text=RequiredOnScreenText(
        surface="石碑", exact_text="斗之气：七段", strategy="embedded_prop",
        appear_start_s=4.0,
    ))
    end_contract = keyframe_visual_contract(end, _bible())
    end_prompt = video_modes.reference_generation_prompt(end, _bible(), "plot_key_frame", 1)
    assert end_contract["required_text_expected"] is True
    assert "the only permitted text is the exact string '斗之气：七段'" in end_prompt


@pytest.mark.parametrize(
    "appear_start,stable_until,expected",
    [
        (5.0, 5.0, True),
        (5.001, None, False),
        (0.0, 5.0, True),
        (0.0, 4.999, False),
    ],
)
def test_required_text_end_target_respects_inclusive_timing_boundaries(
    appear_start: float, stable_until: float | None, expected: bool,
) -> None:
    shot = _contact_shot(required_text=RequiredOnScreenText(
        surface="石碑", exact_text="斗之气：七段", strategy="embedded_prop",
        appear_start_s=appear_start, stable_until_s=stable_until,
    ))
    assert keyframe_visual_contract(shot, _bible())["required_text_expected"] is expected


def test_keyframe_qa_hides_inactive_text_payload_and_explicitly_forbids_text(monkeypatch) -> None:
    captured: dict = {}

    async def fake_vlm(_frames, expectation, call_meta=None):
        captured.update(json.loads(expectation))
        return json.dumps({
            "action_match": 0.9,
            "body_proportion": 0.9,
            "side_view_match": 0.9,
            "contact_visibility": 0.9,
            "contact_phase_match": 0.9,
            "face_identity": 0.9,
            "outfit_match": 0.9,
            "hair_match": 0.9,
            "scene_match": 0.9,
            "hard_failures": [],
            "issues": [],
        })

    monkeypatch.setattr("app.hiagent.vlm_check", fake_vlm)
    monkeypatch.setattr("app.multiview.visual_evidence_qa_enabled", lambda: True)
    shot = _contact_shot(
        primary_action="萧炎抬手按住尚未发光的石碑。",
        last_frame_desc="",
        state_out="",
        required_text=RequiredOnScreenText(
            surface="石碑", exact_text="斗之气：七段", appear_start_s=4.0,
        ),
    )

    asyncio.run(review_keyframe_with_evidence(
        "candidate", shot=shot, bible=_bible(), visual_anchors=[],
    ))

    assert captured["shot"]["required_text_expected"] is False
    assert captured["shot"]["required_text"] is None
    assert "目标定格时刻不应出现画面文字" in " ".join(captured["geometry_requirements"])
    assert captured["output_schema"]["required_text_match"] == "N/A"


def test_provider_boundary_keeps_contract_seeds_and_unique_history(monkeypatch, tmp_path) -> None:
    prompts: list[str] = []

    async def fake_generate(prompt, seed_inputs, *, call_meta=None):
        prompts.append(prompt)
        return {"b64_json": base64.b64encode(b"image").decode("ascii")}

    monkeypatch.setattr(video_modes, "_generate_image_with_seed_fallback", fake_generate)
    monkeypatch.setattr(
        video_modes, "reference_image_path",
        lambda *_a, **_k: tmp_path / "100_plot_key_frame.jpg",
    )
    kwargs = dict(
        project_id="p", episode_no=1, shot=_contact_shot(), bible=_bible(),
        ref_type="plot_key_frame", index=100,
        content_override="Front-facing giant character lineup.",
        seed_inputs=["seed-a"], extra_instruction="REFERENCE IMAGE ROLE MAP: input image 1 = character '萧炎', profile",
        skip_inline_qa=True,
    )
    first = asyncio.run(video_modes._generate_one_reference(**kwargs))
    second = asyncio.run(video_modes._generate_one_reference(**kwargs))

    assert first.path != second.path
    assert first.path and second.path
    assert "SIDE CAMERA REQUIRED" in prompts[0]
    assert "Reference images lock identity, outfit, style, and environment only" in prompts[0]
    assert "input image 1 = character '萧炎', profile" in prompts[0]


def test_missing_geometry_diagnostics_are_unverified(monkeypatch) -> None:
    async def fake_vlm(*_args, **_kwargs):
        return json.dumps({
            "action_match": 0.95, "body_proportion": 0.95,
            "face_identity": 0.95, "outfit_match": 0.95, "hair_match": 0.95,
            "scene_match": 0.95, "hard_failures": [], "issues": [],
        })

    monkeypatch.setattr("app.hiagent.vlm_check", fake_vlm)
    monkeypatch.setattr("app.multiview.visual_evidence_qa_enabled", lambda: True)
    qa = asyncio.run(review_keyframe_with_evidence(
        "candidate", shot=_contact_shot(
            characters=["萧炎", "萧薰儿"], characters_visible=["萧炎", "萧薰儿"],
        ), bible=_bible(), visual_anchors=[],
    ))

    assert qa["status"] == "unverified"
    assert qa["overall"] is None
    assert "diagnostic_missing" in qa["hard_failures"]
