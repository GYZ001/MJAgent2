"""外观标志性特征证据核验测试（王有材事故修复，见 logs/appearance_provenance_plan.md）。

根因：`appearance_canonical` 生成 prompt 曾同时放"必须包含 1 个标志性特征"的正向配额和
"原著未描写处按题材合理补全"的兜底授权，逼模型编造；模型把同场另一个角色的特征安到了
王有材头上。修复：删掉配额，新增 source_evidence 结构性核验（逐字引句 + 40 字上限 + 名字
必须与描写同句出现）。本文件测试 `app._appearance_evidence_verified` / `_validate_
appearance_evidence` 本身，以及 `_supplement_bible_characters` 对核验失败证据的处理。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from app import stages
from app.schemas import AppearanceEvidence, Bible, Character, World


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_DB = ROOT / "data" / "manju.db"
PROJECT_ID = "proj_3ac0b627fa46"


def _read_chapter(idx: int) -> str | None:
    if not PRODUCTION_DB.exists():
        return None
    with sqlite3.connect(f"file:{PRODUCTION_DB}?mode=ro", uri=True) as conn:
        row = conn.execute(
            "SELECT content FROM chapters WHERE project_id=? AND idx=?",
            (PROJECT_ID, idx),
        ).fetchone()
    return row[0] if row else None


def test_appearance_evidence_verified_when_quote_hits_and_name_in_same_span() -> None:
    chapters_by_idx = {5: "王有材站在门口，黑衣束发，腰间挂着一把柴刀。"}

    assert stages._appearance_evidence_verified(
        chapters_by_idx, {"王有材"}, 5, "王有材站在门口，黑衣束发，腰间挂着一把柴刀。",
    ) is True


@pytest.mark.live_integration
def test_appearance_evidence_rejected_when_quote_exceeds_length_cap() -> None:
    """回归：王有材事故的实际触发路径——把同场"小胖子"的体型特征安到王有材头上唯一
    可用的原文句子，从"王有材"三字开头到能覆盖"较胖"为止，最短连续引句需要 44 字；
    40 字上限直接拦住。引句取自真实原文第 1 章（proj_3ac0b627fa46，已用只读连接核对）。"""
    chapter_text = _read_chapter(1)
    if chapter_text is None:
        pytest.skip("production database / chapter fixture not present")
    quote = "王有材身上时，看到了他身边的两个少年，一个是那虎头虎脑的家伙，另一个则是白白净净身子较胖"
    assert quote in chapter_text  # 核对引句确实是该章逐字连续子串
    assert len(quote) == 44
    assert len(quote) > stages.APPEARANCE_EVIDENCE_QUOTE_MAX_CHARS

    assert stages._appearance_evidence_verified(
        {1: chapter_text}, {"王有材"}, 1, quote,
    ) is False


def test_appearance_evidence_rejected_when_name_not_in_quote_itself() -> None:
    chapters_by_idx = {
        10: "小胖子、王有材、还有那虎头虎脑的少年，当初我们四人被一起带上靠山宗。他身材微胖。",
    }
    # 引句真实存在、逐字命中，但角色名不在这条引句本身内部（只在同一章的别处）。
    quote = "他身材微胖。"

    assert stages._appearance_evidence_verified(
        chapters_by_idx, {"王有材"}, 10, quote,
    ) is False


def test_appearance_evidence_rejected_when_quote_not_verbatim_in_chapter() -> None:
    chapters_by_idx = {3: "王有材黑衣束发，腰间挂着一把柴刀。"}

    assert stages._appearance_evidence_verified(
        chapters_by_idx, {"王有材"}, 3, "王有材黑衣束发，腰间挂着一把长剑。",
    ) is False


def test_appearance_evidence_rejected_when_chapter_index_wrong() -> None:
    chapters_by_idx = {3: "王有材黑衣束发，腰间挂着一把柴刀。"}

    assert stages._appearance_evidence_verified(
        chapters_by_idx, {"王有材"}, 4, "王有材黑衣束发，腰间挂着一把柴刀。",
    ) is False


@pytest.mark.live_integration
def test_appearance_evidence_accepted_with_short_battle_damage_quote() -> None:
    """已知限制的具体化（非遗漏）：证据真实存在且确实指向王有材本人，即使描述的是战斗后
    临时状态（不是固定外观），核验按设计仍会通过——见 logs/appearance_provenance_plan.md
    难点A。引句取自真实原文第 130 章（proj_3ac0b627fa46，已核对）。"""
    chapter_text = _read_chapter(130)
    if chapter_text is None:
        pytest.skip("production database / chapter fixture not present")
    quote = "王有材与宋佳，他二人在出现后，都极为狼狈，身体满是伤痕"
    assert quote in chapter_text
    assert len(quote) <= stages.APPEARANCE_EVIDENCE_QUOTE_MAX_CHARS

    assert stages._appearance_evidence_verified(
        {130: chapter_text}, {"王有材"}, 130, quote,
    ) is True


def test_character_appearance_evidence_default_empty() -> None:
    """旧 bible_json（无 source_evidence 键）反序列化不受影响，默认空列表。"""
    character = Character.model_validate({
        "name": "王有材", "role": "重要配角",
        "appearance_canonical": "十六七岁少年，黑色短发，粗麻杂役衫，身形瘦弱",
    })

    assert character.source_evidence == []


def test_validate_appearance_evidence_empty_list_never_errors() -> None:
    """空 source_evidence 永远不产生 error——诚实的"没有可举证特征"是安全默认，不是
    缺陷信号，不能让老实说没有比编一个能蒙混过关的更差。"""
    bible = Bible(
        characters=[Character(
            name="王有材", role="重要配角",
            appearance_canonical="十六七岁少年，黑色短发，粗麻杂役衫，身形瘦弱",
            source_evidence=[],
        )],
        world=World(visual_style_canonical="3D国漫CG渲染，虚构数字角色"),
    )

    assert stages._validate_appearance_evidence(bible, {}) == []


def test_validate_appearance_evidence_rejects_unverified_entry() -> None:
    bible = Bible(
        characters=[Character(
            name="王有材", role="重要配角",
            appearance_canonical="十六七岁少年，黑色短发，粗麻杂役衫，身形瘦弱",
            source_evidence=[AppearanceEvidence(
                evidence_chapter_index=1, evidence_quote="虚构引句不在原文里",
            )],
        )],
        world=World(visual_style_canonical="3D国漫CG渲染，虚构数字角色"),
    )

    errors = stages._validate_appearance_evidence(bible, {1: "别的原文内容，不含引句。"})

    assert errors
    assert "王有材" in errors[0]


def test_bible_prompt_no_longer_requires_mandatory_signature_trait(monkeypatch) -> None:
    """静态回归：改写后的 generate_bible prompt 不再含"必须包含...1 个标志性特征"这类
    强制表述，且已经把 source_evidence/通用形态分层说清楚——防止未来有人无意中把配额
    加回去。"""
    seen: dict[str, object] = {}

    async def fake_loop(*args, **_kwargs):
        seen["prompt"] = args[2]
        return Bible(
            world=World(visual_style_canonical="国漫3D动画电影质感，精致光影"),
            characters=[Character(
                name="孟浩", role="主角",
                appearance_canonical="十六七岁少年，黑色短发额前碎发，蓝色文士长衫，身形瘦弱，腰间挂布袋",
            )],
        )

    monkeypatch.setattr(stages, "_run_with_agent_loop", fake_loop)

    asyncio.run(stages.generate_bible(
        [{"idx": 1, "title": "第一章", "content": "孟浩走入山中。"}],
    ))

    prompt = str(seen["prompt"])
    assert "必须包含 性别年龄感/发型发色/服装款式与颜色/1 个标志性特征" not in prompt
    assert "source_evidence" in prompt
    assert "通用形态" in prompt
    assert "标志性特征" in prompt


def test_supplement_bible_characters_prompt_no_longer_requires_mandatory_signature_trait(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_chat(messages, **_kwargs):
        captured["prompt"] = messages[-1]["content"]
        return json.dumps({"characters": []}, ensure_ascii=False)

    monkeypatch.setattr(stages.model_gateway, "chat", fake_chat)
    bible = Bible(characters=[], world=World(visual_style_canonical="3D国漫CG渲染"))

    asyncio.run(stages._supplement_bible_characters(
        bible, [("王有材", "", 5)], "小说文本占位",
        chapters_by_idx={},
    ))

    prompt = str(captured["prompt"])
    assert "必须包含 性别年龄感/发型发色/服装款式与颜色/1 个标志性特征" not in prompt
    assert "source_evidence" in prompt


def test_supplement_bible_characters_drops_unverified_evidence_keeps_character(
    monkeypatch,
) -> None:
    """没有 AgentLoop 重试可用：核验失败的证据条目直接从 source_evidence 剔除，角色本身
    照常补录（不因一条证据不实拒绝整个角色）。"""

    async def fake_chat(*_args, **_kwargs):
        return json.dumps({"characters": [{
            "name": "王有材", "role": "重要配角",
            "appearance_canonical": "十六七岁少年，黑色短发梳整齐，深棕短打木匠服，身形敦实",
            "personality": "", "speech_style": "",
            "relationships": [],
            "source_evidence": [
                {"evidence_chapter_index": 1, "evidence_quote": "虚构引句不在原文里"},
            ],
        }]}, ensure_ascii=False)

    monkeypatch.setattr(stages.model_gateway, "chat", fake_chat)
    bible = Bible(characters=[], world=World(visual_style_canonical="3D国漫CG渲染"))

    added = asyncio.run(stages._supplement_bible_characters(
        bible, [("王有材", "", 5)], "小说文本占位",
        chapters_by_idx={1: "第一章原文，完全不含引句内容。"},
    ))

    assert added == ["王有材"]
    assert bible.characters[0].name == "王有材"
    assert bible.characters[0].source_evidence == []
