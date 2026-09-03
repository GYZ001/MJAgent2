"""WS10-A：一句话真名不自动出定妆照（WS3 报告遗留项）。

根因：``require_identity_card=True`` 时 ``assess_new_character`` 的提示词契约
固定 ``important=true``——这本身没错（身份消歧已经确认这是稳定真名，人物谱
要能解析这个名字），但下游把"值得建卡"和"值得花一次定妆照生成开销"当成了
同一件事，于是一句话提及的真名（跑不快的孩子：德科、埃托奥、莱曼、蒙铁尔、
马丁内斯）全部自动出图。

修复：``app.portraits.card_verdict.portrait_generation_decision`` 把两者拆开——
人物卡照常建（``important=true`` 的合同原样保留，人物谱条目不受影响），是否
【自动】出图单独按画面存在证据判定（复用 ``app.portraits.presence_evidence.
functional_card_worthy`` 同一套判据：在场 ≥2 段，或单段但对白+动作齐备）。

单独成文件（不并入 ``tests/test_character_presence_discovery.py``）是因为
后者已经在 500 行严格上限附近，装不下这组新用例（新增测试文件按 500 行
严格执行，不进 ``[baseline.*]`` 棘轮）；复用该文件里的 ``_make_conn`` /
``_seed_project`` / ``_patch_settings`` 三个 DB 夹具，不重写一套等价的。
"""
from __future__ import annotations

import asyncio
import json

from app import portraits
from app.portraits.card_verdict import portrait_generation_decision
from app.portraits.presence_evidence import collect_presence_evidence

from tests.conftest import patch_portraits_everywhere
from tests.test_character_presence_discovery import _make_conn, _patch_settings, _seed_project

# 仿《跑不快的孩子》真实生产样本——「队里有罗纳尔迪尼奥，有德科，有埃托奥」
# 「莱曼扑出点球」两种真实句式：前者是纯罗列提及，后者是单段单动作，都不该
# 自动出定妆照。
ROSTER_ONE_LINE_MENTION = "教练回忆当年阵容时说：队里有罗纳尔迪尼奥，有德科，有埃托奥，个个都是巨星。"
GOALKEEPER_SINGLE_ACTION_MENTION = "点球大战最后时刻，莱曼扑出点球，全场沸腾。"
# 反例：同一真名在两段不同文字里都有动作描写，够格自动出图（不能被本次改动
# 误伤成"所有 require_identity_card 的名字都不自动出图"）。
RECURRING_NAMED_IDENTITY = "老张跑进屋子，一把推开门。老张又冲回院子里，扶起摔倒的孩子。"


# ==================== 1. portrait_generation_decision 纯函数单元测试 ====================

def test_portrait_generation_decision_defers_one_line_mention() -> None:
    evidence = collect_presence_evidence("德科", {1: ROSTER_ONE_LINE_MENTION})
    worthy, reason = portrait_generation_decision(require_identity_card=True, presence=evidence)
    assert worthy is False
    assert "戏份不足" in reason


def test_portrait_generation_decision_defers_single_action_mention() -> None:
    evidence = collect_presence_evidence("莱曼", {1: GOALKEEPER_SINGLE_ACTION_MENTION})
    worthy, _reason = portrait_generation_decision(require_identity_card=True, presence=evidence)
    assert worthy is False


def test_portrait_generation_decision_allows_recurring_named_identity() -> None:
    """非回归：同一真名在两段不同文字里都有动作描写，仍然自动出图——本次
    改动只拦一句话提及，不能误伤真正反复在场的角色。"""
    evidence = collect_presence_evidence("老张", {1: RECURRING_NAMED_IDENTITY})
    worthy, reason = portrait_generation_decision(require_identity_card=True, presence=evidence)
    assert worthy is True
    assert reason == ""


def test_portrait_generation_decision_bypassed_when_identity_card_not_required() -> None:
    """require_identity_card=False（模型自主判断戏份的既有路径）不受本次改动
    影响：即使画面存在证据单薄，也不额外拦截——那条路径的 important 本来就是
    模型自己的戏份判断，不是被合同强制的常量。"""
    evidence = collect_presence_evidence("路人甲", {1: "路人甲走过。"})
    worthy, reason = portrait_generation_decision(require_identity_card=False, presence=evidence)
    assert worthy is True
    assert reason == ""


# ==================== 2. ensure_character_card 集成测试 ====================

def test_one_line_named_identity_gets_bible_entry_but_no_auto_portrait(monkeypatch) -> None:
    """集成回归：德科式一句话真名——require_identity_card=True 时人物卡必须
    正常入谱（未来章节可能还会用到这个名字），但不触发定妆照生成。"""
    conn = _make_conn()
    _seed_project(conn, ROSTER_ONE_LINE_MENTION)
    _patch_settings(monkeypatch, conn)

    portrait_calls = {"n": 0}

    async def fake_assess(name, fragments, *, style, known_names, ep_label, **kwargs):
        assert kwargs["require_identity_card"] is True
        assert "德科" in fragments
        return {
            "subject_kind": "person", "important": True, "reason": "身份消歧已确认真名",
            "role": "重要配角",
            "appearance_canonical": "黑发球员，深色球衣，中等身形，短发利落",
            "personality": "", "speech_style": "", "relationships": [],
        }

    async def fake_portrait(*_args, **_kwargs):
        portrait_calls["n"] += 1
        return {"portrait_id": "pt1", "ref_image_path": "/tmp/德科.jpg"}

    async def fake_no_merge(*_a, **_k):
        return None

    patch_portraits_everywhere(monkeypatch, "assess_new_character", fake_assess)
    patch_portraits_everywhere(monkeypatch, "_generate_discovered_character_portrait", fake_portrait)
    patch_portraits_everywhere(monkeypatch, "resolve_card_merge_target", fake_no_merge)

    result = asyncio.run(portraits.ensure_character_card(
        "p1", "德科", 21, require_identity_card=True,
    ))

    assert result["status"] == "added"
    assert result["has_portrait"] is False
    assert result["portrait_on_demand"] is True
    assert portrait_calls["n"] == 0  # 未自动触发定妆照生成

    characters = json.loads(
        conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"],
    )["characters"]
    assert any(character["name"] == "德科" for character in characters)  # 人物谱正常入谱

    queue = json.loads(
        conn.execute(
            "SELECT bible_auto_changes_json FROM projects WHERE id='p1'",
        ).fetchone()["bible_auto_changes_json"],
    )
    assert "戏份不足" in queue[0]["decision_reason"]
    assert "手动生成" in queue[0]["decision_reason"]


def test_recurring_named_identity_still_gets_auto_portrait(monkeypatch) -> None:
    """非回归：同一真名在原文里反复出场（两段不同文字都有动作描写）时，
    require_identity_card=True 路径仍然照旧自动出图——不能因为本次改动把
    "一句话真名不自动出图"泛化成"所有身份确认路径都不自动出图"。"""
    conn = _make_conn()
    _seed_project(conn, RECURRING_NAMED_IDENTITY)
    _patch_settings(monkeypatch, conn)

    portrait_calls = {"n": 0}

    async def fake_assess(name, fragments, *, style, known_names, ep_label, **kwargs):
        assert kwargs["require_identity_card"] is True
        return {
            "subject_kind": "person", "important": True, "reason": "身份消歧已确认真名",
            "role": "重要配角",
            "appearance_canonical": "中年男子，深色外套，体格健壮，神情焦急",
            "personality": "", "speech_style": "", "relationships": [],
        }

    async def fake_portrait(*_args, **_kwargs):
        portrait_calls["n"] += 1
        return {"portrait_id": "pt2", "ref_image_path": "/tmp/老张.jpg"}

    async def fake_no_merge(*_a, **_k):
        return None

    patch_portraits_everywhere(monkeypatch, "assess_new_character", fake_assess)
    patch_portraits_everywhere(monkeypatch, "_generate_discovered_character_portrait", fake_portrait)
    patch_portraits_everywhere(monkeypatch, "resolve_card_merge_target", fake_no_merge)

    result = asyncio.run(portraits.ensure_character_card(
        "p1", "老张", 21, require_identity_card=True,
    ))

    assert result["status"] == "added"
    assert result["has_portrait"] is True
    assert result.get("portrait_on_demand") in (False, None)
    assert portrait_calls["n"] == 1
