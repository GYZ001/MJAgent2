"""技术校验（含字幕闸门）不通过时的收尾与 Supervisor 侧的 Issue 露出。

2026-09-14 第 1 集镜 17：字幕闸门拦截 → Worker 抛 ProviderError → 记成「未分类供应商失败 /
manual_review」→ Supervisor 当外部终态停手 → 整集判失败。修法：Supervisor 模式下任务照常
收工、版本留作不可采用候选；覆盖循环把技术拒绝以 VIDEO_TECHNICAL_CONTRACT_FAILED 露出并 L1 重抽。
"""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from app import db as db_mod
from app.hiagent import ProviderError
from app.media_exec import enqueue as enqueue_mod
from app.media_exec import run_job_steps
from app.media_pipeline import retry_policy
from app.orchestration import media_scheduler
from app.video_supervisor import issues_cascade


# ---------------------------------------------------------------------------
# settle_technical_failure
# ---------------------------------------------------------------------------

def _wire(monkeypatch, *, set_job_ok: bool = True, limit: int = 2):
    calls: dict[str, list] = {"set_job": [], "settle": [], "reconcile": [], "resubmit": []}
    monkeypatch.setattr(run_job_steps, "_set_job", lambda job_id, status, lease_owner=None: calls["set_job"].append((job_id, status)) or set_job_ok)
    monkeypatch.setattr(media_scheduler, "settle_budget", lambda job_id, cost, success: calls["settle"].append((job_id, cost, success)))
    monkeypatch.setattr(enqueue_mod, "reconcile_episode_generation_status", lambda ep: calls["reconcile"].append(ep))
    monkeypatch.setattr(run_job_steps, "resubmit_after_technical_failure", lambda job, resubmits, meta: calls["resubmit"].append(resubmits))
    monkeypatch.setattr(retry_policy, "technical_resubmit_limit", lambda: limit)
    return calls


def _settle(resubmits: int, supervisor: bool) -> None:
    run_job_steps.settle_technical_failure(
        {"episode_id": "ep_1", "shot_id": "shot_1", "after_shot_id": None}, "job_1", "owner", 1.5, resubmits, {}, supervisor,
        version_id="ver_1",
    )


def test_supervisor_mode_settles_job_without_resubmit_or_error(monkeypatch) -> None:
    calls = _wire(monkeypatch)
    _settle(resubmits=5, supervisor=True)  # 超过上限也不抛：重抽权在 Supervisor
    assert calls["set_job"] == [("job_1", "succeeded")]
    assert calls["settle"] == [("job_1", 1.5, True)] and calls["reconcile"] == ["ep_1"]
    assert calls["resubmit"] == []


def test_worker_mode_resubmits_within_limit(monkeypatch) -> None:
    calls = _wire(monkeypatch, limit=2)
    _settle(resubmits=1, supervisor=False)
    assert calls["set_job"] == [("job_1", "succeeded")] and calls["resubmit"] == [1]


def test_worker_mode_raises_when_limit_exhausted(monkeypatch) -> None:
    calls = _wire(monkeypatch, limit=2)
    monkeypatch.setattr(run_job_steps, "_technical_issue_summary", lambda version_id: "画面叠加了字幕：『靠山宗』")
    with pytest.raises(ProviderError) as excinfo:
        _settle(resubmits=2, supervisor=False)
    assert calls["set_job"] == [] and calls["resubmit"] == []
    exc = excinfo.value
    assert isinstance(exc, run_job_steps.VideoTechnicalGateExhausted)
    assert "连续 3 次未通过" in str(exc) and "靠山宗" in str(exc) and "生成台" in str(exc)
    # 报错码系统按「质量校验」展示原因原样，不再是「大模型/外部服务调用失败，可稍后重试」
    from app.errors import classify
    assert classify(exc) == ("quality_gate", "QA")
    assert exc.failure.disposition.value == "manual_review" and exc.retryable is False


def test_lost_lease_settles_nothing(monkeypatch) -> None:
    calls = _wire(monkeypatch, set_job_ok=False)
    _settle(resubmits=0, supervisor=False)
    assert calls["settle"] == [] and calls["resubmit"] == []


# ---------------------------------------------------------------------------
# _issues_from_candidate
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db_mod.SCHEMA)
    for statement in db_mod.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    return conn


def _insert_version(conn, version_id: str, version_no: int, *, passed: bool, status: str = "succeeded") -> None:
    technical = {"passed": passed, "issues": [] if passed else [
        {"code": "subtitle_overlay", "severity": "blocker", "subject": "video",
         "message": "画面叠加了字幕：『靠山宗』（第 1 帧，画面底部）", "repairable": True},
    ], "evidence": {}}
    conn.execute(
        "INSERT INTO shot_versions (id, shot_id, version_no, status, qa_json, technical_validation_json, prompt_text, idem_key, created_at)"
        " VALUES (?,?,?,?,?,?,'',?,?)",
        (version_id, "shot_1", version_no, status, "{}", json.dumps(technical, ensure_ascii=False), f"idem_{version_id}", float(version_no)),
    )


def _entry(best: str | None = None):
    return SimpleNamespace(shot_id="shot_1", shot_no=17, best_version_id=best)


def test_rejected_candidate_surfaces_technical_issue_when_no_best() -> None:
    conn = _conn()
    _insert_version(conn, "ver_a", 1, passed=False)
    issues = issues_cascade._issues_from_candidate(conn, _entry())
    assert issues[0].code == "VIDEO_TECHNICAL_CONTRACT_FAILED"  # 其后可跟 QA 合同事实（WARNING）
    assert "靠山宗" in issues[0].message and issues[0].repairable is True
    assert issues[0].evidence.get("version_id") == "ver_a" or issues[0].evidence.get("rule_id") == "subtitle_overlay"


def test_latest_rejected_version_wins() -> None:
    conn = _conn()
    _insert_version(conn, "ver_old", 1, passed=False)
    _insert_version(conn, "ver_new", 2, passed=False)
    issues = issues_cascade._issues_from_candidate(conn, _entry())
    assert issues and issues[0].evidence.get("rule_id") == "subtitle_overlay"
    assert any("ver_new" in json.dumps(i.model_dump(mode="json"), ensure_ascii=False) for i in issues)


def test_best_version_takes_precedence_over_rejected_ones() -> None:
    conn = _conn()
    _insert_version(conn, "ver_bad", 1, passed=False)
    _insert_version(conn, "ver_good", 2, passed=True)
    assert issues_cascade._issues_from_candidate(conn, _entry(best="ver_good")) == []


@pytest.mark.parametrize("status", ["failed", "running", "cleared"])
def test_non_succeeded_versions_are_ignored(status: str) -> None:
    conn = _conn()
    _insert_version(conn, "ver_x", 1, passed=False, status=status)
    assert issues_cascade._issues_from_candidate(conn, _entry()) == []
