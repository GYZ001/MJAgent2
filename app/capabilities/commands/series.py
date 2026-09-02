"""Capability Registry：连播台（series film）命令声明。

照 ``app/capabilities/commands/video.py`` 里 ``video.complete_project`` 的
注册写法：无金额/预算字段（本产品视频生成不计费），confirmation=NEVER（跟
video/screenplay 系列一致，用户点击按钮本身就是确认，命令总线两阶段审批
对这类内容质量确认门自动放行）。
"""
from __future__ import annotations

from app.capabilities import inputs as I
from app.capabilities.commands import build_command as _cmd
from app.capabilities.handlers import series as h_series
from app.capabilities.registry import CommandSpec
from app.capabilities.schemas import ConfirmationPolicy, IdempotencyPolicy, RiskLevel


def commands() -> list[CommandSpec]:
    return [
        _cmd(
            "series.film_start",
            title="启动连播台",
            description="按连续集号区间串行跑完映射/分镜/确认/生成/成片，再合并为连播成片",
            input_model=I.SeriesFilmStartInput,
            risk=RiskLevel.R2_MATERIAL,
            confirmation=ConfirmationPolicy.NEVER,
            idempotency=IdempotencyPolicy.RECOMMENDED,
            scopes={"manju:generation-media"},
            side_effect="creates_series_film_run",
            handler=h_series.film_start,
            rest_routes=("POST /api/projects/{project_id}/series-film",),
            tags=("series", "video"),
        ),
        _cmd(
            "series.film_pause",
            title="暂停连播台",
            description="协作式暂停：当前集当前步骤完成后停止，可继续",
            input_model=I.SeriesFilmControlInput,
            risk=RiskLevel.R1_REVERSIBLE,
            confirmation=ConfirmationPolicy.NEVER,
            idempotency=IdempotencyPolicy.RECOMMENDED,
            scopes={"manju:generation-media"},
            side_effect="pauses_series_film_run",
            handler=h_series.film_pause,
            rest_routes=("POST /api/projects/{project_id}/series-film/pause",),
            tags=("series", "video"),
        ),
        _cmd(
            "series.film_resume",
            title="继续连播台",
            description="从第一个未完成的集/步骤继续",
            input_model=I.SeriesFilmControlInput,
            risk=RiskLevel.R2_MATERIAL,
            confirmation=ConfirmationPolicy.NEVER,
            idempotency=IdempotencyPolicy.RECOMMENDED,
            scopes={"manju:generation-media"},
            side_effect="resumes_series_film_run",
            handler=h_series.film_resume,
            rest_routes=("POST /api/projects/{project_id}/series-film/resume",),
            tags=("series", "video"),
        ),
    ]
