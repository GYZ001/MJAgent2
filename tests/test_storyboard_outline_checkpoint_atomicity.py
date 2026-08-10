from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from app import db
from app.evidence import repository as evidence_repository
from app.harness.types import EvidenceArtifact
from app.production.revision import ensure_production_revisions_table
from app.schemas import StoryboardOutline, StoryboardOutlineShot
from app.storyboard_authority import (
    StoryboardOutlineAuthorityError,
    persist_storyboard_outline_authority,
    resolve_storyboard_outline_authority,
)
from app.storyboard_supervisor import (
    SupervisorCheckpoint,
    load_latest_checkpoint,
    save_checkpoint,
)


@pytest.fixture()
def outline_checkpoint_db(tmp_path, monkeypatch):
    current = getattr(db._local, "conn", None)
    if current is not None:
        current.close()
    database = tmp_path / "storyboard-outline-checkpoint.db"
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_local", threading.local())
    db.init_db()
    ensure_production_revisions_table()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id,name,status,created_at) VALUES('p1','P','planned',1)"
    )
    conn.execute(
        """INSERT INTO episodes(
               id,project_id,episode_no,title,target_duration_s,status,
               screenplay_artifact_id,created_at
           ) VALUES('e1','p1',1,'E',30,'scripting','screenplay-v1',1)"""
    )
    conn.execute(
        """INSERT INTO production_revisions(
               id,episode_id,kind,status,input_fingerprint,contract_version,
               qa_profile_version,checkpoint_json,created_at,updated_at
           ) VALUES(
               'storyboard-revision','e1','storyboard','active','input-v1',
               'storyboard-test.v1','storyboard-test-qa.v1','{}',1,1
           )"""
    )
    conn.execute(
        """UPDATE episodes
              SET storyboard_production_revision_id='storyboard-revision'
            WHERE id='e1'"""
    )
    conn.commit()
    yield conn
    current = getattr(db._local, "conn", None)
    if current is not None:
        current.close()
        db._local.conn = None


def _outline(revision: int) -> StoryboardOutline:
    return StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=shot_no,
                duration_s=10,
                beat=f"r{revision}-beat-{shot_no}",
            )
            for shot_no in range(1, revision + 2)
        ],
    )


def _outline_artifact(
    revision: int,
    *,
    prompt_version: str,
) -> tuple[StoryboardOutline, dict]:
    outline = _outline(revision)
    artifact = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="storyboard_outline",
            scope_type="episode",
            scope_id="e1",
            status="validated",
            trust_level="T2",
            content=outline.model_dump(mode="json"),
            parent_artifact_ids=["screenplay-v1"],
            contract_version="storyboard-test.v1",
            prompt_version=prompt_version,
        )
    )
    return outline, artifact


def _checkpoint_for(authority, *, candidate_outline=None) -> SupervisorCheckpoint:
    artifact = evidence_repository.get_artifact(authority.artifact_id)
    assert artifact is not None
    last_repair = None
    if candidate_outline is not None:
        last_repair = {
            "status": "candidate_pending",
            "candidate_outline": candidate_outline.model_dump(mode="json"),
        }
    return SupervisorCheckpoint(
        episode_id="e1",
        phase="REPAIRING" if last_repair else "VALIDATING_OUTLINE",
        outline_artifact_id=authority.artifact_id,
        expected_total=len(authority.outline.shots),
        input_versions={
            "screenplay_artifact_id": "screenplay-v1",
            "storyboard_outline_artifact_id": authority.artifact_id,
            "storyboard_outline_revision": str(authority.revision),
            "storyboard_outline_fingerprint": authority.fingerprint,
            "storyboard_outline_prompt_version": artifact["prompt_version"],
        },
        last_repair=last_repair,
    )


def _adopt(revision: int, *, expected_artifact_id: str | None = None):
    prompt_version = f"storyboard-outline-compiler-{revision}.0.0"
    outline, artifact = _outline_artifact(
        revision,
        prompt_version=prompt_version,
    )
    kwargs = {}
    if expected_artifact_id is not None:
        kwargs["expected_outline_artifact_id"] = expected_artifact_id
    authority = persist_storyboard_outline_authority(
        "e1",
        outline,
        artifact_id=artifact["id"],
        **kwargs,
    )
    return authority


def _episode_authority_row(conn) -> dict:
    return dict(
        conn.execute(
            """SELECT storyboard_outline_artifact_id,
                      storyboard_outline_revision,
                      storyboard_outline_fingerprint,
                      storyboard_outline_json,target_duration_s
                 FROM episodes WHERE id='e1'"""
        ).fetchone()
    )


def _authority_prompt_version(authority) -> str:
    artifact = evidence_repository.get_artifact(authority.artifact_id)
    assert artifact is not None
    return str(artifact["prompt_version"] or "")


def _persisted_checkpoint_payload(conn) -> tuple[dict, dict]:
    revision = conn.execute(
        """SELECT checkpoint_json FROM production_revisions
            WHERE id='storyboard-revision'"""
    ).fetchone()
    artifact = conn.execute(
        """SELECT content_json FROM artifacts
            WHERE type='storyboard_supervisor_checkpoint' AND scope_id='e1'
            ORDER BY version DESC LIMIT 1"""
    ).fetchone()
    assert revision is not None
    assert artifact is not None
    return (
        json.loads(revision["checkpoint_json"])["supervisor_checkpoint"],
        json.loads(artifact["content_json"]),
    )


def test_outline_revisions_advance_episode_and_checkpoint_as_one_authority(
    outline_checkpoint_db,
) -> None:
    r1 = _adopt(1)
    save_checkpoint(_checkpoint_for(r1))

    r2 = _adopt(2)
    r3 = _adopt(3)

    episode = _episode_authority_row(outline_checkpoint_db)
    checkpoint = load_latest_checkpoint("e1")
    assert checkpoint is not None
    assert episode["storyboard_outline_artifact_id"] == r3.artifact_id
    assert episode["storyboard_outline_revision"] == r3.revision
    assert episode["storyboard_outline_fingerprint"] == r3.fingerprint
    assert checkpoint.outline_artifact_id == r3.artifact_id
    assert checkpoint.expected_total == len(r3.outline.shots)
    assert checkpoint.input_versions == {
        "screenplay_artifact_id": "screenplay-v1",
        "storyboard_outline_artifact_id": r3.artifact_id,
        "storyboard_outline_revision": str(r3.revision),
        "storyboard_outline_fingerprint": r3.fingerprint,
        "storyboard_outline_prompt_version": _authority_prompt_version(r3),
    }
    revision_checkpoint, artifact_checkpoint = _persisted_checkpoint_payload(
        outline_checkpoint_db
    )
    assert revision_checkpoint == artifact_checkpoint
    assert revision_checkpoint["outline_artifact_id"] == r3.artifact_id
    assert r1.artifact_id != r2.artifact_id != r3.artifact_id


def test_stale_outline_compare_and_swap_cannot_overwrite_newer_authority(
    outline_checkpoint_db,
) -> None:
    r1 = _adopt(1)
    save_checkpoint(_checkpoint_for(r1))
    r2 = _adopt(2, expected_artifact_id=r1.artifact_id)
    before = _episode_authority_row(outline_checkpoint_db)

    with pytest.raises(
        StoryboardOutlineAuthorityError,
        match="CAS|并发冲突",
    ):
        _adopt(3, expected_artifact_id=r1.artifact_id)

    assert _episode_authority_row(outline_checkpoint_db) == before
    checkpoint = load_latest_checkpoint("e1")
    assert checkpoint is not None
    assert checkpoint.outline_artifact_id == r2.artifact_id


def test_concurrent_outline_writers_allow_only_one_old_compare_and_swap(
    outline_checkpoint_db,
) -> None:
    r1 = _adopt(1)
    save_checkpoint(_checkpoint_for(r1))
    outline2, artifact2 = _outline_artifact(
        2,
        prompt_version="storyboard-outline-compiler-2.0.0",
    )
    outline3, artifact3 = _outline_artifact(
        3,
        prompt_version="storyboard-outline-compiler-3.0.0",
    )
    barrier = threading.Barrier(2)

    def compete(outline, artifact):
        barrier.wait()
        try:
            return persist_storyboard_outline_authority(
                "e1",
                outline,
                artifact_id=artifact["id"],
                expected_outline_artifact_id=r1.artifact_id,
            )
        except StoryboardOutlineAuthorityError as exc:
            return exc
        finally:
            connection = getattr(db._local, "conn", None)
            if connection is not None:
                connection.close()
                db._local.conn = None

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda case: compete(*case),
                [
                    (outline2, artifact2),
                    (outline3, artifact3),
                ],
            )
        )

    adopted = [
        outcome
        for outcome in outcomes
        if not isinstance(outcome, Exception)
    ]
    rejected = [
        outcome
        for outcome in outcomes
        if isinstance(outcome, StoryboardOutlineAuthorityError)
    ]
    assert len(adopted) == 1
    assert len(rejected) == 1
    assert "CAS" in str(rejected[0])
    winner = adopted[0]
    episode = _episode_authority_row(outline_checkpoint_db)
    checkpoint = load_latest_checkpoint("e1")
    assert checkpoint is not None
    assert episode["storyboard_outline_artifact_id"] == winner.artifact_id
    assert episode["storyboard_outline_revision"] == winner.revision
    assert episode["storyboard_outline_fingerprint"] == winner.fingerprint
    assert checkpoint.outline_artifact_id == winner.artifact_id


def test_checkpoint_persistence_failure_rolls_back_outline_authority(
    outline_checkpoint_db,
    monkeypatch,
) -> None:
    r1 = _adopt(1)
    save_checkpoint(_checkpoint_for(r1))
    before_episode = _episode_authority_row(outline_checkpoint_db)
    before_checkpoints = outline_checkpoint_db.execute(
        """SELECT COUNT(*) FROM artifacts
             WHERE type='storyboard_supervisor_checkpoint'"""
    ).fetchone()[0]
    before_revision_checkpoint, before_artifact_checkpoint = (
        _persisted_checkpoint_payload(outline_checkpoint_db)
    )
    create_artifact = evidence_repository.create_artifact

    def fail_checkpoint_artifact(artifact, **kwargs):
        if artifact.type == "storyboard_supervisor_checkpoint":
            raise OSError("injected checkpoint persistence failure")
        return create_artifact(artifact, **kwargs)

    monkeypatch.setattr(
        evidence_repository,
        "create_artifact",
        fail_checkpoint_artifact,
    )
    outline, artifact = _outline_artifact(
        2,
        prompt_version="storyboard-outline-compiler-2.0.0",
    )

    with pytest.raises(
        OSError,
        match="injected checkpoint persistence failure",
    ):
        persist_storyboard_outline_authority(
            "e1",
            outline,
            artifact_id=artifact["id"],
            expected_outline_artifact_id=r1.artifact_id,
        )

    assert _episode_authority_row(outline_checkpoint_db) == before_episode
    assert outline_checkpoint_db.execute(
        """SELECT COUNT(*) FROM artifacts
             WHERE type='storyboard_supervisor_checkpoint'"""
    ).fetchone()[0] == before_checkpoints
    checkpoint = load_latest_checkpoint("e1")
    assert checkpoint is not None
    assert checkpoint.outline_artifact_id == r1.artifact_id
    revision_checkpoint, artifact_checkpoint = _persisted_checkpoint_payload(
        outline_checkpoint_db
    )
    assert revision_checkpoint == before_revision_checkpoint
    assert artifact_checkpoint == before_artifact_checkpoint


def test_restart_reads_episode_authority_and_discards_stale_outline_candidate(
    outline_checkpoint_db,
    monkeypatch,
) -> None:
    r1 = _adopt(1)
    stale_checkpoint = _checkpoint_for(r1, candidate_outline=r1.outline)
    save_checkpoint(stale_checkpoint)
    r2 = _adopt(2)
    r3 = _adopt(3)

    stale_payload = stale_checkpoint.model_dump(mode="json")
    outline_checkpoint_db.execute(
        """UPDATE production_revisions
              SET checkpoint_json=?,updated_at=999
            WHERE id='storyboard-revision'""",
        (
            json.dumps(
                {"supervisor_checkpoint": stale_payload},
                ensure_ascii=False,
            ),
        ),
    )
    outline_checkpoint_db.commit()
    outline_checkpoint_db.close()
    monkeypatch.setattr(db, "_local", threading.local())

    recovered_authority = resolve_storyboard_outline_authority("e1")
    recovered_checkpoint = load_latest_checkpoint("e1")

    assert recovered_checkpoint is not None
    assert recovered_authority.artifact_id == r3.artifact_id
    assert recovered_authority.artifact_id != r1.artifact_id
    assert recovered_authority.artifact_id != r2.artifact_id
    assert recovered_checkpoint.outline_artifact_id == r3.artifact_id
    assert recovered_checkpoint.expected_total == len(r3.outline.shots)
    assert recovered_checkpoint.last_repair is None
    assert recovered_checkpoint.repair_candidate_shots == []
    assert recovered_checkpoint.input_versions[
        "storyboard_outline_prompt_version"
    ] == _authority_prompt_version(r3)
    assert db.get_conn().execute(
        "SELECT COUNT(*) FROM provider_calls"
    ).fetchone()[0] == 0
