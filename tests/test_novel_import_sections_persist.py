"""WS10-C 端到端回归：小节边界（``sections``）导入落库后必须在后续的 paratext
偏移计算中原样保留，不被覆盖。

背景（WS1 派单遗留项）：``app.novel.structure._extract_sections`` 早就算出了
``chapter["sections"]``（一/二/三…小节边界），``app.ingest.ingest_novel`` 把它
装进每章的 ``paratext_json``；但两处消费方各自有缺陷：

- ``app.domain.projects.create._create_project_core`` 的 ``INSERT INTO
  chapters`` 语句此前没有 ``paratext_json`` 列，小节信息落库即丢；
- ``app.source_paratext.chapter_paratext_offsets`` 事后再算一次 paratext
  偏移（求收藏/推荐票之类的旁文本）时，把整个 ``paratext_json`` 列整体覆盖，
  第二次也会把刚补上的 ``sections`` 冲掉。

本文件只测这条端到端链路本身：先导入（真实走 ``_create_project_core``，
真实 schema），确认 ``sections`` 落库；再跑一次 ``chapter_paratext_offsets``，
确认 ``sections`` 仍然在——单独成文件是因为 ``tests/test_novel_import_
approval.py`` 与 ``tests/test_source_paratext.py`` 都已经在 500 行严格上限
附近，装不下这条端到端集成测试（新增测试文件按 500 行严格执行，不进
``[baseline.*]`` 棘轮）。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import api, db
from app import source_paratext
from app.harness import model_gateway
from app.source_paratext import ParatextSpans

_NOVEL_WITH_SECTIONS = (
    "第一章 相遇\n"
    "一\n\n"
    "少年在夜雨中推开院门，屋内灯火摇曳，桌上摊着一封未拆的信。\n\n"
    "二\n\n"
    "他将信件放在桌上，转身离开，脚步声渐渐消失在雨里。\n"
)


@pytest.fixture
def project_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "novel-sections.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    return db.get_conn()


def test_import_persists_sections_and_offsets_merge_not_overwrite(project_db, monkeypatch) -> None:
    created = api._create_project_core(None, "story.txt", _NOVEL_WITH_SECTIONS.encode("utf-8"))

    row = project_db.execute(
        "SELECT id, content, paratext_json FROM chapters WHERE project_id=? ORDER BY idx",
        (created["project_id"],),
    ).fetchone()
    assert row is not None
    imported_payload = json.loads(row["paratext_json"])
    assert [s["label"] for s in imported_payload["sections"]] == ["一", "二"]

    async def fake_chat_structured(_messages, **_kwargs):
        return ParatextSpans(spans=[])

    monkeypatch.setattr(model_gateway, "chat_structured", fake_chat_structured)
    source_paratext.paratext_cache_clear()

    asyncio.run(source_paratext.chapter_paratext_offsets(
        project_db, dict(row), operation_id="op_test_sections_survive_offsets",
    ))

    persisted = json.loads(project_db.execute(
        "SELECT paratext_json FROM chapters WHERE id=?", (row["id"],),
    ).fetchone()["paratext_json"])
    assert [s["label"] for s in persisted["sections"]] == ["一", "二"]  # 合并写入后 sections 仍在
    assert "content_hash" in persisted and "spans" in persisted  # 自己的两个键也正常写入
