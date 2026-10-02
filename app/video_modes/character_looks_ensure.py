"""人物造型照的出图、落库与整集补齐编排（数据层见同包 ``character_look_views.py``）。

出图提示词刻意不复用 ``app.multiview.character_view_prompt``：那个函数的视角合同表
（``CHARACTER_VIEW_FRAMING``）定义在 ``app/multiview.py`` 里，该文件已逼近
``app/FILE_CONVENTIONS.toml`` 的行数棘轮基线（零余量），新增一个视角角色会把它推
过基线；本模块改为独立构造提示词，复用的是 ``app.refs`` 里与画风/外观锚点相关的
纯函数原语（``character_visual_style_lock``/``portrait_override_appearance_anchor``/
``production_appearance_anchor``/``_PORTRAIT_CLOTHING_CONTRACT``），与
``character_view_prompt`` 内部用的是同一套，只是不经过那张表。种子图自带的服装
合同必须真正剥离（而不是跳过追加）——做法照抄 ``character_view_prompt`` 对
``face_closeup`` 的处理（见 commit a9b93274）：服装句已字面烘焙进外观锚点文本，
只有逐字替换掉 ``_PORTRAIT_CLOTHING_CONTRACT`` 才能不把它带进本段造型照。

并发去重设计（CLAUDE.md「Ownership Must Be Explicit」：状态转移必须显式声明用谁
的事务）：
1. 进程内合并——``ensure_character_looks`` 先用 ``scan_episode_character_look_
   needs`` 扫出全部需求，按 ``(portrait_id, look_key)`` 去重成任务字典，同一次
   调用里同一张造型照只会被调度一次。
2. 跨调用的库内抢占——``claim_or_get`` 在同一个 ``UNIQUE(portrait_id, look_key)``
   行上做状态 CAS：已 ready 且文件仍在直接复用；已 ``running`` 视为「另一个调用
   正在生成」，本次不重复触发（不等待、不阻塞）。这覆盖两个并发 HTTP 触发、或
   「生成入口闸门」与「用户手动点补齐」撞车的情况。``running`` 卡死（进程被杀、
   从未回写终态）按 ``_RUNNING_STALE_S`` 超时视为可重新抢占——没有这条，一次崩
   溃会把这张造型照永久卡在 generating。
不做跨进程分布式锁：本仓库只有一个后端进程持有这个 SQLite 连接，进程内信号量
（``asyncio.Semaphore``）已经保证了真正的并发互斥；第 2 条库内 CAS 防的是"同一
进程内两次独立的 ensure 调用前后脚各自扫描到同一个缺口"这种时间窗口竞争，不是
需要跨进程锁的场景。

**状态写入必须用独立连接，不能用调用方的 ``get_conn()``**：``claim_or_get``/
``_mark_ready``/``_mark_failed`` 改走 ``app.db.run_write_transaction``（独立连接 +
``BEGIN IMMEDIATE`` + 离开事件循环，见 app/db.py）。原因是真实的——生成入口闸门
与连播钩子都会在"调用方自己的业务事务还没提交"的那个 ``get_conn()`` 任务局部连接
上 ``await ensure_character_looks(...)``；如果造型照的状态转移在那个连接上
``commit()``，就会把调用方尚未提交的写一起提交下去（CLAUDE.md「不得在调用方的
连接上隐式提交」，本仓库已因同类问题毁过三次真实数据）。只读扫描
（``scan_episode_character_look_needs``）不写状态，继续用 ``get_conn()``。

后台任务的强引用：``launch_background_ensure`` 是 POST /character-looks 与生成
入口闸门共用的唯一入口，返回的 ``asyncio.Task`` 放进模块级集合强引用并在结束时
自动摘除——``asyncio.create_task`` 的返回值如果没有任何地方保留引用，事件循环
可能在下一个调度点把它当垃圾回收掉，让"已提交"的后台生成从未真正跑完（Python
官方文档对此有明确警告）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from app import config, db, hiagent
from app.atomic_io import atomic_write_bytes
from app.db import get_conn, new_id, now
from app.evidence.txn_guard import rollback_uncommitted_on_error
from app.refs import (
    _PORTRAIT_CLOTHING_CONTRACT,
    character_visual_style_lock,
    normalize_prompt_text,
    portrait_override_appearance_anchor,
    production_appearance_anchor,
)
from app.video_modes.character_look_views import (
    normalize_look_key_text,
    scan_episode_character_look_needs,
)
from app.video_modes.character_look_views_store import (
    ensure_tables_on_connection,
    insert_running,
    update_failed,
    update_ready,
    update_running,
)

log = logging.getLogger(__name__)

_RUNNING_STALE_S = 600.0  # 10 分钟：单张图生成远不需要这么久，超时视为僵死可重新抢占
_LOOK_PROMPT_VERSION = "v1"
_MAX_CONCURRENT_LOOKS = 3
_BACKGROUND_ENSURE_TASKS: set[asyncio.Task[None]] = set()


def character_look_prompt(
    visual_style: str, appearance: str, portrait_prompt: str | None, wardrobe_text: str,
) -> str:
    raw_source = portrait_override_appearance_anchor(appearance, portrait_prompt) or production_appearance_anchor(appearance)
    source = normalize_prompt_text(
        raw_source.replace(f"{_PORTRAIT_CLOTHING_CONTRACT}。", "").replace(_PORTRAIT_CLOTHING_CONTRACT, ""),
    )
    wardrobe = normalize_prompt_text(wardrobe_text).strip()
    return (
        f"{character_visual_style_lock(visual_style)}。"
        f"角色外观真值锚点：{source}。"
        "外观补充与全局画风是两个独立合同；冲突时全局画风优先，不得按外观文案关键词删除或重写内容。"
        "生成同一角色本段造型照：正面全身立绘入画，全身完整可见，中性站姿，双臂自然，头部与面部在画面中占比较小；"
        "本视角的构图合同优先于前文关于默认定妆照服装、配饰的任何描述——参考图中出现的原有服装、配饰与颜色一律不得"
        f"带入本视角画面，本段实际穿着以下列描述为准：{wardrobe}。"
        "纯浅色背景，单角色，不得出现第二个人物。"
        "同一角色、只改变本段服装，不改变脸部、发型、体型这类稳定身份；结果必须满足结构化资产 QA。"
    )


def _look_image_path(project_id: str, portrait_id: str, look_key: str) -> str:
    root = config.PROJECTS_DIR / project_id / "refs" / "looks"
    root.mkdir(parents=True, exist_ok=True)
    return str(root / f"{portrait_id}__{look_key}__{new_id('look')}.jpg")


def _look_operation_id(portrait_id: str, look_key: str, fingerprint: str) -> str:
    """指纹必须并进 material：定妆照换图/画风变了会算出新指纹，这里不带上它的话，
    ``reuse_successful_operation`` 会按旧 operation_id 把过期的旧图原样复用回来。"""
    material = f"{portrait_id}:{look_key}:{fingerprint}:{_LOOK_PROMPT_VERSION}"
    return "op_character_look_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _look_input_fingerprint(*, front_full_image_path: str, wardrobe_text: str, visual_style: str) -> str:
    material = json.dumps(
        {
            "front_full_image_path": front_full_image_path,
            "wardrobe_text": normalize_look_key_text(wardrobe_text),
            "visual_style": visual_style,
            "version": _LOOK_PROMPT_VERSION,
        },
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


async def _save_look_image_item(item: dict, dest: str) -> None:
    if item.get("url"):
        await hiagent.download(item["url"], dest)
    elif item.get("b64_json"):
        import base64  # 命中率很低的分支（供应商通常回 url），按需导入避免常驻成本

        atomic_write_bytes(dest, base64.b64decode(item["b64_json"]))
    else:
        raise hiagent.ProviderError(f"图像响应缺少 url/b64_json：{list(item.keys())}")


def _claim_or_get_operation(
    conn: sqlite3.Connection, *, project_id: str, portrait_id: str, look_key: str,
    wardrobe_text: str, fingerprint: str,
) -> tuple[dict[str, Any], bool]:
    """``claim_or_get`` 的事务体：读当前行→判定→置 running 必须在同一个独立写
    事务里原子完成，才是真 CAS（读到的行与写回的行之间不能被另一个并发事务
    插队）。``ensure_tables_on_connection`` 用同一连接建表，不调 ``ensure_
    schema()``——理由见 ``character_look_views_store`` 模块文档（会死锁）。"""
    ensure_tables_on_connection(conn)
    row = conn.execute(
        "SELECT * FROM character_look_views WHERE portrait_id=? AND look_key=?",
        (portrait_id, look_key),
    ).fetchone()
    stamp = now()
    if row:
        row = dict(row)
        is_ready = row["status"] == "ready" and row["image_path"] and Path(row["image_path"]).is_file()
        if is_ready and row.get("input_fingerprint") == fingerprint:
            return row, False
        if row["status"] == "running" and (stamp - float(row["updated_at"] or 0)) < _RUNNING_STALE_S:
            return row, False
        update_running(conn, row_id=row["id"], wardrobe_text=wardrobe_text, fingerprint=fingerprint, stamp=stamp)
        row["status"] = "running"
        return row, True
    row_id = new_id("look")
    insert_running(
        conn, row_id=row_id, project_id=project_id, portrait_id=portrait_id, look_key=look_key,
        wardrobe_text=wardrobe_text, fingerprint=fingerprint, stamp=stamp,
    )
    return {"id": row_id, "status": "running"}, True


async def claim_or_get(
    *, project_id: str, portrait_id: str, look_key: str, wardrobe_text: str, fingerprint: str,
) -> tuple[dict[str, Any], bool]:
    """返回 ``(row, should_generate)``。ready 且文件存在、指纹匹配时直接复用；
    running 且未超时视为另一调用正在生成；其余情况（含首次/failed/过期指纹/
    running 超时僵死）把行置为 running 并由当前调用方负责生成。独立写事务，
    见模块文档「状态写入必须用独立连接」。"""
    def _operation(conn: sqlite3.Connection) -> tuple[dict[str, Any], bool]:
        return _claim_or_get_operation(
            conn, project_id=project_id, portrait_id=portrait_id, look_key=look_key,
            wardrobe_text=wardrobe_text, fingerprint=fingerprint,
        )
    return await db.run_write_transaction(_operation)


async def _mark_ready(*, row_id: str, image_path: str, prompt: str, fingerprint: str) -> None:
    def _operation(conn: sqlite3.Connection) -> None:
        ensure_tables_on_connection(conn)
        update_ready(conn, row_id=row_id, image_path=image_path, prompt=prompt, fingerprint=fingerprint, stamp=now())
    await db.run_write_transaction(_operation)


async def _mark_failed(*, row_id: str, error: str) -> None:
    def _operation(conn: sqlite3.Connection) -> None:
        ensure_tables_on_connection(conn)
        update_failed(conn, row_id=row_id, error=error, stamp=now())
    await db.run_write_transaction(_operation)


async def _generate_one_look(*, project_id: str, visual_style: str, spec: dict[str, Any]) -> None:
    portrait_id, key, wardrobe = spec["portrait_id"], spec["look_key"], spec["wardrobe_text"]
    front_path = spec["front_full_image_path"]
    fingerprint = _look_input_fingerprint(
        front_full_image_path=front_path, wardrobe_text=wardrobe, visual_style=visual_style,
    )
    row, should_generate = await claim_or_get(
        project_id=project_id, portrait_id=portrait_id, look_key=key,
        wardrobe_text=wardrobe, fingerprint=fingerprint,
    )
    if not should_generate:
        return
    if not front_path or not Path(front_path).is_file():
        # 没有种子图出来的脸必然不是同一个人——宁可失败也不猜。
        await _mark_failed(row_id=row["id"], error="定妆照全身图缺失，无法生成造型照；先到人物谱补齐定妆照")
        return
    prompt = character_look_prompt(visual_style, spec["appearance"], spec["portrait_prompt"], wardrobe)
    try:
        seed = [hiagent.data_url_from_file(front_path)]
        path = _look_image_path(project_id, portrait_id, key)
        item = await hiagent.generate_image(
            prompt, size=config.REF_IMAGE_SIZE, image_inputs=seed,
            call_meta={
                "asset_kind": "character_look", "view_role": "look",
                "character_name": spec["character_name"],
                "operation_id": _look_operation_id(portrait_id, key, fingerprint),
                "reuse_successful_operation": True,
            },
        )
        await _save_look_image_item(item, path)
        await _mark_ready(row_id=row["id"], image_path=path, prompt=prompt, fingerprint=fingerprint)
    except Exception as exc:  # noqa: BLE001 - 失败要落库可见，不吞；不向上抛出以免一张图失败打断整批
        await _mark_failed(row_id=row["id"], error=str(exc))


def load_target_shot_rows(conn: Any, episode_id: str, shot_ids: list[str] | None) -> list[Any]:
    query = "SELECT id, shot_no, shot_contract_json FROM shots WHERE episode_id=?"
    params: list[Any] = [episode_id]
    if shot_ids:
        placeholders = ",".join("?" for _ in shot_ids)
        query += f" AND id IN ({placeholders})"
        params.extend(shot_ids)
    query += " ORDER BY shot_no"
    return conn.execute(query, params).fetchall()


def summarize_look_items(items: list[dict[str, Any]]) -> dict[str, int]:
    summary = {"ready": 0, "generating": 0, "failed": 0, "missing": 0}
    for item in items:
        status = item.get("status")
        key = "generating" if status == "running" else status
        if key in summary:
            summary[key] += 1
    return summary


def _load_bible_for_ensure(conn: Any, project_id: str) -> Any | None:
    from app.schemas import Bible  # 函数内导入：避免 video_modes 包初始化期对 app.schemas 解析顺序产生额外耦合面（同包其它模块同款写法）

    project_row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project_row or not (project_row["bible_json"] or "").strip():
        return None
    return Bible.model_validate_json(project_row["bible_json"])


async def ensure_character_looks(
    *, project_id: str, episode_id: str, shot_ids: list[str] | None = None,
) -> dict[str, Any]:
    """补齐一集（或 ``shot_ids`` 指定的若干段）里缺失的人物造型照。只读扫描
    用 ``scan_episode_character_look_needs``（走调用方的 ``get_conn()``）；本函数
    只负责把 ``missing``/``failed`` 的条目去重后有界并发（≤3）生成——真正的状态
    写入都经 ``claim_or_get``/``_mark_ready``/``_mark_failed`` 的独立事务，不碰
    这里的 ``conn``。失败的人物不影响其它人物继续生成，调用方可以安全地
    await 后继续后续流程。
    """
    with rollback_uncommitted_on_error(conn := get_conn(), where="ensure_character_looks"):
        episode = conn.execute(
            "SELECT id, episode_no FROM episodes WHERE id=?", (episode_id,),
        ).fetchone()
        if not episode:
            raise ValueError(f"episode not found: {episode_id}")
        bible = _load_bible_for_ensure(conn, project_id)
        if bible is None:
            return {"items": [], "summary": summarize_look_items([])}
        rows = load_target_shot_rows(conn, episode_id, shot_ids)
        episode_no = int(episode["episode_no"])
        items = scan_episode_character_look_needs(
            conn=conn, bible=bible, project_id=project_id, episode_no=episode_no, shot_rows=rows,
        )
        tasks: dict[tuple[str, str], dict[str, Any]] = {
            (item["portrait_id"], item["look_key"]): item
            # running 也进候选：是否真的重做交给 claim_or_get——未超时的跳过，超时僵死的（进程重启
            # 时正在生成）才回收；只挑 missing/failed 会让僵死 running 永远没人收，闸门永久拦截。
            for item in items if item["status"] in ("missing", "failed", "running")
        }
        if tasks:
            semaphore = asyncio.Semaphore(_MAX_CONCURRENT_LOOKS)

            async def _run(spec: dict[str, Any]) -> None:
                async with semaphore:
                    await _generate_one_look(
                        project_id=project_id, visual_style=bible.world.visual_style_canonical, spec=spec,
                    )

            await asyncio.gather(*[_run(spec) for spec in tasks.values()])
        # 重新扫一遍拿刚写回的终态，而不是沿用生成前的快照——GET 状态/调用方展示
        # 都需要这批任务跑完之后的真实结果，不是「已提交生成」这个中间态。
        final_items = scan_episode_character_look_needs(
            conn=conn, bible=bible, project_id=project_id, episode_no=episode_no, shot_rows=rows,
        )
        return {"items": final_items, "summary": summarize_look_items(final_items)}


async def _run_ensure_quietly(
    *, project_id: str, episode_id: str, shot_ids: list[str] | None,
) -> None:
    try:
        await ensure_character_looks(project_id=project_id, episode_id=episode_id, shot_ids=shot_ids)
    except Exception:  # noqa: BLE001 - 后台受理任务，失败只记日志；调用方靠 GET 状态/下次闸门检查观察结果
        log.exception("[CHARACTER_LOOKS_ENSURE_BACKGROUND_FAILED] episode_id=%s", episode_id)


def launch_background_ensure(
    *, project_id: str, episode_id: str, shot_ids: list[str] | None = None,
) -> "asyncio.Task[None]":
    """后台触发一次 ``ensure_character_looks``，不等待完成。返回的 Task 放进
    模块级集合强引用、结束时自动摘除——见模块文档「后台任务的强引用」。
    POST /episodes/{id}/character-looks（``character_looks_api.py``）与生成入口
    闸门（``pending_character_looks_gate``）共用这一个入口，不要各写一份。
    """
    task = asyncio.create_task(
        _run_ensure_quietly(project_id=project_id, episode_id=episode_id, shot_ids=shot_ids),
    )
    _BACKGROUND_ENSURE_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_ENSURE_TASKS.discard)
    return task


def resolve_episode_project(episode_id: str) -> str | None:
    """按 episode_id 查出 project_id；查不到返回 ``None``。"""
    row = get_conn().execute("SELECT project_id FROM episodes WHERE id=?", (episode_id,)).fetchone()
    return str(row["project_id"]) if row else None


def resolve_shot_scope(shot_id: str) -> tuple[str, str] | None:
    """按 shot_id 查出 ``(project_id, episode_id)``；查不到返回 ``None``，调用方
    （生成入口闸门的 handler 侧）按既有口径处理"镜头不存在"，本函数不代为报错。"""
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


async def pending_character_looks_gate(
    project_id: str, episode_id: str, shot_ids: list[str] | None = None,
) -> str | None:
    """生成入口闸门（P0）：只读扫描目标段（``shot_ids`` 为 ``None`` 时整集）的
    造型照需求，``missing``/``running`` 计为待补。有待补时返回一条中文提示供
    调用方 409 拦截；``shot_ids`` 非空时按"本段"措辞，否则按"本集"。含
    ``missing`` 时顺带后台启动一次补齐（``running`` 已经有别的调用在生成，不必
    再触发）。``failed`` 不拦——照常生成，选图会退回定妆照并留可见提示。项目
    没有 bible、或扫不到任何需求时放行（返回 ``None``）。
    """
    conn = get_conn()
    episode_row = conn.execute("SELECT episode_no FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not episode_row:
        return None
    bible = _load_bible_for_ensure(conn, project_id)
    if bible is None:
        return None
    rows = load_target_shot_rows(conn, episode_id, shot_ids)
    items = scan_episode_character_look_needs(
        conn=conn, bible=bible, project_id=project_id,
        episode_no=int(episode_row["episode_no"]), shot_rows=rows,
    )
    pending = [item for item in items if item["status"] in ("missing", "running")]
    if not pending:
        return None
    # 含 running 也启动：僵死的 running（进程重启时正在生成）只有 ensure 的 claim_or_get 能回收，未超时的会被它跳过
    launch_background_ensure(project_id=project_id, episode_id=episode_id, shot_ids=shot_ids)
    scope = "本段" if shot_ids else "本集"
    return (
        f"{scope}需要的 {len(pending)} 张人物造型照正在生成（约 1 分钟），生成好后再点「生成」；"
        "进度见分镜台「补齐造型照」"
    )
