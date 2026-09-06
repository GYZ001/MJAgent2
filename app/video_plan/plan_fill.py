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
from .release_manifest import shot_id_aliases

OMITTED_SHOT_REASON = "AI_PLAN_OMITTED_SHOT_FILLED"
PHANTOM_SHOT_REASON = "AI_PLAN_PHANTOM_SHOT_DROPPED"


def resolve_plan_items(
    shot_plans: list[ShotVideoGenerationPlan], rows: list[Any], shot_payload: list[dict[str, Any]],
) -> tuple[list[tuple[ShotVideoGenerationPlan, str | None]], list[ShotVideoGenerationPlan]]:
    """与校验同一套解析（库 id / shot_uid / 已发布 id 精确命中，否则按 shot_no 兜底）把每条计划落到
    库里的镜；返回 (保留条目及其解析结果, 丢弃的幻影条目)。

    幻影：id 谁都对不上、而它按 shot_no 兜底落到的那一镜已经有精确命中的条目——模型多编了一条
    （第 14 轮第 24 集：18 镜的窗口回了 19 条，凭空多出 shot_af6d8f9ca1f4，按位置占了第 18 号，
    真第 18 镜被挤成第 19 号，校验判 DUPLICATE_SHOT_PLAN）。id 写错但序号落到没人认领的镜
    （第 20 集 shot_38e5d84cf03c → …0307）仍按序号保留。同一镜两条精确命中只留第一条。"""
    aliases, by_shot_no = shot_id_aliases(rows)
    for index, (payload, row) in enumerate(zip(shot_payload, rows)):
        for identifier in (payload.get("shot_id"), payload.get("database_shot_id")):
            if identifier:
                aliases.setdefault(str(identifier), str(row["id"]))
        by_shot_no.setdefault(int(payload.get("shot_no") or index + 1), str(row["id"]))  # 已发布序号与库 shot_no 同源
    exact = {aliases.get(item.shot_id) or aliases.get(item.published_shot_id) for item in shot_plans} - {None}
    kept: list[tuple[ShotVideoGenerationPlan, str | None]] = []
    dropped: list[ShotVideoGenerationPlan] = []
    claimed: set[str] = set()
    for item in shot_plans:
        resolved = aliases.get(item.shot_id) or aliases.get(item.published_shot_id)
        if resolved is None:
            fallback = by_shot_no.get(int(item.shot_no or 0))
            if fallback in exact:
                dropped.append(item)
                continue
            resolved = fallback
        if resolved and resolved in claimed:
            dropped.append(item)
            continue
        if resolved:
            claimed.add(resolved)
        kept.append((item, resolved))
    return kept, dropped


def fill_omitted_shots(
    shot_plans: list[ShotVideoGenerationPlan], rows: list[Any], shot_payload: list[dict[str, Any]], *,
    plan_id: str, plan_revision: int, revision_id: str, snapshot_id: str,
    asset_fingerprints: dict[str, str], episode_id: str, model: str,
) -> list[ShotVideoGenerationPlan]:
    """返回去掉幻影条目、补齐漏报后按 shot_no 排序的计划项；模型没漏也没多时原样返回同一列表。"""
    kept, dropped = resolve_plan_items(shot_plans, rows, shot_payload)
    covered = {resolved for _item, resolved in kept if resolved}  # 与校验同一套解析算覆盖，补齐不会再撞出 DUPLICATE_SHOT_PLAN
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
    if not filled and not dropped:
        return shot_plans
    changes = [{"code": OMITTED_SHOT_REASON, "shot_nos": [item.shot_no for item in filled]}] if filled else []
    if dropped:
        changes.append({"code": PHANTOM_SHOT_REASON, "shot_ids": [item.shot_id for item in dropped], "shot_nos": [item.shot_no for item in dropped]})
    log_provider_call(
        "episode_video_mode_plan_normalization", model, "NORMALIZED", None, 0,
        meta={"episode_id": episode_id, "plan_revision": plan_revision, "changes": changes},
    )
    return sorted([*(item for item, _resolved in kept), *filled], key=lambda item: int(item.shot_no))
