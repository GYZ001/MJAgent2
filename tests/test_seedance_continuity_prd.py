import json
import sqlite3

import pytest

from app import config
from app.compiler import CompileError, SOURCE_EXCERPT_MARKER, compile_prompt
from app.continuity import (
    action_capacity_errors,
    classify_video_hard_failures,
    derive_continuity_mode,
    forbidden_prompt_content_errors,
    information_items_for_shot,
    information_ledger_errors,
    preflight_seedance_gates,
    reference_role_plan,
    resolve_do_not_repeat_texts,
    speech_capacity_errors,
    sync_shot_continuity_fields,
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
    Scene,
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


def test_short_primary_action_uses_complete_action_desc_for_generation() -> None:
    action_desc = "林风收起迟疑神情，认真看向画外同伴，开口承诺日后归还借款。"
    shot = _shot(
        action_desc=action_desc,
        primary_action="林风承诺还钱",
    )

    sync_shot_continuity_fields(shot)

    assert shot.primary_action == action_desc
    assert not any(
        "primary_action 缺失或过短" in error
        for error in preflight_seedance_gates(shot)
    )


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


def test_compile_prompt_locks_scene_canonical_and_persistent_landmark_geometry() -> None:
    bible = _bible().model_copy(update={
        "scenes": [Scene(
            name="山门",
            scene_canonical="青石山门中央固定矗立一块黑色试炼石碑，两侧各有一根铜柱，夜间冷色灯火稳定。",
            landmarks=["中央黑色试炼石碑", "左右铜柱"],
        )]
    })
    shot = _shot(
        scene_setting="夜，山门",
        action_desc="林风从黑色试炼石碑前收回手，转身走向后方台阶。",
        primary_action="林风收回手后转身走向台阶。",
        first_frame_desc="林风站在中央黑色试炼石碑左侧，右手贴着碑面。",
        last_frame_desc="同一机位，林风走向后方台阶，中央黑色试炼石碑仍留在原位。",
    )

    prompt = compile_prompt(shot, bible)

    assert "[PERSISTENT SCENE GEOMETRY]" in prompt
    assert "场景固定锚点：青石山门中央固定矗立一块黑色试炼石碑" in prompt
    assert "显式固定地标：中央黑色试炼石碑、左右铜柱" in prompt
    assert "不得消失、复制、变形、换位后再出现" in prompt
    assert "头部突然放大或幼态大头" in prompt


def test_action_capacity_uses_detailed_action_not_only_primary_summary() -> None:
    shot = _shot(
        duration_s=5,
        primary_action="林风完成测试并退场。",
        action_desc="林风点头，收回手，转身穿过人群，走到队尾后停下。",
    )

    errors = action_capacity_errors(shot)

    assert any("顺序动作节拍" in error for error in errors)


def test_required_text_defaults_to_deterministic_insert_and_keeps_raw_video_textless() -> None:
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
    assert "精确中文由服务端确定性插入" in with_text
    assert "不要生成字幕、乱码、可读道具字样或水印" in with_text
    assert "除「禁地」外不要出现任何其他文字" not in with_text


def test_embedded_prop_text_remains_an_explicit_opt_in() -> None:
    shot = _shot(required_text=RequiredOnScreenText(
        surface="山门木牌", exact_text="禁地", strategy="embedded_prop",
    ))

    prompt = compile_prompt(shot, _bible())

    assert "除「禁地」外不要出现任何其他文字" in prompt


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

    assert any("超过 5s 口播上限" in err for err in direct)
    assert any("超过 5s 口播上限" in err for err in preflight)


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


def test_select_best_video_candidate_defers_then_force_selects_by_risk(monkeypatch) -> None:
    """仍有重试预算时不抢先采用；收口时综合风险优先于原始总分。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
      CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
      CREATE TABLE shots(id TEXT PRIMARY KEY,episode_id TEXT,adopted_version_id TEXT);
      CREATE TABLE shot_versions(id TEXT PRIMARY KEY,shot_id TEXT,version_no INTEGER,status TEXT,
        technical_validation_json TEXT,qa_json TEXT,adoption_reason TEXT,image_inputs TEXT);
      INSERT INTO settings VALUES('auto_retake_threshold','0.8');
      INSERT INTO settings VALUES('video_auto_retake_limit','2');
      INSERT INTO shots VALUES('s','e',NULL);
    """)
    technical = json.dumps({"passed": True})
    conn.execute(
        "INSERT INTO shot_versions VALUES('low','s',1,'succeeded',?,?,NULL,?)",
        (technical, json.dumps({"overall": 0.7}), json.dumps({"auto_retake_count": 0})),
    )
    conn.execute(
        "INSERT INTO shot_versions VALUES('hard','s',2,'succeeded',?,?,NULL,?)",
        (
            technical,
            json.dumps({"overall": 0.95, "failure_types": ["story_repeat"]}),
            json.dumps({"auto_retake_count": 0}),
        ),
    )
    conn.commit()
    monkeypatch.setattr(media, "get_conn", lambda: conn)
    monkeypatch.setattr(media, "get_setting", lambda key: {
        "video_hard_gate_enabled": "true",
        "video_auto_retake_limit": "2",
    }.get(key))
    monkeypatch.setattr(media, "grade_shot_video", lambda *a, **k: {"grade": "B"})
    monkeypatch.setattr(media, "merge_observed_state_out_into_shot_contract", lambda *a, **k: None)

    selected = media.select_best_video_candidate("s")

    assert selected is None
    assert conn.execute("SELECT adopted_version_id FROM shots WHERE id='s'").fetchone()[0] is None
    forced = media.select_best_video_candidate("s", force_best=True)
    assert forced and forced["version_id"] == "low"
    assert forced["fallback"] is True
    assert conn.execute("SELECT adopted_version_id FROM shots WHERE id='s'").fetchone()[0] == "low"


def test_select_best_video_candidate_force_adopts_lowest_risk_when_retakes_exhausted(monkeypatch) -> None:
    """重抽名额用尽后采纳技术合格中综合风险最低者，而非盲选总分最高者。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
      CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
      CREATE TABLE shots(id TEXT PRIMARY KEY,episode_id TEXT,adopted_version_id TEXT);
      CREATE TABLE shot_versions(id TEXT PRIMARY KEY,shot_id TEXT,version_no INTEGER,status TEXT,
        technical_validation_json TEXT,qa_json TEXT,adoption_reason TEXT,image_inputs TEXT);
      INSERT INTO settings VALUES('auto_retake_threshold','0.8');
      INSERT INTO settings VALUES('video_auto_retake_limit','2');
      INSERT INTO shots VALUES('s','e',NULL);
    """)
    technical = json.dumps({"passed": True})
    conn.execute(
        "INSERT INTO shot_versions VALUES('low','s',1,'succeeded',?,?,NULL,?)",
        (technical, json.dumps({"overall": 0.55}), json.dumps({"auto_retake_count": 1})),
    )
    conn.execute(
        "INSERT INTO shot_versions VALUES('best_low','s',2,'succeeded',?,?,NULL,?)",
        (
            technical,
            json.dumps({"overall": 0.72, "failure_types": ["story_repeat"]}),
            json.dumps({"auto_retake_count": 2}),
        ),
    )
    conn.commit()
    monkeypatch.setattr(media, "get_conn", lambda: conn)
    monkeypatch.setattr(media, "get_setting", lambda key: {
        "video_hard_gate_enabled": "true",
        "video_auto_retake_limit": "2",
    }.get(key))
    import app.artifacts
    monkeypatch.setattr(app.artifacts, "invalidate_episode_final", lambda _: False)

    selected = media.select_best_video_candidate("s", force_best=True)

    assert selected and selected["version_id"] == "low"
    assert selected["fallback"] is True
    assert "综合风险最低" in selected["reason"]
    assert conn.execute("SELECT adopted_version_id FROM shots WHERE id='s'").fetchone()[0] == "low"


def test_select_best_video_candidate_force_best_adopts_single_below_threshold(monkeypatch) -> None:
    """单条低分候选先留给重试；预算耗尽后必须自动采用。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
      CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
      CREATE TABLE shots(id TEXT PRIMARY KEY,episode_id TEXT,adopted_version_id TEXT);
      CREATE TABLE shot_versions(id TEXT PRIMARY KEY,shot_id TEXT,version_no INTEGER,status TEXT,
        technical_validation_json TEXT,qa_json TEXT,adoption_reason TEXT,image_inputs TEXT);
      INSERT INTO settings VALUES('auto_retake_threshold','0.8');
      INSERT INTO shots VALUES('s','e',NULL);
    """)
    technical = json.dumps({"passed": True})
    conn.execute(
        "INSERT INTO shot_versions VALUES('only','s',1,'succeeded',?,?,NULL,?)",
        (technical, json.dumps({"overall": 0.4}), json.dumps({"auto_retake_count": 0})),
    )
    conn.commit()
    monkeypatch.setattr(media, "get_conn", lambda: conn)
    monkeypatch.setattr(media, "get_setting", lambda key: "true" if key == "video_hard_gate_enabled" else None)

    selected = media.select_best_video_candidate("s")
    assert selected is None
    assert conn.execute("SELECT adopted_version_id FROM shots WHERE id='s'").fetchone()[0] is None
    forced = media.select_best_video_candidate("s", force_best=True)
    assert forced and forced["version_id"] == "only"


def test_action_continuation_without_prev_downgrades_instead_of_chain_head_error() -> None:
    """ERR-20260725-f8d7ad：缺 prev 时不得保留 action_continuation，也不应报「第一个镜头」。"""
    shot = _shot(
        shot_no=2,
        continuity_mode="action_continuation",
        continuity_from_prev=True,
        state_in="林风按住铜环，目光锁住门缝里的微光。",
        primary_action="林风慢慢推开山门，侧耳听门内动静。",
        state_out="山门半开，林风停在门缝旁。",
        action_desc="林风慢慢推开山门，侧耳听门内动静。",
        first_frame_desc="林风按住铜环，目光锁住门缝里的微光。",
        last_frame_desc="山门半开，林风停在门缝旁。",
    )

    assert derive_continuity_mode(shot, prev=None) == "same_scene_cut"
    preflight = preflight_seedance_gates(shot, prev=None)
    assert not any("第一个镜头没有上一镜可承接" in err for err in preflight)
    assert shot.continuity_mode == "same_scene_cut"
    assert shot.continuity_from_prev is False


def test_action_continuation_with_prev_still_allowed() -> None:
    first = _shot(
        shot_no=1,
        continuity_mode="scene_change",
        state_out="林风按住铜环，目光锁住门缝里的微光。",
        last_frame_desc="林风按住铜环，目光锁住门缝里的微光。",
    )
    second = _shot(
        shot_no=2,
        continuity_mode="action_continuation",
        continuity_from_prev=True,
        state_in="林风按住铜环，目光锁住门缝里的微光。",
        primary_action="林风慢慢推开山门，侧耳听门内动静。",
        state_out="山门半开，林风停在门缝旁。",
        action_desc="林风慢慢推开山门，侧耳听门内动静。",
        first_frame_desc="林风按住铜环，目光锁住门缝里的微光。",
        last_frame_desc="山门半开，林风停在门缝旁。",
        scene_setting=first.scene_setting,
        characters=["林风"],
        characters_visible=["林风"],
    )

    assert derive_continuity_mode(second, prev=first) == "action_continuation"
    preflight = preflight_seedance_gates(second, prev=first)
    assert not any("第一个镜头没有上一镜可承接" in err for err in preflight)
    assert second.continuity_mode == "action_continuation"


def test_dialogue_matching_source_excerpt_prefix_is_not_forbidden_leak() -> None:
    """ERR-20260725-f24b91：台词与 source_excerpt 前缀相同且只出现在对白中，不得误杀。"""
    line = "萧炎哥哥，以前你曾经与薰儿说过，要能放下，才能拿起，提放自如，是自在人！"
    excerpt = line + "萧薰儿微笑着柔声道，略微稚嫩的嗓音，却是暖人心肺。"
    shot = _shot(
        shot_no=10,
        characters=["林风", "苏婉"],
        characters_visible=["林风", "苏婉"],
        source_excerpt=excerpt,
        dialogues=[Dialogue(speaker="苏婉", line=line, emotion="坚定")],
        continuity_mode="same_scene_cut",
        duration_s=10,
        primary_action="苏婉认真注视林风，开口引用他曾经说过的话。",
        action_desc="苏婉认真注视林风，开口引用他曾经说过的话。",
        state_in="林风低着头；苏婉站在他面前准备回应。",
        state_out="苏婉说完，目光坚定望着林风。",
        first_frame_desc="苏婉站在林风面前，认真注视着低头的他。",
        last_frame_desc="苏婉目光坚定望着林风，嘴唇微合刚说完话。",
    )

    prompt = compile_prompt(shot, _bible(), continuity_mode="same_scene_cut")
    assert line in prompt
    assert SOURCE_EXCERPT_MARKER not in prompt
    assert not any("source_excerpt 原文内容" in err for err in forbidden_prompt_content_errors(prompt, shot))
    assert not any("source_excerpt 原文内容" in err for err in preflight_seedance_gates(shot, prompt_text=prompt))


def test_source_excerpt_in_action_block_still_forbidden() -> None:
    excerpt = "林风按住铜环，听见门后传来细微响动，这段原文不得进入画面描述。"
    shot = _shot(
        source_excerpt=excerpt,
        primary_action=excerpt,
        action_desc=excerpt,
        continuity_mode="same_scene_cut",
    )
    prompt = compile_prompt(shot, _bible(), continuity_mode="same_scene_cut")
    assert any("source_excerpt 原文内容" in err for err in forbidden_prompt_content_errors(prompt, shot))


def test_source_excerpt_middle_segment_is_also_forbidden() -> None:
    excerpt = (
        "林风先抬头确认山门无人值守，随后按住铜环，"
        "听见门后传来连续而细微的脚步声，最后退到石阶边缘。"
    )
    shot = _shot(source_excerpt=excerpt, continuity_mode="same_scene_cut")
    middle = excerpt[18:18 + 30]

    errors = forbidden_prompt_content_errors(f"画面动作：{middle}", shot)

    assert any("source_excerpt 原文内容" in err for err in errors)


def test_final_prompt_scrubber_preserves_allowed_dialogue_and_removes_other_excerpt() -> None:
    from app.compiler import ensure_source_excerpt_in_prompt

    line = "苏婉认真提醒林风，眼前山门机关一旦触发便不可逆转。"
    excerpt = line + "她说完后退到石阶边缘，右手仍紧握剑柄。"
    shot = _shot(
        source_excerpt=excerpt,
        dialogues=[Dialogue(speaker="苏婉", line=line, emotion="坚定")],
        continuity_mode="same_scene_cut",
    )
    leaked_middle = excerpt[len(line):]
    prompt = f"[AUDIO TIMELINE]\n苏婉「{line}」\n\n[ONE CURRENT ACTION]\n{leaked_middle}"

    scrubbed = ensure_source_excerpt_in_prompt(prompt, shot)

    assert line in scrubbed
    assert leaked_middle not in scrubbed
    assert not forbidden_prompt_content_errors(scrubbed, shot)


def test_preflight_does_not_leak_prev_shot_errors() -> None:
    """上一镜若误带 action_continuation，不应污染当前镜 preflight。"""
    prev = _shot(
        shot_no=1,
        continuity_mode="action_continuation",
        continuity_from_prev=True,
    )
    cur = _shot(
        shot_no=2,
        continuity_mode="same_scene_cut",
        state_in="林风按住铜环，目光锁住门缝里的微光。",
        scene_setting=prev.scene_setting,
    )
    errors = preflight_seedance_gates(cur, prev=prev)
    assert not any("shot_no=1" in err for err in errors)
    assert not any("第一个镜头没有上一镜可承接" in err for err in errors)
