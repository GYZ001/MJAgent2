"""连播台（series film）：跨集串行编排入口。

产品设定：用户在前端选闭区间 [from, to]（连续集号，跨度 1~10，允许单集），
后端建一条项目级 ``workflow_runs``（``workflow_type='series_film'``）运行，
严格串行地对每一集依次跑映射台→分镜台→确认→生成台→成片台；任一步失败即停
在那一集（fail-closed，不跳过、不兜底），用户在对应工作台修好后点「继续」
从第一个未完成步骤接着跑；已满足完成判据的步骤直接标 ``skipped``。全部集
成片后自动合并成一部连播成片
（``projects/{project_id}/series/ep{from}-ep{to}/film.mp4``）。

Layer：``app.domain.series_ops`` = 5（``app/LAYERS.toml``），与 ``app.domain``
前缀同层。本包是真包（不是 ``exec()`` 聚合外观），子模块之间用
``from . import x`` 互相访问、调用处走 ``x.name(...)`` 属性查找——不用
``from .x import name``。这样 ``monkeypatch.setattr(x模块, "name", stub)``
单条打桩即可覆盖包内全部调用点，不需要额外的 ``patch_series_ops_everywhere``
helper（那类 helper 是为了补救 ``from .sibling import name`` 在每个引用处各
留一份私有副本的问题，本包从设计上就没有这个问题，见 CLAUDE.md「拆包会静默
废掉 monkeypatch」）。

子模块：
- ``state``：运行状态的形状、持久化（``workflow_runs.config_snapshot_json``）
  与到契约 ``SeriesRun`` 的投影。
- ``stages``：映射/分镜/确认/生成/成片五个步骤的完成判据、启动与等待。
- ``merge``：五步全部完成后，把各集成片用 ffmpeg 拼接为连播成片。
- ``orchestrator``：串行主循环 + 四条路由对应的核心函数（启动/暂停/继续）。
- ``routes``：四条 REST 路由的薄封装，只做前置校验与响应整形。
- ``recovery``：开机恢复因服务重启而中断的连播台运行。
"""
from __future__ import annotations

from . import routes  # noqa: F401 -- 导入以在装饰器执行期把 4 条路由注册到共享 router
from .recovery import recover_series_film_runs

__all__ = ["recover_series_film_runs"]
