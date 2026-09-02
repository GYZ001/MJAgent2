#!/usr/bin/env python3
"""对**正在运行**的后端做一次全量 HTTP 路由巡检。

区别于 pytest/vitest（验证源码）：路由清单从活后端的 ``GET /openapi.json``
现取，路径参数从 ``data/manju.db`` 真实数据解析，验证的是活进程而非源码。

默认只读：只对每条 ``GET`` 路由发请求。``--with-safe-writes`` 额外跑一轮幂等
写探测（建临时项目->软删除->彻底清除，全程自清理）；任何可能触发模型/媒体
生成的写接口一律不调用，报告里列出被跳过的写接口。判据：2xx 通过；404 视为
「可选资源不存在」单独计数但仍算通过；401/403/422/5xx 及连接异常一律失败，
任意失败令进程退出码非 0。

用法：``py scripts/smoke_live_routes.py`` 只读巡检；``--per-route N`` 每条
路由多打几个 ID；``--with-safe-writes`` 额外跑安全写探测；``--allow-stale``
跳过版本偏斜闸门（开发期）；``--json PATH`` 另存机器可读结果。日志追加写
``logs/smoke_live_routes.log``。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.session_token import WITH_LOCAL_SECRET, session_token  # noqa: E402
from scripts.smoke_live_routes_params import (  # noqa: E402
    append_query, build_url, format_args, path_param_names, quote_media_url_path,
    resolve_query_params, resolve_route_params,
)

BASE = "http://127.0.0.1:8230"
DB_PATH = ROOT / "data" / "manju.db"
APP_DIR = ROOT / "app"
DIST_DIR = ROOT / "frontend" / "dist"
LOG = ROOT / "logs" / "smoke_live_routes.log"

# 日志

def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")

# 版本偏斜闸门：后端进程启动时间必须晚于 app/**/*.py 最新 mtime；用
# /proc/<pid> 的 mtime 当启动时刻，与 scripts/publish_frontend.py 的
# _backend_processes 同一手法，独立实现一份轻量版（不跨脚本 import 私有函数）。

def _backend_processes() -> list[tuple[int, float]]:
    me = os.getpid()
    found: list[tuple[int, float]] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            argv = [a for a in (entry / "cmdline").read_bytes().decode().split("\0") if a]
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if "app.main:app" in argv and any(Path(a).name.startswith("uvicorn") for a in argv):
            found.append((int(entry.name), entry.stat().st_mtime))
    return sorted(found, key=lambda item: item[1])

def check_staleness(app_dir: Path) -> tuple[bool, str]:
    procs = _backend_processes()
    if not procs:
        return False, "没找到监听中的后端进程（uvicorn app.main:app），后端是否已启动？"
    pid, started = procs[0]
    newest = max((p.stat().st_mtime for p in app_dir.rglob("*.py")), default=None)
    if newest is None:
        return True, f"后端 PID {pid} 在跑，app/ 下没有 .py 文件可比对，跳过偏斜检查"
    if started >= newest:
        return True, f"后端 PID {pid} 启动时间晚于 app/ 最新改动，版本一致"
    return False, (
        f"后端 PID {pid} 启动时间早于 app/ 最新改动（差约 {newest - started:.0f}s）"
        "——它跑的是旧代码，巡检结果对当前代码无意义。用 --allow-stale 可强制跑（仅限开发期调试）。"
    )

# HTTP

def http_call(
    base: str, method: str, path: str, *,
    headers: dict[str, str] | None = None,
    json_body: dict | None = None,
    raw_body: bytes | None = None,
    timeout: float = 20.0,
) -> tuple[int, bytes, float]:
    data = None
    hdrs = dict(headers or {})
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    elif raw_body is not None:
        data = raw_body
    request = urllib.request.Request(base + path, data=data, method=method)
    for key, value in hdrs.items():
        request.add_header(key, value)
    start = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, resp.read(), time.monotonic() - start
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), time.monotonic() - start
    except Exception as exc:  # noqa: BLE001 - 连接异常必须计为失败，不静默吞掉
        return 0, str(exc).encode("utf-8", "replace"), time.monotonic() - start

def fetch_openapi(base: str, timeout: float) -> dict:
    status, body, _ = http_call(base, "GET", "/openapi.json", timeout=timeout)
    if status != 200:
        raise SystemExit(f"无法获取 /openapi.json：HTTP {status}，后端是否在跑？")
    return json.loads(body)

# 判据分类

def classify_result(status: int) -> tuple[str, str]:
    if status == 0:
        return "FAIL", "连接异常"
    if 200 <= status < 300:
        return "PASS", ""
    if status == 404:
        return "OPTIONAL_404", "资源不存在（可选资源，如尚未生成的分镜/视频计划/交付包）"
    return "FAIL", f"HTTP {status}"

# GET 路由巡检

def call_one(
    base: str, headers: dict, method: str, template: str, params: dict, timeout: float,
    query: dict | None = None,
) -> dict:
    url = append_query(build_url(template, params), query or {})
    status, body, elapsed = http_call(base, method, url, headers=headers, timeout=timeout)
    outcome, detail = classify_result(status)
    # query 参数在 args 列里加 "?" 前缀区分于路径参数，方便一眼看出填了什么。
    args_display = format_args({**params, **{f"?{k}": v for k, v in (query or {}).items()}})
    return {
        "method": method, "path": template, "args": args_display, "url": url,
        "status": status, "elapsed_ms": round(elapsed * 1000, 1), "result": outcome,
        "detail": detail, "body_snippet": body[:300].decode("utf-8", "replace") if outcome == "FAIL" else "",
    }

_MEDIA_TEMPLATE = "/media/{path}"

def check_media_ticket(base: str, rel_path: str, timeout: float) -> list[dict]:
    """票据校验由 .env 的 ``MJ_MEDIA_REQUIRE_TICKET=1`` 打开（见
    app/media_urls.py::media_ticket_required），不是旧进程行为。带合法票据须
    放行、不带票据须 403，两条都验证，只测一半会漏掉「票据其实没生效」这类
    回归；``build_media_url`` 只做本地 HMAC 签名，不连网、不写盘。"""
    from app.media_urls import build_media_url  # 延迟导入：脚本其余部分不碰 app.*

    def row(label, url, status, elapsed, body, outcome, detail):
        return {
            "method": "GET", "path": label, "args": f"path={rel_path}", "url": url,
            "status": status, "elapsed_ms": round(elapsed * 1000, 1), "result": outcome,
            "detail": detail,
            "body_snippet": body[:300].decode("utf-8", "replace") if outcome == "FAIL" else "",
        }

    ticketed = build_media_url(rel_path)
    if not ticketed:
        return [row(f"{_MEDIA_TEMPLATE}（带票据）", rel_path, 0, 0.0, b"", "FAIL", "build_media_url 未能签发票据")]
    # build_media_url 不做百分号编码，中文/空格等非 ASCII 路径段直接拼进 URL
    # 会让 urllib 在连接层抛异常（status=0）；发请求前补一道编码。
    ticketed = quote_media_url_path(ticketed)
    s1, b1, e1 = http_call(base, "GET", ticketed, timeout=timeout)
    o1, d1 = classify_result(s1)
    bare = f"/media/{quote(rel_path, safe='/')}"
    s2, b2, e2 = http_call(base, "GET", bare, timeout=timeout)
    o2 = "PASS" if s2 == 403 else "FAIL"
    d2 = "" if s2 == 403 else f"期望无票据返回 403（票据生效证据），实际 HTTP {s2}——票据校验可能已失效"
    return [
        row(f"{_MEDIA_TEMPLATE}（带票据）", ticketed, s1, e1, b1, o1, d1),
        row(f"{_MEDIA_TEMPLATE}（无票据，应403）", bare, s2, e2, b2, o2, d2),
    ]

def run_get_routes(
    base: str, headers: dict, conn: sqlite3.Connection, openapi_paths: dict,
    per_route: int, timeout: float, projects_dir: Path,
) -> list[dict]:
    results: list[dict] = []
    for template, ops in sorted(openapi_paths.items()):
        if "get" not in ops:
            continue
        query, missing_query = resolve_query_params(conn, ops["get"])
        if missing_query:
            results.append({
                "method": "GET", "path": template, "args": "-", "url": template,
                "status": None, "elapsed_ms": None, "result": "SKIP",
                "detail": f"必填 query 参数 {', '.join(missing_query)} 未能解析出真实值"
                          "（未知参数名，或已被更靠前的互斥参数占用，或数据库无可用数据）",
                "body_snippet": "",
            })
            continue
        names = path_param_names(template)
        rows, reason = resolve_route_params(conn, names, per_route, projects_dir)
        if rows is None:
            results.append({
                "method": "GET", "path": template, "args": "-", "url": template,
                "status": None, "elapsed_ms": None, "result": "SKIP",
                "detail": reason, "body_snippet": "",
            })
            continue
        for params in rows:
            if template == _MEDIA_TEMPLATE:
                results.extend(check_media_ticket(base, params["path"], timeout))
            else:
                results.append(call_one(base, headers, "GET", template, params, timeout, query))
    return results

# 前端交付

_ASSET_RE = re.compile(r'(?:src|href)="(/assets/[^"]+\.(?:js|css))"')

def extract_asset_refs(html: str) -> list[str]:
    return sorted(set(_ASSET_RE.findall(html)))

def _probe_static(method: str, path: str, base: str, timeout: float) -> dict:
    status, body, elapsed = http_call(base, method, path, headers={}, timeout=timeout)
    ok = status == 200 and len(body) > 0
    return {
        "method": method, "path": path, "args": "-", "url": path,
        "status": status, "elapsed_ms": round(elapsed * 1000, 1),
        "result": "PASS" if ok else "FAIL",
        "detail": "" if ok else f"HTTP {status} 或响应体为空",
        "body_snippet": "" if ok else body[:300].decode("utf-8", "replace"),
    }

def check_frontend(base: str, dist_dir: Path, timeout: float) -> list[dict]:
    index = dist_dir / "index.html"
    if not dist_dir.exists() or not index.exists():
        return [{
            "method": "GET", "path": "/", "args": "-", "url": "/",
            "status": None, "elapsed_ms": None, "result": "SKIP",
            "detail": f"{dist_dir} 不存在或缺 index.html，前端未发布", "body_snippet": "",
        }]
    results = [_probe_static("GET", "/", base, timeout), _probe_static("GET", "/index.html", base, timeout)]
    html_text = index.read_text(encoding="utf-8", errors="replace")
    for asset in extract_asset_refs(html_text):
        results.append(_probe_static("GET", asset, base, timeout))
    return results

# 安全写探测（--with-safe-writes）：只做幂等且不触发生成的动作。

_PROBE_NOVEL = "第一章 冒烟测试\n" + "本文件由 scripts/smoke_live_routes.py 生成，仅用于验证写接口可用性。" * 30

_GENERATION_KEYWORDS = (
    "generate", "complete", "video", "portrait", "refs", "bible", "screenplay", "storyboard",
    "adopt", "publish", "produce", "engine", "recover", "chapters", "text-models", "target-duration",
)

def build_multipart(fields: dict[str, str], file_field: str, filename: str, file_bytes: bytes) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        for k, v in fields.items()
    ]
    header = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
        f'filename="{filename}"\r\nContent-Type: text/plain\r\n\r\n'
    ).encode()
    parts.append(header + file_bytes + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"

def call_with_approval(base: str, headers: dict, method: str, path: str, timeout: float) -> tuple[int, bytes, float]:
    """命令总线对需确认的写命令先回 202 + approval_token，须带同名头重放一次。"""
    status, body, elapsed = http_call(base, method, path, headers=headers, timeout=timeout)
    try:
        token = json.loads(body or b"{}").get("approval_token")
    except (json.JSONDecodeError, AttributeError):
        token = None
    if token:
        retry_headers = {**headers, "x-manju-approval-token": token}
        status, body, elapsed = http_call(base, method, path, headers=retry_headers, timeout=timeout)
    return status, body, elapsed

def _write_result(method: str, path: str, status: int, elapsed: float, body: bytes) -> dict:
    ok = 200 <= status < 300
    return {
        "method": method, "path": path, "args": "-", "url": path,
        "status": status, "elapsed_ms": round(elapsed * 1000, 1),
        "result": "PASS" if ok else "FAIL",
        "detail": "" if ok else f"HTTP {status}",
        "body_snippet": "" if ok else (body or b"")[:300].decode("utf-8", "replace"),
    }

def safe_write_temp_project(base: str, headers: dict, timeout: float) -> list[dict]:
    """建一个临时项目 -> 软删除 -> 彻底清除；全程自行清理，不留垃圾数据。"""
    steps: list[dict] = []
    name = f"__smoke_probe_{int(time.time())}"
    body, ctype = build_multipart({"name": name}, "file", "smoke.txt", _PROBE_NOVEL.encode("utf-8"))
    status, resp, elapsed = http_call(
        base, "POST", "/api/projects", headers={**headers, "Content-Type": ctype},
        raw_body=body, timeout=timeout,
    )
    steps.append(_write_result("POST", "/api/projects", status, elapsed, resp))
    try:
        payload = json.loads(resp or b"{}")
    except json.JSONDecodeError:
        payload = {}
    project_id = payload.get("project_id") if isinstance(payload, dict) else None
    if not project_id:
        return steps
    d_status, d_resp, d_elapsed = http_call(
        base, "DELETE", f"/api/projects/{project_id}", headers=headers, timeout=timeout,
    )
    steps.append(_write_result("DELETE", "/api/projects/{project_id}", d_status, d_elapsed, d_resp))
    p_status, p_resp, p_elapsed = call_with_approval(
        base, headers, "DELETE", f"/api/projects/{project_id}/purge", timeout,
    )
    steps.append(_write_result("DELETE", "/api/projects/{project_id}/purge", p_status, p_elapsed, p_resp))
    return steps

def classify_skip_reason(method: str, path: str) -> str:
    lowered = path.lower()
    if any(word in lowered for word in _GENERATION_KEYWORDS):
        return "跳过：路径特征显示可能触发模型/媒体生成任务或改动小说正文，本脚本绝不调用"
    if method in ("PUT", "DELETE"):
        return "跳过：会修改或删除真实业务数据，超出安全写操作范围（未验证幂等性/无法保证清理）"
    return "跳过：无可用测试凭据或无法安全构造请求体（如登录）"

def list_skipped_writes(openapi_paths: dict, exercised: set[str]) -> list[tuple[str, str, str]]:
    skipped: list[tuple[str, str, str]] = []
    for template, ops in sorted(openapi_paths.items()):
        for method in ("post", "put", "delete"):
            if method not in ops:
                continue
            key = f"{method.upper()} {template}"
            if key in exercised:
                continue
            skipped.append((method.upper(), template, classify_skip_reason(method.upper(), template)))
    return skipped

# 输出

def print_results(results: list[dict]) -> None:
    for r in results:
        status = r["status"] if r["status"] is not None else "-"
        elapsed = f"{r['elapsed_ms']}ms" if r["elapsed_ms"] is not None else "-"
        log(f"{r['result']:<13} {r['method']:<6} {r['path']:<66} args=[{r['args']}] status={status} {elapsed}")
        if r["result"] == "FAIL" and r["detail"]:
            log(f"    detail: {r['detail']}")
        if r["result"] == "FAIL" and r["body_snippet"]:
            log(f"    body:   {r['body_snippet']}")

def print_skipped_writes(skipped: list[tuple[str, str, str]]) -> None:
    log(f"--- 跳过的写接口（{len(skipped)} 条，均未调用）---")
    for method, path, reason in skipped:
        log(f"  {method:<6} {path:<58} {reason}")

def print_summary(results: list[dict]) -> dict:
    counts = {"PASS": 0, "OPTIONAL_404": 0, "SKIP": 0, "FAIL": 0}
    for r in results:
        counts[r["result"]] = counts.get(r["result"], 0) + 1
    log("--- 汇总 ---")
    log(
        f"总请求数: {len(results)}  通过: {counts['PASS']}  可选404: {counts['OPTIONAL_404']}  "
        f"跳过: {counts['SKIP']}  失败: {counts['FAIL']}"
    )
    if counts["FAIL"]:
        log("失败列表:")
        for r in results:
            if r["result"] == "FAIL":
                log(f"  {r['method']} {r['path']} args=[{r['args']}] -> {r['detail']}")
    return counts

def dump_json(path: str, results: list[dict], skipped_writes: list[tuple[str, str, str]], summary: dict) -> None:
    payload = {
        "summary": summary,
        "results": results,
        "skipped_writes": [{"method": m, "path": p, "reason": r} for m, p, r in skipped_writes],
    }
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

# 入口

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="对运行中的后端做全量 HTTP 路由巡检")
    parser.add_argument("--base", default=BASE, help="后端地址，默认 " + BASE)
    parser.add_argument("--per-route", type=int, default=1, help="每条路由最多打几个真实 ID")
    parser.add_argument("--with-safe-writes", action="store_true", help="额外跑一轮幂等写探测")
    parser.add_argument("--allow-stale", action="store_true", help="跳过版本偏斜闸门（开发期调试）")
    parser.add_argument("--json", default=None, help="另存机器可读结果到该路径")
    parser.add_argument("--timeout", type=float, default=20.0, help="单条请求超时秒数")
    return parser.parse_args()

def main() -> int:
    args = parse_args()
    ok, msg = check_staleness(APP_DIR)
    log(f"[版本偏斜] {msg}")
    if not ok and not args.allow_stale:
        return 1
    headers = {"X-Manju-Session": session_token(WITH_LOCAL_SECRET)}
    openapi = fetch_openapi(args.base, args.timeout)
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    projects_dir = (ROOT / "projects").resolve()
    results = run_get_routes(args.base, headers, conn, openapi["paths"], args.per_route, args.timeout, projects_dir)
    results += check_frontend(args.base, DIST_DIR, args.timeout)
    exercised: set[str] = set()
    if args.with_safe_writes:
        results += safe_write_temp_project(args.base, headers, args.timeout)
        exercised = {
            "POST /api/projects",
            "DELETE /api/projects/{project_id}",
            "DELETE /api/projects/{project_id}/purge",
        }
    skipped_writes = list_skipped_writes(openapi["paths"], exercised)
    print_results(results)
    print_skipped_writes(skipped_writes)
    summary = print_summary(results)
    if args.json:
        dump_json(args.json, results, skipped_writes, summary)
    return 1 if summary["FAIL"] > 0 else 0

if __name__ == "__main__":
    raise SystemExit(main())
