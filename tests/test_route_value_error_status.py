"""路由边界把 ValueError 转成 HTTPException 的回归测试。

CLAUDE.md「错误要转成合适的状态码」：走命令总线的路由会自动把 ValueError 转成
409，不走的路由则裸奔成 500——app/main.py 的全局异常处理器把 http_status 写死
500，领域层写好的中文原因只进日志，用户只看到「系统内部错误」。本文件覆盖四条
已核实会裸奔的路由，验证路由自己在边界把 ValueError 转成 HTTPException(409/404)
并保留原始中文 message：

1. POST .../video-generation-plan/reconcile（P0，业务冲突用 409）
2. POST .../video-generation-plan（P1，create 侧重查不到剧集用 404）
3. POST .../screenplay/preflight（P1，重查不到剧集用 404）
4. GET  .../video-completion/repair-preview（P1，重查不到剧集用 404）

打桩必须打在路由模块实际持有的绑定上，不能打在定义处的模块（CLAUDE.md 拆包后
monkeypatch 静默失效的教训）：
- plan.py 的两个路由在函数体内 `from app.video_plan import ...`，每次调用都从
  ``app.video_plan`` 包命名空间重新取名，patch_video_plan_everywhere 命中；
- completion_contract.py 同理用函数体内 `from app.video_supervisor import ...`，
  patch_video_supervisor_everywhere 命中；
- preflight.py 的路由直接引用同一模块里的全局名字 `_screenplay_generation_preflight`
  （定义即持有该绑定），patch_api_everywhere 递归到 screenplay_ops 子模块时按全限
  定名命中这个模块自己的属性，不是 activation.py 里的原始定义处。

每个用例都用仅测试可见的独特中文错误文案，并用 ``calls`` 列表断言桩确实被路由调
用了一次——这是反向验证：如果打桩没打中真正的绑定，路由会去跑未被替换的真实实现，
``calls`` 仍是空列表，断言在到达状态码检查之前就先失败，不会把“真实现恰好也抛了
同名异常”误判成打桩生效。

``test_reconcile_partial_writes_roll_back_before_409`` 额外覆盖 CLAUDE.md「回滚必
须是异常处理器的第一条语句」：reconcile 路由的整集循环逐镜调用
``reconcile_adopted_revision(conn=conn)`` 写库、最后才统一 ``conn.commit()``；第
2 镜抛 ValueError 时，第 1 镜已写入但未提交的内容必须先回滚，不能留在这条线程/
任务局部、会被后续请求复用的连接上。用同一连接读 + 另开一条独立连接读磁盘上的
文件数据库各验一次，同一连接排掉“看似没回滚、实为读到自己未提交的写入”这类假
阳性，独立连接排掉“同连接内快照错觉”，两者合起来才是可信的“没有落库”证据。
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi import HTTPException

from app import api, db
from tests.conftest import (
    patch_api_everywhere,
    patch_video_plan_everywhere,
    patch_video_supervisor_everywhere,
)


def _conn_with_shot() -> sqlite3.Connection:
    """最小可用连接：一个 project/episode/shot，供 reconcile 路由的直读 SQL 使用。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for statement in db.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p1','P',0)")
    conn.execute(
        """INSERT INTO episodes(
               id,project_id,episode_no,storyboard_artifact_id,target_video_model,created_at
           ) VALUES('e1','p1',1,'storyboard_rev_1','provider',0)"""
    )
    conn.execute(
        """INSERT INTO shots(
               id,shot_uid,episode_id,shot_no,duration_s,shot_size,camera_move,
               scene_setting,characters,action_desc,dialogues,transition,
               shot_contract_json,adopted_version_id
           ) VALUES('s1','uid-1','e1',1,5,'中景','固定','空间','[]','动作','[]',
                    '硬切','{}','v1')"""
    )
    conn.commit()
    return conn


def test_reconcile_video_plan_conflict_becomes_409(monkeypatch) -> None:
    """P0：reconcile_adopted_revision 的 7 处业务冲突 ValueError 要转 409。"""
    conn = _conn_with_shot()
    patch_api_everywhere(monkeypatch, "_episode_or_404", lambda _episode_id: {"id": "e1"})
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    calls: list[tuple] = []

    def _boom(*args, **kwargs):
        calls.append((args, kwargs))
        raise ValueError("测试_采用版本冲突_reconcile")

    patch_video_plan_everywhere(monkeypatch, "reconcile_adopted_revision", _boom)

    with pytest.raises(HTTPException) as exc:
        api.reconcile_episode_video_generation_plan("e1", {"shot_id": "s1"})

    assert len(calls) == 1
    assert exc.value.status_code == 409
    assert "测试_采用版本冲突_reconcile" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_create_video_plan_missing_episode_race_becomes_404(monkeypatch) -> None:
    """P1：generate_episode_plan 内部重查不到剧集时的 ValueError 要转 404。"""
    patch_api_everywhere(monkeypatch, "_episode_or_404", lambda _episode_id: {"id": "e1"})
    patch_api_everywhere(
        monkeypatch, "_assert_storyboard_generation_gate", lambda _episode_id: None,
    )
    # except 块第一条语句要读 get_conn() 判断是否需要回滚，即使这条路径本身无写入。
    conn = sqlite3.connect(":memory:")
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    calls: list[tuple] = []

    async def _boom(*args, **kwargs):
        calls.append((args, kwargs))
        raise ValueError("测试_分集不存在_create")

    patch_video_plan_everywhere(monkeypatch, "generate_episode_plan", _boom)

    with pytest.raises(HTTPException) as exc:
        await api.create_episode_video_generation_plan("e1", {})

    assert len(calls) == 1
    assert exc.value.status_code == 404
    assert "测试_分集不存在_create" in str(exc.value.detail)
    assert conn.in_transaction is False


def test_screenplay_preflight_missing_episode_race_becomes_404(monkeypatch) -> None:
    """P1：_screenplay_blueprint_budget_projection 重查不到剧集时的 ValueError 要转 404。"""
    # except 块第一条语句要读 get_conn() 判断是否需要回滚，即使这条路径本身无写入。
    conn = sqlite3.connect(":memory:")
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    calls: list[tuple] = []

    def _boom(*args, **kwargs):
        calls.append((args, kwargs))
        raise ValueError("测试_剧集不存在_preflight")

    patch_api_everywhere(monkeypatch, "_screenplay_generation_preflight", _boom)

    with pytest.raises(HTTPException) as exc:
        api.screenplay_generation_preflight("e1")

    assert len(calls) == 1
    assert exc.value.status_code == 404
    assert "测试_剧集不存在_preflight" in str(exc.value.detail)
    assert conn.in_transaction is False


def test_video_completion_repair_preview_missing_episode_race_becomes_404(
    monkeypatch,
) -> None:
    """P1：preview_video_completion_repair 重查不到剧集时的 ValueError 要转 404。"""
    patch_api_everywhere(monkeypatch, "_episode_or_404", lambda _episode_id: {"id": "e1"})
    # except 块第一条语句要读 get_conn() 判断是否需要回滚，即使这条路径本身无写入。
    conn = sqlite3.connect(":memory:")
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    calls: list[tuple] = []

    def _boom(*args, **kwargs):
        calls.append((args, kwargs))
        raise ValueError("测试_剧集不存在_repair_preview")

    patch_video_supervisor_everywhere(monkeypatch, "preview_video_completion_repair", _boom)

    with pytest.raises(HTTPException) as exc:
        api.preview_video_completion_repair_route("e1")

    assert len(calls) == 1
    assert exc.value.status_code == 404
    assert "测试_剧集不存在_repair_preview" in str(exc.value.detail)
    assert conn.in_transaction is False


def test_reconcile_partial_writes_roll_back_before_409(monkeypatch, tmp_path) -> None:
    """P0 补充：整集循环第 1 镜已写入、第 2 镜才抛 ValueError 时必须先回滚。

    连接指向磁盘上的文件（不是 ``:memory:``），这样才能在结尾另开一条独立连接
    直接读盘验证——同连接读自己未提交的写入会“看得见”，不能证明没有落库。
    """
    db_path = tmp_path / "reconcile_rollback.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for statement in db.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p1','P',0)")
    conn.execute(
        """INSERT INTO episodes(
               id,project_id,episode_no,storyboard_artifact_id,target_video_model,created_at
           ) VALUES('e1','p1',1,'storyboard_rev_1','provider',0)"""
    )
    for shot_id, shot_no in (("s1", 1), ("s2", 2)):
        conn.execute(
            """INSERT INTO shots(
                   id,shot_uid,episode_id,shot_no,duration_s,shot_size,camera_move,
                   scene_setting,characters,action_desc,dialogues,transition,
                   shot_contract_json,adopted_version_id
               ) VALUES(?,?,?,?,5,'中景','固定','空间','[]','动作','[]',
                        '硬切','{}','v1')""",
            (shot_id, f"uid-{shot_no}", "e1", shot_no),
        )
    conn.commit()
    patch_api_everywhere(monkeypatch, "_episode_or_404", lambda _episode_id: {"id": "e1"})
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    calls: list[str] = []

    def _stub(shot_id, _adopted_version_id, *, conn):
        calls.append(shot_id)
        if shot_id == "s1":
            # 模拟第 1 镜真实成功写库，但整集循环要到最后才统一 commit。
            conn.execute(
                "UPDATE shots SET adopted_version_id='ROLLBACK_PROBE' WHERE id='s1'"
            )
            return {"bound": 1, "stale_shot_ids": []}
        raise ValueError("测试_采用版本冲突_reconcile_第2镜")

    patch_video_plan_everywhere(monkeypatch, "reconcile_adopted_revision", _stub)

    with pytest.raises(HTTPException) as exc:
        api.reconcile_episode_video_generation_plan("e1", {})

    assert calls == ["s1", "s2"]
    assert exc.value.status_code == 409
    assert conn.in_transaction is False
    same_conn_row = conn.execute(
        "SELECT adopted_version_id FROM shots WHERE id='s1'"
    ).fetchone()
    assert same_conn_row["adopted_version_id"] == "v1"
    independent = sqlite3.connect(str(db_path))
    independent.row_factory = sqlite3.Row
    try:
        disk_row = independent.execute(
            "SELECT adopted_version_id FROM shots WHERE id='s1'"
        ).fetchone()
        assert disk_row["adopted_version_id"] == "v1"
    finally:
        independent.close()
