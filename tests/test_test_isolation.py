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


def test_pytest_restores_runtime_state_between_disruptive_tests(
    tmp_path: Path,
) -> None:
    child_sandbox = tmp_path / "runtime-boundary"
    child_sandbox.mkdir()
    probe = tmp_path / "test_runtime_boundary_probe.py"
    probe.write_text(
        textwrap.dedent(
            """
            import os
            import sqlite3
            from pathlib import Path

            import pytest

            from app import config, db
            from tests.isolation import ProviderConfigurationIsolation


            closed_connection = None
            replacement_connection = None


            def provider_isolation():
                return ProviderConfigurationIsolation(
                    settings=config,
                    environment=os.environ,
                )


            def assert_isolated_runtime():
                sandbox = Path(os.environ["MANJU_TEST_SANDBOX"]).resolve()
                assert config.RUNTIME_ROOT == sandbox
                assert config.PROJECTS_DIR == sandbox / "projects"
                assert config.DATA_DIR == sandbox / "data"
                assert config.DB_PATH == sandbox / "data" / "manju.db"
                assert db.DATA_DIR == config.DATA_DIR
                assert db.DB_PATH == config.DB_PATH

                state = provider_isolation().state()
                for values in state["credentials"].values():
                    assert set(values.values()) == {""}
                for values in state["endpoints"].values():
                    assert set(values.values()) == {
                        "http://pytest-deny-network.invalid"
                    }


            def test_01_body_may_close_the_fixture_connection():
                global closed_connection
                isolation = provider_isolation()
                credential = isolation.schema.credentials[0]
                endpoint = isolation.schema.endpoints[0]
                closed_connection = db.get_conn()
                closed_connection.close()

                os.environ[credential] = "direct-fake"
                setattr(config, credential, "direct-fake")
                os.environ[endpoint] = "https://provider.example"
                setattr(config, endpoint, "https://provider.example")
                os.environ["FUTURE_PROVIDER_API_KEY"] = "future-fake"
                config.DB_PATH = Path(os.environ["MANJU_TEST_SANDBOX"]) / "closed.db"
                db.DB_PATH = config.DB_PATH

                with pytest.raises(sqlite3.ProgrammingError):
                    closed_connection.execute("SELECT 1")


            def test_02_next_test_creates_a_fresh_connection():
                assert_isolated_runtime()
                assert db._local.conn is None
                connection = db.get_conn()
                assert connection is not closed_connection
                assert connection.execute(
                    "SELECT 1 FROM settings LIMIT 1"
                ).fetchone() is not None
                connection.close()


            def test_03_body_may_replace_the_connection_and_runtime():
                global replacement_connection
                sandbox = Path(os.environ["MANJU_TEST_SANDBOX"]).resolve()
                replacement_connection = sqlite3.connect(":memory:")
                db._local.conn = replacement_connection
                config.RUNTIME_ROOT = sandbox / "replacement-runtime"
                config.PROJECTS_DIR = sandbox / "replacement-projects"
                config.DATA_DIR = sandbox / "replacement-data"
                config.DB_PATH = sandbox / "replacement.db"
                db.DATA_DIR = config.DATA_DIR
                db.DB_PATH = config.DB_PATH

                isolation = provider_isolation()
                credential = isolation.schema.credentials[0]
                endpoint = isolation.schema.endpoints[0]
                os.environ[credential] = "replacement-fake"
                setattr(config, credential, "replacement-fake")
                os.environ[endpoint] = "https://replacement.example"
                setattr(config, endpoint, "https://replacement.example")

                assert db.get_conn() is replacement_connection


            def test_04_replaced_connection_is_detached_not_reused():
                assert_isolated_runtime()
                assert replacement_connection.execute("SELECT 1").fetchone()[0] == 1
                assert db._local.conn is None
                connection = db.get_conn()
                assert connection is not replacement_connection
                database_file = connection.execute(
                    "PRAGMA database_list"
                ).fetchone()["file"]
                assert Path(database_file).resolve() == config.DB_PATH.resolve()
                connection.close()
                replacement_connection.close()


            @pytest.mark.xfail(raises=RuntimeError, strict=True)
            def test_05_exception_still_runs_fixture_restoration():
                isolation = provider_isolation()
                credential = isolation.schema.credentials[0]
                endpoint = isolation.schema.endpoints[0]
                os.environ[credential] = "exception-fake"
                setattr(config, credential, "exception-fake")
                os.environ[endpoint] = "https://exception.example"
                setattr(config, endpoint, "https://exception.example")
                config.DB_PATH = (
                    Path(os.environ["MANJU_TEST_SANDBOX"]) / "exception.db"
                )
                db.DB_PATH = config.DB_PATH
                db.get_conn().close()
                raise RuntimeError("expected body failure")


            def test_06_exception_path_restores_all_isolation_state():
                assert_isolated_runtime()
                assert db._local.conn is None
                connection = db.get_conn()
                assert connection.execute("SELECT 1").fetchone()[0] == 1
                connection.close()
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
