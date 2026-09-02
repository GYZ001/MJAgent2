"""外观标志性特征证据核验测试（王有材事故修复，见 logs/appearance_provenance_plan.md）。

根因：`appearance_canonical` 生成 prompt 曾同时放"必须包含 1 个标志性特征"的正向配额和
"原著未描写处按题材合理补全"的兜底授权，逼模型编造；模型把同场另一个角色的特征安到了
王有材头上。修复：删掉配额，新增 source_evidence 结构性核验（逐字引句 + 40 字上限 + 名字
必须与描写同句出现）。本文件测试 `app._appearance_evidence_verified` / `_validate_
appearance_evidence` 本身。原有测试 `_generate_character_detail`/`_supplement_
bible_characters` 处理核验失败证据的 3 个用例已随这两个函数退场删除
（生产零调用，见 app/stages/bible_generate.py 模块 docstring）。另有 2 个依赖生产库
`data/manju.db` 章节 fixture 的用例（`test_appearance_evidence_rejected_when_quote_exceeds_length_cap`/
`test_appearance_evidence_accepted_with_short_battle_damage_quote`）在任何常规环境下
`_read_chapter` 都读不到该 fixture、恒定 `pytest.skip`，是永久 skip 的死用例，随同
`_read_chapter` 助手与 `ROOT`/`PRODUCTION_DB`/`PROJECT_ID` 一并删除。
"""
from __future__ import annotations

from app import stages
from app.schemas import AppearanceEvidence, Bible, Character, World


def test_appearance_evidence_verified_when_quote_hits_and_name_in_same_span() -> None:
    chapters_by_idx = {5: "王有材站在门口，黑衣束发，腰间挂着一把柴刀。"}

    assert stages._appearance_evidence_verified(
        chapters_by_idx, {"王有材"}, 5, "王有材站在门口，黑衣束发，腰间挂着一把柴刀。",
    ) is True


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
