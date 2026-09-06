"""模型规划漏掉的镜头按确定性默认补齐，不让整集视频计划失效（2026-09-05 第 4 轮第 14 集）。

第 14 集 18 镜，模型计划漏报了第 15/16 镜，校验 SHOT_COVERAGE_INCOMPLETE，补齐运行在
ensure_video_plan 阶段整集收口。上一段取帧已放弃后，每镜的执行契约就是参考图模式、无链依赖
——与 ``generate_episode_plan(deterministic_only=True)`` 生成的完全一样，模型只是在此之上
标注 relations/state_dependency 等注释性字段。漏掉的镜头按确定性默认补上，reason_codes 记
``AI_PLAN_OMITTED_SHOT_FILLED`` 让缺失可见（账本另记一条 NORMALIZED），不再整集失败。
"""
from __future__ import annotations

from typing import Any

from app.db import log_provider_call, new_id

from .models import ShotVideoGenerationPlan, VideoGenerationMode

OMITTED_SHOT_REASON = "AI_PLAN_OMITTED_SHOT_FILLED"


def fill_omitted_shots(
    shot_plans: list[ShotVideoGenerationPlan], rows: list[Any], shot_payload: list[dict[str, Any]], *,
    plan_id: str, plan_revision: int, revision_id: str, snapshot_id: str,
    asset_fingerprints: dict[str, str], episode_id: str, model: str,
) -> list[ShotVideoGenerationPlan]:
    """返回补齐后按 shot_no 排序的计划项；模型没漏时原样返回同一列表。"""
    db_id_by_identifier: dict[str, str] = {}
    for payload, row in zip(shot_payload, rows):
        for identifier in (payload.get("shot_id"), payload.get("database_shot_id"), str(row["id"])):
            if identifier:
                db_id_by_identifier[str(identifier)] = str(row["id"])
    covered = {db_id_by_identifier.get(item.shot_id, item.shot_id) for item in shot_plans}
    filled: list[ShotVideoGenerationPlan] = []
    for index, (row, payload) in enumerate(zip(rows, shot_payload)):
        if str(row["id"]) in covered:
            continue
        filled.append(ShotVideoGenerationPlan(
            shot_plan_id=new_id("svp"), episode_video_plan_id=plan_id, plan_revision=plan_revision,
            source_storyboard_revision_id=revision_id, shot_id=str(row["id"]),
            published_shot_id=str(payload.get("shot_id") or row["id"]), shot_no=int(payload.get("shot_no") or index + 1),
            mode=VideoGenerationMode.REFERENCE_IMAGE_MODE, reason_codes=[OMITTED_SHOT_REASON], confidence=1.0,
            estimated_latency_ms=690_000, capability_snapshot_id=snapshot_id,
            input_revision_fingerprints={"asset_revisions": asset_fingerprints.get(str(row["id"]), "")},
        ))
    if not filled:
        return shot_plans
    log_provider_call(
        "episode_video_mode_plan_normalization", model, "NORMALIZED", None, 0,
        meta={"episode_id": episode_id, "plan_revision": plan_revision,
              "changes": [{"code": OMITTED_SHOT_REASON, "shot_nos": [item.shot_no for item in filled]}]},
    )
    return sorted([*shot_plans, *filled], key=lambda item: int(item.shot_no))
