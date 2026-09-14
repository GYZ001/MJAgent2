"""models.provider 空串回填（``app.models_registry.schema.ensure_tables_on_connection``）。

生产 B 2026-09-14 03:30 部署「模型库收敛为唯一真源」那轮后的真实故障：EP-05 第一阶段
落表的 7 条目录项在补 ``provider`` 列后全是空串，``routing.resolve()`` 解析出
``provider=''``，``hiagent.active_provider()`` 四个职责全部返回空串，任何生成调用都
报「模型  未配置 API Key」。这里证明建表兜底会把自建形态（``model_*`` id）的空
provider 回填成 ``custom:{id}``，不碰已有值，也不碰无法推导的共享网关家族条目。
"""
from __future__ import annotations

from app import hiagent
from app.db import get_conn
from app.models_registry import bindings, schema, store


def _insert_raw(model_id: str, provider: str, kinds: str = '["text"]') -> None:
    """绕过 upsert_model 直接落一行，模拟迁移期写入、补列后 provider 为空的老行。"""
    schema.ensure_schema()
    get_conn().execute(
        """INSERT INTO models (id, name, protocol, model_ref, kinds_json, base_url, enabled,
                               capabilities_json, rate_limit_json, created_at, updated_at, provider)
           VALUES (?, ?, 'openai', 'ref-' || ?, ?, 'https://gw.example.com/v1', 1, '{}', '{}', 0, 0, ?)""",
        (model_id, model_id, model_id, kinds, provider),
    )
    get_conn().commit()


def _provider_of(model_id: str) -> str:
    row = get_conn().execute("SELECT provider FROM models WHERE id=?", (model_id,)).fetchone()
    return str(row["provider"])


def test_empty_provider_on_custom_row_is_backfilled_to_custom_prefix() -> None:
    _insert_raw("model_4bdbf85ea40b", "")
    schema.ensure_tables_on_connection(get_conn())
    assert _provider_of("model_4bdbf85ea40b") == "custom:model_4bdbf85ea40b"


def test_existing_provider_value_is_left_untouched() -> None:
    _insert_raw("model_keep", "custom:model_keep")
    _insert_raw("model_family", "hiagent")
    schema.ensure_tables_on_connection(get_conn())
    assert _provider_of("model_keep") == "custom:model_keep"
    assert _provider_of("model_family") == "hiagent"


def test_builtin_row_without_provider_is_not_guessed() -> None:
    _insert_raw("builtin:hiagent:doubao", "")
    schema.ensure_tables_on_connection(get_conn())
    assert _provider_of("builtin:hiagent:doubao") == ""


def test_backfill_is_idempotent_across_repeated_startups() -> None:
    _insert_raw("model_twice", "")
    schema.ensure_tables_on_connection(get_conn())
    schema.ensure_tables_on_connection(get_conn())
    assert _provider_of("model_twice") == "custom:model_twice"


def test_active_provider_resolves_after_backfill() -> None:
    """生产症状本身：绑定指向的条目 provider 为空时 active_provider('text') 是空串。"""
    _insert_raw("model_bound", "", kinds='["text", "vlm"]')
    bindings.upsert_binding(purpose="text:default", model_id="model_bound", priority=0, created_by="test")
    before = hiagent.active_provider("text")
    schema.ensure_tables_on_connection(get_conn())
    get_conn().commit()
    after = hiagent.active_provider("text")
    assert before == ""
    assert after == "custom:model_bound"
    assert store.get_model("model_bound")["provider"] == "custom:model_bound"
