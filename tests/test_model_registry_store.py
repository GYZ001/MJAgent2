"""app.models_registry.store / keyprovider 的落表与加密读写证明。

覆盖：
1. models 表落表（``upsert_model``/``get_model``/``list_models``），幂等 upsert。
2. model_credentials 表加密读写（``put_credential``/``get_credential``/
   ``has_credential``/``delete_credential``），库里不留明文。
3. 轮换原子性的"落盘"半边——``put_credential`` 本身是单条 UPSERT，一次调用要么
   整体成功要么整体不落盘；"先探活再落盘"的顺序由 ``app/system_api.py`` 负责，
   见 ``tests/test_model_catalog.py::test_model_credentials_are_saved_by_model_id``。
4. keyprovider：主密钥文件首次生成、落盘权限 0600、``MJ_MASTER_KEY_FILE`` 覆盖路径。
"""
from __future__ import annotations

import stat

from app.db import get_conn
from app.models_registry import store
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
