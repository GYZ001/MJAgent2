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
from tests.isolation import (
    IsolationSession,
    ProviderConfigurationIsolation,
    TestIsolationViolation,
    UNROUTABLE_PROVIDER_BASE_URL,
)


ROOT = Path(__file__).resolve().parents[1]


def _provider_isolation() -> ProviderConfigurationIsolation:
    return ProviderConfigurationIsolation(
        settings=config,
        environment=os.environ,
    )


def test_pytest_defaults_to_injected_runtime_sandbox() -> None:
    sandbox = Path(os.environ["MANJU_TEST_SANDBOX"]).resolve()

    assert config.TEST_PROFILE == "isolated"
    assert config.RUNTIME_ROOT == sandbox
    assert config.DATA_DIR == sandbox / "data"
    assert config.PROJECTS_DIR == sandbox / "projects"
    assert config.DB_PATH == sandbox / "data" / "manju.db"
    assert db.DB_PATH == config.DB_PATH
    assert db.DB_PATH != ROOT / "data" / "manju.db"


def test_pytest_allows_scoped_provider_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolation = _provider_isolation()
    credential = isolation.schema.credentials[0]
    endpoint = isolation.schema.endpoints[0]

    monkeypatch.setenv(credential, "fake-credential")
    monkeypatch.setattr(config, credential, "fake-credential")
    monkeypatch.setenv(endpoint, "https://provider.example")
    monkeypatch.setattr(config, endpoint, "https://provider.example")

    assert os.environ[credential] == getattr(config, credential) == "fake-credential"
    assert os.environ[endpoint] == getattr(config, endpoint) == "https://provider.example"


def test_pytest_injects_provider_isolation_configuration() -> None:
    state = _provider_isolation().state()

    assert state["credentials"]["environment"]
    assert state["credentials"]["runtime"]
    assert state["endpoints"]["environment"]
    assert state["endpoints"]["runtime"]
    for values in state["credentials"].values():
        assert set(values.values()) == {""}
    for values in state["endpoints"].values():
        assert set(values.values()) == {UNROUTABLE_PROVIDER_BASE_URL}


def test_pytest_restores_direct_provider_mutations_between_tests(
    tmp_path: Path,
) -> None:
    child_sandbox = tmp_path / "provider-boundary"
    child_sandbox.mkdir()
    probe = tmp_path / "test_provider_boundary_probe.py"
    probe.write_text(
        textwrap.dedent(
            """
            import os

            from app import config
            from tests.isolation import ProviderConfigurationIsolation


            def provider_isolation():
                return ProviderConfigurationIsolation(
                    settings=config,
                    environment=os.environ,
                )


            def test_direct_provider_mutation_is_visible_to_its_owner():
                isolation = provider_isolation()
                credential = isolation.schema.credentials[0]
                endpoint = isolation.schema.endpoints[0]
                os.environ[credential] = "direct-fake"
                setattr(config, credential, "direct-fake")
                os.environ[endpoint] = "https://provider.example"
                setattr(config, endpoint, "https://provider.example")
                os.environ["FUTURE_PROVIDER_API_KEY"] = "future-fake"
                assert getattr(config, credential) == "direct-fake"


            def test_next_test_starts_fail_closed():
                state = provider_isolation().state()
                for values in state["credentials"].values():
                    assert set(values.values()) == {""}
                for values in state["endpoints"].values():
                    assert set(values.values()) == {
                        "http://pytest-deny-network.invalid"
                    }
            """
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["MANJU_TEST_SANDBOX"] = str(child_sandbox)

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

    assert result.returncode == pytest.ExitCode.OK, result.stdout + result.stderr


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
