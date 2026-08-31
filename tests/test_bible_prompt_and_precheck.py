"""提示词规范化与人物谱扩展预检。"""
from __future__ import annotations

import sqlite3

import pytest

from app.domain import common
from app.refs import normalize_prompt_text, portrait_prompt
from app.orchestration.engine import fingerprint
from app.schemas import Bible, Character, World
from app.validators import validate_bible
from tests.conftest import patch_stages_everywhere as _patch_stages
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


def test_appearance_field_rule_never_asks_for_meta_commentary() -> None:
    """人物谱详情提示词不得再指示模型把「原文未点明性别」写进外观字段。

    旧版规则明写「证据包里确实看不出性别时，写"原文未点明性别"」。不许按名字
    或常识猜性别这个内核是对的，猜出来的是编造；错的是产出位置——那句话是写给
    人看的元话语，却被逐字拼进图像 prompt。

    这条测试守两件事：元话语的指示没有回来，以及"不许猜性别"这个内核还在
    （不能靠让模型瞎猜来消灭元话语）。
    """
    from app.stages import BIBLE_APPEARANCE_FIELD_RULE as rule

    assert "原文未点明" not in rule
    assert "证据不足" not in rule
    assert "猜" in rule, "不许按名字或常识猜性别，这个内核不能连同元话语一起被删掉"
    assert "图像" in rule, "要让模型知道这个字段的读者是图像模型，规则才立得住"


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
    assert result["estimated_cost_cny"] == 0.0
    assert result["style_name"] == "国漫电影风"
    assert "无费用" in result["estimate_note"]


def test_visual_style_options_expose_names_and_descriptions_only(monkeypatch) -> None:
    from app.domain import bible_ops
    import asyncio

    patch_api_everywhere(monkeypatch, "_project_or_404", lambda _pid: {"id": "p1"})

    result = asyncio.run(bible_ops.bible_visual_styles("p1"))

    assert result["default"] == "国漫电影风"
    assert any(
        item["name"] == "真人摄影风" and "真人" in item["description"]
        for item in result["items"]
    )
    assert all(set(item) == {"name", "description", "sample_image"} for item in result["items"])
    assert all(item["sample_image"] for item in result["items"])


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
    # 未签发的原始范围指纹不叫 quote_id（那要经 _issue_payment_quote 落库
    # 才产生，否则调用方拿它去确认会落进 QUOTE_STALE）。
    assert "quote_id" not in quote
    assert quote["scope_fingerprint"] == fingerprint({
        "project_id": "p1",
        "action": "generate_bible_and_refs",
        "character_count": 0,
        "image_count": 0,
        "unit": 0.2,
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
    assert unchanged["estimated_cost_cny"] == 0.0

    changed = bible_ops._compute_bible_generate_precheck("p1", style_name="真人摄影风")
    assert changed["character_count"] == 2
    assert changed["character_names"] == ["甲一", "乙二"]
    assert changed["estimated_cost_cny"] > 0


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


def test_recurring_character_names_ranks_by_verified_onstage_evidence(monkeypatch) -> None:
    """点名之后由结构闸 + 独立裁决闸核验：只有真的证明本人在场、且核验通过的证据
    才计数（verified_onstage_count），不再是名字字符串出现次数。编造的引句（结构闸
    挡下）、只出现一次的候选（低于阈值）都不该进必收名单。"""
    import asyncio
    import json

    from app import stages
    from app.harness import model_gateway

    chapters = [
        {"idx": 1, "title": "第一章", "content": "孟浩与王有材同行，孟浩开口说话。"},
        {"idx": 2, "title": "第二章", "content": "孟浩再遇王有材，路人甲一闪而过。"},
    ] + [
        {"idx": i, "title": f"第{i}章", "content": f"第{i}章：孟浩独行，王有材追来。"}
        for i in range(3, 21)
    ]

    async def fake_chat(_messages, **_kwargs):
        return json.dumps({
            "candidates": [
                {
                    "primary_appellation": "孟浩", "formal_name": "",
                    "onstage_evidence": [
                        {"chapter_index": 1, "quote": "孟浩与王有材同行，孟浩开口说话。"},
                        {"chapter_index": 2, "quote": "孟浩再遇王有材，路人甲一闪而过。"},
                    ],
                },
                {
                    # 只有一条通过结构闸+裁决闸的证据：低于阈值 2，不该进名单。
                    "primary_appellation": "王有材", "formal_name": "",
                    "onstage_evidence": [
                        {"chapter_index": 1, "quote": "孟浩与王有材同行，孟浩开口说话。"},
                    ],
                },
                {
                    # 编造出来的引句：结构闸 G2（逐字命中原文）会挡下，0 条通过。
                    "primary_appellation": "孟浩然", "formal_name": "",
                    "onstage_evidence": [
                        {"chapter_index": 1, "quote": "孟浩然从天而降大喝一声。"},
                    ],
                },
                {
                    "primary_appellation": "路人甲", "formal_name": "",
                    "onstage_evidence": [
                        {"chapter_index": 2, "quote": "孟浩再遇王有材，路人甲一闪而过。"},
                    ],
                },
            ],
        }, ensure_ascii=False)

    async def fake_chat_structured(_messages, **kwargs):
        model_type = kwargs["model_type"]
        return model_type(verdict="onstage", supporting_segment_index=1)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    monkeypatch.setattr(model_gateway, "chat_structured", fake_chat_structured)
    ranked = asyncio.run(stages._recurring_character_names(chapters))

    assert ranked == [("孟浩", "", 2, 21, 20, [])]


def test_onstage_evidence_stuck_in_one_chapter_is_not_a_recurring_character(monkeypatch) -> None:
    """在场证据全挤在同一章的候选不进名单——本函数选的是「复现」人物。

    真实故障：「绿袍男子」是靠山宗那批绿袍修士的类别称谓（靠衣着指人，换个场合就
    指别人），不是谁的专名。它在第 2 章里连出三条在场证据，条数够了通道 A，就建了
    正式角色卡；随后被映射器裸命中，把整集映射卡死在「称谓未逐字出现在本集原文」
    的反幻觉闸上。光数条数分不出「跨章反复登场」和「在某一章里连说三句话」。
    """
    import asyncio
    import json

    from app import stages
    from app.harness import model_gateway

    chapters = [
        {"idx": 1, "title": "第一章", "content": "孟浩独自上山。"},
        {
            "idx": 2, "title": "第二章",
            "content": (
                "绿袍男子一脸不耐，说完转身离去。"
                "走在前方的绿袍男子传来冷漠的声音。"
                "他站起身恭恭敬敬的向着绿袍男子抱拳一拜。"
                "孟浩跟在后面，孟浩低头不语。"
            ),
        },
    ] + [
        {"idx": i, "title": f"第{i}章", "content": f"第{i}章：孟浩继续赶路。"}
        for i in range(3, 21)
    ]

    async def fake_chat(_messages, **_kwargs):
        return json.dumps({
            "candidates": [
                {
                    # 三条在场证据，但全部落在第 2 章：不是复现人物。
                    "primary_appellation": "绿袍男子", "formal_name": "",
                    "onstage_evidence": [
                        {"chapter_index": 2, "quote": "绿袍男子一脸不耐，说完转身离去。"},
                        {"chapter_index": 2, "quote": "走在前方的绿袍男子传来冷漠的声音。"},
                        {"chapter_index": 2, "quote": "他站起身恭恭敬敬的向着绿袍男子抱拳一拜。"},
                    ],
                },
                {
                    # 两条证据跨到两章：这才是复现人物。
                    "primary_appellation": "孟浩", "formal_name": "",
                    "onstage_evidence": [
                        {"chapter_index": 1, "quote": "孟浩独自上山。"},
                        {"chapter_index": 2, "quote": "孟浩跟在后面，孟浩低头不语。"},
                    ],
                },
            ],
        }, ensure_ascii=False)

    async def fake_chat_structured(_messages, **kwargs):
        model_type = kwargs["model_type"]
        return model_type(verdict="onstage", supporting_segment_index=1)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    monkeypatch.setattr(model_gateway, "chat_structured", fake_chat_structured)
    ranked = asyncio.run(stages._recurring_character_names(chapters))

    admitted = [item[0] for item in ranked]
    assert "绿袍男子" not in admitted, "在场证据不跨章的候选不该建卡"
    assert "孟浩" in admitted, "跨章复现的角色必须留下"


def test_admission_relaxes_chapter_floor_for_personal_name_not_referential(monkeypatch) -> None:
    """4a：跨章要求按 name_form 分档。真实故障挡的是「绿袍男子」这类靠衣着指人
    的类别称谓，不是「只出现一章」本身——这个区分已经由资格裁决模型判进
    name_form。姓名形态的候选，证据全挤在同一章也该入选；代称形态仍要求跨 2
    章，用同一份数据（两个称呼提及次数接近）显式验证是 name_form 在把关，不是
    机缘巧合。
    """
    import asyncio
    import json

    from app import stages
    from app.harness import model_gateway

    chapter_one = (
        "陈默大步走出，陈默转身冷笑。"
        "青衣客一言不发，青衣客缓缓后退，青衣客又转身望天。"
    )
    chapters = [{"idx": 1, "title": "第一章", "content": chapter_one}] + [
        {"idx": i, "title": f"第{i}章", "content": f"第{i}章：正文占位。"}
        for i in range(2, 21)
    ]

    async def fake_chat(_messages, **_kwargs):
        return json.dumps({
            "candidates": [
                {
                    "primary_appellation": "陈默", "formal_name": "",
                    "onstage_evidence": [
                        {"chapter_index": 1, "quote": "陈默大步走出，陈默转身冷笑。"},
                        {"chapter_index": 1, "quote": "陈默转身冷笑。"},
                    ],
                },
                {
                    "primary_appellation": "青衣客", "formal_name": "",
                    "onstage_evidence": [
                        {"chapter_index": 1, "quote": "青衣客一言不发，青衣客缓缓后退，青衣客又转身望天。"},
                        {"chapter_index": 1, "quote": "青衣客缓缓后退，青衣客又转身望天。"},
                    ],
                },
            ],
        }, ensure_ascii=False)

    async def fake_chat_structured(_messages, **kwargs):
        model_type = kwargs["model_type"]
        meta = kwargs.get("call_meta") or {}
        if model_type is stages._RosterPersonhoodResolution:
            name_form = "personal_name" if meta.get("character_name") == "陈默" else "referential"
            return model_type(verdict="person", supporting_chapter_index=1, name_form=name_form)
        if model_type is stages._RosterTrueNameResolution:
            return model_type(verdict="unrevealed")
        return model_type(verdict="onstage", supporting_segment_index=1)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    monkeypatch.setattr(model_gateway, "chat_structured", fake_chat_structured)
    ranked = asyncio.run(stages._recurring_character_names(chapters))

    admitted = [item[0] for item in ranked]
    assert "陈默" in admitted, "姓名形态的候选跨章门槛降到 1 章后应当入选"
    assert "青衣客" not in admitted, "代称形态仍要求跨 2 章，门槛放松不能一起放行"


def test_roll_call_coverage_log_reports_absolute_failed_chunks(monkeypatch) -> None:
    """点名分块失败必须以绝对数 + 失败章号入账，不能只留比例：「20/60」和
    「6/20」比例接近，绝对损失差三倍多，只记比例看不出这个差异。"""
    import asyncio
    import json

    from app import stages
    from app.harness import model_gateway
    from tests.conftest import patch_stages_everywhere as patch_stages

    async def fake_chat(_messages, **kwargs):
        meta = kwargs.get("call_meta") or {}
        if meta.get("chunk_index") == 2:
            raise RuntimeError("provider unavailable")
        return json.dumps({"candidates": []})

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    calls: list[tuple] = []

    def fake_log(kind, *args, **kwargs):
        calls.append((kind, kwargs.get("meta") or {}))

    patch_stages(monkeypatch, "log_provider_call", fake_log)
    patch_stages(monkeypatch, "BIBLE_ROLL_CALL_MAX_ATTEMPTS", 1)

    chapters = [{"idx": i, "title": f"第{i}章", "content": "正文" * 50} for i in range(1, 6)]
    result = asyncio.run(stages._recurring_character_names(chapters))

    assert result == []
    coverage = next(meta for kind, meta in calls if kind == "character_roll_call_coverage")
    assert coverage["failed_chunk_count"] == 1
    assert coverage["total_chunk_count"] == 5
    assert coverage["failed_chapters"] == [3]


def test_roster_runaway_guard_rejects_absurd_candidate_counts(monkeypatch) -> None:
    """失控护栏：候选数超过上限时响亮报错，不是静默截断——区别于被拆掉的
    ranked[:20]（静默吃人），这个是停下来报警。"""
    import asyncio
    import json

    from app import stages
    from app.harness import model_gateway
    from tests.conftest import patch_stages_everywhere as patch_stages

    chapters = [
        {"idx": i, "title": f"第{i}章", "content": f"第{i}章：甲现身，乙现身，丙现身。"}
        for i in range(1, 21)
    ]

    def _evidence() -> list[dict]:
        return [
            {"chapter_index": 1, "quote": "第1章：甲现身，乙现身，丙现身。"},
            {"chapter_index": 2, "quote": "第2章：甲现身，乙现身，丙现身。"},
        ]

    async def fake_chat(_messages, **_kwargs):
        return json.dumps({
            "candidates": [
                {"primary_appellation": "甲", "formal_name": "", "onstage_evidence": _evidence()},
                {"primary_appellation": "乙", "formal_name": "", "onstage_evidence": _evidence()},
                {"primary_appellation": "丙", "formal_name": "", "onstage_evidence": _evidence()},
            ],
        }, ensure_ascii=False)

    async def fake_chat_structured(_messages, **kwargs):
        model_type = kwargs["model_type"]
        return model_type(verdict="onstage", supporting_segment_index=1)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    monkeypatch.setattr(model_gateway, "chat_structured", fake_chat_structured)
    patch_stages(monkeypatch, "BIBLE_ROSTER_RUNAWAY_MAX", 2)

    with pytest.raises(stages.StageError) as exc_info:
        asyncio.run(stages._recurring_character_names(chapters))
    assert "候选" in str(exc_info.value)
    assert "200" not in str(exc_info.value)  # 断言用的是被打过补丁的上限，不是硬编码 200


def test_single_chapter_corpus_still_produces_a_verified_roster(monkeypatch) -> None:
    """短篇被切成一章时，跨章门槛必须按语料实际章数封顶，否则名单结构上必空。

    真实故障（proj_177d147e16c7《王六郎》，2944 字自动切分成 1 章）：3 个候选、
    7 条申报证据里有 4 条同时通过结构闸与在场裁决闸，必收名单仍是 0 条，人物谱
    以「人物点名未产出任何经原文核验的角色候选」整体失败。通道 A 要证据跨 2 章、
    通道 C 要命中覆盖 2 章，而全书只有 1 章——判据挂在了语料被切成几章上，不挂在
    「这个人是不是反复登场」上，重试多少次都必然复现。
    """
    import asyncio
    import json

    from app import stages
    from app.harness import model_gateway

    chapters = [{
        "idx": 1,
        "title": "第1段（自动切分）",
        "content": (
            "有个姓许的人，家住淄川城北郊，以捕鱼为业。一天夜里，许某正在独自饮酒。"
            "王六郎走来，在他身旁徘徊。\n\n"
            "许某让他来喝，慷慨地与他一同对饮。许某举网一捞，捕到了好几条鱼。"
            "王六郎替许某把鱼赶来。"
        ),
    }]

    async def fake_chat(_messages, **_kwargs):
        return json.dumps({
            "candidates": [
                {
                    "primary_appellation": "许某", "formal_name": "",
                    "onstage_evidence": [
                        {"chapter_index": 1, "quote": "一天夜里，许某正在独自饮酒。"},
                        {"chapter_index": 1, "quote": "许某让他来喝，慷慨地与他一同对饮。"},
                    ],
                },
                {
                    "primary_appellation": "王六郎", "formal_name": "",
                    "onstage_evidence": [
                        {"chapter_index": 1, "quote": "王六郎走来，在他身旁徘徊"},
                        {"chapter_index": 1, "quote": "王六郎替许某把鱼赶来。"},
                    ],
                },
            ],
        }, ensure_ascii=False)

    async def fake_chat_structured(_messages, **kwargs):
        model_type = kwargs["model_type"]
        return model_type(verdict="onstage", supporting_segment_index=1)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    monkeypatch.setattr(model_gateway, "chat_structured", fake_chat_structured)

    # 修复前的判据：门槛不按语料封顶，写死要跨 2 章。用独立副本跑红，不回退线上代码。
    with monkeypatch.context() as legacy:
        _patch_stages(
            legacy, "_corpus_scoped_chapter_threshold",
            lambda threshold, available_chapters: threshold,
        )
        assert asyncio.run(stages._recurring_character_names(chapters)) == [], (
            "这条断言描述的是修复前的行为：单章语料必然产出空名单"
        )

    admitted = [item[0] for item in asyncio.run(stages._recurring_character_names(chapters))]
    assert admitted == ["王六郎", "许某"] or admitted == ["许某", "王六郎"], admitted


def test_corpus_scoped_chapter_threshold_only_relaxes_short_corpora() -> None:
    """封顶只在语料章数不够时生效；章数够的语料上原门槛一字不改。"""
    from app import stages

    assert stages._corpus_scoped_chapter_threshold(2, 1) == 1
    assert stages._corpus_scoped_chapter_threshold(2, 2) == 2
    assert stages._corpus_scoped_chapter_threshold(2, 20) == 2
    assert stages._corpus_scoped_chapter_threshold(3, 1616) == 3
    # 章数为 0（语料为空）不该退化成 0 门槛：至少要有一章被钉证。
    assert stages._corpus_scoped_chapter_threshold(2, 0) == 1


def test_recurring_character_names_structural_gate_rejects_each_failure_mode(monkeypatch) -> None:
    """结构闸 G1-G3 逐条独立核验：引句不是原文逐字子串（G2）、称呼不在引句里（G3）、
    章节号落在统计窗口之外（G1）——任一不满足直接丢弃该条证据，不发起裁决调用
    （省模型调用，也是"不确定不登记"的第一道闸）。"""
    import asyncio
    import json

    from app import stages
    from app.harness import model_gateway

    chapters = [
        {"idx": i, "title": f"第{i}章", "content": f"第{i}章：真实原文占位。"}
        for i in range(1, 21)
    ]

    async def fake_chat(_messages, **_kwargs):
        return json.dumps({
            "candidates": [
                {
                    # G2：quote 不是该章原文的逐字子串（编造的引句）。
                    "primary_appellation": "甲", "formal_name": "",
                    "onstage_evidence": [{"chapter_index": 1, "quote": "甲从天而降。"}],
                },
                {
                    # G3：appellation 既不在 quote 里也不是其子串。
                    "primary_appellation": "乙", "formal_name": "",
                    "onstage_evidence": [{"chapter_index": 2, "quote": "第2章：真实原文占位。"}],
                },
                {
                    # G1：chapter_index 落在统计窗口（1~20）之外。
                    "primary_appellation": "丙", "formal_name": "",
                    "onstage_evidence": [{"chapter_index": 99, "quote": "丙的引句。"}],
                },
            ],
        }, ensure_ascii=False)

    verdict_calls: list[str] = []

    async def fake_chat_structured(_messages, **kwargs):
        verdict_calls.append((kwargs.get("call_meta") or {}).get("appellation", ""))
        model_type = kwargs["model_type"]
        return model_type(verdict="onstage", supporting_segment_index=1)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    monkeypatch.setattr(model_gateway, "chat_structured", fake_chat_structured)
    ranked = asyncio.run(stages._recurring_character_names(chapters))

    assert ranked == []
    assert verdict_calls == []


def test_recurring_character_names_presence_verdict_gate(monkeypatch) -> None:
    """结构闸通过后仍要过独立低温裁决闸：verdict=="mentioned_only" 不计入；
    verdict=="onstage" 但段号钉证失败（模型编造卷宗外的段号）不计入；
    verdict=="onstage" 且钉证通过才计入 verified_onstage_count。"""
    import asyncio
    import json

    from app import stages
    from app.harness import model_gateway

    chapters = [
        {"idx": i, "title": f"第{i}章", "content": f"第{i}章：甲现身，乙现身，丙现身。"}
        for i in range(1, 21)
    ]

    def _evidence() -> list[dict]:
        return [
            {"chapter_index": 1, "quote": "第1章：甲现身，乙现身，丙现身。"},
            {"chapter_index": 2, "quote": "第2章：甲现身，乙现身，丙现身。"},
        ]

    async def fake_chat(_messages, **_kwargs):
        return json.dumps({
            "candidates": [
                {"primary_appellation": "甲", "formal_name": "", "onstage_evidence": _evidence()},
                {"primary_appellation": "乙", "formal_name": "", "onstage_evidence": _evidence()},
                {"primary_appellation": "丙", "formal_name": "", "onstage_evidence": _evidence()},
            ],
        }, ensure_ascii=False)

    async def fake_chat_structured(_messages, **kwargs):
        appellation = (kwargs.get("call_meta") or {}).get("appellation", "")
        model_type = kwargs["model_type"]
        if appellation == "甲":
            return model_type(verdict="mentioned_only", supporting_segment_index=1)
        if appellation == "乙":
            # 钉证失败：段号伪造成卷宗里不存在的编号。
            return model_type(verdict="onstage", supporting_segment_index=99)
        return model_type(verdict="onstage", supporting_segment_index=1)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    monkeypatch.setattr(model_gateway, "chat_structured", fake_chat_structured)
    ranked = asyncio.run(stages._recurring_character_names(chapters))

    assert ranked == [("丙", "", 2, 20, 20, [])]


def test_recurring_character_names_matches_real_project_evidence_shapes(monkeypatch) -> None:
    """本项目真实数据验收（proj_3ac0b627fa46 前 20 章逐字核实过的引句）：王伯/周员外/
    靠山老祖三个假阳性角色在前 20 章窗口内的命中全部是身份/背景交代（旁白转述、
    他人台词提及），没有一条是本人在场；小胖子（=李富贵）则有本人真实的动作/情绪
    细节描写。字符串用 \\uXXXX 转义逐字录入，保证与原文数据库取值字节一致，不依赖
    人工誊抄中文标点的准确性。

    裁决闸本身用 mock 固定为人工研判结论（结构闸+聚合逻辑是本测试真正要验证的
    东西，不是"真的调用模型会不会判对"——那需要不 mock 的真实集成 dry run，
    不适合放进自动化单元测试套件）。"""
    import asyncio
    import json

    from app import stages
    from app.harness import model_gateway

    # 王伯：孟浩自陈家境时顺带比较（"甚至不如王伯的木匠铺子赚钱"），本人未出场。
    wang_bo_a = (
        "“哪怕是县城里的教习先生，"
        "每月也只有几钱银子，甚至不"
        "如王伯的木匠铺子赚钱，早知"
        "如此头些年不去读书，和王老"
        "伯去学木匠手艺，想来日后总"
        "算能解决温饱，好过如今一无"
        "所有。"
    )
    # 王伯：孟浩感慨木匠铺的王伯只剩这一个儿子，仍是背景交代，本人未出场。
    wang_bo_b = (
        "小胖子神色露出悲伤，孟浩叹"
        "了口气，当年一同上山的四人"
        "，这还不到一年就听闻死去一"
        "个，他内心也很不好受，尤其"
        "想到木匠铺的王伯只有这一子"
        "，心里更为难受起来。"
    )
    # 周员外：孟浩的内心独白提及欠债对象，周员外本人未出场。
    zhou_a = (
        "“家里已经没有多少粮食了，"
        "银两也都花的所剩无几，还欠"
        "了周员外三两银子，以后……"
        "怎么办。"
    )
    zhou_b = (
        "孟浩神色有些古怪，暗道这小"
        "胖子人不大，居然都说了亲，"
        "自己这么大年纪，如今连女人"
        "手都没摸过，不由的感慨还是"
        "有钱好啊，这小胖子家里家财"
        "万贯，衣食无忧，而自己一穷"
        "二白，祖屋去年都卖掉，如今"
        "还欠下周员外一屁股债。"
    )
    # 靠山老祖：宗门历史沿革交代，"失踪四百余年"，本人显然不可能出场。
    kao_a = (
        "实际上靠山宗原本也不是叫这"
        "个名字，只不过在千年前出了"
        "一位轰动整个南域的修士，此"
        "人自号靠山老祖，更是强行将"
        "宗门之名改为靠山宗，横行霸"
        "道，几乎搜刮了赵国所有宗门"
        "之宝，风光一时无两。"
    )
    kao_b = (
        "靠山宗与其他宗门有些不同，"
        "外宗在下，反倒是杂役可以居"
        "住半山腰，这一点是当年靠山"
        "老祖不知什么原因定下的门规。"
    )
    # 小胖子（=李富贵）：本人的动作/情绪描写，真正在场。
    fatty_a = (
        "小胖子身子猛地哆嗦了一下，"
        "双眼露出强烈的恐惧，连忙把"
        "自己的嘴捂住，身子颤抖的越"
        "加剧烈。"
    )
    fatty_b = (
            "“我爹是财主，我应该也是财"
            "主，我不做杂役……”小胖子"
            "哭的极为伤心，身子哆嗦时肥"
            "肉也随着颤抖。众人称小胖子李富贵。"
    )

    chapters = [
        {"idx": 1, "title": "第一章", "content": wang_bo_a},
        {"idx": 2, "title": "第二章", "content": wang_bo_b},
        {"idx": 3, "title": "第三章", "content": zhou_a},
        {"idx": 4, "title": "第四章", "content": zhou_b},
        {"idx": 5, "title": "第五章", "content": kao_a},
        {"idx": 6, "title": "第六章", "content": kao_b},
        {"idx": 7, "title": "第七章", "content": fatty_a},
        {"idx": 8, "title": "第八章", "content": fatty_b},
    ]

    async def fake_chat(_messages, **_kwargs):
        return json.dumps({
            "candidates": [
                {
                    "primary_appellation": "王伯", "formal_name": "",
                    "onstage_evidence": [
                        {"chapter_index": 1, "quote": wang_bo_a},
                        {"chapter_index": 2, "quote": wang_bo_b},
                    ],
                },
                {
                    "primary_appellation": "周员外", "formal_name": "",
                    "onstage_evidence": [
                        {"chapter_index": 3, "quote": zhou_a},
                        {"chapter_index": 4, "quote": zhou_b},
                    ],
                },
                {
                    "primary_appellation": "靠山老祖", "formal_name": "",
                    "onstage_evidence": [
                        {"chapter_index": 5, "quote": kao_a},
                        {"chapter_index": 6, "quote": kao_b},
                    ],
                },
                {
                    "primary_appellation": "小胖子", "formal_name": "李富贵",
                    "onstage_evidence": [
                        {"chapter_index": 7, "quote": fatty_a},
                        {"chapter_index": 8, "quote": fatty_b},
                    ],
                },
            ],
        }, ensure_ascii=False)

    mentioned_only = {"王伯", "周员外", "靠山老祖"}

    async def fake_chat_structured(_messages, **kwargs):
        meta = kwargs.get("call_meta") or {}
        model_type = kwargs["model_type"]
        # 真名复核对点名申报的 formal_name 也要跑一遍；这里照原文回答，
        # 让「小胖子→李富贵」通过，其余候选保持未揭示。
        if model_type is stages._RosterTrueNameResolution:
            if meta.get("character_name") == "小胖子":
                return model_type(
                    verdict="revealed", true_name="李富贵",
                    supporting_chapter_index=8, supporting_quote="众人称小胖子李富贵。",
                )
            return model_type(verdict="unrevealed")
        appellation = meta.get("appellation", "")
        verdict = "mentioned_only" if appellation in mentioned_only else "onstage"
        return model_type(verdict=verdict, supporting_segment_index=1)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    monkeypatch.setattr(model_gateway, "chat_structured", fake_chat_structured)
    ranked = asyncio.run(stages._recurring_character_names(chapters))

    assert ranked == [("小胖子", "李富贵", 2, 7, 4, ["小胖子"])]


def test_bible_covers_name_matches_verified_alias_not_only_canonical_name() -> None:
    """难点 C 直接验收：must_cover 条目是绰号"小胖子"，人物谱里角色是 name="李富贵" +
    aliases 含 text="小胖子" → 判定为已覆盖，不触发补录。别名命中要求精确相等
    （不用子串）：单字"胖"不该因为是"小胖子"的子串就被判定命中一堆无关别名。
    空集合天然未覆盖，不是被短路跳过检查。"""
    from app.schemas import Bible, Character, CharacterAlias, World

    from app import stages

    bible = Bible(
        characters=[Character(
            name="李富贵", role="重要配角",
            appearance_canonical="二十岁男子，圆脸微胖，粗麻布衣，腰系布带，笑容憨厚",
            aliases=[CharacterAlias(
                text="小胖子", name_kind="referential",
                evidence_chapter_index=2, evidence_quote="占位证据引句",
            )],
        )],
        world=World(visual_style_canonical="国漫3D动画电影质感，精致光影"),
    )

    assert stages._bible_covers_name(bible, {"小胖子", ""}) is True
    assert stages._bible_covers_name(bible, {"李富贵"}) is True
    assert stages._bible_covers_name(bible, {"胖"}) is False
    assert stages._bible_covers_name(bible, {"张三"}) is False
    assert stages._bible_covers_name(bible, set()) is False


def test_roster_presence_dossier_locates_quote_with_surrounding_context() -> None:
    """在场裁决卷宗检索：定位 quote 所在自然段，连同前后各 1 段一并收录。"""
    from app import stages

    chapter_text = "段一内容在这里。\n\n段二含有关键引句在此。\n\n段三收尾内容。"
    dossier = stages._roster_presence_dossier(5, chapter_text, "段二含有关键引句在此。")

    assert [item["segment_index"] for item in dossier] == [1, 2, 3]
    assert dossier[1]["text"] == "段二含有关键引句在此。"
    assert all(item["chapter_idx"] == 5 for item in dossier)


def test_roster_presence_dossier_empty_when_quote_not_locatable() -> None:
    """quote 在分段结果里定位不到（极端情况）时返回空列表，交由调用方按
    no_presence_dossier 拒绝——不确定不登记，不是跳过检查。"""
    from app import stages

    assert stages._roster_presence_dossier(1, "毫不相关的原文。", "根本没有的引句") == []


def test_generate_bible_never_calls_any_model_or_roster_pipeline(monkeypatch) -> None:
    """架构转向（2026-08-31 二次拍板）：generate_bible 不再判定世界观、不再点名
    角色、不复用整套 roster 流水线——不发起任何模型调用。上一版方案是「发一次
    轻量模型调用判定 era/genre/画风」，这个方案本身被推翻：画风已经由用户在
    导入面板选定，问模型是多余的，而这次多余调用在真实项目（《我欲封天》）
    上直接触发 HiAgent 内容审核（``finish_reason=content_filter``），把用户
    拦在 ``bible_status=failed`` 且没有出路。这条测试用打桩计数钉住「调用次数
    恒为 0」，同时保留旧用例「不再调用点名流水线」与「characters 恒为 []」的
    回归锁（旧用例断言 bible.characters 非空，验的是已经退场的整套点名-归并-
    详情生成行为；再之前一版断言「发一个只判定世界观的模型调用」，那个中间
    方案本身也已经退场）。"""
    import asyncio
    from app import stages
    from app.harness import model_gateway

    model_calls = {"count": 0}
    roster_called = {"value": False}

    async def fake_recurring_character_names(*_args, **_kwargs):  # pragma: no cover - 不该被调用
        roster_called["value"] = True
        return [("小胖子", "李富贵", 2, 16, 6, [])]

    async def fake_chat_structured(*_args, **_kwargs):  # pragma: no cover - 不该被调用
        model_calls["count"] += 1
        raise AssertionError("generate_bible 不应再发起任何模型调用")

    _patch_stages(monkeypatch, "_recurring_character_names", fake_recurring_character_names)
    monkeypatch.setattr(model_gateway, "chat_structured", fake_chat_structured)

    bible = asyncio.run(stages.generate_bible(
        [{"idx": 1, "title": "第一章", "content": "小胖子与孟浩同行。"}],
        visual_style_prompt="国漫3D动画电影质感，精致光影，统一电影画面",
    ))

    assert bible.characters == []
    assert bible.world.visual_style_canonical == "国漫3D动画电影质感，精致光影，统一电影画面"
    assert roster_called["value"] is False, "不得再调用点名流水线"
    assert model_calls["count"] == 0, "generate_bible 不该再发起任何模型调用"


def test_paratext_scope_does_not_scale_with_book_length() -> None:
    """旁文本净化只覆盖人物谱真正读到的章：代价不随书长增长。

    回归 run_8388b4e31301：净化原本串行跑全书，643 章的项目 15 分钟闸门内
    连人物谱本体调用都没轮上就超时作废。
    """
    from app import stages

    def _book(n: int) -> list[dict]:
        return [
            {"idx": i, "title": f"第{i}章", "content": f"第{i}章开头。" + "文" * 4000}
            for i in range(1, n + 1)
        ]

    short = stages._bible_paratext_scope(_book(40))
    long = stages._bible_paratext_scope(_book(1600))

    # 书长翻 40 倍，净化章数不得跟着涨：上限只由「读多少」的常量决定。
    cap = (stages.BIBLE_HEAD_CHAPTERS + stages.BIBLE_LOOKAHEAD_CHAPTERS
           + stages.BIBLE_PARATEXT_MARGIN_CHAPTERS + stages._BIBLE_TAIL_SAMPLE_MAX)
    assert len(short) <= cap
    assert len(long) <= cap
    assert len(long) - len(short) <= stages._BIBLE_TAIL_SAMPLE_MAX
    # 必收名单的逐字统计窗口（前 HEAD+LOOKAHEAD 章）必须整段净化——作者笔名
    # 正是从这个窗口混进必收名单的。
    window = stages.BIBLE_HEAD_CHAPTERS + stages.BIBLE_LOOKAHEAD_CHAPTERS
    assert set(range(window)).issubset(set(long))
    # 后段抽样章也读进了提示词，同样要净化。
    plan = stages._bible_source_plan(_book(1600), stages.BIBLE_SOURCE_BUDGET_CHARS,
                                     stages.BIBLE_HEAD_CHAPTERS)
    assert set(index for index, _, _ in plan).issubset(set(long))


def test_paratext_cleaning_is_capped_and_fails_open(monkeypatch) -> None:
    """净化是净化步骤不是闸门：超时未完成的章原样进入下游，不拖死人物谱。

    改造后（chapters.paratext_json 持久化，见
    logs/paratext_single_source_plan.md）净化的入口函数换成
    `chapter_paratext_offsets`（取/算/落库），这里改为挂起它本身，验证
    同一条"budget 到点就砍、未完成的章原样保留"的路径依然成立。
    """
    import asyncio

    from app import source_paratext, stages

    chapters = [
        {"id": f"ch{i}", "idx": i, "title": f"第{i}章", "content": f"第{i}章开头。" + "文" * 4000}
        for i in range(1, 40)
    ]

    async def _never_returns(conn, chapter_row, *, operation_id: str):
        await asyncio.sleep(3600)
        return [], False

    monkeypatch.setattr(source_paratext, "chapter_paratext_offsets", _never_returns)
    _patch_stages(monkeypatch, "BIBLE_PARATEXT_BUDGET_S", 0.2)
    _patch_stages(monkeypatch, "BIBLE_PARATEXT_CHAPTER_TIMEOUT_S", 0.05)

    started = asyncio.get_event_loop_policy().new_event_loop()
    try:
        cleaned = started.run_until_complete(stages._chapters_without_paratext(chapters))
    finally:
        started.close()

    assert cleaned == chapters


def test_paratext_budget_is_not_dragged_out_by_slow_cancellation(monkeypatch) -> None:
    """任务一：15s 预算必须真正封顶净化阶段的墙钟耗时，不能只是"发出取消
    信号"的时间点。

    `test_paratext_cleaning_is_capped_and_fails_open` 的桩函数不处理
    `CancelledError`，一被 `cancel()` 就立刻结束——覆盖不到真实事故：供应商
    侧对取消信号的响应本身很慢（实测最长 ~120s 才收尾）。这里的桩显式吞下
    `CancelledError` 后再拖一段时间才重新抛出，复现"取消信号已发、但迟迟
    不落地"的真实形状，断言净化阶段本身不会被这段收尾时间拖累。
    """
    import asyncio
    import time as time_mod

    from app import source_paratext, stages
    from app.stages import bible_paratext as bible_paratext_mod

    chapters = [
        {"id": f"ch{i}", "idx": i, "title": f"第{i}章", "content": f"第{i}章开头。" + "文" * 4000}
        for i in range(1, 10)
    ]

    async def _slow_to_cancel(conn, chapter_row, *, operation_id: str):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            # 供应商侧收到取消信号后仍不肯立刻收尾（真实事故里最长 ~120s）。
            await asyncio.sleep(0.6)
            raise
        return [], False

    monkeypatch.setattr(source_paratext, "chapter_paratext_offsets", _slow_to_cancel)
    _patch_stages(monkeypatch, "BIBLE_PARATEXT_BUDGET_S", 0.2)
    _patch_stages(monkeypatch, "BIBLE_PARATEXT_CHAPTER_TIMEOUT_S", 0.1)

    loop = asyncio.get_event_loop_policy().new_event_loop()
    try:
        wall_started = time_mod.monotonic()
        cleaned = loop.run_until_complete(stages._chapters_without_paratext(chapters))
        elapsed = time_mod.monotonic() - wall_started
        # 关键断言：净化阶段自身的墙钟耗时必须贴着 0.2s 预算，不能被拖到
        # 供应商侧 0.6s 才收尾的取消响应——如果又退回"等它落地"，这里会量出
        # 接近 0.6s+ 的耗时而不是 0.2s 出头。
        assert elapsed < 0.5
        assert cleaned == chapters
    finally:
        # 测试卫生：净化阶段已经返回，但后台收尾任务还在等桩函数落地——
        # 在关闭这个临时 event loop 前先让它跑完，避免 "Task was destroyed
        # but it is pending" 噪音；这段等待不计入上面的耗时断言。
        stragglers = [t for t in bible_paratext_mod._PARATEXT_STRAGGLER_REAPERS if not t.done()]
        if stragglers:
            loop.run_until_complete(asyncio.gather(*stragglers, return_exceptions=True))
        loop.close()


def test_paratext_dispatch_stops_early_instead_of_firing_then_cancelling(monkeypatch) -> None:
    """任务二：不能"发完再靠预算砍掉大半"——1616 章的真实项目里这样干出过
    "成功 50、取消 59"，取消比成功还多，白白占用供应商时间。改成按本轮已
    完成调用的实测耗时动态判断还要不要继续发；章节数远超预算能承载的量时，
    实际发出的请求数必须显著少于章节总数（不断言具体数字，只断言这个
    "发出数 < 总数"的关系，判据从运行时实测耗时推导，不是新写死的常量）。
    """
    import asyncio

    from app import source_paratext, stages
    from app.stages import bible_paratext as bible_paratext_mod

    total = 60
    chapters = [
        {"id": f"ch{i}", "idx": i, "title": f"第{i}章", "content": f"第{i}章开头。" + "文" * 200}
        for i in range(1, total + 1)
    ]
    dispatched = 0

    async def _steady_call(conn, chapter_row, *, operation_id: str):
        nonlocal dispatched
        dispatched += 1
        await asyncio.sleep(0.2)
        return [], False

    monkeypatch.setattr(source_paratext, "chapter_paratext_offsets", _steady_call)
    # 把 scope 强制展开到全部章节：这个测试专门测"发多少"，不掺 scope 本身
    # 的裁剪（那条已有 test_paratext_scope_does_not_scale_with_book_length 守）。
    _patch_stages(monkeypatch, "_bible_paratext_scope", lambda valid: list(range(len(valid))))
    _patch_stages(monkeypatch, "BIBLE_PARATEXT_BUDGET_S", 0.6)
    _patch_stages(monkeypatch, "BIBLE_PARATEXT_CHAPTER_TIMEOUT_S", 0.3)
    _patch_stages(monkeypatch, "BIBLE_PARATEXT_CONCURRENCY", 2)

    loop = asyncio.get_event_loop_policy().new_event_loop()
    try:
        cleaned = loop.run_until_complete(stages._chapters_without_paratext(chapters))
    finally:
        stragglers = [t for t in bible_paratext_mod._PARATEXT_STRAGGLER_REAPERS if not t.done()]
        if stragglers:
            loop.run_until_complete(asyncio.gather(*stragglers, return_exceptions=True))
        loop.close()

    assert cleaned == chapters
    # 60 章、预算 0.6s、并发 2、单条 0.2s：理论上限约 6 条左右能在预算内跑完；
    # 旧代码会把 60 条全部 create_task 再靠超时砍掉大半，新代码应当让明显
    # 来不及的调用自己放弃、从不发起请求。
    assert dispatched < total
