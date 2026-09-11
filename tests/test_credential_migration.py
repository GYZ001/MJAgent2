"""app.models_registry.migration 的三处来源迁移证明。

覆盖 EP-05 第一阶段迁移清单：
1. settings.model_credentials（显式覆盖）→ model_credentials 表。
2. settings.custom_models 内联 api_key（自定义服务商）→ 表，且从目录里剥离明文。
3. .env 的 provider 级 Key（当目录条目自己没有任何 Key 时的兜底）→ 表。
4. 幂等：重复调用不产生重复行，也不会拿旧明文覆盖掉迁移后发生的真实轮换。
5. settings.model_credentials 迁移后置空；落一条 operation_audit 记录。
"""
from __future__ import annotations

import json

from app.db import get_conn, get_setting, set_setting
from app.models_registry import store
from app.models_registry.migration import MIGRATION_FLAG, migrate_model_credentials


def _set_catalog(items: list[dict]) -> None:
    set_setting("custom_models", json.dumps(items, ensure_ascii=False))


def _set_plaintext_credentials(mapping: dict) -> None:
    set_setting("model_credentials", json.dumps(mapping, ensure_ascii=False))


def test_migrates_explicit_settings_override_into_table() -> None:
    _set_catalog([{"id": "mdl_x1", "provider": "hiagent", "model": "m", "label": "L", "kinds": ["text"]}])
    _set_plaintext_credentials({"mdl_x1": {"base_url": "https://gw.example.com/v1", "api_key": "sk-explicit"}})

    result = migrate_model_credentials(force=True)

    assert result["skipped"] is False
    assert "mdl_x1" in result["credentials_by_source"].get("settings.model_credentials", [])
    saved = store.get_credential("mdl_x1")
    assert saved == {"base_url": "https://gw.example.com/v1", "api_key": "sk-explicit"}


def test_migrates_inline_custom_provider_key_and_strips_catalog() -> None:
    _set_catalog([{
        "id": "mdl_inline1", "provider": "custom:mdl_inline1", "provider_label": "网关",
        "model": "m", "label": "L", "kinds": ["text"], "protocol": "openai",
        "base_url": "https://gw.example.com/v1", "api_key": "sk-inline",
    }])
    _set_plaintext_credentials({})

    result = migrate_model_credentials(force=True)

    assert "mdl_inline1" in result["credentials_by_source"].get("custom_models(inline)", [])
    assert "mdl_inline1" in result["stripped_inline"]
    saved = store.get_credential("mdl_inline1")
    assert saved["api_key"] == "sk-inline"
    catalog = json.loads(get_setting("custom_models") or "[]")
    item = next(m for m in catalog if m["id"] == "mdl_inline1")
    assert "api_key" not in item  # 连空串都不留，不是留一个假的"已配置"信号


def test_migrates_env_fallback_when_item_has_no_own_key(monkeypatch) -> None:
    from app import config

    monkeypatch.setattr(config, "HIAGENT_API_KEY", "env-hiagent-secret")
    _set_catalog([{"id": "mdl_env1", "provider": "hiagent", "model": "m", "label": "L", "kinds": ["text"]}])
    _set_plaintext_credentials({})

    result = migrate_model_credentials(force=True)

    assert "mdl_env1" in result["credentials_by_source"].get(".env:HIAGENT_API_KEY", [])
    saved = store.get_credential("mdl_env1")
    assert saved["api_key"] == "env-hiagent-secret"


def test_does_not_migrate_env_key_for_custom_provider() -> None:
    """custom: 前缀条目不属于任何 6 家 provider 家族，没有 env 兜底可言——
    没配自己的 Key 就应该保持"未配置"，不能凭空冒出一把 Key。"""
    _set_catalog([{
        "id": "mdl_custom_noenv", "provider": "custom:mdl_custom_noenv",
        "model": "m", "label": "L", "kinds": ["text"], "protocol": "openai",
    }])
    _set_plaintext_credentials({})

    migrate_model_credentials(force=True)

    assert store.has_credential("mdl_custom_noenv") is False


def test_second_run_does_not_duplicate_rows() -> None:
    _set_catalog([{"id": "mdl_dup1", "provider": "hiagent", "model": "m", "label": "L", "kinds": ["text"]}])
    _set_plaintext_credentials({"mdl_dup1": {"base_url": "", "api_key": "sk-once"}})

    migrate_model_credentials(force=True)
    migrate_model_credentials(force=True)

    rows = get_conn().execute(
        "SELECT COUNT(*) AS c FROM model_credentials WHERE model_id=?", ("mdl_dup1",)
    ).fetchone()
    assert rows["c"] == 1


def test_forced_rerun_does_not_clobber_a_rotation_that_happened_after_migration() -> None:
    """幂等不只是"不重复"，还要"不倒退"：迁移后管理员通过新加密流程轮换过的
    Key，被迫重跑迁移时不能被旧明文覆盖回去——has_credential 已存在即跳过。"""
    _set_catalog([{"id": "mdl_rot1", "provider": "hiagent", "model": "m", "label": "L", "kinds": ["text"]}])
    _set_plaintext_credentials({"mdl_rot1": {"base_url": "", "api_key": "sk-stale"}})
    migrate_model_credentials(force=True)

    store.put_credential("mdl_rot1", base_url="", api_key="sk-rotated-after-migration", rotated_by="admin")

    migrate_model_credentials(force=True)

    assert store.get_credential("mdl_rot1")["api_key"] == "sk-rotated-after-migration"


def test_flag_gated_run_skips_without_force() -> None:
    set_setting(MIGRATION_FLAG, "done")
    result = migrate_model_credentials()
    assert result == {"skipped": True, "reason": "already_migrated"}


def test_settings_model_credentials_cleared_after_migration() -> None:
    _set_catalog([{"id": "mdl_clear1", "provider": "hiagent", "model": "m", "label": "L", "kinds": ["text"]}])
    _set_plaintext_credentials({"mdl_clear1": {"base_url": "", "api_key": "sk-clear"}})

    migrate_model_credentials(force=True)

    assert json.loads(get_setting("model_credentials") or "{}") == {}


def test_migration_writes_an_audit_row() -> None:
    _set_catalog([{"id": "mdl_audit1", "provider": "hiagent", "model": "m", "label": "L", "kinds": ["text"]}])
    _set_plaintext_credentials({"mdl_audit1": {"base_url": "", "api_key": "sk-audit"}})

    migrate_model_credentials(force=True)

    row = get_conn().execute(
        "SELECT outcome, event FROM operation_audit WHERE event=? ORDER BY ts DESC LIMIT 1",
        ("models_registry.credential_migration",),
    ).fetchone()
    assert row is not None
    assert row["outcome"] == "ok"


def test_routing_resolves_identically_before_and_after_migration() -> None:
    """硬指标：选路行为零变化。构造一条同时有 settings.model_credentials 覆盖
    与 custom_models 内联 api_key 的条目（历史上真实存在的组合），按迁移前的
    优先级公式手算期望值，迁移后用真实调用点（``app.hiagent._model_connection``）
    验证解出的 base_url/api_key 逐字段相同。"""
    from app import hiagent

    item = {
        "id": "mdl_zero1", "provider": "custom:mdl_zero1", "provider_label": "网关",
        "model": "gpt-zero", "label": "L", "kinds": ["text"], "protocol": "openai",
        "base_url": "https://inline.example.com/v1", "api_key": "sk-inline-fallback",
    }
    override = {"base_url": "https://override.example.com/v1", "api_key": "sk-override-wins"}
    _set_catalog([item])
    _set_plaintext_credentials({"mdl_zero1": override})

    # 迁移前公式（app/hiagent.py 旧版 _model_connection 的优先级）：
    expected_base_url = override["base_url"]
    expected_api_key = override["api_key"]

    migrate_model_credentials(force=True)

    resolved_base_url, headers = hiagent._model_connection("custom:mdl_zero1", "gpt-zero")
    assert resolved_base_url == expected_base_url
    assert headers["Authorization"] == f"Bearer {expected_api_key}"


def test_items_missing_id_are_skipped_not_crashed() -> None:
    _set_catalog([{"provider": "hiagent", "model": "m", "label": "无 id"}])
    _set_plaintext_credentials({})

    result = migrate_model_credentials(force=True)

    assert result["skipped"] is False
    assert result["models_mirrored"] == []
