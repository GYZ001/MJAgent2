"""Run the smallest useful verification set for the current Git changes.

Usage:
    py scripts/verify.py          # affected checks only
    py scripts/verify.py --plan   # show what would run
    py scripts/verify.py --full   # release/CI-level verification
"""
from __future__ import annotations

import argparse
import ast
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
TESTS = ROOT / "tests"
FRONTEND = ROOT / "frontend"


def _git_lines(*args: str) -> list[str]:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, text=True, capture_output=True, check=True
    )
    return [line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()]


def changed_files() -> list[str]:
    """Return tracked worktree changes plus non-ignored untracked files."""
    paths = _git_lines("diff", "--name-only", "--diff-filter=ACMRD", "HEAD")
    paths += _git_lines("ls-files", "--others", "--exclude-standard")
    return sorted(set(paths))


def _module_name(path: Path) -> str | None:
    try:
        relative = path.relative_to(ROOT).with_suffix("")
    except ValueError:
        return None
    parts = list(relative.parts)
    if not parts or parts[0] not in {"app", "tests"}:
        return None
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _python_files() -> list[Path]:
    return sorted(APP.rglob("*.py")) + sorted(TESTS.glob("test_*.py"))


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeError):
        return None


def _imports_in(path: Path, known_modules: set[str], nodes: list[ast.AST]) -> set[str]:
    """Extract local imports, including ``from app import module`` forms."""

    current = _module_name(path) or ""
    package = current if path.name == "__init__.py" else current.rpartition(".")[0]
    found: set[str] = set()
    for root_node in nodes:
        for node in ast.walk(root_node):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names if alias.name.startswith("app"))
                continue
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level:
                package_parts = package.split(".") if package else []
                trim = node.level - 1
                prefix = package_parts[: len(package_parts) - trim] if trim <= len(package_parts) else []
                base = ".".join(prefix + (node.module.split(".") if node.module else []))
            else:
                base = node.module or ""
            if not base.startswith("app"):
                continue
            found.add(base)
            for alias in node.names:
                candidate = f"{base}.{alias.name}"
                if candidate in known_modules:
                    found.add(candidate)
    return found


def _depends_on(imports: set[str], affected_modules: set[str]) -> bool:
    return any(
        dependency == affected or dependency.startswith(affected + ".")
        for dependency in imports
        for affected in affected_modules
    )


def _runtime_facade_modules(paths: list[str]) -> set[str]:
    """Return compatibility facades that execute implementation slices."""
    facades: set[str] = set()
    for path in paths:
        if path.startswith("app/domain/") and path.endswith(".py"):
            facades.add("app.api")
        if path.startswith("app/media_exec/") and path.endswith(".py"):
            facades.add("app.worker")
    return facades


def affected_python_tests(paths: list[str]) -> list[str]:
    """Find changed tests and tests directly importing a changed app module.

    Direct dependencies keep the edit loop fast. Indirect integration coverage
    intentionally belongs to ``--full`` instead of expanding through a central
    facade such as ``app.api`` and selecting most of the suite.
    """
    python_files = _python_files()
    module_by_file = {path: _module_name(path) for path in python_files}
    known_modules = {module for module in module_by_file.values() if module}

    # ``paths`` comes from a diff filter that includes deletions (see
    # ``changed_files``), on purpose: a deleted app module must still surface
    # its now-orphaned test dependents below (imports break at collection
    # time, so those tests are exactly what should run). A deleted *test*
    # file is different -- pytest is handed this string as a literal target,
    # and a target that doesn't exist on disk makes the whole invocation exit
    # 4 ("file or directory not found") before a single test runs, anywhere.
    # So test paths are filtered to ones still present; app paths are not.
    changed_test_paths = {
        path
        for path in paths
        if path.startswith("tests/test_") and path.endswith(".py") and (ROOT / path).exists()
    }
    changed_app_paths = {
        path for path in paths if path.startswith("app/") and path.endswith(".py")
    }
    affected_modules = {
        _module_name(ROOT / Path(path))
        for path in changed_app_paths
        if _module_name(ROOT / Path(path))
    }
    affected_modules.update(_runtime_facade_modules(list(changed_app_paths)))

    selected = set(changed_test_paths)
    for file_path in python_files:
        relative = file_path.relative_to(ROOT).as_posix()
        if file_path.parent != TESTS or relative in selected:
            continue
        tree = _parse(file_path)
        if tree is None:
            continue
        module_imports = _imports_in(
            file_path,
            known_modules,
            [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))],
        )
        if _depends_on(module_imports, affected_modules):
            selected.add(relative)
            continue
        # A local import usually belongs to one focused regression. Select that
        # node rather than paying for every unrelated test in a large file.
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                if _depends_on(_imports_in(file_path, known_modules, [node]), affected_modules):
                    selected.add(f"{relative}::{node.name}")
    return sorted(selected)


def _npm() -> str:
    return "npm.cmd" if os.name == "nt" else "npm"


def _command_label(command: list[str], cwd: Path) -> str:
    prefix = f"cd {cwd.relative_to(ROOT).as_posix()} && " if cwd != ROOT else ""
    return prefix + " ".join(command)


def _run(
    command: list[str],
    *,
    cwd: Path = ROOT,
    plan: bool = False,
    env: dict[str, str] | None = None,
) -> None:
    print(f"\n> {_command_label(command, cwd)}", flush=True)
    if not plan:
        subprocess.run(command, cwd=cwd, check=True, env=env)


def _isolated_environment(sandbox: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["MANJU_TEST_PROFILE"] = "isolated"
    env["MANJU_TEST_SANDBOX"] = str(sandbox)
    env["HIAGENT_API_KEY"] = ""
    env["OPENROUTER_API_KEY"] = ""
    env["BAILIAN_API_KEY"] = ""
    env["DASHSCOPE_API_KEY"] = ""
    env["DEEPSEEK_API_KEY"] = ""
    env["ZHIPU_API_KEY"] = ""
    env["MINIMAX_H3_API_KEY"] = ""
    env["MINIMAX_H3_BASE_URL"] = ""
    return env


def _live_integration_environment() -> dict[str, str]:
    env = os.environ.copy()
    env["MANJU_TEST_PROFILE"] = "live-integration"
    env.pop("MANJU_TEST_SANDBOX", None)
    return env


_STALE_SANDBOX_MAX_AGE_HOURS = 24.0


def _purge_stale_sandboxes(prefix: str, *, max_age_hours: float = _STALE_SANDBOX_MAX_AGE_HOURS) -> None:
    """Best-effort startup sweep for orphaned ``prefix*`` dirs under /tmp.

    The ``with tempfile.TemporaryDirectory(...)`` block below already removes
    this run's own sandbox on normal completion and on most exceptions
    (including KeyboardInterrupt). Neither that nor any ``finally``/``atexit``
    hook runs when the process is hard-killed (SIGKILL, or the default
    SIGTERM action) -- that is how sandboxes actually accumulated in /tmp.
    This sweep is the backstop: it only ever removes dirs older than
    ``max_age_hours``, so a sandbox still owned by a running process is never
    touched.
    """
    cutoff = time.time() - max_age_hours * 3600
    try:
        entries = list(Path(tempfile.gettempdir()).iterdir())
    except OSError:
        return
    for entry in entries:
        if not entry.name.startswith(prefix):
            continue
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        shutil.rmtree(entry, ignore_errors=True)


def _quick_commands(paths: list[str]) -> list[tuple[list[str], Path]]:
    commands: list[tuple[list[str], Path]] = []
    python_changes = [path for path in paths if path.endswith(".py")]
    lintable = [
        path
        for path in python_changes
        if path.startswith(("app/", "tests/", "scripts/")) and (ROOT / path).exists()
    ]
    if lintable:
        commands.append(([sys.executable, "-m", "ruff", "check", *lintable], ROOT))

    force_all_python = any(
        path in {"pyproject.toml", "requirements.txt", "requirements-dev.txt", "tests/conftest.py"}
        for path in paths
    )
    selected_tests = sorted(path for path in paths if path.startswith("tests/test_") and path.endswith(".py"))
    if force_all_python:
        commands.append(([sys.executable, "-m", "pytest", "-q"], ROOT))
    else:
        selected_tests = affected_python_tests(paths)
        if selected_tests:
            commands.append(([sys.executable, "-m", "pytest", "-q", *selected_tests], ROOT))

    # Keep this set in sync with REUSE_GUARD_ANCHORS in check_contract_surface.py:
    # these files gate whether a historical artifact gets silently reused instead
    # of regenerated, so touching them should also run the (warn-only) version-bump
    # discipline check even outside --full.
    reuse_guard_files = {
        "app/stages.py",
        "app/screenplay_scene_shards.py",
        "app/validators.py",
        "app/production/publish.py",
        "app/production/screenplay_document.py",
        "app/production/screenplay_repair.py",
        "app/production/screenplay_authority.py",
    }
    if (
        any(path.startswith("app/capabilities/") or path == "app/api.py" for path in paths)
        or any(path in reuse_guard_files for path in paths)
    ):
        commands.append(([sys.executable, "scripts/check_contract_surface.py"], ROOT))
    if any(path.startswith("app/capabilities/") or path == "app/api.py" for path in paths):
        commands.append(([sys.executable, "scripts/check_capability_coverage.py"], ROOT))

    if any(p == "frontend/src/index.css" or p.startswith("frontend/src/styles/") for p in paths):
        # 新写的亮色专属色值必须同时给暗色对应值，否则夜间模式会留下白面板。
        commands.append(([sys.executable, "scripts/check_dark_theme.py"], ROOT))
        # 页面样式表只放本页独占的选择器，否则别的页面会掉样式。
        commands.append(([sys.executable, "scripts/check_css_split.py"], ROOT))

    frontend_changes = [path for path in paths if path.startswith("frontend/")]
    if frontend_changes:
        commands.append(([_npm(), "run", "typecheck"], FRONTEND))
        if any(path.startswith("frontend/src/") for path in frontend_changes):
            commands.append(([_npm(), "test"], FRONTEND))
    return commands


def _full_commands(*, live_integration: bool = False) -> list[tuple[list[str], Path]]:
    commands = [
        ([sys.executable, "-m", "ruff", "check", "app", "tests", "scripts"], ROOT),
        ([sys.executable, "scripts/check_contract_surface.py"], ROOT),
        ([sys.executable, "scripts/check_capability_coverage.py"], ROOT),
        ([sys.executable, "scripts/check_dark_theme.py"], ROOT),
        ([sys.executable, "scripts/check_css_split.py"], ROOT),
        ([sys.executable, "-m", "compileall", "-q", "app"], ROOT),
        ([sys.executable, "-m", "pytest", "-q"], ROOT),
        ([_npm(), "run", "build"], FRONTEND),
        ([_npm(), "test"], FRONTEND),
    ]
    if live_integration:
        commands.append(
            (
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "-m",
                    "live_integration",
                    "--live-integration",
                ],
                ROOT,
            )
        )
    return commands


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true", help="run the release/CI-level suite")
    parser.add_argument(
        "--live-integration",
        action="store_true",
        help="after isolated full verification, run explicitly marked live integrations",
    )
    parser.add_argument("--plan", action="store_true", help="print commands without executing them")
    args = parser.parse_args()
    if args.live_integration and not args.full:
        parser.error("--live-integration requires --full")

    started = time.monotonic()
    if args.full:
        commands = _full_commands(live_integration=args.live_integration)
        print("Full verification")
    else:
        paths = changed_files()
        print(f"Quick verification for {len(paths)} changed file(s)")
        for path in paths:
            print(f"  {path}")
        commands = _quick_commands(paths)

    if not commands:
        print("No code changes need verification.")
        return 0
    _purge_stale_sandboxes("manju-verify-")
    with tempfile.TemporaryDirectory(prefix="manju-verify-") as sandbox_dir:
        isolated_env = _isolated_environment(Path(sandbox_dir))
        live_env = _live_integration_environment()
        try:
            for command, cwd in commands:
                command_env = (
                    live_env
                    if "--live-integration" in command
                    else isolated_env
                )
                _run(command, cwd=cwd, plan=args.plan, env=command_env)
        except subprocess.CalledProcessError as exc:
            print(f"\nFAILED (exit {exc.returncode}) in {time.monotonic() - started:.1f}s")
            return exc.returncode
    suffix = " plan" if args.plan else ""
    print(f"\nOK{suffix} in {time.monotonic() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
