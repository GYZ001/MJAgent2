"""逐镜头分镜（sequential storyboard）路径的单测：

覆盖 codex 新增的「按顺序逐镜生成 + 单镜 QA」改造与本次修复：
- 单镜（非收尾）QA 只拦当前镜与承接问题，整集级检查（镜头数/总时长/关键内容）放行；
- 自愿收尾时若整集必保留内容还没补齐，不硬塞单镜而是要求继续补镜；
- 撞到大纲末镜/技术硬上限（must_finish）才对主线缺口硬失败，禁止氛围声轨逼出计划外镜；
- 分镜进度按已通过镜头的模型选择时长求和。
"""

import asyncio

import pytest

from app import config, stages, validators
from app.compiler import CompileError
from app.continuity import outline_atomic_errors
from app.harness.types import Issue, IssueSeverity
from app.schemas import (Bible, Character, Dialogue, EpisodeScreenplay,
                         InformationItem, Scene, Shot, StoryboardContextRequirement,
                         StoryboardOutline, StoryboardOutlineShot,
                         StoryboardSceneContext, NarrativeContinuityPlan,
                         RequiredOnScreenText, World)
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


def test_shot_visual_identity_gate_does_not_treat_required_text_as_cast() -> None:
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
    shot.visible_entity_ids = ["萧炎"]
    shot.action_desc = "片头卷轴上显现“未来出场者”，随后墨迹停稳。"
    shot.required_text = RequiredOnScreenText(
        surface="片头卷轴",
        exact_text="未来出场者",
        strategy="deterministic_insert",
    )
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

    assert issues == []


def test_shot_visual_identity_gate_marks_unsatisfied_text_surface_conflict() -> None:
    bible = _bible()
    bible.characters.append(Character(
        name="未来出场者",
        role="后续事件人物",
        appearance_canonical="成年人物，深色长袍，外观稳定清晰",
    ))
    screenplay = _screenplay()
    screenplay.narrative_plan = NarrativeContinuityPlan(scope_id="e2")
    shot = _shot(1)
    shot.characters = ["萧炎", "未来出场者"]
    shot.characters_visible = ["萧炎", "未来出场者"]
    shot.visible_entity_ids = ["萧炎", "未来出场者"]
    shot.required_text = RequiredOnScreenText(
        surface="未来出场者",
        exact_text="额间印记",
        strategy="embedded_prop",
    )
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

    assert len(issues) == 1
    assert issues[0].repairable is False
    assert issues[0].evidence["authority_conflicts"] == [{
        "preserve_path": "required_text",
        "remove_identity_id": "未来出场者",
        "reason": "文字承载面要求该身份可见",
    }]


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


def test_director_outline_prompt_states_full_speech_budget_table(monkeypatch) -> None:
    """大纲提示词必须给出与逐镜提示词同源的完整口播预算换算表。

    根因：校验器 outline_key_line_capacity_errors 按每镜实际 duration_s 计算容量
    （5s=18字...10s=36字），但大纲提示词过去只告诉模型 10s 那一档天花板，导致模型
    合理地以为"不超过 36 字即可"，选了偏短的 duration_s 后台词超限。这里断言的是
    从 config.max_spoken_chars_for_duration 取值动态生成的换算表，不是硬编码的
    "18/21/25" 字面量——公式一改这条测试也要能感知偏差。
    """
    captured: dict[str, object] = {}

    async def fake_agent_loop(*args, **_kwargs):
        captured["prompt"] = args[2]
        return StoryboardOutline(episode_no=1, shots=[])

    monkeypatch.setattr(stages, "_run_with_agent_loop", fake_agent_loop)

    # narrative_plan 默认 None：这条路径才是当前会真正调用模型的大纲生成器
    # （app.stages._generate_episode_director_outline）。narrative_plan 非空时
    # generate_storyboard_outline 会走确定性编译分支，直接 return，永远不会
    # 执行到后面那段带 prompt 字符串的旧代码——这段旧代码目前是死代码。
    screenplay = _screenplay()
    assert screenplay.narrative_plan is None

    asyncio.run(stages.generate_storyboard_outline(
        {"episode_no": 1, "title": "测试集", "target_duration_s": 50},
        "少年面无表情，缓缓攥紧了手掌。",
        _bible(),
        "",
        screenplay,
    ))

    prompt = captured["prompt"]
    assert isinstance(prompt, str)

    # 完整换算表：与 config.max_spoken_chars_for_duration 同源，覆盖全部合法档位。
    for duration in sorted(config.ALLOWED_DURATIONS):
        expected_cap = config.max_spoken_chars_for_duration(duration)
        assert f"{duration}s≤{expected_cap}字" in prompt, (
            f"大纲提示词缺少 {duration}s 档位的口播预算（应为 {expected_cap} 字）"
        )

    # 因果方向：先数台词字数，再选能装下的时长；不是先选时长再检查。
    assert "key_line_ids 合计口播纯文字字数决定 duration_s 下限" in prompt
    # 保留原有"超限拆镜"出路，并补上"选择更长时长"这条此前未告知模型的出路。
    assert "挪到相邻镜" in prompt
    assert f"{config.MAX_SPOKEN_CHARS_PER_SHOT}字" in prompt


def _outline_key_lines_screenplay() -> EpisodeScreenplay:
    """两句不同说话人的关键台词，供说话人/台词归属类校验测试复用。"""
    return EpisodeScreenplay(
        episode_no=1,
        title="测试集",
        key_lines=["甲：这是甲说的第一句台词。", "乙：这是乙说的第二句台词。"],
        full_script_text="【场1】日 / 测试场景\n甲和乙在场景中对话。",
        ending_hook="留待下集揭晓。",
    )


def _capture_director_outline_prompt(monkeypatch, screenplay: EpisodeScreenplay) -> str:
    """跑一次 generate_storyboard_outline，拦截真正发给模型的大纲提示词文本。"""
    captured: dict[str, object] = {}

    async def fake_agent_loop(*args, **_kwargs):
        captured["prompt"] = args[2]
        captured["validate"] = args[4]
        return StoryboardOutline(episode_no=1, shots=[])

    monkeypatch.setattr(stages, "_run_with_agent_loop", fake_agent_loop)
    asyncio.run(stages.generate_storyboard_outline(
        {"episode_no": 1, "title": "测试集", "target_duration_s": 50},
        "正文占位", _bible(), "", screenplay,
    ))
    prompt = captured["prompt"]
    assert isinstance(prompt, str)
    return prompt, captured["validate"]


def test_director_outline_prompt_declares_beat_length_and_no_stalling(monkeypatch) -> None:
    """校验器 validate_storyboard_outline 要求 beat >= 若干字且不得与上一镜几乎逐字重复
    （app/validators.py ~5497/~5500）。过去提示词只在 JSON schema 写了 "beat": str，
    一个字没提这条约束——EP6 run_eee27c4130a3 真实撞过
    「大纲第 2/3/4/5/6 镜 beat 过短或缺失」。

    该阈值是 validators.py 里的裸字面量，而 validators.py 属于本次禁止改动的文件，
    所以这里不引入共享常量，改为直接探测校验器的真实行为，防止提示词字面量与
    校验器字面量各写各的、以后各自漂移都没人发现。
    """
    screenplay = _outline_key_lines_screenplay()

    def _beat_too_short(chars: int) -> bool:
        shot = StoryboardOutlineShot(shot_no=1, beat="字" * chars, scene_id="SC001")
        outline = StoryboardOutline(episode_no=1, shots=[shot])
        errors = validators.validate_storyboard_outline(outline, screenplay, 50, bible=None)
        return any("beat 过短或缺失" in e for e in errors)

    min_ok = next((n for n in range(0, 12) if not _beat_too_short(n)), None)
    assert min_ok, "校验器前提已变化：beat 过短判定测不到边界，需要重新核对本测试"

    # 反停留：相邻镜 beat 几乎逐字重复也是硬错误。
    stalled = StoryboardOutline(episode_no=1, shots=[
        StoryboardOutlineShot(shot_no=1, beat="他缓缓走向窗边看着远方发呆"),
        StoryboardOutlineShot(shot_no=2, beat="他缓缓走向窗边看着远方发呆"),
    ])
    stall_errors = validators.validate_storyboard_outline(stalled, screenplay, 50, bible=None)
    assert any("停留在同一节拍" in e for e in stall_errors)

    prompt, _validate = _capture_director_outline_prompt(monkeypatch, screenplay)
    assert f"不少于 {min_ok} 字" in prompt, (
        f"提示词未声明与校验器同源的 beat 最短字数（校验器实测 {min_ok} 字）"
    )
    assert "谁做了什么" in prompt and "局势如何变化" in prompt, "未复用校验器给出的范例措辞"
    assert "逐字重复" in prompt, "未声明相邻镜 beat 反停留约束"


def test_director_outline_prompt_declares_state_in_out_must_differ(monkeypatch) -> None:
    """continuity.outline_atomic_errors 要求 state_in != state_out（app/continuity.py:1810 起）；
    过去 JSON schema 只把两者列成普通字符串字段，一个字没提必须不同。"""
    screenplay = _outline_key_lines_screenplay()
    same_state = StoryboardOutline(episode_no=1, shots=[
        StoryboardOutlineShot(
            shot_no=1, beat="有效的剧情推进内容示例",
            state_in="他站在门口张望", state_out="他站在门口张望",
        ),
    ])
    errors = outline_atomic_errors(same_state)
    assert any("state_in 与 state_out 相同" in e for e in errors), (
        "校验器前提已变化：state_in==state_out 不再报错，需要重新核对本测试"
    )

    prompt, _validate = _capture_director_outline_prompt(monkeypatch, screenplay)
    assert "state_in" in prompt and "state_out" in prompt and "真实差异" in prompt


def test_director_outline_prompt_declares_single_speaker_per_shot(monkeypatch) -> None:
    """outline_key_line_speaker_errors 禁止同一镜混入多个说话人的关键台词
    （app/validators.py:4637）；这是上一轮 agent 发现但未修的两处遗漏之一——
    活代码提示词里完全没提，死代码分支的 4b 条反而写清楚了。"""
    screenplay = _outline_key_lines_screenplay()
    mixed = StoryboardOutline(episode_no=1, shots=[
        StoryboardOutlineShot(
            shot_no=1, beat="甲乙两人先后发言的镜头示例",
            key_line_ids=["KL01", "KL02"],
        ),
    ])
    errors = validators.outline_key_line_speaker_errors(mixed, screenplay)
    assert any("OUTLINE_KEY_LINE_SPEAKER_MIXED" in e for e in errors), (
        "校验器前提已变化：混合说话人不再报错，需要重新核对本测试"
    )

    prompt, _validate = _capture_director_outline_prompt(monkeypatch, screenplay)
    assert "同一镜 key_line_ids 只能属于同一说话人" in prompt


def test_director_outline_prompt_declares_key_line_single_owner(monkeypatch) -> None:
    """同一条关键台词只能分配给一镜：outline_key_line_capacity_errors 的
    OUTLINE_KEY_LINE_OWNER_DUPLICATE（app/validators.py ~4505）覆盖全集范围内的重复分配，
    validate_storyboard_outline 里的相邻镜检查（~5505）是它的子集。两条提示词过去都没提。"""
    screenplay = _outline_key_lines_screenplay()
    duplicated = StoryboardOutline(episode_no=1, shots=[
        StoryboardOutlineShot(shot_no=1, beat="甲说出关键台词的镜头", key_line_ids=["KL01"], duration_s=5),
        StoryboardOutlineShot(shot_no=2, beat="甲的台词又被重复说一次", key_line_ids=["KL01"], duration_s=5),
    ])
    capacity_errors = validators.outline_key_line_capacity_errors(duplicated, screenplay)
    assert any("OUTLINE_KEY_LINE_OWNER_DUPLICATE" in e for e in capacity_errors), (
        "校验器前提已变化：重复分配关键台词不再报错，需要重新核对本测试"
    )
    adjacent_errors = validators.validate_storyboard_outline(duplicated, screenplay, 50, bible=None)
    assert any("重复分配关键台词" in e for e in adjacent_errors)

    prompt, _validate = _capture_director_outline_prompt(monkeypatch, screenplay)
    assert "每条关键台词" in prompt and "只能分配给一镜" in prompt


def test_director_outline_prompt_declares_shot_no_must_be_sequential(monkeypatch) -> None:
    """validate_storyboard_outline 要求 shot_no 从 1 连续递增（app/validators.py ~5493）；
    死代码分支第 1 条已有"shot_no 从 1 连续递增"这句现成措辞，活代码提示词没有照抄。"""
    screenplay = _outline_key_lines_screenplay()
    skipped = StoryboardOutline(episode_no=1, shots=[
        StoryboardOutlineShot(shot_no=1, beat="第一镜的有效剧情内容"),
        StoryboardOutlineShot(shot_no=3, beat="跳号后的第二个镜头内容"),
    ])
    errors = validators.validate_storyboard_outline(skipped, screenplay, 50, bible=None)
    assert any("shot_no 必须为连续递增" in e for e in errors), (
        "校验器前提已变化：shot_no 跳号不再报错，需要重新核对本测试"
    )

    prompt, _validate = _capture_director_outline_prompt(monkeypatch, screenplay)
    assert "shot_no 从 1 连续递增" in prompt


def test_director_outline_prompt_declares_context_state_and_delivery(monkeypatch) -> None:
    """_generate_episode_director_outline 自带的本地 _validate 闭包（不在 validators.py 里，
    随本次提示词改动一起补齐）要求：
    1. scene_contexts.entry_state/exit_state 不少于 _OUTLINE_CONTEXT_STATE_MIN_CHARS 字；
    2. 每条 context_requirements 必须被某一镜的 context_requirement_ids 实际交付。
    两个阈值/规则与提示词共用同一个模块级常量，不存在裸字面量各写各的风险。
    """
    screenplay = _outline_key_lines_screenplay()
    prompt, validate = _capture_director_outline_prompt(monkeypatch, screenplay)

    too_short = "短" * (stages._OUTLINE_CONTEXT_STATE_MIN_CHARS - 1)
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=1, beat="有效的剧情推进内容示例", scene_id="SC001",
                context_requirement_ids=[],
            ),
        ],
        scene_contexts=[
            StoryboardSceneContext(
                scene_id="SC001", scene_no=1,
                entry_state=too_short, exit_state=too_short,
                context_requirements=[
                    StoryboardContextRequirement(
                        requirement_id="CTX-SC001-01", description="需要建立时间地点",
                    ),
                ],
            ),
        ],
    )
    errors = validate(outline)
    assert any("缺少可执行的 entry_state/exit_state" in e for e in errors)
    assert any("导演规划未安排上下文要求" in e for e in errors)

    assert f"不少于 {stages._OUTLINE_CONTEXT_STATE_MIN_CHARS} 字" in prompt
    assert "不得只声明不落地" in prompt


def _ep6_kl05_screenplay() -> EpisodeScreenplay:
    """EP6 第五轮真实回归的台词分布（run_54aaa030881f，ERR-20260825-f73e7f）：
    17 条关键台词里只有 KL05 是 29 字，模型当时给它选了 8s（上限 28 字），
    差 1 字不可满足；其余 16 条都远低于默认 5s 预算（18 字），不需要抬时长。
    """
    lines: list[str] = []
    for i in range(1, 18):
        if i == 5:
            content = "台" * 29
        else:
            content = "词" * 6
        lines.append(f"角色{i}：{content}")
    return EpisodeScreenplay(
        episode_no=6,
        title="EP6",
        full_script_text="【场1】日 / 测试场景\n占位正文用于触发大纲生成。",
        key_lines=lines,
        ending_hook="真相仍未揭开。",
    )


def _kl05_outline(duration_s: int) -> StoryboardOutline:
    """17 镜一一对应 KL01..KL17；第 5 镜（KL05）用调用方指定的 duration_s。"""
    shots = []
    for i in range(1, 18):
        shots.append(StoryboardOutlineShot(
            shot_no=i,
            beat=f"分镜第{i}段落交代角色{i}的独立剧情推进内容",
            key_line_ids=[f"KL{i:02d}"],
            duration_s=duration_s if i == 5 else 8,
        ))
    return StoryboardOutline(episode_no=6, shots=shots)


def test_director_outline_validate_raises_duration_to_fit_key_line_capacity(monkeypatch) -> None:
    """本次修复的核心：口播容量下限从"模型必须算对的算术义务"改为
    "校验前确定性计算"。EP6 KL05 真实样本——29 字台词配 8s（上限 28 字）——
    过去会在这里产生 [OUTLINE_KEY_LINE_CAPACITY_INVALID] 并耗尽 Agent Loop
    的两轮重试预算而硬失败；现在 _validate 闭包必须先把该镜 duration_s
    确定性抬到 9s（9s 上限 32 字，装得下 29 字），再跑校验器。
    """
    logged: list[dict] = []

    def _fake_log_provider_call(kind, model, status, http_status, latency_ms, meta=None, **_kw):
        logged.append({"kind": kind, "status": status, "meta": meta or {}})

    monkeypatch.setattr(stages, "log_provider_call", _fake_log_provider_call)

    screenplay = _ep6_kl05_screenplay()
    prompt, validate = _capture_director_outline_prompt(monkeypatch, screenplay)
    outline = _kl05_outline(duration_s=8)

    errors = validate(outline)

    # 确定性抬高：第 5 镜（KL05，29 字）从 8s 抬到 9s（32 字上限），只此一镜。
    assert outline.shots[4].duration_s == 9
    assert [s.duration_s for i, s in enumerate(outline.shots) if i != 4] == [8] * 16

    # 容量错误必须消失——不再是"模型自己算对"，而是校验前已经被代码修正。
    assert not any("OUTLINE_KEY_LINE_CAPACITY_INVALID" in e for e in errors), errors

    # 纪律：自动修正必须可观测，不能悄悄改写模型输出而无人知晓。
    duration_logs = [
        entry for entry in logged
        if entry["kind"] == "storyboard_outline_spoken_duration"
    ]
    assert len(duration_logs) == 1
    meta = duration_logs[0]["meta"]
    assert meta["shot_no"] == 5
    assert meta["from_duration_s"] == 8
    assert meta["to_duration_s"] == 9
    assert meta["required_chars"] == 29
    # _capture_director_outline_prompt 固定用 episode_no=1 的 episode 字典调用
    # generate_storyboard_outline；这里核对的是日志确实带上了该 episode 上下文
    # （可观测性要求），而不是断言剧本自身声明的集数。
    assert meta["episode_no"] == 1


def test_director_outline_validate_never_lowers_model_chosen_duration(monkeypatch) -> None:
    """模型出于动作铺陈节奏主动选了比口播下限更长的时长（此处 10s，远超 29 字所需的
    9s）；这是创作意图,不是物理约束,确定性修正只能抬高、不能替模型压回去。"""
    screenplay = _ep6_kl05_screenplay()
    _prompt, validate = _capture_director_outline_prompt(monkeypatch, screenplay)
    outline = _kl05_outline(duration_s=10)

    errors = validate(outline)

    assert outline.shots[4].duration_s == 10
    assert not any("OUTLINE_KEY_LINE_CAPACITY_INVALID" in e for e in errors), errors


def test_director_outline_validate_still_fails_when_content_exceeds_max_duration(monkeypatch) -> None:
    """连最长合法档位（10s，36 字）都装不下时仍必须是真错误：不得静默截断台词、
    不得超出 config.VIDEO_DURATION_MAX_S，必须要求模型把部分 key_line_ids 挪到相邻镜。"""
    screenplay = EpisodeScreenplay(
        episode_no=6,
        title="EP6",
        full_script_text="【场1】日 / 测试场景\n占位正文。",
        key_lines=["角色甲：" + ("台" * 40)],
        ending_hook="真相仍未揭开。",
    )
    _prompt, validate = _capture_director_outline_prompt(monkeypatch, screenplay)
    outline = StoryboardOutline(episode_no=6, shots=[
        StoryboardOutlineShot(
            shot_no=1, beat="角色甲说出无法在任何合法时长内装下的超长台词",
            key_line_ids=["KL01"], duration_s=5,
        ),
    ])

    errors = validate(outline)

    # 顶到技术上限后仍然超容——必须仍是硬错误，且不得超出合法时长范围。
    assert outline.shots[0].duration_s == config.VIDEO_DURATION_MAX_S
    assert any(
        "OUTLINE_KEY_LINE_CAPACITY_INVALID" in e and "40" in e
        for e in errors
    ), errors


def test_director_outline_prompt_explains_deterministic_duration_raise(monkeypatch) -> None:
    """提示词必须讲清真实规则：模型只管按叙事需要分配台词与选时长；时长不足会被
    系统确定性抬高到刚好够用的最短合法档位，但连最长时长都装不下时模型必须自己
    把 key_line_ids 挪到相邻镜——不能再让模型以为自己必须逐镜算对字数。"""
    screenplay = _outline_key_lines_screenplay()
    prompt, _validate = _capture_director_outline_prompt(monkeypatch, screenplay)

    assert "系统会在校验时确定性地把 duration_s 抬高" in prompt
    assert "只升不降" in prompt
    assert "系统不会替你拆镜" in prompt


def test_director_outline_key_content_block_matches_outline_schema_fields() -> None:
    """StoryboardOutlineShot 没有 action_desc 字段（那是详细分镜阶段 Shot 才有的字段）。
    共享的 _storyboard_key_content_block 过去对所有调用方都写死"action_desc 或有效口播"，
    大纲阶段引用了一个模型根本填不到的字段名；剧情点校验实际检查的是 beat/covers
    （validate_storyboard_outline 的 plan_text = beat+covers）。"""
    assert "action_desc" not in StoryboardOutlineShot.model_fields

    screenplay = _outline_key_lines_screenplay()
    outline_block = stages._storyboard_key_content_block(
        screenplay, plot_point_field_hint="beat/covers",
    )
    assert "action_desc" not in outline_block
    assert "beat/covers" in outline_block

    # 其余调用方（真正生成 Shot、确有 action_desc 字段）行为不变，默认值没有被误改。
    default_block = stages._storyboard_key_content_block(screenplay)
    assert "action_desc" in default_block


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
