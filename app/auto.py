"""一键全自动成片编排器。

把整条流水线串起来：
  人物谱 → 定妆照 + 分集（并行）→ 每集[剧本 → 分镜 → 自动确认 → 参考图视频] 并行 → 合成成片。

两条核心原则：
1. 自适应：每一步先看 DB 当前进度，只补做缺失的部分；已完成的跳过，绝不重复花钱/重复请求。
   因此随时可以重复点击「一键全自动」，它会从断点继续，而不是从头再来。
2. 高并发：图像/视频走 worker 共享队列（auto_concurrency 个常驻 worker 同时消费）；
   剧本/分镜 LLM 由 auto_storyboard_concurrency 限流；各集流水线作为协程并行推进，互不阻塞。

成本护栏：视频是花钱环节（¥0.8/秒）。沿用「每集成本上限」（episode_cost_limit_cny），
某集触顶则该集视频暂停并在进度里报红，其余集继续——不静默吞掉（PRD 原则 P2）。
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path

from app import config, worker
from app.db import get_conn, get_setting, now, rows_to_dicts, set_setting

# 轮询 DB 等待队列阶段完成的间隔（秒）
_POLL = 5.0
# 单个镜头视频在「无在跑任务且未完成」时的最大重试次数（应对偶发网关失败）
_MAX_RETRY = 2

_tasks: dict[str, asyncio.Task] = {}
_states: dict[str, dict] = {}


# ---------- 状态与日志（供前端轮询展示） ----------

def _state(pid: str) -> dict:
    return _states.setdefault(pid, {"running": False, "phase": None, "log": [], "error": None,
                                    "started_at": None, "updated_at": None})


def is_running(pid: str) -> bool:
    t = _tasks.get(pid)
    return bool(t and not t.done())


def _log(pid: str, msg: str) -> None:
    st = _state(pid)
    st["log"].append({"t": now(), "msg": msg})
    st["log"] = st["log"][-120:]
    st["updated_at"] = now()


def _phase(pid: str, phase: str) -> None:
    _state(pid)["phase"] = phase
    _state(pid)["updated_at"] = now()


class _Skip(Exception):
    """某集无法继续（需人工处理），跳过该集但不影响其它集。"""


# ---------- 进度（从 DB 实时统计，重启后仍可见） ----------

def _video_ok(conn, adopted_version_id: str | None) -> bool:
    if not adopted_version_id:
        return False
    v = conn.execute("SELECT status, video_path FROM shot_versions WHERE id=?", (adopted_version_id,)).fetchone()
    return bool(v and v["status"] == "succeeded" and v["video_path"])


def _progress(pid: str) -> dict:
    conn = get_conn()
    p = conn.execute("SELECT bible_status, plan_status, refs_status FROM projects WHERE id=?", (pid,)).fetchone()
    if not p:
        return {}
    eps = rows_to_dicts(conn.execute("SELECT id, status, screenplay_status, screenplay_json FROM episodes WHERE project_id=?", (pid,)).fetchall())
    shots = rows_to_dicts(conn.execute(
        "SELECT s.* FROM shots s JOIN episodes e ON e.id=s.episode_id WHERE e.project_id=?", (pid,)).fetchall())
    kf = sum(1 for s in shots if worker.shot_keyframes_ready(s))
    vid = sum(1 for s in shots if _video_ok(conn, s["adopted_version_id"]))
    return {
        "bible": p["bible_status"], "refs": p["refs_status"], "plan": p["plan_status"],
        "episodes_total": len(eps), "episodes_done": sum(1 for e in eps if e["status"] == "done"),
        "screenplays_ready": sum(1 for e in eps if e["screenplay_status"] == "ready" and e["screenplay_json"]),
        "shots_total": len(shots), "shots_keyframed": kf, "shots_video": vid,
    }


def _export_dir(pid: str) -> str:
    return (get_setting(f"export_dir:{pid}") or "").strip()


def status(pid: str) -> dict:
    st = _state(pid)
    return {
        "running": is_running(pid),
        "phase": st.get("phase"),
        "error": st.get("error"),
        "log": st.get("log", [])[-40:],
        "started_at": st.get("started_at"),
        "updated_at": st.get("updated_at"),
        "export_dir": _export_dir(pid),
        "progress": _progress(pid),
    }


# ---------- 启动 / 取消 ----------

def start(pid: str, export_dir: str | None = None) -> None:
    if export_dir is not None:
        # 记住导出目录，供本次运行与下次预填使用（空串=清除）
        set_setting(f"export_dir:{pid}", export_dir.strip())
    st = _state(pid)
    st.update(running=True, error=None, phase="启动", log=[], started_at=now(), updated_at=now())
    t = asyncio.create_task(_run(pid))
    _tasks[pid] = t
    t.add_done_callback(lambda _t, p=pid: _tasks.pop(p, None))


def cancel(pid: str) -> bool:
    t = _tasks.get(pid)
    if t and not t.done():
        t.cancel()
        st = _state(pid)
        st["running"] = False
        st["phase"] = "已取消"
        st["updated_at"] = now()
        return True
    return False


# ---------- 主流程 ----------

async def _run(pid: str) -> None:
    st = _state(pid)
    try:
        worker.ensure_workers(max(int(get_setting("auto_concurrency") or 24), 1))
        export_dir = _export_dir(pid)
        if export_dir:
            try:
                Path(export_dir).mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise RuntimeError(f"导出目录不可用：{export_dir}（{e}）")
            _log(pid, f"成片将自动导出到：{export_dir}")
        else:
            _log(pid, "未设置导出目录：只在成片台生成整集成品，不另存到外部文件夹")
        await _ensure_bible(pid)
        # 定妆照与分集互不依赖（都只需人物谱），并行推进
        await asyncio.gather(_ensure_refs(pid), _ensure_plan(pid))
        # 分集 + 初始定妆照就绪后，按 20 集分段刷新定妆照（外观大变则图生图重绘并切分集区间）
        await _ensure_portraits(pid)

        conn = get_conn()
        eps = rows_to_dicts(conn.execute(
            "SELECT id, episode_no FROM episodes WHERE project_id=? ORDER BY episode_no", (pid,)).fetchall())
        if not eps:
            raise RuntimeError("分集后没有任何剧集")
        _phase(pid, f"逐集成片（共 {len(eps)} 集，并行）")
        sb_sem = asyncio.Semaphore(max(int(get_setting("auto_storyboard_concurrency") or 8), 1))
        await asyncio.gather(*[_episode_pipeline(pid, e["id"], e["episode_no"], sb_sem) for e in eps])

        prog = _progress(pid)
        done, total = prog.get("episodes_done", 0), prog.get("episodes_total", 0)
        if done >= total:
            _phase(pid, "全部完成 ✅")
            _log(pid, f"全自动成片完成：{total} 集已出片")
        else:
            _phase(pid, f"完成（{done}/{total} 集出片，其余见日志）")
            _log(pid, f"部分集需人工处理：已出片 {done}/{total}，未完成的集请查看上方日志/各工作台")
    except asyncio.CancelledError:
        _phase(pid, "已取消")
        _log(pid, "已取消（已入队的关键帧/视频会继续跑完，可稍后重新点击从断点续做）")
        raise
    except Exception as exc:  # noqa: BLE001 失败要响
        st["error"] = str(exc)[:800]
        _phase(pid, "中断")
        _log(pid, f"流水线中断：{exc}")
    finally:
        st["running"] = False
        st["updated_at"] = now()


# ---------- 各阶段 ----------

def _all_refs_ready(project_row) -> bool:
    if not project_row["bible_json"]:
        return False
    chars = json.loads(project_row["bible_json"]).get("characters", [])
    if not chars:
        return True
    return all(c.get("ref_image_path") and Path(c["ref_image_path"]).exists() for c in chars)


async def _ensure_bible(pid: str) -> None:
    from app import api
    conn = get_conn()
    p = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if p["bible_status"] == "ready" and p["bible_json"]:
        _log(pid, "人物谱已存在，跳过")
        return
    _phase(pid, "谱写人物谱")
    _log(pid, "开始谱写人物谱")
    conn.execute("UPDATE projects SET bible_status='running', bible_error=NULL WHERE id=?", (pid,))
    conn.commit()
    # 把当前 auto 任务登记为该项目的人物谱在跑任务，否则 _recover_orphan_bible_* 会把
    # 这个正在 inline await 的合法任务误判为孤儿、立刻翻成 failed（前端轮询 /projects 即触发）。
    cur = asyncio.current_task()
    if cur is not None:
        api._track_bible_task(pid, cur)
    try:
        await api._bible_task(pid, trigger_full_refs=False)
    finally:
        if api._bible_tasks.get(pid) is cur:
            api._bible_tasks.pop(pid, None)
    p = conn.execute("SELECT bible_status, bible_error FROM projects WHERE id=?", (pid,)).fetchone()
    if p["bible_status"] != "ready":
        raise RuntimeError(f"人物谱生成失败：{p['bible_error']}")
    _log(pid, "人物谱完成")


async def _ensure_refs(pid: str) -> None:
    from app import api
    conn = get_conn()
    p = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if _all_refs_ready(p):
        _log(pid, "定妆照已齐备，跳过")
        return
    _log(pid, "开始生成定妆照")
    conn.execute("UPDATE projects SET refs_status='running', refs_error=NULL, refs_target=NULL WHERE id=?", (pid,))
    conn.commit()
    await api._refs_task(pid, None)
    p = conn.execute("SELECT refs_status, refs_error FROM projects WHERE id=?", (pid,)).fetchone()
    if p["refs_status"] != "ready":
        # 定妆照失败不硬停整条流水线：关键帧没有参考图仍能生成，只是跨集一致性下降
        _log(pid, f"定妆照未全部成功，继续（跨集一致性可能下降）：{p['refs_error']}")
    else:
        _log(pid, "定妆照完成")


async def _ensure_portraits(pid: str) -> None:
    """按 20 集分段刷新定妆照。只有一段（总集数 ≤ 间隔）时初始定妆照已覆盖，直接跳过。
    失败不阻断出片：缺分段定妆照仍可用初始定妆照生成视频，仅时间维一致性下降。"""
    conn = get_conn()
    last = conn.execute("SELECT MAX(episode_no) AS m FROM episodes WHERE project_id=?", (pid,)).fetchone()["m"] or 0
    if last <= config.PORTRAIT_REFRESH_INTERVAL:
        return
    _log(pid, f"按 {config.PORTRAIT_REFRESH_INTERVAL} 集分段刷新定妆照（判断角色外观是否大变）")
    conn.execute("UPDATE projects SET portraits_status='running', portraits_error=NULL WHERE id=?", (pid,))
    conn.commit()
    try:
        from app.portraits import update_portraits_for_blocks
        result = await update_portraits_for_blocks(pid)
        conn.execute("UPDATE projects SET portraits_status='ready', portraits_error=? WHERE id=?",
                     ("；".join(result.get("errors") or [])[:800] or None, pid))
        conn.commit()
        for ch in result.get("changes") or []:
            _log(pid, f"定妆照：{ch}")
        if not result.get("changes"):
            _log(pid, "定妆照：各角色外观无明显变化，沿用初始定妆照")
    except Exception as exc:  # noqa: BLE001 不阻断出片
        conn.execute("UPDATE projects SET portraits_status='failed', portraits_error=? WHERE id=?",
                     (str(exc)[:800], pid))
        conn.commit()
        _log(pid, f"定妆照按集刷新失败（不阻断出片，沿用初始定妆照）：{exc}")


async def _ensure_plan(pid: str) -> None:
    from app import api
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) c FROM episodes WHERE project_id=?", (pid,)).fetchone()["c"]
    if n > 0:
        _log(pid, f"已有 {n} 集，跳过分集")
        return
    _log(pid, "开始分集规划")
    conn.execute("UPDATE projects SET plan_status='running', plan_error=NULL WHERE id=?", (pid,))
    conn.commit()
    await api._plan_task(pid)
    p = conn.execute("SELECT plan_status, plan_error FROM projects WHERE id=?", (pid,)).fetchone()
    if p["plan_status"] != "ready":
        raise RuntimeError(f"分集失败：{p['plan_error']}")
    n = conn.execute("SELECT COUNT(*) c FROM episodes WHERE project_id=?", (pid,)).fetchone()["c"]
    _log(pid, f"分集完成：共 {n} 集")


async def _episode_pipeline(pid: str, eid: str, epno: int, sb_sem: asyncio.Semaphore) -> None:
    from app import api
    conn = get_conn()
    try:
        # 1) 剧本：分集之后先把小说改写成可拍剧本
        ep = conn.execute("SELECT status, screenplay_status, screenplay_json, screenplay_error FROM episodes WHERE id=?", (eid,)).fetchone()
        if not ep["screenplay_json"] or ep["screenplay_status"] in ("pending", "failed", "running"):
            async with sb_sem:
                _log(pid, f"第{epno}集：生成可拍剧本")
                conn.execute("UPDATE episodes SET screenplay_status='running', screenplay_error=NULL WHERE id=?", (eid,))
                conn.commit()
                await api._screenplay_task(eid)
            ep = conn.execute("SELECT screenplay_status, screenplay_error FROM episodes WHERE id=?", (eid,)).fetchone()
            if ep["screenplay_status"] != "ready":
                raise _Skip(f"第{epno}集剧本失败，跳过：{ep['screenplay_error']}")

        # 2) 分镜：仅对「待分镜/分镜中/分镜失败」的集生成
        ep = conn.execute("SELECT status FROM episodes WHERE id=?", (eid,)).fetchone()
        if ep["status"] in ("planned", "scripting", "script_failed"):
            async with sb_sem:
                _log(pid, f"第{epno}集：生成分镜")
                conn.execute("UPDATE episodes SET status='scripting', script_error=NULL WHERE id=?", (eid,))
                conn.commit()
                await api._storyboard_task(eid)
            ep = conn.execute("SELECT status, script_error FROM episodes WHERE id=?", (eid,)).fetchone()
            if ep["status"] not in ("scripted", "confirmed", "generating", "done"):
                raise _Skip(f"第{epno}集分镜失败，跳过：{ep['script_error']}")

        # 3) 确认（自动跳过人工门）：仅对「待确认」的集
        ep = conn.execute("SELECT status FROM episodes WHERE id=?", (eid,)).fetchone()
        if ep["status"] == "scripted":
            try:
                api.confirm_episode_core(eid)
                _log(pid, f"第{epno}集：分镜已自动确认")
            except ValueError as ve:
                raise _Skip(f"第{epno}集未通过确认校验，跳过（请到分镜台人工修订后重跑）：{str(ve)[:200]}")

        # 4) 视频（参考图模式，任务内生成参考图）→ 5) 配音(可选) → 6) 合成
        await _ensure_videos(pid, eid, epno)
        await _ensure_audio(pid, eid, epno)
        await _ensure_concat(pid, eid, epno)
        _log(pid, f"第{epno}集：成片完成 ✅")
    except _Skip as s:
        _log(pid, str(s))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 单集失败不拖垮其它集
        _log(pid, f"第{epno}集失败：{exc}")


def _shots_needing_video(conn, eid: str) -> list[dict]:
    shots = rows_to_dicts(conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (eid,)).fetchall())
    return [s for s in shots if not _video_ok(conn, s["adopted_version_id"])]


async def _ensure_videos(pid: str, eid: str, epno: int) -> None:
    conn = get_conn()
    shots = rows_to_dicts(conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (eid,)).fetchall())
    by_no = {s["shot_no"]: s for s in shots}
    todo = [s for s in shots if not _video_ok(conn, s["adopted_version_id"])]
    if not todo:
        _log(pid, f"第{epno}集：视频已就绪，跳过")
        return
    _log(pid, f"第{epno}集：生成视频（{len(todo)} 镜）")
    # 清空待生成镜的旧采用版，使新成功版被自动采用（沿用 generate_episode 的语义）
    sel_ids = [s["id"] for s in todo]
    conn.execute(
        f"UPDATE shots SET adopted_version_id=NULL WHERE id IN ({','.join('?' for _ in sel_ids)})", sel_ids)
    conn.commit()
    # 视频固定走参考图模式：入队前确保每镜都有固定参考图计划。
    from app.api import _ensure_shot_mode_plan
    for s in todo:
        await _ensure_shot_mode_plan(conn, s["id"])
    for s in todo:
        after = None
        if s["continuity_from_prev"] and s["shot_no"] > 1:
            pr = by_no.get(s["shot_no"] - 1)
            after = pr["id"] if pr else None
        try:
            r = worker.enqueue_shot(s["id"], after_shot_id=after)
            if r.get("reused") and r.get("version_id"):
                conn.execute("UPDATE shots SET adopted_version_id=? WHERE id=?", (r["version_id"], s["id"]))
        except ValueError as e:
            _log(pid, f"第{epno}集 镜{s['shot_no']} 视频入队失败：{e}")
    conn.commit()

    attempts: dict[str, int] = {}
    while True:
        pending = _shots_needing_video(conn, eid)
        if not pending:
            break
        paused = conn.execute(
            "SELECT COUNT(*) c FROM shot_versions v JOIN shots s ON s.id=v.shot_id "
            "WHERE s.episode_id=? AND v.status='paused_budget'", (eid,)).fetchone()["c"]
        if paused:
            raise RuntimeError(
                f"第{epno}集已达成本上限 ¥{get_setting('episode_cost_limit_cny')}，{paused} 个视频暂停。"
                "可在监制房调高「每集成本上限」后重新点击一键全自动（会从断点续做）")
        active = conn.execute(
            "SELECT COUNT(*) c FROM jobs WHERE episode_id=? AND kind='video' AND status IN ('queued','running')",
            (eid,)).fetchone()["c"]
        if active == 0:
            by_no = {s["shot_no"]: s for s in rows_to_dicts(conn.execute(
                "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (eid,)).fetchall())}
            progressed = False
            for s in pending:
                a = attempts.get(s["id"], 0)
                if a < _MAX_RETRY:
                    after = None
                    if s["continuity_from_prev"] and s["shot_no"] > 1:
                        pr = by_no.get(s["shot_no"] - 1)
                        after = pr["id"] if pr else None
                    try:
                        worker.enqueue_shot(s["id"], reroll=True, after_shot_id=after)
                        attempts[s["id"]] = a + 1
                        progressed = True
                    except ValueError as e:
                        _log(pid, f"第{epno}集 镜{s['shot_no']} 视频重试失败：{e}")
            if not progressed:
                raise RuntimeError(
                    f"第{epno}集视频失败镜：{[s['shot_no'] for s in pending]}（已达重试上限）")
        await asyncio.sleep(_POLL)
    _log(pid, f"第{epno}集：视频全部就绪")


_WIN_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_filename(name: str) -> str:
    """把书名清洗成合法文件名（去掉 Windows 非法字符与首尾点/空格）。"""
    cleaned = _WIN_INVALID.sub("_", (name or "").strip()).strip(" .")
    return cleaned or "未命名"


def _export_episode(pid: str, project_id: str, epno: int, final_path: Path) -> None:
    """把整集成品复制到用户指定目录，命名「书名第N集.mp4」；同名已存在则跳过。"""
    export_dir = _export_dir(project_id)
    if not export_dir:
        return
    conn = get_conn()
    row = conn.execute("SELECT name FROM projects WHERE id=?", (project_id,)).fetchone()
    book = _safe_filename(row["name"] if row else "未命名")
    dest = Path(export_dir) / f"{book}第{epno}集.mp4"
    if dest.exists():
        _log(pid, f"第{epno}集：导出目录已有同名文件 {dest.name}，跳过保存")
        return
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(final_path, dest)
        _log(pid, f"第{epno}集：已保存到 {dest}")
    except OSError as e:
        _log(pid, f"第{epno}集：导出失败（{e}）")


async def _ensure_audio(pid: str, eid: str, epno: int) -> None:
    """配音（TTS）+ ASR 校验。总开关关闭时整步跳过，保持无声链路。"""
    from app import audio as audio_mod
    if not audio_mod.is_enabled():
        return
    _log(pid, f"第{epno}集：生成配音并 ASR 校验")
    summary = await audio_mod.generate_episode_audio(eid)
    ok, total, failed = summary.get("ok", 0), summary.get("total", 0), summary.get("failed", 0)
    if failed:
        _log(pid, f"第{epno}集：配音 {ok}/{total} 通过，{failed} 镜 ASR 预检未过"
                  "（已混入最后一版，可在监制房补正音词库后重生该集）")
    else:
        _log(pid, f"第{epno}集：配音完成（{ok}/{total} 通过）")


async def _ensure_concat(pid: str, eid: str, epno: int) -> None:
    conn = get_conn()
    ep = conn.execute("SELECT project_id, episode_no, status FROM episodes WHERE id=?", (eid,)).fetchone()
    final = (config.PROJECTS_DIR / ep["project_id"] / "episodes" / str(ep["episode_no"])
             / "final" / "episode.mp4")
    if not final.exists():
        _log(pid, f"第{epno}集：合成成片")
        # ffmpeg 是阻塞调用，放到线程里跑，避免冻结事件循环（其余集流水线同时在跑）
        res = await asyncio.to_thread(worker.concatenate_episode, eid)
        conn.execute("UPDATE episodes SET status='done' WHERE id=?", (eid,))
        conn.commit()
        if res.get("ffmpeg_missing"):
            # 缺 ffmpeg 时没有可导出的整集文件，仅在成片台留首片段直链
            _log(pid, f"第{epno}集：缺 ffmpeg，无法合成整集文件（装好 ffmpeg 后到成片台重新合成）；跳过导出")
            return
        _log(pid, f"第{epno}集：合成完成（{res.get('shots')} 镜 / {res.get('total_duration_s')}s）")
    else:
        if ep["status"] != "done":
            conn.execute("UPDATE episodes SET status='done' WHERE id=?", (eid,))
            conn.commit()
        _log(pid, f"第{epno}集：已有成片，跳过合成")
    # 不论本次是否新合成，只要整集文件存在就导出（已存在同名则跳过）
    _export_episode(pid, ep["project_id"], epno, final)
