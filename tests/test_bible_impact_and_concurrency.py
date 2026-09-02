"""人物谱定稿影响预检、并发版本控制与付费预检。"""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi import HTTPException

from app.domain import bible_ops
from app.evidence import repository as evidence_repository
from app.orchestration.engine import fingerprint
from tests.conftest import patch_api_everywhere


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
                "name": "甲一",
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
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "_project_or_404", lambda _pid: dict(conn.execute(
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
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "_project_or_404", lambda _pid: dict(conn.execute(
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
        "name": "丙老",
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
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "_project_or_404", lambda _pid: dict(conn.execute(
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
        assert "丙老" in detail["character_names"]
        assert detail["current_version"] == 3

    import asyncio
    asyncio.run(_run())


def test_refs_precheck_counts_images(monkeypatch) -> None:
    conn = _memory_conn()
    _seed_bible(conn)
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "_project_or_404", lambda _pid: dict(conn.execute(
        "SELECT * FROM projects WHERE id='proj_test'"
    ).fetchone()))
    quote = bible_ops.compute_refs_precheck("proj_test")
    assert quote["character_count"] == 1
    assert quote["image_count"] == 3
    # compute_refs_precheck 是未签发的原始范围指纹，quote_id 只有经
    # _issue_scope_quote 落库才产生（见 test_bible_generate_requires_confirm
    # 与 test_payment_quote_expires_and_consumed_quote_replays）。
    assert "quote_id" not in quote
    assert quote["scope_fingerprint"] == fingerprint({
        "project_id": "proj_test",
        "character": None,
        "characters": None,
        "resume": False,
        "view_role": None,
        "image_count": 3,
        "bible_version": 1,
    })


def test_bible_generate_requires_confirm(monkeypatch) -> None:
    conn = _memory_conn()
    _seed_bible(conn)
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "_require_harness_engine", lambda _pid: None)
    patch_api_everywhere(monkeypatch, "_project_or_404", lambda _pid: dict(conn.execute(
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
        "name": "丙老",
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
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "_project_or_404", lambda _pid: dict(conn.execute(
        "SELECT * FROM projects WHERE id='proj_test'"
    ).fetchone()))

    all_quote = bible_ops.compute_refs_precheck("proj_test")
    filtered = bible_ops.compute_refs_precheck("proj_test", characters=["丙老"])
    assert all_quote["character_count"] == 2
    assert filtered["character_count"] == 1
    assert filtered["scope"][0]["character"] == "丙老"
    assert filtered["characters"] == ["丙老"]
    assert filtered["image_count"] == 3


def test_payment_quote_expires_and_consumed_quote_replays(monkeypatch) -> None:
    conn = _memory_conn()
    _seed_bible(conn)
    clock = {"value": 100.0}
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "now", lambda: clock["value"])
    patch_api_everywhere(monkeypatch, "_project_or_404", lambda _pid: dict(conn.execute(
        "SELECT * FROM projects WHERE id='proj_test'"
    ).fetchone()))

    current = bible_ops.compute_refs_precheck("proj_test")
    issued = bible_ops._issue_scope_quote(current)
    clock["value"] = issued["quote_expires_at"] + 1
    with pytest.raises(HTTPException) as exc:
        bible_ops._validate_scope_quote("proj_test", issued["quote_id"], current)
    assert exc.value.detail["code"] == "QUOTE_STALE"

    clock["value"] = 200.0
    consumed = bible_ops._issue_scope_quote(current)
    bible_ops._consume_payment_quote(consumed["quote_id"], task_id="refs:proj_test", run_id="run_1")
    clock["value"] = consumed["quote_expires_at"] + 100
    row = bible_ops._validate_scope_quote("proj_test", consumed["quote_id"], current)
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
            "portrait_bad", "proj_test", "甲一", 1, None, "黑发少年", "prompt", str(image),
            None, 1, None, "failed",
            json.dumps({"status": "failed", "hard_failures": ["face_mismatch"], "issues": []}),
            None, 1.0,
        ),
    )
    conn.commit()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "_media_url", lambda path: str(path))
    patch_api_everywhere(monkeypatch, "_project_or_404", lambda _pid: dict(conn.execute(
        "SELECT * FROM projects WHERE id='proj_test'"
    ).fetchone()))

    async def _run():
        result = await bible_ops.adopt_portrait_candidate(
            "proj_test", "甲一", "portrait_bad",
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
            "portrait_partial", "proj_test", "甲一", 1, None, "黑发少年", "prompt", str(image),
            None, 1, None, "ready", json.dumps({"status": "ready", "issues": []}), None, 1.0,
        ),
    )
    conn.execute(
        "INSERT INTO character_portrait_views(id,portrait_id,view_role,image_path,status,created_at) "
        "VALUES('view_front','portrait_partial','front_full',?,'ready',1)",
        (str(image),),
    )
    conn.commit()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "_media_url", lambda path: str(path))
    patch_api_everywhere(monkeypatch, "_project_or_404", lambda _pid: dict(conn.execute(
        "SELECT * FROM projects WHERE id='proj_test'"
    ).fetchone()))

    import asyncio
    async def _run():
        result = await bible_ops.adopt_portrait_candidate(
            "proj_test", "甲一", "portrait_partial",
            {"reason": "人工检查", "bypass_soft": True},
        )
        assert result["adopted"] is True
        assert result["gate_retry_exhausted"] is True
        assert "missing_required_view=three_quarter" in result["soft_warnings"]

    asyncio.run(_run())


def _ready_pack(conn: sqlite3.Connection, portrait_id: str, name: str) -> None:
    conn.execute(
        "INSERT INTO character_portraits("
        "id, project_id, character_name, ep_start, ep_end, appearance, prompt, image_path, "
        "base_portrait_id, bible_version, artifact_id, pack_status, group_qa_json, change_json, created_at"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            portrait_id, "proj_test", name, 1, None, "黑发少年", "prompt", "/tmp/front.jpg",
            None, 1, None, "ready", None, None, 1.0,
        ),
    )
    for role in ("front_full", "three_quarter", "profile"):
        conn.execute(
            "INSERT INTO character_portrait_views(id,portrait_id,view_role,image_path,status,created_at) "
            "VALUES(?,?,?,?,'ready',1)",
            (f"{portrait_id}-{role}", portrait_id, role, "/tmp/front.jpg"),
        )


def _portrait_with_files(
    conn: sqlite3.Connection, portrait_id: str, name: str, tmp_path,
) -> tuple[object, object]:
    """一套落盘的定妆照：主图 + 一张视角图，返回两个文件路径供断言。"""
    main_image = tmp_path / f"{portrait_id}-main.jpg"
    main_image.write_bytes(b"main")
    view_image = tmp_path / f"{portrait_id}-view.jpg"
    view_image.write_bytes(b"view")
    conn.execute(
        "INSERT INTO character_portraits("
        "id, project_id, character_name, ep_start, ep_end, appearance, prompt, image_path, "
        "base_portrait_id, bible_version, artifact_id, pack_status, group_qa_json, change_json, created_at"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            portrait_id, "proj_test", name, 1, None, "黑发少年", "prompt", str(main_image),
            None, 1, None, "ready", None, None, 1.0,
        ),
    )
    conn.execute(
        "INSERT INTO character_portrait_views(id,portrait_id,view_role,image_path,status,created_at) "
        "VALUES(?,?,'profile',?,'ready',1)",
        (f"{portrait_id}-profile", portrait_id, str(view_image)),
    )
    return main_image, view_image


def _bible_with(names: list[str]) -> object:
    """按名单造一份合法 Bible 实例，只有角色名不同。"""
    from app.schemas import Bible

    return Bible(**{
        "world": {
            "visual_style_canonical": "国风水墨清透光影，细腻线条与柔和晕染",
            "era": "古代",
            "genre": "玄幻",
        },
        "characters": [
            {
                "name": name,
                "role": "主角",
                "appearance_canonical": "黑发少年，玄色劲装，目光坚定，身形修长，腰间佩火纹玉佩，英气逼人",
                "personality": "坚韧",
                "speech_style": "沉稳",
                "relationships": [],
            }
            for name in names
        ],
        "scenes": [],
    })


def test_removing_a_character_takes_its_portrait_with_it(tmp_path) -> None:
    """把角色移出人物谱，它的定妆照必须一起消失。

    _resolve_portrait_id 只按 project_id + character_name 查表，不看这个名字还在
    不在谱里。孤儿行会被映射器当成合法角色绑上去，实测把整集映射卡死在反幻觉闸
    上——删了谱里的卡也修不好，因为闸拦的是 portrait 命中。
    """
    conn = _memory_conn()
    _seed_bible(conn)
    kept_main, kept_view = _portrait_with_files(conn, "portrait_kept", "甲一", tmp_path)
    gone_main, gone_view = _portrait_with_files(conn, "portrait_gone", "乙二", tmp_path)
    conn.commit()
    old_bible_json = json.dumps(
        {"characters": [{"name": "甲一"}, {"name": "乙二"}]}, ensure_ascii=False,
    )

    purged = bible_ops._purge_removed_character_portraits(
        conn, "proj_test", old_bible_json, _bible_with(["甲一"]),
    )

    assert purged["characters"] == ["乙二"]
    assert purged["records"] == 1
    assert purged["files"] == 2
    surviving = [
        row["character_name"] for row in conn.execute(
            "SELECT character_name FROM character_portraits WHERE project_id='proj_test'"
        )
    ]
    assert surviving == ["甲一"], "只有被移出人物谱的角色该失去定妆照"
    assert conn.execute(
        "SELECT COUNT(*) c FROM character_portrait_views WHERE portrait_id='portrait_gone'"
    ).fetchone()["c"] == 0
    assert not gone_main.exists() and not gone_view.exists()
    assert kept_main.exists() and kept_view.exists(), "留在谱里的角色，图一张都不能少"


def test_editing_a_character_never_touches_its_portrait(tmp_path) -> None:
    """名单没变时一张图都不能动——清理只认「移出名单」这一件事，不认字段改动。"""
    conn = _memory_conn()
    _seed_bible(conn)
    main_image, view_image = _portrait_with_files(conn, "portrait_kept", "甲一", tmp_path)
    conn.commit()
    old_bible_json = json.dumps(
        {"characters": [{"name": "甲一", "personality": "坚韧"}]}, ensure_ascii=False,
    )

    purged = bible_ops._purge_removed_character_portraits(
        conn, "proj_test", old_bible_json, _bible_with(["甲一"]),
    )

    assert purged["records"] == 0
    assert conn.execute(
        "SELECT COUNT(*) c FROM character_portraits WHERE project_id='proj_test'"
    ).fetchone()["c"] == 1
    assert main_image.exists() and view_image.exists()


def test_added_character_without_portrait_purges_nothing(tmp_path) -> None:
    """新增角色时旧谱名字是新谱的子集，差集为空，不能误删任何东西。"""
    conn = _memory_conn()
    _seed_bible(conn)
    main_image, _ = _portrait_with_files(conn, "portrait_kept", "甲一", tmp_path)
    conn.commit()
    old_bible_json = json.dumps({"characters": [{"name": "甲一"}]}, ensure_ascii=False)

    purged = bible_ops._purge_removed_character_portraits(
        conn, "proj_test", old_bible_json, _bible_with(["甲一", "乙二"]),
    )

    assert purged == {"characters": [], "records": 0, "files": 0}
    assert main_image.exists()


def test_purge_scopes_to_the_project_being_edited(tmp_path) -> None:
    """同名角色分属两个项目时，只有正在编辑的那个项目的定妆照该退场。"""
    conn = _memory_conn()
    _seed_bible(conn)
    mine_main, _ = _portrait_with_files(conn, "portrait_mine", "乙二", tmp_path)
    other_main, other_view = _portrait_with_files(conn, "portrait_other", "乙二", tmp_path)
    conn.execute(
        "UPDATE character_portraits SET project_id='proj_other' WHERE id='portrait_other'",
    )
    conn.commit()
    old_bible_json = json.dumps(
        {"characters": [{"name": "甲一"}, {"name": "乙二"}]}, ensure_ascii=False,
    )

    purged = bible_ops._purge_removed_character_portraits(
        conn, "proj_test", old_bible_json, _bible_with(["甲一"]),
    )

    assert purged["records"] == 1
    assert not mine_main.exists()
    assert other_main.exists() and other_view.exists(), "别的项目的同名角色不受影响"
    assert conn.execute(
        "SELECT COUNT(*) c FROM character_portraits WHERE id='portrait_other'"
    ).fetchone()["c"] == 1


def test_refs_progress_excludes_ineligible_from_missing(monkeypatch) -> None:
    conn = _memory_conn()
    bible = {
        "world": {"visual_style_canonical": "国风", "era": "古代", "genre": "玄幻"},
        "characters": [
            {
                "name": "甲一",
                "role": "主角",
                "appearance_canonical": "黑发少年，玄色劲装，目光坚定，身形修长，腰间佩火纹玉佩，英气逼人",
                "personality": "坚韧",
                "speech_style": "沉稳",
                "relationships": [],
                "portrait_eligible": True,
                "appearance_status": "grounded",
                "presence_status": "onstage",
            },
            {
                "name": "孟浩",
                "role": "主角",
                "appearance_canonical": "外观待补全，详情生成未通过校验，当前不自动定妆",
                "personality": "",
                "speech_style": "",
                "relationships": [],
                "portrait_eligible": False,
                "appearance_status": "insufficient_evidence",
                "presence_status": "onstage",
            },
            {
                "name": "王腾飞",
                "role": "关键伏笔角色",
                "appearance_canonical": "外观待原文真实出场后补全，当前不自动定妆",
                "personality": "",
                "speech_style": "",
                "relationships": [],
                "portrait_eligible": False,
                "appearance_status": "deferred",
                "presence_status": "mentioned_only",
            },
        ],
        "scenes": [],
    }
    conn.execute(
        "INSERT INTO projects(id, bible_json, bible_version, bible_artifact_id, bible_status, refs_status) "
        "VALUES('proj_test', ?, 1, 'art_bible_1', 'ready', 'ready')",
        (json.dumps(bible, ensure_ascii=False),),
    )
    _ready_pack(conn, "pack_jia", "甲一")
    conn.commit()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "_project_or_404", lambda _pid: dict(conn.execute(
        "SELECT * FROM projects WHERE id='proj_test'"
    ).fetchone()))
    patch_api_everywhere(monkeypatch, "_refs_generation_busy", lambda _pid: False)

    import asyncio
    progress = asyncio.run(bible_ops.refs_progress("proj_test"))
    assert progress["total"] == 1
    assert progress["ready"] == 1
    assert progress["missing"] == 0
    assert progress["failed"] == 0
    assert progress["blocked"] == 2
    assert progress["deferred"] == 0
    by_status = {item["character"]: item["status"] for item in progress["items"]}
    assert by_status == {"甲一": "ready", "孟浩": "blocked", "王腾飞": "blocked"}

    quote = bible_ops.compute_refs_precheck("proj_test", resume=True)
    assert quote["character_count"] == 1
    assert quote["image_count"] == 0
    assert quote["scope"] == []
