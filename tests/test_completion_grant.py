"""StoryboardCompletionGrant 单元测试。"""
from __future__ import annotations

import pytest

from app import db
from app.completion_grant import (
    GrantValidationError,
    consume_grant,
    issue_completion_grant,
    revoke_grant,
    validate_grant_for_confirm,
)


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "grant.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    yield
    monkeypatch.setattr(db._local, "conn", None, raising=False)


def test_grant_bound_to_episode():
    grant, _token = issue_completion_grant(
        episode_id="ep_a",
        project_id="proj_1",
        screenplay_artifact_id="art_sp_1",
        bible_artifact_id="art_bible_1",
    )
    with pytest.raises(GrantValidationError) as exc:
        validate_grant_for_confirm(
            grant.grant_id,
            episode_id="ep_other",
            screenplay_artifact_id="art_sp_1",
            bible_artifact_id="art_bible_1",
        )
    assert exc.value.code == "GRANT_EPISODE_MISMATCH"


def test_grant_invalid_when_screenplay_changes():
    grant, _ = issue_completion_grant(
        episode_id="ep_a",
        project_id="proj_1",
        screenplay_artifact_id="art_sp_1",
        bible_artifact_id="art_bible_1",
    )
    with pytest.raises(GrantValidationError) as exc:
        validate_grant_for_confirm(
            grant.grant_id,
            episode_id="ep_a",
            screenplay_artifact_id="art_sp_CHANGED",
            bible_artifact_id="art_bible_1",
        )
    assert exc.value.code == "UPSTREAM_VERSION_CHANGED"


def test_consume_is_one_shot():
    grant, _ = issue_completion_grant(
        episode_id="ep_a",
        project_id="proj_1",
        screenplay_artifact_id="art_sp_1",
        bible_artifact_id=None,
    )
    ok = validate_grant_for_confirm(
        grant.grant_id,
        episode_id="ep_a",
        screenplay_artifact_id="art_sp_1",
        bible_artifact_id=None,
    )
    assert ok.grant_id == grant.grant_id
    consume_grant(grant.grant_id)
    with pytest.raises(GrantValidationError) as exc:
        validate_grant_for_confirm(
            grant.grant_id,
            episode_id="ep_a",
            screenplay_artifact_id="art_sp_1",
            bible_artifact_id=None,
        )
    assert exc.value.code == "GRANT_CONSUMED"


def test_revoke_blocks_confirm():
    grant, _ = issue_completion_grant(
        episode_id="ep_a",
        project_id="proj_1",
        screenplay_artifact_id="art_sp_1",
    )
    revoke_grant(grant.grant_id)
    with pytest.raises(GrantValidationError) as exc:
        validate_grant_for_confirm(
            grant.grant_id,
            episode_id="ep_a",
            screenplay_artifact_id="art_sp_1",
            bible_artifact_id=None,
        )
    assert exc.value.code == "GRANT_REVOKED"
