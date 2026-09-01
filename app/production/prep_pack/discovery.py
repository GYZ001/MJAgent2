"""New-character/new-scene discovery: loading the project bible and driving
app.portraits/app.scenes discovery for mentions that do not resolve against
the existing bible.

Split out of app/production/prep_pack.py.
"""
from __future__ import annotations

import json
from app.schemas import Bible
from typing import Any

from .contracts import (
    _FALLBACK_VISUAL_STYLE,
    _FUNCTIONAL_RESOLUTION_KINDS,
)


def _load_project_bible(conn, project_id: str) -> Bible:
    row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    raw = (row["bible_json"] or "").strip() if row else ""
    if raw:
        return Bible.model_validate(json.loads(raw))
    return Bible.model_validate({
        "characters": [], "scenes": [],
        "world": {"era": "", "genre": "", "visual_style_canonical": _FALLBACK_VISUAL_STYLE},
    })


def _character_discovery_dispositions(
    discovery_result: dict[str, Any],
) -> tuple[set[str], dict[str, str], set[str]]:
    """Turn app.portraits.ensure_cards_for_text's result into lookup aids for
    the second resolution pass:
    - skip_names: mentions the discovery mechanism itself (not this file)
      determined need no character card/portrait -- typed functional identity,
      stable reference-only identity, or a ``skipped`` disposition. Recorded
      as a functional extra (unless also in non_person_names), not silently
      dropped -- see _resolve_assets.
    - rename_map: mentions whose confirmed real name differs from the event
      chain's raw mention text (e.g. a title resolved to the true name),
      re-keyed by that real name instead.
    - non_person_names: the subset of skip_names discovery explicitly judged
      is not a person at all (``skipped_not_person`` -- a sect/artifact/pen
      name the chunk extractor mistakenly listed as a character). These are
      still legally skip-able (no portrait required) but must NOT show up in
      functional_extras, which is a list of *people* in frame for P1
      storyboard prompts, not a dumping ground for every non-card mention.
    These only match by exact string equality against discovery's own
    source_label/name, which is a *different* model call's phrasing of the
    same source text and will not always coincide with prep_pack's chunk-
    extraction phrasing (real EP13 case: discovery resolved "外宗弟子" while
    the published chunk extraction said "一名外宗弟子" -- same real-world
    concept, different string). A name this misses is not necessarily
    unclassified; see _resolve_assets' functional-extra default and
    _discovery_errored_names for what actually still blocks.
    """
    skip_names: set[str] = set()
    non_person_names: set[str] = set()
    for item in discovery_result.get("skipped") or []:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        skip_names.add(name)
        if str(item.get("status") or "").strip() == "skipped_not_person":
            non_person_names.add(name)
    rename_map: dict[str, str] = {}
    for item in discovery_result.get("resolutions") or []:
        source_label = str(item.get("source_label") or "").strip()
        canonical_name = str(item.get("canonical_name") or "").strip()
        resolution = str(item.get("resolution") or "").strip()
        if not source_label:
            continue
        if resolution in _FUNCTIONAL_RESOLUTION_KINDS:
            skip_names.add(source_label)
        elif canonical_name and canonical_name != source_label:
            rename_map[source_label] = canonical_name
    return skip_names, rename_map, non_person_names


def _discovery_errored_names(
    discovery_result: dict[str, Any], candidate_names: list[str],
) -> set[str]:
    """Which of *our* raw mention strings discovery explicitly failed on.

    ensure_cards_for_text's own error strings are name-prefixed
    ("{name}：原因", app/portraits.py:7383/7407) but not schema-guaranteed, so
    this checks containment against each of our own candidate names rather
    than trying to parse discovery's message format -- a name only lands here
    if discovery said something concrete *about that name*, e.g. "身份模型已
    确认真名，但人物卡模型未返回完整稳定卡片" (a confirmed real identity
    whose card generation itself failed -- a real defect, must block) or an
    exception during its own processing. This is deliberately the one thing
    _resolve_assets still hard-blocks on after discovery runs; everything
    else defaults to a functional extra (see its docstring).
    """
    messages = [str(message) for message in discovery_result.get("errors") or []]
    if not messages:
        return set()
    return {
        name for name in candidate_names
        if name and any(name in message for message in messages)
    }


async def _discover_new_characters(
    conn, *, project_id: str, episode_id: str, episode_no: int,
    source_text: str, discovery_text: str, run_id: str | None,
) -> dict[str, Any]:
    """谱外新角色 → 发现 → 补录人物谱 → 生成定妆照。

    Reuses app.portraits' identity-discovery machinery as-is (does not
    reimplement it): importance = source chapters + CHARACTER_IMPORTANCE_
    FORWARD_CHAPTERS, true-name resolution = its own independent
    IDENTITY_DISCOVERY_FORWARD_CHAPTERS window (portraits.py:384-385), and the
    spoiler rule that forward context may only resolve an already-appeared
    identity's stable name, never pull future plot into this episode
    (ensure_cards_for_text -> discover_character_candidates docstrings). Only
    called when pass 1 of ``_resolve_assets`` below leaves a real,
    non-background-extra character mention unresolved -- see the zero-call
    regression assertion in tests/test_prep_pack_asset_discovery.py.

    ``discovery_text`` (2.0.4, paratext 归一，见 PREP_PACK_VERSION 上方
    2.0.4 大注释): precomputed by the caller from persisted
    ``chapters.paratext_json`` -- this function no longer calls
    ``strip_paratext`` itself (that was a second, independent model
    judgment of the exact same question the world bible already answers
    for any chapter it has scoped). Only the discovery-facing copy is
    stripped; ``source_text`` itself (event-chain evidence elsewhere, and
    this call's own identity-scope fingerprint below) is untouched.
    """
    from app.portraits import (
        ensure_cards_for_text,
        persist_screenplay_character_resolutions,
        screenplay_identity_scope_fingerprint,
    )

    bible = _load_project_bible(conn, project_id)
    # generate_portraits=False：出图从映射台解耦到后台（实测出图占映射台约
    # 三分之二的供应商时间，EP1 image 469.6s / 全部 725.9s，映射墙钟 611s，
    # 用户按下"映射"要干等十分钟）。映射台只负责发现→建卡→绑别名这些纯文本
    # 工作；定妆照交给下面的后台任务，发起付费视频前由生成台的参考图就绪校验
    # 兜底（_assert_shot_generation_gate / 整集入口的 asset_gaps）。
    result = await ensure_cards_for_text(
        project_id, episode_no, discovery_text, bible, generate_portraits=False,
    )
    persist_screenplay_character_resolutions(
        conn, episode_id, result.get("resolutions") or [],
        retire_legacy_future_identity=True,
        expected_active_run_id=run_id,
        replace_identity_scope=screenplay_identity_scope_fingerprint(episode_no, source_text),
    )
    return result


async def _discover_new_scenes(
    conn, *, project_id: str, episode_no: int, labels: list[str],
) -> dict[str, Any]:
    """谱外新场景 → 发现 → 补录场景库 → 生成场景参考图。

    Reuses app.scenes' reactive scene-discovery machinery as-is via
    ``ensure_scenes_for_labels`` (a thin adapter added alongside
    ``ensure_scenes_for_storyboard`` for callers, like this one, that have a
    flat label list instead of a compiled screenplay object -- same
    assess_new_scene/_generate_and_register_scene functions underneath, no
    discovery logic duplicated). Only called when pass 1 below leaves a scene
    mention unresolved.
    """
    from app.scenes import ensure_scenes_for_labels

    return await ensure_scenes_for_labels(project_id, episode_no, labels)


# ---------- 未解析角色标签候选判别（1.8.0，见 PREP_PACK_VERSION 上方大注释
# 的完整案情）：用户原始诉求——同一角色在不同集换脸，真名揭晓前人物建模
# 持续漂移。真实 EP1 现场：标签"银色长袍女子"本该绑定许清（appearance_
# canonical 明确写着"常年穿银色长袍"，人物谱已登记确认别名"许师姐"，本集
# 原文两次出现"许师姐"），却因为标签类型对不上（模型给出场角色起的是外貌
# 描述，别名库登记的是称谓）落 functional_extras 当无图群演。
#
# 根因不是别名机制坏了——是这类"既查不到 portrait、也命中不了别名"、即将
# 落入 functional_extras 的标签，从未真正过一遍"人物谱里有没有人已经在
# 本集原文里跟它共现"的判别。skip_character_names 的两条既有来源（discovery
# 自己判定 skip、以及 _resolve_assets 下方"Coordinator-mandated default"
# 兜底）都只回答了"这不是一个可以直接建卡的新角色"，从未回答这个问题。
#
# 修复范式完全复用 app/stages.py 当晚落地的别名裁决庭三段式（_alias_
# verdict_dossier / _alias_verdict_candidates / _alias_verdict_call /
# _alias_verdict_pin_segment：代码检索卷宗 → 候选判别 → 段号钉证），但作用
# 域收窄到本集自己的 source_text——prep_pack 不需要 stages.py 那样跨全书找
# "桥接章"：这里的候选与证据都只在本集范围内找，找不到就维持原行为落群演，
# 不做跨集检索，跟"确定性、零语义"的既有纪律一致。两个模块不允许互相导入
# 内部函数（保持边界干净），本节是同一范式的独立实现，不是重构共享：
#   1) 候选集（代码，零语义，_prep_pack_functional_candidate_names）：本集
#      source_text 里规范名或已确认别名有字面命中的人物谱角色。不针对任何
#      具体人名/姓氏做特判（真实误登记事故教训，见 stages.py 同名注释）；
#      候选集为空直接维持原行为，不发起任何模型调用。
#   2) 卷宗（代码，零语义，_prep_pack_functional_candidate_dossier）：按
#      自然段切分本集原文，覆盖全部候选各自的出场证据——不能只收集被测
#      标签周围的证据，那会让下一步的选择题名存实亡（stages.py 已验证的
#      真实教训：模型看不到正确候选的材料，只能靠反复出现的候选拍脑袋）。
#      1.8.1 起卷宗主锚点改为事件跨度定位，见该函数与 _prep_pack_
#      functional_candidate_event_span_segments 的完整说明（下面单独一段）。
#   3) 裁决（模型，唯一一次调用，_prep_pack_functional_candidate_call）：
#      候选选择题——"标签 X 最可能指候选中的哪一位"，候选集之外强制一个
#      "都不是/无法确定"选项，schema 用 enum 收紧到候选集与卷宗段号。不是
#      "标签是不是候选 A"的是非题（stages.py 已验证是非题诱发确认偏误：
#      模型看到反复出现的某个候选会不自觉地倾向他，跟他是不是正确答案
#      无关）。
#   4) 钉证（代码，结构性，_prep_pack_functional_candidate_pin_segment）：
#      模型只需引用卷宗目录里的段号，不比对模型转录的逐字引句——今晚已
#      证明那种比对方式会因转录波动（跨段拼接/省略号/标点微调）误杀正确
#      判定，钉证退化为"选中的段号是否落在卷宗集合内"这一结构性判断。
# 选中候选集里的真实一员、且段号钉证通过、且这个候选在本集确有已生成的
# 定妆照（复用既有 _resolve_portrait_id，不重复实现一遍"有没有图"的判断）、
# 且这次改名不会与跨集别名注册表冲突（复用既有 _prep_pack_cross_episode_
# alias_conflict，同一套"不确定不绑"纪律），才把这个标签重新计入
# character_rename——调用点见 _resolve_assets 内 "Coordinator-mandated
# default" 循环之后。选了"都不是/无法确定"、选了候选集之外的值（协议层
# 已经不可能，代码侧仍做防御性核验）、卷宗为空、候选没有可用定妆照、或
# 存在跨集别名冲突，一律返回 None——调用方维持原行为，标签留在
# skip_character_names 正常落 functional_extras，绝不猜。
#
# 严禁任何具体人名/称谓的硬编码特判；严禁外貌关键词模糊匹配（"绿袍男子"
# 这类外貌描述在长篇小说里能撞上一大片人，模糊匹配就是下一个误绑事故）——
# 本节全程只用"人物谱角色的规范名/已确认别名是否逐字命中原文"这一结构判据
# 构造候选与卷宗，谁是正确答案完全交给模型基于真实原文独立判别。
#
# 1.8.1（真实数据、已完整诊断的后续事故）：上面 1.8.0 机制本身工作正常
# （EP1 实测 10 次调用全部 OK），但目标案例仍然失败——标签"银色长袍女子"→
# 候选集正确含"许清"→模型却答"都不是/无法确定"，因为卷宗（2)步骤检索出
# 的段落里根本没有任何相关证据：`label in seg.text` 逐字匹配"银色长袍女子"
# 在原文里 0 次命中（原文写的是"穿着一身银色长袍"，模型转述成了这个标签，
# 不是原文字面），both/text_only 两类因此全空；候选锚点段落（anchor_only）
# 在失去参照点后退化成文档顺序，主角"孟浩"几乎每段都出现的开篇独白段落
# 吃光了卷宗预算，"许师姐"（许清的已确认别名，紧邻"银袍女子被绿袍男子
# 称许师姐"这一幕）那两段根本没进卷宗——这正是 stages.py._alias_verdict_
# dossier docstring 里写明要防的"主角淹没预算"陷阱，prep_pack 这侧因为缺
# 标签锚点而失效。修法：卷宗主锚点改用事件跨度定位而非标签字面匹配——见
# _prep_pack_functional_candidate_event_span_segments（标签所属事件的
# source_span 覆盖段落，事件链抽取模型必须为每个事件声明这个字段，不依赖
# 标签措辞是否逐字命中原文）与 _prep_pack_functional_candidate_dossier
# 改造后的两层主锚点 + 候选锚点段落按"离事件跨度的邻近度"补足预算（详见
# 两个函数各自的完整 docstring）。label 逐字命中原文这条路径继续保留、
# 不因为改用事件定位就丢弃（有些标签确实是原文用词）；事件跨度缺失/为空
# 时防御性退回 1.8.1 之前的既有行为，不崩。
#
# 1.8.2/1.8.3（同一晚同一事故的第二、三层根因）：完整案情见 PREP_PACK_
# VERSION 上方对应版本号大注释，不在这里重复——概括地说，1.8.2 把 A/B 两侧
# 保底配额下沉到卷宗预算分配层，1.8.3 进一步把 B 侧保底粒度下沉到"每个
# 候选"、字数预算也按同样粒度兜底（保底段一律收录，超限做确定性截断而非
# 整段丢弃），并把候选集从"只看本集原文逐字命中"扩展为"逐字命中 ∪ 人物谱
# 注册区间覆盖本集"两类并集。


