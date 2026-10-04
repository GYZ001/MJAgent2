"""视频生成前「本次要生成段落用到的道具卡，复核版本是否落后」的懒复核判定
——触发点②的后半。前半"缺卡要不要补"仍在 ``app.props.card_pending_ensure``
（它判定"没有卡"），本模块只管"卡已经有，但按
``app.props.judge.PROP_CARD_RULES_VERSION`` 的现行规则还没复核过（或复核版
本落后）"这一半；两者由 ``card_pending_ensure.pending_prop_card_gate`` 合并
成同一条闸门消息，与场景状态图/道具补卡两道闸门合并的既有先例一致。

判据：扫描目标段（``shot_ids`` 为空时整集）``resources.props[]`` 实际用到
的全部 label，按 name/alias 逐字比对解析到世界书里的道具卡（同一 label 可
能解析不到任何卡——那是 ``card_pending_ensure`` 的"缺卡"判据管的，这里只管
"卡存在但该复核"）。解析到的每张卡若 ``prop_card_rule_audits`` 没有记录、
或记录的 ``rules_version`` 落后于当前版本、或仍在 ``running``（未超时）、
或 ``failed`` 且未用尽重试额度，都算"还要拦"——与 ``card_pending_ensure.
_is_blocking`` 同一口径，只是判据对象换成复核记录而不是建卡记录。
"""
from __future__ import annotations

from typing import Any

from app.db import now

from . import card_audit, card_audit_store, judge
from .card_pending_scan import label_shot_occurrences


def _props_referenced_by_labels(bible: Any, labels: set[str]) -> set[str]:
    referenced: set[str] = set()
    for prop in bible.props:
        identifiers = {prop.name, *prop.aliases}
        if identifiers & labels:
            referenced.add(prop.name)
    return referenced


def _is_audit_stale_or_blocking(row: dict[str, Any] | None, rules_version: str) -> bool:
    if row is None or row["rules_version"] != rules_version:
        return True
    status = row["status"]
    if status == "ready":
        return False
    if status == "running":
        return True
    if status == "failed":
        return int(row["attempts"] or 0) < card_audit.MAX_AUDIT_ATTEMPTS
    return True


def _audit_needs_new_launch(row: dict[str, Any] | None, rules_version: str) -> bool:
    if row is None or row["rules_version"] != rules_version:
        return True
    if row["status"] != "running":
        return True
    return (now() - float(row["updated_at"] or 0)) >= card_audit._AUDIT_RUNNING_STALE_S


def stale_audit_props_for_shots(
    conn: Any, project_id: str, bible: Any, shot_rows: list[Any], target_shot_nos: set[int] | None,
) -> list[str]:
    """返回目标段用到、且复核版本落后/仍在跑/重试未用尽的道具卡名（排序去重）。"""
    occurrences = label_shot_occurrences(shot_rows)
    if target_shot_nos is not None:
        occurrences = {label: info for label, info in occurrences.items() if target_shot_nos & info["shot_nos"]}
    referenced = _props_referenced_by_labels(bible, set(occurrences))
    rules_version = judge.PROP_CARD_RULES_VERSION
    stale = [
        name for name in sorted(referenced)
        if _is_audit_stale_or_blocking(card_audit_store.get_audit(conn, project_id=project_id, prop_name=name), rules_version)
    ]
    return stale


def launch_audit_for_stale_if_needed(conn: Any, project_id: str, stale_names: list[str]) -> None:
    """只给"确实需要新起一轮"的名字发起后台复核（已有新鲜 running 的不重复
    起任务），与 ``card_pending_ensure._needs_new_launch`` 同一取舍。供生成
    命令闸门（``card_pending_ensure.pending_prop_card_gate``）使用——闸门要
    快速返回 409，不能等复核跑完。"""
    rules_version = judge.PROP_CARD_RULES_VERSION
    to_launch = [
        name for name in stale_names
        if _audit_needs_new_launch(
            card_audit_store.get_audit(conn, project_id=project_id, prop_name=name), rules_version,
        )
    ]
    if to_launch:
        card_audit.launch_background_audit(project_id=project_id, prop_names=to_launch)


async def ensure_fresh_audits_for_episode(conn: Any, project_id: str, episode_id: str) -> None:
    """连播台钩子（``app.domain.series_ops.stages``，视频阶段派发前）用：
    整集范围判定需要复核的道具卡并**等待**复核跑完才返回——与
    ``card_pending_ensure.ensure_storyboard_prop_cards`` 同一"调用方等它跑完
    才继续派发视频"的取舍，不是闸门那种 fire-and-forget（连播台没有 HTTP
    409 重试循环可用）。人物谱未初始化/分集不存在时静默跳过。"""
    # 函数内导入：避免 card_audit_ensure 模块加载期就卷入 card_pending_ensure
    # 更长的导入链（场景状态图/claim_or_get 等），只有真正调用这个连播专用
    # 入口时才需要。
    from .card_pending_ensure import _load_bible_for_ensure, load_episode_shot_rows
    bible = _load_bible_for_ensure(conn, project_id)
    if bible is None:
        return
    shot_rows = load_episode_shot_rows(conn, episode_id)
    stale = stale_audit_props_for_shots(conn, project_id, bible, shot_rows, None)
    if stale:
        await card_audit.audit_specific_prop_cards(project_id, stale)
