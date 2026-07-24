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

__all__ = [name for name in globals() if not name.startswith("__")]
