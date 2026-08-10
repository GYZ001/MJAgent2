from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app import config, db
from tests.isolation import IsolationSession, TestIsolationViolation


ROOT = Path(__file__).resolve().parents[1]


def test_pytest_defaults_to_injected_runtime_sandbox() -> None:
    sandbox = Path(os.environ["MANJU_TEST_SANDBOX"]).resolve()

    assert config.TEST_PROFILE == "isolated"
    assert config.RUNTIME_ROOT == sandbox
    assert config.DATA_DIR == sandbox / "data"
    assert config.PROJECTS_DIR == sandbox / "projects"
    assert config.DB_PATH == sandbox / "data" / "manju.db"
    assert db.DB_PATH == config.DB_PATH
    assert db.DB_PATH != ROOT / "data" / "manju.db"


def test_pytest_does_not_inherit_provider_credentials() -> None:
    credential_names = (
        "HIAGENT_API_KEY",
        "OPENROUTER_API_KEY",
        "BAILIAN_API_KEY",
        "DASHSCOPE_API_KEY",
        "DEEPSEEK_API_KEY",
        "ZHIPU_API_KEY",
        "MINIMAX_H3_API_KEY",
    )

    assert all(os.environ.get(name) == "" for name in credential_names)
    assert config.HIAGENT_API_KEY == ""
    assert config.OPENROUTER_API_KEY == ""
    assert config.BAILIAN_API_KEY == ""
    assert config.DEEPSEEK_API_KEY == ""
    assert config.ZHIPU_API_KEY == ""


def test_pytest_injects_minimax_mock_base_url_without_credentials() -> None:
    assert os.environ["MINIMAX_H3_API_KEY"] == ""
    assert config.MINIMAX_H3_API_KEY == ""
    assert os.environ["MINIMAX_H3_BASE_URL"] == config.MINIMAX_H3_BASE_URL
    assert config.MINIMAX_H3_BASE_URL


def test_isolated_profile_requires_an_explicit_sandbox() -> None:
    env = os.environ.copy()
    env["MANJU_TEST_PROFILE"] = "isolated"
    env.pop("MANJU_TEST_SANDBOX", None)

    result = subprocess.run(
        [sys.executable, "-c", "import app.config"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "requires MANJU_TEST_SANDBOX" in result.stderr


def test_transport_guard_captures_caught_network_attempt(tmp_path: Path) -> None:
    session = IsolationSession(sandbox=tmp_path)
    session.install()
    try:
        with pytest.raises(TestIsolationViolation, match="external network"):
            socket.create_connection(("provider.invalid", 443))
        with pytest.raises(TestIsolationViolation, match="external network"):
            session.audit.assert_clean()
    finally:
        session.restore()


def test_transport_guard_rejects_persistent_db_outside_sandbox(
    tmp_path: Path,
) -> None:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    protected_db = tmp_path / "production" / "manju.db"
    session = IsolationSession(sandbox=sandbox)
    session.install()
    try:
        with pytest.raises(
            TestIsolationViolation,
            match="persistent database outside test sandbox",
        ):
            sqlite3.connect(protected_db)
        with pytest.raises(TestIsolationViolation, match=str(protected_db)):
            session.audit.assert_clean()
    finally:
        session.restore()


def test_transport_guard_allows_sandbox_and_memory_databases(
    tmp_path: Path,
) -> None:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    session = IsolationSession(sandbox=sandbox)
    session.install()
    try:
        memory = sqlite3.connect(":memory:")
        persisted = sqlite3.connect(sandbox / "test.db")
        memory.close()
        persisted.close()
        session.audit.assert_clean()
    finally:
        session.restore()


def test_caught_access_violation_still_fails_pytest_session(
    tmp_path: Path,
) -> None:
    child_sandbox = tmp_path / "child-sandbox"
    child_sandbox.mkdir()
    probe = tmp_path / "test_caught_access_probe.py"
    probe.write_text(
        textwrap.dedent(
            """
            import os
            import socket
            import sqlite3


            def test_application_cannot_swallow_isolation_violations():
                for action in (
                    lambda: socket.create_connection(("provider.invalid", 443)),
                    lambda: sqlite3.connect(os.environ["PRODUCTION_DB_PROBE"]),
                ):
                    try:
                        action()
                    except RuntimeError:
                        pass
            """
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["MANJU_TEST_SANDBOX"] = str(child_sandbox)
    env["PRODUCTION_DB_PROBE"] = str(ROOT / "data" / "manju.db")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "tests.conftest",
            str(probe),
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == pytest.ExitCode.TESTS_FAILED
    assert "TEST ISOLATION VIOLATIONS" in result.stdout
    assert "external network" in result.stdout
    assert "persistent database outside test sandbox" in result.stdout
