"""New-character/new-scene/new-prop discovery: loading the project bible and
driving app.portraits/app.scenes/app.props discovery for mentions that do not
resolve against the existing bible, plus the prop-manifest builder.

Split out of app/production/prep_pack.py. ``_prep_pack_build_prop_manifest``
moved here from ``.resolve_assets`` (2026-09-28, that file's line-count
baseline was already pinned at its exact current value in
FILE_CONVENTIONS.toml -- this module still has room).
"""
from __future__ import annotations

import json
import logging
from app.schemas import Bible, Prop
from app.source_excerpt import SourceSegment
from typing import Any, Sequence

from .contracts import (
    _FALLBACK_VISUAL_STYLE,
    _FUNCTIONAL_RESOLUTION_KINDS,
)
from .provenance import _prep_pack_provenance
from .trailing_anchor import prop_literal_or_trailing_anchor, trailing_anchor_phrase

log = logging.getLogger(__name__)


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
    conn, *, project_id: str, episode_no: int, labels: list[str], segments: list | None = None,
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
    from app.production.scene_evidence import evidence_by_label
    from app.scenes import ensure_scenes_for_labels

    # 判定要看本集原文里含该地点的段落，不只看标签（第 12/13 轮场景库近重复的根因）
    return await ensure_scenes_for_labels(project_id, episode_no, labels, evidence=evidence_by_label(labels, segments or []))


async def _discover_new_props(
    conn, *, project_id: str, episode_no: int, props_payload: list[dict[str, Any]], source_text: str,
) -> list[dict[str, Any]]:
    """道具库反应式登记（2026-09-03 新增）：抽取出的 props 项转交
    ``app.props.ensure_props_for_labels`` 判定是否"关键道具"、补外观锚点与
    参考图。跟角色/场景发现的关键区别——道具库是人物/场景库之外的**增量**
    能力（用户投诉"道具形态漂移"后新加，不是资产解析的既有硬门槛）：失败
    绝不能反过来挡住映射台本身发布，所以这里吞掉全部异常，只落 advisory 日志
    （不是静默吞——CLAUDE.md 禁止"删核心功能掩盖问题"那一条针对的是隐藏真正
    阻塞发布的错误；这里的错误对映射台产物结构没有任何影响，本来就不该阻塞）。
    ``conn`` 参数与 ``_discover_new_scenes`` 保持同一调用形状，但
    ``ensure_props_for_labels`` 内部自己开 ``get_conn()``（同 ``ensure_scenes_
    for_labels``），不复用这个 conn——道具库落库不依赖调用方事务是否提交成功。
    原样返回 ``props_payload``（本函数只登记道具库，不改变 asset_manifest.props
    本身的形状）——调用方 ``_resolve_assets`` 借这个 pass-through 返回值把登记
    动作内联进赋值表达式，省去多一条独立语句（该文件行数基线已顶格，见
    ``app/FILE_CONVENTIONS.toml`` 的棘轮说明）。
    """
    # 延迟导入：避免给 app.production.prep_pack（映射台核心链路）加一条模块级
    # 常驻依赖到 app.props 的模型/出图调用链，与相邻 _discover_new_characters/
    # _discover_new_scenes 的既有写法保持一致（同一文件里两个函数都是函数内
    # import）。
    from app.props import ensure_props_for_labels

    del conn
    try:
        result = await ensure_props_for_labels(project_id, episode_no, props_payload, source_text=source_text)
    except Exception:  # noqa: BLE001 道具库登记失败不得阻断映射台发布，见上方 docstring
        log.exception(
            "道具库登记失败，映射台继续正常发布 project=%s episode_no=%s",
            project_id, episode_no,
        )
        return props_payload
    for message in result.get("errors") or []:
        log.warning(
            "道具库登记出现单条失败 project=%s episode_no=%s: %s",
            project_id, episode_no, message,
        )
    return props_payload


# 2.0.0 新增：道具没有世界书图像素材库，不需要身份消歧/发现，也不需要
# suspected_true_name 声明-核验通道——一个道具就是它自己（结构判据，零
# 语义），按 label 精确字符串去重合并 segment_indexes 即可。
#
# 道具的 label 真的逐字出现在该段落原文里时，走跟角色侧"称谓证据闸"同一判据
# 的"裸直接命中"（method="direct"，_prep_pack_gate_segment_indexes 的结构闸
# 不做这一步是因为它对全部三种资产统一处理、且要给 characters/scenes 的解析
# 路径留豁免空间——道具没有这个豁免需求，在这里单独把关不冲突）。
#
# 2026-09-30 新增第二条独立判据（method="card_match"）：道具现在也有一条"经
# 解析路径绑定可豁免逐字"的路，同 characters/scenes 的别名解析同一先例——
# 世界书已有道具卡与这条提及存在绑定关系（判据本身见 app.props.card_match.
# match_existing_prop_card 模块 docstring「模型提名、代码核验」一节：既可能
# 是模型经 known_prop_name 明确提名 + 代码核验证据，也可能是卡名/别名本身
# 逐字出现在证据里并与 label 存在包含关系）时，这条提及绑定到那张卡：
# ``canonical_name`` 记卡的规范名，``label`` 仍保留模型这次的原文写法（不
# 篡改，供分镜台 known_assets.props 展示 + 下游别名登记）。真实事故
# （proj_ca86b15ab7d7 系列，见 2026-09-30 派单）：素材库第1集建的卡叫「行李
# 箱」，第2集模型把同一件东西报成新标签「旧行李箱」——旧逻辑按 label 精确
# 去重、从不查 bible.props，结果第2集当成全新道具，外观/参考图都查不到。
# 两条判据（本函数的字面锚定 + card_match 的卡片绑定）是"任一满足即可"：一
# 条提及若两条都不满足，整条丢弃（不计入清单，不阻断发布——跟 scene 侧
# "没证据就当未解析"同一处置，不是"空口提名也发布"）。
#
# 已知局限（P2，本次不解决）：合并键是 canonical_name（绑定时）或 label（未绑定时）——
# 同一个原文写法在不同 mention 里若因各自声明的段落证据不同而时而绑上卡、时而绑不上，
# 会拆成两条独立清单条目而不是合并成一条。card_match 判据本身是纯函数、按同一份 cards
# 与各自的段落证据独立运算，不做跨 mention 的二次合并——同一物件反复出现时措辞通常一致，
# 这类拆分预计罕见；需要更强一致性时留给后续有专门预算时再评估。
#
# plot_significant/plot_significant_quote（2026-09-28 新增，见
# .chunk_extraction 提示词与 app.props.judge.is_key_prop_mention 的同名
# 判据）：原样透传模型这次申报的两个字段，不在这里做任何核验——逐字核验
# 是 is_key_prop_mention 消费时的职责（它同时还需要 source_text，本函数
# 不持有），这里只负责把模型的申报值带到 props_payload 里，缺省时按假/空
# 兜底（旧调用方构造的 mention dict 没有这两个键时不报错，向后兼容
# tests/test_props_library.py 里手写的 mention 夹具）。
def _prep_pack_prop_card_anchor(
    card: Prop, valid_indexes: list[int], segments: list[SourceSegment],
) -> tuple[int, str, bool] | None:
    """card_match 分支的 provenance 锚点：``card`` 的 name/alias 里第一个在
    ``valid_indexes`` 某段原文里逐字出现的那个，连同段号、是否经尾部退让
    一起返回。全串定位不到时退让到尾部子串再试一遍（2026-10-01，见
    .trailing_anchor 模块），范围仍限定在 valid_indexes；都落空才 None。"""
    identifiers = [str(card.name or "").strip(), *(str(a or "").strip() for a in card.aliases)]
    for index in valid_indexes:
        text = segments[index - 1].text
        for identifier in identifiers:
            if identifier and identifier in text:
                return index, identifier, False
    evidence_text = "\n".join(segments[i - 1].text for i in valid_indexes)
    phrase = trailing_anchor_phrase(identifiers, evidence_text)
    if not phrase:
        return None
    for index in valid_indexes:
        if phrase in segments[index - 1].text:
            return index, phrase, True
    return None


def _prep_pack_prop_mention_binding(
    label: str, source_wording: str, nominated_card: str, valid_indexes: list[int],
    segments: list[SourceSegment], cards: Sequence[Prop],
) -> tuple[list[int], Prop | None, str, list[int], str, bool] | None:
    """核验一条道具提及：返回 (segment_indexes, 绑定的卡或 None, provenance.method,
    anchor_segments, anchor_phrase, 是否经尾部退让锚定)；两条判据都不满足时返回
    None（整条丢弃，调用方须记入可见的 unanchored 出参，不静默 continue）。

    字面候选依次试 label、source_wording（schemas._ModelPropMention.
    source_wording 上方注释）；都不命中且未绑定既有卡时退让到尾部子串，见
    ``.trailing_anchor.prop_literal_or_trailing_anchor``（2026-10-01，真实
    案例"旧笔记本"→"笔记本"）；已绑定卡的退让改在 _prep_pack_prop_card_
    anchor 内部做（不跟 card_match 自身的单一胜者判据赛跑）。``nominated_
    card`` 原样透传给 ``match_existing_prop_card``（模型提名、代码核验）。
    """
    # 延迟导入：避免给 app.production.prep_pack（映射台核心链路）加一条模块级
    # 常驻依赖到 app.props 的模型/出图调用链——import app.props.card_match 前
    # Python 必须先跑 app/props/__init__.py，它无条件 import .service，而
    # service 又模块级 import .image → app.hiagent（HiAgent 网关客户端）。与
    # 相邻 _discover_new_characters/_discover_new_scenes/_discover_new_props
    # 的既有写法保持一致（同一文件里三个函数都是函数内 import）。
    from app.props.card_match import match_existing_prop_card

    evidence_text = "\n".join(segments[i - 1].text for i in valid_indexes)
    card = match_existing_prop_card(
        label, evidence_text, cards, source_wording=source_wording, nominated_card=nominated_card,
    )
    literal_indexes, literal_phrase, trailing = prop_literal_or_trailing_anchor(
        label, source_wording, valid_indexes, segments, evidence_text, card is not None,
    )
    if not literal_indexes and card is None:
        return None
    segment_indexes = literal_indexes or valid_indexes
    if literal_indexes:
        return segment_indexes, card, "direct", [segment_indexes[0]], literal_phrase, trailing
    anchor = _prep_pack_prop_card_anchor(card, valid_indexes, segments)
    anchor_segments = [anchor[0]] if anchor else [segment_indexes[0]]
    anchor_phrase = anchor[1] if anchor else ""
    card_trailing = anchor[2] if anchor else False
    return segment_indexes, card, "card_match", anchor_segments, anchor_phrase, card_trailing


def _prep_pack_record_unanchored_prop(
    unanchored: list[dict[str, Any]] | None,
    label: str, source_wording: str, valid_indexes: list[int],
) -> None:
    """道具提及两条判据（label/source_wording 逐字命中声明段落、或对照既有
    卡片）都不满足时的可见记录（2026-09-30，见派单真实案例「木星星」「缠着
    细银丝的木簪」「妈妈的字条」）：此前 ``_prep_pack_build_prop_manifest``
    直接 ``continue``，没有任何日志或可见记录，这三件真实道具从
    asset_manifest.props 静默消失。现在打一条固定前缀日志（同 card_match.
    match_existing_prop_card 的 PROP_CARD_MATCH_AMBIGUOUS 同一惯例，供日志
    检索）+ 写入调用方传入的 ``unanchored`` 列表（生成台 payload 的
    asset_manifest.unanchored_prop_mentions，供人工核查；不进 props 清单，
    不阻断发布——跟场景侧 degrade_unresolved_scene 同一处置）。``unanchored``
    为 None（旧调用点未接线，例如既有测试夹具）时只记日志，不因此报错。"""
    reason = (
        "source_wording 非原文字面" if source_wording
        else "缺少 source_wording 且 label 非原文字面，也未匹配到既有道具卡"
    )
    log.warning(
        "[PREP_PACK_PROP_UNANCHORED][未拦截] 道具「%s」在声明段落 %s 未能锚定"
        "（%s），不计入 props 清单，请人工核查",
        label, valid_indexes, reason,
    )
    if unanchored is not None:
        unanchored.append({
            "label": label, "source_wording": source_wording,
            "segment_indexes": valid_indexes, "reason": reason,
        })


def _prep_pack_build_prop_manifest(
    prop_mentions: list[dict[str, Any]], segments: list[SourceSegment],
    *, cards: Sequence[Prop] = (), unanchored: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """``unanchored``（2026-09-30 出参，可选，默认 None 不记录——同
    ``_resolve_assets`` 的 ``appellation_resolutions`` 同一模式，保持既有
    调用点/测试签名不变）：两条判据都不满足、被丢弃的提及原地写入这个列表，
    见 _prep_pack_record_unanchored_prop。"""
    props: dict[str, dict[str, Any]] = {}
    for mention in prop_mentions:
        label = str(mention.get("label") or "").strip()
        source_wording = str(mention.get("source_wording") or "").strip()
        known_prop_name = str(mention.get("known_prop_name") or "").strip()
        valid_indexes = sorted(
            index for index in {int(i) for i in mention.get("segment_indexes") or []}
            if 1 <= index <= len(segments)
        )
        if not label or not valid_indexes:
            continue
        binding = _prep_pack_prop_mention_binding(
            label, source_wording, known_prop_name, valid_indexes, segments, cards,
        )
        if binding is None:
            _prep_pack_record_unanchored_prop(unanchored, label, source_wording, valid_indexes)
            continue
        segment_indexes, card, method, anchor_segments, anchor_phrase, trailing = binding
        canonical_name = card.name if card else None
        key = canonical_name or label
        entry = props.setdefault(key, {
            "label": label,
            "canonical_name": canonical_name,
            "description": str(mention.get("description") or "").strip(),
            "segment_indexes": [],
            "provenance": _prep_pack_provenance(
                method, anchor_segments, anchor_phrase, trailing_anchor=trailing,
            ),
            "plot_significant": bool(mention.get("plot_significant")),
            "plot_significant_quote": str(mention.get("plot_significant_quote") or "").strip(),
            "source_wording": source_wording,
            "known_prop_name": known_prop_name,
        })
        entry["segment_indexes"] = sorted(
            set(entry["segment_indexes"]) | set(segment_indexes)
        )
        # plot_significant/plot_significant_quote 必须跨同 label 的多条提及做
        # "任一为真即采纳"的合并，不能只取 setdefault 首次插入时那一条
        # （2026-09-28 code review 实测发现：同一道具先在早期 chunk 里被平淡
        # 提及、后在更晚的 chunk 里才因交接/特写/伏笔揭示被模型正确标记
        # plot_significant=True，是 Part C 明确要接住的形状——黄铜旧星盘、
        # 童年合影都是这种贴身出现多次、其中一次才是剧情重要时刻的道具。按
        # 处理顺序固定取第一条会让靠后到达的真实证据被静默丢弃，且没有任何
        # 信号提示丢弃发生过）。一旦某条提及命中就不再被后续 False 的提及
        # 覆盖回去——先到的真证据比后到的"这条不重要"更可信。
        if mention.get("plot_significant") and not entry["plot_significant"]:
            entry["plot_significant"] = True
            entry["plot_significant_quote"] = str(
                mention.get("plot_significant_quote") or ""
            ).strip()
    return list(props.values())


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


