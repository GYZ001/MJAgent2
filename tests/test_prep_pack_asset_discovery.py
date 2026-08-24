"""Red-first wiring tests: episode_prep_pack asset mapping must route
unresolved-but-real characters/scenes through the inherited discovery
mechanism (app.portraits / app.scenes) before gate-failing, and must never
silently pass when a mention still cannot be resolved after discovery.

These tests exercise ``app.production.prep_pack._resolve_assets`` -- the
orchestration/wiring this task adds -- by monkeypatching its two discovery
collaborators (``app.portraits.ensure_cards_for_text`` and
``app.scenes.ensure_scenes_for_labels``). The underlying identity-discovery
model contract itself is exhaustively covered by tests/test_character_discovery.py
and is not re-tested here; what is red-first here is the *wiring*: does an
unresolved real mention actually reach discovery, does a resolved-by-discovery
mention flow through to the manifest, does a genuine one-off/occupation-title
extra get legally absorbed into ``functional_extras`` instead of blocking, and
does a mention discovery explicitly and specifically failed on still hard-fail
the gate instead of being silently dropped or silently passed.

Two real runs surfaced concrete bugs this file guards against:

1. EP2: "小胖子" (7 on-screen appearances, real dialogue) was tagged
   ``is_background_extra=true`` by the chunk-extraction model (a *different*,
   earlier model call that never looked at the bible) and, under the first
   build of ``_resolve_assets``, that flag exempted it from resolution
   entirely -- even though "小胖子" is 李富贵, already carded with a portrait.
   ``test_known_alias_flagged_as_background_extra_still_binds_to_its_portrait``
   is that exact case.

2. EP13: several occupation-title one-off mentions ("养丹坊掌柜", "宝阁执事",
   "围观弟子", "一名外宗弟子") got no matching disposition from discovery by
   exact string (discovery phrases its own candidates independently, e.g.
   "外宗弟子" vs the chunk extractor's "一名外宗弟子" for the same crowd
   concept) even though discovery ran cleanly (no errors) and, in the same
   call, successfully carded+portraited a real new character ("曹阳"). Per
   app.portraits' own rule for unconfirmed-real-name one-offs (typed
   functional identity, source label retained, never silently dropped), these
   must be legally absorbed into ``asset_manifest.functional_extras``, not
   block the gate. ``test_occupation_title_extras_absorbed_into_functional_extras``
   is that exact case.

Real, no-mock end-to-end verification (actual model calls, actual portrait/
scene-reference generation) is done separately against live episodes -- see
the task's verification report, not this file.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

from app import portraits, scenes
from app.production import prep_pack


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT)")
    conn.execute(
        "CREATE TABLE character_portraits(id TEXT, project_id TEXT, character_name TEXT, "
        "ep_start INTEGER, ep_end INTEGER)"
    )
    conn.execute(
        "CREATE TABLE scene_references(id TEXT, project_id TEXT, scene_name TEXT, "
        "ep_start INTEGER, ep_end INTEGER)"
    )
    conn.execute(
        "INSERT INTO projects(id, bible_json) VALUES ('p1', ?)",
        (json.dumps({
            "characters": [], "scenes": [],
            "world": {"era": "", "genre": "", "visual_style_canonical": "测试画风"},
        }, ensure_ascii=False),),
    )
    conn.commit()
    return conn


def _event(event_id: str, *, characters=None, scenes_=None) -> dict:
    return {
        "event_id": event_id,
        "characters": characters or [],
        "scenes": scenes_ or [],
    }


def _resolve(conn, **kwargs):
    defaults = dict(
        project_id="p1", episode_id="ep-test", episode_no=2,
        source_text="占位原文。", run_id=None,
    )
    defaults.update(kwargs)
    return asyncio.run(prep_pack._resolve_assets(conn, **defaults))


# ---------------------------------------------------------------------------
# Regression guard: episodes where pass 1 already resolves everything (the
# EP1 shape) must never call discovery -- zero calls, byte-identical result
# to the pre-discovery function.
# ---------------------------------------------------------------------------

def test_fully_known_cast_triggers_zero_discovery_calls(monkeypatch):
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp1','p1','萧炎',1,NULL)"
    )
    conn.execute(
        "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end) "
        "VALUES ('sr1','p1','宗门广场',1,NULL)"
    )
    conn.commit()

    def boom_character(*_a, **_k):
        raise AssertionError("EP1 式全谱内集不应调用角色发现")

    async def boom_scene(*_a, **_k):
        raise AssertionError("EP1 式全谱内集不应调用场景发现")

    monkeypatch.setattr(portraits, "ensure_cards_for_text", boom_character)
    monkeypatch.setattr(scenes, "ensure_scenes_for_labels", boom_scene)

    events = [_event(
        "ev_001",
        characters=[{"display_name": "萧炎", "is_background_extra": False}],
        scenes_=[{"display_name": "宗门广场"}],
    )]
    characters, scene_list, functional_extras, errors, stats = _resolve(
        conn, episode_no=1, events=events,
    )

    assert errors == []
    assert stats == {"character_discovery_calls": 0, "scene_discovery_calls": 0}
    assert functional_extras == []
    assert characters == [{
        "identity_id": "bible:萧炎", "display_name": "萧炎",
        "portrait_id": "cp1", "event_ids": ["ev_001"], "aliases": [],
    }]
    assert scene_list == [{
        "scene_id": "scene:宗门广场", "display_name": "宗门广场",
        "scene_reference_id": "sr1", "event_ids": ["ev_001"],
    }]


# ---------------------------------------------------------------------------
# Real bug #1 (EP2): a mention the chunk-extraction model itself guessed was
# a background extra must NOT be exempted from resolution -- if it is
# actually a known bible character under a nickname, it must still bind to
# that character's existing portrait_id, with the nickname recorded in the
# manifest entry's ``aliases``.
# ---------------------------------------------------------------------------

def test_known_alias_flagged_as_background_extra_still_binds_to_its_portrait(monkeypatch):
    """真实 EP2 案例：'小胖子' 出场 7 个事件，真名李富贵已在人物谱且有定妆照，
    但事件链抽取模型把它标成了 is_background_extra=true。这个标记来自另一个
    从未看过人物谱的模型调用，不能免除解析——必须照常走身份消歧，绑定到李富贵
    已有的 portrait_id，并把'小胖子'记入该角色的 aliases。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-lfg','p1','李富贵',1,NULL)"
    )
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-mh','p1','孟浩',1,NULL)"
    )
    conn.commit()

    calls = {"n": 0}

    async def fake_disambiguate(project_id, episode_no, source_text, bible, *, generate_portraits=True):
        calls["n"] += 1
        return {
            "added": [], "skipped": [],
            "resolutions": [{
                "source_label": "小胖子", "canonical_name": "李富贵",
                "resolution": "future_identity",
            }],
            "errors": [], "warnings": [],
        }

    monkeypatch.setattr(portraits, "ensure_cards_for_text", fake_disambiguate)
    monkeypatch.setattr(portraits, "persist_screenplay_character_resolutions", lambda *a, **k: [])

    events = [
        _event("ev_002", characters=[
            {"display_name": "孟浩", "is_background_extra": False},
            {"display_name": "小胖子", "is_background_extra": True},
        ]),
        _event("ev_003", characters=[
            {"display_name": "小胖子", "is_background_extra": True},
        ]),
    ]
    characters, scene_list, functional_extras, errors, stats = _resolve(conn, events=events)

    assert calls["n"] == 1
    assert stats["character_discovery_calls"] == 1
    assert errors == []
    assert functional_extras == []
    by_portrait = {c["portrait_id"]: c for c in characters}
    assert "cp-lfg" in by_portrait, "小胖子/李富贵必须出现在 asset_manifest 中，不能被静默丢弃"
    entry = by_portrait["cp-lfg"]
    assert entry["display_name"] == "李富贵"
    assert entry["aliases"] == ["小胖子"]
    assert entry["event_ids"] == ["ev_002", "ev_003"]


# ---------------------------------------------------------------------------
# Real bug #2 (EP13): occupation-title one-off mentions discovery has no
# exact-string opinion on (but ran cleanly, no errors) must be legally
# absorbed into functional_extras under their own source label -- not
# blocked, not silently dropped, not renamed to something generic.
# ---------------------------------------------------------------------------

def test_occupation_title_extras_absorbed_into_functional_extras(monkeypatch):
    """真实 EP13 案例：'养丹坊掌柜'/'宝阁执事'/'围观弟子'/'一名外宗弟子' 这类
    职业称谓一次性人物，discovery 干净运行（无 errors）却没有对这些具体字符串
    给出匹配的 disposition（它自己的候选用了不同措辞）。按 portraits.py 既有
    铁律（未确认真名的一次性人物保留来源称谓、签发 typed functional identity），
    这些必须合法收编进 functional_extras，而不是卡在门禁。同一次调用里，真正
    的新角色（曹阳）必须正常建卡出图并出现在 characters[] 里。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-my','p1','孟浩',1,NULL)"
    )

    async def fake_discovery(project_id, episode_no, source_text, bible, *, generate_portraits=True):
        conn.execute(
            "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
            "VALUES ('cp-cy','p1','曹阳',2,NULL)"
        )
        conn.commit()
        return {
            "added": [{"name": "曹阳"}], "skipped": [],
            # Discovery's own phrasing differs from the chunk extractor's
            # raw mentions below on purpose -- this must NOT block them.
            "resolutions": [{
                "source_label": "外宗弟子", "canonical_name": "外宗弟子",
                "resolution": "functional_identity",
            }],
            "errors": [], "warnings": [],
        }

    monkeypatch.setattr(portraits, "ensure_cards_for_text", fake_discovery)
    monkeypatch.setattr(portraits, "persist_screenplay_character_resolutions", lambda *a, **k: [])

    events = [
        _event("ev_002", characters=[
            {"display_name": "孟浩", "is_background_extra": False},
            {"display_name": "养丹坊掌柜", "is_background_extra": True},
            {"display_name": "宝阁执事", "is_background_extra": True},
        ]),
        _event("ev_008", characters=[
            {"display_name": "孟浩", "is_background_extra": False},
            {"display_name": "曹阳", "is_background_extra": False},
            {"display_name": "围观弟子", "is_background_extra": True},
        ]),
        _event("ev_011", characters=[
            {"display_name": "曹阳", "is_background_extra": False},
            {"display_name": "一名外宗弟子", "is_background_extra": True},
            {"display_name": "围观弟子", "is_background_extra": True},
        ]),
    ]
    characters, scene_list, functional_extras, errors, stats = _resolve(conn, events=events)

    assert errors == [], f"职业称谓类龙套不应阻断门禁：{errors}"
    assert stats["character_discovery_calls"] == 1

    by_name = {c["display_name"]: c for c in characters}
    assert "曹阳" in by_name, "同一次发现调用里的真新角色必须正常建卡入谱"
    assert by_name["曹阳"]["portrait_id"] == "cp-cy"

    extras_by_label = {e["label"]: e["event_ids"] for e in functional_extras}
    assert extras_by_label == {
        "养丹坊掌柜": ["ev_002"],
        "宝阁执事": ["ev_002"],
        "围观弟子": ["ev_008", "ev_011"],
        "一名外宗弟子": ["ev_011"],
    }


def test_default_functional_fallback_still_excludes_non_person_skips(monkeypatch):
    """群演兜底只收编"人"——discovery 明确判定"非人"（宗门/器物/作者笔名）的
    条目不能进 functional_extras（那是给 P1 分镜台画面里的人用的），也不能
    卡门禁；两者都以"合法跳过、不建卡"收场，只是不出现在 functional_extras。"""
    conn = _make_conn()

    async def fake_not_person(project_id, episode_no, source_text, bible, *, generate_portraits=True):
        return {
            "added": [], "resolutions": [],
            "skipped": [{"status": "skipped_not_person", "name": "天启宗", "reason": "宗门非人"}],
            "errors": [], "warnings": [],
        }

    monkeypatch.setattr(portraits, "ensure_cards_for_text", fake_not_person)
    monkeypatch.setattr(portraits, "persist_screenplay_character_resolutions", lambda *a, **k: [])
    events = [_event("ev_001", characters=[{"display_name": "天启宗", "is_background_extra": False}])]
    characters, scene_list, functional_extras, errors, stats = _resolve(conn, events=events)

    assert errors == []
    assert characters == []
    assert functional_extras == [], "非人实体不应出现在 functional_extras（那是给人用的）"


# ---------------------------------------------------------------------------
# Red -> green: a real, unresolved character/scene must route through
# discovery, and once discovery registers it, the second pass must resolve it.
# ---------------------------------------------------------------------------

def test_unresolved_new_character_routes_through_discovery_and_resolves(monkeypatch):
    calls = {"n": 0}

    async def fake_ensure_cards_for_text(project_id, episode_no, source_text, bible, *, generate_portraits=True):
        calls["n"] += 1
        assert project_id == "p1" and episode_no == 2
        assert generate_portraits is True
        conn.execute(
            "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
            "VALUES ('cp-new','p1','沈青梧',2,NULL)"
        )
        conn.commit()
        return {"added": [{"name": "沈青梧"}], "resolutions": [], "errors": [], "skipped": [], "warnings": []}

    conn = _make_conn()
    monkeypatch.setattr(portraits, "ensure_cards_for_text", fake_ensure_cards_for_text)
    monkeypatch.setattr(portraits, "persist_screenplay_character_resolutions", lambda *a, **k: [])

    events = [_event(
        "ev_001", characters=[{"display_name": "沈青梧", "is_background_extra": False}],
    )]
    characters, scene_list, functional_extras, errors, stats = _resolve(conn, events=events)

    assert calls["n"] == 1
    assert stats["character_discovery_calls"] == 1
    assert errors == []
    assert functional_extras == []
    assert characters == [{
        "identity_id": "bible:沈青梧", "display_name": "沈青梧",
        "portrait_id": "cp-new", "event_ids": ["ev_001"], "aliases": [],
    }]


def test_unresolved_new_scene_routes_through_discovery_and_resolves(monkeypatch):
    calls = {"n": 0}

    async def fake_ensure_scenes_for_labels(project_id, episode_no, labels):
        calls["n"] += 1
        assert labels == ["藏经阁"]
        conn.execute(
            "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end) "
            "VALUES ('sr-new','p1','藏经阁',2,NULL)"
        )
        conn.commit()
        return {"added": [{"name": "藏经阁"}], "errors": [], "ready_scenes": ["藏经阁"],
                "resolved_names": {"藏经阁": "藏经阁"}}

    conn = _make_conn()
    monkeypatch.setattr(scenes, "ensure_scenes_for_labels", fake_ensure_scenes_for_labels)

    events = [_event("ev_001", scenes_=[{"display_name": "藏经阁"}])]
    characters, scene_list, functional_extras, errors, stats = _resolve(conn, events=events)

    assert calls["n"] == 1
    assert stats["scene_discovery_calls"] == 1
    assert errors == []
    assert scene_list == [{
        "scene_id": "scene:藏经阁", "display_name": "藏经阁",
        "scene_reference_id": "sr-new", "event_ids": ["ev_001"],
    }]


def test_alias_scene_resolves_via_canonical_name_after_discovery(monkeypatch):
    """场景发现判定新提及只是已有场景的别名时，asset_manifest 必须落到已有场景的
    scene_reference_id 上，不能因为原始提及字面量不同而继续报未解析。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end) "
        "VALUES ('sr1','p1','宗门广场',1,NULL)"
    )
    conn.commit()

    async def fake_ensure_scenes_for_labels(project_id, episode_no, labels):
        return {"added": [], "errors": [], "ready_scenes": [],
                "resolved_names": {"门派前庭": "宗门广场"}}

    monkeypatch.setattr(scenes, "ensure_scenes_for_labels", fake_ensure_scenes_for_labels)
    events = [_event("ev_001", scenes_=[{"display_name": "门派前庭"}])]
    characters, scene_list, functional_extras, errors, stats = _resolve(conn, events=events)

    assert errors == []
    assert scene_list == [{
        "scene_id": "scene:宗门广场", "display_name": "宗门广场",
        "scene_reference_id": "sr1", "event_ids": ["ev_001"],
    }]


# ---------------------------------------------------------------------------
# Discovery correctly classifying a one-off mention as a typed functional
# identity (确定性群演) by an exact-string resolution match must also land
# in functional_extras (not just silently resolve with no trace).
# ---------------------------------------------------------------------------

def test_functional_identity_after_discovery_needs_no_portrait(monkeypatch):
    conn = _make_conn()

    async def fake_functional(project_id, episode_no, source_text, bible, *, generate_portraits=True):
        return {
            "added": [], "skipped": [],
            "resolutions": [{
                "source_label": "黑衣人", "canonical_name": "黑衣人",
                "resolution": "functional_identity",
            }],
            "errors": [], "warnings": [],
        }

    monkeypatch.setattr(portraits, "ensure_cards_for_text", fake_functional)
    monkeypatch.setattr(portraits, "persist_screenplay_character_resolutions", lambda *a, **k: [])
    events = [_event("ev_001", characters=[{"display_name": "黑衣人", "is_background_extra": False}])]
    characters, scene_list, functional_extras, errors, stats = _resolve(conn, events=events)

    assert errors == []
    assert characters == []
    assert functional_extras == [{"label": "黑衣人", "event_ids": ["ev_001"]}]
    assert stats["character_discovery_calls"] == 1


def test_skipped_not_person_after_discovery_needs_no_portrait(monkeypatch):
    """判定为“非人”（宗门/器物/作者笔名等）同样不应卡门禁——discovery 的
    ``skipped`` 名单本身就是一个明确处置，不是留白。也不应进 functional_extras
    （见 test_default_functional_fallback_still_excludes_non_person_skips）。"""
    conn = _make_conn()

    async def fake_not_person(project_id, episode_no, source_text, bible, *, generate_portraits=True):
        return {
            "added": [], "resolutions": [],
            "skipped": [{"status": "skipped_not_person", "name": "天启宗", "reason": "宗门非人"}],
            "errors": [], "warnings": [],
        }

    monkeypatch.setattr(portraits, "ensure_cards_for_text", fake_not_person)
    monkeypatch.setattr(portraits, "persist_screenplay_character_resolutions", lambda *a, **k: [])
    events = [_event("ev_001", characters=[{"display_name": "天启宗", "is_background_extra": False}])]
    characters, scene_list, functional_extras, errors, stats = _resolve(conn, events=events)

    assert errors == []
    assert characters == []
    assert functional_extras == []


def test_alias_rename_after_discovery_resolves_to_real_name(monkeypatch):
    """事件链原始提及是称谓，发现机制确认了背后的真名后，manifest 必须落到
    真名已有的 portrait 上（不能因为字面量不同继续报未解析）。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp1','p1','苍玄',1,NULL)"
    )
    conn.commit()

    async def fake_rename(project_id, episode_no, source_text, bible, *, generate_portraits=True):
        return {
            "added": [], "skipped": [],
            "resolutions": [{
                "source_label": "神秘老者", "canonical_name": "苍玄",
                "resolution": "future_identity",
            }],
            "errors": [], "warnings": [],
        }

    monkeypatch.setattr(portraits, "ensure_cards_for_text", fake_rename)
    monkeypatch.setattr(portraits, "persist_screenplay_character_resolutions", lambda *a, **k: [])
    events = [_event("ev_001", characters=[{"display_name": "神秘老者", "is_background_extra": False}])]
    characters, scene_list, functional_extras, errors, stats = _resolve(conn, events=events)

    assert errors == []
    assert functional_extras == []
    assert characters == [{
        "identity_id": "bible:苍玄", "display_name": "苍玄",
        "portrait_id": "cp1", "event_ids": ["ev_001"], "aliases": ["神秘老者"],
    }]


# ---------------------------------------------------------------------------
# The core "no silent pass" guarantee: the ONLY thing that still hard-blocks
# after a real discovery call is discovery explicitly, specifically failing
# on that one name (a confirmed real identity whose card generation itself
# broke, or a processing exception) -- not silence, not "insufficient
# screen time". A clean, silent discovery result defaults to functional
# (see test_occupation_title_extras_absorbed_into_functional_extras above);
# this section is what must still fail.
# ---------------------------------------------------------------------------

def test_discovery_explicit_named_error_still_gate_fails(monkeypatch):
    """协调方要求保留的红灯：discovery 对某个具体称谓给出了明确失败结论（如
    真名已确认但人物卡模型未完成）→ 门禁必须报出该称谓阻断，不能被兜底成
    functional_extras 静默放行。"""
    conn = _make_conn()

    async def failing_discovery(project_id, episode_no, source_text, bible, *, generate_portraits=True):
        return {
            "added": [], "resolutions": [], "skipped": [], "warnings": [],
            "errors": ["神秘蒙面人：身份模型已确认真名，但人物卡模型未返回完整稳定卡片"],
        }

    monkeypatch.setattr(portraits, "ensure_cards_for_text", failing_discovery)
    monkeypatch.setattr(portraits, "persist_screenplay_character_resolutions", lambda *a, **k: [])
    events = [_event(
        "ev_001",
        characters=[{"display_name": "神秘蒙面人", "is_background_extra": True}],
    )]
    characters, scene_list, functional_extras, errors, stats = _resolve(conn, events=events)

    assert stats["character_discovery_calls"] == 1
    assert any("神秘蒙面人" in message for message in errors), (
        "discovery 明确点名失败的称谓必须在门禁错误里被具名报出，不能静默放行"
    )
    assert characters == []
    assert functional_extras == [], "明确失败的称谓不能被兜底成 functional_extras"


def test_scene_discovery_finding_nothing_still_gate_fails(monkeypatch):
    """场景没有 functional_extras 兜底概念（不存在"确定性群演场景"）：discovery
    对未解析场景没有任何结论时，仍必须门禁失败，行为不变。"""
    conn = _make_conn()

    async def noop_scene_discovery(project_id, episode_no, labels):
        return {"added": [], "errors": [], "ready_scenes": [], "resolved_names": {}}

    monkeypatch.setattr(scenes, "ensure_scenes_for_labels", noop_scene_discovery)
    events = [_event("ev_001", scenes_=[{"display_name": "无名之地"}])]
    characters, scene_list, functional_extras, errors, stats = _resolve(conn, events=events)

    assert stats["scene_discovery_calls"] == 1
    assert errors, "场景 discovery 无结果时必须门禁失败，不能静默放行"
    assert scene_list == []


def test_discovery_error_entries_surface_in_final_gate_message(monkeypatch):
    """discovery 自己报出的具体失败原因不能被吞掉，必须能在最终门禁错误里查到。"""
    conn = _make_conn()

    async def failing_discovery(project_id, episode_no, source_text, bible, *, generate_portraits=True):
        return {
            "added": [], "resolutions": [], "skipped": [], "warnings": [],
            "errors": ["无名之人：新角色评估失败（诊断标记 ABC123）"],
        }

    monkeypatch.setattr(portraits, "ensure_cards_for_text", failing_discovery)
    monkeypatch.setattr(portraits, "persist_screenplay_character_resolutions", lambda *a, **k: [])
    events = [_event("ev_001", characters=[{"display_name": "无名之人", "is_background_extra": False}])]
    characters, scene_list, functional_extras, errors, stats = _resolve(conn, events=events)

    assert any("ABC123" in message for message in errors)
    assert functional_extras == []
