"""人物造型照 REST 入口（``app.video_modes.character_looks_api``）结构性回归。

只覆盖路由层自身的职责（404/202/只读 GET 形状）；「POST 是否真的补齐了造型照」
由 ``tests/test_character_looks_ensure.py`` 直接测 ``ensure_character_looks`` 本身
——``asyncio.create_task`` 调度的后台任务在 TestClient 同步请求周期内不保证跑完，
用 API 层断言生成结果会是不稳定的假信号。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.db import get_conn
from app.main import app
from tests.conftest import SessionTestClient


@pytest.fixture
def client():
    return SessionTestClient(TestClient(app))


def _seed_episode(*, project_id="proj_api_1", episode_id="ep_api_1") -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, bible_json, created_at) VALUES(?,?, 'created', ?, 1)",
        (project_id, "测试项目", json.dumps({"world": {"visual_style_canonical": "国风"}, "characters": []})),
    )
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, status, created_at) VALUES(?,?,?,?,?)",
        (episode_id, project_id, 1, "created", 1.0),
    )
    conn.commit()


def test_get_character_looks_404_for_unknown_episode(client):
    r = client.get("/api/episodes/does-not-exist/character-looks")
    assert r.status_code == 404


def test_post_character_looks_404_for_unknown_episode(client):
    r = client.post("/api/episodes/does-not-exist/character-looks")
    assert r.status_code == 404


def test_get_character_looks_empty_episode_returns_zero_summary(client):
    _seed_episode()
    r = client.get("/api/episodes/ep_api_1/character-looks")
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["summary"] == {"ready": 0, "generating": 0, "failed": 0, "missing": 0}


def test_post_character_looks_accepted(client):
    _seed_episode(project_id="proj_api_2", episode_id="ep_api_2")
    r = client.post("/api/episodes/ep_api_2/character-looks")
    assert r.status_code == 202
    assert r.json()["status"] == "accepted"


def test_post_character_looks_accepts_optional_shot_ids(client):
    _seed_episode(project_id="proj_api_3", episode_id="ep_api_3")
    r = client.post("/api/episodes/ep_api_3/character-looks", json={"shot_ids": ["shot_1"]})
    assert r.status_code == 202
