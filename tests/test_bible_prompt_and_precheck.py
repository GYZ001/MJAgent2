"""提示词规范化与人物谱扩展预检。"""
from __future__ import annotations

import sqlite3

from app.domain import common
from app.refs import normalize_prompt_text, portrait_prompt
from app.orchestration.engine import fingerprint
from app.schemas import Bible, Character, World
from app.validators import validate_bible


def test_normalize_prompt_collapses_duplicate_punctuation() -> None:
    assert ".." not in normalize_prompt_text("戒指。。正面站立")
    assert "。。" not in normalize_prompt_text("戒指。。正面站立")
    assert normalize_prompt_text("戒指。。正面站立") == "戒指。正面站立"


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


def test_project_or_404_normalizes_sqlite_row_to_dict(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects("
        "id TEXT PRIMARY KEY, bible_status TEXT, bible_error TEXT, bible_json TEXT, bible_version INTEGER DEFAULT 0"
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
    from app.domain import bible_ops
    import asyncio

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER DEFAULT 0)"
    )
    conn.execute("INSERT INTO projects(id, bible_json, bible_version) VALUES('p1', NULL, 0)")
    conn.commit()
    monkeypatch.setattr(bible_ops, "get_conn", lambda: conn)
    monkeypatch.setattr(bible_ops, "_project_or_404", lambda _pid: dict(conn.execute(
        "SELECT * FROM projects WHERE id='p1'"
    ).fetchone()))

    result = asyncio.run(bible_ops.bible_generate_precheck("p1"))
    assert result["character_count"] == 12
    assert result["image_count"] == 36
    assert result["estimated_cost_cny"] == 7.2
    assert result["style_name"] == "国漫电影风"


def test_visual_style_options_expose_names_and_descriptions_only(monkeypatch) -> None:
    from app.domain import bible_ops
    import asyncio

    monkeypatch.setattr(bible_ops, "_project_or_404", lambda _pid: {"id": "p1"})

    result = asyncio.run(bible_ops.bible_visual_styles("p1"))

    assert result["default"] == "国漫电影风"
    assert any(
        item["name"] == "现实电影风" and "写实" in item["description"]
        for item in result["items"]
    )
    assert all(set(item) == {"name", "description"} for item in result["items"])


def test_bible_generate_precheck_binds_style_name(monkeypatch) -> None:
    from app.domain import bible_ops

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER DEFAULT 0)"
    )
    conn.execute("INSERT INTO projects(id, bible_json, bible_version) VALUES('p1', NULL, 0)")
    conn.commit()
    monkeypatch.setattr(bible_ops, "get_conn", lambda: conn)
    monkeypatch.setattr(bible_ops, "_project_or_404", lambda _pid: dict(conn.execute(
        "SELECT * FROM projects WHERE id='p1'"
    ).fetchone()))

    quote = bible_ops._compute_bible_generate_precheck("p1", style_name="现实电影风")

    assert quote["style_name"] == "现实电影风"
    assert quote["quote_id"] == fingerprint({
        "project_id": "p1",
        "action": "generate_bible_and_refs",
        "character_count": 12,
        "image_count": 36,
        "unit": 0.2,
        "bible_version": 0,
        "style_name": "现实电影风",
    })


def test_generate_bible_forces_backend_visual_style_prompt(monkeypatch) -> None:
    from app import stages
    from app.schemas import Bible, Character, World
    import asyncio

    seen = {}

    async def fake_loop(*_args, **_kwargs):
        seen["allow_warning_candidate"] = _kwargs["loop"].policy.allow_warning_candidate
        seen["repair_all_blockers"] = _kwargs["loop"].policy.repair_all_blockers
        candidate = Bible(
            world=World(visual_style_canonical="模型自行写的画风"),
            characters=[
                Character(
                    name="孟浩",
                    role="主角",
                    appearance_canonical="黑发少年，青色长衫，目光沉稳，身形清瘦，腰间系旧布袋",
                ),
            ],
        )
        assert _args[4](candidate) == []
        seen["style_during_validation"] = candidate.world.visual_style_canonical
        return candidate

    monkeypatch.setattr(stages, "_run_with_agent_loop", fake_loop)

    result = asyncio.run(stages.generate_bible(
        [{"idx": 1, "title": "第一章", "content": "孟浩走入山中。"}],
        visual_style_prompt="电影级真实质感，现实人物建模，自然光影，细节丰富，东方仙侠风。",
    ))

    assert result.world.visual_style_canonical == (
        "电影级真实质感，现实人物建模，自然光影，细节丰富，东方仙侠风。"
    )
    assert seen["style_during_validation"] == result.world.visual_style_canonical
    assert seen["allow_warning_candidate"] is False
    assert seen["repair_all_blockers"] is True


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


def test_recurring_character_names_ranks_by_lookahead_occurrences(monkeypatch) -> None:
    """点名之后由后端逐字统计：出现多次才算重要，改写出来的名字不算数。"""
    import asyncio
    import json

    from app import stages
    from app.harness import model_gateway

    chapters = [
        {"idx": 1, "title": "第一章", "content": "孟浩与王有材同行，孟浩开口。"},
        {"idx": 2, "title": "第二章", "content": "孟浩再遇王有材，路人甲一闪而过。"},
    ] + [
        {"idx": i, "title": f"第{i}章", "content": f"第{i}章：孟浩独行，王有材追来。"}
        for i in range(3, 21)
    ]

    async def fake_chat(_messages, **_kwargs):
        return json.dumps({
            "names": ["孟浩", "王有材", "路人甲", "孟浩然"],
        }, ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    ranked = asyncio.run(stages._recurring_character_names(chapters))

    assert [name for name, _ in ranked] == ["孟浩", "王有材"]
    assert dict(ranked)["孟浩"] >= dict(ranked)["王有材"]


def test_generate_bible_keeps_source_in_repair_rounds_and_supplements(monkeypatch) -> None:
    """修复轮不得截掉原文；必收名单缺人时用一次补录补齐，而不是把项目卡成 warning。"""
    import asyncio
    import json

    from app import stages
    from app.harness import model_gateway

    chapters = [
        {"idx": i, "title": f"第{i}章", "content": f"第{i}章：孟浩与王有材同行，许师姐在旁。"}
        for i in range(1, 21)
    ]
    seen: dict[str, object] = {}

    async def fake_chat(messages, **kwargs):
        stage_key = str((kwargs.get("call_meta") or {}).get("stage_key") or "")
        if stage_key == "character_roll_call":
            return json.dumps({"names": ["孟浩", "王有材", "许师姐"]}, ensure_ascii=False)
        seen["supplement_prompt"] = messages[-1]["content"]
        return json.dumps({"characters": [
            {
                "name": "许师姐",
                "role": "重要配角",
                "appearance_canonical": "二十岁女子，墨发高马尾，银色素面长袍，身形清瘦，背后一柄银色长剑",
                "personality": "外冷内热",
                "speech_style": "话少句短，语气平淡，极少多余修饰",
                "relationships": [{"to": "无名氏", "relation": "同门"}],
            },
            {
                "name": "王有材",
                "role": "重要配角",
                "appearance_canonical": "十六七岁少年，黑色短发梳整齐，深棕短打木匠服，身形敦实，腰间挂木尺",
                "personality": "略带莽撞",
                "speech_style": "说话直白无修饰，情急下语速快，常用口语",
                "relationships": [{"to": "孟浩", "relation": "同乡"}],
            },
            {
                "name": "无名氏",
                "role": "反派",
                "appearance_canonical": "三十岁男子，黑色长发束冠，玄色长袍绣暗纹，身形高瘦，左颊一道旧疤",
                "personality": "阴沉",
                "speech_style": "语调平缓，句子极短，从不多说一个字",
                "relationships": [],
            },
        ]}, ensure_ascii=False)

    async def fake_loop(*args, **kwargs):
        seen["prompt"] = args[2]
        seen["repair_user_prompt_limit"] = kwargs["repair_user_prompt_limit"]
        return Bible(
            world=World(visual_style_canonical="国漫3D动画电影质感，精致光影，统一电影画面"),
            characters=[Character(
                name="孟浩",
                role="主角",
                appearance_canonical="十六七岁少年，黑色短发额前碎发，蓝色文士长衫，身形瘦弱，腰间挂布袋",
            )],
        )

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    monkeypatch.setattr(stages, "_run_with_agent_loop", fake_loop)

    bible = asyncio.run(stages.generate_bible(chapters))

    assert seen["repair_user_prompt_limit"] is None
    assert "【必收角色名单】" in seen["prompt"]
    assert "许师姐" in seen["prompt"]
    # 必收名单里缺的人补齐；名单之外的人不得借补录混进人物谱。
    assert [c.name for c in bible.characters] == ["孟浩", "许师姐", "王有材"]
    assert "许师姐" in str(seen["supplement_prompt"])
    # 补录进来的关系不得指向名单外的人，否则 validate_bible 会退回重写。
    assert validate_bible(bible) == []


def test_generate_bible_prompt_explains_bridging_chapter_for_aliases(monkeypatch) -> None:
    """修复 B：规则 5 必须讲清楚 evidence_chapter_index 要选别名与正式姓名（或已确认别名）
    共现的桥接章，不是别名第一次出现的章节；也要讲清楚不要自己给引句加引号包裹。这两点
    对应全书别名回填 dry-run 12 条只过 0 条的诊断——generate_bible 内联申报别名走的是
    同一套核验（`_verify_character_aliases_in_place`），提示词讲不清同样会全军覆没。"""
    import asyncio

    from app import stages
    from app.schemas import Bible, Character, World

    seen: dict[str, object] = {}

    async def fake_loop(*args, **_kwargs):
        seen["prompt"] = args[2]
        return Bible(
            world=World(visual_style_canonical="国漫3D动画电影质感，精致光影"),
            characters=[Character(
                name="孟浩", role="主角",
                appearance_canonical="十六七岁少年，黑色短发额前碎发，蓝色文士长衫，身形瘦弱，腰间挂布袋",
            )],
        )

    monkeypatch.setattr(stages, "_run_with_agent_loop", fake_loop)

    asyncio.run(stages.generate_bible(
        [{"idx": 1, "title": "第一章", "content": "孟浩走入山中。"}],
    ))

    prompt = str(seen["prompt"])
    assert "不是该别名第一次出现的章节" in prompt
    assert "同时出现" in prompt
    assert "不要自己在引句前后加引号包裹" in prompt


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
    """净化是净化步骤不是闸门：超时未完成的章原样进入下游，不拖死人物谱。"""
    import asyncio

    from app import source_paratext, stages

    chapters = [
        {"id": f"ch{i}", "idx": i, "title": f"第{i}章", "content": f"第{i}章开头。" + "文" * 4000}
        for i in range(1, 40)
    ]

    async def _never_returns(text: str, *, operation_id: str) -> str:
        await asyncio.sleep(3600)
        return ""

    monkeypatch.setattr(source_paratext, "strip_paratext", _never_returns)
    monkeypatch.setattr(stages, "BIBLE_PARATEXT_BUDGET_S", 0.2)

    started = asyncio.get_event_loop_policy().new_event_loop()
    try:
        cleaned = started.run_until_complete(stages._chapters_without_paratext(chapters))
    finally:
        started.close()

    assert cleaned == chapters
