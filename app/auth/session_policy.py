"""EP-03 第二阶段：会话策略——空闲超时、最长时长、单账号并发上限（L2，见
app/LAYERS.toml::app.auth.session_policy）。

三个策略键全部走 ``settings``（``session_idle_timeout_min``/
``session_max_age_hours``/``session_max_concurrent``，见
``app.monitoring.SETTINGS_SCHEMA``），不新建配置体系；``user_sessions.
revoked_reason`` 补列的 DDL 放在 ``app.provisioning.schema``（同层，见该模块
文档「user_sessions 补列」一段），本模块只是调用方。

**故意不 import app.auth.sessions**：``app.auth.sessions.resolve_session``/
``create_session`` 要调用本模块做策略判定（空闲/超龄/并发），若本模块反过来
import ``app.auth.sessions`` 去复用它的 ``_hash_secret``，会构成真实循环导入
（``sessions`` -> ``session_policy`` -> ``sessions``）。``_hash_secret`` 因此
在这里独立复制一份（6 行 stdlib 调用，唯一的替代方案——把它挪到第三个更底层
的模块——对一个几乎不会再变的哈希函数是过度设计，不做）。

被policy 踢下线的会话必须让用户看得到原因（CLAUDE.md「拦住用户时必须给出
路」）：``enforce_idle_and_age``/``enforce_concurrent_limit`` 撤销会话时都会
落 ``revoked_reason``；``describe_invalid_token`` 供 ``app.local_session.
require_local_session`` 鉴权失败时查一次这个原因，替换掉笼统的"缺少或无效的
本机会话凭证"。查询前仍会验证 token 的 secret（与 ``resolve_session`` 同一
条件），避免变成"随便猜一个 session id 就能探测它的撤销原因"的信息泄露口子。

**服务会话（``kind="service"``，EP-03 第二阶段第二轮）**：交互式会话策略
（空闲超时/并发上限）与长期自动化凭证（回归/驱动脚本）共用同一张
``user_sessions`` 表、同一套 token 机制，但存活期判据完全不同——服务会话
豁免 ``enforce_idle_and_age``/``enforce_concurrent_limit`` 这两项（判据挂在
每一行自己的 ``kind`` 字段上，不是按调用方身份/路径特判），换成
``issue_service_session`` 签发时必须显式给定、且落在
``[SERVICE_SESSION_MIN_TTL_DAYS, SERVICE_SESSION_MAX_TTL_DAYS]`` 区间内的
``ttl_days``——"可以很长，但不许无限"，不设上限就是把风险留给将来。
``issue_service_session`` 内部延迟 import ``app.auth.sessions.create_session``
（而不是模块顶层 import）：理由与上面"故意不 import app.auth.sessions"完全
一致，本模块不能在模块级反向依赖 ``sessions``（后者模块级 import 了本模块）
——延迟到函数体内、真正被调用时才导入，此时两个模块都已初始化完毕，不会
触发循环导入。

**全部懒加载建表一律用同连接的 ``ensure_tables_on_connection(conn)``，不用
``ensure_schema()``**（2026-09-12 协调方审查修复）：本模块与
``app.auth.sessions`` 是全仓最热的路径之一，`ensure_schema()` 那个独立连接
+ ``BEGIN IMMEDIATE`` 的变体一旦撞上调用方线程局部连接上尚未提交的写事务，
会在 2 秒 ``WRITE_TXN_BUSY_TIMEOUT_S`` 超时后失败，而失败又被
``ensure_schema()`` 自己的 ``except`` 分支吞掉（只落可观测记录，不向上抛），
于是后续真正的写语句才会报出一个和病因隔着好几层调用栈的 ``no such
column``。真实撞上过的路径：``app.auth.admin_api.update_user`` 同一次请求
既改 ``status`` 又改 ``password`` 时，前者的会话吊销写入未提交就进了后者的
``enforce_password_change`` -> ``check_history_reuse``。
"""
from __future__ import annotations

import hashlib
import hmac

from fastapi import HTTPException

from app.db import get_conn, get_setting, now
from app.provisioning import schema as provisioning_schema

# "可以很长，但不许无限"：区间本身是产品拍板的安全边界，不是从现状反推的
# 分位数——1 天太短起不到"长期服务凭证"的作用，400 天（略超一年）给足自动化
# 场景的续期缓冲，同时仍然强制到期轮换，不留"永久令牌"这种口子。
SERVICE_SESSION_MIN_TTL_DAYS = 1
SERVICE_SESSION_MAX_TTL_DAYS = 400

_REASON_LABELS: dict[str, str] = {
    "idle_timeout": "长时间无操作，会话已自动登出，请重新登录",
    "max_age": "会话已达到最长时长上限，请重新登录",
    "concurrent_limit": "同一账号的登录数超过上限，这是最早登录的一个会话，已被新的登录挤下线",
    # 管理员强制下线（app.auth.admin_api::revoke_user_session）也落这一列，
    # 让被踢用户看到具体原因而不是笼统的"会话失效"。
    "admin_revoked": "管理员已强制下线此会话，请联系管理员了解详情",
}
_GENERIC_MESSAGE = "缺少或无效的本机会话凭证"


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def idle_timeout_s() -> float:
    try:
        return float(get_setting("session_idle_timeout_min") or 480) * 60.0
    except (TypeError, ValueError):
        return 480.0 * 60.0


def max_age_s() -> float:
    try:
        return float(get_setting("session_max_age_hours") or 12) * 3600.0
    except (TypeError, ValueError):
        return 12.0 * 3600.0


def concurrent_limit() -> int:
    try:
        return int(float(get_setting("session_max_concurrent") or 0))
    except (TypeError, ValueError):
        return 0


def _revoke_with_reason(session_id: str, reason: str, *, ts: float) -> None:
    conn = get_conn()
    # 同连接补列，不开独立连接——本函数从 enforce_idle_and_age 被
    # resolve_session（全仓最热路径之一）调用，调用方随时可能已经在这个
    # 线程局部连接上持有未提交写事务（2026-09-12 协调方审查揪出，见
    # app.auth.sessions.create_session 同一条注释）。
    provisioning_schema.ensure_tables_on_connection(conn)
    conn.execute(
        "UPDATE user_sessions SET revoked_at=?, revoked_reason=? WHERE id=? AND revoked_at IS NULL",
        (ts, reason, session_id),
    )
    conn.commit()


def enforce_idle_and_age(session_id: str, row, ts: float) -> bool:
    """空闲超时 / 最长时长判定，命中则撤销并落原因，返回 True——调用方
    （``app.auth.sessions.resolve_session``）应把这枚会话当无效处理。已经被
    撤销的行、以及 ``kind="service"`` 的行直接返回 False：后者的存活期由
    签发时的显式 ``ttl_s`` 决定（见 ``issue_service_session``），不受这两个
    可配置阈值约束——判据是这一行自己的 ``kind`` 字段，不是调用方身份。"""
    if row["revoked_at"] or str(row["kind"] or "interactive") == "service":
        return False
    if ts - float(row["last_seen_at"]) > idle_timeout_s():
        _revoke_with_reason(session_id, "idle_timeout", ts=ts)
        return True
    if ts - float(row["created_at"]) > max_age_s():
        _revoke_with_reason(session_id, "max_age", ts=ts)
        return True
    return False


def enforce_concurrent_limit(user_id: str, *, new_session_id: str, kind: str = "interactive") -> None:
    """``session_max_concurrent<=0`` 时不限；``kind!="interactive"``（服务会话）
    时同样不限——既不占用交互式会话的并发预算，也不会被这条逻辑踢掉，判据是
    这次新建会话自己的 ``kind``，不是调用方身份。否则踢掉超出上限的最旧
    **同为 interactive** 的会话（新签发的这一枚永远不会被自己踢掉）。由
    ``app.auth.sessions.create_session`` 在新会话插入并提交之后调用。"""
    if kind != "interactive":
        return
    limit = concurrent_limit()
    if limit <= 0:
        return
    conn = get_conn()
    provisioning_schema.ensure_tables_on_connection(conn)  # 同连接补列，理由同上
    ts = now()
    active = conn.execute(
        "SELECT id FROM user_sessions WHERE user_id=? AND kind='interactive'"
        " AND revoked_at IS NULL AND expires_at>? ORDER BY created_at ASC",
        (user_id, ts),
    ).fetchall()
    overflow = len(active) - limit
    if overflow <= 0:
        return
    to_kick = [r["id"] for r in active if r["id"] != new_session_id][:overflow]
    for sid in to_kick:
        conn.execute(
            "UPDATE user_sessions SET revoked_at=?, revoked_reason=? WHERE id=? AND revoked_at IS NULL",
            (ts, "concurrent_limit", sid),
        )
    conn.commit()


def issue_service_session(
    user_id: str, *, ttl_days: float, user_agent: str | None = None, ip: str | None = None,
) -> tuple[str, float]:
    """管理员签发一枚长期服务会话，返回 ``(session_token, expires_at)``。

    ``ttl_days`` 必须落在 ``[SERVICE_SESSION_MIN_TTL_DAYS,
    SERVICE_SESSION_MAX_TTL_DAYS]`` 区间内——"可以很长，但不许无限"是产品
    拍板的安全边界，这里就是唯一的强制点，不许调用方绕过。本函数不是 HTTP
    入口、不做鉴权：调用方（``app.auth.admin_api`` 的 REST 路由）必须已经
    过了 ``require_system_admin``。签发的会话仍然绑定 ``user_id`` 这个真实
    账号身份——``Principal``/``can()`` 照常按该账号的角色判定，豁免的只是
    会话时长类策略，不是权限（见 ``app.auth.sessions`` 模块文档）。
    """
    if not (SERVICE_SESSION_MIN_TTL_DAYS <= ttl_days <= SERVICE_SESSION_MAX_TTL_DAYS):
        raise HTTPException(
            422,
            f"服务会话有效期必须在 {SERVICE_SESSION_MIN_TTL_DAYS}~"
            f"{SERVICE_SESSION_MAX_TTL_DAYS} 天之间（不支持无限期）",
        )
    # 延迟 import：避免与 app.auth.sessions（模块顶层 import 了本模块）构成
    # 循环导入，见模块文档「服务会话」一段。
    from app.auth.sessions import create_session

    ttl_s = ttl_days * 86400.0
    token = create_session(user_id, user_agent=user_agent, ip=ip, kind="service", ttl_s=ttl_s)
    return token, now() + ttl_s


def describe_invalid_token(token: str | None) -> str:
    """鉴权失败时的具体原因；token 密钥验证不通过或查无此会话一律退化成
    通用提示，不泄露"这个 session id 是否存在"这类信息。"""
    if not token or "." not in token:
        return _GENERIC_MESSAGE
    sid, _, secret = token.partition(".")
    if not sid or not secret:
        return _GENERIC_MESSAGE
    conn = get_conn()
    provisioning_schema.ensure_tables_on_connection(conn)  # 同连接补列，理由同上
    row = conn.execute(
        "SELECT secret_hash, revoked_at, revoked_reason FROM user_sessions WHERE id=?", (sid,),
    ).fetchone()
    if row is None:
        return _GENERIC_MESSAGE
    if not hmac.compare_digest(_hash_secret(secret), str(row["secret_hash"] or "")):
        return _GENERIC_MESSAGE
    if row["revoked_at"] and row["revoked_reason"]:
        return _REASON_LABELS.get(str(row["revoked_reason"]), _GENERIC_MESSAGE)
    return _GENERIC_MESSAGE
