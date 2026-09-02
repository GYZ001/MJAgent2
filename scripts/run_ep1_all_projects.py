#!/usr/bin/env python3
"""N 个项目第一集全链路驱动：人物谱→定妆→场景库→场景图→映射台→分镜台→生成台。

与 `scripts/yyft_pipeline10.py`（单项目严格串行、失败即停做 RCA）的定位相反：
本脚本要的是**一次跑完多个项目、把失败摊开看**，所以失败策略反过来——

  * 某项目某阶段失败 → 只记录，该项目就地停住，**不影响其它项目继续跑**；
  * 供应商限流（结构化 HTTP 429 或白名单文案）不算失败，等一轮再试，
    每阶段最多重试 RATE_LIMIT_RETRIES 次，避免限流把一次回归拖成无限循环；
  * 全部目标项目都到达终态（ready 或 failed）才退出。

多个项目并行跑：阶段几乎都是等模型，串行等于把各份等待时间相加。并行的代价是
供应商侧竞争，用 STAGGER_S 错开启动来削峰，真撞上限流由上面的重试兜住。

状态写 `logs/ep1_all_state.json`，每个阶段结束都落盘，中途看进度或重跑续跑都读它。
重跑时已经 ok 的阶段直接跳过（判据取自服务端真实状态，不是本文件的记账）。

项目按**名字（或 id）运行时解析**，不写死 id——历史上写死的
"王六郎"/"罗刹海市"/"黄英"/"我欲封天" 四组 (项目名, project_id, episode_id)
已随项目重建全部失效（当前库零命中），写死一次就要再踩一次。EP1 的
episode_id 同样运行时按 episode_no=1 查询。

用法（--projects 必填，无默认值）：
    py scripts/run_ep1_all_projects.py run --projects 我欲封天
    py scripts/run_ep1_all_projects.py run --projects 我欲封天 橘座在上
    py scripts/run_ep1_all_projects.py status --projects 我欲封天   # 只看当前状态，不驱动
    py scripts/run_ep1_all_projects.py report --projects 我欲封天   # 打印汇总
"""
from __future__ import annotations

import argparse
import sys
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 直接 `python scripts/x.py` 运行时 sys.path[0] 是 scripts/，不是仓库根。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.session_token import session_token  # noqa: E402
BASE = "http://127.0.0.1:8230"
DB_PATH = ROOT / "data" / "manju.db"
LOG = ROOT / "logs" / os.environ.get("EP1_LOG_NAME", "ep1_all.log")
# 补跑某个项目时要开第二个进程，与主进程并存。状态文件是读-改-写，两个进程共用
# 同一份会丢更新，所以补跑走独立文件，report 时再合并。
STATE = ROOT / "logs" / os.environ.get("EP1_STATE_NAME", "ep1_all_state.json")

STAGES = ("bible", "refs", "scene_bible", "scene_refs", "screenplay", "storyboard", "video")

STAGGER_S = 90          # 相邻项目启动错开，削供应商侧并发峰值
RATE_LIMIT_RETRIES = 2  # 每阶段因限流重试的次数上限

# 与 yyft_pipeline10.py 同一份口径：只有明确的供应商限流才自动等待重试。
# 刻意不放裸 "429"/"tpm"——错误码形如 ERR-20260822-4295ab，裸数字会假阳性，
# 把真实缺陷误判成限流并自动等待。HTTP 429 由 provider_calls.http_status 单独判定。
RATE_LIMIT_MARKERS = (
    "rate_limit", "rate limit", "ratelimit", "too many requests",
    "quota exceeded", "quota temporarily", "insufficient_quota",
    "限流", "请求过于频繁", "concurrency limit",
    "tokens per minute", "requests per minute",
)

_log_lock = threading.Lock()
_state_lock = threading.Lock()


def log(name: str, msg: str) -> None:
    line = f"[{time.strftime('%m-%d %H:%M:%S')}] {name:6s} {msg}"
    with _log_lock:
        print(line, flush=True)
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


# ---------------------------------------------------------------------------
# HTTP 与两段式审批
# ---------------------------------------------------------------------------

def call(method: str, path: str, body: dict | None = None,
         headers: dict | None = None, timeout: int = 90) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method)
    request.add_header("X-Manju-Session", session_token())
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


def approved(method: str, path: str, body: dict | None = None,
             timeout: int = 180) -> tuple[int, dict]:
    """命令总线对 confirmation=ALWAYS/WHEN_IMPACT 的写命令先回 202 + approval_token，
    须带 `X-Manju-Approval-Token` 用同一份 body 重放才真正执行。与业务侧各自的
    quote_id / preview_token 是两层独立的门，互不替代。"""
    code, resp = call(method, path, body, timeout=timeout)
    token = resp.get("approval_token") if isinstance(resp, dict) else None
    if token:
        code, resp = call(
            method, path, body,
            headers={"x-manju-approval-token": token}, timeout=timeout,
        )
    return code, resp


def _readonly_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def resolve_projects(selectors: list[str]) -> list[tuple[str, str, str]]:
    """把项目名/id 列表解析成 (项目名, project_id, EP1 episode_id) 三元组。

    绝不硬编码历史 id：命中 0 个或多个都直接退出，宁可停下来也不要猜错项目跑
    掉几小时。回收站项目（deleted_at 非空）不参与按名字匹配，避免重名的软删
    项目把解析变成"命中多个"；显式给 id 时仍按给的来。
    """
    conn = _readonly_conn()
    try:
        out: list[tuple[str, str, str]] = []
        for selector in selectors:
            rows = conn.execute(
                "SELECT id, name FROM projects WHERE id=? OR (name=? AND deleted_at IS NULL)"
                " ORDER BY id",
                (selector, selector),
            ).fetchall()
            if not rows:
                raise SystemExit(f"库里找不到项目：{selector!r}")
            if len(rows) > 1:
                matched = "、".join(f"{r['id']}({r['name']})" for r in rows)
                raise SystemExit(f"项目 {selector!r} 命中多个，请改用 id：{matched}")
            project_id, name = rows[0]["id"], rows[0]["name"]
            ep = conn.execute(
                "SELECT id FROM episodes WHERE project_id=? AND episode_no=1",
                (project_id,),
            ).fetchone()
            if not ep:
                raise SystemExit(f"项目 {name!r}（{project_id}）没有 EP1 分集")
            out.append((name, project_id, ep["id"]))
        return out
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 限流判定（结构化优先，文案兜底）
# ---------------------------------------------------------------------------

def is_rate_limited(project_id: str, since: float) -> bool:
    """结构化 429 优先；文案白名单只在本项目的失败调用里找，避免蹭到别的项目。"""
    conn = _readonly_conn()
    try:
        if conn.execute(
            "SELECT 1 FROM provider_calls WHERE ts>=? AND http_status=429 "
            "AND (project_id=? OR project_id IS NULL) LIMIT 1",
            (since, project_id),
        ).fetchone():
            return True
        rows = conn.execute(
            "SELECT error FROM provider_calls WHERE ts>=? AND status!='OK' "
            "AND (project_id=? OR project_id IS NULL) ORDER BY id DESC LIMIT 20",
            (since, project_id),
        ).fetchall()
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    for row in rows:
        text = str(row["error"] or "").lower()
        if any(marker in text for marker in RATE_LIMIT_MARKERS):
            return True
    return False


def failure_evidence(project_id: str, since: float) -> str:
    """最近一次失败的持久化证据：错误日志 + 供应商调用。只读。"""
    conn = _readonly_conn()
    parts: list[str] = []
    try:
        for row in conn.execute(
            "SELECT category, exc_type, action, message FROM error_logs "
            "WHERE ts>=? ORDER BY ts DESC LIMIT 3",
            (since,),
        ).fetchall():
            parts.append(
                f"error_log[{row['category']}/{row['action']}/{row['exc_type']}] "
                f"{(row['message'] or '')[:300]}"
            )
        for row in conn.execute(
            "SELECT kind, model, http_status, error, latency_ms FROM provider_calls "
            "WHERE ts>=? AND status!='OK' AND (project_id=? OR project_id IS NULL) "
            "ORDER BY id DESC LIMIT 3",
            (since, project_id),
        ).fetchall():
            parts.append(
                f"provider[{row['kind']}/{row['model']}] http={row['http_status']} "
                f"{int((row['latency_ms'] or 0) / 1000)}s {str(row['error'] or '')[:300]}"
            )
    except sqlite3.Error as exc:
        parts.append(f"(证据查询失败: {exc})")
    finally:
        conn.close()
    return " || ".join(parts) or "(无新增错误日志)"


# ---------------------------------------------------------------------------
# 状态读取
# ---------------------------------------------------------------------------

def project_status(project_id: str) -> dict:
    code, payload = call("GET", f"/api/projects/{project_id}", timeout=60)
    return payload if code == 200 else {"_http": code, "_detail": payload}


def screenplay_status(eid: str) -> dict:
    code, payload = call("GET", f"/api/episodes/{eid}/screenplay/status", timeout=60)
    return payload if code == 200 else {"screenplay_status": f"HTTP{code}", "detail": payload}


def storyboard_status(eid: str) -> dict:
    code, payload = call("GET", f"/api/episodes/{eid}/storyboard/status", timeout=60)
    return payload if code == 200 else {"state": f"HTTP{code}", "detail": payload}


def video_status(eid: str) -> dict:
    code, payload = call("GET", f"/api/episodes/{eid}/video-completion", timeout=60)
    return payload if code == 200 else {"user_state": f"HTTP{code}", "detail": payload}


def _wait_project_field(name: str, project_id: str, field: str, label: str,
                        timeout_s: int, poll_s: int = 20) -> str:
    """轮询到 projects.<field> 离开 running；返回终态字符串。"""
    waited = 0
    last = ""
    while waited <= timeout_s:
        proj = project_status(project_id)
        state = str(proj.get(field) or "")
        extra = ""
        if field == "refs_status":
            done = len([c for c in (proj.get("bible") or {}).get("characters") or []
                        if c.get("ref_image_path")])
            total = len((proj.get("bible") or {}).get("characters") or [])
            extra = f" 定妆 {done}/{total}"
        elif field == "scene_refs_status":
            scenes = (proj.get("bible") or {}).get("scenes") or []
            done = len([s for s in scenes if s.get("ref_image_path")])
            extra = f" 场景图 {done}/{len(scenes)}"
        line = f"{label}={state}{extra}"
        if line != last:
            log(name, f"{label} :: {line}")
            last = line
        if state != "running":
            return state
        time.sleep(poll_s)
        waited += poll_s
    log(name, f"{label} 轮询超时 {timeout_s}s")
    return str(project_status(project_id).get(field) or "timeout")


# ---------------------------------------------------------------------------
# 各阶段
# ---------------------------------------------------------------------------

def _eligible_characters(proj: dict) -> list[dict]:
    """本次真正该有定妆照的角色，从人物谱当场推导。

    详情生成失败的角色会以 stub 落库（appearance_status=insufficient_evidence、
    portrait_eligible=false），这是设计好的诚实产出，不是缺陷；但它也意味着
    「该有几张定妆照」只能问人物谱要，不能写死一个数字。"""
    return [
        c for c in ((proj.get("bible") or {}).get("characters") or [])
        if c.get("portrait_eligible")
    ]


def _assert_bible_usable(proj: dict) -> tuple[bool, str]:
    """人物谱的产物信号：有角色，且至少一个角色的详情真的生成出来了。

    只看 bible_status='ready' 会放行「整份都是 stub」——2026-08-28 那次
    Idempotency-Key 编码缺陷让每个角色的详情三次尝试全挂，人物谱照样 ready，
    下游定妆 0 张图也照样 ready，一路绿到分镜台才看得出不对。"""
    chars = (proj.get("bible") or {}).get("characters") or []
    if not chars:
        return False, "人物谱为空，一个角色都没有"
    eligible = _eligible_characters(proj)
    if not eligible:
        reasons = {str(c.get("appearance_status") or "unknown") for c in chars}
        return False, (
            f"{len(chars)} 个角色全部是 stub（无一可定妆），"
            f"appearance_status={sorted(reasons)}——详情生成阶段整体失败"
        )
    stub = len(chars) - len(eligible)
    note = f"，其中 {stub} 个仅 stub" if stub else ""
    return True, f"{len(chars)} 个角色，{len(eligible)} 个可定妆{note}"


def stage_bible(name: str, pid: str, eid: str) -> tuple[bool, str]:
    """人物谱。报价 action=generate_bible_and_refs，一次确认同时覆盖定妆扣费。
    成功后服务端自动级联启动定妆与场景清单（bible_ops._start_refs_generation /
    _start_scene_bible_preparation），所以这里不再单独触发那两步。"""
    proj = project_status(pid)
    if proj.get("bible_status") == "ready" and (proj.get("bible") or {}).get("characters"):
        ok, detail = _assert_bible_usable(proj)
        if ok:
            return True, f"{detail}（已就绪，跳过）"
        return False, detail

    code, quote = call("POST", f"/api/projects/{pid}/bible/generate-precheck", {}, timeout=120)
    if code != 200 or not quote.get("quote_id"):
        return False, f"预检失败 HTTP{code} {json.dumps(quote, ensure_ascii=False)[:300]}"
    log(name, f"人物谱预检：{quote.get('character_count')} 角色 预估 {quote.get('estimated_cost_cny')} 元")

    code, resp = approved("POST", f"/api/projects/{pid}/bible", {
        "confirm": True,
        "quote_id": quote["quote_id"],
        "idempotency_key": quote["quote_id"],
    })
    if code not in (200, 202):
        return False, f"启动失败 HTTP{code} {json.dumps(resp, ensure_ascii=False)[:300]}"
    log(name, f"人物谱已启动 HTTP{code}")

    state = _wait_project_field(name, pid, "bible_status", "人物谱", timeout_s=5400)
    if state != "ready":
        proj = project_status(pid)
        return False, f"人物谱终态={state} err={(proj.get('bible_error') or '')[:400]}"
    return _assert_bible_usable(project_status(pid))


def stage_refs(name: str, pid: str, eid: str) -> tuple[bool, str]:
    """定妆照。由人物谱完成时自动级联启动；这里只等它收敛，
    只有在服务端没启动（idle/failed）时才手工补触发一次。"""
    proj = project_status(pid)
    state = str(proj.get("refs_status") or "")
    if state in ("idle", "failed"):
        # 级联没起来或上一轮失败：走一次 precheck→confirm 补触发（resume 保留已有成品）
        code, quote = call("POST", f"/api/projects/{pid}/refs/precheck",
                           {"resume": True}, timeout=120)
        if code == 200 and quote.get("quote_id"):
            code2, resp = approved("POST", f"/api/projects/{pid}/refs", {
                "resume": True, "confirm": True,
                "quote_id": quote["quote_id"], "idempotency_key": quote["quote_id"],
            })
            log(name, f"定妆手工触发 HTTP{code2} {json.dumps(resp, ensure_ascii=False)[:200]}")
        else:
            log(name, f"定妆预检 HTTP{code} {json.dumps(quote, ensure_ascii=False)[:200]}")

    state = _wait_project_field(name, pid, "refs_status", "定妆", timeout_s=10800, poll_s=30)
    proj = project_status(pid)
    eligible = _eligible_characters(proj)
    done = [c for c in eligible if c.get("ref_image_path")]
    detail = f"{len(done)}/{len(eligible)} 个可定妆角色有定妆照"
    err = (proj.get("refs_error") or "")[:300]
    if not done:
        # 一张都没有就是没成，不管 refs_status 写的是什么。
        return False, f"{detail}（状态={state}）err={err}"
    if len(done) < len(eligible):
        missing = [c.get("name") for c in eligible if not c.get("ref_image_path")]
        return True, f"{detail}，缺图：{'、'.join(str(m) for m in missing[:8])}（状态={state}）"
    return True, detail


def stage_scene_bible(name: str, pid: str, eid: str) -> tuple[bool, str]:
    """场景清单（免费）。同样由人物谱完成时自动级联；完成后 scene_refs_status
    回落 'idle' 表示「清单就绪、等待付费出图」，不是失败。"""
    proj = project_status(pid)
    scenes = (proj.get("bible") or {}).get("scenes") or []
    if scenes:
        return True, f"{len(scenes)} 个场景"

    state = _wait_project_field(name, pid, "scene_refs_status", "场景清单",
                                timeout_s=3600, poll_s=20)
    proj = project_status(pid)
    scenes = (proj.get("bible") or {}).get("scenes") or []
    if scenes:
        return True, f"{len(scenes)} 个场景"
    return False, f"场景清单为空 状态={state} err={(proj.get('scene_refs_error') or '')[:400]}"


def stage_scene_refs(name: str, pid: str, eid: str) -> tuple[bool, str]:
    """场景图（付费，需独立确认）。"""
    proj = project_status(pid)
    scenes = (proj.get("bible") or {}).get("scenes") or []
    done = [s for s in scenes if s.get("ref_image_path")]
    if scenes and len(done) == len(scenes):
        return True, f"已齐 {len(done)}/{len(scenes)}"

    if str(proj.get("scene_refs_status") or "") != "running":
        code, quote = call("POST", f"/api/projects/{pid}/scene-refs/precheck",
                           {"resume": True}, timeout=120)
        if code != 200 or not quote.get("quote_id"):
            return False, f"场景图预检失败 HTTP{code} {json.dumps(quote, ensure_ascii=False)[:300]}"
        log(name, f"场景图预检：{quote.get('image_count')} 张 预估 {quote.get('estimated_cost_cny')} 元")
        code, resp = approved("POST", f"/api/projects/{pid}/scene-refs", {
            "resume": True, "confirm": True,
            "quote_id": quote["quote_id"], "idempotency_key": quote["quote_id"],
        })
        if code not in (200, 202):
            return False, f"场景图启动失败 HTTP{code} {json.dumps(resp, ensure_ascii=False)[:300]}"
        log(name, f"场景图已启动 HTTP{code}")

    state = _wait_project_field(name, pid, "scene_refs_status", "场景图",
                                timeout_s=10800, poll_s=30)
    proj = project_status(pid)
    scenes = (proj.get("bible") or {}).get("scenes") or []
    done = [s for s in scenes if s.get("ref_image_path")]
    detail = f"{len(done)}/{len(scenes)} 场景有图"
    if state in ("ready", "warning") or done:
        note = f"（{state}: {(proj.get('scene_refs_error') or '')[:300]}）" if state == "warning" else ""
        return bool(done), detail + note
    return False, f"场景图终态={state} {detail} err={(proj.get('scene_refs_error') or '')[:400]}"


def stage_screenplay(name: str, pid: str, eid: str) -> tuple[bool, str]:
    """映射台（分集映射包）。"""
    status = screenplay_status(eid)
    if status.get("screenplay_status") == "ready":
        return True, "已就绪，跳过"

    if not status.get("active"):
        stamp = int(time.time())
        code, resp = approved("POST", f"/api/episodes/{eid}/screenplay",
                              {"idempotency_key": f"ep1all-{eid}-{stamp}"})
        log(name, f"映射台启动 HTTP{code} {json.dumps(resp, ensure_ascii=False)[:220]}")
        if code not in (200, 202) and resp.get("status") not in {"queued", "running", "repairing"}:
            return False, f"映射台未能启动 HTTP{code} {json.dumps(resp, ensure_ascii=False)[:300]}"

    waited, last, timeout_s, poll_s = 0, "", 5400, 20
    while waited <= timeout_s:
        status = screenplay_status(eid)
        production = status.get("screenplay_production") or {}
        line = (f"{status.get('screenplay_status')} active={status.get('active')} "
                f"phase={production.get('phase')} "
                f"stage={production.get('stage_index')}/{production.get('stage_count')}")
        if line != last:
            log(name, f"映射台 :: {line}")
            last = line
        state = status.get("screenplay_status")
        if state in ("ready", "failed") and not status.get("active"):
            break
        time.sleep(poll_s)
        waited += poll_s

    status = screenplay_status(eid)
    if status.get("screenplay_status") == "ready":
        return True, "映射包就绪"
    return False, (f"映射台终态={status.get('screenplay_status')} "
                   f"err={(status.get('screenplay_error') or '')[:400]}")


def stage_storyboard(name: str, pid: str, eid: str) -> tuple[bool, str]:
    """分镜台，含确认门 A（confirm-preview → confirm）。硬门禁不过就是真失败。"""
    status = storyboard_status(eid)
    if status.get("state") == "confirmed":
        return True, "已确认，跳过"

    if status.get("state") not in ("running", "ready_to_confirm"):
        code, resp = approved("POST", f"/api/episodes/{eid}/storyboard", None)
        log(name, f"分镜台启动 HTTP{code} {json.dumps(resp, ensure_ascii=False)[:220]}")
        started = (code == 200 and resp.get("status") == "scripting") or \
                  (code == 409 and "已被其他请求抢占" in str(resp))
        if not started:
            return False, f"分镜台未能启动 HTTP{code} {json.dumps(resp, ensure_ascii=False)[:300]}"

    waited, last, timeout_s, poll_s = 0, "", 7200, 15
    while waited <= timeout_s:
        status = storyboard_status(eid)
        line = (f"state={status.get('state')} "
                f"{status.get('produced_shots')}/{status.get('planned_shots')}镜 "
                f"validated={status.get('validated_shots')}")
        if line != last:
            log(name, f"分镜台 :: {line}")
            last = line
        if status.get("state") != "running":
            break
        time.sleep(poll_s)
        waited += poll_s

    state = status.get("state")
    if state not in ("ready_to_confirm", "confirmed"):
        return False, (f"分镜台终态={state} headline={status.get('headline')} "
                       f"issues={status.get('hard_gate_issues')}")
    if state == "confirmed":
        return True, f"{status.get('produced_shots')} 镜"

    code, resp = call("POST", f"/api/episodes/{eid}/confirm-preview", None, timeout=60)
    if code == 409:
        detail = (resp.get("detail") or resp) if isinstance(resp, dict) else {}
        hard = (detail.get("hard_gates") or {}).get("errors") if isinstance(detail, dict) else None
        return False, f"确认门 A 硬门禁未通过：{json.dumps(hard or resp, ensure_ascii=False)[:400]}"
    if code != 200 or "preview_token" not in resp:
        return False, f"确认门 A 预览失败 HTTP{code} {json.dumps(resp, ensure_ascii=False)[:300]}"
    code2, resp2 = approved("POST", f"/api/episodes/{eid}/confirm", {
        "preview_token": resp["preview_token"],
        "reason": "四项目第一集全链路回归：分镜整集门禁已通过、无 blocker，确认解锁付费视频阶段",
    })
    if code2 == 200 and resp2.get("confirmed"):
        return True, f"{status.get('produced_shots')} 镜已确认"
    return False, f"确认门 A confirm 失败 HTTP{code2} {json.dumps(resp2, ensure_ascii=False)[:300]}"


def _best_adopt_candidate(conn: sqlite3.Connection, shot_id: str) -> str | None:
    """复现 concat 的可播候选口径，并额外要求技术校验通过（adopt 端点自身的硬性要求）。"""
    rows = conn.execute(
        "SELECT id, video_path, image_inputs, technical_validation_json FROM shot_versions "
        "WHERE shot_id=? AND status='succeeded' AND video_path IS NOT NULL "
        "ORDER BY version_no DESC",
        (shot_id,),
    ).fetchall()
    for row in rows:
        try:
            meta = json.loads(row["image_inputs"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            meta = {}
        if isinstance(meta, dict) and meta.get("delivery_fallback"):
            continue
        if not row["video_path"] or not Path(row["video_path"]).is_file():
            continue
        try:
            technical = json.loads(row["technical_validation_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            technical = {}
        if technical and not technical.get("passed"):
            continue
        return row["id"]
    return None


VIDEO_AUTHORIZATION_TOPUPS = 2   # 追加授权的次数上限


def _authorize_and_resume(name: str, eid: str, proj: dict) -> bool:
    """按产品自己给出的 authorize_continue 出路补授权。

    Supervisor 在算完计划后发现授权额度不够会停在 WAITING_AUTHORIZATION、
    outcome=VIDEO_PLAN_INVALID——12 镜的默认额度 144 元，而含重试系数的成本预测
    高值 198.72 元。这是设计好的两阶段闸门，不是缺陷，正确做法是补授权而不是
    把闸门拆掉。

    补多少从系统自己算的 cost_forecast 推导，不写死数字：目标额度取预测高值的
    1.2 倍，差额即本次追加。预测里已经含了各镜的历史重试系数。"""
    action = next(
        (a for a in (proj.get("next_actions") or [])
         if isinstance(a, dict) and a.get("id") == "authorize_continue"),
        None,
    )
    grant_id = (action or {}).get("request_body", {}).get("completion_grant_id") \
        or proj.get("grant_id")
    if not grant_id:
        log(name, "生成台 :: 停在等待授权，但响应里没有可续的授权号")
        return False

    forecast = proj.get("cost_forecast") or {}
    cap = float((proj.get("budget") or {}).get("cap_cny") or 0)
    high = float(forecast.get("high_cny") or forecast.get("expected_cny") or 0)
    add_budget = max(1.0, round(high * 1.2 - cap, 2)) if high else max(1.0, round(cap, 2))
    code, resp = approved("POST", f"/api/episodes/{eid}/video-completion", {
        "mode": "resume",
        "completion_grant_id": grant_id,
        "add_budget_cny": add_budget,
        "add_wall_clock_s": 7200,
        "idempotency_key": f"ep1all-authz-{grant_id}-{int(cap)}",
    })
    log(name, f"生成台追加授权 +{add_budget} 元（原上限 {cap}，预测高值 {high}）"
              f" -> HTTP{code} {json.dumps(resp, ensure_ascii=False)[:200]}")
    return code in (200, 202)


def _poll_video(name: str, eid: str) -> dict:
    waited, last, timeout_s, poll_s = 0, "", 6 * 3600, 30
    while waited <= timeout_s:
        proj = video_status(eid)
        coverage = proj.get("coverage") or {}
        line = (f"running={proj.get('running')} user_state={proj.get('user_state')} "
                f"adopted={coverage.get('adopted')}/{coverage.get('total')} "
                f"phase={proj.get('phase')}")
        if line != last:
            log(name, f"生成台 :: {line}")
            last = line
        if not proj.get("running") and proj.get("user_state") != "recovering":
            return proj
        time.sleep(poll_s)
        waited += poll_s
    log(name, f"生成台轮询超时 {timeout_s}s")
    return video_status(eid)


def stage_video(name: str, pid: str, eid: str) -> tuple[bool, str]:
    """生成台：集级补齐 Supervisor + 逐镜采纳。"""
    proj = video_status(eid)
    if proj.get("user_state") != "completed":
        if not (proj.get("running") or proj.get("user_state") == "recovering"):
            if proj.get("phase") == "WAITING_AUTHORIZATION":
                _authorize_and_resume(name, eid, proj)
            else:
                code, resp = approved("POST", f"/api/episodes/{eid}/video-completion",
                                      {"mode": "fresh", "idempotency_key": f"ep1all-video-{eid}"})
                log(name, f"生成台启动 HTTP{code} {json.dumps(resp, ensure_ascii=False)[:220]}")
                started = code in (200, 202) or (code == 409 and "运行中" in str(resp))
                if not started:
                    return False, f"生成台未能启动 HTTP{code} {json.dumps(resp, ensure_ascii=False)[:300]}"

        proj = _poll_video(name, eid)
        topups = 0
        while (proj.get("phase") == "WAITING_AUTHORIZATION"
               and topups < VIDEO_AUTHORIZATION_TOPUPS):
            topups += 1
            if not _authorize_and_resume(name, eid, proj):
                break
            proj = _poll_video(name, eid)

        phase = proj.get("phase")
        if phase not in ("SUCCEEDED_COVERED", "COMPLETED_DEADLINE_FALLBACK"):
            return False, (f"生成台未达成功终态 phase={phase} "
                           f"user_state={proj.get('user_state')} "
                           f"outcome={proj.get('outcome')} 追加授权 {topups} 次")

    conn = _readonly_conn()
    try:
        shots = conn.execute(
            "SELECT id, shot_no FROM shots WHERE episode_id=? AND "
            "(adopted_version_id IS NULL OR adopted_version_id='') ORDER BY shot_no",
            (eid,),
        ).fetchall()
        candidates = [(r["id"], r["shot_no"], _best_adopt_candidate(conn, r["id"])) for r in shots]
    finally:
        conn.close()

    missing = []
    for shot_id, shot_no, version_id in candidates:
        if not version_id:
            missing.append(str(shot_no))
            continue
        code, resp = approved("POST", f"/api/shots/{shot_id}/adopt", {
            "version_id": version_id,
            "reason": "四项目第一集全链路回归：补齐已产出该镜技术校验通过的候选，采用最新版本",
            "idempotency_key": f"ep1all-adopt-{shot_id}",
        })
        if not (code == 200 and resp.get("adopted") == version_id):
            missing.append(f"{shot_no}(采纳HTTP{code})")

    final = video_status(eid)
    coverage = final.get("coverage") or {}
    detail = f"采纳 {coverage.get('adopted')}/{coverage.get('total')} 镜"
    if missing:
        return False, f"{detail}，无可采纳候选/采纳失败的镜次：{','.join(missing)}"
    return True, detail


STAGE_FUNCS = {
    "bible": stage_bible,
    "refs": stage_refs,
    "scene_bible": stage_scene_bible,
    "scene_refs": stage_scene_refs,
    "screenplay": stage_screenplay,
    "storyboard": stage_storyboard,
    "video": stage_video,
}

STAGE_LABELS = {
    "bible": "人物谱", "refs": "定妆", "scene_bible": "场景清单",
    "scene_refs": "场景图", "screenplay": "映射台",
    "storyboard": "分镜台", "video": "生成台",
}


# ---------------------------------------------------------------------------
# 状态文件
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_stage(name: str, stage: str, ok: bool, detail: str, elapsed: float) -> None:
    with _state_lock:
        state = load_state()
        entry = state.setdefault(name, {"stages": {}})
        entry["stages"][stage] = {
            "ok": ok, "detail": detail,
            "elapsed_min": round(elapsed / 60, 1),
            "at": time.strftime("%m-%d %H:%M:%S"),
        }
        if not ok:
            entry["failed_at"] = stage
        elif stage == STAGES[-1]:
            entry["failed_at"] = None
            entry["done"] = True
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# 单项目驱动
# ---------------------------------------------------------------------------

def drive_project(name: str, pid: str, eid: str, delay_s: float) -> None:
    if delay_s:
        time.sleep(delay_s)
    log(name, f"=== 开始 EP1 全链路 project={pid} episode={eid} ===")
    for stage in STAGES:
        func = STAGE_FUNCS[stage]
        label = STAGE_LABELS[stage]
        attempts = 0
        while True:
            since = time.time()
            try:
                ok, detail = func(name, pid, eid)
            except Exception as exc:  # noqa: BLE001 单阶段异常不许掀翻其它项目
                ok, detail = False, f"驱动脚本异常 {exc!r}"
            elapsed = time.time() - since
            if ok:
                log(name, f"[{label}] 通过（{elapsed / 60:.1f} 分钟）：{detail}")
                save_stage(name, stage, True, detail, elapsed)
                break
            if attempts < RATE_LIMIT_RETRIES and is_rate_limited(pid, since):
                attempts += 1
                log(name, f"[{label}] 判定为供应商限流，等 20 分钟后第 {attempts} 次重试")
                time.sleep(1200)
                continue
            evidence = failure_evidence(pid, since)
            log(name, f"[{label}] 失败（{elapsed / 60:.1f} 分钟）：{detail}")
            log(name, f"[{label}] 证据：{evidence[:800]}")
            save_stage(name, stage, False, f"{detail} || 证据: {evidence[:800]}", elapsed)
            log(name, f"=== 停在 {label}，本项目不再继续（其它项目不受影响）===")
            return
    log(name, "=== EP1 全链路走通 ===")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_run(args) -> int:
    targets = args.targets
    log("驱动", f"本轮 {len(targets)} 个项目：{'、'.join(p[0] for p in targets)}")
    threads = []
    for index, (name, pid, eid) in enumerate(targets):
        thread = threading.Thread(
            target=drive_project, args=(name, pid, eid, index * STAGGER_S),
            name=f"drive-{name}", daemon=False,
        )
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join()
    log("驱动", f"=== {len(targets)} 个项目全部到达终态 ===")
    return cmd_report(args)


def cmd_status(args) -> int:
    for name, pid, eid in args.targets:
        proj = project_status(pid)
        sp = screenplay_status(eid)
        sb = storyboard_status(eid)
        vd = video_status(eid)
        coverage = vd.get("coverage") or {}
        chars = (proj.get("bible") or {}).get("characters") or []
        scenes = (proj.get("bible") or {}).get("scenes") or []
        print(
            f"{name:6s} 人物谱={proj.get('bible_status')}({len(chars)}) "
            f"定妆={proj.get('refs_status')}({len([c for c in chars if c.get('ref_image_path')])}) "
            f"场景={proj.get('scene_refs_status')}({len([s for s in scenes if s.get('ref_image_path')])}/{len(scenes)}) "
            f"映射台={sp.get('screenplay_status')} "
            f"分镜台={sb.get('state')}({sb.get('produced_shots')}镜) "
            f"生成台={vd.get('user_state')} 采纳={coverage.get('adopted')}/{coverage.get('total')}"
        )
    return 0


def load_merged_state() -> dict:
    """合并主进程与各补跑进程的记账。同一项目以更晚写入的那份为准——补跑总是
    发生在主进程判失败之后，晚的那份就是最新事实。"""
    merged: dict = {}
    for path in sorted((ROOT / "logs").glob("ep1_all_state*.json")):
        try:
            chunk = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for project, entry in chunk.items():
            current = merged.get(project)
            if current is None:
                merged[project] = entry
                continue
            latest = max((s.get("at") or "") for s in (entry.get("stages") or {}).values() or [{}])
            kept = max((s.get("at") or "") for s in (current.get("stages") or {}).values() or [{}])
            if latest >= kept:
                merged[project] = entry
    return merged


def cmd_report(args) -> int:
    state = load_merged_state()
    print("\n" + "=" * 78)
    print(f"{len(args.targets)} 个项目第一集全链路结果")
    print("=" * 78)
    rc = 0
    for name, _pid, _eid in args.targets:
        entry = state.get(name) or {}
        stages = entry.get("stages") or {}
        done = bool(entry.get("done"))
        mark = "成功" if done else "失败"
        if not done:
            rc = 1
        print(f"\n{name} —— {mark}")
        for stage in STAGES:
            info = stages.get(stage)
            if not info:
                print(f"  {STAGE_LABELS[stage]:6s} 未执行")
                continue
            flag = "OK  " if info["ok"] else "FAIL"
            print(f"  {STAGE_LABELS[stage]:6s} {flag} {info['elapsed_min']:>6.1f}分 {info['detail'][:300]}")
    print("\n" + "=" * 78)
    return rc


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, func in (("run", cmd_run), ("status", cmd_status), ("report", cmd_report)):
        subparser = sub.add_parser(name)
        subparser.add_argument(
            "--projects", nargs="+", default=None,
            help="项目名或 id 列表（必填，无默认值——历史硬编码的四个项目已随重建失效）",
        )
        subparser.set_defaults(func=func)
    args = parser.parse_args()
    if not args.projects:
        print(
            "用法：py scripts/run_ep1_all_projects.py <run|status|report> "
            "--projects <项目名或 id> [<项目名或 id> ...]\n"
            "缺少 --projects：历史硬编码的四个项目已随重建失效，必须显式指定目标项目。",
            file=sys.stderr,
        )
        return 2
    args.targets = resolve_projects(args.projects)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
