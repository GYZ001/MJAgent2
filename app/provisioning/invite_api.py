"""EP-03 §6 邀请链接：公开接受流程（``/api/invite/*``，L5，见
app/LAYERS.toml::app.provisioning.invite_api）。

与 ``app.provisioning.api`` 分开成独立文件——那边全部端点要求
``require_system_admin``，这两条（预览 + 接受）恰恰相反，必须在**没有任何
会话**的情况下可调用（与 ``POST /api/auth/login`` 同一种"鉴权入口本身"，见
``app/capabilities/exemptions.py`` 里的登记）。挂
``assert_session_bootstrap_allowed``（与登录/SSO 会话交接同一道 Origin/CSRF
闸门），不挂 ``require_local_session``。

**token 走请求体，不走 URL 路径**（``POST /invite/preview`` / ``POST
/invite/accept``，不是 ``GET /invite/{token}`` / ``POST
/invite/{token}/accept``）——与 ``POST /api/auth/sso/exchange`` 同一条已有教训
（见该路由在 ``app/capabilities/exemptions.py`` 里的登记："会话交接不能走
查询串，否则真令牌会明文落进 nginx access log/浏览器历史/Referer"）。这里
是同一类一次性凭证，同一个理由：路径参数还会被
``app.audit.recorder._build_http_row`` 的 HTTP 级审计行自动落进
``operation_audit.path``，即便 ``target``/``args_json`` 都做了脱敏也挡不住
——唯一安全的做法是token压根不出现在 URL 里。前端页面地址本身仍是
``/invite/{token}``（邀请链接本就要靠 token 定位到人，这条不可避免，且只通过
带外渠道分发），但页面加载后发起的这两个 API 调用把 token 挪进 body。
"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Request

from app.local_session import assert_session_bootstrap_allowed
from app.provisioning import invitations as invitations_domain

router = APIRouter(prefix="/api/invite", tags=["invitations"])


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _require_token(body: dict) -> str:
    token = str(body.get("token") or "")
    if not token:
        raise HTTPException(422, "token 不能为空")
    return token


@router.post("/preview")
def preview_invitation(request: Request, body: dict = Body(...)):
    """接受页在用户输入口令前先展示"这个邀请是谁的、还有效吗"。"""
    assert_session_bootstrap_allowed(request)
    return invitations_domain.get_invitation_preview(_require_token(body))


@router.post("/accept")
def accept_invitation(request: Request, body: dict = Body(...)):
    """设口令 → 建账号（按邀请预置的团队/角色）→ 直接登录。密码策略在领域
    函数里校验，不满足时抛 422 并带上具体哪条不满足（见
    ``app.auth.password_policy.check_strength``）。"""
    assert_session_bootstrap_allowed(request)
    token = _require_token(body)
    password = str(body.get("password") or "")
    if not password:
        raise HTTPException(422, "password 不能为空")
    return invitations_domain.accept_invitation(
        token, password=password,
        user_agent=request.headers.get("user-agent"), ip=_client_ip(request),
    )
