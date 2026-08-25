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
    _split_prep_pack_spoken_line,
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
    """Each projected key_line is verbatim text from a real prep_pack quote --
    but not necessarily the *whole* quote: see
    test_key_lines_over_capacity_are_split_verbatim_not_rewritten below for
    why a too-long quote is deterministically split on punctuation
    boundaries into several shot-capacity-sized key_lines before this
    function returns. "Derived from a real quote" now means "a verbatim
    substring of one", not "textually identical to one".
    """
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
        text = line or key_line
        assert any(text in real for real in real_lines), (
            f"key_line not traceable to any real quote: {text!r}"
        )


def test_key_lines_over_capacity_are_split_verbatim_not_rewritten():
    """Real EP6 failure (run_8c369bc4da23, ERR-20260825-07c92e): prep_pack's
    key_lines[].line is a verbatim novel excerpt, not a pre-compressed
    spoken-form line (unlike the legacy screenplay generator's key_lines,
    which are always <= MAX_SPOKEN_CHARS_PER_SHOT by construction -- see
    app.screenplay_ir's `_split_spoken_line` call and
    project_prep_pack_to_screenplay's module comment for the historical-DB
    evidence). EP6's own prep_pack has a 74-char verbatim quote that fits no
    shot at all. The projection must split it -- deterministically, on
    punctuation boundaries, without touching a single character -- into
    units that each satisfy every shot's spoken-capacity ceiling.
    """
    from app import config
    from app.spoken_contract import content_char_count

    payload = _ep6_payload()
    screenplay = project_prep_pack_to_screenplay(payload)

    long_quotes = [
        item["line"].strip()
        for event in payload["event_chain"]
        for item in event["key_lines"]
        if content_char_count(item["line"]) > config.MAX_SPOKEN_CHARS_PER_SHOT
    ]
    assert long_quotes, "fixture should exercise at least one over-capacity quote"

    # 1) No projected key_line ever exceeds the largest single-shot budget.
    for key_line in screenplay.key_lines:
        _speaker, _sep, text = key_line.partition("：")
        text = text or key_line
        assert content_char_count(text) <= config.MAX_SPOKEN_CHARS_PER_SHOT, (
            f"key_line still exceeds shot capacity after projection: {key_line!r}"
        )
        assert text.strip(), "projection must never emit an empty key_line unit"

    # 2) Splitting a long quote is lossless: its parts, in projected order,
    #    concatenate back to the exact original bytes -- no rewriting, no
    #    dropped characters, no reordering within the quote itself.
    projected_texts = []
    for key_line in screenplay.key_lines:
        _speaker, _sep, text = key_line.partition("：")
        projected_texts.append(text or key_line)
    for quote in long_quotes:
        parts = [text for text in projected_texts if text and text in quote]
        assert parts, f"no projected parts trace back to over-capacity quote {quote!r}"
        assert "".join(parts) == quote, (
            f"split parts do not losslessly reconstruct the source quote: "
            f"{parts!r} vs {quote!r}"
        )


def test_projection_is_deterministic():
    payload = _ep6_payload()
    first = project_prep_pack_to_screenplay(payload).model_dump(mode="json")
    second = project_prep_pack_to_screenplay(payload).model_dump(mode="json")
    assert first == second


def _minimal_prep_pack(key_lines: list[dict]) -> dict:
    """Smallest payload project_prep_pack_to_screenplay actually reads from,
    for boundary cases the real EP6 fixture does not happen to contain."""
    return {
        "prep_pack_version": "1.5.0",
        "episode_no": 99,
        "episode_scope": {"chapter_indexes": [1]},
        "event_chain": [{
            "event_id": "ev_001",
            "order": 1,
            "summary": "边界用例",
            "source_evidence": [],
            "key_lines": key_lines,
        }],
        "asset_manifest": {"characters": [], "scenes": []},
        "cliffhanger": "",
    }


def test_empty_key_line_produces_no_unit_not_a_silent_placeholder():
    """An empty (or whitespace-only) line has nothing to speak; it must
    disappear entirely rather than surface as a hollow key_line entry that
    would occupy a KL* slot the outline is then forced to "assign"."""
    payload = _minimal_prep_pack([
        {"speaker": "甲", "line": "", "segment_index": 1},
        {"speaker": "甲", "line": "   ", "segment_index": 1},
        {"speaker": "甲", "line": "有效台词。", "segment_index": 1},
    ])
    screenplay = project_prep_pack_to_screenplay(payload)
    assert screenplay.key_lines == ["甲：有效台词。"]


def test_punctuation_only_key_line_is_kept_verbatim_and_never_over_capacity():
    """A line that is pure punctuation (e.g. an ellipsis reaction) has zero
    *spoken* content by app.spoken_contract.content_char_count's own
    definition (punctuation is not counted), so it can never itself violate
    the capacity gate -- but it must still survive the projection verbatim,
    not be dropped just because it looks contentless."""
    from app.spoken_contract import content_char_count

    payload = _minimal_prep_pack([
        {"speaker": "甲", "line": "……", "segment_index": 1},
    ])
    screenplay = project_prep_pack_to_screenplay(payload)
    assert screenplay.key_lines == ["甲：……"]
    assert content_char_count("……") == 0


def test_unsplittable_long_single_clause_falls_back_to_character_chunks():
    """A single run-on clause with no internal punctuation to split on (the
    "超长且无法再切的单句" case) must not be silently dropped, truncated, or
    left over-capacity. `_split_spoken_line`'s character-level fallback
    guarantees every resulting chunk is non-empty and <= max_chars; this
    test pins that guarantee at the projection's own boundary, not just
    inside app.screenplay_ir's unit tests."""
    from app import config
    from app.spoken_contract import content_char_count

    long_line = "甲" * 80  # no punctuation anywhere; nothing to split on
    payload = _minimal_prep_pack([
        {"speaker": "某", "line": long_line, "segment_index": 1},
    ])
    screenplay = project_prep_pack_to_screenplay(payload)
    assert screenplay.key_lines, "must not silently drop an unsplittable long line"

    parts = []
    for key_line in screenplay.key_lines:
        _speaker, _sep, text = key_line.partition("：")
        assert text, "must never emit an empty unit"
        assert content_char_count(text) <= config.MAX_SPOKEN_CHARS_PER_SHOT
        parts.append(text)
    assert "".join(parts) == long_line


# ---------------------------------------------------------------------------
# Root cause 2 of the EP6 run_9bfcd5cbe128 regression (2026-08-25): splitting
# on any punctuation boundary (commas included) with equal priority produces
# grammatically incomplete units that trail off on a comma. See
# _split_prep_pack_spoken_line's own docstring for the full analysis.
# ---------------------------------------------------------------------------


def test_split_never_lets_a_unit_span_across_a_sentence_boundary():
    """句子优先：只要引述由多句完整句子组成，任何一个切分单元都不应该横跨到
    下一句中途才断——旧算法（逗号/句号同权重贪心堆叠）会在"上一句尾部 + 下一句
    开头的某个逗号小句"恰好能塞进同一单元时这么做，产出一个既含完整句又带半句
    尾巴的单元（用同一条输入验证：旧算法确实会产出
    ['他站起来。他知道自己错了，', '必须道歉，否则来不及了。']，
    第一个单元把无关的完整句子和下一句的残句粘在一起）。"""
    from app.screenplay_ir import _split_spoken_line
    from app.spoken_contract import content_char_count

    line = "他站起来。他知道自己错了，必须道歉，否则来不及了。"
    old = _split_spoken_line(line, max_chars=11)
    new = _split_prep_pack_spoken_line(line, max_chars=11)

    assert "".join(new) == line, "拆分必须逐字可还原，不得增删改写"
    for part in new:
        assert content_char_count(part) <= 11

    # 旧算法确实把第一句的完整收尾和第二句的开头小句揉进了同一个单元；
    # 新算法必须避免这种跨句拼接。
    assert old == ["他站起来。他知道自己错了，", "必须道歉，否则来不及了。"], old
    assert new == ["他站起来。", "他知道自己错了，必须道歉，", "否则来不及了。"], new

    # 任何一个单元里，句末标点（。！？…）之后不应该还跟着别的非空内容——
    # 也就是说，一个单元最多只在结尾出现一次句末标点，不会先完整收尾一句
    # 又在同一单元里续上下一句的开头。
    import re
    sentence_end = re.compile(r"[。！？…]")
    for part in new:
        matches = list(sentence_end.finditer(part))
        for match in matches:
            assert match.end() == len(part), (
                f"单元在句末标点后仍有内容，说明跨句子拼接：{part!r}"
            )


def test_split_matches_old_splitter_when_no_sentence_boundary_exists():
    """诚实的能力边界：若整条引述本身就是单个长句、除了句尾只有逗号（真实
    EP6 案例：74 字/54 字的孟浩内心独白通篇只有最后一个句号），任何切分方案
    都必须在最后一个句号之前至少切一刀，那一刀左边的单元只能停在逗号上——这是
    原文标点决定的语法边界，句子优先切分对这种输入完全无能为力，因此结果必须
    与旧算法（`app.screenplay_ir._split_spoken_line`）逐字相同，不应假装能
    改善、也不应产生不同的（尤其是更差的）切分。"""
    from app import config
    from app.screenplay_ir import _split_spoken_line

    # 真实 EP6 台词：run_9bfcd5cbe128 被判"未安排"的 3 条关键台词中的前两条，
    # 正是这两句 74 字 / 54 字独白切分出的残句。
    run_on_sentences = [
        "仅仅几个时辰的打坐，就相当于平日里约莫一个月的修行，虽说这是因石室内灵气"
        "天长日久的积累，此后便不会如此，可对我来说，在这里修行的速度要快出外界不少。",
        "此地灵气之所以可以积累而不外散，想来必定是因这些印记的原因，许师姐应是用"
        "这个方法来积累灵气，方便一次性吐纳。",
    ]
    for line in run_on_sentences:
        old = _split_spoken_line(line, max_chars=config.MAX_SPOKEN_CHARS_PER_SHOT)
        new = _split_prep_pack_spoken_line(line, max_chars=config.MAX_SPOKEN_CHARS_PER_SHOT)
        assert new == old, (
            f"单句超容量、无更早句末标点时不应偏离旧算法：{new!r} vs {old!r}"
        )
        # 非最后一个单元必然停在逗号上——这是原文标点结构决定的，不是缺陷。
        for part in new[:-1]:
            assert part.endswith("，"), (
                f"结构性验证：单句独白拆分出的非末尾单元预期以逗号收尾，实际 {part!r}"
            )
        assert new[-1].endswith("。")


def test_split_matches_old_splitter_for_full_ep6_fixture():
    """把新旧切分器套在真实 EP6 fixture 的全部 key_lines 上做端到端一致性抽查：
    句子优先切分不应该在"没有句子边界可利用"的输入上产生和旧算法不同（尤其是
    更碎或超容量）的结果。"""
    from app import config
    from app.screenplay_ir import _split_spoken_line

    payload = _ep6_payload()
    for event in payload["event_chain"]:
        for item in event.get("key_lines") or []:
            line = str(item.get("line") or "")
            old = _split_spoken_line(line, max_chars=config.MAX_SPOKEN_CHARS_PER_SHOT)
            new = _split_prep_pack_spoken_line(
                line, max_chars=config.MAX_SPOKEN_CHARS_PER_SHOT
            )
            assert "".join(new) == line.strip() or "".join(new) == line
            assert new == old, f"{line!r}: {new!r} vs {old!r}"


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
