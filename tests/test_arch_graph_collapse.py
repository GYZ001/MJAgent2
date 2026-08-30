"""Tests for exec-facade folding in `scripts/arch_graph.py`.

Some backend packages (`app/domain`, `app/media_exec`, `app/portraits`) use
`exec(compile(chunk_source), globals())` to run several source files into one
shared module namespace. At runtime those chunk files *are* the facade module
-- there is no import statement connecting them, just bare-name resolution
inside a shared `globals()`. A plain AST import-graph treats every chunk file
as an independent node, which means splitting one big in-cycle module into N
exec chunks can make the "largest SCC as % of backend LOC" metric drop even
though the runtime coupling did not change at all (see
`app/portraits/__init__.py`'s docstring and the `--no-collapse` flag's help
text for the concrete before/after numbers). These tests pin the fix: the
facade + its chunks must collapse into one logical node by default.

Fixtures build a fake package under `tmp_path` and monkeypatch
`scripts.arch_graph.ROOT` to it, so `module_name()`'s `path.relative_to(ROOT)`
resolves correctly without touching the real `app/` tree (which two other
agents are editing concurrently).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.arch_graph import (
    ROOT,
    build_graph,
    collapse_exec_facades,
    exec_facade_chunk_map,
    parse_exec_chunk_files,
)


# ---------------------------------------------------------------------------
# parse_exec_chunk_files: pure AST-shape derivation, no hardcoded names
# ---------------------------------------------------------------------------


def test_parse_exec_chunk_files_finds_chunks_regardless_of_variable_name(tmp_path: Path) -> None:
    """The real facades each use a different variable name (`_DOMAIN_MODULES`,
    `_MEDIA_MODULES`, `_PORTRAIT_CHUNKS`). The parser must not hardcode any of
    them -- it should recognize the AST shape: a module-level string-list
    assignment used as the `iter` of a `for` loop whose body calls
    `exec(compile(...))`.
    """
    facade = tmp_path / "weird_facade.py"
    facade.write_text(
        "SOME_UNRELATED_LIST = ['not_a_chunk.py']\n"
        "for _x in SOME_UNRELATED_LIST:\n"
        "    print(_x)  # no exec/compile in this loop -- must NOT be picked up\n"
        "\n"
        "_TOTALLY_ARBITRARY_NAME_9000 = ('one.py', 'two.py', 'three.py')\n"
        "import pathlib as _pl\n"
        "_base = _pl.Path(__file__).resolve().parent\n"
        "for _rel in _TOTALLY_ARBITRARY_NAME_9000:\n"
        "    _p = _base / _rel\n"
        "    exec(compile(_p.read_text(encoding='utf-8'), str(_p), 'exec'), globals())\n",
        encoding="utf-8",
    )

    assert parse_exec_chunk_files(facade) == ["one.py", "two.py", "three.py"]


def test_parse_exec_chunk_files_returns_empty_for_plain_module(tmp_path: Path) -> None:
    plain = tmp_path / "plain.py"
    plain.write_text("VALUE = 1\n", encoding="utf-8")

    assert parse_exec_chunk_files(plain) == []


def test_parse_exec_chunk_files_ignores_exec_of_a_single_literal_not_a_loop(tmp_path: Path) -> None:
    # Text-level "exec(compile(" heuristic (find_exec_facades) would flag this
    # file as a candidate, but there's no string-list + for-loop shape to
    # recover a chunk list from, so the AST-level parser must return [].
    facade = tmp_path / "one_off.py"
    facade.write_text(
        "exec(compile('VALUE = 1', 'inline', 'exec'), globals())\n",
        encoding="utf-8",
    )

    assert parse_exec_chunk_files(facade) == []


# ---------------------------------------------------------------------------
# exec_facade_chunk_map: filename -> module name resolution against `modules`
# ---------------------------------------------------------------------------


def _write_fake_facade_package(tmp_path: Path) -> dict[str, Path]:
    """Build `tmp_path/app/pkg/` as a two-chunk exec facade + one outside
    module, and return the `modules` dict `collect_modules()` would produce
    for that layout (import targets must start with "app" for `parse_edges`
    to pick them up, so the fake tree is rooted at `tmp_path/app/`).
    """
    pkg_dir = tmp_path / "app" / "pkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text(
        "from pathlib import Path as _P\n"
        "_BASE = _P(__file__).resolve().parent\n"
        "_ODD_NAME_123 = ('a.py', 'b.py')\n"
        "for _f in _ODD_NAME_123:\n"
        "    _fp = _BASE / _f\n"
        "    exec(compile(_fp.read_text(encoding='utf-8'), str(_fp), 'exec'), globals())\n",
        encoding="utf-8",
    )
    (pkg_dir / "a.py").write_text("import app.outside\nVALUE_A = 1\n", encoding="utf-8")
    (pkg_dir / "b.py").write_text(
        # Unreachable-at-runtime fallback import some real chunk files keep
        # for readability (see app/domain/__init__.py docstring) -- still a
        # real AST edge that must be folded away as internal.
        "try:\n"
        "    VALUE_A\n"
        "except NameError:\n"
        "    from app.pkg.a import VALUE_A\n"
        "VALUE_B = 2\n",
        encoding="utf-8",
    )
    outside = tmp_path / "app" / "outside.py"
    outside.write_text("VALUE = 1\n", encoding="utf-8")

    return {
        "app.pkg": pkg_dir / "__init__.py",
        "app.pkg.a": pkg_dir / "a.py",
        "app.pkg.b": pkg_dir / "b.py",
        "app.outside": outside,
    }


def test_exec_facade_chunk_map_resolves_relative_filenames_to_module_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scripts.arch_graph.ROOT", tmp_path)
    modules = _write_fake_facade_package(tmp_path)

    chunk_map = exec_facade_chunk_map(modules)

    assert chunk_map == {"app.pkg": ["app.pkg.a", "app.pkg.b"]}


def test_exec_facade_chunk_map_skips_listed_files_that_do_not_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scripts.arch_graph.ROOT", tmp_path)
    modules = _write_fake_facade_package(tmp_path)
    # Rewrite the facade to also list a chunk file that was never created --
    # collect_modules() never saw it, so it must not appear in the map (not
    # silently pretending a folded package member exists when it doesn't).
    (tmp_path / "app" / "pkg" / "__init__.py").write_text(
        "from pathlib import Path as _P\n"
        "_BASE = _P(__file__).resolve().parent\n"
        "_CHUNKS = ('a.py', 'b.py', 'does_not_exist.py')\n"
        "for _f in _CHUNKS:\n"
        "    _fp = _BASE / _f\n"
        "    exec(compile(_fp.read_text(encoding='utf-8'), str(_fp), 'exec'), globals())\n",
        encoding="utf-8",
    )

    chunk_map = exec_facade_chunk_map(modules)

    assert chunk_map == {"app.pkg": ["app.pkg.a", "app.pkg.b"]}


# ---------------------------------------------------------------------------
# collapse_exec_facades: LOC sums, edge unions, internal edges dropped
# ---------------------------------------------------------------------------


def test_collapse_exec_facades_merges_loc_unions_edges_and_drops_internal_edges() -> None:
    graph = {
        "facade": {"chunk_a"},
        "chunk_a": {"chunk_b", "external"},
        "chunk_b": {"facade"},
        "external": set(),
    }
    edge_rows = [
        ("facade", "chunk_a", "chunk_a", False),
        ("chunk_a", "chunk_b", "b", False),
        ("chunk_a", "external", "thing", True),
        ("chunk_b", "facade", "facade", False),
    ]
    stats = {
        "facade": {"loc": 10, "toplevel": 1},
        "chunk_a": {"loc": 20, "toplevel": 2},
        "chunk_b": {"loc": 30, "toplevel": 3},
        "external": {"loc": 5, "toplevel": 1},
    }
    chunk_map = {"facade": ["chunk_a", "chunk_b"]}

    c_graph, c_edges, c_stats = collapse_exec_facades(graph, edge_rows, stats, chunk_map)

    assert set(c_graph) == {"facade", "external"}
    assert c_graph["facade"] == {"external"}
    assert c_graph["external"] == set()
    assert c_edges == [("facade", "external", "thing", True)]
    assert c_stats["facade"] == {"loc": 60, "toplevel": 6}
    assert c_stats["external"] == {"loc": 5, "toplevel": 1}


def test_collapse_exec_facades_does_not_double_count_a_chunk_shared_by_two_facades() -> None:
    """Legacy edge case: two different facades each exec the *same* physical
    file into their own separate globals() (this actually happened at HEAD
    before app.api/app.domain were unified -- see
    `app/domain/__init__.py`'s docstring). Ownership is genuinely ambiguous,
    but the shared file's line count must land in exactly one bucket, not be
    added to both -- otherwise total_loc would inflate every time this
    pattern exists.
    """
    graph = {"facade_a": set(), "facade_b": set(), "shared_chunk": set()}
    edge_rows: list[tuple[str, str, str, bool]] = []
    stats = {
        "facade_a": {"loc": 5, "toplevel": 1},
        "facade_b": {"loc": 7, "toplevel": 1},
        "shared_chunk": {"loc": 100, "toplevel": 10},
    }
    chunk_map = {"facade_a": ["shared_chunk"], "facade_b": ["shared_chunk"]}

    _c_graph, _c_edges, c_stats = collapse_exec_facades(graph, edge_rows, stats, chunk_map)

    total_loc = sum(s["loc"] for s in c_stats.values())
    assert total_loc == 5 + 7 + 100  # not 5 + 7 + 100 + 100


def test_collapse_preserves_total_loc_versus_uncollapsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scripts.arch_graph.ROOT", tmp_path)
    modules = _write_fake_facade_package(tmp_path)
    names = set(modules)

    graph, edge_rows, stats = build_graph(modules, names)
    uncollapsed_total = sum(s["loc"] for s in stats.values())

    chunk_map = exec_facade_chunk_map(modules)
    c_graph, _c_edges, c_stats = collapse_exec_facades(graph, edge_rows, stats, chunk_map)
    collapsed_total = sum(s["loc"] for s in c_stats.values())

    assert collapsed_total == uncollapsed_total
    assert set(c_graph) == {"app.pkg", "app.outside"}  # 4 raw nodes -> 2 collapsed


# ---------------------------------------------------------------------------
# CLI: --no-collapse keeps the original per-file view; default folds
# ---------------------------------------------------------------------------


def test_cli_default_is_collapsed_and_no_collapse_preserves_total_loc() -> None:
    default_run = subprocess.run(
        [sys.executable, "scripts/arch_graph.py", "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    no_collapse_run = subprocess.run(
        [sys.executable, "scripts/arch_graph.py", "--no-collapse", "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    collapsed = json.loads(default_run.stdout)
    uncollapsed = json.loads(no_collapse_run.stdout)

    assert collapsed["collapsed"] is True
    assert uncollapsed["collapsed"] is False
    # Folding regroups nodes; it must never gain or lose source lines.
    assert collapsed["total_loc"] == uncollapsed["total_loc"]
    assert collapsed["modules"] == uncollapsed["modules"]  # raw file count is view-independent
    # The facade-detection step itself is view-independent (it's a code fact,
    # not a rendering choice) -- both views must agree on which packages
    # *could* be folded, even though only the default view actually folds
    # them into the graph.
    assert set(collapsed["collapsed_packages"]) == set(uncollapsed["collapsed_packages"])
    # 这里曾经断言「本仓当前存在 exec 外观」。那是在断言仓库的一个**临时状态**，
    # 而不是工具的行为：2026-08-30 四个 exec 外观（app.api/app.domain/
    # app.worker/app.media_exec，以及中途新增的 app.portraits）全部改成真包之后，
    # 这条断言必然失败——它把「重构成功」判成了「测试挂了」。折叠逻辑本身由本文件
    # 的 tmp_path 合成用例覆盖，不需要真实仓库恰好有外观来证明。
    # 外观数归零是既定目标；下面的不变式在有无外观两种情况下都必须成立。
    # No individual chunk file may survive as its own node in the collapsed
    # view's biggest-modules ranking -- it must show up only as part of its
    # facade.
    facade_prefixes = tuple(f"{name}." for name in collapsed["collapsed_packages"])
    collapsed_modules = {row["module"] for row in collapsed["biggest_modules"]}
    assert not any(m.startswith(facade_prefixes) for m in collapsed_modules)


def test_cli_check_layers_runs_with_and_without_collapse() -> None:
    for extra in ([], ["--no-collapse"]):
        result = subprocess.run(
            [sys.executable, "scripts/arch_graph.py", "--check-layers", "--max-violations", "100000", *extra],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
