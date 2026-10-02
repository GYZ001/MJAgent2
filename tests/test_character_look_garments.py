"""``app.video_modes.character_look_garments`` 单测，以及它接进
``character_looks_ensure`` 出图管线（种子图顺序/提示词对应关系/端到端）的集成
测试——与纯函数测试放在同一个文件，因为都是围绕"服装单品参考图"这一件事,
比拆成两个文件更利于一起读（``character_looks_ensure`` 自身的 CAS/stale/闸门
测试仍在 ``tests/test_character_looks_ensure.py``，那边已经逼近行数基线）。

真实数据样本取自 2026-10-02 派单附带的 ``/tmp/looks/garment_data.json``
（温念「外套」道具卡与第 1 集 ``continuity_memo`` wardrobe 文本），不在测试里
读 /tmp——按派单要求把相关片段内联成夹具。覆盖：
``match_garment_props``（文本包含匹配、跨度上限、区间包含收敛、同名角色前缀
不误触发、上限 4）、``resolve_garment_refs``（只收 ready 且文件落盘的道具，
查不到/未就绪的跳过，不编造）、种子图顺序、提示词对应关系正面陈述、以及
``ensure_character_looks`` 端到端真的把命中的单品图接成第 2 张种子图。
"""
from __future__ import annotations

import asyncio
import base64
import json

import pytest

from app import hiagent
from app.db import get_conn
from app.props.store import upsert_prop_reference
from app.video_modes import character_looks_ensure as ensure_mod
from app.video_modes.character_look_garments import match_garment_props, resolve_garment_refs

# 世界书 props（与 /tmp/looks/garment_data.json 的 bible_props 子集一致）。
_BIBLE_PROPS = [
    {"name": "绿萝", "aliases": []},
    {"name": "小木星星", "aliases": ["旧木星星"]},
    {"name": "深灰色围巾", "aliases": []},
    {"name": "行李箱", "aliases": ["水泡坏的行李箱", "旧行李箱"]},
    {"name": "手机", "aliases": ["通讯设备"]},
    {"name": "外套", "aliases": []},
    {"name": "米白色针织开衫", "aliases": ["米白针织开衫"]},
    {"name": "浅蓝色碎花长裙", "aliases": ["浅蓝碎花长裙"]},
    {"name": "勺子", "aliases": []},
    {"name": "大衣", "aliases": ["深灰大衣"]},
    {"name": "浅灰色卫衣", "aliases": []},
    {"name": "顾屿外套", "aliases": []},
    {"name": "温念厚外套", "aliases": ["换季厚外套"]},
]

# 第 1 集真实 continuity_memo wardrobe 文本片段。
_WEN_NIAN_WARDROBE = (
    "米白色灯芯绒外套扣好扣子，颈间绕着深灰色针织长围巾，"
    "内穿米白色针织开衫，浅蓝色碎花长裙，米白色平底单鞋"
)
_GU_YU_WARDROBE_1 = "浅灰色圆领宽松卫衣，深色长裤"
_GU_YU_WARDROBE_2 = "深炭灰色羊毛双排扣长大衣，内搭深色毛衫，深色长裤"


# ---------- match_garment_props ----------

def test_matches_multiple_garments_in_order_of_first_appearance():
    assert match_garment_props(_WEN_NIAN_WARDROBE, _BIBLE_PROPS) == [
        "外套", "深灰色围巾", "米白色针织开衫", "浅蓝色碎花长裙",
    ]


def test_matches_single_garment_via_canonical_name():
    assert match_garment_props(_GU_YU_WARDROBE_1, _BIBLE_PROPS) == ["浅灰色卫衣"]


def test_matches_canonical_over_overlong_alias_span():
    """"深灰大衣"别名跨度超限（"深炭灰色...大衣"中间插了太多字），但正名
    "大衣"本身跨度很短，应该命中正名而不是整条判定失败。"""
    assert match_garment_props(_GU_YU_WARDROBE_2, _BIBLE_PROPS) == ["大衣"]


def test_same_character_full_name_props_do_not_misfire_on_other_characters_wardrobe():
    """"温念厚外套"「顾屿外套」是独立登记的道具（各自的字符序列里含角色名），
    wardrobe 文本本身不含角色名，不应该被误判命中。"""
    names = match_garment_props(_WEN_NIAN_WARDROBE, _BIBLE_PROPS)
    assert "温念厚外套" not in names
    assert "顾屿外套" not in names


def test_unrelated_props_not_mentioned_in_text_do_not_match():
    names = match_garment_props(_WEN_NIAN_WARDROBE, _BIBLE_PROPS)
    assert "手机" not in names
    assert "勺子" not in names
    assert "行李箱" not in names


def test_span_exceeding_twice_name_length_does_not_match():
    """"深灰色围巾"五个字被拆得很散（深色外套/灰色长裤/米白色鞋子/围着一条
    巾），子序列确实按顺序存在，但首尾跨度远超 2×5，判定不是在说这件道具。"""
    text = "深色外套，灰色长裤，米白色鞋子，还围着一条巾"
    assert match_garment_props(text, [{"name": "深灰色围巾", "aliases": []}]) == []


def test_contained_interval_keeps_the_longer_name():
    """"围巾"命中的区间被"深灰色围巾"完全包含，只保留长名那件。"""
    text = "颈间绕着深灰色针织长围巾"
    props = [{"name": "围巾", "aliases": []}, {"name": "深灰色围巾", "aliases": []}]
    assert match_garment_props(text, props) == ["深灰色围巾"]


def test_candidate_shorter_than_two_chars_does_not_participate():
    props = [{"name": "衣", "aliases": []}]
    assert match_garment_props("随便穿了件衣服出门", props) == []


def test_empty_wardrobe_text_returns_empty_list():
    assert match_garment_props("", _BIBLE_PROPS) == []
    assert match_garment_props("   ", _BIBLE_PROPS) == []


def test_result_capped_at_max_garment_refs():
    text = "外套大衣围巾卫衣长裙开衫短靴手套"
    props = [
        {"name": "外套", "aliases": []}, {"name": "大衣", "aliases": []},
        {"name": "围巾", "aliases": []}, {"name": "卫衣", "aliases": []},
        {"name": "长裙", "aliases": []}, {"name": "开衫", "aliases": []},
    ]
    assert len(match_garment_props(text, props)) == 4


def test_missing_name_or_blank_aliases_are_skipped_without_crashing():
    props = [{"aliases": ["无主别名"]}, {"name": "", "aliases": []}, {"name": "外套", "aliases": [""]}]
    assert match_garment_props("穿着一件外套", props) == ["外套"]


def test_canonical_name_match_wins_over_alias_match_regardless_of_prop_order():
    """两件不同道具在世界书里出现"甲的正名＝乙的别名"这种数据巧合（道具
    「风衣」把「外套」登记成别名，另有一件道具正名就叫「外套」）：wardrobe
    文本里的"外套"应该裁给正名是「外套」的那件，结果不能随 bible_props 的
    登记顺序改变——改之前这里是真实存在过的 bug（见本次审查复核）。"""
    text = "温念穿着外套，内搭白色衬衫"
    order_a = [{"name": "风衣", "aliases": ["外套"]}, {"name": "外套", "aliases": []}]
    order_b = [{"name": "外套", "aliases": []}, {"name": "风衣", "aliases": ["外套"]}]
    assert match_garment_props(text, order_a) == ["外套"]
    assert match_garment_props(text, order_b) == ["外套"]


@pytest.mark.xfail(strict=True, reason="已知局限：转述为部件名（裙摆）而非整体名（长裙）时，子序列匹配判不到——见模块 docstring，不打算靠再加一张部件词表去堵")
def test_part_name_paraphrase_of_a_full_garment_is_a_known_miss():
    """真实数据（第 1 集 segment_no=13，温念）：同一条裙子被转述成"裙摆"而非
    "长裙"，正名「浅蓝色碎花长裙」没有一个"长"字可凑子序列，无论怎么放宽跨度
    上限都救不回来——这不是跨度限制卡掉的，是文本层面压根不含正名全部字符。
    标 xfail(strict=True) 是为了不假装已覆盖：谁要是悄悄"修好"了这个用例而没
    有改动本测试，这里会反过来报错提醒去掉这个标记并补充说明。"""
    text = "米白色灯芯绒翻领单排扣外套5颗扣子全部扣合，外套下摆以下露出一截浅蓝色碎花裙摆，米白色平底单鞋"
    props = [{"name": "外套", "aliases": []}, {"name": "浅蓝色碎花长裙", "aliases": []}]
    assert match_garment_props(text, props) == ["外套", "浅蓝色碎花长裙"]


# ---------- resolve_garment_refs ----------
# 走真实 app.props.store（与 tests/test_prop_reference_alias_fallback.py 同一套
# get_conn()/upsert_prop_reference 用法），不是自建内存库——resolve_garment_refs
# 的职责就是"接上 app.props 的真实查询"，打桩掉它会测不到接线是否正确。

def _seed_project(project_id: str) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, bible_json, created_at) VALUES(?,?, 'created', ?, 1)",
        (project_id, "测试项目", json.dumps({"props": [
            {"name": "外套", "aliases": []}, {"name": "大衣", "aliases": []},
        ]}, ensure_ascii=False)),
    )
    conn.commit()


def test_resolve_garment_refs_returns_ready_rows_with_existing_files(tmp_path):
    _seed_project("proj_g1")
    conn = get_conn()
    image = tmp_path / "coat.jpg"
    image.write_bytes(b"jpeg")
    ref_id = upsert_prop_reference(
        conn, "proj_g1", "外套", 1,
        appearance="米白灯芯绒，5 颗扣，藏青色罗纹袖口", image_path=str(image), prompt="p",
        status="ready", qa={},
    )
    conn.commit()
    out = resolve_garment_refs(conn, "proj_g1", 1, ["外套"])
    assert out == [{"name": "外套", "prop_reference_id": ref_id, "image_path": str(image)}]


def test_resolve_garment_refs_skips_not_ready_status(tmp_path):
    _seed_project("proj_g2")
    conn = get_conn()
    image = tmp_path / "coat.jpg"
    image.write_bytes(b"jpeg")
    upsert_prop_reference(
        conn, "proj_g2", "外套", 1,
        appearance="a", image_path=str(image), prompt="p", status="generating", qa={},
    )
    conn.commit()
    assert resolve_garment_refs(conn, "proj_g2", 1, ["外套"]) == []


def test_resolve_garment_refs_skips_missing_file_on_disk(tmp_path):
    _seed_project("proj_g3")
    conn = get_conn()
    missing_path = str(tmp_path / "does-not-exist.jpg")
    upsert_prop_reference(
        conn, "proj_g3", "外套", 1,
        appearance="a", image_path=missing_path, prompt="p", status="ready", qa={},
    )
    conn.commit()
    assert resolve_garment_refs(conn, "proj_g3", 1, ["外套"]) == []


def test_resolve_garment_refs_skips_unregistered_prop_without_fabricating():
    _seed_project("proj_g4")
    assert resolve_garment_refs(get_conn(), "proj_g4", 1, ["不存在的道具"]) == []


def test_resolve_garment_refs_preserves_input_order(tmp_path):
    _seed_project("proj_g5")
    conn = get_conn()
    coat = tmp_path / "coat.jpg"
    coat.write_bytes(b"jpeg")
    overcoat = tmp_path / "overcoat.jpg"
    overcoat.write_bytes(b"jpeg")
    upsert_prop_reference(
        conn, "proj_g5", "外套", 1, appearance="a", image_path=str(coat), prompt="p", status="ready", qa={},
    )
    upsert_prop_reference(
        conn, "proj_g5", "大衣", 1, appearance="a", image_path=str(overcoat), prompt="p", status="ready", qa={},
    )
    conn.commit()
    out = resolve_garment_refs(conn, "proj_g5", 1, ["大衣", "外套"])
    assert [ref["name"] for ref in out] == ["大衣", "外套"]


# ---------- character_look_prompt 的对应关系正面陈述 ----------

def test_character_look_prompt_with_garment_names_describes_reference_correspondence():
    """有单品参考图时，提示词必须写清"第 1 张是本人/第 2 张起依次是哪件单品"
    这组对应关系（正面陈述，不是只罗列一句服装文字）。"""
    prompt = ensure_mod.character_look_prompt(
        "国风写实", "二十余岁女子，鹅蛋脸", None, "米白色灯芯绒外套，深灰色围巾",
        ["外套", "深灰色围巾"],
    )
    assert "第 1 张参考图是本人" in prompt
    assert "第 2 张起依次是本段服装单品参考图：外套、深灰色围巾" in prompt
    assert "米白色灯芯绒外套，深灰色围巾" in prompt


def test_character_look_prompt_without_garment_names_keeps_prior_wording():
    """无单品图时保持改造前的提示词语义，不强行套用"第 1 张/第 2 张"措辞。"""
    prompt = ensure_mod.character_look_prompt(
        "国风写实", "二十余岁女子", None, "米白色针织开衫", [],
    )
    assert "第 1 张参考图是本人" not in prompt
    assert "本视角的构图合同优先于前文关于默认定妆照服装、配饰的任何描述" in prompt


# ---------- 种子图顺序：[全身定妆照, 单品1, 单品2, ...] ----------

def test_look_seed_inputs_orders_front_full_first_then_garments(tmp_path):
    front = tmp_path / "front.jpg"
    front.write_bytes(b"front")
    coat = tmp_path / "coat.jpg"
    coat.write_bytes(b"coat")
    scarf = tmp_path / "scarf.jpg"
    scarf.write_bytes(b"scarf")
    seeds = ensure_mod._look_seed_inputs(str(front), [
        {"name": "外套", "prop_reference_id": "r1", "image_path": str(coat)},
        {"name": "深灰色围巾", "prop_reference_id": "r2", "image_path": str(scarf)},
    ])
    assert len(seeds) == 3
    assert seeds[0] == hiagent.data_url_from_file(str(front))
    assert seeds[1] == hiagent.data_url_from_file(str(coat))
    assert seeds[2] == hiagent.data_url_from_file(str(scarf))


def test_look_seed_inputs_with_no_garments_returns_only_front_full(tmp_path):
    front = tmp_path / "front.jpg"
    front.write_bytes(b"front")
    assert ensure_mod._look_seed_inputs(str(front), []) == [hiagent.data_url_from_file(str(front))]


def test_look_seed_inputs_count_stays_within_provider_safe_margin(tmp_path):
    """种子图总数 = 1 + 命中单品数，命中单品数上限 4（``_MAX_GARMENT_REFS``），
    总数不超过 5——远低于已实测/声称的参考图张数上限（见 character_look_
    garments.py 模块文档）。"""
    front = tmp_path / "front.jpg"
    front.write_bytes(b"front")
    garments = []
    for i in range(6):
        p = tmp_path / f"g{i}.jpg"
        p.write_bytes(b"g")
        garments.append({"name": f"单品{i}", "prop_reference_id": f"r{i}", "image_path": str(p)})
    seeds = ensure_mod._look_seed_inputs(str(front), garments[:4])
    assert len(seeds) <= 5


# ---------- ensure_character_looks 端到端：真的把单品图接成第 2 张种子图 ----------

def _seed_episode_with_garment_need(tmp_path) -> tuple[str, str, str]:
    """登记一位人物、一件世界书道具「针织开衫」与它的 ready 参考图——wardrobe
    文本点名了它，验证生成端到端真的把它接成第 2 张种子图。返回
    ``(project_id, episode_id, garment_image_path)``。"""
    front_path = tmp_path / "front_garment.jpg"
    front_path.write_bytes(b"fake-front")
    garment_path = tmp_path / "garment.jpg"
    garment_path.write_bytes(b"fake-garment")
    bible = {
        "world": {"visual_style_canonical": "国风写实"},
        "characters": [{
            "name": "温念", "role": "主角", "appearance_canonical": "二十余岁女子，鹅蛋脸",
        }],
        "props": [{"name": "针织开衫", "aliases": [], "appearance_canonical": "米白色，圆领，针织质地"}],
    }
    conn = get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, bible_json, created_at) VALUES(?,?, 'created', ?, 1)",
        ("proj_ensure_garment", "测试项目", json.dumps(bible, ensure_ascii=False)),
    )
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, "
        "appearance, prompt, image_path, pack_status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("port_ensure_garment", "proj_ensure_garment", "温念", 1, None, "二十余岁女子，鹅蛋脸", "p", str(front_path), "ready", 1.0),
    )
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, status, created_at) VALUES(?,?,?,?,?)",
        ("ep_ensure_garment", "proj_ensure_garment", 1, "created", 1.0),
    )
    payload = {
        "resources": {"characters": [{
            "identity_id": "bible:温念", "display_name": "温念",
            "wardrobe_matches_default": "no", "visibility": "visible",
        }]},
        "continuity_memo": {"characters": [{"identity_id": "bible:温念", "wardrobe": "米白色针织开衫"}]},
    }
    conn.execute(
        "INSERT INTO shots(id, episode_id, shot_no, duration_s, shot_contract_json) VALUES(?,?,?,?,?)",
        ("shot_ensure_garment", "ep_ensure_garment", 1, 15, json.dumps({"storyboard_pack_segment": payload}, ensure_ascii=False)),
    )
    conn.commit()
    upsert_prop_reference(
        conn, "proj_ensure_garment", "针织开衫", 1,
        appearance="米白色，圆领，针织质地", image_path=str(garment_path), prompt="p", status="ready", qa={},
    )
    conn.commit()
    return "proj_ensure_garment", "ep_ensure_garment", str(garment_path)


def test_ensure_character_looks_sends_garment_image_as_second_seed(tmp_path, monkeypatch):
    """wardrobe 点名的道具有 ready 参考图时，生成调用的 ``image_inputs`` 必须是
    [全身定妆照, 单品图]——不是只送定妆照、也不是把单品图漏掉。"""
    project_id, episode_id, garment_path = _seed_episode_with_garment_need(tmp_path)
    captured = {}

    async def fake_generate_image(prompt, *, size, image_inputs=None, call_meta=None):
        captured["image_inputs"] = image_inputs
        captured["prompt"] = prompt
        return {"b64_json": base64.b64encode(b"generated-look-bytes").decode("ascii")}

    monkeypatch.setattr(hiagent, "generate_image", fake_generate_image)

    result = asyncio.run(ensure_mod.ensure_character_looks(project_id=project_id, episode_id=episode_id))

    assert result["summary"]["ready"] == 1
    assert len(captured["image_inputs"]) == 2
    assert captured["image_inputs"][1] == hiagent.data_url_from_file(garment_path)
    assert "针织开衫" in captured["prompt"]
    assert "第 1 张参考图是本人" in captured["prompt"]
