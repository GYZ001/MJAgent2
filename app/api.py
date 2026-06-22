"""REST API。文本阶段（圣经/规划/分镜）为后台任务 + 状态轮询；视频阶段走 worker 队列。"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import string
from pathlib import Path

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile

from app import config, errors, worker
from app.compiler import clip_duration_value, compile_prompt, shot_cost_cny
from app.db import get_conn, get_setting, log_provider_call, new_id, now, rows_to_dicts, set_setting
from app.ingest import ingest_novel
from app.schemas import Bible, EpisodeScreenplay, Shot, Storyboard, schema_errors
from app.stages import (StageError, generate_bible, generate_screenplay, generate_storyboard_next_shot,
                        generate_storyboard_outline, time_agent_compress_durations)
from app.validators import (compact_durations_to_budget, compress_durations_within_floors,
                            enforced_min_duration, relieve_spoken_overflow,
                            normalize_action_desc, normalize_continuity, normalize_durations_for_speech,
                            normalize_episode_opening_shot, normalize_offbible_characters,
                            normalize_transition_visuals,
                            storyboard_duration_limit, storyboard_shot_count_range,
                            validate_screenplay, validate_storyboard,
                            validate_storyboard_preserves_key_content,
                            validate_storyboard_soundtrack)

router = APIRouter(prefix="/api")

BIBLE_TASK_TIMEOUT_S = 15 * 60
BIBLE_INTERRUPTED_ERROR = "人物谱任务已中断（服务重载或后台任务丢失），请重新谱写。"
FALLBACK_VISUAL_STYLE = "国漫风格，非真人CG渲染，统一电影感光影，暖灰色调"

_bible_tasks: dict[str, asyncio.Task] = {}
_refs_tasks: dict[str, asyncio.Task] = {}  # 定妆照生成后台任务，供停止按钮取消
_scene_refs_tasks: dict[str, asyncio.Task] = {}  # 场景图素材库生成后台任务，供停止按钮取消


def _placeholder_bible() -> Bible:
    """剧本/分镜可在人物谱未完成时先独立跑；此处提供最小占位圣经供文本阶段使用。"""
    return Bible.model_validate({
        "characters": [],
        "world": {
            "era": "",
            "genre": "",
            "visual_style_canonical": FALLBACK_VISUAL_STYLE,
        },
    })


def _project_bible_or_placeholder(project_row) -> Bible:
    raw = (project_row["bible_json"] or "").strip() if project_row else ""
    if raw:
        return Bible.model_validate(json.loads(raw))
    return _placeholder_bible()


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


def _refs_task_active(project_id: str) -> bool:
    task = _refs_tasks.get(project_id)
    return bool(task and not task.done())


def _start_refs_generation(project_id: str, only_character: str | None) -> bool:
    """启动定妆照任务。

    返回值表示是否成功启动；若已有同项目定妆任务在跑，则直接返回 False。
    """
    if _refs_task_active(project_id):
        return False
    conn = get_conn()
    if only_character is None:
        conn.execute(
            "UPDATE projects SET refs_status='running', refs_error=NULL, refs_target=NULL WHERE id=?",
            (project_id,),
        )
    else:
        conn.execute(
            "UPDATE projects SET refs_status='running', refs_error=NULL, refs_target=? WHERE id=?",
            (only_character, project_id),
        )
    conn.commit()
    task = asyncio.create_task(_refs_task(project_id, only_character))
    _refs_tasks[project_id] = task
    task.add_done_callback(lambda _t, pid=project_id: _refs_tasks.pop(pid, None))
    return True


def _scene_refs_task_active(project_id: str) -> bool:
    task = _scene_refs_tasks.get(project_id)
    return bool(task and not task.done())


def _start_scene_refs_generation(project_id: str, only_scene: str | None) -> bool:
    """启动场景图素材库生成任务。已有同项目任务在跑则返回 False。"""
    if _scene_refs_task_active(project_id):
        return False
    conn = get_conn()
    conn.execute(
        "UPDATE projects SET scene_refs_status='running', scene_refs_error=NULL, scene_refs_target=? WHERE id=?",
        (only_scene, project_id))
    conn.commit()
    task = asyncio.create_task(_scene_refs_task(project_id, only_scene))
    _scene_refs_tasks[project_id] = task
    task.add_done_callback(lambda _t, pid=project_id: _scene_refs_tasks.pop(pid, None))
    return True


async def _scene_bible_and_refs(project_id: str) -> None:
    """场景圣经生成 + 落库 + 触发场景图批量出图（在人物谱定稿后调用，与定妆照并行）。
    场景圣经是增强项：失败只记录到 scene_refs_error，不影响人物谱/分集主流程。"""
    from app.stages import generate_scene_bible
    conn = get_conn()
    try:
        p = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
        if not p or not p["bible_json"]:
            return
        bible = Bible.model_validate(json.loads(p["bible_json"]))
        # 初始场景清单只取前 N 章：避免一上来就铺满全片场景；更靠后的新场景留到分镜阶段反应式补图。
        from app.scenes import SCENE_BIBLE_CHAPTER_WINDOW
        chapters = rows_to_dicts(conn.execute(
            "SELECT * FROM chapters WHERE project_id=? ORDER BY idx LIMIT ?",
            (project_id, SCENE_BIBLE_CHAPTER_WINDOW)).fetchall())
        scenes = await generate_scene_bible(chapters, bible)
        # 重读 bible（人物谱可能已被并发流程更新），只覆盖 scenes 字段后回写。
        p2 = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
        data = json.loads(p2["bible_json"]) if p2 and p2["bible_json"] else bible.model_dump()
        data["scenes"] = [s.model_dump() for s in scenes]
        conn.execute("UPDATE projects SET bible_json=? WHERE id=?",
                     (json.dumps(data, ensure_ascii=False), project_id))
        conn.commit()
        _start_scene_refs_generation(project_id, None)
    except Exception as exc:  # noqa: BLE001 场景圣经失败不阻断主流程，仅透出状态
        public = errors.record_and_format(exc, action="scene_bible_generate", context={"project_id": project_id})
        conn.execute("UPDATE projects SET scene_refs_status='failed', scene_refs_error=? WHERE id=?",
                     (public, project_id))
        conn.commit()


def recover_bible_tasks() -> None:
    """启动时恢复人物谱任务（对齐 worker.recover_and_start 的语义）：
    进程重启/reload 会丢掉内存里的 asyncio.Task，但 DB 仍是 running。
    与其在下次访问时判孤儿并报错，不如用持久化的 feedback 重新拉起任务续跑。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, bible_feedback FROM projects WHERE bible_status='running'").fetchall()
    for r in rows:
        pid = r["id"]
        if _bible_task_active(pid):
            continue
        feedback = r["bible_feedback"] or ""
        _track_bible_task(pid, asyncio.get_running_loop().create_task(_bible_task(pid, feedback, trigger_full_refs=True)))


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


def _episode_source_text(conn, ep) -> str:
    source_chapters = json.loads(ep["source_chapters"] or "[]")
    if not source_chapters:
        return ""
    placeholders = ",".join("?" for _ in source_chapters)
    chapters = rows_to_dicts(conn.execute(
        f"SELECT * FROM chapters WHERE project_id=? AND idx IN ({placeholders}) ORDER BY idx",
        (ep["project_id"], *source_chapters)).fetchall())
    return "\n\n".join(f"【{ch['title']}】\n{ch['content']}" for ch in chapters)


def _load_screenplay(ep) -> EpisodeScreenplay | None:
    if not ep["screenplay_json"]:
        return None
    return EpisodeScreenplay.model_validate(json.loads(ep["screenplay_json"]))


LEGACY_SCREENPLAY_PURGED_ERROR = "旧版拍卡剧本已下线，请重新生成完整剧本。"


def _source_text_range_label(source_chapters: list[int]) -> str:
    if not source_chapters:
        return ""
    if len(source_chapters) == 1:
        return f"第 {source_chapters[0]} 章"
    return f"第 {source_chapters[0]}-{source_chapters[-1]} 章"


def _screenplay_mode(script: EpisodeScreenplay | None) -> str:
    if not script:
        return "none"
    return "full_script" if (script.full_script_text or "").strip() else "none"


def _prepare_screenplay_for_storage(ep, script: EpisodeScreenplay, *, keep_existing_id: str | None = None,
                                    keep_created_at: float | None = None) -> EpisodeScreenplay:
    source_chapters = json.loads(ep["source_chapters"] or "[]")
    stamp = now()
    script.mode = "full_script"
    script.id = script.id or keep_existing_id or new_id("script")
    script.title = (script.title or ep["title"] or "").strip()
    script.source_text_range = (script.source_text_range or _source_text_range_label(source_chapters)).strip()
    script.logline = (script.logline or ep["synopsis"] or "").strip()
    script.ending_hook = (script.ending_hook or ep["cliffhanger"] or "").strip()
    script.created_at = keep_created_at or script.created_at or stamp
    script.updated_at = stamp
    script.beats = []
    return script


def purge_legacy_screenplays() -> int:
    conn = get_conn()
    episodes = rows_to_dicts(conn.execute(
        "SELECT id, screenplay_json, screenplay_status FROM episodes WHERE screenplay_json IS NOT NULL AND TRIM(screenplay_json) != ''"
    ).fetchall())
    purged = 0
    for ep in episodes:
        try:
            script = EpisodeScreenplay.model_validate(json.loads(ep["screenplay_json"]))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if (script.full_script_text or "").strip():
            continue
        worker.delete_episode_shots(ep["id"])
        conn.execute(
            "UPDATE episodes SET screenplay_json=NULL, screenplay_status='pending', screenplay_error=?, status='planned', script_error=NULL WHERE id=?",
            (LEGACY_SCREENPLAY_PURGED_ERROR, ep["id"]),
        )
        purged += 1
    conn.commit()
    return purged


def _screenplay_ready(ep) -> bool:
    if not (ep["screenplay_json"] and ep["screenplay_status"] == "ready"):
        return False
    try:
        script = EpisodeScreenplay.model_validate(json.loads(ep["screenplay_json"]))
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    return bool((script.full_script_text or "").strip())


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


def _media_url(path_str: str | None) -> str | None:
    """把绝对落盘路径转成前端可取的 /media URL（带 mtime 版本号防缓存）。"""
    from app.config import PROJECTS_DIR
    if not path_str or not os.path.exists(path_str):
        return None
    rel_path = Path(path_str).relative_to(PROJECTS_DIR).as_posix()
    return f"/media/{rel_path}?v={int(os.path.getmtime(path_str))}"


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
        "SELECT scene_name, ep_start, ep_end, scene_canonical, image_path, qa_json "
        "FROM scene_references WHERE project_id=? ORDER BY scene_name, ep_start", (project_id,)).fetchall())
    by_name: dict[str, list[dict]] = {}
    for r in rows:
        qa = None
        if r["qa_json"]:
            try:
                qa = json.loads(r["qa_json"])
            except (TypeError, ValueError):
                qa = None
        by_name.setdefault(r["scene_name"], []).append({
            "ep_start": r["ep_start"], "ep_end": r["ep_end"],
            "scene_canonical": r["scene_canonical"], "image_url": _media_url(r["image_path"]),
            "qa": qa, "qa_overall": (qa or {}).get("overall") if isinstance(qa, dict) else None,
        })
    for s in bible.get("scenes", []):
        segs = by_name.get(s.get("name"), [])
        s["scene_refs"] = segs
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
        ch["preview"] = _chapter_preview(ch.pop("preview", ""))
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

async def _bible_task(project_id: str, feedback: str = "", *, trigger_full_refs: bool = True):
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
        if trigger_full_refs:
            _start_refs_generation(project_id, None)
            # 场景圣经 + 场景图素材库（与定妆照并行）：跨集场景一致性的底稿。增强项，整段失败都不能影响人物谱主流程。
            try:
                conn.execute("UPDATE projects SET scene_refs_status='running', scene_refs_error=NULL WHERE id=?",
                             (project_id,))
                conn.commit()
                asyncio.create_task(_scene_bible_and_refs(project_id))
            except Exception:  # noqa: BLE001 场景库是增强项，触发失败不影响人物谱定稿
                pass
    except asyncio.TimeoutError:
        conn.execute(
            "UPDATE projects SET bible_status='failed', bible_error=? WHERE id=?",
            (f"人物谱解析/修复超时（超过 {timeout_s} 秒），请重新谱写。", project_id),
        )
        conn.commit()
    except asyncio.CancelledError:
        row = conn.execute("SELECT bible_status FROM projects WHERE id=?", (project_id,)).fetchone()
        if row and row["bible_status"] == "running":
            conn.execute(
                "UPDATE projects SET bible_status='failed', bible_error=? WHERE id=?",
                (BIBLE_INTERRUPTED_ERROR, project_id),
            )
            conn.commit()
        raise
    except (StageError, Exception) as exc:  # noqa: BLE001
        public = errors.record_and_format(exc, action="bible_generate", context={"project_id": project_id})
        conn.execute("UPDATE projects SET bible_status='failed', bible_error=? WHERE id=?", (public, project_id))
        conn.commit()


@router.post("/projects/{project_id}/bible")
async def start_bible(project_id: str, body: dict | None = Body(None)):
    p = _project_or_404(project_id)
    if p["bible_status"] == "running" and _bible_task_active(project_id):
        raise HTTPException(409, "角色圣经正在生成中")
    if p["refs_status"] == "running":
        raise HTTPException(409, "定妆照正在生成中，请先停止后再重生人物谱")
    feedback = str((body or {}).get("feedback") or "").strip()
    if len(feedback) > 2000:
        raise HTTPException(400, "打回要求过长，请控制在 2000 字以内")
    conn = get_conn()
    # 持久化 feedback：进程重启后 recover_bible_tasks 能用相同入参续跑，而非中断报错
    conn.execute("UPDATE projects SET bible_status='running', bible_error=NULL, bible_feedback=? WHERE id=?",
                 (feedback, project_id))
    conn.commit()
    _track_bible_task(project_id, asyncio.create_task(_bible_task(project_id, feedback, trigger_full_refs=True)))
    return {"status": "running"}


@router.post("/projects/{project_id}/bible/cancel")
def cancel_bible(project_id: str):
    """停止人物谱生成。若人物谱尚未完成，停止后不会继续触发后续定妆照任务。"""
    p = _project_or_404(project_id)
    task = _bible_tasks.pop(project_id, None)
    if task and not task.done():
        task.cancel()
    conn = get_conn()
    conn.execute(
        "UPDATE projects SET bible_status='idle', bible_error=NULL, bible_feedback=NULL WHERE id=?",
        (project_id,),
    )
    conn.commit()
    was_running = p["bible_status"] == "running"
    return {"stopped": bool(task) or was_running}


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
    # 画风变更 → 旧画风场景图同样是强画风信号，连带作废（落盘文件 + 分段表），并清空 bible.scenes 的图路径。
    scene_refs_cleared = 0
    for sc in getattr(instance, "scenes", None) or []:
        if sc.ref_image_path:
            try:
                Path(sc.ref_image_path).unlink()
            except OSError:
                pass
            sc.ref_image_path = None
            scene_refs_cleared += 1
    conn = get_conn()
    # 画风变更 → 旧画风的分段定妆照全部作废，重新定妆后由分镜阶段按集反应式重建分段。
    conn.execute("DELETE FROM character_portraits WHERE project_id=?", (project_id,))
    conn.execute("DELETE FROM scene_references WHERE project_id=?", (project_id,))
    conn.execute("UPDATE projects SET refs_status='idle', scene_refs_status='idle' WHERE id=?", (project_id,))
    conn.commit()
    return {**purged, "refs_cleared": refs_cleared, "scene_refs_cleared": scene_refs_cleared}


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
# 注：初始定妆在此生成（generate_refs，适用集 1~ 至今）；已有角色的外观漂移重绘已改为分镜阶段
# 按集反应式处理（见 portraits.ensure_cards_for_screenplay），不再有"每 20 集全量轮询"步骤。


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
        public = errors.record_and_format(exc, action="refs_generate", context={"project_id": project_id})
        conn.execute("UPDATE projects SET refs_status='failed', refs_error=? WHERE id=?",
                     (public, project_id))
        conn.commit()


@router.post("/projects/{project_id}/refs")
async def start_refs(project_id: str, body: dict | None = None):
    p = _project_or_404(project_id)
    if not p["bible_json"]:
        raise HTTPException(409, "请先生成角色圣经")
    if _refs_task_active(project_id) or p["refs_status"] == "running":
        raise HTTPException(409, "定妆照正在生成中")
    only = (body or {}).get("character")
    _start_refs_generation(project_id, only)
    return {"status": "running"}


@router.post("/projects/{project_id}/refs/cancel")
def cancel_refs(project_id: str):
    """停止定妆照生成。已落盘的定妆照保留，状态置回空闲。"""
    p = _project_or_404(project_id)
    task = _refs_tasks.pop(project_id, None)
    if task and not task.done():
        task.cancel()
    conn = get_conn()
    conn.execute(
        "UPDATE projects SET refs_status='idle', refs_error=NULL, refs_target=NULL WHERE id=?", (project_id,))
    conn.commit()
    was_running = p["refs_status"] == "running"
    return {"stopped": bool(task) or was_running}


# ---------- 场景图素材库（跨集场景一致性） ----------
# 注：初始批量出图在此（scenes.generate_scene_refs，适用集 1~ 至今）；库外新场景的反应式发现
# 已挂在分镜阶段（见 scenes.ensure_scenes_for_storyboard），不在此轮询。


async def _scene_refs_task(project_id: str, only_scene: str | None):
    from app.scenes import generate_scene_refs
    conn = get_conn()
    try:
        await generate_scene_refs(project_id, only_scene)
        conn.execute("UPDATE projects SET scene_refs_status='ready', scene_refs_error=NULL WHERE id=?", (project_id,))
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        public = errors.record_and_format(exc, action="scene_refs_generate", context={"project_id": project_id})
        conn.execute("UPDATE projects SET scene_refs_status='failed', scene_refs_error=? WHERE id=?",
                     (public, project_id))
        conn.commit()


@router.post("/projects/{project_id}/scene-bible")
async def start_scene_bible(project_id: str):
    """（重新）生成场景圣经并触发场景图批量出图。人物谱必须先就绪。"""
    p = _project_or_404(project_id)
    if not p["bible_json"]:
        raise HTTPException(409, "请先生成角色圣经")
    if _scene_refs_task_active(project_id) or p["scene_refs_status"] == "running":
        raise HTTPException(409, "场景图正在生成中")
    conn = get_conn()
    conn.execute("UPDATE projects SET scene_refs_status='running', scene_refs_error=NULL WHERE id=?", (project_id,))
    conn.commit()
    asyncio.create_task(_scene_bible_and_refs(project_id))
    return {"status": "running"}


@router.post("/projects/{project_id}/scene-refs")
async def start_scene_refs(project_id: str, body: dict | None = None):
    """（重新）生成场景图。需先有场景圣经（bible.scenes 非空）。可带 only 单场景重做。"""
    p = _project_or_404(project_id)
    if not p["bible_json"] or not json.loads(p["bible_json"]).get("scenes"):
        raise HTTPException(409, "还没有场景圣经，请先生成场景清单")
    if _scene_refs_task_active(project_id) or p["scene_refs_status"] == "running":
        raise HTTPException(409, "场景图正在生成中")
    only = (body or {}).get("scene")
    _start_scene_refs_generation(project_id, only)
    return {"status": "running"}


@router.post("/projects/{project_id}/scene-refs/cancel")
def cancel_scene_refs(project_id: str):
    """停止场景图生成。已落盘的场景图保留，状态置回空闲。"""
    p = _project_or_404(project_id)
    task = _scene_refs_tasks.pop(project_id, None)
    if task and not task.done():
        task.cancel()
    conn = get_conn()
    conn.execute(
        "UPDATE projects SET scene_refs_status='idle', scene_refs_error=NULL, scene_refs_target=NULL WHERE id=?",
        (project_id,))
    conn.commit()
    was_running = p["scene_refs_status"] == "running"
    return {"stopped": bool(task) or was_running}


@router.put("/projects/{project_id}/scenes/{scene_name}/prompt")
def edit_scene_prompt(project_id: str, scene_name: str, body: dict):
    """更新单个场景的场景图生成词。传空字符串/null 恢复为默认合成描述。"""
    p = _project_or_404(project_id)
    if not p["bible_json"]:
        raise HTTPException(409, "请先生成角色圣经")
    prompt_text = (body.get("scene_prompt") or "").strip()
    if prompt_text and not 10 <= len(prompt_text) <= 400:
        raise HTTPException(422, f"场景图描述长度 {len(prompt_text)} 字，要求 10~400 字（留空则恢复默认）")
    bible = json.loads(p["bible_json"])
    target = next((s for s in bible.get("scenes", []) if s.get("name") == scene_name), None)
    if target is None:
        raise HTTPException(404, f"场景不存在：{scene_name}")
    target["scene_prompt_override"] = prompt_text or None
    conn = get_conn()
    conn.execute("UPDATE projects SET bible_json=? WHERE id=?",
                 (json.dumps(bible, ensure_ascii=False), project_id))
    conn.commit()
    return {"saved": True, "reset_to_default": not prompt_text}


# ---------- 剧集规划 ----------

PLAN_PREVIEW_CHARS = 100  # 分集台每集预览取源章前 100 字


def _chapter_preview(content: str | None, limit: int = PLAN_PREVIEW_CHARS) -> str:
    """章节正文前 limit 字（归一空白）作为分集预览。"""
    return re.sub(r"\s+", " ", (content or "")).strip()[:limit]


async def _plan_task(project_id: str):
    """正则分集（不依赖模型）：每章直接生成一集，源章 N–N，预览取该章前 100 字。"""
    conn = get_conn()
    try:
        chapters = rows_to_dicts(conn.execute(
            "SELECT * FROM chapters WHERE project_id=? ORDER BY idx", (project_id,)).fetchall())
        if not chapters:
            raise StageError("剧集规划", ["没有可分集的章节，请先上传小说"])
        # 干净替换：清空本项目所有旧剧集及衍生数据，避免重复集/撞号
        worker.delete_project_episodes(project_id)
        for episode_no, ch in enumerate(chapters, start=1):
            conn.execute(
                "INSERT INTO episodes(id, project_id, episode_no, title, hook, cliffhanger, synopsis, source_chapters, target_duration_s, status, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?, 'planned', ?)",
                (new_id("ep"), project_id, episode_no, ch["title"] or f"第{ch['idx']}章", "", "",
                 _chapter_preview(ch["content"]), json.dumps([ch["idx"]]),
                 config.EPISODE_TARGET_DEFAULT_S, now()))
        conn.execute("UPDATE projects SET plan_status='ready', plan_error=NULL, key_timeline='[]', status='planned' WHERE id=?",
                     (project_id,))
        conn.commit()
    except (StageError, Exception) as exc:  # noqa: BLE001
        public = errors.record_and_format(exc, action="plan_generate", context={"project_id": project_id})
        conn.execute("UPDATE projects SET plan_status='failed', plan_error=? WHERE id=?", (public, project_id))
        conn.commit()


@router.post("/projects/{project_id}/plan")
async def start_plan(project_id: str):
    # 正则分集不再依赖角色圣经；上传小说切分出章节后即可分集。
    p = _project_or_404(project_id)
    if p["plan_status"] == "running":
        raise HTTPException(409, "剧集规划正在生成中")
    conn = get_conn()
    conn.execute("UPDATE projects SET plan_status='running', plan_error=NULL WHERE id=?", (project_id,))
    conn.commit()
    asyncio.create_task(_plan_task(project_id))
    return {"status": "running"}


# ---------- 可拍剧本（分集之后、分镜之前） ----------

_screenplay_tasks: dict[str, asyncio.Task] = {}


def _screenplay_task_active(episode_id: str) -> bool:
    task = _screenplay_tasks.get(episode_id)
    return bool(task and not task.done())


def recover_screenplay_tasks() -> None:
    """服务热更/重启后续跑状态为 running 的剧本任务，避免 UI 卡在生成中却没有真实调用。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id FROM episodes WHERE screenplay_status='running'"
    ).fetchall()
    for row in rows:
        episode_id = row["id"]
        if _screenplay_task_active(episode_id):
            continue
        stamp = now()
        conn.execute(
            "UPDATE episodes SET screenplay_started_at=COALESCE(screenplay_started_at, ?), screenplay_updated_at=? WHERE id=?",
            (stamp, stamp, episode_id))
        task = asyncio.get_running_loop().create_task(_screenplay_task(episode_id))
        _screenplay_tasks[episode_id] = task
        task.add_done_callback(lambda _t, eid=episode_id: _screenplay_tasks.pop(eid, None))
    conn.commit()


async def _screenplay_task(episode_id: str):
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    try:
        ep_data = dict(ep)
        p = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
        bible = _project_bible_or_placeholder(p)
        source_text = _episode_source_text(conn, ep)
        compact_target = _storyboard_target_for_source(ep_data.get("target_duration_s"), len(source_text))
        if compact_target != ep_data.get("target_duration_s"):
            conn.execute("UPDATE episodes SET target_duration_s=? WHERE id=?", (compact_target, episode_id))
            conn.commit()
            ep_data["target_duration_s"] = compact_target
        prev = conn.execute(
            "SELECT cliffhanger FROM episodes WHERE project_id=? AND episode_no=?",
            (ep["project_id"], ep["episode_no"] - 1)).fetchone()
        script = await generate_screenplay(ep_data, source_text, bible,
                                           prev_ending=prev["cliffhanger"] if prev else "")
        old_script = _load_screenplay(ep)
        script = _prepare_screenplay_for_storage(
            ep, script,
            keep_existing_id=(old_script.id if old_script else None),
            keep_created_at=(old_script.created_at if old_script else None),
        )
        # 新剧本会让旧分镜/视频失效；保存前清空下游，确保后续必须重新展开。
        worker.delete_episode_shots(episode_id)
        residual = list(getattr(script, "residual_errors", []) or [])
        note = None
        if residual:
            note = "已采用最后一次输出（修复次数用完，以下剧本问题未完全修复，可手动修改或重生）：" + "；".join(residual)
        conn.execute(
            "UPDATE episodes SET screenplay_json=?, screenplay_status='ready', screenplay_error=?, screenplay_updated_at=?, status='planned', script_error=NULL WHERE id=?",
            (script.model_dump_json(), (note or "")[:800] or None, now(), episode_id))
        conn.commit()
    except asyncio.CancelledError:
        conn.execute(
            "UPDATE episodes SET screenplay_status='failed', screenplay_error=?, screenplay_updated_at=? WHERE id=?",
            ("剧本生成已取消，可重新发起。", now(), episode_id))
        conn.commit()
        raise
    except (StageError, Exception) as exc:  # noqa: BLE001
        public = errors.record_and_format(exc, action="screenplay_generate", context={"episode_id": episode_id})
        conn.execute(
            "UPDATE episodes SET screenplay_status='failed', screenplay_error=?, screenplay_updated_at=? WHERE id=?",
            (public, now(), episode_id))
        conn.commit()


@router.post("/episodes/{episode_id}/screenplay")
async def start_screenplay(episode_id: str, body: dict | None = Body(None)):
    ep = _episode_or_404(episode_id)
    if ep["status"] == "scripting":
        raise HTTPException(409, "分镜正在生成中，不能同时重写剧本")
    if ep["screenplay_status"] == "running" and _screenplay_task_active(episode_id):
        raise HTTPException(409, "剧本正在生成中")
    force = bool((body or {}).get("force"))
    conn = get_conn()
    has_shots = conn.execute("SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,)).fetchone()["c"] > 0
    if has_shots and not force:
        raise HTTPException(409, "重新生成剧本会清空本集现有分镜、关键帧、视频和成片，请确认后重试")
    started_at = now()
    conn.execute(
        "UPDATE episodes SET screenplay_status='running', screenplay_error=NULL, screenplay_started_at=?, screenplay_updated_at=? WHERE id=?",
        (started_at, started_at, episode_id))
    conn.commit()
    task = asyncio.create_task(_screenplay_task(episode_id))
    _screenplay_tasks[episode_id] = task
    task.add_done_callback(lambda t, eid=episode_id: _screenplay_tasks.pop(eid, None))
    return {"status": "running"}


async def _screenplay_guarded(episode_id: str, sem: asyncio.Semaphore):
    async with sem:
        await _screenplay_task(episode_id)


@router.post("/projects/{project_id}/screenplay-all")
async def start_screenplay_all(project_id: str):
    _project_or_404(project_id)
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, screenplay_status, screenplay_json FROM episodes WHERE project_id=? ORDER BY episode_no",
        (project_id,)).fetchall()
    ids = [
        r["id"] for r in rows
        if not r["screenplay_json"]
        or r["screenplay_status"] in ("pending", "failed")
        or (r["screenplay_status"] == "running" and r["id"] not in _screenplay_tasks)
    ]
    if not ids:
        raise HTTPException(409, "没有待生成剧本的剧集")
    placeholders = ",".join("?" for _ in ids)
    started_at = now()
    conn.execute(
        f"UPDATE episodes SET screenplay_status='running', screenplay_error=NULL, screenplay_started_at=?, screenplay_updated_at=? WHERE id IN ({placeholders})",
        [started_at, started_at, *ids])
    conn.commit()
    sem = asyncio.Semaphore(max(int(get_setting("storyboard_concurrency") or 2), 1))
    for eid in ids:
        task = asyncio.create_task(_screenplay_guarded(eid, sem))
        _screenplay_tasks[eid] = task
        task.add_done_callback(lambda t, e=eid: _screenplay_tasks.pop(e, None))
    return {"started": len(ids)}


@router.post("/projects/{project_id}/screenplay-all/cancel")
def cancel_screenplay_all(project_id: str):
    """停止本项目所有正在进行的剧本生成：取消在跑任务，未开跑的回退状态。"""
    _project_or_404(project_id)
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, screenplay_json FROM episodes WHERE project_id=? AND screenplay_status='running'",
        (project_id,)).fetchall()
    stopped = 0
    for r in rows:
        eid = r["id"]
        task = _screenplay_tasks.pop(eid, None)
        if task and not task.done():
            task.cancel()
        fallback = "ready" if r["screenplay_json"] else "pending"
        conn.execute(
            "UPDATE episodes SET screenplay_status=?, screenplay_error=NULL, screenplay_updated_at=? WHERE id=?",
            (fallback, now(), eid))
        stopped += 1
    conn.commit()
    return {"stopped": stopped}


@router.post("/episodes/{episode_id}/screenplay/cancel")
def cancel_screenplay(episode_id: str):
    ep = _episode_or_404(episode_id)
    if ep["screenplay_status"] != "running":
        raise HTTPException(409, "当前没有正在进行的剧本生成")
    task = _screenplay_tasks.pop(episode_id, None)
    if task and not task.done():
        task.cancel()
    conn = get_conn()
    fallback = "ready" if ep["screenplay_json"] else "pending"
    conn.execute(
        "UPDATE episodes SET screenplay_status=?, screenplay_error=NULL, screenplay_updated_at=? WHERE id=?",
        (fallback, now(), episode_id))
    conn.commit()
    return {"status": fallback}


@router.put("/episodes/{episode_id}/screenplay")
def edit_screenplay(episode_id: str, body: dict):
    ep = _episode_or_404(episode_id)
    payload = body.get("screenplay", body)
    force = bool(body.get("force"))
    instance, errors = schema_errors(EpisodeScreenplay, payload)
    if errors:
        raise HTTPException(422, "；".join(errors))
    conn = get_conn()
    p = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    bible = _project_bible_or_placeholder(p)
    expected = max(1, int(ep["target_duration_s"]) // config.FIXED_VIDEO_DURATION_S)
    errors = validate_screenplay(instance, bible, expected, episode_no=ep["episode_no"])
    if errors:
        raise HTTPException(422, "；".join(errors))
    old_script = _load_screenplay(ep)
    instance = _prepare_screenplay_for_storage(
        ep, instance,
        keep_existing_id=(old_script.id if old_script else None),
        keep_created_at=(old_script.created_at if old_script else None),
    )
    has_shots = conn.execute("SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,)).fetchone()["c"] > 0
    if has_shots and not force:
        raise HTTPException(409, "修改剧本会清空本集现有分镜、关键帧、视频和成片，请确认后重试")
    if has_shots:
        worker.delete_episode_shots(episode_id)
    conn.execute(
        "UPDATE episodes SET screenplay_json=?, screenplay_status='ready', screenplay_error=NULL, status='planned', script_error=NULL WHERE id=?",
        (instance.model_dump_json(), episode_id))
    conn.commit()
    return {"saved": True, "beats": len(instance.beats), "downstream_cleared": has_shots}


# ---------- 分镜脚本 ----------

# 正在进行的分镜生成任务，按 episode_id 跟踪，便于手动取消
_storyboard_tasks: dict[str, asyncio.Task] = {}


def _insert_storyboard_shot(conn, episode_id: str, screenplay: EpisodeScreenplay, shot: Shot) -> str:
    shot_id = new_id("shot")
    shot.action_desc = normalize_action_desc(shot.action_desc)
    conn.execute(
        "INSERT INTO shots(id, episode_id, script_id, shot_no, duration_s, shot_size, camera_move, scene_setting, scene_name, characters, action_desc, first_frame_desc, last_frame_desc, source_excerpt, narration, dialogues, transition, continuity_from_prev) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (shot_id, episode_id, screenplay.id, shot.shot_no, shot.duration_s, shot.shot_size, shot.camera_move,
         shot.scene_setting, shot.scene_name or None, json.dumps(shot.characters, ensure_ascii=False), shot.action_desc,
         shot.first_frame_desc, shot.last_frame_desc, shot.source_excerpt, shot.narration,
         json.dumps([d.model_dump() for d in shot.dialogues], ensure_ascii=False),
         shot.transition, int(shot.continuity_from_prev)))
    return shot_id


def _sync_storyboard_shot_timing(conn, episode_id: str, board: Storyboard) -> None:
    for shot in board.shots:
        conn.execute(
            "UPDATE shots SET duration_s=?, transition=?, continuity_from_prev=?, last_frame_desc=? WHERE episode_id=? AND shot_no=?",
            (shot.duration_s, shot.transition, int(shot.continuity_from_prev), shot.last_frame_desc,
             episode_id, shot.shot_no),
        )


async def _storyboard_task(episode_id: str):
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    try:
        conn.execute("UPDATE episodes SET status='scripting', script_error=NULL WHERE id=?", (episode_id,))
        conn.commit()
        ep_data = dict(ep)
        screenplay = _load_screenplay(ep)
        if screenplay is None or ep["screenplay_status"] != "ready":
            raise StageError("分镜脚本", ["请先生成并确认本集可拍剧本，再展开分镜"])
        p = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
        bible = _project_bible_or_placeholder(p)
        # 定妆照按集反应式维护（在分镜展开前）：①新角色发现并补进人物谱——否则 validate_storyboard 会因
        # "角色圣经中不存在"把新角色从分镜里刷掉；②已有角色外观漂移则图生图重绘新段并同步 bible 锚点。
        # 任一失败都不阻断分镜，按原人物谱继续。
        try:
            from app.portraits import ensure_cards_for_screenplay
            disc = await ensure_cards_for_screenplay(ep["project_id"], ep["episode_no"], screenplay, bible)
            if disc.get("added") or disc.get("redrawn"):
                p = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
                bible = _project_bible_or_placeholder(p)
        except Exception:  # noqa: BLE001 定妆照维护是增强项，失败就按原人物谱继续分镜
            pass
        # 场景图素材库按集反应式维护（分镜展开前）：剧本里出现、库里没有、够戏份的新场景 → 补入库 + 出图，
        # 使分镜能命中库内场景、validate_storyboard_scenes 通过。失败不阻断分镜（按现有库继续）。
        try:
            from app.scenes import ensure_scenes_for_storyboard
            sdisc = await ensure_scenes_for_storyboard(ep["project_id"], ep["episode_no"], screenplay, bible)
            if sdisc.get("added"):
                p = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
                bible = _project_bible_or_placeholder(p)
        except Exception:  # noqa: BLE001 场景库维护是增强项，失败就按现有场景库继续分镜
            pass
        source_text = _episode_source_text(conn, ep)
        compact_target = _storyboard_target_for_source(ep_data.get("target_duration_s"), len(source_text))
        if compact_target != ep_data.get("target_duration_s"):
            conn.execute("UPDATE episodes SET target_duration_s=? WHERE id=?", (compact_target, episode_id))
            conn.commit()
            ep_data["target_duration_s"] = compact_target
        prev = conn.execute(
            "SELECT cliffhanger FROM episodes WHERE project_id=? AND episode_no=?",
            (ep["project_id"], ep["episode_no"] - 1)).fetchone()

        worker.delete_episode_shots(episode_id)
        conn.execute("UPDATE episodes SET storyboard_outline_json=NULL WHERE id=?", (episode_id,))
        conn.commit()
        # 先出整集分镜大纲定全局节奏，再逐镜按大纲填充——避免多镜停留同一情绪、剧情推进过慢。
        # 大纲是增强项：规划失败（如模型不可用）就回退到无大纲的纯逐镜生成，不阻断分镜。
        outline = None
        try:
            outline = await generate_storyboard_outline(
                ep_data, source_text, bible,
                prev_ending=prev["cliffhanger"] if prev else "", screenplay=screenplay)
            conn.execute("UPDATE episodes SET storyboard_outline_json=? WHERE id=?",
                         (outline.model_dump_json(), episode_id))
            conn.commit()
        except Exception:  # noqa: BLE001 大纲失败不阻断，退回纯逐镜生成
            outline = None
        completed: list[Shot] = []
        final_feedback: list[str] | None = None
        _, max_shots = storyboard_shot_count_range(ep_data["target_duration_s"])
        while True:
            draft = await generate_storyboard_next_shot(
                ep_data, source_text, bible,
                prev_ending=prev["cliffhanger"] if prev else "",
                screenplay=screenplay,
                completed_shots=completed,
                final_feedback=final_feedback,
                outline=outline,
            )
            # 与单镜 QA 使用同一套确定性归一口径后再落库。
            board = Storyboard(episode_no=ep_data["episode_no"], shots=[*completed, draft.shot])
            normalize_continuity(board)
            # 与逐镜 QA 同口径：圣经外路人剥离/规范，落库前去掉非圣经角色。把这次确定性处理记入账本，
            # 让监控里能看到"本该触发一轮『角色圣经中不存在』修复、已被平台就地消化"（减重试 #1）。
            for c in normalize_offbible_characters(board, bible):
                log_provider_call(
                    "storyboard_offbible_character", config.MODEL_TEXT, "OFFBIBLE_NORMALIZED", None, 0,
                    meta={"episode_id": episode_id, "episode_no": ep_data["episode_no"], "stage": "分镜脚本", **c})
            relieve_spoken_overflow(board)  # 与逐镜 QA 同口径：人群旁白降级为画面，单镜口播压回上限内
            normalize_durations_for_speech(board)
            normalize_episode_opening_shot(board)
            compact_durations_to_budget(board, ep_data["target_duration_s"])
            normalize_transition_visuals(board)
            _sync_storyboard_shot_timing(conn, episode_id, board)
            shot = board.shots[-1]
            _insert_storyboard_shot(conn, episode_id, screenplay, shot)
            completed = list(board.shots)
            conn.execute("UPDATE episodes SET status='scripting', script_error=NULL WHERE id=?", (episode_id,))
            conn.commit()
            residual = list(getattr(draft, "residual_errors", []) or [])
            if residual:
                can_continue = (
                    bool(draft.is_final)
                    and len(completed) < max_shots
                    and len(residual) == 1
                    and "暂不能收尾" in residual[0]
                    and "继续补镜" in residual[0]
                )
                if can_continue:
                    # 这类 residual 的意思是"本镜不能当最后一镜"，不是本镜结构坏了；
                    # 保留它作为过渡镜，继续把缺失关键内容喂给后续镜头。
                    object.__setattr__(draft, "is_final", False)
                else:
                    note = (
                        f"镜{shot.shot_no:02d}已达到重试上限，已作为「需修改镜头」保留在分镜台；"
                        + _storyboard_residual_hint(residual)
                        + "。残余问题：" + "；".join(residual[:8])
                    )
                    conn.execute("UPDATE episodes SET status='scripted', script_error=? WHERE id=?",
                                 (note[:800], episode_id))
                    conn.commit()
                    break
            if draft.is_final:
                conn.execute("UPDATE episodes SET status='scripted', script_error=NULL WHERE id=?", (episode_id,))
                conn.commit()
                break
            if len(completed) >= max_shots:
                raise StageError("分镜脚本", [f"已生成 {len(completed)} 镜但模型仍未收束到尾钩，请重试或人工补写最后一镜"])
            # 把"整集必保留台词/剧情点里还没落到镜头的部分"作为下一镜的补镜反馈，
            # 让缺口在后续镜头里逐步补齐，而不是拖到收尾镜才发现、再硬塞进单镜导致卡死。
            final_feedback = validate_storyboard_preserves_key_content(
                Storyboard(episode_no=ep_data["episode_no"], shots=list(completed)), screenplay) or None
    except (StageError, Exception) as exc:  # noqa: BLE001
        rec = errors.log_error(exc, action="storyboard_generate", context={"episode_id": episode_id})
        saved = conn.execute("SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,)).fetchone()["c"]
        if saved:
            note = (
                f"追加镜生成失败，已保留前 {saved} 个 QA 通过镜头，可人工补写最后一镜、修改后确认，"
                f"或重新生成分镜。（{rec.code} · {rec.error_id}）"
            )
            conn.execute("UPDATE episodes SET status='scripted', script_error=? WHERE id=?",
                         (note[:800], episode_id))
        else:
            conn.execute("UPDATE episodes SET status='script_failed', script_error=? WHERE id=?",
                         (rec.public, episode_id))
        conn.commit()


@router.post("/episodes/{episode_id}/storyboard")
async def start_storyboard(episode_id: str):
    ep = _episode_or_404(episode_id)
    if ep["status"] == "scripting":
        raise HTTPException(409, "分镜正在生成中")
    if not _screenplay_ready(ep):
        raise HTTPException(409, "请先在剧本台生成本集可拍剧本")
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
        "SELECT id, status, screenplay_status, screenplay_json FROM episodes WHERE project_id=? AND status IN ('planned','scripting','script_failed') ORDER BY episode_no",
        (project_id,)).fetchall()
    # 待分镜的；以及卡在“分镜中”却没有在跑任务的孤儿（需重新触发）
    ids = [
        r["id"] for r in rows
        if r["screenplay_status"] == "ready" and r["screenplay_json"]
        and (r["status"] in ("planned", "script_failed") or r["id"] not in _storyboard_tasks)
    ]
    if not ids:
        raise HTTPException(409, "没有可展开分镜的剧集（需先生成剧本，且状态为待分镜/分镜失败/卡住的分镜中）")
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
    # 取消时分镜必然未走到尾钩（is_final 后状态已是 scripted、不在 scripting）。已生成的镜头是
    # 半截分镜，不能冒充"待确认"完成态——置为 script_failed 并写清原因，保留已生成镜头供查看/续作。
    if has_shots:
        conn.execute(
            "UPDATE episodes SET status='script_failed', script_error=? WHERE id=?",
            (f"分镜生成已手动取消：已生成 {has_shots} 镜，未收束到尾钩。可点击重新生成分镜。", episode_id))
        conn.commit()
        return {"status": "script_failed", "shots": has_shots}
    conn.execute("UPDATE episodes SET status='planned', script_error=NULL WHERE id=?", (episode_id,))
    conn.commit()
    return {"status": "planned"}


_plan_tasks: dict[str, asyncio.Task] = {}


async def _plan_modes_task(episode_id: str):
    """后台任务：为全集镜头写入固定参考图模式计划。"""
    conn = get_conn()
    shots = rows_to_dicts(conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)).fetchall())
    if not shots:
        return
    for shot_row in shots:
        plan_dict = await _plan_one_shot(shot_row)
        conn.execute("UPDATE shots SET mode_plan=? WHERE id=?",
                     (json.dumps(plan_dict, ensure_ascii=False), shot_row["id"]))
    conn.commit()


async def _plan_one_shot(shot_row) -> dict:
    """返回固定参考图模式计划；不再调用 LLM 做模式选择。"""
    from app import video_modes
    return video_modes.decision_to_dict(video_modes.default_reference_decision())


async def _ensure_shot_mode_plan(conn, shot_id: str, *, force: bool = False) -> None:
    """生成前确保该镜已有固定参考图模式计划。已存在且非强制时跳过。"""
    shot_row = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot_row:
        return
    if not force and shot_row["mode_plan"]:
        return
    plan_dict = await _plan_one_shot(shot_row)
    conn.execute("UPDATE shots SET mode_plan=? WHERE id=?",
                 (json.dumps(plan_dict, ensure_ascii=False), shot_id))
    conn.commit()


@router.post("/episodes/{episode_id}/plan-modes")
async def start_plan_modes(episode_id: str):
    """为全集镜头写入固定参考图模式计划（兼容旧入口，不调用 LLM）。"""
    _episode_or_404(episode_id)
    if episode_id in _plan_tasks and not _plan_tasks[episode_id].done():
        raise HTTPException(409, "参考图计划写入中")
    conn = get_conn()
    conn.execute("UPDATE episodes SET status='confirming' WHERE id=? AND status='scripted'", (episode_id,))
    conn.commit()
    task = asyncio.create_task(_plan_modes_task(episode_id))
    _plan_tasks[episode_id] = task
    task.add_done_callback(lambda t, eid=episode_id: _plan_tasks.pop(eid, None))
    return {"status": "planning"}


@router.get("/episodes/{episode_id}/plan-modes/status")
def plan_modes_status(episode_id: str):
    _episode_or_404(episode_id)
    task = _plan_tasks.get(episode_id)
    if task and not task.done():
        return {"status": "planning"}
    conn = get_conn()
    ep = conn.execute("SELECT status FROM episodes WHERE id=?", (episode_id,)).fetchone()
    total = conn.execute("SELECT COUNT(*) as c FROM shots WHERE episode_id=?", (episode_id,)).fetchone()["c"]
    planned = conn.execute(
        "SELECT COUNT(*) as c FROM shots WHERE episode_id=? AND mode_plan IS NOT NULL",
        (episode_id,)).fetchone()["c"]
    return {"status": ep["status"], "total": total, "planned": planned}


@router.get("/episodes/{episode_id}")
def episode_detail(episode_id: str):
    ep = dict(_episode_or_404(episode_id))
    conn = get_conn()
    ep["source_chapters"] = json.loads(ep["source_chapters"] or "[]")
    script = _load_screenplay(ep)
    ep["screenplay"] = script.model_dump() if script else None
    ep["screenplay_mode"] = _screenplay_mode(script)
    ep.pop("screenplay_json", None)
    # 分镜大纲（先规划后逐镜填充）：透出给前端做 已通过 k / 计划 N 镜 的进度展示
    try:
        outline = json.loads(ep.get("storyboard_outline_json") or "null")
    except (TypeError, ValueError):
        outline = None
    ep.pop("storyboard_outline_json", None)
    ep["storyboard_outline"] = outline
    ep["storyboard_planned_shots"] = len(outline["shots"]) if outline and outline.get("shots") else None
    shot_rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)).fetchall()
    # 上限须带 board 计算：台词密集集的口播保底会把真实生效上限抬到 90s 以上；
    # 不带 board 会少算成固定 90s，与确认/压缩时的实际判断口径不一致，让用户误以为"超了却压不动"。
    board_for_limit = _board_from_shot_rows(shot_rows, ep["episode_no"]) if shot_rows else None
    ep["storyboard_duration_limit_s"] = storyboard_duration_limit(ep["target_duration_s"], board_for_limit)
    ep["cost_cny"] = worker.episode_cost(episode_id)
    ep["cost_limit_cny"] = float(get_setting("episode_cost_limit_cny") or 100)
    shots = rows_to_dicts(shot_rows)
    from app.config import PROJECTS_DIR
    shots_by_no = {s["shot_no"]: s for s in shots}
    for s in shots:
        s["characters"] = json.loads(s["characters"] or "[]")
        s["dialogues"] = json.loads(s["dialogues"] or "[]")
        s["est_cost_cny"] = shot_cost_cny(s["duration_s"])
        s["required_keyframes"] = worker.required_keyframe_kinds(s)
        # mode_plan 存的是 JSON 文本，解析成对象供前端只读展示模型决策
        try:
            s["mode_plan"] = json.loads(s["mode_plan"]) if s.get("mode_plan") else None
        except (TypeError, ValueError):
            s["mode_plan"] = None
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
            if meta.get("mode") != "REFERENCE_IMAGE_MODE":
                pred = shots_by_no.get(s["shot_no"] - 1)
                expected_first = pred.get("approved_tail_scene_id") if pred else None
                expected_last = s.get("approved_tail_scene_id")
                if (expected_first and meta.get("first_frame_scene_id") != expected_first) or (
                        expected_last and meta.get("last_frame_scene_id") != expected_last):
                    s["video_stale"] = True
        elif s["adopted_version_id"]:
            av = conn.execute("SELECT image_inputs FROM shot_versions WHERE id=?", (s["adopted_version_id"],)).fetchone()
            meta = json.loads((av["image_inputs"] if av else None) or "{}")
            if meta.get("mode") != "REFERENCE_IMAGE_MODE":
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
            refs = []
            for ref in meta.get("reference_images") or []:
                item = dict(ref)
                if item.get("path"):
                    try:
                        item["image_url"] = f"/media/{Path(item['path']).relative_to(PROJECTS_DIR).as_posix()}"
                    except ValueError:
                        item["image_url"] = None
                refs.append(item)
            v["image_inputs"] = {"first_frame_used": bool(meta.get("first_frame_used")),
                                 "first_frame_src": meta.get("first_frame_src"),
                                 "first_frame_scene_id": meta.get("first_frame_scene_id"),
                                 "first_frame_image_url": _media_url(meta.get("first_frame_path")),
                                 "last_frame_used": bool(meta.get("last_frame_used")),
                                 "last_frame_src": meta.get("last_frame_src"),
                                 "last_frame_scene_id": meta.get("last_frame_scene_id"),
                                 "last_frame_image_url": _media_url(meta.get("last_frame_path")),
                                 "mode": meta.get("mode"),
                                 "mode_decision": meta.get("mode_decision"),
                                 "reference_image_used": bool(meta.get("reference_image_used")),
                                 "reference_images": refs,
                                 "reference_failure_logs": meta.get("reference_failure_logs") or [],
                                 "fallback_reason": meta.get("fallback_reason"),
                                 "retry_reason": meta.get("retry_reason")}
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
    # 时长 clamp 到产品侧合法区间；缺省/非法时回退默认时长。
    merged["duration_s"] = clip_duration_value(merged.get("duration_s"))
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
    # 剧本改了 → 旧的模型决策作废，下次生成时按新剧本重新规划
    conn.execute("UPDATE shots SET mode_plan=NULL WHERE id=?", (shot_id,))
    # 编辑后剧集回到 scripted（需重新确认才能生成）
    conn.execute("UPDATE episodes SET status='scripted' WHERE id=? AND status='confirmed'", (shot["episode_id"],))
    conn.commit()
    return {"ok": True}


def _storyboard_residual_hint(residual: list[str]) -> str:
    """方案 D：按残余错误类型给可操作的修复建议，不再一律推「自动压缩时长」。

    自动压缩时长只压缩 duration_s，解决不了角色圣经缺人、口播字数超限、covers 未落实——
    但旧提示一律推「自动压缩时长」，对用户形成误导。这里按错误关键词分流到对应修复路径。
    """
    text = "；".join(residual)
    hints: list[str] = []
    if "口播上限" in text or "念不完" in text:
        hints.append("请拆成相邻镜头分担台词，或精简人群议论旁白")
    if "角色圣经中不存在" in text or "圣经角色为" in text:
        hints.append("请在监制房把该角色补入角色圣经，或改由圣经角色完成该动作")
    if "未落实本镜大纲 covers" in text or "只停留在大纲" in text:
        hints.append("请在 action_desc/narration/dialogues 写出该事实，同义改写即可（如\"成绩\"可写成\"测出七段\"、\"追捧\"可写成\"赞叹欢呼\"）")
    if "总时长" in text and "超出上限" in text:
        hints.append("可点击「自动压缩时长」压缩冗余长镜")
    if not hints:
        hints.append("请修改该镜后重试，或点击「重新生成分镜」")
    return "；".join(hints)


def _board_from_shot_rows(rows, episode_no: int) -> Storyboard:
    """把 shots 表行还原成 Storyboard（确认门 / 压缩时长 / 时间 agent 共用，避免构造逻辑分叉）。"""
    shots = [Shot(
        shot_no=r["shot_no"], duration_s=r["duration_s"], shot_size=r["shot_size"], camera_move=r["camera_move"],
        scene_setting=r["scene_setting"], characters=json.loads(r["characters"] or "[]"),
        action_desc=r["action_desc"], first_frame_desc=r["first_frame_desc"] or "", last_frame_desc=r["last_frame_desc"] or "",
        source_excerpt=r["source_excerpt"] or "",
        narration=r["narration"], dialogues=json.loads(r["dialogues"] or "[]"),
        transition=r["transition"] or "硬切", continuity_from_prev=bool(r["continuity_from_prev"])) for r in rows]
    return Storyboard(episode_no=episode_no, shots=shots)


async def _time_agent_rebalance_durations(episode_id: str) -> dict | None:
    """确认分镜前的内置「时间 agent」：把口播保底归一后仍超上限（默认 90s）的整集，
    交给 LLM 依据各镜内容挑选可压缩的镜头削减时长——单镜削减不超过 20% 且不低于口播/开场保底，
    使总时长回到上限内。LLM 失败/越界都有确定性兜底，绝不阻断确认。

    返回调整摘要 dict；无需调整（未超上限）时返回 None。在 /confirm 与一键全自动确认前调用。"""
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        return None
    rows = conn.execute("SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)).fetchall()
    if not rows:
        return None
    board = _board_from_shot_rows(rows, ep["episode_no"])
    # 与确认门同口径：先把每镜抬到能念完台词 / 开场建场的保底时长，再据此判断是否超上限。
    normalize_durations_for_speech(board)
    normalize_episode_opening_shot(board)
    limit = storyboard_duration_limit(ep["target_duration_s"], board)
    total_before = sum(int(s.duration_s or 0) for s in board.shots)
    if total_before <= limit:
        return None  # 未超上限：时间 agent 不介入

    floors = {s.shot_no: enforced_min_duration(board, s) for s in board.shots}
    originals = {s.shot_no: int(s.duration_s or 0) for s in board.shots}
    # 单镜最多压缩 20%（向上取整保留更长），且不得低于口播/开场保底。
    caps = {no: max(floors[no], math.ceil(originals[no] * 0.8)) for no in originals}

    try:
        plan = await time_agent_compress_durations(board, limit, caps)
    except Exception:  # noqa: BLE001 时间 agent 不可用 → 纯确定性兜底，不阻断确认
        plan = {}
    for s in board.shots:
        proposed = plan.get(s.shot_no)
        if proposed is not None:  # 模型给的值一律 clamp 回 [20% 帽, 原时长]，绝不盲信
            s.duration_s = max(caps[s.shot_no], min(originals[s.shot_no], int(proposed)))

    # 兜底①：在 20% 帽内继续削冗余最多的镜，凑到上限内（模型可能没削够）。
    within_cap = compress_durations_within_floors(board, limit, caps)
    # 兜底②：20% 帽都压不到上限（极端长台词/镜头过多）→ 退到口播保底硬压缩，保证 ≤ 上限。
    if not within_cap:
        compress_durations_within_floors(board, limit, floors)

    changes: list[dict] = []
    for r, s in zip(rows, board.shots):
        if r["duration_s"] != s.duration_s:
            conn.execute("UPDATE shots SET duration_s=? WHERE id=?", (s.duration_s, r["id"]))
        # 报告口径与 total_before 一致：相对「口播保底归一后」的基线计削减，不混入归一本身的抬升。
        if originals[s.shot_no] != s.duration_s:
            changes.append({"shot_no": s.shot_no, "before": originals[s.shot_no], "after": s.duration_s})
    conn.commit()
    return {
        "rebalanced": True,
        "used_time_agent": bool(plan),
        "within_20pct_cap": within_cap,
        "total_before": total_before,
        "total_after": sum(int(s.duration_s or 0) for s in board.shots),
        "limit": limit,
        "changes": changes,
    }


@router.post("/episodes/{episode_id}/rebalance-durations")
def rebalance_episode_durations(episode_id: str):
    ep = _episode_or_404(episode_id)
    if ep["status"] in ("generating", "done"):
        raise HTTPException(409, "视频生成中或已成片，不能自动压缩分镜时长")
    conn = get_conn()
    rows = conn.execute("SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)).fetchall()
    if not rows:
        raise HTTPException(409, "本集还没有可压缩的分镜")
    board = _board_from_shot_rows(rows, ep["episode_no"])
    normalize_continuity(board)
    normalize_durations_for_speech(board)
    normalize_episode_opening_shot(board)
    result = compact_durations_to_budget(board, ep["target_duration_s"], desired_total_s=ep["target_duration_s"])
    normalize_transition_visuals(board)
    for row, shot in zip(rows, board.shots):
        if (row["duration_s"] != shot.duration_s or row["transition"] != shot.transition
                or bool(row["continuity_from_prev"]) != shot.continuity_from_prev
                or (row["last_frame_desc"] or "") != shot.last_frame_desc):
            conn.execute(
                "UPDATE shots SET duration_s=?, transition=?, continuity_from_prev=?, last_frame_desc=? WHERE id=?",
                (shot.duration_s, shot.transition, int(shot.continuity_from_prev), shot.last_frame_desc, row["id"]))
    conn.execute("UPDATE episodes SET status='scripted' WHERE id=? AND status='confirmed'", (episode_id,))
    conn.commit()
    return result


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
    has_real_bible = bool((p["bible_json"] or "").strip())
    bible = _project_bible_or_placeholder(p)
    shots_rows = conn.execute("SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)).fetchall()
    if not shots_rows:
        raise ValueError("本集还没有分镜脚本")
    board = _board_from_shot_rows(shots_rows, ep["episode_no"])
    shots = board.shots
    # 确认门同样跑确定性连贯归一，并把修正后的 continuity/transition 写回库，
    # 保证人工编辑过的分镜在进入生成前也满足"同场景接上镜/换场明确转场"的铁律。
    before = [(s.continuity_from_prev, s.transition, s.duration_s, s.shot_size, s.camera_move) for s in shots]
    normalize_continuity(board)
    # 确认门同样把每镜时长抬到能念完台词，并写回库，保证人工编辑/旧分镜进入生成前也满足音画同步。
    normalize_durations_for_speech(board)
    # 第一集第一镜=全片开场建场镜：拉长时长 + 强制远景建场 + 缓慢推近运镜，并写回库。
    normalize_episode_opening_shot(board)
    normalize_transition_visuals(board)
    for r, s, (old_cont, old_trans, old_dur, old_size, old_move) in zip(shots_rows, shots, before):
        if (old_cont != s.continuity_from_prev or old_trans != s.transition or old_dur != s.duration_s
                or old_size != s.shot_size or old_move != s.camera_move
                or (r["last_frame_desc"] or "") != s.last_frame_desc):
            conn.execute(
                "UPDATE shots SET continuity_from_prev=?, transition=?, duration_s=?, shot_size=?, camera_move=?, last_frame_desc=? WHERE id=?",
                (int(s.continuity_from_prev), s.transition, s.duration_s, s.shot_size, s.camera_move,
                 s.last_frame_desc, r["id"]))
    conn.commit()
    errors = validate_storyboard(board, bible, compact_target, enforce_total_duration=False)
    screenplay = _load_screenplay(ep)
    if screenplay is not None:
        errors.extend(validate_storyboard_soundtrack(board, screenplay, compact_target))
        errors.extend(validate_storyboard_preserves_key_content(board, screenplay))
    if errors:
        raise ValueError(json.dumps(errors, ensure_ascii=False))
    # 预编译全部 prompt，把参数错误拦在花钱之前
    if has_real_bible:
        try:
            for s in shots:
                compile_prompt(s, bible)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"Prompt 编译失败：{exc}")
    est = sum(shot_cost_cny(s.duration_s) for s in shots)
    conn.execute("UPDATE episodes SET status='confirmed' WHERE id=?", (episode_id,))
    conn.commit()
    return {
        "confirmed": True,
        "estimated_cost_cny": round(est, 2),
        "shot_count": len(shots),
        "total_duration_s": sum(s.duration_s for s in shots),
        "target_duration_s": compact_target,
    }


@router.post("/episodes/{episode_id}/confirm")
async def confirm_episode(episode_id: str):
    """人工确认门（PRD P3）：全量业务校验通过才进入 confirmed。

    确认前先跑内置「时间 agent」：整集总时长超上限（默认 90s）时按内容压缩部分镜头时长（单镜不超 20%），
    把总时长拉回上限内，再进入确定性校验与确认。"""
    _episode_or_404(episode_id)
    try:
        rebalance = await _time_agent_rebalance_durations(episode_id)
        result = confirm_episode_core(episode_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if rebalance:
        result["time_agent"] = rebalance
    return result


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


@router.post("/episodes/{episode_id}/clear-artifacts")
def clear_episode_artifacts(episode_id: str):
    """清空整集所有镜头的参考图、视频与模型分析（mode_plan），保留关键帧，并回退到「已确认」。"""
    _episode_or_404(episode_id)
    return worker.clear_episode_artifacts(episode_id)


@router.post("/shots/{shot_id}/clear-artifacts")
def clear_shot_artifacts(shot_id: str):
    """清空单个镜头的参考图、视频与模型分析（mode_plan），保留关键帧。"""
    conn = get_conn()
    if not conn.execute("SELECT id FROM shots WHERE id=?", (shot_id,)).fetchone():
        raise HTTPException(404, "镜头不存在")
    return worker.clear_shot_artifacts(shot_id)


@router.delete("/versions/{version_id}")
def delete_version(version_id: str):
    """删除一个已生成的视频版本（含文件）。若是采用版则清空采用、使本集成品失效。"""
    conn = get_conn()
    v = conn.execute("SELECT id FROM shot_versions WHERE id=?", (version_id,)).fetchone()
    if not v:
        raise HTTPException(404, "视频版本不存在")
    shot_id = worker.delete_video_version(version_id)
    return {"deleted": version_id, "shot_id": shot_id}


def _set_reference_image_used(version_id: str, ref_id: str, *, use: bool) -> dict:
    """素材画廊里把某张参考图标记为「废弃」或「恢复使用」。
    废弃后该图不再喂给视频模型（见 video_modes.build_seedance_image_inputs），仅留作展示。"""
    conn = get_conn()
    v = conn.execute("SELECT image_inputs FROM shot_versions WHERE id=?", (version_id,)).fetchone()
    if not v:
        raise HTTPException(404, "视频版本不存在")
    meta = json.loads(v["image_inputs"] or "{}")
    refs = meta.get("reference_images") or []
    target = next((r for r in refs if r.get("id") == ref_id), None)
    if target is None:
        raise HTTPException(404, "参考图不存在")
    changed = target.get("deleted") != (not use) or target.get("selectedForSeedance") != use
    target["deleted"] = not use
    target["selectedForSeedance"] = use
    meta["reference_images"] = refs
    if changed:
        meta["reference_gallery_revision"] = now()
        meta["reference_gallery_edited"] = True
    conn.execute("UPDATE shot_versions SET image_inputs=? WHERE id=?",
                 (json.dumps(meta, ensure_ascii=False), version_id))
    conn.commit()
    return {"version_id": version_id, "ref_id": ref_id, "deleted": not use}


@router.delete("/versions/{version_id}/reference-images/{ref_id}")
def discard_reference_image(version_id: str, ref_id: str):
    """废弃一张参考图：移入废弃画廊，且后续调用视频模型时不再使用它。"""
    return _set_reference_image_used(version_id, ref_id, use=False)


@router.post("/versions/{version_id}/reference-images/{ref_id}/restore")
def restore_reference_image(version_id: str, ref_id: str):
    """把废弃画廊里的参考图恢复为可用（重新计入喂给视频模型的参考图）。"""
    return _set_reference_image_used(version_id, ref_id, use=True)


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


# ----- 视频生成（固定参考图模式） -----

def _shot_by_no(episode_id: str, shot_no: int):
    return get_conn().execute(
        "SELECT id FROM shots WHERE episode_id=? AND shot_no=?", (episode_id, shot_no)).fetchone()


@router.post("/episodes/{episode_id}/generate")
async def generate_episode(episode_id: str, body: dict | None = None):
    """批量生成整集视频（固定参考图模式）：每个视频任务内部生成/复用参考图并提交 Seedance。
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
    # 选中镜清空旧采用版，使新生成版被自动采用。
    sel_ids = [s["id"] for s in selected]
    conn.execute(
        f"UPDATE shots SET adopted_version_id=NULL WHERE id IN ({','.join('?' for _ in sel_ids)})", sel_ids)
    conn.commit()
    # 固定参考图模式：批量生成前确保每个选中镜都有固定参考图计划。
    for s in selected:
        await _ensure_shot_mode_plan(conn, s["id"])
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
            public = errors.record_and_format(exc, action="enqueue_shot",
                                              context={"shot_id": s["id"], "episode_id": episode_id})
            results.append({"shot_id": s["id"], "error": public})
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
    # 固定参考图模式：生成前确保已有固定参考图计划。
    await _ensure_shot_mode_plan(conn, shot_id)
    # 同场景接上镜时，参考图模式可复用上一镜可用素材作为参考。
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
            option("deepseek", hiagent.active_model("text", "deepseek"), bool(config.DEEPSEEK_API_KEY)),
            option("zhipu", hiagent.active_model("text", "zhipu"), bool(config.ZHIPU_API_KEY)),
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
        "deepseek_key_configured": bool(config.DEEPSEEK_API_KEY),
        "zhipu_key_configured": bool(config.ZHIPU_API_KEY),
        "hiagent_model_text": hiagent.active_model("text", "hiagent"),
        "hiagent_model_vlm": hiagent.active_model("vlm", "hiagent"),
        "hiagent_model_video": hiagent.active_model("video", "hiagent"),
        "hiagent_model_image": hiagent.active_model("image", "hiagent"),
        "openrouter_model_text": hiagent.active_model("text", "openrouter"),
        "openrouter_model_vlm": hiagent.active_model("vlm", "openrouter"),
        "bailian_model_text": hiagent.active_model("text", "bailian"),
        "bailian_model_vlm": hiagent.active_model("vlm", "bailian"),
        "deepseek_model_text": hiagent.active_model("text", "deepseek"),
        "zhipu_model_text": hiagent.active_model("text", "zhipu"),
        "models": models,
    }


@router.get("/system/calls")
def recent_calls(limit: int = 30):
    rows = rows_to_dicts(get_conn().execute(
        "SELECT * FROM provider_calls ORDER BY id DESC LIMIT ?", (min(limit, 200),)).fetchall())
    return rows


@router.get("/system/errors")
def recent_errors(limit: int = 50):
    """最近报错码列表（不含原文/堆栈，只给概览）。凭 id 调下方详情接口查根因。"""
    rows = rows_to_dicts(get_conn().execute(
        """SELECT id, ts, category, category_label, code, is_technical, http_status, action, exc_type
           FROM error_logs ORDER BY ts DESC LIMIT ?""", (min(limit, 200),)).fetchall())
    return rows


@router.get("/system/errors/{error_id}")
def error_detail(error_id: str):
    """凭错误ID查全文：请求动作上下文 + 原始报错 + 堆栈，定位根因用。"""
    row = get_conn().execute("SELECT * FROM error_logs WHERE id=?", (error_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"错误ID不存在：{error_id}")
    return dict(row)


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
    screenplay_recent = rows_to_dicts(conn.execute(
        """SELECT 'screenplay_' || e.id AS id, 'screenplay' AS kind,
                  e.id AS episode_id, e.project_id, NULL AS shot_id,
                  CASE e.screenplay_status
                    WHEN 'running' THEN 'running'
                    WHEN 'ready' THEN 'succeeded'
                    WHEN 'failed' THEN 'failed'
                    ELSE e.screenplay_status
                  END AS status,
                  e.screenplay_error AS error, e.episode_no, e.title AS episode_title,
                  p.name AS project_name, NULL AS shot_no,
                  COALESCE(e.screenplay_updated_at, e.screenplay_started_at, e.created_at) AS updated_at
           FROM episodes e JOIN projects p ON p.id=e.project_id
           WHERE e.screenplay_started_at IS NOT NULL
           ORDER BY updated_at DESC LIMIT 40""").fetchall())
    recent = sorted([*recent, *screenplay_recent], key=lambda row: row.get("updated_at") or 0, reverse=True)[:40]
    for row in screenplay_recent:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {"counts": counts, "recent": recent}


@router.get("/settings")
def get_settings():
    rows = get_conn().execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


@router.put("/settings")
def put_settings(body: dict):
    for key, value in body.items():
        skey = str(key)
        sval = str(value).strip()
        if skey == "model_text_provider" and sval not in {"hiagent", "openrouter", "bailian", "deepseek", "zhipu"}:
            raise HTTPException(422, f"{skey} 只能是 hiagent、openrouter、bailian、deepseek 或 zhipu")
        if skey == "model_vlm_provider" and sval not in {"hiagent", "openrouter", "bailian"}:
            raise HTTPException(422, f"{skey} 只能是 hiagent、openrouter 或 bailian")
        if skey == "model_route" and sval not in {"hiagent", "openrouter"}:
            raise HTTPException(422, f"{skey} 只能是 hiagent 或 openrouter")
        if skey in {"model_video_provider", "model_image_provider"} and sval != "hiagent":
            raise HTTPException(422, "当前视频/图像生成只支持火山 HiAgent")
        set_setting(skey, sval)
        if skey == "model_route":
            set_setting("model_text_provider", sval)
            set_setting("model_vlm_provider", sval)
    return {"ok": True}


# ---------- API Key 管理：前端填写 → 持久化 .env ----------

@router.get("/keys")
def get_keys():
    """获取各 provider 的 key 状态（不返回完整 key 值）。"""
    return config.get_key_status()


@router.put("/keys")
def put_keys(body: dict):
    """保存 API Key 到 .env 并热更新运行时变量。

    body 格式：{"hiagent": "sk-xxx", "openrouter": "sk-or-v1-xxx", "bailian": "sk-xxx", "deepseek": "sk-xxx"}
    前端传 provider 名（小写），后端映射到对应的环境变量名。
    """
    provider_to_key = {
        "hiagent": "HIAGENT_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
        "bailian": "BAILIAN_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "zhipu": "ZHIPU_API_KEY",
    }
    env_keys: dict[str, str] = {}
    for provider, value in body.items():
        p = str(provider).strip().lower()
        if p not in provider_to_key:
            raise HTTPException(422, f"不支持的 provider：{p}，可选：{', '.join(provider_to_key)}")
        env_keys[provider_to_key[p]] = str(value).strip()

    updated = config.save_keys_to_env(env_keys)
    if not updated:
        raise HTTPException(422, "没有提供有效的 Key")
    updated_providers = [k.replace("_API_KEY", "").lower() for k in updated]
    return {"ok": True, "updated": updated_providers}
