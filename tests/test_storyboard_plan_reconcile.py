"""分镜计划自更新（_reconcile_storyboard_plan）单测。

覆盖"规划十几镜却分镜24"的透明化修复：逐镜阶段大纲被就地拆分、或模型拆镜超出计划长度时，
落库大纲会追平实际镜头数并留下 harness 事件，使前端 storyboard_planned_shots 实时自更新、
单调不减且始终 ≥ 已通过镜头数。
"""

from app import api, db
from app.schemas import Shot, StoryboardOutline, StoryboardOutlineShot


def _shot(no: int) -> Shot:
    return Shot(
        shot_no=no,
        duration_s=5,
        shot_size="中景",
        camera_move="固定",
        scene_setting="日，萧家广场",
        characters=["萧炎"],
        action_desc=f"萧炎在测验碑前推进第 {no} 段剧情，掌心收力，眼神转冷。",
        first_frame_desc="萧炎站在测验碑前，手掌贴着碑面，神情平静。",
        last_frame_desc="同一机位，萧炎手掌攥成拳，指节发白。",
        source_excerpt="少年面无表情，唇角有着一抹自嘲，缓缓攥紧了手掌。",
    )


def _outline(n: int) -> StoryboardOutline:
    return StoryboardOutline(
        episode_no=2,
        shots=[StoryboardOutlineShot(shot_no=i, scene_setting="日，萧家广场",
                                     beat=f"节拍{i}") for i in range(1, n + 1)],
    )


def _setup_db(tmp_path, monkeypatch, *, planned: int) -> str:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "reconcile.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, bible_json, created_at) "
        "VALUES('p1','P','planned',NULL,1)"
    )
    conn.execute(
        """INSERT INTO episodes(
            id, project_id, episode_no, title, hook, cliffhanger, synopsis,
            source_chapters, target_duration_s, status, created_at, storyboard_outline_json
        ) VALUES('e1','p1',2,'Episode','','','', '[1]', 50, 'scripting', 1, ?)""",
        (_outline(planned).model_dump_json(),),
    )
    conn.commit()
    return "e1"


def _persisted_outline_len(conn) -> int:
    row = conn.execute("SELECT storyboard_outline_json FROM episodes WHERE id='e1'").fetchone()
    return len(StoryboardOutline.model_validate_json(row["storyboard_outline_json"]).shots)


def test_shot_overflow_extends_and_persists_plan(tmp_path, monkeypatch) -> None:
    # 模型拆镜超出计划：补占位节拍追平实际，回写 DB，返回 shot_overflow。
    episode_id = _setup_db(tmp_path, monkeypatch, planned=3)
    conn = db.get_conn()
    outline = _outline(3)
    completed = [_shot(i) for i in range(1, 6)]  # 实际 5 镜 > 计划 3 镜

    revision = api._reconcile_storyboard_plan(conn, episode_id, 2, outline, completed, 3)

    assert revision == (3, 5, "shot_overflow")
    assert len(outline.shots) == 5
    assert [s.shot_no for s in outline.shots] == [1, 2, 3, 4, 5]
    assert _persisted_outline_len(conn) == 5


def test_covers_split_growth_is_persisted(tmp_path, monkeypatch) -> None:
    # 大纲已被 covers 自动拆分（内存 4 条），实际仅 3 镜：不追加占位，但仍需回写落库长度。
    episode_id = _setup_db(tmp_path, monkeypatch, planned=3)
    conn = db.get_conn()
    outline = _outline(4)  # 拆分已在 generate_storyboard_next_shot 内完成
    completed = [_shot(i) for i in range(1, 4)]

    revision = api._reconcile_storyboard_plan(conn, episode_id, 2, outline, completed, 3)

    assert revision == (3, 4, "covers_split")
    assert len(outline.shots) == 4
    assert _persisted_outline_len(conn) == 4


def test_no_change_returns_none_and_is_monotonic(tmp_path, monkeypatch) -> None:
    # 计划长度与已落库一致（实际未超计划、无拆分）：不回写、不发事件、不倒退。
    episode_id = _setup_db(tmp_path, monkeypatch, planned=5)
    conn = db.get_conn()
    outline = _outline(5)
    completed = [_shot(i) for i in range(1, 4)]  # 3 镜 < 计划 5 镜

    revision = api._reconcile_storyboard_plan(conn, episode_id, 2, outline, completed, 5)

    assert revision is None
    assert len(outline.shots) == 5
    assert _persisted_outline_len(conn) == 5


def test_missing_outline_is_noop(tmp_path, monkeypatch) -> None:
    # 无大纲（规划失败回退纯逐镜）：对账应安全跳过。
    episode_id = _setup_db(tmp_path, monkeypatch, planned=3)
    conn = db.get_conn()
    assert api._reconcile_storyboard_plan(conn, episode_id, 2, None, [_shot(1)], 0) is None
