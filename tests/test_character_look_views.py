"""人物造型照数据层/解析层单测（``app.video_modes.character_look_views``）。

覆盖：look_key 结构归一化（不做词表替换）、continuity_memo.wardrobe 按 identity_id
（含去前缀匹配）查找、resolve_character_look_selection 的三种结局（默认造型/
造型照就绪/造型照未就绪退回定妆照+可见提示）、以及 scan_episode_character_look_
needs 对整段 resources.characters 的扫描。
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from app import db as db_mod
from app.schemas import Bible, Character, World
from app.video_modes import character_look_views as clv
from app.video_modes.character_look_views_store import ensure_tables_on_connection


def _memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db_mod.SCHEMA)
    for statement in db_mod.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    ensure_tables_on_connection(conn)
    return conn


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = _memory_conn()
    yield c
    c.close()


def _touch(tmp_path, name: str) -> str:
    """``ready_look_view``/``current_portrait_ref`` 都要求参考图文件确实落盘
    （技术产物存在才算 ready），测试必须写一个真实文件，不能只传一个不存在的路径。"""
    path = tmp_path / name
    path.write_bytes(b"fake")
    return str(path)


def _seed_portrait(conn: sqlite3.Connection, *, image_path: str, portrait_id="port_1", project_id="proj_1", name="温念") -> None:
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES(?,?, 'created', 1)",
        (project_id, "测试项目"),
    )
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, "
        "appearance, prompt, image_path, pack_status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (portrait_id, project_id, name, 1, None, "二十余岁女子，鹅蛋脸", "prompt", image_path, "ready", 1.0),
    )
    conn.commit()


# ---------- look_key 归一化 ----------

def test_normalize_look_key_text_collapses_whitespace_and_trailing_punct():
    assert clv.normalize_look_key_text("米白色 针织开衫，\n内搭浅蓝色碎花长裙。") == "米白色 针织开衫， 内搭浅蓝色碎花长裙"


def test_look_key_for_wardrobe_stable_across_trivial_punct_difference():
    """同一件衣服只差一个句号/顿号，不应该被当成两套不同造型。"""
    key_a = clv.look_key_for_wardrobe("深灰色风衣，系着腰带。")
    key_b = clv.look_key_for_wardrobe("深灰色风衣，系着腰带")
    assert key_a == key_b


def test_look_key_for_wardrobe_differs_for_different_text():
    assert clv.look_key_for_wardrobe("深灰色风衣") != clv.look_key_for_wardrobe("米白色风衣")


# ---------- continuity_memo wardrobe 查找 ----------

def test_segment_character_wardrobe_matches_by_exact_identity_id():
    segment = {"continuity_memo": {"characters": [{"identity_id": "bible:温念", "wardrobe": "米白色针织开衫"}]}}
    assert clv.segment_character_wardrobe(segment, "bible:温念") == "米白色针织开衫"


def test_segment_character_wardrobe_matches_after_stripping_prefix():
    """模型偶尔省略 bible:/entity: 前缀，仍要能按主体匹配到——同
    continuity_memo_character_advisories 的归一方式。"""
    segment = {"continuity_memo": {"characters": [{"identity_id": "温念", "wardrobe": "米白色针织开衫"}]}}
    assert clv.segment_character_wardrobe(segment, "bible:温念") == "米白色针织开衫"


def test_segment_character_wardrobe_missing_returns_empty():
    segment = {"continuity_memo": {"characters": []}}
    assert clv.segment_character_wardrobe(segment, "bible:温念") == ""


def test_segment_from_shot_row_parses_storyboard_pack_segment():
    row = {"id": "shot_1", "shot_no": 3, "shot_contract_json": json.dumps({
        "storyboard_pack_segment": {"resources": {"characters": []}},
    })}
    assert clv.segment_from_shot_row(row) == {"resources": {"characters": []}}


def test_segment_from_shot_row_returns_none_for_legacy_shot_without_field():
    row = {"id": "shot_1", "shot_no": 3, "shot_contract_json": json.dumps({"purpose": "x"})}
    assert clv.segment_from_shot_row(row) is None


# ---------- resolve_character_look_selection 三种结局 ----------

def test_resolve_character_look_selection_default_wardrobe_sends_front_full(conn):
    selected, notice = clv.resolve_character_look_selection(
        conn=conn, segment={}, identity_id="bible:温念", portrait_id="port_1",
        front_full_image_path="/tmp/front.jpg", usable=True, wardrobe_matches_default="yes", name="温念",
    )
    assert selected == {
        "id": "port_1", "view_role": "front_full", "image_path": "/tmp/front.jpg",
        "input_fingerprint": "port_1", "costume_mode": None,
    }
    assert notice is None


def test_resolve_character_look_selection_sends_ready_look_view(conn, tmp_path):
    look_path = _touch(tmp_path, "look.jpg")
    key = clv.look_key_for_wardrobe("米白色针织开衫")
    conn.execute(
        "INSERT INTO character_look_views(id, project_id, portrait_id, look_key, wardrobe_text, "
        "image_path, prompt, status, input_fingerprint, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("look_1", "proj_1", "port_1", key, "米白色针织开衫", look_path, "p", "ready", "fp", 1.0, 1.0),
    )
    conn.commit()
    segment = {"continuity_memo": {"characters": [{"identity_id": "bible:温念", "wardrobe": "米白色针织开衫"}]}}
    selected, notice = clv.resolve_character_look_selection(
        conn=conn, segment=segment, identity_id="bible:温念", portrait_id="port_1",
        front_full_image_path="/tmp/front.jpg", usable=True, wardrobe_matches_default="no", name="温念",
    )
    assert selected == {
        "id": "look_1", "view_role": "look", "image_path": look_path,
        "input_fingerprint": "fp", "costume_mode": None,
    }
    assert notice is None


def test_resolve_character_look_selection_falls_back_with_visible_notice(conn):
    """造型照还没生成好：退回定妆照，并且必须给出「到分镜台点『补齐造型照』」的
    可见提示——CLAUDE.md「拦住用户时必须给出路」。"""
    segment = {"continuity_memo": {"characters": [{"identity_id": "bible:温念", "wardrobe": "米白色针织开衫"}]}}
    selected, notice = clv.resolve_character_look_selection(
        conn=conn, segment=segment, identity_id="bible:温念", portrait_id="port_1",
        front_full_image_path="/tmp/front.jpg", usable=True, wardrobe_matches_default="no", name="温念",
    )
    assert selected["view_role"] == "front_full"
    assert selected["costume_mode"] == "neutral"
    assert notice == "「温念」本段造型照未生成，已退回定妆照，服装可能被定妆照带偏；到分镜台点「补齐造型照」"


def test_resolve_character_look_selection_not_usable_returns_none(conn):
    selected, notice = clv.resolve_character_look_selection(
        conn=conn, segment={}, identity_id="bible:温念", portrait_id=None,
        front_full_image_path="", usable=False, wardrobe_matches_default="no", name="温念",
    )
    assert selected is None
    assert notice is None


# ---------- scan_episode_character_look_needs ----------

def _bible_with(name: str) -> Bible:
    return Bible(
        world=World(visual_style_canonical="国风写实"),
        characters=[Character(name=name, role="主角", appearance_canonical="二十余岁女子")],
    )


def test_scan_episode_character_look_needs_reports_missing(conn, tmp_path):
    _seed_portrait(conn, image_path=_touch(tmp_path, "front.jpg"))
    bible = _bible_with("温念")
    payload = {
        "resources": {"characters": [{
            "identity_id": "bible:温念", "display_name": "温念",
            "wardrobe_matches_default": "no", "visibility": "visible",
        }]},
        "continuity_memo": {"characters": [{"identity_id": "bible:温念", "wardrobe": "米白色针织开衫"}]},
    }
    row = {
        "id": "shot_1", "shot_no": 1,
        "shot_contract_json": json.dumps({"storyboard_pack_segment": payload}),
    }
    items = clv.scan_episode_character_look_needs(
        conn=conn, bible=bible, project_id="proj_1", episode_no=1, shot_rows=[row],
    )
    assert len(items) == 1
    assert items[0]["status"] == "missing"
    assert items[0]["look_key"] == clv.look_key_for_wardrobe("米白色针织开衫")


def test_scan_episode_character_look_needs_skips_default_wardrobe(conn, tmp_path):
    _seed_portrait(conn, image_path=_touch(tmp_path, "front.jpg"))
    bible = _bible_with("温念")
    payload = {"resources": {"characters": [{
        "identity_id": "bible:温念", "display_name": "温念",
        "wardrobe_matches_default": "yes", "visibility": "visible",
    }]}}
    row = {
        "id": "shot_1", "shot_no": 1,
        "shot_contract_json": json.dumps({"storyboard_pack_segment": payload}),
    }
    assert clv.scan_episode_character_look_needs(
        conn=conn, bible=bible, project_id="proj_1", episode_no=1, shot_rows=[row],
    ) == []


def test_scan_episode_character_look_needs_reports_stale_when_fingerprint_mismatches(conn, tmp_path):
    """指纹不同的 ready 行必须报 stale，不能被当成仍然正确——这是本次改造要修
    的现存缺陷（此前 look_view_status 只看 status=='ready'+文件存在，永远不会
    因为种子图/画风/单品图/提示词版本变化而重新生成）。"""
    front_path = _touch(tmp_path, "front.jpg")
    _seed_portrait(conn, image_path=front_path)
    look_path = _touch(tmp_path, "look.jpg")
    key = clv.look_key_for_wardrobe("米白色针织开衫")
    conn.execute(
        "INSERT INTO character_look_views(id, project_id, portrait_id, look_key, wardrobe_text, "
        "image_path, prompt, status, input_fingerprint, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("look_1", "proj_1", "port_1", key, "米白色针织开衫", look_path, "p", "ready",
         "fp_from_before_the_seed_image_or_version_changed", 1.0, 1.0),
    )
    conn.commit()
    bible = _bible_with("温念")
    payload = {
        "resources": {"characters": [{
            "identity_id": "bible:温念", "display_name": "温念",
            "wardrobe_matches_default": "no", "visibility": "visible",
        }]},
        "continuity_memo": {"characters": [{"identity_id": "bible:温念", "wardrobe": "米白色针织开衫"}]},
    }
    row = {
        "id": "shot_1", "shot_no": 1,
        "shot_contract_json": json.dumps({"storyboard_pack_segment": payload}),
    }
    items = clv.scan_episode_character_look_needs(
        conn=conn, bible=bible, project_id="proj_1", episode_no=1, shot_rows=[row],
    )
    assert len(items) == 1
    assert items[0]["status"] == "stale"


def test_scan_episode_character_look_needs_reports_ready_when_fingerprint_matches(conn, tmp_path):
    """反向对照：指纹算对了就是 ready，不应该误判成 stale。"""
    front_path = _touch(tmp_path, "front.jpg")
    _seed_portrait(conn, image_path=front_path)
    look_path = _touch(tmp_path, "look.jpg")
    key = clv.look_key_for_wardrobe("米白色针织开衫")
    fingerprint = clv.look_input_fingerprint(
        front_full_image_path=front_path, wardrobe_text="米白色针织开衫",
        visual_style="国风写实", garment_refs=[],
    )
    conn.execute(
        "INSERT INTO character_look_views(id, project_id, portrait_id, look_key, wardrobe_text, "
        "image_path, prompt, status, input_fingerprint, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("look_1", "proj_1", "port_1", key, "米白色针织开衫", look_path, "p", "ready", fingerprint, 1.0, 1.0),
    )
    conn.commit()
    bible = _bible_with("温念")
    payload = {
        "resources": {"characters": [{
            "identity_id": "bible:温念", "display_name": "温念",
            "wardrobe_matches_default": "no", "visibility": "visible",
        }]},
        "continuity_memo": {"characters": [{"identity_id": "bible:温念", "wardrobe": "米白色针织开衫"}]},
    }
    row = {
        "id": "shot_1", "shot_no": 1,
        "shot_contract_json": json.dumps({"storyboard_pack_segment": payload}),
    }
    items = clv.scan_episode_character_look_needs(
        conn=conn, bible=bible, project_id="proj_1", episode_no=1, shot_rows=[row],
    )
    assert len(items) == 1
    assert items[0]["status"] == "ready"


# ---------- look_input_fingerprint ----------

def test_look_input_fingerprint_changes_with_garment_refs():
    """换了命中的单品参考图（名字或 prop_reference_id 任一变化）都要算出新
    指纹，否则道具卡重新登记拿到新图后旧造型照不会被判过期。"""
    base = clv.look_input_fingerprint(
        front_full_image_path="/tmp/front.jpg", wardrobe_text="w", visual_style="国风写实", garment_refs=[],
    )
    with_one_garment = clv.look_input_fingerprint(
        front_full_image_path="/tmp/front.jpg", wardrobe_text="w", visual_style="国风写实",
        garment_refs=[("外套", "prop_rev_1")],
    )
    with_new_prop_revision = clv.look_input_fingerprint(
        front_full_image_path="/tmp/front.jpg", wardrobe_text="w", visual_style="国风写实",
        garment_refs=[("外套", "prop_rev_2")],
    )
    assert base != with_one_garment
    assert with_one_garment != with_new_prop_revision


def test_look_input_fingerprint_order_sensitive():
    """单品顺序变化也要算出不同指纹——顺序决定了提示词里"第 2 张/第 3 张"分别
    对应哪件单品，顺序错了画面与文案就对不上。"""
    a = clv.look_input_fingerprint(
        front_full_image_path="/tmp/front.jpg", wardrobe_text="w", visual_style="国风写实",
        garment_refs=[("外套", "r1"), ("围巾", "r2")],
    )
    b = clv.look_input_fingerprint(
        front_full_image_path="/tmp/front.jpg", wardrobe_text="w", visual_style="国风写实",
        garment_refs=[("围巾", "r2"), ("外套", "r1")],
    )
    assert a != b


def test_scan_episode_character_look_needs_skips_voice_only():
    bible = _bible_with("温念")
    payload = {"resources": {"characters": [{
        "identity_id": "bible:温念", "wardrobe_matches_default": "no", "visibility": "voice_only",
    }]}}
    row = {
        "id": "shot_1", "shot_no": 1,
        "shot_contract_json": json.dumps({"storyboard_pack_segment": payload}),
    }
    conn = _memory_conn()
    try:
        assert clv.scan_episode_character_look_needs(
            conn=conn, bible=bible, project_id="proj_1", episode_no=1, shot_rows=[row],
        ) == []
    finally:
        conn.close()
