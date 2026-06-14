"""REST API。文本阶段（圣经/规划/分镜）为后台任务 + 状态轮询；视频阶段走 worker 队列。"""
from __future__ import annotations

import asyncio
import json
import os
import re
import string
from pathlib import Path

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile

from app import config, worker
from app.compiler import compile_prompt, shot_cost_cny
from app.db import get_conn, get_setting, new_id, now, rows_to_dicts, set_setting
from app.ingest import ingest_novel
from app.schemas import Bible, Shot, Storyboard, schema_errors
from app.stages import StageError, generate_bible, generate_plan_batch, generate_storyboard, summarize_chapters_concurrent
from app.validators import normalize_action_desc, validate_storyboard

router = APIRouter(prefix="/api")

BIBLE_TASK_TIMEOUT_S = 15 * 60
BIBLE_INTERRUPTED_ERROR = "人物谱任务已中断（服务重载或后台任务丢失），请重新谱写。"

_bible_tasks: dict[str, asyncio.Task] = {}


def _bible_task_active(project_id: str) -> bool:
    task = _bible_tasks.get(project_id)
    return bool(task and not task.done())


def _recover_orphan_bible_row(conn, row):
    if row and row["bible_status"] == "running" and not _bible_task_active(row["id"]):
        conn.execute(
            "UPDATE projects SET bible_status='failed', bible_error=? WHERE id=?",
            (BIBLE_INTERRUPTED_ERROR, row["id"]),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM projects WHERE id=?", (row["id"],)).fetchone()
    return row


def _recover_orphan_bible_dicts(conn, rows: list[dict]) -> None:
    changed = False
    for row in rows:
        if row.get("bible_status") == "running" and not _bible_task_active(row["id"]):
            row["bible_status"] = "failed"
            conn.execute(
                "UPDATE projects SET bible_status='failed', bible_error=? WHERE id=?",
                (BIBLE_INTERRUPTED_ERROR, row["id"]),
            )
            changed = True
    if changed:
        conn.commit()


def _track_bible_task(project_id: str, task: asyncio.Task) -> None:
    _bible_tasks[project_id] = task
    task.add_done_callback(lambda _t, pid=project_id: _bible_tasks.pop(pid, None))


def _project_or_404(project_id: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"项目不存在：{project_id}")
    return _recover_orphan_bible_row(conn, row)


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
    _recover_orphan_bible_dicts(conn, rows)
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
            conn.execute("DELETE FROM shot_scenes WHERE shot_id=?", (sid,))
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


# ---------- 一键全自动成片 ----------

@router.post("/projects/{project_id}/auto")
async def start_auto(project_id: str, body: dict | None = Body(None)):
    """启动全流程自动化：人物谱→定妆照+分集→每集（分镜→确认→关键帧→视频）→合成。
    自适应跳过已完成步骤，可重复点击从断点续做。
    body.export_dir：可选导出目录，每集成片合成后另存为「书名第N集.mp4」（同名已存在则跳过）。"""
    _project_or_404(project_id)
    from app import auto
    if auto.is_running(project_id):
        raise HTTPException(409, "该项目的自动成片已在进行中")
    export_dir = (body or {}).get("export_dir")
    auto.start(project_id, export_dir=export_dir)
    return {"status": "running"}


@router.get("/projects/{project_id}/auto/status")
def auto_status(project_id: str):
    _project_or_404(project_id)
    from app import auto
    return auto.status(project_id)


@router.post("/projects/{project_id}/auto/cancel")
def cancel_auto(project_id: str):
    _project_or_404(project_id)
    from app import auto
    return {"cancelled": auto.cancel(project_id)}


# ---------- 角色圣经 ----------

async def _bible_task(project_id: str, feedback: str = ""):
    conn = get_conn()
    try:
        chapters = rows_to_dicts(conn.execute(
            "SELECT * FROM chapters WHERE project_id=? ORDER BY idx", (project_id,)).fetchall())
        timeout_s = max(int(get_setting("bible_task_timeout_s") or BIBLE_TASK_TIMEOUT_S), 60)
        # 重新谱写时按角色名保留已有定妆照（重生圣经不应丢失一致性锚点）
        old_row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
        old_style = None
        old_bible = None
        if old_row and old_row["bible_json"]:
            old_bible = json.loads(old_row["bible_json"])
        bible = await asyncio.wait_for(
            generate_bible(chapters, feedback=feedback, previous_bible=old_bible),
            timeout=timeout_s,
        )
        if old_bible:
            old_style = (old_bible.get("world") or {}).get("visual_style_canonical")
            old_refs = {c.get("name"): c.get("ref_image_path")
                        for c in old_bible.get("characters", [])}
            for c in bible.characters:
                c.ref_image_path = old_refs.get(c.name) or None
        # 重谱后画风变化 → 旧画风定妆照与旧视频全部作废（否则图像信号会把新画风拉回旧画风）
        if old_style and bible.world.visual_style_canonical != old_style:
            _purge_for_style_change(project_id, bible)
        conn.execute(
            "UPDATE projects SET bible_json=?, bible_version=bible_version+1, bible_status='ready', bible_error=NULL, status='bible_ready' WHERE id=?",
            (bible.model_dump_json(), project_id))
        conn.commit()
    except asyncio.TimeoutError:
        conn.execute(
            "UPDATE projects SET bible_status='failed', bible_error=? WHERE id=?",
            (f"人物谱解析/修复超时（超过 {timeout_s} 秒），请重新谱写。", project_id),
        )
        conn.commit()
    except asyncio.CancelledError:
        conn.execute(
            "UPDATE projects SET bible_status='failed', bible_error=? WHERE id=?",
            (BIBLE_INTERRUPTED_ERROR, project_id),
        )
        conn.commit()
        raise
    except (StageError, Exception) as exc:  # noqa: BLE001
        conn.execute("UPDATE projects SET bible_status='failed', bible_error=? WHERE id=?", (str(exc)[:800], project_id))
        conn.commit()


@router.post("/projects/{project_id}/bible")
async def start_bible(project_id: str, body: dict | None = Body(None)):
    p = _project_or_404(project_id)
    if p["bible_status"] == "running" and _bible_task_active(project_id):
        raise HTTPException(409, "角色圣经正在生成中")
    feedback = str((body or {}).get("feedback") or "").strip()
    if len(feedback) > 2000:
        raise HTTPException(400, "打回要求过长，请控制在 2000 字以内")
    conn = get_conn()
    conn.execute("UPDATE projects SET bible_status='running', bible_error=NULL WHERE id=?", (project_id,))
    conn.commit()
    _track_bible_task(project_id, asyncio.create_task(_bible_task(project_id, feedback)))
    return {"status": "running"}


def _purge_for_style_change(project_id: str, instance: "Bible") -> dict:
    """画风变更的连锁失效：清理全项目旧画风视频产物，并作废旧画风定妆照
    （旧定妆照/旧尾帧是比文字 prompt 更强的画风信号，残留会把新画风拉回旧画风）。"""
    purged = worker.purge_project_video_artifacts(project_id)
    refs_cleared = 0
    for c in instance.characters:
        if c.ref_image_path:
            try:
                Path(c.ref_image_path).unlink()
            except OSError:
                pass
            c.ref_image_path = None
            refs_cleared += 1
    conn = get_conn()
    conn.execute("UPDATE projects SET refs_status='idle' WHERE id=?", (project_id,))
    conn.commit()
    return {**purged, "refs_cleared": refs_cleared}


@router.put("/projects/{project_id}/bible")
def edit_bible(project_id: str, body: dict):
    p = _project_or_404(project_id)
    instance, errors = schema_errors(Bible, body)
    if errors:
        raise HTTPException(422, "；".join(errors))
    from app.validators import validate_bible
    errors = validate_bible(instance)
    if errors:
        raise HTTPException(422, "；".join(errors))
    old_style = None
    if p["bible_json"]:
        old_style = (json.loads(p["bible_json"]).get("world") or {}).get("visual_style_canonical")
    style_changed = bool(old_style) and instance.world.visual_style_canonical != old_style
    purge_info = _purge_for_style_change(project_id, instance) if style_changed else None
    conn = get_conn()
    conn.execute("UPDATE projects SET bible_json=?, bible_version=bible_version+1 WHERE id=?",
                 (instance.model_dump_json(), project_id))
    conn.commit()
    return {"bible_version_bumped": True, "style_changed": style_changed, "purged": purge_info}


@router.put("/projects/{project_id}/characters/{character_name}/portrait")
def edit_portrait_prompt(project_id: str, character_name: str, body: dict):
    """更新单个角色的画像描述（定妆照生成词）。传空字符串/null 恢复为默认合成描述。"""
    p = _project_or_404(project_id)
    if not p["bible_json"]:
        raise HTTPException(409, "请先生成角色圣经")
    prompt_text = (body.get("portrait_prompt") or "").strip()
    if prompt_text and not 10 <= len(prompt_text) <= 400:
        raise HTTPException(422, f"画像描述长度 {len(prompt_text)} 字，要求 10~400 字（留空则恢复默认）")
    bible = json.loads(p["bible_json"])
    target = next((c for c in bible.get("characters", []) if c.get("name") == character_name), None)
    if target is None:
        raise HTTPException(404, f"角色不存在：{character_name}")
    target["portrait_prompt_override"] = prompt_text or None
    conn = get_conn()
    conn.execute("UPDATE projects SET bible_json=? WHERE id=?",
                 (json.dumps(bible, ensure_ascii=False), project_id))
    conn.commit()
    return {"saved": True, "reset_to_default": not prompt_text}


# ---------- 角色定妆照（人物跨集一致性） ----------

async def _refs_task(project_id: str, only_character: str | None):
    from app.refs import generate_refs
    conn = get_conn()
    try:
        # 重做定妆照前，先清理旧人物图衍生的评审视频与成品（按受影响角色范围）
        p = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
        if only_character:
            names = [only_character]
        elif p and p["bible_json"]:
            names = [c["name"] for c in json.loads(p["bible_json"]).get("characters", [])]
        else:
            names = []
        worker.purge_character_video_artifacts(project_id, names)
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
    conn.execute("UPDATE projects SET refs_status='running', refs_error=NULL, refs_target=? WHERE id=?",
                 (only, project_id))
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
        # 传入原文（不止摘要）：分集质量取决于真实情节细节，仅靠 200 字摘要会丢钩子/反转/台词
        chapter_texts = [(ch["idx"], ch["title"] or "", ch["content"] or "") for ch in chapters]

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
                want_timeline=(guard == 1), chapter_texts=chapter_texts)
            if guard == 1:
                key_timeline = batch.key_timeline
            advanced = batch.episodes[-1].source_chapters[-1]
            if advanced < start_chapter:  # 无进展，避免死循环
                raise StageError("剧集规划", [f"分批续写未推进（停在第 {start_chapter} 章），请重试"])
            all_episodes.extend(batch.episodes)
            start_chapter = advanced + 1

        # 干净替换：清空本项目所有旧剧集及衍生数据，避免重复集/撞号
        worker.delete_project_episodes(project_id)
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

# 正在进行的分镜生成任务，按 episode_id 跟踪，便于手动取消
_storyboard_tasks: dict[str, asyncio.Task] = {}


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
            s.action_desc = normalize_action_desc(s.action_desc)
            conn.execute(
                "INSERT INTO shots(id, episode_id, shot_no, duration_s, shot_size, camera_move, scene_setting, characters, action_desc, first_frame_desc, last_frame_desc, source_excerpt, narration, dialogues, transition, continuity_from_prev) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_id("shot"), episode_id, s.shot_no, s.duration_s, s.shot_size, s.camera_move,
                 s.scene_setting, json.dumps(s.characters, ensure_ascii=False), s.action_desc,
                 s.first_frame_desc, s.last_frame_desc, s.source_excerpt, s.narration,
                 json.dumps([d.model_dump() for d in s.dialogues], ensure_ascii=False),
                 s.transition, int(s.continuity_from_prev)))
        # 兜底落地：修复次数用完时以最后一次输出为准，残余校验问题写入 script_error 提示人工修订
        residual = list(getattr(board, "residual_errors", []) or [])
        if residual:
            note = "已采用最后一次输出（修复次数用完，以下问题未完全修复，可手动修改或重新生成）：" + "；".join(residual)
            conn.execute("UPDATE episodes SET status='scripted', script_error=? WHERE id=?",
                         (note[:800], episode_id))
        else:
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
    task = asyncio.create_task(_storyboard_task(episode_id))
    _storyboard_tasks[episode_id] = task
    task.add_done_callback(lambda t, eid=episode_id: _storyboard_tasks.pop(eid, None))
    return {"status": "scripting"}


async def _storyboard_guarded(episode_id: str, sem: asyncio.Semaphore):
    """带并发上限的分镜任务，用于批量生成时不一次性打爆模型网关。"""
    async with sem:
        await _storyboard_task(episode_id)


@router.post("/projects/{project_id}/storyboard-all")
async def start_storyboard_all(project_id: str):
    """为本项目所有【待分镜】(planned) 剧集批量生成分镜，限并发逐集触发。
    必须是 async def：sync 路由跑在无事件循环的线程池里，asyncio.create_task 会抛
    'no running event loop'，导致状态已置为 scripting 但任务从未启动（前端显示分镜中、模型却收不到请求）。
    同时回收状态卡在 scripting 但无在跑任务的孤儿集，便于一键修复。"""
    _project_or_404(project_id)
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, status FROM episodes WHERE project_id=? AND status IN ('planned','scripting') ORDER BY episode_no",
        (project_id,)).fetchall()
    # 待分镜的；以及卡在“分镜中”却没有在跑任务的孤儿（需重新触发）
    ids = [r["id"] for r in rows if r["status"] == "planned" or r["id"] not in _storyboard_tasks]
    if not ids:
        raise HTTPException(409, "没有待分镜的剧集（仅对“待分镜”或卡住的“分镜中”集生效）")
    placeholders = ",".join("?" for _ in ids)
    conn.execute(f"UPDATE episodes SET status='scripting', script_error=NULL WHERE id IN ({placeholders})", ids)
    conn.commit()
    sem = asyncio.Semaphore(max(int(get_setting("storyboard_concurrency") or 2), 1))
    for eid in ids:
        task = asyncio.create_task(_storyboard_guarded(eid, sem))
        _storyboard_tasks[eid] = task
        task.add_done_callback(lambda t, e=eid: _storyboard_tasks.pop(e, None))
    return {"started": len(ids)}


@router.post("/episodes/{episode_id}/storyboard/cancel")
def cancel_storyboard(episode_id: str):
    """手动取消正在进行的分镜生成请求，解除 scripting 锁定，便于重新发起。
    用于模型侧卡死/异常导致状态长期停留在“分镜中”的情况。"""
    ep = _episode_or_404(episode_id)
    if ep["status"] != "scripting":
        raise HTTPException(409, "当前没有正在进行的分镜生成")
    task = _storyboard_tasks.pop(episode_id, None)
    if task and not task.done():
        task.cancel()
    conn = get_conn()
    has_shots = conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,)).fetchone()["c"]
    # 之前已有分镜则回到“待确认”，否则回到“待分镜”
    fallback = "scripted" if has_shots else "planned"
    conn.execute("UPDATE episodes SET status=?, script_error=NULL WHERE id=?", (fallback, episode_id))
    conn.commit()
    return {"status": fallback}


@router.get("/episodes/{episode_id}")
def episode_detail(episode_id: str):
    ep = dict(_episode_or_404(episode_id))
    conn = get_conn()
    ep["source_chapters"] = json.loads(ep["source_chapters"] or "[]")
    ep["cost_cny"] = worker.episode_cost(episode_id)
    ep["cost_limit_cny"] = float(get_setting("episode_cost_limit_cny") or 100)
    shots = rows_to_dicts(conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)).fetchall())
    from app.config import PROJECTS_DIR
    shots_by_no = {s["shot_no"]: s for s in shots}
    for s in shots:
        s["characters"] = json.loads(s["characters"] or "[]")
        s["dialogues"] = json.loads(s["dialogues"] or "[]")
        s["est_cost_cny"] = shot_cost_cny(s["duration_s"])
        s["required_keyframes"] = worker.required_keyframe_kinds(s)
        if s["scene_status"] != "generating":
            ready = worker.shot_keyframes_ready(s)
            if ready and s["scene_status"] == "approved":
                s["scene_status"] = "approved"
            elif not ready and s["scene_status"] == "approved":
                s["scene_status"] = "review"
        # 链失效：视频使用的首/尾关键图已不是当前过审图 → 需从本镜往后重生
        s["video_stale"] = False
        if s["continuity_from_prev"] and s["adopted_version_id"]:
            av = conn.execute("SELECT image_inputs FROM shot_versions WHERE id=?", (s["adopted_version_id"],)).fetchone()
            meta = json.loads((av["image_inputs"] if av else None) or "{}")
            pred = shots_by_no.get(s["shot_no"] - 1)
            expected_first = pred.get("approved_tail_scene_id") if pred else None
            expected_last = s.get("approved_tail_scene_id")
            if (expected_first and meta.get("first_frame_scene_id") != expected_first) or (
                    expected_last and meta.get("last_frame_scene_id") != expected_last):
                s["video_stale"] = True
        elif s["adopted_version_id"]:
            av = conn.execute("SELECT image_inputs FROM shot_versions WHERE id=?", (s["adopted_version_id"],)).fetchone()
            meta = json.loads((av["image_inputs"] if av else None) or "{}")
            expected_first = s.get("approved_head_scene_id")
            expected_last = s.get("approved_tail_scene_id")
            if (expected_first and meta.get("first_frame_scene_id") != expected_first) or (
                    expected_last and meta.get("last_frame_scene_id") != expected_last):
                s["video_stale"] = True
        # 场景关键帧候选（图像评审阶段）
        scenes = rows_to_dicts(conn.execute(
            "SELECT id, version_no, kind, image_path, status, error, qa_json FROM shot_scenes WHERE shot_id=? ORDER BY kind, version_no DESC",
            (s["id"],)).fetchall())
        for sc in scenes:
            sc["qa"] = json.loads(sc["qa_json"]) if sc["qa_json"] else None
            sc.pop("qa_json", None)
            if sc.get("image_path"):
                rel = Path(sc["image_path"]).relative_to(PROJECTS_DIR).as_posix()
                sc["image_url"] = f"/media/{rel}"
            sc.pop("image_path", None)
        s["scenes"] = scenes
        versions = rows_to_dicts(conn.execute(
            "SELECT * FROM shot_versions WHERE shot_id=? ORDER BY version_no DESC", (s["id"],)).fetchall())
        for v in versions:
            v["qa"] = json.loads(v["qa_json"]) if v["qa_json"] else None
            v.pop("qa_json", None)
            meta = json.loads(v.get("image_inputs") or "{}")
            v["image_inputs"] = {"first_frame_used": bool(meta.get("first_frame_used")),
                                 "first_frame_src": meta.get("first_frame_src"),
                                 "first_frame_scene_id": meta.get("first_frame_scene_id"),
                                 "last_frame_used": bool(meta.get("last_frame_used")),
                                 "last_frame_src": meta.get("last_frame_src"),
                                 "last_frame_scene_id": meta.get("last_frame_scene_id")}
            if v["video_path"]:
                rel_path = Path(v["video_path"]).relative_to(PROJECTS_DIR).as_posix()
                v["video_url"] = f"/media/{rel_path}"
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
                "action_desc", "first_frame_desc", "last_frame_desc", "source_excerpt", "narration", "dialogues", "transition", "continuity_from_prev"):
        if key in body:
            merged[key] = body[key]
    merged["duration_s"] = config.FIXED_VIDEO_DURATION_S
    instance, errors = schema_errors(Shot, {k: merged[k] for k in (
        "shot_no", "duration_s", "shot_size", "camera_move", "scene_setting", "characters",
        "action_desc", "first_frame_desc", "last_frame_desc", "source_excerpt", "narration", "dialogues", "transition", "continuity_from_prev")})
    if errors:
        raise HTTPException(422, "；".join(errors))
    instance.action_desc = normalize_action_desc(instance.action_desc)
    conn.execute(
        "UPDATE shots SET duration_s=?, shot_size=?, camera_move=?, scene_setting=?, characters=?, action_desc=?, first_frame_desc=?, last_frame_desc=?, source_excerpt=?, narration=?, dialogues=?, transition=?, continuity_from_prev=? WHERE id=?",
        (instance.duration_s, instance.shot_size, instance.camera_move, instance.scene_setting,
         json.dumps(instance.characters, ensure_ascii=False), instance.action_desc, instance.first_frame_desc, instance.last_frame_desc,
         instance.source_excerpt, instance.narration,
         json.dumps([d.model_dump() for d in instance.dialogues], ensure_ascii=False),
         instance.transition, int(instance.continuity_from_prev), shot_id))
    # 编辑后剧集回到 scripted（需重新确认才能生成）
    conn.execute("UPDATE episodes SET status='scripted' WHERE id=? AND status='confirmed'", (shot["episode_id"],))
    conn.commit()
    return {"ok": True}


def confirm_episode_core(episode_id: str) -> dict:
    """人工确认门（PRD P3）的纯逻辑：全量业务校验通过才进入 confirmed。
    失败抛 ValueError（消息面向 UI）；供路由与一键全自动复用，避免逻辑分叉。"""
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
        raise ValueError("本集还没有分镜脚本")
    shots = [Shot(
        shot_no=r["shot_no"], duration_s=r["duration_s"], shot_size=r["shot_size"], camera_move=r["camera_move"],
        scene_setting=r["scene_setting"], characters=json.loads(r["characters"] or "[]"),
        action_desc=r["action_desc"], first_frame_desc=r["first_frame_desc"] or "", last_frame_desc=r["last_frame_desc"] or "",
        source_excerpt=r["source_excerpt"] or "",
        narration=r["narration"], dialogues=json.loads(r["dialogues"] or "[]"),
        transition=r["transition"] or "硬切", continuity_from_prev=bool(r["continuity_from_prev"])) for r in shots_rows]
    board = Storyboard(episode_no=ep["episode_no"], shots=shots)
    errors = validate_storyboard(board, bible, compact_target)
    if errors:
        raise ValueError(json.dumps(errors, ensure_ascii=False))
    # 预编译全部 prompt，把参数错误拦在花钱之前
    try:
        for s in shots:
            compile_prompt(s, bible)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Prompt 编译失败：{exc}")
    est = sum(shot_cost_cny(s.duration_s) for s in shots)
    conn.execute("UPDATE episodes SET status='confirmed' WHERE id=?", (episode_id,))
    conn.commit()
    return {"confirmed": True, "estimated_cost_cny": round(est, 2), "shot_count": len(shots)}


@router.post("/episodes/{episode_id}/confirm")
def confirm_episode(episode_id: str):
    """人工确认门（PRD P3）：全量业务校验通过才进入 confirmed。"""
    try:
        return confirm_episode_core(episode_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc))


# ---------- 生成 ----------

# ----- 场景关键帧（视频前置：图像生成 + 评审） -----

@router.post("/shots/{shot_id}/scene")
def generate_scene(shot_id: str, body: dict | None = Body(None)):
    """为单个镜头生成场景关键帧候选（1~3 张）并自动评审。"""
    try:
        return worker.enqueue_scene(shot_id, kinds=(body or {}).get("kinds"))
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@router.post("/shots/{shot_id}/scene/approve")
def approve_scene(shot_id: str, body: dict):
    """人工选定某张关键帧候选（首图或尾图）。"""
    scene_id = body.get("scene_id")
    conn = get_conn()
    sc = conn.execute("SELECT * FROM shot_scenes WHERE id=? AND shot_id=?", (scene_id, shot_id)).fetchone()
    if not sc or sc["status"] != "succeeded" or not sc["image_path"]:
        raise HTTPException(409, "该场景候选不存在或未成功")
    if sc["kind"] == "head":
        conn.execute("UPDATE shots SET approved_head_scene_id=? WHERE id=?", (scene_id, shot_id))
    elif sc["kind"] == "tail":
        conn.execute("UPDATE shots SET approved_tail_scene_id=?, approved_scene_id=? WHERE id=?", (scene_id, scene_id, shot_id))
    else:
        raise HTTPException(409, f"未知关键帧类型：{sc['kind']}")
    shot = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    status = "approved" if worker.shot_keyframes_ready(shot) else "review"
    conn.execute("UPDATE shots SET scene_status=? WHERE id=?", (status, shot_id))
    conn.commit()
    return {"approved_scene_id": scene_id, "kind": sc["kind"], "scene_status": status}


@router.delete("/scenes/{scene_id}")
def delete_scene(scene_id: str):
    """删除一张关键帧候选（含图片文件）。若删的是已采用的首/尾图，则清空该采用并重算场景状态。"""
    conn = get_conn()
    sc = conn.execute("SELECT * FROM shot_scenes WHERE id=?", (scene_id,)).fetchone()
    if not sc:
        raise HTTPException(404, "关键帧不存在")
    shot_id = sc["shot_id"]
    if sc["image_path"]:
        try:
            Path(sc["image_path"]).unlink()
        except OSError:
            pass
    conn.execute("DELETE FROM shot_scenes WHERE id=?", (scene_id,))
    conn.execute("UPDATE shots SET approved_head_scene_id=NULL WHERE id=? AND approved_head_scene_id=?", (shot_id, scene_id))
    conn.execute("UPDATE shots SET approved_tail_scene_id=NULL, approved_scene_id=NULL WHERE id=? AND approved_tail_scene_id=?", (shot_id, scene_id))
    shot = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    remaining = conn.execute("SELECT COUNT(*) AS c FROM shot_scenes WHERE shot_id=?", (shot_id,)).fetchone()["c"]
    status = "approved" if worker.shot_keyframes_ready(shot) else ("review" if remaining else "none")
    conn.execute("UPDATE shots SET scene_status=? WHERE id=?", (status, shot_id))
    conn.commit()
    video_purged = 0
    if remaining == 0:
        # 关键帧删空 → 旧成片已无首尾帧依据，一并删除（含成品失效）
        video_purged = worker.purge_shot_videos(shot_id)
    return {"deleted": scene_id, "scene_status": status, "video_purged": video_purged}


@router.delete("/versions/{version_id}")
def delete_version(version_id: str):
    """删除一个已生成的视频版本（含文件）。若是采用版则清空采用、使本集成品失效。"""
    conn = get_conn()
    v = conn.execute("SELECT id FROM shot_versions WHERE id=?", (version_id,)).fetchone()
    if not v:
        raise HTTPException(404, "视频版本不存在")
    shot_id = worker.delete_video_version(version_id)
    return {"deleted": version_id, "shot_id": shot_id}


@router.post("/episodes/{episode_id}/scenes-all")
def generate_scenes_all(episode_id: str):
    """为本集所有【尚无通过评审首/尾关键帧】的镜头批量生成关键帧。"""
    _episode_or_404(episode_id)
    conn = get_conn()
    rows = rows_to_dicts(conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,)).fetchall()
    )
    targets = [r for r in rows if r["scene_status"] != "approved" or not worker.shot_keyframes_ready(r)]
    started = 0
    for r in targets:
        try:
            worker.enqueue_scene(r["id"]); started += 1
        except ValueError:
            pass
    if not started:
        raise HTTPException(409, "没有需要生成关键帧的镜头（首/尾图都已通过评审）")
    return {"started": started}


# ----- 视频生成（需首/尾关键帧通过评审后） -----

def _shot_by_no(episode_id: str, shot_no: int):
    return get_conn().execute(
        "SELECT id FROM shots WHERE episode_id=? AND shot_no=?", (episode_id, shot_no)).fetchone()


@router.post("/episodes/{episode_id}/generate")
def generate_episode(episode_id: str, body: dict | None = None):
    """批量生成整集视频（首尾关键帧）：同场景连续镜的首帧来自上一镜预生成尾图，
    换场/首镜的首帧来自本镜预生成首图；所有镜头都使用本镜预生成尾图作为尾帧，可并行执行。
    body.from_shot_no：只从该镜起、沿其连续段往后重生（中途改动后用）。"""
    ep = _episode_or_404(episode_id)
    if ep["status"] not in ("confirmed", "generating", "done"):
        raise HTTPException(409, "分镜脚本未确认（先在工作台点击确认分镜）")
    conn = get_conn()
    shots = rows_to_dicts(conn.execute(
        "SELECT id, shot_no, continuity_from_prev FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,)).fetchall())
    from_no = (body or {}).get("from_shot_no")
    if from_no:
        selected = []
        for i, s in enumerate(shots):
            if s["shot_no"] == from_no:
                selected = [s]
                for nxt in shots[i + 1:]:
                    if nxt["continuity_from_prev"]:
                        selected.append(nxt)
                    else:
                        break
                break
        if not selected:
            raise HTTPException(404, f"未找到镜 {from_no}")
    else:
        selected = shots
    # 选中镜清空旧采用版，使新生成版被自动采用；下游连续镜使用的是预生成尾图，不再依赖上一镜视频。
    sel_ids = [s["id"] for s in selected]
    conn.execute(
        f"UPDATE shots SET adopted_version_id=NULL WHERE id IN ({','.join('?' for _ in sel_ids)})", sel_ids)
    conn.commit()
    results = []
    for s in selected:
        after = None
        if s["continuity_from_prev"] and s["shot_no"] > 1:
            pr = _shot_by_no(episode_id, s["shot_no"] - 1)
            after = pr["id"] if pr else None
        try:
            r = worker.enqueue_shot(s["id"], after_shot_id=after)
            # 幂等命中（已有相同成片）：上面清空了采用版，这里把复用版重新采用回去
            if r.get("reused") and r.get("version_id"):
                conn.execute("UPDATE shots SET adopted_version_id=? WHERE id=?", (r["version_id"], s["id"]))
            results.append({"shot_id": s["id"], **r})
        except Exception as exc:  # noqa: BLE001
            results.append({"shot_id": s["id"], "error": str(exc)})
    conn.commit()
    return {"enqueued": results}


@router.post("/shots/{shot_id}/generate")
async def generate_shot(shot_id: str, body: dict | None = None):
    body = body or {}
    conn = get_conn()
    shot_row = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot_row:
        raise HTTPException(404, "镜头不存在")
    # 带 AI 评语重生：取「当前采用版 / 最新成功版」的问题清单（必要时现场跑评审），
    # 作为本次必须改正项写入 prompt，避免模型再犯同样的错。
    critique = None
    if body.get("with_critique"):
        ref = None
        if shot_row["adopted_version_id"]:
            ref = conn.execute("SELECT id FROM shot_versions WHERE id=? AND status='succeeded'",
                               (shot_row["adopted_version_id"],)).fetchone()
        if not ref:
            ref = conn.execute(
                "SELECT id FROM shot_versions WHERE shot_id=? AND status='succeeded' ORDER BY version_no DESC LIMIT 1",
                (shot_id,)).fetchone()
        if ref:
            critique = await worker.critique_version(ref["id"])
    # 同场景接上镜的单镜重生使用上一镜已过审尾图作为首帧。
    after = None
    if shot_row["continuity_from_prev"] and shot_row["shot_no"] > 1:
        pr = _shot_by_no(shot_row["episode_id"], shot_row["shot_no"] - 1)
        after = pr["id"] if pr else None
    try:
        return worker.enqueue_shot(
            shot_id,
            prompt_override=body.get("prompt_override"),
            extra_negative=body.get("extra_negative"),
            reroll=bool(body.get("reroll")) or bool(body.get("with_critique")),
            critique=critique, after_shot_id=after)
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


# ---------- 成片台：预览 / 拼接 / 导出 ----------

@router.get("/episodes/{episode_id}/mix-status")
def mix_status(episode_id: str):
    """按镜号顺序返回每镜成片 URL、整体进度、已合成成品（若有）。"""
    _episode_or_404(episode_id)
    return worker.episode_mix_status(episode_id)


@router.post("/episodes/{episode_id}/concatenate")
def concatenate(episode_id: str):
    """把本集所有已采用的视频片段按镜号顺序拼接成一个 MP4。"""
    _episode_or_404(episode_id)
    try:
        return worker.concatenate_episode(episode_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"ffmpeg 合成失败：{exc}")


# ---------- 文件系统目录浏览（本机部署，供导出目录选择器使用） ----------

def _list_drives() -> list[str]:
    if os.name != "nt":
        return []
    return [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]


@router.get("/system/browse")
def browse_dir(path: str = ""):
    """列出某目录下的子目录，供前端目录选择器逐级浏览。
    path 为空时：Windows 返回盘符列表，POSIX 从根目录开始。"""
    drives = _list_drives()
    p = (path or "").strip()
    if not p:
        if os.name == "nt":
            return {"path": "", "parent": None, "drives": drives,
                    "dirs": [{"name": d, "path": d} for d in drives]}
        p = "/"
    base = Path(p)
    if not base.exists() or not base.is_dir():
        raise HTTPException(404, f"目录不存在：{p}")
    dirs = []
    try:
        for child in sorted(base.iterdir(), key=lambda x: x.name.lower()):
            try:
                if child.is_dir():
                    dirs.append({"name": child.name, "path": str(child)})
            except OSError:
                continue  # 个别子项无权访问/不可达，跳过
    except PermissionError:
        raise HTTPException(403, f"无权访问：{p}")
    parent = str(base.parent) if base.parent != base else None
    return {"path": str(base), "parent": parent, "drives": drives, "dirs": dirs}


@router.post("/system/mkdir")
def make_dir(body: dict):
    """在指定父目录下新建文件夹，供选择器「新建文件夹」使用。"""
    parent = (body.get("path") or "").strip()
    name = (body.get("name") or "").strip()
    if not parent or not name:
        raise HTTPException(422, "缺少父目录或文件夹名")
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', name):
        raise HTTPException(422, '文件夹名含非法字符（不能包含 \\ / : * ? " < > |）')
    dest = Path(parent) / name
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise HTTPException(400, f"创建失败：{e}")
    return {"path": str(dest)}


# ---------- 系统 ----------

@router.get("/system/health")
def health():
    from app import config, hiagent

    def option(provider: str, model: str, available: bool = True) -> dict:
        return {"provider": provider, "model": model, "available": available}

    def selected(kind: str, label: str, options: list[dict]) -> dict:
        provider = hiagent.active_provider(kind)
        active = next((o for o in options if o["provider"] == provider), options[0])
        return {
            "key": kind,
            "label": label,
            "provider": active["provider"],
            "model": active["model"],
            "options": options,
        }

    models = {
        "text": selected("text", "Text 模型", [
            option("hiagent", hiagent.active_model("text", "hiagent"), bool(config.HIAGENT_API_KEY)),
            option("openrouter", hiagent.active_model("text", "openrouter"), bool(config.OPENROUTER_API_KEY)),
            option("bailian", hiagent.active_model("text", "bailian"), bool(config.BAILIAN_API_KEY)),
        ]),
        "vlm": selected("vlm", "VLM 模型", [
            option("hiagent", hiagent.active_model("vlm", "hiagent"), bool(config.HIAGENT_API_KEY)),
            option("openrouter", hiagent.active_model("vlm", "openrouter"), bool(config.OPENROUTER_API_KEY)),
            option("bailian", hiagent.active_model("vlm", "bailian"), bool(config.BAILIAN_API_KEY)),
        ]),
        "video": selected("video", "视频模型", [
            option("hiagent", hiagent.active_model("video", "hiagent"), bool(config.HIAGENT_API_KEY)),
            option("openrouter", "", False),
        ]),
        "image": selected("image", "图像模型", [
            option("hiagent", hiagent.active_model("image", "hiagent"), bool(config.HIAGENT_API_KEY)),
            option("openrouter", "", False),
        ]),
    }
    return {
        "ok": True,
        "gateway": config.HIAGENT_BASE_URL,
        "key_configured": bool(config.HIAGENT_API_KEY),
        "model_route": get_setting("model_route") or "hiagent",
        "openrouter_key_configured": bool(config.OPENROUTER_API_KEY),
        "bailian_key_configured": bool(config.BAILIAN_API_KEY),
        "hiagent_model_text": hiagent.active_model("text", "hiagent"),
        "hiagent_model_vlm": hiagent.active_model("vlm", "hiagent"),
        "hiagent_model_video": hiagent.active_model("video", "hiagent"),
        "hiagent_model_image": hiagent.active_model("image", "hiagent"),
        "openrouter_model_text": hiagent.active_model("text", "openrouter"),
        "openrouter_model_vlm": hiagent.active_model("vlm", "openrouter"),
        "bailian_model_text": hiagent.active_model("text", "bailian"),
        "bailian_model_vlm": hiagent.active_model("vlm", "bailian"),
        "audio_enabled": (get_setting("audio_enabled") or "false") == "true",
        "audio_key_configured": bool(config.BAILIAN_API_KEY),
        "audio_tts_model": config.BAILIAN_TTS_MODEL,
        "audio_asr_model": config.BAILIAN_ASR_MODEL,
        "audio_voice": get_setting("audio_voice") or config.BAILIAN_TTS_VOICE,
        "models": models,
    }


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


# ---------- 正音词库 + 配音（仅在 audio_enabled 开启时进入生成流程） ----------

@router.get("/projects/{project_id}/pronunciation")
def list_pronunciation(project_id: str):
    _project_or_404(project_id)
    rows = rows_to_dicts(get_conn().execute(
        "SELECT id, term, tts_alias, asr_aliases, level FROM pronunciation WHERE project_id=? ORDER BY id",
        (project_id,)).fetchall())
    for r in rows:
        r["asr_aliases"] = json.loads(r["asr_aliases"] or "[]")
    return {"terms": rows}


@router.put("/projects/{project_id}/pronunciation")
def save_pronunciation(project_id: str, body: dict):
    """整表替换本项目正音词库。body.terms=[{term, tts_alias, asr_aliases:[], level}]。"""
    _project_or_404(project_id)
    terms = body.get("terms") or []
    cleaned = []
    for i, t in enumerate(terms):
        term = (t.get("term") or "").strip()
        if not term:
            continue
        level = (t.get("level") or "A").upper()
        if level not in ("S", "A", "B"):
            raise HTTPException(422, f"第 {i+1} 条 level=「{level}」非法，只能是 S/A/B")
        aliases = t.get("asr_aliases") or []
        if isinstance(aliases, str):
            aliases = [a.strip() for a in re.split(r"[,，\s]+", aliases) if a.strip()]
        cleaned.append((term, (t.get("tts_alias") or "").strip(),
                        json.dumps(aliases, ensure_ascii=False), level))
    conn = get_conn()
    conn.execute("DELETE FROM pronunciation WHERE project_id=?", (project_id,))
    conn.executemany(
        "INSERT INTO pronunciation(project_id, term, tts_alias, asr_aliases, level, created_at) "
        "VALUES(?,?,?,?,?,?)",
        [(project_id, term, alias, aj, lv, now()) for term, alias, aj, lv in cleaned])
    conn.commit()
    return {"saved": len(cleaned)}


@router.post("/episodes/{episode_id}/audio")
async def generate_episode_audio(episode_id: str):
    """为本集所有镜头生成配音并做 ASR 预检（需先开启 audio_enabled，且分镜已就绪）。"""
    _episode_or_404(episode_id)
    from app import audio as audio_mod
    if not audio_mod.is_enabled():
        raise HTTPException(409, "音频功能未开启，请先在监制房打开「配音/音频」开关")
    if not config.BAILIAN_API_KEY:
        raise HTTPException(409, "未配置 BAILIAN_API_KEY（百炼），无法生成配音")
    try:
        return await audio_mod.generate_episode_audio(episode_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@router.get("/episodes/{episode_id}/audio")
def episode_audio_status(episode_id: str):
    _episode_or_404(episode_id)
    from app import audio as audio_mod
    return audio_mod.episode_audio_status(episode_id)


@router.get("/settings")
def get_settings():
    rows = get_conn().execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


@router.put("/settings")
def put_settings(body: dict):
    for key, value in body.items():
        skey = str(key)
        sval = str(value).strip()
        if skey == "model_text_provider" and sval not in {"hiagent", "openrouter", "bailian"}:
            raise HTTPException(422, f"{skey} 只能是 hiagent、openrouter 或 bailian")
        if skey == "model_vlm_provider" and sval not in {"hiagent", "openrouter", "bailian"}:
            raise HTTPException(422, f"{skey} 只能是 hiagent、openrouter 或 bailian")
        if skey == "model_route" and sval not in {"hiagent", "openrouter"}:
            raise HTTPException(422, f"{skey} 只能是 hiagent 或 openrouter")
        if skey in {"model_video_provider", "model_image_provider"} and sval != "hiagent":
            raise HTTPException(422, "当前视频/图像生成只支持火山 HiAgent")
        if skey == "audio_enabled" and sval.lower() not in {"true", "false"}:
            raise HTTPException(422, "audio_enabled 只能是 true 或 false")
        if skey == "audio_enabled":
            sval = sval.lower()
        set_setting(skey, sval)
        if skey == "model_route":
            set_setting("model_text_provider", sval)
            set_setting("model_vlm_provider", sval)
    return {"ok": True}
