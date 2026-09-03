"""场景粒度判据（WS6）：画面可共用性——同一物理地点+同一年代/时期为可共用的锚点
场景，一句带过/抒情提及不建独立场景。覆盖 app.production.scene_granularity 的
纯函数（提示词构造、模型输出解析、粒度标签编解码、结构化去重）与 app.scenes 的
接线（assess_new_scene 新签名、ensure_scenes_for_storyboard/ensure_scenes_for_labels
的 transitional 分支）。

跨项目回归 fixture（B 上 2026-09-02 只读实测，见 WS6 派单里的实测数据）：
《跑不快的孩子》真实产出过「世界杯赛场」一个场景代表 2006/2010/2018/2022 四届
完全不同的世界杯，且「柏林城市外景」「第三个街口」「罗马城外景」「校园食堂」
各自独立成场景——但只有「世界杯赛场」「校园食堂」「罗萨里奥土场」三个被分镜
真正引用过。三国/西游同样只有少数场景被引用。这些名单用来验证"新判据不会让
已经被分镜引用的场景丢失"（不倒退）：本次改动不做任何回填/迁移，只影响未来新
判定的场景，所以这条不变量在结构上必然成立——用真实名单钉住这个结构性事实，
而不是空口断言。
"""
from __future__ import annotations

from app.production.scene_granularity import (
    ROLE_ANCHOR,
    ROLE_TRANSITIONAL,
    anchor_discovery_sources,
    decode_granularity_tag,
    encode_granularity_tag,
    find_anchor_by_location,
    resolve_existing_anchor_name,
    resolve_scene_granularity_verdict,
    scene_granularity_prompt,
)
from app.schemas import Scene

# ---------------------------------------------------------------------------
# 真实生产数据 fixture（B，只读查询，2026-09-02）
# ---------------------------------------------------------------------------

# 三国演义_白话文版：scene_references 24 个，被 shots.scene_name 真正引用的 6 个。
SANGUO_USED_SCENE_NAMES = [
    "兵器打造工坊", "安喜县城郊", "平原县城郭", "张飞庄上", "桃园庄院", "涿县郊野",
]

# 西游记：scene_references 15 个，被 shots.scene_name 真正引用的 4 个。
XIYOUJI_USED_SCENE_NAMES = [
    "南赡部洲地界", "斜月三星洞", "洞天深处秘境", "花果山水帘洞",
]

# 跑不快的孩子：scene_references 17 个，被 shots.scene_name 真正引用的 3 个
# （「校园食堂」即派单描述的真实误配场景——被使用不代表绑定正确，见覆盖账本
# 诚实性测试 tests/test_coverage_ledger_honesty.py）。
PAOBUKUAI_USED_SCENE_NAMES = ["世界杯赛场", "校园食堂", "罗萨里奥土场"]

# 跑不快的孩子未被任何分镜引用的场景（过度细分的典型：一句带过/抒情提及独立
# 成场，与「世界杯赛场」这一个该分却没分的反例并存）。
PAOBUKUAI_UNUSED_SCENE_NAMES = [
    "医院场景", "学生宿舍场景", "室内更衣室", "家庭室内场景", "巴塞罗那发布会现场",
    "布宜诺斯艾利斯机场", "柏林城市外景", "第三个街口", "网球俱乐部餐厅", "罗马城外景",
    "美洲杯赛场", "诺坎普球场场景", "颁奖活动现场", "马拉卡纳球场场景",
]


def _legacy_scenes(*names: str) -> list[Scene]:
    """构造没有粒度标签的旧场景（B 上的真实历史数据都是这种形状：discovery_sources
    要么是空，要么是一句原文摘录，从未写过 encode_granularity_tag 编码的标签）。"""
    return [
        Scene(name=name, scene_canonical=f"{name}的固定空间结构与光线陈设锚点，画面环境稳定")
        for name in names
    ]


def test_real_used_scenes_have_no_granularity_tag_so_can_never_be_auto_merged() -> None:
    """不倒退的结构性证明：本次改动不做迁移/回填，B 上真实存在、已被分镜引用的
    场景（三国/西游/跑不快三个项目）此刻都没有粒度标签——find_anchor_by_location
    只匹配带标签的场景，untagged 场景永远返回 None，所以它们不可能被新逻辑
    误判为"已有锚点"而合并/降级掉，一个都不会丢。"""
    all_used = [
        *SANGUO_USED_SCENE_NAMES, *XIYOUJI_USED_SCENE_NAMES, *PAOBUKUAI_USED_SCENE_NAMES,
    ]
    scenes = _legacy_scenes(*all_used)
    for scene in scenes:
        assert decode_granularity_tag(scene.discovery_sources) is None
        # 任何候选 location_key 都不可能通过结构化去重指向这些旧场景。
        assert find_anchor_by_location(scene.name, "", scenes) is None
        assert find_anchor_by_location(scene.name, "2022年", scenes) is None


def test_unused_scenes_are_also_left_untouched_by_construction() -> None:
    """同上，覆盖跑不快里"该分却没分"以外、"不该分却分了"的那一半真实数据
    （柏林城市外景/第三个街口/罗马城外景等一句带过型场景）：即便它们是本次
    判据本该识别为 transitional 的候选，既有行数据同样没有标签，不会被结构化
    去重动到——本次改动只管未来新判定，不碰存量。"""
    scenes = _legacy_scenes(*PAOBUKUAI_UNUSED_SCENE_NAMES)
    for scene in scenes:
        assert find_anchor_by_location(scene.name, "", scenes) is None


# ---------------------------------------------------------------------------
# 粒度标签编解码
# ---------------------------------------------------------------------------


def test_encode_decode_granularity_tag_roundtrip() -> None:
    tag = encode_granularity_tag("卢赛尔球场", "2022年", ROLE_ANCHOR)
    decoded = decode_granularity_tag([tag])
    assert decoded == {"location_key": "卢赛尔球场", "era_anchor": "2022年", "role": ROLE_ANCHOR}


def test_decode_granularity_tag_returns_none_without_tag() -> None:
    assert decode_granularity_tag(["雨中第三个街口，一句带过"]) is None
    assert decode_granularity_tag([]) is None
    assert decode_granularity_tag(None) is None


def test_anchor_discovery_sources_carries_both_excerpt_and_tag() -> None:
    sources = anchor_discovery_sources("原文摘录", "卢赛尔球场", "2022年")
    assert sources[0] == "原文摘录"
    assert decode_granularity_tag(sources) == {
        "location_key": "卢赛尔球场", "era_anchor": "2022年", "role": ROLE_ANCHOR,
    }


# ---------------------------------------------------------------------------
# find_anchor_by_location / resolve_existing_anchor_name：
# 「同一 location_key 不同 era_anchor 必须是不同场景」的结构化保证
# ---------------------------------------------------------------------------


def test_find_anchor_by_location_requires_same_era_not_just_same_location() -> None:
    """世界杯赛场缺陷的结构化回归：柏林 2006 决赛与卢赛尔 2022 决赛是同一
    location_key（"世界杯决赛球场"泛称）时，era_anchor 不同必须判不同场景。"""
    berlin_2006 = Scene(
        name="柏林奥林匹克球场",
        scene_canonical="2006年柏林奥林匹克球场，室外足球决赛场地，夜晚灯光璀璨",
        discovery_sources=anchor_discovery_sources("2006年世界杯决赛在柏林举行", "世界杯决赛球场", "2006年"),
    )
    scenes = [berlin_2006]
    # 同 location_key，不同 era_anchor：不得复用柏林 2006 的场景。
    assert find_anchor_by_location("世界杯决赛球场", "2022年", scenes) is None
    # 同 location_key 且同 era_anchor：应当命中。
    assert find_anchor_by_location("世界杯决赛球场", "2006年", scenes) == "柏林奥林匹克球场"
    # era_anchor 留空时的候选（原文没给年份）不会误配到某个特定届次。
    assert find_anchor_by_location("世界杯决赛球场", "", scenes) is None


def test_find_anchor_by_location_ignores_transitional_scenes() -> None:
    """role=transitional 的历史标签（理论上不该出现在独立场景行里，但防御性核验）
    不参与去重匹配——去重只信任 anchor。"""
    scenes = [Scene(
        name="误建的过场",
        scene_canonical="误当成锚点建库的过场地点，画面环境描述占位",
        discovery_sources=["一句带过", encode_granularity_tag("某地", "2010年", ROLE_TRANSITIONAL)],
    )]
    assert find_anchor_by_location("某地", "2010年", scenes) is None


def test_resolve_existing_anchor_name_prefers_structural_match_over_model_claim() -> None:
    """确定性去重优先于模型自报的 existing_scene_name——即使模型没有正确判断出
    这是已有场景（existing_scene_name 留空/答错），结构化标签命中时仍然并入。"""
    anchor = Scene(
        name="诺坎普球场",
        scene_canonical="巴塞罗那诺坎普球场外景，翠绿草坪与看台",
        discovery_sources=anchor_discovery_sources("诺坎普原文依据", "诺坎普球场", ""),
    )
    result = resolve_existing_anchor_name(
        location_key="诺坎普球场", era_anchor="", existing_scene_name="", scenes=[anchor],
    )
    assert result == "诺坎普球场"


def test_resolve_existing_anchor_name_falls_back_to_model_claim_for_legacy_scenes() -> None:
    """旧场景没有标签时，仍然尊重模型自己给出的 existing_scene_name（向后兼容
    既有"时段别名"判定路径，例如 白日神魔陵园/夜晚神魔陵园 这类历史数据）。"""
    legacy = _legacy_scenes("白日神魔陵园")
    result = resolve_existing_anchor_name(
        location_key="神魔陵园", era_anchor="", existing_scene_name="白日神魔陵园", scenes=legacy,
    )
    assert result == "白日神魔陵园"


def test_resolve_existing_anchor_name_returns_none_when_nothing_matches() -> None:
    result = resolve_existing_anchor_name(
        location_key="全新地点", era_anchor="", existing_scene_name="", scenes=_legacy_scenes("无关场景"),
    )
    assert result is None


# ---------------------------------------------------------------------------
# resolve_scene_granularity_verdict：模型输出解析与核验
# ---------------------------------------------------------------------------


def test_transitional_role_forces_important_false_even_if_model_says_true() -> None:
    """role=transitional 是硬约束：即使模型把 important 错填成 true，也不能建场景
    ——一句带过的地点不需要独立画面，见模块 docstring"画面可共用性判据"。"""
    verdict = resolve_scene_granularity_verdict(
        {
            "important": True, "role": "transitional", "name": "第三个街口",
            "scene_canonical": "国漫3D动画电影质感的室外街景，坐落于第三个街口，"
                                "设有路牌与行道树，白日柔和自然光铺洒，精致写实光影渲染",
            "location_key": "第三个街口", "era_anchor": "",
            "anchor_phrase": "雨中撑伞在第三个街口停下",
        },
        label="第三个街口", spatial_context="雨中撑伞在第三个街口停下，随即继续赶路",
        canonical_min=30, canonical_max=80,
    )
    assert verdict.important is False
    assert verdict.role == ROLE_TRANSITIONAL
    assert verdict.anchor_phrase == "雨中撑伞在第三个街口停下"  # 逐字命中，核验通过


def test_invalid_role_defaults_to_anchor_not_dropped() -> None:
    """role 缺省/非法（例如模型没按 schema 返回）时按更安全的一侧兜底为 anchor
    ——漏建一个真实场景比多建一个候选风险更高，两害相权取其轻，不是臆造。"""
    verdict = resolve_scene_granularity_verdict(
        {"important": True, "role": "", "name": "新场景", "scene_canonical": "X" * 40},
        label="新场景", spatial_context="随便什么原文", canonical_min=30, canonical_max=80,
    )
    assert verdict.role == ROLE_ANCHOR
    assert verdict.important is True


def test_anchor_phrase_unverified_against_spatial_context_is_dropped() -> None:
    """anchor_phrase 必须真的逐字命中原文依据，编造的依据不予采信、清空——
    空着比编一个假依据诚实。"""
    verdict = resolve_scene_granularity_verdict(
        {
            "important": True, "role": "anchor", "name": "新场景",
            "scene_canonical": "X" * 40, "anchor_phrase": "原文里根本没有的一句话",
        },
        label="新场景", spatial_context="原文只写了这一句真实内容", canonical_min=30, canonical_max=80,
    )
    assert verdict.anchor_phrase == ""


def test_scene_canonical_too_short_forces_important_false() -> None:
    """既有口径不变：锚点串太稀薄（低于 canonical_min）不足以稳定定场，即使
    role=anchor 也要退回 important=False。"""
    verdict = resolve_scene_granularity_verdict(
        {"important": True, "role": "anchor", "name": "新场景", "scene_canonical": "太短了"},
        label="新场景", spatial_context="原文", canonical_min=30, canonical_max=80,
    )
    assert verdict.important is False


def test_location_key_defaults_to_name_when_model_omits_it() -> None:
    verdict = resolve_scene_granularity_verdict(
        {"important": True, "role": "anchor", "name": "宗门广场", "scene_canonical": "X" * 40},
        label="宗门广场", spatial_context="原文", canonical_min=30, canonical_max=80,
    )
    assert verdict.location_key == "宗门广场"


# ---------------------------------------------------------------------------
# scene_granularity_prompt：正面陈述覆盖判据的四个字段 + 已有场景带上锚点串
# ---------------------------------------------------------------------------


def test_prompt_includes_era_anchor_and_role_positive_statements() -> None:
    prompt = scene_granularity_prompt(
        "世界杯赛场", "2022年卡塔尔世界杯决赛在卢赛尔球场举行",
        style="国漫3D", style_rule="必须贴合画风", known_scenes=[("世界杯赛场", "泛用的世界杯球场锚点串")],
        ep_label="第 10 集", canonical_min=30, canonical_max=80,
        same_location_match_rule="时段差异由分镜用光表达，不构成新场景",
    )
    assert "location_key" in prompt
    assert "era_anchor" in prompt
    assert "role" in prompt
    assert "anchor_phrase" in prompt
    assert "同一 location_key 若 era_anchor 不同" in prompt
    assert "泛用的世界杯球场锚点串" in prompt  # 已有场景带上锚点串本身，不只是名字
    assert "时段差异由分镜用光表达，不构成新场景" in prompt


def test_prompt_no_known_scenes_renders_placeholder() -> None:
    prompt = scene_granularity_prompt(
        "新地点", "原文", style="国漫3D", style_rule="必须贴合画风", known_scenes=[],
        ep_label="第 1 集", canonical_min=30, canonical_max=80, same_location_match_rule="口径",
    )
    assert "（无）" in prompt
