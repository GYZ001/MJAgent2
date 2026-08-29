#!/usr/bin/env python3
"""后端模块依赖图与强连通分量测量。

`docs/coupling_review_2026-08-29.md` 把「团规模单调下降」定为解耦是否真的发生的
唯一判据，但它依赖的统计脚本没有留在仓库里，判据因此不可运行。本文件把那套测量
固化下来，让每一步解耦之后都能重新取数对比。

判据只从代码事实推导：模块集合来自 `app/**/*.py` 实际存在的文件，依赖边来自 AST
解析出的 import 语句，不维护任何模块白名单。

用法:
    .venv/bin/python scripts/arch_graph.py            # 摘要
    .venv/bin/python scripts/arch_graph.py --json     # 机器可读，用于前后对比
    .venv/bin/python scripts/arch_graph.py --top 20   # 调整榜单长度
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"


def module_name(path: Path) -> str:
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def collect_modules() -> dict[str, Path]:
    return {module_name(p): p for p in sorted(APP.rglob("*.py"))}


def resolve(target: str, modules: set[str]) -> str | None:
    """把 import 目标收敛到已知模块；`app.x.y` 里的 y 可能是符号而非模块。"""
    if target in modules:
        return target
    parent = target.rsplit(".", 1)[0]
    return parent if parent in modules else None


def parse_edges(
    name: str, path: Path, modules: set[str]
) -> tuple[list[tuple[str, str, bool]], int, int]:
    """返回 (边列表, 顶层定义数, 行数)。边为 (目标, 符号, 是否函数内延迟导入)。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    except SyntaxError:
        return [], 0, 0

    loc = len(path.read_text(encoding="utf-8").splitlines())
    toplevel = sum(
        1
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    )

    # 记录每个 import 节点是否位于函数体内（延迟导入）。
    deferred: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(node):
                if isinstance(inner, (ast.Import, ast.ImportFrom)):
                    deferred.add(id(inner))

    edges: list[tuple[str, str, bool]] = []
    for node in ast.walk(tree):
        lazy = id(node) in deferred
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app"):
                    target = resolve(alias.name, modules)
                    if target and target != name:
                        edges.append((target, alias.name.rsplit(".", 1)[-1], lazy))
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 相对导入
                base = name.rsplit(".", 1)[0] if "." in name else name
                mod = f"{base}.{node.module}" if node.module else base
            else:
                mod = node.module or ""
            if not mod.startswith("app"):
                continue
            for alias in node.names:
                # `from app.domain import video_ops` -> 目标其实是子模块
                target = resolve(f"{mod}.{alias.name}", modules) or resolve(mod, modules)
                if target and target != name:
                    edges.append((target, alias.name, lazy))
    return edges, toplevel, loc


def tarjan(graph: dict[str, set[str]]) -> list[list[str]]:
    """迭代式 Tarjan，避免大图递归爆栈。"""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: dict[str, bool] = {}
    stack: list[str] = []
    result: list[list[str]] = []
    counter = 0

    for root in graph:
        if root in index:
            continue
        work: list[tuple[str, iter]] = [(root, iter(sorted(graph[root])))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack[root] = True

        while work:
            node, children = work[-1]
            advanced = False
            for child in children:
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack[child] = True
                    work.append((child, iter(sorted(graph.get(child, set())))))
                    advanced = True
                    break
                if on_stack.get(child):
                    low[node] = min(low[node], index[child])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                component = []
                while True:
                    top = stack.pop()
                    on_stack[top] = False
                    component.append(top)
                    if top == node:
                        break
                result.append(component)
    return result


def find_exec_facades(modules: dict[str, Path]) -> list[str]:
    """exec() 源码注入的聚合外观——它们的依赖不出现在 import 图里。"""
    facades = []
    for name, path in modules.items():
        try:
            src = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if "exec(compile(" in src:
            facades.append(name)
    return sorted(facades)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="输出 JSON 便于前后对比")
    parser.add_argument("--top", type=int, default=12, help="榜单长度")
    args = parser.parse_args()

    modules = collect_modules()
    names = set(modules)

    graph: dict[str, set[str]] = {n: set() for n in names}
    edge_rows: list[tuple[str, str, str, bool]] = []
    stats: dict[str, dict] = {}

    for name, path in modules.items():
        edges, toplevel, loc = parse_edges(name, path, names)
        stats[name] = {"loc": loc, "toplevel": toplevel}
        for target, symbol, lazy in edges:
            graph[name].add(target)
            edge_rows.append((name, target, symbol, lazy))

    components = tarjan(graph)
    largest = max(components, key=len) if components else []
    largest_set = set(largest)
    total_loc = sum(s["loc"] for s in stats.values())
    cycle_loc = sum(stats[m]["loc"] for m in largest)

    # 团内边：按 (源,目标) 聚合出各自携带的符号数
    inner: dict[tuple[str, str], set[str]] = {}
    inner_lazy = 0
    for src, dst, sym, lazy in edge_rows:
        if src in largest_set and dst in largest_set:
            inner.setdefault((src, dst), set()).add(sym)
            if lazy:
                inner_lazy += 1
    single = sum(1 for syms in inner.values() if len(syms) == 1)

    out_deg = {n: len(graph[n]) for n in names}
    in_deg = {n: 0 for n in names}
    for _src, dst in {(s, d) for s, d, _sym, _l in edge_rows}:
        in_deg[dst] += 1

    unique_edges = {(s, d) for s, d, _sym, _l in edge_rows}
    lazy_total = sum(1 for _s, _d, _sym, l in edge_rows if l)

    report = {
        "modules": len(modules),
        "total_loc": total_loc,
        "unique_edges": len(unique_edges),
        "deferred_imports": lazy_total,
        "largest_scc_modules": len(largest),
        "largest_scc_loc": cycle_loc,
        "largest_scc_pct_loc": round(cycle_loc / total_loc * 100, 1) if total_loc else 0,
        "scc_inner_edges": len(inner),
        "scc_inner_single_symbol_edges": single,
        "scc_inner_deferred_edges": inner_lazy,
        "exec_facades": find_exec_facades(modules),
        "biggest_modules": sorted(
            ({"module": n, **stats[n]} for n in stats),
            key=lambda r: -r["loc"],
        )[: args.top],
        "highest_out_degree": sorted(
            ({"module": n, "out": out_deg[n], "in": in_deg[n]} for n in names),
            key=lambda r: -r["out"],
        )[: args.top],
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print(f"后端模块        {report['modules']} 个 / {report['total_loc']:,} 行")
    print(f"依赖边          {report['unique_edges']:,} 条（函数内延迟导入 {report['deferred_imports']:,} 处）")
    print(
        f"最大强连通分量  {report['largest_scc_modules']} 个模块 / "
        f"{report['largest_scc_loc']:,} 行 = 全后端 {report['largest_scc_pct_loc']}%"
    )
    print(
        f"  团内边        {report['scc_inner_edges']} 条，其中只携带 1 个符号的 "
        f"{report['scc_inner_single_symbol_edges']} 条 "
        f"({round(single / len(inner) * 100) if inner else 0}%)，延迟导入 {report['scc_inner_deferred_edges']} 条"
    )
    print(f"exec() 聚合外观 {', '.join(report['exec_facades']) or '无'}")
    print(f"\n最大的 {args.top} 个模块：")
    for row in report["biggest_modules"]:
        print(f"  {row['loc']:>6,} 行  {row['toplevel']:>4} 个顶层定义  {row['module']}")
    print(f"\n出度最高的 {args.top} 个模块：")
    for row in report["highest_out_degree"]:
        print(f"  出 {row['out']:>3}  入 {row['in']:>3}   {row['module']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
