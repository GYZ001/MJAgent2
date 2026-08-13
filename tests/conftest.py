import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

from tests.isolation import (
    IsolationSession,
    ProviderConfigurationIsolation,
    UNROUTABLE_PROVIDER_BASE_URL,
    isolate_provider_environment,
)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_LIVE_INTEGRATION = False
_ISOLATION_SESSION: IsolationSession | None = None
_PROVIDER_ISOLATION: ProviderConfigurationIsolation | None = None
_SANDBOX: Path | None = None
_SANDBOX_OWNED = False
_DATABASE_TEMPLATE: Path | None = None
_DATABASE_TEMPLATE_INITIALIZED = False


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("manju test isolation")
    group.addoption(
        "--live-integration",
        action="store_true",
        default=False,
        help="run only tests marked live_integration with real runtime access",
    )


def pytest_configure(config: pytest.Config) -> None:
    global _LIVE_INTEGRATION, _ISOLATION_SESSION, _PROVIDER_ISOLATION
    global _SANDBOX, _SANDBOX_OWNED
    global _DATABASE_TEMPLATE, _DATABASE_TEMPLATE_INITIALIZED

    _LIVE_INTEGRATION = bool(config.getoption("--live-integration"))
    _DATABASE_TEMPLATE = None
    _DATABASE_TEMPLATE_INITIALIZED = False
    if _LIVE_INTEGRATION:
        os.environ["MANJU_TEST_PROFILE"] = "live-integration"
        return

    configured_sandbox = os.environ.get("MANJU_TEST_SANDBOX", "").strip()
    if configured_sandbox:
        _SANDBOX = Path(configured_sandbox).expanduser().resolve()
        _SANDBOX.mkdir(parents=True, exist_ok=True)
        _SANDBOX_OWNED = False
    else:
        _SANDBOX = Path(tempfile.mkdtemp(prefix="manju-pytest-")).resolve()
        _SANDBOX_OWNED = True
    os.environ["MANJU_TEST_SANDBOX"] = str(_SANDBOX)
    config.option.basetemp = str(_SANDBOX / "pytest-tmp")

    os.environ["MANJU_TEST_PROFILE"] = "isolated"
    isolate_provider_environment(os.environ)

    # Configure the process before test module collection imports application code.
    from app import config as app_config

    app_config.RUNTIME_ROOT = _SANDBOX
    app_config.PROJECTS_DIR = _SANDBOX / "projects"
    app_config.DATA_DIR = _SANDBOX / "data"
    app_config.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    app_config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    template_dir = Path(tempfile.mkdtemp(prefix="pytest-db-template-", dir=app_config.DATA_DIR))
    _DATABASE_TEMPLATE = template_dir / "manju.db"
    app_config.DB_PATH = _DATABASE_TEMPLATE
    _PROVIDER_ISOLATION = ProviderConfigurationIsolation(
        settings=app_config,
        environment=os.environ,
        blocked_endpoint=UNROUTABLE_PROVIDER_BASE_URL,
    )
    _PROVIDER_ISOLATION.apply()

    from app import db

    db.DATA_DIR = app_config.DATA_DIR
    db.DB_PATH = app_config.DB_PATH
    _ISOLATION_SESSION = IsolationSession(
        sandbox=_SANDBOX,
    )


def pytest_sessionstart(session: pytest.Session) -> None:
    del session
    if _ISOLATION_SESSION is not None:
        _ISOLATION_SESSION.install()


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    live = bool(config.getoption("--live-integration"))
    for item in items:
        marked_live = item.get_closest_marker("live_integration") is not None
        if live and not marked_live:
            item.add_marker(pytest.mark.skip(reason="live integration mode runs only explicitly marked tests"))
        elif not live and marked_live:
            item.add_marker(pytest.mark.skip(reason="requires --live-integration"))


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    del exitstatus
    if _ISOLATION_SESSION is None:
        return
    _ISOLATION_SESSION.restore()
    if not _ISOLATION_SESSION.audit.violations:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_sep("=", "TEST ISOLATION VIOLATIONS", red=True)
        for violation in _ISOLATION_SESSION.audit.violations:
            reporter.write_line(violation, red=True)
    session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_unconfigure(config: pytest.Config) -> None:
    del config
    if _ISOLATION_SESSION is not None:
        _ISOLATION_SESSION.restore()
    db = sys.modules.get("app.db")
    if db is not None:
        local = getattr(db, "_local", None)
        conn = getattr(local, "conn", None)
        if conn is not None:
            conn.close()
            local.conn = None
    if _SANDBOX_OWNED and _SANDBOX is not None:
        shutil.rmtree(_SANDBOX, ignore_errors=True)


def _restore_isolated_runtime(db, *, database_path: Path | None = None) -> None:
    if _SANDBOX is None or _DATABASE_TEMPLATE is None:
        return

    from app import config as app_config

    target_database = (database_path or _DATABASE_TEMPLATE).resolve()
    os.environ["MANJU_TEST_PROFILE"] = "isolated"
    os.environ["MANJU_TEST_SANDBOX"] = str(_SANDBOX)
    app_config.TEST_PROFILE = "isolated"
    app_config._test_sandbox = str(_SANDBOX)
    app_config.RUNTIME_ROOT = _SANDBOX
    app_config.PROJECTS_DIR = _SANDBOX / "projects"
    app_config.DATA_DIR = _SANDBOX / "data"
    app_config.DB_PATH = target_database
    app_config.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    app_config.DATA_DIR.mkdir(parents=True, exist_ok=True)

    db._local.conn = None
    db.DATA_DIR = app_config.DATA_DIR
    db.DB_PATH = app_config.DB_PATH
    if _PROVIDER_ISOLATION is not None:
        _PROVIDER_ISOLATION.apply()


def _connection_database_path(connection: sqlite3.Connection) -> Path | None:
    try:
        row = connection.execute("PRAGMA database_list").fetchone()
    except sqlite3.ProgrammingError:
        return None
    if row is None or not row[2]:
        return None
    return Path(row[2]).resolve()


def _release_local_connection(db, *, owned_database: Path) -> None:
    connection = getattr(db._local, "conn", None)
    if connection is None:
        return
    db._local.conn = None
    if _connection_database_path(connection) != owned_database.resolve():
        return
    try:
        if connection.in_transaction:
            connection.rollback()
    finally:
        connection.close()


def _initialize_database_template(db) -> None:
    global _DATABASE_TEMPLATE_INITIALIZED

    if _DATABASE_TEMPLATE_INITIALIZED:
        return
    if _DATABASE_TEMPLATE is None:
        raise RuntimeError("pytest database template is not configured")

    _restore_isolated_runtime(db, database_path=_DATABASE_TEMPLATE)
    connection = db.get_conn()
    try:
        db.init_db()
    finally:
        connection.close()
        db._local.conn = None
    _DATABASE_TEMPLATE_INITIALIZED = True


def _clone_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(source)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()


def _reset_command_bus_runtime(capability_bus) -> None:
    capability_bus._BUS = capability_bus.CommandBus(capability_bus.get_registry())


def _reset_media_worker_runtime() -> None:
    """Do not let an in-memory media backlog leak between isolated tests."""

    worker = sys.modules.get("app.worker")
    if worker is None:
        return
    worker._queue = asyncio.Queue()
    worker._reference_queue = worker._queue
    worker._video_ready_queue = asyncio.Queue()
    worker._poll_queue = asyncio.Queue()


@pytest.fixture(autouse=True)
def _reset_capability_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """每个测试独占数据库，并重置进程内 Command Bus 与审批状态。"""

    if _LIVE_INTEGRATION:
        yield
        return

    from app.capabilities import bus as capability_bus
    from app.capabilities.policy import reset_approvals_for_tests
    from app import db

    if _DATABASE_TEMPLATE is None:
        raise RuntimeError("pytest database template is not configured")
    _release_local_connection(db, owned_database=_DATABASE_TEMPLATE)
    _initialize_database_template(db)

    test_database = tmp_path / "manju.db"
    _clone_database(_DATABASE_TEMPLATE, test_database)
    _restore_isolated_runtime(db, database_path=test_database)
    _reset_command_bus_runtime(capability_bus)
    _reset_media_worker_runtime()
    reset_approvals_for_tests()

    try:
        yield
    finally:
        try:
            monkeypatch.undo()
        finally:
            _reset_command_bus_runtime(capability_bus)
            _reset_media_worker_runtime()
            reset_approvals_for_tests()
            _release_local_connection(db, owned_database=test_database)
            _restore_isolated_runtime(db, database_path=_DATABASE_TEMPLATE)


def session_headers() -> dict[str, str]:
    """测试用：领取当前进程本机会话头。"""
    from app.local_session import ensure_session_secret

    return {"X-Manju-Session": ensure_session_secret()}


class SessionTestClient:
    """包装 TestClient，自动附加 X-Manju-Session（Todolist T1 回归）。"""

    def __init__(self, client):
        self._client = client
        self._headers = session_headers()

    def request(self, method: str, url: str, **kwargs):
        headers = {**self._headers, **(kwargs.pop("headers", None) or {})}
        return self._client.request(method, url, headers=headers, **kwargs)

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs):
        return self.request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs):
        return self.request("DELETE", url, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._client, name)
