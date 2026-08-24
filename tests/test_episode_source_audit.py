"""Red-first fixtures for scripts/episode_source_audit.py.

The audit tool is an independent, DB-only cross-check of episode_prep_pack
against the novel's own chapter text -- it does not trust the generator's own
internal gates (which may be older than the currently published Artifact; see
the script's module docstring for the real round-16 EP5/EP2 incidents this
guards against). These tests build tiny throwaway sqlite databases (a subset
of the real manju.db schema: only the columns the script actually reads) and
exercise the two directions the task brief requires:

  a) 名册含原文不存在的名字 -> 方向 A（幻觉检查）报出
     test_hallucinated_character_binding_reported_as_a_class
  b) 谱内名字在原文出现但名册缺失 -> 方向 B（遗漏检查）报出
     test_registered_character_missing_from_manifest_reported_as_b_class

Plus a scene-side mirror of (a), the exit-code contract, and two tests for
the 1.5.0 true-name-hint exception (positive: independently corroborated
elsewhere in the project -> passes; negative: an "accepted" hint with no
textual corroboration anywhere in the project still fails -- the tool must
not blindly trust the generator's own "accepted" label).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import episode_source_audit as audit  # noqa: E402


_SCHEMA = """
CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT,
    bible_json TEXT
);
CREATE TABLE chapters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    title TEXT,
    content TEXT NOT NULL
);
CREATE TABLE character_portraits (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    character_name TEXT NOT NULL,
    ep_start INTEGER NOT NULL,
    ep_end INTEGER
);
CREATE TABLE scene_references (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    scene_name TEXT NOT NULL,
    ep_start INTEGER NOT NULL,
    ep_end INTEGER
);
CREATE TABLE episodes (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    episode_no INTEGER NOT NULL,
    source_chapters TEXT,
    screenplay_status TEXT,
    published_screenplay_artifact_id TEXT
);
CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    content_json TEXT
);
CREATE TABLE evaluations (
    id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    evidence_json TEXT,
    created_at REAL
);
"""


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "audit_fixture.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return db_path


def _writer(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _insert_project(conn: sqlite3.Connection, project_id: str, bible_json: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO projects (id, name, bible_json) VALUES (?, ?, ?)",
        (project_id, project_id, json.dumps(bible_json, ensure_ascii=False) if bible_json else None),
    )


def _insert_chapter(conn: sqlite3.Connection, project_id: str, idx: int, title: str, content: str) -> None:
    conn.execute(
        "INSERT INTO chapters (project_id, idx, title, content) VALUES (?, ?, ?, ?)",
        (project_id, idx, title, content),
    )


def _insert_character(
    conn: sqlite3.Connection, portrait_id: str, project_id: str, name: str, ep_start: int = 1,
) -> None:
    conn.execute(
        "INSERT INTO character_portraits (id, project_id, character_name, ep_start, ep_end) "
        "VALUES (?, ?, ?, ?, NULL)",
        (portrait_id, project_id, name, ep_start),
    )


def _insert_episode(
    conn: sqlite3.Connection, episode_id: str, project_id: str, episode_no: int,
    chapter_indexes: list[int], artifact_id: str,
) -> None:
    conn.execute(
        "INSERT INTO episodes (id, project_id, episode_no, source_chapters, screenplay_status, "
        "published_screenplay_artifact_id) VALUES (?, ?, ?, ?, 'ready', ?)",
        (episode_id, project_id, episode_no, json.dumps(chapter_indexes), artifact_id),
    )


def _insert_pack_artifact(
    conn: sqlite3.Connection, artifact_id: str, episode_id: str, payload: dict,
) -> None:
    conn.execute(
        "INSERT INTO artifacts (id, type, scope_type, scope_id, version, status, content_json) "
        "VALUES (?, 'episode_prep_pack', 'episode', ?, 1, 'approved', ?)",
        (artifact_id, episode_id, json.dumps(payload, ensure_ascii=False)),
    )


def _insert_evaluation(conn: sqlite3.Connection, artifact_id: str, evidence: dict) -> None:
    conn.execute(
        "INSERT INTO evaluations (id, artifact_id, evidence_json, created_at) VALUES (?, ?, ?, 0)",
        (f"eval_{artifact_id}", artifact_id, json.dumps(evidence, ensure_ascii=False)),
    )


def _base_pack(*, chapter_indexes: list[int], characters: list[dict], scenes: list[dict],
                event_chain: list[dict] | None = None) -> dict:
    return {
        "prep_pack_version": "1.4.2",
        "episode_no": 1,
        "episode_scope": {"chapter_indexes": chapter_indexes, "source_segment_count": 1},
        "event_chain": event_chain or [],
        "asset_manifest": {"characters": characters, "scenes": scenes, "functional_extras": []},
        "coverage_ledger": {},
        "hook": "hook", "cliffhanger": "cliffhanger",
    }


# ---------------------------------------------------------------------------
# a) 名册含原文不存在的名字 -> 方向 A 报出
# ---------------------------------------------------------------------------

def test_hallucinated_character_binding_reported_as_a_class(tmp_path):
    db_path = _make_db(tmp_path)
    conn = _writer(db_path)
    _insert_project(conn, "proj_a")
    _insert_chapter(
        conn, "proj_a", 1, "第一章",
        "许清说道：“今日比试，我必胜！”围观弟子纷纷喝彩。",
    )
    _insert_character(conn, "portrait_xu", "proj_a", "许清")
    # "丹鬼" is registered somewhere in this project's roster but this
    # chapter's text never once says it -- mirrors the real round-16 EP5
    # incident (靠山宗旁山峰的灰袍老者 bound to the pre-existing "丹鬼").
    _insert_character(conn, "portrait_gui", "proj_a", "丹鬼")
    pack = _base_pack(
        chapter_indexes=[1],
        characters=[
            {"identity_id": "bible:许清", "display_name": "许清", "portrait_id": "portrait_xu",
             "event_ids": ["ev_001"], "aliases": []},
            {"identity_id": "bible:丹鬼", "display_name": "丹鬼", "portrait_id": "portrait_gui",
             "event_ids": ["ev_001"], "aliases": []},
        ],
        scenes=[],
        event_chain=[{
            "event_id": "ev_001", "order": 1, "summary": "许清参加比试",
            "source_span": {"from_segment": 1, "to_segment": 1},
            "source_evidence": [{"segment_index": 1, "quote": "许清说道：“今日比试，我必胜！”"}],
            "key_lines": [{"speaker": "许清", "line": "今日比试，我必胜！", "segment_index": 1}],
        }],
    )
    _insert_episode(conn, "ep_a1", "proj_a", 1, [1], "art_a1")
    _insert_pack_artifact(conn, "art_a1", "ep_a1", pack)
    conn.commit()
    conn.close()

    ro = audit.readonly_connection(db_path)
    result = audit.audit_episode(ro, "proj_a", 1)
    ro.close()

    assert result.skipped_reason is None
    a_codes = [issue.code for issue in result.a_issues]
    assert a_codes == ["A1_character_no_text_evidence"]
    assert "丹鬼" in result.a_issues[0].message
    assert result.a_issues[0].detail["portrait_id"] == "portrait_gui"
    # 许清 and 丹鬼 are both already in the manifest -> no B-class noise here.
    assert result.b_issues == []
    assert audit.exit_code([result]) == 1


# ---------------------------------------------------------------------------
# b) 谱内名字在原文出现但名册缺失 -> 方向 B 报出
# ---------------------------------------------------------------------------

def test_registered_character_missing_from_manifest_reported_as_b_class(tmp_path):
    db_path = _make_db(tmp_path)
    conn = _writer(db_path)
    _insert_project(conn, "proj_b")
    _insert_chapter(
        conn, "proj_b", 1, "第一章",
        "许清站在广场上，看着人群。\n\n王有材：“等等我！”他快步追了上去。",
    )
    _insert_character(conn, "portrait_xu", "proj_b", "许清")
    # 王有材 is a registered roster character who genuinely appears (with a
    # dialogue tag) in this chapter, but the pack below never binds him.
    _insert_character(conn, "portrait_wang", "proj_b", "王有材")
    pack = _base_pack(
        chapter_indexes=[1],
        characters=[
            {"identity_id": "bible:许清", "display_name": "许清", "portrait_id": "portrait_xu",
             "event_ids": ["ev_001"], "aliases": []},
        ],
        scenes=[],
        event_chain=[{
            "event_id": "ev_001", "order": 1, "summary": "许清站在广场上",
            "source_span": {"from_segment": 1, "to_segment": 1},
            "source_evidence": [{"segment_index": 1, "quote": "许清站在广场上，看着人群。"}],
            "key_lines": [],
        }],
    )
    _insert_episode(conn, "ep_b1", "proj_b", 1, [1], "art_b1")
    _insert_pack_artifact(conn, "art_b1", "ep_b1", pack)
    conn.commit()
    conn.close()

    ro = audit.readonly_connection(db_path)
    result = audit.audit_episode(ro, "proj_b", 1)
    ro.close()

    assert result.a_issues == []  # 许清 binding is properly text-grounded
    assert len(result.b_issues) == 1
    issue = result.b_issues[0]
    assert issue.code == "B1_character_missing"
    assert issue.detail["character_name"] == "王有材"
    assert issue.detail["occurrences"] == 1
    assert issue.detail["dialogue_signal"] is True
    assert audit.exit_code([result]) == 2


# ---------------------------------------------------------------------------
# 场景侧的方向 A 镜像用例（同一机制，验证不是角色专属逻辑）
# ---------------------------------------------------------------------------

def test_hallucinated_scene_binding_reported_as_a_class(tmp_path):
    db_path = _make_db(tmp_path)
    conn = _writer(db_path)
    _insert_project(conn, "proj_c")
    _insert_chapter(
        conn, "proj_c", 1, "第一章",
        "孟浩走到靠山宗南峰，在此峰的山脚下，找到了许师姐所说的洞府。",
    )
    conn.execute(
        "INSERT INTO scene_references (id, project_id, scene_name, ep_start, ep_end) "
        "VALUES ('scene_nf', 'proj_c', '南峰山脚洞府', 1, NULL)",
    )
    pack = _base_pack(
        chapter_indexes=[1],
        characters=[],
        scenes=[{
            "scene_id": "scene:南峰山脚洞府", "display_name": "南峰山脚洞府",
            "scene_reference_id": "scene_nf", "event_ids": ["ev_001"],
        }],
        event_chain=[{
            "event_id": "ev_001", "order": 1, "summary": "孟浩找到洞府",
            "source_span": {"from_segment": 1, "to_segment": 1},
            "source_evidence": [{"segment_index": 1, "quote": "孟浩走到靠山宗南峰"}],
            "key_lines": [],
        }],
    )
    _insert_episode(conn, "ep_c1", "proj_c", 1, [1], "art_c1")
    _insert_pack_artifact(conn, "art_c1", "ep_c1", pack)
    conn.commit()
    conn.close()

    ro = audit.readonly_connection(db_path)
    result = audit.audit_episode(ro, "proj_c", 1)
    ro.close()

    a_codes = [issue.code for issue in result.a_issues]
    assert a_codes == ["A2_scene_no_text_evidence"]
    assert "南峰山脚洞府" in result.a_issues[0].message


# ---------------------------------------------------------------------------
# 1.5.0 真名核验证据（true_name_hints）例外：不是盲信 accepted 标记
# ---------------------------------------------------------------------------

def test_true_name_hint_verified_elsewhere_in_project_passes(tmp_path):
    db_path = _make_db(tmp_path)
    conn = _writer(db_path)
    _insert_project(conn, "proj_d")
    # Episode 2's own chapter only ever calls this person "白发老者"; the
    # model's suspected_true_name guess ("李长老") only shows up in a LATER
    # chapter (5) of the same project -- exactly the forward-window shape
    # app.production.prep_pack._prep_pack_verify_true_name_hypothesis checks.
    _insert_chapter(conn, "proj_d", 2, "第二章", "一位白发老者缓缓走来，负手而立。")
    _insert_chapter(conn, "proj_d", 5, "第五章", "李长老对着弟子们训话。")
    _insert_character(conn, "portrait_li", "proj_d", "李长老")
    pack = _base_pack(
        chapter_indexes=[2],
        characters=[
            {"identity_id": "bible:李长老", "display_name": "李长老", "portrait_id": "portrait_li",
             "event_ids": ["ev_001"], "aliases": []},
        ],
        scenes=[],
        event_chain=[{
            "event_id": "ev_001", "order": 1, "summary": "老者现身",
            "source_span": {"from_segment": 1, "to_segment": 1},
            "source_evidence": [{"segment_index": 1, "quote": "一位白发老者缓缓走来"}],
            "key_lines": [],
        }],
    )
    pack["episode_no"] = 2
    _insert_episode(conn, "ep_d2", "proj_d", 2, [2], "art_d2")
    _insert_pack_artifact(conn, "art_d2", "ep_d2", pack)
    _insert_evaluation(conn, "art_d2", {
        "true_name_hints": [{
            "kind": "character", "mention": "白发老者",
            "suspected_true_name": "李长老", "status": "accepted",
        }],
    })
    conn.commit()
    conn.close()

    ro = audit.readonly_connection(db_path)
    result = audit.audit_episode(ro, "proj_d", 2)
    ro.close()

    assert result.a_issues == []
    assert result.tallies["A1 角色绑定文本依据"].passed == 1


def test_true_name_hint_without_corroboration_still_fails(tmp_path):
    """Same shape as the positive case, but the suspected_true_name never
    appears in *any* chapter of the project -- the tool must not trust the
    generator's own "accepted" label as a bare assertion; it re-verifies."""
    db_path = _make_db(tmp_path)
    conn = _writer(db_path)
    _insert_project(conn, "proj_e")
    _insert_chapter(conn, "proj_e", 2, "第二章", "一位白发老者缓缓走来，负手而立。")
    _insert_character(conn, "portrait_li", "proj_e", "李长老")
    pack = _base_pack(
        chapter_indexes=[2],
        characters=[
            {"identity_id": "bible:李长老", "display_name": "李长老", "portrait_id": "portrait_li",
             "event_ids": ["ev_001"], "aliases": []},
        ],
        scenes=[],
        event_chain=[{
            "event_id": "ev_001", "order": 1, "summary": "老者现身",
            "source_span": {"from_segment": 1, "to_segment": 1},
            "source_evidence": [{"segment_index": 1, "quote": "一位白发老者缓缓走来"}],
            "key_lines": [],
        }],
    )
    pack["episode_no"] = 2
    _insert_episode(conn, "ep_e2", "proj_e", 2, [2], "art_e2")
    _insert_pack_artifact(conn, "art_e2", "ep_e2", pack)
    _insert_evaluation(conn, "art_e2", {
        "true_name_hints": [{
            "kind": "character", "mention": "白发老者",
            "suspected_true_name": "李长老", "status": "accepted",
        }],
    })
    conn.commit()
    conn.close()

    ro = audit.readonly_connection(db_path)
    result = audit.audit_episode(ro, "proj_e", 2)
    ro.close()

    a_codes = [issue.code for issue in result.a_issues]
    assert a_codes == ["A1_character_no_text_evidence"]


# ---------------------------------------------------------------------------
# exit code 契约
# ---------------------------------------------------------------------------

def test_exit_code_all_clean_returns_zero():
    results = [audit.EpisodeAuditResult(episode_no=1), audit.EpisodeAuditResult(episode_no=2)]
    assert audit.exit_code(results) == 0


def test_exit_code_a_class_dominates_returns_one():
    clean = audit.EpisodeAuditResult(episode_no=1)
    with_a = audit.EpisodeAuditResult(episode_no=2)
    with_a.a_issues.append(audit.Issue(code="A1_character_no_text_evidence", message="x"))
    with_b = audit.EpisodeAuditResult(episode_no=3)
    with_b.b_issues.append(audit.Issue(code="B1_character_missing", message="y"))
    assert audit.exit_code([clean, with_a, with_b]) == 1


def test_exit_code_b_class_only_returns_two():
    with_b = audit.EpisodeAuditResult(episode_no=1)
    with_b.b_issues.append(audit.Issue(code="B1_character_missing", message="y"))
    assert audit.exit_code([with_b]) == 2


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
