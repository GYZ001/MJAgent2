"""retry grant 与 revision 的绑定必须只有一个真源。

生产根因（EP4 / ERR-20260822-14fbf4）：为「上次供应商结果未知」签发的 retry grant
被钉进 run 的 config snapshot；随后 ``ensure_production_revision`` 因为指纹变化
合法地新建了一条 revision，并把 ``grant_id`` 带了过去，但 ``production_grants``
行仍然指向被 superseded 的旧 revision。``_commit_blueprint_authority_checkpoint``
同时校验两处，于是每一轮都必然报 ``BLUEPRINT_RESOLUTION_GRANT_INVALID``。
"""
from __future__ import annotations

import pytest

from app import db
from app.production.grant import issue_production_grant
from app.production.revision import (
    ensure_production_revision,
    get_active_production_revision,
)


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "grant-binding.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES('p1','demo','ready',?)",
        (db.now(),),
    )
    conn.execute(
        """INSERT INTO episodes(id, project_id, episode_no, title, source_chapters,
                                screenplay_status, status, created_at)
           VALUES('e1','p1',1,'第一集','[1]','pending','planned',?)""",
        (db.now(),),
    )
    conn.execute(
        """INSERT INTO episodes(id, project_id, episode_no, title, source_chapters,
                                screenplay_status, status, created_at)
           VALUES('e2','p1',2,'第二集','[2]','pending','planned',?)""",
        (db.now(),),
    )
    conn.commit()
    yield


def _grant_row(grant_id: str):
    return db.get_conn().execute(
        "SELECT * FROM production_grants WHERE id=?", (grant_id,)
    ).fetchone()


def test_grant_follows_the_revision_it_authorizes_across_a_rebuild() -> None:
    first = ensure_production_revision(
        episode_id="e1", kind="screenplay",
        input_fingerprint="fp-old", contract_version="c1",
        qa_profile_version="qa1", resume=False,
    )
    grant, _token = issue_production_grant(
        episode_id="e1", project_id="p1",
        production_revision_id=first.id, kind="screenplay",
        input_artifact_hash="sha256:receipts",
        issued_by="user_retry_approval",
    )

    # 输入指纹变化 ⇒ 合法地新建一条 revision，授权跟着这次运行走。
    second = ensure_production_revision(
        episode_id="e1", kind="screenplay",
        input_fingerprint="fp-new", contract_version="c1",
        qa_profile_version="qa1", grant_id=grant.grant_id, resume=True,
    )

    assert second.id != first.id
    assert second.grant_id == grant.grant_id
    row = _grant_row(grant.grant_id)
    # 两处绑定必须一致，否则下游同时校验二者的门禁永远不可满足。
    assert row["production_revision_id"] == second.id
    assert get_active_production_revision("e1", "screenplay").id == second.id


def test_grant_rebinding_never_crosses_episodes() -> None:
    other = ensure_production_revision(
        episode_id="e2", kind="screenplay", resume=False,
    )
    grant, _token = issue_production_grant(
        episode_id="e2", project_id="p1",
        production_revision_id=other.id, kind="screenplay",
        issued_by="user_retry_approval",
    )

    ensure_production_revision(
        episode_id="e1", kind="screenplay",
        input_fingerprint="fp", contract_version="c1",
        qa_profile_version="qa1", grant_id=grant.grant_id, resume=False,
    )

    # 另一集的授权不得被本集的 revision 抢走。
    assert _grant_row(grant.grant_id)["production_revision_id"] == other.id


def test_revoked_grant_is_not_rebound() -> None:
    first = ensure_production_revision(
        episode_id="e1", kind="screenplay", resume=False,
    )
    grant, _token = issue_production_grant(
        episode_id="e1", project_id="p1",
        production_revision_id=first.id, kind="screenplay",
        issued_by="user_retry_approval",
    )
    conn = db.get_conn()
    conn.execute(
        "UPDATE production_grants SET revoked_at=? WHERE id=?",
        (db.now(), grant.grant_id),
    )
    conn.commit()

    ensure_production_revision(
        episode_id="e1", kind="screenplay",
        input_fingerprint="fp-new", contract_version="c1",
        qa_profile_version="qa1", grant_id=grant.grant_id, resume=True,
    )

    assert _grant_row(grant.grant_id)["production_revision_id"] == first.id
