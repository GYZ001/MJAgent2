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
from app.db import log_provider_call


class ProviderError(Exception):
    """对外调用失败。message 面向 UI，包含分类结论 + 原始报文摘要。"""

    def __init__(self, message: str, *, retryable: bool = False, raw: str = ""):
        super().__init__(message)
        self.retryable = retryable
        self.raw = raw[:500]


def _classify_http_error(status: int, body: str) -> ProviderError:
    lowered = body.lower()
    if status in (401, 403):
        if "no access to model" in lowered:
            return ProviderError(f"凭证有效，但模型未授权/未开通（HTTP {status}）：{body[:300]}", raw=body)
        return ProviderError(f"鉴权失败，请检查 .env 中的 HIAGENT_API_KEY（HTTP {status}）：{body[:300]}", raw=body)
    if status == 429:
        return ProviderError(f"网关限流（HTTP 429）：{body[:200]}", retryable=True, raw=body)
    if status >= 500:
        return ProviderError(f"网关/上游故障（HTTP {status}）：{body[:300]}", retryable=True, raw=body)
    return ProviderError(f"请求被拒绝（HTTP {status}）：{body[:300]}", raw=body)


def _headers() -> dict[str, str]:
    if not config.HIAGENT_API_KEY:
        raise ProviderError("未配置 HIAGENT_API_KEY，请在项目根目录 .env 中填写")
    return {"Authorization": f"Bearer {config.HIAGENT_API_KEY}", "Content-Type": "application/json"}


async def _post_json(client: httpx.AsyncClient, url: str, payload: dict, *,
                     kind: str, model: str, retries: int = 2) -> dict:
    last_err: ProviderError | None = None
    for attempt in range(retries + 1):
        start = time.time()
        try:
            resp = await client.post(url, json=payload, headers=_headers())
            latency = int((time.time() - start) * 1000)
            if resp.status_code == 200:
                log_provider_call(kind, model, "OK", 200, latency)
                return resp.json()
            err = _classify_http_error(resp.status_code, resp.text)
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
    """返回 message.content（推理模型的 reasoning_content 一律丢弃）。"""
    model = model or config.MODEL_TEXT
    payload = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    timeout = httpx.Timeout(connect=10, read=config.TIMEOUT_CHAT_READ, write=30, pool=10)
    async with httpx.AsyncClient(timeout=timeout) as client:
        data = await _post_json(client, f"{config.HIAGENT_BASE_URL}/chat/completions", payload,
                                kind="chat", model=model)
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise ProviderError(f"chat 响应结构异常：{json.dumps(data, ensure_ascii=False)[:300]}") from exc
    if not content or not content.strip():
        raise ProviderError("模型返回空内容（content 为空）")
    return content


async def create_video_task(prompt_text: str, *, image_urls: list[tuple[str, str]] | None = None) -> str:
    """创建 Seedance 任务，返回 task id。image_urls: [(url, role)]，role ∈ first_frame/reference_image。"""
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt_text}]
    for url, role in image_urls or []:
        content.append({"type": "image_url", "image_url": {"url": url}, "role": role})
    payload = {"model": config.MODEL_VIDEO, "content": content}
    timeout = httpx.Timeout(connect=10, read=config.TIMEOUT_VIDEO_CREATE, write=30, pool=10)
    async with httpx.AsyncClient(timeout=timeout) as client:
        data = await _post_json(client, f"{config.HIAGENT_BASE_URL}/contents/generations/tasks", payload,
                                kind="video_create", model=config.MODEL_VIDEO)
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
        err = _classify_http_error(resp.status_code, resp.text)
        log_provider_call("video_poll", config.MODEL_VIDEO, "FAILED", resp.status_code, latency, error=str(err))
        raise err
    data = resp.json()
    status = data.get("status", "")
    error_obj = data.get("error") or {}
    if status == "failed":
        log_provider_call("video_poll", config.MODEL_VIDEO, "TASK_FAILED", 200, latency,
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


async def generate_image(prompt: str, *, size: str = "1024x1024") -> dict:
    """Seedream 定妆照。返回 {url 或 b64_json}。"""
    payload = {"model": config.MODEL_IMAGE, "prompt": prompt, "n": 1, "size": size}
    timeout = httpx.Timeout(connect=10, read=120, write=30, pool=10)
    async with httpx.AsyncClient(timeout=timeout) as client:
        data = await _post_json(client, f"{config.HIAGENT_BASE_URL}/images/generations", payload,
                                kind="image", model=config.MODEL_IMAGE)
    items = data.get("data") or []
    if not items:
        raise ProviderError(f"图像生成响应为空：{json.dumps(data, ensure_ascii=False)[:300]}")
    return items[0]


async def vlm_check(frames_b64: list[str], expectation_text: str) -> str:
    """VLM 质检：传入抽帧（base64 jpeg）与预期描述，返回模型原文（上层解析 JSON）。"""
    content: list[dict[str, Any]] = [{"type": "text", "text": expectation_text}]
    for b64 in frames_b64:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    messages = [{"role": "user", "content": content}]
    payload = {"model": config.MODEL_VLM, "messages": messages, "temperature": 0.2, "max_tokens": 1024}
    timeout = httpx.Timeout(connect=10, read=300, write=60, pool=10)
    async with httpx.AsyncClient(timeout=timeout) as client:
        data = await _post_json(client, f"{config.HIAGENT_BASE_URL}/chat/completions", payload,
                                kind="vlm_qa", model=config.MODEL_VLM)
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise ProviderError(f"VLM 响应结构异常：{json.dumps(data, ensure_ascii=False)[:300]}") from exc


def encode_image_file(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def data_url_from_file(path: str) -> str:
    """本地图片 → data URL。实测网关接受 base64 data URL 作为参考图，无需外部托管。"""
    mime = "image/png" if path.lower().endswith(".png") else "image/jpeg"
    return f"data:{mime};base64,{encode_image_file(path)}"
