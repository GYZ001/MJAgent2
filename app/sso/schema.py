"""EP-02 企业身份接入：``identity_providers`` / ``user_identities`` /
``sso_auth_requests`` / ``sso_break_glass_codes`` / ``sso_login_exchanges``
五张表，lazy 建表（L2）。

``app/db.py`` 的 line_count 基线已被 EP-01/EP-05 的多张新表用满（见该文件顶部
注释），本包与 ``app/orgs/schema.py``/``app/models_registry/schema.py`` 同一
手法：``ensure_schema()`` 按当前 ``db.DB_PATH`` 幂等记忆；``app/main.py`` 的
lifespan 在 ``init_db()`` 之后显式调一次；``app/sso/store.py`` 的每个读写
函数也各自兜底调用一次（多数测试不经 lifespan，必须靠这条兜底）。

与 PRD EP-02 §3 的两处差异（均记入交付报告"与 PRD 不符的现实"）：

1. ``identity_providers.client_secret_enc`` 单列在 PRD 里是一列，这里拆成
   ``client_secret_ciphertext``/``client_secret_nonce`` 两列——AES-256-GCM
   （``app.models_registry.crypto``）的输出天然是 ``(nonce, ciphertext)``
   一对，与 ``model_credentials`` 表的 ``key_nonce``/``key_ciphertext`` 同一
   存储形状，不是新造的约定。
2. 新增 ``sso_break_glass_codes`` 表：PRD §6 要求"超级管理员 + 一次性恢复码"
   通道，但 §3 的数据模型没有为它单独建表；不新建就无法安全落地"一次性"
   （必须有地方记 code 的哈希、是否已用、何时过期）。
3. 新增 ``sso_login_exchanges`` 表（2026-09-12 补：会话交接不能走查询串）：
   OIDC callback 是浏览器整页 302 跳转，最初实现把签发好的真实会话令牌直接
   拼进跳转 URL 的查询串——这枚令牌因此会明文落进 nginx access log（默认
   ``combined`` 格式记录完整 ``$request``，随 logrotate 长期保留）、浏览器
   历史，以及页面若加载任何外部资源时可能带出的 Referer，三处同时暴露同一把
   凭证。修复：跳转 URL 只带一枚一次性交换码（60 秒 TTL，只存
   ``sha256(code)``，单次消费），真正的会话令牌只在
   ``POST /api/auth/sso/exchange`` 的**响应体**里返回——响应体不进访问日志、
   不进浏览器历史、不进 Referer。

``sso_auth_requests`` 比 PRD §3 多一列 ``link_user_id``（可空）：已登录用户
发起"绑定 IdP"（``POST /api/auth/sso/link``）与匿名用户发起登录
（``GET .../start``）复用同一张表和同一条 ``callback`` 路由，靠这一列
区分 callback 时是"JIT 开户/登录"还是"绑定到当前已登录账号"——PRD §4 的五条
路由本身没有为这两种模式规定不同的表结构，这是把它们收敛到一条状态机上的
最小实现，不是新增一等公民概念。
``ensure_tables_on_connection()`` 是 ``ensure_schema()`` 之外单独留的第二个
入口，专供 ``app.sso.store`` 里每个"接受调用方 conn"的函数调用——那些函数
可能被别的模块在调用方已持有的 ``BEGIN IMMEDIATE`` 事务里直接传 ``conn``
调用（与 ``app/orgs/schema.py``/``app/quota_policy/schema.py`` 同一手法、同
一故障模式：``ensure_schema()`` 独立开连接抢 ``BEGIN IMMEDIATE`` 会跟调用方
自己持有的写锁抢锁，2 秒 ``WRITE_TXN_BUSY_TIMEOUT_S`` 超时后静默建表失败）。
``ensure_tables_on_connection()`` 复用调用方传入的同一个 ``conn`` 执行纯
``CREATE TABLE/INDEX IF NOT EXISTS``（不开新连接、不申请新锁）；本包没有种
子数据，两个入口除了连接归属没有其它差异。仍自成一体独立开连接的四个函数
（``purge_expired_auth_requests``/``consume_break_glass_code``/
``consume_login_exchange``/``purge_expired_login_exchanges``，见各自
docstring）不受影响，继续用 ``ensure_schema()``。
"""
from __future__ import annotations

import sqlite3

from app import db, monitor_audit_buffer

_CREATE_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS identity_providers (
        id TEXT PRIMARY KEY,
        org_id TEXT,
        kind TEXT NOT NULL,
        name TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 0,
        issuer TEXT,
        client_id TEXT NOT NULL DEFAULT '',
        client_secret_ciphertext BLOB,
        client_secret_nonce BLOB,
        discovery_url TEXT,
        authorize_url TEXT,
        token_url TEXT,
        userinfo_url TEXT,
        jwks_url TEXT,
        scopes TEXT NOT NULL DEFAULT '',
        claim_map_json TEXT NOT NULL DEFAULT '{}',
        provision_json TEXT NOT NULL DEFAULT '{}',
        allowed_domains TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        created_by TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_identity_providers_org ON identity_providers(org_id)",
    """CREATE TABLE IF NOT EXISTS user_identities (
        user_id TEXT NOT NULL,
        idp_id TEXT NOT NULL,
        external_subject TEXT NOT NULL,
        linked_at REAL NOT NULL,
        last_login_at REAL,
        PRIMARY KEY(idp_id, external_subject)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_user_identities_user ON user_identities(user_id)",
    """CREATE TABLE IF NOT EXISTS sso_auth_requests (
        state TEXT PRIMARY KEY,
        idp_id TEXT NOT NULL,
        nonce TEXT NOT NULL,
        code_verifier TEXT NOT NULL,
        redirect_to TEXT,
        link_user_id TEXT,
        created_at REAL NOT NULL,
        consumed_at REAL,
        ip TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_sso_auth_requests_created ON sso_auth_requests(created_at)",
    """CREATE TABLE IF NOT EXISTS sso_break_glass_codes (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        code_hash TEXT NOT NULL,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        used_at REAL,
        created_by TEXT
    )""",
    # 一次性会话交接：只存 code 的哈希，从不落 code 明文；60 秒 TTL、单次消费。
    """CREATE TABLE IF NOT EXISTS sso_login_exchanges (
        code_hash TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        consumed_at REAL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_sso_login_exchanges_expires ON sso_login_exchanges(expires_at)",
)

_ensured_paths: set[str] = set()


def ensure_tables_on_connection(conn: sqlite3.Connection) -> None:
    """轻量、同连接、无副作用的建表兜底——不开新连接、不申请新锁，安全用于
    调用方已持有事务的场景。见模块文档"两个入口"一段。

    逐条 ``execute``，绝不能用 ``executescript``：后者在执行前会对当前连接
    做一次隐式 COMMIT（CPython sqlite3 文档行为）。这个函数唯一的存在理由
    就是跑在调用方传入的、可能正处于 ``BEGIN IMMEDIATE`` 里的连接上——一旦
    内部用 executescript，会把调用方尚未提交的事务连同它持有的写锁一起偷
    偷放掉，是 CLAUDE.md「不得在调用方的连接上隐式提交」记录的那三次真实
    事故同一类地雷（``tests/test_schema_guard.py`` 有 AST 守卫钉死这一点）。
    """
    for statement in _CREATE_STATEMENTS:
        conn.execute(statement)


def ensure_schema() -> None:
    """幂等建表；按当前 ``db.DB_PATH`` 记忆已建，避免每次调用都重跑 DDL。独立
    连接（``db._run_write_transaction_once``），只供不在调用方事务里嵌套的
    入口使用（``app.main`` 启动时）——``app.sso.store`` 的每个读写函数改用
    ``ensure_tables_on_connection`` 见该函数文档。

    这条独立连接跑在自己的事务里，没有调用方的事务可毁，技术上即使用
    ``executescript`` 也不会重演 ``ensure_tables_on_connection`` 那种隐式
    COMMIT 事故；但这里仍然复用 ``ensure_tables_on_connection(conn)`` 而不是
    另起一份 ``executescript`` DDL——两处各维护一份建表语句，日后改表结构
    只改了一边，就会制造"独立连接建的表"与"调用方连接建的表"逐渐不一致的
    新隐患，安全性不需要靠两份代码互相印证。"""
    key = str(db.DB_PATH)
    if key in _ensured_paths:
        return

    def operation(conn: sqlite3.Connection) -> None:
        ensure_tables_on_connection(conn)

    try:
        db._run_write_transaction_once(operation)
    except Exception as exc:  # noqa: BLE001 建表失败留到下一次调用重试，不阻塞
        # 调用方；不能悄悄吞掉——落一条可观测记录，见 app/orgs/schema.py 同名
        # except 分支的注释，理由完全一致。
        monitor_audit_buffer.note_schema_ensure_failure(__name__, exc)
        return
    _ensured_paths.add(key)
