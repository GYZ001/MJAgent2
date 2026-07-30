"""《陨落的天才》第 1 集 Supervisor golden 驱动。

用法：
  set MANJU_GOLDEN_LIVE=1
  .\\.venv\\Scripts\\python.exe scripts/run_golden_storyboard.py [--episode-id ep_…] [--timeout 7200]

流程：启动 Supervisor → 整集门禁通过 → 显式人工确认 → score_renderability → 写入 golden/runs/。
不触发付费视频生成。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASELINE_PATH = ROOT / "golden" / "renderability" / "doupocangqiong_ep1_baseline.json"
DEFAULT_EPISODE_ID = "ep_23517af4b5a8"
DEFAULT_TITLE = "陨落的天才"


def resolve_episode(
    *,
    episode_id: str | None,
    title_hint: str = DEFAULT_TITLE,
    episode_no: int = 1,
) -> dict[str, Any]:
    from app.db import get_conn, init_db

    init_db()
    conn = get_conn()
    if episode_id:
        row = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
        if not row:
            raise SystemExit(f"episode not found: {episode_id}")
        return dict(row)
    rows = conn.execute(
        "SELECT * FROM episodes WHERE episode_no=? AND title LIKE ? ORDER BY created_at DESC",
        (episode_no, f"%{title_hint}%"),
    ).fetchall()
    if not rows:
        # 回退默认 id
        row = conn.execute(
            "SELECT * FROM episodes WHERE id=?", (DEFAULT_EPISODE_ID,)
        ).fetchone()
        if row:
            return dict(row)
        raise SystemExit(f"未找到 title≈{title_hint!r} episode_no={episode_no} 的剧集")
    return dict(rows[0])


def write_run_report(
    *,
    episode_id: str,
    title: str,
    score: dict[str, Any],
    supervisor: dict[str, Any] | None,
    extra: dict[str, Any] | None = None,
    out_dir: Path | None = None,
) -> Path:
    out = out_dir or (ROOT / "golden" / "runs")
    out.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = out / f"{day}_yunluo_ep1_{episode_id[-8:]}.json"
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "label": "yunluo_tiancai_ep1_supervisor",
        "episode_id": episode_id,
        "title": title,
        "flow": "剧本定稿 → 分镜生成/修复 → 整集门禁 → 人工确认",
        "supervisor": supervisor,
        "score": score,
        **(extra or {}),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _count_video_jobs(episode_id: str) -> int:
    from app.db import get_conn

    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE episode_id=? AND kind LIKE '%video%'",
            (episode_id,),
        ).fetchone()
        return int(row["c"]) if row else 0
    except Exception:  # noqa: BLE001
        return 0


async def _start_and_wait(
    episode_id: str,
    *,
    poll_timeout_s: float,
    poll_interval_s: float = 5.0,
) -> dict[str, Any]:
    from app.domain import storyboard_ops
    from app.db import get_conn
    from app.storyboard_supervisor import load_latest_checkpoint

    # 若正在跑，先不重复启动
    ep0 = get_conn().execute("SELECT status FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if ep0 and ep0["status"] != "scripting":
        preview = storyboard_ops.storyboard_start_preflight(episode_id, {})
        started = await storyboard_ops.start_storyboard(episode_id, {
            "preflight_token": preview["preview_token"],
        })
    else:
        started = {"status": "already_running", "run_id": None}

    deadline = time.time() + poll_timeout_s
    last: dict[str, Any] = {}
    while time.time() < deadline:
        ep = get_conn().execute(
            "SELECT id, status, script_error, storyboard_artifact_id, "
            "active_storyboard_run_id FROM episodes WHERE id=?",
            (episode_id,),
        ).fetchone()
        cp = load_latest_checkpoint(episode_id)
        last = {
            "status": ep["status"] if ep else None,
            "script_error": ep["script_error"] if ep else None,
            "phase": cp.phase if cp else None,
            "outcome": cp.outcome if cp else None,
            "repair_epoch": cp.repair_epoch if cp else 0,
            "validated_prefix_end": cp.validated_prefix_end if cp else 0,
            "started": started,
        }
        if ep and ep["status"] == "confirmed":
            last["confirmed"] = True
            return last
        if ep and ep["status"] == "scripted" and cp and cp.outcome == "SUCCEEDED_READY_FOR_CONFIRM":
            from app.domain.video_ops import create_storyboard_confirmation_preview, confirm_episode_core

            confirm_preview = create_storyboard_confirmation_preview(episode_id)
            confirm_episode_core(
                episode_id,
                decided_by="golden_human_reviewer",
                reason="golden 人工确认步骤",
                preview_token=confirm_preview["preview_token"],
            )
            continue
        if ep and ep["status"] not in {"scripting"} and cp and cp.phase in {
            "WAITING_HUMAN", "WAITING_AUTHORIZATION", "CANCELLED", "SUCCEEDED",
        }:
            last["confirmed"] = ep["status"] == "confirmed"
            if cp.phase == "SUCCEEDED" or last["confirmed"]:
                return last
            if cp.phase in {"WAITING_HUMAN", "WAITING_AUTHORIZATION", "CANCELLED"}:
                return last
        await asyncio.sleep(poll_interval_s)
    last["timed_out"] = True
    return last


def run_golden(
    *,
    episode_id: str | None = None,
    title_hint: str = DEFAULT_TITLE,
    episode_no: int = 1,
    poll_timeout_s: float = 7200.0,
    write_report: bool = True,
) -> dict[str, Any]:
    from app.renderability_score import score_renderability_sample
    from scripts.score_renderability import _from_episode, _load_json

    ep = resolve_episode(
        episode_id=episode_id, title_hint=title_hint, episode_no=episode_no
    )
    eid = ep["id"]
    if ep.get("screenplay_status") != "ready" and not ep.get("screenplay_json"):
        raise SystemExit(f"episode {eid} 尚无 ready 剧本，无法跑 golden")

    videos_before = _count_video_jobs(eid)
    result = asyncio.run(
        _start_and_wait(eid, poll_timeout_s=poll_timeout_s)
    )
    videos_after = _count_video_jobs(eid)
    video_started = max(0, videos_after - videos_before)

    screenplay, storyboard = _from_episode(eid)
    baseline = _load_json(BASELINE_PATH) if BASELINE_PATH.exists() else None
    score = score_renderability_sample(
        screenplay=screenplay, storyboard=storyboard, baseline=baseline
    )

    confirmed = bool(result.get("confirmed")) or result.get("status") == "confirmed"
    ok = (
        confirmed
        and video_started == 0
        and not result.get("timed_out")
        and result.get("phase") not in {"WAITING_HUMAN", "WAITING_AUTHORIZATION", "CANCELLED"}
    )
    report_path = None
    if write_report:
        report_path = write_run_report(
            episode_id=eid,
            title=ep.get("title") or title_hint,
            score=score,
            supervisor={
                "phase": result.get("phase"),
                "outcome": result.get("outcome"),
                "repair_epoch": result.get("repair_epoch"),
                "validated_prefix_end": result.get("validated_prefix_end"),
            },
            extra={
                "ok": ok,
                "confirmed": confirmed,
                "video_jobs_started": video_started,
                "poll": {k: v for k, v in result.items() if k != "started"},
                "started": result.get("started"),
            },
        )

    return {
        "ok": ok,
        "confirmed": confirmed,
        "video_jobs_started": video_started,
        "episode_id": eid,
        "title": ep.get("title"),
        "phase": result.get("phase"),
        "outcome": result.get("outcome"),
        "score": score,
        "report_path": str(report_path) if report_path else None,
        "poll": result,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Golden Supervisor E2E（《陨落的天才》第1集）")
    parser.add_argument("--episode-id", default=os.environ.get("MANJU_GOLDEN_EPISODE_ID") or None)
    parser.add_argument("--title", default=DEFAULT_TITLE)
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("MANJU_GOLDEN_TIMEOUT_S", "7200")))
    parser.add_argument("--dry-resolve", action="store_true", help="只解析 episode，不启动")
    args = parser.parse_args()

    if args.dry_resolve:
        ep = resolve_episode(episode_id=args.episode_id, title_hint=args.title, episode_no=1)
        print(json.dumps({"id": ep["id"], "title": ep["title"], "status": ep["status"]}, ensure_ascii=False, indent=2))
        return

    if os.environ.get("MANJU_GOLDEN_LIVE", "").strip() not in {"1", "true", "yes"}:
        print("提示：未设置 MANJU_GOLDEN_LIVE=1；仍将执行（CLI 显式调用视为同意）。", file=sys.stderr)

    report = run_golden(
        episode_id=args.episode_id,
        title_hint=args.title,
        poll_timeout_s=args.timeout,
        write_report=True,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
