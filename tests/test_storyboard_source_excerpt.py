from app.compiler import ensure_source_excerpt_in_prompt, compile_prompt
from app.schemas import Bible, Character, Shot, Storyboard, World
from app.validators import validate_storyboard


def _bible() -> Bible:
    return Bible(
        characters=[
            Character(
                name="谷言",
                role="主角",
                appearance_canonical="二十八岁男性，黑色短发，深灰西装，眉眼冷峻，腕戴银色手表",
                personality="冷静",
                speech_style="短句直接，语气克制",
            )
        ],
        world=World(era="现代", genre="都市", visual_style_canonical="都市漫剧厚涂风，柔和侧光，冷暖对比色"),
    )


def _shot(source_excerpt: str) -> Shot:
    return Shot(
        shot_no=1,
        duration_s=10,
        shot_size="中景",
        camera_move="推近",
        scene_setting="首日上午，咖啡厅",
        characters=["谷言"],
        action_desc=(
            "谷言抬起头看向门口，谷言攥紧手里的纸杯，谷言快步走到桌边，"
            "谷言把消息递给曲惜，谷言发现屏幕弹出新的提醒，谷言脸色沉下去准备追问真相"
        ),
        source_excerpt=source_excerpt,
        narration="这条消息让谷言意识到局面已经失控，他必须立刻确认曲惜隐藏的真实目的。",
        dialogues=[],
    )


def test_storyboard_requires_source_excerpt() -> None:
    board = Storyboard(episode_no=1, shots=[_shot("")])

    errors = validate_storyboard(board, _bible(), 10)

    assert any("source_excerpt" in error for error in errors)


def test_compile_prompt_includes_source_excerpt() -> None:
    prompt = compile_prompt(_shot("谷言攥着纸杯，听见曲惜说出那个名字，脸色骤然沉下去。"), _bible())

    assert "小说原文兜底参考：谷言攥着纸杯" in prompt


def test_legacy_seedance_prompt_is_patched_with_source_excerpt() -> None:
    prompt = ensure_source_excerpt_in_prompt(
        "固定10秒竖屏漫剧视频段，谷言低头攥紧纸杯。弱背景提示：咖啡厅 --ratio 9:16 --dur 10",
        _shot("谷言攥着纸杯，听见曲惜说出那个名字，脸色骤然沉下去。"),
    )

    assert "小说原文兜底参考：谷言攥着纸杯" in prompt
    assert prompt.endswith("--ratio 9:16 --dur 10")
    assert prompt.count("--ratio") == 1
