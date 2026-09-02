"""series.* Command Handlers（连播任务台）。

跟 screenplay/storyboard 的 handler 同一个写法：直接调用同名 REST 路由函数
（``app.domain.series_ops.routes_tasks``/``routes_exports``），路由自己顶部的
``ui_route(...)`` 在 Handler 执行期（``in_handler()`` 为真）会短路返回
``None``，从而直接跑路由函数本体的领域逻辑，不会二次进入 Command Bus。
"""
from __future__ import annotations

from app.capabilities import inputs as I
from app.capabilities.handlers.common import call_guarded, succeeded
from app.capabilities.schemas import CommandResult


async def tasks_generate(args: I.SeriesTasksGenerateInput) -> CommandResult:
    from app.domain.series_ops.routes_tasks import generate_series_tasks

    outcome = await call_guarded(
        generate_series_tasks, args.project_id,
        body={"group_size": args.group_size, "ranges": args.ranges},
    )
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(f"已生成 {outcome.get('created', 0)} 个连播任务", data=outcome)


async def task_delete(args: I.SeriesTaskDeleteInput) -> CommandResult:
    from app.domain.series_ops.routes_tasks import delete_series_task

    outcome = await call_guarded(delete_series_task, args.project_id, args.task_id)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded("任务已删除（成片文件保留）", data=outcome)


async def tasks_enqueue(args: I.SeriesTasksEnqueueInput) -> CommandResult:
    from app.domain.series_ops.routes_tasks import enqueue_series_tasks

    outcome = await call_guarded(
        enqueue_series_tasks, args.project_id,
        body={"task_ids": args.task_ids, "force": args.force},
    )
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(f"已加入队列 {outcome.get('enqueued', 0)} 个任务", data=outcome)


async def tasks_cancel(args: I.SeriesTasksCancelInput) -> CommandResult:
    from app.domain.series_ops.routes_tasks import cancel_series_tasks

    outcome = await call_guarded(
        cancel_series_tasks, args.project_id, body={"task_ids": args.task_ids},
    )
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded("已取消选中的任务", data=outcome)


async def queue_pause(args: I.SeriesQueueControlInput) -> CommandResult:
    from app.domain.series_ops.routes_tasks import pause_series_queue

    outcome = await call_guarded(pause_series_queue, args.project_id)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded("队列已暂停", data=outcome)


async def queue_resume(args: I.SeriesQueueControlInput) -> CommandResult:
    from app.domain.series_ops.routes_tasks import resume_series_queue

    outcome = await call_guarded(resume_series_queue, args.project_id)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded("队列已继续", data=outcome)


async def export_create(args: I.SeriesExportCreateInput) -> CommandResult:
    from app.domain.series_ops.routes_exports import create_series_export

    outcome = await call_guarded(
        create_series_export, args.project_id, body={"task_ids": args.task_ids},
    )
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(f"导出包已生成，共 {outcome.get('item_count', 0)} 个文件", data=outcome)
