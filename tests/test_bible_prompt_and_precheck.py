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
    assert result["character_count"] == 20
    assert result["image_count"] == 60
    assert result["estimated_cost_cny"] == 12.0
    assert result["style_name"] == "国漫电影风"


def test_visual_style_options_expose_names_and_descriptions_only(monkeypatch) -> None:
    from app.domain import bible_ops
    import asyncio

    monkeypatch.setattr(bible_ops, "_project_or_404", lambda _pid: {"id": "p1"})

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
    monkeypatch.setattr(bible_ops, "get_conn", lambda: conn)
    monkeypatch.setattr(bible_ops, "_project_or_404", lambda _pid: dict(conn.execute(
        "SELECT * FROM projects WHERE id='p1'"
    ).fetchone()))

    quote = bible_ops._compute_bible_generate_precheck("p1", style_name="真人摄影风")

    assert quote["style_name"] == "真人摄影风"
    assert quote["quote_id"] == fingerprint({
        "project_id": "p1",
        "action": "generate_bible_and_refs",
        "character_count": 20,
        "image_count": 60,
        "unit": 0.2,
        "bible_version": 0,
        "style_name": "真人摄影风",
    })


def test_generate_bible_forces_backend_visual_style_prompt(monkeypatch) -> None:
    from app import stages
    import asyncio

    seen = {}

    async def fake_roll_call(*_args, **_kwargs):
        return [("孟浩", "", 2, 10, 1, [])]

    async def fake_loop(*_args, **_kwargs):
        seen["allow_warning_candidate"] = _kwargs["loop"].policy.allow_warning_candidate
        seen["repair_all_blockers"] = _kwargs["loop"].policy.repair_all_blockers
        candidate = stages._BibleRosterDraft(
            world=World(visual_style_canonical="模型自行写的画风"),
            characters=[stages._BibleRosterEntry(name="孟浩", role="主角")],
        )
        assert _args[4](candidate) == []
        seen["style_during_validation"] = candidate.world.visual_style_canonical
        return candidate

    async def fake_details(entries, *_args, **_kwargs):
        return [Character(
            name=entries[0].name, role=entries[0].role,
            appearance_canonical="黑发少年，青色长衫，目光沉稳，身形清瘦，腰间系旧布袋",
        )]

    monkeypatch.setattr(stages, "_recurring_character_names", fake_roll_call)
    monkeypatch.setattr(stages, "_run_with_agent_loop", fake_loop)
    monkeypatch.setattr(stages, "_generate_character_detail_batch", fake_details)
    monkeypatch.setattr(stages, "_verify_character_aliases_in_place", lambda *_args, **_kwargs: asyncio.sleep(0))

    result = asyncio.run(stages.generate_bible(
        [{"idx": 1, "title": "第一章", "content": "孟浩走入山中。"}],
        visual_style_prompt="电影级真实质感，现实人物建模，自然光影，细节丰富，东方仙侠风。",
    ))

    assert result.world.visual_style_canonical == "电影级真实质感，现实人物建模，自然光影，细节丰富，东方仙侠风。"
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
        appellation = (kwargs.get("call_meta") or {}).get("appellation", "")
        model_type = kwargs["model_type"]
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


def test_generate_bible_uses_small_roster_contract_and_single_character_details(monkeypatch) -> None:
    import asyncio
    from app import stages

    seen: dict[str, object] = {}

    async def fake_roll_call(*_args, **_kwargs):
        return [("小胖子", "李富贵", 2, 16, 6, [])]

    async def fake_loop(*args, **kwargs):
        seen["prompt"] = args[2]
        seen["repair_user_prompt_limit"] = kwargs["repair_user_prompt_limit"]
        return stages._BibleRosterDraft(
            world=World(visual_style_canonical="国漫3D动画电影质感，精致光影，统一电影画面"),
            characters=[stages._BibleRosterEntry(
                name="李富贵", role="重要配角", source_appellations=["小胖子"],
            )],
        )

    async def fake_details(entries, *_args, **_kwargs):
        seen["entries"] = entries
        return [Character(
            name=entries[0].name, role=entries[0].role,
            appearance_canonical="十六七岁少年，黑色短发，深棕短打，身形敦实，腰间挂木尺",
        )]

    monkeypatch.setattr(stages, "_recurring_character_names", fake_roll_call)
    monkeypatch.setattr(stages, "_run_with_agent_loop", fake_loop)
    monkeypatch.setattr(stages, "_generate_character_detail_batch", fake_details)
    monkeypatch.setattr(stages, "_verify_character_aliases_in_place", lambda *_args, **_kwargs: asyncio.sleep(0))

    bible = asyncio.run(stages.generate_bible([
        {"idx": 1, "title": "第一章", "content": "小胖子与孟浩同行。"}
    ]))

    # 本章「小胖子」出现、真名「李富贵」未出现，主名按原文频次保留绰号。
    assert [item.name for item in bible.characters] == ["小胖子"]
    assert "不要生成外观" in str(seen["prompt"])
    assert "已核验候选摘要" in str(seen["prompt"])
    assert "小胖子与孟浩同行" not in str(seen["prompt"])
    assert seen["repair_user_prompt_limit"] == 16000
    assert seen["entries"][0].name == "小胖子"
    assert seen["entries"][0].source_appellations == ["李富贵"]


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
    monkeypatch.setattr(stages, "BIBLE_PARATEXT_BUDGET_S", 0.2)
    monkeypatch.setattr(stages, "BIBLE_PARATEXT_CHAPTER_TIMEOUT_S", 0.05)

    started = asyncio.get_event_loop_policy().new_event_loop()
    try:
        cleaned = started.run_until_complete(stages._chapters_without_paratext(chapters))
    finally:
        started.close()

    assert cleaned == chapters
