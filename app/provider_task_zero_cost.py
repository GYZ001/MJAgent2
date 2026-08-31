"""供应商终态拒绝、且确凿零产出的清空放行 + 用户可见释放入口。

补 ``app/provider_task_clearance.py`` 里未覆盖的一类：``provably_unsubmitted_
cancelled`` 只覆盖「从未提交给供应商」；``failure_disposition=='external_
terminal'`` 覆盖「模型明确拒绝、按业务口径整笔预留金额写回」。真实事故
（proj_1fce17f77010 第 1 集镜 5/6）落在两者之外——供应商已接单、单次轮询即
返回终态失败（版权拦截），系统正确转人工（``manual_review``）。

⚠️ 2026-08-30 两次订正（都已核实，按最终结论实现，过程记在这里避免下次
重犯）：
  1. 第一版以为 ``VIDEO_PRICE_PER_SECOND``（app/config.py）是"内部记账名义
     值、不是真实账单"，据此按供应商/模型免账单与否放行——**这个前提是错的**，
     用户明确核实过"一秒 0.8 元是真实价格，后面公司就会收费"。已整段移除，
     不要再按供应商/模型名字加免账单例外。
  2. 真正的根因是 ``app/completion_grant.py::reconcile_provider_tasks_for_
     clear``（``project.delete`` 触发的软删除会调用它核对供应商任务真实终态）
     曾经无条件按预留估算（``amount_cny``）结算——包括供应商已确认 **failed**
     （零产出）的情况——把没有任何产出的失败版本按全价结算，这是 2026-08-30
     ``project_soft_delete_terminal_reconcile`` 事件里 proj_1fce17f77010 两笔
     ``cost_cny``/``actual_cost_cny`` 变成 12.0 的真实来源（``video_path`` 全程
     为空）。该函数已同步修复（failed 分支改记 0，succeeded/隔离分支不变，
     见其函数体注释与 ``tests/test_generation_station_asset_clear.py`` 的
     ``test_reconcile_failed_terminal_settles_zero_cost_not_reserved_estimate``/
     ``test_reconcile_succeeded_terminal_still_charges_reserved_estimate`` 两条
     红绿用例）——但历史上已经被这条 bug 污染、还没人工纠正的记录依旧存在
     （即本文件要处理的这两笔），判据因此不能直接相信 ``cost_cny``/
     ``actual_cost_cny`` 的数值本身。

判据（全部必须同时成立，任一缺失就 fail closed，继续阻塞）：
  1. 供应商侧确已终态——``provider_calls`` 里存在一条真实发生的
     ``kind='video_poll' AND status='TASK_FAILED'`` 记录，即供应商自己的任务
     状态查询接口明确返回过 "failed"。局部超时放弃（最后一次已知状态仍是
     "running"/"queued"）不会产生这条记录，因此不会被这里放行——那类失败可能
     是烧到一半才放弃，必须继续走人工核实。
  2. 从未产生可采用产出——``shot_versions.video_path`` 为空且
     ``status != 'succeeded'``。这是本判据里唯一的"零产出"证据来源：QA 判定
     不合格但确已生成视频的场景，``video_path`` 仍会留痕，这里不会误判为零
     产出；而"供应商确认 failed 且从未下载"在本系统的计价模型（按产出时长计
     费，不按尝试次数计费）下**不可能**有合法的非零成本——``cost_cny``/
     ``actual_cost_cny`` 记了多少都不再作为放行或阻塞的依据，只在 reason 里
     如实带出，供人工核对是否命中同一类污染。

放行/释放两个动作分离：``apply_zero_cost_terminal_release`` 只读，把已证明
零产出的 blocker 从清空判据里摘掉（供 ``provider_task_clearance_snapshot``/
``assert_provider_tasks_clearable`` 复用，静默解开非破坏性清空操作，不需要用户
逐一确认——这类结构化证明与既有的 ``provably_unsubmitted_cancelled``/
``external_terminal`` 分支同等地位）；``release_zero_cost_terminal_jobs`` 才是
真正落库结算的动作，只服务用户主动点击的"释放"入口（本地二段式确认，见
``app/provider_task_zero_cost_api.py``），每次都对每个 job_id 独立重新核验，
不信任调用方传入的任何判断结果，并且会把 ``shot_versions.cost_cny``/
``budget_reservations.actual_cost_cny`` 一并纠正为 0（不止是解除阻塞，也是在
清掉这条历史 bug 留下的污染记录）。
"""
from __future__ import annotations

from typing import Any

from app.db import now


def _claims_table_available(db: Any) -> bool:
    return bool(db.execute(
        """SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='provider_video_budget_claims'"""
    ).fetchone())


def _terminal_poll_failure_confirmed(db: Any, provider_task_id: str | None) -> bool:
    """供应商自己的任务状态查询是否曾经明确返回过终态失败。"""
    task_id = str(provider_task_id or "").strip()
    if not task_id:
        return False
    return bool(db.execute(
        """SELECT 1 FROM provider_calls
            WHERE kind='video_poll' AND status='TASK_FAILED' AND meta LIKE ?
            LIMIT 1""",
        (f"%{task_id}%",),
    ).fetchone())


def load_zero_cost_evidence(db: Any, job_id: str) -> dict[str, Any] | None:
    """加载判定零产出终态拒绝所需的全部字段；job 不存在时返回 None。"""
    row = db.execute(
        """SELECT j.id AS job_id, j.status AS job_status,
                  j.project_id, j.episode_id, j.shot_id,
                  j.provider_failure_disposition, j.provider_failure_retryable,
                  j.provider_operation_id, j.updated_at,
                  v.id AS version_id, v.provider_task_id,
                  v.status AS version_status, v.video_path, v.cost_cny,
                  br.status AS reservation_status, br.actual_cost_cny,
                  br.amount_cny AS reserved_amount_cny
             FROM jobs j
             LEFT JOIN shot_versions v ON v.id=j.version_id
             LEFT JOIN budget_reservations br ON br.job_id=j.id
            WHERE j.id=?""",
        (job_id,),
    ).fetchone()
    return dict(row) if row else None


def zero_cost_terminal_release_eligible(
    db: Any, evidence: dict[str, Any],
) -> tuple[bool, str]:
    """结构化判定，永远返回判定依据（不是猜测），可直接展示给用户。

    不排除 ``external_terminal``（模型明确拒绝）——那条分类在
    ``provider_task_clearance.py`` 里已经有自己的清空放行通道，但它结算金额的
    落库函数（``checkpoints.py::_commit_provider_terminal_failure_in_transaction``）
    此前同样存在"零产出仍按预留全价结算"的 bug（生产库 30 条同类污染，已随
    该函数一并修复），这里必须能覆盖、纠正这些历史存量，否则用户只是不再被
    卡住，账上还是继续挂着假钱。
    """
    disposition = str(evidence.get("provider_failure_disposition") or "")
    if not disposition:
        return False, "无匹配的技术失败终态分类"
    if evidence.get("provider_failure_retryable"):
        return False, "该失败被标记为可自动重试，不属于终态拒绝"
    if str(evidence.get("job_status") or "") not in {"failed", "waiting_human"}:
        return False, "任务未处于终态"
    if str(evidence.get("version_status") or "") == "succeeded":
        return False, "该版本已成功，不属于零产出场景"
    if str(evidence.get("video_path") or "").strip():
        return False, "已存在产出文件，无法证明零产出"
    if not _terminal_poll_failure_confirmed(db, evidence.get("provider_task_id")):
        return False, "未查到供应商轮询终态失败的调用记录，无法证明供应商已终态"
    recorded_cost = max(
        float(evidence.get("cost_cny") or 0),
        float(evidence.get("actual_cost_cny") or 0),
    )
    if recorded_cost:
        return True, (
            f"供应商已确认终态失败（轮询接口返回 failed）且从未产生任何产出"
            f"文件；本地记录的成本 ¥{recorded_cost:.2f} 与零产出矛盾，不构成"
            "已花钱的证据，释放时会一并纠正为 0"
        )
    return True, "供应商已确认终态失败（轮询接口返回 failed），且未记录任何已产生费用"


def apply_zero_cost_terminal_release(
    db: Any,
    clearance: dict[str, Any],
    terminal_actions: dict[str, list[str]],
) -> None:
    """只读重新分类：把已证明零产出的 blocker 从清空判据里摘掉。

    不写库——调用方（``provider_task_clearance_snapshot``）本身是只读判据，
    这里只允许改内存里的 dict，绝不能因为一次预检查询就顺带结算预算。
    """
    blockers = clearance.get("blockers") or []
    kept: list[dict[str, Any]] = []
    released_job_ids: list[str] = []
    for blocker in blockers:
        job_id = str(blocker.get("job_id") or "")
        evidence = load_zero_cost_evidence(db, job_id) if job_id else None
        eligible = bool(evidence) and zero_cost_terminal_release_eligible(db, evidence)[0]
        if eligible:
            released_job_ids.append(job_id)
        else:
            kept.append(blocker)
    if released_job_ids:
        clearance["blockers"] = kept
        clearance["safe_to_clear"] = not kept
        clearance["resume_supported"] = bool(kept)
        terminal_actions["zero_cost_release"] = released_job_ids


def list_zero_cost_terminal_candidates(
    db: Any,
    *,
    project_id: str | None = None,
    episode_id: str | None = None,
) -> list[dict[str, Any]]:
    """可见性列表：给用户看"哪些镜头卡住、多少钱、供应商怎么说、能不能释放"。

    范围内只要预留还没被真正「释放」（``status IN ('reserved','settled')``）
    且命中终态失败分类的任务都会出现，不论最终是否满足释放条件——不满足的也
    带着结构化理由，不把用户晾在原地。包含 ``settled`` 是因为历史上曾经有
    结算路径的 bug（见模块 docstring）把零产出的失败按全价结算，只看
    ``reserved`` 会让这些已经挂着虚构费用的记录从列表里消失，用户反而找不到
    入口去纠正。
    """
    clauses = ["br.status IN ('reserved','settled')",
               "j.status IN ('failed','waiting_human')",
               "j.provider_failure_disposition IS NOT NULL"]
    params: list[str] = []
    if project_id:
        clauses.append("j.project_id=?")
        params.append(project_id)
    if episode_id:
        clauses.append("j.episode_id=?")
        params.append(episode_id)
    rows = db.execute(
        f"""SELECT j.id AS job_id, j.status AS job_status, j.project_id,
                   j.episode_id, j.shot_id, j.reason_text, j.updated_at,
                   s.shot_no, e.episode_no, p.name AS project_name,
                   br.amount_cny AS reserved_amount_cny
              FROM jobs j
              JOIN budget_reservations br ON br.job_id=j.id
              LEFT JOIN shots s ON s.id=j.shot_id
              LEFT JOIN episodes e ON e.id=j.episode_id
              LEFT JOIN projects p ON p.id=j.project_id
             WHERE {" AND ".join(clauses)}
             ORDER BY j.updated_at DESC""",
        params,
    ).fetchall()
    out = []
    for row in rows:
        evidence = load_zero_cost_evidence(db, row["job_id"])
        eligible, reason = (
            zero_cost_terminal_release_eligible(db, evidence)
            if evidence else (False, "任务记录已不存在")
        )
        out.append({**dict(row), "eligible": eligible, "reason": reason})
    return out


def _release_zero_cost_terminal_jobs_in_transaction(
    db: Any, job_ids: list[str],
) -> list[dict[str, Any]]:
    """在调用方事务里逐个重新核验并结算；不 BEGIN/COMMIT，由调用方决定何时提交。"""
    claims_available = _claims_table_available(db)
    receipts: list[dict[str, Any]] = []
    for job_id in dict.fromkeys(str(j) for j in job_ids if j):
        evidence = load_zero_cost_evidence(db, job_id)
        if evidence is None:
            raise ValueError(f"任务不存在：{job_id}")
        eligible, reason = zero_cost_terminal_release_eligible(db, evidence)
        if not eligible:
            raise ValueError(f"任务 {job_id} 不满足零产出终态释放条件：{reason}")
        stamp = now()
        # 不按 status 限制：历史结算 bug（见模块 docstring）可能已经把预留按
        # 名义单价记成非零、状态记成 settled；此处已经独立重新核验过
        # eligible，即便之前被结算成非零，这里也要能把那个虚构数字纠正为
        # 0——否则用户点了"释放"却因为状态已是 settled 而静默不生效。
        db.execute(
            """UPDATE budget_reservations
                  SET status='released',settled_at=?,actual_cost_cny=0
                WHERE job_id=?""",
            (stamp, job_id),
        )
        db.execute(
            """UPDATE jobs SET provider_poll_required=0,reserved_cost_cny=0,
                   updated_at=? WHERE id=?""",
            (stamp, job_id),
        )
        version_id = str(evidence.get("version_id") or "")
        if version_id:
            db.execute(
                "UPDATE shot_versions SET cost_cny=0 WHERE id=?",
                (version_id,),
            )
        if claims_available:
            db.execute(
                """UPDATE provider_video_budget_claims
                      SET status='released',updated_at=?,released_at=?
                    WHERE job_id=? AND status!='released'""",
                (stamp, stamp, job_id),
            )
        receipts.append({
            "job_id": job_id, "amount_cny": 0.0, "reason": reason,
            "reserved_amount_cny": float(evidence.get("reserved_amount_cny") or 0),
        })
    return receipts


def release_zero_cost_terminal_jobs(
    db: Any, job_ids: list[str],
) -> list[dict[str, Any]]:
    """独立事务的落库结算：供用户主动点击的释放入口调用（``db`` 必须显式传入，
    不给默认值——这是状态转移写操作，调用方必须清楚用的是谁的连接）。

    任何一个 job_id 未通过核验都整体回滚——不做"能放几个放几个"的部分放行，
    避免用户以为全部释放成功、实际只放了一部分。
    """
    try:
        db.execute("BEGIN IMMEDIATE")
        receipts = _release_zero_cost_terminal_jobs_in_transaction(db, job_ids)
        db.commit()
    except BaseException:
        if db.in_transaction:
            db.rollback()
        raise
    return receipts
