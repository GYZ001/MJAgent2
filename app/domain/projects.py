from __future__ import annotations

from collections.abc import Iterable

try:
    router
except NameError:  # pragma: no cover - used when importing this module directly
    from app.domain.common import *


_SQLITE_IN_CHUNK_SIZE = 400


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
        conn.execute(
            "INSERT INTO projects(id, name, status, novel_chars, created_at) VALUES(?,?,'ingested',?,?)",
            (project_id, project_name, report["total_chars"], now()))
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


@router.get("/projects")
def list_projects():
    conn = get_conn()
    rows = rows_to_dicts(conn.execute(
        "SELECT id, name, status, novel_chars, bible_status, plan_status, created_at FROM projects ORDER BY created_at DESC").fetchall())
    _recover_orphan_bible_dicts(conn, rows)
    for p in rows:
        p["chapter_count"] = conn.execute("SELECT COUNT(*) c FROM chapters WHERE project_id=?", (p["id"],)).fetchone()["c"]
        p["episode_count"] = conn.execute("SELECT COUNT(*) c FROM episodes WHERE project_id=?", (p["id"],)).fetchone()["c"]
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


@router.get("/projects/{project_id}")
def project_detail(
    project_id: str,
    view: str | None = None,
    page: int = 1,
    page_size: int = 15,
    query: str = "",
    status_filter: str = "all",
):
    if view not in (None, "bible", "scenes", "episodes", "picker", "picker_generation"):
        raise HTTPException(400, f"未知项目视图：{view}")
    full = view is None
    p = dict(_project_or_404(project_id))
    conn = get_conn()
    p["refs_error"] = _present_refs_error(conn, p.get("refs_error"))
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
        from app.config import PROJECTS_DIR
        from app.refs import effective_portrait_prompt
        style = p["bible"].get("world", {}).get("visual_style_canonical", "")
        import os
        for c in p["bible"].get("characters", []):
            path_str = c.get("ref_image_path")
            if path_str and os.path.exists(path_str):
                # 使用 Path.relative_to(PROJECTS_DIR).as_posix() 确保 Windows 下路径分隔符正确转换为 /
                rel_path = Path(path_str).relative_to(PROJECTS_DIR).as_posix()
                c["ref_image_url"] = f"/media/{rel_path}?v={int(os.path.getmtime(path_str))}"
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
                rel_path = Path(spath).relative_to(PROJECTS_DIR).as_posix()
                s["ref_image_url"] = f"/media/{rel_path}?v={int(os.path.getmtime(spath))}"
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

    if view == "picker":
        p["episodes"] = rows_to_dicts(conn.execute(
            "SELECT id, episode_no, title, status, screenplay_status "
            "FROM episodes WHERE project_id=? ORDER BY episode_no",
            (project_id,),
        ).fetchall())
        return p
    if view == "picker_generation":
        p["episodes"] = rows_to_dicts(conn.execute(
            """SELECT e.id, e.episode_no, e.title, e.status, e.screenplay_status,
                      (SELECT COUNT(*) FROM shots s WHERE s.episode_id=e.id) AS shot_count,
                      (SELECT COUNT(*) FROM shots s WHERE s.episode_id=e.id AND s.adopted_version_id IS NOT NULL) AS video_count,
                      (SELECT COUNT(*) FROM shots s WHERE s.episode_id=e.id AND s.adopted_version_id IS NULL
                         AND EXISTS(SELECT 1 FROM shot_versions v WHERE v.shot_id=s.id AND v.status='succeeded')) AS pending_adoption_count,
                      (SELECT COUNT(*) FROM shot_versions v JOIN shots s ON s.id=v.shot_id
                         WHERE s.episode_id=e.id AND v.status='failed') AS failed_count
                 FROM episodes e WHERE e.project_id=? ORDER BY e.episode_no""",
            (project_id,),
        ).fetchall())
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
    ep = dict(_episode_or_404(episode_id))
    project = get_conn().execute(
        "SELECT plan_status FROM projects WHERE id=?", (ep["project_id"],)
    ).fetchone()
    if task_registry.active("plan", ep["project_id"]) or (
        project and project["plan_status"] == "running"
    ):
        raise HTTPException(409, "分集规划正在运行，请等待完成后再删除单集")
    _assert_no_other_episode_work(ep["project_id"], episode_id)

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
    evidence_removed = _delete_episode_evidence(conn, episode_id)
    worker.delete_episode_shots(episode_id)
    conn.execute("DELETE FROM episodes WHERE id=?", (episode_id,))
    conn.commit()

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


async def _delete_project_core(project_id: str) -> dict:
    """删除项目的领域逻辑，供 REST 路由与 ``project.delete`` Command Handler 共用。"""
    from app.completion_grant import (
        assert_provider_tasks_clearable,
        prepare_provider_tasks_for_clear,
        reconcile_project_provider_tasks_for_clear,
    )

    _project_or_404(project_id)
    provider_reconciliation = await reconcile_project_provider_tasks_for_clear(
        project_id,
        conn=get_conn(),
    )
    # Fast preflight before cancelling any producer. The authoritative check is
    # repeated inside the deletion transaction after all local writers stop.
    assert_provider_tasks_clearable(
        project_id=project_id,
        conn=get_conn(),
    )
    # 先停止并等待所有项目级后台协程退出，防止删库后任务继续回写孤儿版本/参考图。
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
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    import shutil
    from app.config import PROJECTS_DIR
    shutil.rmtree(PROJECTS_DIR / project_id, ignore_errors=True)
    return {
        "deleted": project_id,
        "cancelled_tasks": cancelled_tasks,
        "evidence_removed": evidence_removed,
        "provider_reconciliation": provider_reconciliation,
    }


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str):
    from app.capabilities.dispatch import dispatch, respond_ui

    result = await dispatch("project.delete", {"project_id": project_id}, initiator="ui")
    return respond_ui(result)


__all__ = [name for name in globals() if not name.startswith("__")]
