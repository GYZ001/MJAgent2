"""视频供应商能力探测、发布物创建与模式审计查询。

从 app/domain/video_ops.py 按原样搬移。
"""
from __future__ import annotations

import asyncio
import time

from app.db import (
    new_id,
    now,
)
from app.domain.common import router
from fastapi import HTTPException


@router.get("/video-capabilities/{provider}/{model:path}")
def get_video_capabilities(provider: str, model: str):
    from app.video_plan import current_capability_snapshot

    return current_capability_snapshot(
        provider=provider, model=model,
    ).model_dump(mode="json")

@router.post("/video-capabilities/{provider}/{model:path}/probe")
async def probe_video_capability(
    provider: str,
    model: str,
    body: dict | None = None,
):
    from app import hiagent
    from app.video_plan import (
        ProviderVideoCapabilitySnapshot,
        current_capability_snapshot,
        save_capability_snapshot,
    )

    payload = body or {}
    if payload.get("confirm") is not True:
        raise HTTPException(409, "能力探针会创建真实付费任务，必须显式提交 confirm=true")
    capability = str(payload.get("capability") or "")
    base = current_capability_snapshot(provider=provider, model=model)
    image_urls: list[tuple[str, str]] = []
    video_urls: list[tuple[str, str]] = []
    if capability == "reference_image":
        image_urls = [(str(payload.get("reference_image_url") or ""), "reference_image")]
    elif capability == "first_last_pair":
        image_urls = [
            (str(payload.get("first_frame_url") or ""), "first_frame"),
            (str(payload.get("last_frame_url") or ""), "last_frame"),
        ]
    elif capability in {"reference_video", "true_video_continuation"}:
        video_urls = [(str(payload.get("reference_video_url") or ""), "reference_video")]
    else:
        raise HTTPException(422, "未知能力探针类型")
    if any(not url.strip() for url, _role in [*image_urls, *video_urls]):
        raise HTTPException(422, "能力探针缺少对应的受控输入素材 URL")
    task_id = None
    result = None
    provider_error = None
    try:
        task_id = await hiagent.create_video_task(
            str(payload.get("prompt") or "受控能力探针：保持输入主体、场景与画面风格。"),
            image_urls=image_urls,
            video_urls=video_urls,
            return_last_frame=bool(payload.get("return_last_frame")),
            call_meta={"stage": "provider_video_capability_probe", "capability": capability},
        )
        deadline = time.time() + float(payload.get("timeout_s") or 1800)
        while time.time() < deadline:
            result = await hiagent.poll_video_task(
                task_id,
                call_meta={"stage": "provider_video_capability_probe", "capability": capability},
            )
            if result["status"] in {"succeeded", "failed"}:
                break
            await asyncio.sleep(float(payload.get("poll_interval_s") or 10))
    except Exception as exc:
        provider_error = exc
    technical_success = bool(
        not provider_error and result and result.get("status") == "succeeded"
    )
    failure_reason = (
        f"{type(provider_error).__name__}:{provider_error}"
        if provider_error else (result or {}).get("error") or "timeout"
    )
    values = base.model_dump(mode="json")
    values.update({
        "id": new_id("cap"),
        "provider": provider,
        "model": model,
        "probe_time": now(),
        "probe_task_id": task_id,
        "probe_result": (
            "succeeded" if technical_success else f"failed:{failure_reason}"
        ),
        "technical_success": technical_success,
    })
    if payload.get("return_last_frame"):
        values["supports_return_last_frame"] = bool(
            technical_success and (result or {}).get("last_frame_url")
        )
    if capability == "reference_image":
        values["supports_reference_image"] = technical_success
    elif capability == "first_last_pair":
        values["supports_first_frame"] = technical_success
        values["supports_last_frame"] = technical_success
        values["supports_first_last_pair"] = technical_success
    elif capability == "reference_video":
        values["supports_reference_video"] = technical_success
    else:
        semantic_success = bool(
            technical_success
            and payload.get("semantic_regression_passed") is True
            and int(payload.get("semantic_sample_count") or 0) >= 20
        )
        values["supports_true_video_continuation"] = semantic_success
        values["semantic_continuation_success"] = semantic_success
    snapshot = ProviderVideoCapabilitySnapshot.model_validate(values)
    save_capability_snapshot(snapshot)
    if provider_error:
        raise HTTPException(
            409,
            {
                "message": f"能力探针失败：{provider_error}",
                "capability_snapshot_id": snapshot.id,
            },
        ) from provider_error
    return snapshot.model_dump(mode="json")

@router.post("/provider-media-publications")
async def create_provider_media_publication(body: dict | None = None):
    from app.video_plan import ProviderMediaPublicationService

    payload = body or {}
    try:
        return await ProviderMediaPublicationService().publish(
            source_revision_id=str(payload.get("source_revision_id") or ""),
            source_url=payload.get("source_url"),
            local_path=payload.get("local_path"),
            expires_at=payload.get("expires_at"),
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

@router.get("/jobs/{job_id}/video-mode-audit")
def get_job_video_mode_audit(job_id: str):
    from app.video_plan import mode_audit_for_job

    audit = mode_audit_for_job(job_id)
    if not audit:
        raise HTTPException(404, "视频任务不存在")
    return audit
