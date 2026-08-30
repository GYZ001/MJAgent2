"""红/绿验收：draft_concat 的音画对齐修复是否落地。

背景见 docs/delivery_pipeline_rca_2026-08-29.md 与 scripts/verify_av_sync.py：
app/media_exec/concat.py 的 draft_concat 用 `ffmpeg -f concat -c copy` 直粘源
片段，不对音频做 aresample/asetpts 归一。源片段音频采样率一旦混用（真实供
应商产出常见），加上每镜音频天然比视频长几十毫秒，拼接后的成片音画会明显
不同步——王六郎 EP1 的真实成片实测漂移 +0.050528s（合 1.21 帧），源片段
采样率混用（镜2/6=32000Hz，其余=44100Hz）。

本文件不依赖 projects/ 下任何真实产物、也不依赖数据库里现有的行——那些会
被 scripts/reset_pipeline_data.py 清空。样本在测试内用 ffmpeg 现场合成（几
秒、含真实正弦音轨），故意复现「采样率混用 + 音频比视频长」，驱动真实的
worker.concatenate_episode() 走一遍 draft_concat（不 mock ffmpeg 子进程），
再用 scripts/verify_av_sync.py 里与命令行工具完全同一份测量/判定函数验收
合成产物。

test_draft_concat_produces_av_synced_final 在 concat.py 的音频归一逻辑落地
之前预期是红的（当时的 `-c copy` 直粘对采样率混用的输入会把音频时钟拉伸，
产生远超 1 帧的漂移——本文件编写时用手写的裸 ffmpeg `-c copy`/重编码兜底
命令独立复现过，容器/音频时长被拉到 8.4s+ 而视频流仍是 6.06s，同源于王六郎
EP1 的真实实测 +0.050528s 漂移）。**截至本次交付，另一个 agent 已经把
`app/media_exec/concat.py`/`app/final_edit.py` 的音频归一逻辑（逐镜
aresample+asetpts+apad/atrim 对齐到视频流时长，draft 与 final_edit 共用同一
份 `audio_normalize_filter`）落到了工作区（尚未提交），所以这条测试当前实
测是绿的**（本样本上实测残余漂移 ~21ms，远小于 1 帧阈值 41.7ms，且这个残
余量级正好对应 AAC 在 48kHz 下 1024 采样点的固有帧粒度，不是漏归一）。保留
这条测试的意义没变——它仍然是修复是否在场的唯一独立验证手段：不要为了让
它现在变绿而放宽下面的阈值或改用别的判据；如果这个断言将来又变红，说明
音频归一逻辑被回退或破坏了。

其余两个测试只验证 scripts/verify_av_sync.py 测量工具自身的判定逻辑（不经
过 concat.py/worker.py，不受另一个 agent 的修复进度影响），用来把「工具本
身对不对」和「被测流水线修没修」这两件事分开验证。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from app import artifacts, db, worker
from tests.conftest import patch_worker_everywhere
from scripts.verify_av_sync import (
    EpisodeReport,
    SyncVerdict,
    evaluate_drift,
    evaluate_final_video,
    probe_media,
)

FFMPEG_AVAILABLE = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
pytestmark = pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="需要 ffmpeg/ffprobe")


def _synth(path: Path, *, color: str, sample_rate: int, video_s: float, audio_s: float) -> None:
    """现场合成一段小样本：video_s 秒纯色视频 + audio_s 秒正弦音轨（sample_rate）。

    不设 -shortest，容器最终时长跟随较长的那条流——这正是 draft_concat 的
    真实上游输入形态：模型产出的每镜视频流恒定，音频流略长。
    """
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c={color}:s=320x240:r=24:d={video_s}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={audio_s}:sample_rate={sample_rate}",
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "96k",
            str(path),
        ],
        check=True, capture_output=True, timeout=60,
    )


def test_evaluate_drift_flags_audio_longer_than_one_frame(tmp_path: Path) -> None:
    """工具自身的判定逻辑：音频比视频长超过 1 帧 -> FAIL。不经过 concat.py。"""
    path = tmp_path / "solo.mp4"
    _synth(path, color="green", sample_rate=44100, video_s=2.0, audio_s=2.1)

    probe = probe_media(path)
    assert probe.video is not None and probe.audio is not None
    verdict = evaluate_drift(probe)

    assert verdict.threshold_s == pytest.approx(1.0 / 24.0, abs=1e-6)
    assert verdict.drift_s == pytest.approx(0.1, abs=0.01)
    assert verdict.synced is False


def test_evaluate_drift_passes_when_audio_matches_video(tmp_path: Path) -> None:
    """工具自身的判定逻辑：音频与视频时长一致 -> PASS。不经过 concat.py。"""
    path = tmp_path / "solo.mp4"
    _synth(path, color="green", sample_rate=44100, video_s=2.0, audio_s=2.0)

    report = evaluate_final_video(path, label="solo")
    assert report.error is None
    assert report.verdict is not None
    assert report.verdict.synced is True
    assert report.ok is True


def test_ok_does_not_gate_on_source_sample_rate_mixing() -> None:
    """判据归属：EpisodeReport.ok 只挂在「成片本体是否同步」上，源片段采样
    率混用只是观测项。合成层现在统一重采样到 48000Hz 并逐镜裁齐，已经吸收
    了这个上游波动，不应该再让它把成片判成 ISSUES FOUND（改前这里是
    False，因为 ok 曾经把 verdict.synced and not sample_rate_mixed 一起 and
    进结论——见 CLAUDE.md「判据必须挂在这件事本身成没成上」）。"""
    report = EpisodeReport(
        label="synced-final-with-mixed-source-rates",
        final_path=Path("/tmp/does-not-need-to-exist.mp4"),
        verdict=SyncVerdict(drift_s=0.01, threshold_s=1.0 / 24.0, drift_frames=0.24, synced=True),
        sample_rates_seen={32000: [2, 6], 44100: [1, 3, 4, 5]},
    )

    assert report.sample_rate_mixed is True
    assert report.ok is True


@pytest.fixture
def _stub_downstream_authority(monkeypatch: pytest.MonkeyPatch):
    """concatenate_episode 依赖的发布权威 / 已采纳视频清单校验——与
    tests/test_episode_partial_concat.py 里验证过的同一种打桩方式，让
    concat.py 的真实逻辑跑到 draft_concat 分支，而不是在权威校验这一步就
    提前失败。"""
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
                "shot_id": row["id"],
                "shot_no": row["shot_no"],
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


def _seed_two_shot_episode() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for statement in db.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,title,status,created_at) "
        "VALUES('e','p',1,'E','confirmed',0)"
    )
    for shot_no in (1, 2):
        conn.execute(
            "INSERT INTO shots(id,episode_id,shot_no,duration_s) VALUES(?,?,?,?)",
            (f"s{shot_no}", "e", shot_no, 3),
        )
    conn.commit()
    return conn


def _adopt_version(conn: sqlite3.Connection, *, shot_no: int, path: Path) -> None:
    version_id = f"v{shot_no}"
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,video_path,created_at
           ) VALUES(?,?,?,?,?,'succeeded',?,0)""",
        (version_id, f"s{shot_no}", 1, "prompt", f"key-{shot_no}", str(path)),
    )
    conn.execute("UPDATE shots SET adopted_version_id=? WHERE id=?", (version_id, f"s{shot_no}"))


def test_draft_concat_produces_av_synced_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stub_downstream_authority,
) -> None:
    # 强制走 draft_concat（而不是可能已经做了音频归一的 final_edit 增强路
    # 径），精确复现报告里"几乎 100% 成片都走 draft_concat"的真实路径,
    # 不依赖运行环境里 MANJU_FINAL_EDIT_MODE 的当前取值。
    monkeypatch.setenv("MANJU_FINAL_EDIT_MODE", "off")

    conn = _seed_two_shot_episode()
    project_root = tmp_path / "projects"
    shot_dir = project_root / "p" / "episodes" / "1" / "shots"
    shot_dir.mkdir(parents=True)

    shot1 = shot_dir / "shot-1.mp4"
    shot2 = shot_dir / "shot-2.mp4"
    # 采样率混用（32000Hz / 44100Hz）+ 每镜音频比视频长 45~60ms——直接对应
    # 王六郎 EP1 draft_concat 实测的真实缺陷条件（镜2/6=32000Hz、其余=
    # 44100Hz，每镜漂移 27~62ms，见 scripts/verify_av_sync.py 手工验收）。
    _synth(shot1, color="red", sample_rate=32000, video_s=3.0, audio_s=3.060)
    _synth(shot2, color="blue", sample_rate=44100, video_s=3.0, audio_s=3.045)

    _adopt_version(conn, shot_no=1, path=shot1)
    _adopt_version(conn, shot_no=2, path=shot2)
    conn.commit()

    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.config, "PROJECTS_DIR", project_root)
    # 不 mock worker.shutil.which / worker.subprocess：worker.shutil 与本文件
    # import 的 shutil 是同一个模块对象（worker.py 把 media_exec/*.py exec 进
    # 自身命名空间共享同一份全局名字），这里要的正是真实 ffmpeg 探测 + 真实
    # 子进程执行，用系统自带的 shutil.which 结果即可，不需要也不能自我替换。

    worker.concatenate_episode("e")

    final_path = project_root / "p" / "episodes" / "1" / "final" / "episode.mp4"
    assert final_path.is_file(), "concatenate_episode 应当产出成片"

    report = evaluate_final_video(final_path, label="draft_concat 回归样本")
    assert report.error is None, f"独立测量失败：{report.error}"
    assert report.verdict is not None
    v = report.verdict

    assert v.synced, (
        f"成片音画不同步：漂移 {v.drift_s:+.6f}s（阈值 {v.threshold_s:.6f}s，"
        f"合 {v.drift_frames:+.2f} 帧）。在 app/media_exec/concat.py 的 "
        "draft_concat 补上音频归一（对齐采样率 + 重置 PTS + 按视频时长裁齐）"
        "之前，这条断言预期失败；不要为了让它现在变绿而放宽阈值或改用别的判据。"
    )
