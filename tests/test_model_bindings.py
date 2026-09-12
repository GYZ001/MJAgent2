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
from app.models_registry.binding_migration import (
    _resolve_moderation_fallback_route_item,
    migrate_legacy_bindings,
)


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


def test_moderation_fallback_route_whole_string_is_provider_not_split_on_colon() -> None:
    """生产真实形状：``custom:model_x`` 整串本身就是 provider（含冒号），裸
    ``partition(":")`` 会把它劈成 "custom"+"model_x" 两个不存在的值——判据
    必须先把整串当 provider 查目录，查到就是它。"""
    _set_catalog([_custom_item(1, "text")])
    set_setting("text_moderation_fallback_route", "custom:model_1")

    item = _resolve_moderation_fallback_route_item(lambda k: get_setting(k))

    assert item is not None
    assert item["id"] == "model_1"


def test_moderation_fallback_route_falls_back_to_two_segment_form_for_builtin_providers() -> None:
    """内置协议家族的 provider 字面量不带冒号（如 "myprov"），历史两段式
    "provider:model" 格式在这种形状下仍然有效——但要求 model 字段逐字匹配，
    不是只要 provider 存在就认。"""
    _set_catalog([{
        "id": "model_builtin", "provider": "myprov", "model": "mymodel",
        "kinds": ["text"], "builtin": False, "protocol": "openai",
        "base_url": "https://builtin.example.test/v1",
    }])
    set_setting("text_moderation_fallback_route", "myprov:mymodel")

    item = _resolve_moderation_fallback_route_item(lambda k: get_setting(k))

    assert item is not None
    assert item["id"] == "model_builtin"


def test_moderation_fallback_route_unresolvable_value_returns_none_and_setting_untouched() -> None:
    """整串查不到、两段式也对不上：不猜、不清空——迁移失败必须可见，不能悄悄
    丢掉运维配置过的值。"""
    _set_catalog([_custom_item(1, "text")])
    set_setting("text_moderation_fallback_route", "custom:does-not-exist")

    assert _resolve_moderation_fallback_route_item(lambda k: get_setting(k)) is None

    result = migrate_legacy_bindings(force=True)
    assert "text_fallback" not in result["migrated"]
    assert get_setting("text_moderation_fallback_route") == "custom:does-not-exist"


def test_moderation_fallback_route_migrates_to_priority_one_even_when_flag_already_done() -> None:
    """生产真实场景（本次协调方指出的漏洞）：B 上 4 键迁移早已跑过、整体
    MIGRATION_FLAG 已是 "done"，随后才补的 text_moderation_fallback_route
    迁移必须仍然生效——不能因为绑定同一个整体闸门而被静默跳过，否则一项
    正在缓解"供应商内容审核拒绝"的能力会在部署当晚静默消失。"""
    _set_catalog(_seven_item_catalog())
    _seed_credentials_realistic_shape()
    _set_legacy_provider_settings()

    migrate_legacy_bindings(force=True)  # 模拟"第二阶段代码已经在 B 上跑过"
    assert get_setting("models_registry_bindings_migrated_v1") == "done"
    assert len(bindings.list_bindings("text:default")) == 1  # 此刻只有 priority=0

    # 之后才发现：运维在旧机制下配过换路目的地，生产真实值形如
    # "custom:model_07030243d87e"——整串本身就是 provider。
    set_setting("text_moderation_fallback_route", "custom:model_5")

    result = migrate_legacy_bindings()  # 非 force：闸门已经是 done

    assert result["skipped"] is False
    assert result["migrated"] == {"text_fallback": "custom:model_5"}
    rows = {row["priority"]: row["model_id"] for row in bindings.list_bindings("text:default")}
    assert rows == {0: "model_1", 1: "model_5"}
    assert get_setting("text_moderation_fallback_route") == ""

    # 幂等：设置已清空，再跑一次是纯粹的 no-op，不产生第二条 priority=1。
    second = migrate_legacy_bindings()
    assert second == {"skipped": True, "reason": "already_migrated"}
    assert len(bindings.list_bindings("text:default")) == 2


async def test_moderation_fallback_route_migration_actually_reroutes_content_rejection(monkeypatch) -> None:
    """端到端：迁移后真的用 priority=1 候选换路成功，且落一条审计——不是
    只把行插进表里，选路真的会用它（CLAUDE.md「界面承诺必须与实际行为
    一致」，这里是"迁移承诺必须与实际选路一致"的同一原则）。"""
    from app import hiagent
    from app.harness import model_gateway
    from app.db import get_conn

    _set_catalog(_seven_item_catalog())
    _seed_credentials_realistic_shape()
    _set_legacy_provider_settings()  # text:default priority=0 -> custom:model_1
    set_setting("text_moderation_fallback_route", "custom:model_5")

    migrate_legacy_bindings(force=True)
    assert bindings.get_priority_zero("text:default")["model_id"] == "model_1"

    calls: list[tuple[list[dict[str, str]], dict]] = []

    async def fake_chat(messages, **kwargs):
        calls.append((messages, kwargs))
        if len(calls) == 1:
            raise hiagent.ProviderError(
                "供应商内容审核已明确拒绝本次请求",
                failure=hiagent.ProviderFailure.model_rejection(),
            )
        return "换路后正常产出"

    monkeypatch.setattr(model_gateway.hiagent, "chat", fake_chat)

    result = await model_gateway.chat([{"role": "user", "content": "写一段追杀情节"}])

    assert result == "换路后正常产出"
    assert len(calls) == 2
    assert calls[1][1]["provider"] == "custom:model_5"
    audit_rows = get_conn().execute(
        "SELECT * FROM operation_audit WHERE event='models_registry.route_failover'"
    ).fetchall()
    assert len(audit_rows) == 1
    assert audit_rows[0]["error_code"] == "content_rejected"


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
