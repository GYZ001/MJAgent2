from __future__ import annotations

import threading

import pytest

from app import db
from app.schemas import StoryboardOutline, StoryboardOutlineShot
from app.storyboard_authority import (
    OUTLINE_AUTHORITY_VERSION,
    StoryboardOutlineAuthorityError,
    StoryboardOutlineMigrationRequired,
    persist_storyboard_outline_authority,
    resolve_storyboard_outline_authority,
)


@pytest.fixture()
def authority_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "duration-authority.db")
    monkeypatch.setattr(db, "_local", threading.local())
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id,name,status,created_at) VALUES('p','P','created',1)"
    )
    conn.execute(
        """INSERT INTO episodes(
               id,project_id,episode_no,target_duration_s,status,created_at
           ) VALUES('e','p',3,710,'scripting',1)"""
    )
    conn.commit()
    yield conn
    conn.close()


def _outline() -> StoryboardOutline:
    return StoryboardOutline(
        episode_no=3,
        shots=[
            StoryboardOutlineShot(shot_no=1, duration_s=5, beat="first"),
            StoryboardOutlineShot(shot_no=2, duration_s=10, beat="second"),
        ],
    )


def test_outline_authority_atomically_preserves_planning_estimate_and_restarts(
    authority_db,
    monkeypatch,
) -> None:
    authority = persist_storyboard_outline_authority("e", _outline())
    row = authority_db.execute(
        """SELECT target_duration_s,planning_target_duration_s,
                  planning_duration_source,target_duration_authority,
                  storyboard_outline_revision,storyboard_outline_fingerprint,
                  storyboard_outline_artifact_id,storyboard_outline_json
             FROM episodes WHERE id='e'"""
    ).fetchone()

    assert row["target_duration_s"] == 15
    assert row["planning_target_duration_s"] == 710
    assert row["planning_duration_source"]
    assert row["target_duration_authority"] == OUTLINE_AUTHORITY_VERSION
    assert row["storyboard_outline_revision"] == authority.revision
    assert row["storyboard_outline_fingerprint"] == authority.fingerprint
    assert row["storyboard_outline_artifact_id"] == authority.artifact_id
    assert row["storyboard_outline_json"] == authority.canonical_json

    authority_db.close()
    monkeypatch.setattr(db, "_local", threading.local())
    restarted = resolve_storyboard_outline_authority("e")
    assert restarted.authoritative_duration_s == 15
    assert restarted.planning_duration_s == 710
    assert restarted.fingerprint == authority.fingerprint


def test_outline_authority_mismatch_fails_closed(authority_db) -> None:
    persist_storyboard_outline_authority("e", _outline())
    authority_db.execute(
        "UPDATE episodes SET target_duration_s=710 WHERE id='e'"
    )
    authority_db.commit()

    with pytest.raises(
        StoryboardOutlineAuthorityError,
        match="stored=710, authoritative=15",
    ):
        resolve_storyboard_outline_authority("e")


def test_unversioned_outline_requires_explicit_cas_migration(authority_db) -> None:
    outline = _outline()
    raw = outline.model_dump_json()
    authority_db.execute(
        "UPDATE episodes SET storyboard_outline_json=? WHERE id='e'",
        (raw,),
    )
    authority_db.commit()

    with pytest.raises(StoryboardOutlineMigrationRequired):
        persist_storyboard_outline_authority("e", outline)
