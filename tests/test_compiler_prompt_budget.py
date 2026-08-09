from app import config, errors
from app.compiler import CompileError, compile_prompt, SOURCE_EXCERPT_MARKER
from app.schemas import (
    Bible,
    Character,
    CharacterContinuityState,
    ContinuityState,
    Dialogue,
    PropContinuityState,
    SceneContinuityState,
    Shot,
    World,
)


def test_default_prompt_budget_matches_generation_editor_contract() -> None:
    assert config.PROMPT_CHAR_LIMIT == 8000


def _bible() -> Bible:
    return Bible(
        characters=[
            Character(
                name="萧炎",
                role="主角",
                appearance_canonical=(
                    "十五岁左右男性少年，墨色利落短发，常穿灰黑色劲装，"
                    "左手指戴古朴黑色戒指，面容清秀眼神坚韧"
                ),
                personality="坚韧",
            ),
            Character(
                name="萧薰儿",
                role="女主",
                appearance_canonical=(
                    "十五岁左右少女，青丝挽成云髻，月白襦裙配淡金纹样，"
                    "眉目清丽，气质温婉沉静"
                ),
                personality="聪慧",
            ),
            Character(
                name="萧战",
                role="配角",
                appearance_canonical=(
                    "中年男子，短须利落，玄色劲装外罩黑甲，肩宽腰直，"
                    "眼神刚毅沉稳"
                ),
                personality="刚正",
            ),
        ],
        world=World(
            era="架空玄幻古代",
            genre="高武玄幻",
            visual_style_canonical=(
                "3D国漫风，光线层次分明，色调浓郁鲜亮，人物建模精致，场景贴合玄幻东方古风"
            ),
        ),
    )


def test_long_reference_prompt_compacts_without_losing_story_anchors() -> None:
    """长锚点 + 多角色 + 台词仍须保留 START/ACTION/END 与台词原文，且不得注入章节原文。"""
    action = (
            "石碑表面骤然亮起刺眼白光，functional:examiner抬头朝人群公布成绩，"
            "周围人群骚动，functional:heckler-a摇头，functional:heckler-b窃语。萧炎按在石碑上的手收紧。"
    )
    shot = Shot(
        shot_no=2,
        duration_s=8,
        shot_size="近景",
        camera_move="推近",
        scene_setting="日，萧家测验广场",
        characters=["萧炎", "functional:heckler-a", "functional:heckler-b", "functional:examiner"],
        action_desc=action,
        first_frame_desc="萧炎按住石碑，functional:examiner低头等待结果。",
        last_frame_desc="functional:examiner公布成绩，萧炎按碑的手收紧。",
        state_in="萧炎按住石碑，functional:examiner低头等待结果。",
        primary_action=action,
        state_out="functional:examiner公布成绩，萧炎按碑的手收紧。",
        continuity_mode="same_scene_cut",
        source_excerpt=(
            "测验员看了一眼碑上所显示出来的信息，语气漠然地将之公布了出来。"
            "中年男子话刚刚脱口，便在人头汹涌的广场上带起一阵嘲讽的骚动。" * 3
        ),
        dialogues=[Dialogue(speaker="functional:examiner", line="萧炎，斗之力，三段！级别：低级！", emotion="平静")],
        continuity_from_prev=False,
    )

    prompt = compile_prompt(shot, _bible(), with_refs=True, prev_state_out=None)

    assert len(prompt) <= config.PROMPT_CHAR_LIMIT
    assert "石碑表面骤然亮起刺眼白光" in prompt
    assert "functional:examiner抬头朝人群公布成绩" in prompt
    assert "[START STATE" in prompt and "[END STATE" in prompt
    assert "萧炎，斗之力，三段！级别：低级！" in prompt
    assert SOURCE_EXCERPT_MARKER not in prompt
    assert "不要重演前序剧情" in prompt
    assert prompt.endswith("--ratio 9:16 --dur 8")


def test_silent_shot_compacts_without_forcing_dialogue_pacing(monkeypatch) -> None:
    """无台词镜头不应被口型铺满纪律绑架；超长时仍可压缩 FORMAT/CONSISTENCY。"""
    monkeypatch.setattr(config, "PROMPT_CHAR_LIMIT", 920)
    action = (
        "萧炎抬手按上冰冷石碑，因用力而发白，碑面光纹向外扩散，萧薰儿侧身注视，"
        "萧战立于边缘压抑呼吸，三人神情绷紧，动作连贯不跳切。"
    ) * 3
    shot = Shot(
        shot_no=3,
        duration_s=10,
        shot_size="全景",
        camera_move="推近",
        scene_setting="日，萧家测验广场",
        characters=["萧炎", "萧薰儿", "萧战"],
        action_desc=action,
        first_frame_desc="萧炎按碑，萧薰儿侧视，萧战立于边缘。",
        last_frame_desc="光纹铺满碑面，三人神情更紧。",
        state_in="萧炎按碑，萧薰儿侧视，萧战立于边缘。",
        primary_action=action,
        state_out="光纹铺满碑面，三人神情更紧。",
        continuity_mode="same_scene_cut",
        source_excerpt="原文。" * 40,
        dialogues=[],
        narration="",
        continuity_from_prev=False,
    )

    prompt = compile_prompt(
        shot,
        _bible(),
        with_refs=True,
        extra_negative="避免出现：" + "伪影，" * 30 + "手指畸形",
    )

    assert len(prompt) <= 920
    assert "[ONE CURRENT ACTION]" in prompt
    assert "萧炎抬手按上冰冷石碑" in prompt
    assert "碑面光纹向外扩散" in prompt
    assert "指节" not in prompt
    assert "口型和肢体随台词自然推进" not in prompt
    assert prompt.endswith("--ratio 9:16 --dur 10")


def test_large_continuity_snapshot_projects_current_pose_and_changed_props() -> None:
    stable_props = {
        f"历史道具-{index}": PropContinuityState(
            canonical_name=f"未参与本镜的历史道具{index}",
            revision_id=f"PROP-{index}",
            owner="萧战",
            location="远处库房",
            form="收纳",
        )
        for index in range(40)
    }
    character_in = CharacterContinuityState(
        look_revision_id="LOOK-XIAOYAN-1",
        outfit_revision_id="OUTFIT-XIAOYAN-1",
        screen_side="center",
        pose="右掌按住石碑",
        facing="面向石碑",
        gaze_target="石碑刻度",
        right_hand="掌心贴住石碑",
    )
    character_out = character_in.model_copy(update={
        "pose": "右掌仍按石碑，肩背绷紧",
        "gaze_target": "亮起的石碑刻度",
    })
    current_prop_in = PropContinuityState(
        canonical_name="测验石碑",
        revision_id="PROP-STELE-1",
        owner="萧家",
        location="广场中央",
        form="表面暗淡",
        required=True,
    )
    current_prop_out = current_prop_in.model_copy(update={"form": "表面光纹亮起"})
    scene = SceneContinuityState(
        scene_revision_id="SCENE-SQUARE-1",
        time_of_day="白天",
        lighting_state="自然日光",
        axis_id="AXIS-STELE",
        landmarks={"测验石碑": "center"},
    )
    shot = Shot(
        shot_no=56,
        duration_s=5,
        shot_size="中景",
        camera_move="推近",
        scene_setting="白天，萧家测验广场",
        characters=["萧炎"],
        characters_visible=["萧炎"],
        action_desc="萧炎右掌按住测验石碑，碑面光纹由暗转亮。",
        first_frame_desc="萧炎右掌贴住暗淡的测验石碑。",
        last_frame_desc="萧炎肩背绷紧，测验石碑表面光纹亮起。",
        state_in="萧炎右掌贴住暗淡的测验石碑。",
        primary_action="萧炎按住测验石碑使碑面光纹亮起",
        state_out="萧炎肩背绷紧，测验石碑表面光纹亮起。",
        continuity_mode="same_scene_cut",
        continuity_state_in=ContinuityState(
            scene=scene,
            characters={"萧炎": character_in},
            props={**stable_props, "测验石碑": current_prop_in},
        ),
        continuity_state_out=ContinuityState(
            scene=scene,
            characters={"萧炎": character_out},
            props={**stable_props, "测验石碑": current_prop_out},
        ),
    )

    prompt = compile_prompt(shot, _bible(), with_refs=True)

    assert len(prompt) <= config.PROMPT_CHAR_LIMIT
    assert "右掌按住石碑" in prompt
    assert "肩背绷紧" in prompt
    assert "测验石碑" in prompt
    assert "表面光纹亮起" in prompt
    assert "未参与本镜的历史道具39" not in prompt
    assert "未列出字段继承起始状态" in prompt


def test_compile_error_is_correctable_generation_error() -> None:
    assert issubclass(CompileError, ValueError)
    assert errors.classify(CompileError("prompt too long")) == ("generation", "GEN")
