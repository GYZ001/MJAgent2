"""按 import/再导出关系把调用点解析成模块限定名，并识别「以无条件 commit 收尾」的
委托提交函数——供 ``scripts/check_write_across_await.py`` 的写事务跨 await 判据复用。

拆成独立文件单纯是体量原因（原单文件超过 CLAUDE.md 的 500 行上限）；单向依赖，本
模块不 import 检查器主脚本。``ROOT`` 不在本模块持有全局状态——``_module_dotted``/
``_ResolutionContext`` 都要求调用方显式传入当前 ``root``，现场解析，避免测试
monkeypatch 主脚本 ``check_write_across_await.ROOT`` 时本模块持有一份不会同步更新
的私有副本（CLAUDE.md「global 重绑定的名字必须与它的写入者同模块」同一类地雷：
两个模块各留一份 ``ROOT`` 全局变量，patch 一边、另一边读到的还是旧值，且不报错）。

三刀踩坑记录（2026-09-23）：

1. 调用点解析先前只按裸函数名做传递闭包——全仓 8 个同名 ``ensure_tables_on_
   connection``（每包一份建表兜底）、25 个同名 ``operation``（写事务回调闭包的
   通用命名）这类符合 Python 习惯但在全仓层面撞名的写法，只要其中一个真的
   「写了不提交」，裸名匹配会把全仓所有同名调用点一并牵连——曾一次性制造 48 条
   新违规，44 条可追溯到唯一真正泄漏的 ``ensure_tables_on_connection``，另 3 条
   追溯到把参数名撞了 ``operation`` 的两个不相干的重试/并发包装函数。现在调用点
   先尝试按 import 关系解析出模块限定名（``from x import f``/``import x`` 后的
   ``x.f``/同模块内直接调用，含形参遮蔽判定），解析出确凿结果按模块限定名精确
   匹配；解析不了（``self.foo()`` 这类接收者类型未知的调用、``import pkg.sub``
   不带别名的多段导入等）保持裸名匹配兜底，不放过任何真隐患。

2. 模块限定名解析到的目标模块若顶层没有这个函数（本仓大量符号经
   ``app/domain/__init__.py``/``app/worker.py`` 这类门面用 ``from .sub import f
   as f`` 转手再导出，定义其实在子模块），第一版会直接判「解析到、但
   qualified_leaky 里没有这一条」=> 判不泄漏，导致经门面调用真泄漏写者的 async
   函数被漏报。``_follow_reexport`` 沿目标模块自己的 import 绑定继续追（最多
   5 跳，防环），追不到、成环、或落到本仓扫描范围外的模块，一律退回 ``None``
   （裸名匹配兜底），绝不当作「确认不泄漏」。

3. 检查器只认「同一个函数体里自己调用 commit/rollback」为关闭，看不见「写完委托
   给另一个以 commit 收尾的函数」这种常见分工（``app/media_exec/job_state.py::
   _release_rejected_continuity_anchor`` 两条 UPDATE 后调用
   ``release_provider_poll``，后者顶层最后一条语句就是 ``conn.commit()``）——
   这类函数被错判成「泄漏写者」，牵连调用方。``_is_trailing_commit`` 只认函数体
   **顶层最后一条语句**无条件执行 ``<x>.commit()``（分支/循环/try 内部的 commit
   不算——不保证执行到）；调用点解析到这类函数（同样走上面两条的 import/再导出
   解析）视为关闭，解析不了的调用不当作关闭——「谁提交了」必须证据确凿，与
   「谁泄漏了」对「猜错方向」的容忍度不对称。
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple


class ImportBinding(NamedTuple):
    """一个名字在某处 import 得到的绑定。

    ``from <module> import <orig_name> [as x]``：module=<module>，orig_name=<orig_name>。
    ``import <module> [as x]``：module=<module>，orig_name=None（x 绑定的是模块本身，
    不是某个函数——裸调用 ``x()`` 因此不可能是函数引用；``x.f()`` 里 f 才是函数）。
    """

    module: str
    orig_name: str | None


class _NotAFunction:
    """``_resolve_*`` 返回它表示「确认这个引用不是函数」（比如裸模块名）——与「解析
    不了」（返回 None，调用方退回裸名匹配兜底）是两回事：这里可以放心判「不匹配」。
    """


_NOT_A_FUNCTION = _NotAFunction()


def _module_dotted(path: Path, root: Path) -> str:
    """``app/domain/common.py`` -> ``app.domain.common``；
    ``app/domain/__init__.py`` -> ``app.domain``（包本身的限定名）。"""
    parts = path.relative_to(root).with_suffix("").parts
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _relative_base(current_dotted: str, is_init: bool, level: int) -> str:
    """相对导入（``from . import x`` 等）的包前缀；level 是前导点数。"""
    if is_init:
        pkg = current_dotted
    else:
        pkg = current_dotted.rsplit(".", 1)[0] if "." in current_dotted else ""
    parts = pkg.split(".") if pkg else []
    strip = level - 1
    if strip > 0:
        parts = parts[: len(parts) - strip] if strip <= len(parts) else []
    return ".".join(parts)


def _importfrom_target_module(node: ast.ImportFrom, current_dotted: str, is_init: bool) -> str:
    if node.level:
        base = _relative_base(current_dotted, is_init, node.level)
        return f"{base}.{node.module}" if node.module else base
    return node.module or ""


class _ImportCollector(ast.NodeVisitor):
    """收集一段语句（函数体或模块顶层）里直接可见的 import 绑定；不进嵌套函数/类/
    lambda——那些有自己的作用域，这里收集的绑定对它们不可见。会进 if/try/while 等
    控制流块，覆盖条件导入、函数体内延迟导入（全仓 1,405 处之一）。"""

    def __init__(self, current_dotted: str, is_init: bool) -> None:
        self.current_dotted = current_dotted
        self.is_init = is_init
        self.bindings: dict[str, ImportBinding] = {}

    def generic_visit(self, node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            return
        if isinstance(node, ast.ImportFrom):
            self._add_from(node)
        elif isinstance(node, ast.Import):
            self._add_import(node)
        super().generic_visit(node)

    def _add_from(self, node: ast.ImportFrom) -> None:
        target = _importfrom_target_module(node, self.current_dotted, self.is_init)
        for alias in node.names:
            if alias.name == "*":
                continue  # 全仓禁止 from x import *（CLAUDE.md）；出现了也解析不了，跳过
            local = alias.asname or alias.name
            self.bindings[local] = ImportBinding(target, alias.name)

    def _add_import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.asname:
                self.bindings[alias.asname] = ImportBinding(alias.name, None)
            elif "." not in alias.name:
                self.bindings.setdefault(alias.name, ImportBinding(alias.name, None))
            # 多段且无别名（如 import a.b.c）绑定的是顶层包 a，.b.c 具体归属解析不了
            # ——不登记，调用点落回保守兜底，不伪造一个可能错的解析。


def _collect_imports(stmts: list[ast.stmt], current_dotted: str, is_init: bool) -> dict[str, ImportBinding]:
    collector = _ImportCollector(current_dotted, is_init)
    for stmt in stmts:
        collector.visit(stmt)
    return collector.bindings


def _toplevel_func_names(tree: ast.AST) -> set[str]:
    return {n.name for n in getattr(tree, "body", []) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _param_names(args: ast.arguments) -> set[str]:
    names = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    return names


@dataclass(frozen=True)
class _Scope:
    """解析一个函数体里的调用点所需的全部本地上下文。"""

    params: set[str]
    imports: dict[str, ImportBinding]
    current_path: Path
    toplevel_funcs: dict[Path, set[str]]
    path_by_dotted: dict[str, Path]
    module_imports: dict[Path, dict[str, ImportBinding]]


def _follow_reexport(
    target: Path, name: str, scope: _Scope, *, hops: int = 5,
    _visited: frozenset[tuple[Path, str]] = frozenset(),
) -> tuple[Path, str] | None:
    """``target`` 顶层没有直接定义 ``name`` 时，沿 ``target`` 自己的模块级 import
    绑定追一层「再导出」（``app/domain/__init__.py`` 这类门面的
    ``from .sub import f as f``、``app/worker.py`` 式facade），最多追 ``hops`` 跳。

    追不到定义、撞上再导出环、或某一跳落到本仓扫描范围外的模块——一律返回
    ``None``（退回裸名匹配兜底），**绝不返回「确认不泄漏」**：这里解析不完整，
    不代表目标不是泄漏写者，函数体级判定错误会漏放真隐患，比过度保守危险得多。
    """
    if name in scope.toplevel_funcs.get(target, ()):
        return (target, name)
    if hops <= 0 or (target, name) in _visited:
        return None
    reexport = scope.module_imports.get(target, {}).get(name)
    if reexport is None or reexport.orig_name is None:
        return None
    next_target = scope.path_by_dotted.get(reexport.module)
    if next_target is None:
        return None
    return _follow_reexport(
        next_target, reexport.orig_name, scope,
        hops=hops - 1, _visited=_visited | {(target, name)},
    )


def _resolve_bare_name(name: str, scope: _Scope) -> tuple[Path, str] | _NotAFunction | None:
    """裸调用 ``f(...)`` 解析到定义它的 (模块路径, 函数名)；``None`` 表示解析不了。"""
    if name in scope.params:
        return _NOT_A_FUNCTION  # 形参遮蔽：本函数里这个名字绑定的是参数，不是全局函数
    binding = scope.imports.get(name)
    if binding is not None:
        if binding.orig_name is None:
            return _NOT_A_FUNCTION  # `import x as name`：name 绑定模块对象，裸调用不是函数引用
        target = scope.path_by_dotted.get(binding.module)
        if target is None:
            return _NOT_A_FUNCTION  # 源模块不在本仓（标准库/三方包），不可能是我们跟踪的泄漏写者
        return _follow_reexport(target, binding.orig_name, scope)
    if name in scope.toplevel_funcs.get(scope.current_path, ()):
        return (scope.current_path, name)
    return None


def _resolve_attr_call(receiver: str, attr: str, scope: _Scope) -> tuple[Path, str] | _NotAFunction | None:
    """``x.f(...)`` 解析到定义 f 的 (模块路径, 函数名)；``None`` 表示解析不了。"""
    if receiver in scope.params:
        return None  # 形参/局部对象的方法调用，类型未知，退保守（不是「确认不匹配」）
    binding = scope.imports.get(receiver)
    if binding is None:
        return None
    if binding.orig_name is None:
        # `import x.y.z as receiver`：receiver 确定绑定该模块本身。
        target = scope.path_by_dotted.get(binding.module)
        if target is None:
            return _NOT_A_FUNCTION
        return _follow_reexport(target, attr, scope)
    # `from pkg import receiver`：receiver 可能是子模块，也可能是函数/类实例返回值——
    # 只有 pkg.receiver 确实是本仓一个模块文件时才当子模块解析，否则退保守。
    submodule = f"{binding.module}.{binding.orig_name}"
    target = scope.path_by_dotted.get(submodule)
    return _follow_reexport(target, attr, scope) if target is not None else None


def _resolve_call(node: ast.Call, scope: _Scope) -> tuple[Path, str] | _NotAFunction | None:
    if isinstance(node.func, ast.Name):
        return _resolve_bare_name(node.func.id, scope)
    if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
        return _resolve_attr_call(node.func.value.id, node.func.attr, scope)
    return None  # 更深的属性链（a.b.c(...)）、调用表达式的返回值等，解析不了


def _is_trailing_commit(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """函数体**顶层最后一条语句**是否无条件执行 ``<x>.commit()``——分支/循环/try
    内部的 commit 不算数（不保证执行到），只认「函数做的最后一件事就是提交」这种
    最明确的委托收尾形态。宁可保守漏识别（真正委托提交但写法更绕的函数仍会被当
    「泄漏写者」），也不能把只在某个分支提交的函数误判成「一定关闭」。"""
    if not fn.body:
        return False
    last = fn.body[-1]
    return (
        isinstance(last, ast.Expr)
        and isinstance(last.value, ast.Call)
        and isinstance(last.value.func, ast.Attribute)
        and last.value.func.attr == "commit"
    )


def _functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


class _ResolutionContext:
    """把「调用点按 import 关系解析成模块限定名」需要的全仓静态信息算一遍、缓存住，
    供 ``_leaky_writers`` 的定点迭代与 ``scan_tree`` 的终扫复用（否则每个函数都要
    重新扫一遍全仓 import，代价是 O(函数数 × 模块数)）。

    ``root`` 由调用方显式传入并原样存住（不读任何模块级全局）：测试 monkeypatch
    主脚本的 ``ROOT`` 后新建的 ``_ResolutionContext`` 会自然用上新值，不存在「两边
    分别持有一份、只改了一边」的漂移可能。
    """

    def __init__(self, trees: dict[Path, ast.AST], root: Path) -> None:
        self.root = root
        self.path_by_dotted = {_module_dotted(p, root): p for p in trees}
        self.toplevel_funcs = {p: _toplevel_func_names(t) for p, t in trees.items()}
        self._module_imports = {
            p: _collect_imports(t.body, _module_dotted(p, root), p.name == "__init__.py")
            for p, t in trees.items()
        }
        self.qualified_committers = {
            (path, fn.name)
            for path, tree in trees.items()
            for fn in _functions(tree)
            if _is_trailing_commit(fn)
        }
        self._scope_cache: dict[int, _Scope] = {}

    def scope_for(self, path: Path, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> _Scope:
        """按 ``id(fn)`` 缓存：``_leaky_writers`` 的 3 轮定点迭代 + ``scan_tree`` 终扫
        会对同一个函数反复求 scope，而它只取决于不变的 AST，不必每次重新扫一遍
        函数体收集形参/延迟 import（全仓量级下这一项此前是主要耗时）。"""
        cached = self._scope_cache.get(id(fn))
        if cached is not None:
            return cached
        dotted = _module_dotted(path, self.root)
        is_init = path.name == "__init__.py"
        local_imports = _collect_imports(fn.body, dotted, is_init)
        merged = {**self._module_imports[path], **local_imports}
        scope = _Scope(
            params=_param_names(fn.args), imports=merged, current_path=path,
            toplevel_funcs=self.toplevel_funcs, path_by_dotted=self.path_by_dotted,
            module_imports=self._module_imports,
        )
        self._scope_cache[id(fn)] = scope
        return scope
