from __future__ import annotations

try:
    router
except NameError:  # pragma: no cover - used when importing this module directly
    from app.domain.common import *

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
    """为 bible.characters 挂上 character_portraits 表里的分段定妆照（按适用集左区间排序）。"""
    rows = rows_to_dicts(conn.execute(
        "SELECT id, character_name, ep_start, ep_end, appearance, base_portrait_id, image_path "
        "FROM character_portraits WHERE project_id=? ORDER BY character_name, ep_start", (project_id,)).fetchall())
    by_name: dict[str, list[dict]] = {}
    for r in rows:
        by_name.setdefault(r["character_name"], []).append({
            "id": r["id"], "ep_start": r["ep_start"], "ep_end": r["ep_end"],
            "appearance": r["appearance"], "base_portrait_id": r["base_portrait_id"],
            "image_url": _media_url(r["image_path"]),
        })
    for c in bible.get("characters", []):
        c["portraits"] = by_name.get(c.get("name"), [])


def _attach_scene_refs(conn, project_id: str, bible: dict) -> None:
    """为 bible.scenes 挂上 scene_references 表里的分段场景图（含 QA 分数），按适用集左区间排序。"""
    rows = rows_to_dicts(conn.execute(
        "SELECT scene_name, ep_start, ep_end, scene_canonical, image_path, qa_json, artifact_id "
        "FROM scene_references WHERE project_id=? ORDER BY scene_name, ep_start", (project_id,)).fetchall())
    by_name: dict[str, list[dict]] = {}
    for r in rows:
        qa = None
        if r["qa_json"]:
            try:
                qa = json.loads(r["qa_json"])
            except (TypeError, ValueError):
                qa = None
        evidence = evidence_repository.get_artifact(r["artifact_id"]) if r.get("artifact_id") else None
        if evidence:
            evidence["evaluations"] = evidence_repository.get_evaluations(evidence["id"])
        by_name.setdefault(r["scene_name"], []).append({
            "ep_start": r["ep_start"], "ep_end": r["ep_end"],
            "scene_canonical": r["scene_canonical"], "image_url": _media_url(r["image_path"]),
            "qa": qa, "qa_overall": (qa or {}).get("overall") if isinstance(qa, dict) else None,
            "artifact_id": r.get("artifact_id"), "evidence": evidence,
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
        # scene_references 是场景图的权威存储；bible 的 ref_image_path 只是回退，二者会因
        # 重新提取场景清单/反应式补图而分叉。bible 没路径时用最新分段的落盘图回填出图状态与主图。
        if not s.get("ref_image_url"):
            latest = next((seg for seg in reversed(segs) if seg.get("image_url")), None)
            if latest:
                s["ref_image_url"] = latest["image_url"]


@router.get("/projects/{project_id}")
def project_detail(project_id: str):
    p = dict(_project_or_404(project_id))
    conn = get_conn()
    p["bible"] = json.loads(p["bible_json"]) if p["bible_json"] else None
    bible_artifact = (
        evidence_repository.get_artifact(p.get("bible_artifact_id"))
        if p.get("bible_artifact_id") else None
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
    p["key_timeline"] = json.loads(p["key_timeline"]) if p["key_timeline"] else []
    p["chapters"] = rows_to_dicts(conn.execute(
        "SELECT idx, title, char_count, summary IS NOT NULL AS has_summary, substr(content,1,200) AS preview "
        "FROM chapters WHERE project_id=? ORDER BY idx",
        (project_id,)).fetchall())
    for ch in p["chapters"]:
        ch["preview"] = chapter_preview(ch.pop("preview", ""))
    # 把每个角色的定妆照分段（适用集区间 + 图生图谱系）挂到 bible.characters 上，供横向预览。
    if p["bible"]:
        _attach_character_portraits(conn, project_id, p["bible"])
        _attach_scene_refs(conn, project_id, p["bible"])
    p["episodes"] = rows_to_dicts(conn.execute(
        "SELECT * FROM episodes WHERE project_id=? ORDER BY episode_no", (project_id,)).fetchall())
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
        ep["cost_cny"] = worker.episode_cost(ep["id"])
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


# ---------- 一键全自动成片 ----------

async def _start_auto_core(
    project_id: str,
    export_dir: str | None,
    mode: str = "to_storyboard",
) -> dict:
    """启动全流程自动化。mode=to_storyboard 停在分镜待确认；mode=full 自动确认并出片。"""
    _project_or_404(project_id)
    _require_harness_engine(project_id)
    if mode not in ("to_storyboard", "full"):
        raise HTTPException(400, f"无效的自动成片模式：{mode}")
    from app import auto
    if auto.is_running(project_id):
        raise HTTPException(409, "该项目的自动成片已在进行中")
    run_id = auto.start(project_id, export_dir=export_dir, mode=mode)
    return {"status": "running", "run_id": run_id, "mode": mode}


@router.post("/projects/{project_id}/auto")
async def start_auto(project_id: str, body: dict | None = Body(None)):
    from app.capabilities.dispatch import dispatch, respond_ui

    body = body or {}
    export_dir = body.get("export_dir")
    mode = body.get("mode") or "to_storyboard"
    result = await dispatch(
        "production.auto_start",
        {"project_id": project_id, "directory_grant": export_dir, "mode": mode},
        initiator="ui",
    )
    return respond_ui(result)


@router.get("/projects/{project_id}/auto/status")
def auto_status(project_id: str):
    _project_or_404(project_id)
    from app import auto
    return auto.status(project_id)


@router.post("/projects/{project_id}/auto/cancel")
async def cancel_auto(project_id: str):
    from app.capabilities.dispatch import dispatch, respond_ui

    result = await dispatch("production.auto_cancel", {"project_id": project_id}, initiator="ui")
    return respond_ui(result)

__all__ = [name for name in globals() if not name.startswith("__")]
