"""HiAgent 网关客户端。全部真实调用，禁止 mock（PRD 原则 P1）；失败必须携带原始报文向上抛（P2）。

API 形态依据 M0 实测（docs/HIAGENT_INTEGRATION.md）：
- chat/completions：OpenAI 兼容；文本模型为推理模型，只读 message.content。
- 视频：POST /contents/generations/tasks 创建（网关无同步参数校验！），GET /tasks/{id} 轮询，
  succeeded 后 content.video_url 7 天过期，必须立即下载。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import inspect
import json
import sqlite3
import shutil
import subprocess
import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

import httpx

from app import config
from app.atomic_io import atomic_write_bytes
from app.db import (finish_provider_call, get_conn, get_setting, log_provider_call,
                    provider_operation_id, start_provider_call)

BAILIAN_TEXT_FREE_MODELS = (
    "qwen3.7-max-2026-06-08",
    "qwen3.7-max-2026-05-20",
    "qwen3.7-max-2026-05-17",
    "qwen3.7-max-preview",
    "qwen3.7-plus-2026-05-26",
)
BAILIAN_TEXT_BASE_MODELS = ("qwen3.7-max", "qwen3.7-plus")
BAILIAN_VLM_FREE_MODELS = ("qwen3.7-plus-2026-05-26",)
BAILIAN_VLM_BASE_MODELS = ("qwen3.7-plus",)
_BAILIAN_FAILED_MODELS: dict[str, set[str]] = {"text": set(), "vlm": set()}
_MEDIA_SEMAPHORES: weakref.WeakKeyDictionary[Any, asyncio.Semaphore] = weakref.WeakKeyDictionary()
_IMAGE_SEMAPHORES: weakref.WeakKeyDictionary[Any, asyncio.Semaphore] = weakref.WeakKeyDictionary()
_VLM_SEMAPHORES: weakref.WeakKeyDictionary[Any, asyncio.Semaphore] = weakref.WeakKeyDictionary()


def _cached_successful_provider_response(
    kind: str,
    model: str,
    payload: dict[str, Any],
    meta: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """恢复时复用同一幂等 operation 的成功响应，避免成功结果因状态竞态丢失。"""
    if not bool((meta or {}).get("reuse_successful_operation")):
        return None
    operation_id = provider_operation_id(kind, model, payload)
    try:
        row = get_conn().execute(
            "SELECT id,response_json FROM provider_calls "
            "WHERE operation_id=? AND status IN ('OK','SUCCESS','SUCCEEDED') "
            "AND response_json IS NOT NULL ORDER BY id DESC LIMIT 1",
            (operation_id,),
        ).fetchone()
        if not row or not row["response_json"]:
            return None
        value = json.loads(row["response_json"])
        if not isinstance(value, dict):
            return None
        log_provider_call(
            "provider_cache_hit",
            model,
            "REUSED",
            None,
            0,
            meta={
                **(meta or {}),
                "operation_id": operation_id,
                "source_provider_call_id": int(row["id"]),
            },
        )
        return value
    except (json.JSONDecodeError, sqlite3.Error, TypeError, ValueError):
        return None


class ProviderError(Exception):
    """对外调用失败。message 面向 UI，包含分类结论 + 原始报文摘要。"""

    def __init__(self, message: str, *, retryable: bool = False, raw: str = "",
                 timeout_phase: str | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.raw = raw[:500]
        self.timeout_phase = timeout_phase


def _channel_semaphore(
    store: weakref.WeakKeyDictionary[Any, asyncio.Semaphore],
    limit: int,
) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = store.get(loop)
    desired = max(1, int(limit))
    if semaphore is None or getattr(semaphore, "_mj_limit", None) != desired:
        semaphore = asyncio.Semaphore(desired)
        semaphore._mj_limit = desired  # type: ignore[attr-defined]
        store[loop] = semaphore
    return semaphore


def _media_semaphore() -> asyncio.Semaphore:
    """兼容旧调用：回退到 image 通道。"""
    return _image_semaphore()


def _image_semaphore() -> asyncio.Semaphore:
    try:
        from app.media_pipeline.concurrency import channel_limit
        from app.media_pipeline import stages as media_stages
        limit = channel_limit(media_stages.RESOURCE_IMAGE)
    except Exception:  # noqa: BLE001
        limit = getattr(config, "IMAGE_REQUEST_CONCURRENCY", config.MEDIA_REQUEST_CONCURRENCY)
    return _channel_semaphore(_IMAGE_SEMAPHORES, limit)


def _vlm_semaphore() -> asyncio.Semaphore:
    try:
        from app.media_pipeline.concurrency import channel_limit
        from app.media_pipeline import stages as media_stages
        limit = channel_limit(media_stages.RESOURCE_VLM)
    except Exception:  # noqa: BLE001
        limit = getattr(config, "VLM_REQUEST_CONCURRENCY", config.MEDIA_REQUEST_CONCURRENCY)
    return _channel_semaphore(_VLM_SEMAPHORES, limit)


def _timeout_phase(exc: httpx.TimeoutException) -> str:
    if isinstance(exc, httpx.WriteTimeout):
        return "write"
    if isinstance(exc, httpx.ReadTimeout):
        return "read"
    if isinstance(exc, httpx.ConnectTimeout):
        return "connect"
    if isinstance(exc, httpx.PoolTimeout):
        return "pool"
    return "unknown"


def _request_size_bytes(payload: Any) -> int:
    try:
        return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def _compress_image_bytes(raw: bytes) -> bytes:
    """用项目已依赖的 ffmpeg 缩小输入图。压缩失败或反而变大时保留原图。"""
    if not raw or not shutil.which("ffmpeg"):
        return raw
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
        "-vf", f"scale={config.MEDIA_INPUT_MAX_EDGE}:{config.MEDIA_INPUT_MAX_EDGE}:force_original_aspect_ratio=decrease",
        "-frames:v", "1", "-q:v", str(config.MEDIA_INPUT_JPEG_QUALITY),
        "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
    ]
    try:
        result = subprocess.run(command, input=raw, capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return raw
    return result.stdout if result.returncode == 0 and 0 < len(result.stdout) < len(raw) else raw


async def _prepare_image_data_urls(values: list[str]) -> tuple[list[str], dict[str, Any]]:
    prepared: list[str] = []
    original_bytes = 0
    sent_bytes = 0
    compressed_count = 0
    for value in values:
        if not value.startswith("data:") or ";base64," not in value[:100]:
            prepared.append(value)
            continue
        _, encoded = value.split(";base64,", 1)
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            prepared.append(value)
            continue
        compressed = await asyncio.to_thread(_compress_image_bytes, raw)
        original_bytes += len(raw)
        sent_bytes += len(compressed)
        if len(compressed) < len(raw):
            compressed_count += 1
            prepared.append("data:image/jpeg;base64," + base64.b64encode(compressed).decode("ascii"))
        else:
            prepared.append(value)
    stats: dict[str, Any] = {
        "media_input_count": len(values),
        "media_input_bytes_original": original_bytes,
        "media_input_bytes_sent": sent_bytes,
        "media_input_compressed_count": compressed_count,
    }
    if original_bytes:
        stats["media_input_compression_ratio"] = round(sent_bytes / original_bytes, 3)
    return prepared, stats


def _classify_http_error(status: int, body: str, key_name: str = "HIAGENT_API_KEY") -> ProviderError:
    lowered = body.lower()
    if status in (401, 403):
        if "no access to model" in lowered:
            return ProviderError(f"凭证有效，但模型未授权/未开通（HTTP {status}）：{body[:300]}", raw=body)
        return ProviderError(f"鉴权失败，请检查 .env 中的 {key_name}（HTTP {status}）：{body[:300]}", raw=body)
    if status == 429:
        return ProviderError(f"网关限流（HTTP 429）：{body[:200]}", retryable=True, raw=body)
    if status >= 500:
        return ProviderError(f"网关/上游故障（HTTP {status}）：{body[:300]}", retryable=True, raw=body)
    return ProviderError(f"请求被拒绝（HTTP {status}）：{body[:300]}", raw=body)


def _headers() -> dict[str, str]:
    if not config.HIAGENT_API_KEY:
        raise ProviderError("未配置 HIAGENT_API_KEY，请在项目根目录 .env 中填写")
    return {"Authorization": f"Bearer {config.HIAGENT_API_KEY}", "Content-Type": "application/json"}


def _deepseek_headers() -> dict[str, str]:
    if not config.DEEPSEEK_API_KEY:
        raise ProviderError("未配置 DEEPSEEK_API_KEY，请在项目根目录 .env 中填写，或在监制房切回其他文本模型")
    return {"Authorization": f"Bearer {config.DEEPSEEK_API_KEY}", "Content-Type": "application/json"}


def _zhipu_headers() -> dict[str, str]:
    if not config.ZHIPU_API_KEY:
        raise ProviderError("未配置 ZHIPU_API_KEY，请在项目根目录 .env 中填写，或在监制房切回其他文本模型")
    return {"Authorization": f"Bearer {config.ZHIPU_API_KEY}", "Content-Type": "application/json"}


def _model_route() -> str:
    return (get_setting("model_route") or "hiagent").strip()


def active_provider(kind: str) -> str:
    configured = (get_setting(f"model_{kind}_provider") or "").strip()
    if configured.startswith("custom:"):
        return configured
    if kind == "text" and configured in {"hiagent", "openrouter", "bailian", "deepseek", "zhipu"}:
        return configured
    if kind == "vlm" and configured in {"hiagent", "openrouter", "bailian"}:
        return configured
    if kind in {"text", "vlm"}:
        route = _model_route()
        if route in {"hiagent", "openrouter"}:
            return route
    return "hiagent"


def _model_setting(key: str, fallback: str) -> str:
    return (get_setting(key) or fallback or "").strip()


def _model_connection(provider: str, model: str, fallback_url: str = "", fallback_key: str = "") -> tuple[str, dict[str, str]]:
    """读取单模型连接信息；旧环境变量仅作为尚未迁移时的兼容兜底。"""
    try:
        custom = json.loads(get_setting("custom_models") or "[]")
    except (TypeError, json.JSONDecodeError):
        custom = []
    item = next((m for m in custom if m.get("provider") == provider and m.get("model") == model), {})
    item_id = item.get("id") or f"builtin:{provider}:{model}"
    try:
        credentials = json.loads(get_setting("model_credentials") or "{}")
    except (TypeError, json.JSONDecodeError):
        credentials = {}
    saved = credentials.get(item_id, {}) if isinstance(credentials, dict) else {}
    base_url = str(saved.get("base_url") or item.get("base_url") or fallback_url).strip().rstrip("/")
    api_key = str(saved.get("api_key") or item.get("api_key") or fallback_key).strip()
    if not base_url:
        raise ProviderError(f"模型 {model} 未配置 Base URL")
    if not api_key:
        raise ProviderError(f"模型 {model} 未配置 API Key，请在模型中心配置")
    return base_url, {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _absolute_provider_url(value: str, base_url: str) -> str:
    """媒体网关有时返回站内相对地址，下载前统一补成完整 URL。"""
    value = (value or "").strip()
    if not value or value.startswith(("http://", "https://", "data:")):
        return value
    return urljoin(base_url.rstrip("/") + "/", value)


def active_model(kind: str, provider: str | None = None) -> str:
    provider = provider or active_provider(kind)
    if provider.startswith("custom:"):
        try:
            custom = json.loads(get_setting("custom_models") or "[]")
        except (TypeError, json.JSONDecodeError):
            custom = []
        item = next((m for m in custom if m.get("provider") == provider and kind in m.get("kinds", [])), None)
        return str(item.get("model") or "").strip() if item else ""
    if provider == "zhipu":
        if kind == "text":
            return _model_setting("zhipu_model_text", config.ZHIPU_MODEL_TEXT)
        return ""
    if provider == "deepseek":
        if kind == "text":
            return _model_setting("deepseek_model_text", config.DEEPSEEK_MODEL_TEXT)
        return ""
    if provider == "bailian":
        if kind == "text":
            return _model_setting("bailian_model_text", config.BAILIAN_MODEL_TEXT)
        if kind == "vlm":
            return _model_setting("bailian_model_vlm", config.BAILIAN_MODEL_VLM)
        return ""
    if provider == "openrouter":
        if kind == "text":
            return _model_setting("openrouter_model_text", config.OPENROUTER_MODEL_TEXT)
        if kind == "vlm":
            return _model_setting("openrouter_model_vlm", config.OPENROUTER_MODEL_VLM)
        return ""
    if kind == "text":
        return _model_setting("hiagent_model_text", config.MODEL_TEXT)
    if kind == "vlm":
        return _model_setting("hiagent_model_vlm", config.MODEL_VLM)
    if kind == "video":
        return _model_setting("hiagent_model_video", config.MODEL_VIDEO)
    if kind == "image":
        return _model_setting("hiagent_model_image", config.MODEL_IMAGE)
    return ""


def _dedupe_models(models: list[str] | tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for model in models:
        model = (model or "").strip()
        if model and model not in seen:
            seen.add(model)
            ordered.append(model)
    return ordered


def _bailian_model_groups(kind: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if kind == "vlm":
        return BAILIAN_VLM_FREE_MODELS, BAILIAN_VLM_BASE_MODELS
    return BAILIAN_TEXT_FREE_MODELS, BAILIAN_TEXT_BASE_MODELS


def _remember_bailian_failure(kind: str, model: str) -> None:
    _, base_models = _bailian_model_groups(kind)
    if model.startswith("qwen3.7") and model not in base_models:
        _BAILIAN_FAILED_MODELS.setdefault(kind, set()).add(model)


def _bailian_fallback_models(kind: str, preferred: str) -> list[str]:
    preferred = (preferred or "").strip()
    free_models, base_models = _bailian_model_groups(kind)
    if preferred and not preferred.startswith("qwen3.7"):
        return [preferred]
    candidates = _dedupe_models([preferred, *free_models, *base_models])
    failed = _BAILIAN_FAILED_MODELS.setdefault(kind, set())
    return [model for model in candidates if model not in failed or model in base_models]


def _chat_content(data: dict, *, label: str = "chat") -> str:
    """从 OpenAI 兼容响应取 message.content（推理字段一律丢弃）。
    个别 provider 经 OpenRouter 返回 content 为分块列表，这里兜底拼接其中的文本块。"""
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise ProviderError(f"{label} 响应结构异常：{json.dumps(data, ensure_ascii=False)[:300]}") from exc
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text")
    return content or ""


def _reasoning_used_all_output_budget(data: dict) -> bool:
    """判断推理模型是否在生成正文前已用完输出预算。

    OpenRouter 的 reasoning 与 message.content 共用 max_tokens。部分模型会在
    finish_reason=length 时只返回 reasoning、将 content 留为 null。
    """
    try:
        choice = data["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError):
        return False
    reasoning = message.get("reasoning") or message.get("reasoning_content")
    return choice.get("finish_reason") == "length" and bool(reasoning)


def _empty_content_detail(data: dict) -> str:
    try:
        choice = data["choices"][0]
        message = choice.get("message") or {}
    except (KeyError, IndexError, TypeError):
        return "响应结构中无可用 choice"
    finish_reason = choice.get("finish_reason") or "unknown"
    reasoning_present = bool(message.get("reasoning") or message.get("reasoning_content"))
    usage = data.get("usage") or {}
    completion_tokens = usage.get("completion_tokens")
    return (f"finish_reason={finish_reason}, reasoning_present={reasoning_present}, "
            f"completion_tokens={completion_tokens}")


def _infer_callsite_meta() -> dict[str, Any]:
    frame = inspect.currentframe()
    try:
        current = frame.f_back if frame else None
        while current:
            module = str(current.f_globals.get("__name__") or "").strip()
            if module and module != __name__:
                file_path = str(current.f_code.co_filename or "")
                rel_file = file_path
                marker = "/app/"
                if marker in file_path:
                    rel_file = "app/" + file_path.split(marker, 1)[1]
                return {
                    "caller_module": module,
                    "caller_function": current.f_code.co_name,
                    "caller_file": rel_file,
                    "caller_line": current.f_lineno,
                }
            current = current.f_back
    finally:
        del frame
    return {}


def _merge_call_meta(meta: dict | None) -> dict | None:
    merged = dict(meta or {})
    inferred = _infer_callsite_meta()
    for key, value in inferred.items():
        merged.setdefault(key, value)
    if merged.get("caller_module") and merged.get("caller_function"):
        merged.setdefault("initiator", f"{merged['caller_module']}.{merged['caller_function']}")
    return merged or None


async def _post_json(client: httpx.AsyncClient, url: str, payload: dict, *,
                     kind: str, model: str, retries: int = 2,
                     headers: dict | None = None, key_name: str = "HIAGENT_API_KEY",
                     meta: dict | None = None,
                     idempotency_key: str | None = None) -> dict:
    last_err: ProviderError | None = None
    merged_meta = _merge_call_meta(meta)
    req_headers = dict(headers if headers is not None else _headers())
    if idempotency_key:
        # Providers that implement the conventional header deduplicate the
        # accept-before-local-commit crash window; providers that ignore it still
        # receive the same durable operation identifier in observability metadata.
        req_headers["Idempotency-Key"] = idempotency_key
    request_bytes = _request_size_bytes(payload)
    for attempt in range(retries + 1):
        start = time.time()
        attempt_meta = {
            "http_attempt": attempt + 1,
            "http_attempts_max": retries + 1,
            "request_bytes": request_bytes,
            **(merged_meta or {}),
        }
        call_id = start_provider_call(kind, model, meta=attempt_meta, request_json=payload)
        try:
            resp = await client.post(url, json=payload, headers=req_headers)
            latency = int((time.time() - start) * 1000)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError as exc:
                    snippet = (resp.text or "")[:500]
                    err = ProviderError(
                        f"上游返回非法 JSON：{exc}",
                        retryable=True,
                        raw=snippet,
                    )
                    finish_provider_call(
                        call_id, "FAILED", 200, latency, error=str(err),
                        response_json={"status_code": 200, "body": snippet},
                    )
                    last_err = err
                    continue
                finish_provider_call(call_id, "OK", 200, latency, response_json=data)
                return data
            err = _classify_http_error(resp.status_code, resp.text, key_name)
            finish_provider_call(
                call_id, "FAILED", resp.status_code, latency, error=str(err),
                response_json={"status_code": resp.status_code, "body": resp.text})
            if not err.retryable:
                raise err
            last_err = err
            # Harness 已为文本模型提供分钟级指数退避。429 若仍在 adapter 内按
            # 1.5s/3s 立即重放，只会在同一个 TPM 窗口连续失败，并放大限流。
            if (
                resp.status_code == 429
                and kind == "chat"
                and (merged_meta or {}).get("gateway") == "execution_harness"
            ):
                raise err
        except ProviderError:
            raise
        except httpx.TimeoutException as exc:
            latency = int((time.time() - start) * 1000)
            phase = _timeout_phase(exc)
            detail = (f"{type(exc).__name__}(phase={phase}, latency_ms={latency}, "
                      f"request_bytes={request_bytes}): {exc!r}")
            last_err = ProviderError(
                f"调用{phase}阶段超时（{latency}ms，请求 {request_bytes} bytes）",
                retryable=True, raw=detail, timeout_phase=phase)
            finish_provider_call(call_id, "TIMEOUT", None, latency, error=detail)
            # Base64 大图写超时时，原样重传三次只会持续占满上行。
            # 立即交给上层：图生图可快速降级为无种子生成，VLM 则明确失败。
            # Harness 文本请求的 read timeout 也必须交给外层分钟级退避：
            # 服务端可能仍在生成，adapter 立即重放会制造并行幽灵请求并吃满 TPM。
            if phase == "write" or (
                phase == "read"
                and kind == "chat"
                and (merged_meta or {}).get("gateway") == "execution_harness"
            ):
                raise last_err
        except httpx.HTTPError as exc:
            latency = int((time.time() - start) * 1000)
            last_err = ProviderError(f"网络错误：{exc}", retryable=True)
            finish_provider_call(call_id, "NETWORK_ERROR", None, latency, error=str(exc))
        except Exception as exc:
            latency = int((time.time() - start) * 1000)
            finish_provider_call(call_id, "FAILED", None, latency, error=str(exc))
            raise
        if attempt < retries:
            await asyncio.sleep(1.5 * (2 ** attempt))
    assert last_err is not None
    raise last_err


async def _post_bailian_chat_with_fallback(client: httpx.AsyncClient, payload: dict, *,
                                           fallback_kind: str, log_kind: str,
                                           preferred_model: str,
                                           meta: dict | None = None) -> tuple[dict, str]:
    base_url, headers = _model_connection("bailian", preferred_model, config.BAILIAN_BASE_URL, config.BAILIAN_API_KEY)
    url = f"{base_url}/chat/completions"
    models = _bailian_fallback_models(fallback_kind, preferred_model)
    errors: list[str] = []
    last_err: ProviderError | None = None
    for candidate in models:
        attempt_payload = {**payload, "model": candidate}
        try:
            data = await _post_json(client, url, attempt_payload, kind=log_kind, model=candidate,
                                    headers=headers, key_name="BAILIAN_API_KEY", meta=meta)
            return data, candidate
        except ProviderError as exc:
            _remember_bailian_failure(fallback_kind, candidate)
            last_err = exc
            errors.append(f"{candidate}: {exc}")
    detail = "；".join(errors)[:500]
    if last_err is None:
        raise ProviderError("百炼模型候选列表为空，请检查模型配置")
    raise ProviderError(f"百炼 {fallback_kind} 模型全部请求失败，已按降级序列尝试：{detail}",
                        retryable=last_err.retryable, raw=last_err.raw)


async def _stream_bailian_chat_with_fallback(
    client: httpx.AsyncClient, payload: dict, *, fallback_kind: str, log_kind: str,
    preferred_model: str, meta: dict | None,
    on_token: Callable[[str, str], None],
) -> tuple[dict, str]:
    """百炼的逐模型流式降级链。

    某候选在尚未产出 token 时失败，可安全尝试下一个模型；一旦已经向前端
    推送过内容就立即报错，禁止换模型重放导致文字重复。
    """
    base_url, headers = _model_connection(
        "bailian", preferred_model, config.BAILIAN_BASE_URL, config.BAILIAN_API_KEY)
    url = f"{base_url}/chat/completions"
    models = _bailian_fallback_models(fallback_kind, preferred_model)
    errors: list[str] = []
    last_err: ProviderError | None = None
    emitted = 0

    def _forward(kind: str, text: str) -> None:
        nonlocal emitted
        emitted += 1
        on_token(kind, text)

    for candidate in models:
        attempt_payload = {**payload, "model": candidate}
        try:
            data = await _stream_or_fallback(
                client, url, attempt_payload, kind=log_kind, model=candidate,
                headers=headers, key_name="BAILIAN_API_KEY", meta=meta,
                on_token=_forward,
            )
            return data, candidate
        except ProviderError as exc:
            if emitted:
                raise
            _remember_bailian_failure(fallback_kind, candidate)
            last_err = exc
            errors.append(f"{candidate}: {exc}")
    detail = "；".join(errors)[:500]
    if last_err is None:
        raise ProviderError("百炼模型候选列表为空，请检查模型配置")
    raise ProviderError(
        f"百炼 {fallback_kind} 模型全部请求失败，已按降级序列尝试：{detail}",
        retryable=last_err.retryable, raw=last_err.raw,
    )


async def _chat_with_reasoning_fallback(client: httpx.AsyncClient, url: str, payload: dict, *,
                                     kind: str, model: str, headers: dict | None, key_name: str,
                                     temperature: float, call_meta: dict | None = None) -> str:
    """封装推理模型的降级重试逻辑：若首轮因推理过长导致 content 为空，则关闭推理重试一次。"""
    data = await _post_json(client, url, payload, kind=kind, model=model,
                            headers=headers, key_name=key_name, meta=call_meta)
    content = _chat_content(data, label=kind)
    if not content.strip() and _reasoning_used_all_output_budget(data):
        # 思考过长时重试；移除 OpenRouter reasoning 参数，使用普通生成。
        fallback_payload = {**payload, "temperature": temperature}
        # 移除 OpenRouter 风格的 reasoning 参数
        fallback_payload.pop("reasoning", None)
        fallback_meta = {
            **(call_meta or {}),
            "reasoning_fallback": True,
            "reasoning_fallback_cause": "reasoning_budget_exhausted",
        }
        data = await _post_json(client, url, fallback_payload, kind=kind, model=model, retries=0,
                                headers=headers, key_name=key_name, meta=fallback_meta)
        content = _chat_content(data, label=f"{kind} reasoning fallback")
    if not content.strip():
        raise ProviderError(f"模型返回空内容（content 为空；{_empty_content_detail(data)}）")
    return content


def _chat_read_timeout_s(call_meta: dict | None) -> float:
    """为长结构化生成使用独立读超时，其他文本请求保持通用上限。"""
    stage_key = str((call_meta or {}).get("stage_key") or "").strip().lower()
    stage = str((call_meta or {}).get("stage") or "").strip().lower()
    if stage_key == "storyboard_outline":
        return max(
            config.TIMEOUT_CHAT_READ,
            config.TIMEOUT_CHAT_STORYBOARD_OUTLINE_READ,
        )
    if "baseline" in stage:
        return max(config.TIMEOUT_CHAT_READ, config.TIMEOUT_CHAT_BASELINE_READ)
    return config.TIMEOUT_CHAT_READ


async def chat(messages: list[dict], *, model: str | None = None, temperature: float = 0.7,
               max_tokens: int = 65535, call_meta: dict | None = None) -> str:
    """文本 LLM 对话，返回 message.content（推理模型的 reasoning 一律丢弃）。
    按设置在火山 HiAgent、OpenRouter、阿里云百炼、DeepSeek、智谱官方 API 之间路由（后两者仅文本，
    图像/视频始终走火山）。"""
    timeout = httpx.Timeout(connect=10, read=_chat_read_timeout_s(call_meta), write=30, pool=10)
    provider = active_provider("text")
    async with httpx.AsyncClient(timeout=timeout) as client:
        if provider == "openrouter":
            or_model = active_model("text", "openrouter")
            base_url, model_headers = _model_connection("openrouter", or_model, config.OPENROUTER_BASE_URL, config.OPENROUTER_API_KEY)
            payload: dict[str, Any] = {"model": or_model, "messages": messages, "max_tokens": max_tokens}
            effort = (config.OPENROUTER_TEXT_REASONING_EFFORT or "").strip().lower()
            if effort and effort != "none":
                payload["reasoning"] = {"effort": effort}
            else:
                payload["temperature"] = temperature
            content = await _chat_with_reasoning_fallback(
                client, f"{base_url}/chat/completions", payload,
                kind="chat", model=or_model, headers=model_headers,
                key_name="OPENROUTER_API_KEY", temperature=temperature, call_meta=call_meta)
        elif provider == "bailian":
            bailian_model = active_model("text", "bailian")
            payload = {"messages": messages, "temperature": temperature, "max_tokens": max_tokens}
            data, _ = await _post_bailian_chat_with_fallback(
                client, payload, fallback_kind="text", log_kind="chat",
                preferred_model=bailian_model, meta=call_meta)
            content = _chat_content(data, label="chat")
        elif provider == "deepseek":
            deepseek_model = active_model("text", "deepseek")
            try:
                base_url, model_headers = _model_connection("deepseek", deepseek_model, config.DEEPSEEK_BASE_URL, config.DEEPSEEK_API_KEY)
            except ProviderError:
                base_url, model_headers = config.DEEPSEEK_BASE_URL, _deepseek_headers()
            payload = {"model": deepseek_model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
            content = await _chat_with_reasoning_fallback(
                client, f"{base_url}/chat/completions", payload,
                kind="chat", model=deepseek_model, headers=model_headers,
                key_name="DEEPSEEK_API_KEY", temperature=temperature, call_meta=call_meta)
        elif provider == "zhipu":
            zhipu_model = active_model("text", "zhipu")
            try:
                base_url, model_headers = _model_connection("zhipu", zhipu_model, config.ZHIPU_BASE_URL, config.ZHIPU_API_KEY)
            except ProviderError:
                base_url, model_headers = config.ZHIPU_BASE_URL, _zhipu_headers()
            payload = {"model": zhipu_model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
            content = await _chat_with_reasoning_fallback(
                client, f"{base_url}/chat/completions", payload,
                kind="chat", model=zhipu_model, headers=model_headers,
                key_name="ZHIPU_API_KEY", temperature=temperature, call_meta=call_meta)
        elif provider.startswith("custom:"):
            custom_model = active_model("text", provider)
            base_url, headers = _model_connection(provider, custom_model)
            payload = {"model": custom_model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
            data = _cached_successful_provider_response(
                "chat", custom_model, payload, call_meta,
            )
            if data is None:
                data = await _post_json(client, f"{base_url}/chat/completions", payload,
                                        kind="chat", model=custom_model, headers=headers,
                                        key_name=f"model:{custom_model}", meta=call_meta)
            content = _chat_content(data, label="custom chat")
        else:
            model = model or active_model("text", "hiagent")
            base_url, model_headers = _model_connection("hiagent", model, config.HIAGENT_BASE_URL, config.HIAGENT_API_KEY)
            payload = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
            data = _cached_successful_provider_response("chat", model, payload, call_meta)
            if data is None:
                data = await _post_json(client, f"{base_url}/chat/completions", payload,
                                        kind="chat", model=model, headers=model_headers,
                                        key_name=f"model:{model}", meta=call_meta)
            content = _chat_content(data, label="chat")

    if not content or not content.strip():
        raise ProviderError(f"模型返回空内容（content 为空；{_empty_content_detail(data)}）")
    return content


# ---------------------------------------------------------------------------
# 原生工具调用（OpenAI function calling）。现有 `chat` 保持不变；此处新增，供对话
# Agent 编排器使用。工具*执行*仍由 Command Bus 负责，本层只做协议解析。
# ---------------------------------------------------------------------------

# 已知支持 OpenAI 原生 function calling 的文本供应商。其余（或网关能力未知）时可通过
# 设置 agent_native_tools=off 强制回退到手写 JSON 协议（见 _chat_tools_via_json_protocol）。
_NATIVE_TOOL_PROVIDERS = frozenset({"openrouter", "bailian", "deepseek", "zhipu", "hiagent"})

_JSON_PROTOCOL_INSTRUCTION = (
    "本网关不支持原生工具调用。请严格只返回一个 JSON 对象，不要 Markdown 代码块或任何额外文字：\n"
    '{"reply": "给用户看的简短中文说明", "tool_calls": [{"tool": "工具名", "arguments": {...}}], "done": true 或 false}\n'
    "tool_calls 最多 1 个元素；若已可回答用户或无更多可执行动作，tool_calls 传空数组并令 done=true。"
)


@dataclass
class ToolCall:
    """模型请求的一次工具调用（已解析）。"""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    # 原始 arguments JSON 串：回填 assistant 消息时优先使用，避免重新序列化产生漂移。
    arguments_raw: str | None = None


@dataclass
class AssistantTurn:
    """一次带工具能力的模型回合结果。tool_calls 为空表示这是最终回复（content）。

    reasoning：推理模型的思考过程原文（OpenAI 兼容 `reasoning`/`reasoning_content`）。
    过去被一律丢弃；现在保留下来供对话助手展示「思考过程」。
    """

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    reasoning: str = ""


def _provider_supports_tools(provider: str) -> bool:
    override = (get_setting("agent_native_tools") or "").strip().lower()
    if override in {"0", "off", "false", "no", "disabled"}:
        return False
    if override in {"1", "on", "true", "yes", "enabled"}:
        return True
    if provider.startswith("custom:"):
        return True
    return provider in _NATIVE_TOOL_PROVIDERS


def _looks_like_tools_unsupported(exc: ProviderError) -> bool:
    """网关以客户端错误明确拒绝 tools 字段时才回退，避免把限流/故障误判为不支持。"""
    if exc.retryable:
        return False
    blob = f"{exc} {exc.raw}".lower()
    return ("tool" in blob or "function" in blob) and (
        "support" in blob or "unknown" in blob or "unrecognized" in blob or "invalid" in blob
    )


def _resolve_text_connection(
    provider: str, model_override: str | None = None
) -> tuple[str, str, dict[str, str], str]:
    """返回 (chat_completions_url, model, headers, key_name)。bailian 需多模型回退，另行处理。"""
    if provider == "openrouter":
        model = active_model("text", "openrouter")
        base_url, headers = _model_connection(
            "openrouter", model, config.OPENROUTER_BASE_URL, config.OPENROUTER_API_KEY)
        return f"{base_url}/chat/completions", model, headers, "OPENROUTER_API_KEY"
    if provider == "deepseek":
        model = active_model("text", "deepseek")
        try:
            base_url, headers = _model_connection(
                "deepseek", model, config.DEEPSEEK_BASE_URL, config.DEEPSEEK_API_KEY)
        except ProviderError:
            base_url, headers = config.DEEPSEEK_BASE_URL, _deepseek_headers()
        return f"{base_url}/chat/completions", model, headers, "DEEPSEEK_API_KEY"
    if provider == "zhipu":
        model = active_model("text", "zhipu")
        try:
            base_url, headers = _model_connection(
                "zhipu", model, config.ZHIPU_BASE_URL, config.ZHIPU_API_KEY)
        except ProviderError:
            base_url, headers = config.ZHIPU_BASE_URL, _zhipu_headers()
        return f"{base_url}/chat/completions", model, headers, "ZHIPU_API_KEY"
    if provider.startswith("custom:"):
        model = active_model("text", provider)
        base_url, headers = _model_connection(provider, model)
        return f"{base_url}/chat/completions", model, headers, f"model:{model}"
    model = model_override or active_model("text", "hiagent")
    base_url, headers = _model_connection("hiagent", model, config.HIAGENT_BASE_URL, config.HIAGENT_API_KEY)
    return f"{base_url}/chat/completions", model, headers, f"model:{model}"


def _parse_tool_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw or not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        try:
            from app.schemas import extract_json

            parsed = extract_json(raw)
        except ValueError:
            return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_assistant_turn(data: dict, *, label: str) -> AssistantTurn:
    try:
        choice = data["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError(f"{label} 响应结构异常：{json.dumps(data, ensure_ascii=False)[:300]}") from exc
    content = message.get("content") or ""
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text")
    tool_calls: list[ToolCall] = []
    for idx, raw_call in enumerate(message.get("tool_calls") or []):
        if not isinstance(raw_call, dict):
            continue
        fn = raw_call.get("function") or {}
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        args_raw = fn.get("arguments")
        tool_calls.append(ToolCall(
            id=str(raw_call.get("id") or f"call_{idx}"),
            name=name,
            arguments=_parse_tool_arguments(args_raw),
            arguments_raw=args_raw if isinstance(args_raw, str) else None,
        ))
    if not tool_calls and not (content or "").strip():
        raise ProviderError(f"模型返回空内容且无工具调用（{_empty_content_detail(data)}）")
    reasoning = message.get("reasoning") or message.get("reasoning_content") or ""
    return AssistantTurn(
        content=content or "", tool_calls=tool_calls, finish_reason=choice.get("finish_reason"),
        reasoning=reasoning if isinstance(reasoning, str) else "",
    )


def _tools_as_text(tools: list[dict[str, Any]]) -> str:
    lines = ["可用工具（tool 字段填工具名，arguments 按其参数）："]
    for tool in tools:
        fn = tool.get("function") or {}
        params = (fn.get("parameters") or {}).get("properties") or {}
        arg_names = "、".join(params.keys()) or "无"
        lines.append(f"  - {fn.get('name')}（参数：{arg_names}）：{fn.get('description', '')}")
    return "\n".join(lines)


def _flatten_tool_messages(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """把原生格式（含 role=tool、assistant.tool_calls）压平成纯文本对话，供 JSON 回退协议使用。"""
    flat: list[dict[str, str]] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
        if role == "tool":
            flat.append({"role": "user", "content": f"[工具结果] {text}"})
        elif role == "assistant" and msg.get("tool_calls"):
            names = "、".join(str((tc.get("function") or {}).get("name") or "") for tc in msg["tool_calls"])
            flat.append({"role": "assistant", "content": f"{text}（已请求调用工具：{names}）".strip()})
        else:
            flat.append({"role": role or "user", "content": text or ""})
    return flat


async def _chat_tools_via_json_protocol(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]], *,
    temperature: float, max_tokens: int, call_meta: dict | None,
    on_token: Callable[[str, str], None] | None = None,
) -> AssistantTurn:
    """回退协议：网关不支持原生 tools 时，用手写 JSON 协议模拟一次工具调用回合。"""
    from app.schemas import extract_json

    flat = _flatten_tool_messages(messages)
    flat.append({"role": "system", "content": _JSON_PROTOCOL_INSTRUCTION + "\n" + _tools_as_text(tools)})
    fallback_meta = {**(call_meta or {}), "tool_protocol": "json_fallback"}
    want_stream = on_token is not None and _streaming_enabled()
    streamed_reply = False
    if want_stream:
        assert on_token is not None
        reply_streamer = _JsonReplyStreamer(on_token)
        raw = await _stream_plain_chat(
            flat, temperature=temperature, max_tokens=max_tokens,
            call_meta=fallback_meta, on_token=reply_streamer.feed,
        )
        streamed_reply = reply_streamer.emitted
    else:
        raw = await chat(
            flat, temperature=temperature, max_tokens=max_tokens,
            call_meta=fallback_meta)
    try:
        plan = extract_json(raw)
    except ValueError:
        if want_stream and on_token is not None and not streamed_reply and raw.strip():
            on_token("content", raw.strip())
        return AssistantTurn(content=raw.strip(), tool_calls=[])
    tool_calls: list[ToolCall] = []
    raw_calls = plan.get("tool_calls")
    if isinstance(raw_calls, list):
        for idx, call in enumerate(raw_calls):
            if not isinstance(call, dict):
                continue
            name = str(call.get("tool") or call.get("name") or "").strip()
            if not name:
                continue
            args = call.get("arguments")
            tool_calls.append(ToolCall(id=f"call_{idx}", name=name,
                                       arguments=args if isinstance(args, dict) else {}))
    reply = str(plan.get("reply") or "").strip()
    # 流式网关在首帧前降级为非流式时不会触发 feed，收尾时补发一次，
    # 保证前端的事件契约不因 provider 能力而改变。
    if want_stream and on_token is not None and not streamed_reply and reply:
        on_token("content", reply)
    return AssistantTurn(content=reply, tool_calls=tool_calls)


class _JsonReplyStreamer:
    """从 JSON 协议的原始 token 中只抽取 `reply` 字符串，避免把工具 JSON 露给 UI。"""

    def __init__(self, on_token: Callable[[str, str], None]) -> None:
        self._on_token = on_token
        self._raw = ""
        self._sent = 0

    @property
    def emitted(self) -> bool:
        return self._sent > 0

    @staticmethod
    def _decoded_reply_prefix(raw: str) -> str:
        import re

        match = re.search(r'"reply"\s*:\s*"', raw)
        if not match:
            return ""
        source = raw[match.end():]
        out: list[str] = []
        i = 0
        escapes = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f",
                   "n": "\n", "r": "\r", "t": "\t"}
        while i < len(source):
            char = source[i]
            if char == '"':
                break
            if char != "\\":
                out.append(char)
                i += 1
                continue
            if i + 1 >= len(source):
                break
            marker = source[i + 1]
            if marker in escapes:
                out.append(escapes[marker])
                i += 2
                continue
            if marker == "u":
                digits = source[i + 2:i + 6]
                if len(digits) < 4:
                    break
                try:
                    out.append(chr(int(digits, 16)))
                except ValueError:
                    break
                i += 6
                continue
            # 非法转义：不猜测，等最终 extract_json 给出明确结果。
            break
        return "".join(out)

    def feed(self, _kind: str, text: str) -> None:
        if _kind != "content":
            return
        self._raw += text
        decoded = self._decoded_reply_prefix(self._raw)
        if len(decoded) <= self._sent:
            return
        delta = decoded[self._sent:]
        self._sent = len(decoded)
        self._on_token("content", delta)


def _streaming_enabled() -> bool:
    """对话助手逐 token 流式开关；默认开启，可用 agent_stream_tokens=off 一键回退非流式。"""
    val = (get_setting("agent_stream_tokens") or "").strip().lower()
    return val not in {"0", "off", "false", "no", "disabled"}


def _accumulate_stream_chunk(
    chunk: dict[str, Any], *,
    content_parts: list[str], reasoning_parts: list[str],
    tool_slots: dict[int, dict[str, Any]], state: dict[str, Any],
    on_token: Callable[[str, str], None] | None,
) -> None:
    """把一帧 SSE delta 累积进重组缓冲，并按需触发 on_token（content / reasoning）。"""
    usage = chunk.get("usage")
    if isinstance(usage, dict):
        state["usage"] = usage
    choices = chunk.get("choices") or []
    if not choices:
        return
    choice = choices[0] or {}
    delta = choice.get("delta") or {}
    content = delta.get("content")
    if isinstance(content, str) and content:
        content_parts.append(content)
        if on_token:
            on_token("content", content)
    reasoning = delta.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning:
        reasoning = delta.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        reasoning_parts.append(reasoning)
        if on_token:
            on_token("reasoning", reasoning)
    for raw_call in delta.get("tool_calls") or []:
        if not isinstance(raw_call, dict):
            continue
        idx = raw_call.get("index")
        idx = int(idx) if isinstance(idx, int) else 0
        slot = tool_slots.setdefault(idx, {"id": None, "name": None, "args": []})
        if raw_call.get("id"):
            slot["id"] = str(raw_call["id"])
        fn = raw_call.get("function") or {}
        if fn.get("name"):
            slot["name"] = str(fn["name"])
        args_piece = fn.get("arguments")
        if isinstance(args_piece, str) and args_piece:
            slot["args"].append(args_piece)
    if choice.get("finish_reason"):
        state["finish_reason"] = choice["finish_reason"]


def _reconstruct_stream_data(
    content_parts: list[str], reasoning_parts: list[str],
    tool_slots: dict[int, dict[str, Any]], state: dict[str, Any],
) -> dict[str, Any]:
    """把流式增量重组成与非流式 `data` 等价的结构，交给 `_parse_assistant_turn` 复用。"""
    message: dict[str, Any] = {"role": "assistant", "content": "".join(content_parts)}
    reasoning_text = "".join(reasoning_parts)
    if reasoning_text:
        message["reasoning"] = reasoning_text
    if tool_slots:
        message["tool_calls"] = [
            {
                "id": slot["id"] or f"call_{idx}",
                "type": "function",
                "function": {"name": slot["name"] or "", "arguments": "".join(slot["args"])},
            }
            for idx, slot in sorted(tool_slots.items())
        ]
    data: dict[str, Any] = {"choices": [{"index": 0, "finish_reason": state.get("finish_reason"), "message": message}]}
    if state.get("usage"):
        data["usage"] = state["usage"]
    return data


async def _stream_chat_completion(
    client: httpx.AsyncClient, url: str, payload: dict, *,
    kind: str, model: str, headers: dict | None = None, key_name: str = "HIAGENT_API_KEY",
    meta: dict | None = None, on_token: Callable[[str, str], None] | None = None,
) -> dict:
    """SSE 流式消费 chat/completions，逐 token 回调 on_token，最终重组为非流式等价 `data`。

    不做重试：流式一旦开始产出 token，重发会重复推送；失败交由上层决定是否降级为非流式。
    """
    merged_meta = _merge_call_meta(meta)
    req_headers = dict(headers if headers is not None else _headers())
    stream_payload = {**payload, "stream": True, "stream_options": {"include_usage": True}}
    request_bytes = _request_size_bytes(stream_payload)
    start = time.time()
    attempt_meta = {
        "http_attempt": 1, "http_attempts_max": 1, "request_bytes": request_bytes,
        "streaming": True, **(merged_meta or {}),
    }
    call_id = start_provider_call(kind, model, meta=attempt_meta, request_json=stream_payload)
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_slots: dict[int, dict[str, Any]] = {}
    state: dict[str, Any] = {}
    try:
        async with client.stream("POST", url, json=stream_payload, headers=req_headers) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                text = body.decode("utf-8", "replace")
                err = _classify_http_error(resp.status_code, text, key_name)
                finish_provider_call(
                    call_id, "FAILED", resp.status_code, int((time.time() - start) * 1000),
                    error=str(err), response_json={"status_code": resp.status_code, "body": text})
                raise err
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                chunk_str = line[len("data:"):].strip()
                if not chunk_str or chunk_str == "[DONE]":
                    if chunk_str == "[DONE]":
                        break
                    continue
                try:
                    chunk = json.loads(chunk_str)
                except (json.JSONDecodeError, ValueError):
                    continue
                _accumulate_stream_chunk(
                    chunk, content_parts=content_parts, reasoning_parts=reasoning_parts,
                    tool_slots=tool_slots, state=state, on_token=on_token)
        latency = int((time.time() - start) * 1000)
        data = _reconstruct_stream_data(content_parts, reasoning_parts, tool_slots, state)
        finish_provider_call(call_id, "OK", 200, latency, response_json=data)
        return data
    except ProviderError:
        raise
    except httpx.TimeoutException as exc:
        latency = int((time.time() - start) * 1000)
        phase = _timeout_phase(exc)
        detail = f"{type(exc).__name__}(phase={phase}, latency_ms={latency}): {exc!r}"
        finish_provider_call(call_id, "TIMEOUT", None, latency, error=detail)
        raise ProviderError(f"流式调用{phase}阶段超时（{latency}ms）", retryable=True, raw=detail, timeout_phase=phase)
    except httpx.HTTPError as exc:
        latency = int((time.time() - start) * 1000)
        finish_provider_call(call_id, "NETWORK_ERROR", None, latency, error=str(exc))
        raise ProviderError(f"流式网络错误：{exc}", retryable=True)


async def _stream_or_fallback(
    client: httpx.AsyncClient, url: str, payload: dict, *,
    kind: str, model: str, headers: dict | None, key_name: str,
    meta: dict | None, on_token: Callable[[str, str], None],
) -> dict:
    """优先流式；若尚未推送任何 token 就失败，则安全降级为非流式（避免重复推送）。"""
    emitted = 0

    def _wrapped(token_kind: str, text: str) -> None:
        nonlocal emitted
        emitted += 1
        on_token(token_kind, text)

    try:
        return await _stream_chat_completion(
            client, url, payload, kind=kind, model=model, headers=headers,
            key_name=key_name, meta=meta, on_token=_wrapped)
    except ProviderError as exc:
        if emitted > 0 or _looks_like_tools_unsupported(exc):
            raise
        fallback_meta = {**(meta or {}), "stream_degraded": True, "stream_degraded_cause": str(exc)[:120]}
        return await _post_json(client, url, payload, kind=kind, model=model,
                                headers=headers, key_name=key_name, meta=fallback_meta)


async def _stream_plain_chat(
    messages: list[dict[str, Any]], *, temperature: float, max_tokens: int,
    call_meta: dict | None, on_token: Callable[[str, str], None],
) -> str:
    """无原生 tools 的流式文本调用，仅供 Agent JSON 协议回退使用。"""
    provider = active_provider("text")
    timeout = httpx.Timeout(connect=10, read=config.TIMEOUT_CHAT_READ, write=30, pool=10)
    async with httpx.AsyncClient(timeout=timeout) as client:
        if provider == "bailian":
            preferred = active_model("text", "bailian")
            payload: dict[str, Any] = {
                "messages": messages, "temperature": temperature, "max_tokens": max_tokens,
            }
            data, _ = await _stream_bailian_chat_with_fallback(
                client, payload, fallback_kind="text", log_kind="chat_tools_json_fallback",
                preferred_model=preferred, meta=call_meta, on_token=on_token,
            )
        else:
            url, resolved_model, headers, key_name = _resolve_text_connection(provider)
            payload = {
                "model": resolved_model, "messages": messages,
                "temperature": temperature, "max_tokens": max_tokens,
            }
            if provider == "openrouter":
                effort = (config.OPENROUTER_TEXT_REASONING_EFFORT or "").strip().lower()
                if effort and effort != "none":
                    payload["reasoning"] = {"effort": effort}
            data = await _stream_or_fallback(
                client, url, payload, kind="chat_tools_json_fallback", model=resolved_model,
                headers=headers, key_name=key_name, meta=call_meta, on_token=on_token,
            )
    return _chat_content(data, label="chat_tools_json_fallback")


async def chat_with_tools(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]], *,
    tool_choice: str = "auto", model: str | None = None, temperature: float = 0.7,
    max_tokens: int = 65535, call_meta: dict | None = None,
    on_token: Callable[[str, str], None] | None = None,
) -> AssistantTurn:
    """带原生工具调用的文本对话。返回 AssistantTurn（content + reasoning + 已解析的 tool_calls）。

    - 供应商路由复用 `chat` 的连接解析；payload 携带 `tools` / `tool_choice`。
    - 供应商不支持 `tools`（能力标志关闭或网关以客户端错误拒绝）时，回退到手写 JSON 协议，
      对上层返回同一种 AssistantTurn，编排器无需感知差异。
    - 传入 `on_token(kind, text)` 且未关闭 agent_stream_tokens 时，走 SSE 流式并逐 token 回调
      （kind ∈ {"content","reasoning"}）；流式在推送前失败会自动降级为非流式。百炼多模型回退路径
      与 JSON 回退协议暂不流式，最终结果一致。
    """
    provider = active_provider("text")
    if not _provider_supports_tools(provider):
        return await _chat_tools_via_json_protocol(
            messages, tools, temperature=temperature, max_tokens=max_tokens,
            call_meta=call_meta, on_token=on_token)
    timeout = httpx.Timeout(connect=10, read=config.TIMEOUT_CHAT_READ, write=30, pool=10)
    stream = on_token is not None and _streaming_enabled()
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            if provider == "bailian":
                bailian_model = active_model("text", "bailian")
                payload: dict[str, Any] = {
                    "messages": messages, "temperature": temperature, "max_tokens": max_tokens,
                    "tools": tools, "tool_choice": tool_choice,
                }
                if stream:
                    data, _ = await _stream_bailian_chat_with_fallback(
                        client, payload, fallback_kind="text", log_kind="chat_tools",
                        preferred_model=bailian_model, meta=call_meta, on_token=on_token)
                else:
                    data, _ = await _post_bailian_chat_with_fallback(
                        client, payload, fallback_kind="text", log_kind="chat_tools",
                        preferred_model=bailian_model, meta=call_meta)
            else:
                url, resolved_model, headers, key_name = _resolve_text_connection(provider, model)
                payload = {
                    "model": resolved_model, "messages": messages, "temperature": temperature,
                    "max_tokens": max_tokens, "tools": tools, "tool_choice": tool_choice,
                }
                if stream:
                    data = await _stream_or_fallback(
                        client, url, payload, kind="chat_tools", model=resolved_model,
                        headers=headers, key_name=key_name, meta=call_meta, on_token=on_token)
                else:
                    data = await _post_json(client, url, payload, kind="chat_tools", model=resolved_model,
                                            headers=headers, key_name=key_name, meta=call_meta)
        except ProviderError as exc:
            if _looks_like_tools_unsupported(exc):
                return await _chat_tools_via_json_protocol(
                    messages, tools, temperature=temperature, max_tokens=max_tokens,
                    call_meta=call_meta, on_token=on_token)
            raise
    return _parse_assistant_turn(data, label="chat_tools")


async def create_video_task(prompt_text: str, *, image_urls: list[tuple[str, str]] | None = None,
                            call_meta: dict | None = None) -> str:
    """创建 Seedance 任务，返回 task id。image_urls: [(url, role)]，role ∈ first_frame/last_frame/reference_image。"""
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt_text}]
    for url, role in image_urls or []:
        content.append({"type": "image_url", "image_url": {"url": url}, "role": role})
    model = active_model("video", "hiagent")
    base_url, model_headers = _model_connection("hiagent", model, config.HIAGENT_BASE_URL, config.HIAGENT_API_KEY)
    payload = {"model": model, "content": content}
    if call_meta and call_meta.get("operation_id"):
        operation_id = str(call_meta["operation_id"])
    elif call_meta and call_meta.get("version_id"):
        operation_id = f"video-create-{call_meta['version_id']}"
    else:
        operation_id = provider_operation_id("video_create", model, payload)
    call_meta = {**(call_meta or {}), "operation_id": operation_id}
    timeout = httpx.Timeout(connect=10, read=config.TIMEOUT_VIDEO_CREATE, write=30, pool=10)
    async with httpx.AsyncClient(timeout=timeout) as client:
        data = await _post_json(client, f"{base_url}/contents/generations/tasks", payload,
                                kind="video_create", model=model, headers=model_headers,
                                key_name=f"model:{model}", meta=call_meta,
                                idempotency_key=operation_id)
    task_id = data.get("id")
    if not task_id:
        raise ProviderError(f"视频任务创建响应缺少 id：{json.dumps(data, ensure_ascii=False)[:300]}")
    return task_id


async def poll_video_task(task_id: str, *, call_meta: dict | None = None) -> dict:
    """轮询单次。返回 {status, video_url, last_frame_url, error}。"""
    timeout = httpx.Timeout(connect=10, read=config.TIMEOUT_VIDEO_POLL, write=10, pool=10)
    start = time.time()
    model = active_model("video", "hiagent")
    base_url, model_headers = _model_connection("hiagent", model, config.HIAGENT_BASE_URL, config.HIAGENT_API_KEY)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"{base_url}/contents/generations/tasks/{task_id}", headers=model_headers)
    except httpx.RequestError as exc:
        latency = int((time.time() - start) * 1000)
        err = ProviderError(
            f"视频任务状态查询网络异常：{type(exc).__name__}: {exc}",
            retryable=True,
            raw=repr(exc),
            timeout_phase=_timeout_phase(exc) if isinstance(exc, httpx.TimeoutException) else None,
        )
        merged_meta = _merge_call_meta(call_meta)
        log_provider_call(
            "video_poll", model, "FAILED", None, latency, error=str(err),
            meta=merged_meta,
            request_json={
                "method": "GET",
                "url": f"{base_url}/contents/generations/tasks/{task_id}",
            },
        )
        raise err from exc
    latency = int((time.time() - start) * 1000)
    if resp.status_code != 200:
        model = active_model("video", "hiagent")
        err = _classify_http_error(resp.status_code, resp.text)
        merged_meta = _merge_call_meta(call_meta)
        log_provider_call("video_poll", model, "FAILED", resp.status_code, latency, error=str(err),
                          meta=merged_meta,
                          request_json={"method": "GET", "url": f"{base_url}/contents/generations/tasks/{task_id}"},
                          response_json={"status_code": resp.status_code, "body": resp.text})
        raise err
    data = resp.json()
    status = data.get("status", "")
    error_obj = data.get("error") or {}
    if status == "failed":
        merged_meta = _merge_call_meta(call_meta)
        log_provider_call("video_poll", active_model("video", "hiagent"), "TASK_FAILED", 200, latency,
                          meta=merged_meta,
                          error=error_obj.get("message", ""),
                          request_json={"method": "GET", "url": f"{base_url}/contents/generations/tasks/{task_id}"},
                          response_json=data)
    return {
        "status": status,
        "video_url": _absolute_provider_url((data.get("content") or {}).get("video_url", ""), base_url),
        "last_frame_url": _absolute_provider_url((data.get("content") or {}).get("last_frame_url", ""), base_url),
        "error": error_obj.get("message", ""),
    }


async def _download_once(url: str, dest_path: str) -> None:
    """单次下载；禁止 SSRF：仅允许公网 http(s)，跟随重定向后再次校验。"""
    import ipaddress
    import socket
    from urllib.parse import urlparse

    def _assert_public_download_url(candidate: str) -> None:
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"}:
            raise ProviderError("下载 URL 必须是 http(s)")
        host = (parsed.hostname or "").strip().lower()
        if not host:
            raise ProviderError("下载 URL 缺少主机名")
        if host in {"localhost", "metadata", "metadata.google.internal"} or host.endswith(".local"):
            raise ProviderError("拒绝下载本机或链路本地地址")
        if host == "169.254.169.254":
            raise ProviderError("拒绝下载云元数据地址")
        candidates: list[str] = []
        try:
            ipaddress.ip_address(host)
            candidates = [host]
        except ValueError:
            try:
                infos = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
            except socket.gaierror as exc:
                raise ProviderError(f"无法解析下载主机：{host}") from exc
            for info in infos:
                addr = info[4][0]
                if addr:
                    candidates.append(addr)
        for addr in candidates:
            try:
                ip = ipaddress.ip_address(addr)
            except ValueError:
                continue
            if (
                ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified
            ):
                raise ProviderError("拒绝下载内网或保留地址")

    _assert_public_download_url(url)
    timeout = httpx.Timeout(connect=10, read=config.TIMEOUT_DOWNLOAD, write=30, pool=10)

    async def _on_redirect(response: httpx.Response) -> None:
        location = response.headers.get("location")
        if location:
            next_url = str(response.url.join(location))
            _assert_public_download_url(next_url)

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        event_hooks={"response": [_on_redirect]},
    ) as client:
        resp = await client.get(url)
        # 最终落地 URL 再校验一次（防 DNS 重绑定后的内网跳转）。
        _assert_public_download_url(str(resp.url))
        if resp.status_code != 200:
            raise ProviderError(f"视频下载失败 HTTP {resp.status_code}（URL 可能已过期，有效期 7 天）")
        atomic_write_bytes(dest_path, resp.content)


async def download(url: str, dest_path: str) -> None:
    """Download provider media with bounded retries for transient transport faults."""
    attempts = 3
    last_error: ProviderError | None = None
    for attempt in range(attempts):
        try:
            await _download_once(url, dest_path)
            return
        except httpx.RequestError as exc:
            phase = _timeout_phase(exc) if isinstance(exc, httpx.TimeoutException) else None
            last_error = ProviderError(
                f"媒体下载网络异常：{type(exc).__name__}: {exc}",
                retryable=True,
                raw=repr(exc),
                timeout_phase=phase,
            )
        if attempt + 1 < attempts:
            await asyncio.sleep(1.5 * (2 ** attempt))
    assert last_error is not None
    raise last_error


async def generate_image(prompt: str, *, size: str = "1024x1024",
                         image_inputs: list[str] | None = None,
                         call_meta: dict | None = None,
                         log_kind: str | None = None) -> dict:
    """Seedream 图像生成。返回 {url 或 b64_json}。
    image_inputs：可选的参考图（data URL 列表），用于让生成图保持角色/场景一致性。
    网关是否支持参考图未知，调用方应 try-with-fallback（带参考图失败则不带重试）。"""
    model = active_model("image", "hiagent")
    base_url, model_headers = _model_connection("hiagent", model, config.HIAGENT_BASE_URL, config.HIAGENT_API_KEY)
    payload: dict[str, Any] = {"model": model, "prompt": prompt, "n": 1, "size": size}
    media_meta: dict[str, Any] = {}
    if image_inputs:
        prepared_inputs, media_meta = await _prepare_image_data_urls(image_inputs)
        payload["image"] = prepared_inputs if len(prepared_inputs) > 1 else prepared_inputs[0]
    kind = log_kind or ("image_edit" if image_inputs else "image_generate")
    operation_id = (
        str((call_meta or {}).get("operation_id") or "").strip()
        or provider_operation_id(kind, model, payload)
    )
    timeout = httpx.Timeout(connect=10, read=config.TIMEOUT_IMAGE_READ,
                            write=config.TIMEOUT_IMAGE_WRITE, pool=10)
    async with _image_semaphore():
        async with httpx.AsyncClient(timeout=timeout) as client:
            data = await _post_json(
                client, f"{base_url}/images/generations", payload,
                kind=kind, model=model, headers=model_headers, key_name=f"model:{model}",
                meta={**(call_meta or {}), **media_meta, "operation_id": operation_id},
                idempotency_key=operation_id)
    items = data.get("data") or []
    if not items:
        raise ProviderError(f"图像生成响应为空：{json.dumps(data, ensure_ascii=False)[:300]}")
    item = dict(items[0])
    if item.get("url"):
        item["url"] = _absolute_provider_url(item["url"], base_url)
    return item


async def vlm_check(frames_b64: list[str], expectation_text: str,
                    *, call_meta: dict | None = None) -> str:
    """VLM 质检：传入抽帧（base64 jpeg）与预期描述，返回模型原文（上层解析 JSON）。
    按设置在火山 HiAgent、OpenRouter、阿里云百炼之间路由。"""
    content: list[dict[str, Any]] = [{"type": "text", "text": expectation_text}]
    for b64 in frames_b64:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    messages = [
        {"role": "system", "content": "Return exactly one valid JSON object. No Markdown, no prose."},
        {"role": "user", "content": content},
    ]
    timeout = httpx.Timeout(connect=10, read=config.TIMEOUT_VLM_READ,
                            write=config.TIMEOUT_VLM_WRITE, pool=10)
    provider = active_provider("vlm")
    if provider == "openrouter":
        model = active_model("vlm", "openrouter")
        base_url, headers = _model_connection("openrouter", model, config.OPENROUTER_BASE_URL, config.OPENROUTER_API_KEY)
        url = f"{base_url}/chat/completions"
        key_name = f"model:{model}"
    elif provider == "bailian":
        model = active_model("vlm", "bailian")
    elif provider.startswith("custom:"):
        model = active_model("vlm", provider)
        base_url, headers = _model_connection(provider, model)
        url = f"{base_url}/chat/completions"
        key_name = f"model:{model}"
    else:
        model = active_model("vlm", "hiagent")
        base_url, headers = _model_connection("hiagent", model, config.HIAGENT_BASE_URL, config.HIAGENT_API_KEY)
        url = f"{base_url}/chat/completions"
        key_name = f"model:{model}"
    prepared_urls, media_meta = await _prepare_image_data_urls(
        [f"data:image/jpeg;base64,{b64}" for b64 in frames_b64])
    content[:] = [content[0], *[
        {"type": "image_url", "image_url": {"url": url}} for url in prepared_urls
    ]]
    payload = {"model": model, "messages": messages, "temperature": 0, "max_tokens": 2048}
    if provider == "openrouter":
        payload["response_format"] = {"type": "json_object"}
    merged_call_meta = {**(call_meta or {}), **media_meta}
    async with _vlm_semaphore():
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                if provider == "bailian":
                    bailian_payload = {"messages": messages, "temperature": 0, "max_tokens": 2048}
                    data, _ = await _post_bailian_chat_with_fallback(
                        client, bailian_payload, fallback_kind="vlm", log_kind="vlm_qa",
                        preferred_model=model, meta=merged_call_meta)
                else:
                    data = await _post_json(client, url, payload, kind="vlm_qa", model=model,
                                            headers=headers, key_name=key_name, meta=merged_call_meta)
            except ProviderError as exc:
                raw = (exc.raw or str(exc)).lower()
                if provider == "openrouter" and "response_format" in payload and (
                        "response_format" in raw or "json" in raw or "schema" in raw):
                    payload.pop("response_format", None)
                    data = await _post_json(client, url, payload, kind="vlm_qa", model=model,
                                            headers=headers, key_name=key_name, meta=merged_call_meta)
                else:
                    raise
    return _chat_content(data, label="VLM")


def encode_image_file(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def data_url_from_file(path: str) -> str:
    """本地图片 → data URL。实测网关接受 base64 data URL 作为参考图，无需外部托管。"""
    mime = "image/png" if path.lower().endswith(".png") else "image/jpeg"
    return f"data:{mime};base64,{encode_image_file(path)}"
