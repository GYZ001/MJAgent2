"""Asset-manifest provenance: locating verbatim source-text anchors for a
resolved character/scene/prop and the manifest-wide provenance verification
gate.

Split out of app/production/prep_pack.py. This is the asset-provenance gate
(``anchor_phrase`` / _prep_pack_verify_manifest_provenance and friends) the
task brief calls out by name -- moved verbatim, gate logic untouched.
"""
from __future__ import annotations

import re
from app.source_excerpt import SourceSegment
from typing import Any

from .asset_lookup import _prep_pack_scene_reference_origin_episode


def _prep_pack_first_evidence_segment(segments: list[SourceSegment], text: str) -> int | None:
    """观测用：`text` 第一次逐字出现在哪个 1-based segment_index，供别名
    锚定来源段号记录（找不到就是 None，不阻断任何流程，纯观测）。"""
    if not text:
        return None
    for index, segment in enumerate(segments, start=1):
        if text in segment.text:
            return index
    return None


# manifest 绑定来源证明（provenance，1.6.0，第25轮收口指令：审计剩余83条
# 定性为"合成标签合法但不可审计"——1.5.x 各轮陆续放宽了字面锚定要求
# （task②：经消歧/发现解析的绑定不再要求 mention 本身逐字出现），但放宽后
# 判断"这次绑定为什么合法"的依据只留在 Evaluation.evidence 里（true_name_
# hints/scene_alias_anchors/absorbed_speakers_count……），不是 payload 的
# 一等公民，审计只能翻 Evaluation 观测，无法直接从 asset_manifest 本身复核
# 每一条绑定的证据链。这个函数是所有 provenance 计算共用的确定性锚点查找：
# 按优先级尝试一组候选逐字短语，返回第一个真的出现在本集原文里的
# (anchor_segments, anchor_phrase)。全部候选都不出现时返回 ([], "")——不是
# 所有合法绑定都必然有本集内的逐字锚点（比如 suspected_true_name 经前瞻
# 窗口核验通过，证据在下一集原文里，不在本集）；空 anchor_phrase 在自校验
# 里视为"这条绑定没有可本地核验的锚点"，直接跳过验证，不阻断——已经比
# 1.5.x 之前"完全没有这个字段"更诚实，不需要为了填满字段而编造一个假锚点。
_PREP_PACK_WHITESPACE_RE = re.compile(r"\s+")
# 引文两端的成对引号属于引用格式，不属于被引用的那句话。中英文各种成对
# 引号都列进来：模型抄哪一种取决于原文用哪一种。
_PREP_PACK_QUOTATION_MARKS = "“”‘’「」『』\"'"
# 句末终止标点。模型引用长句时经常抄到语义完整处停笔，并把原文那个逗号换成
# 句号收尾——见 _prep_pack_locate_phrase 里 ERR-20260828-f819b0 的完整说明。
_PREP_PACK_TERMINAL_MARKS = "。！？…．.!?"


def _prep_pack_locate_verbatim(
    segments: list[SourceSegment], phrase: str,
) -> list[int]:
    """`phrase` 逐字落在哪几个 segment 上（1-based，升序）；定位不到返回 []。

    先按单段严格匹配——绝大多数锚点走这条，与最初实现逐字节等价。都不命中
    时才退一步：把各段按原文顺序接起来、只抹掉空白字符，再做一次逐字定位。

    模型引用原文时经常横跨自然段，而它抄下来的引文里没有段间那个换行。真实
    故障 ERR-20260828-16ce45（《黄英》EP1「郊野归途路径」）：原文「……踏上
    归途。」与「走到半路，他遇见一位少年。」分属相邻两段，模型申报的
    quote 把两句连着写，于是它既不在第一段里、也不在第二段里，三路候选
    全部落空，整集映射被「缺少 anchor_phrase」拦停——而这条引文的每一个字
    都真的在原文里，差的只是一个换行。

    段落分隔是排版，不是内容。抹掉空白之后仍然要求每个字、每个标点逐字
    连续命中，所以编造的引文、改写过的引文、张冠李戴的引文照样定位不到：
    这是把「逐字」定义在文字上，不是放宽逐字。
    """
    if not phrase:
        return []
    for index, segment in enumerate(segments, start=1):
        if phrase in segment.text:
            return [index]
    needle = _PREP_PACK_WHITESPACE_RE.sub("", phrase)
    if not needle:
        return []
    haystack: list[str] = []
    owner: list[int] = []
    for index, segment in enumerate(segments, start=1):
        for char in segment.text:
            if char.isspace():
                continue
            haystack.append(char)
            owner.append(index)
    start = "".join(haystack).find(needle)
    if start < 0:
        return []
    return sorted(set(owner[start:start + len(needle)]))


def _prep_pack_citation_forms(phrase: str) -> list[str]:
    """一条引文的各种「引用格式」形态，按从原样到最省的顺序返回。

    只剥两类纯格式字符，且每一种形态仍要求剩下的每个字逐字连续命中原文：

    * 两端成对引号——引文两端的引号属于引用格式，不属于被引用的那句话；
    * 句末终止标点——模型抄一段长句时会在语义完整处停笔，并把原文那里的
      逗号替换成句号（或问号/叹号/省略号）收尾。

    真实故障 ERR-20260828-f819b0（《我欲封天》EP1「靠山宗山腰青石坪」）：
    原文是「……云雾缭绕绝非凡尘，能看到一些精美的阁楼环绕山峦八方，满眼
    陌生。」，模型申报的 quote 是「……云雾缭绕绝非凡尘。」——从第一个字到
    「绝非凡尘」逐字全对，差的只是把原文的「，」写成了「。」并就此收尾。
    三路候选（canonical_scene_name / name / quote）于是全部落空，整集映射
    被「缺少 anchor_phrase」拦停。这与已修复的 ERR-20260828-91bc95（模型
    自补收尾引号）是同一族：**引文的收尾符号是引用这个动作留下的痕迹，
    不是被引用内容本身**。

    剥的是符号不是文字：编造的、改写的、张冠李戴的引文，剥完照样定位不到
    （见 tests/test_prep_pack_citation_forms.py 的反例）。所以这不是放宽
    逐字，是把「逐字」定义在文字上。
    """
    forms: list[str] = []
    for base in (phrase, phrase.strip(_PREP_PACK_QUOTATION_MARKS)):
        base = base.strip()
        if not base:
            continue
        forms.append(base)
        trimmed = base.rstrip(_PREP_PACK_TERMINAL_MARKS).strip()
        if trimmed and trimmed != base:
            forms.append(trimmed)
    return forms


def _prep_pack_locate_stitched_quote(segments: list[SourceSegment], phrase: str) -> tuple[list[int], str]:
    """整条不命中时的最后一步：拆句后每句都逐字命中且与原文同序，才以最长句（≥6 字）为锚点；任一句定位不到或次序颠倒即整条拒绝。ERR-20260902-507cb0《三国演义》第一回：原文相隔数十字的两句被接成一条 quote，两轮重试一字不差——拼接是引用格式不是改写；反例见 test_prep_pack_asset_discovery 的 invented_quotes。"""
    parts = [part.strip() for part in re.split(r"(?<=[。！？…．.!?])", phrase) if part.strip()]
    located: list[tuple[list[int], str]] = []
    for sentence in parts if len(parts) >= 2 else []:
        hit = next(((s, f) for f in _prep_pack_citation_forms(sentence) if (s := _prep_pack_locate_verbatim(segments, f))), None)
        if hit is None:
            return [], ""
        located.append(hit)
    order = [(segs[0], segments[segs[0] - 1].text.find(form)) for segs, form in located]
    if not located or order != sorted(order):
        return [], ""
    best = max(located, key=lambda item: len(item[1]))
    return best if len(best[1].rstrip(_PREP_PACK_TERMINAL_MARKS)) >= 6 else ([], "")


def _prep_pack_locate_phrase(
    segments: list[SourceSegment], phrase: str,
) -> tuple[list[int], str]:
    """定位 `phrase`，返回 (anchor_segments, 真正落在原文里的那个短语)。

    返回的短语可能比传入的短——引文两端的引号会被剥掉重试。调用方必须把
    返回值当作 anchor_phrase 落库，不能沿用传入的原串：自校验
    （_prep_pack_verify_manifest_provenance）和外部审计
    （scripts/episode_source_audit.py）都会拿 anchor_phrase 回原文逐字复核，
    存一个原文里不存在的串等于把这道闸留给下一次运行去撞。

    真实故障 ERR-20260828-91bc95（《王六郎》EP1，「许姓人家居所」与
    「邬镇土地祠内」两个场景同时中招）：模型引用一段长对白时抄到句号就停笔，
    然后自己补了一个收尾引号——写「妻子笑他说：“这一去有几百里。”」，而原文
    是「“这一去有几百里。即使真有那个地方，只怕泥塑的神像也不能同你说话。”」，
    那个 ” 在原文里出现在四十多字之后。差的就是这一个字符，整集映射被
    「缺少 anchor_phrase」拦停，重试必然复现（两次调用都补了引号）。

    把引文补完整是引用这个动作的一部分，不是对内容的改写。剥掉两端引号之后
    仍然要求剩下每一个字逐字连续命中，所以编造和改写照样定位不到。
    """
    phrase = str(phrase or "").strip()
    if not phrase:
        return [], ""
    seen: set[str] = set()
    for candidate in _prep_pack_citation_forms(phrase):
        if candidate in seen:
            continue
        seen.add(candidate)
        located = _prep_pack_locate_verbatim(segments, candidate)
        if located:
            return located, candidate
    return _prep_pack_locate_stitched_quote(segments, phrase)


def _prep_pack_local_text_anchor(
    segments: list[SourceSegment], candidates: list[str],
) -> tuple[list[int], str]:
    for candidate in candidates:
        candidate = str(candidate or "").strip()
        if not candidate:
            continue
        anchor_segments, anchor_phrase = _prep_pack_locate_phrase(segments, candidate)
        if anchor_segments:
            return anchor_segments, anchor_phrase
    return [], ""


# 跨集别名场景绑定的锚点强化（第30轮②，真实 scripts/episode_source_
# audit.py 复核实测：19 条 A2_scene_no_text_evidence，全部 provenance.
# method="alias"、aliases=0 个、display_name 不逐字出现在本集原文——该
# 审计脚本对 alias/direct 两个 method 走的是 TEXT_VERIFIED 标准（只查
# display_name/aliases 字符串是否逐字出现，不看 anchor_phrase，见该脚本
# TEXT_VERIFIED_METHODS 常量上方注释），跟 resolution/discovery/
# absorbed_speaker 的 ANCHOR_VERIFIED 标准（查 anchor_segments/
# anchor_phrase）是两套不同规则。旧实现只试 [name]（这次绑定用到的原始
# 称谓本身）——这个候选之所以必然命中，只是因为它就是"别名注册表"这个
# 称谓本身，命中的其实只是"这个称谓确实是这么写的"这件同义反复的事实
# （对 TEXT_VERIFIED 毫无帮助：display_name 是规范名，不是这个别名字符
# 串，name 命中不了 TEXT_VERIFIED 检查的是 display_name/aliases，scene
# 目前没有 aliases 字段），没有独立证明本集里还有别的什么依据把它跟这个
# 场景绑在一起。实测（真实 EP1-8 19 条）canonical_scene_name 无一逐字
# 命中本集原文，但该场景所涉事件的 source_evidence 地点描述短语 19/19
# 命中——这才是真正独立、有信息量的证据。改法：候选序列改成
# [canonical_scene_name, *scene_event_evidence_quotes]（沿用第28轮①给
# discovery/resolution 两支的同一批候选来源，故意不包含 name 本身这个
# 同义反复候选）；命中 → 这是比"alias"更强的证据形状，跟 resolution 走
# 同一套锚点核验标准，method 直接升级为 "resolution"（ANCHOR_VERIFIED，
# 自校验/外部审计都认这份 anchor_phrase）；三候选（不含 name）全部落空
# → 绝不伪造锚点，也不再谎称"alias"（同义反复的空壳），改标
# method="alias_inherited"，用现成的 scene_references.ep_start 记这次
# 绑定最初在注册表里生效的集号（source_episode_no，跟审计脚本
# _verify_alias_inherited_scene 期望的字段名/类型完全对齐——那段递归核验
# 逻辑早于本次改动已经写好，是这次改动要对齐的既有契约，不是本次新造的
# 字段名），供审计走对应的递归核验分支（不在
# _PREP_PACK_SCENE_METHODS_REQUIRING_ANCHOR 里，空锚合法豁免，跟
# resolution_forward 同待遇）。
#
# 2.0.2 更新（见 PREP_PACK_VERSION 上方 2.0.2 大注释）：2.0.0 砍 event_chain 后，调用方
# 一度把 scene_event_evidence_quotes 传空列表（event_chain 没了，暂时没有替代来源）——这不是
# 这份函数签名/判据本身的改动，函数体一行未动，仍然是"给一份候选引文列表，命中就升级
# resolution，不命中就诚实降级 alias_inherited"这同一套判据；变化只在调用方现在恢复传入这条
# 场景提及自己申报的 quote（_ModelSceneMention.quote，isomorphic 于旧
# event_chain[].source_evidence[].quote，只是粒度从"事件"下沉到"提及"，见调用点上方注释）。
def _prep_pack_scene_alias_provenance(
    conn, segments: list[SourceSegment], scene_reference_id: str,
    canonical_scene_name: str, scene_event_evidence_quotes: list[str],
) -> tuple[str, list[int], str, int | None]:
    """Returns ``(method, anchor_segments, anchor_phrase, source_episode_no)``
    for a scene mention resolved via the cross-episode alias registry."""
    anchor_segments, anchor_phrase = _prep_pack_local_text_anchor(
        segments, [canonical_scene_name, *scene_event_evidence_quotes],
    )
    if anchor_segments:
        return "resolution", anchor_segments, anchor_phrase, None
    source_episode_no = _prep_pack_scene_reference_origin_episode(conn, scene_reference_id)
    return "alias_inherited", [], "", source_episode_no


def _prep_pack_provenance(
    method: str, anchor_segments: list[int], anchor_phrase: str,
    *, forward_chapter_label: str = "", source_episode_no: int | None = None,
    dual_anchor: bool | None = None,
    candidate_verdict_attempted: bool | None = None,
) -> dict[str, Any]:
    """统一构造 provenance 结构，避免多处调用各自拼一份字面量字典漂移。
    forward_chapter_label（1.6.0 第28轮）只在 method="resolution_forward"
    时非空——见 _prep_pack_verify_manifest_provenance 上方关于
    resolution_forward 空锚豁免的完整说明。source_episode_no（第30轮②）
    只在 method="alias_inherited" 时非 None——字段名/类型（int，不是格式化
    字符串）对齐 scripts/episode_source_audit.py 的
    _verify_alias_inherited_scene 既有契约（该脚本先于本次改动就已经按
    这个字段名写好了递归核验：来源集号须严格早于当前集、来源集需有已发布
    pack 且同 scene_reference_id 的绑定同名、那条来源绑定自身还要递归核验
    通过），不是本次新造的字段名，也故意不跟 forward_chapter_label 复用
    同一个字段——两者是不同的编号域（前瞻窗口指向"章"，跨集别名继承指向
    "集"），混装单位会重蹈第28轮排查过的"同一数据两个真源"覆辙。
    dual_anchor（1.10.0，缺陷 A 修复）只在 suspected_true_name 核验通过
    时非 None——True 表示钉证命中的是同时含 alias 与 true_name 的双锚定
    条目，False 表示全卷宗结构上不存在双锚定证据、退化为仅含 alias 的
    集内指代条目（见 _prep_pack_verify_true_name_hypothesis docstring）。
    显式记录这个布尔值是可观测降级的落地点——本项目明令禁止静默降级，不能
    只在 provider_calls 里才看得出这次绑定走的是退化路径。
    candidate_verdict_attempted（1.10.0，缺陷 A 顺带修复的可观测性缺口）
    只在 method="discovery" 时非 None——区分这批 functional_extras 是
    「从未获得候选判别机会」（候选集为空/卷宗为空，True 之前从未发起过
    候选判别模型调用）还是「候选判别跑过但没选中」（发起过调用，模型选了
    "都不是/无法确定"或钉证未通过），此前两者坍缩成同一个 method 值，只能
    翻 provider_calls 反推。三者都是纯附加字段，其它 method/情形不带这些
    key，不影响既有消费者（payload 冻结纪律照旧）。
    label_literal（1.11.0/1.11.1，任务①）已在 2.0.0 撤下：不是因为它变得
    结构性恒真（合成描述性标签仍然合法、仍然常见非逐字，见
    _prep_pack_gate_segment_indexes 上方说明——那道结构闸刻意不做逐字
    核验，避免在候选判别机会到来之前就堵死它），是纯粹的范围收窄——映射台
    2.0.0 只对"绑定到谁"负责，"这个称谓好不好看/是不是逐字"这类纯观测性
    标记不再是这个模块的职责，见 PREP_PACK_VERSION 上方 2.0.0 大注释。"""
    provenance = {
        "method": method,
        "anchor_segments": list(anchor_segments),
        "anchor_phrase": anchor_phrase,
    }
    if forward_chapter_label:
        provenance["forward_chapter_label"] = forward_chapter_label
    if source_episode_no is not None:
        provenance["source_episode_no"] = source_episode_no
    if dual_anchor is not None:
        provenance["dual_anchor"] = dual_anchor
    if candidate_verdict_attempted is not None:
        provenance["candidate_verdict_attempted"] = candidate_verdict_attempted
    return provenance


# 场景侧 resolution/discovery 两个 method 不允许空锚（第28轮 ERR-20260824，
# v3 审计 A2_scene_no_text_evidence 25 条）：消歧/发现判定"这是哪个场景"
# 凭的必然是本集文本依据，不存在"合法但无锚"的场景绑定——跟角色侧
# resolution_forward（证据在前瞻窗口，本集内没有锚点是正确的）不是同一件
# 事。direct method 已经被 _prep_pack_resolve_scene_reference_with_alias
# 上游的证据闸挡住（见该函数与其调用点上方注释），结构上不可能出现空锚，
# 不需要在这里重复要求——第30轮②：原来的 alias 分支不再产出裸的
# method="alias" 挂空锚（见 _prep_pack_scene_alias_provenance 上方完整
# 说明）：真找到独立证据就升级成 resolution（走这条锚点必填规则），真没有
# 就诚实改标 alias_inherited，跟 resolution_forward 同样豁免、同样在这里
# 不需要登记。
_PREP_PACK_SCENE_METHODS_REQUIRING_ANCHOR = frozenset({"resolution", "discovery"})


def _prep_pack_verify_manifest_provenance(
    segments: list[SourceSegment], asset_manifest: dict[str, Any],
    source_text: str = "",
) -> list[str]:
    """发布前自校验（1.6.0）：每一条非空 anchor_phrase 必须真的逐字出现在
    它自己 anchor_segments 指向的原文段里（至少一段命中即可，不要求每段都
    命中——一条绑定可能引用多个证据段，只要其中一个真的载有这句 anchor_
    phrase 就算锚定成立）。anchor_phrase 为空默认视为"这条绑定没有本集
    本地锚点"，跳过逐字校验（见 _prep_pack_local_text_anchor 的完整说明）
    ——但这条豁免不是无条件的：场景侧的 resolution/discovery 两个 method
    真实回归证明"空锚"从来不是合法状态，而是 1.6.0 最初实现里
    _prep_pack_local_text_anchor 候选序列覆盖不全（只试了触发发现/消歧的
    原始 label，没试模型申报的规范名、也没试该场景所涉事件自己的证据
    原文）导致的假阴性——真锚点本来就在，只是没找全；这两个 method 现在
    强制要求非空锚，空锚直接判定自校验失败（见
    _PREP_PACK_SCENE_METHODS_REQUIRING_ANCHOR）。resolution_forward（角色
    与场景共用同一语义）的空 anchor_segments（本地段号）是合法的：
    suspected_true_name 核验通过、证据在前瞻窗口而非本集，本集内找不到
    本地锚点是正确结论，不是缺陷——但 forward_chapter_label（指向哪一章）
    和 anchor_phrase（那一章里被钉住的支撑句）两个字段第30轮起强制同时
    非空："半张证书等于没证书"：真实 EP2/6/8 回归过 anchor_phrase 被误写
    成空字符串（钉住的支撑句明明存在，却没记下来）、EP2/6/8 角色侧还
    额外查出 forward_chapter_label 本身也会在特定路径下丢失（见
    _prep_pack_verify_true_name_hypothesis 调用点上方 via_suspected_
    true_name 标志位的完整说明）——两种半张证书现在都在这里被拦截，不再
    悄悄发布。

    段号越界（不在 1..len(segments) 内）本身就是判定失败，不静默忽略。
    段号取值域（第28轮排查记录，E 类重复真源变体）：本函数与
    _prep_pack_local_text_anchor/_pass 全程共用同一个 segments 闭包变量
    （index_source_segments(source_text) 的全局 1-based 编号），跟
    coverage_ledger/source_span 同域；_chunk_segments 分块时用
    list(enumerate(segments, start=1)) 保留原始全局下标分组、不做分块内
    重新编号，_render_chunk 展示给模型的编号、event 的 source_span/
    source_evidence[].segment_index 因此也都是全局域——沿链路排查未发现
    第二套局部编号并存（若未来某处引入分块内局部重新编号，必须只保留
    全局域一份，禁止两个编号域并存后再各自"验一遍"，那正是同一数据的
    两个真源互相打架、两边各自"验过"却结论相反的形状）。见
    tests/test_prep_pack_asset_discovery.py 里显式构造跨 chunk 场景的
    自校验红灯，作为这条不变量的回归防线。

    ``source_text``（2.0.0 起不再驱动任何检查，仅保留形参兼容既有调用点/
    测试签名——1.11.0/1.11.1 引入的 label_literal 自校验已随该字段一起撤下
    （纯范围收窄，不是失败类别被结构性堵死，见 PREP_PACK_VERSION 上方
    2.0.0 大注释与 _prep_pack_provenance 的 docstring）。"""
    errors: list[str] = []
    total_segments = len(segments)

    def _check(
        kind: str, label: str, provenance: Any, *, require_anchor: bool = False,
    ) -> None:
        if not isinstance(provenance, dict):
            return
        method = str(provenance.get("method") or "")
        phrase = str(provenance.get("anchor_phrase") or "").strip()
        # resolution_forward（第30轮，用户点名"半张证书等于没证书"）：证据
        # 在前瞻/别处章节而非本集，anchor_segments 本地段号合法留空——但
        # forward_chapter_label（指向哪一章）和 anchor_phrase（那一章里的
        # 哪句话）两个字段必须同时非空，才是一条完整、可被
        # scripts/episode_source_audit.py 的 _verify_provenance_forward_
        # anchor 独立复核的证明；任一为空都是半张证书，一律拦截，不再往下
        # 走本地 anchor_segments 逐字校验（那套校验假设短语就在本集里，对
        # resolution_forward 从语义上就不适用）。
        if method == "resolution_forward":
            forward_chapter_label = str(
                provenance.get("forward_chapter_label") or ""
            ).strip()
            if not forward_chapter_label or not phrase:
                errors.append(
                    f"{kind}「{label}」的 provenance.method=resolution_forward "
                    f"缺少 forward_chapter_label（{forward_chapter_label!r}）或 "
                    f"anchor_phrase（{phrase!r}）——前瞻绑定必须同时携带"
                    "章节标注与被钉住的支撑句，半张证书等于没证书，来源"
                    "证明自校验失败，门禁具名拦截"
                )
            return
        if not phrase:
            if require_anchor:
                errors.append(
                    f"{kind}「{label}」的 provenance.method="
                    f"{provenance.get('method')!r} 缺少 anchor_phrase——"
                    "resolution/discovery 绑定必须有本集文本依据，来源"
                    "证明自校验失败，门禁具名拦截"
                )
            return
        raw_segments = provenance.get("anchor_segments") or []
        segment_indexes = [
            int(value) for value in raw_segments
            if isinstance(value, (int, float)) or (
                isinstance(value, str) and value.strip().lstrip("-").isdigit()
            )
        ]
        in_range = [
            index for index in segment_indexes if 1 <= index <= total_segments
        ]
        # 判据与 scripts/episode_source_audit.py::_verify_provenance_anchor
        # 逐字同构：把声明的那几段按序拼起来，短语必须落在拼接结果里。审计
        # 侧一直是这么核验的（anchor_segments 本就是复数），只有这里还停在
        # "任一单段内命中"——横跨自然段的引文因此生成侧算命中、这里算不命中。
        # 同一份数据两个真源互相打架时，这道闸拦下的不是幻觉，是我们自己。
        joined = "".join(segments[index - 1].text for index in in_range)
        if not in_range or phrase not in joined:
            errors.append(
                f"{kind}「{label}」的 provenance.anchor_phrase「{phrase}」未在"
                f"anchor_segments={segment_indexes} 所指原文中逐字命中，来源"
                "证明自校验失败，门禁具名拦截"
            )

    for character in asset_manifest.get("characters") or []:
        _check("角色", str(character.get("display_name") or ""), character.get("provenance"))
    for scene in asset_manifest.get("scenes") or []:
        provenance = scene.get("provenance")
        require_anchor = (
            isinstance(provenance, dict)
            and str(provenance.get("method") or "")
            in _PREP_PACK_SCENE_METHODS_REQUIRING_ANCHOR
        )
        _check(
            "场景", str(scene.get("display_name") or ""), provenance,
            require_anchor=require_anchor,
        )
    for extra in asset_manifest.get("functional_extras") or []:
        _check("群演", str(extra.get("label") or ""), extra.get("provenance"))
    # 2.0.0 新增：props 也走同一条 anchor_phrase 自校验（见
    # _prep_pack_build_prop_manifest 的 provenance 构造，method 恒
    # "direct"，anchor_segments/anchor_phrase 来自它自己已经逐段字面核验
    # 过的 segment_indexes/label——道具没有解析路径豁免，见该函数说明）。
    for prop in asset_manifest.get("props") or []:
        _check("道具", str(prop.get("label") or ""), prop.get("provenance"))
    return errors


# 称谓/场景名证据闸（1.4.2，real round-16 EP5 regression fix）. Real EP5 output
# resolved a completely unrelated pair of mountain-top old men -- the raw
# text only ever calls them "两个老者"/穿灰袍的高大老者", never a proper
# name -- to a pre-existing character ("丹鬼") and scene ("大青山山顶") from
# elsewhere in the story, purely because the event-chain extraction model
# happened to write those exact already-registered names (both are 0
# occurrences in chapter 5's own text; verified directly against the real
# chapters row). Root cause: neither _resolve_portrait_id nor
# _resolve_scene_reference_id require any evidence beyond "a DB row with
# this exact name exists somewhere, for any episode" -- a bare name-string
# coincidence was silently trusted as a real identification. Traced two
# independent binds:
#   - character "丹鬼": the chunk-extraction model wrote "丹鬼" directly as
#     characters[].display_name (NOT "灰袍老者"/"山顶老者" -- those only ever
#     appeared as key_lines[].speaker, a field this module never resolves
#     through) -- a bare direct hit, not a legitimate forward-looking
#     identity resolution (which is why aliases ended up empty: no rename
#     ever happened, so the existing "aliases.append(name) when name !=
#     resolved_name" logic never had anything to record).
#   - scene "大青山山顶": same shape -- scenes[].display_name was written as
#     "大青山山顶" directly, an existing scene_reference from unrelated
#     earlier context, despite the text explicitly saying "靠山宗四周的山峰"
#     / "外宗旁的山峰".
def _prep_pack_mention_has_text_evidence(name: str, source_text: str) -> bool:
    """Does ``name`` -- the raw mention/称谓 text an event actually carries,
    exactly as the event-chain extraction model wrote it -- appear verbatim
    anywhere in this episode's own ``source_text``? A plain substring check
    is deliberately sufficient here (unlike align_source_excerpt's fuzzy
    quote-matching, which exists for full-sentence quotes): character/scene
    names are short proper nouns, not sentences, so an exact substring
    either is or isn't real textual grounding for "this term was actually
    used to refer to something in this chapter."
    """
    return bool(name) and name in (source_text or "")


# 跨集别名一致性（1.5.2/task②，真实第18轮审计 B 类缺陷）：proj_3ac0b627fa46
# 项目内"小胖子"同时被登记为李富贵和王有材的别名——溯源确认 EP3 那次绑定
# 完全没有本集文本依据（chapters 表 EP3 原文直查："王有材"逐字出现 0 次，
# "小胖子"高频出现且从上下文看自始至终是同一个人）；EP2/EP6 两集独立正确
# 绑到了李富贵。EP3 反复重新生成横跨第15-18轮，累计 80+ 次 identity.current
# 与 144+ 次涉及"小胖子"的 identity.future 调用，未能定位到单一一次把
# "小胖子"分组直接判给"王有材"的调用记录——大概率是 app.portraits.
# append_candidate 的"全批唯一字面锚点自动改绑"机制或跨 chunk 的
# functional_identity_key 合并在某次重试中产生，具体哪一次已经无法在保留的
# provider_calls 历史里精确复现（EP3 之后的多轮重新生成覆盖了当时的中间
# 状态）。核心事实清楚：这次改绑在其发生的那一集里没有任何逐字证据支撑，
# 正是"真名核验佐证不足"的形状。
#
# 1.7.0（层一，docs/CHARACTER_IDENTITY_ENTITY_DESIGN.md §4.1/§6 第3项）：
# 主读源切换为 Bible.characters[].aliases（app.schemas.CharacterAlias，全书
# 分析阶段模型申报+代码核验后落库，见 app.stages.generate_bible）。旧的
# "扫描项目内其它已发布分集 asset_manifest" 路径不是本次要修的 bug 现场，
# 而是 bug 本身的根因——第 1 集永远是"其它已发布分集"最空的一集，未绑定
# 角色落 functional_extras 从不写别名，形成死循环（详见设计文档 §2.3，
# 真实案例：许清 EP1/EP5/EP6 三集三种措辞、无一绑定，EP13 才第一次绑上）。
# 人物谱在全书分析阶段就已经知道这些别名，不需要等任何一集先发布。
# P2 §16 决定：旧扫描路径保留一段时间做双重校验再退役，但只在人物谱对这个
# 别名字符串毫无记录时才补充生效——绝不允许旧路径的结论推翻人物谱已经给出
# 的结论（人物谱是唯一被要求携带逐字证据锚点的数据源，可信度更高）。
