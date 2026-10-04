"""分镜阶段补卡的抢占、建卡与生成入口闸门——与场景状态图
（``app.video_modes.scene_state_ensure``）同一套并发/事务取舍，详见该模块
docstring；本文件只记与它不同的地方。

与场景状态图的关键差异：道具卡一旦建成是永久性的（外观锚定不会因为别的段
进度而改变），没有"指纹是否仍匹配当前期望"这层概念，所以状态机只有
queued/running/ready/failed 四态，没有 fingerprint/stale。失败有重试上限
（``MAX_BUILD_ATTEMPTS``，CLAUDE.md「额度」纪律），超限后不再阻塞生成——退回
本功能上线前"该 label 没有卡"的既有降级行为，不会永久卡住视频生成命令。

**状态写入必须用独立连接**：``claim_or_get``/``_mark_ready``/``_mark_failed``
走 ``app.db.run_write_transaction``，不在调用方 ``get_conn()`` 的连接上
``commit()``（CLAUDE.md「不得在调用方的连接上隐式提交」）。

补卡本身只新增道具卡/别名，不改写任何镜头的 ``shot_contract_json``、不删除
任何既有 ``prop_references`` 行：已采纳视频的交付校验
（``app.downstream_authority``）只核验视频文件/Artifact/技术门禁，不重新解析
参考图清单，补卡不影响已交付视频。

**在途任务的漂移判定——范围有限，不是全局保证**：``pending_prop_card_gate``
只确保"本次调用检查的这批 label，在这次要派发的任务入队前已经补完"，这对
触发闸门的那一次派发是成立的。但道具卡是按 ``(project_id, label)`` 项目级
共享（见 ``app.props.card_pending_store`` 模块 docstring），同一 label 可能
被另一集/另一镜的派发先冻结了"无卡"的 manifest 再入队、随后才被这次补卡
后台任务建成。那个更早入队、仍在队列里或正在被 worker 处理的任务，会在它
自己的 write_point 复核（``app.media_exec.authority._review_shot_manifest_
equal``）里发现该 label 的 ``ready``/``prop_revision_id`` 从冻结时的值变了，
被判定为 ``REVIEW_DEPENDENCY_STALE``——这不是本功能引入的新漏洞，而是
``app.video_modes.prop_references.manifest_props_signature``2026-10-01 就
刻意设计的信号（任何道具从"无图"变"有图"都算这个镜头的依赖漂移，需要用新
图重新生成，不能让它继续用旧的"无图"假设跑完）；判定结果是任务状态转
``failed`` 并带可读错误（``app.media_exec.run_job_errors._handle_review_
dependency_fence``），不是卡死不动，用户可见后重新点生成即可，与本功能上
线前"道具卡被人工编辑/重新登记导致同类漂移"的既有行为一致。
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from app import db
from app.db import get_conn, new_id, now
from app.evidence.txn_guard import rollback_uncommitted_on_error
from app.props.card_match import match_existing_prop_card
from app.props.card_pending_scan import (
    candidate_labels_without_card,
    label_shot_occurrences,
    load_episode_shot_rows,
)
from app.props.card_pending_store import (
    ensure_tables_on_connection,
    get_pending,
    insert_running,
    update_failed,
    update_ready,
    update_running,
)
from app.props.service import bind_existing_prop_alias, register_prop_card_for_label

log = logging.getLogger(__name__)

_RUNNING_STALE_S = 600.0  # 10 分钟：单张图远不需要这么久，超时视为僵死可重新抢占
MAX_BUILD_ATTEMPTS = 3
_MAX_CONCURRENT_BUILDS = 3
_SENTENCE_SPLIT_RE = re.compile(r"[。！？\n]")
_BACKGROUND_ENSURE_TASKS: set[asyncio.Task[None]] = set()


def _is_blocking(row: dict[str, Any] | None) -> bool:
    """这个 label 本轮还要不要拦住生成：``ready`` 不拦；``running``（未超时）
    说明已有人在建，拦着等；``failed`` 只在重试额度未用尽时才继续拦，用尽后
    放行（退回"无卡"的既有降级）；没有记录（从未尝试过）要拦。"""
    if row is None:
        return True
    status = row["status"]
    if status == "ready":
        return False
    if status == "running":
        return True  # 未超时=有人在建；僵死=下一次 ensure 会重新抢占，两种都先拦
    if status == "failed":
        return int(row["attempts"] or 0) < MAX_BUILD_ATTEMPTS
    return True


def _needs_new_launch(row: dict[str, Any] | None) -> bool:
    """这个 label 要不要再起一个新的后台任务：``running`` 且未超时说明已有
    任务在处理它，无需再起一个（底层 ``claim_or_get`` 的 CAS 本就会在第二个
    任务里提前返回、不会重复建卡，但每次用户重试点击生成都重新 launch 一整
    个 ``ensure_storyboard_prop_cards``——重新加载 episode/bible、重新扫描整
    集候选——是可避免的浪费）；没有记录/``failed``/僵死 ``running`` 都需要。
    """
    if row is None:
        return True
    if row["status"] != "running":
        return True
    return (now() - float(row["updated_at"] or 0)) >= _RUNNING_STALE_S


def _claim_or_get_operation(conn: Any, *, project_id: str, label: str) -> tuple[dict[str, Any], bool]:
    ensure_tables_on_connection(conn)
    row = get_pending(conn, project_id=project_id, label=label)
    stamp = now()
    if row:
        if row["status"] == "ready":
            return row, False
        if row["status"] == "running" and (stamp - float(row["updated_at"] or 0)) < _RUNNING_STALE_S:
            return row, False
        if row["status"] == "failed" and int(row["attempts"] or 0) >= MAX_BUILD_ATTEMPTS:
            return row, False
        attempts = int(row["attempts"] or 0) + 1
        update_running(conn, row_id=row["id"], attempts=attempts, stamp=stamp)
        row = {**row, "status": "running", "attempts": attempts}
        return row, True
    row_id = new_id("propcard")
    insert_running(conn, row_id=row_id, project_id=project_id, label=label, stamp=stamp)
    return {"id": row_id, "status": "running", "attempts": 1}, True


async def claim_or_get(*, project_id: str, label: str) -> tuple[dict[str, Any], bool]:
    def _operation(conn: Any) -> tuple[dict[str, Any], bool]:
        return _claim_or_get_operation(conn, project_id=project_id, label=label)
    return await db.run_write_transaction(_operation)


async def _mark_ready(*, row_id: str, resolved_name: str) -> None:
    def _operation(conn: Any) -> None:
        ensure_tables_on_connection(conn)
        update_ready(conn, row_id=row_id, resolved_name=resolved_name, stamp=now())
    await db.run_write_transaction(_operation)


async def _mark_failed(*, row_id: str, error: str) -> None:
    def _operation(conn: Any) -> None:
        ensure_tables_on_connection(conn)
        update_failed(conn, row_id=row_id, error=error, stamp=now())
    await db.run_write_transaction(_operation)


def _source_sentence_for_label(source_text: str, label: str) -> str:
    """结构性查找——按句末标点切句，取第一句逐字包含 ``label`` 的原文句子；
    没有原文/没有命中都返回空串，不编造。纯字符串匹配，不是关键词表。"""
    if not source_text or not label:
        return ""
    for sentence in _SENTENCE_SPLIT_RE.split(source_text):
        if label in sentence:
            return sentence.strip()
    return ""


def _combined_evidence(label: str, info: dict[str, Any], source_text: str) -> tuple[str, str]:
    """返回 ``(evidence_text, description_for_model)``：逐段描述去重拼接 +
    原文原句（有则附）。多条互不相同的段描述一起喂给模型，供它在
    ``register_prop_card_for_label``/``assess_prop_appearance`` 里取舍出一个
    一致版本（CLAUDE.md「不得兜底填充」：没有证据支撑的特征不编造）。"""
    descriptions = list(dict.fromkeys(info.get("descriptions") or []))
    sentence = _source_sentence_for_label(source_text, label)
    parts = [f"第{i + 1}条分镜描述：{d}" for i, d in enumerate(descriptions)]
    if sentence:
        parts.append(f"原文：{sentence}")
    text = "\n".join(parts)
    return text, (text or label)


async def _build_one_label(
    *, project_id: str, episode_no: int, label: str, info: dict[str, Any],
    style: str, ep_label: str, source_text: str, cards: list[Any],
    allowed_aliases: frozenset[str],
) -> None:
    row, should_build = await claim_or_get(project_id=project_id, label=label)
    if not should_build:
        return
    evidence_text, description = _combined_evidence(label, info, source_text)
    if len(set(info.get("descriptions") or [])) >= 2:
        log.info(
            "[PROP_STORYBOARD_CARD_DESCRIPTION_CONFLICT] label=%s 段描述=%s",
            label, info.get("descriptions"),
        )
    try:
        conn = get_conn()
        card = match_existing_prop_card(
            label, evidence_text, cards,
            cards_with_prior_evidence=frozenset(),
        )
        if card is not None:
            bind_existing_prop_alias(conn, project_id, card.name, label)
            conn.commit()
            await _mark_ready(row_id=row["id"], resolved_name=card.name)
            return
        result = await register_prop_card_for_label(
            conn, project_id, episode_no, label, description, style=style, ep_label=ep_label,
            allowed_aliases=allowed_aliases,
        )
        resolved_name = (result or {}).get("name") or label
        if result is not None and not result.get("has_image"):
            await _mark_failed(row_id=row["id"], error="出图失败，无参考图")
            return
        await _mark_ready(row_id=row["id"], resolved_name=resolved_name)
    except Exception as exc:  # noqa: BLE001 单个道具建卡失败不打断其它道具/不向上抛出
        await _mark_failed(row_id=row["id"], error=str(exc))


def _load_bible_for_ensure(conn: Any, project_id: str) -> Any | None:
    from app.schemas import Bible  # 函数内导入：同包其它模块（scene_state_ensure）同款写法，避免模块初始化期额外耦合面

    row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row or not (row["bible_json"] or "").strip():
        return None
    return Bible.model_validate_json(row["bible_json"])


def _episode_text_and_no(conn: Any, episode_id: str) -> tuple[dict[str, Any], int, str] | None:
    """返回 ``(episode_row, episode_no, source_text)``；分集不存在返回 None。"""
    from app.source_chapters import _episode_source_text  # 函数内导入：避免 app.props 模块级卷入该模块更长的导入链

    episode = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if episode is None:
        return None
    return dict(episode), int(episode["episode_no"]), _episode_source_text(conn, episode)


def _target_shot_nos(shot_rows: list[Any], shot_ids: list[str] | None) -> set[int] | None:
    if not shot_ids:
        return None
    wanted = set(shot_ids)
    return {int(row["shot_no"]) for row in shot_rows if row["id"] in wanted}


def _filter_candidates_for_shots(
    candidates: dict[str, dict[str, Any]], target_shot_nos: set[int] | None,
) -> dict[str, dict[str, Any]]:
    if target_shot_nos is None:
        return candidates
    return {
        label: info for label, info in candidates.items()
        if target_shot_nos & info["shot_nos"]
    }


async def ensure_storyboard_prop_cards(
    *, project_id: str, episode_id: str, shot_ids: list[str] | None = None,
) -> dict[str, Any]:
    """补齐一集（或 ``shot_ids`` 指定的若干段）里缺卡的道具。候选判定始终按
    整集计算，``shot_ids`` 只过滤最终要并发建卡的目标集合（与
    ``ensure_scene_state_views`` 同一取舍）。"""
    with rollback_uncommitted_on_error(conn := get_conn(), where="ensure_storyboard_prop_cards"):
        resolved = _episode_text_and_no(conn, episode_id)
        if resolved is None:
            return {"candidates": [], "attempted": []}
        episode, episode_no, source_text = resolved
        bible = _load_bible_for_ensure(conn, project_id)
        if bible is None:
            return {"candidates": [], "attempted": []}
        shot_rows = load_episode_shot_rows(conn, episode_id)
        candidates = candidate_labels_without_card(conn, project_id, episode_no, shot_rows)
        target_shot_nos = _target_shot_nos(shot_rows, shot_ids)
        scoped = _filter_candidates_for_shots(candidates, target_shot_nos)
        buildable = {
            label: info for label, info in scoped.items()
            if _is_blocking(get_pending(conn, project_id=project_id, label=label))
        }
        if buildable:
            style = bible.world.visual_style_canonical
            ep_label = f"第 {episode_no} 集"
            semaphore = asyncio.Semaphore(_MAX_CONCURRENT_BUILDS)
            # 别名收紧的数据来源：本集（始终按整集，不受 shot_ids 收窄，与候选判定
            # 同一取舍）分镜里实际出现过的全部 label——模型提议的别名只有落在这个
            # 集合里才登记，见 service.register_prop_card_for_label 的案情与派单。
            episode_labels = frozenset(label_shot_occurrences(shot_rows))

            async def _run(label: str, info: dict[str, Any]) -> None:
                async with semaphore:
                    await _build_one_label(
                        project_id=project_id, episode_no=episode_no, label=label, info=info,
                        style=style, ep_label=ep_label, source_text=source_text, cards=bible.props,
                        allowed_aliases=episode_labels,
                    )

            await asyncio.gather(*[_run(label, info) for label, info in buildable.items()])
        return {"candidates": sorted(scoped), "attempted": sorted(buildable)}


async def _run_ensure_quietly(*, project_id: str, episode_id: str, shot_ids: list[str] | None) -> None:
    try:
        await ensure_storyboard_prop_cards(project_id=project_id, episode_id=episode_id, shot_ids=shot_ids)
    except Exception:  # noqa: BLE001 - 后台受理任务，失败只记日志
        log.exception("[PROP_STORYBOARD_CARD_ENSURE_BACKGROUND_FAILED] episode_id=%s", episode_id)


def launch_background_ensure(
    *, project_id: str, episode_id: str, shot_ids: list[str] | None = None,
) -> "asyncio.Task[None]":
    task = asyncio.create_task(
        _run_ensure_quietly(project_id=project_id, episode_id=episode_id, shot_ids=shot_ids),
    )
    _BACKGROUND_ENSURE_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_ENSURE_TASKS.discard)
    return task


async def pending_prop_card_gate(
    project_id: str, episode_id: str, shot_ids: list[str] | None = None,
) -> str | None:
    """生成入口闸门（P0）：只读扫描目标段（``shot_ids`` 为 ``None`` 时整集）
    涉及的候选 label，任意一个仍处在"要拦"的状态（见 ``_is_blocking``）就
    启动后台补卡并返回提示文案；全部不拦后，再检查这些段用到的既有道具卡
    是否按现行规则复核版本落后（``app.props.card_audit_ensure``，2026-10-03
    新增，触发点②），落后同样拦住并后台发起复核。全部不拦（已建成/复核已是
    最新版本/重试额度用尽）时返回 ``None``，不阻塞生成。"""
    conn = get_conn()
    resolved = _episode_text_and_no(conn, episode_id)
    if resolved is None:
        return None
    _episode, episode_no, _source_text = resolved
    bible = _load_bible_for_ensure(conn, project_id)
    if bible is None:
        return None
    shot_rows = load_episode_shot_rows(conn, episode_id)
    candidates = candidate_labels_without_card(conn, project_id, episode_no, shot_rows)
    target_shot_nos = _target_shot_nos(shot_rows, shot_ids)
    scoped = _filter_candidates_for_shots(candidates, target_shot_nos)
    pending_rows = {label: get_pending(conn, project_id=project_id, label=label) for label in scoped}
    pending = [label for label, row in pending_rows.items() if _is_blocking(row)]
    scope = "本段" if shot_ids else "本集"
    if pending:
        if any(_needs_new_launch(pending_rows[label]) for label in pending):
            launch_background_ensure(project_id=project_id, episode_id=episode_id, shot_ids=shot_ids)
        return f"{scope}涉及的 {len(pending)} 件道具正在补建参考图（约 1 分钟），生成好后再点「生成」"
    # 函数内导入：card_audit_ensure 拉入 card_audit 整条模型调用契约，只有建卡闸门
    # 全部放行、确实要查复核版本时才需要。
    from app.props import card_audit_ensure
    stale_audit = card_audit_ensure.stale_audit_props_for_shots(conn, project_id, bible, shot_rows, target_shot_nos)
    if not stale_audit:
        return None
    card_audit_ensure.launch_audit_for_stale_if_needed(conn, project_id, stale_audit)
    return f"{scope}涉及的 {len(stale_audit)} 件道具卡正在按现行规则复核外观/别名，稍后重试"
