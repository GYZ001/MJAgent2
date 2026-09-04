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
    assert store.prop_reference_for_episode(conn, "p1", "旧猫包", 2) is None
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

    result = await service.ensure_props_for_labels("p1", 3, mentions)

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

    result = await service.ensure_props_for_labels("p1", 1, mentions)

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

    result = await service.ensure_props_for_labels("p1", 1, mentions)

    assert result == {"added": [], "errors": []}


async def test_ensure_props_for_labels_records_failed_image(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_project("p1")
    monkeypatch.setattr(judge.model_gateway, "chat_structured", _fake_chat_structured)
    monkeypatch.setattr(service, "generate_prop_reference_image", _failing_generate_image)
    mentions = [{"label": "旧猫包", "description": "旧猫包", "segment_indexes": [2, 9]}]

    result = await service.ensure_props_for_labels("p1", 1, mentions)

    assert [item["has_image"] for item in result["added"]] == [False]
    row = store.prop_reference_for_episode(get_conn(), "p1", "旧猫包", 1)
    assert row["status"] == "failed"
    assert row["image_path"] is None


async def test_ensure_props_for_labels_no_bible_is_advisory_noop() -> None:
    result = await service.ensure_props_for_labels("nope", 1, [
        {"label": "旧猫包", "description": "旧猫包", "segment_indexes": [1, 2]},
    ])
    assert result == {"added": [], "errors": []}


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

    assert response == {"name": "旧猫包", "status": "ready", "image_path": "/fake/p1/旧猫包.png"}
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
