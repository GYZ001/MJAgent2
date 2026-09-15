"""视频字幕闸门：候选视频落盘后抽帧问 VLM「画面上有没有叠加字幕」。L5（碰 hiagent 与 db）。

产品规则（2026-09-14 用户拍板，2026-09-15 收窄为「台词黑名单」）：视频生成只负责画面与
声音，字幕由后续功能另做；牌匾、书信、倒计时、贴图艺术字这类画面文字**不拦**。判据从数据
推导：VLM 报出的画面文字与本镜台词（台词账本里说出口的话）逐句比对，对得上的才是字幕，
对不上的记为 ``diegetic_frames`` 放行。实测反例：「距续约30天」倒计时被当字幕连拦三版。

结论写进 ``shot_versions.qa_json`` 的 ``subtitle_gate`` 键，由 ``app.evidence.subtitle_overlay``
（L2）在候选登记时并进技术校验：有字幕就 ``passed=False``，走既有的
``technical_resubmit_limit`` 自动重提，与坏文件同一条路，不另造重试。

标定（2026-09-14，B 现网 text/vlm 路由）：已知阳性（第 1 集镜 13 烧进画面的「仙人」）
与三张阴性帧 4/4 判对，阳性帧连位置都报对（画面下方、嘴部）；360 宽抽帧足够。

取舍：抽帧或 VLM 调用失败**放行**并打 ``[VIDEO_SUBTITLE_GATE][未判定]``——文本调用
不计费但会挂，模型不可用不该把整条视频流水线卡死；与 continuity_memo 布局引文降级为
告警同一取舍。视觉质检 2026-08-29 整体退场的理由是「评分可靠性为 0」；这里不评分，
只回答一个可标定的二值问题，且每次判定的帧数、命中帧与模型原话都留在 qa_json 里可回看。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from app import hiagent, textmatch
from app.db import get_conn, get_setting

_LOGGER = logging.getLogger(__name__)

SETTING_KEY = "video_subtitle_gate_enabled"
GATE_KEY = "subtitle_gate"
FRAME_INTERVAL_S = 1.5
FRAME_WIDTH = 360
MAX_FRAMES = 12

PROMPT = (
    "下面每张图是一段短视频的抽帧。逐张判断：画面上有没有叠加在画面之上、不属于场景物体的文字"
    "（字幕、台词文本、说话人名条、标题条）？牌匾、书信、招牌、碑刻上的字属于场景物体，不算；"
    "角落里的小水印或 AI 标识也不算。只返回一个 JSON 对象："
    '{"frames":[{"index":1,"overlay_text":true,"text_seen":"看到的文字原样","where":"位置"}]}，'
    "frames 按输入顺序逐张给出，没有叠加文字的帧 overlay_text 为 false。"
)


def enabled() -> bool:
    value = (get_setting(SETTING_KEY) or "true").strip().lower()
    if value not in {"true", "false"}:
        raise RuntimeError(f"非法运行时设置 {SETTING_KEY}；请在监制房修正")
    return value == "true"


def sample_frames(video_path: str) -> list[bytes]:
    """每 1.5 秒抽一帧、缩到 360 宽，返回 JPEG 字节；字幕只在台词出声的那几秒出现，抽稀了会漏。"""
    with tempfile.TemporaryDirectory() as td:
        pattern = Path(td) / "f%02d.jpg"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", video_path,
             "-vf", f"fps=1/{FRAME_INTERVAL_S},scale={FRAME_WIDTH}:-2",
             "-frames:v", str(MAX_FRAMES), "-q:v", "4", str(pattern)],
            check=True, capture_output=True,
        )
        return [path.read_bytes() for path in sorted(Path(td).glob("f*.jpg"))]


def build_messages(frames: list[bytes]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [{"type": "text", "text": PROMPT}]
    for frame in frames:
        data = base64.b64encode(frame).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}"}})
    return [
        {"role": "system", "content": "Return exactly one valid JSON object. No Markdown, no prose."},
        {"role": "user", "content": content},
    ]


_FRAGMENT = re.compile(r"\{[^{}]*\}")
_OVERLAY = re.compile(r'"overlay_text"\s*:\s*(true|false)\b')
_INDEX = re.compile(r'"index"\s*:\s*(\d+)')
_TEXT_SEEN = re.compile(r'"text_seen"\s*:\s*"((?:[^"\\]|\\.)*)"')
_WHERE = re.compile(r'"where"\s*:\s*"((?:[^"\\]|\\.)*)"')


def parse_verdict(raw: str, frames_checked: int) -> dict[str, Any]:
    """模型原文 → 结论。

    整体 JSON 合法就按结构读；不合法就退到逐帧碎片解析——B 上实测 43 次里有 3 次
    整体 JSON 坏了但逐帧对象完好（键名里混进控制字符、多写一个 ``]``）。两种读法
    共用同一条判据：任一帧 ``overlay_text: true`` 即命中；没有命中时必须每一帧都
    读到了、且不少于送检帧数，才判「无」；否则抛 ValueError，由调用方按「未判定」
    放行并留痕。读不出的帧不编值。
    """
    text = (raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型没有返回 JSON 对象")
    body = text[start:end + 1]
    frames = _frames_by_structure(body)
    if frames is None:
        frames = _frames_by_fragments(body)
    overlay = [
        {"index": item["index"], "text_seen": item["text_seen"], "where": item["where"]}
        for item in frames if item["overlay_text"] is True
    ]
    if not overlay and len(frames) < frames_checked:
        raise ValueError(f"模型只报告了 {len(frames)}/{frames_checked} 帧")
    return {
        "checked": True, "frames_checked": frames_checked, "frames_reported": len(frames),
        "subtitle_overlay": bool(overlay), "overlay_frames": overlay,
    }


def _frames_by_structure(body: str) -> list[dict[str, Any]] | None:
    """整体 JSON 合法时按结构读；不合法返回 None 交给碎片解析。只认字面 True。"""
    try:
        data = json.loads(body)
    except ValueError:
        return None
    frames = data.get("frames") if isinstance(data, dict) else None
    if not isinstance(frames, list):
        raise ValueError("模型返回缺少 frames 列表")
    return [
        {"index": item.get("index"), "overlay_text": item.get("overlay_text") is True,
         "text_seen": str(item.get("text_seen") or ""), "where": str(item.get("where") or "")}
        for item in frames if isinstance(item, dict)
    ]


def _frames_by_fragments(body: str) -> list[dict[str, Any]]:
    """整体 JSON 坏了时逐个 ``{...}`` 碎片读：读得出 true/false 的才算一帧。"""
    frames: list[dict[str, Any]] = []
    for ordinal, match in enumerate(_FRAGMENT.finditer(body), start=1):
        fragment = match.group(0)
        flag = _OVERLAY.search(fragment)
        if flag is None:
            continue
        index = _INDEX.search(fragment)
        text_seen = _TEXT_SEEN.search(fragment)
        where = _WHERE.search(fragment)
        frames.append({
            "index": int(index.group(1)) if index else ordinal,
            "overlay_text": flag.group(1) == "true",
            "text_seen": (text_seen.group(1) if text_seen else "").replace('\\"', '"'),
            "where": (where.group(1) if where else "").replace('\\"', '"'),
        })
    return frames

async def detect_subtitle_overlay(video_path: str, *, call_meta: dict[str, Any]) -> dict[str, Any]:
    frames = await asyncio.to_thread(sample_frames, video_path)
    if not frames:
        raise ValueError("抽不出任何帧")
    raw = await hiagent.chat(
        build_messages(frames), temperature=0, max_tokens=1200,
        call_meta={"kind": "vlm_subtitle_gate", **call_meta},
        response_format={"type": "json_object"},
    )
    return parse_verdict(raw, len(frames))


def write_verdict(version_id: str, verdict: dict[str, Any]) -> None:
    """把结论并进该版本的 qa_json（保留其它键）。独立提交：这是诊断类写入，不借调用方事务。"""
    conn = get_conn()
    row = conn.execute("SELECT qa_json FROM shot_versions WHERE id=?", (version_id,)).fetchone()
    try:
        existing = json.loads((row["qa_json"] if row else None) or "{}")
    except ValueError:
        existing = {}
    merged = {**(existing if isinstance(existing, dict) else {}), GATE_KEY: verdict}
    conn.execute("UPDATE shot_versions SET qa_json=? WHERE id=?", (json.dumps(merged, ensure_ascii=False), version_id))
    conn.commit()


# 画面文字算不算「台词」：去标点后互为子串，或最长连续公共块 / 二元组覆盖率过半（VLM 抄字会错一两个
# 字：「重伤」抄成「重山」）。阈值取 0.5——半句台词叠在画面上就是字幕，短于两字的碎片只认子串。
DIALOGUE_MATCH_RATIO = 0.5


def _is_dialogue_text(seen: str, lines: list[str]) -> bool:
    needle = textmatch.condense(seen)
    if not needle:
        return False
    for line in lines:
        hay = textmatch.condense(line)
        if not hay:
            continue
        if needle in hay or hay in needle:
            return True
        if len(needle) >= 2 and (
            textmatch.longest_run_ratio(needle, hay) >= DIALOGUE_MATCH_RATIO
            or textmatch.bigram_coverage(needle, hay) >= DIALOGUE_MATCH_RATIO
        ):
            return True
    return False


def apply_dialogue_blacklist(verdict: dict[str, Any], lines: list[str] | None) -> dict[str, Any]:
    """只有与本镜台词对得上的画面文字才算字幕。``lines`` 为 None（读不到台词）时保持 VLM 原判。"""
    if lines is None or not verdict.get("checked"):
        return verdict
    frames = list(verdict.get("overlay_frames") or [])
    subtitle = [f for f in frames if _is_dialogue_text(str(f.get("text_seen") or ""), lines)]
    diegetic = [f for f in frames if f not in subtitle]
    return {**verdict, "subtitle_overlay": bool(subtitle), "overlay_frames": subtitle, "diegetic_frames": diegetic,
            "dialogue_lines_checked": len(lines)}


def spoken_lines_for_shot(shot_id: str) -> list[str] | None:
    """本镜台词账本里说出口的话（与字幕对齐同一份 LineSpec）；读不到返回 None，闸门保持 VLM 原判。"""
    from app.subtitles.episode import shot_line_specs  # L4，与本模块（L5）方向合法

    try:
        row = get_conn().execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
        if row is None:
            return None
        return [spec.text for spec in shot_line_specs(row) if spec.text]
    except Exception as exc:  # noqa: BLE001 读台词失败不改变闸门结论，只留痕
        _LOGGER.warning("[VIDEO_SUBTITLE_GATE] 读取镜头 %s 台词失败，保持模型原判：%s", shot_id, exc)
        return None


async def evaluate_version(job: Any, version: Any, dest: str) -> dict[str, Any] | None:
    """worker 在候选登记前调用；返回写入的结论，闸门关闭时返回 None。永不抛出。"""
    if not enabled():
        return None
    version_id = str(version["id"])
    try:
        verdict = await detect_subtitle_overlay(
            dest, call_meta={"project_id": job["project_id"], "shot_id": job["shot_id"], "version_id": version_id},
        )
    except Exception as exc:  # noqa: BLE001 未判定放行，见模块文档
        verdict = {"checked": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
        _LOGGER.warning("[VIDEO_SUBTITLE_GATE][未判定] 版本 %s 放行：%s", version_id, verdict["error"])
    else:
        verdict = apply_dialogue_blacklist(verdict, spoken_lines_for_shot(str(job["shot_id"])))
        if verdict.get("diegetic_frames"):
            _LOGGER.info("[VIDEO_SUBTITLE_GATE][画面文字放行] 版本 %s：%s", version_id, verdict["diegetic_frames"][:3])
        if verdict["subtitle_overlay"]:
            _LOGGER.warning("[VIDEO_SUBTITLE_GATE][拦截] 版本 %s 画面叠加字幕：%s", version_id, verdict["overlay_frames"][:3])
        else:
            _LOGGER.info("[VIDEO_SUBTITLE_GATE][通过] 版本 %s 检查 %s 帧无台词字幕", version_id, verdict["frames_checked"])
    write_verdict(version_id, verdict)
    return verdict
