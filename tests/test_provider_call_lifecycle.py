import asyncio
import base64
import sqlite3

import httpx

from app import api, hiagent, video_modes


class _Response:
    status_code = 200
    text = '{"choices": []}'

    def json(self):
        return {"choices": []}


class _Client:
    async def post(self, url, *, json, headers):
        return _Response()


def test_post_json_writes_running_before_updating_same_ledger_row(monkeypatch) -> None:
    events: list[tuple] = []

    def start(kind, model, *, meta=None, request_json=None):
        events.append(("start", kind, model, meta, request_json))
        return 42

    def finish(call_id, status, http_status, latency_ms, *, error=None, response_json=None):
        events.append(("finish", call_id, status, http_status, response_json))

    monkeypatch.setattr(hiagent, "start_provider_call", start)
    monkeypatch.setattr(hiagent, "finish_provider_call", finish)

    result = asyncio.run(hiagent._post_json(
        _Client(), "https://example.invalid/chat", {"prompt": "hello"},
        kind="chat", model="test-model", headers={"x": "y"}, meta={"stage": "可拍剧本"},
    ))

    assert result == {"choices": []}
    assert events[0][0] == "start"
    assert events[0][3]["http_attempt"] == 1
    assert events[1][:4] == ("finish", 42, "OK", 200)


def test_post_json_write_timeout_is_logged_and_not_retried(monkeypatch) -> None:
    events: list[tuple] = []

    class WriteTimeoutClient:
        async def post(self, url, *, json, headers):
            raise httpx.WriteTimeout("upload stalled")

    monkeypatch.setattr(
        hiagent, "start_provider_call",
        lambda kind, model, *, meta=None, request_json=None: events.append(("start", meta)) or 7,
    )
    monkeypatch.setattr(
        hiagent, "finish_provider_call",
        lambda call_id, status, http_status, latency_ms, *, error=None, response_json=None:
            events.append(("finish", status, error)),
    )

    try:
        asyncio.run(hiagent._post_json(
            WriteTimeoutClient(), "https://example.invalid/image", {"image": "abc"},
            kind="image_edit", model="test-model", headers={"x": "y"}, retries=2,
        ))
    except hiagent.ProviderError as exc:
        assert exc.timeout_phase == "write"
        assert "请求" in str(exc)
    else:
        raise AssertionError("expected ProviderError")

    assert [event[0] for event in events] == ["start", "finish"]
    assert events[0][1]["request_bytes"] > 0
    assert "WriteTimeout" in events[1][2]
    assert "phase=write" in events[1][2]


def test_prepare_image_data_urls_records_compression_stats(monkeypatch) -> None:
    raw = b"large-image-payload"
    monkeypatch.setattr(hiagent, "_compress_image_bytes", lambda value: b"small" if value == raw else value)

    prepared, stats = asyncio.run(hiagent._prepare_image_data_urls([
        "data:image/png;base64," + base64.b64encode(raw).decode("ascii"),
    ]))

    assert prepared == ["data:image/jpeg;base64," + base64.b64encode(b"small").decode("ascii")]
    assert stats["media_input_bytes_original"] == len(raw)
    assert stats["media_input_bytes_sent"] == len(b"small")
    assert stats["media_input_compressed_count"] == 1


def test_seeded_image_write_timeout_immediately_falls_back_without_seed(monkeypatch) -> None:
    seen_inputs: list[list[str] | None] = []

    async def fake_generate_image(prompt, *, size, image_inputs=None, call_meta=None):
        seen_inputs.append(image_inputs)
        if image_inputs:
            raise hiagent.ProviderError("上传超时", retryable=True, timeout_phase="write")
        return {"url": "https://example.invalid/generated.jpg"}

    monkeypatch.setattr(hiagent, "generate_image", fake_generate_image)

    result = asyncio.run(video_modes._generate_image_with_seed_fallback(
        "prompt", ["data:image/jpeg;base64,abc"], call_meta={"shot_no": 1},
    ))

    assert result["url"].endswith("generated.jpg")
    assert seen_inputs == [["data:image/jpeg;base64,abc"], None]


def test_jobs_overview_includes_running_screenplay(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE jobs(
            id TEXT, kind TEXT, shot_id TEXT, version_id TEXT, episode_id TEXT,
            project_id TEXT, status TEXT, error TEXT, created_at REAL, updated_at REAL,
            after_shot_id TEXT, after_version_id TEXT, scene_kinds TEXT
        );
        CREATE TABLE shots(id TEXT, shot_no INTEGER);
        CREATE TABLE projects(id TEXT, name TEXT);
        CREATE TABLE episodes(
            id TEXT, project_id TEXT, episode_no INTEGER, title TEXT,
            screenplay_status TEXT, screenplay_error TEXT, screenplay_started_at REAL,
            screenplay_updated_at REAL, created_at REAL
        );
        INSERT INTO projects VALUES('p1', '测试项目');
        INSERT INTO episodes VALUES('e1', 'p1', 1, '第一集', 'running', NULL, 100, 120, 10);
    """)
    monkeypatch.setattr(api, "get_conn", lambda: conn)

    result = api.jobs_overview()

    assert result["recent"][0]["kind"] == "screenplay"
    assert result["recent"][0]["status"] == "running"
    assert result["recent"][0]["episode_no"] == 1
    assert result["counts"]["running"] == 1
