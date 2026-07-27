import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


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
