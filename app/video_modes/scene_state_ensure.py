"""场景状态图的出图、落库与整集补齐编排（数据层/分组见同包 ``scene_state_
views.py``）。并发去重设计、状态写入的独立事务要求、后台任务强引用，整体照搬
人物造型照最终版的取舍（``git show cc2b612d:app/video_modes/character_looks_
ensure.py``——该功能已整体退场，但并发/事务模型与本功能无关，仍是可信先例）：

1. 进程内合并——``ensure_scene_state_views`` 先扫出整集全部缺口，按
   ``(scene_reference_id, state_key)`` 去重成任务字典，同一次调用里同一张状态
   图只会被调度一次。
2. 跨调用的库内抢占——``claim_or_get`` 在 ``UNIQUE(scene_reference_id,
   episode_id, state_key)`` 行上做状态 CAS：已 ready 且文件仍在、指纹匹配直接
   复用；``running`` 未超时视为另一调用正在生成，不重复触发。``running`` 超过
   ``_RUNNING_STALE_S`` 视为僵死，可重新抢占。

**状态写入必须用独立连接**：``claim_or_get``/``_mark_ready``/``_mark_failed``
改走 ``app.db.run_write_transaction``（独立连接 + ``BEGIN IMMEDIATE``），不在
调用方 ``get_conn()`` 的连接上 ``commit()``（CLAUDE.md「不得在调用方的连接上
隐式提交」）。只读扫描（``scan_episode_scene_state_needs``）继续用 ``get_conn()``。

``launch_background_ensure`` 的返回值放进模块级集合强引用、结束时自动摘除——
``asyncio.create_task`` 的返回值如果没有任何地方保留引用，事件循环可能在下一个
调度点把它当垃圾回收掉，让"已提交"的后台生成从未真正跑完。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Any

from app import config, db, hiagent
from app.atomic_io import atomic_write_bytes
from app.db import get_conn, new_id, now
from app.evidence.txn_guard import rollback_uncommitted_on_error
from app.project_settings import canvas_phrase, resolve_aspect_ratio
from app.refs import scene_visual_style_lock
from app.video_modes.scene_state_views import (
    PROMPT_VERSION,
    load_episode_shot_rows,
    normalize_state_description,
    scan_episode_scene_state_needs,
)
from app.video_modes.scene_state_views_store import (
    ensure_tables_on_connection,
    insert_running,
    update_failed,
    update_ready,
    update_running,
)

log = logging.getLogger(__name__)

_RUNNING_STALE_S = 600.0  # 10 分钟：单张图生成远不需要这么久，超时视为僵死可重新抢占
_MAX_CONCURRENT_STATES = 3
_BACKGROUND_ENSURE_TASKS: set[asyncio.Task[None]] = set()


def scene_state_prompt(
    visual_style: str, scene_name: str, description: str, aspect_ratio: str,
    prop_appearance_notes: str = "",
) -> str:
    """``prop_appearance_notes``（2026-10-05，见 ``scene_state_views.
    prop_appearance_notes_for_description``）是本段状态描述里逐字命中的道具卡
    外观陈述：状态图是在场景卡主图上做图生图编辑，图像模型会连道具的颜色/
    材质也一并照抄主图（《顾念长安》EP1 真实故障：绿萝花盆被画成酒红陶盆、
    鞋柜画成高木柜），这句话把道具外观的最终话语权明确交还给道具卡。"""
    notes_clause = f"{prop_appearance_notes} " if prop_appearance_notes else ""
    return (
        f"{scene_visual_style_lock(visual_style)}。这是一次基于参考图的状态编辑任务，不是重新构图：同一个"
        f"空间「{scene_name}」、同一机位与构图，墙面、门窗、家具的位置与参考图保持一致，只把画面状态改为："
        f"{description}。{notes_clause}画面中没有任何人物，不出现文字、字幕、水印、logo。{canvas_phrase(aspect_ratio)}。"
    )


def _scene_state_operation_id(scene_reference_id: str, state_key: str, fingerprint: str) -> str:
    material = f"{scene_reference_id}:{state_key}:{fingerprint}:{PROMPT_VERSION}"
    return "op_scene_state_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _state_image_path(project_id: str, scene_reference_id: str) -> str:
    root = config.PROJECTS_DIR / project_id / "refs" / "scene_states"
    root.mkdir(parents=True, exist_ok=True)
    return str(root / f"{scene_reference_id}__{new_id('scstate')}.jpg")


async def _save_state_image_item(item: dict, dest: str) -> None:
    if item.get("url"):
        await hiagent.download(item["url"], dest)
    elif item.get("b64_json"):
        import base64  # 命中率很低的分支（供应商通常回 url），按需导入避免常驻成本

        atomic_write_bytes(dest, base64.b64decode(item["b64_json"]))
    else:
        raise hiagent.ProviderError(f"图像响应缺少 url/b64_json：{list(item.keys())}")


def _claim_or_get_operation(
    conn: Any, *, project_id: str, episode_id: str, scene_reference_id: str, state_key: str,
    description: str, fingerprint: str,
) -> tuple[dict[str, Any], bool]:
    """``claim_or_get`` 的事务体：读当前行→判定→置 running 必须在同一个独立写
    事务里原子完成，才是真 CAS。"""
    ensure_tables_on_connection(conn)
    row = conn.execute(
        "SELECT * FROM scene_state_views WHERE scene_reference_id=? AND episode_id=? AND state_key=?",
        (scene_reference_id, episode_id, state_key),
    ).fetchone()
    stamp = now()
    if row:
        row = dict(row)
        is_ready = row["status"] == "ready" and row["image_path"] and Path(row["image_path"]).is_file()
        if is_ready and row.get("input_fingerprint") == fingerprint:
            return row, False
        if row["status"] == "running" and (stamp - float(row["updated_at"] or 0)) < _RUNNING_STALE_S:
            return row, False
        update_running(conn, row_id=row["id"], description=description, fingerprint=fingerprint, stamp=stamp)
        row["status"] = "running"
        return row, True
    row_id = new_id("scstate")
    insert_running(
        conn, row_id=row_id, project_id=project_id, episode_id=episode_id, scene_reference_id=scene_reference_id,
        state_key=state_key, description=description, fingerprint=fingerprint, stamp=stamp,
    )
    return {"id": row_id, "status": "running"}, True


async def claim_or_get(
    *, project_id: str, episode_id: str, scene_reference_id: str, state_key: str,
    description: str, fingerprint: str,
) -> tuple[dict[str, Any], bool]:
    def _operation(conn: Any) -> tuple[dict[str, Any], bool]:
        return _claim_or_get_operation(
            conn, project_id=project_id, episode_id=episode_id, scene_reference_id=scene_reference_id,
            state_key=state_key, description=description, fingerprint=fingerprint,
        )
    return await db.run_write_transaction(_operation)


async def _mark_ready(*, row_id: str, image_path: str, prompt: str, fingerprint: str) -> None:
    def _operation(conn: Any) -> None:
        ensure_tables_on_connection(conn)
        update_ready(conn, row_id=row_id, image_path=image_path, prompt=prompt, fingerprint=fingerprint, stamp=now())
    await db.run_write_transaction(_operation)


async def _mark_failed(*, row_id: str, error: str) -> None:
    def _operation(conn: Any) -> None:
        ensure_tables_on_connection(conn)
        update_failed(conn, row_id=row_id, error=error, stamp=now())
    await db.run_write_transaction(_operation)


async def _generate_one_state(
    *, project_id: str, episode_id: str, visual_style: str, aspect_ratio: str, spec: dict[str, Any],
) -> None:
    scene_reference_id, state_key = spec["scene_reference_id"], spec["state_key"]
    establishing_path = spec["establishing_image_path"]
    row, should_generate = await claim_or_get(
        project_id=project_id, episode_id=episode_id, scene_reference_id=scene_reference_id,
        state_key=state_key, description=spec["description"], fingerprint=spec["fingerprint"],
    )
    if not should_generate:
        return
    if not establishing_path or not Path(establishing_path).is_file():
        # 没有种子图就没有"默认状态"这个参照系——宁可失败也不猜，不盲出图。
        await _mark_failed(row_id=row["id"], error="场景卡主图缺失，无法生成状态图")
        return
    if not normalize_state_description(spec["description"]):
        # 2026-10-02 代码评审 #0：分镜模型只被要求填 scene_state_matches_card，
        # description 没有对应的"必须填"提示词规则，串内全部段落都没写这段
        # 状态具体是什么时，没有任何依据知道该往参考图上改什么——盲目拿默认
        # 状态图原样出一遍图，会把一张几乎等同默认状态、却被当作"已确认新
        # 状态"的图发给视频模型，比改动前"不发图、全凭正文"的降级更危险
        # （CLAUDE.md「不得兜底填充」）。
        await _mark_failed(row_id=row["id"], error="场景状态描述为空，无法生成状态图")
        return
    prompt = scene_state_prompt(
        visual_style, spec["scene_name"], spec["description"], aspect_ratio,
        spec.get("prop_appearance_notes", ""),
    )
    try:
        size = config.SCENE_REF_SIZES.get(aspect_ratio, config.REF_IMAGE_SIZE)
        seed = [hiagent.data_url_from_file(establishing_path)]
        path = _state_image_path(project_id, scene_reference_id)
        item = await hiagent.generate_image(
            prompt, size=size, image_inputs=seed,
            call_meta={
                "asset_kind": "scene_state", "view_role": "scene_state", "scene_name": spec["scene_name"],
                "operation_id": _scene_state_operation_id(scene_reference_id, state_key, spec["fingerprint"]),
                "reuse_successful_operation": True,
            },
        )
        await _save_state_image_item(item, path)
        await _mark_ready(row_id=row["id"], image_path=path, prompt=prompt, fingerprint=spec["fingerprint"])
    except Exception as exc:  # noqa: BLE001 - 失败要落库可见，不吞；不向上抛出以免一张图失败打断整批
        await _mark_failed(row_id=row["id"], error=str(exc))


def _load_bible_for_ensure(conn: Any, project_id: str) -> Any | None:
    from app.schemas import Bible  # 函数内导入：避免 video_modes 包初始化期对 app.schemas 解析顺序产生额外耦合面（同包其它模块同款写法）

    row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row or not (row["bible_json"] or "").strip():
        return None
    return Bible.model_validate_json(row["bible_json"])


def _target_shot_nos(shot_rows: list[Any], shot_ids: list[str] | None) -> set[int] | None:
    if not shot_ids:
        return None
    wanted = set(shot_ids)
    return {int(row["shot_no"]) for row in shot_rows if row["id"] in wanted}


def _filter_items_for_shots(items: list[dict[str, Any]], target_shot_nos: set[int] | None) -> list[dict[str, Any]]:
    if target_shot_nos is None:
        return items
    return [item for item in items if target_shot_nos & set(item["shot_nos"])]


def summarize_scene_state_items(items: list[dict[str, Any]]) -> dict[str, int]:
    summary = {"ready": 0, "generating": 0, "failed": 0, "missing": 0}
    for item in items:
        status = item.get("status")
        key = "generating" if status == "running" else ("missing" if status == "stale" else status)
        if key in summary:
            summary[key] += 1
    return summary


async def ensure_scene_state_views(
    *, project_id: str, episode_id: str, shot_ids: list[str] | None = None,
) -> dict[str, Any]:
    """补齐一集（或 ``shot_ids`` 指定的若干段所属状态串）里缺失的场景状态图。
    分组始终按整集计算（状态串可能跨越目标段之外的镜头起串），``shot_ids`` 只
    用来过滤最终结果，不改变分组输入。"""
    with rollback_uncommitted_on_error(conn := get_conn(), where="ensure_scene_state_views"):
        if not conn.execute("SELECT 1 FROM episodes WHERE id=?", (episode_id,)).fetchone():
            raise ValueError(f"episode not found: {episode_id}")
        bible = _load_bible_for_ensure(conn, project_id)
        if bible is None:
            return {"items": [], "summary": summarize_scene_state_items([])}
        shot_rows = load_episode_shot_rows(conn, episode_id)
        items = scan_episode_scene_state_needs(
            conn=conn, bible=bible, project_id=project_id, episode_id=episode_id, shot_rows=shot_rows,
        )
        target_shot_nos = _target_shot_nos(shot_rows, shot_ids)
        tasks: dict[tuple[str, str], dict[str, Any]] = {
            (item["scene_reference_id"], item["state_key"]): item
            for item in _filter_items_for_shots(items, target_shot_nos)
            if item["status"] in ("missing", "failed", "running", "stale")
        }
        if tasks:
            aspect_ratio = resolve_aspect_ratio(conn, project_id)
            visual_style = bible.world.visual_style_canonical
            semaphore = asyncio.Semaphore(_MAX_CONCURRENT_STATES)

            async def _run(spec: dict[str, Any]) -> None:
                async with semaphore:
                    await _generate_one_state(
                        project_id=project_id, episode_id=episode_id, visual_style=visual_style,
                        aspect_ratio=aspect_ratio, spec=spec,
                    )

            await asyncio.gather(*[_run(spec) for spec in tasks.values()])
        final_items = _filter_items_for_shots(
            scan_episode_scene_state_needs(
                conn=conn, bible=bible, project_id=project_id, episode_id=episode_id, shot_rows=shot_rows,
            ),
            target_shot_nos,
        )
        return {"items": final_items, "summary": summarize_scene_state_items(final_items)}


async def _run_ensure_quietly(*, project_id: str, episode_id: str, shot_ids: list[str] | None) -> None:
    try:
        await ensure_scene_state_views(project_id=project_id, episode_id=episode_id, shot_ids=shot_ids)
    except Exception:  # noqa: BLE001 - 后台受理任务，失败只记日志
        log.exception("[SCENE_STATE_ENSURE_BACKGROUND_FAILED] episode_id=%s", episode_id)


def launch_background_ensure(
    *, project_id: str, episode_id: str, shot_ids: list[str] | None = None,
) -> "asyncio.Task[None]":
    task = asyncio.create_task(
        _run_ensure_quietly(project_id=project_id, episode_id=episode_id, shot_ids=shot_ids),
    )
    _BACKGROUND_ENSURE_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_ENSURE_TASKS.discard)
    return task


def resolve_episode_project(episode_id: str) -> str | None:
    row = get_conn().execute("SELECT project_id FROM episodes WHERE id=?", (episode_id,)).fetchone()
    return str(row["project_id"]) if row else None


def resolve_shot_scope(shot_id: str) -> tuple[str, str] | None:
    conn = get_conn()
    shot_row = conn.execute("SELECT episode_id FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot_row:
        return None
    episode_row = conn.execute(
        "SELECT project_id FROM episodes WHERE id=?", (shot_row["episode_id"],),
    ).fetchone()
    if not episode_row:
        return None
    return str(episode_row["project_id"]), str(shot_row["episode_id"])


async def pending_scene_state_gate(
    project_id: str, episode_id: str, shot_ids: list[str] | None = None,
) -> str | None:
    """生成入口闸门（P0）：只读扫描目标段（``shot_ids`` 为 ``None`` 时整集）所属
    状态串的需求，``missing``/``running``/``stale`` 计为待补。``failed`` 不拦——
    照常生成，装配期会退回现有省略行为并留 advisory。"""
    conn = get_conn()
    if not conn.execute("SELECT 1 FROM episodes WHERE id=?", (episode_id,)).fetchone():
        return None
    bible = _load_bible_for_ensure(conn, project_id)
    if bible is None:
        return None
    shot_rows = load_episode_shot_rows(conn, episode_id)
    items = scan_episode_scene_state_needs(
        conn=conn, bible=bible, project_id=project_id, episode_id=episode_id, shot_rows=shot_rows,
    )
    target_shot_nos = _target_shot_nos(shot_rows, shot_ids)
    pending = [
        item for item in _filter_items_for_shots(items, target_shot_nos)
        if item["status"] in ("missing", "running", "stale")
    ]
    if not pending:
        return None
    launch_background_ensure(project_id=project_id, episode_id=episode_id, shot_ids=shot_ids)
    scope = "本段" if shot_ids else "本集"
    return f"{scope}需要的 {len(pending)} 张场景状态图正在生成（约 1 分钟），生成好后再点「生成」"
