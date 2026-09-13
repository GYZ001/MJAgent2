"""app.models_registry.store / keyprovider 的落表与加密读写证明。

覆盖：
1. models 表落表（``upsert_model``/``get_model``/``list_models``/``delete_model``），
   幂等 upsert；``provider``（自建 ``custom:{id}`` 或共享网关家族字面量）与
   ``extra_json``（``params``/``requires_api_key``）两个字段的往返（2026-09-13
   模型库收敛成唯一真源那轮新增）。
2. model_credentials 表加密读写（``put_credential``/``get_credential``/
   ``has_credential``/``delete_credential``），库里不留明文。
3. 轮换原子性的"落盘"半边——``put_credential`` 本身是单条 UPSERT，一次调用要么
   整体成功要么整体不落盘；"先探活再落盘"的顺序由 ``app/system_api.py`` 负责，
   见 ``tests/test_model_catalog.py::test_model_credentials_are_saved_by_model_id``。
4. keyprovider：主密钥文件首次生成、落盘权限 0600、``MJ_MASTER_KEY_FILE`` 覆盖路径。
5. 硬指标——选路零变化：全新安装（经 ``system_api.add_model`` 写入）与 B 的真实
   形状（7 条目录项、4 条 custom:*、3 条共用同一把 Key，直接经 ``upsert_model``/
   ``put_credential`` 落表，模拟"已迁移"状态）两种路径下，四个用途的
   provider/model_ref/base_url/api_key 必须逐字段相同。
"""
from __future__ import annotations

import stat

from app.db import get_conn
from app.models_registry import bindings, routing, store
from app.models_registry.keyprovider import FileKeyProvider


def _sample_item(model_id: str, **overrides) -> dict:
    item = {
        "id": model_id, "provider": "custom:" + model_id, "provider_label": "网关",
        "model": "gpt-x", "label": "示例模型", "kinds": ["text", "vlm"],
        "protocol": "openai", "base_url": "https://gw.example.com/v1",
        "context_window_tokens": 131072, "max_output_tokens": 32768,
        "token_limits_source": "provider_metadata",
    }
    item.update(overrides)
    return item


# ---------------------------------------------------------------------------
# models 表
# ---------------------------------------------------------------------------

def test_upsert_model_then_get_model_roundtrips_fields() -> None:
    store.upsert_model(_sample_item("mdl_a1"), created_by="tester")
    got = store.get_model("mdl_a1")
    assert got is not None
    assert got["name"] == "示例模型"
    assert got["model_ref"] == "gpt-x"
    assert got["protocol"] == "openai"
    assert got["base_url"] == "https://gw.example.com/v1"
    assert got["kinds"] == ["text", "vlm"]
    assert got["capabilities"]["context_window_tokens"] == 131072
    assert got["enabled"] == 1


def test_upsert_model_is_idempotent_no_duplicate_rows() -> None:
    store.upsert_model(_sample_item("mdl_dup"), created_by="tester")
    store.upsert_model(_sample_item("mdl_dup", label="改名后"), created_by="tester")
    rows = get_conn().execute("SELECT COUNT(*) AS c FROM models WHERE id=?", ("mdl_dup",)).fetchone()
    assert rows["c"] == 1
    assert store.get_model("mdl_dup")["name"] == "改名后"


def test_get_model_missing_returns_none() -> None:
    assert store.get_model("mdl_does_not_exist") is None


def test_list_models_includes_all_upserted() -> None:
    store.upsert_model(_sample_item("mdl_list_1"), created_by="tester")
    store.upsert_model(_sample_item("mdl_list_2"), created_by="tester")
    ids = {m["id"] for m in store.list_models()}
    assert {"mdl_list_1", "mdl_list_2"} <= ids


def test_upsert_model_requires_id() -> None:
    import pytest

    with pytest.raises(ValueError):
        store.upsert_model({"label": "无 id"})


def test_upsert_model_roundtrips_provider_column() -> None:
    """``provider`` 是独立列，不是从 id 派生的——自建条目是 ``custom:{id}``，
    共享网关家族条目是字面量（``hiagent``/``bailian``/...），两种形状都必须
    原样存取。"""
    store.upsert_model(_sample_item("mdl_provider_custom", provider="custom:mdl_provider_custom"), created_by="tester")
    store.upsert_model(_sample_item("mdl_provider_family", provider="hiagent"), created_by="tester")

    assert store.get_model("mdl_provider_custom")["provider"] == "custom:mdl_provider_custom"
    assert store.get_model("mdl_provider_family")["provider"] == "hiagent"


def test_upsert_model_roundtrips_params_and_requires_api_key_via_extra() -> None:
    """``params``（供媒体协议读取的额外请求参数）与 ``requires_api_key`` 没有
    专属列，落进 ``extra_json``；只存条目里实际出现的键，不伪造默认值。"""
    store.upsert_model(_sample_item(
        "mdl_extra", params={"vae": "v2"}, requires_api_key=False,
    ), created_by="tester")

    row = store.get_model("mdl_extra")
    assert row["extra"] == {"params": {"vae": "v2"}, "requires_api_key": False}


def test_delete_model_removes_row() -> None:
    store.upsert_model(_sample_item("mdl_to_delete"), created_by="tester")
    assert store.get_model("mdl_to_delete") is not None

    store.delete_model("mdl_to_delete")

    assert store.get_model("mdl_to_delete") is None


def test_delete_model_missing_id_is_a_no_op() -> None:
    store.delete_model("mdl_never_existed")  # 不抛错，纯粹的"本来就没有"


# ---------------------------------------------------------------------------
# model_credentials 表
# ---------------------------------------------------------------------------

def test_put_credential_then_get_credential_roundtrips() -> None:
    result = store.put_credential(
        "mdl_cred_1", base_url="https://gw.example.com/v1", api_key="sk-abcdef1234",
        rotated_by="tester",
    )
    assert "api_key" not in result
    assert result["masked_key"] != "sk-abcdef1234"
    assert len(result["key_fingerprint"]) == 12

    saved = store.get_credential("mdl_cred_1")
    assert saved == {"base_url": "https://gw.example.com/v1", "api_key": "sk-abcdef1234"}


def test_get_credential_missing_returns_empty_dict() -> None:
    assert store.get_credential("mdl_never_configured") == {}


def test_has_credential_reflects_presence() -> None:
    assert store.has_credential("mdl_cred_2") is False
    store.put_credential("mdl_cred_2", base_url="", api_key="sk-x", rotated_by="tester")
    assert store.has_credential("mdl_cred_2") is True


def test_put_credential_overwrite_replaces_not_duplicates() -> None:
    store.put_credential("mdl_cred_3", base_url="https://a.example.com/v1", api_key="sk-old", rotated_by="t1")
    store.put_credential("mdl_cred_3", base_url="https://b.example.com/v1", api_key="sk-new", rotated_by="t2")
    rows = get_conn().execute(
        "SELECT COUNT(*) AS c FROM model_credentials WHERE model_id=?", ("mdl_cred_3",)
    ).fetchone()
    assert rows["c"] == 1
    saved = store.get_credential("mdl_cred_3")
    assert saved["api_key"] == "sk-new"
    assert saved["base_url"] == "https://b.example.com/v1"


def test_delete_credential_removes_row() -> None:
    store.put_credential("mdl_cred_del", base_url="", api_key="sk-del", rotated_by="tester")
    store.delete_credential("mdl_cred_del")
    assert store.get_credential("mdl_cred_del") == {}


def test_credentials_table_never_holds_plaintext_key() -> None:
    """库里 key_ciphertext 无明文——直接对原始密文列做子串匹配，零命中。"""
    secret = "sk-plaintext-should-never-appear-in-db"
    store.put_credential("mdl_no_plaintext", base_url="https://gw.example.com/v1", api_key=secret, rotated_by="tester")
    row = get_conn().execute(
        "SELECT key_ciphertext, base_url FROM model_credentials WHERE model_id=?",
        ("mdl_no_plaintext",),
    ).fetchone()
    assert secret.encode("utf-8") not in bytes(row["key_ciphertext"])


def test_put_credential_rejects_empty_api_key() -> None:
    import pytest

    with pytest.raises(ValueError):
        store.put_credential("mdl_empty", base_url="", api_key="", rotated_by="tester")


# ---------------------------------------------------------------------------
# keyprovider：主密钥文件
# ---------------------------------------------------------------------------

def test_file_key_provider_generates_32_byte_key_with_0600(tmp_path, monkeypatch) -> None:
    key_path = tmp_path / "master.key"
    monkeypatch.setenv("MJ_MASTER_KEY_FILE", str(key_path))
    provider = FileKeyProvider()

    key = provider.get_key()

    assert len(key) == 32
    assert key_path.exists()
    mode = stat.S_IMODE(key_path.stat().st_mode)
    assert mode == 0o600


def test_file_key_provider_reuses_existing_key_across_instances(tmp_path, monkeypatch) -> None:
    key_path = tmp_path / "master.key"
    monkeypatch.setenv("MJ_MASTER_KEY_FILE", str(key_path))

    first = FileKeyProvider().get_key()
    second = FileKeyProvider().get_key()

    assert first == second


def test_file_key_provider_rejects_wrong_length_key_file(tmp_path, monkeypatch) -> None:
    import pytest

    key_path = tmp_path / "master.key"
    key_path.write_bytes(b"too-short")
    monkeypatch.setenv("MJ_MASTER_KEY_FILE", str(key_path))

    with pytest.raises(ValueError):
        FileKeyProvider().get_key()


# ---------------------------------------------------------------------------
# 硬指标：选路零变化——全新安装 vs B 的真实形状
#
# "迁移前后逐字段相同"对一次内部存储重构而言，没有意义相同的"before"可跑
# （改动前 settings.custom_models 是唯一来源，改动后 models 表是唯一来源，
# 二者不可能同时跑在同一进程里逐行 diff）；能做且确实做的独立观察点是：
# 两种真实起点——B（``migrate_model_credentials`` 已经把 custom_models
# 镜像进 models 表之后的形状）与全新安装（只经 ``system_api.add_model``
# 写入，never 碰过 custom_models）——对同一份逻辑配置必须解出同一份连接
# 信息（base_url/api_key/model_ref），这正是下游代码唯一关心的字段。
# ---------------------------------------------------------------------------

_PURPOSE_ITEMS = {
    "text:default": {
        "id": "sot_text", "provider": "custom:sot_text", "provider_label": "网关A",
        "model": "text-model-sot", "label": "文本", "kinds": ["text"], "protocol": "openai",
        "base_url": "https://gw-text.example.test/v1", "api_key": "sk-shared-KEY0001",
    },
    "vlm:default": {
        "id": "sot_vlm", "provider": "custom:sot_vlm", "provider_label": "网关B",
        "model": "vlm-model-sot", "label": "视觉", "kinds": ["vlm"], "protocol": "openai",
        "base_url": "https://gw-vlm.example.test/v1", "api_key": "sk-shared-KEY0001",
    },
    "video:default": {
        "id": "sot_video", "provider": "custom:sot_video", "provider_label": "网关C",
        "model": "video-model-sot", "label": "视频", "kinds": ["video"], "protocol": "seedance",
        "base_url": "https://gw-video.example.test/v1", "api_key": "sk-distinct-0002",
    },
    "image:default": {
        "id": "sot_image", "provider": "custom:sot_image", "provider_label": "网关D",
        "model": "image-model-sot", "label": "图像", "kinds": ["image"], "protocol": "seedream",
        "base_url": "https://gw-image.example.test/v1", "api_key": "sk-shared-KEY0001",
    },
}

# 3 条噪音条目：共享网关家族字面量 provider（不是 custom:*），B 上真实存在的
# "同 kind 配了不止一家"常态；不参与任何用途绑定，只验证它们的存在不干扰选路。
_NOISE_FAMILY_ITEMS = [
    {"id": "sot_noise_1", "provider": "hiagent", "model": "hia-text-2", "label": "备用1",
     "kinds": ["text"], "protocol": "openai", "base_url": "https://hia.example.test/v1"},
    {"id": "sot_noise_2", "provider": "bailian", "model": "qwen-x", "label": "备用2",
     "kinds": ["text", "vlm"], "protocol": "openai", "base_url": "https://bailian.example.test/v1"},
    {"id": "sot_noise_3", "provider": "openrouter", "model": "glm-x", "label": "备用3",
     "kinds": ["text"], "protocol": "openrouter", "base_url": "https://openrouter.example.test/v1"},
]


def test_b_shaped_migrated_catalog_resolves_all_four_purposes() -> None:
    """B 的真实形状：7 条目录项（4 条撑起四个用途的 custom:*，3 条是共享网关
    家族字面量 provider 的噪音条目）、3 条共用同一把 Key——直接经
    ``upsert_model``/``put_credential`` 落表，模拟"已迁移"状态（不经
    settings.custom_models，那条 setting 已退场，见 store.py 模块文档）。"""
    for purpose, item in _PURPOSE_ITEMS.items():
        entry = {k: v for k, v in item.items() if k != "api_key"}
        store.upsert_model(entry, created_by="migration")
        store.put_credential(item["id"], base_url=item["base_url"], api_key=item["api_key"], rotated_by="migration")
        bindings.upsert_binding(purpose=purpose, model_id=item["id"], priority=0, created_by="migration")
    for noise in _NOISE_FAMILY_ITEMS:
        store.upsert_model(noise, created_by="migration")

    for purpose, expected in _PURPOSE_ITEMS.items():
        resolved = routing.resolve(purpose)
        assert resolved is not None, purpose
        assert resolved.provider == expected["provider"]
        assert resolved.model_ref == expected["model"]
        assert resolved.base_url == expected["base_url"]
        assert resolved.api_key == expected["api_key"]


def test_fresh_install_add_model_resolves_identically_to_b_shape() -> None:
    """全新安装：同样的四个用途改经 ``app.system_api.add_model``（真实 HTTP
    写入口背后的领域函数）逐条创建，从空表开始、从未写过 custom_models——
    连接信息（base_url/api_key/model_ref）必须与"已迁移" B 形状逐字段相同。
    provider 字符串本身不比较字面量相等：``add_model`` 生成的是
    ``custom:{new_id}``，new_id 每次运行都不同，这是"全新安装"与"已迁移
    存量"两种起点的必然差异，不是选路行为的差异——下游代码从不比较 provider
    字符串是否等于某个历史值，只用它做换路排除与限速分桶。
    """
    from app import system_api

    for purpose, item in _PURPOSE_ITEMS.items():
        created = system_api.add_model({
            "provider": "custom", "provider_label": item["provider_label"],
            "base_url": item["base_url"], "api_key": item["api_key"],
            "protocol": item["protocol"], "model": item["model"],
            "label": item["label"], "kinds": item["kinds"],
        })
        bindings.upsert_binding(purpose=purpose, model_id=created["id"], priority=0, created_by="test")

    for purpose, expected in _PURPOSE_ITEMS.items():
        resolved = routing.resolve(purpose)
        assert resolved is not None, purpose
        assert resolved.provider.startswith("custom:")
        assert resolved.model_ref == expected["model"]
        assert resolved.base_url == expected["base_url"]
        assert resolved.api_key == expected["api_key"]
