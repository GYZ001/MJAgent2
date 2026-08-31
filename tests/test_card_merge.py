"""``app.portraits.card_merge`` 的建卡前归并判断回归。

背景：``app.portraits.card_owner.resolve_card_owner`` 判 "none" 只回答"这个
称呼没有逐字命中人物谱里任何 name/alias"，不回答"这不是已有的某个人"。真实
事故：《我的女友井田》原文明写"以后别叫我井田了，叫我妈妈吧"，但"妈妈"从未
被连回"井田"的卡，安静地生成了第二套长相（见 app/portraits/card_merge.py
模块 docstring）。

三条覆盖：
1) 原文有明示身份链接句时，以新称呼触发建卡能连回既有角色的卡、登记别名、
   不新建（正例）。
2) 两个真的不同的人不被合并——候选合法（在候选名单内）但模型指向的段号
   缺乏双锚定证据时，结构性钉证必须拒绝，不能只靠模型自己说了算（反例，
   本文件最重要的一条：防判据过宽）。
3) 卷宗里没有任何既有角色命中时，不发起模型调用（打桩计数断言）。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

from app import portraits
from app.harness import model_gateway
from app.schemas import Bible, Character, World
from tests.conftest import patch_portraits_everywhere


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER DEFAULT 0)"
    )
    conn.execute("CREATE TABLE chapters(project_id TEXT, idx INTEGER, content TEXT)")
    conn.execute("CREATE TABLE episodes(project_id TEXT, episode_no INTEGER, source_chapters TEXT)")
    conn.execute(
        "CREATE TABLE character_portraits(id TEXT, project_id TEXT, character_name TEXT, ep_start INTEGER, "
        "ep_end INTEGER, appearance TEXT, prompt TEXT, image_path TEXT, base_portrait_id TEXT, "
        "bible_version INTEGER, created_at REAL)"
    )
    return conn


def _seed_project(conn: sqlite3.Connection, bible: Bible, chapter_content: str) -> None:
    conn.execute(
        "INSERT INTO projects(id, bible_json, bible_version) VALUES('p1', ?, 1)",
        (json.dumps(bible.model_dump(), ensure_ascii=False),),
    )
    conn.execute("INSERT INTO episodes(project_id, episode_no, source_chapters) VALUES('p1', 21, '[30]')")
    conn.execute("INSERT INTO chapters(project_id, idx, content) VALUES('p1', 30, ?)", (chapter_content,))
    conn.commit()


def _patch_everything(monkeypatch, conn: sqlite3.Connection) -> dict:
    settings: dict[str, str] = {}
    patch_portraits_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_portraits_everywhere(monkeypatch, "get_setting", lambda k: settings.get(k, ""))
    patch_portraits_everywhere(monkeypatch, "set_setting", lambda k, v: settings.__setitem__(k, v))
    return settings


def _characters(conn: sqlite3.Connection) -> list[dict]:
    row = conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()
    return json.loads(row["bible_json"])["characters"]


async def _fake_assess_important_person(name, fragments, *, style, known_names, ep_label, **_kwargs):
    return {
        "subject_kind": "person",
        "important": True,
        "reason": "反复出场，原文明确点出身份",
        "role": "重要配角",
        "appearance_canonical": "圆脸少女，扎着双马尾，身穿碎花棉布衣裙，笑容温和亲切",
        "personality": "温柔",
        "speech_style": "温和",
        "relationships": [],
    }


# ---------------------------------------------------------------------------
# 1) 正例：身份链接句能把新称呼连回既有角色的卡，登记别名，不新建
# ---------------------------------------------------------------------------

def test_identity_link_sentence_merges_new_label_into_existing_card(monkeypatch) -> None:
    conn = _make_conn()
    _seed_project(
        conn,
        Bible(
            world=World(visual_style_canonical="都市"),
            characters=[Character(name="井田", role="主角", appearance_canonical="圆脸少女，双马尾")],
        ),
        "以后别叫我井田了，叫我妈妈吧。妈妈牵着孩子的手在院子里散步。妈妈笑着摸了摸孩子的头。",
    )
    _patch_everything(monkeypatch, conn)
    patch_portraits_everywhere(monkeypatch, "assess_new_character", _fake_assess_important_person)

    calls: list[dict] = []

    async def fake_chat_structured(messages, *, model_type, **kwargs):
        calls.append({"prompt": messages[0]["content"], "candidates": kwargs["call_meta"]["candidates"]})
        assert kwargs["call_meta"]["candidates"] == ["井田"]
        return model_type(selected_candidate="井田", supporting_entry_index=1, supporting_quote="")

    monkeypatch.setattr(model_gateway, "chat_structured", fake_chat_structured)

    result = asyncio.run(portraits.ensure_character_card("p1", "妈妈", 21, generate_portrait=False))

    assert result == {"status": "exists", "name": "井田"}
    assert len(calls) == 1  # 归并判断确实发起了一次模型调用

    characters = _characters(conn)
    assert len(characters) == 1  # 没有建出第二张卡
    alias_texts = {a["text"]: a for a in characters[0]["aliases"]}
    assert "妈妈" in alias_texts
    assert alias_texts["妈妈"]["is_exclusive"] is False
    assert "妈妈" in alias_texts["妈妈"]["evidence_quote"]
    assert "井田" in alias_texts["妈妈"]["evidence_quote"]


# ---------------------------------------------------------------------------
# 2) 反例（最重要）：候选合法但钉证段落缺乏双锚定证据，必须拒绝合并，
#    不能只靠模型自己选了一个候选名单内的名字就采信
# ---------------------------------------------------------------------------

def test_weakly_pinned_candidate_is_rejected_not_merged(monkeypatch) -> None:
    conn = _make_conn()
    _seed_project(
        conn,
        Bible(
            world=World(visual_style_canonical="武侠"),
            characters=[Character(name="沈婉", role="重要配角", appearance_canonical="红衣女子，腰佩软剑")],
        ),
        # 两段：第一段只有称谓"红衣人"（不含候选姓名"沈婉"）；
        # 第二段才同时出现"沈婉"与"红衣人"。候选"沈婉"因为在卷宗全文（两段
        # 拼接）里出现而合法入选候选名单，但模型（下面的桩）故意钉在第一段
        # （entry_index=1，缺"沈婉"本人）——结构性钉证必须拒绝这次采信。
        "红衣人匆匆走过街角，无人看清她的脸。\n\n沈婉换上一身红衣人打扮，在庭院里练剑。",
    )
    _patch_everything(monkeypatch, conn)
    patch_portraits_everywhere(monkeypatch, "assess_new_character", _fake_assess_important_person)

    calls: list[dict] = []

    async def fake_chat_structured(messages, *, model_type, **kwargs):
        calls.append(kwargs["call_meta"])
        assert kwargs["call_meta"]["candidates"] == ["沈婉"]
        # 故意钉错段落：选中候选"沈婉"合法（在候选名单内），但支撑段号
        # 指向没有"沈婉"本人的第一段——这正是要被结构性钉证拦下的情形。
        return model_type(selected_candidate="沈婉", supporting_entry_index=1, supporting_quote="红衣人")

    monkeypatch.setattr(model_gateway, "chat_structured", fake_chat_structured)

    result = asyncio.run(portraits.ensure_character_card("p1", "红衣人", 21, generate_portrait=False))

    assert len(calls) == 1  # 确实问过模型，不是靠"没候选"侥幸躲过
    assert result["status"] == "added"  # 没有被错误合并，照常建了新卡
    assert result["name"] == "红衣人"

    characters = _characters(conn)
    names = {c["name"] for c in characters}
    assert names == {"沈婉", "红衣人"}  # 两个人依然是两张卡
    shen_wan = next(c for c in characters if c["name"] == "沈婉")
    assert shen_wan["aliases"] == []  # 沈婉没有被错误地追加"红衣人"这个别名


# ---------------------------------------------------------------------------
# 3) 卷宗里没有任何既有角色命中：不发起模型调用（浪费 + 会逼模型瞎选）
# ---------------------------------------------------------------------------

def test_no_candidate_hit_skips_model_call_entirely(monkeypatch) -> None:
    conn = _make_conn()
    _seed_project(
        conn,
        Bible(
            world=World(visual_style_canonical="武侠"),
            characters=[Character(name="井田", role="主角", appearance_canonical="圆脸少女，双马尾")],
        ),
        "小石头蹲在墙角发呆，谁也不知道他是谁。小石头又踢了踢脚边的碎石。小石头终于站起身来。",
    )
    _patch_everything(monkeypatch, conn)
    patch_portraits_everywhere(monkeypatch, "assess_new_character", _fake_assess_important_person)

    calls: list[dict] = []

    async def fake_chat_structured(messages, *, model_type, **kwargs):
        calls.append(kwargs)
        raise AssertionError("不应该发起模型调用：候选集为空")

    monkeypatch.setattr(model_gateway, "chat_structured", fake_chat_structured)

    result = asyncio.run(portraits.ensure_character_card("p1", "小石头", 21, generate_portrait=False))

    assert calls == []  # 打桩计数断言：候选集为空时零次模型调用
    assert result["status"] == "added"
    names = {c["name"] for c in _characters(conn)}
    assert names == {"井田", "小石头"}
