"""Capability Registry：连播任务台（series task console）命令声明。

无金额/预算字段（本产品视频生成不计费），confirmation=NEVER（跟 video/
screenplay 系列一致，用户点击按钮本身就是确认）。风险等级：会烧生成的
（enqueue/queue_resume）R2_MATERIAL，其余 R1_REVERSIBLE。

``series.task_delete`` 的 ``side_effect`` 刻意不用 ``deletes_``/``purges_``
前缀——``app/capabilities/coverage.py::_is_resource_deletion`` 用这个前缀判定
「删除资源」进而要求 ``confirmation=ALWAYS``；这条命令只删 ``series_tasks``
的一条记录，磁盘上真正的资源（``film.mp4``）原样保留，语义上不是资源删除。

七条命令拆两个小函数拼起来（而不是一个 ``commands()`` 塞满）：单函数 ≤50
代码行是新增代码的硬指标，不是存量债务，这里没有理由不遵守。
"""
from __future__ import annotations

from app.capabilities import inputs as I
from app.capabilities.commands import build_command as _cmd
from app.capabilities.handlers import series as h_series
from app.capabilities.registry import CommandSpec
from app.capabilities.schemas import ConfirmationPolicy, IdempotencyPolicy, RiskLevel


def _task_lifecycle_commands() -> list[CommandSpec]:
    return [
        _cmd(
            "series.tasks_generate",
            title="生成连播任务清单",
            description="按 group_size 切分整个项目的集号，补齐式生成连播任务；同区间已存在则跳过",
            input_model=I.SeriesTasksGenerateInput,
            risk=RiskLevel.R1_REVERSIBLE,
            confirmation=ConfirmationPolicy.NEVER,
            idempotency=IdempotencyPolicy.RECOMMENDED,
            scopes={"manju:generation-media"},
            side_effect="creates_series_task_rows",
            handler=h_series.tasks_generate,
            rest_routes=("POST /api/projects/{project_id}/series-tasks",),
            tags=("series", "video"),
        ),
        _cmd(
            "series.task_delete",
            title="删除连播任务",
            description="只删任务记录，磁盘上已生成的成片保留；排队中/运行中的任务不能删",
            input_model=I.SeriesTaskDeleteInput,
            risk=RiskLevel.R1_REVERSIBLE,
            confirmation=ConfirmationPolicy.NEVER,
            idempotency=IdempotencyPolicy.RECOMMENDED,
            scopes={"manju:generation-media"},
            side_effect="retires_series_task_row",
            handler=h_series.task_delete,
            rest_routes=("DELETE /api/projects/{project_id}/series-tasks/{task_id}",),
            tags=("series", "video"),
        ),
        _cmd(
            "series.export_create",
            title="打包导出连播任务",
            description="勾选已完成任务生成导出目录：硬链接 + manifest + 下载清单，不打 zip",
            input_model=I.SeriesExportCreateInput,
            risk=RiskLevel.R1_REVERSIBLE,
            confirmation=ConfirmationPolicy.NEVER,
            idempotency=IdempotencyPolicy.RECOMMENDED,
            scopes={"manju:generation-media"},
            side_effect="creates_series_export_bundle",
            handler=h_series.export_create,
            rest_routes=("POST /api/projects/{project_id}/series-exports",),
            tags=("series", "video"),
        ),
    ]


def _queue_enqueue_cancel_commands() -> list[CommandSpec]:
    return [
        _cmd(
            "series.tasks_enqueue",
            title="连播任务入队",
            description="按勾选顺序把任务加入项目队列，串行执行；已完成且未过期的任务默认跳过",
            input_model=I.SeriesTasksEnqueueInput,
            risk=RiskLevel.R2_MATERIAL,
            confirmation=ConfirmationPolicy.NEVER,
            idempotency=IdempotencyPolicy.RECOMMENDED,
            scopes={"manju:generation-media"},
            side_effect="enqueues_series_tasks",
            handler=h_series.tasks_enqueue,
            rest_routes=("POST /api/projects/{project_id}/series-tasks/enqueue",),
            tags=("series", "video"),
        ),
        _cmd(
            "series.tasks_cancel",
            title="取消连播任务",
            description="排队中的任务退回 idle；命中正在跑的任务会打断它，队列继续跑后面的任务",
            input_model=I.SeriesTasksCancelInput,
            risk=RiskLevel.R1_REVERSIBLE,
            confirmation=ConfirmationPolicy.NEVER,
            idempotency=IdempotencyPolicy.RECOMMENDED,
            scopes={"manju:generation-media"},
            side_effect="cancels_series_tasks",
            handler=h_series.tasks_cancel,
            rest_routes=("POST /api/projects/{project_id}/series-tasks/cancel",),
            tags=("series", "video"),
        ),
    ]


def _queue_pause_resume_commands() -> list[CommandSpec]:
    return [
        _cmd(
            "series.queue_pause",
            title="暂停连播队列",
            description="当前正在跑的任务退回排队中（进度保留），队列停止自动取下一个任务",
            input_model=I.SeriesQueueControlInput,
            risk=RiskLevel.R1_REVERSIBLE,
            confirmation=ConfirmationPolicy.NEVER,
            idempotency=IdempotencyPolicy.RECOMMENDED,
            scopes={"manju:generation-media"},
            side_effect="pauses_series_queue",
            handler=h_series.queue_pause,
            rest_routes=("POST /api/projects/{project_id}/series-tasks/queue/pause",),
            tags=("series", "video"),
        ),
        _cmd(
            "series.queue_resume",
            title="继续连播队列",
            description="解除暂停并重新启动 runner，从队首任务继续跑",
            input_model=I.SeriesQueueControlInput,
            risk=RiskLevel.R2_MATERIAL,
            confirmation=ConfirmationPolicy.NEVER,
            idempotency=IdempotencyPolicy.RECOMMENDED,
            scopes={"manju:generation-media"},
            side_effect="resumes_series_queue",
            handler=h_series.queue_resume,
            rest_routes=("POST /api/projects/{project_id}/series-tasks/queue/resume",),
            tags=("series", "video"),
        ),
    ]


def commands() -> list[CommandSpec]:
    return (
        _task_lifecycle_commands()
        + _queue_enqueue_cancel_commands()
        + _queue_pause_resume_commands()
    )
