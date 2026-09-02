"""当前身份决议的语义签名、去重合并、消歧 key 与决议上限计算，
以及 schema 违规的分类判定。
"""

from __future__ import annotations

import re

from app.evidence import repository as evidence_repository

from .constants import (
    CURRENT_IDENTITY_LITERAL_PROVENANCE,
    CURRENT_IDENTITY_SYNTHETIC_PROVENANCE,
)
from .discovery_resample import _bounded_owned_identity_evidence

def _current_identity_semantic_signature(item: dict) -> tuple[str, ...]:
    """Return the identity fields which must agree across owned occurrences."""
    return (
        str(item.get("source_label") or "").strip(),
        str(item.get("identity_kind") or "").strip(),
        str(item.get("name") or "").strip(),
        str(item.get("identity_group") or "").strip(),
        str(item.get("authority_id") or "").strip(),
        str(item.get("existing_route_name") or "").strip(),
        str(item.get("source_label_provenance") or "").strip(),
        "1" if item.get("materialization_compatible") else "0",
        str(item.get("_current_response_group_key") or "").strip(),
    )


def _current_identity_durable_signature(item: dict) -> tuple[str, ...]:
    """Stable identity signature after request-local F/P tokens are resolved."""
    return _current_identity_semantic_signature(item)[:-1]


def _current_identity_declared_signature(item: dict) -> tuple[str, ...]:
    """模型自己申报的内容签名（第27轮真实回归 ERR-20260824-079190，见
    _project_current_identity_response 的 by_label 合并循环上方完整
    说明）：排除 identity_group/source_label_provenance 这两个后端为
    "这次具体出现"单独推导、会随手气浮动的字段（identity_group 在
    synthetic 分支下按 evidence_id 生成哈希；source_label_provenance
    取决于这次引用是否恰好落在"全批唯一逐字锚点"自动改写的判定窗口
    内），只保留模型真正申报的身份判断本身：identity_kind、canonical_
    name（named 分支）、authority_id、existing_route_name、
    functional_identity_key（f 分支，即 _current_response_group_key）、
    kind（onscreen/mentioned）。跟 _current_identity_durable_signature
    是两把不同的尺子：durable signature 问"这两次出现在后端眼里是不是
    完全一样"，declared signature 问"模型自己申报的内容是不是完全一样"
    ——同一 source_label 下两次出现 durable signature 不一致，不代表
    模型自相矛盾，可能只是后端对其中一次的证据判定恰好没找到全批唯一
    逐字锚点。"""
    return (
        str(item.get("identity_kind") or "").strip(),
        str(item.get("name") or "").strip(),
        str(item.get("authority_id") or "").strip(),
        str(item.get("existing_route_name") or "").strip(),
        str(item.get("_current_response_group_key") or "").strip(),
        str(item.get("kind") or "").strip(),
    )


def _current_identity_receipt_sort_key(value: dict) -> tuple[object, ...]:
    return (
        str(value.get("origin") or ""),
        str(value.get("source_hash") or ""),
        int(value.get("start_offset") or 0),
        int(value.get("end_offset") or 0),
        str(value.get("evidence_id") or ""),
    )


def _merge_current_identity_occurrences(options: list[dict]) -> dict:
    """Merge one exact semantic identity while retaining every typed receipt.

    真实 EP5 回归（ERR-20260828-2fa501，"老者"F3 子组）：declared-signature
    + has_literal_anchor 这条合并路径（马脸青年案①，见
    _current_identity_reconcile_as_single）可以把一个 source_label_
    provenance=literal 的 occurrence 和一个 provenance=synthetic 的
    occurrence 合到同一条身份——declared signature 本就不比较 provenance/
    identity_group（见 _current_identity_declared_signature 的说明），两条
    只要模型申报的字段本身相同就会被判定"同一人"，不要求 provenance 也
    一致。旧实现只按 kind=="onscreen" 挑 strongest 当合并基底，receipt
    列表却无条件并入全部 options 的 receipt——最终 provenance 继承自
    strongest，receipt 列表却可能来自另一条 provenance 不同的 occurrence，
    破坏了 receipt v2 自己的不变量（owned_current_literal.v1 要求
    source_label 逐字出现在每一条 receipt 文本里；provider_synthetic_
    functional.v1 要求恰好一条且不逐字出现），无论 strongest 落在哪一边
    都必定校验失败（_validate_current_identity_receipt_bundle）。

    _current_identity_reconcile_as_single 的两条合并路径已经保证：一旦
    len(options) > 1 真正触发合并，要么全体 provenance 一致（durable
    signature 完全相等的直接合并路径——synthetic_repeat 检查已经挡掉了
    "全体一致但等于 synthetic" 的情形，只剩全体 literal 一种可能），要么
    至少有一条是 literal（declared-signature 路径要求 has_literal_
    anchor）。不存在"全体 synthetic 却被合并"的情形。于是只要合并组里
    出现任何一条 literal，最终结果就必须以 literal 为准：strongest 优先
    从 literal occurrence 里选，receipt 列表过滤掉不逐字含 source_label
    的 receipt——这些 receipt 从未真正见证过这个逐字身份，保留它们只会让
    receipt v2 校验永远无法通过，不是"该保留的证据"（source_labels 折叠
    逻辑本身不动，这里只收窄了同一条身份最终携带的 receipt 集合）。
    """
    if not options:
        raise ValueError("current identity occurrence merge requires candidates")
    ordered = sorted(
        options,
        key=lambda item: _current_identity_receipt_sort_key(
            item.get("source_evidence_receipt") or {}
        ),
    )
    literal_ordered = [
        item for item in ordered
        if item.get("source_label_provenance") == CURRENT_IDENTITY_LITERAL_PROVENANCE
    ]
    strongest_pool = literal_ordered or ordered
    strongest = next(
        (item for item in strongest_pool if item.get("kind") == "onscreen"),
        strongest_pool[0],
    )
    final_provenance = strongest.get("source_label_provenance")
    source_label = str(strongest.get("source_label") or "").strip()
    receipt_by_id: dict[str, dict] = {}
    for item in ordered:
        raw_receipts = item.get("source_evidence_receipts")
        receipts = (
            raw_receipts
            if isinstance(raw_receipts, list)
            else [item.get("source_evidence_receipt")]
        )
        for raw in receipts:
            if not isinstance(raw, dict):
                continue
            evidence_id = str(raw.get("evidence_id") or "").strip()
            if evidence_id:
                receipt_by_id.setdefault(evidence_id, dict(raw))
    pooled_receipts = sorted(
        receipt_by_id.values(), key=_current_identity_receipt_sort_key
    )
    if final_provenance == CURRENT_IDENTITY_LITERAL_PROVENANCE:
        receipts = [
            receipt for receipt in pooled_receipts
            if source_label and source_label in str(receipt.get("text") or "")
        ]
    else:
        receipts = pooled_receipts
    # The singular receipt is compatibility-only in RF11.  Keep it derivable
    # from the durable v2 list: the canonical first receipt is the primary,
    # while the candidate kind is still the strongest occurrence across all
    # receipts.  We intentionally do not encode an unverifiable per-receipt
    # kind inside the backend evidence seal.
    primary_receipt = receipts[0] if receipts else None
    source_segment_ids = list(dict.fromkeys(
        str(receipt.get("source_segment_id") or "").strip()
        for receipt in receipts
        if str(receipt.get("source_segment_id") or "").strip()
    ))
    merged = {
        **strongest,
        "source_evidence_receipts": receipts,
        "source_segment_ids": source_segment_ids,
    }
    if primary_receipt is not None:
        primary_text = str(primary_receipt.get("text") or "")
        primary_evidence = _bounded_owned_identity_evidence(
            primary_text,
            anchors=[source_label] if source_label in primary_text else [],
            max_chars=80,
        ) or primary_text.strip()[:80]
        merged.update({
            "source_evidence_receipt": dict(primary_receipt),
            "source_segment_id": str(
                primary_receipt.get("source_segment_id") or ""
            ),
            "source_quote": primary_text,
            "evidence": primary_evidence,
        })
    return merged


def _current_identity_disambiguation_key(item: dict) -> str:
    """模型是否已经用结构性字段区分了"这是哪个人"的键（第31轮 EP5
    ERR-20260824-614276）：功能身份（f 分支）看 functional_identity_key
    （即 _current_response_group_key，append_candidate 只在
    identity_kind=="functional" 时才填这个字段——prompt 规则6"同一
    functional_identity_key=同一人"，模型自己申报的分组信号）；已登记
    具名身份（k 分支）append_candidate 时 _current_response_group_key
    恒为空串（见该调用点），改看 authority_id——k 分支的 decision_id 精确
    复用同一个已登记决议才会解析出同一个 authority_id，这是同一层级的
    "模型精确复用同一个 ID = 申报同一个人"信号，decision_id 本身在
    _project_current_identity_response 里已经被消费掉、没有原样保留到
    这一层，authority_id 是它解析后的等价代理。两者都拿不到时返回空串
    ——空串一律各自成组（没有任何区分信号，不能假装它们是被区分开的）。

    真实 EP4 回归（"同宗"，两个 n 分支 referential 称谓被降级为 functional，
    见 append_candidate 的 N 分支调用点）：_current_response_group_key 在
    这条降级路径上来自 _identity_form_functional_key，是对标签文本本身的
    纯哈希，跟"模型自己申报的分组信号"不是一回事——它只随标签文字变化，
    完全不随"这次和上次是不是同一个人"变化。两个不同的指称对象共用同一
    个泛指标签（"同宗"）时会拿到完全相同的哈希，如果仍当作强区分信号，
    会把它们错误地锁进同一个消歧子组，导致下面的甲/乙自动区分机制拿不到
    第二个子组、无法补足限定语，只能致命失败——而它们本该和"老者"F3/F4
    案一样，退回按申报签名聚类（签名相同的仍归一/合并，签名不同的各自
    成组，交由甲/乙机制处理）。用 _current_identity_group_key_synthetic
    标记区分这两类 key 来源：只有这条降级路径置位，真正 F 分支模型主动
    声明的 functional_identity_key 不受影响，继续原样当强信号使用——
    "马脸青年案②分支"（同一个真 F key 但 kind 自相矛盾时必须维持致命）
    不受本次改动影响。
    """
    group_key = str(item.get("_current_response_group_key") or "").strip()
    if group_key and not item.get("_current_identity_group_key_synthetic"):
        return group_key
    authority_id = str(item.get("authority_id") or "").strip()
    if authority_id:
        return authority_id
    if group_key:
        # 后端合成的哈希键本身不携带"是不是同一个人"的信号，但模型自己
        # 申报的其它字段（declared signature）仍然是真实信号：签名相同的
        # 继续聚在一起，交给 _current_identity_reconcile_as_single 的既有
        # ①②判据决定合并/归一；签名不同（如本例的 kind 不一致）的各自
        # 落进不同的 key，从而被拆成不同子组。
        return "declared:" + "\x1f".join(_current_identity_declared_signature(item))
    return f"__no_key__:{id(item)}"


def _current_identity_reconcile_as_single(options: list[dict]) -> dict | None:
    """尝试把一组"同一 (source_label, scope_qualifier) 复合键"下的候选
    归一成一条身份，复用既有①②判据（见 _project_current_identity_response
    的 by_label 合并循环）：durable signature 全等 -> 直接合并；申报字段
    签名全等 + 至少一条逐字锚定 -> 归一（标记 _current_identity_
    normalized_duplicate）。两条都不满足则返回 None（调用方决定是当作
    "不同人只是缺限定语"继续按子组拆分，还是真矛盾致命）——本函数纯判定，
    不写 errors，不认识"是第几层调用"。"""
    signatures = {_current_identity_durable_signature(item) for item in options}
    synthetic_repeat = len(options) > 1 and any(
        item.get("source_label_provenance") == CURRENT_IDENTITY_SYNTHETIC_PROVENANCE
        for item in options
    )
    if len(signatures) == 1 and not synthetic_repeat:
        return _merge_current_identity_occurrences(options)
    declared_signatures = {
        _current_identity_declared_signature(item) for item in options
    }
    has_literal_anchor = any(
        item.get("source_label_provenance") == CURRENT_IDENTITY_LITERAL_PROVENANCE
        for item in options
    )
    if len(declared_signatures) == 1 and has_literal_anchor:
        normalized = _merge_current_identity_occurrences(options)
        normalized["_current_identity_normalized_duplicate"] = True
        return normalized
    return None


# The one field a K decision may echo without adding anything: its token
# already binds the evidence, so ``evidence_ref`` alongside it restates a
# receipt the backend owns outright (production EP4: ``k.0.evidence_ref Extra
# inputs are not permitted`` failed a whole episode over that echo).  Naming it
# is the point -- "drop whatever the K schema does not declare" reads like the
# same rule but is a different, much wider one, and it deletes model-authored
# content too.  Real incident run_690cebdd45a7 (ep_bf9051d167a7 EP1): the model
# nested the whole K/N/F wire one level too deep, writing ``f``/``k``/``n``
# inside ``k[0]``; the wider rule swept all three away, leaving ``{"decision_id":
# ...}``, and the failure surfaced as "k.0.kind Field required" -- a missing
# field, with no trace of the misplaced N branch that was the actual fault.
_CURRENT_KNOWN_BACKEND_OWNED_ECHO_KEYS = frozenset({"evidence_ref"})


def _normalize_current_identity_payload(payload: dict) -> dict:
    """Drop fields the K/N/F wire declares redundant before strict validation.

    Only keys the model has no authority over are removed (see
    ``_CURRENT_KNOWN_BACKEND_OWNED_ECHO_KEYS``); anything else it wrote stays
    and still fails closed, on K exactly as on N/F, because those bytes are
    model-authored content the backend cannot second-guess -- and because a
    strict-schema rejection that names what the model actually sent is the
    only thing that makes the next failure diagnosable.
    """
    if not isinstance(payload, dict):
        return payload
    known = payload.get("k")
    if not isinstance(known, list):
        return payload
    cleaned: list[dict] = []
    changed = False
    for item in known:
        if not isinstance(item, dict):
            cleaned.append(item)
            continue
        trimmed = {
            key: value for key, value in item.items()
            if key not in _CURRENT_KNOWN_BACKEND_OWNED_ECHO_KEYS
        }
        changed = changed or trimmed != item
        cleaned.append(trimmed)
    if not changed:
        return payload
    return {**payload, "k": cleaned}


def _resolved_evidence_ref(raw: str, expected_refs: set[str]) -> str:
    """Repair a zero-padding slip in an otherwise in-catalog evidence ref.

    The schema pins ``evidence_ref`` to a closed enum, but the provider does
    not always honour strict mode: production delivered ``E01`` for a catalog
    holding ``E001``.  Re-padding is pure formatting -- it selects an existing
    backend-owned receipt and only when exactly one matches, so it can never
    move a decision onto a different span.  Anything ambiguous is returned
    unchanged and still fails closed.
    """
    ref = str(raw or "").strip()
    if ref in expected_refs:
        return ref
    match = re.fullmatch(r"([A-Za-z]+)([0-9]+)", ref)
    if match is None:
        return ref
    prefix, digits = match.group(1), match.group(2).lstrip("0") or "0"
    candidates = [
        candidate for candidate in expected_refs
        if candidate.startswith(prefix)
        and (candidate[len(prefix):].lstrip("0") or "0") == digits
    ]
    return candidates[0] if len(candidates) == 1 else ref


def _identity_form_functional_key(identity_label: str) -> str:
    """Stable per-label group key for an appellation demoted out of N."""
    return "NF" + evidence_repository.content_hash(
        str(identity_label or "").strip()
    )[:12]



# k/n/f 计数帽的下限：与批规模脱钩前的历史基线（见 _current_identity_decision_cap
# 的完整推导），任何批次都不会比这更严。
_CURRENT_IDENTITY_DECISION_CAP_FLOOR = 64
# 每个 evidence ref 允许的决策密度倍数：见 _current_identity_decision_cap 的
# 完整推导与真实校准数据。
_CURRENT_IDENTITY_DECISION_CAP_PER_REF = 3


def _current_identity_decision_cap(evidence_ref_count: int) -> int:
    """k/n/f 单分支计数帽（第22轮总审计 ERR-20260824-aeee2d 修复，参数重推导）。

    考古：这道帽子最早（commit 5accd39/a2aeab2）是 RF10 "每个 evidence ref
    自带一份 k/n/f、各自封顶 64" 形状下的产物——分母是"一个 evidence ref"
    （≤900 字的一个自然段），64 对一个短段落里能出现的人数已经非常宽松。
    紧接着的下一次改动把响应形状压平成"整批只输出一次全局 k/n/f"（RF11，
    _current_identity_schema 的 docstring："RF10 要求每个 evidence span 都配一
    份 K/N/F……RF11 只在三个全局数组里输出被选中的身份"），分母从"一个
    evidence ref"变成了"一整批 evidence ref"，但那个 64 的数字被原样照抄过来，
    从未重新推导——批规模一旦变大，同一个常量就变得越来越紧，跟它原本要防的
    "单段文本里的失控值"已经不是同一件事。

    真实回归 ERR-20260824-aeee2d（第 22 轮 EP3，provider_calls.id=8964）：该批
    对话密集，共 84 个 evidence ref（许多角色反复对话，每次出场都是独立的
    evidence ref），模型如实为已登记角色选了 78 条合法 k 决议（78/84≈0.93
    条/ref），全部指向 known_decisions 目录里真实存在的条目——不是幻觉或
    失控，只是这一集对话轮次天然多。旧的固定 64 硬把这批合法输出当成失控拒绝。

    公式：max(_CURRENT_IDENTITY_DECISION_CAP_FLOOR, 批次 evidence ref 数量 *
    _CURRENT_IDENTITY_DECISION_CAP_PER_REF)。k 分支另有下界：不得低于本批目录里
    实际提供的 K 决议数（调用方 _project_current_identity_response 取
    max(本函数, len(known_decisions))）——每个已登记称谓在每条证据里都必须选 K，
    选满目录是契约要求的结果。ERR-20260902-b227f9（《三国演义》第一回第二轮）：
    人物谱 15 人 × 7 条证据提供了 87 条 K，模型如实选了 87 条，被 64 拒绝。
    - 下限 64：保留历史基线，≤21 个 evidence ref 的批次（绝大多数集数）行为
      与改动前完全一致，不放宽任何既有防护。
    - 倍数 3：以 ERR-20260824-aeee2d 的真实密度（≈1 条/ref）为校准点，
      3 倍留出多人同段出场的余量，同时仍是一个会拒绝真正失控值（比如模型
      对同一批复读上千条重复决议）的真实上限，不是形同虚设的"数据字段"。
    该帽子只做资源/失控防线：每一条 k/n/f 决议是否真的锚定到后端自己的证据
    目录，由 _project_current_identity_response 里逐条的"越界"校验独立把关
    （k：decision_id 必须命中 known_decisions；n/f：evidence_ref 必须落在
    expected_refs 内），计数帽拿掉也不会削弱那道锚定护栏。
    """
    return max(
        _CURRENT_IDENTITY_DECISION_CAP_FLOOR,
        int(evidence_ref_count) * _CURRENT_IDENTITY_DECISION_CAP_PER_REF,
    )



class _CurrentIdentitySchemaViolation(str):
    """A projection error whose violated constraint is declared in the wire
    JSON schema (``_current_identity_schema()``'s ``enum``/``required``/
    ``additionalProperties`` -- the keywords that actually survive
    ``_identity_strict_provider_schema``'s whitelist and reach the provider;
    ``maxItems``/``minLength``/``maxLength`` do not and so never qualify).

    RCA ERR-20260824-e3628f: a supplier occasionally violates its own
    strict-mode ``enum`` contract at low frequency (~0.5%) on deep array
    elements. That is a format defect of the sampled answer, not a business
    disagreement -- resampling the whole episode is safe. Behaves exactly
    like ``str`` everywhere (equality, ``in``, ``"；".join(...)``, f-strings);
    the one addition is letting the two callers below classify it via
    ``isinstance`` instead of pattern-matching message text (判据必须是结构性
    的，不是错误文案名单 -- only the handful of append_error(...,
    schema_violation=True) call sites below ever construct one; every other
    call site keeps appending a plain ``str`` and stays in the semantic/
    fail-closed family).
    """

    __slots__ = ()


def _current_identity_is_schema_violation(error: str) -> bool:
    return isinstance(error, _CurrentIdentitySchemaViolation)

