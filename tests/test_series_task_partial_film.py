"""连播任务台「成片缺段」可见性回归。

取证背景（2026-09-27，proj_c89e1d2fa4be《顾念长安》第 1 集）：合成时已经把
``shots_total``/``skipped_shot_nos``/``skip_reasons`` 算出来并持久化到
``episode.edit-report.json``（``app.media_exec.concat``），CinemaPage 也已经在
用；但连播任务台的列表/详情只报「完成」，看不到「这一集其实缺了第几段」。

本文件验证 ``app.domain.series_ops.final_status.final_partial_status``/
``partial_episodes_in_range`` 与 ``tasks.py`` 序列化处的接线：字段名/形状直接
复用 CinemaPage 已用的 ``final_is_partial``/``skipped_shot_nos``/
``skip_reasons``/``final_video_stale``，不重新计算、不新造第二套判据；判据只挂
已持久化的产物文件，不碰 ``app/media_exec/concat.py`` 本身（只读复用其
``_final_video_path``/``_read_edit_report``/``_final_video_is_stale``，与
``app.domain.series_ops.merge`` 现有的复用方式一致）。

``final_video_stale`` 覆盖审查发现的缺口：采纳新镜头只给 ``final/episode.mp4``
打 ``.stale`` 标记，不改 ``episode.edit-report.json``——若不带这个字段，连播
任务台会在采纳之后继续展示合成时那一刻的旧缺段清单，且没有任何「已过期」提示。
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app import api, config, db
from app.capabilities.loader import ensure_catalog_loaded
from app.capabilities.bus import reset_command_bus_for_tests, set_request_approval_token
from app.capabilities.policy import reset_approvals_for_tests
from app.domain.series_ops import final_status as series_final_status
from app.local_session import APPROVAL_HEADER, ensure_session_secret, set_request_session_id
from app.media_exec.concat import _final_video_path
from tests.conftest import SessionTestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "series-tasks-partial.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    db.init_db()
    ensure_catalog_loaded()
    reset_approvals_for_tests()
    reset_command_bus_for_tests()

    test_app = FastAPI()

    @test_app.middleware("http")
    async def inject_approval_token(request: Request, call_next):
        set_request_approval_token(request.headers.get(APPROVAL_HEADER))
        set_request_session_id(ensure_session_secret())
        try:
            return await call_next(request)
        finally:
            set_request_approval_token(None)
            set_request_session_id(None)

    test_app.include_router(api.router)
    with TestClient(test_app) as test_client:
        yield SessionTestClient(test_client)


def _seed_episodes(project_id: str, episode_nos: list[int]) -> None:
    conn = db.get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO projects(id,name,created_at) VALUES(?,?,0)", (project_id, "演示项目"),
    )
    conn.executemany(
        """INSERT INTO episodes(id,project_id,episode_no,title,status,created_at)
           VALUES(?,?,?,?, 'planned', 0)""",
        [(f"{project_id}-ep{no}", project_id, no, f"第{no}集") for no in episode_nos],
    )
    conn.commit()


def _write_final(
    project_id: str, episode_no: int, *, timeline: dict | None, extra_report: dict | None = None, stale: bool = False,
) -> None:
    """在 ``final/episode.mp4`` 旁落一份 ``episode.edit-report.json``——与合成
    真实落盘的形状一致（只是不用真 ffmpeg），``timeline=None`` 时只造视频文件、
    不造报告，覆盖「有成片但没有 timeline 信息」这条分支。顺带把项目行插进
    ``projects`` 表（``INSERT OR IGNORE``，幂等）——``_final_video_is_stale``
    要读项目的画幅/AI 标识设置，没有项目行会 ``LookupError``，与
    ``_seed_episodes`` 各自独立、互不冲突。``stale=True`` 时额外落一份
    ``.stale`` 标记（``_invalidate_final_video()`` 真实产生的那份，同名同址）。
    """
    conn = db.get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO projects(id,name,created_at) VALUES(?,?,0)", (project_id, "测试项目"),
    )
    conn.commit()
    final_path = _final_video_path(project_id, episode_no)
    final_path.write_bytes(b"fake-mp4")
    if timeline is not None:
        report = {"episode_no": episode_no, "timeline": timeline, **(extra_report or {})}
        final_path.with_name("episode.edit-report.json").write_text(
            json.dumps(report, ensure_ascii=False), encoding="utf-8",
        )
    if stale:
        final_path.with_suffix(".stale").write_bytes(b"")


_PARTIAL_TIMELINE = {
    "partial": True,
    "shots_total": 12,
    "skipped_shot_nos": [7, 3],
    "skip_reasons": {"3": "供应商拒收", "7": "供应商拒收"},
    "included_shot_nos": [1, 2, 4, 5, 6, 8, 9, 10, 11, 12],
    "missing_model_shot_nos": [3, 7],
    "auto_adopted_shot_nos": [],
}

_FULL_TIMELINE = {
    "partial": False,
    "shots_total": 5,
    "skipped_shot_nos": [],
    "skip_reasons": {},
    "included_shot_nos": [1, 2, 3, 4, 5],
    "missing_model_shot_nos": [],
    "auto_adopted_shot_nos": [],
}


# --------------------------------------------------------- unit：final_status

def test_final_partial_status_reads_persisted_timeline(client) -> None:
    _write_final("p1", 2, timeline=_PARTIAL_TIMELINE)
    status = series_final_status.final_partial_status(db.get_conn(), "p1", 2)
    assert status["final_is_partial"] is True
    assert status["skipped_shot_nos"] == [3, 7]  # 排序而不是原样透传
    assert status["skip_reasons"] == {"3": "供应商拒收", "7": "供应商拒收"}
    assert status["final_video_stale"] is False


def test_final_partial_status_false_when_no_final_video(client) -> None:
    status = series_final_status.final_partial_status(db.get_conn(), "p1", 9)
    assert status == {
        "final_is_partial": False, "skipped_shot_nos": [], "skip_reasons": {}, "final_video_stale": False,
    }


def test_final_partial_status_false_when_video_exists_but_no_report(client) -> None:
    """有成片但没有报告（旧版合成产物/报告写入失败）不能误报缺段——空集合
    不等于没查，这里是查了但确实没有 timeline 信息可读。"""
    _write_final("p1", 3, timeline=None)
    status = series_final_status.final_partial_status(db.get_conn(), "p1", 3)
    assert status == {
        "final_is_partial": False, "skipped_shot_nos": [], "skip_reasons": {}, "final_video_stale": False,
    }


def test_final_partial_status_false_when_timeline_says_full(client) -> None:
    _write_final("p1", 4, timeline=_FULL_TIMELINE)
    status = series_final_status.final_partial_status(db.get_conn(), "p1", 4)
    assert status["final_is_partial"] is False
    assert status["skipped_shot_nos"] == []
    assert status["final_video_stale"] is False


def test_final_partial_status_stale_flag_true_after_adoption_invalidates_final(client) -> None:
    """审查发现的缺口：采纳新镜头只给 final/episode.mp4 打 .stale 标记，不改
    episode.edit-report.json——旧的缺段清单仍然读得到，但必须同时带
    final_video_stale=True，调用方才知道这份清单可能已经不准了。"""
    _write_final("p1", 5, timeline=_PARTIAL_TIMELINE, stale=True)
    status = series_final_status.final_partial_status(db.get_conn(), "p1", 5)
    assert status["final_video_stale"] is True
    assert status["final_is_partial"] is True  # 旧报告仍然如实转述，只是多了「可能过期」信号
    assert status["skipped_shot_nos"] == [3, 7]


def test_final_partial_status_stale_flag_true_on_canvas_drift_even_without_stale_file(client) -> None:
    """.stale 只是覆盖「采纳变化」这一种触发方式；``_final_video_is_stale`` 还会
    比画幅/AI 标识——这里直接复用 concat.py 的那份判据（不重新实现第二套），
    项目画幅改到 16:9 但报告里记录的还是旧的 9:16 时同样要判过期。"""
    _write_final("p1", 6, timeline=_FULL_TIMELINE, extra_report={"canvas": {"width": 1080, "height": 1920}})
    conn = db.get_conn()
    conn.execute("UPDATE projects SET aspect_ratio='16:9' WHERE id=?", ("p1",))
    conn.commit()
    status = series_final_status.final_partial_status(conn, "p1", 6)
    assert status["final_video_stale"] is True


def test_partial_episodes_in_range_only_lists_partial_ones(client) -> None:
    _write_final("p1", 1, timeline=_FULL_TIMELINE)
    _write_final("p1", 2, timeline=_PARTIAL_TIMELINE)
    # 第 3 集尚无成片，第 4 集有成片但完整——两者都不应出现在结果里。
    found = series_final_status.partial_episodes_in_range(db.get_conn(), "p1", 1, 4)
    assert [item["episode_no"] for item in found] == [2]
    assert found[0]["skipped_shot_nos"] == [3, 7]
    assert found[0]["skip_reasons"] == {"3": "供应商拒收", "7": "供应商拒收"}
    assert found[0]["final_video_stale"] is False


def test_partial_episodes_in_range_skips_missing_episode_nos_without_side_effects(client) -> None:
    """区间内缺集（还没生成到那一集）不该被探测——``_final_video_path`` 会顺带
    mkdir，对压根不存在的集号探测会在磁盘上留下空目录这种多余副作用。"""
    _write_final("p1", 1, timeline=_PARTIAL_TIMELINE)
    found = series_final_status.partial_episodes_in_range(db.get_conn(), "p1", 1, 3, missing_episode_nos=[2, 3])
    assert [item["episode_no"] for item in found] == [1]
    assert not (config.PROJECTS_DIR / "p1" / "episodes" / "2").exists()
    assert not (config.PROJECTS_DIR / "p1" / "episodes" / "3").exists()


# --------------------------------------------------------------- API：list

def test_list_tasks_surfaces_partial_episodes_for_the_range(client) -> None:
    _seed_episodes("p1", [1, 2, 3])
    _write_final("p1", 1, timeline=_FULL_TIMELINE)
    _write_final("p1", 2, timeline=_PARTIAL_TIMELINE)
    client.post("/api/projects/p1/series-tasks", json={"ranges": [{"episode_from": 1, "episode_to": 3}]})

    body = client.get("/api/projects/p1/series-tasks").json()
    task = body["tasks"][0]
    assert task["partial_episodes"] == [{
        "episode_no": 2, "final_is_partial": True,
        "skipped_shot_nos": [3, 7], "skip_reasons": {"3": "供应商拒收", "7": "供应商拒收"},
        "final_video_stale": False,
    }]


def test_list_tasks_partial_episode_flags_stale_when_adoption_invalidated_final(client) -> None:
    """端到端覆盖审查发现的缺口：任务列表的 partial_episodes 必须带上
    final_video_stale，否则用户点「去成片台」采纳新镜头之后，任务台会一直
    展示这份已经过期的旧缺段清单而不自知。"""
    _seed_episodes("p1", [1, 2])
    _write_final("p1", 2, timeline=_PARTIAL_TIMELINE, stale=True)
    client.post("/api/projects/p1/series-tasks", json={"ranges": [{"episode_from": 1, "episode_to": 2}]})

    task = client.get("/api/projects/p1/series-tasks").json()["tasks"][0]
    assert task["partial_episodes"] == [{
        "episode_no": 2, "final_is_partial": True,
        "skipped_shot_nos": [3, 7], "skip_reasons": {"3": "供应商拒收", "7": "供应商拒收"},
        "final_video_stale": True,
    }]


def test_list_tasks_partial_episodes_empty_when_all_episodes_full(client) -> None:
    _seed_episodes("p1", [1, 2])
    _write_final("p1", 1, timeline=_FULL_TIMELINE)
    _write_final("p1", 2, timeline=_FULL_TIMELINE)
    client.post("/api/projects/p1/series-tasks", json={"ranges": [{"episode_from": 1, "episode_to": 2}]})

    task = client.get("/api/projects/p1/series-tasks").json()["tasks"][0]
    assert task["partial_episodes"] == []


# ------------------------------------------------------------- API：detail

def test_task_detail_marks_each_episode_entry_with_final_partial_fields(client) -> None:
    _seed_episodes("p1", [1, 2, 3])
    _write_final("p1", 2, timeline=_PARTIAL_TIMELINE)
    client.post("/api/projects/p1/series-tasks", json={"ranges": [{"episode_from": 1, "episode_to": 3}]})
    task_id = client.get("/api/projects/p1/series-tasks").json()["tasks"][0]["task_id"]

    episodes = client.get(f"/api/projects/p1/series-tasks/{task_id}").json()["episodes"]
    by_no = {e["episode_no"]: e for e in episodes}
    assert by_no[1]["final_is_partial"] is False
    assert by_no[1]["skipped_shot_nos"] == []
    assert by_no[1]["final_video_stale"] is False
    assert by_no[2]["final_is_partial"] is True
    assert by_no[2]["skipped_shot_nos"] == [3, 7]
    assert by_no[2]["skip_reasons"] == {"3": "供应商拒收", "7": "供应商拒收"}
    assert by_no[2]["final_video_stale"] is False
    assert by_no[3]["final_is_partial"] is False


def test_task_detail_marks_episode_stale_after_adoption_invalidates_final(client) -> None:
    _seed_episodes("p1", [1, 2])
    _write_final("p1", 2, timeline=_PARTIAL_TIMELINE, stale=True)
    client.post("/api/projects/p1/series-tasks", json={"ranges": [{"episode_from": 1, "episode_to": 2}]})
    task_id = client.get("/api/projects/p1/series-tasks").json()["tasks"][0]["task_id"]

    episodes = client.get(f"/api/projects/p1/series-tasks/{task_id}").json()["episodes"]
    by_no = {e["episode_no"]: e for e in episodes}
    assert by_no[2]["final_video_stale"] is True
    assert by_no[2]["skipped_shot_nos"] == [3, 7]  # 旧清单仍如实转述，前端靠 stale 标记改文案，不是把数据吞掉
