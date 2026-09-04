"""上一段结尾画面作空间参考（试验开关 ``video_prev_frame_reference``，默认关闭）。

放在 app.video_plan 而不是 app.video_modes：video_modes 包的 __init__ 会导入 mode_selection，后者
模块级导入 app.video_plan，generate.py 若反过来导入 video_modes 就成环（实测 5 个测试模块收集期 ImportError）。
本模块只依赖 app.db，是叶子。

用户投诉（2026-09-03，橘座在上 EP1）：相邻两段 15 秒视频之间物件形态与空间位置漂移——
猫一会儿在车底一会儿在后备箱。每段都走参考图模式、彼此独立生成，人物靠定妆照锁住了，
空间布局却没有任何图像锚点。

做法（用户拍板：**不用首尾帧模式**，只把上一段的末帧当一张普通参考图）：
- 同一场戏的后续段（判定复用 ``normalize.apply_scene_boundary_strategy`` 的 scene-entry 分类）在视频
  计划里挂 ``depends_on_shot_id`` = 上一段镜头，``state_dependency=start_only``；
- 参考图装配阶段等上一段视频成功后抽末帧，作为 ``previous_shot_frame`` 类型的参考图
  挂入（既有 ``continuity_tail.assemble_continuity_tail`` 机器），定妆照与场景图照旧同时
  附上，所以身份仍由定妆照负责，不会重蹈只靠尾帧起画的漂移；
- 同一场戏内因此串行，不同场戏之间仍并行。
开关关闭时以上三处都不生效，行为与 2.4.0 完全一致。
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from app.db import get_setting

SETTING_KEY = "video_prev_frame_reference"

#: 打包进提示词的参考图用途说明：只锁布局与物件，不锁人物姿势。
PREVIOUS_FRAME_PURPOSE_ZH = (
    "上一段{who}画面参考，只用来锁定场景布局、家具与关键道具的位置和形态；"
    "人物的姿势与动作按本段文字描述，不沿用这张图"
)
#: 每段最多带几张上一段画面（上一段 2-4 镜，取 3 张：与定妆照 ≤4 + 场景图 1 合计不超过 9 张上限）。
MAX_PREVIOUS_FRAMES = 3
#: ffmpeg 场景切换检测阈值与两个切点之间的最小间隔（秒）：内切镜头至少两秒。
SCENE_CUT_THRESHOLD = 0.35
MIN_CUT_GAP_S = 2.0
_PTS_RE = re.compile(r"pts_time:\s*([0-9]+(?:\.[0-9]+)?)")


def prev_frame_reference_enabled() -> bool:
    """默认启用（2026-09-04 用户看过串接成片后拍板进主线）；settings 表里
    ``video_prev_frame_reference`` 显式写 0 才关闭，空或非数字按默认开。"""
    raw = str(get_setting(SETTING_KEY) or "").strip()
    if not raw:
        return True
    try:
        return int(raw) != 0
    except ValueError:
        return True


def video_duration_s(video_path: str) -> float | None:
    if not shutil.which("ffprobe"):
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", video_path],
            capture_output=True, text=True, timeout=30, check=False,
        ).stdout.strip()
        return float(out) if out else None
    except (subprocess.SubprocessError, ValueError, OSError):
        return None


def parse_showinfo_cut_times(stderr_text: str, *, duration_s: float) -> list[float]:
    """从 ffmpeg showinfo 输出里取切点时间：去掉贴近首尾的、合并间隔小于 MIN_CUT_GAP_S 的。"""
    cuts: list[float] = []
    for raw in _PTS_RE.findall(stderr_text or ""):
        ts = float(raw)
        if ts < MIN_CUT_GAP_S / 2 or ts > duration_s - MIN_CUT_GAP_S / 2:
            continue
        if cuts and ts - cuts[-1] < MIN_CUT_GAP_S:
            continue
        cuts.append(ts)
    return cuts


def detect_internal_cuts(video_path: str, *, duration_s: float) -> list[float]:
    """用 ffmpeg 场景切换检测找上一段内部的硬切点；ffmpeg 不可用或失败返回空列表（走三等分回退）。"""
    if not shutil.which("ffmpeg"):
        return []
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", video_path,
             "-vf", f"select='gt(scene,{SCENE_CUT_THRESHOLD})',showinfo", "-f", "null", "-"],
            capture_output=True, text=True, timeout=120, check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    return parse_showinfo_cut_times(proc.stderr, duration_s=duration_s)


def sample_timestamps(duration_s: float, cuts: list[float], *, max_frames: int = MAX_PREVIOUS_FRAMES) -> list[float]:
    """每个内切镜头取中点一帧；检测不到切点就按三等分取中点；镜头多于上限时保留最长的几段（按时间排序）。"""
    if duration_s <= 0:
        return []
    bounds = [0.0, *[c for c in cuts if 0 < c < duration_s], duration_s]
    segments = [(a, b) for a, b in zip(bounds, bounds[1:]) if b - a > 0]
    if len(segments) < 2:
        step = duration_s / max_frames
        segments = [(i * step, (i + 1) * step) for i in range(max_frames)]
    if len(segments) > max_frames:
        segments = sorted(sorted(segments, key=lambda seg: seg[1] - seg[0], reverse=True)[:max_frames])
    return [round((a + b) / 2, 2) for a, b in segments]


def extract_frame(video_path: str, ts: float, dest: Path) -> bool:
    if not shutil.which("ffmpeg"):
        return False
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{ts:.2f}", "-i", video_path,
             "-vframes", "1", "-q:v", "3", str(dest)],
            capture_output=True, timeout=60, check=False,
        )
        return dest.is_file() and dest.stat().st_size > 0
    except (subprocess.SubprocessError, OSError):
        return False


def sample_previous_segment_frames(video_path: str, dest_dir: Path, signature: str) -> list[tuple[int, Path]]:
    """上一段视频 → [(镜序号 1 起, 帧文件)]；已抽过的帧直接复用（按来源签名命名）。"""
    duration = video_duration_s(video_path)
    if not duration:
        return []
    cuts = detect_internal_cuts(video_path, duration_s=duration)
    dest_dir.mkdir(parents=True, exist_ok=True)
    frames: list[tuple[int, Path]] = []
    for index, ts in enumerate(sample_timestamps(duration, cuts), start=1):
        dest = dest_dir / f"0{index}_previous_frame_{signature}.jpg"
        if (dest.is_file() and dest.stat().st_size > 0) or extract_frame(video_path, ts, dest):
            frames.append((index, dest))
    return frames


def planned_previous_shot_id(conn: Any, shot_id: str) -> str | None:
    """入队时的链依赖来源：非叙事权威集不加载视频计划（``enqueue_context.resolve_mode_decision``
    刻意丢弃模型规划的模式/依赖），开关打开时单独从已发布计划里取本镜的 ``depends_on_shot_id``
    ——这是 ``apply_scene_boundary_strategy`` 写进去的同场戏上一段，不是模型的自由声明。
    EP1 对比跑实测：不取这一步，调度按计划串行了，但 ``jobs.after_shot_id`` 为空，参考图装配
    阶段不知道上一段是谁，一张画面都没挂上。"""
    if not prev_frame_reference_enabled():
        return None
    # 同包内延迟导入：本模块是 app.db 之上的叶子，mode_attempt 反过来依赖整个包的初始化链。
    from app.video_plan.mode_attempt import get_shot_plan

    plan = get_shot_plan(shot_id, conn=conn)
    if plan is None or not plan.depends_on_shot_id:
        return None
    return str(plan.depends_on_shot_id)
