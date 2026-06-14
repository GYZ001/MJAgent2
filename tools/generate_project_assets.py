from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import worker
from app.db import get_conn, init_db, rows_to_dicts

LOG_PATH: Path | None = None


def _log(event: str, **payload) -> None:
    line = json.dumps({"ts": round(time.time(), 1), "event": event, **payload}, ensure_ascii=False)
    print(line, flush=True)
    if LOG_PATH:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def _project_shots(project_id: str) -> list[dict]:
    conn = get_conn()
    return rows_to_dicts(conn.execute(
        """SELECT s.*, e.episode_no
           FROM shots s
           JOIN episodes e ON e.id=s.episode_id
           WHERE e.project_id=?
           ORDER BY e.episode_no, s.shot_no""",
        (project_id,),
    ).fetchall())


def _scene_summary(project_id: str) -> dict:
    conn = get_conn()
    rows = _project_shots(project_id)
    ready = sum(1 for r in rows if worker.shot_keyframes_ready(r))
    approved = sum(1 for r in rows if r["scene_status"] == "approved" and worker.shot_keyframes_ready(r))
    by_status = {
        r["status"]: r["c"]
        for r in conn.execute(
            """SELECT j.status, COUNT(*) c
               FROM jobs j
               JOIN shots s ON s.id=j.shot_id
               JOIN episodes e ON e.id=s.episode_id
               WHERE e.project_id=? AND j.kind='scene'
               GROUP BY j.status""",
            (project_id,),
        ).fetchall()
    }
    return {"shots": len(rows), "keyframes_ready": ready, "scene_approved": approved, "jobs": by_status}


def _video_summary(project_id: str) -> dict:
    conn = get_conn()
    rows = conn.execute(
        """SELECT e.episode_no, COUNT(s.id) shots,
                  SUM(CASE WHEN s.adopted_version_id IS NOT NULL THEN 1 ELSE 0 END) adopted
           FROM episodes e
           LEFT JOIN shots s ON s.episode_id=e.id
           WHERE e.project_id=?
           GROUP BY e.id
           ORDER BY e.episode_no""",
        (project_id,),
    ).fetchall()
    by_status = {
        r["status"]: r["c"]
        for r in conn.execute(
            """SELECT j.status, COUNT(*) c
               FROM jobs j
               JOIN shots s ON s.id=j.shot_id
               JOIN episodes e ON e.id=s.episode_id
               WHERE e.project_id=? AND j.kind='video'
               GROUP BY j.status""",
            (project_id,),
        ).fetchall()
    }
    return {
        "episodes": [{"episode_no": r["episode_no"], "shots": r["shots"], "adopted": r["adopted"] or 0} for r in rows],
        "jobs": by_status,
    }


async def _run_workers(concurrency: int) -> None:
    worker.recover_and_start(loop_concurrency=concurrency)
    await worker._queue.join()
    await worker.stop()


async def generate_scenes(project_id: str, concurrency: int) -> None:
    init_db()
    conn = get_conn()
    shots = _project_shots(project_id)
    started = 0
    for shot in shots:
        if shot["scene_status"] == "approved" and worker.shot_keyframes_ready(shot):
            continue
        try:
            worker.enqueue_scene(shot["id"])
            started += 1
        except ValueError as exc:
            _log("scene_enqueue_error", episode_no=shot["episode_no"], shot_no=shot["shot_no"], error=str(exc))
    _log("scenes_enqueued", started=started, summary=_scene_summary(project_id))
    if started:
        await _run_workers(concurrency)
    _log("scenes_finished", summary=_scene_summary(project_id))
    failed = rows_to_dicts(conn.execute(
        """SELECT e.episode_no, s.shot_no, j.status, j.error
           FROM jobs j
           JOIN shots s ON s.id=j.shot_id
           JOIN episodes e ON e.id=s.episode_id
           WHERE e.project_id=? AND j.kind='scene' AND j.status='failed'
           ORDER BY e.episode_no, s.shot_no""",
        (project_id,),
    ).fetchall())
    if failed:
        _log("scene_failed_jobs", failed=failed[-20:])


async def drain(project_id: str, concurrency: int) -> None:
    init_db()
    _log("drain_started", concurrency=concurrency, scene_summary=_scene_summary(project_id), video_summary=_video_summary(project_id))
    await _run_workers(concurrency)
    _log("drain_finished", scene_summary=_scene_summary(project_id), video_summary=_video_summary(project_id))


async def generate_videos(project_id: str, concurrency: int) -> None:
    init_db()
    from app.api import generate_episode

    conn = get_conn()
    not_ready = [
        {"episode_no": s["episode_no"], "shot_no": s["shot_no"], "scene_status": s["scene_status"]}
        for s in _project_shots(project_id)
        if s["scene_status"] != "approved" or not worker.shot_keyframes_ready(s)
    ]
    if not_ready:
        _log("videos_blocked_keyframes_not_approved", count=len(not_ready), examples=not_ready[:20])
        return
    episodes = conn.execute(
        "SELECT id, episode_no FROM episodes WHERE project_id=? ORDER BY episode_no",
        (project_id,),
    ).fetchall()
    started = 0
    for ep in episodes:
        result = generate_episode(ep["id"], {})
        enqueued = result.get("enqueued") or []
        started += sum(1 for item in enqueued if item.get("job_id"))
        errors = [item for item in enqueued if item.get("error")]
        if errors:
            _log("video_enqueue_errors", episode_no=ep["episode_no"], errors=errors)
    _log("videos_enqueued", started=started, summary=_video_summary(project_id))
    if started:
        await _run_workers(concurrency)
    _log("videos_finished", summary=_video_summary(project_id))
    failed = rows_to_dicts(conn.execute(
        """SELECT e.episode_no, s.shot_no, j.status, j.error
           FROM jobs j
           JOIN shots s ON s.id=j.shot_id
           JOIN episodes e ON e.id=s.episode_id
           WHERE e.project_id=? AND j.kind='video' AND j.status='failed'
           ORDER BY e.episode_no, s.shot_no""",
        (project_id,),
    ).fetchall())
    if failed:
        _log("video_failed_jobs", failed=failed[-20:])


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_id")
    parser.add_argument("--phase", choices=("scenes", "videos", "all", "drain"), default="all")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--log", default="")
    args = parser.parse_args()
    global LOG_PATH
    LOG_PATH = Path(args.log) if args.log else None
    if LOG_PATH and LOG_PATH.exists():
        LOG_PATH.unlink()
    if args.phase == "drain":
        await drain(args.project_id, args.concurrency)
        return
    if args.phase in ("scenes", "all"):
        await generate_scenes(args.project_id, args.concurrency)
    if args.phase in ("videos", "all"):
        await generate_videos(args.project_id, args.concurrency)


if __name__ == "__main__":
    asyncio.run(main())
