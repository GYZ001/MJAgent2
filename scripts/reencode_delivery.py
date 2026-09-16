#!/usr/bin/env python3
"""按当前交付编码参数重新编码已生成的成片（原子替换）。

用途：交付档位（``app.media_pipeline.delivery_encode.DELIVERY_VIDEO_ARGS``）调整后，
存量成片不会自动跟进——新参数只对之后生成的成片生效。个别成片需要让存量也享受新
档位时，用这个脚本点名重编。2026-09-16 把交付 crf 20 调到 23 后，用户点名重编了
《龙猫出爪_第一季》前两集，就是这个脚本的第一次使用。

**这是二次有损编码**：源本身已经是一次交付编码的产物，重编等于再压一代。所以故意
不做「扫全仓自动重编」——必须 ``--project`` + ``--episodes`` 点名，范围由人来定。

原子性：先编到同目录的 ``.reencode.tmp.mp4``，校验通过（时长对得上、音视频流齐全、
分辨率不变、体积确实减小）之后才替换原文件；任何一步失败，原文件原封不动。
音频一律 ``-c:a copy``——重编音频只会平白多一代损失，省不下多少体积。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402
from app.media_pipeline.delivery_encode import (  # noqa: E402
    DELIVERY_VIDEO_ARGS, encode_timeout_s, low_priority,
)

# 时长容差：重编不改时间轴，对不上就是出了别的问题，不能放行。
_DURATION_TOLERANCE_S = 0.5


def _ffmpeg_bin(name: str) -> str:
    """B 上 ffmpeg 装在 /opt/ffmpeg-static，非交互 ssh 的 PATH 里不一定有。"""
    found = shutil.which(name)
    if found:
        return found
    static = Path("/opt/ffmpeg-static") / name
    if static.exists():
        return str(static)
    raise SystemExit(f"找不到 {name}（PATH 与 /opt/ffmpeg-static 都没有）")


def _probe(path: Path) -> dict:
    out = subprocess.run(
        [_ffmpeg_bin("ffprobe"), "-v", "error", "-show_entries",
         "format=duration,size", "-show_entries", "stream=codec_type,width,height",
         "-of", "json", str(path)],
        capture_output=True, text=True, timeout=120, check=True,
    ).stdout
    data = json.loads(out)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    return {
        "duration": float(data.get("format", {}).get("duration") or 0.0),
        "size": int(data.get("format", {}).get("size") or 0),
        "width": video.get("width"),
        "height": video.get("height"),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
    }


def _encode(src: Path, dest: Path, duration_s: float) -> None:
    subprocess.run(
        [_ffmpeg_bin("ffmpeg"), "-y", "-loglevel", "error", "-i", str(src),
         *DELIVERY_VIDEO_ARGS, "-c:a", "copy", "-movflags", "+faststart", str(dest)],
        check=True, timeout=encode_timeout_s(duration_s), preexec_fn=low_priority,
    )


def _verify(before: dict, after: dict) -> str | None:
    """返回拒绝替换的理由；None 表示产物可用。"""
    if abs(after["duration"] - before["duration"]) > _DURATION_TOLERANCE_S:
        return f"时长对不上（原 {before['duration']:.2f}s，新 {after['duration']:.2f}s）"
    if (after["width"], after["height"]) != (before["width"], before["height"]):
        return f"分辨率变了（原 {before['width']}x{before['height']}，新 {after['width']}x{after['height']}）"
    if before["has_audio"] and not after["has_audio"]:
        return "音轨丢了"
    if after["size"] >= before["size"]:
        return f"体积没减小（原 {before['size']}，新 {after['size']}），重编无收益"
    return None


def reencode_one(path: Path, *, dry_run: bool) -> bool:
    before = _probe(path)
    label = f"{path.parent.parent.name}/{path.name}"
    print(f"  {label}: {before['size'] / 1048576:.1f} MB / {before['duration']:.1f}s", flush=True)
    if dry_run:
        return True
    tmp = path.with_suffix(".reencode.tmp.mp4")
    tmp.unlink(missing_ok=True)
    try:
        _encode(path, tmp, before["duration"])
        after = _probe(tmp)
        reason = _verify(before, after)
        if reason:
            print(f"    拒绝替换：{reason}", flush=True)
            return False
        # 原文件在这一刻之前始终完好；replace 在同一文件系统内是原子的。
        tmp.replace(path)
        saved = 100.0 * (1 - after["size"] / before["size"])
        print(f"    已替换 -> {after['size'] / 1048576:.1f} MB（−{saved:.0f}%）", flush=True)
        return True
    finally:
        tmp.unlink(missing_ok=True)


def _final_videos(project_id: str, episodes: list[int]) -> list[Path]:
    paths = []
    for ep in episodes:
        path = Path(config.PROJECTS_DIR) / project_id / "episodes" / str(ep) / "final" / "episode.mp4"
        if not path.is_file():
            raise SystemExit(f"第 {ep} 集成片不存在：{path}")
        paths.append(path)
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", required=True, help="项目 id，例如 proj_f4c8ca4a5775")
    parser.add_argument("--episodes", required=True, help="集号，逗号分隔，例如 1,2")
    parser.add_argument("--dry-run", action="store_true", help="只列出将要重编的成片，不动文件")
    args = parser.parse_args()

    episodes = [int(x) for x in args.episodes.split(",") if x.strip()]
    paths = _final_videos(args.project, episodes)
    crf = DELIVERY_VIDEO_ARGS[DELIVERY_VIDEO_ARGS.index("-crf") + 1]
    preset = DELIVERY_VIDEO_ARGS[DELIVERY_VIDEO_ARGS.index("-preset") + 1]
    print(f"目标档位：x264 {preset} crf{crf}（来自 DELIVERY_VIDEO_ARGS）")
    print(f"待处理 {len(paths)} 个成片：")
    failed = [p for p in paths if not reencode_one(p, dry_run=args.dry_run)]
    if failed:
        print(f"失败 {len(failed)} 个，原文件均未改动", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
