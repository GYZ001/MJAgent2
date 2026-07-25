import json
import sqlite3

import pytest

from app import config
from app.compiler import CompileError, SOURCE_EXCERPT_MARKER, compile_prompt
from app.continuity import (
    action_capacity_errors,
    classify_video_hard_failures,
    information_items_for_shot,
    information_ledger_errors,
    preflight_seedance_gates,
    reference_role_plan,
    resolve_do_not_repeat_texts,
    speech_capacity_errors,
    uses_previous_tail_frame,
)
from app.evidence import media
from app.schemas import (
    Bible,
    Character,
    Dialogue,
    EpisodeScreenplay,
    InformationItem,
    RequiredOnScreenText,
    Shot,
    Storyboard,
    World,
)


def _bible() -> Bible:
    return Bible(
        characters=[
            Character(
                name="林风",
                role="主角",
                appearance_canonical="二十岁青年，黑色束发，青色劲装，腰间佩短剑，眼神坚毅",
                personality="果断",
                speech_style="短句直接",
            ),
            Character(
                name="苏婉",
                role="同伴",
                appearance_canonical="年轻女性，乌发半挽，白色长裙配银色发簪，神情冷静",
                personality="冷静",
                speech_style="语气沉稳",
            ),
        ],
        world=World(era="架空古代", genre="玄幻", visual_style_canonical="3D国漫电影感，暖色侧光，材质细腻"),
    )


def _shot(**overrides) -> Shot:
    data = dict(
        shot_no=1,
        duration_s=5,
        shot_size="中景",
        camera_move="固定",
        scene_setting="夜，山门前",
        characters=["林风"],
        action_desc="林风抬手按住山门铜环，停住呼吸观察门缝里的光。",
        first_frame_desc="林风站在山门前，右手刚抬向铜环。",
        last_frame_desc="林风按住铜环，目光锁住门缝里的微光。",
        source_excerpt="林风站在山门前，按住铜环看向门缝。",
        narration=None,
        dialogues=[],
        transition="硬切",
        continuity_from_prev=False,
        state_in="林风站在山门前，右手刚抬向铜环。",
        primary_action="林风抬手按住山门铜环，停住呼吸观察门缝里的光。",
        state_out="林风按住铜环，目光锁住门缝里的微光。",
        continuity_mode="same_scene_cut",
        characters_visible=["林风"],
        audio_cast=[],
    )
    data.update(overrides)
    return Shot(**data)


def test_5s_multi_action_rejected_by_capacity_and_preflight() -> None:
    shot = _shot(
        duration_s=5,
        action_desc="林风走进山门，然后伸手触碰石碑，接着转身喊出名字，随后举起短剑。",
        primary_action="林风走进山门，然后伸手触碰石碑，接着转身喊出名字，随后举起短剑。",
    )

    direct = action_capacity_errors(shot)
    preflight = preflight_seedance_gates(shot)

    assert any("顺序动作节拍" in err for err in direct)
    assert any("顺序动作节拍" in err for err in preflight)


def test_only_action_continuation_uses_previous_tail_frame() -> None:
    expected = {
        "action_continuation": True,
        "same_scene_cut": False,
        "reaction_cut": False,
        "reverse_angle": False,
        "insert_detail": False,
        "scene_change": False,
    }

    assert {mode: uses_previous_tail_frame(mode) for mode in expected} == expected


def test_compile_prompt_excludes_source_excerpt_and_prev_full_action_when_not_continuation() -> None:
    prev_action = "林风上一镜完整地拔剑、跃起、击退守卫并冲进山门。"
    shot = _shot(
        source_excerpt="林风按住铜环，听见门后传来细微响动，这段原文不得进入最终提示。",
        continuity_mode="reaction_cut",
    )

    prompt = compile_prompt(
        shot,
        _bible(),
        prev_action=prev_action,
        prev_tail_action=prev_action,
        continuity_mode="reaction_cut",
    )

    assert SOURCE_EXCERPT_MARKER not in prompt
    assert "source_excerpt" not in prompt
    assert "这段原文不得进入最终提示" not in prompt
    assert prev_action not in prompt


def test_required_text_none_bans_text_but_required_text_allows_exact_text_without_conflict() -> None:
    no_text = compile_prompt(_shot(required_text=None), _bible())
    assert "画面中不出现任何文字" in no_text

    with_text_shot = _shot(
        required_text=RequiredOnScreenText(
            surface="山门木牌",
            exact_text="禁地",
            appear_start_s=0.5,
            stable_until_s=4.0,
            style="古朴刻字",
        )
    )
    with_text = compile_prompt(with_text_shot, _bible())

    assert "禁地" in with_text
    assert "画面中不出现任何文字" not in with_text
    assert "除「禁地」外不要出现任何其他文字" in with_text


def test_offscreen_speaker_keeps_speaker_id_not_narration() -> None:
    shot = _shot(
        characters=["林风"],
        characters_visible=["林风"],
        audio_cast=["苏婉"],
        dialogues=[Dialogue(speaker="苏婉", line="别碰那扇门", emotion="惊恐", delivery="offscreen_voice")],
    )

    prompt = compile_prompt(shot, _bible())

    assert "苏婉在画外" in prompt
    assert "不得改成通用旁白" in prompt
    assert "旁白用独立叙述者嗓音念「别碰那扇门」" not in prompt


def test_speech_over_capacity_fails_speech_capacity_and_preflight() -> None:
    shot = _shot(
        duration_s=5,
        dialogues=[Dialogue(speaker="林风", line="这扇门背后藏着我们寻找多年的真相现在必须立刻进去确认", emotion="坚定")],
    )

    direct = speech_capacity_errors(shot)
    preflight = preflight_seedance_gates(shot)

    assert any("超过 5s 可用说话容量" in err for err in direct)
    assert any("超过 5s 可用说话容量" in err for err in preflight)


def test_repeated_info_id_without_reinforcement_fails_ledger_and_preflight() -> None:
    first = _shot(shot_no=1, new_information_ids=["info-door"])
    second = _shot(
        shot_no=2,
        new_information_ids=["info-door"],
        state_in="林风按住铜环，目光锁住门缝里的微光。",
        scene_setting=first.scene_setting,
    )
    screenplay = EpisodeScreenplay(
        episode_no=1,
        information_ledger=[
            InformationItem(info_id="info-door", content="山门背后有异常微光", reinforcement_allowed=False)
        ],
    )

    ledger_errors = information_ledger_errors(Storyboard(episode_no=1, shots=[first, second]), screenplay)
    preflight = preflight_seedance_gates(second, prev=first, screenplay=screenplay, delivered_info_ids={"info-door"})

    assert any("重复交付" in err for err in ledger_errors)
    assert any("new_information_ids 含已交付信息 info-door" in err for err in preflight)


def test_legacy_information_id_gets_chinese_display_content() -> None:
    shot = _shot(
        new_information_ids=["xiaoyan_test_result_3duan"],
        purpose="公布萧炎的斗气测试结果为三段",
    )

    items = information_items_for_shot(shot)

    assert items == [{
        "info_id": "xiaoyan_test_result_3duan",
        "content": "公布萧炎的斗气测试结果为三段",
        "source": "derived",
    }]


def test_do_not_repeat_ids_resolve_to_chinese_before_seedance() -> None:
    prior = _shot(
        shot_no=1,
        new_information_ids=["world_setup_qidou_mainland", "xiaoyan_status_testing"],
        purpose="建立斗气大陆规则并交代萧炎正在接受测试",
    )
    current = _shot(
        shot_no=2,
        do_not_repeat=["world_setup_qidou_mainland", "xiaoyan_status_testing", "unknown_raw_id"],
    )

    resolved = resolve_do_not_repeat_texts(current, prior_shots=[prior])
    current.do_not_repeat = resolved
    prompt = compile_prompt(current, _bible())

    assert resolved == ["建立斗气大陆规则并交代萧炎正在接受测试"]
    assert "不要重复：建立斗气大陆规则并交代萧炎正在接受测试" in prompt
    assert "world_setup_qidou_mainland" not in prompt
    assert "xiaoyan_status_testing" not in prompt
    assert "unknown_raw_id" not in prompt


def test_ledger_chinese_content_takes_priority_over_legacy_fallback() -> None:
    screenplay = EpisodeScreenplay(
        episode_no=1,
        information_ledger=[InformationItem(
            info_id="I1",
            content="石碑显示萧炎的斗气测试结果为三段",
        )],
    )
    shot = _shot(
        new_information_ids=["I1"],
        purpose="泛化的镜头目的",
    )

    assert information_items_for_shot(shot, screenplay)[0]["content"] == "石碑显示萧炎的斗气测试结果为三段"


def test_required_prompt_sections_not_truncated_when_required_content_over_limit(monkeypatch) -> None:
    monkeypatch.setattr(config, "PROMPT_CHAR_LIMIT", 420)
    shot = _shot(
        duration_s=10,
        state_in="起始状态" + "非常长" * 40,
        primary_action="主动作" + "必须完整保留" * 40,
        state_out="结束状态" + "不得截断" * 40,
        dialogues=[Dialogue(speaker="林风", line="必须完整说完这句关键台词", emotion="坚定")],
        required_text=RequiredOnScreenText(surface="石碑", exact_text="天门已开"),
    )

    with pytest.raises(CompileError, match="必填提示词段落总长"):
        compile_prompt(shot, _bible())


def test_reference_role_plan_sequence_for_continuity_modes() -> None:
    sequence = [
        ("reaction_cut", False),
        ("same_scene_cut", False),
        ("action_continuation", True),
        ("insert_detail", False),
    ]

    for mode, needs_tail in sequence:
        shot = _shot(continuity_mode=mode)
        roles = reference_role_plan(shot, continuity_mode=mode)
        assert ("start_state_reference" in roles) is needs_tail
        assert "scene_reference" in roles
        assert "character_identity:林风" in roles


def test_classify_video_hard_failures_detects_story_repeat_and_related_failures() -> None:
    failures = classify_video_hard_failures(
        {
            "overall": 0.8,
            "issues": ["画面重演上一镜动作", "字幕文字乱码"],
            "failure_types": ["future_leak"],
            "start_state_match": 0.3,
        },
        technical={"passed": False},
    )

    assert "story_repeat" in failures
    assert "future_leak" in failures
    assert "text_error" in failures
    assert "state_mismatch" in failures
    assert "needs_crop" in failures


def test_select_best_video_candidate_rejects_below_threshold_and_hard_failures(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
      CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
      CREATE TABLE shots(id TEXT PRIMARY KEY,episode_id TEXT,adopted_version_id TEXT);
      CREATE TABLE shot_versions(id TEXT PRIMARY KEY,shot_id TEXT,version_no INTEGER,status TEXT,
        technical_validation_json TEXT,qa_json TEXT,adoption_reason TEXT);
      INSERT INTO settings VALUES('auto_retake_threshold','0.8');
      INSERT INTO shots VALUES('s','e',NULL);
    """)
    technical = json.dumps({"passed": True})
    conn.execute(
        "INSERT INTO shot_versions VALUES('low','s',1,'succeeded',?,?,NULL)",
        (technical, json.dumps({"overall": 0.7})),
    )
    conn.execute(
        "INSERT INTO shot_versions VALUES('hard','s',2,'succeeded',?,?,NULL)",
        (technical, json.dumps({"overall": 0.95, "failure_types": ["story_repeat"]})),
    )
    conn.commit()
    monkeypatch.setattr(media, "get_conn", lambda: conn)
    monkeypatch.setattr(media, "get_setting", lambda key: "true" if key == "video_hard_gate_enabled" else None)

    selected = media.select_best_video_candidate("s")

    assert selected is None
    assert conn.execute("SELECT adopted_version_id FROM shots WHERE id='s'").fetchone()[0] is None
