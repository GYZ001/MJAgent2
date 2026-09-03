"""v6 当前身份契约的确定性降级决策——WS5：不重试、不回喂模型，只在特定、
有据可查的形状下把「判死整集」改写成「按规则做出可审计的决定，继续」。

**为什么不是"重试"**：`app/harness/model_gateway.py` 里
``IDENTITY_UNUSABLE_RESPONSE_RESAMPLES`` 上方大段注释与
``app/portraits/identity_investigation.py`` 模块 docstring 已经明确、且有
6+ 个 ``_fails_once``/``StructuredSemanticError`` 测试实测：当前身份契约的
语义违规必须一次性失败，不得把模型自己的错误答案回显给它换取"改对"——那是
在教它伪造合规。``discovery_legacy.py`` 里 ``run_phase_b`` 对
``semantic_retry_limit=0`` 是刻意的。因此本模块不做回喂重试，只做与
``identity_literal_evidence.py``（0 命中丢弃、多命中改绑首条）同一类的
确定性代码侧降级：判据完全来自本批已产出的结构化数据与证据目录本身，
不第二次询问模型。

**为什么只落地了一条规则**：WS5 派单原本列了三条候选降级——P token 复用
缺失、K 决议 absorbed_functional_keys 部分越界、source_label 重复——逐条对照
``tests/test_character_discovery.py`` 的既有回归后，前两条被明确的红灯测试
锁定为"必须继续硬失败"，不实现：

- P token 复用缺失：``test_current_identity_cross_batch_same_label_new_group_
  fails_once`` 断言同一 source_label 在下一批不显式给 P token 时必须
  ``StructuredSemanticError``——按标签字面自动认领 prior group 会让"model
  确实是在开一个新人物、只是称谓撞车"和"model 想复用前一个人"两种情况无法
  区分，P token 是模型唯一的显式确认信号，不能被字面匹配绕过。
- absorbed_functional_keys 越界：``test_current_identity_absorbed_functional_
  keys_rejects_forged_token`` / ``_rejects_own_source_label_ep5_regression`` /
  ``_rejects_named_characters_other_appellations_ep1_regression`` 三个真实
  RCA 回归都明确写着"必须继续锁定硬失败，不能锁定成通过"——这道校验挡的是
  模型编造/误用 token 伪造合并，不是可以从数据本身消歧的歧义。

**第三条（source_label 重复）落地在下面 ``merge_declared_functional_repeat_
if_eligible``**，且比最初设想收窄了一道关键门槛。生产两个真实样本（橘座在上
ep6「陶总」/ep10「黄总」，B 上 provider_calls id=27179/27181）取证发现：两者
都不是 F 分支模型显式声明的同一 functional_identity_key，而是 N 分支敬称
（honorific）被降级为 functional 时，`_identity_form_functional_key` 对标签
文本取的纯哈希——且这个称谓在本批整份请求文本（含证据目录）里逐字出现次数
为 0（实测确认，见 identity 测试 fixture）。与之相对，
``tests/test_character_discovery.py::
test_declared_repeat_label_with_ambiguous_literal_home_still_hard_fails``
是刻意保留的对照红灯：申报字段同样完全一致，但称谓在本批别处逐字出现了两次
（只是都没被引用到），必须维持致命——这种情况下称谓不是"批里根本不存在"，
是模型选错了引用位置，存在真正的改绑歧义，交给下面
``test_current_identity_declared_conflict_stays_fatal_with_side_by_side_diff``
同一挂钩不动。因此这里的合并条件是「申报签名完全一致 **且** 该称谓在本批
证据目录里逐字出现次数为 0」——后一半排除了"称谓真实存在但引用位置有歧义"
这一类，只处理"称谓从未出现过、无论怎么选证据都不会有逐字锚点"这一类。
"""
from __future__ import annotations

import logging
from typing import Any

from app.observability.metrics import inc

from .evidence_merge import (
    _current_identity_declared_signature,
    _merge_current_identity_occurrences,
)

log = logging.getLogger(__name__)

# WS14：只观测，不改判定。规则→类别是结构前缀表，不是正则猜中文——每条前缀
# 逐字取自产生该错误的那个 errors.append(...) 调用处的字面量段（见
# identity_response_projection.py / identity_literal_evidence.py
# named_literal_miss_verdict），动态内容（source_label/decision_id 等）严格
# 跟在前缀之后，未命中已知前缀一律落 other，不猜测。
IDENTITY_CONTRACT_HARD_FAIL_METRIC = "identity_contract_hard_fail_total"
_CURRENT_IDENTITY_HARD_FAIL_RULE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("current 后续batch的同称谓必须用P token显式复用 prior group：", "p_token_reuse_missing"),
    ("current K decision absorbed_functional_keys 越界：", "absorbed_keys_out_of_bounds"),
    ("current named 缺少逐字 owned evidence：", "owned_evidence_missing"),
    ("source_label 重复：", "source_label_duplicate"),
    ("current 同一 source_label 对应多个 identity_group：", "authority_multi_group"),
)


def _current_identity_hard_fail_rule(error: str) -> str:
    text = str(error or "")
    for prefix, rule in _CURRENT_IDENTITY_HARD_FAIL_RULE_PREFIXES:
        if text.startswith(prefix):
            return rule
    return "other"


def record_current_identity_hard_fail_metrics(
    errors: list[str],
    *,
    project_id: str | None,
    episode_no: int,
    batch_index: int,
) -> None:
    """v6 errors 按规则类别计数，仅观测；inc() 失败静默，不产生新的失败点。"""
    for error in errors:
        inc(
            IDENTITY_CONTRACT_HARD_FAIL_METRIC,
            rule=_current_identity_hard_fail_rule(error),
            project_id=project_id or "",
            episode_no=episode_no,
            batch_index=batch_index,
        )


def _label_has_literal_occurrence(source_label: str, evidence_by_ref: dict[str, Any]) -> bool:
    return any(source_label in str(record.get("text") or "") for record in evidence_by_ref.values())


def merge_declared_functional_repeat_if_eligible(
    options: list[dict], evidence_by_ref: dict[str, Any],
) -> dict | None:
    """source_label 重复且常规归一（``_current_identity_reconcile_as_single``）
    判不出来时的确定性降级：这组候选全部是 functional、申报签名
    （``_current_identity_declared_signature``，逐字段包含 kind/
    functional_identity_key 等）完全一致、且共享同一个非空 functional_
    identity_key（无论是模型真正的 F 分支声明，还是 N 分支敬称降级产生的
    纯标签哈希——rule 6 的"重复即同一人"信号对两者同样成立），同时这个
    source_label 在本批整份证据目录里逐字出现次数为 0——不是"存在但引用错
    位置"的可歧义形状，才按模型申报合并；否则返回 ``None``，调用方维持原有
    硬失败不变（申报签名不一致，或称谓在别处确有逐字出处存在改绑歧义）。
    """
    if len(options) < 2:
        return None
    if not all(item.get("identity_kind") == "functional" for item in options):
        return None
    keys = {str(item.get("_current_response_group_key") or "").strip() for item in options}
    if len(keys) != 1 or not next(iter(keys)):
        return None
    if len({_current_identity_declared_signature(item) for item in options}) != 1:
        return None
    label = str(options[0].get("source_label") or "")
    if _label_has_literal_occurrence(label, evidence_by_ref):
        return None
    merged = _merge_current_identity_occurrences(options)
    merged["_current_identity_normalized_duplicate"] = True
    merged["_current_identity_degrade_note"] = (
        f"称谓「{label}」本批 {len(options)} 条记录申报字段完全一致（含同一个 "
        "functional_identity_key，模型自己声明的同一人信号），且该称谓在本批"
        "证据目录里从未逐字出现过（不是可改绑的引用歧义）——已按模型声明合并为"
        "一条 functional 身份；如这些实际是不同的人，请在下一批为每一条各自"
        "指定不同的 functional_identity_key，或填写能互相区分的 scope_qualifier。"
    )
    log.warning(
        "current functional 降级：申报签名一致且称谓全批无逐字出处，已合并 source_label=%s（%d 条）",
        label, len(options),
    )
    return merged
