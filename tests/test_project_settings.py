"""项目级设置（改编强度档位/画幅/AI 标识）存取与 PUT 接口回归（2026-09-23 新增）。

覆盖：迁移后存量项目的默认值、创建入口写入（含非法画幅拒绝）、``app.project_settings``
纯函数契约（``resolve_*``/``canvas_size``/``update_project_settings``）、PUT
``/api/projects/{project_id}/settings`` 的部分更新/非法值/项目不存在三条路径。

每个测试都拿到独立的隔离数据库（``tests/conftest.py`` 的 ``_reset_capability_runtime``
autouse fixture 逐测试克隆模板库），因此这里不需要任何额外的数据库夹具。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.db import get_conn, now
from app.domain.projects import create as projects_create
from app.main import app
from app.project_settings import (
    ai_label_enabled,
    canvas_size,
    resolve_adaptation_mode,
    resolve_aspect_ratio,
    update_project_settings,
)
from tests.conftest import SessionTestClient


def _minimal_project(conn, project_id: str, name: str = "存量项目") -> None:
    """最小化 INSERT，只写三个无默认值的 NOT NULL 列——模拟「从未显式设置过
    改编强度/画幅/AI 标识」的存量项目，验证迁移列默认值真的生效。"""
    conn.execute(
        "INSERT INTO projects(id, name, created_at) VALUES(?,?,?)",
        (project_id, name, now()),
    )
    conn.commit()


def _admin_client() -> SessionTestClient:
    return SessionTestClient(TestClient(app))


# ---------------------------------------------------------------------------
# 迁移默认值 / 创建入口写入
# ---------------------------------------------------------------------------


def test_legacy_project_defaults_to_faithful_9x16_off():
    conn = get_conn()
    _minimal_project(conn, "proj_legacy_settings")
    assert resolve_adaptation_mode(conn, "proj_legacy_settings") == "faithful"
    assert resolve_aspect_ratio(conn, "proj_legacy_settings") == "9:16"
    assert ai_label_enabled(conn, "proj_legacy_settings") is False


def test_new_project_defaults_to_short_drama_9x16_off():
    created = projects_create._create_project_core(
        "新建项目测试", "story.txt", "测试正文，项目设置回归。".encode("utf-8"),
    )
    conn = get_conn()
    assert resolve_adaptation_mode(conn, created["project_id"]) == "short_drama"
    assert resolve_aspect_ratio(conn, created["project_id"]) == "9:16"
    assert ai_label_enabled(conn, created["project_id"]) is False


def test_new_project_accepts_requested_aspect_ratio():
    created = projects_create._create_project_core(
        "新建项目测试-16比9", "story2.txt", "测试正文二。".encode("utf-8"), aspect_ratio="16:9",
    )
    conn = get_conn()
    assert resolve_aspect_ratio(conn, created["project_id"]) == "16:9"
    # 传了值时不覆盖改编强度/AI 标识的新建默认。
    assert resolve_adaptation_mode(conn, created["project_id"]) == "short_drama"
    assert ai_label_enabled(conn, created["project_id"]) is False


def test_new_project_rejects_invalid_aspect_ratio_with_422():
    with pytest.raises(HTTPException) as exc_info:
        projects_create._create_project_core(
            "坏画幅", "story3.txt", "测试正文三。".encode("utf-8"), aspect_ratio="4:3",
        )
    assert exc_info.value.status_code == 422
    assert "画幅" in str(exc_info.value.detail)


# ---------------------------------------------------------------------------
# app.project_settings 纯函数契约
# ---------------------------------------------------------------------------


def test_canvas_size_mapping():
    assert canvas_size("9:16") == (1080, 1920)
    assert canvas_size("16:9") == (1920, 1080)
    with pytest.raises(ValueError):
        canvas_size("4:3")


def test_resolve_missing_project_raises_lookup_error():
    conn = get_conn()
    with pytest.raises(LookupError):
        resolve_adaptation_mode(conn, "proj_does_not_exist")
    with pytest.raises(LookupError):
        resolve_aspect_ratio(conn, "proj_does_not_exist")
    with pytest.raises(LookupError):
        ai_label_enabled(conn, "proj_does_not_exist")


def test_resolve_corrupted_value_raises_runtime_error_not_value_error():
    """数据损坏（库里的值不在合法集合内）不是用户输入冲突，必须是 RuntimeError——
    全局 ValueError 已统一转 409，混用会把「数据损坏」误判成「客户端可重试」。"""
    conn = get_conn()
    _minimal_project(conn, "proj_corrupt_settings")
    conn.execute("UPDATE projects SET adaptation_mode='bogus' WHERE id=?", ("proj_corrupt_settings",))
    conn.commit()
    with pytest.raises(RuntimeError):
        resolve_adaptation_mode(conn, "proj_corrupt_settings")


def test_update_project_settings_partial_update_only_touches_given_fields():
    conn = get_conn()
    _minimal_project(conn, "proj_update_settings")
    result = update_project_settings(
        conn, "proj_update_settings",
        adaptation_mode="short_drama", aspect_ratio=None, ai_label_enabled=None,
    )
    conn.commit()
    assert result == {"adaptation_mode": "short_drama", "aspect_ratio": "9:16", "ai_label_enabled": False}
    assert resolve_aspect_ratio(conn, "proj_update_settings") == "9:16"


def test_update_project_settings_invalid_value_raises_value_error_chinese_message():
    conn = get_conn()
    _minimal_project(conn, "proj_update_bad")
    with pytest.raises(ValueError, match="不支持的画幅"):
        update_project_settings(
            conn, "proj_update_bad", adaptation_mode=None, aspect_ratio="4:3", ai_label_enabled=None,
        )


def test_update_project_settings_missing_project_raises_lookup_error():
    conn = get_conn()
    with pytest.raises(LookupError):
        update_project_settings(
            conn, "proj_does_not_exist_2",
            adaptation_mode="short_drama", aspect_ratio=None, ai_label_enabled=None,
        )


# ---------------------------------------------------------------------------
# PUT /api/projects/{project_id}/settings
# ---------------------------------------------------------------------------


def test_put_settings_partial_update_via_http():
    conn = get_conn()
    _minimal_project(conn, "proj_http_settings")
    client = _admin_client()
    resp = client.put("/api/projects/proj_http_settings/settings", json={"ai_label_enabled": True})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ai_label_enabled"] is True
    assert body["adaptation_mode"] == "faithful"  # 未传，保持迁移默认值
    assert body["aspect_ratio"] == "9:16"


def test_put_settings_invalid_value_is_409_with_chinese_message():
    conn = get_conn()
    _minimal_project(conn, "proj_http_bad")
    client = _admin_client()
    resp = client.put("/api/projects/proj_http_bad/settings", json={"aspect_ratio": "4:3"})
    assert resp.status_code == 409, resp.text
    assert "画幅" in resp.text


def test_put_settings_missing_project_is_404():
    client = _admin_client()
    resp = client.put("/api/projects/proj_does_not_exist_http/settings", json={"aspect_ratio": "16:9"})
    assert resp.status_code == 404, resp.text


def test_project_detail_exposes_settings_fields_for_frontend_panel():
    """GET /api/projects/{id}（app.domain.projects.detail.project_detail）按原始行
    透出（``p = dict(_project_or_404(project_id))`` 后只增删个别 key，从不收窄成
    白名单），三个项目设置字段本就在返回体里——前端设置面板从这里读，锁住这条契约。
    """
    conn = get_conn()
    _minimal_project(conn, "proj_detail_settings")
    update_project_settings(
        conn, "proj_detail_settings",
        adaptation_mode="short_drama", aspect_ratio="16:9", ai_label_enabled=True,
    )
    conn.commit()
    client = _admin_client()
    resp = client.get("/api/projects/proj_detail_settings")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["adaptation_mode"] == "short_drama"
    assert body["aspect_ratio"] == "16:9"
    assert body["ai_label_enabled"] == 1
