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
