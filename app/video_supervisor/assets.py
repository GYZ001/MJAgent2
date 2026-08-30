"""整集参考素材（人物/场景）预热扫描与准备。"""
from __future__ import annotations

import asyncio
import json

from typing import Any

from app.db import get_conn
from app.evidence import repository as evidence_repository

from .authority import _verify_supervisor_paid_authority
from .checkpoint import _save_checkpoint_async
from .constants import ASSET_PREP_HEARTBEAT_INTERVAL_S, SUPERVISOR_HEARTBEAT_STALE_S, _REFERENCE_ASSET_PREP_LOCKS
from .models import VideoSupervisorCheckpoint



def _reference_asset_scan(episode_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the read-only episode asset scan using the persisted storyboard."""
    conn = get_conn()
    episode = conn.execute(
        "SELECT id, project_id, episode_no FROM episodes WHERE id=?", (episode_id,),
    ).fetchone()
    if not episode:
        raise ValueError(f"episode not found: {episode_id}")
    project = conn.execute(
        "SELECT bible_json FROM projects WHERE id=?", (episode["project_id"],),
    ).fetchone()
    if not project or not (project["bible_json"] or "").strip():
        return dict(episode), {"characters": [], "scenes": [], "blockers": []}
    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,),
    ).fetchall()
    from app.domain.storyboard_ops import _board_from_shot_rows
    from app.multiview import scan_episode_reference_asset_gaps
    from app.production.screenplay_authority import resolve_downstream_screenplay
    from app.schemas import Bible

    board = _board_from_shot_rows(rows, int(episode["episode_no"]))
    bible = Bible.model_validate_json(project["bible_json"])
    screenplay = resolve_downstream_screenplay(episode_id, conn=conn).screenplay
    scan = scan_episode_reference_asset_gaps(
        project_id=episode["project_id"],
        episode_no=int(episode["episode_no"]),
        shots=[(row["id"], board.shots[index]) for index, row in enumerate(rows)],
        conn=conn,
        bible=bible,
        screenplay=screenplay,
    )
    return dict(episode), scan


async def _asset_prep_heartbeat(
    cp: VideoSupervisorCheckpoint,
    *,
    run_id: str | None,
    stop: asyncio.Event,
    interval_s: float = ASSET_PREP_HEARTBEAT_INTERVAL_S,
) -> None:
    """Keep long reference generation from looking like a dead supervisor.

    Character and scene packs can each spend several minutes in a provider call,
    and preparation may also wait behind the per-project lock.  Neither wait is a
    control-plane failure, so keep both the run row and checkpoint fresh until the
    preparation stage exits.  Ownership is checked before every write so an old
    task cannot revive its heartbeat after a newer run has taken over.
    """
    wait_s = max(0.01, min(float(interval_s), SUPERVISOR_HEARTBEAT_STALE_S / 3.0))
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait_s)
            return
        except asyncio.TimeoutError:
            pass
        if run_id:
            owner = get_conn().execute(
                "SELECT active_video_run_id FROM episodes WHERE id=?",
                (cp.episode_id,),
            ).fetchone()
            if not owner or owner["active_video_run_id"] != run_id:
                return
        cp.phase = "PREPARING_ASSETS"
        await _save_checkpoint_async(cp, run_id=run_id)


async def _prepare_episode_reference_assets(
    episode_id: str,
    *,
    cp: VideoSupervisorCheckpoint,
    run_id: str | None,
) -> dict[str, Any]:
    """Prepare only missing Bible-managed assets before any video dispatch."""
    _verify_supervisor_paid_authority(cp, stage="reference_asset_scan")
    episode, initial = _reference_asset_scan(episode_id)
    if not initial["blockers"]:
        return initial

    cp.phase = "PREPARING_ASSETS"
    cp.outcome = None
    await _save_checkpoint_async(cp, run_id=run_id)
    if run_id:
        evidence_repository.append_event(
            run_id,
            "VIDEO_REFERENCE_ASSET_PREP_STARTED",
            "info",
            "正在补齐本集视频所需的人物与场景资产",
            payload={
                "characters": initial["characters"],
                "scenes": initial["scenes"],
            },
        )

    project_id = str(episode["project_id"])
    lock = _REFERENCE_ASSET_PREP_LOCKS.setdefault(project_id, asyncio.Lock())
    heartbeat_stop = asyncio.Event()
    heartbeat_task = asyncio.create_task(
        _asset_prep_heartbeat(cp, run_id=run_id, stop=heartbeat_stop),
        name=f"video-asset-prep-heartbeat:{episode_id}",
    )
    try:
        async with lock:
            _, current = _reference_asset_scan(episode_id)
            conn = get_conn()
            project = conn.execute(
                "SELECT bible_json FROM projects WHERE id=?", (project_id,),
            ).fetchone()
            bible_payload = json.loads(project["bible_json"] or "{}") if project else {}
            visual_style = str(
                (bible_payload.get("world") or {}).get("visual_style_canonical") or ""
            )
            episode_no = int(episode["episode_no"])
            initial_characters: list[str] = []
            if current["characters"]:
                from app.multiview import complete_legacy_character_pack

                for name in current["characters"]:
                    _verify_supervisor_paid_authority(
                        cp,
                        stage="character_pack_completion",
                    )
                    pack = await complete_legacy_character_pack(
                        project_id, name, episode_no, visual_style,
                    )
                    if pack is None:
                        initial_characters.append(name)
            if initial_characters:
                from app.refs import generate_refs
                _verify_supervisor_paid_authority(
                    cp,
                    stage="character_reference_generation",
                )
                await generate_refs(
                    project_id,
                    only_characters=initial_characters,
                    resume=True,
                )
            # Portrait generation merges a newer Bible snapshot, so re-scan before
            # preparing scenes rather than reusing stale project JSON.
            _, current = _reference_asset_scan(episode_id)
            initial_scenes: list[str] = []
            if current["scenes"]:
                from app.multiview import complete_legacy_scene_pack

                for name in current["scenes"]:
                    _verify_supervisor_paid_authority(
                        cp,
                        stage="scene_pack_completion",
                    )
                    pack = await complete_legacy_scene_pack(
                        project_id, name, episode_no, visual_style,
                    )
                    if pack is None:
                        initial_scenes.append(name)
            if initial_scenes:
                from app.scenes import generate_scene_refs
                _verify_supervisor_paid_authority(
                    cp,
                    stage="scene_reference_generation",
                )
                await generate_scene_refs(
                    project_id,
                    only_scene=initial_scenes,
                    resume=True,
                )
            _, current = _reference_asset_scan(episode_id)
    finally:
        heartbeat_stop.set()
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)

    if current["blockers"]:
        # 补齐动作已完成有界尝试；缺口转为输入风险，后续镜头使用已有锚点、
        # 关键帧或纯文本继续，不能把整集停在资产门禁。
        if run_id:
            evidence_repository.append_event(
                run_id,
                "VIDEO_REFERENCE_ASSET_PREP_FALLBACK",
                "warning",
                "参考资产补齐重试耗尽，继续使用当前可用产物",
                payload={"blockers": current["blockers"][:8]},
            )
    if run_id:
        evidence_repository.append_event(
            run_id,
            "VIDEO_REFERENCE_ASSET_PREP_COMPLETED",
            "info",
            "本集视频所需资产已就绪",
        )
    return current
