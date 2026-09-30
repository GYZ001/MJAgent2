"""``app.video_modes.asset_lookup.character_reference_assets`` 的 ``costume_mode``
接线端到端定向测试（代码评审确认的 blocking 缺陷补测，2026-09-30）。

背景：``app.portraits.neutral_identity`` 采纳中性定妆照后会把
``character_portraits.costume_mode`` 写成 ``"neutral"``，
``app.video_modes.seedance_reference_notes`` 也已经有切换文案的分支——但
真实生成请求唯一的参考图构造入口 ``character_reference_assets`` /
``ReferenceImageAsset`` 从未读出、透传过这个字段，中性定妆照因此在真实生成
时形同虚设（只读复核 2026-09-30 实测复现）。本文件覆盖：

1. 单图回退分支（无多视角库图）：``costume_mode`` 经
   ``app.portraits.portrait_lookup.portrait_lookup_for_episode`` 带到
   ``ReferenceImageAsset.costume_mode``。
2. 多视角库图分支：``costume_mode`` 经 ``app.multiview.portrait_row_for_episode``
   带到 ``ReferenceImageAsset.costume_mode``。
3. 从真实 asset_lookup 产出一路到
   ``build_seedance_reference_prompt_notes`` 的完整链路：中性角色的参考图
   说明切到不声称锁定服装的文案。
4. 老数据（``costume_mode="baked"``）行为不变（回归防线）。
"""
from __future__ import annotations

from app import db
from app.db import get_conn
from app.portraits.neutral_identity import _ensure_costume_mode_column
from app.schemas import Bible
from app.video_modes.asset_lookup import character_reference_assets
from app.video_modes.seedance_reference_notes import build_seedance_reference_prompt_notes


def _isolated_db(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "asset-lookup-costume-mode.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()


def _bible() -> Bible:
    return Bible.model_validate({
        "characters": [{"name": "温念", "role": "配角", "appearance_canonical": "二十四岁的年轻女性"}],
        "scenes": [], "world": {"era": "", "genre": "", "visual_style_canonical": "国风"},
    })


def _seed_portrait_row(tmp_path, *, project_id: str, name: str, costume_mode: str) -> tuple[str, str]:
    conn = get_conn()
    conn.execute("INSERT INTO projects(id, name, created_at) VALUES(?,?,0)", (project_id, "P"))
    _ensure_costume_mode_column(conn)
    image_path = tmp_path / "portrait.jpg"
    image_path.write_bytes(b"x")
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, "
        "appearance, prompt, image_path, bible_version, costume_mode, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,1,?,1.0)",
        ("portrait1", project_id, name, 1, None, "二十四岁的年轻女性", "旧提示词", str(image_path), costume_mode),
    )
    conn.commit()
    return "portrait1", str(image_path)


def test_fallback_branch_carries_neutral_costume_mode_to_asset(tmp_path, monkeypatch):
    _isolated_db(tmp_path, monkeypatch)
    _, image_path = _seed_portrait_row(tmp_path, project_id="p1", name="温念", costume_mode="neutral")

    assets = character_reference_assets(_bible(), ["温念"], limit=1, project_id="p1", episode_no=1)

    assert len(assets) == 1
    assert assets[0].path == image_path
    assert assets[0].costume_mode == "neutral"


def test_views_branch_carries_neutral_costume_mode_to_asset(tmp_path, monkeypatch):
    _isolated_db(tmp_path, monkeypatch)
    portrait_id, _ = _seed_portrait_row(tmp_path, project_id="p1", name="温念", costume_mode="neutral")
    conn = get_conn()
    view_path = tmp_path / "front_full.jpg"
    view_path.write_bytes(b"x")
    conn.execute(
        "INSERT INTO character_portrait_views(id, portrait_id, view_role, status, image_path, created_at) "
        "VALUES(?,?,?,?,?,1.0)",
        ("view1", portrait_id, "front_full", "ready", str(view_path)),
    )
    conn.commit()

    assets = character_reference_assets(_bible(), ["温念"], limit=1, project_id="p1", episode_no=1)

    assert len(assets) == 1
    assert assets[0].path == str(view_path)
    assert assets[0].costume_mode == "neutral"


def test_neutral_costume_mode_asset_switches_seedance_prompt_note_wording(tmp_path, monkeypatch):
    """从真实 asset_lookup 产出一路到 build_seedance_reference_prompt_notes：
    中性角色的参考图说明不再声称锁定服装。"""
    _isolated_db(tmp_path, monkeypatch)
    _seed_portrait_row(tmp_path, project_id="p1", name="温念", costume_mode="neutral")

    assets = character_reference_assets(_bible(), ["温念"], limit=1, project_id="p1", episode_no=1)
    prompt = build_seedance_reference_prompt_notes(
        "镜头1：@温念 端着茶杯站在窗边。", [a.public_dict() for a in assets], aspect_ratio="9:16",
    )

    assert "服装和表情以本段文字为准" in prompt
    assert "锁定长相与服装" not in prompt


def test_baked_costume_mode_asset_keeps_old_prompt_note_wording(tmp_path, monkeypatch):
    """老数据（``costume_mode="baked"``）：文案逐字不变，回归防线。"""
    _isolated_db(tmp_path, monkeypatch)
    _seed_portrait_row(tmp_path, project_id="p1", name="温念", costume_mode="baked")

    assets = character_reference_assets(_bible(), ["温念"], limit=1, project_id="p1", episode_no=1)
    prompt = build_seedance_reference_prompt_notes(
        "镜头1：@温念 端着茶杯站在窗边。", [a.public_dict() for a in assets], aspect_ratio="9:16",
    )

    assert assets[0].costume_mode == "baked"
    assert "锁定长相与服装" in prompt
    assert "服装和表情以本段文字为准" not in prompt
