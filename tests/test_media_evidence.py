import json
import sqlite3

from app import db, worker
from tests.conftest import patch_worker_everywhere
from app.evidence import media


def test_video_file_gate_accepts_mp4_signature_but_marks_unverified_duration(tmp_path, monkeypatch) -> None:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 64)
    monkeypatch.setattr(media.shutil, "which", lambda _: None)
    result = media.validate_video_file(str(path), expected_duration_s=5)
    assert result["passed"] is True
    assert result["evidence"]["container_signature"] == "mp4"
    assert [issue.code for issue in result["issues"]] == ["VIDEO_DURATION_UNVERIFIED"]


def test_video_file_gate_rejects_unknown_container(tmp_path, monkeypatch) -> None:
    path = tmp_path / "bad.mp4"
    path.write_bytes(b"not-an-mp4")
    monkeypatch.setattr(media.shutil, "which", lambda _: None)
    result = media.validate_video_file(str(path))
    assert result["passed"] is False
    assert "VIDEO_CONTAINER_INVALID" in {issue.code for issue in result["issues"]}


def test_candidate_selection_uses_first_technical_version_and_records_reason(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
      CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
      CREATE TABLE shots(id TEXT PRIMARY KEY,episode_id TEXT,adopted_version_id TEXT);
      CREATE TABLE shot_versions(id TEXT PRIMARY KEY,shot_id TEXT,version_no INTEGER,status TEXT,
        technical_validation_json TEXT,qa_json TEXT,adoption_reason TEXT);
      INSERT INTO shots VALUES('s','e',NULL);
    """)
    technical = json.dumps({"passed": True})
    conn.execute("INSERT INTO shot_versions VALUES('v1','s',1,'succeeded',?,?,NULL)", (technical, json.dumps({"overall": .7})))
    conn.execute("INSERT INTO shot_versions VALUES('v2','s',2,'succeeded',?,?,NULL)", (technical, json.dumps({"overall": .9})))
    conn.execute("INSERT INTO shot_versions VALUES('v3','s',3,'succeeded',?,?,NULL)", (technical, json.dumps({"overall": 1, "qa_recovered": True})))
    conn.commit()
    monkeypatch.setattr(media, "get_conn", lambda: conn)
    import app.artifacts
    monkeypatch.setattr(app.artifacts, "invalidate_episode_final", lambda _: False)
    selected = media.select_best_video_candidate("s")
    assert selected and selected["version_id"] == "v1"
    assert "首个技术有效视频" in selected["reason"]
    assert conn.execute("SELECT adopted_version_id FROM shots WHERE id='s'").fetchone()[0] == "v1"


def test_adoption_rolls_back_when_a_later_pipeline_step_fails_after_real_adoption(
    monkeypatch,
) -> None:
    """真实失败注入：技术校验通过 → 真实 select_best_video_candidate() 采纳
    → 尾段真实失败路径把同一版本标为 failed → 不变量必须在同一事务内自愈。

    这条测试刻意不走原始 SQL 模拟：采纳用 ``app.evidence.media`` 的真实
    ``select_best_video_candidate``，失败注入用 ``app.worker`` 的真实
    ``_set_version``（run_job.py 里每一条失败处理分支实际调用的同一个
    函数）。两边都用同一张全量 schema（含 ``guard_adopted_version_terminal_
    status`` 触发器），复现 EP1 段5/6/7 的真实代码路径，而不是只验证触发器
    本身的 SQL 语义。
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    conn.executescript(db.INTEGRITY_SCHEMA)
    conn.execute(
        "INSERT INTO projects(id,name,status,created_at) VALUES('p','P','ready',0)"
    )
    conn.execute(
        """INSERT INTO episodes(id,project_id,episode_no,status,created_at)
           VALUES('e','p',1,'generating',0)"""
    )
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s) VALUES('s','e',1,5)"
    )
    technical = json.dumps({"passed": True})
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,
               technical_validation_json,video_path,created_at
           ) VALUES('v2','s',2,'p','k2','succeeded',?,?,0)""",
        (technical, "/tmp/does-not-need-to-exist-for-this-test.mp4"),
    )
    conn.commit()
    monkeypatch.setattr(media, "get_conn", lambda: conn)
    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    import app.artifacts
    monkeypatch.setattr(app.artifacts, "invalidate_episode_final", lambda _episode_id: False)

    # 真实采纳路径：技术校验通过，select_best_video_candidate() 采纳 v2 并
    # commit（对应 run_job.py 里 select_best_video_candidate 调用点）。
    selected = media.select_best_video_candidate("s")
    assert selected and selected["version_id"] == "v2"
    assert conn.execute(
        "SELECT adopted_version_id FROM shots WHERE id='s'"
    ).fetchone()[0] == "v2"

    # 真实失败注入：尾段依赖新鲜度复核（或任何其它写入点）判定失败时，
    # run_job.py 的 except 分支统一调用 _set_version(..., status="failed", ...)
    # 把同一条 shot_versions 行打成终态——这里直接调用同一个真实函数。
    changed = worker._set_version(
        "v2", status="failed", error="REVIEW_DEPENDENCY_STALE：依赖资格复核失败",
    )
    assert changed is True

    # 不变量：adopted_version_id 非空 ⟺ 它指向的版本 status='succeeded'。
    # v2 已经是 failed，触发器必须已经在同一事务内把采用指针释放掉。
    assert conn.execute(
        "SELECT adopted_version_id FROM shots WHERE id='s'"
    ).fetchone()[0] is None
    assert conn.execute(
        "SELECT status FROM shot_versions WHERE id='v2'"
    ).fetchone()[0] == "failed"
