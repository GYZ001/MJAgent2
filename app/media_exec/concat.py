from __future__ import annotations

import math
import hashlib

try:
    _queue
except NameError:  # pragma: no cover - used when importing this module directly
    from app.media_exec.common import *


_ACTIVE_VIDEO_JOB_STATUSES = ("queued", "running", "waiting_provider", "waiting_retry")
_CONCAT_PROBE_TIMEOUT_S = 30.0
_CONCAT_DURATION_TOLERANCE_RATIO = 0.10
_CONCAT_DURATION_TOLERANCE_MIN_S = 0.75


def _media_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _publish_concat_output(
    conn,
    *,
    episode_id: str,
    candidate_path: Path,
    final_path: Path,
    report: dict[str, Any],
    release_authority: dict[str, Any],
    video_delivery_manifest: dict[str, Any],
) -> None:
    """Serialize adoption changes with final video/report/stale publication."""
    from app.atomic_io import atomic_write_text
    from app.downstream_authority import (
        current_adopted_video_delivery_manifest,
        verify_current_storyboard_release_authority,
    )

    if conn.in_transaction:
        conn.commit()
    owner = new_id("concatpub")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS episode_video_publish_leases(
                   episode_id TEXT PRIMARY KEY,owner TEXT NOT NULL,
                   video_manifest_hash TEXT NOT NULL,status TEXT NOT NULL,updated_at REAL NOT NULL
               )"""
        )
        conn.execute(
            """INSERT INTO episode_video_publish_leases(
                   episode_id,owner,video_manifest_hash,status,updated_at
               ) VALUES(?,?,?,'publishing',?)
               ON CONFLICT(episode_id) DO UPDATE SET owner=excluded.owner,
                   video_manifest_hash=excluded.video_manifest_hash,
                   status='publishing',updated_at=excluded.updated_at""",
            (episode_id, owner, video_delivery_manifest["manifest_hash"], now()),
        )
        if verify_current_storyboard_release_authority(
            episode_id, conn=conn,
        ) != release_authority:
            raise ValueError("合片发布前分镜权威发生漂移")
        if current_adopted_video_delivery_manifest(
            episode_id, conn=conn,
        ) != video_delivery_manifest:
            raise ValueError("合片发布前已采纳视频发生漂移")
        report["final_video_sha256"] = _media_sha256(candidate_path)
        atomic_copy(candidate_path, final_path)
        atomic_write_text(
            _edit_report_path(final_path),
            json.dumps(report, ensure_ascii=False, indent=2),
        )
        final_path.with_suffix(".stale").unlink(missing_ok=True)
        updated = conn.execute(
            """UPDATE episode_video_publish_leases SET status='published',updated_at=?
                 WHERE episode_id=? AND owner=? AND status='publishing'""",
            (now(), episode_id, owner),
        )
        if updated.rowcount != 1:
            raise ValueError("合片 publish owner CAS 冲突")
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def _probe_concat_media(path: str | Path) -> dict[str, Any]:
    media_path = Path(path)
    if not media_path.is_file() or media_path.stat().st_size <= 0:
        raise ValueError("文件不存在或为空")
    try:
        completed = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=format_name,duration:stream=codec_type",
                "-of", "json", str(media_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=_CONCAT_PROBE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(
            f"ffprobe 超过 {int(_CONCAT_PROBE_TIMEOUT_S)} 秒"
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()[-500:]
        raise ValueError("ffprobe 无法读取容器" + (f"：{detail}" if detail else "")) from exc
    except OSError as exc:
        raise ValueError(f"ffprobe 无法执行：{exc}") from exc
    try:
        payload = json.loads(completed.stdout or "{}")
        fmt = payload.get("format") or {}
        format_name = str(fmt.get("format_name") or "").strip().lower()
        duration_s = float(fmt.get("duration"))
        streams = payload.get("streams") or []
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("ffprobe 返回了无效的容器元数据") from exc
    if "mp4" not in format_name.split(","):
        raise ValueError(f"容器不是 MP4（format_name={format_name or 'unknown'}）")
    if not any(
        isinstance(stream, dict) and stream.get("codec_type") == "video"
        for stream in streams
    ):
        raise ValueError("容器中没有视频流")
    if not math.isfinite(duration_s) or duration_s <= 0:
        raise ValueError(f"容器时长无效（duration={duration_s!r}）")
    return {"duration_s": duration_s, "format_name": format_name}


def _validate_concat_output(
    path: str | Path,
    *,
    expected_duration_s: float,
    decode_timeout_s: float,
) -> float:
    try:
        probe = _probe_concat_media(path)
    except ValueError as exc:
        raise ValueError(
            f"合片产物容器/时长校验失败，上一版成片仍保留：{exc}"
        ) from exc
    if not math.isfinite(expected_duration_s) or expected_duration_s <= 0:
        raise ValueError("无法确定合片预期时长，上一版成片仍保留")
    actual_duration_s = float(probe["duration_s"])
    tolerance_s = max(
        _CONCAT_DURATION_TOLERANCE_MIN_S,
        expected_duration_s * _CONCAT_DURATION_TOLERANCE_RATIO,
    )
    if abs(actual_duration_s - expected_duration_s) > tolerance_s:
        raise ValueError(
            "合片产物时长异常，上一版成片仍保留："
            f"实测 {actual_duration_s:.3f}s，预期 {expected_duration_s:.3f}s，"
            f"允许误差 {tolerance_s:.3f}s"
        )
    try:
        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-v", "error", "-xerror",
                "-i", str(path), "-map", "0:v:0", "-f", "null", "-",
            ],
            check=True,
            capture_output=True,
            timeout=decode_timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(
            f"合片产物完整解码超过 {int(decode_timeout_s)} 秒，上一版成片仍保留"
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", "replace").strip()[-500:]
        raise ValueError(
            "合片产物完整解码失败，上一版成片仍保留"
            + (f"：{detail}" if detail else "")
        ) from exc
    except OSError as exc:
        raise ValueError(f"合片产物无法执行完整解码，上一版成片仍保留：{exc}") from exc
    return actual_duration_s


def _active_generation_shot_nos(conn, episode_id: str) -> list[int]:
    placeholders = ",".join("?" for _ in _ACTIVE_VIDEO_JOB_STATUSES)
    rows = conn.execute(
        f"""SELECT DISTINCT s.shot_no
              FROM jobs j JOIN shots s ON s.id=j.shot_id
             WHERE s.episode_id=? AND j.status IN ({placeholders})
             ORDER BY s.shot_no""",
        (episode_id, *_ACTIVE_VIDEO_JOB_STATUSES),
    ).fetchall()
    return [int(row["shot_no"]) for row in rows]


def _is_delivery_fallback(row) -> bool:
    if row is None:
        return False
    try:
        meta = json.loads(row["image_inputs"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return bool(isinstance(meta, dict) and meta.get("delivery_fallback"))


def _playable_model_candidate(conn, shot_id: str):
    rows = conn.execute(
        """SELECT * FROM shot_versions
           WHERE shot_id=? AND status='succeeded' AND video_path IS NOT NULL
           ORDER BY version_no DESC""",
        (shot_id,),
    ).fetchall()
    return next(
        (
            row for row in rows
            if not _is_delivery_fallback(row)
            and row["video_path"]
            and Path(row["video_path"]).is_file()
        ),
        None,
    )


def episode_mix_status(episode_id: str) -> dict:
    """返回当前已有真实视频的可合成状态。"""
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        return {"ready": False, "shots_total": 0, "shots_ready": 0, "shots": []}
    shots = rows_to_dicts(conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)).fetchall())
    out = []
    for s in shots:
        vid = None
        v = None
        if s["adopted_version_id"]:
            v = conn.execute(
                "SELECT * FROM shot_versions WHERE id=? AND status='succeeded'",
                (s["adopted_version_id"],)).fetchone()
            if (
                v and not _is_delivery_fallback(v)
                and v["video_path"] and Path(v["video_path"]).is_file()
            ):
                from app.config import PROJECTS_DIR
                try:
                    rel_path = Path(v["video_path"]).relative_to(PROJECTS_DIR).as_posix()
                except ValueError:
                    rel_path = None
                if rel_path:
                    vid = f"/media/{rel_path}"
        playback_rate = float(v["playback_rate"] or 1.0) if vid and v else 1.0
        model_candidate = _playable_model_candidate(conn, s["id"])
        out.append({"shot_id": s["id"], "shot_no": s["shot_no"],
                    "duration_s": s["duration_s"], "video_url": vid,
                    "has_adopted": bool(vid),
                    "has_model_candidate": bool(model_candidate),
                    "playback_rate": playback_rate,
                    "effective_duration_s": round(float(s["duration_s"] or 0) / playback_rate, 2)})
    available = sum(1 for item in out if item["has_model_candidate"])
    skipped_shot_nos = [item["shot_no"] for item in out if not item["has_model_candidate"]]
    active_shot_nos = _active_generation_shot_nos(conn, episode_id)
    final_path = _final_video_path(ep["project_id"], ep["episode_no"])
    final_edit_report = _read_edit_report(final_path)
    final_timeline = (
        final_edit_report.get("timeline")
        if isinstance(final_edit_report, dict) else None
    )
    return {
        "episode_id": ep["id"],
        "title": ep["title"],
        "episode_no": ep["episode_no"],
        "shots_total": len(shots),
        "shots_ready": available,
        # 部分合成是主流程：任意一镜真实视频已落盘即可合成，
        # 其他缺镜/生成中镜头只做透明跳过，不生成图片占位。
        "ready": available > 0,
        "generation_active": bool(active_shot_nos),
        "active_shot_nos": active_shot_nos,
        "all_ready": len(shots) > 0 and available == len(shots),
        "shots_skipped": len(skipped_shot_nos),
        "skipped_shot_nos": skipped_shot_nos,
        "final_video_url": _existing_final_url(ep),
        "final_video_stale": _final_video_is_stale(ep),
        "final_is_partial": bool(
            isinstance(final_timeline, dict) and final_timeline.get("partial")
        ),
        "final_edit_report": final_edit_report,
        "shots": out,
    }


def _existing_final_url(ep_row) -> str | None:
    final_path = _final_video_path(ep_row["project_id"], ep_row["episode_no"])
    if final_path.exists():
        return _versioned_final_url(final_path)
    return None


def _versioned_final_url(final_path: Path) -> str:
    """返回随成品文件变化的 URL，避免重新合成后浏览器继续播放旧缓存。"""
    from app.config import PROJECTS_DIR

    rel_path = final_path.relative_to(PROJECTS_DIR).as_posix()
    stat = final_path.stat()
    revision = f"{stat.st_mtime_ns}-{stat.st_size}"
    return f"/media/{rel_path}?v={revision}"


def _final_video_is_stale(ep_row) -> bool:
    final_path = _final_video_path(ep_row["project_id"], ep_row["episode_no"])
    return final_path.is_file() and final_path.with_suffix(".stale").is_file()


def _final_video_path(project_id: str, episode_no: int) -> Path:
    d = config.PROJECTS_DIR / project_id / "episodes" / str(episode_no) / "final"
    d.mkdir(parents=True, exist_ok=True)
    return d / "episode.mp4"


def _edit_report_path(final_path: Path) -> Path:
    return final_path.with_name("episode.edit-report.json")


def _read_edit_report(final_path: Path) -> dict | None:
    path = _edit_report_path(final_path)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _final_edit_mode() -> str:
    import os

    mode = (os.getenv("MANJU_FINAL_EDIT_MODE") or "auto").strip().lower()
    if mode in {"always", "auto", "off"}:
        return mode
    if mode in {"1", "true", "yes", "on"}:
        return "always"
    if mode in {"0", "false", "no", "draft", "fast"}:
        return "off"
    return "auto"


def _final_edit_decision(
    conn,
    episode_id: str,
    piece_specs: list[tuple[int, str, float]],
    skipped_shot_nos: list[int],
) -> tuple[bool, str]:
    """Return whether the expensive final-edit pass is worth running now."""
    mode = _final_edit_mode()
    if mode == "always":
        return True, "forced_by_env"
    if mode == "off":
        return False, "disabled_by_env"
    if skipped_shot_nos:
        return False, "partial_timeline_fast_preview"

    from app.continuity import required_text_strategy
    from app.final_edit import shot_from_row, transition_spec

    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall()
    shot_by_no = {int(row["shot_no"]): shot_from_row(row) for row in rows}
    ordered_shots = [
        shot_by_no[shot_no]
        for shot_no, _path, _rate in piece_specs
        if shot_no in shot_by_no
    ]

    for shot in ordered_shots:
        required = shot.required_text
        exact = (required.exact_text or "").strip() if required else ""
        if exact and required_text_strategy(shot) == "deterministic_insert":
            return True, "deterministic_text"

    for previous, current in zip(ordered_shots, ordered_shots[1:]):
        if current.shot_no == previous.shot_no + 1 and transition_spec(current.transition).edit_type != "cut":
            return True, "enhanced_transition"

    return False, "simple_timeline_fast_concat"


def concatenate_episode(episode_id: str) -> dict:
    """把当前已有的真实模型视频按镜号拼接成 MP4。

    只接受真实模型视频。内容 QA 低分不拦截，但静态图片、轻运动卡和静音片段
    不具备成片资格。缺镜或生成中镜头直接跳过；任何时候只要已有一镜
    真实视频就允许生成当前阶段成片。
    """
    from pathlib import Path as _P
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        raise ValueError("剧集不存在")
    from app.downstream_authority import verify_current_storyboard_release_authority

    release_authority = verify_current_storyboard_release_authority(
        episode_id,
        conn=conn,
    )
    if not shutil.which("ffmpeg"):
        raise ValueError(
            "服务端未找到视频合成组件 ffmpeg；请安装 ffmpeg 或修正服务启动 PATH 后重试，"
            "本次未生成成片"
        )
    if not shutil.which("ffprobe"):
        raise ValueError(
            "服务端未找到视频校验组件 ffprobe；无法在替换前校验成片，"
            "本次未生成成片"
        )

    missing_model_shot_nos: list[int] = []
    shot_rows = conn.execute(
        "SELECT id,shot_no,adopted_version_id FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall()
    for shot in shot_rows:
        adopted = (
            conn.execute(
                "SELECT * FROM shot_versions WHERE id=? AND status='succeeded'",
                (shot["adopted_version_id"],),
            ).fetchone()
            if shot["adopted_version_id"] else None
        )
        if (
            adopted and not _is_delivery_fallback(adopted)
            and adopted["video_path"] and Path(adopted["video_path"]).is_file()
        ):
            continue
        missing_model_shot_nos.append(int(shot["shot_no"]))
    if missing_model_shot_nos:
        raise ValueError(
            "当前分镜仍有镜头未采纳通过技术校验的真实视频，禁止生成部分成片："
            + ", ".join(str(no) for no in missing_model_shot_nos)
        )
    from app.downstream_authority import current_adopted_video_delivery_manifest

    video_delivery_manifest = current_adopted_video_delivery_manifest(
        episode_id,
        conn=conn,
    )
    pieces = _adopted_video_paths(episode_id)
    if not pieces:
        raise ValueError(
            "本集当前还没有任何可播放的真实模型视频；"
            "不会使用静态图片或静音片段冒充成片"
        )

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
    started_at = time.perf_counter()
    common_result = {
        "shots": len(pieces),
        "ffmpeg_missing": False,
        "shots_total": len(all_shot_nos),
        "shots_skipped": len(skipped_shot_nos),
        "skipped_shot_nos": skipped_shot_nos,
        "included_shot_nos": piece_shot_nos,
        "partial": bool(skipped_shot_nos),
        "final_video_stale": False,
        "fallback_shots_created": 0,
        "fallback_shots_reused": 0,
        "playback_rates": {str(no): rate for no, _path, rate in piece_specs},
        "storyboard_release_authority": release_authority,
        "video_delivery_manifest": video_delivery_manifest,
    }

    # final-edit 是质量增强层，不是交付门禁。任何字体/滤镜/转场失败都回退到
    # 下方的传统硬拼，上一版成片仍在原子替换成功前保持可用。
    final_edit_failure: str | None = None
    final_edit_enabled, final_edit_reason = _final_edit_decision(
        conn,
        episode_id,
        piece_specs,
        skipped_shot_nos,
    )
    final_edit_elapsed_s: float | None = None
    if final_edit_enabled:
        final_edit_started_at = time.perf_counter()
        try:
            from app.final_edit import render_episode_final_edit

            with tempfile.TemporaryDirectory() as edit_td:
                edit_dir = _P(edit_td)
                edited_video = edit_dir / "final-edit.mp4"
                edit_report = render_episode_final_edit(
                    conn,
                    episode_id,
                    piece_specs,
                    edited_video,
                    edit_dir,
                )
                edit_report["timeline"] = {
                    "partial": bool(skipped_shot_nos),
                    "shots_total": len(all_shot_nos),
                    "included_shot_nos": piece_shot_nos,
                    "skipped_shot_nos": skipped_shot_nos,
                    "missing_model_shot_nos": missing_model_shot_nos,
                }
                edit_report["mode"] = "final_edit"
                edit_report["video_delivery_manifest"] = video_delivery_manifest
                edit_report["video_delivery_manifest_hash"] = video_delivery_manifest[
                    "manifest_hash"
                ]
                edit_report["decision_reason"] = final_edit_reason
                edit_report["elapsed_s"] = round(time.perf_counter() - final_edit_started_at, 3)
                validated_duration_s = _validate_concat_output(
                    edited_video,
                    expected_duration_s=float(edit_report["total_duration_s"]),
                    decode_timeout_s=concat_timeout_s,
                )
                _publish_concat_output(
                    conn,
                    episode_id=episode_id,
                    candidate_path=edited_video,
                    final_path=final_path,
                    report=edit_report,
                    release_authority=release_authority,
                    video_delivery_manifest=video_delivery_manifest,
                )
            return {
                "video_url": _versioned_final_url(final_path),
                "total_duration_s": round(validated_duration_s, 1),
                **common_result,
                "elapsed_s": round(time.perf_counter() - started_at, 3),
                "final_edit": edit_report,
            }
        except Exception as exc:  # noqa: BLE001 - 质量增强失败必须继续完整交付
            final_edit_elapsed_s = time.perf_counter() - final_edit_started_at
            final_edit_failure = f"{type(exc).__name__}: {exc}"[:1000]

    measured_total_dur = 0.0
    for shot_no, vpath, rate in piece_specs:
        try:
            source_probe = _probe_concat_media(vpath)
        except ValueError as exc:
            raise ValueError(
                f"镜 {shot_no} 的源片段无法完成容器/时长校验，上一版成片仍保留：{exc}"
            ) from exc
        measured_total_dur += float(source_probe["duration_s"]) / rate

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
        total_dur = _validate_concat_output(
            silent_video,
            expected_duration_s=measured_total_dur,
            decode_timeout_s=concat_timeout_s,
        )
        publish_candidate = final_path.with_name(
            f".{final_path.name}.{new_id('candidate')}.tmp"
        )
        atomic_copy(silent_video, publish_candidate)

    fallback_edit_report = {
        "ok": False,
        "mode": "draft_concat",
        "fallback": "draft_concat",
        "error": final_edit_failure,
        "skipped_final_edit": not final_edit_enabled,
        "decision_reason": final_edit_reason,
        "final_edit_elapsed_s": round(final_edit_elapsed_s, 3) if final_edit_elapsed_s is not None else None,
        "elapsed_s": round(time.perf_counter() - started_at, 3),
        "runtime_blocking": False,
        "timeline": {
            "partial": bool(skipped_shot_nos),
            "shots_total": len(all_shot_nos),
            "included_shot_nos": piece_shot_nos,
            "skipped_shot_nos": skipped_shot_nos,
            "missing_model_shot_nos": missing_model_shot_nos,
        },
        "video_delivery_manifest": video_delivery_manifest,
        "video_delivery_manifest_hash": video_delivery_manifest["manifest_hash"],
    }
    try:
        _publish_concat_output(
            conn,
            episode_id=episode_id,
            candidate_path=publish_candidate,
            final_path=final_path,
            report=fallback_edit_report,
            release_authority=release_authority,
            video_delivery_manifest=video_delivery_manifest,
        )
    finally:
        publish_candidate.unlink(missing_ok=True)
    return {
        # 文件已经在上方原子覆盖；版本参数确保前端把它作为新的媒体资源加载。
        "video_url": _versioned_final_url(final_path),
        "total_duration_s": round(total_dur, 1),
        **common_result,
        "elapsed_s": round(time.perf_counter() - started_at, 3),
        "final_edit": fallback_edit_report,
    }

__all__ = [name for name in globals() if not name.startswith("__")]
