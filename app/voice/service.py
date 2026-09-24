"""角色固定声音的生成编排：描述 → 供应商调用 → 落盘裁片 → 语音识别核验 →
候选/自动采用，以及 REST 层要用的行→VoiceVersion 投影（PRD 见
docs/角色固定音色_声音生成接口调研与实施方案_2026-09-23.md §5.2）。

L4（包根 ``"app.voice" = 4``）：本模块是真正发起外部调用（模型/ffmpeg/语音
识别）与编排状态转移的地方，``app.voice.store`` 只管持久化，两者分层理由见
该模块 docstring。

事务边界：每次 ``INSERT``/``UPDATE`` 后立即 ``conn.commit()``，任何一次
``await``（供应商调用、``asyncio.to_thread``）之前都不持有未提交的写——
``scripts/check_write_across_await.py`` 守着，本仓库已因写锁跨 await 冻结过
整个后端（CLAUDE.md 同一条教训）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from app import task_registry
from app.db import get_conn
from app.media_urls import build_media_url
from app.schemas import Bible, Character, character_is_portrait_eligible

from app.voice import store
from app.voice.asr_check import check_preview_match
from app.voice.clipping import VoiceClipError, cut_and_normalize_clip
from app.voice.description import generate_voice_description
from app.voice.naming import preferred_name_for
from app.voice.providers import dispatch
from app.voice.providers.base import VoiceDesignRequest, VoiceDesignResult, VoiceProviderError
from app.voice.settings import voice_auto_generate_enabled

ANCHOR_KEY_DEFAULT = ""
VOICE_PURPOSE = "voice:default"
AUTO_ADOPT_CHECK_STATUSES = ("passed", "unchecked")
#: 同一项目的「补齐缺失」与「定妆后自动生成」共用同一个 task_registry 键——
#: 两个触发源本质都是"批量后台生成"，共用键让 task_registry.spawn 的天然互斥
#: 防住同项目并发触发时对同一批角色重复生成的竞态，不需要另造锁。
_VOICE_BULK_TASK_KIND = "voice_bulk_generate"


class VoiceLookupError(LookupError):
    """人物卡或声音版本查无——REST 层统一映射成 404。"""


def voice_model_configured() -> bool:
    return dispatch.resolve_voice_model(VOICE_PURPOSE) is not None


def _project_bible(conn, project_id: str) -> Bible | None:
    row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row or not row["bible_json"]:
        return None
    return Bible.model_validate(json.loads(row["bible_json"]))


def _character_or_404(bible: Bible | None, character_name: str) -> Character:
    if bible is not None:
        for character in bible.characters:
            if character.name == character_name:
                return character
    raise VoiceLookupError(f"角色「{character_name}」不存在")


def _character_roster(bible: Bible | None) -> list[str]:
    if bible is None:
        return []
    return [c.name for c in bible.characters if character_is_portrait_eligible(c)]


def _existing_prompts(conn, project_id: str, *, exclude: str) -> dict[str, str]:
    bible = _project_bible(conn, project_id)
    out: dict[str, str] = {}
    for name in _character_roster(bible):
        if name == exclude:
            continue
        current = store.current_for(conn, project_id, name, ANCHOR_KEY_DEFAULT)
        if current is not None and current["voice_prompt"]:
            out[name] = str(current["voice_prompt"])
    return out


def to_version_dict(row: Any) -> dict[str, Any]:
    status, error = store.effective_status(row)
    return {
        "id": row["id"], "status": status, "source": row["source"],
        "voice_prompt": row["voice_prompt"], "preview_text": row["preview_text"],
        "audio_url": build_media_url(row["audio_path"]) or "",
        "clip_url": build_media_url(row["clip_path"]) or "",
        "clip_duration_s": row["clip_duration_s"],
        "check_status": row["check_status"], "check_reason": row["check_reason"],
        "asr_text": row["asr_text"], "error": error,
        "created_at": row["created_at"], "adopted_at": row["adopted_at"],
    }


def _is_actively_generating(row: Any) -> bool:
    return store.effective_status(row)[0] == store.STATUS_GENERATING


def list_project_voices(project_id: str) -> dict[str, Any]:
    conn = get_conn()
    bible = _project_bible(conn, project_id)
    items = []
    for name in _character_roster(bible):
        rows = store.list_for_character(conn, project_id, name, ANCHOR_KEY_DEFAULT)
        current = next((r for r in rows if r["status"] == store.STATUS_CURRENT), None)
        candidates = [
            r for r in rows if r["status"] not in (store.STATUS_CURRENT, store.STATUS_RETIRED)
        ][:store.MAX_NONCURRENT_PER_CHARACTER]
        items.append({
            "character_name": name, "anchor_key": ANCHOR_KEY_DEFAULT,
            "current": to_version_dict(current) if current is not None else None,
            "candidates": [to_version_dict(r) for r in candidates],
            "generating": any(_is_actively_generating(r) for r in rows),
        })
    return {
        "voice_model_configured": voice_model_configured(),
        "auto_generate": voice_auto_generate_enabled(),
        "items": items,
    }


async def suggest_description(project_id: str, character_name: str) -> dict[str, str]:
    """只返回建议，不落库。"""
    conn = get_conn()
    bible = _project_bible(conn, project_id)
    character = _character_or_404(bible, character_name)
    era = bible.world.era if bible is not None else ""
    existing = _existing_prompts(conn, project_id, exclude=character_name)
    voice_prompt, preview_text = await generate_voice_description(
        character, era=era, existing_prompts=existing,
    )
    return {"voice_prompt": voice_prompt, "preview_text": preview_text}


def _finalize_voice_files(
    project_id: str, voice_id: str, result: VoiceDesignResult, preview_text: str,
) -> dict[str, Any]:
    """同步阻塞（ffmpeg + 语音识别）；调用方必须 ``asyncio.to_thread``。"""
    ext = {"wav": "wav", "mp3": "mp3"}.get(result.audio_format, "bin")
    voices_dir = store.voice_dir(project_id)
    audio_path = voices_dir / f"{voice_id}_full.{ext}"
    audio_path.write_bytes(result.audio)
    clip_path = voices_dir / f"{voice_id}_clip.wav"
    try:
        duration = cut_and_normalize_clip(audio_path, clip_path)
    except VoiceClipError as exc:
        return {
            "status": store.STATUS_FAILED, "provider_voice_id": result.provider_voice_id,
            "audio_path": str(audio_path), "error": f"裁片失败：{exc}",
        }
    check = check_preview_match(audio_path, preview_text)
    return {
        "status": store.STATUS_CANDIDATE, "provider_voice_id": result.provider_voice_id,
        "audio_path": str(audio_path), "clip_path": str(clip_path),
        "clip_duration_s": duration, "clip_sha256": hashlib.sha256(clip_path.read_bytes()).hexdigest(),
        "check_status": check.check_status, "check_reason": check.check_reason,
        "asr_text": check.asr_text, "asr_match": check.asr_match,
    }


def _maybe_auto_adopt(conn, project_id: str, character_name: str, row: Any) -> Any:
    """该角色还没有 current 时，第一个校验通过（passed 或 unchecked）的候选
    自动成为 current；已有 current 时只进候选列表，等人工采用。"""
    if row["status"] != store.STATUS_CANDIDATE or row["check_status"] not in AUTO_ADOPT_CHECK_STATUSES:
        return row
    if store.current_for(conn, project_id, character_name, ANCHOR_KEY_DEFAULT) is not None:
        return row
    store.set_current(
        conn, project_id, character_name, row["id"], ANCHOR_KEY_DEFAULT, adopted_by="system:auto",
    )
    return store.get(conn, project_id, row["id"])


async def generate_voice_for_character(
    project_id: str, character_name: str, *, voice_prompt: str, preview_text: str, created_by: str,
) -> dict[str, Any]:
    """同步完成的单角色生成流程（约 10 秒内）。角色不存在抛
    ``VoiceLookupError``；未配置/供应商失败抛 ``VoiceProviderError``——两者都
    由 REST 层翻译成对应状态码，本函数不引入 fastapi 依赖。"""
    conn = get_conn()
    bible = _project_bible(conn, project_id)
    character = _character_or_404(bible, character_name)
    resolved = dispatch.resolve_voice_model(VOICE_PURPOSE)
    if resolved is None:
        raise VoiceProviderError("未配置声音生成模型，请在模型中心添加并绑定", failure_kind="not_configured")
    voice_prompt = voice_prompt.strip()
    preview_text = preview_text.strip()
    if not voice_prompt or not preview_text:
        era = bible.world.era if bible is not None else ""
        existing = _existing_prompts(conn, project_id, exclude=character_name)
        voice_prompt, preview_text = await generate_voice_description(
            character, era=era, existing_prompts=existing,
        )

    voice_id = store.insert_generating(
        conn, project_id=project_id, character_name=character_name, anchor_key=ANCHOR_KEY_DEFAULT,
        model_id=resolved.model_id, voice_prompt=voice_prompt, preview_text=preview_text,
        created_by=created_by,
    )
    conn.commit()

    req = VoiceDesignRequest(
        voice_prompt=voice_prompt, preview_text=preview_text,
        preferred_name=preferred_name_for(character_name),
    )
    try:
        result = await dispatch.design_voice(
            req, purpose=VOICE_PURPOSE,
            call_meta={"project_id": project_id, "character_name": character_name},
        )
    except VoiceProviderError as exc:
        conn = get_conn()
        store.mark_finished(conn, project_id, voice_id, status=store.STATUS_FAILED, error=str(exc))
        conn.commit()
        raise

    return await _finish_generation(project_id, character_name, voice_id, result, preview_text)


async def _finish_generation(
    project_id: str, character_name: str, voice_id: str, result: VoiceDesignResult, preview_text: str,
) -> dict[str, Any]:
    """落盘、裁片、语音识别核对（线程里跑）后写回结果；意外失败要落在这一行上
    标成 failed，不能让它一直停在 generating 等 10 分钟超时。"""
    try:
        outcome = await asyncio.to_thread(_finalize_voice_files, project_id, voice_id, result, preview_text)
    except Exception as exc:  # noqa: BLE001 -- 原样上抛，这里只负责把失败记到该行上
        conn = get_conn()
        store.mark_finished(
            conn, project_id, voice_id, status=store.STATUS_FAILED,
            error=f"保存或裁剪试听音频失败：{type(exc).__name__}: {exc}",
        )
        conn.commit()
        raise
    conn = get_conn()
    store.mark_finished(conn, project_id, voice_id, **outcome)
    row = _maybe_auto_adopt(conn, project_id, character_name, store.get(conn, project_id, voice_id))
    store.prune_noncurrent(conn, project_id, character_name, ANCHOR_KEY_DEFAULT)
    conn.commit()
    return to_version_dict(row)


async def adopt_voice(
    project_id: str, character_name: str, voice_id: str, *, adopted_by: str,
) -> dict[str, Any]:
    conn = get_conn()
    row = store.get(conn, project_id, voice_id)
    if row is None or row["character_name"] != character_name:
        raise VoiceLookupError(f"声音版本「{voice_id}」不存在")
    if row["status"] in (store.STATUS_FAILED, store.STATUS_RETIRED, store.STATUS_GENERATING):
        raise ValueError(f"该声音版本当前状态为「{row['status']}」，不能被采用")
    store.set_current(conn, project_id, character_name, voice_id, ANCHOR_KEY_DEFAULT, adopted_by=adopted_by)
    conn.commit()
    return to_version_dict(store.get(conn, project_id, voice_id))


def _names_needing_voice(conn, project_id: str, candidate_names: list[str]) -> list[str]:
    """候选名单里，还没有 current、且当前也没有活跃 generating 行的角色。"""
    eligible = set(_character_roster(_project_bible(conn, project_id)))
    out: list[str] = []
    for name in dict.fromkeys(candidate_names):
        if name not in eligible:
            continue
        if store.current_for(conn, project_id, name, ANCHOR_KEY_DEFAULT) is not None:
            continue
        rows = store.list_for_character(conn, project_id, name, ANCHOR_KEY_DEFAULT)
        if any(_is_actively_generating(r) for r in rows):
            continue
        out.append(name)
    return out


async def _generate_missing_task(project_id: str, names: list[str], *, triggered_by: str) -> None:
    for name in names:
        try:
            await generate_voice_for_character(
                project_id, name, voice_prompt="", preview_text="", created_by=triggered_by,
            )
        except Exception:  # noqa: BLE001 -- 单个角色失败不影响其余角色继续跑，原因已落在该行上
            continue


async def generate_missing_for_project(project_id: str, *, triggered_by: str) -> tuple[int, list[str]]:
    """后台串行为所有还没有 current 的具名角色生成声音，立即返回受理数量。
    未配置模型、或同项目已有批量任务在跑时明确报错（界面据此给出原因），不静默返回 0。"""
    if not voice_model_configured():
        raise VoiceProviderError("未配置声音生成模型，请在模型中心添加并绑定", failure_kind="not_configured")
    conn = get_conn()
    targets = _names_needing_voice(conn, project_id, _character_roster(_project_bible(conn, project_id)))
    if not targets:
        return 0, []
    try:
        task_registry.spawn(
            _VOICE_BULK_TASK_KIND, project_id,
            _generate_missing_task(project_id, targets, triggered_by=triggered_by),
            project_id=project_id,
        )
    except RuntimeError:
        raise ValueError("已有声音批量生成任务在进行中，完成后刷新即可看到结果") from None
    return len(targets), targets


def trigger_auto_generate_after_portrait(project_id: str, character_names: list[str]) -> None:
    """定妆批次成功后台触发同批角色的声音自动生成；不阻塞定妆流程，失败不
    外抛。挂钩点见 ``app.domain.bible_ops.refs_generation._refs_task``。"""
    if not character_names or not voice_auto_generate_enabled() or not voice_model_configured():
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    conn = get_conn()
    targets = _names_needing_voice(conn, project_id, character_names)
    if not targets:
        return
    try:
        task_registry.spawn(
            _VOICE_BULK_TASK_KIND, project_id,
            _generate_missing_task(project_id, targets, triggered_by="system:auto_after_portrait"),
            project_id=project_id,
        )
    except RuntimeError:
        return  # 已有同项目自动生成/补齐任务在跑，交给那一轮处理
