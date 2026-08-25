"""episode_prep_pack -> EpisodeScreenplay storyboard projection.

docs/TRANSFORM_FREEZE_PLAN.md P1: storyboard's contract input switched from
episode_screenplay to episode_prep_pack, but the storyboard stage itself
still consumes the legacy EpisodeScreenplay shape (narrative_plan is None
branch). app.production.screenplay_authority.project_prep_pack_to_screenplay
is the deterministic projection built for that switch; this file covers:

  1. the projection is a pure, deterministic, non-fabricating function
     (full_script_text is a quote splice, plot_spine/narrative_plan stay
     None, the identity triad is a lossless passthrough);
  2. the previously-silent "EpisodeScreenplay.model_validate(prep_pack
     payload) parses into an almost-empty object" landmine is closed
     (EpisodeScreenplay is now extra="forbid", and every parse site that can
     see a prep_pack payload routes it through the projection instead);
  3. resolve_current_screenplay_authority / resolve_downstream_screenplay
     (the "throat point" every downstream stage resolves through) correctly
     dispatch a published episode_prep_pack artifact to the new prep_pack
     authority chain, without disturbing the legacy screenplay_document
     chain;
  4. the storyboard contract's declared input_types matches what the
     screenplay stage actually publishes.

The EP6 fixture (tests/fixtures/episode_prep_pack_ep6_ep_94adca9b9942.json)
is a verbatim, read-only export of the real published episode_prep_pack for
ep_94adca9b9942 (artifact art_1fe89a0875fe) from data/manju.db -- the exact
episode named in this task's key test (许清/李富贵, both portrait-bound).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import db
from app.evidence import repository as evidence_repository
from app.harness.contracts import get_contract
from app.harness.types import EvidenceArtifact
from app.production.screenplay_authority import (
    PREP_PACK_PROJECTION_FORMAT_NOTE,
    is_prep_pack_payload,
    project_prep_pack_to_screenplay,
    resolve_current_screenplay_authority,
    resolve_downstream_screenplay,
)
from app.schemas import EpisodeScreenplay

EP6_FIXTURE = (
    Path(__file__).parent / "fixtures" / "episode_prep_pack_ep6_ep_94adca9b9942.json"
)


def _ep6_payload() -> dict:
    return json.loads(EP6_FIXTURE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Pure projection tests -- no DB, project_prep_pack_to_screenplay is a pure
# function of its payload argument.
# ---------------------------------------------------------------------------


def test_rejects_a_payload_without_the_prep_pack_marker():
    with pytest.raises(ValueError):
        project_prep_pack_to_screenplay({"episode_no": 1, "full_script_text": ""})


def test_ep6_identity_triad_survives_the_projection():
    """Task's key test: 许清/李富贵's visual_entity_id/portrait_id/
    display_appellation must not be dropped by the projection."""
    payload = _ep6_payload()
    screenplay = project_prep_pack_to_screenplay(payload)
    by_name = {a.display_name: a for a in screenplay.prep_pack_character_assets}

    assert by_name["许清"].portrait_id == "portrait_e01eec6ef5ef"
    assert by_name["许清"].visual_entity_id == "bible:许清"
    assert by_name["许清"].display_appellation == "许师姐"

    assert by_name["李富贵"].portrait_id == "portrait_9e2209df3692"
    assert by_name["李富贵"].visual_entity_id == "bible:李富贵"
    assert by_name["李富贵"].display_appellation == "小胖子"

    # scene identity triad's equivalent (scene_reference_id) is carried too.
    by_scene_id = {a.scene_id: a for a in screenplay.prep_pack_scene_assets}
    assert by_scene_id["scene:靠山宗外宗广场"].scene_reference_id == "scene_425e99e10132"


def test_full_script_text_is_a_quote_splice_never_authored_prose():
    payload = _ep6_payload()
    screenplay = project_prep_pack_to_screenplay(payload)
    assert screenplay.script_format_note == PREP_PACK_PROJECTION_FORMAT_NOTE

    verbatim_texts = set()
    for event in payload["event_chain"]:
        for item in event["source_evidence"]:
            verbatim_texts.add(item["quote"].strip())
        for item in event["key_lines"]:
            verbatim_texts.add(f"{item['speaker']}：{item['line']}")

    lines = [line for line in screenplay.full_script_text.splitlines() if line.strip()]
    assert lines, "projection produced no text at all"
    for line in lines:
        assert line in verbatim_texts, f"line not traceable to a real quote: {line!r}"


def test_scene_outline_characters_use_bible_display_name_not_the_appellation():
    """app.portraits.ensure_cards_for_screenplay's legacy (non-narrative_plan)
    branch matches scene_outline[].characters against Bible character
    *names*. Using the in-episode appellation (许师姐) instead of the
    canonical display_name (许清) would make a real, already-carded
    character look off-bible and wrongly block asset prep."""
    payload = _ep6_payload()
    screenplay = project_prep_pack_to_screenplay(payload)
    all_characters = {c for scene in screenplay.scene_outline for c in scene.characters}
    assert "许清" in all_characters
    assert "李富贵" in all_characters
    assert "许师姐" not in all_characters
    assert "小胖子" not in all_characters


def test_functional_extras_never_enter_scene_outline_characters():
    """Same reason as above, other direction: a functional extra's label is
    never a Bible name, so it must never be asked to resolve as one."""
    payload = _ep6_payload()
    screenplay = project_prep_pack_to_screenplay(payload)
    extra_labels = {
        e["label"] for e in payload["asset_manifest"].get("functional_extras", [])
    }
    assert extra_labels, "fixture should exercise a functional extra"
    all_characters = {c for scene in screenplay.scene_outline for c in scene.characters}
    assert not (extra_labels & all_characters)


def test_plot_spine_and_narrative_plan_are_left_none_not_fabricated():
    payload = _ep6_payload()
    screenplay = project_prep_pack_to_screenplay(payload)
    assert screenplay.plot_spine is None
    assert screenplay.narrative_plan is None


def test_scene_no_is_contiguous_from_one():
    payload = _ep6_payload()
    screenplay = project_prep_pack_to_screenplay(payload)
    assert [s.scene_no for s in screenplay.scene_outline] == list(
        range(1, len(screenplay.scene_outline) + 1)
    )


def test_key_lines_are_derived_from_real_quotes_only():
    payload = _ep6_payload()
    screenplay = project_prep_pack_to_screenplay(payload)
    real_lines = {
        item["line"].strip()
        for event in payload["event_chain"]
        for item in event["key_lines"]
    }
    assert screenplay.key_lines
    for key_line in screenplay.key_lines:
        _speaker, _sep, line = key_line.partition("：")
        assert (line or key_line) in real_lines


def test_projection_is_deterministic():
    payload = _ep6_payload()
    first = project_prep_pack_to_screenplay(payload).model_dump(mode="json")
    second = project_prep_pack_to_screenplay(payload).model_dump(mode="json")
    assert first == second


# ---------------------------------------------------------------------------
# Landmine: EpisodeScreenplay.model_validate(prep_pack payload) must never
# silently succeed with an almost-empty object again.
# ---------------------------------------------------------------------------


def test_bare_model_validate_on_a_prep_pack_payload_now_raises_loud():
    payload = _ep6_payload()
    with pytest.raises(Exception):
        EpisodeScreenplay.model_validate(payload)


def test_is_prep_pack_payload_predicate():
    assert is_prep_pack_payload(_ep6_payload())
    assert not is_prep_pack_payload({"episode_no": 1, "full_script_text": ""})
    assert not is_prep_pack_payload(None)
    assert not is_prep_pack_payload([])


# ---------------------------------------------------------------------------
# Contract declaration consistency.
# ---------------------------------------------------------------------------


def test_storyboard_input_types_match_what_screenplay_actually_publishes():
    assert get_contract("screenplay").output_type == "episode_prep_pack"
    assert get_contract("storyboard").input_types == ["episode_prep_pack"]


# ---------------------------------------------------------------------------
# resolve_current_screenplay_authority / resolve_downstream_screenplay
# dispatch, against a *really published* episode_prep_pack artifact (built
# through app.production.prep_pack._publish_prep_pack itself -- the actual
# atomic-publish transaction, not a hand-rolled stand-in for it).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "prep-pack-authority.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()


def _seed_episode(conn, *, episode_id: str, project_id: str, episode_no: int) -> None:
    conn.execute(
        "INSERT INTO projects(id,name,bible_json,created_at) VALUES(?,?,?,?)",
        (project_id, "prep pack fixture", "{}", db.now()),
    )
    conn.execute(
        """INSERT INTO episodes(
               id,project_id,episode_no,title,source_chapters,target_duration_s,
               status,screenplay_status,screenplay_character_resolutions,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            episode_id, project_id, episode_no, "Fixture", "[]", 1800,
            "planned", "pending", "[]", db.now(),
        ),
    )
    conn.commit()


def _publish_ep6_fixture(conn, episode_id: str, *, project_id: str = "proj-1") -> dict:
    from app.production import prep_pack

    _seed_episode(conn, episode_id=episode_id, project_id=project_id, episode_no=6)
    payload = _ep6_payload()
    # The fixture is a frozen historical export (prep_pack_version "1.11.0"
    # at export time); _publish_prep_pack's QA evaluation always records
    # today's running app.production.prep_pack.PREP_PACK_VERSION as evidence
    # -- in real flows the payload is generated and published in the same
    # call so the two can never diverge. Align the fixture to the currently
    # running constant so this test exercises a *legitimately consistent*
    # publish rather than fabricating a version drift that could never occur
    # in reality (see resolve_current_screenplay_authority's prep_pack_version
    # cross-check, which is exactly this invariant).
    payload["prep_pack_version"] = prep_pack.PREP_PACK_VERSION
    return prep_pack._publish_prep_pack(episode_id=episode_id, payload=payload, run_id=None)


def test_resolve_current_screenplay_authority_resolves_published_prep_pack():
    conn = db.get_conn()
    episode_id = "ep-prep-pack-1"
    _publish_ep6_fixture(conn, episode_id)

    resolved = resolve_current_screenplay_authority(
        episode_id, conn=conn, require_narrative=False,
    )
    assert resolved.screenplay.episode_no == 6
    assert resolved.screenplay.scene_outline
    by_name = {a.display_name: a for a in resolved.screenplay.prep_pack_character_assets}
    assert by_name["许清"].portrait_id == "portrait_e01eec6ef5ef"
    assert by_name["李富贵"].portrait_id == "portrait_9e2209df3692"


def test_require_narrative_true_fails_closed_for_prep_pack_not_silently():
    conn = db.get_conn()
    episode_id = "ep-prep-pack-2"
    _publish_ep6_fixture(conn, episode_id)
    with pytest.raises(ValueError, match="narrative_plan"):
        resolve_current_screenplay_authority(episode_id, conn=conn, require_narrative=True)


def test_resolve_downstream_screenplay_dispatches_prep_pack_correctly():
    conn = db.get_conn()
    episode_id = "ep-prep-pack-3"
    _publish_ep6_fixture(conn, episode_id)

    ctx = resolve_downstream_screenplay(episode_id, conn=conn)
    assert ctx.immutable_authority_required is True
    assert ctx.narrative_authority_required is False
    assert ctx.screenplay.scene_outline
    assert ctx.screenplay.full_script_text


def test_artifact_content_drift_is_rejected_not_silently_served():
    conn = db.get_conn()
    episode_id = "ep-prep-pack-4"
    result = _publish_ep6_fixture(conn, episode_id)
    tampered = {
        "prep_pack_version": "9.9.9", "episode_no": 6,
        "episode_scope": {"chapter_indexes": [6], "source_segment_count": 1},
        "event_chain": [], "asset_manifest": {"characters": [], "scenes": []},
        "coverage_ledger": {
            "total_segments": 0, "delivered": [], "merged": [],
            "retained_as_context": [], "proven_duplicates": [], "uncovered": [],
        },
        "hook": "", "cliffhanger": "",
    }
    conn.execute(
        "UPDATE artifacts SET content_json=? WHERE id=?",
        (json.dumps(tampered, ensure_ascii=False), result["artifact_id"]),
    )
    conn.commit()
    with pytest.raises(ValueError):
        resolve_current_screenplay_authority(episode_id, conn=conn, require_narrative=False)


def test_legacy_screenplay_document_type_still_takes_the_legacy_branch():
    """A non-prep_pack published Artifact must still hit the ORIGINAL legacy
    validity check untouched -- proves the new prep_pack dispatch branch
    added ahead of it does not swallow legacy artifacts."""
    conn = db.get_conn()
    episode_id = "ep-legacy-1"
    _seed_episode(conn, episode_id=episode_id, project_id="proj-legacy", episode_no=1)
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id=episode_id,
        status="candidate",  # deliberately not "approved"
        trust_level="T1",
        content={"episode_no": 1, "full_script_text": ""},
        contract_version="4.0.0",
    ))
    conn.execute(
        "UPDATE episodes SET screenplay_artifact_id=?, published_screenplay_artifact_id=? "
        "WHERE id=?",
        (artifact["id"], artifact["id"], episode_id),
    )
    conn.commit()
    with pytest.raises(ValueError, match="已发布剧本 Artifact 的类型、作用域或状态无效"):
        resolve_current_screenplay_authority(episode_id, conn=conn, require_narrative=False)


def test_storyboard_certificate_narrative_authority_check_does_not_crash_on_prep_pack():
    """app.production.certificate._narrative_screenplay_for_artifact's
    kind="storyboard" branch reads the episode's CURRENT screenplay_json
    directly (not through resolve_downstream_screenplay) to answer "does
    this episode use narrative authority at all". Before EpisodeScreenplay
    was extra="forbid" this silently parsed a prep_pack payload into an
    almost-empty object (narrative_plan=None) and correctly answered "no".
    After extra="forbid" it must still answer "no" without raising --
    otherwise every storyboard-certificate verification for a prep_pack
    episode (i.e. every storyboard publish) breaks."""
    from app.production.certificate import _narrative_screenplay_for_artifact

    conn = db.get_conn()
    episode_id = "ep-prep-pack-cert-1"
    _publish_ep6_fixture(conn, episode_id)

    result = _narrative_screenplay_for_artifact(
        kind="storyboard", scope_id=episode_id, artifact={}, conn=conn,
    )
    assert result is None
