"""run.* / job.cancel Command Handlers（Workflow Run 与媒体 Job 控制）。"""
from __future__ import annotations

from app.capabilities import inputs as I
from app.capabilities.handlers.common import call_guarded, failed, succeeded
from app.capabilities.schemas import CommandResult

_ACTION_FUNCS = {
    "cancel": "cancel_run",
    "resume": "resume_run",
    "retry": "retry_run",
}
_ACTION_LABEL = {"cancel": "已取消", "resume": "已恢复", "retry": "已重试"}


async def control(args: I.RunControlInput) -> CommandResult:
    from app.orchestration import api as orch_api

    func_name = _ACTION_FUNCS.get(args.action)
    if func_name is None:
        return failed(f"未知 action：{args.action}", error_code="invalid_input")
    func = getattr(orch_api, func_name)
    outcome = await call_guarded(func, args.run_id)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(
        f"Run {_ACTION_LABEL[args.action]}",
        data=outcome,
        run_id=(outcome.get("run") or {}).get("id", args.run_id),
        resource_uris=[f"manju://runs/{args.run_id}"],
    )


async def create(args: I.BenchmarkRunInput) -> CommandResult:
    from app.orchestration import api as orch_api

    outcome = await call_guarded(orch_api.create_run, body=args.payload)
    if isinstance(outcome, CommandResult):
        return outcome
    run_id = outcome.get("id")
    return succeeded(
        "Workflow Run 已创建",
        data=outcome,
        run_id=run_id,
        resource_uris=[f"manju://runs/{run_id}"] if run_id else [],
    )


async def job_cancel(args: I.JobCancelInput) -> CommandResult:
    from app.orchestration import api as orch_api

    outcome = await call_guarded(orch_api.cancel_media_job, args.job_id)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(f"媒体任务 {args.job_id} 已请求取消", data=outcome)
