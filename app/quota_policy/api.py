"""EP-04 §7/§8 REST 路由：策略/分配管理 + 用量查询（L5，见 app/LAYERS.toml::
app.quota_policy.api）。

层号如实按依赖落：需要 ``app.auth.principal``/``app.orgs.store`` 做鉴权，与
``app.orgs.api``/``app.provisioning.api`` 同一种"路由→域"入口角色，只被
``app.main`` 引用。鉴权模型照抄 ``app/orgs/api.py``：写操作要求
``_require_org_admin``（系统管理员或本组织 ``org_admin`` 角色持有者），只读
端点只要求已登录；本文件不搬迁 ``app.orgs.api`` 里那份同名私有函数为公共
helper（避免给 EP-01 那个包新增一条对外承诺的公共 API），改成本文件内一份
几乎相同的小实现。

写操作全部走本文件手写的角色校验 + ``app.quota_policy.plans``/``allocation``，
不经 Command Bus——与 ``app.orgs.api`` 同一条既有先例（组织治理端点本身是
"运维身份管理"，不是 76 条领域命令），因此同样需要在
``app/capabilities/exemptions.py`` 登记豁免。
"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Query

from app.auth.principal import Principal, current_actor_name, get_current_principal
from app.db import get_conn
from app.orgs import store as orgs_store
from app.quota_policy import plans as quota_plans
from app.quota_policy import storage as quota_storage
from app.quota_policy import usage_query
from app.quota_policy.allocation import set_allocation
from app.quota_policy.allocation import list_allocations as list_allocations_store
from app.quota_policy.usage_query import UsageQueryError

router = APIRouter(prefix="/api/system/quota")
usage_router = APIRouter(prefix="/api/system/usage")
storage_router = APIRouter(prefix="/api/system/storage")


def _principal() -> Principal:
    principal = get_current_principal()
    if principal is None:
        raise HTTPException(401, "缺少或无效的本机会话凭证")
    return principal


def _require_org_admin() -> Principal:
    """同 ``app.orgs.api._require_org_admin``：系统管理员或本组织
    ``org_admin`` 角色持有者才能改策略/分配额度。"""
    principal = _principal()
    conn = get_conn()
    is_admin = principal.is_system_admin or bool(
        principal.org_id and orgs_store.user_has_org_admin(conn, principal.user_id, principal.org_id)
    )
    if not is_admin:
        raise HTTPException(403, "该操作仅限组织管理员，请联系组织管理员代为操作")
    return principal


def _resolve_org_id(principal: Principal, org_id_param: str | None) -> str:
    """系统管理员可以查任意 org（须显式传 ``org_id``）；组织管理员/普通成员
    只能查自己所在组织，忽略/校验 ``org_id_param`` 与 ``principal.org_id`` 是
    否一致，不一致按跨组织统一 404（同 ``app.orgs.api`` 的既有口径）。"""
    if principal.is_system_admin:
        if not org_id_param:
            raise HTTPException(422, "系统管理员必须显式传 org_id")
        return org_id_param
    if not principal.org_id:
        raise HTTPException(404, "组织不存在")
    if org_id_param and org_id_param != principal.org_id:
        raise HTTPException(404, "组织不存在")
    return principal.org_id


# ---------------------------------------------------------------------------
# /api/system/quota/plans
# ---------------------------------------------------------------------------


@router.get("/plans")
def list_plans(org_id: str | None = Query(None)):
    principal = _principal()
    conn = get_conn()
    return {"items": quota_plans.list_plans(conn, _resolve_org_id(principal, org_id))}


@router.post("/plans")
def create_plan(body: dict = Body(...)):
    principal = _require_org_admin()
    org_id = str(body.get("org_id") or "").strip() or principal.org_id
    if not org_id:
        raise HTTPException(422, "org_id 不能为空（账号未归属任何组织）")
    if not principal.is_system_admin and org_id != principal.org_id:
        raise HTTPException(403, "组织管理员只能为本组织创建策略")
    key = str(body.get("key") or "").strip()
    name = str(body.get("name") or "").strip()
    if not key or not name:
        raise HTTPException(422, "策略 key 与 name 不能为空")
    limits = quota_plans.validate_limits_payload(body.get("limits") or {})
    period_days = int(body.get("period_days") or 30)
    if period_days <= 0:
        raise HTTPException(422, "period_days 必须是正整数")
    conn = get_conn()
    plan_id = quota_plans.create_plan(
        conn, org_id=org_id, key=key, name=name, limits=limits,
        period_days=period_days, created_by=current_actor_name(),
    )
    conn.commit()
    return quota_plans.get_plan(conn, plan_id)


# ---------------------------------------------------------------------------
# /api/system/quota/allocations
# ---------------------------------------------------------------------------


@router.get("/allocations")
def list_allocations(org_id: str | None = Query(None)):
    principal = _principal()
    conn = get_conn()
    resolved_org_id = _resolve_org_id(principal, org_id)
    items = list_allocations_store(conn, resolved_org_id)
    for item in items:
        item["usage"] = usage_query.usage_summary(
            conn, scope_type=item["scope_type"], scope_id=item["scope_id"],
        )["usage"]
    return {"org_id": resolved_org_id, "items": items}


def _org_scope_family(conn, org_id: str) -> list[tuple[str, str]]:
    """本组织自身 + 下属团队 + 下属用户——与 ``allocation.list_allocations`` 的
    scope 收集口径一致，供 ``/allocations``/``/alerts`` 两个端点共用。"""
    scopes: list[tuple[str, str]] = [("org", org_id)]
    scopes += [("team", t["id"]) for t in orgs_store.list_teams(conn, org_id)]
    scopes += [
        ("user", r["id"]) for r in conn.execute("SELECT id FROM users WHERE org_id=?", (org_id,)).fetchall()
    ]
    return scopes


@router.get("/alerts")
def list_alerts(org_id: str | None = Query(None)):
    """管理员首页预警横幅数据源：本组织范围内最近触发的 80%/95% 预警
    （EP-04 §7）。"""
    principal = _principal()
    conn = get_conn()
    resolved_org_id = _resolve_org_id(principal, org_id)
    items = usage_query.list_recent_alerts(conn, _org_scope_family(conn, resolved_org_id))
    return {"org_id": resolved_org_id, "items": items}


@router.put("/allocations/{scope_type}/{scope_id}")
def put_allocation(scope_type: str, scope_id: str, body: dict = Body(...)):
    principal = _require_org_admin()
    plan_id = str(body.get("plan_id") or "").strip()
    if not plan_id:
        raise HTTPException(422, "plan_id 不能为空")
    overrides = quota_plans.validate_limits_payload(body.get("overrides") or {}) if body.get("overrides") else None
    expires_at = body.get("expires_at")
    conn = get_conn()
    _assert_scope_in_principal_org(conn, principal, scope_type, scope_id)
    alloc_id = set_allocation(
        conn, scope_type=scope_type, scope_id=scope_id, plan_id=plan_id,
        overrides=overrides, expires_at=(float(expires_at) if expires_at is not None else None),
        created_by=current_actor_name(),
    )
    conn.commit()
    return {"id": alloc_id, "scope_type": scope_type, "scope_id": scope_id, "plan_id": plan_id}


def _assert_scope_in_principal_org(conn, principal: Principal, scope_type: str, scope_id: str) -> None:
    """系统管理员可以给任意 org/team/user 分配；组织管理员只能改自己组织范
    围内的 scope——跨组织一律 404（同 ``app.orgs.api`` 的既有口径），不能靠
    ``allocation.set_allocation`` 内部的存在性校验替代（那条校验只管"这个
    scope_id 存在不存在"，不管"存在于哪个组织"）。"""
    if principal.is_system_admin:
        return
    if not principal.org_id:
        raise HTTPException(404, "组织不存在")
    if scope_type == "org" and scope_id != principal.org_id:
        raise HTTPException(404, "组织不存在")
    if scope_type == "team":
        team = orgs_store.get_team(conn, scope_id)
        if team is None or team["org_id"] != principal.org_id:
            raise HTTPException(404, "团队不存在")
    if scope_type == "user":
        user_org_id = orgs_store.user_org_id(conn, scope_id)
        if user_org_id != principal.org_id:
            raise HTTPException(404, "用户不存在")


# ---------------------------------------------------------------------------
# /api/system/usage/*
# ---------------------------------------------------------------------------


@usage_router.get("/summary")
def usage_summary(
    scope: str = Query(...), id: str = Query(...),
    start: float | None = Query(None), end: float | None = Query(None),
):
    _principal()
    conn = get_conn()
    try:
        result = usage_query.usage_summary(conn, scope_type=scope, scope_id=id, start=start, end=end)
    except UsageQueryError as exc:
        raise HTTPException(422, str(exc)) from exc
    # 惰性预警：查看用量顺带核对这个 scope 自己配置的分配是否跨过 80%/95%，
    # 见 usage_query.record_alerts_from_own_allocation 文档（不在配额记账的
    # 热路径事务里做，避免写锁竞争）。
    usage_query.record_alerts_from_own_allocation(
        conn, scope_type=scope, scope_id=id, usage=result["usage"],
    )
    return result


@usage_router.get("/timeseries")
def usage_timeseries(
    scope: str = Query(...), id: str = Query(...), resource: str = Query(...),
    start: float | None = Query(None), end: float | None = Query(None),
):
    _principal()
    conn = get_conn()
    try:
        items = usage_query.usage_timeseries(
            conn, scope_type=scope, scope_id=id, resource=resource, start=start, end=end,
        )
    except UsageQueryError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"scope_type": scope, "scope_id": id, "resource": resource, "items": items}


@usage_router.get("/top")
def usage_top(dimension: str = Query(...), resource: str = Query(...), limit: int = Query(20)):
    _principal()
    conn = get_conn()
    try:
        items = usage_query.usage_top(conn, dimension=dimension, resource=resource, limit=limit)
    except UsageQueryError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"dimension": dimension, "resource": resource, "items": items}


# ---------------------------------------------------------------------------
# /api/system/storage —— EP-04 第二阶段：采样结果只读、清理候选只读、清理执
# 行需要用户确认（CLAUDE.md「超限行为：不删任何数据」+「拦住用户时必须给出
# 路」）。三个端点都要求调用方能管理这个项目（本人项目，或系统管理员，或本
# 组织 org_admin），不是任意登录用户都能看/删别人项目的存储明细。
# ---------------------------------------------------------------------------


def _assert_can_manage_project_storage(conn, principal: Principal, project_id: str) -> None:
    if principal.is_system_admin:
        return
    row = conn.execute("SELECT owner_user_id FROM projects WHERE id=?", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "项目不存在")
    owner_user_id = row["owner_user_id"]
    if owner_user_id == principal.user_id:
        return
    owner_org_id = orgs_store.user_org_id(conn, owner_user_id) if owner_user_id else None
    if (
        principal.org_id and owner_org_id == principal.org_id
        and orgs_store.user_has_org_admin(conn, principal.user_id, principal.org_id)
    ):
        return
    raise HTTPException(403, "无权访问该项目的存储信息")


@storage_router.get("/sample")
def storage_sample(project_id: str = Query(...)):
    principal = _principal()
    conn = get_conn()
    _assert_can_manage_project_storage(conn, principal, project_id)
    sample = quota_storage.latest_sample(conn, project_id)
    return {"project_id": project_id, "sample": sample}


@storage_router.get("/cleanup_candidates")
def storage_cleanup_candidates(project_id: str = Query(...), limit: int = Query(20)):
    principal = _principal()
    conn = get_conn()
    _assert_can_manage_project_storage(conn, principal, project_id)
    items = quota_storage.cleanup_candidates(conn, project_id, limit)
    return {"project_id": project_id, "items": items}


@storage_router.post("/cleanup")
def storage_cleanup(body: dict = Body(...)):
    principal = _principal()
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        raise HTTPException(422, "project_id 不能为空")
    version_ids = body.get("version_ids") or []
    if not isinstance(version_ids, list) or not all(isinstance(v, str) for v in version_ids):
        raise HTTPException(422, "version_ids 必须是字符串数组")
    conn = get_conn()
    _assert_can_manage_project_storage(conn, principal, project_id)
    return quota_storage.execute_cleanup(conn, project_id, version_ids, actor=current_actor_name())
