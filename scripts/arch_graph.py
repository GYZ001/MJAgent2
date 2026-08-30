#!/usr/bin/env python3
"""后端模块依赖图与强连通分量测量。

`docs/coupling_review_2026-08-29.md` 把「团规模单调下降」定为解耦是否真的发生的
唯一判据，但它依赖的统计脚本没有留在仓库里，判据因此不可运行。本文件把那套测量
固化下来，让每一步解耦之后都能重新取数对比。

判据只从代码事实推导：模块集合来自 `app/**/*.py` 实际存在的文件，依赖边来自 AST
解析出的 import 语句，不维护任何模块白名单。

exec(compile(...)) 聚合外观（app.domain / app.media_exec / app.portraits）把多个
chunk 文件 exec 进同一份 globals()——运行时它们是同一个模块，跨 chunk 调用不产生
import 语句。默认按这个运行时事实把「外观 + 它的 chunk」折叠成一个节点再统计，
否则"把大文件拆成 exec chunk"能直接刷低 SCC 占比而运行时耦合分毫未变。

用法:
    .venv/bin/python scripts/arch_graph.py            # 摘要（默认折叠口径）
    .venv/bin/python scripts/arch_graph.py --json     # 机器可读，用于前后对比
    .venv/bin/python scripts/arch_graph.py --top 20   # 调整榜单长度
    .venv/bin/python scripts/arch_graph.py --no-collapse
        # 不折叠，按原始逐文件视图输出，仅用于对照真实的折叠效果。

    # 分层防回潮闸门（docs/architecture_layering_plan_2026-08-29.md 2.2 节）：
    .venv/bin/python scripts/arch_graph.py --check-layers
        # 读 app/LAYERS.toml，报出所有「上行边」（低层 import 高层）。不带
        # --max-violations 时是严格模式：只要有一条上行边就非零退出。
    .venv/bin/python scripts/arch_graph.py --check-layers --max-violations 42
        # 报告模式：违规数 <= 42 就算通过，只有超过阈值才非零退出。
        # scripts/verify.py --full 用这个模式，阈值取自 LAYERS.toml 的
        # max_violations（红线只降不升）。
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import json
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
DEFAULT_LAYERS_FILE = APP / "LAYERS.toml"

# 未声明层号的模块按 fail-safe 规则视为最高层——不会假装它合法，也不会被
# 空集合短路跳过（CLAUDE.md「空集合不等于无需检查」）。
UNDECLARED_LAYER = 5


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


def _string_list_literal(node: ast.expr) -> list[str] | None:
    """`node` 是不是一个纯字符串常量的 Tuple/List？是则按序返回其值，否则 None。"""
    if not isinstance(node, (ast.Tuple, ast.List)):
        return None
    values: list[str] = []
    for elt in node.elts:
        if not (isinstance(elt, ast.Constant) and isinstance(elt.value, str)):
            return None
        values.append(elt.value)
    return values


def _calls_exec_compile(node: ast.AST) -> bool:
    """`node` 子树里是否存在形如 `exec(compile(...))` 的调用。"""
    return any(
        isinstance(inner, ast.Call)
        and isinstance(inner.func, ast.Name)
        and inner.func.id == "exec"
        and inner.args
        and isinstance(inner.args[0], ast.Call)
        and isinstance(inner.args[0].func, ast.Name)
        and inner.args[0].func.id == "compile"
        for inner in ast.walk(node)
    )


def parse_exec_chunk_files(path: Path) -> list[str]:
    """从 exec 外观文件里解析出它实际 exec 了哪些相对路径的源文件。

    外观文件的写法固定为「先把一组文件名字符串赋给某个模块级名字，再用
    `for x in <那个名字>: exec(compile(...))` 遍历执行」——例如
    `app/domain/__init__.py` 的 `_DOMAIN_MODULES`、`app/media_exec/__init__.py`
    的 `_MEDIA_MODULES`、`app/portraits/__init__.py` 的 `_PORTRAIT_CHUNKS`。三处
    变量名互不相同，所以这里不认变量名也不认包名，只认这个 AST 形状：模块级的
    字符串列表/元组赋值 + 以它为 `iter`、循环体内调用 `exec(compile(...))` 的
    `for` 循环。解不出这个形状就返回空列表（调用方据此认定该文件不是聚合外观，
    即便它文本里恰好出现了 "exec(compile("）。
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    except (SyntaxError, OSError):
        return []

    string_list_assigns: dict[str, list[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            values = _string_list_literal(node.value)
            if values:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        string_list_assigns[target.id] = values

    chunk_files: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.For)
            and isinstance(node.iter, ast.Name)
            and node.iter.id in string_list_assigns
            and _calls_exec_compile(node)
        ):
            chunk_files.extend(string_list_assigns[node.iter.id])
    return chunk_files


def exec_facade_chunk_map(modules: dict[str, Path]) -> dict[str, list[str]]:
    """外观模块名 -> 它折叠吸收的 chunk 模块名列表（按 exec 顺序，只含已知模块）。

    只对 `find_exec_facades` 挑出的候选文件做 AST 解析；解析不出 chunk 列表的
    （文本里恰好出现 "exec(compile(" 但不是这个模式，或列出的文件当前不存在）
    不进结果，调用方按未折叠处理——不静默假装折叠了不存在的东西。
    """
    result: dict[str, list[str]] = {}
    for name in find_exec_facades(modules):
        path = modules[name]
        chunk_files = parse_exec_chunk_files(path)
        if not chunk_files:
            continue
        facade_dir = path.parent
        chunk_names = []
        for rel in chunk_files:
            candidate = module_name(facade_dir / rel)
            if candidate in modules and candidate != name:
                chunk_names.append(candidate)
        if chunk_names:
            result[name] = chunk_names
    return result


def build_graph(
    modules: dict[str, Path], names: set[str]
) -> tuple[dict[str, set[str]], list[tuple[str, str, str, bool]], dict[str, dict]]:
    """解析全部模块的 import 边，返回 (邻接表, 边明细, 每模块的行数/顶层定义数)。"""
    graph: dict[str, set[str]] = {n: set() for n in names}
    edge_rows: list[tuple[str, str, str, bool]] = []
    stats: dict[str, dict] = {}
    for name, path in modules.items():
        edges, toplevel, loc = parse_edges(name, path, names)
        stats[name] = {"loc": loc, "toplevel": toplevel}
        for target, symbol, lazy in edges:
            graph[name].add(target)
            edge_rows.append((name, target, symbol, lazy))
    return graph, edge_rows, stats


def collapse_exec_facades(
    graph: dict[str, set[str]],
    edge_rows: list[tuple[str, str, str, bool]],
    stats: dict[str, dict],
    chunk_map: dict[str, list[str]],
) -> tuple[dict[str, set[str]], list[tuple[str, str, str, bool]], dict[str, dict]]:
    """把「外观 + 它 exec 的所有 chunk」合并成一个逻辑节点。

    这些 chunk 在运行时 exec 进同一份 `globals()`，跨 chunk 调用是裸名解析，不
    产生任何 import 语句——AST 依赖图如果继续把它们当独立节点，"把大文件拆成
    exec chunk" 就能直接刷低 SCC 占比，而运行时耦合分毫未变。折叠让指标只看
    真实的模块边界：行数相加、边取并集、内部边（折叠后 source == target）丢弃。
    """
    # 同一个 chunk 理论上可能被两个不同外观各自 exec 一份（历史遗留的重复执行反模式，
    # 例如旧版 app/api.py 和 app/domain/__init__.py 曾各自把 app/domain/*.py exec 进
    # 自己的 globals()，见 app/domain/__init__.py 顶部文档字符串）。这种情况下单个
    # chunk 会同时出现在两个外观的吸收列表里，所有权本身就是模糊的；这里用一次
    # 确定性归属（字典推导式后写者赢）打破歧义，但归属只解出一次、全程复用，
    # 不按每个外观的原始 chunk 列表重新求行数——否则同一份源码的行数会被两个外观
    # 各计一次，把 total_loc 算大。
    chunk_to_facade = {chunk: facade for facade, chunks in chunk_map.items() for chunk in chunks}

    def canon(name: str) -> str:
        return chunk_to_facade.get(name, name)

    collapsed_names = {canon(n) for n in graph}
    collapsed_graph: dict[str, set[str]] = {n: set() for n in collapsed_names}
    collapsed_edge_rows: list[tuple[str, str, str, bool]] = []
    for src, dst, sym, lazy in edge_rows:
        c_src, c_dst = canon(src), canon(dst)
        if c_src == c_dst:
            continue
        collapsed_graph[c_src].add(c_dst)
        collapsed_edge_rows.append((c_src, c_dst, sym, lazy))

    # 按 canon() 把每个真实文件的行数/顶层定义数记一次账，落进它的归属节点——
    # 这样无论所有权是否有歧义，每一行源码在总账里都恰好出现一次。
    collapsed_stats: dict[str, dict] = {n: {"loc": 0, "toplevel": 0} for n in collapsed_names}
    for original, s in stats.items():
        target = canon(original)
        collapsed_stats[target]["loc"] += s["loc"]
        collapsed_stats[target]["toplevel"] += s["toplevel"]

    return collapsed_graph, collapsed_edge_rows, collapsed_stats


class LayersConfigError(Exception):
    """`LAYERS.toml` 本身有配置错误（不是违规，是工具不能信任这份数据）。"""


@dataclass(frozen=True)
class LayerException:
    source: str
    target: str
    reason: str
    expires: dt.date


@dataclass(frozen=True)
class LayersConfig:
    layers: dict[str, int]
    max_violations: int
    exceptions: list[LayerException] = field(default_factory=list)


def load_layers_config(path: Path) -> LayersConfig:
    """读取并校验 `LAYERS.toml`。配置本身错了就报错，不静默接受。"""
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise LayersConfigError(f"找不到分层声明文件: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise LayersConfigError(f"{path} 不是合法 TOML: {exc}") from exc

    raw_layers = data.get("layers", {})
    if not isinstance(raw_layers, dict):
        raise LayersConfigError(f"{path}: [layers] 必须是表")
    layers: dict[str, int] = {}
    for key, value in raw_layers.items():
        if not isinstance(value, int) or isinstance(value, bool) or not (0 <= value <= 5):
            raise LayersConfigError(
                f"{path}: 模块 {key!r} 的层号必须是 0-5 之间的整数，实得 {value!r}"
            )
        layers[key] = value

    max_violations = data.get("max_violations")
    if not isinstance(max_violations, int) or isinstance(max_violations, bool) or max_violations < 0:
        raise LayersConfigError(f"{path}: max_violations 必须是非负整数")

    exceptions: list[LayerException] = []
    for i, entry in enumerate(data.get("allowed_exceptions", [])):
        if not isinstance(entry, dict):
            raise LayersConfigError(f"{path}: allowed_exceptions[{i}] 必须是表")
        missing = [k for k in ("from", "to", "reason", "expires") if not entry.get(k)]
        if missing:
            raise LayersConfigError(
                f"{path}: allowed_exceptions[{i}] 缺少字段 {missing}"
                "（豁免必须带 reason 和 expires，没有理由或失效日期的豁免一律拒绝）"
            )
        try:
            expires = dt.date.fromisoformat(str(entry["expires"]))
        except ValueError as exc:
            raise LayersConfigError(
                f"{path}: allowed_exceptions[{i}].expires 不是合法日期 (YYYY-MM-DD): "
                f"{entry['expires']!r}"
            ) from exc
        exceptions.append(
            LayerException(
                source=str(entry["from"]),
                target=str(entry["to"]),
                reason=str(entry["reason"]),
                expires=expires,
            )
        )

    return LayersConfig(layers=layers, max_violations=max_violations, exceptions=exceptions)


def resolve_layer(name: str, layers: dict[str, int]) -> tuple[int, str | None]:
    """最长前缀匹配：具体模块声明优先于包前缀声明；都没有就是未声明（fail-safe L5）。

    前缀按 `.` 切分逐段回退，不做裸字符串前缀匹配——`app.narrative_blueprint`
    不会被 `app.narrative` 的声明误吃掉。
    """
    parts = name.split(".")
    for i in range(len(parts), 0, -1):
        candidate = ".".join(parts[:i])
        if candidate in layers:
            return layers[candidate], candidate
    return UNDECLARED_LAYER, None


@dataclass(frozen=True)
class LayerViolation:
    source: str
    source_layer: int
    target: str
    target_layer: int
    symbol: str
    lazy: bool


def find_layer_violations(
    modules: dict[str, Path],
    config: LayersConfig,
    *,
    today: dt.date | None = None,
    chunk_map: dict[str, list[str]] | None = None,
) -> tuple[list[LayerViolation], list[str], list[LayerException]]:
    """返回 (违规列表, 未声明层号的模块清单, 已过期的豁免清单)。

    违规判据完全来自数据：AST 解析出的真实 import 边 + LAYERS.toml 声明的层号，
    不维护任何「已知违规模块」名单。

    `chunk_map`：exec 聚合外观模块名 -> 它吸收的 chunk 模块名列表。默认 `None`
    时按 `exec_facade_chunk_map(modules)` 自动探测（真实场景下的默认行为）；传
    `{}` 关闭折叠，按原始逐文件视图判层（对应 CLI 的 `--no-collapse`）。折叠后，
    chunk 在运行时和它的外观是同一个模块，判层时统一取外观的层号——不管
    LAYERS.toml 里是否为某个 chunk 单独声明了不同的层号，因为那份声明在运行时
    没有意义可言；折叠后的内部边（source 和 target 归一到同一个外观）也不再算
    依赖，不参与判违规。
    """
    today = today or dt.date.today()
    names = set(modules)

    if chunk_map is None:
        chunk_map = exec_facade_chunk_map(modules)
    chunk_to_facade = {chunk: facade for facade, chunks in chunk_map.items() for chunk in chunks}

    def canon(name: str) -> str:
        return chunk_to_facade.get(name, name)

    undeclared = sorted({canon(m) for m in names if resolve_layer(canon(m), config.layers)[1] is None})

    active_pairs: set[tuple[str, str]] = set()
    expired: list[LayerException] = []
    for exc in config.exceptions:
        if exc.expires >= today:
            active_pairs.add((exc.source, exc.target))
        else:
            expired.append(exc)

    violations: list[LayerViolation] = []
    for name, path in modules.items():
        edges, _toplevel, _loc = parse_edges(name, path, names)
        source_name = canon(name)
        for target, symbol, lazy in edges:
            target_name = canon(target)
            if target_name == source_name:
                continue  # 折叠后的内部边：运行时是同一个模块，裸名解析，不是依赖
            source_layer, _ = resolve_layer(source_name, config.layers)
            target_layer, _ = resolve_layer(target_name, config.layers)
            if source_layer >= target_layer:
                continue
            if (source_name, target_name) in active_pairs:
                continue
            violations.append(
                LayerViolation(source_name, source_layer, target_name, target_layer, symbol, lazy)
            )
    return violations, undeclared, expired


def run_check_layers(
    layers_file: Path, max_violations: int | None, top: int, *, collapse: bool = True
) -> int:
    try:
        config = load_layers_config(layers_file)
    except LayersConfigError as exc:
        print(f"LAYERS.toml 配置错误: {exc}", file=sys.stderr)
        return 2

    modules = collect_modules()
    chunk_map = exec_facade_chunk_map(modules) if collapse else {}
    if chunk_map:
        absorbed = sum(len(v) for v in chunk_map.values())
        print(
            f"exec 外观折叠：{len(chunk_map)} 个外观吸收 {absorbed} 个 chunk"
            "（运行时同一命名空间，chunk 层号继承外观）："
        )
        for facade in sorted(chunk_map):
            print(f"  {facade} 吸收 {len(chunk_map[facade])} 个 chunk")
        print()

    violations, undeclared, expired = find_layer_violations(modules, config, chunk_map=chunk_map)
    module_level = [v for v in violations if not v.lazy]
    deferred = [v for v in violations if v.lazy]
    total = len(violations)

    print(
        f"层级违规（上行边）共 {total} 条：模块级 {len(module_level)} 条，"
        f"函数内延迟导入 {len(deferred)} 条"
    )

    if expired:
        print(f"\n过期豁免 {len(expired)} 条（已按违规计入上方总数，不再豁免）：")
        for exc in sorted(expired, key=lambda e: (e.source, e.target)):
            print(f"  {exc.source} -> {exc.target}  (expires {exc.expires}, reason: {exc.reason})")

    if undeclared:
        print(f"\n未声明层号的模块 {len(undeclared)} 个（按 fail-safe 规则视为 L5，需要补声明）：")
        shown = undeclared[:top]
        for name in shown:
            print(f"  {name}")
        if len(undeclared) > len(shown):
            print(f"  ...另有 {len(undeclared) - len(shown)} 个未列出（用 --top 调整显示数量）")

    ranked = sorted(
        violations,
        key=lambda v: (v.target_layer - v.source_layer, v.source, v.target, v.symbol),
        reverse=True,
    )
    print(f"\nTop {min(top, len(ranked))} 上行边（按跨层数排序）：")
    for v in ranked[:top]:
        tag = "延迟" if v.lazy else "模块级"
        print(f"  [{tag}] {v.source}(L{v.source_layer}) -> {v.target}(L{v.target_layer}) :: {v.symbol}")

    threshold = 0 if max_violations is None else max_violations
    if total > threshold:
        print(f"\nFAIL: 违规数 {total} 超过阈值 {threshold}")
        return 1
    print(f"\nOK: 违规数 {total}，阈值 {threshold}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="输出 JSON 便于前后对比")
    parser.add_argument("--top", type=int, default=12, help="榜单长度")
    parser.add_argument(
        "--no-collapse",
        action="store_true",
        help=(
            "不折叠 exec 聚合外观（app.domain / app.media_exec / app.portraits 等），"
            "按原始逐文件视图输出，仅用于对照。默认按运行时命名空间折叠——那些包的"
            "chunk 文件是 exec() 进同一份 globals() 的同一个模块，不折叠会被"
            "『拆文件』直接刷低 SCC 占比而运行时耦合分毫未变。"
        ),
    )
    parser.add_argument(
        "--check-layers",
        action="store_true",
        help="按 app/LAYERS.toml 检查分层上行边，取代默认摘要输出",
    )
    parser.add_argument(
        "--layers-file",
        type=Path,
        default=DEFAULT_LAYERS_FILE,
        help="分层声明文件路径（默认 app/LAYERS.toml）",
    )
    parser.add_argument(
        "--max-violations",
        type=int,
        default=None,
        help="报告模式阈值：违规数超过它才非零退出。不传则严格模式（任何违规都非零退出）。",
    )
    args = parser.parse_args()
    collapse = not args.no_collapse

    if args.check_layers:
        return run_check_layers(args.layers_file, args.max_violations, args.top, collapse=collapse)

    modules = collect_modules()
    names = set(modules)

    graph, edge_rows, stats = build_graph(modules, names)

    # exec 聚合外观折叠：chunk_map 的探测本身与 --no-collapse 无关（它只描述代码
    # 事实），是否把它套用到图上才受这个开关控制。
    chunk_map = exec_facade_chunk_map(modules)
    if collapse and chunk_map:
        graph, edge_rows, stats = collapse_exec_facades(graph, edge_rows, stats, chunk_map)
    names = set(graph)

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
        "collapsed": collapse,
        "collapsed_packages": {
            facade: {"absorbed_chunks": chunks, "num_absorbed": len(chunks)}
            for facade, chunks in chunk_map.items()
        },
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

    view = "折叠口径：exec 外观 = 单节点" if collapse else "未折叠口径：逐文件视图（--no-collapse）"
    print(f"[{view}]")
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
    if report["collapsed_packages"]:
        note = "已折叠进上面的图" if collapse else "未折叠（--no-collapse），仍是独立节点"
        print(f"  折叠明细（{note}）：")
        for facade, info in report["collapsed_packages"].items():
            print(f"    {facade} 吸收 {info['num_absorbed']} 个 chunk: {', '.join(info['absorbed_chunks'])}")
    print(f"\n最大的 {args.top} 个模块：")
    for row in report["biggest_modules"]:
        print(f"  {row['loc']:>6,} 行  {row['toplevel']:>4} 个顶层定义  {row['module']}")
    print(f"\n出度最高的 {args.top} 个模块：")
    for row in report["highest_out_degree"]:
        print(f"  出 {row['out']:>3}  入 {row['in']:>3}   {row['module']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
