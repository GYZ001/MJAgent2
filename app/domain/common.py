"""REST API 共享导言。

后续 domain 切片通过 ``exec`` 注入同一命名空间，因此这里的 import 看似未使用，
实际供 projects/bible/storyboard 等切片复用。勿用 ruff 自动删 import。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
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


def _normalize_required_dialogue_lines(value) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise HTTPException(422, "必保留原文台词必须按行提交")
    from app.textmatch import condense, strip_speaker

    lines: list[str] = []
    seen: set[str] = set()
    for raw in value:
        line = str(raw or "").strip().lstrip("-• ").strip()
        if not line:
            continue
        content = condense(strip_speaker(line))
        if len(content) < 2:
            raise HTTPException(422, f"必保留原文台词过短：{line}")
        if len(line) > 160:
            raise HTTPException(422, f"单条必保留原文台词不能超过 160 字：{line[:30]}…")
        if content in seen:
            continue
        seen.add(content)
        lines.append(line)
    return lines


def _screenplay_required_dialogues(ep) -> list[str]:
    try:
        raw = ep["screenplay_required_dialogues"] or "[]"
    except (KeyError, IndexError, TypeError):
        return []
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
        return _normalize_required_dialogue_lines(value)
    except (json.JSONDecodeError, HTTPException):
        return []


def _as_body_dict(body) -> dict:
    """FastAPI ``Body(None)`` 在直接调用时会把默认值变成 Body 对象，不能当 dict 展开。"""
    return body if isinstance(body, dict) else {}

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

def _project_or_404(project_id: str) -> dict:
    conn = get_conn()
    row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"项目不存在：{project_id}")
    # sqlite3.Row supports item access but not Mapping.get().  Project callers
    # use both styles, so normalize once at the shared boundary instead of
    # leaving individual endpoints vulnerable to AttributeError -> HTTP 500.
    return dict(_recover_orphan_bible_row(conn, row))


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


def _storyboard_target_for_source(target_duration_s: int | None, source_chars: int,
                                  *, spine_beat_count: int | None = None) -> int:
    """Renderability：集时长跟主线走，不再因原文很长就抬高目标秒数。"""
    _ = source_chars  # 保留参数兼容旧调用
    if spine_beat_count is not None and spine_beat_count > 0:
        from app.renderability import episode_target_from_spine
        return episode_target_from_spine(spine_beat_count)
    return _compact_episode_target(target_duration_s)


def _episode_source_text(conn, ep) -> str:
    raw_source_chapters = ep["source_chapters"] or []
    source_chapters = (
        json.loads(raw_source_chapters)
        if isinstance(raw_source_chapters, str)
        else list(raw_source_chapters)
    )
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
            "UPDATE episodes SET screenplay_json=NULL, screenplay_character_resolutions='[]', "
            "screenplay_status='pending', screenplay_error=?, status='planned', script_error=NULL WHERE id=?",
            (LEGACY_SCREENPLAY_PURGED_ERROR, ep["id"]),
        )
        purged += 1
    conn.commit()
    return purged


def _screenplay_ready(ep) -> bool:
    """仅带正式投影的 ready 剧本可进分镜。"""
    data = dict(ep)
    status = data.get("screenplay_status")
    if status in {"repairing", "running", "failed", "pending"}:
        return False
    screenplay_json = data.get("screenplay_json")
    if not (screenplay_json and status == "ready"):
        return False
    try:
        script = EpisodeScreenplay.model_validate(json.loads(screenplay_json))
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    if not (script.full_script_text or "").strip():
        return False
    # Any modern published chain is resolved as one immutable authority even
    # when the mutable projection claims ``narrative_plan=null``.  This closes
    # the downgrade path where stripping the plan could otherwise enter the
    # historical compatibility branch.  Truly historical plan-null rows have
    # none of these publication fields and keep the legacy behavior below.
    # A published-artifact pointer predates the narrative authority chain and
    # therefore cannot, by itself, distinguish a legacy episode.  Durable
    # authority evidence does: a production revision/certificate or narrative
    # review is only created by the modern release path.  If any such evidence
    # survives while the mutable projection loses its plan, resolve fail-closed
    # instead of letting that mutation downgrade into legacy compatibility.
    has_modern_authority = any(
        data.get(field)
        for field in (
            "screenplay_completion_certificate_id",
            "screenplay_production_revision_id",
            "narrative_review_artifact_id",
            "narrative_calibration_artifact_id",
        )
    )
    published_script = None
    published_artifact = None
    if has_modern_authority:
        current_artifact_id = str(data.get("screenplay_artifact_id") or "")
        published_artifact_id = str(
            data.get("published_screenplay_artifact_id") or ""
        )
        if not current_artifact_id or current_artifact_id != published_artifact_id:
            return False
        published_artifact = evidence_repository.get_artifact(published_artifact_id)
        if (
            published_artifact is None
            or published_artifact.get("type") != "screenplay_document"
            or published_artifact.get("scope_type") != "episode"
            or published_artifact.get("scope_id") != str(data.get("id") or "")
            or published_artifact.get("status") != "approved"
        ):
            return False
        try:
            from app.production.patch import load_screenplay_from_artifact

            published_script = load_screenplay_from_artifact(published_artifact_id)
            current_hash = evidence_repository.content_hash(
                published_artifact.get("content"),
                published_artifact.get("file_path"),
            )
        except Exception:  # noqa: BLE001 - readiness is fail closed
            return False
        if (
            current_hash != str(published_artifact.get("content_hash") or "")
            or published_script.model_dump(mode="json")
            != script.model_dump(mode="json")
        ):
            return False

    if (
        script.narrative_plan is not None
        or (
            published_script is not None
            and published_script.narrative_plan is not None
        )
    ):
        try:
            from app.production.screenplay_authority import (
                resolve_current_screenplay_authority,
            )

            resolved = resolve_current_screenplay_authority(
                str(data.get("id") or ""),
                require_narrative=True,
            )
            return resolved.screenplay.model_dump(mode="json") == script.model_dump(mode="json")
        except Exception:  # noqa: BLE001 - readiness is a fail-closed predicate
            return False
    if has_modern_authority:
        try:
            from app.production.certificate import verify_completion_certificate

            cert = verify_completion_certificate(
                str(data.get("screenplay_completion_certificate_id") or ""),
                expected_kind="screenplay",
                expected_scope_id=str(data.get("id") or ""),
                expected_artifact_id=str(data.get("screenplay_artifact_id") or ""),
                expected_artifact_hash=str(
                    (published_artifact or {}).get("content_hash") or ""
                ),
                expected_production_revision_id=str(
                    data.get("screenplay_production_revision_id") or ""
                ),
                allow_consumed=True,
            )
            if cert.consumed_at is None:
                return False
            revision = get_conn().execute(
                "SELECT kind,episode_id,status,working_artifact_id,published_artifact_id "
                "FROM production_revisions WHERE id=?",
                (str(data.get("screenplay_production_revision_id") or ""),),
            ).fetchone()
            return bool(
                revision
                and revision["kind"] == "screenplay"
                and revision["episode_id"] == data.get("id")
                and revision["status"] == "published"
                and revision["working_artifact_id"]
                == data.get("screenplay_artifact_id")
                and revision["published_artifact_id"]
                == data.get("screenplay_artifact_id")
            )
        except Exception:  # noqa: BLE001 - compatibility still fails closed on drift
            return False
    # 新发布链必须持有与当前 Artifact 精确绑定且已消费的完成凭证。
    # 无 revision 的历史发布版保留兼容读取，迁移后自然进入新合同。
    revision_id = data.get("screenplay_production_revision_id")
    if not revision_id:
        return True
    certificate_id = data.get("screenplay_completion_certificate_id")
    artifact_id = data.get("screenplay_artifact_id")
    if not certificate_id or not artifact_id:
        return False
    try:
        row = get_conn().execute(
            """SELECT kind,scope_id,artifact_id,blockers,must_fix_issues,consumed_at,
                      production_revision_id
                 FROM completion_certificates WHERE id=?""",
            (certificate_id,),
        ).fetchone()
    except Exception:  # noqa: BLE001
        return False
    return bool(
        row
        and row["kind"] == "screenplay"
        and row["scope_id"] == data.get("id")
        and row["artifact_id"] == artifact_id
        and row["production_revision_id"] == revision_id
        and int(row["blockers"] or 0) == 0
        and int(row["must_fix_issues"] or 0) == 0
        and row["consumed_at"] is not None
    )

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
        "entity_type": ref.get("entity_type"),
        "entity_name": ref.get("entity_name"),
        "library_revision_id": ref.get("library_revision_id"),
        "library_view_id": ref.get("library_view_id"),
        "view_role": ref.get("view_role"),
        "purposes": ref.get("purposes"),
        "required": bool(ref.get("required")),
        "slot_key": ref.get("slot_key"),
        "keyframe_index": ref.get("keyframe_index") or ((ref.get("qa") or {}).get("keyframe_beat") or {}).get("beat_index"),
        "keyframe_total": ref.get("keyframe_total") or ((ref.get("qa") or {}).get("keyframe_beat") or {}).get("beat_total"),
        "keyframe_time_ratio": (
            ref.get("keyframe_time_ratio")
            if ref.get("keyframe_time_ratio") is not None
            else ((ref.get("qa") or {}).get("keyframe_beat") or {}).get("time_ratio")
        ),
        "keyframe_target_desc": (
            ref.get("keyframe_target_desc") or ((ref.get("qa") or {}).get("keyframe_beat") or {}).get("target_desc")
        ),
        "dependency_manifest": ref.get("dependency_manifest"),
        "gate_status": ref.get("gate_status"),
        "downstream_eligibility": ref.get("downstream_eligibility"),
        "rule_version": ref.get("rule_version") or (ref.get("qa") or {}).get("rule_version"),
        "hard_failures": ref.get("hard_failures") or (ref.get("qa") or {}).get("hard_failures") or [],
        "soft_warnings": ref.get("soft_warnings") or [],
        "referenced_by_version_ids": ref.get("referenced_by_version_ids") or [],
        "selection_reason": ref.get("selection_reason"),
        "restoreOverrideReason": ref.get("restoreOverrideReason"),
    }


def _public_failure_log(log: dict) -> dict:
    """参考图失败日志对外表示：剥掉嵌套 reference_images 里的 base64，只留轻量元信息。"""
    out = {k: v for k, v in log.items() if k != "reference_images"}
    nested = log.get("reference_images")
    if isinstance(nested, list) and nested:
        out["reference_images"] = [_public_reference_image(r) for r in nested if isinstance(r, dict)]
    return out

__all__ = [name for name in globals() if not name.startswith("__")]
