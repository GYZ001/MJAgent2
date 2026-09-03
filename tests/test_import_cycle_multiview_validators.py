"""app.multiview ↔ app.validators 的导入环必须在任何导入顺序下都能加载。

2026-09-03 实测：`import app.multiview` 作为进程里第一个 app 导入时，multiview 模块级
`from app.validators import match_scene_name` 触发 validators 门面 → resource_forecast →
`from app.multiview import manifest_production_blockers`，此时 multiview 只初始化了一半，
直接 ImportError；单跑 tests/test_storyboard_gate_consistency.py 就是这样在收集期被打死，
而全量测试只是碰巧导入顺序不同。用子进程（干净解释器）逐个顺序验证，进程内测试验不到。
"""
from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.mark.parametrize("first_import", [
    "app.multiview",
    "app.validators",
    "app.validators.resource_forecast",
    "app.main",
])
def test_fresh_interpreter_can_import_in_any_order(first_import: str) -> None:
    code = f"import {first_import}; import app.multiview, app.validators.resource_forecast; print('ok')"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, f"first import {first_import!r} failed:\n{result.stderr[-1500:]}"
    assert result.stdout.strip().endswith("ok")
