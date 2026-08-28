"""场景图素材库相关校验与注入的单元测试。"""
from app.schemas import Bible, Character, Scene, Shot, Storyboard, World
from app.scene_contract import split_legacy_scene_setting
from app.validators import (canonicalize_storyboard_scene, match_scene_name, validate_scene_bible,
                            validate_storyboard_scenes)
from app.scenes import scene_refs_as_image_inputs


def _scenes() -> list[Scene]:
    return [
        Scene(name="宗门广场", scene_canonical="白日宗门广场，青石铺地，四周高耸石柱与飘扬旗幡，光线明亮，庄严肃穆", location_kind="室外"),
        Scene(name="破败客栈内", scene_canonical="夜晚破败客栈内堂，昏黄油灯，木桌斑驳，墙皮剥落，光线昏暗，萧索冷清", location_kind="室内"),
    ]


def _bible_with_scenes() -> Bible:
    return Bible(
        characters=[Character(name="甲一", role="主角",
                              appearance_canonical="十五岁少年，黑发束起，黑色劲装，眉眼倔强坚毅")],
        world=World(era="玄幻", genre="玄幻", visual_style_canonical="国风玄幻漫剧厚涂风，暖冷对比光"),
        scenes=_scenes(),
    )


# ---------- validate_scene_bible ----------

def test_validate_scene_bible_ok() -> None:
    assert validate_scene_bible(_scenes()) == []


def test_validate_scene_bible_rejects_short_canonical_and_dups() -> None:
    bad = [
        Scene(name="A", scene_canonical="太短"),
        Scene(name="A", scene_canonical="x" * 40),
    ]
    errors = validate_scene_bible(bad)
    assert any("scene_canonical" in e for e in errors)
    assert any("重复" in e for e in errors)


def test_validate_scene_bible_rejects_empty_name() -> None:
    errors = validate_scene_bible([Scene(name="", scene_canonical="x" * 40)])
    assert any("不能为空" in e for e in errors)


def test_scene_prompt_states_the_same_length_the_gate_enforces(monkeypatch) -> None:
    """写进提示词的字数区间，必须就是闸放行的那个区间。

    真实故障 ERR-20260828-4f4f19（《罗刹海市》场景库）：提示词写「30~60 字」，闸卡
    在 30~80，模型照着 60 盲打，12 个场景里 3 个写到 81 字。preview 端点把这份清单
    原样返回，用户点确认时才被自己的提交端点以 422 拒收，而界面从头到尾没提示过是
    哪几条超标——预览承诺的东西提交不进去。

    模型数不准字数是常态，所以判据不能挂在「模型会不会超」上；能挂住的是「它至少
    被告知了真实红线」。这条测试断言提示词里出现的就是闸的两个端点值，任何一边改
    了数字而另一边没跟上，这里立刻红。
    """
    import asyncio
    import json

    from app.harness import model_gateway
    from app.refs import SCENE_CANONICAL_MAX_CHARS, SCENE_CANONICAL_MIN_CHARS
    from app.stages import generate_scene_bible

    seen: dict[str, str] = {}

    async def capture(messages, **_kwargs):
        seen["prompt"] = messages[-1]["content"]
        # 恰好卡在上限：闸放行、模型也照着提示词的数字写，两边一致才不报错。
        return json.dumps({"scenes": [{
            "name": "宗门广场",
            "scene_canonical": "青" * SCENE_CANONICAL_MAX_CHARS,
            "location_kind": "室外",
        }]}, ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", capture)
    scenes = asyncio.run(generate_scene_bible(
        [{"idx": 1, "title": "第一章", "content": "宗门广场上人潮涌动。"}],
        _bible_with_scenes(),
    ))

    assert validate_scene_bible(scenes) == [], (
        "写到上限整数的锚点必须被放行——闸要是比提示词严，模型永远踩不准"
    )
    prompt = seen["prompt"]
    assert f"{SCENE_CANONICAL_MIN_CHARS}~{SCENE_CANONICAL_MAX_CHARS} 字" in prompt
    assert "30~60 字" not in prompt, "提示词不得再写一个闸不认的字数区间"


# ---------- match_scene_name ----------

def test_match_scene_name_substring_and_normalized() -> None:
    scenes = _scenes()
    # "时间，地点" 标签里含规范场景名 → 子串命中
    assert match_scene_name("白日，宗门广场", scenes) == "宗门广场"
    # 标点/时间差异下的归一化匹配
    assert match_scene_name("夜 / 破败客栈内", scenes) == "破败客栈内"


def test_scene_heading_slash_structurally_separates_open_ended_time_label() -> None:
    assert split_legacy_scene_setting(
        "【场5】状态变化后即刻 / 胡家客厅椅子旁"
    ) == ("状态变化后即刻", "胡家客厅椅子旁")


def test_legacy_comma_heading_uses_field_position_not_time_vocabulary() -> None:
    assert split_legacy_scene_setting(
        "【场5】状态变化后即刻，胡家客厅椅子旁"
    ) == ("状态变化后即刻", "胡家客厅椅子旁")


def test_scene_match_uses_location_alias_without_classifying_time_words() -> None:
    scenes = [
        Scene(
            name="胡家六楼客厅",
            scene_canonical="六楼客厅固定空间",
            aliases=["高潮后即刻 / 胡家客厅椅子旁"],
        )
    ]

    assert match_scene_name(
        "【场5】状态变化后即刻 / 胡家客厅椅子旁",
        scenes,
        allow_fuzzy=False,
    ) == "胡家六楼客厅"


def test_match_scene_name_no_match_returns_none() -> None:
    assert match_scene_name("海边沙滩", _scenes()) is None
    assert match_scene_name("任意场景", []) is None


def test_match_scene_name_prefers_specific_scene_over_earlier_prefix_alias() -> None:
    scenes = [
        Scene(
            name="大青山山顶",
            scene_canonical="大青山山顶的固定地貌、落日光线与山河远景锚点，环境稳定清晰，无人物无文字",
            aliases=["黄昏 / 大青山顶"],
        ),
        Scene(
            name="大青山顶山崖",
            scene_canonical="大青山顶山崖与半山裂缝、藤条和崖壁的固定空间锚点，环境稳定清晰，无人物无文字",
            aliases=["黄昏 / 大青山顶边缘至山崖", "黄昏 / 大青山崖边"],
        ),
    ]

    assert match_scene_name(
        "黄昏，大青山顶山崖", scenes, allow_fuzzy=False,
    ) == "大青山顶山崖"
    assert match_scene_name(
        "黄昏 / 大青山顶边缘至山崖", scenes, allow_fuzzy=False,
    ) == "大青山顶山崖"


def test_match_scene_name_rejects_equal_rank_ambiguity() -> None:
    scenes = [
        Scene(name="东院", scene_canonical="东院固定空间环境锚点描述足够完整，无人物无文字", aliases=["旧院"]),
        Scene(name="西院", scene_canonical="西院固定空间环境锚点描述足够完整，无人物无文字", aliases=["旧院"]),
    ]

    assert match_scene_name("夜，旧院", scenes, allow_fuzzy=False) is None


def test_match_scene_name_uses_text_order_for_compound_location() -> None:
    scenes = [
        Scene(name="黑山外围", scene_canonical="黑山外围固定空间环境锚点描述足够完整，无人物无文字"),
        Scene(name="荒山林海", scene_canonical="荒山林海固定空间环境锚点描述足够完整，无人物无文字"),
    ]

    assert match_scene_name(
        "日 / 荒山林海至黑山外围", scenes, allow_fuzzy=False,
    ) == "荒山林海"


# ---------- validate_storyboard_scenes ----------

def _shot(no: int, scene_setting: str) -> Shot:
    return Shot(shot_no=no, duration_s=5, shot_size="全景", camera_move="固定",
                scene_setting=scene_setting, characters=["甲一"],
                action_desc="甲一站在场景中，缓缓抬头环视四周，眼神逐渐变得坚定，握紧了拳头",
                first_frame_desc="甲一立于场景中神情平静", last_frame_desc="甲一握拳神情坚定",
                source_excerpt="甲一抬头环视四周。")


def test_validate_storyboard_scenes_empty_library_is_noop() -> None:
    bible = _bible_with_scenes()
    bible.scenes = []
    board = Storyboard(episode_no=1, shots=[_shot(1, "某个库外场景")])
    assert validate_storyboard_scenes(board, bible) == []


def test_validate_storyboard_scenes_backfills_scene_name_on_match() -> None:
    bible = _bible_with_scenes()
    shot = _shot(1, "白日，宗门广场")
    board = Storyboard(episode_no=1, shots=[shot])
    errors = validate_storyboard_scenes(board, bible)
    assert errors == []
    assert board.shots[0].scene_time == "白日"
    assert board.shots[0].scene_name == "宗门广场"
    assert board.shots[0].scene_setting == "白日，宗门广场"


def test_separate_scene_time_does_not_affect_scene_image_binding() -> None:
    bible = _bible_with_scenes()
    shot = _shot(1, "")
    shot.scene_time = "18:30"
    shot.scene_name = "宗门广场"
    board = Storyboard(episode_no=1, shots=[shot])

    assert validate_storyboard_scenes(board, bible) == []
    assert shot.scene_time == "18:30"
    assert shot.scene_name == "宗门广场"
    assert shot.scene_setting == "18:30，宗门广场"


def test_fuzzy_scene_input_is_persisted_as_canonical_scene_name() -> None:
    bible = _bible_with_scenes()
    shot = _shot(1, "")
    shot.scene_time = "黄昏"
    shot.scene_name = "宗门广场中央"
    board = Storyboard(episode_no=1, shots=[shot])

    assert validate_storyboard_scenes(board, bible) == []
    assert shot.scene_name == "宗门广场"
    assert shot.scene_setting == "黄昏，宗门广场"


def test_explicit_scene_edit_is_not_overridden_by_stale_legacy_setting() -> None:
    bible = _bible_with_scenes()
    shot = _shot(1, "白日，宗门广场")
    shot.scene_time = ""
    shot.scene_name = "破败客栈内"

    assert canonicalize_storyboard_scene(shot, bible, prefer_explicit=True) == "破败客栈内"
    assert shot.scene_time == ""
    assert shot.scene_setting == "破败客栈内"


def test_validate_storyboard_scenes_flags_out_of_library_scene() -> None:
    bible = _bible_with_scenes()
    board = Storyboard(episode_no=1, shots=[_shot(1, "夜，海边沙滩")])
    errors = validate_storyboard_scenes(board, bible)
    assert len(errors) == 1
    assert "不在场景图素材库内" in errors[0]
    assert board.shots[0].scene_name == ""


# ---------- scene_refs_as_image_inputs ----------

def test_scene_refs_as_image_inputs_fallback_to_bible_path(tmp_path) -> None:
    img = tmp_path / "scene.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg-bytes")
    bible = _bible_with_scenes()
    bible.scenes[0].ref_image_path = str(img)
    inputs = scene_refs_as_image_inputs(bible, ["宗门广场"], 1)
    assert len(inputs) == 1
    url, role = inputs[0]
    assert role == "reference_image"
    assert url.startswith("data:")


def test_scene_refs_as_image_inputs_skips_missing_file() -> None:
    bible = _bible_with_scenes()
    bible.scenes[0].ref_image_path = "/nonexistent/scene.jpg"
    assert scene_refs_as_image_inputs(bible, ["宗门广场"], 1) == []
