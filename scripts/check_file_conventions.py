#!/usr/bin/env python3
"""单文件形状闸门：文件行数、最长单函数、模块 docstring、星号导入。

`scripts/arch_graph.py --check-layers` 管「谁能依赖谁」（跨模块）；本文件管「单个
文件长什么样才算合格」（文件内部）。两者互不重叠，判据都从代码事实（AST/行数）
推导，不维护任何模块白名单。

现状（`docs/architecture_layering_plan_2026-08-29.md`）：`app/stages.py` 12,142
行焊了 7 个关注点，`app/portraits.py` 10,821 行——这类巨型文件会被专门的重构工作
拆分，但拆完之后如果没有闸门，一定会重新长回去。本工具就是那个防回潮的棘轮。

不检查「顶层定义数」（曾经的 `toplevel_defs` 维度，2026-08-29 移除）：对全仓库
258 个 `app/**/*.py` 实测，`lines` 与顶层定义数的相关系数达 0.895——高定义数
几乎总是与高行数同时出现，`line_count` 维度已经在管这件事。唯一一个「行数、最长
函数都不超默认值，只有定义数超默认值 30」的文件是 `app/capabilities/inputs.py`
（47 个几行长的小 Pydantic 模型，340 行）——这是教科书级的良好代码，不是
god-module。而真正的反例更严重：把一个 3,585 行的巨型函数拆成 236 行编排器 +
约 45 个具名 helper 是教科书级的正确重构，`function_lines` 大幅改善（3,585 →
322），但顶层定义数从 55 涨到 82，正好撞上这道本该抓 god-module 的闸门，逼着
基线从 55 上调到 95——即闸门在奖励「焊一个巨型函数」、惩罚「拆成许多具名
helper」。真正的 god-module 信号（单个函数/方法过大）已经被 `function_lines`
完整覆盖（它用 `ast.walk` 遍历全树，含类方法与嵌套函数，不是只看顶层）；「文件
整体过大」已经被 `line_count` 完整覆盖。`toplevel_defs` 在这两者之外没有提供任何
独有的真阳性，只贡献了假阳性和一次被迫违背棘轮原则的基线上调，因此整个维度被
删除，而不是调参或加豁免。

棘轮机制（与 `LAYERS.toml`/`--check-layers` 同构）：
    - `[defaults]` 是对「新文件」生效的紧阈值（写新代码时应该达到的标准）。
    - `[baseline.*]` 记录每个已超标文件当时的实测行数/定义数（外加缓冲），是该
      文件此刻允许的上限。**判据是「不得比基线更差」，不是「必须低于 defaults」**——
      基线只能人工调低（文件瘦身之后），不得调高。
    - 不在任何 baseline 表里的文件一律按 `[defaults]` 的紧阈值检查，不会被静默
      放过（CLAUDE.md「空集合不等于无需检查」）。
    - 二元维度（docstring 是否存在、是否有星号导入）用同样性质的棘轮：
      `[baseline.docstring_exempt]` / `[baseline.star_import_exempt]` 各是一份
      「引入本闸门那一刻已存在的既有情况」快照，不是业务判据白名单——例如
      `app/media_exec/*.py` 里的 `from ...common import *` 是文档化的 exec()
      外观遗留写法（CLAUDE.md「Retiring Features」），闸门要能看见它、但不因为
      它现在还在用就把所有并行工作拦死；新文件、新出现的星号导入不受此豁免。
    - `[[exemptions]]` 是另一套机制：整个豁免某个文件的某个维度检查（例如生成
      代码），必须带 `reason` + `expires`（日期），过期即按违规计入，缺字段的
      豁免条目在加载配置时就被拒绝。

用法:
    .venv/bin/python scripts/check_file_conventions.py           # 检查，非零退出=有违规
    .venv/bin/python scripts/check_file_conventions.py --json     # 机器可读
    .venv/bin/python scripts/check_file_conventions.py --top 20   # 调整每个维度的榜单长度
    .venv/bin/python scripts/check_file_conventions.py --report-actuals
        # 只打印当前每个文件的实测指标，不做通过/失败判定。用于人工核对是否该
        # 调低某个文件的基线（文件瘦身之后应该手工调紧，闸门不会自动帮你调）。
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
FRONTEND_SRC = ROOT / "frontend" / "src"
DEFAULT_CONFIG_FILE = APP / "FILE_CONVENTIONS.toml"

# 维度名——闭集，由本工具自身定义（不是从外部数据枚举业务值，类比 LAYERS.toml
# 校验层号只能是 0-5）。exemptions 里的 dimension 字段必须落在这个集合里。
# 曾有第 5 个维度 `toplevel_defs`（顶层定义数），2026-08-29 移除——理由见模块
# docstring。移除后 `toplevel_defs` 不再是合法的 exemptions.dimension 取值。
DIM_LINE_COUNT = "line_count"
DIM_FUNCTION_LINES = "function_lines"
DIM_DOCSTRING = "docstring"
DIM_STAR_IMPORT = "star_import"
KNOWN_DIMENSIONS = {
    DIM_LINE_COUNT,
    DIM_FUNCTION_LINES,
    DIM_DOCSTRING,
    DIM_STAR_IMPORT,
}

PY_ONLY_DIMENSIONS = {DIM_FUNCTION_LINES, DIM_DOCSTRING, DIM_STAR_IMPORT}


class ConventionsConfigError(Exception):
    """`FILE_CONVENTIONS.toml` 本身有配置错误——工具不能信任这份数据，直接拒绝。"""


def relpath(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def collect_python_files() -> list[Path]:
    """`app/**/*.py`，与 `scripts/arch_graph.py` 的扫描范围一致。"""
    return sorted(APP.rglob("*.py"))


def collect_frontend_files() -> list[Path]:
    """`frontend/src/**/*.{ts,tsx}`。不含 `frontend` 根下的 `node_modules`/配置文件。"""
    return sorted(FRONTEND_SRC.rglob("*.ts")) + sorted(FRONTEND_SRC.rglob("*.tsx"))


# ---------------------------------------------------------------------------
# 配置加载与校验
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Exemption:
    path: str
    dimension: str
    reason: str
    expires: dt.date


@dataclass(frozen=True)
class ConventionsConfig:
    max_lines_python: int
    max_lines_frontend: int
    max_function_lines_python: int
    line_baseline: dict[str, int] = field(default_factory=dict)
    function_lines_baseline: dict[str, int] = field(default_factory=dict)
    docstring_exempt: frozenset[str] = field(default_factory=frozenset)
    star_import_exempt: frozenset[str] = field(default_factory=frozenset)
    exemptions: list[Exemption] = field(default_factory=list)


def _require_positive_int(data: dict, key: str, path: Path) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConventionsConfigError(
            f"{path}: defaults.{key} 必须是正整数，实得 {value!r}"
        )
    return value


def _load_int_baseline_table(data: dict, table_name: str, path: Path) -> dict[str, int]:
    raw = data.get("baseline", {}).get(table_name, {})
    if not isinstance(raw, dict):
        raise ConventionsConfigError(f"{path}: [baseline.{table_name}] 必须是表")
    out: dict[str, int] = {}
    for key, value in raw.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ConventionsConfigError(
                f"{path}: baseline.{table_name}[{key!r}] 必须是非负整数，实得 {value!r}"
            )
        out[key] = value
    return out


def _load_file_list_table(data: dict, table_name: str, path: Path) -> frozenset[str]:
    """读 `[baseline.<table_name>] files = [...]`——二元维度（有/没有）的棘轮基线：
    记录检查引入时已存在的既有情况，不是黑白名单业务判据，是当时实测状态的快照
    （与 line_count 等连续维度的 baseline 表同一性质，只是值域是布尔而非数字）。
    """
    raw = data.get("baseline", {}).get(table_name, {})
    if not isinstance(raw, dict):
        raise ConventionsConfigError(f"{path}: [baseline.{table_name}] 必须是表")
    files = raw.get("files", [])
    if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
        raise ConventionsConfigError(f"{path}: baseline.{table_name}.files 必须是字符串数组")
    return frozenset(files)


def load_config(path: Path) -> ConventionsConfig:
    """读取并校验 `FILE_CONVENTIONS.toml`。配置本身错了就报错，不静默接受。"""
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConventionsConfigError(f"找不到文件规范配置: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConventionsConfigError(f"{path} 不是合法 TOML: {exc}") from exc

    defaults = data.get("defaults")
    if not isinstance(defaults, dict):
        raise ConventionsConfigError(f"{path}: 缺少 [defaults] 表")
    max_lines_python = _require_positive_int(defaults, "max_lines_python", path)
    max_lines_frontend = _require_positive_int(defaults, "max_lines_frontend", path)
    max_function_lines_python = _require_positive_int(defaults, "max_function_lines_python", path)

    line_baseline = _load_int_baseline_table(data, "line_count", path)
    function_lines_baseline = _load_int_baseline_table(data, "function_lines", path)

    docstring_exempt = _load_file_list_table(data, "docstring_exempt", path)
    star_import_exempt = _load_file_list_table(data, "star_import_exempt", path)

    exemptions: list[Exemption] = []
    for i, entry in enumerate(data.get("exemptions", [])):
        if not isinstance(entry, dict):
            raise ConventionsConfigError(f"{path}: exemptions[{i}] 必须是表")
        missing = [k for k in ("path", "dimension", "reason", "expires") if not entry.get(k)]
        if missing:
            raise ConventionsConfigError(
                f"{path}: exemptions[{i}] 缺少字段 {missing}"
                "（豁免必须带 path/dimension/reason/expires，缺一律拒绝）"
            )
        dimension = str(entry["dimension"])
        if dimension not in KNOWN_DIMENSIONS:
            raise ConventionsConfigError(
                f"{path}: exemptions[{i}].dimension={dimension!r} 不是已知维度 "
                f"{sorted(KNOWN_DIMENSIONS)}"
            )
        try:
            expires = dt.date.fromisoformat(str(entry["expires"]))
        except ValueError as exc:
            raise ConventionsConfigError(
                f"{path}: exemptions[{i}].expires 不是合法日期 (YYYY-MM-DD): "
                f"{entry['expires']!r}"
            ) from exc
        exemptions.append(
            Exemption(
                path=str(entry["path"]),
                dimension=dimension,
                reason=str(entry["reason"]),
                expires=expires,
            )
        )

    return ConventionsConfig(
        max_lines_python=max_lines_python,
        max_lines_frontend=max_lines_frontend,
        max_function_lines_python=max_function_lines_python,
        line_baseline=line_baseline,
        function_lines_baseline=function_lines_baseline,
        docstring_exempt=docstring_exempt,
        star_import_exempt=star_import_exempt,
        exemptions=exemptions,
    )


# ---------------------------------------------------------------------------
# 单文件度量（纯函数：给一个路径，出实测数字。测试用 tmp_path 造真文件直接调用）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PythonMetrics:
    lines: int
    toplevel_defs: int
    longest_function_lines: int
    has_docstring: bool
    has_star_import: bool


def _function_code_lines(
    node: ast.FunctionDef | ast.AsyncFunctionDef, source_lines: list[str]
) -> int:
    """函数体里**实打实的代码行数**——剔除 docstring、空行、纯注释行。

    这个维度要衡量的是「一个函数塞了多少逻辑」，不是「它占了多少行」。用原始
    跨度（``end_lineno - lineno + 1``）会把文档算成复杂度，于是同一份逻辑，写了
    事故复盘 docstring 的版本反而更容易撞线——**惩罚写文档**。本仓库已经因为
    同一类反向激励删掉过整个 ``max_toplevel_defs_python`` 维度（它奖励焊大函数、
    惩罚拆分），不能再犯第二次。

    这也让阈值与业界工具可比：ESLint ``max-lines-per-function`` 的常用配置就是
    ``skipBlankLines`` + ``skipComments``，数的同样是代码行。

    多行表达式（跨行的函数调用、字典字面量）按其实际占用的行数计入——它们确实
    是代码；被剔除的只有文档与排版。
    """
    body = node.body
    if not body:
        return 0
    start = body[0].lineno
    # 首条语句是 docstring 时整体跳过：它是文档，不是逻辑。
    if (
        isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        if len(body) == 1:
            return 0
        start = body[1].lineno
    end = getattr(node, "end_lineno", None) or start
    count = 0
    for raw in source_lines[start - 1:end]:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        count += 1
    return count




def measure_python_file(path: Path) -> PythonMetrics | None:
    """返回 None 表示语法错误——不可解析的文件交给别的闸门（ruff/compileall）报，
    这里不重复报错也不假装它合格（不计入任何维度，报告里单列可见）。
    """
    try:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src, str(path))
    except (OSError, SyntaxError, UnicodeError):
        return None

    lines = len(src.splitlines())
    toplevel_defs = sum(
        1
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    )
    source_lines = src.splitlines()
    longest = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            longest = max(longest, _function_code_lines(node, source_lines))
    has_docstring = ast.get_docstring(tree) is not None
    has_star_import = any(
        isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names)
        for node in ast.walk(tree)
    )
    return PythonMetrics(
        lines=lines,
        toplevel_defs=toplevel_defs,
        longest_function_lines=longest,
        has_docstring=has_docstring,
        has_star_import=has_star_import,
    )


def measure_frontend_lines(path: Path) -> int | None:
    try:
        return len(path.read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeError):
        return None


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Violation:
    dimension: str
    path: str
    actual: int
    threshold: int

    @property
    def excess(self) -> int:
        return self.actual - self.threshold


@dataclass
class Report:
    violations: list[Violation] = field(default_factory=list)
    docstring_violations: list[tuple[str, int]] = field(default_factory=list)  # (path, lines) 排序用
    star_import_violations: list[str] = field(default_factory=list)
    expired_exemptions: list[Exemption] = field(default_factory=list)
    unparseable: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not (
            self.violations
            or self.docstring_violations
            or self.star_import_violations
            or self.expired_exemptions
        )


def _active_exemptions(
    exemptions: list[Exemption], today: dt.date
) -> tuple[dict[tuple[str, str], Exemption], list[Exemption]]:
    active: dict[tuple[str, str], Exemption] = {}
    expired: list[Exemption] = []
    for exc in exemptions:
        if exc.expires >= today:
            active[(exc.path, exc.dimension)] = exc
        else:
            expired.append(exc)
    return active, expired


def evaluate(
    python_files: list[Path],
    frontend_files: list[Path],
    config: ConventionsConfig,
    *,
    today: dt.date | None = None,
) -> Report:
    """核心判定：给一份显式文件清单（不是「全仓库」），逐个量、逐个判。

    显式清单是为了可测试性——测试传 tmp_path 里的假文件，不依赖真实 app/ 树
    （工作区正被并行 agent 改动）。真实检查用 `collect_python_files()` /
    `collect_frontend_files()` 的结果调用本函数。
    """
    today = today or dt.date.today()
    active, expired = _active_exemptions(config.exemptions, today)
    report = Report(expired_exemptions=expired)

    def exempted(rel: str, dimension: str) -> bool:
        return (rel, dimension) in active

    for path in python_files:
        rel = relpath(path)
        metrics = measure_python_file(path)
        if metrics is None:
            report.unparseable.append(rel)
            continue

        if not exempted(rel, DIM_LINE_COUNT):
            threshold = config.line_baseline.get(rel, config.max_lines_python)
            if metrics.lines > threshold:
                report.violations.append(Violation(DIM_LINE_COUNT, rel, metrics.lines, threshold))

        if not exempted(rel, DIM_FUNCTION_LINES):
            threshold = config.function_lines_baseline.get(
                rel, config.max_function_lines_python
            )
            if metrics.longest_function_lines > threshold:
                report.violations.append(
                    Violation(DIM_FUNCTION_LINES, rel, metrics.longest_function_lines, threshold)
                )

        if not exempted(rel, DIM_DOCSTRING):
            if not metrics.has_docstring and rel not in config.docstring_exempt:
                report.docstring_violations.append((rel, metrics.lines))

        if not exempted(rel, DIM_STAR_IMPORT):
            if metrics.has_star_import and rel not in config.star_import_exempt:
                report.star_import_violations.append(rel)

    for path in frontend_files:
        rel = relpath(path)
        lines = measure_frontend_lines(path)
        if lines is None:
            report.unparseable.append(rel)
            continue
        if not exempted(rel, DIM_LINE_COUNT):
            threshold = config.line_baseline.get(rel, config.max_lines_frontend)
            if lines > threshold:
                report.violations.append(Violation(DIM_LINE_COUNT, rel, lines, threshold))

    return report


# ---------------------------------------------------------------------------
# 报告输出
# ---------------------------------------------------------------------------

_DIM_LABEL = {
    DIM_LINE_COUNT: "文件行数超基线",
    DIM_FUNCTION_LINES: "单函数最长行数超基线",
}


def _print_dimension(dimension: str, violations: list[Violation], top: int) -> None:
    rows = [v for v in violations if v.dimension == dimension]
    if not rows:
        return
    rows.sort(key=lambda v: v.excess, reverse=True)
    print(f"\n{_DIM_LABEL[dimension]}：{len(rows)} 个文件")
    for v in rows[:top]:
        print(f"  超出 {v.excess:>6,}  实测 {v.actual:>7,}  基线 {v.threshold:>7,}  {v.path}")
    if len(rows) > top:
        print(f"  ...另有 {len(rows) - top} 个未列出（用 --top 调整显示数量）")


def print_report(report: Report, top: int) -> None:
    total = (
        len(report.violations)
        + len(report.docstring_violations)
        + len(report.star_import_violations)
    )
    print(f"文件规范违规共 {total} 条（另有过期豁免 {len(report.expired_exemptions)} 条）")

    for dimension in (DIM_LINE_COUNT, DIM_FUNCTION_LINES):
        _print_dimension(dimension, report.violations, top)

    if report.docstring_violations:
        rows = sorted(report.docstring_violations, key=lambda r: r[1], reverse=True)
        print(f"\n缺少模块 docstring：{len(rows)} 个文件")
        for rel, lines in rows[:top]:
            print(f"  {lines:>7,} 行  {rel}")
        if len(rows) > top:
            print(f"  ...另有 {len(rows) - top} 个未列出")

    if report.star_import_violations:
        print(f"\n使用 `from X import *`：{len(report.star_import_violations)} 个文件")
        for rel in sorted(report.star_import_violations)[:top]:
            print(f"  {rel}")

    if report.expired_exemptions:
        print(f"\n过期豁免 {len(report.expired_exemptions)} 条（已按违规计入上方总数，不再豁免）：")
        for exc in sorted(report.expired_exemptions, key=lambda e: (e.path, e.dimension)):
            print(
                f"  {exc.path} :: {exc.dimension}  (expires {exc.expires}, reason: {exc.reason})"
            )

    if report.unparseable:
        print(f"\n无法解析/读取（不计入任何维度，由其它闸门负责）：{len(report.unparseable)} 个")
        for rel in report.unparseable[:top]:
            print(f"  {rel}")

    if report.is_clean:
        print("\nOK: 未发现违规")
    else:
        print(f"\nFAIL: 共 {total} 条违规" + ("，另有过期豁免" if report.expired_exemptions else ""))


def report_to_dict(report: Report) -> dict:
    return {
        "violations": [
            {"dimension": v.dimension, "path": v.path, "actual": v.actual, "threshold": v.threshold, "excess": v.excess}
            for v in report.violations
        ],
        "docstring_violations": [{"path": p, "lines": n} for p, n in report.docstring_violations],
        "star_import_violations": report.star_import_violations,
        "expired_exemptions": [
            {"path": e.path, "dimension": e.dimension, "reason": e.reason, "expires": e.expires.isoformat()}
            for e in report.expired_exemptions
        ],
        "unparseable": report.unparseable,
        "clean": report.is_clean,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def run_check(config_path: Path, top: int, *, as_json: bool) -> int:
    try:
        config = load_config(config_path)
    except ConventionsConfigError as exc:
        print(f"FILE_CONVENTIONS.toml 配置错误: {exc}", file=sys.stderr)
        return 2

    python_files = collect_python_files()
    frontend_files = collect_frontend_files()
    # 空集合不等于「无需检查」：扫描范围本身配错了（比如路径改名）必须报错，
    # 不能因为「一个文件都没找到」就悄悄放行。
    if not python_files and not frontend_files:
        print(
            f"FAIL: 扫描范围为空（{APP} 与 {FRONTEND_SRC} 均无匹配文件），"
            "这通常意味着扫描路径配置错误，不是「全部合格」",
            file=sys.stderr,
        )
        return 2

    report = evaluate(python_files, frontend_files, config)

    if as_json:
        print(json.dumps(report_to_dict(report), ensure_ascii=False, indent=2))
    else:
        print_report(report, top)

    return 0 if report.is_clean else 1


def run_report_actuals(top: int) -> int:
    """只打印实测指标，不判定通过/失败。用于人工核对该不该调紧某个文件的基线。"""
    python_files = collect_python_files()
    frontend_files = collect_frontend_files()
    rows = []
    for path in python_files:
        metrics = measure_python_file(path)
        if metrics is None:
            continue
        rows.append(
            {
                "path": relpath(path),
                "lines": metrics.lines,
                "toplevel_defs": metrics.toplevel_defs,
                "longest_function_lines": metrics.longest_function_lines,
                "has_docstring": metrics.has_docstring,
            }
        )
    for path in frontend_files:
        lines = measure_frontend_lines(path)
        if lines is None:
            continue
        rows.append({"path": relpath(path), "lines": lines})
    print(json.dumps(sorted(rows, key=lambda r: -r["lines"]), ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_FILE, help="配置文件路径")
    parser.add_argument("--top", type=int, default=10, help="每个维度榜单长度")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument(
        "--report-actuals",
        action="store_true",
        help="只打印当前实测指标（JSON），不做通过/失败判定，用于人工决定是否调紧基线",
    )
    args = parser.parse_args()

    if args.report_actuals:
        return run_report_actuals(args.top)
    return run_check(args.config, args.top, as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
