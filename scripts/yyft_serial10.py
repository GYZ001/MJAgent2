#!/usr/bin/env python3
"""EP1→EP10 严格串行剧本生成驱动（原为「我欲封天」专用，2026-09-01 起按项目名
运行时解析，不再硬编码单一项目）。

用法（--project 必填，无默认值——模块内 EPISODES 是随某个已重建项目失效的历史
硬编码值，见 resolve_project_episodes() docstring）:
    py scripts/yyft_serial10.py status --project 我欲封天
    py scripts/yyft_serial10.py clear --project 我欲封天    # 仅清空本项目 EP1-EP10 的剧本数据
    py scripts/yyft_serial10.py run --project 我欲封天      # 默认=自动循环模式，见下
    py scripts/yyft_serial10.py run --project 我欲封天 --from EP4
                                                      # 首轮起点；仅对第 1 轮生效——
                                                      # 自动循环触发的每一轮之后都固定
                                                      # 从 EP1 重跑（协议要求），不续用
                                                      # 这个 --from
    py scripts/yyft_serial10.py run --project 我欲封天 --single-pass
                                                      # 退回旧的单轮语义：一集失败即停轮
                                                      # 等人工 RCA，不自动重启/清库/重跑
                                                      # （测试、以及需要人工先看一眼再决定
                                                      # 要不要继续时用）

失败后"重启后端 + 清库 + 重新发 run"有两个独立触发器，都会走到这套流程，互不冲突：
  1) 【外部触发，协调层职责，不在本文件内实现】协调层的修复一旦落地，会**主动杀掉
     正在跑的驱动进程**、重启后端、clear、重新发 run——不等本轮跑完。因此本文件
     必须对"运行到任意一步突然被 SIGTERM/SIGKILL"保持健壮：进程内状态（cycle 计数、
     上一轮失败签名、重试计数）只存在 Python 变量里，不落盘、不写锁文件、不依赖"进程
     还活着才能恢复"的任何东西——被杀掉之后重新执行 `run` 就是全新的一次调用，配合
     协调层已经做的 clear，天然从干净状态起步，无需本文件额外加恢复机制。
  2) 【内部触发，本文件默认实现，2026-08-24 起】没有新修复、纯粹因为分诊后停轮
     （见下方"失败分诊"），驱动自己在 cmd_run 内部循环：重启后端 → 健康探测 →
     clear → 从 EP1 重新开始一轮，全程不需要人工协调。护栏（详见
     AUTO_RUN_CYCLE_MAX/FailureSignature 处注释）：连续两轮命中同一失败签名，或
     单次 run 调用触达轮数上限，都会停止自动循环，交回人工 RCA——这不是黑名单，是
     防止在真实、确定性故障上死循环的保险丝。

设计约束（与任务书一致）：
  * 严格串行，任何时刻只有一集在跑；
  * 失败分诊三族（2026-08-24 起，替代原来"限流才等/其余一律停轮"的二分；
    判据与结构化证据来源见 classify_failure_family() docstring）：
    - 内容族（quality_gate：PrepPackGateError/StructuredSemanticError 等门禁
      与业务校验）—— 真信号，立即停轮，不自动重试；
    - 瞬时族（provider/限流/ReadTimeout/INTERRUPTED/429/5xx/
      StructuredFormatError 掷骰子失败）—— 每集最多自动重试
      TRANSIENT_RETRY_MAX 次，阶梯退避 TRANSIENT_RETRY_BACKOFF_S
      （60s→120s→300s）；上限用尽仍瞬时失败则停轮并汇总全部重试证据
      （#20 债务收口：原限流固定等 1800s 的特判已并入这套统一分诊）；
    - 未知族 —— fail-safe 默认，停轮等人工 RCA；
  * GEN-RETRY-GRANT（error_logs.category=='generation_retry_grant'）不参与
    上面的自动重试——2026-08-24 剧本台改造为轻量 episode_prep_pack 流程后，这条
    路径在当前后端下已结构性不可达（证据与判断见 is_retry_grant_category() 的
    docstring），命中即代表异常，只标注、不自动重试，按分诊结果处理（通常落
    未知族，停轮）；
  * 只清理本项目这 10 集的剧本数据，绝不触碰其它项目或分集。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent

# 直接 `python scripts/x.py` 运行时 sys.path[0] 是 scripts/，不是仓库根。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.session_token import session_token  # noqa: E402
BASE = "http://127.0.0.1:8230"
# 2026-08-24 起改用回归专用会话凭证（data/regression_session_token.txt），不再
# 复用开发者本机的 local_session_secret.txt；读取方式不变：整行 .strip() 后原样
# 当 X-Manju-Session 头发出。已用新旧两个 token 分别对只读端点
# （GET /api/system/jobs、/screenplay/status）验证过 200，写操作（含审批）的
# 验证按约定留给正式回归的 clear 步骤。
# 历史硬编码值，随某个已重建项目失效（当前库零命中）；`tests/test_yyft_serial10_
# *.py` 直接调用 cmd_run/classify_failure_family 等纯函数、从不经过 main()，
# 保留这份常量不影响那些测试。真正要跑这个驱动时必须显式传 `--project`，
# main() 会用 resolve_project_episodes() 的运行时解析结果整体替换这个列表
# （同模块 global 重绑定，见该函数与 main() 里的 `global EPISODES`）。
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

# --- 失败分诊（2026-08-24 起，替代原来的"限流才等/其余一律停轮"二分） -------
#
# 三族：
#   内容族 content   -- error_logs.category=='quality_gate'（PrepPackGateError/
#     StructuredSemanticError/ContentGenerationError/ScreenplayNarrativeGateError，
#     app/errors.py:classify 的既有结构化分类）。供应商调用本身成功，失败的是
#     业务/QA 校验——真信号，立即停轮，不自动重试。
#   瞬时族 transient  -- category=='provider'（含 ReadTimeout 等传输层异常，
#     app/hiagent.py 统一包装成 ProviderError）、exc_type=='StructuredFormatError'
#     （模型拼错字段/畸形 JSON 的掷骰子失败——后端 run 内的格式重试已经用尽才会
#     外抛到这一层，驱动层给整个 run 重新采样一次是安全的，不是绕过门禁）、
#     provider_calls.http_status in (429, 500..599)、provider_calls.status==
#     'INTERRUPTED'，或下面 TRANSIENT_TEXT_MARKERS 兜底命中——每集最多自动重试
#     TRANSIENT_RETRY_MAX 次，阶梯退避 TRANSIENT_RETRY_BACKOFF_S。
#   未知族 unknown    -- 以上都不命中，fail-safe 默认，仍按停轮处理。
#
# 刻意不放裸 "429"：错误码形如 ERR-20260822-4295ab，裸数字会在无关文本里假阳性
# ——429 一律走 provider_calls.http_status 这一**结构化**字段判定，不做文本匹配。
CONTENT_FAILURE_CATEGORIES = frozenset({"quality_gate"})

TRANSIENT_TEXT_MARKERS = (
    "rate_limit", "rate limit", "ratelimit", "too many requests",
    "quota exceeded", "quota temporarily", "insufficient_quota",
    "限流", "请求过于频繁", "concurrency limit",
    "tokens per minute", "requests per minute",
    "readtimeout", "read timeout", "connecttimeout", "connect timeout",
    "connection reset", "connection aborted", "connection refused",
    "bad gateway", "gateway timeout", "service unavailable",
    "internal server error", "temporarily unavailable",
)

# #20 债务收口：原限流固定等 1800s（或遵循 Retry-After）的特判并入统一分诊，
# 不再单独处理——限流本就是瞬时族的一种，走同一套阶梯退避。
TRANSIENT_RETRY_BACKOFF_S = (60.0, 120.0, 300.0)
TRANSIENT_RETRY_MAX = len(TRANSIENT_RETRY_BACKOFF_S)

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


def resolve_project_episodes(selector: str, count: int = 10) -> list[tuple[str, str]]:
    """按项目名（或 id）运行时解析该项目前 ``count`` 集，供 ``main()`` 的
    ``--project`` 用来替换模块级 ``EPISODES`` 硬编码。

    模块顶部硬编码的 ``proj_3ac0b627fa46`` 及其 10 个 ``ep_*`` id 已随项目重建
    全部失效（当前库里零命中，见 docs/dead_code_locations_2026-08-30.md 的
    2026-09-01 复核附录），直接跑 `main()` 会在第一次 HTTP 调用就打空。这里
    按名字解析、绝不新写死一份 id：命中 0 个或多个都直接退出，避免猜错项目
    悄悄跑掉几小时。
    """
    conn = _readonly_conn()
    try:
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
        project_id = rows[0]["id"]
        episodes = conn.execute(
            "SELECT id, episode_no FROM episodes WHERE project_id=? AND episode_no<=? "
            "ORDER BY episode_no",
            (project_id, count),
        ).fetchall()
    finally:
        conn.close()
    if not episodes:
        raise SystemExit(f"项目 {selector!r}（{project_id}）没有任何分集")
    return [(f"EP{row['episode_no']}", row["id"]) for row in episodes]


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


def classify_failure_family(
    eid: str, since: float, *, db_path: Path | None = None,
) -> tuple[str, str]:
    """Diagnose the episode's most recent failure into content/transient/unknown.

    Structured evidence first, same discipline as ``is_retry_grant_category``:
    DB fields the classifier/provider layer itself wrote, not text guessing,
    decide the family; free-text markers are only a fallback for the cases
    that genuinely have no dedicated structured field (e.g. a bare transport
    ``ReadTimeout`` before any HTTP status was ever assigned).

    Returns ``(family, evidence)`` where ``family`` is:
      - ``"content"``: an ``error_logs.category=='quality_gate'`` row exists
        for this episode in the window -- a real business/QA signal (see
        CONTENT_FAILURE_CATEGORIES docstring above) -- **and** every
        provider_calls row in the same window is a clean OK/settled outcome.
        Real gate signal, no自动重试.
      - ``"transient"``: either (a) a quality_gate row exists but the window
        also contains a provider_calls row that was itself INTERRUPTED or
        timed out (2026-08-24 refinement, real 第 30 轮 EP7 事故: 场景发现
        调用被供应商 ReadTimeout 打断 302s，发现空手而归才连带把资产映射判成
        quality_gate 失败——表象是内容失败，根子是瞬时中断，值得重试而不是
        当真门禁信号停轮); or (b) no quality_gate row, and there's
        category=='provider', or exc_type=='StructuredFormatError', or a
        provider_calls row with http_status in (429, 500..599) or
        status=='INTERRUPTED', or a TRANSIENT_TEXT_MARKERS hit in the raw
        evidence text.
      - ``"unknown"``: none of the above -- fail-safe default, still stops
        the run for a human RCA (unchanged from before this triage existed).
    """
    conn = _readonly_conn(db_path)
    try:
        error_rows = conn.execute(
            "SELECT id, category, exc_type, message FROM error_logs "
            "WHERE ts>=? AND json_extract(context_json,'$.episode_id')=? "
            "ORDER BY ts DESC LIMIT 6",
            (since, eid),
        ).fetchall()
        call_rows = conn.execute(
            "SELECT id, http_status, status, error FROM provider_calls "
            "WHERE ts>=? AND status!='OK' ORDER BY id DESC LIMIT 8",
            (since,),
        ).fetchall()
    finally:
        conn.close()

    def _timeout_or_interrupted_call_reason(row) -> str | None:
        """Narrow check used only to decide content-vs-hybrid: specifically an
        INTERRUPTED call or transport-timeout evidence (the shape of the
        real 第 30 轮 EP7 incident), not the broader 429/5xx transient set
        (a distinct real 5xx alongside a gate failure is *not* evidence the
        gate failure itself was timeout-induced, so it must not flip a real
        quality_gate signal into an auto-retried one)."""
        status = str(row["status"] or "").upper()
        text = str(row["error"] or "").lower()
        if status == "INTERRUPTED" or any(
            marker in text
            for marker in (
                "readtimeout", "read timeout", "connecttimeout", "connect timeout",
                "writetimeout", "write timeout", "pooltimeout", "pool timeout",
                "timeout", "超时",
            )
        ):
            return (
                f"call {row['id']} http={row['http_status']} status={status} "
                f"err={str(row['error'] or '')[:160]}"
            )
        return None

    content_row = next(
        (row for row in error_rows if row["category"] in CONTENT_FAILURE_CATEGORIES),
        None,
    )
    if content_row is not None:
        timeout_reasons = [
            reason for reason in (
                _timeout_or_interrupted_call_reason(row) for row in call_rows
            )
            if reason is not None
        ]
        if timeout_reasons:
            return "transient", (
                "内容失败疑由瞬时中断诱发：" + timeout_reasons[0]
                + f"（quality_gate 表象：{content_row['id']} | "
                f"{content_row['exc_type']} | "
                f"{str(content_row['message'] or '')[:120]}）"
            )
        return "content", (
            f"{content_row['id']} | {content_row['category']} | "
            f"{content_row['exc_type']} | {str(content_row['message'] or '')[:200]}"
        )

    reasons: list[str] = []

    def _mark(reason: str) -> None:
        if reason not in reasons:
            reasons.append(reason)

    for row in error_rows:
        if row["category"] == "provider":
            _mark(
                f"{row['id']} | provider | {row['exc_type']} | "
                f"{str(row['message'] or '')[:160]}"
            )
        elif row["exc_type"] == "StructuredFormatError":
            _mark(
                f"{row['id']} | StructuredFormatError（掷骰子失败，后端格式内重试已"
                f"用尽，驱动层给整 run 重新采样一次）| {str(row['message'] or '')[:160]}"
            )
    for row in call_rows:
        http_status = row["http_status"]
        status = str(row["status"] or "").upper()
        is_5xx = isinstance(http_status, int) and 500 <= http_status < 600
        if http_status == 429 or status == "INTERRUPTED" or is_5xx:
            _mark(
                f"call {row['id']} http={http_status} status={status} "
                f"err={str(row['error'] or '')[:160]}"
            )

    blob = " ".join(
        [str(row["message"] or "") for row in error_rows]
        + [str(row["error"] or "") for row in call_rows]
    ).lower()
    if any(marker in blob for marker in TRANSIENT_TEXT_MARKERS):
        _mark("文本命中瞬时故障白名单（rate_limit/ReadTimeout/5xx 说明文案等）")

    if reasons:
        return "transient", "；".join(reasons[:5])
    return "unknown", "(未命中任何已知瞬时/内容证据，按未知族 fail-safe 停轮)"


# --- 失败停轮后自动恢复（2026-08-24 起，内部触发器，见文件头协议说明） -------
#
# 单次 `run` 调用的自动循环轮数上限：不是黑名单，是防止在真实、确定性故障上
# 死循环的保险丝。达到上限就停，把每一轮的失败签名汇总进日志交回人工。
AUTO_RUN_CYCLE_MAX = 8


class FailureSignature(NamedTuple):
    """一次停轮的机械化签名，用于判断"连续两轮同一故障"（另一道死循环保险丝）。

    四个字段全部从失败对象机械推导，不含任何具体业务词/硬编码名单：
      - episode：停轮所在的集号（如 'EP4'）；
      - family：故障族——classify_failure_family() 的返回值（content/
        transient/unknown），或 'start_refused'（start_or_resume 被后端拒绝，
        不在失败分诊范围内的另一种停轮原因）；
      - exc_type：该集失败窗口内最新一条 error_logs.exc_type（结构化字段，
        没有则为空串，不做任何文本猜测）；
      - message_digest：失败原始文本经 _normalize_message() 机械归一化
        （数字统一替换、空白折叠、截断）后的前缀。
    两轮的 FailureSignature 相等（NamedTuple 逐字段比较）即视为"同一失败复现"。
    """

    episode: str
    family: str
    exc_type: str
    message_digest: str


def _normalize_message(text: str, length: int = 160) -> str:
    """把失败文本机械归一化成可比较的摘要。

    折叠连续空白、把所有数字串统一替换成 '#'（时间戳/错误码序号/延迟毫秒数
    这类逐次必然不同的数字不该让本质相同的故障被误判成不同签名），再截断到
    length 字符。纯正则变换，不含任何业务词表，对任何项目/任何故障文本通用。
    """
    collapsed = re.sub(r"\s+", " ", (text or "").strip())
    digitless = re.sub(r"\d+", "#", collapsed)
    return digitless[:length]


def _latest_exc_type(eid: str, since: float, *, db_path: Path | None = None) -> str:
    """该集失败窗口内最新一条 error_logs.exc_type（结构化字段，无则空串）。"""
    conn = _readonly_conn(db_path)
    try:
        row = conn.execute(
            "SELECT exc_type FROM error_logs WHERE ts>=? AND "
            "json_extract(context_json,'$.episode_id')=? ORDER BY ts DESC LIMIT 1",
            (since, eid),
        ).fetchone()
    finally:
        conn.close()
    return str(row["exc_type"] or "") if row is not None else ""


def _log_cycle_history(history: list[FailureSignature]) -> None:
    log(f"=== AUTO-CYCLE 失败签名汇总（共 {len(history)} 轮） ===")
    for idx, sig in enumerate(history, start=1):
        log(f"  第 {idx} 轮：episode={sig.episode} family={sig.family} "
            f"exc_type={sig.exc_type!r} message={sig.message_digest!r}")


# --- 代码指纹护栏（2026-08-24 起，第 36 轮回归事故复盘新增） ------------------
#
# 事故：自愈重启从**磁盘**加载代码；第 36 轮回归期间有多个 agent 正在改
# app/，自愈重启把半成品代码捞进了服务，导致该轮后续结果不可信——协调层只能
# 手工终止整轮。既有的 import 自检（_backend_import_self_check）只能拦语法/
# 导入错误，拦不住"语义半成品"（A 文件已加新字段、依赖它的 B 文件还没改完，
# 导入完全正常但行为不一致）。
#
# 核心原则：一轮回归只能测一个已知的、固定的代码状态，否则它的绿灯和红灯都
# 不能当证据用。护栏职责是**检测并诚实停下**，不是给代码加锁/阻止别人改
# 文件——不做任何文件锁、不做任何写保护。
#
# 指纹覆盖范围：
#   * app/**/*.py 的内容——sha256 逐文件摘要，按"仓库相对路径"排序后拼接
#     （不是 mtime！mtime 会被无意义的 touch 改变，产生假阳性停轮；按内容
#     哈希才是"代码是否真的变了"的唯一可靠判据）。文件被新增/删除也会改变
#     参与拼接的路径集合，因此同样能被侦测到。
#   * 当前 git HEAD——commit 切换/revert 场景下，即使 app/**/*.py 字节内容
#     出于巧合恰好相同（理论上限，双重兜底），HEAD 变化也能单独触发侦测。
#   * 本驱动脚本自身（scripts/yyft_serial10.py）。纳入的理由：指纹要保证的
#     是"整个回归协议在本轮期间保持不变"，不只是被测后端代码不变——分诊
#     阈值（TRANSIENT_RETRY_MAX 等）、退出码语义、失败签名归一化规则全部
#     定义在这个文件里。这个文件的改动不会让"正在运行的这个 Python 进程"
#     当场变化（模块已经导入进内存，不会热重载），但它会让接下来重启出的
#     新一轮、以及回看本次回归日志的人，落在与本轮开头不同的协议假设上——
#     同样属于"代码状态已经不再固定"，必须一并纳入侦测范围。
#
# 不覆盖（刻意）：
#   * logs/、data/——运行期产物，理应随着每一轮跑动而变化，不该触发误停；
#   * app/ 下非 .py 的资源文件（模板/静态资源等）——当前不参与 import 后的
#     实际执行语义，纳入只会增加"资源文件被合理更新也误报漂移"的噪音；
#   * .venv 等第三方依赖——不属于本仓库改动范围，且体积远大于 app/，逐字节
#     哈希会显著拖慢每次自愈重启前的检查。
#
# 校验时机：只在"每次自愈重启前"（cmd_run 的自动循环里，重启后端之前）比对，
# **不**在单集内的瞬时重试（60/120/300s 阶梯，见 TRANSIENT_RETRY_BACKOFF_S）
# 之间比对。理由：瞬时重试不重启后端，跑的仍是同一个已经加载进内存的旧
# 进程——磁盘上 app/ 是否漂移，不会改变这个正在跑的进程接下来的行为，因此
# 不会污染"这一轮的结果对应哪个代码状态"这个前提；真正会把磁盘代码读进服务
# 的唯一动作是 restart_backend()，指纹护栏卡在这个动作前面就足以保证"一轮
# 回归只测一个已知固定代码状态"。在重试间额外校验只能换来略早一点的
# *可观测性*（本来在本轮结束后、下次重启前也会测到同样的漂移），却要为
# 每集最多 3 次重试各付一次哈希+git 子进程的开销，性价比不划算，故不实现。


def _app_python_files(root: Path) -> list[Path]:
    """`app/**/*.py`，按路径排序保证跨次运行的枚举顺序稳定（目录遍历顺序
    依赖文件系统，不能直接信任）。"""
    app_dir = root / "app"
    if not app_dir.is_dir():
        return []
    return sorted(app_dir.rglob("*.py"))


def _git_head(root: Path) -> str:
    """当前 git HEAD；仓库异常（例如给了个没有 .git 的目录，测试场景会用到）
    时不崩，退化成一个仍然确定性的占位串。"""
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root,
        capture_output=True, text=True, timeout=15,
    )
    if proc.returncode == 0:
        return proc.stdout.strip()
    return f"<git-head-unavailable:{proc.returncode}>"


def compute_code_fingerprint(
    *, root: Path | None = None, driver_path: Path | None = None,
) -> str:
    """回归期间代码指纹：app/**/*.py 内容 + git HEAD + 驱动脚本自身内容。

    `root`/`driver_path` 仅供测试注入隔离的假仓库；生产调用（cmd_run 内）
    不传参数，默认用真实 ROOT 与真实 __file__。
    """
    resolved_root = root if root is not None else ROOT
    resolved_driver = (
        driver_path if driver_path is not None else Path(__file__).resolve()
    )
    hasher = hashlib.sha256()
    files = _app_python_files(resolved_root)
    files.append(resolved_driver)
    for path in files:
        try:
            content = path.read_bytes()
        except OSError:
            content = b"<unreadable>"
        try:
            rel = path.relative_to(resolved_root)
        except ValueError:
            rel = path
        hasher.update(str(rel).replace("\\", "/").encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(hashlib.sha256(content).digest())
        hasher.update(b"\0")
    hasher.update(b"HEAD=")
    hasher.update(_git_head(resolved_root).encode("utf-8"))
    return hasher.hexdigest()


# 与既有 run 退出码（0/2/3/4/5/6）都不冲突的专用值。
CODE_DRIFT_EXIT_CODE = 7


# --- 重启后端（照抄协调层已验证的安全序） ------------------------------------
#
# 安全序：先 import 自检（工作树可能正被其他 agent 半编辑，自检失败绝不碰旧
# 进程，宁可用旧代码继续跑也不能把服务打死）→ 自检通过才用 `ss -ltnp` 按端口
# 取 PID（严禁 pgrep/pkill 按名匹配——历史上自匹配翻车 4 次，见
# mjagent2-backend-restart-on-this-box 记忆）→ kill 旧进程、等端口释放 →
# setsid nohup 拉起新进程 → 带会话头轮询 /api/system/jobs 到 200 才算重启成功。
BACKEND_PORT = 8230
BACKEND_LOG_PATH = "/tmp/manju2_backend.log"
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"
VENV_UVICORN = ROOT / ".venv" / "bin" / "uvicorn"

BACKEND_IMPORT_RETRY_MAX = 5
BACKEND_IMPORT_RETRY_DELAY_S = 60.0
BACKEND_PORT_RELEASE_TIMEOUT_S = 30.0
BACKEND_PORT_RELEASE_POLL_S = 1.0
BACKEND_HEALTH_TIMEOUT_S = 120.0
BACKEND_HEALTH_POLL_S = 2.0


def _backend_import_self_check() -> tuple[bool, str]:
    """`.venv/bin/python -c "import app.main"` 一次性自检，不改动任何进程。"""
    proc = subprocess.run(
        [str(VENV_PYTHON), "-c", "import app.main"],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    detail = (proc.stderr or proc.stdout or "").strip()[-2000:]
    return proc.returncode == 0, detail


def _wait_for_import_self_check() -> bool:
    """自检失败最多重试 BACKEND_IMPORT_RETRY_MAX 次（阶梯说明见任务书）：
    工作树很可能正被其他 agent 半编辑，绝不能在这个状态下杀旧进程——旧进程
    好歹还能跑，杀了就是把服务彻底打死。"""
    for attempt in range(1, BACKEND_IMPORT_RETRY_MAX + 1):
        ok, detail = _backend_import_self_check()
        if ok:
            if attempt > 1:
                log(f"import app.main 自检在第 {attempt} 次尝试通过。")
            return True
        remaining = BACKEND_IMPORT_RETRY_MAX - attempt
        log(f"import app.main 自检失败（第 {attempt}/{BACKEND_IMPORT_RETRY_MAX} 次）"
            "——工作树可能正被其他 agent 半编辑，不重启旧进程。"
            + (f"等待 {int(BACKEND_IMPORT_RETRY_DELAY_S)}s 后重试（还剩 {remaining} 次）。"
               if remaining > 0 else "重试已用尽。")
            + f" detail={detail[:500]}")
        if remaining > 0:
            time.sleep(BACKEND_IMPORT_RETRY_DELAY_S)
    log(f"import app.main 自检连续 {BACKEND_IMPORT_RETRY_MAX} 次失败，判定工作树处于"
        "半编辑状态。宁可用旧代码继续跑也不能把服务打死——放弃本次重启，旧进程原样保留。")
    return False


def _find_backend_pid() -> int | None:
    """`ss -ltnp` 按端口取监听进程 PID——严禁 pgrep/pkill 按名匹配。"""
    proc = subprocess.run(
        ["ss", "-ltnp"], capture_output=True, text=True, timeout=15,
    )
    port_pattern = re.compile(rf":{BACKEND_PORT}\s")
    pid_pattern = re.compile(r"pid=(\d+)")
    for line in proc.stdout.splitlines():
        if "LISTEN" not in line or not port_pattern.search(line):
            continue
        match = pid_pattern.search(line)
        if match:
            return int(match.group(1))
    return None


def _kill_backend_pid(pid: int) -> bool:
    """SIGTERM 旧进程，等端口释放；超时则 SIGKILL 后再等一轮。成功返回 True。"""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    for _escalate in (False, True):
        deadline = time.monotonic() + BACKEND_PORT_RELEASE_TIMEOUT_S
        while time.monotonic() < deadline:
            if _find_backend_pid() is None:
                return True
            time.sleep(BACKEND_PORT_RELEASE_POLL_S)
        if _escalate:
            return False
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return True
    return False


def _launch_backend() -> None:
    command = (
        f"cd {shlex.quote(str(ROOT))} && setsid nohup {shlex.quote(str(VENV_PYTHON))} "
        f"{shlex.quote(str(VENV_UVICORN))} app.main:app --host 127.0.0.1 "
        f"--port {BACKEND_PORT} --timeout-graceful-shutdown 30 "
        f"> {shlex.quote(BACKEND_LOG_PATH)} 2>&1 &"
    )
    subprocess.run(["bash", "-c", command], cwd=ROOT, check=True)


def _health_probe() -> bool:
    """带 X-Manju-Session 头轮询 GET /api/system/jobs 到 200，超时返回 False。"""
    deadline = time.monotonic() + BACKEND_HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        code, _resp = call("GET", "/api/system/jobs", timeout=10)
        if code == 200:
            return True
        time.sleep(BACKEND_HEALTH_POLL_S)
    return False


def restart_backend() -> bool:
    """完整重启协议：import 自检 → kill 旧进程 → 拉起新进程 → 健康探测。

    任一环节失败都返回 False 且不再往下走（自检失败尤其不动旧进程）。
    """
    log("=== 后端重启协议开始（导入自检 → kill 旧进程 → 拉起新进程 → 健康探测） ===")
    if not _wait_for_import_self_check():
        return False
    pid = _find_backend_pid()
    if pid is not None:
        log(f"发现监听 :{BACKEND_PORT} 的旧进程 pid={pid}"
            "（ss -ltnp 定位，不用 pgrep/pkill 按名匹配）。")
        if not _kill_backend_pid(pid):
            log(f"旧进程 pid={pid} 在等待窗口内未释放端口 :{BACKEND_PORT}，停止重启。")
            return False
        log(f"旧进程 pid={pid} 已退出，端口 :{BACKEND_PORT} 已释放。")
    else:
        log(f"未发现监听 :{BACKEND_PORT} 的旧进程，直接拉起新进程。")
    _launch_backend()
    log(f"已提交后端拉起命令（setsid nohup ... --port {BACKEND_PORT}），等待健康探测。")
    if not _health_probe():
        log(f"健康探测超时（{int(BACKEND_HEALTH_TIMEOUT_S)}s 内 "
            "GET /api/system/jobs 未返回 200），停止。")
        return False
    log("健康探测通过（GET /api/system/jobs -> 200）。后端重启协议完成。")
    return True


def _clear_all_episodes() -> bool:
    return all([clear_one(name, eid) for name, eid in EPISODES])


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
    ok = _clear_all_episodes()
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


def _execute_serial_pass(start_index: int) -> tuple[int, dict, FailureSignature | None]:
    """跑一轮 EP{start_index+1}→EP10 严格串行；一集被拒绝启动或分诊后停轮，
    这一轮就立即返回（其余集不再动）。

    这是原来 cmd_run 的全部单轮逻辑，原样保留、未改变任何判定；唯一的区别是
    现在把结果打包返回给上层的 cmd_run 自动循环，而不是直接当 CLI 出口。

    返回 (rc, results, signature)：
      * rc==0：全部 ready，signature 恒为 None（成功不需要给自动循环判定重复）；
      * rc==3：某集被拒绝启动/续跑；rc==4：某集分诊后停轮——这两种情况
        signature 是这次停轮的 FailureSignature，供 cmd_run 判断"连续两轮
        同一签名"。
    rc 的语义与原来单轮版 cmd_run 完全一致，未新增/未挪用其它返回码。
    """
    log(f"=== SERIAL RUN EP{start_index + 1}-EP{len(EPISODES)} START ===")
    results: dict[str, str] = {}
    for name, eid in EPISODES[start_index:]:
        transient_retries = 0
        transient_evidence_log: list[str] = []
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
                refusal_state = json.dumps(status_of(eid), ensure_ascii=False)
                signature = FailureSignature(
                    episode=name, family="start_refused",
                    exc_type=_latest_exc_type(eid, since),
                    message_digest=_normalize_message(refusal_state),
                )
                return 3, results, signature
            payload = await_terminal(name, eid)
            state = str(payload.get("screenplay_status") or "")
            if state == "ready":
                log(f"{name} READY ✅")
                results[name] = "ready"
                break
            failure = (payload.get("screenplay_error") or "")[:400]
            log(f"{name} 未通过：state={state} err={failure}")
            if is_retry_grant_category(eid, since):
                log(f"{name} 命中 GEN-RETRY-GRANT"
                    "（error_logs.category='generation_retry_grant'）。"
                    "在当前 episode_prep_pack-only 后端下这理论上结构性不可达"
                    "（证据见 is_retry_grant_category() docstring）——出现即代表"
                    "后端行为已漂移，或该集仍绑定着改造前的旧管线残留状态，"
                    "不参与下面的失败分诊自动重试，按分诊结果处理。")

            family, evidence = classify_failure_family(eid, since)

            if family == "transient" and transient_retries < TRANSIENT_RETRY_MAX:
                delay = TRANSIENT_RETRY_BACKOFF_S[transient_retries]
                transient_retries += 1
                transient_evidence_log.append(f"第 {transient_retries} 次：{evidence}")
                log(f"{name} 自动重试 {transient_retries}/{TRANSIENT_RETRY_MAX}："
                    f"瞬时故障（{evidence}）——等待 {int(delay)}s 后从本集重新发起")
                time.sleep(delay)
                continue

            if family == "transient":
                log(f"{name} 瞬时故障自动重试已达上限（{TRANSIENT_RETRY_MAX} 次），"
                    "判定为真实故障，停轮。三次证据汇总：")
                for line in transient_evidence_log:
                    log(f"    {line}")
            elif family == "content":
                log(f"{name} 内容族失败（quality_gate）—— 真信号，需人工 RCA，"
                    f"不自动重试：{evidence}")
            else:
                log(f"{name} 未知族失败 —— fail-safe 默认，停轮等待人工根因分析。")
            log(f"{name} 停止整轮。证据：")
            for line in recent_failure_evidence(eid, since).splitlines()[:10]:
                log(f"    {line}")
            results[name] = state or "failed"
            log("=== SERIAL RUN STOPPED ===")
            log(json.dumps(results, ensure_ascii=False))
            signature = FailureSignature(
                episode=name, family=family,
                exc_type=_latest_exc_type(eid, since),
                message_digest=_normalize_message(failure),
            )
            return 4, results, signature
    log("=== SERIAL RUN DONE === " + json.dumps(results, ensure_ascii=False))
    rc = 0 if all(value == "ready" for value in results.values()) else 1
    return rc, results, None


def cmd_run(args) -> int:
    """CLI 入口。默认=自动循环模式（内部触发器，见文件头协议说明）：一轮跑到
    停轮就自动 重启后端 → 健康探测 → clear → 从 EP1 重新开始，直到全部 ready、
    或触发下面任一护栏。`--single-pass` 退回旧的单轮语义（失败即停轮返回，不
    自动恢复），供人工介入场景与测试单轮触发逻辑本身时使用。

    `--from` 只影响第一轮的起点——自动循环触发的每一轮都固定从 EP1 开始
    （协议要求：清库后必须从头验证，不得只续跑失败那集，参见
    mjagent2-serial-regression-discipline 的"从第 1 集重新串行跑"纪律）。
    """
    start_index = 0
    if args.start_from:
        names = [name for name, _ in EPISODES]
        if args.start_from not in names:
            log(f"未知起始集：{args.start_from}")
            return 2
        start_index = names.index(args.start_from)

    if getattr(args, "single_pass", False):
        log("--single-pass：退回旧的单轮语义，失败即停轮，不自动重启后端/清库/重跑。")
        rc, _results, _signature = _execute_serial_pass(start_index)
        return rc

    log("=== AUTO-CYCLE RUN（2026-08-24 起默认开启：一轮停轮后自动 重启后端 → "
        f"健康探测 → clear → 从 EP1 重跑；--single-pass 可退回旧语义）"
        f"单次调用最多 {AUTO_RUN_CYCLE_MAX} 轮 ===")
    baseline_fingerprint = compute_code_fingerprint()
    log("AUTO-CYCLE 代码指纹基线已记录（覆盖 app/**/*.py + git HEAD + 本驱动"
        f"脚本自身，sha256={baseline_fingerprint[:12]}…）；此后每次自愈重启前"
        f"都会重新计算比对，指纹不一致立即停轮（退出码 {CODE_DRIFT_EXIT_CODE}），"
        "不会带着可能已变化的代码继续重启/清库/重跑。")
    cycle = 0
    prev_signature: FailureSignature | None = None
    history: list[FailureSignature] = []
    while True:
        cycle += 1
        log(f"--- AUTO-CYCLE {cycle}/{AUTO_RUN_CYCLE_MAX} 开始"
            f"（起点 {EPISODES[start_index][0]}） ---")
        rc, _results, signature = _execute_serial_pass(start_index)
        if rc == 0:
            log(f"=== AUTO-CYCLE {cycle} 全部 READY，自动循环结束 ===")
            return 0
        if signature is None:
            log(f"AUTO-CYCLE {cycle} 失败（rc={rc}）但未产出可比较的失败签名"
                "（不应发生），fail-safe 停止自动循环，需人工介入。")
            return rc
        history.append(signature)
        log(f"AUTO-CYCLE {cycle} 失败签名：episode={signature.episode} "
            f"family={signature.family} exc_type={signature.exc_type!r} "
            f"message={signature.message_digest!r}")
        if prev_signature is not None and signature == prev_signature:
            log("同一失败签名连续出现 2 次，判定为确定性问题，停止自动循环，"
                "需人工 RCA。")
            _log_cycle_history(history)
            return rc
        if cycle >= AUTO_RUN_CYCLE_MAX:
            log(f"自动循环已达单次 run 调用的上限（{AUTO_RUN_CYCLE_MAX} 轮），"
                "停止自动循环，需人工介入。")
            _log_cycle_history(history)
            return rc
        current_fingerprint = compute_code_fingerprint()
        if current_fingerprint != baseline_fingerprint:
            log("!!! 回归期间代码发生变更，本轮结果不可信，已停止；"
                "请在代码稳定后重新发起 !!!")
            log(f"    基线指纹={baseline_fingerprint} "
                f"当前指纹={current_fingerprint}")
            _log_cycle_history(history)
            return CODE_DRIFT_EXIT_CODE
        log(f"AUTO-CYCLE {cycle} 结束，按协议进入下一轮："
            "重启后端 → 健康探测 → clear → 从 EP1 重跑。")
        if not restart_backend():
            log("后端重启/健康探测失败，停止自动循环（不清数据、不重跑，"
                "旧进程原样保留，需人工介入）。")
            _log_cycle_history(history)
            return 5
        log("AUTO-CYCLE：clear 本项目 EP1-EP10（仅本项目这 10 集，不触碰其它）。")
        clear_ok = _clear_all_episodes()
        log(f"AUTO-CYCLE：clear 完成 ok={clear_ok}")
        if not clear_ok:
            log("clear 未完全成功，停止自动循环，需人工介入。")
            _log_cycle_history(history)
            return 6
        prev_signature = signature
        start_index = 0


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
    parser.add_argument(
        "--single-pass", dest="single_pass", action="store_true",
        help="run 命令退回旧的单轮语义：失败即停轮，不自动重启后端/清库/重跑"
             "（默认=自动循环模式，见 cmd_run docstring）。",
    )
    parser.add_argument(
        "--project", default=None,
        help="项目名或 id（必填，无默认值）。模块内 EPISODES 是随某个已重建项目"
             "失效的历史硬编码值，此参数运行时解析目标项目的 EP1-EP10 并整体替换它。",
    )
    args = parser.parse_args()
    if not args.project:
        print(
            "用法：.venv/bin/python scripts/yyft_serial10.py "
            "<status|clear|run|verify> --project <项目名或 id>\n"
            "缺少 --project：模块内硬编码的历史项目/分集 id 已随项目重建失效，"
            "必须显式指定目标项目。",
            file=sys.stderr,
        )
        return 2
    global EPISODES
    EPISODES = resolve_project_episodes(args.project)
    return {
        "status": cmd_status, "clear": cmd_clear,
        "run": cmd_run, "verify": cmd_verify,
    }[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
