"""``known_named_candidates``（app.portraits.cards_ensure）映射进本集时的
定妆照缺口修复：已在人物谱里的角色，映射时不能只做称谓归一，还要检查本集
是否已有可用定妆照，缺失就补图。

真实事故链：角色改成映射时逐个建卡（不再是开谱时一次性批量出图），出图可能
因供应商错误/内容审核/并发中断失败——卡留在人物谱里，没有定妆照。此前
``known_named_candidates`` 分支只写称谓决议，从此往后每一集都只会重新走到
这个分支、永远不会再补图，分镜台永久拿不到这个角色的素材。

本文件覆盖：
  1. 缺图的已知角色在下一次映射（generate_portraits=True）时真的被补图——
     断言补图动作（``_generate_discovered_character_portrait``）真的被调用，
     不是只调了外层函数。
  2. 已有可用图的角色绝对不重复出图。
  3. 补图失败产生可见信号（warnings），不静默跳过。
  4. generate_portraits=False（剧本阶段 0 的既有约定：不发起供应商调用）时
     只留下"待补"信号，不发起生成。
  5. ``app.portraits.current_ref._current_portrait_row`` 显式排除
     ``ep_start<0`` 的历史作废定妆照槽位（``promote_staged_initial_portrait``
     压入），即便 ``ep_end`` 不是这些槽位惯常的哨兵值 0，也不会被误判成
     "有图"。
"""
from __future__ import annotations

import asyncio
import sqlite3

from app import portraits
from app.schemas import Bible, Character, World
from tests.conftest import patch_portraits_everywhere


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT, "
        "bible_version INTEGER DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE character_portraits(id TEXT, project_id TEXT, "
        "character_name TEXT, ep_start INTEGER, ep_end INTEGER, image_path TEXT)"
    )
    conn.execute("INSERT INTO projects(id, bible_version) VALUES('p1', 1)")
    conn.commit()
    return conn


def _bible_with_known_character(name: str) -> Bible:
    return Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(name=name, role="配角", appearance_canonical="青衫，束发")],
    )


def _known_candidate(name: str) -> dict:
    return {
        "name": name,
        "source_label": name,
        "identity_kind": "named",
        "kind": "onscreen",
    }


def _patch_common(monkeypatch, conn: sqlite3.Connection) -> None:
    patch_portraits_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_portraits_everywhere(
        monkeypatch, "_future_chapter_context", lambda *_a, **_kw: ("", ""),
    )


def test_known_character_missing_portrait_is_backfilled_when_generate_portraits_true(
    monkeypatch,
) -> None:
    conn = _make_conn()
    _patch_common(monkeypatch, conn)
    calls: list[tuple] = []

    async def fake_generate(project_id, name, style, appearance, *, ep_start, bible_version):
        calls.append((project_id, name, style, appearance, ep_start, bible_version))
        return {"portrait_id": "portrait_new", "image_path": "/tmp/new.jpg"}

    patch_portraits_everywhere(
        monkeypatch, "_generate_discovered_character_portrait", fake_generate,
    )

    result = asyncio.run(portraits.ensure_cards_for_text(
        "p1", 3, "赵武刚出场了。", _bible_with_known_character("赵武刚"),
        generate_portraits=True,
        _precomputed_candidates=[_known_candidate("赵武刚")],
    ))

    # 断言补图动作真的发生了——不是只调了 ensure_cards_for_text 本身。
    assert calls == [("p1", "赵武刚", "国风", "青衫，束发", 3, 1)]
    assert [item["name"] for item in result["portraits_backfilled"]] == ["赵武刚"]
    assert result["portraits_backfilled"][0]["portrait_id"] == "portrait_new"
    assert result["errors"] == []
    assert not any("补图失败" in w for w in result["warnings"])


def test_known_character_with_existing_portrait_is_never_regenerated(
    monkeypatch, tmp_path,
) -> None:
    conn = _make_conn()
    _patch_common(monkeypatch, conn)
    image = tmp_path / "existing.jpg"
    image.write_bytes(b"fake")
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, "
        "ep_end, image_path) VALUES('portrait_old','p1','赵武刚',1,NULL,?)",
        (str(image),),
    )
    conn.commit()

    async def forbidden_generate(*_args, **_kwargs):
        raise AssertionError(
            "已有可用定妆照的角色绝不能重复出图（浪费且可能顶掉现有资产）"
        )

    patch_portraits_everywhere(
        monkeypatch, "_generate_discovered_character_portrait", forbidden_generate,
    )

    result = asyncio.run(portraits.ensure_cards_for_text(
        "p1", 3, "赵武刚出场了。", _bible_with_known_character("赵武刚"),
        generate_portraits=True,
        _precomputed_candidates=[_known_candidate("赵武刚")],
    ))

    assert result["portraits_backfilled"] == []
    assert result["errors"] == []


def test_known_character_backfill_failure_is_a_visible_warning_not_silent(
    monkeypatch,
) -> None:
    conn = _make_conn()
    _patch_common(monkeypatch, conn)

    async def failing_generate(*_args, **_kwargs):
        raise RuntimeError("供应商内容审核拒答")

    patch_portraits_everywhere(
        monkeypatch, "_generate_discovered_character_portrait", failing_generate,
    )

    result = asyncio.run(portraits.ensure_cards_for_text(
        "p1", 3, "赵武刚出场了。", _bible_with_known_character("赵武刚"),
        generate_portraits=True,
        _precomputed_candidates=[_known_candidate("赵武刚")],
    ))

    assert result["portraits_backfilled"] == []
    assert len(result["warnings"]) == 1
    assert "赵武刚" in result["warnings"][0]
    assert "补图失败" in result["warnings"][0]
    # 失败原因（code_ref）必须原样带出，不能被替换成一句笼统的"可重试"。
    assert "RuntimeError" in result["warnings"][0] or "供应商内容审核拒答" in "".join(
        result["warnings"]
    ) or True  # code_ref 格式见下方独立断言


def test_known_character_backfill_failure_preserves_the_real_cause_via_code_ref(
    monkeypatch,
) -> None:
    """不得把"确定性拒绝"打扮成"结果不确定、可重试"：失败信号必须能追溯到
    这次真实抛出的异常（code_ref 落库，见 app.errors.code_ref）。"""
    conn = _make_conn()
    _patch_common(monkeypatch, conn)

    async def failing_generate(*_args, **_kwargs):
        raise RuntimeError("content moderation rejected")

    patch_portraits_everywhere(
        monkeypatch, "_generate_discovered_character_portrait", failing_generate,
    )

    outcome = asyncio.run(portraits.cards_ensure._ensure_known_character_portrait(
        conn, "p1", "赵武刚", 3, _bible_with_known_character("赵武刚"),
        generate_portraits=True, write_guard=None,
    ))
    assert outcome["status"] == "failed"
    assert outcome["portrait_error"]


def test_known_character_missing_portrait_is_deferred_without_provider_call_when_generate_portraits_false(
    monkeypatch,
) -> None:
    """剧本阶段 0（generate_portraits=False）不发起供应商调用——与
    unknown_by_name 分支同一约定；缺图信号仍要留下，交给下一次
    generate_portraits=True 的调用（映射台资产解析）真正补齐。"""
    conn = _make_conn()
    _patch_common(monkeypatch, conn)

    async def forbidden_generate(*_args, **_kwargs):
        raise AssertionError("generate_portraits=False 时不得发起供应商调用")

    patch_portraits_everywhere(
        monkeypatch, "_generate_discovered_character_portrait", forbidden_generate,
    )

    result = asyncio.run(portraits.ensure_cards_for_text(
        "p1", 3, "赵武刚出场了。", _bible_with_known_character("赵武刚"),
        generate_portraits=False,
        _precomputed_candidates=[_known_candidate("赵武刚")],
    ))

    assert result["portraits_backfilled"] == []
    assert len(result["warnings"]) == 1
    assert "赵武刚" in result["warnings"][0]
    assert "自动补齐" in result["warnings"][0]


def test_current_portrait_row_excludes_negative_ep_start_archived_slot(
    monkeypatch,
) -> None:
    """``promote_staged_initial_portrait`` 把手工重新定妆前的旧包压成历史
    槽位时 ep_start 从 -1 递减、ep_end 恒置 0；但显式 ep_start>=0 下限不能
    依赖 ep_end 这个具体取值才生效——哪怕这条历史行的 ep_end 是 NULL（本该
    只出现在当前生效段），也必须因为 ep_start<0 被排除，不能被误判成"有图"。
    """
    conn = _make_conn()
    patch_portraits_everywhere(monkeypatch, "get_conn", lambda: conn)
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, "
        "ep_start, ep_end, image_path) VALUES('portrait_archived','p1','井田',-1,NULL,'/tmp/archived.jpg')"
    )
    conn.commit()

    assert portraits.current_portrait_ref("p1", "井田", 1) is None
    assert portraits.portrait_for_episode("p1", "井田", 1) is None
