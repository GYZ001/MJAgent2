from __future__ import annotations

import sqlite3
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app import api, db, planning, task_registry
from app.capabilities import attachments
from app.capabilities.loader import ensure_catalog_loaded
from app.capabilities.bus import reset_command_bus_for_tests, set_request_approval_token
from app.capabilities.handlers import project as project_handler
from app.capabilities.inputs import ProjectImportNovelInput
from app.capabilities.policy import reset_approvals_for_tests
from app.local_session import APPROVAL_HEADER, ensure_session_secret, set_request_session_id
from tests.conftest import SessionTestClient, patch_api_everywhere


def _epub_bytes() -> bytes:
    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=ZIP_STORED)
        archive.writestr(
            "META-INF/container.xml",
            """<?xml version="1.0"?>
            <container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
              <rootfiles>
                <rootfile full-path="OEBPS/content.opf"
                          media-type="application/oebps-package+xml"/>
              </rootfiles>
            </container>""",
            compress_type=ZIP_DEFLATED,
        )
        archive.writestr(
            "OEBPS/content.opf",
            """<?xml version="1.0" encoding="UTF-8"?>
            <package xmlns="http://www.idpf.org/2007/opf" version="3.0">
              <manifest>
                <item id="chapter-1" href="chapter-1.xhtml" media-type="application/xhtml+xml"/>
                <item id="chapter-2" href="chapter-2.xhtml" media-type="application/xhtml+xml"/>
              </manifest>
              <spine>
                <itemref idref="chapter-1"/>
                <itemref idref="chapter-2"/>
              </spine>
            </package>""",
            compress_type=ZIP_DEFLATED,
        )
        # Deliberately store chapter 2 first: imports must follow the OPF spine.
        archive.writestr(
            "OEBPS/chapter-2.xhtml",
            """<html xmlns="http://www.w3.org/1999/xhtml"><head><title>网页标题</title></head>
            <body><h1>第二章 转折</h1><p>远处钟声打破了沉默。</p></body></html>""",
            compress_type=ZIP_DEFLATED,
        )
        archive.writestr(
            "OEBPS/chapter-1.xhtml",
            """<html xmlns="http://www.w3.org/1999/xhtml"><head><title>不应导入</title></head>
            <body><nav>目录噪声</nav><h1>第一章 开端</h1><p>少年在夜雨中推开院门。</p>
            <script>脚本噪声</script></body></html>""",
            compress_type=ZIP_DEFLATED,
        )
    return stream.getvalue()


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
        set_request_session_id(ensure_session_secret())
        try:
            return await call_next(request)
        finally:
            set_request_approval_token(None)
            set_request_session_id(None)

    test_app.include_router(api.router)
    with TestClient(test_app) as test_client:
        yield SessionTestClient(test_client)
    attachments.reset_for_tests()


def test_import_reuses_attachment_token_after_approval(
    client: TestClient, monkeypatch,
) -> None:
    started: dict[str, str] = {}

    async def fake_start_bible(project_id: str, feedback: str) -> dict:
        started["project_id"] = project_id
        started["feedback"] = feedback
        return {"status": "running", "run_id": "run_bootstrap"}

    async def fake_start_plan(project_id: str) -> dict:
        started["plan_project_id"] = project_id
        return {"status": "running", "planner": "regex", "rule": "one_chapter_one_episode"}

    patch_api_everywhere(monkeypatch, "_start_bible_core", fake_start_bible)
    monkeypatch.setattr(planning, "start_plan", fake_start_plan)
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
    assert payload["asset_generation"] == {
        "status": "running",
        "run_id": "run_bootstrap",
    }
    assert payload["episode_planning"] == {
        "status": "running",
        "planner": "regex",
        "rule": "one_chapter_one_episode",
    }
    assert started == {
        "plan_project_id": payload["project_id"],
        "project_id": payload["project_id"],
        "feedback": "",
    }

    replayed = client.post("/api/projects/import", json=command_args)
    assert replayed.status_code == 200
    assert replayed.json()["project_id"] == payload["project_id"]
    project_count = db.get_conn().execute("SELECT COUNT(*) AS c FROM projects").fetchone()["c"]
    assert project_count == 1


def test_legacy_multipart_import_reuses_upload_across_approval(
    client: TestClient, monkeypatch,
) -> None:
    async def fake_start_plan(project_id: str) -> dict:
        return {"status": "running", "task_id": f"plan:{project_id}"}

    async def fake_start_bible(project_id: str, _feedback: str) -> dict:
        return {"status": "running", "task_id": f"bible:{project_id}"}

    monkeypatch.setattr(planning, "start_plan", fake_start_plan)
    patch_api_everywhere(monkeypatch, "_start_bible_core", fake_start_bible)
    file_payload = {
        "file": ("legacy.txt", "第一章 兼容入口\n正文内容。".encode(), "text/plain")
    }
    waiting = client.post(
        "/api/projects",
        data={"name": "兼容入口"},
        files=file_payload,
    )
    assert waiting.status_code == 202

    imported = client.post(
        "/api/projects",
        data={"name": "兼容入口"},
        files=file_payload,
        headers={"X-Manju-Approval-Token": waiting.json()["approval_token"]},
    )

    assert imported.status_code == 200, imported.text
    assert imported.json()["project_id"].startswith("proj_")
    assert db.get_conn().execute("SELECT COUNT(*) AS c FROM projects").fetchone()["c"] == 1


def test_import_keeps_attachment_available_when_project_creation_fails(
    client: TestClient, monkeypatch,
) -> None:
    original_create = api._create_project_core

    async def fake_start_plan(project_id: str) -> dict:
        return {"status": "running", "task_id": f"plan:{project_id}"}

    async def fake_start_bible(project_id: str, _feedback: str) -> dict:
        return {"status": "running", "task_id": f"bible:{project_id}"}

    monkeypatch.setattr(planning, "start_plan", fake_start_plan)
    patch_api_everywhere(monkeypatch, "_start_bible_core", fake_start_bible)
    upload = client.post(
        "/api/attachments/novel",
        files={"file": ("retry.txt", "第一章 重试\n正文仍在。".encode(), "text/plain")},
    )
    args = {"attachment_token": upload.json()["attachment_token"], "name": "可重试导入"}
    waiting = client.post("/api/projects/import", json=args)

    def fail_create(*_args, **_kwargs):
        raise HTTPException(503, "数据库暂时不可用")

    patch_api_everywhere(monkeypatch, "_create_project_core", fail_create)
    failed = client.post(
        "/api/projects/import",
        json=args,
        headers={"X-Manju-Approval-Token": waiting.json()["approval_token"]},
    )
    assert failed.status_code == 503
    assert db.get_conn().execute("SELECT COUNT(*) AS c FROM projects").fetchone()["c"] == 0

    patch_api_everywhere(monkeypatch, "_create_project_core", original_create)
    retry_waiting = client.post("/api/projects/import", json=args)
    retried = client.post(
        "/api/projects/import",
        json=args,
        headers={"X-Manju-Approval-Token": retry_waiting.json()["approval_token"]},
    )
    assert retried.status_code == 200
    assert retried.json()["project_id"].startswith("proj_")


def test_import_reports_paid_asset_bootstrap_as_waiting_confirmation(
    client: TestClient, monkeypatch,
) -> None:
    async def fake_start_plan(project_id: str) -> dict:
        return {"status": "running", "task_id": f"plan:{project_id}"}

    async def require_payment(project_id: str, _feedback: str) -> dict:
        raise HTTPException(
            409,
            detail={
                "code": "PAYMENT_CONFIRM_REQUIRED",
                "message": "需要确认费用",
                "precheck": {"project_id": project_id, "estimated_cost_cny": 12.0},
            },
        )

    monkeypatch.setattr(planning, "start_plan", fake_start_plan)
    patch_api_everywhere(monkeypatch, "_start_bible_core", require_payment)
    upload = client.post(
        "/api/attachments/novel",
        files={"file": ("paid.txt", "第一章 开始\n这是正文。".encode(), "text/plain")},
    )
    args = {"attachment_token": upload.json()["attachment_token"], "name": "费用确认"}
    waiting = client.post("/api/projects/import", json=args)
    imported = client.post(
        "/api/projects/import",
        json=args,
        headers={"X-Manju-Approval-Token": waiting.json()["approval_token"]},
    )

    assert imported.status_code == 200
    asset = imported.json()["asset_generation"]
    assert asset["status"] == "awaiting_confirmation"
    assert asset["retryable"] is True
    assert asset["precheck"]["estimated_cost_cny"] == 12.0
    assert "等待费用确认" in imported.json()["summary"]


@pytest.mark.asyncio
async def test_import_receipt_recovers_project_after_attachment_store_is_lost(
    client: TestClient, monkeypatch,
) -> None:
    del client  # fixture provides the isolated production schema
    raw = "第一章 重启窗口\n项目已经提交，但响应尚未返回。".encode()
    token = attachments.store_upload("restart.txt", raw)
    token_hash = api._novel_import_token_hash(token)
    created = api._create_project_core(
        "重启恢复项目",
        "restart.txt",
        raw,
        import_token_hash=token_hash,
    )
    attachments.reset_for_tests()

    async def fake_start_plan(project_id: str) -> dict:
        return {"status": "running", "task_id": f"plan:{project_id}"}

    async def fake_start_bible(project_id: str, _feedback: str) -> dict:
        return {"status": "running", "task_id": f"bible:{project_id}"}

    monkeypatch.setattr(planning, "start_plan", fake_start_plan)
    patch_api_everywhere(monkeypatch, "_start_bible_core", fake_start_bible)

    result = await project_handler.import_novel(
        ProjectImportNovelInput(attachment_token=token, name="重启恢复项目")
    )

    assert result.status.value == "succeeded"
    assert result.data["project_id"] == created["project_id"]
    assert result.data["idempotent_replay"] is True
    assert db.get_conn().execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1


def test_upload_accepts_epub_and_imports_spine_in_order(client: TestClient) -> None:
    upload = client.post(
        "/api/attachments/novel",
        files={"file": ("长夜.epub", _epub_bytes(), "application/epub+zip")},
    )

    assert upload.status_code == 200, upload.text
    token = upload.json()["attachment_token"]
    filename, raw = attachments.read(token)
    try:
        created = api._create_project_core(None, filename, raw)
    finally:
        attachments.discard(token)

    assert created["ingestion"]["source_format"] == "EPUB"
    assert created["ingestion"]["chapter_count"] == 2
    project = db.get_conn().execute(
        "SELECT name FROM projects WHERE id=?",
        (created["project_id"],),
    ).fetchone()
    chapters = db.get_conn().execute(
        "SELECT title, content FROM chapters WHERE project_id=? ORDER BY idx",
        (created["project_id"],),
    ).fetchall()
    assert project["name"] == "长夜"
    assert [row["title"] for row in chapters] == ["第一章 开端", "第二章 转折"]
    assert "少年在夜雨中推开院门。" in chapters[0]["content"]
    assert "远处钟声打破了沉默。" in chapters[1]["content"]
    assert all("噪声" not in row["content"] for row in chapters)


def test_upload_rejects_invalid_epub_before_approval(client: TestClient) -> None:
    response = client.post(
        "/api/attachments/novel",
        files={"file": ("broken.epub", b"not a zip archive", "application/epub+zip")},
    )

    assert response.status_code == 422
    assert "EPUB 文件损坏" in response.json()["detail"]


def test_upload_rejects_unsupported_format_before_approval(client: TestClient) -> None:
    response = client.post(
        "/api/attachments/novel",
        files={"file": ("story.pdf", b"%PDF-1.7 binary", "application/pdf")},
    )

    assert response.status_code == 422
    assert "仅支持 TXT 或 EPUB" in response.json()["detail"]


def test_create_project_rolls_back_partial_rows(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE projects(
            id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT,
            novel_chars INTEGER, created_at REAL,
            -- 简化版 schema 要跟真表保持同步：建项目现在会显式写入归属团队
            -- （见 app/domain/projects.py:_creation_workspace_id），缺这列会让
            -- 本用例以 OperationalError 收场，掩盖它真正要验的回滚行为。
            workspace_id TEXT NOT NULL DEFAULT 'ws_default'
        );
        CREATE TABLE chapters(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL, idx INTEGER NOT NULL, title TEXT,
            content TEXT NOT NULL, char_count INTEGER,
            UNIQUE(project_id, idx)
        );
        """
    )
    duplicate_report = {
        "total_chars": 20,
        "removed_lines": 0,
        "chapter_count": 2,
        "deduplicated_stub_chapters": 0,
        "auto_split": False,
        "chapters": [
            {"idx": 1, "title": "第一章", "content": "正文一"},
            {"idx": 1, "title": "第二章", "content": "正文二"},
        ],
    }
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "ingest_novel", lambda _raw: duplicate_report)

    with pytest.raises(sqlite3.IntegrityError):
        api._create_project_core("事务测试", "story.txt", b"valid novel")

    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM chapters").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_plan_spawn_failure_is_retryable(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "plan-spawn.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES(?,?,?,?)",
        ("proj_plan_retry", "分集恢复", "ingested", db.now()),
    )
    conn.commit()

    def fail_spawn(_kind, _key, coro, **_kwargs):
        coro.close()
        raise RuntimeError("event loop unavailable")

    monkeypatch.setattr(task_registry, "spawn", fail_spawn)
    with pytest.raises(HTTPException) as exc_info:
        await planning.start_plan("proj_plan_retry")

    assert exc_info.value.status_code == 503
    row = conn.execute(
        "SELECT plan_status, plan_error FROM projects WHERE id='proj_plan_retry'"
    ).fetchone()
    assert row["plan_status"] == "failed"
    assert "可直接重试" in row["plan_error"]
