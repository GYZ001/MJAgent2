"""``_refs_task`` 成功路径接到的声音自动生成钩子（角色固定音色 U1）。

不重复测 ``app.voice.service.trigger_auto_generate_after_portrait`` 自己的
三种分支（已配置/未配置/开关关闭，见 ``tests/test_voice_service.py``）——这里
只钉住**接线本身**：``_refs_task`` 成功结束时确实调用了这个钩子，且传的是
本批次计算出的角色名单，不是全量或空名单。写法与
``tests/test_refs_status_produced_signal.py`` 同一套 monkeypatch 约定
（``app.refs.generate_refs`` 走局部 import，patch 源模块属性即可）。
"""
from __future__ import annotations

import asyncio
import json

import app.refs as refs_module
from app.db import get_conn, new_id, now
from app.domain.bible_ops.refs_generation import _refs_task
from app.multiview import CHARACTER_REQUIRED_VIEWS
from app.voice import service as voice_service


def _make_project_with_bible(names: list[str]) -> str:
    conn = get_conn()
    project_id = new_id("proj")
    bible_json = json.dumps({
        "characters": [
            {"name": n, "role": "配角", "appearance_canonical": f"{n}占位外观"}
            for n in names
        ],
    }, ensure_ascii=False)
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at, bible_json) VALUES(?,?,?,?,?)",
        (project_id, "P", "created", now(), bible_json),
    )
    conn.commit()
    return project_id


def _insert_ready_pack(conn, project_id: str, name: str) -> None:
    portrait_id = new_id("portrait")
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, "
        "appearance, prompt, image_path, pack_status, created_at) "
        "VALUES(?,?,?,1,NULL,?,?,?,?,?)",
        (portrait_id, project_id, name, f"{name}外观", "prompt", "/tmp/x.jpg", "ready", now()),
    )
    for role in CHARACTER_REQUIRED_VIEWS:
        conn.execute(
            "INSERT INTO character_portrait_views(id, portrait_id, view_role, image_path, "
            "status, created_at) VALUES(?,?,?,?,?,?)",
            (f"{portrait_id}-{role}", portrait_id, role, "/tmp/x.jpg", "ready", now()),
        )
    conn.commit()


def test_refs_task_success_triggers_voice_auto_generate_with_batch_names(monkeypatch) -> None:
    project_id = _make_project_with_bible(["甲一", "乙二"])
    captured: list[tuple[str, list[str]]] = []

    async def fake_generate_refs(pid, *_a, **_kw):
        conn = get_conn()
        _insert_ready_pack(conn, pid, "甲一")
        _insert_ready_pack(conn, pid, "乙二")

    monkeypatch.setattr(refs_module, "generate_refs", fake_generate_refs)
    monkeypatch.setattr(
        voice_service, "trigger_auto_generate_after_portrait",
        lambda pid, names, **_kw: captured.append((pid, list(names))),
    )

    asyncio.run(_refs_task(project_id, None))

    assert len(captured) == 1
    called_project_id, called_names = captured[0]
    assert called_project_id == project_id
    assert set(called_names) == {"甲一", "乙二"}


def test_refs_task_partial_failure_still_triggers_hook_for_batch_names(monkeypatch) -> None:
    """定妆批次里有角色仍缺图（refs_status=warning）时，钩子依然按"本次定妆
    涉及"的名单触发——是否给这批角色配声音，跟这批定妆是否全部齐整是两件事，
    不能因为整体状态是 warning 就连已经出图成功的角色也不生成声音。"""
    project_id = _make_project_with_bible(["甲一", "乙二", "丙三"])
    captured: list[tuple[str, list[str]]] = []

    async def fake_generate_refs(pid, *_a, **_kw):
        _insert_ready_pack(get_conn(), pid, "甲一")  # 乙二/丙三 本批次没落盘

    monkeypatch.setattr(refs_module, "generate_refs", fake_generate_refs)
    monkeypatch.setattr(
        voice_service, "trigger_auto_generate_after_portrait",
        lambda pid, names, **_kw: captured.append((pid, list(names))),
    )

    asyncio.run(_refs_task(project_id, None))

    row = get_conn().execute("SELECT refs_status FROM projects WHERE id=?", (project_id,)).fetchone()
    assert row["refs_status"] == "warning"
    assert len(captured) == 1
    assert set(captured[0][1]) == {"甲一", "乙二", "丙三"}  # 本批次原计划名单，不是"缺口"名单
