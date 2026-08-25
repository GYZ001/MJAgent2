"""scripts/yyft_serial10.py 的失败分诊覆盖（2026-08-24 协调方裁决）。

背景：原 cmd_run 只有二分——限流才自动等重试，其余一律立即停轮。协调方指令：
"合理的重试机制应自动恢复瞬时故障，不该协调员手动续跑"，把失败升级为三族分诊：
  - 内容族（quality_gate）：真信号，立即停轮，不自动重试；
  - 瞬时族（provider/限流/ReadTimeout/INTERRUPTED/429/5xx/StructuredFormatError
    掷骰子失败）：每集最多自动重试 TRANSIENT_RETRY_MAX 次，阶梯退避
    TRANSIENT_RETRY_BACKOFF_S（60s→120s→300s）；
  - 未知族：fail-safe 默认，停轮。
同时收口 #20：原限流固定等 1800s 的特判并入这套统一阶梯退避。

本文件覆盖：
  1) classify_failure_family 分类器本身——内容族优先命中、瞬时族的四种结构化
     证据（provider 类别/StructuredFormatError/429/5xx/INTERRUPTED）各自命中、
     未知族兜底；
  2) cmd_run 的分诊执行——红灯 a) 瞬时证据走重试分支、b) quality_gate 证据立即
     停、c) 阶梯退避时长断言（60/120/300）、以及上限用尽后仍失败会停轮并汇总
     三次证据。

注：所有 `cmd_run(SimpleNamespace(...))` 调用都显式传 `single_pass=True`——
2026-08-24 起 cmd_run 默认升级为"停轮后自动重启后端/清库/从 EP1 重跑"的循环
模式（见 tests/test_yyft_serial10_auto_cycle.py），而本文件测的是分诊/重试
阶梯这个单轮内部逻辑本身，不应该被外层循环协议影响（也不应该在这些纯 mock
测试里意外触发真实的后端重启子进程）。`single_pass=True` 精确复刻这些用例
最初编写时的单轮语义，与本文件的测试意图完全一致。
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from app import db
from scripts import yyft_serial10


EP1 = "ep_3d523ff4d0a4"


def _insert_provider_call(conn, **overrides) -> None:
    row = {
        "id": None, "ts": 1787600000.0, "kind": "chat", "model": "d8p318cv256o70qpgv90",
        "status": "OK", "http_status": None, "latency_ms": 1000, "error": None,
        "meta": "{}", "run_id": None, "operation_id": None,
        "recovery_disposition": None, "received_chars": 0,
    }
    row.update(overrides)
    conn.execute(
        """INSERT INTO provider_calls(
               id,ts,kind,model,status,http_status,latency_ms,error,meta,run_id,
               operation_id,recovery_disposition,received_chars
           ) VALUES(:id,:ts,:kind,:model,:status,:http_status,:latency_ms,:error,
                     :meta,:run_id,:operation_id,:recovery_disposition,:received_chars)""",
        row,
    )


def _insert_error_log(conn, **overrides) -> None:
    row = {
        "id": "ERR-20260824-000001", "ts": 1787600000.0, "category": "system",
        "category_label": "系统内部", "code": "SYS", "action": "screenplay_generate",
        "context_json": json.dumps({"episode_id": EP1}, ensure_ascii=False),
        "message": "", "exc_type": None,
    }
    row.update(overrides)
    conn.execute(
        """INSERT INTO error_logs(
               id,ts,category,category_label,code,action,context_json,message,exc_type
           ) VALUES(:id,:ts,:category,:category_label,:code,:action,:context_json,
                     :message,:exc_type)""",
        row,
    )


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """A standalone sqlite file (real app schema), empty except what a test seeds."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "triage-fixture.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    return tmp_path / "triage-fixture.db"


# ---------------------------------------------------------------------------
# 1) classify_failure_family：结构化证据优先，各族命中/不命中
# ---------------------------------------------------------------------------

def test_quality_gate_category_is_content_family(fresh_db) -> None:
    """正面：category='quality_gate'（真实 PrepPackGateError 会写这个分类，见
    app/errors.py:classify）必须判为内容族——真信号，不进重试分支。"""
    conn = db.get_conn()
    _insert_error_log(
        conn, category="quality_gate", category_label="质量校验", code="QA",
        exc_type="PrepPackGateError",
        message="[PREP_PACK_COVERAGE_INCOMPLETE] coverage_ledger.uncovered 非空",
    )
    conn.commit()
    family, evidence = yyft_serial10.classify_failure_family(
        EP1, 1787599999.0, db_path=db.DB_PATH,
    )
    assert family == "content"
    assert "quality_gate" in evidence


def test_provider_category_is_transient_family(fresh_db) -> None:
    """正面：category='provider'（真实 ProviderError，含 ReadTimeout 等传输层
    异常，app/hiagent.py 统一包装）必须判为瞬时族——值得自动重试一次。"""
    conn = db.get_conn()
    _insert_error_log(
        conn, category="provider", category_label="大模型/外部服务", code="LLM",
        exc_type="ProviderError",
        message="HiAgent 请求超时（httpx.ReadTimeout）；请求结果不确定，已禁止自动重试",
    )
    conn.commit()
    family, evidence = yyft_serial10.classify_failure_family(
        EP1, 1787599999.0, db_path=db.DB_PATH,
    )
    assert family == "transient"
    assert "provider" in evidence


def test_structured_format_error_is_transient_family(fresh_db) -> None:
    """正面：exc_type='StructuredFormatError'（模型拼错字段/畸形 JSON，后端
    format_retry_limit 已用尽才会外抛）必须判为瞬时族——驱动层给整 run 一次
    新骰子，不是绕过门禁。"""
    conn = db.get_conn()
    _insert_error_log(
        conn, category="system", category_label="系统内部", code="SYS",
        exc_type="StructuredFormatError",
        message="response could not be parsed into the contracted object",
    )
    conn.commit()
    family, evidence = yyft_serial10.classify_failure_family(
        EP1, 1787599999.0, db_path=db.DB_PATH,
    )
    assert family == "transient"
    assert "StructuredFormatError" in evidence


def test_http_429_without_error_log_is_transient_family(fresh_db) -> None:
    """正面：#20 收口——限流本就是瞬时族的一种，即使没有专门的 error_logs
    记录，单凭 provider_calls.http_status=429 这一结构化字段也要判为瞬时族。"""
    conn = db.get_conn()
    _insert_provider_call(conn, id=9001, http_status=429, status="ERROR", error="rate limited")
    conn.commit()
    family, evidence = yyft_serial10.classify_failure_family(
        EP1, 1787599999.0, db_path=db.DB_PATH,
    )
    assert family == "transient"
    assert "http=429" in evidence


def test_5xx_provider_call_is_transient_family(fresh_db) -> None:
    """正面：5xx 单凭 provider_calls.http_status 结构化字段判定，不做文本匹配
    （避免 "5xx" 这种短串在无关文本里假阳性）。"""
    conn = db.get_conn()
    _insert_provider_call(conn, id=9002, http_status=502, status="ERROR", error="Bad Gateway")
    conn.commit()
    family, evidence = yyft_serial10.classify_failure_family(
        EP1, 1787599999.0, db_path=db.DB_PATH,
    )
    assert family == "transient"
    assert "http=502" in evidence


def test_interrupted_provider_call_is_transient_family(fresh_db) -> None:
    """正面：provider_calls.status='INTERRUPTED'（流式请求被打断，结果未知）
    单独也应判为瞬时族。"""
    conn = db.get_conn()
    _insert_provider_call(
        conn, id=9003, status="INTERRUPTED",
        error="流式请求被取消，供应商结果未知",
    )
    conn.commit()
    family, evidence = yyft_serial10.classify_failure_family(
        EP1, 1787599999.0, db_path=db.DB_PATH,
    )
    assert family == "transient"
    assert "INTERRUPTED" in evidence


def test_content_family_wins_over_simultaneous_transient_looking_row(
    fresh_db,
) -> None:
    """反面：同一时间窗内既有 quality_gate 记录、又有看起来像瞬时故障的
    provider_calls 噪声（例如门禁失败前一次无关的瞬态 5xx）——真信号优先，
    必须判为内容族，不能被瞬时证据"冲淡"成可重试。"""
    conn = db.get_conn()
    _insert_provider_call(conn, id=9004, http_status=502, status="ERROR", error="Bad Gateway")
    _insert_error_log(
        conn, category="quality_gate", category_label="质量校验", code="QA",
        exc_type="PrepPackGateError", message="硬门禁未通过",
    )
    conn.commit()
    family, _evidence = yyft_serial10.classify_failure_family(
        EP1, 1787599999.0, db_path=db.DB_PATH,
    )
    assert family == "content"


def test_quality_gate_with_interrupted_call_is_hybrid_transient(fresh_db) -> None:
    """正面（2026-08-24 精化，真实第 30 轮 EP7 事故形态）：quality_gate 报错
    （场景发现空手而归导致资产映射失败），但同窗口存在被供应商 INTERRUPTED/
    ReadTimeout 打断的 provider_calls——根子是发现调用被打断，表象才是内容
    失败，必须归瞬时族自动重试，不能被"内容族优先"误判成真门禁信号停轮。"""
    conn = db.get_conn()
    _insert_provider_call(
        conn, id=9005, status="INTERRUPTED", latency_ms=302000,
        error="HiAgent 场景发现请求超时（httpx.ReadTimeout，302.0s）；请求结果不确定",
    )
    _insert_error_log(
        conn, category="quality_gate", category_label="质量校验", code="QA",
        exc_type="PrepPackGateError",
        message="资产映射未能 100% 解析（已尝试身份/场景发现，调用次数：场景 1）：场景「大青山」未解析到已有 scene_reference_id",
    )
    conn.commit()
    family, evidence = yyft_serial10.classify_failure_family(
        EP1, 1787599999.0, db_path=db.DB_PATH,
    )
    assert family == "transient"
    assert "内容失败疑由瞬时中断诱发" in evidence
    assert "9005" in evidence
    assert "quality_gate 表象" in evidence


def test_quality_gate_with_only_5xx_call_stays_content(fresh_db) -> None:
    """反面：quality_gate 同窗口即使有 provider_calls 非 OK 记录，只要不是
    INTERRUPTED/超时（这里是一次真实 5xx，跟"调用被打断"是不同性质），就不能
    被精化规则误当成混合形态——维持内容族即停，这是精化规则的窄口径（只认
    INTERRUPTED/超时，不是任意"看起来像瞬时"的信号）。"""
    conn = db.get_conn()
    _insert_provider_call(conn, id=9006, http_status=503, status="ERROR", error="Service Unavailable")
    _insert_error_log(
        conn, category="quality_gate", category_label="质量校验", code="QA",
        exc_type="PrepPackGateError", message="硬门禁未通过",
    )
    conn.commit()
    family, _evidence = yyft_serial10.classify_failure_family(
        EP1, 1787599999.0, db_path=db.DB_PATH,
    )
    assert family == "content"


def test_unrelated_failure_is_unknown_family(fresh_db) -> None:
    """反面：既不是 quality_gate、也没有任何瞬时信号（这里用一个真实存在但
    与本次分诊无关的 category，如 not_found）——必须落未知族，fail-safe 停轮，
    不能被误判为可以自动重试。"""
    conn = db.get_conn()
    _insert_error_log(
        conn, category="not_found", category_label="资源不存在", code="NF-404",
        exc_type="ValueError", message="剧集不存在",
    )
    conn.commit()
    family, _evidence = yyft_serial10.classify_failure_family(
        EP1, 1787599999.0, db_path=db.DB_PATH,
    )
    assert family == "unknown"


# ---------------------------------------------------------------------------
# 2) cmd_run：三族分诊的执行 —— 红灯 a) b) c)
# ---------------------------------------------------------------------------

def _prep_single_episode_run(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(yyft_serial10, "EPISODES", [("EP1", EP1)])
    monkeypatch.setattr(yyft_serial10, "LOG", tmp_path / "serial10.log")
    monkeypatch.setattr(
        yyft_serial10, "status_of",
        lambda eid: {"screenplay_status": "failed", "active": False},
    )
    monkeypatch.setattr(
        yyft_serial10, "is_retry_grant_category", lambda eid, since: False,
    )
    monkeypatch.setattr(
        yyft_serial10, "recent_failure_evidence", lambda eid, since: "(evidence)",
    )
    monkeypatch.setattr(
        yyft_serial10, "_latest_exc_type", lambda eid, since: "",
    )


def _fake_await_terminal_always_failed(_name, _eid, **_kwargs):
    return {"screenplay_status": "failed", "screenplay_error": "(failure)"}


def test_red_a_transient_evidence_takes_the_retry_branch(
    monkeypatch, tmp_path,
) -> None:
    """红灯 a）构造瞬时证据 → 走重试分支：classify_failure_family 报
    transient 时，cmd_run 必须自动继续（不停轮），直到该集在自动恢复次数内
    转 ready。"""
    _prep_single_episode_run(monkeypatch, tmp_path)
    sleeps: list[float] = []
    monkeypatch.setattr(yyft_serial10.time, "sleep", lambda s: sleeps.append(s))
    starts = {"n": 0}

    def fake_start_or_resume(_name, _eid):
        starts["n"] += 1
        return True

    monkeypatch.setattr(yyft_serial10, "start_or_resume", fake_start_or_resume)
    monkeypatch.setattr(
        yyft_serial10, "classify_failure_family",
        lambda eid, since: ("transient", "provider ReadTimeout（构造证据）"),
    )
    terminal_calls = {"n": 0}

    def fake_await_terminal(_name, _eid, **_kwargs):
        terminal_calls["n"] += 1
        if terminal_calls["n"] <= 2:
            return {"screenplay_status": "failed", "screenplay_error": "(transient)"}
        return {"screenplay_status": "ready"}

    monkeypatch.setattr(yyft_serial10, "await_terminal", fake_await_terminal)

    rc = yyft_serial10.cmd_run(SimpleNamespace(start_from="", single_pass=True))

    assert rc == 0
    assert starts["n"] == 3  # 初次 + 2 次自动重试后第 3 次成功
    assert sleeps == [60.0, 120.0]  # 阶梯退避的前两级


def test_red_b_quality_gate_evidence_stops_immediately(
    monkeypatch, tmp_path,
) -> None:
    """红灯 b）quality_gate 证据 → 立即停：内容族是真信号，一次都不重试、
    不 sleep，直接停轮等人工 RCA。"""
    _prep_single_episode_run(monkeypatch, tmp_path)
    sleeps: list[float] = []
    monkeypatch.setattr(yyft_serial10.time, "sleep", lambda s: sleeps.append(s))
    starts = {"n": 0}

    def fake_start_or_resume(_name, _eid):
        starts["n"] += 1
        return True

    monkeypatch.setattr(yyft_serial10, "start_or_resume", fake_start_or_resume)
    monkeypatch.setattr(
        yyft_serial10, "classify_failure_family",
        lambda eid, since: ("content", "quality_gate（构造证据）"),
    )
    monkeypatch.setattr(
        yyft_serial10, "await_terminal", _fake_await_terminal_always_failed,
    )

    rc = yyft_serial10.cmd_run(SimpleNamespace(start_from="", single_pass=True))

    assert rc == 4
    assert starts["n"] == 1
    assert sleeps == []


def test_red_c_backoff_ladder_is_60_120_300_then_stops(
    monkeypatch, tmp_path,
) -> None:
    """红灯 c）阶梯时长断言：瞬时故障持续复现（真实故障，不是一次性抖动）时，
    三次退避必须严格是 60s→120s→300s，用满 TRANSIENT_RETRY_MAX 次后停轮，
    并把三次证据都汇总进日志（用真实 LOG 文件核对，不只看返回码）。"""
    _prep_single_episode_run(monkeypatch, tmp_path)
    sleeps: list[float] = []
    monkeypatch.setattr(yyft_serial10.time, "sleep", lambda s: sleeps.append(s))
    starts = {"n": 0}

    def fake_start_or_resume(_name, _eid):
        starts["n"] += 1
        return True

    monkeypatch.setattr(yyft_serial10, "start_or_resume", fake_start_or_resume)
    calls = {"n": 0}

    def fake_classify(_eid, _since):
        calls["n"] += 1
        return "transient", f"第 {calls['n']} 次构造证据"

    monkeypatch.setattr(yyft_serial10, "classify_failure_family", fake_classify)
    monkeypatch.setattr(
        yyft_serial10, "await_terminal", _fake_await_terminal_always_failed,
    )

    rc = yyft_serial10.cmd_run(SimpleNamespace(start_from="", single_pass=True))

    assert rc == 4
    assert sleeps == list(yyft_serial10.TRANSIENT_RETRY_BACKOFF_S) == [60.0, 120.0, 300.0]
    # 初次尝试 + TRANSIENT_RETRY_MAX 次自动重试 = 4 次 start_or_resume。
    assert starts["n"] == yyft_serial10.TRANSIENT_RETRY_MAX + 1
    log_text = (tmp_path / "serial10.log").read_text(encoding="utf-8")
    assert "三次证据汇总" in log_text
    assert "第 1 次" in log_text and "第 2 次" in log_text and "第 3 次" in log_text


# ---------------------------------------------------------------------------
# 3) 混合形态精化（2026-08-24，真实第 30 轮 EP7 事故）：端到端红灯
# ---------------------------------------------------------------------------

def test_red_mixed_shape_quality_gate_plus_interrupted_call_retries_end_to_end(
    monkeypatch, tmp_path,
) -> None:
    """红灯：混合形态 → 重试分支，端到端（不 mock classify_failure_family，
    走真实分类器 + 真实 sqlite 文件）。第一次尝试落库"quality_gate 表象 +
    INTERRUPTED 调用"（真实第 30 轮 EP7 事故形态：场景发现被供应商超时打断，
    资产映射空手而归才连带报 quality_gate），cmd_run 必须把它送进瞬时族自动
    重试而不是当真门禁信号停轮；第二次尝试成功后整轮 READY。"""
    fake_root = tmp_path / "fakeroot"
    (fake_root / "data").mkdir(parents=True)
    monkeypatch.setattr(yyft_serial10, "ROOT", fake_root)
    monkeypatch.setattr(yyft_serial10, "LOG", fake_root / "logs" / "serial10.log")
    monkeypatch.setattr(db, "DB_PATH", fake_root / "data" / "manju.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()

    monkeypatch.setattr(yyft_serial10, "EPISODES", [("EP1", EP1)])
    monkeypatch.setattr(
        yyft_serial10, "status_of",
        lambda eid: {"screenplay_status": "failed", "active": False},
    )
    monkeypatch.setattr(
        yyft_serial10, "is_retry_grant_category", lambda eid, since: False,
    )
    sleeps: list[float] = []
    monkeypatch.setattr(yyft_serial10.time, "sleep", lambda s: sleeps.append(s))
    starts = {"n": 0}

    def fake_start_or_resume(_name, _eid):
        starts["n"] += 1
        if starts["n"] == 1:
            # 模拟"这次尝试真的产生了新证据"：写入时间戳必须晚于 cmd_run 刚
            # 捕获的 since，否则会被 classify_failure_family 的 ts>=since
            # 窗口过滤掉，判不出混合形态。
            conn = db.get_conn()
            now_ts = time.time()
            _insert_provider_call(
                conn, id=9100, ts=now_ts, status="INTERRUPTED", latency_ms=302000,
                error="HiAgent 场景发现请求超时（httpx.ReadTimeout，302.0s）；请求结果不确定",
            )
            _insert_error_log(
                conn, id="ERR-20260824-hybrid01", ts=now_ts,
                category="quality_gate", category_label="质量校验", code="QA",
                exc_type="PrepPackGateError",
                message="资产映射未能 100% 解析（已尝试身份/场景发现，调用次数：场景 1）",
            )
            conn.commit()
        return True

    monkeypatch.setattr(yyft_serial10, "start_or_resume", fake_start_or_resume)
    terminal_calls = {"n": 0}

    def fake_await_terminal(_name, _eid, **_kwargs):
        terminal_calls["n"] += 1
        if terminal_calls["n"] == 1:
            return {
                "screenplay_status": "failed",
                "screenplay_error": "quality_gate（混合形态：场景发现超时诱发）",
            }
        return {"screenplay_status": "ready"}

    monkeypatch.setattr(yyft_serial10, "await_terminal", fake_await_terminal)

    rc = yyft_serial10.cmd_run(SimpleNamespace(start_from="", single_pass=True))

    assert rc == 0
    assert starts["n"] == 2  # 初次 + 1 次自动重试后成功
    assert sleeps == [60.0]
    log_text = (fake_root / "logs" / "serial10.log").read_text(encoding="utf-8")
    assert "内容失败疑由瞬时中断诱发" in log_text
