"""声音生成统一入口：按 purpose 选路，按协议分发给对应适配器，统一记账。

L3（与 ``app.hiagent``/``app.models_registry.routing`` 同层）：真源是
``app.models_registry.routing.resolve()``，不自己维护第二套"哪个模型当前生效"
的判断——这正是「换绑定即换供应商，业务代码不改」的落点：本函数永远只认
purpose，不认具体供应商，调用方（人物卡音色生成流程）只需要传 purpose
（默认 ``"voice:default"``）。

付费调用不做自动重试：发出后超时是"不知道有没有扣费"，只有调用方知道要不要
让用户承担重复扣费的风险去重试（CLAUDE.md「Ownership Must Be Explicit」同一
类教训）。因此这里既不接 ``routing.call_with_failover``（那是给可安全重试的
调用准备的跨模型换路机制），也没有内部重试循环——``resolve()`` 只解析一次，
适配器调用失败就原样向上抛。
"""
from __future__ import annotations

import hashlib
import time
from typing import Any

from app import hiagent
from app.models_registry import routing
from app.voice.providers import adapter_for
from app.voice.providers.base import (
    VoiceConnection,
    VoiceDesignRequest,
    VoiceDesignResult,
    VoiceProviderError,
)


def _connection_from_resolved(resolved: routing.ResolvedModel) -> VoiceConnection:
    return VoiceConnection(
        base_url=resolved.base_url, api_key=resolved.api_key,
        model_ref=resolved.model_ref, params=dict(resolved.params or {}),
    )


def _redact_result_for_log(result: VoiceDesignResult) -> dict[str, Any]:
    """落库前把音频字节换成「长度 + sha256 前16位」占位。

    ``app.observability.provider_call_payload.compact_provider_payload`` 只
    压缩 ``;base64,`` 形态的 data URL——千问返回裸 base64、MiniMax 返回十六
    进制，都不会被它压缩，必须在这里自己脱敏，否则 ``provider_calls`` 会被
    真实音频字节撑爆（同 2026-09-06 B 上参考图撑到 6.67GB 写锁风暴那类故障）。
    """
    digest = hashlib.sha256(result.audio).hexdigest()[:16]
    return {
        "provider_voice_id": result.provider_voice_id,
        "audio": f"[omitted {len(result.audio)} bytes sha256:{digest}]",
        "audio_format": result.audio_format,
        "sample_rate": result.sample_rate,
        "request_id": result.request_id,
        "latency_ms": result.latency_ms,
    }


def resolve_voice_model(purpose: str = "voice:default") -> routing.ResolvedModel | None:
    """先按 purpose 的优先级绑定选路；没有绑定时回落到模型库里第一条同能力的模型——
    与 ``hiagent.active_provider`` 的既有回落、也就是模型中心「当前运行」显示的是同
    一个条目。只认绑定会出现界面显示「当前运行 X」、生成却报「未配置」的界面撒谎
    （2026-09-24 用户实测）。"""
    resolved = routing.resolve(purpose)
    if resolved is not None:
        return resolved
    kind = purpose.split(":", 1)[0]
    provider = routing.resolve_provider_for_kind(kind)
    return routing.resolve_explicit(provider, kind) if provider else None


async def design_voice(
    req: VoiceDesignRequest, *, purpose: str = "voice:default", call_meta: dict[str, Any] | None = None,
) -> VoiceDesignResult:
    """凭文字描述生成一个新音色；未配置或协议不认识都明确报错，不静默回落。"""
    resolved = resolve_voice_model(purpose)
    if resolved is None:
        raise VoiceProviderError("未配置声音生成模型，请在模型中心添加并绑定", failure_kind="not_configured")
    adapter = adapter_for(resolved.protocol)
    if adapter is None:
        raise VoiceProviderError(f"未知的声音生成协议：{resolved.protocol}", failure_kind="invalid_request")
    conn = _connection_from_resolved(resolved)
    meta = {**(call_meta or {}), "purpose": purpose, "provider": resolved.provider}
    request_log = {
        "voice_prompt": req.voice_prompt, "preview_text": req.preview_text,
        "preferred_name": req.preferred_name, "model_ref": conn.model_ref,
    }
    started = time.perf_counter()
    try:
        result = await adapter.design_voice(conn, req)
    except VoiceProviderError as exc:
        hiagent.log_provider_call(
            "voice_design", conn.model_ref, "FAILED", exc.http_status,
            int((time.perf_counter() - started) * 1000), error=str(exc)[:500],
            meta=meta, request_json=request_log,
        )
        raise
    hiagent.log_provider_call(
        "voice_design", conn.model_ref, "OK", 200, result.latency_ms,
        meta=meta, request_json=request_log, response_json=_redact_result_for_log(result),
    )
    return result


async def probe_voice_model(protocol: str, base_url: str, api_key: str, model_ref: str) -> dict[str, Any]:
    """模型中心「测试连接」专用：不经过 purpose 选路，直接按协议探测。"""
    adapter = adapter_for(protocol)
    if adapter is None:
        raise VoiceProviderError(f"未知的声音生成协议：{protocol}", failure_kind="invalid_request")
    conn = VoiceConnection(base_url=base_url, api_key=api_key, model_ref=model_ref, params={})
    return await adapter.probe(conn)
