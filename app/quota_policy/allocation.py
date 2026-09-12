"""三级（org → team → user）取最紧判定（L2，见 app/LAYERS.toml::app.quota_policy）。

``resolve_effective_limits()`` 是 ``app.quota.effective_limits()`` 的唯一取值
来源（EP-04 §4）：每个维度独立取候选池里的最小值，候选池固定包含调用方已经
算好的账号自身档位（``builtin``，即今天的 ``TIER_TABLE.get(tier, ...)``），
再叠加该账号所属 org / 所有 team / 自己（user）三级里**显式配置过**的
quota_allocations（未配置的层级不参与候选，语义是"继承上级"而不是"这一级判
不限"——JSON 里"缺键"与"键存在但值是 null"是两码事，见 plans.py 的
``validate_limits_payload`` 文档）。``None``（不限）当作 +inf 参与 min：只有
候选池里**所有**候选都是 None 时最终结果才是 None，这正是 PRD §4"None 只在
上级也不限时才生效"的字面含义——``builtin`` 对五档真实用户永远是具体数字，
因此这条分支只在系统管理员/账号缺失（``effective_limits`` 在调用本函数前就
已经用 ``_UNLIMITED`` 短路返回，见该函数）时才会出现。

过期的 quota_allocations（``expires_at`` 已过）视为未配置，退回继承上级，与
``app.quota_expiry`` 对到期账号的既有处理同一口径（本模块不做任何清理写入，
纯读判断）。
"""
from __future__ import annotations

import json
import sqlite3
from types import MappingProxyType

from fastapi import HTTPException

from app import config
from app import quota_tiers
from app.db import new_id, now
from app.orgs import store as orgs_store
from app.quota_policy import schema
from app.quota_policy.schema import DIMENSIONS
from app.quota_tiers import TierLimits

VALID_SCOPE_TYPES = frozenset({"org", "team", "user"})

_Candidate = tuple[float | int | None, str | None]


def resolve_effective_limits(conn: sqlite3.Connection, *, user_id: str, builtin: TierLimits) -> TierLimits:
    # 建表兜底在叶子函数 scope_limits_dict 里做（同连接、不抢锁）；这里不重复
    # 调用 schema.ensure_schema()——那个入口开独立连接抢 BEGIN IMMEDIATE，会
    # 跟本函数调用方（check_module_concurrency 等）已经持有的写锁抢锁，2 秒
    # busy_timeout 超时，见 app/quota_policy/schema.py 模块文档。
    #
    # 本函数及其调用的 _org_id_of/_team_ids_of/_org_name/_team_name 一律不经
    # app.orgs.store——2026-09-12 实测：app.orgs.store 的读函数（user_org_id/
    # list_team_ids_for_user/get_org/get_team 等）现在都会调用
    # app.orgs.schema.ensure_tables_on_connection(conn)，其实现是
    # conn.executescript(_SCHEMA_DDL)。CPython sqlite3 文档明确记载
    # executescript() 执行前会对"挂起中的事务"做一次隐式 COMMIT——本函数被
    # app.quota 的并发闸门（check_module_concurrency 等）在调用方已经持有的
    # BEGIN IMMEDIATE 事务里调用，一旦触发这条隐式 COMMIT，占位所在的独占事
    # 务会被提前偷偷提交掉（此时还没写任何行，COMMIT 只是白白释放写锁），另一
    # 个并发请求就能在同一个窗口里读到同样的"未超限"计数，两边都各自建一行，
    # 把并发上限撑破——这正是
    # test_storyboard_reservation_admits_exactly_one_under_real_concurrency
    # 间歇性放行两个（约 20% 复现率）的根因，用 in_transaction 前后打点 +
    # 200+ 次连跑复现确认。修复方式：本模块永远只用一条只读 SELECT 直接读
    # users.org_id / team_members / orgs / teams，不调用 app.orgs.store 的任
    # 何函数——因此也就不会触发它内部的 executescript()。app.orgs 是否要把
    # ensure_tables_on_connection 的实现换成逐条 execute()（同 app/quota_
    # policy/schema.py 的写法，不会触发隐式提交）不在本模块职责范围内。
    candidates: dict[str, list[_Candidate]] = {d: [(getattr(builtin, d), None)] for d in DIMENSIONS}

    org_id = _org_id_of(conn, user_id)
    if org_id:
        _merge_scope(conn, candidates, "org", org_id, _org_name(conn, org_id))
    for team_id in sorted(_team_ids_of(conn, user_id)):
        _merge_scope(conn, candidates, "team", team_id, _team_name(conn, team_id))
    _merge_scope(conn, candidates, "user", user_id, _user_display_name(conn, user_id))

    values: dict[str, float | int | None] = {}
    bound_by: dict[str, str] = {}
    for dim in DIMENSIONS:
        value, tag = _tightest_with_label(candidates[dim])
        values[dim] = value
        if tag is not None:
            bound_by[dim] = tag
    return TierLimits(tier=builtin.tier, bound_by=MappingProxyType(bound_by), **values)


def _org_id_of(conn: sqlite3.Connection, user_id: str) -> str | None:
    """直接读 ``users.org_id``，不经过 ``app.orgs.store.user_org_id``——理由见
    ``resolve_effective_limits`` 顶部的大段注释（避免触发它内部的
    ``ensure_tables_on_connection`` / ``executescript`` 隐式提交）。
    ``OperationalError``（手写简化 schema 从未跑过 app.orgs 的迁移，
    ``users.org_id`` 列不存在）退化为"这个账号没有组织"，与
    ``app.quota._user_row`` 对同类既有测试双的既有处理是同一条约定。"""
    try:
        row = conn.execute("SELECT org_id FROM users WHERE id=?", (user_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return (row["org_id"] or None) if row else None


def _team_ids_of(conn: sqlite3.Connection, user_id: str) -> frozenset[str]:
    """直接读 ``team_members``，不经过
    ``app.orgs.store.list_team_ids_for_user``——同上一条理由。"""
    try:
        rows = conn.execute("SELECT team_id FROM team_members WHERE user_id=?", (user_id,)).fetchall()
    except sqlite3.OperationalError:
        return frozenset()
    return frozenset(r["team_id"] for r in rows)


def _org_name(conn: sqlite3.Connection, org_id: str) -> str | None:
    """直接读 ``orgs.name``，不经过 ``app.orgs.store.get_org``——同上一条理由。"""
    try:
        row = conn.execute("SELECT name FROM orgs WHERE id=?", (org_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row["name"] if row else None


def _team_name(conn: sqlite3.Connection, team_id: str) -> str | None:
    """直接读 ``teams.name``，不经过 ``app.orgs.store.get_team``——同上一条理由。"""
    try:
        row = conn.execute("SELECT name FROM teams WHERE id=?", (team_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row["name"] if row else None


def _user_display_name(conn: sqlite3.Connection, user_id: str) -> str | None:
    row = conn.execute("SELECT display_name, username FROM users WHERE id=?", (user_id,)).fetchone()
    if row is None:
        return None
    return row["display_name"] or row["username"]


def _merge_scope(
    conn: sqlite3.Connection, candidates: dict[str, list[_Candidate]],
    scope_type: str, scope_id: str, label_name: str | None,
) -> None:
    limits = scope_limits_dict(conn, scope_type, scope_id)
    if not limits:
        return
    tag = f"{scope_type}={label_name or scope_id}"
    for dim, value in limits.items():
        candidates[dim].append((value, tag))


def _tightest_with_label(pairs: list[_Candidate]) -> _Candidate:
    """None 当 +inf：``concrete`` 为空（全体候选都是 None，或候选池本身为
    空——理论上不会，``builtin`` 总在场）时返回 ``(None, None)``；否则取数值
    最小的一条。多条并列最小值时取先出现的一条（``builtin`` 恒排第一——并列
    时消息回退到既有的"{tier} 档"文案，不算错误，只是没有指出恰好并列的那
    个组织/团队，可接受的降级）。"""
    concrete = [pair for pair in pairs if pair[0] is not None]
    if not concrete:
        return None, None
    return min(concrete, key=lambda pair: pair[0])


def scope_limits_dict(conn: sqlite3.Connection, scope_type: str, scope_id: str) -> dict[str, float | int | None]:
    """某一 scope 当前生效的显式维度字典（plan.limits_json 叠加
    overrides_json）；未配置分配、或分配已过期，返回空字典（＝这一层没有意
    见，继承上级）。"""
    schema.ensure_tables_on_connection(conn)
    row = conn.execute(
        "SELECT plan_id, overrides_json, expires_at FROM quota_allocations "
        "WHERE scope_type=? AND scope_id=?", (scope_type, scope_id),
    ).fetchone()
    if row is None:
        return {}
    if row["expires_at"] and float(row["expires_at"]) <= now():
        return {}
    merged: dict[str, float | int | None] = {}
    plan = conn.execute("SELECT limits_json FROM quota_plans WHERE id=?", (row["plan_id"],)).fetchone()
    if plan is not None:
        merged.update(json.loads(plan["limits_json"]))
    if row["overrides_json"]:
        merged.update(json.loads(row["overrides_json"]))
    return {k: v for k, v in merged.items() if k in DIMENSIONS}


# ---------------------------------------------------------------------------
# 写侧：分配额度（quota_policy/api.py 的 PUT /api/system/quota/allocations/...
# 是唯一入口——CLAUDE.md「绕过扫描：分配/追加/改策略全部有 HTTP 入口，无一需
# 要改库」）。
# ---------------------------------------------------------------------------


def _assert_scope_exists(conn: sqlite3.Connection, scope_type: str, scope_id: str) -> None:
    if scope_type == "org":
        found = orgs_store.get_org(conn, scope_id) is not None
    elif scope_type == "team":
        found = orgs_store.get_team(conn, scope_id) is not None
    else:
        found = conn.execute("SELECT 1 FROM users WHERE id=?", (scope_id,)).fetchone() is not None
    if not found:
        raise HTTPException(404, f"{scope_type}={scope_id!r} 不存在")


def set_allocation(
    conn: sqlite3.Connection, *, scope_type: str, scope_id: str, plan_id: str,
    overrides: dict[str, float | int | None] | None, expires_at: float | None, created_by: str,
) -> str:
    schema.ensure_schema()
    if scope_type not in VALID_SCOPE_TYPES:
        raise HTTPException(422, f"scope_type 必须是 org/team/user 之一，收到 {scope_type!r}")
    _assert_scope_exists(conn, scope_type, scope_id)
    if conn.execute("SELECT 1 FROM quota_plans WHERE id=?", (plan_id,)).fetchone() is None:
        raise HTTPException(404, f"策略不存在：plan_id={plan_id!r}")
    ts = now()
    overrides_json = json.dumps(overrides, ensure_ascii=False) if overrides else None
    existing = conn.execute(
        "SELECT id FROM quota_allocations WHERE scope_type=? AND scope_id=?", (scope_type, scope_id),
    ).fetchone()
    if existing is not None:
        conn.execute(
            "UPDATE quota_allocations SET plan_id=?, overrides_json=?, expires_at=?, "
            "period_started_at=? WHERE id=?",
            (plan_id, overrides_json, expires_at, ts, existing["id"]),
        )
        return existing["id"]
    alloc_id = new_id("qa")
    conn.execute(
        "INSERT INTO quota_allocations(id, scope_type, scope_id, plan_id, overrides_json, "
        "period_started_at, expires_at, created_at, created_by) VALUES(?,?,?,?,?,?,?,?,?)",
        (alloc_id, scope_type, scope_id, plan_id, overrides_json, ts, expires_at, ts, created_by),
    )
    return alloc_id


def get_allocation(conn: sqlite3.Connection, scope_type: str, scope_id: str) -> dict | None:
    schema.ensure_schema()
    row = conn.execute(
        "SELECT * FROM quota_allocations WHERE scope_type=? AND scope_id=?", (scope_type, scope_id),
    ).fetchone()
    return _allocation_view(row) if row is not None else None


def list_allocations(conn: sqlite3.Connection, org_id: str) -> list[dict]:
    """本组织范围内**已配置**的全部分配（org 自身 + 下属团队 + 下属用户），
    不列出未配置的 scope——未配置＝继承上级，没有数字可展示，由
    ``quota_policy.api`` 按需叠加用量再返回给前端。"""
    schema.ensure_schema()
    scopes: list[tuple[str, str]] = [("org", org_id)]
    scopes += [("team", t["id"]) for t in orgs_store.list_teams(conn, org_id)]
    scopes += [
        ("user", r["id"]) for r in conn.execute("SELECT id FROM users WHERE org_id=?", (org_id,)).fetchall()
    ]
    result: list[dict] = []
    for scope_type, scope_id in scopes:
        row = conn.execute(
            "SELECT * FROM quota_allocations WHERE scope_type=? AND scope_id=?", (scope_type, scope_id),
        ).fetchone()
        if row is not None:
            result.append(_allocation_view(row))
    return result


def _allocation_view(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "scope_type": row["scope_type"], "scope_id": row["scope_id"],
        "plan_id": row["plan_id"],
        "overrides": json.loads(row["overrides_json"]) if row["overrides_json"] else {},
        "period_started_at": row["period_started_at"], "expires_at": row["expires_at"],
        "created_at": row["created_at"],
    }


def admin_contact_text(conn: sqlite3.Connection, user_id: str) -> str:
    """企业形态升级文案的联系入口（EP-04 §8：「联系组织管理员申请额度」不能
    只说这一句话而不给联系方式，见 CLAUDE.md「拦住用户时必须给出路」）。优先
    本组织持有 ``org_admin`` 角色的成员；一个都没有（org_admin 角色尚未分配
    给任何人）时退到系统管理员——系统管理员至少存在一个是全仓不变式
    （``app/auth/admin_api.py`` 禁止删除最后一个管理员），因此这个兜底永远
    能给出非空联系人，不会把用户晾在原地。由 ``app.quota`` 调用，不在
    ``app.quota`` 里直接查 ``app.orgs``——避免给它新增一条对 ``app.orgs`` 的
    直接依赖，借道本模块已有的 ``orgs_store`` import。用 ``_org_id_of``（不是
    直接 ``orgs_store.user_org_id``）：本函数同样可能被企业形态下的配额闸门
    在调用方已持有的 ``BEGIN IMMEDIATE`` 事务里调用，需要同一条抢锁防护。"""
    org_id = _org_id_of(conn, user_id)
    names: list[str] = []
    if org_id:
        rows = conn.execute(
            "SELECT DISTINCT u.display_name, u.username FROM team_members tm "
            "JOIN teams t ON t.id = tm.team_id JOIN roles r ON r.id = tm.role_id "
            "JOIN users u ON u.id = tm.user_id WHERE t.org_id=? AND r.key='org_admin' LIMIT 5",
            (org_id,),
        ).fetchall()
        names = [r["display_name"] or r["username"] for r in rows]
    if not names:
        rows = conn.execute(
            "SELECT display_name, username FROM users WHERE is_system_admin=1 LIMIT 5",
        ).fetchall()
        names = [r["display_name"] or r["username"] for r in rows]
    return f"（管理员：{'、'.join(names)}）" if names else ""


def upgrade_path_for(conn: sqlite3.Connection | None, user_id: str | None, tier: str) -> str:
    """``app.quota.QuotaExceeded`` 的 ``upgrade_path`` 字段。SaaS（默认）：
    既有的「升级到 xx 档位」文案，逐字不变——本仓库上线以来唯一的产品形态，
    ``tests/test_quota.py`` 多条用例隐式依赖这份文案不变（迁移零变化）。企业
    形态（``config.is_enterprise_profile()``）：支付入口已经 403
    （``app/payments/routes.py``），继续给这份文案等于把用户指向一条已经关
    闭的路，换成 EP-04 §8 要求的"联系组织管理员申请额度"并带真实联系人。
    ``conn``/``user_id`` 缺失时（测试直接构造 ``QuotaExceeded``、或极少数没
    有账号上下文的调用点）保守退回 SaaS 文案，不猜测联系人。"""
    if not config.is_enterprise_profile() or conn is None or user_id is None:
        table = quota_tiers._UPGRADE_PATH
        return table.get(tier, table["free"])
    return quota_tiers.enterprise_upgrade_path(admin_contact_text(conn, user_id))


def tier_or_scope_label(limits: TierLimits, dim: str) -> str:
    """``QuotaExceeded`` 消息里"是哪一档/哪一级"的短语。``limits.bound_by``
    为空（账号自身档位本来就是最紧的一环——没有组织/团队/用户级分配收紧过，
    迁移前后的默认状态）时原样保留既有的"{tier} 档"文案；被收紧过时换成能
    定位到具体策略的短语，形如"团队「内容中心」（team=内容中心）"——
    CLAUDE.md「拦住用户时必须给出路」要求消息写清是哪一级、哪条策略挡的。"""
    info = limits.bound_by.get(dim)
    if not info:
        return f"{limits.tier} 档"
    scope_type, _, scope_name = info.partition("=")
    scope_cn = {"org": "组织", "team": "团队", "user": "个人分配"}.get(scope_type, scope_type)
    return f"{scope_cn}「{scope_name}」（{info}）"
