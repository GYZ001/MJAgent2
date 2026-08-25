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
    # Neither entry carries a 1.6.0 provenance field -> both took the legacy
    # fallback path; the tally must say so via legacy_fallback, not silently.
    assert result.tallies["A1 角色绑定文本依据"].legacy_fallback == 2


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
# round-18 口径修正：A3 说话人判定要接纳"正名 speaker + 原文只有别名"
# ---------------------------------------------------------------------------

def test_key_line_speaker_passes_via_character_alias_when_canonical_name_absent_from_text(tmp_path):
    """speaker 写的是正名"李富贵"，本集原文只喊过他的别名"小胖子"——manifest
    该角色条目登记了 aliases=["小胖子"]（本就是 1.4.2 证据闸的产物，保证在
    原文里逐字出现过），A3 应该通过，不能因为 speaker 字符串本身没出现就报错。
    """
    db_path = _make_db(tmp_path)
    conn = _writer(db_path)
    _insert_project(conn, "proj_f")
    _insert_chapter(
        conn, "proj_f", 1, "第一章",
        "小胖子挤在人群里，探头探脑地看着比试。",
    )
    _insert_character(conn, "portrait_li", "proj_f", "李富贵")
    pack = _base_pack(
        chapter_indexes=[1],
        characters=[
            {"identity_id": "bible:李富贵", "display_name": "李富贵", "portrait_id": "portrait_li",
             "event_ids": ["ev_001"], "aliases": ["小胖子"]},
        ],
        scenes=[],
        event_chain=[{
            "event_id": "ev_001", "order": 1, "summary": "小胖子看比试",
            "source_span": {"from_segment": 1, "to_segment": 1},
            "source_evidence": [{"segment_index": 1, "quote": "小胖子挤在人群里"}],
            "key_lines": [{
                "speaker": "李富贵", "line": "小胖子挤在人群里，探头探脑地看着比试。",
                "segment_index": 1, "speaker_ref": "bible:李富贵",
            }],
        }],
    )
    _insert_episode(conn, "ep_f1", "proj_f", 1, [1], "art_f1")
    _insert_pack_artifact(conn, "art_f1", "ep_f1", pack)
    conn.commit()
    conn.close()

    ro = audit.readonly_connection(db_path)
    result = audit.audit_episode(ro, "proj_f", 1)
    ro.close()

    a3_issues = [i for i in result.a_issues if i.code == "A3_speaker_no_text_evidence"]
    assert a3_issues == []
    assert result.tallies["A3 台词说话人文本依据"].passed == 1


def test_key_line_speaker_still_fails_when_neither_speaker_nor_alias_in_text(tmp_path):
    """同样是正名 speaker + manifest 登记了别名，但这次原文里两个称谓都没
    出现过（既不是"李富贵"也不是"小胖子"）——仍然必须报 A3，别名回退不能
    变成"只要角色在名册里就无条件放行"。
    """
    db_path = _make_db(tmp_path)
    conn = _writer(db_path)
    _insert_project(conn, "proj_g")
    _insert_chapter(
        conn, "proj_g", 1, "第一章",
        "远处传来一阵喧哗，众人纷纷望去，却什么也没看清。",
    )
    _insert_character(conn, "portrait_li", "proj_g", "李富贵")
    pack = _base_pack(
        chapter_indexes=[1],
        characters=[
            {"identity_id": "bible:李富贵", "display_name": "李富贵", "portrait_id": "portrait_li",
             "event_ids": ["ev_001"], "aliases": ["小胖子"]},
        ],
        scenes=[],
        event_chain=[{
            "event_id": "ev_001", "order": 1, "summary": "众人围观喧哗",
            "source_span": {"from_segment": 1, "to_segment": 1},
            "source_evidence": [{"segment_index": 1, "quote": "远处传来一阵喧哗"}],
            "key_lines": [{
                "speaker": "李富贵", "line": "众人纷纷望去", "segment_index": 1,
                "speaker_ref": "bible:李富贵",
            }],
        }],
    )
    _insert_episode(conn, "ep_g1", "proj_g", 1, [1], "art_g1")
    _insert_pack_artifact(conn, "art_g1", "ep_g1", pack)
    conn.commit()
    conn.close()

    ro = audit.readonly_connection(db_path)
    result = audit.audit_episode(ro, "proj_g", 1)
    ro.close()

    a3_issues = [i for i in result.a_issues if i.code == "A3_speaker_no_text_evidence"]
    assert len(a3_issues) == 1
    assert a3_issues[0].detail["speaker"] == "李富贵"
    assert a3_issues[0].detail["checked_aliases"] == ["小胖子"]


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
# 1.6.0 provenance 升级（协调方指令，管线尚未实际发布）：manifest 绑定/
# key_line 说话人携带 provenance={method, anchor_segments, anchor_phrase}，
# method 枚举 direct/alias/resolution/discovery/absorbed_speaker。
#
# 假定形状说明（指令原话："1.6.0 形状以后端 agent 实际发布为准，字段名有出
# 入以实物为准并注明"）：
#   - manifest.characters[]/scenes[] 每项的键名假定为 "provenance"；
#   - key_lines[] 每条的键名假定为 "speaker_provenance"（script 里
#     check_key_line_speakers 对它有一个 "provenance" 兜底读取，万一实物用
#     的是跟角色/场景一样的裸 "provenance" 这个名字也能兼容，但本文件按
#     "speaker_provenance" 这个更明确的假设写夹具）。
#   - 若实物字段名/形状不同，需要改的只有 scripts/episode_source_audit.py
#     里 _dispatch_provenance 的调用点（三处：check_manifest_characters /
#     check_manifest_scenes / check_key_line_speakers）取值那一行，以及本节
#     夹具的 dict 字面量，判定逻辑本身（_verify_provenance_anchor）不用动。
#
# 两个纯函数单测先验证锚点核验本身的两种失效子模式（段号越界 / 短语不命
# 中），再用 5 个 audit_episode 集成用例覆盖 5 个 method 各自的"通过 + 失
# 效"（direct/alias 用文本判据的通过/失败；resolution/discovery/
# absorbed_speaker 用锚点判据的通过/失败，失效子模式在 3 个方法间分摊覆盖
# 段号越界和短语不命中两种）。
# ---------------------------------------------------------------------------

def test_verify_provenance_anchor_passes_when_phrase_found_in_referenced_segment():
    segments = audit.index_source_segments("【第一章】\n孟浩推开柴门，看见院子里落满黄叶。")
    ok, reason = audit._verify_provenance_anchor(segments, {
        "anchor_segments": [1], "anchor_phrase": "推开柴门",
    })
    assert ok is True
    assert reason == ""


def test_verify_provenance_anchor_fails_on_out_of_range_segment():
    segments = audit.index_source_segments("【第一章】\n孟浩推开柴门。")
    ok, reason = audit._verify_provenance_anchor(segments, {
        "anchor_segments": [5], "anchor_phrase": "推开柴门",
    })
    assert ok is False
    assert "越界" in reason


def test_verify_provenance_anchor_fails_on_phrase_mismatch():
    segments = audit.index_source_segments("【第一章】\n孟浩推开柴门。")
    ok, reason = audit._verify_provenance_anchor(segments, {
        "anchor_segments": [1], "anchor_phrase": "这句话原文根本没有",
    })
    assert ok is False
    assert "命中" in reason


def test_provenance_method_direct(tmp_path):
    """direct：维持现行逐字标准。通过=display_name 在原文；失效=不在。"""
    db_path = _make_db(tmp_path)
    conn = _writer(db_path)
    _insert_project(conn, "proj_h")
    _insert_chapter(conn, "proj_h", 1, "第一章", "许清站在广场上，看着人群。")
    _insert_character(conn, "portrait_xu", "proj_h", "许清")
    _insert_character(conn, "portrait_gui", "proj_h", "丹鬼")
    pack = _base_pack(
        chapter_indexes=[1],
        characters=[
            {"identity_id": "bible:许清", "display_name": "许清", "portrait_id": "portrait_xu",
             "event_ids": ["ev_001"], "aliases": [],
             "provenance": {"method": "direct", "anchor_segments": [1], "anchor_phrase": "许清"}},
            {"identity_id": "bible:丹鬼", "display_name": "丹鬼", "portrait_id": "portrait_gui",
             "event_ids": ["ev_001"], "aliases": [],
             "provenance": {"method": "direct", "anchor_segments": [1], "anchor_phrase": "丹鬼"}},
        ],
        scenes=[],
        event_chain=[{
            "event_id": "ev_001", "order": 1, "summary": "许清站在广场上",
            "source_span": {"from_segment": 1, "to_segment": 1},
            "source_evidence": [{"segment_index": 1, "quote": "许清站在广场上，看着人群。"}],
            "key_lines": [],
        }],
    )
    _insert_episode(conn, "ep_h1", "proj_h", 1, [1], "art_h1")
    _insert_pack_artifact(conn, "art_h1", "ep_h1", pack)
    conn.commit()
    conn.close()

    ro = audit.readonly_connection(db_path)
    result = audit.audit_episode(ro, "proj_h", 1)
    ro.close()

    assert [i.code for i in result.a_issues] == ["A1_character_no_text_evidence"]
    assert "丹鬼" in result.a_issues[0].message
    assert "direct" in result.a_issues[0].message
    tally = result.tallies["A1 角色绑定文本依据"]
    assert (tally.checked, tally.passed, tally.legacy_fallback) == (2, 1, 0)


def test_provenance_method_alias(tmp_path):
    """alias：命中的别名逐字在原文即通过。通过=aliases 命中；失效=display_name
    与全部 aliases 均未命中。"""
    db_path = _make_db(tmp_path)
    conn = _writer(db_path)
    _insert_project(conn, "proj_i")
    _insert_chapter(conn, "proj_i", 1, "第一章", "小胖子挤在人群里，探头探脑地看着比试。")
    _insert_character(conn, "portrait_li", "proj_i", "李富贵")
    _insert_character(conn, "portrait_wang", "proj_i", "王有材")
    pack = _base_pack(
        chapter_indexes=[1],
        characters=[
            {"identity_id": "bible:李富贵", "display_name": "李富贵", "portrait_id": "portrait_li",
             "event_ids": ["ev_001"], "aliases": ["小胖子"],
             "provenance": {"method": "alias", "anchor_segments": [1], "anchor_phrase": "小胖子"}},
            {"identity_id": "bible:王有材", "display_name": "王有材", "portrait_id": "portrait_wang",
             "event_ids": ["ev_001"], "aliases": ["二狗子"],
             "provenance": {"method": "alias", "anchor_segments": [1], "anchor_phrase": "二狗子"}},
        ],
        scenes=[],
        event_chain=[{
            "event_id": "ev_001", "order": 1, "summary": "小胖子看比试",
            "source_span": {"from_segment": 1, "to_segment": 1},
            "source_evidence": [{"segment_index": 1, "quote": "小胖子挤在人群里"}],
            "key_lines": [],
        }],
    )
    _insert_episode(conn, "ep_i1", "proj_i", 1, [1], "art_i1")
    _insert_pack_artifact(conn, "art_i1", "ep_i1", pack)
    conn.commit()
    conn.close()

    ro = audit.readonly_connection(db_path)
    result = audit.audit_episode(ro, "proj_i", 1)
    ro.close()

    assert [i.code for i in result.a_issues] == ["A1_character_no_text_evidence"]
    assert "王有材" in result.a_issues[0].message
    assert "alias" in result.a_issues[0].message
    tally = result.tallies["A1 角色绑定文本依据"]
    assert (tally.checked, tally.passed, tally.legacy_fallback) == (2, 1, 0)


def test_provenance_method_resolution(tmp_path):
    """resolution：display_name 可以是合成规范名，不苛求逐字；改核验锚点链。
    通过=锚点段号有效且短语命中；失效（短语不命中）=A2_scene_anchor_invalid。
    """
    db_path = _make_db(tmp_path)
    conn = _writer(db_path)
    _insert_project(conn, "proj_j")
    _insert_chapter(
        conn, "proj_j", 1, "第一章",
        "孟浩走到靠山宗南峰，在此峰的山脚下，找到了许师姐所说的洞府。",
    )
    conn.execute(
        "INSERT INTO scene_references (id, project_id, scene_name, ep_start, ep_end) "
        "VALUES ('scene_nf', 'proj_j', '南峰山脚洞府', 1, NULL)",
    )
    conn.execute(
        "INSERT INTO scene_references (id, project_id, scene_name, ep_start, ep_end) "
        "VALUES ('scene_bf', 'proj_j', '黑风寨密室', 1, NULL)",
    )
    pack = _base_pack(
        chapter_indexes=[1],
        characters=[],
        scenes=[
            {
                "scene_id": "scene:南峰山脚洞府", "display_name": "南峰山脚洞府",
                "scene_reference_id": "scene_nf", "event_ids": ["ev_001"],
                "provenance": {
                    "method": "resolution", "anchor_segments": [1],
                    "anchor_phrase": "靠山宗南峰，在此峰的山脚下",
                },
            },
            {
                "scene_id": "scene:黑风寨密室", "display_name": "黑风寨密室",
                "scene_reference_id": "scene_bf", "event_ids": ["ev_001"],
                "provenance": {
                    "method": "resolution", "anchor_segments": [1],
                    "anchor_phrase": "这句话原文根本没有",
                },
            },
        ],
        event_chain=[{
            "event_id": "ev_001", "order": 1, "summary": "孟浩找到洞府",
            "source_span": {"from_segment": 1, "to_segment": 1},
            "source_evidence": [{"segment_index": 1, "quote": "孟浩走到靠山宗南峰"}],
            "key_lines": [],
        }],
    )
    _insert_episode(conn, "ep_j1", "proj_j", 1, [1], "art_j1")
    _insert_pack_artifact(conn, "art_j1", "ep_j1", pack)
    conn.commit()
    conn.close()

    ro = audit.readonly_connection(db_path)
    result = audit.audit_episode(ro, "proj_j", 1)
    ro.close()

    assert [i.code for i in result.a_issues] == ["A2_scene_anchor_invalid"]
    assert "黑风寨密室" in result.a_issues[0].message
    assert "命中" in result.a_issues[0].detail["reason"]
    tally = result.tallies["A2 场景绑定文本依据"]
    # "南峰山脚洞府" passed purely via the anchor -- its display_name never
    # once appears verbatim in the chapter text (confirmed by the pre-1.6.0
    # A2 fixture above, which flags this exact string as a hallucination).
    assert (tally.checked, tally.passed, tally.legacy_fallback) == (2, 1, 0)


def test_provenance_method_discovery(tmp_path):
    """discovery：同 resolution 走锚点核验。失效子模式换成段号越界。"""
    db_path = _make_db(tmp_path)
    conn = _writer(db_path)
    _insert_project(conn, "proj_k")
    _insert_chapter(conn, "proj_k", 1, "第一章", "一位灰袍老者忽然现身，众人骇然。")
    _insert_character(conn, "portrait_grey", "proj_k", "灰衣异人")
    _insert_character(conn, "portrait_black", "proj_k", "黑衣刺客")
    pack = _base_pack(
        chapter_indexes=[1],
        characters=[
            {"identity_id": "bible:灰衣异人", "display_name": "灰衣异人", "portrait_id": "portrait_grey",
             "event_ids": ["ev_001"], "aliases": [],
             "provenance": {
                 "method": "discovery", "anchor_segments": [1],
                 "anchor_phrase": "一位灰袍老者忽然现身",
             }},
            {"identity_id": "bible:黑衣刺客", "display_name": "黑衣刺客", "portrait_id": "portrait_black",
             "event_ids": ["ev_001"], "aliases": [],
             "provenance": {
                 # 本集只切出 1 段，7 越界。
                 "method": "discovery", "anchor_segments": [7],
                 "anchor_phrase": "一位灰袍老者忽然现身",
             }},
        ],
        scenes=[],
        event_chain=[{
            "event_id": "ev_001", "order": 1, "summary": "灰袍老者现身",
            "source_span": {"from_segment": 1, "to_segment": 1},
            "source_evidence": [{"segment_index": 1, "quote": "一位灰袍老者忽然现身"}],
            "key_lines": [],
        }],
    )
    _insert_episode(conn, "ep_k1", "proj_k", 1, [1], "art_k1")
    _insert_pack_artifact(conn, "art_k1", "ep_k1", pack)
    conn.commit()
    conn.close()

    ro = audit.readonly_connection(db_path)
    result = audit.audit_episode(ro, "proj_k", 1)
    ro.close()

    assert [i.code for i in result.a_issues] == ["A1_character_anchor_invalid"]
    assert "黑衣刺客" in result.a_issues[0].message
    assert "越界" in result.a_issues[0].detail["reason"]
    tally = result.tallies["A1 角色绑定文本依据"]
    assert (tally.checked, tally.passed, tally.legacy_fallback) == (2, 1, 0)


def test_provenance_method_absorbed_speaker(tmp_path):
    """absorbed_speaker：key_line 的 speaker 可以是群演标签，改核验锚点链。
    假定字段名 speaker_provenance（见本节顶部说明）。通过=锚点命中；失效
    （短语不命中）=A3_speaker_anchor_invalid。"""
    db_path = _make_db(tmp_path)
    conn = _writer(db_path)
    _insert_project(conn, "proj_l")
    _insert_chapter(
        conn, "proj_l", 1, "第一章",
        "围观弟子们交头接耳，议论纷纷。人群中有人低声说道：靠山宗要出大事了。",
    )
    pack = _base_pack(
        chapter_indexes=[1],
        characters=[],
        scenes=[],
        event_chain=[{
            "event_id": "ev_001", "order": 1, "summary": "围观弟子议论",
            "source_span": {"from_segment": 1, "to_segment": 1},
            "source_evidence": [{"segment_index": 1, "quote": "围观弟子们交头接耳"}],
            "key_lines": [
                {
                    "speaker": "围观弟子", "line": "靠山宗要出大事了", "segment_index": 1,
                    "speaker_provenance": {
                        "method": "absorbed_speaker", "anchor_segments": [1],
                        "anchor_phrase": "人群中有人低声说道",
                    },
                },
                {
                    "speaker": "围观弟子", "line": "靠山宗要出大事了", "segment_index": 1,
                    "speaker_provenance": {
                        "method": "absorbed_speaker", "anchor_segments": [1],
                        "anchor_phrase": "这句台词原文里根本没有",
                    },
                },
            ],
        }],
    )
    pack["asset_manifest"]["functional_extras"] = [{"label": "围观弟子", "event_ids": ["ev_001"]}]
    _insert_episode(conn, "ep_l1", "proj_l", 1, [1], "art_l1")
    _insert_pack_artifact(conn, "art_l1", "ep_l1", pack)
    conn.commit()
    conn.close()

    ro = audit.readonly_connection(db_path)
    result = audit.audit_episode(ro, "proj_l", 1)
    ro.close()

    a3_issues = [i for i in result.a_issues if i.code == "A3_speaker_anchor_invalid"]
    assert len(a3_issues) == 1
    assert "命中" in a3_issues[0].detail["reason"]
    tally = result.tallies["A3 台词说话人文本依据"]
    assert (tally.checked, tally.passed, tally.legacy_fallback) == (2, 1, 0)


def test_provenance_missing_falls_back_to_legacy_standard_with_annotation(tmp_path):
    """provenance 整体缺失（<=1.5.x 旧包）：回退现行标准，且 legacy_fallback
    计数必须如实反映，供输出标注"无来源证明（旧版产物）"。"""
    db_path = _make_db(tmp_path)
    conn = _writer(db_path)
    _insert_project(conn, "proj_m")
    _insert_chapter(conn, "proj_m", 1, "第一章", "许清站在广场上，看着人群。")
    _insert_character(conn, "portrait_xu", "proj_m", "许清")
    pack = _base_pack(
        chapter_indexes=[1],
        characters=[
            {"identity_id": "bible:许清", "display_name": "许清", "portrait_id": "portrait_xu",
             "event_ids": ["ev_001"], "aliases": []},  # no "provenance" key at all
        ],
        scenes=[],
        event_chain=[{
            "event_id": "ev_001", "order": 1, "summary": "许清站在广场上",
            "source_span": {"from_segment": 1, "to_segment": 1},
            "source_evidence": [{"segment_index": 1, "quote": "许清站在广场上，看着人群。"}],
            "key_lines": [],
        }],
    )
    pack["prep_pack_version"] = "1.5.0"
    _insert_episode(conn, "ep_m1", "proj_m", 1, [1], "art_m1")
    _insert_pack_artifact(conn, "art_m1", "ep_m1", pack)
    conn.commit()
    conn.close()

    ro = audit.readonly_connection(db_path)
    result = audit.audit_episode(ro, "proj_m", 1)
    ro.close()

    assert result.a_issues == []
    tally = result.tallies["A1 角色绑定文本依据"]
    assert (tally.checked, tally.passed, tally.legacy_fallback) == (1, 1, 1)
    assert "无来源证明" in audit.format_episode(result)


# ---------------------------------------------------------------------------
# 收尾单：两个此前 fail-closed 的未识别 method 现在有正式判据。
# 假定字段名（协调方原话"字段名有出入以实物为准并注明"）：
#   - resolution_forward: provenance.forward_chapter_label（如"第 5 章"）+
#     沿用的 provenance.anchor_phrase；不使用 anchor_segments（前瞻章节不在
#     本集自己的 segment 编号体系内）。
#   - alias_inherited: provenance.source_episode_no（int，来源集号）。
# ---------------------------------------------------------------------------

def test_provenance_method_resolution_forward(tmp_path):
    """resolution_forward：核验 forward_chapter_label 定位到的整章原文里是否
    有 anchor_phrase。通过=章节存在且短语命中；失效=标注定位不到有效章节。"""
    db_path = _make_db(tmp_path)
    conn = _writer(db_path)
    _insert_project(conn, "proj_n")
    _insert_chapter(conn, "proj_n", 2, "第二章", "一位灰袍老者忽然现身，众人骇然。")
    _insert_chapter(conn, "proj_n", 5, "第五章", "李长老对着弟子们训话，声若洪钟。")
    _insert_character(conn, "portrait_li", "proj_n", "李长老")
    _insert_character(conn, "portrait_wang", "proj_n", "王统领")
    pack = _base_pack(
        chapter_indexes=[2],
        characters=[
            {"identity_id": "bible:李长老", "display_name": "李长老", "portrait_id": "portrait_li",
             "event_ids": ["ev_001"], "aliases": [],
             "provenance": {
                 "method": "resolution_forward", "forward_chapter_label": "第 5 章",
                 "anchor_phrase": "李长老对着弟子们训话",
             }},
            {"identity_id": "bible:王统领", "display_name": "王统领", "portrait_id": "portrait_wang",
             "event_ids": ["ev_001"], "aliases": [],
             "provenance": {
                 # 第 999 章在本项目里根本不存在。
                 "method": "resolution_forward", "forward_chapter_label": "第 999 章",
                 "anchor_phrase": "随便什么短语",
             }},
        ],
        scenes=[],
        event_chain=[{
            "event_id": "ev_001", "order": 1, "summary": "灰袍老者现身",
            "source_span": {"from_segment": 1, "to_segment": 1},
            "source_evidence": [{"segment_index": 1, "quote": "一位灰袍老者忽然现身"}],
            "key_lines": [],
        }],
    )
    pack["episode_no"] = 2
    _insert_episode(conn, "ep_n2", "proj_n", 2, [2], "art_n2")
    _insert_pack_artifact(conn, "art_n2", "ep_n2", pack)
    conn.commit()
    conn.close()

    ro = audit.readonly_connection(db_path)
    result = audit.audit_episode(ro, "proj_n", 2)
    ro.close()

    assert [i.code for i in result.a_issues] == ["A1_character_anchor_invalid"]
    assert "王统领" in result.a_issues[0].message
    assert "未能定位到有效章节" in result.a_issues[0].detail["reason"]
    tally = result.tallies["A1 角色绑定文本依据"]
    # "李长老" passed purely off chapter 5's text -- chapter 2 (this episode's
    # own scope) never mentions him at all, proving the forward-chapter path
    # is doing real work, not just falling through to the legacy check.
    assert (tally.checked, tally.passed, tally.legacy_fallback) == (2, 1, 0)


def test_provenance_method_alias_inherited(tmp_path):
    """alias_inherited：来源集已发布 pack 中该资产（同 portrait_id）需有同名
    绑定，且那条绑定自身核验通过才放行。通过=来源集里真有且自身核验通过
    （旧版标准文本命中）；失效=来源集 pack 里根本没有这个 portrait_id 的绑
    定（来源断链）。"""
    db_path = _make_db(tmp_path)
    conn = _writer(db_path)
    _insert_project(conn, "proj_o")
    # 集 1（来源集）：许清本名就在原文里出现过，走旧版标准（无 provenance）
    # 就能核验通过，供集 3 继承。
    _insert_chapter(conn, "proj_o", 1, "第一章", "许清站在广场上，看着人群。")
    # 集 3（当前集）：这个称呼本集自己完全没提，靠继承集 1 的同名绑定放行。
    _insert_chapter(conn, "proj_o", 3, "第三章", "众人议论纷纷，气氛紧张。")
    _insert_character(conn, "portrait_xu", "proj_o", "许清")
    _insert_character(conn, "portrait_wang", "proj_o", "王有材")

    pack_ep1 = _base_pack(
        chapter_indexes=[1],
        characters=[
            {"identity_id": "bible:许清", "display_name": "许清", "portrait_id": "portrait_xu",
             "event_ids": ["ev_001"], "aliases": []},  # 无 provenance -> 旧版标准，靠原文命中通过
        ],
        scenes=[],
        event_chain=[{
            "event_id": "ev_001", "order": 1, "summary": "许清站在广场上",
            "source_span": {"from_segment": 1, "to_segment": 1},
            "source_evidence": [{"segment_index": 1, "quote": "许清站在广场上，看着人群。"}],
            "key_lines": [],
        }],
    )
    _insert_episode(conn, "ep_o1", "proj_o", 1, [1], "art_o1")
    _insert_pack_artifact(conn, "art_o1", "ep_o1", pack_ep1)

    pack_ep3 = _base_pack(
        chapter_indexes=[3],
        characters=[
            {"identity_id": "bible:许清", "display_name": "许清", "portrait_id": "portrait_xu",
             "event_ids": ["ev_001"], "aliases": [],
             "provenance": {"method": "alias_inherited", "source_episode_no": 1}},
            {"identity_id": "bible:王有材", "display_name": "王有材", "portrait_id": "portrait_wang",
             "event_ids": ["ev_001"], "aliases": [],
             # 集 1 的 pack 里根本没有王有材这个 portrait_id 的绑定 -> 断链。
             "provenance": {"method": "alias_inherited", "source_episode_no": 1}},
        ],
        scenes=[],
        event_chain=[{
            "event_id": "ev_001", "order": 1, "summary": "众人议论",
            "source_span": {"from_segment": 1, "to_segment": 1},
            "source_evidence": [{"segment_index": 1, "quote": "众人议论纷纷"}],
            "key_lines": [],
        }],
    )
    pack_ep3["episode_no"] = 3
    _insert_episode(conn, "ep_o3", "proj_o", 3, [3], "art_o3")
    _insert_pack_artifact(conn, "art_o3", "ep_o3", pack_ep3)
    conn.commit()
    conn.close()

    ro = audit.readonly_connection(db_path)
    result = audit.audit_episode(ro, "proj_o", 3)
    ro.close()

    assert [i.code for i in result.a_issues] == ["A1_character_inherited_alias_broken"]
    assert "王有材" in result.a_issues[0].message
    assert "断链" in result.a_issues[0].detail["reason"]
    tally = result.tallies["A1 角色绑定文本依据"]
    # "许清" passed purely via the source-episode's (episode 1's) own binding
    # -- episode 3's own chapter text never mentions him, proving the
    # alias_inherited path (including its recursive re-verification of the
    # source binding) is doing real work.
    assert (tally.checked, tally.passed, tally.legacy_fallback) == (2, 1, 0)


def test_provenance_method_alias_inherited_rejects_non_earlier_source(tmp_path):
    """来源集号必须严格早于当前集——这既是"来源"这个词本身的语义要求，也是
    递归不成环的保证；来源集号等于/晚于当前集必须直接判定失败，不能真去查
    那一集的 pack（哪怕它碰巧存在且同名，逻辑上也不构成"继承"）。"""
    db_path = _make_db(tmp_path)
    conn = _writer(db_path)
    _insert_project(conn, "proj_p")
    _insert_chapter(conn, "proj_p", 1, "第一章", "许清站在广场上，看着人群。")
    _insert_character(conn, "portrait_xu", "proj_p", "许清")
    pack = _base_pack(
        chapter_indexes=[1],
        characters=[
            {"identity_id": "bible:许清", "display_name": "许清", "portrait_id": "portrait_xu",
             "event_ids": ["ev_001"], "aliases": [],
             # 声称继承自"集 1"，但自己就是集 1 —— source_episode_no 不早于
             # 当前集，必须直接失败。
             "provenance": {"method": "alias_inherited", "source_episode_no": 1}},
        ],
        scenes=[],
        event_chain=[{
            "event_id": "ev_001", "order": 1, "summary": "许清站在广场上",
            "source_span": {"from_segment": 1, "to_segment": 1},
            "source_evidence": [{"segment_index": 1, "quote": "许清站在广场上，看着人群。"}],
            "key_lines": [],
        }],
    )
    _insert_episode(conn, "ep_p1", "proj_p", 1, [1], "art_p1")
    _insert_pack_artifact(conn, "art_p1", "ep_p1", pack)
    conn.commit()
    conn.close()

    ro = audit.readonly_connection(db_path)
    result = audit.audit_episode(ro, "proj_p", 1)
    ro.close()

    assert [i.code for i in result.a_issues] == ["A1_character_inherited_alias_broken"]
    assert "不早于当前集" in result.a_issues[0].detail["reason"]


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
