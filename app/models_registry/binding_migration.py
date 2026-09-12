"""把 4 个旧 settings 键迁成 ``model_bindings`` 的 priority=0 绑定。L2（碰 db）。

只迁 EP-05 §4 点名的 4 个键——``model_text_provider``/``model_vlm_provider``/
``model_video_provider``/``model_image_provider``。``model_route`` 是历史合并
开关，写它时 ``app.system_api.put_settings`` 已经把它连带展开成
``model_text_provider``/``model_vlm_provider`` 一起落库（见该函数里
"model_route is a legacy shorthand" 注释），迁移这两个键已经覆盖了它，不需要
再单独迁一次。

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

    if not force and str(get_setting(MIGRATION_FLAG) or "").strip() == "done":
        return {"skipped": True, "reason": "already_migrated"}

    migrated: dict[str, str] = {}
    for kind, setting_key in _LEGACY_KIND_SETTINGS.items():
        provider = _migrate_one_kind(get_setting, kind, setting_key)
        if provider:
            migrated[kind] = provider

    set_setting(MIGRATION_FLAG, "done")
    summary = f"迁移 {len(migrated)}/4 个用途的 priority=0 绑定：{migrated}"
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
