import asyncio
import sqlite3

import pytest
from fastapi import HTTPException

from app import api, db, task_registry
from app.api import BIBLE_INTERRUPTED_ERROR, _recover_orphan_bible_row
from app.schemas import Bible, Character, World
from tests.conftest import patch_api_everywhere


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
            characters=[Character(name="甲一", role="主角", appearance_canonical="黑发少年，玄色劲装，目光坚定，身形修长，腰间佩火纹玉佩")],
        )

    started: dict[str, object] = {}

    def fake_start_refs(project_id: str, only_character: str | None, *, only_characters=None, **_kwargs) -> bool:
        started["args"] = (project_id, only_character, tuple(only_characters or []))
        return True

    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "generate_bible", fake_generate_bible)
    patch_api_everywhere(monkeypatch, "_start_refs_generation", fake_start_refs)

    asyncio.run(api._bible_task("proj_test", trigger_full_refs=True))

    row = conn.execute("SELECT * FROM projects WHERE id='proj_test'").fetchone()
    assert row["bible_status"] == "ready"
    assert row["status"] == "bible_ready"
    assert row["bible_version"] == 1
    # only_characters 必须显式带出全部具备定妆资格的角色名单——不能让
    # _start_refs_generation 靠「没传」自己去猜范围（见 precheck.py
    # _purge_for_style_change 与本文件顶部改动说明：established-gap 扫描在
    # 表刚被清空时会把整批错判成空，一个角色都不出图却仍报 refs_status=ready）。
    assert started["args"] == ("proj_test", None, ("甲一",))


def test_bible_task_skips_refs_regen_when_style_unchanged_with_existing_characters(monkeypatch) -> None:
    """回归锁（协调方 2026-08-31 打回）：重新判定世界观现在会原样带出已有角色
    （见 app.stages.generate_bible 的 previous_bible 处理），characters 不再
    恒为空——如果 _start_refs_generation 还像以前那样无条件触发，「重新判定
    世界观并更换画风」在画风没变时也会把已有角色的定妆照全部打回重做，产生
    真实的图片费用。画风未变时世界观判定不改动任何角色内容，不该触发定妆。
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE chapters(project_id TEXT, idx INTEGER)")
    conn.execute(
        "CREATE TABLE projects("
        "id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER DEFAULT 0, "
        "bible_status TEXT, bible_error TEXT, status TEXT)"
    )
    old_bible = Bible(
        world=World(visual_style_canonical="国风水墨，虚构数字角色，电影光影，古典留白"),
        characters=[Character(
            name="甲一", role="主角",
            appearance_canonical="黑发少年，玄色劲装，目光坚定，身形修长，腰间佩火纹玉佩",
            ref_image_path="/media/refs/jia_yi.png",
        )],
    )
    conn.execute(
        "INSERT INTO projects(id, bible_json, bible_version, bible_status, bible_error, status) "
        "VALUES('proj_test', ?, 1, 'running', NULL, 'bible_ready')",
        (old_bible.model_dump_json(),),
    )
    conn.commit()

    async def fake_generate_bible(*_args, previous_bible=None, **_kwargs):
        # 复刻真实 generate_bible：画风不变、角色原样带出。
        assert previous_bible is not None
        return Bible(
            world=World(
                era="架空古代", genre="东方仙侠",
                visual_style_canonical="国风水墨，虚构数字角色，电影光影，古典留白",
            ),
            characters=[Character(**c) for c in previous_bible["characters"]],
        )

    refs_started: list[str] = []
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "generate_bible", fake_generate_bible)
    patch_api_everywhere(monkeypatch, "_start_refs_generation",
        lambda project_id, *_a, **_k: refs_started.append(project_id) or True,
    )

    asyncio.run(api._bible_task("proj_test", trigger_full_refs=True))

    row = conn.execute("SELECT bible_status, bible_json FROM projects WHERE id='proj_test'").fetchone()
    assert row["bible_status"] == "ready"
    assert refs_started == [], "画风未变时不该触发定妆照重新生成"
    import json
    saved = json.loads(row["bible_json"])
    assert [c["name"] for c in saved["characters"]] == ["甲一"]
    assert saved["characters"][0]["ref_image_path"] == "/media/refs/jia_yi.png"


def test_bible_task_still_regens_refs_when_style_actually_changes(monkeypatch) -> None:
    """与上一条对照：画风确实变化时，旧定妆照被 _purge_for_style_change 判定
    失效，仍要触发定妆照重新生成——这条不能被上面那条「画风未变不触发」的
    修复连带误伤。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE chapters(project_id TEXT, idx INTEGER)")
    conn.execute(
        "CREATE TABLE projects("
        "id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER DEFAULT 0, "
        "bible_status TEXT, bible_error TEXT, status TEXT)"
    )
    old_bible = Bible(
        world=World(visual_style_canonical="国风水墨，虚构数字角色，电影光影，古典留白"),
        characters=[Character(
            name="甲一", role="主角",
            appearance_canonical="黑发少年，玄色劲装，目光坚定，身形修长，腰间佩火纹玉佩",
            ref_image_path="/media/refs/jia_yi.png",
        )],
    )
    conn.execute(
        "INSERT INTO projects(id, bible_json, bible_version, bible_status, bible_error, status) "
        "VALUES('proj_test', ?, 1, 'running', NULL, 'bible_ready')",
        (old_bible.model_dump_json(),),
    )
    conn.commit()

    async def fake_generate_bible(*_args, previous_bible=None, **_kwargs):
        return Bible(
            world=World(visual_style_canonical="赛博朋克霓虹质感，虚构数字角色，高对比光影"),
            characters=[Character(**c) for c in previous_bible["characters"]],
        )

    refs_started: list[str] = []
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "generate_bible", fake_generate_bible)
    patch_api_everywhere(monkeypatch, "_purge_for_style_change", lambda *_a, **_k: {})
    patch_api_everywhere(monkeypatch, "_start_refs_generation",
        lambda project_id, *_a, **_k: refs_started.append(project_id) or True,
    )

    asyncio.run(api._bible_task("proj_test", trigger_full_refs=True))

    assert refs_started == ["proj_test"], "画风确实变化时必须触发定妆照重新生成"


def test_bible_task_style_change_regenerates_every_character_not_just_first(monkeypatch) -> None:
    """回归锁（实战撞到：《我欲封天》换画风后 5 个角色只有 1 个重新出图）。

    _purge_for_style_change 会先把 character_portraits 整表清空；如果这里
    仍像旧代码一样只传 _start_refs_generation(project_id, None)（不显式给
    only_characters），它内部退化成的「已建卡角色缺口」扫描
    （_established_portrait_gap_names）会因为表刚被清空查到零个已建卡角色，
    把整批角色当空选中悄悄早退——一个都不会重新出图。这里用 5 个角色钉住：
    必须把全部具备定妆资格的角色名单传下去，不能只传第一个或漏传。
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE chapters(project_id TEXT, idx INTEGER)")
    conn.execute(
        "CREATE TABLE projects("
        "id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER DEFAULT 0, "
        "bible_status TEXT, bible_error TEXT, status TEXT)"
    )
    names = ["孟浩", "王有材", "上官修", "赵武刚", "王腾飞"]
    old_bible = Bible(
        world=World(visual_style_canonical="真人摄影风，实拍质感，自然光影"),
        characters=[
            Character(name=n, role="配角", appearance_canonical=f"{n}的外观描述占位")
            for n in names
        ],
    )
    conn.execute(
        "INSERT INTO projects(id, bible_json, bible_version, bible_status, bible_error, status) "
        "VALUES('proj_test', ?, 1, 'running', NULL, 'bible_ready')",
        (old_bible.model_dump_json(),),
    )
    conn.commit()

    async def fake_generate_bible(*_args, previous_bible=None, **_kwargs):
        return Bible(
            world=World(visual_style_canonical="国漫3D动画电影质感，虚构数字角色，精致光影"),
            characters=[Character(**c) for c in previous_bible["characters"]],
        )

    calls: list[tuple] = []
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "generate_bible", fake_generate_bible)
    patch_api_everywhere(monkeypatch, "_purge_for_style_change", lambda *_a, **_k: {})
    patch_api_everywhere(monkeypatch, "_start_refs_generation",
        lambda project_id, only_character, *, only_characters=None, **_k:
            calls.append((project_id, only_character, tuple(only_characters or []))) or True,
    )

    asyncio.run(api._bible_task("proj_test", trigger_full_refs=True))

    assert len(calls) == 1
    assert calls[0][0] == "proj_test"
    assert set(calls[0][2]) == set(names), "换画风必须重新生成人物谱里的每一个角色，不能只出第一个"


def test_bible_task_no_longer_auto_starts_scene_preparation(monkeypatch) -> None:
    """架构转向（2026-08-31）：generate_scene_bible 批量场景清单生成退出首版
    流程，_bible_task 成功后不再自动触发 _start_scene_bible_preparation——
    场景改为分镜阶段按需反应式发现（app.scenes.assess_new_scene）。这条用例
    钉住「不再自动触发」这一新契约，替换同名旧用例（旧用例断言
    prepared == ["proj_test"]，验的是已经退场的行为）。"""
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
        # 首版人物谱只产出 world，characters 恒为 []（见 app.stages.generate_bible）。
        return Bible(world=World(visual_style_canonical="国风水墨"), characters=[])

    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "generate_bible", fake_generate_bible)
    patch_api_everywhere(monkeypatch, "_start_refs_generation", lambda *_args, **_kwargs: True)
    prepared: list[str] = []
    patch_api_everywhere(monkeypatch,
        "_start_scene_bible_preparation",
        lambda project_id: prepared.append(project_id) or True,
    )
    monkeypatch.setattr(
        task_registry,
        "spawn",
        lambda *_args, **_kwargs: pytest.fail("场景清单准备不应再被 _bible_task 自动启动"),
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
    assert prepared == [], "场景清单生成必须只能由映射台/场景库手动触发，不再随人物谱谱写自动启动"


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

    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "generate_bible", fake_generate_bible)
    patch_api_everywhere(monkeypatch, "_start_refs_generation", lambda *_args, **_kwargs: True)

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
        characters=[Character(name="甲一", role="主角", appearance_canonical="黑发少年，玄色劲装，目光坚定，身形修长，腰间佩火纹玉佩")],
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
            characters=[Character(name="甲一", role="主角", appearance_canonical="黑发少年，玄色劲装，目光坚定，身形修长，腰间佩火纹玉佩")],
        )

    purge_calls: list[str] = []

    def fake_purge_for_style_change(project_id, instance):
        # 复现真实 purge 的失败形状：先在这个连接上做一次未提交的写入
        # （模拟"部分镜头已经 DELETE"），再中途失败（模拟文件 I/O 报错）。
        purge_calls.append(project_id)
        conn.execute("DELETE FROM fake_video_artifact WHERE marker='pre-existing-real-video'")
        raise OSError("模拟清理旧画风视频文件时中途失败")

    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "generate_bible", fake_generate_bible)
    patch_api_everywhere(monkeypatch, "_purge_for_style_change", fake_purge_for_style_change)

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
    patch_api_everywhere(monkeypatch, "_require_harness_engine", lambda _project_id: None)
    precheck = api._compute_bible_generate_precheck("p1")
    quote = api._issue_scope_quote(precheck)

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

    patch_api_everywhere(monkeypatch, "generate_bible", interrupted)
    monkeypatch.setattr(task_registry, "shutdown_in_progress", lambda: True)

    with pytest.raises(asyncio.CancelledError):
        await api._bible_task("p1")

    project = conn.execute(
        "SELECT bible_status,bible_error FROM projects WHERE id='p1'"
    ).fetchone()
    assert dict(project) == {"bible_status": "running", "bible_error": None}


@pytest.mark.asyncio
async def test_start_bible_core_recovers_stale_failed_project_without_model_call(
    tmp_path, monkeypatch,
) -> None:
    """存量修复回归锁：真实卡住的《我欲封天》项目停在 bible_status='failed'、
    bible_json 为空（HiAgent content_filter 拦下了旧版「轻量模型调用判定
    era/genre/画风」），但 bible_style_name 在导入时已经落库。二次拍板后
    generate_bible 不再发起任何模型调用，既有「重新生成人物谱」按钮天然就是
    自愈路径——这条用例复刻那批项目的真实落库状态，钉住重试能让它们恢复
    可用，且过程中不发起任何模型调用（model_gateway.chat_structured 一被
    调用就报错，不是靠打桩绕过它）。"""
    import json

    from app.harness import model_gateway
    from app.visual_styles import visual_style_prompt

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "bible-recover.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects("
        "id,name,status,bible_status,bible_error,bible_json,bible_style_name,plan_status,created_at"
        ") VALUES('p1','我欲封天','planned','failed',?,NULL,'真人摄影风','ready',1)",
        ("「内容生成」内容生成未通过格式或业务校验，可点击重试（错误码 GEN · ERR-20260831-577587）",),
    )
    conn.commit()
    patch_api_everywhere(monkeypatch, "_require_harness_engine", lambda _project_id: None)
    patch_api_everywhere(monkeypatch, "_start_refs_generation", lambda *_args, **_kwargs: None)

    async def fail_if_called(*_args, **_kwargs):  # pragma: no cover - 不该被调用
        raise AssertionError("重新生成人物谱不该再发起任何模型调用")

    monkeypatch.setattr(model_gateway, "chat_structured", fail_if_called)

    precheck = api._compute_bible_generate_precheck("p1", style_name="真人摄影风")
    quote = api._issue_scope_quote(precheck)

    result = await api._start_bible_core(
        "p1", "", confirm=True, quote_id=quote["quote_id"], style_name="真人摄影风",
    )

    assert result["status"] == "ready"
    row = conn.execute(
        "SELECT bible_status, bible_error, bible_json FROM projects WHERE id='p1'"
    ).fetchone()
    assert row["bible_status"] == "ready"
    assert row["bible_error"] is None
    saved = json.loads(row["bible_json"])
    assert saved["world"]["visual_style_canonical"] == visual_style_prompt("真人摄影风")
    assert saved["characters"] == []


@pytest.mark.asyncio
async def test_start_bible_core_confirm_flow_from_409_precheck_succeeds(
    tmp_path, monkeypatch,
) -> None:
    """回归锁：真实故障复现路径。POST /projects/{id}/bible 不带 confirm 时，
    _start_bible_core 走 ``if not confirm: raise _payment_confirm_required(precheck)``
    这条分支——之前 precheck 是 _compute_bible_generate_precheck 的原始返回值，
    其中占位的 ``quote_id`` 字段实际装的是 scope_fingerprint，从未写进
    character_payment_quotes。任何按 409 响应指引（带 confirm=true + 该
    quote_id）再来一次的调用方都会在 _validate_scope_quote 里查不到这一行，
    命中 QUOTE_STALE——而"请重新确认"没有出路，因为重新预检拿到的还是同一个
    假值，是死循环（实测复现：尝试恢复 proj_f8cf2eeb2e66 时撞上）。

    这条用例钉住修复：未确认调用必须先 _issue_scope_quote() 把报价落库，
    409 响应里的 quote_id 才是一个真正能拿去确认的凭证。"""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "bible-409-confirm.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id,name,status,bible_status,bible_error,created_at) "
        "VALUES('p1','P','ingested','idle',NULL,1)"
    )
    conn.commit()
    patch_api_everywhere(monkeypatch, "_require_harness_engine", lambda _project_id: None)
    patch_api_everywhere(monkeypatch, "_start_refs_generation", lambda *_args, **_kwargs: None)

    # 第一步：不带 confirm 调用，复刻真实的 POST /projects/{id}/bible {} 请求。
    with pytest.raises(HTTPException) as first_call:
        await api._start_bible_core("p1", "", confirm=False)
    assert first_call.value.status_code == 409
    assert first_call.value.detail["code"] == "PAYMENT_CONFIRM_REQUIRED"
    precheck = first_call.value.detail["precheck"]
    quote_id_from_409 = precheck["quote_id"]
    assert quote_id_from_409, "409 响应必须带一个 quote_id"
    assert quote_id_from_409 != precheck["scope_fingerprint"], (
        "quote_id 不能等于 scope_fingerprint——那是未签发的占位值，"
        "从未写进 character_payment_quotes，必然导致 QUOTE_STALE"
    )
    issued_row = conn.execute(
        "SELECT * FROM character_payment_quotes WHERE quote_id=?", (quote_id_from_409,),
    ).fetchone()
    assert issued_row is not None, "409 里的 quote_id 必须已经落库，否则调用方无路可走"

    # 第二步：完全按 409 响应指引——带 confirm=true 与它给的 quote_id 再调一次。
    result = await api._start_bible_core(
        "p1", "", confirm=True, quote_id=quote_id_from_409,
    )

    assert result["status"] == "ready"
    row = conn.execute("SELECT bible_status FROM projects WHERE id='p1'").fetchone()
    assert row["bible_status"] == "ready"
