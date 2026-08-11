"""MiniMax H3 ComfyUI video provider adapter."""
from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import mimetypes
import re
import socket
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from app import config
from app.atomic_io import atomic_write_bytes
from app.db import (
    finish_provider_call,
    get_setting,
    latest_provider_request_json,
    log_provider_call,
    provider_operation_id,
    start_provider_call,
    update_provider_call_request,
)
from app.hiagent import (
    ProviderError,
    ProviderFailure,
    ProviderFailureKind,
    provider_failure_from_http_payload,
)


TASK_PREFIX = "minimax_h3:"
_SUPPORTED_IMAGE_MIMES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}
_SUPPORTED_VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm"}
_APP_MODE_TO_PROVIDER_MODE = {
    "FIRST_FRAME_MODE": "keyframes",
    "FIRST_LAST_FRAME_MODE": "keyframes",
    "REFERENCE_IMAGE_MODE": "reference_images",
    "VIDEO_INPUT_MODE": "reference_video",
}
_TURBO_DURATION_BENCHMARKS_S = {
    "keyframes": ((1.0, 63.0), (5.0, 207.0), (10.0, 396.0), (15.0, 736.0)),
    "reference_images": ((1.0, 95.0), (5.0, 175.0), (10.0, 379.0), (15.0, 712.0)),
    "reference_video": ((1.0, 155.0), (5.0, 358.0), (10.0, 652.0), (15.0, 3918.0)),
}
_STANDARD_LATENCY_MULTIPLIER = 1.86
_REQUEST_CHECKPOINT_VERSION = 1
_PROVIDER_STAGE_PHASES = {
    "queued": "queued",
    "checking_comfyui": "waiting",
    "waiting_for_comfyui": "waiting",
    "submitting": "waiting",
    "queued_in_comfyui": "waiting",
    "conditioning": "generating",
    "sampling": "generating",
    "video_vae": "generating",
    "audio_vae": "generating",
    "video_encoding": "generating",
    "locating_comfyui_job": "generating",
    "generating": "generating",
    "completed": "completed",
}
_PROVIDER_STAGE_LABELS = {
    "queued": "H3 队列等待",
    "checking_comfyui": "检查 ComfyUI",
    "waiting_for_comfyui": "等待 ComfyUI 恢复",
    "submitting": "提交 H3 工作流",
    "queued_in_comfyui": "等待 ComfyUI 执行",
    "conditioning": "编码提示词与参考素材",
    "sampling": "H3 去噪采样",
    "video_vae": "Video VAE 解码",
    "audio_vae": "Audio VAE 解码",
    "video_encoding": "封装音视频",
    "locating_comfyui_job": "恢复 ComfyUI 任务定位",
    "generating": "H3 生成",
    "completed": "H3 任务完成",
}


def base_url() -> str:
    return (
        get_setting("minimax_h3_base_url")
        or config.MINIMAX_H3_BASE_URL
    ).strip().rstrip("/")


def _headers(*, json_content: bool = False) -> dict[str, str]:
    headers: dict[str, str] = {}
    if config.MINIMAX_H3_API_KEY:
        headers["Authorization"] = f"Bearer {config.MINIMAX_H3_API_KEY}"
    if json_content:
        headers["Content-Type"] = "application/json"
    return headers


def _error_from_response(action: str, response: httpx.Response) -> ProviderError:
    detail = response.text[:500]
    payload: Any = None
    try:
        payload = response.json()
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            detail = str(error.get("message") or detail)
        elif error:
            detail = str(error)
    except (TypeError, ValueError):
        pass
    failure = provider_failure_from_http_payload(payload)
    explicit_not_accepted = False
    if isinstance(payload, dict):
        error_payload = payload.get("error")
        failure_payload = (
            error_payload.get("failure")
            if isinstance(error_payload, dict)
            else payload.get("failure")
        )
        explicit_not_accepted = bool(
            isinstance(failure_payload, dict)
            and failure_payload.get("create_not_accepted") is True
        )
    return ProviderError(
        f"MiniMaxH3 {action}失败 HTTP {response.status_code}：{detail}",
        retryable=(
            failure.retryable
            if failure is not None
            else response.status_code in {408, 425, 429}
            or response.status_code >= 500
        ),
        raw=response.text,
        delivery_state="responded",
        create_not_accepted=explicit_not_accepted,
        failure=failure,
    )


def _assert_public_input_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProviderError("MiniMaxH3 输入素材必须是 data URL 或 http(s) URL")
    host = parsed.hostname.lower()
    if host in {"localhost", "metadata", "metadata.google.internal"} or host.endswith(".local"):
        raise ProviderError("MiniMaxH3 输入素材 URL 不能指向本机或链路本地地址")
    try:
        addresses = {str(ipaddress.ip_address(host))}
    except ValueError:
        try:
            addresses = {
                item[4][0]
                for item in socket.getaddrinfo(
                    host,
                    parsed.port or (443 if parsed.scheme == "https" else 80),
                    type=socket.SOCK_STREAM,
                )
            }
        except socket.gaierror as exc:
            raise ProviderError(f"MiniMaxH3 输入素材主机无法解析：{host}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ProviderError("MiniMaxH3 输入素材 URL 不能指向私网或保留地址")


def _max_input_bytes() -> int:
    try:
        return max(
            1_048_576,
            int(get_setting("provider_media_max_download_bytes") or 512 * 1024 * 1024),
        )
    except (TypeError, ValueError):
        return 512 * 1024 * 1024


def _decode_data_image(value: str, index: int) -> tuple[str, bytes, str]:
    match = re.fullmatch(
        r"data:([^;,]+);base64,(.+)",
        value,
        flags=re.DOTALL,
    )
    if not match:
        raise ProviderError("MiniMaxH3 图片 data URL 格式无效")
    mime = match.group(1).lower()
    suffix = _SUPPORTED_IMAGE_MIMES.get(mime)
    if not suffix:
        raise ProviderError(f"MiniMaxH3 不支持图片类型：{mime}")
    try:
        raw = base64.b64decode(match.group(2), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ProviderError("MiniMaxH3 图片 base64 数据无效") from exc
    if not raw or len(raw) > _max_input_bytes():
        raise ProviderError("MiniMaxH3 图片为空或超过上传大小限制")
    return f"image_{index}{suffix}", raw, mime


async def _download_input(
    value: str,
    *,
    media_kind: str,
    index: int,
) -> tuple[str, bytes, str]:
    _assert_public_input_url(value)
    current = value
    limit = _max_input_bytes()
    timeout = httpx.Timeout(connect=10, read=180, write=30, pool=10)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for _hop in range(6):
            _assert_public_input_url(current)
            async with client.stream("GET", current) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ProviderError("MiniMaxH3 输入素材重定向缺少 Location")
                    current = urljoin(current, location)
                    continue
                if response.status_code != 200:
                    raise _error_from_response("读取输入素材", response)
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > limit:
                        raise ProviderError("MiniMaxH3 输入素材超过允许的大小")
                mime = response.headers.get("content-type", "").split(";", 1)[0].lower()
                suffix = Path(urlparse(current).path).suffix.lower()
                if media_kind == "image":
                    if mime not in _SUPPORTED_IMAGE_MIMES:
                        mime = mimetypes.guess_type(f"x{suffix}")[0] or "image/jpeg"
                    suffix = _SUPPORTED_IMAGE_MIMES.get(mime, ".jpg")
                else:
                    if suffix not in _SUPPORTED_VIDEO_SUFFIXES:
                        suffix = ".mp4"
                    mime = mime if mime.startswith("video/") else "video/mp4"
                if not raw:
                    raise ProviderError("MiniMaxH3 输入素材为空")
                return f"{media_kind}_{index}{suffix}", bytes(raw), mime
    raise ProviderError("MiniMaxH3 输入素材重定向次数过多")


async def _materialize_input(
    value: str,
    *,
    media_kind: str,
    index: int,
) -> tuple[str, bytes, str]:
    if value.startswith("data:"):
        if media_kind != "image":
            raise ProviderError("MiniMaxH3 参考视频不接受 data URL")
        return _decode_data_image(value, index)
    return await _download_input(value, media_kind=media_kind, index=index)


async def _upload_file(
    client: httpx.AsyncClient,
    item: tuple[str, bytes, str],
) -> str:
    filename, raw, mime = item
    response = await client.post(
        f"{base_url()}/v1/files",
        headers=_headers(),
        files={"file": (filename, raw, mime)},
    )
    if response.status_code not in {200, 201}:
        raise _error_from_response("上传素材", response)
    try:
        uploaded = str(response.json().get("filename") or "").strip()
    except (TypeError, ValueError) as exc:
        raise ProviderError("MiniMaxH3 上传响应不是合法 JSON") from exc
    if not uploaded:
        raise ProviderError("MiniMaxH3 上传响应缺少 filename")
    return uploaded


def _split_trailing_video_args(prompt_text: str) -> tuple[str, str]:
    match = re.search(
        r"(?P<suffix>(?:\s+--(?:ratio\s+\d+:\d+|dur\s+\d+(?:\.\d+)?))+)\s*$",
        str(prompt_text or ""),
    )
    if not match:
        return str(prompt_text or "").strip(), ""
    return str(prompt_text or "")[:match.start()].strip(), match.group("suffix")


def _duration(prompt_text: str, call_meta: dict[str, Any] | None) -> float:
    value = (call_meta or {}).get("duration_s")
    if value is None:
        _body, suffix = _split_trailing_video_args(prompt_text)
        match = re.search(r"--dur\s+(\d+(?:\.\d+)?)", suffix)
        value = match.group(1) if match else config.DEFAULT_VIDEO_DURATION_S
    try:
        duration = float(value)
    except (TypeError, ValueError):
        duration = float(config.DEFAULT_VIDEO_DURATION_S)
    return min(15.0, max(0.2, duration))


def _output_dimensions(prompt_text: str) -> tuple[int, int]:
    width = config.MINIMAX_H3_VIDEO_WIDTH
    height = config.MINIMAX_H3_VIDEO_HEIGHT
    _body, suffix = _split_trailing_video_args(prompt_text)
    ratio = re.search(r"--ratio\s+(\d+):(\d+)", suffix)
    if ratio and int(ratio.group(1)) > int(ratio.group(2)) and width < height:
        return height, width
    return width, height


def _video_reference_instruction(intent: str, index: int) -> str:
    label = f"<Video {index}>"
    instructions = {
        "MOTION_REFERENCE": (
            f"{label} supplies body motion and physical trajectory only; "
            "do not copy its identity, clothing, scene, camera path, or audio."
        ),
        "CAMERA_REFERENCE": (
            f"{label} supplies camera movement, framing change, and lens rhythm only; "
            "do not copy its people, clothing, scene, actions, or audio."
        ),
        "RHYTHM_REFERENCE": (
            f"{label} supplies temporal pacing and beat timing only; "
            "do not copy its identity, scene, exact motion, camera path, or audio."
        ),
        "AUDIO_REFERENCE": (
            f"{label} is only the carrier of <Audio {index}>; "
            "ignore its visual identity, scene, motion, framing, and camera behavior."
        ),
        "CONTINUE_PREVIOUS_TAKE": (
            f"{label} is the preceding take for visual and audio continuity; "
            "continue from its final state without replaying completed action."
        ),
    }
    return instructions.get(
        intent,
        f"{label} is a declared video reference; use only the purpose stated in the shot contract.",
    )


def _tagged_prompt(
    prompt_text: str,
    *,
    image_count: int = 0,
    video_count: int = 0,
    use_source_audio: bool = False,
    video_input_intent: str = "",
) -> str:
    prompt_text, _suffix = _split_trailing_video_args(prompt_text)
    for index in range(1, image_count + 1):
        prompt_text = re.sub(
            rf"\bReference image {index}\b",
            f"<Picture {index}>",
            prompt_text,
        )
    mappings = [
        f"<Picture {index}> is uploaded reference image {index}; keep its declared subject binding."
        for index in range(1, image_count + 1)
    ]
    mappings.extend(
        _video_reference_instruction(video_input_intent, index)
        for index in range(1, video_count + 1)
    )
    if use_source_audio:
        mappings.extend(
            (
                f"<Audio {index}> is source audio reference {index}; "
                "use its voice/timing as declared, without importing visual content."
            )
            for index in range(1, video_count + 1)
        )
    return (
        "[MiniMax H3 input mapping]\n"
        + " ".join(mappings)
        + "\n"
        + prompt_text
        if mappings
        else prompt_text
    )


def _request_mode(
    image_urls: list[tuple[str, str]],
    video_urls: list[tuple[str, str]],
) -> str:
    roles = [role for _url, role in image_urls]
    if video_urls:
        return "reference_video"
    if roles and all(role == "reference_image" for role in roles):
        return "reference_images"
    if roles in (["first_frame"], ["first_frame", "last_frame"]):
        return "keyframes"
    raise ProviderError(f"MiniMaxH3 无法映射输入角色：{roles}")


def provider_mode(mode: str) -> str:
    normalized = str(mode or "").strip()
    normalized = _APP_MODE_TO_PROVIDER_MODE.get(normalized, normalized)
    if normalized not in _TURBO_DURATION_BENCHMARKS_S:
        raise ValueError(f"unsupported MiniMax H3 mode: {mode}")
    return normalized


def estimated_generation_seconds(
    mode: str,
    duration_s: float,
    *,
    acceleration: str | None = None,
) -> int:
    """Estimate latency from the conservative legacy curve when v1.3 gives none."""
    normalized_mode = provider_mode(mode)
    duration = min(15.0, max(0.2, float(duration_s)))
    points = _TURBO_DURATION_BENCHMARKS_S[normalized_mode]
    estimate = points[-1][1]
    if duration <= points[0][0]:
        estimate = points[0][1]
    else:
        for (left_duration, left_time), (right_duration, right_time) in zip(
            points,
            points[1:],
        ):
            if duration <= right_duration:
                ratio = (
                    (duration - left_duration)
                    / (right_duration - left_duration)
                )
                estimate = left_time + ratio * (right_time - left_time)
                break
    selected_acceleration = str(
        acceleration or config.MINIMAX_H3_ACCELERATION
    ).strip().lower()
    if selected_acceleration == "standard":
        estimate *= _STANDARD_LATENCY_MULTIPLIER
    return max(1, round(estimate))


def generation_timeout_seconds(
    mode: str,
    duration_s: float,
    *,
    acceleration: str | None = None,
    expected_seconds: float | None = None,
) -> int:
    """Return a generation-only timeout with cold-load and runtime headroom."""
    try:
        expected = float(expected_seconds or 0)
    except (TypeError, ValueError):
        expected = 0
    if expected <= 0:
        expected = estimated_generation_seconds(
            mode,
            duration_s,
            acceleration=acceleration,
        )
    return max(900, round(expected * 1.75 + 600))


def poll_interval_seconds() -> float:
    return float(config.MINIMAX_H3_POLL_INTERVAL)


def provider_task_phase(result: dict[str, Any]) -> str:
    """Classify provider progress by protocol state, with forward-compatible inference."""
    status = str(result.get("status") or "").strip().lower()
    if status in {"succeeded", "failed", "not_found"}:
        return "completed"
    stage = str(result.get("stage") or "").strip().lower()
    phase = _PROVIDER_STAGE_PHASES.get(stage)
    if phase:
        return phase
    if status == "queued":
        return "queued"
    if status == "running":
        try:
            queue_position = int(result.get("queue_position") or 0)
        except (TypeError, ValueError):
            queue_position = 0
        return "queued" if queue_position > 0 else "generating"
    return "waiting"


def provider_stage_label(stage: str) -> str:
    normalized = str(stage or "").strip().lower()
    return _PROVIDER_STAGE_LABELS.get(
        normalized,
        f"H3 阶段：{normalized}" if normalized else "H3 处理中",
    )


def _request_fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _input_fingerprint(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()


def _idempotency_key(operation_id: str) -> str:
    value = str(operation_id or "").strip()
    if re.fullmatch(r"[!-~]{1,200}", value):
        return value
    digest = hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()
    return f"h3_{digest}"


def _load_request_checkpoint(
    operation_id: str,
    logical_fingerprint: str,
) -> dict[str, Any] | None:
    saved = latest_provider_request_json(
        "video_create",
        config.DEFAULT_MINIMAX_H3_MODEL_VIDEO,
        operation_id,
    )
    if not isinstance(saved, dict):
        return None
    if saved.get("checkpoint_version") != _REQUEST_CHECKPOINT_VERSION:
        return None
    saved_fingerprint = str(saved.get("logical_fingerprint") or "")
    if saved_fingerprint != logical_fingerprint:
        raise ProviderError(
            "MiniMaxH3 同一业务操作的请求内容发生变化，已阻止复用幂等键；"
            "请为新的生成意图创建新的 operation_id",
            retryable=False,
            delivery_state="not_sent",
            replay_safe=True,
            create_not_accepted=True,
        )
    provider_request = saved.get("provider_request")
    if not isinstance(provider_request, dict):
        return None
    return provider_request


async def create_video_task(
    prompt_text: str,
    *,
    image_urls: list[tuple[str, str]] | None = None,
    video_urls: list[tuple[str, str]] | None = None,
    call_meta: dict[str, Any] | None = None,
) -> str:
    images = list(image_urls or [])
    videos = list(video_urls or [])
    mode = _request_mode(images, videos)
    model = config.DEFAULT_MINIMAX_H3_MODEL_VIDEO
    width, height = _output_dimensions(prompt_text)
    intent = str((call_meta or {}).get("video_input_intent") or "")
    use_source_audio = intent in {"AUDIO_REFERENCE", "CONTINUE_PREVIOUS_TAKE"}
    provider_prompt = _tagged_prompt(
        prompt_text,
        image_count=len(images) if mode == "reference_images" else 0,
        video_count=len(videos) if mode == "reference_video" else 0,
        use_source_audio=use_source_audio,
        video_input_intent=intent,
    )
    logical_request: dict[str, Any] = {
        "mode": mode,
        "prompt": provider_prompt,
        "image_inputs": [
            {"role": role, "fingerprint": _input_fingerprint(value)}
            for value, role in images
        ],
        "video_inputs": [
            {"role": role, "fingerprint": _input_fingerprint(value)}
            for value, role in videos
        ],
        "width": width,
        "height": height,
        "duration": _duration(prompt_text, call_meta),
        "acceleration": config.MINIMAX_H3_ACCELERATION,
        "turbo_profile": (
            config.MINIMAX_H3_TURBO_PROFILE
            if config.MINIMAX_H3_ACCELERATION == "turbo"
            else None
        ),
        "steps": (
            config.MINIMAX_H3_STEPS
            if config.MINIMAX_H3_ACCELERATION != "turbo"
            else None
        ),
        "video_vae": config.MINIMAX_H3_VIDEO_VAE,
        "scheduler": "simple",
        "use_te_speed": config.MINIMAX_H3_USE_TE_SPEED,
        "use_source_audio": use_source_audio,
    }
    logical_fingerprint = _request_fingerprint(logical_request)
    operation_id = (
        str((call_meta or {}).get("operation_id") or "").strip()
        or provider_operation_id("video_create", model, logical_request)
    )
    recovered_payload = _load_request_checkpoint(
        operation_id,
        logical_fingerprint,
    )
    request_checkpoint: dict[str, Any] = {
        "checkpoint_version": _REQUEST_CHECKPOINT_VERSION,
        "logical_fingerprint": logical_fingerprint,
        "request_summary": logical_request,
    }
    if recovered_payload is not None:
        request_checkpoint["provider_request"] = recovered_payload
    ledger_meta = {**(call_meta or {}), "operation_id": operation_id}
    call_id = start_provider_call(
        "video_create",
        model,
        meta=ledger_meta,
        request_json=request_checkpoint,
    )
    if recovered_payload is not None:
        update_provider_call_request(
            call_id,
            request_checkpoint,
            preserve_exact=True,
        )
    started = time.time()
    status_code: int | None = None
    create_request_started = False
    try:
        timeout = httpx.Timeout(connect=10, read=config.TIMEOUT_VIDEO_CREATE, write=180, pool=10)
        async with httpx.AsyncClient(timeout=timeout) as client:
            if recovered_payload is not None:
                payload = dict(recovered_payload)
            else:
                uploaded_images = []
                for index, (value, _role) in enumerate(images, 1):
                    uploaded_images.append(
                        await _upload_file(
                            client,
                            await _materialize_input(
                                value,
                                media_kind="image",
                                index=index,
                            ),
                        )
                    )
                uploaded_videos = []
                for index, (value, _role) in enumerate(videos, 1):
                    uploaded_videos.append(
                        await _upload_file(
                            client,
                            await _materialize_input(
                                value,
                                media_kind="video",
                                index=index,
                            ),
                        )
                    )

                payload = {
                    "mode": mode,
                    "prompt": provider_prompt,
                    "width": width,
                    "height": height,
                    "duration": logical_request["duration"],
                    "acceleration": config.MINIMAX_H3_ACCELERATION,
                    "video_vae": config.MINIMAX_H3_VIDEO_VAE,
                    "seed": -1,
                    "scheduler": "simple",
                    "use_te_speed": config.MINIMAX_H3_USE_TE_SPEED,
                    "output_prefix": (
                        "video/manju_"
                        + re.sub(r"[^A-Za-z0-9_-]+", "_", operation_id)
                    )[:180],
                }
                if config.MINIMAX_H3_ACCELERATION == "turbo":
                    payload.update({
                        "turbo_profile": config.MINIMAX_H3_TURBO_PROFILE,
                        "turbo_strength": config.MINIMAX_H3_TURBO_STRENGTH,
                        "turbo_low_vram": config.MINIMAX_H3_TURBO_LOW_VRAM,
                    })
                else:
                    payload["steps"] = config.MINIMAX_H3_STEPS
                if mode == "keyframes":
                    payload["first_frame"] = uploaded_images[0]
                    if len(uploaded_images) > 1:
                        payload["last_frame"] = uploaded_images[1]
                elif mode == "reference_images":
                    payload["reference_images"] = uploaded_images
                    payload["ref_image_size"] = "match"
                else:
                    payload["reference_videos"] = uploaded_videos
                    payload["use_source_audio"] = use_source_audio
                    payload["ref_image_size"] = "match"

                request_checkpoint["provider_request"] = payload
                update_provider_call_request(
                    call_id,
                    request_checkpoint,
                    preserve_exact=True,
                )

            create_request_started = True
            response = await client.post(
                f"{base_url()}/v1/videos/generations",
                headers={
                    **_headers(json_content=True),
                    "Idempotency-Key": _idempotency_key(operation_id),
                },
                json=payload,
            )
            status_code = response.status_code
            if response.status_code not in {200, 202}:
                raise _error_from_response("创建任务", response)
            try:
                data = response.json()
            except ValueError as exc:
                raise ProviderError("MiniMaxH3 创建任务响应不是合法 JSON") from exc
            provider_id = str(data.get("id") or "").strip()
            if not provider_id:
                raise ProviderError("MiniMaxH3 创建任务响应缺少 id")
            task_id = TASK_PREFIX + provider_id
            response_record = dict(data)
            response_record.update({
                "id": task_id,
                "provider_id": provider_id,
            })
            finish_provider_call(
                call_id,
                "OK",
                status_code,
                int((time.time() - started) * 1000),
                response_json=response_record,
            )
            return task_id
    except httpx.RequestError as exc:
        not_sent = (not create_request_started) or isinstance(
            exc, (httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ConnectError),
        )
        error = ProviderError(
            f"MiniMaxH3 创建任务网络异常：{type(exc).__name__}: {exc}",
            retryable=True,
            raw=repr(exc),
            delivery_state="not_sent" if not_sent else "unknown",
            replay_safe=not_sent,
            requires_explicit_retry=not not_sent,
            create_not_accepted=not create_request_started,
        )
        finish_provider_call(
            call_id,
            "FAILED",
            status_code,
            int((time.time() - started) * 1000),
            error=str(error),
        )
        raise error from exc
    except ProviderError as exc:
        if not create_request_started and not exc.create_not_accepted:
            exc = ProviderError(
                str(exc),
                retryable=exc.retryable,
                raw=exc.raw,
                timeout_phase=exc.timeout_phase,
                failure_kind=exc.failure_kind,
                delivery_state="not_sent",
                replay_safe=True,
                requires_explicit_retry=False,
                create_not_accepted=True,
                failure=exc.failure,
            )
        finish_provider_call(
            call_id,
            "FAILED",
            status_code,
            int((time.time() - started) * 1000),
            error=str(exc),
        )
        raise


def is_task_id(task_id: str) -> bool:
    return str(task_id).startswith(TASK_PREFIX)


def _provider_task_id(task_id: str) -> str:
    if not is_task_id(task_id):
        raise ProviderError("不是 MiniMaxH3 任务 ID")
    value = task_id[len(TASK_PREFIX):].strip()
    if not value:
        raise ProviderError("MiniMaxH3 任务 ID 为空")
    return value


async def poll_video_task(
    task_id: str,
    *,
    call_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    provider_id = _provider_task_id(task_id)
    started = time.time()
    model = config.DEFAULT_MINIMAX_H3_MODEL_VIDEO
    try:
        timeout = httpx.Timeout(connect=10, read=config.TIMEOUT_VIDEO_POLL, write=10, pool=10)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                f"{base_url()}/v1/videos/generations/{provider_id}",
                headers=_headers(),
            )
    except httpx.RequestError as exc:
        error = ProviderError(
            f"MiniMaxH3 状态查询网络异常：{type(exc).__name__}: {exc}",
            retryable=True,
            raw=repr(exc),
        )
        log_provider_call(
            "video_poll",
            model,
            "FAILED",
            None,
            int((time.time() - started) * 1000),
            error=str(error),
            meta=call_meta,
        )
        raise error from exc
    if response.status_code != 200:
        error = _error_from_response("查询任务", response)
        log_provider_call(
            "video_poll",
            model,
            "FAILED",
            response.status_code,
            int((time.time() - started) * 1000),
            error=str(error),
            meta=call_meta,
        )
        raise error
    try:
        data = response.json()
        if not isinstance(data, dict):
            raise TypeError("expected a JSON object")
    except (TypeError, ValueError) as exc:
        raise ProviderError(
            f"MiniMaxH3 状态响应不是合法 JSON 对象：{exc}",
            retryable=True,
            raw=response.text,
            failure_kind=ProviderFailureKind.MALFORMED_RESPONSE,
            delivery_state="responded",
        ) from exc
    status = str(data.get("status") or "").strip().lower()
    stage = str(data.get("stage") or "").strip().lower()
    failure: ProviderFailure | None = None
    files = data.get("files") if isinstance(data.get("files"), list) else []
    output = next(
        (
            item for item in files
            if isinstance(item, dict)
            and str(item.get("filename") or "").lower().endswith(".mp4")
        ),
        {},
    )
    errors = data.get("errors") if isinstance(data.get("errors"), list) else []
    error_text = "; ".join(
        str(
            item.get("exception_message")
            or item.get("message")
            or json.dumps(item, ensure_ascii=False)
        )
        for item in errors
        if isinstance(item, dict)
    )
    if status == "not_found":
        status = "failed"
        error_text = error_text or "MiniMaxH3 队列和历史中均找不到该任务"
        failure = ProviderFailure.technical(ProviderFailureKind.TASK_NOT_FOUND)
    if status == "succeeded" and not output.get("url"):
        status = "failed"
        error_text = "MiniMaxH3 任务成功但未返回 MP4 文件"
        failure = ProviderFailure.technical(ProviderFailureKind.OUTPUT_MISSING)
    if status == "failed" and failure is None:
        failure = ProviderFailure.from_provider_payload(data.get("failure"))
    timings = data.get("timings") if isinstance(data.get("timings"), dict) else {}
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    return {
        "status": status,
        "stage": stage,
        "stage_label": provider_stage_label(stage),
        "provider": "minimax_h3",
        "provider_label": "MiniMax H3",
        "sequence": data.get("sequence"),
        "queue_position": data.get("queue_position"),
        "estimated_seconds": data.get("estimated_seconds"),
        "timings": timings,
        "metadata": metadata,
        "video_url": str(output.get("url") or ""),
        "last_frame_url": "",
        "error": error_text,
        "failure": failure.to_payload() if failure else None,
    }


def is_output_url(value: str) -> bool:
    candidate = urlparse(value)
    configured = urlparse(base_url())
    return (
        candidate.scheme == configured.scheme
        and candidate.netloc == configured.netloc
        and candidate.path == "/v1/outputs"
    )


async def download_output(url: str, dest_path: str) -> None:
    if not is_output_url(url):
        raise ProviderError("MiniMaxH3 输出 URL 不属于已配置服务")
    timeout = httpx.Timeout(connect=10, read=config.TIMEOUT_DOWNLOAD, write=30, pool=10)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.get(url, headers=_headers())
    except httpx.RequestError as exc:
        raise ProviderError(
            f"MiniMaxH3 视频下载网络异常：{type(exc).__name__}: {exc}",
            retryable=True,
            raw=repr(exc),
        ) from exc
    if response.status_code != 200:
        raise _error_from_response("下载视频", response)
    atomic_write_bytes(dest_path, response.content)


def _probe_target(override_base_url: str | None = None) -> str:
    target = str(override_base_url or base_url()).strip().rstrip("/")
    if not re.fullmatch(r"https?://[^\s]+", target):
        raise ProviderError("MiniMaxH3 Base URL 必须是有效的 http(s) 地址")
    return target


def _probe_response_json(
    action: str,
    response: httpx.Response,
) -> dict[str, Any]:
    if response.status_code != 200:
        raise _error_from_response(action, response)
    try:
        data = response.json()
    except ValueError as exc:
        raise ProviderError(f"MiniMaxH3 {action}响应不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise ProviderError(f"MiniMaxH3 {action}响应必须是 JSON 对象")
    return data


def _probe_result(
    root: dict[str, Any],
    health: dict[str, Any],
    *,
    target: str,
    latency_ms: int,
) -> dict[str, Any]:
    advertised_modes = [
        str(name)
        for name in (root.get("modes") or [])
        if str(name).strip()
    ]
    health_modes = health.get("modes") if isinstance(health.get("modes"), dict) else {}
    modes = {
        name: bool(
            isinstance(health_modes.get(name), dict)
            and health_modes[name].get("ready") is True
        )
        for name in advertised_modes
    }
    advertised_accelerations = [
        str(name)
        for name in (root.get("accelerations") or [])
        if str(name).strip()
    ]
    health_accelerations = (
        health.get("accelerations")
        if isinstance(health.get("accelerations"), dict)
        else {}
    )
    accelerations = {
        name: bool(
            isinstance(health_accelerations.get(name), dict)
            and health_accelerations[name].get("ready") is True
        )
        for name in advertised_accelerations
    }
    turbo_profiles = (
        root.get("turbo_profiles")
        if isinstance(root.get("turbo_profiles"), dict)
        else {}
    )
    advertised_vaes = [
        str(name)
        for name in (root.get("video_vae_profiles") or [])
        if str(name).strip()
    ]
    health_vaes = (
        health.get("video_vae_profiles")
        if isinstance(health.get("video_vae_profiles"), dict)
        else {}
    )
    video_vae_profiles = {
        name: bool(
            isinstance(health_vaes.get(name), dict)
            and health_vaes[name].get("ready") is True
        )
        for name in advertised_vaes
    }
    selected_acceleration = config.MINIMAX_H3_ACCELERATION
    selected_profile = config.MINIMAX_H3_TURBO_PROFILE
    selected_vae = config.MINIMAX_H3_VIDEO_VAE
    api_version = str(root.get("version") or "").strip()
    problems: list[str] = []
    if health.get("status") != "ok":
        problems.append("health")
    if not api_version:
        problems.append("api_version")
    if not any(modes.values()):
        problems.append("generation_modes")
    if accelerations.get(selected_acceleration) is not True:
        problems.append(f"acceleration:{selected_acceleration}")
    profile_steps = turbo_profiles.get(selected_profile)
    if selected_acceleration == "turbo":
        if not isinstance(profile_steps, int) or isinstance(profile_steps, bool):
            problems.append(f"turbo_profile:{selected_profile}")
    if video_vae_profiles.get(selected_vae) is not True:
        problems.append(f"video_vae:{selected_vae}")
    if problems:
        raise ProviderError(
            "MiniMaxH3 服务能力与当前配置不匹配：" + ", ".join(problems)
        )
    ready_modes = [name for name, ready in modes.items() if ready]
    return {
        "ok": True,
        "base_url": target,
        "api_version": api_version,
        "latency_ms": latency_ms,
        "preview": (
            f"可用模式：{', '.join(ready_modes)}；"
            f"当前 {selected_acceleration}"
            + (
                f"/{selected_profile}"
                if selected_acceleration == "turbo"
                else ""
            )
            + f"，Video VAE={selected_vae}"
        ),
        "modes": modes,
        "accelerations": accelerations,
        "acceleration": selected_acceleration,
        "turbo_profiles": turbo_profiles,
        "turbo_profile": (
            selected_profile if selected_acceleration == "turbo" else None
        ),
        "steps": (
            int(profile_steps)
            if selected_acceleration == "turbo"
            else config.MINIMAX_H3_STEPS
        ),
        "video_vae_profiles": video_vae_profiles,
        "video_vae": selected_vae,
        "te_speed_available": bool(health.get("te_speed_available")),
    }


async def probe_connection(override_base_url: str | None = None) -> dict[str, Any]:
    target = _probe_target(override_base_url)
    started = time.time()
    timeout = httpx.Timeout(connect=5, read=30, write=10, pool=5)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            root_response = await client.get(f"{target}/", headers=_headers())
            health_response = await client.get(
                f"{target}/health",
                headers=_headers(),
            )
    except httpx.RequestError as exc:
        raise ProviderError(
            f"MiniMaxH3 连接失败：{type(exc).__name__}: {exc}",
            retryable=True,
            raw=repr(exc),
        ) from exc
    root = _probe_response_json("版本检查", root_response)
    health = _probe_response_json("健康检查", health_response)
    return _probe_result(
        root,
        health,
        target=target,
        latency_ms=int((time.time() - started) * 1000),
    )


def probe_connection_sync(
    override_base_url: str | None = None,
) -> dict[str, Any]:
    """Synchronous capability discovery for sync planning boundaries."""
    target = _probe_target(override_base_url)
    started = time.time()
    timeout = httpx.Timeout(connect=5, read=30, write=10, pool=5)
    try:
        with httpx.Client(timeout=timeout) as client:
            root_response = client.get(f"{target}/", headers=_headers())
            health_response = client.get(f"{target}/health", headers=_headers())
    except httpx.RequestError as exc:
        raise ProviderError(
            f"MiniMaxH3 连接失败：{type(exc).__name__}: {exc}",
            retryable=True,
            raw=repr(exc),
        ) from exc
    root = _probe_response_json("版本检查", root_response)
    health = _probe_response_json("健康检查", health_response)
    return _probe_result(
        root,
        health,
        target=target,
        latency_ms=int((time.time() - started) * 1000),
    )
