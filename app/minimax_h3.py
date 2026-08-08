"""MiniMax H3 ComfyUI video provider adapter."""
from __future__ import annotations

import base64
import binascii
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
    log_provider_call,
    start_provider_call,
)
from app.hiagent import ProviderError


TASK_PREFIX = "minimax_h3:"
_SUPPORTED_IMAGE_MIMES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}
_SUPPORTED_VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm"}
_APP_MODE_TO_PROVIDER_MODE = {
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
    try:
        payload = response.json()
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            detail = str(error.get("message") or detail)
        elif error:
            detail = str(error)
    except (TypeError, ValueError):
        pass
    return ProviderError(
        f"MiniMaxH3 {action}失败 HTTP {response.status_code}：{detail}",
        retryable=response.status_code in {408, 409, 425, 429} or response.status_code >= 500,
        raw=response.text,
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


def _duration(prompt_text: str, call_meta: dict[str, Any] | None) -> float:
    value = (call_meta or {}).get("duration_s")
    if value is None:
        match = re.search(r"(?:^|\s)--dur\s+(\d+(?:\.\d+)?)", prompt_text)
        value = match.group(1) if match else config.DEFAULT_VIDEO_DURATION_S
    try:
        duration = float(value)
    except (TypeError, ValueError):
        duration = float(config.DEFAULT_VIDEO_DURATION_S)
    return min(15.0, max(0.2, duration))


def _output_dimensions(prompt_text: str) -> tuple[int, int]:
    width = config.MINIMAX_H3_VIDEO_WIDTH
    height = config.MINIMAX_H3_VIDEO_HEIGHT
    ratio = re.search(r"(?:^|\s)--ratio\s+(\d+):(\d+)", prompt_text)
    if ratio and int(ratio.group(1)) > int(ratio.group(2)) and width < height:
        return height, width
    return width, height


def _tagged_prompt(
    prompt_text: str,
    *,
    image_count: int = 0,
    has_video: bool = False,
    use_source_audio: bool = False,
) -> str:
    prompt_text = re.sub(
        r"(?:^|\s)--(?:ratio\s+\S+|dur\s+\d+(?:\.\d+)?)",
        "",
        prompt_text,
    ).strip()
    mappings = [
        f"<Picture {index}> corresponds to Reference image {index}."
        for index in range(1, image_count + 1)
    ]
    if has_video:
        mappings.append("<Video 1> is the reference video for motion and camera behavior.")
    if has_video and use_source_audio:
        mappings.append("<Audio 1> is the source audio reference.")
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
    if roles == ["first_frame", "last_frame"]:
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
    """Estimate warm generation latency from the v1.2.0 576x1024 benchmark."""
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
) -> int:
    """Return a generation-only timeout with cold-load and runtime headroom."""
    expected = estimated_generation_seconds(
        mode,
        duration_s,
        acceleration=acceleration,
    )
    return max(900, round(expected * 1.75 + 600))


def poll_interval_seconds() -> float:
    return float(config.MINIMAX_H3_POLL_INTERVAL)


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
    operation_id = str((call_meta or {}).get("operation_id") or "").strip()
    width, height = _output_dimensions(prompt_text)
    request_summary = {
        "mode": mode,
        "image_roles": [role for _url, role in images],
        "video_roles": [role for _url, role in videos],
        "width": width,
        "height": height,
        "duration": _duration(prompt_text, call_meta),
        "acceleration": config.MINIMAX_H3_ACCELERATION,
        "steps": config.MINIMAX_H3_STEPS,
        "scheduler": "simple",
        "use_te_speed": config.MINIMAX_H3_USE_TE_SPEED,
    }
    ledger_meta = {**(call_meta or {}), "operation_id": operation_id}
    call_id = start_provider_call(
        "video_create",
        model,
        meta=ledger_meta,
        request_json=request_summary,
    )
    started = time.time()
    status_code: int | None = None
    try:
        timeout = httpx.Timeout(connect=10, read=config.TIMEOUT_VIDEO_CREATE, write=180, pool=10)
        async with httpx.AsyncClient(timeout=timeout) as client:
            uploaded_images = []
            for index, (value, _role) in enumerate(images, 1):
                uploaded_images.append(
                    await _upload_file(
                        client,
                        await _materialize_input(value, media_kind="image", index=index),
                    )
                )
            uploaded_videos = []
            for index, (value, _role) in enumerate(videos, 1):
                uploaded_videos.append(
                    await _upload_file(
                        client,
                        await _materialize_input(value, media_kind="video", index=index),
                    )
                )

            intent = str((call_meta or {}).get("video_input_intent") or "")
            use_source_audio = intent in {"AUDIO_REFERENCE", "CONTINUE_PREVIOUS_TAKE"}
            payload: dict[str, Any] = {
                "mode": mode,
                "prompt": _tagged_prompt(
                    prompt_text,
                    image_count=len(uploaded_images) if mode == "reference_images" else 0,
                    has_video=mode == "reference_video",
                    use_source_audio=use_source_audio,
                ),
                "width": width,
                "height": height,
                "duration": request_summary["duration"],
                "acceleration": config.MINIMAX_H3_ACCELERATION,
                "steps": config.MINIMAX_H3_STEPS,
                "seed": -1,
                "scheduler": "simple",
                "use_te_speed": config.MINIMAX_H3_USE_TE_SPEED,
                "output_prefix": (
                    "video/manju_"
                    + re.sub(r"[^A-Za-z0-9_-]+", "_", operation_id or str(int(started)))
                )[:180],
            }
            if config.MINIMAX_H3_ACCELERATION == "turbo":
                payload["turbo_strength"] = config.MINIMAX_H3_TURBO_STRENGTH
                payload["turbo_low_vram"] = config.MINIMAX_H3_TURBO_LOW_VRAM
            if mode == "keyframes":
                payload["first_frame"] = uploaded_images[0]
                payload["last_frame"] = uploaded_images[1]
            elif mode == "reference_images":
                payload["reference_images"] = uploaded_images
                payload["ref_image_size"] = "match"
            else:
                payload["reference_videos"] = uploaded_videos
                payload["use_source_audio"] = use_source_audio
                payload["ref_image_size"] = "match"

            response = await client.post(
                f"{base_url()}/v1/videos/generations",
                headers=_headers(json_content=True),
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
            finish_provider_call(
                call_id,
                "OK",
                status_code,
                int((time.time() - started) * 1000),
                response_json={
                    "id": task_id,
                    "provider_id": provider_id,
                    "status": data.get("status"),
                    "metadata": data.get("metadata"),
                },
            )
            return task_id
    except httpx.RequestError as exc:
        error = ProviderError(
            f"MiniMaxH3 创建任务网络异常：{type(exc).__name__}: {exc}",
            retryable=True,
            raw=repr(exc),
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
    except ValueError as exc:
        raise ProviderError("MiniMaxH3 状态响应不是合法 JSON", retryable=True) from exc
    status = str(data.get("status") or "").strip().lower()
    stage = str(data.get("stage") or "").strip().lower()
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
    if status == "succeeded" and not output.get("url"):
        status = "failed"
        error_text = "MiniMaxH3 任务成功但未返回 MP4 文件"
    return {
        "status": status,
        "stage": stage,
        "queue_position": data.get("queue_position"),
        "video_url": str(output.get("url") or ""),
        "last_frame_url": "",
        "error": error_text,
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


async def probe_connection(override_base_url: str | None = None) -> dict[str, Any]:
    target = str(override_base_url or base_url()).strip().rstrip("/")
    if not re.fullmatch(r"https?://[^\s]+", target):
        raise ProviderError("MiniMaxH3 Base URL 必须是有效的 http(s) 地址")
    started = time.time()
    timeout = httpx.Timeout(connect=5, read=30, write=10, pool=5)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{target}/health", headers=_headers())
    except httpx.RequestError as exc:
        raise ProviderError(
            f"MiniMaxH3 连接失败：{type(exc).__name__}: {exc}",
            retryable=True,
            raw=repr(exc),
        ) from exc
    if response.status_code != 200:
        raise _error_from_response("健康检查", response)
    try:
        data = response.json()
    except ValueError as exc:
        raise ProviderError("MiniMaxH3 健康检查响应不是合法 JSON") from exc
    modes = data.get("modes") if isinstance(data.get("modes"), dict) else {}
    required = ("keyframes", "reference_images", "reference_video")
    unavailable = [
        name for name in required
        if not isinstance(modes.get(name), dict) or modes[name].get("ready") is not True
    ]
    accelerations = (
        data.get("accelerations")
        if isinstance(data.get("accelerations"), dict)
        else {}
    )
    selected_acceleration = config.MINIMAX_H3_ACCELERATION
    acceleration_ready = (
        isinstance(accelerations.get(selected_acceleration), dict)
        and accelerations[selected_acceleration].get("ready") is True
    )
    if data.get("status") != "ok" or unavailable or not acceleration_ready:
        problems = list(unavailable)
        if not acceleration_ready:
            problems.append(f"acceleration:{selected_acceleration}")
        raise ProviderError(
            "MiniMaxH3 服务未就绪"
            + (f"：{', '.join(problems)}" if problems else "")
        )
    return {
        "ok": True,
        "latency_ms": int((time.time() - started) * 1000),
        "preview": (
            "首尾帧、参考图、视频输入三种模式均可用；"
            f"当前默认 {selected_acceleration}"
        ),
        "modes": {name: True for name in required},
        "acceleration": selected_acceleration,
        "steps": config.MINIMAX_H3_STEPS,
        "te_speed_available": bool(data.get("te_speed_available")),
    }
