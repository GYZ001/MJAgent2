"""账号视频并发触顶：入队异常翻译成可等待的告警（L0，不付费），不是阻断也不进人工。"""
from __future__ import annotations

from app.quota import QuotaExceeded
from app.video_issues import IssueSeverity, issues_from_enqueue_error


def test_concurrency_quota_becomes_a_wait_warning():
    exc = QuotaExceeded(gate="concurrency", tier="max", limit=10, used=10, remaining=0,
                        message="video 同时在跑的任务已达 max 档上限（10 个），请等待现有任务结束后再试")
    issues = issues_from_enqueue_error(exc, shot_id="shot-1", shot_no=1)
    assert [i.code for i in issues] == ["VIDEO_ENQUEUE_WAIT_CONCURRENCY"]
    assert issues[0].severity is IssueSeverity.WARNING
    assert (issues[0].evidence or {}).get("recommended_level") == "L0"


def test_other_quota_gates_stay_blocking():
    exc = QuotaExceeded(gate="video_seconds", tier="free", limit=60, used=60, remaining=0, message="视频时长用尽")
    issues = issues_from_enqueue_error(exc, shot_id="shot-1", shot_no=1)
    assert issues[0].code == "VIDEO_ENQUEUE_OPERATION_FAILED"
    assert issues[0].severity is IssueSeverity.BLOCKER
