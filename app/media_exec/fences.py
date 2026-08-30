"""媒体作业围栏异常（真包拆分自 ``run_job.py``）。

``LeaseLost``（``app.media_exec.common``）之外的六个围栏异常里，剩下这五个—— ``ReviewDependencyFence``（入队快照过期）、``VideoPlanStaleFence``（供应商结果
不属于当前计划）、``VideoInputRepairRequired``（计划仍有效但本地输入待修）、
``ProviderCreateUnresolved``（供应商可能已接单但没有可持久化的任务句柄）、
``VideoInflightAdmissionDeferred``（提交侧原子在途名额占用失败）——原来都定义
在 ``run_job.py`` 内部。历史上它们曾有两份互不相识的拷贝（``app/worker.py`` 把
``media_exec/*.py`` 第二次 ``exec()`` 进自己的命名空间），``except
worker.LeaseLost`` 抓不住 ``app.media_exec`` 那份实例，一路穿透到顶层被当成未知
故障。拆成真包后这五个异常单独成叶子文件，`.authority`/`.checkpoints`/
`.input_boundary`/`.job_state`/`.run_job` 等所有需要判定/抛出它们的子模块都
``from .fences import name`` 同一个类对象——全仓任何一处 ``except
XxxFence`` 与任何一处 ``raise XxxFence`` 必然是同一个类，不会重演上述穿透。
"""

from __future__ import annotations


class ReviewDependencyFence(RuntimeError):
    """The upstream/asset snapshot captured at enqueue is no longer current."""


class VideoPlanStaleFence(RuntimeError):
    """The provider result belongs to a superseded or stale video plan."""


class VideoInputRepairRequired(RuntimeError):
    """The planned mode is still valid, but its local input assets need repair."""


class ProviderCreateUnresolved(RuntimeError):
    """The provider may have accepted create, but no durable task handle exists."""


class VideoInflightAdmissionDeferred(RuntimeError):
    """The atomic submit-side inflight claim found no capacity."""

__all__ = [name for name in globals() if not name.startswith("__")]
