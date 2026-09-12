"""把旧 settings 键迁成 ``model_bindings`` 的绑定。L2（碰 db）。

迁 EP-05 §4 点名的 4 个键——``model_text_provider``/``model_vlm_provider``/
``model_video_provider``/``model_image_provider``（各自迁成 priority=0）。
``model_route`` 是历史合并开关，写它时 ``app.system_api.put_settings`` 已经
把它连带展开成 ``model_text_provider``/``model_vlm_provider`` 一起落库（见
该函数里 "model_route is a legacy shorthand" 注释），迁移这两个键已经覆盖了
它的写入路径；``model_route`` 自身当前的存量值只在 ``GET`` 状态接口里回显，
不再被任何选路决策读取（``app.hiagent.active_provider``/``routing.resolve``
都不读它），因此不需要、也没有对应的绑定可迁——它没有"归属"问题，只是一个
显示字段。

EP-05 第三阶段补一条：``text_moderation_fallback_route``（原 WS1b「文本审核
拒答换路目的地」）迁成 ``text:default`` 的 **priority=1** 绑定。这条不是
"新增能力"，是把一项**生产正在生效**的换路配置从旧机制（``app.harness.
model_gateway_moderation.attempt_moderation_fallback``，已随该阶段一起退役）
迁到接替它的 ``app.models_registry.routing.call_with_failover`` 优先级链上
——漏迁会让一项正在缓解"供应商内容审核拒绝"（全平台失败最大单一类别，见
``docs/platform-capability-review-2026-09-03.md``）的能力在部署当晚静默消失
（原来遇到拒答会换路重试并常成功，迁移缺失后会变成直接原样抛出）。
``_resolve_moderation_fallback_route_item`` 负责解析：历史文档写的格式是
"provider:model"，但生产真实值形如 ``"custom:model_07030243d87e"``——**这个
值本身就带冒号**，裸 ``split(":")``/``partition(":")`` 会把它错误劈成
``"custom"`` + ``"model_07030243d87e"`` 两个在模型库里都不存在的值。判据从
模型库数据推导，不猜分隔符：先把整串当 provider 查目录，查到就是它；查不到
才退回两段式解析，且要求 model 字段也逐字匹配。这条迁移**不受下面的
``MIGRATION_FLAG`` 整体闸门约束**——它是本阶段才补上的，生产环境很可能早已
因为第二阶段代码跑过而把闸门标成 "done"，如果绑定同一个闸门，这条迁移永远
不会被触发；改为独立幂等（设置一旦被迁移清空，后续调用读到空字符串直接
短路返回，与整体闸门是否 "done" 无关，可重复安全调用）。迁移成功后清空
``text_moderation_fallback_route`` 本身——它不再是任何代码路径的权威来源，
界面/直接查表继续看到旧值就是撒谎（CLAUDE.md「界面承诺必须与实际行为一致」）。

**硬指标**：迁移前后四个用途的实际选路（provider 字符串）必须逐字段不变。
旧版 ``app.hiagent.active_provider`` 在"配置值有效"与"配置为空/失效"两种
情况下走不同分支（前者直接用配置值，后者回落到模型库第一条支持该 kind 的
条目）——``_legacy_provider_for_kind`` 原样复刻这两条分支，不能只搬"配置有效"
这一半。

幂等与重入闸门与 ``app/models_registry/migration.py``（凭据迁移）同一手法：
``MIGRATION_FLAG`` 防重复整体执行；``_in_progress`` 防
``schema.ensure_schema()`` 触发的自举调用链在自己的调用栈里把自己再触发一次
（同一个陷阱、同一个解法，见该文件模块文档，不重复展开）。
"""
from __future__ import annotations

from typing import Any

from app.db_schema import register_table as _register_table

MIGRATION_FLAG = "models_registry_bindings_migrated_v1"

_in_progress = False

_LEGACY_KIND_SETTINGS = {
    "text": "model_text_provider",
    "vlm": "model_vlm_provider",
    "video": "model_video_provider",
    "image": "model_image_provider",
}


def _legacy_provider_for_kind(get_setting: Any, kind: str, setting_key: str) -> str:
    """复刻旧版 ``app.hiagent.active_provider`` 的回落逻辑：配置值能在模型库
    里找到支持该 kind 的条目就用它，否则退到模型库里第一条支持该 kind 的条目。
    """
    # 迁移是一次性引导操作，不是运行时热路径；app.model_registry 是 L3、本模块
    # L2，模块级 import 会构成静态分层上行边，函数内引入把它挪出静态依赖图
    # （与 app/models_registry/migration.py 里同款延迟 import 同一处理方式）。
    from app import model_registry

    configured = str(get_setting(setting_key) or "").strip()
    if configured and model_registry.catalog_item_for_kind(configured, kind):
        return configured
    candidates = model_registry.items_for_kind(kind)
    return str(candidates[0].get("provider") or "").strip() if candidates else ""


def _migrate_one_kind(get_setting: Any, kind: str, setting_key: str) -> str | None:
    from app import model_registry
    from app.models_registry import bindings

    provider = _legacy_provider_for_kind(get_setting, kind, setting_key)
    if not provider:
        return None
    item = model_registry.catalog_item_for_kind(provider, kind)
    if item is None:
        return None
    bindings.upsert_binding(
        purpose=f"{kind}:default", model_id=str(item.get("id") or ""),
        priority=0, created_by="migration",
    )
    return provider


def _resolve_moderation_fallback_route_item(get_setting: Any, kind: str = "text") -> dict[str, Any] | None:
    """按模型库数据推导 ``text_moderation_fallback_route`` 指向哪条目录条目。

    见模块文档"判据从模型库数据推导，不猜分隔符"一段：先把整串当 provider
    查，查不到才退回 "provider:model" 两段式解析，且要求 model 字段逐字匹配。
    """
    from app import model_registry

    raw = str(get_setting("text_moderation_fallback_route") or "").strip()
    if not raw:
        return None
    whole = model_registry.catalog_item_for_kind(raw, kind)
    if whole is not None:
        return whole
    provider, _, model = raw.partition(":")
    provider, model = provider.strip(), model.strip()
    if not provider or not model:
        return None
    split_item = model_registry.catalog_item_for_kind(provider, kind)
    if split_item is not None and str(split_item.get("model") or "") == model:
        return split_item
    return None


def _migrate_moderation_fallback_route(get_setting: Any, set_setting: Any) -> str | None:
    """把 ``text_moderation_fallback_route`` 迁成 ``text:default`` 的
    priority=1 绑定；迁完清空该设置。独立幂等，不受 ``MIGRATION_FLAG`` 约束
    ——原因见模块文档。返回被迁移条目的 provider 字符串，未迁移（设置本就
    为空，或解析不出有效条目）时返回 ``None``。
    """
    from app.models_registry import bindings

    item = _resolve_moderation_fallback_route_item(get_setting)
    if item is None:
        return None
    bindings.upsert_binding(
        purpose="text:default", model_id=str(item.get("id") or ""),
        priority=1, created_by="migration",
    )
    set_setting("text_moderation_fallback_route", "")
    return str(item.get("provider") or "")


def migrate_legacy_bindings(*, force: bool = False) -> dict[str, Any]:
    """执行迁移；幂等，返回可打印摘要供启动日志/手工执行确认。"""
    global _in_progress
    if _in_progress:
        return {"skipped": True, "reason": "reentrant_call"}
    _in_progress = True
    try:
        return _migrate_legacy_bindings_body(force=force)
    finally:
        _in_progress = False


def _migrate_legacy_bindings_body(*, force: bool) -> dict[str, Any]:
    from app.db import get_setting, set_setting

    # 独立于下面的整体闸门：见 _migrate_moderation_fallback_route 与模块文档
    # "这条迁移不受 MIGRATION_FLAG 整体闸门约束"一段——B 上整体闸门大概率
    # 早已是 "done"（第二阶段代码跑过），绑定同一个闸门这条迁移永远不会触发。
    fallback_provider = _migrate_moderation_fallback_route(get_setting, set_setting)

    if not force and str(get_setting(MIGRATION_FLAG) or "").strip() == "done":
        if fallback_provider is None:
            return {"skipped": True, "reason": "already_migrated"}
        migrated = {"text_fallback": fallback_provider}
        summary = f"补迁文本审核拒答换路目的地为 text:default priority=1：{fallback_provider}"
        _write_migration_audit(migrated, summary)
        return {"skipped": False, "migrated": migrated, "summary": summary}

    migrated: dict[str, str] = {}
    for kind, setting_key in _LEGACY_KIND_SETTINGS.items():
        provider = _migrate_one_kind(get_setting, kind, setting_key)
        if provider:
            migrated[kind] = provider
    if fallback_provider:
        migrated["text_fallback"] = fallback_provider

    set_setting(MIGRATION_FLAG, "done")
    summary = f"迁移 {len(migrated)} 项绑定：{migrated}"
    _write_migration_audit(migrated, summary)
    return {"skipped": False, "migrated": migrated, "summary": summary}


def _write_migration_audit(migrated: dict[str, str], summary: str) -> None:
    import json

    from app.audit.store import insert_operation_audit_row
    from app.db import new_id, now

    insert_operation_audit_row({
        "id": new_id("opaudit"), "ts": now(),
        "user_id": None, "username": None, "is_system_admin": None,
        "source": "migration", "event": "models_registry.binding_migration",
        "event_label": "模型用途绑定迁移（EP-05 第二阶段）",
        "method": None, "path": None,
        "project_id": None, "episode_id": None, "target": None,
        "outcome": "ok", "http_status": None, "error_id": None, "error_code": None,
        "summary": summary, "duration_ms": None, "ip": None, "user_agent": None,
        "args_json": json.dumps({"migrated": migrated}, ensure_ascii=False),
    })


# app.db.init_db() 不直接 import 本模块（同 app/models_registry/migration.py
# 的 P0-3 依赖倒置理由）——按名字通过 app.db_schema 查找。
_register_table("models_registry_binding_migration", lambda conn: migrate_legacy_bindings())
