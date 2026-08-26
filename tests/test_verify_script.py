from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from scripts.verify import (
    ROOT,
    _full_commands,
    _isolated_environment,
    _quick_commands,
    _run,
    _runtime_facade_modules,
    affected_python_tests,
)


def test_runtime_facades_cover_exec_loaded_implementation_slices() -> None:
    assert _runtime_facade_modules([
        "app/media_exec/run_job.py",
        "app/domain/video_ops.py",
        "app/delivery.py",
    ]) == {"app.worker", "app.api"}


def test_media_exec_change_selects_worker_regressions() -> None:
    selected = affected_python_tests(["app/media_exec/run_job.py"])

    assert "tests/test_media_pipeline_v2.py" in selected
    assert "tests/test_media_job_recovery.py" in selected


def test_deleted_test_file_is_excluded_from_pytest_targets() -> None:
    """Reproduces a real incident: a bulk deletion put a gone test file into
    ``git diff --diff-filter=ACMRD`` output (``D`` is in the filter on purpose,
    see the next test), and that path used to be handed straight to pytest as
    a literal target. pytest then exits 4 ("file or directory not found")
    before a single test runs -- not even the surviving tests execute.

    A deleted test file has nothing left to select: it must be dropped, not
    substituted with anything.
    """
    ghost_test = "tests/test_this_file_was_deleted_in_bulk_cleanup.py"
    assert not (ROOT / ghost_test).exists()  # sanity: genuinely absent from disk

    selected = affected_python_tests([ghost_test])

    assert ghost_test not in selected


def test_deleted_source_file_still_selects_its_dependents() -> None:
    """The other half of the same fix: deleting *app* code (as opposed to a
    test) must still surface the tests that import it, because deleting
    source is exactly what breaks imports elsewhere. ``affected_python_tests``
    resolves module dependents by import scanning, not by checking whether
    the changed app path still exists on disk -- so a path that never existed
    stands in fine for "just deleted" here.
    """
    selected = affected_python_tests([
        "tests/test_a_bulk_deleted_test_file.py",  # deleted test: must drop out
        "app/media_exec/run_job.py",  # deleted source: dependents must survive
    ])

    assert "tests/test_a_bulk_deleted_test_file.py" not in selected
    assert "tests/test_media_pipeline_v2.py" in selected
    assert "tests/test_media_job_recovery.py" in selected


def test_quick_commands_pytest_target_list_is_collectible() -> None:
    """End-to-end proof, not just unit-level: the exact pytest command line
    ``_quick_commands`` would hand to ``subprocess.run`` in the real deletion
    scenario must not exit 4. ``--collect-only`` keeps this fast while still
    exercising real pytest argument parsing against the real filesystem,
    which is where the original bug actually bit (a unit test of the
    selection logic alone would not have caught the crash).
    """
    paths = [
        "tests/test_yet_another_bulk_deleted_test_file.py",
        "app/media_exec/run_job.py",
    ]
    commands = _quick_commands(paths)
    pytest_commands = [command for command, _cwd in commands if "pytest" in command]
    assert len(pytest_commands) == 1
    command = pytest_commands[0]
    assert "tests/test_yet_another_bulk_deleted_test_file.py" not in command

    result = subprocess.run(
        [*command, "--collect-only"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_isolated_environment_removes_runtime_provider_access(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HIAGENT_API_KEY", "production-secret")
    monkeypatch.setenv("MINIMAX_H3_BASE_URL", "http://production-provider")

    env = _isolated_environment(tmp_path)

    assert env["MANJU_TEST_PROFILE"] == "isolated"
    assert env["MANJU_TEST_SANDBOX"] == str(tmp_path)
    assert env["HIAGENT_API_KEY"] == ""
    assert env["MINIMAX_H3_BASE_URL"] == ""


def test_full_verification_is_isolated_unless_live_is_explicit() -> None:
    isolated_commands = [command for command, _cwd in _full_commands()]
    live_commands = [
        command for command, _cwd in _full_commands(live_integration=True)
    ]

    assert all("--live-integration" not in command for command in isolated_commands)
    assert live_commands[:-1] == isolated_commands
    assert live_commands[-1][-3:] == [
        "-m",
        "live_integration",
        "--live-integration",
    ]


def test_run_passes_the_isolated_environment_to_subprocess(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(command, *, cwd, check, env):
        captured.update(command=command, cwd=cwd, check=check, env=env)

    monkeypatch.setattr(subprocess, "run", fake_run)
    expected_env = {"MANJU_TEST_PROFILE": "isolated"}

    _run(["tool", "check"], env=expected_env)

    assert captured == {
        "command": ["tool", "check"],
        "cwd": ROOT,
        "check": True,
        "env": expected_env,
    }


def test_live_integration_requires_full_verification() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/verify.py", "--live-integration", "--plan"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "--live-integration requires --full" in result.stderr
