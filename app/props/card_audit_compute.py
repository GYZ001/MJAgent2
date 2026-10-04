"""道具卡复核的纯计算核心：给定一张道具卡 + 两次独立模型判定的原始返回值，
算出"最终该删哪些子句/别名、新外观长什么样、有哪些存疑"——不写库、不出图，
只读不写（从 ``app.props.card_audit`` 拆出来，2026-10-04，避免该文件连续
超出 500 行文件行数基线；拆分边界按"纯计算"与"claim/DB 写回/后台编排"切，
本文件对 ``app.props.card_audit`` 没有任何反向依赖，不构成循环）。

``compute_prop_card_audit`` 供 ``app.props.card_audit`` 的真实写入路径
（``audit_one_prop_card``/批量入口）与 dry-run 路径共用同一份判定逻辑；两次
独立判定的取舍见 ``app.props.card_audit_consensus`` 模块 docstring。

2026-10-04-v4：此前这里有一层"否定关联子句组安全网"（``_apply_negation_
group_safety_net``），在 ``judge.split_appearance_clauses`` 按标点切分后
事后补救"无A、B"这类半删风险；v4 把那条合并逻辑前移到切分本身，安全网删除，
不留 ``negation_overrides`` 字段（CLAUDE.md「退场要一次删干净」）。
2026-10-04-v5：合并逻辑本身也已删除——第 4 轮沙箱实测发现"否定词开头+下一个
顿号单元合并"只认得否定词落在句首这一种写法，仍会漏救大半真实案例；根治是
不再按顿号切分，``split_appearance_clauses`` 改回只按句子边界切分，"无印花、
刺绣等额外装饰"这类并列否定短语天然就是一条完整子句，结构上不需要再识别
任何否定词模式（见 ``judge._clause_spans``）。``all_props`` 现在也会传给
``card_audit_consensus.merge_clause_judgments``，供 owner 归属证据的第二条
路径（卡名/别名逐字出现在子句原文里，见 ``app.props.card_audit_
cooccurrence`` 模块 docstring）核验。
"""
from __future__ import annotations

from typing import Any

from app.schemas import Prop

from . import card_audit_consensus, card_audit_cooccurrence, card_audit_rules, judge


def _partial_coverage_audit_result(prop: Prop, clauses: list[str], missing_indexes: frozenset[int]) -> dict[str, Any]:
    """两次调用里只要有一次没对全部子句给出判定——不采用本轮结果，整体按
    失败处理待重试（见 ``card_audit_rules.verify_clause_removal_verdicts``
    的说明）。"""
    judged = len(clauses) - len(missing_indexes)
    return {
        "prop_name": prop.name,
        "old_appearance": prop.appearance_canonical,
        "new_appearance": prop.appearance_canonical,
        "appearance_changed": False,
        "removed_clauses": [],
        "removed_aliases": [],
        "feature_shortfall": False,
        "failed": True,
        "fail_reason": (
            f"模型只对 {judged}/{len(clauses)} 条外观子句给出判定"
            f"（缺少编号：{sorted(missing_indexes)}），为安全起见本轮不采用，视为失败重试"
        ),
        "reimaged": False,
        "doubts": [],
    }


def _finalize_audit_result(
    prop: Prop, clauses: list[str], removed_indexes: set[int], removed_clauses: list[dict[str, Any]],
    removed_aliases: list[dict[str, Any]], doubts: list[dict[str, Any]], keep_fragments: dict[int, str],
) -> dict[str, Any]:
    """子句/别名两次判定都已合并完毕后，拼出最终外观与结果 dict——从
    ``compute_prop_card_audit`` 拆出来，保持两个函数都在单函数行数红线内。"""
    failed = False
    fail_reason: str | None = None
    if clauses and removed_indexes == set(range(1, len(clauses) + 1)) and not keep_fragments:
        new_appearance = prop.appearance_canonical
        failed = True
        fail_reason = "模型判定全部外观子句都该删除，保留原外观不改动，待人工核查"
    elif clauses:
        new_appearance = judge.rebuild_appearance_excluding(prop.appearance_canonical, removed_indexes, keep_fragments)
    else:
        new_appearance = prop.appearance_canonical

    # 用与新建卡同一口径的特征计数（``judge.description_feature_count``），不是
    # 复核子句切分（``split_appearance_clauses``）——2026-10-04-v5 后者改成只按
    # 句子边界切分，粒度比"至少 3 项特征"的判据粗得多，用它数特征数会让纯顿号
    # 连接的外观（"红色，圆形，带柄把"式常见写法之外、偶发只用顿号的旧卡）误报
    # 远超实际的 feature_shortfall；两处判据各管各的，见 judge 模块 docstring。
    new_feature_count = judge.description_feature_count(new_appearance)
    feature_shortfall = not failed and 0 < new_feature_count < judge.MIN_APPEARANCE_FEATURES
    appearance_changed = not failed and new_appearance != prop.appearance_canonical
    return {
        "prop_name": prop.name,
        "old_appearance": prop.appearance_canonical,
        "new_appearance": new_appearance,
        "appearance_changed": appearance_changed,
        "removed_clauses": removed_clauses,
        "removed_aliases": removed_aliases,
        "feature_shortfall": feature_shortfall,
        "failed": failed,
        "fail_reason": fail_reason,
        "reimaged": False,
        "doubts": doubts,
    }


async def compute_prop_card_audit(
    prop: Prop, all_props: list[Prop], kept_doubt_keys: frozenset[str] = frozenset(),
    *, label_segments: dict[str, frozenset[tuple[str, int]]],
) -> dict[str, Any]:
    """纯计算（两次独立模型调用 + 代码核验 + 两次取交集才真正删除），不写库、
    不出图——供 ``audit_one_prop_card`` 的真实写入路径与 dry-run 路径共用同
    一份判定逻辑。两次独立判定的取舍见 ``app.props.card_audit_consensus``
    模块 docstring；``kept_doubt_keys`` 是人工已经点过「保留」的存疑键（见
    ``card_audit_store.get_kept_doubt_keys``），dry-run 路径不读决定表，
    始终传空集合（纯计算，供沙箱验证）。``label_segments`` 是
    ``app.props.card_audit_cooccurrence.label_segment_keys_for_project`` 算好
    的「道具 label -> 它在哪些分镜段/原文段出现过」字典，必传（没有分镜/
    映射数据的项目传空字典即可，owner 共现核验会因此对全部候选保守地判定
    "未共现"，转存疑而不是跳过检查，见该模块 docstring）——调用方只需要算
    一次，不要求是纯计算之外的职责，这里只读不写。"""
    clauses = judge.split_appearance_clauses(prop.appearance_canonical)
    owner_catalog_text = card_audit_rules.catalog_text_for_prompt(prop, all_props)
    raw_a, raw_b = await card_audit_consensus.run_two_independent_clause_judgments(
        prop, clauses, owner_catalog_text,
    )
    other_identifiers = card_audit_rules.other_card_identifiers(prop, all_props)
    cooccurring_owners = card_audit_cooccurrence.cooccurring_owners_for_prop(
        label_segments, prop, other_identifiers, all_props,
    )
    clause_merge = card_audit_consensus.merge_clause_judgments(
        clauses, raw_a["clauses"], raw_b["clauses"], other_identifiers,
        cooccurring_owners=cooccurring_owners, all_props=all_props,
    )
    if clauses and clause_merge["missing_indexes"]:
        return _partial_coverage_audit_result(prop, clauses, clause_merge["missing_indexes"])
    removed_indexes = clause_merge["removed_indexes"]
    removed_clauses = clause_merge["removed_records"]
    keep_fragments = clause_merge["keep_fragments"]
    for record in removed_clauses:
        record["text"] = clauses[record["index"] - 1]

    ambiguous_aliases = card_audit_rules.ambiguous_aliases_to_drop(prop, all_props)
    alias_merge = card_audit_consensus.merge_alias_judgments(prop.aliases, raw_a["aliases"], raw_b["aliases"])
    seen_aliases = {record["alias"] for record in ambiguous_aliases}
    removed_aliases = [*ambiguous_aliases, *[r for r in alias_merge["removed"] if r["alias"] not in seen_aliases]]

    doubts = card_audit_consensus.filter_doubts_against_kept_decisions(
        [*clause_merge["doubts"], *alias_merge["doubts"]], kept_doubt_keys,
    )
    return _finalize_audit_result(
        prop, clauses, removed_indexes, removed_clauses, removed_aliases, doubts, keep_fragments,
    )
