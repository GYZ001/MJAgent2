"""身份调查的工具对话必须过供应商请求槽位（2026-09-06 第 5 轮：绕过槽位直打网关，30 集并发把 HiAgent 打到用内容审核话术回绝）。

2026-09-23 起 ``identity_investigation._chat_with_tools`` 改为委托给共享网关入口
``app.harness.model_gateway_tools.chat_with_tools``，供应商请求槽位随之从前者的
函数体挪进了后者（见 tests/test_model_gateway_chat_with_tools.py 的完整行为覆盖）。
这里保留原有的纯源码结构断言、分两段核对整条调用链，不依赖 conftest 的 autouse 桩：
桩替换的是 ``_chat_with_tools`` 这个名字本身，静态解析源码不受影响。"""
from __future__ import annotations

import ast
import inspect

from app.harness import model_gateway_tools
from app.portraits import identity_investigation


def _calls_in_async_def(source: str, fn_name: str) -> list[str]:
    tree = ast.parse(source)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == fn_name
    )
    return [
        n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")
        for n in ast.walk(fn) if isinstance(n, ast.Call)
    ]


def test_chat_with_tools_source_takes_the_provider_call_slot() -> None:
    """身份调查侧：``_chat_with_tools`` 必须把请求委托给网关入口
    ``chat_with_tools``，不得自己绕过去直连供应商适配层。"""
    delegate_calls = _calls_in_async_def(
        inspect.getsource(identity_investigation), "_chat_with_tools"
    )
    assert "chat_with_tools" in delegate_calls


def test_gateway_chat_with_tools_source_takes_the_provider_call_slot() -> None:
    """网关侧：共享入口的函数体里必须经 ``run_with_provider_call_slot`` 调
    供应商适配层的 ``chat_with_tools``——身份调查与其它未来调用方共享这一份槽位
    包装，不必各自手搓。"""
    gateway_calls = _calls_in_async_def(
        inspect.getsource(model_gateway_tools), "chat_with_tools"
    )
    assert "run_with_provider_call_slot" in gateway_calls and "chat_with_tools" in gateway_calls
