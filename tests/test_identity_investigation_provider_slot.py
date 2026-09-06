"""身份调查的工具对话必须过供应商请求槽位（2026-09-06 第 5 轮：绕过槽位直打网关，30 集并发把 HiAgent 打到用内容审核话术回绝）。"""
from __future__ import annotations

import ast
import inspect

from app.portraits import identity_investigation


def test_chat_with_tools_source_takes_the_provider_call_slot() -> None:
    """conftest 的 autouse 桩替换了 ``_chat_with_tools`` 本身，所以按源码结构断言：
    函数体里必须经 ``run_with_provider_call_slot`` 调 ``hiagent.chat_with_tools``。"""
    tree = ast.parse(inspect.getsource(identity_investigation))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_chat_with_tools"
    )
    calls = [
        n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")
        for n in ast.walk(fn) if isinstance(n, ast.Call)
    ]
    assert "run_with_provider_call_slot" in calls and "chat_with_tools" in calls
