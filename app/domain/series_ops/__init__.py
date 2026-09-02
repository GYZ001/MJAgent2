"""连播任务台（series task console）：项目级串行任务队列 + 单任务编排。

产品设定（docs/series_task_console_plan.md，2026-09-02 冻结）：用户把整部小说
按每 ``group_size`` 集切成若干「连播任务」（大集），每个任务是一个可独立触发、
独立查看、独立出片的区间 ``[episode_from, episode_to]``；任务进队列后严格
串行执行——依次对区间内每一集跑映射台→分镜台→确认→生成台→成片台，全部集
成片后自动合并成一部连播成片。失败标 ``failed`` 并继续下一个任务，连续 3 个
失败自动暂停整队。已完成的任务可勾选批量导出（硬链接 + manifest + 下载清单）。

Layer：``app.domain.series_ops`` = 5（``app/LAYERS.toml``），与 ``app.domain``
前缀同层。本包是真包（不是 ``exec()`` 聚合外观），子模块之间用
``from . import x`` 互相访问、调用处走 ``x.name(...)`` 属性查找——不用
``from .x import name``，这样 ``monkeypatch.setattr(x模块, "name", stub)``
单条打桩即可覆盖包内全部调用点（见 ``tests/test_series_ops_monkeypatch_guard.py``）。

子模块：
- ``tasks``：``series_tasks`` 表读写、切分计划、TaskSummary/TaskDetail 投影、
  队列状态的原子转移（mark_running/mark_queued_again/mark_idle/...）。
- ``queue``：项目级串行 runner、入队/出队/暂停/继续、连续失败自动停队。
- ``state``：单任务进度树的形状与持久化（写进 ``series_tasks.progress_json``）。
- ``stages``：映射/分镜/确认/生成/成片五个步骤的完成判据、启动与等待。
- ``merge``：五步全部完成后，把各集成片用 ffmpeg 拼接为连播成片。
- ``orchestrator``：跑一个任务的五台 + merge 主循环。
- ``exports``：打包导出（硬链接 + manifest + 下载清单）的生成与列举。
- ``routes_tasks``/``routes_exports``：REST 路由的薄封装，只做前置校验与响应
  整形，业务判断都在 ``tasks``/``queue``/``orchestrator``/``exports`` 里。
- ``recovery``：开机恢复——旧单例模型遗留的 ``workflow_runs`` 半边状态清理
  （标 CANCELLED）+ 因进程重启卡住的任务复位为 queued + 重启队列 runner。
"""
from __future__ import annotations

from . import routes_tasks as routes_tasks  # noqa: F401 -- 触发路由注册副作用；plan 必须先于 {task_id} 注册
from . import routes_exports as routes_exports  # noqa: F401 -- 触发路由注册副作用
from .recovery import recover_series_film_runs

__all__ = ["recover_series_film_runs"]
