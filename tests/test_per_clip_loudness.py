"""逐段响度归一的红/绿端到端验收（app.media_pipeline.loudness 支撑两条路径）。

背景：生产成片实测（ffmpeg ebur128/astats，接缝前后 0.4 秒窗口）显示走快速路径
（draft_concat）的成片接缝电平跳变最大 28dB，终剪路径（final_edit）整片只做一次
loudnorm，段间电平差同样收不拢——根因是两条路径共用的逐段音频滤镜只做重采样/
补齐/截齐，没有逐段响度归一。本文件驱动真实 ffmpeg 生成短片段，分别走两条路径
的逐段准备，验证：1）两段正常对白电平处理后收敛到 ≤2dB；2）低电平/纯静音段的
增益不超过上限、不产生 NaN/异常；3）报告里如实记录每段的实测响度与增益。

红/绿证据：本文件写成后，先手抄修复前的 `audio_normalize_filter`（无 gain_db
参数）在 /tmp 下独立跑过一遍同样的两段对比，差值 9.98dB，证明缺陷存在；修复后
（本文件测的当前代码）差值收窄到 <0.1dB，见下方 `test_draft_concat_levels_...`
与 `test_final_edit_levels_...`。
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from app import db
from app.final_edit import render_episode_final_edit
from app.media_exec.concat import _draft_concat_pieces, _probe_concat_media
from app.media_pipeline.loudness import (
    MAX_BOOST_DB,
    TARGET_LUFS,
    _gain_for_measurement,
    _parse_measured_lufs,
    measure_clip_gain,
)

_FFMPEG_AVAILABLE = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
pytestmark = pytest.mark.skipif(not _FFMPEG_AVAILABLE, reason="ffmpeg/ffprobe unavailable")

_DUR = 3.0  # 秒；本机 2 核，片段保持很短


def _make_clip(path: Path, *, attn_db: float | None, silent: bool = False) -> None:
    """3 秒 64x64 黑画面 + 440Hz 正弦音轨（衰减 attn_db 分贝）；silent=True 时整段数字静音。"""
    if silent:
        audio_src = f"anullsrc=channel_layout=stereo:sample_rate=48000:duration={_DUR}"
    else:
        audio_src = f"sine=frequency=440:sample_rate=48000:duration={_DUR}"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=black:s=64x64:r=24:d={_DUR}",
        "-f", "lavfi", "-i", audio_src,
    ]
    if not silent and attn_db:
        cmd += ["-af", f"volume=-{attn_db}dB"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000", str(path)]
    subprocess.run(cmd, check=True, capture_output=True, timeout=30)


def _measure_lufs(path: Path) -> float:
    """独立观察点：不复用被测代码的 measure_clip_gain，只借它的 JSON 解析工具函数，
    自己起一遍 ffmpeg 验证被测代码真的把电平调对了，而不是读被测代码自己的记账。
    """
    completed = subprocess.run(
        ["ffmpeg", "-nostdin", "-i", str(path), "-vn",
         "-af", f"loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=11:print_format=json", "-f", "null", "/dev/null"],
        capture_output=True, timeout=30,
    )
    measured = _parse_measured_lufs(completed.stderr.decode("utf-8", "replace"))
    assert measured is not None, f"独立测量本身失败：{completed.stderr!r}"
    return measured


def _extract_window(source: Path, dest: Path, *, start_s: float, duration_s: float) -> None:
    """`-c copy` 精确截取一段区间；用于从拼接产物里孤立出单段做独立响度测量——
    直接把 `-ss/-t` 接在 `-af loudnorm` 前会让 loudnorm 的内部统计吃到整份文件
    （实测过：会把前一段的响度混进来，`input_lra` 从 0 变成两位数），必须先物理
    切出单段文件再测量。
    """
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(source),
         "-ss", f"{start_s:.3f}", "-t", f"{duration_s:.3f}", "-c", "copy", str(dest)],
        check=True, capture_output=True, timeout=30,
    )


# lavfi sine(1000Hz) 不加 volume 滤镜时的基准峰值（实测，见 /tmp 下的探测记录）；
# 下面按此换算到目标 dBFS。
_SINE_1K_BASE_PEAK_DBFS = -20.1


def _make_spiky_clip(path: Path) -> None:
    """3 秒片段：安静主体（约 -45dBFS 峰值）中间夹一个 30ms、约 -20dBFS 的短促
    瞬态，两端各 5ms 淡入淡出。淡入淡出不是装饰——三段 `sine=` 各自独立起始
    相位，硬拼接处会有相位不连续（等效一次瞬间阶跃/爆音），实测过：不加淡入
    淡出时同一组 limit 扫描测出的真峰非单调（0.77 比 0.80/0.841 都差），换成
    平滑边沿后才拿到单调、可复现的数据。

    安静主体把整段积分响度拉得很低，逐段增益因此逼近上限；瞬态本身接近满幅，
    验证的正是「增益算对了，但限幅器要是不生效，瞬态会被推过 0dBFS」这个
    场景——alimiter 的 level（auto level）默认 true，不管有没有真的发生限幅
    都会把输出按 1/limit 统一放大，见 app/media_pipeline/loudness.py 里
    `_ALIMITER_LIMIT_LINEAR` 旁的注释。
    """
    body_gain = -45.0 - _SINE_1K_BASE_PEAK_DBFS
    peak_gain = -20.0 - _SINE_1K_BASE_PEAK_DBFS
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=black:s=64x64:r=24:d=3",
        "-f", "lavfi", "-i", "sine=frequency=1000:duration=1.485",
        "-f", "lavfi", "-i", "sine=frequency=1000:duration=0.03",
        "-f", "lavfi", "-i", "sine=frequency=1000:duration=1.485",
        "-filter_complex",
        f"[1:a]volume={body_gain}dB[a0];"
        f"[2:a]volume={peak_gain}dB,afade=t=in:st=0:d=0.005,afade=t=out:st=0.025:d=0.005[a1];"
        f"[3:a]volume={body_gain}dB[a2];"
        "[a0][a1][a2]concat=n=3:v=0:a=1[aout]",
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000", str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=30)


def _measure_true_peak_dbtp(path: Path) -> float:
    """用 ebur128(peak=true) 测真峰（过采样后的峰值，比样本域峰值更贴近实际
    D/A 还原后的电平）；不复用被测代码，独立观察点。
    """
    completed = subprocess.run(
        ["ffmpeg", "-nostdin", "-i", str(path), "-vn", "-af", "ebur128=peak=true", "-f", "null", "/dev/null"],
        capture_output=True, timeout=30,
    )
    text = completed.stderr.decode("utf-8", "replace")
    match = re.search(r"Peak:\s*(-?[\d.]+)\s*dBFS", text)
    assert match, f"未能从 ebur128 输出解析真峰：{text[-800:]}"
    return float(match.group(1))


def test_gain_for_measurement_caps_boost_not_attenuation() -> None:
    """纯函数级契约：MAX_BOOST_DB 只封升益，不封衰减；-inf 落在同一条封顶分支。"""
    quiet_gain, quiet_capped = _gain_for_measurement(TARGET_LUFS - (MAX_BOOST_DB - 5))
    assert quiet_gain == pytest.approx(MAX_BOOST_DB - 5) and not quiet_capped

    loud_gain, loud_capped = _gain_for_measurement(TARGET_LUFS + 20)
    assert loud_gain == pytest.approx(-20.0) and not loud_capped  # 衰减不设上限

    over_gain, over_capped = _gain_for_measurement(TARGET_LUFS - (MAX_BOOST_DB + 10))
    assert over_gain == MAX_BOOST_DB and over_capped

    silence_gain, silence_capped = _gain_for_measurement(float("-inf"))
    assert silence_gain == MAX_BOOST_DB and silence_capped


def test_measure_clip_gain_handles_missing_file_without_raising(tmp_path: Path) -> None:
    """测量失败（文件不存在）必须退化为 0dB 并记录 error，不得抛异常。"""
    result = measure_clip_gain(str(tmp_path / "does-not-exist.mp4"))
    assert result == {"measured_lufs": None, "gain_db": 0.0, "capped": False, "error": result["error"]}
    assert result["error"]  # 非空字符串，如实记录


def test_draft_concat_levels_audible_segments_and_caps_low_level_or_silent(tmp_path: Path) -> None:
    """快速路径 _draft_concat_pieces：4 段（两段正常对白电平、一段低电平噪声、一段
    纯静音）走真实逐段准备与 concat demuxer 拼接，验证响度归一是否生效。
    """
    clip4, clip14, clip30, silent = (tmp_path / n for n in
                                      ("attn4.mp4", "attn14.mp4", "attn30.mp4", "silent.mp4"))
    _make_clip(clip4, attn_db=4)
    _make_clip(clip14, attn_db=14)
    _make_clip(clip30, attn_db=30)
    _make_clip(silent, attn_db=None, silent=True)

    piece_specs = [(1, str(clip4), 1.0), (2, str(clip14), 1.0), (3, str(clip30), 1.0), (4, str(silent), 1.0)]
    probe_by_shot = {no: _probe_concat_media(path) for no, path, _rate in piece_specs}
    final_path = tmp_path / "episode.mp4"

    total_dur, publish_candidate, _artifacts, clip_loudness = _draft_concat_pieces(
        piece_specs, probe_by_shot, final_path, concat_timeout_s=60.0, subtitle_plan=None,
    )
    assert total_dur == pytest.approx(_DUR * 4, abs=0.2)
    assert publish_candidate.is_file() and publish_candidate.stat().st_size > 0

    by_shot = {item["shot_no"]: item for item in clip_loudness}
    assert set(by_shot) == {1, 2, 3, 4}
    assert by_shot[1]["capped"] is False and by_shot[2]["capped"] is False
    assert by_shot[3]["capped"] is True and by_shot[3]["gain_db"] == MAX_BOOST_DB
    assert by_shot[4]["capped"] is True and by_shot[4]["gain_db"] == MAX_BOOST_DB
    assert by_shot[4]["measured_lufs"] == "-inf"  # 字符串而非裸 float，JSON 安全
    assert all(item["error"] is None for item in clip_loudness)
    json.dumps(clip_loudness, ensure_ascii=False)  # 不得因 NaN/Infinity 抛异常

    # 独立观察点：物理切出每段区间（留 0.2s 边距避开镜头切换的编码边界），各自
    # 重新用真实 ffmpeg 测一遍，不读被测代码自己算的 measured_lufs。
    window_paths = [tmp_path / f"win{i}.mp4" for i in range(4)]
    for index, window_path in enumerate(window_paths):
        _extract_window(publish_candidate, window_path, start_s=index * _DUR + 0.2, duration_s=_DUR - 0.4)
    seg0, seg1, seg2, seg3 = (_measure_lufs(path) for path in window_paths)
    assert abs(seg0 - seg1) <= 2.0, f"两段正常对白电平归一后应收敛：seg0={seg0:.2f} seg1={seg1:.2f}"
    assert seg2 < TARGET_LUFS - 4.0, f"低电平段不得被拉到目标响度附近：seg2={seg2:.2f}"
    assert seg3 < TARGET_LUFS - 4.0, f"静音段升益后仍必须保持安静：seg3={seg3:.2f}"


def test_draft_concat_alimiter_caps_true_peak_after_large_boost(tmp_path: Path) -> None:
    """大幅升益 + 片段内含接近满幅的短促瞬态时，真峰必须被限幅器压住。

    alimiter 的 `level`（auto level）默认 true：不管有没有真的发生限幅，都会把
    输出按 1/limit 统一放大，等于限幅名存实亡（app/media_pipeline/loudness.py
    的 `_ALIMITER_LIMIT_LINEAR` 旁有实测记录）。本用例走真实快速路径逐段准备，
    独立用 ebur128 测最终成片的真峰，不读被测代码自己的响度记账。
    """
    clip = tmp_path / "spiky.mp4"
    _make_spiky_clip(clip)
    piece_specs = [(1, str(clip), 1.0)]
    probe_by_shot = {1: _probe_concat_media(str(clip))}
    final_path = tmp_path / "episode.mp4"

    _total_dur, publish_candidate, _artifacts, clip_loudness = _draft_concat_pieces(
        piece_specs, probe_by_shot, final_path, concat_timeout_s=60.0, subtitle_plan=None,
    )
    assert clip_loudness[0]["gain_db"] >= 15.0, f"测试场景本身要求大幅升益：{clip_loudness[0]}"

    true_peak = _measure_true_peak_dbtp(publish_candidate)
    assert true_peak <= -1.0, f"限幅未生效，真峰 {true_peak:.1f} dBTP 超过 -1.0 dBTP 上限"


def _seed_two_shot_episode(conn: sqlite3.Connection) -> None:
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
            """INSERT INTO shots(
                   id,episode_id,shot_no,duration_s,shot_size,camera_move,scene_setting,
                   scene_name,characters,action_desc,source_excerpt,dialogues,transition,
                   continuity_from_prev,continuity_mode,shot_contract_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"s{shot_no}", "e", shot_no, 3, "中景", "固定", "测试", "测试场", "[]", "",
             "", "[]", "硬切", 0, "", "{}"),
        )
    conn.commit()


def test_final_edit_levels_audible_segments(tmp_path: Path) -> None:
    """终剪路径 render_episode_final_edit：两段正常对白电平，各自 _prepare_clip
    产出的独立片段（work_dir/prepared-N.mp4）应收敛到 ≤2dB 之内。
    """
    clip4, clip14 = tmp_path / "attn4.mp4", tmp_path / "attn14.mp4"
    _make_clip(clip4, attn_db=4)
    _make_clip(clip14, attn_db=14)

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _seed_two_shot_episode(conn)

    work_dir = tmp_path / "work"
    destination = tmp_path / "final.mp4"
    report = render_episode_final_edit(
        conn, "e", [(1, str(clip4), 1.0), (2, str(clip14), 1.0)], destination, work_dir,
    )

    assert destination.is_file() and destination.stat().st_size > 0
    clip_loudness: list[dict[str, Any]] = report["clip_loudness"]
    by_shot = {item["shot_no"]: item for item in clip_loudness}
    assert set(by_shot) == {1, 2}
    assert by_shot[1]["capped"] is False and by_shot[2]["capped"] is False
    assert by_shot[1]["error"] is None and by_shot[2]["error"] is None

    seg0 = _measure_lufs(work_dir / "prepared-1.mp4")
    seg1 = _measure_lufs(work_dir / "prepared-2.mp4")
    assert abs(seg0 - seg1) <= 2.0, f"终剪路径两段正常对白电平应收敛：seg0={seg0:.2f} seg1={seg1:.2f}"
