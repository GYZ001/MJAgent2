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
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

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


@dataclass(frozen=True)
class H3Connection:
    """一个 H3 服务实例的连接与出片参数。

    协议相同、连接不同的服务（同一份 ComfyUI 工作流部署在不同机器/隧道上）
    因此可以共用这份适配器，不必再为每个实例改代码。内置实例由
    ``default_connection()`` 从 settings/env 组装，页面新增的实例由
    ``connection_from_catalog_item()`` 从模型库条目组装。
    """

    base_url: str
    api_key: str
    model: str
    width: int
    height: int
    acceleration: str
    turbo_profile: str
    turbo_strength: float
    turbo_low_vram: bool
    video_vae: str
    steps: int
    use_te_speed: bool
    poll_interval: float


def base_url() -> str:
    """内置实例的服务地址；隧道地址轮换时改 settings 即可，不必动代码。"""
    return (
        get_setting("minimax_h3_base_url")
        or config.MINIMAX_H3_BASE_URL
    ).strip().rstrip("/")


def default_connection() -> H3Connection:
    return H3Connection(
        base_url=base_url(),
        api_key=config.MINIMAX_H3_API_KEY,
        model=(
            get_setting("minimax_h3_model_video")
            or config.DEFAULT_MINIMAX_H3_MODEL_VIDEO
        ),
        width=config.MINIMAX_H3_VIDEO_WIDTH,
        height=config.MINIMAX_H3_VIDEO_HEIGHT,
        acceleration=config.MINIMAX_H3_ACCELERATION,
        turbo_profile=config.MINIMAX_H3_TURBO_PROFILE,
        turbo_strength=config.MINIMAX_H3_TURBO_STRENGTH,
        turbo_low_vram=config.MINIMAX_H3_TURBO_LOW_VRAM,
        video_vae=config.MINIMAX_H3_VIDEO_VAE,
        steps=config.MINIMAX_H3_STEPS,
        use_te_speed=config.MINIMAX_H3_USE_TE_SPEED,
        poll_interval=config.MINIMAX_H3_POLL_INTERVAL,
    )


def _clamped_int(value: Any, low: int, high: int, fallback: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return fallback


def connection_from_catalog_item(
    item: dict[str, Any],
    *,
    base_url_override: str = "",
    api_key_override: str = "",
) -> H3Connection:
    """把模型库里的一条自建实例翻译成连接参数。

    缺省一律回落到内置实例的取值：页面上只填 Base URL 和 Key 就能跑起来，
    加速档之类的调优项留空即沿用当前默认，而不是让实例带着一堆空值提交。
    """
    fallback = default_connection()
    params = item.get("params") if isinstance(item.get("params"), dict) else {}
    acceleration = str(
        params.get("acceleration") or fallback.acceleration
    ).strip().lower()
    if acceleration not in {"standard", "turbo"}:
        acceleration = fallback.acceleration
    turbo_profile = str(
        params.get("turbo_profile") or fallback.turbo_profile
    ).strip() or fallback.turbo_profile
    step_low, step_high = (4, 8) if acceleration == "turbo" else (1, 100)
    return H3Connection(
        base_url=str(
            base_url_override or item.get("base_url") or fallback.base_url
        ).strip().rstrip("/"),
        api_key=str(
            api_key_override or item.get("api_key") or fallback.api_key
        ).strip(),
        model=str(item.get("model") or fallback.model).strip(),
        width=_clamped_int(
            params.get("width", fallback.width), 32, 4096, fallback.width,
        ) // 32 * 32,
        height=_clamped_int(
            params.get("height", fallback.height), 32, 4096, fallback.height,
        ) // 32 * 32,
        acceleration=acceleration,
        turbo_profile=turbo_profile,
        turbo_strength=fallback.turbo_strength,
        turbo_low_vram=fallback.turbo_low_vram,
        video_vae=str(
            params.get("video_vae") or fallback.video_vae
        ).strip() or fallback.video_vae,
        steps=_clamped_int(
            params.get("steps", fallback.steps),
            step_low,
            step_high,
            min(step_high, max(step_low, fallback.steps)),
        ),
        use_te_speed=bool(
            params.get("use_te_speed", fallback.use_te_speed)
        ),
        poll_interval=fallback.poll_interval,
    )


def _connection(connection: H3Connection | None) -> H3Connection:
    return connection if connection is not None else default_connection()


def _headers(
    connection: H3Connection | None = None,
    *,
    json_content: bool = False,
) -> dict[str, str]:
    headers: dict[str, str] = {}
    api_key = _connection(connection).api_key
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
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
    connection: H3Connection,
) -> str:
    filename, raw, mime = item
    response = await client.post(
        f"{connection.base_url}/v1/files",
        headers=_headers(connection),
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


def _output_dimensions(
    prompt_text: str,
    connection: H3Connection,
) -> tuple[int, int]:
    width = connection.width
    height = connection.height
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
    for index in range(1, image_count + 1):  # "图片N" 锚点见 seedance_reference_notes
        prompt_text = re.sub(
            rf"\b图片{index}\b",
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
    if mappings and prompt_text.startswith("subject_definitions:\n"):
        mappings = [
            line
            for line in mappings
            if not (
                (label_match := re.match(r"(<(?:Picture|Video|Audio) \d+>)", line))
                and re.search(  # 全角冒号=中文参考图说明，半角=H3 原生英文写法，两种都要认
                    re.escape(label_match.group(1)) + r"\s*[:：]",
                    prompt_text,
                )
            )
        ]
        if not mappings:
            return prompt_text
        heading, body = prompt_text.split("\n", 1)
        return (
            heading
            + "\n"
            + "\n".join(mappings)
            + "\n"
            + body
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


def poll_interval_seconds(connection: H3Connection | None = None) -> float:
    return float(_connection(connection).poll_interval)


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
    model: str,
) -> dict[str, Any] | None:
    saved = latest_provider_request_json(
        "video_create",
        model,
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
            delivery_state="unknown",
            requires_explicit_retry=True,
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
    connection: H3Connection | None = None,
) -> str:
    conn = _connection(connection)
    images = list(image_urls or [])
    videos = list(video_urls or [])
    mode = _request_mode(images, videos)
    model = conn.model
    width, height = _output_dimensions(prompt_text, conn)
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
        "model": model,
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
        "acceleration": conn.acceleration,
        "turbo_profile": (
            conn.turbo_profile if conn.acceleration == "turbo" else None
        ),
        "steps": (
            conn.steps if conn.acceleration != "turbo" else None
        ),
        "video_vae": conn.video_vae,
        "scheduler": "simple",
        "use_te_speed": conn.use_te_speed,
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
        model,
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
                            conn,
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
                            conn,
                        )
                    )

                payload = {
                    "model": model,
                    "mode": mode,
                    "prompt": provider_prompt,
                    "width": width,
                    "height": height,
                    "duration": logical_request["duration"],
                    "acceleration": conn.acceleration,
                    "video_vae": conn.video_vae,
                    "seed": -1,
                    "scheduler": "simple",
                    "use_te_speed": conn.use_te_speed,
                    "output_prefix": (
                        "video/manju_"
                        + re.sub(r"[^A-Za-z0-9_-]+", "_", operation_id)
                    )[:180],
                }
                if conn.acceleration == "turbo":
                    payload.update({
                        "turbo_profile": conn.turbo_profile,
                        "turbo_strength": conn.turbo_strength,
                        "turbo_low_vram": conn.turbo_low_vram,
                    })
                else:
                    payload["steps"] = conn.steps
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
                f"{conn.base_url}/v1/videos/generations",
                headers={
                    **_headers(conn, json_content=True),
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
        raise exc


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
    connection: H3Connection | None = None,
) -> dict[str, Any]:
    conn = _connection(connection)
    provider_id = _provider_task_id(task_id)
    started = time.time()
    model = conn.model
    try:
        timeout = httpx.Timeout(connect=10, read=config.TIMEOUT_VIDEO_POLL, write=10, pool=10)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                f"{conn.base_url}/v1/videos/generations/{provider_id}",
                headers=_headers(conn),
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
    raw_output_url = str(output.get("url") or "")
    output_url = normalize_output_url(raw_output_url, conn)
    if status == "succeeded" and not raw_output_url:
        status = "failed"
        error_text = "MiniMaxH3 任务成功但未返回 MP4 文件"
        failure = ProviderFailure.technical(ProviderFailureKind.OUTPUT_MISSING)
    elif status == "succeeded" and not output_url:
        status = "failed"
        error_text = f"MiniMaxH3 输出 URL 不属于已配置服务：{raw_output_url}"
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
        "video_url": output_url,
        "last_frame_url": "",
        "error": error_text,
        "failure": failure.to_payload() if failure else None,
    }


def normalize_output_url(
    value: str,
    connection: H3Connection | None = None,
) -> str:
    """Rebind a provider output URL onto the configured base, or return "".

    H3 builds ``files[].url`` from the request Host without honouring
    ``X-Forwarded-Proto``, so behind the public tunnel it advertises ``http://``
    even when the service is only reachable over ``https://``. Requiring an exact
    scheme match would reject every download, so only host and path are matched
    and the configured scheme is reimposed — plaintext media transfers stay off
    the public internet either way.
    """
    candidate = urlparse(str(value or ""))
    configured = urlparse(_connection(connection).base_url)
    if (
        candidate.scheme not in {"http", "https"}
        or candidate.netloc != configured.netloc
        or candidate.path != "/v1/outputs"
    ):
        return ""
    return urlunparse((
        configured.scheme,
        configured.netloc,
        candidate.path,
        candidate.params,
        candidate.query,
        candidate.fragment,
    ))


def is_output_url(value: str, connection: H3Connection | None = None) -> bool:
    return bool(normalize_output_url(value, connection))


async def download_output(
    url: str,
    dest_path: str,
    connection: H3Connection | None = None,
) -> None:
    conn = _connection(connection)
    target = normalize_output_url(url, conn)
    if not target:
        raise ProviderError("MiniMaxH3 输出 URL 不属于已配置服务")
    timeout = httpx.Timeout(connect=10, read=config.TIMEOUT_DOWNLOAD, write=30, pool=10)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.get(target, headers=_headers(conn))
    except httpx.RequestError as exc:
        raise ProviderError(
            f"MiniMaxH3 视频下载网络异常：{type(exc).__name__}: {exc}",
            retryable=True,
            raw=repr(exc),
        ) from exc
    if response.status_code != 200:
        raise _error_from_response("下载视频", response)
    atomic_write_bytes(dest_path, response.content)


def _probe_target(
    override_base_url: str | None = None,
    connection: H3Connection | None = None,
) -> str:
    target = str(
        override_base_url or _connection(connection).base_url
    ).strip().rstrip("/")
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
    connection: H3Connection | None = None,
) -> dict[str, Any]:
    conn = _connection(connection)
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
    selected_acceleration = conn.acceleration
    selected_profile = conn.turbo_profile
    selected_vae = conn.video_vae
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
            else conn.steps
        ),
        "video_vae_profiles": video_vae_profiles,
        "video_vae": selected_vae,
        "te_speed_available": bool(health.get("te_speed_available")),
    }


async def probe_connection(
    override_base_url: str | None = None,
    connection: H3Connection | None = None,
) -> dict[str, Any]:
    conn = _connection(connection)
    target = _probe_target(override_base_url, conn)
    started = time.time()
    timeout = httpx.Timeout(connect=5, read=30, write=10, pool=5)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            root_response = await client.get(f"{target}/", headers=_headers(conn))
            health_response = await client.get(
                f"{target}/health",
                headers=_headers(conn),
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
        connection=conn,
    )


def probe_connection_sync(
    override_base_url: str | None = None,
    connection: H3Connection | None = None,
) -> dict[str, Any]:
    """Synchronous capability discovery for sync planning boundaries."""
    conn = _connection(connection)
    target = _probe_target(override_base_url, conn)
    started = time.time()
    timeout = httpx.Timeout(connect=5, read=30, write=10, pool=5)
    try:
        with httpx.Client(timeout=timeout) as client:
            root_response = client.get(f"{target}/", headers=_headers(conn))
            health_response = client.get(f"{target}/health", headers=_headers(conn))
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
        connection=conn,
    )


class MiniMaxH3Adapter:
    """MiniMax H3（ComfyUI 私有协议）。

    模块级函数保持原样对外可用——它们既是本适配器的实现，也是既有测试与
    运维脚本的入口；这个类只把它们组织成注册表认得的形状。
    """

    gateway = "minimax_h3"
    serial_generation = True
    wait_meta_keys: tuple[str, ...] = (
        "minimax_h3_generation_started_at",
        "minimax_h3_generation_timeout_s",
        "minimax_h3_acceleration",
        "minimax_h3_turbo_profile",
        "minimax_h3_video_vae",
        "minimax_h3_estimated_generation_s",
        "minimax_h3_provider_status",
        "minimax_h3_provider_stage",
        "minimax_h3_provider_stage_label",
        "minimax_h3_provider_phase",
        "minimax_h3_provider_sequence",
        "minimax_h3_provider_queue_position",
        "minimax_h3_provider_estimated_s",
        "minimax_h3_provider_timings",
    )

    def __init__(
        self,
        *,
        provider: str = "minimax_h3",
        connection: H3Connection | None = None,
    ) -> None:
        # connection 为 None 表示"跟随内置实例的当前配置"：隧道地址改了 settings
        # 之后不必重建适配器，每次调用都重新解析。页面新增的实例则绑定一份固定连接。
        self.provider = provider
        self._connection = connection

    @property
    def connection(self) -> H3Connection:
        return _connection(self._connection)

    async def create_video_task(
        self,
        prompt_text: str,
        *,
        image_urls: list[tuple[str, str]] | None = None,
        video_urls: list[tuple[str, str]] | None = None,
        return_last_frame: bool = False,
        call_meta: dict[str, Any] | None = None,
    ) -> str:
        # H3 不产出尾帧，return_last_frame 在能力快照里已声明为不支持。
        return await create_video_task(
            prompt_text,
            image_urls=image_urls,
            video_urls=video_urls,
            call_meta=call_meta,
            connection=self.connection,
        )

    async def poll_video_task(
        self,
        task_id: str,
        *,
        call_meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await poll_video_task(
            task_id,
            call_meta=call_meta,
            connection=self.connection,
        )

    def owns_task_id(self, task_id: str) -> bool:
        return is_task_id(task_id)

    def owns_output_url(self, url: str) -> bool:
        return is_output_url(url, self.connection)

    async def download_output(self, url: str, dest_path: str) -> None:
        await download_output(url, dest_path, self.connection)

    def capability_snapshot(self, *, provider: str, model: str):
        from app.hiagent import ProviderError
        from app.video_plan.capability_snapshot import (
            failed_minimax_h3_snapshot,
            minimax_h3_snapshot_from_probe,
        )

        try:
            probe = probe_connection_sync(connection=self.connection)
        except ProviderError as exc:
            return failed_minimax_h3_snapshot(
                provider=provider,
                model=model,
                error=exc,
                connection=self.connection,
            )
        return minimax_h3_snapshot_from_probe(
            probe,
            provider=provider,
            model=model,
        )

    def capability_snapshot_is_current(self, snapshot) -> bool:
        from app.video_plan.capability_snapshot import minimax_h3_snapshot_matches_runtime

        return minimax_h3_snapshot_matches_runtime(snapshot, self.connection)

    def prompt_profile(self):
        from app.video_prompt_profiles import MINIMAX_H3_PROFILE

        return MINIMAX_H3_PROFILE

    def estimated_generation_seconds(self, mode: str, duration_s: float) -> int:
        return estimated_generation_seconds(
            mode, duration_s, acceleration=self.connection.acceleration,
        )

    def generation_timeout_seconds(
        self,
        mode: str,
        duration_s: float,
        *,
        expected_seconds: float | None = None,
    ) -> int:
        return generation_timeout_seconds(
            mode,
            duration_s,
            acceleration=self.connection.acceleration,
            expected_seconds=expected_seconds,
        )

    def apply_wait_policy(
        self,
        task_id: str,
        result: dict[str, Any],
        meta: dict[str, Any],
        policy: dict[str, Any],
        *,
        duration_s: float,
        current: float,
    ) -> dict[str, Any]:
        """H3 上报阶段与队列位置，据此把"排队"与"真正在生成"分开计时。"""
        from app import config as app_config

        policy["poll_delay_s"] = poll_interval_seconds(self.connection)
        status = str(result.get("status") or "").strip().lower()
        stage = str(result.get("stage") or "").strip().lower()
        phase = provider_task_phase(result)
        provider_observation = {
            "minimax_h3_provider_status": status,
            "minimax_h3_provider_stage": stage,
            "minimax_h3_provider_stage_label": provider_stage_label(stage),
            "minimax_h3_provider_phase": phase,
            "minimax_h3_provider_sequence": result.get("sequence"),
            "minimax_h3_provider_queue_position": result.get("queue_position"),
            "minimax_h3_provider_estimated_s": result.get("estimated_seconds"),
            "minimax_h3_provider_timings": (
                result.get("timings")
                if isinstance(result.get("timings"), dict)
                else {}
            ),
        }
        for key, value in provider_observation.items():
            if meta.get(key) != value:
                meta[key] = value
                policy["meta_changed"] = True
        policy["stage_progress"] = {
            "provider": "minimax_h3",
            "provider_label": str(result.get("provider_label") or "MiniMax H3"),
            "provider_status": status,
            "provider_phase": phase,
            "provider_stage": stage,
            "provider_stage_label": provider_observation[
                "minimax_h3_provider_stage_label"
            ],
            "provider_sequence": result.get("sequence"),
            "provider_queue_position": result.get("queue_position"),
            "provider_estimated_s": result.get("estimated_seconds"),
            "provider_timings": provider_observation[
                "minimax_h3_provider_timings"
            ],
        }
        if status != "running" or phase != "generating":
            return policy

        started_key = "minimax_h3_generation_started_at"
        try:
            generation_started_at = float(meta.get(started_key) or 0)
        except (TypeError, ValueError):
            generation_started_at = 0.0
        if generation_started_at <= 0:
            generation_started_at = current
            mode = str(
                meta.get("actual_mode")
                or meta.get("mode")
                or meta.get("planned_mode")
                or ""
            )
            try:
                provider_estimated_s = float(result.get("estimated_seconds") or 0)
            except (TypeError, ValueError):
                provider_estimated_s = 0
            try:
                estimated_s = (
                    round(provider_estimated_s)
                    if provider_estimated_s > 0
                    else self.estimated_generation_seconds(mode, duration_s)
                )
                timeout_s = self.generation_timeout_seconds(
                    mode,
                    duration_s,
                    expected_seconds=estimated_s,
                )
            except (TypeError, ValueError):
                timeout_s = float(app_config.VIDEO_PROVIDER_MAX_WAIT)
                estimated_s = 0
            meta.update({
                started_key: generation_started_at,
                "minimax_h3_generation_timeout_s": timeout_s,
                "minimax_h3_acceleration": self.connection.acceleration,
                "minimax_h3_turbo_profile": self.connection.turbo_profile,
                "minimax_h3_video_vae": self.connection.video_vae,
                "minimax_h3_estimated_generation_s": estimated_s,
            })
            policy["meta_changed"] = True
        try:
            generation_timeout_s = float(
                meta.get("minimax_h3_generation_timeout_s")
                or self.generation_timeout_seconds(
                    str(
                        meta.get("actual_mode")
                        or meta.get("mode")
                        or meta.get("planned_mode")
                        or ""
                    ),
                    duration_s,
                )
            )
        except (TypeError, ValueError):
            generation_timeout_s = float(app_config.VIDEO_PROVIDER_MAX_WAIT)
        policy.update({
            "elapsed_s": max(0.0, current - generation_started_at),
            "timeout_s": min(
                float(app_config.VIDEO_PROVIDER_MAX_WAIT),
                generation_timeout_s,
            ),
            "scope": "MiniMaxH3 生成阶段",
        })
        policy["stage_progress"]["provider_generation_started_at"] = (
            generation_started_at
        )
        return policy
