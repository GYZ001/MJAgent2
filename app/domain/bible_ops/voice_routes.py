"""人物卡固定声音：查询、描述建议、生成、采用、批量补齐的 REST 入口。

L5（``app.domain`` 前缀覆盖）：只做请求解析/状态码映射，编排逻辑在
``app.voice.service``（L4）。写操作先过 ``ui_route``（Command Bus 统一入口，
Handler 内再次调用本函数时 ``in_handler()`` 为真、``ui_route`` 直接返回
``None`` 落到下面的直接逻辑）——与
``app.domain.bible_ops.refs_generation.start_refs`` 同一先例。GET 是只读路由，
不需要能力分类，不经过 ``ui_route``（与 ``portrait_candidates.
list_portrait_candidates`` 同一写法）。
"""
from __future__ import annotations

from app.auth.principal import current_actor_name
from app.domain.common import _project_or_404, router
from app.voice import service as voice_service
from app.voice.providers.base import VoiceProviderError
from fastapi import HTTPException


def _provider_error_to_http(exc: VoiceProviderError) -> HTTPException:
    status = 409 if exc.failure_kind == "not_configured" else 502
    return HTTPException(status, str(exc))


@router.get("/projects/{project_id}/voices")
async def list_character_voices(project_id: str):
    """人物谱里每个具名角色一项（没有声音的也列出，current=null）。"""
    _project_or_404(project_id)
    return {"project_id": project_id, **voice_service.list_project_voices(project_id)}


@router.post("/projects/{project_id}/characters/{character_name}/voice-description")
async def voice_description_suggest(project_id: str, character_name: str):
    from app.capabilities.dispatch import ui_route  # 延迟导入避免与 app.capabilities 顶层互相导入成环，同 refs_generation.start_refs

    routed = await ui_route(
        "voice.suggest_description", {"project_id": project_id, "character": character_name},
    )
    if routed is not None:
        return routed
    _project_or_404(project_id)
    try:
        return await voice_service.suggest_description(project_id, character_name)
    except voice_service.VoiceLookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/projects/{project_id}/characters/{character_name}/voices")
async def generate_character_voice(project_id: str, character_name: str, body: dict | None = None):
    from app.capabilities.dispatch import ui_route  # 延迟导入避免与 app.capabilities 顶层互相导入成环，同 refs_generation.start_refs

    payload = body or {}
    voice_prompt = str(payload.get("voice_prompt") or "")
    preview_text = str(payload.get("preview_text") or "")
    idempotency_key = str(payload.get("idempotency_key") or "").strip()
    routed = await ui_route("voice.generate", {
        "project_id": project_id, "character": character_name,
        "voice_prompt": voice_prompt, "preview_text": preview_text,
        "idempotency_key": idempotency_key or None,
    })
    if routed is not None:
        return routed
    _project_or_404(project_id)
    if not idempotency_key:
        raise HTTPException(422, "生成声音需要提供 idempotency_key")
    try:
        voice, run_id = await voice_service.generate_voice_for_character_run(
            project_id, character_name, voice_prompt=voice_prompt, preview_text=preview_text,
            created_by=current_actor_name(),
        )
    except voice_service.VoiceLookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except VoiceProviderError as exc:
        raise _provider_error_to_http(exc) from exc
    return {"voice": voice, "run_id": run_id}


@router.post("/projects/{project_id}/characters/{character_name}/voices/{voice_id}/adopt")
async def adopt_character_voice(project_id: str, character_name: str, voice_id: str):
    from app.capabilities.dispatch import ui_route  # 延迟导入避免与 app.capabilities 顶层互相导入成环，同 refs_generation.start_refs

    routed = await ui_route("voice.adopt", {
        "project_id": project_id, "character": character_name, "voice_id": voice_id,
    })
    if routed is not None:
        return routed
    _project_or_404(project_id)
    try:
        voice = await voice_service.adopt_voice(
            project_id, character_name, voice_id, adopted_by=current_actor_name(),
        )
    except voice_service.VoiceLookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"voice": voice}


@router.post("/projects/{project_id}/voices/generate-missing")
async def generate_missing_voices(project_id: str):
    from app.capabilities.dispatch import ui_route  # 延迟导入避免与 app.capabilities 顶层互相导入成环，同 refs_generation.start_refs

    routed = await ui_route("voice.generate_missing", {"project_id": project_id})
    if routed is not None:
        return routed
    _project_or_404(project_id)
    try:
        accepted, names, run_id = await voice_service.generate_missing_for_project(
            project_id, triggered_by=current_actor_name(fallback="system"),
        )
    except VoiceProviderError as exc:
        raise _provider_error_to_http(exc) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"accepted": accepted, "characters": names, "run_id": run_id}
