"""app.multiview.regenerate_character_view：中性定妆照角色的单视角重做守卫
（代码评审确认的 major 缺陷补测，2026-09-30）。

背景：``regenerate_character_view``（人物谱「单视角重做」的唯一入口）此前完全
不读 ``character_portraits.costume_mode``，对 ``costume_mode="neutral"`` 的
角色重做任意一个视角都会静默套回常规着装合同（``effective_portrait_prompt``/
``character_view_prompt`` 默认 ``costume_mode="baked"``），悄悄撤销用户刚
采纳的中性定妆效果且没有任何报错信号。真正支持中性分支的单视角重做不在本次
范围内（见 ``app.portraits.neutral_identity`` 模块 docstring），这里只补
CLAUDE.md「缺失要有可见信号」要求的最小安全网：命中时明确报错，不静默降级。
"""
from __future__ import annotations

import threading

import pytest

from app import config, db, hiagent, multiview


@pytest.fixture
def real_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "regen-neutral-guard.db")
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_local", threading.local())
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    db.init_db()
    yield db.get_conn()
    db.get_conn().close()


def _seed_portrait(conn, *, project_id: str, portrait_id: str, costume_mode: str | None) -> None:
    conn.execute(
        "INSERT INTO projects(id, name, status, bible_json, bible_version, created_at) "
        "VALUES(?,?,?,?,1,1)", (project_id, "P", "bible_ready", "{}"),
    )
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, "
        "appearance, image_path, created_at) VALUES(?,?,?,1,NULL,?,?,1)",
        (portrait_id, project_id, "温念", "原有外观锚点", "/tmp/x.jpg"),
    )
    if costume_mode is not None:
        conn.execute("ALTER TABLE character_portraits ADD COLUMN costume_mode TEXT NOT NULL DEFAULT 'baked'")
        conn.execute(
            "UPDATE character_portraits SET costume_mode=? WHERE id=?", (costume_mode, portrait_id),
        )
    conn.commit()


@pytest.mark.asyncio
async def test_regenerate_character_view_rejects_neutral_costume_mode(real_db) -> None:
    _seed_portrait(real_db, project_id="proj1", portrait_id="portrait1", costume_mode="neutral")

    with pytest.raises(hiagent.ProviderError, match="中性定妆照"):
        await multiview.regenerate_character_view(
            project_id="proj1", portrait_id="portrait1", view_role="front_full",
        )


@pytest.mark.asyncio
async def test_regenerate_character_view_allows_baked_costume_mode(real_db, monkeypatch) -> None:
    """非中性（含老数据没有 costume_mode 列）行为不受影响——只到达守卫之后的
    第一步真实生成调用即可证明没被新增的守卫拦下，不需要跑完整个重做流程。"""
    _seed_portrait(real_db, project_id="proj1", portrait_id="portrait1", costume_mode=None)
    called = {"count": 0}

    async def _fake_generate_image(*_args, **_kwargs):
        called["count"] += 1
        raise RuntimeError("stop-after-guard")

    monkeypatch.setattr(multiview, "_generate_image", _fake_generate_image)

    with pytest.raises(RuntimeError, match="stop-after-guard"):
        await multiview.regenerate_character_view(
            project_id="proj1", portrait_id="portrait1", view_role="front_full",
        )
    assert called["count"] == 1
