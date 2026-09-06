"""分池并发 + 自适应限流 + 可升降 worker 池。"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from app.db import get_setting, set_setting
from app.media_pipeline import stages as S

_LOGGER = logging.getLogger(__name__)

# 文本 provider 调用槽位不是 QPSP 视频媒体阶段（app/media_pipeline/stages.py 只
# 覆盖视频 job 生命周期），但复用同一套"拥塞减半 + 健康爬升"自适应状态机，资源键
# 直接定义在本模块，不去污染 stages.py 的视频阶段枚举。
RESOURCE_TEXT_PROVIDER = "text_provider_calls"

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
    RESOURCE_TEXT_PROVIDER: 6,
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
    RESOURCE_TEXT_PROVIDER: "text_generation_concurrency",
}

# 0/空 = 自动：不设固定上限（用户 2026-09-06 拍板：所有并发不设上限，以机器与供应商的实际表现为界）。
# 自动模式下硬上限只是安全阀，真正生效的并发由供应商信号自适应发现：慢启动——没见过拥塞前每个健康
# 周期翻倍，见过拥塞后每周期 +1，拥塞（429/5xx/超时/掐流）减半。机器内存/磁盘/CPU 水位另有
# app/observability/machine_watermark 在准入处把关。
AUTO_CEILINGS: dict[str, int] = {
    S.RESOURCE_REFERENCE: 64, S.RESOURCE_IMAGE: 64, S.RESOURCE_VLM: 64, S.RESOURCE_VIDEO_SUBMIT: 64,
    S.RESOURCE_VIDEO_INFLIGHT: 128, S.RESOURCE_VIDEO_POLL: 128, S.RESOURCE_DOWNLOAD: 16, S.RESOURCE_FINALIZE: 16,
    RESOURCE_TEXT_PROVIDER: 32,
}
AUTO_HEALTHY_INTERVAL_S = 60.0   # 自动模式：每分钟健康就升一档，几分钟内探到供应商真实容量
FIXED_HEALTHY_INTERVAL_S = 600.0  # 固定上限模式沿用原来的 10 分钟 +1
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
    auto: bool = False
    slow_start: bool = True


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


def _setting_mode(key: str | None, default: int, resource: str) -> tuple[int, bool]:
    """(硬上限, 是否自动)。0/空/auto = 自动，硬上限取安全阀；显式数字 = 固定上限。"""
    if not key:
        return max(1, int(default)), False
    try:
        raw = get_setting(key)
    except Exception:  # noqa: BLE001 设置表未就绪
        raw = None
    text = str(raw or "").strip().lower()
    if text in ("", "0", "auto"):
        return AUTO_CEILINGS.get(resource, max(1, int(default))), True
    return _int_setting(key, default), False


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
        limit, auto = _setting_mode(key, default, resource)
        _channels[resource] = _ChannelState(  # 自动：从均衡档热启动，靠慢启动往上探；固定：直接顶到上限
            name=resource, hard_limit=limit, current=(min(default, limit) if auto else limit), auto=auto,
        )
    return _channels[resource]


def reload_limits_from_settings() -> None:
    """设置变更后即时生效：更新硬上限；当前值向硬上限对齐（升），或立即下调。"""
    for resource, key in SETTING_KEYS.items():
        state = ensure_channel(resource)
        new_hard, auto = _setting_mode(key, CHANNEL_DEFAULTS[resource], resource)
        old_hard = state.hard_limit
        state.hard_limit = new_hard
        state.auto = auto
        if new_hard < state.current:
            state.current = new_hard
            _resize_semaphore(resource)
        elif not auto and new_hard > old_hard and state.current < new_hard and time.time() >= state.cooldown_until:
            # 固定模式：提高硬上限时立刻放开到新上限（健康增长仍由 report_healthy 微调）
            # 自动模式不跳到安全阀——那等于把 30 集的突发直接砸向供应商，让慢启动去探
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
    """连续拥塞：通道并发减半，冷却 60 秒。视频提交与轮询分通道，互不误伤。

    降档必须可见（不许静默限流让人以为系统很闲）：真正触发减半时打一条 WARNING，
    带上通道名、旧/新并发值和触发原因，落进后端运行日志。
    """
    state = ensure_channel(resource)
    if time.time() < state.cooldown_until:
        return  # 冷却期内不再连续减半：一波同时失败（重启掐流、批量取消）只算一次拥塞证据
    state.congestion_hits += 1
    if state.congestion_hits < 2:
        return
    state.congestion_hits = 0
    previous = state.current
    state.current = max(1, state.current // 2)
    state.slow_start = False  # 见过拥塞：此后只加法爬升
    state.cooldown_until = time.time() + 60.0
    state.healthy_since = None
    _resize_semaphore(resource)
    if state.current != previous:
        _LOGGER.warning(
            "concurrency-downgrade resource=%s reason=%s %d->%d cooldown_s=60",
            resource, reason, previous, state.current,
        )


def report_healthy(resource: str) -> None:
    """固定模式：连续 10 分钟健康 +1；自动模式：每分钟健康就升——慢启动翻倍，见过拥塞后 +1，直到安全阀。"""
    state = ensure_channel(resource)
    now = time.time()
    if now < state.cooldown_until:
        return
    state.congestion_hits = 0
    if state.healthy_since is None:
        state.healthy_since = now
        return
    if now - state.healthy_since < (AUTO_HEALTHY_INTERVAL_S if state.auto else FIXED_HEALTHY_INTERVAL_S):
        return
    if state.current < state.hard_limit:
        previous = state.current
        state.current = min(state.hard_limit, state.current * 2 if (state.auto and state.slow_start) else state.current + 1)
        _resize_semaphore(resource)
        _LOGGER.info(
            "concurrency-upgrade resource=%s %d->%d hard_limit=%d",
            resource, previous, state.current, state.hard_limit,
        )
    state.healthy_since = now


# 阈值推导（2026-08-29 实测，本机 2 核 / MemTotal≈3747732 kB，见 /proc/meminfo）：
# - 单个已在跑的后端进程 RSS ≈ 453MB（`ps -o rss` 实测，uvicorn 单进程常驻）；
# - 前一晚十集并发回归已把约 640MB 推入 swap（SwapTotal-SwapFree 实测），说明峰值
#   负载下物理内存缺口至少是这个量级——这台机器已经真实触过底。
# 可用内存（MemAvailable，内核自己算的"还能分配多少而不用换页"，比 MemFree 准，
# 后者不含可回收页缓存）跌破"一个后端进程的常驻体量"时，再挤入任何新的并发工作
# 都会把缺口继续推大、重演那次 swap 挤占；取整到 512MB，在测得的 453MB 之上留一点
# 余量，不卡在测得值上。
MEMORY_AVAILABLE_FLOOR_KB = 512 * 1024


def _available_memory_kb() -> int | None:
    """读 /proc/meminfo 的 MemAvailable；非 Linux 或读取失败时返回 None。

    调用方必须把 None 当"这次不检查"，不得当成"内存充足"——静默假设健康和静默
    限流一样不诚实。
    """
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def memory_pressure_reason() -> str | None:
    """可用内存低于本机实测水位时返回可读原因；充足或无法判断时返回 None。"""
    available_kb = _available_memory_kb()
    if available_kb is None:
        return None
    if available_kb < MEMORY_AVAILABLE_FLOOR_KB:
        return (
            f"available_memory={available_kb // 1024}MB "
            f"< floor={MEMORY_AVAILABLE_FLOOR_KB // 1024}MB"
        )
    return None


def snapshot() -> dict[str, dict]:
    out = {}
    for resource in CHANNEL_DEFAULTS:
        state = ensure_channel(resource)
        out[resource] = {
            "hard_limit": state.hard_limit,
            "current": state.current,
            "cooldown_until": state.cooldown_until,
            "setting_key": SETTING_KEYS.get(resource),
            "auto": state.auto,
            "slow_start": state.slow_start,
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
            set_setting(key, "0")  # 新安装默认自动（0）


