"""场景图素材库相关校验与注入的单元测试。"""
from app.schemas import Bible, Character, Scene, Shot, Storyboard, World
from app.validators import (match_scene_name, validate_scene_bible,
                            validate_storyboard_scenes)
from app.scenes import scene_refs_as_image_inputs


def _scenes() -> list[Scene]:
    return [
        Scene(name="宗门广场", scene_canonical="白日宗门广场，青石铺地，四周高耸石柱与飘扬旗幡，光线明亮，庄严肃穆", location_kind="室外"),
        Scene(name="破败客栈内", scene_canonical="夜晚破败客栈内堂，昏黄油灯，木桌斑驳，墙皮剥落，光线昏暗，萧索冷清", location_kind="室内"),
    ]


def _bible_with_scenes() -> Bible:
    return Bible(
        characters=[Character(name="萧炎", role="主角",
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


# ---------- match_scene_name ----------

def test_match_scene_name_substring_and_normalized() -> None:
    scenes = _scenes()
    # "时间，地点" 标签里含规范场景名 → 子串命中
    assert match_scene_name("白日，宗门广场", scenes) == "宗门广场"
    # 标点/时间差异下的归一化匹配
    assert match_scene_name("夜 / 破败客栈内", scenes) == "破败客栈内"


def test_match_scene_name_no_match_returns_none() -> None:
    assert match_scene_name("海边沙滩", _scenes()) is None
    assert match_scene_name("任意场景", []) is None


# ---------- validate_storyboard_scenes ----------

def _shot(no: int, scene_setting: str) -> Shot:
    return Shot(shot_no=no, duration_s=10, shot_size="全景", camera_move="固定",
                scene_setting=scene_setting, characters=["萧炎"],
                action_desc="萧炎站在场景中，缓缓抬头环视四周，眼神逐渐变得坚定，握紧了拳头",
                first_frame_desc="萧炎立于场景中神情平静", last_frame_desc="萧炎握拳神情坚定",
                source_excerpt="萧炎抬头环视四周。")


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
    assert board.shots[0].scene_name == "宗门广场"


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
