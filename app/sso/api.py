"""EP-02 §4 OIDC 登录流程 REST 路由（L5，见 app/LAYERS.toml::app.sso.api）。

PRD EP-02 §4 的五条路由 + 绑定/解绑：``GET providers`` / ``GET {idp_id}/start``
/ ``GET {idp_id}/callback`` / ``POST link`` / ``DELETE link/{idp_id}``，另加
``POST break-glass``（PRD §6 强制 SSO 下的应急本地登录通道）。

``POST /auth/sso/link`` 与 PRD 字面描述有一处实现差异（交付报告"与 PRD 不符
的现实"一节已记录）：不是直接 302 跳转，而是返回 ``{"authorize_url": ...}``
JSON，由前端 JS 自行 ``window.location.href = authorize_url`` 跳转——一个
带 body 的 POST 与"发起跳转"在语义上更适合做成"要跳到哪里"的 JSON 查询，
浏览器对 POST 的跨源 302 处理也不如 GET 直观。``start``/``callback`` 仍是
PRD 描述的真实 302。

**会话交接不走查询串**（2026-09-12 修复）：登录成功后回跳 URL 只带一枚一次性
交换码 ``?sso_code=``（60 秒 TTL，单次消费，只存 ``sha256(code)``），真正的
会话令牌只在 ``POST /auth/sso/exchange`` 的**响应体**里返回。理由：查询串
会被公网入口 nginx 的 ``combined`` access log 完整记录并随 logrotate 长期
保留，也会留在浏览器历史与可能的 Referer 里——如果直接把真会话令牌塞进 302
的查询串，等于把一枚 7 天滑动有效的凭证同时暴露在三个持久化/半持久化的地方。
前端拿到 ``sso_code`` 后应立即用 ``history.replaceState`` 清掉地址栏，但那
只是纵深防御的最外层，不是唯一防线——真正的防线是响应体从不进日志。

不经 Command Bus（与 ``POST /api/auth/login`` 同一分类口径，见
``app/capabilities/exemptions.py`` 里为本文件写的豁免项）：登录/绑定/解绑
都是鉴权入口本身，不是制作领域命令。
"""
from __future__ import annotations

import hashlib
import os
import secrets

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.auth.principal import get_current_principal
from app.auth.sessions import create_session, revoke_all_for_user
from app.db import get_conn
from app.local_session import assert_session_bootstrap_allowed, require_local_session
from app.sso import oidc, oidc_verify, provision
from app.sso import store as sso_store
from app.sso.profiles import profile_for

router = APIRouter(prefix="/api")


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _public_base_url(request: Request) -> str:
    """OIDC ``redirect_uri`` 必须是预先在 IdP 侧登记好的固定绝对地址；反代
    拓扑下 ``request.base_url`` 可能因 X-Forwarded-* 未透传而给出错误的
    scheme/host，与 ``app.payments.config.public_base_url`` 同一问题，同一
    解法：优先信任显式环境变量，缺省才退回请求自带的地址（本地开发/测试）。
    """
    override = os.environ.get("SSO_PUBLIC_BASE_URL", "").strip()
    if override:
        return override.rstrip("/")
    return str(request.base_url).rstrip("/")


def _safe_redirect_target(value: str | None) -> str:
    """只允许本站相对路径；否则是开放重定向（PRD EP-02 §10 陷阱 3）。"""
    text = (value or "/").strip()
    if not text.startswith("/") or text.startswith("//") or "://" in text:
        return "/"
    return text


def _append_query(url: str, **params: str) -> str:
    sep = "&" if "?" in url else "?"
    from urllib.parse import urlencode

    return f"{url}{sep}{urlencode(params)}"


async def _begin_authorize(idp_id: str, *, redirect_to: str, link_user_id: str | None, request: Request) -> str:
    conn = get_conn()
    idp_row = sso_store.get_idp(conn, idp_id)
    if idp_row is None or not idp_row["enabled"]:
        raise HTTPException(404, "身份提供方不存在或未启用")
    endpoints = await oidc.resolve_endpoints(idp_row)
    if not endpoints["authorize_url"]:
        raise HTTPException(422, "该身份提供方尚未配置 authorize_url（或 discovery_url）")
    code_verifier, code_challenge = oidc.generate_pkce()
    nonce = secrets.token_urlsafe(24)
    state = sso_store.create_auth_request(
        conn, idp_id=idp_id, nonce=nonce, code_verifier=code_verifier,
        redirect_to=redirect_to, link_user_id=link_user_id, ip=_client_ip(request),
    )
    conn.commit()
    redirect_uri = f"{_public_base_url(request)}/api/auth/sso/{idp_id}/callback"
    return oidc.build_authorize_url(
        idp_row, endpoints, redirect_uri=redirect_uri, state=state, nonce=nonce, code_challenge=code_challenge,
    )


async def _exchange_token(idp_row: dict, auth_req: dict, *, code: str, request: Request, endpoints: dict) -> dict:
    idp_id = idp_row["id"]
    client_secret = None
    if idp_row["client_secret_ciphertext"]:
        client_secret = sso_store.decrypt_client_secret(
            idp_id, bytes(idp_row["client_secret_nonce"]), bytes(idp_row["client_secret_ciphertext"]),
        )
    redirect_uri = f"{_public_base_url(request)}/api/auth/sso/{idp_id}/callback"
    try:
        return await oidc.exchange_code(
            endpoints, client_id=idp_row["client_id"], client_secret=client_secret,
            code=code, redirect_uri=redirect_uri, code_verifier=auth_req["code_verifier"],
        )
    except oidc.OidcTransportError as exc:
        provision.record_sso_audit(
            event="sso.login_failed", outcome="failed", target=idp_id,
            error_code="token_exchange_failed", summary=str(exc),
        )
        raise HTTPException(502, f"向身份提供方换取令牌失败：{exc}") from exc


async def _resolve_raw_claims(idp_row: dict, auth_req: dict, endpoints: dict, token_resp: dict) -> dict:
    idp_id = idp_row["id"]
    profile = profile_for(idp_row["kind"])
    if not profile.requires_id_token:
        access_token = token_resp.get("access_token")
        if not access_token:
            raise HTTPException(502, "身份提供方未返回 access_token")
        try:
            return await oidc.fetch_userinfo(idp_row, endpoints, access_token)
        except oidc.OidcTransportError as exc:
            provision.record_sso_audit(
                event="sso.login_failed", outcome="failed", target=idp_id,
                error_code="userinfo_failed", summary=str(exc),
            )
            raise HTTPException(502, f"获取用户信息失败：{exc}") from exc
    id_token = token_resp.get("id_token")
    if not id_token:
        raise HTTPException(502, "身份提供方未返回 id_token")
    try:
        return await oidc.verify_id_token(id_token, idp_row=idp_row, endpoints=endpoints, nonce=auth_req["nonce"])
    except oidc_verify.IdTokenError as exc:
        provision.record_sso_audit(
            event="sso.login_failed", outcome="rejected", target=idp_id,
            error_code="id_token_invalid", summary=str(exc),
        )
        raise HTTPException(401, f"id_token 校验失败：{exc}") from exc


async def _exchange_and_get_claims(idp_row: dict, auth_req: dict, *, code: str, request: Request):
    idp_id = idp_row["id"]
    endpoints = await oidc.resolve_endpoints(idp_row)
    token_resp = await _exchange_token(idp_row, auth_req, code=code, request=request, endpoints=endpoints)
    claims_raw = await _resolve_raw_claims(idp_row, auth_req, endpoints, token_resp)
    try:
        return provision.normalize_claims(idp_row, claims_raw)
    except ValueError as exc:
        provision.record_sso_audit(
            event="sso.login_failed", outcome="rejected", target=idp_id,
            error_code="claims_invalid", summary=str(exc),
        )
        raise HTTPException(401, str(exc)) from exc


def _finish_link(conn, *, link_user_id: str, idp_id: str, claims, redirect_to: str) -> RedirectResponse:
    try:
        sso_store.link_identity(conn, user_id=link_user_id, idp_id=idp_id, external_subject=claims.subject)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    conn.commit()
    provision.record_sso_audit(
        event="sso.link", outcome="ok", user_id=link_user_id, target=claims.subject, summary=f"idp={idp_id}",
    )
    return RedirectResponse(redirect_to, status_code=302)


def _finish_login(conn, *, idp_row: dict, claims, redirect_to: str) -> RedirectResponse:
    try:
        result = provision.provision_or_login(idp_row, claims)
    except provision.NoAccountError as exc:
        raise HTTPException(403, {"code": "no_account", "message": "你的账号尚未开通，请联系管理员为你开通后再登录"}) from exc
    except provision.ProvisionRejectedError as exc:
        raise HTTPException(403, {"code": "provision_rejected", "message": "你的身份未匹配任何可用的团队/角色映射规则，请联系管理员"}) from exc

    code = secrets.token_urlsafe(32)
    sso_store.create_login_exchange(conn, user_id=result.user_id, code_hash=hashlib.sha256(code.encode("utf-8")).hexdigest())
    conn.commit()
    user_row = conn.execute("SELECT username FROM users WHERE id=?", (result.user_id,)).fetchone()
    provision.record_sso_audit(
        event="sso.login_success", outcome="ok", user_id=result.user_id,
        username=user_row["username"] if user_row else None, target=claims.subject,
        summary=f"idp={idp_row['id']} created={result.created} team_id={result.team_id} role_id={result.role_id}",
    )
    # 真会话令牌不进这条 URL——见模块文档"会话交接不走查询串"。这里只带一枚
    # 一次性交换码，前端用它调 POST /auth/sso/exchange 换取响应体里的令牌。
    target = _append_query(redirect_to, sso_code=code)
    return RedirectResponse(target, status_code=302)


@router.get("/auth/sso/providers")
def list_providers():
    conn = get_conn()
    items = sso_store.list_idps(conn, enabled_only=True)
    return {"items": [{"id": i["id"], "name": i["name"], "kind": i["kind"]} for i in items]}


@router.get("/auth/sso/{idp_id}/start")
async def sso_start(idp_id: str, request: Request, redirect_to: str = "/"):
    authorize_url = await _begin_authorize(
        idp_id, redirect_to=_safe_redirect_target(redirect_to), link_user_id=None, request=request,
    )
    return RedirectResponse(authorize_url, status_code=302)


@router.get("/auth/sso/{idp_id}/callback")
async def sso_callback(idp_id: str, request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    if error:
        raise HTTPException(400, f"身份提供方返回错误：{error}")
    if not code or not state:
        raise HTTPException(400, "回调缺少 code 或 state 参数")
    conn = get_conn()
    idp_row = sso_store.get_idp(conn, idp_id)
    if idp_row is None or not idp_row["enabled"]:
        raise HTTPException(404, "身份提供方不存在或未启用")
    auth_req = sso_store.consume_auth_request(conn, state)
    # 立即提交：consume_auth_request 的 UPDATE 即使 0 行命中也会在这个连接上
    # 开一个隐式写事务，不马上提交会一直攥着写锁，直到本请求后面某处 commit
    # 或整个请求结束——期间任何独立开连接的写入（比如下面 record_sso_audit
    # 走 app.audit.store 的独立连接）都会因抢不到写锁而静默失败并落入
    # monitor_audit_buffer 兜底（CLAUDE.md「不得在调用方的连接上隐式提交」
    # 的反面教训：这里恰恰是"忘记提交"而不是"提交了不该提交的"）。state 的
    # 一次性消费本身就是独立的原子动作，不需要等后续步骤一起提交。
    conn.commit()
    if auth_req is None or auth_req["idp_id"] != idp_id:
        provision.record_sso_audit(
            event="sso.login_failed", outcome="rejected", target=idp_id,
            error_code="state_replayed_or_expired", summary="state 不存在、已被消费、已过期或与 idp_id 不匹配",
        )
        raise HTTPException(400, "登录请求已失效（state 重放或过期），请重新发起登录")

    claims = await _exchange_and_get_claims(idp_row, auth_req, code=code, request=request)
    redirect_to = auth_req["redirect_to"] or "/"
    if auth_req["link_user_id"]:
        return _finish_link(conn, link_user_id=auth_req["link_user_id"], idp_id=idp_id, claims=claims, redirect_to=redirect_to)
    return _finish_login(conn, idp_row=idp_row, claims=claims, redirect_to=redirect_to)


@router.post("/auth/sso/exchange")
def sso_exchange(body: dict, request: Request):
    """把 302 跳转里的一次性交换码换成真会话令牌——见模块文档"会话交接不走
    查询串"。与 ``POST /api/auth/login`` 同一类"签发会话前"闸门：会话尚不
    存在，用 Origin/CSRF 闸门代替。交换码重放或过期一律 400，不区分具体
    原因（同 ``sso_callback`` 对 state 的处理口径）。
    """
    assert_session_bootstrap_allowed(request)
    code = str(body.get("code") or "").strip()
    if not code:
        raise HTTPException(422, "code 不能为空")
    row = sso_store.consume_login_exchange(code_hash=hashlib.sha256(code.encode("utf-8")).hexdigest())
    if row is None:
        raise HTTPException(400, "交换码无效、已使用或已过期，请重新发起登录")
    token = create_session(row["user_id"], user_agent=request.headers.get("user-agent"), ip=_client_ip(request))
    return {"session_token": token, "header": "X-Manju-Session"}


@router.get("/auth/sso/my-identities")
def list_my_identities(_: str = Depends(require_local_session)):
    """已登录用户当前绑定的全部 IdP 身份（2026-09-12 前端接入个人设置的
    绑定/解绑界面时发现的契约缺口补上）：``POST .../link`` 只知道要跳去哪，
    ``DELETE .../link/{idp_id}`` 只知道要解绑哪个，两者都回答不了"我现在
    绑了哪些"——没有这个读接口，界面就只能对绑定状态说谎或干脆不显示。"""
    principal = get_current_principal()
    conn = get_conn()
    items = []
    for identity in sso_store.list_identities_for_user(conn, principal.user_id):
        idp = sso_store.get_idp(conn, identity["idp_id"])
        items.append({
            "idp_id": identity["idp_id"],
            "idp_name": idp["name"] if idp else identity["idp_id"],
            "idp_kind": idp["kind"] if idp else None,
            "linked_at": identity["linked_at"],
            "last_login_at": identity["last_login_at"],
        })
    return {"items": items}


@router.post("/auth/sso/link")
async def sso_link_start(request: Request, body: dict = Body(default={}), _: str = Depends(require_local_session)):
    principal = get_current_principal()
    idp_id = str(body.get("idp_id") or "").strip()
    if not idp_id:
        raise HTTPException(422, "idp_id 不能为空")
    redirect_to = _safe_redirect_target(body.get("redirect_to"))
    authorize_url = await _begin_authorize(idp_id, redirect_to=redirect_to, link_user_id=principal.user_id, request=request)
    return {"authorize_url": authorize_url}


@router.delete("/auth/sso/link/{idp_id}")
def sso_unlink(idp_id: str, _: str = Depends(require_local_session)):
    principal = get_current_principal()
    conn = get_conn()
    if sso_store.login_method_count(conn, principal.user_id) <= 1:
        raise HTTPException(422, "这是你唯一的登录方式，解绑后将无法登录；请先设置本地口令或绑定另一个身份提供方")
    removed = sso_store.unlink_identity(conn, user_id=principal.user_id, idp_id=idp_id)
    conn.commit()
    if not removed:
        raise HTTPException(404, "未绑定该身份提供方")
    provision.record_sso_audit(event="sso.unlink", outcome="ok", user_id=principal.user_id, username=principal.username, target=idp_id)
    return {"ok": True}


@router.post("/auth/sso/break-glass")
def break_glass_login(body: dict, request: Request):
    """PRD EP-02 §6 应急通道：一次性恢复码由 ``scripts/break_glass_login.py``
    在服务器 shell 上生成（需要服务器访问权限，见该脚本 docstring），仅限
    系统管理员账号；用后强制 ``must_change_password`` 并写审计。"""
    username = str(body.get("username") or "").strip()
    code = str(body.get("code") or "").strip()
    if not username or not code:
        raise HTTPException(422, "username 与 code 不能为空")
    generic_error = HTTPException(401, "恢复码无效或已过期")
    conn = get_conn()
    row = conn.execute(
        "SELECT id, is_system_admin, status FROM users WHERE username=?", (username,)
    ).fetchone()
    if row is None or not row["is_system_admin"] or row["status"] != "active":
        raise generic_error
    code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
    if not sso_store.consume_break_glass_code(user_id=row["id"], code_hash=code_hash):
        provision.record_sso_audit(
            event="sso.break_glass_login", outcome="rejected", target=username,
            error_code="invalid_or_expired_code",
        )
        raise generic_error
    conn.execute("UPDATE users SET must_change_password=1 WHERE id=?", (row["id"],))
    conn.commit()
    revoke_all_for_user(row["id"])
    token = create_session(row["id"], user_agent=request.headers.get("user-agent"), ip=_client_ip(request))
    provision.record_sso_audit(
        event="sso.break_glass_login", outcome="ok", user_id=row["id"], username=username,
        summary="应急本地登录通道已使用，已强制标记 must_change_password",
    )
    return {"session_token": token, "header": "X-Manju-Session", "must_change_password": True}
