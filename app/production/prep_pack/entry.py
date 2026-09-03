"""run_episode_prep_pack: the Run/Step-harness entry point that drives one or
more generation attempts through to a published prep pack.

Split out of app/production/prep_pack.py.
"""
from __future__ import annotations

import json
from app.db import get_conn
from app.harness.contracts import get_contract
from typing import Any

from .contracts import PrepPackGateError
from .generate_once import _generate_prep_pack_once
from .publish import _publish_prep_pack
from .timeline_segments import attach_episode_timeline


def _reconcile_degraded_retry(
    exc: PrepPackGateError, prior_had_events: bool, prior_reason: str,
) -> tuple[PrepPackGateError, bool, str]:
    """退化重试护栏（ERR-20260824-7ab7cb）：本次重试事件链归零，但此前一次
    尝试确实抽到过事件时，拒绝让这次退化静默覆盖——把两次的失败原因合并成
    一条具名错误；否则原样记录本次结果，供下一轮判断沿用。见
    ``run_episode_prep_pack`` docstring 的完整案情。"""
    had_events = bool(getattr(exc, "had_events", True))
    if not had_events and prior_had_events:
        exc = PrepPackGateError(
            "本次重试事件链退化为空，拒绝采纳该退化结果（此前一次"
            f"尝试已抽到事件，但因以下原因被拒：{prior_reason}）；"
            f"本次重试事件抽取本身失败：{exc}",
            had_events=False,
        )
    if had_events:
        prior_had_events = True
        prior_reason = str(exc)[:500]
    return exc, prior_had_events, prior_reason


async def run_episode_prep_pack(
    *,
    episode_id: str,
    episode: dict[str, Any],
    source_text: str,
    run_id: str | None,
) -> dict[str, Any]:
    """Generate + atomically publish one episode's episode_prep_pack.

    Bounded retry (contract.max_iterations, currently 2): each attempt
    regenerates the whole pack from scratch -- there is no partial-checkpoint
    repair loop (that heavier design was explicitly retired, see
    docs/TRANSFORM_FREEZE_PLAN.md §3/§6). If the last attempt still fails a
    hard gate, the run fails with the gate's error message.

    退化重试护栏（第23轮真实回归 ERR-20260824-7ab7cb，2.0.0 起 had_events
    语义改为"这次尝试有没有发现任何人物/场景/道具提及"，机制本身不变）：
    真实事故形态——尝试1 拿到一批真实提及，一路通过资产映射，只在后面
    某道门禁被拒；尝试2 重新抽取时模型这次的原始 JSON 本身在中途缺了一段
    结构（截断/自愈失败），修复重试拿到的候选又被误判，模型据此"忠实"地
    把提及列表修回空——整批提及退化为零。旧逻辑里 attempt_hint/last_error
    每轮无条件覆盖，尝试2 的"本集未发现任何素材"就这样悄悄盖掉了尝试1
    更有信息量的失败原因。护栏：一旦本运行内任何一次尝试发现过素材
    （PrepPackGateError.had_events=True），后续任何退化为零（had_events=
    False）的尝试都不得被当成普通失败静默采纳——必须把两次的失败原因合并
    成一条具名错误，明说"这是一次退化重试，不是从未发现过素材"。只有本
    运行内全部尝试都是零素材，才维持原始的终态报错。
    """
    contract = get_contract("screenplay")
    project_id = str(episode["project_id"])
    episode_no = int(episode["episode_no"])
    try:
        raw_chapters = episode.get("source_chapters") or []
        chapter_indexes = (
            json.loads(raw_chapters or "[]")
            if isinstance(raw_chapters, str)
            else list(raw_chapters)
        )
    except (TypeError, ValueError):
        chapter_indexes = []
    chapter_indexes = [int(idx) for idx in chapter_indexes]

    attempt_hint = ""
    last_error: Exception | None = None
    prior_attempt_had_events = False
    prior_attempt_reason = ""
    for attempt in range(1, max(1, contract.max_iterations) + 1):
        try:
            (
                payload, rejected_paratext_claims, true_name_hints,
                scene_alias_anchors, rejected_alias_conflicts,
                character_manifest_anomaly,
            ) = await _generate_prep_pack_once(
                episode_id=episode_id,
                episode_no=episode_no,
                project_id=project_id,
                chapter_indexes=chapter_indexes,
                source_text=source_text,
                run_id=run_id,
                attempt_hint=attempt_hint,
            )
            payload = await attach_episode_timeline(
                payload, project_id=project_id, chapter_indexes=chapter_indexes,
                source_text=source_text, conn=get_conn(),
            )
            _publish_prep_pack(
                episode_id=episode_id, payload=payload, run_id=run_id,
                rejected_paratext_claims=rejected_paratext_claims,
                true_name_hints=true_name_hints,
                scene_alias_anchors=scene_alias_anchors,
                rejected_alias_conflicts=rejected_alias_conflicts,
                character_manifest_anomaly=character_manifest_anomaly,
            )
            return payload
        except PrepPackGateError as exc:
            exc, prior_attempt_had_events, prior_attempt_reason = _reconcile_degraded_retry(
                exc, prior_attempt_had_events, prior_attempt_reason,
            )
            last_error = exc
            attempt_hint = str(exc)[:2000]
            continue
    raise last_error if last_error is not None else RuntimeError("分集映射包生成失败")
