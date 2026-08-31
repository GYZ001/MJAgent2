"""refs_status='ready' 必须挂在产物信号上，不能挂在过程信号上。

实战撞到：《我欲封天》换画风后 bible_json 里 5 个角色只有 1 个真的有完整
定妆包，但 refs_status 仍报 ready——`_refs_task` 此前只要 recorder.step()
（内部的 generate_refs 调用）没抛异常就无条件把 refs_status 置为 ready，
没有检查人物谱里每个具备定妆资格的角色是否真的都有图（CLAUDE.md
「Gates and Criteria」：判据必须挂在「这件事本身成没成」上）。

本文件在 _refs_task 这一层（而不是只测被抽出来的判据函数）钉住修复：
generate_refs 只把部分角色的包落到 character_portraits 时，refs_status 必须
报 warning 并在 refs_error 里点名缺口角色；全部角色都有完整包时才是 ready。
"""
from __future__ import annotations

import asyncio
import json

import app.refs as refs_module
from app.db import get_conn, new_id, now
from app.domain.bible_ops.refs_generation import _refs_task
from app.multiview import CHARACTER_REQUIRED_VIEWS


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
        "INSERT INTO projects(id, name, status, created_at, bible_json) "
        "VALUES(?,?,?,?,?)",
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


def test_refs_task_reports_warning_when_some_eligible_characters_still_lack_a_pack(monkeypatch) -> None:
    project_id = _make_project_with_bible(["甲一", "乙二", "丙三"])

    async def fake_generate_refs(pid, *_a, **_kw):
        # 模拟批次只把「甲一」真正落盘成功（供应商失败/并发中断等常见成因），
        # 其余两个角色一个记录都没留下——这正是 _purge_for_style_change 清空
        # 全表后、established-gap 名单口径漏算的场景。
        _insert_ready_pack(get_conn(), pid, "甲一")

    monkeypatch.setattr(refs_module, "generate_refs", fake_generate_refs)

    asyncio.run(_refs_task(project_id, None))

    row = get_conn().execute(
        "SELECT refs_status, refs_error FROM projects WHERE id=?", (project_id,)
    ).fetchone()
    assert row["refs_status"] == "warning"
    assert "乙二" in row["refs_error"]
    assert "丙三" in row["refs_error"]
    assert "甲一" not in row["refs_error"]


def test_refs_task_reports_ready_only_when_every_eligible_character_has_a_pack(monkeypatch) -> None:
    project_id = _make_project_with_bible(["甲一", "乙二"])

    async def fake_generate_refs(pid, *_a, **_kw):
        conn = get_conn()
        _insert_ready_pack(conn, pid, "甲一")
        _insert_ready_pack(conn, pid, "乙二")

    monkeypatch.setattr(refs_module, "generate_refs", fake_generate_refs)

    asyncio.run(_refs_task(project_id, None))

    row = get_conn().execute(
        "SELECT refs_status, refs_error FROM projects WHERE id=?", (project_id,)
    ).fetchone()
    assert row["refs_status"] == "ready"
    assert row["refs_error"] is None
