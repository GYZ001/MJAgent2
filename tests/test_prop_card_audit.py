"""道具卡「按现行规则复核」的判定核心（``app.props.card_audit``/
``card_audit_rules``）：规则版本、子句删除代码核验、别名歧义判定、三类
真实场景、原子重出图、CAS/规则版本前进/失败重试上限。

与 ``tests/test_prop_card_audit_ensure.py``（三个触发点接线、手动入口、
dry-run）互补：那个文件钉住"复核接在产品里哪三个地方"，这个文件钉住"复核
本身算得对不对"。背景见本次派单（2026-10-03，proj_ca86b15ab7d7 实测）：
「浅灰色卫衣」外观把别的物件（星盘）压出的印痕写进去，出图画出不该有的星盘
图案；「绿萝」/「纸箱」/「行李箱」外观把泡水后的时点状态写进去，泡水前的
段落也会拿到泡水后的图；「对面木椅」等卡的别名含只剩品类名的"椅子"泛称，
送错参考图比不送更糟。

模型调用全部 monkeypatch（不发真实网络请求，遵循 ``tests/test_prep_pack_
asset_discovery.py`` 顶部同一条边界说明：只打桩外部协作者，不重新测试模型
契约本身）。
"""
from __future__ import annotations

import json

import pytest

from app.db import get_conn, now
from app.props import card_audit, card_audit_rules, card_audit_store, judge
from app.schemas import Prop


def _seed_project(project_id: str, *, props_list: list[dict] | None = None, style: str = "写实") -> None:
    bible = {
        "characters": [], "scenes": [], "props": props_list or [],
        "world": {"era": "", "genre": "", "visual_style_canonical": style},
    }
    conn = get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, bible_json, bible_version, created_at) VALUES(?,?,?,0,?)",
        (project_id, "测试项目", json.dumps(bible, ensure_ascii=False), now()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 1) judge：子句切分 / 原文拼接重建（零新增字符）
# ---------------------------------------------------------------------------

def test_split_appearance_clauses_matches_separator_set() -> None:
    """切分边界只认句子边界（逗号/分号/句号/问号/感叹号/换行），顿号「、」
    不算边界——"B、C"保持为同一条子句（2026-10-04-v5）。"""
    assert judge.split_appearance_clauses("A，B、C；D。E！F？G") == ["A", "B、C", "D", "E", "F", "G"]


def test_split_appearance_clauses_does_not_split_on_touhao() -> None:
    """真实缺陷（2026-10-03 沙箱实测 123 张卡命中 3 次，第 4 轮改成"按顿号切 +
    否定词开头特判合并"后 4 张待救回卡仍只救回 1 张）："表面无印花、刺绣等
    额外装饰""衣身无印花、刺绣等额外装饰"这类否定词不在句首的真实写法，旧的
    否定合并识别不到；改成不按顿号切分后，这类句子天然就是一条完整子句，
    不需要再识别任何否定词模式。"""
    assert judge.split_appearance_clauses("表面无印花、刺绣等额外装饰") == ["表面无印花、刺绣等额外装饰"]
    assert judge.split_appearance_clauses("衣身无印花、刺绣等额外装饰") == ["衣身无印花、刺绣等额外装饰"]


def test_split_appearance_clauses_splits_on_period_too() -> None:
    assert judge.split_appearance_clauses("米白色针织。合身版型") == ["米白色针织", "合身版型"]


def test_split_appearance_clauses_keeps_decimal_point_inside_clause() -> None:
    """英文句点是小数点（真实卡「正面为6.7英寸超窄边框全面屏」），不是子句边界。"""
    text = "正面为6.7英寸超窄边框全面屏，直径约2.5cm。"
    assert judge.split_appearance_clauses(text) == ["正面为6.7英寸超窄边框全面屏", "直径约2.5cm"]
    assert judge.rebuild_appearance_excluding(text, set()) == text


def test_rebuild_excluding_tail_clause() -> None:
    assert judge.rebuild_appearance_excluding("浅灰色卫衣，棉质，胸前有星盘压痕", {3}) == "浅灰色卫衣，棉质"


def test_rebuild_excluding_middle_clause_reuses_following_separator() -> None:
    assert judge.rebuild_appearance_excluding("A，B，C", {2}) == "A，C"


def test_rebuild_excluding_all_returns_empty() -> None:
    assert judge.rebuild_appearance_excluding("A，B", {1, 2}) == ""


def test_rebuild_excluding_none_returns_unchanged() -> None:
    assert judge.rebuild_appearance_excluding("A，B，C", set()) == "A，B，C"


def test_rebuild_excluding_preserves_trailing_sentence_punctuation_when_last_clause_kept() -> None:
    """真实回归（B 上 123 张卡实测，123 张里 53 张"什么都不删"时结尾的句末
    标点会被静默吞掉）：句末标点落在最后一条子句的 span 之外，此前的拼接
    循环只在"当前子句不是本次输出最后一项"时才补分隔符，这对"删除尾部子句"
    是对的，但原文真正的最后一条子句被保留时（最常见的"什么都没删"场景），
    它后面的句末标点会被同一条判断误伤而丢失——必须原样补回去。"""
    text = "A，B。"
    assert judge.rebuild_appearance_excluding(text, set()) == text
    assert judge.rebuild_appearance_excluding(text, {1}) == "B。"


def test_rebuild_excluding_whole_touhao_clause_keeps_zero_new_characters() -> None:
    """顿号不切分，整条含顿号的子句被删除时，零新增字符的拼接约束依旧成立。"""
    text = "米白色针织，合身版型，无印花、刺绣等额外装饰"
    assert judge.rebuild_appearance_excluding(text, {3}) == "米白色针织，合身版型"


def test_rebuild_excluding_keep_fragment_within_touhao_clause() -> None:
    """"原生心形翠绿色叶片约三分之二边缘发黑发蔫、部分枝条软垂倒伏"整句是
    一条子句（内部顿号不切分），混合了植物固有外观与泡水后的剧情时点状态，
    靠 ``keep_fragment`` 保留"原生心形翠绿色叶片"这一小段，不是靠切分本身
    区分出两部分。"""
    text = "原生心形翠绿色叶片约三分之二边缘发黑发蔫、部分枝条软垂倒伏"
    assert judge.split_appearance_clauses(text) == [text]
    assert judge.rebuild_appearance_excluding(text, {1}, {1: "原生心形翠绿色叶片"}) == "原生心形翠绿色叶片"


# ---------------------------------------------------------------------------
# 2) card_audit_rules：子句/别名判定的代码核验（越界、重复、改写企图、非法类别）
# ---------------------------------------------------------------------------

def test_verify_clause_removal_rejects_out_of_range_index() -> None:
    removed, records, missing, doubts = card_audit_rules.verify_clause_removal_verdicts(
        ["甲", "乙"], [{"index": 5, "remove": True, "category": "plot_state", "reason": "x"}], frozenset(),
        cooccurring_owners=frozenset(), all_props=[],
    )
    assert removed == set() and records == [] and doubts == []
    assert missing == {1, 2}


def test_verify_clause_removal_downgrades_invalid_category_to_doubt() -> None:
    """类别不在三选一范围内：不采信删除，也不像此前那样静默丢弃（审查发现）。"""
    removed, _records, missing, doubts = card_audit_rules.verify_clause_removal_verdicts(
        ["甲", "乙"], [{"index": 1, "remove": True, "category": "made_up_category", "reason": "x"}], frozenset(),
        cooccurring_owners=frozenset(), all_props=[],
    )
    assert removed == set() and missing == {2}
    assert doubts[0]["doubt_type"] == card_audit_rules.DOUBT_TYPE_INVALID_CATEGORY


def test_verify_clause_removal_ignores_duplicate_index_keeps_first() -> None:
    removed, records, missing, _doubts = card_audit_rules.verify_clause_removal_verdicts(
        ["甲", "乙"], [
            {"index": 1, "remove": True, "category": "plot_state", "reason": "first"},
            {"index": 1, "remove": True, "category": "not_appearance", "reason": "dup"},
        ], frozenset(), cooccurring_owners=frozenset(), all_props=[],
    )
    assert removed == {1}
    assert len(records) == 1 and records[0]["reason"] == "first"
    assert missing == {2}


def test_verify_clause_removal_accepts_valid_removal_with_verified_owner() -> None:
    """owner 归属证据核验另见 ``test_prop_card_audit_consensus.py``。"""
    removed, records, missing, doubts = card_audit_rules.verify_clause_removal_verdicts(
        ["甲", "压痕", "丙"],
        [{"index": 2, "remove": True, "category": "other_object_or_mark", "owner": "星盘", "reason": "压痕"}],
        frozenset({"星盘"}), cooccurring_owners=frozenset({"星盘"}), all_props=[],
    )
    assert removed == {2}
    assert records[0]["category"] == "other_object_or_mark"
    assert missing == {1, 3} and doubts == []


def test_verify_clause_removal_ignores_non_dict_entries() -> None:
    removed, _records, missing, _doubts = card_audit_rules.verify_clause_removal_verdicts(
        ["甲", "乙"], ["not-a-dict", {"index": 1, "remove": True, "category": "plot_state", "reason": "x"}],
        frozenset(), cooccurring_owners=frozenset(), all_props=[],
    )
    assert removed == {1}
    assert missing == {2}


def test_verify_alias_removal_requires_membership_in_current_aliases() -> None:
    out = card_audit_rules.verify_alias_removal_verdicts(
        ["椅子", "木椅"], [{"alias": "不存在的别名", "category_only": True, "reason": "x"}],
    )
    assert out == []


def test_verify_alias_removal_accepts_valid_category_only() -> None:
    out = card_audit_rules.verify_alias_removal_verdicts(
        ["椅子"], [{"alias": "椅子", "category_only": True, "reason": "只剩品类名"}],
    )
    assert [r["alias"] for r in out] == ["椅子"]


def test_verify_alias_removal_ignores_non_category_only() -> None:
    out = card_audit_rules.verify_alias_removal_verdicts(
        ["咖啡馆木椅"], [{"alias": "咖啡馆木椅", "category_only": False, "reason": "x"}],
    )
    assert out == []


def test_verify_alias_removal_ignores_non_dict_entries() -> None:
    out = card_audit_rules.verify_alias_removal_verdicts(
        ["椅子"], ["椅子", {"alias": "椅子", "category_only": True, "reason": "x"}],
    )
    assert [r["alias"] for r in out] == ["椅子"]


# ---------------------------------------------------------------------------
# 3) card_audit_rules.ambiguous_aliases_to_drop：数据推导歧义剪除
# ---------------------------------------------------------------------------

def test_ambiguous_aliases_drops_alias_matching_another_cards_name() -> None:
    cards = [
        Prop(name="外套", appearance_canonical="x", aliases=[]),
        Prop(name="顾屿外套", appearance_canonical="y", aliases=["外套"]),
    ]
    out = card_audit_rules.ambiguous_aliases_to_drop(cards[1], cards)
    assert [r["alias"] for r in out] == ["外套"]
    assert out[0]["source"] == "ambiguous_data"


def test_ambiguous_aliases_drops_alias_shared_by_two_cards() -> None:
    cards = [
        Prop(name="椅子甲", appearance_canonical="x", aliases=["椅子"]),
        Prop(name="椅子乙", appearance_canonical="y", aliases=["椅子"]),
    ]
    out = card_audit_rules.ambiguous_aliases_to_drop(cards[0], cards)
    assert [r["alias"] for r in out] == ["椅子"]


def test_ambiguous_aliases_keeps_unique_alias() -> None:
    cards = [Prop(name="旧猫包", appearance_canonical="x", aliases=["旧包"])]
    assert card_audit_rules.ambiguous_aliases_to_drop(cards[0], cards) == []


# ---------------------------------------------------------------------------
# 4) compute_prop_card_audit：三类真实场景 + 全删 failed + 特征不足标注
# ---------------------------------------------------------------------------

def _mock_judgment(monkeypatch: pytest.MonkeyPatch, clauses: list[dict], aliases: list[dict] | None = None) -> None:
    """两次独立调用都打同一个桩，模拟"两次判定一致"（不一致场景见
    ``test_prop_card_audit_consensus.py``）。"""
    async def _fake(_prop, _clauses, _owner_catalog_text, **_kwargs):
        return {"clauses": clauses, "aliases": aliases or []}
    monkeypatch.setattr(card_audit.card_audit_rules, "request_prop_card_audit_judgment", _fake)


async def test_compute_audit_removes_plot_state_clause(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实案例：绿萝泡水后发蔫的样子是时点状态，不是固有外观。"""
    prop = Prop(name="绿萝", appearance_canonical="心形翠绿色叶片，米白色哑光塑料花盆，叶片边缘发黑发蔫", aliases=[])
    _mock_judgment(monkeypatch, [
        {"index": 1, "remove": False}, {"index": 2, "remove": False},
        {"index": 3, "remove": True, "category": "plot_state", "reason": "泡水后才发蔫"},
    ])
    result = await card_audit.compute_prop_card_audit(prop, [prop], label_segments={})
    assert result["new_appearance"] == "心形翠绿色叶片，米白色哑光塑料花盆"
    assert result["removed_clauses"][0]["category"] == "plot_state"


async def test_compute_audit_keeps_inherent_patina_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """固有旧化/包浆保留：模型全部判定不删，外观不变、不触发重出图。"""
    prop = Prop(name="旧星盘", appearance_canonical="黄铜材质，表面常年使用留下浅划痕，边缘略有磕碰", aliases=[])
    clauses = judge.split_appearance_clauses(prop.appearance_canonical)
    _mock_judgment(monkeypatch, [{"index": i + 1, "remove": False} for i in range(len(clauses))])
    result = await card_audit.compute_prop_card_audit(prop, [prop], label_segments={})
    assert result["appearance_changed"] is False
    assert result["new_appearance"] == prop.appearance_canonical


async def test_compute_audit_all_removed_marks_failed_without_changing_appearance(monkeypatch: pytest.MonkeyPatch) -> None:
    prop = Prop(name="纸箱", appearance_canonical="黄褐色瓦楞纸材质，整体被水泡软塌陷", aliases=[])
    clauses = judge.split_appearance_clauses(prop.appearance_canonical)
    _mock_judgment(monkeypatch, [
        {"index": i + 1, "remove": True, "category": "plot_state", "reason": "泡水后"} for i in range(len(clauses))
    ])
    result = await card_audit.compute_prop_card_audit(prop, [prop], label_segments={})
    assert result["failed"] is True
    assert result["new_appearance"] == prop.appearance_canonical
    assert result["appearance_changed"] is False


async def test_compute_audit_feature_shortfall_flagged_when_below_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    prop = Prop(name="道具X", appearance_canonical="红色，圆形，带柄把", aliases=[])
    clauses = judge.split_appearance_clauses(prop.appearance_canonical)
    assert len(clauses) == judge.MIN_APPEARANCE_FEATURES
    _mock_judgment(monkeypatch, [
        {"index": 1, "remove": False}, {"index": 2, "remove": False},
        {"index": 3, "remove": True, "category": "not_appearance", "reason": "x"},
    ])
    result = await card_audit.compute_prop_card_audit(prop, [prop], label_segments={})
    assert result["feature_shortfall"] is True
    assert result["new_appearance"] == "红色，圆形"


async def test_compute_audit_combines_ambiguous_and_model_nominated_aliases_without_duplicating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = Prop(name="椅子乙", appearance_canonical="x", aliases=["椅子"])
    prop = Prop(name="对面木椅", appearance_canonical="深色木质，靠背雕花，扶手处有裂纹", aliases=["椅子", "咖啡馆木椅"])
    clauses = judge.split_appearance_clauses(prop.appearance_canonical)
    _mock_judgment(
        monkeypatch, [{"index": i + 1, "remove": False} for i in range(len(clauses))],
        [
            {"alias": "咖啡馆木椅", "category_only": False, "reason": "可指认"},
            {"alias": "椅子", "category_only": True, "reason": "只剩品类名"},
        ],
    )
    result = await card_audit.compute_prop_card_audit(prop, [prop, other], label_segments={})
    removed = {r["alias"] for r in result["removed_aliases"]}
    assert removed == {"椅子"}
    assert "咖啡馆木椅" not in removed


# ---------------------------------------------------------------------------
# 5) _apply_audit_result：原子重出图（失败时旧外观/旧图原封不动）
# ---------------------------------------------------------------------------

def _base_result(old: str, new: str) -> dict:
    return {
        "prop_name": "卫衣", "old_appearance": old, "new_appearance": new,
        "appearance_changed": old != new, "removed_clauses": [], "removed_aliases": [],
        "feature_shortfall": False, "failed": False, "fail_reason": None, "reimaged": False,
    }


async def test_apply_audit_result_writes_new_appearance_on_successful_reimage(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_project("p-apply-ok", props_list=[{
        "name": "卫衣", "appearance_canonical": "灰色、棉质、胸前有压痕", "aliases": [], "ref_image_path": "/old.png",
    }])
    async def _fake_generate(_project_id, _name, _prompt):
        return "/fake/new.png"
    monkeypatch.setattr(card_audit.image, "generate_prop_reference_image", _fake_generate)
    conn = get_conn()
    prop = Prop(name="卫衣", appearance_canonical="灰色、棉质、胸前有压痕", aliases=[], ref_image_path="/old.png")
    applied = await card_audit._apply_audit_result(
        conn, "p-apply-ok", prop, _base_result("灰色、棉质、胸前有压痕", "灰色、棉质"), style="写实", episode_no=1,
    )
    assert applied["reimaged"] is True
    bible = json.loads(conn.execute("SELECT bible_json FROM projects WHERE id='p-apply-ok'").fetchone()["bible_json"])
    card = bible["props"][0]
    assert card["appearance_canonical"] == "灰色、棉质"
    assert card["ref_image_path"] == "/fake/new.png"


async def test_apply_audit_result_keeps_old_appearance_when_reimage_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_project("p-apply-fail", props_list=[{
        "name": "卫衣", "appearance_canonical": "灰色、棉质、胸前有压痕", "aliases": [], "ref_image_path": "/old.png",
    }])
    async def _fail_generate(_project_id, _name, _prompt):
        return None
    monkeypatch.setattr(card_audit.image, "generate_prop_reference_image", _fail_generate)
    conn = get_conn()
    prop = Prop(name="卫衣", appearance_canonical="灰色、棉质、胸前有压痕", aliases=[], ref_image_path="/old.png")
    applied = await card_audit._apply_audit_result(
        conn, "p-apply-fail", prop, _base_result("灰色、棉质、胸前有压痕", "灰色、棉质"), style="写实", episode_no=1,
    )
    assert applied["reimaged"] is False
    bible = json.loads(conn.execute("SELECT bible_json FROM projects WHERE id='p-apply-fail'").fetchone()["bible_json"])
    card = bible["props"][0]
    assert card["appearance_canonical"] == "灰色、棉质、胸前有压痕"
    assert card["ref_image_path"] == "/old.png"


# ---------------------------------------------------------------------------
# 6) audit_one_prop_card / claim_audit：CAS、规则版本前进重新复核、失败重试上限
# ---------------------------------------------------------------------------

def _patch_no_change_judgment(monkeypatch: pytest.MonkeyPatch, counter: dict | None = None):
    """模拟模型对每条子句都显式给出"不删除"的判定——而不是返回空列表。空列表
    会被新的"覆盖不全"核验（``verify_clause_removal_verdicts`` 的
    ``missing_indexes``）判定成"模型什么都没判定"从而走失败重试分支，不是
    "模型判定全部保留"；必须按真实契约（对每条子句都要给出判定）构造桩数据。"""
    async def _fake(_prop, clauses, _owner_catalog_text, **_kwargs):
        if counter is not None:
            counter["n"] = counter.get("n", 0) + 1
        return {"clauses": [{"index": i + 1, "remove": False} for i in range(len(clauses))], "aliases": []}
    monkeypatch.setattr(card_audit.card_audit_rules, "request_prop_card_audit_judgment", _fake)


async def test_audit_one_prop_card_marks_ready_with_no_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_no_change_judgment(monkeypatch)
    _seed_project("p-audit-1", props_list=[{"name": "道具A", "appearance_canonical": "红色、圆形、带柄把", "aliases": []}])
    result = await card_audit.audit_one_prop_card("p-audit-1", "道具A", dry_run=False)
    assert result["status"] == "ready"
    row = card_audit_store.get_audit(get_conn(), project_id="p-audit-1", prop_name="道具A")
    assert row["status"] == "ready"
    assert row["rules_version"] == judge.PROP_CARD_RULES_VERSION


async def test_audit_one_prop_card_missing_prop_raises() -> None:
    _seed_project("p-audit-2")
    with pytest.raises(ValueError):
        await card_audit.audit_one_prop_card("p-audit-2", "不存在", dry_run=False)


async def test_audit_one_prop_card_dry_run_does_not_write_store_or_bible(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_no_change_judgment(monkeypatch)
    _seed_project("p-audit-dry", props_list=[{"name": "道具A", "appearance_canonical": "红色、圆形、带柄把", "aliases": []}])
    result = await card_audit.audit_one_prop_card("p-audit-dry", "道具A", dry_run=True)
    assert "new_appearance" in result
    assert card_audit_store.get_audit(get_conn(), project_id="p-audit-dry", prop_name="道具A") is None


async def test_audit_one_prop_card_skips_when_already_ready_under_current_version(monkeypatch: pytest.MonkeyPatch) -> None:
    counter: dict = {}
    _patch_no_change_judgment(monkeypatch, counter)
    _seed_project("p-audit-3", props_list=[{"name": "道具A", "appearance_canonical": "红色、圆形、带柄把", "aliases": []}])
    await card_audit.audit_one_prop_card("p-audit-3", "道具A", dry_run=False)
    assert counter["n"] == 2  # 两次独立判定调用（见 card_audit_consensus）
    result2 = await card_audit.audit_one_prop_card("p-audit-3", "道具A", dry_run=False)
    assert result2.get("skipped") is True
    assert counter["n"] == 2


async def test_audit_one_prop_card_rules_version_advance_triggers_recheck(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_no_change_judgment(monkeypatch)
    _seed_project("p-audit-4", props_list=[{"name": "道具A", "appearance_canonical": "红色、圆形、带柄把", "aliases": []}])
    await card_audit.audit_one_prop_card("p-audit-4", "道具A", dry_run=False)
    monkeypatch.setattr(judge, "PROP_CARD_RULES_VERSION", "9999-99-99")
    result = await card_audit.audit_one_prop_card("p-audit-4", "道具A", dry_run=False)
    assert result.get("status") == "ready"
    row = card_audit_store.get_audit(get_conn(), project_id="p-audit-4", prop_name="道具A")
    assert row["rules_version"] == "9999-99-99"


async def test_audit_one_prop_card_marks_failed_on_exception_and_stops_retrying_after_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raise(_prop, _clauses):
        raise RuntimeError("模型超时")
    monkeypatch.setattr(card_audit.card_audit_rules, "request_prop_card_audit_judgment", _raise)
    _seed_project("p-audit-5", props_list=[{"name": "道具A", "appearance_canonical": "红色、圆形、带柄把", "aliases": []}])
    for _ in range(card_audit.MAX_AUDIT_ATTEMPTS):
        result = await card_audit.audit_one_prop_card("p-audit-5", "道具A", dry_run=False)
        assert result["status"] == "failed"
    row = card_audit_store.get_audit(get_conn(), project_id="p-audit-5", prop_name="道具A")
    assert row["attempts"] == card_audit.MAX_AUDIT_ATTEMPTS
    final = await card_audit.audit_one_prop_card("p-audit-5", "道具A", dry_run=False)
    assert final.get("skipped") is True


# ---------------------------------------------------------------------------
# 7) 审查发现修复：否定并列短语不按顿号切分、覆盖不全判失败、CAS 写回围栏、
#    launch_background_audit 并发限流
# ---------------------------------------------------------------------------

async def test_compute_audit_negation_linked_clause_judged_as_single_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"无印花、刺绣等额外装饰"整句交给模型一次性判定，顿号不切分，结构上
    不再可能出现"只删后半句、留下前半句"的半删场景（2026-10-04-v5）。"""
    prop = Prop(name="开衫", appearance_canonical="米白色针织，合身版型，无印花、刺绣等额外装饰", aliases=[])
    clauses = judge.split_appearance_clauses(prop.appearance_canonical)
    assert clauses == ["米白色针织", "合身版型", "无印花、刺绣等额外装饰"]
    _mock_judgment(monkeypatch, [
        {"index": 1, "remove": False}, {"index": 2, "remove": False},
        {"index": 3, "remove": True, "category": "not_appearance", "reason": "切分残留"},
    ])
    result = await card_audit.compute_prop_card_audit(prop, [prop], label_segments={})
    assert result["new_appearance"] == "米白色针织，合身版型"
    assert result["appearance_changed"] is True


async def test_compute_audit_partial_coverage_marks_failed_without_applying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型只对部分子句给出判定——不采用本轮结果，整体按失败处理待重试，
    不得把缺失的判定静默当"不删除"。"""
    prop = Prop(name="道具P", appearance_canonical="红色，圆形，带柄把", aliases=[])
    _mock_judgment(monkeypatch, [{"index": 1, "remove": True, "category": "not_appearance", "reason": "x"}])
    result = await card_audit.compute_prop_card_audit(prop, [prop], label_segments={})
    assert result["failed"] is True
    assert result["new_appearance"] == prop.appearance_canonical
    assert result["removed_clauses"] == [] and result["removed_aliases"] == []
    assert "2" in result["fail_reason"] and "3" in result["fail_reason"]


async def test_mark_audit_ready_discards_stale_write_after_reclaim(monkeypatch: pytest.MonkeyPatch) -> None:
    """CAS 围栏：这一行在原调用完成前被重新抢占（claim_token 已改写），原调用
    仍试图用旧 claim_token 写回，必须被丢弃，不能把过期结果覆盖回新一轮。"""
    _seed_project("p-fence-1", props_list=[{"name": "道具F", "appearance_canonical": "红色、圆形、带柄把", "aliases": []}])
    row1, should_run1 = await card_audit.claim_audit(project_id="p-fence-1", prop_name="道具F", rules_version="V1")
    assert should_run1
    row2, should_run2 = await card_audit.claim_audit(project_id="p-fence-1", prop_name="道具F", rules_version="V2")
    assert should_run2
    assert row2["claim_token"] != row1["claim_token"]
    await card_audit._mark_audit_ready(
        row_id=row1["id"], claim_token=row1["claim_token"], result=_base_result("红色、圆形、带柄把", "过期结果"),
    )
    row = card_audit_store.get_audit(get_conn(), project_id="p-fence-1", prop_name="道具F")
    assert row["status"] == "running"  # 过期写回被丢弃，没有把新一轮覆盖成 ready
    assert row["rules_version"] == "V2"
    await card_audit._mark_audit_ready(
        row_id=row2["id"], claim_token=row2["claim_token"], result=_base_result("红色、圆形、带柄把", "新一轮结果"),
    )
    row = card_audit_store.get_audit(get_conn(), project_id="p-fence-1", prop_name="道具F")
    assert row["status"] == "ready" and row["new_appearance"] == "新一轮结果"
