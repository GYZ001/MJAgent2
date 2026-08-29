"""覆盖问题一/三（同源）的修复：draft_concat 逐镜音频归一 + 视频流时长权威。

背景见 docs/delivery_pipeline_rca_2026-08-29.md。核心场景：模型产出的源片段
音轨是真实录音，采样率并不保证一致（例如 32000Hz 与 44100Hz 混用）。旧版
draft_concat 直接 `ffmpeg -f concat -c copy` 硬拼，不对音频做任何归一；一旦
首段恰好是较低采样率，concat demuxer 会用首段的音频 timebase 解释后续所有
音频包，把时长拉伸到秒级（EP3 即是如此），即便采样率一致也会有几十毫秒的
逐镜音画漂移累积。

本文件的样本全部在测试内用 ffmpeg 自行生成（不依赖 projects/ 下任何素材，
后者会被主会话的 scripts/reset_pipeline_data.py 清空）。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from app import db, worker, artifacts
from app.evidence import media as media_evidence
from app.final_edit import FINAL_AUDIO_RATE, audio_normalize_filter

_FFMPEG_AVAILABLE = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
pytestmark = pytest.mark.skipif(not _FFMPEG_AVAILABLE, reason="ffmpeg/ffprobe unavailable")


def _make_clip(
    path: Path, *, color: str, video_duration_s: float, sample_rate: int, fps: int = 24,
) -> None:
    """生成一段真实可解码的 mp4：视频严格落在 fps 网格上，音频是有真实内容的
    正弦波（不是无声占位），且不在输出端强制裁剪，让 AAC 编码器的整帧量化
    自然产生音轨比视频轨长几毫秒到几十毫秒的漂移——这正是真实模型产物的模式
    （见 RCA 附录：13 个源片段视频流恒定 15.041667s，音频流按采样率浮动
    15.069~15.104s）。
    """
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c={color}:s=64x64:r={fps}:d={video_duration_s}",
            "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate={sample_rate}:duration={video_duration_s}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", str(sample_rate),
            str(path),
        ],
        check=True, capture_output=True, timeout=30,
    )


def _probe(path: Path) -> tuple[float, float, float, int | None]:
    """返回 (video_duration_s, audio_duration_s, container_duration_s, sample_rate)。"""
    raw = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration:stream=codec_type,duration,sample_rate",
            "-of", "json", str(path),
        ],
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout
    payload = json.loads(raw)
    container = float(payload["format"]["duration"])
    video_dur = audio_dur = 0.0
    sample_rate: int | None = None
    for stream in payload.get("streams") or []:
        if stream.get("codec_type") == "video":
            video_dur = float(stream.get("duration") or 0)
        elif stream.get("codec_type") == "audio":
            audio_dur = float(stream.get("duration") or 0)
            sample_rate = int(stream["sample_rate"]) if stream.get("sample_rate") else None
    return video_dur, audio_dur, container, sample_rate


def _legacy_naive_concat_copy(pieces: list[Path], destination: Path, *, timeout_s: float = 30.0) -> None:
    """独立观察点（红）：手写一份 concat.py 修复前 draft_concat 核心逻辑的副本——
    对原始源片段直接 `-f concat -c copy`，不做任何音频归一。这不是生产代码，
    只是把 docs/delivery_pipeline_rca_2026-08-29.md 描述的旧算法原样重现，用于
    证明缺陷本身存在，且独立于当前（已修复）的 app.media_exec.concat 实现——
    以后即便生产代码继续演进，这个红色对照点也不会被误改绿。
    """
    listfile = destination.parent / f"{destination.stem}-legacy-list.txt"
    lines = [f"file '{str(p)}'" for p in pieces]
    listfile.write_text("\n".join(lines), encoding="utf-8")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(listfile), "-c", "copy", "-movflags", "+faststart", str(destination)],
        check=True, capture_output=True, timeout=timeout_s,
    )


def test_legacy_naive_concat_copy_inflates_duration_on_mixed_sample_rates(tmp_path: Path) -> None:
    """红：修复前的算法在采样率混用、且首段采样率较低时，会把音频/容器时长
    拉伸到远超视频流的量级——这正是 EP3 CON-409 的机理（首段 32000Hz，后续
    44100Hz 包被按 32000Hz 的 timebase 解释，时钟被系统性拉慢）。
    """
    clip_a = tmp_path / "a.mp4"  # 32000Hz 领头，复现 EP3 的触发条件
    clip_b = tmp_path / "b.mp4"
    clip_c = tmp_path / "c.mp4"
    _make_clip(clip_a, color="red", video_duration_s=2.0, sample_rate=32000)
    _make_clip(clip_b, color="green", video_duration_s=2.0, sample_rate=44100)
    _make_clip(clip_c, color="blue", video_duration_s=2.0, sample_rate=44100)

    out = tmp_path / "legacy_out.mp4"
    _legacy_naive_concat_copy([clip_a, clip_b, clip_c], out)

    video_s, audio_s, container_s, sample_rate = _probe(out)
    expected_video_s = 6.0  # 3 * 2.0s，严格落在 24fps 网格上

    assert abs(video_s - expected_video_s) < 0.1, "视频流本身应当保持正确"
    # 缺陷本体：音频/容器时长被拉伸到远超视频流、远超任何合理时长门容差的量级。
    assert audio_s > expected_video_s * 1.2, (
        f"复现失败：修复前算法本应把音频拉伸，实测 audio={audio_s}, video={video_s}"
    )
    assert container_s > expected_video_s * 1.2
    assert sample_rate == 32000  # 容器沿用了首段（较低）的采样率


def test_legacy_naive_concat_copy_stays_correct_when_sample_rates_match(tmp_path: Path) -> None:
    """对照：采样率一致时旧算法不会时长爆炸，但音频仍比视频略长——这正是
    EP2/EP9/EP10 那种「通过了时长门、但仍然音画不同步」的问题三本体。
    """
    clip_a = tmp_path / "a.mp4"
    clip_b = tmp_path / "b.mp4"
    _make_clip(clip_a, color="red", video_duration_s=2.0, sample_rate=44100)
    _make_clip(clip_b, color="blue", video_duration_s=2.0, sample_rate=44100)

    out = tmp_path / "legacy_out.mp4"
    _legacy_naive_concat_copy([clip_a, clip_b], out)

    video_s, audio_s, container_s, _rate = _probe(out)
    assert abs(video_s - 4.0) < 0.1
    # 不会爆炸，但音频仍然比视频长（AAC 整帧量化残留），且旧算法完全没有
    # 消除它的机制——这就是问题三：逐镜漂移会随集数累积。
    assert audio_s > video_s


def _audio_normalize_filter_smoke() -> None:
    identity = audio_normalize_filter(atempo_rate=1.0, duration_s=3.0)
    assert "atempo" not in identity  # rate==1.0 时不应该插入无意义的 atempo
    assert f"aresample={FINAL_AUDIO_RATE}" in identity
    assert "asetpts=PTS-STARTPTS" in identity
    assert "apad=whole_dur=3.000000" in identity
    assert "atrim=duration=3.000000" in identity

    sped = audio_normalize_filter(atempo_rate=2.0, duration_s=1.5)
    assert "atempo=2.000000" in sped
    assert "apad=whole_dur=1.500000" in sped


def test_audio_normalize_filter_shared_contract() -> None:
    _audio_normalize_filter_smoke()


def test_probe_concat_media_reports_video_duration_and_audio_presence(tmp_path: Path) -> None:
    from app.media_exec.concat import _probe_concat_media

    silent = tmp_path / "silent.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=c=red:s=64x64:r=24:d=1.0", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         str(silent)],
        check=True, capture_output=True, timeout=30,
    )
    probe = _probe_concat_media(silent)
    assert probe["has_audio"] is False
    assert abs(probe["video_duration_s"] - 1.0) < 0.05

    with_audio = tmp_path / "with_audio.mp4"
    _make_clip(with_audio, color="blue", video_duration_s=1.0, sample_rate=32000)
    probe2 = _probe_concat_media(with_audio)
    assert probe2["has_audio"] is True
    assert abs(probe2["video_duration_s"] - 1.0) < 0.05
    # 容器 duration 取音视频流较长者，通常会比纯视频流时长略长（AAC 帧量化）。
    assert probe2["duration_s"] >= probe2["video_duration_s"]


def _database_with_shots(conn: sqlite3.Connection, shot_paths: list[Path]) -> None:
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,title,status,created_at) "
        "VALUES('e','p',1,'E','confirmed',0)"
    )
    for shot_no, path in enumerate(shot_paths, start=1):
        conn.execute(
            "INSERT INTO shots(id,episode_id,shot_no,duration_s) VALUES(?,?,?,?)",
            (f"s{shot_no}", "e", shot_no, 2),
        )
        version_id = f"v{shot_no}"
        conn.execute(
            """INSERT INTO shot_versions(
                   id,shot_id,version_no,prompt_text,idem_key,status,video_path,created_at
               ) VALUES(?,?,?,?,?,'succeeded',?,0)""",
            (version_id, f"s{shot_no}", 1, "prompt", f"key-{shot_no}", str(path)),
        )
        conn.execute("UPDATE shots SET adopted_version_id=? WHERE id=?", (version_id, f"s{shot_no}"))
    conn.commit()


@pytest.fixture
def _current_authority(monkeypatch):
    from app import downstream_authority

    monkeypatch.setattr(
        downstream_authority,
        "verify_current_storyboard_release_authority",
        lambda episode_id, conn=None: {
            "published_storyboard_artifact_id": f"storyboard:{episode_id}",
            "release_qualification_hash": "release-current",
        },
    )

    def video_manifest(episode_id, conn=None):
        rows = (conn or worker.get_conn()).execute(
            """SELECT s.id,s.shot_no,s.adopted_version_id,v.playback_rate,v.video_path
                 FROM shots s LEFT JOIN shot_versions v ON v.id=s.adopted_version_id
                WHERE s.episode_id=? ORDER BY s.shot_no""",
            (episode_id,),
        ).fetchall()
        items = [
            {
                "shot_id": row["id"], "shot_no": row["shot_no"],
                "adopted_version_id": row["adopted_version_id"],
                "playback_rate": float(row["playback_rate"] or 1),
                "video_path": row["video_path"],
            }
            for row in rows
        ]
        return {"manifest_hash": json.dumps(items, sort_keys=True), "items": items}

    monkeypatch.setattr(
        downstream_authority, "current_adopted_video_delivery_manifest", video_manifest,
    )
    # concatenate_episode 现在为幂等/CAS 漂移检测算的是容错版本清单（单镜
    # 权威失效只跳过那一镜，不整份失败）；入选候选仍走 _adopted_video_paths，
    # 这里只是让"机制测试"用的直通 mock 同时覆盖新旧两个函数名。
    monkeypatch.setattr(
        downstream_authority, "current_partial_adopted_video_delivery_manifest", video_manifest,
    )


def test_concatenate_episode_normalizes_mixed_sample_rate_audio_and_passes_gate(
    tmp_path, monkeypatch, _current_authority,
) -> None:
    """绿：真实生产代码 worker.concatenate_episode()（即 app.media_exec.concat 里
    已修复的 draft_concat）面对采样率混用、且低采样率片段领头（EP3 触发条件）
    的真实源片段时，必须：1）不抛异常、能通过时长门；2）产物视频/音频/容器
    时长彼此高度收敛；3）输出采样率统一到 FINAL_AUDIO_RATE。
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for statement in db.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass

    project_root = tmp_path / "projects"
    shot_dir = project_root / "p" / "episodes" / "1" / "shots"
    shot_dir.mkdir(parents=True)
    shot_paths = [shot_dir / f"shot-{i}.mp4" for i in range(1, 4)]
    # shot-1 用较低采样率领头（EP3 的真实触发条件），shot-2/3 用更常见的 44100Hz。
    _make_clip(shot_paths[0], color="red", video_duration_s=2.0, sample_rate=32000)
    _make_clip(shot_paths[1], color="green", video_duration_s=2.0, sample_rate=44100)
    _make_clip(shot_paths[2], color="blue", video_duration_s=2.0, sample_rate=44100)
    _database_with_shots(conn, shot_paths)

    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(media_evidence, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.config, "PROJECTS_DIR", project_root)

    result = worker.concatenate_episode("e")

    assert result["final_edit"]["mode"] == "draft_concat"
    assert result["final_edit"]["decision_reason"] == "simple_timeline_fast_concat"
    # 预期时长以视频流为权威：3 * 2.0s，不会被混用的采样率污染。
    assert abs(result["total_duration_s"] - 6.0) < 0.2

    final_path = project_root / "p" / "episodes" / "1" / "final" / "episode.mp4"
    assert final_path.is_file()
    video_s, audio_s, container_s, sample_rate = _probe(final_path)

    assert abs(video_s - 6.0) < 0.1
    # 修复前同样输入会把 audio/container 拉到 8s+（见上面的红测试）；修复后
    # 三者必须高度收敛，不再是「爆炸或侥幸」的量级。
    assert abs(audio_s - video_s) < 0.5, f"音画仍未收敛：video={video_s} audio={audio_s}"
    assert abs(container_s - video_s) < 0.5
    assert sample_rate == FINAL_AUDIO_RATE  # 两条路径共用同一套目标采样率


def test_concatenate_episode_order_independent_of_which_sample_rate_leads(
    tmp_path, monkeypatch, _current_authority,
) -> None:
    """修复前『炸不炸只取决于首段采样率』本身就是缺陷：同一批源片段，仅仅
    调换镜序（哪一镜是 32000Hz）就能让同一份坏算法从"交付成功"变成
    "CON-409"。修复后无论哪一镜领头，结果都应当一致（音视频独立于拼接顺序）。
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for statement in db.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass

    project_root = tmp_path / "projects"
    shot_dir = project_root / "p" / "episodes" / "1" / "shots"
    shot_dir.mkdir(parents=True)
    shot_paths = [shot_dir / f"shot-{i}.mp4" for i in range(1, 4)]
    # 这次让 44100Hz 领头、32000Hz 排在中间——与上一个测试相反的顺序。
    _make_clip(shot_paths[0], color="green", video_duration_s=2.0, sample_rate=44100)
    _make_clip(shot_paths[1], color="red", video_duration_s=2.0, sample_rate=32000)
    _make_clip(shot_paths[2], color="blue", video_duration_s=2.0, sample_rate=44100)
    _database_with_shots(conn, shot_paths)

    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(media_evidence, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.config, "PROJECTS_DIR", project_root)

    result = worker.concatenate_episode("e")
    assert abs(result["total_duration_s"] - 6.0) < 0.2

    final_path = project_root / "p" / "episodes" / "1" / "final" / "episode.mp4"
    video_s, audio_s, container_s, sample_rate = _probe(final_path)
    assert abs(video_s - 6.0) < 0.1
    assert abs(audio_s - video_s) < 0.5
    assert sample_rate == FINAL_AUDIO_RATE
