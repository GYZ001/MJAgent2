"""稳定称谓建卡：真名揭晓之前先把视觉身份钉住（判据见模块 docstring）。"""
from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass

import pytest

from app import db
from app.production.prep_pack import persistent_appellation as pa
from tests.conftest import patch_portraits_everywhere, patch_prep_pack_everywhere


@pytest.fixture(autouse=True)
def _same_person_by_default(monkeypatch):
    """跨章同一人核验默认判 true（各用例只测建卡/绑定本身）；否决用例自行覆盖。"""
    async def _yes(label, contexts, *, project_id, episode_id):
        return True

    patch_prep_pack_everywhere(monkeypatch, "appellation_denotes_one_person", _yes)


@dataclass
class _Seg:
    text: str


def _conn(chapter_texts: list[str]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for statement in db.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    for idx, text in enumerate(chapter_texts, start=1):
        conn.execute(
            "INSERT INTO chapters(project_id,idx,title,content) VALUES('p',?,?,?)",
            (idx, f"第{idx}章", text),
        )
    conn.commit()
    return conn


def _run(coro):
    return asyncio.run(coro)


def test_chapter_span_counts_literal_containment_only() -> None:
    conn = _conn(["许师姐来了", "无关", "许师姐又来了", "许师姐第三次"])

    assert pa.label_chapter_span(conn, "p", "许师姐") == 3
    assert pa.label_chapter_span(conn, "p", "虎头虎脑的少年") == 0
    assert pa.label_chapter_span(conn, "p", "") == 0


def test_recurring_appellation_gets_its_own_card(monkeypatch) -> None:
    """跨 3 章的称谓建卡出图，卡名就是这个称谓——不等真名。"""
    conn = _conn(["许师姐来了", "许师姐又来了", "许师姐第三次", "无关"])
    calls: list[dict] = []

    async def fake_card(project_id, name, episode_no, **kwargs):
        calls.append({"name": name, "kwargs": kwargs})
        return {"status": "added", "name": name}

    patch_portraits_everywhere(monkeypatch, "ensure_character_card", fake_card)
    patch_prep_pack_everywhere(
        monkeypatch, "_resolve_portrait_id", lambda *a, **k: "portrait_x",
    )

    result = _run(pa.resolve_persistent_appellation(
        conn, project_id="p", episode_no=1, label="许师姐",
        segments=[_Seg("旁白"), _Seg("许师姐走了进来")],
    ))

    assert result is not None
    assert result["resolved"] is True
    assert result["canonical_name"] == "许师姐"
    assert result["segment_index"] == 2
    assert result["text"] == "许师姐走了进来"
    # 跨章复现已是"稳定身份"的结构证据，不得再被判成戏份少的路人。
    assert calls[0]["kwargs"]["require_identity_card"] is True
    # 出图已解耦到后台：映射阶段只建卡，不在这里等图。
    assert calls[0]["kwargs"]["generate_portrait"] is False


def test_two_chapter_label_stays_a_functional_extra(monkeypatch) -> None:
    """用户定的口径是"大于 2 章"：正好 2 章仍然是群演。"""
    conn = _conn(["虎头虎脑的少年", "无关", "虎头虎脑的少年又出现"])

    async def fake_card(*a, **k):  # pragma: no cover - 不应被调用
        raise AssertionError("2 章的标签不该触发建卡")

    patch_portraits_everywhere(monkeypatch, "ensure_character_card", fake_card)

    result = _run(pa.resolve_persistent_appellation(
        conn, project_id="p", episode_no=1, label="虎头虎脑的少年",
        segments=[_Seg("虎头虎脑的少年")],
    ))

    assert result is None


def test_label_without_literal_anchor_in_episode_is_not_carded(monkeypatch) -> None:
    """本集钉不住锚点（标签是模型转述的短语）就不建卡——不确定不绑。"""
    conn = _conn(["银袍女子", "银袍女子", "银袍女子"])

    async def fake_card(*a, **k):  # pragma: no cover - 不应被调用
        raise AssertionError("钉不住锚点不该建卡")

    patch_portraits_everywhere(monkeypatch, "ensure_character_card", fake_card)

    result = _run(pa.resolve_persistent_appellation(
        conn, project_id="p", episode_no=1, label="银袍女子",
        segments=[_Seg("一名女子走来")],
    ))

    assert result is None


def test_later_appellation_binds_back_to_the_original_card(monkeypatch) -> None:
    """后来的代称必须绑回最初那张卡：canonical_name 取归属者规范名，不是标签本身。

    ensure_character_card 内部的 resolve_card_build_or_merge 先问"这个称呼是不是
    人物谱里已有角色的另一种叫法"，是则登记别名、复用既有卡并回 status=exists +
    归属者的规范名。下游据此把标签改名到归属者，自然拿到那张卡的定妆照——同一个
    人从第一集到真名揭晓用的一直是同一张图。
    """
    conn = _conn(["许姑娘", "许姑娘", "许姑娘"])

    async def fake_card(project_id, name, episode_no, **kwargs):
        return {"status": "exists", "name": "许师姐"}

    patch_portraits_everywhere(monkeypatch, "ensure_character_card", fake_card)

    result = _run(pa.resolve_persistent_appellation(
        conn, project_id="p", episode_no=3, label="许姑娘",
        segments=[_Seg("许姑娘点了点头")],
    ))

    assert result["canonical_name"] == "许师姐"
    assert result["resolved"] is True


def test_card_without_portrait_still_binds(monkeypatch) -> None:
    """出图解耦到后台后，刚建的卡此刻必然没图；绑定只看建卡结果，不看定妆照。

    真实事故（2026-09-14 第 11 集）：「大汉」建成「曹阳（大汉）」（status=added、
    无图），旧实现在这里查 _resolve_portrait_id 拿到 None 就返回 None，标签落
    群演、卡进不了准备包，分镜前资产准备也永远轮不到给它补图。
    """
    conn = _conn(["大汉来了", "大汉又来了", "大汉第三次"])

    async def fake_card(project_id, name, episode_no, **kwargs):
        assert kwargs["generate_portrait"] is False
        return {"status": "added", "name": "曹阳", "has_portrait": False, "portrait_deferred": True}

    patch_portraits_everywhere(monkeypatch, "ensure_character_card", fake_card)
    patch_prep_pack_everywhere(monkeypatch, "_resolve_portrait_id", lambda *a, **k: None)

    result = _run(pa.resolve_persistent_appellation(
        conn, project_id="p", episode_no=1, label="大汉",
        segments=[_Seg("身后大汉手中一把飞剑")],
    ))

    assert result is not None
    assert result["canonical_name"] == "曹阳"
    assert result["persistent_appellation"] is True


def test_conflicting_owner_is_not_carded(monkeypatch) -> None:
    """同一称呼命中多个角色是真实存在的合法数据，猜一个就会制造错误归属。"""
    conn = _conn(["大汉", "大汉", "大汉"])

    async def fake_card(*a, **k):
        return {"status": "conflict", "name": "大汉", "owners": ["曹阳", "虎爷"]}

    patch_portraits_everywhere(monkeypatch, "ensure_character_card", fake_card)

    result = _run(pa.resolve_persistent_appellation(
        conn, project_id="p", episode_no=1, label="大汉",
        segments=[_Seg("大汉挡在门口")],
    ))

    assert result is None


def test_wrapper_skips_carding_when_the_verdict_already_bound(monkeypatch) -> None:
    """第一级绑上了就不该再建第二张卡——一人一卡。"""
    from app.production.prep_pack import functional_candidate_verdict as fcv

    async def fake_verdict(*a, **k):
        return {"resolved": True, "attempted": True, "canonical_name": "许清",
                "segment_index": 1, "text": "许清"}

    async def fake_card(*a, **k):  # pragma: no cover - 不应被调用
        raise AssertionError("第一级已绑定，不该再建卡")

    monkeypatch.setattr(fcv, "_prep_pack_functional_candidate_verdict_only", fake_verdict)
    monkeypatch.setattr(fcv, "resolve_persistent_appellation", fake_card)

    result = _run(fcv._prep_pack_resolve_functional_extra_candidate(
        None, project_id="p", episode_id="e", episode_no=1, label="许师姐",
        source_text="", segments=[], bible=None, character_mentions=[],
    ))

    assert result["canonical_name"] == "许清"


def test_wrapper_falls_through_to_carding_when_unbound(monkeypatch) -> None:
    """第一级没绑上才轮到第二级，且保留 attempted 供可观测性区分两种情形。"""
    from app.production.prep_pack import functional_candidate_verdict as fcv

    async def fake_verdict(*a, **k):
        return {"resolved": False, "attempted": True}

    async def fake_card(*a, **k):
        return {"resolved": True, "canonical_name": "许师姐",
                "persistent_appellation": True, "segment_index": 2, "text": "许师姐"}

    monkeypatch.setattr(fcv, "_prep_pack_functional_candidate_verdict_only", fake_verdict)
    monkeypatch.setattr(fcv, "resolve_persistent_appellation", fake_card)

    result = _run(fcv._prep_pack_resolve_functional_extra_candidate(
        None, project_id="p", episode_id="e", episode_no=1, label="许师姐",
        source_text="", segments=[], bible=None, character_mentions=[],
    ))

    assert result["resolved"] is True
    assert result["attempted"] is True
    assert result["canonical_name"] == "许师姐"
    assert result["persistent_appellation"] is True


def test_wrapper_keeps_extra_when_both_levels_decline(monkeypatch) -> None:
    from app.production.prep_pack import functional_candidate_verdict as fcv

    async def fake_verdict(*a, **k):
        return {"resolved": False, "attempted": True}

    async def fake_card(*a, **k):
        return None

    monkeypatch.setattr(fcv, "_prep_pack_functional_candidate_verdict_only", fake_verdict)
    monkeypatch.setattr(fcv, "resolve_persistent_appellation", fake_card)

    result = _run(fcv._prep_pack_resolve_functional_extra_candidate(
        None, project_id="p", episode_id="e", episode_no=1, label="绿袍男子",
        source_text="", segments=[], bible=None, character_mentions=[],
    ))

    assert result == {"resolved": False, "attempted": True}



def test_generic_role_phrase_denoting_different_people_is_not_carded(monkeypatch) -> None:
    """「受伤的修士」跨 3 章逐字命中，但模型按片段判定不是同一个人 → 不建卡、不绑定（2026-09-14 第 11/12 集实测）。"""
    conn = _conn(["受伤的修士右肩流血", "受伤的修士手臂被划开", "受伤的修士倒地"])
    calls: list[str] = []

    async def fake_card(project_id, name, episode_no, **kwargs):
        calls.append(name)
        return {"status": "added", "name": name}

    async def _no(label, contexts, *, project_id, episode_id):
        assert label == "受伤的修士" and len(contexts) == 3 and episode_id == "ep_x"
        return False

    patch_portraits_everywhere(monkeypatch, "ensure_character_card", fake_card)
    patch_prep_pack_everywhere(monkeypatch, "appellation_denotes_one_person", _no)
    result = _run(pa.resolve_persistent_appellation(
        conn, project_id="p", episode_no=2, label="受伤的修士", segments=[_Seg("受伤的修士手臂被划开")], episode_id="ep_x",
    ))
    assert result is None and calls == []


def test_chapter_contexts_take_first_hit_per_chapter_with_window() -> None:
    conn = _conn(["无关。" * 30 + "许师姐来了。" + "后文。" * 30, "无关", "许师姐又来了"])
    contexts = pa.label_chapter_contexts(conn, "p", "许师姐")
    assert [c["chapter_idx"] for c in contexts] == [1, 3]
    assert "许师姐来了" in contexts[0]["text"] and len(contexts[0]["text"]) <= 2 * pa._IDENTITY_CONTEXT_WINDOW + 3
    assert pa.label_chapter_contexts(conn, "p", "") == []
