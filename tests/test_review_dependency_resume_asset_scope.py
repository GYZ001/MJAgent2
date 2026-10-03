"""暂停任务恢复路径的资产围栏回归（2026-10-02 代码评审发现的同根因残留）。

``app.media_exec.authority._assert_review_dependency_fence`` 的 worker_start
判据已经改成只认本镜自己冻结的 ``reference_manifest``（见
``tests/test_review_dependency_asset_scope.py``）。但 ``app.media_exec.
enqueue._resume_reused_paused_job``（暂停/放弃任务恢复路径）另有一份独立实现
的资产比较：旧代码里，只要 ``current_snapshot["narrative_authority_required"]``
为真——生产中任何已发布剧本的集都会进入这一档——就会把资产比较无条件短路成
"未过期"（``assets_equal = bool(current_requires_authority or ...)``），恢复
路径因此从未真正核验过本镜自己依赖的库资产在暂停期间是否被替换。

修法：复用与 worker_start 完全相同的 ``_review_shot_manifest_equal``
判据，不再分支旁路，也不再比较"其它镜头当前选了哪些素材"。
"""
import json
import sqlite3

import pytest

from app import db, worker
from app.media_exec.enqueue import _load_shot_model
from app.multiview import resolve_shot_asset_dependencies
from app.schemas import Bible, Character, World
from tests.conftest import patch_worker_everywhere
from tests.test_review_wall_prd import _published_screenplay_json


def _conn() -> sqlite3.Connection:
    """同 ``tests/test_review_wall_prd.py::_conn``（带真实已发布剧本投影，
    ``_review_shot_manifest_equal`` 内部会无条件尝试 ``resolve_downstream_
    screenplay``），再补一组暂停任务恢复路径需要的 jobs/shot_versions 行。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for statement in db.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    conn.execute(
        """INSERT INTO episodes(
               id,project_id,episode_no,title,status,screenplay_status,
               screenplay_json,
               screenplay_artifact_id,storyboard_artifact_id,
               published_screenplay_artifact_id,published_storyboard_artifact_id,created_at
           ) VALUES('e','p',1,'E','confirmed','ready',?,'screenplay-1','board-1','screenplay-1','board-1',0)""",
        (_published_screenplay_json(),),
    )
    conn.execute(
        """INSERT INTO shots(
               id,episode_id,shot_no,duration_s,shot_size,camera_move,
               scene_setting,action_desc,characters,dialogues,storyboard_artifact_id
           ) VALUES('s1','e',1,5,'中景','固定','日，测试室内场景',
                    'action','[]','[]','board-1')"""
    )
    conn.execute(
        "INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,created_at) "
        "VALUES('v1','s1',1,'p','k1','queued',0)"
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,
               provider_non_cancellable,created_at,updated_at
           ) VALUES('j1','video','s1','v1','e','p','queued',0,0,0)"""
    )
    conn.commit()
    return conn


def _freeze_manifest(conn):
    shot_row = conn.execute("SELECT * FROM shots WHERE id='s1'").fetchone()
    project = conn.execute("SELECT bible_json FROM projects WHERE id='p'").fetchone()
    bible = Bible.model_validate(json.loads(project["bible_json"]))
    return resolve_shot_asset_dependencies(
        project_id="p", episode_no=1, shot_id="s1", shot=_load_shot_model(shot_row),
        scene_name=None, conn=conn, bible=bible, screenplay=None,
    )


def _seed_portrait(conn, tmp_path, *, suffix: str) -> None:
    image_path = tmp_path / f"portrait-{suffix}.png"
    image_path.write_bytes(b"x")
    conn.execute(
        """INSERT INTO character_portraits(
               id,project_id,character_name,ep_start,ep_end,appearance,image_path,bible_version,created_at
           ) VALUES(?,'p','角色甲',1,NULL,'黑发',?,1,0)""",
        (f"portrait-{suffix}", str(image_path)),
    )
    conn.execute(
        """INSERT INTO character_portrait_views(
               id,portrait_id,view_role,status,selected,input_fingerprint,image_path,created_at
           ) VALUES(?,?,'front_full','ready',1,?,?,0)""",
        (f"portrait-{suffix}_view", f"portrait-{suffix}", f"fp-{suffix}", str(image_path)),
    )


def _seed_and_freeze(conn, monkeypatch, tmp_path) -> tuple[dict, dict]:
    conn.execute(
        "UPDATE projects SET bible_json=? WHERE id='p'",
        (Bible(
            characters=[Character(name="角色甲", role="lead", appearance_canonical="黑发")],
            world=World(visual_style_canonical="写实"),
        ).model_dump_json(),),
    )
    conn.execute("UPDATE shots SET characters=? WHERE id='s1'", (json.dumps(["角色甲"]),))
    _seed_portrait(conn, tmp_path, suffix="v1")
    conn.commit()
    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_worker_everywhere(monkeypatch, "mark_media_job_state", lambda *args, **kwargs: None)

    frozen = _freeze_manifest(conn)
    authority_snapshot = {
        "qualification_version": "authority-q1:asset-old",
        "published_screenplay_artifact_id": None,
        "confirmed_storyboard_artifact_id": None,
        "screenplay_revision": None,
        "storyboard_revision": None,
    }
    current_snapshot = {
        **authority_snapshot,
        "narrative_authority_required": True,
        "narrative_authority_verified": True,
        "narrative_authority_version": "authority-q1",
    }
    conn.execute(
        """UPDATE episodes
              SET video_completion_mode='complete', active_video_run_id='run-new'
            WHERE id='e'"""
    )
    conn.execute(
        "UPDATE shot_versions SET image_inputs=? WHERE id='v1'",
        (json.dumps({"review_dependency_snapshot": authority_snapshot, "reference_manifest": frozen}),),
    )
    conn.commit()
    worker.pause_episode_video_tasks("e")
    return authority_snapshot, current_snapshot


def test_resume_fences_when_own_portrait_replaced_under_narrative_authority(monkeypatch, tmp_path) -> None:
    """本镜定妆照在暂停期间被替换，恢复仍须 fail closed——即便
    ``narrative_authority_required`` 为真（生产中的常态）。"""
    conn = _conn()
    _authority_snapshot, current_snapshot = _seed_and_freeze(conn, monkeypatch, tmp_path)
    # 暂停期间定妆照被替换成新 portrait（级联删旧视角）。
    conn.execute("DELETE FROM character_portraits WHERE character_name='角色甲'")
    _seed_portrait(conn, tmp_path, suffix="v2")
    conn.commit()

    with pytest.raises(ValueError, match="REVIEW_DEPENDENCY_STALE"):
        worker._resume_reused_paused_job(
            "v1", supervisor_run_id="run-new", dependency_snapshot=current_snapshot,
        )
    assert conn.execute(
        "SELECT status,owner_run_id FROM jobs WHERE id='j1'"
    ).fetchone()[:] == ("paused", None)


def test_resume_unaffected_by_unrelated_library_churn(monkeypatch, tmp_path) -> None:
    """安全网：与本镜无关的素材库变化不得牵连本镜恢复——新判据只看本镜
    自己冻结的 ``reference_manifest``，与 worker_start 同口径。"""
    conn = _conn()
    _authority_snapshot, current_snapshot = _seed_and_freeze(conn, monkeypatch, tmp_path)
    # 与本镜无关的素材库写入：另一个角色新增一张定妆照。
    conn.execute(
        """INSERT INTO character_portraits(
               id,project_id,character_name,ep_start,ep_end,appearance,image_path,bible_version,created_at
           ) VALUES('portrait-other','p','角色乙',1,NULL,'黑发','/tmp/other.png',1,0)"""
    )
    conn.commit()

    result = worker._resume_reused_paused_job(
        "v1", supervisor_run_id="run-new", dependency_snapshot=current_snapshot,
    )
    assert result and result["resumed"] is True
