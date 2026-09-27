"""定妆照分段收窄/删除的可见性守护——独立小模块，供 portrait_io.py 与
portrait_drift.py 共用。

背景（《顾念长安》proj_c89e1d2fa4be 第 1 集温念定妆照并发丢失事故）：
``portrait_io._complete_candidate`` 曾经在候选行与既有当前行冲突时直接
``DELETE FROM character_portraits``，无任何日志——一条可能刚完整生成三视角
包的定妆照就此消失，且没有任何痕迹能在事后定位。本模块是全包里所有"收窄/
删除一条定妆照分段"操作的唯一出口：不是多写一次判断，而是把"这次操作是否
让一条本来有效的分段不再覆盖任何集"这件事本身变成可见信号（结构化
WARNING），不猜测原因、不拦截调用方、不改变原有的行为决策——只记录。

拆成独立文件是 app/FILE_CONVENTIONS.toml 的行数/单函数棘轮逼的：直接加进
portrait_io.py 会把它顶穿既有基线（该文件已经是存量欠账，棘轮只降不升）。
"""
from __future__ import annotations

import logging

from ._db_probe import _has_table
from .current_ref import _current_portrait_row

log = logging.getLogger(__name__)


def _portrait_segment_covers_any_episode(ep_start, ep_end) -> bool:
    """``(ep_start, ep_end)`` 是否至少覆盖一集：``ep_end`` 为空即开区间覆盖到
    无穷；否则要求 ``ep_end>=ep_start``——倒置/退化区间视为不覆盖任何集。"""
    if ep_start is None:
        return False
    return ep_end is None or int(ep_end) >= int(ep_start)


def _warn_if_portrait_coverage_lost(
    *,
    character_name: str,
    portrait_id: str,
    before_ep_start,
    before_ep_end,
    after_ep_start,
    after_ep_end,
    caller_episode_no: int,
    reason: str,
    deleted: bool = False,
) -> None:
    """本次收窄/删除操作前该行是否覆盖某集、操作后是否仍覆盖——由有变成无才
    记 WARNING；操作前就已经不覆盖任何集（例如清理一条已作废的占位行）不产生
    日志噪音。"""
    covered_before = _portrait_segment_covers_any_episode(before_ep_start, before_ep_end)
    covered_after = (not deleted) and _portrait_segment_covers_any_episode(after_ep_start, after_ep_end)
    if covered_before and not covered_after:
        log.warning(
            "定妆照分段操作后不再覆盖任何集：character=%s portrait_id=%s "
            "before_ep_start=%s before_ep_end=%s after_ep_start=%s after_ep_end=%s "
            "deleted=%s caller_episode_no=%s reason=%s",
            character_name, portrait_id, before_ep_start, before_ep_end,
            after_ep_start, after_ep_end, deleted, caller_episode_no, reason,
        )


def _close_portrait_segment(
    conn,
    *,
    character_name: str,
    portrait_id: str,
    before_ep_start,
    before_ep_end,
    new_ep_end: int,
    caller_episode_no: int,
    reason: str,
    pack_status: str | None = None,
) -> None:
    """把 ``portrait_id`` 行的 ``ep_end`` 收窄到 ``new_ep_end``（``ep_start``
    不变），可选一并写 ``pack_status``；统一走这里而不是各自裸写 UPDATE，保证
    "是否让该行不再覆盖任何集"这件事永远被检查一次，不会随手改代码就漏记。"""
    if pack_status is not None:
        conn.execute(
            "UPDATE character_portraits SET ep_end=?,pack_status=? WHERE id=?",
            (new_ep_end, pack_status, portrait_id),
        )
    else:
        conn.execute(
            "UPDATE character_portraits SET ep_end=? WHERE id=?",
            (new_ep_end, portrait_id),
        )
    _warn_if_portrait_coverage_lost(
        character_name=character_name, portrait_id=portrait_id,
        before_ep_start=before_ep_start, before_ep_end=before_ep_end,
        after_ep_start=before_ep_start, after_ep_end=new_ep_end,
        caller_episode_no=caller_episode_no, reason=reason,
    )


def _delete_portrait_segment(
    conn,
    *,
    character_name: str,
    portrait_id: str,
    before_ep_start,
    before_ep_end,
    caller_episode_no: int,
    reason: str,
) -> None:
    """物理删除 ``portrait_id`` 行——仅供"删除前就已经不覆盖任何集"的清理路径
    使用（例如恢复重试时清掉一条从未完整过的占位行）。温念事故之后，任何
    仍然覆盖某一集的有效定妆照分段都不应该再经这里物理删除；真正需要
    "退场"的分段一律改用 ``_close_portrait_segment`` 收窄区间。"""
    conn.execute("DELETE FROM character_portraits WHERE id=?", (portrait_id,))
    _warn_if_portrait_coverage_lost(
        character_name=character_name, portrait_id=portrait_id,
        before_ep_start=before_ep_start, before_ep_end=before_ep_end,
        after_ep_start=None, after_ep_end=None, deleted=True,
        caller_episode_no=caller_episode_no, reason=reason,
    )


def _project_episode_count(conn, project_id: str) -> int:
    """项目已有的最大集号；缺 ``episodes`` 表（部分定向测试用最小 schema）
    或缺数据时返回 0，调用方据此判定"没有可核对的集号范围，跳过守护"。"""
    if not _has_table(conn, "episodes"):
        return 0
    row = conn.execute(
        "SELECT MAX(episode_no) AS value FROM episodes WHERE project_id=?", (project_id,),
    ).fetchone()
    return int(row["value"]) if row and row["value"] is not None else 0


def _uncovered_episode_snapshot(conn, project_id: str, character_name: str) -> set[int]:
    """[1, 项目已有集数] 里该角色当前没有任何 ``ep_start>=0`` 有效行覆盖的集号
    快照。直接复用 ``current_ref._current_portrait_row``——本包判定"某集是否
    有当前定妆照"的唯一权威查询，其模块 docstring 明确禁止另写一份相似判据，
    这里不新起一套区间归并逻辑。"""
    max_episode_no = _project_episode_count(conn, project_id)
    if max_episode_no < 1:
        return set()
    return {
        episode_no
        for episode_no in range(1, max_episode_no + 1)
        if _current_portrait_row(project_id, character_name, episode_no, conn=conn) is None
    }


def _warn_if_new_coverage_gap(
    conn,
    *,
    project_id: str,
    character_name: str,
    caller_episode_no: int,
    before_uncovered: set[int],
) -> None:
    """写入完成后，若"写之前有覆盖、写之后没有覆盖"的集号集合非空，只记
    WARNING、不改变任何行为——判据是同一个权威查询在写之前/写之后各跑一次，
    不猜测原因，也不拦截调用方（CLAUDE.md「空集合不等于无需检查」的另一面：
    这里反过来是"写之前已经没有覆盖"不算回归，不产生噪音）。"""
    after_uncovered = _uncovered_episode_snapshot(conn, project_id, character_name)
    regressed = sorted(after_uncovered - before_uncovered)
    if regressed:
        log.warning(
            "定妆照覆盖出现回退：character=%s project=%s caller_episode_no=%s "
            "曾有覆盖但写入后变为无有效行覆盖的集号=%s",
            character_name, project_id, caller_episode_no, regressed,
        )
