from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from scripts.verify import (
    ROOT,
    _full_commands,
    _isolated_environment,
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
