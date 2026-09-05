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
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

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

    写的来源两种：本函数里的字面量写语句；或调用了「自身含未提交写」的辅助函数
    （``leaky``，第一遍扫描得到）。任何 ``commit()``/``rollback()`` 调用都视为关闭。
    """

    def __init__(self, leaky: set[str]) -> None:
        self.leaky = leaky
        self.open = False
        self.wrote = False
        self.closed = False
        self.hits: list[int] = []

    def generic_visit(self, node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return
        if isinstance(node, ast.Await) and self.open:
            self.hits.append(node.lineno)
        name = _called_name(node)
        if _is_write_call(node) or (name in self.leaky and name not in _CLOSERS):
            self.open = True
            self.wrote = True
        if name in _CLOSERS:
            self.open = False
            self.closed = True
        super().generic_visit(node)


def _functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _leaky_writers(trees: dict[Path, ast.AST]) -> set[str]:
    """第一遍：含写语句（字面量或调用已知泄漏者）但从不 commit/rollback 的函数名；
    传递闭包最多迭代 3 轮，够覆盖「helper 调 helper」的常见深度。"""
    leaky: set[str] = set()
    for _ in range(3):
        before = len(leaky)
        for tree in trees.values():
            for fn in _functions(tree):
                scan = _FunctionScan(leaky)
                for stmt in fn.body:
                    scan.visit(stmt)
                if scan.wrote and not scan.closed:
                    leaky.add(fn.name)
        if len(leaky) == before:
            break
    return leaky


def scan_tree(root: Path = APP) -> list[str]:
    trees = {
        path: ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for path in sorted(root.rglob("*.py"))
    }
    leaky = _leaky_writers(trees)
    found: list[str] = []
    for path, tree in trees.items():
        for fn in _functions(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            scan = _FunctionScan(leaky)
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
