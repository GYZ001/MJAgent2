"""按 purpose 的优先级选路 + 故障转移。L3（与 app.hiagent/app.model_registry 同层）。

选路结果是不可分割的 :class:`ResolvedModel`——``model_id``/``protocol``/
``base_url``/``api_key``/``model_ref``/``params`` 一起返回，不允许调用方把
model 和凭据分开传递（CLAUDE.md「配套参数必须一起传递」；本仓库已因
``model``/``provider`` 分传打到错误端点栽过跟头）。

``app.hiagent.active_provider(kind)`` 自 EP-05 第二阶段起委托
:func:`resolve_provider_for_kind`，不再直接读 ``model_{kind}_provider`` 设置项
——之所以只改这一个函数：``app.hiagent`` 全文件行数基线（``app/
FILE_CONVENTIONS.toml``）已经顶到零余量，且 ``chat()``/``_post_json()`` 的重试
循环是本仓库文档最密集的一段代码（EP-05 §11 已知陷阱 1：改这条共享原语必须
跑全量测试）——``active_provider()`` 恰好是一个足够小、职责单一、外部契约只是
"返回一个字符串"的函数。

EP-05 第三阶段起，:func:`call_with_failover` 真正接入了生产调用路径：
``app.harness.model_gateway.chat`` 用它替换了原来读固定设置
``text_moderation_fallback_route`` 的 WS1b 文本审核拒答换路特例（见
``app.harness.model_gateway_moderation`` 与 ``app.harness.
model_gateway_failover.content_rejection_failover``）——按 ``text:default``
优先级链换路，不再是运维手填的单一目的地，两套机制不再并存。视频侧的
``confirm_terminal_failure`` 真实实现见 ``app.models_registry.
video_confirmation``；接入 ``app/media_exec/run_job.py`` 的多供应商换路重发
（创建新任务、切换 job 归属 provider）本阶段未做，见该模块文档与交付报告
"未完成项"——那条路径已经有一套独立的、经实战检验的防重复计费机制
（``provider_create_state``/``ProviderCreateUnresolved``），本阶段不改动它。

``sync_legacy_binding`` 是旧监制房兼容桥：4 个旧 ``model_*_provider`` 设置项
在迁移后不再是选路的权威来源（``model_bindings`` 才是），但界面上那个下拉框
还在，写它必须真的影响选路——否则就是 CLAUDE.md 明确禁止的"界面撒谎"。
"""
from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from app.models_registry import bindings, health

T = TypeVar("T")


@dataclass(frozen=True)
class ResolvedModel:
    model_id: str
    purpose: str
    priority: int
    provider: str
    protocol: str
    model_ref: str
    base_url: str
    api_key: str
    params: dict[str, Any]
    # EP-05 第三阶段新增：模型库条目上的可选 {"rpm":int,"tpm":int,"concurrency":int}，
    # 供 ratelimit.acquire 分桶用；当前模型中心（前端）还没有配置入口（下一轮范围），
    # 绝大多数条目上这是空 dict——调用方必须按"空/全零=不限速"处理，不得替它
    # 编一个默认并发上限（见 app.models_registry.ratelimit 模块文档：concurrency=0
    # 会被读成 1，不是"不限"）。
    rate_limit: dict[str, int] = field(default_factory=dict)


def _resolved_model_from_item(
    item: dict[str, Any], *, purpose: str, priority: int, params: dict[str, Any],
) -> ResolvedModel:
    return ResolvedModel(
        model_id=str(item.get("id") or ""), purpose=purpose, priority=priority,
        provider=str(item.get("provider") or ""), protocol=str(item.get("protocol") or ""),
        model_ref=str(item.get("model") or ""),
        base_url=str(item.get("base_url") or ""), api_key=str(item.get("api_key") or ""),
        params=dict(params or {}), rate_limit=dict(item.get("rate_limit") or {}),
    )


def _resolve_binding_row(row: dict[str, Any]) -> ResolvedModel | None:
    # app.model_registry（单数）是 L3，与本文件同层；延迟 import 只是为了不在
    # 模块级引入这条兄弟包依赖，两者互不 import 对方，没有真实循环风险。
    from app import model_registry

    item = model_registry.catalog_item_by_id(row["model_id"])
    if item is None or item.get("enabled") is False:
        return None
    return _resolved_model_from_item(
        item, purpose=str(row["purpose"]), priority=int(row["priority"]), params=dict(row.get("params") or {}),
    )


def resolve_explicit(provider: str, kind: str) -> ResolvedModel | None:
    """按显式 provider 字符串直接构造一个候选，不经过 purpose 优先级链。

    供"该环节已经显式指定用哪个 provider"的场景使用（世界书/映射台/分镜台的
    分环节文本模型覆盖，见 ``app.harness.text_provider_scope``）——那类选择是
    项目管理员的显式决定，不应该被 ``text:default`` 的优先级链悄悄覆盖；这里
    只是把它也包装成 ``ResolvedModel`` 形状，供调用方复用限速分桶（
    ``rate_limit``/``model_id``）与换路排除（"这个 model_id 刚失败过，下一跳
    别再选它"），不代表它参与优先级竞争。模型库里找不到这个 provider 对应的
    条目（例如从未被 ``app.model_migration`` 迁移过的裸 provider 字符串）时
    返回 ``None``，调用方据此跳过限速/换路，直接走原有的裸 provider 调用。
    """
    from app import model_registry

    item = model_registry.catalog_item_for_kind(str(provider or "").strip(), kind)
    if item is None:
        return None
    return _resolved_model_from_item(item, purpose=f"{kind}:default", priority=-1, params={})


def resolve(
    purpose: str, *, org_id: str = "", exclude_model_ids: frozenset[str] = frozenset(),
) -> ResolvedModel | None:
    """按 purpose 取 enabled 且未熔断的最小 priority 候选；全部候选都不可用
    （熔断/条目缺失）时返回 ``None``——不会悄悄回退到某个已知坏掉的候选，宁可
    如实报"当前无可用模型"（EP-05 §11 陷阱 6：模型库为空不得回退内嵌默认）。
    """
    # 4 个旧 model_*_provider 设置迁成 priority=0 绑定的触发点：该迁移真实依赖
    # app.model_registry（L3），本模块与它同层，是唯一能在模块级/合法层级下
    # import 它的地方（app.models_registry.schema 是 L2，不能承担这个触发，见
    # binding_migration.py 模块文档）。函数内幂等（MIGRATION_FLAG），重复调用
    # 成本只是一次 get_setting 查询。
    from app.models_registry.binding_migration import migrate_legacy_bindings

    migrate_legacy_bindings()
    health.refresh()
    for row in bindings.list_bindings(purpose, org_id=org_id):
        if row["model_id"] in exclude_model_ids or not health.is_selectable(row["model_id"]):
            continue
        resolved = _resolve_binding_row(row)
        if resolved is not None:
            return resolved
    return None


def resolve_provider_for_kind(kind: str) -> str:
    """``app.hiagent.active_provider`` 的新实现体。没有绑定（迁移前/未配置的
    自定义 purpose）时回落到模型库里第一条支持该 kind 的条目——与旧版
    ``active_provider`` 在"配置无效/为空"时的行为逐字段一致。
    """
    resolved = resolve(f"{kind}:default")
    if resolved is not None:
        return resolved.provider
    from app import model_registry

    candidates = model_registry.items_for_kind(kind)
    return str(candidates[0].get("provider") or "").strip() if candidates else ""


def sync_legacy_binding(kind: str, provider: str) -> None:
    """旧监制房下拉框仍在写 ``model_{kind}_provider``/``model_route`` 时，同步
    一条 priority=0 绑定，避免"保存成功但选路无变化"的界面谎言。``provider``
    存在但不支持这个 kind 时静默跳过——``normalize_setting`` 已经校验过它是
    模型库里的真实条目，这里只是不为一个不支持该能力的条目写坏绑定。
    """
    from app import model_registry

    item = model_registry.catalog_item_for_kind(provider, kind)
    if item is None:
        return
    bindings.upsert_binding(
        purpose=f"{kind}:default", model_id=str(item.get("id") or ""),
        priority=0, created_by="legacy_settings_sync",
    )


_LEGACY_PROVIDER_SETTING_KEYS = (
    ("text", "model_text_provider"), ("vlm", "model_vlm_provider"),
    ("video", "model_video_provider"), ("image", "model_image_provider"),
)


def sync_legacy_bindings(changed: dict[str, Any]) -> None:
    """``app.system_api.put_settings`` 提交设置变更后调用：``changed`` 里凡是
    4 个旧 ``model_*_provider`` 键之一，同步一条 priority=0 绑定。
    ``model_route`` 不用单独处理——写它时 ``put_settings`` 已经把它展开成
    ``model_text_provider``/``model_vlm_provider`` 一起进 ``changed``。这段
    同步逻辑属于模型库领域，特意放在这里而不是 ``system_api.py``（L5 路由层，
    行数基线已顶满零余量），路由层只需要调用它。
    """
    for kind, key in _LEGACY_PROVIDER_SETTING_KEYS:
        if key in changed:
            sync_legacy_binding(kind, changed[key])


def _classify_exception(exc: BaseException) -> str | None:
    """结构化鸭子类型分类，不 import ``app.hiagent.ProviderError``——那会让
    ``app.hiagent``（需要 import 本模块做选路）与本模块互相 import，制造真实
    循环。只认字段形状，不认异常类型或消息文本内容。返回 ``None`` 表示"不换路"
    （对应 contract_invalid：那类失败从不经过这里，见 health.py 模块文档；以及
    任何没有这套字段形状的普通异常，原样抛出更安全）。
    """
    failure_kind = str(getattr(exc, "failure_kind", "") or "")
    category = str(getattr(exc, "failure_category", "") or "")
    if getattr(exc, "timeout_phase", None) is not None or failure_kind in {
        "connection_failed", "request_outcome_unknown",
    }:
        return "timeout"
    if failure_kind == "rate_limited":
        return "rate_limited"
    if category == "model_rejection":
        return "content_rejected"
    if failure_kind:
        return "server_error"  # 已分类的其余技术失败：宁可多切一跳，不卡死在坏节点
    return None


def record_route_failure(
    purpose: str, model_id: str, priority: int, category: str, request_id: str, attempt_no: int,
) -> None:
    """落一条"model_id 在这次请求里失败、原因是 category"的换路审计行。

    公开函数（不再是 ``call_with_failover`` 私有）：``app.harness.
    model_gateway_failover.content_rejection_failover`` 也要用它——文本审核
    拒答换路的"主用 provider 失败"发生在 ``call_with_failover`` 的循环之外
    （主用调用本身走 ``model_gateway.chat`` 既有的重试路径，不经过这里），
    但同样需要留痕，否则"为什么这次用了别的模型"从审计里看不出来（CLAUDE.md
    「换路必须留痕」）。``attempt_no=0`` 表示"链路外的第一次失败"这一约定
    由调用方决定，本函数只负责落库。
    """
    from app.audit.store import insert_operation_audit_row
    from app.db import new_id
    from app.db import now as db_now

    insert_operation_audit_row({
        "id": new_id("opaudit"), "ts": db_now(),
        "user_id": None, "username": None, "is_system_admin": None,
        "source": "routing_failover", "event": "models_registry.route_failover",
        "event_label": "模型路由自动换路",
        "method": None, "path": None,
        "project_id": None, "episode_id": None, "target": model_id,
        "outcome": "failover", "http_status": None, "error_id": None, "error_code": category,
        "summary": (
            f"purpose={purpose} attempt={attempt_no} model_id={model_id} "
            f"priority={priority} reason={category} request_id={request_id}"
        ),
        "duration_ms": None, "ip": None, "user_agent": None,
        "args_json": json.dumps({
            "purpose": purpose, "model_id": model_id, "priority": priority,
            "category": category, "request_id": request_id,
        }, ensure_ascii=False),
    })


async def call_with_failover(
    purpose: str,
    fn: Callable[[ResolvedModel], Awaitable[T]],
    *,
    request_id: str,
    org_id: str = "",
    confirm_terminal_failure: Callable[[], Awaitable[bool]] | None = None,
    initial_exclude: frozenset[str] = frozenset(),
) -> T:
    """按优先级链依次尝试 ``fn(candidate)``；可换路失败换下一优先级并落审计。

    ``purpose`` 以 ``video:`` 开头时，timeout 类失败（客户端侧结果不确定）必须
    先经 ``await confirm_terminal_failure()`` 确认供应商侧确已终态失败才允许
    换路重发——视频计费，客户端超时不等于供应商侧失败（EP-05 §6 第 3 条硬
    约束）。回调是 async 的：真实确认必须向供应商发起一次轮询（见
    ``app.models_registry.video_confirmation.confirm_video_terminal_failure``），
    不可能是同步操作。未提供确认回调时一律不换路、原样抛出，不得因为想"自动
    恢复"就悄悄重发一次真实计费请求。

    ``initial_exclude`` 供调用方已经在别处试过某个 model_id（例如
    ``app.harness.model_gateway.chat`` 显式指定了某个 provider、失败后才第一次
    调用本函数换路）时把它排除在候选之外，避免重试同一个刚失败的模型——
    EP-05 第三阶段起，``app.harness.model_gateway.chat`` 的文本审核拒答换路
    （原 WS1b ``text_moderation_fallback_route`` 特例）已收编到这里，见该模块。
    """
    tried: set[str] = set(initial_exclude)
    last_exc: BaseException | None = None
    while True:
        candidate = resolve(purpose, org_id=org_id, exclude_model_ids=frozenset(tried))
        if candidate is None:
            if last_exc is not None:
                raise last_exc
            raise LookupError(f"purpose={purpose} 没有可用的模型绑定")
        tried.add(candidate.model_id)
        started = time.monotonic()
        try:
            result = await fn(candidate)
        except Exception as exc:  # noqa: BLE001 分类后决定换路还是原样抛出
            category = _classify_exception(exc)
            latency_ms = int((time.monotonic() - started) * 1000)
            if category is None:
                raise
            if category == "timeout" and purpose.startswith("video:"):
                if confirm_terminal_failure is None or not await confirm_terminal_failure():
                    raise
            health.record_outcome(candidate.model_id, category, latency_ms=latency_ms)
            record_route_failure(purpose, candidate.model_id, candidate.priority, category, request_id, len(tried))
            last_exc = exc
            continue
        health.record_outcome(candidate.model_id, None, latency_ms=int((time.monotonic() - started) * 1000))
        return result
