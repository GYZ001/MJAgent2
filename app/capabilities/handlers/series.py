"""series.* Command Handlers（连播台）。

跟 screenplay/storyboard 的 handler 同一个写法：直接调用同名 REST 路由函数
（``app.domain.series_ops.routes`` 里的 4 个路由），路由自己顶部的
``ui_route(...)`` 在 Handler 执行期（``in_handler()`` 为真）会短路返回
``None``，从而直接跑路由函数本体的领域逻辑，不会二次进入 Command Bus。
"""
from __future__ import annotations

from app.capabilities import inputs as I
from app.capabilities.handlers.common import call_guarded, succeeded
from app.capabilities.schemas import CommandResult


async def film_start(args: I.SeriesFilmStartInput) -> CommandResult:
    from app.domain.series_ops.routes import start_series_film

    outcome = await call_guarded(
        start_series_film,
        args.project_id,
        body={"episode_from": args.episode_from, "episode_to": args.episode_to},
    )
    if isinstance(outcome, CommandResult):
        return outcome
    run_id = outcome.get("run_id")
    return succeeded(
        f"连播台已启动，第 {args.episode_from}-{args.episode_to} 集将依次跑完",
        data=outcome,
        run_id=run_id,
        resource_uris=[f"manju://runs/{run_id}"] if run_id else [],
    )


async def film_pause(args: I.SeriesFilmControlInput) -> CommandResult:
    from app.domain.series_ops.routes import pause_series_film

    outcome = await call_guarded(pause_series_film, args.project_id)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded("连播台已暂停", data=outcome)


async def film_resume(args: I.SeriesFilmControlInput) -> CommandResult:
    from app.domain.series_ops.routes import resume_series_film

    outcome = await call_guarded(resume_series_film, args.project_id)
    if isinstance(outcome, CommandResult):
        return outcome
    run_id = outcome.get("run_id")
    return succeeded(
        "连播台已继续",
        data=outcome,
        run_id=run_id,
        resource_uris=[f"manju://runs/{run_id}"] if run_id else [],
    )
