from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from app import db, scenes
from app.schemas import (
    Bible,
    EpisodeScreenplay,
    Scene,
    ScriptScene,
    Shot,
    Storyboard,
    StoryboardOutline,
    StoryboardOutlineShot,
    World,
)
from app.stages import _scene_library_block
from app.domain.storyboard_ops import (
    _reconcile_storyboard_scene_projection,
    _sync_storyboard_scene_bindings,
)
from app.validators import (
    match_scene_name,
    validate_storyboard_screenplay_scene_alignment,
    validate_storyboard_scenes,
    validate_storyboard_outline_scene_alignment,
    validate_storyboard_shot_scene_alignment,
)


def _bible(*names: str) -> Bible:
    return Bible(
        characters=[],
        world=World(visual_style_canonical="国风玄幻厚涂"),
        scenes=[
            Scene(
                name=name,
                scene_canonical=f"{name}的固定空间结构与光线陈设锚点，画面环境稳定清晰，无人物无文字",
            )
            for name in names
        ],
    )


def _screenplay() -> EpisodeScreenplay:
    return EpisodeScreenplay(
        episode_no=209,
        title="美杜莎女王",
        full_script_text="【场1】日 / 蛇人族大殿内\n月媚：你可终于赶来了。",
        scene_outline=[
            ScriptScene(
                scene_no=1,
                scene_heading="【场1】日 / 蛇人族大殿内",
                story_function="月媚向墨巴斯通报敌情并推动首领集结",
                characters=[],
                summary="月媚在蛇人族大殿内等待墨巴斯，随后通报昨夜与人类强者交锋的结果。",
                turn="两人感应到城外强者气息",
                source_basis="月媚与墨巴斯在大殿内谈论来敌",
            ),
            ScriptScene(
                scene_no=2,
                scene_heading="【场2】日 / 蛇人族城墙上空",
                story_function="蛇人族首领与古河一行在城墙上空正面对峙",
                characters=[],
                summary="墨巴斯与月媚飞到蛇人族城墙上空，质问古河一行擅闯的目的。",
                turn="古河传声呼唤美杜莎女王",
                source_basis="双方在蛇人族城墙上空对峙",
            ),
        ],
    )


def _shot(scene_setting: str, shot_no: int = 1) -> Shot:
    return Shot(
        shot_no=shot_no,
        duration_s=5,
        shot_size="中景",
        camera_move="固定",
        scene_setting=scene_setting,
        action_desc="月媚坐在大殿椅背前抬眼看向刚刚赶到的墨巴斯。",
        first_frame_desc="月媚独自坐在大殿椅背前，墨巴斯尚未进入画面。",
        last_frame_desc="墨巴斯走到桌前停下，月媚抬眼看向他的方向。",
        source_excerpt="月媚抬眼看向刚刚赶到的墨巴斯",
    )


def _fresh_project(tmp_path, monkeypatch, bible: Bible) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "scene-preflight.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id,name,bible_json,bible_version,created_at) VALUES('p1','测试',?,1,1)",
        (bible.model_dump_json(),),
    )
    conn.commit()


def _install_fake_scene_generator(tmp_path, monkeypatch, generated: list[str]) -> None:
    async def fake_generate(
        project_id: str,
        name: str,
        scene_canonical: str,
        _style: str,
        *,
        ep_start: int,
        bible_version: int,
    ) -> str:
        generated.append(name)
        path = tmp_path / f"{name}.jpg"
        path.write_bytes(b"scene")
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO scene_references(id,project_id,scene_name,ep_start,ep_end,scene_canonical,"
            "prompt,image_path,qa_json,bible_version,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"scene_{len(generated)}",
                project_id,
                name,
                ep_start,
                None,
                scene_canonical,
                "prompt",
                str(path),
                "{}",
                bible_version,
                db.now(),
            ),
        )
        conn.commit()
        return str(path)

    monkeypatch.setattr(scenes, "_generate_and_register_scene", fake_generate)
    monkeypatch.setattr("app.multiview.scene_multiview_enabled", lambda: False)


def test_scene_prompt_and_outline_cannot_borrow_unrelated_library_scene() -> None:
    bible = _bible("萧家迎客大厅", "萧家测验广场", "蛇人族大殿", "蛇人族城墙上空")
    screenplay = _screenplay()

    block = _scene_library_block(bible, screenplay)
    assert "蛇人族大殿" in block and "蛇人族城墙上空" in block
    assert "萧家迎客大厅" not in block and "萧家测验广场" not in block

    wrong = StoryboardOutline(
        episode_no=209,
        shots=[
            StoryboardOutlineShot(shot_no=1, scene_setting="日，萧家迎客大厅", beat="月媚通报敌情并等待墨巴斯"),
            StoryboardOutlineShot(shot_no=2, scene_setting="日，萧家测验广场", beat="墨巴斯飞到城墙上空质问古河"),
        ],
    )
    errors = validate_storyboard_outline_scene_alignment(wrong, screenplay, bible)
    assert any("大纲第 1 镜误用了" in error and "萧家迎客大厅" in error for error in errors)


def test_each_shot_must_follow_its_outline_scene() -> None:
    bible = _bible("蛇人族大殿", "蛇人族城墙上空")
    errors = validate_storyboard_shot_scene_alignment(
        _shot("日，蛇人族城墙上空"),
        _screenplay(),
        bible,
        expected_scene_setting="日，蛇人族大殿",
    )
    assert any("第 1 镜" in error and "本镜必须使用「蛇人族大殿」" in error for error in errors)


def test_overlapping_scene_aliases_do_not_create_false_missing_scene() -> None:
    bible = _bible("大青山山顶", "大青山顶山崖")
    bible.scenes[0].aliases = ["黄昏 / 大青山顶"]
    bible.scenes[1].aliases = ["黄昏 / 大青山顶边缘至山崖", "黄昏 / 大青山崖边"]
    screenplay = _screenplay().model_copy(update={
        "episode_no": 1,
        "scene_outline": [
            ScriptScene(
                scene_no=1,
                scene_heading="黄昏 / 大青山顶",
                story_function="孟浩在山顶结束落榜后的自省",
                summary="孟浩在山顶扔下葫芦并准备下山。",
            ),
            ScriptScene(
                scene_no=2,
                scene_heading="黄昏 / 大青山顶边缘至山崖",
                story_function="孟浩到崖边发现被困的王有材",
                summary="孟浩在山崖边听到呼救并俯身查看。",
            ),
        ],
    })
    board = Storyboard(
        episode_no=1,
        shots=[
            _shot("黄昏，大青山山顶", shot_no=1),
            _shot("黄昏，大青山顶山崖", shot_no=2),
        ],
    )
    # 模拟旧版「先遇到短别名就返回」造成的历史误绑定。
    board.shots[1].scene_name = "大青山山顶"

    assert validate_storyboard_scenes(board, bible) == []
    assert board.shots[1].scene_time == "黄昏"
    assert board.shots[1].scene_name == "大青山顶山崖"
    assert validate_storyboard_screenplay_scene_alignment(board, screenplay, bible) == []


def test_canonical_scene_name_outranks_same_text_legacy_alias() -> None:
    bible = _bible("白洁家客厅", "白洁王申的家")
    bible.scenes[0].aliases = ["【场8】晚上 到家后 / 白洁王申的家"]

    assert match_scene_name("白洁王申的家", bible.scenes) == "白洁王申的家"


def test_validated_scene_binding_is_persisted_for_downstream_generation() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE shots (id TEXT, episode_id TEXT, shot_no INTEGER, "
        "scene_time TEXT, scene_setting TEXT, scene_name TEXT)"
    )
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,scene_time,scene_setting,scene_name) "
        "VALUES('shot-5','ep-1',5,'','黄昏，大青山顶山崖',?)",
        ("大青山山顶",),
    )
    shot = _shot("黄昏，大青山顶山崖", shot_no=5)
    shot.scene_time = "黄昏"
    shot.scene_name = "大青山顶山崖"

    changed = _sync_storyboard_scene_bindings(
        conn, "ep-1", Storyboard(episode_no=1, shots=[shot]),
    )

    assert changed == 1
    row = conn.execute(
        "SELECT scene_time,scene_setting,scene_name FROM shots WHERE id='shot-5'"
    ).fetchone()
    assert dict(row) == {
        "scene_time": "黄昏",
        "scene_setting": "黄昏，大青山顶山崖",
        "scene_name": "大青山顶山崖",
    }


def test_scene_projection_reconciliation_does_not_wait_for_full_episode_success() -> None:
    bible = _bible("大青山山顶", "大青山顶山崖")
    bible.scenes[0].aliases = ["黄昏 / 大青山顶"]
    bible.scenes[1].aliases = ["黄昏 / 大青山顶边缘至山崖"]
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=5,
                scene_setting="黄昏，大青山顶山崖",
                beat="孟浩听到呼救走到崖边向下看",
            ),
        ],
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE episodes ("
        "id TEXT PRIMARY KEY, "
        "storyboard_outline_json TEXT, "
        "screenplay_json TEXT, "
        "target_duration_authority TEXT NOT NULL DEFAULT 'planning_estimate'"
        ")"
    )
    conn.execute(
        "CREATE TABLE shots (id TEXT, episode_id TEXT, shot_no INTEGER, "
        "scene_time TEXT, scene_setting TEXT, scene_name TEXT)"
    )
    conn.execute(
        "INSERT INTO episodes(id, storyboard_outline_json) VALUES('ep-1', ?)",
        (outline.model_dump_json(),),
    )
    conn.execute(
        "INSERT INTO shots VALUES('shot-5','ep-1',5,'','黄昏，大青山顶山崖','大青山山顶')"
    )

    changed = _reconcile_storyboard_scene_projection(conn, "ep-1", bible)

    assert changed == {"shots": 1, "outline_shots": 1}
    shot = conn.execute(
        "SELECT scene_time,scene_setting,scene_name FROM shots WHERE id='shot-5'"
    ).fetchone()
    assert dict(shot) == {
        "scene_time": "黄昏",
        "scene_setting": "黄昏，大青山顶山崖",
        "scene_name": "大青山顶山崖",
    }
    saved_outline = StoryboardOutline.model_validate_json(
        conn.execute(
            "SELECT storyboard_outline_json FROM episodes WHERE id='ep-1'"
        ).fetchone()["storyboard_outline_json"]
    )
    assert saved_outline.shots[0].scene_time == "黄昏"
    assert saved_outline.shots[0].scene_name == "大青山顶山崖"


def test_scene_preflight_waits_for_each_relevant_scene_image(tmp_path, monkeypatch) -> None:
    bible = _bible("萧家迎客大厅", "蛇人族大殿", "蛇人族城墙上空")
    _fresh_project(tmp_path, monkeypatch, bible)
    generated: list[str] = []
    _install_fake_scene_generator(tmp_path, monkeypatch, generated)
    monkeypatch.setattr(scenes, "screen_scene_state_changes", lambda *_args, **_kwargs: {})

    result = asyncio.run(scenes.ensure_scenes_for_storyboard("p1", 209, _screenplay(), bible))

    assert result["blocking_errors"] == []
    assert generated == ["蛇人族大殿", "蛇人族城墙上空"]
    assert "萧家迎客大厅" not in generated
    for name in generated:
        row = db.get_conn().execute(
            "SELECT image_path FROM scene_references WHERE project_id='p1' AND scene_name=?",
            (name,),
        ).fetchone()
        assert row and Path(row["image_path"]).is_file()


def test_reactive_scene_recovery_resumes_missing_views_without_regenerating_main(
    tmp_path,
    monkeypatch,
) -> None:
    bible = _bible("蛇人族大殿")
    _fresh_project(tmp_path, monkeypatch, bible)
    image_path = tmp_path / "existing-main.jpg"
    image_path.write_bytes(b"existing-scene")
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO scene_references("
        "id,project_id,scene_name,ep_start,scene_canonical,prompt,image_path,"
        "qa_json,bible_version,created_at,pack_status"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            "scene-existing",
            "p1",
            "蛇人族大殿",
            209,
            bible.scenes[0].scene_canonical,
            "prompt",
            str(image_path),
            "{}",
            1,
            db.now(),
            "generating",
        ),
    )
    conn.commit()
    resumed: list[str] = []

    async def complete_pack(
        project_id: str,
        scene_name: str,
        episode_no: int,
        style: str,
    ) -> dict:
        resumed.append(
            f"{project_id}:{scene_name}:{episode_no}:{style}"
        )
        conn.execute(
            "UPDATE scene_references SET pack_status='ready' WHERE id=?",
            ("scene-existing",),
        )
        conn.commit()
        return {"ok": True, "status": "ready"}

    async def must_not_regenerate(*_args, **_kwargs):
        raise AssertionError("恢复缺失视角时不得重新生成已成功的主图")

    monkeypatch.setattr(
        "app.multiview.scene_multiview_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        "app.multiview.complete_legacy_scene_pack",
        complete_pack,
    )
    monkeypatch.setattr(
        "app.multiview.pack_result_ok",
        lambda result: bool(result.get("ok")),
    )
    monkeypatch.setattr(
        scenes,
        "_generate_and_register_scene",
        must_not_regenerate,
    )

    result = asyncio.run(scenes._ensure_reactive_scene_image(
        "p1",
        bible.scenes[0],
        episode_no=209,
        style=bible.world.visual_style_canonical,
        bible_version=1,
    ))

    assert result == {
        "image_path": str(image_path),
        "reused": True,
        "resumed_pack": True,
        "pack_status": "ready",
    }
    assert resumed == [
        "p1:蛇人族大殿:209:国风玄幻厚涂",
    ]
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM scene_references "
        "WHERE project_id='p1' AND scene_name='蛇人族大殿' AND ep_start=209"
    ).fetchone()["n"] == 1


def test_reactive_scene_pack_failure_preserves_paid_main_image(
    tmp_path,
    monkeypatch,
) -> None:
    bible = _bible("蛇人族大殿")
    _fresh_project(tmp_path, monkeypatch, bible)
    image_path = tmp_path / "existing-main.jpg"
    image_path.write_bytes(b"existing-scene")
    conn = db.get_conn()
    conn.execute(
        """INSERT INTO scene_references(
               id,project_id,scene_name,ep_start,scene_canonical,prompt,image_path,
               qa_json,bible_version,created_at,pack_status
           ) VALUES(
               'scene-existing','p1','蛇人族大殿',209,?,'prompt',?,'{}',1,?,'failed'
           )""",
        (bible.scenes[0].scene_canonical, str(image_path), db.now()),
    )
    conn.commit()

    async def fail_pack(*_args, **_kwargs):
        raise RuntimeError("side view provider unavailable")

    monkeypatch.setattr(
        "app.multiview.scene_multiview_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        "app.multiview.complete_legacy_scene_pack",
        fail_pack,
    )

    with pytest.raises(RuntimeError, match="side view provider unavailable"):
        asyncio.run(scenes._ensure_reactive_scene_image(
            "p1",
            bible.scenes[0],
            episode_no=209,
            style=bible.world.visual_style_canonical,
            bible_version=1,
        ))

    saved = conn.execute(
        "SELECT image_path,pack_status FROM scene_references WHERE id='scene-existing'"
    ).fetchone()
    assert dict(saved) == {
        "image_path": str(image_path),
        "pack_status": "failed",
    }
    assert image_path.is_file()


def test_new_screenplay_scene_is_ai_adopted_and_hidden_from_human_queue(tmp_path, monkeypatch) -> None:
    bible = _bible("萧家迎客大厅")
    _fresh_project(tmp_path, monkeypatch, bible)
    generated: list[str] = []
    _install_fake_scene_generator(tmp_path, monkeypatch, generated)

    async def fake_assess(*_args, **_kwargs):
        return {
            "subject_kind": "person",
            "important": True,
            "existing_scene_name": "",
            "reason": "本集真实开拍的新地点",
            "name": "蛇人族大殿",
            "scene_canonical": "蛇人族核心大殿内部，深色石柱与蛇纹火盆环绕，冷绿色天光压低空间，无人物",
            "location_kind": "室内",
        }

    monkeypatch.setattr(scenes, "assess_new_scene", fake_assess)
    monkeypatch.setattr(scenes, "screen_scene_state_changes", lambda *_args, **_kwargs: {})
    one_scene = _screenplay().model_copy(update={"scene_outline": [_screenplay().scene_outline[0]]})

    result = asyncio.run(scenes.ensure_scenes_for_storyboard("p1", 209, one_scene, bible))

    assert result["blocking_errors"] == []
    assert generated == ["蛇人族大殿"]
    project = db.get_conn().execute(
        "SELECT bible_json,bible_auto_changes_json FROM projects WHERE id='p1'",
    ).fetchone()
    current = json.loads(project["bible_json"])
    adopted = next(scene for scene in current["scenes"] if scene["name"] == "蛇人族大殿")
    assert "【场1】日 / 蛇人族大殿内" in adopted["aliases"]
    changes = json.loads(project["bible_auto_changes_json"])
    change = next(item for item in changes if item["scene"] == "蛇人族大殿")
    assert change["status"] == "auto_applied"
    assert change["decided_by"] == "ai_scene_preflight"


def test_scene_discovery_receives_spatial_context_without_plot_summary(
    tmp_path,
    monkeypatch,
) -> None:
    bible = _bible("萧家迎客大厅")
    _fresh_project(tmp_path, monkeypatch, bible)
    generated: list[str] = []
    _install_fake_scene_generator(tmp_path, monkeypatch, generated)
    received: list[str] = []

    async def fake_assess(_label, spatial_context, **_kwargs):
        received.append(spatial_context)
        return {
            "subject_kind": "person",
            "important": True,
            "existing_scene_name": "",
            "reason": "本集真实开拍的新地点",
            "name": "蛇人族大殿",
            "scene_canonical": (
                "蛇人族核心大殿内部，深色石柱与蛇纹火盆环绕，"
                "冷绿色天光压低空间，无人物"
            ),
            "location_kind": "室内",
        }

    monkeypatch.setattr(scenes, "assess_new_scene", fake_assess)
    monkeypatch.setattr(
        scenes,
        "screen_scene_state_changes",
        lambda *_args, **_kwargs: {},
    )
    one_scene = _screenplay().model_copy(
        update={"scene_outline": [_screenplay().scene_outline[0]]}
    )

    result = asyncio.run(
        scenes.ensure_scenes_for_storyboard("p1", 209, one_scene, bible)
    )

    assert result["blocking_errors"] == []
    assert received == ["蛇人族大殿内"]
    assert one_scene.scene_outline[0].summary not in received[0]


def test_new_interior_scene_is_not_collapsed_to_generic_exterior_alias(
    tmp_path,
    monkeypatch,
) -> None:
    bible = _bible("外宗宝阁前")
    bible.scenes[0].aliases = ["日 / 外宗宝阁"]
    _fresh_project(tmp_path, monkeypatch, bible)
    generated: list[str] = []
    _install_fake_scene_generator(tmp_path, monkeypatch, generated)

    async def fake_assess(*_args, **_kwargs):
        return {
            "subject_kind": "person",
            "important": True,
            "existing_scene_name": "",
            "reason": "宝阁内部与门前外景是不同物理空间",
            "name": "外宗宝阁内",
            "scene_canonical": (
                "室内日间，外宗宝阁内部多层木架陈列法器宝物，"
                "暖灰色调，国漫风格，电影感光影，无人物"
            ),
            "location_kind": "室内",
        }

    async def no_state_changes(*_args, **_kwargs):
        return {}

    screenplay = EpisodeScreenplay(
        episode_no=4,
        title="一面铜镜",
        full_script_text="【场1】日 / 宝阁内\n孟浩走入宝阁。",
        scene_outline=[
            ScriptScene(
                scene_no=1,
                scene_heading="日 / 宝阁内",
                story_function="孟浩发现铜镜",
                summary="孟浩在宝阁内部的木架间发现一面铜镜。",
                turn="孟浩拿起铜镜",
                source_basis="孟浩进入宝阁内部查看法宝",
            ),
        ],
    )
    monkeypatch.setattr(scenes, "assess_new_scene", fake_assess)
    monkeypatch.setattr(scenes, "screen_scene_state_changes", no_state_changes)

    result = asyncio.run(
        scenes.ensure_scenes_for_storyboard("p1", 4, screenplay, bible)
    )

    assert result["blocking_errors"] == []
    assert generated == ["外宗宝阁内"]
    current = Bible.model_validate_json(
        db.get_conn().execute(
            "SELECT bible_json FROM projects WHERE id='p1'"
        ).fetchone()["bible_json"]
    )
    assert {scene.name for scene in current.scenes} == {"外宗宝阁前", "外宗宝阁内"}


def test_scene_preflight_does_not_continue_when_own_image_is_unavailable(tmp_path, monkeypatch) -> None:
    bible = _bible("蛇人族大殿", "蛇人族城墙上空")
    _fresh_project(tmp_path, monkeypatch, bible)

    async def unavailable(*_args, **_kwargs):
        return None

    monkeypatch.setattr(scenes, "_generate_and_register_scene", unavailable)
    monkeypatch.setattr("app.multiview.scene_multiview_enabled", lambda: False)
    monkeypatch.setattr(scenes, "screen_scene_state_changes", lambda *_args, **_kwargs: {})

    result = asyncio.run(scenes.ensure_scenes_for_storyboard("p1", 209, _screenplay(), bible))

    assert len(result["blocking_errors"]) == 2
    assert all("自动场景图生成尚未就绪" in error for error in result["blocking_errors"])
