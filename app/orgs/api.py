"""EP-01 §9 REST 路由：组织/团队/角色/权限点目录/项目授权（L5）。

层号如实按依赖落：本文件需要 ``app.authz.catalog.build_permission_catalog()``
（L5，读运行时 Command Registry）做 422 校验的合法值来源，因此不能是 L2——
与 ``app/authz/catalog.py``/``app/orgs/bootstrap.py`` 顶部文档同一条理由。

挂载方式与 ``app/api.py``/``app/system_api.py`` 等其它"路由→域"入口一致：
只被 ``app.main`` 引用，挂在 ``_PROJECT_OWNER_DEPS``（``require_local_session``
+ ``require_project_owner_access``）之上——后者按路径参数识别归属，本文件
除 ``/projects/{project_id}/grants*`` 外的路由都不带它认识的路径参数，因此
对它们是无操作的直通（等价于只要求登录）。

鉴权模型（EP-01 §9 的 ``[org_admin]`` 标注）分三档，全部在本文件内手写，不
经 Command Bus——组织治理端点本身就是"运维身份管理"，与 76 条领域命令是
两类东西（同 ``app/auth/admin_api.py`` 用 ``require_system_admin`` 而不是
``ui_route`` 的分类口径）：

1. 只读端点（``GET /orgs/current``/``/teams``/``/roles``/``/permissions``）：
   只要求已登录（路由挂载点保证），不额外要求角色。
2. 团队/角色的写端点：要求 ``_require_org_admin``——系统管理员或本组织
   ``org_admin`` 角色持有者；跨组织对象（``team.org_id``/``role.org_id``
   不属于调用者所在组织）一律 404，与 EP-01 §8 的"跨组织访问统一 404"一致。
3. 项目授权（grants）的写端点：要求 ``_require_project_manage_access``——
   项目实际所有者、本组织 org_admin 或系统管理员；GET 只要求"这个项目对你
   可见"（已由挂载点的 ``require_project_owner_access`` 保证，同一逻辑不再
   重复）。

写操作全部走本文件手写的角色校验 + ``app.orgs.service``，不经 Command
Bus——因此每一条 mutating 路由都必须在 ``app/capabilities/exemptions.py``
里显式登记豁免原因，满足 ``app/capabilities/coverage.py`` 的覆盖扫描（详见
该文件里新增的"EP-01 组织/团队/角色/项目授权 REST"一段）。
"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from app.auth.principal import Principal, current_actor_name, get_current_principal
from app.authz.catalog import build_permission_catalog
from app.db import get_conn
from app.orgs import service as orgs_service
from app.orgs import store as orgs_store

router = APIRouter(prefix="/api")


# ---------------------------------------------------------------------------
# 鉴权 helper：全部手写，不经 Command Bus（见模块文档）。
# ---------------------------------------------------------------------------


def _principal() -> Principal:
    principal = get_current_principal()
    if principal is None:
        raise HTTPException(401, "缺少或无效的本机会话凭证")
    return principal


def _is_org_admin(conn, principal: Principal) -> bool:
    return principal.is_system_admin or bool(
        principal.org_id and orgs_store.user_has_org_admin(conn, principal.user_id, principal.org_id)
    )


def _require_org_admin() -> Principal:
    principal = _principal()
    conn = get_conn()
    if not _is_org_admin(conn, principal):
        raise HTTPException(403, "该操作仅限组织管理员，请联系组织管理员代为操作")
    return principal


def _team_in_scope_or_404(conn, team_id: str, principal: Principal) -> dict:
    team = orgs_store.get_team(conn, team_id)
    if team is None or (not principal.is_system_admin and team["org_id"] != principal.org_id):
        raise HTTPException(404, "团队不存在")
    return team


def _role_in_scope_or_404(conn, role_id: str, principal: Principal) -> dict:
    role = orgs_store.get_role(conn, role_id)
    if role is None:
        raise HTTPException(404, "角色不存在")
    # org_id IS NULL = 全局内置模板，任何组织都看得见；自定义角色只对本组织可见。
    if role["org_id"] is not None and not principal.is_system_admin and role["org_id"] != principal.org_id:
        raise HTTPException(404, "角色不存在")
    return role


def _require_project_manage_access(project_id: str) -> Principal:
    """管理项目授权（grants 的写操作）：项目所有者、本组织 org_admin 或系统
    管理员。挂载点的 ``require_project_owner_access`` 已经保证走到这里的调用
    者至少"看得见"这个项目（owner/project_grants/org_admin 任一命中），这里
    只再收紧到"能不能替这个项目发新的授权"——看得见但不是所有者/管理员的人
    （比如被授予 viewer 的协作者）应该 403，而不是 404（对象存在，角色不够）。
    """
    principal = _principal()
    if principal.is_system_admin:
        return principal
    conn = get_conn()
    row = conn.execute("SELECT owner_user_id, org_id FROM projects WHERE id=?", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "项目不存在")
    if row["owner_user_id"] == principal.user_id:
        return principal
    org_id = row["org_id"]
    if org_id and org_id == principal.org_id and orgs_store.user_has_org_admin(conn, principal.user_id, org_id):
        return principal
    raise HTTPException(403, "只有项目所有者或组织管理员可以管理项目授权")


def _validate_permission_keys(permission_keys: frozenset[str]) -> None:
    """service 层不校验权限点是否在目录内（见 app/orgs/service.py 模块文档：
    catalog.py 是 L5，service.py 是 L2，结构上够不着）——422 校验补在这里，
    合法值从 build_permission_catalog() 现算，不写第二张名单。
    """
    catalog = build_permission_catalog()
    invalid = sorted(k for k in permission_keys if k not in catalog)
    if invalid:
        raise HTTPException(422, f"未知权限点：{', '.join(invalid)}")


# ---------------------------------------------------------------------------
# 序列化 helper
# ---------------------------------------------------------------------------


def _permission_point_payload(point) -> dict:
    return {
        "key": point.key, "title": point.title, "risk": point.risk,
        "scopes": list(point.scopes), "side_effect": point.side_effect,
        "admin_only": point.admin_only, "tags": list(point.tags),
    }


def _catalog_payload() -> list[dict]:
    catalog = build_permission_catalog()
    return [_permission_point_payload(catalog[key]) for key in sorted(catalog)]


def _role_payload(conn, role: dict) -> dict:
    permission_keys = orgs_store.list_role_permission_keys(conn, role["id"])
    return {
        "id": role["id"], "org_id": role["org_id"], "key": role["key"],
        "name": role["name"], "description": role["description"],
        "builtin": bool(role["builtin"]), "permission_keys": sorted(permission_keys),
    }


def _team_payload(conn, team: dict) -> dict:
    members = orgs_store.list_team_members(conn, team["id"])
    return {
        "id": team["id"], "org_id": team["org_id"], "name": team["name"],
        "description": team["description"], "status": team["status"],
        "members": [
            {"user_id": m["user_id"], "role_id": m["role_id"], "created_at": m["created_at"]}
            for m in members
        ],
    }


def _grant_payload(grant: dict) -> dict:
    return {
        "project_id": grant["project_id"], "subject_type": grant["subject_type"],
        "subject_id": grant["subject_id"], "role_id": grant["role_id"],
        "created_at": grant["created_at"], "expires_at": grant["expires_at"],
    }


# ---------------------------------------------------------------------------
# /api/orgs/current
# ---------------------------------------------------------------------------


@router.get("/orgs/current")
def get_current_org():
    principal = _principal()
    conn = get_conn()
    org = orgs_store.get_org(conn, principal.org_id) if principal.org_id else None
    teams = []
    for team_id in sorted(principal.team_ids):
        team = orgs_store.get_team(conn, team_id)
        if team is None:
            continue
        role_id = next(
            (m["role_id"] for m in orgs_store.list_team_members(conn, team_id) if m["user_id"] == principal.user_id),
            None,
        )
        role = orgs_store.get_role(conn, role_id) if role_id else None
        teams.append({
            "team_id": team_id, "team_name": team["name"],
            "role_id": role_id, "role_name": role["name"] if role else None,
        })
    return {
        "org": org,
        "is_system_admin": principal.is_system_admin,
        "is_org_admin": _is_org_admin(conn, principal),
        "role_governed": principal.role_governed,
        "teams": teams,
        "permission_keys": sorted(principal.permission_keys),
    }


# ---------------------------------------------------------------------------
# /api/teams
# ---------------------------------------------------------------------------


@router.get("/teams")
def list_teams():
    principal = _principal()
    conn = get_conn()
    if not principal.org_id:
        return {"items": []}
    teams = orgs_store.list_teams(conn, principal.org_id)
    if not _is_org_admin(conn, principal):
        teams = [t for t in teams if t["id"] in principal.team_ids]
    return {"items": [_team_payload(conn, t) for t in teams]}


@router.post("/teams")
def create_team(body: dict = Body(...)):
    principal = _require_org_admin()
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(422, "团队名称不能为空")
    conn = get_conn()
    team_id = orgs_service.create_team(
        org_id=principal.org_id, name=name,
        description=(body.get("description") or None), created_by=current_actor_name(),
    )
    return _team_payload(conn, orgs_store.get_team(conn, team_id))


@router.put("/teams/{team_id}")
def update_team(team_id: str, body: dict = Body(...)):
    principal = _require_org_admin()
    conn = get_conn()
    _team_in_scope_or_404(conn, team_id, principal)
    orgs_service.update_team(
        team_id=team_id,
        name=(str(body["name"]).strip() if "name" in body else None),
        description=(body["description"] if "description" in body else None),
        status=(str(body["status"]) if "status" in body else None),
    )
    return _team_payload(conn, orgs_store.get_team(conn, team_id))


@router.post("/teams/{team_id}/members")
def add_team_members(team_id: str, body: list[dict] = Body(...)):
    principal = _require_org_admin()
    conn = get_conn()
    _team_in_scope_or_404(conn, team_id, principal)
    members: list[tuple[str, str]] = []
    for entry in body:
        user_id = str(entry.get("user_id") or "").strip()
        role_id = str(entry.get("role_id") or "").strip()
        if not user_id or not role_id:
            raise HTTPException(422, "每条成员记录都必须包含 user_id 与 role_id")
        members.append((user_id, role_id))
    orgs_service.add_team_members(team_id=team_id, members=members, created_by=current_actor_name())
    return _team_payload(conn, orgs_store.get_team(conn, team_id))


@router.delete("/teams/{team_id}/members/{user_id}")
def remove_team_member(team_id: str, user_id: str):
    principal = _require_org_admin()
    conn = get_conn()
    _team_in_scope_or_404(conn, team_id, principal)
    orgs_service.remove_team_member(team_id=team_id, user_id=user_id)
    return _team_payload(conn, orgs_store.get_team(conn, team_id))


# ---------------------------------------------------------------------------
# /api/roles ＋ /api/permissions
# ---------------------------------------------------------------------------


@router.get("/roles")
def list_roles():
    principal = _principal()
    conn = get_conn()
    org_id = principal.org_id or orgs_store.ORG_DEFAULT_ID
    roles = orgs_store.list_roles(conn, org_id)
    return {
        "items": [_role_payload(conn, r) for r in roles],
        "permission_catalog": _catalog_payload(),
    }


@router.get("/permissions")
def list_permissions():
    _principal()
    return {"items": _catalog_payload()}


@router.post("/roles")
def create_role(body: dict = Body(...)):
    principal = _require_org_admin()
    key = str(body.get("key") or "").strip()
    name = str(body.get("name") or "").strip()
    if not key or not name:
        raise HTTPException(422, "角色 key 与 name 不能为空")
    from_template = body.get("from_template")
    base_keys: frozenset[str] = frozenset()
    if from_template:
        from app.authz.catalog import build_builtin_role_permissions

        try:
            base_keys = build_builtin_role_permissions(str(from_template))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    extra_keys = frozenset(body.get("permission_keys") or [])
    permission_keys = base_keys | extra_keys
    if not permission_keys and not from_template:
        raise HTTPException(422, "必须提供 permission_keys 或 from_template 之一")
    _validate_permission_keys(permission_keys)
    conn = get_conn()
    role_id = orgs_service.create_custom_role(
        org_id=principal.org_id, key=key, name=name,
        description=(body.get("description") or None),
        permission_keys=permission_keys, created_by=current_actor_name(),
    )
    return _role_payload(conn, orgs_store.get_role(conn, role_id))


@router.put("/roles/{role_id}")
def update_role(role_id: str, body: dict = Body(...)):
    principal = _require_org_admin()
    conn = get_conn()
    _role_in_scope_or_404(conn, role_id, principal)
    permission_keys = frozenset(body.get("permission_keys") or [])
    _validate_permission_keys(permission_keys)
    orgs_service.update_role_permissions(role_id=role_id, permission_keys=permission_keys)
    return _role_payload(conn, orgs_store.get_role(conn, role_id))


@router.delete("/roles/{role_id}")
def delete_role(role_id: str):
    principal = _require_org_admin()
    conn = get_conn()
    _role_in_scope_or_404(conn, role_id, principal)
    orgs_service.delete_role(role_id=role_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# /api/projects/{project_id}/grants
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/grants")
def list_project_grants(project_id: str):
    # 挂载点的 require_project_owner_access 已经保证"这个项目对你可见"；
    # 看得见即可读取授权列表，不额外要求 org_admin（透明是协作场景的基本要求）。
    _principal()
    conn = get_conn()
    return {"items": [_grant_payload(g) for g in orgs_store.list_project_grants(conn, project_id)]}


@router.post("/projects/{project_id}/grants")
def create_project_grant(project_id: str, body: dict = Body(...)):
    _require_project_manage_access(project_id)
    subject_type = str(body.get("subject_type") or "").strip()
    subject_id = str(body.get("subject_id") or "").strip()
    role_id = str(body.get("role_id") or "").strip()
    if not subject_id or not role_id:
        raise HTTPException(422, "subject_id 与 role_id 不能为空")
    expires_at = body.get("expires_at")
    orgs_service.grant_project_access(
        project_id=project_id, subject_type=subject_type, subject_id=subject_id,
        role_id=role_id, created_by=current_actor_name(),
        expires_at=(float(expires_at) if expires_at is not None else None),
    )
    conn = get_conn()
    return {"items": [_grant_payload(g) for g in orgs_store.list_project_grants(conn, project_id)]}


@router.delete("/projects/{project_id}/grants/{subject_type}/{subject_id}")
def delete_project_grant(project_id: str, subject_type: str, subject_id: str):
    _require_project_manage_access(project_id)
    revoked = orgs_service.revoke_project_access(
        project_id=project_id, subject_type=subject_type, subject_id=subject_id,
    )
    if not revoked:
        raise HTTPException(404, "该授权不存在")
    return {"ok": True}
