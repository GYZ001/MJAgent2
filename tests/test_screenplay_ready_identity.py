"""``_screenplay_ready`` 结论缓存的键必须覆盖它能读到的每一类输入。

``_screenplay_ready`` 是 fail-closed 门禁，一次完整重验证实测约 1.9 s，而剧本台
每 15 s 轮询、每次打开还要跑两遍。结论按「全部输入的内容指纹」缓存后，命中与重算
必须语义等价——这只在键**完整**时成立。本文件逐类输入锁死这一点：
任何一类输入变化都必须改变键，否则缓存就可能给出过期的权威结论。
"""
from __future__ import annotations

import json

import pytest

from app import db
from app.domain.common import screenplay_ready_identity
from app.evidence import repository
from app.harness.types import EvidenceArtifact
from app.schemas import EpisodeScreenplay


def _episode_row() -> dict:
    return dict(db.get_conn().execute("SELECT * FROM episodes WHERE id='e1'").fetchone())


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ready-identity.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, bible_json, created_at) "
        "VALUES('p1','demo','ready','{\"characters\":[]}',?)",
        (db.now(),),
    )
    conn.execute(
        "INSERT INTO chapters(project_id, idx, title, content, char_count) "
        "VALUES('p1',1,'第一章','少年抬头看天。',8)",
    )
    script = EpisodeScreenplay(
        episode_no=1, title="第一集", full_script_text="【场1】山顶\n少年抬头看天。",
    ).model_dump(mode="json")
    conn.execute(
        """INSERT INTO episodes(
               id, project_id, episode_no, title, source_chapters,
               screenplay_json, screenplay_status, screenplay_updated_at,
               status, created_at
           ) VALUES('e1','p1',1,'第一集','[1]',?, 'ready', ?, 'planned', ?)""",
        (json.dumps(script, ensure_ascii=False), db.now(), db.now()),
    )
    conn.commit()
    repository.create_artifact(EvidenceArtifact(
        type="screenplay_document", scope_type="episode", scope_id="e1",
        status="approved", trust_level="T3", content={"a": 1},
    ))
    repository.create_artifact(EvidenceArtifact(
        type="character_bible", scope_type="project", scope_id="p1",
        status="approved", trust_level="T3", content={"b": 1},
    ))
    yield


def _episode_artifact_id() -> str:
    return str(db.get_conn().execute(
        "SELECT id FROM artifacts WHERE scope_type='episode' AND scope_id='e1'"
    ).fetchone()["id"])


def _assert_changes(mutate) -> None:
    before = screenplay_ready_identity(_episode_row())
    mutate(db.get_conn())
    db.get_conn().commit()
    assert screenplay_ready_identity(_episode_row()) != before


def test_identity_is_stable_without_any_change() -> None:
    assert screenplay_ready_identity(_episode_row()) == screenplay_ready_identity(
        _episode_row()
    )


def test_episode_row_change_changes_identity() -> None:
    _assert_changes(lambda conn: conn.execute(
        "UPDATE episodes SET screenplay_artifact_id='art-x' WHERE id='e1'"
    ))


def test_screenplay_projection_change_changes_identity() -> None:
    _assert_changes(lambda conn: conn.execute(
        "UPDATE episodes SET screenplay_json=? WHERE id='e1'",
        (json.dumps({"episode_no": 1, "full_script_text": "改了"}),),
    ))


def test_project_row_change_changes_identity() -> None:
    _assert_changes(lambda conn: conn.execute(
        "UPDATE projects SET bible_json='{\"characters\":[{\"name\":\"孟浩\"}]}' WHERE id='p1'"
    ))


def test_source_chapter_change_changes_identity() -> None:
    _assert_changes(lambda conn: conn.execute(
        "UPDATE chapters SET content='少年低头。' WHERE project_id='p1' AND idx=1"
    ))


def test_stub_fallback_chapter_change_changes_identity() -> None:
    """存根回退会读到下一条存在的章节，它同样是输入。"""
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO chapters(project_id, idx, title, content, char_count) "
        "VALUES('p1',7,'第七章','后续正文。',5)",
    )
    conn.commit()
    _assert_changes(lambda db_conn: db_conn.execute(
        "UPDATE chapters SET content='后续正文改了。' WHERE project_id='p1' AND idx=7"
    ))


def test_unrelated_chapter_does_not_change_identity() -> None:
    """整本小说上千章：只有本集真正读到的章节能影响结论。"""
    conn = db.get_conn()
    conn.executemany(
        "INSERT INTO chapters(project_id, idx, title, content, char_count) "
        "VALUES('p1',?,?,?,4)",
        [(20, "第二十章", "无关正文。"), (21, "第二十一章", "无关正文。")],
    )
    conn.commit()
    before = screenplay_ready_identity(_episode_row())
    conn.execute(
        "UPDATE chapters SET content='彻底改写。' WHERE project_id='p1' AND idx=21"
    )
    conn.commit()

    assert screenplay_ready_identity(_episode_row()) == before


def test_episode_artifact_status_change_changes_identity() -> None:
    artifact_id = _episode_artifact_id()
    _assert_changes(lambda conn: conn.execute(
        "UPDATE artifacts SET status='stale' WHERE id=?", (artifact_id,)
    ))


def test_episode_artifact_content_change_changes_identity() -> None:
    artifact_id = _episode_artifact_id()
    _assert_changes(lambda conn: conn.execute(
        "UPDATE artifacts SET content_hash='deadbeef' WHERE id=?", (artifact_id,)
    ))


def test_project_artifact_change_changes_identity() -> None:
    _assert_changes(lambda conn: conn.execute(
        "UPDATE artifacts SET status='stale' "
        "WHERE scope_type='project' AND scope_id='p1'"
    ))


def test_new_certificate_changes_identity() -> None:
    _assert_changes(lambda conn: conn.execute(
        """INSERT INTO completion_certificates(
               id, kind, scope_id, artifact_id, artifact_hash, input_fingerprint,
               contract_version, qa_profile_version, evaluation_ids_json,
               blockers, must_fix_issues, issued_at, payload_json
           ) VALUES('cert1','screenplay','e1','art-x','h','fp','c','q','[]',0,0,?, '{}')""",
        (db.now(),),
    ))


def test_new_revision_changes_identity() -> None:
    _assert_changes(lambda conn: conn.execute(
        """INSERT INTO production_revisions(
               id, episode_id, kind, status, baseline_generation_count,
               input_fingerprint, contract_version, qa_profile_version,
               checkpoint_json, created_at, updated_at
           ) VALUES('rev1','e1','screenplay','active',0,'','','','{}',?,?)""",
        (db.now(), db.now()),
    ))


def test_new_evaluation_on_a_scoped_artifact_changes_identity() -> None:
    artifact_id = _episode_artifact_id()
    _assert_changes(lambda conn: conn.execute(
        """INSERT INTO evaluations(
               id, artifact_id, evaluator_type, evaluator_name, evaluator_version,
               status, score, hard_gate_passed, dimension_scores_json, issues_json,
               evidence_json, created_at
           ) VALUES('eval1',?, 'model','screenplay_production_qa','v1','passed',
                    90,1,'{}','[]','{}',?)""",
        (artifact_id, db.now()),
    ))


def test_trimmed_input_row_is_not_treated_as_the_full_row() -> None:
    """裁剪过的字典必须键不同——R4 正是「先裁剪后判定」造成的状态分裂。"""
    full = _episode_row()
    trimmed = {key: value for key, value in full.items() if key != "screenplay_json"}

    assert screenplay_ready_identity(trimmed) != screenplay_ready_identity(full)
