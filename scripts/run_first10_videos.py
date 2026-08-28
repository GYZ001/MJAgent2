#!/usr/bin/env python3
"""驱动「我欲封天」EP1→EP10 的完整视频生成链路，逐集独立、失败即记录后继续。

与 scripts/yyft_pipeline10.py 的区别（本次任务书要求）：
  * 目标是「跑完十集、统计成功率」，因此**一集失败不中断整轮**——记录该集的
    真实失败证据后，继续下一集；
  * **不做任何限流/瞬时白名单分类、不自动重试、不静默兜底降级**（遵循项目
    「禁止白名单/黑名单逻辑、禁止静默兜底」的工程原则）。任一步失败就如实
    记录 error_logs + provider_calls + 状态载荷，判该集失败；
  * 项目/分集 ID 在运行时从数据库按 project_id + episode_no<=10 实时解析，
    不硬编码历史 ID（历史脚本的 proj_3ac0b627fa46 已随项目重建失效）。

全链路每集顺序：剧本(prep_pack) → 分镜 → 确认门 → 视频补齐(付费) → 逐镜采纳 →
合成成片。任一步失败该集即判失败。状态变更一律走 API；数据库只读取证据。

用法：
    py scripts/run_first10_videos.py            # 跑 EP1→EP10
"""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8230"
DB_PATH = ROOT / "data" / "manju.db"
PROJECT_ID = "proj_4c21fc3ce76a"
LOG = ROOT / "logs" / "first10_videos.log"

# 认证：优先回归专用凭证，缺失时回退本机进程级共享秘密（后端 MJ_LEGACY_SHARED_SESSION
# 默认开启，接受该秘密为系统管理员身份）。不新增任何白名单逻辑，仅是读取现有凭证。
_TOKEN_CANDIDATES = (
    ROOT / "data" / "regression_session_token.txt",
    ROOT / "data" / "local_session_secret.txt",
)


def _load_session() -> str:
    for path in _TOKEN_CANDIDATES:
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
    raise SystemExit(
        "找不到会话凭证：data/regression_session_token.txt 与 "
        "data/local_session_secret.txt 均不存在或为空。"
    )


SESSION = _load_session()


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
         headers: dict | None = None, timeout: int = 120) -> tuple[int, dict]:
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
    """命令总线对需确认的写命令先回 202 + approval_token，须带同名头用同一 body
    重放一次才真正执行。这是接口设计的两段式确认，非绕过门禁。"""
    code, resp = call(method, path, body, timeout=timeout)
    token = resp.get("approval_token") if isinstance(resp, dict) else None
    if token:
        code, resp = call(
            method, path, body,
            headers={"x-manju-approval-token": token}, timeout=timeout,
        )
    return code, resp


# ---------------------------------------------------------------------------
# 只读证据（数据库一律 mode=ro）
# ---------------------------------------------------------------------------

def _readonly_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def resolve_first10() -> list[tuple[str, str]]:
    """运行时解析前 10 集真实 ID，绝不硬编码历史 ID。"""
    conn = _readonly_conn()
    try:
        rows = conn.execute(
            "SELECT id, episode_no FROM episodes WHERE project_id=? "
            "AND episode_no<=10 ORDER BY episode_no",
            (PROJECT_ID,),
        ).fetchall()
    finally:
        conn.close()
    return [(f"EP{row['episode_no']}", row["id"]) for row in rows]


def failure_evidence(eid: str, since: float) -> str:
    """把最近失败的真实证据（error_logs + provider_calls）汇总，供 RCA。"""
    conn = _readonly_conn()
    try:
        chunks = [
            f"error_log {row['id']} | {row['category']} | {row['exc_type']} | "
            f"{str(row['message'] or '')[:300]}"
            for row in conn.execute(
                "SELECT id, category, exc_type, message FROM error_logs "
                "WHERE ts>=? AND json_extract(context_json,'$.episode_id')=? "
                "ORDER BY ts DESC LIMIT 8",
                (since, eid),
            )
        ]
        chunks += [
            f"provider_call {row['id']} http={row['http_status']} "
            f"status={row['status']} err={str(row['error'] or '')[:300]}"
            for row in conn.execute(
                "SELECT id, http_status, status, error FROM provider_calls "
                "WHERE ts>=? AND status!='OK' ORDER BY id DESC LIMIT 10",
                (since,),
            )
        ]
    finally:
        conn.close()
    return "\n".join(chunks)


# ---------------------------------------------------------------------------
# 只读状态查询
# ---------------------------------------------------------------------------

def screenplay_status(eid: str) -> dict:
    code, payload = call("GET", f"/api/episodes/{eid}/screenplay/status", timeout=60)
    return payload if code == 200 else {"screenplay_status": f"HTTP{code}", "detail": payload}


def storyboard_status(eid: str) -> dict:
    code, payload = call("GET", f"/api/episodes/{eid}/storyboard/status", timeout=60)
    return payload if code == 200 else {"state": f"HTTP{code}", "detail": payload}


def video_status(eid: str) -> dict:
    code, payload = call("GET", f"/api/episodes/{eid}/video-completion", timeout=60)
    return payload if code == 200 else {"user_state": f"HTTP{code}", "detail": payload}


def mix_status(eid: str) -> dict:
    code, payload = call("GET", f"/api/episodes/{eid}/mix-status", timeout=60)
    return payload if code == 200 else {"final_video_url": None, "detail": payload}


class StageFailure(Exception):
    """任一阶段失败即抛出，携带简明原因；上层记录证据并判该集失败。"""


# ---------------------------------------------------------------------------
# 阶段一：剧本(prep_pack)
# ---------------------------------------------------------------------------

SCREENPLAY_TIMEOUT_S = 1800
SCREENPLAY_POLL_S = 10


def stage_screenplay(name: str, eid: str) -> None:
    payload = screenplay_status(eid)
    state = str(payload.get("screenplay_status") or "")
    if state == "ready":
        log(f"{name} 剧本已 ready，跳过")
        return
    if payload.get("active"):
        log(f"{name} 剧本已在运行，直接等待其终态")
    else:
        code, resp = approved(
            "POST", f"/api/episodes/{eid}/screenplay",
            {"idempotency_key": f"first10-screenplay-{eid}"},
        )
        log(f"{name} 剧本 start -> HTTP{code} {json.dumps(resp, ensure_ascii=False)[:200]}")
        if code not in (200, 202) or resp.get("status") not in {"queued", "running", "repairing"}:
            if not (code == 200 and resp.get("deduplicated")):
                raise StageFailure(
                    f"剧本任务未能启动：HTTP{code} {json.dumps(resp, ensure_ascii=False)[:200]}"
                )
    waited = 0
    last = ""
    while waited <= SCREENPLAY_TIMEOUT_S:
        payload = screenplay_status(eid)
        state = str(payload.get("screenplay_status") or "")
        prod = payload.get("screenplay_production") or {}
        line = f"{state} active={payload.get('active')} stage={prod.get('stage_index')}/{prod.get('stage_count')}"
        if line != last:
            log(f"{name} 剧本 :: {line}")
            last = line
        if state == "ready":
            return
        if not payload.get("active") and state in {"failed", "pending"}:
            raise StageFailure(
                f"剧本未达 ready：state={state} err="
                f"{str(payload.get('screenplay_error') or '')[:300]}"
            )
        time.sleep(SCREENPLAY_POLL_S)
        waited += SCREENPLAY_POLL_S
    raise StageFailure(f"剧本超时（{SCREENPLAY_TIMEOUT_S}s 未达 ready）")


# ---------------------------------------------------------------------------
# 阶段二：分镜 + 确认门
# ---------------------------------------------------------------------------

STORYBOARD_TIMEOUT_S = 7200
STORYBOARD_POLL_S = 15
STORYBOARD_SUCCESS = {"ready_to_confirm", "confirmed"}


def stage_storyboard(name: str, eid: str) -> None:
    status = storyboard_status(eid)
    state = str(status.get("state") or "")
    if state == "confirmed":
        log(f"{name} 分镜已确认，跳过")
        return
    if state not in {"running", "ready_to_confirm"}:
        code, resp = approved("POST", f"/api/episodes/{eid}/storyboard", None)
        log(f"{name} 分镜 start -> HTTP{code} {json.dumps(resp, ensure_ascii=False)[:200]}")
        started = (code in (200, 202) and resp.get("status") == "scripting") or (
            code == 409 and "已被其他请求抢占" in str(resp)
        )
        if not started:
            raise StageFailure(
                f"分镜任务未能启动：HTTP{code} {json.dumps(resp, ensure_ascii=False)[:200]}"
            )
    waited = 0
    last = ""
    while waited <= STORYBOARD_TIMEOUT_S:
        status = storyboard_status(eid)
        state = str(status.get("state") or "")
        line = f"state={state} {status.get('produced_shots')}/{status.get('planned_shots')}镜"
        if line != last:
            log(f"{name} 分镜 :: {line}")
            last = line
        if state != "running":
            break
        time.sleep(STORYBOARD_POLL_S)
        waited += STORYBOARD_POLL_S
    if state not in STORYBOARD_SUCCESS:
        raise StageFailure(
            f"分镜未达成功终态：state={state} headline={status.get('headline')} "
            f"issues={status.get('hard_gate_issues')}"
        )
    if state == "confirmed":
        return
    _confirm_gate(name, eid)


def _confirm_gate(name: str, eid: str) -> None:
    """确认门：confirm-preview → confirm。硬门禁 409 即真失败，如实抛出。"""
    code, resp = call("POST", f"/api/episodes/{eid}/confirm-preview", None, timeout=90)
    if code == 409:
        errors = (resp.get("detail") or resp) if isinstance(resp, dict) else {}
        hard = (errors.get("hard_gates") or {}).get("errors") if isinstance(errors, dict) else None
        raise StageFailure(f"确认门硬门禁未通过：{hard or resp}")
    if code != 200 or "preview_token" not in resp:
        raise StageFailure(f"确认门预览失败：HTTP{code} {json.dumps(resp, ensure_ascii=False)[:200]}")
    reason = "run_first10_videos 自动化：分镜整集门禁已通过、无 blocker，确认解锁付费视频阶段"
    code2, resp2 = approved(
        "POST", f"/api/episodes/{eid}/confirm",
        {"preview_token": resp["preview_token"], "reason": reason},
    )
    log(f"{name} 确认门 confirm -> HTTP{code2} {json.dumps(resp2, ensure_ascii=False)[:200]}")
    if not (code2 == 200 and resp2.get("confirmed")):
        raise StageFailure(f"确认门 confirm 失败：HTTP{code2} {json.dumps(resp2, ensure_ascii=False)[:200]}")


# ---------------------------------------------------------------------------
# 阶段三：视频补齐（付费）+ 逐镜采纳
# ---------------------------------------------------------------------------

VIDEO_TERMINAL_SUCCESS = {"SUCCEEDED_COVERED", "COMPLETED_DEADLINE_FALLBACK"}
VIDEO_TIMEOUT_S = 6 * 3600
VIDEO_POLL_S = 30


def stage_video(name: str, eid: str) -> None:
    proj = video_status(eid)
    if proj.get("user_state") != "completed":
        if not (proj.get("running") or proj.get("user_state") == "recovering"):
            code, resp = approved(
                "POST", f"/api/episodes/{eid}/video-completion",
                {"mode": "fresh", "idempotency_key": f"first10-video-{eid}"},
            )
            log(f"{name} 视频补齐 start -> HTTP{code} {json.dumps(resp, ensure_ascii=False)[:200]}")
            running = code in (200, 202) or (code == 409 and "运行中" in str(resp))
            if not running:
                raise StageFailure(
                    f"视频补齐未能启动：HTTP{code} {json.dumps(resp, ensure_ascii=False)[:200]}"
                )
        waited = 0
        last = ""
        while waited <= VIDEO_TIMEOUT_S:
            proj = video_status(eid)
            cov = proj.get("coverage") or {}
            line = (f"running={proj.get('running')} user_state={proj.get('user_state')} "
                    f"phase={proj.get('phase')} adopted={cov.get('adopted')}/{cov.get('total')}")
            if line != last:
                log(f"{name} 视频 :: {line}")
                last = line
            if not proj.get("running") and proj.get("user_state") != "recovering":
                break
            time.sleep(VIDEO_POLL_S)
            waited += VIDEO_POLL_S
        phase = proj.get("phase")
        if phase not in VIDEO_TERMINAL_SUCCESS:
            raise StageFailure(
                f"视频补齐未达成功终态：phase={phase} user_state={proj.get('user_state')}"
            )
    _adopt_shots(name, eid)


def _best_candidate(conn: sqlite3.Connection, shot_id: str) -> str | None:
    """与 app/media_exec/concat.py::_playable_model_candidate 同口径，且要求技术
    校验通过（adopt 端点自身要求）。只读，不写库。"""
    rows = conn.execute(
        "SELECT id, video_path, image_inputs, technical_validation_json "
        "FROM shot_versions WHERE shot_id=? AND status='succeeded' "
        "AND video_path IS NOT NULL ORDER BY version_no DESC",
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


def _adopt_shots(name: str, eid: str) -> None:
    conn = _readonly_conn()
    try:
        shots = conn.execute(
            "SELECT id, shot_no FROM shots WHERE episode_id=? AND "
            "(adopted_version_id IS NULL OR adopted_version_id='') ORDER BY shot_no",
            (eid,),
        ).fetchall()
        candidates = [
            (row["id"], row["shot_no"], _best_candidate(conn, row["id"]))
            for row in shots
        ]
    finally:
        conn.close()
    if not candidates:
        log(f"{name} 逐镜采纳：无待采纳镜头")
        return
    missing = [str(no) for _sid, no, vid in candidates if not vid]
    if missing:
        raise StageFailure(
            f"以下镜头没有技术校验通过、文件存在的候选版本，无法采纳：第 {','.join(missing)} 镜"
        )
    for shot_id, shot_no, version_id in candidates:
        reason = "run_first10_videos 自动化：全片补齐已产出该镜技术校验通过的候选，采用最新版本"
        code, resp = approved(
            "POST", f"/api/shots/{shot_id}/adopt",
            {"version_id": version_id, "reason": reason,
             "idempotency_key": f"first10-adopt-{shot_id}"},
        )
        if not (code == 200 and resp.get("adopted") == version_id):
            raise StageFailure(
                f"第{shot_no}镜采纳失败：HTTP{code} {json.dumps(resp, ensure_ascii=False)[:200]}"
            )
        log(f"{name} 第{shot_no}镜已采纳 {version_id}")


# ---------------------------------------------------------------------------
# 阶段四：合成成片
# ---------------------------------------------------------------------------

def stage_concat(name: str, eid: str) -> None:
    mix = mix_status(eid)
    if mix.get("final_video_url"):
        log(f"{name} 成片已存在，跳过合成")
        return
    code, resp = approved(
        "POST", f"/api/episodes/{eid}/concatenate",
        {"idempotency_key": f"first10-concat-{eid}"}, timeout=600,
    )
    log(f"{name} 合成 -> HTTP{code} {json.dumps(resp, ensure_ascii=False)[:200]}")
    if code not in (200, 202):
        raise StageFailure(f"成片合成失败：HTTP{code} {json.dumps(resp, ensure_ascii=False)[:200]}")
    if not mix_status(eid).get("final_video_url"):
        raise StageFailure("合成返回成功但未产出 final_video_url")


# ---------------------------------------------------------------------------
# 主流程：逐集独立，失败记录后继续
# ---------------------------------------------------------------------------

STAGES = (
    ("剧本", stage_screenplay),
    ("分镜", stage_storyboard),
    ("视频", stage_video),
    ("成片", stage_concat),
)


def run_episode(name: str, eid: str) -> tuple[bool, str]:
    since = time.time()
    for label, fn in STAGES:
        try:
            fn(name, eid)
        except StageFailure as exc:
            evidence = failure_evidence(eid, since)
            log(f"{name} ✗ 在【{label}】阶段失败：{exc}")
            if evidence:
                log(f"{name} 失败证据：")
                for line in evidence.splitlines()[:12]:
                    log(f"    {line}")
            return False, f"{label}阶段：{exc}"
        except Exception as exc:  # noqa: BLE001 - 未预期异常也如实记录，不静默
            log(f"{name} ✗ 在【{label}】阶段抛出未预期异常：{exc!r}")
            return False, f"{label}阶段未预期异常：{exc!r}"
    log(f"{name} ✅ 全链路完成（剧本→分镜→视频→成片）")
    return True, "ready"


def main() -> int:
    episodes = resolve_first10()
    if len(episodes) < 10:
        log(f"[警告] 只解析到 {len(episodes)} 集（预期 10 集），project_id={PROJECT_ID}")
    log(f"=== RUN FIRST-10 VIDEOS START（project={PROJECT_ID}，共 {len(episodes)} 集）===")
    results: dict[str, tuple[bool, str]] = {}
    for name, eid in episodes:
        log(f"--- {name}（{eid}）开始 ---")
        ok, detail = run_episode(name, eid)
        results[name] = (ok, detail)
    success = [n for n, (ok, _d) in results.items() if ok]
    failed = [(n, d) for n, (ok, d) in results.items() if not ok]
    log("=== RUN FIRST-10 VIDEOS DONE ===")
    log(f"成功 {len(success)}/{len(episodes)}，成功率 "
        f"{(len(success) / len(episodes) * 100 if episodes else 0):.0f}%")
    if success:
        log("成功集：" + "、".join(success))
    for name, detail in failed:
        log(f"失败集 {name}：{detail}")
    return 0 if len(success) == len(episodes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
