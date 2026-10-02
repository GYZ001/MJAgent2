"""映射台 2.0.13/2.0.14 缺陷②：模型只看到道具卡名，看不到卡的外观/归属，
泛名卡吸走不同的东西（见 ``chunking.py`` ``_prep_pack_known_prop_names`` 与
``chunk_extraction.py`` ``_KNOWN_PROP_NAME_FIELD_RULE`` 上方 PREP_PACK_
VERSION 2.0.13/2.0.14 changelog）。

真实案例（顾念长安第2集，用准备包 2.0.12 重跑映射台）：项目物件库里有一张
名字很泛的「外套」卡，``appearance_canonical`` 画的是温念那件"米白色灯芯绒、
翻领单排扣、藏青色罗纹袖口"的外套。第6段原文「顾屿……又顺手把自己的外套
搭在她肩上」被提名成这张「外套」卡，分镜台因此给顾屿的外套配上了温念外套
的参考图——提示词里本来就要求"同一件实物，不只是同类"，但
``chunking._prep_pack_known_prop_names`` 只给模型名字+别名列表，模型手里
根本没有外观信息，无从判断。这不是模型能力问题，是模型没拿到判断所需的
标准答案（CLAUDE.md「模型答不出来时，先查它有没有收到标准答案」）。

2.0.13 先给了外观，2.0.14 用 B 沙箱真实数据复测仍未堵住——根因是这条提及
自己的本段原文（"又顺手把自己的外套搭在她肩上"）压根没有描述外套的材质/
颜色，外观矛盾这条判据没有机会触发。appearance_canonical 装不下"这是谁的"
（关系型事实，不扩展 Prop 数据结构），改为从项目内已有数据推导：这张卡在
更早集数里真实出现过的 ``asset_manifest.props`` 条目本来就带着当时的
``description``（真实数据第1集那条是"温念穿的外套，顾屿曾帮她扣好扣子"，
物主写得明明白白），``_prep_pack_known_prop_names`` 现在把这些「此前出场」
证据也带给模型。

本文件钉住修复：
1. ``_prep_pack_known_prop_names`` 把每张卡的名字、别名、外观
   （``appearance_canonical``，过长截断）、以及「此前出场」归属证据一起
   带给模型；``episode_id``/``episode_no`` 必传（Ownership Must Be
   Explicit），用于排除当前集自己，不拿本次正在重算的结果自证。
2. ``known_prop_name`` 字段说明（单源常量 ``_KNOWN_PROP_NAME_FIELD_RULE``，
   抽取/复核共用）改成按外观/归属核对"是不是同一件实物"的完整正面陈述。
3. 显示格式的变化不弄坏 ``app.props.card_match`` 的名字核验——提名核验仍然
   按卡的 ``name``/``alias`` 精确字符串匹配，不读这里拼出的展示行。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from app.production.prep_pack import chunk_extraction as ce
from app.production.prep_pack import chunking, prop_recheck
from app.props.card_match import match_existing_prop_card
from app.schemas import Prop
from app.source_excerpt import SourceSegment

CUR_EPISODE_ID = "ep_current"
CUR_EPISODE_NO = 2


class _Captured(Exception):
    """只用来把控制权从被测协程里拿回来，不代表失败。"""


def _make_conn_with_props(props: list[dict]) -> sqlite3.Connection:
    """最小 conn：只有 ``projects``/``episodes`` 两张表——「此前出场」查询
    （``_prep_pack_other_episode_prop_mentions``）读 ``episodes``，没有任何
    其它集时查询结果天然为空，不需要额外判空分支。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT)")
    conn.execute(
        "CREATE TABLE episodes(id TEXT PRIMARY KEY, project_id TEXT, episode_no INTEGER, "
        "screenplay_json TEXT)"
    )
    conn.execute(
        "INSERT INTO projects(id, bible_json) VALUES ('p1', ?)",
        (json.dumps({
            "characters": [], "scenes": [], "props": props,
            "world": {"era": "", "genre": "", "visual_style_canonical": "测试画风"},
        }, ensure_ascii=False),),
    )
    conn.commit()
    return conn


def _seed_other_episode_props(
    conn: sqlite3.Connection, episode_id: str, episode_no: int, props: list[dict],
) -> None:
    """往 ``episodes`` 表插入一条别的集的已发布 ``screenplay_json``，形状
    只保留 ``_prep_pack_other_episode_prop_mentions`` 真正读取的
    ``asset_manifest.props`` 这一块（其它键没有调用方需要，省略）。"""
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, screenplay_json) VALUES (?, 'p1', ?, ?)",
        (episode_id, episode_no, json.dumps({"asset_manifest": {"props": props}}, ensure_ascii=False)),
    )
    conn.commit()


def _known_prop_names(conn: sqlite3.Connection) -> list[str]:
    """照抄 ``generate_once.py`` 调用点的参数形状——当前集固定为
    ``CUR_EPISODE_ID``/``CUR_EPISODE_NO``，供全部测试复用同一组默认值。"""
    return chunking._prep_pack_known_prop_names(conn, "p1", CUR_EPISODE_ID, CUR_EPISODE_NO)


# ---------------------------------------------------------------------------
# _prep_pack_known_prop_names：名单要带外观，不只是名字/别名
# ---------------------------------------------------------------------------


def test_known_prop_names_includes_name_alias_and_appearance() -> None:
    conn = _make_conn_with_props([{
        "name": "外套", "aliases": ["温念的外套"],
        "appearance_canonical": "米白色灯芯绒，翻领单排扣，藏青色罗纹袖口",
    }])

    names = _known_prop_names(conn)

    assert len(names) == 1
    assert "名称：外套" in names[0]
    assert "别名：温念的外套" in names[0]
    assert "外观：米白色灯芯绒，翻领单排扣，藏青色罗纹袖口" in names[0]


def test_known_prop_names_omits_empty_alias_or_appearance_parts() -> None:
    conn = _make_conn_with_props([{
        "name": "旧铜盘", "aliases": [], "appearance_canonical": "",
    }])

    names = _known_prop_names(conn)

    assert names == ["名称：旧铜盘"]
    assert "别名" not in names[0]
    assert "外观" not in names[0]


def test_long_appearance_is_truncated_not_dropped() -> None:
    """外观过长时截断而不是整条丢弃——截断长度取自模块常量，不是魔数，
    截断后仍保留"外观："前缀，只是内容变短。"""
    long_appearance = "甲" * 200
    conn = _make_conn_with_props([{
        "name": "外套", "aliases": [], "appearance_canonical": long_appearance,
    }])

    names = _known_prop_names(conn)

    limit = chunking._KNOWN_PROP_APPEARANCE_PREVIEW_CHARS
    assert f"外观：{'甲' * limit}…" in names[0]
    assert long_appearance not in names[0], "超出截断长度的原文不应该完整出现"


# ---------------------------------------------------------------------------
# 「此前出场」：从项目内已有数据推导归属证据，不扩展 Prop 数据结构（2.0.14）
# ---------------------------------------------------------------------------


def test_known_prop_names_includes_prior_episode_ownership_evidence() -> None:
    """真实案例原样复现：卡「外套」，第1集已发布的 asset_manifest.props 里
    canonical_name="外套" 的条目 description="温念穿的外套，顾屿曾帮她扣好
    扣子"——名单行要能看到这段证据。"""
    conn = _make_conn_with_props([{
        "name": "外套", "aliases": [],
        "appearance_canonical": "米白色灯芯绒，翻领单排扣，藏青色罗纹袖口",
    }])
    _seed_other_episode_props(conn, "ep1", 1, [{
        "label": "外套", "canonical_name": "外套",
        "description": "温念穿的外套，顾屿曾帮她扣好扣子",
        "segment_indexes": [3, 21], "plot_significant": False,
        "plot_significant_quote": "", "source_wording": "外套", "known_prop_name": "外套",
    }])

    names = _known_prop_names(conn)

    assert len(names) == 1
    assert "此前出场：第1集 温念穿的外套，顾屿曾帮她扣好扣子" in names[0]


def test_prior_appearance_matches_by_label_against_card_name_or_alias_too() -> None:
    """跨集 label 写法可能跟这次的卡名不完全一致（真实数据第2集这条提及的
    label 就是「外套」而不是精确登记名）——命中判据是 canonical_name==卡名
    或 label 命中卡名/别名，不要求 canonical_name 存在。"""
    conn = _make_conn_with_props([{
        "name": "行李箱", "aliases": ["水泡坏的行李箱"],
        "appearance_canonical": "24寸卡其色硬壳拉杆箱",
    }])
    _seed_other_episode_props(conn, "ep1", 1, [{
        "label": "水泡坏的行李箱", "canonical_name": None,
        "description": "温念从老家带来的行李箱，边角有水渍",
        "segment_indexes": [10], "plot_significant": False,
        "plot_significant_quote": "", "source_wording": "", "known_prop_name": "",
    }])

    names = _known_prop_names(conn)

    assert "此前出场：第1集 温念从老家带来的行李箱，边角有水渍" in names[0]


def test_current_episode_own_screenplay_json_is_never_read() -> None:
    """排除当前集自己（CLAUDE.md「Ownership Must Be Explicit」：不拿本次
    正在重算、尚未定稿的结果自证）——哪怕当前集自己的 episodes 行已经有
    screenplay_json（例如上一次失败的尝试遗留），也不读进「此前出场」。"""
    conn = _make_conn_with_props([{
        "name": "外套", "aliases": [], "appearance_canonical": "米白色灯芯绒",
    }])
    _seed_other_episode_props(conn, CUR_EPISODE_ID, CUR_EPISODE_NO, [{
        "label": "外套", "canonical_name": "外套",
        "description": "这是本集自己尚未定稿的结果，不该被读到",
        "segment_indexes": [6], "plot_significant": False,
        "plot_significant_quote": "", "source_wording": "外套", "known_prop_name": "外套",
    }])

    names = _known_prop_names(conn)

    assert "此前出场" not in names[0]


def test_later_episode_is_not_prior_appearance() -> None:
    """「此前出场」按字面是"更早"，不是"项目内任何其它集"——episode_no 比
    当前集大的集数即使已有 screenplay_json，在故事时间线上也发生在本集
    之后，不构成可比对的历史证据。"""
    conn = _make_conn_with_props([{
        "name": "外套", "aliases": [], "appearance_canonical": "米白色灯芯绒",
    }])
    _seed_other_episode_props(conn, "ep5", CUR_EPISODE_NO + 3, [{
        "label": "外套", "canonical_name": "外套",
        "description": "未来某集才会出现的描述",
        "segment_indexes": [1], "plot_significant": False,
        "plot_significant_quote": "", "source_wording": "外套", "known_prop_name": "外套",
    }])

    names = _known_prop_names(conn)

    assert "此前出场" not in names[0]


def test_prior_appearance_falls_back_to_plot_significant_quote_when_description_blank() -> None:
    description = ""
    conn = _make_conn_with_props([{
        "name": "旧铜盘", "aliases": [], "appearance_canonical": "直径12cm黄铜",
    }])
    _seed_other_episode_props(conn, "ep1", 1, [{
        "label": "旧铜盘", "canonical_name": "旧铜盘", "description": description,
        "segment_indexes": [6], "plot_significant": True,
        "plot_significant_quote": "顾屿贴身收着这枚旧铜盘，是父亲留给他的",
        "source_wording": "旧铜盘", "known_prop_name": "旧铜盘",
    }])

    names = _known_prop_names(conn)

    assert "此前出场：第1集 顾屿贴身收着这枚旧铜盘，是父亲留给他的" in names[0]


def test_prior_appearance_text_is_truncated() -> None:
    conn = _make_conn_with_props([{
        "name": "外套", "aliases": [], "appearance_canonical": "米白色灯芯绒",
    }])
    long_description = "甲" * 200
    _seed_other_episode_props(conn, "ep1", 1, [{
        "label": "外套", "canonical_name": "外套", "description": long_description,
        "segment_indexes": [3], "plot_significant": False,
        "plot_significant_quote": "", "source_wording": "外套", "known_prop_name": "外套",
    }])

    names = _known_prop_names(conn)

    limit = chunking._KNOWN_PROP_PRIOR_APPEARANCE_DESC_CHARS
    assert f"此前出场：第1集 {'甲' * limit}…" in names[0]
    assert long_description not in names[0]


def test_prior_appearance_takes_at_most_two_most_recent_episodes() -> None:
    conn = _make_conn_with_props([{
        "name": "外套", "aliases": [], "appearance_canonical": "米白色灯芯绒",
    }])
    _seed_other_episode_props(conn, "ep1", 1, [{
        "label": "外套", "canonical_name": "外套", "description": "第1集的描述",
        "segment_indexes": [1], "plot_significant": False,
        "plot_significant_quote": "", "source_wording": "外套", "known_prop_name": "外套",
    }])
    _seed_other_episode_props(conn, "ep2", 2, [{
        "label": "外套", "canonical_name": "外套", "description": "第2集的描述",
        "segment_indexes": [1], "plot_significant": False,
        "plot_significant_quote": "", "source_wording": "外套", "known_prop_name": "外套",
    }])
    _seed_other_episode_props(conn, "ep3", 3, [{
        "label": "外套", "canonical_name": "外套", "description": "第3集的描述",
        "segment_indexes": [1], "plot_significant": False,
        "plot_significant_quote": "", "source_wording": "外套", "known_prop_name": "外套",
    }])

    names = chunking._prep_pack_known_prop_names(conn, "p1", "ep_current", 4)

    assert "第3集的描述" in names[0]
    assert "第2集的描述" in names[0]
    assert "第1集的描述" not in names[0], "只取最近 2 条，更早的第1集被挤出"


# ---------------------------------------------------------------------------
# known_prop_name 字段说明：单源常量，抽取/复核两处提示词都要带上
# ---------------------------------------------------------------------------


def test_known_prop_name_rule_tells_model_to_check_appearance_and_ownership() -> None:
    """完整正面陈述：核对外观是否吻合、物主是否对得上，而不是只靠名字像不像
    —— CLAUDE.md「schema 允许的，校验就不许拒绝」姊妹纪律「写完整的正面
    陈述」：告诉模型怎么判断，而不是只禁止它犯错。"""
    assert "名单每条都附了这张卡的外观特征" in ce._KNOWN_PROP_NAME_FIELD_RULE
    assert "外观明显矛盾、或原文写明这是另一个人的东西" in ce._KNOWN_PROP_NAME_FIELD_RULE
    assert "自己的外套" in ce._KNOWN_PROP_NAME_FIELD_RULE
    assert "不是同一件实物，不要提名" in ce._KNOWN_PROP_NAME_FIELD_RULE


def test_known_prop_name_rule_tells_model_to_check_prior_appearance_ownership() -> None:
    """2.0.14 续修：外观矛盾在"本段原文自己没有外观描述"时没有机会触发
    （真实案例「自己的外套」本段没写材质/颜色），判据要补上"拿「此前出场」
    的归属跟本段原文写到的归属核对"这条完整正面陈述，而不是放弃核验。"""
    rule = ce._KNOWN_PROP_NAME_FIELD_RULE
    assert "「此前出场」" in rule
    assert "往往写明了它当时是谁的、谁带来的、从哪来" in rule
    assert "归属对不上就是另一件东西，不要提名" in rule
    assert "不要因为名字和外观都对得上就忽略归属矛盾" in rule
    assert "没有「此前出场」信息、或双方都没写明归属线索时，退回只看外观是否矛盾" in rule


def test_known_prop_name_rule_still_requires_copying_the_bare_name() -> None:
    """模型最终要填进 known_prop_name 的仍然是卡的名称本身（供
    app.props.card_match 精确匹配），不是整段外观/此前出场描述——显示格式
    变丰富了，但返回值契约不能跟着变丰富。"""
    assert (
        "从名单里逐字复制那张卡的名称（只复制名称本身，不要写别名、外观或"
        "此前出场文字）"
    ) in ce._KNOWN_PROP_NAME_FIELD_RULE


def test_extraction_prompt_includes_the_rule_verbatim() -> None:
    seen: dict[str, str] = {}

    async def fake_call(**kwargs):
        seen["prompt"] = kwargs.get("prompt") or ""
        raise _Captured

    original = ce._call_structured
    ce._call_structured = fake_call
    try:
        segment = SourceSegment(
            segment_id="s1", start_offset=0, end_offset=20,
            text="顾屿又顺手把自己的外套搭在她肩上。",
        )
        with pytest.raises(_Captured):
            asyncio.run(ce._extract_chunk(
                chunk=[(1, segment)], known_characters=[], known_scenes=[],
                known_props=["名称：外套｜外观：米白色灯芯绒，翻领单排扣，藏青色罗纹袖口"],
                attempt_hint="", run_id=None, episode_id="ep2", episode_no=2, chunk_index=0,
            ))
    finally:
        ce._call_structured = original

    assert ce._KNOWN_PROP_NAME_FIELD_RULE in seen["prompt"]
    assert "名称：外套｜外观：米白色灯芯绒" in seen["prompt"]


def test_recheck_prompt_includes_the_same_rule_via_single_source() -> None:
    """复核提示词通过同一个 ``_KNOWN_PROP_NAME_FIELD_RULE`` 常量带上这段
    说明——字符串级相等，不是两处各写一份措辞。"""
    prompt = prop_recheck._prompt("（原文）", ["名称：外套｜外观：米白色灯芯绒"], [])
    assert ce._KNOWN_PROP_NAME_FIELD_RULE in prompt


# ---------------------------------------------------------------------------
# 显示格式变化不弄坏 app.props.card_match 的名字核验
# ---------------------------------------------------------------------------


def test_card_match_nomination_still_matches_by_bare_card_name() -> None:
    """名单展示行变成"名称：外套｜别名：...｜外观：..."这种富文本，但
    card_match 核验的仍然是模型返回的裸名字（known_prop_name="外套"）对照
    ``Prop.name``/``aliases`` 的精确字符串——显示格式变化不影响名字核验。
    提名非空（`nominated_card="外套"`）时走提名路径，不经过 2026-10-01
    第三轮新增的 ``cards_with_prior_evidence`` 收紧分支（那条只在未提名时
    生效，见 tests/test_prop_card_binding.py 对那条分支的专项测试）。"""
    card = Prop(name="外套", appearance_canonical="米白色灯芯绒，翻领单排扣，藏青色罗纹袖口")
    evidence_text = "顾屿又顺手把自己的外套搭在她肩上"

    resolved = match_existing_prop_card(
        "外套", evidence_text, [card], source_wording="外套", nominated_card="外套",
        cards_with_prior_evidence=frozenset(),
    )

    assert resolved is card
