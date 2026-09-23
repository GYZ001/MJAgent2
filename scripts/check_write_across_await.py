#!/usr/bin/env python
"""静态闸门：async 函数里写事务不得跨 await。

2026-09-04/05 B 两次整站无响应的根因是 SQLite 单写锁 + 写事务在协程里跨 await 被握着、
事件循环线程上的同步写等锁把整个循环冻结（见 docs/failure_triage_and_self_heal_plan_2026-09-05.md）。
这里按 AST 检查：在一个 ``async def`` 里，对某个连接执行了 INSERT/UPDATE/DELETE/REPLACE
（``x.execute(...)``/``x.executemany(...)`` 且第一个参数是以这些词开头的字符串字面量）之后、
在同一连接 ``commit()``/``rollback()`` 之前出现了 ``await`` —— 记一条违规。

判据只看语法结构，不猜运行时；漏报（SQL 不是字面量、连接经函数传递）是已知盲区，
但凡报出来的都是真实的「写锁跨 await」。存量走 ``app/WRITE_ACROSS_AWAIT_BASELINE.txt``
棘轮（一行一个 ``路径::函数``，只减不增），新增即拒。

辅助函数是否「泄漏写者」（写了但不 commit/rollback）、是否「委托提交者」（写完转手
给另一个以 commit 收尾的函数）不能只按裸函数名判断——全仓大量同名辅助函数（每包一份
的 ``ensure_tables_on_connection``、写事务回调闭包的通用命名 ``operation``）会把裸名
匹配的结论错误传染到不相干的调用点。调用点解析（import 关系、``__init__.py``/
``app/worker.py`` 式门面再导出、委托提交识别）拆在同目录 ``write_await_resolution.py``
——单纯是体量原因（本文件曾经超过 CLAUDE.md 的 500 行上限），排查记录见该文件头部。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

# 保证无论以什么方式加载本文件（直接跑脚本、``importlib.util.spec_from_file_location``
# 动态加载、subprocess 里用相对/绝对路径跑），同目录的 write_await_resolution 都能被
# import 到——不依赖当前工作目录，也不依赖调用方有没有把 scripts/ 加进 sys.path。
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from write_await_resolution import (  # noqa: E402  # 必须在上面的 sys.path 补丁之后
    _NOT_A_FUNCTION as _NOT_A_FUNCTION,
    _NotAFunction as _NotAFunction,
    _ResolutionContext as _ResolutionContext,
    _Scope as _Scope,
    _follow_reexport as _follow_reexport,
    _functions as _functions,
    _is_trailing_commit as _is_trailing_commit,
    _resolve_call as _resolve_call,
)

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
BASELINE = APP / "WRITE_ACROSS_AWAIT_BASELINE.txt"
WRITE_PREFIXES = ("INSERT", "UPDATE", "DELETE", "REPLACE")


def _sql_literal(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = [v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)]
        return "".join(parts) if parts else None
    return None


def _receiver(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id
    return None


def _is_write_call(node: ast.AST) -> str | None:
    """返回被写的连接变量名；不是写语句返回 None。"""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    if node.func.attr not in {"execute", "executemany"} or not node.args:
        return None
    sql = _sql_literal(node.args[0])
    if sql is None:
        return None
    if sql.lstrip().upper().startswith(WRITE_PREFIXES):
        return _receiver(node)
    return None


def _is_close_call(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"commit", "rollback"}:
        return _receiver(node)
    return None


def _called_name(node: ast.AST) -> str | None:
    """调用表达式的裸名字（``foo(...)`` → foo，``mod.foo(...)`` → foo）。"""
    if not isinstance(node, ast.Call):
        return None
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


_CLOSERS = {"commit", "rollback"}


class _FunctionScan(ast.NodeVisitor):
    """按源码顺序扫一个函数体（不进入嵌套函数），维护「有未提交写」状态。

    写的来源两种：本函数里的字面量写语句；或调用了「自身含未提交写」的辅助函数。
    关闭的来源两种：本函数里直接调用 ``commit()``/``rollback()``（按裸名，不认收发
    是否同一个连接）；或调用了「自身以无条件 commit 收尾」的委托提交函数。两种调用
    都先按 import 关系解析成 (模块路径, 函数名)：解析出确凿结果就用 ``qualified_leaky``
    / ``qualified_committers`` 精确匹配。解析不了时两边不对称——开：退回按裸名匹配
    ``flat_leaky``（不放过任何真隐患，代价是过度保守）；关：解析不了就**不**当作
    关闭（「谁提交了」必须证据确凿，误判关闭会放过真隐患，比误判泄漏危险）。
    """

    def __init__(
        self, *, flat_leaky: set[str], qualified_leaky: set[tuple[Path, str]],
        qualified_committers: set[tuple[Path, str]], scope: _Scope,
    ) -> None:
        self.flat_leaky = flat_leaky
        self.qualified_leaky = qualified_leaky
        self.qualified_committers = qualified_committers
        self.scope = scope
        self.open = False
        self.wrote = False
        self.closed = False
        self.hits: list[int] = []

    def _resolution_for(self, node: ast.AST) -> tuple[Path, str] | _NotAFunction | None:
        return _resolve_call(node, self.scope) if isinstance(node, ast.Call) else None

    def _opens_write(
        self, node: ast.AST, call_name: str | None, resolved: tuple[Path, str] | _NotAFunction | None,
    ) -> bool:
        if not isinstance(node, ast.Call):
            return False
        if _is_write_call(node):
            return True
        if call_name is None or call_name in _CLOSERS:
            return False
        if resolved is None:
            return call_name in self.flat_leaky  # 解析不了，按原裸名逻辑保守兜底
        if resolved is _NOT_A_FUNCTION:
            return False
        return resolved in self.qualified_leaky

    def _closes_write(self, call_name: str | None, resolved: tuple[Path, str] | _NotAFunction | None) -> bool:
        if call_name in _CLOSERS:
            return True
        return isinstance(resolved, tuple) and resolved in self.qualified_committers

    def generic_visit(self, node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return
        if isinstance(node, ast.Await) and self.open:
            self.hits.append(node.lineno)
        name = _called_name(node)
        resolved = self._resolution_for(node)
        if self._opens_write(node, name, resolved):
            self.open = True
            self.wrote = True
        if self._closes_write(name, resolved):
            self.open = False
            self.closed = True
        super().generic_visit(node)


def _leaky_writers(trees: dict[Path, ast.AST], ctx: _ResolutionContext) -> tuple[set[str], set[tuple[Path, str]]]:
    """第一遍：含写语句（字面量、或调用已知泄漏者）但从不 commit/rollback 的函数。

    按模块限定名收敛出 ``qualified``；解析不了的调用点并行喂给按裸名收敛的
    ``flat``（给 ``scan_tree`` 终扫时的保守兜底用——``flat`` 恒为 ``qualified``
    涉及名字的超集，精确匹配只会让「解析得了」的调用点更精确，不会让「解析不了」
    的调用点漏判）。传递闭包最多迭代 3 轮，够覆盖「helper 调 helper」的常见深度。
    """
    flat: set[str] = set()
    qualified: set[tuple[Path, str]] = set()
    for _ in range(3):
        before = (len(flat), len(qualified))
        for path, tree in trees.items():
            for fn in _functions(tree):
                scope = ctx.scope_for(path, fn)
                scan = _FunctionScan(
                    flat_leaky=flat, qualified_leaky=qualified,
                    qualified_committers=ctx.qualified_committers, scope=scope,
                )
                for stmt in fn.body:
                    scan.visit(stmt)
                if scan.wrote and not scan.closed:
                    flat.add(fn.name)
                    qualified.add((path, fn.name))
        if (len(flat), len(qualified)) == before:
            break
    return flat, qualified


def scan_tree(root: Path = APP) -> list[str]:
    trees = {
        path: ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for path in sorted(root.rglob("*.py"))
    }
    ctx = _ResolutionContext(trees, ROOT)
    flat_leaky, qualified_leaky = _leaky_writers(trees, ctx)
    found: list[str] = []
    for path, tree in trees.items():
        for fn in _functions(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            scope = ctx.scope_for(path, fn)
            scan = _FunctionScan(
                flat_leaky=flat_leaky, qualified_leaky=qualified_leaky,
                qualified_committers=ctx.qualified_committers, scope=scope,
            )
            for stmt in fn.body:
                scan.visit(stmt)
            if scan.hits:
                found.append(f"{path.relative_to(ROOT).as_posix()}::{fn.name}")
    return found


def main(argv: list[str]) -> int:
    seed = "--seed-baseline" in argv
    found = scan_tree()
    baseline = set(BASELINE.read_text(encoding="utf-8").split()) if BASELINE.exists() else set()
    if seed:
        BASELINE.write_text("\n".join(found) + ("\n" if found else ""), encoding="utf-8")
        print(f"已播种基线 {len(found)} 条 → {BASELINE.relative_to(ROOT)}")
        return 0
    new = sorted(set(found) - baseline)
    fixed = sorted(baseline - set(found))
    for item in new:
        print(f"  新增违规  {item}")
    if fixed:
        print(f"基线里已修好 {len(fixed)} 条，请从 {BASELINE.name} 删除：{', '.join(fixed)}")
    if new or fixed:
        print(f"FAIL: 写事务跨 await —— 新增 {len(new)} 条，待收基线 {len(fixed)} 条")
        return 1
    print(f"OK: 写事务跨 await 0 条新增（存量基线 {len(baseline)} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
