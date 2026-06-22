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
import shutil
import subprocess
import time
import weakref
from typing import Any

import httpx

from app import config
from app.db import finish_provider_call, get_setting, log_provider_call, start_provider_call

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


class ProviderError(Exception):
    """对外调用失败。message 面向 UI，包含分类结论 + 原始报文摘要。"""

    def __init__(self, message: str, *, retryable: bool = False, raw: str = "",
                 timeout_phase: str | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.raw = raw[:500]
        self.timeout_phase = timeout_phase


def _media_semaphore() -> asyncio.Semaphore:
    """图生图与 VLM 共用同一并发门。按 event loop 存放，避免测试/worker
    使用多个 asyncio.run 时将 Semaphore 绑到已关闭的 loop。
    """
    loop = asyncio.get_running_loop()
    semaphore = _MEDIA_SEMAPHORES.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(config.MEDIA_REQUEST_CONCURRENCY)
        _MEDIA_SEMAPHORES[loop] = semaphore
    return semaphore


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


def _openrouter_headers() -> dict[str, str]:
    if not config.OPENROUTER_API_KEY:
        raise ProviderError("未配置 OPENROUTER_API_KEY，请在项目根目录 .env 中填写，或在监制房切回火山路由")
    return {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        # OpenRouter 可选归因头（用于其排行榜/用量页，不影响功能）
        "HTTP-Referer": "http://127.0.0.1:8230",
        "X-Title": "MJAgent2",
    }


def _bailian_headers() -> dict[str, str]:
    if not config.BAILIAN_API_KEY:
        raise ProviderError("未配置 BAILIAN_API_KEY，请在项目根目录 .env 中填写，或在监制房切回其他文本模型")
    return {"Authorization": f"Bearer {config.BAILIAN_API_KEY}", "Content-Type": "application/json"}


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


def active_model(kind: str, provider: str | None = None) -> str:
    provider = provider or active_provider(kind)
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


def _use_openrouter(kind: str = "text") -> bool:
    return active_provider(kind) == "openrouter" and bool(config.OPENROUTER_API_KEY)


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
                     meta: dict | None = None) -> dict:
    last_err: ProviderError | None = None
    merged_meta = _merge_call_meta(meta)
    req_headers = headers or _headers()
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
                data = resp.json()
                finish_provider_call(call_id, "OK", 200, latency, response_json=data)
                return data
            err = _classify_http_error(resp.status_code, resp.text, key_name)
            finish_provider_call(
                call_id, "FAILED", resp.status_code, latency, error=str(err),
                response_json={"status_code": resp.status_code, "body": resp.text})
            if not err.retryable:
                raise err
            last_err = err
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
            if phase == "write":
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
    url = f"{config.BAILIAN_BASE_URL}/chat/completions"
    headers = _bailian_headers()
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


async def chat(messages: list[dict], *, model: str | None = None, temperature: float = 0.7,
               max_tokens: int = 65535, call_meta: dict | None = None) -> str:
    """文本 LLM 对话，返回 message.content（推理模型的 reasoning 一律丢弃）。
    按设置在火山 HiAgent、OpenRouter、阿里云百炼、DeepSeek、智谱官方 API 之间路由（后两者仅文本，
    图像/视频始终走火山）。"""
    timeout = httpx.Timeout(connect=10, read=config.TIMEOUT_CHAT_READ, write=30, pool=10)
    provider = active_provider("text")
    async with httpx.AsyncClient(timeout=timeout) as client:
        if provider == "openrouter":
            or_model = active_model("text", "openrouter")
            payload: dict[str, Any] = {"model": or_model, "messages": messages, "max_tokens": max_tokens}
            effort = (config.OPENROUTER_TEXT_REASONING_EFFORT or "").strip().lower()
            if effort and effort != "none":
                payload["reasoning"] = {"effort": effort}
            else:
                payload["temperature"] = temperature
            content = await _chat_with_reasoning_fallback(
                client, f"{config.OPENROUTER_BASE_URL}/chat/completions", payload,
                kind="chat", model=or_model, headers=_openrouter_headers(),
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
            payload = {"model": deepseek_model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
            content = await _chat_with_reasoning_fallback(
                client, f"{config.DEEPSEEK_BASE_URL}/chat/completions", payload,
                kind="chat", model=deepseek_model, headers=_deepseek_headers(),
                key_name="DEEPSEEK_API_KEY", temperature=temperature, call_meta=call_meta)
        elif provider == "zhipu":
            zhipu_model = active_model("text", "zhipu")
            payload = {"model": zhipu_model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
            content = await _chat_with_reasoning_fallback(
                client, f"{config.ZHIPU_BASE_URL}/chat/completions", payload,
                kind="chat", model=zhipu_model, headers=_zhipu_headers(),
                key_name="ZHIPU_API_KEY", temperature=temperature, call_meta=call_meta)
        else:
            model = model or active_model("text", "hiagent")
            payload = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
            data = await _post_json(client, f"{config.HIAGENT_BASE_URL}/chat/completions", payload,
                                    kind="chat", model=model, meta=call_meta)
            content = _chat_content(data, label="chat")

    if not content or not content.strip():
        raise ProviderError(f"模型返回空内容（content 为空；{_empty_content_detail(data)}）")
    return content


async def create_video_task(prompt_text: str, *, image_urls: list[tuple[str, str]] | None = None,
                            call_meta: dict | None = None) -> str:
    """创建 Seedance 任务，返回 task id。image_urls: [(url, role)]，role ∈ first_frame/last_frame/reference_image。"""
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt_text}]
    for url, role in image_urls or []:
        content.append({"type": "image_url", "image_url": {"url": url}, "role": role})
    model = active_model("video", "hiagent")
    payload = {"model": model, "content": content}
    timeout = httpx.Timeout(connect=10, read=config.TIMEOUT_VIDEO_CREATE, write=30, pool=10)
    async with httpx.AsyncClient(timeout=timeout) as client:
        data = await _post_json(client, f"{config.HIAGENT_BASE_URL}/contents/generations/tasks", payload,
                                kind="video_create", model=model, meta=call_meta)
    task_id = data.get("id")
    if not task_id:
        raise ProviderError(f"视频任务创建响应缺少 id：{json.dumps(data, ensure_ascii=False)[:300]}")
    return task_id


async def poll_video_task(task_id: str, *, call_meta: dict | None = None) -> dict:
    """轮询单次。返回 {status, video_url, last_frame_url, error}。"""
    timeout = httpx.Timeout(connect=10, read=config.TIMEOUT_VIDEO_POLL, write=10, pool=10)
    start = time.time()
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(f"{config.HIAGENT_BASE_URL}/contents/generations/tasks/{task_id}",
                                headers=_headers())
    latency = int((time.time() - start) * 1000)
    if resp.status_code != 200:
        model = active_model("video", "hiagent")
        err = _classify_http_error(resp.status_code, resp.text)
        merged_meta = _merge_call_meta(call_meta)
        log_provider_call("video_poll", model, "FAILED", resp.status_code, latency, error=str(err),
                          meta=merged_meta,
                          request_json={"method": "GET", "url": f"{config.HIAGENT_BASE_URL}/contents/generations/tasks/{task_id}"},
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
                          request_json={"method": "GET", "url": f"{config.HIAGENT_BASE_URL}/contents/generations/tasks/{task_id}"},
                          response_json=data)
    return {
        "status": status,
        "video_url": (data.get("content") or {}).get("video_url", ""),
        "last_frame_url": (data.get("content") or {}).get("last_frame_url", ""),
        "error": error_obj.get("message", ""),
    }


async def download(url: str, dest_path: str) -> None:
    timeout = httpx.Timeout(connect=10, read=config.TIMEOUT_DOWNLOAD, write=30, pool=10)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            raise ProviderError(f"视频下载失败 HTTP {resp.status_code}（URL 可能已过期，有效期 7 天）")
        with open(dest_path, "wb") as f:
            f.write(resp.content)


async def generate_image(prompt: str, *, size: str = "1024x1024",
                         image_inputs: list[str] | None = None,
                         call_meta: dict | None = None,
                         log_kind: str | None = None) -> dict:
    """Seedream 图像生成。返回 {url 或 b64_json}。
    image_inputs：可选的参考图（data URL 列表），用于让生成图保持角色/场景一致性。
    网关是否支持参考图未知，调用方应 try-with-fallback（带参考图失败则不带重试）。"""
    model = active_model("image", "hiagent")
    payload: dict[str, Any] = {"model": model, "prompt": prompt, "n": 1, "size": size}
    media_meta: dict[str, Any] = {}
    if image_inputs:
        prepared_inputs, media_meta = await _prepare_image_data_urls(image_inputs)
        payload["image"] = prepared_inputs if len(prepared_inputs) > 1 else prepared_inputs[0]
    kind = log_kind or ("image_edit" if image_inputs else "image_generate")
    timeout = httpx.Timeout(connect=10, read=config.TIMEOUT_IMAGE_READ,
                            write=config.TIMEOUT_IMAGE_WRITE, pool=10)
    async with _media_semaphore():
        async with httpx.AsyncClient(timeout=timeout) as client:
            data = await _post_json(
                client, f"{config.HIAGENT_BASE_URL}/images/generations", payload,
                kind=kind, model=model, meta={**(call_meta or {}), **media_meta})
    items = data.get("data") or []
    if not items:
        raise ProviderError(f"图像生成响应为空：{json.dumps(data, ensure_ascii=False)[:300]}")
    return items[0]


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
        url = f"{config.OPENROUTER_BASE_URL}/chat/completions"
        headers, key_name = _openrouter_headers(), "OPENROUTER_API_KEY"
    elif provider == "bailian":
        model = active_model("vlm", "bailian")
    else:
        model = active_model("vlm", "hiagent")
        url = f"{config.HIAGENT_BASE_URL}/chat/completions"
        headers, key_name = None, "HIAGENT_API_KEY"
    prepared_urls, media_meta = await _prepare_image_data_urls(
        [f"data:image/jpeg;base64,{b64}" for b64 in frames_b64])
    content[:] = [content[0], *[
        {"type": "image_url", "image_url": {"url": url}} for url in prepared_urls
    ]]
    payload = {"model": model, "messages": messages, "temperature": 0, "max_tokens": 2048}
    if provider == "openrouter":
        payload["response_format"] = {"type": "json_object"}
    merged_call_meta = {**(call_meta or {}), **media_meta}
    async with _media_semaphore():
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
