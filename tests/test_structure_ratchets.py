"""结构棘轮闸门测试：app/ 根目录散文件基线 + 函数体内 app.* 延迟导入无说明基线。

两条判据都来自 CLAUDE.md「Code Architecture Norms」两条结构红线（此前无闸门守着，
纯靠自觉）：
    1. app/ 根目录不再新增散文件——新模块必须进包。
    2. 函数体内 `import app.*` 必须带一行说明注释，为什么这里不能模块级导入。

判据细节见 scripts/check_structure_ratchets.py 模块 docstring；这里用 tmp_path 造
真实文件（度量函数读真文件），不依赖真实 app/ 树——那棵树正被其它并行 agent 改动。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.check_structure_ratchets import (
    BASELINE_FILE,
    ROOT,
    RatchetConfig,
    StructureRatchetsConfigError,
    collect_app_files,
    collect_root_files,
    evaluate_lazy_imports,
    evaluate_root_files,
    load_config,
    run_check,
    run_seed,
    scan_lazy_imports,
)


def _write(tmp_path: Path, rel: str, content: str) -> Path:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _config(root_files: set[str] = frozenset(), lazy_baseline: dict[str, int] | None = None) -> RatchetConfig:
    return RatchetConfig(root_files=frozenset(root_files), lazy_import_baseline=dict(lazy_baseline or {}))


# ---------------------------------------------------------------------------
# app/ 根目录散文件棘轮
# ---------------------------------------------------------------------------


def test_root_new_file_not_in_baseline_is_a_violation(tmp_path: Path) -> None:
    _write(tmp_path, "app/a.py", "VALUE = 1\n")
    _write(tmp_path, "app/b.py", "VALUE = 2\n")
    config = _config(root_files={"app/a.py"})

    report = evaluate_root_files(collect_root_files(tmp_path / "app"), config, tmp_path)

    assert report.new == ["app/b.py"]
    assert report.missing == []
    assert report.is_clean is False


def test_root_baseline_file_missing_is_a_violation(tmp_path: Path) -> None:
    _write(tmp_path, "app/a.py", "VALUE = 1\n")
    config = _config(root_files={"app/a.py", "app/gone.py"})

    report = evaluate_root_files(collect_root_files(tmp_path / "app"), config, tmp_path)

    assert report.new == []
    assert report.missing == ["app/gone.py"]
    assert report.is_clean is False


def test_root_files_matching_baseline_exactly_is_clean(tmp_path: Path) -> None:
    _write(tmp_path, "app/a.py", "VALUE = 1\n")
    _write(tmp_path, "app/sub/b.py", "VALUE = 2\n")  # 子包，不计入根目录
    config = _config(root_files={"app/a.py"})

    report = evaluate_root_files(collect_root_files(tmp_path / "app"), config, tmp_path)

    assert report.is_clean is True


def test_collect_root_files_excludes_subpackages(tmp_path: Path) -> None:
    _write(tmp_path, "app/a.py", "VALUE = 1\n")
    _write(tmp_path, "app/sub/b.py", "VALUE = 2\n")

    files = collect_root_files(tmp_path / "app")

    assert [p.relative_to(tmp_path).as_posix() for p in files] == ["app/a.py"]


# ---------------------------------------------------------------------------
# scan_lazy_imports：函数体内 app.* 导入 + 「带说明」判据
# ---------------------------------------------------------------------------


def test_module_top_level_import_is_not_counted(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "m.py",
        "import app.foo\n"
        "from app.bar import baz\n"
        "\n"
        "def f():\n"
        "    return baz\n",
    )

    assert scan_lazy_imports(path) == []


def test_function_body_import_without_comment_is_flagged(tmp_path: Path) -> None:
    path = _write(tmp_path, "m.py", "def f():\n    import app.foo\n    return app.foo\n")

    assert scan_lazy_imports(path) == [2]


def test_trailing_comment_counts_as_documented(tmp_path: Path) -> None:
    path = _write(
        tmp_path, "m.py", "def f():\n    import app.foo  # 避免循环依赖\n    return app.foo\n"
    )

    assert scan_lazy_imports(path) == []


def test_comment_on_previous_line_counts_as_documented(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "m.py",
        "def f():\n"
        "    # 避免循环依赖，这里不能模块级导入\n"
        "    import app.foo\n"
        "    return app.foo\n",
    )

    assert scan_lazy_imports(path) == []


def test_multiline_comment_block_above_counts_as_documented(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "m.py",
        "def f():\n"
        "    # 第一行说明\n"
        "    # 第二行说明——紧邻导入语句的是这一行\n"
        "    import app.foo\n"
        "    return app.foo\n",
    )

    assert scan_lazy_imports(path) == []


def test_blank_line_between_comment_and_import_does_not_count(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "m.py",
        "def f():\n"
        "    # 说明\n"
        "\n"
        "    import app.foo\n"
        "    return app.foo\n",
    )

    assert scan_lazy_imports(path) == [4]


def test_nested_block_inside_function_is_counted(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "m.py",
        "def f(flag):\n"
        "    if flag:\n"
        "        for _ in range(1):\n"
        "            import app.foo\n"
        "    return flag\n",
    )

    assert scan_lazy_imports(path) == [4]


def test_module_level_try_except_import_is_not_counted(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "m.py",
        "try:\n    import app.foo\nexcept ImportError:\n    app = None\n",
    )

    assert scan_lazy_imports(path) == []


def test_class_body_import_outside_method_is_not_counted(tmp_path: Path) -> None:
    path = _write(tmp_path, "m.py", "class C:\n    import app.foo\n")

    assert scan_lazy_imports(path) == []


def test_method_body_import_is_counted(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "m.py",
        "class C:\n    def f(self):\n        import app.foo\n        return app.foo\n",
    )

    assert scan_lazy_imports(path) == [3]


def test_relative_import_bare_dot_is_counted(tmp_path: Path) -> None:
    path = _write(tmp_path, "m.py", "def f():\n    from . import sibling\n    return sibling\n")

    assert scan_lazy_imports(path) == [2]


def test_relative_import_dot_module_is_counted(tmp_path: Path) -> None:
    path = _write(tmp_path, "m.py", "def f():\n    from .sub import thing\n    return thing\n")

    assert scan_lazy_imports(path) == [2]


def test_from_app_import_without_comment_is_flagged(tmp_path: Path) -> None:
    path = _write(tmp_path, "m.py", "def f():\n    from app.bar import baz\n    return baz\n")

    assert scan_lazy_imports(path) == [2]


def test_non_app_lazy_import_is_not_counted(tmp_path: Path) -> None:
    path = _write(tmp_path, "m.py", "def f():\n    import os\n    return os\n")

    assert scan_lazy_imports(path) == []


def test_multiline_import_statement_checks_first_line_only(tmp_path: Path) -> None:
    """已知简化：多行 import 只看首行——见 scan_lazy_imports 的 docstring。"""
    path = _write(
        tmp_path,
        "m.py",
        "def f():\n"
        "    from app.bar import (\n"
        "        baz,\n"
        "    )  # 这条注释打在收尾括号行，不算首行说明\n"
        "    return baz\n",
    )

    assert scan_lazy_imports(path) == [2]


def test_multiple_lazy_imports_are_each_reported(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "m.py",
        "def f():\n    import app.a\n    import app.b  # 有说明\n    import app.c\n    return 1\n",
    )

    assert scan_lazy_imports(path) == [2, 4]


def test_syntax_error_file_returns_none(tmp_path: Path) -> None:
    path = _write(tmp_path, "broken.py", "def f(:\n    pass\n")

    assert scan_lazy_imports(path) is None


# ---------------------------------------------------------------------------
# evaluate_lazy_imports：按文件基线判定
# ---------------------------------------------------------------------------


def test_new_file_not_in_lazy_baseline_with_one_undocumented_is_flagged(tmp_path: Path) -> None:
    path = _write(tmp_path, "app/m.py", "def f():\n    import app.foo\n    return app.foo\n")
    config = _config()

    report = evaluate_lazy_imports([path], config, tmp_path)

    assert len(report.violations) == 1
    v = report.violations[0]
    assert (v.path, v.actual, v.threshold, v.lines) == ("app/m.py", 1, 0, [2])
    assert report.is_clean is False


def test_file_within_its_baseline_is_clean(tmp_path: Path) -> None:
    path = _write(tmp_path, "app/m.py", "def f():\n    import app.foo\n    return app.foo\n")
    config = _config(lazy_baseline={"app/m.py": 1})

    report = evaluate_lazy_imports([path], config, tmp_path)

    assert report.violations == []
    assert report.is_clean is True


def test_file_exceeding_its_baseline_is_flagged_with_line_numbers(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "app/m.py",
        "def f():\n    import app.a\n    import app.b\n    import app.c\n    return 1\n",
    )
    config = _config(lazy_baseline={"app/m.py": 1})

    report = evaluate_lazy_imports([path], config, tmp_path)

    assert len(report.violations) == 1
    v = report.violations[0]
    assert v.actual == 3
    assert v.threshold == 1
    assert v.lines == [2, 3, 4]


def test_file_below_its_baseline_is_reported_as_tightenable_not_a_failure(tmp_path: Path) -> None:
    path = _write(tmp_path, "app/m.py", "def f():\n    import app.foo\n    return app.foo\n")
    config = _config(lazy_baseline={"app/m.py": 5})

    report = evaluate_lazy_imports([path], config, tmp_path)

    assert report.violations == []
    assert report.below_baseline == ["app/m.py"]
    assert report.is_clean is True


def test_unparseable_file_is_skipped_not_a_violation(tmp_path: Path) -> None:
    path = _write(tmp_path, "app/broken.py", "def f(:\n    pass\n")
    config = _config()

    report = evaluate_lazy_imports([path], config, tmp_path)

    assert report.violations == []
    assert report.unparseable == ["app/broken.py"]


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------


def test_load_config_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(StructureRatchetsConfigError):
        load_config(tmp_path / "missing.toml")


def test_load_config_rejects_malformed_toml(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("this is not [valid toml\n", encoding="utf-8")

    with pytest.raises(StructureRatchetsConfigError):
        load_config(bad)


def test_load_config_parses_well_formed_baseline(tmp_path: Path) -> None:
    toml_path = tmp_path / "STRUCTURE_RATCHETS.toml"
    toml_path.write_text(
        '[root_files]\n'
        'files = ["app/a.py", "app/b.py"]\n'
        '\n'
        '[lazy_import_undocumented]\n'
        '"app/a.py" = 2\n',
        encoding="utf-8",
    )

    config = load_config(toml_path)

    assert config.root_files == frozenset({"app/a.py", "app/b.py"})
    assert config.lazy_import_baseline == {"app/a.py": 2}


def test_load_config_rejects_negative_lazy_baseline_value(tmp_path: Path) -> None:
    toml_path = tmp_path / "bad.toml"
    toml_path.write_text(
        '[root_files]\nfiles = []\n\n[lazy_import_undocumented]\n"app/a.py" = -1\n',
        encoding="utf-8",
    )

    with pytest.raises(StructureRatchetsConfigError):
        load_config(toml_path)


def test_load_config_rejects_non_string_root_files_entries(tmp_path: Path) -> None:
    toml_path = tmp_path / "bad.toml"
    toml_path.write_text(
        "[root_files]\nfiles = [1, 2]\n\n[lazy_import_undocumented]\n", encoding="utf-8"
    )

    with pytest.raises(StructureRatchetsConfigError):
        load_config(toml_path)


# ---------------------------------------------------------------------------
# CLI：run_check / run_seed
# ---------------------------------------------------------------------------


def test_run_check_returns_0_when_clean(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    app_dir = tmp_path / "app"
    _write(tmp_path, "app/a.py", "VALUE = 1\n")
    toml_path = tmp_path / "STRUCTURE_RATCHETS.toml"
    toml_path.write_text(
        '[root_files]\nfiles = ["app/a.py"]\n\n[lazy_import_undocumented]\n', encoding="utf-8"
    )

    exit_code = run_check(toml_path, root=tmp_path, app_dir=app_dir)

    assert exit_code == 0
    assert "OK" in capsys.readouterr().out


def test_run_check_returns_1_and_lists_line_numbers_on_violation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    app_dir = tmp_path / "app"
    _write(tmp_path, "app/a.py", "def f():\n    import app.foo\n    return app.foo\n")
    toml_path = tmp_path / "STRUCTURE_RATCHETS.toml"
    toml_path.write_text(
        '[root_files]\nfiles = ["app/a.py"]\n\n[lazy_import_undocumented]\n', encoding="utf-8"
    )

    exit_code = run_check(toml_path, root=tmp_path, app_dir=app_dir)

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL" in out
    assert "app/a.py" in out
    assert "2" in out


def test_run_check_returns_2_on_missing_config(tmp_path: Path) -> None:
    exit_code = run_check(tmp_path / "missing.toml", root=tmp_path, app_dir=tmp_path / "app")

    assert exit_code == 2


def test_run_seed_writes_a_loadable_baseline_matching_current_state(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    _write(tmp_path, "app/a.py", "VALUE = 1\n")
    _write(tmp_path, "app/b.py", "def f():\n    import app.foo\n    return app.foo\n")
    toml_path = tmp_path / "STRUCTURE_RATCHETS.toml"

    run_seed(toml_path, root=tmp_path, app_dir=app_dir)
    config = load_config(toml_path)

    assert config.root_files == frozenset({"app/a.py", "app/b.py"})
    assert config.lazy_import_baseline == {"app/b.py": 1}

    # 播种完立刻用同一批文件跑检查必须是干净的——这是棘轮「当下状态即合法基线」
    # 的定义性质，不是附带效果。
    report_root = evaluate_root_files(collect_root_files(app_dir), config, tmp_path)
    report_lazy = evaluate_lazy_imports(collect_app_files(app_dir), config, tmp_path)
    assert report_root.is_clean is True
    assert report_lazy.is_clean is True


# ---------------------------------------------------------------------------
# 对真实仓库配置的冒烟测试：只断言「不崩」，不断言「干净」——app/ 树正被其它并行
# agent 改动，断言固定 pass/fail 会在无关改动上 flake（同
# test_check_file_conventions.py::test_real_repo_check_runs_cleanly 的取舍）。
# ---------------------------------------------------------------------------


def test_real_repo_config_loads_without_crashing() -> None:
    config = load_config(BASELINE_FILE)

    assert isinstance(config.root_files, frozenset)
    assert isinstance(config.lazy_import_baseline, dict)


def test_real_repo_check_runs_without_crashing() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/check_structure_ratchets.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode in (0, 1), result.stdout + result.stderr
    assert "配置错误" not in result.stderr
