import json

import pytest

from app import api, db
from app.character_policy import is_functional_extra
from app.compiler import CompileError, compile_prompt, compile_scene_prompt
from app.continuity import shot_contract_dict
from app.evidence import repository
from app.harness.contracts import get_contract
from app.harness.types import EvidenceArtifact
from app.schemas import (
    AudioTimelineItem,
    Bible,
    Character,
    Dialogue,
    EpisodeScreenplay,
    Shot,
    Storyboard,
    VoiceCanonical,
    World,
)
from app.validators import normalize_offbible_characters, validate_storyboard


def _bible() -> Bible:
    return Bible(
        characters=[
            Character(
                name="甲一",
                role="主角",
                appearance_canonical="十五岁少年，黑发束起，黑色劲装，眉眼倔强坚毅",
                personality="坚韧",
            )
        ],
        world=World(
            era="玄幻古代",
            genre="玄幻",
            visual_style_canonical="国风玄幻漫剧厚涂风，暖冷对比光",
        ),
    )


def _real_shot_2() -> Shot:
    return Shot(
        shot_no=1,
        duration_s=10,
        shot_size="中景",
        camera_move="固定",
        scene_setting="日，甲家测验广场",
        characters=["甲一"],
        action_desc=(
            "甲一右手仍紧贴黑色魔石碑表面，碑面骤然亮起白光；碑旁functional:examiner确认碑面信息，"
            "面无表情地公布成绩，甲一听完后握紧拳头压住情绪。"
        ),
        first_frame_desc="甲一右手贴住黑色魔石碑，functional:examiner站在碑旁低头等待结果。",
        last_frame_desc="同一机位，functional:examiner已经公布成绩，甲一仍站在碑前握紧拳头。",
        source_excerpt="测验员看了一眼碑上所显示出来的信息，语气漠然地将之公布了出来。",
        narration="",
        dialogues=[
            Dialogue(
                speaker="functional:examiner",
                line="甲一，测验力，三段！级别：低级！",
                emotion="平静",
            )
        ],
        transition="硬切",
        continuity_from_prev=False,
    )


def test_functional_extra_classifier_is_bounded_and_deterministic() -> None:
    assert is_functional_extra("functional:examiner")
    assert all(not is_functional_extra(name) for name in (
        "路人甲", "路人乙", "路人12", "测验员", "中年测验员", "测验员甲", "族人甲", "弟子乙",
        "守卫3", "老管家", "二长老", "甲一", "韩枫",
        "黑袍老者", "神秘黑袍老者", "绿袍男子", "青衣女子",
    ))


def test_real_shot_2_speaker_is_added_as_visible_functional_extra() -> None:
    shot = _real_shot_2()

    changes = normalize_offbible_characters(
        Storyboard(episode_no=1, shots=[shot]), _bible()
    )
    errors = validate_storyboard(
        Storyboard(episode_no=1, shots=[shot]), _bible(), target_duration_s=50
    )

    assert shot.characters == ["甲一", "functional:examiner"]
    assert shot.characters_visible == ["甲一", "functional:examiner"]
    assert shot.audio_cast == ["functional:examiner"]
    assert shot.audio_timeline[0].type == "spoken_dialogue"
    assert shot.audio_timeline[0].lip_sync is True
    assert any(
        change.get("allowed_functional_extra") == "functional:examiner"
        and change.get("source") == "dialogue_speaker"
        for change in changes
    )
    assert not any("speaker=「functional:examiner」不在该镜头 characters" in error for error in errors)
    assert not any("既不在角色圣经" in error for error in errors)


def test_source_described_person_cannot_bypass_screenplay_identity_resolution() -> None:
    """历史分镜若残留“绿袍男子”，不得把它当功能路人直接放行。"""
    shot = _real_shot_2()
    shot.characters = ["甲一"]
    shot.characters_visible = ["甲一"]
    shot.action_desc = "绿袍男子站在甲一面前厉声警告，甲一紧张地看向他。"
    shot.dialogues = [
        Dialogue(speaker="绿袍男子", line="再说一句废话，直接割了你的舌头。", delivery="spoken_dialogue")
    ]
    shot.audio_cast = ["绿袍男子"]
    shot.audio_timeline = [
        AudioTimelineItem(
            start_s=0.5,
            end_s=3.5,
            type="offscreen_voice",
            speaker_id="绿袍男子",
            text="再说一句废话，直接割了你的舌头。",
            lip_sync=False,
        )
    ]

    changes = normalize_offbible_characters(
        Storyboard(episode_no=1, shots=[shot]), _bible()
    )

    assert shot.characters == ["甲一"]
    assert shot.characters_visible == ["甲一"]
    assert shot.audio_cast == []
    assert shot.dialogues == []
    assert shot.audio_timeline == []
    assert any(change.get("stripped") == "绿袍男子" for change in changes)


def test_functional_extra_compiles_without_persistent_bible_asset() -> None:
    shot = _real_shot_2()
    normalize_offbible_characters(Storyboard(episode_no=1, shots=[shot]), _bible())

    video_prompt = compile_prompt(shot, _bible())
    frame_prompt = compile_scene_prompt(shot, _bible(), kind="tail")

    assert "功能性路人「functional:examiner」" in video_prompt
    assert "功能性路人「functional:examiner」" in frame_prompt
    assert "functional:examiner" in video_prompt and "甲一，测验力，三段" in video_prompt
    assert "[AUDIO TIMELINE]" in video_prompt


def test_legacy_functional_name_requires_persisted_typed_voice_contract() -> None:
    shot = _real_shot_2()
    shot.characters = ["甲一", "测验员"]
    shot.characters_visible = ["甲一", "测验员"]
    shot.audio_cast = ["测验员"]
    shot.dialogues[0].speaker = "测验员"
    screenplay = EpisodeScreenplay(
        episode_no=1,
        full_script_text="【场1】日 / 广场\n测验员：成绩已确认。",
        voice_bible=[
            VoiceCanonical(
                speaker_id="测验员",
                role_type="functional_character",
                voice_canonical="语气平直，播报清楚",
            )
        ],
    )

    video_prompt = compile_prompt(
        shot,
        _bible(),
        screenplay=screenplay,
    )
    frame_prompt = compile_scene_prompt(
        shot,
        _bible(),
        kind="tail",
        screenplay=screenplay,
    )

    assert "功能性路人「测验员」" in video_prompt
    assert "功能性路人「测验员」" in frame_prompt


def test_functional_extra_must_be_visibly_staged() -> None:
    shot = _real_shot_2()
    shot.characters.append("functional:observer")

    errors = validate_storyboard(
        Storyboard(episode_no=1, shots=[shot]), _bible(), target_duration_s=50
    )

    assert any("功能性路人「functional:observer」未在 action_desc/首尾帧中明确入画" in error for error in errors)


def test_named_unknown_character_still_fails_the_bible_gate() -> None:
    shot = _real_shot_2()
    shot.characters.append("韩枫")
    shot.action_desc += "韩枫站在人群后方冷眼旁观。"

    errors = validate_storyboard(
        Storyboard(episode_no=1, shots=[shot]), _bible(), target_duration_s=50
    )

    assert any("韩枫" in error and "既不在角色圣经" in error for error in errors)


def test_offbible_normalization_removes_ghost_character_from_full_contract() -> None:
    """历史修复可能只改了 characters，却把同一人留在可见/声轨合同中。

    这种“幽灵角色”会让确认门看似已规范化，Prompt 编译却仍收到旧名并抛出
    ``not a functional extra``。规范化必须以整个镜头合同为原子单位。
    """
    shot = _real_shot_2()
    shot.characters = ["甲一", "韩枫"]
    shot.characters_visible = ["韩枫", "甲一"]
    shot.dialogues = [
        Dialogue(speaker="韩枫", line="再说一句废话，直接割了你的舌头。")
    ]
    shot.audio_cast = ["韩枫"]
    shot.audio_timeline = [
        AudioTimelineItem(
            start_s=0.5,
            end_s=3.5,
            type="spoken_dialogue",
            speaker_id="韩枫",
            text="再说一句废话，直接割了你的舌头。",
            lip_sync=True,
        )
    ]
    shot.reference_roles = ["字符串参考", "character_identity:韩枫"]

    changes = normalize_offbible_characters(
        Storyboard(episode_no=1, shots=[shot]), _bible()
    )

    assert shot.characters == ["甲一"]
    assert shot.characters_visible == ["甲一"]
    assert shot.dialogues == []
    assert shot.audio_cast == []
    assert shot.audio_timeline == []
    assert shot.reference_roles == ["字符串参考"]
    assert any(change.get("stripped") == "韩枫" for change in changes)
    assert "再说一句废话" in shot.action_desc
    prompt = compile_prompt(shot, _bible())
    assert "韩枫" not in prompt


def test_prompt_compiler_reports_stale_visible_character_as_contract_error() -> None:
    shot = _real_shot_2()
    shot.characters_visible = ["甲一", "韩枫"]

    errors = validate_storyboard(
        Storyboard(episode_no=1, shots=[shot]), _bible(), target_duration_s=50
    )
    assert any("characters_visible" in error and "韩枫" in error for error in errors)
    with pytest.raises(CompileError, match="韩枫"):
        compile_prompt(shot, _bible())


def test_prompt_compiler_never_leaks_raw_functional_extra_error_without_bible() -> None:
    shot = _real_shot_2()
    empty_bible = Bible(characters=[], world=_bible().world)

    with pytest.raises(CompileError, match="甲一"):
        compile_prompt(shot, empty_bible)


def test_validate_storyboard_does_not_strip_characters_when_bible_is_empty() -> None:
    """架构转向（2026-08-31）回归锁：首版人物谱不再点名角色，
    ``Bible(characters=[])`` 是新主路径的正常产物，不是异常态。
    ``is_allowed_storyboard_character`` 早有 ``allow_without_bible=True`` 分支
    （没有真实角色圣经时不做强制删除，保持占位圣经的历史兼容行为）——这条
    用例把它钉在 ``validate_storyboard`` 这个真实调用点上：空人物谱下，镜头
    引用一个尚未在圣经里登记的角色名（甲一，等待映射台/分集反应式建卡）不
    应该被判定为「既不在角色圣经中，也不是允许的功能性路人标签」而报错，
    分镜展开前的反应式发现才是真正把关的地方，不是这条校验。"""
    shot = _real_shot_2()
    empty_bible = Bible(characters=[], world=_bible().world)

    errors = validate_storyboard(
        Storyboard(episode_no=1, shots=[shot]), empty_bible, target_duration_s=50,
    )

    assert not any("既不在角色圣经中" in error for error in errors)
    assert not any("甲一" in error and "characters" in error for error in errors)


def test_historical_shot_repair_creates_lineaged_t1_candidate(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "functional-extra.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES('p1','P','planned',1)"
    )
    conn.execute(
        """INSERT INTO episodes(
            id, project_id, episode_no, title, hook, cliffhanger, synopsis,
            source_chapters, target_duration_s, status, created_at
        ) VALUES('e1','p1',1,'E','','','', '[1]', 50, 'scripted', 1)"""
    )
    shot = _real_shot_2()
    original = repository.create_artifact(EvidenceArtifact(
        type="storyboard_shot",
        scope_type="storyboard_checkpoint",
        scope_id="e1:1",
        status="approved",
        trust_level="T2",
        content=shot.model_dump(mode="json"),
        contract_version="2.0.1",
    ))
    descendant = repository.create_artifact(EvidenceArtifact(
        type="compiled_prompt",
        scope_type="shot",
        scope_id="s1",
        status="approved",
        trust_level="T2",
        content={"prompt": "old character roster"},
        parent_artifact_ids=[original["id"]],
    ))
    conn.execute(
        """INSERT INTO shots(
            id, episode_id, shot_no, duration_s, shot_size, camera_move, scene_setting,
            characters, action_desc, first_frame_desc, last_frame_desc, source_excerpt,
            narration, dialogues, transition, continuity_from_prev, storyboard_artifact_id
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "s1", "e1", shot.shot_no, shot.duration_s, shot.shot_size, shot.camera_move,
            shot.scene_setting, json.dumps(shot.characters, ensure_ascii=False), shot.action_desc,
            shot.first_frame_desc, shot.last_frame_desc, shot.source_excerpt, shot.narration,
            json.dumps([item.model_dump() for item in shot.dialogues], ensure_ascii=False),
            shot.transition, int(shot.continuity_from_prev), original["id"],
        ),
    )
    conn.commit()
    board = Storyboard(episode_no=1, shots=[shot])
    changes = normalize_offbible_characters(board, _bible())

    artifact_ids = api._persist_storyboard_character_policy_repairs(
        conn, "e1", board, changes
    )

    assert len(artifact_ids) == 1
    repaired = repository.get_artifact(artifact_ids[0])
    evaluation = repository.get_evaluations(artifact_ids[0])[0]
    row = conn.execute(
        "SELECT characters, storyboard_artifact_id FROM shots WHERE id='s1'"
    ).fetchone()
    assert json.loads(row["characters"]) == ["甲一", "functional:examiner"]
    assert row["storyboard_artifact_id"] == artifact_ids[0]
    assert repaired["status"] == "candidate"
    assert repaired["trust_level"] == "T1"
    assert repaired["parent_artifact_ids"] == [original["id"]]
    assert repaired["contract_version"] == get_contract("storyboard").version
    assert evaluation["evidence"]["scope"] == "character_policy_only"
    assert repository.get_artifact(original["id"])["status"] == "approved"
    assert repository.get_artifact(descendant["id"])["status"] == "stale"


def test_historical_ghost_repair_persists_the_complete_character_contract(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ghost-character.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES('p1','P','planned',1)"
    )
    conn.execute(
        """INSERT INTO episodes(
            id, project_id, episode_no, title, hook, cliffhanger, synopsis,
            source_chapters, target_duration_s, status, created_at
        ) VALUES('e1','p1',1,'E','','','', '[1]', 50, 'scripted', 1)"""
    )
    shot = _real_shot_2()
    # 复制真实事故状态：legacy characters/dialogues 已被部分修复，
    # 但扩展合同仍保留非法角色和发声轨。
    shot.characters = ["甲一"]
    shot.characters_visible = ["韩枫", "甲一"]
    shot.dialogues = []
    shot.audio_cast = ["韩枫"]
    shot.audio_timeline = [
        AudioTimelineItem(
            start_s=0.5,
            end_s=3.5,
            type="spoken_dialogue",
            speaker_id="韩枫",
            text="再说一句废话，直接割了你的舌头。",
            lip_sync=True,
        )
    ]
    shot.reference_roles = ["character_identity:韩枫"]
    original = repository.create_artifact(EvidenceArtifact(
        type="storyboard_shot",
        scope_type="storyboard_checkpoint",
        scope_id="e1:1",
        status="approved",
        trust_level="T2",
        content=shot.model_dump(mode="json"),
        contract_version="2.0.1",
    ))
    conn.execute(
        """INSERT INTO shots(
            id, episode_id, shot_no, duration_s, shot_size, camera_move, scene_setting,
            characters, action_desc, first_frame_desc, last_frame_desc, source_excerpt,
            narration, dialogues, transition, continuity_from_prev, shot_contract_json,
            storyboard_artifact_id
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "s1", "e1", shot.shot_no, shot.duration_s, shot.shot_size, shot.camera_move,
            shot.scene_setting, json.dumps(shot.characters, ensure_ascii=False), shot.action_desc,
            shot.first_frame_desc, shot.last_frame_desc, shot.source_excerpt, shot.narration,
            "[]", shot.transition, int(shot.continuity_from_prev),
            json.dumps(shot_contract_dict(shot), ensure_ascii=False), original["id"],
        ),
    )
    conn.commit()

    board = Storyboard(episode_no=1, shots=[shot])
    changes = normalize_offbible_characters(board, _bible())
    artifact_ids = api._persist_storyboard_character_policy_repairs(
        conn, "e1", board, changes
    )

    assert len(artifact_ids) == 1
    row = conn.execute("SELECT * FROM shots WHERE id='s1'").fetchone()
    contract = json.loads(row["shot_contract_json"])
    assert json.loads(row["characters"]) == ["甲一"]
    assert json.loads(row["dialogues"]) == []
    assert contract["characters_visible"] == ["甲一"]
    assert contract["audio_cast"] == []
    assert contract["audio_timeline"] == []
    assert contract["reference_roles"] == []
    restored = api._board_from_shot_rows([row], episode_no=1)
    prompt = compile_prompt(restored.shots[0], _bible())
    assert "韩枫" not in prompt
