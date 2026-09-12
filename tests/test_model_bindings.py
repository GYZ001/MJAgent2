"""``model_bindings`` 表读写、purpose 目录/自检、4 个旧 settings 键的迁移。

EP-05 第二阶段。迁移场景按生产真实形状造数据（合成假值，不是最小可复现的
单条目玩具数据）——上一阶段在这里栽过一次："测试数据按最小可复现造，漏掉了
生产真实形状"，B 上真实是 7 条目录项、5 条凭据表条目（3 条共享同一把 Key）、
四个用途全部指向 ``custom:*`` 条目。见 ``test_migration_matches_pre_migration_routing_field_by_field``。
"""
from __future__ import annotations

import json

from app.db import get_setting, set_setting
from app.models_registry import bindings, purposes, routing, store
from app.models_registry.binding_migration import migrate_legacy_bindings


def _custom_item(idx: int, kind: str, *, base_url_suffix: str | None = None) -> dict:
    suffix = base_url_suffix or f"gw{idx}"
    return {
        "id": f"model_{idx}", "provider": f"custom:model_{idx}", "model": f"{kind}-model-{idx}",
        "label": f"测试模型{idx}", "kinds": [kind], "builtin": False,
        "protocol": "openai", "base_url": f"https://{suffix}.example.test/v1",
    }


def _set_catalog(items: list[dict]) -> None:
    set_setting("custom_models", json.dumps(items, ensure_ascii=False))


def _seven_item_catalog() -> list[dict]:
    """7 条目录项：4 条各自撑起一个用途（text/vlm/video/image），另 3 条是同
    kind 的额外条目（真实部署里"同一能力配了不止一家"的常态噪音）。"""
    return [
        _custom_item(1, "text"), _custom_item(2, "vlm"),
        _custom_item(3, "video"), _custom_item(4, "image"),
        _custom_item(5, "text"), _custom_item(6, "text"), _custom_item(7, "vlm"),
    ]


def _seed_credentials_realistic_shape() -> None:
    """5 条凭据表条目，其中 3 条（5/6/7）共用同一把 Key——生产真实形状的
    "共享 Key" 那部分；1/2/3/4 各自独立。model_5..7 故意不接任何用途绑定，
    验证共享 Key 不会污染其它用途的选路。"""
    store.put_credential("model_1", base_url="https://gw1.example.test/v1", api_key="sk-fake-AAAA1111", rotated_by="test")
    store.put_credential("model_2", base_url="https://gw2.example.test/v1", api_key="sk-fake-BBBB2222", rotated_by="test")
    store.put_credential("model_3", base_url="https://gw3.example.test/v1", api_key="sk-fake-CCCC3333", rotated_by="test")
    store.put_credential("model_4", base_url="https://gw4.example.test/v1", api_key="sk-fake-DDDD4444", rotated_by="test")
    for idx in (5, 6, 7):
        store.put_credential(f"model_{idx}", base_url=f"https://gw{idx}.example.test/v1", api_key="sk-fake-SHARED9999", rotated_by="test")


def _set_legacy_provider_settings() -> None:
    set_setting("model_text_provider", "custom:model_1")
    set_setting("model_vlm_provider", "custom:model_2")
    set_setting("model_video_provider", "custom:model_3")
    set_setting("model_image_provider", "custom:model_4")


def test_migration_matches_pre_migration_routing_field_by_field() -> None:
    """硬指标：迁移前后四个用途的实际选路（provider/model/base_url/api_key）
    逐字段不变。"""
    _set_catalog(_seven_item_catalog())
    _seed_credentials_realistic_shape()
    _set_legacy_provider_settings()

    result = migrate_legacy_bindings(force=True)
    assert result["skipped"] is False
    assert result["migrated"] == {
        "text": "custom:model_1", "vlm": "custom:model_2",
        "video": "custom:model_3", "image": "custom:model_4",
    }

    expected = {
        "text:default": ("custom:model_1", "text-model-1", "sk-fake-AAAA1111"),
        "vlm:default": ("custom:model_2", "vlm-model-2", "sk-fake-BBBB2222"),
        "video:default": ("custom:model_3", "video-model-3", "sk-fake-CCCC3333"),
        "image:default": ("custom:model_4", "image-model-4", "sk-fake-DDDD4444"),
    }
    for purpose, (provider, _label_hint, api_key) in expected.items():
        resolved = routing.resolve(purpose)
        assert resolved is not None, purpose
        assert resolved.provider == provider
        assert resolved.api_key == api_key
        assert resolved.base_url.startswith("https://gw")


def test_migration_idempotent_does_not_overwrite_later_manual_rebind() -> None:
    _set_catalog(_seven_item_catalog())
    _seed_credentials_realistic_shape()
    _set_legacy_provider_settings()

    migrate_legacy_bindings(force=True)
    assert get_setting("models_registry_bindings_migrated_v1") == "done"

    # 迁移后管理员手工把 text 用途换绑到 model_5；再跑一次迁移（非 force）必须
    # 是 no-op，不能拿旧的 settings 值把这次手工绑定覆盖回去。
    bindings.upsert_binding(purpose="text:default", model_id="model_5", priority=0, created_by="admin")
    second = migrate_legacy_bindings()
    assert second["skipped"] is True
    assert bindings.get_priority_zero("text:default")["model_id"] == "model_5"


def test_migration_falls_back_to_first_catalog_item_when_configured_value_invalid() -> None:
    """配置值指向不存在/不支持该 kind 的条目时，回落到模型库里第一条支持该
    kind 的条目——逐字段复刻旧版 app.hiagent.active_provider 的回落分支。"""
    catalog = [_custom_item(5, "text"), _custom_item(1, "text")]
    _set_catalog(catalog)
    store.put_credential("model_5", base_url="https://gw5.example.test/v1", api_key="sk-fake-5", rotated_by="test")
    store.put_credential("model_1", base_url="https://gw1.example.test/v1", api_key="sk-fake-1", rotated_by="test")
    set_setting("model_text_provider", "custom:does-not-exist")

    result = migrate_legacy_bindings(force=True)

    # 目录里第一条（插入顺序）支持 text 的条目是 model_5，不是配置里写的无效值。
    assert result["migrated"]["text"] == "custom:model_5"
    assert bindings.get_priority_zero("text:default")["model_id"] == "model_5"


def test_migration_skips_kind_when_catalog_has_none() -> None:
    _set_catalog([])
    set_setting("model_text_provider", "custom:whatever")

    result = migrate_legacy_bindings(force=True)

    assert "text" not in result["migrated"]
    assert bindings.get_priority_zero("text:default") is None


def test_bindings_upsert_conflict_updates_not_duplicates() -> None:
    _set_catalog([_custom_item(1, "text")])
    first_id = bindings.upsert_binding(purpose="text:screenplay", model_id="model_1", priority=0, created_by="a")
    second_id = bindings.upsert_binding(purpose="text:screenplay", model_id="model_1", priority=0, created_by="b")

    assert first_id == second_id  # 同一 (org_id, purpose, priority) UPSERT，不新增行
    rows = bindings.list_bindings("text:screenplay")
    assert len(rows) == 1
    assert rows[0]["id"] == first_id


def test_bindings_org_id_sentinel_is_empty_string_not_null() -> None:
    """org_id 用空串占位而不是 SQL NULL，UNIQUE 约束才对全局绑定真正生效。"""
    bindings.upsert_binding(purpose="text:x", model_id="model_1", priority=0)
    row = bindings.get_priority_zero("text:x")
    assert row is not None
    assert row["org_id"] == ""


def test_known_purposes_derived_from_catalog_kinds_not_hardcoded() -> None:
    """purpose 目录从模型库活数据推导：目录只有一个 kind 时,已知 purpose 只有
    那一个——不写死 text/vlm/video/image 四个字面量。"""
    _set_catalog([_custom_item(1, "text")])
    assert purposes.required_kind_purposes() == {"text:default"}

    # 有人为一个从未出现在目录里的阶段建了条覆盖绑定，它也自动进目录。
    bindings.upsert_binding(purpose="text:screenplay", model_id="model_1", priority=0)
    assert "text:screenplay" in purposes.known_purposes()


def test_missing_priority_zero_purposes_reports_unconfigured_kind() -> None:
    _set_catalog([_custom_item(1, "text"), _custom_item(3, "video")])
    assert set(purposes.missing_priority_zero_purposes()) == {"text:default", "video:default"}

    bindings.upsert_binding(purpose="text:default", model_id="model_1", priority=0)
    assert purposes.missing_priority_zero_purposes() == ["video:default"]


def test_startup_self_check_empty_catalog_reports_nothing_missing() -> None:
    """模型库为空的新部署：没有已知 purpose，自检不误报缺口（EP-05 §11 陷阱 6）。"""
    _set_catalog([])
    assert purposes.startup_self_check() == []
