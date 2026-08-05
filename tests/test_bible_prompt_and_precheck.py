"""提示词规范化与人物谱扩展预检。"""
from __future__ import annotations

import sqlite3

from app.domain import common
from app.refs import normalize_prompt_text, portrait_prompt
from app.orchestration.engine import fingerprint
from app.schemas import Bible, Character, World
from app.validators import validate_bible


def test_normalize_prompt_collapses_duplicate_punctuation() -> None:
    assert ".." not in normalize_prompt_text("戒指。。正面站立")
    assert "。。" not in normalize_prompt_text("戒指。。正面站立")
    assert normalize_prompt_text("戒指。。正面站立") == "戒指。正面站立"


def test_portrait_prompt_uses_normalization() -> None:
    text = portrait_prompt("国风水墨清透光影细腻晕染", "黑发少年。。玄色劲装，目光坚定，身形修长腰佩玉佩")
    assert "。。" not in text
    assert "国风水墨" in text


def test_bible_rejects_identity_traits_hidden_by_normal_clothing() -> None:
    bible = Bible(
        characters=[Character(
            name="角色甲",
            role="主角",
            appearance_canonical=(
                "24岁女性，黑色长发，白色衬衫配深色半身裙，"
                "身形修长，标志性特征是粉色乳头"
            ),
        )],
        world=World(visual_style_canonical="3D动漫CG渲染，虚构数字角色，电影光影"),
    )

    assert any("常规完整着装、中性站姿下静态可见" in error for error in validate_bible(bible))


def test_bible_rejects_behavior_and_subjective_personality_as_appearance() -> None:
    bible = Bible(
        characters=[Character(
            name="角色甲",
            role="反派",
            appearance_canonical=(
                "四十岁男性，短发，深色正装配白衬衫，身材微胖，"
                "标志性特征是眼神猥琐，看人总带着色欲和算计感"
            ),
        )],
        world=World(visual_style_canonical="3D动漫CG渲染，虚构数字角色，电影光影"),
    )

    assert any("常规完整着装、中性站姿下静态可见" in error for error in validate_bible(bible))


def test_project_or_404_normalizes_sqlite_row_to_dict(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects("
        "id TEXT PRIMARY KEY, bible_status TEXT, bible_error TEXT, bible_json TEXT, bible_version INTEGER DEFAULT 0"
        ")"
    )
    conn.execute(
        "INSERT INTO projects(id, bible_status, bible_json, bible_version) VALUES('p1', 'idle', NULL, 0)"
    )
    conn.commit()
    monkeypatch.setattr(common, "get_conn", lambda: conn)

    project = common._project_or_404("p1")

    assert isinstance(project, dict)
    assert project.get("bible_json") is None
    assert project["bible_version"] == 0


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
    assert result["style_name"] == "国漫电影风"


def test_visual_style_options_expose_names_and_descriptions_only(monkeypatch) -> None:
    from app.domain import bible_ops
    import asyncio

    monkeypatch.setattr(bible_ops, "_project_or_404", lambda _pid: {"id": "p1"})

    result = asyncio.run(bible_ops.bible_visual_styles("p1"))

    assert result["default"] == "国漫电影风"
    assert any(
        item["name"] == "现实电影风" and "写实" in item["description"]
        for item in result["items"]
    )
    assert all(set(item) == {"name", "description"} for item in result["items"])


def test_bible_generate_precheck_binds_style_name(monkeypatch) -> None:
    from app.domain import bible_ops

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

    quote = bible_ops._compute_bible_generate_precheck("p1", style_name="现实电影风")

    assert quote["style_name"] == "现实电影风"
    assert quote["quote_id"] == fingerprint({
        "project_id": "p1",
        "action": "generate_bible_and_refs",
        "character_count": 8,
        "image_count": 24,
        "unit": 0.2,
        "bible_version": 0,
        "style_name": "现实电影风",
    })


def test_generate_bible_forces_backend_visual_style_prompt(monkeypatch) -> None:
    from app import stages
    from app.schemas import Bible, Character, World
    import asyncio

    seen = {}

    async def fake_loop(*_args, **_kwargs):
        seen["allow_warning_candidate"] = _kwargs["loop"].policy.allow_warning_candidate
        seen["repair_all_blockers"] = _kwargs["loop"].policy.repair_all_blockers
        return Bible(
            world=World(visual_style_canonical="模型自行写的画风"),
            characters=[
                Character(
                    name="孟浩",
                    role="主角",
                    appearance_canonical="黑发少年，青色长衫，目光沉稳，身形清瘦，腰间系旧布袋",
                ),
            ],
        )

    monkeypatch.setattr(stages, "_run_with_agent_loop", fake_loop)

    result = asyncio.run(stages.generate_bible(
        [{"idx": 1, "title": "第一章", "content": "孟浩走入山中。"}],
        visual_style_prompt="电影级真实质感，现实人物建模，自然光影，细节丰富，东方仙侠风。",
    ))

    assert result.world.visual_style_canonical == (
        "电影级真实质感，现实人物建模，自然光影，细节丰富，东方仙侠风。"
    )
    assert seen["allow_warning_candidate"] is False
    assert seen["repair_all_blockers"] is True
