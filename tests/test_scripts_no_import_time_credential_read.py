"""守住「回归脚本不得在导入期读会话凭证」。

`data/regression_session_token.txt` 是 gitignore 的本地产物。五个回归脚本曾在
模块级直接读它：

    SESSION = (ROOT / "data" / "regression_session_token.txt").read_text(...).strip()

而 `tests/` 会导入这些脚本（只为取其中的纯判据函数）。于是任何没有该文件的
环境——全新 clone、CI、`git worktree add` 出来的干净树——在 pytest 的**收集
阶段**就抛 `FileNotFoundError`：

    Interrupted: 5 errors during collection

**整个套件一个测试都不会跑**，而且 16 秒就退出、输出里一个 `FAILED` 都没有，
看起来像通过。2026-08-30 实测：/tmp/wt-verify 里跑全量得到 5 errors / 0 tests。

危害高于普通的路径硬编码（那类见 tests/test_scripts_hardcoded_paths_exist.py）：
它把「无法验证任何东西」伪装成一次快速成功。

修法是 `scripts/session_token.py` 的惰性 `session_token()`，调用点必须在函数体
内。本文件用 AST 守住它不被写回模块级——判据是「模块级出现了对凭证的读取」，
不是文件名黑名单。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

#: 会话凭证的读取入口。新增读取方式时补进来。
_CREDENTIAL_READERS = {"session_token", "_load_session"}


def _module_level_nodes(tree: ast.Module):
    """模块级语句（含 if/try 包住的），但不进入函数体与类体。"""
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield node
        for child in ast.iter_child_nodes(node):
            stack.append(child)


def _script_paths() -> list[Path]:
    return sorted(p for p in SCRIPTS.glob("*.py") if p.name != "__init__.py")


@pytest.mark.parametrize("path", _script_paths(), ids=lambda p: p.name)
def test_no_module_level_credential_read(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders = []
    for node in _module_level_nodes(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name in _CREDENTIAL_READERS:
                offenders.append((name, node.lineno))
    assert not offenders, (
        f"{path.relative_to(ROOT)} 在模块级读取会话凭证："
        + "、".join(f"第 {line} 行 {name}()" for name, line in offenders)
        + "。导入期读盘会让没有该文件的环境在 pytest 收集阶段整体失败"
        "（Interrupted: N errors during collection，零个测试执行）。"
        "把调用挪进函数体。"
    )


def test_credential_helper_itself_does_not_read_at_import() -> None:
    """helper 自己也不能在导入期读——否则等于把问题换个地方保留。"""
    tree = ast.parse((SCRIPTS / "session_token.py").read_text(encoding="utf-8"))
    for node in _module_level_nodes(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None)
            assert name not in {"read_text", "open"}, (
                f"scripts/session_token.py 第 {node.lineno} 行在模块级读盘"
            )


def test_guard_would_catch_the_original_defect(tmp_path: Path) -> None:
    """反向断言：证明这条守卫确实能抓出修复前的写法，不是空测试。"""
    bad = tmp_path / "bad_script.py"
    bad.write_text(
        "from scripts.session_token import session_token\n"
        "SESSION = session_token()\n",
        encoding="utf-8",
    )
    tree = ast.parse(bad.read_text(encoding="utf-8"))
    found = [
        getattr(n.func, "id", None) or getattr(n.func, "attr", None)
        for n in _module_level_nodes(tree)
        if isinstance(n, ast.Call)
    ]
    assert "session_token" in found, "守卫抓不到模块级调用，判据本身是坏的"
