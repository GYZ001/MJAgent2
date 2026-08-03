from __future__ import annotations

try:
    _queue
except NameError:  # pragma: no cover - used when importing this module directly
    from app.media_exec.common import *

def episode_mix_status(episode_id: str) -> dict:
    """返回每镜采纳片段及合成状态；至少一个可用片段即可合成。"""
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        return {"ready": False, "shots_total": 0, "shots_ready": 0, "shots": []}
    shots = rows_to_dicts(conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)).fetchall())
    ready = 0
    out = []
    for s in shots:
        vid = None
        v = None
        if s["adopted_version_id"]:
            v = conn.execute(
                "SELECT * FROM shot_versions WHERE id=? AND status='succeeded'",
                (s["adopted_version_id"],)).fetchone()
            if v and v["video_path"] and Path(v["video_path"]).is_file():
                from app.config import PROJECTS_DIR
                try:
                    rel_path = Path(v["video_path"]).relative_to(PROJECTS_DIR).as_posix()
                except ValueError:
                    rel_path = None
                if rel_path:
                    vid = f"/media/{rel_path}"
                    ready += 1
        playback_rate = float(v["playback_rate"] or 1.0) if vid and v else 1.0
        out.append({"shot_id": s["id"], "shot_no": s["shot_no"],
                    "duration_s": s["duration_s"], "video_url": vid,
                    "has_adopted": bool(vid), "playback_rate": playback_rate,
                    "effective_duration_s": round(float(s["duration_s"] or 0) / playback_rate, 2)})
    skipped_shot_nos = [item["shot_no"] for item in out if not item["has_adopted"]]
    return {
        "episode_id": ep["id"],
        "title": ep["title"],
        "episode_no": ep["episode_no"],
        "shots_total": len(shots),
        "shots_ready": ready,
        # ready 表示“可发起合成”，而不再表示所有分镜已齐。
        "ready": ready > 0,
        "all_ready": len(shots) > 0 and ready == len(shots),
        "shots_skipped": len(skipped_shot_nos),
        "skipped_shot_nos": skipped_shot_nos,
        "final_video_url": _existing_final_url(ep),
        "shots": out,
    }


def _existing_final_url(ep_row) -> str | None:
    from app.config import PROJECTS_DIR
    final_path = _final_video_path(ep_row["project_id"], ep_row["episode_no"])
    if final_path.exists():
        rel_path = final_path.relative_to(PROJECTS_DIR).as_posix()
        return f"/media/{rel_path}"
    return None


def _final_video_path(project_id: str, episode_no: int) -> Path:
    d = config.PROJECTS_DIR / project_id / "episodes" / str(episode_no) / "final"
    d.mkdir(parents=True, exist_ok=True)
    return d / "episode.mp4"


def concatenate_episode(episode_id: str) -> dict:
    """把本集可用的已采纳镜头按镜号拼接成 MP4，未采纳镜头直接跳过。
    返回 {video_url, shots, total_duration_s}。系统未装 ffmpeg 时明确失败，不返回伪成片。
    """
    from pathlib import Path as _P
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        raise ValueError("剧集不存在")
    pieces = _adopted_video_paths(episode_id)
    if not pieces:
        raise ValueError("本集没有任何已采用的视频片段，先生成/采用后再试")

    from app.video_playback import normalize_playback_rate

    rate_rows = conn.execute(
        """SELECT s.shot_no, v.playback_rate
           FROM shots s JOIN shot_versions v ON v.id=s.adopted_version_id
           WHERE s.episode_id=?""",
        (episode_id,),
    ).fetchall()
    rate_by_shot = {
        int(row["shot_no"]): normalize_playback_rate(row["playback_rate"])
        for row in rate_rows
    }
    piece_specs = [
        (int(shot_no), path, rate_by_shot.get(int(shot_no), 1.0))
        for shot_no, path in pieces
    ]
    piece_shot_nos = [shot_no for shot_no, _path, _rate in piece_specs]
    all_shot_nos = [
        int(row["shot_no"])
        for row in conn.execute(
            "SELECT shot_no FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,),
        ).fetchall()
    ]
    skipped_shot_nos = [shot_no for shot_no in all_shot_nos if shot_no not in set(piece_shot_nos)]
    # 拿不到实测时长时，只累加本次真正参与拼接的镜头时长。
    duration_by_shot = {
        int(row["shot_no"]): float(row["duration_s"] or 0)
        for row in conn.execute(
            "SELECT shot_no,duration_s FROM shots WHERE episode_id=?", (episode_id,),
        ).fetchall()
    }
    est_total_dur = sum(
        duration_by_shot.get(shot_no, 0.0) / rate
        for shot_no, _path, rate in piece_specs
    )
    concat_timeout_s = min(1800.0, max(120.0, est_total_dur * 10.0 + 60.0))

    final_path = _final_video_path(ep["project_id"], ep["episode_no"])
    if not shutil.which("ffmpeg"):
        raise ValueError(
            "服务端未找到视频合成组件 ffmpeg；请安装 ffmpeg 或修正服务启动 PATH 后重试，"
            "本次未生成成片"
        )

    # 用 concat demuxer 优先无重编码直粘（画质无损）；但 -c copy 要求各片段编码参数
    # （像素格式/timebase/SAR/profile）完全一致，否则会失败或花屏。一旦失败，回退重编码兜底。
    with tempfile.TemporaryDirectory() as td:
        listfile = _P(td) / "list.txt"
        lines = []
        prepared_specs: list[tuple[int, str, float]] = []
        for shot_no, vpath, rate in piece_specs:
            prepared_path = vpath
            if abs(rate - 1.0) > 0.0001:
                sped_path = _P(td) / f"shot-{shot_no}-x{rate:.2f}.mp4"
                speed_cmd = [
                    "ffmpeg", "-y", "-loglevel", "error", "-i", vpath,
                    "-map", "0:v:0", "-map", "0:a?",
                    "-vf", f"setpts=PTS/{rate:.6f}",
                    "-af", f"atempo={rate:.6f}",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart",
                    str(sped_path),
                ]
                try:
                    subprocess.run(
                        speed_cmd, check=True, capture_output=True, timeout=concat_timeout_s,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise ValueError(
                        f"镜 {shot_no} 的 {rate:g} 倍速定稿处理超时；上一版成片仍保留，可稍后重试"
                    ) from exc
                except subprocess.CalledProcessError as exc:
                    detail = (exc.stderr or b"").decode("utf-8", "replace")[-500:]
                    raise ValueError(
                        f"镜 {shot_no} 的 {rate:g} 倍速定稿处理失败；上一版成片仍保留"
                        + (f"：{detail}" if detail else "")
                    ) from exc
                if not sped_path.is_file() or sped_path.stat().st_size <= 0:
                    raise ValueError(
                        f"镜 {shot_no} 的 {rate:g} 倍速定稿未产出有效片段；上一版成片仍保留"
                    )
                prepared_path = str(sped_path)
            prepared_specs.append((shot_no, prepared_path, rate))
            # concat demuxer 要求绝对路径并转义单引号
            safe = prepared_path.replace("'", "'\\''")
            lines.append(f"file '{safe}'")
        listfile.write_text("\n".join(lines), encoding="utf-8")
        silent_video = _P(td) / "concat.mp4"
        concat_in = ["ffmpeg", "-y", "-loglevel", "error",
                     "-f", "concat", "-safe", "0", "-i", str(listfile)]
        try:
            subprocess.run(
                concat_in + ["-c", "copy", "-movflags", "+faststart", str(silent_video)],
                check=True, capture_output=True, timeout=concat_timeout_s)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            # 片段编码参数不一致导致 -c copy 失败 → 重编码兜底（画质损失极小，但保证能拼成整集）
            try:
                subprocess.run(
                    concat_in + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                                 "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(silent_video)],
                    check=True, capture_output=True, timeout=concat_timeout_s)
            except subprocess.TimeoutExpired as exc:
                raise ValueError(
                    f"整集合成超过 {int(concat_timeout_s)} 秒，已停止本次任务；上一版成片仍保留，可稍后重试"
                ) from exc
            except subprocess.CalledProcessError as exc:
                detail = (exc.stderr or b"").decode("utf-8", "replace")[-500:]
                raise ValueError(
                    "整集合成失败，上一版成片仍保留，可检查片段后重试"
                    + (f"：{detail}" if detail else "")
                ) from exc
        if not silent_video.is_file() or silent_video.stat().st_size <= 0:
            raise ValueError("ffmpeg 未产出有效成片，上一版成片仍保留，可检查片段后重试")
        atomic_copy(silent_video, final_path)

    total_dur = 0
    try:
        for shot_no, vpath, rate in piece_specs:
            raw = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", vpath], capture_output=True, text=True, check=True,
                timeout=30,
            ).stdout.strip()
            total_dur += (float(raw) / rate) if raw else 0
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, ValueError):
        total_dur = est_total_dur or config.DEFAULT_VIDEO_DURATION_S * len(pieces)

    from app.config import PROJECTS_DIR
    rel_path = final_path.relative_to(PROJECTS_DIR).as_posix()
    return {
        "video_url": f"/media/{rel_path}",
        "shots": len(pieces),
        "total_duration_s": round(total_dur, 1),
        "ffmpeg_missing": False,
        "shots_total": len(all_shot_nos),
        "shots_skipped": len(skipped_shot_nos),
        "skipped_shot_nos": skipped_shot_nos,
        "playback_rates": {str(no): rate for no, _path, rate in piece_specs},
    }

__all__ = [name for name in globals() if not name.startswith("__")]
