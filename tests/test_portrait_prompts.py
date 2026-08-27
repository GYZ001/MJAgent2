import json
import sqlite3

from app.refs import (
    _merge_generated_portraits,
    character_visual_style_lock,
    effective_portrait_prompt,
    portrait_appearance_anchor,
    portrait_prompt,
    production_appearance_anchor,
    scene_visual_style_lock,
)
from app.multiview import character_view_prompt
from app.portraits import bible_for_episode
from app.schemas import Bible, Character, World
from app.visual_styles import visual_style_prompt


def test_portrait_prompt_preserves_nonstandard_form_without_word_routing() -> None:
    prompt = portrait_prompt(
        "国漫风3D渲染CG，斗气特效华丽",
        "透明苍老人影，白须飘飘，悬浮于黑色古朴戒指之上，神态戏谑",
    )

    assert "透明苍老人影" in prompt
    assert "悬浮于黑色古朴戒指之上" in prompt
    assert "神态戏谑" in prompt
    assert "完整遵循锚点声明的实体形态、空间关系、姿态和关联道具" in prompt


def test_portrait_prompt_uses_open_conditional_model_sheet_pose() -> None:
    prompt = portrait_prompt("国漫风", "黑发少年，身着青灰色布衣")

    assert "若锚点未声明特殊姿态，则采用正面中性展示姿态" in prompt
    assert "鞋底均不得贴边或出画" in prompt
    assert "8% 安全边距" in prompt
    assert "明显动画化比例和非照片级卡通渲染材质" in prompt
    assert "不得生成可误认成真人照片的写实人脸" in prompt
    assert "不得添加外观合同未声明的主体或视觉元素" in prompt


def test_portrait_prompt_does_not_fight_photographic_style_preset() -> None:
    """根因回归：真人摄影风预设不应再被硬编码的"必须是卡通/CG"文案顶掉。

    改动前 character_visual_style_lock/portrait_prompt 对所有画风一律追加
    "人物面部与皮肤必须采用明显动画化比例和非照片级卡通渲染材质"，与刚刚在同一
    句话里声明的"必须严格保持「照片级人像摄影质感」"直接自相矛盾，这是"新增的
    真人风格对定妆照不生效"的根因。"""
    real_photo_style = visual_style_prompt("真人摄影风")
    prompt = portrait_prompt(real_photo_style, "十五六岁少年，黑发束成马尾")

    assert "照片级人像摄影质感" in prompt
    assert "摄影级写实质感和自然人体比例" in prompt
    assert "非照片级卡通" not in prompt
    assert "动画化比例" not in prompt
    assert "不得擅自切换成卡通" in prompt


def test_character_and_scene_style_lock_stay_non_photographic_for_cg_presets() -> None:
    """未选中照片级预设时，原有"必须保持 CG/非真人渲染"防线保持不变（不回归）。"""
    guoman_style = visual_style_prompt("国漫电影风")

    char_lock = character_visual_style_lock(guoman_style)
    scene_lock = scene_visual_style_lock(guoman_style)

    assert "CG/动画/漫画/插画类非真人渲染" in char_lock
    assert "明显动画化比例和非照片级卡通/CG 渲染材质" in char_lock
    assert "动画/插画/CG 场景渲染" in scene_lock


def test_portrait_prompt_preserves_approved_identity_contract_verbatim() -> None:
    anchor = (
        "24岁女性，黑色长发，常穿低领露肤上衣配半身裙，杏眼，"
        "标志性特征是粉色乳头、腰侧淡褐色小痣、左手臂烟疤与右眉上方细疤"
    )

    production_anchor = production_appearance_anchor(anchor)
    prompt = portrait_prompt("3D动漫CG，虚构数字角色", anchor)

    assert production_anchor == anchor
    assert anchor in prompt
    assert "服装面料不透明并完整覆盖身体" in prompt


def test_production_anchor_does_not_classify_behavior_words() -> None:
    anchor = (
        "40岁男性，短发，身材微胖，深色西装配白衬衫，"
        "标志性特征是看向女性时色欲外露的眼神，气质强势"
    )

    production_anchor = production_appearance_anchor(anchor)

    assert production_anchor == anchor


def test_production_anchor_does_not_filter_appearance_vocabulary() -> None:
    anchor = (
        "24岁女性，黑色长发，杏眼秀眉，身材丰满修长，"
        "常穿简约通勤衬衫配及膝裙、丝袜、高跟鞋"
    )

    production_anchor = production_appearance_anchor(anchor)

    assert production_anchor == anchor


def test_effective_portrait_prompt_keeps_override_and_independent_style_contract() -> None:
    prompt = effective_portrait_prompt(
        "国漫电影风",
        "黑发少女，月白长裙",
        "用户最新定妆提示词：银发少女，红色机甲，赛博写实风",
    )

    assert "国漫电影风" in prompt
    assert "红色机甲" in prompt
    assert "赛博写实风" in prompt
    assert "全局画风合同仍独立生效" in prompt


def test_multiview_prompt_keeps_latest_edit_without_keyword_filtering() -> None:
    prompt = character_view_prompt(
        "旧画风",
        "旧外观锚点",
        "three_quarter",
        "用户最新定妆提示词：银发少女，红色机甲，赛博写实风",
    )

    assert "旧画风" in prompt
    assert "旧外观锚点" not in prompt
    assert "红色机甲" in prompt
    assert "赛博写实风" in prompt
    assert "3/4" in prompt
    assert "画风最高优先级" in prompt
    assert "视角与构图要求覆盖源提示词" in prompt


def test_persisted_appearance_is_authority_instead_of_prompt_parsing() -> None:
    latest = (
        "3D国漫写实风。全身角色立绘定妆照：少女，黑色及腰长发，"
        "淡紫色古风长裙，金色刺绣纹饰，广袖流仙裙。正面站立，中性表情，"
        "纯浅米色背景，全身完整可见。"
    )

    anchor = portrait_appearance_anchor(latest, "淡绿色上衣搭配紧腿长裤")

    assert anchor == "淡绿色上衣搭配紧腿长裤"


def test_placeholder_portrait_prompt_falls_back_to_segment_appearance() -> None:
    anchor = portrait_appearance_anchor(
        "p",
        "早期：黑发少年，玄色劲装，目光坚定",
    )

    assert anchor == "早期：黑发少年，玄色劲装，目光坚定"


def test_episode_bible_uses_persisted_appearance_not_prompt_word_extraction(monkeypatch) -> None:
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
            "p", "甲二儿", 1, None, "淡绿色上衣搭配紧腿长裤",
            "全身角色立绘定妆照：黑色长发，淡紫色古风长裙，金色刺绣。"
            "正面站立，纯浅米色背景。",
            None,
        ),
    )
    monkeypatch.setattr("app.portraits.get_conn", lambda: conn)
    bible = Bible(
        characters=[Character(
            name="甲二儿", role="主角", appearance_canonical="淡绿色上衣搭配紧腿长裤",
        )],
        world=World(visual_style_canonical="3D国漫"),
    )

    episode_bible = bible_for_episode("p", bible, 1)

    appearance = episode_bible.characters[0].appearance_canonical
    assert appearance == "淡绿色上衣搭配紧腿长裤"


def test_portrait_completion_merges_without_erasing_concurrent_scenes() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT)")
    bible = {
        "characters": [{"name": "丙老", "ref_image_path": None}],
        "scenes": [{"name": "后山山崖", "scene_canonical": "并发场景任务已写入"}],
    }
    conn.execute("INSERT INTO projects VALUES('p', ?)", (json.dumps(bible, ensure_ascii=False),))
    character = Character(
        name="丙老", role="配角", appearance_canonical="透明苍老人影",
        ref_image_path="accepted.jpg",
    )

    _merge_generated_portraits(conn, "p", [character])
    merged = json.loads(conn.execute("SELECT bible_json FROM projects WHERE id='p'").fetchone()[0])

    assert merged["characters"][0]["ref_image_path"] == "accepted.jpg"
    assert merged["characters"][0]["appearance_canonical"] == "透明苍老人影"
    assert merged["scenes"] == bible["scenes"]
