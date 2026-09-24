"""交付编码参数：成片分辨率、编码常量与超时公式的唯一权威来源。

背景：供应商侧自 2026-09-01 19:00（提交 5954e1d，``app/seedance.py`` 显式
``resolution=1080p``）起出片已全部是 1080×1920，但交付链路（``app.final_edit``、
``app.media_exec.concat``、``app.domain.series_ops.merge``）此前把画布写死成
720×1280，把供应商已经给到的分辨率又缩小一次。本模块把「交付用多大画布、
用什么编码参数」收敛成一处，三处调用方都从这里导入，不再各自写死常量。

实测依据（2026-09-02，本机 2 核 / 3 GB 内存 / 无 GPU，负载 load 4.3 下用真实
5 s 源片段测得，原始记录见 ``/tmp/ffbench/bench.log``、
``/tmp/ffbench/bench1080.log``）：

| 原生 1080p 片段（源 4.37 MB/5 s，7.3 Mbps）      | 耗时/5 s | 体积（相对源） |
|---------------------------------------------------|----------|----------------|
| x264 veryfast crf18（旧参数）                      | 5.7 s    | 2.87 MB（−34%）|
| x264 medium crf20（本次拍板：交付编码）            | 12.2 s   | 2.42 MB（−45%）|
| x264 medium crf21                                  | 9.9 s    | 2.12 MB（−51%）|
| x265 medium crf24                                  | 17.5 s   | 1.00 MB        |
| svt-av1 preset8 crf32                              | 12.0 s   | 0.79 MB        |

720p→1080p lanczos 放大再 x264 medium crf20：11.0 s/5 s（约 2.2 倍实时）。

拍板：主成片用 H.264 High、``-preset medium -crf 20``——浏览器通吃、视觉近
无损、比现状（veryfast crf18）小约 20%、比原始供应商产出小约 45%，编码耗时
约 2.5 倍实时。x265 播放兼容差且更慢，AV1 只作为将来可选的「压缩交付版」，
均不在本次范围。中间件（片段级归一化，尚未是最终交付物）改用近无损的
``veryfast -crf 14``，确保最终成片全链路只经历一次有损编码（xfade/concat
路径此前是中间件与最终成片同一档 crf 18，两代有损）。

2026-09-16 修订（交付 crf 20 → 23）：用户报「下载成片特别慢」。先量链路，
结论是最大的可控杠杆在码率不在传输——客户端↔A（大陆↔新加坡跨境，RTT 154 ms）
实测中位 1.3 MB/s、天花板 3.6–4.8 MB/s（受 A 出站带宽封顶），而当时《我欲封天》
ep5 成片 601 s / 593 MB（7.89 Mbps），一集要下七分钟以上。在 B（8 核，nice 19）
上取两部真实成片各 60 s 中段实测：

| 参数              | 《我欲封天》ep5     | 《龙猫出爪》ep1     |
|-------------------|---------------------|---------------------|
| 源（medium crf20）| 40.1 MB / 30.6 s    | 27.1 MB             |
| medium crf23      | 26.8 MB / 27.0 s    | 18.7 MB / 22.3 s    |
| slow   crf23      | 25.9 MB / 38.2 s    | —                   |
| slow   crf26      | 17.5 MB / 32.4 s    | —                   |

取 ``medium crf23``：体积 −31%~−33%，用户对比三档样片后确认画质无差别。没取
slow 是因为它只再省 3.4% 体积却多花 41% 编码时间，而 B 同时还要扛 uvicorn 与
合成任务；crf26 省得更多但用户未选，留档不启用。存量成片一律不自动重编（那是
二次有损），改动只对新成片生效——2026-09-16 只按用户点名把《龙猫出爪_第一季》
前两集单独重编了一次。

这些数字只在与上面机器规格相同或相近的环境下有效；换机器（尤其换 CPU 核数
或加 GPU 编码器）必须重新跑 ``/tmp/ffbench`` 同类基准，不能直接沿用。
"""
from __future__ import annotations

import os

import json
import subprocess
from pathlib import Path

# 2026-09-23 起不再是唯一真源：项目画幅（9:16/16:9）由
# ``app.project_settings.resolve_aspect_ratio``/``canvas_size`` 决定，三个合成入口
# （final_edit/draft_concat/series merge）各自解析一次后显式往下传。这两个常量降级为
# 「9:16 默认值」，仍被 tests/ 与未涉及画幅切换的脚本直接引用，予以保留。
DELIVERY_WIDTH = 1080
DELIVERY_HEIGHT = 1920

# 最终成片：一次性有损编码，H.264 High + medium + crf 23（依据见模块 docstring）。
# 编码线程给机器留两核（2026-09-15 实测：8 核 B 合成时负载 9+，uvicorn 与 ASR 抢不到 CPU）；
# 配合 low_priority() 把 ffmpeg/ASR 子进程 nice 到 10，后端进程的响应优先于重编码。
ENCODE_THREADS = max(2, (os.cpu_count() or 4) - 2)


def low_priority() -> None:
    """subprocess.run(..., preexec_fn=low_priority)：子进程自降 CPU 优先级，不影响父进程。"""
    try:
        os.nice(10)
    except OSError:
        pass


DELIVERY_VIDEO_ARGS = [
    "-c:v", "libx264", "-preset", "medium", "-crf", "23",
    "-pix_fmt", "yuv420p", "-profile:v", "high", "-threads", str(ENCODE_THREADS),
]
# 中间件（片段级归一化，非最终交付物）：近无损，避免与最终编码叠加成两代有损。
INTERMEDIATE_VIDEO_ARGS = [
    "-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-pix_fmt", "yuv420p",
]

# 2026-09-02 在 2 核 A 上实测 medium crf20 约 2.5 倍实时，乘 1.6 倍余量后取整；
# 2026-09-16 在 8 核 B 上实测 medium crf23 仅 0.37 倍实时，余量只多不少，沿用 4.0。
DELIVERY_ENCODE_REALTIME_FACTOR = 4.0


def encode_timeout_s(total_duration_s: float) -> float:
    """交付编码的超时上限：下限 5 分钟，上限 4 小时（多集连播本就该跑很久）。

    旧上限 ``min(1800, total*10+60)`` 是按 veryfast crf18 的粗略估算定的 30
    分钟顶格，在 medium crf20 下会把长成片直接判超时——抬到 4 小时不是放松
    校验，是纠正一个已经不成立的旧假设。
    """
    return min(4 * 3600.0, max(300.0, total_duration_s * DELIVERY_ENCODE_REALTIME_FACTOR + 120.0))


def canvas_filter(width: int, height: int, *, flags: str = "lanczos") -> str:
    """交付画布的标准 scale+crop 滤镜链：等比放大铺满后居中裁切到交付分辨率。

    ``width``/``height`` 必须由调用方显式传入（通常是
    ``app.project_settings.canvas_size(resolve_aspect_ratio(...))`` 的结果）——
    本函数不再兜底默认 ``DELIVERY_WIDTH``/``DELIVERY_HEIGHT``，漏传在调用时就是
    ``TypeError``，不会静默产出 1080x1920。
    """
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=increase:"
        f"flags={flags},crop={width}:{height}"
    )


def scale_px(canvas_width: int, value: float) -> int:
    """把写死在 720 宽画布上的像素数按 ``canvas_width/720`` 缩放到当前交付画布。"""
    return max(1, round(value * canvas_width / 720))


def scale_box(canvas_width: int, *values: float) -> tuple[int, ...]:
    return tuple(scale_px(canvas_width, value) for value in values)


def _probe_video_stream(path: str | Path) -> dict[str, object]:
    """探测失败（非法/不可解码媒体、ffprobe 缺失等）一律返回 {}，不向上抛异常：
    分辨率/编码探测是尽力而为的元信息，不该让整个发布流程因为一次探测失败中断。
    """
    try:
        raw = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height,codec_name",
                "-of", "json", str(path),
            ],
            check=True, capture_output=True, text=True, timeout=30,
        ).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return {}
    try:
        streams = json.loads(raw or "{}").get("streams") or []
    except ValueError:
        return {}
    return streams[0] if isinstance(streams, list) and streams else {}


def probe_resolution(path: str | Path) -> tuple[int, int]:
    """用 ffprobe 读取媒体文件的真实视频分辨率（宽, 高）；探测失败返回 (0, 0)。"""
    stream = _probe_video_stream(path)
    try:
        return int(stream.get("width") or 0), int(stream.get("height") or 0)
    except (TypeError, ValueError):
        return 0, 0


def probe_video_codec(path: str | Path) -> str:
    """用 ffprobe 读取媒体文件实际使用的视频编码（如 ``h264``）；探测失败返回空串。"""
    stream = _probe_video_stream(path)
    return str(stream.get("codec_name") or "")


def uniform_resolution(paths: list[str] | list[Path]) -> tuple[int, int] | None:
    """全部片段分辨率一致才返回该尺寸；不一致或探测失败返回 None。

    空列表也返回 None——空不等于「无需检查」，调用方不应把它当「已确认一致」
    使用。
    """
    if not paths:
        return None
    resolutions = {probe_resolution(path) for path in paths}
    if len(resolutions) != 1:
        return None
    only = next(iter(resolutions))
    return only if only != (0, 0) else None
