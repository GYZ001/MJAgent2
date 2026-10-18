"""截图故障回归：401 失败可见、重试耗尽仍拦截、修复配置后可显式补建。"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from app import db
from app.db import get_conn, now
from app.domain.bible_ops import props_api
from app.props import card_pending_ensure as ensure
from app.props.card_pending_recovery import builds_for_project, public_build_error, retry_failed_build
from app.props.card_pending_store import ensure_tables_on_connection, get_pending
from tests.test_prop_storyboard_card_pending import _fake_chat_structured, _seed_episode, _seed_project, _seed_shot


def _failed_project(*, attempts: int = 3, error: str = '鉴权失败（HTTP 401）：{"api_key":"secret"}') -> None:
    _seed_project("proj_recovery")
    _seed_episode("proj_recovery", "ep_recovery", 4)
    for no in (1, 2):
        _seed_shot("ep_recovery", no, [{"label": "铜镜", "description": "铜制、圆形、背有纹路"}])
    conn = get_conn()
    ensure_tables_on_connection(conn)
    conn.execute(
        "INSERT INTO prop_storyboard_card_pending(id,project_id,label,status,attempts,error,created_at,updated_at) "
        "VALUES('pending','proj_recovery','铜镜','failed',?,?,?,?)", (attempts, error, now(), now()),
    )
    conn.commit()


@pytest.mark.parametrize("attempts", [1, 3])
def test_failed_gate_reports_auth_failure_without_auto_retry(monkeypatch, attempts) -> None:
    _failed_project(attempts=attempts)
    launches = []
    monkeypatch.setattr(ensure, "launch_background_ensure", lambda **kwargs: launches.append(kwargs))
    message = asyncio.run(ensure.pending_prop_card_gate("proj_recovery", "ep_recovery"))
    assert "补建失败" in message and "铜镜" in message
    assert "HTTP 401" in message and "模型中心" in message and "重试补建" in message
    assert "正在补建" not in message and "secret" not in message and ".env" not in message
    assert launches == []
    assert get_pending(get_conn(), project_id="proj_recovery", label="铜镜")["attempts"] == attempts


def test_failed_build_does_not_restart_when_another_label_is_ensured(monkeypatch) -> None:
    _failed_project(attempts=1)
    async def unexpected_call(**kwargs):
        raise AssertionError("失败记录必须由显式重试恢复")
    monkeypatch.setattr(ensure, "_build_one_label", unexpected_call)
    result = asyncio.run(ensure.ensure_storyboard_prop_cards(project_id="proj_recovery", episode_id="ep_recovery"))
    assert result["attempted"] == []


@pytest.mark.asyncio
async def test_library_shows_failed_build_without_any_bible_card() -> None:
    _failed_project()
    response = await props_api.list_props("proj_recovery")
    assert response["items"] == []
    assert len(response["builds"]) == 1
    build = response["builds"][0]
    assert build["label"] == "铜镜" and build["status"] == "failed"
    assert build["episodes"] == [{"id": "ep_recovery", "episode_no": 4}]
    assert "HTTP 401" in build["error"] and "secret" not in build["error"]


@pytest.mark.parametrize("status", [401, 403, 429, 503])
def test_public_error_uses_only_http_status(status) -> None:
    text = public_build_error(f'HTTP {status}：{{"token":"secret","message":"private detail"}}')
    assert f"HTTP {status}" in text
    assert "secret" not in text and "private detail" not in text


def test_library_does_not_expose_obsolete_or_ready_builds() -> None:
    _failed_project()
    conn = get_conn()
    conn.execute("UPDATE shots SET shot_contract_json='{}' WHERE episode_id='ep_recovery'")
    conn.commit()
    assert builds_for_project(conn, "proj_recovery") == []
    conn.execute("UPDATE prop_storyboard_card_pending SET status='ready'")
    conn.commit()
    assert builds_for_project(conn, "proj_recovery") == []


def test_retry_episode_comes_from_actual_candidates_not_earlier_single_mention() -> None:
    _failed_project()
    _seed_shot("ep_recovery", 3, [{"label": "铜镜", "description": "铜镜"}])
    conn = get_conn()
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,status,created_at) VALUES('ep_earlier','proj_recovery',1,'scripted',0)",
    )
    conn.execute("UPDATE shots SET episode_id='ep_earlier' WHERE shot_no=3")
    conn.commit()
    builds = builds_for_project(conn, "proj_recovery")
    assert builds[0]["episodes"] == [{"id": "ep_recovery", "episode_no": 4}]


@pytest.mark.asyncio
async def test_explicit_retry_builds_only_requested_label(monkeypatch) -> None:
    _failed_project()
    for no in (1, 2):
        _seed_shot("ep_recovery", no + 2, [{"label": "铜镜"}, {"label": "其它道具"}])
    calls = []
    async def build_one(**kwargs):
        calls.append(kwargs["label"])
    monkeypatch.setattr(ensure, "_build_one_label", build_one)
    response = await props_api.retry_prop_build("proj_recovery", props_api.PropBuildRetryBody(
        episode_id="ep_recovery", label="铜镜",
    ))
    assert response["accepted"]
    await asyncio.gather(*tuple(ensure._BACKGROUND_ENSURE_TASKS))
    assert calls == ["铜镜"]


@pytest.mark.asyncio
async def test_explicit_retry_is_committed_and_deduplicated_under_concurrency() -> None:
    _failed_project()
    results = await asyncio.gather(*(
        retry_failed_build("proj_recovery", "ep_recovery", "铜镜") for _ in range(2)
    ))
    assert sum(bool(result) for result in results) == 1
    with sqlite3.connect(db.DB_PATH) as observer:
        status, attempts, error = observer.execute(
            "SELECT status,attempts,error FROM prop_storyboard_card_pending WHERE id='pending'",
        ).fetchone()
    assert (status, attempts, error) == ("queued", 0, None)


@pytest.mark.asyncio
async def test_retry_rejects_cross_project_and_unreferenced_label() -> None:
    _failed_project()
    _seed_project("proj_other")
    _seed_episode("proj_other", "ep_other", 1)
    with pytest.raises(ValueError, match="不属于当前项目"):
        await retry_failed_build("proj_recovery", "ep_other", "铜镜")
    with pytest.raises(ValueError, match="不是本集"):
        await retry_failed_build("proj_recovery", "ep_recovery", "虚构道具")
    assert get_pending(get_conn(), project_id="proj_recovery", label="铜镜")["attempts"] == 3


@pytest.mark.asyncio
async def test_retry_route_restores_card_and_reference_without_changing_shots(monkeypatch, tmp_path) -> None:
    from app.props import judge, service
    _failed_project()
    conn = get_conn()
    shots_before = [tuple(row) for row in conn.execute("SELECT * FROM shots ORDER BY id")]
    async def generate_image(project_id, name, prompt):
        path = tmp_path / "prop.png"
        path.write_bytes(b"reference")
        return str(path)
    monkeypatch.setattr(judge.model_gateway, "chat_structured", _fake_chat_structured)
    monkeypatch.setattr(service, "generate_prop_reference_image", generate_image)
    response = await props_api.retry_prop_build("proj_recovery", props_api.PropBuildRetryBody(
        episode_id="ep_recovery", label="铜镜",
    ))
    assert response["accepted"] is True
    await asyncio.gather(*tuple(ensure._BACKGROUND_ENSURE_TASKS))
    with sqlite3.connect(db.DB_PATH) as observer:
        assert observer.execute("SELECT status FROM prop_storyboard_card_pending WHERE id='pending'").fetchone()[0] == "ready"
        assert observer.execute("SELECT status FROM prop_references WHERE prop_name='铜镜'").fetchone()[0] == "ready"
        assert len(json.loads(observer.execute("SELECT bible_json FROM projects WHERE id='proj_recovery'").fetchone()[0])["props"]) == 1
    assert await ensure.pending_prop_card_gate("proj_recovery", "ep_recovery") is None
    assert [tuple(row) for row in conn.execute("SELECT * FROM shots ORDER BY id")] == shots_before
    assert (await props_api.list_props("proj_recovery"))["builds"] == []


@pytest.mark.asyncio
async def test_retry_existing_card_regenerates_missing_image(monkeypatch, tmp_path) -> None:
    from app.props import service
    _failed_project()
    conn = get_conn()
    bible = json.loads(conn.execute("SELECT bible_json FROM projects WHERE id='proj_recovery'").fetchone()[0])
    bible["props"] = [{"name": "铜镜", "appearance_canonical": "铜制、圆形、背有纹路", "aliases": []}]
    conn.execute("UPDATE projects SET bible_json=? WHERE id='proj_recovery'", (json.dumps(bible),))
    conn.commit()
    calls = []
    async def generate_image(project_id, name, prompt):
        calls.append(name)
        path = tmp_path / "prop.png"
        path.write_bytes(b"reference")
        return str(path)
    monkeypatch.setattr(service, "generate_prop_reference_image", generate_image)
    await retry_failed_build("proj_recovery", "ep_recovery", "铜镜")
    await ensure.ensure_storyboard_prop_cards(project_id="proj_recovery", episode_id="ep_recovery")
    assert calls == ["铜镜"]
    assert get_pending(conn, project_id="proj_recovery", label="铜镜")["status"] == "ready"


@pytest.mark.asyncio
async def test_retry_route_translates_invalid_scope_to_409() -> None:
    from fastapi import HTTPException
    _failed_project()
    with pytest.raises(HTTPException) as exc:
        await props_api.retry_prop_build("proj_recovery", props_api.PropBuildRetryBody(episode_id="missing", label="铜镜"))
    assert exc.value.status_code == 409
