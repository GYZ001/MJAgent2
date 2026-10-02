"""compute_refs_precheck 单视角重做分支的真实出图数（2026-10-02）。

face_closeup 单视角重做已改为从 front_full 纯像素裁切
（app.portraits.headshot_crop），不调用生图模型；预检必须如实报告
image_count=0，不能沿用「单视角重做恒为 1」的旧口径糊弄过去。其它视角单独
重做仍是一次真实生图调用，image_count 不变。独立成文件只是为了不挤占
test_bible_impact_and_concurrency.py 已经顶格的 line_count 棘轮基线。
"""
from __future__ import annotations

import json
import sqlite3

from app.domain import bible_ops
from tests.conftest import patch_api_everywhere


def _memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER DEFAULT 0)"
    )
    return conn


def _seed_bible(conn: sqlite3.Connection) -> None:
    bible = {
        "world": {"visual_style_canonical": "国风水墨清透光影，细腻线条与柔和晕染"},
        "characters": [{
            "name": "甲一", "role": "主角",
            "appearance_canonical": "黑发少年，玄色劲装，目光坚定，身形修长，腰间佩火纹玉佩，英气逼人",
        }],
        "scenes": [],
    }
    conn.execute(
        "INSERT INTO projects(id, bible_json, bible_version) VALUES('proj_test', ?, 1)",
        (json.dumps(bible, ensure_ascii=False),),
    )
    conn.commit()


def test_refs_precheck_single_view_face_closeup_costs_zero_images(monkeypatch) -> None:
    conn = _memory_conn()
    _seed_bible(conn)
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "_project_or_404", lambda _pid: dict(conn.execute(
        "SELECT * FROM projects WHERE id='proj_test'"
    ).fetchone()))

    face_quote = bible_ops.compute_refs_precheck("proj_test", character="甲一", view_role="face_closeup")
    front_quote = bible_ops.compute_refs_precheck("proj_test", character="甲一", view_role="front_full")

    assert face_quote["image_count"] == 0
    assert face_quote["scope"][0]["reason"] == "头像照由定妆照裁切，不消耗生图"
    assert front_quote["image_count"] == 1
    assert front_quote["scope"][0]["reason"] == "单视角重做"
