"""Renderability 金样对照 CLI。

用法：
  python scripts/score_renderability.py --screenplay path.json --storyboard path.json
  python scripts/score_renderability.py --episode-id <id>   # 从本地 DB 读

输出对照 PRD §4.2 与 golden/renderability/doupocangqiong_ep1_baseline.json。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.renderability_score import score_renderability_sample  # noqa: E402

BASELINE_PATH = ROOT / "golden" / "renderability" / "doupocangqiong_ep1_baseline.json"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _from_episode(episode_id: str) -> tuple[dict | None, dict | None]:
    from app.db import get_conn

    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        raise SystemExit(f"episode not found: {episode_id}")
    screenplay = json.loads(ep["screenplay_json"]) if ep["screenplay_json"] else None
    rows = conn.execute(
        "SELECT shot_no, duration_s, action_desc, first_frame_desc, last_frame_desc, "
        "shot_contract_json FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall()
    shots = []
    for r in rows:
        item = {
            "shot_no": r["shot_no"],
            "duration_s": r["duration_s"],
            "action_desc": r["action_desc"],
            "first_frame_desc": r["first_frame_desc"],
            "last_frame_desc": r["last_frame_desc"],
        }
        if r["shot_contract_json"]:
            try:
                item.update(json.loads(r["shot_contract_json"]))
            except json.JSONDecodeError:
                pass
        shots.append(item)
    storyboard = {"episode_no": ep["episode_no"], "shots": shots} if shots else None
    return screenplay, storyboard


def main() -> None:
    parser = argparse.ArgumentParser(description="Renderability 金样打分")
    parser.add_argument("--screenplay", type=Path)
    parser.add_argument("--storyboard", type=Path)
    parser.add_argument("--episode-id")
    parser.add_argument("--baseline", type=Path, default=BASELINE_PATH)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    if args.episode_id:
        screenplay, storyboard = _from_episode(args.episode_id)
    else:
        if not args.screenplay or not args.storyboard:
            parser.error("需要 --episode-id，或同时提供 --screenplay 与 --storyboard")
        screenplay = _load_json(args.screenplay)
        storyboard = _load_json(args.storyboard)

    baseline = _load_json(args.baseline) if args.baseline and args.baseline.exists() else None
    result = score_renderability_sample(
        screenplay=screenplay, storyboard=storyboard, baseline=baseline
    )
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
