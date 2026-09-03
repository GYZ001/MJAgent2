"""app.media_exec.reference_pool_gate._time_anchor_advisories_for_job（WS9）。

覆盖：project_id/episode_no 正确从 job/episodes 派生后转交
``app.validators.resource_forecast.character_time_anchor_advisories``；
episode 行查不到时 episode_no 传 None（不崩，不假装有数据）。
"""
from __future__ import annotations

from app.media_exec import reference_pool_gate


class _Conn:
    """最小可用连接桩：只需要支持一次 SELECT episode_no 查询。"""

    def __init__(self, episode_no: int | None) -> None:
        self._episode_no = episode_no

    def execute(self, sql: str, params: tuple) -> "_Conn":
        assert "episode_no" in sql
        return self

    def fetchone(self):
        return {"episode_no": self._episode_no} if self._episode_no is not None else None


def test_time_anchor_advisories_for_job_derives_project_and_episode_no(monkeypatch):
    captured: dict = {}

    def fake_advisories(*, shot, project_id, episode_no, conn):
        captured["shot"] = shot
        captured["project_id"] = project_id
        captured["episode_no"] = episode_no
        return ["[STORYBOARD_PACK_PORTRAIT_TIME_ANCHOR_MISMATCH][未拦截] ..."]

    monkeypatch.setattr(
        "app.validators.resource_forecast.character_time_anchor_advisories", fake_advisories,
    )
    conn = _Conn(episode_no=3)
    job = {"episode_id": "ep1", "project_id": "p1"}
    shot_model = object()

    result = reference_pool_gate._time_anchor_advisories_for_job(conn, job, shot_model)

    assert result == ["[STORYBOARD_PACK_PORTRAIT_TIME_ANCHOR_MISMATCH][未拦截] ..."]
    assert captured["project_id"] == "p1"
    assert captured["episode_no"] == 3
    assert captured["shot"] is shot_model


def test_time_anchor_advisories_for_job_episode_no_none_when_episode_missing(monkeypatch):
    captured: dict = {}

    def fake_advisories(*, shot, project_id, episode_no, conn):
        captured["episode_no"] = episode_no
        return []

    monkeypatch.setattr(
        "app.validators.resource_forecast.character_time_anchor_advisories", fake_advisories,
    )
    conn = _Conn(episode_no=None)
    job = {"episode_id": "does-not-exist", "project_id": "p1"}

    result = reference_pool_gate._time_anchor_advisories_for_job(conn, job, object())

    assert result == []
    assert captured["episode_no"] is None
