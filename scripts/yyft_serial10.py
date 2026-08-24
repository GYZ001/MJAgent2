#!/usr/bin/env python3
"""「我欲封天」EP1→EP10 严格串行剧本生成驱动。

用法:
    py scripts/yyft_serial10.py status
    py scripts/yyft_serial10.py clear            # 仅清空本项目 EP1-EP10 的剧本数据
    py scripts/yyft_serial10.py run              # EP1→EP10 串行，一集失败即停下待 RCA
    py scripts/yyft_serial10.py run --from EP4   # 限流恢复后从当前失败集继续

设计约束（与任务书一致）：
  * 严格串行，任何时刻只有一集在跑；
  * 非限流失败**立即停止**，不自动重试、不自动跳过，留给人做根因分析；
  * 只有明确的供应商限流（HTTP 429 / rate_limit / quota / 限流）才自动等待重试：
    优先遵循 Retry-After，没有就等 30 分钟；
  * 唯一的例外是 GEN-RETRY-GRANT（error_logs.category=='generation_retry_grant'，
    见 [BLUEPRINT_PROVIDER_RETRY_GRANT_REQUIRED]）：这是「上次供应商结果未知」
    的结构化、可安全自愈的失败——重新调用本脚本已经在用的 POST /screenplay
    两步确认协议即可由领域层签发新 Production Grant 继续生成，门禁本身不变。
    每集最多自动恢复 RETRY_GRANT_MAX_AUTO_RECOVERIES 次，超过仍按非限流失败停下；
  * 只清理本项目这 10 集的剧本数据，绝不触碰其它项目或分集。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8230"
SESSION = (ROOT / "data" / "local_session_secret.txt").read_text(encoding="utf-8").strip()
PROJECT_ID = "proj_3ac0b627fa46"
EPISODES = [
    ("EP1", "ep_3d523ff4d0a4"),
    ("EP2", "ep_94fc1dd627f5"),
    ("EP3", "ep_a0e90058f83c"),
    ("EP4", "ep_3b07c59c0856"),
    ("EP5", "ep_0a7130b7b402"),
    ("EP6", "ep_94adca9b9942"),
    ("EP7", "ep_621d93ac1231"),
    ("EP8", "ep_677fcf50aa52"),
    ("EP9", "ep_ec45c5f38a9d"),
    ("EP10", "ep_4a29e6cfc088"),
]
LOG = ROOT / "logs" / "serial10.log"

# 只有这些才算供应商限流。普通 timeout / 连接错误 / JSON / schema / 模型输出异常
# 一律不算，必须停下来做根因分析。
#
# 刻意不放裸 "429"/"tpm"/"rpm"：错误码形如 ERR-20260822-4295ab，裸数字与两字母
# 缩写会在无关文本里假阳性，把一个真实缺陷误判成限流并自动等 30 分钟——那正是
# 任务书禁止的。HTTP 429 由 provider_calls.http_status 这一**结构化**字段单独判定。
RATE_LIMIT_MARKERS = (
    "rate_limit",
    "rate limit",
    "ratelimit",
    "too many requests",
    "quota exceeded",
    "quota temporarily",
    "insufficient_quota",
    "限流",
    "请求过于频繁",
    "concurrency limit",
    "tokens per minute",
    "requests per minute",
)

# GEN-RETRY-GRANT（app/errors.py CATEGORIES["generation_retry_grant"]）是唯一一类
# 有客观结构化证据、且领域层已经设计好全自动安全恢复路径的失败：
#   - 判据不是文本猜测，是 error_logs.category 这一分类器自身写下的结构化字段
#     （app/errors.py:classify 命中 [BLUEPRINT_PROVIDER_RETRY_GRANT_REQUIRED] 时
#     写入，见 app/errors.py:161-165），与本文件其它地方刻意避免裸文本匹配的
#     RATE_LIMIT_MARKERS 判断同一纪律；
#   - 恢复手段不是绕过门禁，是重新调用本脚本已经在用的同一个两步确认协议：
#     POST /api/episodes/{id}/screenplay 在 requires_fresh_retry_grant 时经
#     Command Bus 返回 202 + approval_token，`approved()` 帮助函数已经会自动
#     用该 token 二次确认；确认后 app/domain/screenplay_ops.py 的
#     `_spawn_screenplay_activation` 会按当前未确认调用集签发新 Production
#     Grant 再继续生成（见该文件 1723-1809 行），门禁本身不做任何改动、不放宽。
#     该端到端路径由 tests/test_screenplay_controls.py::
#     test_confirmed_unknown_retry_crosses_handler_api_facade_and_mints_grant 覆盖。
# 每集自动恢复次数设上限，防止真实故障（例如后端反复重启、或该集持续复现同一
# 缺陷）被无限静默重试掩盖；超过上限仍按原逻辑停下等人工 RCA。
RETRY_GRANT_MAX_AUTO_RECOVERIES = 3


def log(msg: str) -> None:
    line = f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def call(method: str, path: str, body: dict | None = None,
         headers: dict | None = None, timeout: int = 90) -> tuple[int, dict]:
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


def approved(method: str, path: str, body: dict, timeout: int = 120) -> tuple[int, dict]:
    """Issue one command, confirming the two-step approval when asked."""
    code, resp = call(method, path, body, timeout=timeout)
    token = resp.get("approval_token")
    if token:
        code, resp = call(
            method, path, body,
            headers={"x-manju-approval-token": token}, timeout=timeout,
        )
    return code, resp


def status_of(eid: str) -> dict:
    code, payload = call("GET", f"/api/episodes/{eid}/screenplay/status", timeout=60)
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
        e=(payload.get("screenplay_error") or "").replace("\n", " ")[:200],
    )


def _readonly_conn(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path if db_path is not None else (ROOT / "data" / "manju.db")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def recent_failure_evidence(eid: str, since: float) -> str:
    """Durable evidence for the last failure: error logs + provider calls."""
    conn = _readonly_conn()
    try:
        chunks = [
            f"{row['id']} | {row['category']} | {row['exc_type']} | {row['message']}"
            for row in conn.execute(
                "SELECT id, category, exc_type, message FROM error_logs "
                "WHERE ts>=? AND json_extract(context_json,'$.episode_id')=? "
                "ORDER BY ts DESC LIMIT 6",
                (since, eid),
            )
        ]
        chunks += [
            f"call {row['id']} http={row['http_status']} status={row['status']} "
            f"err={str(row['error'] or '')[:300]}"
            for row in conn.execute(
                "SELECT id, http_status, status, error FROM provider_calls "
                "WHERE ts>=? AND status!='OK' ORDER BY id DESC LIMIT 8",
                (since,),
            )
        ]
    finally:
        conn.close()
    return "\n".join(chunks)


def provider_retry_after(since: float) -> float | None:
    """Honour a provider-declared wait when one exists in the durable record."""
    conn = _readonly_conn()
    try:
        rows = conn.execute(
            "SELECT error, response_json FROM provider_calls "
            "WHERE ts>=? AND http_status=429 ORDER BY id DESC LIMIT 5",
            (since,),
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        for blob in (row["error"], row["response_json"]):
            text = str(blob or "")
            for key in ("retry-after", "retry_after", "retryAfter", "retry_delay"):
                index = text.lower().find(key)
                if index < 0:
                    continue
                digits = "".join(
                    ch for ch in text[index + len(key): index + len(key) + 24]
                    if ch.isdigit() or ch == "."
                )
                try:
                    value = float(digits)
                except ValueError:
                    continue
                if value > 0:
                    return min(value, 3600.0)
    return None


def is_rate_limited(eid: str, since: float) -> bool:
    """Only an explicit provider throttle counts; everything else needs an RCA."""
    conn = _readonly_conn()
    try:
        if conn.execute(
            "SELECT 1 FROM provider_calls WHERE ts>=? AND http_status=429 LIMIT 1",
            (since,),
        ).fetchone():
            return True
    finally:
        conn.close()
    text = recent_failure_evidence(eid, since).lower()
    return any(marker in text for marker in RATE_LIMIT_MARKERS)


def is_retry_grant_recoverable(
    eid: str, since: float, *, db_path: Path | None = None,
) -> bool:
    """Whether this failure is exactly GEN-RETRY-GRANT for this episode.

    Structured evidence only: ``error_logs.category`` is the classifier's own
    output (app/errors.py:classify), not a text guess. Scoped to this
    episode's ``context_json.episode_id`` and to calls at/after ``since`` so a
    stale, already-superseded error from an earlier attempt -- or a different
    episode's/another session's unrelated interrupted call sharing the same
    time window -- can never be misread as this attempt's outcome.
    """
    conn = _readonly_conn(db_path)
    try:
        return bool(conn.execute(
            "SELECT 1 FROM error_logs "
            "WHERE ts>=? AND category='generation_retry_grant' "
            "AND json_extract(context_json,'$.episode_id')=? LIMIT 1",
            (since, eid),
        ).fetchone())
    finally:
        conn.close()


def clear_one(name: str, eid: str) -> bool:
    payload = status_of(eid)
    state = payload.get("screenplay_status")
    if state in {"queued", "running", "repairing"} and payload.get("active"):
        code, resp = approved("POST", f"/api/episodes/{eid}/screenplay/cancel", {})
        log(f"{name} cancel -> HTTP{code} {json.dumps(resp, ensure_ascii=False)[:160]}")
        for _ in range(60):
            time.sleep(2)
            if not status_of(eid).get("active"):
                break
    code, resp = approved("DELETE", f"/api/episodes/{eid}/screenplay", {})
    final = status_of(eid).get("screenplay_status")
    log(f"{name} clear: {state} -> delete HTTP{code} -> {final}")
    return final == "pending"


def cmd_clear(_args) -> int:
    log("=== CLEAR EP1-EP10 (test project only) ===")
    ok = all([clear_one(name, eid) for name, eid in EPISODES])
    log(f"=== CLEAR DONE ok={ok} ===")
    return 0 if ok else 1


def cmd_status(_args) -> int:
    for name, eid in EPISODES:
        log(f"{name} {eid} :: {brief(status_of(eid))}")
    return 0


def await_terminal(name: str, eid: str, interval: int = 30, limit: int = 7200) -> dict:
    waited = 0
    last = ""
    while waited <= limit:
        payload = status_of(eid)
        state = str(payload.get("screenplay_status") or "")
        line = brief(payload)
        if line != last:
            log(f"{name} :: {line}")
            last = line
        if state == "ready":
            return payload
        if state in {"failed"} and not payload.get("active"):
            return payload
        if state == "repairing" and not payload.get("active"):
            return payload
        if state == "pending" and not payload.get("active"):
            return payload
        time.sleep(interval)
        waited += interval
    log(f"{name} TIMEOUT after {waited}s")
    return status_of(eid)


def start_or_resume(name: str, eid: str) -> bool:
    payload = status_of(eid)
    state = str(payload.get("screenplay_status") or "")
    eligibility = (payload.get("screenplay_production") or {}).get("eligibility") or {}
    resumable = bool(eligibility.get("resumable"))
    stamp = int(time.time())
    if resumable and state in {"repairing", "failed"}:
        code, resp = approved(
            "POST", f"/api/episodes/{eid}/screenplay/resume",
            {"idempotency_key": f"serial10-resume-{eid}-{stamp}"},
        )
        log(f"{name} resume -> HTTP{code} {json.dumps(resp, ensure_ascii=False)[:200]}")
    else:
        code, resp = approved(
            "POST", f"/api/episodes/{eid}/screenplay",
            {"idempotency_key": f"serial10-start-{eid}-{stamp}"},
        )
        log(f"{name} start -> HTTP{code} {json.dumps(resp, ensure_ascii=False)[:200]}")
    return resp.get("status") in {"queued", "running", "repairing"}


def cmd_run(args) -> int:
    start_index = 0
    if args.start_from:
        names = [name for name, _ in EPISODES]
        if args.start_from not in names:
            log(f"未知起始集：{args.start_from}")
            return 2
        start_index = names.index(args.start_from)
    log(f"=== SERIAL RUN EP{start_index + 1}-EP{len(EPISODES)} START ===")
    results: dict[str, str] = {}
    for name, eid in EPISODES[start_index:]:
        retry_grant_recoveries = 0
        while True:
            payload = status_of(eid)
            if payload.get("screenplay_status") == "ready":
                log(f"{name} already ready")
                results[name] = "ready"
                break
            if payload.get("active"):
                log(f"{name} 已在运行，等待其结束")
                payload = await_terminal(name, eid)
                if payload.get("screenplay_status") == "ready":
                    results[name] = "ready"
                    break
            since = time.time()
            if not start_or_resume(name, eid):
                log(f"{name} 无法启动或续跑 —— 停止，等待人工根因分析")
                results[name] = "start_refused"
                log("=== SERIAL RUN STOPPED ===")
                log(json.dumps(results, ensure_ascii=False))
                return 3
            payload = await_terminal(name, eid)
            state = str(payload.get("screenplay_status") or "")
            if state == "ready":
                log(f"{name} READY ✅")
                results[name] = "ready"
                break
            failure = (payload.get("screenplay_error") or "")[:400]
            log(f"{name} 未通过：state={state} err={failure}")
            if is_rate_limited(eid, since):
                delay = provider_retry_after(since) or 1800.0
                log(f"{name} 判定为供应商限流，等待 {int(delay)} 秒后从本集继续"
                    "（不修改业务代码）")
                time.sleep(delay)
                continue
            if (
                is_retry_grant_recoverable(eid, since)
                and retry_grant_recoveries < RETRY_GRANT_MAX_AUTO_RECOVERIES
            ):
                retry_grant_recoveries += 1
                log(f"{name} 判定为 GEN-RETRY-GRANT（上次供应商结果未知，蓝图分片"
                    f"准入被安全拦截）——第 {retry_grant_recoveries}/"
                    f"{RETRY_GRANT_MAX_AUTO_RECOVERIES} 次自动重新发起首版剧本"
                    "（走既有两步确认协议签发新 Production Grant，不绕过门禁）")
                continue
            if is_retry_grant_recoverable(eid, since):
                log(f"{name} GEN-RETRY-GRANT 已达自动恢复上限"
                    f"（{RETRY_GRANT_MAX_AUTO_RECOVERIES} 次），判定为真实故障")
            log(f"{name} 非限流失败 —— 停止整轮，等待根因分析。证据：")
            for line in recent_failure_evidence(eid, since).splitlines()[:10]:
                log(f"    {line}")
            results[name] = state or "failed"
            log("=== SERIAL RUN STOPPED ===")
            log(json.dumps(results, ensure_ascii=False))
            return 4
    log("=== SERIAL RUN DONE === " + json.dumps(results, ensure_ascii=False))
    return 0 if all(value == "ready" for value in results.values()) else 1


def cmd_verify(_args) -> int:
    """验收：10 集全部通过项目自身的业务校验，且没有残留异常或脏数据。

    只用项目**已有**的判据，不另立标准：
      * `_screenplay_ready` —— 分镜前置门禁，会把已发布权威链整条重新验证一遍；
      * `resolve_current_screenplay_authority` —— 不可变权威解析，任何漂移即抛错；
      * 轻量状态端点 —— 页面看到的状态必须同样是已交付；
      * 没有活跃 run、没有 screenplay_error、没有未消费的完成凭证。
    """
    import sys as _sys

    _sys.path.insert(0, str(ROOT))
    from app.db import get_conn
    from app.domain.common import _screenplay_ready
    from app.production.screenplay_authority import (
        resolve_current_screenplay_authority,
    )

    log("=== VERIFY EP1-EP10 ===")
    ok = True
    db = get_conn()
    for name, eid in EPISODES:
        row = db.execute("SELECT * FROM episodes WHERE id=?", (eid,)).fetchone()
        payload = status_of(eid)
        problems: list[str] = []
        if row is None:
            problems.append("分集记录不存在")
        else:
            if row["screenplay_status"] != "ready":
                problems.append(f"screenplay_status={row['screenplay_status']}")
            if row["screenplay_error"]:
                problems.append(f"screenplay_error={str(row['screenplay_error'])[:120]}")
            if row["active_screenplay_run_id"]:
                run = db.execute(
                    "SELECT status FROM workflow_runs WHERE id=?",
                    (row["active_screenplay_run_id"],),
                ).fetchone()
                if run is not None and run["status"] not in {
                    "SUCCEEDED", "FAILED", "CANCELLED", "PARTIAL",
                }:
                    problems.append(f"仍有活跃 run（{run['status']}）")
            if not _screenplay_ready(dict(row)):
                problems.append("_screenplay_ready=False")
            try:
                resolve_current_screenplay_authority(eid)
            except Exception as exc:  # noqa: BLE001 - 验收即要看到真实原因
                problems.append(f"权威解析失败：{exc}")
        state = (payload.get("screenplay_state") or {}).get("code")
        if not str(state or "").startswith("ready"):
            problems.append(f"页面状态={state}")
        if problems:
            ok = False
            log(f"{name} ✗ " + "；".join(problems))
        else:
            log(f"{name} ✓ ready / 权威链完整 / 页面状态={state}")
    log(f"=== VERIFY {'PASSED' if ok else 'FAILED'} ===")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["status", "clear", "run", "verify"])
    parser.add_argument("--from", dest="start_from", default="")
    args = parser.parse_args()
    return {
        "status": cmd_status, "clear": cmd_clear,
        "run": cmd_run, "verify": cmd_verify,
    }[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
