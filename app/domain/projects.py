from __future__ import annotations

try:
    router
except NameError:  # pragma: no cover - used when importing this module directly
    from app.domain.common import *


def _present_refs_error(conn, value: str | None) -> str | None:
    """Repair the display of legacy QA failures stored as generic LLM errors.

    Older runs wrapped every ``ProviderError`` as an external-service failure,
    including the semantic message raised after two successful QA calls.  Keep
    the immutable log handle, but show the safe workflow cause on project reads.
    """
    text = str(value or "").strip()
    if not text or "错误码 LLM" not in text or "ERR-" not in text:
        return value
    start = text.rfind("ERR-")
    error_id = text[start:start + 19]
    try:
        row = conn.execute(
            "SELECT message FROM error_logs WHERE id=? AND action='refs_generate'",
            (error_id,),
        ).fetchone()
    except Exception:  # noqa: BLE001 - compatibility with minimal/legacy schemas
        return value
    message = str(row["message"] if row else "").strip()
    if "一致性检查未通过" not in message and "未通过质量校验" not in message:
        return value
    message = message.replace("部分定妆照失败：", "部分定妆照未通过质量校验：", 1)
    return f"{message}（QA · {error_id}）"


def _create_project_core(name: str | None, filename: str, raw: bytes) -> dict:
    """导入小说的领域逻辑，供 REST 路由与 ``project.import_novel`` Command Handler 共用。"""
    if not raw:
        raise HTTPException(400, "文件为空")
    report = ingest_novel(raw)
    if not report["chapters"]:
        raise HTTPException(400, "未能从文件中切分出任何章节")
    conn = get_conn()
    project_id = new_id("proj")
    conn.execute(
        "INSERT INTO projects(id, name, status, novel_chars, created_at) VALUES(?,?,'ingested',?,?)",
        (project_id, (name or "").strip() or filename, report["total_chars"], now()))
    conn.executemany(
        "INSERT INTO chapters(project_id, idx, title, content, char_count) VALUES(?,?,?,?,?)",
        [(project_id, ch["idx"], ch["title"], ch["content"], len(ch["content"])) for ch in report["chapters"]])
    conn.commit()
    return {
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
        },
    }


@router.post("/attachments/novel")
async def upload_novel_attachment(file: UploadFile = File(...)):
    """用户在系统文件选择器中挑选 TXT 后，前端立即换发短时效 attachment_token（不暴露真实路径）。"""
    from app.capabilities.attachments import store_upload

    raw = await file.read()
    if not raw:
        raise HTTPException(400, "文件为空")
    token = store_upload(file.filename or "novel.txt", raw, content_type=file.content_type)
    return {"attachment_token": token, "filename": file.filename}


@router.post("/projects")
async def create_project(name: str = Form(...), file: UploadFile = File(...)):
    """页面上传入口：内部换发 attachment_token 后统一走 Command Bus，与 Agent/MCP 同一实现。"""
    from app.capabilities.attachments import store_upload
    from app.capabilities.dispatch import dispatch, respond_ui

    raw = await file.read()
    if not raw:
        raise HTTPException(400, "文件为空")
    token = store_upload(file.filename or "novel.txt", raw)
    result = await dispatch(
        "project.import_novel",
        {"attachment_token": token, "name": name},
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
               WHERE p.project_id=? AND p.ep_start<>? ORDER BY v.portrait_id, v.created_at""",
            (project_id, STAGED_INITIAL_EP_START),
        ).fetchall())
    except Exception:  # noqa: BLE001
        view_rows = []
    views_by_portrait: dict[str, list[dict]] = {}
    for v in view_rows:
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
    if view not in (None, "bible", "scenes", "episodes", "picker", "picker_review"):
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
        from app.refs import portrait_prompt
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
            c["portrait_prompt_effective"] = override or portrait_prompt(style, c.get("appearance_canonical", ""))
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
            s["scene_prompt_effective"] = soverride or scene_ref_prompt(style, s.get("scene_canonical", ""))
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
    if view == "picker_review":
        # Review tables are additive and may be absent in databases created by
        # older builds. The helper is loaded later into the shared API facade
        # but is available by the time this route can be called.
        ensure_review_tables = globals().get("_ensure_review_wall_tables")
        if callable(ensure_review_tables):
            ensure_review_tables(conn)
        p["episodes"] = rows_to_dicts(conn.execute(
            """SELECT e.id, e.episode_no, e.title, e.status, e.screenplay_status,
                      (SELECT COUNT(*) FROM shots s WHERE s.episode_id=e.id) AS shot_count,
                      (SELECT COUNT(*) FROM shots s WHERE s.episode_id=e.id AND s.adopted_version_id IS NOT NULL) AS video_count,
                      (SELECT COUNT(*) FROM shots s WHERE s.episode_id=e.id AND s.adopted_version_id IS NULL
                         AND EXISTS(SELECT 1 FROM shot_versions v WHERE v.shot_id=s.id AND v.status='succeeded')) AS pending_adoption_count,
                      (SELECT COUNT(*) FROM shot_versions v JOIN shots s ON s.id=v.shot_id
                         WHERE s.episode_id=e.id AND v.status='failed') AS failed_count,
                      (SELECT COUNT(*) FROM shot_review_items ri JOIN shots s ON s.id=ri.shot_id
                         WHERE s.episode_id=e.id AND ri.status IN ('open','in_progress')) AS open_review_count,
                      (SELECT COUNT(*) FROM shot_review_states rs JOIN shots s ON s.id=rs.shot_id
                         WHERE s.episode_id=e.id AND rs.review_status='completed') AS reviewed_count
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
            clauses.append("(screenplay_status IN ('failed','warning','repairing') OR status LIKE '%failed%')")
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
                      SUM(CASE WHEN screenplay_status='running' THEN 1 ELSE 0 END) AS screenplay_running,
                      SUM(CASE WHEN status='scripting' THEN 1 ELSE 0 END) AS scripting,
                      SUM(CASE WHEN screenplay_status IN ('pending','failed','warning','repairing')
                                OR screenplay_json IS NULL THEN 1 ELSE 0 END) AS screenplay_todo,
                      SUM(CASE WHEN screenplay_status='ready'
                                AND status IN ('planned','script_failed') THEN 1 ELSE 0 END) AS storyboard_ready
               FROM episodes WHERE project_id=?""",
            (project_id,),
        ).fetchone()
        p["episode_counts"] = {key: int(counts[key] or 0) for key in counts.keys()}
        p["episodes_busy"] = bool(
            p["plan_status"] == "running"
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
                ep["screenplay_beats"] = len(script.beats)
                ep["screenplay_mode"] = _screenplay_mode(script)
                ep["screenplay_title"] = script.title or ep["title"]
            except (json.JSONDecodeError, TypeError, ValueError):
                ep["screenplay_beats"] = 0
                ep["screenplay_mode"] = "unknown"
                ep["screenplay_title"] = ep["title"]
        else:
            ep["screenplay_beats"] = 0
            ep["screenplay_mode"] = "none"
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


async def _delete_project_core(project_id: str) -> dict:
    """删除项目的领域逻辑，供 REST 路由与 ``project.delete`` Command Handler 共用。"""
    _project_or_404(project_id)
    # 先停止并等待所有项目级后台协程退出，防止删库后任务继续回写孤儿版本/参考图。
    cancelled_tasks = await task_registry.cancel_project(project_id)
    conn = get_conn()
    # 文件和衍生产物由同一权威清理函数处理；数据库级联负责关系完整性。
    worker.delete_project_episodes(project_id)
    conn.execute("DELETE FROM chapters WHERE project_id=?", (project_id,))
    conn.execute("DELETE FROM jobs WHERE project_id=?", (project_id,))
    conn.execute("DELETE FROM character_portraits WHERE project_id=?", (project_id,))
    conn.execute("DELETE FROM scene_references WHERE project_id=?", (project_id,))
    conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
    conn.commit()
    import shutil
    from app.config import PROJECTS_DIR
    shutil.rmtree(PROJECTS_DIR / project_id, ignore_errors=True)
    return {"deleted": project_id, "cancelled_tasks": cancelled_tasks}


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str):
    from app.capabilities.dispatch import dispatch, respond_ui

    result = await dispatch("project.delete", {"project_id": project_id}, initiator="ui")
    return respond_ui(result)


__all__ = [name for name in globals() if not name.startswith("__")]
