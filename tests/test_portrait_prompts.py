import json
import sqlite3

from app.refs import _merge_generated_portraits, portrait_prompt
from app.schemas import Character


def test_spectral_portrait_prompt_does_not_force_neutral_grounded_pose() -> None:
    prompt = portrait_prompt(
        "国漫风3D渲染CG，斗气特效华丽",
        "透明苍老人影，白须飘飘，悬浮于黑色古朴戒指之上，神态戏谑",
    )

    assert "不要套用普通人的站立证件照姿态" in prompt
    assert "双脚离地" in prompt
    assert "角色垂直悬浮在戒指正上方" in prompt
    assert "禁止严肃皱眉或中性表情" in prompt
    assert "禁止额外火焰、斗气光环" in prompt
    assert "正面站立，中性表情，双臂自然下垂" not in prompt


def test_ordinary_portrait_prompt_keeps_standard_model_sheet_pose() -> None:
    prompt = portrait_prompt("国漫风", "黑发少年，身着青灰色布衣")

    assert "正面站立，中性表情，双臂自然下垂" in prompt
    assert "禁止额外火焰、斗气光环" in prompt


def test_portrait_completion_merges_without_erasing_concurrent_scenes() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT)")
    bible = {
        "characters": [{"name": "药老", "ref_image_path": None}],
        "scenes": [{"name": "后山山崖", "scene_canonical": "并发场景任务已写入"}],
    }
    conn.execute("INSERT INTO projects VALUES('p', ?)", (json.dumps(bible, ensure_ascii=False),))
    character = Character(
        name="药老", role="配角", appearance_canonical="透明苍老人影",
        ref_image_path="accepted.jpg",
    )

    _merge_generated_portraits(conn, "p", [character])
    merged = json.loads(conn.execute("SELECT bible_json FROM projects WHERE id='p'").fetchone()[0])

    assert merged["characters"][0]["ref_image_path"] == "accepted.jpg"
    assert merged["scenes"] == bible["scenes"]
