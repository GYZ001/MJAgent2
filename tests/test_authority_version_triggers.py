"""作用域权威版本号由触发器在写路径维护——漏一个写入路径，围栏就会读到过期版本号 fail open。

``screenplay_ready_identity`` 原来每次都重扫本集工件 + 项目级工件 + 评估 + 凭证 + 修订来算指纹，
成本 = 项目积累量 × 作业并发度（2026-09-07 实测占后端 47.8% CPU）。改成读两个整数后，这些整数
的正确性就是全部判据，所以每一类写入都要在这里钉死。
"""
from __future__ import annotations


import pytest

from app import db
from app.evidence import repository
from app.evidence.authority_version import scope_versions
from app.harness.types import EvidenceArtifact


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "authority-version.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute("INSERT INTO projects(id, name, status, bible_json, created_at) "
                 "VALUES('p1','demo','ready','{}',?)", (db.now(),))
    conn.execute("INSERT INTO episodes(id, project_id, episode_no, title, source_chapters, status, created_at) "
                 "VALUES('e1','p1',1,'第一集','[1]','planned',?)", (db.now(),))
    conn.commit()
    yield


def _versions() -> dict[str, int]:
    return dict(scope_versions(db.get_conn(), episode_id="e1", project_id="p1"))


def _artifact(scope_type: str, scope_id: str, **over) -> dict:
    payload = dict(type="screenplay_document", scope_type=scope_type, scope_id=scope_id,
                   status="approved", trust_level="T3", content={"a": 1})
    payload.update(over)
    return repository.create_artifact(EvidenceArtifact(**payload))


def test_unwritten_scopes_start_at_zero() -> None:
    assert _versions() == {"episode:e1": 0, "project:p1": 0}


def test_each_artifact_write_bumps_only_its_own_scope() -> None:
    before = _versions()
    _artifact("episode", "e1")
    after_episode = _versions()
    assert after_episode["episode:e1"] > before["episode:e1"]
    assert after_episode["project:p1"] == before["project:p1"]
    _artifact("project", "p1", type="character_bible")
    after_project = _versions()
    assert after_project["project:p1"] > after_episode["project:p1"]
    assert after_project["episode:e1"] == after_episode["episode:e1"]


def test_update_and_delete_also_bump() -> None:
    row = _artifact("episode", "e1")
    conn = db.get_conn()
    baseline = _versions()["episode:e1"]
    conn.execute("UPDATE artifacts SET status='superseded' WHERE id=?", (row["id"],))
    conn.commit()
    updated = _versions()["episode:e1"]
    assert updated > baseline
    conn.execute("DELETE FROM artifacts WHERE id=?", (row["id"],))
    conn.commit()
    assert _versions()["episode:e1"] > updated


def test_evaluation_bumps_the_scope_of_its_artifact() -> None:
    row = _artifact("episode", "e1")
    conn = db.get_conn()
    baseline = _versions()
    conn.execute(
        "INSERT INTO evaluations(id, artifact_id, evaluator_type, evaluator_name, evaluator_version, "
        "status, hard_gate_passed, score, created_at) VALUES('ev1',?, 'file','v','1.0','passed',1,100,?)",
        (row["id"], db.now()))
    conn.commit()
    assert _versions()["episode:e1"] > baseline["episode:e1"]
    assert _versions()["project:p1"] == baseline["project:p1"]


def test_lazily_created_tables_get_their_triggers() -> None:
    """完成凭证与生产修订是按需建表的：表建好后必须补挂触发器，否则历史库会静默 fail open。"""
    from app.production.certificate import ensure_completion_certificates_table
    from app.production.revision import ensure_production_revisions_table

    conn = db.get_conn()
    ensure_completion_certificates_table(conn)
    ensure_production_revisions_table(conn)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'bump_authority_%'")}
    for table in ("artifacts", "evaluations", "completion_certificates", "production_revisions"):
        for event in ("insert", "update", "delete"):
            assert f"bump_authority_{table}_{event}" in names, (table, event)
    baseline = _versions()["episode:e1"]
    conn.execute(
        "INSERT INTO completion_certificates(id, kind, scope_id, artifact_id, artifact_hash, issued_at) "
        "VALUES('c1','storyboard','e1','a1','h',?)", (db.now(),))
    conn.commit()
    assert _versions()["episode:e1"] > baseline
