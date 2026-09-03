"""WS3：人物发现按叙事分量与画面存在判定，非角色判定不再与画面事实相反。

两层测试：
1. ``presence_evidence`` 纯函数单元测试——fixture 摘自生产库原文（proj_ce9fcf749b23
   《跑不快的孩子》、神墓，均为只读查询取得的逐字原文，不是编造文本）。
2. ``ensure_character_card`` / ``ensure_cards_for_text`` 集成测试——复用
   tests/test_character_discovery.py 的 _make_conn/_seed_project/_patch_settings/
   patch_portraits_everywhere 套路，验证：
   - 画面存在证据能纠正 subject_kind 误判（生产事故复现：马拉多纳/姆巴佩）；
   - 没有证据时非人闸门原样生效，不被本次改动松动
     （对照既有 test_non_person_never_enters_the_character_bible）；
   - 负缓存挂在证据指纹上，新证据出现必须重判；
   - 无名但反复在场/处于高潮的功能身份可以建卡，一句话路人不建卡。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

from app import portraits
from app.portraits import cards_ensure
from app.portraits.presence_evidence import (
    collect_presence_evidence,
    functional_card_worthy,
    has_onscreen_evidence,
    presence_evidence_fingerprint,
)
from app.schemas import Bible, Character, World

from tests.conftest import patch_portraits_everywhere

# ---------- fixture 原文：proj_ce9fcf749b23《跑不快的孩子》，B 库只读查询取得 ----------
MESSI_CHAPTER = """
针扎进去的时候，他就盯着墙上那张马拉多纳的海报。三年，一千多次。他没跟任何队友提过这件事。

十一岁那年，奶奶去世了。她是唯一一个坐在场边看过他踢球的人。葬礼之后，里奥回到土场，踢了很久。

奶奶坐在场边的矮墙上看着，说他像"一只被踢开了又滚回来的皮球"。

诊断书下来的时候，医生说的词他听不懂。他只记得那个词很长，长到念完要换两口气。

体育总监卡洛斯·雷克萨奇听完经纪人说"再不定，人就走了"，要了一张餐巾纸，在背面写下一句话，签了字。

终场哨响的时候他没有倒地，也没有捂脸。马拉多纳是那届的主教练。赛后马拉多纳哭了，里奥没哭。

那届世界杯上流传最广的一张照片，是里奥双手叉腰站着，身后的姆巴佩刚刚跑过他身边，快得像一道白色的光。

姆巴佩的帽子戏法。里奥站在中圈，喘着气，看着球网里那个球。
"""

# ---------- fixture 原文：神墓，B 库只读查询取得 ----------
SHENMU_CHAPTER = """
雪枫林前方不远处出现三间茅屋，一个瘦骨嶙峋的老人立于门前，老人须发皆百，满脸镌刻着饱经风霜的皱纹。

万年前他降生在他的父母面前，万年后他再生时，却面对这样一个老人。"我怎么会将父母和这个老人联系到一起呢？"他自嘲的笑了笑。老人拄着一条拐杖颤颤巍巍向他走来。

辰南想起了他父亲对他说的话："辰南你要记住，能够看透我们家传玄功内息流转的人都不简单，不是真正的武学高手，就是修道者。"

之境，在大多数人眼里，武人所走的道路不如修道者，但是……他父亲没有继续说下去，但辰南已然明白，武者并非不能和修道者相抗，因为他父亲本身就是一个最好的例子，即使那些修道有成之人见了他之后也只以平辈论交。
"""

# ---------- fixture 原文：西游记 proj_a5d711b0a337，B 库只读查询取得 ----------
# 协调者复验探针命中的假阳性：地名被引号内的对白提到（"被谈论"），旧版本
# 只看"句子里有引号 + 有这个名字"就判 dialogue，误把它们判成"在场"。
XIYOU_PLACE_INSIDE_QUOTE = (
    "又见那洞门紧闭，静悄悄杳无人迹。忽回头，见崖头立一石碑，约有三丈余高，"
    "八尺余阔，上有一行十个大字，乃是「灵台方寸山，斜月三星洞」。"
    "美猴王十分欢喜道：「此间人果是朴实，果有此山此洞。」"
)
XIYOU_LINGXIAO_INSIDE_QUOTE = (
    "猴王听说，心中大怒道：「泼毛神！休夸大口，少弄长舌。我本待一棒打死你，恐无人去报信。"
    "且留你性命，快早回天，对玉皇说：他甚不用贤。老孙有无穷的本事，为何教我替他养马？"
    "你看我这旌旗上字号，若依此字号升官，我就不动刀兵，自然的天地清泰﹔如若不依，"
    "时间就打上灵霄宝殿，教他龙床定坐不成。」这巨灵神闻此言，急睁睛迎风观看，果见门外竖一高竿"
)
XIYOU_HUAGUOSHAN_INSIDE_QUOTE = (
    "猴王道：「弟子乃东胜神洲傲来国花果山水帘洞人氏。」"
    "祖师喝令：「赶出去！他本是个撒诈捣虚之徒，那里修甚么道果！」"
)
XIYOU_ZHONGHOU_SPEAKER_FRAME = (
    "众猴都道：「这股水不知是那里的水。我们今日赶闲无事，"
    "顺涧边往上溜头寻看源流，耍子去耶！」"
)


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER DEFAULT 0)")
    conn.execute("CREATE TABLE chapters(project_id TEXT, idx INTEGER, content TEXT)")
    conn.execute("CREATE TABLE episodes(project_id TEXT, episode_no INTEGER, source_chapters TEXT)")
    conn.execute(
        "CREATE TABLE character_portraits(id TEXT, project_id TEXT, character_name TEXT, ep_start INTEGER, "
        "ep_end INTEGER, appearance TEXT, prompt TEXT, image_path TEXT, base_portrait_id TEXT, "
        "bible_version INTEGER, created_at REAL)")
    return conn


def _seed_project(conn: sqlite3.Connection, chapter_content: str, *, idx: int = 30, episode_no: int = 21) -> None:
    bible = Bible(world=World(visual_style_canonical="写实"),
                  characters=[Character(name="里奥", role="主角",
                                        appearance_canonical="黑发少年，运动装，身形精瘦，深色短裤")])
    conn.execute("INSERT INTO projects(id, bible_json, bible_version) VALUES('p1', ?, 1)",
                 (json.dumps(bible.model_dump(), ensure_ascii=False),))
    conn.execute("INSERT INTO episodes(project_id, episode_no, source_chapters) VALUES('p1', ?, ?)",
                 (episode_no, json.dumps([idx])))
    conn.execute("INSERT INTO chapters(project_id, idx, content) VALUES('p1', ?, ?)", (idx, chapter_content))
    conn.commit()


def _patch_settings(monkeypatch, conn) -> dict:
    settings: dict[str, str] = {}
    patch_portraits_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_portraits_everywhere(monkeypatch, "get_setting", lambda k: settings.get(k))
    patch_portraits_everywhere(monkeypatch, "set_setting", lambda k, v: settings.__setitem__(k, v))
    return settings


# ==================== 1. presence_evidence 纯函数单元测试 ====================

def test_maradona_action_sentence_is_onscreen_evidence() -> None:
    """生产事故复现：马拉多纳先是被盯着看的海报（mention_only），又是"赛后
    哭了"的主教练——"马拉多纳哭了"动词紧邻人名，必须落入 onscreen_mentions。"""
    evidence = collect_presence_evidence("马拉多纳", {1: MESSI_CHAPTER})
    assert has_onscreen_evidence(evidence)
    assert any("马拉多纳哭了" in item["excerpt"] for item in evidence["onscreen_mentions"])
    assert any("海报" in item["excerpt"] for item in evidence["mention_only"])


def test_mbappe_action_sentence_is_onscreen_evidence() -> None:
    evidence = collect_presence_evidence("姆巴佩", {1: MESSI_CHAPTER})
    assert has_onscreen_evidence(evidence)
    assert any("跑过他身边" in item["excerpt"] for item in evidence["onscreen_mentions"])


def test_organization_place_object_do_not_get_false_onscreen_evidence() -> None:
    """"靠山宗矗立在山谷之中"——"矗立"含"立"，是中文写建筑/宗门"站立"的
    常见套路，不能被误判成"这是人"（对照 test_character_discovery.py::
    test_non_person_never_enters_the_character_bible 用的同一形状的夹具）。"""
    evidence = collect_presence_evidence("靠山宗", {1: "靠山宗矗立在山谷之中。" * 6})
    assert not has_onscreen_evidence(evidence)


def test_place_names_mentioned_inside_quoted_speech_stay_mention_only() -> None:
    """协调者复验探针命中的假阳性回归：西游记「弟子乃东胜神洲傲来国花果山
    水帘洞人氏」「…灵霄宝殿…」这类地名整段落在别人台词的引号内部（被谈论，
    不是在场），必须落进 mention_only；只有说话人框架（引号前的"X道："，
    与该名之间没有其它引号）或引号外的动作句才算 onscreen——"众猴都道：
    「…」"这种集体角色的说话人框架必须保住。四句均为生产库原文逐字摘录。"""
    for text, name in [
        (XIYOU_PLACE_INSIDE_QUOTE, "斜月三星洞"),
        (XIYOU_LINGXIAO_INSIDE_QUOTE, "灵霄宝殿"),
        (XIYOU_HUAGUOSHAN_INSIDE_QUOTE, "花果山水帘洞"),
    ]:
        evidence = collect_presence_evidence(name, {4: text})
        assert not has_onscreen_evidence(evidence), f"{name} 不应被判在场"
        assert evidence["mention_only"], f"{name} 应该落进 mention_only"

    zhonghou_evidence = collect_presence_evidence(
        "众猴", {2: XIYOU_ZHONGHOU_SPEAKER_FRAME},
    )
    assert has_onscreen_evidence(zhonghou_evidence)
    assert {"dialogue", "action"} <= set(
        zhonghou_evidence["onscreen_mentions"][0]["evidence_kinds"]
    )


def test_grandmother_qualifies_as_functional_card_worthy() -> None:
    """奶奶：对白+多段出场（"矮墙"意象贯穿全文的角色），够格建卡。"""
    evidence = collect_presence_evidence("奶奶", {1: MESSI_CHAPTER})
    assert functional_card_worthy(evidence)


def test_one_line_passerby_is_not_functional_card_worthy() -> None:
    """一句话路人不建卡：与既有 test_minor_character_is_skipped_and_negatively_
    cached 同一份"路人甲走过"文本，在新证据结构下仍然不够格。"""
    evidence = collect_presence_evidence("路人甲", {21: "路人甲走过。" * 6})
    assert not functional_card_worthy(evidence)


def test_doctor_single_action_mention_is_a_known_limitation() -> None:
    """医生：单段、只有动作没有对白，不够格 functional_card_worthy——已知
    局限（见模块 docstring）：纯结构信号分不清"一次性交代背景"与"叙事
    高潮"，需要更多上下文/未来章节证据才会入谱，这里如实记录这个边界。"""
    evidence = collect_presence_evidence("医生", {1: MESSI_CHAPTER})
    assert has_onscreen_evidence(evidence)
    assert not functional_card_worthy(evidence)


def test_shenmu_old_man_recurs_across_distinct_sentences() -> None:
    evidence = collect_presence_evidence("老人", {2: SHENMU_CHAPTER})
    assert functional_card_worthy(evidence)
    assert evidence["recurrence"]["paragraph_count"] >= 2


def test_shenmu_his_father_recurs_across_two_distinct_scenes() -> None:
    """"辰南想起了他父亲对他说的话："…"" 这类叙述里，"他父亲"前面是"想起
    了"（不是分句边界），说话人框架/言说动词邻接都不算数——协调者复验探针
    命中的假阳性（西游记"出灵霄宝殿道"）根子就是同一种句式："称谓紧邻在
    某个动词后面，但那个动词的主语其实是别人"，"想起了他父亲说"与"出
    灵霄宝殿道"结构同形，不能只因为这次的主语猜对了就放宽判据（那样下次
    猜错的还是会漏判成真）。宁可这一句本身判不出 dialogue/action，也要靠
    "两个不同场景都提到他父亲"这条更硬的结构信号（recurrence≥2）够格。"""
    evidence = collect_presence_evidence("他父亲", {2: SHENMU_CHAPTER})
    assert functional_card_worthy(evidence)
    assert evidence["recurrence"]["paragraph_count"] >= 2


def test_repeated_identical_sentence_does_not_inflate_recurrence() -> None:
    """同一句原文逐字重复（测试夹具常见手法）只算一次证据，不是"出现了几
    次"——真正的叙事反复出场靠不同的句子，不是同一句话的字面复读。"""
    evidence = collect_presence_evidence("路人甲", {1: "路人甲走过。" * 6})
    assert evidence["recurrence"]["paragraph_count"] == 1


def test_quote_with_internal_terminal_punctuation_is_not_split() -> None:
    """引号内部的问号不能把引号切成两半——神墓"我怎么会将父母和这个老人
    联系到一起呢？"这类夹在句中的引述必须保持成一整句，_quote_spans 才能
    正确配对引号，"老人"才能被判定成"引号内部的称谓"。

    这句话本身恰好也是"引号内部提及不算在场"规则的最小例子：句中的"老人"
    整个出现在引号内部（是"这个老人"被谈论，不是说话人本人），必须落进
    mention_only，不是 dialogue——旧版本把"句子里有引号+有这个名字"直接
    判成 dialogue，被西游记探针复验揪出假阳性（见
    test_place_names_mentioned_inside_quoted_speech_stay_mention_only）。"""
    evidence = collect_presence_evidence(
        "老人", {1: "他说：“我怎么会将这个老人联系到一起呢？”他自嘲地笑了笑。"},
    )
    assert evidence["onscreen_mentions"] == []
    assert len(evidence["mention_only"]) == 1
    assert evidence["mention_only"][0]["evidence_kinds"] == []
    # 引号确实被完整识别成了一整句（没有在内部的"？"处被切开）：整句原文
    # （含首尾）都在这一条 mention_only 摘录里，不是被腰斩后的半句。
    assert "他自嘲地笑了笑" in evidence["mention_only"][0]["excerpt"]


def test_shot_tagged_label_counts_as_onscreen_even_without_action_words() -> None:
    """已有分镜时最强信号：分镜台已经把这个标签标成在场角色（characters
    列表命中），即使这句原文本身没有动作/对白邻接。"""
    evidence = collect_presence_evidence(
        "神秘老者", {},
        shot_rows=[{"characters": json.dumps(["神秘老者", "辰南"]),
                    "action_desc": "神秘老者出现在辰南面前", "source_excerpt": ""}],
    )
    assert has_onscreen_evidence(evidence)
    assert evidence["onscreen_mentions"][0]["evidence_kinds"] == ["shot_tagged"]


def test_presence_evidence_fingerprint_changes_when_onscreen_evidence_changes() -> None:
    """负缓存键必须挂在证据指纹上：分镜后来才把这个标签标成在场角色时，
    指纹要变，负缓存才会失效重判——不能只按原文片段文本哈希（那样"分镜后来
    标出了在场证据"这件事永远不会触发重判，见 card_verdict.py docstring）。"""
    before = collect_presence_evidence("神秘老者", {1: "神秘老者的传闻很多，无人知晓真相。"})
    after = collect_presence_evidence(
        "神秘老者", {1: "神秘老者的传闻很多，无人知晓真相。"},
        shot_rows=[{"characters": json.dumps(["神秘老者"]), "action_desc": "", "source_excerpt": "神秘老者现身"}],
    )
    assert presence_evidence_fingerprint(before) != presence_evidence_fingerprint(after)
    assert not has_onscreen_evidence(before)
    assert has_onscreen_evidence(after)


# ==================== 2. ensure_character_card 集成测试 ====================

def test_ensure_character_card_corrects_non_person_verdict_with_onscreen_evidence(monkeypatch) -> None:
    """生产事故复现：模型把马拉多纳误判成 subject_kind=other（proj_ce9fcf749b23
    实测 char_not_character:proj_ce9fcf749b23:马拉多纳=1），但原文有"赛后马拉
    多纳哭了"的动作描写——重判后不能再被判 skipped_not_person。"""
    conn = _make_conn()
    _seed_project(conn, MESSI_CHAPTER)
    _patch_settings(monkeypatch, conn)

    async def fake_assess(name, fragments, *, style, known_names, ep_label, **_kwargs):
        assert "马拉多纳" in fragments
        return {
            "subject_kind": "other", "important": True, "reason": "海报与历史人物提及，非本剧角色",
            "role": "重要配角",
            "appearance_canonical": "银灰色卷发老者，深色运动外套，络腮胡，标志性烟斗手势",
            "personality": "", "speech_style": "", "relationships": [],
        }

    async def fake_no_merge(*_a, **_k):
        return None

    patch_portraits_everywhere(monkeypatch, "assess_new_character", fake_assess)
    patch_portraits_everywhere(monkeypatch, "resolve_card_merge_target", fake_no_merge)

    result = asyncio.run(portraits.ensure_character_card(
        "p1", "马拉多纳", 21, require_identity_card=True,
    ))

    assert result["status"] != "skipped_not_person"
    assert result["status"] == "added"


def test_ensure_character_card_still_rejects_genuine_non_person_without_evidence(monkeypatch) -> None:
    """非回归：没有画面在场证据时，subject_kind 硬闸门原样生效——本次改动
    没有松动 test_non_person_never_enters_the_character_bible 守的那道闸门。"""
    conn = _make_conn()
    _seed_project(conn, "凝气卷是靠山宗发放的修行典籍。" * 6)
    _patch_settings(monkeypatch, conn)

    async def fake_assess(*_a, **_k):
        return {
            "subject_kind": "object", "important": False, "reason": "这是一本书，不是人",
            "role": "重要配角", "appearance_canonical": "", "personality": "", "speech_style": "",
            "relationships": [],
        }

    patch_portraits_everywhere(monkeypatch, "assess_new_character", fake_assess)

    result = asyncio.run(portraits.ensure_character_card("p1", "凝气卷", 21))
    assert result["status"] == "skipped_not_person"


def test_negative_cache_reconsiders_when_new_onscreen_evidence_appears(monkeypatch) -> None:
    """第一次没有在场证据、被判非人并写进负缓存；新章节补上动作描写后，
    第二次调用必须重判，不能被旧的负缓存签名挡住。"""
    conn = _make_conn()
    _seed_project(conn, "神秘人的传闻很多，无人知晓真相。" * 6, idx=30, episode_no=21)
    _patch_settings(monkeypatch, conn)

    calls = {"n": 0}

    async def fake_assess(*_a, **_k):
        calls["n"] += 1
        return {
            "subject_kind": "other", "important": False, "reason": "只是传闻",
            "role": "重要配角", "appearance_canonical": "", "personality": "", "speech_style": "",
            "relationships": [],
        }

    patch_portraits_everywhere(monkeypatch, "assess_new_character", fake_assess)

    first = asyncio.run(portraits.ensure_character_card("p1", "神秘人", 21))
    assert first["status"] == "skipped_not_person"
    assert calls["n"] == 1

    conn.execute(
        "INSERT INTO chapters(project_id, idx, content) VALUES('p1', 31, ?)",
        ("神秘人猛地推开石门，冲了进来，喊了一声。" * 3,),
    )
    conn.commit()

    asyncio.run(portraits.ensure_character_card("p1", "神秘人", 21))
    assert calls["n"] == 2  # 证据变了必须重判，不能被旧的负缓存挡住


# ==================== 3. ensure_cards_for_text 功能身份建卡集成测试 ====================

def test_ensure_cards_for_text_builds_card_for_recurring_functional_identity(monkeypatch) -> None:
    """无名但反复在场/有对白的功能身份（奶奶）应该建卡定妆，不再只留裸
    标签资源——生产事故：奶奶只能以决议存在，从未有卡也没有定妆照。"""
    conn = _make_conn()
    _seed_project(conn, MESSI_CHAPTER)
    _patch_settings(monkeypatch, conn)

    async def fake_assess(name, fragments, *, style, known_names, ep_label, **_kwargs):
        assert name == "奶奶"
        return {
            "subject_kind": "person", "important": True, "reason": "反复出场且有对白",
            "role": "重要配角",
            "appearance_canonical": "花白头发的老年女性，深色朴素外套，身形微驼，慈祥面容",
            "personality": "", "speech_style": "", "relationships": [],
        }

    async def fake_portrait(project_id, name, style, appearance, *, ep_start, bible_version):
        return {"portrait_id": "pt1", "ref_image_path": f"/tmp/{name}.jpg"}

    async def fake_no_merge(*_a, **_k):
        return None

    patch_portraits_everywhere(monkeypatch, "assess_new_character", fake_assess)
    patch_portraits_everywhere(monkeypatch, "_generate_discovered_character_portrait", fake_portrait)
    patch_portraits_everywhere(monkeypatch, "resolve_card_merge_target", fake_no_merge)

    bible = Bible.model_validate(json.loads(
        conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"],
    ))
    candidates = [{
        "identity_kind": "functional", "source_label": "奶奶", "name": "奶奶",
        "identity_group": "source:奶奶",
    }]
    result = asyncio.run(cards_ensure.ensure_cards_for_text(
        "p1", 21, MESSI_CHAPTER, bible, _precomputed_candidates=candidates,
    ))
    assert "奶奶" in [item["name"] for item in result["added"]]


def test_ensure_cards_for_text_does_not_build_card_for_one_line_functional_extra(monkeypatch) -> None:
    """一句话的功能性群演（既无对白也无明显动作）不应该触发建卡尝试——
    连 assess_new_character 都不该被调用（避免为每个偶然命中的路人烧模型
    调用），仍然只留一条 functional_identity 决议。"""
    conn = _make_conn()
    _seed_project(conn, "路人甲走过。" * 6)
    _patch_settings(monkeypatch, conn)

    calls = {"n": 0}

    async def fake_assess(*_a, **_k):
        calls["n"] += 1
        raise AssertionError("一句话路人不应该触发人物卡评估")

    patch_portraits_everywhere(monkeypatch, "assess_new_character", fake_assess)

    bible = Bible.model_validate(json.loads(
        conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"],
    ))
    candidates = [{
        "identity_kind": "functional", "source_label": "路人甲", "name": "路人甲",
        "identity_group": "source:路人甲",
    }]
    result = asyncio.run(cards_ensure.ensure_cards_for_text(
        "p1", 21, "路人甲走过。" * 6, bible, _precomputed_candidates=candidates,
    ))
    assert calls["n"] == 0
    assert result["added"] == []
    assert any(
        item.get("resolution") == "functional_identity" for item in result["resolutions"]
    )


def test_ensure_cards_for_text_skips_card_when_functional_label_collides() -> None:
    """同一 source_label 命中 ≥2 个不同 identity_group（真实歧义，比如两个
    不同的"外宗弟子"）时不建卡——裸文本检索分不清是哪一个人，宁可只留
    route_name 消歧决议，不能把两个人的证据混进同一张卡。"""
    functional_candidates = [
        {"identity_kind": "functional", "source_label": "外宗弟子", "name": "外宗弟子",
         "identity_group": "current-1:F1"},
        {"identity_kind": "functional", "source_label": "外宗弟子", "name": "外宗弟子",
         "identity_group": "current-2:F9"},
    ]
    resolutions = cards_ensure._functional_identity_resolutions(functional_candidates)
    route_names = {item["canonical_name"] for item in resolutions}
    assert len(route_names) == 2  # 两个不同的人拿到了两个不同的 route_name
