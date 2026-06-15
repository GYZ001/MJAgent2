"""音频子系统：TTS 配音 + ASR 校验（百炼/DashScope）。

设计（结合产品讨论）：
- TTS 先生成"确定要念的内容"，ASR 校验"实际念成了什么"，正音词库负责把人名/术语读对。
- 视觉文本不改（画面仍显示"萧炎/斗之力"），但 TTS 文本可套用别名（"肖炎/豆之力"）保证读音。
- 关键术语（S/A 级）必须全部命中；普通文本用字符错误率(CER)弱判定。失败自动改写重试，仍失败则标红（不静默）。
- 当前 Seedance 出的是无声视频，故配音作为成片混音轨注入（每镜 10s，配音补齐到 10s 后按镜序拼接，混入整集成片）。

总开关 settings.audio_enabled=false 时，本模块全部跳过，保持现有无声链路。
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from app import config, hiagent
from app.db import get_conn, get_setting, now, rows_to_dicts

_PUNCT_RE = re.compile(r"[\s，。、！？；：,.!?;:\"'「」『』（）()【】\-—…·]+")

LEVELS = ("S", "A", "B")


# ---------- 总开关 ----------

def is_enabled() -> bool:
    return (get_setting("audio_enabled") or "false").strip().lower() == "true"


# ---------- 正音词库 ----------

def load_lexicon(project_id: str) -> list[dict]:
    rows = rows_to_dicts(get_conn().execute(
        "SELECT term, tts_alias, asr_aliases, level FROM pronunciation WHERE project_id=? ORDER BY length(term) DESC",
        (project_id,)).fetchall())
    out = []
    for r in rows:
        out.append({
            "term": r["term"],
            "tts_alias": (r["tts_alias"] or "").strip() or r["term"],
            "asr_aliases": json.loads(r["asr_aliases"] or "[]"),
            "level": (r["level"] or "A").upper() if (r["level"] or "A").upper() in LEVELS else "A",
        })
    return out


# ---------- 文本处理 ----------

# 全知视角的结尾悬念钩旁白（"可他不知道…/殊不知…/然而…"）念在台词【之后】；
# 其余旁白（情境画外音、人物内心OS、人群声）都是先给情境、人物再开口反应，必须念在台词【之前】。
_NARRATION_AFTER_MARKERS = (
    "可他", "可她", "可这", "可此时", "殊不知", "却不知", "然而", "但他不知", "但她不知",
    "但谁也", "而此刻", "只是此时", "谁也没想到", "没有人注意到", "没人知道", "此时的他", "此时的她",
)


def narration_after_dialogue(narration: str) -> bool:
    """该镜旁白是否应念在台词【之后】：仅全知结尾悬念钩旁白（"可他不知道…/殊不知…"）放最后，其余放台词前。
    供配音合成与 Seedance prompt 共用，保证两条链路时序口径一致。"""
    n = (narration or "").lstrip(" 　")
    return any(n.startswith(m) for m in _NARRATION_AFTER_MARKERS)


def spoken_segments(shot_row) -> list[dict]:
    """本镜配音的【有序分段】，每段标注角色（role/speaker），供分音色合成：
    role='narration' 旁白/画外音/内心OS 用旁白音色；role='dialogue' 角色台词用台词音色。
    时序：默认旁白(情境/内心)在前、台词在后；仅全知结尾钩旁白排在台词之后。"""
    dialogues = shot_row["dialogues"] if not isinstance(shot_row["dialogues"], str) else json.loads(shot_row["dialogues"] or "[]")
    dlg = [{"role": "dialogue", "speaker": (d.get("speaker") or "").strip(), "text": d.get("line", "").strip()}
           for d in (dialogues or []) if d.get("line", "").strip()]
    narration = (shot_row["narration"] or "").strip()
    if not narration:
        return dlg
    narr_seg = {"role": "narration", "speaker": "", "text": narration}
    return [*dlg, narr_seg] if narration_after_dialogue(narration) else [narr_seg, *dlg]


def spoken_text(shot_row) -> str:
    """本镜口播标准文本（拼接后的全文），用于 ASR 预检比对。时序同 spoken_segments。"""
    return "。".join(seg["text"].rstrip("。") for seg in spoken_segments(shot_row) if seg["text"])


def tts_safe(text: str, lexicon: list[dict]) -> str:
    """把标准文本里的词替换成 TTS 安全别名（保证读音）。长词优先，避免子串误替换。"""
    out = text
    for item in sorted(lexicon, key=lambda x: len(x["term"]), reverse=True):
        if item["tts_alias"] != item["term"] and item["term"] in out:
            out = out.replace(item["term"], item["tts_alias"])
    return out


def normalize_asr(text: str, lexicon: list[dict]) -> str:
    """把 ASR 文本里的别字/别名映射回标准词，便于和标准文本比对。"""
    out = text or ""
    for item in sorted(lexicon, key=lambda x: len(x["term"]), reverse=True):
        for alias in [item["tts_alias"], *item["asr_aliases"]]:
            if alias and alias != item["term"] and alias in out:
                out = out.replace(alias, item["term"])
    return out


def _strip(s: str) -> str:
    return _PUNCT_RE.sub("", s or "")


def _edit_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def cer(expected: str, got: str) -> float:
    e, g = _strip(expected), _strip(got)
    if not e:
        return 0.0 if not g else 1.0
    return round(_edit_distance(e, g) / len(e), 3)


def present_terms(text: str, lexicon: list[dict]) -> list[dict]:
    return [it for it in lexicon if it["term"] in text]


def shot_level(terms: list[dict]) -> str:
    for lv in LEVELS:  # S 优先
        if any(t["level"] == lv for t in terms):
            return lv
    return "A"


def _cer_threshold(level: str) -> float:
    key = {"S": "asr_cer_s", "A": "asr_cer_a", "B": "asr_cer_b"}.get(level, "asr_cer_a")
    try:
        return float(get_setting(key) or {"asr_cer_s": 0.03, "asr_cer_a": 0.08, "asr_cer_b": 0.18}[key])
    except (TypeError, ValueError):
        return 0.08


def check(expected: str, asr_raw: str, lexicon: list[dict]) -> dict:
    """对比标准文本与 ASR 结果：关键术语强判定 + CER 弱判定。"""
    normalized = normalize_asr(asr_raw, lexicon)
    terms = present_terms(expected, lexicon)
    level = shot_level(terms)
    missing = [t["term"] for t in terms if t["level"] in ("S", "A") and t["term"] not in normalized]
    c = cer(expected, normalized)
    ok = (not missing) and c <= _cer_threshold(level)
    return {"pass": ok, "cer": c, "level": level, "missing_terms": missing,
            "asr_normalized": normalized, "threshold": _cer_threshold(level)}


def _emphasize(safe_text: str, missing_terms: list[dict]) -> str:
    """改写策略：在读错/漏读的词两侧加停顿，降低连读吞字概率。"""
    out = safe_text
    for it in missing_terms:
        alias = it["tts_alias"]
        if alias and alias in out:
            out = out.replace(alias, f"，{alias}，")
    return re.sub(r"，{2,}", "，", out)


# ---------- 多音色分段合成（旁白音色 ≠ 角色台词音色） ----------

_SEGMENT_GAP_S = 0.35  # 旁白与台词之间的停顿，避免不同音色连读糊在一起


def _voice_for(role: str) -> str:
    if role == "narration":
        return get_setting("audio_narration_voice") or get_setting("audio_voice") or config.BAILIAN_TTS_VOICE
    return get_setting("audio_voice") or config.BAILIAN_TTS_VOICE


def _to_wav(src_bytes: bytes, fmt: str, dest: Path) -> None:
    """把一段 TTS 字节（wav/mp3）统一转成 44100/立体声 wav。"""
    tmp = dest.with_suffix(f".in.{fmt}")
    tmp.write_bytes(src_bytes)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(tmp),
                    "-ar", "44100", "-ac", "2", str(dest)], check=True, capture_output=True)
    tmp.unlink(missing_ok=True)


async def _synthesize_segments(segs: list[dict]) -> tuple[bytes, str]:
    """逐段按角色选音色 TTS，再拼成一条音轨（段间留微小停顿）。
    单段直接返回原始 TTS 字节；多段用 ffmpeg 拼接并统一为 wav。无 ffmpeg 时退回单音色全文合成（至少有声）。
    每段使用 seg['safe'] 作为实际念稿。"""
    if len(segs) == 1:
        audio = await hiagent.tts(segs[0]["safe"], voice=_voice_for(segs[0]["role"]))
        return audio, ("wav" if audio[:4] == b"RIFF" else "mp3")
    if not shutil.which("ffmpeg"):
        joined = "。".join(s["safe"].rstrip("。") for s in segs if s["safe"])
        audio = await hiagent.tts(joined, voice=_voice_for("dialogue"))
        return audio, ("wav" if audio[:4] == b"RIFF" else "mp3")
    rendered: list[tuple[bytes, str]] = []
    for seg in segs:
        audio = await hiagent.tts(seg["safe"], voice=_voice_for(seg["role"]))
        rendered.append((audio, "wav" if audio[:4] == b"RIFF" else "mp3"))
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        gap = tdp / "gap.wav"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                        "-i", "anullsrc=r=44100:cl=stereo", "-t", f"{_SEGMENT_GAP_S:.3f}", str(gap)],
                       check=True, capture_output=True)
        wavs: list[Path] = []
        for i, (audio, fmt) in enumerate(rendered):
            w = tdp / f"seg{i}.wav"
            _to_wav(audio, fmt, w)
            if i:
                wavs.append(gap)
            wavs.append(w)
        listfile = tdp / "list.txt"
        listfile.write_text(
            "\n".join(f"file '{str(p).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'" for p in wavs),
            encoding="utf-8")
        out = tdp / "out.wav"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                        "-i", str(listfile), "-ar", "44100", "-ac", "2", str(out)],
                       check=True, capture_output=True)
        return out.read_bytes(), "wav"


# ---------- 单镜配音生成（TTS + ASR 预检 + 自动改写重试） ----------

def _audio_path(project_id: str, episode_no: int, shot_no: int) -> Path:
    d = config.PROJECTS_DIR / project_id / "episodes" / str(episode_no) / "audio"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"shot{shot_no}.audio"


def _save_shot_audio(shot_id: str, episode_id: str, **fields) -> None:
    conn = get_conn()
    cols = ["shot_id", "episode_id", *fields.keys(), "updated_at"]
    vals = [shot_id, episode_id, *fields.values(), now()]
    placeholders = ",".join("?" for _ in cols)
    updates = ",".join(f"{k}=excluded.{k}" for k in [*fields.keys(), "updated_at"])
    conn.execute(
        f"INSERT INTO shot_audio({','.join(cols)}) VALUES({placeholders}) "
        f"ON CONFLICT(shot_id) DO UPDATE SET {updates}", vals)
    conn.commit()


async def generate_shot_audio(shot_row, project_row) -> dict:
    """为单镜生成配音并做 ASR 预检；失败按词库强化重写重试。结果落 shot_audio。"""
    conn = get_conn()
    ep = conn.execute("SELECT episode_no FROM episodes WHERE id=?", (shot_row["episode_id"],)).fetchone()
    src = spoken_text(shot_row)
    if not src.strip():
        _save_shot_audio(shot_row["id"], shot_row["episode_id"], source_text="", tts_text="",
                         audio_path=None, asr_text=None, cer=-1, level="A", status="empty",
                         regen_count=0, error=None)
        return {"status": "empty"}

    lexicon = load_lexicon(project_row["id"])
    # 分段：旁白(旁白音色) + 台词(角色音色)，按听感时序。每段单独 tts_safe 出念稿。
    segs = spoken_segments(shot_row)
    for seg in segs:
        seg["safe"] = tts_safe(seg["text"], lexicon)
    max_regen = max(int(get_setting("audio_max_regen") or 2), 0)
    dest = _audio_path(project_row["id"], ep["episode_no"], shot_row["shot_no"])
    last = {"asr": "", "cer": -1, "level": "A"}
    real_dest = dest.with_suffix(".wav")
    for attempt in range(max_regen + 1):
        audio, fmt = await _synthesize_segments(segs)
        real_dest = dest.with_suffix(f".{fmt}")
        real_dest.write_bytes(audio)
        asr_raw = await hiagent.asr(audio, fmt=fmt)
        res = check(src, asr_raw, lexicon)
        last = {"asr": res["asr_normalized"], "cer": res["cer"], "level": res["level"]}
        tts_text = " || ".join(s["safe"] for s in segs)
        if res["pass"]:
            _save_shot_audio(shot_row["id"], shot_row["episode_id"], source_text=src, tts_text=tts_text,
                             audio_path=str(real_dest), asr_text=res["asr_normalized"], cer=res["cer"],
                             level=res["level"], status="ok", regen_count=attempt, error=None)
            return {"status": "ok", **last}
        # 失败 → 用词库对漏读/读错的词在各段念稿里加停顿后重试
        missing = [m for m in present_terms(src, lexicon) if m["term"] in res["missing_terms"]]
        if missing:
            for seg in segs:
                seg["safe"] = _emphasize(seg["safe"], missing)
    # 用尽重试仍未通过：保留最后一版音频，但标红（失败要响），供人工补词库后重生
    _save_shot_audio(shot_row["id"], shot_row["episode_id"], source_text=src,
                     tts_text=" || ".join(s["safe"] for s in segs),
                     audio_path=str(real_dest) if real_dest.exists() else None,
                     asr_text=last["asr"], cer=last["cer"], level=last["level"], status="failed",
                     regen_count=max_regen, error=f"ASR 预检未通过（CER={last['cer']}，疑似读错关键词）")
    return {"status": "failed", **last}


async def generate_episode_audio(episode_id: str, *, concurrency: int = 4) -> dict:
    """为本集所有镜头生成配音（并发）。返回汇总。"""
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        raise ValueError("剧集不存在")
    project = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    shots = rows_to_dicts(conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)).fetchall())
    sem = asyncio.Semaphore(max(concurrency, 1))

    async def one(s):
        async with sem:
            try:
                return await generate_shot_audio(s, project)
            except Exception as exc:  # noqa: BLE001 单镜失败不拖垮整集，落库标红
                _save_shot_audio(s["id"], episode_id, source_text=spoken_text(s), status="failed",
                                 error=str(exc)[:300], cer=-1, regen_count=0)
                return {"status": "failed", "error": str(exc)[:120]}

    results = await asyncio.gather(*(one(s) for s in shots))
    summary = {"total": len(shots)}
    for r in results:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    return summary


# ---------- 整集配音轨（每镜补齐到 10s 后按镜序拼接），供成片混音 ----------

def _probe_duration(path: str) -> float | None:
    """用 ffprobe 取媒体真实时长（秒）。失败返回 None。"""
    try:
        raw = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
            capture_output=True, text=True, check=True).stdout.strip()
        d = float(raw) if raw else 0.0
        return d if d > 0 else None
    except (subprocess.CalledProcessError, ValueError, OSError):
        return None


def _adopted_video_durations(conn, episode_id: str) -> dict[str, float]:
    """每镜【已采用视频】的真实时长（秒），用于把配音逐镜对齐到对应视频段，避免累积失步。"""
    rows = rows_to_dicts(conn.execute(
        """SELECT s.id AS shot_id, v.video_path
           FROM shots s JOIN shot_versions v ON v.id = s.adopted_version_id
           WHERE s.episode_id=? AND v.status='succeeded' AND v.video_path IS NOT NULL""",
        (episode_id,)).fetchall())
    out: dict[str, float] = {}
    for r in rows:
        if r["video_path"] and Path(r["video_path"]).exists():
            d = _probe_duration(r["video_path"])
            if d:
                out[r["shot_id"]] = d
    return out


def build_episode_audio_track(episode_id: str, out_path: Path) -> Path | None:
    """把各镜配音按镜序拼成整集音轨：每镜补静音/截断到【该镜已采用视频的真实时长】，逐镜与视频段对齐。
    Seedance 出片常非精确 10.0s，若刚性按 10s 铺音轨会逐镜累积失步、并被成片 -shortest 截尾，
    因此优先按 ffprobe 实测时长对齐；拿不到时退回固定 10s。没有任何可用配音则返回 None。需要 ffmpeg。"""
    if not shutil.which("ffmpeg"):
        return None
    conn = get_conn()
    shots = rows_to_dicts(conn.execute(
        "SELECT s.shot_no, s.id, s.duration_s FROM shots s WHERE s.episode_id=? ORDER BY s.shot_no", (episode_id,)).fetchall())
    if not shots:
        return None
    video_durations = _adopted_video_durations(conn, episode_id)
    audio_by_shot = {a["shot_id"]: a for a in rows_to_dicts(conn.execute(
        "SELECT shot_id, audio_path, status FROM shot_audio WHERE episode_id=?", (episode_id,)).fetchall())}
    if not any(a.get("audio_path") and Path(a["audio_path"]).exists() for a in audio_by_shot.values()):
        return None
    with tempfile.TemporaryDirectory() as td:
        segs = []
        for s in shots:
            seg = Path(td) / f"seg{s['shot_no']}.wav"
            # 优先用已采用视频的实测时长；拿不到时退回本镜分镜 duration_s（5~10s），而非固定 10s。
            fallback_dur = float(s.get("duration_s") or config.FIXED_VIDEO_DURATION_S)
            dur = f"{video_durations.get(s['id'], fallback_dur):.3f}"
            a = audio_by_shot.get(s["id"])
            ap = a.get("audio_path") if a else None
            if ap and Path(ap).exists():
                # 开场留白：先延迟 SPEECH_LEAD_IN_S 让本镜动作建立，人声再起（adelay，单位毫秒）；
                # 之后补静音到该镜视频时长（apad），超长则截断（-t），统一采样率/声道。
                lead_ms = int(config.SPEECH_LEAD_IN_S * 1000)
                af = f"adelay={lead_ms}:all=1,apad" if lead_ms > 0 else "apad"
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", ap,
                                "-af", af, "-t", dur, "-ar", "44100", "-ac", "2", str(seg)],
                               check=True, capture_output=True)
            else:
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                                "-i", "anullsrc=r=44100:cl=stereo", "-t", dur, str(seg)],
                               check=True, capture_output=True)
            segs.append(seg)
        listfile = Path(td) / "alist.txt"
        listfile.write_text("\n".join(f"file '{str(p).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'" for p in segs), encoding="utf-8")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                        "-i", str(listfile), "-ar", "44100", "-ac", "2", str(out_path)],
                       check=True, capture_output=True)
    return out_path if out_path.exists() else None


def episode_audio_status(episode_id: str) -> dict:
    rows = rows_to_dicts(get_conn().execute(
        "SELECT shot_id, status, cer, level, source_text, asr_text, error FROM shot_audio WHERE episode_id=?",
        (episode_id,)).fetchall())
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"enabled": is_enabled(), "counts": counts, "shots": rows}
