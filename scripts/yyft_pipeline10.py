#!/usr/bin/env python3
"""「我欲封天」EP1→EP10 严格串行「分镜→视频→成片→交付」全链路驱动。

与 `scripts/yyft_serial10.py`（剧本串行驱动）同一套工程纪律，本脚本接着剧本
之后跑完剩余四步。两者是同一条流水线的前后段，常量（BASE/SESSION/PROJECT_ID/
EPISODES）直接复用，互不修改对方文件。

用法:
    py scripts/yyft_pipeline10.py status
    py scripts/yyft_pipeline10.py run                       # EP1→EP10 串行跑完 6 步
    py scripts/yyft_pipeline10.py run --from EP4             # 从指定集续跑
    py scripts/yyft_pipeline10.py run --stage video           # 只跑 1→10 集的视频阶段
    py scripts/yyft_pipeline10.py clear --stage storyboard    # 只清分镜模块 1-10 集
    py scripts/yyft_pipeline10.py clear --stage video         # 只清视频模块 1-10 集
    py scripts/yyft_pipeline10.py verify                      # 用项目自身判据验收

设计约束（与任务书一致）：
  * 严格串行，任何时刻只有一集在跑，一个阶段跑完全部 10 集才轮到下一阶段
    （`run` 默认按集贯穿六步；`run --stage` 按阶段贯穿 10 集，用于定向回归）；
  * 非限流失败**立即停止整轮**，不自动重试、不自动跳过，留给人做根因分析；
  * 只有明确的供应商限流（`provider_calls.http_status=429` 或白名单文案）才自动
    等待重试，裸 "429"/"tpm" 不放进白名单，避免把 ERR-20260822-4295ab 这类错误码
    误判成限流；
  * 三道人工确认门（分镜确认、交付打包、交付批准）按接口设计正常调用并填写真实
    确认理由，不是绕过门禁——门禁给出 blocker 就是真失败，必须停下；
  * 数据库一律只读（`mode=ro`），任何状态变更都走 API，不直接写库；
  * 只操作本项目这 10 集。
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
LOG = ROOT / "logs" / "pipeline10.log"
DB_PATH = ROOT / "data" / "manju.db"

# 只有这些才算供应商限流。普通 timeout / 连接错误 / JSON / schema / 模型输出异常
# 一律不算，必须停下来做根因分析。
#
# 刻意不放裸 "429"/"tpm"/"rpm"：错误码形如 ERR-20260822-4295ab，裸数字与两字母
# 缩写会在无关文本里假阳性，把一个真实缺陷误判成限流并自动等待——那正是任务书
# 禁止的。HTTP 429 由 provider_calls.http_status 这一**结构化**字段单独判定。
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

STAGES = ("storyboard", "video", "delivery")


def log(msg: str) -> None:
    line = f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}"
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


def approved(method: str, path: str, body: dict | None = None,
             timeout: int = 180) -> tuple[int, dict]:
    """命令总线对 confirmation=ALWAYS/WHEN_IMPACT 的写命令会先回 202 + approval_token
    （`app/capabilities/dispatch.py::respond_ui`），须带 `X-Manju-Approval-Token`
    头用同一份 body 重放一次才会真正执行（`app/capabilities/policy.py`）。
    这与业务侧各自的 preview_token 机制（分镜确认等）是两层独立的门，互不替代。
    """
    code, resp = call(method, path, body, timeout=timeout)
    token = resp.get("approval_token") if isinstance(resp, dict) else None
    if token:
        code, resp = call(
            method, path, body,
            headers={"x-manju-approval-token": token}, timeout=timeout,
        )
    return code, resp


# ---------------------------------------------------------------------------
# 只读证据（数据库一律只读，状态变更一律走 API）
# ---------------------------------------------------------------------------

def _readonly_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def recent_failure_evidence(eid: str, since: float) -> str:
    """把最近失败的真实证据（error_logs + provider_calls）打进日志，供人做 RCA。"""
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
    """遵循供应商声明的 Retry-After，没有就交给调用方用保守默认值。"""
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
    """只有结构化 429 或保守白名单文案才算限流；其余一律要人做 RCA。"""
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


def stop_for_rca(name: str, eid: str, since: float, reason: str) -> None:
    log(f"{name} 非限流失败 —— 停止整轮，等待根因分析。原因：{reason}")
    evidence = recent_failure_evidence(eid, since)
    if evidence:
        log(f"{name} 证据：")
        for line in evidence.splitlines()[:10]:
            log(f"    {line}")


# ---------------------------------------------------------------------------
# 只读状态查询（GET，供 status / verify / 轮询复用）
# ---------------------------------------------------------------------------

def get_storyboard_status(eid: str) -> dict:
    code, payload = call("GET", f"/api/episodes/{eid}/storyboard/status", timeout=60)
    if code != 200:
        return {"state": f"HTTP{code}", "detail": payload}
    return payload


def get_video_status(eid: str) -> dict:
    code, payload = call("GET", f"/api/episodes/{eid}/video-completion", timeout=60)
    if code != 200:
        return {"user_state": f"HTTP{code}", "detail": payload}
    return payload


def get_delivery_readiness(eid: str) -> dict:
    code, payload = call("GET", f"/api/episodes/{eid}/delivery/readiness", timeout=60)
    if code != 200:
        return {"ready": False, "blockers": [{"message": f"HTTP{code}: {payload}"}]}
    return payload


def get_mix_status(eid: str) -> dict:
    code, payload = call("GET", f"/api/episodes/{eid}/mix-status", timeout=60)
    if code != 200:
        return {"ready": False, "all_ready": False, "final_video_url": None, "detail": payload}
    return payload


def get_delivery_packages(eid: str) -> list[dict]:
    code, payload = call("GET", f"/api/episodes/{eid}/delivery/packages", timeout=60)
    if code != 200 or not isinstance(payload, list):
        return []
    return payload


def status_brief(name: str, eid: str) -> str:
    sb = get_storyboard_status(eid)
    vd = get_video_status(eid)
    dl = get_delivery_readiness(eid)
    mx = get_mix_status(eid)
    return (
        f"{name} {eid} :: storyboard={sb.get('state')} "
        f"({sb.get('produced_shots')}/{sb.get('planned_shots')}镜) | "
        f"video={vd.get('user_state')} adopted={((vd.get('coverage') or {}).get('adopted'))}"
        f"/{((vd.get('coverage') or {}).get('total'))} | "
        f"delivery_ready={dl.get('ready')} blockers={len(dl.get('blockers') or [])} | "
        f"final_video={'有' if mx.get('final_video_url') else '无'}"
    )


# ---------------------------------------------------------------------------
# 阶段一：分镜（含确认门 A）
# ---------------------------------------------------------------------------

STORYBOARD_RUNNING_STATE = "running"
STORYBOARD_SUCCESS_STATES = {"ready_to_confirm", "confirmed"}
STORYBOARD_TIMEOUT_S = 7200      # 分镜是文本生成，量级：分钟到小时
STORYBOARD_POLL_S = 15


def _start_or_confirm_storyboard(name: str, eid: str) -> bool:
    """POST /storyboard 不带 body（`body_was_explicit=False`）以跳过
    `preflight_token` 强制要求——`app/domain/storyboard_ops.py:2359` 的
    `start_storyboard` 只在客户端显式带 body 时才要求预检凭据；空 body 是
    该接口本身支持的正常路径（对应任务书“可先预检”的可选项）。
    """
    code, resp = approved("POST", f"/api/episodes/{eid}/storyboard", None)
    log(f"{name} storyboard start -> HTTP{code} {json.dumps(resp, ensure_ascii=False)[:220]}")
    if code == 200 and resp.get("status") in {"scripting"}:
        return True
    if code == 409 and "已被其他请求抢占" in str(resp):
        return True  # 别的请求已经在跑，交给轮询收敛
    return False


def _poll_storyboard(name: str, eid: str) -> dict:
    waited = 0
    last = ""
    while waited <= STORYBOARD_TIMEOUT_S:
        status = get_storyboard_status(eid)
        line = (
            f"state={status.get('state')} "
            f"{status.get('produced_shots')}/{status.get('planned_shots')}镜 "
            f"validated={status.get('validated_shots')}"
        )
        if line != last:
            log(f"{name} storyboard :: {line}")
            last = line
        if status.get("state") != STORYBOARD_RUNNING_STATE:
            return status
        time.sleep(STORYBOARD_POLL_S)
        waited += STORYBOARD_POLL_S
    log(f"{name} storyboard TIMEOUT after {waited}s")
    return get_storyboard_status(eid)


def _confirm_storyboard_gate(name: str, eid: str) -> bool:
    """确认门 A：POST confirm-preview → POST confirm。

    `confirm-preview`（`app/domain/video_ops.py:765`）硬门禁不过会返回 409，
    detail 里带 `hard_gates.errors`——这是真失败，必须停下，不许强行通过。
    """
    code, resp = call("POST", f"/api/episodes/{eid}/confirm-preview", None, timeout=60)
    if code == 409:
        errors = ((resp.get("detail") or resp) if isinstance(resp, dict) else {})
        hard = (errors.get("hard_gates") or {}).get("errors") if isinstance(errors, dict) else None
        log(f"{name} 确认门 A 被拒绝，硬门禁未通过：{hard or resp}")
        return False
    if code != 200 or "preview_token" not in resp:
        log(f"{name} 确认门 A 预览失败 -> HTTP{code} {resp}")
        return False
    reason = "yyft_pipeline10 自动化回归：分镜整集门禁已通过，无 blocker，确认解锁付费视频阶段"
    code2, resp2 = approved(
        "POST", f"/api/episodes/{eid}/confirm",
        {"preview_token": resp["preview_token"], "reason": reason},
    )
    log(f"{name} 确认门 A confirm -> HTTP{code2} {json.dumps(resp2, ensure_ascii=False)[:200]}")
    return code2 == 200 and bool(resp2.get("confirmed"))


def stage_storyboard(name: str, eid: str) -> bool:
    status = get_storyboard_status(eid)
    state = status.get("state")
    if state == "confirmed":
        log(f"{name} 分镜已确认，跳过")
        return True
    since = time.time()
    if state != STORYBOARD_RUNNING_STATE:
        if state != "ready_to_confirm":
            if not _start_or_confirm_storyboard(name, eid):
                stop_for_rca(name, eid, since, "分镜任务未能启动")
                return False
    status = _poll_storyboard(name, eid)
    state = status.get("state")
    if state not in STORYBOARD_SUCCESS_STATES:
        if is_rate_limited(eid, since):
            delay = provider_retry_after(since) or 1800.0
            log(f"{name} 分镜阶段判定为供应商限流，等待 {int(delay)} 秒后重试本阶段")
            time.sleep(delay)
            return stage_storyboard(name, eid)
        stop_for_rca(
            name, eid, since,
            f"分镜未达成功终态：state={state} headline={status.get('headline')} "
            f"issues={status.get('hard_gate_issues')}",
        )
        return False
    if state == "confirmed":
        return True
    return _confirm_storyboard_gate(name, eid)


# ---------------------------------------------------------------------------
# 阶段二：视频补齐（含逐镜采纳）
# ---------------------------------------------------------------------------

VIDEO_TERMINAL_SUCCESS_PHASES = {"SUCCEEDED_COVERED", "COMPLETED_DEADLINE_FALLBACK"}
VIDEO_TIMEOUT_S = 6 * 3600     # 视频慢：单镜数十秒到数分钟，一集几十镜
VIDEO_POLL_S = 30


def _start_video_completion(name: str, eid: str) -> tuple[bool, bool]:
    """启动集级补齐 Supervisor；返回 (started_or_running, used_fallback)。

    `video-completion` 在本项目里是否总能覆盖所有集尚未验证过；若接口明确
    答复不适用（404），按任务书退回手工 `/generate`。这是唯一允许的分支
    降级，其余任何非 2xx/409-重复 都视为失败。
    """
    idem = f"pipeline10-video-{eid}"
    code, resp = approved(
        "POST", f"/api/episodes/{eid}/video-completion",
        {"mode": "fresh", "idempotency_key": idem},
    )
    log(f"{name} video-completion start -> HTTP{code} {json.dumps(resp, ensure_ascii=False)[:220]}")
    if code in (200, 202):
        return True, False
    if code == 409 and "运行中" in str(resp):
        return True, False
    if code == 404:
        log(f"{name} video-completion 接口不适用，退回 /generate")
        code2, resp2 = approved(
            "POST", f"/api/episodes/{eid}/generate", {"idempotency_key": idem},
        )
        log(f"{name} generate(fallback) -> HTTP{code2} {json.dumps(resp2, ensure_ascii=False)[:220]}")
        return code2 in (200, 202), True
    return False, False


def _poll_video(name: str, eid: str) -> dict:
    waited = 0
    last = ""
    while waited <= VIDEO_TIMEOUT_S:
        proj = get_video_status(eid)
        coverage = proj.get("coverage") or {}
        line = (
            f"running={proj.get('running')} user_state={proj.get('user_state')} "
            f"adopted={coverage.get('adopted')}/{coverage.get('total')} "
            f"phase={(proj.get('phase'))}"
        )
        if line != last:
            log(f"{name} video :: {line}")
            last = line
        if not proj.get("running") and proj.get("user_state") != "recovering":
            return proj
        time.sleep(VIDEO_POLL_S)
        waited += VIDEO_POLL_S
    log(f"{name} video TIMEOUT after {waited}s")
    return get_video_status(eid)


def _best_adopt_candidate(conn: sqlite3.Connection, shot_id: str) -> str | None:
    """复现 `app/media_exec/concat.py::_playable_model_candidate` 的可播候选口径，
    并额外要求技术校验通过（`adopt` 端点自身的硬性要求，
    `app/domain/video_ops.py::_adopt_version_core`）。只读查询，不写库。
    """
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


def _adopt_unadopted_shots(name: str, eid: str) -> bool:
    """逐镜采纳：未自动采纳的调用 `POST /shots/{id}/adopt`。"""
    conn = _readonly_conn()
    try:
        shots = conn.execute(
            "SELECT id, shot_no FROM shots WHERE episode_id=? AND "
            "(adopted_version_id IS NULL OR adopted_version_id='') ORDER BY shot_no",
            (eid,),
        ).fetchall()
        candidates = [
            (row["id"], row["shot_no"], _best_adopt_candidate(conn, row["id"]))
            for row in shots
        ]
    finally:
        conn.close()
    if not candidates:
        log(f"{name} 逐镜采纳：无待采纳镜头")
        return True
    ok = True
    for shot_id, shot_no, version_id in candidates:
        if not version_id:
            log(f"{name} 第{shot_no}镜没有技术校验通过、文件存在的候选版本，无法采纳")
            ok = False
            continue
        reason = "yyft_pipeline10 自动化回归：全片补齐已产出该镜技术校验通过的候选，采用最新版本"
        idem = f"pipeline10-adopt-{shot_id}"
        code, resp = approved(
            "POST", f"/api/shots/{shot_id}/adopt",
            {"version_id": version_id, "reason": reason, "idempotency_key": idem},
        )
        if code == 200 and resp.get("adopted") == version_id:
            log(f"{name} 第{shot_no}镜已采纳 {version_id}")
        else:
            log(f"{name} 第{shot_no}镜采纳失败 -> HTTP{code} {json.dumps(resp, ensure_ascii=False)[:200]}")
            ok = False
    return ok


def stage_video(name: str, eid: str) -> bool:
    proj = get_video_status(eid)
    if proj.get("user_state") == "completed":
        log(f"{name} 视频已补齐，跳过启动")
    else:
        since = time.time()
        if not (proj.get("running") or proj.get("user_state") == "recovering"):
            started, _ = _start_video_completion(name, eid)
            if not started:
                stop_for_rca(name, eid, since, "视频补齐 Supervisor 未能启动")
                return False
        proj = _poll_video(name, eid)
        phase = proj.get("phase")
        if phase not in VIDEO_TERMINAL_SUCCESS_PHASES:
            if is_rate_limited(eid, since):
                delay = provider_retry_after(since) or 1800.0
                log(f"{name} 视频阶段判定为供应商限流，等待 {int(delay)} 秒后重试本阶段")
                time.sleep(delay)
                return stage_video(name, eid)
            stop_for_rca(
                name, eid, since,
                f"视频补齐未达成功终态：phase={phase} user_state={proj.get('user_state')}",
            )
            return False
    return _adopt_unadopted_shots(name, eid)


# ---------------------------------------------------------------------------
# 阶段三：合成 + 交付（含确认门 B/C）
# ---------------------------------------------------------------------------

def _concatenate(name: str, eid: str) -> bool:
    mix = get_mix_status(eid)
    if mix.get("final_video_url"):
        log(f"{name} 成片已存在，跳过合成")
        return True
    idem = f"pipeline10-concat-{eid}"
    code, resp = approved(
        "POST", f"/api/episodes/{eid}/concatenate", {"idempotency_key": idem}, timeout=600,
    )
    log(f"{name} concatenate -> HTTP{code} {json.dumps(resp, ensure_ascii=False)[:200]}")
    if code not in (200, 202):
        return False
    return bool(get_mix_status(eid).get("final_video_url"))


def _delivery_package_and_approve(name: str, eid: str) -> bool:
    """确认门 B（生成交付包）+ 确认门 C（批准交付）。

    `GET delivery/readiness`（`app/orchestration/api.py:1018`）的 `blockers`
    才是权威判据：非空即真失败，必须停下，不许强行打包或批准。
    """
    readiness = get_delivery_readiness(eid)
    if not readiness.get("ready"):
        log(f"{name} 交付未就绪，blockers=" + json.dumps(readiness.get("blockers"), ensure_ascii=False)[:600])
        return False

    packages = get_delivery_packages(eid)
    approved_pkg = next((p for p in packages if p.get("status") == "approved"), None)
    if approved_pkg:
        log(f"{name} 交付包已批准（{approved_pkg.get('id')}），跳过")
        return True

    package_id = None
    waiting = next((p for p in packages if p.get("status") == "waiting_human"), None)
    if waiting:
        package_id = waiting.get("id")
        log(f"{name} 复用已存在的待审交付包 {package_id}")
    else:
        idem_pkg = f"pipeline10-package-{eid}"
        code, resp = approved(
            "POST", f"/api/episodes/{eid}/delivery/package",
            {
                "idempotency_key": idem_pkg,
                "decided_by": "yyft_pipeline10",
                "reason": "yyft_pipeline10 自动化回归：delivery readiness 门禁已全部通过，生成交付候选包",
            },
            timeout=300,
        )
        log(f"{name} 确认门 B package -> HTTP{code} {json.dumps(resp, ensure_ascii=False)[:220]}")
        if code not in (200, 202) or not resp.get("package_id"):
            return False
        package_id = resp["package_id"]

    idem_appr = f"pipeline10-approve-{eid}"
    code2, resp2 = approved(
        "POST", f"/api/episodes/{eid}/delivery/approve",
        {
            "idempotency_key": idem_appr,
            "package_id": package_id,
            "decision": "approve",
            "decided_by": "yyft_pipeline10",
            "reason": "yyft_pipeline10 自动化回归：交付包证据链完整、readiness 全绿，予以批准发布",
        },
        timeout=120,
    )
    log(f"{name} 确认门 C approve -> HTTP{code2} {json.dumps(resp2, ensure_ascii=False)[:220]}")
    return code2 == 200


def stage_delivery(name: str, eid: str) -> bool:
    since = time.time()
    if not _concatenate(name, eid):
        stop_for_rca(name, eid, since, "成片合成失败")
        return False
    if not _delivery_package_and_approve(name, eid):
        stop_for_rca(name, eid, since, "交付打包/批准未通过")
        return False
    return True


STAGE_FUNCS = {
    "storyboard": stage_storyboard,
    "video": stage_video,
    "delivery": stage_delivery,
}


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

def cmd_status(_args) -> int:
    for name, eid in EPISODES:
        log(status_brief(name, eid))
    return 0


def run_episode_all_stages(name: str, eid: str) -> bool:
    for stage in STAGES:
        if not STAGE_FUNCS[stage](name, eid):
            return False
    return True


def cmd_run(args) -> int:
    names = [n for n, _ in EPISODES]
    if args.start_from and args.start_from not in names:
        log(f"未知起始集：{args.start_from}")
        return 2
    start_index = names.index(args.start_from) if args.start_from else 0
    episodes = EPISODES[start_index:]
    if args.stage:
        log(f"=== STAGE RUN [{args.stage}] EP{start_index + 1}-EP{len(EPISODES)} START ===")
        fn = STAGE_FUNCS[args.stage]
        for name, eid in episodes:
            if not fn(name, eid):
                log("=== STAGE RUN STOPPED ===")
                return 4
        log("=== STAGE RUN DONE ===")
        return 0
    log(f"=== PIPELINE RUN EP{start_index + 1}-EP{len(EPISODES)} START ===")
    for name, eid in episodes:
        if not run_episode_all_stages(name, eid):
            log("=== PIPELINE RUN STOPPED ===")
            return 4
        log(f"{name} 全部 6 步完成 ✅")
    log("=== PIPELINE RUN DONE ===")
    return 0


def _clear_storyboard_one(name: str, eid: str) -> bool:
    status = get_storyboard_status(eid)
    if status.get("state") == "running":
        code, resp = call("POST", f"/api/episodes/{eid}/storyboard/cancel", None)
        log(f"{name} clear: cancel -> HTTP{code} {json.dumps(resp, ensure_ascii=False)[:160]}")
        for _ in range(60):
            time.sleep(2)
            if get_storyboard_status(eid).get("state") != "running":
                break
    code, resp = call("POST", f"/api/episodes/{eid}/storyboard/clear-preview", None)
    if code == 409 and "没有可清空" in str(resp):
        log(f"{name} clear: 当前没有分镜数据，跳过")
        return True
    if code != 200 or "preview_token" not in resp:
        log(f"{name} clear: clear-preview 失败 -> HTTP{code} {resp}")
        return False
    code2, resp2 = call(
        "POST", f"/api/episodes/{eid}/storyboard/clear",
        {"preview_token": resp["preview_token"]},
    )
    log(f"{name} clear: clear -> HTTP{code2} {json.dumps(resp2, ensure_ascii=False)[:160]}")
    return code2 == 200


def _clear_video_one(name: str, eid: str) -> bool:
    """清视频模块：用 clear-artifacts（含参考图）做整模块彻底重置。"""
    code, resp = approved("POST", f"/api/episodes/{eid}/clear-artifacts", None, timeout=300)
    log(f"{name} clear video -> HTTP{code} {json.dumps(resp, ensure_ascii=False)[:200]}")
    return code == 200


def cmd_clear(args) -> int:
    fn = {"storyboard": _clear_storyboard_one, "video": _clear_video_one}[args.stage]
    log(f"=== CLEAR [{args.stage}] EP1-EP10 (test project only) ===")
    ok = True
    for name, eid in EPISODES:
        if not fn(name, eid):
            ok = False
    log(f"=== CLEAR [{args.stage}] DONE ok={ok} ===")
    return 0 if ok else 1


def cmd_verify(_args) -> int:
    """验收：只用项目自身已有判据，不另立标准。

    逐集检查：
      * 分镜已发布确认（`storyboard/status` 的 `state=='confirmed'`）；
      * 每镜都有已采用且技术校验通过的视频（`mix-status` 的 `all_ready`，
        与 `app/media_exec/concat.py::episode_mix_status` 同一套口径）；
      * 成片文件存在（`mix-status.final_video_url`）；
      * 交付包状态为 `approved`（`delivery/packages`）；
      * 生成台资格（`app/domain/review_wall.py:673` 的
        `_review_upstream_snapshot`/`eligible_for_production`）。
    """
    import sys as _sys
    _sys.path.insert(0, str(ROOT))
    from app.domain.review_wall import _review_upstream_snapshot

    log("=== VERIFY EP1-EP10 ===")
    ok = True
    for name, eid in EPISODES:
        problems: list[str] = []
        sb = get_storyboard_status(eid)
        if sb.get("state") != "confirmed":
            problems.append(f"分镜未确认：state={sb.get('state')}")
        mix = get_mix_status(eid)
        if not mix.get("all_ready"):
            problems.append(
                f"并非每镜都有已采用且通过技术校验的视频："
                f"shots_ready={mix.get('shots_ready')}/{mix.get('shots_total')}"
            )
        if not mix.get("final_video_url"):
            problems.append("成片文件不存在")
        packages = get_delivery_packages(eid)
        approved_pkg = next((p for p in packages if p.get("status") == "approved"), None)
        if not approved_pkg:
            latest = packages[0]["status"] if packages else "无交付包"
            problems.append(f"交付包未批准：当前状态={latest}")
        try:
            snapshot = _review_upstream_snapshot(eid)
            if not snapshot.get("eligible_for_production"):
                problems.append(
                    "生成台资格未通过：" + "；".join(snapshot.get("blockers") or [])
                )
        except Exception as exc:  # noqa: BLE001 - 验收即要看到真实原因
            problems.append(f"生成台资格查询失败：{exc}")
        if problems:
            ok = False
            log(f"{name} ✗ " + "；".join(problems))
        else:
            log(f"{name} ✓ 分镜已确认 / 视频全采纳 / 成片存在 / 交付已批准 / 生成台资格通过")
    log(f"=== VERIFY {'PASSED' if ok else 'FAILED'} ===")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status")

    p_run = sub.add_parser("run")
    p_run.add_argument("--from", dest="start_from", default="")
    p_run.add_argument("--stage", choices=STAGES, default="")

    p_clear = sub.add_parser("clear")
    p_clear.add_argument("--stage", choices=("storyboard", "video"), required=True)

    sub.add_parser("verify")

    args = parser.parse_args()
    return {
        "status": cmd_status, "run": cmd_run,
        "clear": cmd_clear, "verify": cmd_verify,
    }[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
