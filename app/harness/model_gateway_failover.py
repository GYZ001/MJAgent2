"""``app.harness.model_gateway.chat`` 的限速分桶 + 换路辅助（拆出以避免把
model_gateway.py 推过文件行数基线，与同目录 ``model_gateway_moderation.py``
同一手法）。L3（与 model_gateway 同层）。

EP-05 第三阶段把 ``app.models_registry.routing.call_with_failover`` 与
``app.models_registry.ratelimit.acquire`` 真正接进这条生产调用路径：

- :func:`rate_limit_candidate` / :func:`rate_limit_scope`：按模型库条目上
  可选的 ``rate_limit``（``{"rpm","tpm","concurrency"}``）分桶限速；当前
  生产没有任何条目配置这个字段（模型中心还没有配置入口，属下一轮范围），
  所以这套限速对现有部署是纯粹的 no-op——真实效果只在测试里显式配置
  ``rate_limit`` 时才能看到，如实标注不假装已经在生产限速。
- :func:`content_rejection_failover`：原 WS1b「文本审核拒答换路」特例（固定
  读 ``settings.text_moderation_fallback_route``）已收编到这里，改按
  ``text:default`` 优先级链换路，两套机制不再并存，见
  ``model_gateway_moderation.py`` 模块文档。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator


def rate_limit_candidate(provider: str | None) -> Any | None:
    """尽力解析出本次文本调用对应的模型库条目，仅用于限速分桶。

    显式 provider（环节覆盖）走 ``routing.resolve_explicit``，不参与优先级
    竞争；否则按 ``text:default`` 的当前选路结果分桶——与 ``hiagent.chat``
    内部即将各自独立完成的选路是同一份数据，重复解析一次的代价是可接受的
    （EP-05 §7：限速与选路是正交维度）。任何解析失败（模型库为空、条目缺失）
    都返回 None——限速因此对这次调用静默跳过，不得因为分桶失败就阻断真实
    调用（EP-05 §11 陷阱 6 同一姿态）。
    """
    from app.models_registry import routing

    try:
        if provider:
            return routing.resolve_explicit(provider, "text")
        return routing.resolve("text:default")
    except Exception:  # noqa: BLE001 限速分桶失败不得影响真实调用
        return None


@asynccontextmanager
async def rate_limit_scope(candidate: Any | None, *, estimated_tokens: int) -> AsyncIterator[None]:
    """按 ``candidate.rate_limit`` 显式配置才生效的进程内限速上下文管理器。

    未配置时是纯粹的 no-op：不得因为"没配置"就套用 ``ratelimit.acquire`` 的
    参数默认值——``concurrency=0`` 会被读成 1（信号量不能是 0 容量），把一个
    从未打算限速的模型意外压到并发 1，见 ``app.models_registry.ratelimit``
    模块文档。
    """
    limits = dict(getattr(candidate, "rate_limit", None) or {}) if candidate is not None else {}
    rpm, tpm = int(limits.get("rpm") or 0), int(limits.get("tpm") or 0)
    concurrency = int(limits.get("concurrency") or 0)
    if rpm <= 0 and tpm <= 0 and concurrency <= 0:
        yield
        return
    from app.models_registry import ratelimit

    async with await ratelimit.acquire(
        candidate.model_id, rpm=rpm, tpm=tpm,
        concurrency=concurrency or 10_000,  # 只配了 rpm/tpm 时不额外加并发上限
        estimated_tokens=estimated_tokens,
    ):
        yield


async def content_rejection_failover(
    messages: list[dict[str, str]], provider_kwargs: dict[str, Any],
    meta: dict[str, Any], current_provider: str | None, request_id: str,
) -> str | None:
    """文本审核拒答换路（原 WS1b 特例）收编进统一策略（EP-05 第三阶段）。

    不再读固定的 ``settings.text_moderation_fallback_route``，改用
    ``routing.call_with_failover`` 按 ``text:default`` 优先级链换路——换路
    目的地从模型库/用途绑定推导，不再是运维手填的单一目的地。失败（含链路
    耗尽、未配置任何绑定）一律返回 None，调用方（``model_gateway.chat``）据此
    保留并抛出第一次的原始错误，不被换路请求自己的报错覆盖（CLAUDE.md「界面
    承诺必须与实际行为一致」：调用方看到的应当是"这次请求为什么失败"）。

    主用 provider 的这次拒答发生在 ``call_with_failover`` 的循环之外（主用
    调用本身走 ``model_gateway.chat`` 既有的重试路径），单独落一条
    ``attempt_no=0`` 的换路审计，否则"为什么这次用了别的模型"只能看到换路
    *之后*那一段、看不到*为什么离开*主用模型（CLAUDE.md「换路必须留痕」）。
    """
    from app import hiagent
    from app.generation_concurrency import run_with_provider_call_slot
    from app.harness.model_gateway_moderation import framed_moderation_messages
    from app.models_registry import routing

    primary = (
        routing.resolve_explicit(current_provider, "text") if current_provider
        else routing.resolve("text:default")
    )
    exclude = frozenset({primary.model_id}) if primary is not None else frozenset()
    if primary is not None:
        routing.record_route_failure(
            "text:default", primary.model_id, primary.priority, "content_rejected", request_id, 0,
        )

    async def _fn(candidate: routing.ResolvedModel) -> str:
        kwargs = {
            **provider_kwargs, "provider": candidate.provider, "model": candidate.model_ref,
            "call_meta": {**meta, "moderation_fallback": True},
        }
        return await run_with_provider_call_slot(
            lambda: hiagent.chat(framed_moderation_messages(messages), **kwargs)
        )

    try:
        return await routing.call_with_failover(
            "text:default", _fn, request_id=request_id, initial_exclude=exclude,
        )
    except Exception:  # noqa: BLE001 换路链路耗尽/未配置：交回原始错误，见函数文档
        return None
