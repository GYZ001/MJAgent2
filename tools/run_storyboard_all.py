from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api import _storyboard_task
from app.db import get_conn, init_db


async def main(project_id: str, episode_nos: set[int] | None = None) -> None:
    init_db()
    conn = get_conn()
    if episode_nos:
        placeholders = ",".join("?" for _ in episode_nos)
        rows = conn.execute(
            f"SELECT id, episode_no, title FROM episodes WHERE project_id=? AND episode_no IN ({placeholders}) ORDER BY episode_no",
            (project_id, *sorted(episode_nos)),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, episode_no, title FROM episodes WHERE project_id=? ORDER BY episode_no",
            (project_id,),
        ).fetchall()
    if not rows:
        raise SystemExit(f"project has no episodes: {project_id}")
    ids = [r["id"] for r in rows]
    placeholders = ",".join("?" for _ in ids)
    conn.execute(
        f"UPDATE episodes SET status='scripting', script_error=NULL WHERE id IN ({placeholders})",
        ids,
    )
    conn.commit()
    print(json.dumps({
        "event": "started",
        "project_id": project_id,
        "episodes": [{"id": r["id"], "episode_no": r["episode_no"], "title": r["title"]} for r in rows],
        "concurrency": len(ids),
    }, ensure_ascii=False), flush=True)

    started = time.time()
    tasks = [asyncio.create_task(_storyboard_task(eid)) for eid in ids]
    await asyncio.gather(*tasks)

    final = conn.execute(
        "SELECT episode_no, title, status, script_error FROM episodes WHERE project_id=? ORDER BY episode_no",
        (project_id,),
    ).fetchall()
    print(json.dumps({
        "event": "finished",
        "elapsed_s": round(time.time() - started, 1),
        "episodes": [dict(r) for r in final],
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: python tools/run_storyboard_all.py <project_id> [episode_no ...]")
    selected = {int(x) for x in sys.argv[2:]} or None
    asyncio.run(main(sys.argv[1], selected))
