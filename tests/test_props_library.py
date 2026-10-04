"""app.props（世界书物件库）：表自建幂等、区间查询、关键道具判据、反应式登记、
API 列表/重生成。

用户投诉根因：相邻分集视频里同一件道具形态漂移（猫包一会儿网状一会儿透明）——
道具此前只有映射台抽出的 label+description 文字描述，没有素材库锚定。本文件钉住
该判据与落库流程；模型与出图调用全部 monkeypatch（不发真实网络请求，遵循
tests/test_prep_pack_asset_discovery.py 顶部同一条边界说明：只打桩外部协作者，
不重新测试模型契约本身）。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.db import get_conn, now
from app.domain.bible_ops import props_api
from app.props import judge, service, store
from app.schemas import Bible


def _seed_project(project_id: str, *, props_list: list[dict] | None = None, style: str = "国漫电影风") -> None:
    bible = {
        "characters": [], "scenes": [], "props": props_list or [],
        "world": {"era": "", "genre": "", "visual_style_canonical": style},
    }
    conn = get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, bible_json, bible_version, created_at) VALUES(?,?,?,0,?)",
        (project_id, "测试项目", json.dumps(bible, ensure_ascii=False), now()),
    )
    conn.commit()


async def _fake_chat_structured(_messages, **_kwargs):
    return SimpleNamespace(appearance_canonical="灰色帆布材质、边角磨损、铜扣锁头", aliases=["旧包"])


async def _explode_chat_structured(*_a, **_k):
    raise AssertionError("结构判据没过就不该发起模型调用")


async def _fake_generate_image(project_id, name, _prompt):
    return f"/fake/{project_id}/{name}.png"


async def _failing_generate_image(project_id, name, _prompt):
    return None


# ---------------------------------------------------------------------------
# store.py：表自建幂等 + 区间查询 + 覆盖式登记
# ---------------------------------------------------------------------------

def test_ensure_schema_idempotent() -> None:
    store.ensure_schema()
    store.ensure_schema()
    get_conn().execute("SELECT 1 FROM prop_references LIMIT 1")


def test_prop_reference_for_episode_interval() -> None:
    conn = get_conn()
    store.upsert_prop_reference(
        conn, "p1", "旧猫包", 3, appearance="灰色帆布，边角磨损", image_path="a.png",
        prompt="p", status="ready", qa={},
    )
    conn.commit()
    # 登记集之前的集回退到最早那张（2026-09-05：并行跑集时后面的集先登记，前面的集不该显示占位）。
    assert store.prop_reference_for_episode(conn, "p1", "旧猫包", 2)["image_path"] == "a.png"
    row = store.prop_reference_for_episode(conn, "p1", "旧猫包", 3)
    assert row["image_path"] == "a.png"
    # ep_end=NULL 是开区间，覆盖到当前最新版——与 scene_row_for_episode 同一语义。
    later = store.prop_reference_for_episode(conn, "p1", "旧猫包", 99)
    assert later["image_path"] == "a.png"


def test_upsert_prop_reference_overwrites_previous_segment() -> None:
    conn = get_conn()
    store.upsert_prop_reference(
        conn, "p1", "旧猫包", 1, appearance="a", image_path="1.png", prompt="p", status="ready", qa={},
    )
    store.upsert_prop_reference(
        conn, "p1", "旧猫包", 5, appearance="a2", image_path="2.png", prompt="p", status="ready", qa={},
    )
    conn.commit()
    rows = conn.execute(
        "SELECT * FROM prop_references WHERE project_id='p1' AND prop_name='旧猫包'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["ep_start"] == 5
    assert rows[0]["image_path"] == "2.png"


# ---------------------------------------------------------------------------
# judge.py：结构判据（不用道具名/关键词黑白名单）
# ---------------------------------------------------------------------------

def test_is_key_prop_mention_segment_count_gate() -> None:
    assert judge.is_key_prop_mention({"segment_indexes": [3, 7], "description": "一个杯子"}) is True
    assert judge.is_key_prop_mention({"segment_indexes": [3], "description": "一个杯子"}) is False


def test_is_key_prop_mention_description_clause_gate() -> None:
    rich = {"segment_indexes": [5], "description": "灰色帆布材质、边角磨损、铜扣锁头"}
    thin = {"segment_indexes": [5], "description": "一个杯子"}
    assert judge.is_key_prop_mention(rich) is True
    assert judge.is_key_prop_mention(thin) is False


async def test_assess_prop_appearance_prompt_forbids_other_objects_bleeding_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """用户投诉根因：《顾念长安》"浅灰色卫衣"的 appearance_canonical 把贴身佩戴的
    另一件道具（星盘）压在卫衣上的印痕写进了卫衣外观，出图据此在卫衣胸前画出一个
    不该有的星盘图案——星盘另有自己的道具卡。提示词必须正面声明"只写这件道具自身
    可见的外观，其它物件与动作痕迹各归其它物件/剧情"，锁住这条规则不被回退。"""
    captured: dict[str, str] = {}

    async def _capture_chat_structured(messages, **_kwargs):
        captured["prompt"] = messages[0]["content"]
        return SimpleNamespace(appearance_canonical="浅灰色卫衣、棉质、圆领、无印花", aliases=[])

    monkeypatch.setattr(judge.model_gateway, "chat_structured", _capture_chat_structured)
    await judge.assess_prop_appearance(
        "浅灰色卫衣", "隔着卫衣按住胸前那枚星盘", style="国漫电影风", ep_label="EP01",
    )
    prompt = captured["prompt"]
    assert "藏在它里面的" in prompt and "被它包住或遮住的东西" in prompt
    assert "它自己有自己的道具卡或分镜画面去表现" in prompt
    assert "appearance_canonical" in prompt.split("不写进")[-1]
    # 2026-10-03-v3：能指认出具体来源的痕迹，不论长期还是一次性都优先按「别的
    # 物件/动作痕迹」处理，不被"搬不走"这个对痕迹本身无效的测试带偏（真实案例
    # 回归：长期贴身佩戴压出的星盘印子曾被判成固有外观）。
    assert "判断标准不是这处痕迹本身能不能被单独搬走" in prompt
    assert "时长不改变这个判定" in prompt


async def test_assess_prop_appearance_prompt_rejects_category_only_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实缺陷（proj_ca86b15ab7d7《顾念长安》EP1）：模型给"对面木椅"报的别名里
    混着只剩品类名的"椅子"，登记后以后任何一集写"椅子"都会错误复用这张咖啡馆
    木椅的参考图。提示词必须正面声明"只剩品类名的泛称不算别名"，锁住这条规则。"""
    captured: dict[str, str] = {}

    async def _capture_chat_structured(messages, **_kwargs):
        captured["prompt"] = messages[0]["content"]
        return SimpleNamespace(appearance_canonical="深色木质，靠背雕花，扶手处有裂纹", aliases=[])

    monkeypatch.setattr(judge.model_gateway, "chat_structured", _capture_chat_structured)
    await judge.assess_prop_appearance(
        "对面木椅", "深色木质，靠背雕花", style="国漫电影风", ep_label="EP01",
    )
    prompt = captured["prompt"]
    assert "椅子" in prompt and "杯子" in prompt  # 示例词命中新规则
    assert "只剩下品类名的泛称" in prompt
    assert "本身不是别名" in prompt


# ---------------------------------------------------------------------------
# service.register_prop_card_for_label：分镜阶段补卡的别名收紧（allowed_aliases）
# ---------------------------------------------------------------------------

async def test_register_prop_card_for_label_keeps_alias_used_as_label_in_episode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_project("p_alias_keep")
    monkeypatch.setattr(judge.model_gateway, "chat_structured", _fake_chat_structured)
    monkeypatch.setattr(service, "generate_prop_reference_image", _fake_generate_image)

    result = await service.register_prop_card_for_label(
        get_conn(), "p_alias_keep", 1, "旧猫包", "灰色帆布材质的旧背包",
        style="国漫电影风", ep_label="第 1 集", allowed_aliases=frozenset({"旧包"}),
    )

    assert result["name"] == "旧猫包"
    bible = json.loads(get_conn().execute(
        "SELECT bible_json FROM projects WHERE id='p_alias_keep'",
    ).fetchone()["bible_json"])
    prop = next(p for p in bible["props"] if p["name"] == "旧猫包")
    assert prop["aliases"] == ["旧包"]  # _fake_chat_structured 的别名落在 allowed_aliases 里，保留


async def test_register_prop_card_for_label_drops_alias_not_used_as_label(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    _seed_project("p_alias_drop")
    monkeypatch.setattr(judge.model_gateway, "chat_structured", _fake_chat_structured)
    monkeypatch.setattr(service, "generate_prop_reference_image", _fake_generate_image)

    with caplog.at_level("INFO"):
        result = await service.register_prop_card_for_label(
            get_conn(), "p_alias_drop", 1, "旧猫包", "灰色帆布材质的旧背包",
            style="国漫电影风", ep_label="第 1 集", allowed_aliases=frozenset(),
        )

    assert result["name"] == "旧猫包"
    bible = json.loads(get_conn().execute(
        "SELECT bible_json FROM projects WHERE id='p_alias_drop'",
    ).fetchone()["bible_json"])
    prop = next(p for p in bible["props"] if p["name"] == "旧猫包")
    assert prop["aliases"] == []  # "旧包" 不在空的 allowed_aliases 里，丢弃
    assert any("[PROP_STORYBOARD_CARD_ALIAS_DROPPED]" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# schemas：旧数据无 props 字段仍可加载
# ---------------------------------------------------------------------------

def test_bible_loads_without_props_field() -> None:
    bible = Bible.model_validate({"characters": [], "world": {"visual_style_canonical": "x"}})
    assert bible.props == []


# ---------------------------------------------------------------------------
# service.ensure_props_for_labels：入库判据 + 落库 + 出图
# ---------------------------------------------------------------------------

async def test_ensure_props_for_labels_registers_key_prop(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_project("p1")
    monkeypatch.setattr(judge.model_gateway, "chat_structured", _fake_chat_structured)
    monkeypatch.setattr(service, "generate_prop_reference_image", _fake_generate_image)
    mentions = [{"label": "旧猫包", "description": "旧猫包", "segment_indexes": [2, 9]}]

    result = await service.ensure_props_for_labels("p1", 3, mentions, cards_with_prior_evidence=frozenset())

    assert result["errors"] == []
    assert [item["name"] for item in result["added"]] == ["旧猫包"]
    conn = get_conn()
    bible = json.loads(conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"])
    assert bible["props"][0]["name"] == "旧猫包"
    assert bible["props"][0]["appearance_canonical"].startswith("灰色帆布")
    assert bible["props"][0]["ref_image_path"] == "/fake/p1/旧猫包.png"
    row = store.prop_reference_for_episode(conn, "p1", "旧猫包", 3)
    assert row["status"] == "ready"


async def test_ensure_props_for_labels_skips_background_object(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_project("p1")
    monkeypatch.setattr(judge.model_gateway, "chat_structured", _explode_chat_structured)
    mentions = [{"label": "路人手中的杯子", "description": "一只杯子", "segment_indexes": [4]}]

    result = await service.ensure_props_for_labels("p1", 1, mentions, cards_with_prior_evidence=frozenset())

    assert result == {"added": [], "errors": []}


async def test_ensure_props_for_labels_skips_already_known(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_project("p1", props_list=[{
        "name": "旧猫包", "appearance_canonical": "已登记的锚点", "aliases": ["旧包"],
    }])
    monkeypatch.setattr(judge.model_gateway, "chat_structured", _explode_chat_structured)
    mentions = [
        {"label": "旧猫包", "description": "旧猫包", "segment_indexes": [2, 9]},
        {"label": "旧包", "description": "旧包", "segment_indexes": [2, 9]},
    ]

    result = await service.ensure_props_for_labels("p1", 1, mentions, cards_with_prior_evidence=frozenset())

    assert result == {"added": [], "errors": []}


async def test_ensure_props_for_labels_records_failed_image(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_project("p1")
    monkeypatch.setattr(judge.model_gateway, "chat_structured", _fake_chat_structured)
    monkeypatch.setattr(service, "generate_prop_reference_image", _failing_generate_image)
    mentions = [{"label": "旧猫包", "description": "旧猫包", "segment_indexes": [2, 9]}]

    result = await service.ensure_props_for_labels("p1", 1, mentions, cards_with_prior_evidence=frozenset())

    assert [item["has_image"] for item in result["added"]] == [False]
    row = store.prop_reference_for_episode(get_conn(), "p1", "旧猫包", 1)
    assert row["status"] == "failed"
    assert row["image_path"] is None


async def test_ensure_props_for_labels_no_bible_is_advisory_noop() -> None:
    result = await service.ensure_props_for_labels("nope", 1, [
        {"label": "旧猫包", "description": "旧猫包", "segment_indexes": [1, 2]},
    ], cards_with_prior_evidence=frozenset())
    assert result == {"added": [], "errors": []}


async def test_ensure_props_for_labels_logs_registry_summary(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """``[PREP_PACK_PROP_REGISTRY_SUMMARY]`` 固定前缀集计日志（2026-10-01，
    用户反馈"分镜台道具大多没图"调查新增可观测性）：候选数、新建卡数、归到
    既有卡数、未过判定跳过数都要如实反映，不设上限、不拦截任何候选。四条
    mention 分别落四个不同分支：已登记（跳过）、未过结构判据（跳过）、
    归一后绑定既有卡「野鸡」（归到既有卡）、全新道具「黄铜星盘」（新建卡）。"""
    _seed_project("p1", props_list=[
        {"name": "旧猫包", "appearance_canonical": "已登记的锚点", "aliases": []},
        {"name": "野鸡", "appearance_canonical": "已登记的野鸡锚点", "aliases": []},
    ])
    monkeypatch.setattr(judge.model_gateway, "chat_structured", _fake_chat_structured)
    monkeypatch.setattr(service, "generate_prop_reference_image", _fake_generate_image)
    mentions = [
        {"label": "旧猫包", "description": "旧猫包", "segment_indexes": [2, 9]},
        {"label": "路人手中的杯子", "description": "一只杯子", "segment_indexes": [4]},
        {"label": "两只野鸡", "description": "两只野鸡", "segment_indexes": [2, 9]},
        {"label": "黄铜星盘", "description": "一只旧星盘", "segment_indexes": [1, 2]},
    ]

    with caplog.at_level("INFO"):
        result = await service.ensure_props_for_labels("p1", 7, mentions, cards_with_prior_evidence=frozenset())

    assert result["errors"] == []
    assert [item["name"] for item in result["added"]] == ["黄铜星盘"]
    summary = [r.message for r in caplog.records if "[PREP_PACK_PROP_REGISTRY_SUMMARY]" in r.message]
    assert len(summary) == 1, "每次调用只应该落一条集计日志"
    assert "候选=4" in summary[0]
    assert "新建卡=1" in summary[0]
    assert "归到既有卡=1" in summary[0]
    assert "未过判定跳过=2" in summary[0]


# ---------------------------------------------------------------------------
# API：列表 + 重生成
# ---------------------------------------------------------------------------

async def test_props_for_project_merges_bible_and_reference_status() -> None:
    _seed_project("p1", props_list=[{
        "name": "旧猫包", "appearance_canonical": "灰色帆布", "aliases": [],
        "ref_image_path": "a.png",
    }])
    conn = get_conn()
    store.upsert_prop_reference(
        conn, "p1", "旧猫包", 1, appearance="灰色帆布", image_path="a.png",
        prompt="p", status="ready", qa={},
    )
    conn.commit()

    items = service.props_for_project(conn, "p1")

    assert items == [{
        "name": "旧猫包", "appearance": "灰色帆布", "aliases": [],
        "image_path": "a.png", "status": "ready",
    }]


async def test_list_props_route(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_project("p1", props_list=[{
        "name": "旧猫包", "appearance_canonical": "灰色帆布", "aliases": [],
    }])

    response = await props_api.list_props("p1")

    assert response["project_id"] == "p1"
    assert response["items"][0]["name"] == "旧猫包"
    assert "image_url" in response["items"][0]  # 前端物件库页直接用它，不自己拼 /media 路径


async def test_regenerate_prop_route_missing_prop_returns_409() -> None:
    _seed_project("p1")
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        await props_api.regenerate_prop("p1", "不存在的道具")
    assert excinfo.value.status_code == 409


async def test_regenerate_prop_route_regenerates_image(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_project("p1", props_list=[{
        "name": "旧猫包", "appearance_canonical": "灰色帆布材质", "aliases": [], "first_episode_no": 2,
    }])
    monkeypatch.setattr(service, "generate_prop_reference_image", _fake_generate_image)

    response = await props_api.regenerate_prop("p1", "旧猫包")

    assert response == {
        "name": "旧猫包", "status": "ready", "image_path": "/fake/p1/旧猫包.png", "image_url": None,
    }  # 假路径不在盘上 → image_url 为 None；真实出图后由 _media_url 给出带票据的 /media URL
    row = store.prop_reference_for_episode(get_conn(), "p1", "旧猫包", 2)
    assert row["status"] == "ready"


def test_key_prop_when_head_noun_repeats_in_source_text():
    """EP1 真实数据：「旧猫包」只占一个原文段、描述一句，但原文里「猫包」出现 5 次。"""
    from app.props.judge import is_key_prop_mention, source_occurrences

    source = "李麦麦翻出一个旧猫包，拉开拉链。腿上的猫包被她死死按住。猫包里传出猫叫。橘座顶开猫包拉链。猫包露出一点头。"
    mention = {"label": "旧猫包", "description": "李麦麦用来装橘座的老旧背包", "segment_indexes": [3]}
    assert source_occurrences("旧猫包", source) == 5
    assert is_key_prop_mention(mention, source_text=source) is True
    assert is_key_prop_mention({"label": "泡面碗", "description": "桌上的泡面碗", "segment_indexes": [3]}, source_text=source) is False
    assert is_key_prop_mention(mention) is False


# ---------------------------------------------------------------------------
# 判据 d)：模型提名 + 代码核验（真实缺陷：《顾念长安（第二版）》proj_ca86b15ab7d7
# EP1 贴身佩戴的黄铜旧星盘只出现一次、描述也只有一句，前三条结构信号覆盖不到）
# ---------------------------------------------------------------------------


def test_key_prop_when_plot_significant_and_quote_verified():
    """星盘只出现一次、描述单薄（不满足 segment_count/clause/occurrence 任一条），
    但模型申报的证据逐字命中原文，代码核验通过后仍应判定为关键道具。"""
    from app.props.judge import is_key_prop_mention

    source = "顾屿快一步把照片收回内袋，指尖顺势碰了碰贴身挂着的一枚硬物——是一枚黄铜色的旧星盘，用皮质表袋裹着。"
    mention = {
        "label": "黄铜旧星盘", "description": "贴身佩戴的旧物件", "segment_indexes": [5],
        "plot_significant": True,
        "plot_significant_quote": "指尖顺势碰了碰贴身挂着的一枚硬物——是一枚黄铜色的旧星盘",
    }
    assert is_key_prop_mention(mention, source_text=source) is True


def test_key_prop_plot_significant_without_verified_quote_is_rejected():
    """红灯（手写一份修复前逻辑的临时副本）：如果代码只信模型的 plot_significant
    自报、不核验 quote 是否真的出现在原文里，编造证据也会被判定为关键道具——
    这正是"模型说重要就信"要避免的漏洞，必须先复现再证明修复关上了它。"""
    from app.props.judge import is_key_prop_mention

    source = "顾屿把大衣脱下来换成一件浅灰色卫衣，随手搭在沙发扶手上。"
    fabricated = {
        "label": "凭空捏造的圣物", "description": "一件从未在原文出现过的法器",
        "segment_indexes": [5], "plot_significant": True,
        "plot_significant_quote": "这句话在原文里根本不存在",
    }

    def _pre_fix_trusts_model_claim_without_verification(mention: dict) -> bool:
        return bool(mention.get("plot_significant"))  # 修复前：不核验 quote，直接信

    assert _pre_fix_trusts_model_claim_without_verification(fabricated) is True, "前提校验：红灯必须先复现"
    assert is_key_prop_mention(fabricated, source_text=source) is False


def test_key_prop_plot_significant_false_does_not_pass_even_with_real_quote():
    """plot_significant=false 时即使 quote 恰好逐字命中原文，也不该被这条判据
    采信——正面陈述要求"证据真实存在"是 true 的必要条件，不是 quote 命中就够。"""
    from app.props.judge import is_key_prop_mention

    source = "桌上摆着一只泡面碗，还没来得及收拾。"
    mention = {
        "label": "泡面碗", "description": "桌上的泡面碗", "segment_indexes": [1],
        "plot_significant": False, "plot_significant_quote": "桌上摆着一只泡面碗",
    }
    assert is_key_prop_mention(mention, source_text=source) is False


def test_key_prop_plot_significant_missing_source_text_never_passes():
    from app.props.judge import is_key_prop_mention

    mention = {
        "label": "黄铜旧星盘", "description": "贴身佩戴的旧物件", "segment_indexes": [5],
        "plot_significant": True, "plot_significant_quote": "指尖顺势碰了碰贴身挂着的一枚硬物",
    }
    assert is_key_prop_mention(mention) is False


async def test_ensure_props_for_labels_normalises_quantifier_and_compound_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    """第 11 轮物件库核查：野鸡/两只野鸡、灵石/半块灵石/凝灵丹与半块灵石 各建了一条。归一后本体已登记只补别名，
    未登记的以本体名建卡、原标签作别名。"""
    _seed_project("p1", props_list=[
        {"name": "野鸡", "appearance_canonical": "已登记的野鸡锚点", "aliases": []},
        {"name": "灵石", "appearance_canonical": "已登记的灵石锚点", "aliases": []},
    ])
    monkeypatch.setattr(judge.model_gateway, "chat_structured", _fake_chat_structured)
    monkeypatch.setattr(service, "generate_prop_reference_image", _fake_generate_image)
    mentions = [
        {"label": "两只野鸡", "description": "两只野鸡", "segment_indexes": [2, 9]},
        {"label": "凝灵丹与半块灵石", "description": "凝灵丹与半块灵石", "segment_indexes": [3, 8]},
    ]
    result = await service.ensure_props_for_labels("p1", 6, mentions, cards_with_prior_evidence=frozenset())
    assert result["errors"] == []
    assert [item["name"] for item in result["added"]] == ["凝灵丹"]
    conn = get_conn()
    bible = json.loads(conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"])
    by_name = {p["name"]: p for p in bible["props"]}
    assert set(by_name) == {"野鸡", "灵石", "凝灵丹"}
    assert by_name["野鸡"]["aliases"] == ["两只野鸡"]
    assert "凝灵丹与半块灵石" in by_name["灵石"]["aliases"] and "凝灵丹与半块灵石" in by_name["凝灵丹"]["aliases"]
