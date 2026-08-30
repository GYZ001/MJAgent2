"""Tests for `scripts/arch_graph.py --check-layers`.

docs/architecture_layering_plan_2026-08-29.md 2.2 turns the six-layer table
into an executable gate: LAYERS.toml declares layer numbers, and any import
edge from a lower layer into a higher layer is a violation. These tests build
fake modules under tmp_path (real files, since `parse_edges` reads real
source via AST) so the assertions do not drift with the real app/ tree, which
two other agents are editing concurrently.
"""
from __future__ import annotations

import datetime as dt
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from scripts.arch_graph import (
    DEFAULT_LAYERS_FILE,
    ROOT,
    LayerException,
    LayersConfig,
    LayersConfigError,
    find_layer_violations,
    load_layers_config,
    resolve_layer,
    run_check_layers,
)


def _write(tmp_path: Path, rel: str, content: str) -> Path:
    path = tmp_path / rel
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# resolve_layer: longest-prefix matching, dot-boundary safe
# ---------------------------------------------------------------------------


def test_resolve_layer_prefers_the_most_specific_declared_key() -> None:
    layers = {"app.production": 4, "app.production.screenplay_repair": 2}

    assert resolve_layer("app.production.certificate", layers) == (4, "app.production")
    assert resolve_layer("app.production.screenplay_repair", layers) == (
        2,
        "app.production.screenplay_repair",
    )


def test_resolve_layer_package_prefix_covers_undeclared_submodules() -> None:
    layers = {"app.production": 4}

    assert resolve_layer("app.production.grant", layers) == (4, "app.production")


def test_resolve_layer_does_not_confuse_sibling_modules_by_string_prefix() -> None:
    # app.narrative_blueprint is a sibling of app.narrative, not nested under
    # it. A naive `str.startswith` prefix check would wrongly match; dot-based
    # prefix walking must not.
    layers = {"app.narrative": 4}

    layer, matched = resolve_layer("app.narrative_blueprint", layers)

    assert matched is None
    assert layer == 5  # UNDECLARED_LAYER fail-safe default


def test_resolve_layer_undeclared_module_is_fail_safe_l5() -> None:
    layer, matched = resolve_layer("app.totally_unknown", {"app.config": 1})

    assert layer == 5
    assert matched is None


# ---------------------------------------------------------------------------
# find_layer_violations: the core AST-driven judgement
# ---------------------------------------------------------------------------


def test_upward_edge_is_flagged_as_a_violation(tmp_path: Path) -> None:
    low = _write(tmp_path, "low.py", "import app.fake_high\n")
    high = _write(tmp_path, "high.py", "VALUE = 1\n")
    modules = {"app.fake_low": low, "app.fake_high": high}
    config = LayersConfig(
        layers={"app.fake_low": 1, "app.fake_high": 5}, max_violations=0, exceptions=[]
    )

    violations, undeclared, expired = find_layer_violations(modules, config)

    assert len(violations) == 1
    v = violations[0]
    assert (v.source, v.target) == ("app.fake_low", "app.fake_high")
    assert (v.source_layer, v.target_layer) == (1, 5)
    assert v.lazy is False
    assert undeclared == []
    assert expired == []


def test_undeclared_target_defaults_to_l5_and_is_listed(tmp_path: Path) -> None:
    low = _write(tmp_path, "low.py", "import app.fake_high\n")
    high = _write(tmp_path, "high.py", "VALUE = 1\n")
    modules = {"app.fake_low": low, "app.fake_high": high}
    # app.fake_high has no declared layer at all.
    config = LayersConfig(layers={"app.fake_low": 1}, max_violations=0, exceptions=[])

    violations, undeclared, _expired = find_layer_violations(modules, config)

    assert undeclared == ["app.fake_high"]
    assert len(violations) == 1
    assert violations[0].target_layer == 5


def test_downward_edge_is_legal(tmp_path: Path) -> None:
    hi = _write(tmp_path, "hi.py", "import app.fake_lo\n")
    lo = _write(tmp_path, "lo.py", "VALUE = 1\n")
    modules = {"app.fake_hi": hi, "app.fake_lo": lo}
    config = LayersConfig(
        layers={"app.fake_hi": 4, "app.fake_lo": 1}, max_violations=0, exceptions=[]
    )

    violations, _undeclared, _expired = find_layer_violations(modules, config)

    assert violations == []


def test_same_layer_edge_is_legal(tmp_path: Path) -> None:
    a = _write(tmp_path, "a.py", "import app.fake_peer_b\n")
    b = _write(tmp_path, "b.py", "VALUE = 1\n")
    modules = {"app.fake_peer_a": a, "app.fake_peer_b": b}
    config = LayersConfig(
        layers={"app.fake_peer_a": 3, "app.fake_peer_b": 3}, max_violations=0, exceptions=[]
    )

    violations, _undeclared, _expired = find_layer_violations(modules, config)

    assert violations == []


def test_deferred_import_violation_is_tagged_lazy(tmp_path: Path) -> None:
    low = _write(
        tmp_path,
        "low.py",
        "def f():\n    import app.fake_high\n    return app.fake_high\n",
    )
    high = _write(tmp_path, "high.py", "VALUE = 1\n")
    modules = {"app.fake_low": low, "app.fake_high": high}
    config = LayersConfig(
        layers={"app.fake_low": 1, "app.fake_high": 5}, max_violations=0, exceptions=[]
    )

    violations, _undeclared, _expired = find_layer_violations(modules, config)

    assert len(violations) == 1
    assert violations[0].lazy is True


def test_module_level_and_deferred_violations_are_counted_separately(
    tmp_path: Path,
) -> None:
    low = _write(
        tmp_path,
        "low.py",
        "import app.fake_high\n\n\ndef f():\n    import app.fake_high\n",
    )
    high = _write(tmp_path, "high.py", "VALUE = 1\n")
    modules = {"app.fake_low": low, "app.fake_high": high}
    config = LayersConfig(
        layers={"app.fake_low": 1, "app.fake_high": 5}, max_violations=0, exceptions=[]
    )

    violations, _undeclared, _expired = find_layer_violations(modules, config)

    lazy_flags = sorted(v.lazy for v in violations)
    assert lazy_flags == [False, True]


def test_active_exception_suppresses_the_violation(tmp_path: Path) -> None:
    low = _write(tmp_path, "low.py", "import app.fake_high\n")
    high = _write(tmp_path, "high.py", "VALUE = 1\n")
    modules = {"app.fake_low": low, "app.fake_high": high}
    future = dt.date.today() + dt.timedelta(days=30)
    config = LayersConfig(
        layers={"app.fake_low": 1, "app.fake_high": 5},
        max_violations=0,
        exceptions=[LayerException("app.fake_low", "app.fake_high", "temporary bridge", future)],
    )

    violations, _undeclared, expired = find_layer_violations(modules, config)

    assert violations == []
    assert expired == []


def test_expired_exception_still_reports_as_a_violation(tmp_path: Path) -> None:
    low = _write(tmp_path, "low.py", "import app.fake_high\n")
    high = _write(tmp_path, "high.py", "VALUE = 1\n")
    modules = {"app.fake_low": low, "app.fake_high": high}
    config = LayersConfig(
        layers={"app.fake_low": 1, "app.fake_high": 5},
        max_violations=0,
        exceptions=[
            LayerException("app.fake_low", "app.fake_high", "temporary bridge", dt.date(2000, 1, 1))
        ],
    )

    violations, _undeclared, expired = find_layer_violations(modules, config)

    assert len(violations) == 1
    assert len(expired) == 1
    assert expired[0].reason == "temporary bridge"


def test_exec_facade_chunk_inherits_the_facades_layer_even_with_a_stale_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chunk files exec()'d into a shared facade namespace (app.domain,
    app.media_exec, app.portraits) are the same runtime module as their
    facade. If LAYERS.toml ever declared a different layer for one specific
    chunk (stale override, or someone assuming the old per-file semantics),
    the folded judgement must ignore it and use the facade's own layer --
    there is no second namespace at runtime for that override to mean
    anything.
    """
    monkeypatch.setattr("scripts.arch_graph.ROOT", tmp_path)
    pkg_dir = tmp_path / "app" / "pkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text(
        "from pathlib import Path as _P\n"
        "_BASE = _P(__file__).resolve().parent\n"
        "_ODD_NAME_123 = ('a.py',)\n"
        "for _f in _ODD_NAME_123:\n"
        "    _fp = _BASE / _f\n"
        "    exec(compile(_fp.read_text(encoding='utf-8'), str(_fp), 'exec'), globals())\n",
        encoding="utf-8",
    )
    (pkg_dir / "a.py").write_text("import app.outside\nVALUE = 1\n", encoding="utf-8")
    outside = tmp_path / "app" / "outside.py"
    outside.write_text("VALUE = 1\n", encoding="utf-8")

    modules = {
        "app.pkg": pkg_dir / "__init__.py",
        "app.pkg.a": pkg_dir / "a.py",
        "app.outside": outside,
    }
    # app.pkg (the facade) is L1 -- but someone declared app.pkg.a itself as
    # L5, presumably before the exec-facade split, or by mistake. Under plain
    # longest-prefix resolution this exact-match override would win and hide
    # a real upward edge (L5 -> L5 read as same-layer, legal). Folded
    # judgement must use the facade's L1 instead, exposing the violation.
    config = LayersConfig(
        layers={"app.pkg": 1, "app.pkg.a": 5, "app.outside": 5}, max_violations=0, exceptions=[]
    )

    violations, _undeclared, _expired = find_layer_violations(modules, config)

    assert len(violations) == 1
    v = violations[0]
    assert (v.source, v.target) == ("app.pkg", "app.outside")
    assert (v.source_layer, v.target_layer) == (1, 5)


def test_exec_facade_internal_edge_is_folded_away_not_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chunk importing another chunk of the *same* facade (the "except
    NameError: from app.domain.common import *" fallback guard pattern) is
    an internal, folded-away edge at runtime -- it must not show up as a
    violation (nor as a legal same-layer edge counted anywhere), because
    after folding source == target.
    """
    monkeypatch.setattr("scripts.arch_graph.ROOT", tmp_path)
    pkg_dir = tmp_path / "app" / "pkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text(
        "from pathlib import Path as _P\n"
        "_BASE = _P(__file__).resolve().parent\n"
        "_CHUNKS = ('a.py', 'b.py')\n"
        "for _f in _CHUNKS:\n"
        "    _fp = _BASE / _f\n"
        "    exec(compile(_fp.read_text(encoding='utf-8'), str(_fp), 'exec'), globals())\n",
        encoding="utf-8",
    )
    (pkg_dir / "a.py").write_text("VALUE_A = 1\n", encoding="utf-8")
    (pkg_dir / "b.py").write_text(
        "try:\n    VALUE_A\nexcept NameError:\n    from app.pkg.a import VALUE_A\n",
        encoding="utf-8",
    )

    modules = {
        "app.pkg": pkg_dir / "__init__.py",
        "app.pkg.a": pkg_dir / "a.py",
        "app.pkg.b": pkg_dir / "b.py",
    }
    # Deliberately declare the two chunks at *different* layers -- if the
    # internal edge were not folded away, this would be flagged (b's L5 -> a's
    # L1 is downward and legal, but the point is it must not even be counted
    # as an edge at all once source and target canonicalize to the same node).
    config = LayersConfig(layers={"app.pkg": 1}, max_violations=0, exceptions=[])

    violations, _undeclared, _expired = find_layer_violations(modules, config)

    assert violations == []


# ---------------------------------------------------------------------------
# load_layers_config: fail loudly on malformed configuration
# ---------------------------------------------------------------------------


def test_load_layers_config_rejects_exception_missing_reason(tmp_path: Path) -> None:
    toml_path = tmp_path / "LAYERS.toml"
    toml_path.write_text(
        """
max_violations = 0

[layers]
"app.x" = 0

[[allowed_exceptions]]
from = "app.a"
to = "app.b"
expires = "2099-01-01"
""",
        encoding="utf-8",
    )

    with pytest.raises(LayersConfigError, match="reason"):
        load_layers_config(toml_path)


def test_load_layers_config_rejects_exception_missing_expires(tmp_path: Path) -> None:
    toml_path = tmp_path / "LAYERS.toml"
    toml_path.write_text(
        """
max_violations = 0

[layers]
"app.x" = 0

[[allowed_exceptions]]
from = "app.a"
to = "app.b"
reason = "temporary"
""",
        encoding="utf-8",
    )

    with pytest.raises(LayersConfigError, match="expires"):
        load_layers_config(toml_path)


def test_load_layers_config_rejects_unparseable_expires_date(tmp_path: Path) -> None:
    toml_path = tmp_path / "LAYERS.toml"
    toml_path.write_text(
        """
max_violations = 0

[layers]
"app.x" = 0

[[allowed_exceptions]]
from = "app.a"
to = "app.b"
reason = "temporary"
expires = "not-a-date"
""",
        encoding="utf-8",
    )

    with pytest.raises(LayersConfigError, match="expires"):
        load_layers_config(toml_path)


def test_load_layers_config_rejects_out_of_range_layer_number(tmp_path: Path) -> None:
    toml_path = tmp_path / "LAYERS.toml"
    toml_path.write_text('max_violations = 0\n\n[layers]\n"app.x" = 6\n', encoding="utf-8")

    with pytest.raises(LayersConfigError):
        load_layers_config(toml_path)


def test_load_layers_config_rejects_negative_max_violations(tmp_path: Path) -> None:
    toml_path = tmp_path / "LAYERS.toml"
    toml_path.write_text('max_violations = -1\n\n[layers]\n"app.x" = 0\n', encoding="utf-8")

    with pytest.raises(LayersConfigError):
        load_layers_config(toml_path)


def test_load_layers_config_accepts_a_well_formed_exception(tmp_path: Path) -> None:
    toml_path = tmp_path / "LAYERS.toml"
    toml_path.write_text(
        """
max_violations = 3

[layers]
"app.a" = 0
"app.b" = 5

[[allowed_exceptions]]
from = "app.a"
to = "app.b"
reason = "temporary bridge while migrating"
expires = "2099-01-01"
""",
        encoding="utf-8",
    )

    config = load_layers_config(toml_path)

    assert config.max_violations == 3
    assert config.layers == {"app.a": 0, "app.b": 5}
    assert len(config.exceptions) == 1
    assert config.exceptions[0].reason == "temporary bridge while migrating"


def test_load_layers_config_missing_file_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(LayersConfigError):
        load_layers_config(tmp_path / "does-not-exist.toml")


# ---------------------------------------------------------------------------
# run_check_layers: CLI-level report + exit code, with collect_modules faked
# ---------------------------------------------------------------------------


def test_run_check_layers_returns_2_on_bad_config(tmp_path: Path, capsys) -> None:
    bad_toml = tmp_path / "LAYERS.toml"
    bad_toml.write_text('max_violations = 0\n\n[layers]\n"app.x" = 9\n', encoding="utf-8")

    exit_code = run_check_layers(bad_toml, None, 12)

    assert exit_code == 2
    assert "配置错误" in capsys.readouterr().err


def test_run_check_layers_strict_mode_fails_on_any_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    low = _write(tmp_path, "low.py", "import app.fake_high\n")
    high = _write(tmp_path, "high.py", "VALUE = 1\n")
    monkeypatch.setattr(
        "scripts.arch_graph.collect_modules",
        lambda: {"app.fake_low": low, "app.fake_high": high},
    )
    toml_path = tmp_path / "LAYERS.toml"
    toml_path.write_text(
        'max_violations = 999\n\n[layers]\n"app.fake_low" = 1\n"app.fake_high" = 5\n',
        encoding="utf-8",
    )

    exit_code = run_check_layers(toml_path, None, 12)  # no --max-violations: strict

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "层级违规（上行边）共 1 条" in out
    assert "app.fake_low(L1) -> app.fake_high(L5)" in out


def test_run_check_layers_threshold_mode_passes_within_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    low = _write(tmp_path, "low.py", "import app.fake_high\n")
    high = _write(tmp_path, "high.py", "VALUE = 1\n")
    monkeypatch.setattr(
        "scripts.arch_graph.collect_modules",
        lambda: {"app.fake_low": low, "app.fake_high": high},
    )
    toml_path = tmp_path / "LAYERS.toml"
    toml_path.write_text(
        'max_violations = 5\n\n[layers]\n"app.fake_low" = 1\n"app.fake_high" = 5\n',
        encoding="utf-8",
    )

    exit_code = run_check_layers(toml_path, 5, 12)

    assert exit_code == 0
    assert "OK: 违规数 1" in capsys.readouterr().out


def test_run_check_layers_threshold_mode_fails_once_budget_exceeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    low = _write(tmp_path, "low.py", "import app.fake_high\n")
    high = _write(tmp_path, "high.py", "VALUE = 1\n")
    monkeypatch.setattr(
        "scripts.arch_graph.collect_modules",
        lambda: {"app.fake_low": low, "app.fake_high": high},
    )
    toml_path = tmp_path / "LAYERS.toml"
    toml_path.write_text(
        'max_violations = 0\n\n[layers]\n"app.fake_low" = 1\n"app.fake_high" = 5\n',
        encoding="utf-8",
    )

    exit_code = run_check_layers(toml_path, 0, 12)

    assert exit_code == 1
    assert "FAIL" in capsys.readouterr().out


def test_run_check_layers_lists_undeclared_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    low = _write(tmp_path, "low.py", "VALUE = 1\n")
    monkeypatch.setattr("scripts.arch_graph.collect_modules", lambda: {"app.fake_low": low})
    toml_path = tmp_path / "LAYERS.toml"
    toml_path.write_text('max_violations = 0\n\n[layers]\n"app.other" = 0\n', encoding="utf-8")

    exit_code = run_check_layers(toml_path, None, 12)

    out = capsys.readouterr().out
    assert exit_code == 0  # no import edges at all -> no violations, just a reminder
    assert "未声明层号的模块 1 个" in out
    assert "app.fake_low" in out


# ---------------------------------------------------------------------------
# Real-repo integration: config loads cleanly, CLI wiring untouched
# ---------------------------------------------------------------------------


def test_real_layers_toml_loads_without_config_errors() -> None:
    config = load_layers_config(DEFAULT_LAYERS_FILE)

    assert config.max_violations >= 0
    assert config.layers["app.schemas"] == 0
    assert config.layers["app.db"] == 2


def test_check_layers_subprocess_passes_with_configured_threshold() -> None:
    with DEFAULT_LAYERS_FILE.open("rb") as fh:
        threshold = tomllib.load(fh)["max_violations"]

    result = subprocess.run(
        [sys.executable, "scripts/arch_graph.py", "--check-layers", "--max-violations", str(threshold)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


def test_check_layers_bare_invocation_is_strict_mode_against_real_repo() -> None:
    # The repo currently has known upward edges (see
    # docs/architecture_layering_plan_2026-08-29.md 2.2), so strict mode
    # (no --max-violations) must be non-zero today. This pins the *mode's*
    # behavior, not a specific count.
    result = subprocess.run(
        [sys.executable, "scripts/arch_graph.py", "--check-layers"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "FAIL" in result.stdout


def test_default_summary_output_is_unaffected_by_check_layers_flag() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/arch_graph.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "最大强连通分量" in result.stdout
    assert "层级违规" not in result.stdout
