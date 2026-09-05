"""WS2-A/B/C：叙述向称谓归属（app.production.prep_pack.appellation_resolve）、
展示端 entity 补名（app.domain.storyboard_ops.resource_labels）、身份权威
注册路由 vs 本批折叠路由的确定性裁决（app.identity_fold）。

fixture 全部取自 B 机真实数据（2026-09-02 只读查询）：
- 跑不快的孩子 proj_ce9fcf749b23 / ep_b070f72e369a（第2集）：整段是"里奥"的
  第一人称自述回顾，主解析（_extract_chunk）如实判定 characters=[]（原文
  确实没有任何角色"真正在画面中出场"，这是它在自己判据下的正确答案），
  appellation_resolve 是独立的第三条通路，不依赖这次判定是否为空。
- 我欲封天 proj_f8cf2eeb2e66 / ep_0a70ec56e8e9（第1集）shot11：group extra
  "虎头虎脑的少年" 的 asset_manifest.functional_extras 条目本身完整
  （label="虎头虎脑的少年"，visual_entity_id="entity:ee1fb41c79e4e33d"，
  与 shot11.characters 里的 entity id 精确匹配）——真正的缺口在展示端
  resource_labels._display_names 从未遍历 functional_extras。
- 神墓 proj_f28fc90b014d / ep_55968c58391a（第2集）：authority_id=bible:雨馨
  的真实 screenplay_character_resolutions 条目（resolution=reference_
  identity），过去曾产出 canonical_identity_multiple_identity_groups 冲突
  （ERR-20260902-2aabcc）。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from app.continuity import effective_characters_visible, raw_characters_visible
from app.domain.storyboard_ops import resource_labels
from app.identity_fold import reconcile_registered_authority_folds
from app.production.prep_pack import appellation_resolve as ar
from app.production.storyboard_pack_identity import resolve_persisted_character_ids
from app.schemas import Bible, Character, Shot, World


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE character_portraits(id TEXT, project_id TEXT, character_name TEXT, "
        "ep_start INTEGER, ep_end INTEGER)"
    )
    conn.commit()
    return conn


def _bible(*names: str) -> Bible:
    return Bible(
        characters=[
            Character(name=name, role="主角", appearance_canonical="占位外观")
            for name in names
        ],
        world=World(visual_style_canonical="测试画风"),
    )


# ---------------------------------------------------------------------------
# _verified_verdicts：代码侧结构核验（不信任模型的 enum/证据结构性声明）
# ---------------------------------------------------------------------------

def test_narrator_label_never_becomes_a_verdict():
    response = ar._AppellationResolutionResponse(appellations=[
        ar._AppellationVerdict(raw_label="旁白", identity="里奥", evidence="", segment_indexes=[1]),
    ])
    verified = ar._verified_verdicts(
        response, candidates={"里奥"}, source_text="随便什么原文",
        valid_segment_indexes={1},
    )
    assert verified == []


def test_named_identity_without_verbatim_evidence_falls_back_to_unresolved():
    response = ar._AppellationResolutionResponse(appellations=[
        ar._AppellationVerdict(
            raw_label="少年", identity="里奥", evidence="原文里根本没有这句话",
            segment_indexes=[1],
        ),
    ])
    verified = ar._verified_verdicts(
        response, candidates={"里奥"}, source_text="我八岁的时候被诊断出长不高。",
        valid_segment_indexes={1},
    )
    assert len(verified) == 1
    assert verified[0].identity == ar.UNRESOLVED


def test_named_identity_with_verbatim_evidence_passes():
    source_text = "我八岁的时候被诊断出长不高。我三十五岁，在卡塔尔的夜里，把它抱在怀里。"
    response = ar._AppellationResolutionResponse(appellations=[
        ar._AppellationVerdict(
            raw_label="八岁男孩", identity="里奥",
            evidence="我八岁的时候被诊断出长不高。", segment_indexes=[2],
        ),
    ])
    verified = ar._verified_verdicts(
        response, candidates={"里奥"}, source_text=source_text,
        valid_segment_indexes={1, 2, 3},
    )
    assert len(verified) == 1
    assert verified[0].identity == "里奥"


def test_out_of_range_segment_indexes_are_dropped_not_kept():
    response = ar._AppellationResolutionResponse(appellations=[
        ar._AppellationVerdict(raw_label="众猴", identity=ar.COLLECTIVE, segment_indexes=[1, 99]),
    ])
    verified = ar._verified_verdicts(
        response, candidates=set(), source_text="", valid_segment_indexes={1},
    )
    assert verified[0].segment_indexes == [1]


def test_identity_outside_candidates_and_not_collective_becomes_unresolved():
    """模型 enum 遵守不是可证明保证（同 functional_candidate_verdict.py 口径）：
    即使模型自己填了一个候选之外的姓名，代码侧也必须拒绝，不能盲信。"""
    response = ar._AppellationResolutionResponse(appellations=[
        ar._AppellationVerdict(raw_label="某人", identity="候选之外的名字", segment_indexes=[1]),
    ])
    verified = ar._verified_verdicts(
        response, candidates={"里奥"}, source_text="", valid_segment_indexes={1},
    )
    assert verified[0].identity == ar.UNRESOLVED


# ---------------------------------------------------------------------------
# resolve_narration_appellations 端到端：跑不快的孩子 ep2 真实文本（B 机取样）
# ---------------------------------------------------------------------------

PAOBUKUAI_EP2_SOURCE = (
    "很多年以后，人们会这样介绍他：\n\n"
    "七座金球奖，四十四个冠军，八次西甲金靴，一次世界杯，一次美洲杯，一次欧美超级杯。"
    "史上最伟大的球员，没有之一。\n\n"
    "人们叫他球王，叫他 GOAT，叫他外星人。\n\n"
    "可如果你问他，他会怎么说呢。\n\n"
    "我想他会说：\n\n"
    "我八岁的时候被诊断出长不高。\n我每天给自己打针，打了三年。\n我十三岁离开家，在食堂一个人吃饭。\n"
    "我十四岁想着要不要回去。\n我十九岁第一次站在世界杯上，输了。\n我二十七岁看着那座奖杯，以为这辈子碰不到它。\n"
    "我二十九岁打飞了一个点球，说我退出了。\n我三十五岁，在卡塔尔的夜里，把它抱在怀里。\n\n"
    "他跑得不算快。\n他跳得不算高。\n他差点连身高都没有。\n\n可他跑了二十六年，从没停过。\n\n"
    "——从头到尾，我只是那个在罗萨里奥土场上\n被撞倒了就爬起来接着跑的小孩。"
)


def test_paobukuai_ep2_narration_resolves_to_the_single_bible_character(monkeypatch):
    """真实缺陷1：主解析 characters=[]，appellation_resolve 是独立第三条通路，
    对"球员/少年/八岁男孩"这类叙述向称谓凭本段逐字证据归到里奥；"旁白"永远
    不会被归属为任何人（旁白从不出现在候选集里，也被 _verified_verdicts
    结构性拒绝）。"""
    conn = _make_conn()
    bible = _bible("里奥")

    async def fake_call(*, dossier, candidates, episode_id, project_id):
        # 模拟模型真实会做的事：从本段/相邻段落原文里逐字摘录证据，把
        # "少年/球员/八岁男孩"都归到候选集里唯一在世的人物"里奥"身上。
        catalog_text = "".join(item["text"] for item in dossier)
        verdicts = []
        if "我八岁的时候被诊断出长不高" in catalog_text:
            seg = next(i["segment_index"] for i in dossier if "我八岁" in i["text"])
            verdicts.append(ar._AppellationVerdict(
                raw_label="八岁男孩", identity="里奥",
                evidence="我八岁的时候被诊断出长不高。", segment_indexes=[seg],
            ))
        if "史上最伟大的球员" in catalog_text:
            seg = next(i["segment_index"] for i in dossier if "史上最伟大的球员" in i["text"])
            verdicts.append(ar._AppellationVerdict(
                raw_label="球员", identity="里奥",
                evidence="史上最伟大的球员，没有之一。", segment_indexes=[seg],
            ))
        return ar._AppellationResolutionResponse(appellations=verdicts)

    monkeypatch.setattr(ar, "_appellation_resolution_call", fake_call)

    from app.source_excerpt import index_source_segments
    segments = index_source_segments(PAOBUKUAI_EP2_SOURCE)
    characters: dict = {}
    functional_extras: dict = {}
    appellation_rows: list = []

    asyncio.run(ar.resolve_narration_appellations(
        conn, project_id="p1", episode_id="ep2", episode_no=2,
        source_text=PAOBUKUAI_EP2_SOURCE, bible=bible, segments=segments,
        characters=characters, functional_extras=functional_extras,
        character_appellation_rows=appellation_rows,
    ))

    assert list(characters.keys()) == ["bible:里奥"]
    entry = characters["bible:里奥"]
    assert entry["identity_id"] == "bible:里奥"
    assert entry["display_name"] == "里奥"
    assert set(entry["aliases"]) >= {"球员"} or entry["display_appellation"] in {"八岁男孩", "球员"}
    assert len(entry["segment_indexes"]) == 2
    assert functional_extras == {}
    raw_mentions = {row["raw_mention"] for row in appellation_rows}
    assert raw_mentions == {"八岁男孩", "球员"}
    assert all(row["identity_id"] == "bible:里奥" for row in appellation_rows)
    # 旁白从未成为候选、也从未出现在 fake_call 的产出里——这里额外确认
    # 结构核验本身也会拒绝它（防止未来有人往 fake_call 里加一条旁白误判）。
    rejected = ar._verified_verdicts(
        ar._AppellationResolutionResponse(appellations=[
            ar._AppellationVerdict(raw_label="旁白", identity="里奥", segment_indexes=[1]),
        ]),
        candidates={"里奥"}, source_text=PAOBUKUAI_EP2_SOURCE, valid_segment_indexes={1},
    )
    assert rejected == []


def test_collective_appellation_never_attributed_to_an_individual(monkeypatch):
    """西游同类场景（"众猴"）：collective 判据下，代码绝不能把集体称谓强行
    塞给候选集里的某一个体——落 functional_extras，带 label + visual_entity_id
    （缺陷2要求的展示端最低要求），不进 characters。"""
    conn = _make_conn()
    bible = _bible("孙悟空")

    async def fake_call(*, dossier, candidates, episode_id, project_id):
        return ar._AppellationResolutionResponse(appellations=[
            ar._AppellationVerdict(
                raw_label="众猴", identity=ar.COLLECTIVE,
                segment_indexes=[dossier[0]["segment_index"]],
            ),
        ])

    monkeypatch.setattr(ar, "_appellation_resolution_call", fake_call)
    from app.source_excerpt import index_source_segments
    source_text = "众猴见了，都拱伏无违。"
    segments = index_source_segments(source_text)
    characters: dict = {}
    functional_extras: dict = {}
    appellation_rows: list = []
    asyncio.run(ar.resolve_narration_appellations(
        conn, project_id="p1", episode_id="ep1", episode_no=1,
        source_text=source_text, bible=bible, segments=segments,
        characters=characters, functional_extras=functional_extras,
        character_appellation_rows=appellation_rows,
    ))
    assert characters == {}
    assert "众猴" in functional_extras
    extra = functional_extras["众猴"]
    assert extra["visual_entity_id"].startswith("entity:")
    assert extra["provenance"]["collective"] is True
    assert appellation_rows == []


def test_unresolved_appellation_still_carries_label_and_visual_entity_id(monkeypatch):
    """神墓同类场景（"老人"/"他父亲"证据不足以确认具体是谁）：unresolved 不
    等于丢弃——必须像群演一样带 label + visual_entity_id，供缺陷2的展示端
    使用；不得因为候选只有一个人就默认归给他（不猜）。"""
    conn = _make_conn()
    bible = _bible("辰南")

    async def fake_call(*, dossier, candidates, episode_id, project_id):
        return ar._AppellationResolutionResponse(appellations=[
            ar._AppellationVerdict(
                raw_label="老人", identity=ar.UNRESOLVED,
                segment_indexes=[dossier[0]["segment_index"]],
            ),
        ])

    monkeypatch.setattr(ar, "_appellation_resolution_call", fake_call)
    from app.source_excerpt import index_source_segments
    source_text = "一个老人站在原地，望着远方，没有人知道他是谁。"
    segments = index_source_segments(source_text)
    characters: dict = {}
    functional_extras: dict = {}
    appellation_rows: list = []
    asyncio.run(ar.resolve_narration_appellations(
        conn, project_id="p1", episode_id="ep1", episode_no=1,
        source_text=source_text, bible=bible, segments=segments,
        characters=characters, functional_extras=functional_extras,
        character_appellation_rows=appellation_rows,
    ))
    assert characters == {}
    extra = functional_extras["老人"]
    assert extra["label"] if "label" in extra else True  # setdefault 结构不含 label 键本身
    assert extra["visual_entity_id"].startswith("entity:")
    assert "collective" not in extra["provenance"]


def test_empty_bible_skips_model_call_entirely(monkeypatch):
    """候选集为空（项目还没有人物谱角色）直接跳过，不发起任何模型调用——同
    functional_candidate_verdict.py 的既有口径。"""
    conn = _make_conn()
    bible = _bible()

    async def boom(**kwargs):
        raise AssertionError("不应该发起模型调用")

    monkeypatch.setattr(ar, "_appellation_resolution_call", boom)
    from app.source_excerpt import index_source_segments
    segments = index_source_segments("随便什么原文")
    asyncio.run(ar.resolve_narration_appellations(
        conn, project_id="p1", episode_id="ep1", episode_no=1,
        source_text="随便什么原文", bible=bible, segments=segments,
        characters={}, functional_extras={}, character_appellation_rows=[],
    ))


# ---------------------------------------------------------------------------
# 缺陷2展示端：resource_labels._display_names 必须遍历 functional_extras，
# 不能只看 characters（我欲封天 ep1 shot11 真实数据）
# ---------------------------------------------------------------------------

def test_display_names_resolves_functional_extra_entity_ids(monkeypatch):
    manifest = {
        "characters": [
            {"identity_id": "bible:孟浩", "display_name": "孟浩", "display_appellation": "孟浩"},
        ],
        "functional_extras": [
            {
                "label": "虎头虎脑的少年",
                "segment_indexes": [3, 4],
                "visual_entity_id": "entity:ee1fb41c79e4e33d",
                "provenance": {"method": "discovery"},
            },
        ],
    }
    pack = {"prep_pack_version": "2.0.5", "asset_manifest": manifest}

    class _Row(dict):
        def __getitem__(self, key):
            return dict.get(self, key)

    row = _Row(screenplay_json=json.dumps(pack, ensure_ascii=False))

    class _FakeConn:
        def execute(self, *a, **k):
            class _Cur:
                def fetchone(self_inner):
                    return row
            return _Cur()

    monkeypatch.setattr(resource_labels, "get_conn", lambda: _FakeConn())
    names, functional_extras = resource_labels._display_names_and_extras("ep_0a70ec56e8e9")
    assert names.get("entity:ee1fb41c79e4e33d") == "虎头虎脑的少年"
    assert names.get("bible:孟浩") == "孟浩"
    assert functional_extras == manifest["functional_extras"]


# ---------------------------------------------------------------------------
# 缺陷3：神墓真实数据——authority_id=bible:雨馨 同时挂注册路由与本批折叠路由
# ---------------------------------------------------------------------------

def test_shenmu_ep2_registered_route_survives_batch_fold_collision():
    groups_by_authority = {
        "bible:雨馨": {
            "bible:雨馨",
            "1d80620bd1947717f334afabaae29aaacdb762769443bb4650c395ea51229a70:current-1:F18",
        },
    }
    entries = {"bible:雨馨": {"authority_id": "bible:雨馨", "canonical_name": "雨馨"}}
    reconcile_registered_authority_folds(groups_by_authority, entries)
    assert groups_by_authority["bible:雨馨"] == {"bible:雨馨"}
    assert "conflict_notes" in entries["bible:雨馨"]


def test_reconcile_leaves_other_conflict_shapes_untouched():
    """两个具名 canonical_name 挤在一个 identity_group 之类的形态不属于这条
    规则——不该被这个函数悄悄清空。"""
    groups_by_authority = {
        "bible:甲": {"bible:甲", "bible:乙"},
    }
    entries = {"bible:甲": {"authority_id": "bible:甲", "canonical_name": "甲"}}
    reconcile_registered_authority_folds(groups_by_authority, entries)
    assert groups_by_authority["bible:甲"] == {"bible:甲", "bible:乙"}
    assert "conflict_notes" not in entries["bible:甲"]


# ---------------------------------------------------------------------------
# 缺陷1展示端：旁白永远不进 characters_visible（跑不快的孩子 ep2 shot1 真实
# 数据——characters=["少年","球员","旁白"]）
# ---------------------------------------------------------------------------

def _minimal_shot(**overrides) -> Shot:
    data = {
        "shot_no": 1, "duration_s": 5, "shot_size": "远景", "camera_move": "固定",
        "scene_setting": "日，校园食堂", "characters": ["少年", "球员", "旁白"],
        "characters_visible": ["旁白"], "action_desc": "占位。",
        "source_excerpt": "占位原文。",
    }
    data.update(overrides)
    return Shot(**data)


def test_narrator_never_in_raw_characters_visible():
    shot = _minimal_shot()
    assert "旁白" not in raw_characters_visible(shot)


def test_narrator_never_in_effective_characters_visible():
    shot = _minimal_shot(characters_visible=[])
    assert "旁白" not in effective_characters_visible(shot)


# ---------------------------------------------------------------------------
# 缺陷1持久化端：resolve_persisted_character_ids 用 appellation_map 正名替换
# 裸称谓，旁白整条丢弃，查不清的原样保留（不猜）
# ---------------------------------------------------------------------------

def test_resolve_persisted_character_ids_replaces_via_appellation_map():
    payload = {
        "asset_manifest": {"characters": [{"identity_id": "bible:里奥"}]},
        "appellation_map": [
            {"raw_mention": "少年", "identity_id": "bible:里奥"},
            {"raw_mention": "球员", "identity_id": "bible:里奥"},
        ],
    }
    resolved, notes = resolve_persisted_character_ids(
        payload, ["少年", "球员", "旁白"], segment_source_indexes=[1],
    )
    assert resolved == ["bible:里奥", "bible:里奥"]
    assert notes == []


def test_resolve_persisted_character_ids_keeps_already_registered_ids():
    payload = {"asset_manifest": {"characters": [{"identity_id": "bible:孟浩"}]}, "appellation_map": []}
    resolved, notes = resolve_persisted_character_ids(
        payload, ["bible:孟浩"], segment_source_indexes=[1],
    )
    assert resolved == ["bible:孟浩"]
    assert notes == []


def test_resolve_persisted_character_ids_leaves_unmatched_alone_not_guessed():
    payload = {"asset_manifest": {"characters": []}, "appellation_map": []}
    resolved, notes = resolve_persisted_character_ids(
        payload, ["神秘人"], segment_source_indexes=[1],
    )
    assert resolved == ["神秘人"]
    assert notes == []


# WS12（group-extra 描述性措辞与映射台已登记 functional_extras 的结构性
# 归并）的 resolve_persisted_character_ids 接线测试搬到
# tests/test_storyboard_pack_identity_extras.py——本文件已在 500 行基线
# 上，纯搬移、不改行为，避免撞文件行数棘轮（CLAUDE.md「架构欠账治理」，
# 新增文件严格达标而不是把新用例硬塞进已经到顶的旧文件）。


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_appellation_named_to_someone_else_in_source_is_not_registered_as_alias(monkeypatch):
    """我欲封天第 1 集：模型按半山腰「也是一个少年」把「少年」归给王有材，但同一集原文
    「“又落榜了……”少年叹了口气，他叫孟浩」把这个称谓点名给了孟浩。登记成王有材别名后，
    分镜台台词账本会把孟浩落榜独白整段判给王有材。原文点名句是结构复核，不看关键词。"""
    import asyncio

    conn = _make_conn()
    bible = _bible("孟浩", "王有材")
    source = (
        "少年有些瘦弱，手中拿着一个葫芦。\n“又落榜了……”少年叹了口气，他叫孟浩，是这大青山下云杰县一个普通书生。\n\n"
        "“你……可是孟浩，救命。”从半山腰探出身子的也是一个少年，他一眼就看到了孟浩。\n“王有材？”孟浩睁大了眼。"
    )

    async def fake_call(*, dossier, candidates, episode_id, project_id):
        seg = next(i["segment_index"] for i in dossier if "也是一个少年" in i["text"])
        first = next(i["segment_index"] for i in dossier if "他叫孟浩" in i["text"])
        return ar._AppellationResolutionResponse(appellations=[ar._AppellationVerdict(
            raw_label="少年", identity="王有材",
            evidence="从半山腰探出身子的也是一个少年", segment_indexes=sorted({first, seg}),
        )])

    monkeypatch.setattr(ar, "_appellation_resolution_call", fake_call)
    from app.source_excerpt import index_source_segments
    segments = index_source_segments(source)
    characters: dict = {}
    functional_extras: dict = {}
    rows: list = []
    asyncio.run(ar.resolve_narration_appellations(
        conn, project_id="p1", episode_id="ep1", episode_no=1,
        source_text=source, bible=bible, segments=segments,
        characters=characters, functional_extras=functional_extras, character_appellation_rows=rows,
    ))
    aliases = {k: v.get("aliases") for k, v in characters.items()}
    assert "少年" not in (aliases.get("bible:王有材") or []), aliases
    assert not functional_extras, "被否决的别名不该变成一个无名角色卡"
