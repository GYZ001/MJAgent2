"""人物谱「裸泛指别名」修复：CharacterAlias.is_exclusive 与
app.identity_authority.identity_authority_registry 的排他性折叠 + 跨角色排重。

背景（真实事故）：EP2「少年」、EP3/EP10「大汉」误登记为身份凭证，导致「我欲封天」
EP1-10 并发回归里 3 集失败；跨项目复现见 ERR-20260828-9fcabe（《罗刹海市》EP1,
「大夫」被登记成主角马骥的别名）。核心思路：别名不删——把「能否作为全局身份凭证」
（source_labels，参与身份决议）与「能否作为可检索的称呼线索」（Character.aliases 本身，
供 app.production.prep_pack._prep_pack_bible_alias_owner 等通道直接读取）拆开。

本文件覆盖：
1. is_exclusive=False 的别名不进 source_labels，但仍留在 Character.aliases 里。
2. 跨角色碰撞（「大汉」复刻）：两个角色各自申报同一别名文本、两次裁决都 accept，
   两边都不进 source_labels（不猜赢家）。
3. 非回归：带限定语的真实别名（许师姐/陈师兄/赵武刚师兄/金袍老者/上官老者/李富贵/
   虎爷爷）必须全部保留且仍进 source_labels。
4. `_attach_roster_source_appellations`（免检通道，ERR-20260828-9fcabe 的登记入口）
   登记的别名 is_exclusive 必须为 False。
"""
from __future__ import annotations

import asyncio

from app.identity_authority import identity_authority_registry
from app.schemas import Bible, Character, CharacterAlias, World


APPEARANCE = "二十岁女子，墨发高马尾，银色素面长袍，身形清瘦，背后一柄银色长剑，眉目清冷"
WORLD = World(visual_style_canonical="国漫3D动画电影质感，精致光影")


def _bible(*characters: Character) -> Bible:
    return Bible(characters=list(characters), world=WORLD)


def _entry(registry: list[dict], authority_id: str) -> dict:
    return next(item for item in registry if item["authority_id"] == authority_id)


def _fake_verdict_chat_structured(
    selected_candidate: str,
    supporting_segment_index: int = 1,
    supporting_quote: str = "",
    is_exclusive_reference: bool = True,
):
    """与 tests/test_character_alias.py 同名 helper 同一实现，见该文件的完整注释。"""

    async def fake(_messages, **kwargs):
        model_type = kwargs["model_type"]
        return model_type(
            selected_candidate=selected_candidate,
            supporting_segment_index=supporting_segment_index,
            supporting_quote=supporting_quote,
            is_exclusive_reference=is_exclusive_reference,
        )

    return fake


# ---------- 1. is_exclusive=False：不进 source_labels，但解析能力不丢 ----------

def test_non_exclusive_alias_excluded_from_source_labels_but_kept_on_character() -> None:
    """「大汉」复刻（单角色场景）：模型申报的称谓字面本身是泛指（换任何符合体型
    特征的陌生人都可能被这样称呼），裁决闸判定 is_exclusive_reference=False——
    这条别名仍然登记（accepted=True，三闸通过就照常登记，不新增拒绝分支），只是
    不折进 source_labels，不参与身份决议的排他判断。"""
    character = Character(
        name="孟浩", role="主角", appearance_canonical=APPEARANCE,
        aliases=[CharacterAlias(
            text="大汉", name_kind="referential",
            evidence_chapter_index=3, evidence_quote="那魁梧大汉环视四周",
            is_exclusive=False,
        )],
    )
    bible = _bible(character)
    registry = identity_authority_registry(bible, [])
    entry = _entry(registry, "bible:孟浩")

    assert entry["source_labels"] == ["孟浩"]  # 「大汉」没有折进身份凭证
    # 解析能力不丢：app.production.prep_pack._prep_pack_bible_alias_owner 之类的
    # 通道直接读 Character.aliases，不看 is_exclusive，这条别名必须还在。
    assert [a.text for a in bible.characters[0].aliases] == ["大汉"]
    assert bible.characters[0].aliases[0].is_exclusive is False


def test_alias_evidence_resolution_returns_is_exclusive_false_for_generic_appellation(
    monkeypatch,
) -> None:
    """端到端：裁决闸判定泛指（is_exclusive_reference=False）时，
    `_alias_evidence_resolution` 返回的 accepted 字典里 `is_exclusive` 必须原样
    透传该结论，供三处调用方构造 `CharacterAlias(..., is_exclusive=...)`。"""
    from app import stages
    from app.harness import model_gateway

    chapters = [{"idx": 3, "title": "第三章", "content": (
        "从外面走进一个穿着杂役衫的魁梧大汉，他凶狠的看了孟浩一眼。"
    )}]
    chapters_by_idx = stages._chapters_by_idx(chapters)
    roster = {"孟浩": ["孟浩"], "大汉": ["大汉"]}

    monkeypatch.setattr(
        model_gateway, "chat_structured",
        _fake_verdict_chat_structured("大汉", is_exclusive_reference=False),
    )
    resolved = asyncio.run(stages._alias_evidence_resolution(
        chapters_by_idx, {"大汉"}, "魁梧大汉", "大汉", 3,
        "从外面走进一个穿着杂役衫的魁梧大汉",
        roster=roster,
    ))
    assert resolved["accepted"] is True
    assert resolved["is_exclusive"] is False


# ---------- 2. 跨角色碰撞：两个角色各自申报同一别名，两次裁决都 accept ----------

def test_alias_text_claimed_by_two_characters_excluded_from_both(monkeypatch) -> None:
    """「大汉」复刻（跨角色场景，真实事故：EP3/EP10 回归失败）：两个不同角色各自
    独立申报同一个别名文本"大汉"，两次裁决都判定 selected_candidate 命中各自的
    true_name、且都判定 is_exclusive_reference=True（模型认为脱离语境这个称谓本身
    专属于"这一个人"——两次模型判断互相矛盾但各自独立看起来都合理，这正是问题的
    根源）。跨角色排重是纯数据判据：同一 alias 文本被 ≥2 个角色登记，结构上已经
    证明它对任一角色都不排他，不猜哪个角色是"真正的"主人，两边都不折进
    source_labels——即使两次裁决各自都通过。别名本身仍分别保留在两个角色的
    aliases 里（不删）。"""
    from app import stages
    from app.harness import model_gateway

    chapter_a = [{"idx": 3, "title": "第三章", "content": (
        "那魁梧大汉一声怒喝，向着孟浩的方向冲了过去。"
    )}]
    chapter_b = [{"idx": 8, "title": "第八章", "content": (
        "李逵不愧是山寨里出了名的大汉，抡起板斧便是一阵猛砍。"
    )}]

    monkeypatch.setattr(
        model_gateway, "chat_structured",
        _fake_verdict_chat_structured("孟浩", is_exclusive_reference=True),
    )
    resolved_a = asyncio.run(stages._alias_evidence_resolution(
        stages._chapters_by_idx(chapter_a), {"孟浩"}, "大汉", "孟浩", 3,
        "那魁梧大汉一声怒喝，向着孟浩的方向冲了过去",
        roster={"孟浩": ["孟浩"]},
    ))
    monkeypatch.setattr(
        model_gateway, "chat_structured",
        _fake_verdict_chat_structured("李逵", is_exclusive_reference=True),
    )
    resolved_b = asyncio.run(stages._alias_evidence_resolution(
        stages._chapters_by_idx(chapter_b), {"李逵"}, "大汉", "李逵", 8,
        "李逵不愧是山寨里出了名的大汉，抡起板斧便是一阵猛砍",
        roster={"李逵": ["李逵"]},
    ))
    assert resolved_a["accepted"] is True and resolved_a["is_exclusive"] is True
    assert resolved_b["accepted"] is True and resolved_b["is_exclusive"] is True

    meng_hao = Character(
        name="孟浩", role="主角", appearance_canonical=APPEARANCE,
        aliases=[CharacterAlias(
            text="大汉", name_kind="referential",
            evidence_chapter_index=resolved_a["chapter_idx"],
            evidence_quote=resolved_a["quote"],
            is_exclusive=resolved_a["is_exclusive"],
        )],
    )
    li_kui = Character(
        name="李逵", role="重要配角", appearance_canonical="黑脸虬髯，膀大腰圆",
        aliases=[CharacterAlias(
            text="大汉", name_kind="referential",
            evidence_chapter_index=resolved_b["chapter_idx"],
            evidence_quote=resolved_b["quote"],
            is_exclusive=resolved_b["is_exclusive"],
        )],
    )
    bible = _bible(meng_hao, li_kui)
    registry = identity_authority_registry(bible, [])
    meng_hao_entry = _entry(registry, "bible:孟浩")
    li_kui_entry = _entry(registry, "bible:李逵")

    assert meng_hao_entry["source_labels"] == ["孟浩"]
    assert li_kui_entry["source_labels"] == ["李逵"]
    # 别名不删：两边各自的 aliases 仍然保留"大汉"这条可检索的称呼线索。
    assert [a.text for a in bible.characters[0].aliases] == ["大汉"]
    assert [a.text for a in bible.characters[1].aliases] == ["大汉"]


# ---------- 3. 非回归：带限定语的真实别名必须继续折进 source_labels ----------

def test_qualified_aliases_are_not_regressed_by_exclusivity_filtering() -> None:
    """带姓氏/门派身份/独有外观细节等限定成分的别名（结构上真实专属于这一个人）
    必须继续通过，不被本次改动误伤——覆盖非回归清单：许师姐、陈师兄、赵武刚师兄、
    金袍老者、上官老者、李富贵、虎爷爷。每个别名各自只属于一个角色（不触发跨角色
    排重），is_exclusive 使用默认值 True（裁决闸判定为排他）。

    真实作用域声明（真实事故：这组测试没能拦住 `_alias_verdict_call` 任务二提示词
    把排他性标准拔到不可能达到的高度——18 条真实别名实测 15 条被误判非排他，其中
    就包括这里列出的许师姐/金袍老者/李富贵等）：本测试组直接构造
    `CharacterAlias(...)`（`is_exclusive` 用 schema 默认值 True，不调用
    `_alias_verdict_call`，不 mock model_gateway、也没有喂任何裁决闸的假答案），
    验的是"给定 is_exclusive=True 时，identity_authority_registry 的折叠逻辑是否
    正确把这些别名保留在 source_labels 里"——这是纯粹的下游折叠正确性，不涉及模型
    判据本身。它天然验证不了"模型的排他性判据措辞是否合理/模型真实调用会不会答
    对"，因为它压根没有经过那条提示词。真正验证提示词措辞的是
    scripts/calibrate_alias_exclusivity.py——用同一批真实别名的真实原文/候选人
    卷宗，真实调用 `_alias_verdict_call`，把模型返回的 `is_exclusive_reference`
    与人工标定的期望值比对打分。两组测试缺一不可：这里保证"答案对了之后折叠对不
    对"，夹具保证"模型答案本身对不对"，不要用其中一个替代另一个。"""
    qualified = [
        ("许清", "许师姐"),
        ("陈瑾", "陈师兄"),
        ("赵武", "赵武刚师兄"),
        ("上官洪", "金袍老者"),
        ("柳成", "上官老者"),
        ("李富贵", "李富贵"),  # 真名本身也应作为可用标签（不受本次改动影响）
        ("孟浩", "虎爷爷"),
    ]
    characters = [
        Character(
            name=name, role="重要配角", appearance_canonical=APPEARANCE,
            aliases=[CharacterAlias(
                text=alias_text, name_kind="honorific",
                evidence_chapter_index=1, evidence_quote=f"{alias_text}登场",
            )] if alias_text != name else [],
        )
        for name, alias_text in qualified
    ]
    bible = _bible(*characters)
    registry = identity_authority_registry(bible, [])

    for name, alias_text in qualified:
        entry = _entry(registry, f"bible:{name}")
        assert name in entry["source_labels"]
        if alias_text != name:
            assert alias_text in entry["source_labels"], (
                f"{name} 的别名 {alias_text} 应当保留在 source_labels 里"
            )


# ---------- 4. _attach_roster_source_appellations：免检通道显式登记 is_exclusive=False ----------

def test_roster_source_appellation_backfill_marks_alias_non_exclusive() -> None:
    """免检通道（ERR-20260828-9fcabe 事故登记入口）只做共现检查，从未为排他性做过
    任何核验——必须显式写 is_exclusive=False，不依赖 schema 默认值。别名仍然登记
    （解析能力不丢），只是不参与身份决议。"""
    from app import stages

    character = Character(name="马骥", role="主角", appearance_canonical="书生模样")
    entry = stages._BibleRosterEntry(
        name="马骥", role="主角", source_appellations=["大夫"],
    )
    chapters = [{"idx": 1, "title": "第一章", "content": (
        "那些士绅大夫争着想开开眼界，便叫村民邀请马骥前去。"
    )}]

    stages._attach_roster_source_appellations(character, entry, chapters)

    assert [a.text for a in character.aliases] == ["大夫"]
    assert character.aliases[0].is_exclusive is False

    bible = _bible(character)
    registry = identity_authority_registry(bible, [])
    entry_registry = _entry(registry, "bible:马骥")
    assert entry_registry["source_labels"] == ["马骥"]  # 「大夫」不折进身份凭证
