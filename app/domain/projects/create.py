"""小说导入 / 项目创建：上传校验、幂等回执、领域核心与三个 REST 路由。"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import Body, File, Form, HTTPException, UploadFile

from app import quota
from app.db import get_conn, new_id, now
from app.domain.common import router
from app.ingest import ingest_novel
from app.novel_formats import (
    SUPPORTED_NOVEL_LABEL,
    novel_file_suffix,
    prepare_novel_bytes,
    validate_novel_filename,
)
from app.orgs import schema as orgs_schema
from app.orgs import store as orgs_store


async def _read_novel_upload(file: UploadFile) -> tuple[str, bytes]:
    """Bound memory use and reject unsupported uploads before issuing a token."""
    from app.ingest import MAX_NOVEL_UPLOAD_BYTES

    try:
        filename = validate_novel_filename(file.filename)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    raw = await file.read(MAX_NOVEL_UPLOAD_BYTES + 1)
    if len(raw) > MAX_NOVEL_UPLOAD_BYTES:
        limit_mb = MAX_NOVEL_UPLOAD_BYTES // (1024 * 1024)
        raise HTTPException(413, f"小说文件超过 {limit_mb} MB，请拆分后再导入")
    if not raw:
        raise HTTPException(400, f"文件为空，请选择包含正文的 {SUPPORTED_NOVEL_LABEL} 小说")
    try:
        # Validate while the user is still on the file-selection step. The
        # authoritative parse is repeated inside the transaction below.
        ingest_novel(prepare_novel_bytes(filename, raw))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return filename, raw


def _novel_import_token_hash(attachment_token: str) -> str:
    import hashlib

    return hashlib.sha256(attachment_token.encode("utf-8")).hexdigest()


def _novel_import_receipt(token_hash: str) -> dict | None:
    if not token_hash:
        return None
    row = get_conn().execute(
        """SELECT r.result_json
             FROM novel_import_receipts r
             JOIN projects p ON p.id=r.project_id
            WHERE r.token_hash=?""",
        (token_hash,),
    ).fetchone()
    if not row:
        return None
    try:
        result = json.loads(row["result_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(result, dict) or not result.get("project_id"):
        return None
    return {**result, "idempotent_replay": True}


_LEGACY_NO_PRINCIPAL_OWNER = "legacy-shared"


def _creation_owner_user_id() -> str:
    """新项目归属哪个账号——就是发起创建的那个人，没有任何间接概念。

    账号即项目空间之后，这条规则不再需要「团队」这层中间概念：项目直接归属
    ``principal.user_id``，无歧义、无需选择。``principal is None``（兼容期共享
    会话、内部调用、既有测试）保持原行为，落到与 ``app.local_session.
    _legacy_shared_principal`` 一致的占位账号，不阻塞这些既有路径。
    """
    from app.auth.principal import get_current_principal

    principal = get_current_principal()
    if principal is None:
        return _LEGACY_NO_PRINCIPAL_OWNER
    return principal.user_id


def _creation_org_id() -> str:
    """新项目归属哪个组织——绝不落 ``None``。

    此前这里根本不写 ``org_id`` 列，导致 ``INSERT`` 隐式落 NULL：
    ``app.orgs.schema._seed_org_default_and_roles`` 的一次性回填只在进程/
    DB_PATH 首次建表时跑一次，首启之后新建的每个项目永远是
    ``org_id=NULL``，而 ``NULL`` 永远不等于任何 ``org_id``——组织管理员
    （``app.orgs.store.user_has_org_admin`` + ``app.authz.access_cache.
    project_access_allowed``）因此永远看不到新项目，组织维度随新项目增加
    逐步失效。这正是「可选参数是缺陷的温床」的实例，所以这里不给调用方
    留退路，函数本身也不返回 ``None``。

    取值规则与 ``app.orgs.api.list_roles``/``app.provisioning.api.
    _actor_org_id`` 同一条既有惯例：``principal.org_id or
    orgs_store.ORG_DEFAULT_ID``。单租户下只有一个默认组织（见
    ``app.orgs.schema`` 模块文档「单租户下默认一个 org_default，回填零
    风险」），下面两种「取不到真实组织」的情况因此都安全地回退到同一个
    默认组织，而不是落 NULL 静默漏项：
    - 没有 Principal（后台任务、CLI、尚未挂会话闸门的既有测试直接调用）；
    - 有 Principal 但 ``org_id`` 恰好是 ``None``（理论上不该发生——
      ``users.org_id`` 由 ``ensure_schema()`` 的种子回填保证非空——但同样
      不允许在这里把不确定性再传染给 ``projects.org_id``）。
    """
    from app.auth.principal import get_current_principal

    principal = get_current_principal()
    org_id = principal.org_id if principal is not None else None
    return org_id or orgs_store.ORG_DEFAULT_ID


def _creation_ownership(conn) -> tuple[str, str]:
    """一次性解析新项目的账号归属 + 组织归属，供 ``_create_project_core`` 调用。

    合并成一次调用（而不是让调用方分别调 ``_creation_owner_user_id()``/
    ``_creation_org_id()`` 再自己记得加 schema 兜底），是为了不让
    ``_create_project_core`` 自身的代码行数被这次修复推过
    ``app/FILE_CONVENTIONS.toml`` 的存量超标基线——该函数已经在棘轮里挂账，
    CLAUDE.md「红线只降不升」不允许这次改动把它推得更高。

    ``orgs_schema.ensure_tables_on_connection(conn)`` 必须在这里做一次：
    ``_create_project_core`` 稍后的 INSERT 要写 ``projects.org_id``，这一列
    在部分不走完整 ``app.main.lifespan()`` 的最小化测试宿主（自建
    ``FastAPI()`` 只挂 ``app.api.router``、只调 ``db.init_db()``）里还不
    存在，缺列会让 INSERT 直接 ``OperationalError``，把"组织维度悄悄失效"
    的真实修复误判成"测试环境搭建有问题"。同连接、幂等、只加列不做种子/
    回填，必须在事务开始前调用——sqlite3 的 ``executescript`` 会隐式
    COMMIT 掉任何尚未提交的事务，调用方已经把它放在 ``BEGIN IMMEDIATE``
    之前。
    """
    orgs_schema.ensure_tables_on_connection(conn)
    return _creation_owner_user_id(), _creation_org_id()


def _create_project_core(
    name: str | None,
    filename: str,
    raw: bytes,
    *,
    import_token_hash: str | None = None,
) -> dict:
    """导入小说的领域逻辑，供 REST 路由与 ``project.import_novel`` Command Handler 共用。"""
    if not raw:
        raise HTTPException(400, f"文件为空，请选择包含正文的 {SUPPORTED_NOVEL_LABEL} 小说")
    try:
        filename = validate_novel_filename(filename)
        report = ingest_novel(prepare_novel_bytes(filename, raw))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not report["chapters"]:
        raise HTTPException(422, "未能从文件中切分出任何章节，请检查正文或章节标题")
    conn = get_conn()
    owner_user_id, org_id = _creation_ownership(conn)
    project_id = new_id("proj")
    fallback_name = Path(filename).stem.strip() or "未命名小说"
    project_name = (name or "").strip() or fallback_name
    if len(project_name) > 120:
        raise HTTPException(422, "项目名称不能超过 120 个字符")
    if import_token_hash:
        existing = _novel_import_receipt(import_token_hash)
        if existing is not None:
            return existing
    outcome = {
        "project_id": project_id,
        "ingestion": {
            key: report[key]
            for key in (
                "total_chars",
                "removed_lines",
                "chapter_count",
                "deduplicated_stub_chapters",
                "auto_split",
            )
        } | {"source_format": novel_file_suffix(filename).lstrip(".").upper()},
    }
    try:
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        active_projects = conn.execute(
            "SELECT COUNT(*) AS c FROM projects WHERE owner_user_id=? AND deleted_at IS NULL",
            (owner_user_id,),
        ).fetchone()["c"]
        quota.check_project_slot(conn, owner_user_id, active_count=int(active_projects))
        conn.execute(
            "INSERT INTO projects(id, name, status, novel_chars, created_at, owner_user_id, org_id) "
            "VALUES(?,?,'ingested',?,?,?,?)",
            (project_id, project_name, report["total_chars"], now(), owner_user_id, org_id))
        # ingest_novel 已经算好本章的小节边界（app.novel.structure._extract_sections），
        # 装在 ch["paratext_json"] 里；此前这里没写这一列，小节信息落地即丢——见
        # app/source_paratext.py::chapter_paratext_offsets 的合并写入注释。
        conn.executemany(
            "INSERT INTO chapters(project_id, idx, title, content, char_count, paratext_json) VALUES(?,?,?,?,?,?)",
            [
                (project_id, ch["idx"], ch["title"], ch["content"], len(ch["content"]), ch.get("paratext_json"))
                for ch in report["chapters"]
            ])
        if import_token_hash:
            conn.execute(
                """INSERT INTO novel_import_receipts(
                       token_hash,project_id,result_json,created_at
                   ) VALUES(?,?,?,?)""",
                (
                    import_token_hash,
                    project_id,
                    json.dumps(outcome, ensure_ascii=False),
                    now(),
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return outcome


@router.post("/attachments/novel")
async def upload_novel_attachment(file: UploadFile = File(...)):
    """用户选择 TXT/EPUB 后，前端立即换发短时效 attachment_token（不暴露真实路径）。"""
    from app.capabilities.attachments import store_upload

    filename, raw = await _read_novel_upload(file)
    token = store_upload(filename, raw, content_type=file.content_type)
    return {
        "attachment_token": token,
        "filename": filename,
        "size_bytes": len(raw),
        "expires_in_s": 15 * 60,
    }


@router.post("/projects")
async def create_project(
    name: str = Form(...),
    file: UploadFile = File(...),
    style_name: str | None = Form(default=None),
):
    """页面上传入口：内部换发 attachment_token 后统一走 Command Bus，与 Agent/MCP 同一实现。"""
    from app.capabilities.attachments import store_upload
    from app.capabilities.dispatch import dispatch, respond_ui

    filename, raw = await _read_novel_upload(file)
    token = store_upload(filename, raw, content_type=file.content_type)
    result = await dispatch(
        "project.import_novel",
        {
            "attachment_token": token,
            "name": name,
            "style_name": style_name,
            "idempotency_key": f"novel-import:{token}",
        },
        initiator="ui",
    )
    return respond_ui(result)


@router.post("/projects/import")
async def create_project_from_attachment(
    attachment_token: str = Body(...),
    name: str | None = Body(default=None),
    style_name: str | None = Body(default=None),
):
    """用已上传的附件令牌导入小说，确保批准前后的命令参数保持不变。"""
    from app.capabilities.dispatch import dispatch, respond_ui

    result = await dispatch(
        "project.import_novel",
        {
            "attachment_token": attachment_token,
            "name": name,
            "style_name": style_name,
            # The one-time attachment token is unique for this import. Reusing
            # it as the command key makes response-loss retries replay-safe.
            "idempotency_key": f"novel-import:{attachment_token}",
        },
        initiator="ui",
    )
    return respond_ui(result)
