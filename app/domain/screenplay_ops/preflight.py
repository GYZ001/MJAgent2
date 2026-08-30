"""剧本生成前置检查（预算/卡司投影汇总）及其路由包装。

从 app/domain/screenplay_ops.py 按原样搬移；依赖 activation 与 status_snapshot，因此排在 activation 之后。
"""
from __future__ import annotations

import json
import math

from app.db import get_conn
from app.domain.common import (
    _episode_or_404,
    _episode_source_text,
    router,
)
from app.narrative_blueprint import BLUEPRINT_TARGET_SOURCE_SEGMENTS_PER_SHARD

from .activation import _screenplay_blueprint_budget_projection
from .status_snapshot import _screenplay_cast_impact


def _screenplay_generation_preflight(episode_id: str):
    """首次生成的纯读预检；只报告输入范围和人物资产影响。"""
    ep = dict(_episode_or_404(episode_id))
    conn = get_conn()
    source_text = _episode_source_text(conn, ep)
    chapters = json.loads(ep["source_chapters"] or "[]")
    cast_impact = _screenplay_cast_impact(conn, ep, source_text)
    from app.source_excerpt import index_source_segments

    source_segment_count = len(index_source_segments(source_text))
    estimated_blueprint_shards = max(
        1,
        math.ceil(
            source_segment_count
            / BLUEPRINT_TARGET_SOURCE_SEGMENTS_PER_SHARD
        ),
    )
    estimated_scene_shards = max(1, math.ceil(source_segment_count / 16))
    reusable_rows = conn.execute(
        """SELECT id,type,status,contract_version,content_json FROM artifacts
             WHERE scope_type='episode' AND scope_id=? AND status='validated'
               AND type IN (
                 'screenplay_identity_discovery','screenplay_narrative_blueprint',
                 'screenplay_identity_registry','screenplay_envelope',
                 'screenplay_scene_shard','screenplay_generation_ir_merged'
               )""",
        (episode_id,),
    ).fetchall()
    from app.production.revision import (
        get_active_production_revision,
        get_production_revision,
        resolve_screenplay_resume_eligibility,
    )

    revision_id = str(ep.get("screenplay_production_revision_id") or "")
    revision = (
        get_production_revision(revision_id)
        if revision_id
        else get_active_production_revision(episode_id, "screenplay")
    )
    eligibility = resolve_screenplay_resume_eligibility(
        episode_id,
        revision=revision,
        conn=conn,
    )
    reusable_shard_ids = {
        str(item.get("normalized_artifact_id") or "")
        for item in eligibility.reusable_checkpoint.get("shards") or []
        if isinstance(item, dict)
    }
    reusable_counts: dict[str, int] = {}
    for reusable_row in reusable_rows:
        row = dict(reusable_row)
        if (
            row["type"] == "screenplay_scene_shard"
            and str(row["id"]) not in reusable_shard_ids
        ):
            continue
        artifact_type = str(row["type"])
        reusable_counts[artifact_type] = (
            reusable_counts.get(artifact_type, 0) + 1
        )
    budget_projection = _screenplay_blueprint_budget_projection(episode_id)
    return {
        "action": "generate_screenplay",
        "episode_id": episode_id,
        "input": {
            "source_chapters": chapters,
            "source_chars": len(source_text),
            "source_segment_count": source_segment_count,
            "estimated_blueprint_shards": estimated_blueprint_shards,
            "estimated_scene_writing_shards": estimated_scene_shards,
        },
        "wait_estimate": None,
        "cost_estimate_cny": None,
        "cast_impact": cast_impact,
        "reusable_validated_artifacts": reusable_counts,
        "blueprint_budget": {
            key: budget_projection[key]
            for key in (
                "requires_fresh_retry_grant",
                "unknown_receipts",
                "provider_calls",
                "charged_output_tokens",
                "unknown_output_tokens",
                "token_admissible",
                "call_admissible",
                "admissible_after_approval",
            )
        },
        "idempotency_scope": {
            "baseline": ep.get("screenplay_artifact_id") or "empty",
            "constraint_version": int(ep.get("screenplay_constraint_version") or 0),
        },
    }

@router.post("/episodes/{episode_id}/screenplay/preflight")
def screenplay_generation_preflight(episode_id: str):
    """返回首次生成的只读输入预检，不创建任务。"""
    return _screenplay_generation_preflight(episode_id)
