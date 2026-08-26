import asyncio
import sqlite3

import pytest
from fastapi import HTTPException

from app import api, db, task_registry
from app.api import BIBLE_INTERRUPTED_ERROR, _recover_orphan_bible_row
from app.schemas import Bible, Character, World


def test_orphan_running_bible_status_is_recovered() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, bible_status TEXT, bible_error TEXT)")
    conn.execute(
        "INSERT INTO projects(id, bible_status, bible_error) VALUES('proj_test', 'running', NULL)"
    )
    conn.commit()
    task_registry.cancel("bible", "proj_test")

    row = conn.execute("SELECT * FROM projects WHERE id='proj_test'").fetchone()
    recovered = _recover_orphan_bible_row(conn, row)

    assert recovered["bible_status"] == "failed"
    assert recovered["bible_error"] == BIBLE_INTERRUPTED_ERROR


def test_bible_task_starts_full_refs_after_success(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE chapters(project_id TEXT, idx INTEGER)")
    conn.execute(
        "CREATE TABLE projects("
        "id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER DEFAULT 0, "
        "bible_status TEXT, bible_error TEXT, status TEXT)"
    )
    conn.execute(
        "INSERT INTO projects(id, bible_json, bible_version, bible_status, bible_error, status) "
        "VALUES('proj_test', NULL, 0, 'running', NULL, NULL)"
    )
    conn.commit()

    async def fake_generate_bible(*_args, **_kwargs):
        return Bible(
            world=World(visual_style_canonical="国风水墨"),
            characters=[Character(name="萧炎", role="主角", appearance_canonical="黑发少年，玄色劲装，目光坚定，身形修长，腰间佩火纹玉佩")],
        )

    started: dict[str, object] = {}

    def fake_start_refs(project_id: str, only_character: str | None) -> bool:
        started["args"] = (project_id, only_character)
        return True

    monkeypatch.setattr(api, "get_conn", lambda: conn)
    monkeypatch.setattr(api, "generate_bible", fake_generate_bible)
    monkeypatch.setattr(api, "_start_refs_generation", fake_start_refs)

    asyncio.run(api._bible_task("proj_test", trigger_full_refs=True))

    row = conn.execute("SELECT * FROM projects WHERE id='proj_test'").fetchone()
    assert row["bible_status"] == "ready"
    assert row["status"] == "bible_ready"
    assert row["bible_version"] == 1
    assert started["args"] == ("proj_test", None)


def test_bible_task_starts_scene_preparation_without_unquoted_images(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE chapters(project_id TEXT, idx INTEGER)")
    conn.execute(
        "CREATE TABLE projects("
        "id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER DEFAULT 0, "
        "bible_status TEXT, bible_error TEXT, status TEXT, "
        "scene_refs_status TEXT DEFAULT 'idle', scene_refs_error TEXT)"
    )
    conn.execute(
        "INSERT INTO projects(id, bible_status, status, scene_refs_status) "
        "VALUES('proj_test', 'running', 'ingested', 'idle')"
    )
    conn.commit()

    async def fake_generate_bible(*_args, **_kwargs):
        return Bible(
            world=World(visual_style_canonical="国风水墨"),
            characters=[
                Character(
                    name="萧炎",
                    role="主角",
                    appearance_canonical="黑发少年，玄色劲装，目光坚定，身形修长，腰间佩火纹玉佩",
                )
            ],
        )

    monkeypatch.setattr(api, "get_conn", lambda: conn)
    monkeypatch.setattr(api, "generate_bible", fake_generate_bible)
    monkeypatch.setattr(api, "_start_refs_generation", lambda *_args: True)
    prepared: list[str] = []
    monkeypatch.setattr(
        api,
        "_start_scene_bible_preparation",
        lambda project_id: prepared.append(project_id) or True,
    )
    monkeypatch.setattr(
        task_registry,
        "spawn",
        lambda *_args, **_kwargs: pytest.fail("免费场景清单准备不应直接启动付费图片任务"),
    )

    asyncio.run(api._bible_task("proj_test", trigger_full_refs=True))

    row = conn.execute(
        "SELECT bible_status,scene_refs_status,scene_refs_error FROM projects "
        "WHERE id='proj_test'"
    ).fetchone()
    assert dict(row) == {
        "bible_status": "ready",
        "scene_refs_status": "idle",
        "scene_refs_error": None,
    }
    assert prepared == ["proj_test"]


def test_bible_completion_preserves_planned_project_status(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE chapters(project_id TEXT, idx INTEGER)")
    conn.execute(
        "CREATE TABLE projects("
        "id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER DEFAULT 0, "
        "bible_status TEXT, bible_error TEXT, plan_status TEXT, status TEXT)"
    )
    conn.execute(
        "INSERT INTO projects(id, bible_json, bible_version, bible_status, bible_error, plan_status, status) "
        "VALUES('proj_planned', NULL, 0, 'running', NULL, 'ready', 'planned')"
    )
    conn.commit()

    async def fake_generate_bible(*_args, **_kwargs):
        return Bible(
            world=World(visual_style_canonical="ink animation"),
            characters=[
                Character(
                    name="Hero",
                    role="lead",
                    appearance_canonical="black hair, blue coat, tall build",
                )
            ],
        )

    monkeypatch.setattr(api, "get_conn", lambda: conn)
    monkeypatch.setattr(api, "generate_bible", fake_generate_bible)
    monkeypatch.setattr(api, "_start_refs_generation", lambda *_args: True)

    asyncio.run(api._bible_task("proj_planned", trigger_full_refs=True))

    row = conn.execute("SELECT status, bible_status FROM projects WHERE id='proj_planned'").fetchone()
    assert row["bible_status"] == "ready"
    assert row["status"] == "planned"


def test_bible_task_rolls_back_pending_purge_before_logging_style_change_failure(monkeypatch) -> None:
    """回归锁：画风变更触发的全项目视频产物清理若中途失败，_bible_task 的顶层
    异常处理不能把这次失败尝试自己产生的未提交半成品写入一起提交掉。

    真实复现路径：app.domain.bible_ops._bible_task 重谱后发现
    bible.world.visual_style_canonical 变化，调用 _purge_for_style_change →
    worker.purge_project_video_artifacts，后者对全项目逐镜头 DELETE
    shot_versions/shot_scenes/jobs、逐集回退状态，整段过程故意不提交，只在
    处理完全部镜头后 conn.commit() 一次——中途任何一步失败（文件 I/O、约束
    冲突等）都会把尚未提交的部分 DELETE 留在这个连接上。_bible_task 的
    ``except (StageError, Exception)`` 此前直接调用 errors.record_and_format
    而不先回滚；errors.log_error 内部 app.db.insert_error_log 在同一个连接上
    无条件 conn.commit()，会把这份半成品定型进库——而且波及的是整个项目的
    视频产物，不止一集。这里用同一连接上的等价写入直接验证「守卫失败即回滚」
    这条边界，不依赖完整 shots/shot_versions schema。
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE chapters(project_id TEXT, idx INTEGER)")
    conn.execute(
        "CREATE TABLE projects("
        "id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER DEFAULT 0, "
        "bible_status TEXT, bible_error TEXT, status TEXT, "
        "bible_text_provider TEXT NOT NULL DEFAULT '')"
    )
    # 模拟真实清理会做的「未提交批量写入」：真实场景是 shot_versions 等表的
    # DELETE，这里用同一连接上的等价占位表验证事务边界，不引入完整 shots
    # schema。
    conn.execute("CREATE TABLE fake_video_artifact(marker TEXT)")
    conn.execute("INSERT INTO fake_video_artifact(marker) VALUES('pre-existing-real-video')")

    old_bible = Bible(
        world=World(visual_style_canonical="国风水墨"),
        characters=[Character(name="萧炎", role="主角", appearance_canonical="黑发少年，玄色劲装，目光坚定，身形修长，腰间佩火纹玉佩")],
    )
    conn.execute(
        "INSERT INTO projects(id, bible_json, bible_version, bible_status, bible_error, status) "
        "VALUES('proj_test', ?, 1, 'running', NULL, 'bible_ready')",
        (old_bible.model_dump_json(),),
    )
    conn.commit()

    async def fake_generate_bible(*_args, **_kwargs):
        return Bible(
            world=World(visual_style_canonical="赛博朋克"),  # 画风变化，触发 purge 分支
            characters=[Character(name="萧炎", role="主角", appearance_canonical="黑发少年，玄色劲装，目光坚定，身形修长，腰间佩火纹玉佩")],
        )

    purge_calls: list[str] = []

    def fake_purge_for_style_change(project_id, instance):
        # 复现真实 purge 的失败形状：先在这个连接上做一次未提交的写入
        # （模拟"部分镜头已经 DELETE"），再中途失败（模拟文件 I/O 报错）。
        purge_calls.append(project_id)
        conn.execute("DELETE FROM fake_video_artifact WHERE marker='pre-existing-real-video'")
        raise OSError("模拟清理旧画风视频文件时中途失败")

    monkeypatch.setattr(api, "get_conn", lambda: conn)
    monkeypatch.setattr(api, "generate_bible", fake_generate_bible)
    monkeypatch.setattr(api, "_purge_for_style_change", fake_purge_for_style_change)

    asyncio.run(api._bible_task("proj_test", trigger_full_refs=False))

    assert purge_calls == ["proj_test"], (
        "本测试要验证的正是 _purge_for_style_change 失败后的回滚时机；"
        "没有走到这一步说明测试提前在别处失败，结论不成立"
    )
    # 回滚必须先于 record_and_format 的隐式 commit：半成品清理痕迹不能落库。
    assert conn.in_transaction is False
    remaining = conn.execute(
        "SELECT COUNT(*) c FROM fake_video_artifact WHERE marker='pre-existing-real-video'"
    ).fetchone()["c"]
    assert remaining == 1, "画风变更清理中途失败时，已提交的真实视频记录绝不能被连带清空"

    row = conn.execute("SELECT bible_status, bible_error FROM projects WHERE id='proj_test'").fetchone()
    assert row["bible_status"] == "failed"
    # OSError 归类为技术类「媒体处理」错误，前端只看安全提示+错误码（原文脱敏
    # 进 error_logs），故这里只断言失败态已落库，不断言原始异常文案。
    assert row["bible_error"]


@pytest.mark.asyncio
async def test_bible_spawn_failure_restores_state_and_keeps_quote(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "bible-spawn.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id,name,status,bible_status,bible_error,created_at) "
        "VALUES('p1','P','ingested','idle','上次状态',1)"
    )
    conn.execute(
        "INSERT INTO chapters(project_id,idx,title,content,char_count) "
        "VALUES('p1',1,'第一章','正文',2)"
    )
    conn.commit()
    monkeypatch.setattr(api, "_require_harness_engine", lambda _project_id: None)
    precheck = api._compute_bible_generate_precheck("p1")
    quote = api._issue_payment_quote(precheck)

    def fail_spawn(_kind, _key, coro, *, project_id=None):
        coro.close()
        raise RuntimeError("event loop unavailable")

    monkeypatch.setattr(task_registry, "spawn", fail_spawn)

    with pytest.raises(HTTPException) as exc_info:
        await api._start_bible_core(
            "p1",
            "保留这条反馈",
            confirm=True,
            quote_id=quote["quote_id"],
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "BIBLE_START_FAILED"
    project = conn.execute(
        "SELECT bible_status,bible_error,bible_feedback FROM projects WHERE id='p1'"
    ).fetchone()
    assert dict(project) == {
        "bible_status": "idle",
        "bible_error": "上次状态",
        "bible_feedback": None,
    }
    assert conn.execute(
        "SELECT consumed_at FROM character_payment_quotes WHERE quote_id=?",
        (quote["quote_id"],),
    ).fetchone()["consumed_at"] is None


@pytest.mark.asyncio
async def test_bible_shutdown_keeps_running_projection_for_recovery(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "bible-shutdown.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id,name,status,bible_status,created_at) "
        "VALUES('p1','P','ingested','running',1)"
    )
    conn.execute(
        "INSERT INTO chapters(project_id,idx,title,content,char_count) "
        "VALUES('p1',1,'第一章','正文',2)"
    )
    conn.commit()

    async def interrupted(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(api, "generate_bible", interrupted)
    monkeypatch.setattr(task_registry, "shutdown_in_progress", lambda: True)

    with pytest.raises(asyncio.CancelledError):
        await api._bible_task("p1")

    project = conn.execute(
        "SELECT bible_status,bible_error FROM projects WHERE id='p1'"
    ).fetchone()
    assert dict(project) == {"bible_status": "running", "bible_error": None}
