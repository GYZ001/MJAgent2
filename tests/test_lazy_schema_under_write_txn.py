"""复现与证伪：调用方已持有 ``BEGIN IMMEDIATE`` 时，lazy 建表不能静默失败。

背景（2026-09-12 系统性隐患修复，见 app/orgs/schema.py 等五个包模块文档
「两个入口」一段）：这几轮企业化改造新增的五个包（``app.orgs``/
``app.models_registry``/``app.provisioning``/``app.sso``/``app.quota_policy``）
都用同一套 lazy 建表模式抄自 ``app/audit/store.py``——``ensure_schema()`` 走
``db._run_write_transaction_once``（独立连接 + ``BEGIN IMMEDIATE``），异常被
吞掉留到下次调用重试。当某个 store 函数是在调用方**已经持有**的写事务连接
上被调用时，这条独立连接会去抢同一把写锁，等满 2 秒
``WRITE_TXN_BUSY_TIMEOUT_S`` 超时、异常被吞掉、``_ensured_paths`` 不置位、
函数静默返回——表没建成，紧接着的查询报 ``no such table``。EP-04 代理在
``app.quota_policy`` 上真实踩到过这颗雷（``quota_allocations`` 报
``no such table``），修法是让"接受调用方 conn"的函数改用同连接的
``ensure_tables_on_connection(conn)``；本次任务把同一个修法补给另外四个包。

本文件对 orgs/sso 是用真实的 store 读函数复现——``修复前``（即本次改动落地
前）跑这两条会红（等满 busy timeout 后报 ``no such table``），修复后应该
立刻返回、不抛异常；已经在一个干净的 ``/tmp`` worktree（HEAD，本次改动落地
前）上跑过一遍确认过这一点，见交付报告里贴的失败输出。

provisioning/models_registry 目前没有一个会被外部持有事务"嵌套调用"的公开
入口——它们的自开连接函数都在触碰连接前的第一条语句就调用
``ensure_schema()``，此时连接还没做过任何写、不持有锁，独立连接版建表不会
跟自己抢锁（这也是保留它们继续用 ``ensure_schema()`` 而不改动调用点的理由，
见两个包 schema.py 模块文档）。所以这两个包没有像 orgs/sso 那样"真实调用链
路"上可复现的红，但底层同一颗地雷对新入口本身同样适用，这里直接验证
``ensure_tables_on_connection()`` 在调用方已持锁时正确工作（不抛异常、不用
等锁）——修复前这两个包根本没有这个函数，调用会直接 ``AttributeError``，
同样是"红"，只是失败形态不同（缺函数 vs. no such table）。

quota_policy 是这批改造里最先踩雷、也最先修好的包（EP-04 代理），这里当
回归基线：应该在本次改动前后都保持绿。
"""
from __future__ import annotations

import pytest

from app import db
from app.models_registry import schema as models_registry_schema
from app.orgs import schema as orgs_schema
from app.orgs import store as orgs_store
from app.provisioning import schema as provisioning_schema
from app.quota_policy import allocation as quota_policy_allocation
from app.quota_policy import schema as quota_policy_schema
from app.sso import schema as sso_schema
from app.sso import store as sso_store


@pytest.fixture
def fresh_conn(tmp_path, monkeypatch):
    """全新、独占的 SQLite 文件：只建核心表（``db.init_db()``），不预先建
    任何 lazy 表。每个测试的 ``tmp_path`` 都是唯一的，五个包各自 schema 模块
    的 ``_ensured_paths``（进程内、按 ``str(db.DB_PATH)`` 记忆）不可能已经
    命中这个新路径——保证第一次调用 store 函数时真的会触发
    ``ensure_schema()``/``ensure_tables_on_connection()``，而不是被测试模板
    clone 带来的、已经建好的表掩盖了这次复现（模板库里 orgs 的表已经在
    ``tests/conftest.py::_initialize_database_template`` 里显式建好+种子过）。

    fixture 退出时把 ``conn`` 归还（回滚未提交的 ``BEGIN IMMEDIATE``、关闭、
    清空线程局部绑定），不影响同一 worker 里其它测试对 ``db._local.conn``/
    ``db.DB_PATH`` 的假设——与 ``tests/test_completion_grant_atomicity.py::
    grant_db`` 同一惯例。
    """
    existing = getattr(db._local, "conn", None)
    if existing is not None:
        if existing.in_transaction:
            existing.rollback()
        existing.close()
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "lazy-schema-under-write-txn.db")
    db._local.conn = None
    db.init_db()
    conn = db.get_conn()
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()
        db._local.conn = None


def test_orgs_store_read_under_caller_write_txn(fresh_conn):
    """调用方已经 BEGIN IMMEDIATE 之后，orgs_store 的读函数不应该报
    no such table，也不应该抛任何异常——它内部只需要在同一个 conn 上建表。"""
    result = orgs_store.get_org(fresh_conn, "no-such-org")
    assert result is None


def test_sso_store_read_under_caller_write_txn(fresh_conn):
    """同上一条，换 sso_store。"""
    result = sso_store.get_idp(fresh_conn, "no-such-idp")
    assert result is None


def test_quota_policy_allocation_read_under_caller_write_txn(fresh_conn):
    """回归基线：quota_policy 已经用 ensure_tables_on_connection 修过（EP-04），
    这里确认它在本次改动前后都保持绿，不会被连带改坏。"""
    result = quota_policy_allocation.scope_limits_dict(fresh_conn, "org", "no-such-org")
    assert result == {}


def test_provisioning_schema_same_connection_entry_point_under_caller_write_txn(fresh_conn):
    """provisioning 没有可复现的「真实调用链路」红（见模块文档），直接验证
    新入口本身：在调用方已经 BEGIN IMMEDIATE 之后，同连接建表不应该抛异常、
    不应该卡住等锁。"""
    provisioning_schema.ensure_tables_on_connection(fresh_conn)
    row = fresh_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='user_import_batches'"
    ).fetchone()
    assert row is not None


def test_models_registry_schema_same_connection_entry_point_under_caller_write_txn(fresh_conn):
    """同上一条，换 models_registry。"""
    models_registry_schema.ensure_tables_on_connection(fresh_conn)
    row = fresh_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='models'"
    ).fetchone()
    assert row is not None


# ---------------------------------------------------------------------------
# 隐式 COMMIT 回归：conn.executescript() 在执行前会对当前连接做一次隐式 COMMIT
# （CPython sqlite3 文档行为）。ensure_tables_on_connection() 存在的全部意义
# 就是跑在调用方给的、可能正处于 BEGIN IMMEDIATE 里的连接上，一旦内部用了
# executescript 就会把调用方的事务偷偷提交掉——与 CLAUDE.md 记录的「不得在
# 调用方的连接上隐式提交」三次真实事故同一类地雷，且已经在
# app/quota_policy/schema.py 之外的配额并发闸门链路上真实炸过一次
# （tests/test_quota_concurrency_atomicity.py 约 20% 间歇复现：orgs.schema 的
# ensure_tables_on_connection 隐式提交了调用方刚拿到的 BEGIN IMMEDIATE 独占
# 写锁）。这里直接钉住"调用前后 conn.in_transaction 必须保持 True"这条最小
# 判据，不经过任何业务 store 函数，任何一个包只要内部换回 executescript 就必
# 须立刻红。quota_policy 是唯一从一开始就写对（逐条 execute）的包，纳入同一
# 组用例当反例基线：它必须在任何时候都保持绿。
# ---------------------------------------------------------------------------


def test_orgs_schema_does_not_implicitly_commit_callers_transaction(fresh_conn):
    assert fresh_conn.in_transaction
    orgs_schema.ensure_tables_on_connection(fresh_conn)
    assert fresh_conn.in_transaction, (
        "orgs.schema.ensure_tables_on_connection 隐式提交了调用方的事务"
        "（很可能内部还在用 executescript）"
    )


def test_sso_schema_does_not_implicitly_commit_callers_transaction(fresh_conn):
    assert fresh_conn.in_transaction
    sso_schema.ensure_tables_on_connection(fresh_conn)
    assert fresh_conn.in_transaction, (
        "sso.schema.ensure_tables_on_connection 隐式提交了调用方的事务"
        "（很可能内部还在用 executescript）"
    )


def test_provisioning_schema_does_not_implicitly_commit_callers_transaction(fresh_conn):
    assert fresh_conn.in_transaction
    provisioning_schema.ensure_tables_on_connection(fresh_conn)
    assert fresh_conn.in_transaction, (
        "provisioning.schema.ensure_tables_on_connection 隐式提交了调用方的事务"
        "（很可能内部还在用 executescript）"
    )


def test_models_registry_schema_does_not_implicitly_commit_callers_transaction(fresh_conn):
    assert fresh_conn.in_transaction
    models_registry_schema.ensure_tables_on_connection(fresh_conn)
    assert fresh_conn.in_transaction, (
        "models_registry.schema.ensure_tables_on_connection 隐式提交了调用方的事务"
        "（很可能内部还在用 executescript）"
    )


def test_quota_policy_schema_keeps_being_a_negative_control(fresh_conn):
    """反例基线：quota_policy 从一开始就逐条 execute，这条必须在任何时候都绿。"""
    assert fresh_conn.in_transaction
    quota_policy_schema.ensure_tables_on_connection(fresh_conn)
    assert fresh_conn.in_transaction
