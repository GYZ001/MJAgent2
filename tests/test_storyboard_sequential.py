"""逐镜头分镜（sequential storyboard）路径的单测：

覆盖 codex 新增的「按顺序逐镜生成 + 单镜 QA」改造与本次修复：
- 单镜（非收尾）QA 只拦当前镜与承接问题，整集级检查（镜头数/总时长/关键内容）放行；
- 自愿收尾时若整集必保留内容还没补齐，不硬塞单镜而是要求继续补镜；
- 撞到大纲末镜/技术硬上限（must_finish）才对主线缺口硬失败，禁止氛围声轨逼出计划外镜；
- 分镜进度按已通过镜头的模型选择时长求和。
"""

import asyncio

import pytest

from app import config, stages
from app.compiler import CompileError
from app.harness.types import Issue, IssueSeverity
from app.schemas import (Bible, Character, Dialogue, EpisodeScreenplay,
                         InformationItem, Scene, Shot, StoryboardOutline,
                         StoryboardOutlineShot, NarrativeContinuityPlan, World)
from app.stages import (StoryboardShotDraft, _storyboard_progress_block,
                        _project_shot_scene_from_outline,
                        _relevant_text_windows, _render_completed_shots_context,
                        _storyboard_shot_visual_identity_issues,
                        _validate_storyboard_shot_draft)

KEY_LINE = "我一定要查清斗气消失的真相。"


def _bible() -> Bible:
    return Bible(
        characters=[
            Character(
                name="萧炎",
                role="主角",
                appearance_canonical="十五岁少年，黑色短发，青色长袍，眉眼坚定，腰悬玉佩",
                personality="坚韧",
                speech_style="短句直接",
            )
        ],
        world=World(era="架空", genre="玄幻", visual_style_canonical="国漫厚涂风，暖灰色调，电影感光影"),
    )


def _screenplay(*, key_lines: list[str] | None = None) -> EpisodeScreenplay:
    return EpisodeScreenplay(
        episode_no=2,
        title="测试集",
        logline="萧炎在嘲讽中立誓查清斗气消失的真相。",
        full_script_text="【场1】日 / 萧家广场\n萧炎攥紧拳头看向碑石。",
        key_lines=key_lines if key_lines is not None else [KEY_LINE],
        ending_hook="斗气消失的真相仍未揭开。",
    )


def _shot(no: int, *, narration: str | None = None, dialogues: list[Dialogue] | None = None) -> Shot:
    return Shot(
        shot_no=no,
        duration_s=5,
        shot_size="中景",
        camera_move="固定",
        scene_setting="日，萧家广场",
        characters=["萧炎"],
        action_desc="萧炎站在测验碑前缓缓攥紧手掌，萧炎抬眼扫过四周议论的人群，掌心因用力而发白。",
        first_frame_desc="萧炎站在测验碑前，手掌贴着碑面，神情平静。",
        last_frame_desc="同一机位，萧炎手掌攥成拳，眼神转冷。",
        source_excerpt="少年面无表情，唇角有着一抹自嘲，缓缓攥紧了手掌。",
        narration=narration,
        dialogues=dialogues or [],
    )


def _episode() -> dict:
    return {"episode_no": 2, "target_duration_s": 50}


def _draft(shot: Shot, *, is_final: bool) -> StoryboardShotDraft:
    return StoryboardShotDraft(episode_no=2, shot=shot, is_final=is_final)


@pytest.mark.parametrize("field", ["first_frame_desc", "last_frame_desc"])
def test_storyboard_shot_draft_rejects_missing_production_frame(field: str) -> None:
    shot = _shot(1)
    setattr(shot, field, "   ")

    with pytest.raises(ValueError, match="分镜生产必填字段"):
        StoryboardShotDraft(episode_no=2, shot=shot, is_final=False)


def test_downstream_prompt_compile_failure_is_a_structural_loop_issue(
    monkeypatch,
) -> None:
    def fail_compile(*_args, **_kwargs):
        raise CompileError("可见身份没有 typed identity contract")

    monkeypatch.setattr("app.compiler.compile_prompt", fail_compile)
    findings = _validate(
        _draft(_shot(1), is_final=False),
        allow_finish=False,
        must_finish=False,
        screenplay=_screenplay(),
    )

    issue = next(item for item in findings if isinstance(item, Issue))
    assert issue.code == "SHOT_PROMPT_COMPILE_FAILED"
    assert issue.category == "structural"
    assert issue.severity == IssueSeverity.BLOCKER


def test_prompt_compile_probe_cannot_mutate_candidate_continuity(
    monkeypatch,
) -> None:
    screenplay = _screenplay()
    screenplay.narrative_plan = NarrativeContinuityPlan(scope_id="e2")
    previous = _shot(1)
    current = _shot(2)
    current.continuity_mode = "same_scene_cut"
    current.continuity_from_prev = True

    def mutate_probe(shot, *_args, **_kwargs):
        shot.continuity_from_prev = False
        return "compiled"

    monkeypatch.setattr("app.compiler.compile_prompt", mutate_probe)
    errors = _validate_storyboard_shot_draft(
        _draft(current, is_final=False),
        episode={"id": "e2", **_episode()},
        bible=_bible(),
        screenplay=screenplay,
        completed_shots=[previous],
        shot_no=2,
        allow_finish=False,
        must_finish=False,
        narrative_authority=True,
    )

    assert current.continuity_from_prev is True
    assert not any(
        "continuity_from_prev=false" in str(error)
        for error in errors
    )


def test_shot_visual_identity_must_belong_to_current_narrative_task() -> None:
    bible = _bible()
    bible.characters.append(Character(
        name="旁场人物",
        role="另一场戏中的人物",
        appearance_canonical="成年男子，灰色长袍，短发",
    ))
    screenplay = _screenplay()
    screenplay.narrative_plan = NarrativeContinuityPlan(scope_id="e2")
    shot = _shot(1)
    shot.characters = ["萧炎", "旁场人物"]
    shot.characters_visible = ["萧炎", "旁场人物"]
    task = StoryboardOutlineShot(
        shot_no=1,
        visible_entity_ids=["萧炎"],
        audio_cast=["旁场人物"],
    )

    issues = _storyboard_shot_visual_identity_issues(
        shot,
        task,
        bible,
        screenplay,
        episode_id="e2",
    )

    assert [issue.code for issue in issues] == [
        "SHOT_VISIBLE_IDENTITY_NOT_GROUNDED"
    ]
    assert issues[0].category == "structural"
    assert issues[0].severity == IssueSeverity.BLOCKER
    assert issues[0].evidence["task_identity_ids"] == ["萧炎"]
    assert issues[0].evidence["unexpected_identity_ids"] == ["旁场人物"]
    assert "offscreen_voice" in issues[0].repair_hint
    assert "lip_sync=false" in issues[0].repair_hint


def test_shot_visual_identity_gate_reads_visual_prose_not_only_cast_lists() -> None:
    bible = _bible()
    bible.characters.append(Character(
        name="未来出场者",
        role="后续事件人物",
        appearance_canonical="成年人物，深色长袍，外观稳定清晰",
    ))
    screenplay = _screenplay()
    screenplay.narrative_plan = NarrativeContinuityPlan(scope_id="e2")
    shot = _shot(1)
    shot.characters = ["萧炎"]
    shot.characters_visible = ["萧炎"]
    shot.action_desc = "未来出场者站在萧炎身旁，听完他的话后闭口作出反应。"
    task = StoryboardOutlineShot(
        shot_no=1,
        visible_entity_ids=["萧炎"],
    )

    issues = _storyboard_shot_visual_identity_issues(
        shot,
        task,
        bible,
        screenplay,
        episode_id="e2",
    )

    assert [issue.code for issue in issues] == [
        "SHOT_VISIBLE_IDENTITY_NOT_GROUNDED"
    ]
    assert issues[0].evidence["unexpected_identity_ids"] == ["未来出场者"]


def _validate(draft: StoryboardShotDraft, *, allow_finish: bool, must_finish: bool,
             screenplay: EpisodeScreenplay, completed: list[Shot] | None = None) -> list[str]:
    return _validate_storyboard_shot_draft(
        draft,
        episode=_episode(),
        bible=_bible(),
        screenplay=screenplay,
        completed_shots=completed or [],
        shot_no=draft.shot.shot_no,
        allow_finish=allow_finish,
        must_finish=must_finish,
    )


def test_progress_block_has_no_episode_duration_limit() -> None:
    nearly_full = [_shot(i) for i in range(1, 14)]
    for shot in nearly_full:
        shot.duration_s = 5
    low = _storyboard_progress_block(nearly_full)
    assert "13" in low and "65s" in low and "duration_s" in low
    assert "默认 5s" in low and "不设数量上限" in low
    assert "软预算" not in low
    plenty = _storyboard_progress_block([])
    assert "duration_s" in plenty and "is_final=true" in plenty


def test_completed_context_exposes_state_handoff_not_full_action_history() -> None:
    """逐镜上下文只传承接状态摘要，避免把完整 action_desc 历史喂给模型重演。"""
    shots = [_shot(i) for i in range(1, 5)]
    for shot in shots:
        shot.state_out = f"镜{shot.shot_no}结束状态：萧炎停在石碑前"
        shot.continuity_mode = "same_scene_cut"
    rendered = _render_completed_shots_context(shots)

    assert '"承接状态"' in rendered
    assert '"continuity_mode"' in rendered
    assert '"action_desc"' not in rendered
    assert all(f'"shot_no": {i}' in rendered for i in range(1, 5))
    assert "镜4结束状态" in rendered


def test_relevant_text_windows_keeps_current_hint_and_caps_context() -> None:
    text = "开场铺垫。" * 900 + "谷言终于拿起储物柜钥匙。" + "尾声铺垫。" * 900
    result = _relevant_text_windows(text, ["谷言拿起储物柜钥匙"], max_chars=1800)

    assert "储物柜钥匙" in result
    assert len(result) <= 1850  # 含窗口之间的省略标记


def test_per_shot_fallback_projects_scene_from_approved_outline() -> None:
    bible = _bible()
    bible.scenes.append(Scene(
        name="萧家广场",
        scene_canonical="萧家广场固定石碑、观众席与青石地面空间锚点",
    ))
    brief = StoryboardOutlineShot(
        shot_no=1,
        scene_time="白天",
        scene_name="萧家广场",
        scene_setting="白天，萧家广场",
        beat="萧炎在石碑前攥拳",
    )
    shot = _shot(1)
    shot.scene_time = ""
    shot.scene_name = ""
    shot.scene_setting = ""

    assert _project_shot_scene_from_outline(shot, brief, bible)
    assert shot.scene_time == "白天"
    assert shot.scene_name == "萧家广场"
    assert shot.scene_setting == "白天，萧家广场"


def test_next_shot_uses_bounded_single_shot_output_budget(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_agent_loop(*_args, **kwargs):
        captured["max_tokens"] = kwargs["max_tokens"]
        captured["max_iterations"] = kwargs["loop"].policy.max_iterations
        captured["repair_all_blockers"] = kwargs["loop"].policy.repair_all_blockers
        return _draft(_shot(1), is_final=False)

    monkeypatch.setattr(stages, "_run_with_agent_loop", fake_agent_loop)
    result = asyncio.run(stages.generate_storyboard_next_shot(
        {
            "id": "e2",
            "episode_no": 2,
            "title": "测试集",
            "hook": "碑石亮起",
            "cliffhanger": "真相仍未揭开",
            "target_duration_s": 50,
        },
        "少年面无表情，唇角有着一抹自嘲，缓缓攥紧了手掌。",
        _bible(),
        "",
        _screenplay(),
        [],
    ))

    assert result.shot.shot_no == 1
    assert captured["max_tokens"] == config.STORYBOARD_SHOT_MAX_TOKENS == 8192
    assert captured["max_tokens"] < 65535
    assert captured["max_iterations"] == 4
    assert captured["repair_all_blockers"] is True


def test_storyboard_outline_uses_32k_output_budget() -> None:
    assert config.STORYBOARD_OUTLINE_MAX_TOKENS == 32768


def test_invalid_legacy_outline_information_id_is_not_injected_after_validation(monkeypatch) -> None:
    async def fake_agent_loop(*_args, **_kwargs):
        return _draft(_shot(1), is_final=False)

    screenplay = _screenplay()
    screenplay.information_ledger = [
        InformationItem(info_id="I1", event_id="E1", content="合法信息")
    ]
    outline = StoryboardOutline(episode_no=2, shots=[StoryboardOutlineShot(
        shot_no=1,
        beat="萧炎离开人群",
        information_ids=["INFO_03"],
    )])
    monkeypatch.setattr(stages, "_run_with_agent_loop", fake_agent_loop)

    result = asyncio.run(stages.generate_storyboard_next_shot(
        {"id": "e2", "episode_no": 2, "title": "测试集", "target_duration_s": 50},
        "少年面无表情，转身走到队伍最后。",
        _bible(),
        "",
        screenplay,
        [],
        outline=outline,
    ))

    assert result.shot.information_ids == []
    assert result.shot.new_information_ids == []


def test_unknown_storyboard_identity_is_returned_for_model_repair_without_deleting_dialogue() -> None:
    shot = _shot(1, dialogues=[Dialogue(speaker="绿袍男子", line="此路不通。")])
    shot.characters = ["萧炎", "绿袍男子"]
    shot.characters_visible = ["萧炎", "绿袍男子"]
    original_dialogue = shot.dialogues[0].model_dump(mode="json")

    errors = _validate(
        _draft(shot, is_final=False),
        allow_finish=False,
        must_finish=False,
        screenplay=_screenplay(),
    )

    assert any("未在剧本阶段解析的人物身份：绿袍男子" in error for error in errors)
    assert shot.characters == ["萧炎", "绿袍男子"]
    assert shot.characters_visible == ["萧炎", "绿袍男子"]
    assert shot.dialogues[0].model_dump(mode="json") == original_dialogue


# ---------- 单镜 QA 的整集级放行 / 收尾分支 ----------

def test_partial_nonfinal_skips_episode_level_checks() -> None:
    # 非收尾镜：即便关键台词还没出现，也不应报"镜头数/关键内容/继续补镜"。
    errors = _validate(_draft(_shot(1), is_final=False),
                       allow_finish=False, must_finish=False, screenplay=_screenplay())
    assert not any(e.startswith("镜头数 ") for e in errors)
    assert not any(e.startswith("分镜丢失了剧本标记的") for e in errors)
    assert not any("继续补镜" in e for e in errors)


def test_final_shot_narrative_gate_uses_compiled_outline(monkeypatch) -> None:
    screenplay = _screenplay()
    screenplay.narrative_plan = NarrativeContinuityPlan(scope_id="e2")
    compiled_outline = StoryboardOutline(episode_no=2)
    calls: list[tuple[StoryboardOutline | None, bool]] = []

    def capture_gate(
        _board,
        _screenplay,
        *,
        outline=None,
        complete=True,
        expected_scope_id=None,
    ):
        calls.append((outline, complete))
        return []

    monkeypatch.setattr(
        "app.narrative.validate_storyboard_narrative",
        capture_gate,
    )

    _validate_storyboard_shot_draft(
        _draft(_shot(1), is_final=True),
        episode=_episode(),
        bible=_bible(),
        screenplay=screenplay,
        completed_shots=[],
        shot_no=1,
        allow_finish=True,
        must_finish=True,
        outline=compiled_outline,
    )

    assert calls == [(compiled_outline, True)]


def test_current_outline_covers_are_checked_before_final_shot() -> None:
    # 本镜大纲声明要落实的内容必须在当前镜正文中出现，不能拖到收尾时才发现漏戏。
    errors = _validate(
        _draft(_shot(1), is_final=False),
        allow_finish=False,
        must_finish=False,
        screenplay=_screenplay(),
    )
    assert not any("未落实本镜大纲 covers" in e for e in errors)

    errors = _validate_storyboard_shot_draft(
        _draft(_shot(1), is_final=False),
        episode=_episode(),
        bible=_bible(),
        screenplay=_screenplay(),
        completed_shots=[],
        shot_no=1,
        allow_finish=False,
        must_finish=False,
        outline_covers="中年测验员宣读萧炎斗之力三段并定性为低级",
    )
    assert any("未落实本镜大纲 covers" in e for e in errors)


def test_voluntary_final_with_missing_key_content_asks_to_continue() -> None:
    # 自愿收尾但关键台词缺失：要求改判 is_final=false 继续补镜，而不是硬塞单镜。
    errors = _validate(_draft(_shot(1), is_final=True),
                       allow_finish=True, must_finish=False, screenplay=_screenplay())
    assert any("继续补镜" in e for e in errors)
    assert not any(e.startswith("分镜丢失了剧本标记的") for e in errors)


def test_must_finish_hard_fails_on_missing_key_content() -> None:
    # 已到收束位：没有后续镜头分担，主线台词缺失必须硬失败。
    errors = _validate(_draft(_shot(1), is_final=True),
                       allow_finish=True, must_finish=True, screenplay=_screenplay())
    assert any(e.startswith("分镜丢失了剧本标记的") for e in errors)
    assert not any("继续补镜" in e for e in errors)


def test_must_finish_does_not_ask_to_continue_for_soft_soundtrack_gap() -> None:
    """大纲末镜 must_finish：氛围/对白密度软缺口不再逼「继续补镜」（防计划外幻觉镜）。"""
    # 关键台词已落地，但若仅有声轨密度类缺口，must_finish 路径不应再发继续补镜。
    shot = _shot(1, dialogues=[Dialogue(speaker="萧炎", line=KEY_LINE, emotion="坚定")])
    errors = _validate(_draft(shot, is_final=True),
                       allow_finish=True, must_finish=True, screenplay=_screenplay())
    assert not any("继续补镜" in e for e in errors)


def test_must_finish_rejects_missing_is_final_flag() -> None:
    shot = _shot(1, dialogues=[Dialogue(speaker="萧炎", line=KEY_LINE, emotion="坚定")])
    errors = _validate(_draft(shot, is_final=False),
                       allow_finish=True, must_finish=True, screenplay=_screenplay())
    assert any("必须收束" in e and "is_final=true" in e for e in errors)


def test_allows_shot_reusing_previous_source_excerpt() -> None:
    # Renderability：相邻镜允许共享同一主线段落 source_excerpt，不再因此拒收。
    prev = _shot(1)
    cur = _shot(2)  # 与 prev 用同一段 source_excerpt
    errors = _validate(_draft(cur, is_final=False),
                       allow_finish=False, must_finish=False, screenplay=_screenplay(), completed=[prev])
    assert not any("source_excerpt 与上一镜几乎相同" in e for e in errors)


def test_final_passes_when_key_content_present() -> None:
    # 关键台词已写进收尾镜台词：不应再要求继续补镜，也不报关键内容缺失。
    shot = _shot(1, dialogues=[Dialogue(speaker="萧炎", line=KEY_LINE, emotion="坚定")])
    errors = _validate(_draft(shot, is_final=True),
                       allow_finish=True, must_finish=False, screenplay=_screenplay())
    assert not any("继续补镜" in e for e in errors)
    assert not any(e.startswith("分镜丢失了剧本标记的") for e in errors)
