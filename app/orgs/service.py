"""EP-01 组织/团队/成员/角色/授权业务动作（L2，见 app/LAYERS.toml::app.orgs）。

每个函数代表一次完整业务动作，自己用 ``get_conn()`` 取本线程/任务局部连接并
显式 ``commit()``（与 ``app/auth/sessions.py::create_session`` 同一惯例）——
调用方不需要、也不应该传入自己的连接，这类"动作"天然各自是独立事务，不存在
"复用调用方连接"的场景，因此与 ``app.orgs.store``（每个函数把 ``conn`` 当
必填参数、由调用方决定事务边界）刻意采用不同约定。

不做 ``app.authz.catalog`` 的权限点合法性校验（PUT /api/roles 的 422 校验是
REST 路由那一批工作，见 EP-01 §9"本阶段不做"）；这里只做结构性校验（内置
模板不可删、不可改权限点；subject_type 只能是 user/team）。
"""
from __future__ import annotations

from fastapi import HTTPException

from app.db import get_conn
from app.orgs import store


def create_org(*, name: str, created_by: str) -> str:
    conn = get_conn()
    org_id = store.create_org(conn, name=name, created_by=created_by)
    conn.commit()
    return org_id


def create_team(*, org_id: str, name: str, description: str | None, created_by: str) -> str:
    conn = get_conn()
    team_id = store.create_team(conn, org_id=org_id, name=name, description=description, created_by=created_by)
    conn.commit()
    return team_id


def add_team_members(*, team_id: str, members: list[tuple[str, str]], created_by: str) -> None:
    """``members``: ``[(user_id, role_id), ...]``，批量加人（EP-01 §9 的批量语义）。

    先整批校验角色存在，再整批写入：任何一条无效就整批 404，不做部分写入——
    批量语义下"哪几条成功了"比一次性拒绝更难向调用方解释清楚。**不**校验
    ``user_id`` 对应的账号是否存在——``tests/test_org_rbac_matrix.py`` 的多个
    既有用例（EP-01 第一阶段）直接用未落库的合成 user_id 验证角色权限传播，
    ``team_members.user_id`` 在这一阶段本来就是松耦合外键（同一份 docstring
    的"已知陷阱"精神：不在这里新增本次派单未要求、且会打破既有约定的强校验）。
    """
    conn = get_conn()
    if store.get_team(conn, team_id) is None:
        raise HTTPException(404, "团队不存在")
    for _user_id, role_id in members:
        if store.get_role(conn, role_id) is None:
            raise HTTPException(404, f"角色不存在：{role_id}")
    for user_id, role_id in members:
        store.add_team_member(conn, team_id=team_id, user_id=user_id, role_id=role_id, created_by=created_by)
    conn.commit()


def update_team(
    *, team_id: str, name: str | None, description: str | None, status: str | None,
) -> None:
    conn = get_conn()
    if store.get_team(conn, team_id) is None:
        raise HTTPException(404, "团队不存在")
    if status is not None and status not in {"active", "disabled"}:
        raise HTTPException(422, f"status 必须是 active 或 disabled，收到 {status!r}")
    store.update_team(conn, team_id, name=name, description=description, status=status)
    conn.commit()


def remove_team_member(*, team_id: str, user_id: str) -> bool:
    conn = get_conn()
    removed = store.remove_team_member(conn, team_id=team_id, user_id=user_id)
    conn.commit()
    return removed


def create_custom_role(
    *, org_id: str, key: str, name: str, description: str | None,
    permission_keys: frozenset[str], created_by: str,
) -> str:
    conn = get_conn()
    role_id = store.create_role(
        conn, org_id=org_id, key=key, name=name, description=description,
        builtin=False, created_by=created_by,
    )
    store.set_role_permissions(conn, role_id, permission_keys)
    conn.commit()
    return role_id


def update_role_permissions(*, role_id: str, permission_keys: frozenset[str]) -> None:
    conn = get_conn()
    role = store.get_role(conn, role_id)
    if role is None:
        raise HTTPException(404, "角色不存在")
    if role["builtin"]:
        raise HTTPException(422, "内置角色模板不可修改权限点")
    store.set_role_permissions(conn, role_id, permission_keys)
    conn.commit()


def delete_role(*, role_id: str) -> None:
    conn = get_conn()
    role = store.get_role(conn, role_id)
    if role is None:
        raise HTTPException(404, "角色不存在")
    if role["builtin"]:
        raise HTTPException(422, "内置角色模板不可删除")
    detail = store.role_reference_detail(conn, role_id)
    if detail["team_members"] or detail["project_grants"]:
        raise HTTPException(
            409,
            {"message": "角色仍被引用，无法删除，请先解除下列引用后重试", **detail},
        )
    store.delete_role(conn, role_id)
    conn.commit()


def grant_project_access(
    *, project_id: str, subject_type: str, subject_id: str, role_id: str, created_by: str,
    expires_at: float | None = None,
) -> None:
    """校验存在性，不校验组织归属一致性——``projects.org_id`` 目前只在
    ``app.orgs.schema.ensure_schema()`` 的一次性回填里写过，创建项目的
    正常路径（``app.domain.projects.create``）至今不写这一列，绝大多数
    项目（含全部既有测试夹具）的 ``org_id`` 是 ``NULL``。若在这里额外要求
    "角色/团队所属组织必须与项目一致"，会把这条本来就存在的历史空洞变成
    对现有个人授权场景（EP-01 §12 的 ``project_grants`` 用户级授权）的
    误杀——这不是本单元的职责，见交付报告"与 PRD 不符的现实"一节。
    """
    if subject_type not in {"user", "team"}:
        raise HTTPException(422, f"subject_type 必须是 user 或 team，收到 {subject_type!r}")
    conn = get_conn()
    if store.get_role(conn, role_id) is None:
        raise HTTPException(404, "角色不存在")
    if subject_type == "team":
        if store.get_team(conn, subject_id) is None:
            raise HTTPException(404, "团队不存在")
    elif conn.execute("SELECT 1 FROM users WHERE id=?", (subject_id,)).fetchone() is None:
        raise HTTPException(404, "用户不存在")
    store.create_project_grant(
        conn, project_id=project_id, subject_type=subject_type, subject_id=subject_id,
        role_id=role_id, created_by=created_by, expires_at=expires_at,
    )
    conn.commit()


def revoke_project_access(*, project_id: str, subject_type: str, subject_id: str) -> bool:
    conn = get_conn()
    revoked = store.delete_project_grant(
        conn, project_id=project_id, subject_type=subject_type, subject_id=subject_id,
    )
    conn.commit()
    return revoked


def principal_context(user_id: str) -> tuple[str | None, frozenset[str], frozenset[str], bool]:
    """给 ``app.auth.sessions.resolve_session`` 用：一次查出

    ``(org_id, team_ids, permission_keys, is_governed)``，对应
    ``Principal`` 的 ``org_id``/``team_ids``/``permission_keys``/
    ``role_governed`` 四个新字段。只读，不提交（没有写操作）。
    """
    conn = get_conn()
    org_id = store.user_org_id(conn, user_id)
    team_ids = store.list_team_ids_for_user(conn, user_id)
    permission_keys = store.user_permission_keys(conn, user_id)
    is_governed = store.user_is_governed(conn, user_id, team_ids)
    return org_id, team_ids, permission_keys, is_governed
