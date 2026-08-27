from __future__ import annotations

import pytest

from app.db import get_conn
from app.domain import video_ops
from app import video_supervisor


def _seed_episode(episode_id: str, project_id: str) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO projects(id,name,status,created_at) VALUES(?,?,?,0)",
        (project_id, "P", "created"),
    )
    conn.execute(
        """INSERT INTO episodes(id,project_id,episode_no,status,created_at)
           VALUES(?,?,1,'confirmed',0)""",
        (episode_id, project_id),
    )
    conn.commit()


def test_repair_route_rolls_back_pending_adoption_before_marking_failed_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回归锁：``repair_video_completion_route`` 的顶层 except 块必须在调用
    ``_mark_failed_closed`` / ``recorder.fail`` 之前先回滚。

    真实复现路径：``_deadline_closeout`` 对每个镜头调用
    ``app.evidence.media.select_best_video_candidate`` 采用最佳候选——那个函数
    先 UPDATE ``shots.adopted_version_id`` 与 ``shot_versions.adoption_reason``，
    再调用 ``invalidate_episode_delivery_authority`` 写 ``delivery_packages``，
    最后才一次性 ``conn.commit()``；这几条语句之间没有中间提交点。这里用同一
    连接上的等价占位写入模拟"两条 UPDATE 已经执行、commit 还没来得及跑就抛出
    异常"，不依赖完整 shots/shot_versions 造数。

    ``_mark_failed_closed`` 内部经 ``save_checkpoint`` 写检查点：那个函数的
    真实逻辑是"连接已经在事务中就不再开新事务，直接复用现有事务"，所以它的
    ``conn.commit()`` 会把上面挂起的半途采用一并提交——除非 except 块先回滚。
    """
    conn = get_conn()
    episode_id = "ep_repair_rollback_test"
    project_id = "proj_repair_rollback_test"
    _seed_episode(episode_id, project_id)

    def fake_deadline_closeout(cp, *, run_id, reason):
        del cp, run_id, reason
        conn.execute("CREATE TABLE IF NOT EXISTS fake_pending_adoption(marker TEXT)")
        conn.execute(
            "INSERT INTO fake_pending_adoption(marker) VALUES('half_done_adoption')"
        )
        # Real code would still call invalidate_episode_delivery_authority and
        # only conn.commit() after that returns; we stop here, uncommitted,
        # exactly like that call raising partway through.
        raise RuntimeError("模拟 select_best_video_candidate 写完两条 UPDATE 后、commit 前失败")

    checkpoint_commits: list[bool] = []

    def fake_mark_failed_closed(cp, *, run_id, reason):
        del cp, run_id, reason
        # Mirrors save_checkpoint reusing an already-open transaction instead
        # of starting a fresh one, then committing it.
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE IF NOT EXISTS fake_checkpoint(marker TEXT)")
        conn.execute("INSERT INTO fake_checkpoint(marker) VALUES('checkpoint_write')")
        conn.commit()
        checkpoint_commits.append(True)

    monkeypatch.setattr(
        video_supervisor, "preview_video_completion_repair", lambda _eid: {"preview": True}
    )
    monkeypatch.setattr(video_supervisor, "load_latest_checkpoint", lambda _eid: None)
    monkeypatch.setattr(video_supervisor, "_deadline_closeout", fake_deadline_closeout)
    monkeypatch.setattr(video_supervisor, "_mark_failed_closed", fake_mark_failed_closed)

    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        video_ops.repair_video_completion_route(episode_id, {"confirm": True})

    assert checkpoint_commits == [True], (
        "本测试要验证的正是 _mark_failed_closed 落检查点之后的提交时机；"
        "没有走到这一步说明测试提前在别处失败，结论不成立"
    )
    leaked = conn.execute(
        "SELECT COUNT(*) AS c FROM fake_pending_adoption WHERE marker='half_done_adoption'"
    ).fetchone()["c"]
    assert leaked == 0, (
        "遗留收口失败时，_deadline_closeout 半途的候选采用写入不能被 "
        "_mark_failed_closed 的检查点提交一并带下去"
    )
