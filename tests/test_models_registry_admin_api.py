"""模型中心管理界面新增的只读聚合 + 用途绑定读写端点
（``/api/models/registry/*``，EP-05 §8）：健康度渲染所需字段齐全、凭据只回
掩码/指纹绝不含明文、"正在用备用模型顶着"这条横幅数据在主用模型熔断时真的
能从 ``GET /purposes`` 读出来。HTTP 层测试，不是纯单元测试——横幅信号要端到
端验证"数据库里发生了什么 -> 接口吐出了什么"，不能只测中间某个函数。

``models`` 影子表（``app.models_registry.store.upsert_model``）只在一次性
迁移时写入，本测试文件故意不调用它——``test_health_endpoint_reports_state_
and_24h_window`` 就是在验证"近 24h 调用统计"改走活目录（
``app.models_registry.health.calls_by_ref_in_window``）之后，即使影子表
一行都没有也能正确统计，见该函数模块文档。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import system_api as msys
from app.auth.passwords import hash_password
from app.auth.sessions import create_session
from app.db import get_conn, new_id, now, set_setting
from app.main import app
from app.models_registry import health, store


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def admin_headers() -> dict[str, str]:
    conn = get_conn()
    user_id = new_id("usr")
    conn.execute(
        "INSERT INTO users(id, username, password_hash, status, is_system_admin, "
        "must_change_password, created_at) VALUES(?,?,?,'active',1,0,?)",
        (user_id, "models-admin", hash_password("pw-root-000"), now()),
    )
    conn.commit()
    return {"X-Manju-Session": create_session(user_id)}


def _custom_item(idx: int, kind: str = "text") -> dict:
    return {
        "id": f"model_{idx}", "provider": f"custom:model_{idx}", "model": f"{kind}-model-{idx}",
        "label": f"测试模型{idx}", "kinds": [kind], "builtin": False,
        "protocol": "openai", "base_url": f"https://gw{idx}.example.test/v1",
        "provider_label": f"测试供应商{idx}",
    }


def _set_catalog(items: list[dict]) -> None:
    set_setting("custom_models", json.dumps(items, ensure_ascii=False))


def _insert_provider_call(model_ref: str, *, status: str, http_status: int | None, latency_ms: int) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO provider_calls(ts, kind, model, status, http_status, latency_ms) VALUES(?,?,?,?,?,?)",
        (now(), "text", model_ref, status, http_status, latency_ms),
    )
    conn.commit()


def test_endpoints_require_system_admin(client: TestClient) -> None:
    resp = client.get("/api/models/registry/health")
    assert resp.status_code == 401


def test_health_endpoint_reports_state_and_24h_window(client: TestClient, admin_headers) -> None:
    _set_catalog([_custom_item(1)])
    store.put_credential("model_1", base_url="https://gw1.example.test/v1", api_key="sk-fake-AAAA1111", rotated_by="test")
    _insert_provider_call("text-model-1", status="OK", http_status=200, latency_ms=80)
    _insert_provider_call("text-model-1", status="FAILED", http_status=500, latency_ms=120)

    resp = client.get("/api/models/registry/health", headers=admin_headers)
    assert resp.status_code == 200
    row = next(item for item in resp.json()["items"] if item["model_id"] == "model_1")
    assert row["state"] == "healthy"  # 只有 2 次调用，没到 _MIN_SAMPLES=5，不会误判熔断
    assert row["calls_window"] == 2
    assert row["failures_window"] == 1
    assert row["failure_rate_window"] == pytest.approx(0.5)
    assert row["p50_latency_ms_window"] == 120
    assert row["p95_latency_ms_window"] == 120


def test_credentials_endpoint_never_leaks_plaintext(client: TestClient, admin_headers) -> None:
    _set_catalog([_custom_item(1)])
    store.put_credential("model_1", base_url="https://gw1.example.test/v1", api_key="sk-fake-SECRET7777", rotated_by="test")

    resp = client.get("/api/models/registry/credentials", headers=admin_headers)
    assert resp.status_code == 200
    assert "sk-fake-SECRET7777" not in resp.text
    row = next(item for item in resp.json()["items"] if item["model_id"] == "model_1")
    assert row["masked_key"] and row["masked_key"] != "sk-fake-SECRET7777"
    assert row["key_fingerprint"]
    assert row["rotated_at"]


def test_purposes_endpoint_flags_missing_priority_zero(client: TestClient, admin_headers) -> None:
    _set_catalog([_custom_item(1)])
    resp = client.get("/api/models/registry/purposes", headers=admin_headers)
    assert resp.status_code == 200
    row = next(item for item in resp.json()["items"] if item["purpose"] == "text:default")
    assert row["missing_priority_zero"] is True
    assert row["fallback_active"] is False


def test_put_binding_creates_priority_zero_and_clears_missing_flag(client: TestClient, admin_headers) -> None:
    _set_catalog([_custom_item(1)])
    resp = client.put(
        "/api/models/registry/bindings", headers=admin_headers,
        json={"purpose": "text:default", "model_id": "model_1", "priority": 0},
    )
    assert resp.status_code == 200

    listed = client.get("/api/models/registry/purposes", headers=admin_headers)
    row = next(item for item in listed.json()["items"] if item["purpose"] == "text:default")
    assert row["missing_priority_zero"] is False
    assert row["bindings"][0]["model_id"] == "model_1"
    assert row["bindings"][0]["label"] == "测试模型1"


def test_fallback_banner_reflects_primary_circuit_open(client: TestClient, admin_headers) -> None:
    """核心场景（PRD §8 第 4 条）：主用 Key 过期 -> 连续 401 归类 server_error
    -> 熔断 -> 静默换路到备用 -> 界面必须能看出来。"""
    _set_catalog([_custom_item(1), _custom_item(2)])
    client.put("/api/models/registry/bindings", headers=admin_headers,
               json={"purpose": "text:default", "model_id": "model_1", "priority": 0})
    client.put("/api/models/registry/bindings", headers=admin_headers,
               json={"purpose": "text:default", "model_id": "model_2", "priority": 1})

    for _ in range(5):
        health.record_outcome("model_1", "server_error")
    assert health.effective_state("model_1") == "circuit_open"

    resp = client.get("/api/models/registry/purposes", headers=admin_headers)
    row = next(item for item in resp.json()["items"] if item["purpose"] == "text:default")
    assert row["fallback_active"] is True
    assert row["reason_code"] == "circuit_open"
    assert "server_error" in row["reason_label"] or "服务端错误" in row["reason_label"]
    assert row["active_model_id"] == "model_2"
    assert row["since"] is not None


def test_update_model_accepts_rate_limit_and_enabled_via_existing_endpoint() -> None:
    """限速/启停走既有 ``PUT /api/models/{id}``（``system_api.update_model``），
    不新开第二条写路径——直接调用同一个函数，跳过它外面那层做 token 能力探测
    的异步路由（``update_model_route``），避免测试触发真实网络请求。"""
    created = msys.add_model({
        "provider": "custom", "provider_label": "测试供应商9",
        "base_url": "https://gw9.example.test/v1", "api_key": "sk-fake-9",
        "protocol": "openai", "model": "text-model-9", "label": "测试模型9",
        "kinds": ["text"],
    })
    model_id = created["id"]

    updated = msys.update_model(model_id, {
        "rate_limit": {"rpm": 60, "tpm": 200000, "concurrency": 4},
        "enabled": False,
    })

    assert updated["rate_limit"] == {"rpm": 60, "tpm": 200000, "concurrency": 4}
    assert updated["enabled"] is False
    refreshed = next(m for m in msys.get_models()["items"] if m["id"] == model_id)
    assert refreshed["enabled"] is False
    assert refreshed["rate_limit"]["concurrency"] == 4
