"""叙事蓝图——语义双审裁决编排入口 ``_semantic_review_narrative_blueprint``。

原来是一条 958 代码行的单函数（缓存复用 -> 逐轮双审 -> 共识计算 -> 修复 ->
下一轮，四个阶段全部内联在同一个循环体里，外加大量跨轮次共享的可变局部状态）。
按真实阶段边界拆成兄弟模块，每个模块只负责一个阶段，本文件只做编排：

* ``blueprint_semantic_review_cache.py`` —— 审稿会话指纹、缓存复用、权威产物
  持久化。
* ``blueprint_semantic_review_projection.py`` —— 定向复审的风险节点投影。
* ``blueprint_semantic_review_prompt.py`` —— 独立审稿人 Prompt 文案。
* ``blueprint_semantic_review_reviewer.py`` —— 单轮输入构造 + 单份审稿人调用。
* ``blueprint_semantic_review_round.py`` —— 收集一轮的两份审稿样本（含未送达
  补采）。
* ``blueprint_semantic_review_consensus.py`` —— 双份审稿共识计算与共识产物
  落盘。
* ``blueprint_semantic_review_repair.py`` —— 按权威问题修复蓝图。

拆分时抓到的闭包/绑定陷阱：

* ``review_projection()``/``run_reviewer()`` 等原来是内联闭包，隐式捕获
  ``blueprint``/``source_text``/``targeted_review``/``review_round`` 等外层
  变量；提升为顶层函数后一律改成显式参数（``_BlueprintReviewRoundInputs`` 打包
  只读的单轮快照），不再依赖闭包作用域。
* ``reviews``/``review_artifact_ids``/``dropped_voice_issue_counts`` 是原来
  闭包共享的可变容器（``list.append``/``dict[k]=v``）——拆分后依旧原地 mutate，
  只是从「闭包捕获」改成「显式参数传入的同一个对象」，语义不变、没有孤儿副本
  风险。
* ``targeted_review`` 是跨轮次重新绑定的布尔量（``targeted_review = False``），
  不是原地 mutate——拆到 ``_repair_blueprint_from_review`` 后必须显式作为返回值
  之一回传给编排循环，不能指望调用方通过闭包看到内部重新绑定。本文件的
  ``for review_round in range(1, 5):`` 循环体每一步都显式重新赋值
  ``blueprint, targeted_review = ...``，不依赖任何跨函数的隐式共享。
"""
from __future__ import annotations

from typing import Any

from app.db import get_setting
from app.errors import ContentGenerationError
from app.narrative_blueprint import (  # noqa: F401 -- re-exported, see below
    BlueprintSemanticReview,
    NarrativeBlueprint,
    blueprint_semantic_issue_is_resolved,
    blueprint_semantic_review_schema,
    blueprint_semantic_voice_issue_has_dialogue_authority,
    filter_blueprint_semantic_review_voice_issues,
    normalize_blueprint_semantic_review_payload,
    validate_blueprint_semantic_review,
)

from .blueprint_budget import _BlueprintGenerationBudget
from .blueprint_semantic_review_cache import (
    _blueprint_semantic_review_fingerprints,
    _persist_reviewed_blueprint_authority,
    _reuse_cached_blueprint_semantic_review,
)
from .blueprint_semantic_review_consensus import (
    _blueprint_semantic_review_consensus,
    _create_blueprint_review_consensus_artifact,
    _create_insufficient_blueprint_review_artifact,
)
from .blueprint_semantic_review_repair import _repair_blueprint_from_review
from .blueprint_semantic_review_inputs import _blueprint_semantic_review_round_inputs
from .blueprint_semantic_review_round import _collect_blueprint_review_samples

# 以下全部是「供其他模块（以及 app/stages/__init__.py 的既有再导出块）沿用旧
# 导入路径」的兼容 shim，本文件自身不直接使用：
# - BlueprintSemanticReview / blueprint_semantic_issue_is_resolved / ... 等
#   原本是 app.narrative_blueprint 的名字，经由本文件顶层导入被 app/stages/
#   __init__.py 透传导出（上面 import 块已带出，标了 noqa: F401）；
# - 下面两条 _blueprint_semantic_issue_* 判据函数与
#   _blueprint_review_sample_is_undelivered 原本定义在本文件，现在实现搬到了
#   consensus.py / reviewer.py，这里保留同名再导出，避免動 app/stages/
#   __init__.py 的既有导入语句。
from .blueprint_semantic_review_consensus import (  # noqa: F401
    _blueprint_semantic_issue_exact_scope,
    _blueprint_semantic_issue_has_deterministic_authority,
)
from .blueprint_semantic_review_reviewer import (  # noqa: F401
    _blueprint_review_sample_is_undelivered,
)


async def _semantic_review_narrative_blueprint(
    blueprint: NarrativeBlueprint,
    *,
    episode: dict[str, Any],
    source_text: str,
    generation_budget: _BlueprintGenerationBudget | None = None,
) -> NarrativeBlueprint:
    initial_blueprint_hash, review_source_corpus_hash, review_input_fingerprint = (
        _blueprint_semantic_review_fingerprints(blueprint, episode, source_text)
    )

    reused_artifact_id = _reuse_cached_blueprint_semantic_review(
        episode,
        initial_blueprint_hash=initial_blueprint_hash,
        review_source_corpus_hash=review_source_corpus_hash,
        review_input_fingerprint=review_input_fingerprint,
    )
    if reused_artifact_id is not None:
        _persist_reviewed_blueprint_authority(
            blueprint,
            episode=episode,
            source_text=source_text,
            generation_budget=generation_budget,
            parent_artifact_ids=[reused_artifact_id],
        )
        return blueprint

    targeted_review = str(
        get_setting("screenplay_targeted_blueprint_review_enabled") or "true"
    ).strip().lower() not in {"0", "false", "off", "no"}

    for review_round in range(1, 5):
        round_inputs = _blueprint_semantic_review_round_inputs(
            blueprint, source_text, review_round, targeted_review,
        )
        reviews, review_artifact_ids, dropped_voice_issue_counts = (
            await _collect_blueprint_review_samples(
                round_inputs,
                blueprint=blueprint,
                source_text=source_text,
                episode=episode,
                generation_budget=generation_budget,
            )
        )
        if len(reviews) < 2:
            _create_insufficient_blueprint_review_artifact(
                round_inputs,
                episode=episode,
                reviews=reviews,
                review_artifact_ids=review_artifact_ids,
                dropped_voice_issue_counts=dropped_voice_issue_counts,
                review_source_corpus_hash=review_source_corpus_hash,
                review_input_fingerprint=review_input_fingerprint,
            )
            raise ContentGenerationError(
                "蓝图语义审稿人不足两份，已停止而非静默视为无问题"
            )

        consensus = _blueprint_semantic_review_consensus(
            reviews,
            blueprint=blueprint,
            source_text=source_text,
            targeted_review=round_inputs.targeted_review,
        )
        consensus_artifact = _create_blueprint_review_consensus_artifact(
            round_inputs,
            consensus,
            episode=episode,
            review_artifact_ids=review_artifact_ids,
            dropped_voice_issue_counts=dropped_voice_issue_counts,
            review_source_corpus_hash=review_source_corpus_hash,
            review_input_fingerprint=review_input_fingerprint,
        )

        if consensus.needs_full_fallback:
            # A targeted one-sided result cannot establish clean authority.
            # The next bounded round switches to the complete Blueprint; no
            # patch is attempted from non-consensus findings.
            if review_round >= 4:
                raise ContentGenerationError(
                    "蓝图定向语义复审仍有单侧必须修复问题，已按非 clean 停止"
                )
            targeted_review = False
            continue
        if consensus.reviews_are_clean:
            _persist_reviewed_blueprint_authority(
                blueprint,
                episode=episode,
                source_text=source_text,
                generation_budget=generation_budget,
                parent_artifact_ids=[str(consensus_artifact["id"])],
            )
            return blueprint
        if consensus.full_review_has_non_authoritative_residual:
            _persist_reviewed_blueprint_authority(
                blueprint,
                episode=episode,
                source_text=source_text,
                generation_budget=generation_budget,
                parent_artifact_ids=[str(consensus_artifact["id"])],
            )
            return blueprint
        if not consensus.authoritative_issues:
            raise ContentGenerationError(
                "蓝图双审存在未解决问题，但没有可安全修复的权威问题"
            )
        if review_round >= 4:
            gate_label = (
                "语义共识"
                if consensus.consensus_issues
                else "确定性权威"
            )
            raise ContentGenerationError(
                f"蓝图{gate_label}复审仍有必须修复问题："
                + "；".join(
                    issue.message for issue in consensus.authoritative_issues[:10]
                )
            )

        blueprint, targeted_review = await _repair_blueprint_from_review(
            blueprint,
            consensus,
            episode=episode,
            source_text=source_text,
            generation_budget=generation_budget,
            review_artifact_ids=review_artifact_ids,
            targeted_review=targeted_review,
        )
    return blueprint
