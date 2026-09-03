"""服务重启后残留 RUNNING/PAUSED_EXTERNAL 运行的收尾（WS8-B）。

背景：``app.recovery.recover_all()`` 里逐工作流的恢复步骤（``recover_
screenplay_tasks`` 等）只负责把「这一类工作流自己认识的、还欠着工作的实体」
重新接管——一旦某个具体运行被判定为「不可续跑」或「恢复重跑本身失败」，那些
步骤只回滚所属实体（episode/project）的状态指针，从不回头收尾这条
``workflow_runs`` 记录本身：它会永远停在 RUNNING/PAUSED_EXTERNAL，
``finished_at`` 永远是 NULL。

父/批类型的运行更明显：``screenplay_batch`` 只在它每个子运行的
``_screenplay_guarded`` 协程正常跑完的 ``finally`` 块里调用
``_refresh_screenplay_batch_run`` 判「子运行是否全部终态」（见
``app.domain.screenplay_ops.guarded``）；一旦某个子运行从未真正重新起跑（例如
恢复判定「不可续跑」，直接重置了 episode 指针、没有再走一次
``_screenplay_guarded``），这个判定就再也不会被触发，父运行永久挂在
RUNNING/PAUSED_EXTERNAL。真实复现：proj_ce9fcf749b23「跑不快的孩子」的
run_df4b60ccef89（screenplay_batch）——3 个子运行里 1 SUCCEEDED、1 FAILED、
1 至今仍是 PAUSED_EXTERNAL（它的所属 episode 早已转向另一次与这条子运行毫无
parent_run_id 关系的独立重试，这条子运行本身被彻底遗弃，永远不会自然终态），
父运行因此永远卡在非终态、``finished_at`` 永远是 NULL。严格要求「子运行全部
终态」会让这类批运行永久卡住，所以对非终态子运行再加一层判断——见
``_child_is_resolved_or_orphaned``。

本模块是 ``recover_all()`` 的最后一步，作为兜底：在全部逐类恢复步骤都跑完之后
再扫一遍仍然停在 RUNNING/PAUSED_EXTERNAL、``started_at`` 已经过了安全阈值的
运行，把它们收口成一个诚实的状态。

范围收敛（不是偷懒，是没有安全依据不动，CLAUDE.md「查不清就说查不清」）：

- 批运行（``workflow_type`` 以 ``_batch`` 结尾）：子运行只要是终态，或者是
  非终态但已核实 (task_registry kind, scope_id) 映射关系确认「现在没有活跃
  任务」（见 ``_ACTIVE_TASK_KIND_BY_WORKFLOW_TYPE``），就计入收口判断——后一种
  按"未完成"计入失败统计，不假装它成功过。只要有一个子运行既非终态、也无法
  确认没有活跃任务（未核实映射关系的工作流类型，或 task_registry 显示确实
  还在跑），批运行就不收口，退回"标记为孤儿暂停"（仅当批运行自身仍是
  ``RUNNING`` 时才会真的改动它）。
- 非批叶子运行只处理仍处于 ``RUNNING`` 的行，且只对已核实映射关系的工作流
  类型做判断；已经是 ``PAUSED_EXTERNAL`` 的非批叶子运行本次不处理——从
  PAUSED_EXTERNAL 到终态需要先确认所属实体没有转向别的继任运行，这是逐工作流
  的业务知识，不在本模块能安全推断的范围内，宁可留着已知缺口也不猜测关闭
  （批运行的子运行是唯一例外：上面的"未完成按失败计入"只影响父运行怎么收口，
  不会改写子运行自己这一行的状态）。
"""
from __future__ import annotations

from typing import Any

from app.orchestration.state_machine import StateConflict, transition_run

#: 判定"这个非批运行现在有没有活跃任务"的 (workflow_type -> task_registry
#: kind) 映射；key 一律就是 ``workflow_runs.scope_id``。逐条核实自真实调用点
#: （app.domain.*_ops / app.orchestration.api 里的 task_registry.spawn/active
#: 调用），不是猜的字符串——portrait_view_redo/scene_view_redo 的
#: task_registry key 是 ``f"{portrait_id}:{view_role}"``，不等于 scope_id，
#: delivery_*/video_generation 走的是另一套（kind="run" 键的是继任 run 的
#: id，或干脆不经 task_registry），刻意都不收录（见模块 docstring）。
_ACTIVE_TASK_KIND_BY_WORKFLOW_TYPE: dict[str, str] = {
    "screenplay": "screenplay",
    "storyboard": "storyboard",
    "character_bible": "bible",
    "scene_bible": "scene_bible",
    "character_references": "refs",
    "scene_references": "scene_refs",
    "episode_video_completion": "video_completion",
    "project_video_completion_queue": "video_completion_project",
}

_TERMINAL_STATUSES = {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"}

#: 非批运行走"没有已知无害证据"时的保守失败码；批运行两个分支各自的语义更
#: 具体，单列常量方便测试与调用方按码筛选。
ORPHAN_FAILURE_CODE = "SERVICE_RESTART_ORPHAN"
_BATCH_PARTIAL_FAILURE_CODE = "PARTIAL_RESULT"
_BATCH_FAILED_FAILURE_CODE = "BATCH_CHILDREN_FAILED"


def _has_active_task(workflow_type: str, scope_id: str) -> bool:
    """未核实映射关系的工作流类型保守返回 True（当作"可能还活跃"），交给
    调用方跳过——CLAUDE.md「查不清就说查不清」，宁可漏收尾也不误杀在跑任务。
    """
    from app import task_registry

    kind = _ACTIVE_TASK_KIND_BY_WORKFLOW_TYPE.get(workflow_type)
    if kind is None:
        return True
    return task_registry.active(kind, scope_id)


def _child_is_resolved_or_orphaned(child: Any) -> bool:
    """终态子运行直接算数；非终态子运行只有在已核实映射关系下确认"现在没有
    活跃任务"时，才允许当作"未完成、按失败计入"纳入批运行的收口判断——
    ``_has_active_task`` 对未核实的工作流类型保守返回 True，这里因此自动继承
    同一条"查不清就不动"的红线。
    """
    if child["status"] in _TERMINAL_STATUSES:
        return True
    return not _has_active_task(child["workflow_type"], child["scope_id"])


def _batch_children_reason(children: list[Any]) -> tuple[str, str | None]:
    failed = [c for c in children if c["status"] != "SUCCEEDED"]
    if not failed:
        return f"批运行已收口，共 {len(children)} 个子运行全部成功", None
    detail = "；".join(
        f"{c['id']}:{(c['failure_message'] or c['status'] or '')[:60]}" for c in failed[:5]
    )
    return f"{len(failed)} 个子运行失败：{detail}", (
        _BATCH_FAILED_FAILURE_CODE if len(failed) == len(children) else _BATCH_PARTIAL_FAILURE_CODE
    )


def _finalize_batch_parent(conn: Any, row: Any) -> str | None:
    children = conn.execute(
        "SELECT id, status, failure_message, workflow_type, scope_id "
        "FROM workflow_runs WHERE parent_run_id=?",
        (row["id"],),
    ).fetchall()
    if not children or not all(_child_is_resolved_or_orphaned(c) for c in children):
        return _mark_orphan_paused(conn, row)
    reason, failure_code = _batch_children_reason(children)
    target = (
        "SUCCEEDED" if failure_code is None
        else "FAILED" if failure_code == _BATCH_FAILED_FAILURE_CODE
        else "PARTIAL"
    )
    try:
        if row["status"] == "PAUSED_EXTERNAL":
            transition_run(
                row["id"], "PAUSED_EXTERNAL", "RUNNING",
                "服务重启恢复：批运行收尾前重新激活以落终态", conn=conn,
            )
        transition_run(row["id"], "RUNNING", target, reason, failure_code=failure_code, conn=conn)
    except StateConflict:
        return None
    return target


def _mark_orphan_paused(conn: Any, row: Any) -> str | None:
    if row["status"] != "RUNNING":
        return None
    message = "服务重启后无人接管此运行，系统已自动收尾为暂停；可在对应工作台重新发起"
    try:
        transition_run(
            row["id"], "RUNNING", "PAUSED_EXTERNAL", message,
            failure_code=ORPHAN_FAILURE_CODE, conn=conn,
        )
    except StateConflict:
        return None
    return "PAUSED_EXTERNAL"


def finalize_stale_workflow_runs(
    conn: Any, *, now_ts: float, stale_after_s: float,
) -> dict[str, int]:
    """B1 唯一入口：扫描、分类、落终态。返回按落地目标状态计数的字典，供
    ``app.recovery.recover_all()`` 的 run_step 报告使用。显式接收 ``conn``、
    调用方负责提交前的连接所有权（CLAUDE.md「不得在调用方的连接上隐式提
    交」）——本函数内部对每一行的收尾都在同一个 ``conn`` 上做，最后统一
    ``commit()`` 一次。
    """
    threshold = now_ts - stale_after_s
    rows = conn.execute(
        """SELECT * FROM workflow_runs
            WHERE status IN ('RUNNING','PAUSED_EXTERNAL')
              AND started_at < ?
            ORDER BY started_at""",
        (threshold,),
    ).fetchall()
    outcomes: dict[str, int] = {}
    for row in rows:
        workflow_type = row["workflow_type"] or ""
        if workflow_type.endswith("_batch"):
            outcome = _finalize_batch_parent(conn, row)
        elif row["status"] == "RUNNING" and not _has_active_task(workflow_type, row["scope_id"]):
            outcome = _mark_orphan_paused(conn, row)
        else:
            outcome = None
        if outcome:
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
    conn.commit()
    return outcomes


__all__ = [name for name in globals() if not name.startswith("__")]
