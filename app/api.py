"""REST API。文本阶段（圣经/规划/分镜）为后台任务 + 状态轮询；视频阶段走 worker 队列。"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile

from app import config, errors, task_registry, worker
from app.compiler import clip_duration_value, compile_prompt, shot_cost_cny
from app.db import get_conn, get_setting, log_provider_call, new_id, now, rows_to_dicts
from app.evidence import repository as evidence_repository
from app.harness.contracts import get_contract
from app.harness.types import Evaluation, EvidenceArtifact
from app.harness.context import ContextPack
from app.ingest import chapter_is_stub, chapter_titles_match, ingest_novel
from app.orchestration.engine import WorkflowRecorder, fingerprint
from app.planning import chapter_preview
from app.schemas import (Bible, EpisodeScreenplay, Shot, Storyboard,
                         StoryboardOutline, StoryboardOutlineShot, schema_errors)
from app.stages import (SCREENPLAY_SOURCE_BUDGET_CHARS, StageError, generate_bible,
                        generate_screenplay, generate_storyboard_next_shot,
                        generate_storyboard_outline)
from app.validators import (relieve_spoken_overflow,
                            normalize_action_desc, normalize_continuity,
                            normalize_offbible_characters,
                            normalize_transition_visuals,
                            storyboard_shot_count_range,
                            validate_screenplay, validate_storyboard,
                            validate_storyboard_preserves_key_content,
                            validate_storyboard_soundtrack)

router = APIRouter(prefix="/api")

BIBLE_TASK_TIMEOUT_S = 15 * 60
BIBLE_INTERRUPTED_ERROR = "人物谱任务已中断（服务重载或后台任务丢失），请重新谱写。"
FALLBACK_VISUAL_STYLE = "国漫风格，非真人CG渲染，统一电影感光影，暖灰色调"

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
    return task_registry.active("bible", project_id)


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
    task_registry.register("bible", project_id, task, project_id=project_id)


def _refs_task_active(project_id: str) -> bool:
    return task_registry.active("refs", project_id)


def _start_refs_generation(
    project_id: str,
    only_character: str | None,
    *,
    resume: bool = False,
    parent_run_id: str | None = None,
) -> bool:
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
    task_registry.spawn(
        "refs", project_id,
        _refs_task(
            project_id, only_character, resume=resume, parent_run_id=parent_run_id,
            requested_by="system" if resume else "user",
            trigger_type="resume" if resume else "manual",
        ),
        project_id=project_id,
    )
    return True


def _scene_refs_task_active(project_id: str) -> bool:
    """Whether the image-generation phase itself is active.

    Do not include ``scene_bible`` here: that coroutine deliberately starts the
    image phase before it returns.  Treating the parent phase as an already
    active image task makes the hand-off reject itself and leaves the persisted
    status stuck at ``running``.
    """
    return task_registry.active("scene_refs", project_id)


def _scene_assets_task_active(project_id: str) -> bool:
    """Whether either phase of the scene-asset pipeline is active."""
    return _scene_refs_task_active(project_id) or task_registry.active("scene_bible", project_id)


def _start_scene_refs_generation(
    project_id: str,
    only_scene: str | None,
    *,
    resume: bool = False,
    parent_run_id: str | None = None,
) -> bool:
    """启动场景图素材库生成任务。已有同项目任务在跑则返回 False。"""
    if _scene_refs_task_active(project_id):
        return False
    conn = get_conn()
    conn.execute(
        "UPDATE projects SET scene_refs_status='running', scene_refs_error=NULL, scene_refs_target=? WHERE id=?",
        (only_scene, project_id))
    conn.commit()
    task_registry.spawn(
        "scene_refs", project_id,
        _scene_refs_task(
            project_id, only_scene, resume=resume, parent_run_id=parent_run_id,
            requested_by="system" if resume else "user",
            trigger_type="resume" if resume else "manual",
        ),
        project_id=project_id,
    )
    return True


async def _scene_bible_and_refs(project_id: str) -> None:
    """场景圣经生成 + 落库 + 触发场景图批量出图（在人物谱定稿后调用，与定妆照并行）。
    场景圣经是增强项：失败只记录到 scene_refs_error，不影响人物谱/分集主流程。"""
    from app.stages import generate_scene_bible
    conn = get_conn()
    recorder = WorkflowRecorder.create(
        workflow_type="scene_bible",
        scope_type="project",
        scope_id=project_id,
        input_fingerprint=fingerprint(project_id, "scene_bible"),
        requested_by="user",
        policy_snapshot={"max_iterations": 4, "warning_blocks_downstream": True},
    )
    try:
        recorder.start()
        p = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
        if not p or not p["bible_json"]:
            raise ValueError("人物谱不存在，不能生成场景 Bible")
        bible = Bible.model_validate(json.loads(p["bible_json"]))
        # 初始场景清单只取前 N 章：避免一上来就铺满全片场景；更靠后的新场景留到分镜阶段反应式补图。
        from app.scenes import SCENE_BIBLE_CHAPTER_WINDOW
        chapters = rows_to_dicts(conn.execute(
            "SELECT * FROM chapters WHERE project_id=? ORDER BY idx LIMIT ?",
            (project_id, SCENE_BIBLE_CHAPTER_WINDOW)).fetchall())
        _, scenes = await recorder.step(
            "scene_bible",
            lambda: generate_scene_bible(chapters, bible, project_id=project_id),
            contract_key="scene_bible",
            agent_name="scene_bible",
        )
        # 重读 bible（人物谱可能已被并发流程更新），只覆盖 scenes 字段后回写。
        p2 = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
        data = json.loads(p2["bible_json"]) if p2 and p2["bible_json"] else bible.model_dump()
        data["scenes"] = [s.model_dump() for s in scenes]
        conn.execute("UPDATE projects SET bible_json=? WHERE id=?",
                     (json.dumps(data, ensure_ascii=False), project_id))
        conn.commit()
        recorder.succeed("场景 Bible 已通过合同")
        if not _start_scene_refs_generation(project_id, None):
            raise RuntimeError("场景 Bible 已完成，但场景图任务未能启动")
    except asyncio.CancelledError:
        recorder.cancel()
        raise
    except Exception as exc:  # noqa: BLE001 场景圣经失败不阻断主流程，仅透出状态
        recorder.fail(exc)
        public = errors.record_and_format(exc, action="scene_bible_generate", context={"project_id": project_id})
        conn.execute("UPDATE projects SET scene_refs_status='failed', scene_refs_error=? WHERE id=?",
                     (public, project_id))
        conn.commit()


def recover_bible_tasks() -> int:
    """启动时恢复人物谱任务（对齐 worker.recover_and_start 的语义）：
    进程重启/reload 会丢掉内存里的 asyncio.Task，但 DB 仍是 running。
    与其在下次访问时判孤儿并报错，不如用持久化的 feedback 重新拉起任务续跑。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, bible_feedback FROM projects WHERE bible_status='running'").fetchall()
    resumed = 0
    for r in rows:
        pid = r["id"]
        from app import auto
        if auto.is_running(pid):
            continue
        if _bible_task_active(pid):
            continue
        feedback = r["bible_feedback"] or ""
        parent = conn.execute(
            "SELECT id FROM workflow_runs WHERE workflow_type='character_bible' "
            "AND scope_type='project' AND scope_id=? AND status='PAUSED_EXTERNAL' "
            "AND recovered_by_run_id IS NULL ORDER BY updated_at DESC LIMIT 1",
            (pid,),
        ).fetchone()
        recorder = _new_bible_recorder(
            pid, trigger_type="resume", requested_by="system",
            parent_run_id=parent["id"] if parent else None,
        )
        _track_bible_task(
            pid,
            asyncio.get_running_loop().create_task(
                _recorded_bible_task(pid, feedback, recorder, trigger_full_refs=True)
            ),
        )
        resumed += 1
    return resumed


def recover_character_ref_tasks() -> int:
    """Resume initial portrait batches and skip per-character committed checkpoints."""
    from app import auto

    conn = get_conn()
    rows = conn.execute(
        "SELECT id, refs_target FROM projects WHERE refs_status='running'"
    ).fetchall()
    resumed = 0
    for row in rows:
        project_id = row["id"]
        if auto.is_running(project_id) or _refs_task_active(project_id):
            continue
        parent = conn.execute(
            "SELECT id FROM workflow_runs WHERE workflow_type='character_references' "
            "AND scope_type='project' AND scope_id=? AND status='PAUSED_EXTERNAL' "
            "AND recovered_by_run_id IS NULL ORDER BY updated_at DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        if _start_refs_generation(
            project_id,
            row["refs_target"],
            resume=True,
            parent_run_id=parent["id"] if parent else None,
        ):
            resumed += 1
    return resumed


def recover_scene_ref_tasks() -> int:
    """Resume persisted scene-asset work after a reload or process restart.

    Scene generation is idempotent: approved references are skipped, so an
    interrupted batch safely continues from the first missing scene instead of
    regenerating accepted assets.
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, bible_json, bible_status, scene_refs_target "
        "FROM projects WHERE scene_refs_status='running'"
    ).fetchall()
    resumed = 0
    for row in rows:
        project_id = row["id"]
        from app import auto
        # A recovered character-bible task will start a fresh scene pipeline
        # after committing its new Bible.  Starting from the old Bible here
        # would race it and could generate obsolete assets.
        if (auto.is_running(project_id) or row["bible_status"] == "running"
                or _scene_assets_task_active(project_id)):
            continue
        try:
            bible = json.loads(row["bible_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            bible = {}
        if bible.get("scenes"):
            parent = conn.execute(
                "SELECT id FROM workflow_runs WHERE workflow_type='scene_references' "
                "AND scope_type='project' AND scope_id=? AND status='PAUSED_EXTERNAL' "
                "AND recovered_by_run_id IS NULL ORDER BY updated_at DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            if _start_scene_refs_generation(
                project_id,
                row["scene_refs_target"],
                resume=True,
                parent_run_id=parent["id"] if parent else None,
            ):
                resumed += 1
            continue
        task_registry.spawn(
            "scene_bible", project_id, _scene_bible_and_refs(project_id), project_id=project_id
        )
        resumed += 1
    return resumed


def _project_or_404(project_id: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"项目不存在：{project_id}")
    return _recover_orphan_bible_row(conn, row)


def _require_harness_engine(project_id: str) -> None:
    row = get_conn().execute(
        "SELECT harness_engine_enabled FROM projects WHERE id=?", (project_id,)
    ).fetchone()
    if row and not bool(row["harness_engine_enabled"]):
        raise HTTPException(409, "该项目的 Harness Engine 已由灰度开关隔离；请重新开启后再启动新任务")


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
    # Backward-compatible repair for already imported projects: if an episode points
    # at a title-only duplicate, use the adjacent rich copy with the same normalized
    # heading. New uploads are deduplicated in app.ingest before reaching the DB.
    if len(chapters) == 1 and chapter_is_stub(chapters[0]):
        following = conn.execute(
            "SELECT * FROM chapters WHERE project_id=? AND idx>? ORDER BY idx LIMIT 1",
            (ep["project_id"], chapters[0]["idx"]),
        ).fetchone()
        if following:
            following_dict = dict(following)
            if (
                not chapter_is_stub(following_dict)
                and chapter_titles_match(chapters[0], following_dict)
            ):
                chapters = [following_dict]
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
    from app.capabilities.dispatch import dispatch, raise_if_failed, result_http_payload

    raw = await file.read()
    if not raw:
        raise HTTPException(400, "文件为空")
    token = store_upload(file.filename or "novel.txt", raw)
    result = await dispatch(
        "project.import_novel",
        {"attachment_token": token, "name": name},
        initiator="ui",
    )
    raise_if_failed(result)
    return result_http_payload(result)


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


def _public_reference_image(ref: dict) -> dict:
    """参考图对外表示：只透出前端需要的字段。绝不带上 base64 的 url 与本地 path，
    否则单集响应会因每张参考图内嵌 ~500KB base64 膨胀到数百 MB，拖垮页面甚至崩溃标签页。"""
    from app.config import PROJECTS_DIR
    image_url = None
    if ref.get("path"):
        try:
            image_url = f"/media/{Path(ref['path']).relative_to(PROJECTS_DIR).as_posix()}"
        except ValueError:
            image_url = None
    return {
        "id": ref.get("id"),
        "type": ref.get("type"),
        "source": ref.get("source"),
        "qualityScore": ref.get("qualityScore"),
        "selectedForSeedance": bool(ref.get("selectedForSeedance")),
        "deleted": bool(ref.get("deleted")),
        "rejectReason": ref.get("rejectReason"),
        "qa": ref.get("qa"),
        "image_url": image_url,
    }


def _public_failure_log(log: dict) -> dict:
    """参考图失败日志对外表示：剥掉嵌套 reference_images 里的 base64，只留轻量元信息。"""
    out = {k: v for k, v in log.items() if k != "reference_images"}
    nested = log.get("reference_images")
    if isinstance(nested, list) and nested:
        out["reference_images"] = [_public_reference_image(r) for r in nested if isinstance(r, dict)]
    return out


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
    from app.capabilities.dispatch import dispatch, raise_if_failed, result_http_payload

    result = await dispatch("project.delete", {"project_id": project_id}, initiator="ui")
    raise_if_failed(result)
    return result_http_payload(result)


# ---------- 一键全自动成片 ----------

async def _start_auto_core(project_id: str, export_dir: str | None) -> dict:
    """启动全流程自动化的领域逻辑，供 REST 路由与 ``production.auto_start`` Command Handler 共用。
    人物谱→定妆照+分集→每集（分镜→确认→参考图视频）→合成。自适应跳过已完成步骤，可重复点击从断点续做。
    export_dir：可选导出目录，每集成片合成后另存为「书名第N集.mp4」（同名已存在则跳过）。"""
    _project_or_404(project_id)
    _require_harness_engine(project_id)
    from app import auto
    if auto.is_running(project_id):
        raise HTTPException(409, "该项目的自动成片已在进行中")
    run_id = auto.start(project_id, export_dir=export_dir)
    return {"status": "running", "run_id": run_id}


@router.post("/projects/{project_id}/auto")
async def start_auto(project_id: str, body: dict | None = Body(None)):
    from app.capabilities.dispatch import dispatch, raise_if_failed, result_http_payload

    export_dir = (body or {}).get("export_dir")
    result = await dispatch(
        "production.auto_start",
        {"project_id": project_id, "directory_grant": export_dir},
        initiator="ui",
    )
    raise_if_failed(result)
    return result_http_payload(result)


@router.get("/projects/{project_id}/auto/status")
def auto_status(project_id: str):
    _project_or_404(project_id)
    from app import auto
    return auto.status(project_id)


@router.post("/projects/{project_id}/auto/cancel")
async def cancel_auto(project_id: str):
    from app.capabilities.dispatch import dispatch, raise_if_failed, result_http_payload

    result = await dispatch("production.auto_cancel", {"project_id": project_id}, initiator="ui")
    raise_if_failed(result)
    return result_http_payload(result)


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
            generate_bible(
                chapters, feedback=feedback, previous_bible=old_bible, project_id=project_id
            ),
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
        residual = list(getattr(bible, "residual_errors", []) or [])
        artifact_id = getattr(bible, "evidence_artifact_id", None)
        bible_status = "warning" if residual else "ready"
        bible_error = (
            "人物谱存在阻塞问题，允许人工修订，但不会进入下游：" + "；".join(residual[:8])
            if residual else None
        )
        # A few unit tests intentionally use a minimal legacy schema.  Production
        # databases always receive the incremental migration in app.db, while the
        # fallback keeps the stage function independently testable.
        project_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(projects)").fetchall()
        }
        if "bible_artifact_id" in project_columns:
            conn.execute(
                "UPDATE projects SET bible_json=?, bible_version=bible_version+1, bible_status=?, "
                "bible_error=?, bible_artifact_id=?, status=? WHERE id=?",
                (
                    bible.model_dump_json(), bible_status, bible_error, artifact_id,
                    "bible_ready" if not residual else "created", project_id,
                ))
        else:
            conn.execute(
                "UPDATE projects SET bible_json=?, bible_version=bible_version+1, bible_status=?, "
                "bible_error=?, status=? WHERE id=?",
                (
                    bible.model_dump_json(), bible_status, bible_error,
                    "bible_ready" if not residual else "created", project_id,
                ))
        conn.commit()
        if trigger_full_refs and not residual:
            _start_refs_generation(project_id, None)
            # 场景圣经 + 场景图素材库（与定妆照并行）：跨集场景一致性的底稿。增强项，整段失败都不能影响人物谱主流程。
            try:
                conn.execute("UPDATE projects SET scene_refs_status='running', scene_refs_error=NULL WHERE id=?",
                             (project_id,))
                conn.commit()
                task_registry.spawn(
                    "scene_bible", project_id, _scene_bible_and_refs(project_id), project_id=project_id
                )
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


def _new_bible_recorder(
    project_id: str,
    *,
    requested_by: str = "user",
    trigger_type: str = "manual",
    parent_run_id: str | None = None,
) -> WorkflowRecorder:
    conn = get_conn()
    chapters = rows_to_dicts(conn.execute(
        "SELECT idx, title, content FROM chapters WHERE project_id=? ORDER BY idx", (project_id,)
    ).fetchall())
    project = conn.execute(
        "SELECT bible_version, bible_feedback FROM projects WHERE id=?", (project_id,)
    ).fetchone()
    return WorkflowRecorder.create(
        workflow_type="character_bible",
        scope_type="project",
        scope_id=project_id,
        input_fingerprint=fingerprint(
            chapters, project["bible_version"] if project else 0,
            project["bible_feedback"] if project else None,
        ),
        requested_by=requested_by,
        trigger_type=trigger_type,
        policy_snapshot={"max_iterations": 4, "warning_blocks_downstream": True},
        parent_run_id=parent_run_id,
    )


async def _recorded_bible_task(
    project_id: str,
    feedback: str,
    recorder: WorkflowRecorder,
    *,
    trigger_full_refs: bool,
) -> None:
    recorder.start()
    try:
        conn = get_conn()
        chapters = rows_to_dicts(conn.execute(
            "SELECT idx, title, content FROM chapters WHERE project_id=? ORDER BY idx", (project_id,)
        ).fetchall())
        context = ContextPack(goal="生成可追溯人物圣经")
        context.add_text("chapters", "\n\n".join(ch["content"] for ch in chapters), limit=60000)
        await recorder.step(
            "character_bible",
            lambda: _bible_task(project_id, feedback, trigger_full_refs=trigger_full_refs),
            contract_key="character_bible",
            agent_name="character_bible",
            context_manifest=context.manifest(),
        )
        row = conn.execute("SELECT bible_status, bible_error FROM projects WHERE id=?", (project_id,)).fetchone()
        if row and row["bible_status"] == "ready":
            recorder.succeed("人物谱已通过确定性门禁")
        elif row and row["bible_status"] == "warning":
            recorder.partial(row["bible_error"] or "人物谱需要人工修订")
        else:
            recorder.fail(RuntimeError(row["bible_error"] if row else "人物谱生成失败"))
    except asyncio.CancelledError:
        recorder.cancel()
        raise
    except Exception as exc:
        recorder.fail(exc)
        raise


async def _start_bible_core(project_id: str, feedback: str) -> dict:
    """启动人物谱生成的领域逻辑，供 REST 路由与 ``bible.generate`` Command Handler 共用。"""
    p = _project_or_404(project_id)
    _require_harness_engine(project_id)
    if p["bible_status"] == "running" and _bible_task_active(project_id):
        raise HTTPException(409, "角色圣经正在生成中")
    if p["refs_status"] == "running":
        raise HTTPException(409, "定妆照正在生成中，请先停止后再重生人物谱")
    feedback = feedback.strip()
    if len(feedback) > 2000:
        raise HTTPException(400, "打回要求过长，请控制在 2000 字以内")
    conn = get_conn()
    # 持久化 feedback：进程重启后 recover_bible_tasks 能用相同入参续跑，而非中断报错
    conn.execute("UPDATE projects SET bible_status='running', bible_error=NULL, bible_feedback=? WHERE id=?",
                 (feedback, project_id))
    conn.commit()
    recorder = _new_bible_recorder(project_id)
    _track_bible_task(
        project_id,
        asyncio.create_task(
            _recorded_bible_task(project_id, feedback, recorder, trigger_full_refs=True)
        ),
    )
    return {"status": "running", "run_id": recorder.run_id}


@router.post("/projects/{project_id}/bible")
async def start_bible(project_id: str, body: dict | None = Body(None)):
    from app.capabilities.dispatch import dispatch, raise_if_failed, result_http_payload

    feedback = str((body or {}).get("feedback") or "")
    result = await dispatch(
        "bible.generate", {"project_id": project_id, "feedback": feedback}, initiator="ui"
    )
    raise_if_failed(result)
    return result_http_payload(result)


async def _cancel_bible_core(project_id: str) -> dict:
    """停止人物谱生成的领域逻辑，供 REST 路由与 ``bible.cancel`` Command Handler 共用。
    若人物谱尚未完成，停止后不会继续触发后续定妆照任务。"""
    p = _project_or_404(project_id)
    stopped = await task_registry.cancel_and_wait("bible", project_id)
    conn = get_conn()
    conn.execute(
        "UPDATE projects SET bible_status='idle', bible_error=NULL, bible_feedback=NULL WHERE id=?",
        (project_id,),
    )
    conn.commit()
    was_running = p["bible_status"] == "running"
    return {"stopped": stopped or was_running}


@router.post("/projects/{project_id}/bible/cancel")
async def cancel_bible(project_id: str):
    from app.capabilities.dispatch import dispatch, raise_if_failed, result_http_payload

    result = await dispatch("bible.cancel", {"project_id": project_id}, initiator="ui")
    raise_if_failed(result)
    return result_http_payload(result)


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
    previous_artifact_id = p.get("bible_artifact_id")
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="character_bible",
        scope_type="project",
        scope_id=project_id,
        status="validated",
        trust_level="T4",
        content=instance.model_dump(mode="json"),
        parent_artifact_ids=[previous_artifact_id] if previous_artifact_id else [],
        contract_version="character-bible-1.0.0",
    ))
    artifact = evidence_repository.commit_artifact(None, artifact["id"], [Evaluation(
        evaluator_type="human",
        evaluator_name="bible_editor",
        evaluator_version="1.0.0",
        status="passed",
        hard_gate_passed=True,
        score=100,
        evidence={"decision": "manual_edit", "style_changed": style_changed},
    )])
    stale_ids = evidence_repository.invalidate_descendants(
        previous_artifact_id,
        "人物谱已人工修订，需要重新复验下游产物",
        exclude_ids={artifact["id"]},
    ) if previous_artifact_id else []
    conn.execute(
        "UPDATE projects SET bible_json=?, bible_version=bible_version+1, bible_artifact_id=?, "
        "bible_status='ready', bible_error=NULL WHERE id=?",
        (instance.model_dump_json(), artifact["id"], project_id),
    )
    conn.execute(
        "INSERT INTO gate_decisions(id, artifact_id, gate_key, decision, decided_by, reason, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (new_id("gate"), artifact["id"], "character_bible", "approve", "bible_editor", "人工修订并定稿", now()),
    )
    conn.commit()
    return {
        "bible_version_bumped": True,
        "style_changed": style_changed,
        "purged": purge_info,
        "artifact_id": artifact["id"],
        "impact": {
            "stale_descendant_ids": stale_ids,
            "requires_reconfirm": bool(stale_ids),
            "paid_media_invalidated": bool(style_changed or stale_ids),
        },
    }


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


async def _refs_task(
    project_id: str,
    only_character: str | None,
    *,
    resume: bool = False,
    parent_run_id: str | None = None,
    requested_by: str = "user",
    trigger_type: str = "manual",
):
    from app.refs import generate_refs
    conn = get_conn()
    recorder = WorkflowRecorder.create(
        workflow_type="character_references",
        scope_type="project",
        scope_id=project_id,
        input_fingerprint=fingerprint(project_id, only_character, "character_references"),
        requested_by=requested_by,
        trigger_type=trigger_type,
        budget_limit_cny=float(get_setting("episode_cost_limit_cny") or 100),
        parent_run_id=parent_run_id,
    )
    try:
        recorder.start()
        # 重做定妆照前，先清理旧人物图衍生的评审视频与成品（按受影响角色范围）
        p = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
        if only_character:
            names = [only_character]
        elif p and p["bible_json"]:
            names = [c["name"] for c in json.loads(p["bible_json"]).get("characters", [])]
        else:
            names = []
        if not resume:
            worker.purge_character_video_artifacts(project_id, names)
        await recorder.step(
            "character_references",
            lambda: generate_refs(project_id, only_character, resume=resume),
            agent_name="reference_asset_loop",
        )
        conn.execute("UPDATE projects SET refs_status='ready', refs_error=NULL WHERE id=?", (project_id,))
        conn.commit()
        recorder.succeed("人物参考资产已生成并通过证据门禁")
    except asyncio.CancelledError:
        recorder.cancel()
        raise
    except Exception as exc:  # noqa: BLE001
        recorder.fail(exc)
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
async def cancel_refs(project_id: str):
    """停止定妆照生成。已落盘的定妆照保留，状态置回空闲。"""
    p = _project_or_404(project_id)
    stopped = await task_registry.cancel_and_wait("refs", project_id)
    conn = get_conn()
    conn.execute(
        "UPDATE projects SET refs_status='idle', refs_error=NULL, refs_target=NULL WHERE id=?", (project_id,))
    conn.commit()
    was_running = p["refs_status"] == "running"
    return {"stopped": stopped or was_running}


# ---------- 场景图素材库（跨集场景一致性） ----------
# 注：初始批量出图在此（scenes.generate_scene_refs，适用集 1~ 至今）；库外新场景的反应式发现
# 已挂在分镜阶段（见 scenes.ensure_scenes_for_storyboard），不在此轮询。


async def _scene_refs_task(
    project_id: str,
    only_scene: str | None,
    *,
    resume: bool = False,
    parent_run_id: str | None = None,
    requested_by: str = "user",
    trigger_type: str = "manual",
):
    from app.scenes import generate_scene_refs
    conn = get_conn()
    recorder = WorkflowRecorder.create(
        workflow_type="scene_references",
        scope_type="project",
        scope_id=project_id,
        input_fingerprint=fingerprint(project_id, only_scene, "scene_references"),
        requested_by=requested_by,
        trigger_type=trigger_type,
        budget_limit_cny=float(get_setting("episode_cost_limit_cny") or 100),
        parent_run_id=parent_run_id,
    )
    try:
        recorder.start()
        await recorder.step(
            "scene_references",
            lambda: generate_scene_refs(project_id, only_scene, resume=resume),
            agent_name="reference_asset_loop",
        )
        conn.execute("UPDATE projects SET scene_refs_status='ready', scene_refs_error=NULL WHERE id=?", (project_id,))
        conn.commit()
        recorder.succeed("场景参考资产已生成并通过证据门禁")
    except asyncio.CancelledError:
        recorder.cancel()
        raise
    except Exception as exc:  # noqa: BLE001
        recorder.fail(exc)
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
    if _scene_assets_task_active(project_id):
        raise HTTPException(409, "场景图正在生成中")
    conn = get_conn()
    conn.execute("UPDATE projects SET scene_refs_status='running', scene_refs_error=NULL WHERE id=?", (project_id,))
    conn.commit()
    task_registry.spawn(
        "scene_bible", project_id, _scene_bible_and_refs(project_id), project_id=project_id
    )
    return {"status": "running"}


@router.post("/projects/{project_id}/scene-refs")
async def start_scene_refs(project_id: str, body: dict | None = None):
    """（重新）生成场景图。需先有场景圣经（bible.scenes 非空）。可带 only 单场景重做。"""
    p = _project_or_404(project_id)
    if not p["bible_json"] or not json.loads(p["bible_json"]).get("scenes"):
        raise HTTPException(409, "还没有场景圣经，请先生成场景清单")
    if _scene_assets_task_active(project_id):
        raise HTTPException(409, "场景图正在生成中")
    only = (body or {}).get("scene")
    _start_scene_refs_generation(project_id, only)
    return {"status": "running"}


@router.post("/projects/{project_id}/scene-refs/cancel")
async def cancel_scene_refs(project_id: str):
    """停止场景图生成。已落盘的场景图保留，状态置回空闲。"""
    p = _project_or_404(project_id)
    stopped_bible = await task_registry.cancel_and_wait("scene_bible", project_id)
    stopped_refs = await task_registry.cancel_and_wait("scene_refs", project_id)
    stopped = stopped_bible or stopped_refs
    conn = get_conn()
    conn.execute(
        "UPDATE projects SET scene_refs_status='idle', scene_refs_error=NULL, scene_refs_target=NULL WHERE id=?",
        (project_id,))
    conn.commit()
    was_running = p["scene_refs_status"] == "running"
    return {"stopped": stopped or was_running}


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


# ---------- 可拍剧本（分集之后、分镜之前） ----------

def _screenplay_task_active(episode_id: str) -> bool:
    return task_registry.active("screenplay", episode_id)


def _screenplay_fallback_status(ep) -> str:
    if not ep["screenplay_json"]:
        return "pending"
    artifact_id = ep["screenplay_artifact_id"] if "screenplay_artifact_id" in ep.keys() else None
    if not artifact_id:
        return "ready"
    artifact = evidence_repository.get_artifact(artifact_id)
    return "ready" if artifact and artifact["status"] == "approved" else "warning"


def recover_screenplay_tasks() -> int:
    """服务热更/重启后续跑状态为 running 的剧本任务，避免 UI 卡在生成中却没有真实调用。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, project_id FROM episodes WHERE screenplay_status='running'"
    ).fetchall()
    resumed = 0
    for row in rows:
        episode_id = row["id"]
        from app import auto
        if auto.is_running(row["project_id"]):
            continue
        if _screenplay_task_active(episode_id):
            continue
        stamp = now()
        conn.execute(
            "UPDATE episodes SET screenplay_started_at=COALESCE(screenplay_started_at, ?), screenplay_updated_at=? WHERE id=?",
            (stamp, stamp, episode_id))
        parent = conn.execute(
            "SELECT id FROM workflow_runs WHERE workflow_type='screenplay' "
            "AND scope_type='episode' AND scope_id=? ORDER BY updated_at DESC LIMIT 1",
            (episode_id,),
        ).fetchone()
        recorder = _new_screenplay_recorder(
            episode_id,
            requested_by="recovery",
            trigger_type="resume",
            parent_run_id=parent["id"] if parent else None,
        )
        task_registry.spawn(
            "screenplay",
            episode_id,
            _recorded_screenplay_task(episode_id, recorder),
            project_id=row["project_id"],
        )
        resumed += 1
    conn.commit()
    return resumed


async def _screenplay_character_discovery(
    episode_id: str,
    source_text: str,
    *,
    draft_text: str = "",
) -> dict:
    """Run the required incremental cast pass for one screenplay generation."""
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        raise StageError("新人物发现", ["剧集不存在"])
    project = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    if not project or not (project["bible_json"] or "").strip():
        # Preserve the existing placeholder-bible workflow. Projects with a real
        # bible must pass discovery; placeholder projects can still draft text.
        return {
            "checked": 0, "candidates": [], "added": [], "skipped": [],
            "errors": [], "warnings": ["项目尚无人物谱，已跳过增量人物发现"],
        }
    bible = _project_bible_or_placeholder(project)
    from app.portraits import ensure_cards_for_text

    result = await ensure_cards_for_text(
        ep["project_id"],
        ep["episode_no"],
        source_text,
        bible,
        draft_text=draft_text,
    )
    if result.get("errors"):
        raise StageError("新人物发现", list(result["errors"]))
    for warning in result.get("warnings") or []:
        errors.log_error(
            None,
            action="screenplay_character_discovery_warning",
            context={
                "project_id": ep["project_id"],
                "episode_id": episode_id,
                "episode_no": ep["episode_no"],
            },
            message=warning,
        )
    return result


async def _screenplay_task(
    episode_id: str,
    *,
    preflight_result: dict | None = None,
) -> EpisodeScreenplay | None:
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    try:
        ep_data = dict(ep)
        p = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
        bible = _project_bible_or_placeholder(p)
        source_text = _episode_source_text(conn, ep)
        if preflight_result is None:
            preflight_result = await _screenplay_character_discovery(episode_id, source_text)
        if preflight_result.get("added"):
            p = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
            bible = _project_bible_or_placeholder(p)
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
        # Second guard: audit the actual generated body, not only the source preflight.
        # This catches named people that the model placed in prose/dialogue while
        # omitting them from scene_outline.characters (the historical deadlock).
        draft_audit = await _screenplay_character_discovery(
            episode_id,
            source_text,
            draft_text=script.model_dump_json(),
        )
        if draft_audit.get("added"):
            p = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
            bible = _project_bible_or_placeholder(p)
            # Regenerate at most once with the expanded bible so the final outline,
            # dialogue attribution, relationships, and speech styles all share the
            # same authoritative cast.
            script = await generate_screenplay(
                ep_data,
                source_text,
                bible,
                prev_ending=prev["cliffhanger"] if prev else "",
            )
        old_script = _load_screenplay(ep)
        script = _prepare_screenplay_for_storage(
            ep, script,
            keep_existing_id=(old_script.id if old_script else None),
            keep_created_at=(old_script.created_at if old_script else None),
        )
        # 新剧本会让旧分镜/视频失效；保存前清空下游，确保后续必须重新展开。
        worker.delete_episode_shots(episode_id)
        residual = list(getattr(script, "residual_errors", []) or [])
        artifact_id = getattr(script, "evidence_artifact_id", None)
        note = None
        if residual:
            note = (
                "当前仅为 WARNING 候选，存在未解决硬门禁；可手动修改后保存，"
                "但修复前不能进入分镜：" + "；".join(residual)
            )
        screenplay_status = "warning" if residual else "ready"
        conn.execute(
            "UPDATE episodes SET screenplay_json=?, screenplay_status=?, screenplay_error=?, "
            "screenplay_updated_at=?, screenplay_artifact_id=?, status='planned', script_error=NULL WHERE id=?",
            (
                script.model_dump_json(), screenplay_status, (note or "")[:800] or None,
                now(), artifact_id, episode_id,
            ))
        conn.commit()
        return script
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
        return None


def _new_screenplay_recorder(
    episode_id: str,
    *,
    requested_by: str = "user",
    trigger_type: str = "manual",
    parent_run_id: str | None = None,
) -> WorkflowRecorder:
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        raise ValueError(f"episode not found: {episode_id}")
    project = conn.execute(
        "SELECT bible_version FROM projects WHERE id=?", (ep["project_id"],)
    ).fetchone()
    source_text = _episode_source_text(conn, ep)
    return WorkflowRecorder.create(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id=episode_id,
        input_fingerprint=fingerprint(
            episode_id,
            ep["source_chapters"],
            source_text,
            project["bible_version"] if project else 0,
        ),
        requested_by=requested_by,
        trigger_type=trigger_type,
        policy_snapshot={
            "contract": "screenplay@1.0.0",
            "max_iterations": min(max(int(get_setting("max_repair_attempts") or 4), 1), 4),
            "stall_rounds": 2,
            "min_quality_gain": 0.03,
        },
        parent_run_id=parent_run_id,
    )


def _screenplay_context_pack(episode_id: str) -> tuple[list[str], dict]:
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    source_text = _episode_source_text(conn, ep)
    bible_artifact = evidence_repository.latest_artifact(
        "character_bible", "project", ep["project_id"]
    )
    mapping_artifact = evidence_repository.latest_artifact(
        "episode_mapping", "project", ep["project_id"]
    )
    previous_artifact_id = ep["screenplay_artifact_id"]
    input_ids = [
        artifact_id
        for artifact_id in (
            bible_artifact["id"] if bible_artifact else None,
            mapping_artifact["id"] if mapping_artifact else None,
            previous_artifact_id,
        )
        if artifact_id
    ]
    pack = ContextPack(
        goal=f"生成第 {ep['episode_no']} 集可拍剧本",
        metadata={
            "episode_id": episode_id,
            "episode_no": ep["episode_no"],
            "contract_version": "1.0.0",
        },
    )
    pack.add_text(
        "source_text",
        source_text,
        limit=SCREENPLAY_SOURCE_BUDGET_CHARS,
        truncation_strategy="head_with_truncation_notice",
    )
    bible_json = project["bible_json"] or "{}"
    pack.add_text(
        "character_bible",
        bible_json,
        limit=max(len(bible_json), 1),
        source_artifact_id=bible_artifact["id"] if bible_artifact else None,
        truncation_strategy="none",
    )
    return list(dict.fromkeys(input_ids)), pack.manifest()


async def _recorded_screenplay_task(
    episode_id: str,
    recorder: WorkflowRecorder,
) -> EpisodeScreenplay | None:
    async def operation(preflight: dict) -> EpisodeScreenplay:
        generated = await _screenplay_task(episode_id, preflight_result=preflight)
        if generated is None:
            row = get_conn().execute(
                "SELECT screenplay_error FROM episodes WHERE id=?", (episode_id,)
            ).fetchone()
            raise RuntimeError(row["screenplay_error"] if row else "剧本任务未产生结果")
        return generated

    try:
        recorder.start()
        discovery_source = _episode_source_text(
            get_conn(),
            get_conn().execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone(),
        )
        _, preflight = await recorder.step(
            "character_discovery",
            lambda: _screenplay_character_discovery(episode_id, discovery_source),
            agent_name="screenplay_character_discovery",
            context_manifest={
                "episode_id": episode_id,
                "source_chars": len(discovery_source),
                "phase": "before_screenplay",
            },
        )
        # Discovery may advance bible_version. Refresh the persisted fingerprint and
        # context pack before the screenplay step so evidence describes the inputs
        # actually used by generation.
        fingerprint_ep = get_conn().execute(
            "SELECT * FROM episodes WHERE id=?", (episode_id,)
        ).fetchone()
        fingerprint_project = get_conn().execute(
            "SELECT bible_version FROM projects WHERE id=?", (fingerprint_ep["project_id"],)
        ).fetchone()
        get_conn().execute(
            "UPDATE workflow_runs SET input_fingerprint=?, updated_at=? WHERE id=?",
            (
                fingerprint(
                    episode_id,
                    fingerprint_ep["source_chapters"],
                    discovery_source,
                    fingerprint_project["bible_version"] if fingerprint_project else 0,
                ),
                now(),
                recorder.run_id,
            ),
        )
        get_conn().commit()
        input_artifact_ids, context_manifest = _screenplay_context_pack(episode_id)
        _, script = await recorder.step(
            "screenplay",
            lambda: operation(preflight),
            contract_key="screenplay",
            agent_name="screenplay_agent_loop",
            input_artifact_ids=input_artifact_ids,
            context_manifest=context_manifest,
        )
        row = get_conn().execute(
            "SELECT screenplay_status, screenplay_error FROM episodes WHERE id=?", (episode_id,)
        ).fetchone()
        if not row:
            raise RuntimeError("剧本任务完成后剧集记录不存在")
        if row["screenplay_status"] == "warning":
            recorder.partial(row["screenplay_error"] or "剧本存在未解决门禁")
        else:
            recorder.succeed("剧本已通过确定性门禁")
        return script
    except asyncio.CancelledError:
        recorder.cancel("剧本生成已取消")
        raise
    except Exception as exc:  # noqa: BLE001 -- failure is persisted for Run Center
        row = get_conn().execute(
            "SELECT screenplay_status FROM episodes WHERE id=?", (episode_id,)
        ).fetchone()
        if row and row["screenplay_status"] == "running":
            public = errors.record_and_format(
                exc,
                action="screenplay_generate",
                context={"episode_id": episode_id, "phase": "character_discovery"},
            )
            get_conn().execute(
                "UPDATE episodes SET screenplay_status='failed', screenplay_error=?, screenplay_updated_at=? WHERE id=?",
                (public, now(), episode_id),
            )
            get_conn().commit()
        recorder.fail(exc)
        return None


@router.post("/episodes/{episode_id}/screenplay")
async def start_screenplay(episode_id: str, body: dict | None = Body(None)):
    ep = _episode_or_404(episode_id)
    _require_harness_engine(ep["project_id"])
    if ep["status"] == "scripting":
        raise HTTPException(409, "分镜正在生成中，不能同时重写剧本")
    if ep["screenplay_status"] == "running" and _screenplay_task_active(episode_id):
        raise HTTPException(409, "剧本正在生成中")
    force = bool((body or {}).get("force"))
    conn = get_conn()
    has_shots = conn.execute("SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,)).fetchone()["c"] > 0
    if has_shots and not force:
        raise HTTPException(409, "重新生成剧本会清空本集现有分镜、参考图、视频和成片，请确认后重试")
    started_at = now()
    conn.execute(
        "UPDATE episodes SET screenplay_status='running', screenplay_error=NULL, screenplay_started_at=?, screenplay_updated_at=? WHERE id=?",
        (started_at, started_at, episode_id))
    conn.commit()
    recorder = _new_screenplay_recorder(episode_id)
    task_registry.spawn(
        "screenplay",
        episode_id,
        _recorded_screenplay_task(episode_id, recorder),
        project_id=ep["project_id"],
    )
    return {"status": "running", "run_id": recorder.run_id}


async def _screenplay_guarded(
    episode_id: str,
    sem: asyncio.Semaphore,
    recorder: WorkflowRecorder,
):
    async with sem:
        await _recorded_screenplay_task(episode_id, recorder)


@router.post("/projects/{project_id}/screenplay-all")
async def start_screenplay_all(project_id: str):
    _project_or_404(project_id)
    _require_harness_engine(project_id)
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, screenplay_status, screenplay_json FROM episodes WHERE project_id=? ORDER BY episode_no",
        (project_id,)).fetchall()
    ids = [
        r["id"] for r in rows
        if not r["screenplay_json"]
        or r["screenplay_status"] in ("pending", "failed", "warning")
        or (r["screenplay_status"] == "running" and not task_registry.active("screenplay", r["id"]))
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
    run_ids: list[str] = []
    for eid in ids:
        recorder = _new_screenplay_recorder(eid)
        run_ids.append(recorder.run_id)
        task_registry.spawn(
            "screenplay", eid, _screenplay_guarded(eid, sem, recorder), project_id=project_id
        )
    return {"started": len(ids), "run_ids": run_ids}


@router.post("/projects/{project_id}/screenplay-all/cancel")
async def cancel_screenplay_all(project_id: str):
    """停止本项目所有正在进行的剧本生成：取消在跑任务，未开跑的回退状态。"""
    _project_or_404(project_id)
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, screenplay_json FROM episodes WHERE project_id=? AND screenplay_status='running'",
        (project_id,)).fetchall()
    stopped = 0
    for r in rows:
        eid = r["id"]
        await task_registry.cancel_and_wait("screenplay", eid)
        full = conn.execute("SELECT * FROM episodes WHERE id=?", (eid,)).fetchone()
        fallback = _screenplay_fallback_status(full)
        conn.execute(
            "UPDATE episodes SET screenplay_status=?, screenplay_error=NULL, screenplay_updated_at=? WHERE id=?",
            (fallback, now(), eid))
        stopped += 1
    conn.commit()
    return {"stopped": stopped}


@router.post("/episodes/{episode_id}/screenplay/cancel")
async def cancel_screenplay(episode_id: str):
    ep = _episode_or_404(episode_id)
    if ep["screenplay_status"] != "running":
        raise HTTPException(409, "当前没有正在进行的剧本生成")
    await task_registry.cancel_and_wait("screenplay", episode_id)
    conn = get_conn()
    fallback = _screenplay_fallback_status(ep)
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
    expected = max(1, int(ep["target_duration_s"]) // config.VIDEO_DURATION_MIN_S)
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
        raise HTTPException(409, "修改剧本会清空本集现有分镜、参考图、视频和成片，请确认后重试")
    if has_shots:
        worker.delete_episode_shots(episode_id)
    candidate = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="episode_screenplay",
            scope_type="episode",
            scope_id=episode_id,
            status="validated",
            trust_level="T2",
            content=instance.model_dump(mode="json"),
            parent_artifact_ids=(
                [ep["screenplay_artifact_id"]]
                if ep["screenplay_artifact_id"]
                else []
            ),
            contract_version="1.0.0",
        )
    )
    adopted = evidence_repository.commit_artifact(
        None,
        candidate["id"],
        [
            Evaluation(
                evaluator_type="deterministic",
                evaluator_name="screenplay_validator",
                evaluator_version="1.0.0",
                status="passed",
                hard_gate_passed=True,
                score=100,
                evidence={"episode_id": episode_id, "source": "manual_edit"},
            ),
            Evaluation(
                evaluator_type="human",
                evaluator_name="screenplay_editor",
                evaluator_version="1.0.0",
                status="passed",
                hard_gate_passed=True,
                evidence={"decision": "approve", "source": "manual_edit"},
            ),
        ],
    )
    conn.execute(
        "UPDATE episodes SET screenplay_json=?, screenplay_status='ready', screenplay_error=NULL, "
        "screenplay_artifact_id=?, status='planned', script_error=NULL WHERE id=?",
        (instance.model_dump_json(), adopted["id"], episode_id))
    conn.commit()
    return {"saved": True, "beats": len(instance.beats), "downstream_cleared": has_shots}


# ---------- 分镜脚本 ----------

# 正在进行的分镜生成任务，按 episode_id 跟踪，便于手动取消
def _insert_storyboard_shot(conn, episode_id: str, screenplay: EpisodeScreenplay, shot: Shot) -> str:
    shot_id = new_id("shot")
    shot.action_desc = normalize_action_desc(shot.action_desc)
    conn.execute(
        "INSERT INTO shots(id, episode_id, script_id, shot_no, duration_s, shot_size, camera_move, scene_setting, scene_name, characters, action_desc, first_frame_desc, last_frame_desc, source_excerpt, narration, dialogues, transition, continuity_from_prev, storyboard_artifact_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (shot_id, episode_id, screenplay.id, shot.shot_no, shot.duration_s, shot.shot_size, shot.camera_move,
         shot.scene_setting, shot.scene_name or None, json.dumps(shot.characters, ensure_ascii=False), shot.action_desc,
         shot.first_frame_desc, shot.last_frame_desc, shot.source_excerpt, shot.narration,
         json.dumps([d.model_dump() for d in shot.dialogues], ensure_ascii=False),
         shot.transition, int(shot.continuity_from_prev),
         getattr(shot, "evidence_artifact_id", None)))
    return shot_id


def _sync_storyboard_shot_timing(conn, episode_id: str, board: Storyboard) -> None:
    for shot in board.shots:
        conn.execute(
            "UPDATE shots SET duration_s=?, transition=?, continuity_from_prev=?, last_frame_desc=? WHERE episode_id=? AND shot_no=?",
            (shot.duration_s, shot.transition, int(shot.continuity_from_prev), shot.last_frame_desc,
             episode_id, shot.shot_no),
        )


def _persist_storyboard_character_policy_repairs(
    conn, episode_id: str, board: Storyboard, changes: list[dict]
) -> list[str]:
    """Persist deterministic repairs as derived T1 candidates, preserving lineage.

    The character-policy evaluation only proves this normalization, not every storyboard
    gate, so the derived artifact must not be committed as T2 on its own.
    """
    material = [change for change in changes if change.get("mutated")]
    if not material:
        return []
    contract_version = get_contract("storyboard").version
    artifact_ids: list[str] = []
    by_shot = {shot.shot_no: shot for shot in board.shots}
    for shot_no in dict.fromkeys(int(change["shot_no"]) for change in material):
        row = conn.execute(
            "SELECT id, storyboard_artifact_id FROM shots WHERE episode_id=? AND shot_no=?",
            (episode_id, shot_no),
        ).fetchone()
        shot = by_shot.get(shot_no)
        if row is None or shot is None:
            continue
        shot_changes = [change for change in material if int(change["shot_no"]) == shot_no]
        previous_artifact_id = row["storyboard_artifact_id"]
        artifact = evidence_repository.create_artifact(EvidenceArtifact(
            type="storyboard_shot",
            scope_type="storyboard_checkpoint",
            scope_id=f"{episode_id}:{shot_no}",
            status="candidate",
            trust_level="T1",
            content=shot.model_dump(mode="json"),
            parent_artifact_ids=[previous_artifact_id] if previous_artifact_id else [],
            contract_version=contract_version,
        ))
        evidence_repository.create_evaluation(
            artifact["id"],
            Evaluation(
                evaluator_type="deterministic",
                evaluator_name="storyboard_character_policy",
                evaluator_version=contract_version,
                status="passed",
                hard_gate_passed=True,
                score=100,
                evidence={
                    "policy": "functional_extra_v1",
                    "scope": "character_policy_only",
                    "changes": shot_changes,
                },
            ),
        )
        if previous_artifact_id:
            evidence_repository.invalidate_descendants(
                previous_artifact_id,
                f"镜头角色合同已由 {artifact['id']} 修订",
                exclude_ids={str(artifact["id"])},
            )
        has_runtime_derivatives = conn.execute(
            """SELECT EXISTS(SELECT 1 FROM shot_versions WHERE shot_id=?)
                      OR EXISTS(SELECT 1 FROM shot_scenes WHERE shot_id=?) AS present""",
            (row["id"], row["id"]),
        ).fetchone()["present"]
        if has_runtime_derivatives:
            worker.clear_shot_artifacts(row["id"])
        conn.execute(
            """UPDATE shots SET characters=?, action_desc=?, first_frame_desc=?,
               last_frame_desc=?, narration=?, dialogues=?, storyboard_artifact_id=? WHERE id=?""",
            (
                json.dumps(shot.characters, ensure_ascii=False),
                shot.action_desc,
                shot.first_frame_desc,
                shot.last_frame_desc,
                shot.narration,
                json.dumps([dialogue.model_dump() for dialogue in shot.dialogues], ensure_ascii=False),
                artifact["id"],
                row["id"],
            ),
        )
        artifact_ids.append(str(artifact["id"]))
        log_provider_call(
            "storyboard_character_policy",
            config.MODEL_TEXT,
            "CHARACTER_POLICY_REPAIRED",
            None,
            0,
            meta={
                "episode_id": episode_id,
                "shot_no": shot_no,
                "contract_version": contract_version,
                "artifact_id": artifact["id"],
                "changes": shot_changes,
            },
        )
    conn.commit()
    return artifact_ids


def _finalize_storyboard_evidence(episode_id: str, board: Storyboard) -> str:
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    shot_rows = conn.execute(
        "SELECT storyboard_artifact_id FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)
    ).fetchall()
    parents = [
        artifact_id for artifact_id in (
            project["bible_artifact_id"], ep["screenplay_artifact_id"],
            *(row["storyboard_artifact_id"] for row in shot_rows),
        ) if artifact_id
    ]
    contract_version = get_contract("storyboard").version
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="storyboard",
        scope_type="episode",
        scope_id=episode_id,
        status="validated",
        trust_level="T2",
        content=board.model_dump(mode="json"),
        parent_artifact_ids=parents,
        contract_version=contract_version,
    ))
    evaluation = Evaluation(
        evaluator_type="deterministic",
        evaluator_name="storyboard_full_gate",
        evaluator_version=contract_version,
        status="passed",
        hard_gate_passed=True,
        score=100,
        evidence={
            "shot_count": len(board.shots),
            "duration_range_s": [config.VIDEO_DURATION_MIN_S, config.VIDEO_DURATION_MAX_S],
            "duration_decided_by": "model",
            "checkpoint_artifact_ids": parents,
        },
    )
    artifact = evidence_repository.commit_artifact(None, artifact["id"], [evaluation])
    conn.execute(
        "UPDATE episodes SET storyboard_artifact_id=? WHERE id=?", (artifact["id"], episode_id)
    )
    conn.commit()
    return str(artifact["id"])


def _reconcile_storyboard_plan(conn, episode_id: str, episode_no: int,
                              outline: StoryboardOutline | None, completed: list[Shot],
                              persisted_total: int) -> tuple[int, int, str] | None:
    """让落库大纲成为唯一事实源，消除"规划十几镜却分镜24"的困惑。

    逐镜阶段大纲会被就地改写：①covers 不可单镜完成时 _maybe_split_outline_covers 会插入新节拍；
    ②模型判断单镜超过 10 秒仍演不完而继续拆镜、镜头数超出计划长度。两种情况下内存 outline 都会领先于
    落库的 storyboard_outline_json，导致前端 storyboard_planned_shots 显示陈旧的初始估算。

    本函数在每提交一镜后把当前计划追平实际镜头数并回写 DB，使规划数随逐镜细化实时自更新、
    单调不减且始终 ≥ 已通过镜头数。返回 (from_total, to_total, reason) 供事件记录；无变化返回 None。
    """
    if outline is None:
        return None
    appended = False
    # 模型拆镜超出计划、但未触发 covers 自动拆分：补占位节拍，让计划长度追平实际。
    if len(outline.shots) < len(completed):
        appended = True
        for shot in completed[len(outline.shots):]:
            raw = (shot.action_desc or shot.narration or "").strip()
            beat = "".join(raw.split())[:60] or "逐镜细化新增镜头"
            outline.shots.append(StoryboardOutlineShot(
                shot_no=len(outline.shots) + 1,
                scene_setting=shot.scene_setting or "",
                beat=beat,
                covers="",
            ))
    to_total = len(outline.shots)
    if to_total == persisted_total:
        return None
    conn.execute("UPDATE episodes SET storyboard_outline_json=? WHERE id=?",
                 (outline.model_dump_json(), episode_id))
    conn.commit()
    reason = "shot_overflow" if appended else "covers_split"
    log_provider_call(
        "storyboard_plan_revised", config.MODEL_TEXT, "PLAN_REVISED", None, 0,
        meta={"episode_id": episode_id, "episode_no": episode_no, "stage": "分镜脚本",
              "from": persisted_total, "to": to_total,
              "actual_shots": len(completed), "reason": reason})
    return (persisted_total, to_total, reason)


async def _storyboard_task(episode_id: str, *, resume: bool = True):
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
        # 新人物补卡失败是阻塞问题；已有角色漂移重绘失败是可见的非阻塞警告。
        from app.portraits import ensure_cards_for_screenplay
        disc = await ensure_cards_for_screenplay(
            ep["project_id"], ep["episode_no"], screenplay, bible,
        )
        if disc.get("blocking_errors"):
            raise StageError("新人物发现", list(disc["blocking_errors"]))
        for warning in disc.get("errors") or []:
            errors.log_error(
                None,
                action="storyboard_character_maintenance_warning",
                context={
                    "project_id": ep["project_id"],
                    "episode_id": episode_id,
                    "episode_no": ep["episode_no"],
                },
                message=warning,
            )
        if disc.get("added") or disc.get("redrawn"):
            p = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
            bible = _project_bible_or_placeholder(p)
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

        existing_rows = conn.execute(
            "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)
        ).fetchall()
        if not resume:
            worker.delete_episode_shots(episode_id)
            conn.execute(
                "UPDATE episodes SET storyboard_outline_json=NULL, storyboard_artifact_id=NULL WHERE id=?",
                (episode_id,),
            )
            conn.commit()
            existing_rows = []
        # 先出整集分镜大纲定全局节奏，再逐镜按大纲填充——避免多镜停留同一情绪、剧情推进过慢。
        # 大纲是增强项：规划失败（如模型不可用）就回退到无大纲的纯逐镜生成，不阻断分镜。
        outline = None
        if resume and ep["storyboard_outline_json"]:
            try:
                from app.schemas import StoryboardOutline

                outline = StoryboardOutline.model_validate_json(ep["storyboard_outline_json"])
            except (TypeError, ValueError):
                outline = None
        if outline is None:
            try:
                outline = await generate_storyboard_outline(
                    ep_data, source_text, bible,
                    prev_ending=prev["cliffhanger"] if prev else "", screenplay=screenplay)
                conn.execute("UPDATE episodes SET storyboard_outline_json=? WHERE id=?",
                             (outline.model_dump_json(), episode_id))
                conn.commit()
            except Exception:  # noqa: BLE001 大纲失败不阻断，退回纯逐镜生成
                outline = None
        completed: list[Shot] = (
            list(_board_from_shot_rows(existing_rows, ep_data["episode_no"]).shots)
            if existing_rows else []
        )
        if completed:
            recovered_board = Storyboard(
                episode_no=ep_data["episode_no"], shots=list(completed)
            )
            character_changes = normalize_offbible_characters(recovered_board, bible)
            _persist_storyboard_character_policy_repairs(
                conn, episode_id, recovered_board, character_changes
            )
            completed = list(recovered_board.shots)
        if completed and outline and len(completed) >= len(outline.shots):
            recovered_board = Storyboard(episode_no=ep_data["episode_no"], shots=list(completed))
            recovered_errors = validate_storyboard(
                recovered_board, bible, ep_data["target_duration_s"]
            )
            recovered_errors.extend(validate_storyboard_soundtrack(
                recovered_board, screenplay, ep_data["target_duration_s"]
            ))
            recovered_errors.extend(validate_storyboard_preserves_key_content(recovered_board, screenplay))
            if not recovered_errors:
                _finalize_storyboard_evidence(episode_id, recovered_board)
                conn.execute(
                    "UPDATE episodes SET status='scripted', script_error=NULL WHERE id=?", (episode_id,)
                )
                conn.commit()
                return
        final_feedback: list[str] | None = None
        _, max_shots = storyboard_shot_count_range(ep_data["target_duration_s"])
        # 落库大纲的当前长度（内存与 DB 此刻一致：resume 从 DB 加载、新规划刚回写）。
        planned_persisted = len(outline.shots) if (outline and outline.shots) else 0
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
            # 与逐镜 QA 同口径：实名角色服从角色圣经；功能性路人按确定性合同保留，其它圣经外名字剥离。
            # 每次分类都写入账本，避免“放宽角色限制”变成不可审计的静默绕过。
            for c in normalize_offbible_characters(board, bible):
                allowed_extra = bool(c.get("allowed_functional_extra"))
                log_provider_call(
                    "storyboard_character_policy", config.MODEL_TEXT,
                    "FUNCTIONAL_EXTRA_ALLOWED" if allowed_extra else "OFFBIBLE_NORMALIZED",
                    None, 0,
                    meta={"episode_id": episode_id, "episode_no": ep_data["episode_no"], "stage": "分镜脚本", **c})
            relieve_spoken_overflow(board)  # 与逐镜 QA 同口径：人群旁白降级为画面，单镜口播压回上限内
            normalize_transition_visuals(board)
            _sync_storyboard_shot_timing(conn, episode_id, board)
            shot = board.shots[-1]
            object.__setattr__(
                shot, "evidence_artifact_id", getattr(draft, "evidence_artifact_id", None)
            )
            _insert_storyboard_shot(conn, episode_id, screenplay, shot)
            completed = list(board.shots)
            conn.execute("UPDATE episodes SET status='scripting', script_error=NULL WHERE id=?", (episode_id,))
            conn.commit()
            # 计划自更新：把落库大纲追平实际镜头数，让前端"规划 N 镜"随逐镜拆镜细化实时更新（harness 事件留痕）。
            revision = _reconcile_storyboard_plan(
                conn, episode_id, ep_data["episode_no"], outline, completed, planned_persisted)
            if revision is not None:
                planned_persisted = revision[1]
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
                        f"镜{shot.shot_no:02d}{_storyboard_loop_exit_text(getattr(draft, 'loop_exit_reason', ''))}，"
                        "已作为「需修改镜头」保留在分镜台（逐镜 checkpoint 已保存，可从下一镜继续）；"
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
                _finalize_storyboard_evidence(
                    episode_id,
                    Storyboard(episode_no=ep_data["episode_no"], shots=list(completed)),
                )
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


def recover_storyboard_tasks() -> int:
    """恢复中断的分镜任务；从最后一个已提交的逐镜 checkpoint 继续。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, project_id FROM episodes "
        "WHERE status='scripting' AND screenplay_status='ready' AND screenplay_json IS NOT NULL"
    ).fetchall()
    resumed = 0
    for row in rows:
        from app import auto
        if auto.is_running(row["project_id"]):
            continue
        if not task_registry.active("storyboard", row["id"]):
            parent = conn.execute(
                "SELECT id FROM workflow_runs WHERE workflow_type='storyboard' "
                "AND scope_type='episode' AND scope_id=? AND status='PAUSED_EXTERNAL' "
                "AND recovered_by_run_id IS NULL ORDER BY updated_at DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
            recorder = _new_storyboard_recorder(
                row["id"], requested_by="system", trigger_type="resume",
                parent_run_id=parent["id"] if parent else None,
            )
            task_registry.spawn(
                "storyboard", row["id"],
                _recorded_storyboard_task(row["id"], recorder, resume=True),
                project_id=row["project_id"],
            )
            resumed += 1
    return resumed


def _new_storyboard_recorder(
    episode_id: str,
    *,
    requested_by: str = "user",
    trigger_type: str = "manual",
    parent_run_id: str | None = None,
) -> WorkflowRecorder:
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    checkpoints = rows_to_dicts(conn.execute(
        "SELECT shot_no, storyboard_artifact_id FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall())
    contract = get_contract("storyboard")
    return WorkflowRecorder.create(
        workflow_type="storyboard",
        scope_type="episode",
        scope_id=episode_id,
        input_fingerprint=fingerprint(
            ep["screenplay_artifact_id"], ep["storyboard_outline_json"], checkpoints
        ),
        requested_by=requested_by,
        trigger_type=trigger_type,
        policy_snapshot={
            "checkpoint": "per_shot",
            "max_iterations_per_shot": contract.max_iterations,
            "provider_retry": {
                "max_retries_per_call": config.TEXT_PROVIDER_MAX_RETRIES,
                "base_delay_s": config.TEXT_PROVIDER_RETRY_BASE_DELAY,
                "strategy": "bounded_exponential_backoff_same_request",
            },
        },
        config_snapshot={"storyboard_shot_max_tokens": config.STORYBOARD_SHOT_MAX_TOKENS},
        parent_run_id=parent_run_id,
    )


async def _recorded_storyboard_task(
    episode_id: str,
    recorder: WorkflowRecorder,
    *,
    resume: bool,
) -> None:
    recorder.start()
    try:
        conn = get_conn()
        ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
        project = conn.execute("SELECT bible_artifact_id FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
        input_ids = [
            artifact_id for artifact_id in (
                project["bible_artifact_id"] if project else None,
                ep["screenplay_artifact_id"],
            ) if artifact_id
        ]
        context = ContextPack(goal="按已确认剧本逐镜生成分镜并从 checkpoint 恢复")
        if ep["screenplay_json"]:
            context.add_text(
                "screenplay", ep["screenplay_json"],
                source_artifact_id=ep["screenplay_artifact_id"], limit=24000,
            )
        await recorder.step(
            "storyboard",
            lambda: _storyboard_task(episode_id, resume=resume),
            contract_key="storyboard",
            agent_name="storyboard",
            input_artifact_ids=input_ids,
            context_manifest=context.manifest(),
        )
        result = conn.execute(
            "SELECT status, script_error, storyboard_artifact_id FROM episodes WHERE id=?",
            (episode_id,),
        ).fetchone()
        if result and result["status"] == "scripted" and result["storyboard_artifact_id"]:
            recorder.succeed("分镜已完成并提交整版 Artifact")
        elif result and result["status"] == "scripted":
            recorder.partial(result["script_error"] or "分镜含需人工修订 checkpoint")
        else:
            recorder.fail(RuntimeError(result["script_error"] if result else "分镜生成失败"))
    except asyncio.CancelledError:
        recorder.cancel()
        raise
    except Exception as exc:
        recorder.fail(exc)
        raise


@router.post("/episodes/{episode_id}/storyboard")
async def start_storyboard(episode_id: str):
    ep = _episode_or_404(episode_id)
    _require_harness_engine(ep["project_id"])
    if ep["status"] == "scripting":
        raise HTTPException(409, "分镜正在生成中")
    if not _screenplay_ready(ep):
        raise HTTPException(409, "请先在剧本台生成本集可拍剧本")
    conn = get_conn()
    conn.execute("UPDATE episodes SET status='scripting', script_error=NULL WHERE id=?", (episode_id,))
    conn.commit()
    recorder = _new_storyboard_recorder(episode_id)
    task_registry.spawn(
        "storyboard", episode_id,
        _recorded_storyboard_task(episode_id, recorder, resume=False),
        project_id=ep["project_id"],
    )
    return {"status": "scripting", "run_id": recorder.run_id}


@router.post("/episodes/{episode_id}/storyboard/resume")
async def resume_storyboard(episode_id: str):
    """Continue after the last committed per-shot checkpoint without deleting it."""
    ep = _episode_or_404(episode_id)
    _require_harness_engine(ep["project_id"])
    if ep["status"] == "scripting" or task_registry.active("storyboard", episode_id):
        raise HTTPException(409, "分镜正在生成中")
    if not _screenplay_ready(ep):
        raise HTTPException(409, "请先在剧本台生成本集可拍剧本")
    conn = get_conn()
    saved = conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,)
    ).fetchone()["c"]
    if not saved:
        raise HTTPException(409, "当前没有可恢复的逐镜 checkpoint，请重新生成分镜")
    parent = conn.execute(
        """SELECT id FROM workflow_runs
           WHERE workflow_type='storyboard' AND scope_type='episode' AND scope_id=?
           ORDER BY updated_at DESC LIMIT 1""",
        (episode_id,),
    ).fetchone()
    conn.execute(
        "UPDATE episodes SET status='scripting', script_error=NULL WHERE id=?", (episode_id,)
    )
    conn.commit()
    recorder = _new_storyboard_recorder(
        episode_id,
        trigger_type="resume",
        parent_run_id=parent["id"] if parent else None,
    )
    task_registry.spawn(
        "storyboard",
        episode_id,
        _recorded_storyboard_task(episode_id, recorder, resume=True),
        project_id=ep["project_id"],
    )
    return {
        "status": "scripting",
        "run_id": recorder.run_id,
        "resumed_from_shot": int(saved),
        "next_shot_no": int(saved) + 1,
    }


async def _storyboard_guarded(episode_id: str, sem: asyncio.Semaphore):
    """带并发上限的分镜任务，用于批量生成时不一次性打爆模型网关。"""
    async with sem:
        recorder = _new_storyboard_recorder(episode_id, trigger_type="batch")
        await _recorded_storyboard_task(episode_id, recorder, resume=True)
        return recorder.run_id


@router.post("/projects/{project_id}/storyboard-all")
async def start_storyboard_all(project_id: str):
    """为本项目所有【待分镜】(planned) 剧集批量生成分镜，限并发逐集触发。
    必须是 async def：sync 路由跑在无事件循环的线程池里，asyncio.create_task 会抛
    'no running event loop'，导致状态已置为 scripting 但任务从未启动（前端显示分镜中、模型却收不到请求）。
    同时回收状态卡在 scripting 但无在跑任务的孤儿集，便于一键修复。"""
    _project_or_404(project_id)
    _require_harness_engine(project_id)
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, status, screenplay_status, screenplay_json FROM episodes WHERE project_id=? AND status IN ('planned','scripting','script_failed') ORDER BY episode_no",
        (project_id,)).fetchall()
    # 待分镜的；以及卡在“分镜中”却没有在跑任务的孤儿（需重新触发）
    ids = [
        r["id"] for r in rows
        if r["screenplay_status"] == "ready" and r["screenplay_json"]
        and (r["status"] in ("planned", "script_failed")
             or not task_registry.active("storyboard", r["id"]))
    ]
    if not ids:
        raise HTTPException(409, "没有可展开分镜的剧集（需先生成剧本，且状态为待分镜/分镜失败/卡住的分镜中）")
    placeholders = ",".join("?" for _ in ids)
    conn.execute(f"UPDATE episodes SET status='scripting', script_error=NULL WHERE id IN ({placeholders})", ids)
    conn.commit()
    sem = asyncio.Semaphore(max(int(get_setting("storyboard_concurrency") or 2), 1))
    run_ids: list[str] = []
    for eid in ids:
        recorder = _new_storyboard_recorder(eid, trigger_type="batch")
        run_ids.append(recorder.run_id)
        task_registry.spawn(
            "storyboard", eid,
            _storyboard_guarded_recorded(eid, sem, recorder),
            project_id=project_id,
        )
    return {"started": len(ids), "run_ids": run_ids}


async def _storyboard_guarded_recorded(
    episode_id: str, sem: asyncio.Semaphore, recorder: WorkflowRecorder
) -> None:
    async with sem:
        await _recorded_storyboard_task(episode_id, recorder, resume=True)


@router.post("/episodes/{episode_id}/storyboard/cancel")
async def cancel_storyboard(episode_id: str):
    """手动取消正在进行的分镜生成请求，解除 scripting 锁定，便于重新发起。
    用于模型侧卡死/异常导致状态长期停留在“分镜中”的情况。"""
    ep = _episode_or_404(episode_id)
    if ep["status"] != "scripting":
        raise HTTPException(409, "当前没有正在进行的分镜生成")
    await task_registry.cancel_and_wait("storyboard", episode_id)
    conn = get_conn()
    has_shots = conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,)).fetchone()["c"]
    # 取消时分镜必然未走到尾钩（is_final 后状态已是 scripted、不在 scripting）。已生成的镜头是
    # 半截分镜，不能冒充"待确认"完成态——置为 script_failed 并写清原因，保留已生成镜头供查看/续作。
    if has_shots:
        conn.execute(
            "UPDATE episodes SET status='script_failed', script_error=? WHERE id=?",
            (f"分镜生成已手动取消：已保留 {has_shots} 个逐镜 checkpoint，恢复时将从下一镜继续。", episode_id))
        conn.commit()
        return {"status": "script_failed", "shots": has_shots}
    conn.execute("UPDATE episodes SET status='planned', script_error=NULL WHERE id=?", (episode_id,))
    conn.commit()
    return {"status": "planned"}


async def _plan_one_shot(shot_row) -> dict:
    """返回固定参考图模式计划；不再调用 LLM 做模式选择。"""
    from app import video_modes
    return video_modes.decision_to_dict(video_modes.default_reference_decision())


async def _ensure_shot_mode_plan(conn, shot_id: str, *, force: bool = False) -> None:
    """生成前确保该镜已有固定参考图模式计划。

    旧版可能残留首/尾关键帧模式计划；这类计划不能因为字段非空就被复用，
    必须原地升级为固定参考图模式，保证所有新视频只走同一条链路。
    """
    shot_row = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot_row:
        return
    if not force and shot_row["mode_plan"]:
        try:
            existing = json.loads(shot_row["mode_plan"])
        except (TypeError, ValueError, json.JSONDecodeError):
            existing = None
        if isinstance(existing, dict) and existing.get("mode") == "REFERENCE_IMAGE_MODE":
            return
    plan_dict = await _plan_one_shot(shot_row)
    conn.execute("UPDATE shots SET mode_plan=? WHERE id=?",
                 (json.dumps(plan_dict, ensure_ascii=False), shot_id))
    conn.commit()


@router.get("/episodes/{episode_id}")
def episode_detail(episode_id: str):
    ep = dict(_episode_or_404(episode_id))
    conn = get_conn()
    ep["source_chapters"] = json.loads(ep["source_chapters"] or "[]")
    script = _load_screenplay(ep)
    ep["screenplay"] = script.model_dump() if script else None
    ep["screenplay_mode"] = _screenplay_mode(script)
    artifact_id = ep.get("screenplay_artifact_id")
    artifact = evidence_repository.get_artifact(artifact_id) if artifact_id else None
    if artifact:
        artifact.pop("content_json", None)
        artifact.pop("content", None)
        artifact["evaluations"] = evidence_repository.get_evaluations(artifact_id)
    ep["screenplay_evidence"] = artifact
    storyboard_artifact_id = ep.get("storyboard_artifact_id")
    storyboard_artifact = (
        evidence_repository.get_artifact(storyboard_artifact_id)
        if storyboard_artifact_id else None
    )
    if storyboard_artifact:
        storyboard_artifact.pop("content_json", None)
        storyboard_artifact.pop("content", None)
        storyboard_artifact["evaluations"] = evidence_repository.get_evaluations(
            storyboard_artifact_id
        )
    ep["storyboard_evidence"] = storyboard_artifact
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
    # 预估只按模型选择的实际分镜时长累计；单集不设总时长产品上限。
    ep["cost_cny"] = worker.episode_cost(episode_id)
    ep["cost_limit_cny"] = float(get_setting("episode_cost_limit_cny") or 100)
    shots = rows_to_dicts(shot_rows)
    from app.config import PROJECTS_DIR
    for s in shots:
        s["characters"] = json.loads(s["characters"] or "[]")
        s["dialogues"] = json.loads(s["dialogues"] or "[]")
        s["est_cost_cny"] = shot_cost_cny(s["duration_s"])
        if s.get("storyboard_artifact_id"):
            shot_artifact = evidence_repository.get_artifact(s["storyboard_artifact_id"])
            if shot_artifact:
                shot_artifact.pop("content_json", None)
                shot_artifact.pop("content", None)
                shot_artifact["evaluations"] = evidence_repository.get_evaluations(
                    s["storyboard_artifact_id"]
                )
            s["storyboard_evidence"] = shot_artifact
        else:
            s["storyboard_evidence"] = None
        # mode_plan 存的是 JSON 文本，解析成对象供前端只读展示模型决策
        try:
            s["mode_plan"] = json.loads(s["mode_plan"]) if s.get("mode_plan") else None
        except (TypeError, ValueError):
            s["mode_plan"] = None
        # 新链路只使用参考图；旧关键帧字段仅保留在数据库中做历史兼容，不再对外暴露或参与状态判断。
        for legacy_key in (
            "approved_scene_id", "approved_head_scene_id", "approved_tail_scene_id", "scene_status",
        ):
            s.pop(legacy_key, None)
        s["video_stale"] = False
        versions = rows_to_dicts(conn.execute(
            "SELECT * FROM shot_versions WHERE shot_id=? ORDER BY version_no DESC", (s["id"],)).fetchall())
        for v in versions:
            v["qa"] = json.loads(v["qa_json"]) if v["qa_json"] else None
            v.pop("qa_json", None)
            meta = json.loads(v.get("image_inputs") or "{}")
            refs = []
            for ref in meta.get("reference_images") or []:
                refs.append(_public_reference_image(ref))
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
                                 "reference_failure_logs": [_public_failure_log(x) for x in (meta.get("reference_failure_logs") or []) if isinstance(x, dict)],
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
    conn.commit()
    previous_artifact_id = shot["storyboard_artifact_id"]
    manual_artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="storyboard_shot",
        scope_type="storyboard_checkpoint",
        scope_id=f"{shot['episode_id']}:{shot['shot_no']}",
        status="validated",
        trust_level="T4",
        content=instance.model_dump(mode="json"),
        parent_artifact_ids=[previous_artifact_id] if previous_artifact_id else [],
        contract_version=get_contract("storyboard").version,
    ))
    manual_artifact = evidence_repository.commit_artifact(
        None,
        manual_artifact["id"],
        [Evaluation(
            evaluator_type="human",
            evaluator_name="storyboard_editor",
            evaluator_version="1.0.0",
            status="passed",
            hard_gate_passed=True,
            score=100,
            evidence={"decision": "manual_edit", "shot_id": shot_id},
        )],
    )
    conn.execute(
        "UPDATE shots SET storyboard_artifact_id=? WHERE id=?",
        (manual_artifact["id"], shot_id),
    )
    conn.commit()
    # 任一分镜字段都参与参考图或视频 prompt。保存后统一清理全部旧衍生产物，
    # 避免 done/generating 状态继续展示旧成片；剧集必须重新确认后才能花钱生成。
    invalidated = worker.clear_shot_artifacts(shot_id)
    conn.execute("UPDATE episodes SET status='scripted' WHERE id=?", (shot["episode_id"],))
    conn.commit()
    impact = evidence_repository.get_lineage(previous_artifact_id or manual_artifact["id"])
    return {
        "ok": True,
        "invalidated": invalidated,
        "artifact_id": manual_artifact["id"],
        "impact": {
            "stale_descendant_ids": [
                item["id"] for item in impact["descendants"] if item["status"] == "stale"
            ],
            "requires_reconfirm": True,
            "paid_media_invalidated": bool(invalidated),
        },
    }


def _storyboard_residual_hint(residual: list[str]) -> str:
    """Return an actionable repair hint for the current validation failures."""
    text = "；".join(residual)
    hints: list[str] = []
    if "口播上限" in text or "念不完" in text:
        hints.append("请拆成相邻镜头分担台词，或精简人群议论旁白")
    if "角色圣经中不存在" in text or "既不在角色圣经" in text or "圣经角色为" in text:
        hints.append("请在监制房把该角色补入角色圣经，或改由圣经角色完成该动作")
    if "未落实本镜大纲 covers" in text or "只停留在大纲" in text:
        hints.append("请在 action_desc/narration/dialogues 写出该事实，同义改写即可（如\"成绩\"可写成\"测出七段\"、\"追捧\"可写成\"赞叹欢呼\"）")
    if not hints:
        hints.append("请修改该镜后从下一镜继续，或点击「重新生成整版」")
    return "；".join(hints)


def _storyboard_loop_exit_text(exit_reason: str) -> str:
    """Translate the actual AgentLoop exit reason without misreporting exhaustion."""
    return {
        "max_iterations": "已达到重试上限",
        "no_quality_gain": "连续修复无质量提升，修复循环已停止",
        "stalled": "连续输出相同问题，修复循环已停止",
    }.get(exit_reason, "修复循环未通过")


def _board_from_shot_rows(rows, episode_no: int) -> Storyboard:
    """Restore a Storyboard from persisted shot rows for confirmation and validation."""
    shots = [Shot(
        shot_no=r["shot_no"], duration_s=r["duration_s"], shot_size=r["shot_size"], camera_move=r["camera_move"],
        scene_setting=r["scene_setting"], characters=json.loads(r["characters"] or "[]"),
        action_desc=r["action_desc"], first_frame_desc=r["first_frame_desc"] or "", last_frame_desc=r["last_frame_desc"] or "",
        source_excerpt=r["source_excerpt"] or "",
        narration=r["narration"], dialogues=json.loads(r["dialogues"] or "[]"),
        transition=r["transition"] or "硬切", continuity_from_prev=bool(r["continuity_from_prev"])) for r in rows]
    return Storyboard(episode_no=episode_no, shots=shots)


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
    character_changes = normalize_offbible_characters(board, bible)
    character_artifact_ids = _persist_storyboard_character_policy_repairs(
        conn, episode_id, board, character_changes
    )
    # 确认门同样跑确定性连贯归一，并把修正后的 continuity/transition 写回库，
    # 保证人工编辑过的分镜在进入生成前也满足"同场景接上镜/换场明确转场"的铁律。
    before = [(s.continuity_from_prev, s.transition, s.duration_s, s.shot_size, s.camera_move) for s in shots]
    normalize_continuity(board)
    # 确认门保留模型/人工选择的时长，由全量校验验证 5~10s 合同与对应口播预算。
    normalize_transition_visuals(board)
    normalized_fields_changed = False
    for r, s, (old_cont, old_trans, old_dur, old_size, old_move) in zip(shots_rows, shots, before):
        if (old_cont != s.continuity_from_prev or old_trans != s.transition or old_dur != s.duration_s
                or old_size != s.shot_size or old_move != s.camera_move
                or (r["last_frame_desc"] or "") != s.last_frame_desc):
            normalized_fields_changed = True
            conn.execute(
                "UPDATE shots SET continuity_from_prev=?, transition=?, duration_s=?, shot_size=?, camera_move=?, last_frame_desc=? WHERE id=?",
                (int(s.continuity_from_prev), s.transition, s.duration_s, s.shot_size, s.camera_move,
                 s.last_frame_desc, r["id"]))
    conn.commit()
    errors = validate_storyboard(board, bible, compact_target)
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
    storyboard_artifact_id = ep["storyboard_artifact_id"]
    if character_artifact_ids or normalized_fields_changed or not storyboard_artifact_id:
        # Any deterministic rewrite changes the adopted content hash. Rebuild the full-board
        # T2 artifact from current checkpoint parents before attaching the human T4 decision.
        storyboard_artifact_id = _finalize_storyboard_evidence(episode_id, board)
    if storyboard_artifact_id:
        human_eval = Evaluation(
            evaluator_type="human",
            evaluator_name="storyboard_reviewer",
            evaluator_version="1.0.0",
            status="passed",
            hard_gate_passed=True,
            score=100,
            evidence={"decision": "approve", "shot_count": len(shots)},
        )
        evidence_repository.commit_artifact(None, storyboard_artifact_id, [human_eval])
        conn.execute(
            """INSERT INTO gate_decisions(
                   id, artifact_id, gate_key, decision, decided_by, reason, created_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                new_id("gate"), storyboard_artifact_id, "storyboard", "approve", "user",
                "分镜全量确定性校验通过并人工确认", now(),
            ),
        )
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
    """运行确定性确认门；校验通过后把剧集推进到 confirmed。"""
    from app.capabilities.dispatch import dispatch, raise_if_failed, result_http_payload

    result = await dispatch("storyboard.confirm", {"episode_id": episode_id}, initiator="ui")
    raise_if_failed(result)
    return result_http_payload(result)


@router.post("/episodes/{episode_id}/clear-artifacts")
def clear_episode_artifacts(episode_id: str):
    """清空整集所有镜头的参考图、视频与模型分析，并回退到「已确认」。"""
    _episode_or_404(episode_id)
    return worker.clear_episode_artifacts(episode_id)


@router.post("/shots/{shot_id}/clear-artifacts")
def clear_shot_artifacts(shot_id: str):
    """清空单个镜头的参考图、视频与模型分析。"""
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


def _set_reference_image_used(
    version_id: str, ref_id: str, *, use: bool, override_reason: str | None = None,
) -> dict:
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
    if use and target.get("rejectReason") and not (override_reason or "").strip():
        raise HTTPException(400, "恢复质检淘汰的参考图必须填写覆盖理由")
    changed = target.get("deleted") != (not use) or target.get("selectedForSeedance") != use
    target["deleted"] = not use
    target["selectedForSeedance"] = use
    if use and (override_reason or "").strip():
        target["restoreOverrideReason"] = override_reason.strip()
        target["restoredAt"] = now()
        changed = True
    meta["reference_images"] = refs
    if changed:
        meta["reference_gallery_revision"] = now()
        meta["reference_gallery_edited"] = True
    conn.execute("UPDATE shot_versions SET image_inputs=? WHERE id=?",
                 (json.dumps(meta, ensure_ascii=False), version_id))
    conn.commit()
    return {
        "version_id": version_id,
        "ref_id": ref_id,
        "deleted": not use,
        "override_reason": (override_reason or "").strip() or None,
    }


@router.delete("/versions/{version_id}/reference-images/{ref_id}")
def discard_reference_image(version_id: str, ref_id: str):
    """废弃一张参考图：移入废弃画廊，且后续调用视频模型时不再使用它。"""
    return _set_reference_image_used(version_id, ref_id, use=False)


@router.post("/versions/{version_id}/reference-images/{ref_id}/restore")
def restore_reference_image(version_id: str, ref_id: str, body: dict | None = Body(None)):
    """把废弃画廊里的参考图恢复为可用（重新计入喂给视频模型的参考图）。
    若该图曾被 QA 淘汰，body.override_reason 必填，写入审计字段。"""
    body = body or {}
    return _set_reference_image_used(
        version_id, ref_id, use=True, override_reason=body.get("override_reason"),
    )


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
    # 不再预先清空 adopted_version_id：新版本成功并通过技术门禁后由
    # select_best_video_candidate 比较切换；任务失败时保留原可交付采用结果。
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
            # 幂等命中（已有相同成片）：若当前无采用版，把复用版标为采用
            if r.get("reused") and r.get("version_id"):
                row = conn.execute(
                    "SELECT adopted_version_id FROM shots WHERE id=?", (s["id"],)
                ).fetchone()
                if not row or not row["adopted_version_id"]:
                    conn.execute(
                        "UPDATE shots SET adopted_version_id=? WHERE id=?",
                        (r["version_id"], s["id"]),
                    )
            results.append({"shot_id": s["id"], **r})
        except Exception as exc:  # noqa: BLE001
            public = errors.record_and_format(exc, action="enqueue_shot",
                                              context={"shot_id": s["id"], "episode_id": episode_id})
            results.append({"shot_id": s["id"], "error": public})
    conn.commit()
    return {"enqueued": results}


async def _generate_shot_core(shot_id: str, body: dict) -> dict:
    """单镜生成视频的领域逻辑，供 REST 路由与 ``video.generate_shot`` Command Handler 共用。"""
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


@router.post("/shots/{shot_id}/generate")
async def generate_shot(shot_id: str, body: dict | None = None):
    from app.capabilities.dispatch import dispatch, raise_if_failed, result_http_payload

    body = body or {}
    result = await dispatch(
        "video.generate_shot",
        {
            "shot_id": shot_id,
            "prompt_override": body.get("prompt_override"),
            "reroll": bool(body.get("reroll")),
            "critique": "with_critique" if body.get("with_critique") else None,
        },
        initiator="ui",
    )
    raise_if_failed(result)
    return result_http_payload(result)


@router.post("/shots/{shot_id}/video/stop")
def stop_shot_video(shot_id: str):
    """立即停止本镜全部排队中或运行中的视频任务；重复调用安全。"""
    try:
        return worker.stop_shot_video_tasks(shot_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


def _adopt_version_core(shot_id: str, body: dict) -> dict:
    """人工采用视频版本的领域逻辑，供 REST 路由与 ``video.adopt_version`` Command Handler 共用。"""
    version_id = body.get("version_id")
    conn = get_conn()
    v = conn.execute("SELECT * FROM shot_versions WHERE id=? AND shot_id=?", (version_id, shot_id)).fetchone()
    if not v or v["status"] != "succeeded":
        raise HTTPException(409, "该版本不存在或未成功")
    from app.evidence import media as media_evidence

    try:
        artifact = media_evidence.record_video_candidate(version_id)
    except (OSError, ValueError) as exc:
        raise HTTPException(409, f"候选证据创建失败：{exc}") from exc
    technical = json.loads(v["technical_validation_json"] or "{}")
    if not technical:
        refreshed = conn.execute(
            "SELECT technical_validation_json FROM shot_versions WHERE id=?", (version_id,)
        ).fetchone()
        technical = json.loads(refreshed["technical_validation_json"] or "{}")
    if not technical.get("passed"):
        raise HTTPException(409, "视频技术门禁未通过，不能人工采用")
    reason = str(body.get("reason") or "人工横向比较后采用").strip()
    evidence_repository.commit_artifact(
        None,
        artifact["id"],
        [Evaluation(
            evaluator_type="human", evaluator_name=str(body.get("decided_by") or "user"),
            evaluator_version="1.0.0", status="passed", hard_gate_passed=True,
            score=100, evidence={"decision": "adopt", "reason": reason},
        )],
    )
    shot = conn.execute("SELECT episode_id, adopted_version_id FROM shots WHERE id=?", (shot_id,)).fetchone()
    conn.execute("UPDATE shots SET adopted_version_id=? WHERE id=?", (version_id, shot_id))
    conn.execute("UPDATE shot_versions SET adoption_reason=? WHERE id=?", (reason, version_id))
    conn.execute(
        """INSERT INTO gate_decisions(
               id, artifact_id, gate_key, decision, decided_by, reason, created_at
           ) VALUES(?,?,?,?,?,?,?)""",
        (
            new_id("gate"), artifact["id"], "video_adoption", "approve",
            str(body.get("decided_by") or "user"), reason, now(),
        ),
    )
    conn.commit()
    if shot and shot["adopted_version_id"] != version_id:
        worker.invalidate_episode_final(shot["episode_id"])
    return {"adopted": version_id, "artifact_id": artifact["id"], "reason": reason}


@router.post("/shots/{shot_id}/adopt")
async def adopt_version(shot_id: str, body: dict):
    from app.capabilities.dispatch import dispatch, raise_if_failed, result_http_payload

    result = await dispatch(
        "video.adopt_version",
        {"shot_id": shot_id, "version_id": body.get("version_id"), "reason": body.get("reason")},
        initiator="ui",
    )
    raise_if_failed(result)
    return result_http_payload(result)


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
