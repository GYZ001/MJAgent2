"""RBAC 第二阶段：SQLite 落地的真实登录会话（替换进程级共享秘密）。

约定与 ``app/mcp/auth.py`` 一致：落盘只存 token 的 sha256，明文只在签发那一刻
返回一次。区别在于这里落 SQLite 而不是 JSON 文件——``user_sessions`` 是高频
校验路径（几乎每个 /api/* 请求都要过一次），JSON 文件的整读整写在这个量级下
会成为瓶颈，SQLite 按主键点查 + 节流写回更合适。

Token 格式：``{session_id}.{secret}``，只 split 第一个 "."（secret 本身是
``secrets.token_urlsafe`` 输出，不含 "."，但按“第一个”切更保守）。

EP-03 第二阶段第二轮：``user_sessions.kind``（``'interactive'``|``'service'``，
lazy 补列见 ``app.provisioning.schema``）把交互式会话与长期服务凭证
（供自动化脚本用，如 ``data/regression_session_token.txt``）从"同一种凭证"
里拆出来——签发规则相同（同一张表、同一套 secret/hash 机制、``Principal``
解析完全一致，``can()`` 照常受角色约束，见 ``app.auth.principal``），差异
只在存活期判据：service 豁免空闲超时/并发上限/30 天绝对上限这三项，换成
签发时必须显式给定的有限 ``ttl_s``（业务规则——多长算"有限"——在
``app.auth.session_policy.issue_service_session`` 里，本模块的
``create_session`` 只负责按调用方传入的 kind/ttl_s 落库，不重复校验）。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading

from app.auth import session_policy
from app.auth.principal import Principal
from app.db import get_conn, new_id, now
from app.orgs.service import principal_context
from app.provisioning import schema as provisioning_schema

# 会话滑动过期窗口：每次有效访问都把 expires_at 续到 now + SESSION_TTL_S。
#
# 原来是 12 小时，实际用起来太短：隔夜不用就超窗，第二天上班要重登一次。而且它和
# 前端"令牌只放内存"叠加后更难受——刷新丢内存、隔夜丢会话，两头都掉。
# 改成 7 天滑动：日常使用（每天都会碰）永远不会掉线，真正长期不用的会话仍会自然
# 过期。绝对上限 30 天不变，所以最坏情况下一枚被窃令牌的寿命没有变长。
SESSION_TTL_S = 7 * 24 * 60 * 60
# 绝对上限：即便持续活跃，会话也不能超过 created_at 起 30 天。
ABSOLUTE_TTL_S = 30 * 24 * 60 * 60
# 续期写库节流：距离上次落库不足这个阈值就不再 UPDATE，避免轮询/媒体请求把
# SQLite 写爆。
SLIDING_WRITE_THROTTLE_S = 60.0
# 过期清理节流：只在这个间隔之外才真正跑一次 DELETE，同样是为了避免高频路径
# 上出现额外写开销。
PURGE_INTERVAL_S = 10 * 60.0

_purge_lock = threading.Lock()
_last_purge_at = 0.0


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _maybe_purge_expired() -> None:
    """节流触发 purge_expired：多数调用只是比较一次时间戳，不落库。"""
    global _last_purge_at
    ts = now()
    with _purge_lock:
        if ts - _last_purge_at < PURGE_INTERVAL_S:
            return
        _last_purge_at = ts
    purge_expired()


def create_session(
    user_id: str, *, user_agent: str | None = None, ip: str | None = None,
    kind: str = "interactive", ttl_s: float | None = None,
) -> str:
    """签发一枚新会话，返回明文 token（仅此一次，落盘只存 hash）。

    ``kind="service"``：长期服务凭证，调用方必须传 ``ttl_s``（有限，业务规则
    的上下限校验在 ``app.auth.session_policy.issue_service_session``，不在
    这里重复）；``kind="interactive"``（默认）时 ``ttl_s`` 通常留空，走既有
    的 ``SESSION_TTL_S`` 滑动窗口初值。本函数本身不区分调用方是谁，只按
    参数落库——kind 相关的豁免逻辑全部在 ``resolve_session``/
    ``session_policy`` 里按这一行的 ``kind`` 字段判定，不在这里分叉。
    """
    if ttl_s is not None and ttl_s <= 0:
        raise ValueError("ttl_s must be positive")
    conn = get_conn()
    # 同连接补列，不开独立连接：resolve_session/create_session 是全仓最热的
    # 两个函数之一，调用方随时可能已经在这个线程局部连接上持有未提交的写
    # 事务；ensure_schema() 的独立连接变体会去抢 BEGIN IMMEDIATE，2 秒超时
    # 后失败又被吞掉，表现成很远处的 "no such column"（2026-09-12 协调方
    # 审查揪出）。ensure_tables_on_connection 只在这一个 conn 上跑
    # CREATE/ALTER，参与调用方已有事务，不抢锁。
    provisioning_schema.ensure_tables_on_connection(conn)  # kind 补列
    sid = new_id("sess")
    secret = secrets.token_urlsafe(32)
    ts = now()
    effective_ttl = ttl_s if ttl_s is not None else SESSION_TTL_S
    conn.execute(
        """INSERT INTO user_sessions(
               id, user_id, secret_hash, created_at, last_seen_at, expires_at,
               user_agent, ip, kind
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (sid, user_id, _hash_secret(secret), ts, ts, ts + effective_ttl, user_agent, ip, kind),
    )
    conn.commit()
    # EP-03 第二阶段会话策略：并发会话数超限时踢最旧的**同 kind** 会话（这枚
    # 新会话永远不会踢自己）；session_max_concurrent<=0 或 kind!="interactive"
    # 时是 no-op（service 会话豁免并发上限，见模块文档）。
    session_policy.enforce_concurrent_limit(user_id, new_session_id=sid, kind=kind)
    _maybe_purge_expired()
    return f"{sid}.{secret}"


def _is_service(row) -> bool:
    return str(row["kind"] or "interactive") == "service"


def _maybe_touch_last_seen(conn, sid: str, row, ts: float, *, is_service: bool) -> None:
    """滑动过期，节流写：距上次落库超过阈值才真正 UPDATE。service 会话不滑动
    ``expires_at``（管理员签发时给定的到期时间是固定 deadline，不是"7 天不
    动就续期"），但仍然更新 ``last_seen_at``——管理员的活跃会话面板要靠它
    判断"这枚服务凭证还有没有人在用"。"""
    last_seen = float(row["last_seen_at"])
    if ts - last_seen <= SLIDING_WRITE_THROTTLE_S:
        return
    if is_service:
        conn.execute("UPDATE user_sessions SET last_seen_at=? WHERE id=?", (ts, sid))
    else:
        conn.execute(
            "UPDATE user_sessions SET last_seen_at=?, expires_at=? WHERE id=?",
            (ts, ts + SESSION_TTL_S, sid),
        )
    conn.commit()


def resolve_session(token: str | None) -> Principal | None:
    """校验 token 并返回对应 Principal；任何一步不合法都返回 None（不抛异常）。"""
    if not token or "." not in token:
        return None
    sid, _, secret = token.partition(".")
    if not sid or not secret:
        return None
    conn = get_conn()
    # 同连接补列，理由见 create_session 同一条注释——resolve_session 在几乎
    # 每个已认证请求上都会跑，绝不能用独立连接的 ensure_schema()。
    provisioning_schema.ensure_tables_on_connection(conn)  # kind 补列
    row = conn.execute(
        """SELECT id, user_id, secret_hash, created_at, last_seen_at, expires_at, revoked_at, kind
             FROM user_sessions WHERE id=?""",
        (sid,),
    ).fetchone()
    if row is None:
        return None
    if not hmac.compare_digest(_hash_secret(secret), str(row["secret_hash"] or "")):
        return None
    ts = now()
    is_service = _is_service(row)
    # EP-03 第二阶段会话策略：空闲超时 / 最长时长命中则当场撤销并落原因
    # （见 app.auth.session_policy 模块文档）；row 是查询快照，撤销发生在
    # DB 里不会反映到这个局部变量，靠返回值 True 判断，不再重查 row。
    # service 会话在 enforce_idle_and_age 内部就按 kind 豁免（数据驱动，不是
    # 这里分叉）。
    if session_policy.enforce_idle_and_age(sid, row, ts):
        return None
    if row["revoked_at"]:
        return None
    if float(row["expires_at"]) <= ts:
        return None
    # 30 天绝对上限只约束 interactive：service 的存活期完全由签发时的显式
    # ttl_s（上面已经落进 expires_at）决定，可以比 30 天长——这正是它存在的
    # 理由（长跑自动化任务/回归脚本），上一行的 expires_at 硬检查仍然适用，
    # 不是"不设上限"。
    if not is_service and float(row["created_at"]) + ABSOLUTE_TTL_S <= ts:
        return None
    user_row = conn.execute(
        "SELECT id, username, status, is_system_admin FROM users WHERE id=?",
        (row["user_id"],),
    ).fetchone()
    if user_row is None or str(user_row["status"]) != "active":
        return None
    _maybe_touch_last_seen(conn, sid, row, ts, is_service=is_service)
    _maybe_purge_expired()
    # EP-01：真实 HTTP 会话身份接入组织角色模型——role_governed 只在账号已经
    # 被拉进某个团队或直接授予某个项目时才为 True（见
    # app/orgs/store.py::user_is_governed 与 app/auth/principal.py 模块文档
    # 的 "role_governed" 段落；不能对所有会话恒置 True，否则这一阶段创建的、
    # 还没有机会被加进任何团队的新账号会被 Command Bus 拒绝一切非只读命令）。
    org_id, team_ids, permission_keys, is_governed = principal_context(str(user_row["id"]))
    return Principal(
        user_id=str(user_row["id"]),
        username=str(user_row["username"]),
        is_system_admin=bool(user_row["is_system_admin"]),
        org_id=org_id,
        team_ids=team_ids,
        permission_keys=permission_keys,
        role_governed=is_governed,
    )


def revoke_session(session_id: str) -> bool:
    conn = get_conn()
    cur = conn.execute(
        "UPDATE user_sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
        (now(), session_id),
    )
    conn.commit()
    return cur.rowcount > 0


def revoke_all_for_user(user_id: str) -> int:
    conn = get_conn()
    cur = conn.execute(
        "UPDATE user_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
        (now(), user_id),
    )
    conn.commit()
    return cur.rowcount


def purge_expired() -> int:
    """尽力清理已经过期（滑动窗口或绝对上限）的会话行，非关键路径调用。"""
    conn = get_conn()
    ts = now()
    cur = conn.execute(
        "DELETE FROM user_sessions WHERE expires_at<=? OR created_at+?<=?",
        (ts, ABSOLUTE_TTL_S, ts),
    )
    conn.commit()
    return cur.rowcount
