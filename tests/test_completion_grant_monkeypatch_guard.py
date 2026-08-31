"""Guard against the app.completion_grant package-split monkeypatch trap.

``app/completion_grant.py`` used to be one file (2,467 行 = 500 行上限的 5 倍)；
包内每个调用点共享同一个模块命名空间，所以
``monkeypatch.setattr(completion_grant, "name", value)`` 能够到所有调用方。
2026-08-31（#75）它被拆成 ``app.completion_grant`` 包，8 个子模块各自在 import 时
绑定了自己那份副本（``from .budget_authority import
authorize_episode_video_budget_absolute`` 之类）。此后只打包级再导出属性**够不到**
真正调用它的子模块——而且没有异常、没有报错：桩静默失效，测试照常通过，验证的却是
一条从未被替换的代码路径。

拆分当场实测到的两种形态：
  * ``grants_issue.py`` 模块级绑定 ``authorize_episode_video_budget_absolute``，
    包属性上的桩对它无效，测试表现为 ``DID NOT RAISE``；
  * ``get_conn`` 这类底层依赖原本是单文件的模块属性，拆包后包命名空间里根本没有，
    直接 ``AttributeError``。

解法是 ``tests/conftest.py`` 的 ``patch_completion_grant_everywhere(monkeypatch,
name, value)``：走遍每个子模块，凡是绑了该名字的都打上，重现拆包前的单命名空间
语义。本文件扫描 ``tests/`` 下所有文件，发现裸形态就失败——helper 自身的实现除外，
那里**就是**「everywhere」该做的事。

**本守卫额外覆盖 CLAUDE.md 记录的一个已知盲区：循环变量形态**
``for m in (a, b, completion_grant): setattr(m, ...)``。既有的同类守卫只看
``setattr`` 的第一个实参是不是裸包名，循环里它是个循环变量，静态上看不出来。
这个形态不是假想——本次拆分在 ``test_video_completion_authority.py`` 与
``test_review_wall_prd.py`` 里实际撞到 5 处。

不会被标记的（有意为之）：``completion_grant.<模块对象>.attr`` 这种打在真实共享
模块对象上的属性，拆包从来没有破坏过它——只有「被包再导出、按值拷进各 importer
命名空间」的名字才受影响。
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
CONFTEST_PATH = TESTS_DIR / "conftest.py"
HELPER_NAME = "patch_completion_grant_everywhere"
PKG_ALIAS = "completion_grant"
PKG_PATH = "app.completion_grant"


def _helper_exempt_span(tree: ast.Module) -> tuple[int, int]:
    """helper 自身实现的行范围——唯一允许出现裸形态的地方。

    只豁免这个函数的 lineno..end_lineno，而不是整个 conftest.py：将来往
    conftest 里加的其它 helper 仍然受本守卫检查。
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == HELPER_NAME:
            assert node.end_lineno is not None
            return node.lineno, node.end_lineno
    raise AssertionError(
        f"{HELPER_NAME}() 不在 {CONFTEST_PATH} 里——本守卫的豁免范围算不出来。"
        "helper 是被改名还是被删了？请同步改这里的 HELPER_NAME，不要直接跳过扫描。"
    )


def _is_bare_pkg_name(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == PKG_ALIAS


def _is_bare_pkg_attr_string(node: ast.expr) -> bool:
    """``"app.completion_grant.<单个标识符>"``。

    不含 ``"app.completion_grant"`` 本身（整体替换模块对象是另一回事），也不含更深的
    点号路径（那是打在真实共享模块对象上的属性，拆包没有破坏过）。
    """
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return False
    parts = node.value.split(".")
    return len(parts) == 3 and ".".join(parts[:2]) == PKG_PATH and parts[2].isidentifier()


def _loop_vars_bound_to_pkg(tree: ast.Module) -> dict[str, int]:
    """``for m in (..., completion_grant, ...)`` 里的循环变量名 -> 行号。

    这是既有同类守卫的盲区：循环里 setattr 的第一个实参是循环变量，静态上看不出
    它绑的是包。这里先把这类变量识别出来，下面再按普通裸形态处理。
    """
    bound: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.For) or not isinstance(node.target, ast.Name):
            continue
        if not isinstance(node.iter, (ast.Tuple, ast.List, ast.Set)):
            continue
        if any(_is_bare_pkg_name(item) for item in node.iter.elts):
            bound[node.target.id] = node.lineno
    return bound


def _violations_in_file(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    exempt_start, exempt_end = (-1, -1)
    if path == CONFTEST_PATH:
        exempt_start, exempt_end = _helper_exempt_span(tree)

    loop_vars = _loop_vars_bound_to_pkg(tree)
    advice = (
        f"改用 tests.conftest.{HELPER_NAME}(monkeypatch, name, value)。"
    )
    violations: list[str] = []
    for node in ast.walk(tree):
        lineno = getattr(node, "lineno", None)
        if lineno is not None and exempt_start <= lineno <= exempt_end:
            continue

        if isinstance(node, ast.Call):
            func = node.func
            is_setattr = (isinstance(func, ast.Attribute) and func.attr == "setattr") or (
                isinstance(func, ast.Name) and func.id == "setattr"
            )
            is_patch_object = isinstance(func, ast.Attribute) and func.attr == "object"
            is_patch = (isinstance(func, ast.Attribute) and func.attr == "patch") or (
                isinstance(func, ast.Name) and func.id == "patch"
            )

            if (is_setattr or is_patch_object) and node.args:
                target = node.args[0]
                if _is_bare_pkg_name(target):
                    violations.append(
                        f"{path}:{node.lineno}: 直接打 {PKG_ALIAS} 包属性只改到包自身的"
                        f"再导出，够不到真正绑定该名字的子模块——静默无效。{advice}"
                    )
                elif isinstance(target, ast.Name) and target.id in loop_vars:
                    violations.append(
                        f"{path}:{node.lineno}: 循环变量 {target.id!r} 来自第 "
                        f"{loop_vars[target.id]} 行含 {PKG_ALIAS} 的元组，等同于直接打"
                        f"包属性——静默无效。把 {PKG_ALIAS} 从元组里拿出来，{advice}"
                    )

            if (is_setattr or is_patch) and node.args and _is_bare_pkg_attr_string(node.args[0]):
                violations.append(
                    f"{path}:{node.lineno}: 字符串形态 "
                    f"{ast.literal_eval(node.args[0])!r} 同样只到包级再导出。{advice}"
                )

        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == PKG_ALIAS
                ):
                    violations.append(
                        f"{path}:{node.lineno}: 直接赋值 {PKG_ALIAS}.{target.attr} = ... "
                        f"只重绑包属性——静默无效。{advice}"
                    )

    return violations


def test_no_bare_completion_grant_package_monkeypatch() -> None:
    test_files = sorted(TESTS_DIR.glob("*.py"))
    # 扫描范围为空必须失败，不能读成「没有问题」——tests/ 被移动改名、或 CI 工作目录
    # 弄错，都会让守卫悄悄不再扫任何东西然后因为错误的原因变绿。
    assert test_files, f"{TESTS_DIR} 下没有 .py 文件——扫描范围是空的"
    assert CONFTEST_PATH in test_files, "扫描范围里应当包含 tests/conftest.py"

    violations: list[str] = []
    for path in test_files:
        violations.extend(_violations_in_file(path))
    assert violations == [], "\n".join(violations)


def test_guard_catches_the_loop_variable_blind_spot(tmp_path: Path) -> None:
    """反向断言：证明循环形态真的能被抓到，不是空测试。

    CLAUDE.md 把这个形态列为既有守卫的已知盲区。本次拆分实际撞到 5 处，所以这里
    用一份合成源码钉住它。
    """
    bad = tmp_path / "bad_test.py"
    bad.write_text(
        "import app.completion_grant as completion_grant\n"
        "def test_x(monkeypatch):\n"
        "    for module in (completion_grant, other):\n"
        "        monkeypatch.setattr(module, 'get_conn', None)\n",
        encoding="utf-8",
    )
    found = _violations_in_file(bad)
    assert len(found) == 1, f"循环形态没被抓到：{found}"
    assert "循环变量" in found[0]


def test_guard_catches_the_direct_form(tmp_path: Path) -> None:
    bad = tmp_path / "bad_direct.py"
    bad.write_text(
        "import app.completion_grant as completion_grant\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(completion_grant, 'get_conn', None)\n",
        encoding="utf-8",
    )
    assert len(_violations_in_file(bad)) == 1
