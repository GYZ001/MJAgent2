"""HiAgent Seedance 视频供应商适配器。

这里的创建/轮询实现是从 ``app.hiagent`` 内联搬出来的，行为逐字保持不变：
Seedance 的输入走 OpenAI 风格的 ``content[]``（``image_url``/``video_url`` 带
role），供应商自己去拉素材，产物直接出现在轮询响应的 ``content`` 里。

依赖的日志、错误分类与幂等键工具仍留在 ``app.hiagent``——那些是全供应商共用
的调用账本，不属于某一家的接入细节。
"""
from __future__ import annotations

import json
import time
from typing import Any

import httpx

from app import config


class SeedanceAdapter:
    """Seedance 2.0（火山 HiAgent 网关）。"""

    gateway = "hiagent"
    serial_generation = False
    wait_meta_keys: tuple[str, ...] = ()

    def __init__(self, *, provider: str = "hiagent") -> None:
        # provider 同时是 _model_connection 的查表键：内置实例落到 HiAgent 的
        # 全局配置，自建实例落到模型库里那一条的 base_url / api_key。
        self.provider = provider

    async def create_video_task(
        self,
        prompt_text: str,
        *,
        image_urls: list[tuple[str, str]] | None = None,
        video_urls: list[tuple[str, str]] | None = None,
        return_last_frame: bool = False,
        call_meta: dict[str, Any] | None = None,
    ) -> str:
        from app.hiagent import (
            ProviderError,
            _latest_provider_operation_request,
            _model_connection,
            _post_json,
            active_model,
            provider_operation_id,
        )

        content: list[dict[str, Any]] = [{"type": "text", "text": prompt_text}]
        for url, role in image_urls or []:
            content.append({"type": "image_url", "image_url": {"url": url}, "role": role})
        for url, role in video_urls or []:
            content.append({"type": "video_url", "video_url": {"url": url}, "role": role})
        model = active_model("video", self.provider)
        base_url, model_headers = _model_connection(
            self.provider, model, config.HIAGENT_BASE_URL, config.HIAGENT_API_KEY,
        )
        payload = {"model": model, "content": content}
        if return_last_frame:
            payload["return_last_frame"] = True
        if call_meta and call_meta.get("operation_id"):
            operation_id = str(call_meta["operation_id"])
        elif call_meta and call_meta.get("version_id"):
            operation_id = f"video-create-{call_meta['version_id']}"
        else:
            operation_id = provider_operation_id("video_create", model, payload)
        saved_request = _latest_provider_operation_request(
            "video_create", operation_id,
        )
        if saved_request is not None and saved_request != payload:
            raise ProviderError(
                "Seedance 同一业务操作的请求内容发生变化，已阻止复用幂等键；"
                "请保留原任务等待供应商结果确认，或通过页面明确创建新的生成尝试",
                failure_kind="idempotency_request_mismatch",
                delivery_state="unknown",
                requires_explicit_retry=True,
            )
        call_meta = {**(call_meta or {}), "operation_id": operation_id}
        timeout = httpx.Timeout(
            connect=10, read=config.TIMEOUT_VIDEO_CREATE, write=30, pool=10,
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            data = await _post_json(
                client, f"{base_url}/contents/generations/tasks", payload,
                kind="video_create", model=model, headers=model_headers,
                key_name=f"model:{model}", meta=call_meta,
                idempotency_key=operation_id,
                preserve_exact_request=True,
            )
        task_id = data.get("id")
        if not task_id:
            raise ProviderError(
                f"视频任务创建响应缺少 id：{json.dumps(data, ensure_ascii=False)[:300]}"
            )
        return task_id

    async def poll_video_task(
        self,
        task_id: str,
        *,
        call_meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """轮询单次；失败时返回结构化 failure，不从错误正文推断类别。"""
        from app.hiagent import (
            ProviderError,
            ProviderFailure,
            ProviderFailureKind,
            _absolute_provider_url,
            _classify_http_error,
            _merge_call_meta,
            _model_connection,
            _timeout_phase,
            active_model,
            log_provider_call,
        )

        timeout = httpx.Timeout(
            connect=10, read=config.TIMEOUT_VIDEO_POLL, write=10, pool=10,
        )
        start = time.time()
        model = active_model("video", self.provider)
        base_url, model_headers = _model_connection(
            self.provider, model, config.HIAGENT_BASE_URL, config.HIAGENT_API_KEY,
        )
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(
                    f"{base_url}/contents/generations/tasks/{task_id}",
                    headers=model_headers,
                )
        except httpx.RequestError as exc:
            latency = int((time.time() - start) * 1000)
            err = ProviderError(
                f"视频任务状态查询网络异常：{type(exc).__name__}: {exc}",
                retryable=True,
                raw=repr(exc),
                timeout_phase=(
                    _timeout_phase(exc)
                    if isinstance(exc, httpx.TimeoutException)
                    else None
                ),
                failure_kind="connection_failed",
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
            model = active_model("video", self.provider)
            err = _classify_http_error(resp.status_code, resp.text)
            merged_meta = _merge_call_meta(call_meta)
            log_provider_call(
                "video_poll", model, "FAILED", resp.status_code, latency,
                error=str(err), meta=merged_meta,
                request_json={
                    "method": "GET",
                    "url": f"{base_url}/contents/generations/tasks/{task_id}",
                },
                response_json={"status_code": resp.status_code, "body": resp.text},
            )
            raise err
        try:
            data = resp.json()
            if not isinstance(data, dict):
                raise TypeError("expected a JSON object")
        except (TypeError, ValueError) as exc:
            err = ProviderError(
                f"视频任务状态响应不是合法 JSON 对象：{exc}",
                retryable=True,
                raw=resp.text,
                failure_kind=ProviderFailureKind.MALFORMED_RESPONSE,
                delivery_state="responded",
            )
            merged_meta = _merge_call_meta(call_meta)
            log_provider_call(
                "video_poll", model, "FAILED", 200, latency, error=str(err),
                meta=merged_meta,
                request_json={
                    "method": "GET",
                    "url": f"{base_url}/contents/generations/tasks/{task_id}",
                },
                response_json={"status_code": 200, "body": resp.text},
            )
            raise err from exc
        status = data.get("status", "")
        error_obj = data.get("error") if isinstance(data.get("error"), dict) else {}
        failure = (
            ProviderFailure.from_provider_payload(error_obj.get("failure"))
            if status == "failed"
            else None
        )
        if status == "failed":
            merged_meta = _merge_call_meta(call_meta)
            log_provider_call(
                "video_poll", active_model("video", self.provider), "TASK_FAILED",
                200, latency, meta=merged_meta,
                error=error_obj.get("message", ""),
                request_json={
                    "method": "GET",
                    "url": f"{base_url}/contents/generations/tasks/{task_id}",
                },
                response_json=data,
            )
        return {
            "status": status,
            "video_url": _absolute_provider_url(
                (data.get("content") or {}).get("video_url", ""), base_url,
            ),
            "last_frame_url": _absolute_provider_url(
                (data.get("content") or {}).get("last_frame_url", ""), base_url,
            ),
            "error": error_obj.get("message", ""),
            "failure": failure.to_payload() if failure else None,
        }

    def owns_task_id(self, task_id: str) -> bool:
        """Seedance 的 task_id 没有自有前缀，只能作为兜底承接。

        路由顺序因此必须先问带前缀的适配器；``adapter_for_task_id`` 通过
        注册顺序保证这一点，这里返回 False 让它不去抢别人的任务。
        """
        return False

    def owns_output_url(self, url: str) -> bool:
        """Seedance 产物是普通公网 URL，走 hiagent 的通用下载与 SSRF 校验。"""
        return False

    async def download_output(self, url: str, dest_path: str) -> None:
        from app.hiagent import _download_public_url

        await _download_public_url(url, dest_path)

    def capability_snapshot(self, *, provider: str, model: str):
        from app import hiagent
        from app.db import new_id, now
        from app.video_plan import ProviderVideoCapabilitySnapshot

        active_provider = hiagent.active_provider("video")
        active_model = hiagent.active_model("video", active_provider)
        observed_channel = provider == active_provider and model == active_model
        capability_verified = observed_channel
        return ProviderVideoCapabilitySnapshot(
            id=new_id("cap"),
            provider=provider,
            model=model,
            gateway=self.gateway,
            api_version="2024-01-01",
            supports_reference_image=capability_verified,
            supports_first_frame=capability_verified,
            supports_last_frame=capability_verified,
            supports_first_last_pair=capability_verified,
            supports_reference_video=capability_verified,
            supports_true_video_continuation=False,
            supports_return_last_frame=False,
            supports_data_url_by_media_type={"image": True, "video": False},
            requires_web_url_by_media_type={"image": False, "video": True},
            mutually_exclusive_input_roles=[
                ["reference_image", "first_frame"],
                ["reference_image", "last_frame"],
                ["reference_image", "reference_video"],
                ["first_frame", "reference_video"],
                ["last_frame", "reference_video"],
            ],
            duration_limits={"min_s": 5, "max_s": 10},
            size_limits={},
            format_limits={},
            probe_time=now(),
            probe_result=(
                "observed_channel_baseline_2026_08_04"
                if observed_channel else "unverified"
            ),
            technical_success=capability_verified,
            semantic_continuation_success=False,
        )

    def capability_snapshot_is_current(self, snapshot) -> bool:
        """Seedance 快照是静态基线，存下来就一直有效。"""
        return True

    def prompt_profile(self):
        from app.video_prompt_profiles import SEEDANCE_2_PROFILE

        return SEEDANCE_2_PROFILE

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
        """Seedance 不上报阶段，沿用通用的供应商总等待预算。"""
        return policy
