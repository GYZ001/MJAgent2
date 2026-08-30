from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable
from typing import Any

from app import api
from tests.conftest import patch_api_everywhere


class RecordingRecorder:
    def __init__(self) -> None:
        self.context_manifest: dict[str, Any] | None = None
        self.outcome: str | None = None
        self.run_id: str = "run_test"

    def start(self) -> None:
        pass

    async def step(
        self,
        _step_key: str,
        operation: Callable[[], Awaitable[Any]],
        **kwargs: Any,
    ) -> tuple[str, Any]:
        self.context_manifest = kwargs["context_manifest"]
        return "step_test", await operation()

    def succeed(self, _message: str, conn=None) -> None:
        self.outcome = "succeeded"

    def partial(self, _message: str, conn=None) -> None:
        self.outcome = "partial"

    def fail(self, _exc: BaseException, conn=None) -> None:
        self.outcome = "failed"

    def cancel(self, conn=None) -> None:
        self.outcome = "cancelled"


def test_recorded_bible_task_builds_bounded_context(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects(id TEXT PRIMARY KEY, bible_status TEXT, bible_error TEXT)"
    )
    conn.execute("CREATE TABLE chapters(project_id TEXT, idx INTEGER, title TEXT, content TEXT)")
    conn.execute("INSERT INTO projects VALUES('p1', 'running', NULL)")
    conn.execute("INSERT INTO chapters VALUES('p1', 1, 'chapter', ?)", ("x" * 60001,))
    conn.commit()

    async def fake_bible_task(*_args: Any, **_kwargs: Any) -> None:
        conn.execute("UPDATE projects SET bible_status='ready' WHERE id='p1'")
        conn.commit()

    recorder = RecordingRecorder()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "_bible_task", fake_bible_task)

    asyncio.run(
        api._recorded_bible_task(
            "p1", "", recorder, trigger_full_refs=False  # type: ignore[arg-type]
        )
    )

    assert recorder.outcome == "succeeded"
    assert recorder.context_manifest is not None
    item = recorder.context_manifest["items"][0]
    assert item["key"] == "chapters"
    assert item["selected_chars"] == 60000
    assert item["truncated"] is True


def test_recorded_storyboard_task_builds_bounded_context(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, bible_artifact_id TEXT)")
    conn.execute(
        "CREATE TABLE episodes("
        "id TEXT PRIMARY KEY, project_id TEXT, screenplay_json TEXT, "
        "screenplay_artifact_id TEXT, status TEXT, script_error TEXT, "
        "storyboard_artifact_id TEXT)"
    )
    conn.execute("INSERT INTO projects VALUES('p1', 'art_bible')")
    conn.execute(
        "INSERT INTO episodes VALUES('e1', 'p1', ?, 'art_screenplay', "
        "'scripting', NULL, NULL)",
        ("x" * 24001,),
    )
    conn.commit()

    async def fake_storyboard_task(*_args: Any, **_kwargs: Any) -> None:
        conn.execute(
            "UPDATE episodes SET status='scripted', storyboard_artifact_id='art_board' "
            "WHERE id='e1'"
        )
        conn.commit()

    recorder = RecordingRecorder()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "_storyboard_task", fake_storyboard_task)

    asyncio.run(
        api._recorded_storyboard_task(
            "e1", recorder, resume=False  # type: ignore[arg-type]
        )
    )

    assert recorder.outcome == "succeeded"
    assert recorder.context_manifest is not None
    item = recorder.context_manifest["items"][0]
    assert item["key"] == "screenplay"
    assert item["selected_chars"] == 24000
    assert item["truncated"] is True
    assert item["source_artifact_id"] == "art_screenplay"
