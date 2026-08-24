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
  * GEN-RETRY-GRANT（error_logs.category=='generation_retry_grant'）不再自动
    恢复——2026-08-24 剧本台改造为轻量 episode_prep_pack 流程后，这条路径在当前
    后端下已结构性不可达（证据与判断见 is_retry_grant_category() 的 docstring），
    命中即代表异常，只标注、不自动重试，仍按非限流失败停下；
  * 只清理本项目这 10 集的剧本数据，绝不触碰其它项目或分集。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8230"
# 2026-08-24 起改用回归专用会话凭证（data/regression_session_token.txt），不再
# 复用开发者本机的 local_session_secret.txt；读取方式不变：整行 .strip() 后原样
# 当 X-Manju-Session 头发出。已用新旧两个 token 分别对只读端点
# （GET /api/system/jobs、/screenplay/status）验证过 200，写操作（含审批）的
# 验证按约定留给正式回归的 clear 步骤。
SESSION = (ROOT / "data" / "regression_session_token.txt").read_text(encoding="utf-8").strip()
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

# GEN-RETRY-GRANT（app/errors.py CATEGORIES["generation_retry_grant"]）曾是唯一
# 一类有客观结构化证据、且领域层设计了全自动安全恢复路径的失败——但那条恢复路径
# 是给旧的重型「蓝图→场次分片→编译→修复回路」管线设计的，2026-08-24 剧本台改造
# 为轻量 episode_prep_pack 流程后，在当前后端下已经结构性不可达，证据见
# is_retry_grant_category() 的 docstring。因此本文件不再对它做自动恢复，只保留
# 检测：命中就说明后端行为发生了漂移（或该集残留着改造前的旧状态），需要人工看，
# 不能假装脚本还在"安全自愈"一个已经走不到的分支。


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


def is_retry_grant_category(
    eid: str, since: float, *, db_path: Path | None = None,
) -> bool:
    """Whether the last failure carries error_logs.category='generation_retry_grant'.

    Structured evidence only: ``category`` is the classifier's own output
    (app/errors.py:classify), not a text guess. Scoped to this episode's
    ``context_json.episode_id`` and to calls at/after ``since`` so a stale,
    already-superseded error from an earlier attempt -- or a different
    episode's/another session's unrelated interrupted call sharing the same
    time window -- can never be misread as this attempt's outcome.

    Detection only, no automatic recovery (see 2026-08-24 change). The old
    auto-heal loop replayed the two-step approval on POST /screenplay so
    app/domain/screenplay_ops.py's ``_spawn_screenplay_activation`` could mint
    a fresh Production Grant and resume the interrupted *heavy blueprint*
    stage. That gate is scoped exclusively to
    ``provider_calls.meta.stage_key IN ('screenplay_blueprint_shard',
    'screenplay_blueprint_patch', 'screenplay_blueprint_review')``
    (app/stages.py:_BlueprintGenerationBudget.from_durable_calls, query at
    app/stages.py:7372-7374; the gate itself at app/stages.py:7561-7567's
    ``requires_fresh_retry_grant``). The current episode_prep_pack pipeline
    (app/production/prep_pack.py) never writes a provider_calls row with any
    of those three stage_keys and never raises
    ``BLUEPRINT_PROVIDER_RETRY_GRANT_REQUIRED`` -- confirmed by reading the
    whole file, not by absence of a grep hit alone. Any legacy INTERRUPTED
    receipt left over from before the 2026-08-24 transform is also
    unconditionally abandoned by every DELETE /screenplay
    (screenplay_ops.py:2873, ``_abandon_orphaned_blueprint_receipts``), which
    ``clear_one`` below always calls before a fresh run. Verified empirically
    against this project's live data on 2026-08-24: calling
    ``_screenplay_blueprint_budget_projection`` for all 10 EPISODES returned
    ``requires_fresh_retry_grant=False`` and ``revision=None`` for every one,
    even though four of them (EP1/EP3/EP4/EP7) still carry old INTERRUPTED
    ``screenplay_blueprint_shard``/``screenplay_blueprint_review`` rows in
    provider_calls -- all already settled via
    ``recovery_disposition='ABANDONED_BY_SCREENPLAY_DELETE'`` or
    ``'RETRIED_SUCCESSFULLY'``/``'RETRY_STARTED'`` with a superseding call.
    So on the current backend this cannot fire from real prep_pack activity;
    if it ever does, that is itself the anomaly worth a human look, not
    something to paper over with a retry loop.
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


def await_terminal(name: str, eid: str, interval: int = 10, limit: int = 1800) -> dict:
    """Poll until the episode reaches a terminal state.

    interval/limit 重定于 2026-08-24（旧值 30s/7200s 是给单集 29-51 分钟、
    55-83 次模型调用的重型蓝图管线设计的）。轻量 episode_prep_pack 实测单集
    80-263s；limit=1800s(30min) 给含新角色发现的集（身份判定 + 定妆照/场景
    参考图生成都是额外的真实模型调用，耗时可观）留足余量——约为已知最长实测
    值的 7 倍——但不再按小时计。interval 收紧到 10s 以便更快看到状态变化。
    """
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
            if is_retry_grant_category(eid, since):
                log(f"{name} 命中 GEN-RETRY-GRANT"
                    "（error_logs.category='generation_retry_grant'）。"
                    "在当前 episode_prep_pack-only 后端下这理论上结构性不可达"
                    "（证据见 is_retry_grant_category() docstring）——出现即代表"
                    "后端行为已漂移，或该集仍绑定着改造前的旧管线残留状态，"
                    "不再自动重试，按非限流失败停下等人工核实。")
            log(f"{name} 非限流失败 —— 停止整轮，等待根因分析。证据：")
            for line in recent_failure_evidence(eid, since).splitlines()[:10]:
                log(f"    {line}")
            results[name] = state or "failed"
            log("=== SERIAL RUN STOPPED ===")
            log(json.dumps(results, ensure_ascii=False))
            return 4
    log("=== SERIAL RUN DONE === " + json.dumps(results, ensure_ascii=False))
    return 0 if all(value == "ready" for value in results.values()) else 1


EXPECTED_SCREENPLAY_CONTRACT_VERSION = "6.0.0"
# 版本期望值直接取生成器的单一真源，不再手抄数字。教训（独立 Code Review 抓获）：
# 此处曾手写 "1.2.0"，随后生成器升到 1.3.0（functional_extras），两处各自维护导致
# verify 会把一切合规产物误判为不合格——典型 E 类"重复真源"。导入失败就让脚本
# 响亮地崩，不做兜底默认值。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from app.production.prep_pack import PREP_PACK_VERSION as EXPECTED_PREP_PACK_VERSION


def cmd_verify(_args) -> int:
    """验收：10 集全部通过项目自身的业务校验，且没有残留异常或脏数据。

    2026-08-24 随剧本台改造为轻量 episode_prep_pack 流程重写判据（旧判据只看
    `_screenplay_ready`/权威解析/页面状态，对新契约的产物形状/覆盖账本/资产
    映射真实存在性完全不敏感，会把半成品或旧契约残留误判为合格）。合格需同时
    满足：
      * `episodes.screenplay_status='ready'`；
      * 已发布 artifact 存在，`type='episode_prep_pack'`、`status='approved'`、
        `contract_version='6.0.0'`；
      * artifact payload 的 `prep_pack_version` 等于生成器单一真源
        `app.production.prep_pack.PREP_PACK_VERSION`（此处不手抄具体数字——
        曾因手抄 1.2.0 而生成器已升 1.3.0 造成全量误报，见常量处注释）；
      * `coverage_ledger.uncovered` 为空列表（发布前 app/production/prep_pack.py
        的 `assert_prep_pack_coverage_complete` 已经是硬门禁，这里是复核，不是
        新增标准）；
      * `asset_manifest.characters[].portrait_id` 在 `character_portraits` 表
        真实存在、`asset_manifest.scenes[].scene_reference_id` 在
        `scene_references` 表真实存在（不只信任 payload 里的字符串，防止发布
        后角色库被别的流程删除/改名导致的悬空引用）；
      * payload 的 `hook`/`cliffhanger` 均非空（注意不是 `episodes.hook` 列——
        该列存的是*下一集*的 hook，来自本集 cliffhanger 的转存，EP1 天然为空，
        用它当判据会把 EP1 永远判成不合格）；
      * 沿用项目已有判据：`_screenplay_ready`（app/domain/common.py，已经会按
        payload 里的 `prep_pack_version` 分派到 prep_pack 专用校验
        `_prep_pack_ready_uncached`，是最新的）、没有仍活跃的 run、页面轻量
        状态端点的 `screenplay_state.code` 以 ready 开头。

    2026-08-24 **不再**调用 `resolve_current_screenplay_authority`（曾经也在
    这份判据里）：真机验证过，它在 app/production/screenplay_authority.py:1856
    硬编码只认 `artifact.type=='screenplay_document'`，对 `episode_prep_pack`
    一律判"类型无效"报错——这不是 bug，是分镜台契约尚未迁移到 prep_pack
    （docs/TRANSFORM_FREEZE_PLAN.md §4 P1，本轮明确不动）留下的边界，其余校验
    （`_verified_artifact_hash`/`assert_screenplay_matches_validated_v7_source`
    等）也全部是旧契约专属，对 prep_pack 不适用。继续把它当硬性判据只会让
    verify 对任何合规的 P0 产物永远报红。上面对 artifact
    type/status/contract_version/prep_pack_version/hard-gate/资产真实存在性的
    直接核对已经覆盖了它原本想保证的"已发布产物真实、完整、可信"，颗粒度更细。
    """
    import sys as _sys

    _sys.path.insert(0, str(ROOT))
    from app.db import get_conn
    from app.domain.common import _screenplay_ready

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

            artifact_id = row["published_screenplay_artifact_id"]
            art = (
                db.execute(
                    "SELECT * FROM artifacts WHERE id=?", (artifact_id,)
                ).fetchone()
                if artifact_id else None
            )
            if art is None:
                problems.append("已发布 episode_prep_pack artifact 不存在")
            else:
                if art["type"] != "episode_prep_pack":
                    problems.append(f"artifact type={art['type']}")
                if art["status"] != "approved":
                    problems.append(f"artifact status={art['status']}")
                if art["contract_version"] != EXPECTED_SCREENPLAY_CONTRACT_VERSION:
                    problems.append(
                        f"artifact contract_version={art['contract_version']}"
                    )
                try:
                    content = json.loads(art["content_json"] or "{}")
                except json.JSONDecodeError:
                    content = {}
                    problems.append("artifact content_json 解析失败")
                if content.get("prep_pack_version") != EXPECTED_PREP_PACK_VERSION:
                    problems.append(
                        f"prep_pack_version={content.get('prep_pack_version')!r}"
                    )
                ledger = content.get("coverage_ledger") or {}
                uncovered = ledger.get("uncovered") or []
                if uncovered:
                    problems.append(f"coverage_ledger.uncovered 非空：{uncovered[:10]}")
                manifest = content.get("asset_manifest") or {}
                for character in manifest.get("characters") or []:
                    pid = character.get("portrait_id")
                    exists = pid and db.execute(
                        "SELECT 1 FROM character_portraits WHERE id=?", (pid,),
                    ).fetchone()
                    if not exists:
                        problems.append(
                            f"角色「{character.get('display_name')}」"
                            f"portrait_id={pid!r} 在 character_portraits 中不存在"
                        )
                for scene in manifest.get("scenes") or []:
                    sid = scene.get("scene_reference_id")
                    exists = sid and db.execute(
                        "SELECT 1 FROM scene_references WHERE id=?", (sid,),
                    ).fetchone()
                    if not exists:
                        problems.append(
                            f"场景「{scene.get('display_name')}」"
                            f"scene_reference_id={sid!r} 在 scene_references 中不存在"
                        )
                if not str(content.get("hook") or "").strip():
                    problems.append("artifact payload.hook 为空")
                if not str(content.get("cliffhanger") or "").strip():
                    problems.append("artifact payload.cliffhanger 为空")
        state = (payload.get("screenplay_state") or {}).get("code")
        if not str(state or "").startswith("ready"):
            problems.append(f"页面状态={state}")
        if problems:
            ok = False
            log(f"{name} ✗ " + "；".join(problems))
        else:
            log(f"{name} ✓ ready / prep_pack 契约完整 / 资产映射真实存在 / 页面状态={state}")
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
