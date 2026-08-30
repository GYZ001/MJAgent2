"""项目/回收站列表、按段文本模型设置、单章节正文读取。"""
from __future__ import annotations

from fastapi import HTTPException

from app.db import get_conn, now, rows_to_dicts
from app.domain.common import (
    _as_body_dict,
    _project_or_404,
    _recover_orphan_bible_dicts,
    router,
)
from app.domain.projects.constants import PROJECT_RECYCLE_BIN_RETENTION_S


def _listing_owner_scope() -> str | None:
    """列表类查询的归属范围：普通账号只能是自己，``None`` 表示不过滤（系统管理员/
    无身份上下文的内部调用，与既有约定一致）。"""
    from app.auth.principal import get_current_principal

    principal = get_current_principal()
    if principal is not None and not principal.is_system_admin:
        return principal.user_id
    return None


@router.get("/projects")
def list_projects():
    conn = get_conn()
    # 系统管理员看全部；普通用户只看自己名下的项目——RBAC 第四阶段收紧点之一，
    # 此前这里没有任何 WHERE，任何登录用户都能看到全量项目列表。deleted_at IS
    # NULL：软删除的项目进了回收站，不再出现在正常列表——回收站专用列表见
    # list_deleted_projects()。
    owner = _listing_owner_scope()
    if owner is not None:
        rows = rows_to_dicts(conn.execute(
            "SELECT id, name, status, novel_chars, bible_status, plan_status, created_at "
            "FROM projects WHERE deleted_at IS NULL AND owner_user_id=? ORDER BY created_at DESC",
            (owner,),
        ).fetchall())
    else:
        # ALL_OWNERS: caller is a system admin (or an internal call with no
        # Principal at all, per _listing_owner_scope()) -- both are meant to
        # see every project; the marker inside the SQL text is what
        # tests/test_project_ownership_query_guard.py actually looks for.
        rows = rows_to_dicts(conn.execute(
            "SELECT id, name, status, novel_chars, bible_status, plan_status, created_at "
            "FROM projects -- ALL_OWNERS: system admin / internal caller\n"
            "WHERE deleted_at IS NULL ORDER BY created_at DESC").fetchall())
    _recover_orphan_bible_dicts(conn, rows)
    for p in rows:
        p["chapter_count"] = conn.execute("SELECT COUNT(*) c FROM chapters WHERE project_id=?", (p["id"],)).fetchone()["c"]
        p["episode_count"] = conn.execute("SELECT COUNT(*) c FROM episodes WHERE project_id=?", (p["id"],)).fetchone()["c"]
    return rows


@router.get("/projects/deleted")
def list_deleted_projects():
    """回收站：已软删除但还没到 24 小时保留期（或还没被手动彻底清理）的项目。

    必须注册在 ``GET /projects/{project_id}`` 之前（见本文件靠后处该路由的
    注册位置）——否则 Starlette 会先把 "deleted" 当成 project_id 匹配掉。
    """
    conn = get_conn()
    owner = _listing_owner_scope()
    if owner is not None:
        rows = rows_to_dicts(conn.execute(
            "SELECT id, name, status, novel_chars, bible_status, plan_status, created_at, "
            "deleted_at, recycle_bin_retention_s FROM projects "
            "WHERE deleted_at IS NOT NULL AND owner_user_id=? ORDER BY deleted_at DESC",
            (owner,),
        ).fetchall())
    else:
        # ALL_OWNERS: same admin/internal-caller rationale as list_projects()
        # above; the marker inside the SQL text is what
        # tests/test_project_ownership_query_guard.py actually looks for.
        rows = rows_to_dicts(conn.execute(
            "SELECT id, name, status, novel_chars, bible_status, plan_status, created_at, "
            "deleted_at, recycle_bin_retention_s "
            "FROM projects -- ALL_OWNERS: system admin / internal caller\n"
            "WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC").fetchall())
    stamp = now()
    for p in rows:
        p["chapter_count"] = conn.execute("SELECT COUNT(*) c FROM chapters WHERE project_id=?", (p["id"],)).fetchone()["c"]
        p["episode_count"] = conn.execute("SELECT COUNT(*) c FROM episodes WHERE project_id=?", (p["id"],)).fetchone()["c"]
        # 每行自己的保留期（账号级联软删除写 30 天，见 ACCOUNT_DELETE_RETENTION_S），
        # NULL 时回退默认 24 小时——与 sweep_expired_deleted_projects() 同一口径。
        retention_s = p["recycle_bin_retention_s"] or PROJECT_RECYCLE_BIN_RETENTION_S
        purge_at = float(p["deleted_at"]) + retention_s
        p["purge_at"] = purge_at
        p["retention_seconds_remaining"] = max(0.0, purge_at - stamp)
    return rows


_TEXT_MODEL_STAGE_COLUMNS = {
    "bible_text_provider": "bible_text_provider",
    "script_text_provider": "script_text_provider",
    "board_text_provider": "board_text_provider",
}


@router.put("/projects/{project_id}/text-models")
def set_project_text_models(project_id: str, body: dict):
    """保存世界书/映射台/分镜台各自的专属文本模型（项目级，三个环节共用一个项目
    设置，不按分集单独选）。body 只需带想改的字段；值为空串表示回落到全局默认
    文本 provider。每个非空值都必须出现在当前 text_model_choices() 里——不接受
    选一个没配凭据或已被删除的 provider，防止保存一个必然失败的选项。账号即
    项目空间之后，能触达这个端点就已经是本项目的所有者或系统管理员（HTTP 边界
    ``require_project_owner_access`` 已经拦过一轮），不再需要按团队角色二次
    收紧写权限。
    """
    from app import model_registry

    _project_or_404(project_id)
    body = _as_body_dict(body)
    valid_providers = {choice["provider"] for choice in model_registry.text_model_choices()}
    updates: dict[str, str] = {}
    for field, column in _TEXT_MODEL_STAGE_COLUMNS.items():
        if field not in body:
            continue
        value = str(body.get(field) or "").strip()
        if value and value not in valid_providers:
            raise HTTPException(422, f"未知或不可用的文本模型：{field}={value}")
        updates[column] = value
    if not updates:
        raise HTTPException(400, "未提供任何字段：bible_text_provider/script_text_provider/board_text_provider")
    conn = get_conn()
    set_clause = ", ".join(f"{column}=?" for column in updates)
    conn.execute(
        f"UPDATE projects SET {set_clause} WHERE id=?",
        (*updates.values(), project_id),
    )
    conn.commit()
    return {"project_id": project_id, **updates}


@router.get("/projects/{project_id}/chapters/{idx}")
def read_chapter(project_id: str, idx: int):
    """看正文：返回某章完整正文 + 上一章/下一章索引，供沉浸式阅读页翻页。"""
    _project_or_404(project_id)
    conn = get_conn()
    ch = conn.execute("SELECT idx, title, content FROM chapters WHERE project_id=? AND idx=?",
                      (project_id, idx)).fetchone()
    if not ch:
        raise HTTPException(404, f"章节不存在：第 {idx} 章")
    bounds = conn.execute(
        "SELECT MIN(idx) AS lo, MAX(idx) AS hi, COUNT(*) AS n FROM chapters WHERE project_id=?",
        (project_id,)).fetchone()
    prev_idx = conn.execute("SELECT MAX(idx) AS m FROM chapters WHERE project_id=? AND idx<?",
                            (project_id, idx)).fetchone()["m"]
    next_idx = conn.execute("SELECT MIN(idx) AS m FROM chapters WHERE project_id=? AND idx>?",
                            (project_id, idx)).fetchone()["m"]
    return {"idx": ch["idx"], "title": ch["title"], "content": ch["content"],
            "prev_idx": prev_idx, "next_idx": next_idx,
            "first_idx": bounds["lo"], "last_idx": bounds["hi"], "total": bounds["n"]}
