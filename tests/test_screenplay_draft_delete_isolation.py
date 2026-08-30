"""钉死「三义 draft」边界：DELETE /screenplay/draft 只清会话草稿。

映射台的 "draft" 一词曾一词三义，容易让使用者与 Agent 混淆：

- 会话草稿：``screenplay_drafts`` 表，路由 ``/screenplay/draft``（GET/PUT/DELETE），
  只是页面自动保存的未发布编辑内容，不发布、不生成、不进入下游。
- 工作文档：Repair 环节的服务端 working Artifact，由
  ``episodes.working_screenplay_artifact_id`` 指向。
- 已发布剧本：``episodes.screenplay_json``。

本测试锁定 catalog.py 描述与 delete_screenplay_draft 实现的数据流转一致：
``DELETE /api/episodes/{episode_id}/screenplay/draft`` 只删除 ``screenplay_drafts``
中该分集的会话草稿记录，不触碰已发布剧本（``screenplay_json``）与工作文档指针
（``working_screenplay_artifact_id``）。
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app import api, db
from app.capabilities.loader import ensure_catalog_loaded
from app.capabilities.bus import reset_command_bus_for_tests, set_request_approval_token
from app.capabilities.policy import reset_approvals_for_tests
from app.capabilities.registry import get_registry
from app.local_session import APPROVAL_HEADER, ensure_session_secret, set_request_session_id
from tests.conftest import SessionTestClient
from tests.test_screenplay_edit_save import _seed_episode, _valid_script


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "screenplay-draft-delete.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    ensure_catalog_loaded()
    reset_approvals_for_tests()
    reset_command_bus_for_tests()

    test_app = FastAPI()

    @test_app.middleware("http")
    async def inject_approval_token(request: Request, call_next):
        set_request_approval_token(request.headers.get(APPROVAL_HEADER))
        set_request_session_id(ensure_session_secret())
        try:
            return await call_next(request)
        finally:
            set_request_approval_token(None)
            set_request_session_id(None)

    test_app.include_router(api.router)
    with TestClient(test_app) as test_client:
        yield SessionTestClient(test_client)


def _episode_row() -> dict:
    return dict(
        db.get_conn()
        .execute("SELECT * FROM episodes WHERE id='e1'")
        .fetchone()
    )


def _stamp_published_and_working_pointers() -> None:
    """构造一份已发布 screenplay_json 与一个 working 工作文档指针。"""
    conn = db.get_conn()
    published = _valid_script().model_dump(mode="json")
    conn.execute(
        """UPDATE episodes SET
               screenplay_json=?,
               screenplay_status='ready',
               screenplay_artifact_id='art_sp_published',
               published_screenplay_artifact_id='art_sp_published',
               working_screenplay_artifact_id='art_sp_working'
           WHERE id='e1'""",
        (json.dumps(published, ensure_ascii=False),),
    )
    conn.commit()


def test_delete_draft_only_clears_session_draft_row(client) -> None:
    """PUT 一条会话草稿 + 构造已发布/工作指针，DELETE draft 后只清会话草稿。"""
    _seed_episode(with_artifact=True)
    _stamp_published_and_working_pointers()

    # 页面自动保存一条会话草稿到 screenplay_drafts。
    draft_content = _valid_script().model_dump(mode="json")
    saved = client.put(
        "/api/episodes/e1/screenplay/draft",
        json={"content": draft_content, "baseline_artifact_id": "art_sp_published"},
    )
    assert saved.status_code == 200, saved.text

    conn = db.get_conn()
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM screenplay_drafts WHERE episode_id='e1'"
    ).fetchone()["c"] == 1

    before = _episode_row()
    assert before["screenplay_json"] is not None
    assert before["working_screenplay_artifact_id"] == "art_sp_working"
    assert before["published_screenplay_artifact_id"] == "art_sp_published"

    # 删除会话草稿。
    deleted = client.delete("/api/episodes/e1/screenplay/draft")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"deleted": True}

    # 1) 会话草稿记录被清空。
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM screenplay_drafts WHERE episode_id='e1'"
    ).fetchone()["c"] == 0
    assert client.get("/api/episodes/e1/screenplay/draft").json()["draft"] is None

    # 2) 已发布剧本与工作文档指针逐字不变。
    after = _episode_row()
    assert after["screenplay_json"] == before["screenplay_json"]
    assert after["screenplay_artifact_id"] == before["screenplay_artifact_id"]
    assert after["published_screenplay_artifact_id"] == before["published_screenplay_artifact_id"]
    assert after["working_screenplay_artifact_id"] == before["working_screenplay_artifact_id"]
    assert after["screenplay_status"] == before["screenplay_status"]


def test_delete_draft_is_idempotent_and_never_touches_pointers(client) -> None:
    """无会话草稿时 DELETE 返回 deleted=False，且不影响发布/工作指针。"""
    _seed_episode(with_artifact=True)
    _stamp_published_and_working_pointers()

    before = _episode_row()
    conn = db.get_conn()
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM screenplay_drafts WHERE episode_id='e1'"
    ).fetchone()["c"] == 0

    deleted = client.delete("/api/episodes/e1/screenplay/draft")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"deleted": False}

    after = _episode_row()
    assert after["screenplay_json"] == before["screenplay_json"]
    assert after["working_screenplay_artifact_id"] == before["working_screenplay_artifact_id"]
    assert after["published_screenplay_artifact_id"] == before["published_screenplay_artifact_id"]


def test_catalog_draft_delete_description_matches_implementation() -> None:
    """catalog.py 对 DELETE draft 的豁免描述必须与实现的数据流转一致。"""
    ensure_catalog_loaded()
    registry = get_registry()
    reason = registry.rest_exemptions[
        "DELETE /api/episodes/{episode_id}/screenplay/draft"
    ]
    # 描述锁定：只删会话草稿表，且明确点名不碰工作文档与已发布剧本。
    assert "screenplay_drafts" in reason
    assert "working_screenplay_artifact_id" in reason
    assert "screenplay_json" in reason
