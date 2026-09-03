"""最近活跃：有历史登录、直接打开页面/发请求也算活跃——独立于登录动作本身。

中间件对 ``/api/*`` 的任意方法（含 GET）、principal 为真实登录用户时调用
``touch()``；进程内每用户 60 秒节流，避免高频轮询把 user_activity 写爆。
"""
from __future__ import annotations

import threading
import time

from app.audit import store
from app.auth.principal import Principal

_THROTTLE_S = 60.0
_MAX_TRACKED_USERS = 10000

_lock = threading.Lock()
_last_touch: dict[str, float] = {}


def touch(principal: Principal | None, path: str) -> None:
    """记录一次活跃；同一用户节流窗口内只落一次库，写失败静默跳过（下次请求再写）。"""
    if principal is None or principal.user_id == "legacy-shared":
        return
    now = time.time()
    with _lock:
        last = _last_touch.get(principal.user_id)
        if last is not None and now - last < _THROTTLE_S:
            return
        _last_touch[principal.user_id] = now
        _evict_stale_if_too_large(now)
    store.upsert_user_activity(principal.user_id, now, path)


def _evict_stale_if_too_large(now: float) -> None:
    """有界 dict：只在超过阈值时才扫描剔除已过节流窗口的旧条目。"""
    if len(_last_touch) <= _MAX_TRACKED_USERS:
        return
    stale = [uid for uid, ts in _last_touch.items() if now - ts >= _THROTTLE_S]
    for uid in stale:
        del _last_touch[uid]
