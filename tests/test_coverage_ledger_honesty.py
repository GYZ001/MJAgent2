"""覆盖账本诚实性（WS6 + 追加条目）。

part 1：``_prep_pack_build_coverage_ledger``（五账）+
``_prep_pack_finalize_scene_coverage``（场景专项覆盖账 ``scene_coverage``，
必须等资产解析/provenance 自校验都跑完才能诚实计算，见两个函数各自的
docstring）——结构性保证 ``retained_as_context`` 恒为
``scene_coverage.scene_uncovered`` 的子集，不再是"两处分开算、指望它们碰巧
一致"的默契。

真实缺陷（WS6 派单描述，映射台真实产出）：第 2 集 11 段里 scene_delivered=
[7,8]，scene_uncovered=[2,3,4,5,6,9,10]，这些段落被记成 retained_as_context，
分镜台随后给一段荣誉清单独白配了错误场景「校园食堂」。本文件钉住这个真实
场景形状：retained_as_context 必须诚实地等于（不多不少）scene_uncovered 里
"既没有场景、也没有其它任何素材维度覆盖"的那部分，不能有遗漏或多算。

part 2（协调方追加条目）：``app.production.prep_pack.scene_degrade``——场景
未解析到 scene_reference_id / provenance 自校验失败，此前整批
``PrepPackGateError`` 判死整集（B 上真实实例：橘座在上 ×2、神墓 ×1），现在
原地降级为可见记录、不阻断，且必须正确落进 scene_coverage.scene_uncovered。
两条回归测试直接用 B 上那三条真实错误文本做 fixture。
"""
from __future__ import annotations

from app.production import prep_pack
from app.production.prep_pack import scene_degrade


def _ledger(total: int, delivered: set[int], paratext: set[int], scene_delivered: set[int]):
    """两步 API 的测试便捷封装：先建五账，再用（已排除降级条目的）场景交付
    集合诚实重算 scene_coverage——跟 _generate_prep_pack_once 里的真实调用
    顺序一致（先 ledger，资产解析完再 finalize_scene_coverage）。"""
    ledger, rejected = prep_pack._prep_pack_build_coverage_ledger(total, delivered, paratext)
    prep_pack._prep_pack_finalize_scene_coverage(ledger, total, scene_delivered, paratext)
    return ledger, rejected


# ---------------------------------------------------------------------------
# part 1：五账 + scene_coverage 的诚实性
# ---------------------------------------------------------------------------


def test_real_defect_reproduction_ep2_11_segments() -> None:
    """逐字复现派单给出的真实映射台产出：第 2 集共 11 段，1/11 是确定性副文本
    （章节标题/尾注），7/8 两段有场景交付，2~6/9/10 这 7 段既没有场景、也没有
    任何其它维度的交付（模型对"荣誉清单独白"这类段落连角色都没报出来——这正是
    分镜台随后给它配错场景「校园食堂」的根因：账本诚实地记下了空白，但
    downstream 没有正确处理这个信号，不是账本本身算错）。"""
    ledger, rejected = _ledger(11, {7, 8}, {1, 11}, {7, 8})
    assert rejected == []
    assert ledger["scene_coverage"]["scene_delivered"] == [7, 8]
    assert ledger["scene_coverage"]["scene_uncovered"] == [2, 3, 4, 5, 6, 9, 10]
    assert ledger["retained_as_context"] == [2, 3, 4, 5, 6, 9, 10]
    # 诚实性的核心断言：这个真实场景下两账逐字相等（不只是子集关系）——
    # 没有场景交付的段落，恰好就是"什么素材都没有"的段落，退化案例。
    assert ledger["retained_as_context"] == ledger["scene_coverage"]["scene_uncovered"]


def test_character_delivered_segment_is_not_retained_as_context_but_still_scene_uncovered() -> None:
    """核心区分：一个段落有角色/道具交付（例如"荣誉清单独白"——有人在说话）时，
    它不再进 retained_as_context（五账语义：至少一个维度覆盖到就不算"什么都
    没有"），但只要没有场景交付，它必须仍然出现在 scene_coverage.scene_uncovered
    里——这正是"诚实"的核心：分镜台/生成台不能因为 retained_as_context 里看不到
    这一段，就误以为它不需要场景。"""
    ledger, _ = _ledger(11, {2, 3, 4, 5, 6, 9, 10, 7, 8}, set(), {7, 8})
    for idx in (2, 3, 4, 5, 6, 9, 10):
        assert idx not in ledger["retained_as_context"], (
            "这一段有角色交付，不应被记成'什么素材都没有'的 retained_as_context"
        )
        assert idx in ledger["scene_coverage"]["scene_uncovered"], (
            "但场景维度确实没有交付，必须仍然在 scene_uncovered 里可见，"
            "不能因为角色维度覆盖了就从场景缺口信号里消失"
        )


def test_retained_as_context_is_always_subset_of_scene_uncovered() -> None:
    """结构性不变量本身（覆盖账本诚实性的最小保证）：无论 delivered/scene_
    delivered/paratext 如何组合，retained_as_context 永远不会比 scene_uncovered
    更"乐观"——这条不变量由 _prep_pack_finalize_scene_coverage 内部的 assert
    钉死，本测试从外部黑盒角度覆盖若干组合，确认调用本身不会抛异常、且集合
    关系成立。"""
    cases = [
        (10, set(), set(), set()),
        (10, {1, 2, 3}, set(), {2, 3}),
        (10, {1, 2, 3, 4, 5}, {10}, {2, 3}),
        (10, set(range(1, 11)), set(), set(range(1, 11))),
        (1, set(), set(), set()),
    ]
    for total, delivered, paratext, scene_delivered in cases:
        ledger, _ = _ledger(total, delivered, paratext, scene_delivered)
        retained = set(ledger["retained_as_context"])
        scene_uncovered = set(ledger["scene_coverage"]["scene_uncovered"])
        assert retained.issubset(scene_uncovered)


def test_scene_coverage_total_segments_matches_top_level() -> None:
    ledger, _ = _ledger(5, {1}, set(), {1})
    assert ledger["scene_coverage"]["total_segments"] == ledger["total_segments"] == 5


def test_paratext_still_wins_over_unclaimed_scene_delivery() -> None:
    """paratext（确定性投影，如章节标题）优先于"没有场景交付"：既有五账语义不变
    ——paratext 段落既不进 retained_as_context 也不进 scene_uncovered。"""
    ledger, _ = _ledger(5, set(), {1, 5}, set())
    assert 1 not in ledger["retained_as_context"]
    assert 5 not in ledger["retained_as_context"]
    assert 1 not in ledger["scene_coverage"]["scene_uncovered"]
    assert 5 not in ledger["scene_coverage"]["scene_uncovered"]
    assert ledger["scene_coverage"]["scene_uncovered"] == [2, 3, 4]


def test_rejected_paratext_claims_unaffected_by_scene_coverage_split() -> None:
    """paratext 声明与 delivered 冲突时 delivered 优先、记入 rejected——这条既有
    行为在拆分出 finalize_scene_coverage 之后必须逐字不变（回归保护）。"""
    ledger, rejected = _ledger(5, {3}, {3}, set())
    assert rejected == [3]
    assert 3 in ledger["delivered"]
    assert 3 not in ledger["paratext"]


# ---------------------------------------------------------------------------
# part 2（协调方追加条目）：未解析/自校验失败场景的就地降级
# ---------------------------------------------------------------------------

# B 上真实错误文本（2026-09-02，只读查询 step_runs.error_message）。

# 神墓 proj_facfc3964f69，ERR-20260902-033f75：resolve_assets 阶段两个场景
# 从未解析到 scene_reference_id，此前整批 PrepPackGateError 判死整集。
_SHENMU_RESOLVE_FAILURE_REASON = (
    "场景「神魔陵园」（段 [1, 2, 3]）未解析到已有 scene_reference_id"
)

# 橘座在上 proj_5a2ab19ef388，ERR-20260901-8ebc2d：provenance 自校验阶段
# anchor_phrase 缺失，此前同样整批判死整集。
_JUZUO_PROVENANCE_FAILURE_REASON = (
    "场景「金猫拍卖会场」的 provenance.method='resolution' 缺少 anchor_phrase"
    "——resolution/discovery 绑定必须有本集文本依据，来源证明自校验失败，门禁具名拦截"
)


def test_real_resolve_failure_degrades_instead_of_blocking() -> None:
    """真实回归（神墓 ERR-20260902-033f75）：修复前，这条错误单独出现在
    asset_errors 里就会让 _generate_prep_pack_once 整批 raise；修复后，
    split_scene_errors 必须把它归为"已就地降级、不再阻断"一类（blocking 为
    空），manifest 由 degrade_unresolved_scene 就地写出一条诚实、可见的
    unresolved 记录，且不占用 scene_coverage 的 scene_delivered 名额。"""
    scenes: dict[str, dict] = {}
    scene_degrade.degrade_unresolved_scene(
        scenes, name="神魔陵园", segment_indexes=[1, 2, 3], reason=_SHENMU_RESOLVE_FAILURE_REASON,
    )
    degraded, blocking = scene_degrade.split_scene_errors([_SHENMU_RESOLVE_FAILURE_REASON])
    assert blocking == [], "场景未解析失败修复后不应再阻断整集发布"
    assert degraded == [_SHENMU_RESOLVE_FAILURE_REASON]
    entry = scenes["scene:unresolved:神魔陵园"]
    assert entry["unresolved"] is True
    assert entry["asset_required"] is False
    assert entry["segment_indexes"] == [], "降级条目不得再喂给分镜台当可用场景候选"
    assert entry["attempted_segment_indexes"] == [1, 2, 3]
    assert entry["reason"] == _SHENMU_RESOLVE_FAILURE_REASON
    assert "映射台" in entry["resolution_hint"] and "神魔陵园" in entry["resolution_hint"]
    # 覆盖账本诚实性：这些段号必须落进 scene_uncovered，不能因为"模型确实
    # 报过一条场景提及"就被计成已覆盖。
    scene_delivered = scene_degrade.resolved_scene_delivered_indexes(list(scenes.values()))
    assert scene_delivered == set()
    _ledger_result, _ = _ledger(3, set(), set(), scene_delivered)
    assert _ledger_result["scene_coverage"]["scene_uncovered"] == [1, 2, 3]


def test_real_provenance_failure_degrades_instead_of_blocking() -> None:
    """真实回归（橘座在上 ERR-20260901-8ebc2d）：修复前，provenance 自校验
    失败会让 _generate_prep_pack_once 整批 raise；修复后，已发布的 manifest
    entry 被原地降级（不是删除——观测者仍能看到这条场景曾经"几乎"解析成功，
    只是证据链验不过），non-scene 错误列表为空即不再阻断。"""
    asset_manifest = {"scenes": [{
        "scene_id": "scene:金猫拍卖会场", "display_name": "金猫拍卖会场",
        "scene_reference_id": "sr-jinmao", "segment_indexes": [4, 5],
        "provenance": {"method": "resolution", "anchor_segments": [], "anchor_phrase": ""},
    }]}
    remaining = scene_degrade.degrade_scene_provenance_failures(
        asset_manifest, [_JUZUO_PROVENANCE_FAILURE_REASON],
    )
    assert remaining == [], "场景 provenance 自校验失败修复后不应再阻断整集发布"
    entry = asset_manifest["scenes"][0]
    assert entry["unresolved"] is True
    assert entry["asset_required"] is False
    assert entry["segment_indexes"] == [], "降级条目不得再喂给分镜台当可用场景候选"
    assert entry["attempted_segment_indexes"] == [4, 5]
    assert entry["reason"] == _JUZUO_PROVENANCE_FAILURE_REASON
    assert entry["scene_reference_id"] == "sr-jinmao", "保留原绑定供人工核对，不静默清空"
    scene_delivered = scene_degrade.resolved_scene_delivered_indexes(asset_manifest["scenes"])
    assert scene_delivered == set()


def test_character_errors_still_block_scene_errors_do_not() -> None:
    """split_scene_errors 的分流边界：非场景（角色/道具/群演）错误必须继续
    出现在 blocking 分组——本次改动范围只覆盖场景。"""
    character_error = "角色「神秘蒙面人」（段 [1]）未解析到已有 portrait_id，身份消歧也未能将其归类为已有角色或确定性群演"
    degraded, blocking = scene_degrade.split_scene_errors([
        _SHENMU_RESOLVE_FAILURE_REASON, character_error,
    ])
    assert degraded == [_SHENMU_RESOLVE_FAILURE_REASON]
    assert blocking == [character_error]
