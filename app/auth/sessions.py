"""RBAC 第二阶段：SQLite 落地的真实登录会话（替换进程级共享秘密）。

约定与 ``app/mcp/auth.py`` 一致：落盘只存 token 的 sha256，明文只在签发那一刻
返回一次。区别在于这里落 SQLite 而不是 JSON 文件——``user_sessions`` 是高频
校验路径（几乎每个 /api/* 请求都要过一次），JSON 文件的整读整写在这个量级下
会成为瓶颈，SQLite 按主键点查 + 节流写回更合适。

Token 格式：``{session_id}.{secret}``，只 split 第一个 "."（secret 本身是
``secrets.token_urlsafe`` 输出，不含 "."，但按“第一个”切更保守）。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading

from app.auth.principal import Principal
from app.db import get_conn, new_id, now

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


def create_session(user_id: str, *, user_agent: str | None = None, ip: str | None = None) -> str:
    """签发一枚新会话，返回明文 token（仅此一次，落盘只存 hash）。"""
    sid = new_id("sess")
    secret = secrets.token_urlsafe(32)
    ts = now()
    conn = get_conn()
    conn.execute(
        """INSERT INTO user_sessions(
               id, user_id, secret_hash, created_at, last_seen_at, expires_at,
               user_agent, ip
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (sid, user_id, _hash_secret(secret), ts, ts, ts + SESSION_TTL_S, user_agent, ip),
    )
    conn.commit()
    _maybe_purge_expired()
    return f"{sid}.{secret}"


def resolve_session(token: str | None) -> Principal | None:
    """校验 token 并返回对应 Principal；任何一步不合法都返回 None（不抛异常）。"""
    if not token or "." not in token:
        return None
    sid, _, secret = token.partition(".")
    if not sid or not secret:
        return None
    conn = get_conn()
    row = conn.execute(
        """SELECT id, user_id, secret_hash, created_at, last_seen_at, expires_at, revoked_at
             FROM user_sessions WHERE id=?""",
        (sid,),
    ).fetchone()
    if row is None:
        return None
    if not hmac.compare_digest(_hash_secret(secret), str(row["secret_hash"] or "")):
        return None
    ts = now()
    if row["revoked_at"]:
        return None
    if float(row["expires_at"]) <= ts:
        return None
    if float(row["created_at"]) + ABSOLUTE_TTL_S <= ts:
        return None
    user_row = conn.execute(
        "SELECT id, username, status, is_system_admin FROM users WHERE id=?",
        (row["user_id"],),
    ).fetchone()
    if user_row is None or str(user_row["status"]) != "active":
        return None
    # 滑动过期，节流写：距上次落库超过阈值才真正 UPDATE。
    last_seen = float(row["last_seen_at"])
    if ts - last_seen > SLIDING_WRITE_THROTTLE_S:
        conn.execute(
            "UPDATE user_sessions SET last_seen_at=?, expires_at=? WHERE id=?",
            (ts, ts + SESSION_TTL_S, sid),
        )
        conn.commit()
    _maybe_purge_expired()
    return Principal(
        user_id=str(user_row["id"]),
        username=str(user_row["username"]),
        is_system_admin=bool(user_row["is_system_admin"]),
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
