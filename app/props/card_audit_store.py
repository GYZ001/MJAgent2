"""道具卡「按现行规则复核」（``app.props.card_audit``）的表结构与读写原语——
懒建表，照 ``app/props/card_pending_store.py`` 的写法（同一类"按
(project_id, 判据对象) 做跨调用 CAS 抢占"需求，连同它的前提一起照抄：独立
连接、不隐式提交调用方事务、建表失败不得静默吞掉调用方事务）。

不进 ``app/db.py``：该文件扇入 >100，CLAUDE.md「扇入 >100 的模块不得再加
职责」。``ensure_schema()``/``ensure_tables_on_connection()`` 双入口分工、
DDL 逐条 ``conn.execute()``（禁 ``executescript``，它执行前的隐式 COMMIT 会
把调用方尚未提交的事务一起偷偷提交掉）照抄既有先例，不重复整套论证。

本模块只做持久化原语，不做业务判断（该不该复核、判据怎么算）——那些留在
同包 ``card_audit_rules.py``/``card_audit.py``。``conn`` 全部由调用方传入、
本模块任何函数都不自行 ``commit()``（CLAUDE.md「不得在调用方的连接上隐式
提交」）。

按 ``(project_id, prop_name)`` 维度抢占，一张卡只保留最近一轮复核记录（与
``card_pending_store`` 同一粒度，不做历史版本累积——复核是"按当前规则版本
是否已经核过"的幂等状态，不是审计日志）。``rules_version`` 记这一行对应的
是哪个规则版本的复核结果；规则版本前进后，旧行的 ``rules_version`` 落后于
``app.props.judge.PROP_CARD_RULES_VERSION`` 就该被重新抢占复核，见
``card_audit.py`` 的 claim 逻辑。``attempts`` 记本轮（当前 ``rules_version``）
的复核尝试次数，供「失败重试有限次」额度判据使用。

``claim_token``（审查发现，2026-10-03 新增）：每次 ``insert_running``/
``update_running`` 抢占都生成一个新的随机 token 写入这一列，代表"这一轮
抢占"的身份。``update_ready``/``update_failed`` 必须带上抢占时拿到的
``claim_token`` 且要求 ``WHERE id=? AND claim_token=?``——如果这张卡在
它处理期间被判定为僵死/版本落后而被另一次调用重新抢占（``claim_token``
已改写成新值），这次写回会因为 ``WHERE`` 条件不匹配而影响 0 行，调用方据此
判定"本次结果已过期、被后来者抢占，不采信"，不会用旧一轮算出来的结果覆盖
新一轮，也不会把这行误标成"已按现行版本复核过"。没有 ``claim_token`` 这层
围栏，单纯按 ``rules_version``/``attempts`` 做乐观锁会在"版本号恰好重复"
或"同版本内被重新抢占两次"时失效（两种生成路径都可能产出相同的
``attempts`` 数值）。

``doubts_json``（2026-10-03-v2 新增）：两次独立判定不一致、或归属没有自己的
卡、或模型自述拿不准时产生的「待人工确认」记录（见 ``app.props.card_audit_
consensus``），结构是列表，每条形如 ``{"kind", "index"/"alias", "text",
"doubt_type", "reason_a", "reason_b", ...}``。

``prop_card_audit_doubt_decisions`` 表（同一新增）：人工对某条存疑的决定
（"deleted"/"kept"），按 ``(project_id, prop_name, rules_version, doubt_key)``
去重——``doubt_key`` 是 ``app.props.card_audit_consensus.doubt_key`` 算出的
稳定定位键（按子句/别名**原文**而不是下标，因为下标会在前面的子句被删除后
错位）。人工点「保留」后，同一规则版本内的后续复核不再重复呈现这条存疑
（见 ``filter_doubts_against_kept_decisions``）；规则版本前进后决定不再生效
——新规则下这条外观信息是否该删是一个新问题，需要重新判定。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from app import db, db_schema

_CREATE_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS prop_card_rule_audits (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        prop_name TEXT NOT NULL,
        rules_version TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        attempts INTEGER NOT NULL DEFAULT 0,
        claim_token TEXT,
        old_appearance TEXT,
        new_appearance TEXT,
        removed_clauses_json TEXT,
        removed_aliases_json TEXT,
        reimaged INTEGER NOT NULL DEFAULT 0,
        feature_shortfall INTEGER NOT NULL DEFAULT 0,
        doubts_json TEXT,
        error TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE(project_id, prop_name)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_prop_card_rule_audits_proj "
    "ON prop_card_rule_audits(project_id, prop_name, status)",
    """CREATE TABLE IF NOT EXISTS prop_card_audit_doubt_decisions (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        prop_name TEXT NOT NULL,
        rules_version TEXT NOT NULL,
        doubt_key TEXT NOT NULL,
        decision TEXT NOT NULL,
        decided_at REAL NOT NULL,
        UNIQUE(project_id, prop_name, rules_version, doubt_key)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_prop_card_audit_doubt_decisions_lookup "
    "ON prop_card_audit_doubt_decisions(project_id, prop_name, rules_version)",
)


def new_claim_token() -> str:
    """生成一枚新的抢占围栏 token（见模块 docstring），每次 (re)claim 调用一次。"""
    return uuid.uuid4().hex

_ensured_paths: set[str] = set()


def ensure_tables_on_connection(conn: sqlite3.Connection) -> None:
    """轻量、同连接、无副作用的建表兜底，安全用于调用方已持有事务的场景。"""
    for statement in _CREATE_STATEMENTS:
        conn.execute(statement)


def ensure_schema() -> None:
    """幂等建表；按当前 ``db.DB_PATH`` 记忆已建，避免每次调用都重跑 DDL。"""
    key = str(db.DB_PATH)
    if key in _ensured_paths:
        return

    def _run_independent() -> None:
        def operation(conn: sqlite3.Connection) -> None:
            ensure_tables_on_connection(conn)

        try:
            db._run_write_transaction_once(operation)
        except Exception:  # noqa: BLE001 建表失败留到下一次调用重试，不阻塞调用方
            return
        _ensured_paths.add(key)

    db_schema.ensure_schema_respecting_caller_transaction(
        db.get_conn(),
        on_caller_connection=ensure_tables_on_connection,
        run_independent=_run_independent,
    )


def get_audit(conn: Any, *, project_id: str, prop_name: str) -> dict[str, Any] | None:
    ensure_schema()
    row = conn.execute(
        "SELECT * FROM prop_card_rule_audits WHERE project_id=? AND prop_name=?",
        (project_id, prop_name),
    ).fetchone()
    return dict(row) if row else None


def list_audits(conn: Any, *, project_id: str) -> list[dict[str, Any]]:
    ensure_schema()
    rows = conn.execute(
        "SELECT * FROM prop_card_rule_audits WHERE project_id=? ORDER BY prop_name",
        (project_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def insert_running(
    conn: Any, *, row_id: str, project_id: str, prop_name: str, rules_version: str, stamp: float,
    claim_token: str | None = None,
) -> str:
    """调用方必须先在同一个 ``conn`` 上调用过 ``ensure_tables_on_connection``。
    返回本轮抢占的 ``claim_token``（未显式传入时自动生成），调用方必须记下它、
    供之后的 ``update_ready``/``update_failed`` 用作围栏校验。"""
    token = claim_token or new_claim_token()
    conn.execute(
        """INSERT INTO prop_card_rule_audits(
               id, project_id, prop_name, rules_version, status, attempts,
               claim_token, created_at, updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (row_id, project_id, prop_name, rules_version, "running", 1, token, stamp, stamp),
    )
    return token


def update_running(
    conn: Any, *, row_id: str, rules_version: str, attempts: int, stamp: float,
    claim_token: str | None = None,
) -> str:
    """同 ``insert_running`` 的建表前提；重新抢占时 ``rules_version`` 可能从
    上一轮的旧版本前进到当前版本（见 ``card_audit.py`` 的 claim 逻辑）。每次
    调用都换发一个新的 ``claim_token``（未显式传入时自动生成）并返回，使前一
    轮调用者持有的旧 token 失效——这正是围栏机制本身（见模块 docstring）。"""
    token = claim_token or new_claim_token()
    conn.execute(
        "UPDATE prop_card_rule_audits SET rules_version=?, status='running', "
        "attempts=?, claim_token=?, error=NULL, updated_at=? WHERE id=?",
        (rules_version, attempts, token, stamp, row_id),
    )
    return token


def update_ready(
    conn: Any, *, row_id: str, old_appearance: str, new_appearance: str,
    removed_clauses: list[dict], removed_aliases: list[dict],
    reimaged: bool, feature_shortfall: bool, stamp: float,
    claim_token: str | None = None, doubts: list[dict] | None = None,
) -> bool:
    """同 ``insert_running`` 的建表前提。``claim_token`` 非 None 时作为围栏
    条件（``WHERE id=? AND claim_token=?``）——返回 False 表示这一行已被
    另一轮抢占改写过 token，本次结果已过期，调用方不应当把它当成"已采信"
    （见模块 docstring）；``claim_token`` 为 None 时（测试/脚本直接摆数据的
    既有写法）退化为不校验围栏的旧行为，始终返回 True。``doubts`` 缺省为
    空列表（历史调用点不传时不丢数据，只是没有存疑可写）。"""
    doubts_json = json.dumps(doubts or [], ensure_ascii=False)
    if claim_token is None:
        conn.execute(
            "UPDATE prop_card_rule_audits SET status='ready', old_appearance=?, "
            "new_appearance=?, removed_clauses_json=?, removed_aliases_json=?, "
            "reimaged=?, feature_shortfall=?, doubts_json=?, error=NULL, updated_at=? WHERE id=?",
            (
                old_appearance, new_appearance,
                json.dumps(removed_clauses, ensure_ascii=False),
                json.dumps(removed_aliases, ensure_ascii=False),
                1 if reimaged else 0, 1 if feature_shortfall else 0, doubts_json, stamp, row_id,
            ),
        )
        return True
    cursor = conn.execute(
        "UPDATE prop_card_rule_audits SET status='ready', old_appearance=?, "
        "new_appearance=?, removed_clauses_json=?, removed_aliases_json=?, "
        "reimaged=?, feature_shortfall=?, doubts_json=?, error=NULL, updated_at=? WHERE id=? AND claim_token=?",
        (
            old_appearance, new_appearance,
            json.dumps(removed_clauses, ensure_ascii=False),
            json.dumps(removed_aliases, ensure_ascii=False),
            1 if reimaged else 0, 1 if feature_shortfall else 0, doubts_json, stamp, row_id, claim_token,
        ),
    )
    return cursor.rowcount > 0


def update_doubts(conn: Any, *, row_id: str, doubts: list[dict]) -> None:
    """单独更新某一行的 ``doubts_json``——人工确认删除/保留一条存疑之后，
    不需要重跑整张卡的复核，只需把这条存疑从剩余列表里摘掉（见
    ``app.domain.bible_ops.props_api`` 的确认/保留端点）。"""
    conn.execute(
        "UPDATE prop_card_rule_audits SET doubts_json=? WHERE id=?",
        (json.dumps(doubts, ensure_ascii=False), row_id),
    )


def record_doubt_decision(
    conn: Any, *, project_id: str, prop_name: str, rules_version: str,
    doubt_key: str, decision: str, stamp: float,
) -> None:
    """持久化人工对一条存疑的决定（"deleted"/"kept"）。``INSERT OR REPLACE``：
    同一 ``(project_id, prop_name, rules_version, doubt_key)`` 再次决定时覆盖
    旧决定，不累积历史——这是"当前这一规则版本下怎么处理"的状态，不是审计
    日志。调用方必须先在同一个 ``conn`` 上调用过 ``ensure_tables_on_
    connection``（与 ``update_running``/``update_ready`` 同一约定）。"""
    conn.execute(
        "INSERT OR REPLACE INTO prop_card_audit_doubt_decisions("
        "id, project_id, prop_name, rules_version, doubt_key, decision, decided_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (
            f"{project_id}:{prop_name}:{rules_version}:{doubt_key}",
            project_id, prop_name, rules_version, doubt_key, decision, stamp,
        ),
    )


def get_kept_doubt_keys(conn: Any, *, project_id: str, prop_name: str, rules_version: str) -> frozenset[str]:
    """人工点过「保留」的存疑键集合——同一规则版本内不再重复呈现（见
    ``app.props.card_audit_consensus.filter_doubts_against_kept_decisions``）。
    规则版本前进后这个集合天然为空：新规则下是否该删是新问题，要重新判定。"""
    ensure_schema()
    rows = conn.execute(
        "SELECT doubt_key FROM prop_card_audit_doubt_decisions "
        "WHERE project_id=? AND prop_name=? AND rules_version=? AND decision='kept'",
        (project_id, prop_name, rules_version),
    ).fetchall()
    return frozenset(row["doubt_key"] for row in rows)


def update_failed(
    conn: Any, *, row_id: str, error: str, stamp: float, claim_token: str | None = None,
) -> bool:
    """同 ``insert_running`` 的建表前提；围栏语义同 ``update_ready``。"""
    if claim_token is None:
        conn.execute(
            "UPDATE prop_card_rule_audits SET status='failed', error=?, "
            "updated_at=? WHERE id=?",
            (error[:2000], stamp, row_id),
        )
        return True
    cursor = conn.execute(
        "UPDATE prop_card_rule_audits SET status='failed', error=?, "
        "updated_at=? WHERE id=? AND claim_token=?",
        (error[:2000], stamp, row_id, claim_token),
    )
    return cursor.rowcount > 0
