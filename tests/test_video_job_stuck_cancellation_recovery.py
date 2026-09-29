"""2026-09-29 生产事故复盘：queued+cancellation_requested=1 的视频任务永久卡死。

时间线（B 库只读核实，job_92f3cd206935 / shot_2822971a4398）：
07:41:37 带非法模板的 ``prompt_override`` 请求 409（STORYBOARD_IDENTITY_
REPAIR_REQUIRED），预检 job 留在 status='waiting_human'、video_slot_active=1、
cancellation_requested=0，因为该镜已有成功采用版（ver_7548e9cac1de）。
在这之后、07:42:19 之前，``_stale_lease_sweeper``（每 60 秒跑一次
``reconcile_stalled_video_jobs``）判定这个无 version_id 的预检 job 冗余，把它
标成 status='cancelled', cancellation_requested=1，但**没有释放
video_slot_active**——它继续占着 ``uq_jobs_active_video_shot`` 这个每镜唯一
的活动槽。07:42:19 同镜合法模板的第二次请求命中 ``_begin_video_preflight_job``
的"复用现有活动槽"分支，把这一行认领回 status='waiting_retry' 继续往下走到
status='queued'、version_id=ver_3923b209c1a2，但认领分支只清了 lease/error，
没有清 cancellation_requested——任务带着这个标记停在 queued。
``app/media_exec/dispatch.py`` 的两版派发查询都无条件排除
``cancellation_requested=1``，这一行因此没有任何 worker 会再碰它，20 分钟里
唯一变化是用户手动调用 ``/shots/{id}/video/stop``。

三处修复（均见对应模块 docstring/注释）：
  1. ``app/media_exec/job_recovery.py`` 的冗余预检收口 UPDATE 现在同时把
     ``video_slot_active`` 置 0，不再让一个已判定"冗余"的空壳继续占槽。
  2. ``app/media_exec/enqueue.py`` 的 ``_begin_video_preflight_job`` 认领分支
     现在显式把 ``cancellation_requested``/``abandoned``/``reason_code``/
     ``reason_text`` 一并清空——这是防线二：不管 video_slot_active=1 的历史
     行是被谁、以什么方式标上取消的，认领时都必须真正重新开始。
  3. ``app/orchestration/media_scheduler.py`` 新增
     ``converge_stuck_cancelled_queued_jobs``，由
     ``reconcile_stalled_video_jobs`` 周期调用：任何 status='queued' 且
     cancellation_requested=1 的视频任务都会被主动收敛为终态，即使前两条
     防线都被绕过，也不会再无声卡死 20 分钟。
"""
from __future__ import annotations

import sqlite3

import pytest

from app import compiler, db, worker
from app.orchestration import media_scheduler
from app.schemas import Bible, Character, World
from tests.conftest import patch_worker_everywhere


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for stmt in db.MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    return conn


def _seed_shot_with_succeeded_take(conn: sqlite3.Connection) -> None:
    """一镜一集，镜头已有一版成功并被采用——冗余预检收口的触发前提。"""
    bible = Bible(
        characters=[Character(name="A", role="lead", appearance_canonical="black hair")],
        world=World(visual_style_canonical="anime drama style"),
    )
    conn.execute(
        "INSERT INTO projects(id,name,status,bible_json,created_at) VALUES(?,?,?,?,?)",
        ("p1", "P", "created", bible.model_dump_json(), 1.0),
    )
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,status,created_at) VALUES(?,?,?,?,?)",
        ("e1", "p1", 1, "confirmed", 1.0),
    )
    conn.execute(
        """INSERT INTO shots(
               id,episode_id,shot_no,duration_s,shot_size,camera_move,scene_setting,
               characters,action_desc,source_excerpt,dialogues,transition,
               continuity_from_prev,first_frame_desc,last_frame_desc,scene_status,
               adopted_version_id
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "s1", "e1", 1, 5, "中景", "固定", "室内", '["A"]',
            "A把桌上的文件整理整齐。", "A把桌上的文件整理整齐。", "[]", "硬切",
            0, "A坐在散开的文件前。", "A面前的文件已经整齐平码。", "approved",
            "v-old",
        ),
    )
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,created_at
           ) VALUES('v-old','s1',1,'旧提示词','idem-old','succeeded',1.0)"""
    )
    conn.commit()


def _patch_enqueue_runtime(monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection) -> None:
    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.media_scheduler, "get_conn", lambda: conn)
    patch_worker_everywhere(monkeypatch, "ensure_media_trace", lambda **_kwargs: (None, None))
    monkeypatch.setattr(worker.media_scheduler, "reserve_budget", lambda *_a, **_k: True)
    patch_worker_everywhere(monkeypatch, "_enqueue_for_current_status", lambda _job_id: None)


def _identity_repair_rejection(*_args, **_kwargs) -> str:
    raise ValueError(
        "[STORYBOARD_IDENTITY_REPAIR_REQUIRED] prompt_text 须在每句发声时机写一次 "
        "{{speech:utterance_id}}；请修订当前片段的发声与人物合同后重试，其他片段可继续。"
    )


def test_two_request_sequence_no_longer_leaves_job_stuck_queued_and_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """端到端复现生产事故的两次请求；修复后不应再出现 queued+cancellation_requested=1。"""
    conn = _conn()
    _seed_shot_with_succeeded_take(conn)
    _patch_enqueue_runtime(monkeypatch, conn)

    # 第一次请求：非法模板，identity 校验拒绝（对应生产 07:41:37 的 409）。
    monkeypatch.setattr(compiler, "compile_prompt", _identity_repair_rejection)
    with pytest.raises(ValueError, match="STORYBOARD_IDENTITY_REPAIR_REQUIRED"):
        worker.enqueue_shot("s1", prompt_override="缺少发声标签的非法模板")

    first_job = conn.execute(
        "SELECT * FROM jobs WHERE shot_id='s1' AND kind='video'"
    ).fetchone()
    assert first_job["status"] == "waiting_human"
    assert first_job["version_id"] is None
    assert first_job["cancellation_requested"] == 0

    # 60 秒周期扫描（_stale_lease_sweeper 实际调用间隔）判定这行冗余并收口。
    report = worker.reconcile_stalled_video_jobs()
    assert report["redundant_preflight_closed"] == 1
    closed_job = conn.execute(
        "SELECT * FROM jobs WHERE id=?", (first_job["id"],)
    ).fetchone()
    assert closed_job["status"] == "cancelled"
    assert closed_job["cancellation_requested"] == 1
    # 修复 1：冗余收口必须同时放槽，否则第二次请求只能走复用分支而不是
    # 干净地新建一行。
    assert closed_job["video_slot_active"] == 0

    # 第二次请求：合法模板（对应生产 07:42:19 的 200）。
    monkeypatch.setattr(
        compiler, "compile_prompt",
        lambda shot, *_a, **_kw: "画面：合法提示词 --ratio 9:16 --dur 5",
    )
    second = worker.enqueue_shot("s1", prompt_override="合法提示词")

    assert second.get("task_accepted") or second.get("job_id")
    stuck = conn.execute(
        """SELECT COUNT(*) AS c FROM jobs
           WHERE shot_id='s1' AND kind='video'
             AND status='queued' AND cancellation_requested=1"""
    ).fetchone()["c"]
    assert stuck == 0, "任务不能带着 cancellation_requested=1 卡在 queued"

    # 派发查询（app/media_exec/dispatch.py 两版共用的判据）必须能看见新任务。
    dispatchable = conn.execute(
        """SELECT COUNT(*) AS c FROM jobs
           WHERE shot_id='s1' AND kind='video'
             AND status IN ('queued','waiting_provider')
             AND cancellation_requested=0 AND abandoned=0"""
    ).fetchone()["c"]
    assert dispatchable == 1


def test_reclaimed_preflight_slot_clears_stale_cancellation_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """防线二：不管某一行 video_slot_active=1 时是被谁标上取消的，
    ``_begin_video_preflight_job`` 认领它时都必须清掉 cancellation_requested，
    否则复用后的任务会带着这个标记一路进 queued 却被派发查询永久过滤。"""
    conn = _conn()
    _seed_shot_with_succeeded_take(conn)
    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)

    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,episode_id,project_id,status,video_slot_active,
               cancellation_requested,abandoned,reason_code,reason_text,
               lease_owner,lease_expires_at,created_at,updated_at
           ) VALUES(
               'job-stale','video','s1','e1','p1','cancelled',1,
               1,0,'SUPERSEDED_PREFLIGHT','已有成功采用版，关闭并发产生的冗余校验任务',
               NULL,NULL,1.0,1.0
           )"""
    )
    conn.commit()

    result = worker._begin_video_preflight_job("s1", supervisor_run_id=None)

    assert result["acquired"] is True
    assert result["job_id"] == "job-stale"
    row = conn.execute("SELECT * FROM jobs WHERE id='job-stale'").fetchone()
    assert row["status"] == "waiting_retry"
    assert row["cancellation_requested"] == 0
    assert row["abandoned"] == 0
    # reason_code 会被随后的 set_pipeline_stage(VIDEO_PREFLIGHT_VALIDATING)
    # 覆盖成新阶段说明，不是本修复要断言的字段；只断言它不再是旧的取消原因。
    assert row["reason_code"] != "SUPERSEDED_PREFLIGHT"


def test_stuck_queued_cancelled_job_converges_to_cancelled_terminal_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """防线三：即使前两道防线都被绕过，周期对账也要把
    queued+cancellation_requested=1 的任务收敛成终态，不能永远停在 queued。"""
    conn = _conn()
    _seed_shot_with_succeeded_take(conn)
    monkeypatch.setattr(media_scheduler, "get_conn", lambda: conn)

    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,created_at
           ) VALUES('v-stuck','s1',2,'新提示词','idem-stuck','queued',2.0)"""
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,
               video_slot_active,cancellation_requested,abandoned,
               provider_create_state,provider_non_cancellable,
               created_at,updated_at
           ) VALUES(
               'job-stuck','video','s1','v-stuck','e1','p1','queued',
               1,1,0,'not_started',0,2.0,2.0
           )"""
    )
    conn.commit()

    converged = media_scheduler.converge_stuck_cancelled_queued_jobs(conn, 50)

    assert converged == 1
    row = conn.execute("SELECT status,video_slot_active FROM jobs WHERE id='job-stuck'").fetchone()
    assert row["status"] == "cancelled"
    assert row["video_slot_active"] == 0
    assert conn.execute(
        """SELECT COUNT(*) AS c FROM jobs
           WHERE id='job-stuck' AND status='queued' AND cancellation_requested=1"""
    ).fetchone()["c"] == 0
