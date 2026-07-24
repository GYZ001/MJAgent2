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
from pathlib import Path

from app import config, errors, planning, task_registry, worker
from app.atomic_io import atomic_copy
from app.db import get_conn, get_setting, now, rows_to_dicts, set_setting
from app.evidence import repository as evidence_repository
from app.harness.types import Evaluation, EvidenceArtifact
from app.orchestration.engine import WorkflowRecorder, fingerprint

# 轮询 DB 等待队列阶段完成的间隔（秒）
_POLL = 5.0
# 单个镜头视频在「无在跑任务且未完成」时的最大重试次数（应对偶发网关失败）
_MAX_RETRY = 2

# ---------- 状态与日志（供前端轮询展示） ----------

def _latest_auto_run(pid: str):
    return get_conn().execute(
        "SELECT * FROM workflow_runs WHERE workflow_type='auto_project' AND scope_type='project' "
        "AND scope_id=? ORDER BY updated_at DESC LIMIT 1",
        (pid,),
    ).fetchone()


def is_running(pid: str) -> bool:
    return task_registry.active("auto", pid)


def _log(pid: str, msg: str) -> None:
    run = _latest_auto_run(pid)
    if run:
        evidence_repository.append_event(run["id"], "AUTO_LOG", "info", msg)


def _phase(pid: str, phase: str) -> None:
    run = _latest_auto_run(pid)
    if not run:
        return
    conn = get_conn()
    conn.execute(
        "UPDATE workflow_runs SET current_step_key=?, updated_at=? WHERE id=?",
        (phase, now(), run["id"]),
    )
    conn.commit()
    evidence_repository.append_event(run["id"], "AUTO_PHASE", "info", phase)


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
    vid = sum(1 for s in shots if _video_ok(conn, s["adopted_version_id"]))
    return {
        "bible": p["bible_status"], "refs": p["refs_status"], "plan": p["plan_status"],
        "episodes_total": len(eps), "episodes_done": sum(1 for e in eps if e["status"] == "done"),
        "screenplays_ready": sum(1 for e in eps if e["screenplay_status"] == "ready" and e["screenplay_json"]),
        "shots_total": len(shots), "shots_video": vid,
    }


def _export_dir(pid: str) -> str:
    return (get_setting(f"export_dir:{pid}") or "").strip()


def status(pid: str) -> dict:
    run = _latest_auto_run(pid)
    events = evidence_repository.get_events(run["id"], limit=120) if run else []
    return {
        "running": is_running(pid),
        "phase": run["current_step_key"] if run else None,
        "error": run["failure_message"] if run else None,
        "log": [
            {"t": event["ts"], "msg": event["message"]}
            for event in events if event["event_type"] in {"AUTO_LOG", "AUTO_PHASE"}
        ][-40:],
        "started_at": run["started_at"] if run else None,
        "updated_at": run["updated_at"] if run else None,
        "run_id": run["id"] if run else None,
        "export_dir": _export_dir(pid),
        "progress": _progress(pid),
    }


# ---------- 启动 / 取消 ----------

def _input_fingerprint(pid: str) -> str:
    conn = get_conn()
    project = conn.execute(
        "SELECT id, bible_version, plan_status FROM projects WHERE id=?", (pid,)
    ).fetchone()
    chapters = rows_to_dicts(conn.execute(
        "SELECT idx, title, content FROM chapters WHERE project_id=? ORDER BY idx", (pid,)
    ).fetchall())
    return fingerprint(dict(project) if project else {"id": pid}, chapters)


def start(
    pid: str,
    export_dir: str | None = None,
    *,
    requested_by: str = "user",
    trigger_type: str = "manual",
    parent_run_id: str | None = None,
) -> str:
    if export_dir is not None:
        # 记住导出目录，供本次运行与下次预填使用（空串=清除）
        set_setting(f"export_dir:{pid}", export_dir.strip())
    recorder = WorkflowRecorder.create(
        workflow_type="auto_project",
        scope_type="project",
        scope_id=pid,
        input_fingerprint=_input_fingerprint(pid),
        requested_by=requested_by,
        trigger_type=trigger_type,
        policy_snapshot={
            "auto_concurrency": get_setting("auto_concurrency"),
            "auto_storyboard_concurrency": get_setting("auto_storyboard_concurrency"),
            "episode_cost_limit_cny": get_setting("episode_cost_limit_cny"),
            "video_retry_limit": _MAX_RETRY,
        },
        config_snapshot={
            "shot_duration_range_s": [config.VIDEO_DURATION_MIN_S, config.VIDEO_DURATION_MAX_S],
            "shot_duration_decided_by": "model",
        },
        # episode_cost_limit_cny is a per-episode guard, not a project-run budget.
        # Keep it in the policy snapshot and do not mislabel it as the run hard limit.
        budget_limit_cny=None,
        parent_run_id=parent_run_id,
    )
    task_registry.spawn("auto", pid, _run(pid, recorder), project_id=pid)
    return recorder.run_id


def recover_auto_tasks() -> int:
    """Resume the latest interrupted project DAG before child-stage recovery runs.

    The pipeline is intentionally adaptive: every stage checks committed business
    output before doing work, so a new attempt can safely continue the same logical
    project without replaying completed paid media jobs.
    """
    conn = get_conn()
    rows = conn.execute(
        """SELECT wr.id, wr.scope_id
           FROM workflow_runs wr
           WHERE wr.workflow_type='auto_project'
             AND wr.scope_type='project'
             AND wr.status='PAUSED_EXTERNAL'
             AND wr.failure_code='SERVICE_RESTART'
             AND wr.recovered_by_run_id IS NULL
             AND NOT EXISTS (
                 SELECT 1 FROM workflow_runs newer
                 WHERE newer.workflow_type='auto_project'
                   AND newer.scope_type='project'
                   AND newer.scope_id=wr.scope_id
                   AND newer.updated_at>wr.updated_at
                   AND newer.status IN ('CREATED','RUNNING','WAITING_RETRY','WAITING_HUMAN',
                                        'PAUSED_BUDGET','PAUSED_EXTERNAL')
             )
           ORDER BY wr.updated_at"""
    ).fetchall()
    resumed = 0
    for row in rows:
        project_id = row["scope_id"]
        if is_running(project_id):
            continue
        start(
            project_id,
            requested_by="system",
            trigger_type="resume",
            parent_run_id=row["id"],
        )
        resumed += 1
    return resumed


async def cancel(pid: str) -> bool:
    if await task_registry.cancel_and_wait("auto", pid):
        return True
    return False


# ---------- 主流程 ----------

async def _run(pid: str, recorder: WorkflowRecorder) -> None:
    try:
        recorder.start()
        from app.media_pipeline.concurrency import channel_limit, reload_limits_from_settings
        from app.media_pipeline import stages as media_stages
        reload_limits_from_settings()
        # 全自动按上游在途上限扩 worker，不再用模糊的 auto_concurrency=24 假高并发
        worker.ensure_workers(channel_limit(media_stages.RESOURCE_VIDEO_INFLIGHT))
        export_dir = _export_dir(pid)
        if export_dir:
            try:
                Path(export_dir).mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise RuntimeError(f"导出目录不可用：{export_dir}（{e}）")
            _log(pid, f"成片将自动导出到：{export_dir}")
        else:
            _log(pid, "未设置导出目录：只在成片台生成整集成品，不另存到外部文件夹")
        await recorder.step(
            "character_bible", lambda: _ensure_bible(pid), contract_key="character_bible",
            agent_name="character_bible",
        )
        project = get_conn().execute(
            "SELECT bible_json, bible_version, bible_artifact_id FROM projects WHERE id=?", (pid,)
        ).fetchone()
        bible_artifact = (
            evidence_repository.get_artifact(project["bible_artifact_id"])
            if project and project["bible_artifact_id"] else None
        )
        # 定妆照与分集互不依赖（都只需人物谱），并行推进
        (_, _), (plan_step_id, _) = await asyncio.gather(
            recorder.step("character_references", lambda: _ensure_refs(pid), agent_name="reference_assets"),
            recorder.step(
                "episode_mapping", lambda: _ensure_plan(pid), contract_key="episode_mapping",
                agent_name="deterministic_regex",
            ),
        )
        episodes_for_artifact = rows_to_dicts(get_conn().execute(
            "SELECT id, episode_no, title, source_chapters FROM episodes WHERE project_id=? ORDER BY episode_no",
            (pid,),
        ).fetchall())
        mapping_content = [
            {**episode, "source_chapters": json.loads(episode["source_chapters"] or "[]")}
            for episode in episodes_for_artifact
        ]
        mapping_artifact = recorder.artifact(
            plan_step_id,
            EvidenceArtifact(
                type="episode_mapping", scope_type="project", scope_id=pid,
                status="validated", trust_level="T2", content=mapping_content,
                contract_version="1.0.0",
                parent_artifact_ids=[bible_artifact["id"]] if bible_artifact else [],
            ),
        )
        evidence_repository.create_evaluation(
            mapping_artifact["id"],
            Evaluation(
                evaluator_type="deterministic", evaluator_name="one_chapter_one_episode",
                evaluator_version="1.0.0", status="passed", hard_gate_passed=True,
                score=100, evidence={"episode_count": len(mapping_content), "model_calls": 0},
            ),
            step_run_id=plan_step_id,
        )
        # 注：已有角色的外观漂移已改为分镜阶段按集反应式重绘（见 portraits.ensure_cards_for_screenplay），
        # 不再在这里做"每 20 集全量轮询"。

        conn = get_conn()
        eps = rows_to_dicts(conn.execute(
            "SELECT id, episode_no FROM episodes WHERE project_id=? ORDER BY episode_no", (pid,)).fetchall())
        if not eps:
            raise RuntimeError("分集后没有任何剧集")
        _phase(pid, f"逐集成片（共 {len(eps)} 集，并行）")
        sb_sem = asyncio.Semaphore(max(int(get_setting("auto_storyboard_concurrency") or 8), 1))
        await asyncio.gather(*[
            recorder.step(
                f"episode:{e['episode_no']}:pipeline",
                lambda e=e: _episode_pipeline(pid, e["id"], e["episode_no"], sb_sem),
                agent_name="episode_pipeline",
                input_artifact_ids=[
                    mapping_artifact["id"],
                    *([bible_artifact["id"]] if bible_artifact else []),
                ],
                context_manifest={"episode_id": e["id"], "episode_no": e["episode_no"]},
            )
            for e in eps
        ])

        prog = _progress(pid)
        done, total = prog.get("episodes_done", 0), prog.get("episodes_total", 0)
        if done >= total:
            _phase(pid, "全部完成 ✅")
            _log(pid, f"全自动成片完成：{total} 集已出片")
            recorder.succeed(f"全自动成片完成：{total} 集已出片")
        else:
            _phase(pid, f"完成（{done}/{total} 集出片，其余见日志）")
            _log(pid, f"部分集需人工处理：已出片 {done}/{total}，未完成的集请查看上方日志/各工作台")
            recorder.partial(f"部分完成：{done}/{total} 集已出片")
    except asyncio.CancelledError:
        _phase(pid, "已取消")
        _log(pid, "已取消（已入队的关键帧/视频会继续跑完，可稍后重新点击从断点续做）")
        recorder.cancel()
        raise
    except Exception as exc:  # noqa: BLE001 失败要响
        rec = errors.log_error(exc, action="auto_pipeline", context={"project_id": pid})
        # RuntimeError 由流水线主动抛出、消息已是安全中文（且内嵌下游错误码）；其它异常按技术类脱敏。
        public = (str(exc)[:760] + f"（{rec.error_id}）") if isinstance(exc, RuntimeError) else rec.public
        _phase(pid, "中断")
        _log(pid, f"流水线中断：{public}")
        recorder.fail(exc)


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
        task_registry.register("bible", pid, cur, project_id=pid)
    try:
        await api._bible_task(pid, trigger_full_refs=False)
    finally:
        if cur is not None:
            task_registry.unregister("bible", pid, task=cur)
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
    recovering = p["refs_status"] == "running"
    parent = None
    if recovering:
        parent = conn.execute(
            "SELECT id FROM workflow_runs WHERE workflow_type='character_references' "
            "AND scope_type='project' AND scope_id=? AND status='PAUSED_EXTERNAL' "
            "AND recovered_by_run_id IS NULL ORDER BY updated_at DESC LIMIT 1",
            (pid,),
        ).fetchone()
    _log(pid, "开始生成定妆照")
    conn.execute("UPDATE projects SET refs_status='running', refs_error=NULL, refs_target=NULL WHERE id=?", (pid,))
    conn.commit()
    await api._refs_task(
        pid,
        None,
        resume=recovering,
        parent_run_id=parent["id"] if parent else None,
        requested_by="system" if recovering else "user",
        trigger_type="resume" if recovering else "manual",
    )
    p = conn.execute("SELECT refs_status, refs_error FROM projects WHERE id=?", (pid,)).fetchone()
    if p["refs_status"] != "ready":
        # 定妆照失败不硬停整条流水线：关键帧没有参考图仍能生成，只是跨集一致性下降
        _log(pid, f"定妆照未全部成功，继续（跨集一致性可能下降）：{p['refs_error']}")
    else:
        _log(pid, "定妆照完成")


async def _ensure_plan(pid: str) -> None:
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) c FROM episodes WHERE project_id=?", (pid,)).fetchone()["c"]
    if n > 0:
        _log(pid, f"已有 {n} 集，跳过分集")
        return
    _log(pid, "开始分集规划")
    conn.execute("UPDATE projects SET plan_status='running', plan_error=NULL WHERE id=?", (pid,))
    conn.commit()
    await planning.run_regex_plan(pid)
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
        if not ep["screenplay_json"] or ep["screenplay_status"] in ("pending", "failed", "warning", "running"):
            async with sb_sem:
                _log(pid, f"第{epno}集：生成可拍剧本")
                started_at = now()
                conn.execute(
                    "UPDATE episodes SET screenplay_status='running', screenplay_error=NULL, screenplay_started_at=?, screenplay_updated_at=? WHERE id=?",
                    (started_at, started_at, eid))
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

        # 4) 视频（参考图模式，任务内生成参考图）→ 5) 合成
        await _ensure_videos(pid, eid, epno)
        await _ensure_concat(pid, eid, epno)
        _log(pid, f"第{epno}集：成片完成 ✅")
    except _Skip as s:
        _log(pid, str(s))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 单集失败不拖垮其它集
        rec = errors.log_error(exc, action="auto_episode_pipeline",
                               context={"project_id": pid, "episode_id": eid, "episode_no": epno})
        detail = f"{str(exc)[:400]}（{rec.error_id}）" if isinstance(exc, RuntimeError) else rec.public
        _log(pid, f"第{epno}集失败：{detail}")


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
    # 不预先清空 adopted_version_id：失败时保留原采用；成功后由 select_best 比较切换。
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
                row = conn.execute(
                    "SELECT adopted_version_id FROM shots WHERE id=?", (s["id"],)
                ).fetchone()
                if not row or not row["adopted_version_id"]:
                    conn.execute(
                        "UPDATE shots SET adopted_version_id=? WHERE id=?",
                        (r["version_id"], s["id"]),
                    )
        except ValueError as e:
            _log(pid, f"第{epno}集 镜{s['shot_no']} 视频入队失败：{e}")
    conn.commit()

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
        # 媒体流水线负责重试/重抽；编排层只等待，不再第二套 enqueue
        active = conn.execute(
            "SELECT COUNT(*) c FROM jobs WHERE episode_id=? AND kind='video' "
            "AND status IN ('queued','running','waiting_provider','waiting_retry') "
            "AND cancellation_requested=0 AND abandoned=0",
            (eid,)).fetchone()["c"]
        waiting_human = conn.execute(
            "SELECT COUNT(*) c FROM jobs WHERE episode_id=? AND kind='video' "
            "AND status='waiting_human' AND cancellation_requested=0 AND abandoned=0",
            (eid,),
        ).fetchone()["c"]
        if waiting_human:
            raise RuntimeError(
                f"第{epno}集：{waiting_human} 镜待人工处理，自动流水线已停止。"
                "请到评审墙处理后再重新点击一键全自动（会从断点续做）")
        if active == 0:
            # 无活跃任务且仍有未采用镜头：可能全部失败。不再自动 reroll，
            # 避免与 worker QA 自动重抽叠加重复付费。
            failed_nos = [s["shot_no"] for s in pending]
            raise RuntimeError(
                f"第{epno}集视频未完成镜：{failed_nos}（已无活跃任务，请到评审墙查看失败/待人工）")
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
        atomic_copy(final_path, dest)
        _log(pid, f"第{epno}集：已保存到 {dest}")
    except OSError as e:
        _log(pid, f"第{epno}集：导出失败（{e}）")


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
