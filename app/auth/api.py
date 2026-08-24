"""RBAC 第二阶段：登录 / 登出 / 当前用户 / 改密（``/api/auth/*``）。

登录端点本身不要求已有会话（否则先有鸡还是先有蛋），但仍必须过 Origin/CSRF
闸门——复用 ``app.local_session.assert_session_bootstrap_allowed``，与
``GET /api/session`` 领取匿名会话时完全一致的口子。其余端点都挂
``require_local_session``，和其它 ``/api/*`` 路由同一套闸门。
"""
from __future__ import annotations

import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth.passwords import hash_password, verify_password
from app.auth.principal import Principal, get_current_principal
from app.auth.sessions import create_session, resolve_session, revoke_all_for_user, revoke_session
from app.db import get_conn, now
from app.local_session import assert_session_bootstrap_allowed, require_local_session

router = APIRouter(prefix="/api/auth", tags=["auth"])

_MIN_NEW_PASSWORD_LEN = 8

# 登录失败节流：同一用户名 5 分钟内失败 5 次即拒绝，直到最早一次失败滑出窗口。
# 只用一个小 dict + 时间戳，不引入新依赖。
_LOGIN_FAILURE_WINDOW_S = 5 * 60
_LOGIN_FAILURE_MAX = 5
_login_failures_lock = threading.Lock()
_login_failures: dict[str, list[float]] = {}

# 用户名不存在时也跑一次哈希验证，摊平「账号是否存在」的时序差异。
_DUMMY_PASSWORD_HASH = hash_password("__no-such-account__")


def _throttle_key(username: str) -> str:
    return username.strip().lower()


def _check_login_throttle(username: str) -> None:
    key = _throttle_key(username)
    ts = time.time()
    with _login_failures_lock:
        attempts = [t for t in _login_failures.get(key, []) if ts - t < _LOGIN_FAILURE_WINDOW_S]
        if attempts:
            _login_failures[key] = attempts
        elif key in _login_failures:
            del _login_failures[key]
        if len(attempts) >= _LOGIN_FAILURE_MAX:
            raise HTTPException(429, "登录尝试过多，请稍后再试")


def _record_login_failure(username: str) -> None:
    key = _throttle_key(username)
    with _login_failures_lock:
        _login_failures.setdefault(key, []).append(time.time())


def _clear_login_failures(username: str) -> None:
    key = _throttle_key(username)
    with _login_failures_lock:
        _login_failures.pop(key, None)


def _workspaces_payload(principal: Principal) -> list[dict[str, str]]:
    conn = get_conn()
    if principal.is_system_admin:
        # 系统管理员隐式是所有空间的成员；真实 workspace_members 行（如有）优先。
        rows = conn.execute("SELECT id, name FROM workspaces WHERE status='active'").fetchall()
        return [
            {
                "id": str(r["id"]),
                "name": str(r["name"]),
                "role": principal.workspace_roles.get(str(r["id"]), "workspace_admin"),
            }
            for r in rows
        ]
    # 必须和 resolve_session 用同一个口径（只认 status='active'）。这里少一个条件，
    # 用户就会在界面上看到一个自己其实已经没有任何权限的团队——点进去每个请求都
    # 404，而"团队还在列表里"会让人以为是系统坏了。两处算"我属于哪些团队"的地方
    # 只要有一处不带 status，就会出现这种展示与授权不一致。
    rows = conn.execute(
        """SELECT w.id AS id, w.name AS name
             FROM workspace_members m JOIN workspaces w ON w.id=m.workspace_id
            WHERE m.user_id=? AND w.status='active'""",
        (principal.user_id,),
    ).fetchall()
    return [
        {
            "id": str(r["id"]),
            "name": str(r["name"]),
            "role": principal.workspace_roles.get(str(r["id"]), ""),
        }
        for r in rows
    ]


def _profile_payload(principal: Principal) -> dict:
    conn = get_conn()
    row = conn.execute(
        "SELECT id, username, display_name, must_change_password FROM users WHERE id=?",
        (principal.user_id,),
    ).fetchone()
    if row is not None:
        user = {
            "id": str(row["id"]),
            "username": str(row["username"]),
            "display_name": str(row["display_name"] or row["username"]),
        }
        must_change_password = bool(row["must_change_password"])
    else:
        # legacy 共享会话（MJ_LEGACY_SHARED_SESSION，Stage 8 前）没有真实用户行。
        user = {
            "id": principal.user_id,
            "username": principal.username,
            "display_name": principal.username,
        }
        must_change_password = False
    return {
        "user": user,
        "workspaces": _workspaces_payload(principal),
        "is_system_admin": principal.is_system_admin,
        "must_change_password": must_change_password,
    }


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post("/login")
def login(body: dict, request: Request):
    """账号密码登录；成功后签发一枚真实用户会话，替代旧的共享秘密。"""
    assert_session_bootstrap_allowed(request)
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    generic_error = HTTPException(401, "用户名或密码不正确")
    if not username or not password:
        raise generic_error
    _check_login_throttle(username)

    conn = get_conn()
    row = conn.execute(
        "SELECT id, password_hash, status FROM users WHERE username=?",
        (username,),
    ).fetchone()
    active_hash = row["password_hash"] if row is not None and row["status"] == "active" else None
    verified = verify_password(password, active_hash or _DUMMY_PASSWORD_HASH)
    if row is None or row["status"] != "active" or not active_hash or not verified:
        _record_login_failure(username)
        raise generic_error
    _clear_login_failures(username)

    user_id = str(row["id"])
    conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (now(), user_id))
    conn.commit()
    token = create_session(user_id, user_agent=request.headers.get("user-agent"), ip=_client_ip(request))
    principal = resolve_session(token)
    if principal is None:  # pragma: no cover - 刚签发即应可解析
        raise HTTPException(500, "会话创建失败")
    payload = _profile_payload(principal)
    payload["session_token"] = token
    payload["header"] = "X-Manju-Session"
    return payload


@router.post("/logout")
def logout(token: str = Depends(require_local_session)):
    sid = token.split(".", 1)[0] if token else ""
    if sid:
        revoke_session(sid)
    return {"ok": True}


@router.get("/me")
def me(_: str = Depends(require_local_session)):
    principal = get_current_principal()
    if principal is None:  # pragma: no cover - require_local_session 失败会先 401
        raise HTTPException(401, "缺少或无效的本机会话凭证")
    return _profile_payload(principal)


@router.post("/change-password")
def change_password(body: dict, request: Request, token: str = Depends(require_local_session)):
    principal = get_current_principal()
    if principal is None:  # pragma: no cover
        raise HTTPException(401, "缺少或无效的本机会话凭证")
    if principal.user_id == "legacy-shared":
        raise HTTPException(403, "共享会话不支持改密，请先用账号登录")
    old_password = str(body.get("old_password") or "")
    new_password = str(body.get("new_password") or "")
    if len(new_password) < _MIN_NEW_PASSWORD_LEN:
        raise HTTPException(422, f"新口令至少 {_MIN_NEW_PASSWORD_LEN} 位")

    conn = get_conn()
    row = conn.execute("SELECT password_hash FROM users WHERE id=?", (principal.user_id,)).fetchone()
    if row is None or not row["password_hash"] or not verify_password(old_password, row["password_hash"]):
        raise HTTPException(401, "原口令不正确")

    ts = now()
    conn.execute(
        "UPDATE users SET password_hash=?, password_changed_at=?, must_change_password=0 WHERE id=?",
        (hash_password(new_password), ts, principal.user_id),
    )
    conn.commit()
    # 改密后原会话全部作废，重新签发一枚，避免调用方把自己挤下线。
    revoke_all_for_user(principal.user_id)
    new_token = create_session(
        principal.user_id, user_agent=request.headers.get("user-agent"), ip=_client_ip(request)
    )
    new_principal = resolve_session(new_token)
    if new_principal is None:  # pragma: no cover - 刚签发即应可解析
        raise HTTPException(500, "会话创建失败")
    payload = _profile_payload(new_principal)
    payload["session_token"] = new_token
    payload["header"] = "X-Manju-Session"
    return payload
