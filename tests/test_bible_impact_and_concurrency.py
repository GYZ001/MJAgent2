"""人物谱定稿影响预检、并发版本控制与付费预检。"""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi import HTTPException

from app.domain import bible_ops
from app.evidence import repository as evidence_repository
from app.orchestration.engine import fingerprint


def _memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE projects(
          id TEXT PRIMARY KEY,
          bible_json TEXT,
          bible_version INTEGER DEFAULT 0,
          bible_artifact_id TEXT,
          bible_status TEXT DEFAULT 'idle',
          bible_error TEXT,
          refs_status TEXT DEFAULT 'idle',
          refs_error TEXT,
          refs_target TEXT,
          scene_refs_status TEXT DEFAULT 'idle'
        );
        CREATE TABLE character_portraits(
          id TEXT PRIMARY KEY,
          project_id TEXT,
          character_name TEXT,
          ep_start INTEGER,
          ep_end INTEGER,
          appearance TEXT,
          prompt TEXT,
          image_path TEXT,
          base_portrait_id TEXT,
          bible_version INTEGER DEFAULT 0,
          artifact_id TEXT,
          pack_status TEXT,
          group_qa_json TEXT,
          change_json TEXT,
          created_at REAL DEFAULT 0
        );
        CREATE TABLE character_portrait_views(
          id TEXT PRIMARY KEY,
          portrait_id TEXT,
          view_role TEXT,
          framing TEXT,
          image_path TEXT,
          prompt TEXT,
          qa_json TEXT,
          status TEXT,
          selected INTEGER DEFAULT 0,
          input_fingerprint TEXT,
          created_at REAL
        );
        CREATE TABLE scene_references(
          id TEXT PRIMARY KEY,
          project_id TEXT,
          scene_name TEXT
        );
        CREATE TABLE artifacts(
          id TEXT PRIMARY KEY,
          type TEXT,
          scope_type TEXT,
          scope_id TEXT,
          status TEXT,
          trust_level TEXT,
          content_json TEXT,
          content_hash TEXT,
          parent_artifact_ids_json TEXT,
          contract_version TEXT,
          created_at REAL,
          stale_reason TEXT
        );
        CREATE TABLE evaluations(
          id TEXT PRIMARY KEY,
          artifact_id TEXT,
          evaluator_type TEXT,
          evaluator_name TEXT,
          evaluator_version TEXT,
          status TEXT,
          hard_gate_passed INTEGER,
          score REAL,
          evidence_json TEXT,
          created_at REAL
        );
        CREATE TABLE gate_decisions(
          id TEXT PRIMARY KEY,
          artifact_id TEXT,
          gate_key TEXT,
          decision TEXT,
          decided_by TEXT,
          reason TEXT,
          created_at REAL
        );
        """
    )
    return conn


def _seed_bible(conn: sqlite3.Connection, *, version: int = 1, artifact_id: str = "art_bible_1") -> dict:
    bible = {
        "world": {
            "visual_style_canonical": "国风水墨清透光影，细腻线条与柔和晕染",
            "era": "古代",
            "genre": "玄幻",
        },
        "characters": [
            {
                "name": "萧炎",
                "role": "主角",
                "appearance_canonical": "黑发少年，玄色劲装，目光坚定，身形修长，腰间佩火纹玉佩，英气逼人",
                "personality": "坚韧",
                "speech_style": "沉稳",
                "relationships": [],
            }
        ],
        "scenes": [],
    }
    conn.execute(
        "INSERT INTO projects(id, bible_json, bible_version, bible_artifact_id, bible_status) "
        "VALUES('proj_test', ?, ?, ?, 'ready')",
        (json.dumps(bible, ensure_ascii=False), version, artifact_id),
    )
    conn.execute(
        "INSERT INTO artifacts(id, type, scope_type, scope_id, status, trust_level, content_json, "
        "content_hash, parent_artifact_ids_json, contract_version, created_at) "
        "VALUES(?, 'character_bible', 'project', 'proj_test', 'approved', 'T4', ?, 'h', '[]', 'v1', 1)",
        (artifact_id, json.dumps(bible, ensure_ascii=False)),
    )
    conn.execute(
        "INSERT INTO artifacts(id, type, scope_type, scope_id, status, trust_level, content_json, "
        "content_hash, parent_artifact_ids_json, contract_version, created_at) "
        "VALUES('art_child_1', 'episode_screenplay', 'episode', 'ep1', 'approved', 'T3', '{}', 'h2', ?, 'v1', 2)",
        (json.dumps([artifact_id]),),
    )
    conn.commit()
    return bible


async def _passthrough_ui_route(_name: str, _args: dict):
    return None


def test_list_descendants_does_not_mutate(monkeypatch) -> None:
    conn = _memory_conn()
    _seed_bible(conn)
    monkeypatch.setattr(evidence_repository, "get_conn", lambda: conn)
    found = evidence_repository.list_descendants("art_bible_1")
    assert found == ["art_child_1"]
    row = conn.execute("SELECT status FROM artifacts WHERE id='art_child_1'").fetchone()
    assert row["status"] == "approved"


def test_impact_preview_returns_fingerprint(monkeypatch) -> None:
    conn = _memory_conn()
    bible = _seed_bible(conn)
    monkeypatch.setattr(bible_ops, "get_conn", lambda: conn)
    monkeypatch.setattr(bible_ops, "_project_or_404", lambda _pid: dict(conn.execute(
        "SELECT * FROM projects WHERE id='proj_test'"
    ).fetchone()))
    monkeypatch.setattr(evidence_repository, "get_conn", lambda: conn)

    preview = bible_ops.compute_bible_impact_preview(
        "proj_test", bible, expected_version=1,
    )
    assert preview["stale_count"] == 1
    assert preview["stale_assets"] == [{
        "id": "art_child_1",
        "type": "episode_screenplay",
        "status": "approved",
        "scope_type": "episode",
        "scope_id": "ep1",
    }]
    assert preview["stale_assets_truncated"] is False
    assert preview["fingerprint"]
    assert preview["change_types"]


def test_edit_bible_requires_expected_version(monkeypatch) -> None:
    conn = _memory_conn()
    bible = _seed_bible(conn)
    monkeypatch.setattr(bible_ops, "get_conn", lambda: conn)
    monkeypatch.setattr(bible_ops, "_project_or_404", lambda _pid: dict(conn.execute(
        "SELECT * FROM projects WHERE id='proj_test'"
    ).fetchone()))

    async def _run():
        # 绕过 Command Bus，直接验证领域门禁
        monkeypatch.setattr(
            "app.capabilities.dispatch.ui_route", _passthrough_ui_route,
        )
        with pytest.raises(HTTPException) as exc:
            await bible_ops.edit_bible("proj_test", {"bible": bible, "confirm": True})
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "EXPECTED_VERSION_REQUIRED"

    import asyncio
    asyncio.run(_run())


def test_edit_bible_conflict_keeps_server_characters(monkeypatch) -> None:
    conn = _memory_conn()
    bible = _seed_bible(conn, version=2)
    server = json.loads(conn.execute("SELECT bible_json FROM projects WHERE id='proj_test'").fetchone()["bible_json"])
    server["characters"].append({
        "name": "药老",
        "role": "配角",
        "appearance_canonical": "白发老者，道袍飘逸，目光深邃，手持药鼎，气质出尘，须发皆白",
        "personality": "慈祥",
        "speech_style": "沉稳",
        "relationships": [],
    })
    conn.execute(
        "UPDATE projects SET bible_json=?, bible_version=3 WHERE id='proj_test'",
        (json.dumps(server, ensure_ascii=False),),
    )
    conn.commit()
    monkeypatch.setattr(bible_ops, "get_conn", lambda: conn)
    monkeypatch.setattr(bible_ops, "_project_or_404", lambda _pid: dict(conn.execute(
        "SELECT * FROM projects WHERE id='proj_test'"
    ).fetchone()))
    monkeypatch.setattr(
        "app.capabilities.dispatch.ui_route", _passthrough_ui_route,
    )

    stale_client = dict(bible)
    async def _run():
        with pytest.raises(HTTPException) as exc:
            await bible_ops.edit_bible("proj_test", {
                "bible": stale_client,
                "expected_version": 2,
                "confirm": True,
                "impact_preview_fingerprint": "x",
            })
        assert exc.value.status_code == 409
        detail = exc.value.detail
        assert detail["code"] == "BIBLE_VERSION_CONFLICT"
        assert "药老" in detail["character_names"]
        assert detail["current_version"] == 3

    import asyncio
    asyncio.run(_run())


def test_refs_precheck_counts_images(monkeypatch) -> None:
    conn = _memory_conn()
    _seed_bible(conn)
    monkeypatch.setattr(bible_ops, "get_conn", lambda: conn)
    monkeypatch.setattr(bible_ops, "_project_or_404", lambda _pid: dict(conn.execute(
        "SELECT * FROM projects WHERE id='proj_test'"
    ).fetchone()))
    quote = bible_ops.compute_refs_cost_precheck("proj_test")
    assert quote["character_count"] == 1
    assert quote["image_count"] == 3
    assert quote["estimated_cost_cny"] == pytest.approx(0.6)
    assert quote["quote_id"] == fingerprint({
        "project_id": "proj_test",
        "character": None,
        "characters": None,
        "resume": False,
        "view_role": None,
        "image_count": 3,
        "unit": 0.2,
        "bible_version": 1,
    })


def test_bible_generate_requires_confirm(monkeypatch) -> None:
    conn = _memory_conn()
    _seed_bible(conn)
    monkeypatch.setattr(bible_ops, "get_conn", lambda: conn)
    monkeypatch.setattr(bible_ops, "_require_harness_engine", lambda _pid: None)
    monkeypatch.setattr(bible_ops, "_project_or_404", lambda _pid: dict(conn.execute(
        "SELECT * FROM projects WHERE id='proj_test'"
    ).fetchone()))

    async def _run():
        with pytest.raises(HTTPException) as exc:
            await bible_ops._start_bible_core("proj_test", "", confirm=False)
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "PAYMENT_CONFIRM_REQUIRED"
        assert exc.value.detail["precheck"]["quote_id"]

    import asyncio
    asyncio.run(_run())


def test_refs_precheck_filters_characters(monkeypatch) -> None:
    conn = _memory_conn()
    bible = _seed_bible(conn)
    bible["characters"].append({
        "name": "药老",
        "role": "导师",
        "appearance_canonical": "白发老者，道袍飘逸，目光深邃，手持药鼎，气质出尘，须发皆白",
        "personality": "慈祥",
        "speech_style": "沉稳",
        "relationships": [],
    })
    conn.execute(
        "UPDATE projects SET bible_json=? WHERE id='proj_test'",
        (json.dumps(bible, ensure_ascii=False),),
    )
    conn.commit()
    monkeypatch.setattr(bible_ops, "get_conn", lambda: conn)
    monkeypatch.setattr(bible_ops, "_project_or_404", lambda _pid: dict(conn.execute(
        "SELECT * FROM projects WHERE id='proj_test'"
    ).fetchone()))

    all_quote = bible_ops.compute_refs_cost_precheck("proj_test")
    filtered = bible_ops.compute_refs_cost_precheck("proj_test", characters=["药老"])
    assert all_quote["character_count"] == 2
    assert filtered["character_count"] == 1
    assert filtered["scope"][0]["character"] == "药老"
    assert filtered["characters"] == ["药老"]
    assert filtered["image_count"] == 3


def test_payment_quote_expires_and_consumed_quote_replays(monkeypatch) -> None:
    conn = _memory_conn()
    _seed_bible(conn)
    clock = {"value": 100.0}
    monkeypatch.setattr(bible_ops, "get_conn", lambda: conn)
    monkeypatch.setattr(bible_ops, "now", lambda: clock["value"])
    monkeypatch.setattr(bible_ops, "_project_or_404", lambda _pid: dict(conn.execute(
        "SELECT * FROM projects WHERE id='proj_test'"
    ).fetchone()))

    current = bible_ops.compute_refs_cost_precheck("proj_test")
    issued = bible_ops._issue_payment_quote(current)
    clock["value"] = issued["quote_expires_at"] + 1
    with pytest.raises(HTTPException) as exc:
        bible_ops._validate_payment_quote("proj_test", issued["quote_id"], current)
    assert exc.value.detail["code"] == "QUOTE_STALE"

    clock["value"] = 200.0
    consumed = bible_ops._issue_payment_quote(current)
    bible_ops._consume_payment_quote(consumed["quote_id"], task_id="refs:proj_test", run_id="run_1")
    clock["value"] = consumed["quote_expires_at"] + 100
    row = bible_ops._validate_payment_quote("proj_test", consumed["quote_id"], current)
    assert row["consumed_task_id"] == "refs:proj_test"
    assert row["consumed_run_id"] == "run_1"


def test_adopt_portrait_candidate_accepts_hard_failure_as_warning(monkeypatch, tmp_path) -> None:
    conn = _memory_conn()
    _seed_bible(conn)
    image = tmp_path / "portrait-bad.jpg"
    image.write_bytes(b"portrait")
    conn.execute(
        "INSERT INTO character_portraits("
        "id, project_id, character_name, ep_start, ep_end, appearance, prompt, image_path, "
        "base_portrait_id, bible_version, artifact_id, pack_status, group_qa_json, change_json, created_at"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "portrait_bad", "proj_test", "萧炎", 1, None, "黑发少年", "prompt", str(image),
            None, 1, None, "failed",
            json.dumps({"status": "failed", "hard_failures": ["face_mismatch"], "issues": []}),
            None, 1.0,
        ),
    )
    conn.commit()
    monkeypatch.setattr(bible_ops, "get_conn", lambda: conn)
    monkeypatch.setattr(bible_ops, "_media_url", lambda path: str(path))
    monkeypatch.setattr(bible_ops, "_project_or_404", lambda _pid: dict(conn.execute(
        "SELECT * FROM projects WHERE id='proj_test'"
    ).fetchone()))

    async def _run():
        result = await bible_ops.adopt_portrait_candidate(
            "proj_test", "萧炎", "portrait_bad",
            {"reason": "人工检查", "bypass_soft": True},
        )
        assert result["adopted"] is True
        assert result["gate_retry_exhausted"] is True
        assert "face_mismatch" in result["soft_warnings"]

    import asyncio
    asyncio.run(_run())


def test_adopt_portrait_candidate_accepts_missing_views_as_warning(monkeypatch, tmp_path) -> None:
    conn = _memory_conn()
    _seed_bible(conn)
    image = tmp_path / "front.jpg"
    image.write_bytes(b"front")
    conn.execute(
        "INSERT INTO character_portraits("
        "id, project_id, character_name, ep_start, ep_end, appearance, prompt, image_path, "
        "base_portrait_id, bible_version, artifact_id, pack_status, group_qa_json, change_json, created_at"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "portrait_partial", "proj_test", "萧炎", 1, None, "黑发少年", "prompt", str(image),
            None, 1, None, "ready", json.dumps({"status": "ready", "issues": []}), None, 1.0,
        ),
    )
    conn.execute(
        "INSERT INTO character_portrait_views(id,portrait_id,view_role,image_path,status,created_at) "
        "VALUES('view_front','portrait_partial','front_full',?,'ready',1)",
        (str(image),),
    )
    conn.commit()
    monkeypatch.setattr(bible_ops, "get_conn", lambda: conn)
    monkeypatch.setattr(bible_ops, "_media_url", lambda path: str(path))
    monkeypatch.setattr(bible_ops, "_project_or_404", lambda _pid: dict(conn.execute(
        "SELECT * FROM projects WHERE id='proj_test'"
    ).fetchone()))

    import asyncio
    async def _run():
        result = await bible_ops.adopt_portrait_candidate(
            "proj_test", "萧炎", "portrait_partial",
            {"reason": "人工检查", "bypass_soft": True},
        )
        assert result["adopted"] is True
        assert result["gate_retry_exhausted"] is True
        assert "missing_required_view=three_quarter" in result["soft_warnings"]

    asyncio.run(_run())
