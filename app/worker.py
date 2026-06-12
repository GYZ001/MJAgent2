"""视频生成队列（PRD §4.5）：asyncio worker、幂等、成本熔断、重启恢复、自动质检与重抽。"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from app import config, hiagent
from app.compiler import idem_key as make_idem_key, shot_cost_cny
from app.db import get_conn, get_setting, log_provider_call, new_id, now
from app.hiagent import ProviderError

_queue: asyncio.Queue[str] = asyncio.Queue()
_workers: list[asyncio.Task] = []


# ---------- 落盘路径 ----------

def _video_path(project_id: str, episode_no: int, shot_no: int, version_no: int) -> Path:
    d = config.PROJECTS_DIR / project_id / "episodes" / str(episode_no) / "shots" / str(shot_no)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"v{version_no}.mp4"


# ---------- 成本熔断 ----------

def episode_cost(episode_id: str) -> float:
    row = get_conn().execute(
        """SELECT COALESCE(SUM(v.cost_cny), 0) AS c FROM shot_versions v
           JOIN shots s ON s.id = v.shot_id
           WHERE s.episode_id = ? AND v.status IN ('succeeded', 'running', 'queued')""",
        (episode_id,),
    ).fetchone()
    return float(row["c"])


def _budget_exceeded(episode_id: str) -> bool:
    limit = float(get_setting("episode_cost_limit_cny") or 100)
    return episode_cost(episode_id) >= limit


# ---------- 入队 ----------

def _load_shot_model(shot_row) -> "object":
    from app.schemas import Shot
    return Shot(
        shot_no=shot_row["shot_no"], duration_s=shot_row["duration_s"], shot_size=shot_row["shot_size"],
        camera_move=shot_row["camera_move"], scene_setting=shot_row["scene_setting"],
        characters=json.loads(shot_row["characters"] or "[]"), action_desc=shot_row["action_desc"],
        narration=shot_row["narration"], dialogues=json.loads(shot_row["dialogues"] or "[]"),
        transition=shot_row["transition"] or "硬切", continuity_from_prev=bool(shot_row["continuity_from_prev"]),
    )


def enqueue_shot(shot_id: str, *, prompt_override: str | None = None,
                 extra_negative: list[str] | None = None, reroll: bool = False,
                 after_shot_id: str | None = None) -> dict:
    """为镜头创建新版本并入队。幂等：相同 idem_key 的成功版本直接复用（reroll 时跳过复用）。

    after_shot_id：连贯链依赖——本镜头需等该镜头出成片后，取其尾帧作为首帧（PRD §5.4 第 3 层）。
    """
    from app.compiler import compile_prompt, normalize_video_args
    from app.refs import refs_as_image_inputs
    from app.schemas import Bible

    conn = get_conn()
    shot_row = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot_row:
        raise ValueError(f"镜头不存在：{shot_id}")
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (shot_row["episode_id"],)).fetchone()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    if ep["status"] not in ("confirmed", "generating", "done"):
        raise ValueError("分镜脚本未确认，不能生成视频（PRD 原则 P3：贵的环节前人工把关）")

    bible = Bible.model_validate(json.loads(project["bible_json"]))
    shot = _load_shot_model(shot_row)

    use_refs = get_setting("use_character_refs") == "true"
    max_refs = int(get_setting("max_ref_images") or 2)
    ref_inputs = refs_as_image_inputs(bible, shot.characters, max_refs) if use_refs else []
    chaining = get_setting("use_first_frame_chaining") == "true" and after_shot_id is not None

    prev_action = None
    if chaining:
        prev_row = conn.execute("SELECT action_desc FROM shots WHERE id=?", (after_shot_id,)).fetchone()
        prev_action = prev_row["action_desc"] if prev_row else None

    # first_frame 与 reference_image 互斥（实测 400）：链中镜头 prompt 用"延续首帧"指令，
    # 链头/独立镜头用"与参考图一致"指令
    prompt_text = (normalize_video_args(prompt_override) if prompt_override else
                   compile_prompt(
                       shot, bible, extra_negative,
                       with_refs=bool(ref_inputs) and not chaining, chained=chaining,
                       prev_action=prev_action))

    # 幂等键用稳定标识（定妆照文件路径 + 依赖镜头），不用体积巨大的 data URL
    ref_paths = [c.ref_image_path or "" for c in bible.characters if c.name in shot.characters][:max_refs]
    key_material = prompt_text + "|refs:" + ",".join(ref_paths) + f"|after:{after_shot_id or ''}"
    if reroll:
        key = make_idem_key(key_material + f"#reroll{time.time()}")
    else:
        key = make_idem_key(key_material)
        existing = conn.execute(
            "SELECT * FROM shot_versions WHERE shot_id=? AND idem_key=? AND status='succeeded' LIMIT 1",
            (shot_id, key)).fetchone()
        if existing:
            return {"reused": True, "version_id": existing["id"]}

    version_no = (conn.execute(
        "SELECT COALESCE(MAX(version_no), 0) AS m FROM shot_versions WHERE shot_id=?",
        (shot_id,)).fetchone()["m"]) + 1
    version_id = new_id("ver")
    conn.execute(
        "INSERT INTO shot_versions(id, shot_id, version_no, prompt_text, idem_key, status, created_at, image_inputs) "
        "VALUES(?,?,?,?,?, 'queued', ?, ?)",
        (version_id, shot_id, version_no, prompt_text, key, now(),
         json.dumps({"ref_paths": [p for p in ref_paths if p], "after_shot_id": after_shot_id}, ensure_ascii=False)))
    job_id = new_id("job")
    conn.execute(
        "INSERT INTO jobs(id, kind, shot_id, version_id, episode_id, project_id, status, created_at, updated_at, after_shot_id) "
        "VALUES(?, 'video', ?, ?, ?, ?, 'queued', ?, ?, ?)",
        (job_id, shot_id, version_id, ep["id"], project["id"], now(), now(), after_shot_id))
    conn.execute("UPDATE episodes SET status='generating' WHERE id=? AND status='confirmed'", (ep["id"],))
    conn.commit()
    _queue.put_nowait(job_id)
    return {"reused": False, "version_id": version_id, "job_id": job_id}


# ---------- 执行 ----------

def _set_job(job_id: str, status: str, error: str | None = None) -> None:
    conn = get_conn()
    conn.execute("UPDATE jobs SET status=?, error=?, updated_at=? WHERE id=?", (status, error, now(), job_id))
    conn.commit()


def _set_version(version_id: str, **fields) -> None:
    conn = get_conn()
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE shot_versions SET {cols} WHERE id=?", (*fields.values(), version_id))
    conn.commit()


def _extract_last_frame(video_path: str) -> str | None:
    """从已落盘视频抽尾帧（实测网关成功响应不回传 last_frame_url，必须本地抽）。"""
    if not shutil.which("ffmpeg"):
        return None
    out = str(Path(video_path).with_suffix("")) + "_last.jpg"
    if not Path(out).exists():
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-sseof", "-0.2", "-i", video_path,
                 "-vframes", "1", "-q:v", "3", out],
                check=True, capture_output=True)
        except subprocess.CalledProcessError:
            return None
    return out if Path(out).exists() else None


def _resolve_first_frame(after_shot_id: str) -> tuple[str | None, str]:
    """取依赖镜头已采用（或最新成功）版本的尾帧 data URL。返回 (data_url|None, 状态说明)。"""
    conn = get_conn()
    pred = conn.execute("SELECT * FROM shots WHERE id=?", (after_shot_id,)).fetchone()
    if not pred:
        return None, "依赖镜头不存在"
    version = None
    if pred["adopted_version_id"]:
        version = conn.execute(
            "SELECT * FROM shot_versions WHERE id=? AND status='succeeded'",
            (pred["adopted_version_id"],)).fetchone()
    if not version:
        version = conn.execute(
            "SELECT * FROM shot_versions WHERE shot_id=? AND status='succeeded' ORDER BY version_no DESC LIMIT 1",
            (after_shot_id,)).fetchone()
    if version and version["video_path"]:
        frame = _extract_last_frame(version["video_path"])
        if frame:
            return hiagent.data_url_from_file(frame), "ok"
        if version["last_frame_url"]:  # 兜底：极少数情况网关有回传
            return version["last_frame_url"], "ok"
        return None, "unavailable"
    active = conn.execute(
        "SELECT COUNT(*) c FROM jobs WHERE shot_id=? AND status IN ('queued','running')",
        (after_shot_id,)).fetchone()["c"]
    return None, "waiting" if active else "unavailable"


async def _run_job(job_id: str) -> None:
    conn = get_conn()
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job or job["status"] not in ("queued", "running"):
        return
    version = conn.execute("SELECT * FROM shot_versions WHERE id=?", (job["version_id"],)).fetchone()
    shot = conn.execute("SELECT * FROM shots WHERE id=?", (job["shot_id"],)).fetchone()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (job["episode_id"],)).fetchone()

    if _budget_exceeded(job["episode_id"]):
        _set_job(job_id, "paused_budget", f"本集成本已达上限 ¥{get_setting('episode_cost_limit_cny')}，队列暂停。可在设置中调高后重试")
        _set_version(version["id"], status="paused_budget")
        return

    # 连贯链：等待依赖镜头出尾帧。依赖仍在生成→稍后重新入队；依赖已失败→不带首帧继续（锚点串兜底）
    first_frame_url: str | None = None
    if job["after_shot_id"] and get_setting("use_first_frame_chaining") == "true" and not version["provider_task_id"]:
        first_frame_url, state = _resolve_first_frame(job["after_shot_id"])
        if state == "waiting":
            await asyncio.sleep(8)
            _queue.put_nowait(job_id)
            return

    _set_job(job_id, "running")
    started = time.time()
    try:
        task_id = version["provider_task_id"]
        if not task_id:  # 重启恢复时可能已有 task_id，直接续轮询
            _set_version(version["id"], status="running")
            # 实测约束：first_frame 与 reference_image 不能混用（网关 400）。
            # 链中镜头只传首帧（角色形象由链头的定妆照沿帧传递）；链头/独立镜头传定妆照。
            image_inputs: list[tuple[str, str]] = []
            meta = json.loads(version["image_inputs"] or "{}")
            if first_frame_url:
                image_inputs.append((first_frame_url, "first_frame"))
            else:
                for path in meta.get("ref_paths", []):
                    try:
                        image_inputs.append((hiagent.data_url_from_file(path), "reference_image"))
                    except OSError:
                        pass
            meta["first_frame_used"] = bool(first_frame_url)
            _set_version(version["id"], image_inputs=json.dumps(meta, ensure_ascii=False))
            task_id = await hiagent.create_video_task(version["prompt_text"], image_urls=image_inputs)
            _set_version(version["id"], provider_task_id=task_id)

        deadline = time.time() + config.VIDEO_POLL_BUDGET
        result = None
        while time.time() < deadline:
            result = await hiagent.poll_video_task(task_id)
            if result["status"] in ("succeeded", "failed"):
                break
            await asyncio.sleep(config.VIDEO_POLL_INTERVAL)
        if result is None or result["status"] not in ("succeeded", "failed"):
            raise ProviderError(f"轮询超出 {config.VIDEO_POLL_BUDGET // 60} 分钟预算，任务 {task_id} 仍未完成；可稍后对该镜头重试")
        if result["status"] == "failed":
            raise ProviderError(f"Seedance 任务失败：{result['error'][:400]}")

        dest = _video_path(job["project_id"], ep["episode_no"], shot["shot_no"], version["version_no"])
        await hiagent.download(result["video_url"], str(dest))
        latency = round(time.time() - started, 1)
        cost = shot_cost_cny(shot["duration_s"])
        _set_version(version["id"], status="succeeded", video_path=str(dest),
                     last_frame_url=result["last_frame_url"], cost_cny=cost, latency_s=latency)
        _set_job(job_id, "succeeded")
        # 无已采用版本时自动采用本次成功版本
        conn.execute("UPDATE shots SET adopted_version_id=? WHERE id=? AND adopted_version_id IS NULL",
                     (version["id"], job["shot_id"]))
        conn.commit()
        await _maybe_auto_qa(job, version["id"], str(dest))
    except (ProviderError, Exception) as exc:  # noqa: BLE001 失败要响：原样透出
        message = str(exc)[:500]
        _set_version(version["id"], status="failed", error=message)
        _set_job(job_id, "failed", message)


async def _maybe_auto_qa(job, version_id: str, video_path: str) -> None:
    """自动质检 + 一次自动重抽（QA 失败不阻塞流程，只标记未质检）。"""
    if get_setting("auto_qa") != "true" or not shutil.which("ffmpeg"):
        return
    conn = get_conn()
    try:
        frames = _extract_frames(video_path)
        shot = conn.execute("SELECT * FROM shots WHERE id=?", (job["shot_id"],)).fetchone()
        project = conn.execute("SELECT * FROM projects WHERE id=?", (job["project_id"],)).fetchone()
        bible = json.loads(project["bible_json"])
        anchor_map = {c["name"]: c["appearance_canonical"] for c in bible["characters"]}
        anchors = [anchor_map[n] for n in json.loads(shot["characters"] or "[]") if n in anchor_map]
        from app.stages import qa_shot
        qa = await qa_shot(frames, shot["action_desc"], shot["scene_setting"], anchors)
        _set_version(version_id, qa_json=json.dumps(qa, ensure_ascii=False))
        threshold = float(get_setting("auto_retake_threshold") or 0.6)
        version = conn.execute("SELECT * FROM shot_versions WHERE id=?", (version_id,)).fetchone()
        if 0 <= qa.get("overall", -1) < threshold and version["version_no"] == 1 and qa.get("issues"):
            enqueue_shot(job["shot_id"], extra_negative=qa["issues"][:3],
                         after_shot_id=job["after_shot_id"])
    except Exception as exc:  # noqa: BLE001 QA 异常只记录，不影响已落盘的视频
        _set_version(version_id, qa_json=json.dumps({"overall": -1, "issues": [f"质检未完成：{exc}"]}, ensure_ascii=False))
        log_provider_call("vlm_qa", config.MODEL_VLM, "QA_ERROR", None, 0, error=str(exc))


def _extract_frames(video_path: str) -> list[str]:
    """ffmpeg 抽 首/中/尾 3 帧，返回 base64 列表。"""
    frames = []
    with tempfile.TemporaryDirectory() as td:
        for i, pos in enumerate(("0", "50%", "99%")):
            out = Path(td) / f"f{i}.jpg"
            vf = "select=eq(n\\,0)" if pos == "0" else None
            cmd = ["ffmpeg", "-y", "-loglevel", "error"]
            if pos == "0":
                cmd += ["-i", video_path, "-vf", "select=eq(n\\,0)", "-vframes", "1"]
            else:
                # 用 ffprobe 拿时长再定位
                dur = float(subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", video_path],
                    capture_output=True, text=True, check=True).stdout.strip() or 5)
                ts = dur * (0.5 if pos == "50%" else 0.97)
                cmd += ["-ss", f"{ts:.2f}", "-i", video_path, "-vframes", "1"]
            cmd += ["-q:v", "4", str(out)]
            subprocess.run(cmd, check=True, capture_output=True)
            frames.append(hiagent.encode_image_file(str(out)))
    return frames


# ---------- worker 生命周期 ----------

async def _worker_loop(name: str) -> None:
    while True:
        job_id = await _queue.get()
        try:
            await _run_job(job_id)
        except Exception as exc:  # noqa: BLE001 worker 永不死亡，但错误必须落库
            _set_job(job_id, "failed", f"worker 异常：{exc}")
        finally:
            _queue.task_done()


def recover_and_start(loop_concurrency: int | None = None) -> None:
    """启动时恢复队列（PRD §4.5 验收：中途杀进程重启后队列状态可恢复）。"""
    conn = get_conn()
    rows = conn.execute("SELECT id FROM jobs WHERE status IN ('queued', 'running') ORDER BY created_at").fetchall()
    for r in rows:
        _queue.put_nowait(r["id"])
    n = loop_concurrency or int(get_setting("video_concurrency") or 2)
    for i in range(n):
        _workers.append(asyncio.get_running_loop().create_task(_worker_loop(f"w{i}")))


def retry_paused(episode_id: str) -> int:
    """成本上限调高后，恢复因预算暂停的任务。"""
    conn = get_conn()
    rows = conn.execute("SELECT id FROM jobs WHERE episode_id=? AND status='paused_budget'", (episode_id,)).fetchall()
    for r in rows:
        conn.execute("UPDATE jobs SET status='queued', error=NULL, updated_at=? WHERE id=?", (now(), r["id"]))
        _queue.put_nowait(r["id"])
    conn.commit()
    return len(rows)


# ---------- 成片台：汇总状态 / 拼接 / 导出 ----------

def episode_mix_status(episode_id: str) -> dict:
    """返回：每镜是否已有成片（采用版），以及整体状态。"""
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
        if s["adopted_version_id"]:
            v = conn.execute(
                "SELECT * FROM shot_versions WHERE id=? AND status='succeeded'",
                (s["adopted_version_id"],)).fetchone()
            if v and v["video_path"]:
                from app.config import PROJECTS_DIR
                vid = "/media/" + v["video_path"].removeprefix(str(PROJECTS_DIR) + "/")
                ready += 1
        out.append({"shot_id": s["id"], "shot_no": s["shot_no"],
                    "duration_s": s["duration_s"], "video_url": vid,
                    "has_adopted": bool(vid)})
    return {
        "episode_id": ep["id"],
        "title": ep["title"],
        "episode_no": ep["episode_no"],
        "shots_total": len(shots),
        "shots_ready": ready,
        "ready": len(shots) > 0 and ready == len(shots),
        "final_video_url": _existing_final_url(ep),
        "shots": out,
    }


def _existing_final_url(ep_row) -> str | None:
    from app.config import PROJECTS_DIR
    final_path = _final_video_path(ep_row["project_id"], ep_row["episode_no"])
    if final_path.exists():
        return "/media/" + str(final_path).removeprefix(str(PROJECTS_DIR) + "/")
    return None


def _final_video_path(project_id: str, episode_no: int) -> Path:
    d = config.PROJECTS_DIR / project_id / "episodes" / str(episode_no) / "final"
    d.mkdir(parents=True, exist_ok=True)
    return d / "episode.mp4"


def _adopted_video_paths(episode_id: str) -> list[tuple[int, str]]:
    """按镜头顺序返回 (shot_no, video_path)，仅含已有成片的镜头。"""
    conn = get_conn()
    rows = conn.execute(
        """SELECT s.shot_no, v.video_path
           FROM shots s
           JOIN shot_versions v ON v.id = s.adopted_version_id
           WHERE s.episode_id=? AND v.status='succeeded' AND v.video_path IS NOT NULL
           ORDER BY s.shot_no""",
        (episode_id,)).fetchall()
    return [(r["shot_no"], r["video_path"]) for r in rows]


def concatenate_episode(episode_id: str) -> dict:
    """把本集所有已采用的镜头顺序拼接成一个 MP4。
    返回 {video_url, shots, total_duration_s}。若系统未装 ffmpeg 则返回占位说明。
    """
    from pathlib import Path as _P
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        raise ValueError("剧集不存在")
    pieces = _adopted_video_paths(episode_id)
    if not pieces:
        raise ValueError("本集没有任何已采用的视频片段，先生成/采用后再试")

    final_path = _final_video_path(ep["project_id"], ep["episode_no"])
    if not shutil.which("ffmpeg"):
        # 缺 ffmpeg 的保底：回传首个片段 URL，前端提示用户安装 ffmpeg
        first = next(p for p in pieces if p[1])
        from app.config import PROJECTS_DIR
        return {
            "video_url": "/media/" + first[1].removeprefix(str(PROJECTS_DIR) + "/"),
            "shots": len(pieces),
            "total_duration_s": 10 * len(pieces),
            "ffmpeg_missing": True,
            "note": "服务端缺少 ffmpeg，已临时回退为首个片段的直链；请安装 ffmpeg 后重新合成",
        }

    # 用 concat demuxer（支持不同编码的 mp4 也能直接粘，画质不重编码）
    with tempfile.TemporaryDirectory() as td:
        listfile = _P(td) / "list.txt"
        lines = []
        for _, vpath in pieces:
            # concat demuxer 要求绝对路径并转义单引号
            safe = vpath.replace("'", "'\\''")
            lines.append(f"file '{safe}'")
        listfile.write_text("\n".join(lines), encoding="utf-8")
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-f", "concat", "-safe", "0", "-i", str(listfile),
             "-c", "copy", "-movflags", "+faststart",
             str(final_path)],
            check=True, capture_output=True)

    total_dur = 0
    try:
        for _, vpath in pieces:
            raw = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", vpath], capture_output=True, text=True, check=True
            ).stdout.strip()
            total_dur += float(raw) if raw else 0
    except (subprocess.CalledProcessError, ValueError):
        total_dur = 10 * len(pieces)

    from app.config import PROJECTS_DIR
    return {
        "video_url": "/media/" + str(final_path).removeprefix(str(PROJECTS_DIR) + "/"),
        "shots": len(pieces),
        "total_duration_s": round(total_dur, 1),
        "ffmpeg_missing": False,
    }
