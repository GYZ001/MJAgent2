"""REST API 共享导言。

后续 domain 切片通过 ``exec`` 注入同一命名空间，因此这里的 import 看似未使用，
实际供 projects/bible/storyboard 等切片复用。勿用 ruff 自动删 import。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import time
from collections import OrderedDict
from pathlib import Path
from threading import RLock

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile
from pydantic import ValidationError

from app import config, errors, task_registry, worker
from app.compiler import clip_duration_value, compile_prompt, shot_cost_cny
from app.db import get_conn, get_setting, log_provider_call, new_id, now, rows_to_dicts
from app.evidence import repository as evidence_repository
from app.harness.contracts import get_contract
from app.harness.types import Evaluation, EvidenceArtifact
from app.harness.context import ContextPack
from app.ingest import chapter_is_stub, chapter_titles_match, ingest_novel
from app.media_urls import build_media_url
from app.novel_formats import (
    SUPPORTED_NOVEL_LABEL,
    novel_file_suffix,
    prepare_novel_bytes,
    validate_novel_filename,
)
from app.orchestration.engine import WorkflowRecorder, fingerprint
from app.planning import chapter_preview
from app.schemas import (Bible, EpisodeScreenplay, Shot, Storyboard,
                         StoryboardOutline, StoryboardOutlineShot, schema_errors,
                         character_is_portrait_eligible)
from app.stages import (SCREENPLAY_SOURCE_BUDGET_CHARS, StageError, generate_bible)
from app.validators import (relieve_spoken_overflow,
                            normalize_action_desc, normalize_continuity,
                            normalize_offbible_characters,
                            normalize_transition_visuals,
                            storyboard_shot_count_range,
                            validate_screenplay, validate_storyboard,
                            validate_storyboard_preserves_key_content,
                            validate_storyboard_soundtrack)

router = APIRouter(prefix="/api")
_LOGGER = logging.getLogger(__name__)

BIBLE_TASK_TIMEOUT_S = 15 * 60
BIBLE_INTERRUPTED_ERROR = "人物谱任务已中断（服务重载或后台任务丢失），请重新谱写。"
FALLBACK_VISUAL_STYLE = "国漫风格，非真人CG渲染，统一电影感光影，暖灰色调"


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


def _refs_task_active(project_id: str) -> bool:
    return task_registry.active("refs", project_id)

def _scene_refs_task_active(project_id: str) -> bool:
    """Whether the paid image-generation phase itself is active."""
    return task_registry.active("scene_refs", project_id)


def _scene_assets_task_active(project_id: str) -> bool:
    """Whether either phase of the scene-asset pipeline is active."""
    return _scene_refs_task_active(project_id) or task_registry.active("scene_bible", project_id)

def _principal_access_check(
    owner_user_id: str | None, *, object_type: str, object_id: str | None,
) -> bool:
    """账号级隔离的唯一判据——``_assert_principal_owns``（抛 404）与
    ``owned_project_row``/``owned_episode_row``/``owned_shot_row``（返回
    ``None``）共用同一份判断，不允许两边各自维护一份（会漂移）。

    ``principal is None`` 视为未挂会话闸门的内部调用，与全仓既有约定一致，
    直接放行（后台任务、CLI、尚未注入 Principal 的既有测试）。

    系统管理员访问非本人账号的对象是被允许的设计（``Principal.owns`` 对管理员
    恒真），但跨账号访问必须留痕（P0-2）：判据只挂在「principal 是管理员 且
    目标 owner 不是他本人」这一个事实上，不挂路由白名单——任何新端点只要经过
    ``_project_or_404``/``_episode_or_404``/``owned_*_row`` 就自动被审计，
    不需要单独接线。同账号访问（绝大多数请求）在下面第一个分支就短路返回，
    不做任何 DB 读写，热路径零额外开销；只有「是管理员」与「owner 不是自己」
    同时成立时才会打一次独立连接的审计写入（见 ``app.db.insert_monitor_audit``
    的 docstring：诊断类写入不得提交调用方尚未提交的事务）。
    """
    from app.auth.principal import get_current_principal

    principal = get_current_principal()
    if principal is None:
        return True
    if not principal.owns(owner_user_id):
        return False
    if principal.is_system_admin and owner_user_id != principal.user_id:
        from app.db import insert_monitor_audit

        insert_monitor_audit(
            action="admin_cross_account_access",
            object_type=object_type,
            object_id=object_id or owner_user_id or "unknown",
            outcome="ok",
            detail={
                "admin_user_id": principal.user_id,
                "target_owner_user_id": owner_user_id,
            },
        )
    return True


def _assert_principal_owns(
    owner_user_id: str | None,
    *,
    not_found_detail: str,
    object_type: str = "project",
    object_id: str | None = None,
) -> None:
    """账号级项目隔离的第二道闸门（domain 层，独立于 HTTP 边界）。

    ``app.authz.resolve.require_project_owner_access`` 只在请求经 ASGI 路由、
    命中已挂闸门的路由组时才会执行——直接调用 domain 函数（Agent/MCP 工具、
    内部脚本、测试）完全绕过它。这里在 ``_project_or_404``/``_episode_or_404``
    这两个"整个 domain 包几乎唯一的项目/剧集存在性入口"上重复同一条判据，
    使得任何新端点即便忘了在路由上挂 ``require_project_owner_access``，只要
    它（几乎必然）调用这两个函数之一，归属校验依然会执行——结构上拿不到，
    不依赖每个端点作者记得挂鉴权。

    统一 404 而非 403：不让外部区分"对象不存在"与"对象存在但你无权"，与
    HTTP 边界的既有口径一致。判据本体（含管理员跨账号访问审计）见
    ``_principal_access_check``；``object_type``/``object_id`` 只影响审计行
    怎么标注被访问的对象，不影响放行/拒绝结果，默认值保证既有调用方
    （未传这两个新参数）行为不变。
    """
    if not _principal_access_check(
        owner_user_id, object_type=object_type, object_id=object_id,
    ):
        raise HTTPException(404, not_found_detail)


def owned_project_row(project_id: str) -> dict | None:
    """裸 SQL 查 ``projects`` 的统一入口（P0-1）。

    ``existence`` 与 ``ownership`` 折叠进同一个 ``None`` 分支，供不便直接抛
    ``HTTPException`` 的调用方（Command Bus 的 ``preflight`` 构造器等域内读
    函数）复用同一条判据，而不是各自重新实现一份「只查 not found、漏查
    owner」的裸 SQL——那类调用方（``app/capabilities/preflight.py`` 的
    ``PREFLIGHT_MAP`` 全部 17 个函数）是 Command Bus 在 HTTP 边界的
    ``require_project_owner_access`` 之前就会执行的入口：Agent/MCP 工具调用
    把 project_id 放在命令参数体里而不是 URL 路径参数，结构上没有任何上游
    校验过它。语义与 ``_project_or_404`` 完全一致（软删除项目同样视为不
    存在），区别只是返回 ``None`` 而不是抛异常。
    """
    conn = get_conn()
    # deleted_at IS NULL：软删除的项目已进回收站，对全部常规读写路径一律
    # 视为不存在——这是整个 domain 包唯一的项目存在性入口，几乎每个
    # bible/screenplay/storyboard/video 端点都先过这一道，回收站里的项目
    # 因此天然拿不到任何新操作（恢复/彻底清理走各自专用查询，不经过这里）。
    row = conn.execute(
        "SELECT * FROM projects WHERE id=? AND deleted_at IS NULL", (project_id,)
    ).fetchone()
    if not row:
        return None
    if not _principal_access_check(
        row["owner_user_id"], object_type="project", object_id=project_id,
    ):
        return None
    # sqlite3.Row supports item access but not Mapping.get().  Project callers
    # use both styles, so normalize once at the shared boundary instead of
    # leaving individual endpoints vulnerable to AttributeError -> HTTP 500.
    return dict(_recover_orphan_bible_row(conn, row))


def _project_or_404(project_id: str) -> dict:
    row = owned_project_row(project_id)
    if row is None:
        raise HTTPException(404, f"项目不存在：{project_id}")
    return row


def _require_harness_engine(project_id: str) -> None:
    row = get_conn().execute(
        "SELECT harness_engine_enabled FROM projects WHERE id=?", (project_id,)
    ).fetchone()
    if row and not bool(row["harness_engine_enabled"]):
        raise HTTPException(409, "该项目的 Harness Engine 已由灰度开关隔离；请重新开启后再启动新任务")


def owned_episode_row(episode_id: str):
    """同 ``owned_project_row``，剧集版本；owner 取其所属项目。"""
    conn = get_conn()
    row = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not row:
        return None
    owner_row = conn.execute(
        "SELECT owner_user_id FROM projects WHERE id=?", (row["project_id"],)
    ).fetchone()
    if not _principal_access_check(
        owner_row["owner_user_id"] if owner_row else None,
        object_type="episode", object_id=episode_id,
    ):
        return None
    return row


def owned_shot_row(shot_id: str):
    """同 ``owned_project_row``，镜头版本；owner 沿 shot -> episode -> project 取。"""
    conn = get_conn()
    row = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not row:
        return None
    ep_row = conn.execute(
        "SELECT project_id FROM episodes WHERE id=?", (row["episode_id"],)
    ).fetchone()
    owner_row = conn.execute(
        "SELECT owner_user_id FROM projects WHERE id=?",
        (ep_row["project_id"] if ep_row else None,),
    ).fetchone()
    if not _principal_access_check(
        owner_row["owner_user_id"] if owner_row else None,
        object_type="shot", object_id=shot_id,
    ):
        return None
    return row


def _episode_or_404(episode_id: str):
    row = owned_episode_row(episode_id)
    if row is None:
        raise HTTPException(404, f"剧集不存在：{episode_id}")
    return row


def _compact_episode_target(target_duration_s: int | None) -> int:
    if target_duration_s is None:
        return config.EPISODE_TARGET_DEFAULT_S
    target = max(int(target_duration_s), config.EPISODE_TARGET_MIN_S)
    step = config.EPISODE_TARGET_STEP_S
    rounded = ((target + step // 2) // step) * step
    return max(config.EPISODE_TARGET_MIN_S, rounded)


def _storyboard_target_for_source(target_duration_s: int | None, source_chars: int,
                                  *, spine_beat_count: int | None = None) -> int:
    """Return a lower-bound duration without imposing a product maximum."""
    _ = source_chars
    if spine_beat_count is not None and spine_beat_count > 0:
        from app.renderability import episode_target_from_spine
        return max(
            _compact_episode_target(target_duration_s),
            episode_target_from_spine(spine_beat_count),
        )
    return _compact_episode_target(target_duration_s)


def _compact_target_columns(compact_target: int) -> dict[str, object]:
    """Every episode column rewritten when re-baselining to a compact target.

    Single source of truth for the compact-target write: callers use this same
    dict both for the ``UPDATE episodes`` statement and to refresh the in-memory
    episode snapshot, so the snapshot can never drift from the row on disk.
    """
    return {
        "target_duration_s": compact_target,
        "planning_target_duration_s": compact_target,
        "planning_duration_source": "screenplay_source_capacity_estimate",
        "target_duration_authority": "planning_estimate",
    }


def _apply_compact_target(conn, episode_id: str, ep_data: dict, compact_target: int) -> None:
    """Persist the compact target and mirror every written column into ``ep_data``.

    ``ep_data`` is the in-memory episode snapshot passed through the
    screenplay-generation pipeline and read downstream as
    ``episode.get("planning_target_duration_s")`` for the
    duration-expansion CAS.  Writing the DB and the snapshot from one
    ``_compact_target_columns`` dict guarantees "whatever was written is synced",
    so the CAS can never see a stale non-rounded planning value.
    """
    compact_columns = _compact_target_columns(compact_target)
    conn.execute(
        """UPDATE episodes
              SET target_duration_s=:target_duration_s,
                  planning_target_duration_s=:planning_target_duration_s,
                  planning_duration_source=:planning_duration_source,
                  target_duration_authority=:target_duration_authority
            WHERE id=:episode_id""",
        {**compact_columns, "episode_id": episode_id},
    )
    conn.commit()
    ep_data.update(compact_columns)


def _episode_chapters(conn, ep) -> list[dict]:
    """本集源章节行（stub 修复后），供 `_episode_source_text` 和 paratext
    偏移换算（`app.production.prep_pack`）共用——"读哪些章、按什么顺序"
    只能有一份实现，两处各写一份会产生漂移风险（见
    logs/paratext_single_source_plan.md）。返回的每行是 `SELECT *`，含
    `id`/`title`/`content`/`paratext_json` 等全部列。
    """
    raw_source_chapters = ep["source_chapters"] or []
    source_chapters = (
        json.loads(raw_source_chapters)
        if isinstance(raw_source_chapters, str)
        else list(raw_source_chapters)
    )
    if not source_chapters:
        return []
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
    return chapters


def _episode_source_blocks(chapters: list[dict]) -> tuple[str, list[int]]:
    """章节行 -> 集源文本 + 每章 `content` 在这段文本里的绝对起点。

    唯一的拼接实现——集源文本本身和"把 chapters.paratext_json 里以章为
    单位的偏移平移到集级坐标"（`app.production.prep_pack`）都调用这一份，
    禁止另起一份公式，否则又是"两处判据各自实现导致漂移"（见
    logs/paratext_single_source_plan.md）。`offsets[i]` = `chapters[i]`
    的 `content` 在返回文本里的起始下标（紧跟在 `【title】\\n` 前缀之后）。
    """
    parts: list[str] = []
    offsets: list[int] = []
    cursor = 0
    for index, ch in enumerate(chapters):
        if index > 0:
            parts.append("\n\n")
            cursor += 2
        prefix = f"【{ch['title']}】\n"
        parts.append(prefix)
        cursor += len(prefix)
        offsets.append(cursor)
        content = ch["content"]
        parts.append(content)
        cursor += len(content)
    return "".join(parts), offsets


def _episode_source_text(conn, ep) -> str:
    text, _content_offsets = _episode_source_blocks(_episode_chapters(conn, ep))
    return text


def _load_screenplay(ep) -> EpisodeScreenplay | None:
    from app.production.screenplay_authority import (
        published_stale_screenplay_rebuild_error,
    )

    rebuild_error = published_stale_screenplay_rebuild_error(ep)
    if rebuild_error is not None:
        raise rebuild_error
    if not ep["screenplay_json"]:
        return None
    payload = json.loads(ep["screenplay_json"])
    if isinstance(payload, dict) and "prep_pack_version" in payload:
        # episode_prep_pack (screenplay contract 6.0.0+,
        # docs/TRANSFORM_FREEZE_PLAN.md) is a structurally different artifact
        # from the legacy EpisodeScreenplay projection this function loads --
        # callers built for the legacy shape must see "no legacy projection"
        # rather than a validation crash. Callers that need the new shape use
        # episode_prep_pack_payload() below.
        return None
    return EpisodeScreenplay.model_validate(payload)


def episode_prep_pack_payload(ep) -> dict | None:
    """Return the raw episode_prep_pack payload if this episode's current
    screenplay_json holds one (screenplay contract 6.0.0+); otherwise None.
    Counterpart to _load_screenplay()'s legacy-shape branch above.
    """
    raw = ep["screenplay_json"]
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if isinstance(payload, dict) and "prep_pack_version" in payload:
        return payload
    return None


# 映射台工作区既不展示也不编辑这些字段：它们由生成管线撰写，是叙事权威的一部分。
# 让页面下载 1.6 MB 的叙事蓝图再原样传回来，一是纯浪费（实测占 view=script 响应体
# 的 85%，并让草稿自动保存每次都要 JSON.stringify 一份接近 localStorage 配额的对象），
# 二是把「客户端回声」变成权威内容的一条写路径 —— 前端任何一次裁剪或序列化差异
# 都会静默改写权威。工作区读不到它们，写回时由服务端从当前权威补齐。
SCREENPLAY_WORKSPACE_WITHHELD_FIELDS = ("narrative_plan",)


def screenplay_workspace_projection(payload):
    """Strip the pipeline-authored fields the screenplay workspace never uses."""
    if not isinstance(payload, dict):
        return payload
    return {
        key: value
        for key, value in payload.items()
        if key not in SCREENPLAY_WORKSPACE_WITHHELD_FIELDS
    }


def merge_withheld_screenplay_fields(payload, *, authority):
    """Restore withheld authority fields the workspace was never given.

    A field the client never received cannot be an intentional deletion, so an
    absent key inherits the current authority value.  A key the client did send
    (an older local draft, an API consumer) is honoured unchanged.
    """
    if not isinstance(payload, dict) or authority is None:
        return payload
    missing = [
        field
        for field in SCREENPLAY_WORKSPACE_WITHHELD_FIELDS
        if field not in payload
    ]
    if not missing:
        return payload
    authority_dump = (
        authority.model_dump(mode="json")
        if hasattr(authority, "model_dump")
        else dict(authority)
    )
    merged = dict(payload)
    for field in missing:
        merged[field] = authority_dump.get(field)
    return merged


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
            parsed = json.loads(ep["screenplay_json"])
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(parsed, dict) and "prep_pack_version" in parsed:
            # episode_prep_pack (screenplay contract 6.0.0+, see
            # app.production.prep_pack) is not the legacy EpisodeScreenplay
            # shape this startup sweep targets. EpisodeScreenplay.model_validate
            # does not raise on it (unknown keys are ignored and
            # full_script_text defaults to empty), so without this guard the
            # sweep would silently wipe a freshly-published, fully valid
            # prep_pack artifact on every process restart -- caught via a real
            # EP1 run going from screenplay_status='ready' back to 'pending'
            # with this function's own LEGACY_SCREENPLAY_PURGED_ERROR message
            # after an unrelated backend restart.
            continue
        try:
            script = EpisodeScreenplay.model_validate(parsed)
        except (TypeError, ValueError):
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


# ``_screenplay_ready`` 是一个对不可变已发布权威链的完整重验证：重解析 2 MB
# Artifact、重编译 IR、重验完成凭证、逐字段比对投影。实测一次 ~1.9 s，而映射台
# 每 15 s 轮询一次、每次打开页面还要再跑两遍（详情 + 轻量状态）。
#
# 它同时是一个**纯函数**：结论只由本集的 episodes/projects 行、原文章节、
# 本集与本项目名下的全部 Artifact（含 status 与 content_hash）、完成凭证、
# production revision、以及绑定在这些 Artifact 上的 evaluation 决定。
# 因此这里按「这些输入的完整内容指纹」缓存结论：任何一个输入变化都会改变键，
# 缓存命中与重算在语义上完全等价，fail-closed 语义不受影响。
# 键的完整性由 tests/test_screenplay_ready_identity.py 逐类输入锁死。
_SCREENPLAY_READY_CACHE: "OrderedDict[str, bool]" = OrderedDict()
_SCREENPLAY_READY_CACHE_SIZE = 64
_SCREENPLAY_READY_CACHE_LOCK = RLock()


def _rows_or_empty(conn, sql: str, params: tuple) -> list:
    """Query a lazily-created table; a missing table means "no rows yet"."""
    import sqlite3 as _sqlite3

    try:
        return conn.execute(sql, params).fetchall()
    except _sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise


def _digest(*parts: object) -> str:
    return hashlib.blake2b(
        json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str).encode(
            "utf-8"
        ),
        digest_size=20,
    ).hexdigest()


def screenplay_ready_identity(data: dict) -> str:
    """Content identity of everything ``_screenplay_ready`` can read."""
    from app.production.screenplay_authority import SCREENPLAY_QA_PROFILE_VERSION
    from app.schemas import NARRATIVE_CONTRACT_VERSION

    episode_id = str(data.get("id") or "")
    project_id = str(data.get("project_id") or "")
    conn = get_conn()
    project_row = conn.execute(
        "SELECT * FROM projects WHERE id=?", (project_id,)
    ).fetchone()
    # 只有本集实际读到的章节才影响结论：`_source_records` 取 `source_chapters`，
    # 并在首章是占位存根时回退到紧邻的下一章。整本小说有上千章，全量哈希一次要
    # 130 ms，比它保护的判定本身还贵。
    try:
        source_indexes = [
            int(value) for value in json.loads(data.get("source_chapters") or "[]")
        ]
    except (TypeError, ValueError, json.JSONDecodeError):
        source_indexes = []
    chapters: list = []
    if source_indexes:
        chapters = _rows_or_empty(
            conn,
            "SELECT idx, title, char_count, content FROM chapters "
            "WHERE project_id=? AND idx IN "
            f"({','.join('?' for _ in source_indexes)}) ORDER BY idx",
            (project_id, *source_indexes),
        )
        # 存根回退读的是「下一条**存在**的章节」，章节号可能不连续，所以照同样的
        # 方式取，而不是假定 max+1。
        chapters += _rows_or_empty(
            conn,
            "SELECT idx, title, char_count, content FROM chapters "
            "WHERE project_id=? AND idx>? ORDER BY idx LIMIT 1",
            (project_id, max(source_indexes)),
        )
    artifacts = _rows_or_empty(
        conn,
        "SELECT id, type, status, version, content_hash, contract_version, "
        "       parent_artifact_ids_json, file_path "
        "  FROM artifacts "
        " WHERE (scope_type='episode' AND scope_id=?) "
        "    OR (scope_type='project' AND scope_id=?) "
        " ORDER BY id",
        (episode_id, project_id),
    )
    # 完成凭证与 production revision 表是按需建表的；表不存在等价于「一条也没有」，
    # 建表并写入后行会自然出现在指纹里。
    certificates = _rows_or_empty(
        conn,
        "SELECT * FROM completion_certificates WHERE scope_id=? ORDER BY id",
        (episode_id,),
    )
    revisions = _rows_or_empty(
        conn,
        "SELECT * FROM production_revisions WHERE episode_id=? ORDER BY id",
        (episode_id,),
    )
    evaluations = _rows_or_empty(
        conn,
        "SELECT evaluation.* FROM evaluations AS evaluation "
        "  JOIN artifacts AS artifact ON artifact.id=evaluation.artifact_id "
        " WHERE (artifact.scope_type='episode' AND artifact.scope_id=?) "
        "    OR (artifact.scope_type='project' AND artifact.scope_id=?) "
        " ORDER BY evaluation.id",
        (episode_id, project_id),
    )
    return _digest(
        "screenplay-ready.v1",
        NARRATIVE_CONTRACT_VERSION,
        SCREENPLAY_QA_PROFILE_VERSION,
        data,
        [dict(row) for row in ([project_row] if project_row else [])],
        [dict(row) for row in chapters],
        [dict(row) for row in artifacts],
        [dict(row) for row in certificates],
        [dict(row) for row in revisions],
        [dict(row) for row in evaluations],
    )


def storyboard_pack_prompts_complete(conn, episode_id: str) -> bool:
    """产物信号：本集分镜台 2.0.0（storyboard_pack）的视频提示词是否已全部生成。

    判据只看产物本身，不看 ``episodes.status``：
    ``app.production.storyboard_pack.run_storyboard_pack_generation`` 在派发
    生成前会先把 status 改成中间态用于去重/串行化，生成完成后也只落
    ``scripted``——它从不把 status 推到 ``confirmed``（那是给旧版逐镜叙事
    管线用的人工确认仪式，本函数覆盖的这条新管线里没有等价步骤）。挂
    status 白名单会把这类已经产出完整产物的分集永久判不过（同类事故见
    ``run_storyboard_pack_generation`` 内的记录：``resume_storyboard()`` 派发
    任务前自己先把 status 写成 'scripting' 并提交，随后 Supervisor 重新读到
    的快照必然不在白名单里）。

    每一镜必须都带 ``storyboard_pack_segment.prompt_text``，且尾镜自带
    ``is_final=True``——``persist_storyboard_pack`` 是单事务一次性全写，
    只有整批 segments 都写完才会带上这个标记，看到它就等价于这是一整套
    完整产物、不是半途残留。

    只对全体 shots 都是 storyboard_pack_segment 格式的分集有意义；只要有
    一镜不是这个格式（老版逐镜叙事契约、或历史 plan-null 兼容分集，那类
    行没有存量 prompt_text——提示词是生成请求时才从多个结构化字段现场编译
    的），一律返回 False。调用方必须自己判断这个 False 是"真没做完"还是
    "这集根本不是这条管线"，不能反过来拿这个函数当那些格式的完整性判据。
    """
    rows = conn.execute(
        "SELECT shot_contract_json FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall()
    if not rows:
        return False
    last_is_final = False
    for row in rows:
        try:
            contract = json.loads(row["shot_contract_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        segment = contract.get("storyboard_pack_segment")
        if not isinstance(segment, dict):
            return False
        if not str(segment.get("prompt_text") or "").strip():
            return False
        last_is_final = bool(contract.get("is_final"))
    return last_is_final


def ensure_storyboard_pack_release_gate_decision(conn, episode_id: str) -> None:
    """给"分镜提示词齐全 → 可进生成台"这个转换点补一条审计留痕。

    分镜台 2.0.0（storyboard_pack）管线放行付费生成不再要求人工点一次
    "确认视频提示词"——``storyboard_pack_prompts_complete`` 本身就是唯一
    判据（见该函数docstring）。但这个转换点仍需要一条可审计记录回答
    "这一集是凭什么被放行的"：调用方必须先自行确认
    ``storyboard_pack_prompts_complete(conn, episode_id)`` 为真，本函数不
    重复这个判断，只把已经成立的判据落成账。

    ``decided_by`` 如实标注为系统自动判定，不写 'user'：这不是人工点击。
    ``gate_key`` 用独立的 'storyboard_pack_release'，不复用
    app.domain.video_ops 里人工确认写入的 gate_key='storyboard'——那条的
    ``decided_by`` 是真实操作者、且 _confirm_storyboard_impl 会用
    ``decision IN ('approve','approve_with_risk')`` 查同一 gate_key 做幂等
    短路；共用 gate_key 会让这条自动记录被误当成一次真人工确认，短路掉
    真正的人工确认路径。

    幂等：按 (artifact_id, gate_key='storyboard_pack_release') 判重，schema
    里 idx_gate_decisions_storyboard_pack_release 是这个组合上的唯一索引，
    并发重复写会在 INSERT 层被挡住，不只靠前置 SELECT。同一份分镜产物
    （同一 episodes.storyboard_artifact_id）只留一行；分镜被重新生成产出
    新 artifact_id 后允许再写一行——这是新一轮产物、新一轮放行凭证，不是
    重复。

    只记账，不拦截：写失败只记日志、吞掉异常，绝不向上抛出挡住生成——
    审计是记账，不是闸门。
    """
    try:
        episode = conn.execute(
            "SELECT storyboard_artifact_id FROM episodes WHERE id=?",
            (episode_id,),
        ).fetchone()
        artifact_id = episode["storyboard_artifact_id"] if episode else None
        if not artifact_id:
            _LOGGER.error(
                "storyboard_pack_release 审计：episode %s 产物信号已通过但缺少 "
                "storyboard_artifact_id，无法挂账", episode_id,
            )
            return
        existing = conn.execute(
            "SELECT id FROM gate_decisions WHERE artifact_id=? "
            "AND gate_key='storyboard_pack_release'",
            (artifact_id,),
        ).fetchone()
        if existing:
            return
        shot_count = conn.execute(
            "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,),
        ).fetchone()["c"]
        reason = (
            "系统判定自动放行（storyboard_pack_prompts_complete 产物信号通过）："
            f"共 {shot_count} 段，逐段 shot_contract_json.storyboard_pack_segment."
            "prompt_text 均非空，尾镜 is_final=True。"
        )
        already_in_transaction = conn.in_transaction
        try:
            conn.execute(
                """INSERT INTO gate_decisions(
                       id, artifact_id, gate_key, decision, decided_by, reason, created_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    new_id("gate"), artifact_id, "storyboard_pack_release",
                    "approve", "system:storyboard_pack_prompts_complete",
                    reason, now(),
                ),
            )
        except sqlite3.IntegrityError:
            # 唯一索引挡下的并发重复写：另一路径已经记过账，符合预期，
            # 只需把这次自己开的空事务收掉，不算失败。
            if not already_in_transaction and conn.in_transaction:
                conn.rollback()
            return
        if not already_in_transaction:
            conn.commit()
    except Exception:  # noqa: BLE001 - 记账失败绝不能挡生成
        _LOGGER.exception(
            "storyboard_pack_release 审计写入失败：episode_id=%s", episode_id,
        )
        try:
            if conn.in_transaction:
                conn.rollback()
        except Exception:  # noqa: BLE001
            pass


def _screenplay_ready(ep) -> bool:
    """仅带正式投影的 ready 剧本可进分镜。"""
    data = dict(ep)
    status = data.get("screenplay_status")
    if status in {"repairing", "running", "failed", "pending"}:
        return False
    screenplay_json = data.get("screenplay_json")
    if not (screenplay_json and status == "ready"):
        return False
    identity = screenplay_ready_identity(data)
    with _SCREENPLAY_READY_CACHE_LOCK:
        cached = _SCREENPLAY_READY_CACHE.get(identity)
        if cached is not None:
            _SCREENPLAY_READY_CACHE.move_to_end(identity)
            return cached
    verdict = _screenplay_ready_uncached(data)
    with _SCREENPLAY_READY_CACHE_LOCK:
        _SCREENPLAY_READY_CACHE[identity] = verdict
        _SCREENPLAY_READY_CACHE.move_to_end(identity)
        while len(_SCREENPLAY_READY_CACHE) > _SCREENPLAY_READY_CACHE_SIZE:
            _SCREENPLAY_READY_CACHE.popitem(last=False)
    return verdict


def _prep_pack_ready_uncached(data: dict, payload: dict) -> bool:
    """Readiness check for the lightweight episode_prep_pack pipeline
    (screenplay contract 6.0.0+, docs/TRANSFORM_FREEZE_PLAN.md). Mirrors the
    legacy check's shape below (published pointer must be current, the
    artifact must exist/approve/hash-match) without any narrative_plan
    concept, which prep_pack does not have.
    """
    current_artifact_id = str(data.get("screenplay_artifact_id") or "")
    published_artifact_id = str(data.get("published_screenplay_artifact_id") or "")
    if not current_artifact_id or current_artifact_id != published_artifact_id:
        return False
    artifact = evidence_repository.get_artifact(published_artifact_id)
    if (
        artifact is None
        or artifact.get("type") != "episode_prep_pack"
        or artifact.get("scope_type") != "episode"
        or artifact.get("scope_id") != str(data.get("id") or "")
        or artifact.get("status") != "approved"
    ):
        return False
    try:
        current_hash = evidence_repository.content_hash(
            artifact.get("content"), artifact.get("file_path"),
        )
    except Exception:  # noqa: BLE001 - readiness is fail closed
        return False
    if current_hash != str(artifact.get("content_hash") or ""):
        return False
    # 2.0.0 architecture narrowing (see app.production.prep_pack's module
    # docstring, "2.0.0" note) removed all narrative content -- event_chain,
    # hook, cliffhanger, key_lines -- from this stage's payload entirely; the
    # mapping stage's only remaining job is asset resolution + proving every
    # source segment was read. ``hook`` is therefore structurally absent from
    # every 2.0.x payload, and the old ``bool(payload.get("hook"))`` check
    # below made this function return False unconditionally -- verified
    # against a real EP1 2.0.2 artifact (art_3fe31ed511fa), which has none of
    # the four purged keys at the top level. There is no replacement "does it
    # have narrative content" signal to gate on here because this stage no
    # longer produces narrative content at all (that becomes the storyboard
    # stage's own job, derived straight from source text). The coverage
    # completeness check just above -- coverage_ledger.uncovered empty -- is
    # already the 2.0.x equivalent terminal gate: it is the same 洞即删戏
    # deterministic projection that assert_prep_pack_coverage_complete
    # enforces at publish time (see that function's docstring in
    # app/validators.py), so an artifact that reaches this point has already
    # passed identity/hash verification and proven every source segment was
    # covered. Nothing else remains to check.
    return True


def _screenplay_ready_uncached(data: dict) -> bool:
    screenplay_json = data.get("screenplay_json")
    try:
        parsed = json.loads(screenplay_json)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    if isinstance(parsed, dict) and "prep_pack_version" in parsed:
        return _prep_pack_ready_uncached(data, parsed)
    try:
        script = EpisodeScreenplay.model_validate(parsed)
    except (TypeError, ValueError):
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
    """把绝对落盘路径转成前端可取的 /media URL（带 mtime 版本号防缓存 + 访问票据）。"""
    if not path_str or not os.path.exists(path_str):
        return None
    return build_media_url(path_str, version=int(os.path.getmtime(path_str)))


def _public_reference_image(ref: dict) -> dict:
    """参考图对外表示：只透出前端需要的字段。绝不带上 base64 的 url 与本地 path，
    否则单集响应会因每张参考图内嵌 ~500KB base64 膨胀到数百 MB，拖垮页面甚至崩溃标签页。"""
    image_url = build_media_url(ref.get("path"))
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
