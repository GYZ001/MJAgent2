"""WS8-B：服务重启后残留的 RUNNING/PAUSED_EXTERNAL 运行必须在启动恢复时收尾。

真实事故：跑不快的孩子（proj_ce9fcf749b23）run_df4b60ccef89（screenplay_batch）
至今 status=RUNNING、finished_at 为空；它的 3 个子运行里 1 个 SUCCEEDED、1 个
FAILED、1 个仍停在 PAUSED_EXTERNAL（服务重启中断，恢复重跑判定"不可续跑"后只
回滚了 episode 指针，从未回头收尾这条子运行本身）——
``app.domain.screenplay_ops.guarded._refresh_screenplay_batch_run`` 只在子运行
的 ``_screenplay_guarded`` 协程正常跑完的 ``finally`` 块里触发，那个从未真正
重新起跑的子运行永远不会触发它，父运行因此永久挂在 RUNNING。

本测试用纯内存 sqlite（不经过 app.db/get_conn，直接把 conn 传给
``finalize_stale_workflow_runs``）构造父子运行，覆盖 app.domain.
orchestration_ops.stale_run_finalize 的判据矩阵。
"""
from __future__ import annotations

import sqlite3

import pytest

from app import task_registry
from app.domain.orchestration_ops.stale_run_finalize import (
    ORPHAN_FAILURE_CODE,
    _ACTIVE_TASK_KIND_BY_WORKFLOW_TYPE,
    finalize_stale_workflow_runs,
)

# 与 app/db.py 的 workflow_runs 建表语句保持一致（本测试不经过 app.db，独立
# 起一份内存 sqlite，手工维护同步；本函数只用到其中一部分列，但整份复制以防
# 将来新增列后这里漏建导致 INSERT 失败得含糊）。
_WORKFLOW_RUNS_DDL = """
CREATE TABLE workflow_runs (
    id TEXT PRIMARY KEY,
    workflow_type TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    parent_run_id TEXT,
    status TEXT NOT NULL,
    current_step_key TEXT,
    requested_by TEXT,
    trigger_type TEXT,
    input_fingerprint TEXT NOT NULL,
    policy_snapshot_json TEXT NOT NULL DEFAULT '{}',
    config_snapshot_json TEXT NOT NULL DEFAULT '{}',
    budget_limit_cny REAL,
    cost_cny REAL NOT NULL DEFAULT 0,
    deadline_at REAL,
    started_at REAL,
    updated_at REAL NOT NULL,
    finished_at REAL,
    failure_code TEXT,
    failure_message TEXT,
    resume_from_step TEXT,
    recovered_by_run_id TEXT,
    recovered_at REAL,
    recovery_count INTEGER NOT NULL DEFAULT 0
)
"""

NOW = 1_000_000.0
STALE_AFTER_S = 3600.0
OLD = NOW - STALE_AFTER_S - 10  # 早于阈值
FRESH = NOW - 60  # 晚于阈值，不该被碰


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(_WORKFLOW_RUNS_DDL)
    yield c
    c.close()


def _insert(
    conn, *, id, workflow_type, status, started_at,
    scope_type="episode", scope_id="ep1", parent_run_id=None,
    failure_code=None, failure_message=None,
):
    conn.execute(
        """INSERT INTO workflow_runs(
            id, workflow_type, scope_type, scope_id, parent_run_id, status,
            requested_by, trigger_type, input_fingerprint, updated_at, started_at,
            failure_code, failure_message
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            id, workflow_type, scope_type, scope_id, parent_run_id, status,
            "user", "manual", "fp", started_at, started_at,
            failure_code, failure_message,
        ),
    )
    conn.commit()


def _status(conn, run_id) -> sqlite3.Row:
    return conn.execute(
        "SELECT status, failure_code, failure_message, finished_at FROM workflow_runs WHERE id=?",
        (run_id,),
    ).fetchone()


class TestBatchParentFinalization:
    def test_real_repro_running_batch_with_orphaned_child_resolves_to_partial(self, conn, monkeypatch):
        """跑不快的孩子真实复现：批运行 RUNNING，子运行 1 SUCCEEDED / 1 FAILED /
        1 仍 PAUSED_EXTERNAL（所属 episode 早已转向另一次与这条子运行毫无
        parent_run_id 关系的独立重试，这条子运行本身被彻底遗弃，永远不会自然
        终态）。task_registry 确认这个 (screenplay, ep3) 现在没有活跃任务——
        按"未完成"计入失败统计，批运行收口为 PARTIAL，不再永久谎称"正在运行"；
        子运行自己这一行不改写，仍是 PAUSED_EXTERNAL（历史留痕）。"""
        monkeypatch.setattr(task_registry, "active", lambda kind, key: False)
        _insert(conn, id="batch1", workflow_type="screenplay_batch", status="RUNNING",
                started_at=OLD, scope_type="project", scope_id="proj1")
        _insert(conn, id="c1", workflow_type="screenplay", status="SUCCEEDED",
                started_at=OLD, parent_run_id="batch1", scope_id="ep1")
        _insert(conn, id="c2", workflow_type="screenplay", status="FAILED",
                started_at=OLD, parent_run_id="batch1", scope_id="ep2",
                failure_message="QA 校验失败")
        _insert(conn, id="c3", workflow_type="screenplay", status="PAUSED_EXTERNAL",
                started_at=OLD, parent_run_id="batch1", scope_id="ep3",
                failure_code="SERVICE_RESTART", failure_message="服务重启，剧本运行等待自动续跑")

        outcomes = finalize_stale_workflow_runs(conn, now_ts=NOW, stale_after_s=STALE_AFTER_S)

        row = _status(conn, "batch1")
        assert row["status"] == "PARTIAL"
        assert row["failure_code"] == "PARTIAL_RESULT"
        assert "c2" in row["failure_message"] and "c3" in row["failure_message"]
        assert row["finished_at"] is not None
        assert outcomes.get("PARTIAL", 0) >= 1
        # 子运行自己这一行不被改写——只有父运行的收口判断把它算作"未完成"。
        assert _status(conn, "c3")["status"] == "PAUSED_EXTERNAL"

    def test_running_batch_with_genuinely_active_child_is_not_finalized(self, conn, monkeypatch):
        """子运行确实还在跑（task_registry 显示活跃）——不能被误判成孤儿，批
        运行退回"标记为孤儿暂停"（因为它自己此刻仍是 RUNNING、没有独立的活跃
        任务），而不是编一个假的收口结果。"""
        monkeypatch.setattr(task_registry, "active", lambda kind, key: True)
        _insert(conn, id="batch1b", workflow_type="screenplay_batch", status="RUNNING",
                started_at=OLD, scope_type="project", scope_id="proj1")
        _insert(conn, id="c1", workflow_type="screenplay", status="RUNNING",
                started_at=OLD, parent_run_id="batch1b", scope_id="ep1")

        outcomes = finalize_stale_workflow_runs(conn, now_ts=NOW, stale_after_s=STALE_AFTER_S)

        row = _status(conn, "batch1b")
        assert row["status"] == "PAUSED_EXTERNAL"
        assert row["failure_code"] == ORPHAN_FAILURE_CODE
        assert outcomes.get("PAUSED_EXTERNAL", 0) >= 1
        assert _status(conn, "c1")["status"] == "RUNNING"

    def test_running_batch_with_unmapped_child_type_cannot_confirm_orphan(self, conn):
        """子运行的工作流类型没有核实过 task_registry 映射——无法确认"现在没
        有活跃任务"，批运行同样不收口，只标孤儿暂停，不猜测。"""
        _insert(conn, id="batch1c", workflow_type="screenplay_batch", status="RUNNING",
                started_at=OLD, scope_type="project", scope_id="proj1")
        _insert(conn, id="c1", workflow_type="portrait_view_redo", status="RUNNING",
                started_at=OLD, parent_run_id="batch1c", scope_id="portrait_1:profile")

        finalize_stale_workflow_runs(conn, now_ts=NOW, stale_after_s=STALE_AFTER_S)

        assert _status(conn, "batch1c")["status"] == "PAUSED_EXTERNAL"

    def test_all_children_succeeded_marks_batch_succeeded(self, conn):
        _insert(conn, id="batch2", workflow_type="screenplay_batch", status="RUNNING",
                started_at=OLD, scope_type="project", scope_id="proj1")
        _insert(conn, id="c1", workflow_type="screenplay", status="SUCCEEDED",
                started_at=OLD, parent_run_id="batch2", scope_id="ep1")
        _insert(conn, id="c2", workflow_type="screenplay", status="SUCCEEDED",
                started_at=OLD, parent_run_id="batch2", scope_id="ep2")

        finalize_stale_workflow_runs(conn, now_ts=NOW, stale_after_s=STALE_AFTER_S)

        row = _status(conn, "batch2")
        assert row["status"] == "SUCCEEDED"
        assert row["failure_code"] is None
        assert row["finished_at"] is not None

    def test_all_children_failed_marks_batch_failed_with_n_failed_message(self, conn):
        _insert(conn, id="batch3", workflow_type="screenplay_batch", status="RUNNING",
                started_at=OLD, scope_type="project", scope_id="proj1")
        _insert(conn, id="c1", workflow_type="screenplay", status="FAILED",
                started_at=OLD, parent_run_id="batch3", scope_id="ep1",
                failure_message="供应商拒绝请求")
        _insert(conn, id="c2", workflow_type="screenplay", status="FAILED",
                started_at=OLD, parent_run_id="batch3", scope_id="ep2",
                failure_message="校验失败")

        finalize_stale_workflow_runs(conn, now_ts=NOW, stale_after_s=STALE_AFTER_S)

        row = _status(conn, "batch3")
        assert row["status"] == "FAILED"
        assert row["failure_message"].startswith("2 个子运行失败：")
        assert "c1" in row["failure_message"] and "c2" in row["failure_message"]

    def test_mixed_children_mark_batch_partial(self, conn):
        _insert(conn, id="batch4", workflow_type="screenplay_batch", status="RUNNING",
                started_at=OLD, scope_type="project", scope_id="proj1")
        _insert(conn, id="c1", workflow_type="screenplay", status="SUCCEEDED",
                started_at=OLD, parent_run_id="batch4", scope_id="ep1")
        _insert(conn, id="c2", workflow_type="screenplay", status="FAILED",
                started_at=OLD, parent_run_id="batch4", scope_id="ep2",
                failure_message="失败原因")

        finalize_stale_workflow_runs(conn, now_ts=NOW, stale_after_s=STALE_AFTER_S)

        row = _status(conn, "batch4")
        assert row["status"] == "PARTIAL"
        assert row["failure_code"] == "PARTIAL_RESULT"
        assert row["failure_message"].startswith("1 个子运行失败：")

    def test_paused_external_batch_with_all_terminal_children_hops_through_running(self, conn):
        """批运行本身已经是 PAUSED_EXTERNAL（例如上一次进程重启时被打断），
        子运行这次全部终态——收口逻辑要先经过 RUNNING 这一跳（RUN_TRANSITIONS
        不允许 PAUSED_EXTERNAL 直接到 SUCCEEDED/PARTIAL），最终仍落到正确的
        终态，不能因为状态机限制就放弃收尾。"""
        _insert(conn, id="batch5", workflow_type="screenplay_batch", status="PAUSED_EXTERNAL",
                started_at=OLD, scope_type="project", scope_id="proj1",
                failure_code="SERVICE_RESTART")
        _insert(conn, id="c1", workflow_type="screenplay", status="SUCCEEDED",
                started_at=OLD, parent_run_id="batch5", scope_id="ep1")

        finalize_stale_workflow_runs(conn, now_ts=NOW, stale_after_s=STALE_AFTER_S)

        row = _status(conn, "batch5")
        assert row["status"] == "SUCCEEDED"

    def test_paused_external_batch_with_orphaned_child_hops_through_to_partial(self, conn, monkeypatch):
        """当前生产库真实快照：run_df4b60ccef89（screenplay_batch）本身已是
        PAUSED_EXTERNAL，子运行 1 SUCCEEDED / 1 FAILED / 1 仍 PAUSED_EXTERNAL
        （task_registry 确认无活跃任务）。要先经过 RUNNING 这一跳
        （RUN_TRANSITIONS 不允许 PAUSED_EXTERNAL 直接到 PARTIAL/FAILED/
        SUCCEEDED），再落到正确终态，不能因为批运行自己已经是 PAUSED_EXTERNAL
        就放弃收尾。"""
        monkeypatch.setattr(task_registry, "active", lambda kind, key: False)
        _insert(conn, id="batch6", workflow_type="screenplay_batch", status="PAUSED_EXTERNAL",
                started_at=OLD, scope_type="project", scope_id="proj1",
                failure_code="SERVICE_RESTART")
        _insert(conn, id="c1", workflow_type="screenplay", status="SUCCEEDED",
                started_at=OLD, parent_run_id="batch6", scope_id="ep1")
        _insert(conn, id="c2", workflow_type="screenplay", status="FAILED",
                started_at=OLD, parent_run_id="batch6", scope_id="ep2",
                failure_message="校验失败")
        _insert(conn, id="c3", workflow_type="screenplay", status="PAUSED_EXTERNAL",
                started_at=OLD, parent_run_id="batch6", scope_id="ep3",
                failure_code="SERVICE_RESTART")

        finalize_stale_workflow_runs(conn, now_ts=NOW, stale_after_s=STALE_AFTER_S)

        row = _status(conn, "batch6")
        assert row["status"] == "PARTIAL"
        assert row["failure_message"].startswith("2 个子运行失败：")
        assert _status(conn, "c3")["status"] == "PAUSED_EXTERNAL"  # 子运行不改写

    def test_paused_external_batch_with_genuinely_active_child_is_left_alone(self, conn, monkeypatch):
        """批运行已经是 PAUSED_EXTERNAL，子运行确实还活跃（task_registry 确认）
        ——PAUSED_EXTERNAL 没有到自身的合法转移，本次不处理，留着已知缺口而不
        是猜测关闭。"""
        monkeypatch.setattr(task_registry, "active", lambda kind, key: True)
        _insert(conn, id="batch6b", workflow_type="screenplay_batch", status="PAUSED_EXTERNAL",
                started_at=OLD, scope_type="project", scope_id="proj1",
                failure_code="SERVICE_RESTART")
        _insert(conn, id="c1", workflow_type="screenplay", status="RUNNING",
                started_at=OLD, parent_run_id="batch6b", scope_id="ep1")

        outcomes = finalize_stale_workflow_runs(conn, now_ts=NOW, stale_after_s=STALE_AFTER_S)

        assert _status(conn, "batch6b")["status"] == "PAUSED_EXTERNAL"
        assert _status(conn, "c1")["status"] == "RUNNING"
        assert outcomes == {}

    def test_batch_with_no_children_is_treated_as_orphan(self, conn):
        _insert(conn, id="batch7", workflow_type="screenplay_batch", status="RUNNING",
                started_at=OLD, scope_type="project", scope_id="proj1")

        finalize_stale_workflow_runs(conn, now_ts=NOW, stale_after_s=STALE_AFTER_S)

        row = _status(conn, "batch7")
        assert row["status"] == "PAUSED_EXTERNAL"
        assert row["failure_code"] == ORPHAN_FAILURE_CODE

    def test_fresh_batch_within_threshold_is_untouched(self, conn):
        _insert(conn, id="batch8", workflow_type="screenplay_batch", status="RUNNING",
                started_at=FRESH, scope_type="project", scope_id="proj1")
        _insert(conn, id="c1", workflow_type="screenplay", status="FAILED",
                started_at=FRESH, parent_run_id="batch8", scope_id="ep1")

        outcomes = finalize_stale_workflow_runs(conn, now_ts=NOW, stale_after_s=STALE_AFTER_S)

        assert _status(conn, "batch8")["status"] == "RUNNING"
        assert outcomes == {}


class TestLeafRunFinalization:
    def test_running_leaf_without_active_task_becomes_orphan_paused(self, conn, monkeypatch):
        monkeypatch.setattr(task_registry, "active", lambda kind, key: False)
        _insert(conn, id="leaf1", workflow_type="screenplay", status="RUNNING",
                started_at=OLD, scope_id="ep1")

        finalize_stale_workflow_runs(conn, now_ts=NOW, stale_after_s=STALE_AFTER_S)

        row = _status(conn, "leaf1")
        assert row["status"] == "PAUSED_EXTERNAL"
        assert row["failure_code"] == ORPHAN_FAILURE_CODE
        assert "无人接管" in row["failure_message"]

    def test_running_leaf_with_active_task_is_left_running(self, conn, monkeypatch):
        """task_registry 显示这个 (kind, scope_id) 确实还活跃——绝不能误杀
        真正在跑的任务。"""
        seen: list[tuple[str, str]] = []

        def fake_active(kind, key):
            seen.append((kind, key))
            return True

        monkeypatch.setattr(task_registry, "active", fake_active)
        _insert(conn, id="leaf2", workflow_type="screenplay", status="RUNNING",
                started_at=OLD, scope_id="ep_live")

        outcomes = finalize_stale_workflow_runs(conn, now_ts=NOW, stale_after_s=STALE_AFTER_S)

        assert _status(conn, "leaf2")["status"] == "RUNNING"
        assert outcomes == {}
        assert seen == [("screenplay", "ep_live")]

    def test_running_leaf_with_unmapped_workflow_type_is_conservatively_skipped(self, conn, monkeypatch):
        """没有核实过 task_registry kind 映射的工作流类型（例如
        portrait_view_redo，key 不等于 scope_id）宁可不动，也不猜测。"""
        monkeypatch.setattr(
            task_registry, "active",
            lambda *a: pytest.fail("不该对未映射类型查询 task_registry"),
        )
        _insert(conn, id="leaf3", workflow_type="portrait_view_redo", status="RUNNING",
                started_at=OLD, scope_type="project", scope_id="p1")

        outcomes = finalize_stale_workflow_runs(conn, now_ts=NOW, stale_after_s=STALE_AFTER_S)

        assert _status(conn, "leaf3")["status"] == "RUNNING"
        assert outcomes == {}

    def test_paused_external_leaf_is_left_alone(self, conn, monkeypatch):
        """非批运行已经是 PAUSED_EXTERNAL——本次不处理（已知缺口：需要确认所属
        实体没有转向别的继任运行，属于逐工作流业务知识，见模块 docstring）。"""
        monkeypatch.setattr(task_registry, "active", lambda *a: False)
        _insert(conn, id="leaf4", workflow_type="screenplay", status="PAUSED_EXTERNAL",
                started_at=OLD, scope_id="ep1", failure_code="SERVICE_RESTART")

        outcomes = finalize_stale_workflow_runs(conn, now_ts=NOW, stale_after_s=STALE_AFTER_S)

        assert _status(conn, "leaf4")["status"] == "PAUSED_EXTERNAL"
        assert outcomes == {}

    def test_fresh_leaf_within_threshold_is_untouched(self, conn, monkeypatch):
        monkeypatch.setattr(task_registry, "active", lambda *a: False)
        _insert(conn, id="leaf5", workflow_type="screenplay", status="RUNNING",
                started_at=FRESH, scope_id="ep1")

        outcomes = finalize_stale_workflow_runs(conn, now_ts=NOW, stale_after_s=STALE_AFTER_S)

        assert _status(conn, "leaf5")["status"] == "RUNNING"
        assert outcomes == {}


class TestActiveTaskKindMapping:
    """核实过的 (workflow_type -> task_registry kind) 映射是本模块唯一敢自动
    收尾非批运行的依据，回归锁住每一条，避免以后被静默改错。"""

    @pytest.mark.parametrize("workflow_type,expected_kind", [
        ("screenplay", "screenplay"),
        ("storyboard", "storyboard"),
        ("character_bible", "bible"),
        ("scene_bible", "scene_bible"),
        ("character_references", "refs"),
        ("scene_references", "scene_refs"),
        ("episode_video_completion", "video_completion"),
        ("project_video_completion_queue", "video_completion_project"),
    ])
    def test_mapping(self, workflow_type, expected_kind):
        assert _ACTIVE_TASK_KIND_BY_WORKFLOW_TYPE[workflow_type] == expected_kind

    def test_known_gaps_are_not_mapped(self):
        """key 带 view_role、不等于 scope_id 的两类，以及 delivery_*/
        video_generation（另有专属恢复路径或不经 task_registry）刻意不收录。"""
        for unmapped in (
            "portrait_view_redo", "scene_view_redo",
            "delivery_package", "delivery_approval", "delivery_revision",
            "video_generation",
        ):
            assert unmapped not in _ACTIVE_TASK_KIND_BY_WORKFLOW_TYPE
