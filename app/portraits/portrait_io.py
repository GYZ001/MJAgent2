"""定妆照资产的读写：登记、暂存、提升、按集选用，以及重绘/生成
定妆照图片的底层调用。
"""

from __future__ import annotations

import base64
import json
import sqlite3

from pathlib import Path

from app.evidence import repository as evidence_repository
from app import config, hiagent
from app.atomic_io import atomic_write_bytes
from app.bible_store import mutate_bible_json
from app.db import get_conn, new_id, now
from app.errors import ContentGenerationError, code_ref
from app.evidence.media import record_reference_asset
from app.harness.types import EvidenceArtifact
from app.portraits.card_owner import resolve_card_owner
from app.refs import _safe_name, portrait_prompt, production_appearance_anchor
from app.schemas import Bible

from ._db_probe import (
    _has_column,
    _has_table,
)
from ._identity_tokens import _visual_entity_id_for_resolution_safe
from .constants import STAGED_INITIAL_EP_START
from .current_ref import portrait_for_episode

# ---------- 定妆照落盘 / 登记 ----------

async def _save_image_item(item: dict, dest: str) -> None:
    """把 hiagent.generate_image 的返回落盘到 dest（url 优先下载，其次写 b64）。"""
    if item.get("url"):
        await hiagent.download(item["url"], dest)
    elif item.get("b64_json"):
        atomic_write_bytes(dest, base64.b64decode(item["b64_json"]))
    else:
        raise hiagent.ProviderError(f"图像响应缺少 url/b64_json：{list(item.keys())}")


def _portrait_dir(project_id: str) -> Path:
    d = config.PROJECTS_DIR / project_id / "refs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _new_portrait_path(project_id: str, name: str, ep_start: int) -> str:
    return str(
        _portrait_dir(project_id)
        / f"{_safe_name(name)}__ep{ep_start}__{new_id('candidate')}.jpg"
    )


async def _review_portrait_asset(image_path: str, appearance: str) -> dict:
    """VLM 图片质检已下线：反应式定妆照是否可用只看文件是否存在（技术校验）。

    保留函数签名和空字典返回值，使既有调用方（按集反应式重绘）无需改动即可继续运行。
    """
    del image_path, appearance
    return {}


def register_initial_portrait(conn, project_id: str, name: str, image_path: str,
                              appearance: str, prompt: str, bible_version: int,
                              artifact_id: str | None = None) -> str:
    """初次定妆后登记角色首张定妆照（适用集 1~ 至今）。覆盖式：先清掉该角色全部旧分段。"""
    conn.execute("DELETE FROM character_portraits WHERE project_id=? AND character_name=?",
                 (project_id, name))
    portrait_id = new_id("portrait")
    pack_supported = _has_column(conn, "character_portraits", "pack_status")
    if pack_supported:
        conn.execute(
            "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, appearance, "
            "prompt, image_path, base_portrait_id, bible_version, artifact_id, pack_status, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (portrait_id, project_id, name, 1, None, appearance, prompt, image_path, None,
             bible_version, artifact_id, "legacy_partial", now()))
    else:
        conn.execute(
            "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, appearance, "
            "prompt, image_path, base_portrait_id, bible_version, artifact_id, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (portrait_id, project_id, name, 1, None, appearance, prompt, image_path, None,
             bible_version, artifact_id, now()))
    conn.commit()
    return portrait_id


def stage_initial_portrait(conn, project_id: str, name: str, image_path: str,
                           appearance: str, prompt: str, bible_version: int,
                           artifact_id: str | None = None) -> str:
    """暂存新的初始定妆包，不提前删除当前已采用包。

    STAGED_INITIAL_EP_START 是仅供生成/QA 使用的候选槽位，不会命中任何
    真实集号；整包验收通过后再由
    promote_staged_initial_portrait 以单个事务替换 ep_start=1 的当前包。
    """
    current = conn.execute(
        "SELECT id FROM character_portraits WHERE project_id=? AND character_name=? "
        "AND ep_end IS NULL AND ep_start<>? ORDER BY created_at DESC LIMIT 1",
        (project_id, name, STAGED_INITIAL_EP_START),
    ).fetchone()
    base_portrait_id = current["id"] if current else None
    conn.execute(
        "DELETE FROM character_portraits WHERE project_id=? AND character_name=? AND ep_start=?",
        (project_id, name, STAGED_INITIAL_EP_START),
    )
    portrait_id = new_id("portrait")
    pack_supported = _has_column(conn, "character_portraits", "pack_status")
    if pack_supported:
        conn.execute(
            "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, appearance, "
            "prompt, image_path, base_portrait_id, bible_version, artifact_id, pack_status, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (portrait_id, project_id, name, STAGED_INITIAL_EP_START, None, appearance, prompt, image_path, base_portrait_id,
             bible_version, artifact_id, "legacy_partial", now()),
        )
    else:
        conn.execute(
            "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, appearance, "
            "prompt, image_path, base_portrait_id, bible_version, artifact_id, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (portrait_id, project_id, name, STAGED_INITIAL_EP_START, None, appearance, prompt, image_path, base_portrait_id,
             bible_version, artifact_id, now()),
        )
    conn.commit()
    return portrait_id


def promote_staged_initial_portrait(conn, project_id: str, name: str, portrait_id: str) -> None:
    """整包验收通过后原子发布为全局初始定妆。

    手工重新定妆与剧情中的分集造型演进是两种操作：前者必须从第 1 集
    起替换全时间线，后者由 ``_refresh_portrait_on_drift`` 继续维护分段。
    """
    row = conn.execute(
        "SELECT id FROM character_portraits "
        "WHERE id=? AND project_id=? AND character_name=? AND ep_start=?",
        (portrait_id, project_id, name, STAGED_INITIAL_EP_START),
    ).fetchone()
    if not row:
        raise ValueError(f"定妆候选不存在：{name}")
    with conn:
        previous = conn.execute(
            "SELECT id FROM character_portraits "
            "WHERE project_id=? AND character_name=? AND id<>? AND ep_start>0 "
            "ORDER BY ep_start, created_at",
            (project_id, name, portrait_id),
        ).fetchall()
        minimum = conn.execute(
            "SELECT MIN(ep_start) AS value FROM character_portraits "
            "WHERE project_id=? AND character_name=? AND ep_start<=0",
            (project_id, name),
        ).fetchone()
        history_start = int(
            minimum["value"] if minimum and minimum["value"] is not None else 0
        ) - len(previous)
        for offset, previous_row in enumerate(previous):
            conn.execute(
                "UPDATE character_portraits SET ep_start=?, ep_end=0 WHERE id=?",
                (history_start + offset, previous_row["id"]),
            )
        conn.execute(
            "UPDATE character_portraits SET ep_start=1, ep_end=NULL WHERE id=?",
            (portrait_id,),
        )


def _open_portrait(
    conn, project_id: str, name: str, *, visual_entity_id: str | None = None
):
    """该角色当前开区间（ep_end IS NULL）的最新定妆照。

    ``visual_entity_id`` 非空时优先按视觉实体 ID 查询（跨集稳定，覆盖未
    具名角色，见 docs/CHARACTER_IDENTITY_ENTITY_DESIGN.md §4.2）；未命中，
    或该列尚未迁移落地（``sqlite3.OperationalError``），回退到既有的
    ``character_name`` 路径——迁移期双轨并存，向后兼容具名角色的既有行为。
    """
    if visual_entity_id:
        try:
            row = conn.execute(
                "SELECT * FROM character_portraits WHERE project_id=? "
                "AND visual_entity_id=? AND ep_end IS NULL AND ep_start<>? "
                "ORDER BY ep_start DESC LIMIT 1",
                (project_id, visual_entity_id, STAGED_INITIAL_EP_START),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        if row is not None:
            return row
    return conn.execute(
        "SELECT * FROM character_portraits WHERE project_id=? AND character_name=? "
        "AND ep_end IS NULL AND ep_start<>? ORDER BY ep_start DESC LIMIT 1",
        (project_id, name, STAGED_INITIAL_EP_START)).fetchone()


def appearance_for_episode(
    project_id: str,
    name: str,
    episode_no: int | None,
    *,
    visual_entity_id: str | None = None,
) -> str | None:
    """返回覆盖该集的定妆照有效外观锚点。

    ``appearance`` 是验收时单独持久化的结构化外观权威；不得再从
    prompt 文案中按关键词反向提取。``visual_entity_id`` 语义同
    ``portrait_for_episode``：优先按视觉实体 ID 查询，未命中回退
    ``character_name``。
    """
    if episode_no is None:
        return None
    if visual_entity_id:
        try:
            row = get_conn().execute(
                "SELECT appearance,prompt FROM character_portraits "
                "WHERE project_id=? AND visual_entity_id=? AND ep_start<=? "
                "AND (ep_end IS NULL OR ep_end>=?) "
                "ORDER BY ep_start DESC LIMIT 1",
                (project_id, visual_entity_id, episode_no, episode_no),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        if row:
            anchor = production_appearance_anchor(row["appearance"] or "")
            if anchor:
                return anchor
    try:
        row = get_conn().execute(
            "SELECT appearance,prompt FROM character_portraits "
            "WHERE project_id=? AND character_name=? AND ep_start<=? AND (ep_end IS NULL OR ep_end>=?) "
            "ORDER BY ep_start DESC LIMIT 1",
            (project_id, name, episode_no, episode_no)).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    return production_appearance_anchor(row["appearance"] or "") or None


def bible_for_episode(project_id: str, bible: "Bible", episode_no: int | None) -> "Bible":
    """返回 bible 的【本集视图】：每个角色的 appearance_canonical / ref_image_path 用覆盖该集的分段
    定妆照覆盖（未命中保留原值）。让关键帧文字锚点与参考图同段同源——同一集永远是同一套外观描述+图。

    已具名角色的 ``visual_entity_id`` 与其命名权威同构（``bible:{name}``，
    设计文档 §4.2 "已具名分支……零迁移成本"），无需 ``Character`` 新增字段
    即可派生：优先经由 ``visual_entity_id_for_resolution`` 计算（依赖尚未
    落地时回退等价的字面量拼接），把查图路径切到按视觉实体 ID 查询——
    对已生效的具名绑定行为不变，同时是向"未具名角色也能查到同一张脸"
    过渡的必要一步（本函数目前仍只遍历 ``bible.characters``，functional
    extras 的接入点在 app/production/prep_pack.py，属另一模块边界）。
    """
    if episode_no is None:
        return bible
    view = bible.model_copy(deep=True)
    for c in view.characters:
        if not c.name:
            continue
        visual_entity_id = (
            _visual_entity_id_for_resolution_safe({
                "resolution": "future_identity",
                "canonical_name": c.name,
            })
            or f"bible:{c.name}"
        )
        anchor = appearance_for_episode(
            project_id, c.name, episode_no, visual_entity_id=visual_entity_id
        )
        if anchor:
            c.appearance_canonical = anchor
        img = portrait_for_episode(
            project_id, c.name, episode_no, visual_entity_id=visual_entity_id
        )
        if img:
            c.ref_image_path = img
    return view


def portrait_views_for_episode(project_id: str, name: str, episode_no: int | None, *, ready_only: bool = False):
    """本集有效人物多视角包；供新链路使用。"""
    from app.multiview import portrait_views_for_episode as _views
    return _views(project_id, name, episode_no, ready_only=ready_only)


def redraw_prompt(style: str, appearance: str) -> str:
    """图生图重绘提示词：以参考图（旧定妆照）为身份锚点，只按新外观调整。"""
    return (
        f"{style}。参考图是同一角色的既有定妆照，请在保持【同一个人、同一角色身份】的前提下，"
        f"按新外观重绘其全身定妆照：{appearance}。"
        "正面站立，中性表情，双臂自然下垂，纯浅米色背景，全身完整可见，无文字无水印"
    )


async def _redraw_portrait(project_id: str, name: str, style: str, appearance: str,
                           *, base_path: str | None, ep_start: int) -> tuple[str, str]:
    """以上一张定妆照为底【图生图】重绘新定妆照，落盘。返回 (落盘路径, 生成 prompt)。"""
    prompt = redraw_prompt(style, appearance)
    image_inputs = None
    if base_path and Path(base_path).exists():
        image_inputs = [hiagent.data_url_from_file(base_path)]
    item = await hiagent.generate_image(
        prompt,
        size=config.REF_IMAGE_SIZE,
        image_inputs=image_inputs,
        call_meta={
            "asset_kind": "portrait",
            "character_name": name,
            "episode_no": ep_start,
            "portrait_mode": "redraw",
        })
    dest = _new_portrait_path(project_id, name, ep_start)
    await _save_image_item(item, dest)
    return dest, prompt


async def _generate_fresh_portrait(project_id: str, name: str, style: str, appearance: str,
                                   *, ep_start: int) -> tuple[str, str]:
    """为新登场角色生成一张全新定妆照（无底图，不走图生图），落盘。返回 (落盘路径, 生成 prompt)。"""
    prompt = portrait_prompt(style, appearance)
    item = await hiagent.generate_image(
        prompt,
        size=config.REF_IMAGE_SIZE,
        call_meta={
            "asset_kind": "portrait",
            "character_name": name,
            "episode_no": ep_start,
            "portrait_mode": "fresh",
        })
    dest = _new_portrait_path(project_id, name, ep_start)
    await _save_image_item(item, dest)
    return dest, prompt



def _update_bible_appearance(conn, project_id: str, name: str, appearance: str, ref_image_path: str) -> None:
    """漂移重绘后把 bible 里该角色的外观锚点/参考图同步成最新版（供人物谱 UI 展示）。
    真正驱动按集渲染的是 character_portraits 分段表 + bible_for_episode 的本集视图，所以这里只是展示用。"""
    def sync(data: dict) -> bool:
        for c in data.get("characters", []):
            if c.get("name") == name:
                c["appearance_canonical"] = appearance
                c["ref_image_path"] = ref_image_path
                return True
        return False

    mutate_bible_json(conn, project_id, sync)


def _append_character_to_bible(conn, project_id: str, char: dict) -> bool:
    """Atomically append a discovered character and advance bible lineage/version."""
    artifact_supported = (
        _has_column(conn, "projects", "bible_artifact_id")
        and _has_table(conn, "artifacts")
    )
    select_cols = "bible_json, bible_version"
    if artifact_supported:
        select_cols += ", bible_artifact_id"
    row = conn.execute(f"SELECT {select_cols} FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row or not row["bible_json"]:
        return False
    data = json.loads(row["bible_json"])
    if resolve_card_owner(Bible.model_validate(data), char.get("name") or "")[0] != "none":
        return False
    data.setdefault("characters", []).append(char)
    payload = json.dumps(data, ensure_ascii=False)
    next_artifact_id = None
    if artifact_supported:
        try:
            previous_id = row["bible_artifact_id"]
            artifact = evidence_repository.create_artifact(EvidenceArtifact(
                type="character_bible",
                scope_type="project",
                scope_id=project_id,
                status="approved",
                trust_level="T2",
                content=data,
                parent_artifact_ids=[previous_id] if previous_id else [],
                contract_version="character-bible-1.0.0",
                prompt_version="incremental-character-discovery-1.0.0",
                model_snapshot={"operation": "incremental_add", "character_name": char.get("name")},
            ))
            next_artifact_id = artifact["id"]
        except Exception as exc:  # noqa: BLE001 - authority mutation must fail closed
            code_ref(
                exc,
                action="append_character_bible_artifact",
                context={"project_id": project_id, "character_name": char.get("name")},
            )
            return False
    expected_version = int(row["bible_version"] or 0)
    if artifact_supported:
        cursor = conn.execute(
            "UPDATE projects SET bible_json=?,bible_version=?,bible_artifact_id=? "
            "WHERE id=? AND COALESCE(bible_version,0)=?",
            (
                payload,
                expected_version + 1,
                next_artifact_id,
                project_id,
                expected_version,
            ),
        )
    else:
        cursor = conn.execute(
            "UPDATE projects SET bible_json=?,bible_version=? "
            "WHERE id=? AND COALESCE(bible_version,0)=?",
            (payload, expected_version + 1, project_id, expected_version),
        )
    conn.commit()
    return cursor.rowcount == 1


async def _generate_discovered_character_portrait(
    project_id: str,
    name: str,
    style: str,
    appearance: str,
    *,
    ep_start: int,
    bible_version: int,
) -> dict:
    """为后续剧情自动发现的角色生成并原子接入定妆包。

    Score-only（PRD QA-SO #15）：第一张技术有效主图即可接入；QA 只评分，
    不因低分重生。多视角包完整性只看必需视角文件是否齐全。
    """
    conn = get_conn()
    pack_supported = _has_column(conn, "character_portraits", "pack_status")
    candidate = conn.execute(
        "SELECT * FROM character_portraits WHERE project_id=? AND character_name=? "
        "AND ep_start=? ORDER BY created_at DESC LIMIT 1",
        (project_id, name, ep_start),
    ).fetchone()

    async def _complete_candidate(
        row,
        *,
        primary_qa: dict | None = None,
        purge_on_failure: bool,
    ) -> dict:
        """补齐并发布同一个候选；重启恢复时不得再占用相同分段键。"""
        portrait_id = str(row["id"])
        image_path = str(row["image_path"] or "")
        candidate_appearance = str(row["appearance"] or appearance)
        if pack_supported and str(row["pack_status"] or "") == "ready" and row["ep_end"] is None:
            # 包已就绪且已是开区间：纯复用、不写库。分镜前的补齐重试走到这里时若再写库，撞上写锁
            # 就会被外层当成「定妆包生成失败」（ERR-20260902-30223f 刘备：三视角齐全却报失败）。
            return {"portrait_id": portrait_id, "image_path": image_path, "pack_status": "ready", "reused": True, "gate_retry_exhausted": False}
        try:
            if pack_supported:
                from app.multiview import ensure_character_multiview_pack, pack_result_ok

                existing_status = str(row["pack_status"] or "")
                if existing_status == "ready":
                    pack = {"status": "ready", "portrait_id": portrait_id, "reused": True}
                else:
                    pack = await ensure_character_multiview_pack(
                        project_id=project_id, portrait_id=portrait_id, character_name=name,
                        appearance=candidate_appearance, visual_style=style, ep_start=ep_start,
                        base_portrait_id=row["base_portrait_id"], primary_qa=primary_qa,
                    )
                if not pack_result_ok(pack):
                    conn.execute(
                        "UPDATE character_portraits SET pack_status='failed' WHERE id=?",
                        (portrait_id,),
                    )
                    conn.commit()
                    raise ContentGenerationError(f"角色多视角包结构不完整：{name}")

                # 候选在多视角完成前只占本集闭区间。发布时再原子切换为开区间；
                # 服务重启后重复执行本段仍更新同一 portrait_id，不会触发唯一键冲突。
                current = _open_portrait(conn, project_id, name)
                if current and current["id"] != portrait_id:
                    if int(current["ep_start"] or 1) < ep_start:
                        conn.execute(
                            "UPDATE character_portraits SET ep_end=? WHERE id=?",
                            (ep_start - 1, current["id"]),
                        )
                    else:
                        conn.execute("DELETE FROM character_portraits WHERE id=?", (current["id"],))
                conn.execute(
                    "UPDATE character_portraits SET ep_end=NULL,pack_status=? WHERE id=?",
                    ("ready", portrait_id),
                )
                conn.commit()

            _update_bible_appearance(conn, project_id, name, candidate_appearance, image_path)
            conn.commit()
        except Exception:
            # 新候选在本调用内失败可沿用原清理语义；重启前已经付费落盘的候选必须保留，
            # 让下一次恢复继续使用，不能因为恢复代码自身异常再次烧图。
            if purge_on_failure:
                from app.rejected_media import purge_character_portrait
                purge_character_portrait(conn, portrait_id)
            raise
        return {
            "portrait_id": portrait_id,
            "image_path": image_path,
            "pack_status": "ready",
            "reused": not purge_on_failure,
            "gate_retry_exhausted": False,
        }

    # 服务重启可能发生在主图和候选行已落盘、侧视角尚未完成之间。此时该行以
    # ep_start=ep_end 占用候选槽；必须在原 portrait_id 上续补，不能重生主图后重复 INSERT。
    if candidate is not None:
        candidate_path = str(candidate["image_path"] or "")
        if candidate_path and Path(candidate_path).is_file():
            return await _complete_candidate(candidate, purge_on_failure=False)
        from app.rejected_media import purge_character_portrait
        purge_character_portrait(conn, str(candidate["id"]))

    current = _open_portrait(conn, project_id, name)
    if current and current["image_path"] and Path(current["image_path"]).is_file():
        current_pack = current["pack_status"] if pack_supported else "ready"
        if current_pack == "ready" and int(current["ep_start"] or 1) <= ep_start:
            return {
                "portrait_id": current["id"], "image_path": current["image_path"],
                "pack_status": "ready", "reused": True,
            }

    artifact_supported = (
        _has_column(conn, "character_portraits", "artifact_id")
        and _has_table(conn, "artifacts")
    )
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    parent_ids = []
    if project and "bible_artifact_id" in project.keys() and project["bible_artifact_id"]:
        parent_ids.append(project["bible_artifact_id"])

    artifact = None
    qa = None
    image_path, prompt = await _generate_fresh_portrait(
        project_id, name, style, appearance, ep_start=ep_start,
    )
    if artifact_supported:
        qa = await _review_portrait_asset(image_path, appearance)
        artifact = record_reference_asset(
            asset_type="character_portrait",
            scope_id=f"{project_id}:{name}:{ep_start}",
            file_path=image_path,
            content={
                "character_name": name,
                "appearance": appearance,
                "prompt": prompt,
                "episode_start": ep_start,
                "attempt": 1,
                "origin": "automatic_character_discovery",
            },
            parent_artifact_ids=parent_ids,
            qa=qa,
        )
        if artifact["status"] not in {"approved", "validated"}:
            if current:
                return {
                    "portrait_id": current["id"], "image_path": current["image_path"],
                    "pack_status": current["pack_status"] if pack_supported else "ready",
                    "reused": True, "gate_retry_exhausted": True,
                }
            raise hiagent.ProviderError(f"新角色定妆照文件不可用：{name}")

    portrait_id = new_id("portrait")
    values = {
        "id": portrait_id,
        "project_id": project_id,
        "character_name": name,
        "ep_start": ep_start,
        # 多视角尚未通过时只占本集候选槽，不开放右区间。
        "ep_end": ep_start if pack_supported else None,
        "appearance": appearance,
        "prompt": prompt,
        "image_path": image_path,
        "base_portrait_id": current["id"] if current else None,
        "bible_version": bible_version,
        "created_at": now(),
    }
    if _has_column(conn, "character_portraits", "artifact_id"):
        values["artifact_id"] = artifact["id"] if artifact else None
    if pack_supported:
        values["pack_status"] = "generating"
    columns = list(values)
    conn.execute(
        f"INSERT INTO character_portraits({', '.join(columns)}) "
        f"VALUES({', '.join('?' for _ in columns)})",
        tuple(values[column] for column in columns),
    )
    conn.commit()
    inserted = conn.execute(
        "SELECT * FROM character_portraits WHERE id=?", (portrait_id,),
    ).fetchone()
    return await _complete_candidate(
        inserted,
        primary_qa=qa,
        purge_on_failure=True,
    )

