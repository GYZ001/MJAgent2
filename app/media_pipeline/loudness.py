"""逐段响度归一：draft_concat 与 final_edit 共用的测量与增益计算。

背景（生产成片实测，接缝前后 0.4 秒窗口 ffmpeg ebur128/astats）：整集由若干
15 秒片段拼成，每段音频由视频模型各自生成，响度互不相干。快速路径
（``draft_concat``）完全不做响度归一；终剪路径（``final_edit``）只在整片层面
做一次 ``loudnorm``，段与段之间的电平差不会被这一遍动态压缩拉平（LRA 11 的
约束控制的是整体响度范围，不逐段对齐）。本模块把「测量 -> 换算增益 -> 应用」
这条链路收敛成一处，两条路径的逐段准备阶段（``app.final_edit._prepare_clip``、
``app.media_exec.concat._draft_concat_pieces``）各调用一次。

设计取舍：
1. **测量**：用 ``loudnorm`` 单遍 ``print_format=json`` 模式测积分响度
   （``-vn -af loudnorm=...:print_format=json -f null /dev/null``，只解码音频、
   不写真实输出），比完整两遍 loudnorm 或额外跑 ``ebur128`` 再正则解析文本都
   更省事——JSON 是标准 ffmpeg 输出，不需要自己维护解析容差。
2. **增益应用用线性 ``volume=XdB``，不用单遍 loudnorm 动态模式**：loudnorm 的
   动态模式会在片段内部随时间抽吸增益（"呼吸感"），逐段各来一次会在段内制造
   新的响度起伏；线性增益是整段统一平移，人工剪辑对白电平就是这么做的。升
   益时额外接 ``alimiter`` 限幅（选它不选 loudnorm 的 linear 模式，是因为
   ``alimiter`` 只在真正需要防溢出时介入、不改变已经安全的样本，而 loudnorm
   linear 模式即使增益为 0 也会重新过一遍其内部处理）。
3. **上限只封升益，不封衰减**：过响的片段允许直接压到目标，不设下限；接近
   静音/纯环境音的片段如果不设上限会被拉到与对白同等响度，把底噪拉响。
   ``MAX_BOOST_DB`` 取 24dB 不是拍脑袋：本模块开发时用 ffmpeg lavfi 正弦波
   实测过一组样本（440Hz，目标 -16 LUFS）——基准电平测得 -21.75 LUFS，衰减
   4dB/14dB/30dB 后分别测得 -25.75/-35.75/-51.75 LUFS，纯静音测得 ``-inf``。
   -35.75 LUFS（需要 +19.75dB 才能打到目标）仍在真实对白可能出现的安静范围
   内，24dB 的上限留了余量让它完全归一；而需要 +24dB 以上的片段，原始响度
   已经低于约 -40 LUFS，15 秒单镜对白很少会录/生成到这个量级，更符合房间
   底噪或纯环境音的特征——封顶在这里，既不会把真实但偏安静的对白拉不到位，
   也不会把底噪拉响。
4. **测量失败与极端值都不得让整集合成失败**：ffmpeg 测量出错/超时/输出解析
   不出——按 0dB 处理，原样保留调用方后续的重采样/补齐/截齐；纯静音测得
   ``-inf``——按上限封顶的增益处理（对静音样本而言，任何有限增益乘 0 仍是
   0，不会产生 NaN 或可闻爆响）。两种情况都在返回值里如实记录，不静默。
"""
from __future__ import annotations

import json
import math
import subprocess
from typing import Any

from app.media_pipeline.delivery_encode import low_priority

FINAL_AUDIO_RATE = 48_000

# 与 app.final_edit._compose 现有的整片 loudnorm 目标一致，逐段先归一到同一
# 目标，整片那一遍就只是微调，不是本模块的目标改了它就得跟着改。
TARGET_LUFS = -16.0

# 取值依据见模块 docstring 第 3 条。
MAX_BOOST_DB = 24.0

# 测量只解码音频、不写真实输出，15 秒量级片段的实测耗时远小于 1 秒；20 秒是
# 留给较长源文件（如误传入整集）的宽松上限，不是常态耗时。
_MEASURE_TIMEOUT_S = 20.0

# alimiter 限幅目标：与整片 loudnorm 的 TP=-1.5 同口径（-1.5 dBFS 的线性值，
# 10**(-1.5/20)），不是另立一套阈值。alimiter 的 `limit` 只接受线性幅度
# （0.0625~1），不能直接填 dB。
_ALIMITER_LIMIT_LINEAR = 0.841
# alimiter 的 `level`（auto level）参数默认 true：无论是否真的发生了限幅，
# 都会把输出按 1/limit 统一放大——A 机 ffmpeg 6.1.1 与生产机 B 的 7.0.2 都是
# 同一行为（`ffmpeg -h filter=alimiter`：level <boolean> auto level (default
# true)）。实测过：一段先被 volume 推到远超满幅的样本，走 `alimiter=limit=
# 0.97`（不带 level）后峰值精确落在 0.0 dBFS——不是「压到 0.97 附近」，是
# `limit * (1/limit) = 1`，限幅名存实亡；快速拼接路径后面没有整片 loudnorm
# 兜底，AAC 编码器对贴着满幅的样本经常产生互采样过冲（inter-sample peak），
# 实测真峰能到 +0.4 dBTP（见 tests/test_per_clip_loudness.py 的红/绿用例）。
# 必须显式 `level=false`，让 `limit` 说了算。
# attack/release 保持 ffmpeg 默认（5ms/50ms）；latency（是否把这 5ms 前瞻
# 补偿进时间戳）也保持默认 false——5ms 远小于一帧（24fps≈41.7ms），且只在
# 增益、不在音画对齐环节生效，对口型的可感知影响可以忽略，不值得为此再接一
# 层时间戳补偿的复杂度。


def audio_normalize_filter(
    *, atempo_rate: float | None, duration_s: float, gain_db: float = 0.0,
) -> str:
    """ffmpeg 音频滤镜链，draft_concat 与 render_episode_final_edit 共用。

    统一重采样到 FINAL_AUDIO_RATE、清零 PTS，再用 apad+atrim 把音轨精确对齐到
    `duration_s`（调用方传入的权威时长——通常是该镜视频流的实测时长，必要时
    已按倍速折算）。两条路径共用同一份逻辑，不允许只有一条做对，另一条假设
    「模型视频没有音轨」而放任音频原样直粘。

    `gain_db` 是 `measure_clip_gain` 算出的逐段响度增益，默认 0（不变）保持
    旧调用方零改动兼容。非零时插入线性 `volume`；只有升益（`gain_db > 0`）
    才接 `alimiter` 限幅——衰减不会把电平推过 0dBFS，不需要限幅。
    """
    parts: list[str] = []
    if atempo_rate is not None and abs(atempo_rate - 1.0) > 1e-6:
        parts.append(f"atempo={atempo_rate:.6f}")
    if abs(gain_db) > 1e-6:
        parts.append(f"volume={gain_db:.3f}dB")
        if gain_db > 0:
            parts.append(f"alimiter=limit={_ALIMITER_LIMIT_LINEAR}:level=false")
    parts.append(f"aresample={FINAL_AUDIO_RATE}")
    parts.append("asetpts=PTS-STARTPTS")
    parts.append(f"apad=whole_dur={duration_s:.6f}")
    parts.append(f"atrim=duration={duration_s:.6f}")
    return ",".join(parts)


def _parse_measured_lufs(stderr_text: str) -> float | None:
    """从 loudnorm `print_format=json` 的 stderr 尾部提取 `input_i`。

    loudnorm 把测量结果的 JSON 块打在 stderr 最后（前面是常规日志行），不是
    独立可解析的一整段输出，所以取最后一对花括号而不是整段 json.loads。
    """
    start, end = stderr_text.rfind("{"), stderr_text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        payload = json.loads(stderr_text[start:end + 1])
        return float(payload["input_i"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def _gain_for_measurement(measured_lufs: float) -> tuple[float, bool]:
    """目标减实测＝所需增益，超过上限就截断；不设下限。

    `measured_lufs` 为 `-inf`（纯静音）时 `TARGET_LUFS - measured_lufs` 是
    `+inf`，天然大于 `MAX_BOOST_DB`，落回同一条封顶分支，不需要为静音单独
    判支。
    """
    uncapped = TARGET_LUFS - measured_lufs
    if uncapped > MAX_BOOST_DB:
        return MAX_BOOST_DB, True
    return uncapped, False


def measure_clip_gain(path: str, *, timeout: float = _MEASURE_TIMEOUT_S) -> dict[str, Any]:
    """测量 `path` 音轨积分响度，换算为到 `TARGET_LUFS` 的线性增益（dB）。

    只应在调用方已确认该片段有音轨时调用。测量失败（ffmpeg 报错/超时/进程
    起不来/输出解析不出）一律按 0dB 处理并在 `error` 字段如实记录，不向上
    抛异常——单段测量失败不该让整集合成失败。捕获面故意比 subprocess 相关
    异常更宽：这条路径的失败必须永远退化为「不调整」，不能把任何异常泄漏给
    调用方（该函数的唯一契约就是"总有返回值，从不抛出"）。
    """
    try:
        completed = subprocess.run(
            ["ffmpeg", "-nostdin", "-i", path, "-vn",
             "-af", f"loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=11:print_format=json",
             "-f", "null", "/dev/null"],
            check=True, capture_output=True, timeout=timeout, preexec_fn=low_priority,
        )
        stderr_text = (completed.stderr or b"").decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - 测量失败必须退化为 0dB，不得让整集合成失败
        return {"measured_lufs": None, "gain_db": 0.0, "capped": False,
                "error": f"{type(exc).__name__}: {exc}"[:500]}

    measured = _parse_measured_lufs(stderr_text)
    if measured is None or math.isnan(measured):
        error = "loudnorm 返回 NaN（数字静音）" if measured is not None else "未能解析 loudnorm 测量输出"
        return {"measured_lufs": None, "gain_db": 0.0, "capped": False, "error": error}

    gain_db, capped = _gain_for_measurement(measured)
    return {
        "measured_lufs": round(measured, 2) if math.isfinite(measured) else str(measured),
        "gain_db": round(gain_db, 3),
        "capped": capped,
        "error": None,
    }


def clip_loudness_report(source_path: str, has_audio: bool, *, shot_no: int) -> dict[str, Any]:
    """`has_audio` 为假（anullsrc 合成的占位静音）时直接给 0dB 报告，不必测量；
    为真时调用 `measure_clip_gain`。draft_concat 与 final_edit 的逐镜准备循环
    共用同一份判断，不允许一条路径测、另一条路径悄悄跳过。
    """
    report: dict[str, Any] = {"shot_no": shot_no}
    if not has_audio:
        report.update({"measured_lufs": None, "gain_db": 0.0, "capped": False, "error": None})
        return report
    report.update(measure_clip_gain(source_path))
    return report
