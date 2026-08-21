#!/usr/bin/env python3
"""Drive「我欲封天」前 5 集剧本的清空 / 并发启动 / 监控。

用法:
    py scripts/yyft_first5.py status
    py scripts/yyft_first5.py clear      # 取消活动运行 + 两步批准删除剧本
    py scripts/yyft_first5.py start      # 并发启动 5 集
    py scripts/yyft_first5.py monitor    # 轮询直到全部 ready 或出现失败
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8230"
SESSION = (ROOT / "data" / "local_session_secret.txt").read_text(encoding="utf-8").strip()
LOG = ROOT / "logs" / "yyft_first5.log"
EPISODES = [
    ("EP1", "ep_3d523ff4d0a4"),
    ("EP2", "ep_94fc1dd627f5"),
    ("EP3", "ep_a0e90058f83c"),
    ("EP4", "ep_3b07c59c0856"),
    ("EP5", "ep_0a7130b7b402"),
]


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def call(method: str, path: str, body: dict | None = None, headers: dict | None = None,
         timeout: int = 60) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method)
    request.add_header("X-Manju-Session", SESSION)
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": raw[:500]}
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": repr(exc)}


def status_of(eid: str) -> dict:
    code, payload = call("GET", f"/api/episodes/{eid}/screenplay/status", timeout=30)
    if code != 200:
        return {"screenplay_status": f"HTTP{code}", "detail": payload}
    return payload


def brief(payload: dict) -> str:
    production = payload.get("screenplay_production") or {}
    return "{st} active={a} phase={p} stage={i}/{n} err={e}".format(
        st=payload.get("screenplay_status"),
        a=payload.get("active"),
        p=production.get("phase"),
        i=production.get("stage_index"),
        n=production.get("stage_count"),
        e=(payload.get("screenplay_error") or "").replace("\n", " ")[:160],
    )


def cmd_status() -> int:
    for name, eid in EPISODES:
        log(f"{name} {eid} :: {brief(status_of(eid))}")
    return 0


def clear_one(name: str, eid: str) -> bool:
    payload = status_of(eid)
    state = payload.get("screenplay_status")
    log(f"{name} clear: current={state}")
    if state in {"queued", "running", "repairing"}:
        code, resp = call("POST", f"/api/episodes/{eid}/screenplay/cancel", {}, timeout=90)
        log(f"{name} cancel -> HTTP{code} {json.dumps(resp, ensure_ascii=False)[:200]}")
        for _ in range(30):
            time.sleep(2)
            state = status_of(eid).get("screenplay_status")
            if state not in {"queued", "running", "cancelling"}:
                break
        log(f"{name} after cancel status={state}")
    code, resp = call("DELETE", f"/api/episodes/{eid}/screenplay", {}, timeout=120)
    token = resp.get("approval_token")
    if token:
        code, resp = call(
            "DELETE", f"/api/episodes/{eid}/screenplay", {},
            headers={"x-manju-approval-token": token}, timeout=120,
        )
    log(f"{name} delete -> HTTP{code} {json.dumps(resp, ensure_ascii=False)[:240]}")
    final = status_of(eid).get("screenplay_status")
    log(f"{name} post-clear status={final}")
    return final == "pending"


def cmd_clear() -> int:
    log("=== CLEAR FIRST 5 START ===")
    ok = all([clear_one(name, eid) for name, eid in EPISODES])
    log(f"=== CLEAR FIRST 5 DONE ok={ok} ===")
    return 0 if ok else 1


def cmd_start() -> int:
    log("=== START FIRST 5 (concurrent) ===")
    stamp = int(time.time())
    failed = False
    for index, (name, eid) in enumerate(EPISODES, start=1):
        body = {"idempotency_key": f"yyft-ep{index}-{stamp}"}
        code, resp = call("POST", f"/api/episodes/{eid}/screenplay", body, timeout=60)
        if resp.get("status") == "waiting_approval" and resp.get("approval_token"):
            log(f"{name} start needs approval ({resp.get('approval_id')}), confirming...")
            code, resp = call(
                "POST", f"/api/episodes/{eid}/screenplay", body,
                headers={"x-manju-approval-token": resp["approval_token"]}, timeout=60,
            )
        log(f"{name} start -> HTTP{code} {json.dumps(resp, ensure_ascii=False)[:240]}")
        if code != 200 or resp.get("status") not in {"queued", "running", "repairing"}:
            failed = True
    return 1 if failed else 0


def cmd_monitor(interval: int = 30, limit: int = 20000) -> int:
    log("=== MONITOR FIRST 5 ===")
    waited = 0
    while waited <= limit:
        states = {}
        for name, eid in EPISODES:
            payload = status_of(eid)
            states[name] = payload
            log(f"{name} :: {brief(payload)}")
        values = [p.get("screenplay_status") for p in states.values()]
        if any(v == "failed" for v in values):
            log("RESULT=FAILED " + ",".join(
                f"{k}={v.get('screenplay_status')}" for k, v in states.items()))
            return 2
        paused = [k for k, v in states.items()
                  if v.get("screenplay_status") in {"repairing", "pending"}
                  and not v.get("active")]
        if paused:
            log(f"RESULT=PAUSED {paused}")
            return 3
        if all(v == "ready" for v in values):
            log("RESULT=ALL_READY ✅")
            return 0
        time.sleep(interval)
        waited += interval
    log("RESULT=TIMEOUT")
    return 4


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    handlers = {
        "status": cmd_status,
        "clear": cmd_clear,
        "start": cmd_start,
        "monitor": cmd_monitor,
    }
    sys.exit(handlers[command]())
