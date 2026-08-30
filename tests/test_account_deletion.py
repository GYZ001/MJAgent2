"""账号删除（自删 / 管理员软删 / 恢复 / 到期清理）的硬证明。

镜像 tests/test_core_regressions.py 里项目回收站那组测试的既有夹具风格：
独立临时 DB + PROJECTS_DIR，第二条独立连接验证落盘结果，deleted_at 时间戳
直接拨表模拟"时间流逝"（不依赖任何内存计时器）。
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest
from fastapi import HTTPException

from app import db
from app.auth.principal import Principal, set_current_principal
from tests.conftest import patch_worker_everywhere


def _init_db(tmp_path, monkeypatch, name: str):
    from app import config

    monkeypatch.setattr(db, "DB_PATH", tmp_path / name)
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    project_root = tmp_path / "projects"
    monkeypatch.setattr(config, "PROJECTS_DIR", project_root)
    return db.get_conn(), project_root


def _insert_user(conn, user_id: str, *, is_admin: bool = False, deleted_at=None) -> None:
    conn.execute(
        "INSERT INTO users(id, username, display_name, password_hash, status, "
        "is_system_admin, must_change_password, created_at, deleted_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (user_id, user_id, user_id, "hash", "active", 1 if is_admin else 0, 0, 1, deleted_at),
    )
    conn.commit()


def _insert_project(conn, project_root, project_id: str, owner_user_id: str,
                     *, deleted_at=None, retention_s=None):
    media = project_root / project_id / "scene_refs" / "scene.jpg"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"image")
    conn.execute(
        "INSERT INTO projects(id,name,status,created_at,owner_user_id,deleted_at,"
        "recycle_bin_retention_s) VALUES(?,?,?,?,?,?,?)",
        (project_id, "P", "planned", 1, owner_user_id, deleted_at, retention_s),
    )
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,status,created_at) "
        "VALUES(?,?,1,'planned',1)", (f"{project_id}-e1", project_id),
    )
    conn.commit()
    return media


@pytest.fixture(autouse=True)
def _clear_principal():
    set_current_principal(None)
    yield
    set_current_principal(None)


def test_self_delete_purges_account_and_all_project_disk_state(tmp_path, monkeypatch):
    """硬证明 1：自删确认后，账号与全部项目资源（数据库行 + 磁盘产物）都没了；
    用第二条独立连接读盘验证磁盘目录确实被删。"""
    from app.auth.sessions import create_session
    from app.domain.account_deletion import self_delete_account_core

    conn, project_root = _init_db(tmp_path, monkeypatch, "self-delete.db")
    _insert_user(conn, "u-owner")
    media = _insert_project(conn, project_root, "p1", "u-owner")
    create_session("u-owner")
    assert conn.execute("SELECT COUNT(*) FROM user_sessions WHERE user_id='u-owner'").fetchone()[0] == 1

    set_current_principal(Principal(user_id="u-owner", username="u-owner", is_system_admin=False))
    result = asyncio.run(self_delete_account_core())
    assert result["deleted_user_id"] == "u-owner"
    assert result["projects"]["purged"] == ["p1"]

    # 独立连接读盘，不是同一连接读自己刚写的东西。
    verify_conn = sqlite3.connect(db.DB_PATH)
    verify_conn.row_factory = sqlite3.Row
    assert verify_conn.execute("SELECT COUNT(*) FROM users WHERE id='u-owner'").fetchone()[0] == 0
    assert verify_conn.execute("SELECT COUNT(*) FROM projects WHERE id='p1'").fetchone()[0] == 0
    assert verify_conn.execute("SELECT COUNT(*) FROM episodes WHERE project_id='p1'").fetchone()[0] == 0
    # user_sessions 靠 FOREIGN KEY ... ON DELETE CASCADE 自动清空。
    assert verify_conn.execute("SELECT COUNT(*) FROM user_sessions WHERE user_id='u-owner'").fetchone()[0] == 0
    verify_conn.close()
    assert not media.exists()
    assert not (project_root / "p1").exists()


def test_self_delete_requires_confirmation_at_the_http_boundary(tmp_path, monkeypatch):
    """硬证明 4（自删要加确认）：REST 层不带 confirm=true 直接拒绝，且完全不
    触碰任何数据；带 confirm=true 才真正执行——用 TestClient 走真实 HTTP 路径。"""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.auth.sessions import create_session

    conn, project_root = _init_db(tmp_path, monkeypatch, "self-delete-confirm.db")
    _insert_user(conn, "u-owner2")
    _insert_project(conn, project_root, "p-confirm", "u-owner2")
    token = create_session("u-owner2")

    client = TestClient(app)
    resp = client.delete("/api/auth/me", headers={"X-Manju-Session": token})
    assert resp.status_code == 422
    body = resp.json()["detail"]
    assert body["code"] == "confirmation_required"
    assert body["project_count"] == 1
    # 未确认：账号与项目原封不动。
    assert conn.execute("SELECT COUNT(*) FROM users WHERE id='u-owner2'").fetchone()[0] == 1
    assert (project_root / "p-confirm").exists()

    resp2 = client.delete("/api/auth/me?confirm=true", headers={"X-Manju-Session": token})
    assert resp2.status_code == 200
    assert resp2.json()["ok"] is True
    assert not (project_root / "p-confirm").exists()


def test_admin_soft_delete_leaves_disk_untouched(tmp_path, monkeypatch):
    """硬证明 2：管理员软删——账号与项目都软删，磁盘文件一个没动。"""
    from app.domain.account_deletion import admin_soft_delete_account_core
    from app.domain.projects import ACCOUNT_DELETE_RETENTION_S

    conn, project_root = _init_db(tmp_path, monkeypatch, "admin-soft-delete.db")
    _insert_user(conn, "u-admin", is_admin=True)
    _insert_user(conn, "u-target")
    media = _insert_project(conn, project_root, "p2", "u-target")

    set_current_principal(Principal(user_id="u-admin", username="u-admin", is_system_admin=True))
    result = asyncio.run(admin_soft_delete_account_core("u-target"))
    assert result["deleted_user_id"] == "u-target"
    assert result["purge_at"] == pytest.approx(result["deleted_at"] + ACCOUNT_DELETE_RETENTION_S)
    assert result["projects"]["soft_deleted"] == ["p2"]

    verify_conn = sqlite3.connect(db.DB_PATH)
    verify_conn.row_factory = sqlite3.Row
    user_row = verify_conn.execute("SELECT deleted_at,status FROM users WHERE id='u-target'").fetchone()
    assert user_row["deleted_at"] is not None
    assert user_row["status"] == "disabled"
    proj_row = verify_conn.execute(
        "SELECT deleted_at, recycle_bin_retention_s FROM projects WHERE id='p2'"
    ).fetchone()
    assert proj_row["deleted_at"] is not None
    assert proj_row["recycle_bin_retention_s"] == ACCOUNT_DELETE_RETENTION_S
    # 数据库行还在、只是标记软删除；磁盘一个文件都不能少。
    for table in ("episodes",):
        assert verify_conn.execute(f"SELECT COUNT(*) FROM {table} WHERE project_id='p2'").fetchone()[0] == 1
    verify_conn.close()
    assert media.exists()
    assert (project_root / "p2").exists()


def test_retention_periods_do_not_interfere(tmp_path, monkeypatch):
    """硬证明 3：同一套 sweep 区分两种保留期。
    - 普通回收站项目（24 小时）：25 小时前删除 -> 该被清。
    - 账号级联项目（30 天）：25 小时前删除 -> 不该被清；31 天前删除 -> 该被清。
    """
    from app.domain.projects import ACCOUNT_DELETE_RETENTION_S, sweep_expired_deleted_projects

    conn, project_root = _init_db(tmp_path, monkeypatch, "retention-sweep.db")
    _insert_user(conn, "u-a")
    stamp = db.now()
    _insert_project(conn, project_root, "p-plain-25h", "u-a", deleted_at=stamp - 25 * 3600)
    _insert_project(
        conn, project_root, "p-cascade-25h", "u-a",
        deleted_at=stamp - 25 * 3600, retention_s=ACCOUNT_DELETE_RETENTION_S,
    )
    _insert_project(
        conn, project_root, "p-cascade-31d", "u-a",
        deleted_at=stamp - 31 * 24 * 3600, retention_s=ACCOUNT_DELETE_RETENTION_S,
    )

    # 模拟"重启后"：清空进程内连接缓存，sweep 只能靠时间戳工作。
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    result = asyncio.run(sweep_expired_deleted_projects())

    assert sorted(result["purged"]) == ["p-cascade-31d", "p-plain-25h"]
    assert result["failed"] == []

    verify_conn = sqlite3.connect(db.DB_PATH)
    verify_conn.row_factory = sqlite3.Row
    assert verify_conn.execute("SELECT COUNT(*) FROM projects WHERE id='p-plain-25h'").fetchone()[0] == 0
    assert verify_conn.execute("SELECT COUNT(*) FROM projects WHERE id='p-cascade-25h'").fetchone()[0] == 1
    assert verify_conn.execute("SELECT COUNT(*) FROM projects WHERE id='p-cascade-31d'").fetchone()[0] == 0
    verify_conn.close()
    assert not (project_root / "p-plain-25h").exists()
    assert (project_root / "p-cascade-25h").exists()
    assert not (project_root / "p-cascade-31d").exists()


def test_admin_restore_brings_back_only_the_cascaded_projects(tmp_path, monkeypatch):
    """硬证明 5：30 天内恢复账号，级联软删的项目一并回来；用户此前自己放进
    回收站的项目不受账号恢复影响，保留原判。"""
    from app.domain.account_deletion import admin_restore_account_core, admin_soft_delete_account_core
    from app.domain.projects import _delete_project_core

    conn, project_root = _init_db(tmp_path, monkeypatch, "admin-restore.db")
    _insert_user(conn, "u-admin", is_admin=True)
    _insert_user(conn, "u-target")
    _insert_project(conn, project_root, "p-active", "u-target")
    _insert_project(conn, project_root, "p-own-recycle", "u-target")
    # 用户自己先把 p-own-recycle 放进了 24 小时回收站。
    asyncio.run(_delete_project_core("p-own-recycle"))

    set_current_principal(Principal(user_id="u-admin", username="u-admin", is_system_admin=True))
    asyncio.run(admin_soft_delete_account_core("u-target"))
    # p-active 现在也进了回收站（30 天级联）；p-own-recycle 保留原 24 小时判据。
    restore_result = asyncio.run(admin_restore_account_core("u-target"))
    assert restore_result["projects"]["restored"] == ["p-active"]

    user_row = conn.execute("SELECT deleted_at,status FROM users WHERE id='u-target'").fetchone()
    assert user_row["deleted_at"] is None
    assert user_row["status"] == "active"
    active_row = conn.execute(
        "SELECT deleted_at, recycle_bin_retention_s FROM projects WHERE id='p-active'"
    ).fetchone()
    assert active_row["deleted_at"] is None
    assert active_row["recycle_bin_retention_s"] is None
    # 用户自己独立做过的删除操作，账号恢复不应该顺带撤销。
    own_recycle_row = conn.execute(
        "SELECT deleted_at FROM projects WHERE id='p-own-recycle'"
    ).fetchone()
    assert own_recycle_row["deleted_at"] is not None


def test_last_system_admin_cannot_be_deleted_self_or_by_others(tmp_path, monkeypatch):
    """硬证明 6：最后一个系统管理员不可删——自删、互删都要拦。"""
    from app.domain.account_deletion import admin_soft_delete_account_core, self_delete_account_core

    conn, project_root = _init_db(tmp_path, monkeypatch, "last-admin.db")
    _insert_user(conn, "u-only-admin", is_admin=True)
    _insert_user(conn, "u-plain")

    # 互删：另一个（非管理员）身份也拦不住这条判据本身要生效——判据只看目标
    # 是不是最后一个管理员，与操作者是谁无关；用系统管理员本人发起更贴近真实
    # 场景（普通用户根本走不到这个端点，被 require_system_admin 挡在更外层）。
    set_current_principal(Principal(user_id="u-only-admin", username="u-only-admin", is_system_admin=True))
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(admin_soft_delete_account_core("u-only-admin"))
    assert exc_info.value.status_code == 422

    # 自删：同一条判据也拦自己删自己。
    with pytest.raises(HTTPException) as exc_info2:
        asyncio.run(self_delete_account_core())
    assert exc_info2.value.status_code == 422

    # 账号原封不动。
    assert conn.execute("SELECT COUNT(*) FROM users WHERE id='u-only-admin'").fetchone()[0] == 1

    # 补第二个管理员后，删除第一个管理员应该放行——证明判据不是一刀切拦所有人。
    _insert_user(conn, "u-second-admin", is_admin=True)
    result = asyncio.run(admin_soft_delete_account_core("u-only-admin"))
    assert result["deleted_user_id"] == "u-only-admin"


def test_purge_transaction_rolls_back_before_touching_disk(tmp_path, monkeypatch):
    """硬证明（事务原子性）：DB 事务中途失败必须整体回滚，且 rmtree 完全不会
    被调用——"数据库删除先提交成功，rmtree 才执行"这条顺序保证的反向验证。"""
    from app.domain.account_deletion import self_delete_account_core

    conn, project_root = _init_db(tmp_path, monkeypatch, "purge-atomic.db")
    _insert_user(conn, "u-owner3")
    media = _insert_project(conn, project_root, "p3", "u-owner3")

    rmtree_calls: list[str] = []
    import shutil
    real_rmtree = shutil.rmtree

    def _spy_rmtree(path, *a, **kw):
        rmtree_calls.append(str(path))
        return real_rmtree(path, *a, **kw)

    monkeypatch.setattr(shutil, "rmtree", _spy_rmtree)

    def _boom(*a, **kw):
        raise RuntimeError("simulated mid-transaction failure")

    # 用 patch_worker_everywhere 而不是裸 monkeypatch.setattr(worker, ...)：
    # app.media_exec 是真包，每个子模块持有自己的绑定，改包属性只命中
    # app.worker 的再导出。当前调用点 app/domain/projects.py:1842 用的是
    # 限定访问 worker.delete_project_episodes(...)，裸打桩碰巧生效——但哪天
    # 它改成本地绑定，裸打桩就会静默失效、测试照常绿（CLAUDE.md 记录过的
    # 陷阱），tests/test_worker_monkeypatch_guard.py 正是守这一类。
    patch_worker_everywhere(monkeypatch, "delete_project_episodes", _boom)

    set_current_principal(Principal(user_id="u-owner3", username="u-owner3", is_system_admin=False))
    with pytest.raises(RuntimeError):
        asyncio.run(self_delete_account_core())

    # 回滚：项目行、账号行都还在；rmtree 从未被调用——磁盘原封不动。
    verify_conn = sqlite3.connect(db.DB_PATH)
    verify_conn.row_factory = sqlite3.Row
    assert verify_conn.execute("SELECT COUNT(*) FROM projects WHERE id='p3'").fetchone()[0] == 1
    assert verify_conn.execute("SELECT COUNT(*) FROM users WHERE id='u-owner3'").fetchone()[0] == 1
    verify_conn.close()
    assert rmtree_calls == []
    assert media.exists()
    assert (project_root / "p3").exists()


def test_retention_tag_is_committed_before_the_soft_delete(tmp_path, monkeypatch):
    """崩溃窗口必须落在良性状态上。

    ``_cascade_soft_delete_owner_projects`` 里两步各自提交，无法合成一个事务
    （``_delete_project_core`` 自带连接与提交，还要动磁盘产物），所以中间必然
    有一个崩溃窗口。能选的只是它落在哪个状态：

    - 先删后标记：崩溃后项目 ``deleted_at`` 已置、``recycle_bin_retention_s``
      为 NULL，sweep 的 ``COALESCE`` 把它判成 24 小时——管理员承诺的 30 天可
      恢复期缩水成 1 天，**真丢数据**。
    - 先标记后删：崩溃后项目仍是活跃的，只是多带一个 retention 值，而该值只在
      ``deleted_at`` 非空时才会被 sweep 读到——完全无害。

    本用例在 ``_delete_project_core`` 被调用的那一刻、用**另一条独立连接**读盘
    上数据，锁死"标记此时已经落盘"这个顺序不变量。用独立连接是因为同一连接读
    自己未提交的写入必然成功，证明不了"真提交"。
    """
    import app.domain.projects as projects_mod
    from app.domain.account_deletion import admin_soft_delete_account_core
    from app.domain.projects import ACCOUNT_DELETE_RETENTION_S

    conn, project_root = _init_db(tmp_path, monkeypatch, "retention-ordering.db")
    _insert_user(conn, "u-admin", is_admin=True)
    _insert_user(conn, "u-target")
    _insert_project(conn, project_root, "p-order", "u-target")

    observed: list = []
    real_delete = projects_mod._delete_project_core

    async def _observing_delete(project_id: str, *args, **kwargs):
        probe = sqlite3.connect(db.DB_PATH)
        probe.row_factory = sqlite3.Row
        row = probe.execute(
            "SELECT deleted_at, recycle_bin_retention_s FROM projects WHERE id=?",
            (project_id,),
        ).fetchone()
        observed.append((row["deleted_at"], row["recycle_bin_retention_s"]))
        probe.close()
        return await real_delete(project_id, *args, **kwargs)

    monkeypatch.setattr(projects_mod, "_delete_project_core", _observing_delete)

    set_current_principal(Principal(user_id="u-admin", username="u-admin", is_system_admin=True))
    asyncio.run(admin_soft_delete_account_core("u-target"))

    assert len(observed) == 1, "本用例只有一个项目，应当只观察到一次删除调用"
    deleted_at_at_call, retention_at_call = observed[0]
    assert retention_at_call == ACCOUNT_DELETE_RETENTION_S, (
        "进入 _delete_project_core 时 30 天保留标记就必须已经提交落盘；"
        "否则在这一刻崩溃会让项目退化成 24 小时保留期"
    )
    assert deleted_at_at_call is None, "标记落盘时项目应当还是活跃的（软删尚未发生）"


def test_failed_cascade_does_not_leave_a_stray_retention_tag(tmp_path, monkeypatch):
    """级联删除单个项目失败时，刚打上的 30 天标记必须撤掉。

    否则该项目仍是活跃的、却带着 30 天保留期；用户日后自己把它放进回收站时，
    会拿到 30 天而不是本该的 24 小时——一个用户从没要求过的行为差异。
    """
    import app.domain.projects as projects_mod
    from app.domain.account_deletion import admin_soft_delete_account_core

    conn, project_root = _init_db(tmp_path, monkeypatch, "retention-rollback.db")
    _insert_user(conn, "u-admin", is_admin=True)
    _insert_user(conn, "u-target")
    _insert_project(conn, project_root, "p-fail", "u-target")

    async def _always_fails(project_id: str, *args, **kwargs):
        raise RuntimeError("供应商任务未到终态")

    monkeypatch.setattr(projects_mod, "_delete_project_core", _always_fails)

    set_current_principal(Principal(user_id="u-admin", username="u-admin", is_system_admin=True))
    result = asyncio.run(admin_soft_delete_account_core("u-target"))

    assert result["projects"]["soft_deleted"] == []
    assert [f["project_id"] for f in result["projects"]["failed"]] == ["p-fail"]

    verify_conn = sqlite3.connect(db.DB_PATH)
    verify_conn.row_factory = sqlite3.Row
    row = verify_conn.execute(
        "SELECT deleted_at, recycle_bin_retention_s FROM projects WHERE id='p-fail'"
    ).fetchone()
    verify_conn.close()
    assert row["deleted_at"] is None, "删除失败，项目必须原封不动"
    assert row["recycle_bin_retention_s"] is None, "失败路径必须把刚打上的保留标记撤掉"
