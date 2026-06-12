"""REST API。文本阶段（圣经/规划/分镜）为后台任务 + 状态轮询；视频阶段走 worker 队列。"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app import config, worker
from app.compiler import compile_prompt, shot_cost_cny
from app.db import get_conn, get_setting, new_id, now, rows_to_dicts, set_setting
from app.ingest import ingest_novel
from app.schemas import Bible, Shot, Storyboard, schema_errors
from app.stages import StageError, generate_bible, generate_plan_batch, generate_storyboard, summarize_chapters_concurrent
from app.validators import validate_storyboard

router = APIRouter(prefix="/api")


def _project_or_404(project_id: str):
    row = get_conn().execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"项目不存在：{project_id}")
    return row


def _episode_or_404(episode_id: str):
    row = get_conn().execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"剧集不存在：{episode_id}")
    return row


def _compact_episode_target(target_duration_s: int | None) -> int:
    if target_duration_s is None:
        return config.EPISODE_TARGET_DEFAULT_S
    target = int(target_duration_s)
    if target > config.EPISODE_TARGET_MAX_S:
        target = config.EPISODE_TARGET_MAX_S
    elif target < config.EPISODE_TARGET_MIN_S:
        target = config.EPISODE_TARGET_MIN_S
    step = config.EPISODE_TARGET_STEP_S
    rounded = ((target + step // 2) // step) * step
    return min(config.EPISODE_TARGET_MAX_S, max(config.EPISODE_TARGET_MIN_S, rounded))


def _storyboard_target_for_source(target_duration_s: int | None, source_chars: int) -> int:
    target = _compact_episode_target(target_duration_s)
    if source_chars >= 5000:
        return max(target, config.EPISODE_TARGET_MAX_S)
    if source_chars >= 3500:
        return max(target, config.EPISODE_TARGET_MAX_S)
    if source_chars >= 2200:
        return max(target, 50)
    return target


# ---------- 项目与摄入 ----------

@router.post("/projects")
async def create_project(name: str = Form(...), file: UploadFile = File(...)):
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "文件为空")
    report = ingest_novel(raw)
    if not report["chapters"]:
        raise HTTPException(400, "未能从文件中切分出任何章节")
    conn = get_conn()
    project_id = new_id("proj")
    conn.execute(
        "INSERT INTO projects(id, name, status, novel_chars, created_at) VALUES(?,?,'ingested',?,?)",
        (project_id, name.strip() or file.filename, report["total_chars"], now()))
    conn.executemany(
        "INSERT INTO chapters(project_id, idx, title, content, char_count) VALUES(?,?,?,?,?)",
        [(project_id, ch["idx"], ch["title"], ch["content"], len(ch["content"])) for ch in report["chapters"]])
    conn.commit()
    return {"project_id": project_id, "ingestion": {k: report[k] for k in ("total_chars", "removed_lines", "chapter_count", "auto_split")}}


@router.get("/projects")
def list_projects():
    conn = get_conn()
    rows = rows_to_dicts(conn.execute(
        "SELECT id, name, status, novel_chars, bible_status, plan_status, created_at FROM projects ORDER BY created_at DESC").fetchall())
    for p in rows:
        p["chapter_count"] = conn.execute("SELECT COUNT(*) c FROM chapters WHERE project_id=?", (p["id"],)).fetchone()["c"]
        p["episode_count"] = conn.execute("SELECT COUNT(*) c FROM episodes WHERE project_id=?", (p["id"],)).fetchone()["c"]
    return rows


@router.get("/projects/{project_id}")
def project_detail(project_id: str):
    p = dict(_project_or_404(project_id))
    conn = get_conn()
    p["bible"] = json.loads(p["bible_json"]) if p["bible_json"] else None
    p.pop("bible_json", None)
    if p["bible"]:
        from app.config import PROJECTS_DIR
        for c in p["bible"].get("characters", []):
            path = c.get("ref_image_path")
            c["ref_image_url"] = ("/media/" + path.removeprefix(str(PROJECTS_DIR) + "/")) if path else None
    p["key_timeline"] = json.loads(p["key_timeline"]) if p["key_timeline"] else []
    p["chapters"] = rows_to_dicts(conn.execute(
        "SELECT idx, title, char_count, summary IS NOT NULL AS has_summary FROM chapters WHERE project_id=? ORDER BY idx",
        (project_id,)).fetchall())
    p["episodes"] = rows_to_dicts(conn.execute(
        "SELECT * FROM episodes WHERE project_id=? ORDER BY episode_no", (project_id,)).fetchall())
    for ep in p["episodes"]:
        ep["source_chapters"] = json.loads(ep["source_chapters"] or "[]")
        ep["cost_cny"] = worker.episode_cost(ep["id"])
    return p


@router.delete("/projects/{project_id}")
def delete_project(project_id: str):
    _project_or_404(project_id)
    conn = get_conn()
    ep_ids = [r["id"] for r in conn.execute("SELECT id FROM episodes WHERE project_id=?", (project_id,)).fetchall()]
    for eid in ep_ids:
        shot_ids = [r["id"] for r in conn.execute("SELECT id FROM shots WHERE episode_id=?", (eid,)).fetchall()]
        for sid in shot_ids:
            conn.execute("DELETE FROM shot_versions WHERE shot_id=?", (sid,))
        conn.execute("DELETE FROM shots WHERE episode_id=?", (eid,))
    conn.execute("DELETE FROM episodes WHERE project_id=?", (project_id,))
    conn.execute("DELETE FROM chapters WHERE project_id=?", (project_id,))
    conn.execute("DELETE FROM jobs WHERE project_id=?", (project_id,))
    conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
    conn.commit()
    import shutil
    from app.config import PROJECTS_DIR
    shutil.rmtree(PROJECTS_DIR / project_id, ignore_errors=True)
    return {"deleted": project_id}


# ---------- 角色圣经 ----------

async def _bible_task(project_id: str):
    conn = get_conn()
    try:
        chapters = rows_to_dicts(conn.execute(
            "SELECT * FROM chapters WHERE project_id=? ORDER BY idx", (project_id,)).fetchall())
        bible = await generate_bible(chapters)
        # 重新谱写时按角色名保留已有定妆照（重生圣经不应丢失一致性锚点）
        old_row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
        if old_row and old_row["bible_json"]:
            old_refs = {c.get("name"): c.get("ref_image_path")
                        for c in json.loads(old_row["bible_json"]).get("characters", [])}
            for c in bible.characters:
                c.ref_image_path = old_refs.get(c.name) or None
        conn.execute(
            "UPDATE projects SET bible_json=?, bible_version=bible_version+1, bible_status='ready', bible_error=NULL, status='bible_ready' WHERE id=?",
            (bible.model_dump_json(), project_id))
        conn.commit()
    except (StageError, Exception) as exc:  # noqa: BLE001
        conn.execute("UPDATE projects SET bible_status='failed', bible_error=? WHERE id=?", (str(exc)[:800], project_id))
        conn.commit()


@router.post("/projects/{project_id}/bible")
async def start_bible(project_id: str):
    p = _project_or_404(project_id)
    if p["bible_status"] == "running":
        raise HTTPException(409, "角色圣经正在生成中")
    conn = get_conn()
    conn.execute("UPDATE projects SET bible_status='running', bible_error=NULL WHERE id=?", (project_id,))
    conn.commit()
    asyncio.create_task(_bible_task(project_id))
    return {"status": "running"}


@router.put("/projects/{project_id}/bible")
def edit_bible(project_id: str, body: dict):
    _project_or_404(project_id)
    instance, errors = schema_errors(Bible, body)
    if errors:
        raise HTTPException(422, "；".join(errors))
    from app.validators import validate_bible
    errors = validate_bible(instance)
    if errors:
        raise HTTPException(422, "；".join(errors))
    conn = get_conn()
    conn.execute("UPDATE projects SET bible_json=?, bible_version=bible_version+1 WHERE id=?",
                 (instance.model_dump_json(), project_id))
    conn.commit()
    return {"bible_version_bumped": True}


# ---------- 角色定妆照（人物跨集一致性） ----------

async def _refs_task(project_id: str, only_character: str | None):
    from app.refs import generate_refs
    conn = get_conn()
    try:
        await generate_refs(project_id, only_character)
        conn.execute("UPDATE projects SET refs_status='ready', refs_error=NULL WHERE id=?", (project_id,))
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        conn.execute("UPDATE projects SET refs_status='failed', refs_error=? WHERE id=?",
                     (str(exc)[:800], project_id))
        conn.commit()


@router.post("/projects/{project_id}/refs")
async def start_refs(project_id: str, body: dict | None = None):
    p = _project_or_404(project_id)
    if not p["bible_json"]:
        raise HTTPException(409, "请先生成角色圣经")
    if p["refs_status"] == "running":
        raise HTTPException(409, "定妆照正在生成中")
    only = (body or {}).get("character")
    conn = get_conn()
    conn.execute("UPDATE projects SET refs_status='running', refs_error=NULL WHERE id=?", (project_id,))
    conn.commit()
    asyncio.create_task(_refs_task(project_id, only))
    return {"status": "running"}


# ---------- 剧集规划 ----------

async def _plan_task(project_id: str):
    conn = get_conn()
    try:
        chapters = rows_to_dicts(conn.execute(
            "SELECT * FROM chapters WHERE project_id=? ORDER BY idx", (project_id,)).fetchall())
        new_summaries = await summarize_chapters_concurrent(chapters)
        for idx, summary in new_summaries.items():
            conn.execute("UPDATE chapters SET summary=? WHERE project_id=? AND idx=?", (summary, project_id, idx))
        conn.commit()
        chapters = rows_to_dicts(conn.execute(
            "SELECT * FROM chapters WHERE project_id=? ORDER BY idx", (project_id,)).fetchall())
        p = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        bible = Bible.model_validate(json.loads(p["bible_json"]))
        chapter_count = len(chapters)
        batch_size = max(int(get_setting("plan_episode_count") or 12), 1)
        summaries = [(ch["idx"], ch["title"] or "", ch["summary"] or "") for ch in chapters]

        # 分批续写，铺满全书：每批从首个未覆盖章节起，直到最后一章被纳入（防长篇被截断/丢弃）
        all_episodes = []
        key_timeline: list[str] = []
        start_chapter = chapters[0]["idx"] if chapters else 1
        last_chapter = chapters[-1]["idx"] if chapters else 0
        guard = 0
        while start_chapter <= last_chapter and guard < 40:
            guard += 1
            batch = await generate_plan_batch(
                summaries, bible,
                start_episode_no=len(all_episodes) + 1, start_chapter=start_chapter,
                chapter_count=chapter_count, batch_size=batch_size,
                want_timeline=(guard == 1))
            if guard == 1:
                key_timeline = batch.key_timeline
            advanced = batch.episodes[-1].source_chapters[-1]
            if advanced < start_chapter:  # 无进展，避免死循环
                raise StageError("剧集规划", [f"分批续写未推进（停在第 {start_chapter} 章），请重试"])
            all_episodes.extend(batch.episodes)
            start_chapter = advanced + 1

        conn.execute("DELETE FROM episodes WHERE project_id=? AND status='planned'", (project_id,))
        for ep in all_episodes:
            conn.execute(
                "INSERT INTO episodes(id, project_id, episode_no, title, hook, cliffhanger, synopsis, source_chapters, target_duration_s, status, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?, 'planned', ?)",
                (new_id("ep"), project_id, ep.episode_no, ep.title, ep.hook, ep.cliffhanger,
                 ep.synopsis, json.dumps(ep.source_chapters), ep.target_duration_s, now()))
        conn.execute("UPDATE projects SET plan_status='ready', plan_error=NULL, key_timeline=?, status='planned' WHERE id=?",
                     (json.dumps(key_timeline, ensure_ascii=False), project_id))
        conn.commit()
    except (StageError, Exception) as exc:  # noqa: BLE001
        conn.execute("UPDATE projects SET plan_status='failed', plan_error=? WHERE id=?", (str(exc)[:800], project_id))
        conn.commit()


@router.post("/projects/{project_id}/plan")
async def start_plan(project_id: str):
    p = _project_or_404(project_id)
    if not p["bible_json"]:
        raise HTTPException(409, "请先生成并确认角色圣经")
    if p["plan_status"] == "running":
        raise HTTPException(409, "剧集规划正在生成中")
    conn = get_conn()
    conn.execute("UPDATE projects SET plan_status='running', plan_error=NULL WHERE id=?", (project_id,))
    conn.commit()
    asyncio.create_task(_plan_task(project_id))
    return {"status": "running"}


# ---------- 分镜脚本 ----------

async def _storyboard_task(episode_id: str):
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    try:
        ep_data = dict(ep)
        p = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
        bible = Bible.model_validate(json.loads(p["bible_json"]))
        source_chapters = json.loads(ep["source_chapters"] or "[]")
        placeholders = ",".join("?" for _ in source_chapters)
        chapters = rows_to_dicts(conn.execute(
            f"SELECT * FROM chapters WHERE project_id=? AND idx IN ({placeholders}) ORDER BY idx",
            (ep["project_id"], *source_chapters)).fetchall())
        source_text = "\n\n".join(f"【{ch['title']}】\n{ch['content']}" for ch in chapters)
        compact_target = _storyboard_target_for_source(ep_data.get("target_duration_s"), len(source_text))
        if compact_target != ep_data.get("target_duration_s"):
            conn.execute("UPDATE episodes SET target_duration_s=? WHERE id=?", (compact_target, episode_id))
            conn.commit()
            ep_data["target_duration_s"] = compact_target
        prev = conn.execute(
            "SELECT cliffhanger FROM episodes WHERE project_id=? AND episode_no=?",
            (ep["project_id"], ep["episode_no"] - 1)).fetchone()
        board = await generate_storyboard(ep_data, source_text, bible,
                                          prev_ending=prev["cliffhanger"] if prev else "")
        conn.execute("DELETE FROM shots WHERE episode_id=?", (episode_id,))
        for s in board.shots:
            conn.execute(
                "INSERT INTO shots(id, episode_id, shot_no, duration_s, shot_size, camera_move, scene_setting, characters, action_desc, narration, dialogues, transition, continuity_from_prev) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_id("shot"), episode_id, s.shot_no, s.duration_s, s.shot_size, s.camera_move,
                 s.scene_setting, json.dumps(s.characters, ensure_ascii=False), s.action_desc, s.narration,
                 json.dumps([d.model_dump() for d in s.dialogues], ensure_ascii=False),
                 s.transition, int(s.continuity_from_prev)))
        conn.execute("UPDATE episodes SET status='scripted', script_error=NULL WHERE id=?", (episode_id,))
        conn.commit()
    except (StageError, Exception) as exc:  # noqa: BLE001
        conn.execute("UPDATE episodes SET status='script_failed', script_error=? WHERE id=?",
                     (str(exc)[:800], episode_id))
        conn.commit()


@router.post("/episodes/{episode_id}/storyboard")
async def start_storyboard(episode_id: str):
    ep = _episode_or_404(episode_id)
    if ep["status"] == "scripting":
        raise HTTPException(409, "分镜正在生成中")
    conn = get_conn()
    conn.execute("UPDATE episodes SET status='scripting', script_error=NULL WHERE id=?", (episode_id,))
    conn.commit()
    asyncio.create_task(_storyboard_task(episode_id))
    return {"status": "scripting"}


@router.get("/episodes/{episode_id}")
def episode_detail(episode_id: str):
    ep = dict(_episode_or_404(episode_id))
    conn = get_conn()
    ep["source_chapters"] = json.loads(ep["source_chapters"] or "[]")
    ep["cost_cny"] = worker.episode_cost(episode_id)
    ep["cost_limit_cny"] = float(get_setting("episode_cost_limit_cny") or 100)
    shots = rows_to_dicts(conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)).fetchall())
    for s in shots:
        s["characters"] = json.loads(s["characters"] or "[]")
        s["dialogues"] = json.loads(s["dialogues"] or "[]")
        s["est_cost_cny"] = shot_cost_cny(s["duration_s"])
        versions = rows_to_dicts(conn.execute(
            "SELECT * FROM shot_versions WHERE shot_id=? ORDER BY version_no DESC", (s["id"],)).fetchall())
        for v in versions:
            v["qa"] = json.loads(v["qa_json"]) if v["qa_json"] else None
            v.pop("qa_json", None)
            if v["video_path"]:
                from app.config import PROJECTS_DIR
                v["video_url"] = "/media/" + v["video_path"].removeprefix(str(PROJECTS_DIR) + "/")
        s["versions"] = versions
    ep["shots"] = shots
    return ep


@router.put("/shots/{shot_id}")
def edit_shot(shot_id: str, body: dict):
    conn = get_conn()
    shot = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot:
        raise HTTPException(404, "镜头不存在")
    merged = dict(shot)
    merged["characters"] = json.loads(merged["characters"] or "[]")
    merged["dialogues"] = json.loads(merged["dialogues"] or "[]")
    merged["continuity_from_prev"] = bool(merged["continuity_from_prev"])
    for key in ("duration_s", "shot_size", "camera_move", "scene_setting", "characters",
                "action_desc", "narration", "dialogues", "transition", "continuity_from_prev"):
        if key in body:
            merged[key] = body[key]
    merged["duration_s"] = config.FIXED_VIDEO_DURATION_S
    instance, errors = schema_errors(Shot, {k: merged[k] for k in (
        "shot_no", "duration_s", "shot_size", "camera_move", "scene_setting", "characters",
        "action_desc", "narration", "dialogues", "transition", "continuity_from_prev")})
    if errors:
        raise HTTPException(422, "；".join(errors))
    conn.execute(
        "UPDATE shots SET duration_s=?, shot_size=?, camera_move=?, scene_setting=?, characters=?, action_desc=?, narration=?, dialogues=?, transition=?, continuity_from_prev=? WHERE id=?",
        (instance.duration_s, instance.shot_size, instance.camera_move, instance.scene_setting,
         json.dumps(instance.characters, ensure_ascii=False), instance.action_desc, instance.narration,
         json.dumps([d.model_dump() for d in instance.dialogues], ensure_ascii=False),
         instance.transition, int(instance.continuity_from_prev), shot_id))
    # 编辑后剧集回到 scripted（需重新确认才能生成）
    conn.execute("UPDATE episodes SET status='scripted' WHERE id=? AND status='confirmed'", (shot["episode_id"],))
    conn.commit()
    return {"ok": True}


@router.post("/episodes/{episode_id}/confirm")
def confirm_episode(episode_id: str):
    """人工确认门（PRD P3）：全量业务校验通过才进入 confirmed。"""
    ep = _episode_or_404(episode_id)
    conn = get_conn()
    compact_target = _compact_episode_target(ep["target_duration_s"])
    if compact_target != ep["target_duration_s"]:
        conn.execute("UPDATE episodes SET target_duration_s=? WHERE id=?", (compact_target, episode_id))
        conn.commit()
    p = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    bible = Bible.model_validate(json.loads(p["bible_json"]))
    shots_rows = conn.execute("SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)).fetchall()
    if not shots_rows:
        raise HTTPException(409, "本集还没有分镜脚本")
    shots = [Shot(
        shot_no=r["shot_no"], duration_s=r["duration_s"], shot_size=r["shot_size"], camera_move=r["camera_move"],
        scene_setting=r["scene_setting"], characters=json.loads(r["characters"] or "[]"),
        action_desc=r["action_desc"], narration=r["narration"], dialogues=json.loads(r["dialogues"] or "[]"),
        transition=r["transition"] or "硬切", continuity_from_prev=bool(r["continuity_from_prev"])) for r in shots_rows]
    board = Storyboard(episode_no=ep["episode_no"], shots=shots)
    errors = validate_storyboard(board, bible, compact_target)
    if errors:
        raise HTTPException(422, json.dumps(errors, ensure_ascii=False))
    # 预编译全部 prompt，把参数错误拦在花钱之前
    try:
        for s in shots:
            compile_prompt(s, bible)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(422, f"Prompt 编译失败：{exc}")
    est = sum(shot_cost_cny(s.duration_s) for s in shots)
    conn.execute("UPDATE episodes SET status='confirmed' WHERE id=?", (episode_id,))
    conn.commit()
    return {"confirmed": True, "estimated_cost_cny": round(est, 2), "shot_count": len(shots)}


# ---------- 生成 ----------

@router.post("/episodes/{episode_id}/generate")
def generate_episode(episode_id: str):
    """整集入队。continuity_from_prev=true 的镜头与上一镜头构成依赖链：
    等上一镜成片后取其尾帧作首帧（连贯性核心，实测网关 first_frame 与 reference_image 互斥）。"""
    ep = _episode_or_404(episode_id)
    if ep["status"] not in ("confirmed", "generating", "done"):
        raise HTTPException(409, "分镜脚本未确认（先在工作台点击确认分镜）")
    conn = get_conn()
    shots = conn.execute(
        "SELECT id, continuity_from_prev FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,)).fetchall()
    results = []
    prev_shot_id: str | None = None
    for s in shots:
        after = prev_shot_id if s["continuity_from_prev"] else None
        try:
            results.append({"shot_id": s["id"], **worker.enqueue_shot(s["id"], after_shot_id=after)})
        except Exception as exc:  # noqa: BLE001
            results.append({"shot_id": s["id"], "error": str(exc)})
        prev_shot_id = s["id"]
    return {"enqueued": results}


def _predecessor_shot_id(shot_row) -> str | None:
    """单镜头重生成时恢复其链依赖（取上一编号镜头）。"""
    if not shot_row["continuity_from_prev"]:
        return None
    prev = get_conn().execute(
        "SELECT id FROM shots WHERE episode_id=? AND shot_no=?",
        (shot_row["episode_id"], shot_row["shot_no"] - 1)).fetchone()
    return prev["id"] if prev else None


@router.post("/shots/{shot_id}/generate")
def generate_shot(shot_id: str, body: dict | None = None):
    body = body or {}
    shot_row = get_conn().execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot_row:
        raise HTTPException(404, "镜头不存在")
    try:
        return worker.enqueue_shot(
            shot_id,
            prompt_override=body.get("prompt_override"),
            extra_negative=body.get("extra_negative"),
            reroll=bool(body.get("reroll")),
            after_shot_id=_predecessor_shot_id(shot_row))
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@router.post("/shots/{shot_id}/adopt")
def adopt_version(shot_id: str, body: dict):
    version_id = body.get("version_id")
    conn = get_conn()
    v = conn.execute("SELECT * FROM shot_versions WHERE id=? AND shot_id=?", (version_id, shot_id)).fetchone()
    if not v or v["status"] != "succeeded":
        raise HTTPException(409, "该版本不存在或未成功")
    conn.execute("UPDATE shots SET adopted_version_id=? WHERE id=?", (version_id, shot_id))
    conn.commit()
    return {"adopted": version_id}


@router.post("/episodes/{episode_id}/resume")
def resume_episode(episode_id: str):
    _episode_or_404(episode_id)
    return {"resumed_jobs": worker.retry_paused(episode_id)}


# ---------- 系统 ----------

@router.get("/system/health")
def health():
    from app import config
    return {"ok": True, "gateway": config.HIAGENT_BASE_URL, "key_configured": bool(config.HIAGENT_API_KEY)}


@router.get("/system/calls")
def recent_calls(limit: int = 30):
    rows = rows_to_dicts(get_conn().execute(
        "SELECT * FROM provider_calls ORDER BY id DESC LIMIT ?", (min(limit, 200),)).fetchall())
    return rows


@router.get("/system/jobs")
def jobs_overview():
    conn = get_conn()
    counts = {r["status"]: r["c"] for r in conn.execute(
        "SELECT status, COUNT(*) c FROM jobs GROUP BY status").fetchall()}
    recent = rows_to_dicts(conn.execute(
        """SELECT j.*, s.shot_no, e.episode_no, e.title AS episode_title, p.name AS project_name
           FROM jobs j LEFT JOIN shots s ON s.id=j.shot_id
           LEFT JOIN episodes e ON e.id=j.episode_id LEFT JOIN projects p ON p.id=j.project_id
           ORDER BY j.updated_at DESC LIMIT 40""").fetchall())
    return {"counts": counts, "recent": recent}


@router.get("/settings")
def get_settings():
    rows = get_conn().execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


@router.put("/settings")
def put_settings(body: dict):
    for key, value in body.items():
        set_setting(str(key), str(value))
    return {"ok": True}
