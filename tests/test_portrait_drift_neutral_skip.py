"""app.portraits.portrait_drift.ensure_cards_for_screenplay：中性定妆照生效的
角色跳过按集自动漂移重绘（app.portraits.neutral_identity 2026-09-30）。

只覆盖新增的一条判据：``costume_mode=="neutral"`` 的角色即便本集原文正面
提到，也不再进 ``screen_appearance_changes`` 判定、更不会触发
``_refresh_portrait_on_drift`` 的自动付费重绘——不然会把服装焊回常规定妆照。
不测试 ``ensure_cards_for_screenplay`` 其余既有行为（narrative_authority 分支/
新角色建卡等），那些有各自现存测试覆盖。
"""
from __future__ import annotations

import json
import threading

import pytest

from app import config, db
from app.portraits import portrait_drift
from app.schemas import Bible, Character, World


@pytest.fixture
def real_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "drift.db")
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_local", threading.local())
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    db.init_db()
    yield db.get_conn()
    db.get_conn().close()


def _seed_common(conn, project_id: str, name: str, mention: str) -> None:
    conn.execute(
        "INSERT INTO projects(id, name, status, bible_json, bible_version, created_at) "
        "VALUES(?,?,?,?,1,1)", (project_id, "P", "bible_ready", "{}"),
    )
    conn.execute(
        "INSERT INTO chapters(project_id, idx, title, content) VALUES(?,?,?,?)",
        (project_id, 1, "第一章", mention),
    )
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, source_chapters, created_at) "
        "VALUES(?,?,?,?,1)", (f"{project_id}_ep2", project_id, 2, json.dumps([1])),
    )
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, "
        "appearance, image_path, created_at) VALUES(?,?,?,1,NULL,?,?,1)",
        (f"{project_id}_portrait", project_id, name, "原有外观锚点", "/tmp/x.jpg"),
    )
    conn.commit()


def _bible(name: str) -> Bible:
    return Bible(
        world=World(visual_style_canonical="国漫风"),
        characters=[Character(name=name, role="lead", appearance_canonical="原有外观锚点")],
    )


class _Screenplay:
    """``ensure_cards_for_screenplay`` 只用 getattr 访问，最小鸭子类型桩。"""

    def __init__(self, name: str) -> None:
        self.scene_outline = [type("Scene", (), {"characters": [name]})()]


async def test_neutral_costume_mode_skips_drift_screening(real_db, monkeypatch) -> None:
    project_id = "proj_neutral"
    name = "温念"
    _seed_common(real_db, project_id, name, f"{name}换上了一身崭新的红色长裙。")
    real_db.execute("ALTER TABLE character_portraits ADD COLUMN costume_mode TEXT")
    real_db.execute(
        "UPDATE character_portraits SET costume_mode='neutral' WHERE project_id=? AND character_name=?",
        (project_id, name),
    )
    real_db.commit()

    async def boom(*_args, **_kwargs):
        raise AssertionError("neutral 模式角色不应触发 screen_appearance_changes 判定")

    monkeypatch.setattr(portrait_drift, "screen_appearance_changes", boom)

    result = await portrait_drift.ensure_cards_for_screenplay(
        project_id, 2, _Screenplay(name), _bible(name),
    )

    assert result["redrawn"] == []
    assert result["errors"] == []


async def test_baked_costume_mode_still_screens_for_drift(real_db, monkeypatch) -> None:
    project_id = "proj_baked"
    name = "温念"
    _seed_common(real_db, project_id, name, f"{name}换上了一身崭新的红色长裙。")

    calls: list[str] = []

    async def fake_screen(entries, ep_label):
        assert entries and entries[0]["name"] == name
        return {name: {
            "new_appearance": "红裙外观", "reason": "换装", "change_dimensions": ["clothing"],
            "persistence": "persistent", "evidence_excerpt": "换上了一身崭新的红色长裙",
        }}

    async def fake_refresh(*_args, **_kwargs):
        calls.append(name)
        return {"ep_start": 2, "image_path": "/tmp/new.jpg", "pack_status": "ready", "portrait_id": "p2"}

    monkeypatch.setattr(portrait_drift, "screen_appearance_changes", fake_screen)
    monkeypatch.setattr(portrait_drift, "_refresh_portrait_on_drift", fake_refresh)

    result = await portrait_drift.ensure_cards_for_screenplay(
        project_id, 2, _Screenplay(name), _bible(name),
    )

    assert calls == [name]
    assert result["redrawn"] and result["redrawn"][0]["name"] == name
