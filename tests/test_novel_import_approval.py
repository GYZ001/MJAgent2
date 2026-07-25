from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app import api, db
from app.capabilities import attachments, ensure_catalog_loaded
from app.capabilities.bus import reset_command_bus_for_tests, set_request_approval_token
from app.capabilities.policy import reset_approvals_for_tests
from app.local_session import APPROVAL_HEADER


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "novel-import.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    ensure_catalog_loaded()
    attachments.reset_for_tests()
    reset_approvals_for_tests()
    reset_command_bus_for_tests()

    test_app = FastAPI()

    @test_app.middleware("http")
    async def inject_approval_token(request: Request, call_next):
        set_request_approval_token(request.headers.get(APPROVAL_HEADER))
        try:
            return await call_next(request)
        finally:
            set_request_approval_token(None)

    test_app.include_router(api.router)
    with TestClient(test_app) as test_client:
        yield test_client
    attachments.reset_for_tests()


def test_import_reuses_attachment_token_after_approval(client: TestClient) -> None:
    upload = client.post(
        "/api/attachments/novel",
        files={"file": ("story.txt", "第一章 开始\n这是正文。".encode("utf-8"), "text/plain")},
    )
    assert upload.status_code == 200
    command_args = {
        "attachment_token": upload.json()["attachment_token"],
        "name": "回归测试小说",
    }

    waiting = client.post("/api/projects/import", json=command_args)
    assert waiting.status_code == 202
    approval_token = waiting.json()["approval_token"]

    imported = client.post(
        "/api/projects/import",
        json=command_args,
        headers={"X-Manju-Approval-Token": approval_token},
    )
    assert imported.status_code == 200, imported.text
    payload = imported.json()
    assert payload["project_id"].startswith("proj_")
    assert payload["ingestion"]["chapter_count"] == 1
