#!/usr/bin/env python
"""静态闸门：CLAUDE.md「Code Architecture Norms」两条结构红线，此前无闸门守着。

两条判据都是棘轮（只降不升，与 `check_write_across_await.py`/
`check_file_conventions.py` 同构，基线存在 `app/STRUCTURE_RATCHETS.toml`）：

1. **app/ 根目录不再新增散文件**——`app/*.py`（不含子包，`Path.glob("*.py")` 本就
   不递归）的文件名集合对比 `[root_files] files = [...]` 白名单快照：出现名单外的
   新文件即失败（提示放进包）；名单里的文件已不在根目录（改名/拆包/删除）也失败
   （提示从基线删除这一行）——体验对齐 `check_write_across_await.py` 的
   「新增违规 / 待收基线」两段式输出。

2. **函数体内 `import app.*` 必须带说明**——AST 扫描 `app/**/*.py`，找函数体内
   （含嵌套的 if/for/try/with 等控制流块，不含模块顶层、不含类体里游离于方法之外
   的语句）对 app 包的导入：
       - `import app` / `import app.foo`（`ast.Import`，任一 alias 名等于 "app"
         或以 "app." 开头）；
       - `from app import x` / `from app.foo import bar`（`ast.ImportFrom`，
         `level == 0` 且 `module` 等于 "app" 或以 "app." 开头）；
       - 相对导入 `from . import x` / `from .x import y`（`level > 0`）——**口径**：
         扫描范围本就限定在 `app/**/*.py`，而相对导入不可能跨出它所在的顶层包
         （Python 运行时会直接报 `ImportError: attempted relative import beyond
         top-level package`），所以只要文件本身在 app 包内，任何相对导入静态
         等价于某个 `app.*` 目标，不需要再解析 `module`/`level` 具体指向哪个
         子模块——只要 `level > 0` 就一定计入。
   「带说明」判据：导入语句首行（`node.lineno`，多行 import 只看首行——本仓延迟
   import 几乎都是单行写法，这是已知的简化，宁可让打在续行/收尾括号那一行的注释
   被判「未说明」多写一行，也不做更复杂的判定）行尾有 `#` 注释，或紧邻上一行是
   整行 `#` 注释（允许再往上还有连续多行注释块，但只需要紧邻的这一行满足）。用
   `tokenize` 而不是裸字符串找 `#`——字符串字面量里的 `#` 不是注释，裸匹配会把
   这类误判成「带说明」。
   不带说明的条数按**文件**记基线（`"app/x.py" = N`）：实测超过基线即失败并报出
   具体行号；未登记的新文件一律按 0 计（新增延迟导入必须当场写注释，CLAUDE.md
   原文）；实测低于基线只提示可收紧，不算失败（与 `check_file_conventions.py`的
   `line_count` 维度同一惯例：棘轮只用「超过即失败」单向判定，「低于基线」不是
   缺陷，不需要每次改动都手工调紧，那是 `--seed-baseline` 之后人工核对的事）。

用法:
    .venv/bin/python scripts/check_structure_ratchets.py               # 检查
    .venv/bin/python scripts/check_structure_ratchets.py --seed-baseline
        # 用当前实测值重写 app/STRUCTURE_RATCHETS.toml。只应在确认这就是「新基线」
        # 时运行——多代理并行开发时在脏工作区播种会把别人的在途改动写进基线，
        # 正确做法是在干净 worktree 里播种，再把生成的文件拷回主工作区。
"""
from __future__ import annotations

import ast
import io
import json
import sys
import tokenize
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
BASELINE_FILE = APP / "STRUCTURE_RATCHETS.toml"

_APP_DOT_PREFIX = "app."


# ---------------------------------------------------------------------------
# 文件收集
# ---------------------------------------------------------------------------


def relpath(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def collect_root_files(app_dir: Path) -> list[Path]:
    """``app/*.py``——只看根目录，不含子包（``glob`` 不递归，与 ``rglob`` 相对）。"""
    return sorted(app_dir.glob("*.py"))


def collect_app_files(app_dir: Path) -> list[Path]:
    """``app/**/*.py``——与 ``check_file_conventions.collect_python_files`` 同一扫描范围。"""
    return sorted(app_dir.rglob("*.py"))


# ---------------------------------------------------------------------------
# 延迟导入扫描：AST 找调用点，tokenize 找注释
# ---------------------------------------------------------------------------


def _is_app_module(name: str) -> bool:
    return name == "app" or name.startswith(_APP_DOT_PREFIX)


class _LazyAppImportVisitor(ast.NodeVisitor):
    """收集函数体内（含嵌套控制流块，不含模块顶层/类体游离语句）对 app 包的导入。

    只在 ``FunctionDef``/``AsyncFunctionDef`` 处加深度；``ClassDef`` 与模块顶层的
    ``if``/``try``/``with`` 等控制流不增加深度——它们的执行时机是模块加载期，
    语义上等价于模块顶层，不是本闸门要管的「函数调用期才执行」的延迟导入。
    """

    def __init__(self) -> None:
        self._depth = 0
        self.hits: list[ast.Import | ast.ImportFrom] = []

    def _enter_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._depth += 1
        self.generic_visit(node)
        self._depth -= 1

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._enter_function(node)

    def visit_Import(self, node: ast.Import) -> None:
        if self._depth > 0 and any(_is_app_module(alias.name) for alias in node.names):
            self.hits.append(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if self._depth <= 0:
            return
        # level > 0 即相对导入：口径见模块 docstring，不必解析 module 指向哪。
        if node.level > 0 or _is_app_module(node.module or ""):
            self.hits.append(node)


_COMMENT_SKIP_TOKENS = {
    tokenize.NEWLINE,
    tokenize.NL,
    tokenize.INDENT,
    tokenize.DEDENT,
    tokenize.ENDMARKER,
    tokenize.ENCODING,
    tokenize.COMMENT,
}


def _comment_line_sets(source: str) -> tuple[set[int], set[int]]:
    """返回 (行尾带注释的行号集合, 整行只有注释的行号集合)，行号从 1 开始。

    用 ``tokenize`` 而不是按字符串找 ``#``：字符串字面量里的 ``#`` 不是注释，
    ``tokenize`` 天然区分，裸字符串匹配会把这类误判成「带说明」。
    """
    trailing: set[int] = set()
    comment_only: set[int] = set()
    code_lines: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                lineno = tok.start[0]
                (trailing if lineno in code_lines else comment_only).add(lineno)
            elif tok.type not in _COMMENT_SKIP_TOKENS:
                code_lines.add(tok.start[0])
    except (tokenize.TokenizeError, SyntaxError, IndentationError):
        pass  # 容错：极端情况下按「无注释」处理，不让整个扫描崩掉
    return trailing, comment_only


def _is_documented(node: ast.Import | ast.ImportFrom, trailing: set[int], comment_only: set[int]) -> bool:
    if node.lineno in trailing:
        return True
    return (node.lineno - 1) in comment_only


def scan_lazy_imports(path: Path) -> list[int] | None:
    """返回一个文件里『函数体内 app 包导入且无说明』的行号列表（升序）。

    语法错误返回 ``None``——交给别的闸门（ruff/compileall）报，这里不重复报错也
    不假装它合格（同 ``check_file_conventions.py`` 的 unparseable 惯例）。
    """
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, SyntaxError, UnicodeError):
        return None
    visitor = _LazyAppImportVisitor()
    visitor.visit(tree)
    if not visitor.hits:
        return []
    trailing, comment_only = _comment_line_sets(source)
    return sorted(node.lineno for node in visitor.hits if not _is_documented(node, trailing, comment_only))


# ---------------------------------------------------------------------------
# 配置加载与校验
# ---------------------------------------------------------------------------


class StructureRatchetsConfigError(Exception):
    """``STRUCTURE_RATCHETS.toml`` 本身配置有误——不可信任，直接拒绝。"""


@dataclass(frozen=True)
class RatchetConfig:
    root_files: frozenset[str]
    lazy_import_baseline: dict[str, int]


def _load_root_files(data: dict, path: Path) -> frozenset[str]:
    section = data.get("root_files", {})
    if not isinstance(section, dict):
        raise StructureRatchetsConfigError(f"{path}: [root_files] 必须是表")
    files = section.get("files", [])
    if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
        raise StructureRatchetsConfigError(f"{path}: root_files.files 必须是字符串数组")
    return frozenset(files)


def _load_lazy_baseline(data: dict, path: Path) -> dict[str, int]:
    section = data.get("lazy_import_undocumented", {})
    if not isinstance(section, dict):
        raise StructureRatchetsConfigError(f"{path}: [lazy_import_undocumented] 必须是表")
    out: dict[str, int] = {}
    for key, value in section.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise StructureRatchetsConfigError(
                f"{path}: lazy_import_undocumented[{key!r}] 必须是非负整数，实得 {value!r}"
            )
        out[key] = value
    return out


def load_config(path: Path) -> RatchetConfig:
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise StructureRatchetsConfigError(f"找不到结构棘轮配置: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise StructureRatchetsConfigError(f"{path} 不是合法 TOML: {exc}") from exc
    return RatchetConfig(
        root_files=_load_root_files(data, path),
        lazy_import_baseline=_load_lazy_baseline(data, path),
    )


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RootFilesReport:
    new: list[str]
    missing: list[str]

    @property
    def is_clean(self) -> bool:
        return not self.new and not self.missing


@dataclass(frozen=True)
class LazyImportViolation:
    path: str
    actual: int
    threshold: int
    lines: list[int]


@dataclass(frozen=True)
class LazyImportReport:
    violations: list[LazyImportViolation]
    below_baseline: list[str]
    unparseable: list[str]

    @property
    def is_clean(self) -> bool:
        return not self.violations


def evaluate_root_files(root_files: list[Path], config: RatchetConfig, root: Path) -> RootFilesReport:
    current = {relpath(p, root) for p in root_files}
    new = sorted(current - config.root_files)
    missing = sorted(config.root_files - current)
    return RootFilesReport(new=new, missing=missing)


def evaluate_lazy_imports(app_files: list[Path], config: RatchetConfig, root: Path) -> LazyImportReport:
    violations: list[LazyImportViolation] = []
    below_baseline: list[str] = []
    unparseable: list[str] = []
    for file_path in app_files:
        rel = relpath(file_path, root)
        hits = scan_lazy_imports(file_path)
        if hits is None:
            unparseable.append(rel)
            continue
        threshold = config.lazy_import_baseline.get(rel, 0)
        actual = len(hits)
        if actual > threshold:
            violations.append(LazyImportViolation(rel, actual, threshold, hits))
        elif rel in config.lazy_import_baseline and actual < threshold:
            below_baseline.append(rel)
    return LazyImportReport(
        violations=violations, below_baseline=sorted(below_baseline), unparseable=sorted(unparseable)
    )


# ---------------------------------------------------------------------------
# 报告输出
# ---------------------------------------------------------------------------


def _print_root_section(report: RootFilesReport, baseline_name: str) -> None:
    for item in report.new:
        print(f"  新增散文件  {item}  → 新模块请放进包（CLAUDE.md：app/ 根目录不再新增散文件）")
    if report.missing:
        joined = ", ".join(report.missing)
        print(f"基线里 {len(report.missing)} 个文件已不在根目录，请从 {baseline_name} 删除：{joined}")


def _print_lazy_section(report: LazyImportReport, baseline_name: str) -> None:
    for v in report.violations:
        lines = ", ".join(str(n) for n in v.lines)
        print(f"  延迟导入无说明超基线  {v.path}  实测 {v.actual}  基线 {v.threshold}  行号 {lines}")
    if report.below_baseline:
        joined = ", ".join(report.below_baseline)
        print(f"可收紧（不计入失败，供人工核对后调低 {baseline_name}）：{joined}")


def print_report(root_report: RootFilesReport, lazy_report: LazyImportReport, baseline_path: Path) -> None:
    name = baseline_path.name
    _print_root_section(root_report, name)
    _print_lazy_section(lazy_report, name)
    if root_report.is_clean and lazy_report.is_clean:
        print(
            f"OK: 结构棘轮 0 条新增（根目录基线 {len(root_report.new) + len(root_report.missing)} "
            "处差异，延迟导入 0 处超基线）"
        )
    else:
        print("FAIL: 结构棘轮出现新增违规，见上")


# ---------------------------------------------------------------------------
# --seed-baseline：重写 STRUCTURE_RATCHETS.toml
# ---------------------------------------------------------------------------

_BASELINE_HEADER = """\
# 结构棘轮基线：CLAUDE.md「Code Architecture Norms」两条结构红线的执行闸门。
# 由 scripts/check_structure_ratchets.py 读取；只能人工调低，不许为了让改动过关
# 而调大——机制与 app/WRITE_ACROSS_AWAIT_BASELINE.txt / app/FILE_CONVENTIONS.toml
# 同构。口径细节见该脚本的模块 docstring。
#
# [root_files]：app/ 根目录（不含子包）允许存在的散文件白名单快照。出现名单外的
# 新文件即失败（提示放进包）；名单里的文件已不在根目录（改名/拆包/删除）也失败
# （提示删除这一行）。
#
# [lazy_import_undocumented]：app/**/*.py 函数体内（含嵌套块，不含模块顶层）的
# `import app.*` / `from app... import ...` / 相对导入（`from . import x`、
# `from .x import y`——在 app 包内静态等价于 app.*，相对导入不可能跨出 app 顶层
# 包）里，没有说明注释（同行行尾 `# ...` 或紧邻上一行整行 `# ...`）的条数，按
# 文件计。某文件实测超过这里记的数字即失败并报行号；未登记的新文件一律按 0 计
# （新增延迟导入必须当场写注释）。
"""


def _render_toml(root_files: list[str], lazy_counts: dict[str, int]) -> str:
    lines = [_BASELINE_HEADER.rstrip("\n"), "", "[root_files]", "files = ["]
    lines += [f"    {json.dumps(p)}," for p in root_files]
    lines += ["]", "", "[lazy_import_undocumented]"]
    lines += [f"{json.dumps(p)} = {lazy_counts[p]}" for p in sorted(lazy_counts)]
    return "\n".join(lines) + "\n"


def _lazy_counts(app_files: list[Path], root: Path) -> tuple[dict[str, int], list[str]]:
    counts: dict[str, int] = {}
    unparseable: list[str] = []
    for path in app_files:
        hits = scan_lazy_imports(path)
        rel = relpath(path, root)
        if hits is None:
            unparseable.append(rel)
        elif hits:
            counts[rel] = len(hits)
    return counts, unparseable


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def run_check(baseline_path: Path = BASELINE_FILE, *, root: Path = ROOT, app_dir: Path = APP) -> int:
    try:
        config = load_config(baseline_path)
    except StructureRatchetsConfigError as exc:
        print(f"STRUCTURE_RATCHETS.toml 配置错误: {exc}", file=sys.stderr)
        return 2
    root_report = evaluate_root_files(collect_root_files(app_dir), config, root)
    lazy_report = evaluate_lazy_imports(collect_app_files(app_dir), config, root)
    print_report(root_report, lazy_report, baseline_path)
    return 0 if (root_report.is_clean and lazy_report.is_clean) else 1


def run_seed(baseline_path: Path = BASELINE_FILE, *, root: Path = ROOT, app_dir: Path = APP) -> int:
    root_rel = sorted(relpath(p, root) for p in collect_root_files(app_dir))
    lazy_counts, unparseable = _lazy_counts(collect_app_files(app_dir), root)
    baseline_path.write_text(_render_toml(root_rel, lazy_counts), encoding="utf-8")
    total = sum(lazy_counts.values())
    print(
        f"已播种基线 → {baseline_path}\n"
        f"  根目录文件 {len(root_rel)} 个\n"
        f"  延迟导入无说明 {total} 条，涉及 {len(lazy_counts)} 个文件"
    )
    if unparseable:
        print(f"  跳过 {len(unparseable)} 个无法解析的文件（交给其它闸门报）：{', '.join(unparseable)}")
    return 0


def main(argv: list[str]) -> int:
    if "--seed-baseline" in argv:
        return run_seed()
    return run_check()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
