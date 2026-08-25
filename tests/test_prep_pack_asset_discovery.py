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

import pytest

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
    # 1.5.0/1.5.2: episodes.source_chapters is only used by
    # app.portraits._future_chapter_context (suspected_true_name forward-window
    # check); episodes.id/screenplay_json is only used by
    # _prep_pack_cross_episode_alias_conflict (task② cross-episode alias
    # consistency, scans OTHER episodes' published asset_manifest). Existing
    # tests never set suspected_true_name and never pre-populate another
    # episode's screenplay_json, so they never exercise either path -- these
    # tables/columns are additive and safe.
    conn.execute(
        "CREATE TABLE episodes(id TEXT, project_id TEXT, episode_no INTEGER, "
        "source_chapters TEXT, screenplay_json TEXT)"
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


def _bible_alias(
    text: str, *, name_kind: str = "referential",
    evidence_chapter_index: int = 1, evidence_quote: str = "",
) -> dict:
    """一条 CharacterAlias 字面量（app.schemas.CharacterAlias 形状），供测试
    往 Bible.characters[].aliases 里塞数据用。prep_pack.py 的读侧只消费
    ``text``，不重新核验证据锚点是否逐字命中——那是全书分析阶段
    （app.stages.generate_bible）自己的职责，不在本文件测试范围内。"""
    return {
        "text": text, "name_kind": name_kind,
        "evidence_chapter_index": evidence_chapter_index,
        "evidence_quote": evidence_quote,
    }


def _seed_bible_characters(conn, project_id: str, characters: list[dict]) -> None:
    """覆盖 projects.bible_json 的 characters[]，其余键保持 _make_conn 的默认
    占位值。1.7.0（docs/CHARACTER_IDENTITY_ENTITY_DESIGN.md §4.1/§6 第3项）：
    跨集别名注册表主读源切换为 Bible.characters[].aliases 后，这是驱动
    _prep_pack_cross_episode_alias_conflict / _prep_pack_lookup_character_
    alias_canonical_name 的主数据源。"""
    conn.execute(
        "UPDATE projects SET bible_json=? WHERE id=?",
        (json.dumps({
            "characters": characters, "scenes": [],
            "world": {"era": "", "genre": "", "visual_style_canonical": "测试画风"},
        }, ensure_ascii=False), project_id),
    )
    conn.commit()


def _bible_character(
    name: str, *, aliases: list[dict] | None = None,
    role: str = "配角", appearance_canonical: str = "占位外观",
) -> dict:
    """一条 Character 字面量（app.schemas.Character 形状）的最小合法构造，
    只填测试关心的字段，其余用 schema 自带默认值。"""
    return {
        "name": name, "role": role, "appearance_canonical": appearance_canonical,
        "aliases": aliases or [],
    }


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
    characters, scene_list, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(
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
        "visual_entity_id": "bible:萧炎", "display_appellation": "萧炎",
        "provenance": {
            "method": "direct", "anchor_segments": [1], "anchor_phrase": "萧炎",
        },
    }]
    assert scene_list == [{
        "scene_id": "scene:宗门广场", "display_name": "宗门广场",
        "scene_reference_id": "sr1", "event_ids": ["ev_001"],
        "provenance": {
            "method": "direct", "anchor_segments": [1], "anchor_phrase": "宗门广场",
        },
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
    characters, scene_list, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(
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
    characters, scene_list, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(
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
    characters, scene_list, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(conn, events=events)

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
    characters, scene_list, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(
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
        "visual_entity_id": "bible:沈青梧", "display_appellation": "沈青梧",
        "provenance": {
            "method": "discovery", "anchor_segments": [1], "anchor_phrase": "沈青梧",
        },
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
    characters, scene_list, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(conn, events=events)

    assert calls["n"] == 1
    assert stats["scene_discovery_calls"] == 1
    assert errors == []
    assert scene_list == [{
        "scene_id": "scene:藏经阁", "display_name": "藏经阁",
        "scene_reference_id": "sr-new", "event_ids": ["ev_001"],
        "provenance": {"method": "discovery", "anchor_segments": [], "anchor_phrase": ""},
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
    characters, scene_list, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(conn, events=events)

    assert errors == []
    assert scene_list == [{
        "scene_id": "scene:宗门广场", "display_name": "宗门广场",
        "scene_reference_id": "sr1", "event_ids": ["ev_001"],
        "provenance": {"method": "resolution", "anchor_segments": [], "anchor_phrase": ""},
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
    characters, scene_list, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(conn, events=events)

    assert errors == []
    assert characters == []
    assert functional_extras == [{
        "label": "黑衣人", "event_ids": ["ev_001"],
        "visual_entity_id": "entity:b645a470abc42e7e",
        "provenance": {"method": "discovery", "anchor_segments": [], "anchor_phrase": ""},
    }]
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
    characters, scene_list, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(conn, events=events)

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
    characters, scene_list, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(
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
        "visual_entity_id": "bible:苍玄", "display_appellation": "神秘老者",
        "provenance": {
            "method": "resolution", "anchor_segments": [1], "anchor_phrase": "神秘老者",
        },
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
    characters, scene_list, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(conn, events=events)

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
    characters, scene_list, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(conn, events=events)

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
    characters, scene_list, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(conn, events=events)

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
    characters, scene_list, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(
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
    characters, scene_list, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(
        conn, events=events,
        source_text="山顶上，两个老者盘膝而坐，笑眯眯地看着山下的广场。",
    )

    assert calls["n"] == 1  # 没证据的裸命中被当成未解析，触发了场景发现
    assert stats["scene_discovery_calls"] == 1
    assert errors == []
    assert scene_list == [{
        "scene_id": "scene:靠山宗外围山峰", "display_name": "靠山宗外围山峰",
        "scene_reference_id": "sr-new", "event_ids": ["ev_008"],
        "provenance": {"method": "discovery", "anchor_segments": [], "anchor_phrase": ""},
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
    """红灯（第29轮身份绑定审判程序）：模型申报"灰袍老者"疑似真名"丹鬼"，
    本集原文没有"丹鬼"，但全书 chapters 扫描（卷宗检索不再局限于某个前瞻
    窗口）能找到含"丹鬼"的第 6 章原文——卷宗非空，裁决模型独立判 same 并
    逐字引用第 6 章原句，引句核验通过——核验通过，绑定到丹鬼已有的
    portrait_id，走的是核验快车道，不再触发全量身份消歧模型调用，
    aliases=["灰袍老者"]，method 标注 resolution_forward（钉住的支撑句
    不落在本集自己的段落里）。"""
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

    verdict_calls = {"n": 0}

    async def fake_chat_structured(messages, **kwargs):
        verdict_calls["n"] += 1
        prompt = str(messages[0]["content"])
        assert "灰袍老者" in prompt and "丹鬼" in prompt
        assert "丹鬼缓缓摘下兜帽" in prompt, "卷宗必须真的把第6章原文塞进裁决提示词"
        return prep_pack._PrepPackTrueNameVerdictResponse(
            verdict="same", supporting_quote="丹鬼缓缓摘下兜帽，露出真容。",
        )

    monkeypatch.setattr(prep_pack.model_gateway, "chat_structured", fake_chat_structured)

    events = [_event("ev_013", characters=[
        {"display_name": "灰袍老者", "is_background_extra": False, "suspected_true_name": "丹鬼"},
    ])]
    characters, scene_list, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(
        conn, events=events,
        source_text="山顶上，灰袍老者哈哈大笑起来。",
    )

    assert errors == []
    assert stats["character_discovery_calls"] == 0
    assert verdict_calls["n"] == 1
    assert characters == [{
        "identity_id": "bible:丹鬼", "display_name": "丹鬼",
        "portrait_id": "cp-dg", "event_ids": ["ev_013"], "aliases": ["灰袍老者"],
        "visual_entity_id": "bible:丹鬼", "display_appellation": "灰袍老者",
        "provenance": {
            "method": "resolution_forward", "anchor_segments": [],
            # 第30轮 RCA 修正：anchor_phrase 记裁决钉住的支撑句本身（第29轮
            # 曾误写成空字符串），只有 anchor_segments（本地段号）合法留空。
            "anchor_phrase": "丹鬼缓缓摘下兜帽，露出真容。",
            # 第29轮：suspected_true_name 经身份绑定审判程序核验通过，但钉住
            # 的支撑句来自全书检索出的第 6 章（不是本集自己的段落）——method
            # 标注 resolution_forward，空锚合法豁免，附带裁决真正引用的章节号
            # 供审计核对（不是场景侧那批真正的空锚缺陷）。
            "forward_chapter_label": "第 6 章",
        },
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
    characters, scene_list, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(
        conn, events=events,
        source_text="靠山宗四周的山峰上，两个老者盘膝而坐。",
    )

    assert calls["n"] == 1
    assert stats["scene_discovery_calls"] == 1
    assert errors == []
    assert scene_list == [{
        "scene_id": "scene:靠山宗外围山峰", "display_name": "靠山宗外围山峰",
        "scene_reference_id": "sr-new", "event_ids": ["ev_008"],
        "provenance": {"method": "discovery", "anchor_segments": [1], "anchor_phrase": "山峰"},
    }]
    rejected = [h for h in true_name_hints if h["status"] == "rejected"]
    assert len(rejected) == 1
    assert rejected[0] == {
        "kind": "scene", "mention": "山峰", "suspected_true_name": "大青山", "status": "rejected",
        # 第29轮身份绑定审判程序：chapter 6 原文（"完全不相关的后续剧情。"）
        # 既不含"山峰"也不含"大青山"，卷宗检索（全书 chapters 扫描）结果
        # 为空——第一步卷宗检索就拦下，根本不会走到裁决那次模型调用。
        "reason": "rejected_no_dossier",
    }
    assert not any(s["scene_reference_id"] == "sr-dqs" for s in scene_list), (
        "绝不能静默沿用谱内那个不相关的「大青山」"
    )


# ---------------------------------------------------------------------------
# 1.5.0 -- speaker 名册引用化（真实 EP2 回归：关键台词"割舌头"的 speaker 被
# 写成"韩宗"，实际说话人是"绿袍男子"，韩宗第 5 章才出场，speaker 字段从未
# 进任何校验管线）。这两个函数是纯确定性查表，不需要 DB/异步，直接单测。
# ---------------------------------------------------------------------------

def test_speaker_matching_full_bible_name_absent_from_episode_roster_is_gate_blocked():
    """红灯 a（第 21 轮 ERR-20260824-34347a，韩宗形状——既有测试保持）：台词
    说话人"韩宗"是项目人物谱里真实存在的角色（character_portraits 里有此
    人，只是本集尚未出场，韩宗第 5 章才登场），本集资产名册/群演都不是他
    ——这是三分支里唯一仍然致命的一支：谱内名 + 本集无证据 = 疑似幻觉归
    属，必须门禁具名阻断，绝不能被吸收为群演静默放行。"""
    payload_events = [{
        "event_id": "ev_003",
        "key_lines": [{"speaker": "韩宗", "line": "割了他的舌头。", "segment_index": 10}],
    }]
    roster = prep_pack._prep_pack_build_speaker_roster(
        characters=[{"display_name": "绿袍男子", "identity_id": "bible:绿袍男子", "aliases": []}],
        functional_extras=[],
    )
    functional_extras: list[dict] = []
    errors, absorbed_count = prep_pack._prep_pack_resolve_key_line_speakers(
        payload_events, roster,
        all_project_character_names={"韩宗", "绿袍男子"},
        functional_extras=functional_extras,
    )
    assert any("韩宗" in message and "ev_003" in message for message in errors)
    assert "speaker_ref" not in payload_events[0]["key_lines"][0]
    assert absorbed_count == 0
    assert functional_extras == []


def test_speaker_with_zero_bible_collision_is_absorbed_as_functional_extra():
    """红灯 b（第 21 轮 ERR-20260824-34347a，"被困者"形状——新增）：台词说话
    人"被困者"跟项目人物谱（含本集之外的全部角色）零碰撞，是纯描述性的
    一次性称谓，不是幻觉归属——必须吸收进 functional_extras（label 用原文
    措辞），门禁放行、absorbed 计数=1，而不是被当成韩宗那样具名阻断。"""
    payload_events = [{
        "event_id": "ev_005",
        "key_lines": [{"speaker": "被困者", "line": "救命！", "segment_index": 12}],
    }]
    roster = prep_pack._prep_pack_build_speaker_roster(
        characters=[{"display_name": "绿袍男子", "identity_id": "bible:绿袍男子", "aliases": []}],
        functional_extras=[],
    )
    functional_extras: list[dict] = []
    errors, absorbed_count = prep_pack._prep_pack_resolve_key_line_speakers(
        payload_events, roster,
        all_project_character_names={"韩宗", "绿袍男子"},
        functional_extras=functional_extras,
    )
    assert errors == []
    assert absorbed_count == 1
    assert payload_events[0]["key_lines"][0]["speaker_ref"] == "extra:被困者"
    assert functional_extras == [{
        "label": "被困者", "event_ids": ["ev_005"],
        "visual_entity_id": "entity:e2f70bd9be906dde",
        "provenance": {
            "method": "absorbed_speaker", "anchor_segments": [12], "anchor_phrase": "救命！",
        },
    }]


def test_speaker_matching_real_character_display_name_resolves():
    """红灯 c（协调方点名 1.5.0-speaker-b）：台词说话人"绿袍男子"是本集资产
    名册里真实出场的角色（本集原文称谓），必须正确解析出 speaker_ref。"""
    payload_events = [{
        "event_id": "ev_003",
        "key_lines": [{"speaker": "绿袍男子", "line": "割了他的舌头。", "segment_index": 10}],
    }]
    roster = prep_pack._prep_pack_build_speaker_roster(
        characters=[{"display_name": "绿袍男子", "identity_id": "bible:绿袍男子", "aliases": []}],
        functional_extras=[],
    )
    errors, absorbed_count = prep_pack._prep_pack_resolve_key_line_speakers(
        payload_events, roster,
        all_project_character_names={"绿袍男子"},
        functional_extras=[],
    )
    assert errors == []
    assert absorbed_count == 0
    assert payload_events[0]["key_lines"][0]["speaker_ref"] == "bible:绿袍男子"


def test_speaker_matching_registered_alias_resolves_to_owning_character():
    """红灯 c（协调方点名 1.5.0-speaker-c）：台词说话人是名册角色的已登记别名
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
    errors, absorbed_count = prep_pack._prep_pack_resolve_key_line_speakers(
        payload_events, roster,
        all_project_character_names={"李富贵"},
        functional_extras=[],
    )
    assert errors == []
    assert absorbed_count == 0
    assert payload_events[0]["key_lines"][0]["speaker_ref"] == "bible:李富贵"


def test_speaker_matching_functional_extra_label_resolves():
    """红灯 c 补充覆盖：speaker 是群演 label（functional_extras），同样应该
    正确解析到 extra: 前缀的 speaker_ref，不应被具名阻断。"""
    payload_events = [{
        "event_id": "ev_008",
        "key_lines": [{"speaker": "山顶老者", "line": "……", "segment_index": 29}],
    }]
    roster = prep_pack._prep_pack_build_speaker_roster(
        characters=[], functional_extras=[{"label": "山顶老者", "event_ids": ["ev_008"]}],
    )
    errors, absorbed_count = prep_pack._prep_pack_resolve_key_line_speakers(
        payload_events, roster,
        all_project_character_names=set(),
        functional_extras=[{"label": "山顶老者", "event_ids": ["ev_008"]}],
    )
    assert errors == []
    assert absorbed_count == 0
    assert payload_events[0]["key_lines"][0]["speaker_ref"] == "extra:山顶老者"


# ---------------------------------------------------------------------------
# 1.5.1 -- 场景别名锚定（task①，真实第18轮审计 A2 主病灶，47 条）：场景规范
# 名（如"杂役处居所内"）是发现时铸造的标签，天然不在原文——本集若换了个
# 说法提这个场景，裸精确匹配 scene_references.scene_name 找不到它，哪怕这
# 个说法早就被登记成了该场景的别名（Bible.scenes[].aliases）也一样，因为
# 场景解析从来不读别名表。修法：读侧接上既有别名判定
# （app.validators.match_scene_name，跟 app.scenes 发现路径同一套逻辑）；
# 写侧把本集实际用到的新说法记回别名表（复用既有 app.scenes._append_scene_
# alias，幂等）。
# ---------------------------------------------------------------------------

def test_scene_alias_fallback_resolves_via_registered_alias_without_discovery():
    """红灯（task①）：场景规范名"杂役处居所内"是发现时铸造的标签，本集用的
    是早就登记过的别名"杂役们住的地方"——裸精确匹配 scene_references.
    scene_name 找不到它，必须靠别名回退（复用 app.validators.match_scene_
    name，跟 app.scenes 发现路径同一套判定）才能命中既有场景。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end) "
        "VALUES ('sr1','p1','杂役处居所内',1,NULL)"
    )
    conn.execute(
        "UPDATE projects SET bible_json=? WHERE id='p1'",
        (json.dumps({
            "characters": [],
            "scenes": [{
                "name": "杂役处居所内", "scene_canonical": "杂役居所，简陋，木床油灯",
                "aliases": ["杂役们住的地方"],
            }],
            "world": {"era": "", "genre": "", "visual_style_canonical": "测试画风"},
        }, ensure_ascii=False),),
    )
    conn.commit()

    bible = prep_pack._load_project_bible(conn, "p1")
    scene_reference_id, canonical = prep_pack._prep_pack_resolve_scene_reference_with_alias(
        conn, "p1", 2, "杂役们住的地方", bible,
    )
    assert scene_reference_id == "sr1"
    assert canonical == "杂役处居所内"


def test_scene_label_aliased_to_two_scenes_still_resolves_via_this_calls_own_verdict(
    monkeypatch,
):
    """红灯（第32/30轮真实 EP7 回归 ERR-20260824-6ecfbe 根因，直查
    run_e6bf34fa133f / run_361d35061549 的 provider_calls 原始记录确认）：
    这个测试直接调用真实的 ``app.scenes.ensure_scenes_for_labels``（不像本
    文件其它场景测试那样把它整体 mock 掉——那些测试验证的是 prep_pack 这一侧
    的调用/编排是否正确，本测试验证的是缺陷真正所在的函数本身）。

    真实数据形状：跨集历史累积（"洞府修行石室" 11:05、"南峰山脚洞府" 17:37，
    均早于两次失败的 run）导致同一个原文标签"洞府"合法地同时是两个不同规范
    场景各自登记表里的别名——app.scenes._append_scene_alias 本身没有跨场景
    排他约束，这不是数据损坏，是历史累积的合法结果。app.validators.
    match_scene_name 的"唯一胜者"要求（len(winners)==1）在这种平局下必然
    返回 None——即使 assess_new_scene 两次独立真实调用（round 30 的 call
    9371/9380、round 32 的 call 9508/9512）都干净利落、结论完全一致地裁决
    "洞府"就是"南峰山脚洞府"。旧实现在算完这份已核验的裁决后，又在
    resolved_names 计算里用 match_scene_name 反查一遍，被历史平局打死，
    "洞府"从未进入 resolved_names——最终 PrepPackGateError 报"未解析到已有
    scene_reference_id"，全部10个事件落空。"""
    conn = _make_conn()
    conn.execute("ALTER TABLE projects ADD COLUMN bible_version INTEGER")
    conn.execute("ALTER TABLE projects ADD COLUMN bible_auto_changes_json TEXT")
    conn.execute(
        "UPDATE projects SET bible_json=?, bible_version=0, bible_auto_changes_json='[]' "
        "WHERE id='p1'",
        (json.dumps({
            "characters": [],
            "scenes": [
                {
                    "name": "南峰山脚洞府",
                    "scene_canonical": "靠山宗南峰脚下的室外洞府，青石门被藤条缠绕，"
                                        "门前有两丈石台，旁边流淌着清冽山泉，白日阳光细碎洒落。",
                    "aliases": ["洞府", "南峰山脚洞府内"],
                },
                {
                    "name": "洞府修行石室",
                    "scene_canonical": "南峰洞府内部的室内石室，岩壁嵌着发光的暖光晶石，"
                                        "中心有汩汩冒灵气的半枯灵泉，地面刻着聚灵纹路。",
                    "aliases": ["洞府", "洞府内"],
                },
            ],
            "world": {"era": "", "genre": "", "visual_style_canonical": "测试画风"},
        }, ensure_ascii=False),),
    )
    conn.commit()

    # 前提校验：这就是让 match_scene_name 卡死的真实平局形状，不是我编出来的。
    from app.schemas import Bible as _Bible
    bible_row = conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()
    seeded_scenes = _Bible.model_validate(json.loads(bible_row["bible_json"])).scenes
    from app.validators import match_scene_name as _match_scene_name
    assert _match_scene_name("洞府", seeded_scenes, allow_fuzzy=False) is None, (
        "前提条件失败：历史别名平局没有复现，这个测试就没有意义"
    )

    async def fake_assess_new_scene(label, spatial_context, *, style, known_names, ep_label):
        assert label == "洞府"
        # 真实两次调用（round 30/32）的裁决完全一致：这是既有场景"南峰山脚
        # 洞府"的简称，不是新场景。
        return {
            "important": False,
            "existing_scene_name": "南峰山脚洞府",
            "reason": "剧本中的「洞府」是已有规范场景「南峰山脚洞府」的简称，属于同一地点。",
            "name": "",
            "scene_canonical": "",
            "location_kind": "",
        }

    async def fake_ensure_reactive_scene_image(project_id, scene, *, episode_no, style, bible_version):
        return {"image_path": "/fake/南峰山脚洞府.jpg", "reused": True}

    monkeypatch.setattr(scenes, "get_conn", lambda: conn)
    monkeypatch.setattr(scenes, "assess_new_scene", fake_assess_new_scene)
    monkeypatch.setattr(scenes, "_ensure_reactive_scene_image", fake_ensure_reactive_scene_image)

    result = asyncio.run(scenes.ensure_scenes_for_labels("p1", 7, ["洞府"]))

    assert result["errors"] == [], result["errors"]
    assert result["resolved_names"] == {"洞府": "南峰山脚洞府"}, result["resolved_names"]
    assert result["ready_scenes"] == ["南峰山脚洞府"]


def test_scene_alias_registration_persists_new_wording_and_is_idempotent(monkeypatch):
    """红灯（task①）：命中既有场景后，本集用到的新说法必须被记为该场景的
    新别名（复用既有 app.scenes._append_scene_alias，幂等：重复调用不重复
    写入；canonical name 本身绝不该被当成自己的别名记入）。"""
    calls: list[tuple[str, str, str]] = []

    def fake_append_scene_alias(conn, project_id, scene_name, alias):
        calls.append((project_id, scene_name, alias))
        return len(calls) == 1

    monkeypatch.setattr(scenes, "_append_scene_alias", fake_append_scene_alias)

    conn = _make_conn()
    registered_first = prep_pack._prep_pack_register_scene_alias_if_new(
        conn, "p1", canonical_name="藏经阁", wording="藏书的阁楼",
    )
    assert registered_first is True
    registered_second = prep_pack._prep_pack_register_scene_alias_if_new(
        conn, "p1", canonical_name="藏经阁", wording="藏书的阁楼",
    )
    assert registered_second is False
    assert calls == [("p1", "藏经阁", "藏书的阁楼"), ("p1", "藏经阁", "藏书的阁楼")]

    calls.clear()
    result = prep_pack._prep_pack_register_scene_alias_if_new(
        conn, "p1", canonical_name="藏经阁", wording="藏经阁",
    )
    assert result is False
    assert calls == []


# ---------------------------------------------------------------------------
# 1.5.2 -- 跨集别名一致性（task②，真实项目发现：proj_3ac0b627fa46 里"小胖子"
# 在 EP2/EP6 正确绑到李富贵，但 EP3 误绑到王有材——直查 chapters 表 EP3 原文，
# "王有材"逐字出现 0 次，"小胖子"高频出现且自始至终是同一个人）。
# ---------------------------------------------------------------------------

def test_character_alias_registry_binds_ep2_shape_alias_in_ep3_zero_discovery(
    monkeypatch,
):
    """红灯（第24轮真实回归 ERR-20260824-d0830a task①）：另一集（EP2 形状）
    已发布的 asset_manifest 里"小胖子"已经是李富贵的别名——EP3 形状的本集裸
    精确匹配失败后，必须先查这份已确立的跨集别名注册表并直接绑定，压根不
    应该触发身份消歧模型调用（不再"每集重新赌一次消歧"）。跟场景轴 1.5.1
    （_prep_pack_resolve_scene_reference_with_alias）完全对称。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-lfg','p1','李富贵',1,NULL)"
    )
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, screenplay_json) VALUES "
        "('ep-2', 'p1', 2, ?)",
        (json.dumps({
            "asset_manifest": {
                "characters": [{
                    "identity_id": "bible:李富贵", "display_name": "李富贵",
                    "portrait_id": "cp-lfg", "event_ids": ["ev_001"], "aliases": ["小胖子"],
                }],
            },
        }, ensure_ascii=False),),
    )
    conn.commit()

    def boom_character(*_a, **_k):
        raise AssertionError("跨集别名注册表命中唯一目标，不应该回炉重新消歧")

    monkeypatch.setattr(portraits, "ensure_cards_for_text", boom_character)

    events = [_event("ev_010", characters=[
        {"display_name": "小胖子", "is_background_extra": False},
    ])]
    (
        characters, scene_list, functional_extras, errors, stats,
        true_name_hints, scene_alias_anchors, rejected_alias_conflicts,
    ) = _resolve(
        conn, events=events,
        source_text="小胖子憨憨一笑，抓了抓头。",
    )

    assert stats["character_discovery_calls"] == 0, "注册表命中必须零消歧调用"
    assert errors == []
    assert rejected_alias_conflicts == []
    assert any(
        c["display_name"] == "李富贵" and c["portrait_id"] == "cp-lfg"
        and "小胖子" in c["aliases"]
        for c in characters
    )


def test_character_alias_registry_ambiguous_across_episodes_falls_back_to_discovery(
    monkeypatch,
):
    """红灯：同一个别名字符串"小胖子"在项目内被不同的已发布分集分别绑给了
    李富贵和王有材（矛盾的注册表，理论上不该发生，但注册表本身没有唯一性
    约束）——task① 的读侧必须复用 _prep_pack_cross_episode_alias_conflict
    的冲突拒绝逻辑，不猜任何一边，回退到常规发现路线（记入
    rejected_alias_conflicts 留痕）。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-lfg','p1','李富贵',1,NULL)"
    )
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-wyc','p1','王有材',1,NULL)"
    )
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, screenplay_json) VALUES "
        "('ep-2', 'p1', 2, ?)",
        (json.dumps({
            "asset_manifest": {"characters": [{
                "identity_id": "bible:李富贵", "display_name": "李富贵",
                "portrait_id": "cp-lfg", "event_ids": ["ev_001"], "aliases": ["小胖子"],
            }]},
        }, ensure_ascii=False),),
    )
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, screenplay_json) VALUES "
        "('ep-5', 'p1', 5, ?)",
        (json.dumps({
            "asset_manifest": {"characters": [{
                "identity_id": "bible:王有材", "display_name": "王有材",
                "portrait_id": "cp-wyc", "event_ids": ["ev_002"], "aliases": ["小胖子"],
            }]},
        }, ensure_ascii=False),),
    )
    conn.commit()

    async def fake_disambiguate(project_id, episode_no, source_text, bible, *, generate_portraits=True):
        return {
            "added": [], "resolutions": [], "errors": [], "warnings": [],
            "skipped": [{"status": "skipped", "name": "小胖子", "reason": "跨集别名矛盾，回炉观察"}],
        }

    monkeypatch.setattr(portraits, "ensure_cards_for_text", fake_disambiguate)
    monkeypatch.setattr(portraits, "persist_screenplay_character_resolutions", lambda *a, **k: [])

    events = [_event("ev_010", characters=[
        {"display_name": "小胖子", "is_background_extra": False},
    ])]
    (
        characters, scene_list, functional_extras, errors, stats,
        true_name_hints, scene_alias_anchors, rejected_alias_conflicts,
    ) = _resolve(
        conn, events=events,
        source_text="小胖子憨憨一笑，抓了抓头。",
    )

    assert stats["character_discovery_calls"] == 1, "矛盾注册表不能静默猜绑，必须回炉发现"
    assert not any(c["display_name"] in {"李富贵", "王有材"} for c in characters)
    assert rejected_alias_conflicts, "矛盾的跨集别名必须留痕"


# ---------------------------------------------------------------------------
# 1.7.0（docs/CHARACTER_IDENTITY_ENTITY_DESIGN.md §4.1/§6 第3项/§8 判据4）：
# 跨集别名注册表主读源切换为 Bible.characters[].aliases。上面两个测试证明
# 旧的"扫描其它已发布分集 asset_manifest"路径仍然工作（P2 §16 双重校验期
# 未退役）；下面三个测试证明新的主读源本身、以及主源与旧路径的优先级关系：
#   - 死循环的真正断点：EP1（项目内还没有任何其它分集发布过）也能查到人物
#     谱别名并直接绑定，不再要求"必须有其它已发布分集先命中过"这个前置
#     条件（真实故障：许清 EP1/EP5/EP6 三集三种措辞、全部未绑定，直到 EP13
#     才第一次绑上——根因正是旧路径这个前置条件在项目早期永远不成立）。
#   - 主源结论不可被旧路径推翻：人物谱给出明确答案时，旧路径的扫描信号
#     （哪怕存在、哪怕矛盾）不得改变结论。
#   - 人物谱自身矛盾（同一别名字符串被登记给两个不同角色）时的安全默认：
#     不确定不绑，回退到常规发现路线，行为跟旧路径的矛盾处理同构。
# ---------------------------------------------------------------------------

def test_character_alias_registry_binds_via_bible_aliases_with_zero_other_episodes(
    monkeypatch,
):
    """红灯（核心价值点）：项目内一集都还没发布过（episodes 表完全没有这个
    项目的行，旧的"扫描其它已发布分集"路径天然查无所获）——但全书分析阶段
    已经把"小胖子"登记进人物谱李富贵的 aliases。裸精确匹配失败后必须直接
    命中人物谱主读源并绑定，零消歧调用，不依赖任何其它分集先发布过。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-lfg','p1','李富贵',1,NULL)"
    )
    _seed_bible_characters(conn, "p1", [
        _bible_character("李富贵", aliases=[
            _bible_alias("小胖子", name_kind="referential", evidence_chapter_index=2,
                         evidence_quote="小胖子憨憨一笑，抓了抓头。"),
        ]),
    ])

    def boom_character(*_a, **_k):
        raise AssertionError("人物谱别名命中唯一目标，不应该回炉重新消歧")

    monkeypatch.setattr(portraits, "ensure_cards_for_text", boom_character)

    events = [_event("ev_001", characters=[
        {"display_name": "小胖子", "is_background_extra": False},
    ])]
    (
        characters, scene_list, functional_extras, errors, stats,
        true_name_hints, scene_alias_anchors, rejected_alias_conflicts,
    ) = _resolve(
        conn, events=events, episode_no=1,
        source_text="小胖子憨憨一笑，抓了抓头。",
    )

    assert stats["character_discovery_calls"] == 0, "人物谱注册表命中必须零消歧调用"
    assert errors == []
    assert rejected_alias_conflicts == []
    assert any(
        c["display_name"] == "李富贵" and c["portrait_id"] == "cp-lfg"
        and "小胖子" in c["aliases"]
        # visual_entity_id 决定取图，本集措辞"小胖子"只落进 aliases/
        # display_appellation，不提前暴露 display_name 这个规范真名。
        and c["visual_entity_id"] == "bible:李富贵"
        and c["display_appellation"] == "小胖子"
        for c in characters
    )


def test_bible_alias_conflict_check_is_not_overridden_by_legacy_scan(monkeypatch):
    """红灯（"旧路径只作补充且不得覆盖主源结论"的结构性保证）：人物谱明确
    把"小胖子"登记为李富贵一人的别名（无第二个认领者），但项目内另有一个
    已发布分集（旧路径的数据源）把同一个字符串绑给了王有材——如果旧路径能
    推翻主源，这里就会被误判为冲突、回退到发现；正确行为是完全信任人物谱
    的"无冲突"结论，直接绑定李富贵，零消歧调用。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-lfg','p1','李富贵',1,NULL)"
    )
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-wyc','p1','王有材',1,NULL)"
    )
    _seed_bible_characters(conn, "p1", [
        _bible_character("李富贵", aliases=[_bible_alias("小胖子")]),
    ])
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, screenplay_json) VALUES "
        "('ep-5', 'p1', 5, ?)",
        (json.dumps({
            "asset_manifest": {"characters": [{
                "identity_id": "bible:王有材", "display_name": "王有材",
                "portrait_id": "cp-wyc", "event_ids": ["ev_002"], "aliases": ["小胖子"],
            }]},
        }, ensure_ascii=False),),
    )
    conn.commit()

    def boom_character(*_a, **_k):
        raise AssertionError("人物谱已给出明确无冲突结论，不应该回炉重新消歧")

    monkeypatch.setattr(portraits, "ensure_cards_for_text", boom_character)

    events = [_event("ev_010", characters=[
        {"display_name": "小胖子", "is_background_extra": False},
    ])]
    (
        characters, scene_list, functional_extras, errors, stats,
        true_name_hints, scene_alias_anchors, rejected_alias_conflicts,
    ) = _resolve(
        conn, events=events,
        source_text="小胖子憨憨一笑，抓了抓头。",
    )

    assert stats["character_discovery_calls"] == 0
    assert errors == []
    assert rejected_alias_conflicts == [], (
        "人物谱的无冲突结论不能被旧路径的扫描信号推翻"
    )
    assert any(c["display_name"] == "李富贵" and c["portrait_id"] == "cp-lfg" for c in characters)
    assert not any(c["display_name"] == "王有材" for c in characters)


def test_bible_alias_ambiguous_across_characters_falls_back_to_discovery(monkeypatch):
    """红灯：人物谱自身把同一个别名字符串"小胖子"登记给了两个不同角色
    （数据质量异常，理论上不该发生，但 Character.aliases 之间没有跨角色
    唯一性约束）——必须判定为冲突、不确定不绑，回退到常规发现路线，跟旧
    路径矛盾时的安全默认同构。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-lfg','p1','李富贵',1,NULL)"
    )
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-wyc','p1','王有材',1,NULL)"
    )
    _seed_bible_characters(conn, "p1", [
        _bible_character("李富贵", aliases=[_bible_alias("小胖子")]),
        _bible_character("王有材", aliases=[_bible_alias("小胖子")]),
    ])

    async def fake_disambiguate(project_id, episode_no, source_text, bible, *, generate_portraits=True):
        return {
            "added": [], "resolutions": [], "errors": [], "warnings": [],
            "skipped": [{"status": "skipped", "name": "小胖子", "reason": "人物谱别名矛盾，回炉观察"}],
        }

    monkeypatch.setattr(portraits, "ensure_cards_for_text", fake_disambiguate)
    monkeypatch.setattr(portraits, "persist_screenplay_character_resolutions", lambda *a, **k: [])

    events = [_event("ev_010", characters=[
        {"display_name": "小胖子", "is_background_extra": False},
    ])]
    (
        characters, scene_list, functional_extras, errors, stats,
        true_name_hints, scene_alias_anchors, rejected_alias_conflicts,
    ) = _resolve(
        conn, events=events,
        source_text="小胖子憨憨一笑，抓了抓头。",
    )

    assert stats["character_discovery_calls"] == 1, "人物谱矛盾不能静默猜绑，必须回炉发现"
    assert not any(c["display_name"] in {"李富贵", "王有材"} for c in characters)
    assert rejected_alias_conflicts, "人物谱内部的矛盾别名必须留痕"


# ---------------------------------------------------------------------------
# 退化重试护栏（第23轮真实回归 ERR-20260824-7ab7cb「本集未抽取到任何事件」）：
# 真实 EP3 事故——尝试1 的事件链抽取拿到 13 个真实事件，一路通过跨度账本/
# 覆盖完整性/资产映射，只在后面某道门禁（如 hook/cliffhanger 接地）被拒；
# 尝试2 重新抽取事件链时，模型的原始 JSON 中途结构缺失，格式修复重试拿到
# 的候选又被 app.harness.model_gateway._latest_json_authority_root 误判成
# 一个无意义的嵌套片段，模型据此"忠实"地把 events 修回空列表——事件链整个
# 退化为零。旧逻辑里 run_episode_prep_pack 的 attempt_hint/last_error 每轮
# 无条件覆盖，尝试2 的"本集未抽取到任何事件"就这样悄悄盖掉了尝试1 更有
# 信息量的失败原因。护栏：本运行内任何一次尝试抽到过事件后，后续退化为
# 零事件的尝试不得被静默采纳，必须把两次失败原因合并成一条具名错误；只有
# 全部尝试都是零事件，才维持原始的"本集未抽取到任何事件"作为终态。
# ---------------------------------------------------------------------------

def test_degraded_empty_retry_after_nonempty_attempt_is_rejected_not_adopted(
    monkeypatch,
) -> None:
    """红灯（真实第23轮 EP3 回归 ERR-20260824-7ab7cb）：尝试1 抽到了事件、
    只是被别的门禁拒绝；尝试2 事件链退化为空——最终报出的错误必须点名
    "这是一次退化重试"并保留尝试1 的失败原因，不能是裸的"本集未抽取到
    任何事件"（那会让人误以为这一集从头到尾就没抽到过事件）。"""
    calls: list[int] = []

    async def fake_generate_once(**_kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise prep_pack.PrepPackGateError(
                "hook「...」与其接地事件及相邻事件的文本重合度过低（0.120 < 0.300），疑似编造",
                had_events=True,
            )
        raise prep_pack.PrepPackGateError("本集未抽取到任何事件", had_events=False)

    monkeypatch.setattr(prep_pack, "_generate_prep_pack_once", fake_generate_once)

    with pytest.raises(prep_pack.PrepPackGateError) as excinfo:
        asyncio.run(prep_pack.run_episode_prep_pack(
            episode_id="ep_test", episode={"project_id": "proj1", "episode_no": 3},
            source_text="占位原文", run_id=None,
        ))

    message = str(excinfo.value)
    assert len(calls) == 2
    assert "退化" in message
    assert "疑似编造" in message, "尝试1 的失败原因不能被尝试2 的空事件失败悄悄吞掉"
    assert excinfo.value.had_events is False


def test_empty_retry_terminal_error_stays_plain_when_every_attempt_was_empty(
    monkeypatch,
) -> None:
    """绿灯对照：如果本运行内每一次尝试都是零事件（从来没有一次真的抽到过
    事件），最终报出的错误必须维持原始的"本集未抽取到任何事件"——不应该
    被误套上"退化重试"的框架（没有"退化"可言，只是这一集确实没有事件）。
    """
    calls: list[int] = []

    async def fake_generate_once(**_kwargs):
        calls.append(1)
        raise prep_pack.PrepPackGateError("本集未抽取到任何事件", had_events=False)

    monkeypatch.setattr(prep_pack, "_generate_prep_pack_once", fake_generate_once)

    with pytest.raises(prep_pack.PrepPackGateError) as excinfo:
        asyncio.run(prep_pack.run_episode_prep_pack(
            episode_id="ep_test", episode={"project_id": "proj1", "episode_no": 3},
            source_text="占位原文", run_id=None,
        ))

    message = str(excinfo.value)
    assert len(calls) == 2
    assert message == "本集未抽取到任何事件"
    assert "退化" not in message


# ---------------------------------------------------------------------------
# 组合式描述称谓的证据语义（第24轮真实回归 ERR-20260824-d0830a task②）：
# 「穿杂役衫的魁梧大汉」经消歧正确解析到赵武刚，却被称谓证据闸拦下——原文
# 对这个人只有分散的描述性叙述（"穿着杂役衫的魁梧大汉"分几处提到"大汉"/
# "杂役衫"，从未拼成"穿杂役衫的魁梧大汉"这个连续短语），这是模型综合出的
# 名词短语，天然不可能逐字命中，不是幻觉归属的形状。裸直接命中（丹鬼形状，
# 见 test_ep5_hallucinated_character_bind_with_no_text_evidence_is_gate_
# blocked，未受本次改动影响，继续保持红灯）反幻觉主防线不动；经消歧解析
# 绑定的合法性改由解析步骤自身的证据链承担。别名注册仍只登记逐字出现于
# 原文的称谓——组合短语不进别名库，防止注册表被合成词污染。
# ---------------------------------------------------------------------------

def test_composite_description_resolved_via_discovery_bypasses_literal_gate(
    monkeypatch,
):
    """红灯 b：「穿杂役衫的魁梧大汉」经身份消歧正确解析到赵武刚——称谓证据闸
    必须放行（合法性由消歧步骤自身的证据链承担），但这个合成短语绝不能进
    aliases（别名库只登记逐字出现于原文的称谓）。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-zwg','p1','赵武刚',1,NULL)"
    )
    conn.commit()

    async def fake_disambiguate(project_id, episode_no, source_text, bible, *, generate_portraits=True):
        return {
            "added": [], "skipped": [],
            "resolutions": [{
                "source_label": "穿杂役衫的魁梧大汉", "canonical_name": "赵武刚",
                "resolution": "future_identity",
            }],
            "errors": [], "warnings": [],
        }

    monkeypatch.setattr(portraits, "ensure_cards_for_text", fake_disambiguate)
    monkeypatch.setattr(portraits, "persist_screenplay_character_resolutions", lambda *a, **k: [])

    events = [_event("ev_001", characters=[
        {"display_name": "穿杂役衫的魁梧大汉", "is_background_extra": False},
    ])]
    (
        characters, scene_list, functional_extras, errors, stats,
        true_name_hints, scene_alias_anchors, rejected_alias_conflicts,
    ) = _resolve(
        conn, events=events,
        # 原文只有分散的描述，从未拼成模型综合出的这个连续短语。
        source_text="一个穿着杂役衫的魁梧男子闯了进来，凶狠地看了众人一眼。",
    )

    assert errors == []
    assert any(
        c["display_name"] == "赵武刚" and c["portrait_id"] == "cp-zwg"
        for c in characters
    )
    zwg = next(c for c in characters if c["display_name"] == "赵武刚")
    assert "穿杂役衫的魁梧大汉" not in zwg["aliases"], (
        "合成的组合描述短语不能进别名库，防止注册表被污染"
    )


def test_composite_description_discovery_failure_still_gate_blocked(monkeypatch):
    """红灯 c：「穿杂役衫的魁梧大汉」这类组合描述称谓，一旦消歧本身明确失败
    （不是"没有意见"），照旧门禁具名拦截——语义精化只豁免逐字证据要求，不
    豁免消歧失败本身。"""
    conn = _make_conn()

    async def failing_discovery(project_id, episode_no, source_text, bible, *, generate_portraits=True):
        return {
            "added": [], "resolutions": [], "skipped": [], "warnings": [],
            "errors": ["穿杂役衫的魁梧大汉：身份模型已确认真名，但人物卡模型未返回完整稳定卡片"],
        }

    monkeypatch.setattr(portraits, "ensure_cards_for_text", failing_discovery)
    monkeypatch.setattr(portraits, "persist_screenplay_character_resolutions", lambda *a, **k: [])

    events = [_event("ev_001", characters=[
        {"display_name": "穿杂役衫的魁梧大汉", "is_background_extra": False},
    ])]
    (
        characters, scene_list, functional_extras, errors, stats,
        true_name_hints, scene_alias_anchors, rejected_alias_conflicts,
    ) = _resolve(
        conn, events=events,
        source_text="一个穿着杂役衫的魁梧男子闯了进来，凶狠地看了众人一眼。",
    )

    assert characters == []
    assert functional_extras == [], "消歧明确失败的称谓不能被兜底成 functional_extras"
    assert any("穿杂役衫的魁梧大汉" in message for message in errors)


# ---------------------------------------------------------------------------
# manifest 绑定来源证明（provenance，1.6.0，第25轮收口指令）：审计剩余83条
# 定性为"合成标签合法但不可审计"——1.5.x 各轮陆续放宽了字面锚定要求，但
# "这次绑定为什么合法"的依据只留在 Evaluation.evidence 里，不是 payload 的
# 一等公民。asset_manifest.characters[]/scenes[]/functional_extras[] 每项
# 新增 provenance: {method, anchor_segments, anchor_phrase}；
# event_chain[].key_lines[] 每条新增 speaker_provenance（协调方形状对齐
# 指令的字段名）。发布前 _prep_pack_verify_manifest_provenance 自校验一遍：
# 非空 anchor_phrase 必须逐字命中它自己 anchor_segments 指向的原文段。
# ---------------------------------------------------------------------------

def _provenance_self_verify(source_text, characters, scene_list, functional_extras):
    segments = prep_pack.index_source_segments(source_text)
    return prep_pack._prep_pack_verify_manifest_provenance(
        segments,
        {"characters": characters, "scenes": scene_list, "functional_extras": functional_extras},
    )


def test_provenance_direct_method_self_verifies():
    """红灯 a（method=direct）：裸命中角色的 provenance 是
    {method:"direct", anchor_segments:[段号], anchor_phrase:称谓}，自校验
    通过。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp1','p1','萧炎',1,NULL)"
    )
    conn.commit()
    source_text = "萧炎快步穿过宗门广场，众弟子纷纷让路。"
    events = [_event("ev_001", characters=[
        {"display_name": "萧炎", "is_background_extra": False},
    ])]
    characters, scene_list, functional_extras, errors, stats, *_ = _resolve(
        conn, events=events, source_text=source_text,
    )
    assert errors == []
    xiao_yan = next(c for c in characters if c["display_name"] == "萧炎")
    assert xiao_yan["provenance"] == {
        "method": "direct", "anchor_segments": [1], "anchor_phrase": "萧炎",
    }
    assert _provenance_self_verify(source_text, characters, scene_list, functional_extras) == []


def test_provenance_alias_method_self_verifies(monkeypatch):
    """红灯 a（method=alias）：EP2 形状先例存在，EP3 形状经跨集别名注册表
    命中绑定——provenance 是 {method:"alias", anchor_segments:[段号],
    anchor_phrase:"小胖子"}，自校验通过（跟第24轮 task① 同一场景）。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-lfg','p1','李富贵',1,NULL)"
    )
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, screenplay_json) VALUES "
        "('ep-2', 'p1', 2, ?)",
        (json.dumps({
            "asset_manifest": {"characters": [{
                "identity_id": "bible:李富贵", "display_name": "李富贵",
                "portrait_id": "cp-lfg", "event_ids": ["ev_001"], "aliases": ["小胖子"],
            }]},
        }, ensure_ascii=False),),
    )
    conn.commit()

    def boom_character(*_a, **_k):
        raise AssertionError("跨集别名注册表命中唯一目标，不应该回炉重新消歧")

    monkeypatch.setattr(portraits, "ensure_cards_for_text", boom_character)

    source_text = "小胖子憨憨一笑，抓了抓头。"
    events = [_event("ev_010", characters=[
        {"display_name": "小胖子", "is_background_extra": False},
    ])]
    characters, scene_list, functional_extras, errors, stats, *_ = _resolve(
        conn, events=events, source_text=source_text,
    )
    assert errors == []
    lfg = next(c for c in characters if c["display_name"] == "李富贵")
    assert lfg["provenance"] == {
        "method": "alias", "anchor_segments": [1], "anchor_phrase": "小胖子",
    }
    assert _provenance_self_verify(source_text, characters, scene_list, functional_extras) == []


def test_provenance_scene_alias_hit_with_event_evidence_upgrades_to_resolution():
    """红灯 a（第30轮②，真实 scripts/episode_source_audit.py 复核实测：19 条
    A2_scene_no_text_evidence，全部 provenance.method=alias、canonical
    display_name 无一逐字出现在本集原文，但该场景所涉事件的 source_evidence
    地点描述短语 19/19 逐字命中）：跨集别名命中的场景，若该场景所涉事件的
    source_evidence 里有独立于"这个别名本身"之外的地点描述短语逐字出现，
    这才是真正有信息量的证据（不是"这个别名确实这么写"的同义反复）——
    provenance.method 升级为 resolution（走锚点核验标准，审计脚本对
    resolution 用 ANCHOR_VERIFIED，不像 alias 那样要求 display_name 本身
    逐字出现），anchor_phrase 记这句独立证据。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end) "
        "VALUES ('sr1','p1','杂役处居所内',1,NULL)"
    )
    conn.execute(
        "UPDATE projects SET bible_json=? WHERE id='p1'",
        (json.dumps({
            "characters": [],
            "scenes": [{
                "name": "杂役处居所内", "scene_canonical": "杂役居所，简陋，木床油灯",
                "aliases": ["杂役们住的地方"],
            }],
            "world": {"era": "", "genre": "", "visual_style_canonical": "测试画风"},
        }, ensure_ascii=False),),
    )
    conn.commit()

    source_text = "杂役们住的地方，简陋异常，木床吱呀作响。"
    events = [{
        "event_id": "ev_020",
        "characters": [],
        "scenes": [{"display_name": "杂役们住的地方"}],
        "source_evidence": [{"segment_index": 1, "quote": "杂役们住的地方，简陋异常"}],
    }]
    characters, scene_list, functional_extras, errors, stats, *_ = _resolve(
        conn, events=events, source_text=source_text,
    )
    assert errors == []
    entry = next(s for s in scene_list if s["scene_reference_id"] == "sr1")
    assert entry["display_name"] == "杂役处居所内"
    assert entry["provenance"] == {
        "method": "resolution", "anchor_segments": [1],
        "anchor_phrase": "杂役们住的地方，简陋异常",
    }
    assert _provenance_self_verify(source_text, characters, scene_list, functional_extras) == []


def test_provenance_scene_alias_hit_with_no_independent_evidence_becomes_alias_inherited():
    """红灯 b（第30轮②的兜底半支）：跨集别名命中的场景，规范名和该场景所涉
    事件的地点描述短语都没有独立证据（本集只用到了这个别名字符串本身，跟
    "只体现命中了注册表" 的问题形状完全一致）——不伪造锚点，method 诚实标
    alias_inherited，source_episode_no 取这个场景参考最初在注册表里生效的
    集号（scene_references.ep_start，现成数据），供审计走对应的递归核验
    分支。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end) "
        "VALUES ('sr2','p1','藏经阁',3,NULL)"
    )
    conn.execute(
        "UPDATE projects SET bible_json=? WHERE id='p1'",
        (json.dumps({
            "characters": [],
            "scenes": [{
                "name": "藏经阁", "scene_canonical": "藏书楼，古朴",
                "aliases": ["藏书的阁楼"],
            }],
            "world": {"era": "", "genre": "", "visual_style_canonical": "测试画风"},
        }, ensure_ascii=False),),
    )
    conn.commit()

    source_text = "藏书的阁楼里堆满了竹简。"
    events = [{
        "event_id": "ev_021",
        "characters": [],
        "scenes": [{"display_name": "藏书的阁楼"}],
    }]
    characters, scene_list, functional_extras, errors, stats, *_ = _resolve(
        conn, events=events, source_text=source_text, episode_no=5,
    )
    assert errors == []
    entry = next(s for s in scene_list if s["scene_reference_id"] == "sr2")
    assert entry["display_name"] == "藏经阁"
    assert entry["provenance"] == {
        "method": "alias_inherited", "anchor_segments": [], "anchor_phrase": "",
        "source_episode_no": 3,
    }
    # alias_inherited 空锚是合法豁免，不需要参与逐字自校验。
    assert _provenance_self_verify(source_text, characters, scene_list, functional_extras) == []


def test_provenance_resolution_method_self_verifies(monkeypatch):
    """红灯 a（method=resolution）：身份消歧把称谓改绑到已有真名，
    provenance 是 {method:"resolution", anchor_segments:[段号],
    anchor_phrase:原始称谓}，自校验通过。"""
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
    source_text = "一名神秘老者悄然现身，气息深不可测。"
    events = [_event("ev_001", characters=[
        {"display_name": "神秘老者", "is_background_extra": False},
    ])]
    characters, scene_list, functional_extras, errors, stats, *_ = _resolve(
        conn, events=events, source_text=source_text,
    )
    assert errors == []
    cangxuan = next(c for c in characters if c["display_name"] == "苍玄")
    assert cangxuan["provenance"] == {
        "method": "resolution", "anchor_segments": [1], "anchor_phrase": "神秘老者",
    }
    assert _provenance_self_verify(source_text, characters, scene_list, functional_extras) == []


def test_provenance_discovery_method_self_verifies(monkeypatch):
    """红灯 a（method=discovery）：发现本次新建了一张人物卡，provenance 是
    {method:"discovery", anchor_segments:[段号], anchor_phrase:触发发现的
    原始描述}，自校验通过。"""
    conn = _make_conn()

    async def fake_ensure_cards_for_text(project_id, episode_no, source_text, bible, *, generate_portraits=True):
        conn.execute(
            "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
            "VALUES ('cp-new','p1','沈青梧',2,NULL)"
        )
        conn.commit()
        return {"added": [{"name": "沈青梧"}], "resolutions": [], "errors": [], "skipped": [], "warnings": []}

    monkeypatch.setattr(portraits, "ensure_cards_for_text", fake_ensure_cards_for_text)
    monkeypatch.setattr(portraits, "persist_screenplay_character_resolutions", lambda *a, **k: [])
    source_text = "沈青梧提剑而立，目光冷冽。"
    events = [_event("ev_001", characters=[
        {"display_name": "沈青梧", "is_background_extra": False},
    ])]
    characters, scene_list, functional_extras, errors, stats, *_ = _resolve(
        conn, events=events, source_text=source_text,
    )
    assert errors == []
    shen = next(c for c in characters if c["display_name"] == "沈青梧")
    assert shen["provenance"] == {
        "method": "discovery", "anchor_segments": [1], "anchor_phrase": "沈青梧",
    }
    assert _provenance_self_verify(source_text, characters, scene_list, functional_extras) == []


def test_provenance_absorbed_speaker_method_self_verifies_and_propagates_to_key_line():
    """红灯 a（method=absorbed_speaker）：台词说话人被吸收为群演时，
    functional_extras 条目和它触发的那条 key_line 的 speaker_provenance
    共用同一份 provenance；anchor_segments 是"台词所在事件的证据段"（协调方
    原话），自校验通过。"""
    payload_events = [{
        "event_id": "ev_005",
        "source_evidence": [{"segment_index": 11, "quote": "山洞里传来求救声。"}],
        "key_lines": [{"speaker": "被困者", "line": "救命！", "segment_index": 12}],
    }]
    roster = prep_pack._prep_pack_build_speaker_roster(
        characters=[{"display_name": "绿袍男子", "identity_id": "bible:绿袍男子", "aliases": []}],
        functional_extras=[],
    )
    functional_extras: list[dict] = []
    errors, absorbed_count = prep_pack._prep_pack_resolve_key_line_speakers(
        payload_events, roster,
        all_project_character_names={"韩宗", "绿袍男子"},
        functional_extras=functional_extras,
    )
    assert errors == []
    assert absorbed_count == 1
    extra = next(e for e in functional_extras if e["label"] == "被困者")
    assert extra["provenance"] == {
        "method": "absorbed_speaker",
        "anchor_segments": [11, 12],
        "anchor_phrase": "救命！",
    }
    key_line = payload_events[0]["key_lines"][0]
    assert key_line["speaker_provenance"] == extra["provenance"]

    source_text = "\n\n".join(
        [f"占位段落{i}。" for i in range(1, 11)] + ["山洞里传来求救声。", "救命！"],
    )
    segments = prep_pack.index_source_segments(source_text)
    assert segments[10].text == "山洞里传来求救声。"
    assert segments[11].text == "救命！"
    verify_errors = prep_pack._prep_pack_verify_manifest_provenance(
        segments, {"characters": [], "scenes": [], "functional_extras": functional_extras},
    )
    assert verify_errors == []


def test_provenance_discovery_extra_speaker_backfills_empty_anchor_from_key_line():
    """红灯（第30轮①，真实 scripts/episode_source_audit.py 复核实测：31 条
    A3_speaker_anchor_invalid，全部 provenance.method=discovery、
    anchor_segments 为空）：discovery 类群演（角色发现判定 skip 落地时创建
    的 functional_extra，触发发现的原始描述短语未必逐字出现在原文，见角色
    循环 extra_anchor_phrase 的取法，anchor 天然可能是空的）这次真开口说了
    台词——回填锚点跟 absorbed_speaker 同一套证据源：台词所在事件的
    source_evidence 段号 ∪ 这条台词自己的 segment_index。回填是就地修改
    functional_extras 里那条共享的 provenance 字典，manifest 自身
    （functional_extras[].provenance）与 key_line.speaker_provenance 必须
    同步拿到锚点，不是只改 speaker 这一侧。"""
    payload_events = [{
        "event_id": "ev_004",
        "source_evidence": [{"segment_index": 8, "quote": "围观弟子纷纷议论起来。"}],
        "key_lines": [{"speaker": "围观弟子", "line": "这下有好戏看了！", "segment_index": 9}],
    }]
    roster = prep_pack._prep_pack_build_speaker_roster(
        characters=[], functional_extras=[{"label": "围观弟子", "event_ids": ["ev_002"]}],
    )
    # discovery 落地时锚点搜索失败的真实形状：anchor_segments/anchor_phrase
    # 均为空，method=discovery（不是 absorbed_speaker——这条群演不是本函数
    # 吸收出来的，是角色发现那条路径更早创建好、这里只是复用）。
    functional_extras = [{
        "label": "围观弟子", "event_ids": ["ev_002"],
        "provenance": {"method": "discovery", "anchor_segments": [], "anchor_phrase": ""},
    }]
    errors, absorbed_count = prep_pack._prep_pack_resolve_key_line_speakers(
        payload_events, roster,
        all_project_character_names=set(),
        functional_extras=functional_extras,
    )
    assert errors == []
    assert absorbed_count == 0, "复用既有群演不是新吸收，absorbed 计数不应变化"
    extra = next(e for e in functional_extras if e["label"] == "围观弟子")
    assert extra["provenance"] == {
        "method": "discovery",
        "anchor_segments": [8, 9],
        "anchor_phrase": "这下有好戏看了！",
    }
    key_line = payload_events[0]["key_lines"][0]
    assert key_line["speaker_provenance"] == extra["provenance"]

    source_text = "\n\n".join(
        [f"占位段落{i}。" for i in range(1, 8)]
        + ["围观弟子纷纷议论起来。", "这下有好戏看了！"],
    )
    segments = prep_pack.index_source_segments(source_text)
    assert segments[7].text == "围观弟子纷纷议论起来。"
    assert segments[8].text == "这下有好戏看了！"
    verify_errors = prep_pack._prep_pack_verify_manifest_provenance(
        segments, {"characters": [], "scenes": [], "functional_extras": functional_extras},
    )
    assert verify_errors == []


def test_provenance_discovery_extra_speaker_with_no_recoverable_evidence_stays_empty():
    """回填只在真有证据可用时才做——事件没有 source_evidence、台词自己也没
    segment_index 时，fallback_segments 是空集合，provenance 原样保留空锚
    （不伪造锚点），跟第29轮"默认安全侧"是同一条纪律。"""
    payload_events = [{
        "event_id": "ev_099",
        "key_lines": [{"speaker": "围观弟子", "line": "……"}],
    }]
    roster = prep_pack._prep_pack_build_speaker_roster(
        characters=[], functional_extras=[{"label": "围观弟子", "event_ids": ["ev_002"]}],
    )
    functional_extras = [{
        "label": "围观弟子", "event_ids": ["ev_002"],
        "provenance": {"method": "discovery", "anchor_segments": [], "anchor_phrase": ""},
    }]
    errors, absorbed_count = prep_pack._prep_pack_resolve_key_line_speakers(
        payload_events, roster,
        all_project_character_names=set(),
        functional_extras=functional_extras,
    )
    assert errors == []
    extra = next(e for e in functional_extras if e["label"] == "围观弟子")
    assert extra["provenance"] == {
        "method": "discovery", "anchor_segments": [], "anchor_phrase": "",
    }


def test_provenance_anchor_mismatch_blocks_publish():
    """红灯 b：anchor_phrase 不在 anchor_segments 指定的原文段里——自校验
    必须失败并具名报出，供 _generate_prep_pack_once 在发布前门禁拦截。"""
    source_text = "萧炎快步穿过宗门广场，众弟子纷纷让路。"
    segments = prep_pack.index_source_segments(source_text)
    tampered_characters = [{
        "identity_id": "bible:萧炎", "display_name": "萧炎",
        "portrait_id": "cp1", "event_ids": ["ev_001"], "aliases": [],
        "provenance": {
            "method": "direct", "anchor_segments": [1], "anchor_phrase": "根本不存在的短语",
        },
    }]
    errors = prep_pack._prep_pack_verify_manifest_provenance(
        segments, {"characters": tampered_characters, "scenes": [], "functional_extras": []},
    )
    assert errors, "anchor_phrase 未逐字命中所指段落必须门禁拦截"
    assert any("萧炎" in message and "根本不存在的短语" in message for message in errors)


def test_provenance_resolution_forward_half_certificate_blocks_publish():
    """红灯（第30轮 RCA，真实 EP2/6/8/9 回归：resolution_forward 空
    forward_chapter_label/空 anchor_phrase）：两个字段任一为空都是"半张
    证书"，跟空锚同待遇，必须门禁拦截，不允许发布——
    a) forward_chapter_label 丢失（真实 EP2/6/8「李富贵」形状：
       _prep_pack_verify_true_name_hypothesis 从未真正跑过，
       true_name_pinned_chapter_idx 停在 None）；
    b) anchor_phrase 丢失（真实 EP9「南峰山脚洞府」形状：forward_
       chapter_label 有效，但 anchor_phrase 被误写成空字符串）；
    c) 两个字段都非空——完整证书，自校验放行，不再要求 anchor_segments
       本地命中（那套校验对 resolution_forward 从语义上就不适用）。"""
    source_text = "占位原文。"
    segments = prep_pack.index_source_segments(source_text)

    missing_label = [{
        "identity_id": "bible:李富贵", "display_name": "李富贵",
        "portrait_id": "cp-lfg", "event_ids": ["ev_001"], "aliases": ["小胖子"],
        "provenance": {
            "method": "resolution_forward", "anchor_segments": [],
            "anchor_phrase": "他是当年的小胖子，李富贵。",
        },
    }]
    errors_a = prep_pack._prep_pack_verify_manifest_provenance(
        segments, {"characters": missing_label, "scenes": [], "functional_extras": []},
    )
    assert errors_a, "forward_chapter_label 缺失必须门禁拦截"
    assert any("李富贵" in message and "forward_chapter_label" in message for message in errors_a)

    missing_phrase = [{
        "scene_id": "scene:南峰山脚洞府", "display_name": "南峰山脚洞府",
        "scene_reference_id": "sr-nf", "event_ids": ["ev_001"],
        "provenance": {
            "method": "resolution_forward", "anchor_segments": [],
            "anchor_phrase": "", "forward_chapter_label": "第 5 章",
        },
    }]
    errors_b = prep_pack._prep_pack_verify_manifest_provenance(
        segments, {"characters": [], "scenes": missing_phrase, "functional_extras": []},
    )
    assert errors_b, "anchor_phrase 缺失同样是半张证书，必须门禁拦截"
    assert any("南峰山脚洞府" in message and "anchor_phrase" in message for message in errors_b)

    complete = [{
        "identity_id": "bible:李富贵", "display_name": "李富贵",
        "portrait_id": "cp-lfg", "event_ids": ["ev_001"], "aliases": ["小胖子"],
        "provenance": {
            "method": "resolution_forward", "anchor_segments": [],
            "anchor_phrase": "他是当年的小胖子，李富贵。",
            "forward_chapter_label": "第 692 章",
        },
    }]
    errors_c = prep_pack._prep_pack_verify_manifest_provenance(
        segments, {"characters": complete, "scenes": [], "functional_extras": []},
    )
    assert errors_c == [], "两个字段齐全的完整证书必须放行"


def test_provenance_missing_field_on_legacy_manifest_does_not_crash():
    """红灯 c（前端兼容/旧包兼容）：provenance 是新增可选字段——完全没有这
    个字段的旧 manifest（1.5.x 之前发布的包）传进自校验，必须优雅跳过，不
    崩溃、不报错，payload 冻结纪律没有被打破。"""
    source_text = "占位原文。"
    segments = prep_pack.index_source_segments(source_text)
    legacy_manifest = {
        "characters": [{
            "identity_id": "bible:萧炎", "display_name": "萧炎",
            "portrait_id": "cp1", "event_ids": ["ev_001"], "aliases": [],
        }],
        "scenes": [{
            "scene_id": "scene:宗门广场", "display_name": "宗门广场",
            "scene_reference_id": "sr1", "event_ids": ["ev_001"],
        }],
        "functional_extras": [{"label": "路人", "event_ids": ["ev_001"]}],
    }
    errors = prep_pack._prep_pack_verify_manifest_provenance(segments, legacy_manifest)
    assert errors == []


# ---------------------------------------------------------------------------
# 第28轮 ERR-20260824（v3 审计拆出的第二病灶排查）：锚失效疑似"分块集里
# provenance 记的是 chunk 内局部段号，审计用 index_source_segments 全局
# 段号"编号域错位。取证：人工比对 EP1-10 全部真实已发布 1.6.0 产物（用
# 真实 chapters 原文重算 index_source_segments，逐条核对每一条非空
# anchor_phrase 是否命中它自己 anchor_segments 所指的原文）——零个不一致，
# 且这批真实章节全部单 chunk（_CHUNK_MAX_CHARS=6000，最长的 EP3 才 4808
# 字），没有任何一集真的触发过跨 chunk 场景，无法用真实数据证实/证伪这个
# 假设。静态追踪确认：_chunk_segments 用 list(enumerate(segments, start=1))
# 分组，不做块内重新编号；_render_chunk 展示给模型的编号、event 的
# source_span/source_evidence[].segment_index 因此全程都是全局域，跟
# provenance/coverage_ledger 同一份 segments 闭包变量——沿链路排查未发现
# 第二套局部编号并存。结论：当前代码库没有坐实这个假设（真实数据规模不足以
# 触发，静态代码追踪也没找到局部重编号的位置），但风险本身成立（是"结构上
# 目前不会发生"而不是"结构上不可能发生"）——补一条跨 chunk 构造夹具的红灯
# 作为这条不变量的回归防线，而不是不做任何验证就当作已解决。
# ---------------------------------------------------------------------------

def test_chunk_segments_preserve_global_indices_across_multiple_chunks():
    """红灯：source_text 长度超过 _CHUNK_MAX_CHARS，强制切成多个 chunk——
    第二个 chunk 的段号必须紧接第一个 chunk 的末尾，不能在块内从 1 重新
    编号；用全局 segments 对跨入第二个 chunk 的锚点做自校验必须通过，
    证明 provenance 与 coverage_ledger/source_span 全程共用同一个全局
    段号域，不存在局部/全局两套编号并存的情形。"""
    paragraphs = [
        f"占位段落{i}，凑够长度用于触发多个 chunk 的切分边界测试用途。"
        for i in range(1, 260)
    ]
    source_text = "\n\n".join(paragraphs)
    segments = prep_pack.index_source_segments(source_text)
    chunks = prep_pack._chunk_segments(segments)
    assert len(chunks) >= 2, "夹具必须真的触发多 chunk 切分才有意义"

    first_chunk_indexes = [index for index, _segment in chunks[0]]
    second_chunk_indexes = [index for index, _segment in chunks[1]]
    assert second_chunk_indexes[0] == first_chunk_indexes[-1] + 1, (
        "第二个 chunk 的段号必须紧接第一个 chunk，不能块内重新从 1 编号"
    )

    late_index = second_chunk_indexes[len(second_chunk_indexes) // 2]
    late_text = segments[late_index - 1].text
    manifest = {
        "characters": [{
            "display_name": "占位角色",
            "provenance": {
                "method": "direct", "anchor_segments": [late_index],
                "anchor_phrase": late_text,
            },
        }],
        "scenes": [], "functional_extras": [],
    }
    assert prep_pack._prep_pack_verify_manifest_provenance(segments, manifest) == []


# ---------------------------------------------------------------------------
# 第29轮：身份绑定审判程序（"卷宗检索→裁决→钉证→记账"，见
# app.production.prep_pack._prep_pack_verify_true_name_hypothesis 上方
# 完整说明）。协调方两次否决了结构规则式路线（列举反证、包含方向性规则）
# ——那是"穿着语法外衣的黑白名单"，靠人工穷举分隔符/方位词覆盖不了语言的
# 全部表达方式。下面四条红灯全部使用真实语料（data/manju.db，
# project_id=proj_3ac0b627fa46）验证过的原文片段，模拟裁决模型给出确定性
# 判决，只验证代码这一侧（卷宗检索是否真的把该找到的原文段落塞进了裁决
# 提示词、钉证是否真的按逐字包含判定、判决结果是否被正确翻译成
# accepted/rejected 与对应 reason）。
# ---------------------------------------------------------------------------

def test_true_name_dossier_trial_rejects_enumeration_counter_evidence_real_corpus(
    monkeypatch,
):
    """红灯 a（真实语料，EP2/EP3 回归 "小胖子"误绑"王有材"）：真实第10章
    原文「"小胖子、王有材、还有那虎头虎脑的少年，当初我们四人被一起带上
    靠山宗，不知此刻他们怎样。"」把两个名字并列举出，证明二者不是同一人。
    这段反证段落因同时含"小胖子"和"王有材"两个词，天然会被卷宗检索
    （_prep_pack_true_name_dossier 全书扫描）捞到并塞进裁决提示词——不需要
    写任何"列举/分隔符"规则去猜它长什么样。裁决模型看到后独立判 different，
    假设一票否决，"王有材"这个 portrait 绝不能出现在"小胖子"的结果里。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-wyc','p1','王有材',1,NULL)"
    )
    conn.execute(
        "INSERT INTO chapters(project_id, idx, content) VALUES ('p1', 10, ?)",
        (
            "“小胖子、王有材、还有那虎头虎脑的少年，当初我们四人被一起带上"
            "靠山宗，不知此刻他们怎样。”孟浩沉吟片刻，身子向前一晃，体内灵气"
            "运转，整个人顿时远去，在这山峦中走向北峰。",
        ),
    )
    conn.commit()

    verdict_calls = {"n": 0}

    async def fake_chat_structured(messages, **kwargs):
        verdict_calls["n"] += 1
        prompt = str(messages[0]["content"])
        assert (
            "小胖子、王有材、还有那虎头虎脑的少年，当初我们四人被一起带上"
            "靠山宗，不知此刻他们怎样。" in prompt
        ), "第10章列举反证段落必须真的进入裁决卷宗"
        return prep_pack._PrepPackTrueNameVerdictResponse(
            verdict="different",
            supporting_quote=(
                "小胖子、王有材、还有那虎头虎脑的少年，当初我们四人被一起带上"
                "靠山宗，不知此刻他们怎样。"
            ),
        )

    monkeypatch.setattr(prep_pack.model_gateway, "chat_structured", fake_chat_structured)

    async def fake_ensure_cards_for_text(project_id, episode_no, source_text, bible, *, generate_portraits=True):
        conn.execute(
            "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
            "VALUES ('cp-xpz','p1','小胖子',2,NULL)"
        )
        conn.commit()
        return {"added": [{"name": "小胖子"}], "resolutions": [], "errors": [], "skipped": [], "warnings": []}

    monkeypatch.setattr(portraits, "ensure_cards_for_text", fake_ensure_cards_for_text)
    monkeypatch.setattr(portraits, "persist_screenplay_character_resolutions", lambda *a, **k: [])

    events = [_event("ev_099", characters=[
        {"display_name": "小胖子", "is_background_extra": False, "suspected_true_name": "王有材"},
    ])]
    (
        characters, scene_list, functional_extras, errors, stats,
        true_name_hints, scene_alias_anchors, rejected_alias_conflicts,
    ) = _resolve(
        conn, events=events,
        source_text="小胖子摸了摸后脑勺，嘿嘿一笑。",
    )

    assert verdict_calls["n"] == 1
    assert errors == []
    rejected = [h for h in true_name_hints if h["status"] == "rejected"]
    assert rejected == [{
        "kind": "character", "mention": "小胖子", "suspected_true_name": "王有材",
        "status": "rejected", "reason": "rejected_verdict_different",
    }]
    assert not any(c["portrait_id"] == "cp-wyc" for c in characters), (
        "绝不能把「小胖子」误绑给列举反证已经证明是另一个人的「王有材」"
    )


def test_true_name_dossier_trial_rejects_containment_false_positive_real_corpus(
    monkeypatch,
):
    """红灯 b（真实语料，EP6 回归 "上官修身边的男子"误绑"上官修"）：这是
    称谓包含目标人名子串导致的经典误绑陷阱，用户否决过两版结构规则（列举
    反证、包含方向性规则）去猜它的语法形状，最终架构完全不猜语义规则：
    真实第6章原文「除了许师姐外，便是上官修身边的男子。」因含"上官修"被
    卷宗检索到并塞进裁决提示词，裁决模型看到"身边的男子"这个描述后独立判
    different，假设被拒。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-sgx','p1','上官修',1,NULL)"
    )
    conn.execute(
        "INSERT INTO chapters(project_id, idx, content) VALUES ('p1', 6, ?)",
        ("除了许师姐外，便是上官修身边的男子。",),
    )
    conn.commit()

    verdict_calls = {"n": 0}

    async def fake_chat_structured(messages, **kwargs):
        verdict_calls["n"] += 1
        prompt = str(messages[0]["content"])
        assert "除了许师姐外，便是上官修身边的男子。" in prompt, (
            "第6章反证段落必须真的进入裁决卷宗"
        )
        return prep_pack._PrepPackTrueNameVerdictResponse(
            verdict="different", supporting_quote="除了许师姐外，便是上官修身边的男子。",
        )

    monkeypatch.setattr(prep_pack.model_gateway, "chat_structured", fake_chat_structured)

    async def fake_ensure_cards_for_text(project_id, episode_no, source_text, bible, *, generate_portraits=True):
        conn.execute(
            "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
            "VALUES ('cp-mysbdnz','p1','上官修身边的男子',2,NULL)"
        )
        conn.commit()
        return {
            "added": [{"name": "上官修身边的男子"}], "resolutions": [],
            "errors": [], "skipped": [], "warnings": [],
        }

    monkeypatch.setattr(portraits, "ensure_cards_for_text", fake_ensure_cards_for_text)
    monkeypatch.setattr(portraits, "persist_screenplay_character_resolutions", lambda *a, **k: [])

    events = [_event("ev_100", characters=[
        {
            "display_name": "上官修身边的男子", "is_background_extra": False,
            "suspected_true_name": "上官修",
        },
    ])]
    (
        characters, scene_list, functional_extras, errors, stats,
        true_name_hints, scene_alias_anchors, rejected_alias_conflicts,
    ) = _resolve(
        conn, events=events,
        source_text="上官修身边的男子上前一步，冷冷开口。",
    )

    assert verdict_calls["n"] == 1
    assert errors == []
    rejected = [h for h in true_name_hints if h["status"] == "rejected"]
    assert rejected == [{
        "kind": "character", "mention": "上官修身边的男子", "suspected_true_name": "上官修",
        "status": "rejected", "reason": "rejected_verdict_different",
    }]
    assert not any(c["portrait_id"] == "cp-sgx" for c in characters), (
        "含有目标人名子串不等于同一人，绝不能因包含关系静默绑定"
    )


def test_true_name_dossier_trial_accepts_verified_link_real_corpus(monkeypatch):
    """红灯 c（真实语料，人工核实过链接确实存在——见
    data/manju.db project_id=proj_3ac0b627fa46 第692章第14段）：真实原文
    「他是当年的小胖子，李富贵。」明确陈述"小胖子"就是"李富贵"，裁决模型
    独立判 same 并逐字引用这句原文，钉证通过——核验通过，绑定到李富贵已有
    的 portrait_id，别名库记入"小胖子"，method 标注 resolution_forward
    （钉住的支撑句来自全书检索出的第692章，不是本集自己的段落）并带上真实
    引用的章节号。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-lfg','p1','李富贵',1,NULL)"
    )
    conn.execute(
        "INSERT INTO chapters(project_id, idx, content) VALUES ('p1', 692, ?)",
        ("他是当年的小胖子，李富贵。",),
    )
    conn.commit()

    verdict_calls = {"n": 0}

    async def fake_chat_structured(messages, **kwargs):
        verdict_calls["n"] += 1
        prompt = str(messages[0]["content"])
        assert "他是当年的小胖子，李富贵。" in prompt, "第692章链接段落必须真的进入裁决卷宗"
        return prep_pack._PrepPackTrueNameVerdictResponse(
            verdict="same", supporting_quote="他是当年的小胖子，李富贵。",
        )

    monkeypatch.setattr(prep_pack.model_gateway, "chat_structured", fake_chat_structured)

    def boom_character(*_a, **_k):
        raise AssertionError("核验通过的假设应走确定性快车道，不应该再触发全量身份发现")

    monkeypatch.setattr(portraits, "ensure_cards_for_text", boom_character)

    events = [_event("ev_101", characters=[
        {"display_name": "小胖子", "is_background_extra": False, "suspected_true_name": "李富贵"},
    ])]
    (
        characters, scene_list, functional_extras, errors, stats,
        true_name_hints, scene_alias_anchors, rejected_alias_conflicts,
    ) = _resolve(
        conn, events=events,
        source_text="小胖子摸了摸后脑勺，嘿嘿一笑。",
    )

    assert verdict_calls["n"] == 1
    assert errors == []
    assert stats["character_discovery_calls"] == 0
    assert characters == [{
        "identity_id": "bible:李富贵", "display_name": "李富贵",
        "portrait_id": "cp-lfg", "event_ids": ["ev_101"], "aliases": ["小胖子"],
        "visual_entity_id": "bible:李富贵", "display_appellation": "小胖子",
        "provenance": {
            "method": "resolution_forward", "anchor_segments": [],
            # 第30轮 RCA 修正：anchor_phrase 记裁决钉住的支撑句本身。
            "anchor_phrase": "他是当年的小胖子，李富贵。",
            "forward_chapter_label": "第 692 章",
        },
    }]
    assert any(
        h["status"] == "accepted" and h["mention"] == "小胖子"
        and h["suspected_true_name"] == "李富贵"
        for h in true_name_hints
    )


def test_true_name_dossier_trial_rejects_unpinnable_quote_anti_forgery(monkeypatch):
    """红灯 d（防编证词）：裁决模型判 same，但引用的"支撑句"根本不在卷宗
    任何一条原文里逐字存在（编造证词，或者只是意译/概括而非逐字摘录）——
    钉证（_prep_pack_pin_dossier_quote）必须失败，reason=
    rejected_quote_not_pinned，绝不能仅凭模型说"same"就采信，防止模型在
    没有真实依据时也能编一句听起来像证据的话蒙混过关。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-lfg','p1','李富贵',1,NULL)"
    )
    conn.execute(
        "INSERT INTO chapters(project_id, idx, content) VALUES ('p1', 692, ?)",
        ("他是当年的小胖子，李富贵。",),
    )
    conn.commit()

    verdict_calls = {"n": 0}

    async def fake_chat_structured(messages, **kwargs):
        verdict_calls["n"] += 1
        return prep_pack._PrepPackTrueNameVerdictResponse(
            verdict="same",
            # 卷宗里根本没有这句话——编造的"证词"。
            supporting_quote="两人身份完全一致，毫无疑问是同一个人。",
        )

    monkeypatch.setattr(prep_pack.model_gateway, "chat_structured", fake_chat_structured)

    async def fake_ensure_cards_for_text(project_id, episode_no, source_text, bible, *, generate_portraits=True):
        conn.execute(
            "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
            "VALUES ('cp-xpz','p1','小胖子',2,NULL)"
        )
        conn.commit()
        return {"added": [{"name": "小胖子"}], "resolutions": [], "errors": [], "skipped": [], "warnings": []}

    monkeypatch.setattr(portraits, "ensure_cards_for_text", fake_ensure_cards_for_text)
    monkeypatch.setattr(portraits, "persist_screenplay_character_resolutions", lambda *a, **k: [])

    events = [_event("ev_102", characters=[
        {"display_name": "小胖子", "is_background_extra": False, "suspected_true_name": "李富贵"},
    ])]
    (
        characters, scene_list, functional_extras, errors, stats,
        true_name_hints, scene_alias_anchors, rejected_alias_conflicts,
    ) = _resolve(
        conn, events=events,
        source_text="小胖子摸了摸后脑勺，嘿嘿一笑。",
    )

    assert verdict_calls["n"] == 1
    assert errors == []
    rejected = [h for h in true_name_hints if h["status"] == "rejected"]
    assert rejected == [{
        "kind": "character", "mention": "小胖子", "suspected_true_name": "李富贵",
        "status": "rejected", "reason": "rejected_quote_not_pinned",
    }]
    assert not any(c["portrait_id"] == "cp-lfg" for c in characters), (
        "编造的证词不得被钉证接受，绝不能因模型口头声称 same 就静默采信"
    )


def test_character_rename_coincidentally_matching_suspected_true_name_uses_resolution_not_forward(
    monkeypatch,
):
    """红灯（第30轮 RCA 根因复现，真实 EP2/6/8「李富贵」回归）：真正的病灶
    不是"忘了传章节号"，而是 method 判定曾经用 resolved_name==
    suspected_true_name 反推"这次是不是走的真名核验"——但 resolved_name
    也可能通过 character_rename（角色发现，完全独立的另一条路径）巧合算出
    同一个真名。真实场景：pass 1 里"小胖子"→"李富贵"的假设被裁决拒绝
    （模型态度不确定），"小胖子"在 pass 1 里留作未解析；discovery 在
    pass 2 里独立判定"小胖子"应改绑"李富贵"（跟 suspected_true_name 猜的
    是同一个人，但走的是完全不同的证据链——discovery 自己看过本集原文
    判定的，不是 dossier 裁决）。这种情况下 suspected_true_name（"李富贵"）
    在 pass 2 里从一开始就等于 character_rename 算出的 resolved_name，
    核验分支的 if 判据直接为 False，根本不会重新发起裁决——如果 method
    判定还在用状态反推，就会把这次 character_rename 驱动的改名误判成
    "真名核验通过"，产出一张 forward_chapter_label/anchor_phrase 双双为空
    的 resolution_forward 半张证书。修复后：via_suspected_true_name 只在
    核验真的跑过且 accepted 时才置位，这次改名正确走 method="resolution"
    分支（锚点来自 discovery 证据链/称谓本身），resolution_forward 完全
    不会被产出，也就没有半张证书可言。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-lfg','p1','李富贵',1,NULL)"
    )
    conn.execute(
        "INSERT INTO chapters(project_id, idx, content) VALUES ('p1', 692, ?)",
        ("他是当年的小胖子，李富贵。",),
    )
    conn.commit()

    verdict_calls = {"n": 0}

    async def fake_chat_structured(messages, **kwargs):
        verdict_calls["n"] += 1
        # pass 1 的裁决态度不确定——假设不被采信，"小胖子"留作未解析。
        return prep_pack._PrepPackTrueNameVerdictResponse(
            verdict="uncertain", supporting_quote="",
        )

    monkeypatch.setattr(prep_pack.model_gateway, "chat_structured", fake_chat_structured)

    async def fake_ensure_cards_for_text(project_id, episode_no, source_text, bible, *, generate_portraits=True):
        # discovery 独立判定："小胖子"应改绑"李富贵"——巧合跟 suspected_
        # true_name 猜的是同一个人，但这是 discovery 自己的证据链，不是
        # 上面那次被拒绝的裁决复活。
        return {
            "added": [], "skipped": [],
            "resolutions": [{
                "source_label": "小胖子", "canonical_name": "李富贵",
                "resolution": "future_identity",
            }],
            "errors": [], "warnings": [],
        }

    monkeypatch.setattr(portraits, "ensure_cards_for_text", fake_ensure_cards_for_text)
    monkeypatch.setattr(portraits, "persist_screenplay_character_resolutions", lambda *a, **k: [])

    events = [_event("ev_001", characters=[
        {"display_name": "小胖子", "is_background_extra": False, "suspected_true_name": "李富贵"},
    ])]
    (
        characters, scene_list, functional_extras, errors, stats,
        true_name_hints, scene_alias_anchors, rejected_alias_conflicts,
    ) = _resolve(
        conn, events=events,
        source_text="小胖子挠了挠头，笑了笑。",
    )

    assert verdict_calls["n"] == 1, "裁决被 pass 1 拒绝后不应在 pass 2 重新发起（character_rename 已经算出同一个名字，核验分支条件不成立）"
    assert errors == []
    lfg = next(c for c in characters if c["display_name"] == "李富贵")
    assert lfg["provenance"]["method"] == "resolution", (
        "character_rename 巧合等于 suspected_true_name 不等于真的走过核验，"
        "绝不能被误判成 resolution_forward"
    )
    assert lfg["provenance"]["anchor_phrase"], "resolution 方法必须有非空本地锚点"
    assert "forward_chapter_label" not in lfg["provenance"]
    verify_errors = _provenance_self_verify(
        "小胖子挠了挠头，笑了笑。", characters, scene_list, functional_extras,
    )
    assert verify_errors == []


# ---------------------------------------------------------------------------
# 独立评审 blocker：_prep_pack_verify_true_name_hypothesis /
# _prep_pack_true_name_verdict 被角色分支（resolve_fn=_resolve_portrait_id）
# 与场景分支（resolve_fn=_resolve_scene_reference_id）共用，旧版裁决提示词
# 硬编码人物语义（"判断称谓 X 与人名 Y 是否指同一个人"）——场景假设走到这
# 条路时模型被问"这两个是不是同一个人"，语义错误，裁决不可靠。上面四条
# true_name_dossier 红灯只覆盖了角色分支真正发起裁决调用的形状；场景分支
# 此前唯一的覆盖（test_suspected_true_name_hypothesis_rejected_with_no_
# evidence_routes_to_discovery）卷宗为空、在 rejected_no_dossier 就提前
# 返回，从未真正走到裁决模型调用。下面两条红灯补齐：场景分支真正走到裁决
# 调用时提示词必须是场景语义；以及同一份 verdict_cache 字典被角色/场景两
# 域共用时必须按 subject_kind 隔离，不能让跨域撞名复用错误域的裁决。
# ---------------------------------------------------------------------------

def test_scene_true_name_hypothesis_verdict_prompt_uses_scene_semantics(monkeypatch):
    """红灯 a：给场景假设一份真正非空的卷宗（含"荒地"与"无极峰绝顶"共现
    的原文段落），把场景分支真正推进到 _prep_pack_true_name_verdict 的模型
    调用——断言发给模型的提示词已经换成场景语义（含"场景"或"地点"字样），
    绝不能再出现"同一个人"这个人物专属措辞。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end) "
        "VALUES ('sr-wjfjd','p1','无极峰绝顶',1,NULL)"
    )
    conn.execute(
        "INSERT INTO chapters(project_id, idx, content) VALUES ('p1', 50, ?)",
        ("众人所说的荒地，其实正是宗门秘境无极峰绝顶。",),
    )
    conn.commit()

    verdict_calls = {"n": 0}
    seen_prompt = {"text": ""}

    async def fake_chat_structured(messages, **kwargs):
        verdict_calls["n"] += 1
        prompt = str(messages[0]["content"])
        seen_prompt["text"] = prompt
        assert "众人所说的荒地，其实正是宗门秘境无极峰绝顶。" in prompt, (
            "第50章链接段落必须真的进入裁决卷宗"
        )
        return prep_pack._PrepPackTrueNameVerdictResponse(
            verdict="same", supporting_quote="众人所说的荒地，其实正是宗门秘境无极峰绝顶。",
        )

    monkeypatch.setattr(prep_pack.model_gateway, "chat_structured", fake_chat_structured)

    async def boom_scene(*_a, **_k):
        raise AssertionError("核验通过的假设应走确定性快车道，不应该再触发场景发现")

    monkeypatch.setattr(scenes, "ensure_scenes_for_labels", boom_scene)

    events = [_event("ev_201", scenes_=[
        {"display_name": "荒地", "suspected_true_name": "无极峰绝顶"},
    ])]
    characters, scene_list, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(
        conn, events=events,
        source_text="荒地上尘土飞扬，无人问津。",
    )

    assert verdict_calls["n"] == 1
    assert errors == []
    assert stats["scene_discovery_calls"] == 0
    prompt = seen_prompt["text"]
    assert ("场景" in prompt) or ("地点" in prompt), (
        "场景假设的裁决提示词必须换成场景/地点语义"
    )
    assert "同一个人" not in prompt, "场景假设绝不能被问「是否是同一个人」"
    assert any(
        h["status"] == "accepted" and h["kind"] == "scene" and h["mention"] == "荒地"
        and h["suspected_true_name"] == "无极峰绝顶"
        for h in true_name_hints
    )
    assert any(s["scene_reference_id"] == "sr-wjfjd" for s in scene_list)


def test_true_name_verdict_cache_isolated_by_subject_kind(monkeypatch):
    """红灯 b：verdict_cache 缓存键若只有 (alias, true_name)，角色循环与
    场景循环共用同一个 true_name_verdict_cache 字典对象——同一 (alias,
    true_name) 字符串组合若碰巧在两个域各自的候选真名表里都能查到（跨域
    撞名，纯为验证缓存隔离而刻意构造，不代表任何真实语义），第二个域会
    直接复用第一个域已经缓存的裁决结果，跳过本该属于自己那次的模型调用。
    缓存键纳入 subject_kind 后，两域必须各自真正发起一次裁决调用（总计
    2 次），不能因为字符串相同就被对方的缓存结果顶替。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-sds','p1','石大山',1,NULL)"
    )
    conn.execute(
        "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end) "
        "VALUES ('sr-sds','p1','石大山',1,NULL)"
    )
    conn.execute(
        "INSERT INTO chapters(project_id, idx, content) VALUES ('p1', 60, ?)",
        ("阿石本名就是石大山，村里人都爱这么叫他。",),
    )
    conn.commit()

    verdict_calls = {"n": 0}

    async def fake_chat_structured(messages, **kwargs):
        verdict_calls["n"] += 1
        return prep_pack._PrepPackTrueNameVerdictResponse(
            verdict="same", supporting_quote="阿石本名就是石大山，村里人都爱这么叫他。",
        )

    monkeypatch.setattr(prep_pack.model_gateway, "chat_structured", fake_chat_structured)

    async def boom_character(*_a, **_k):
        raise AssertionError("核验通过的假设应走确定性快车道，不应该再触发角色发现")

    async def boom_scene(*_a, **_k):
        raise AssertionError("核验通过的假设应走确定性快车道，不应该再触发场景发现")

    monkeypatch.setattr(portraits, "ensure_cards_for_text", boom_character)
    monkeypatch.setattr(scenes, "ensure_scenes_for_labels", boom_scene)

    events = [_event(
        "ev_401",
        characters=[{
            "display_name": "阿石", "is_background_extra": False,
            "suspected_true_name": "石大山",
        }],
        scenes_=[{"display_name": "阿石", "suspected_true_name": "石大山"}],
    )]
    characters, scene_list, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(
        conn, events=events,
        source_text="阿石本名就是石大山，村里人都爱这么叫他。",
    )

    assert verdict_calls["n"] == 2, (
        "角色域与场景域共用同一份 verdict_cache，缓存键必须纳入 subject_kind "
        "隔离两域，不能让相同的 (alias, true_name) 字符串复用另一个域的裁决"
    )
    assert errors == []
    accepted_kinds = {h["kind"] for h in true_name_hints if h["status"] == "accepted"}
    assert accepted_kinds == {"character", "scene"}
    assert any(c["display_name"] == "石大山" for c in characters)
    assert any(s["display_name"] == "石大山" for s in scene_list)


# ---------------------------------------------------------------------------
# 1.7.0（docs/CHARACTER_IDENTITY_ENTITY_DESIGN.md §4.3/§6 第7项/§8 判据1/2/6）：
# asset_manifest.characters[]/functional_extras[] 新增 visual_entity_id（决定
# 取图，跨集稳定）+ characters[] 新增 display_appellation（本集原文措辞，
# 决定字幕/称呼）。这里直接对齐设计文档 §8 的机械判据本身：同一个角色在
# 不同集用不同措辞出场时，visual_entity_id 必须逐字相同；display_name/
# display_appellation 当集写什么完全不影响这个结论。
# ---------------------------------------------------------------------------

def test_visual_entity_id_stable_across_episodes_despite_different_in_episode_wording(
    monkeypatch,
):
    """红灯（§8 判据1 的直接机械化：许清 EP1「银色长袍女子」/EP5「许姓女子」/
    EP6「许师姐」三种不同措辞场景的简化复现）：同一个已具名角色在两集里用了
    两种完全不同的本集措辞（display_appellation 因此不同），但只要都解析到
    同一个 canonical name，visual_entity_id 必须逐字相同——画面取图只看
    visual_entity_id，不受本集措辞影响。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-xq','p1','许清',1,NULL)"
    )
    conn.commit()

    def boom_character(*_a, **_k):
        raise AssertionError("裸命中/别名注册表命中都不应该回炉重新消歧")

    monkeypatch.setattr(portraits, "ensure_cards_for_text", boom_character)

    events_ep1 = [_event("ev_ep1", characters=[
        {"display_name": "许清", "is_background_extra": False},
    ])]
    characters_ep1, _, _, errors_ep1, stats_ep1, *_ = _resolve(
        conn, events=events_ep1, episode_no=1,
        source_text="许清缓步走出竹林。",
    )
    assert errors_ep1 == [] and stats_ep1["character_discovery_calls"] == 0

    _seed_bible_characters(conn, "p1", [
        _bible_character("许清", aliases=[_bible_alias("许师姐")]),
    ])
    events_ep6 = [_event("ev_ep6", characters=[
        {"display_name": "许师姐", "is_background_extra": False},
    ])]
    characters_ep6, _, _, errors_ep6, stats_ep6, *_ = _resolve(
        conn, events=events_ep6, episode_no=6,
        source_text="许师姐冷冷地看了他一眼。",
    )
    assert errors_ep6 == [] and stats_ep6["character_discovery_calls"] == 0

    assert len(characters_ep1) == 1 and len(characters_ep6) == 1
    ep1_entry, ep6_entry = characters_ep1[0], characters_ep6[0]
    assert ep1_entry["display_appellation"] == "许清"
    assert ep6_entry["display_appellation"] == "许师姐"
    assert ep1_entry["visual_entity_id"] == ep6_entry["visual_entity_id"] == "bible:许清", (
        "同一角色跨集出场措辞不同不得导致 visual_entity_id 漂移——那正是"
        "「每集换脸」故障的根因，见设计文档 §0/§8 判据1"
    )


def test_functional_extra_visual_entity_id_stable_for_same_raw_label_across_episodes(
    monkeypatch,
):
    """§8 判据的未具名侧：同一个未绑定角色在两集里都用了同一个原文标签
    （尚未触发任何跨集别名/真名核验），functional_extras[] 的 visual_entity_id
    必须逐字相同——未具名角色也要有跨集稳定视觉实体，这正是"不再每集换脸"
    对群演同样成立的关键（设计文档 §4.3 动机段）。"""
    conn = _make_conn()

    async def fake_functional(project_id, episode_no, source_text, bible, *, generate_portraits=True):
        return {
            "added": [], "skipped": [],
            "resolutions": [{
                "source_label": "神秘蒙面客", "canonical_name": "神秘蒙面客",
                "resolution": "functional_identity",
            }],
            "errors": [], "warnings": [],
        }

    monkeypatch.setattr(portraits, "ensure_cards_for_text", fake_functional)
    monkeypatch.setattr(portraits, "persist_screenplay_character_resolutions", lambda *a, **k: [])
    events = [_event("ev_a", characters=[
        {"display_name": "神秘蒙面客", "is_background_extra": False},
    ])]
    _, _, extras_1, errors_1, *_ = _resolve(
        conn, events=events, episode_no=1,
        source_text="神秘蒙面客悄然离去。",
    )
    events2 = [_event("ev_b", characters=[
        {"display_name": "神秘蒙面客", "is_background_extra": False},
    ])]
    _, _, extras_2, errors_2, *_ = _resolve(
        conn, events=events2, episode_no=4,
        source_text="神秘蒙面客再度现身。",
    )

    assert errors_1 == [] and errors_2 == []
    assert len(extras_1) == 1 and len(extras_2) == 1
    assert extras_1[0]["visual_entity_id"] == extras_2[0]["visual_entity_id"]
    assert extras_1[0]["visual_entity_id"].startswith("entity:")


def test_prep_pack_version_is_1_7_0():
    """版本号哨兵：本次改造（跨集别名读源切换 + visual_entity_id/
    display_appellation）是 P0 收口，schema/payload 均有变更，版本必须
    推进到 1.7.0（docs/CHARACTER_IDENTITY_ENTITY_DESIGN.md §4.3 末段）。"""
    assert prep_pack.PREP_PACK_VERSION == "1.7.0"
