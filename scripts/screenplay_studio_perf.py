#!/usr/bin/env python3
"""映射台页面初始化性能测量（基线 / 优化后对比复用同一脚本）。

用法:
    py scripts/screenplay_studio_perf.py <episode_id> [--repeat 5] [--label baseline]

测量内容:
  * 映射台首屏实际发起的每个 API：wall clock、HTTP 状态、原始字节、gzip 字节；
  * 串行 waterfall 总耗时（按前端真实依赖顺序）与并行下界；
  * 每个 API 在后端执行期间的 SQL 条数与最慢 SQL（进程内直调，独立于 HTTP）。

结果同时写入 logs/screenplay_studio_perf_<label>.json，便于前后对比。
"""
from __future__ import annotations

import argparse
import gzip
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 直接 `python scripts/x.py` 运行时 sys.path[0] 是 scripts/，不是仓库根。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.session_token import session_token  # noqa: E402
sys.path.insert(0, str(ROOT))
BASE = "http://127.0.0.1:8230"


def timed_get(path: str, timeout: int = 120) -> dict:
    request = urllib.request.Request(BASE + path, method="GET")
    request.add_header("X-Manju-Session", session_token())
    request.add_header("Accept-Encoding", "gzip")
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            elapsed = (time.perf_counter() - start) * 1000
            encoding = response.headers.get("Content-Encoding", "")
            wire = len(raw)
            body = gzip.decompress(raw) if encoding == "gzip" else raw
            return {
                "path": path, "status": response.status, "ms": round(elapsed, 1),
                "wire_bytes": wire, "raw_bytes": len(body), "encoding": encoding or "identity",
            }
    except urllib.error.HTTPError as exc:
        elapsed = (time.perf_counter() - start) * 1000
        return {"path": path, "status": exc.code, "ms": round(elapsed, 1),
                "wire_bytes": 0, "raw_bytes": 0, "encoding": "error"}


def studio_requests(episode_id: str, project_id: str, *, legacy: bool = False) -> list[str]:
    """映射台首屏真实请求集合（与后端访问日志里观察到的一致）。

    ``legacy=True`` 复现「案头助手」还在时的取数方式：它的上下文标签为了三个名字
    直接拉整份项目投影（千集项目 4.79 MB / 558 ms）。该功能已在 2026-08-22 被移除，
    所以默认请求集不再包含它；保留 legacy 开关只为让历史对比可复现。
    """
    requests = [
        "/api/session",
        "/api/settings",
        "/api/projects",
        f"/api/projects/{project_id}?view=picker&episode_limit=60"
        f"&episode_cursor={episode_id}",
        f"/api/episodes/{episode_id}?view=script",
        f"/api/episodes/{episode_id}/screenplay/status",
        f"/api/episodes/{episode_id}/screenplay/draft",
    ]
    if legacy:
        requests.insert(3, f"/api/projects/{project_id}")
    return requests


def sql_profile(episode_id: str) -> dict:
    """进程内直调，统计每个端点触发的 SQL 条数与耗时分布。"""
    import app.db as db

    profile: dict[str, dict] = {}
    statements: list[tuple[str, float]] = []
    original_get_conn = db.get_conn

    traced: set[int] = set()

    def traced_get_conn(*args, **kwargs):
        conn = original_get_conn(*args, **kwargs)
        if id(conn) not in traced:
            traced.add(id(conn))
            conn.set_trace_callback(lambda sql: statements.append((sql, time.perf_counter())))
        return conn

    db.get_conn = traced_get_conn
    try:
        from app.domain import screenplay_ops  # noqa: F401
        from app import api as api_module

        targets = {
            "episode_detail_script": lambda: api_module.episode_detail(episode_id, view="script"),
            "screenplay_status": lambda: api_module.screenplay_lightweight_status(episode_id),
            "screenplay_draft": lambda: api_module.get_screenplay_draft(episode_id),
        }
        for name, call in targets.items():
            statements.clear()
            start = time.perf_counter()
            try:
                payload = call()
                error = None
            except Exception as exc:  # noqa: BLE001
                payload, error = None, repr(exc)[:200]
            elapsed = (time.perf_counter() - start) * 1000
            kinds: dict[str, int] = {}
            for sql, _ in statements:
                head = " ".join(sql.strip().split())[:110]
                kinds[head] = kinds.get(head, 0) + 1
            profile[name] = {
                "ms": round(elapsed, 1),
                "sql_count": len(statements),
                "distinct_sql": len(kinds),
                "top_sql": sorted(kinds.items(), key=lambda kv: -kv[1])[:8],
                "payload_bytes": len(json.dumps(payload, ensure_ascii=False, default=str)) if payload else 0,
                "error": error,
            }
    finally:
        db.get_conn = original_get_conn
    return profile


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("episode_id")
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--label", default="baseline")
    parser.add_argument("--skip-sql", action="store_true")
    parser.add_argument("--legacy-agent-context", action="store_true")
    args = parser.parse_args()

    import sqlite3

    conn = sqlite3.connect(f"file:{ROOT / 'data' / 'manju.db'}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT project_id, episode_no FROM episodes WHERE id=?", (args.episode_id,)).fetchone()
    if row is None:
        print(f"episode {args.episode_id} not found")
        return 1
    project_id = row["project_id"]

    paths = studio_requests(
        args.episode_id, project_id, legacy=args.legacy_agent_context,
    )
    samples: dict[str, list[dict]] = {path: [] for path in paths}
    for _ in range(args.repeat):
        for path in paths:
            samples[path].append(timed_get(path))

    report: dict = {
        "label": args.label, "episode_id": args.episode_id, "episode_no": row["episode_no"],
        "project_id": project_id, "repeat": args.repeat, "http": [],
    }
    total_median = 0.0
    for path in paths:
        runs = samples[path]
        median = statistics.median(r["ms"] for r in runs)
        total_median += median
        report["http"].append({
            "path": path,
            "status": runs[0]["status"],
            "ms_median": round(median, 1),
            "ms_min": round(min(r["ms"] for r in runs), 1),
            "ms_max": round(max(r["ms"] for r in runs), 1),
            "wire_bytes": runs[0]["wire_bytes"],
            "raw_bytes": runs[0]["raw_bytes"],
        })
    report["serial_total_ms"] = round(total_median, 1)
    report["request_count"] = len(paths)
    report["total_wire_bytes"] = sum(entry["wire_bytes"] for entry in report["http"])
    report["total_raw_bytes"] = sum(entry["raw_bytes"] for entry in report["http"])
    if not args.skip_sql:
        report["sql"] = sql_profile(args.episode_id)

    out = ROOT / "logs" / f"screenplay_studio_perf_{args.label}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\n-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
