"""提示词规范化与人物谱扩展预检。"""
from __future__ import annotations

import json
import sqlite3

from app.refs import normalize_prompt_text, portrait_prompt


def test_normalize_prompt_collapses_duplicate_punctuation() -> None:
    assert ".." not in normalize_prompt_text("戒指。。正面站立")
    assert "。。" not in normalize_prompt_text("戒指。。正面站立")
    assert normalize_prompt_text("戒指。。正面站立") == "戒指。正面站立"


def test_portrait_prompt_uses_normalization() -> None:
    text = portrait_prompt("国风水墨清透光影细腻晕染", "黑发少年。。玄色劲装，目光坚定，身形修长腰佩玉佩")
    assert "。。" not in text
    assert "国风水墨" in text


def test_generate_precheck_estimates_without_bible(monkeypatch) -> None:
    from app.domain import bible_ops
    import asyncio

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER DEFAULT 0)"
    )
    conn.execute("INSERT INTO projects(id, bible_json, bible_version) VALUES('p1', NULL, 0)")
    conn.commit()
    monkeypatch.setattr(bible_ops, "get_conn", lambda: conn)
    monkeypatch.setattr(bible_ops, "_project_or_404", lambda _pid: dict(conn.execute(
        "SELECT * FROM projects WHERE id='p1'"
    ).fetchone()))

    result = asyncio.run(bible_ops.bible_generate_precheck("p1"))
    assert result["character_count"] == 8
    assert result["image_count"] == 24
    assert result["estimated_cost_cny"] == 4.8
