"""资产更新检查在世界书缺失时保持只读，并给出可恢复的中文状态。"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.domain.video_ops import asset_refresh
from app.domain.video_ops.asset_refresh_report import episode_asset_refresh_groups
from app.main import _on_http_exception, _on_unhandled


@pytest.fixture
def asset_case(monkeypatch) -> Iterator[tuple[sqlite3.Connection, dict]]:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE projects (id TEXT PRIMARY KEY, bible_json TEXT);
        CREATE TABLE shots (
            id TEXT PRIMARY KEY, episode_id TEXT, shot_no INTEGER, adopted_version_id TEXT
        );
        CREATE TABLE shot_versions (id TEXT PRIMARY KEY, status TEXT);
        INSERT INTO projects VALUES ('p', '');
    """)
    for index in range(3):
        conn.execute("INSERT INTO shots VALUES (?, 'e', ?, ?)", (f"s{index}", index, f"v{index}"))
        conn.execute("INSERT INTO shot_versions VALUES (?, 'succeeded')", (f"v{index}",))
    conn.commit()
    episode = {"id": "e", "project_id": "p", "episode_no": 1}
    monkeypatch.setattr(asset_refresh, "get_conn", lambda: conn)
    monkeypatch.setattr(asset_refresh, "_episode_or_404", lambda _: episode)
    monkeypatch.setattr(asset_refresh, "_account_quota", lambda *_: None)
    try:
        yield conn, episode
    finally:
        conn.close()


def _set_bible(conn: sqlite3.Connection, raw: str | None) -> str:
    conn.execute("UPDATE projects SET bible_json=? WHERE id='p'", (raw,))
    conn.commit()
    return "\n".join(conn.iterdump())


@pytest.mark.parametrize("raw", [None, "", "  ", "{}", " { } "])
def test_missing_bible_has_chinese_recovery_error_and_preserves_videos(asset_case, raw) -> None:
    conn, episode = asset_case
    before = _set_bible(conn, raw)
    with pytest.raises(HTTPException) as caught:
        episode_asset_refresh_groups(conn, episode)
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "ASSET_REFRESH_BIBLE_NOT_READY"
    assert "尚未建立世界书" in caught.value.detail["message"]
    assert caught.value.detail["recommended_action"] == "open_bible"
    assert "\n".join(conn.iterdump()) == before


@pytest.mark.parametrize("raw", [
    "{", "null", "[]", '{"characters": []}',
    '{"characters": [], "world": {}}',
])
def test_invalid_bible_is_distinct_from_missing_and_keeps_original_cause(asset_case, raw) -> None:
    conn, episode = asset_case
    before = _set_bible(conn, raw)
    with pytest.raises(HTTPException) as caught:
        episode_asset_refresh_groups(conn, episode)
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "ASSET_REFRESH_BIBLE_INVALID"
    assert "世界书数据不完整或已损坏" in caught.value.detail["message"]
    assert caught.value.detail["recommended_action"] == "contact_support"
    assert caught.value.__cause__ is not None
    assert "\n".join(conn.iterdump()) == before


@pytest.mark.parametrize("no_shots", [False, True])
def test_no_adopted_videos_needs_no_bible_or_screenplay(asset_case, monkeypatch, no_shots) -> None:
    conn, episode = asset_case
    conn.execute("DELETE FROM shots" if no_shots else "UPDATE shots SET adopted_version_id=NULL")
    conn.commit()

    def unexpected_load(*_):
        pytest.fail("没有已采用视频时不应读取世界书或剧本")

    monkeypatch.setattr(asset_refresh.drift, "episode_bible_and_screenplay", unexpected_load)
    report = asset_refresh.asset_refresh_report(episode["id"])
    assert report["groups"] == []
    assert report["quota"]["needs_regen_shot_count"] == 0
    assert report["quota"]["needs_regen_seconds"] == 0


@pytest.mark.parametrize("raw", ["", '{"characters": []}'])
def test_http_response_has_safe_chinese_detail_and_diagnostic_reference(asset_case, raw) -> None:
    conn, _ = asset_case
    before = _set_bible(conn, raw)
    app = FastAPI()
    app.add_exception_handler(HTTPException, _on_http_exception)
    app.add_exception_handler(Exception, _on_unhandled)
    app.add_api_route("/api/episodes/{episode_id}/asset-refresh", asset_refresh.asset_refresh_report)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/episodes/e/asset-refresh")
    assert response.status_code == 409
    body = response.json()
    assert body["detail"]["recommended_action"] == ("open_bible" if not raw else "contact_support")
    assert body["detail"]["error_ref"]["error_id"] == body["error_id"]
    assert "世界书" in body["detail"]["message"]
    assert "validation errors" not in json.dumps(body)
    assert "errors.pydantic.dev" not in json.dumps(body)
    assert "\n".join(conn.iterdump()) == before


@pytest.mark.parametrize("operation", ["regenerate", "adopt"])
async def test_missing_bible_also_blocks_mutations_without_changing_videos(asset_case, operation) -> None:
    conn, _ = asset_case
    before = "\n".join(conn.iterdump())
    with pytest.raises(HTTPException) as caught:
        if operation == "regenerate":
            await asset_refresh._asset_refresh_regenerate_core("e", {"idempotency_key": "probe"})
        else:
            asset_refresh._asset_refresh_adopt_core("e", {
                "entity_key": "character:角色", "versions": {"s0": "v0"}, "reason": "核对参考资产",
            })
    assert caught.value.detail["code"] == "ASSET_REFRESH_BIBLE_NOT_READY"
    assert "\n".join(conn.iterdump()) == before
