"""EP-02 管理面：IdP 配置 CRUD + 强制 SSO 开关（L5，见 app/LAYERS.toml::
app.sso.admin_api）。仅系统管理员可调用，不向 Agent/MCP 开放——与
``app/auth/admin_api.py``（开户）、``app/orgs/api.py``（组织治理）同一分类
口径：运维身份/安全配置，不是制作领域命令，见 ``app/capabilities/
exemptions.py`` 为本文件写的豁免项。

**新增一家 IdP 只改配置**：本文件对全部四种 ``kind`` 走同一段
``create_idp``/``update_idp`` 代码，没有按 ``kind`` 分支的 if——差异全部由
``app.sso.profiles`` 的默认值吸收（管理员不显式提供 ``scopes``/
``claim_map_json`` 时才会用到 profile 默认值）。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Body, Depends, HTTPException

from app.auth.deps import require_system_admin
from app.auth.principal import Principal, current_actor_name
from app.db import get_conn, get_setting, set_setting
from app.sso import oidc
from app.sso import provision as sso_provision
from app.sso import store as sso_store
from app.sso.profiles import profile_for

router = APIRouter(prefix="/api", dependencies=[Depends(require_system_admin)])

_VALID_POLICIES = frozenset({"enabled", "admin_only", "disabled"})
LOCAL_LOGIN_POLICY_KEY = "local_login_policy"


def _idp_public_payload(idp: dict) -> dict:
    # 界面不许在没验证的地方看起来像验证过：interop_verified/interop_note
    # 直接抄 app.sso.profiles 的诚实标注，见该模块文档"互通验证状态"一节——
    # 企业微信/飞书/钉钉三家目前是 False，只有标准 OIDC 是 True。
    profile = profile_for(idp["kind"])
    return {
        "id": idp["id"], "org_id": idp["org_id"], "kind": idp["kind"], "name": idp["name"],
        "enabled": bool(idp["enabled"]), "issuer": idp["issuer"], "client_id": idp["client_id"],
        "has_client_secret": bool(idp["client_secret_ciphertext"]),
        "discovery_url": idp["discovery_url"], "authorize_url": idp["authorize_url"],
        "token_url": idp["token_url"], "userinfo_url": idp["userinfo_url"], "jwks_url": idp["jwks_url"],
        "scopes": idp["scopes"], "claim_map_json": idp["claim_map_json"],
        "provision_json": idp["provision_json"], "allowed_domains": idp["allowed_domains"],
        "created_at": idp["created_at"], "updated_at": idp["updated_at"],
        "interop_verified": profile.interop_verified, "interop_note": profile.interop_note,
    }


@router.get("/admin/sso/providers")
def list_providers():
    conn = get_conn()
    return {"items": [_idp_public_payload(i) for i in sso_store.list_idps(conn)]}


@router.post("/admin/sso/providers")
def create_provider(body: dict = Body(...), actor: Principal = Depends(require_system_admin)):
    kind = str(body.get("kind") or "").strip()
    name = str(body.get("name") or "").strip()
    client_id = str(body.get("client_id") or "").strip()
    if not name or not client_id:
        raise HTTPException(422, "name 与 client_id 不能为空")
    try:
        profile = profile_for(kind)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    conn = get_conn()
    idp_id = sso_store.create_idp(
        conn, org_id=body.get("org_id"), kind=kind, name=name, issuer=body.get("issuer"),
        client_id=client_id, client_secret_plain=body.get("client_secret"),
        discovery_url=body.get("discovery_url"), authorize_url=body.get("authorize_url"),
        token_url=body.get("token_url"), userinfo_url=body.get("userinfo_url"),
        jwks_url=body.get("jwks_url"), scopes=str(body.get("scopes") or profile.default_scopes),
        claim_map_json=json.dumps(body.get("claim_map") or profile.default_claim_map, ensure_ascii=False),
        provision_json=json.dumps(body.get("provision") or {"auto_create": True, "on_no_match": "default"}, ensure_ascii=False),
        allowed_domains=body.get("allowed_domains"), enabled=bool(body.get("enabled", False)),
        created_by=current_actor_name(fallback=actor.username),
    )
    conn.commit()
    return _idp_public_payload(sso_store.get_idp(conn, idp_id))


@router.put("/admin/sso/providers/{idp_id}")
def update_provider(idp_id: str, body: dict = Body(...)):
    conn = get_conn()
    if sso_store.get_idp(conn, idp_id) is None:
        raise HTTPException(404, "身份提供方不存在")
    if "kind" in body:
        try:
            profile_for(str(body["kind"]))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    fields = {
        k: body[k] for k in (
            "kind", "name", "enabled", "issuer", "client_id",
            "discovery_url", "authorize_url", "token_url", "userinfo_url", "jwks_url",
            "scopes", "allowed_domains",
        ) if k in body
    }
    if "client_secret" in body:
        fields["client_secret_plain"] = body["client_secret"]
    if "claim_map" in body:
        fields["claim_map_json"] = json.dumps(body["claim_map"], ensure_ascii=False)
    if "provision" in body:
        fields["provision_json"] = json.dumps(body["provision"], ensure_ascii=False)
    sso_store.update_idp(conn, idp_id, **fields)
    conn.commit()
    return _idp_public_payload(sso_store.get_idp(conn, idp_id))


@router.delete("/admin/sso/providers/{idp_id}")
def delete_provider(idp_id: str):
    conn = get_conn()
    if sso_store.get_idp(conn, idp_id) is None:
        raise HTTPException(404, "身份提供方不存在")
    sso_store.delete_idp(conn, idp_id)
    conn.commit()
    return {"ok": True}


@router.get("/admin/sso/local-login-policy")
def get_local_login_policy():
    return {"policy": get_setting(LOCAL_LOGIN_POLICY_KEY) or "enabled"}


@router.put("/admin/sso/local-login-policy")
async def set_local_login_policy(body: dict = Body(...), actor: Principal = Depends(require_system_admin)):
    """切到 ``disabled`` 前必须先通过至少一个已启用 IdP 的真实连通性自检
    （PRD EP-02 §6）——这是唯一"配错了就没人能登录"的开关，闸门是拒绝而不是
    警告，不能靠人自觉。``enabled``/``admin_only`` 不需要自检：本地口令通道
    仍然存在（或仅收窄到管理员），配错不会把所有人锁在外面。
    """
    policy = str(body.get("policy") or "").strip()
    if policy not in _VALID_POLICIES:
        raise HTTPException(422, f"policy 必须是 {'/'.join(sorted(_VALID_POLICIES))} 之一，收到 {policy!r}")
    if policy == "disabled":
        conn = get_conn()
        enabled_idps = sso_store.list_idps(conn, enabled_only=True)
        if not enabled_idps:
            raise HTTPException(422, "没有任何已启用的身份提供方，无法切换到 disabled——切换后将没有人能登录")
        failures = []
        for idp in enabled_idps:
            ok, detail = await oidc.check_connectivity(idp)
            if not ok:
                failures.append(f"{idp['name']}（{idp['id']}）：{detail}")
        if failures:
            raise HTTPException(422, {
                "message": "SSO 连通性自检未通过，已拒绝切换到 disabled（本地口令登录保持可用）",
                "failures": failures,
            })
    set_setting(LOCAL_LOGIN_POLICY_KEY, policy)
    sso_provision.record_sso_audit(
        event="sso.local_login_policy_changed", outcome="ok", user_id=actor.user_id,
        username=actor.username, target=policy, summary=f"local_login_policy -> {policy}",
    )
    return {"policy": policy}
