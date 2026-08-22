#!/usr/bin/env python3
"""一条命令取齐一次剧本生成失败的全部 durable 证据。

用法:
    py scripts/screenplay_failure_rca.py <episode_id> [--since-min 60] [--calls 12]

输出：
  * 分集当前投影状态与 production revision checkpoint；
  * 该集最近的 run / step 及其失败码；
  * 该集最近的 error_logs（含嵌套错误码链）；
  * 时间窗内的 provider_calls（状态、http、延迟、收到字符数、max_tokens、
    completion/reasoning token、错误文本），用于区分「答案被截断」「流中断」
    「未送达」「限流」这几类完全不同的失败。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ERROR_CODE = re.compile(r"ERR-\d{8}-[0-9a-f]{6}")


def conn() -> sqlite3.Connection:
    handle = sqlite3.connect(f"file:{ROOT / 'data' / 'manju.db'}?mode=ro", uri=True)
    handle.row_factory = sqlite3.Row
    return handle


def show_episode(db: sqlite3.Connection, episode_id: str) -> str:
    row = db.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if row is None:
        raise SystemExit(f"episode not found: {episode_id}")
    print("=== EPISODE ===")
    for key in (
        "episode_no", "screenplay_status", "status", "screenplay_error",
        "active_screenplay_run_id", "screenplay_production_revision_id",
        "screenplay_artifact_id", "working_screenplay_artifact_id",
        "published_screenplay_artifact_id",
    ):
        print(f"  {key}: {str(row[key])[:400]}")
    revision = db.execute(
        "SELECT * FROM production_revisions WHERE episode_id=? "
        "ORDER BY updated_at DESC LIMIT 1", (episode_id,),
    ).fetchone()
    if revision is not None:
        print("=== LATEST REVISION ===")
        print(f"  id={revision['id']} status={revision['status']} "
              f"grant={revision['grant_id']} baseline={revision['baseline_generation_count']}")
        checkpoint = json.loads(revision["checkpoint_json"] or "{}")
        for key in ("phase", "yield_reason", "activation_no", "open_issue_ids"):
            print(f"  checkpoint.{key}: {str(checkpoint.get(key))[:300]}")
        shards = checkpoint.get("shards") or []
        if shards:
            states: dict[str, int] = {}
            for shard in shards:
                state = str((shard or {}).get("status") or "?")
                states[state] = states.get(state, 0) + 1
            print(f"  checkpoint.shards: {states}")
    return str(row["active_screenplay_run_id"] or "")


def show_runs(db: sqlite3.Connection, episode_id: str, limit: int = 3) -> None:
    print("=== RECENT RUNS ===")
    for run in db.execute(
        "SELECT id,status,failure_code,failure_message,current_step_key,started_at "
        "FROM workflow_runs WHERE scope_type='episode' AND scope_id=? "
        "ORDER BY started_at DESC LIMIT ?", (episode_id, limit),
    ):
        print(f"  {run['id']} {run['status']} step={run['current_step_key']} "
              f"code={run['failure_code']}")
        if run["failure_message"]:
            print(f"      {str(run['failure_message'])[:300]}")


def show_errors(db: sqlite3.Connection, episode_id: str, since: float) -> None:
    print("=== ERROR LOGS (含嵌套错误码链) ===")
    seen: set[str] = set()
    queue = [
        dict(row) for row in db.execute(
            "SELECT * FROM error_logs WHERE ts>=? "
            "AND json_extract(context_json,'$.episode_id')=? ORDER BY ts DESC LIMIT 8",
            (since, episode_id),
        )
    ]
    while queue:
        row = queue.pop(0)
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        print(f"  {row['id']} | {row['category']} | {row['exc_type']} | {row['action']}")
        print(f"      {str(row['message'])[:400]}")
        tail = str(row["traceback"] or "").strip().splitlines()[-1:]
        if tail:
            print(f"      ^ {tail[0][:300]}")
        for code in ERROR_CODE.findall(str(row["message"] or "")):
            if code in seen:
                continue
            nested = db.execute(
                "SELECT * FROM error_logs WHERE id=?", (code,)
            ).fetchone()
            if nested is not None:
                queue.append(dict(nested))


def show_provider_calls(db: sqlite3.Connection, since: float, limit: int) -> None:
    print("=== PROVIDER CALLS (时间窗内，非 OK 优先) ===")
    for row in db.execute(
        "SELECT * FROM provider_calls WHERE ts>=? ORDER BY (status='OK'), id DESC LIMIT ?",
        (since, limit),
    ):
        try:
            request = json.loads(row["request_json"] or "{}")
            response = json.loads(row["response_json"] or "{}")
            meta = json.loads(row["meta"] or "{}")
        except (TypeError, ValueError):
            request, response, meta = {}, {}, {}
        usage = response.get("usage") or {}
        reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        finish = ((response.get("choices") or [{}])[0] or {}).get("finish_reason")
        print(
            f"  #{row['id']} {row['status']} http={row['http_status']} "
            f"stage={meta.get('stage_key')} lat={row['latency_ms']}ms "
            f"chars={row['received_chars']} max_tokens={request.get('max_tokens')} "
            f"completion={usage.get('completion_tokens')} reasoning={reasoning} "
            f"finish={finish}"
        )
        if row["error"]:
            print(f"      {str(row['error'])[:300]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("episode_id")
    parser.add_argument("--since-min", type=float, default=60.0)
    parser.add_argument("--calls", type=int, default=12)
    args = parser.parse_args()
    since = time.time() - args.since_min * 60
    db = conn()
    try:
        show_episode(db, args.episode_id)
        show_runs(db, args.episode_id)
        show_errors(db, args.episode_id, since)
        show_provider_calls(db, since, args.calls)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
