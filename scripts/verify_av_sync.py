#!/usr/bin/env python3
"""独立验收：成片台「音画同不同步」的只读观测工具。

背景（见 docs/delivery_pipeline_rca_2026-08-29.md 问题一/三）：draft 合成路径
（app/media_exec/concat.py 的 draft_concat）用 `ffmpeg -f concat -c copy` 直粘
源片段，不对音频做重采样/PTS 归一。源片段的音频采样率一旦混用，concat
demuxer 会用首段的 timebase 解释后续所有音频包，产生系统性的音频时钟拉伸；
即便采样率恰好一致，draft 路径也不裁齐每镜音视频，逐镜残余漂移会累积到整
片。这类问题此前**没有任何独立观察点**：合成只校验容器总时长（被拉伸的音
频污染），从未单独比较音频流与视频流的时长，成片台前端也不展示这个数字，
用户是自己看出来的，产品侧零信号。

本工具就是那个独立观察点——完全不依赖、不导入 app/media_exec/concat.py 或
app/final_edit.py 的任何函数或常量，只用 ffprobe 对已产出的成片（以及可选
的每个源片段）逐流量测，自己重新推导「音画是否同步」，不信任被测系统自报
的任何状态字段。

判据来源（不是白名单，不写死集号/采样率/项目 ID）：
  - 「同步」的阈值 = 该文件视频流自身实测帧率对应的 1 帧时长（1/fps）。
    帧时长从每个文件自己的 ffprobe 结果现算，不是记忆哪一集应该是多少。
    理由：小于 1 帧的音画偏移不可能在该视频自身的时间分辨率上被看见；
    达到或超过 1 帧，音频与视频至少错开了一整帧画面，是可测量、可感知的
    错位。这与 CLAUDE.md「禁止黑白名单与枚举穷举」一致——合法阈值从这份
    输入自己的数据推导，换一集、换一个帧率，阈值自动跟着变。
  - 「源片段采样率是否混用」直接从本次实际探测到的采样率集合判断
    （len(set) > 1），不比对任何预先记忆的采样率列表。

只读约束：数据库一律 `mode=ro` 打开；不写任何文件到 projects/ 下；不修改
任何数据库行。日志追加写 logs/verify_av_sync.log，不在仓库根目录留任何文件。

用法：
    # 默认：解析「我欲封天」当前有效的项目 id，检查它已有成片的所有集
    .venv/bin/python scripts/verify_av_sync.py

    # 指定项目名（项目会因 scripts/reset_pipeline_data.py 等重置操作换 id，
    # 因此本工具不硬编码任何历史项目 ID，一律按名字或显式 --project-id 解析）
    .venv/bin/python scripts/verify_av_sync.py --project-name 王六郎

    # 显式项目 id + 集号范围
    .venv/bin/python scripts/verify_av_sync.py --project-id proj_xxx --episodes 1-10

    # 直接给成片路径，不查库（仍会尝试按目录约定关联逐镜源片段）
    .venv/bin/python scripts/verify_av_sync.py \\
        --final-video projects/proj_xxx/episodes/1/final/episode.mp4

    # 只测整片，跳过逐镜源片段分析（快）
    .venv/bin/python scripts/verify_av_sync.py --no-shots

    # 列出当前库里的项目（重置后重新发现有效 id 用）
    .venv/bin/python scripts/verify_av_sync.py --list-projects

退出码：
    0 = 已检查的成片全部音画同步（源片段采样率混用与否只作为观测项打印，
        不影响这个判定——合成层现在统一重采样到 48000Hz 并逐镜裁齐，已经
        吸收了这个上游波动，见下方 EpisodeReport.ok）
    1 = 至少一集成片不同步，或测量失败（真实缺陷信号）
    2 = 没有可检查的目标（项目下还没有任何成片——例如清库后、回归还没跑出
        第一集成片的窗口内属正常状态，不代表「验证通过」，也不是「验证失
        败」，用独立退出码区分，调用方不应把它当 0 处理）
    3 = 硬错误（参数错误、ffprobe/ffmpeg 缺失、数据库打不开等），本次未产
        生任何判定结果
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = ROOT / "data" / "manju.db"
DEFAULT_PROJECT_NAME = "我欲封天"
LOG = ROOT / "logs" / "verify_av_sync.log"
PROBE_TIMEOUT_S = 30.0

EXIT_SYNCED = 0
EXIT_ISSUES_FOUND = 1
EXIT_NO_TARGETS = 2
EXIT_ERROR = 3


def log(msg: str) -> None:
    line = f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


class ProbeError(RuntimeError):
    """ffprobe 测量本身失败，或测出的数据不足以做出判定。"""


# --------------------------------------------------------------------------
# ffprobe 测量（不依赖 app/ 下任何模块，独立于被测合成代码）
# --------------------------------------------------------------------------

@dataclass
class StreamProbe:
    codec_name: str | None = None
    duration_s: float | None = None
    frame_rate: float | None = None          # 仅视频流
    sample_rate: int | None = None           # 仅音频流
    channels: int | None = None              # 仅音频流
    channel_layout: str | None = None        # 仅音频流


@dataclass
class MediaProbe:
    path: Path
    container_duration_s: float | None
    video: StreamProbe | None
    audio: StreamProbe | None


def _parse_fraction(text: str | None) -> float | None:
    if not text:
        return None
    parts = text.split("/")
    try:
        if len(parts) == 2:
            num, den = float(parts[0]), float(parts[1])
            if den == 0:
                return None
            return num / den
        return float(text)
    except (TypeError, ValueError):
        return None


def probe_media(path: Path) -> MediaProbe:
    """对单个媒体文件逐流量测容器/视频/音频时长与音频采样率、声道布局。"""
    if not path.is_file() or path.stat().st_size <= 0:
        raise ProbeError(f"文件不存在或为空：{path}")
    if not shutil.which("ffprobe"):
        raise ProbeError("本机没有 ffprobe，无法做任何音画同步判定")
    try:
        completed = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries",
                "format=duration:stream=codec_type,codec_name,duration,"
                "sample_rate,channels,channel_layout,r_frame_rate",
                "-of", "json", str(path),
            ],
            check=True, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"ffprobe 超过 {int(PROBE_TIMEOUT_S)} 秒：{path}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()[-500:]
        raise ProbeError(f"ffprobe 无法读取 {path}" + (f"：{detail}" if detail else "")) from exc
    except OSError as exc:
        raise ProbeError(f"ffprobe 无法执行：{exc}") from exc

    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe 返回了无法解析的 JSON：{path}") from exc

    fmt = payload.get("format") or {}
    container_duration_s: float | None
    try:
        container_duration_s = float(fmt.get("duration"))
    except (TypeError, ValueError):
        container_duration_s = None

    video: StreamProbe | None = None
    audio: StreamProbe | None = None
    for stream in payload.get("streams") or []:
        if not isinstance(stream, dict):
            continue
        codec_type = stream.get("codec_type")
        duration_s: float | None
        try:
            duration_s = float(stream.get("duration"))
        except (TypeError, ValueError):
            duration_s = None
        if codec_type == "video" and video is None:
            video = StreamProbe(
                codec_name=stream.get("codec_name"),
                duration_s=duration_s,
                frame_rate=_parse_fraction(stream.get("r_frame_rate")),
            )
        elif codec_type == "audio" and audio is None:
            sample_rate = stream.get("sample_rate")
            channels = stream.get("channels")
            audio = StreamProbe(
                codec_name=stream.get("codec_name"),
                duration_s=duration_s,
                sample_rate=int(sample_rate) if sample_rate not in (None, "") else None,
                channels=int(channels) if channels not in (None, "") else None,
                channel_layout=stream.get("channel_layout"),
            )

    return MediaProbe(
        path=path, container_duration_s=container_duration_s, video=video, audio=audio,
    )


# --------------------------------------------------------------------------
# 判定：音画是否同步（阈值从这份文件自己的实测帧率现算，不写死任何常量）
# --------------------------------------------------------------------------

@dataclass
class SyncVerdict:
    drift_s: float
    threshold_s: float
    drift_frames: float
    synced: bool


def evaluate_drift(probe: MediaProbe) -> SyncVerdict:
    if probe.video is None or probe.video.duration_s is None:
        raise ProbeError(f"{probe.path}：容器里没有可用的视频流时长，无法判定音画同步")
    if probe.audio is None or probe.audio.duration_s is None:
        raise ProbeError(f"{probe.path}：容器里没有可用的音频流，无法判定音画同步（可能音轨整段丢失）")
    if probe.video.frame_rate is None or probe.video.frame_rate <= 0:
        raise ProbeError(f"{probe.path}：无法从视频流解析真实帧率（r_frame_rate 无效），拒绝瞎猜阈值")
    threshold_s = 1.0 / probe.video.frame_rate
    drift_s = probe.audio.duration_s - probe.video.duration_s
    drift_frames = drift_s / threshold_s
    return SyncVerdict(
        drift_s=drift_s, threshold_s=threshold_s, drift_frames=drift_frames,
        synced=abs(drift_s) <= threshold_s,
    )


@dataclass
class EpisodeReport:
    label: str
    final_path: Path
    probe: MediaProbe | None = None
    verdict: SyncVerdict | None = None
    error: str | None = None
    shot_reports: list[dict[str, Any]] = field(default_factory=list)
    sample_rates_seen: dict[int, list[int]] = field(default_factory=dict)

    @property
    def sample_rate_mixed(self) -> bool:
        return len(self.sample_rates_seen) > 1

    @property
    def ok(self) -> bool:
        if self.error is not None:
            return False
        if self.verdict is None:
            return False
        return self.verdict.synced


def evaluate_final_video(path: Path, *, label: str | None = None) -> EpisodeReport:
    report = EpisodeReport(label=label or str(path), final_path=path)
    try:
        probe = probe_media(path)
        report.probe = probe
        report.verdict = evaluate_drift(probe)
    except ProbeError as exc:
        report.error = str(exc)
    return report


def evaluate_source_shots(
    report: EpisodeReport, shots: list[tuple[int, Path | None, str | None]],
) -> None:
    """逐镜测量源片段，填充 report.shot_reports 与 report.sample_rates_seen。

    shots: [(shot_no, video_path_or_None, note_if_unavailable), ...]
    """
    for shot_no, shot_path, note in shots:
        if shot_path is None:
            report.shot_reports.append({
                "shot_no": shot_no, "ok": False, "note": note or "无可用源片段路径",
            })
            continue
        try:
            probe = probe_media(shot_path)
        except ProbeError as exc:
            report.shot_reports.append({"shot_no": shot_no, "ok": False, "note": str(exc)})
            continue
        entry: dict[str, Any] = {
            "shot_no": shot_no,
            "ok": True,
            "path": str(shot_path),
            "video_duration_s": probe.video.duration_s if probe.video else None,
            "audio_duration_s": probe.audio.duration_s if probe.audio else None,
            "sample_rate": probe.audio.sample_rate if probe.audio else None,
            "channel_layout": probe.audio.channel_layout if probe.audio else None,
        }
        if probe.video and probe.audio and probe.video.duration_s and probe.audio.duration_s:
            entry["shot_drift_s"] = round(probe.audio.duration_s - probe.video.duration_s, 6)
        if probe.audio and probe.audio.sample_rate:
            report.sample_rates_seen.setdefault(probe.audio.sample_rate, []).append(shot_no)
        report.shot_reports.append(entry)


# --------------------------------------------------------------------------
# 项目 / 集号解析（只读 DB；不硬编码任何历史项目 ID —— 项目会随
# scripts/reset_pipeline_data.py 等重置操作换 id，见 CLAUDE.md 与本仓库教训）
# --------------------------------------------------------------------------

def open_db(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise ProbeError(f"数据库文件不存在：{db_path}")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError as exc:
        raise ProbeError(f"无法以只读方式打开数据库：{db_path}：{exc}") from exc
    conn.row_factory = sqlite3.Row
    return conn


def list_projects(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = conn.execute("SELECT id, name FROM projects ORDER BY name, id").fetchall()
    return [(row["id"], row["name"]) for row in rows]


def resolve_project_id(
    conn: sqlite3.Connection, *, project_id: str | None, project_name: str,
) -> tuple[str, str]:
    if project_id:
        row = conn.execute(
            "SELECT id, name FROM projects WHERE id=?", (project_id,),
        ).fetchone()
        if row is None:
            raise ProbeError(
                f"--project-id={project_id!r} 在当前数据库里不存在（项目可能已被重置换过 id，"
                "用 --list-projects 查看当前有效的项目）"
            )
        return row["id"], row["name"]
    rows = conn.execute(
        "SELECT id, name FROM projects WHERE name=?", (project_name,),
    ).fetchall()
    if not rows:
        raise ProbeError(
            f"按名字 {project_name!r} 找不到项目（用 --list-projects 查看当前有效的项目，"
            "或用 --project-id 显式指定）"
        )
    if len(rows) > 1:
        ids = ", ".join(r["id"] for r in rows)
        raise ProbeError(
            f"名字 {project_name!r} 命中了多个项目（{ids}），用 --project-id 显式指定其一"
        )
    return rows[0]["id"], rows[0]["name"]


def parse_episode_spec(spec: str) -> list[int]:
    result: list[int] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            lo_s, _, hi_s = chunk.partition("-")
            try:
                lo, hi = int(lo_s), int(hi_s)
            except ValueError as exc:
                raise ProbeError(f"--episodes 里的范围写法无效：{chunk!r}") from exc
            if lo > hi:
                raise ProbeError(f"--episodes 里的范围顺序无效：{chunk!r}")
            result.extend(range(lo, hi + 1))
        else:
            try:
                result.append(int(chunk))
            except ValueError as exc:
                raise ProbeError(f"--episodes 里的集号无效：{chunk!r}") from exc
    seen: set[int] = set()
    ordered: list[int] = []
    for no in result:
        if no not in seen:
            seen.add(no)
            ordered.append(no)
    return ordered


def final_video_path(project_id: str, episode_no: int) -> Path:
    return ROOT / "projects" / project_id / "episodes" / str(episode_no) / "final" / "episode.mp4"


def discover_episode_numbers_with_final(conn: sqlite3.Connection, project_id: str) -> list[int]:
    rows = conn.execute(
        "SELECT episode_no FROM episodes WHERE project_id=? ORDER BY episode_no", (project_id,),
    ).fetchall()
    found = []
    for row in rows:
        episode_no = int(row["episode_no"])
        if final_video_path(project_id, episode_no).is_file():
            found.append(episode_no)
    return found


def episode_id_for(conn: sqlite3.Connection, project_id: str, episode_no: int) -> str | None:
    row = conn.execute(
        "SELECT id FROM episodes WHERE project_id=? AND episode_no=?", (project_id, episode_no),
    ).fetchone()
    return row["id"] if row else None


def source_shots_for_episode(conn: sqlite3.Connection, episode_id: str) -> list[tuple[int, Path | None, str | None]]:
    rows = conn.execute(
        """SELECT s.shot_no, sv.video_path, s.adopted_version_id
             FROM shots s
             LEFT JOIN shot_versions sv
               ON sv.id = s.adopted_version_id AND sv.status='succeeded'
            WHERE s.episode_id=?
            ORDER BY s.shot_no""",
        (episode_id,),
    ).fetchall()
    out: list[tuple[int, Path | None, str | None]] = []
    for row in rows:
        shot_no = int(row["shot_no"])
        if not row["adopted_version_id"]:
            out.append((shot_no, None, "该镜没有已采纳版本"))
            continue
        video_path = row["video_path"]
        if not video_path:
            out.append((shot_no, None, "已采纳版本没有记录 video_path"))
            continue
        path = Path(video_path)
        if not path.is_file():
            out.append((shot_no, None, f"已采纳版本的源文件在磁盘上不存在：{path}"))
            continue
        out.append((shot_no, path, None))
    return out


# --------------------------------------------------------------------------
# 尝试从「直接给的成片路径」反推项目 id / 集号（按目录约定
# projects/<project_id>/episodes/<episode_no>/final/*.mp4，仅用于关联逐镜
# 源片段做诊断；关联不上不影响整片判定，只是跳过逐镜细节）
# --------------------------------------------------------------------------

def infer_project_episode_from_path(path: Path) -> tuple[str, int] | None:
    parts = path.resolve().parts
    try:
        idx = parts.index("projects")
    except ValueError:
        return None
    try:
        project_id = parts[idx + 1]
        if parts[idx + 2] != "episodes":
            return None
        episode_no = int(parts[idx + 3])
    except (IndexError, ValueError):
        return None
    return project_id, episode_no


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------

def print_report(report: EpisodeReport, *, show_shots: bool) -> None:
    print(f"\n=== {report.label} ===")
    print(f"  成片：{report.final_path}")
    if report.error:
        print(f"  [ERROR] {report.error}")
        return
    probe = report.probe
    verdict = report.verdict
    assert probe is not None and verdict is not None
    v = probe.video
    a = probe.audio
    print(f"  容器时长     = {probe.container_duration_s:.6f}s" if probe.container_duration_s is not None else "  容器时长     = 未知")
    if v:
        print(f"  视频流时长   = {v.duration_s:.6f}s（帧率 {v.frame_rate:.3f}fps，codec={v.codec_name}）" if v.duration_s is not None else "  视频流时长   = 未知")
    if a:
        print(
            f"  音频流时长   = {a.duration_s:.6f}s（{a.sample_rate}Hz，{a.channel_layout or f'{a.channels}ch'}，codec={a.codec_name}）"
            if a.duration_s is not None else "  音频流时长   = 未知"
        )
    sign = "音频比视频长" if verdict.drift_s >= 0 else "音频比视频短"
    print(
        f"  漂移         = {verdict.drift_s:+.6f}s（{sign} {abs(verdict.drift_s):.6f}s，"
        f"合 {verdict.drift_frames:+.2f} 帧）"
    )
    print(f"  同步阈值     = 1 帧 = {verdict.threshold_s:.6f}s（该文件实测帧率现算，非写死常量）")
    print(f"  同步判定     = {'PASS 同步' if verdict.synced else 'FAIL 不同步'}")
    if report.sample_rates_seen:
        if report.sample_rate_mixed:
            detail = "; ".join(
                f"{rate}Hz: 镜{','.join(str(n) for n in nos)}"
                for rate, nos in sorted(report.sample_rates_seen.items())
            )
            print(
                f"  源片段采样率 = 混用（观测项，不计入结论；{detail}）"
                "—— 曾是 EP3 时长膨胀的触发条件，现由合成层统一重采样到 48000Hz 吸收"
            )
        else:
            rate = next(iter(report.sample_rates_seen))
            print(f"  源片段采样率 = 统一（{rate}Hz，共 {sum(len(v) for v in report.sample_rates_seen.values())} 镜）")
    if show_shots and report.shot_reports:
        print("  逐镜源片段：")
        cumulative = 0.0
        for entry in report.shot_reports:
            shot_no = entry["shot_no"]
            if not entry.get("ok"):
                print(f"    镜{shot_no:>3}: 跳过 —— {entry.get('note')}")
                continue
            drift = entry.get("shot_drift_s")
            if drift is not None:
                cumulative += drift
                print(
                    f"    镜{shot_no:>3}: 视频{entry['video_duration_s']:.6f}s "
                    f"音频{entry['audio_duration_s']:.6f}s "
                    f"({entry['sample_rate']}Hz) 漂移{drift:+.6f}s 累计{cumulative:+.6f}s"
                )
            else:
                print(f"    镜{shot_no:>3}: 数据不完整，无法算漂移")
    print(f"  最终结论     = {'OK' if report.ok else 'ISSUES FOUND'}")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

@dataclass
class _Target:
    label: str
    final_path: Path
    project_id: str | None
    episode_no: int | None


def _resolve_targets(args: argparse.Namespace) -> tuple[list[_Target], sqlite3.Connection | None]:
    """解析目标成片列表；如需要按 DB 关联逐镜源片段，一并返回打开的只读连接
    （调用方负责关闭）。"""
    if args.final_video:
        targets = []
        for raw in args.final_video:
            path = Path(raw).resolve()
            inferred = infer_project_episode_from_path(path)
            if inferred:
                project_id, episode_no = inferred
                targets.append(_Target(f"{project_id} EP{episode_no}（{path.name}）", path, project_id, episode_no))
            else:
                targets.append(_Target(str(path), path, None, None))
        conn = None
        if not args.no_shots and any(t.project_id for t in targets):
            try:
                conn = open_db(args.db)
            except ProbeError:
                conn = None
        return targets, conn

    conn = open_db(args.db)
    project_id, project_name = resolve_project_id(
        conn, project_id=args.project_id, project_name=args.project_name,
    )
    episode_nos = (
        parse_episode_spec(args.episodes) if args.episodes
        else discover_episode_numbers_with_final(conn, project_id)
    )
    targets = [
        _Target(
            f"{project_name}（{project_id}）EP{episode_no}",
            final_video_path(project_id, episode_no),
            project_id, episode_no,
        )
        for episode_no in episode_nos
    ]
    return targets, conn


def build_reports(args: argparse.Namespace) -> tuple[list[EpisodeReport], list[str]]:
    """解析目标 + 逐一 ffprobe 测量，返回 (EpisodeReport 列表, 因无成片被跳过的说明列表)。"""
    targets, conn = _resolve_targets(args)
    try:
        reports: list[EpisodeReport] = []
        skipped: list[str] = []
        for target in targets:
            if not target.final_path.is_file():
                skipped.append(f"{target.label}：成片不存在（{target.final_path}）")
                continue
            report = evaluate_final_video(target.final_path, label=target.label)
            if (
                not args.no_shots and conn is not None
                and target.project_id is not None and target.episode_no is not None
            ):
                episode_id = episode_id_for(conn, target.project_id, target.episode_no)
                if episode_id is not None:
                    shots = source_shots_for_episode(conn, episode_id)
                    evaluate_source_shots(report, shots)
            reports.append(report)
        return reports, skipped
    finally:
        if conn is not None:
            conn.close()


def run(args: argparse.Namespace) -> int:
    if not shutil.which("ffprobe"):
        log("[ERROR] 本机没有 ffprobe，无法做任何音画同步判定")
        return EXIT_ERROR

    if args.list_projects:
        try:
            conn = open_db(args.db)
        except ProbeError as exc:
            log(f"[ERROR] {exc}")
            return EXIT_ERROR
        try:
            for project_id, name in list_projects(conn):
                print(f"{project_id}\t{name}")
        finally:
            conn.close()
        return EXIT_SYNCED

    try:
        reports, skipped = build_reports(args)
    except ProbeError as exc:
        log(f"[ERROR] {exc}")
        return EXIT_ERROR

    for note in skipped:
        log(f"[SKIP] {note}")

    if not reports:
        log(
            "[NO TARGETS] 没有找到任何可检查的成片——项目下还没有产出成片属正常状态"
            "（例如清库后回归还没跑出第一集），不代表验证通过或失败。"
        )
        return EXIT_NO_TARGETS

    any_issue = False
    for report in reports:
        print_report(report, show_shots=not args.no_shots)
        if not report.ok:
            any_issue = True

    print(
        f"\n共检查 {len(reports)} 集："
        f"{sum(1 for r in reports if r.ok)} 同步，"
        f"{sum(1 for r in reports if not r.ok)} 有问题"
        + (f"；另有 {len(skipped)} 集因无成片被跳过" if skipped else "")
    )
    return EXIT_ISSUES_FOUND if any_issue else EXIT_SYNCED


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="独立验收成片台音画是否同步（只读，不改动任何数据）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--project-id", default=None, help="项目 id；不给则按 --project-name 解析")
    parser.add_argument(
        "--project-name", default=DEFAULT_PROJECT_NAME,
        help=f"按项目名解析当前有效的项目 id（默认 {DEFAULT_PROJECT_NAME}）；"
             "项目会随重置操作换 id，因此不提供任何硬编码的历史项目 id 默认值",
    )
    parser.add_argument("--episodes", default=None, help='集号范围，如 "1-10" 或 "2,9,10"；不给则自动发现该项目下已有成片的集')
    parser.add_argument(
        "--final-video", action="append", default=None,
        help="直接给成片路径（可重复）；跳过项目/集号解析，仍会尝试按目录约定关联逐镜源片段",
    )
    parser.add_argument("--no-shots", action="store_true", help="跳过逐镜源片段分析，只测整片（更快）")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"数据库路径（只读打开，默认 {DEFAULT_DB_PATH}）")
    parser.add_argument("--list-projects", action="store_true", help="列出当前库里的项目并退出")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except ProbeError as exc:
        log(f"[ERROR] {exc}")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
