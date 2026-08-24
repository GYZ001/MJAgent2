import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
import time
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

_STALE_SANDBOX_MAX_AGE_HOURS = 24.0


def _purge_stale_sandboxes(prefix: str, *, max_age_hours: float = _STALE_SANDBOX_MAX_AGE_HOURS) -> None:
    """Best-effort startup sweep for orphaned ``prefix*`` dirs under /tmp.

    ``pytest_unconfigure`` below already removes this run's own sandbox on
    normal completion and on most exceptions (including KeyboardInterrupt).
    Neither that nor any ``finally``/``atexit`` hook runs when the process is
    hard-killed (SIGKILL, or the default SIGTERM action) -- that is how
    sandboxes actually accumulated in /tmp. This sweep is the backstop: it
    only ever removes dirs older than ``max_age_hours``, so a sandbox still
    owned by a running process is never touched.
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
        _purge_stale_sandboxes("manju-pytest-")
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

    from app.auth.principal import Principal, set_current_principal
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
    # 直接调用 Command Bus 的测试（不经 HTTP，也就绕过 require_local_session）
    # 兜底注入一个系统管理员身份，后续阶段 Command Bus 收紧 scope 校验时
    # 这批测试不需要逐个改造。
    set_current_principal(
        Principal(user_id="test-bus-admin", username="test-bus-admin",
                  is_system_admin=True, workspace_roles={})
    )

    try:
        yield
    finally:
        try:
            monkeypatch.undo()
        finally:
            _reset_command_bus_runtime(capability_bus)
            _reset_media_worker_runtime()
            reset_approvals_for_tests()
            set_current_principal(None)
            _release_local_connection(db, owned_database=test_database)
            _restore_isolated_runtime(db, database_path=_DATABASE_TEMPLATE)


_TEST_ADMIN_USERNAME = "test-admin"


def ensure_test_admin() -> str:
    """确保当前（隔离沙盒）数据库里有一个系统管理员账号，返回其 user_id。"""
    from app.auth.passwords import hash_password
    from app.db import get_conn, new_id, now

    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM users WHERE username=?", (_TEST_ADMIN_USERNAME,)
    ).fetchone()
    if row is not None:
        return str(row["id"])
    user_id = new_id("user")
    ts = now()
    conn.execute(
        """INSERT INTO users(
               id, username, display_name, password_hash, auth_provider,
               status, is_system_admin, must_change_password, created_at,
               password_changed_at
           ) VALUES(?,?,?,?,'local','active',1,0,?,?)""",
        (
            user_id,
            _TEST_ADMIN_USERNAME,
            "测试系统管理员",
            hash_password("test-admin-password-000"),
            ts,
            ts,
        ),
    )
    conn.execute(
        "INSERT OR IGNORE INTO workspace_members(workspace_id, user_id, role, created_at) "
        "VALUES('ws_default', ?, 'workspace_admin', ?)",
        (user_id, ts),
    )
    conn.commit()
    return user_id


def session_headers() -> dict[str, str]:
    """测试用：为隔离测试库里的系统管理员账号签发一枚真实登录会话。"""
    from app.auth.sessions import create_session

    user_id = ensure_test_admin()
    return {"X-Manju-Session": create_session(user_id)}


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
