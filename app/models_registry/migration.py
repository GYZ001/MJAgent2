"""把散落在三处的模型与密钥迁进 ``app.models_registry`` 的真表。L2（碰 db）。

背景见 ``PRD/enterprise/EP-05_模型管理与智能路由.md`` §1：API Key 明文落
``settings`` 表、明文写 ``.env``，无法查询/索引/加密。这份迁移不改变任何一条
现有读取路径的"来源优先级"（settings.custom_models 的目录、.env 的 provider
级兜底继续被 ``app/hiagent.py``/``app/video_providers.py`` 按原顺序读取），只是
把"能确定属于哪个模型"的密钥换成加密存储、可查询的新表：

1. ``settings.custom_models``——自定义服务商（``provider`` 形如 ``custom:xxx``）
   条目上内联的 ``api_key``（历史上直接存在目录条目里，是仅次于
   ``model_credentials`` 的第二个明文来源）。迁移后从目录条目里删掉这个字段
   （不是留着不用——留着的话解密工作白做，"库里无明文"这条验收标准过不了）。
2. ``settings.model_credentials``（JSON 对象，``{model_id: {base_url, api_key}}``）
   ——本来就是"这个模型用这把 Key"的显式覆盖，直接搬。
3. ``.env`` 的 6 个 provider 级 ``*_API_KEY``——不专属任何一个模型，是"这个
   供应商家族缺自己的 Key 时"的运行时兜底（``app/hiagent.py::_model_connection``
   的 ``fallback_key`` 参数）。只对"当前确实在吃这份兜底、自己没有任何别的
   Key"的目录条目落一条加密记录；``.env`` 本身不清空、继续作为兜底读取源，
   所有读取路径的解析结果因此不变。

幂等：``MIGRATION_FLAG`` 标记位保证只整体跑一次（重启不重复执行、不会拿旧
明文覆盖掉迁移之后管理员通过新加密流程做过的轮换）；即便标记位失效被迫重跑，
``model_credentials`` 表 ``model_id`` 主键的 UPSERT 与"已有记录则跳过"双重兜底
也保证不产生重复行。
"""
from __future__ import annotations

import json
from typing import Any

from app.db_schema import register_table as _register_table

MIGRATION_FLAG = "models_registry_credentials_migrated_v1"

# 重入闸门：migrate_model_credentials() 内部经 store.upsert_model()/put_credential()
# 调用 schema.ensure_schema()（建表后自动触发一次迁移）；当迁移本身就是"表刚建好
# 第一次被摸到"的那个调用者时，这条链会在自己的调用栈里把自己再触发一次——
# 内层这次会在每个 model_id 上抢先落库，外层循环随后看到 has_credential=True 就
# 提前 return，credentials_by_source 统计不到任何一条（实测：直接调
# migrate_model_credentials(force=True) 时触发，tests/test_credential_migration.py
# 三个用例假绿）。不用 threading.Lock：这条路径全程同步、单线程重入，一个模块级
# 布尔量就够，且不会跨请求持锁。
_in_progress = False

def _env_key_attr(provider: str) -> str:
    """provider 字面量 → app.config 里对应的 env 变量属性名，从
    ``app.config.MANAGED_KEYS`` 机械反推（与 ``app/system_api.py`` 里
    ``provider_to_key`` 同一手法，EP-05 第三阶段清理）——不再单独维护一份
    6 家供应商的硬编码拷贝，两份表迟早漂移。"""
    from app import config

    return {
        name.replace("_API_KEY", "").lower(): name for name in config.MANAGED_KEYS
    }.get(provider, "")


def _json_setting(get_setting: Any, key: str, fallback: Any) -> Any:
    try:
        value = json.loads(get_setting(key) or "")
    except (TypeError, ValueError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def _resolve_secret(
    item: dict[str, Any], override: dict[str, Any]
) -> tuple[str, str, str]:
    """按现有运行时同款优先级解出 (api_key, base_url, 来源标签)。"""
    from app import config

    api_key = str(override.get("api_key") or "").strip()
    base_url = str(override.get("base_url") or "").strip()
    if api_key:
        return api_key, base_url, "settings.model_credentials"

    inline_key = str(item.get("api_key") or "").strip()
    if inline_key:
        return inline_key, base_url or str(item.get("base_url") or "").strip(), "custom_models(inline)"

    provider = str(item.get("provider") or "").strip()
    env_attr = _env_key_attr(provider)
    if env_attr:
        env_key = str(getattr(config, env_attr, "") or "").strip()
        if env_key:
            return env_key, base_url, f".env:{env_attr}"

    return "", base_url, ""


def _migrate_one_item(item: dict[str, Any], plaintext_credentials: dict[str, Any]) -> dict[str, Any] | None:
    """落表一条目录条目，需要时把它的凭据加密迁进新表。

    返回 ``None`` 表示这条没有 id、整体跳过；否则返回
    ``{"model_id", "source"（迁了凭据才有）, "stripped_inline"（剥离了内联明文才有）}``。
    """
    from app.models_registry import store

    model_id = str(item.get("id") or "").strip()
    if not model_id:
        return None
    store.upsert_model(item, created_by="migration")
    result: dict[str, Any] = {"model_id": model_id}
    if store.has_credential(model_id):
        return result  # 已经有加密记录（比如上次强制重跑过），不拿旧明文覆盖
    override = plaintext_credentials.get(model_id)
    override = override if isinstance(override, dict) else {}
    api_key, base_url, source = _resolve_secret(item, override)
    if not api_key:
        return result
    store.put_credential(model_id, base_url=base_url, api_key=api_key, rotated_by="migration")
    result["source"] = source
    if "api_key" in item:
        # 判据是"目录项上还有没有这个字段"，不是"这次落库用的是哪个来源的值"：
        # 生产数据演练抓到的真漏——一条目录项同时有内联 api_key 和
        # settings.model_credentials 覆盖、且两者不同值时，上面按优先级选中了
        # 凭据表那把（正确），但旧判据 source == "custom_models(inline)" 为
        # False，从没走到这里，内联明文就一直留在 custom_models 里没剔除。
        item.pop("api_key")
        result["stripped_inline"] = True
    return result


def _apply_item_migrations(
    catalog: list[Any], plaintext_credentials: dict[str, Any]
) -> tuple[list[str], dict[str, list[str]], list[str], bool]:
    """对目录里每一条跑 ``_migrate_one_item``，聚合成汇总统计。"""
    models_mirrored: list[str] = []
    credentials_by_source: dict[str, list[str]] = {}
    stripped_inline: list[str] = []
    catalog_changed = False
    for item in catalog:
        if not isinstance(item, dict):
            continue
        outcome = _migrate_one_item(item, plaintext_credentials)
        if outcome is None:
            continue
        models_mirrored.append(outcome["model_id"])
        source = outcome.get("source")
        if source:
            credentials_by_source.setdefault(source, []).append(outcome["model_id"])
        if outcome.get("stripped_inline"):
            stripped_inline.append(outcome["model_id"])
            catalog_changed = True
    return models_mirrored, credentials_by_source, stripped_inline, catalog_changed


def _write_migration_audit(
    models_mirrored: list[str], credentials_by_source: dict[str, list[str]], stripped_inline: list[str]
) -> str:
    """落一条 operation_audit 记录，返回摘要文本供启动日志打印。"""
    from app.audit.store import insert_operation_audit_row
    from app.db import new_id, now

    total = sum(len(v) for v in credentials_by_source.values())
    summary = (
        f"models 落表 {len(models_mirrored)} 条；凭据迁移 {total} 条"
        f"（按来源：{ {k: len(v) for k, v in credentials_by_source.items()} }）；"
        f"从 custom_models 剥离内联明文 {len(stripped_inline)} 条；settings.model_credentials 已置空"
    )
    insert_operation_audit_row({
        "id": new_id("opaudit"), "ts": now(),
        "user_id": None, "username": None, "is_system_admin": None,
        "source": "migration", "event": "models_registry.credential_migration",
        "event_label": "模型凭据迁移（EP-05 第一阶段）",
        "method": None, "path": None,
        "project_id": None, "episode_id": None, "target": None,
        "outcome": "ok", "http_status": None, "error_id": None, "error_code": None,
        "summary": summary, "duration_ms": None, "ip": None, "user_agent": None,
        "args_json": json.dumps(
            {"models": len(models_mirrored), "credentials": total,
             "sources": {k: len(v) for k, v in credentials_by_source.items()}},
            ensure_ascii=False,
        ),
    })
    return summary


def migrate_model_credentials(*, force: bool = False) -> dict[str, Any]:
    """执行迁移；幂等，返回可打印摘要供启动日志/手工执行确认。"""
    global _in_progress
    if _in_progress:
        return {"skipped": True, "reason": "reentrant_call"}
    _in_progress = True
    try:
        return _migrate_model_credentials_body(force=force)
    finally:
        _in_progress = False


def _migrate_model_credentials_body(*, force: bool) -> dict[str, Any]:
    """``migrate_model_credentials`` 去掉重入闸门之后的实际迁移逻辑。"""
    from app.db import get_setting, set_setting

    if not force and str(get_setting(MIGRATION_FLAG) or "").strip() == "done":
        return {"skipped": True, "reason": "already_migrated"}

    catalog = _json_setting(get_setting, "custom_models", [])
    plaintext_credentials = _json_setting(get_setting, "model_credentials", {})
    if not isinstance(plaintext_credentials, dict):
        plaintext_credentials = {}

    models_mirrored, credentials_by_source, stripped_inline, catalog_changed = _apply_item_migrations(
        catalog, plaintext_credentials,
    )

    if catalog_changed:
        set_setting("custom_models", json.dumps(catalog, ensure_ascii=False))
    set_setting("model_credentials", "{}")
    set_setting(MIGRATION_FLAG, "done")
    summary = _write_migration_audit(models_mirrored, credentials_by_source, stripped_inline)
    return {
        "skipped": False,
        "models_mirrored": models_mirrored,
        "credentials_by_source": credentials_by_source,
        "stripped_inline": stripped_inline,
        "summary": summary,
    }


# app.db.init_db() 不直接 import 本模块（P0-3 依赖倒置，见
# docs/coupling_review_2026-08-29.md 第2步）——按名字通过 app.db_schema 查找。
_register_table("models_registry_migration", lambda conn: migrate_model_credentials())
