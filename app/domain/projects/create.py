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
    owner_user_id = _creation_owner_user_id()
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
            "INSERT INTO projects(id, name, status, novel_chars, created_at, owner_user_id) "
            "VALUES(?,?,'ingested',?,?,?)",
            (project_id, project_name, report["total_chars"], now(), owner_user_id))
        conn.executemany(
            "INSERT INTO chapters(project_id, idx, title, content, char_count) VALUES(?,?,?,?,?)",
            [
                (project_id, ch["idx"], ch["title"], ch["content"], len(ch["content"]))
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
