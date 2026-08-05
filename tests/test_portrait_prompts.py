import json
import sqlite3

from app.refs import (
    _merge_generated_portraits,
    portrait_appearance_anchor,
    portrait_prompt,
    production_appearance_anchor,
)
from app.multiview import character_view_prompt
from app.portraits import bible_for_episode
from app.schemas import Bible, Character, World


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
    assert "鞋底均不得贴边或出画" in prompt
    assert "8% 安全边距" in prompt
    assert "禁止额外火焰、斗气光环" in prompt


def test_portrait_prompt_keeps_only_clothed_visible_identity_traits() -> None:
    anchor = (
        "24岁女性，黑色长发，常穿低领露肤上衣配半身裙，杏眼，"
        "标志性特征是粉色乳头、腰侧淡褐色小痣、左手臂烟疤与右眉上方细疤"
    )

    production_anchor = production_appearance_anchor(anchor)
    prompt = portrait_prompt("3D动漫CG，虚构数字角色", anchor)

    assert "粉色乳头" not in production_anchor
    assert "腰侧淡褐色小痣" not in production_anchor
    assert "低领露肤上衣" not in production_anchor
    assert "左手臂烟疤" not in production_anchor
    assert "右眉上方细疤" in production_anchor
    assert "粉色乳头" not in prompt
    assert "腰侧淡褐色小痣" not in prompt
    assert "服装面料不透明并完整覆盖身体" in prompt


def test_multiview_prompt_uses_latest_edited_portrait_prompt() -> None:
    prompt = character_view_prompt(
        "旧画风",
        "旧外观锚点",
        "three_quarter",
        "用户最新定妆提示词：银发少女，红色机甲，赛博写实风",
    )

    assert "用户最新定妆提示词：银发少女，红色机甲，赛博写实风" in prompt
    assert "旧画风" not in prompt
    assert "旧外观锚点" not in prompt
    assert "3/4" in prompt
    assert "视角与构图要求覆盖源提示词" in prompt


def test_accepted_portrait_prompt_derives_downstream_outfit_anchor() -> None:
    latest = (
        "3D国漫写实风。全身角色立绘定妆照：少女，黑色及腰长发，"
        "淡紫色古风长裙，金色刺绣纹饰，广袖流仙裙。正面站立，中性表情，"
        "纯浅米色背景，全身完整可见。"
    )

    anchor = portrait_appearance_anchor(latest, "淡绿色上衣搭配紧腿长裤")

    assert "淡紫色古风长裙" in anchor
    assert "紧腿长裤" not in anchor
    assert "正面站立" not in anchor
    assert "纯浅米色背景" not in anchor


def test_placeholder_portrait_prompt_falls_back_to_segment_appearance() -> None:
    anchor = portrait_appearance_anchor(
        "p",
        "早期：黑发少年，玄色劲装，目光坚定",
    )

    assert anchor == "早期：黑发少年，玄色劲装，目光坚定"


def test_episode_bible_uses_accepted_portrait_prompt_not_stale_appearance(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE character_portraits("
        "project_id TEXT, character_name TEXT, ep_start INTEGER, ep_end INTEGER, "
        "appearance TEXT, prompt TEXT, image_path TEXT)"
    )
    conn.execute(
        "INSERT INTO character_portraits VALUES(?,?,?,?,?,?,?)",
        (
            "p", "萧薰儿", 1, None, "淡绿色上衣搭配紧腿长裤",
            "全身角色立绘定妆照：黑色长发，淡紫色古风长裙，金色刺绣。"
            "正面站立，纯浅米色背景。",
            None,
        ),
    )
    monkeypatch.setattr("app.portraits.get_conn", lambda: conn)
    bible = Bible(
        characters=[Character(
            name="萧薰儿", role="主角", appearance_canonical="淡绿色上衣搭配紧腿长裤",
        )],
        world=World(visual_style_canonical="3D国漫"),
    )

    episode_bible = bible_for_episode("p", bible, 1)

    appearance = episode_bible.characters[0].appearance_canonical
    assert "淡紫色古风长裙" in appearance
    assert "紧腿长裤" not in appearance


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
    assert merged["characters"][0]["appearance_canonical"] == "透明苍老人影"
    assert merged["scenes"] == bible["scenes"]
