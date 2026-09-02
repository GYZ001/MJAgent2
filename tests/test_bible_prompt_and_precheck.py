"""提示词规范化与人物谱扩展预检。"""
from __future__ import annotations

import sqlite3

from app.domain import common
from app.refs import normalize_prompt_text, portrait_prompt
from app.orchestration.engine import fingerprint
from app.schemas import Bible, Character, World
from app.validators import validate_bible
from tests.conftest import patch_api_everywhere
from tests.conftest import patch_worker_everywhere


def test_normalize_prompt_collapses_duplicate_punctuation() -> None:
    assert ".." not in normalize_prompt_text("戒指。。正面站立")
    assert "。。" not in normalize_prompt_text("戒指。。正面站立")
    assert normalize_prompt_text("戒指。。正面站立") == "戒指。正面站立"


def test_whatever_lands_in_appearance_becomes_a_literal_drawing_instruction() -> None:
    """appearance_canonical 的内容会原样变成图像模型的作画指令。

    这条锁的是契约本身，不是措辞：portrait_prompt 不做任何语义过滤，谱里写
    什么就画什么。所以往这个字段里写「关于原文的说明」等于让图像模型去画那
    句说明——真实事故里三个角色（靠山老祖/陈凡/何洛华）的定妆照 prompt 变成
    「单角色全身定妆照：原文未点明性别，是靠山宗掌门……」。

    BIBLE_APPEARANCE_FIELD_RULE 之所以要求整段都是可画内容，根据就在这里。
    """
    leaked = portrait_prompt("国风水墨", "原文未点明性别，是靠山宗掌门，神态威严")
    assert "原文未点明性别" in leaked, (
        "拼接层不过滤也不该过滤——它无从判断哪句是说明、哪句是描述。"
        "唯一能守住的地方是写入这个字段的时候。"
    )


def test_portrait_prompt_uses_normalization() -> None:
    text = portrait_prompt("国风水墨清透光影细腻晕染", "黑发少年。。玄色劲装，目光坚定，身形修长腰佩玉佩")
    assert "。。" not in text
    assert "国风水墨" in text


def test_bible_does_not_reject_appearance_by_word_list() -> None:
    bible = Bible(
        characters=[Character(
            name="角色甲",
            role="主角",
            appearance_canonical=(
                "24岁女性，黑色长发，白色衬衫配深色半身裙，"
                "身形修长，标志性特征是粉色乳头"
            ),
        )],
        world=World(visual_style_canonical="3D动漫CG渲染，虚构数字角色，电影光影"),
    )

    assert validate_bible(bible) == []


def test_bible_does_not_reject_subjective_appearance_by_word_list() -> None:
    bible = Bible(
        characters=[Character(
            name="角色甲",
            role="反派",
            appearance_canonical=(
                "四十岁男性，短发，深色正装配白衬衫，身材微胖，"
                "标志性特征是眼神猥琐，看人总带着色欲和算计感"
            ),
        )],
        world=World(visual_style_canonical="3D动漫CG渲染，虚构数字角色，电影光影"),
    )

    assert validate_bible(bible) == []


def test_bible_accepts_compact_complete_production_identity() -> None:
    bible = Bible(
        characters=[Character(
            name="角色甲",
            role="反派",
            appearance_canonical="三十岁男性，短发身材高大，常穿深色正装配黑皮鞋",
        )],
        world=World(visual_style_canonical="3D动漫CG渲染，虚构数字角色，电影光影"),
    )

    assert validate_bible(bible) == []


def test_bible_allows_empty_character_roster() -> None:
    """架构转向（2026-08-31）回归锁：首版人物谱只判定世界观，characters=[]
    是新主路径（app.stages.generate_bible）的正常产出，不再是「characters
    数量 0，要求至少 1 个」的失败信号。人物改为映射台按需提名/分集反应式
    建卡，不在这里强制要求非空。"""
    bible = Bible(characters=[], world=World(visual_style_canonical="3D动漫CG渲染，虚构数字角色，电影光影"))

    assert validate_bible(bible) == []


def test_project_or_404_normalizes_sqlite_row_to_dict(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        # deleted_at 必须有：`_project_or_404` 自软删除/回收站落地起会查
        # `AND deleted_at IS NULL`，合成表缺这一列会直接 OperationalError。
        # owner_user_id 同理：账号即项目空间落地后 `_project_or_404` 会读它做
        # 归属校验（`_assert_principal_owns`），合成表缺这一列会直接 IndexError。
        # 本用例验的是 sqlite Row → dict 的归一化，与这两者都无关，补列即可。
        "CREATE TABLE projects("
        "id TEXT PRIMARY KEY, bible_status TEXT, bible_error TEXT, bible_json TEXT, "
        "bible_version INTEGER DEFAULT 0, deleted_at REAL, owner_user_id TEXT"
        ")"
    )
    conn.execute(
        "INSERT INTO projects(id, bible_status, bible_json, bible_version) VALUES('p1', 'idle', NULL, 0)"
    )
    conn.commit()
    monkeypatch.setattr(common, "get_conn", lambda: conn)

    project = common._project_or_404("p1")

    assert isinstance(project, dict)
    assert project.get("bible_json") is None
    assert project["bible_version"] == 0


def test_generate_precheck_estimates_without_bible(monkeypatch) -> None:
    """架构转向（2026-08-31）回归锁：首次生成只判定世界观（不点名角色），
    真实成本是 0——不能再按「粗估 20 角色」报一个 12 元的假价格（协调方
    2026-08-31 打回：就钱的事情给用户假数字，比文案不准严重）。替换同名旧
    用例（旧用例断言 20 角色/12 元，验的是已退场的点名规模粗估）。"""
    from app.domain import bible_ops
    import asyncio

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER DEFAULT 0)"
    )
    conn.execute("INSERT INTO projects(id, bible_json, bible_version) VALUES('p1', NULL, 0)")
    conn.commit()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "_project_or_404", lambda _pid: dict(conn.execute(
        "SELECT * FROM projects WHERE id='p1'"
    ).fetchone()))

    result = asyncio.run(bible_ops.bible_generate_precheck("p1"))
    assert result["character_count"] == 0
    assert result["image_count"] == 0
    assert result["style_name"] == "国漫电影风"
    assert "不产生角色或图片" in result["estimate_note"]


def test_visual_style_options_expose_photographic_flag(monkeypatch) -> None:
    """``photographic`` 加入响应体（2026-08-31）：导入面板据此对摄影类画风给出
    "视频阶段易被隐私政策拒收"的提示——判据是既有的
    ``VisualStylePreset.photographic``字段，不是前端另建一份画风名单。"""
    from app.domain import bible_ops
    import asyncio

    patch_api_everywhere(monkeypatch, "_project_or_404", lambda _pid: {"id": "p1"})

    result = asyncio.run(bible_ops.bible_visual_styles("p1"))

    assert result["default"] == "国漫电影风"
    assert any(
        item["name"] == "真人摄影风" and "真人" in item["description"]
        for item in result["items"]
    )
    assert all(
        set(item) == {"name", "description", "sample_image", "photographic"}
        for item in result["items"]
    )
    assert all(item["sample_image"] for item in result["items"])
    photographic_names = {item["name"] for item in result["items"] if item["photographic"]}
    non_photographic_names = {item["name"] for item in result["items"] if not item["photographic"]}
    assert photographic_names == {"真人摄影风", "精修真人风"}
    assert non_photographic_names == {"国漫电影风", "古典水墨风"}


def test_bible_generate_precheck_binds_style_name(monkeypatch) -> None:
    from app.domain import bible_ops

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER DEFAULT 0)"
    )
    conn.execute("INSERT INTO projects(id, bible_json, bible_version) VALUES('p1', NULL, 0)")
    conn.commit()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "_project_or_404", lambda _pid: dict(conn.execute(
        "SELECT * FROM projects WHERE id='p1'"
    ).fetchone()))

    quote = bible_ops._compute_bible_generate_precheck("p1", style_name="真人摄影风")

    assert quote["style_name"] == "真人摄影风"
    # 未签发的原始范围指纹不叫 quote_id（那要经 _issue_scope_quote 落库
    # 才产生，否则调用方拿它去确认会落进 QUOTE_STALE）。
    assert "quote_id" not in quote
    assert quote["scope_fingerprint"] == fingerprint({
        "project_id": "p1",
        "action": "generate_bible_and_refs",
        "character_count": 0,
        "image_count": 0,
        "bible_version": 0,
        "style_name": "真人摄影风",
    })


def test_bible_generate_precheck_prices_existing_characters_only_when_style_changes(monkeypatch) -> None:
    """回归锁：已有人物谱时，报价必须与 _bible_task 的真实触发条件同一份口径——
    请求的画风与当前画风相同就是 0 费用（世界观判定不改动角色，不触发定妆
    重生成），画风真的不同才按现有角色数计价（那才会真的触发定妆重生成）。"""
    from app.domain import bible_ops
    from app.schemas import Bible, Character, World

    from app.domain.bible_ops.primitives import _visual_style_prompt_or_default as _style_prompt
    bible = Bible(
        # 用真实的「国漫电影风」prompt 文本（不是随手写的自定义字符串），才能
        # 让下面「请求同一个 style_name」真正命中"未变化"分支——这条测试要验
        # 的正是这次判据与文本比较，不是随便什么字符串都相等。
        world=World(visual_style_canonical=_style_prompt("国漫电影风")),
        characters=[
            Character(name="甲一", role="主角", appearance_canonical="十五岁少年，黑发束起，黑色劲装，眉眼倔强坚毅"),
            Character(name="乙二", role="重要配角", appearance_canonical="四十岁男性，短发，深色正装配白衬衫，身材微胖"),
        ],
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO projects(id, bible_json, bible_version) VALUES('p1', ?, 3)",
        (bible.model_dump_json(),),
    )
    conn.commit()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "_project_or_404", lambda _pid: dict(conn.execute(
        "SELECT * FROM projects WHERE id='p1'"
    ).fetchone()))

    unchanged = bible_ops._compute_bible_generate_precheck("p1", style_name="国漫电影风")
    assert unchanged["character_count"] == 0
    assert unchanged["image_count"] == 0

    changed = bible_ops._compute_bible_generate_precheck("p1", style_name="真人摄影风")
    assert changed["character_count"] == 2
    assert changed["character_names"] == ["甲一", "乙二"]
    assert changed["image_count"] > 0


def test_generate_bible_forces_backend_visual_style_prompt(monkeypatch) -> None:
    """架构转向（2026-08-31 二次拍板）：generate_bible 不再发起任何模型调用——
    画风已经由用户在导入面板选定，visual_style_prompt 就是选定结果本身，
    原样写进 world，不问模型。没有 previous_bible（首次生成）时 era/genre
    留空、characters 恒为空。替换同名旧用例（旧用例走一次「轻量模型调用判定
    era/genre」的中间方案，那条方案本身已被推翻——它在真实项目上把用户拦在
    HiAgent 内容审核后面）。"""
    from app import stages
    from app.harness import model_gateway
    import asyncio

    calls = {"count": 0}

    async def fake_chat_structured(*_args, **_kwargs):  # pragma: no cover - 不该被调用
        calls["count"] += 1
        raise AssertionError("generate_bible 不应再发起任何模型调用")

    monkeypatch.setattr(model_gateway, "chat_structured", fake_chat_structured)

    result = asyncio.run(stages.generate_bible(
        [{"idx": 1, "title": "第一章", "content": "孟浩走入山中。"}],
        visual_style_prompt="电影级真实质感，现实人物建模，自然光影，细节丰富，东方仙侠风。",
    ))

    assert calls["count"] == 0, "画风已由用户选定，不该再问模型"
    assert result.world.visual_style_canonical == "电影级真实质感，现实人物建模，自然光影，细节丰富，东方仙侠风。"
    assert result.world.era == ""
    assert result.world.genre == ""
    assert result.characters == []


def test_generate_bible_carries_forward_existing_characters_and_scenes_unchanged(monkeypatch) -> None:
    """回归锁（协调方 2026-08-31 打回，二次拍板后依然成立）：换画风绝不能清空
    已积累的角色/场景卡，也不该动 era/genre——这次调用压根没有重新判断过它们。
    新架构下角色卡/场景卡是随分集由映射台提名或分镜展开前反应式建卡
    （ensure_character_card / assess_new_scene）陆续积累出来的，不是靠这个
    「重新生成人物谱」按钮点名出来的——早期实现 `characters=[]` 是把"首次生成
    没有候选可点名"和"重新判定世界观"两种情况错误合并成同一种「清空」处理，
    会把用户攒了几十集的角色卡和场景卡随手一个按钮清零。这里钉住：传入
    previous_bible 时，返回的 Bible.characters/scenes 与旧数据逐字段一致，
    world.era/genre 原样带出，只有 visual_style_canonical 换成新选定的画风——
    防止将来又被改回覆盖，也防止「不问模型」被悄悄改回「问模型」。"""
    from app import stages
    from app.harness import model_gateway
    import asyncio

    previous_bible = {
        "world": {"era": "现代都市", "genre": "都市异能", "visual_style_canonical": "旧画风描述占位十五字以上凑数"},
        "characters": [
            {
                "name": "甲一", "role": "主角",
                "appearance_canonical": "十五岁少年，黑发束起，黑色劲装，眉眼倔强坚毅",
                "ref_image_path": "/media/refs/jia_yi.png",
            },
            {
                "name": "乙二", "role": "重要配角",
                "appearance_canonical": "四十岁男性，短发，深色正装配白衬衫，身材微胖",
            },
        ],
        "scenes": [
            {"name": "甲家测验广场", "scene_canonical": "青石广场，日光正盛，围观人群环绕，测验碑立于中央",
             "ref_image_path": "/media/refs/scene_plaza.png"},
        ],
    }

    async def fake_chat_structured(*_args, **_kwargs):  # pragma: no cover - 不该被调用
        raise AssertionError("换画风不该再发起任何模型调用")

    monkeypatch.setattr(model_gateway, "chat_structured", fake_chat_structured)

    result = asyncio.run(stages.generate_bible(
        [{"idx": 1, "title": "第一章", "content": "甲一走入山中。"}],
        previous_bible=previous_bible,
        visual_style_prompt="全新画风描述也凑够十五个字以上",
    ))

    assert result.world.era == "现代都市"
    assert result.world.genre == "都市异能"
    assert result.world.visual_style_canonical == "全新画风描述也凑够十五个字以上"
    assert [c.name for c in result.characters] == ["甲一", "乙二"]
    assert result.characters[0].appearance_canonical == "十五岁少年，黑发束起，黑色劲装，眉眼倔强坚毅"
    assert result.characters[0].ref_image_path == "/media/refs/jia_yi.png"
    assert [s.name for s in result.scenes] == ["甲家测验广场"]
    assert result.scenes[0].ref_image_path == "/media/refs/scene_plaza.png"


def test_purge_for_style_change_clears_both_character_and_scene_refs(monkeypatch) -> None:
    """协调方 2026-08-31 要求复核：generate_bible 现在原样带出 previous_bible
    的 characters/scenes（不再恒为空），_purge_for_style_change 的场景清理分支
    （for sc in instance.scenes: ...）之前因为 instance.scenes 恒为 [] 一直是
    死代码，从未真正清理过场景定妆图；这里钉住它现在确实按角色与场景两条腿
    都清理：ref_image_path 清空、character_portraits/scene_references 落库
    行删除、refs_status/scene_refs_status 回到 idle。"""
    from app.domain import bible_ops
    from app.schemas import Bible, Character, Scene, World
    import tempfile
    from pathlib import Path

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects(id TEXT PRIMARY KEY, refs_status TEXT, scene_refs_status TEXT)"
    )
    conn.execute("CREATE TABLE character_portraits(project_id TEXT, character_name TEXT)")
    conn.execute("CREATE TABLE scene_references(project_id TEXT, scene_name TEXT)")
    conn.execute(
        "INSERT INTO projects(id, refs_status, scene_refs_status) VALUES('p1', 'ready', 'ready')"
    )
    conn.execute("INSERT INTO character_portraits VALUES('p1', '甲一')")
    conn.execute("INSERT INTO scene_references VALUES('p1', '甲家广场')")
    conn.commit()

    tmp_dir = tempfile.mkdtemp()
    char_img = Path(tmp_dir) / "char.png"
    scene_img = Path(tmp_dir) / "scene.png"
    char_img.write_bytes(b"x")
    scene_img.write_bytes(b"x")

    # 必须走 patch_worker_everywhere：app.media_exec 已经是真包，每个子模块各持
    # 一份自己的绑定，裸 monkeypatch.setattr(worker, ...) 只改到 app.worker 的
    # 再导出，真实调用点（app/media_exec/common.py 等）看不见，测试会照常变绿
    # 而被验证的代码路径从未被替换。tests/test_worker_monkeypatch_guard.py 守着这条。
    patch_worker_everywhere(monkeypatch, "purge_project_video_artifacts", lambda _pid: {"purged_videos": 0})
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)

    bible = Bible(
        world=World(visual_style_canonical="赛博朋克霓虹质感，虚构数字角色，高对比光影"),
        characters=[Character(
            name="甲一", role="主角", appearance_canonical="十五岁少年，黑发束起，黑色劲装，眉眼倔强坚毅",
            ref_image_path=str(char_img),
        )],
        scenes=[Scene(
            name="甲家广场", scene_canonical="青石广场，日光正盛，围观人群环绕，测验碑立于中央",
            ref_image_path=str(scene_img),
        )],
    )

    result = bible_ops._purge_for_style_change("p1", bible)

    assert result["refs_cleared"] == 1
    assert result["scene_refs_cleared"] == 1
    assert bible.characters[0].ref_image_path is None
    assert bible.scenes[0].ref_image_path is None
    assert not char_img.exists()
    assert not scene_img.exists()
    assert conn.execute("SELECT COUNT(*) c FROM character_portraits").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM scene_references").fetchone()["c"] == 0
    row = conn.execute("SELECT refs_status, scene_refs_status FROM projects WHERE id='p1'").fetchone()
    assert row["refs_status"] == "idle"
    assert row["scene_refs_status"] == "idle"


def test_bible_source_keeps_first_ten_chapters_complete() -> None:
    """长章小说不能只读到第三四章：前 10 章必须整章进入首版人物谱输入。"""
    from app import stages

    chapters = [
        {"idx": i, "title": f"第{i}章", "content": f"第{i}章开头。" + "文" * 5000 + f"第{i}章结尾。"}
        for i in range(1, 40)
    ]

    proportional = stages._render_bible_source(chapters)
    guaranteed = stages._render_bible_source(chapters, head_chapters=10)

    assert "第9章结尾。" not in proportional
    for i in range(1, 11):
        assert f"第{i}章开头。" in guaranteed
        assert f"第{i}章结尾。" in guaranteed


def test_generate_bible_never_calls_any_model(monkeypatch) -> None:
    """架构转向（2026-08-31 二次拍板）：generate_bible 不再判定世界观、不再点名
    角色——不发起任何模型调用。上一版方案是「发一次轻量模型调用判定
    era/genre/画风」，这个方案本身被推翻：画风已经由用户在导入面板选定，问
    模型是多余的，而这次多余调用在真实项目（《我欲封天》）上直接触发 HiAgent
    内容审核（``finish_reason=content_filter``），把用户拦在
    ``bible_status=failed`` 且没有出路。这条测试用打桩计数钉住「调用次数恒为
    0」，同时保留「characters 恒为 []」的回归锁。

    原用例还额外打桩旧点名管线入口函数断言它不被调用；该管线已于 2026-09-01
    整体退场删除（生产零调用方），这里不再需要打桩一个已经不存在的函数——
    它不存在这件事本身就是最强的"不会被调用"证明。
    """
    import asyncio
    from app import stages
    from app.harness import model_gateway

    model_calls = {"count": 0}

    async def fake_chat_structured(*_args, **_kwargs):  # pragma: no cover - 不该被调用
        model_calls["count"] += 1
        raise AssertionError("generate_bible 不应再发起任何模型调用")

    monkeypatch.setattr(model_gateway, "chat_structured", fake_chat_structured)

    bible = asyncio.run(stages.generate_bible(
        [{"idx": 1, "title": "第一章", "content": "小胖子与孟浩同行。"}],
        visual_style_prompt="国漫3D动画电影质感，精致光影，统一电影画面",
    ))

    assert bible.characters == []
    assert bible.world.visual_style_canonical == "国漫3D动画电影质感，精致光影，统一电影画面"
    assert model_calls["count"] == 0, "generate_bible 不该再发起任何模型调用"


