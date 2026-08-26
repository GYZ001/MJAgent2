"""分池并发 + 自适应限流 + 可升降 worker 池。"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from app.db import get_setting, set_setting
from app.media_pipeline import stages as S

# settings 键 → 默认硬上限（均衡档）
CHANNEL_DEFAULTS: dict[str, int] = {
    S.RESOURCE_REFERENCE: 15,
    S.RESOURCE_IMAGE: 4,
    S.RESOURCE_VLM: 6,
    S.RESOURCE_VIDEO_SUBMIT: 15,
    S.RESOURCE_VIDEO_INFLIGHT: 15,
    S.RESOURCE_VIDEO_POLL: 15,
    S.RESOURCE_DOWNLOAD: 3,
    S.RESOURCE_FINALIZE: 4,
}

SETTING_KEYS = {
    S.RESOURCE_REFERENCE: "reference_pipeline_concurrency",
    S.RESOURCE_IMAGE: "image_request_concurrency",
    S.RESOURCE_VLM: "vlm_request_concurrency",
    S.RESOURCE_VIDEO_SUBMIT: "video_submit_concurrency",
    S.RESOURCE_VIDEO_INFLIGHT: "video_inflight_limit",
    S.RESOURCE_VIDEO_POLL: "video_poll_concurrency",
    S.RESOURCE_DOWNLOAD: "download_concurrency",
    S.RESOURCE_FINALIZE: "finalize_concurrency",
}

# 兼容旧键：读时回填到新键
LEGACY_MAP = {
    "video_concurrency": S.RESOURCE_VIDEO_SUBMIT,
    "auto_concurrency": S.RESOURCE_VIDEO_INFLIGHT,
}


@dataclass
class _ChannelState:
    name: str
    hard_limit: int
    current: int
    congestion_hits: int = 0
    healthy_since: float | None = None
    cooldown_until: float = 0.0
    semaphore: asyncio.Semaphore | None = field(default=None, repr=False)


_channels: dict[str, _ChannelState] = {}
_loop_semaphores: dict[tuple[int, str], asyncio.Semaphore] = {}


def _int_setting(key: str, default: int) -> int:
    try:
        raw = get_setting(key)
    except Exception:  # noqa: BLE001 设置表未就绪时回落到通道默认值
        return max(1, int(default))
    if raw is None or raw == "":
        return max(1, int(default))
    try:
        return max(1, int(raw))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"非法运行时设置 {key}={raw!r}；请在监制房修正") from exc


def channel_limit(resource: str) -> int:
    """当前生效并发（含自适应下调后的值，不超过硬上限）。"""
    state = ensure_channel(resource)
    return max(1, min(state.current, state.hard_limit))


def hard_limit(resource: str) -> int:
    return ensure_channel(resource).hard_limit


def ensure_channel(resource: str) -> _ChannelState:
    if resource not in _channels:
        key = SETTING_KEYS.get(resource)
        default = CHANNEL_DEFAULTS.get(resource, 2)
        limit = _int_setting(key, default) if key else default
        _channels[resource] = _ChannelState(
            name=resource, hard_limit=limit, current=limit,
        )
    return _channels[resource]


def reload_limits_from_settings() -> None:
    """设置变更后即时生效：更新硬上限；当前值向硬上限对齐（升），或立即下调。"""
    for resource, key in SETTING_KEYS.items():
        state = ensure_channel(resource)
        new_hard = _int_setting(key, CHANNEL_DEFAULTS[resource])
        old_hard = state.hard_limit
        state.hard_limit = new_hard
        if new_hard < state.current:
            state.current = new_hard
            _resize_semaphore(resource)
        elif new_hard > old_hard and state.current < new_hard and time.time() >= state.cooldown_until:
            # 提高硬上限时立刻放开到新上限（健康增长仍由 report_healthy 微调）
            state.current = new_hard
            _resize_semaphore(resource)


def semaphore_for(resource: str) -> asyncio.Semaphore:
    """按 event loop + 资源通道取信号量；限制变更时重建。"""
    loop = asyncio.get_running_loop()
    key = (id(loop), resource)
    state = ensure_channel(resource)
    sem = _loop_semaphores.get(key)
    if sem is None or getattr(sem, "_mj_limit", None) != state.current:
        sem = asyncio.Semaphore(state.current)
        sem._mj_limit = state.current  # type: ignore[attr-defined]
        _loop_semaphores[key] = sem
        state.semaphore = sem
    return sem


def _resize_semaphore(resource: str) -> None:
    """丢弃缓存信号量，下次 acquire 时按 current 重建。"""
    dead = [k for k in _loop_semaphores if k[1] == resource]
    for k in dead:
        _loop_semaphores.pop(k, None)


def report_congestion(resource: str, *, reason: str = "429") -> None:
    """连续拥塞：通道并发减半，冷却 60 秒。视频提交与轮询分通道，互不误伤。"""
    state = ensure_channel(resource)
    state.congestion_hits += 1
    if state.congestion_hits < 2:
        return
    state.congestion_hits = 0
    state.current = max(1, state.current // 2)
    state.cooldown_until = time.time() + 60.0
    state.healthy_since = None
    _resize_semaphore(resource)


def report_healthy(resource: str) -> None:
    """连续 10 分钟健康：每次 +1，直到硬上限。"""
    state = ensure_channel(resource)
    now = time.time()
    if now < state.cooldown_until:
        return
    state.congestion_hits = 0
    if state.healthy_since is None:
        state.healthy_since = now
        return
    if now - state.healthy_since < 600.0:
        return
    if state.current < state.hard_limit:
        state.current += 1
        _resize_semaphore(resource)
    state.healthy_since = now


def snapshot() -> dict[str, dict]:
    out = {}
    for resource in CHANNEL_DEFAULTS:
        state = ensure_channel(resource)
        out[resource] = {
            "hard_limit": state.hard_limit,
            "current": state.current,
            "cooldown_until": state.cooldown_until,
            "setting_key": SETTING_KEYS.get(resource),
        }
    return out


def migrate_legacy_settings() -> None:
    """把 video_concurrency / auto_concurrency 迁移到分通道键（仅当新键未设置）。"""
    for legacy, resource in LEGACY_MAP.items():
        key = SETTING_KEYS[resource]
        if get_setting(key):
            continue
        legacy_val = get_setting(legacy)
        if legacy_val:
            set_setting(key, str(legacy_val))
    # 写入均衡档缺省，方便监制房展示
    for resource, key in SETTING_KEYS.items():
        if not get_setting(key):
            set_setting(key, str(CHANNEL_DEFAULTS[resource]))


