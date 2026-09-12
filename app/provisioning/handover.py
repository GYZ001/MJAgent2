"""EP-03 §5 离职移交：资产查询 + 移交动作 + 删除前置校验（L2）。

现状缺口（EP-03 §1/§5）：``DELETE /api/system/users/{id}``
（``app.domain.account_deletion.admin_soft_delete_account_core``）今天会把
账号名下**当前活跃**的项目直接一并移入回收站——这在"1 账号 1 项目空间、零
协作者"的旧模型下没有问题，但 EP-01 落地协作（``project_grants``/团队授权）
之后，项目被回收站化即对全部协作者一起失联，不是"转移给同事"。本模块补上
"先移交、不处置不许删"这道前置闸门；真正的软删本身不改（``account_deletion``
已经测试覆盖、可逆、走 30 天回收站），只在 ``DELETE`` 路由调用它之前插入
``has_unresolved_assets`` 校验（见 ``app/auth/admin_api.py::delete_user``）。

事务边界：``handover()`` 自己用 ``get_conn()`` 取本线程/任务局部连接并显式
``commit()``，与 ``app.orgs.service`` 同一惯例（调用方不持有、也不传入
连接）；两个只读函数（``list_user_assets``/``has_unresolved_assets``）不提交。

**移交要连带授权**（EP-03 §9 已知陷阱 3）：
- ``to_user_id``：``projects.owner_user_id`` 直接改成新主人——``Principal.
  owns()`` 已经覆盖这条路径；额外补一条 ``project_grants(subject_type=
  'user')`` 记录是防御性冗余，防止未来"仅凭 owner_user_id 访问"这条隐式路径
  被收紧后新主人失联，无害。
- ``to_team_id``：``projects.owner_user_id`` 没有"团队所有者"这一列，也不该
  瞎猜一个团队成员顶替——真正的读法是 ``app/authz/resolve.py::Principal.
  owns()``：空字符串 ``owner_user_id`` 天然让 ``bool(owner_user_id)`` 为假，
  不会被任何人误判成所有者；团队访问完全靠新建的
  ``project_grants(subject_type='team')`` 记录，用内置角色模板 ``owner``
  （PRD/enterprise/EP-01：本项目全部命令）。
"""
from __future__ import annotations

import sqlite3

from fastapi import HTTPException

from app import config
from app.db import get_conn, now
from app.orgs import schema as orgs_schema
from app.orgs import store as orgs_store

_INFLIGHT_JOB_STATUSES = ("queued", "running")


def _owned_active_project_rows(conn: sqlite3.Connection, user_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, name, created_at FROM projects WHERE owner_user_id=? AND deleted_at IS NULL",
        (user_id,),
    ).fetchall()


def _project_storage_bytes(project_id: str) -> int:
    project_dir = config.PROJECTS_DIR / project_id
    if not project_dir.exists():
        return 0
    total = 0
    for path in project_dir.rglob("*"):
        if path.is_file():
            try:
                total += path.stat().st_size
            except OSError:
                continue
    return total


def _inflight_job_count(conn: sqlite3.Connection, project_ids: list[str]) -> int:
    if not project_ids:
        return 0
    id_slots = ",".join("?" for _ in project_ids)
    status_slots = ",".join("?" for _ in _INFLIGHT_JOB_STATUSES)
    row = conn.execute(
        f"SELECT COUNT(*) c FROM jobs WHERE project_id IN ({id_slots}) AND status IN ({status_slots})",
        (*project_ids, *_INFLIGHT_JOB_STATUSES),
    ).fetchone()
    return row["c"] if row else 0


def _team_memberships(conn: sqlite3.Connection, user_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT tm.team_id, t.name AS team_name, tm.role_id, r.name AS role_name "
        "FROM team_members tm JOIN teams t ON t.id = tm.team_id "
        "JOIN roles r ON r.id = tm.role_id WHERE tm.user_id=? ORDER BY tm.created_at",
        (user_id,),
    ).fetchall()
    return [
        {"team_id": r["team_id"], "team_name": r["team_name"], "role_id": r["role_id"], "role_name": r["role_name"]}
        for r in rows
    ]


def list_user_assets(user_id: str) -> dict:
    """``GET /users/{id}/assets`` 的领域实现：只读，不提交。"""
    orgs_schema.ensure_schema()
    conn = get_conn()
    if conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone() is None:
        raise HTTPException(404, "用户不存在")
    projects = _owned_active_project_rows(conn, user_id)
    project_ids = [p["id"] for p in projects]
    return {
        "user_id": user_id,
        "owned_projects": [
            {"id": p["id"], "name": p["name"], "created_at": p["created_at"]} for p in projects
        ],
        "owned_projects_count": len(projects),
        "in_flight_jobs_count": _inflight_job_count(conn, project_ids),
        "storage_bytes": sum(_project_storage_bytes(pid) for pid in project_ids),
        "team_memberships": _team_memberships(conn, user_id),
    }


def has_unresolved_assets(user_id: str) -> bool:
    """删除前置闸门：名下是否还有活跃项目未处置。

    团队成员资格与在途任务本身不单独阻塞删除——前者随账号软删自然失去意义
    （账号软删后无法登录，团队角色查不到人）；后者的归属已经绑在"是否还有
    活跃项目"这一个判据上（项目没处置就意味着它名下的在途任务也没有新主人，
    项目一旦移交，在途任务自然跟着新主人继续跑，不需要单独处理）。
    """
    orgs_schema.ensure_schema()
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM projects WHERE owner_user_id=? AND deleted_at IS NULL LIMIT 1", (user_id,)
    ).fetchone()
    return row is not None


def _owner_role_id(conn: sqlite3.Connection) -> str:
    role = orgs_store.get_role_by_key(conn, None, "owner")
    if role is None:
        raise HTTPException(500, "内置角色模板缺失：owner，请先完成组织模块初始化")
    return role["id"]


def handover(*, from_user_id: str, to_user_id: str | None, to_team_id: str | None, created_by: str) -> dict:
    """离职移交：把 ``from_user_id`` 名下全部活跃项目转移给 ``to_user_id``
    或 ``to_team_id``，并连带落 ``project_grants``（见模块文档）。
    """
    if bool(to_user_id) == bool(to_team_id):
        raise HTTPException(422, "to_user_id 与 to_team_id 必须二选一，且只能提供一个")
    orgs_schema.ensure_schema()
    conn = get_conn()
    if conn.execute(
        "SELECT 1 FROM users WHERE id=? AND deleted_at IS NULL", (from_user_id,)
    ).fetchone() is None:
        raise HTTPException(404, "源账号不存在")
    if to_user_id is not None:
        if conn.execute(
            "SELECT 1 FROM users WHERE id=? AND deleted_at IS NULL", (to_user_id,)
        ).fetchone() is None:
            raise HTTPException(404, "接收用户不存在")
    elif orgs_store.get_team(conn, to_team_id) is None:
        raise HTTPException(404, "接收团队不存在")

    projects = _owned_active_project_rows(conn, from_user_id)
    owner_role_id = _owner_role_id(conn)
    transferred = [
        _transfer_one_project(
            conn, project["id"], to_user_id=to_user_id, to_team_id=to_team_id,
            owner_role_id=owner_role_id, from_user_id=from_user_id, created_by=created_by,
        )
        for project in projects
    ]
    conn.commit()
    stamp = now()
    _record_handover_audit(from_user_id, to_user_id, to_team_id, len(transferred))
    return {
        "from_user_id": from_user_id, "to_user_id": to_user_id, "to_team_id": to_team_id,
        "transferred_projects": transferred, "transferred_count": len(transferred),
        "transferred_at": stamp,
    }


def _transfer_one_project(
    conn: sqlite3.Connection, project_id: str, *, to_user_id: str | None, to_team_id: str | None,
    owner_role_id: str, from_user_id: str, created_by: str,
) -> str:
    if to_user_id is not None:
        conn.execute("UPDATE projects SET owner_user_id=? WHERE id=?", (to_user_id, project_id))
        orgs_store.create_project_grant(
            conn, project_id=project_id, subject_type="user", subject_id=to_user_id,
            role_id=owner_role_id, created_by=created_by,
        )
    else:
        conn.execute("UPDATE projects SET owner_user_id='' WHERE id=?", (project_id,))
        orgs_store.create_project_grant(
            conn, project_id=project_id, subject_type="team", subject_id=to_team_id,
            role_id=owner_role_id, created_by=created_by,
        )
    orgs_store.delete_project_grant(conn, project_id=project_id, subject_type="user", subject_id=from_user_id)
    return project_id


def _record_handover_audit(from_user_id: str, to_user_id: str | None, to_team_id: str | None, count: int) -> None:
    from app.audit import recorder

    recorder.record_command(
        "provisioning.user_handover", "离职资产移交", recorder.current_source(), "ok", None,
        f"{from_user_id} -> {to_user_id or to_team_id}（{count} 个项目）", None, None,
        {
            "from_user_id": from_user_id, "to_user_id": to_user_id, "to_team_id": to_team_id,
            "transferred_count": count,
        },
        None,
    )
