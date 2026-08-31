"""零扣费终态拒绝：可见性列表 + 本地二段式确认的释放动作。

两个端点只服务这一件事——判据与结算逻辑全在
``app/provider_task_zero_cost.py``，本文件只是薄路由层。CLAUDE.md「拦住用户
时必须给出路」：``PROVIDER_TASKS_NOT_TERMINAL`` 的错误原文让用户「核对供应商
任务状态」，但界面此前没有任何入口能做这件事——这两个端点就是那个入口。

都挂 ``require_system_admin``（与 ``app/system_api.py::retry_job`` 同等敏感度：
这是运维介入付费任务状态，不是普通项目内操作）。释放端点走本地
``?confirm=true`` 二段式协议（参照 ``app/auth/api.py::delete_my_account``），
不经 Command Bus——WAITING_APPROVAL 的 token 会被前端自动消费掉、等于没有真正
确认过，见本次任务派单的明确要求。绝不提供「不管三七二十一都放行」的开关：
不带 confirm 只预览且不写库，带 confirm 时仍对每个 job_id 重新核验一遍，任何
一个不满足条件就整体拒绝（409），不做"能放几个放几个"的部分释放。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.auth.deps import require_system_admin
from app.db import get_conn
from app.provider_task_zero_cost import (
    list_zero_cost_terminal_candidates,
    load_zero_cost_evidence,
    release_zero_cost_terminal_jobs,
    zero_cost_terminal_release_eligible,
)

router = APIRouter(prefix="/api")


@router.get("/system/provider-tasks/zero-cost-candidates")
def zero_cost_candidates(
    project_id: str | None = None,
    episode_id: str | None = None,
    _admin=Depends(require_system_admin),
):
    """列出仍卡着预留预算、命中终态失败分类的任务；不论是否已满足释放条件都
    带结构化理由展示，不满足的也让用户看到"为什么"。"""
    conn = get_conn()
    items = list_zero_cost_terminal_candidates(
        conn, project_id=project_id, episode_id=episode_id,
    )
    return {"items": items, "total": len(items)}


@router.get("/system/provider-tasks/zero-cost-candidates/{job_id}")
def zero_cost_candidate_detail(job_id: str, _admin=Depends(require_system_admin)):
    """单任务判定——给 JobDrawer 用：加载详情时顺带查一次，决定要不要展示
    「释放零扣费预留」按钮，以及确认文案里要写的金额与理由。"""
    conn = get_conn()
    evidence = load_zero_cost_evidence(conn, job_id)
    if evidence is None:
        raise HTTPException(404, "任务不存在")
    eligible, reason = zero_cost_terminal_release_eligible(conn, evidence)
    return {
        "job_id": job_id,
        "eligible": eligible,
        "reason": reason,
        "reserved_amount_cny": float(evidence.get("reserved_amount_cny") or 0),
    }


def _verify_release_preview(conn, job_ids: list[str]) -> list[dict]:
    """只读重新核验；任一 job_id 不合格就直接抛 404/409，不返回部分结果。"""
    preview = []
    for job_id in job_ids:
        evidence = load_zero_cost_evidence(conn, job_id)
        if evidence is None:
            raise HTTPException(404, f"任务不存在：{job_id}")
        eligible, reason = zero_cost_terminal_release_eligible(conn, evidence)
        if not eligible:
            raise HTTPException(409, {
                "code": "ZERO_COST_RELEASE_NOT_ELIGIBLE",
                "message": f"任务 {job_id} 不满足零扣费终态释放条件：{reason}",
                "job_id": job_id,
                "reason": reason,
            })
        preview.append({
            "job_id": job_id,
            "shot_id": evidence.get("shot_id"),
            "episode_id": evidence.get("episode_id"),
            "reserved_amount_cny": float(evidence.get("reserved_amount_cny") or 0),
            "reason": reason,
        })
    return preview


@router.post("/system/provider-tasks/zero-cost-release")
def zero_cost_release(
    body: dict,
    confirm: bool = False,
    _admin=Depends(require_system_admin),
):
    """两步确认：不带 ``confirm=true`` 只预览，不写库；带 ``confirm=true``
    才真正把预留预算结算为 0 并解除清空阻塞。"""
    job_ids = list(dict.fromkeys(str(j) for j in (body or {}).get("job_ids") or [] if str(j)))
    if not job_ids:
        raise HTTPException(422, "job_ids 不能为空")
    conn = get_conn()
    preview = _verify_release_preview(conn, job_ids)
    if not confirm:
        raise HTTPException(422, {
            "code": "confirmation_required",
            "message": (
                f"将把 {len(preview)} 个供应商终态拒绝任务的预留预算结算为 0 元，"
                "并解除对清空/重做等操作的阻塞。请带 confirm=true 重试。"
            ),
            "items": preview,
        })
    receipts = release_zero_cost_terminal_jobs(conn, job_ids)
    return {"ok": True, "released": receipts}
