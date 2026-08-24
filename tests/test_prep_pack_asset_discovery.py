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
    # 1.5.0: only used by app.portraits._future_chapter_context, which
    # _prep_pack_verify_true_name_hypothesis reuses for the suspected_true_name
    # forward-window check. Existing tests never set suspected_true_name, so
    # they never reach that code path -- these two empty-by-default tables
    # are additive and safe.
    conn.execute(
        "CREATE TABLE episodes(project_id TEXT, episode_no INTEGER, source_chapters TEXT)"
    )
    conn.execute("CREATE TABLE chapters(project_id TEXT, idx INTEGER, content TEXT)")
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
    characters, scene_list, functional_extras, errors, stats, true_name_hints = _resolve(
        conn, episode_no=1, events=events,
        # 1.4.2 称谓/场景名证据闸要求直接命中必须有本集文本证据 -- 两个提及都
        # 得逐字出现在这里，否则会被误当成"裸命中没证据"（这不是本测试要覆盖
        # 的场景，本测试要的是"零调用发现"，所以必须让直接命中干净通过）。
        source_text="萧炎快步穿过宗门广场，众弟子纷纷让路。",
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
    characters, scene_list, functional_extras, errors, stats, true_name_hints = _resolve(
        conn, events=events,
        # 1.4.2 称谓证据闸：小胖子是原文里对他的真实称呼，天然会出现在原文中
        # -- 这正是合法前瞻解析该有的样子，跟真实 EP5 裸幻觉绑定（"丹鬼"原文
        # 0 次出现）的区别就在这里。
        source_text="孟浩看着小胖子，觉得这称呼倒也贴切，小胖子憨憨一笑。",
    )

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
    characters, scene_list, functional_extras, errors, stats, true_name_hints = _resolve(
        conn, events=events,
        # 1.4.2 称谓证据闸只管走 portrait_id 解析的名字（孟浩/曹阳）；职业称谓
        # 类群演走 skip_character_names 分支，从不到达证据闸，不需要出现在这里。
        source_text="孟浩与曹阳并肩走在外宗广场上，四周弟子纷纷侧目。",
    )

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
    characters, scene_list, functional_extras, errors, stats, true_name_hints = _resolve(conn, events=events)

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
    characters, scene_list, functional_extras, errors, stats, true_name_hints = _resolve(
        conn, events=events,
        # 1.4.2 称谓证据闸：discovery 在本例里直接以原始提及字符串建卡（沒有
        # rename），第二遍走的仍是裸直接命中路径，一样要求证据。
        source_text="沈青梧提剑而立，目光冷冽。",
    )

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
    characters, scene_list, functional_extras, errors, stats, true_name_hints = _resolve(conn, events=events)

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
    characters, scene_list, functional_extras, errors, stats, true_name_hints = _resolve(conn, events=events)

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
    characters, scene_list, functional_extras, errors, stats, true_name_hints = _resolve(conn, events=events)

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
    characters, scene_list, functional_extras, errors, stats, true_name_hints = _resolve(conn, events=events)

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
    characters, scene_list, functional_extras, errors, stats, true_name_hints = _resolve(
        conn, events=events,
        # 1.4.2 称谓证据闸：discovery 之所以能提出这条改名，前提就是它自己在
        # 本集文本里找到了"神秘老者"这个称谓；真实场景里这原本就该出现在原文，
        # 这里补上让夹具符合这个前提（不是放宽门禁本身）。
        source_text="一名神秘老者悄然现身，气息深不可测。",
    )

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
    characters, scene_list, functional_extras, errors, stats, true_name_hints = _resolve(conn, events=events)

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
    characters, scene_list, functional_extras, errors, stats, true_name_hints = _resolve(conn, events=events)

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
    characters, scene_list, functional_extras, errors, stats, true_name_hints = _resolve(conn, events=events)

    assert any("ABC123" in message for message in errors)
    assert functional_extras == []


# ---------------------------------------------------------------------------
# 1.4.2 -- real round-16 EP5 regression: text-evidence gate for direct
# character/scene binds. Real EP5 output resolved two events describing an
# unnamed pair of old men on an unrelated mountain peak near 靠山宗 to a
# pre-existing character ("丹鬼") and scene ("大青山山顶") from elsewhere in
# the story, purely because the event-chain extraction model happened to
# write those exact already-registered names directly as display_name --
# chapter 5's own text has zero occurrences of either string (verified
# against the real chapters row, proj_3ac0b627fa46 idx=5). Fixture below
# mirrors that exact shape: bible already carries 丹鬼/大青山山顶 from
# "elsewhere", this episode's own text only ever describes the pair
# generically ("两个老者盘膝而坐"), never naming them.
# ---------------------------------------------------------------------------

def test_ep5_hallucinated_character_bind_with_no_text_evidence_is_gate_blocked(monkeypatch):
    """红灯（协调方点名，2c）：裸直接命中在人物谱里找到了「丹鬼」，但「丹鬼」
    在本集原文里 0 次出现——必须门禁具名拦截，且绝不能静默改路由去发现（发现
    有可能重犯同一种臆断错误，协调方明确要求"具名拦截"而非"回炉"）。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-dg','p1','丹鬼',1,NULL)"
    )
    conn.commit()

    def boom_character(*_a, **_k):
        raise AssertionError("裸命中没有证据应门禁具名拦截，不应该回炉重新发现")

    monkeypatch.setattr(portraits, "ensure_cards_for_text", boom_character)

    events = [_event(
        "ev_008", characters=[{"display_name": "丹鬼", "is_background_extra": False}],
    )]
    characters, scene_list, functional_extras, errors, stats, true_name_hints = _resolve(
        conn, events=events,
        source_text="山顶上，两个老者盘膝而坐，笑眯眯地看着山下的广场。",
    )

    assert characters == [], "没有证据的裸命中绝不能进入 asset_manifest"
    assert stats["character_discovery_calls"] == 0
    assert any(
        "丹鬼" in message and "缺少称谓证据" in message and "门禁具名拦截" in message
        for message in errors
    )


def test_ep5_hallucinated_scene_bind_with_no_text_evidence_routes_through_discovery(monkeypatch):
    """红灯（协调方点名，2c）：裸直接命中在场景库里找到了「大青山山顶」（孟浩
    老家的山，跟本集靠山宗旁的山峰毫无关系），但「大青山山顶」在本集原文里
    0 次出现——必须当作未解析改走场景发现（本例应新建"靠山宗外围山峰"），
    绝不能静默沿用谱内那个不相关的既有场景。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end) "
        "VALUES ('sr-dqs','p1','大青山山顶',1,NULL)"
    )
    conn.commit()

    calls = {"n": 0}

    async def fake_ensure_scenes_for_labels(project_id, episode_no, labels):
        calls["n"] += 1
        assert labels == ["大青山山顶"]
        conn.execute(
            "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end) "
            "VALUES ('sr-new','p1','靠山宗外围山峰',2,NULL)"
        )
        conn.commit()
        return {
            "added": [{"name": "靠山宗外围山峰"}], "errors": [],
            "ready_scenes": ["靠山宗外围山峰"],
            "resolved_names": {"大青山山顶": "靠山宗外围山峰"},
        }

    monkeypatch.setattr(scenes, "ensure_scenes_for_labels", fake_ensure_scenes_for_labels)

    events = [_event("ev_008", scenes_=[{"display_name": "大青山山顶"}])]
    characters, scene_list, functional_extras, errors, stats, true_name_hints = _resolve(
        conn, events=events,
        source_text="山顶上，两个老者盘膝而坐，笑眯眯地看着山下的广场。",
    )

    assert calls["n"] == 1  # 没证据的裸命中被当成未解析，触发了场景发现
    assert stats["scene_discovery_calls"] == 1
    assert errors == []
    assert scene_list == [{
        "scene_id": "scene:靠山宗外围山峰", "display_name": "靠山宗外围山峰",
        "scene_reference_id": "sr-new", "event_ids": ["ev_008"],
    }]
    assert not any(s["scene_reference_id"] == "sr-dqs" for s in scene_list), (
        "绝不能静默沿用谱内那个不相关的「大青山山顶」"
    )


# ---------------------------------------------------------------------------
# 1.5.0 -- 用户修正令：outright 禁止先验知识会扔掉真正猜对的真名（"丹鬼"这
# 类猜对了本该是加分项）。模型可以申报 suspected_true_name，但从不被直接
# 采信——必须经 _prep_pack_verify_true_name_hypothesis 核验：申报名解析到
# 已有谱内身份，且该名本身出现在本集原文或 app.portraits 同一条前瞻窗口
# （IDENTITY_DISCOVERY_FORWARD_CHAPTERS）的文本里，两条都满足才采信。
# ---------------------------------------------------------------------------

def test_suspected_true_name_hypothesis_verified_via_forward_window_binds_with_alias(monkeypatch):
    """红灯（协调方点名 1.5.0-4a）：模型申报"灰袍老者"疑似真名"丹鬼"，本集
    原文没有"丹鬼"，但前瞻窗口（本集源章节之后 IDENTITY_DISCOVERY_FORWARD_
    CHAPTERS 章内）的原文确实提到"丹鬼"——核验通过，绑定到丹鬼已有的
    portrait_id，走的是核验快车道，不再触发全量身份消歧模型调用，
    aliases=["灰袍老者"]。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-dg','p1','丹鬼',1,NULL)"
    )
    conn.execute(
        "INSERT INTO episodes(project_id, episode_no, source_chapters) VALUES ('p1', 2, '[5]')"
    )
    conn.execute(
        "INSERT INTO chapters(project_id, idx, content) VALUES ('p1', 6, '丹鬼缓缓摘下兜帽，露出真容。')"
    )
    conn.commit()

    def boom_character(*_a, **_k):
        raise AssertionError("核验通过的假设应走确定性快车道，不应该再触发全量身份发现")

    monkeypatch.setattr(portraits, "ensure_cards_for_text", boom_character)

    events = [_event("ev_013", characters=[
        {"display_name": "灰袍老者", "is_background_extra": False, "suspected_true_name": "丹鬼"},
    ])]
    characters, scene_list, functional_extras, errors, stats, true_name_hints = _resolve(
        conn, events=events,
        source_text="山顶上，灰袍老者哈哈大笑起来。",
    )

    assert errors == []
    assert stats["character_discovery_calls"] == 0
    assert characters == [{
        "identity_id": "bible:丹鬼", "display_name": "丹鬼",
        "portrait_id": "cp-dg", "event_ids": ["ev_013"], "aliases": ["灰袍老者"],
    }]
    assert any(
        h["status"] == "accepted" and h["mention"] == "灰袍老者"
        and h["suspected_true_name"] == "丹鬼"
        for h in true_name_hints
    )


def test_suspected_true_name_hypothesis_rejected_with_no_evidence_routes_to_discovery(monkeypatch):
    """红灯（协调方点名 1.5.0-4b）：模型申报"山峰"疑似正名"大青山"，本集原文
    和前瞻窗口都没有任何"大青山"的踪迹——假设核验失败、丢弃，回退到"山峰"
    本身的常规解析（未注册场景→走场景发现），rejected 计数=1，绝不能静默
    沿用谱内那个不相关的"大青山"。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end) "
        "VALUES ('sr-dqs','p1','大青山',1,NULL)"
    )
    conn.execute(
        "INSERT INTO episodes(project_id, episode_no, source_chapters) VALUES ('p1', 2, '[5]')"
    )
    conn.execute(
        "INSERT INTO chapters(project_id, idx, content) VALUES ('p1', 6, '完全不相关的后续剧情。')"
    )
    conn.commit()

    calls = {"n": 0}

    async def fake_ensure_scenes_for_labels(project_id, episode_no, labels):
        calls["n"] += 1
        assert labels == ["山峰"]
        conn.execute(
            "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end) "
            "VALUES ('sr-new','p1','靠山宗外围山峰',2,NULL)"
        )
        conn.commit()
        return {
            "added": [{"name": "靠山宗外围山峰"}], "errors": [],
            "ready_scenes": ["靠山宗外围山峰"],
            "resolved_names": {"山峰": "靠山宗外围山峰"},
        }

    monkeypatch.setattr(scenes, "ensure_scenes_for_labels", fake_ensure_scenes_for_labels)

    events = [_event("ev_008", scenes_=[
        {"display_name": "山峰", "suspected_true_name": "大青山"},
    ])]
    characters, scene_list, functional_extras, errors, stats, true_name_hints = _resolve(
        conn, events=events,
        source_text="靠山宗四周的山峰上，两个老者盘膝而坐。",
    )

    assert calls["n"] == 1
    assert stats["scene_discovery_calls"] == 1
    assert errors == []
    assert scene_list == [{
        "scene_id": "scene:靠山宗外围山峰", "display_name": "靠山宗外围山峰",
        "scene_reference_id": "sr-new", "event_ids": ["ev_008"],
    }]
    rejected = [h for h in true_name_hints if h["status"] == "rejected"]
    assert len(rejected) == 1
    assert rejected[0] == {
        "kind": "scene", "mention": "山峰", "suspected_true_name": "大青山", "status": "rejected",
    }
    assert not any(s["scene_reference_id"] == "sr-dqs" for s in scene_list), (
        "绝不能静默沿用谱内那个不相关的「大青山」"
    )


# ---------------------------------------------------------------------------
# 1.5.0 -- speaker 名册引用化（真实 EP2 回归：关键台词"割舌头"的 speaker 被
# 写成"韩宗"，实际说话人是"绿袍男子"，韩宗第 5 章才出场，speaker 字段从未
# 进任何校验管线）。这两个函数是纯确定性查表，不需要 DB/异步，直接单测。
# ---------------------------------------------------------------------------

def test_speaker_with_no_roster_match_is_gate_blocked():
    """红灯（协调方点名 1.5.0-speaker-a，真实 EP2 回归）：台词说话人"韩宗"
    没有出现在本集资产名册里（本集角色/群演都不是韩宗，韩宗第 5 章才出场）
    ——必须门禁具名阻断，绝不能静默放行。"""
    payload_events = [{
        "event_id": "ev_003",
        "key_lines": [{"speaker": "韩宗", "line": "割了他的舌头。", "segment_index": 10}],
    }]
    roster = prep_pack._prep_pack_build_speaker_roster(
        characters=[{"display_name": "绿袍男子", "identity_id": "bible:绿袍男子", "aliases": []}],
        functional_extras=[],
    )
    errors = prep_pack._prep_pack_resolve_key_line_speakers(payload_events, roster)
    assert any("韩宗" in message and "ev_003" in message for message in errors)
    assert "speaker_ref" not in payload_events[0]["key_lines"][0]


def test_speaker_matching_real_character_display_name_resolves():
    """红灯（协调方点名 1.5.0-speaker-b）：台词说话人"绿袍男子"是本集资产
    名册里真实出场的角色（本集原文称谓），必须正确解析出 speaker_ref。"""
    payload_events = [{
        "event_id": "ev_003",
        "key_lines": [{"speaker": "绿袍男子", "line": "割了他的舌头。", "segment_index": 10}],
    }]
    roster = prep_pack._prep_pack_build_speaker_roster(
        characters=[{"display_name": "绿袍男子", "identity_id": "bible:绿袍男子", "aliases": []}],
        functional_extras=[],
    )
    errors = prep_pack._prep_pack_resolve_key_line_speakers(payload_events, roster)
    assert errors == []
    assert payload_events[0]["key_lines"][0]["speaker_ref"] == "bible:绿袍男子"


def test_speaker_matching_registered_alias_resolves_to_owning_character():
    """红灯（协调方点名 1.5.0-speaker-c）：台词说话人是名册角色的已登记别名
    （比如"小胖子"是李富贵记录在案的 alias），必须正确落到该角色的
    speaker_ref，不能因为字面量不是 display_name 就拦截。"""
    payload_events = [{
        "event_id": "ev_002",
        "key_lines": [{"speaker": "小胖子", "line": "……", "segment_index": 5}],
    }]
    roster = prep_pack._prep_pack_build_speaker_roster(
        characters=[{
            "display_name": "李富贵", "identity_id": "bible:李富贵", "aliases": ["小胖子"],
        }],
        functional_extras=[],
    )
    errors = prep_pack._prep_pack_resolve_key_line_speakers(payload_events, roster)
    assert errors == []
    assert payload_events[0]["key_lines"][0]["speaker_ref"] == "bible:李富贵"


def test_speaker_matching_functional_extra_label_resolves():
    """补充覆盖：speaker 是群演 label（functional_extras），同样应该正确
    解析到 extra: 前缀的 speaker_ref，不应被具名阻断。"""
    payload_events = [{
        "event_id": "ev_008",
        "key_lines": [{"speaker": "山顶老者", "line": "……", "segment_index": 29}],
    }]
    roster = prep_pack._prep_pack_build_speaker_roster(
        characters=[], functional_extras=[{"label": "山顶老者", "event_ids": ["ev_008"]}],
    )
    errors = prep_pack._prep_pack_resolve_key_line_speakers(payload_events, roster)
    assert errors == []
    assert payload_events[0]["key_lines"][0]["speaker_ref"] == "extra:山顶老者"
