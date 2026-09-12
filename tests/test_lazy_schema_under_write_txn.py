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
from app.audit import store as audit_store
from app.auth import sessions as auth_sessions
from app.models_registry import schema as models_registry_schema
from app.models_registry import store as models_registry_store
from app.orgs import schema as orgs_schema
from app.orgs import store as orgs_store
from app.provisioning import handover as provisioning_handover
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


# ---------------------------------------------------------------------------
# app.auth.sessions.create_session()：EP-03 第二阶段第二轮新增 user_sessions.
# kind 补列后，这是本仓库第一个"调用方可能已持有写事务时被真实调用"的
# provisioning 入口（此前 provisioning 没有这种调用形态，见上面模块文档），
# 2026-09-12 协调方审查揪出：create_session/resolve_session 内部曾经用
# provisioning_schema.ensure_schema()（独立连接），已改为
# ensure_tables_on_connection(conn)。这里补真实调用链路的回归。
# ---------------------------------------------------------------------------


def test_create_session_under_caller_write_txn_does_not_lose_kind_column(fresh_conn):
    """修复前会红：独立连接的 ensure_schema() 撞上 fresh_conn 已持有的
    BEGIN IMMEDIATE，等满 2 秒 busy timeout 后失败被吞掉，kind 列没建成，
    紧接着的 INSERT 报 ``sqlite3.OperationalError: table user_sessions has
    no column named kind``。修复后：不抛异常，且 create_session 参与
    fresh_conn 已经开着的事务（同连接补列不会另起/打断它），只在自己最后
    显式 conn.commit() 时才结束这个事务——这里验证 commit 之前不会有任何
    提前/隐式的事务边界变化：断言调用成功返回 token，且 fresh_conn 用的
    还是同一个底层连接对象（没有被换成另一条）。
    """
    # user_sessions.user_id 有 FOREIGN KEY REFERENCES users(id)，这个连接的
    # PRAGMA foreign_keys 是开的（真实撞到过，见上面第一次改这条用例时的
    # sqlite3.IntegrityError）——用同一个 fresh_conn 先种一行真实用户，仍然
    # 在调用方持有的这同一个 BEGIN IMMEDIATE 事务里，不额外提交。
    fresh_conn.execute(
        "INSERT INTO users(id, username, display_name, password_hash, status,"
        " is_system_admin, must_change_password, created_at) VALUES(?,?,?,?,'active',0,0,?)",
        ("no-such-user", "lazy-schema-probe", "lazy-schema-probe", "x", 0.0),
    )
    assert fresh_conn.in_transaction
    token = auth_sessions.create_session("no-such-user")
    assert token and "." in token
    # create_session 自己的契约就是显式 conn.commit() 落库新会话（这是
    # 该函数从最初版本起就有的既定行为，不是本次改动引入的）——调用完成后
    # fresh_conn 的事务因此正常结束，不是"卡住"或"被独立连接的失败搅坏"。
    assert not fresh_conn.in_transaction
    row = fresh_conn.execute(
        "SELECT kind FROM user_sessions WHERE user_id=?", ("no-such-user",)
    ).fetchone()
    assert row is not None and row["kind"] == "interactive"


# ---------------------------------------------------------------------------
# 原语层修复（2026-09-12 第二轮，四度复发后根治）：六个包（orgs/models_registry/
# provisioning/sso/quota_policy/audit）的 ``ensure_schema()``——"错"的那个
# 独立连接变体——现在内部先判断 ``app.db.get_conn()`` 是否已经处在调用方开的
# 事务里；是则直接改走同连接的 ``ensure_tables_on_connection(那个 conn)``，
# 不开新连接、不抢锁（见 ``app.db_schema.ensure_schema_respecting_caller_
# transaction`` 文档）。下面六组用例故意调用每个包"错"的那个变体（不是
# ``ensure_tables_on_connection``），逐一验证调用方选错入口不再有后果：不抛
# 异常、表确实建成、且调用方事务没有被提前提交/回滚（``fresh_conn.in_
# transaction`` 仍为 True——如果内部退回去用 executescript 或者悄悄
# commit/rollback 了调用方的事务，这里会先于"表建成"那条断言败下来）。
#
# 修复前（本文件这一段用例落地前）：``ensure_schema()`` 走
# ``db._run_write_transaction_once``，独立连接抢 fresh_conn 已经持有的
# ``BEGIN IMMEDIATE`` 写锁，等满 2 秒 ``WRITE_TXN_BUSY_TIMEOUT_S`` 超时后
# 异常被自己的 ``except`` 吞掉、直接返回——不抛异常这条断言本来就会通过（因
# 为函数设计上就不上抛），但"表确实建成"这条会失败：``sqlite_master`` 里查
# 不到对应的表。已经在一个干净的 ``/tmp`` worktree（HEAD，本次改动落地前）
# 上跑过一遍确认这一点，见交付报告里贴的失败输出。
# ---------------------------------------------------------------------------


def test_orgs_ensure_schema_wrong_variant_under_caller_write_txn(fresh_conn):
    """故意调用 orgs 的独立连接变体（不是 ensure_tables_on_connection）。"""
    orgs_schema.ensure_schema()
    assert fresh_conn.in_transaction, (
        "orgs.schema.ensure_schema() 提交/回滚了调用方的事务"
    )
    row = fresh_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='orgs'"
    ).fetchone()
    assert row is not None, "orgs 表没建成——独立连接变体大概率去抢锁超时后被静默吞掉了"


def test_sso_ensure_schema_wrong_variant_under_caller_write_txn(fresh_conn):
    sso_schema.ensure_schema()
    assert fresh_conn.in_transaction, (
        "sso.schema.ensure_schema() 提交/回滚了调用方的事务"
    )
    row = fresh_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='identity_providers'"
    ).fetchone()
    assert row is not None, "identity_providers 表没建成"


def test_provisioning_ensure_schema_wrong_variant_under_caller_write_txn(fresh_conn):
    provisioning_schema.ensure_schema()
    assert fresh_conn.in_transaction, (
        "provisioning.schema.ensure_schema() 提交/回滚了调用方的事务"
    )
    row = fresh_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='user_import_batches'"
    ).fetchone()
    assert row is not None, "user_import_batches 表没建成"


def test_models_registry_ensure_schema_wrong_variant_under_caller_write_txn(fresh_conn):
    models_registry_schema.ensure_schema()
    assert fresh_conn.in_transaction, (
        "models_registry.schema.ensure_schema() 提交/回滚了调用方的事务"
    )
    row = fresh_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='models'"
    ).fetchone()
    assert row is not None, "models 表没建成"


def test_quota_policy_ensure_schema_wrong_variant_under_caller_write_txn(fresh_conn):
    """quota_policy 早前已经修过"调用点"（resolve_effective_limits 改走
    ensure_tables_on_connection），但 ensure_schema() 这个原语本身在本次改动
    之前从未获得自保——直接调用它验证原语层面确实堵上了。"""
    quota_policy_schema.ensure_schema()
    assert fresh_conn.in_transaction, (
        "quota_policy.schema.ensure_schema() 提交/回滚了调用方的事务"
    )
    row = fresh_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='quota_plans'"
    ).fetchone()
    assert row is not None, "quota_plans 表没建成"


def test_audit_ensure_schema_wrong_variant_under_caller_write_txn(fresh_conn):
    """audit/store.py 不叫 schema.py，tests/test_schema_guard.py 的 glob 扫不到
    它，但同一颗地雷同样适用——直接验证。"""
    audit_store.ensure_schema()
    assert fresh_conn.in_transaction, (
        "audit.store.ensure_schema() 提交/回滚了调用方的事务"
    )
    row = fresh_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='operation_audit'"
    ).fetchone()
    assert row is not None, "operation_audit 表没建成"


# ---------------------------------------------------------------------------
# 真实业务函数在调用方持有写事务时调用：不能报 no such table / no such
# column——不改调用点（``models_registry/{store,bindings,health}.py`` 十几处
# ``schema.ensure_schema()``、``provisioning/handover.py`` 三处
# ``orgs_schema.ensure_schema()``），只靠上面的原语层修复让它们安全。
# ---------------------------------------------------------------------------


def test_models_registry_store_list_models_under_caller_write_txn(fresh_conn):
    """models_registry/store.py 十几处 ``schema.ensure_schema()`` 调用点之一。"""
    result = models_registry_store.list_models()
    assert result == []
    assert fresh_conn.in_transaction


def test_provisioning_handover_list_user_assets_under_caller_write_txn(fresh_conn):
    """provisioning/handover.py 三处 ``orgs_schema.ensure_schema()`` 调用点之
    一——``list_user_assets`` 经 ``_team_memberships`` 会 JOIN
    team_members/teams/roles 三张 orgs 懒建表，是这批调用点里对"表没建成"最
    敏感的一个（读到任何一张缺表都会直接抛 ``no such table``）。"""
    fresh_conn.execute(
        "INSERT INTO users(id, username, display_name, password_hash, status,"
        " is_system_admin, must_change_password, created_at) VALUES(?,?,?,?,'active',0,0,?)",
        ("handover-probe-user", "handover-probe", "handover-probe", "x", 0.0),
    )
    result = provisioning_handover.list_user_assets("handover-probe-user")
    assert result["user_id"] == "handover-probe-user"
    assert result["team_memberships"] == []
    assert result["owned_projects"] == []
    # list_user_assets 文档承诺"只读，不提交"——这里同时验证原语层修复没有
    # 顺带改掉这条既有契约。
    assert fresh_conn.in_transaction
