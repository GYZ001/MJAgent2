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
import re
import sqlite3

import pytest

from app import scenes
from app.production import prep_pack
from app.source_excerpt import SourceSegment
from tests.conftest import patch_portraits_everywhere, patch_prep_pack_everywhere


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


def _event(event_id: str, *, characters=None, scenes_=None, source_span=None) -> dict:
    """2.0.0 兼容外壳：event_chain/event_id/source_span 概念本身已随架构
    收窄撤销（见 app.production.prep_pack.PREP_PACK_VERSION 上方 2.0.0
    大注释），保留这个构造函数只是为了不用逐一改写本文件里近百处既有
    调用点——真正喂给 ``_resolve_assets`` 的是 ``_resolve()`` 内部经
    ``_mentions_with_segment_indexes`` 摊平、并用生产代码自己的逐段证据
    闸（``prep_pack._prep_pack_gate_segment_indexes``）算出真实
    segment_indexes 之后的扁平提及列表，不是这个函数返回的字典本身。
    ``source_span`` 不再被任何生产代码读取，仅为兼容旧调用点签名保留。"""
    return {
        "event_id": event_id,
        "characters": characters or [],
        "scenes": scenes_ or [],
        "source_span": source_span,
    }


def _mentions_with_segment_indexes(
    events: list[dict], source_text: str, group_key: str,
) -> list[dict]:
    """2.0.0 兼容外壳：把旧版按事件分组的提及（``_event()`` 构造）摊平成
    ``_resolve_assets`` 现在期望的扁平提及列表。生产代码的段号入口闸
    （``_prep_pack_gate_segment_indexes``）刻意只做结构核验、不做逐字
    核验（见该函数上方说明——逐字核验会在候选判别机会到来之前堵死合成
    描述性标签，真实 EP1"银色长袍女子"案例即此），所以不能拿它来算这里
    的 segment_indexes；改为直接在测试夹具这一层做逐段字面搜索——对绝大
    多数既有测试用例（display_name/label 本来就是原文逐字用词）这就是
    它们真实会落在哪些段落，等价于真实产线上模型自己会申报的段号。少数
    故意构造"标签不逐字出现在原文"的测试（模拟合成描述性标签）会在这里
    自然得到空 segment_indexes，与 _resolve_assets 里"裸命中要求逐字/
    解析路径豁免"的既有分支语义完全吻合，不是伪造。"""
    from app.source_excerpt import index_source_segments

    segments = index_source_segments(source_text)
    mentions: list[dict] = []
    for event in events:
        for mention in event[group_key]:
            label = mention.get("display_name") or mention.get("label") or ""
            valid_indexes = [
                index for index, segment in enumerate(segments, start=1)
                if label and label in segment.text
            ]
            new_mention = dict(mention)
            new_mention.setdefault("segment_indexes", valid_indexes)
            if not new_mention.get("segment_indexes"):
                new_mention["segment_indexes"] = valid_indexes
            mentions.append(new_mention)
    return mentions


def _resolve(conn, **kwargs):
    defaults = dict(
        project_id="p1", episode_id="ep-test", episode_no=2,
        source_text="占位原文。", run_id=None,
    )
    defaults.update(kwargs)
    events = defaults.pop("events", [])
    source_text = defaults["source_text"]
    defaults.setdefault(
        "character_mentions",
        _mentions_with_segment_indexes(events, source_text, "characters"),
    )
    defaults.setdefault(
        "scene_mentions",
        _mentions_with_segment_indexes(events, source_text, "scenes"),
    )
    defaults.setdefault("prop_mentions", [])
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
        "VALUES ('cp1','p1','甲一',1,NULL)"
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

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", boom_character)
    monkeypatch.setattr(scenes, "ensure_scenes_for_labels", boom_scene)

    events = [_event(
        "ev_001",
        characters=[{"display_name": "甲一", "is_background_extra": False}],
        scenes_=[{"display_name": "宗门广场"}],
    )]
    characters, scene_list, props, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(
        conn, episode_no=1, events=events,
        # 1.4.2 称谓/场景名证据闸要求直接命中必须有本集文本证据 -- 两个提及都
        # 得逐字出现在这里，否则会被误当成"裸命中没证据"（这不是本测试要覆盖
        # 的场景，本测试要的是"零调用发现"，所以必须让直接命中干净通过）。
        source_text="甲一快步穿过宗门广场，众弟子纷纷让路。",
    )

    assert errors == []
    assert stats == {"character_discovery_calls": 0, "scene_discovery_calls": 0}
    assert functional_extras == []
    assert characters == [{
        "identity_id": "bible:甲一", "display_name": "甲一",
        "portrait_id": "cp1", "segment_indexes": [1], "aliases": [],
        "visual_entity_id": "bible:甲一", "display_appellation": "甲一",
        "provenance": {
            "method": "direct", "anchor_segments": [1], "anchor_phrase": "甲一",
        },
    }]
    assert scene_list == [{
        "scene_id": "scene:宗门广场", "display_name": "宗门广场",
        "scene_reference_id": "sr1", "segment_indexes": [1],
        "provenance": {
            "method": "direct", "anchor_segments": [1], "anchor_phrase": "宗门广场",
        },
    }]


# ---------------------------------------------------------------------------
# 任务V收尾（2026-08-31 真实 EP1 回归 ERR-20260831-63a9d2）：出图已解耦到
# 后台，建映射清单这一刻 character_portraits/scene_references 往往一行都
# 没有（图/定场图还没生成完）。判据必须挂人物谱/场景库里有没有这张卡，不挂
# 那两张表有没有行——男主角孟浩人物谱在册，仅仅因为图还没出就被判"未解析到
# 已有 portrait_id"，连同王有材、许师姐一起把整集剧本阶段判 failed。
# ---------------------------------------------------------------------------

def test_character_resolves_from_bible_card_without_any_portrait_row(monkeypatch):
    """孟浩人物谱在册，但 character_portraits 一行都没有（出图还没跑完）。
    裸命中必须在 pass 1 直接解析成功（zero discovery calls），portrait_id
    留空而不是报错落 functional_extras——出图闸门是生成台的事，不是这里。"""
    conn = _make_conn()
    _seed_bible_characters(conn, "p1", [
        _bible_character("孟浩", appearance_canonical="十七岁少年，黑发短打"),
    ])

    def boom_character(*_a, **_k):
        raise AssertionError("人物谱已有卡的裸命中不应调用角色发现")

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", boom_character)

    events = [_event("ev_001", characters=[
        {"display_name": "孟浩", "is_background_extra": False},
    ])]
    characters, scene_list, props, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(
        conn, episode_no=1, events=events,
        source_text="孟浩站在山门前，神情凝重。",
    )

    assert errors == []
    assert stats["character_discovery_calls"] == 0
    assert functional_extras == []
    assert characters == [{
        "identity_id": "bible:孟浩", "display_name": "孟浩",
        "portrait_id": None, "segment_indexes": [1], "aliases": [],
        "visual_entity_id": "bible:孟浩", "display_appellation": "孟浩",
        "provenance": {
            "method": "direct", "anchor_segments": [1], "anchor_phrase": "孟浩",
        },
    }]


def test_scene_resolves_from_bible_card_without_any_reference_row(monkeypatch):
    """场景侧同构：场景库有「宗门广场」这张卡，但 scene_references 一行都
    没有（定场图还没生成）。裸命中必须直接解析成功，不落"未解析到已有
    scene_reference_id"。"""
    conn = _make_conn()
    conn.execute(
        "UPDATE projects SET bible_json=? WHERE id='p1'",
        (json.dumps({
            "characters": [],
            "scenes": [{"name": "宗门广场", "scene_canonical": "青石铺地的宽阔广场"}],
            "world": {"era": "", "genre": "", "visual_style_canonical": "测试画风"},
        }, ensure_ascii=False),),
    )
    conn.commit()

    def boom_scene(*_a, **_k):
        raise AssertionError("场景库已有卡的裸命中不应调用场景发现")

    monkeypatch.setattr(scenes, "ensure_scenes_for_labels", boom_scene)

    events = [_event("ev_001", scenes_=[{"display_name": "宗门广场"}])]
    characters, scene_list, props, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(
        conn, episode_no=1, events=events,
        source_text="众人聚在宗门广场，等候号令。",
    )

    assert errors == []
    assert stats["scene_discovery_calls"] == 0
    assert scene_list == [{
        "scene_id": "scene:宗门广场", "display_name": "宗门广场",
        "scene_reference_id": None, "segment_indexes": [1],
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

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", fake_disambiguate)
    patch_portraits_everywhere(monkeypatch, "persist_screenplay_character_resolutions", lambda *a, **k: [])

    events = [
        _event("ev_002", characters=[
            {"display_name": "孟浩", "is_background_extra": False},
            {"display_name": "小胖子", "is_background_extra": True},
        ]),
        _event("ev_003", characters=[
            {"display_name": "小胖子", "is_background_extra": True},
        ]),
    ]
    characters, scene_list, props, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(
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
    assert entry["segment_indexes"] == [1]


# ---------------------------------------------------------------------------
# Real bug #2 (EP13): occupation-title one-off mentions discovery has no
# exact-string opinion on (but ran cleanly, no errors) must be legally
# absorbed into functional_extras under their own source label -- not
# blocked, not silently dropped, not renamed to something generic.
# ---------------------------------------------------------------------------


def _card_in_bible(conn, project_id: str, name: str) -> None:
    """把角色写进人物谱——真实 ``ensure_cards_for_text`` 建卡就是写这里。

    出图解耦到后台之后，"是具名角色还是群演"的判据挂在人物谱有没有这张卡，不再
    挂"现在有没有定妆照图"（建清单那一刻图往往还没出来）。桩必须跟着真实行为走，
    只插 character_portraits 行不写人物谱就不是忠实替身了。
    """
    import json as _json

    row = conn.execute(
        "SELECT bible_json FROM projects WHERE id=?", (project_id,),
    ).fetchone()
    try:
        bible = _json.loads((row[0] if row else "") or "{}")
    except (TypeError, ValueError):
        bible = {}
    characters = list(bible.get("characters") or [])
    if not any(str(c.get("name") or "") == name for c in characters):
        characters.append({
            "name": name, "aliases": [],
            # role / appearance_canonical 是 Character 的必填字段，缺了
            # Bible.model_validate 会抛，桩就不再是忠实替身。
            "role": "配角", "appearance_canonical": f"{name}的外貌",
        })
    bible["characters"] = characters
    # upsert 而不是 UPDATE：夹具只建表，projects 行由各测试自己插，行不在时
    # UPDATE 会静默不生效，桩看起来建了卡其实没建。
    conn.execute(
        "INSERT INTO projects(id, bible_json) VALUES(?,?)"
        " ON CONFLICT(id) DO UPDATE SET bible_json=excluded.bible_json",
        (project_id, _json.dumps(bible, ensure_ascii=False)),
    )
    conn.commit()


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
        _card_in_bible(conn, "p1", "曹阳")
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

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", fake_discovery)
    # 曹阳建卡之后候选集不再为空，未解析标签会走候选判别（一次模型调用）。
    # 本用例只关心"职业称谓收编成群演、曹阳进 characters"，判别一律回未绑定。
    async def no_candidate(*_a, **_k):
        return {"resolved": False, "attempted": False}

    patch_prep_pack_everywhere(
        monkeypatch, "_prep_pack_resolve_functional_extra_candidate", no_candidate,
    )
    patch_portraits_everywhere(monkeypatch, "persist_screenplay_character_resolutions", lambda *a, **k: [])

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
    # 2.0.0：segment_indexes 是逐段真实推导的，三个自然段依次对应旧版
    # ev_002/ev_008/ev_011 三个事件各自的原文位置，让每个群演标签的
    # segment_indexes 聚合仍然是可断言、有信息量的（不是随便给个占位段号）。
    source_text = (
        "孟浩来到外院，向养丹坊掌柜和宝阁执事分别行礼问候。"
        "\n\n"
        "孟浩与曹阳并肩走在外宗广场上，四周围观弟子纷纷侧目。"
        "\n\n"
        "曹阳身旁，一名外宗弟子低声议论，围观弟子越聚越多。"
    )
    characters, scene_list, props, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(
        conn, events=events,
        # 1.4.2 称谓证据闸只管走 portrait_id 解析的名字（孟浩/曹阳）；职业称谓
        # 类群演走 skip_character_names 分支，不要求逐字证据，不需要出现在这里。
        source_text=source_text,
    )

    assert errors == [], f"职业称谓类龙套不应阻断门禁：{errors}"
    assert stats["character_discovery_calls"] == 1

    by_name = {c["display_name"]: c for c in characters}
    assert "曹阳" in by_name, "同一次发现调用里的真新角色必须正常建卡入谱"
    assert by_name["曹阳"]["portrait_id"] == "cp-cy"

    extras_by_label = {e["label"]: e["segment_indexes"] for e in functional_extras}
    assert extras_by_label == {
        "养丹坊掌柜": [1],
        "宝阁执事": [1],
        "围观弟子": [2, 3],
        "一名外宗弟子": [3],
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

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", fake_not_person)
    patch_portraits_everywhere(monkeypatch, "persist_screenplay_character_resolutions", lambda *a, **k: [])
    events = [_event("ev_001", characters=[{"display_name": "天启宗", "is_background_extra": False}])]
    characters, scene_list, props, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(conn, events=events)

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
        # 出图已从映射台解耦到后台（见 screenplay_ops.background_portraits）：
        # 映射只建卡，定妆照由后台任务补，付费视频前由参考图就绪校验兜底。
        assert generate_portraits is False
        conn.execute(
            "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
            "VALUES ('cp-new','p1','沈青梧',2,NULL)"
        )
        conn.commit()
        _card_in_bible(conn, "p1", "沈青梧")
        _card_in_bible(conn, "p1", "沈青梧")
        return {"added": [{"name": "沈青梧"}], "resolutions": [], "errors": [], "skipped": [], "warnings": []}

    conn = _make_conn()
    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", fake_ensure_cards_for_text)
    patch_portraits_everywhere(monkeypatch, "persist_screenplay_character_resolutions", lambda *a, **k: [])

    events = [_event(
        "ev_001", characters=[{"display_name": "沈青梧", "is_background_extra": False}],
    )]
    characters, scene_list, props, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(
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
        "portrait_id": "cp-new", "segment_indexes": [1], "aliases": [],
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
    characters, scene_list, props, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(conn, events=events)

    assert calls["n"] == 1
    assert stats["scene_discovery_calls"] == 1
    assert errors == []
    assert scene_list == [{
        "scene_id": "scene:藏经阁", "display_name": "藏经阁",
        "scene_reference_id": "sr-new", "segment_indexes": [],
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
    characters, scene_list, props, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(conn, events=events)

    assert errors == []
    assert scene_list == [{
        "scene_id": "scene:宗门广场", "display_name": "宗门广场",
        "scene_reference_id": "sr1", "segment_indexes": [],
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

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", fake_functional)
    patch_portraits_everywhere(monkeypatch, "persist_screenplay_character_resolutions", lambda *a, **k: [])
    events = [_event("ev_001", characters=[{"display_name": "黑衣人", "is_background_extra": False}])]
    characters, scene_list, props, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(conn, events=events)

    assert errors == []
    assert characters == []
    assert functional_extras == [{
        "label": "黑衣人", "segment_indexes": [],
        "visual_entity_id": "entity:b645a470abc42e7e",
        "provenance": {
            "method": "discovery", "anchor_segments": [], "anchor_phrase": "",
            # 1.10.0 缺陷 A 顺带修复：候选集为空（bible 未注册任何角色），
            # 候选判别从未获得发起机会。
            "candidate_verdict_attempted": False,
        },
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

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", fake_not_person)
    patch_portraits_everywhere(monkeypatch, "persist_screenplay_character_resolutions", lambda *a, **k: [])
    events = [_event("ev_001", characters=[{"display_name": "天启宗", "is_background_extra": False}])]
    characters, scene_list, props, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(conn, events=events)

    assert errors == []
    assert characters == []
    assert functional_extras == []


def test_discovery_functional_verdict_yields_to_carded_bible_character(monkeypatch):
    """discovery 判 functional_identity，但同一次调用里它自己已经把这个人补录
    进人物谱并生成了定妆照——产物信号（谱内具名 + 本集有可绑定定妆照）压过那
    句判词，该提及必须走具名解析，不得落群演。

    真实事故（proj_195be7df1fd6 EP1「王有材」）：人物谱重生成后王有材一度不在
    谱内，映射台 pass1 解析不到他，discovery 判 functional_identity 的同时补录
    建卡（17:59:30 建卡完成）。旧代码里 `if name in skip_character_names:
    continue` 排在"已建卡就正常解析"这道救援之前，救援永远执行不到；一分钟后
    的候选判别又拿开跑时的旧 bible 快照构造候选集（「孟浩、小虎、许清」），把
    「王有材」这个标签拿去问模型"他是这三个里的哪一个"，模型只能答"都不是"，
    本人反倒落进 functional_extras。
    """
    conn = _make_conn()

    async def fake_discovery(project_id, episode_no, source_text, bible, *, generate_portraits=True):
        # 真实 ensure_cards_for_text 用同一个 conn 就地补录人物谱并建定妆照。
        conn.execute(
            "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
            "VALUES ('cp_wyc','p1','王有材',1,NULL)"
        )
        _seed_bible_characters(conn, "p1", [_bible_character("王有材")])
        return {
            "added": [], "skipped": [],
            "resolutions": [{
                "source_label": "王有材", "canonical_name": "王有材",
                "resolution": "functional_identity",
            }],
            "errors": [], "warnings": [],
        }

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", fake_discovery)
    patch_portraits_everywhere(monkeypatch, "persist_screenplay_character_resolutions", lambda *a, **k: [])
    events = [_event("ev_001", characters=[{"display_name": "王有材", "is_background_extra": False}])]
    characters, _scene_list, _props, functional_extras, errors, _stats, *_rest = _resolve(
        conn, events=events, source_text="王有材站在半山腰那里。",
    )

    assert errors == []
    assert functional_extras == []
    assert [c["display_name"] for c in characters] == ["王有材"]
    assert characters[0]["portrait_id"] == "cp_wyc"
    assert characters[0]["identity_id"] == "bible:王有材"


def test_discovery_non_person_skip_survives_carded_namesake(monkeypatch):
    """上一条的边界：discovery 明确判「非人」的标签，即使人物谱里恰好有同名
    具名角色且有定妆照，也不得被救回具名路线——宗门/器物不该绑定到任何真人。"""
    conn = _make_conn()

    async def fake_not_person(project_id, episode_no, source_text, bible, *, generate_portraits=True):
        conn.execute(
            "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
            "VALUES ('cp_ns','p1','天启宗',1,NULL)"
        )
        _seed_bible_characters(conn, "p1", [_bible_character("天启宗")])
        return {
            "added": [], "resolutions": [],
            "skipped": [{"status": "skipped_not_person", "name": "天启宗", "reason": "宗门非人"}],
            "errors": [], "warnings": [],
        }

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", fake_not_person)
    patch_portraits_everywhere(monkeypatch, "persist_screenplay_character_resolutions", lambda *a, **k: [])
    events = [_event("ev_001", characters=[{"display_name": "天启宗", "is_background_extra": False}])]
    characters, _scene_list, _props, functional_extras, errors, _stats, *_rest = _resolve(
        conn, events=events, source_text="天启宗坐落在山中。",
    )

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

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", fake_rename)
    patch_portraits_everywhere(monkeypatch, "persist_screenplay_character_resolutions", lambda *a, **k: [])
    events = [_event("ev_001", characters=[{"display_name": "神秘老者", "is_background_extra": False}])]
    characters, scene_list, props, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(
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
        "portrait_id": "cp1", "segment_indexes": [1], "aliases": ["神秘老者"],
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

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", failing_discovery)
    patch_portraits_everywhere(monkeypatch, "persist_screenplay_character_resolutions", lambda *a, **k: [])
    events = [_event(
        "ev_001",
        characters=[{"display_name": "神秘蒙面人", "is_background_extra": True}],
    )]
    characters, scene_list, props, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(conn, events=events)

    assert stats["character_discovery_calls"] == 1
    assert any("神秘蒙面人" in message for message in errors), (
        "discovery 明确点名失败的称谓必须在门禁错误里被具名报出，不能静默放行"
    )
    assert characters == []
    assert functional_extras == [], "明确失败的称谓不能被兜底成 functional_extras"


def test_scene_discovery_finding_nothing_degrades_instead_of_gate_fail(monkeypatch):
    """未解析场景不再整批门禁失败（WS6 追加：真实事故橘座在上 ×2/神墓 ×1）——降级见 test_coverage_ledger_honesty.py。"""
    conn = _make_conn()

    async def noop_scene_discovery(project_id, episode_no, labels):
        return {"added": [], "errors": [], "ready_scenes": [], "resolved_names": {}}

    monkeypatch.setattr(scenes, "ensure_scenes_for_labels", noop_scene_discovery)
    events = [_event("ev_001", scenes_=[{"display_name": "无名之地"}])]
    characters, scene_list, props, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(conn, events=events)

    assert stats["scene_discovery_calls"] == 1 and any("无名之地" in m for m in errors)
    entry = scene_list[0]
    assert (entry["unresolved"], entry["asset_required"], entry["scene_reference_id"]) == (True, False, None)
    assert entry["segment_indexes"] == [] and entry["reason"] == errors[0]


def test_discovery_error_entries_surface_in_final_gate_message(monkeypatch):
    """discovery 自己报出的具体失败原因不能被吞掉，必须能在最终门禁错误里查到。"""
    conn = _make_conn()

    async def failing_discovery(project_id, episode_no, source_text, bible, *, generate_portraits=True):
        return {
            "added": [], "resolutions": [], "skipped": [], "warnings": [],
            "errors": ["无名之人：新角色评估失败（诊断标记 ABC123）"],
        }

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", failing_discovery)
    patch_portraits_everywhere(monkeypatch, "persist_screenplay_character_resolutions", lambda *a, **k: [])
    events = [_event("ev_001", characters=[{"display_name": "无名之人", "is_background_extra": False}])]
    characters, scene_list, props, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(conn, events=events)

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

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", boom_character)

    events = [_event(
        "ev_008", characters=[{"display_name": "丹鬼", "is_background_extra": False}],
    )]
    characters, scene_list, props, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(
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
    characters, scene_list, props, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(
        conn, events=events,
        source_text="山顶上，两个老者盘膝而坐，笑眯眯地看着山下的广场。",
    )

    assert calls["n"] == 1  # 没证据的裸命中被当成未解析，触发了场景发现
    assert stats["scene_discovery_calls"] == 1
    assert errors == []
    assert scene_list == [{
        "scene_id": "scene:靠山宗外围山峰", "display_name": "靠山宗外围山峰",
        "scene_reference_id": "sr-new", "segment_indexes": [],
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
    """红灯（第29轮身份绑定审判程序，1.10.0 缺陷 A 修复后改写为候选判别 +
    双锚定钉证）：模型申报"灰袍老者"疑似真名"丹鬼"，本集原文没有"丹鬼"，
    但全书 chapters 扫描（卷宗检索不再局限于某个前瞻窗口）能找到第 6 章
    一句同时点出"灰袍老者"与"丹鬼"的揭示原文——卷宗非空且构成双锚定证据
    （dual_anchor_available），裁决模型独立选中候选"丹鬼"并钉住这条双锚定
    卷宗条目，钉证通过（引句同时逐字包含 alias 与 true_name）——核验通过，
    绑定到丹鬼已有的 portrait_id，走的是核验快车道，不再触发全量身份消歧
    模型调用，aliases=["灰袍老者"]，method 标注 resolution_forward（钉住的
    支撑句不落在本集自己的段落里），provenance.dual_anchor=True。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-dg','p1','丹鬼',1,NULL)"
    )
    conn.execute(
        "INSERT INTO episodes(project_id, episode_no, source_chapters) VALUES ('p1', 2, '[5]')"
    )
    conn.execute(
        "INSERT INTO chapters(project_id, idx, content) VALUES ('p1', 6, "
        "'灰袍老者缓缓摘下兜帽，露出真容，他正是丹鬼。')"
    )
    conn.commit()

    def boom_character(*_a, **_k):
        raise AssertionError("核验通过的假设应走确定性快车道，不应该再触发全量身份发现")

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", boom_character)

    verdict_calls = {"n": 0}

    async def fake_chat_structured(messages, **kwargs):
        verdict_calls["n"] += 1
        prompt = str(messages[0]["content"])
        assert "灰袍老者" in prompt and "丹鬼" in prompt
        assert "灰袍老者缓缓摘下兜帽" in prompt, "卷宗必须真的把第6章原文塞进裁决提示词"
        assert kwargs.get("output_schema")["properties"]["selected_candidate"]["enum"] == [
            "丹鬼", "都不是/无法确定",
        ]
        return prep_pack._PrepPackTrueNameVerdictResponse(
            selected_candidate="丹鬼", supporting_entry_index=1,
        )

    monkeypatch.setattr(prep_pack.model_gateway, "chat_structured", fake_chat_structured)

    events = [_event("ev_013", characters=[
        {"display_name": "灰袍老者", "is_background_extra": False, "suspected_true_name": "丹鬼"},
    ])]
    characters, scene_list, props, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(
        conn, events=events,
        source_text="山顶上，灰袍老者哈哈大笑起来。",
    )

    assert errors == []
    assert stats["character_discovery_calls"] == 0
    assert verdict_calls["n"] == 1
    assert characters == [{
        "identity_id": "bible:丹鬼", "display_name": "丹鬼",
        "portrait_id": "cp-dg", "segment_indexes": [1], "aliases": ["灰袍老者"],
        "visual_entity_id": "bible:丹鬼", "display_appellation": "灰袍老者",
        "provenance": {
            "method": "resolution_forward", "anchor_segments": [],
            # 第30轮 RCA 修正：anchor_phrase 记裁决钉住的支撑句本身（第29轮
            # 曾误写成空字符串），只有 anchor_segments（本地段号）合法留空。
            "anchor_phrase": "灰袍老者缓缓摘下兜帽，露出真容，他正是丹鬼。",
            # 第29轮：suspected_true_name 经身份绑定审判程序核验通过，但钉住
            # 的支撑句来自全书检索出的第 6 章（不是本集自己的段落）——method
            # 标注 resolution_forward，空锚合法豁免，附带裁决真正引用的章节号
            # 供审计核对（不是场景侧那批真正的空锚缺陷）。
            "forward_chapter_label": "第 6 章",
            # 1.10.0 缺陷 A：这条支撑句同时逐字包含 alias 与 true_name，是
            # 结构上可能存在的双锚定证据，钉证钉在了它上面，非退化路径。
            "dual_anchor": True,
        },
    }]
    assert any(
        h["status"] == "accepted" and h["mention"] == "灰袍老者"
        and h["suspected_true_name"] == "丹鬼" and h["dual_anchor"] is True
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
    characters, scene_list, props, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(
        conn, events=events,
        source_text="靠山宗四周的山峰上，两个老者盘膝而坐。",
    )

    assert calls["n"] == 1
    assert stats["scene_discovery_calls"] == 1
    assert errors == []
    assert scene_list == [{
        "scene_id": "scene:靠山宗外围山峰", "display_name": "靠山宗外围山峰",
        "scene_reference_id": "sr-new", "segment_indexes": [1],
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
# 2.0.2 -- 真实事故回归（48e01ff 当晚上线即复现，ERR-20260826-37cf79，
# EP1「大青山山顶」/「大青山半山腰裂缝」/「大青山脚下大河」/「大青山上空」/
# 「半山青石空地」五个场景全部 has_scene_anchor 门禁具名拦截）：2.0.0 砍掉
# event_chain 时，场景 resolution/discovery 两支的锚点候选只剩
# [canonical_scene_name, name]——EP1 这五个场景名都是模型综合出的合成
# 标签，原文只写"这青山顶端"/"山腰裂缝"等，两路候选逐字子串搜索结构上
# 必然全部落空。下面用真实 EP1 原文（data/manju.db，project_id=
# proj_3ac0b627fa46，chapters.idx=1）与真实事故的五个场景名构造红灯，
# 覆盖"合成场景名不逐字出现在原文，但模型自己申报的 quote 能取到独立的
# 本集锚点"这条——quote 不是 name/canonical_scene_name 的重复或变体
# （见 PREP_PACK_VERSION 上方 2.0.2 大注释"为什么这不是同义反复"一节），
# 是对"这段原文是不是在写这个地点"这个独立问题的另一次申报，真假由它
# 是否逐字命中原文决定，不是靠申报本身。
# ---------------------------------------------------------------------------

# 真实 EP1 第一章原文节录（chapters.idx=1，五段分别是五个受灾场景各自的
# 依据段落，逐字摘自 data/manju.db，不是编造的样例）。
_EP1_REAL_SOURCE_TEXT = (
    "四月的季节，说不出冷，也自然没有难熬的热，轻微的风抚过大地，掠过了"
    "北漠羌笛，吹过了东土大唐，掀起一些尘土如雾，在黄昏的夕阳下，转了个"
    "弯儿，卷在南域边缘赵国的大青山，落在了此刻于这青山顶端，坐在那里的"
    "一个文生少年身上。"
    "\n\n"
    "孟浩快走几步，到了山顶的边缘，向下看时，立刻看到在这峭壁的半山腰上，"
    "似乎存在了一处裂缝，有人从那里探出半个身子，面色苍白带着惊恐绝望，"
    "正在呼喊。"
    "\n\n"
    "青山下有一条大河，河水寒冬不冻，传说通往东土大唐。"
    "\n\n"
    "女子没有说话，右手抬起一挥，绿风再次出现，呼啸卷起孟浩以及王有材等"
    "人，与这女子一同飞出了洞穴，直奔天空而去，刹那不见了踪影，只有这大"
    "青山，依旧耸立，在这黄昏里渐渐融到了黑夜中。"
    "\n\n"
    "当他睁开眼睛时，已经在了一处半山腰的青石空地上，四周山峦起伏，云雾"
    "缭绕绝非凡尘，能看到一些精美的阁楼环绕山峦八方，满眼陌生。"
)

# 五个场景各自的 display_name（真实事故名单）、declared segment（1-based，
# 对应上面 _EP1_REAL_SOURCE_TEXT 的五段）、以及模型对这条提及自己申报的
# quote（逐字摘自对应段落，不是全段照抄——跟真实模型输出的摘录粒度一致）。
_EP1_REAL_SCENE_QUOTES = [
    ("大青山山顶", 1, "落在了此刻于这青山顶端，坐在那里的一个文生少年身上。"),
    ("大青山半山腰裂缝", 2, "在这峭壁的半山腰上，似乎存在了一处裂缝"),
    ("大青山脚下大河", 3, "青山下有一条大河，河水寒冬不冻"),
    ("大青山上空", 4, "与这女子一同飞出了洞穴，直奔天空而去"),
    ("半山青石空地", 5, "已经在了一处半山腰的青石空地上"),
]


def test_synthetic_scene_labels_get_independent_anchor_from_mention_quote(monkeypatch):
    """红灯（真实事故复现 + 修复验证，ERR-20260826-37cf79）：EP1 五个场景名
    对原文做裸字面搜索 100% 落空——如果修复退化回只试
    [canonical_scene_name, name]（2.0.0 砍 event_chain 后的回归态），本测试
    必须失败（_provenance_self_verify 非空、has_scene_anchor 具名拦截）。
    模拟真实链路：这五个场景在 EP1 里都是首次出现的新地名，走场景发现
    （ensure_scenes_for_labels 返回 resolved_names 自映射、未落 added——
    也就是"发现判定为已注册场景"这一支，跟真实事故报告里
    provenance.method='resolution' 完全对齐，不是随手选的另一条分支）。"""
    conn = _make_conn()
    for label, _segment, _quote in _EP1_REAL_SCENE_QUOTES:
        conn.execute(
            "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end) "
            "VALUES (?, 'p1', ?, 1, NULL)",
            (f"sr-{label}", label),
        )
    conn.commit()

    async def fake_ensure_scenes_for_labels(project_id, episode_no, labels):
        assert set(labels) == {label for label, _s, _q in _EP1_REAL_SCENE_QUOTES}
        return {
            "added": [],  # 已经在 scene_references 里挂号，不是"新建"
            "errors": [], "ready_scenes": list(labels),
            "resolved_names": {label: label for label in labels},
        }

    monkeypatch.setattr(scenes, "ensure_scenes_for_labels", fake_ensure_scenes_for_labels)

    # 直接构造扁平 scene_mentions（不走 _event()/_mentions_with_segment_
    # indexes 的裸字面自动定位——那条自动定位对这五个合成场景名会算出空
    # segment_indexes，掩盖了真实场景：生产链路里 segment_indexes 来自
    # 模型自己申报的"画面出场"编号，跟 display_name 是否逐字出现无关，见
    # _prep_pack_gate_segment_indexes 上方说明）。
    scene_mentions = [
        {"display_name": label, "segment_indexes": [segment], "quote": quote}
        for label, segment, quote in _EP1_REAL_SCENE_QUOTES
    ]

    characters, scene_list, props, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(
        conn, source_text=_EP1_REAL_SOURCE_TEXT,
        character_mentions=[], scene_mentions=scene_mentions, prop_mentions=[],
    )

    assert errors == []
    # 五个场景虽然已经在 scene_references 挂号（模拟"这不是这个项目第一次
    # 生成 EP1"——真实事故对照的老包 art_89068e6b5606 证明这五个场景名此前
    # 就已经存在），但场景证据闸（1.4.2，见上方"场景证据闸"注释）只信任
    # "经发现处理过"的绑定，裸直接命中且原文无字面证据时会被打回未解析、
    # 强制回炉发现——这正是真实事故报告 method='resolution'（不是
    # 'discovery'：发现判定"这不是新场景"，不落 added）的由来。
    assert stats["scene_discovery_calls"] == 1
    assert len(scene_list) == 5
    by_name = {s["display_name"]: s for s in scene_list}
    for label, segment, quote in _EP1_REAL_SCENE_QUOTES:
        entry = by_name[label]
        provenance = entry["provenance"]
        assert provenance["method"] == "resolution", (
            f"「{label}」真实事故报告里就是 method='resolution'"
        )
        assert provenance["anchor_phrase"] == quote, (
            f"「{label}」的 anchor_phrase 必须来自这条提及自己申报的 quote"
            "（canonical_scene_name/name 两路候选对这个合成标签结构上必然"
            "落空，不能是同义反复命中的假阳性）"
        )
        assert provenance["anchor_segments"] == [segment]
        assert label not in provenance["anchor_phrase"] or quote != label, (
            "quote 不能退化成场景名本身的复读"
        )

    verify_errors = _provenance_self_verify(
        _EP1_REAL_SOURCE_TEXT, characters, scene_list, functional_extras,
    )
    assert verify_errors == [], (
        "发布前来源证明自校验必须全绿——这正是真实事故里 RUNTIMEERROR 具名"
        "拦截的那道门禁"
    )


def test_synthetic_scene_label_without_any_independent_evidence_still_blocked(monkeypatch):
    """红灯（红线①反向验证：绝不允许放宽 has_scene_anchor 门禁）：合成场景名
    不逐字出现在原文，且这条提及自己也没有申报任何 quote（模型如实留空，
    不是漏填字段）——candidate 三路全空，anchor_phrase 必须仍是空字符串，
    has_scene_anchor 门禁必须照样具名拦截。修复绝不能让"quote 存在这个
    字段"本身变成放行条件，必须是"quote 逐字命中原文"才放行。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end) "
        "VALUES ('sr-dqssd','p1','大青山山顶',1,NULL)"
    )
    conn.commit()

    async def fake_ensure_scenes_for_labels(project_id, episode_no, labels):
        assert labels == ["大青山山顶"]
        return {
            "added": [], "errors": [], "ready_scenes": list(labels),
            "resolved_names": {label: label for label in labels},
        }

    monkeypatch.setattr(scenes, "ensure_scenes_for_labels", fake_ensure_scenes_for_labels)

    scene_mentions = [
        {"display_name": "大青山山顶", "segment_indexes": [1], "quote": ""},
    ]
    characters, scene_list, props, functional_extras, errors, stats, *_ = _resolve(
        conn, source_text=_EP1_REAL_SOURCE_TEXT,
        character_mentions=[], scene_mentions=scene_mentions, prop_mentions=[],
    )
    assert errors == []
    entry = next(s for s in scene_list if s["display_name"] == "大青山山顶")
    assert entry["provenance"]["anchor_phrase"] == ""
    verify_errors = _provenance_self_verify(
        _EP1_REAL_SOURCE_TEXT, characters, scene_list, functional_extras,
    )
    assert len(verify_errors) == 1
    assert "缺少 anchor_phrase" in verify_errors[0]
    assert "大青山山顶" in verify_errors[0]


# ---------------------------------------------------------------------------
# 2.0.5 -- 真实事故回归（ERR-20260901-2e124f，run_44dab0986f49，EP1「大青山
# 半山裂缝」）：同一个地点本集被提到两次，模型各自铸出了不同标签（跟真实
# 现场"大青山半山裂缝"/"大青山半山腰裂缝"一个降为别名同一个形状），经场景
# 发现统一判给了同一个规范场景——真实现场里其中一条提及自己的 quote 是
# 空字符串（provider_calls 逐字取证：那一遍 chunk 抽取的原始流式响应恰好
# 在这个场景的 quote 字段值之前被截断，触发的"只修 JSON 格式不改语义"
# 修复调用因为压根没见过这段内容，只能诚实地留空，不是模型拒绝引用），
# 而另一条姐妹提及确实申报了逐字命中原文的 quote——canonical_scene_name/
# name 两路合成标签结构上必然不逐字出现，旧实现只看"这一条提及自己的
# scene_quote"，三路全灭，has_scene_anchor 门禁具名拦截整集。
# ---------------------------------------------------------------------------

def test_scene_sibling_mention_quote_rescues_empty_quote_anchor(monkeypatch):
    """红灯（真实故障 ERR-20260901-2e124f 复现 + 修复验证）：把没带 quote
    的那条提及排在前面（_pass 内 scenes.setdefault 只认第一条写入的
    provenance），验证它自己的锚点计算真的用上了姐妹提及的引文，不是
    靠处理顺序侥幸绕过。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end) "
        "VALUES ('sr-dqsbc','p1','大青山半山裂缝',1,NULL)"
    )
    conn.commit()

    async def fake_ensure_scenes_for_labels(project_id, episode_no, labels):
        assert set(labels) == {"大青山半山腰裂缝", "半山裂缝处"}
        return {
            "added": [], "errors": [], "ready_scenes": list(labels),
            "resolved_names": {label: "大青山半山裂缝" for label in labels},
        }

    monkeypatch.setattr(scenes, "ensure_scenes_for_labels", fake_ensure_scenes_for_labels)

    scene_mentions = [
        {
            # 真实事故里被采纳、决定 provenance 的那条：quote 被传输/格式
            # 修复层丢空，不是模型没申报（模型确实被要求逐字引用）。
            "display_name": "半山裂缝处", "segment_indexes": [2], "quote": "",
        },
        {
            # 姐妹提及：同一处地点，真的逐字申报了原文引文。
            "display_name": "大青山半山腰裂缝", "segment_indexes": [2],
            "quote": "在这峭壁的半山腰上，似乎存在了一处裂缝",
        },
    ]
    characters, scene_list, props, functional_extras, errors, stats, *_ = _resolve(
        conn, source_text=_EP1_REAL_SOURCE_TEXT,
        character_mentions=[], scene_mentions=scene_mentions, prop_mentions=[],
    )
    assert errors == []
    assert len(scene_list) == 1, "两条提及必须归并成同一个规范场景条目"
    entry = scene_list[0]
    assert entry["display_name"] == "大青山半山裂缝"
    assert entry["provenance"]["method"] == "resolution"
    assert entry["provenance"]["anchor_phrase"] == "在这峭壁的半山腰上，似乎存在了一处裂缝", (
        "锚点必须来自姐妹提及真实申报的引文，不能是编造或空字符串"
    )
    assert entry["provenance"]["anchor_segments"] == [2]
    verify_errors = _provenance_self_verify(
        _EP1_REAL_SOURCE_TEXT, characters, scene_list, functional_extras,
    )
    assert verify_errors == [], (
        "带 quote 的姐妹提及必须能让没带 quote 的那条一并通过来源证明自校验"
    )


def test_scene_sibling_quote_does_not_leak_across_unrelated_scenes(monkeypatch):
    """红灯（红线反向验证：姐妹引文聚合必须按规范场景分组，不能全局共用）：
    两个互不相关的场景各出现一条提及，一条有 quote、一条没有——没 quote
    的那条绝不能借用另一个不相关场景的引文蒙混过关，必须仍然 fail closed。
    """
    conn = _make_conn()
    conn.execute(
        "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end) "
        "VALUES ('sr-a','p1','大青山半山裂缝',1,NULL)"
    )
    conn.execute(
        "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end) "
        "VALUES ('sr-b','p1','青山下大河',1,NULL)"
    )
    conn.commit()

    async def fake_ensure_scenes_for_labels(project_id, episode_no, labels):
        mapping = {"大青山半山腰裂缝": "大青山半山裂缝", "河边渡口": "青山下大河"}
        assert set(labels) == set(mapping)
        return {
            "added": [], "errors": [], "ready_scenes": list(labels),
            "resolved_names": {label: mapping[label] for label in labels},
        }

    monkeypatch.setattr(scenes, "ensure_scenes_for_labels", fake_ensure_scenes_for_labels)

    scene_mentions = [
        {"display_name": "河边渡口", "segment_indexes": [3], "quote": ""},
        {
            "display_name": "大青山半山腰裂缝", "segment_indexes": [2],
            "quote": "在这峭壁的半山腰上，似乎存在了一处裂缝",
        },
    ]
    characters, scene_list, props, functional_extras, errors, stats, *_ = _resolve(
        conn, source_text=_EP1_REAL_SOURCE_TEXT,
        character_mentions=[], scene_mentions=scene_mentions, prop_mentions=[],
    )
    assert errors == []
    river_entry = next(s for s in scene_list if s["display_name"] == "青山下大河")
    assert river_entry["provenance"]["anchor_phrase"] == "", (
        "「河边渡口」没有自己的独立证据，不能借用「大青山半山裂缝」的引文"
    )
    verify_errors = _provenance_self_verify(
        _EP1_REAL_SOURCE_TEXT, characters, scene_list, functional_extras,
    )
    assert len(verify_errors) == 1
    assert "缺少 anchor_phrase" in verify_errors[0]
    assert "青山下大河" in verify_errors[0]


# ---------------------------------------------------------------------------
# 1.5.0 -- speaker 名册引用化（真实 EP2 回归：关键台词"割舌头"的 speaker 被
# 写成"韩宗"，实际说话人是"绿袍男子"，韩宗第 5 章才出场，speaker 字段从未
# 进任何校验管线）。这两个函数是纯确定性查表，不需要 DB/异步，直接单测。
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

    async def fake_assess_new_scene(label, spatial_context, *, style, known_scenes, ep_label):
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

    monkeypatch.setattr(scenes, "get_conn", lambda: conn)
    monkeypatch.setattr(scenes, "assess_new_scene", fake_assess_new_scene)

    result = asyncio.run(scenes.ensure_scenes_for_labels("p1", 7, ["洞府"]))

    assert result["errors"] == [], result["errors"]
    assert result["resolved_names"] == {"洞府": "南峰山脚洞府"}, result["resolved_names"]
    # 出图已解耦到后台（见 app/scenes.py::ensure_scenes_for_labels 尾段
    # 说明），本函数不再内联等图落盘，返回值里也不再有 ready_scenes——本测试
    # 只验证历史别名平局下 resolved_names 仍能正确落到 direct_resolutions
    # 的裁决结果，不是这里的断言重点。


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

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", boom_character)

    events = [_event("ev_010", characters=[
        {"display_name": "小胖子", "is_background_extra": False},
    ])]
    (
        characters, scene_list, props, functional_extras, errors, stats,
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

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", fake_disambiguate)
    patch_portraits_everywhere(monkeypatch, "persist_screenplay_character_resolutions", lambda *a, **k: [])

    events = [_event("ev_010", characters=[
        {"display_name": "小胖子", "is_background_extra": False},
    ])]
    (
        characters, scene_list, props, functional_extras, errors, stats,
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

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", boom_character)

    events = [_event("ev_001", characters=[
        {"display_name": "小胖子", "is_background_extra": False},
    ])]
    (
        characters, scene_list, props, functional_extras, errors, stats,
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

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", boom_character)

    events = [_event("ev_010", characters=[
        {"display_name": "小胖子", "is_background_extra": False},
    ])]
    (
        characters, scene_list, props, functional_extras, errors, stats,
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

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", fake_disambiguate)
    patch_portraits_everywhere(monkeypatch, "persist_screenplay_character_resolutions", lambda *a, **k: [])

    # 1.8.0：discovery 判定 skip 后，"小胖子"落入未解析角色标签候选判别
    # （_prep_pack_resolve_functional_extra_candidate）——它字面命中了
    # 李富贵、王有材两个人的已确认别名，两者都会入选候选集，真的会发起一次
    # 候选判别模型调用。这条红灯只关心"矛盾不能静默猜绑"这个安全默认，
    # 如实模拟模型在证据不足以区分时诚实回答"都不是/无法确定"（原文只有
    # "小胖子憨憨一笑，抓了抓头。"一句，确实分不出是谁）——候选判别机制
    # 因此也维持"不确定不绑"的原行为，两道防线殊途同归。
    async def fake_chat_structured(messages, **kwargs):
        return prep_pack._PrepPackFunctionalCandidateVerdict(
            selected_candidate="都不是/无法确定", supporting_segment_index=1,
        )

    monkeypatch.setattr(prep_pack.model_gateway, "chat_structured", fake_chat_structured)

    events = [_event("ev_010", characters=[
        {"display_name": "小胖子", "is_background_extra": False},
    ])]
    (
        characters, scene_list, props, functional_extras, errors, stats,
        true_name_hints, scene_alias_anchors, rejected_alias_conflicts,
    ) = _resolve(
        conn, events=events,
        source_text="小胖子憨憨一笑，抓了抓头。",
    )

    assert stats["character_discovery_calls"] == 1, "人物谱矛盾不能静默猜绑，必须回炉发现"
    assert not any(c["display_name"] in {"李富贵", "王有材"} for c in characters)
    assert rejected_alias_conflicts, "人物谱内部的矛盾别名必须留痕"
    assert any(e["label"] == "小胖子" for e in functional_extras), (
        "候选判别在证据不足时也必须维持原行为，落 functional_extras"
    )


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

    patch_prep_pack_everywhere(monkeypatch, "_generate_prep_pack_once", fake_generate_once)

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

    patch_prep_pack_everywhere(monkeypatch, "_generate_prep_pack_once", fake_generate_once)

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

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", fake_disambiguate)
    patch_portraits_everywhere(monkeypatch, "persist_screenplay_character_resolutions", lambda *a, **k: [])

    events = [_event("ev_001", characters=[
        {"display_name": "穿杂役衫的魁梧大汉", "is_background_extra": False},
    ])]
    (
        characters, scene_list, props, functional_extras, errors, stats,
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

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", failing_discovery)
    patch_portraits_everywhere(monkeypatch, "persist_screenplay_character_resolutions", lambda *a, **k: [])

    events = [_event("ev_001", characters=[
        {"display_name": "穿杂役衫的魁梧大汉", "is_background_extra": False},
    ])]
    (
        characters, scene_list, props, functional_extras, errors, stats,
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
        source_text,
    )


def test_provenance_direct_method_self_verifies():
    """红灯 a（method=direct）：裸命中角色的 provenance 是
    {method:"direct", anchor_segments:[段号], anchor_phrase:称谓}，自校验
    通过。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp1','p1','甲一',1,NULL)"
    )
    conn.commit()
    source_text = "甲一快步穿过宗门广场，众弟子纷纷让路。"
    events = [_event("ev_001", characters=[
        {"display_name": "甲一", "is_background_extra": False},
    ])]
    characters, scene_list, props, functional_extras, errors, stats, *_ = _resolve(
        conn, events=events, source_text=source_text,
    )
    assert errors == []
    xiao_yan = next(c for c in characters if c["display_name"] == "甲一")
    assert xiao_yan["provenance"] == {
        "method": "direct", "anchor_segments": [1], "anchor_phrase": "甲一",
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

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", boom_character)

    source_text = "小胖子憨憨一笑，抓了抓头。"
    events = [_event("ev_010", characters=[
        {"display_name": "小胖子", "is_background_extra": False},
    ])]
    characters, scene_list, props, functional_extras, errors, stats, *_ = _resolve(
        conn, events=events, source_text=source_text,
    )
    assert errors == []
    lfg = next(c for c in characters if c["display_name"] == "李富贵")
    assert lfg["provenance"] == {
        "method": "alias", "anchor_segments": [1], "anchor_phrase": "小胖子",
    }
    assert _provenance_self_verify(source_text, characters, scene_list, functional_extras) == []


def test_provenance_scene_alias_hit_with_no_independent_evidence_stays_alias_inherited():
    """红灯 a（第30轮②的历史根因，2.0.0 起行为改变，见下方说明）：跨集别名
    命中的场景（本集只写了已确认别名"杂役们住的地方"，canonical 规范名
    "杂役处居所内"本身不逐字出现在本集原文）——第30轮②当年能把这种情形
    升级成 method="resolution"，靠的是该场景所涉**事件**自己 source_evidence
    里独立于别名本身之外的地点描述短语。event_chain 在 2.0.0 被撤销后，
    "事件的 source_evidence"这个证据来源不复存在（见 app/production/
    prep_pack.py 模块 docstring 的 2.0.0 说明，_prep_pack_scene_alias_
    provenance 的第三个候选参数固定传空列表）——本场景不再有独立于别名本身
    的额外证据可用，诚实退回 method="alias_inherited"（第30轮②本来就设计
    的"真没有独立证据就诚实改标，不伪造锚点"分支），不是回归缺陷：宁可少
    一次升级到 resolution 的精确分类，也不凭空捏造一个不存在的锚点。"""
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
    events = [_event("ev_020", scenes_=[{"display_name": "杂役们住的地方"}])]
    characters, scene_list, props, functional_extras, errors, stats, *_ = _resolve(
        conn, events=events, source_text=source_text,
    )
    assert errors == []
    entry = next(s for s in scene_list if s["scene_reference_id"] == "sr1")
    assert entry["display_name"] == "杂役处居所内"
    assert entry["segment_indexes"] == [1]
    assert entry["provenance"] == {
        "method": "alias_inherited", "anchor_segments": [], "anchor_phrase": "",
        "source_episode_no": 1,
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
    characters, scene_list, props, functional_extras, errors, stats, *_ = _resolve(
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

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", fake_rename)
    patch_portraits_everywhere(monkeypatch, "persist_screenplay_character_resolutions", lambda *a, **k: [])
    source_text = "一名神秘老者悄然现身，气息深不可测。"
    events = [_event("ev_001", characters=[
        {"display_name": "神秘老者", "is_background_extra": False},
    ])]
    characters, scene_list, props, functional_extras, errors, stats, *_ = _resolve(
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
        _card_in_bible(conn, "p1", "沈青梧")
        return {"added": [{"name": "沈青梧"}], "resolutions": [], "errors": [], "skipped": [], "warnings": []}

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", fake_ensure_cards_for_text)
    patch_portraits_everywhere(monkeypatch, "persist_screenplay_character_resolutions", lambda *a, **k: [])
    source_text = "沈青梧提剑而立，目光冷冽。"
    events = [_event("ev_001", characters=[
        {"display_name": "沈青梧", "is_background_extra": False},
    ])]
    characters, scene_list, props, functional_extras, errors, stats, *_ = _resolve(
        conn, events=events, source_text=source_text,
    )
    assert errors == []
    shen = next(c for c in characters if c["display_name"] == "沈青梧")
    assert shen["provenance"] == {
        "method": "discovery", "anchor_segments": [1], "anchor_phrase": "沈青梧",
    }
    assert _provenance_self_verify(source_text, characters, scene_list, functional_extras) == []


def test_provenance_anchor_mismatch_blocks_publish():
    """红灯 b：anchor_phrase 不在 anchor_segments 指定的原文段里——自校验
    必须失败并具名报出，供 _generate_prep_pack_once 在发布前门禁拦截。"""
    source_text = "甲一快步穿过宗门广场，众弟子纷纷让路。"
    segments = prep_pack.index_source_segments(source_text)
    tampered_characters = [{
        "identity_id": "bible:甲一", "display_name": "甲一",
        "portrait_id": "cp1", "event_ids": ["ev_001"], "aliases": [],
        "provenance": {
            "method": "direct", "anchor_segments": [1], "anchor_phrase": "根本不存在的短语",
        },
    }]
    errors = prep_pack._prep_pack_verify_manifest_provenance(
        segments, {"characters": tampered_characters, "scenes": [], "functional_extras": []},
    )
    assert errors, "anchor_phrase 未逐字命中所指段落必须门禁拦截"
    assert any("甲一" in message and "根本不存在的短语" in message for message in errors)


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


# 《黄英》EP1 原文的真实形状：一句被段落分隔切成两截的叙述。
_PARAGRAPH_BREAK_SOURCE = (
    "那客人多方替他寻求，终于弄到两株嫩芽。马子才像珍宝一样把它们包裹收藏好，踏上归途。"
    "\n\n"
    "走到半路，他遇见一位少年。少年骑着一头小毛驴，跟随在一辆青碧色的车子旁边。"
)
# 模型申报的引文：逐字抄自原文，只是没抄那个段落分隔。
_CROSS_PARAGRAPH_QUOTE = "马子才像珍宝一样把它们包裹收藏好，踏上归途。走到半路，他遇见一位少年"


def test_quote_spanning_a_paragraph_break_still_anchors():
    """引文横跨自然段时，段间那个换行不该让整条绑定失去本集依据。

    真实故障 ERR-20260828-16ce45（run_1a3721d1a9c2 / ep_cae1ede1c62f，《黄英》
    EP1）：场景「郊野归途路径」是模型综合出的合成名，原文里不会有这四个字，
    三路候选（canonical_scene_name / name / quote）里唯一能落地的就是它自己
    申报的 quote。那条 quote 每个字都抄自原文，只是跨过了一个段落分隔——两句
    话在原文里分属相邻两段，模型抄下来时连着写，于是它既不在第一段里也不在
    第二段里。锚点落空，整集映射被"resolution 绑定必须有本集文本依据"拦停，
    重试必然复现。
    """
    segments = prep_pack.index_source_segments(_PARAGRAPH_BREAK_SOURCE)
    assert len(segments) == 2, "夹具必须真的被切成两段才有意义"
    assert not any(
        _CROSS_PARAGRAPH_QUOTE in segment.text for segment in segments
    ), "夹具必须真的跨段：任何单段都不完整包含这条引文"

    located, matched = prep_pack._prep_pack_locate_phrase(
        segments, _CROSS_PARAGRAPH_QUOTE,
    )

    assert located == [1, 2], "跨段引文要定位到它真正覆盖的那几段"
    assert matched == _CROSS_PARAGRAPH_QUOTE, "这条引文本身就落在原文里，不该被改写"
    anchor_segments, anchor_phrase = prep_pack._prep_pack_local_text_anchor(
        segments, ["郊野归途路径", "郊野归途路径", _CROSS_PARAGRAPH_QUOTE],
    )
    assert anchor_segments == [1, 2]
    assert anchor_phrase == _CROSS_PARAGRAPH_QUOTE


def test_paragraph_break_tolerance_still_rejects_invented_quotes():
    """抹掉的只有空白：改写过、编造的、张冠李戴的引文照样定位不到。

    这条守的是上一条修复的边界——如果为了让跨段引文过关而放宽到"差不多就行"，
    这道闸就不再拦幻觉了。段落分隔是排版，字和标点都还是逐字要求。
    """
    segments = prep_pack.index_source_segments(_PARAGRAPH_BREAK_SOURCE)

    invented = [
        # 完全编造。
        "马子才骑着毛驴独自返回了顺天府",
        # 改写：原文是"他遇见一位少年"，这里换了词。
        "马子才像珍宝一样把它们包裹收藏好，踏上归途。走到半路，他碰上一位少年",
        # 标点被改：原文两句之间是句号。
        "马子才像珍宝一样把它们包裹收藏好，踏上归途，走到半路，他遇见一位少年",
        # 顺序颠倒：两句都在原文里，但不是这个次序。
        "走到半路，他遇见一位少年。马子才像珍宝一样把它们包裹收藏好，踏上归途。",
    ]
    for quote in invented:
        assert prep_pack._prep_pack_locate_phrase(segments, quote) == ([], ""), (
            f"这条引文不该被定位到：{quote}"
        )
        assert prep_pack._prep_pack_local_text_anchor(segments, [quote]) == ([], "")


# 《王六郎》EP1 原文的真实形状：一段长对白，第一个句号之后还有四十多字，
# 收尾引号在整段的最末尾。
_TRUNCATED_QUOTATION_SOURCE = (
    "许某回家后，立即想收拾行装，向东出发。妻子笑他说：“这一去有几百里。"
    "即使真有那个地方，只怕泥塑的神像也不能同你说话。”许某不听，终于来到招远县。"
)
# 模型申报的引文：抄到句号就停笔，然后自己补了一个收尾引号。
_MODEL_CLOSED_THE_QUOTE = "许某回家后，立即想收拾行装，向东出发。妻子笑他说：“这一去有几百里。”"


def test_quote_the_model_closed_early_still_anchors():
    """模型引到半句就补上收尾引号时，那一个引号不该让整条绑定失去本集依据。

    真实故障 ERR-20260828-91bc95（run_4ef5e3935554 / ep_17fb1391f17f，《王六郎》
    EP1）：「许姓人家居所」「邬镇土地祠内」两个场景都是合成名，原文里没有这几个
    字，三路候选里唯一能落地的就是模型申报的 quote。它抄到句号停笔，补了一个原文
    那个位置没有的 ”——原文里那个 ” 在四十多字之后。两个场景同时落空，整集映射被
    「缺少 anchor_phrase」拦停；两次调用都补了引号，重试必然复现。

    返回的 anchor_phrase 必须是剥掉引号之后、真正落在原文里的那个串：它会被自校验
    和审计脚本拿回原文逐字复核，存原串等于把这道闸留给下一次运行去撞。
    """
    segments = prep_pack.index_source_segments(_TRUNCATED_QUOTATION_SOURCE)
    assert _MODEL_CLOSED_THE_QUOTE not in _TRUNCATED_QUOTATION_SOURCE, (
        "夹具必须真的对不上：模型补的那个引号在原文这个位置不存在"
    )
    # 修复前整个定位就只有这一层（严格逐字 + 跨段去空白），它照旧不命中——
    # 事故当时落空的正是这里，剥引号是加在它后面的一次退让，不是替换它。
    assert prep_pack._prep_pack_locate_verbatim(segments, _MODEL_CLOSED_THE_QUOTE) == []

    located, matched = prep_pack._prep_pack_locate_phrase(
        segments, _MODEL_CLOSED_THE_QUOTE,
    )

    assert located == [1]
    assert matched == _MODEL_CLOSED_THE_QUOTE.rstrip("”")
    assert matched in _TRUNCATED_QUOTATION_SOURCE, (
        "落库的 anchor_phrase 必须逐字存在于原文，否则自校验与审计都会判红"
    )
    anchor_segments, anchor_phrase = prep_pack._prep_pack_local_text_anchor(
        segments, ["许姓人家居所", "许姓人家居所", _MODEL_CLOSED_THE_QUOTE],
    )
    assert (anchor_segments, anchor_phrase) == (located, matched)
    assert prep_pack._prep_pack_verify_manifest_provenance(
        segments,
        {
            "characters": [], "functional_extras": [],
            "scenes": [{
                "display_name": "许姓人家居所",
                "provenance": {
                    "method": "resolution",
                    "anchor_segments": anchor_segments,
                    "anchor_phrase": anchor_phrase,
                },
            }],
        },
    ) == [], "生成侧算出来的锚点必须能过自己那道自校验"


def test_stripping_quotation_marks_still_rejects_invented_quotes():
    """剥掉的只有两端引号：中间少一个字、改一个词，照样定位不到。

    这条守的是上一条修复的边界。补全引号是引用这个动作的一部分，放宽的只有
    引文两端那对符号；被引用的内容仍然逐字要求，否则这道闸就不再拦幻觉了。
    """
    segments = prep_pack.index_source_segments(_TRUNCATED_QUOTATION_SOURCE)

    invented = [
        # 剥掉引号后仍是编造：原文是"向东出发"。
        "“许某回家后，立即想收拾行装，向西出发。”",
        # 引文内部的引号不剥：这里把原文中间那个 “ 抹掉了。
        "许某回家后，立即想收拾行装，向东出发。妻子笑他说：这一去有几百里。",
        # 两端都是引号，但中间的话根本不在原文里。
        "“泥塑的神像也不能同你说话，何况几百里。”",
        # 退化输入：剥完什么都不剩，不能命中任何东西。
        "“”",
    ]
    for quote in invented:
        assert prep_pack._prep_pack_locate_phrase(segments, quote) == ([], ""), (
            f"这条引文不该被定位到：{quote}"
        )
        assert prep_pack._prep_pack_local_text_anchor(segments, [quote]) == ([], "")


def test_self_verify_matches_the_audit_scripts_anchor_rule():
    """自校验与外部审计必须用同一条锚点判据，且照旧核对声明的段号。

    scripts/episode_source_audit.py::_verify_provenance_anchor 一直是"把声明
    的那几段按序拼起来再查"（anchor_segments 本就是复数）；自校验却停在"任一
    单段内命中"。跨段引文因此在两边得出相反结论——生成侧放行、审计侧判红，
    同一份数据两个真源打架。这条测试把两边钉在一起。
    """
    segments = prep_pack.index_source_segments(_PARAGRAPH_BREAK_SOURCE)

    def _verify(anchor_segments):
        return prep_pack._prep_pack_verify_manifest_provenance(
            segments,
            {
                "characters": [], "functional_extras": [],
                "scenes": [{
                    "display_name": "郊野归途路径",
                    "provenance": {
                        "method": "resolution",
                        "anchor_segments": anchor_segments,
                        "anchor_phrase": _CROSS_PARAGRAPH_QUOTE,
                    },
                }],
            },
        )

    assert _verify([1, 2]) == [], "两段都声明上的跨段锚点要放行"
    for incomplete in ([1], [2], [9]):
        blocked = _verify(incomplete)
        assert blocked, f"声明 {incomplete} 装不下这条引文，必须照旧拦截"
        assert any("郊野归途路径" in message for message in blocked)

    # 生成侧算出来的段号本身就是完整覆盖的，所以这条路走不到上面的拦截。
    anchor_segments, anchor_phrase = prep_pack._prep_pack_local_text_anchor(
        segments, [_CROSS_PARAGRAPH_QUOTE],
    )
    assert _verify(anchor_segments) == []
    assert anchor_phrase == _CROSS_PARAGRAPH_QUOTE


def test_self_verify_agrees_with_the_audit_script_on_the_same_anchor():
    """同一条 provenance 送进两套核验，结论必须一致。

    这里直接引入审计脚本自己的函数比对，不是复述它的规则——转述会失真，
    而这两套判据一旦漂移，就会出现"发布时通过、审计时判红"的死结。
    """
    from scripts.episode_source_audit import _verify_provenance_anchor

    segments = prep_pack.index_source_segments(_PARAGRAPH_BREAK_SOURCE)
    for anchor_segments in ([1, 2], [1], [2], [9]):
        provenance = {
            "method": "resolution",
            "anchor_segments": anchor_segments,
            "anchor_phrase": _CROSS_PARAGRAPH_QUOTE,
        }
        audit_ok, _reason = _verify_provenance_anchor(segments, provenance)
        self_verify_errors = prep_pack._prep_pack_verify_manifest_provenance(
            segments,
            {
                "characters": [], "functional_extras": [],
                "scenes": [{"display_name": "郊野归途路径", "provenance": provenance}],
            },
        )
        assert audit_ok == (self_verify_errors == []), (
            f"anchor_segments={anchor_segments} 上两套核验结论不一致："
            f"审计 ok={audit_ok}，自校验 errors={self_verify_errors}"
        )


def test_provenance_missing_field_on_legacy_manifest_does_not_crash():
    """红灯 c（前端兼容/旧包兼容）：provenance 是新增可选字段——完全没有这
    个字段的旧 manifest（1.5.x 之前发布的包）传进自校验，必须优雅跳过，不
    崩溃、不报错，payload 冻结纪律没有被打破。"""
    source_text = "占位原文。"
    segments = prep_pack.index_source_segments(source_text)
    legacy_manifest = {
        "characters": [{
            "identity_id": "bible:甲一", "display_name": "甲一",
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
        # 1.10.0 缺陷 A：是非题改候选判别——候选集里唯一的候选就是假设本身
        # "王有材"（roster 未注册其它角色），列举反证段落明确把"小胖子"跟
        # "王有材"并列举成两个不同的人，模型据此选"都不是/无法确定"，不是
        # 勉强确认候选。
        return prep_pack._PrepPackTrueNameVerdictResponse(
            selected_candidate="都不是/无法确定", supporting_entry_index=1,
        )

    monkeypatch.setattr(prep_pack.model_gateway, "chat_structured", fake_chat_structured)

    async def fake_ensure_cards_for_text(project_id, episode_no, source_text, bible, *, generate_portraits=True):
        conn.execute(
            "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
            "VALUES ('cp-xpz','p1','小胖子',2,NULL)"
        )
        conn.commit()
        return {"added": [{"name": "小胖子"}], "resolutions": [], "errors": [], "skipped": [], "warnings": []}

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", fake_ensure_cards_for_text)
    patch_portraits_everywhere(monkeypatch, "persist_screenplay_character_resolutions", lambda *a, **k: [])

    events = [_event("ev_099", characters=[
        {"display_name": "小胖子", "is_background_extra": False, "suspected_true_name": "王有材"},
    ])]
    (
        characters, scene_list, props, functional_extras, errors, stats,
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
        "status": "rejected", "reason": "rejected_verdict_uncertain",
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
        # 1.10.0 缺陷 A：是非题改候选判别——alias 本身包含 true_name 子串，
        # 这条卷宗记录天然构成"双锚定"（正是本用例要测的误绑陷阱：字面包含
        # 不等于身份链接），模型据此选"都不是/无法确定"，不是勉强确认候选。
        return prep_pack._PrepPackTrueNameVerdictResponse(
            selected_candidate="都不是/无法确定", supporting_entry_index=1,
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

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", fake_ensure_cards_for_text)
    patch_portraits_everywhere(monkeypatch, "persist_screenplay_character_resolutions", lambda *a, **k: [])

    events = [_event("ev_100", characters=[
        {
            "display_name": "上官修身边的男子", "is_background_extra": False,
            "suspected_true_name": "上官修",
        },
    ])]
    (
        characters, scene_list, props, functional_extras, errors, stats,
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
        "status": "rejected", "reason": "rejected_verdict_uncertain",
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
        # 1.10.0 缺陷 A：这句原文同时逐字包含 alias"小胖子"与 true_name
        # "李富贵"，是结构上真实存在的双锚定证据，候选判别选中"李富贵"、
        # 钉证钉在这条双锚定条目上。
        return prep_pack._PrepPackTrueNameVerdictResponse(
            selected_candidate="李富贵", supporting_entry_index=1,
        )

    monkeypatch.setattr(prep_pack.model_gateway, "chat_structured", fake_chat_structured)

    def boom_character(*_a, **_k):
        raise AssertionError("核验通过的假设应走确定性快车道，不应该再触发全量身份发现")

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", boom_character)

    events = [_event("ev_101", characters=[
        {"display_name": "小胖子", "is_background_extra": False, "suspected_true_name": "李富贵"},
    ])]
    (
        characters, scene_list, props, functional_extras, errors, stats,
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
        "portrait_id": "cp-lfg", "segment_indexes": [1], "aliases": ["小胖子"],
        "visual_entity_id": "bible:李富贵", "display_appellation": "小胖子",
        "provenance": {
            "method": "resolution_forward", "anchor_segments": [],
            # 第30轮 RCA 修正：anchor_phrase 记裁决钉住的支撑句本身。
            "anchor_phrase": "他是当年的小胖子，李富贵。",
            "forward_chapter_label": "第 692 章",
            "dual_anchor": True,
        },
    }]
    assert any(
        h["status"] == "accepted" and h["mention"] == "小胖子"
        and h["suspected_true_name"] == "李富贵" and h["dual_anchor"] is True
        for h in true_name_hints
    )


def test_true_name_dossier_trial_rejects_unpinnable_entry_index_anti_forgery(monkeypatch):
    """红灯 d（防编证词，1.10.0 缺陷 A 修复后钉证机制改为段号钉证，见
    PREP_PACK_VERSION 上方大注释——旧版逐字引句比对被真实生产数据证明会
    因模型转录噪音系统性误杀，改为结构性段号核验）：裁决模型选中了正确
    候选"李富贵"，但引用的 supporting_entry_index 不在本次卷宗实际收录的
    候选编号集合内（协议层 enum 已经不允许，这里模拟 provider 未必遵守
    enum 的防御性核验）——钉证（_prep_pack_true_name_pin_dossier_entry）
    必须失败，reason=rejected_entry_not_pinned，绝不能仅凭模型选中正确
    候选就采信，还必须验证它真的钉在卷宗里真实存在的条目上。"""
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
            selected_candidate="李富贵",
            # 卷宗只有 1 条记录（entry_index=1）——99 不在集合内，模拟
            # provider 未遵守协议层 enum 约束的防御性场景。
            supporting_entry_index=99,
        )

    monkeypatch.setattr(prep_pack.model_gateway, "chat_structured", fake_chat_structured)

    async def fake_ensure_cards_for_text(project_id, episode_no, source_text, bible, *, generate_portraits=True):
        conn.execute(
            "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
            "VALUES ('cp-xpz','p1','小胖子',2,NULL)"
        )
        conn.commit()
        return {"added": [{"name": "小胖子"}], "resolutions": [], "errors": [], "skipped": [], "warnings": []}

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", fake_ensure_cards_for_text)
    patch_portraits_everywhere(monkeypatch, "persist_screenplay_character_resolutions", lambda *a, **k: [])

    events = [_event("ev_102", characters=[
        {"display_name": "小胖子", "is_background_extra": False, "suspected_true_name": "李富贵"},
    ])]
    (
        characters, scene_list, props, functional_extras, errors, stats,
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
        "status": "rejected", "reason": "rejected_entry_not_pinned",
    }]
    assert not any(c["portrait_id"] == "cp-lfg" for c in characters), (
        "编造的证词不得被钉证接受，绝不能因模型口头声称选中正确候选就静默采信"
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
            selected_candidate="都不是/无法确定", supporting_entry_index=1,
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

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", fake_ensure_cards_for_text)
    patch_portraits_everywhere(monkeypatch, "persist_screenplay_character_resolutions", lambda *a, **k: [])

    events = [_event("ev_001", characters=[
        {"display_name": "小胖子", "is_background_extra": False, "suspected_true_name": "李富贵"},
    ])]
    (
        characters, scene_list, props, functional_extras, errors, stats,
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
            selected_candidate="无极峰绝顶", supporting_entry_index=1,
        )

    monkeypatch.setattr(prep_pack.model_gateway, "chat_structured", fake_chat_structured)

    async def boom_scene(*_a, **_k):
        raise AssertionError("核验通过的假设应走确定性快车道，不应该再触发场景发现")

    monkeypatch.setattr(scenes, "ensure_scenes_for_labels", boom_scene)

    events = [_event("ev_201", scenes_=[
        {"display_name": "荒地", "suspected_true_name": "无极峰绝顶"},
    ])]
    characters, scene_list, props, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(
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
            selected_candidate="石大山", supporting_entry_index=1,
        )

    monkeypatch.setattr(prep_pack.model_gateway, "chat_structured", fake_chat_structured)

    async def boom_character(*_a, **_k):
        raise AssertionError("核验通过的假设应走确定性快车道，不应该再触发角色发现")

    async def boom_scene(*_a, **_k):
        raise AssertionError("核验通过的假设应走确定性快车道，不应该再触发场景发现")

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", boom_character)
    monkeypatch.setattr(scenes, "ensure_scenes_for_labels", boom_scene)

    events = [_event(
        "ev_401",
        characters=[{
            "display_name": "阿石", "is_background_extra": False,
            "suspected_true_name": "石大山",
        }],
        scenes_=[{"display_name": "阿石", "suspected_true_name": "石大山"}],
    )]
    characters, scene_list, props, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(
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
# 1.10.0（缺陷 A：真名裁决从是非题改候选判别，钉证要求引句含 alias 本身，
# 双锚定优先、结构上不存在双锚定证据时退化为集内指代段落且可观测标记，见
# PREP_PACK_VERSION 上方 1.10.0 大注释）。下面四条红灯用真实生产数据坐实
# 过的失败/成功形状覆盖新钉证规则的四个分支。
# ---------------------------------------------------------------------------

def test_true_name_verdict_rejects_pinned_entry_missing_alias_real_data_shape(monkeypatch):
    """红灯（"零保护"形状，真实数据坐实：114 条真实 same 判决里 56 条支撑句
    缺 alias/true_name 至少一个，明确询问人名的 75 条里 2 条连 alias 本身
    都不含）：裁决模型选中了正确候选"王有材"，钉证段号本身合法（真实落在
    卷宗集合内），但钉住的那条卷宗记录通篇只提到"王有材"，一个字都没提到
    待判别名"小胖子"——这条"证据"证不出"小胖子就是王有材"，必须拒绝，
    reason=rejected_pinned_entry_missing_alias，不能仅凭模型选对候选就
    采信。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-wyc','p1','王有材',1,NULL)"
    )
    conn.execute(
        "INSERT INTO chapters(project_id, idx, content) VALUES ('p1', 15, ?)",
        ("王有材是个内门弟子，为人低调，从不与人争执。",),
    )
    conn.commit()

    verdict_calls = {"n": 0}

    async def fake_chat_structured(messages, **kwargs):
        verdict_calls["n"] += 1
        return prep_pack._PrepPackTrueNameVerdictResponse(
            selected_candidate="王有材", supporting_entry_index=1,
        )

    async def fake_discovery(project_id, episode_no, source_text, bible, *, generate_portraits=True):
        return {"added": [], "resolutions": [], "errors": [], "skipped": [], "warnings": []}

    monkeypatch.setattr(prep_pack.model_gateway, "chat_structured", fake_chat_structured)
    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", fake_discovery)
    patch_portraits_everywhere(monkeypatch, "persist_screenplay_character_resolutions", lambda *a, **k: [])

    events = [_event("ev_301", characters=[
        {"display_name": "小胖子", "is_background_extra": False, "suspected_true_name": "王有材"},
    ])]
    (
        characters, scene_list, props, functional_extras, errors, stats,
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
        "status": "rejected", "reason": "rejected_pinned_entry_missing_alias",
    }]
    assert not any(c["portrait_id"] == "cp-wyc" for c in characters), (
        "钉住的证据压根没提到待判别名，绝不能采信"
    )
    assert any(e["label"] == "小胖子" for e in functional_extras)


def test_true_name_verdict_requires_dual_anchor_pin_when_available(monkeypatch):
    """红灯（双锚定优先，见 PREP_PACK_VERSION 上方 1.10.0 大注释）：全卷宗
    结构上确实存在能同时证明 alias 与 true_name 的双锚定条目（第10章），
    但模型选对候选后却把钉证钉在了另一条只含 alias 的弱证据（第20章）上——
    卷宗里明明有更强的桥接句摆在模型面前，不能接受它舍强就弱，reason=
    rejected_dual_anchor_available_not_pinned。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-wyc','p1','王有材',1,NULL)"
    )
    conn.execute(
        "INSERT INTO chapters(project_id, idx, content) VALUES ('p1', 10, ?)",
        ("小胖子其实就是王有材的绰号。",),
    )
    conn.execute(
        "INSERT INTO chapters(project_id, idx, content) VALUES ('p1', 20, ?)",
        ("小胖子路过集市，买了个白面馒头。",),
    )
    conn.commit()

    verdict_calls = {"n": 0}

    async def fake_chat_structured(messages, **kwargs):
        verdict_calls["n"] += 1
        prompt = str(messages[0]["content"])
        assert "小胖子其实就是王有材的绰号。" in prompt
        assert "小胖子路过集市，买了个白面馒头。" in prompt
        # 候选编号1是双锚定条目（both桶排在前面），候选编号2是弱证据——
        # 模型选对了候选人本身，却钉在了编号2上。
        return prep_pack._PrepPackTrueNameVerdictResponse(
            selected_candidate="王有材", supporting_entry_index=2,
        )

    async def fake_discovery(project_id, episode_no, source_text, bible, *, generate_portraits=True):
        return {"added": [], "resolutions": [], "errors": [], "skipped": [], "warnings": []}

    monkeypatch.setattr(prep_pack.model_gateway, "chat_structured", fake_chat_structured)
    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", fake_discovery)
    patch_portraits_everywhere(monkeypatch, "persist_screenplay_character_resolutions", lambda *a, **k: [])

    events = [_event("ev_302", characters=[
        {"display_name": "小胖子", "is_background_extra": False, "suspected_true_name": "王有材"},
    ])]
    (
        characters, scene_list, props, functional_extras, errors, stats,
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
        "status": "rejected", "reason": "rejected_dual_anchor_available_not_pinned",
    }]
    assert not any(c["portrait_id"] == "cp-wyc" for c in characters)


def test_true_name_verdict_accepts_degraded_in_episode_pin_when_no_dual_anchor_exists(monkeypatch):
    """红灯（退化路径，真实 EP5"许姓女子"→"许清"案例的简化复现——"许清"要到
    全书第34章才第一次出现，跟"许姓女子"在任何一段里都不会同时出现，双锚定
    在这类跨章绑定上结构上不可能存在，见 PREP_PACK_VERSION 上方 1.10.0
    大注释）：全卷宗不存在任何双锚定条目，唯一的卷宗记录只含 alias，但这条
    记录确实来自本集自己的原文（"集内指代段落"）——允许退化接受，
    provenance.dual_anchor 显式标记 False（不是静默降级），method 判定为
    本地锚点（resolution，不是 resolution_forward——退化接受的前提正是
    钉住的段落必须在本集内）。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-swj','p1','沈无极',1,NULL)"
    )
    conn.execute(
        "INSERT INTO chapters(project_id, idx, content) VALUES ('p1', 7, ?)",
        ("山道上走来一位银发老者，负手而立。",),
    )
    conn.commit()

    verdict_calls = {"n": 0}

    async def fake_chat_structured(messages, **kwargs):
        verdict_calls["n"] += 1
        assert kwargs.get("output_schema")["properties"]["selected_candidate"]["enum"] == [
            "沈无极", "都不是/无法确定",
        ]
        return prep_pack._PrepPackTrueNameVerdictResponse(
            selected_candidate="沈无极", supporting_entry_index=1,
        )

    monkeypatch.setattr(prep_pack.model_gateway, "chat_structured", fake_chat_structured)

    def boom_character(*_a, **_k):
        raise AssertionError("核验通过的假设应走确定性快车道，不应该再触发全量身份发现")

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", boom_character)

    events = [_event("ev_303", characters=[
        {"display_name": "银发老者", "is_background_extra": False, "suspected_true_name": "沈无极"},
    ])]
    (
        characters, scene_list, props, functional_extras, errors, stats,
        true_name_hints, scene_alias_anchors, rejected_alias_conflicts,
    ) = _resolve(
        conn, events=events,
        source_text="山道上走来一位银发老者，负手而立。",
    )

    assert verdict_calls["n"] == 1
    assert errors == []
    assert characters == [{
        "identity_id": "bible:沈无极", "display_name": "沈无极",
        "portrait_id": "cp-swj", "segment_indexes": [1], "aliases": ["银发老者"],
        "visual_entity_id": "bible:沈无极", "display_appellation": "银发老者",
        "provenance": {
            "method": "resolution", "anchor_segments": [1],
            "anchor_phrase": "山道上走来一位银发老者，负手而立。",
            "dual_anchor": False,
        },
    }]
    assert any(
        h["status"] == "accepted" and h["mention"] == "银发老者"
        and h["suspected_true_name"] == "沈无极" and h["dual_anchor"] is False
        for h in true_name_hints
    )


# ---------------------------------------------------------------------------
# 任务②（K/M 并发化，见 PREP_PACK_VERSION 上方大注释"并发闸"一节）最重要
# 的一条红灯：确定性——两个互相独立的裁决任务，不管谁先完成，最终产物必须
# 逐字节一致。用可控延迟制造两种相反的完成顺序，断言两次真实产物完全相同。
# ---------------------------------------------------------------------------

def test_true_name_verification_concurrent_completion_order_does_not_affect_output(
    monkeypatch,
):
    """红灯（K 并发化确定性）：两条互相独立的 suspected_true_name 核验
    （"银发老者"→"沈无极"、"小胖子"→"李富贵"，各自的卷宗互不相干）现在会
    被 asyncio.gather 并发发起。用可控的 sleep 延迟制造两种相反的完成
    顺序（先银发老者后小胖子 / 先小胖子后银发老者），两次调用
    _resolve_assets 的最终 characters/true_name_hints 必须逐字节相同——
    并发完成顺序不能影响 true_name_verdict_cache 的最终内容，也不能影响
    _pass() 主循环按事件原始顺序写回 characters 字典的既有确定性。"""

    def make_conn():
        conn = _make_conn()
        conn.execute(
            "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
            "VALUES ('cp-swj','p1','沈无极',1,NULL)"
        )
        conn.execute(
            "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
            "VALUES ('cp-lfg','p1','李富贵',1,NULL)"
        )
        conn.execute(
            "INSERT INTO chapters(project_id, idx, content) VALUES ('p1', 7, ?)",
            ("山道上走来一位银发老者，负手而立。",),
        )
        conn.execute(
            "INSERT INTO chapters(project_id, idx, content) VALUES ('p1', 8, ?)",
            ("他是当年的小胖子，李富贵。",),
        )
        conn.commit()
        return conn

    source_text = "山道上走来一位银发老者，负手而立。\n\n他是当年的小胖子，李富贵。"
    events = [
        _event("ev_a", characters=[
            {"display_name": "银发老者", "is_background_extra": False, "suspected_true_name": "沈无极"},
        ]),
        _event("ev_b", characters=[
            {"display_name": "小胖子", "is_background_extra": False, "suspected_true_name": "李富贵"},
        ]),
    ]

    def boom_character(*_a, **_k):
        raise AssertionError("核验通过的假设应走确定性快车道，不应该再触发全量身份发现")

    async def run_once(*, slow_alias: str) -> dict:
        conn = make_conn()
        call_order: list[str] = []

        async def fake_chat_structured(messages, **kwargs):
            prompt = str(messages[0]["content"])
            if "银发老者" in prompt:
                alias, candidate = "银发老者", "沈无极"
            elif "小胖子" in prompt:
                alias, candidate = "小胖子", "李富贵"
            else:
                raise AssertionError(f"未识别的裁决提示词：{prompt[:50]}")
            # 可控延迟：让 slow_alias 那一路故意晚完成，制造两种相反的完成
            # 顺序——不引入真实网络等待，只是确定性地翻转 asyncio 事件循环
            # 的调度顺序。
            await asyncio.sleep(0.02 if alias == slow_alias else 0.0)
            call_order.append(alias)
            return prep_pack._PrepPackTrueNameVerdictResponse(
                selected_candidate=candidate, supporting_entry_index=1,
            )

        monkeypatch.setattr(prep_pack.model_gateway, "chat_structured", fake_chat_structured)
        patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", boom_character)

        result = await prep_pack._resolve_assets(
            conn, project_id="p1", episode_id="ep-test", episode_no=2,
            source_text=source_text,
            character_mentions=_mentions_with_segment_indexes(events, source_text, "characters"),
            scene_mentions=_mentions_with_segment_indexes(events, source_text, "scenes"),
            prop_mentions=[], run_id=None,
        )
        characters, _scene_list, _props, _functional_extras, errors, _stats, true_name_hints, *_ = result
        assert errors == []
        return {
            "characters": characters,
            "true_name_hints": sorted(
                true_name_hints, key=lambda h: (h["kind"], h["mention"]),
            ),
            "call_order": list(call_order),
        }

    result_a = asyncio.run(run_once(slow_alias="银发老者"))
    result_b = asyncio.run(run_once(slow_alias="小胖子"))

    # 前提校验：两次真的以不同顺序完成——不是延迟设置无效导致的假阳性
    # （sleep(0) 的那一路应该先完成，sleep(0.02) 的那一路后完成）。
    assert result_a["call_order"] == ["小胖子", "银发老者"]
    assert result_b["call_order"] == ["银发老者", "小胖子"]
    assert result_a["call_order"] != result_b["call_order"], "夹具没能制造出不同的完成顺序"

    assert len(result_a["characters"]) == 2, "两条独立核验都必须真的绑定成功"
    assert json.dumps(result_a["characters"], sort_keys=True, ensure_ascii=False) == json.dumps(
        result_b["characters"], sort_keys=True, ensure_ascii=False,
    ), "并发完成顺序不能影响最终 characters 产物，必须逐字节一致"
    assert result_a["true_name_hints"] == result_b["true_name_hints"], (
        "并发完成顺序不能影响 true_name_hints 观测记录"
    )


def test_functional_extra_candidate_concurrent_completion_order_does_not_affect_output(
    monkeypatch,
):
    """红灯（M 并发化确定性）：两个互相独立的未解析角色标签候选判别
    （"绿袍男子"→许清、"白袍老者"→上官笑，各自的候选/卷宗互不相干）现在
    会被 asyncio.gather 并发发起。用可控延迟制造两种相反的完成顺序，两次
    调用 _resolve_assets 的最终 characters 产物必须逐字节一致——并发完成
    顺序不能影响 skip_character_names/character_rename/candidate_verdict_
    pins 这几个共享容器最终按 unresolved_chars 原始顺序写回的确定性。"""

    def make_conn():
        conn = _make_conn()
        conn.execute(
            "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
            "VALUES ('cp-xuqing','p1','许清',1,NULL)"
        )
        conn.execute(
            "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
            "VALUES ('cp-sgx','p1','上官笑',1,NULL)"
        )
        _seed_bible_characters(conn, "p1", [
            _bible_character(
                "许清", appearance_canonical="常年穿银色长袍，气质清冷。",
                aliases=[_bible_alias("许师姐", evidence_chapter_index=1)],
            ),
            _bible_character(
                "上官笑", appearance_canonical="面容清瘦，常着白袍。",
                aliases=[_bible_alias("上官师兄", evidence_chapter_index=1)],
            ),
        ])
        conn.commit()
        return conn

    source_text = "\n\n".join([
        "绿袍男子对着那女子躬身行礼，口称许师姐，随后请四人随他一同返回宗门。",
        "白袍老者缓步上前，众人纷纷让开道路，齐声唤他上官师兄。",
    ])
    events = [
        _event("ev_a", characters=[
            {"display_name": "绿袍男子", "is_background_extra": True},
        ]),
        _event("ev_b", characters=[
            {"display_name": "白袍老者", "is_background_extra": True},
        ]),
    ]

    async def fake_discovery(project_id, episode_no, source_text, bible, *, generate_portraits=True):
        return {"added": [], "resolutions": [], "errors": [], "warnings": [], "skipped": []}

    async def run_once(*, slow_label: str) -> dict:
        conn = make_conn()
        call_order: list[str] = []

        async def fake_chat_structured(messages, **kwargs):
            if kwargs.get("model_type") is prep_pack._PrepPackFunctionalCandidateVerdict:
                prompt = str(messages[0]["content"])
                # 卷宗（原文段落）两段都会出现在每一次调用的提示词里（候选
                # 判别的候选名单是全集共享的，不是按标签各自切分卷宗）——
                # 不能用"许师姐"/"上官师兄"这两个字面区分是哪次调用，两段
                # 原文各自的锚点词在任一次调用里都存在；只有"判断标签...”
                # 这一行的标签本身才是这次调用独有的，用它来区分。
                if '标签"绿袍男子"' in prompt:
                    label, candidate = "绿袍男子", "许清"
                elif '标签"白袍老者"' in prompt:
                    label, candidate = "白袍老者", "上官笑"
                else:
                    raise AssertionError(f"未识别的候选判别提示词：{prompt[:50]}")
                await asyncio.sleep(0.02 if label == slow_label else 0.0)
                call_order.append(label)
                return prep_pack._PrepPackFunctionalCandidateVerdict(
                    selected_candidate=candidate, supporting_segment_index=1,
                    supporting_quote="",
                )
            from app.source_paratext import ParatextSpans
            return ParatextSpans(spans=[])

        patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", fake_discovery)
        patch_portraits_everywhere(monkeypatch, "persist_screenplay_character_resolutions", lambda *a, **k: [])
        monkeypatch.setattr(prep_pack.model_gateway, "chat_structured", fake_chat_structured)

        result = await prep_pack._resolve_assets(
            conn, project_id="p1", episode_id="ep-test", episode_no=2,
            source_text=source_text,
            character_mentions=_mentions_with_segment_indexes(events, source_text, "characters"),
            scene_mentions=_mentions_with_segment_indexes(events, source_text, "scenes"),
            prop_mentions=[], run_id=None,
        )
        characters, _scene_list, _props, functional_extras, errors, _stats, *_ = result
        assert errors == []
        return {
            "characters": characters, "functional_extras": functional_extras,
            "call_order": list(call_order),
        }

    result_a = asyncio.run(run_once(slow_label="绿袍男子"))
    result_b = asyncio.run(run_once(slow_label="白袍老者"))

    # 前提校验：两次真的以不同顺序完成——不是延迟设置无效导致的假阳性。
    assert result_a["call_order"] == ["白袍老者", "绿袍男子"]
    assert result_b["call_order"] == ["绿袍男子", "白袍老者"]
    assert result_a["call_order"] != result_b["call_order"], "夹具没能制造出不同的完成顺序"

    assert len(result_a["characters"]) == 2, "两条独立候选判别都必须真的绑定成功"
    assert result_a["functional_extras"] == [], "绑定成功后不应残留在 functional_extras 里"
    assert json.dumps(result_a["characters"], sort_keys=True, ensure_ascii=False) == json.dumps(
        result_b["characters"], sort_keys=True, ensure_ascii=False,
    ), "并发完成顺序不能影响最终 characters 产物，必须逐字节一致"


# ---------------------------------------------------------------------------
# 任务②失败语义（见 _prep_pack_gather_concurrent 上方大注释）：某个并发任务
# 抛异常时不得被吞掉，也不得让其它任务的结果被静默丢弃导致部分写回。
# ---------------------------------------------------------------------------

def test_gather_concurrent_reraises_first_exception_after_all_tasks_complete():
    """红灯（_prep_pack_gather_concurrent 单元测试）：多个任务里有一个抛
    异常时，必须等全部任务真正跑完（不产生"孤儿"后台任务）后，把这个异常
    原样重新抛出——不吞、不改写成别的异常类型、不静默丢弃。用一个可变列表
    记录每个任务是否真的跑到了自己的结尾，证明"等全部完成"这个承诺成立。"""
    completed: list[str] = []

    async def ok(tag: str, delay: float) -> str:
        await asyncio.sleep(delay)
        completed.append(tag)
        return tag

    async def boom(tag: str, delay: float) -> str:
        await asyncio.sleep(delay)
        completed.append(tag)
        raise ValueError(f"{tag} 失败")

    async def scenario() -> None:
        with pytest.raises(ValueError, match="任务B 失败"):
            await prep_pack._prep_pack_gather_concurrent([
                ok("任务A", 0.03),
                boom("任务B", 0.0),
                ok("任务C", 0.02),
            ])

    asyncio.run(scenario())
    # 三个任务全部真正跑完了（包括比失败任务更晚完成的任务A），不是"任务B
    # 一失败就立刻甩出异常、任务A被扔下孤儿运行/结果被丢弃"。
    assert set(completed) == {"任务A", "任务B", "任务C"}


def test_gather_concurrent_returns_all_results_in_input_order_when_nothing_fails():
    """绿灯对照：没有任务失败时，返回值必须是按传入顺序（不是完成顺序）的
    全部结果——这正是 K/M 两条并发化循环用来保证确定性写回的基础保证。"""
    async def slow_first(value: int) -> int:
        await asyncio.sleep(0.03 if value == 0 else 0.0)
        return value

    async def scenario() -> list[int]:
        return await prep_pack._prep_pack_gather_concurrent(
            [slow_first(i) for i in range(5)],
        )

    results = asyncio.run(scenario())
    assert results == [0, 1, 2, 3, 4]


def test_true_name_verification_task_failure_aborts_pass_without_partial_writeback(
    monkeypatch,
):
    """红灯（K 失败语义的集成验证）：两条独立核验里有一条的模型调用直接
    抛异常（模拟 provider 调用失败）——_resolve_assets 必须整体失败（异常
    原样传播出去，不是吞掉后继续拿一份只写了一半的 characters），另一条
    本该成功的核验也不能悄悄留下部分产物：这一整次生成尝试要么完整成功，
    要么完整失败，不允许"一部分角色已经绑定、另一部分因为并发同伴失败而
    从未绑定"这种介于两者之间的产物流出——跟现有门禁失败即整包重试的既有
    纪律一致，不是本次新引入的语义。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-swj','p1','沈无极',1,NULL)"
    )
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-lfg','p1','李富贵',1,NULL)"
    )
    conn.execute(
        "INSERT INTO chapters(project_id, idx, content) VALUES ('p1', 7, ?)",
        ("山道上走来一位银发老者，负手而立。",),
    )
    conn.execute(
        "INSERT INTO chapters(project_id, idx, content) VALUES ('p1', 8, ?)",
        ("他是当年的小胖子，李富贵。",),
    )
    conn.commit()

    source_text = "山道上走来一位银发老者，负手而立。\n\n他是当年的小胖子，李富贵。"
    events = [
        _event("ev_a", characters=[
            {"display_name": "银发老者", "is_background_extra": False, "suspected_true_name": "沈无极"},
        ]),
        _event("ev_b", characters=[
            {"display_name": "小胖子", "is_background_extra": False, "suspected_true_name": "李富贵"},
        ]),
    ]

    async def fake_chat_structured(messages, **kwargs):
        prompt = str(messages[0]["content"])
        if "小胖子" in prompt:
            # 模拟 provider 调用本身失败（网络错误/schema 校验失败等），
            # 不是业务上的"都不是/无法确定"。
            raise RuntimeError("模拟 provider 调用失败")
        await asyncio.sleep(0.02)  # 比失败的那一路更晚完成
        return prep_pack._PrepPackTrueNameVerdictResponse(
            selected_candidate="沈无极", supporting_entry_index=1,
        )

    monkeypatch.setattr(prep_pack.model_gateway, "chat_structured", fake_chat_structured)

    def boom_character(*_a, **_k):
        raise AssertionError("核验通过的假设应走确定性快车道，不应该再触发全量身份发现")

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", boom_character)

    with pytest.raises(RuntimeError, match="模拟 provider 调用失败"):
        asyncio.run(prep_pack._resolve_assets(
            conn, project_id="p1", episode_id="ep-test", episode_no=2,
            source_text=source_text,
            character_mentions=_mentions_with_segment_indexes(events, source_text, "characters"),
            scene_mentions=_mentions_with_segment_indexes(events, source_text, "scenes"),
            prop_mentions=[], run_id=None,
        ))


def test_true_name_verdict_rejects_degraded_pin_from_unrelated_out_of_episode_chapter(monkeypatch):
    """红灯（退化路径的安全网，真实数据坐实的风险：project_id=
    proj_3ac0b627fa46 第981章有一处"许姓女子"，是完全不相关的转世预言
    片段，跟 EP5 的孟浩/上官修剧情毫无关系，见 PREP_PACK_VERSION 上方
    1.10.0 大注释）：全卷宗不存在双锚定条目，唯一命中的卷宗记录含 alias，
    但来自一处跟本集毫无关系的其它章节（不是本集自己的段落）——退化接受
    只对"集内指代段落"开放，这条不满足，必须拒绝，reason=
    rejected_degraded_pin_out_of_episode。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-swj','p1','沈无极',1,NULL)"
    )
    conn.execute(
        "INSERT INTO chapters(project_id, idx, content) VALUES ('p1', 500, ?)",
        ("远处那银发老者转身离去，再未回头，与此集剧情毫无关系。",),
    )
    conn.commit()

    verdict_calls = {"n": 0}

    async def fake_chat_structured(messages, **kwargs):
        verdict_calls["n"] += 1
        return prep_pack._PrepPackTrueNameVerdictResponse(
            selected_candidate="沈无极", supporting_entry_index=1,
        )

    async def fake_discovery(project_id, episode_no, source_text, bible, *, generate_portraits=True):
        return {"added": [], "resolutions": [], "errors": [], "skipped": [], "warnings": []}

    monkeypatch.setattr(prep_pack.model_gateway, "chat_structured", fake_chat_structured)
    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", fake_discovery)
    patch_portraits_everywhere(monkeypatch, "persist_screenplay_character_resolutions", lambda *a, **k: [])

    events = [_event("ev_304", characters=[
        {"display_name": "银发老者", "is_background_extra": False, "suspected_true_name": "沈无极"},
    ])]
    (
        characters, scene_list, props, functional_extras, errors, stats,
        true_name_hints, scene_alias_anchors, rejected_alias_conflicts,
    ) = _resolve(
        conn, events=events,
        source_text="山顶上，银发老者哈哈大笑起来。",
    )

    assert verdict_calls["n"] == 1
    assert errors == []
    rejected = [h for h in true_name_hints if h["status"] == "rejected"]
    assert rejected == [{
        "kind": "character", "mention": "银发老者", "suspected_true_name": "沈无极",
        "status": "rejected", "reason": "rejected_degraded_pin_out_of_episode",
    }]
    assert not any(c["portrait_id"] == "cp-swj" for c in characters)


# ---------------------------------------------------------------------------
# 1.10.0（缺陷 B：skip_character_names 短路抢在 suspected_true_name 核验
# 之前执行，作废已通过的裁决——真实 EP5 完整因果链，见 PREP_PACK_VERSION
# 上方 1.10.0 大注释）。
# ---------------------------------------------------------------------------

def test_pass2_discovery_skip_name_collision_does_not_discard_pass1_accepted_true_name(monkeypatch):
    """红灯（缺陷 B 核心场景，真实 EP5 完整因果链复现）：pass1 里
    "许姓女子"→"许清"的 suspected_true_name 核验已经通过（双锚定卷宗，
    accepted=True），但同一轮里另一个提及"神秘人"未能直接解析，触发 pass2
    的角色发现——discovery 是本函数之外一次独立的全集重新通读，即使本集
    其实只有一处"许姓女子"，它也可能把这同一个原文标签独立判定为"没有
    归类结论"（跟 pass1 已经核验通过的结论毫无关系，只是撞了同一个字面
    量）。旧代码里 pass2 的 `if name in skip_character_names: continue`
    排在核验代码之前，会把"许姓女子"重新划进 skip_character_names 后
    直接短路，作废 pass1 已经成立的绑定；修复后 suspected_true_name 核验
    优先于这个短路，"许姓女子"必须仍然绑定到"许清"，且只发起一次模型
    调用（pass2 命中 true_name_verdict_cache，不重复发起裁决）。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-xuqing','p1','许清',1,NULL)"
    )
    conn.execute(
        "INSERT INTO chapters(project_id, idx, content) VALUES ('p1', 34, ?)",
        ("许姓女子其实就是许清本人。",),
    )
    conn.commit()

    verdict_calls = {"n": 0}

    async def fake_chat_structured(messages, **kwargs):
        verdict_calls["n"] += 1
        return prep_pack._PrepPackTrueNameVerdictResponse(
            selected_candidate="许清", supporting_entry_index=1,
        )

    async def fake_discovery(project_id, episode_no, source_text, bible, *, generate_portraits=True):
        # discovery 独立通读全集正文，跟 pass1 的核验毫无关系地把"许姓女子"
        # 判成"没有归类结论"（真实 EP5 事故的确切因果链：角色发现自己的
        # 候选面/措辞跟本集原文不完全对齐，撞上同一个字面量）。
        return {
            "added": [], "resolutions": [],
            "skipped": [{"name": "许姓女子", "status": "skipped"}],
            "errors": [], "warnings": [],
        }

    monkeypatch.setattr(prep_pack.model_gateway, "chat_structured", fake_chat_structured)
    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", fake_discovery)
    patch_portraits_everywhere(monkeypatch, "persist_screenplay_character_resolutions", lambda *a, **k: [])

    events = [_event("ev_401", characters=[
        {"display_name": "许姓女子", "is_background_extra": False, "suspected_true_name": "许清"},
        {"display_name": "神秘人", "is_background_extra": True},
    ])]
    (
        characters, scene_list, props, functional_extras, errors, stats,
        true_name_hints, scene_alias_anchors, rejected_alias_conflicts,
    ) = _resolve(
        conn, events=events,
        source_text="许姓女子从人群中走过，无人问津。神秘人则悄然隐入暗处。",
    )

    assert errors == []
    assert verdict_calls["n"] == 1, (
        "pass2 重新核验同一个 (subject_kind, alias, suspected_true_name) 必须"
        "命中 true_name_verdict_cache，不重复发起模型调用"
    )
    by_portrait = {c["portrait_id"]: c for c in characters}
    assert "cp-xuqing" in by_portrait, (
        "「许姓女子」必须仍然绑定到许清，pass1 的结论不能被 pass2 静默作废"
    )
    assert by_portrait["cp-xuqing"]["display_appellation"] == "许姓女子"
    assert not any(e["label"] == "许姓女子" for e in functional_extras), (
        "绝不能因为角色发现独立通读凑巧撞出同名功能簇，就把已核验通过的绑定重新打回群演"
    )
    assert any(e["label"] == "神秘人" for e in functional_extras)
    assert any(
        h["status"] == "accepted" and h["mention"] == "许姓女子"
        and h["suspected_true_name"] == "许清"
        for h in true_name_hints
    )


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

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", boom_character)

    events_ep1 = [_event("ev_ep1", characters=[
        {"display_name": "许清", "is_background_extra": False},
    ])]
    characters_ep1, _, _, _, errors_ep1, stats_ep1, *_ = _resolve(
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
    characters_ep6, _, _, _, errors_ep6, stats_ep6, *_ = _resolve(
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

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", fake_functional)
    patch_portraits_everywhere(monkeypatch, "persist_screenplay_character_resolutions", lambda *a, **k: [])
    events = [_event("ev_a", characters=[
        {"display_name": "神秘蒙面客", "is_background_extra": False},
    ])]
    _, _, _, extras_1, errors_1, *_ = _resolve(
        conn, events=events, episode_no=1,
        source_text="神秘蒙面客悄然离去。",
    )
    events2 = [_event("ev_b", characters=[
        {"display_name": "神秘蒙面客", "is_background_extra": False},
    ])]
    _, _, _, extras_2, errors_2, *_ = _resolve(
        conn, events=events2, episode_no=4,
        source_text="神秘蒙面客再度现身。",
    )

    assert errors_1 == [] and errors_2 == []
    assert len(extras_1) == 1 and len(extras_2) == 1
    assert extras_1[0]["visual_entity_id"] == extras_2[0]["visual_entity_id"]
    assert extras_1[0]["visual_entity_id"].startswith("entity:")


# ---------------------------------------------------------------------------
# 未解析角色标签候选判别（1.8.0，见 app.production.prep_pack.PREP_PACK_
# VERSION 上方大注释、_prep_pack_resolve_functional_extra_candidate 的完整
# 说明）：用户原始诉求——同一角色在不同集换脸，真名揭晓前人物建模持续
# 漂移。真实第 37 轮 EP1 现场：许清人物谱已登记确认别名"许师姐"、
# appearance_canonical 写着"常年穿银色长袍"、定妆照 ep_start=1 已就绪，
# 本集原文两次出现"许师姐"，但事件链抽取模型给出场角色起的标签是外貌描述
# "银色长袍女子"——跟别名库登记的称谓类型对不上，此前一路落
# functional_extras 当无图群演。下面几条红灯覆盖：核心红→绿（标签类型
# 对不上时，候选判别机制正确绑定到人物谱候选并保留本集原文措辞）、以及
# 三条必须守住的反例（候选集为空 / 模型选"都不是无法确定" / 模型引用卷宗
# 外段号，三种情形都必须维持原行为落群演，绝不猜）。
# ---------------------------------------------------------------------------

def test_unresolved_appearance_label_binds_via_candidate_verdict(monkeypatch):
    """红灯→绿灯核心场景：discovery 干净运行却没能给"银色长袍女子"任何
    归类结论（真实场景——discovery 自己的候选面用的也是称谓/姓名，同样
    对不上外貌描述这个标签类型），落入 Coordinator-mandated default 兜底，
    即将落 functional_extras 之前，候选判别机制必须介入：本集原文里许清的
    已确认别名"许师姐"字面出现，候选集只有许清一人入选（另一个人物谱角色
    "孟浩"的姓名/别名本集原文里从未出现，不该被拉进候选集，也不该出现在
    发给模型的候选名单里）；候选判别模型调用选中许清、引用卷宗内的段号，
    最终必须绑定到许清已有的 portrait_id/identity_id/visual_entity_id，
    但 display_appellation 仍须是本集原文措辞"银色长袍女子"（不提前剧透
    许清这个规范名），且这个标签不能再出现在 functional_extras 里。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-xuqing','p1','许清',1,NULL)"
    )
    _seed_bible_characters(conn, "p1", [
        _bible_character(
            "许清", appearance_canonical="常年穿银色长袍，气质清冷。",
            aliases=[_bible_alias("许师姐", evidence_chapter_index=1)],
        ),
        # 决胜点：孟浩不该入选候选集——他的规范名/别名本集原文里完全没出现。
        _bible_character("孟浩", appearance_canonical="占位外观"),
    ])

    async def fake_discovery(project_id, episode_no, source_text, bible, *, generate_portraits=True):
        return {"added": [], "resolutions": [], "errors": [], "warnings": [], "skipped": []}

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", fake_discovery)
    patch_portraits_everywhere(monkeypatch, "persist_screenplay_character_resolutions", lambda *a, **k: [])

    seen: dict = {}

    async def fake_chat_structured(messages, **kwargs):
        seen["prompt"] = str(messages[0]["content"])
        seen["schema"] = kwargs.get("output_schema")
        return prep_pack._PrepPackFunctionalCandidateVerdict(
            selected_candidate="许清", supporting_segment_index=1,
            supporting_quote="许师姐武功高强，众人皆知。",
        )

    monkeypatch.setattr(prep_pack.model_gateway, "chat_structured", fake_chat_structured)

    events = [_event("ev_001", characters=[
        {"display_name": "银色长袍女子", "is_background_extra": True},
    ])]
    characters, scene_list, props, functional_extras, errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts = _resolve(
        conn, events=events,
        source_text="许师姐武功高强，众人皆知。\n\n银色长袍女子缓步走出大殿，无人认得她的身份。",
    )

    assert errors == []
    assert stats["character_discovery_calls"] == 1
    by_portrait = {c["portrait_id"]: c for c in characters}
    assert "cp-xuqing" in by_portrait, "许清必须真的出现在 asset_manifest.characters 中"
    entry = by_portrait["cp-xuqing"]
    assert entry["display_name"] == "许清"
    assert entry["identity_id"] == "bible:许清"
    assert entry["visual_entity_id"] == "bible:许清"
    assert entry["display_appellation"] == "银色长袍女子", "字幕/取图措辞必须是本集原文说法，不提前剧透"
    assert entry["provenance"]["method"] == "candidate_verdict"
    assert entry["provenance"]["anchor_segments"] == [1]
    assert "许师姐" in entry["provenance"]["anchor_phrase"]
    assert not any(e["label"] == "银色长袍女子" for e in functional_extras), (
        "绑定成功后，这个标签不能再出现在 functional_extras 里"
    )
    # 候选面必须只含真正命中本集原文的角色：孟浩不该入选。
    assert "许清" in seen["prompt"] and "孟浩" not in seen["prompt"]
    assert seen["schema"]["properties"]["selected_candidate"]["enum"] == [
        "许清", "都不是/无法确定",
    ]


def test_candidate_verdict_empty_candidate_set_stays_functional_extra(monkeypatch):
    """反例①：人物谱里没有任何角色的规范名/已确认别名字面出现在本集原文
    里，候选集为空——必须直接维持原行为落 functional_extras，且绝不发起
    任何候选判别模型调用（候选集为空时连卷宗都不该检索，更不该问模型）。"""
    conn = _make_conn()

    async def fake_discovery(project_id, episode_no, source_text, bible, *, generate_portraits=True):
        return {"added": [], "resolutions": [], "errors": [], "warnings": [], "skipped": []}

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", fake_discovery)
    patch_portraits_everywhere(monkeypatch, "persist_screenplay_character_resolutions", lambda *a, **k: [])

    async def boom_chat_structured(messages, **kwargs):
        raise AssertionError("候选集为空时绝不该发起候选判别模型调用")

    monkeypatch.setattr(prep_pack.model_gateway, "chat_structured", boom_chat_structured)

    events = [_event("ev_001", characters=[
        {"display_name": "银色长袍女子", "is_background_extra": True},
    ])]
    characters, scene_list, props, functional_extras, errors, stats, *_ = _resolve(
        conn, events=events,
        source_text="银色长袍女子缓步走出大殿，无人认得她的身份。",
    )

    assert errors == []
    assert not any(c["display_name"] == "许清" for c in characters)
    assert any(e["label"] == "银色长袍女子" for e in functional_extras)


def test_candidate_verdict_no_match_selected_stays_functional_extra(monkeypatch):
    """反例②：候选集非空（许清入选），但模型如实回答"都不是/无法确定"——
    必须维持原行为落 functional_extras，绝不强行绑定候选集里的任何一人。
    1.10.0 缺陷 A 顺带修复：这批 functional_extras 确实发起过一次候选判别
    模型调用（候选集/卷宗均非空）只是没选中，provenance.candidate_verdict_
    attempted 必须是 True——跟"候选集为空、从未获得机会"的 False 区分开，
    此前两者会坍缩成同一个 method="discovery" 值，只能翻 provider_calls
    反推。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-xuqing','p1','许清',1,NULL)"
    )
    _seed_bible_characters(conn, "p1", [
        _bible_character(
            "许清", appearance_canonical="常年穿银色长袍，气质清冷。",
            aliases=[_bible_alias("许师姐", evidence_chapter_index=1)],
        ),
    ])

    async def fake_discovery(project_id, episode_no, source_text, bible, *, generate_portraits=True):
        return {"added": [], "resolutions": [], "errors": [], "warnings": [], "skipped": []}

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", fake_discovery)
    patch_portraits_everywhere(monkeypatch, "persist_screenplay_character_resolutions", lambda *a, **k: [])

    async def fake_chat_structured(messages, **kwargs):
        return prep_pack._PrepPackFunctionalCandidateVerdict(
            selected_candidate="都不是/无法确定", supporting_segment_index=1,
        )

    monkeypatch.setattr(prep_pack.model_gateway, "chat_structured", fake_chat_structured)

    events = [_event("ev_001", characters=[
        {"display_name": "银色长袍女子", "is_background_extra": True},
    ])]
    characters, scene_list, props, functional_extras, errors, stats, *_ = _resolve(
        conn, events=events,
        source_text="许师姐武功高强，众人皆知。\n\n银色长袍女子缓步走出大殿，无人认得她的身份。",
    )

    assert errors == []
    assert not any(c["display_name"] == "许清" for c in characters)
    assert any(e["label"] == "银色长袍女子" for e in functional_extras)
    extra = next(e for e in functional_extras if e["label"] == "银色长袍女子")
    assert extra["provenance"]["method"] == "discovery"
    assert extra["provenance"]["candidate_verdict_attempted"] is True


def test_candidate_verdict_out_of_dossier_segment_rejected_stays_functional_extra(monkeypatch):
    """反例③：模型选中了候选集里真实存在的人（许清），但引用的段号根本
    不在卷宗目录里（凭空编造/协议之外的取值）——结构性钉证必须拒绝，绝不
    因为候选人本身选对了就放行，一律维持原行为落 functional_extras。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-xuqing','p1','许清',1,NULL)"
    )
    _seed_bible_characters(conn, "p1", [
        _bible_character(
            "许清", appearance_canonical="常年穿银色长袍，气质清冷。",
            aliases=[_bible_alias("许师姐", evidence_chapter_index=1)],
        ),
    ])

    async def fake_discovery(project_id, episode_no, source_text, bible, *, generate_portraits=True):
        return {"added": [], "resolutions": [], "errors": [], "warnings": [], "skipped": []}

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", fake_discovery)
    patch_portraits_everywhere(monkeypatch, "persist_screenplay_character_resolutions", lambda *a, **k: [])

    async def fake_chat_structured(messages, **kwargs):
        return prep_pack._PrepPackFunctionalCandidateVerdict(
            selected_candidate="许清", supporting_segment_index=9999,
        )

    monkeypatch.setattr(prep_pack.model_gateway, "chat_structured", fake_chat_structured)

    events = [_event("ev_001", characters=[
        {"display_name": "银色长袍女子", "is_background_extra": True},
    ])]
    characters, scene_list, props, functional_extras, errors, stats, *_ = _resolve(
        conn, events=events,
        source_text="许师姐武功高强，众人皆知。\n\n银色长袍女子缓步走出大殿，无人认得她的身份。",
    )

    assert errors == []
    assert not any(c["display_name"] == "许清" for c in characters)
    assert any(e["label"] == "银色长袍女子" for e in functional_extras)


# ---------------------------------------------------------------------------
# 1.8.1（真实数据、已完整诊断的后续事故）：上面 1.8.0 的机制本身没坏，但
# 目标案例仍然失败——`label in seg.text` 逐字定位卷宗时，标签"银色长袍
# 女子"在原文里 0 次逐字出现（原文写的是"穿着一身银色长袍"），both/
# text_only 两类全空，候选锚点段落失去参照点后退化成文档顺序，主角"孟浩"
# 反复出现的开篇独白段落吃光卷宗预算，真正的证据段（"许师姐"，紧邻案发
# 现场）根本没进卷宗，模型如实回答"无法确定"（模型没有错，是卷宗本身没
# 证据）。修法：卷宗主锚点改用标签所属事件的 source_span 定位，不依赖
# 标签字面。以下测试覆盖 PREP_PACK_VERSION 上方 1.8.1 大注释列出的五点
# 要求：①事件跨度段落必须优先全部收录；②候选锚点段落改按到事件跨度的
# 邻近度排序；③标签字面命中时继续收录为主锚点之一；④事件跨度缺失/为空
# 时防御性退回既有行为；⑤全程确定性可复现。
# ---------------------------------------------------------------------------

def test_functional_candidate_dossier_ignores_out_of_range_event_span_segments():
    """反例（防御）：event_span_segments 含越界（0、超出总段数）值——一律
    丢弃，不崩；候选锚点段落仍正常收录（此时主锚点整体为空，走文档顺序
    兜底，即事件跨度缺失时的既有行为）。"""
    segments = prep_pack.index_source_segments("第一段。\n\n第二段。")
    dossier = prep_pack._prep_pack_functional_candidate_dossier(
        segments, "不存在的标签", {"角色甲": ["第二段"]}, {0, 999, -5},
    )
    assert dossier == [{"segment_index": 2, "text": "第二段。"}]


def test_functional_candidate_dossier_keeps_literal_label_segment_as_primary_alongside_event_span():
    """要求③：标签恰好逐字出现在原文时（有些标签确实是原文用词），继续把
    这一段当作主锚点之一收录——不因为改用事件定位就丢掉这条路径。这里标签
    在第 5 段逐字出现，且与事件跨度（第 13/14 段）完全不相邻，两处证据都
    必须进卷宗。"""
    paragraphs = [f"孟浩独自盘坐山顶，心绪起伏，此为第{i}段。" for i in range(1, 13)]
    paragraphs[4] = "远处忽然传来一声轻笑，众人抬头，只见银色长袍女子缓步而来。"
    paragraphs.append("一个面色苍白的女子，穿着一身银色长袍，站在那里面无表情地望着孟浩。")
    paragraphs.append("绿袍男子对着她躬身行礼，口称许师姐，随后请四人随他回宗门。")
    source_text = "\n\n".join(paragraphs)
    segments = prep_pack.index_source_segments(source_text)
    assert len(segments) == 14
    label = "银色长袍女子"
    assert label in source_text, "本用例专测字面命中路径，前提是标签确实逐字出现"

    dossier = prep_pack._prep_pack_functional_candidate_dossier(
        segments, label, {"孟浩": ["孟浩"], "许清": ["许清", "许师姐"]}, {13, 14},
    )
    dossier_indexes = {item["segment_index"] for item in dossier}
    assert {5, 13, 14} <= dossier_indexes, "字面命中段（5）与事件跨度段（13/14）都必须进卷宗"


def test_unresolved_appearance_label_binds_via_candidate_verdict_using_own_segment_indexes(monkeypatch):
    """红灯→绿灯核心场景（1.8.1 引入，2.0.0 起改用提及自报 segment_indexes
    直接定位，见 app/production/prep_pack.py 模块 docstring 的 2.0.0 说明、
    _prep_pack_functional_candidate_label_segments 的完整对比）：标签"银色
    长袍女子"不逐字出现在原文里（原文写的是"穿着一身银色长袍"）；这条提及
    自己申报的 segment_indexes 覆盖"银袍女子登场 + 绿袍男子称许师姐"这两段
    （2.0.0 下不再经过"先分事件、再从事件粗粒度跨度反推段号"这层间接——
    模型对每条提及直接申报它在哪些段落画面出场）；候选集含许清（确认别名
    "许师姐"字面出现在这两段内）；另有主角孟浩在这两段之外的 12 个开篇
    独白段落里反复出现——足以在纯字面+文档顺序算法下吃光卷宗预算
    （_PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MAX_ENTRIES == 12）。

    对照组（嵌入本测试的第一部分）：直接调用 _prep_pack_functional_
    candidate_dossier 时不传主锚点段号——1.8.1 之前唯一的定位方式。1.8.2
    起，B 侧改为按候选公平轮转合并（见 _prep_pack_functional_candidate_
    anchor_pool 的完整说明）：候选集里许清只对应"许师姐"那一段，孟浩对应
    密集的开篇独白十余段，轮转合并让许清那一段排到全局第 2 位，即使不传
    主锚点段号也能挤进 B 侧保底配额——这是候选粒度公平性机制的加成效果，
    不代表主锚点定位从此可有可无（候选证据本身分散在离案发现场很远处、
    或候选数量更多时，主锚点段号仍是唯一能锚定"案发现场本身长什么样"的
    机制；1.8.2 真正修复的新事故——主锚点本身连续覆盖到占满预算、B 侧
    只有一位候选——是候选公平轮转救不了的形状，见另一条独立回归
    test_functional_candidate_dossier_reserves_quota_for_candidate_anchor_
    when_event_span_fills_budget）。
    绿（第二部分）：_resolve_assets 真实调用链（经 _prep_pack_resolve_
    functional_extra_candidate 传入 character_mentions，内部用
    _prep_pack_functional_candidate_label_segments 直接读这条提及自己的
    segment_indexes 当主锚点）必须让卷宗改为优先收录这两段，模型因此能
    看到"许师姐"证据，正确绑定许清。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-xuqing','p1','许清',1,NULL)"
    )
    _seed_bible_characters(conn, "p1", [
        _bible_character(
            "许清", appearance_canonical="常年穿银色长袍，气质清冷。",
            aliases=[_bible_alias("许师姐", evidence_chapter_index=1)],
        ),
        # 决胜点：孟浩必须真的入选候选集（他的姓名在开篇 12 段里反复出现），
        # 旧算法正是被他的候选身份连累吃光了预算——这不是"孟浩不该是候选"，
        # 而是"候选选择题的裁决必须交给模型，卷宗不能提前把真证据挤没"。
        _bible_character("孟浩", appearance_canonical="占位外观"),
    ])

    opening_paragraphs = [
        f"孟浩独自盘膝坐在山顶，心绪起伏，暗自思量第{i}件往事。"
        for i in range(1, 13)
    ]
    rescue_paragraphs = [
        "一个面色苍白，看不出年纪的女子，穿着一身银色长袍，站在那里面无表情地望着孟浩。",
        "绿袍男子对着她躬身行礼，口称许师姐，随后请四人随他回宗门。",
    ]
    paragraphs = opening_paragraphs + rescue_paragraphs
    source_text = "\n\n".join(paragraphs)
    label = "银色长袍女子"
    assert label not in source_text, "复现事故前提：标签不是原文字面，靠字面定位卷宗必然打空"
    assert "许师姐" in source_text

    segments = prep_pack.index_source_segments(source_text)
    assert len(segments) == 14
    from_segment, to_segment = 13, 14

    # ---- 对照组：不传事件跨度（1.8.1 之前唯一的定位方式）----
    candidate_anchor_texts_no_span = {"孟浩": ["孟浩"], "许清": ["许清", "许师姐"]}
    no_span_dossier = prep_pack._prep_pack_functional_candidate_dossier(
        segments, label, candidate_anchor_texts_no_span,
    )
    no_span_indexes = {item["segment_index"] for item in no_span_dossier}
    assert to_segment in no_span_indexes, "1.8.2 候选公平轮转让许师姐段即使没有事件跨度也能进卷宗"
    assert any("许师姐" in item["text"] for item in no_span_dossier)

    # ---- 绿：真实调用链（事件跨度定位）----
    async def fake_discovery(project_id, episode_no, source_text, bible, *, generate_portraits=True):
        return {"added": [], "resolutions": [], "errors": [], "warnings": [], "skipped": []}

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", fake_discovery)
    patch_portraits_everywhere(monkeypatch, "persist_screenplay_character_resolutions", lambda *a, **k: [])

    seen: dict = {}

    async def fake_chat_structured(messages, **kwargs):
        # 本用例源文本超过 200 字，会先触发 _discover_new_characters 内部
        # strip_paratext -> paratext_spans 的旁文本探测模型调用（跟候选
        # 判别共用同一个 model_gateway.chat_structured，见
        # app.source_paratext.paratext_spans 的 200 字阈值）——按
        # model_type 区分两路请求，旁文本探测给"无旁文本"的安全默认响应，
        # 不干扰本测试真正关心的候选判别调用。
        if kwargs.get("model_type") is prep_pack._PrepPackFunctionalCandidateVerdict:
            seen["prompt"] = str(messages[0]["content"])
            return prep_pack._PrepPackFunctionalCandidateVerdict(
                selected_candidate="许清", supporting_segment_index=to_segment,
                supporting_quote="绿袍男子对着她躬身行礼，口称许师姐，随后请四人随他回宗门。",
            )
        from app.source_paratext import ParatextSpans
        return ParatextSpans(spans=[])

    monkeypatch.setattr(prep_pack.model_gateway, "chat_structured", fake_chat_structured)

    # 2.0.0：不再靠 event 的 source_span 间接反推段号——这条提及直接自报
    # segment_indexes（模型自己的语义判断："银色长袍女子"这个人真的在这两
    # 段画面里出场"，见 _prep_pack_gate_segment_indexes 上方说明：段号入口
    # 只做结构核验，不要求 display_name 本身逐字命中，合成描述性标签的
    # segment_indexes 因此不会被这道结构闸打回空）。
    events = [_event(
        "ev_001",
        characters=[{
            "display_name": label, "is_background_extra": True,
            "segment_indexes": list(range(from_segment, to_segment + 1)),
        }],
    )]
    characters, scene_list, props, functional_extras, errors, stats, *_ = _resolve(
        conn, events=events, source_text=source_text,
    )

    assert errors == []
    by_portrait = {c["portrait_id"]: c for c in characters}
    assert "cp-xuqing" in by_portrait, "绿：许清必须真的绑定成功"
    entry = by_portrait["cp-xuqing"]
    # 1.11.0→1.11.1（真实回归，撤回替换手段）→2.0.0（label_literal 字段
    # 整体撤下，纯范围收窄，不是结构性恒真——见 PREP_PACK_VERSION 上方
    # 2.0.0 大注释、_prep_pack_gate_segment_indexes 上方说明）：这正是
    # label_literal 机制当初要防的真实触发案例本身——label"银色长袍女子"
    # 不是原文逐字（open 断言已经确认），candidate_verdict 只核验了"这个
    # 标签指向许清"这件事，从没核验过标签字符串本身。1.11.0 曾经把
    # display_appellation 确定性替换成这条绑定分支算出的 anchor_phrase
    # （候选判别钉证命中的卷宗段落原文），但真实生产数据证明这条 anchor_
    # phrase 不保证是短语——它是钉证命中的整段证据句，真实 EP1 复现里
    # 长达数十字、带引号和句号，替换后字幕显示的是一整句旁白而非称谓，比
    # 替换前更差（真实回归，见 1.11.1 大注释）。1.11.1 撤回替换，
    # display_appellation 保留原始合成标签 label 不变；2.0.0 进一步撤下
    # label_literal 这个纯观测性标记字段（映射台只对"绑定到谁"负责）。
    assert entry["display_appellation"] == label, (
        "标签本身非逐字时，display_appellation 必须原样保留合成标签，"
        "绝不能替换成 anchor_phrase 这类证据段落——1.11.0 曾经替换、"
        "1.11.1 因真实回归撤回"
    )
    assert entry["provenance"]["method"] == "candidate_verdict"
    assert entry["provenance"]["anchor_segments"] == [to_segment]
    assert "许师姐" in entry["provenance"]["anchor_phrase"]
    assert not any(e["label"] == label for e in functional_extras), (
        "绑定成功后，这个标签不能再出现在 functional_extras 里"
    )
    # 卷宗必须真的把事件跨度两段都递给了模型（不是模型碰巧选对）。
    assert "口称许师姐" in seen["prompt"]
    assert "面色苍白" in seen["prompt"]


# ---------------------------------------------------------------------------
# 1.11.0（任务①，见 PREP_PACK_VERSION 上方大注释）：反幻觉主防线的姊妹
# 判定——标签用词接地（provenance.label_literal）。上面的 candidate_verdict
# 测试覆盖了"非逐字但有可用的逐字 anchor_phrase 可替换"这条分支；下面四条
# 红灯分别补齐其余分支：(1) 非逐字且没有任何可用的本地逐字材料时，绝不
# 伪造替换，保留原始标签、只标记 label_literal=False；(2) candidate_verdict
# 钉中的卷宗条目若被确定性截断（带省略标记，不再是原文纯净子串）时，同样
# 不得当作逐字替代品使用；(3)/(4) functional_extras 侧 label_literal 的
# True/False 两个取值都要有覆盖（此前只测过 discovery 分支的 False 与
# absorbed_speaker 分支的 False，两条 True 分支此前完全没有回归）；最后一条
# 覆盖发布前自校验能抓出 label_literal 声明与实际复核结果不一致的情形。
# ---------------------------------------------------------------------------

def test_functional_candidate_dossier_reserves_quota_for_candidate_anchor_when_event_span_fills_budget():
    """红→绿核心场景（1.8.2，真实事故形状复现）：标签所属事件的
    source_span 连续覆盖 12 段，恰好等于 _PREP_PACK_FUNCTIONAL_CANDIDATE_
    DOSSIER_MAX_ENTRIES；候选"许清"唯一的锚点段（含已确认别名"许师姐"）
    落在事件跨度之外。1.8.1 的"A 侧全收，剩余预算才给 B 侧"在这里必然打空
    B 侧——A 侧单独就有 12 条，选择循环遍历完 A 侧时 selected 已经等于
    MAX_ENTRIES，B 侧一条都轮不到（可用下面的变异验证复现：把保底配额改回
    "reserve_a = len(primary_indexes)"、"reserve_b = 0" 就会看到这个用例
    变红）。1.8.2 按层保底配额后，A、B 两侧都必须有代表段进卷宗。"""
    event_scene_paragraphs = [
        f"殿内烛火摇曳，气氛凝重，此为事发经过第{i}段。" for i in range(1, 13)
    ]
    candidate_paragraphs = [
        "一个面色苍白，看不出年纪的女子，穿着一身银色长袍，站在那里面无表情地望着孟浩。",
        "绿袍男子对着她躬身行礼，口称许师姐，随后请四人随他回宗门。",
    ]
    paragraphs = event_scene_paragraphs + candidate_paragraphs
    source_text = "\n\n".join(paragraphs)
    segments = prep_pack.index_source_segments(source_text)
    assert len(segments) == 14
    label = "银色长袍女子"
    assert label not in source_text, "复现前提：标签不是原文字面，逐字定位必然打空"

    event_span_segments = set(range(1, 13))  # 连续 12 段，恰好等于条数上限
    assert len(event_span_segments) == prep_pack._PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MAX_ENTRIES, (
        "复现前提：事件跨度本身就能塞满整份卷宗预算"
    )

    candidate_anchor_texts = {"许清": ["许清", "许师姐"]}
    dossier = prep_pack._prep_pack_functional_candidate_dossier(
        segments, label, candidate_anchor_texts, event_span_segments,
    )
    dossier_indexes = {item["segment_index"] for item in dossier}
    assert len(dossier) <= prep_pack._PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MAX_ENTRIES
    assert 14 in dossier_indexes, "B 侧（候选锚点段：许师姐所在段）必须有代表段进卷宗"
    assert any("许师姐" in item["text"] for item in dossier)
    assert dossier_indexes & event_span_segments, "A 侧（事件跨度段）也必须有代表段进卷宗，不能被B侧反向挤没"
    # 确定性：同一输入精确复现同一份卷宗（按层保底配额 + 既有优先级/邻近度
    # 规则完全是纯函数，没有随机采样）。
    assert dossier_indexes == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 14}


def test_functional_candidate_dossier_shares_anchor_quota_fairly_across_multiple_candidates():
    """用例二（1.8.2 多候选公平性，见 PREP_PACK_VERSION 上方 1.8.2 大注释第
    3 点纪律：'多候选时 B 侧配额要在候选之间公平分配'）：两个候选，"孟浩"
    模拟本章高频出现的主角（20 段独立提及），"许清"只出现 2 次且都排在
    文档末尾。若 B 侧仍按 1.8.1 的"全部候选锚点段落混在一起、无邻近度参照
    时退化为文档顺序"处理，前 12 个文档顺序命中全部是孟浩，许清两段（排在
    第 21、22 段）一条都进不了卷宗——跟 A 侧曾经淹没 B 侧是同一个"主角
    淹没预算"陷阱，只是这次发生在 B 侧内部的候选粒度。1.8.2 的按候选
    公平轮转合并必须让许清至少有一段进卷宗。"""
    paragraphs = [
        f"孟浩在演武场上独自练剑，汗流浃背，此为第{i}件事。" for i in range(1, 21)
    ]
    paragraphs.append("许清默默递上一方手帕，什么话也没说。")
    paragraphs.append("许清转身离开，衣袂翻飞，再未回头。")
    source_text = "\n\n".join(paragraphs)
    segments = prep_pack.index_source_segments(source_text)
    assert len(segments) == 22
    label = "不存在的标签"  # 无字面命中、无事件跨度：纯 B 侧候选公平性测试

    candidate_anchor_texts = {"孟浩": ["孟浩"], "许清": ["许清"]}
    dossier = prep_pack._prep_pack_functional_candidate_dossier(
        segments, label, candidate_anchor_texts,
    )
    dossier_indexes = {item["segment_index"] for item in dossier}
    assert len(dossier) <= prep_pack._PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MAX_ENTRIES
    assert {21, 22} <= dossier_indexes, "低频候选许清必须也有代表段进入卷宗，不能被高频候选孟浩挤没"
    assert any("孟浩" in item["text"] for item in dossier), "高频候选孟浩仍应正常占用剩余配额，不是被排斥"
    # 确定性：同一输入精确复现同一份卷宗。
    assert dossier_indexes == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 21, 22}


# ---------------------------------------------------------------------------
# 1.8.3（真实数据、同一晚同一事故的第四层根因，provider_calls id=10520 可
# 复核，见 PREP_PACK_VERSION 上方 1.8.3 大注释）：1.8.2 的 A/B 两侧保底
# 配额确实生效——EP1 目标标签"银色长袍女子"的卷宗从纯事件跨度变成了段
# 31,33-42,60，B 侧成功挤进 1 段——但目标依然失败：①条数配额没有对应的
# 字数配额，A 侧那 11 段大段外貌/环境描写几乎吃光 MAX_CHARS，B 侧"保底
# 4 条"里只有排在最前面、恰好塞得进剩余预算的 1 条真被选择循环收录；
# ②那唯一挤进去的 1 段还被候选轮转顺序里排第一的主角类候选占了。以下
# 两组用例分别覆盖改动一（保底粒度下沉到每个候选 + 字数预算同粒度兜底）
# 与改动二（候选集补全为逐字命中∪人物谱注册区间两类并集）。
# ---------------------------------------------------------------------------

def test_functional_candidate_dossier_guarantees_every_candidate_a_segment_when_a_side_text_is_long():
    """红→绿核心场景（1.8.3 改动一，真实事故形状复现）：A 侧（事件跨度段）
    的 4 段文本本身很长（大段环境描写，合计吃掉 MAX_CHARS 的 99%），只给
    B 侧留极小余量；B 侧有三个候选，候选轮转顺序里排第一的"孟浩"锚点段
    最短、最先被选择循环尝试因此塞得进剩余预算，"许清""王有材"的唯一
    锚点段完全没有机会——这正是 1.8.2"B 侧保底 4 条"这个位置数字治不了的
    问题：保底的是配额位置，不是"配额一定进得去卷宗"。

    修前（可用下面的变异验证复现，或直接用本文件同目录的 git 历史版本
    验证）：_prep_pack_functional_candidate_dossier 只有"孟浩"的段进卷宗，
    "许清""王有材"一段都没有。修后：三个候选各自的锚点段都必须进卷宗，
    不管 A 侧文本多长。"""
    max_chars = prep_pack._PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MAX_CHARS
    reserve_a = prep_pack._PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MIN_SIDE_ENTRIES
    per_event_paragraph_chars = (max_chars * 99 // 100) // reserve_a

    def _segment(index: int, text: str) -> SourceSegment:
        return SourceSegment(segment_id=f"s{index}", text=text, start_offset=0, end_offset=len(text))

    event_texts = [
        ("殿内金碧辉煌，陈设奢华，此为事发现场描写第" + str(i) + "段。" + "香烟缭绕不散，" * 400)
        [:per_event_paragraph_chars]
        for i in range(1, reserve_a + 1)
    ]
    candidate_texts = [
        "孟浩独自站在最前方，神情冷峻，双手负于身后，一言不发地凝视着远方的天际线。",
        "许清安静地立于左侧，衣袂随风轻轻摆动，始终一言不发地望着前方发呆出神。",
        "王有材缩在人群后方角落，面露惧色，双腿微微发抖，不敢抬头看向任何人。",
    ]
    all_texts = event_texts + candidate_texts
    segments = [_segment(i, text) for i, text in enumerate(all_texts)]
    assert sum(len(t) for t in event_texts) > max_chars * 0.9, (
        "复现前提：A 侧四段合计必须几乎吃光 MAX_CHARS，B 侧才会被挤压"
    )

    label = "不存在的标签"
    event_span_segments = set(range(1, reserve_a + 1))
    candidate_anchor_texts = {"孟浩": ["孟浩"], "许清": ["许清"], "王有材": ["王有材"]}

    dossier = prep_pack._prep_pack_functional_candidate_dossier(
        segments, label, candidate_anchor_texts, event_span_segments,
    )
    dossier_indexes = {item["segment_index"] for item in dossier}
    total_chars = sum(len(item["text"]) for item in dossier)

    mengh_idx, xuqing_idx, wangyoucai_idx = len(event_texts) + 1, len(event_texts) + 2, len(event_texts) + 3
    assert {mengh_idx, xuqing_idx, wangyoucai_idx} <= dossier_indexes, (
        "三个候选各自的唯一锚点段都必须进卷宗，不能只有候选轮转顺序里排第一的孟浩"
    )
    assert any("许清" in item["text"] for item in dossier)
    assert any("王有材" in item["text"] for item in dossier)
    assert len(dossier) <= prep_pack._PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MAX_ENTRIES
    assert total_chars <= max_chars, "保底层的确定性截断必须仍然守住 MAX_CHARS 总预算"
    # 确定性：同一输入任何时候重跑得到同一份卷宗（纯函数，无随机采样）。
    dossier_again = prep_pack._prep_pack_functional_candidate_dossier(
        segments, label, candidate_anchor_texts, event_span_segments,
    )
    assert dossier == dossier_again


def test_functional_candidate_dossier_shares_char_budget_across_candidates_when_dossier_is_tight(
    monkeypatch,
):
    """变异验证（1.8.3 改动一）：把"每候选保底"破坏成 1.8.2 的旧行为
    （B 侧只按位置数量保底、不按候选粒度、保底段仍可被字数预算跳过），
    上面那条红→绿用例必须变红——证明这条测试确实在验证按候选粒度的字数
    保底，不是碰巧绿了。验证完立即用 monkeypatch 撤销，不污染其它用例。"""
    def legacy_side_only_dossier(segments, label, candidate_anchor_texts, event_span_segments=frozenset()):
        # 1.8.2 旧行为：A/B 两侧各自按位置数量保底 MIN_SIDE_ENTRIES 条，
        # 不区分候选、保底段仍可被字数预算跳过（`continue` 而非确定性
        # 截断）——精简复刻，只用于证明"如果退回旧逻辑，红→绿用例会变红"。
        total_segments = len(segments)
        event_span_indexes = sorted(
            {i - 1 for i in event_span_segments if 1 <= i <= total_segments},
        )
        event_span_index_set = set(event_span_indexes)
        label_text_indexes, anchor_pool_ordered, _per_candidate = (
            prep_pack._prep_pack_functional_candidate_anchor_pool(
                segments, label, candidate_anchor_texts, event_span_indexes, event_span_index_set,
            )
        )
        primary_indexes = event_span_indexes + label_text_indexes
        min_side = prep_pack._PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MIN_SIDE_ENTRIES
        reserve_a = min(min_side, len(primary_indexes))
        reserve_b = min(min_side, len(anchor_pool_ordered))
        guaranteed_a, overflow_a = primary_indexes[:reserve_a], primary_indexes[reserve_a:]
        guaranteed_b, overflow_b = anchor_pool_ordered[:reserve_b], anchor_pool_ordered[reserve_b:]
        ordered = guaranteed_a + guaranteed_b + overflow_a + overflow_b
        selected: list[int] = []
        used_chars = 0
        max_entries = prep_pack._PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MAX_ENTRIES
        max_chars = prep_pack._PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MAX_CHARS
        for index in ordered:
            if len(selected) >= max_entries:
                break
            seg_text = segments[index].text
            if selected and used_chars + len(seg_text) > max_chars:
                continue
            selected.append(index)
            used_chars += len(seg_text)
        selected.sort()
        return [{"segment_index": i + 1, "text": segments[i].text} for i in selected]

    patch_prep_pack_everywhere(monkeypatch, "_prep_pack_functional_candidate_dossier", legacy_side_only_dossier)

    max_chars = prep_pack._PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MAX_CHARS
    reserve_a = prep_pack._PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MIN_SIDE_ENTRIES
    per_event_paragraph_chars = (max_chars * 99 // 100) // reserve_a

    def _segment(index: int, text: str) -> SourceSegment:
        return SourceSegment(segment_id=f"s{index}", text=text, start_offset=0, end_offset=len(text))

    event_texts = [
        ("殿内金碧辉煌，陈设奢华，此为事发现场描写第" + str(i) + "段。" + "香烟缭绕不散，" * 400)
        [:per_event_paragraph_chars]
        for i in range(1, reserve_a + 1)
    ]
    candidate_texts = [
        "孟浩独自站在最前方，神情冷峻，双手负于身后，一言不发地凝视着远方的天际线。",
        "许清安静地立于左侧，衣袂随风轻轻摆动，始终一言不发地望着前方发呆出神。",
        "王有材缩在人群后方角落，面露惧色，双腿微微发抖，不敢抬头看向任何人。",
    ]
    all_texts = event_texts + candidate_texts
    segments = [_segment(i, text) for i, text in enumerate(all_texts)]
    label = "不存在的标签"
    event_span_segments = set(range(1, reserve_a + 1))
    candidate_anchor_texts = {"孟浩": ["孟浩"], "许清": ["许清"], "王有材": ["王有材"]}

    dossier = prep_pack._prep_pack_functional_candidate_dossier(
        segments, label, candidate_anchor_texts, event_span_segments,
    )
    dossier_indexes = {item["segment_index"] for item in dossier}
    xuqing_idx, wangyoucai_idx = len(event_texts) + 2, len(event_texts) + 3
    assert not ({xuqing_idx, wangyoucai_idx} <= dossier_indexes), (
        "变异验证：退回 1.8.2 旧算法后，许清/王有材必须至少缺一个——"
        "证明红→绿用例确实在验证 1.8.3 的按候选字数保底，不是碰巧通过"
    )


# ---------------------------------------------------------------------------
# 1.8.4（协调层复核，见 PREP_PACK_VERSION 上方 1.8.4 大注释）：两件事。
# 一，1.8.3 改动二（候选集补入人物谱注册区间覆盖本集的"乙类"候选）真实
# 落库后确认误绑（EP1/EP2 的"赵武刚"被误判为"绿袍男子"，赵武刚在这两章
# 原文一次都没被提到）——原理性失效，已完整回退，候选集恢复为甲类单一
# 来源。二，真实数据复核发现"每候选保底"自身此前一直没暴露的根因：候选
# 锚点段落如果恰好落在事件跨度（A 侧主锚点）内部，旧版
# _prep_pack_functional_candidate_anchor_pool 对事件跨度内的段落一律
# 跳过候选匹配，这个候选就从 per_candidate_indexes 里彻底消失，保底对它
# 形同虚设——目标案例"许清"的确认别名"许师姐"两次出现都恰好落在事件跨度
# 并集内部，1.8.1-1.8.3 三轮修复因此全部失效。已修复：候选匹配现在覆盖
# 全部段落，不再因为段落已被事件跨度收录就跳过。
# ---------------------------------------------------------------------------

def test_functional_candidate_dossier_guarantees_candidate_anchor_segment_inside_event_span():
    """红→绿核心场景（1.8.4，真实数据形状复现，provider_calls id=10582 可
    复核）：事件链抽取模型给标签"银色长袍女子"关联的事件 source_span 并集
    真实连续覆盖 25 段（本用例用等价的 20 段复现同一形状），候选"许清"的
    确认别名"许师姐"真实出现在这个并集内部的第 15 段——不是在事件跨度
    之外（那种形状 1.8.2/1.8.3 已经用另外两条用例覆盖过，见上面
    test_functional_candidate_dossier_reserves_quota_for_candidate_anchor_
    when_event_span_fills_budget 与 test_functional_candidate_dossier_
    guarantees_every_candidate_a_segment_when_a_side_text_is_long，那两条
    在 1.8.4 之前就已经绿了，不是这次要修的缺口）。

    旧版 _prep_pack_functional_candidate_anchor_pool 对"已被事件跨度收录
    的段"统一 continue，候选匹配根本不会执行到这些段落——第 15 段因此从
    per_candidate_indexes["许清"] 里永远消失，每候选保底对它形同虚设，
    最终卷宗只有事件跨度自己按文档顺序排前面的 12 段（1-12），第 15 段
    一条都不在（可用下面的变异验证复现）。修后：候选匹配对事件跨度内的
    段落同样执行，第 15 段必须作为"许清"的保底段进入卷宗，不管它是否也在
    事件跨度集合里。"""
    event_paragraphs = [f"殿内烛火摇曳，气氛凝重，此为事发经过第{i}段。" for i in range(1, 21)]
    event_paragraphs[14] = "许师姐默默立于人群之中，一言不发，此为事发经过第15段。"
    source_text = "\n\n".join(event_paragraphs)
    segments = prep_pack.index_source_segments(source_text)
    assert len(segments) == 20
    label = "不存在的标签"
    event_span_segments = set(range(1, 21))  # 连续 20 段，远超 MAX_ENTRIES
    assert len(event_span_segments) > prep_pack._PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MAX_ENTRIES, (
        "复现前提：事件跨度本身就远超卷宗预算，候选锚点段必须靠每候选保底才能挤进去"
    )

    candidate_anchor_texts = {"许清": ["许清", "许师姐"]}

    dossier = prep_pack._prep_pack_functional_candidate_dossier(
        segments, label, candidate_anchor_texts, event_span_segments,
    )
    dossier_indexes = {item["segment_index"] for item in dossier}
    assert len(dossier) <= prep_pack._PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MAX_ENTRIES
    assert 15 in dossier_indexes, (
        "候选'许清'唯一的锚点段（第15段，落在事件跨度内部）必须作为每候选"
        "保底段进入卷宗，即使它同时也是事件跨度的一部分"
    )
    assert any(
        item["segment_index"] == 15 and "许师姐" in item["text"] for item in dossier
    )
    # 确定性：同一输入任何时候重跑得到同一份卷宗（纯函数，无随机采样）。
    dossier_again = prep_pack._prep_pack_functional_candidate_dossier(
        segments, label, candidate_anchor_texts, event_span_segments,
    )
    assert dossier == dossier_again


def test_functional_candidate_dossier_loses_in_span_candidate_anchor_with_legacy_anchor_pool(
    monkeypatch,
):
    """变异验证（1.8.4）：把 _prep_pack_functional_candidate_anchor_pool
    退回旧版"事件跨度内的段一律跳过候选匹配"的行为，上面那条红→绿用例的
    核心断言必须变红——证明这条测试确实在验证 1.8.4 的修复，不是碰巧绿了。
    验证完立即用 monkeypatch 撤销，不污染其它用例。"""
    def legacy_anchor_pool(segments, label, candidate_anchor_texts, event_span_indexes, event_span_index_set):
        # 旧版（1.8.1-1.8.3）行为：事件跨度内的段一律 continue，候选匹配
        # 根本不会执行到——精简复刻，只用于证明"如果退回旧逻辑，红→绿
        # 用例会变红"。
        label_text_indexes: list[int] = []
        per_candidate_indexes: dict[str, list[int]] = {name: [] for name in candidate_anchor_texts}
        for index, seg in enumerate(segments):
            if index in event_span_index_set:
                continue
            if label and label in seg.text:
                label_text_indexes.append(index)
                continue
            for name, forms in candidate_anchor_texts.items():
                if any(form and form in seg.text for form in forms):
                    per_candidate_indexes[name].append(index)
        proximity_anchor = event_span_indexes or label_text_indexes
        if proximity_anchor:
            for indexes in per_candidate_indexes.values():
                indexes.sort(
                    key=lambda index: (min(abs(index - anchor) for anchor in proximity_anchor), index),
                )
        candidate_order = list(candidate_anchor_texts.keys())
        seen: set[int] = set()
        anchor_pool_ordered: list[int] = []
        max_round = max((len(indexes) for indexes in per_candidate_indexes.values()), default=0)
        for round_idx in range(max_round):
            for name in candidate_order:
                indexes = per_candidate_indexes[name]
                if round_idx >= len(indexes):
                    continue
                index = indexes[round_idx]
                if index in seen:
                    continue
                seen.add(index)
                anchor_pool_ordered.append(index)
        return label_text_indexes, anchor_pool_ordered, per_candidate_indexes

    patch_prep_pack_everywhere(monkeypatch, "_prep_pack_functional_candidate_anchor_pool", legacy_anchor_pool)

    event_paragraphs = [f"殿内烛火摇曳，气氛凝重，此为事发经过第{i}段。" for i in range(1, 21)]
    event_paragraphs[14] = "许师姐默默立于人群之中，一言不发，此为事发经过第15段。"
    source_text = "\n\n".join(event_paragraphs)
    segments = prep_pack.index_source_segments(source_text)
    label = "不存在的标签"
    event_span_segments = set(range(1, 21))
    candidate_anchor_texts = {"许清": ["许清", "许师姐"]}

    dossier = prep_pack._prep_pack_functional_candidate_dossier(
        segments, label, candidate_anchor_texts, event_span_segments,
    )
    dossier_indexes = {item["segment_index"] for item in dossier}
    assert 15 not in dossier_indexes, (
        "变异验证：退回旧版 anchor_pool 后，候选'许清'落在事件跨度内的唯一"
        "锚点段必须缺失——证明红→绿用例确实在验证 1.8.4 的修复，不是碰巧通过"
    )


def test_registered_only_candidate_never_enters_candidate_set_after_1_8_4_revert(monkeypatch):
    """回退验证（1.8.4，见 PREP_PACK_VERSION 上方 1.8.4 大注释）：1.8.3
    改动二曾经让"人物谱注册区间覆盖本集、但本集原文未直接点名"的角色
    （乙类）也能进候选集——本用例正是那条回退验证：李富贵人物谱
    ep_start=1（覆盖本集），有已确认别名（小胖子、胖爷），但本集原文一次
    都没提到他本人或任何别名，只有一句纯外貌描写。回退后，候选集必须
    只剩甲类判据（规范名/已确认别名在本集原文逐字出现），李富贵不再有
    资格进候选集——候选集因此为空，函数必须在发起任何模型调用之前就
    返回 None（跟既有"候选集为空仍不发模型调用"纪律完全一致），绝不能
    再走到"人物谱登记显示在本集活跃"那条已删除的提示词分区。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) VALUES "
        "('cp-lifugui','p1','李富贵',1,NULL)"  # ep_start=1 覆盖本集，人物谱确实登记本集活跃
    )
    _seed_bible_characters(conn, "p1", [
        _bible_character(
            "李富贵", appearance_canonical="身形圆胖皮肤白净",
            aliases=[_bible_alias("小胖子"), _bible_alias("胖爷")],
        ),
    ])

    async def fake_discovery(project_id, episode_no, source_text, bible, *, generate_portraits=True):
        return {"added": [], "resolutions": [], "errors": [], "warnings": [], "skipped": []}

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", fake_discovery)
    patch_portraits_everywhere(monkeypatch, "persist_screenplay_character_resolutions", lambda *a, **k: [])

    async def boom_chat_structured(messages, **kwargs):
        raise AssertionError("候选集为空时绝不该发起候选判别模型调用——乙类候选已在1.8.4回退")

    monkeypatch.setattr(prep_pack.model_gateway, "chat_structured", boom_chat_structured)

    label = "白白净净的胖少年"
    events = [_event("ev_001", characters=[{"display_name": label, "is_background_extra": True}])]
    characters, scene_list, props, functional_extras, errors, stats, *_ = _resolve(
        conn, events=events, episode_no=2,
        source_text="白白净净身子较胖的少年跟在人群身后，一声不吭。",
    )

    assert errors == []
    assert not any(c["display_name"] == "李富贵" for c in characters)
    assert any(e["label"] == label for e in functional_extras)


# "都不是/无法确定"落 functional_extras、段号不在卷宗则拒绝这两条通用
# 边界，既有的 test_candidate_verdict_no_match_selected_stays_functional_
# extra / test_candidate_verdict_out_of_dossier_segment_rejected_stays_
# functional_extra（1.8.0 时代已落地，均使用甲类候选"许清"真实触发模型
# 调用，见上方约 2927/2968 行）已经覆盖，1.8.4 不重复造一份同形状的用例。


# ---------------------------------------------------------------------------
# 1.8.5：事件链抽取给角色起标签的提示词——原文有称谓就必须逐字用称谓，不能
# 自己综合一个外貌/关系描述短语（真实回归根因：EP1/EP5/EP8"许清"/"赵武刚"
# 明明在人物谱里登记着原文真实用过的称谓，标签却写成了模型自己综合的描述
# 短语，导致本该零成本命中别名表的绑定退化成一次不保真的候选判别模型
# 裁决，甚至完全绑不上，见 PREP_PACK_VERSION 上方 1.8.5 大注释）。
#
# 这里测的是 _extract_chunk 构造出的提示词本身，不是真实模型输出——跟
# test_scene_true_name_hypothesis_verdict_prompt_uses_scene_semantics
# （上方约 2581 行）同一测法：monkeypatch model_gateway.chat_structured
# 截获发给模型的提示词全文，断言其中包含（或不包含）特定指令片段。
# ---------------------------------------------------------------------------

def _flatten(text: str) -> str:
    """去掉全部空白（含提示词多行 f-string 里的换行/两格缩进），只留字符
    序列本身用于子串断言——提示词源码里的换行位置属于排版细节，不是被测
    语义的一部分，断言不该因为源码换行点挪动就跟着碎（Chinese 本身书写
    不含空格，唯一残留的空格只来自英文标识符/数字，两边同时展平即可
    无损对齐）。"""
    return re.sub(r"\s+", "", text)


def _fake_chunk_response(*, display_name: str = "沈师姐") -> "prep_pack._ChunkResponse":
    return prep_pack._ChunkResponse(
        events=[
            prep_pack._ModelEvent(
                event_id="ev_001",
                summary="测试事件",
                source_span=prep_pack._ModelEventSpan(from_segment=1, to_segment=1),
                source_evidence=[
                    prep_pack._ModelSourceEvidence(segment_index=1, quote="沈师姐"),
                ],
                key_lines=[],
                characters=[
                    prep_pack._ModelCharacterMention(
                        display_name=display_name, is_background_extra=False,
                        suspected_true_name=None,
                    ),
                ],
                scenes=[],
            ),
        ],
        paratext_segments=[],
    )


# ---------------------------------------------------------------------------
# 2.0.0 新增字段补测（架构改造 48e01ff 遗留的测试缺口）：props（道具，文字
# 描述，无图像素材库）与 appellation_map（模糊称谓 -> 人物谱正名的显式映射
# 表）此前在整个 tests/ 目录下没有任何断言落在产出的 payload 上——128 个
# 测试全绿是因为没有一条在看新东西。断言必须落在 _resolve_assets/
# _prep_pack_build_prop_manifest/_prep_pack_build_appellation_map 实际产出
# 的字典/列表内容上，不是"字段存在"这种空判据。
# ---------------------------------------------------------------------------

def test_prop_mention_with_literal_evidence_appears_in_asset_manifest_props():
    """模型报了一个道具、逐字出现在它自己申报的段落里——必须出现在
    asset_manifest.props，label/description/segment_indexes 三项均须与
    提及一致（还原生产 payload 形状，不是只查字段是否存在）。"""
    conn = _make_conn()
    characters, scene_list, props, functional_extras, errors, stats, *_ = _resolve(
        conn,
        source_text="甲一从怀中取出一枚血玉玦，放在桌上。",
        prop_mentions=[{
            "label": "血玉玦",
            "description": "一枚泛着血光的玉玦，边缘刻着古老符文",
            "segment_indexes": [1],
        }],
    )
    assert errors == []
    assert props == [{
        "label": "血玉玦",
        "description": "一枚泛着血光的玉玦，边缘刻着古老符文",
        "segment_indexes": [1],
        "provenance": {
            "method": "direct", "anchor_segments": [1], "anchor_phrase": "血玉玦",
        },
    }]


def test_prop_mention_without_literal_evidence_is_blocked_not_published():
    """道具没有角色/场景侧那种别名注册表/身份发现豁免路径（2.0.0 有意
    为之，见 _prep_pack_build_prop_manifest 上方大注释）：编造的、原文里
    查无实据的道具必须被挡掉，绝不能进 asset_manifest.props——跟场景侧
    "没证据就当未解析"同一处置，是合法丢弃，不是需要报错阻断门禁的失败。"""
    conn = _make_conn()
    characters, scene_list, props, functional_extras, errors, stats, *_ = _resolve(
        conn,
        source_text="甲一从怀中取出一枚血玉玦，放在桌上。",
        prop_mentions=[{
            "label": "屠龙宝刀",
            "description": "一把传说中的绝世神兵",
            "segment_indexes": [1],
        }],
    )
    assert props == [], "原文里一次都没出现过的道具绝不能进 asset_manifest.props"
    assert errors == [], "道具证据缺失是合法丢弃，不是需要阻断整体发布的门禁错误"


def test_prop_mention_claiming_unverified_segment_keeps_only_evidenced_segment():
    """混合场景：提及自报了两个段号，但道具字样只在其中一段真的逐字出现——
    逐段字面证据闸必须只保留验得过的那一段，不是"整条提及要么全收要么
    全弃"，也不是把没证据的段号一并放行发布。"""
    conn = _make_conn()
    characters, scene_list, props, functional_extras, errors, stats, *_ = _resolve(
        conn,
        source_text=(
            "甲一从怀中取出一枚血玉玦，放在桌上。"
            "\n\n林動只是随口一提往事，并未提到那枚玉玦。"
        ),
        prop_mentions=[{
            "label": "血玉玦",
            "description": "一枚泛着血光的玉玦",
            "segment_indexes": [1, 2],
        }],
    )
    assert len(props) == 1
    assert props[0]["segment_indexes"] == [1], (
        "第2段申报没有道具字样的字面证据，必须被剔除，不能整条放行"
    )


def test_prop_appearing_in_multiple_segments_merges_segment_indexes_as_union():
    """同一道具由两条不同提及分别在两段各命中一次——manifest 里必须是并集
    [1, 2]，不能被后处理的第二条提及覆盖成只剩最后一次命中的段号。"""
    conn = _make_conn()
    characters, scene_list, props, functional_extras, errors, stats, *_ = _resolve(
        conn,
        source_text=(
            "甲一从怀中取出一枚血玉玦，放在桌上。"
            "\n\n林動盯着那枚血玉玦，若有所思。"
        ),
        prop_mentions=[
            {"label": "血玉玦", "description": "一枚泛着血光的玉玦", "segment_indexes": [1]},
            {"label": "血玉玦", "description": "一枚泛着血光的玉玦", "segment_indexes": [2]},
        ],
    )
    assert len(props) == 1, "同一 label 的两条提及必须合并为 manifest 里的同一条道具，不是两条"
    assert props[0]["segment_indexes"] == [1, 2], (
        "两条提及各自命中不同段落，必须合并为排序去重的并集，不是被第二条覆盖"
    )


# ---------------------------------------------------------------------------
# 角色侧 segment_indexes 并集推导：既有断言只覆盖过单条提及、单段命中的
# entry["segment_indexes"] == [1] 这一种最简单形状（test_known_alias_
# flagged_as_background_extra_still_binds_to_its_portrait 等）。这里补一条
# 同一角色被两条不同措辞的提及分别命中、分落不同段落的场景。
# ---------------------------------------------------------------------------

def test_character_manifest_segment_indexes_is_union_of_multiple_mentions_not_overwrite(
    monkeypatch,
):
    """manifest 条目的 segment_indexes 必须是全部贡献提及的并集（排序
    去重）：如果实现退化成"后一条提及直接覆盖"，第二条提及处理完后 entry
    会丢失第一条提及贡献的段号 1。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-lfg','p1','李富贵',1,NULL)"
    )
    _seed_bible_characters(conn, "p1", [
        _bible_character("李富贵", aliases=[_bible_alias("小胖子")]),
    ])

    def boom_character(*_a, **_k):
        raise AssertionError("别名注册表命中唯一目标，不应该回炉重新消歧")

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", boom_character)

    events = [_event("ev_001", characters=[
        {"display_name": "李富贵", "is_background_extra": False},
        {"display_name": "小胖子", "is_background_extra": False},
    ])]
    characters, *_ = _resolve(
        conn, events=events, episode_no=1,
        source_text="李富贵站在门口。\n\n小胖子挥了挥手。\n\n小胖子又笑了笑。",
    )
    by_portrait = {c["portrait_id"]: c for c in characters}
    assert by_portrait["cp-lfg"]["segment_indexes"] == [1, 2, 3], (
        "'李富贵'命中段1、'小胖子'命中段2和3，必须在同一 manifest 条目里"
        "合并为排序去重的并集 [1, 2, 3]，不能被后处理的提及覆盖丢失先前"
        "贡献的段号"
    )


# ---------------------------------------------------------------------------
# appellation_map（2.0.0 新增，2.0.1 重做真源）：直接消费 _resolve_assets
# 解析每条角色提及时原地记下的结论（见 _prep_pack_build_appellation_map
# 上方"2.0.1 根因"大注释），不再拿 characters[].aliases 反查
# character_mentions——旧实现把"能不能安全进跨集别名注册表"（aliases 的
# 门槛）误当成"这条提及有没有解析出身份"的判据，导致合成描述性称谓
# （"穿杂役衫的魁梧大汉"一类）已经真实解析成功却从映射表里静默消失，
# 详见 tests/test_prep_pack_asset_discovery.py 里 2.0.1 之前的
# _known_bug 版本（已改成正向断言，见下方
# test_appellation_map_includes_resolved_composite_description_mention）。
# ---------------------------------------------------------------------------

def test_appellation_map_maps_ambiguous_mention_to_canonical_name_not_raw_text():
    """单元测试：直接喂给 _prep_pack_build_appellation_map 一条已解析
    结论（_resolve_assets 通过 appellation_resolutions 出参记录的形状），
    映射行必须给出人物谱正名，不是把模糊称谓原样透传。"""
    resolutions = [{
        "raw_mention": "小胖子", "segment_indexes": [3],
        "identity_id": "bible:李富贵", "canonical_appellation": "李富贵",
    }]
    rows = prep_pack._prep_pack_build_appellation_map(resolutions)
    assert rows == [{
        "raw_mention": "小胖子", "segment_index": 3,
        "identity_id": "bible:李富贵", "canonical_appellation": "李富贵",
    }]
    assert rows[0]["canonical_appellation"] != "小胖子", (
        "canonical_appellation 必须是人物谱正名，不是原始模糊称谓原样透传"
    )


def test_appellation_map_multiple_appellations_for_same_person_all_point_to_same_identity():
    """同一个人的多个不同称谓（小胖子/胖公子/李富贵本名）各自成行，必须
    都指向同一个 identity_id、同一个正名——不是各算各的、也不是只保留
    最后一个。"""
    resolutions = [
        {"raw_mention": "小胖子", "segment_indexes": [1],
         "identity_id": "bible:李富贵", "canonical_appellation": "李富贵"},
        {"raw_mention": "胖公子", "segment_indexes": [2],
         "identity_id": "bible:李富贵", "canonical_appellation": "李富贵"},
        {"raw_mention": "李富贵", "segment_indexes": [3],
         "identity_id": "bible:李富贵", "canonical_appellation": "李富贵"},
    ]
    rows = prep_pack._prep_pack_build_appellation_map(resolutions)
    assert {row["raw_mention"] for row in rows} == {"小胖子", "胖公子", "李富贵"}
    assert {row["identity_id"] for row in rows} == {"bible:李富贵"}
    assert {row["canonical_appellation"] for row in rows} == {"李富贵"}
    assert len(rows) == 3


def test_appellation_map_matches_resolve_assets_alias_registry_conclusion(monkeypatch):
    """appellation_map 不是第二条消歧路径：人物谱别名注册表命中（零消歧
    调用直接绑定"小胖子"->李富贵，见 test_character_alias_registry_binds_
    via_bible_aliases_with_zero_other_episodes）时，_resolve_assets 通过
    appellation_resolutions 出参记下的结论必须原样流进 appellation_map，
    不是另算一遍消歧。"""
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

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", boom_character)

    events = [_event("ev_001", characters=[
        {"display_name": "小胖子", "is_background_extra": False},
    ])]
    source_text = "小胖子憨憨一笑，抓了抓头。"
    appellation_resolutions: list[dict] = []
    _resolve(
        conn, events=events, episode_no=1, source_text=source_text,
        appellation_resolutions=appellation_resolutions,
    )

    appellation_map = prep_pack._prep_pack_build_appellation_map(appellation_resolutions)
    rows = [row for row in appellation_map if row["raw_mention"] == "小胖子"]
    assert rows, "appellation_map 必须收录别名注册表命中的这条称谓"
    assert all(row["identity_id"] == "bible:李富贵" for row in rows)
    assert all(row["canonical_appellation"] == "李富贵" for row in rows)
    assert all(row["segment_index"] == 1 for row in rows)


def test_appellation_map_includes_resolved_composite_description_mention(monkeypatch):
    """2.0.1 回归（原为已知 bug 的红灯，现改为正向断言，见
    _prep_pack_build_appellation_map 上方"2.0.1 根因"大注释）：合成描述性
    称谓（"穿杂役衫的魁梧大汉"经消歧正确解析到赵武刚，按 aliases 的字面
    证据门槛不会进别名库——见 test_composite_description_resolved_via_
    discovery_bypasses_literal_gate）真实发布进 asset_manifest.characters
    之后，必须出现在 appellation_map 里——这正是这个字段存在的理由本身
    （模糊称谓 -> 正名）。appellation_map 不该拿 aliases 的字面证据门槛
    当自己的真源。

    生产链路里 segment_indexes 由模型自报、只经过结构闸（见
    _prep_pack_gate_segment_indexes 上方说明），不要求标签本身逐字出现——
    跟本文件模块级 _mentions_with_segment_indexes 测试夹具（逐字搜索算
    段号，会让这种合成标签自然拿到空段号，从而巧合地掩盖这条回归想验证
    的东西）不同，这里手写 character_mentions 还原生产真实形态：合成
    标签依然携带非空 segment_indexes。"""
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

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", fake_disambiguate)
    patch_portraits_everywhere(monkeypatch, "persist_screenplay_character_resolutions", lambda *a, **k: [])

    character_mentions = [{
        "display_name": "穿杂役衫的魁梧大汉", "suspected_true_name": None,
        "segment_indexes": [1],
    }]
    source_text = "一个穿着杂役衫的魁梧男子闯了进来，凶狠地看了众人一眼。"
    appellation_resolutions: list[dict] = []
    characters, *_ = _resolve(
        conn, character_mentions=character_mentions, source_text=source_text,
        appellation_resolutions=appellation_resolutions,
    )
    zwg = next(c for c in characters if c["display_name"] == "赵武刚")
    assert "穿杂役衫的魁梧大汉" not in zwg["aliases"], (
        "既有既定行为不变：合成描述短语不进别名注册表（防止污染跨集别名库）"
    )

    appellation_map = prep_pack._prep_pack_build_appellation_map(appellation_resolutions)
    assert appellation_map == [{
        "raw_mention": "穿杂役衫的魁梧大汉", "segment_index": 1,
        "identity_id": "bible:赵武刚", "canonical_appellation": "赵武刚",
    }], (
        "合成描述性称谓已经真实解析成功、发布进 asset_manifest.characters，"
        "必须出现在 appellation_map 里，不能因为它同时也不满足 aliases 的"
        "字面证据门槛就被连带静默丢弃"
    )


def test_appellation_map_identity_id_always_matches_asset_manifest_characters(monkeypatch):
    """协调方验收要求：appellation_map 每一行的 identity_id 必须能在
    asset_manifest.characters 里找到同一个 identity_id 的条目，且
    canonical_appellation 与那个条目的 display_name 一致——两处不能各说
    各话。混合三种解析路径覆盖：裸直接命中（孟浩）、别名注册表命中
    （小胖子 -> 李富贵）、消歧发现命中的合成描述（穿杂役衫的魁梧大汉 ->
    赵武刚，唯一会触发发现调用的一条，因为它既不是已知名字也不在别名
    注册表里）。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-mh','p1','孟浩',1,NULL)"
    )
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-lfg','p1','李富贵',1,NULL)"
    )
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp-zwg','p1','赵武刚',1,NULL)"
    )
    _seed_bible_characters(conn, "p1", [
        _bible_character("孟浩"),
        _bible_character("李富贵", aliases=[_bible_alias("小胖子")]),
        _bible_character("赵武刚"),
    ])

    async def fake_disambiguate(project_id, episode_no, source_text, bible, *, generate_portraits=True):
        return {
            "added": [], "skipped": [],
            "resolutions": [{
                "source_label": "穿杂役衫的魁梧大汉", "canonical_name": "赵武刚",
                "resolution": "future_identity",
            }],
            "errors": [], "warnings": [],
        }

    patch_portraits_everywhere(monkeypatch, "ensure_cards_for_text", fake_disambiguate)
    patch_portraits_everywhere(monkeypatch, "persist_screenplay_character_resolutions", lambda *a, **k: [])

    character_mentions = [
        {"display_name": "孟浩", "suspected_true_name": None, "segment_indexes": [1]},
        {"display_name": "小胖子", "suspected_true_name": None, "segment_indexes": [1]},
        {"display_name": "穿杂役衫的魁梧大汉", "suspected_true_name": None, "segment_indexes": [2]},
    ]
    source_text = (
        "孟浩看着小胖子憨憨一笑。"
        "\n\n一个穿着杂役衫的魁梧男子闯了进来，凶狠地看了众人一眼。"
    )
    appellation_resolutions: list[dict] = []
    characters, *_ = _resolve(
        conn, character_mentions=character_mentions, source_text=source_text,
        appellation_resolutions=appellation_resolutions,
    )
    assert {c["display_name"] for c in characters} == {"孟浩", "李富贵", "赵武刚"}, (
        "夹具前提：三条提及必须都真的解析成功，否则下面的一致性断言没有意义"
    )

    appellation_map = prep_pack._prep_pack_build_appellation_map(appellation_resolutions)
    assert appellation_map, "映射表不能是空的，否则一致性断言是空真"

    characters_by_identity = {c["identity_id"]: c for c in characters}
    for row in appellation_map:
        match = characters_by_identity.get(row["identity_id"])
        assert match is not None, (
            f"appellation_map 行 identity_id={row['identity_id']!r} 在 "
            "asset_manifest.characters 里找不到同一个 identity_id 的条目"
            "——两处各说各话"
        )
        assert row["canonical_appellation"] == match["display_name"], (
            "canonical_appellation 必须和 asset_manifest.characters 里同一"
            "实体的 display_name 一致"
        )
    # 反向也要成立：三个身份都真的在映射表里露过面，不是巧合只覆盖了其中
    # 一部分——排除掉两处"各说各话"以另一种方式蒙混过关（比如某个身份
    # 干脆没有任何映射行、却也没有被上面的循环揭穿）。
    assert {row["identity_id"] for row in appellation_map} == set(characters_by_identity)


def test_prep_pack_version_is_1_8_0():
    """版本号哨兵：本次改造（未解析角色标签候选判别——真实 EP1"银色长袍
    女子"应绑定许清却因标签类型对不上落 functional_extras 的用户诉求收口）
    新增 provenance.method="candidate_verdict" 取值、会实际改变部分此前落
    functional_extras 的标签的解析结果，版本必须推进到 1.8.0（见
    PREP_PACK_VERSION 上方大注释）。1.8.1（同一机制的后续事故修复：卷宗
    检索改用事件跨度定位，见 PREP_PACK_VERSION 上方 1.8.1 大注释）、1.8.2
    （同一机制的第三轮事故修复：卷宗预算改为按层保底配额 + B 侧候选公平
    轮转合并，见 PREP_PACK_VERSION 上方 1.8.2 大注释）、1.8.3（同一机制的
    第四轮事故修复：保底粒度下沉到每个候选、字数预算同粒度兜底 + 候选集
    扩展为逐字命中∪人物谱注册区间两类并集，见 PREP_PACK_VERSION 上方
    1.8.3 大注释）与 1.8.4（协调层复核：1.8.3 候选集扩展真实落库后确认
    误绑，完整回退候选集扩展；同时修复"每候选保底"自身此前一直没暴露的
    根因——候选锚点落在事件跨度内部时旧版扫描逻辑会跳过候选匹配，见
    PREP_PACK_VERSION 上方 1.8.4 大注释）都会实际改变发给候选判别模型的
    卷宗内容/候选名单本身，同样是 prompt-contract 变更，版本逐次推进。
    1.8.5（根因收口：往回收到事件链抽取给角色起标签这一步——原文有称谓
    就必须逐字用称谓，不能自己综合一个描述短语，见 PREP_PACK_VERSION
    上方 1.8.5 大注释）改的是 _extract_chunk 的"命名纪律"提示词分区，
    同样会实际改变部分角色标签的选词结果，是 prompt-contract 变更，
    版本继续推进——函数名/测试名沿用旧号不改，只更新断言值，避免无谓的
    大范围改名。1.9.0（真实 EP5 回归：章节标题段因模型 paratext_segments
    申报非确定性漏报，被包装成一个只覆盖 SRC0001 的"显示标题"伪事件，见
    PREP_PACK_VERSION 上方 1.9.0 大注释）把 coverage_ledger.paratext 对
    chapters.title 这一 DB 锚定子集的判定改回确定性——不再要求模型申报，
    是 ledger 判定语义变更，同样比照 1.4.1 的先例推进版本号。1.10.0（两个
    已完成根因定位的缺陷：真名裁决是非题改候选判别 + 钉证要求引句含 alias
    本身/双锚定优先、结构上不存在双锚定证据时退化为集内指代段落且可观测、
    skip_character_names 短路不再作废已核验通过的 suspected_true_name
    结论，见 PREP_PACK_VERSION 上方 1.10.0 大注释）会实际改变真名裁决的
    模型输出与部分绑定的钉证通过与否，同样是 prompt-contract 变更，版本
    继续推进。1.11.0（任务①，独立评审 blocker：反幻觉主防线覆盖面比它
    宣称的窄——candidate_verdict/resolution/alias/discovery 等解析路径
    只核验"标签指向谁"，从未核验"标签字符串本身是否逐字出现在本集原文"，
    真实 EP1"银色长袍女子"绑定许清即是一例：身份指向正确，但
    display_appellation 是模型综合的合成短语，非逐字，见 PREP_PACK_
    VERSION 上方 1.11.0 大注释的测量数据与方案取舍）：新增
    provenance.label_literal 独立判定字段（characters[]/functional_
    extras[] 每项都带），characters[] 侧非逐字时曾经确定性替换为已有的
    anchor_phrase（不新开证据检索），functional_extras[] 侧只标记（label
    同时是台词说话人匹配的连接键，不能改值）——会实际改变部分 characters[]
    条目的 display_appellation 取值，是真正的产出语义变更，比照 1.9.0 的
    先例推进版本号。1.11.1（真实回归，1.11.0 上线后首次真实生成 EP1 当场
    复现，见 PREP_PACK_VERSION 上方 1.11.1 大注释）：1.11.0 的 characters[]
    替换手段撤回——真实数据证明 candidate_verdict 分支的 anchor_phrase 是
    钉证命中的整段证据句而非称谓（EP1"许清"replace 后变成一整句带引号的
    旁白，字段不可用，比替换前更差），characters[] 改回跟 functional_
    extras[] 侧同一处置：只标记 provenance.label_literal，不替换 display_
    appellation 的取值。literal_evidence 无条件计算、label_literal 标记
    机制、functional_extras[] 侧处置三者均不变。是判定语义变更（处置手段
    从确定性替换降为如实标记），比照 1.4.1/1.6.1/1.8.1-1.8.5/1.9.0/1.10.0
    的先例推进版本号（第三位）——函数名/测试名沿用旧号不改，只更新断言
    值，避免无谓的大范围改名。

    2.0.0（用户判定链路错误，架构收窄，schema 大幅变更，版本主位推进，见
    app/production/prep_pack.py 模块 docstring 的 2.0.0 大注释）：本模块从
    "剧本台"改造成"映射台"——event_chain/hook/cliffhanger 全部砍掉，
    asset_manifest 各类条目的锚点从 event_ids 换成 segment_indexes（原文
    段号，真实重新推导，不是改名），新增 props（道具，文字描述，无图像
    素材）与 appellation_map（模糊称谓 -> 人物谱精准称谓的显式映射表）。
    这是本文件版本号推进历史里第一次真正的 schema 大版本位变更（此前全部
    是次版本/修订号），函数名沿用旧号不改，只更新断言值。

    2.0.1（bug fix，补 2.0.0 appellation_map/props 测试缺口过程中发现、
    协调方独立复现确认，见 app/production/prep_pack.py 模块 docstring 的
    2.0.1 大注释与 _prep_pack_build_appellation_map 上方"2.0.1 根因"）：
    appellation_map 的构造真源从"拿 characters[].aliases 反查
    character_mentions"改成"_resolve_assets 解析每条角色提及时原地记录
    自己的结论"——旧实现把 aliases 的字面证据门槛（保护跨集别名注册表）
    误当成"这条提及有没有解析出身份"的判据，导致已经真实解析成功、发布进
    asset_manifest.characters 的合成描述性称谓（"穿杂役衫的魁梧大汉"一类）
    从 appellation_map 里静默消失。产出语义变更（appellation_map 实际
    行数），比照 1.4.1/1.6.1/1.8.1-1.8.5/1.9.0/1.10.0/1.11.1 的先例推进
    版本号第三位，不动 schema 位。

    2.0.2（真实回归，48e01ff 当晚上线即复现，ERR-20260826-37cf79，见
    app/production/prep_pack.py 模块 docstring 与 PREP_PACK_VERSION 上方
    2.0.2 大注释）：48e01ff 砍掉 event_chain 时，场景 resolution/discovery
    两支的锚点候选表被收窄成 [canonical_scene_name, name]，丢了唯一一路
    独立于场景名本身的证据（原来是 event_chain[].source_evidence[].quote）
    ——场景名是模型综合出的合成标签时（EP1 真实五例："大青山山顶"等，
    原文只写"这青山顶端"）两路候选结构上必然落空，has_scene_anchor 门禁
    具名拦截。修复：_ModelSceneMention 新增 ``quote``（required str，
    可空），模型对这条场景提及自己申报一条逐字引文；resolution/discovery
    两支候选表恢复成 [canonical_scene_name, name, scene_quote]，alias 分支
    同样恢复传入这条提及的 quote。是 prompt-contract 变更（新增模型必须
    申报的字段）且实际改变部分场景绑定的 anchor_phrase 取值，比照
    1.4.1/1.6.1/1.8.1-1.8.5/1.9.0/1.10.0/1.11.1/2.0.1 的先例推进版本号
    第三位，不动 schema 位（asset_manifest.scenes[] 自身字段集合不变，
    quote 只是 _ModelSceneMention 这一模型响应内部字段，不进入发布产物）。

    2.0.3（真实回归，EP4 离线复现：54 段章节只报出 1 个场景、segment_
    indexes 停在 20 段，21~54 段的另外两个已登记场景（"外宗边缘单人居所"
    "外宗放丹广场"）整段缺席，见 PREP_PACK_VERSION 上方 2.0.3 大注释的完整
    根因排查——分块合并只留第一个 chunk、场景去重吃掉新场景两个怀疑方向
    均已用真实章节文本 + 代码路径核验排除，真正成立的是模型在单次长 chunk
    调用里只完整报出了最先出现的那个场景）：两处改动。a) _extract_chunk
    提示词新增"场景的持续性"一段，明确同一地点的后续编号默认延续、不需要
    每个编号重新出现地点描写——prompt-contract 变更，会实际改变模型对
    scenes 的申报范围；b) coverage_ledger 新增并列账目 scene_coverage
    （scene_delivered/scene_uncovered），不影响既有五账或 assert_prep_
    pack_coverage_complete 门禁，是新增可见性账目而非新的拦截。两者合计
    比照 1.4.1/1.6.1/1.8.1-1.8.5/1.9.0/1.10.0/1.11.1/2.0.1/2.0.2 的先例
    推进版本号第三位，不动 schema 位。

    2.0.4（paratext 判定机制归一，logs/paratext_single_source_plan.md，
    见 PREP_PACK_VERSION 上方 2.0.4 大注释）：本文件此前独立发明的第三套
    paratext 判据（_extract_chunk 每个 chunk 无条件自报 paratext_segments，
    自己的措辞+温度，与世界书用的 app.source_paratext.PARATEXT_RULE 完全
    不同源）退休——coverage_ledger.paratext 改为对 chapters.paratext_json
    持久化偏移（PARATEXT_RULE，惰性计算，谁先问谁替所有人算好）做确定性
    投影，模型不再被问这件事。_ChunkResponse 删除 paratext_segments 字段
    （schema 变小），_discover_new_characters 也改用这份持久化结果，不再
    自己独立发起一次 strip_paratext 模型调用。是 prompt-contract 变更
    （删字段）+ ledger 判定语义变更（判据来源从模型自报换成确定性投影），
    比照 1.4.1/1.9.0 的先例推进版本号第三位，不动 schema 位（coverage_
    ledger.paratext 自身仍是 flat [int] list）。"""
    assert prep_pack.PREP_PACK_VERSION == "2.0.4"


# ---------------------------------------------------------------------------
# 角色维度单独退化的可见性信号（第31轮真实回归 EP7，ep_621d93ac1231）：见
# _generate_prep_pack_once 里 character_manifest_anomaly 那段大注释。真实
# 事故——chunk 抽取第一次调用本已正确报出主角，但那次调用所在的 run 中途
# 被打断；同一 run_id 重新整体起跑后，chunk 抽取的原始 JSON 结构中途缺了
# 一段，本地格式修复 candidate 被 app.harness.model_gateway.
# _latest_json_authority_root 误判成末尾一个只含 scenes 的孤立片段，格式
# 修复调用据此"忠实"地只交回 scenes、把 characters/props 一并修没了——
# scene_mentions 非空使"三项任一非空就放行"的门禁直接放行，角色维度归零
# 这件事从此再没有任何信号能被看见（该集最终发布的 asset_manifest.
# characters == []，唯一主角出场约43次却被漏光）。这里不复现 model_
# gateway 的 JSON 误判本身（那是另一层、更底层的既有缺陷点，见
# ERR-20260824-7ab7cb 的既有说明），只验证 prep_pack.py 这一层新增的可见
# 性信号：_extract_chunk 返回空 characters + 非空 scenes 时，
# character_manifest_anomaly 必须被计算出来、不能是 None——不拦截发布
# （既定方向是必被看见，不是必被拦住）。
# ---------------------------------------------------------------------------

def test_empty_character_mentions_with_nonempty_scene_mentions_is_flagged_visible(
    monkeypatch,
) -> None:
    """红灯改绿灯：EP7 真实事故的最小复现。已登记角色谱非空
    （known_characters=['李四']），chunk 抽取回来的 characters 却是空
    列表，scenes 非空——这正是那道"三项任一非空即放行"门禁天生看不见的
    单维度退化。断言 character_manifest_anomaly 非 None、且里面记录的
    计数跟真实输入一致；同时确认这不是拦截（payload 仍然正常产出，
    characters 确实是空列表——可见不拦停）。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp1','p1','李四',1,NULL)"
    )
    conn.execute(
        "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end) "
        "VALUES ('sr1','p1','演武场',1,NULL)"
    )
    conn.commit()
    # _generate_prep_pack_once 内部不接受 conn 参数，永远读模块级 get_conn()
    # （生产环境下指向 app.db 的任务级/线程级单例连接）——跟本文件其余测试
    # 直接调用 _resolve_assets(conn, ...)（显式传参）不是同一层级。这里换成
    # monkeypatch 模块级 get_conn，让它在测试范围内返回上面搭好的内存夹具，
    # 不触碰真实数据库（pytest 的 sandbox 隔离本身已经保证 app.db 不会碰到
    # 真实 data/manju.db，这里再加一层是为了让 _generate_prep_pack_once
    # 真正读到本测试插入的 character_portraits/scene_references 行）。
    patch_prep_pack_everywhere(monkeypatch, "get_conn", lambda: conn)

    source_text = "老者站在演武场上说话。"

    async def fake_extract_chunk(**_kwargs) -> prep_pack._ChunkResponse:
        return prep_pack._ChunkResponse(
            characters=[],
            scenes=[
                prep_pack._ModelSceneMention(
                    display_name="演武场",
                    suspected_true_name=None,
                    segment_indexes=[1],
                    quote=source_text,
                )
            ],
            props=[],
        )

    patch_prep_pack_everywhere(monkeypatch, "_extract_chunk", fake_extract_chunk)

    (
        payload, _rejected_paratext_claims, _true_name_hints,
        _scene_alias_anchors, _rejected_alias_conflicts, character_manifest_anomaly,
    ) = asyncio.run(prep_pack._generate_prep_pack_once(
        episode_id="ep-test", episode_no=2, project_id="p1",
        chapter_indexes=[], source_text=source_text, run_id=None, attempt_hint="",
    ))

    assert payload["asset_manifest"]["characters"] == [], (
        "复现前提：角色维度确实退化为空（不是这条测试断言的目标，是它的前提）"
    )
    assert payload["asset_manifest"]["scenes"], "场景维度必须仍然非空，门禁才会被放行"
    assert character_manifest_anomaly is not None, (
        "已登记角色谱非空、scenes 非空、characters 却整段为空——"
        "这个可疑信号必须被记录下来，不能悄悄消失"
    )
    assert character_manifest_anomaly["known_character_count"] == 1
    assert character_manifest_anomaly["scene_mention_count"] == 1
    assert character_manifest_anomaly["prop_mention_count"] == 0


def test_empty_character_mentions_without_known_roster_is_not_flagged(monkeypatch) -> None:
    """对照绿灯：这个信号判据必须是"从数据推导"，不是"角色是空的就报警"。
    项目根本没有登记过任何角色（known_characters 为空）时，即便本集
    characters 确实是空列表，也不该被标为异常——空谱本身就说明"没有已知
    角色可比对"，不是可疑的单维度退化。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end) "
        "VALUES ('sr1','p1','演武场',1,NULL)"
    )
    conn.commit()
    patch_prep_pack_everywhere(monkeypatch, "get_conn", lambda: conn)

    source_text = "老者站在演武场上说话。"

    async def fake_extract_chunk(**_kwargs) -> prep_pack._ChunkResponse:
        return prep_pack._ChunkResponse(
            characters=[],
            scenes=[
                prep_pack._ModelSceneMention(
                    display_name="演武场",
                    suspected_true_name=None,
                    segment_indexes=[1],
                    quote=source_text,
                )
            ],
            props=[],
        )

    patch_prep_pack_everywhere(monkeypatch, "_extract_chunk", fake_extract_chunk)

    (
        _payload, _rejected_paratext_claims, _true_name_hints,
        _scene_alias_anchors, _rejected_alias_conflicts, character_manifest_anomaly,
    ) = asyncio.run(prep_pack._generate_prep_pack_once(
        episode_id="ep-test", episode_no=2, project_id="p1",
        chapter_indexes=[], source_text=source_text, run_id=None, attempt_hint="",
    ))

    assert character_manifest_anomaly is None


# ---------------------------------------------------------------------------
# known_characters 提示词参数改逐字命中过滤（不是 RAG/关键词检索——见
# chunking.py._prep_pack_character_shortlist 上方大注释）：项目登记的角色
# 可能有几十个，本集原文通常只出现其中几个，另外那几十个不是中性噪音，是
# 诱导错误归属的噪音。判据与 true_name.py._prep_pack_true_name_verdict_
# candidates 同一口径：已登记角色的全部称谓（name/alias）里，任一个逐字出现
# 在本集原文中，该角色的规范名才入选。
# ---------------------------------------------------------------------------

def test_character_shortlist_is_literal_hit_not_full_registry() -> None:
    """三个已登记角色：'灰袍老者' 靠 name 本身在本集原文逐字命中；'许清' 的
    name 本身没出现，但它的别名'许姑娘'逐字命中——入选名单的必须是规范名
    '许清'本人，不是命中的那个别名；'王有材' 的 name 与全部别名都没在本集
    原文出现，必须被排除在 shortlist 之外。known_names（全量注册表）必须
    原样返回三个名字，不受这道过滤影响——那是喂给 character_manifest_
    anomaly 的数据，跟 shortlist 是两个不同的问题。"""
    conn = _make_conn()
    for row_id, name in (("cp1", "灰袍老者"), ("cp2", "许清"), ("cp3", "王有材")):
        conn.execute(
            "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
            "VALUES (?,'p1',?,1,NULL)",
            (row_id, name),
        )
    conn.commit()
    _seed_bible_characters(conn, "p1", [
        _bible_character("灰袍老者"),
        _bible_character("许清", aliases=[_bible_alias("许姑娘")]),
        _bible_character("王有材", aliases=[_bible_alias("老王")]),
    ])
    source_text = "灰袍老者缓步走入大殿，许姑娘紧随其后。"

    known_names, shortlist = prep_pack._prep_pack_character_shortlist(
        conn, "p1", 2, source_text,
    )

    assert set(known_names) == {"灰袍老者", "许清", "王有材"}, (
        "全量注册表必须原样返回，不受逐字命中过滤影响"
    )
    assert shortlist == ["灰袍老者", "许清"], (
        "灰袍老者靠 name 本身命中；许清靠别名'许姑娘'命中，入选的必须是规范名"
        "'许清'本人，不是命中的那个别名字符串"
    )
    assert "王有材" not in shortlist, "王有材的 name 与全部别名都没在本集原文出现，必须被排除"


def test_character_shortlist_empty_hit_does_not_fall_back_to_full_registry() -> None:
    """空集不许退回全量（硬性纪律）：本集原文一个已登记角色都没提到，
    shortlist 必须是空列表本身，不能因为空而悄悄回退成 known_names。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp1','p1','王有材',1,NULL)"
    )
    conn.commit()
    _seed_bible_characters(conn, "p1", [_bible_character("王有材")])
    source_text = "一段与已登记角色毫无关系的原文。"

    known_names, shortlist = prep_pack._prep_pack_character_shortlist(
        conn, "p1", 2, source_text,
    )

    assert known_names == ["王有材"]
    assert shortlist == [], "空命中必须是空列表本身，不是回退到 known_names"


def test_shortlist_exclusion_does_not_remove_registry_or_true_name_visibility(
    monkeypatch,
) -> None:
    """钉住"四条必须写死"第4条：逐字命中过滤只砍"chunk 抽取时的拼写对齐
    提示"（喂进 _extract_chunk 的 known_characters 参数），不砍身份体系的
    可见性。'王有材' 因为本集原文没提到而被排除出提示词名单，仍必须能
    通过 _resolve_portrait_id（角色卡注册表）与 true_name.py 的全书卷宗
    真名裁决（_prep_pack_true_name_verdict_candidates）正常查到——没有这个
    测试，将来很容易被改成真的从身份体系里过滤掉。"""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp1','p1','灰袍老者',1,NULL)"
    )
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('cp2','p1','王有材',1,NULL)"
    )
    conn.commit()
    _seed_bible_characters(conn, "p1", [
        _bible_character("灰袍老者"), _bible_character("王有材"),
    ])
    patch_prep_pack_everywhere(monkeypatch, "get_conn", lambda: conn)

    source_text = "灰袍老者缓步走入大殿。"
    captured_known_characters: list[list[str]] = []

    async def fake_extract_chunk(*, known_characters, **_kwargs):
        captured_known_characters.append(known_characters)
        return prep_pack._ChunkResponse(
            characters=[
                prep_pack._ModelCharacterMention(
                    display_name="灰袍老者", suspected_true_name=None, segment_indexes=[1],
                )
            ],
            scenes=[], props=[],
        )

    patch_prep_pack_everywhere(monkeypatch, "_extract_chunk", fake_extract_chunk)

    asyncio.run(prep_pack._generate_prep_pack_once(
        episode_id="ep-test", episode_no=2, project_id="p1",
        chapter_indexes=[], source_text=source_text, run_id=None, attempt_hint="",
    ))

    assert captured_known_characters, "_extract_chunk 必须至少被调用过一次"
    for shortlist in captured_known_characters:
        assert shortlist == ["灰袍老者"], (
            "王有材未在本集原文出现，不该进提示词名单——这是本次改动要收紧的部分"
        )

    # 身份体系可见性：王有材没进提示词名单，不代表它从注册表/真名裁决里消失。
    assert prep_pack._resolve_portrait_id(conn, "p1", "王有材", 2) == "cp2", (
        "角色卡注册表（character_portraits）必须仍能正常解析到王有材——"
        "shortlist 只是提示词内容，不是身份注册表本身"
    )
    known_names, _shortlist = prep_pack._prep_pack_character_shortlist(
        conn, "p1", 2, source_text,
    )
    assert "王有材" in known_names, "全量注册表读侧必须仍然看得见王有材"

    roster = prep_pack._prep_pack_true_name_verdict_roster(
        prep_pack._load_project_bible(conn, "p1"), "character",
    )
    dossier = [
        {"entry_index": 1, "chapter_idx": 1, "segment_index": 1, "text": "王有材出现在这一章。"},
    ]
    candidates = prep_pack._prep_pack_true_name_verdict_candidates(dossier, roster, "王有材")
    assert "王有材" in candidates, (
        "全书卷宗真名裁决的候选集完全来自人物谱 + 卷宗文本逐字命中，跟本集 chunk "
        "提示词的 shortlist 无关——王有材必须仍然是合法候选"
    )
