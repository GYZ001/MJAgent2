"""逐镜头分镜（sequential storyboard）路径的单测：

覆盖 codex 新增的「按顺序逐镜生成 + 单镜 QA」改造与本次修复：
- 单镜（非收尾）QA 只拦当前镜与承接问题，整集级检查（镜头数/总时长/关键内容）放行；
- 自愿收尾时若整集必保留内容还没补齐，不硬塞单镜而是要求继续补镜；
- 撞到大纲末镜/软预算/硬上限（must_finish）才对主线缺口硬失败，禁止氛围声轨逼出计划外镜；
- 分镜进度按已通过镜头的模型选择时长求和。
"""

import asyncio

from app import config, stages
from app.schemas import (Bible, Character, Dialogue, EpisodeScreenplay,
                         InformationItem, Shot, StoryboardOutline,
                         StoryboardOutlineShot, World)
from app.stages import (StoryboardShotDraft, _storyboard_progress_block,
                        _relevant_text_windows, _render_completed_shots_context,
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
    assert "默认 5s" in low and "软预算" in low
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


def test_next_shot_uses_bounded_single_shot_output_budget(monkeypatch) -> None:
    captured: dict[str, int] = {}

    async def fake_agent_loop(*_args, **kwargs):
        captured["max_tokens"] = kwargs["max_tokens"]
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


# ---------- 单镜 QA 的整集级放行 / 收尾分支 ----------

def test_partial_nonfinal_skips_episode_level_checks() -> None:
    # 非收尾镜：即便关键台词还没出现，也不应报"镜头数/关键内容/继续补镜"。
    errors = _validate(_draft(_shot(1), is_final=False),
                       allow_finish=False, must_finish=False, screenplay=_screenplay())
    assert not any(e.startswith("镜头数 ") for e in errors)
    assert not any(e.startswith("分镜丢失了剧本标记的") for e in errors)
    assert not any("继续补镜" in e for e in errors)


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
