import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from tests.isolation import IsolationSession

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_LIVE_INTEGRATION = False
_ISOLATION_SESSION: IsolationSession | None = None
_SANDBOX: Path | None = None
_SANDBOX_OWNED = False


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("manju test isolation")
    group.addoption(
        "--live-integration",
        action="store_true",
        default=False,
        help="run only tests marked live_integration with real runtime access",
    )


def pytest_configure(config: pytest.Config) -> None:
    global _LIVE_INTEGRATION, _ISOLATION_SESSION, _SANDBOX, _SANDBOX_OWNED

    _LIVE_INTEGRATION = bool(config.getoption("--live-integration"))
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
    config.option.basetemp = str(_SANDBOX / "pytest-tmp")

    os.environ["MANJU_TEST_PROFILE"] = "isolated"
    os.environ["HIAGENT_API_KEY"] = ""
    os.environ["OPENROUTER_API_KEY"] = ""
    os.environ["BAILIAN_API_KEY"] = ""
    os.environ["DASHSCOPE_API_KEY"] = ""
    os.environ["DEEPSEEK_API_KEY"] = ""
    os.environ["ZHIPU_API_KEY"] = ""
    os.environ["MINIMAX_H3_API_KEY"] = ""
    os.environ["MINIMAX_H3_BASE_URL"] = ""

    # Configure the process before test module collection imports application code.
    from app import config as app_config

    app_config.RUNTIME_ROOT = _SANDBOX
    app_config.PROJECTS_DIR = _SANDBOX / "projects"
    app_config.DATA_DIR = _SANDBOX / "data"
    app_config.DB_PATH = app_config.DATA_DIR / "manju.db"
    app_config.HIAGENT_API_KEY = ""
    app_config.OPENROUTER_API_KEY = ""
    app_config.BAILIAN_API_KEY = ""
    app_config.DEEPSEEK_API_KEY = ""
    app_config.ZHIPU_API_KEY = ""
    app_config.MINIMAX_H3_API_KEY = ""
    app_config.MINIMAX_H3_BASE_URL = ""
    app_config.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    app_config.DATA_DIR.mkdir(parents=True, exist_ok=True)

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
    try:
        from app import db

        conn = getattr(db._local, "conn", None)
        if conn is not None:
            conn.close()
            db._local.conn = None
    except (ImportError, AttributeError):
        pass
    if _SANDBOX_OWNED and _SANDBOX is not None:
        shutil.rmtree(_SANDBOX, ignore_errors=True)


@pytest.fixture(autouse=True)
def _reset_capability_runtime():
    """各测试隔离 Command Bus 幂等缓存与审批令牌，避免共用 episode_id 等夹具键串扰。"""
    from app.capabilities.bus import reset_command_bus_for_tests
    from app.capabilities.idempotency import clear_for_tests
    from app.capabilities.policy import reset_approvals_for_tests
    from app import db

    # 幂等清理会打开进程级 DB；若此前被建成空库，先补齐 SCHEMA，避免无 settings 表。
    try:
        db.get_conn().execute("SELECT 1 FROM settings LIMIT 1").fetchone()
    except Exception:  # noqa: BLE001
        db.init_db()

    reset_command_bus_for_tests()
    reset_approvals_for_tests()
    clear_for_tests()
    yield
    reset_command_bus_for_tests()
    reset_approvals_for_tests()
    clear_for_tests()


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
