"""开机恢复把停在 CREATED 的分镜运行当重启孤儿收掉并续跑（2026-09-05 第 4 轮 8 集分镜台失败）。

28 集并行时分镜工作流排队等槽位，运行行停在 CREATED（还没 start）；重启后 resume 把
CREATED 当活跃任务去重、什么都不跑，连播台判这些集「步骤已运行但未达到完成判据」。
"""
from __future__ import annotations

from app import api, task_registry
from app.orchestration.engine import WorkflowRecorder
from tests.test_storyboard_workspace_prd import storyboard_db  # noqa: F401  复用同一套隔离库夹具


def test_created_orphan_is_cancelled_and_resumed(storyboard_db, monkeypatch) -> None:  # noqa: F811
    parent = WorkflowRecorder.create(
        workflow_type="storyboard", scope_type="episode", scope_id="e1", input_fingerprint="queued",
    )
    # 不 start()：重启前它还在等工作流槽位
    storyboard_db.execute(
        "UPDATE episodes SET status='scripting',active_storyboard_run_id=? WHERE id='e1'", (parent.run_id,),
    )
    storyboard_db.commit()
    spawned: dict[str, object] = {}

    def fake_spawn(kind, key, coro, *, project_id=None):
        spawned.update(kind=kind, key=key)
        coro.close()
        return None

    monkeypatch.setattr(task_registry, "spawn", fake_spawn)
    assert api.recover_storyboard_tasks() == 1
    old = storyboard_db.execute("SELECT status, failure_code FROM workflow_runs WHERE id=?", (parent.run_id,)).fetchone()
    assert (old["status"], old["failure_code"]) == ("CANCELLED", "SERVICE_RESTART")
    assert spawned.get("kind") == "storyboard" and spawned.get("key") == "e1"
    child = storyboard_db.execute(
        "SELECT parent_run_id FROM workflow_runs WHERE workflow_type='storyboard' AND scope_id='e1' AND id!=?",
        (parent.run_id,),
    ).fetchone()
    assert child is not None and child["parent_run_id"] == parent.run_id
