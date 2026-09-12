"""EP-01 组织/团队/角色/项目授权的存取层（L2，见 app/LAYERS.toml::app.orgs）。

只做 SQL 读写，零业务判定、零 ``app.capabilities``/``app.authz.catalog``
依赖（后者在 L5，L2 不能反向依赖——见 ``app/authz/catalog.py`` 顶部文档）。
每个函数的 ``conn: sqlite3.Connection`` 是必填参数，不留 ``conn=None`` 这类
隐式所有权（CLAUDE.md「可选参数是缺陷的温床」）；函数内部不调用
``conn.commit()``——事务边界由调用方决定，与 ``app/quota_addon.py`` 同一惯例。

表结构、org_default 种子与 ``users``/``projects.org_id`` 回填在
``app/orgs/schema.py``（lazy 建表，``app/db.py`` 基线已用满，见该模块
文档）；每个函数入口调用 ``schema.ensure_tables_on_connection(conn)`` 兜底
（同连接、不开新连接、不申请新锁）——本模块的函数会被
``app.quota_policy.allocation`` 在调用方已持有的 ``BEGIN IMMEDIATE`` 事务里
直接调用，若沿用 ``schema.ensure_schema()`` 会跟调用方抢写锁、2 秒超时后
静默建表失败（见 ``app/orgs/schema.py`` 模块文档「两个入口」一段）。种子
（org_default + 5 个内置角色元数据行）不在这条兜底里做，由
``app.main`` lifespan 与测试模板初始化各自调用一次完整的
``schema.ensure_schema()`` 保证。

**「迁移后行为零变化」不靠一次性把既有账号写进 team_members 达成**（早期
设计草案这样做过，被 ``tests/test_rbac_enforcement_evidence.py`` 的一条端到端
用例当场证伪：它在测试运行期间新建账号，而 EP-01 的 REST 路由尚未开放团队
管理，新账号永远没有机会被加进任何团队——按"迁移时刻是否已存在"分流会让
**这一阶段创建的每一个新账号**都被永久拒绝一切非只读命令，这不是"迁移零
变化"，是"迁移后新功能全部失效"）。真正的判据是 ``user_is_governed``——
一个账号「是否已经被接入新模型」看它有没有 **任何** team_members 行或
project_grants 行，而不是看它的创建时间：
- 零团队、零项目授权 = 未接入，``Principal.can()`` 只受 ``admin_only``
  把关，语义与今天完全一致（这正是当前唯一的账号创建路径产出的状态，EP-02/
  EP-03 的批量导入/SSO 自动开户落地前不会变）。
- 一旦被管理员显式拉进某个团队或直接授予某个项目，才真正进入新模型，严格
  按 ``role_permissions`` 判定——包括角色权限点为空集合时必须拒绝一切
  （CLAUDE.md「空集合不等于放行」），不能因为"曾经是老用户"就豁免。
"""
from __future__ import annotations

import sqlite3

from app.db import new_id, now
from app.orgs import schema
from app.orgs.schema import ORG_DEFAULT_ID as ORG_DEFAULT_ID


def create_org(conn: sqlite3.Connection, *, name: str, created_by: str, tenant_id: str = "default") -> str:
    schema.ensure_tables_on_connection(conn)
    org_id = new_id("org")
    conn.execute(
        "INSERT INTO orgs(id, tenant_id, name, status, created_at, created_by) VALUES(?,?,?,?,?,?)",
        (org_id, tenant_id, name, "active", now(), created_by),
    )
    return org_id


def get_org(conn: sqlite3.Connection, org_id: str) -> dict | None:
    schema.ensure_tables_on_connection(conn)
    row = conn.execute("SELECT * FROM orgs WHERE id=?", (org_id,)).fetchone()
    return dict(row) if row else None


def create_team(conn: sqlite3.Connection, *, org_id: str, name: str, description: str | None, created_by: str) -> str:
    schema.ensure_tables_on_connection(conn)
    team_id = new_id("team")
    conn.execute(
        "INSERT INTO teams(id, org_id, name, description, status, created_at, created_by) "
        "VALUES(?,?,?,?,?,?,?)",
        (team_id, org_id, name, description, "active", now(), created_by),
    )
    return team_id


def get_team(conn: sqlite3.Connection, team_id: str) -> dict | None:
    schema.ensure_tables_on_connection(conn)
    row = conn.execute("SELECT * FROM teams WHERE id=?", (team_id,)).fetchone()
    return dict(row) if row else None


def list_teams(conn: sqlite3.Connection, org_id: str) -> list[dict]:
    schema.ensure_tables_on_connection(conn)
    rows = conn.execute("SELECT * FROM teams WHERE org_id=? ORDER BY created_at", (org_id,)).fetchall()
    return [dict(r) for r in rows]


def update_team(
    conn: sqlite3.Connection, team_id: str, *,
    name: str | None, description: str | None, status: str | None,
) -> None:
    """局部更新：只有非 None 的字段会被写入，调用方决定要改哪些列。"""
    schema.ensure_tables_on_connection(conn)
    fields: list[str] = []
    values: list[object] = []
    if name is not None:
        fields.append("name=?")
        values.append(name)
    if description is not None:
        fields.append("description=?")
        values.append(description)
    if status is not None:
        fields.append("status=?")
        values.append(status)
    if not fields:
        return
    values.append(team_id)
    conn.execute(f"UPDATE teams SET {', '.join(fields)} WHERE id=?", values)


def add_team_member(conn: sqlite3.Connection, *, team_id: str, user_id: str, role_id: str, created_by: str) -> None:
    schema.ensure_tables_on_connection(conn)
    conn.execute(
        "INSERT INTO team_members(team_id, user_id, role_id, created_at, created_by) "
        "VALUES(?,?,?,?,?) "
        "ON CONFLICT(team_id, user_id) DO UPDATE SET role_id=excluded.role_id",
        (team_id, user_id, role_id, now(), created_by),
    )


def remove_team_member(conn: sqlite3.Connection, *, team_id: str, user_id: str) -> bool:
    schema.ensure_tables_on_connection(conn)
    cur = conn.execute("DELETE FROM team_members WHERE team_id=? AND user_id=?", (team_id, user_id))
    return cur.rowcount > 0


def list_team_members(conn: sqlite3.Connection, team_id: str) -> list[dict]:
    schema.ensure_tables_on_connection(conn)
    rows = conn.execute(
        "SELECT * FROM team_members WHERE team_id=? ORDER BY created_at", (team_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def list_team_ids_for_user(conn: sqlite3.Connection, user_id: str) -> frozenset[str]:
    schema.ensure_tables_on_connection(conn)
    rows = conn.execute("SELECT team_id FROM team_members WHERE user_id=?", (user_id,)).fetchall()
    return frozenset(r["team_id"] for r in rows)


def create_role(
    conn: sqlite3.Connection, *, org_id: str | None, key: str, name: str, description: str | None,
    builtin: bool, created_by: str,
) -> str:
    schema.ensure_tables_on_connection(conn)
    role_id = new_id("role")
    ts = now()
    conn.execute(
        "INSERT INTO roles(id, org_id, key, name, description, builtin, created_at, created_by, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (role_id, org_id, key, name, description, int(builtin), ts, created_by, ts),
    )
    return role_id


def get_role(conn: sqlite3.Connection, role_id: str) -> dict | None:
    schema.ensure_tables_on_connection(conn)
    row = conn.execute("SELECT * FROM roles WHERE id=?", (role_id,)).fetchone()
    return dict(row) if row else None


def get_role_by_key(conn: sqlite3.Connection, org_id: str | None, key: str) -> dict | None:
    schema.ensure_tables_on_connection(conn)
    if org_id is None:
        row = conn.execute("SELECT * FROM roles WHERE org_id IS NULL AND key=?", (key,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM roles WHERE org_id=? AND key=?", (org_id, key)).fetchone()
    return dict(row) if row else None


def list_roles(conn: sqlite3.Connection, org_id: str) -> list[dict]:
    """本组织自定义角色 + 全局内置模板（org_id IS NULL）。"""
    schema.ensure_tables_on_connection(conn)
    rows = conn.execute(
        "SELECT * FROM roles WHERE org_id=? OR org_id IS NULL ORDER BY builtin DESC, created_at", (org_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def delete_role(conn: sqlite3.Connection, role_id: str) -> None:
    schema.ensure_tables_on_connection(conn)
    conn.execute("DELETE FROM roles WHERE id=?", (role_id,))


def role_reference_detail(conn: sqlite3.Connection, role_id: str) -> dict[str, list[dict]]:
    """角色仍被哪些团队成员/项目授权引用——不只是计数，供 409 响应体把引用方
    列出来（CLAUDE.md「拦住用户时必须给出路」：只给一个数字，管理员无从下手
    去解除哪一条引用）。"""
    schema.ensure_tables_on_connection(conn)
    team_rows = conn.execute(
        "SELECT tm.team_id, t.name AS team_name, tm.user_id FROM team_members tm "
        "JOIN teams t ON t.id = tm.team_id WHERE tm.role_id=? ORDER BY tm.created_at",
        (role_id,),
    ).fetchall()
    grant_rows = conn.execute(
        "SELECT project_id, subject_type, subject_id FROM project_grants "
        "WHERE role_id=? ORDER BY created_at",
        (role_id,),
    ).fetchall()
    return {
        "team_members": [
            {"team_id": r["team_id"], "team_name": r["team_name"], "user_id": r["user_id"]}
            for r in team_rows
        ],
        "project_grants": [
            {"project_id": r["project_id"], "subject_type": r["subject_type"], "subject_id": r["subject_id"]}
            for r in grant_rows
        ],
    }


def set_role_permissions(conn: sqlite3.Connection, role_id: str, permission_keys: frozenset[str]) -> None:
    schema.ensure_tables_on_connection(conn)
    conn.execute("DELETE FROM role_permissions WHERE role_id=?", (role_id,))
    conn.executemany(
        "INSERT INTO role_permissions(role_id, permission_key) VALUES(?,?)",
        [(role_id, key) for key in sorted(permission_keys)],
    )


def list_role_permission_keys(conn: sqlite3.Connection, role_id: str) -> frozenset[str]:
    schema.ensure_tables_on_connection(conn)
    rows = conn.execute("SELECT permission_key FROM role_permissions WHERE role_id=?", (role_id,)).fetchall()
    return frozenset(r["permission_key"] for r in rows)


def create_project_grant(
    conn: sqlite3.Connection, *, project_id: str, subject_type: str, subject_id: str, role_id: str,
    created_by: str, expires_at: float | None = None,
) -> None:
    schema.ensure_tables_on_connection(conn)
    conn.execute(
        "INSERT INTO project_grants(project_id, subject_type, subject_id, role_id, created_at, created_by, expires_at) "
        "VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(project_id, subject_type, subject_id) "
        "DO UPDATE SET role_id=excluded.role_id, expires_at=excluded.expires_at",
        (project_id, subject_type, subject_id, role_id, now(), created_by, expires_at),
    )


def delete_project_grant(conn: sqlite3.Connection, *, project_id: str, subject_type: str, subject_id: str) -> bool:
    schema.ensure_tables_on_connection(conn)
    cur = conn.execute(
        "DELETE FROM project_grants WHERE project_id=? AND subject_type=? AND subject_id=?",
        (project_id, subject_type, subject_id),
    )
    return cur.rowcount > 0


def list_project_grants(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    schema.ensure_tables_on_connection(conn)
    rows = conn.execute(
        "SELECT * FROM project_grants WHERE project_id=? ORDER BY created_at", (project_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def user_org_id(conn: sqlite3.Connection, user_id: str) -> str | None:
    schema.ensure_tables_on_connection(conn)
    row = conn.execute("SELECT org_id FROM users WHERE id=?", (user_id,)).fetchone()
    return row["org_id"] if row else None


def project_org_id(conn: sqlite3.Connection, project_id: str) -> str | None:
    schema.ensure_tables_on_connection(conn)
    row = conn.execute("SELECT org_id FROM projects WHERE id=?", (project_id,)).fetchone()
    return row["org_id"] if row else None


def user_has_org_admin(conn: sqlite3.Connection, user_id: str, org_id: str | None) -> bool:
    """用户是否在本组织内的任意团队持有 key='org_admin' 的角色。"""
    schema.ensure_tables_on_connection(conn)
    if not org_id:
        return False
    row = conn.execute(
        "SELECT 1 FROM team_members tm "
        "JOIN teams t ON t.id = tm.team_id "
        "JOIN roles r ON r.id = tm.role_id "
        "WHERE tm.user_id=? AND t.org_id=? AND r.key='org_admin' LIMIT 1",
        (user_id, org_id),
    ).fetchone()
    return row is not None


def project_grant_hit(conn: sqlite3.Connection, project_id: str, user_id: str, team_ids: frozenset[str]) -> bool:
    """项目是否直接授予了该用户本人，或授予了他所属的某个团队（且未过期）。"""
    schema.ensure_tables_on_connection(conn)
    ts = now()
    row = conn.execute(
        "SELECT 1 FROM project_grants "
        "WHERE project_id=? AND subject_type='user' AND subject_id=? "
        "AND (expires_at IS NULL OR expires_at > ?) LIMIT 1",
        (project_id, user_id, ts),
    ).fetchone()
    if row is not None:
        return True
    if not team_ids:
        return False
    placeholders = ",".join("?" for _ in team_ids)
    row = conn.execute(
        f"SELECT 1 FROM project_grants "
        f"WHERE project_id=? AND subject_type='team' AND subject_id IN ({placeholders}) "
        f"AND (expires_at IS NULL OR expires_at > ?) LIMIT 1",
        (project_id, *team_ids, ts),
    ).fetchone()
    return row is not None


def user_permission_keys(conn: sqlite3.Connection, user_id: str) -> frozenset[str]:
    """用户当前持有的全部权限点：团队角色 ∪ 项目授权角色，均未过期。

    与 Command Bus 的既有分工一致（见 ``app/capabilities/bus.py::_authorize``
    的文档）：这里只回答"这个人一般能不能做这类动作"，不区分具体项目——
    "碰不碰得到这个项目"由 ``app/authz/resolve.py`` 的 HTTP 边界另行判断。
    """
    schema.ensure_tables_on_connection(conn)
    ts = now()
    rows = conn.execute(
        "SELECT DISTINCT rp.permission_key FROM team_members tm "
        "JOIN role_permissions rp ON rp.role_id = tm.role_id "
        "WHERE tm.user_id=?",
        (user_id,),
    ).fetchall()
    keys = {r["permission_key"] for r in rows}
    rows = conn.execute(
        "SELECT DISTINCT rp.permission_key FROM project_grants pg "
        "JOIN role_permissions rp ON rp.role_id = pg.role_id "
        "WHERE pg.subject_type='user' AND pg.subject_id=? "
        "AND (pg.expires_at IS NULL OR pg.expires_at > ?)",
        (user_id, ts),
    ).fetchall()
    keys.update(r["permission_key"] for r in rows)
    return frozenset(keys)


def user_is_governed(conn: sqlite3.Connection, user_id: str, team_ids: frozenset[str]) -> bool:
    """账号是否已经被接入新的组织角色模型——见模块文档"迁移后行为零变化"
    那一段：零团队成员资格、零项目授权的账号视为"未接入"，``Principal.
    can()`` 继续只受 ``admin_only`` 把关；一旦二者任一非空，就严格按
    ``role_permissions`` 判定（含权限点为空集合时拒绝一切）。
    """
    schema.ensure_tables_on_connection(conn)
    if team_ids:
        return True
    row = conn.execute(
        "SELECT 1 FROM project_grants WHERE subject_type='user' AND subject_id=? LIMIT 1",
        (user_id,),
    ).fetchone()
    return row is not None
