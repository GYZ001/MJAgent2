import threading

import pytest

from app import completion_grant, db


@pytest.fixture
def ledger_db(tmp_path, monkeypatch):
    database = tmp_path / "legacy-video-liability.db"
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_local", threading.local())
    db.init_db()
    conn = db.get_conn()
    yield conn
    conn.close()


def _seed_version(
    conn,
    *,
    project_id: str,
    episode_id: str,
    episode_no: int,
    amount_cny: float,
) -> str:
    shot_id = f"s-{episode_id}"
    version_id = f"v-{episode_id}"
    conn.execute(
        "INSERT OR IGNORE INTO projects(id,name,status,created_at) "
        "VALUES(?,?,'created',1)",
        (project_id, project_id),
    )
    conn.execute(
        """INSERT INTO episodes(id,project_id,episode_no,status,created_at)
           VALUES(?,?,?,'confirmed',1)""",
        (episode_id, project_id, episode_no),
    )
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s) VALUES(?,?,1,5)",
        (shot_id, episode_id),
    )
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,
               provider_task_id,cost_cny,created_at
           ) VALUES(?,?,1,'prompt',?,'succeeded',?,?,2)""",
        (
            version_id,
            shot_id,
            f"idem-{episode_id}",
            f"task-{episode_id}",
            amount_cny,
        ),
    )
    return version_id


def test_init_db_migrates_unowned_legacy_video_liability_once(ledger_db) -> None:
    _seed_version(
        ledger_db,
        project_id="p1",
        episode_id="e1",
        episode_no=1,
        amount_cny=4,
    )
    ledger_db.commit()

    db.init_db()

    first = ledger_db.execute(
        """SELECT operation_id,project_id,episode_id,shot_id,job_id,version_id,
                  origin_episode_id,origin_shot_id,origin_job_id,
                  origin_version_id,amount_cny,status,liability_source,
                  created_at,updated_at,settled_at
             FROM provider_video_budget_claims"""
    ).fetchall()
    assert len(first) == 1
    assert dict(first[0]) == {
        "operation_id": "legacy-video-liability:v-e1",
        "project_id": "p1",
        "episode_id": "e1",
        "shot_id": "s-e1",
        "job_id": None,
        "version_id": "v-e1",
        "origin_episode_id": "e1",
        "origin_shot_id": "s-e1",
        "origin_job_id": "legacy-version:v-e1",
        "origin_version_id": "v-e1",
        "amount_cny": 4.0,
        "status": "settled",
        "liability_source": "legacy_version_migration",
        "created_at": 2.0,
        "updated_at": first[0]["updated_at"],
        "settled_at": first[0]["settled_at"],
    }
    assert first[0]["updated_at"] == first[0]["settled_at"]

    db.init_db()

    repeated = ledger_db.execute(
        """SELECT operation_id,liability_source,updated_at,settled_at
             FROM provider_video_budget_claims"""
    ).fetchall()
    assert [dict(row) for row in repeated] == [{
        "operation_id": "legacy-video-liability:v-e1",
        "liability_source": "legacy_version_migration",
        "updated_at": first[0]["updated_at"],
        "settled_at": first[0]["settled_at"],
    }]


def test_project_budget_mixed_ownership_counts_each_liability_once(
    ledger_db,
) -> None:
    for episode_no, episode_id, amount in (
        (1, "e-authority", 3),
        (2, "e-claimed", 4),
        (3, "e-unmigrated", 5),
    ):
        _seed_version(
            ledger_db,
            project_id="p1",
            episode_id=episode_id,
            episode_no=episode_no,
            amount_cny=amount,
        )
    ledger_db.execute(
        """INSERT INTO episode_video_budget_authorities(
               episode_id,baseline_cny,cap_cny,source,authorized_at,updated_at
           ) VALUES('e-authority',3,10,'legacy-authority',2,2)"""
    )
    ledger_db.execute(
        """INSERT INTO provider_video_budget_claims(
               operation_id,project_id,episode_id,shot_id,job_id,version_id,
               origin_episode_id,origin_shot_id,origin_job_id,origin_version_id,
               amount_cny,status,created_at,updated_at,accepted_at,settled_at
           ) VALUES(
               'provider-op','p1','e-claimed','s-e-claimed',NULL,'v-e-claimed',
               'e-claimed','s-e-claimed','deleted-job','v-e-claimed',
               4,'settled',2,2,2,2
           )"""
    )
    ledger_db.commit()

    assert completion_grant.project_video_budget_snapshot(
        "p1",
        conn=ledger_db,
    ) == {
        "baseline_cny": 3.0,
        "legacy_cny": 5.0,
        "claimed_cny": 4.0,
        "used_cny": 12.0,
    }

    db.init_db()

    assert completion_grant.project_video_budget_snapshot(
        "p1",
        conn=ledger_db,
    ) == {
        "baseline_cny": 3.0,
        "legacy_cny": 0.0,
        "claimed_cny": 9.0,
        "used_cny": 12.0,
    }
    claims = ledger_db.execute(
        """SELECT origin_episode_id,origin_version_id,amount_cny,liability_source
             FROM provider_video_budget_claims
            ORDER BY origin_episode_id"""
    ).fetchall()
    assert [dict(row) for row in claims] == [
        {
            "origin_episode_id": "e-claimed",
            "origin_version_id": "v-e-claimed",
            "amount_cny": 4.0,
            "liability_source": "provider_operation",
        },
        {
            "origin_episode_id": "e-unmigrated",
            "origin_version_id": "v-e-unmigrated",
            "amount_cny": 5.0,
            "liability_source": "legacy_version_migration",
        },
    ]
