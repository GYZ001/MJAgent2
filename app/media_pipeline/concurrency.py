"""分池并发 + 自适应限流 + 可升降 worker 池。"""
from __future__ import annotations

import asyncio
import collections
import logging
import statistics
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
    # 视频在途安全阀 32：实测在途 15 时供应商单任务 p50 7.8 分钟，128 时涨到 20-38 分钟且吞吐只涨 1.7 倍
    # ——远在 128 之前就劣化了。32 是保守阀门（已知健康档位的两倍余量），真实水位仍由上面的自适应发现。
    S.RESOURCE_VIDEO_INFLIGHT: 32, S.RESOURCE_VIDEO_POLL: 128, S.RESOURCE_DOWNLOAD: 16, S.RESOURCE_FINALIZE: 16,
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




def report_video_submit_congestion(*, reason: str) -> None:
    """提交侧遭遇拥塞时同时给提交通道与在途通道降档：两者面对同一个供应商，
    在途水位过高正是提交被拒的原因之一（2026-09-07 第 15 轮：在途通道从没人上报过成败）。"""
    for resource in (S.RESOURCE_VIDEO_SUBMIT, S.RESOURCE_VIDEO_INFLIGHT):
        report_congestion(resource, reason=reason)


# 交付延迟是这条通道唯一的拥塞信号：供应商对超发不回 429、不报错，只是**变慢**
# （2026-09-07 实测：在途 15 时单任务 p50 7.8 分钟，拉到 128 后 p50 24.8、p99 45.9，
# 吞吐只涨 1.7 倍而延迟涨 3 倍——过饱和都排在供应商内部）。只认错误的 AIMD 因此会一路
# 顶到安全阀。这里用「当前中位数 / 历史最好中位数」判饱和：超过 LATENCY_CONGESTION_FACTOR
# 就当拥塞降档，让通道停在供应商真实容量附近，而不是名义上限。
LATENCY_SAMPLE_WINDOW = 20
LATENCY_CONGESTION_FACTOR = 2.0
# 参照值取实测的「不拥挤时单镜要多久」，不跟自己学：2026-09-07 B 库实测，在途 15 时
# 提交→完成 p50 7.8 分钟（395 个样本），拉到 128 后 p50 涨到 20-38 分钟。
# 跟自己学的基线（历史最好中位数）两次都被污染——第一次是通道在基线成型前就冲到安全阀，
# 样本本身是饱和耗时；第二次是重启后最先完成的那批任务带着重启前的排队时间。参照一个
# 实测常量就不会被样本到达顺序左右。
HEALTHY_DELIVERY_S = 480.0
_delivery_latencies: collections.deque[float] = collections.deque(maxlen=LATENCY_SAMPLE_WINDOW)


def delivery_latency_verdict(duration_s: float) -> str:
    """记一次交付耗时并给出 ``"healthy"`` / ``"congested"`` / ``"unknown"``（样本不足）。"""
    if duration_s > 0:
        _delivery_latencies.append(float(duration_s))
    if len(_delivery_latencies) < LATENCY_SAMPLE_WINDOW // 2:
        return "unknown"
    median = statistics.median(_delivery_latencies)
    return "congested" if median > HEALTHY_DELIVERY_S * LATENCY_CONGESTION_FACTOR else "healthy"


def report_video_delivered(duration_s: float = 0.0) -> None:
    """供应商在当前在途水位下交付了一个视频。

    交付本身是升档证据（此前 ``video_inflight`` 只被 ``channel_limit`` 读取、没有任何调用点
    上报成败，慢启动永远停在热启动值 15，550 个镜头排了三个多小时）；但交付**变慢**同样是
    证据——见上方 ``LATENCY_CONGESTION_FACTOR``，饱和时降档而不是继续往上顶。

    样本不足以判断快慢时**按兵不动**，既不升也不降：2026-09-07 实测，把「未知」当成健康会让
    通道在基线成型之前就翻倍冲到安全阀 128，等攒够样本时这些样本本身已是饱和耗时（中位 19.9
    分钟，而低并发档位只要 7.8），基线被记成饱和值，此后再慢也超不过它的 2 倍——闭环就此失灵。
    先在热启动档位攒出一个真实的「快」基线，再开始往上探。
    """
    verdict = delivery_latency_verdict(duration_s)
    if verdict == "unknown":
        return
    if verdict == "congested":
        report_congestion(S.RESOURCE_VIDEO_INFLIGHT, reason="delivery_latency")
        _delivery_latencies.clear()  # 旧样本描述的是降档前的水位，留着会连环降档一路砍到 1
        return
    report_healthy(S.RESOURCE_VIDEO_INFLIGHT)
