from __future__ import annotations

import json

from collections.abc import Iterable
from pathlib import Path

from fastapi import (
    Body,
    File,
    Form,
    HTTPException,
    UploadFile,
)

from app import (
    config,
    quota,
    task_registry,
    worker,
)
from app.db import (
    get_conn,
    new_id,
    now,
    rows_to_dicts,
)
from app.domain.common import (
    _as_body_dict,
    _assert_principal_owns,
    _episode_or_404,
    _media_url,
    _project_or_404,
    _recover_orphan_bible_dicts,
    router,
)
from app.evidence import repository as evidence_repository
from app.ingest import ingest_novel
from app.media_urls import build_media_url
from app.novel_formats import (
    SUPPORTED_NOVEL_LABEL,
    novel_file_suffix,
    prepare_novel_bytes,
    validate_novel_filename,
)
from app.planning import chapter_preview
from app.schemas import EpisodeScreenplay


_SQLITE_IN_CHUNK_SIZE = 400

# 软删除项目在回收站保留的时长；到期由 sweep_expired_deleted_projects 彻底清理。
# 判据是 deleted_at 时间戳（见 app/db.py MIGRATIONS 的列注释），这个常量只用来
# 算「到期时间」，不是驱动清理的计时器本身。
PROJECT_RECYCLE_BIN_RETENTION_S = 24 * 3600

# 账号级联软删除（管理员删账号）带出的项目改用这个更长的保留期，见
# sweep_expired_deleted_projects() 与 app.domain.account_deletion。
ACCOUNT_DELETE_RETENTION_S = 30 * 24 * 3600


def _project_task_timings(conn, project: dict) -> dict[str, dict[str, float | None]]:
    """项目级任务计时的服务端起止时间。

    前端曾把起点存在 localStorage：任务运行中刷新页面会让起点永久搁浅，下一个
    任务复用旧起点后显示出「已等待 1244 分」这类虚高时长，故一律以服务端为准。
    """
    project_id = project["id"]

    def run_timing(workflow_type: str) -> dict[str, float | None]:
        return evidence_repository.latest_run_timing(
            workflow_type=workflow_type,
            scope_type="project",
            scope_id=project_id,
            conn=conn,
        )

    def batch_timing(workflow_type: str, batch_column: str) -> dict[str, float | None]:
        """批次任务的计时：起点取批次列，结束沿用最近一次 run。

        这类任务续跑时会新建 workflow_run，只看最近一次 run 会让计时在每次
        续跑后归零（剧本台曾表现为跑了 43 分钟却显示 3 分钟）。批次列在续跑时
        由 resume 分支保留，才是任务级起点。
        """
        timing = run_timing(workflow_type)
        batch_started_at = project.get(batch_column)
        if batch_started_at is not None:
            timing["started_at"] = batch_started_at
        return timing

    # 批量分镜没有父 run，只能按活跃子 run 聚合；全部结束后不再有「本次耗时」可言。
    marks = ",".join("?" for _ in evidence_repository.ACTIVE_RUN_STATUSES)
    storyboard_batch = conn.execute(
        f"""SELECT MIN(run.started_at) AS started_at
              FROM workflow_runs AS run
              JOIN episodes AS episode ON episode.id=run.scope_id
             WHERE run.workflow_type='storyboard' AND run.scope_type='episode'
               AND episode.project_id=? AND run.started_at IS NOT NULL
               AND run.status IN ({marks})""",
        (project_id, *sorted(evidence_repository.ACTIVE_RUN_STATUSES)),
    ).fetchone()

    return {
        # 人物谱没有批次级起点列，只能取最近一次 run；续跑会让它归零，是已知限制。
        "bible": run_timing("character_bible"),
        "refs": batch_timing("character_references", "refs_batch_started_at"),
        "scene_refs": batch_timing("scene_references", "scene_refs_batch_started_at"),
        "screenplay_batch": run_timing("screenplay_batch"),
        "storyboard_batch": {
            "started_at": storyboard_batch["started_at"] if storyboard_batch else None,
            "finished_at": None,
        },
        # 分集规划不在此列：run_regex_plan 是确定性正则切分，毫秒级完成，无需计时。
    }


def _in_chunks(values: Iterable[object], size: int | None = None):
    size = size or _SQLITE_IN_CHUNK_SIZE
    items = list(values)
    for index in range(0, len(items), size):
        yield items[index:index + size]


def _marks(values: list[object]) -> str:
    return ",".join("?" for _ in values)


def _ids_by_in(conn, sql_template: str, values: Iterable[object]) -> set[str]:
    ids: set[str] = set()
    for chunk in _in_chunks(values):
        ids.update(
            row["id"] for row in conn.execute(
                sql_template.format(marks=_marks(chunk)),
                chunk,
            ).fetchall()
        )
    return ids


def _execute_by_in(conn, sql_template: str, values: Iterable[object]) -> int:
    affected = 0
    for chunk in _in_chunks(values):
        cursor = conn.execute(sql_template.format(marks=_marks(chunk)), chunk)
        affected += max(0, cursor.rowcount)
    return affected


def _scope_ids(conn, table: str, *, scope_ids: Iterable[str],
               scope_prefix: str, id_column: str = "id") -> set[str]:
    ids = {
        row[id_column] for row in conn.execute(
            f"SELECT {id_column} FROM {table} WHERE scope_id LIKE ?",
            (f"{scope_prefix}:%",),
        ).fetchall()
    }
    ids.update(_ids_by_in(
        conn,
        f"SELECT {id_column} AS id FROM {table} WHERE scope_id IN ({{marks}})",
        scope_ids,
    ))
    return ids


def _delete_scope_rows(conn, table: str, *, scope_ids: Iterable[str],
                       scope_prefix: str) -> None:
    conn.execute(f"DELETE FROM {table} WHERE scope_id LIKE ?", (f"{scope_prefix}:%",))
    _execute_by_in(
        conn,
        f"DELETE FROM {table} WHERE scope_id IN ({{marks}})",
        scope_ids,
    )


def _present_refs_error(conn, value: str | None) -> str | None:
    """Legacy errors are immutable display data; prose never changes semantics."""
    _ = conn
    return value


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
async def create_project(name: str = Form(...), file: UploadFile = File(...)):
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
            "idempotency_key": f"novel-import:{token}",
        },
        initiator="ui",
    )
    return respond_ui(result)


@router.post("/projects/import")
async def create_project_from_attachment(
    attachment_token: str = Body(...),
    name: str | None = Body(default=None),
):
    """用已上传的附件令牌导入小说，确保批准前后的命令参数保持不变。"""
    from app.capabilities.dispatch import dispatch, respond_ui

    result = await dispatch(
        "project.import_novel",
        {
            "attachment_token": attachment_token,
            "name": name,
            # The one-time attachment token is unique for this import. Reusing
            # it as the command key makes response-loss retries replay-safe.
            "idempotency_key": f"novel-import:{attachment_token}",
        },
        initiator="ui",
    )
    return respond_ui(result)


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

def _attach_character_portraits(conn, project_id: str, bible: dict) -> None:
    """为 bible.characters 挂上 character_portraits 表里的分段定妆照（含多视角）。"""
    from app.portraits import STAGED_INITIAL_EP_START

    try:
        rows = rows_to_dicts(conn.execute(
            "SELECT id, character_name, ep_start, ep_end, appearance, base_portrait_id, image_path, "
            "pack_status, group_qa_json, change_json "
            "FROM character_portraits WHERE project_id=? AND ep_start<>? ORDER BY character_name, ep_start",
            (project_id, STAGED_INITIAL_EP_START)).fetchall())
    except Exception:  # noqa: BLE001
        rows = rows_to_dicts(conn.execute(
            "SELECT id, character_name, ep_start, ep_end, appearance, base_portrait_id, image_path "
            "FROM character_portraits WHERE project_id=? AND ep_start<>? ORDER BY character_name, ep_start",
            (project_id, STAGED_INITIAL_EP_START)).fetchall())
    view_rows = []
    try:
        view_rows = rows_to_dicts(conn.execute(
            """SELECT v.* FROM character_portrait_views v
               JOIN character_portraits p ON p.id=v.portrait_id
               WHERE p.project_id=? AND p.ep_start<>?
               ORDER BY v.portrait_id, v.view_role, v.selected DESC,
                        (v.status='ready') DESC, v.created_at DESC""",
            (project_id, STAGED_INITIAL_EP_START),
        ).fetchall())
    except Exception:  # noqa: BLE001
        view_rows = []
    views_by_portrait: dict[str, list[dict]] = {}
    seen_view_roles: set[tuple[str, str]] = set()
    for v in view_rows:
        view_key = (str(v["portrait_id"]), str(v.get("view_role") or ""))
        if view_key in seen_view_roles:
            continue
        seen_view_roles.add(view_key)
        qa = None
        if v.get("qa_json"):
            try:
                qa = json.loads(v["qa_json"])
            except (TypeError, ValueError):
                qa = None
        views_by_portrait.setdefault(v["portrait_id"], []).append({
            "id": v["id"],
            "view_role": v.get("view_role"),
            "framing": v.get("framing"),
            "status": v.get("status"),
            "image_url": _media_url(v.get("image_path")),
            "qa": qa,
            "qa_overall": (qa or {}).get("overall") if isinstance(qa, dict) else None,
        })
    by_name: dict[str, list[dict]] = {}
    for r in rows:
        group_qa = None
        if r.get("group_qa_json"):
            try:
                group_qa = json.loads(r["group_qa_json"])
            except (TypeError, ValueError):
                group_qa = None
        change = None
        if r.get("change_json"):
            try:
                change = json.loads(r["change_json"])
            except (TypeError, ValueError):
                change = None
        by_name.setdefault(r["character_name"], []).append({
            "id": r["id"], "ep_start": r["ep_start"], "ep_end": r["ep_end"],
            "appearance": r["appearance"], "base_portrait_id": r["base_portrait_id"],
            "image_url": _media_url(r["image_path"]),
            "pack_status": r.get("pack_status"),
            "group_qa": group_qa,
            "change": change,
            "views": views_by_portrait.get(r["id"], []),
        })
    for c in bible.get("characters", []):
        portraits = by_name.get(c.get("name"), [])
        c["portraits"] = portraits
        # ``bible_json.characters[].ref_image_path`` is a compatibility cache,
        # not the source of truth for versioned portraits. A ready pack is
        # committed to ``character_portraits`` before a long batch finishes, so
        # expose that checkpoint immediately instead of leaving the UI gated on
        # a later Bible merge (or process restart).
        if not c.get("ref_image_url"):
            latest_ready = next((
                portrait for portrait in reversed(portraits)
                if portrait.get("pack_status") in (None, "ready")
                and portrait.get("image_url")
            ), None)
            if latest_ready:
                c["ref_image_url"] = latest_ready["image_url"]


def _attach_scene_refs(conn, project_id: str, bible: dict) -> None:
    """为 bible.scenes 挂上 scene_references 表里的分段场景图（含多视角与 QA）。"""
    try:
        rows = rows_to_dicts(conn.execute(
            "SELECT id, scene_name, ep_start, ep_end, scene_canonical, image_path, qa_json, artifact_id, "
            "pack_status, group_qa_json, change_json "
            "FROM scene_references WHERE project_id=? ORDER BY scene_name, ep_start", (project_id,)).fetchall())
    except Exception:  # noqa: BLE001 旧库缺列
        rows = rows_to_dicts(conn.execute(
            "SELECT scene_name, ep_start, ep_end, scene_canonical, image_path, qa_json, artifact_id "
            "FROM scene_references WHERE project_id=? ORDER BY scene_name, ep_start", (project_id,)).fetchall())
    view_rows = []
    try:
        view_rows = rows_to_dicts(conn.execute(
            """SELECT v.* FROM scene_reference_views v
               JOIN scene_references s ON s.id=v.scene_reference_id
               WHERE s.project_id=? ORDER BY v.scene_reference_id, v.created_at""",
            (project_id,),
        ).fetchall())
    except Exception:  # noqa: BLE001
        view_rows = []
    views_by_scene: dict[str, list[dict]] = {}
    for v in view_rows:
        qa = None
        if v.get("qa_json"):
            try:
                qa = json.loads(v["qa_json"])
            except (TypeError, ValueError):
                qa = None
        views_by_scene.setdefault(v["scene_reference_id"], []).append({
            "id": v["id"],
            "view_role": v.get("view_role"),
            "camera_axis": v.get("camera_axis"),
            "status": v.get("status"),
            "image_url": _media_url(v.get("image_path")),
            "qa": qa,
            "qa_overall": (qa or {}).get("overall") if isinstance(qa, dict) else None,
        })
    try:
        reference_rows = rows_to_dicts(conn.execute(
            "SELECT s.scene_name,e.id AS episode_id,e.episode_no,COUNT(*) AS shot_count FROM shots s "
            "JOIN episodes e ON e.id=s.episode_id WHERE e.project_id=? AND s.scene_name IS NOT NULL "
            "AND s.scene_name!='' GROUP BY s.scene_name,e.episode_no ORDER BY e.episode_no",
            (project_id,),
        ).fetchall())
    except Exception:  # noqa: BLE001 兼容精简测试库/历史库
        reference_rows = []
    references_by_name: dict[str, list[dict]] = {}
    for item in reference_rows:
        references_by_name.setdefault(item["scene_name"], []).append(item)
    by_name: dict[str, list[dict]] = {}
    for r in rows:
        qa = None
        if r.get("qa_json"):
            try:
                qa = json.loads(r["qa_json"])
            except (TypeError, ValueError):
                qa = None
        evidence = evidence_repository.get_artifact(r["artifact_id"]) if r.get("artifact_id") else None
        if evidence:
            evidence["evaluations"] = evidence_repository.get_evaluations(evidence["id"])
        group_qa = None
        if r.get("group_qa_json"):
            try:
                group_qa = json.loads(r["group_qa_json"])
            except (TypeError, ValueError):
                group_qa = None
        change = None
        if r.get("change_json"):
            try:
                change = json.loads(r["change_json"])
            except (TypeError, ValueError):
                change = None
        segment_references = [
            item for item in references_by_name.get(r["scene_name"], [])
            if int(item["episode_no"]) >= int(r["ep_start"] or 1)
            and (r["ep_end"] is None or int(item["episode_no"]) <= int(r["ep_end"]))
        ]
        by_name.setdefault(r["scene_name"], []).append({
            "id": r.get("id"),
            "ep_start": r["ep_start"], "ep_end": r["ep_end"],
            "scene_canonical": r["scene_canonical"], "image_url": _media_url(r["image_path"]),
            "qa": qa, "qa_overall": (qa or {}).get("overall") if isinstance(qa, dict) else None,
            "artifact_id": r.get("artifact_id"), "evidence": evidence,
            "pack_status": r.get("pack_status"),
            "group_qa": group_qa,
            "change": change,
            "reference_summary": {
                "episode_numbers": [int(item["episode_no"]) for item in segment_references],
                "episodes": [{"id": item["episode_id"], "episode_no": int(item["episode_no"])}
                             for item in segment_references],
                "shot_count": sum(int(item["shot_count"] or 0) for item in segment_references),
            },
            "views": views_by_scene.get(r.get("id") or "", []),
        })
    candidate_by_name: dict[str, list[dict]] = {}
    artifact_rows = rows_to_dicts(conn.execute(
        """SELECT * FROM artifacts
           WHERE type='scene_reference' AND scope_type='reference_asset' AND scope_id LIKE ?
           ORDER BY created_at, version""",
        (f"{project_id}:%",),
    ).fetchall())
    for artifact in artifact_rows:
        try:
            content = json.loads(artifact.get("content_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            content = {}
        scene_name = str(content.get("scene_name") or "").strip()
        if not scene_name:
            continue
        artifact["evaluations"] = evidence_repository.get_evaluations(artifact["id"])
        artifact.pop("content_json", None)
        candidate_by_name.setdefault(scene_name, []).append({
            "artifact_id": artifact["id"],
            "status": artifact["status"],
            "trust_level": artifact["trust_level"],
            "attempt": content.get("attempt"),
            "image_url": _media_url(artifact.get("file_path")),
            "evidence": artifact,
        })
    for s in bible.get("scenes", []):
        segs = by_name.get(s.get("name"), [])
        s["scene_refs"] = segs
        s["scene_candidates"] = candidate_by_name.get(s.get("name"), [])
        if not s.get("ref_image_url"):
            latest = next((seg for seg in reversed(segs) if seg.get("image_url")), None)
            if latest:
                s["ref_image_url"] = latest["image_url"]


_PICKER_COLUMNS = "id, episode_no, title, status, screenplay_status"

_PICKER_GENERATION_COLUMNS = """e.id, e.episode_no, e.title, e.status, e.screenplay_status,
    (SELECT COUNT(*) FROM shots s WHERE s.episode_id=e.id) AS shot_count,
    (SELECT COUNT(*) FROM shots s WHERE s.episode_id=e.id AND s.adopted_version_id IS NOT NULL) AS video_count,
    (SELECT COUNT(*) FROM shots s WHERE s.episode_id=e.id AND s.adopted_version_id IS NULL
       AND EXISTS(SELECT 1 FROM shot_versions v WHERE v.shot_id=s.id AND v.status='succeeded')) AS pending_adoption_count,
    (SELECT COUNT(*) FROM shot_versions v JOIN shots s ON s.id=v.shot_id
       WHERE s.episode_id=e.id AND v.status='failed') AS failed_count"""

# 与前端 episodePicker.filterEpisodeOptions 的制作状态筛选一一对应。
_PRODUCTION_FILTER_SQL = {
    "with_video": "video_count > 0",
    "pending_adoption": "pending_adoption_count > 0",
    "failed": "failed_count > 0",
    "unproduced": "(shot_count = 0 OR video_count = 0)",
}

_PICKER_MAX_LIMIT = 200


def _attach_picker_episodes(
    conn,
    payload: dict,
    project_id: str,
    *,
    with_production_counts: bool,
    limit: int = 0,
    keyword: str = "",
    cursor: str = "",
    production_filter: str = "all",
) -> None:
    """分集切换器的数据源。

    ``limit<=0`` 返回整份分集，保持旧契约。``limit>0`` 只返回一个窗口：
    1616 集的项目整份 payload 未压缩 250KB，其中中文标题占 72KB 且 gzip 压不动，
    而下拉最多只展示 60 条——搜索、制作状态筛选、取窗因此全部下沉到服务端。

    窗口之外仍要保证三件事可用，故一并返回：总集数、光标所在序号，以及
    上一集/下一集（按全量顺序算，不受搜索与筛选影响）。光标分集本身始终包含
    在 ``episodes`` 里，这样前端 ``resolveWindowedEpisodeId`` 的语义不用改。
    """
    base = (
        f"SELECT {_PICKER_GENERATION_COLUMNS} FROM episodes e WHERE e.project_id=?"
        if with_production_counts
        else f"SELECT {_PICKER_COLUMNS} FROM episodes WHERE project_id=?"
    )
    if limit <= 0:
        payload["episodes"] = rows_to_dicts(
            conn.execute(f"{base} ORDER BY episode_no", (project_id,)).fetchall()
        )
        return

    limit = max(1, min(int(limit), _PICKER_MAX_LIMIT))
    kw = (keyword or "").strip().lower()
    predicate = (
        _PRODUCTION_FILTER_SQL.get(production_filter or "all")
        if with_production_counts
        else None
    )

    clauses: list[str] = []
    params: list[object] = [project_id]
    if kw:
        clauses.append("(LOWER(title) LIKE ? OR CAST(episode_no AS TEXT) LIKE ?)")
        params.extend([f"%{kw}%", f"%{kw}%"])
    if predicate:
        clauses.append(predicate)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    windowed = f"SELECT * FROM ({base}){where}"

    total = int(conn.execute(
        "SELECT COUNT(*) AS c FROM episodes WHERE project_id=?", (project_id,)
    ).fetchone()["c"])
    if predicate:
        # 制作状态筛选依赖派生列，只能包一层统计；无筛选时走轻量的直接统计。
        match_total = int(conn.execute(
            f"SELECT COUNT(*) AS c FROM ({windowed})", params
        ).fetchone()["c"])
    elif kw:
        match_total = int(conn.execute(
            "SELECT COUNT(*) AS c FROM episodes WHERE project_id=? "
            "AND (LOWER(title) LIKE ? OR CAST(episode_no AS TEXT) LIKE ?)",
            (project_id, f"%{kw}%", f"%{kw}%"),
        ).fetchone()["c"])
    else:
        match_total = total

    cursor_row = None
    index = prev_row = next_row = None
    if cursor:
        cursor_row = conn.execute(
            f"SELECT * FROM ({base}) WHERE id=?", (project_id, cursor)
        ).fetchone()
    if cursor_row is not None:
        episode_no = cursor_row["episode_no"]
        index = int(conn.execute(
            "SELECT COUNT(*) AS c FROM episodes WHERE project_id=? AND episode_no < ?",
            (project_id, episode_no),
        ).fetchone()["c"])
        prev_row = conn.execute(
            "SELECT id, episode_no, title FROM episodes WHERE project_id=? AND episode_no < ? "
            "ORDER BY episode_no DESC LIMIT 1",
            (project_id, episode_no),
        ).fetchone()
        next_row = conn.execute(
            "SELECT id, episode_no, title FROM episodes WHERE project_id=? AND episode_no > ? "
            "ORDER BY episode_no LIMIT 1",
            (project_id, episode_no),
        ).fetchone()

    # 有搜索或筛选时从头给结果；否则把窗口落在光标附近，保留「打开即定位当前集」。
    if kw or predicate or index is None:
        offset = 0
    else:
        offset = max(0, min(index - limit // 3, max(0, match_total - limit)))

    rows = rows_to_dicts(conn.execute(
        f"{windowed} ORDER BY episode_no LIMIT ? OFFSET ?", (*params, limit, offset)
    ).fetchall())
    if cursor_row is not None and all(row["id"] != cursor for row in rows):
        rows.append(dict(cursor_row))
        rows.sort(key=lambda row: row["episode_no"])

    payload["episodes"] = rows
    payload["episode_total"] = total
    payload["episode_match_total"] = match_total
    payload["episode_offset"] = offset
    payload["episode_index"] = index
    payload["episode_current"] = dict(cursor_row) if cursor_row is not None else None
    payload["episode_prev"] = dict(prev_row) if prev_row is not None else None
    payload["episode_next"] = dict(next_row) if next_row is not None else None


@router.get("/projects/{project_id}")
def project_detail(
    project_id: str,
    view: str | None = None,
    page: int = 1,
    page_size: int = 15,
    query: str = "",
    status_filter: str = "all",
    episode_limit: int = 0,
    episode_query: str = "",
    episode_cursor: str = "",
    episode_filter: str = "all",
):
    if view not in (None, "bible", "scenes", "episodes", "picker", "picker_generation"):
        raise HTTPException(400, f"未知项目视图：{view}")
    full = view is None
    p = dict(_project_or_404(project_id))
    conn = get_conn()
    from app import model_registry

    # 世界书/映射台/分镜台分环节文本模型下拉的可选清单；p 里已经带着三个环节各自
    # 保存的选择（bible_text_provider/script_text_provider/board_text_provider，
    # 空串＝未设置，直接来自 projects 表原始行，不需要额外处理）。清单本身很小，
    # 各视图都带上，不按 view 特判。
    p["text_model_choices"] = model_registry.text_model_choices()
    p["refs_error"] = _present_refs_error(conn, p.get("refs_error"))
    if full or view in ("bible", "scenes", "episodes"):
        p["task_timings"] = _project_task_timings(conn, p)
    include_bible = full or view in ("bible", "scenes")
    p["bible"] = json.loads(p["bible_json"]) if include_bible and p["bible_json"] else None
    bible_artifact = (
        evidence_repository.get_artifact(p.get("bible_artifact_id"))
        if p.get("bible_artifact_id") and (full or view == "bible") else None
    )
    if bible_artifact:
        bible_artifact.pop("content_json", None)
        bible_artifact.pop("content", None)
        bible_artifact["evaluations"] = evidence_repository.get_evaluations(
            bible_artifact["id"]
        )
    p["bible_evidence"] = bible_artifact
    p.pop("bible_json", None)
    if p["bible"]:
        from app.refs import effective_portrait_prompt
        style = p["bible"].get("world", {}).get("visual_style_canonical", "")
        import os
        for c in p["bible"].get("characters", []):
            path_str = c.get("ref_image_path")
            if path_str and os.path.exists(path_str):
                c["ref_image_url"] = build_media_url(path_str, version=int(os.path.getmtime(path_str)))
            else:
                c["ref_image_url"] = None
            override = (c.get("portrait_prompt_override") or "").strip()
            c["portrait_prompt_effective"] = effective_portrait_prompt(
                style, c.get("appearance_canonical", ""), override or None,
            )
        # 场景图素材库：为每个规范场景挂上落盘图 url + QA + 有效生成词，供「场景图」菜单页展示。
        from app.scenes import scene_ref_prompt
        for s in p["bible"].get("scenes", []):
            spath = s.get("ref_image_path")
            if spath and os.path.exists(spath):
                s["ref_image_url"] = build_media_url(spath, version=int(os.path.getmtime(spath)))
            else:
                s["ref_image_url"] = None
            soverride = (s.get("scene_prompt_override") or "").strip()
            s["scene_prompt_effective"] = soverride or scene_ref_prompt(
                style,
                s.get("scene_canonical", ""),
                scene_name=s.get("name", ""),
            )
    p["key_timeline"] = (
        json.loads(p["key_timeline"]) if p["key_timeline"] and (full or view == "bible") else []
    )
    p["chapter_count"] = int(conn.execute(
        "SELECT COUNT(*) AS c FROM chapters WHERE project_id=?", (project_id,)
    ).fetchone()["c"])
    if full:
        p["chapters"] = rows_to_dicts(conn.execute(
            "SELECT idx, title, char_count, summary IS NOT NULL AS has_summary, substr(content,1,200) AS preview "
            "FROM chapters WHERE project_id=? ORDER BY idx",
            (project_id,)).fetchall())
        for ch in p["chapters"]:
            ch["preview"] = chapter_preview(ch.pop("preview", ""))
    else:
        p["chapters"] = []
    # 把每个角色的定妆照分段（适用集区间 + 图生图谱系）挂到 bible.characters 上，供横向预览。
    if p["bible"] and (full or view == "bible"):
        _attach_character_portraits(conn, project_id, p["bible"])
    # The prep navigation is also shown on the character page. Attach current
    # scene-reference status there so it can report actual video usability
    # instead of a stale project-level warning from an older multi-view run.
    if p["bible"] and (full or view in ("bible", "scenes")):
        _attach_scene_refs(conn, project_id, p["bible"])

    if view in ("picker", "picker_generation"):
        # 切换器用不到自动改动流水（13KB），前端也没有任何消费点，别跟着每次切集来回传。
        p.pop("bible_auto_changes_json", None)
        _attach_picker_episodes(
            conn,
            p,
            project_id,
            with_production_counts=view == "picker_generation",
            limit=episode_limit,
            keyword=episode_query,
            cursor=episode_cursor,
            production_filter=episode_filter,
        )
        return p
    if view not in (None, "episodes"):
        p["episodes"] = []
        return p

    if view == "episodes":
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 100))
        clauses = ["project_id=?"]
        params: list[object] = [project_id]
        keyword = query.strip().lower()
        if keyword:
            clauses.append("(LOWER(title) LIKE ? OR CAST(episode_no AS TEXT) LIKE ? OR source_chapters LIKE ?)")
            needle = f"%{keyword}%"
            params.extend((needle, needle, needle))
        if status_filter == "running":
            clauses.append("(screenplay_status='running' OR status IN ('scripting','generating'))")
        elif status_filter == "failed":
            clauses.append("(screenplay_status IN ('failed','repairing') OR status LIKE '%failed%')")
        elif status_filter == "done":
            clauses.append("status='done'")
        elif status_filter == "pending":
            clauses.append("(screenplay_status='pending' OR status IN ('planned','drafting'))")
        elif status_filter != "all":
            raise HTTPException(400, f"未知分集状态筛选：{status_filter}")
        where = " AND ".join(clauses)
        filtered_total = int(conn.execute(
            f"SELECT COUNT(*) AS c FROM episodes WHERE {where}", params
        ).fetchone()["c"])
        offset = (page - 1) * page_size
        p["episodes"] = rows_to_dicts(conn.execute(
            f"SELECT e.*, (SELECT COUNT(*) FROM shots s WHERE s.episode_id=e.id) AS shot_count "
            f"FROM episodes e WHERE {where} ORDER BY episode_no LIMIT ? OFFSET ?",
            [*params, page_size, offset],
        ).fetchall())
        p["episodes_total"] = filtered_total
        p["episodes_page"] = page
        p["episodes_page_count"] = max(1, (filtered_total + page_size - 1) // page_size)
        p["episodes_query"] = keyword
        p["episodes_status_filter"] = status_filter
        counts = conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done,
                      SUM(CASE WHEN screenplay_status='queued' THEN 1 ELSE 0 END) AS screenplay_queued,
                      SUM(CASE WHEN screenplay_status='running' THEN 1 ELSE 0 END) AS screenplay_running,
                      SUM(CASE WHEN status='scripting' THEN 1 ELSE 0 END) AS scripting,
                      SUM(CASE WHEN screenplay_status IN ('pending','failed','repairing')
                                OR (
                                    screenplay_json IS NULL
                                    AND screenplay_status NOT IN ('queued','running')
                                ) THEN 1 ELSE 0 END) AS screenplay_todo,
                      SUM(CASE WHEN screenplay_status='ready'
                                AND status IN ('planned','script_failed') THEN 1 ELSE 0 END) AS storyboard_ready
               FROM episodes WHERE project_id=?""",
            (project_id,),
        ).fetchone()
        p["episode_counts"] = {key: int(counts[key] or 0) for key in counts.keys()}
        p["episodes_busy"] = bool(
            p["plan_status"] == "running"
            or p["episode_counts"]["screenplay_queued"]
            or p["episode_counts"]["screenplay_running"]
            or p["episode_counts"]["scripting"]
        )
    else:
        p["episodes"] = rows_to_dicts(conn.execute(
            "SELECT * FROM episodes WHERE project_id=? ORDER BY episode_no", (project_id,)).fetchall())
    page_costs: dict[str, float] = {}
    if view == "episodes" and p["episodes"]:
        episode_ids = [ep["id"] for ep in p["episodes"]]
        marks = ",".join("?" for _ in episode_ids)
        cost_rows = conn.execute(
            f"""SELECT s.episode_id, COALESCE(SUM(v.cost_cny), 0) AS cost_cny
                 FROM shots s
                 JOIN shot_versions v ON v.shot_id=s.id
                 WHERE s.episode_id IN ({marks})
                   AND v.status IN ('succeeded', 'running', 'queued')
                 GROUP BY s.episode_id""",
            episode_ids,
        ).fetchall()
        page_costs = {row["episode_id"]: float(row["cost_cny"] or 0) for row in cost_rows}
    for ep in p["episodes"]:
        ep["source_chapters"] = json.loads(ep["source_chapters"] or "[]")
        if ep.get("screenplay_json"):
            try:
                script = EpisodeScreenplay.model_validate(json.loads(ep["screenplay_json"]))
                ep["screenplay_title"] = script.title or ep["title"]
            except (json.JSONDecodeError, TypeError, ValueError):
                ep["screenplay_title"] = ep["title"]
        else:
            ep["screenplay_title"] = ep["title"]
        ep.pop("screenplay_json", None)
        outline_raw = ep.pop("storyboard_outline_json", None)
        try:
            _outline = json.loads(outline_raw) if outline_raw else None
        except (TypeError, ValueError):
            _outline = None
        ep["storyboard_planned_shots"] = len(_outline["shots"]) if _outline and _outline.get("shots") else None
        ep["cost_cny"] = (
            page_costs.get(ep["id"], 0.0)
            if view == "episodes" else worker.episode_cost(ep["id"])
        )
    if view == "episodes":
        chapter_ids = sorted({
            int(ep["source_chapters"][0])
            for ep in p["episodes"] if ep.get("source_chapters")
        })
        if chapter_ids:
            marks = ",".join("?" for _ in chapter_ids)
            p["chapters"] = rows_to_dicts(conn.execute(
                f"SELECT idx, title, char_count, substr(content,1,200) AS preview "
                f"FROM chapters WHERE project_id=? AND idx IN ({marks}) ORDER BY idx",
                [project_id, *chapter_ids],
            ).fetchall())
            for chapter in p["chapters"]:
                chapter["preview"] = chapter_preview(chapter.get("preview") or "")
        first = conn.execute(
            "SELECT MIN(idx) AS idx FROM chapters WHERE project_id=?", (project_id,)
        ).fetchone()
        p["first_chapter_idx"] = first["idx"]
    return p


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


def _delete_scoped_evidence(
    conn,
    *,
    scope_ids: list[str],
    scope_prefix: str,
    episode_ids: list[str],
) -> dict[str, int]:
    """Delete Harness evidence owned by one project or episode subtree."""
    run_ids = _scope_ids(
        conn,
        "workflow_runs",
        scope_ids=scope_ids,
        scope_prefix=scope_prefix,
    )
    # Recovery/child runs can use their own scope. Include the whole descendant
    # chain so no run keeps a parent pointer to a deleted project run.
    frontier = set(run_ids)
    while frontier:
        children: set[str] = set()
        for chunk in _in_chunks(frontier):
            marks = _marks(chunk)
            children.update(
                row["id"] for row in conn.execute(
                    f"""SELECT id FROM workflow_runs
                        WHERE parent_run_id IN ({marks})
                           OR recovered_by_run_id IN ({marks})""",
                    [*chunk, *chunk],
                ).fetchall()
            )
        children -= run_ids
        run_ids.update(children)
        frontier = children

    step_ids: set[str] = set()
    if run_ids:
        step_ids = _ids_by_in(
            conn,
            "SELECT id FROM step_runs WHERE run_id IN ({marks})",
            run_ids,
        )

    artifact_ids = _scope_ids(
        conn,
        "artifacts",
        scope_ids=scope_ids,
        scope_prefix=scope_prefix,
    )
    if step_ids:
        artifact_ids.update(_ids_by_in(
            conn,
            "SELECT id FROM artifacts WHERE created_by_step_run_id IN ({marks})",
            step_ids,
        ))
    provider_call_ids: set[object] = set()
    if run_ids:
        provider_call_ids.update(_ids_by_in(
            conn,
            "SELECT id FROM provider_calls WHERE run_id IN ({marks})",
            run_ids,
        ))
    if step_ids:
        provider_call_ids.update(_ids_by_in(
            conn,
            "SELECT id FROM provider_calls WHERE step_run_id IN ({marks})",
            step_ids,
        ))

    if episode_ids:
        _execute_by_in(
            conn,
            "DELETE FROM delivery_packages WHERE episode_id IN ({marks})",
            episode_ids,
        )
        _execute_by_in(
            conn,
            "DELETE FROM customer_feedback WHERE episode_id IN ({marks})",
            episode_ids,
        )
        _execute_by_in(
            conn,
            "DELETE FROM production_revisions WHERE episode_id IN ({marks})",
            episode_ids,
        )
        _execute_by_in(
            conn,
            "DELETE FROM production_grants WHERE episode_id IN ({marks})",
            episode_ids,
        )
        _execute_by_in(
            conn,
            "DELETE FROM completion_grants WHERE episode_id IN ({marks})",
            episode_ids,
        )
        _execute_by_in(
            conn,
            "DELETE FROM completion_certificates WHERE scope_id IN ({marks})",
            episode_ids,
        )

    if artifact_ids:
        _execute_by_in(
            conn,
            "DELETE FROM gate_decisions WHERE artifact_id IN ({marks})",
            artifact_ids,
        )
        _execute_by_in(
            conn,
            "DELETE FROM evaluations WHERE artifact_id IN ({marks})",
            artifact_ids,
        )
        _execute_by_in(
            conn,
            "DELETE FROM completion_certificates WHERE artifact_id IN ({marks})",
            artifact_ids,
        )
        _execute_by_in(
            conn,
            "UPDATE artifacts SET superseded_by_artifact_id=NULL "
            "WHERE superseded_by_artifact_id IN ({marks})",
            artifact_ids,
        )
        _execute_by_in(
            conn,
            "DELETE FROM artifacts WHERE id IN ({marks})",
            artifact_ids,
        )

    if run_ids:
        _execute_by_in(conn, "DELETE FROM gate_decisions WHERE run_id IN ({marks})", run_ids)
        _execute_by_in(conn, "DELETE FROM run_events WHERE run_id IN ({marks})", run_ids)
        _execute_by_in(conn, "DELETE FROM agent_tool_calls WHERE run_id IN ({marks})", run_ids)
    if step_ids:
        _execute_by_in(conn, "DELETE FROM evaluations WHERE step_run_id IN ({marks})", step_ids)
        _execute_by_in(conn, "DELETE FROM run_events WHERE step_run_id IN ({marks})", step_ids)
    if provider_call_ids:
        _execute_by_in(
            conn,
            "UPDATE provider_calls SET supersedes_call_id=NULL "
            "WHERE supersedes_call_id IN ({marks})",
            provider_call_ids,
        )
        _execute_by_in(
            conn,
            "UPDATE provider_calls SET superseded_by_call_id=NULL "
            "WHERE superseded_by_call_id IN ({marks})",
            provider_call_ids,
        )
        _execute_by_in(conn, "DELETE FROM provider_calls WHERE id IN ({marks})", provider_call_ids)
    if run_ids:
        _execute_by_in(conn, "DELETE FROM step_runs WHERE run_id IN ({marks})", run_ids)
        _execute_by_in(
            conn,
            "UPDATE workflow_runs SET parent_run_id=NULL "
            "WHERE parent_run_id IN ({marks})",
            run_ids,
        )
        _execute_by_in(
            conn,
            "UPDATE workflow_runs SET recovered_by_run_id=NULL "
            "WHERE recovered_by_run_id IN ({marks})",
            run_ids,
        )
        _execute_by_in(conn, "DELETE FROM workflow_runs WHERE id IN ({marks})", run_ids)

    _delete_scope_rows(
        conn,
        "review_action_audit",
        scope_ids=scope_ids,
        scope_prefix=scope_prefix,
    )
    return {
        "artifacts": len(artifact_ids),
        "runs": len(run_ids),
        "steps": len(step_ids),
    }


def _delete_project_evidence(conn, project_id: str) -> dict[str, int]:
    """Delete project-scoped Harness evidence before removing business rows."""
    episode_ids = [
        row["id"] for row in conn.execute(
            "SELECT id FROM episodes WHERE project_id=?", (project_id,)
        ).fetchall()
    ]
    shot_ids = [
        row["id"] for row in conn.execute(
            """SELECT s.id FROM shots s
               JOIN episodes e ON e.id=s.episode_id WHERE e.project_id=?""",
            (project_id,),
        ).fetchall()
    ]
    return _delete_scoped_evidence(
        conn,
        scope_ids=[project_id, *episode_ids, *shot_ids],
        scope_prefix=project_id,
        episode_ids=episode_ids,
    )


def _delete_episode_evidence(conn, episode_id: str) -> dict[str, int]:
    """Delete only the evidence rooted at one episode and its shots."""
    shot_ids = [
        row["id"] for row in conn.execute(
            "SELECT id FROM shots WHERE episode_id=?", (episode_id,)
        ).fetchall()
    ]
    return _delete_scoped_evidence(
        conn,
        scope_ids=[episode_id, *shot_ids],
        scope_prefix=episode_id,
        episode_ids=[episode_id],
    )


def _json_with_episode_number(value: str | None, episode_no: int) -> str | None:
    """Keep mutable screenplay projections aligned with their episode row."""
    if not value:
        return value
    try:
        payload = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return value
    if not isinstance(payload, dict):
        return value
    payload["episode_no"] = episode_no
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _compact_asset_episode_ranges(
    conn,
    *,
    table: str,
    name_column: str,
    project_id: str,
    surviving_numbers: list[int],
) -> dict[str, int]:
    """Project an asset's inclusive episode range onto a compacted sequence."""
    from bisect import bisect_left, bisect_right

    rows = conn.execute(
        f"SELECT id,{name_column} AS asset_name,ep_start,ep_end "
        f"FROM {table} WHERE project_id=? ORDER BY ep_start,id",
        (project_id,),
    ).fetchall()
    mapped: list[dict] = []
    deleted_ids: set[str] = set()
    for row in rows:
        old_start = int(row["ep_start"])
        old_end = int(row["ep_end"]) if row["ep_end"] is not None else None
        if old_start <= 0:
            new_start = old_start
            new_end = old_end
            if old_end is not None and old_end > 0:
                new_end = bisect_right(surviving_numbers, old_end)
        else:
            left = bisect_left(surviving_numbers, old_start)
            if left >= len(surviving_numbers) or (
                old_end is not None and surviving_numbers[left] > old_end
            ):
                deleted_ids.add(row["id"])
                continue
            new_start = left + 1
            new_end = (
                bisect_right(surviving_numbers, old_end)
                if old_end is not None
                else None
            )
        if new_end is not None and new_start > new_end:
            deleted_ids.add(row["id"])
            continue
        mapped.append({
            "id": row["id"],
            "asset_name": row["asset_name"],
            "old_start": old_start,
            "old_end": old_end,
            "new_start": new_start,
            "new_end": new_end,
        })

    # Legacy overlapping ranges can collapse onto the same unique start after a
    # gap is removed. The latest old range is the one that governed the first
    # surviving episode, so retain it deterministically.
    by_key: dict[tuple[str, int], list[dict]] = {}
    for item in mapped:
        by_key.setdefault((item["asset_name"], item["new_start"]), []).append(item)
    for candidates in by_key.values():
        if len(candidates) <= 1:
            continue
        keep = max(candidates, key=lambda item: (item["old_start"], item["id"]))
        deleted_ids.update(item["id"] for item in candidates if item is not keep)
    mapped = [item for item in mapped if item["id"] not in deleted_ids]

    if deleted_ids:
        marks = ",".join("?" for _ in deleted_ids)
        conn.execute(f"DELETE FROM {table} WHERE id IN ({marks})", sorted(deleted_ids))

    updates = [
        item for item in mapped
        if item["old_start"] != item["new_start"] or item["old_end"] != item["new_end"]
    ]
    temporary_base = max([*surviving_numbers, 0]) + 1_000_000
    for index, item in enumerate(updates, start=1):
        conn.execute(
            f"UPDATE {table} SET ep_start=? WHERE id=?",
            (temporary_base + index, item["id"]),
        )
    for item in updates:
        conn.execute(
            f"UPDATE {table} SET ep_start=?,ep_end=? WHERE id=?",
            (item["new_start"], item["new_end"], item["id"]),
        )
    return {"updated": len(updates), "deleted": len(deleted_ids)}


def _replace_episode_path_prefixes(
    conn,
    *,
    project_id: str,
    number_changes: list[tuple[int, int]],
) -> int:
    """Rewrite operational file references after episode directories move."""
    path_columns = {
        "artifacts": ("file_path",),
        "delivery_packages": ("package_path", "manifest_json", "quality_report_json"),
        "evaluations": ("evidence_json",),
        "reference_assets": ("path", "dependency_manifest_json"),
        "shot_scenes": ("image_path",),
        "shot_versions": (
            "video_path",
            "last_frame_url",
            "technical_validation_json",
            "image_inputs",
        ),
    }
    available_tables = {
        row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    changed = 0
    for table, columns in path_columns.items():
        if table not in available_tables:
            continue
        available_columns = {
            row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for old_no, new_no in number_changes:
            old_prefix = str(config.PROJECTS_DIR / project_id / "episodes" / str(old_no))
            new_prefix = str(config.PROJECTS_DIR / project_id / "episodes" / str(new_no))
            for column in columns:
                if column not in available_columns:
                    continue
                cursor = conn.execute(
                    f"UPDATE {table} SET {column}=REPLACE({column}, ?, ?) "
                    f"WHERE {column} LIKE ?",
                    (old_prefix, new_prefix, f"%{old_prefix}%"),
                )
                changed += max(0, cursor.rowcount)
    return changed


def _compact_project_episode_numbers(conn, project_id: str) -> dict[str, object]:
    """Renumber surviving episodes densely while preserving stable episode IDs."""
    episodes = conn.execute(
        "SELECT id,episode_no,screenplay_json,storyboard_outline_json "
        "FROM episodes WHERE project_id=? ORDER BY episode_no,id",
        (project_id,),
    ).fetchall()
    surviving_numbers = [int(row["episode_no"]) for row in episodes]
    changes = [
        (row, new_no)
        for new_no, row in enumerate(episodes, start=1)
        if int(row["episode_no"]) != new_no
    ]
    if not changes:
        return {
            "renumbered": 0,
            "directories_moved": 0,
            "path_references_updated": 0,
            "character_ranges": {"updated": 0, "deleted": 0},
            "scene_ranges": {"updated": 0, "deleted": 0},
        }

    episode_root = config.PROJECTS_DIR / project_id / "episodes"
    number_changes = [(int(row["episode_no"]), new_no) for row, new_no in changes]
    source_directories = {episode_root / str(old_no) for old_no, _ in number_changes}
    for _, new_no in number_changes:
        destination = episode_root / str(new_no)
        if destination.exists() and destination not in source_directories:
            raise RuntimeError(f"分集目录重编号目标已存在：{destination}")

    directory_moves: list[tuple[Path, Path, Path]] = []
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if episode_root.exists():
            for old_no, new_no in number_changes:
                source = episode_root / str(old_no)
                if not source.exists():
                    continue
                temporary = episode_root / f".{new_id('renumber')}-{old_no}"
                source.rename(temporary)
                directory_moves.append((source, temporary, episode_root / str(new_no)))

        temporary_base = max(surviving_numbers) + len(episodes) + 1_000_000
        for index, (row, _) in enumerate(changes, start=1):
            conn.execute(
                "UPDATE episodes SET episode_no=? WHERE id=?",
                (temporary_base + index, row["id"]),
            )
        for row, new_no in changes:
            conn.execute(
                "UPDATE episodes SET episode_no=? WHERE id=?",
                (new_no, row["id"]),
            )
            # Published screenplay/storyboard JSON is an immutable projection
            # of content-addressed artifacts.  Episode numbering is display
            # metadata keyed by the stable episode id; renumbering must not
            # silently rewrite certified narrative content.
            draft = conn.execute(
                "SELECT content_json FROM screenplay_drafts WHERE episode_id=?",
                (row["id"],),
            ).fetchone()
            if draft:
                conn.execute(
                    "UPDATE screenplay_drafts SET content_json=? WHERE episode_id=?",
                    (_json_with_episode_number(draft["content_json"], new_no), row["id"]),
                )

        character_ranges = _compact_asset_episode_ranges(
            conn,
            table="character_portraits",
            name_column="character_name",
            project_id=project_id,
            surviving_numbers=surviving_numbers,
        )
        scene_ranges = _compact_asset_episode_ranges(
            conn,
            table="scene_references",
            name_column="scene_name",
            project_id=project_id,
            surviving_numbers=surviving_numbers,
        )
        path_references_updated = _replace_episode_path_prefixes(
            conn,
            project_id=project_id,
            number_changes=number_changes,
        )
        for _, temporary, destination in directory_moves:
            temporary.rename(destination)
        conn.commit()
    except Exception:
        conn.rollback()
        for source, temporary, destination in reversed(directory_moves):
            try:
                if destination.exists():
                    destination.rename(source)
                elif temporary.exists():
                    temporary.rename(source)
            except OSError:
                # Preserve the original exception. Any stranded hidden directory
                # is intentionally not deleted so its media remains recoverable.
                pass
        raise

    return {
        "renumbered": len(changes),
        "directories_moved": len(directory_moves),
        "path_references_updated": path_references_updated,
        "character_ranges": character_ranges,
        "scene_ranges": scene_ranges,
    }


def _assert_no_other_episode_work(project_id: str, deleting_episode_id: str) -> None:
    """Avoid renumbering paths while another episode is actively writing them."""
    from app.planning import ACTIVE_MEDIA_JOB_STATUSES

    conn = get_conn()
    marks = ",".join("?" for _ in ACTIVE_MEDIA_JOB_STATUSES)
    active_job = conn.execute(
        f"""SELECT id FROM jobs
             WHERE project_id=? AND episode_id!=? AND status IN ({marks})
             LIMIT 1""",
        (project_id, deleting_episode_id, *sorted(ACTIVE_MEDIA_JOB_STATUSES)),
    ).fetchone()
    other_episode_ids = [
        row["id"] for row in conn.execute(
            "SELECT id FROM episodes WHERE project_id=? AND id!=?",
            (project_id, deleting_episode_id),
        ).fetchall()
    ]
    active_task = any(
        task_registry.active(kind, episode_id)
        for episode_id in other_episode_ids
        for kind in ("screenplay", "storyboard", "video_completion")
    )
    if active_job or active_task:
        raise HTTPException(
            409,
            "项目内其他分集仍在生成，请先等待完成或停止任务，再删除并自动重排集号",
        )


async def _delete_episode_core(episode_id: str) -> dict:
    """Permanently remove one episode and every downstream production asset."""
    from app.completion_grant import (
        ProviderTasksNotTerminalError,
        reconcile_provider_tasks_for_clear,
    )

    ep = dict(_episode_or_404(episode_id))
    project = get_conn().execute(
        "SELECT plan_status FROM projects WHERE id=?", (ep["project_id"],)
    ).fetchone()
    if task_registry.active("plan", ep["project_id"]) or (
        project and project["plan_status"] == "running"
    ):
        raise HTTPException(409, "分集规划正在运行，请等待完成后再删除单集")
    _assert_no_other_episode_work(ep["project_id"], episode_id)
    # 与 _delete_project_core 同一个理由：删除前先做一次只读式核对，把供应商
    # 自己已经确认终态、只是本地还没结算的任务先落定，减少用户被
    # PROVIDER_TASKS_NOT_TERMINAL 挡住却无法自愈的情况（真正仍在途的任务
    # 依旧原样挡下，不受影响）。
    await reconcile_provider_tasks_for_clear(
        episode_id=episode_id,
        conn=get_conn(),
        evidence_source="episode_delete_terminal_reconcile",
    )

    cancelled_tasks = 0
    for kind in ("screenplay", "storyboard", "video_completion"):
        cancelled_tasks += int(await task_registry.cancel_and_wait(kind, episode_id))

    conn = get_conn()
    # Cancellation finalizers may have refreshed the episode projection. Recheck
    # existence before deleting its immutable evidence and generated media.
    ep = conn.execute(
        "SELECT id,project_id,episode_no,title FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    if not ep:
        raise HTTPException(404, f"分集不存在：{episode_id}")
    # 与 _delete_project_core 同一个理由、同一种写法：_delete_episode_evidence →
    # worker.delete_episode_shots → DELETE episodes → commit 中途任何未捕获异常
    # 冒到 app/main.py 的全局处理器都会调 errors.log_error，而 log_error 目前在
    # 调用方的 task 缓存连接上隐式 commit——谁先提交谁定型，回滚必须在那之前，
    # 且必须是 except 分支的第一条语句（同一顺序要求见 _storyboard_task 顶层
    # except 分支上方的大注释）。这里只加回滚兜底，不改变任何拦截判定：内层
    # ProviderTasksNotTerminalError 分支仍然优先命中并把 409 转换好，外层
    # except 只在它重新抛出的 HTTPException 上做一次空操作回滚（此时事务已经
    # 不在途）再原样重新抛出，不会把 409 吞成别的状态码。
    try:
        evidence_removed = _delete_episode_evidence(conn, episode_id)
        try:
            worker.delete_episode_shots(episode_id)
        except ProviderTasksNotTerminalError as exc:
            if conn.in_transaction:
                conn.rollback()
            raise HTTPException(409, exc.detail) from exc
        conn.execute("DELETE FROM episodes WHERE id=?", (episode_id,))
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise

    import shutil
    shutil.rmtree(
        config.PROJECTS_DIR / ep["project_id"] / "episodes" / str(ep["episode_no"]),
        ignore_errors=True,
    )
    compaction = _compact_project_episode_numbers(conn, ep["project_id"])
    return {
        "deleted": episode_id,
        "project_id": ep["project_id"],
        "episode_no": ep["episode_no"],
        "title": ep["title"],
        "cancelled_tasks": cancelled_tasks,
        "evidence_removed": evidence_removed,
        **compaction,
    }


@router.delete("/episodes/{episode_id}")
async def delete_episode(episode_id: str):
    return await _delete_episode_core(episode_id)


def _deleted_project_or_404(project_id: str) -> dict:
    """回收站专用存在性校验：只认已软删除（``deleted_at`` 非空）的项目。

    与 ``_project_or_404``（app.domain.common）互补而非重复：那个函数只认
    "正常"（未删除）项目，这个只认"回收站里"的项目——恢复/彻底清理必须落在
    这条判据上，否则一个还没删除的项目也能被拿来"彻底清理"。
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM projects WHERE id=? AND deleted_at IS NOT NULL", (project_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, f"回收站中不存在该项目：{project_id}")
    # 与 ``_project_or_404`` 同一条归属判据，理由也相同：HTTP 边界的
    # ``require_project_owner_access`` 只在请求经 ASGI 路由时执行，Agent/MCP
    # 工具调用、内部脚本、测试直接进 domain 函数会完全绕过它。缺了这一行，
    # 「恢复」与「彻底清理」两个端点在非 HTTP 路径上就能按 id 操作**别人**
    # 回收站里的项目——而彻底清理是不可逆的（删库行 + rmtree 产物目录）。
    # 静态 SQL 守卫看不见这个洞：它对 ``WHERE id=?`` 按「主键锚定」放行，
    # 前提是「这个 id 进系统时已过归属闸门」，而这条路径上没有。
    _assert_principal_owns(
        row["owner_user_id"], not_found_detail=f"回收站中不存在该项目：{project_id}"
    )
    return dict(row)


async def _delete_project_core(project_id: str) -> dict:
    """软删除的领域逻辑，供 REST 路由与 ``project.delete`` Command Handler 共用。

    只把项目标记进回收站（``deleted_at``），不删除任何数据库行、不碰磁盘文件。
    24 小时后由 ``sweep_expired_deleted_projects`` 自动彻底清理，用户也可以
    随时在回收站里手动恢复或彻底清理——见 ``_restore_project_core`` /
    ``_purge_project_core``。

    在途任务的处理：与旧版硬删除一致，先核对供应商付费任务是否已到终态、
    再取消项目级后台协程——回收站里的项目不应该继续烧算力。这一步失败
    （``ProviderTasksNotTerminalError``）会整体拒绝这次软删除，用户需要等
    任务到终态或去控制台核对后重试；不提供强制忽略。
    """
    from app.completion_grant import (
        assert_provider_tasks_clearable,
        prepare_provider_tasks_for_clear,
        reconcile_project_provider_tasks_for_clear,
    )

    _project_or_404(project_id)
    provider_reconciliation = await reconcile_project_provider_tasks_for_clear(
        project_id,
        conn=get_conn(),
        evidence_source="project_soft_delete_terminal_reconcile",
    )
    # Fast preflight before cancelling any producer. The authoritative check is
    # repeated inside the update transaction after all local writers stop.
    assert_provider_tasks_clearable(
        project_id=project_id,
        conn=get_conn(),
    )
    # 先停止并等待所有项目级后台协程退出，防止回收站里的项目继续跑生成/烧算力；
    # 被取消的任务各自的 CancelledError 处理器会把 bible_status 等字段翻成终态
    # （见 app/domain/bible_ops/task_run.py），启动恢复扫描因此不会把它当成
    # "重启丢失的在途任务"再拉起来。
    cancelled_tasks = await task_registry.cancel_project(project_id)
    conn = get_conn()
    stamp = now()
    try:
        prepare_provider_tasks_for_clear(
            project_id=project_id,
            conn=conn,
        )
        cur = conn.execute(
            "UPDATE projects SET deleted_at=? WHERE id=? AND deleted_at IS NULL",
            (stamp, project_id),
        )
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    if cur.rowcount != 1:
        # 与另一次并发的软删除请求赛跑输了：对方已经先把它标进回收站。
        raise HTTPException(409, "项目已在回收站中")
    return {
        "deleted": project_id,
        "deleted_at": stamp,
        "purge_at": stamp + PROJECT_RECYCLE_BIN_RETENTION_S,
        "cancelled_tasks": cancelled_tasks,
        "provider_reconciliation": provider_reconciliation,
    }


async def _restore_project_core(project_id: str) -> dict:
    """把项目从回收站恢复成正常项目：清空 ``deleted_at``，不改动其余任何数据。"""
    _deleted_project_or_404(project_id)
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE projects SET deleted_at=NULL WHERE id=? AND deleted_at IS NOT NULL",
            (project_id,),
        )
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    if cur.rowcount != 1:
        raise HTTPException(404, f"回收站中不存在该项目：{project_id}")
    return {"restored": project_id}


async def _purge_project_core(project_id: str) -> dict:
    """彻底清理：只对已在回收站的项目生效，物理删除数据库行与磁盘产物。

    破坏性操作的原子性：数据库删除全部提交成功之后才执行 ``shutil.rmtree``；
    数据库提交失败（异常/回滚）时磁盘上一个文件都不会被动。反过来的顺序
    （先删文件）一旦中途失败，会把仍在数据库里的行指向已经消失的文件——
    比"删除慢了一步但数据完好"更危险。
    """
    from app.completion_grant import (
        assert_provider_tasks_clearable,
        prepare_provider_tasks_for_clear,
        reconcile_project_provider_tasks_for_clear,
    )

    project = _deleted_project_or_404(project_id)
    provider_reconciliation = await reconcile_project_provider_tasks_for_clear(
        project_id,
        conn=get_conn(),
        evidence_source="project_purge_terminal_reconcile",
    )
    assert_provider_tasks_clearable(
        project_id=project_id,
        conn=get_conn(),
    )
    # 软删除时已经取消过一轮；这里再取消一次是防御性的（例如用户在软删除后
    # 短暂恢复、又发起新任务、又再次软删除的场景），不是重复劳动的赘余。
    cancelled_tasks = await task_registry.cancel_project(project_id)
    conn = get_conn()
    try:
        prepare_provider_tasks_for_clear(
            project_id=project_id,
            conn=conn,
        )
        evidence_removed = _delete_project_evidence(conn, project_id)
        # 文件和衍生产物由同一权威清理函数处理；数据库级联负责关系完整性。
        worker.delete_project_episodes(
            project_id,
            conn=conn,
            commit=False,
            check_provider=False,
        )
        conn.execute("DELETE FROM chapters WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM jobs WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM character_portraits WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM scene_references WHERE project_id=?", (project_id,))
        # deleted_at IS NOT NULL：只删仍在回收站里的这一行，防止跟一次并发的
        # 恢复请求赛跑——真撞上了，物理清理就整体失败，磁盘文件原封不动
        # （下面的 rmtree 不会执行），下一轮清理再来。
        purge_cur = conn.execute(
            "DELETE FROM projects WHERE id=? AND deleted_at IS NOT NULL", (project_id,)
        )
        if purge_cur.rowcount != 1:
            raise HTTPException(409, "项目已被恢复，取消本次彻底清理")
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    import shutil
    from app.config import PROJECTS_DIR
    shutil.rmtree(PROJECTS_DIR / project_id, ignore_errors=True)
    return {
        "purged": project_id,
        "name": project["name"],
        "cancelled_tasks": cancelled_tasks,
        "evidence_removed": evidence_removed,
        "provider_reconciliation": provider_reconciliation,
    }


async def _purge_all_deleted_projects_core() -> dict:
    """清空回收站：逐个彻底清理全部已软删除的项目。

    每个项目的清理各自独立提交；一个项目失败（例如供应商任务未到终态）不
    得阻塞其余项目——收集失败项返回，而不是让调用方一次报错看不到全貌。

    归属范围与 ``list_deleted_projects`` 一致：普通账号只清空自己名下的回收
    站条目，系统管理员（或无 Principal 的内部调用）才清空全部——这是
    ``DELETE /projects/deleted``（"一键清空回收站"）背后的真正查询，此前
    完全没有 owner 过滤，任何登录账号都会把其他账号回收站里的项目一并彻底
    删除；project_id 不在 ``ProjectPurgeAllInput`` 里，HTTP 边缘的
    ``require_project_owner_access`` 没有路径参数可挂，全靠这里补上。
    """
    conn = get_conn()
    owner = _listing_owner_scope()
    if owner is not None:
        project_ids = [
            row["id"] for row in conn.execute(
                "SELECT id FROM projects WHERE deleted_at IS NOT NULL AND owner_user_id=?",
                (owner,),
            ).fetchall()
        ]
    else:
        # ALL_OWNERS: same admin/internal-caller rationale as
        # list_deleted_projects() -- the marker inside the SQL text is what
        # tests/test_project_ownership_query_guard.py actually looks for.
        project_ids = [
            row["id"] for row in conn.execute(
                "SELECT id FROM projects -- ALL_OWNERS: system admin / internal caller\n"
                "WHERE deleted_at IS NOT NULL"
            ).fetchall()
        ]
    purged: list[str] = []
    failed: list[dict] = []
    for pid in project_ids:
        try:
            await _purge_project_core(pid)
            purged.append(pid)
        except Exception as exc:  # noqa: BLE001 — 单个项目失败不得阻塞其余项目清空
            from app.errors import log_error
            rec = log_error(
                exc,
                action="project_purge_all",
                context={"project_id": pid},
                meta={"stage": "recycle_bin_purge_all"},
            )
            failed.append({"project_id": pid, "error_id": rec.error_id, "error": str(exc)})
    return {"purged": purged, "purged_count": len(purged), "failed": failed}


async def sweep_expired_deleted_projects() -> dict:
    """保留期到期的项目自动彻底清理；由周期性系统任务调用（见 ``app.recovery``）。
    判据是 ``deleted_at`` + 每行 ``recycle_bin_retention_s``（NULL 时默认 24 小时；
    账号级联软删除写 30 天，见 ``ACCOUNT_DELETE_RETENTION_S``）与当前时间的差值，
    不依赖任何内存计时器；供应商任务未到终态的项目会在这一轮失败并保留，下一
    轮重试。
    """
    conn = get_conn()
    stamp = now()
    project_ids = [
        row["id"] for row in conn.execute(
            "SELECT id FROM projects -- ALL_OWNERS: periodic background sweep "
            "loop (app.recovery.project_recycle_bin_sweep_loop), no request "
            "context; retention is enforced globally by deleted_at + "
            "recycle_bin_retention_s, not per caller\n"
            "WHERE deleted_at IS NOT NULL "
            "AND deleted_at + COALESCE(recycle_bin_retention_s, ?) < ?",
            (PROJECT_RECYCLE_BIN_RETENTION_S, stamp),
        ).fetchall()
    ]
    purged: list[str] = []
    failed: list[dict] = []
    for pid in project_ids:
        try:
            await _purge_project_core(pid)
            purged.append(pid)
        except Exception as exc:  # noqa: BLE001 — 单个项目失败不得阻塞其余到期项目
            from app.errors import log_error
            rec = log_error(
                exc,
                action="project_recycle_bin_sweep",
                context={"project_id": pid},
                meta={"stage": "recycle_bin_sweep"},
            )
            failed.append({"project_id": pid, "error_id": rec.error_id, "error": str(exc)})
    return {"purged": purged, "purged_count": len(purged), "failed": failed}


@router.delete("/projects/deleted")
async def purge_all_deleted_projects():
    """一键清空回收站。必须注册在 ``DELETE /projects/{project_id}`` 之前，
    理由同 ``list_deleted_projects``：都是 "projects" 后接一个静态段。"""
    from app.capabilities.dispatch import dispatch, respond_ui

    result = await dispatch("project.purge_all", {}, initiator="ui")
    return respond_ui(result)


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str):
    from app.capabilities.dispatch import dispatch, respond_ui

    result = await dispatch("project.delete", {"project_id": project_id}, initiator="ui")
    return respond_ui(result)


@router.post("/projects/{project_id}/restore")
async def restore_project(project_id: str):
    from app.capabilities.dispatch import dispatch, respond_ui

    result = await dispatch("project.restore", {"project_id": project_id}, initiator="ui")
    return respond_ui(result)


@router.delete("/projects/{project_id}/purge")
async def purge_project(project_id: str):
    from app.capabilities.dispatch import dispatch, respond_ui

    result = await dispatch("project.purge", {"project_id": project_id}, initiator="ui")
    return respond_ui(result)


__all__ = [name for name in globals() if not name.startswith("__")]
