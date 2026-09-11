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
    """``members``: ``[(user_id, role_id), ...]``，批量加人（EP-01 §9 的批量语义）。"""
    conn = get_conn()
    for user_id, role_id in members:
        store.add_team_member(conn, team_id=team_id, user_id=user_id, role_id=role_id, created_by=created_by)
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
    refs = store.role_reference_counts(conn, role_id)
    if refs["team_members"] or refs["project_grants"]:
        raise HTTPException(409, f"角色仍被引用，无法删除：{refs}")
    store.delete_role(conn, role_id)
    conn.commit()


def grant_project_access(
    *, project_id: str, subject_type: str, subject_id: str, role_id: str, created_by: str,
    expires_at: float | None = None,
) -> None:
    if subject_type not in {"user", "team"}:
        raise HTTPException(422, f"subject_type 必须是 user 或 team，收到 {subject_type!r}")
    conn = get_conn()
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
