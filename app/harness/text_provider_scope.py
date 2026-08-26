"""Ambient override for which text-model provider a business stage's chat calls use.

世界书/映射台/分镜台三个环节允许各自在项目上配一个专属文本模型（``projects.
bible_text_provider`` / ``script_text_provider`` / ``board_text_provider``，见
app/db.py 迁移与 app/model_registry.py::resolve_stage_text_provider）。三个环节
的实际生成代码分散在 app/stages.py、app/production/prep_pack.py、
app/screenplay_scene_shards.py、app/video_prompt_ai.py 等多个共享模块里，
在每一个 model_gateway.chat()/chat_structured() 调用点都显式传 provider 既
易漏也易在共享函数上产生"这个函数到底该用谁的模型"的歧义。

改用一个独立的 ContextVar：领域层的任务入口（app/domain/bible_ops.py::
_bible_task、app/domain/screenplay_ops.py::_screenplay_task、
app/domain/storyboard_ops.py::_recorded_storyboard_task）在发起本环节的生成
调用前用 ``stage_text_provider(...)`` 包一层，包裹范围内不管调用链路经过多少
共享模块，最终到达 model_gateway.chat() 时都会读到同一个覆盖值。

传播性质与 app/observability/tracing.py 的 set_worker_trace 一致：这三个入口
都在同一个 asyncio Task 里 await 到底，没有跨线程池的边界，因此不是曾经让鉴权
静默 fail-open 的"同步依赖里写 ContextVar"陷阱（那次是 Starlette 用
run_in_threadpool 跑同步依赖，线程内的写入不会传回请求所在的 Context）。这里
从 set 到读全部发生在同一个 Context 里，一次 with 块内的普通写入即可。
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_TEXT_PROVIDER_OVERRIDE: ContextVar[str | None] = ContextVar(
    "mjagent_stage_text_provider_override", default=None,
)


def current_stage_text_provider() -> str | None:
    """当前生效的分环节文本 provider 覆盖；未处在任何 stage_text_provider 作用域
    内，或该环节未配专属模型时为 None（调用方回落到全局默认文本 provider）。"""
    return _TEXT_PROVIDER_OVERRIDE.get()


@contextmanager
def stage_text_provider(provider: str | None) -> Iterator[None]:
    """在 with 块内把该环节选定的文本 provider 设为环境默认。

    ``provider`` 传 None 等价于不覆盖（沿用全局默认），调用方不必先判空。
    """
    token = _TEXT_PROVIDER_OVERRIDE.set(provider)
    try:
        yield
    finally:
        _TEXT_PROVIDER_OVERRIDE.reset(token)
