"""被供应商确定性拒绝的镜头不得阻塞它的下游依赖链（2026-09-05 我欲封天第 15 集）。

第 16 镜 model_rejected（按约定视为跳过），第 17 镜的计划依赖指向它，
``upstream_adopted_version_id`` 永远等不到 → 覆盖账本判 dependency_ready=False →
第 17–21 镜整条链一镜没派发就收口 PARTIAL。修法是运行时依赖行的状态转移：依赖被拒镜头的
依赖行改挂到被拒镜头自己的上游（连续被拒沿链跳过），没有上游就标成已放弃；派发锚点同样
跳过被拒的上一镜。已发布的计划行不动——它的 depends_on 在执行契约指纹里，改了整集就
RELEASE_QUALIFICATION_CHANGED（第 15 集实测）。
"""
from __future__ import annotations

import sqlite3

from app.db import get_conn
from app.video_plan.rejected_rebase import (
    DROPPED_DEPENDENCY_KIND, effective_dependency, rebase_dependencies_past_rejected_shots,
)
from app.video_supervisor import rebuild_coverage_ledger
from app.video_supervisor.dispatch import _after_shot_id
from tests.conftest import patch_video_plan_everywhere


def _seed(conn: sqlite3.Connection, *, shots: int = 4) -> None:
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p1','P',1)")
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,status,created_at) VALUES('e1','p1',1,'generating',1)"
    )
    conn.execute(
        """INSERT INTO provider_video_capability_snapshots(
               id,provider,model,capabilities_json,probe_time,probe_result,technical_success,created_at
           ) VALUES('cap','provider','model','{}',1,'succeeded',1,1)"""
    )
    conn.execute(
        """INSERT INTO episode_video_generation_plans(
               id,episode_id,plan_revision,source_storyboard_revision_id,capability_snapshot_id,status,created_at
           ) VALUES('evp','e1',1,'board','cap','valid',1)"""
    )
    for no in range(1, shots + 1):
        conn.execute(
            """INSERT INTO shots(id,episode_id,shot_no,duration_s,shot_size,camera_move,scene_setting,
                                 characters,action_desc,dialogues,transition,continuity_from_prev)
               VALUES(?,'e1',?,5,'中景','固定','室内','[]','人物站定','[]','硬切',1)""",
            (f"s{no}", no),
        )
        dep = f"s{no - 1}" if no > 1 else None
        conn.execute(
            """INSERT INTO shot_video_generation_plans(
                   id,episode_video_plan_id,shot_id,shot_no,planned_mode,depends_on_shot_id,
                   capability_snapshot_id,status,created_at,updated_at
               ) VALUES(?,'evp',?,?,'REFERENCE_IMAGE_MODE',?,'cap',?,1,1)""",
            (f"svp{no}", f"s{no}", no, dep, "ready" if dep is None else "planned"),
        )
        if dep:
            conn.execute(
                """INSERT INTO video_plan_dependencies(
                       id,episode_video_plan_id,shot_plan_id,shot_id,depends_on_shot_id,dependency_kind,created_at
                   ) VALUES(?,'evp',?,?,?,'adopted_tail_frame',1)""",
                (f"vdep{no}", f"svp{no}", f"s{no}", dep),
            )
    conn.commit()


def _adopt(conn: sqlite3.Connection, shot: str, version: str) -> None:
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at)
           VALUES(?,?,1,'p',?,'succeeded','{}',1)""",
        (version, shot, f"idem-{version}"),
    )
    conn.execute("UPDATE shots SET adopted_version_id=? WHERE id=?", (version, shot))
    conn.execute(
        """UPDATE video_plan_dependencies SET upstream_adopted_version_id=?, resolved_at=1
            WHERE depends_on_shot_id=?""",
        (version, shot),
    )
    conn.commit()


def _reject(conn: sqlite3.Connection, shot: str) -> None:
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at)
           VALUES(?,?,9,'p',?,'failed','{}',1)""",
        (f"ver-rej-{shot}", shot, f"idem-rej-{shot}"),
    )
    conn.execute(
        """INSERT INTO jobs(id,kind,shot_id,version_id,episode_id,project_id,status,
                            provider_create_state,provider_failure_kind,created_at,updated_at)
           VALUES(?,'video',?,?,'e1','p1','failed','model_rejected','provider_content_rejected',1,1)""",
        (f"job-rej-{shot}", shot, f"ver-rej-{shot}"),
    )
    conn.commit()


def _planned(conn: sqlite3.Connection) -> dict[str, str | None]:
    return {
        r["shot_id"]: r["depends_on_shot_id"]
        for r in conn.execute("SELECT shot_id, depends_on_shot_id FROM shot_video_generation_plans")
    }


def _dependency_row(conn: sqlite3.Connection, shot: str) -> dict | None:
    row = conn.execute(
        """SELECT depends_on_shot_id, dependency_kind, upstream_adopted_version_id
             FROM video_plan_dependencies WHERE shot_id=?""",
        (shot,),
    ).fetchone()
    return dict(row) if row else None


def test_dependents_of_rejected_shot_rebase_onto_its_adopted_upstream_without_touching_plan() -> None:
    conn = get_conn()
    _seed(conn)
    planned_before = _planned(conn)
    _adopt(conn, "s1", "v1")
    _reject(conn, "s2")
    changes = rebase_dependencies_past_rejected_shots(conn, "e1")
    conn.commit()
    assert [(c["shot_id"], c["from"], c["to"]) for c in changes] == [("s3", "s2", "s1")]
    assert _dependency_row(conn, "s3") == {
        "depends_on_shot_id": "s1", "dependency_kind": "adopted_tail_frame", "upstream_adopted_version_id": "v1",
    }
    assert _dependency_row(conn, "s4")["depends_on_shot_id"] == "s3"
    # 已发布计划行一个字段都不动：它的 depends_on 在执行契约指纹里，动了整集就 RELEASE_QUALIFICATION_CHANGED。
    assert _planned(conn) == planned_before
    assert conn.execute("SELECT status FROM shot_video_generation_plans WHERE shot_id='s3'").fetchone()[0] == "ready"
    assert effective_dependency(conn, episode_video_plan_id="evp", shot_id="s3", planned_dependency="s2") == "s1"
    assert rebase_dependencies_past_rejected_shots(conn, "e1") == []


def test_consecutive_rejections_are_skipped_and_dependency_without_upstream_is_dropped() -> None:
    conn = get_conn()
    _seed(conn)
    planned_before = _planned(conn)
    _reject(conn, "s1")
    _reject(conn, "s2")
    changes = rebase_dependencies_past_rejected_shots(conn, "e1")
    conn.commit()
    assert {(c["shot_id"], c["to"]) for c in changes} == {("s2", None), ("s3", None)}
    row = _dependency_row(conn, "s3")
    assert row["dependency_kind"] == DROPPED_DEPENDENCY_KIND and row["upstream_adopted_version_id"] is None
    assert effective_dependency(conn, episode_video_plan_id="evp", shot_id="s3", planned_dependency="s2") is None
    assert _dependency_row(conn, "s4")["depends_on_shot_id"] == "s3"
    assert _planned(conn) == planned_before


def test_coverage_ledger_marks_rebased_shot_dispatchable() -> None:
    conn = get_conn()
    _seed(conn)
    _adopt(conn, "s1", "v1")
    _reject(conn, "s2")
    ledger = rebuild_coverage_ledger("e1")
    by_no = {e.shot_no: e for e in ledger.entries}
    assert by_no[2].provider_rejected
    assert by_no[3].dependency_ready and by_no[3].blocked_by_shot_no is None
    assert by_no[3].depends_on_shot_id == "s1"
    actionable = {e.shot_no for e in ledger.actionable()}
    assert 3 in actionable and 2 not in actionable
    assert not conn.in_transaction


def test_after_shot_anchor_skips_rejected_previous_shot(monkeypatch) -> None:
    conn = get_conn()
    _seed(conn)
    _adopt(conn, "s1", "v1")
    _reject(conn, "s2")
    rebase_dependencies_past_rejected_shots(conn, "e1")
    conn.commit()
    # 锚点取自已发布计划；这里的计划没有绑定分镜发布权威，把"计划仍是当前"的校验钉真。
    patch_video_plan_everywhere(monkeypatch, "verify_episode_plan_is_current", lambda plan, conn=None: True)
    assert _after_shot_id("e1", 3) == "s1"
    # 没有可用上游时干脆不挂锚，而不是等一个永远不会来的采纳版本。
    conn.execute("UPDATE shots SET adopted_version_id=NULL WHERE id='s1'")
    _reject(conn, "s1")
    rebase_dependencies_past_rejected_shots(conn, "e1")
    conn.commit()
    assert _after_shot_id("e1", 3) is None
