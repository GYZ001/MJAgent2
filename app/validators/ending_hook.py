"""尾钩（ending hook）取证判据：校验剧本声明的尾钩事件是否有原文支撑，
供剧本尾钩校验与发布前置校验复用。
"""
from __future__ import annotations

from typing import Any

from app.schemas import StoryEvent

from .screenplay_text import (
    KEY_POINT_COVERAGE,
    _bigram_coverage,
    _bigram_set,
    _longest_run_ratio,
    _strip_speaker,
)

COVERS_ATOM_ABSENT_RUN = 0.3
COVERS_ATOM_ABSENT_COVERAGE = 0.25
def _claim_clearly_absent(atom: str, haystack: str) -> bool:
    """这条原子在文本里是否"几乎完全没出现"——主干连续命中和 2-gram 覆盖都低于宽松下限才算缺失。"""
    core = _strip_speaker(atom)
    if (_longest_run_ratio(core, haystack) >= COVERS_ATOM_ABSENT_RUN
            or _bigram_coverage(core, haystack) >= COVERS_ATOM_ABSENT_COVERAGE):
        return False
    return True


# ending_hook 是叙事摘要式的一句话陈述（"下一步会发生什么"），不是逐字引用，天然比
# 逐字台词/事实陈述的 2-gram 命中率低。参照 COVERS_ATOM_ABSENT_COVERAGE=0.25 同量级，
# 但取更宽松的下限：目标只是拦住"正文完全没有对应内容、纯属编造"的钩子，不能把
# 真实但高度改写/概括的钩子误判为编造进而清空。
#
# 这条 2-gram 覆盖率本身**不足以**独立当作防编造门禁：字符 2-gram 是无序集合，
# "复用真实词汇编造情节"（沿用剧本里出现过的人名/常用词，拼出一句正文里从未
# 发生的事）在几百到几千字的整篇正文里几乎总能凑出 >0.4 的覆盖率——已用真实
# 案例验证。因此单独达标只作为最低门槛（挡住完全无关词汇的硬编，如"外星飞船
# 降落"），不再是唯一判据；见下方 events 结构化校验。
ENDING_HOOK_GROUNDING_COVERAGE = 0.2

# 结构化交叉校验 Tier A：与 full_script_text 的整篇正文不同，这里的比对对象是
# 单条 StoryEvent 的精炼字段（trigger/visible_change/state_out/source_fact），
# 文本量小两个数量级，"复用词汇但内容无关"很难再靠随机碰撞凑出覆盖率——真正
# 编造的钩子对不上任何一条真实事件。取值复用本文件里 KEY_POINT_COVERAGE
# （0.34）：同属"概括性描述 vs 结构化条目"的比对量级。
ENDING_HOOK_EVENT_COVERAGE = KEY_POINT_COVERAGE

# 结构化交叉校验 Tier B：场次分片路径实测会把一集拆成几百条原子事件（EP4 真实
# 数据：269 条事件覆盖 5998 字正文，单事件精炼字段中位数仅 76 字符）。这种粒度
# 下 ending_hook——"对结尾 5~8 条事件的一句话综合概括"——天然不可能在单条事件
# 上摸到 0.34：概括用词被摊薄到每条事件里只剩几个字。Tier A 单事件比对本身不动
# （历史已验证：真实钩子在粗粒度事件表上普遍能摸到 0.4~0.7，编造钩子摸不到
# 0.34），Tier A 落空时才启用 Tier B。
#
# Tier B 把比对对象换成事件表**末尾**最多 ENDING_HOOK_WINDOW_MAX_EVENTS 条相邻
# 事件拼接的文本——ending_hook 描述的是"本集结尾"，证据理应集中在事件表尾部，
# 而不是散落在全篇任意位置。限定末尾窗口是关键：EP4 269 条真实事件实测过，若
# 允许窗口滑动到任意位置且不设上限，窗口一旦扩大到 20 条事件，覆盖率会跳到
# 0.43，与编造钩子对整篇正文的覆盖率同量级——退化回第一层已被证明拦不住编造
# 的弱判据。限定末尾 + 有限窗口大小后这一退化未再出现（末尾窗口从 5 条扩到 15
# 条，覆盖率稳定停在 0.297，说明这就是真实上限，不是窗口不够大）。
#
# 只降覆盖率门槛（0.34→0.28）不足以独立成立：单降门槛，编造钩子只要把窗口开大
# 到吞下几乎全部事件，一样能在小样本里凑出同量级覆盖率——这仍是"退化成整篇
# 比对"的老问题。因此额外要求窗口内至少 ENDING_HOOK_WINDOW_MIN_CONTRIBUTORS 条
# 事件分别贡献"其它事件都给不出"的独立命中 2-gram：真钩子的证据分布在多条事件
# 里，编造钩子即使窗口再大，命中的 2-gram 几乎全部只来自最先"运气好"命中的那
# 一条事件，其余事件净贡献为零。
#
# 第 10 轮真实回归：5 集里 3 集真钩子被 Tier B 误杀（EP1/EP2/EP4，provider_calls
# kind=ending_hook_grounding_rejected 三条真实记录）。逐条核对发现 EP2 那条自相
# 矛盾——窗口覆盖率 0.2857（远超阈值）但 contributors=0。溯源到 StoryEvent.
# state_out 字段的 schema 设计：它按约定原样复述"下一条"事件的 trigger（"当前
# 动作完成，局势推进到下一事件「<next.trigger>」发生前"）。逐事件核对 EP2 真实
# 数据：E226.state_out 与 E227.trigger 逐字重复。这让相邻事件在 Tier B 窗口里
# 天然共享大段字面重叠——不是因为词汇碰巧复用，是 schema 本身让事件 N 的文本
# 包含了事件 N+1 的文本。"贡献者=该 2-gram 在窗口内其它事件里完全没出现"这一
# 定义假设窗口内各事件的命中集合互不重叠，被这一结构性复述系统性打破：两条
# 独立的真实事件各自命中同一批 2-gram（因为其中一条字面包含了另一条），互相
# 抵消判定为零独立贡献，而这批 2-gram 明明来自两条不同事件描述的同一情节。
# 这是计算口径的 bug，不是"证据不够"——用 5 条真实生产钩子 + 5 条编造钩子
# （4 条已知能拦下 + 1 条已知漏网）离线标定验证：只把 Tier B 覆盖率/贡献者
# 计算改用不含 state_out 的核心字段（trigger+visible_change+source_fact，
# 见 _ending_hook_event_core_text），contributors 不再虚假归零——EP1/EP2/
# EP4 三条真钩子在 MIN_CONTRIBUTORS=2 下全部稳定摸到 2（EP2 从 0→2，EP4 从
# 4→2 但不再受益于 state_out 重复注入的虚高覆盖率），5 条编造样本在窗口
# 2~15 的全部尺寸下最高只摸到 1（多数为 0），2 与其余样本之间有稳定的整数
# 缺口，不是靠卡阈值卡出来的巧合。
#
# 覆盖率门槛 0.28→0.10：contributors 口径修复后，覆盖率不再是主判据（已被
# MIN_CONTRIBUTORS=2 的整数缺口挡住），只需兜底"贡献够但内容量微不足道"的
# 边角情况。0.10 的取值依据：三条真实误杀钩子里覆盖率最低的一条（EP1，核心
# 字段口径下 0.1111）必须能过，同时仍显著高于 0（拦住"凑够 2 条贡献事件但
# 每条只命中一两个无意义 2-gram"的退化情况）。5 条编造样本此时全部仍被
# MIN_CONTRIBUTORS=2 挡住，与覆盖率门槛具体取值无关——即使编造样本覆盖率
# 达标（如样本 B 达 0.2222），contributors 上限只有 1，过不了 2 的门槛。
#
# 已知局限：这仍是字符 2-gram 无序集合比对，不理解语义关系。刻意拼接跨多条
# 真实事件的**逐字/近逐字**片段、伪造它们之间从未发生过的因果关联，理论上仍可
# 能同时喂出足够覆盖率与足够的独立贡献事件数——这是任何词袋类溯源判据的通病
# （第一层的整篇比对同样存在，且更严重），不是本次修复引入的新缺口；本次修复
# 只针对已确认的失败模式（state_out 复述拖累的贡献者虚假归零、粒度化事件表
# 拖累的假阳性），不追求对抗任意精心构造的拼接攻击。
ENDING_HOOK_WINDOW_COVERAGE = 0.10
ENDING_HOOK_WINDOW_MAX_EVENTS = 8
ENDING_HOOK_WINDOW_MIN_CONTRIBUTORS = 2

# 覆盖率门槛降到 0.10 后暴露了另一个真实回归：contributors≥2 本身可以靠两条
# 事件各贡献恰好 1 个字符 2-gram 凑出来——而 1 个 2-gram 往往就是一个人名
# （"李明"/"王芳"这类两字词）。编造钩子只要蹭到窗口里两条不同事件各自出现
# 过的人名，就能在完全没有真实情节支持的情况下"凑够 2 条独立贡献事件"。
# 已用现有回归夹具实测坐实（tests/test_narrative_continuity.py 的 5 事件小样
# 本，事件颗粒度比真实生产数据粗得多）：编造钩子"李明看着王芳，忽然想起多年
# 前的一段往事……"能在窗口 [E2,E3,E4,E5] 摸到 contributors=2，但两条贡献
# 分别只是"往事"（来自 E2 自己也在讲的"往事"一词）和"李明"（来自 E4 提到
# 的"李明母亲"），没有一条命中量高于单个人名/常见词的量级。真实钩子里至少
# 有一条贡献事件的独立命中远不止 1 个 2-gram（EP1 实测最小值仍有 2、EP2/
# EP3/EP4/EP5 更高，见上方门槛调整记录）。因此额外要求窗口内命中量最大的
# 那条贡献事件至少独立命中 ENDING_HOOK_WINDOW_MIN_STRONG_CONTRIBUTION 个
# 2-gram，把"两条事件各自贡献 1 个人名"这类退化情况和"至少一条事件贡献一
# 小段有实质内容的文本"区分开。
ENDING_HOOK_WINDOW_MIN_STRONG_CONTRIBUTION = 2


def _ending_hook_event_text(event: StoryEvent) -> str:
    """StoryEvent 里跟"发生了什么"直接相关的精炼字段拼接，供钩子溯源比对。"""
    return "".join([
        event.trigger or "",
        event.visible_change or "",
        event.state_out or "",
        event.source_fact or "",
    ])


# Tier B 专用：不含 state_out。真实生产数据验证过，state_out 按 schema 设计会
# 原样复述"下一条"事件的 trigger（"当前动作完成，局势推进到下一事件「<next
# trigger>」发生前"），即事件 N 的 state_out 字面包含事件 N+1 的内容。这对单
# 事件 Tier A 比对无害，但 Tier B 的"独立贡献事件数"要求恰恰假设窗口内每条
# 事件贡献的命中 2-gram 互不重叠；相邻事件之间这条 schema 内建的复述会人为
# 制造重叠，导致明明是两条不同事件分别命中、真实存在的证据被判定为"互相抵
# 消、零独立贡献"（真实回归实例：EP2 窗口覆盖率 0.2857 但 contributors=0，
# 逐事件核对确认 E226.state_out 与 E227.trigger 逐字重复，与词汇复用无关，
# 是统计口径 bug 不是真实证据缺失）。Tier B 的覆盖率与贡献者数改用这份不含
# state_out 的文本计算，消除该人为重叠；Tier A 与 eligible 事件池本身不变。
def _ending_hook_event_core_text(event: StoryEvent) -> str:
    return "".join([
        event.trigger or "",
        event.visible_change or "",
        event.source_fact or "",
    ])


def _ending_hook_eligible_events(
    events: list[StoryEvent] | None,
) -> list[tuple[str, str, str]]:
    """Tier A/Tier B 共用的事件过滤：有素材文本、有来源证据（source_fact 或
    source_span 任一非空）、且不是未批准的改编新增。保留原始顺序（events 列表
    本身即按叙事顺序排列），返回 (event_id, haystack, core_haystack) 列表——
    haystack 供 Tier A 单事件比对，core_haystack（不含 state_out）供 Tier B
    窗口比对，见 _ending_hook_event_core_text 上方注释。"""
    eligible: list[tuple[str, str, str]] = []
    for event in events or []:
        haystack = _ending_hook_event_text(event)
        if not haystack:
            continue
        if not ((event.source_fact or "").strip() or (event.source_span or "").strip()):
            continue
        if event.adaptation_addition and not event.approved:
            # 未批准的改编新增：即便文本命中，也恰恰证明 ending_hook 复述的是
            # 一条尚未被批准的发明内容，不能当作已溯源，整条排除出候选池。
            continue
        eligible.append((event.event_id, haystack, _ending_hook_event_core_text(event)))
    return eligible


def _ending_hook_window_match(
    text: str,
    eligible: list[tuple[str, str, str]],
) -> dict[str, Any]:
    """Tier B：事件表末尾滑动窗口（相邻事件拼接）比对。覆盖率与贡献者数都基于
    core_haystack（不含 state_out，见 _ending_hook_event_core_text），避免
    schema 内建的"事件 N 的 state_out 复述事件 N+1 的 trigger"人为拉高相邻
    事件间的重叠、压低独立贡献事件数。

    始终返回实际尝试过的最佳窗口信息（覆盖率最高的一次），无论最终是否达标——
    调用方在判定编造时要能记录"差多少、差在哪个窗口"，而不只是一个 bool。
    """
    hook_bigrams = _bigram_set(text)
    n = len(eligible)
    best: dict[str, Any] = {
        "passed": False,
        "window_size": 0,
        "coverage": 0.0,
        "contributors": 0,
        "strongest_contribution": 0,
        "event_ids": [],
    }
    if n < 2 or not hook_bigrams:
        return best
    for window_size in range(2, min(ENDING_HOOK_WINDOW_MAX_EVENTS, n) + 1):
        window = eligible[n - window_size:]
        matches = [_bigram_set(core_haystack) & hook_bigrams for _, _, core_haystack in window]
        union: set[str] = set()
        for match in matches:
            union |= match
        coverage = len(union) / len(hook_bigrams)
        contributors = 0
        strongest_contribution = 0
        for index, match in enumerate(matches):
            others: set[str] = set()
            for other_index, other_match in enumerate(matches):
                if other_index != index:
                    others |= other_match
            unique = match - others
            if unique:
                contributors += 1
                strongest_contribution = max(strongest_contribution, len(unique))
        passed = (
            coverage >= ENDING_HOOK_WINDOW_COVERAGE
            and contributors >= ENDING_HOOK_WINDOW_MIN_CONTRIBUTORS
            and strongest_contribution >= ENDING_HOOK_WINDOW_MIN_STRONG_CONTRIBUTION
        )
        if passed:
            best = {
                "passed": True,
                "window_size": window_size,
                "coverage": coverage,
                "contributors": contributors,
                "strongest_contribution": strongest_contribution,
                "event_ids": [event_id for event_id, _, _ in window],
            }
            break
        if coverage >= best["coverage"]:
            best = {
                "passed": False,
                "window_size": window_size,
                "coverage": coverage,
                "contributors": contributors,
                "strongest_contribution": strongest_contribution,
                "event_ids": [event_id for event_id, _, _ in window],
            }
    return best


def ending_hook_grounding_report(
    ending_hook: str,
    full_script_text: str,
    events: list[StoryEvent] | None = None,
) -> dict[str, Any]:
    """ending_hook 溯源判定的完整诊断信息，供调用方在判定为编造并清空时记录
    可观测证据：被清空的钩子原文、两层覆盖率实测值、最佳匹配 event id/窗口。

    report["tier"] 取值：
      "empty"       —— 钩子本身为空，合法放行；
      "layer1_fail" —— 连宽松的整篇正文 2-gram 门槛都过不了，判定编造；
      "no_events"   —— 未传入 events（早期生成阶段），只应用第一层，放行；
      "tierA"       —— 命中某一条单独事件，覆盖率 ≥ ENDING_HOOK_EVENT_COVERAGE；
      "tierB"       —— 命中末尾相邻事件滑动窗口，覆盖率 ≥
                        ENDING_HOOK_WINDOW_COVERAGE 且独立贡献事件数达标；
      "ungrounded"  —— 前两层结构化校验都没有找到支持，判定编造。
    """
    text = (ending_hook or "").strip()
    report: dict[str, Any] = {
        "grounded": True,
        "hook_text": text,
        "layer1_coverage": None,
        "tier": None,
        "best_event_id": None,
        "best_event_coverage": 0.0,
        "window": None,
    }
    if not text:
        report["tier"] = "empty"
        return report
    layer1_coverage = _bigram_coverage(text, full_script_text or "")
    report["layer1_coverage"] = layer1_coverage
    if layer1_coverage < ENDING_HOOK_GROUNDING_COVERAGE:
        report["grounded"] = False
        report["tier"] = "layer1_fail"
        return report
    if not events:
        report["tier"] = "no_events"
        return report
    eligible = _ending_hook_eligible_events(events)
    best_event_id: str | None = None
    best_event_coverage = 0.0
    for event_id, haystack, _core_haystack in eligible:
        coverage = _bigram_coverage(text, haystack)
        if coverage > best_event_coverage:
            best_event_coverage = coverage
            best_event_id = event_id
        if coverage >= ENDING_HOOK_EVENT_COVERAGE:
            report["tier"] = "tierA"
            report["best_event_id"] = event_id
            report["best_event_coverage"] = coverage
            return report
    report["best_event_id"] = best_event_id
    report["best_event_coverage"] = best_event_coverage
    window = _ending_hook_window_match(text, eligible)
    report["window"] = window
    if window["passed"]:
        report["tier"] = "tierB"
        return report
    report["grounded"] = False
    report["tier"] = "ungrounded"
    return report


def ending_hook_is_grounded(
    ending_hook: str,
    full_script_text: str,
    events: list[StoryEvent] | None = None,
) -> bool:
    """ending_hook 是否确有本集正文内容支持；空值本身合法，直接放行。

    结构化判据分三层，逐级放宽比对粒度（详见 ending_hook_grounding_report 与
    ENDING_HOOK_WINDOW_* 常量上方注释）：
    1）字符 2-gram 覆盖率门槛（宽松，只挡完全无关词汇的硬编）；
    2）Tier A：与单条 StoryEvent 精炼字段比对，覆盖率 ≥ ENDING_HOOK_EVENT_COVERAGE
       （=KEY_POINT_COVERAGE=0.34）；
    3）Tier B：Tier A 落空时，与事件表末尾相邻若干条事件的滑动窗口拼接比对
       （核心字段口径，不含 state_out，见 _ending_hook_event_core_text）：覆盖率
       ≥ ENDING_HOOK_WINDOW_COVERAGE（0.10）、至少 ENDING_HOOK_WINDOW_MIN_CONTRIBUTORS
       （2）条事件独立贡献命中、且其中至少一条贡献量达
       ENDING_HOOK_WINDOW_MIN_STRONG_CONTRIBUTION（2 个 2-gram，拦住"两条事件各
       自只贡献一个人名"这类退化情况）——处理"结尾被拆成一串细碎原子事件、钩子
       是对它们的一句话综合概括"这种真实但天然摸不到单事件 0.34 的场景，同时仍
       能拦住复用词汇但内容无关的编造（结构化事件溯源）。

    events 为空（未传入，例如尚未编译出事件台账的早期生成阶段）时只应用
    第 1 层，保持既有行为不变。
    """
    return ending_hook_grounding_report(ending_hook, full_script_text, events)["grounded"]
