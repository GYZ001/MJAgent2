"""Tests for `scripts/check_file_conventions.py`.

This gate is the file-shape counterpart to `scripts/arch_graph.py
--check-layers` (which governs cross-module dependencies): it governs what a
single file is allowed to look like (line count, longest single function,
module docstring, star imports). Like the layering gate, it is a ratchet --
baselines in `app/FILE_CONVENTIONS.toml` are the current allowance per file,
and anything not baselined falls back to the strict `[defaults]`. These tests
build fake files under `tmp_path` (real files on disk, since the metric
functions read real source) so assertions do not drift with the real
`app/`/`frontend/src` trees, which other agents are editing concurrently.

A fifth dimension, `toplevel_defs` (top-level definition count), was removed
2026-08-29: across the real `app/**/*.py` tree, line count and top-level
definition count correlate at 0.895 (line_count already covers "file is too
big"), and the one real god-function pattern it was meant to catch (a single
huge function/method) is fully covered by `function_lines`, which walks the
whole AST (including class methods and nested functions), not just the
top-level. Worse, it actively penalized good decomposition: splitting
`compile_screenplay_ir` from one 3,585-line function into a 236-line
orchestrator + ~45 named helpers improved `function_lines` from 3,585 to 322,
but pushed `toplevel_defs` from 55 to 82, forcing its baseline up --
rewarding the monolith and punishing the refactor. `measure_python_file`
still reports `toplevel_defs` for human inspection via `--report-actuals`;
it is simply no longer a gating dimension. See
`scripts/check_file_conventions.py`'s module docstring for the full data.
"""
from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.check_file_conventions import (
    DEFAULT_CONFIG_FILE,
    DIM_FUNCTION_LINES,
    DIM_LINE_COUNT,
    ROOT,
    ConventionsConfig,
    ConventionsConfigError,
    Exemption,
    evaluate,
    load_config,
    measure_frontend_lines,
    measure_python_file,
    report_to_dict,
    run_check,
)


def _write(tmp_path: Path, rel: str, content: str) -> Path:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _base_config(**overrides) -> ConventionsConfig:
    defaults = dict(
        max_lines_python=20,
        max_lines_frontend=20,
        max_function_lines_python=10,
    )
    defaults.update(overrides)
    return ConventionsConfig(**defaults)


# ---------------------------------------------------------------------------
# measure_python_file: AST-driven metrics
# ---------------------------------------------------------------------------


def test_measure_python_file_counts_lines_and_toplevel_defs(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "m.py",
        '"""Module doc."""\n\ndef f():\n    pass\n\n\nclass C:\n    pass\n',
    )

    metrics = measure_python_file(path)

    assert metrics is not None
    assert metrics.toplevel_defs == 2
    assert metrics.has_docstring is True
    assert metrics.has_star_import is False
    assert metrics.lines == 8


def test_measure_python_file_detects_missing_docstring(tmp_path: Path) -> None:
    path = _write(tmp_path, "m.py", "VALUE = 1\n")

    metrics = measure_python_file(path)

    assert metrics is not None
    assert metrics.has_docstring is False


def test_measure_python_file_detects_star_import(tmp_path: Path) -> None:
    path = _write(tmp_path, "m.py", '"""doc"""\nfrom app.foo import *\n')

    metrics = measure_python_file(path)

    assert metrics is not None
    assert metrics.has_star_import is True


def test_measure_python_file_computes_longest_function(tmp_path: Path) -> None:
    body = "\n".join(f"    x{i} = {i}" for i in range(15))
    path = _write(tmp_path, "m.py", f'"""doc"""\ndef big():\n{body}\n\n\ndef small():\n    pass\n')

    metrics = measure_python_file(path)

    assert metrics is not None
    # 15 条 body 语句。度量的是函数体里的代码行，不含 def 那一行——这个维度回答
    # 的是「这个函数塞了多少逻辑」，签名行不是逻辑。
    assert metrics.longest_function_lines == 15


def test_longest_function_excludes_docstrings_blank_lines_and_comments(tmp_path: Path) -> None:
    """函数行数只数代码，不数文档与排版。

    否则同一份逻辑，写了事故复盘 docstring 的版本反而更容易撞线——**惩罚写
    文档**。本仓库已经因为同一类反向激励删掉过整个 max_toplevel_defs_python
    维度（它奖励焊大函数、惩罚拆分），这个维度不能重蹈覆辙。
    """
    source = (
        '"""module"""\n'
        "def documented():\n"
        '    """一段很长的 docstring。\n'
        "\n"
        "    这里记着事故复盘，占很多行，\n"
        "    但它一行逻辑都没有。\n"
        "\n"
        "    第二段说明。\n"
        '    """\n'
        "    # 一条纯注释，不是逻辑\n"
        "\n"
        "    a = 1\n"
        "\n"
        "    # 又一条注释\n"
        "    b = 2\n"
        "    return a + b\n"
    )
    path = _write(tmp_path, "m.py", source)

    metrics = measure_python_file(path)

    assert metrics is not None
    # 只有 a = 1 / b = 2 / return 三行是代码。
    assert metrics.longest_function_lines == 3


def test_longest_function_counts_a_docstring_only_function_as_zero(tmp_path: Path) -> None:
    """只有 docstring、没有语句的函数（协议桩、抽象方法）计 0，不是「很大」。"""
    path = _write(tmp_path, "m.py", '"""module"""\ndef stub():\n    """只有文档。\n\n    第二段。\n    """\n')

    metrics = measure_python_file(path)

    assert metrics is not None
    assert metrics.longest_function_lines == 0


def test_measure_python_file_returns_none_on_syntax_error(tmp_path: Path) -> None:
    path = _write(tmp_path, "m.py", "def broken(:\n")

    assert measure_python_file(path) is None


def test_measure_frontend_lines_counts_lines(tmp_path: Path) -> None:
    path = _write(tmp_path, "c.tsx", "export function C() {\n  return null\n}\n")

    assert measure_frontend_lines(path) == 3


def test_measure_frontend_lines_returns_none_when_unreadable(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.ts"

    assert measure_frontend_lines(missing) is None


# ---------------------------------------------------------------------------
# evaluate(): the core per-file ratchet judgement
# ---------------------------------------------------------------------------


def test_undeclared_file_is_still_checked_against_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A file with no baseline entry and no exemption must not be silently
    # skipped -- it is judged against the strict [defaults] ceiling.
    monkeypatch.setattr("scripts.check_file_conventions.ROOT", tmp_path)
    long_body = "\n".join(f"x{i} = {i}" for i in range(30))
    path = _write(tmp_path, "new_module.py", f'"""doc"""\n{long_body}\n')
    config = _base_config()  # max_lines_python=20, file has 31 lines

    report = evaluate([path], [], config)

    assert len(report.violations) == 1
    v = report.violations[0]
    assert v.dimension == DIM_LINE_COUNT
    assert v.path == "new_module.py"
    assert v.threshold == 20


def test_file_under_default_is_clean_without_any_baseline_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scripts.check_file_conventions.ROOT", tmp_path)
    path = _write(tmp_path, "small.py", '"""doc"""\nVALUE = 1\n')
    config = _base_config()

    report = evaluate([path], [], config)

    assert report.is_clean


def test_line_count_violation_reports_excess_relative_to_threshold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Put the fake file inside ROOT via monkeypatching relpath's dependency
    # (ROOT) so the baseline key matches what evaluate() computes.
    fake_root = tmp_path
    monkeypatch.setattr("scripts.check_file_conventions.ROOT", fake_root)
    long_body = "\n".join(f"x{i} = {i}" for i in range(30))
    path = _write(tmp_path, "big.py", f'"""doc"""\n{long_body}\n')  # 31 lines
    config = _base_config(**{})
    config = ConventionsConfig(
        max_lines_python=20,
        max_lines_frontend=20,
        max_function_lines_python=10,
        line_baseline={"big.py": 25},
    )

    report = evaluate([path], [], config)

    assert len(report.violations) == 1
    v = report.violations[0]
    assert v.path == "big.py"
    assert v.threshold == 25
    assert v.actual == 31
    assert v.excess == 6


def test_baselined_file_at_or_below_its_ceiling_is_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scripts.check_file_conventions.ROOT", tmp_path)
    long_body = "\n".join(f"x{i} = {i}" for i in range(30))
    path = _write(tmp_path, "big.py", f'"""doc"""\n{long_body}\n')  # 31 lines
    config = ConventionsConfig(
        max_lines_python=20,
        max_lines_frontend=20,
        max_function_lines_python=10,
        line_baseline={"big.py": 50},  # generous ceiling: 31 <= 50
    )

    report = evaluate([path], [], config)

    assert report.is_clean


def test_many_toplevel_defs_is_not_a_violation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # `toplevel_defs` (removed 2026-08-29, see module docstring) no longer
    # gates anything -- a file with far more top-level definitions than the
    # old default (30) must not fail on that basis alone.
    monkeypatch.setattr("scripts.check_file_conventions.ROOT", tmp_path)
    path = _write(
        tmp_path,
        "many_defs.py",
        '"""doc"""\n' + "\n".join(f"def f{i}(): pass" for i in range(50)),
    )
    # Generous line/function caps so only definition *count* is at stake --
    # 51 lines, 50 one-line defs, nowhere near max_lines_python or
    # max_function_lines_python.
    config = _base_config(max_lines_python=100, max_function_lines_python=100)

    report = evaluate([path], [], config)

    assert report.is_clean


def test_decomposing_a_monolith_scores_no_worse_than_the_monolith(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reverse-incentive regression test (the reason `toplevel_defs` was
    deleted): splitting one giant function into many small named helpers,
    holding the total amount of code roughly constant, must never score
    worse than leaving it as one function. This mirrors the real case that
    motivated the removal -- `compile_screenplay_ir` went from a 3,585-line
    single function (a `function_lines` violation) to a 236-line
    orchestrator + ~45 helpers each well under the cap, a strict quality
    improvement that the old absolute-count `toplevel_defs` dimension
    penalized (55 -> 82 defs, forcing its baseline up) instead of rewarding.
    """
    monkeypatch.setattr("scripts.check_file_conventions.ROOT", tmp_path)
    config = ConventionsConfig(
        max_lines_python=600,
        max_lines_frontend=400,
        max_function_lines_python=200,
    )

    # One 496-line function: trips function_lines (496 > 200), stays under
    # the 600-line file cap.
    monolith_body = "\n".join(f"    x{i} = {i}" for i in range(495))
    monolith = _write(tmp_path, "monolith.py", f'"""doc"""\ndef big():\n{monolith_body}\n')

    # The same amount of code, decomposed into 10 named helpers of ~49 lines
    # each -- comfortably under the function_lines cap, and the file total
    # (~500 lines) stays under the line_count cap too.
    helper_blocks = []
    for i in range(10):
        body = "\n".join(f"    y{j} = {j}" for j in range(48))
        helper_blocks.append(f"def helper_{i}():\n{body}\n")
    decomposed_src = '"""doc"""\n' + "\n\n".join(helper_blocks) + "\n"
    decomposed = _write(tmp_path, "decomposed.py", decomposed_src)

    monolith_report = evaluate([monolith], [], config)
    decomposed_report = evaluate([decomposed], [], config)

    assert [v.dimension for v in monolith_report.violations] == [DIM_FUNCTION_LINES]
    assert decomposed_report.is_clean
    assert len(decomposed_report.violations) <= len(monolith_report.violations)


def test_function_lines_violation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scripts.check_file_conventions.ROOT", tmp_path)
    body = "\n".join(f"    x{i} = {i}" for i in range(20))
    path = _write(tmp_path, "big_fn.py", f'"""doc"""\ndef f():\n{body}\n')
    config = _base_config()  # max_function_lines_python=10

    report = evaluate([path], [], config)

    dims = [v.dimension for v in report.violations]
    assert DIM_FUNCTION_LINES in dims


def test_missing_docstring_on_new_file_is_a_violation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scripts.check_file_conventions.ROOT", tmp_path)
    path = _write(tmp_path, "no_doc.py", "VALUE = 1\n")
    config = _base_config()

    report = evaluate([path], [], config)

    assert ("no_doc.py", 1) in report.docstring_violations


def test_missing_docstring_on_grandfathered_file_is_not_a_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scripts.check_file_conventions.ROOT", tmp_path)
    path = _write(tmp_path, "legacy.py", "VALUE = 1\n")
    config = ConventionsConfig(
        max_lines_python=20,
        max_lines_frontend=20,
        max_function_lines_python=10,
        docstring_exempt=frozenset({"legacy.py"}),
    )

    report = evaluate([path], [], config)

    assert report.docstring_violations == []


def test_new_star_import_not_in_exempt_list_is_a_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scripts.check_file_conventions.ROOT", tmp_path)
    path = _write(tmp_path, "leaky.py", '"""doc"""\nfrom app.foo import *\n')
    config = _base_config()

    report = evaluate([path], [], config)

    assert "leaky.py" in report.star_import_violations


def test_grandfathered_star_import_is_not_a_violation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scripts.check_file_conventions.ROOT", tmp_path)
    path = _write(tmp_path, "facade.py", '"""doc"""\nfrom app.media_exec.common import *\n')
    config = ConventionsConfig(
        max_lines_python=20,
        max_lines_frontend=20,
        max_function_lines_python=10,
        star_import_exempt=frozenset({"facade.py"}),
    )

    report = evaluate([path], [], config)

    assert report.star_import_violations == []


def test_active_exemption_suppresses_a_specific_dimension(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scripts.check_file_conventions.ROOT", tmp_path)
    long_body = "\n".join(f"x{i} = {i}" for i in range(30))
    path = _write(tmp_path, "vendored.py", f'"""doc"""\n{long_body}\n')  # 31 lines
    future = dt.date.today() + dt.timedelta(days=30)
    config = ConventionsConfig(
        max_lines_python=20,
        max_lines_frontend=20,
        max_function_lines_python=10,
        exemptions=[Exemption("vendored.py", DIM_LINE_COUNT, "vendored snapshot", future)],
    )

    report = evaluate([path], [], config)

    assert report.is_clean
    assert report.expired_exemptions == []


def test_expired_exemption_still_reports_as_a_violation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scripts.check_file_conventions.ROOT", tmp_path)
    long_body = "\n".join(f"x{i} = {i}" for i in range(30))
    path = _write(tmp_path, "vendored.py", f'"""doc"""\n{long_body}\n')
    config = ConventionsConfig(
        max_lines_python=20,
        max_lines_frontend=20,
        max_function_lines_python=10,
        exemptions=[Exemption("vendored.py", DIM_LINE_COUNT, "vendored snapshot", dt.date(2000, 1, 1))],
    )

    report = evaluate([path], [], config)

    assert len(report.violations) == 1
    assert len(report.expired_exemptions) == 1
    assert report.expired_exemptions[0].reason == "vendored snapshot"


def test_frontend_file_uses_frontend_default_not_python_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scripts.check_file_conventions.ROOT", tmp_path)
    # 15 lines: over max_lines_frontend (10) but under max_lines_python (20).
    content = "\n".join(f"const x{i} = {i};" for i in range(15)) + "\n"
    path = _write(tmp_path, "Component.tsx", content)
    config = ConventionsConfig(
        max_lines_python=20,
        max_lines_frontend=10,
        max_function_lines_python=10,
    )

    report = evaluate([], [path], config)

    assert len(report.violations) == 1
    assert report.violations[0].threshold == 10


def test_unparseable_file_is_listed_but_not_counted_as_a_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scripts.check_file_conventions.ROOT", tmp_path)
    path = _write(tmp_path, "broken.py", "def broken(:\n")
    config = _base_config()

    report = evaluate([path], [], config)

    assert report.is_clean  # not silently passed as "compliant" either -- surfaced separately
    assert "broken.py" in report.unparseable


def test_dimension_violations_are_ranked_by_excess_descending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scripts.check_file_conventions.ROOT", tmp_path)
    small_excess = _write(tmp_path, "small_excess.py", '"""doc"""\n' + "x = 1\n" * 22)  # 23 lines
    big_excess = _write(tmp_path, "big_excess.py", '"""doc"""\n' + "x = 1\n" * 60)  # 61 lines
    config = _base_config()  # max_lines_python=20

    report = evaluate([small_excess, big_excess], [], config)

    line_violations = sorted(
        (v for v in report.violations if v.dimension == DIM_LINE_COUNT),
        key=lambda v: v.excess,
        reverse=True,
    )
    assert [v.path for v in line_violations] == ["big_excess.py", "small_excess.py"]


# ---------------------------------------------------------------------------
# load_config: fail loudly on malformed configuration
# ---------------------------------------------------------------------------


def _minimal_defaults() -> str:
    # Deliberately omits a top-level `exemptions = []` -- load_config() treats
    # a missing key as "no exemptions" (tested separately), and several tests
    # below append `[[exemptions]]` array-of-tables after this block, which
    # TOML forbids if a plain `exemptions = []` already fixed the key's type
    # (this is the exact bug app/LAYERS.toml's header comment warns about).
    return (
        "[defaults]\n"
        "max_lines_python = 600\n"
        "max_lines_frontend = 400\n"
        "max_function_lines_python = 200\n"
    )


def test_load_config_accepts_a_well_formed_minimal_file(tmp_path: Path) -> None:
    toml_path = tmp_path / "FILE_CONVENTIONS.toml"
    toml_path.write_text(_minimal_defaults(), encoding="utf-8")

    config = load_config(toml_path)

    assert config.max_lines_python == 600
    assert config.line_baseline == {}
    assert config.docstring_exempt == frozenset()


def test_load_config_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConventionsConfigError):
        load_config(tmp_path / "does-not-exist.toml")


def test_load_config_rejects_malformed_toml(tmp_path: Path) -> None:
    toml_path = tmp_path / "FILE_CONVENTIONS.toml"
    toml_path.write_text("not [ valid toml", encoding="utf-8")

    with pytest.raises(ConventionsConfigError):
        load_config(toml_path)


def test_load_config_rejects_missing_defaults_table(tmp_path: Path) -> None:
    toml_path = tmp_path / "FILE_CONVENTIONS.toml"
    toml_path.write_text("exemptions = []\n", encoding="utf-8")

    with pytest.raises(ConventionsConfigError, match=r"\[defaults\]"):
        load_config(toml_path)


@pytest.mark.parametrize(
    "key", ["max_lines_python", "max_lines_frontend", "max_function_lines_python"]
)
def test_load_config_rejects_non_positive_default(tmp_path: Path, key: str) -> None:
    toml_path = tmp_path / "FILE_CONVENTIONS.toml"
    values = {
        "max_lines_python": 600,
        "max_lines_frontend": 400,
        "max_function_lines_python": 200,
    }
    values[key] = 0
    body = "[defaults]\n" + "\n".join(f"{k} = {v}" for k, v in values.items()) + "\n"
    toml_path.write_text(body, encoding="utf-8")

    with pytest.raises(ConventionsConfigError, match=key):
        load_config(toml_path)


def test_load_config_rejects_non_integer_baseline_value(tmp_path: Path) -> None:
    toml_path = tmp_path / "FILE_CONVENTIONS.toml"
    toml_path.write_text(
        _minimal_defaults() + '\n[baseline.line_count]\n"app/x.py" = "not-a-number"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConventionsConfigError):
        load_config(toml_path)


def test_load_config_rejects_exemption_missing_reason(tmp_path: Path) -> None:
    toml_path = tmp_path / "FILE_CONVENTIONS.toml"
    toml_path.write_text(
        _minimal_defaults()
        + '\n[[exemptions]]\npath = "app/x.py"\ndimension = "line_count"\nexpires = "2099-01-01"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConventionsConfigError, match="reason"):
        load_config(toml_path)


def test_load_config_rejects_exemption_missing_expires(tmp_path: Path) -> None:
    toml_path = tmp_path / "FILE_CONVENTIONS.toml"
    toml_path.write_text(
        _minimal_defaults()
        + '\n[[exemptions]]\npath = "app/x.py"\ndimension = "line_count"\nreason = "temp"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConventionsConfigError, match="expires"):
        load_config(toml_path)


def test_load_config_rejects_exemption_with_unknown_dimension(tmp_path: Path) -> None:
    toml_path = tmp_path / "FILE_CONVENTIONS.toml"
    toml_path.write_text(
        _minimal_defaults()
        + '\n[[exemptions]]\npath = "app/x.py"\ndimension = "not_a_real_dimension"\n'
        'reason = "temp"\nexpires = "2099-01-01"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConventionsConfigError, match="not_a_real_dimension"):
        load_config(toml_path)


def test_load_config_rejects_exemption_with_unparseable_date(tmp_path: Path) -> None:
    toml_path = tmp_path / "FILE_CONVENTIONS.toml"
    toml_path.write_text(
        _minimal_defaults()
        + '\n[[exemptions]]\npath = "app/x.py"\ndimension = "line_count"\n'
        'reason = "temp"\nexpires = "not-a-date"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConventionsConfigError, match="expires"):
        load_config(toml_path)


def test_load_config_accepts_well_formed_exemption(tmp_path: Path) -> None:
    toml_path = tmp_path / "FILE_CONVENTIONS.toml"
    toml_path.write_text(
        _minimal_defaults()
        + '\n[[exemptions]]\npath = "app/x.py"\ndimension = "line_count"\n'
        'reason = "temp bridge"\nexpires = "2099-01-01"\n',
        encoding="utf-8",
    )

    config = load_config(toml_path)

    assert len(config.exemptions) == 1
    assert config.exemptions[0].reason == "temp bridge"


def test_load_config_rejects_docstring_exempt_non_string_entries(tmp_path: Path) -> None:
    toml_path = tmp_path / "FILE_CONVENTIONS.toml"
    toml_path.write_text(
        _minimal_defaults() + "\n[baseline.docstring_exempt]\nfiles = [1, 2]\n",
        encoding="utf-8",
    )

    with pytest.raises(ConventionsConfigError):
        load_config(toml_path)


# ---------------------------------------------------------------------------
# run_check: CLI-level exit codes, empty-scan guard
# ---------------------------------------------------------------------------


def test_run_check_returns_2_on_missing_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scripts.check_file_conventions.collect_python_files", lambda: [])
    monkeypatch.setattr("scripts.check_file_conventions.collect_frontend_files", lambda: [])

    exit_code = run_check(tmp_path / "missing.toml", 10, as_json=False)

    assert exit_code == 2


def test_run_check_returns_2_when_scan_scope_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Simulates a misconfigured scan path: config is valid but no files are
    # discovered at all. An empty set here must not be read as "all clean".
    toml_path = tmp_path / "FILE_CONVENTIONS.toml"
    toml_path.write_text(_minimal_defaults(), encoding="utf-8")
    monkeypatch.setattr("scripts.check_file_conventions.collect_python_files", lambda: [])
    monkeypatch.setattr("scripts.check_file_conventions.collect_frontend_files", lambda: [])

    exit_code = run_check(toml_path, 10, as_json=False)

    assert exit_code == 2
    assert "FAIL" in capsys.readouterr().err


def test_run_check_returns_0_when_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("scripts.check_file_conventions.ROOT", tmp_path)
    toml_path = tmp_path / "FILE_CONVENTIONS.toml"
    toml_path.write_text(_minimal_defaults(), encoding="utf-8")
    good = _write(tmp_path, "clean.py", '"""doc"""\nVALUE = 1\n')
    monkeypatch.setattr("scripts.check_file_conventions.collect_python_files", lambda: [good])
    monkeypatch.setattr("scripts.check_file_conventions.collect_frontend_files", lambda: [])

    exit_code = run_check(toml_path, 10, as_json=False)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "OK" in out


def test_run_check_returns_1_when_a_file_violates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("scripts.check_file_conventions.ROOT", tmp_path)
    toml_path = tmp_path / "FILE_CONVENTIONS.toml"
    toml_path.write_text(_minimal_defaults(), encoding="utf-8")
    bad = _write(tmp_path, "no_doc.py", "VALUE = 1\n")
    monkeypatch.setattr("scripts.check_file_conventions.collect_python_files", lambda: [bad])
    monkeypatch.setattr("scripts.check_file_conventions.collect_frontend_files", lambda: [])

    exit_code = run_check(toml_path, 10, as_json=False)

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL" in out


def test_run_check_json_output_matches_report_to_dict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json as _json

    monkeypatch.setattr("scripts.check_file_conventions.ROOT", tmp_path)
    toml_path = tmp_path / "FILE_CONVENTIONS.toml"
    toml_path.write_text(_minimal_defaults(), encoding="utf-8")
    good = _write(tmp_path, "clean.py", '"""doc"""\nVALUE = 1\n')
    monkeypatch.setattr("scripts.check_file_conventions.collect_python_files", lambda: [good])
    monkeypatch.setattr("scripts.check_file_conventions.collect_frontend_files", lambda: [])

    exit_code = run_check(toml_path, 10, as_json=True)

    payload = _json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["clean"] is True


def test_report_to_dict_is_json_serializable_with_violations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scripts.check_file_conventions.ROOT", tmp_path)
    long_body = "\n".join(f"x{i} = {i}" for i in range(30))
    path = _write(tmp_path, "big.py", f'"""doc"""\n{long_body}\n')
    config = _base_config()

    report = evaluate([path], [], config)
    payload = report_to_dict(report)

    assert payload["clean"] is False
    assert payload["violations"][0]["dimension"] == DIM_LINE_COUNT
    assert payload["violations"][0]["path"] == "big.py"


# ---------------------------------------------------------------------------
# Real-repo integration: the committed config loads and the gate currently
# passes against its own baseline (the ratchet was captured from this exact
# tree; if this starts failing, either the baseline needs a manual refresh
# after real shrinkage, or someone regressed past their buffer).
# ---------------------------------------------------------------------------


def _repo_root():
    """仓库根目录——从配置文件位置推导（app/FILE_CONVENTIONS.toml 的祖父目录）。"""
    return DEFAULT_CONFIG_FILE.parent.parent


def test_real_config_loads_without_errors() -> None:
    config = load_config(DEFAULT_CONFIG_FILE)

    assert config.max_lines_python > 0
    assert config.max_lines_frontend > 0
    # 判据从数据推导，不写死文件名：写死会在任何一次拆包之后断裂（本断言原本
    # 钉在 app/stages.py 上，该文件拆成包之后测试就红了，而配置本身是对的）。
    assert config.line_baseline, "基线不应为空"
    # 基线条目指向已不存在的文件 = 陈旧配置，棘轮会因此永远收不紧。
    stale = [path for path in config.line_baseline if not (_repo_root() / path).exists()]
    assert not stale, f"基线里有指向已不存在文件的陈旧条目: {stale}"
    # 这里曾经断言「star_import 豁免名单不应为空」。那是把断言钉在一个**会被
    # 正常演进消除**的临时状态上：豁免名单里原本是 exec() 外观遗留的
    # `from .common import *`，2026-08-30 四个 exec 外观全部改成真包之后名单
    # 清零——空名单是既定目标，不是配置坏了。真正该守的是「名单里不许有指向
    # 已不存在文件的陈旧条目」，那条在上面。
    assert isinstance(config.star_import_exempt, (set, frozenset, list, tuple))


def test_real_repo_check_runs_cleanly_against_the_committed_config() -> None:
    # This does NOT assert returncode == 0. The baseline was captured from a
    # single git-status snapshot while three other agents were actively
    # committing to app/ and frontend/src/ -- a brand-new file created after
    # capture (no baseline entry yet) legitimately trips the tight [defaults]
    # ceiling the moment it lands, which is the gate doing its job, not a
    # bug. Asserting a fixed pass/fail here would make this test flake on
    # unrelated concurrent work. What must hold regardless of who else is
    # editing: the config itself loads (no exit-2 config error) and the tool
    # doesn't crash.
    result = subprocess.run(
        [sys.executable, "scripts/check_file_conventions.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode in (0, 1), result.stdout + result.stderr
    assert "配置错误" not in result.stderr


def test_real_repo_report_actuals_runs_and_emits_json() -> None:
    import json as _json

    result = subprocess.run(
        [sys.executable, "scripts/check_file_conventions.py", "--report-actuals"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    rows = _json.loads(result.stdout)
    assert isinstance(rows, list)
    # 同上：不钉具体文件名。只断言真的扫到了东西、且每行形状正确。
    assert rows, "--report-actuals 应当至少报出一个文件"
    assert all({"path", "lines"} <= set(r) for r in rows)
    assert any((_repo_root() / r["path"]).exists() for r in rows)
