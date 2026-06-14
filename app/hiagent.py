"""HiAgent 网关客户端。全部真实调用，禁止 mock（PRD 原则 P1）；失败必须携带原始报文向上抛（P2）。

API 形态依据 M0 实测（docs/HIAGENT_INTEGRATION.md）：
- chat/completions：OpenAI 兼容；文本模型为推理模型，只读 message.content。
- 视频：POST /contents/generations/tasks 创建（网关无同步参数校验！），GET /tasks/{id} 轮询，
  succeeded 后 content.video_url 7 天过期，必须立即下载。
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any

import httpx

from app import config
from app.db import get_setting, log_provider_call


class ProviderError(Exception):
    """对外调用失败。message 面向 UI，包含分类结论 + 原始报文摘要。"""

    def __init__(self, message: str, *, retryable: bool = False, raw: str = ""):
        super().__init__(message)
        self.retryable = retryable
        self.raw = raw[:500]


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


def _model_route() -> str:
    return (get_setting("model_route") or "hiagent").strip()


def active_provider(kind: str) -> str:
    configured = (get_setting(f"model_{kind}_provider") or "").strip()
    if configured in {"hiagent", "openrouter"}:
        return configured
    if kind in {"text", "vlm"} and configured == "bailian":
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


async def _post_json(client: httpx.AsyncClient, url: str, payload: dict, *,
                     kind: str, model: str, retries: int = 2,
                     headers: dict | None = None, key_name: str = "HIAGENT_API_KEY") -> dict:
    last_err: ProviderError | None = None
    req_headers = headers or _headers()
    for attempt in range(retries + 1):
        start = time.time()
        try:
            resp = await client.post(url, json=payload, headers=req_headers)
            latency = int((time.time() - start) * 1000)
            if resp.status_code == 200:
                log_provider_call(kind, model, "OK", 200, latency)
                return resp.json()
            err = _classify_http_error(resp.status_code, resp.text, key_name)
            log_provider_call(kind, model, "FAILED", resp.status_code, latency, error=str(err))
            if not err.retryable:
                raise err
            last_err = err
        except httpx.TimeoutException as exc:
            latency = int((time.time() - start) * 1000)
            last_err = ProviderError(f"调用超时（{latency}ms）：{exc}", retryable=True)
            log_provider_call(kind, model, "TIMEOUT", None, latency, error=str(exc))
        except httpx.HTTPError as exc:
            latency = int((time.time() - start) * 1000)
            last_err = ProviderError(f"网络错误：{exc}", retryable=True)
            log_provider_call(kind, model, "NETWORK_ERROR", None, latency, error=str(exc))
        if attempt < retries:
            await asyncio.sleep(1.5 * (2 ** attempt))
    assert last_err is not None
    raise last_err


async def chat(messages: list[dict], *, model: str | None = None, temperature: float = 0.7,
               max_tokens: int = 8192) -> str:
    """文本 LLM 对话，返回 message.content（推理模型的 reasoning 一律丢弃）。
    按设置在火山 HiAgent、OpenRouter、阿里云百炼之间路由（仅文本可走百炼，质检可走 OpenRouter，
    图像/视频始终走火山）。"""
    timeout = httpx.Timeout(connect=10, read=config.TIMEOUT_CHAT_READ, write=30, pool=10)
    provider = active_provider("text")
    if provider == "openrouter":
        or_model = active_model("text", "openrouter")
        payload: dict[str, Any] = {"model": or_model, "messages": messages, "max_tokens": max_tokens}
        effort = (config.OPENROUTER_TEXT_REASONING_EFFORT or "").strip().lower()
        if effort and effort != "none":
            # 启用思考；Claude 思考模式要求 temperature=1，故省略 temperature 交由模型默认
            payload["reasoning"] = {"effort": effort}
        else:
            payload["temperature"] = temperature
        async with httpx.AsyncClient(timeout=timeout) as client:
            data = await _post_json(client, f"{config.OPENROUTER_BASE_URL}/chat/completions", payload,
                                    kind="chat", model=or_model,
                                    headers=_openrouter_headers(), key_name="OPENROUTER_API_KEY")
        content = _chat_content(data, label="chat")
    elif provider == "bailian":
        bailian_model = active_model("text", "bailian")
        payload = {"model": bailian_model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        async with httpx.AsyncClient(timeout=timeout) as client:
            data = await _post_json(client, f"{config.BAILIAN_BASE_URL}/chat/completions", payload,
                                    kind="chat", model=bailian_model,
                                    headers=_bailian_headers(), key_name="BAILIAN_API_KEY")
        content = _chat_content(data, label="chat")
    else:
        model = model or active_model("text", "hiagent")
        payload = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        async with httpx.AsyncClient(timeout=timeout) as client:
            data = await _post_json(client, f"{config.HIAGENT_BASE_URL}/chat/completions", payload,
                                    kind="chat", model=model)
        content = _chat_content(data, label="chat")
    if not content or not content.strip():
        raise ProviderError("模型返回空内容（content 为空）")
    return content


async def create_video_task(prompt_text: str, *, image_urls: list[tuple[str, str]] | None = None) -> str:
    """创建 Seedance 任务，返回 task id。image_urls: [(url, role)]，role ∈ first_frame/last_frame/reference_image。"""
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt_text}]
    for url, role in image_urls or []:
        content.append({"type": "image_url", "image_url": {"url": url}, "role": role})
    model = active_model("video", "hiagent")
    payload = {"model": model, "content": content}
    timeout = httpx.Timeout(connect=10, read=config.TIMEOUT_VIDEO_CREATE, write=30, pool=10)
    async with httpx.AsyncClient(timeout=timeout) as client:
        data = await _post_json(client, f"{config.HIAGENT_BASE_URL}/contents/generations/tasks", payload,
                                kind="video_create", model=model)
    task_id = data.get("id")
    if not task_id:
        raise ProviderError(f"视频任务创建响应缺少 id：{json.dumps(data, ensure_ascii=False)[:300]}")
    return task_id


async def poll_video_task(task_id: str) -> dict:
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
        log_provider_call("video_poll", model, "FAILED", resp.status_code, latency, error=str(err))
        raise err
    data = resp.json()
    status = data.get("status", "")
    error_obj = data.get("error") or {}
    if status == "failed":
        log_provider_call("video_poll", active_model("video", "hiagent"), "TASK_FAILED", 200, latency,
                          error=error_obj.get("message", ""))
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
                         image_inputs: list[str] | None = None) -> dict:
    """Seedream 图像生成。返回 {url 或 b64_json}。
    image_inputs：可选的参考图（data URL 列表），用于让生成图保持角色/场景一致性。
    网关是否支持参考图未知，调用方应 try-with-fallback（带参考图失败则不带重试）。"""
    model = active_model("image", "hiagent")
    payload: dict[str, Any] = {"model": model, "prompt": prompt, "n": 1, "size": size}
    if image_inputs:
        payload["image"] = image_inputs if len(image_inputs) > 1 else image_inputs[0]
    timeout = httpx.Timeout(connect=10, read=120, write=30, pool=10)
    async with httpx.AsyncClient(timeout=timeout) as client:
        data = await _post_json(client, f"{config.HIAGENT_BASE_URL}/images/generations", payload,
                                kind="image", model=model)
    items = data.get("data") or []
    if not items:
        raise ProviderError(f"图像生成响应为空：{json.dumps(data, ensure_ascii=False)[:300]}")
    return items[0]


async def vlm_check(frames_b64: list[str], expectation_text: str) -> str:
    """VLM 质检：传入抽帧（base64 jpeg）与预期描述，返回模型原文（上层解析 JSON）。
    按设置在火山 HiAgent、OpenRouter、阿里云百炼之间路由。"""
    content: list[dict[str, Any]] = [{"type": "text", "text": expectation_text}]
    for b64 in frames_b64:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    messages = [
        {"role": "system", "content": "Return exactly one valid JSON object. No Markdown, no prose."},
        {"role": "user", "content": content},
    ]
    timeout = httpx.Timeout(connect=10, read=300, write=60, pool=10)
    provider = active_provider("vlm")
    if provider == "openrouter":
        model = active_model("vlm", "openrouter")
        url = f"{config.OPENROUTER_BASE_URL}/chat/completions"
        headers, key_name = _openrouter_headers(), "OPENROUTER_API_KEY"
    elif provider == "bailian":
        model = active_model("vlm", "bailian")
        url = f"{config.BAILIAN_BASE_URL}/chat/completions"
        headers, key_name = _bailian_headers(), "BAILIAN_API_KEY"
    else:
        model = active_model("vlm", "hiagent")
        url = f"{config.HIAGENT_BASE_URL}/chat/completions"
        headers, key_name = None, "HIAGENT_API_KEY"
    payload = {"model": model, "messages": messages, "temperature": 0, "max_tokens": 2048}
    if provider == "openrouter":
        payload["response_format"] = {"type": "json_object"}
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            data = await _post_json(client, url, payload, kind="vlm_qa", model=model,
                                    headers=headers, key_name=key_name)
        except ProviderError as exc:
            raw = (exc.raw or str(exc)).lower()
            if provider == "openrouter" and "response_format" in payload and (
                    "response_format" in raw or "json" in raw or "schema" in raw):
                payload.pop("response_format", None)
                data = await _post_json(client, url, payload, kind="vlm_qa", model=model,
                                        headers=headers, key_name=key_name)
            else:
                raise
    return _chat_content(data, label="VLM")


# ---------- 音频：TTS（DashScope 原生多模态）/ ASR（兼容模式 omni，base64 输入） ----------

async def tts(text: str, *, voice: str | None = None, model: str | None = None) -> bytes:
    """文本转语音，返回音频字节。走 DashScope 原生 multimodal-generation（返回 audio.url 再下载）。
    兼容模式无 /audio/speech（实测 404），故用原生端点。"""
    model = model or config.BAILIAN_TTS_MODEL
    voice = voice or config.BAILIAN_TTS_VOICE
    payload = {"model": model, "input": {"text": text, "voice": voice}}
    timeout = httpx.Timeout(connect=10, read=config.TIMEOUT_AUDIO, write=30, pool=10)
    async with httpx.AsyncClient(timeout=timeout) as client:
        data = await _post_json(client, config.BAILIAN_NATIVE_TTS_URL, payload,
                                kind="tts", model=model,
                                headers=_bailian_headers(), key_name="BAILIAN_API_KEY")
        audio = ((data.get("output") or {}).get("audio") or {})
        url = audio.get("url")
        if not url:
            b64 = audio.get("data")
            if b64:
                return base64.b64decode(b64)
            raise ProviderError(f"TTS 响应缺少 audio.url/data：{json.dumps(data, ensure_ascii=False)[:300]}")
        resp = await client.get(url)
        if resp.status_code != 200:
            raise ProviderError(f"TTS 音频下载失败 HTTP {resp.status_code}（链接可能已过期）")
        return resp.content


async def asr(audio_bytes: bytes, *, fmt: str = "wav", model: str | None = None) -> str:
    """语音转文本（识别音频里到底念了什么）。走 DashScope 兼容模式 omni，base64 input_audio。"""
    model = model or config.BAILIAN_ASR_MODEL
    b64 = base64.b64encode(audio_bytes).decode("ascii")
    messages = [{"role": "user", "content": [
        {"type": "input_audio", "input_audio": {"data": f"data:;base64,{b64}", "format": fmt}},
        {"type": "text", "text": "请逐字转写这段音频，只输出中文文字，不要标点解释、不要额外内容。"},
    ]}]
    payload = {"model": model, "modalities": ["text"], "messages": messages, "temperature": 0}
    timeout = httpx.Timeout(connect=10, read=config.TIMEOUT_AUDIO, write=60, pool=10)
    async with httpx.AsyncClient(timeout=timeout) as client:
        data = await _post_json(client, f"{config.BAILIAN_BASE_URL}/chat/completions", payload,
                                kind="asr", model=model,
                                headers=_bailian_headers(), key_name="BAILIAN_API_KEY")
    return _chat_content(data, label="ASR")


def encode_image_file(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def data_url_from_file(path: str) -> str:
    """本地图片 → data URL。实测网关接受 base64 data URL 作为参考图，无需外部托管。"""
    mime = "image/png" if path.lower().endswith(".png") else "image/jpeg"
    return f"data:{mime};base64,{encode_image_file(path)}"
